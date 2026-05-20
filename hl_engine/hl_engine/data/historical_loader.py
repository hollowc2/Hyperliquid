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
import pyarrow as pa
import pyarrow.parquet as pq

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from hl_engine.adapters.hyperliquid.constants import (
    HL_BASE_URL,
    HL_INFO_ENDPOINT,
    HYPERLIQUID_VENUE,
)
from hl_engine.data.live_recorder import _build_instrument, _infer_price_precision

log = logging.getLogger(__name__)

HL_INFO_URL = HL_BASE_URL + HL_INFO_ENDPOINT

# Hyperliquid candle API max: ~5000 bars per request
_MAX_CANDLES_PER_REQUEST = 5000
_ONE_MINUTE_MS = 60_000
_REQUEST_RETRIES = 6
_REQUEST_BACKOFF_BASE_SECS = 0.5
_REQUEST_BACKOFF_MAX_SECS = 8.0


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
            data = await _post_json_with_retry(
                session,
                self._info_url,
                {"type": "metaAndAssetCtxs"},
                request_label="instrument metadata",
            )

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
            self._catalog.write_data(data=[instrument])

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
                candles = await _post_json_with_retry(
                    session,
                    self._info_url,
                    payload,
                    request_label=f"{coin} candle snapshot",
                )

                if not candles:
                    chunk_start = chunk_end
                    continue

                bars = [_parse_candle(c, bar_type, instrument) for c in candles]
                self._catalog.write_data(data=bars)
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
        Fetch funding rate history and write to:
          - CSV sidecar at data/catalog/funding/<coin>.csv
          - Custom Parquet at data/catalog/custom/funding_rate/<instrument_id>/<ts_start>_<ts_end>.parquet
            (same format as the live recorder, so the backtest can seed FundingModel warmup)
        """
        payload = {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": start_ms,
            "endTime": end_ms,
        }

        async with aiohttp.ClientSession() as session:
            records = await _post_json_with_retry(
                session,
                self._info_url,
                payload,
                request_label=f"{coin} funding history",
            )

        if not records:
            log.info(f"  {coin}: no funding history returned.")
            return

        # --- CSV sidecar (legacy) ---
        out_dir = self._catalog_path / "funding"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{coin}.csv"
        file_exists = out_path.exists()
        with out_path.open("a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["ts_ms", "coin", "funding_rate"])
            for rec in records:
                writer.writerow([rec.get("time", 0), coin, rec.get("fundingRate", 0)])

        # --- Custom Parquet (for backtest FundingModel pre-seeding) ---
        instrument = self._instruments.get(coin)
        if instrument is not None:
            instrument_id_str = str(instrument.id)
            rows = []
            for rec in records:
                ts_ns = int(rec.get("time", 0)) * 1_000_000  # ms → ns
                rows.append({
                    "instrument_id": instrument_id_str,
                    "rate": float(rec.get("fundingRate", 0.0)),
                    "next_funding_time": 0,
                    "open_interest": 0.0,
                    "ts_event": ts_ns,
                    "ts_init": ts_ns,
                })
            schema = pa.schema([
                ("instrument_id", pa.string()),
                ("rate", pa.float64()),
                ("next_funding_time", pa.int64()),
                ("open_interest", pa.float64()),
                ("ts_event", pa.int64()),
                ("ts_init", pa.int64()),
            ])
            out_pq_dir = self._catalog_path / "custom" / "funding_rate" / instrument_id_str
            out_pq_dir.mkdir(parents=True, exist_ok=True)
            ts_start = rows[0]["ts_event"]
            ts_end = rows[-1]["ts_event"]
            out_pq = out_pq_dir / f"{ts_start}_{ts_end}.parquet"
            table = pa.table(
                {col: [r[col] for r in rows] for col in schema.names},
                schema=schema,
            )
            pq.write_table(table, out_pq)
            log.info(
                f"  {coin}: {len(records)} funding records written to {out_path} "
                f"and {out_pq.name}."
            )
        else:
            log.info(f"  {coin}: {len(records)} funding records written to {out_path}.")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
    _map = {
        "1m": "1-MINUTE",
        "3m": "3-MINUTE",
        "5m": "5-MINUTE",
        "15m": "15-MINUTE",
        "30m": "30-MINUTE",
        "1h": "1-HOUR",
        "4h": "4-HOUR",
        "1d": "1-DAY",
    }
    spec_str = _map.get(interval, "1-MINUTE")
    return BarType.from_str(f"{instrument_id}-{spec_str}-LAST-EXTERNAL")


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


async def _post_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    *,
    request_label: str,
) -> object:
    """
    POST JSON with bounded retry for transient rate limits / server errors.

    Hyperliquid will occasionally return 429 when the public API is busy.
    Retrying here keeps catalog builds from failing on the first throttled call.
    """

    for attempt in range(1, _REQUEST_RETRIES + 1):
        async with session.post(url, json=payload) as resp:
            if resp.status == 429 or 500 <= resp.status < 600:
                if attempt >= _REQUEST_RETRIES:
                    resp.raise_for_status()

                delay = _request_backoff_seconds(resp, attempt)
                log.warning(
                    "%s request failed with HTTP %s on attempt %s/%s; retrying in %.1fs",
                    request_label,
                    resp.status,
                    attempt,
                    _REQUEST_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()
            return await resp.json()

    raise RuntimeError(f"{request_label} request failed after {_REQUEST_RETRIES} attempts")


def _request_backoff_seconds(resp: aiohttp.ClientResponse, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After")
    header_delay: float | None = None
    if retry_after is not None:
        try:
            header_delay = float(retry_after)
        except ValueError:
            header_delay = None

    backoff = min(_REQUEST_BACKOFF_MAX_SECS, _REQUEST_BACKOFF_BASE_SECS * (2 ** (attempt - 1)))
    if header_delay is None:
        return backoff
    return max(backoff, header_delay)
