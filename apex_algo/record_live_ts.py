"""
Hyperliquid → TimescaleDB live recorder.

Streams L2 order book, trade ticks, and 1-minute candles from Hyperliquid
WebSocket and writes them to a TimescaleDB (PostgreSQL) hypertable.
No NautilusTrader dependency — pure asyncpg + websockets + aiohttp.

Configuration (env / .env)
--------------------------
    HL_RECORD_COINS      Comma-separated symbols          (default: BTC,ETH,SOL)
    HL_WS_URL            Hyperliquid WebSocket URL        (default: wss://api.hyperliquid.xyz/ws)
    HL_FLUSH_INTERVAL    Seconds between DB flushes       (default: 10)
    HL_L2_LEVELS         Top-N book levels to store       (default: 10)
    TS_DSN               asyncpg DSN for TimescaleDB      (required)
                         e.g. postgresql://hl:secret@localhost:5432/market_data

Usage
-----
    python record_live_ts.py
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time

import aiohttp
import websockets
from dotenv import load_dotenv

from hl_engine.data.timescale_sink import TimescaleSink

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("record_live_ts")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
COINS: list[str] = [
    c.strip()
    for c in os.getenv("HL_RECORD_COINS", "BTC,ETH,SOL").split(",")
    if c.strip()
]
WS_URL: str = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
INFO_URL: str = "https://api.hyperliquid.xyz/info"
FLUSH_INTERVAL: int = int(os.getenv("HL_FLUSH_INTERVAL", "10"))
L2_LEVELS: int = int(os.getenv("HL_L2_LEVELS", "10"))
TS_DSN: str = os.environ["TS_DSN"]  # fail fast if missing
PING_INTERVAL: int = 50


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class HyperliquidTSRecorder:
    """
    Connects to Hyperliquid WebSocket, parses messages, buffers data,
    and flushes to TimescaleDB on a configurable interval.
    """

    def __init__(self, coins: list[str], sink: TimescaleSink) -> None:
        self._coins = coins
        self._sink = sink

        # Raw buffers — tuples matching TimescaleSink.insert_* signatures
        self._trades: list[tuple] = []
        self._l2: list[tuple] = []
        self._bars: list[tuple] = []

        self._ws = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._ws_loop(),    name="ws_loop")
                tg.create_task(self._flush_loop(), name="flush_loop")
                tg.create_task(self._ping_loop(),  name="ping_loop")
        except* (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._running = False
            await self._flush()
            log.info("Recorder stopped — final flush complete.")

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self._ws = ws
                    log.info(f"Connected to {WS_URL}")
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._handle(json.loads(raw))
                        except Exception as exc:
                            log.error(f"Message handling error: {exc}")
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning(f"WS disconnected: {exc}. Reconnecting in 5 s...")
                await asyncio.sleep(5)
            except Exception as exc:
                log.error(f"Unexpected WS error: {exc}. Reconnecting in 10 s...")
                await asyncio.sleep(10)

    async def _subscribe(self, ws) -> None:
        for coin in self._coins:
            for sub in [
                {"type": "l2Book", "coin": coin},
                {"type": "trades", "coin": coin},
                {"type": "candle",  "coin": coin, "interval": "1m"},
            ]:
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
        log.info(f"Subscribed — coins={self._coins}")

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps({"method": "ping"}))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _handle(self, msg: dict) -> None:
        ch = msg.get("channel", "")
        if ch == "l2Book":
            self._on_l2(msg)
        elif ch == "trades":
            self._on_trades(msg)
        elif ch == "candle":
            self._on_candle(msg)

    # ------------------------------------------------------------------
    # L2 order book
    # ------------------------------------------------------------------

    def _on_l2(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        if coin not in self._coins:
            return
        ts_ms: int = int(data.get("time", time.time() * 1000))
        levels = data.get("levels", [[], []])

        for side_idx, side_levels in enumerate(levels[:2]):
            side_char = "B" if side_idx == 0 else "A"
            for lvl_idx, level in enumerate(side_levels[:L2_LEVELS]):
                self._l2.append((
                    ts_ms,
                    coin,
                    side_char,
                    lvl_idx,
                    float(level["px"]),
                    float(level["sz"]),
                ))

    # ------------------------------------------------------------------
    # Trade ticks
    # ------------------------------------------------------------------

    def _on_trades(self, msg: dict) -> None:
        for trade in msg.get("data", []):
            coin = trade.get("coin", "")
            if coin not in self._coins:
                continue
            self._trades.append((
                int(trade.get("time", time.time() * 1000)),
                coin,
                float(trade["px"]),
                float(trade["sz"]),
                trade.get("side", "S"),   # 'B' or 'S'
                trade.get("hash", "")[:36],
            ))

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    def _on_candle(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("s", "")
        if coin not in self._coins:
            return
        ts_ms = int(data.get("T", data.get("t", time.time() * 1000)))
        self._bars.append((
            ts_ms,
            coin,
            float(data["o"]),
            float(data["h"]),
            float(data["l"]),
            float(data["c"]),
            float(data["v"]),
        ))

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        trades, self._trades = self._trades, []
        l2,     self._l2     = self._l2,     []
        bars,   self._bars   = self._bars,   []

        counts: dict[str, int] = {}
        try:
            if trades:
                await self._sink.insert_trades(trades)
                counts["trades"] = len(trades)
            if l2:
                await self._sink.insert_l2(l2)
                counts["l2_rows"] = len(l2)
            if bars:
                await self._sink.insert_bars(bars)
                counts["bars"] = len(bars)
        except Exception as exc:
            log.error(f"Flush error: {exc}")
            # Re-queue on failure so data isn't lost
            self._trades = trades + self._trades
            self._l2     = l2     + self._l2
            self._bars   = bars   + self._bars
            return

        if counts:
            log.info(f"Flushed to TimescaleDB: {counts}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info(f"Starting | coins={COINS} | flush={FLUSH_INTERVAL}s | l2_levels={L2_LEVELS}")

    sink = TimescaleSink(dsn=TS_DSN)
    await sink.connect()
    await sink.init_schema()

    recorder = HyperliquidTSRecorder(coins=COINS, sink=sink)

    loop = asyncio.get_running_loop()
    task = loop.create_task(recorder.run())

    def _shutdown(sig):
        log.info(f"Received {sig.name}, shutting down...")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await sink.close()

    log.info("Exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
