"""
Hyperliquid live data recorder — VPS entry point.

Streams L2 order book, trade ticks, and 1m candles from Hyperliquid
and writes them to a NautilusTrader ParquetDataCatalog for backtesting.

Configuration via .env or environment variables:
    HL_RECORD_COINS      Comma-separated list of coins (default: BTC,ETH,SOL)
    HL_CATALOG_PATH      Catalog directory path   (default: data/catalog)
    HL_FLUSH_INTERVAL    Seconds between flushes  (default: 60)
    HL_WS_URL            WebSocket URL            (default: wss://api.hyperliquid.xyz/ws)

Usage:
    python record_live_data.py
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("record_live_data")

# --- Config ---
COINS = [c.strip() for c in os.getenv("HL_RECORD_COINS", "BTC,ETH,SOL").split(",") if c.strip()]
CATALOG_PATH = Path(os.getenv("HL_CATALOG_PATH", "data/catalog"))
FLUSH_INTERVAL = int(os.getenv("HL_FLUSH_INTERVAL", "60"))
WS_URL = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")


async def main() -> None:
    from hl_engine.data.live_recorder import HyperliquidRecorder

    CATALOG_PATH.mkdir(parents=True, exist_ok=True)

    log.info(f"Starting recorder | coins={COINS} | catalog={CATALOG_PATH} | flush={FLUSH_INTERVAL}s")

    recorder = HyperliquidRecorder(
        coins=COINS,
        catalog_path=CATALOG_PATH,
        flush_interval=FLUSH_INTERVAL,
        ws_url=WS_URL,
    )

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

    log.info("Recorder exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
