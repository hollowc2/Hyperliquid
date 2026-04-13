"""
ZmqLiveDataClient — NautilusTrader LiveMarketDataClient that receives market data
from the orchestrator via ZMQ PUB/SUB instead of directly from Hyperliquid.

Key behaviours:
  - Subscribes to ZMQ topics matching NT subscription requests
  - Detects seq gaps and requests REST snapshots for resync
  - Heartbeat watchdog: warns at >1.5s stale, resyncs at >5s stale
  - On subscribe: immediately requests snapshot for consistent initial state
  - HWM set to 1000 (ZMQ drops oldest on overflow)

Data reconstruction mirrors HyperliquidLiveMarketDataClient handlers exactly.
ts_init comes from orchestrator (ts_ns in frame) — orchestrator is time source of truth.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp
import zmq.asyncio

from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import (
    Bar,
    BarType,
    CustomData,
    DataType,
    OrderBookDelta,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggressorSide,
    BarAggregation,
    BookAction,
    OrderSide,
)
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity

from hl_engine.adapters.hyperliquid.constants import HYPERLIQUID_VENUE
from hl_engine.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData
from hl_engine.transport.serialization import unwrap

log = logging.getLogger(__name__)

_HEARTBEAT_WARN_SECS = 1.5
_HEARTBEAT_RESYNC_SECS = 5.0


class ZmqLiveDataClient(LiveMarketDataClient):
    """
    Live market data client that receives data from the orchestrator over ZMQ.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
    client_id : ClientId
    msgbus, cache, clock : NautilusTrader core objects
    instrument_provider : HyperliquidInstrumentProvider
    zmq_data_url : str
        ZMQ PUB socket URL (e.g. "tcp://orchestrator:5555")
    orchestrator_rest_url : str
        Orchestrator REST base URL for snapshot and bar requests.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        msgbus,
        cache,
        clock,
        instrument_provider: HyperliquidInstrumentProvider,
        zmq_data_url: str,
        orchestrator_rest_url: str,
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
        self._zmq_data_url = zmq_data_url
        self._rest_url = orchestrator_rest_url.rstrip("/")

        self._zmq_ctx: Optional[zmq.asyncio.Context] = None
        self._zmq_sock: Optional[zmq.asyncio.Socket] = None

        # Per-topic sequence tracking for gap detection
        self._last_seq: dict[bytes, Optional[int]] = {}
        # Reset seq tracking after a snapshot (accept any seq as first)
        self._seq_reset_topics: set[bytes] = set()

        # Subscribed instrument ids (for heartbeat resync)
        self._subscribed_instruments: set[str] = set()

        self._last_heartbeat_ts: float = time.monotonic()

        self._recv_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # NautilusTrader lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        # Load instruments from HL REST (public, no auth needed)
        await self._instrument_provider.load_all_async()
        for instrument in self._instrument_provider.list_all():
            self._cache.add_instrument(instrument)
        self._log.info(f"Loaded {len(self._instrument_provider.list_all())} instruments")

        # Create ZMQ SUB socket
        self._zmq_ctx = zmq.asyncio.Context()
        self._zmq_sock = self._zmq_ctx.socket(zmq.SUB)
        self._zmq_sock.setsockopt(zmq.RCVHWM, 1000)
        self._zmq_sock.connect(self._zmq_data_url)
        # Always subscribe to heartbeat
        self._zmq_sock.setsockopt(zmq.SUBSCRIBE, b"heartbeat")
        self._log.info(f"ZMQ SUB connected to {self._zmq_data_url}")

        self._recv_task = self.create_task(self._recv_loop())
        self._watchdog_task = self.create_task(self._heartbeat_watchdog())

    async def _disconnect(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._zmq_sock:
            self._zmq_sock.close()
        if self._zmq_ctx:
            self._zmq_ctx.term()
        self._log.info("ZMQ data client disconnected")

    # ------------------------------------------------------------------
    # Subscription handlers
    # ------------------------------------------------------------------

    async def _subscribe_order_book_deltas(self, command) -> None:
        instrument_id = command.instrument_id
        coin = _instrument_id_to_coin(instrument_id)
        topic = f"orderbook.{instrument_id}".encode()
        self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
        self._subscribed_instruments.add(str(instrument_id))
        self._log.info(f"Subscribed ZMQ: {topic.decode()}")
        # Request initial snapshot immediately
        await self._request_snapshot(str(instrument_id))

    async def _subscribe_trade_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        topic = f"trades.{instrument_id}".encode()
        self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
        self._subscribed_instruments.add(str(instrument_id))
        self._log.info(f"Subscribed ZMQ: {topic.decode()}")

    async def _subscribe_bars(self, command) -> None:
        bar_type: BarType = command.bar_type
        instrument_id = bar_type.instrument_id
        coin = _instrument_id_to_coin(instrument_id)
        topic = f"bar.{instrument_id}.1m".encode()
        self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
        self._subscribed_instruments.add(str(instrument_id))
        self._log.info(f"Subscribed ZMQ: {topic.decode()}")

    async def _subscribe(self, command) -> None:
        """Handle CustomData subscriptions (FundingRateData, OI, LiquidationData)."""
        data_type = command.data_type
        instrument_id: Optional[InstrumentId] = data_type.metadata.get("instrument_id")

        if data_type.type in (FundingRateData, OpenInterestData) and instrument_id:
            topic = f"funding.{instrument_id}".encode()
            self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
            self._subscribed_instruments.add(str(instrument_id))
            self._log.info(f"Subscribed ZMQ: {topic.decode()}")
        elif data_type.type == LiquidationData and instrument_id:
            topic = f"liquidation.{instrument_id}".encode()
            self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
            self._log.info(f"Subscribed ZMQ: {topic.decode()}")

    # ------------------------------------------------------------------
    # Historical bars (via orchestrator REST)
    # ------------------------------------------------------------------

    async def _request_bars(self, request) -> None:
        bar_type: BarType = request.bar_type
        limit: int = int(request.limit or 200)
        coin = _instrument_id_to_coin(bar_type.instrument_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._rest_url}/bars",
                    params={"coin": coin, "interval": "1m", "limit": limit},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            candles = data.get("candles", [])
            instrument = self._cache.instrument(bar_type.instrument_id)
            if instrument is None:
                return
            for c in candles:
                # Candle time field is in ms; convert to ns for ts_ns
                ts_ns = int(c.get("t", 0)) * 1_000_000
                bar = self._parse_candle(c, bar_type, instrument, ts_ns)
                self._handle_data(bar)
        except Exception as e:
            self._log.warning(f"Failed to request bars for {coin}: {e}")

    # ------------------------------------------------------------------
    # ZMQ receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        while True:
            try:
                [topic_bytes, frame] = await self._zmq_sock.recv_multipart()
                try:
                    seq, ts_ns, type_str, data = unwrap(frame)
                except ValueError as e:
                    self._log.warning(f"Bad ZMQ frame: {e}")
                    continue

                if type_str == "heartbeat":
                    self._last_heartbeat_ts = time.monotonic()
                    continue

                # Seq gap detection
                if topic_bytes in self._seq_reset_topics:
                    self._last_seq[topic_bytes] = seq
                    self._seq_reset_topics.discard(topic_bytes)
                else:
                    prev = self._last_seq.get(topic_bytes)
                    if prev is not None and seq != prev + 1:
                        self._log.warning(
                            f"Seq gap on {topic_bytes.decode()}: expected {prev+1}, got {seq}"
                        )
                        # Request resync for the instrument in this topic
                        await self._resync_from_topic(topic_bytes)
                    self._last_seq[topic_bytes] = seq

                # Dispatch by type
                if type_str == "l2book":
                    self._handle_l2book(data, ts_ns)
                elif type_str == "trade":
                    self._handle_trade(data, ts_ns)
                elif type_str == "candle":
                    self._handle_candle_data(data, ts_ns)
                elif type_str == "asset_ctx":
                    self._handle_asset_ctx(data, ts_ns)
                elif type_str == "liquidation":
                    self._handle_liquidation(data, ts_ns)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"ZMQ recv error: {e}")

    # ------------------------------------------------------------------
    # Heartbeat watchdog
    # ------------------------------------------------------------------

    async def _heartbeat_watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            staleness = time.monotonic() - self._last_heartbeat_ts
            if staleness > _HEARTBEAT_RESYNC_SECS:
                self._log.warning(
                    f"Heartbeat stale {staleness:.1f}s — requesting resync for all instruments"
                )
                for instrument_id_str in list(self._subscribed_instruments):
                    await self._request_snapshot(instrument_id_str)
                self._last_heartbeat_ts = time.monotonic()
            elif staleness > _HEARTBEAT_WARN_SECS:
                self._log.warning(f"Heartbeat stale {staleness:.1f}s — orchestrator may be slow")

    # ------------------------------------------------------------------
    # Snapshot resync
    # ------------------------------------------------------------------

    async def _request_snapshot(self, instrument_id_str: str) -> None:
        """Request full L2 book snapshot from orchestrator REST and apply it."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._rest_url}/snapshot/{instrument_id_str}"
                ) as resp:
                    if resp.status == 404:
                        return  # no snapshot yet (orchestrator just started)
                    resp.raise_for_status()
                    snapshot = await resp.json()

            coin = snapshot.get("coin", "")
            ts_ns = snapshot.get("ts_ns", time.time_ns())
            bids = snapshot.get("bids", [])
            asks = snapshot.get("asks", [])

            # Convert snapshot format to levels format used by _handle_l2book
            bid_levels = [{"px": str(px), "sz": sz, "n": 1} for px, sz in bids]
            ask_levels = [{"px": str(px), "sz": sz, "n": 1} for px, sz in asks]
            data = {
                "coin": coin,
                "time": ts_ns // 1_000_000,
                "levels": [bid_levels, ask_levels],
                "is_snapshot": True,
            }
            self._handle_l2book(data, ts_ns)

            # Mark topic seq as reset so we accept any next seq
            topic = f"orderbook.{instrument_id_str}".encode()
            self._seq_reset_topics.add(topic)
            self._last_seq.pop(topic, None)
            self._log.info(f"Snapshot applied for {instrument_id_str}")
        except Exception as e:
            self._log.warning(f"Snapshot request failed for {instrument_id_str}: {e}")

    async def _resync_from_topic(self, topic: bytes) -> None:
        """Infer instrument_id from topic and request snapshot."""
        topic_str = topic.decode()
        if topic_str.startswith("orderbook."):
            instrument_id_str = topic_str[len("orderbook."):]
            await self._request_snapshot(instrument_id_str)

    # ------------------------------------------------------------------
    # Message handlers (mirror HyperliquidLiveMarketDataClient)
    # ------------------------------------------------------------------

    def _handle_l2book(self, data: dict, ts_ns: int) -> None:
        coin = data.get("coin", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        levels = data.get("levels", [[], []])
        ts_event = int(data.get("time", 0)) * 1_000_000  # ms → ns
        ts_init = ts_ns
        is_snapshot = data.get("is_snapshot", False)

        deltas = []
        for side_idx, side_levels in enumerate(levels[:2]):
            order_side = OrderSide.BUY if side_idx == 0 else OrderSide.SELL

            if is_snapshot and side_idx == 0:
                clear_delta = OrderBookDelta.clear(instrument_id, 0, ts_event, ts_init)
                deltas.append(clear_delta)

            for level in side_levels:
                px = float(level["px"])
                sz = float(level["sz"])
                action = BookAction.DELETE if sz == 0.0 else (
                    BookAction.ADD if is_snapshot else BookAction.UPDATE
                )
                order = BookOrder(
                    order_side,
                    Price(px, instrument.price_precision),
                    Quantity(sz, instrument.size_precision),
                    0,
                )
                deltas.append(OrderBookDelta(instrument_id, action, order, 0, 0, ts_event, ts_init))

        if deltas:
            last = deltas[-1]
            deltas[-1] = OrderBookDelta(
                last.instrument_id, last.action, last.order, 1, last.sequence, last.ts_event, last.ts_init
            )
            for delta in deltas:
                self._handle_data(delta)

    def _handle_trade(self, data: dict, ts_ns: int) -> None:
        coin = data.get("coin", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        aggressor = AggressorSide.BUYER if data.get("side", "B") == "B" else AggressorSide.SELLER
        ts_event = int(data.get("time", 0)) * 1_000_000
        hash_ = data.get("hash", "0x0000000000000000")

        tick = TradeTick(
            instrument_id=instrument_id,
            price=Price(float(data["px"]), instrument.price_precision),
            size=Quantity(float(data["sz"]), instrument.size_precision),
            aggressor_side=aggressor,
            trade_id=TradeId(hash_[:16]),
            ts_event=ts_event,
            ts_init=ts_ns,
        )
        self._handle_data(tick)

    def _handle_candle_data(self, data: dict, ts_ns: int) -> None:
        coin = data.get("s", data.get("coin", ""))
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        interval = data.get("i", "1m")
        bar_type = _interval_to_bar_type(instrument_id, interval)
        bar = self._parse_candle(data, bar_type, instrument, ts_ns)
        self._handle_data(bar)

    def _handle_asset_ctx(self, data: dict, ts_ns: int) -> None:
        coin = data.get("coin", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return

        funding = FundingRateData(
            instrument_id=instrument_id,
            rate=float(data.get("funding", 0.0)),
            next_funding_time=int(data.get("nextFundingTime", 0)) * 1_000_000,
            open_interest=float(data.get("openInterest", 0.0)),
            ts_event=ts_ns,
            ts_init=ts_ns,
        )
        self._handle_data(CustomData(DataType(FundingRateData, metadata={"instrument_id": instrument_id}), funding))

        oi = float(data.get("openInterest", 0.0))
        mark_px = float(data.get("markPx", 0.0))
        oi_data = OpenInterestData(
            instrument_id=instrument_id,
            open_interest=oi,
            open_interest_usd=oi * mark_px,
            ts_event=ts_ns,
            ts_init=ts_ns,
        )
        self._handle_data(CustomData(DataType(OpenInterestData, metadata={"instrument_id": instrument_id}), oi_data))

    def _handle_liquidation(self, data: dict, ts_ns: int) -> None:
        coin = data.get("coin", "")
        instrument_id = self._coin_to_instrument_id(coin)
        if instrument_id is None:
            return

        side_raw = data.get("side", "")
        side = "LONG" if side_raw in ("B", "buy", "LONG") else "SHORT"
        qty = float(data.get("sz", 0.0))
        px = float(data.get("px", 0.0))

        liq = LiquidationData(
            instrument_id=instrument_id,
            side=side,
            quantity=qty,
            price=px,
            usd_value=qty * px,
            ts_event=ts_ns,
            ts_init=ts_ns,
        )
        self._handle_data(CustomData(DataType(LiquidationData, metadata={"instrument_id": instrument_id}), liq))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _coin_to_instrument_id(self, coin: str) -> Optional[InstrumentId]:
        if not coin:
            return None
        instrument_id = InstrumentId(symbol=Symbol(f"{coin}-USD"), venue=HYPERLIQUID_VENUE)
        if self._cache.instrument(instrument_id) is not None:
            return instrument_id
        return None

    def _parse_candle(self, data: dict, bar_type: BarType, instrument, ts_ns: int) -> Bar:
        ts_event = int(data.get("T", data.get("t", 0))) * 1_000_000
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
            ts_init=ts_ns,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _instrument_id_to_coin(instrument_id: InstrumentId) -> str:
    return instrument_id.symbol.value.split("-")[0]


def _interval_to_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
    from nautilus_trader.model.data import BarSpecification
    from nautilus_trader.model.enums import AggregationSource, PriceType
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
    spec = BarSpecification(step, agg, PriceType.LAST)
    return BarType(instrument_id, spec, AggregationSource.EXTERNAL)
