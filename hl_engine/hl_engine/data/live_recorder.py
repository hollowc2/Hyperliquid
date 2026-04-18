"""
Standalone Hyperliquid live data recorder.

Connects to the Hyperliquid WebSocket and records:
  - L2 order book snapshots → OrderBookDelta
  - Trade ticks            → TradeTick
  - 1-minute candles       → Bar
  - Funding rate + OI      → FundingRateData / OpenInterestData  (via activeAssetCtx)
  - Liquidations           → LiquidationData  (via webData2, requires wallet_address)

Data is buffered in memory and flushed to a NautilusTrader
ParquetDataCatalog at a configurable interval (default 60s).
Custom types (funding/OI/liquidations) are written to
  data/catalog/custom/{type}/{instrument_id}/{ts_start}_{ts_end}.parquet
Runs independently of the NautilusTrader trading engine.

Usage:
    recorder = HyperliquidRecorder(
        coins=["BTC", "ETH"],
        catalog_path="data/catalog",
        wallet_address="0xYourAddress",   # optional, enables liquidation recording
    )
    asyncio.run(recorder.run())
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq
import websockets
from websockets.connection import State as WsState

from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import (
    Bar,
    BarSpecification,
    BarType,
    OrderBookDelta,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggressorSide,
    BarAggregation,
    BookAction,
    CurrencyType,
    OrderSide,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from hl_engine.adapters.hyperliquid.constants import (
    HL_BASE_URL,
    HL_INFO_ENDPOINT,
    HL_WS_URL,
    HL_PING_INTERVAL_SECS,
    HYPERLIQUID_VENUE,
    WS_TYPE_L2_BOOK,
    WS_TYPE_TRADES,
    WS_TYPE_CANDLE,
    WS_TYPE_ACTIVE_ASSET_CTX,
    WS_TYPE_WEB_DATA2,
)

# ---------------------------------------------------------------------------
# Pyarrow schemas for custom data types
# ---------------------------------------------------------------------------

_FUNDING_SCHEMA = pa.schema([
    ("instrument_id", pa.string()),
    ("rate", pa.float64()),
    ("next_funding_time", pa.int64()),
    ("open_interest", pa.float64()),
    ("ts_event", pa.int64()),
    ("ts_init", pa.int64()),
])

_OI_SCHEMA = pa.schema([
    ("instrument_id", pa.string()),
    ("open_interest", pa.float64()),
    ("open_interest_usd", pa.float64()),
    ("ts_event", pa.int64()),
    ("ts_init", pa.int64()),
])

_LIQ_SCHEMA = pa.schema([
    ("instrument_id", pa.string()),
    ("side", pa.string()),
    ("quantity", pa.float64()),
    ("price", pa.float64()),
    ("usd_value", pa.float64()),
    ("ts_event", pa.int64()),
    ("ts_init", pa.int64()),
])

log = logging.getLogger(__name__)


class HyperliquidRecorder:
    """
    Records live Hyperliquid market data to a NautilusTrader ParquetDataCatalog.

    Parameters
    ----------
    coins : list[str]
        Coins to record (e.g. ["BTC", "ETH", "SOL"]).
    catalog_path : str | Path
        Destination ParquetDataCatalog directory.
    flush_interval : int
        Seconds between catalog writes (default 60).
    zmq_endpoint : str | None
        If set, subscribe to orchestrator ZMQ PUB (e.g. ``tcp://orchestrator:5555``)
        instead of opening a direct WebSocket to Hyperliquid.
    ws_url : str
        Hyperliquid WebSocket URL (used only when zmq_endpoint is None).
    info_url : str
        Hyperliquid REST /info URL (always used for instrument metadata).
    wallet_address : str | None
        Wallet address for liquidation recording (WS-mode only; in ZMQ mode
        liquidations arrive from the orchestrator automatically).
    """

    def __init__(
        self,
        coins: list[str],
        catalog_path: str | Path,
        flush_interval: int = 60,
        zmq_endpoint: Optional[str] = None,
        ws_url: str = HL_WS_URL,
        info_url: str = HL_BASE_URL + HL_INFO_ENDPOINT,
        wallet_address: Optional[str] = None,
    ) -> None:
        self._coins = coins
        self._catalog = ParquetDataCatalog(str(catalog_path))
        self._catalog_path = Path(catalog_path)
        self._flush_interval = flush_interval
        self._zmq_endpoint = zmq_endpoint
        self._ws_url = ws_url
        self._info_url = info_url
        self._wallet_address = wallet_address

        # coin → CryptoPerpetual
        self._instruments: dict[str, CryptoPerpetual] = {}

        # Standard NT data buffers
        self._ob_deltas: list[OrderBookDelta] = []
        self._trade_ticks: list[TradeTick] = []
        # Keyed by (bar_type_str, ts_event) so mid-candle WS updates overwrite
        # rather than accumulate — Hyperliquid emits one update per trade tick.
        self._bars: dict[tuple, Bar] = {}

        # Custom data buffers (list of row dicts, flushed to Parquet)
        self._funding_rows: list[dict] = []
        self._oi_rows: list[dict] = []
        self._liq_rows: list[dict] = []

        self._book_initialized: set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start recording. Runs until cancelled."""
        self._running = True
        await self._load_instruments()
        self._write_instruments()

        try:
            if self._zmq_endpoint:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._zmq_loop(), name="zmq_loop")
                    tg.create_task(self._flush_loop(), name="flush_loop")
            else:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._ws_loop(), name="ws_loop")
                    tg.create_task(self._flush_loop(), name="flush_loop")
                    tg.create_task(self._ping_loop(), name="ping_loop")
        except* (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._running = False
            self._flush()
            log.info("Recorder stopped — final flush complete.")

    # ------------------------------------------------------------------
    # Instrument loading
    # ------------------------------------------------------------------

    async def _load_instruments(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._info_url, json={"type": "metaAndAssetCtxs"}
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        universe = data[0].get("universe", [])
        asset_ctxs = data[1]

        for idx, meta in enumerate(universe):
            name = meta["name"]
            if name not in self._coins:
                continue
            ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
            instrument = _build_instrument(name, meta, ctx)
            if instrument is not None:
                self._instruments[name] = instrument
                log.info(f"Loaded instrument: {instrument.id}")

        missing = [c for c in self._coins if c not in self._instruments]
        if missing:
            log.warning(f"Could not load instruments for: {missing}")

    def _write_instruments(self) -> None:
        for instrument in self._instruments.values():
            self._catalog.write_data(data=[instrument])
        log.info(f"Wrote {len(self._instruments)} instruments to catalog.")

    # ------------------------------------------------------------------
    # WebSocket loop (with auto-reconnect)
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=None
                ) as ws:
                    self._ws = ws
                    log.info(f"Connected to {self._ws_url}")
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._handle_message(json.loads(raw))
                        except Exception as e:
                            log.error(f"Error handling WS message: {e}")
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning(f"WebSocket disconnected: {e}. Reconnecting in 5s...")
                self._book_initialized.clear()  # force snapshot on reconnect
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Unexpected WS error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)

    async def _subscribe(self, ws) -> None:
        for coin in self._instruments:
            for sub_type, extra in [
                (WS_TYPE_L2_BOOK, {}),
                (WS_TYPE_TRADES, {}),
                (WS_TYPE_CANDLE, {"interval": "1m"}),
                (WS_TYPE_ACTIVE_ASSET_CTX, {}),
            ]:
                msg = {
                    "method": "subscribe",
                    "subscription": {"type": sub_type, "coin": coin, **extra},
                }
                await ws.send(json.dumps(msg))

        if self._wallet_address:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": WS_TYPE_WEB_DATA2, "user": self._wallet_address},
            }))
            log.info(f"Subscribed to webData2 (liquidations) for wallet {self._wallet_address[:8]}…")

        log.info(f"Subscribed to {list(self._instruments.keys())}")

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HL_PING_INTERVAL_SECS)
            if self._ws and self._ws.state is WsState.OPEN:
                try:
                    await self._ws.send(json.dumps({"method": "ping"}))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ZMQ subscriber loop  (replaces WS when zmq_endpoint is set)
    # ------------------------------------------------------------------

    async def _zmq_loop(self) -> None:
        """Subscribe to orchestrator ZMQ PUB and dispatch messages to handlers."""
        import zmq
        import zmq.asyncio as azmq
        from hl_engine.transport.serialization import unwrap

        ctx = azmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, 10_000)

        for coin in self._instruments:
            iid = f"{coin}-USD.HYPERLIQUID"
            for prefix in [
                f"orderbook.{iid}",
                f"trades.{iid}",
                f"bar.{iid}.1m",
                f"funding.{iid}",
                f"liquidation.{iid}",
            ]:
                sock.setsockopt(zmq.SUBSCRIBE, prefix.encode())

        sock.connect(self._zmq_endpoint)
        log.info(f"ZMQ SUB connected to {self._zmq_endpoint}, coins={list(self._instruments)}")

        try:
            while self._running:
                try:
                    parts = await asyncio.wait_for(sock.recv_multipart(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if len(parts) < 2:
                    continue
                try:
                    _, _, type_str, d = unwrap(parts[1])
                except (ValueError, KeyError) as e:
                    log.error(f"Bad ZMQ frame: {e}")
                    continue
                try:
                    if type_str == "l2book":
                        self._handle_zmq_l2book(d)
                    elif type_str == "trade":
                        self._handle_trades({"data": [d]})
                    elif type_str == "candle":
                        self._handle_candle({"data": d})
                    elif type_str == "asset_ctx":
                        coin = d.get("coin", "")
                        ctx_inner = {k: v for k, v in d.items() if k != "coin"}
                        self._handle_asset_ctx({"data": {"coin": coin, "ctx": ctx_inner}})
                    elif type_str == "liquidation":
                        self._handle_web_data2({"data": {"liquidations": [d]}})
                except Exception as e:
                    log.error(f"Handler error ({type_str}): {e}")
        finally:
            sock.close()
            ctx.term()

    def _handle_zmq_l2book(self, d: dict) -> None:
        """
        Adapter for ZMQ l2book messages.

        The orchestrator pre-computes is_snapshot; we honour it by clearing
        _book_initialized so the existing handler treats the message correctly.
        """
        coin = d.get("coin", "")
        if d.get("is_snapshot") and coin in self._book_initialized:
            self._book_initialized.discard(coin)
        self._handle_l2_book({"data": d})

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
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

    # ------------------------------------------------------------------
    # L2 order book
    # ------------------------------------------------------------------

    def _handle_l2_book(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        instrument = self._instruments.get(coin)
        if instrument is None:
            return

        instrument_id = instrument.id
        ts_event = int(data.get("time", 0)) * 1_000_000  # ms → ns
        ts_init = time.time_ns()
        is_snapshot = coin not in self._book_initialized
        levels = data.get("levels", [[], []])
        deltas: list[OrderBookDelta] = []

        if is_snapshot:
            deltas.append(
                OrderBookDelta.clear(instrument_id, 0, ts_event, ts_init)
            )

        for side_idx, side_levels in enumerate(levels[:2]):
            order_side = OrderSide.BUY if side_idx == 0 else OrderSide.SELL
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
                deltas.append(
                    OrderBookDelta(
                        instrument_id,
                        action,
                        order,
                        0,
                        0,
                        ts_event,
                        ts_init,
                    )
                )

        if not deltas:
            return

        # Mark the last delta with F_LAST flag
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
        self._ob_deltas.extend(deltas)
        self._book_initialized.add(coin)

    # ------------------------------------------------------------------
    # Trade ticks
    # ------------------------------------------------------------------

    def _handle_trades(self, msg: dict) -> None:
        for trade in msg.get("data", []):
            coin = trade.get("coin", "")
            instrument = self._instruments.get(coin)
            if instrument is None:
                continue

            aggressor = (
                AggressorSide.BUYER if trade.get("side") == "B" else AggressorSide.SELLER
            )
            ts_event = int(trade.get("time", 0)) * 1_000_000
            trade_hash = trade.get("hash", "0x0000000000000000")

            tick = TradeTick(
                instrument_id=instrument.id,
                price=Price(float(trade["px"]), instrument.price_precision),
                size=Quantity(float(trade["sz"]), instrument.size_precision),
                aggressor_side=aggressor,
                trade_id=TradeId(trade_hash[:16]),
                ts_event=ts_event,
                ts_init=time.time_ns(),
            )
            self._trade_ticks.append(tick)

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    def _handle_candle(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("s", "")
        instrument = self._instruments.get(coin)
        if instrument is None:
            return

        bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
        ts_event = int(data.get("T", data.get("t", 0))) * 1_000_000  # close time ms → ns
        pp, sp = instrument.price_precision, instrument.size_precision

        bar = Bar(
            bar_type=bar_type,
            open=Price(float(data["o"]), pp),
            high=Price(float(data["h"]), pp),
            low=Price(float(data["l"]), pp),
            close=Price(float(data["c"]), pp),
            volume=Quantity(float(data["v"]), sp),
            ts_event=ts_event,
            ts_init=time.time_ns(),
        )
        self._bars[(str(bar_type), ts_event)] = bar

    # ------------------------------------------------------------------
    # Funding rate + open interest  (activeAssetCtx)
    # ------------------------------------------------------------------

    def _handle_asset_ctx(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        instrument = self._instruments.get(coin)
        if instrument is None:
            return

        ctx = data.get("ctx", {})
        instrument_id_str = str(instrument.id)
        ts = time.time_ns()
        oi = float(ctx.get("openInterest", 0.0))
        mark_px = float(ctx.get("markPx", 0.0))

        self._funding_rows.append({
            "instrument_id": instrument_id_str,
            "rate": float(ctx.get("funding", 0.0)),
            "next_funding_time": int(ctx.get("nextFundingTime", 0)) * 1_000_000,
            "open_interest": oi,
            "ts_event": ts,
            "ts_init": ts,
        })
        self._oi_rows.append({
            "instrument_id": instrument_id_str,
            "open_interest": oi,
            "open_interest_usd": oi * mark_px,
            "ts_event": ts,
            "ts_init": ts,
        })

    # ------------------------------------------------------------------
    # Liquidations  (webData2)
    # ------------------------------------------------------------------

    def _handle_web_data2(self, msg: dict) -> None:
        data = msg.get("data", {})
        for liq in data.get("liquidations", []):
            coin = liq.get("coin", "")
            instrument = self._instruments.get(coin)
            if instrument is None:
                continue

            side_raw = liq.get("side", "")
            side = "LONG" if side_raw in ("B", "buy", "LONG") else "SHORT"
            qty = float(liq.get("sz", 0.0))
            px = float(liq.get("px", 0.0))
            ts = time.time_ns()

            self._liq_rows.append({
                "instrument_id": str(instrument.id),
                "side": side,
                "quantity": qty,
                "price": px,
                "usd_value": qty * px,
                "ts_event": ts,
                "ts_init": ts,
            })

    # ------------------------------------------------------------------
    # Catalog flush
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            self._flush()

    def _flush(self) -> None:
        counts: dict[str, int] = {}

        if self._ob_deltas:
            self._catalog.write_data(data=self._ob_deltas)
            counts["OrderBookDelta"] = len(self._ob_deltas)
            self._ob_deltas.clear()

        if self._trade_ticks:
            self._catalog.write_data(data=self._trade_ticks)
            counts["TradeTick"] = len(self._trade_ticks)
            self._trade_ticks.clear()

        if self._bars:
            bars = list(self._bars.values())
            self._catalog.write_data(data=bars)
            counts["Bar"] = len(bars)
            self._bars.clear()

        if self._funding_rows:
            self._flush_custom_rows(self._funding_rows, "funding_rate", _FUNDING_SCHEMA)
            counts["FundingRateData"] = len(self._funding_rows)
            self._funding_rows.clear()

        if self._oi_rows:
            self._flush_custom_rows(self._oi_rows, "open_interest", _OI_SCHEMA)
            counts["OpenInterestData"] = len(self._oi_rows)
            self._oi_rows.clear()

        if self._liq_rows:
            self._flush_custom_rows(self._liq_rows, "liquidations", _LIQ_SCHEMA)
            counts["LiquidationData"] = len(self._liq_rows)
            self._liq_rows.clear()

        if counts:
            log.info(f"Flushed to catalog: {counts}")

    def _flush_custom_rows(self, rows: list[dict], type_name: str, schema: pa.Schema) -> None:
        """Write a batch of custom data rows to per-instrument Parquet files."""
        # Group by instrument_id
        by_instrument: dict[str, list[dict]] = {}
        for row in rows:
            iid = row["instrument_id"]
            by_instrument.setdefault(iid, []).append(row)

        for instrument_id, iid_rows in by_instrument.items():
            ts_start = iid_rows[0]["ts_event"]
            ts_end = iid_rows[-1]["ts_event"]
            out_dir = self._catalog_path / "custom" / type_name / instrument_id
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{ts_start}_{ts_end}.parquet"

            table = pa.table(
                {col: [r[col] for r in iid_rows] for col in schema.names},
                schema=schema,
            )
            pq.write_table(table, out_file)


# ------------------------------------------------------------------
# Shared instrument builder (reused by historical_loader too)
# ------------------------------------------------------------------

def _build_instrument(
    name: str, meta: dict, ctx: dict
) -> Optional[CryptoPerpetual]:
    """Convert Hyperliquid asset metadata into a NautilusTrader CryptoPerpetual."""
    try:
        sz_decimals = int(meta.get("szDecimals", 4))
        mark_px_str = ctx.get("markPx", "0")
        price_precision = _infer_price_precision(mark_px_str)
        size_precision = sz_decimals
        tick_size = Decimal(10) ** -price_precision
        step_size = Decimal(10) ** -size_precision

        from nautilus_trader.model.currencies import BTC, ETH, SOL
        _known = {"BTC": BTC, "ETH": ETH, "SOL": SOL}
        base = _known.get(name) or Currency(
            code=name[:8],
            precision=8,
            iso4217=0,
            name=name,
            currency_type=CurrencyType.CRYPTO,
        )

        return CryptoPerpetual(
            instrument_id=InstrumentId(
                symbol=Symbol(f"{name}-USD"), venue=HYPERLIQUID_VENUE
            ),
            raw_symbol=Symbol(name),
            base_currency=base,
            quote_currency=USDC,
            settlement_currency=USDC,
            is_inverse=False,
            price_precision=price_precision,
            size_precision=size_precision,
            price_increment=Price(tick_size, price_precision),
            size_increment=Quantity(step_size, size_precision),
            multiplier=Quantity(1, 0),
            lot_size=None,
            max_quantity=None,
            min_quantity=Quantity(step_size, size_precision),
            max_notional=None,
            min_notional=None,
            max_price=None,
            min_price=None,
            margin_init=Decimal("0.05"),
            margin_maint=Decimal("0.03"),
            maker_fee=Decimal("0.0002"),
            taker_fee=Decimal("0.0005"),
            ts_event=0,
            ts_init=0,
        )
    except Exception as e:
        log.warning(f"Failed to build instrument {name}: {e}")
        return None


def _infer_price_precision(px_str: str) -> int:
    try:
        px_str = str(float(px_str))
        if "." in px_str:
            _, decimal_part = px_str.split(".")
            decimal_part = decimal_part.rstrip("0")
            if decimal_part:
                return len(decimal_part)
        return 2
    except (ValueError, TypeError):
        return 2
