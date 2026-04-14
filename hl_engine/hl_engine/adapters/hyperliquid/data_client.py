"""
HyperliquidLiveMarketDataClient — asyncio-native WebSocket market data client.

Uses the `websockets` library directly (NOT the SDK's threaded WebsocketManager)
to stay compatible with NautilusTrader's asyncio event loop.
"""

import asyncio
import json
import time
from typing import Optional

import aiohttp
import websockets
from websockets.connection import State as WsState

from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import (
    Bar,
    BarType,
    CustomData,
    DataType,
    OrderBookDelta,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggressorSide,
    BookAction,
    OrderSide,
    BarAggregation,
)
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from hl_engine.adapters.hyperliquid.constants import (
    HYPERLIQUID_VENUE,
    HL_INFO_ENDPOINT,
    HL_PING_INTERVAL_SECS,
    WS_TYPE_L2_BOOK,
    WS_TYPE_TRADES,
    WS_TYPE_CANDLE,
    WS_TYPE_ACTIVE_ASSET_CTX,
    WS_TYPE_WEB_DATA2,
    BAR_INTERVAL_MAP,
)
from hl_engine.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData


class HyperliquidLiveMarketDataClient(LiveMarketDataClient):
    """
    Live market data client for Hyperliquid perpetual futures.

    Handles:
      - L2 order book (full snapshot + incremental deltas)
      - Trade ticks
      - OHLCV bars (candles)
      - Funding rate / open interest (custom data types)
      - Liquidation events (custom data type)
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        msgbus,
        cache,
        clock,
        instrument_provider: HyperliquidInstrumentProvider,
        base_url: str,
        ws_url: str,
        wallet_address: Optional[str] = None,
        config=None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=HYPERLIQUID_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url
        self._wallet_address = wallet_address
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: dict[str, dict] = {}  # channel_key → sub message
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._book_snapshots: dict[str, bool] = {}  # coin → got_snapshot

    # ------------------------------------------------------------------
    # NautilusTrader lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        await self._instrument_provider.load_all_async()
        for instrument in self._instrument_provider.list_all():
            self._cache.add_instrument(instrument)
        self._ws = await websockets.connect(self._ws_url, ping_interval=None)
        self._ping_task = self.create_task(self._ping_loop())
        self._recv_task = self.create_task(self._recv_loop())
        self._log.info(f"Connected to Hyperliquid WS: {self._ws_url}")

    async def _disconnect(self) -> None:
        if self._ping_task:
            self._ping_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        self._log.info("Disconnected from Hyperliquid WS")

    # ------------------------------------------------------------------
    # Subscription handlers (called by NautilusTrader engine)
    # ------------------------------------------------------------------

    async def _subscribe_order_book_deltas(self, command) -> None:
        instrument_id = command.instrument_id
        coin = instrument_id.symbol.value.split("-")[0]
        sub = {"method": "subscribe", "subscription": {"type": WS_TYPE_L2_BOOK, "coin": coin}}
        await self._send(sub)
        self._subscriptions[f"l2Book:{coin}"] = sub

    async def _subscribe_trade_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        coin = instrument_id.symbol.value.split("-")[0]
        sub = {"method": "subscribe", "subscription": {"type": WS_TYPE_TRADES, "coin": coin}}
        await self._send(sub)
        self._subscriptions[f"trades:{coin}"] = sub

    async def _subscribe_bars(self, command) -> None:
        bar_type: BarType = command.bar_type
        coin = bar_type.instrument_id.symbol.value.split("-")[0]
        interval = self._bar_type_to_interval(bar_type)
        sub = {
            "method": "subscribe",
            "subscription": {"type": WS_TYPE_CANDLE, "coin": coin, "interval": interval},
        }
        await self._send(sub)
        self._subscriptions[f"candle:{coin}:{interval}"] = sub

    async def _subscribe(self, command) -> None:
        from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData

        data_type = command.data_type
        instrument_id: Optional[InstrumentId] = data_type.metadata.get("instrument_id")

        if data_type.type in (FundingRateData, OpenInterestData) and instrument_id:
            coin = instrument_id.symbol.value.split("-")[0]
            sub = {
                "method": "subscribe",
                "subscription": {"type": WS_TYPE_ACTIVE_ASSET_CTX, "coin": coin},
            }
            await self._send(sub)
            self._subscriptions[f"activeAssetCtx:{coin}"] = sub

        elif data_type.type == LiquidationData and self._wallet_address:
            sub = {
                "method": "subscribe",
                "subscription": {
                    "type": WS_TYPE_WEB_DATA2,
                    "user": self._wallet_address,
                },
            }
            await self._send(sub)
            self._subscriptions["webData2"] = sub

    # ------------------------------------------------------------------
    # Historical data request
    # ------------------------------------------------------------------

    async def _request_bars(self, request) -> None:
        bar_type: BarType = request.bar_type
        limit: int = int(request.limit or 200)
        coin = bar_type.instrument_id.symbol.value.split("-")[0]
        interval = self._bar_type_to_interval(bar_type)
        now_ms = int(time.time() * 1000)
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": 0, "endTime": now_ms},
        }
        info_url = self._base_url + HL_INFO_ENDPOINT
        async with aiohttp.ClientSession() as session:
            async with session.post(info_url, json=payload) as resp:
                resp.raise_for_status()
                candles = await resp.json()

        # Take last `limit` candles
        candles = candles[-limit:] if limit and len(candles) > limit else candles
        instrument = self._cache.instrument(bar_type.instrument_id)
        if instrument is None:
            return

        bars = [self._parse_candle(c, bar_type, instrument) for c in candles]
        for bar in bars:
            self._handle_data(bar)

    # ------------------------------------------------------------------
    # WebSocket receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async for raw in self._ws:
                    backoff = 1.0  # reset on successful message
                    try:
                        msg = json.loads(raw)
                        channel = msg.get("channel", "")
                        if channel == WS_TYPE_L2_BOOK:
                            self._handle_l2_book(msg)
                        elif channel == WS_TYPE_TRADES:
                            self._handle_trades(msg)
                        elif channel == WS_TYPE_CANDLE:
                            self._handle_candle(msg)
                        elif channel == WS_TYPE_ACTIVE_ASSET_CTX:
                            self._handle_asset_ctx(msg)
                        elif channel == WS_TYPE_WEB_DATA2:
                            self._handle_web_data2(msg)
                    except Exception as e:
                        self._log.error(f"Error handling WS message: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._log.warning(f"WebSocket connection lost: {e}")

            # Reconnect with exponential backoff (cap at 60s)
            self._log.info(f"Reconnecting in {backoff:.0f}s …")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

            try:
                if self._ws:
                    await self._ws.close()
            except Exception:
                pass

            try:
                self._ws = await websockets.connect(self._ws_url, ping_interval=None)
                self._book_snapshots.clear()
                # Re-subscribe to all active channels
                for sub in list(self._subscriptions.values()):
                    await self._ws.send(json.dumps(sub))
                self._log.info("Reconnected to Hyperliquid WS and re-subscribed")
                backoff = 1.0
            except Exception as e:
                self._log.error(f"Reconnect attempt failed: {e}")

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(HL_PING_INTERVAL_SECS)
            if self._ws and self._ws.state is WsState.OPEN:
                await self._ws.send(json.dumps({"method": "ping"}))

    async def _send(self, msg: dict) -> None:
        if self._ws and self._ws.state is WsState.OPEN:
            await self._ws.send(json.dumps(msg))

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_l2_book(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return

        levels = data.get("levels", [[], []])
        ts_event = int(data.get("time", 0)) * 1_000_000  # ms → ns
        ts_init = self._clock.timestamp_ns()

        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        is_snapshot = not self._book_snapshots.get(coin, False)
        deltas = []

        # levels[0] = bids, levels[1] = asks
        for side_idx, side_levels in enumerate(levels[:2]):
            order_side = OrderSide.BUY if side_idx == 0 else OrderSide.SELL
            action = BookAction.CLEAR if (is_snapshot and side_idx == 0) else BookAction.UPDATE

            if is_snapshot and side_idx == 0:
                # Emit a CLEAR delta first
                clear_delta = OrderBookDelta.clear(instrument_id, 0, ts_event, ts_init)
                deltas.append(clear_delta)

            for level in side_levels:
                px = float(level["px"])
                sz = float(level["sz"])
                action = BookAction.DELETE if sz == 0.0 else (
                    BookAction.ADD if is_snapshot else BookAction.UPDATE
                )
                from nautilus_trader.model.data import BookOrder
                order = BookOrder(
                    order_side,
                    Price(px, instrument.price_precision),
                    Quantity(sz, instrument.size_precision),
                    0,
                )
                delta = OrderBookDelta(
                    instrument_id,
                    action,
                    order,
                    0,
                    0,
                    ts_event,
                    ts_init,
                )
                deltas.append(delta)

        if deltas:
            # Mark last delta with F_LAST flag
            last = deltas[-1]
            deltas[-1] = OrderBookDelta(
                last.instrument_id,
                last.action,
                last.order,
                1,  # F_LAST
                last.sequence,
                last.ts_event,
                last.ts_init,
            )
            for delta in deltas:
                self._handle_data(delta)

        self._book_snapshots[coin] = True

    def _handle_trades(self, msg: dict) -> None:
        trades = msg.get("data", [])
        for trade in trades:
            coin = trade.get("coin", "")
            instrument_id = self._coin_to_instrument_id(coin)
            if instrument_id is None:
                continue
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                continue

            side_str = trade.get("side", "B")
            aggressor_side = (
                AggressorSide.BUYER if side_str == "B" else AggressorSide.SELLER
            )
            ts_event = int(trade.get("time", 0)) * 1_000_000  # ms → ns
            trade_hash = trade.get("hash", "0x0000000000000000")
            trade_id = TradeId(trade_hash[:16])

            tick = TradeTick(
                instrument_id=instrument_id,
                price=Price(float(trade["px"]), instrument.price_precision),
                size=Quantity(float(trade["sz"]), instrument.size_precision),
                aggressor_side=aggressor_side,
                trade_id=trade_id,
                ts_event=ts_event,
                ts_init=self._clock.timestamp_ns(),
            )
            self._handle_data(tick)

    def _handle_candle(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("s", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        # Reconstruct bar_type from interval
        interval = data.get("i", "1m")
        bar_type = self._interval_to_bar_type(instrument_id, interval)
        bar = self._parse_candle(data, bar_type, instrument)
        self._handle_data(bar)

    def _handle_asset_ctx(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        ctx = data.get("ctx", {})
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return

        ts_event = self._clock.timestamp_ns()
        ts_init = ts_event

        # Publish FundingRateData
        funding = FundingRateData(
            instrument_id=instrument_id,
            rate=float(ctx.get("funding", 0.0)),
            next_funding_time=int(ctx.get("nextFundingTime", 0)) * 1_000_000,
            open_interest=float(ctx.get("openInterest", 0.0)),
            ts_event=ts_event,
            ts_init=ts_init,
        )
        self._handle_data(CustomData(DataType(FundingRateData, metadata={"instrument_id": instrument_id}), funding))

        # Publish OpenInterestData
        oi = float(ctx.get("openInterest", 0.0))
        mark_px = float(ctx.get("markPx", 0.0))
        oi_data = OpenInterestData(
            instrument_id=instrument_id,
            open_interest=oi,
            open_interest_usd=oi * mark_px,
            ts_event=ts_event,
            ts_init=ts_init,
        )
        self._handle_data(CustomData(DataType(OpenInterestData, metadata={"instrument_id": instrument_id}), oi_data))

    def _handle_web_data2(self, msg: dict) -> None:
        data = msg.get("data", {})
        liquidations = data.get("liquidations", [])
        ts_event = self._clock.timestamp_ns()
        ts_init = ts_event

        for liq in liquidations:
            coin = liq.get("coin", "")
            instrument_id = self._coin_to_instrument_id(coin)
            if instrument_id is None:
                continue

            side_str = liq.get("side", "")
            side = "LONG" if side_str in ("B", "buy", "LONG") else "SHORT"
            qty = float(liq.get("sz", 0.0))
            px = float(liq.get("px", 0.0))

            liq_data = LiquidationData(
                instrument_id=instrument_id,
                side=side,
                quantity=qty,
                price=px,
                usd_value=qty * px,
                ts_event=ts_event,
                ts_init=ts_init,
            )
            self._handle_data(CustomData(DataType(LiquidationData, metadata={"instrument_id": instrument_id}), liq_data))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _coin_to_instrument_id(self, coin: str) -> Optional[InstrumentId]:
        from nautilus_trader.model.identifiers import Symbol
        if not coin:
            return None
        symbol = Symbol(f"{coin}-USD")
        from nautilus_trader.model.identifiers import InstrumentId as IID
        instrument_id = IID(symbol=symbol, venue=HYPERLIQUID_VENUE)
        # Verify it exists in cache
        if self._cache.instrument(instrument_id) is not None:
            return instrument_id
        return None

    def _parse_candle(self, data: dict, bar_type: BarType, instrument) -> Bar:
        ts_event = int(data.get("T", data.get("t", 0))) * 1_000_000  # close time, ms → ns
        pp = instrument.price_precision
        sp = instrument.size_precision
        return Bar(
            bar_type=bar_type,
            open=Price(float(data["o"]), pp),
            high=Price(float(data["h"]), pp),
            low=Price(float(data["l"]), pp),
            close=Price(float(data["c"]), pp),
            volume=Quantity(float(data["v"]), sp),
            ts_event=ts_event,
            ts_init=self._clock.timestamp_ns(),
        )

    @staticmethod
    def _bar_type_to_interval(bar_type: BarType) -> str:
        step = bar_type.spec.step
        agg = bar_type.spec.aggregation
        if agg == BarAggregation.MINUTE:
            return BAR_INTERVAL_MAP.get(step, "1m")
        elif agg == BarAggregation.HOUR:
            return BAR_INTERVAL_MAP.get(step * 60, "1h")
        elif agg == BarAggregation.DAY:
            return "1d"
        return "1m"

    @staticmethod
    def _interval_to_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
        from nautilus_trader.model.data import BarSpecification, BarType as BT
        from nautilus_trader.model.enums import BarAggregation, PriceType
        _map = {
            "1m": (1, BarAggregation.MINUTE),
            "3m": (3, BarAggregation.MINUTE),
            "5m": (5, BarAggregation.MINUTE),
            "15m": (15, BarAggregation.MINUTE),
            "30m": (30, BarAggregation.MINUTE),
            "1h": (1, BarAggregation.HOUR),
            "4h": (4, BarAggregation.HOUR),
            "1d": (1, BarAggregation.DAY),
        }
        step, agg = _map.get(interval, (1, BarAggregation.MINUTE))
        from nautilus_trader.model.enums import AggregationSource
        spec = BarSpecification(step, agg, PriceType.LAST)
        return BT(instrument_id, spec, AggregationSource.EXTERNAL)
