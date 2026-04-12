"""
Build a NautilusTrader catalog from Hyperliquid historical REST data.

Fetches OHLCV bars and funding rate history for the configured coins
and writes them to data/catalog. Use this to backtest before you have
enough live-recorded L2 data.

Note: The public Hyperliquid API does not provide historical trade ticks
or L2 order book data. Collect those live via record_live_data.py.

Configuration via .env or environment variables:
    HL_RECORD_COINS   Comma-separated coins  (default: BTC,ETH,SOL)
    HL_CATALOG_PATH   Catalog path           (default: data/catalog)
    HL_START_DATE     Start date YYYY-MM-DD  (default: 2024-01-01)
    HL_END_DATE       End date   YYYY-MM-DD  (default: today)
    HL_INTERVAL       Bar interval           (default: 1m)

Usage:
    python build_historical_catalog.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("build_historical_catalog")

COINS = [c.strip() for c in os.getenv("HL_RECORD_COINS", "BTC,ETH,SOL").split(",") if c.strip()]
CATALOG_PATH = Path(os.getenv("HL_CATALOG_PATH", "data/catalog"))
START_DATE = os.getenv("HL_START_DATE", "2024-01-01")
END_DATE = os.getenv("HL_END_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
INTERVAL = os.getenv("HL_INTERVAL", "1m")


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def main() -> None:
    from hl_engine.data.historical_loader import HistoricalDataLoader

    CATALOG_PATH.mkdir(parents=True, exist_ok=True)

    start_ms = _date_to_ms(START_DATE)
    end_ms = _date_to_ms(END_DATE)

    log.info(
        f"Building catalog | coins={COINS} | {START_DATE} → {END_DATE} | "
        f"interval={INTERVAL} | catalog={CATALOG_PATH}"
    )

    loader = HistoricalDataLoader(catalog_path=CATALOG_PATH)
    await loader.load(coins=COINS, start_ms=start_ms, end_ms=end_ms, interval=INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
