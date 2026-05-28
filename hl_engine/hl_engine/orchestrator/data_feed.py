"""
OrchestratorDataFeed — Hyperliquid WebSocket → ZMQ PUB.

Directly adapted from live_recorder.py:HyperliquidRecorder.
Replaces catalog flush with immediate ZMQ publish.
Maintains in-memory L2 book snapshots (refreshed from HL REST every 30s)
for subscriber resync via GET /snapshot/{instrument_id}.

Publishes on ZMQ PUB socket with topics:
  b"orderbook.{instrument_id}"   OrderBookDelta batch
  b"trades.{instrument_id}"      TradeTick
  b"bar.{instrument_id}.{interval}" Bar candle
  b"funding.{instrument_id}"     FundingRateData + OI
  b"liquidation.{instrument_id}" LiquidationData
  b"heartbeat"                   Liveness pulse every 500ms
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp
import websockets
from websockets.connection import State as WsState

import zmq.asyncio

from hl_engine.adapters.hyperliquid.constants import (
    HL_BASE_URL,
    HL_INFO_ENDPOINT,
    HL_PING_INTERVAL_SECS,
    HL_WS_URL,
    WS_TYPE_ACTIVE_ASSET_CTX,
    WS_TYPE_CANDLE,
    WS_TYPE_L2_BOOK,
    WS_TYPE_TRADES,
    WS_TYPE_WEB_DATA2,
)
from hl_engine.transport.serialization import (
    wrap_asset_ctx,
    wrap_candle,
    wrap_heartbeat,
    wrap_l2book,
    wrap_liquidation,
    wrap_trade,
)

log = logging.getLogger(__name__)

_SNAPSHOT_REFRESH_INTERVAL = 30.0  # seconds


class OrchestratorDataFeed:
    """
    Streams Hyperliquid market data and publishes it over ZMQ PUB.

    Parameters
    ----------
    coins : list[str]
        Coins to subscribe to (e.g. ["BTC", "ETH"]).
    zmq_pub : zmq.asyncio.Socket
        Bound ZMQ PUB socket (tcp://*:5555).
    ws_url : str
        Hyperliquid WebSocket URL.
    info_url : str
        Hyperliquid REST /info URL.
    wallet_address : Optional[str]
        Wallet address for webData2 (liquidation) subscription.
    """

    def __init__(
        self,
        coins: list[str],
        zmq_pub: "zmq.asyncio.Socket",
        ws_url: str = HL_WS_URL,
        info_url: str = HL_BASE_URL + HL_INFO_ENDPOINT,
        wallet_address: Optional[str] = None,
        candle_intervals: Optional[list[str]] = None,
    ) -> None:
        self._coins = list(coins)
        self._zmq_pub = zmq_pub
        self._ws_url = ws_url
        self._info_url = info_url
        self._wallet_address = wallet_address
        self._candle_intervals = candle_intervals or _default_candle_intervals()

        # Instrument metadata: coin → {price_precision, size_precision, szDecimals}
        self._instruments: dict[str, dict] = {}

        # In-memory L2 book snapshots for resync: coin → {bids: {px_str: sz}, asks: {px_str: sz}}
        self._books: dict[str, dict[str, dict[str, float]]] = {}

        # Track whether each coin has received its first snapshot (set is_snapshot=True on first)
        self._book_initialized: set[str] = set()

        # Per-topic sequence counters (monotonic)
        self._seq: dict[str, int] = {}

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start publishing. Runs until cancelled."""
        self._running = True
        await self._load_instruments()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._ws_loop(), name="datafeed_ws")
                tg.create_task(self._ping_loop(), name="datafeed_ping")
                tg.create_task(self._heartbeat_loop(), name="datafeed_heartbeat")
                tg.create_task(self._snapshot_refresh_loop(), name="datafeed_snapshot_refresh")
        except* (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._running = False

    def get_snapshot(self, coin: str) -> Optional[dict]:
        """Return current L2 book snapshot for a coin (for /snapshot REST endpoint)."""
        book = self._books.get(coin)
        if book is None:
            return None
        bids = sorted(
            ((float(px), sz) for px, sz in book["bids"].items() if sz > 0),
            key=lambda x: -x[0],
        )
        asks = sorted(
            ((float(px), sz) for px, sz in book["asks"].items() if sz > 0),
            key=lambda x: x[0],
        )
        return {
            "coin": coin,
            "instrument_id": f"{coin}-USD.HYPERLIQUID",
            "bids": [[px, sz] for px, sz in bids[:20]],
            "asks": [[px, sz] for px, sz in asks[:20]],
            "ts_ns": time.time_ns(),
        }

    def get_subscribed_coins(self) -> list[str]:
        return list(self._coins)

    # ------------------------------------------------------------------
    # Instrument loading
    # ------------------------------------------------------------------

    async def _load_instruments(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(self._info_url, json={"type": "metaAndAssetCtxs"}) as resp:
                resp.raise_for_status()
                data = await resp.json()

        universe = data[0].get("universe", [])
        for meta in universe:
            name = meta["name"]
            if name not in self._coins:
                continue
            sz_decimals = int(meta.get("szDecimals", 4))
            self._instruments[name] = {"szDecimals": sz_decimals, "meta": meta}

        missing = [c for c in self._coins if c not in self._instruments]
        if missing:
            log.warning(f"Could not find metadata for: {missing}")
        log.info(f"Loaded instrument metadata for: {list(self._instruments.keys())}")

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self._ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    log.info(f"Data feed connected to {self._ws_url}")
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._handle_message(json.loads(raw))
                        except Exception as e:
                            log.error(f"Data feed WS error: {e}")
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning(f"Data feed WS disconnected: {e}. Reconnecting in 5s…")
                self._book_initialized.clear()  # force snapshots on reconnect
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Data feed unexpected error: {e}. Reconnecting in 10s…")
                await asyncio.sleep(10)

    async def _subscribe(self, ws) -> None:
        for coin in self._instruments:
            for sub_type, extra in [
                (WS_TYPE_L2_BOOK, {}),
                (WS_TYPE_TRADES, {}),
                (WS_TYPE_ACTIVE_ASSET_CTX, {}),
            ]:
                msg = {"method": "subscribe", "subscription": {"type": sub_type, "coin": coin, **extra}}
                await ws.send(json.dumps(msg))
            for interval in self._candle_intervals:
                msg = {
                    "method": "subscribe",
                    "subscription": {"type": WS_TYPE_CANDLE, "coin": coin, "interval": interval},
                }
                await ws.send(json.dumps(msg))

        if self._wallet_address:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": WS_TYPE_WEB_DATA2, "user": self._wallet_address},
            }))
        log.info(
            f"Data feed subscribed to {list(self._instruments.keys())} "
            f"candles={self._candle_intervals}"
        )

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HL_PING_INTERVAL_SECS)
            if self._ws and self._ws.state is WsState.OPEN:
                try:
                    await self._ws.send(json.dumps({"method": "ping"}))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            seq = self._next_seq("heartbeat")
            topic, payload = wrap_heartbeat(seq)
            await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Snapshot refresh loop (every 30s — rebuild books from HL REST)
    # ------------------------------------------------------------------

    async def _snapshot_refresh_loop(self) -> None:
        await asyncio.sleep(_SNAPSHOT_REFRESH_INTERVAL)  # initial delay
        while self._running:
            for coin in list(self._instruments.keys()):
                try:
                    await self._refresh_book_from_rest(coin)
                except Exception as e:
                    log.warning(f"Book refresh failed for {coin}: {e}")
            await asyncio.sleep(_SNAPSHOT_REFRESH_INTERVAL)

    async def _refresh_book_from_rest(self, coin: str) -> None:
        """Pull full L2 book from HL REST and rebuild the snapshot + publish as is_snapshot=True."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._info_url, json={"type": "l2Book", "coin": coin}
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        levels = data.get("levels", [[], []])
        time_ms = int(time.time() * 1000)

        # Rebuild book snapshot
        new_book: dict[str, dict[str, float]] = {"bids": {}, "asks": {}}
        for px_entry in levels[0]:  # bids
            new_book["bids"][px_entry["px"]] = float(px_entry["sz"])
        for px_entry in levels[1]:  # asks
            new_book["asks"][px_entry["px"]] = float(px_entry["sz"])
        self._books[coin] = new_book

        # Publish as snapshot so subscribers can resync
        # Force is_snapshot=True by removing from initialized set temporarily
        was_initialized = coin in self._book_initialized
        self._book_initialized.discard(coin)

        seq = self._next_seq(f"orderbook.{coin}")
        topic, payload = wrap_l2book(seq, coin, time_ms, levels, is_snapshot=True)
        await self._zmq_pub.send_multipart([topic, payload])

        if was_initialized:
            self._book_initialized.add(coin)
        log.debug(f"Book refreshed from REST for {coin}")

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        if channel == WS_TYPE_L2_BOOK:
            asyncio.get_event_loop().create_task(self._publish_l2book(msg))
        elif channel == WS_TYPE_TRADES:
            asyncio.get_event_loop().create_task(self._publish_trades(msg))
        elif channel == WS_TYPE_CANDLE:
            asyncio.get_event_loop().create_task(self._publish_candle(msg))
        elif channel == WS_TYPE_ACTIVE_ASSET_CTX:
            asyncio.get_event_loop().create_task(self._publish_asset_ctx(msg))
        elif channel == WS_TYPE_WEB_DATA2:
            asyncio.get_event_loop().create_task(self._publish_liquidations(msg))

    # ------------------------------------------------------------------
    # L2 order book
    # ------------------------------------------------------------------

    async def _publish_l2book(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        if coin not in self._instruments:
            return

        levels = data.get("levels", [[], []])
        time_ms = int(data.get("time", 0))
        is_snapshot = coin not in self._book_initialized

        # Update in-memory book
        if is_snapshot:
            self._books[coin] = {"bids": {}, "asks": {}}

        book = self._books.setdefault(coin, {"bids": {}, "asks": {}})
        for side_idx, side_levels in enumerate(levels[:2]):
            side_key = "bids" if side_idx == 0 else "asks"
            for level in side_levels:
                px_str = level["px"]
                sz = float(level["sz"])
                if sz == 0.0:
                    book[side_key].pop(px_str, None)
                else:
                    book[side_key][px_str] = sz

        self._book_initialized.add(coin)

        seq = self._next_seq(f"orderbook.{coin}")
        topic, payload = wrap_l2book(seq, coin, time_ms, levels, is_snapshot)
        await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    async def _publish_trades(self, msg: dict) -> None:
        for trade in msg.get("data", []):
            coin = trade.get("coin", "")
            if coin not in self._instruments:
                continue
            seq = self._next_seq(f"trades.{coin}")
            topic, payload = wrap_trade(seq, coin, trade)
            await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Candles
    # ------------------------------------------------------------------

    async def _publish_candle(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("s", "")
        if coin not in self._instruments:
            return
        interval = str(data.get("i", "1m"))
        seq = self._next_seq(f"bar.{coin}.{interval}")
        topic, payload = wrap_candle(seq, coin, data)
        await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Funding / OI  (activeAssetCtx)
    # ------------------------------------------------------------------

    async def _publish_asset_ctx(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        if coin not in self._instruments:
            return
        ctx = data.get("ctx", {})
        seq = self._next_seq(f"funding.{coin}")
        topic, payload = wrap_asset_ctx(seq, coin, ctx)
        await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Liquidations  (webData2)
    # ------------------------------------------------------------------

    async def _publish_liquidations(self, msg: dict) -> None:
        data = msg.get("data", {})
        ts_ns = time.time_ns()
        for liq in data.get("liquidations", []):
            coin = liq.get("coin", "")
            if coin not in self._instruments:
                continue
            seq = self._next_seq(f"liquidation.{coin}")
            topic, payload = wrap_liquidation(seq, coin, liq, ts_ns)
            await self._zmq_pub.send_multipart([topic, payload])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_seq(self, key: str) -> int:
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]


def _default_candle_intervals() -> list[str]:
    raw = os.getenv("HL_CANDLE_INTERVALS", "1m,15m")
    intervals: list[str] = []
    for item in raw.split(","):
        interval = item.strip()
        if interval and interval not in intervals:
            intervals.append(interval)
    return intervals or ["1m"]
