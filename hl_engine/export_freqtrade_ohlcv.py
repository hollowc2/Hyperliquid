"""
Export Hyperliquid bars from the Nautilus catalog to Freqtrade OHLCV JSON.

The downloader pipeline in this package writes a NautilusTrader Parquet catalog.
Freqtrade expects its own local OHLCV files, so this script bridges the formats
without re-downloading candles.

Usage:
    HL_CATALOG_PATH=data/catalog python export_freqtrade_ohlcv.py \
      --coin ETH \
      --timeframe 5m \
      --output ../freqtrade_lab/user_data/data/hyperliquid
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TIMEFRAME_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def coin_to_instrument_id(coin: str) -> str:
    return f"{coin.upper()}-USD.HYPERLIQUID"


def coin_to_freqtrade_pair(coin: str) -> str:
    return f"{coin.upper()}/USDC:USDC"


def pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def numeric(value) -> float:
    if hasattr(value, "as_double"):
        return float(value.as_double())
    return float(str(value))


def bar_type_label(timeframe: str) -> str:
    minutes = TIMEFRAME_MINUTES[timeframe]
    if minutes < 60:
        return f"{minutes}-MINUTE"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-DAY"
    return f"{minutes // 60}-HOUR"


def freqtrade_open_timestamp_ms(ts_event_ns: int, timeframe: str) -> int:
    # HistoricalDataLoader stores Hyperliquid candle close time (field "T")
    # as the Nautilus ts_event. Freqtrade local JSON expects candle open time.
    return int(ts_event_ns // 1_000_000) - TIMEFRAME_MINUTES[timeframe] * 60_000 + 1


def freqtrade_ohlcv_path(output_dir: Path, pair: str, timeframe: str, candle_type: str) -> Path:
    # Freqtrade stores futures/mark/index/funding candles under datadir/futures.
    # Spot candles live directly under datadir.
    base_dir = output_dir / "futures" if candle_type != "spot" else output_dir
    return base_dir / f"{pair_to_filename(pair)}-{timeframe}-{candle_type}.json"


def resample_ohlcv_rows(rows: list[list[float]], source_timeframe: str, target_timeframe: str) -> list[list[float]]:
    source_minutes = TIMEFRAME_MINUTES[source_timeframe]
    target_minutes = TIMEFRAME_MINUTES[target_timeframe]
    if target_minutes <= source_minutes:
        raise ValueError("target_timeframe must be larger than source_timeframe")
    if target_minutes % source_minutes != 0:
        raise ValueError("target_timeframe must be an even multiple of source_timeframe")

    target_ms = target_minutes * 60_000
    buckets: dict[int, list[list[float]]] = {}
    for row in rows:
        bucket_ts = int(row[0]) - (int(row[0]) % target_ms)
        buckets.setdefault(bucket_ts, []).append(row)

    resampled = []
    for timestamp_ms in sorted(buckets):
        bucket = sorted(buckets[timestamp_ms], key=lambda item: item[0])
        resampled.append(
            [
                timestamp_ms,
                bucket[0][1],
                max(row[2] for row in bucket),
                min(row[3] for row in bucket),
                bucket[-1][4],
                sum(row[5] for row in bucket),
            ]
        )
    return resampled


def catalog_rows(catalog, instrument: str, timeframe: str) -> list[list[float]]:
    bar_type = f"{instrument}-{bar_type_label(timeframe)}-LAST-EXTERNAL"
    bars = catalog.bars([bar_type])
    if not bars:
        return []

    dedup = {bar.ts_event: bar for bar in bars}
    rows = []
    for bar in sorted(dedup.values(), key=lambda item: item.ts_event):
        rows.append(
            [
                freqtrade_open_timestamp_ms(bar.ts_event, timeframe),
                numeric(bar.open),
                numeric(bar.high),
                numeric(bar.low),
                numeric(bar.close),
                numeric(bar.volume),
            ]
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="data/catalog", type=Path)
    parser.add_argument("--coin", default="ETH", help="Hyperliquid coin symbol, e.g. ETH")
    parser.add_argument("--instrument", help="Override Nautilus instrument id")
    parser.add_argument("--pair", help="Override Freqtrade pair name")
    parser.add_argument("--timeframe", default="5m", choices=sorted(TIMEFRAME_MINUTES))
    parser.add_argument(
        "--source-timeframe",
        choices=sorted(TIMEFRAME_MINUTES),
        help="Read this catalog timeframe and resample to --timeframe.",
    )
    parser.add_argument(
        "--candle-type",
        default="futures",
        choices=["futures", "mark", "index", "premiumIndex", "funding_rate", "spot"],
        help="Freqtrade candle type. Hyperliquid perpetual OHLCV uses futures.",
    )
    parser.add_argument(
        "--output",
        default="../freqtrade_lab/user_data/data/hyperliquid",
        type=Path,
    )
    args = parser.parse_args()

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    instrument = args.instrument or coin_to_instrument_id(args.coin)
    pair = args.pair or coin_to_freqtrade_pair(args.coin)

    catalog = ParquetDataCatalog(str(args.catalog))
    if args.source_timeframe:
        source_rows = catalog_rows(catalog, instrument, args.source_timeframe)
        if not source_rows:
            raise RuntimeError(
                f"No bars found for {instrument}-{bar_type_label(args.source_timeframe)}-LAST-EXTERNAL "
                f"in {args.catalog}"
            )
        rows = resample_ohlcv_rows(source_rows, args.source_timeframe, args.timeframe)
    else:
        rows = catalog_rows(catalog, instrument, args.timeframe)
        if not rows:
            raise RuntimeError(
                f"No bars found for {instrument}-{bar_type_label(args.timeframe)}-LAST-EXTERNAL "
                f"in {args.catalog}"
            )

    if not rows:
        raise RuntimeError(
            f"No rows available for {instrument} {args.timeframe}"
        )

    out_file = freqtrade_ohlcv_path(args.output, pair, args.timeframe, args.candle_type)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, separators=(",", ":"))
    out_file.write_text(payload, encoding="utf-8")
    print(f"Wrote {len(rows)} candles to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
