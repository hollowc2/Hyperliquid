"""
Historical data loader for Hyperliquid REST API.

Fetches what the public API offers historically:
  - OHLCV bars (candleSnapshot)      → Bar
  - Funding rate history (fundingHistory) → written as CSV sidecar
    (NautilusTrader has no built-in Parquet type for custom Data subclasses,
     so funding is stored as a separate CSV and loaded at backtest time)

Trade ticks and L2 order book data are NOT available via the public
Hyperliquid REST API. Collect them via the live recorder instead.

Usage:
    loader = HistoricalDataLoader(catalog_path="data/catalog")
    asyncio.run(loader.load(coins=["BTC", "ETH"], start_ms=..., end_ms=...))
"""

import asyncio
import csv
import logging
import time
from pathlib import Path
from typing import Optional

import aiohttp

from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from apex_trader.adapters.hyperliquid.constants import (
    HL_BASE_URL,
    HL_INFO_ENDPOINT,
    HYPERLIQUID_VENUE,
)
from apex_trader.data.live_recorder import _build_instrument, _infer_price_precision

log = logging.getLogger(__name__)

HL_INFO_URL = HL_BASE_URL + HL_INFO_ENDPOINT

# Hyperliquid candle API max: ~5000 bars per request
_MAX_CANDLES_PER_REQUEST = 5000
_ONE_MINUTE_MS = 60_000


class HistoricalDataLoader:
    """
    Loads historical bars (and funding rates) from Hyperliquid REST
    into a NautilusTrader ParquetDataCatalog.

    Parameters
    ----------
    catalog_path : str | Path
        Destination catalog directory.
    info_url : str
        Hyperliquid /info REST endpoint.
    """

    def __init__(
        self,
        catalog_path: str | Path,
        info_url: str = HL_INFO_URL,
    ) -> None:
        self._catalog = ParquetDataCatalog(str(catalog_path))
        self._catalog_path = Path(catalog_path)
        self._info_url = info_url
        self._instruments: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load(
        self,
        coins: list[str],
        start_ms: int,
        end_ms: Optional[int] = None,
        interval: str = "1m",
    ) -> None:
        """
        Fetch historical bars and funding for the given coins and write
        them to the catalog.

        Parameters
        ----------
        coins : list[str]
        start_ms : int
            Start time in milliseconds (Unix epoch).
        end_ms : int | None
            End time in milliseconds. Defaults to now.
        interval : str
            Candle interval string, e.g. "1m", "5m", "1h".
        """
        if end_ms is None:
            end_ms = int(time.time() * 1000)

        await self._load_instruments(coins)
        self._write_instruments()

        for coin in coins:
            if coin not in self._instruments:
                log.warning(f"Skipping {coin} — instrument not loaded.")
                continue
            log.info(f"Fetching {coin} bars ({interval}) from {start_ms} to {end_ms}...")
            await self._fetch_bars(coin, start_ms, end_ms, interval)
            log.info(f"Fetching {coin} funding history...")
            await self._fetch_funding(coin, start_ms, end_ms)

        log.info("Historical load complete.")

    # ------------------------------------------------------------------
    # Instrument loading
    # ------------------------------------------------------------------

    async def _load_instruments(self, coins: list[str]) -> None:
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
            if name not in coins:
                continue
            ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
            instrument = _build_instrument(name, meta, ctx)
            if instrument is not None:
                self._instruments[name] = instrument
                log.info(f"Loaded instrument: {instrument.id}")

    def _write_instruments(self) -> None:
        for instrument in self._instruments.values():
            self._catalog.write_chunk(data=[instrument], data_cls=type(instrument))

    # ------------------------------------------------------------------
    # Bar fetching (paginated)
    # ------------------------------------------------------------------

    async def _fetch_bars(
        self, coin: str, start_ms: int, end_ms: int, interval: str
    ) -> None:
        instrument = self._instruments[coin]
        bar_type = _make_bar_type(instrument.id, interval)
        interval_ms = _interval_to_ms(interval)
        chunk_ms = _MAX_CANDLES_PER_REQUEST * interval_ms

        chunk_start = start_ms
        total_bars = 0

        async with aiohttp.ClientSession() as session:
            while chunk_start < end_ms:
                chunk_end = min(chunk_start + chunk_ms, end_ms)
                payload = {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": interval,
                        "startTime": chunk_start,
                        "endTime": chunk_end,
                    },
                }
                async with session.post(self._info_url, json=payload) as resp:
                    resp.raise_for_status()
                    candles = await resp.json()

                if not candles:
                    chunk_start = chunk_end
                    continue

                bars = [_parse_candle(c, bar_type, instrument) for c in candles]
                self._catalog.write_chunk(data=bars, data_cls=Bar)
                total_bars += len(bars)
                log.debug(f"  {coin}: wrote {len(bars)} bars (chunk {chunk_start}–{chunk_end})")

                # Advance past the last candle we received
                last_ts = int(candles[-1].get("T", candles[-1].get("t", chunk_end)))
                chunk_start = last_ts + interval_ms

                # Rate-limit: HL allows ~10 req/s, be conservative
                await asyncio.sleep(0.2)

        log.info(f"  {coin}: {total_bars} total bars written to catalog.")

    # ------------------------------------------------------------------
    # Funding history (CSV sidecar)
    # ------------------------------------------------------------------

    async def _fetch_funding(self, coin: str, start_ms: int, end_ms: int) -> None:
        """
        Fetch funding rate history and write to a CSV sidecar at
        data/catalog/funding/<coin>.csv

        Columns: ts_ms, coin, funding_rate
        """
        payload = {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": start_ms,
            "endTime": end_ms,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self._info_url, json=payload) as resp:
                resp.raise_for_status()
                records = await resp.json()

        if not records:
            log.info(f"  {coin}: no funding history returned.")
            return

        out_dir = self._catalog_path / "funding"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{coin}.csv"

        # Append if file exists, write header only when creating
        file_exists = out_path.exists()
        with out_path.open("a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["ts_ms", "coin", "funding_rate"])
            for rec in records:
                writer.writerow([rec.get("time", 0), coin, rec.get("fundingRate", 0)])

        log.info(f"  {coin}: {len(records)} funding records written to {out_path}.")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
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
    return BarType(
        instrument_id=instrument_id,
        spec=BarSpecification(step=step, aggregation=agg, price_type=PriceType.LAST),
        aggregation_source=0,
    )


def _interval_to_ms(interval: str) -> int:
    _map = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    return _map.get(interval, 60_000)


def _parse_candle(data: dict, bar_type: BarType, instrument) -> Bar:
    ts_event = int(data.get("T", data.get("t", 0))) * 1_000_000  # ms → ns
    pp, sp = instrument.price_precision, instrument.size_precision
    return Bar(
        bar_type=bar_type,
        open=Price(float(data["o"]), pp),
        high=Price(float(data["h"]), pp),
        low=Price(float(data["l"]), pp),
        close=Price(float(data["c"]), pp),
        volume=Quantity(float(data["v"]), sp),
        ts_event=ts_event,
        ts_init=ts_event,
    )
