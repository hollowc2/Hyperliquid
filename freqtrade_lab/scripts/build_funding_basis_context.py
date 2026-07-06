from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def ohlcv_json_to_frame(path: Path, prefix: str) -> pd.DataFrame:
    rows = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(
        rows,
        columns=[
            "timestamp_ms",
            f"{prefix}_open",
            f"{prefix}_high",
            f"{prefix}_low",
            f"{prefix}_close",
            f"{prefix}_volume",
        ],
    )
    frame["date"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(None)
    return frame.drop(columns=["timestamp_ms"]).sort_values("date")


def funding_csv_to_frame(path: Path, z_window_hours: int) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame.columns = [str(column).strip().lower() for column in frame.columns]

    date_column = next(
        (column for column in ("ts_ms", "timestamp_ms", "timestamp", "time", "date") if column in frame),
        None,
    )
    if date_column is None:
        raise ValueError(f"{path} does not contain a funding timestamp column")

    numeric_time = pd.to_numeric(frame[date_column], errors="coerce")
    if numeric_time.notna().any():
        unit = "ms" if numeric_time.dropna().gt(10_000_000_000).any() else "s"
        frame["date"] = pd.to_datetime(numeric_time, unit=unit, utc=True, errors="coerce")
    else:
        frame["date"] = pd.to_datetime(frame[date_column], utc=True, errors="coerce")
    frame["date"] = frame["date"].dt.tz_convert(None)

    rate_column = next(
        (column for column in ("funding_rate", "fundingrate", "rate", "funding") if column in frame),
        None,
    )
    if rate_column is None:
        raise ValueError(f"{path} does not contain a funding rate column")

    frame["ctx_funding_rate"] = pd.to_numeric(frame[rate_column], errors="coerce")
    frame = frame.dropna(subset=["date", "ctx_funding_rate"]).sort_values("date")
    if frame.empty:
        return frame[["date", "ctx_funding_rate"]]

    indexed = frame.set_index("date")
    frame["ctx_funding_8h_mean"] = (
        indexed["ctx_funding_rate"].rolling("8h", min_periods=2).mean().to_numpy()
    )
    frame["ctx_funding_24h_mean"] = (
        indexed["ctx_funding_rate"].rolling("24h", min_periods=4).mean().to_numpy()
    )
    rolling = indexed["ctx_funding_rate"].rolling(f"{z_window_hours}h", min_periods=24)
    mean = rolling.mean().to_numpy()
    std = rolling.std(ddof=0).replace(0, np.nan).to_numpy()
    frame["ctx_funding_z"] = (frame["ctx_funding_rate"].to_numpy() - mean) / std
    return frame[
        [
            "date",
            "ctx_funding_rate",
            "ctx_funding_8h_mean",
            "ctx_funding_24h_mean",
            "ctx_funding_z",
        ]
    ]


def build_context(
    hyperliquid_data: Path,
    funding_csv: Path,
    spot_data: Path,
    spot_tolerance: str,
    funding_tolerance: str,
    basis_window: int,
    funding_z_window_hours: int,
) -> pd.DataFrame:
    futures = ohlcv_json_to_frame(hyperliquid_data, "hl")
    spot = ohlcv_json_to_frame(spot_data, "spot")
    funding = funding_csv_to_frame(funding_csv, funding_z_window_hours)

    merged = pd.merge_asof(
        futures.sort_values("date"),
        spot[["date", "spot_close"]].sort_values("date"),
        on="date",
        direction="nearest",
        tolerance=pd.Timedelta(spot_tolerance),
    )
    merged = pd.merge_asof(
        merged.sort_values("date"),
        funding.sort_values("date"),
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(funding_tolerance),
    )

    merged["ctx_basis_pct"] = (merged["hl_close"] - merged["spot_close"]) / merged["spot_close"]
    basis_roll = merged["ctx_basis_pct"].rolling(
        basis_window,
        min_periods=max(24, basis_window // 4),
    )
    basis_std = basis_roll.std(ddof=0).replace(0, np.nan)
    merged["ctx_basis_z"] = (merged["ctx_basis_pct"] - basis_roll.mean()) / basis_std

    keep = [
        "date",
        "ctx_basis_pct",
        "ctx_basis_z",
        "ctx_funding_rate",
        "ctx_funding_8h_mean",
        "ctx_funding_24h_mean",
        "ctx_funding_z",
    ]
    context = merged[keep].copy()
    for column in keep[1:]:
        context[column] = pd.to_numeric(context[column], errors="coerce")
    context = context.dropna(how="all", subset=keep[1:])
    context["ctx_loaded"] = 1.0
    return context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build pair-specific Freqtrade context from HL futures, HL funding, and Coinbase spot."
    )
    parser.add_argument("--coin", default="ETH")
    parser.add_argument("--pair", default="ETH/USDC:USDC")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument(
        "--hyperliquid-data",
        type=Path,
        help="Freqtrade OHLCV JSON for Hyperliquid futures.",
    )
    parser.add_argument(
        "--funding-csv",
        default="../hl_engine/data/catalog/funding/ETH.csv",
        type=Path,
        help="Hyperliquid funding CSV, usually hl_engine/data/catalog/funding/<COIN>.csv.",
    )
    parser.add_argument(
        "--spot-data",
        type=Path,
        help="Coinbase spot OHLCV JSON for the same base coin and timeframe.",
    )
    parser.add_argument("--output-dir", default="user_data/data/context", type=Path)
    parser.add_argument("--spot-tolerance", default="10min")
    parser.add_argument("--funding-tolerance", default="2h")
    parser.add_argument("--basis-window", default=288, type=int)
    parser.add_argument("--funding-z-window-hours", default=168, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    coin = args.coin.upper()
    pair = args.pair
    hyperliquid_data = args.hyperliquid_data or Path(
        f"user_data/data/hyperliquid/futures/{pair_to_filename(pair)}-{args.timeframe}-futures.json"
    )
    spot_data = args.spot_data or Path(f"user_data/data/coinbase/{coin}_USD-{args.timeframe}.json")

    context = build_context(
        hyperliquid_data=hyperliquid_data,
        funding_csv=args.funding_csv,
        spot_data=spot_data,
        spot_tolerance=args.spot_tolerance,
        funding_tolerance=args.funding_tolerance,
        basis_window=args.basis_window,
        funding_z_window_hours=args.funding_z_window_hours,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_file = args.output_dir / f"{pair_to_filename(pair)}_{args.timeframe}.csv"
    context.to_csv(out_file, index=False)
    print(f"Wrote {len(context)} context rows to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
