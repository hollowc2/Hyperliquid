from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib import request

import ccxt
import numpy as np
import pandas as pd


LOG = logging.getLogger("funding_basis_context")


def pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def ohlcv_to_frame(rows: list[list[float]], prefix: str) -> pd.DataFrame:
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
    return frame.drop(columns=["timestamp_ms"]).dropna(subset=["date"]).sort_values("date")


def fetch_hyperliquid_funding(coin: str, start_ms: int) -> pd.DataFrame:
    payload = json.dumps(
        {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": start_ms,
        }
    ).encode("utf-8")
    req = request.Request(
        "https://api.hyperliquid.xyz/info",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        records = json.loads(response.read().decode("utf-8"))

    rows = []
    for rec in records or []:
        try:
            rows.append(
                {
                    "date": pd.to_datetime(int(rec["time"]), unit="ms", utc=True).tz_convert(None),
                    "ctx_funding_rate": float(rec["fundingRate"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["date", "ctx_funding_rate"])
    return frame.dropna(subset=["date", "ctx_funding_rate"]).sort_values("date")


def add_funding_features(frame: pd.DataFrame, z_window_hours: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.copy().sort_values("date")
    indexed = frame.set_index("date")
    frame["ctx_funding_8h_mean"] = (
        indexed["ctx_funding_rate"].rolling("8h", min_periods=2).mean().to_numpy()
    )
    frame["ctx_funding_24h_mean"] = (
        indexed["ctx_funding_rate"].rolling("24h", min_periods=4).mean().to_numpy()
    )
    rolling = indexed["ctx_funding_rate"].rolling(f"{z_window_hours}h", min_periods=24)
    std = rolling.std(ddof=0).replace(0, np.nan)
    frame["ctx_funding_z"] = (
        frame["ctx_funding_rate"].to_numpy() - rolling.mean().to_numpy()
    ) / std.to_numpy()
    return frame


def build_context(args: argparse.Namespace) -> pd.DataFrame:
    funding_since_ms = int((time.time() - args.history_hours * 3600) * 1000)

    hl = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
    cb = ccxt.coinbase()
    try:
        futures_rows = hl.fetch_ohlcv(args.pair, timeframe=args.timeframe, limit=args.limit)
        spot_rows = cb.fetch_ohlcv(args.spot_pair, timeframe=args.timeframe, limit=args.limit)
    finally:
        hl.close()
        cb.close()

    futures = ohlcv_to_frame(futures_rows, "hl")
    spot = ohlcv_to_frame(spot_rows, "spot")
    funding = add_funding_features(
        fetch_hyperliquid_funding(args.coin, funding_since_ms),
        z_window_hours=args.funding_z_window_hours,
    )

    if futures.empty:
        raise RuntimeError(f"Hyperliquid returned no OHLCV rows for {args.pair}")
    if spot.empty:
        raise RuntimeError(f"Coinbase returned no OHLCV rows for {args.spot_pair}")
    if funding.empty:
        raise RuntimeError(f"Hyperliquid returned no funding rows for {args.coin}")

    merged = pd.merge_asof(
        futures.sort_values("date"),
        spot[["date", "spot_close"]].sort_values("date"),
        on="date",
        direction="nearest",
        tolerance=pd.Timedelta(args.spot_tolerance),
    )
    merged = pd.merge_asof(
        merged.sort_values("date"),
        funding.sort_values("date"),
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(args.funding_tolerance),
    )

    merged["ctx_basis_pct"] = (merged["hl_close"] - merged["spot_close"]) / merged["spot_close"]
    basis_roll = merged["ctx_basis_pct"].rolling(
        args.basis_window,
        min_periods=max(24, args.basis_window // 4),
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
    context = context.dropna(how="all", subset=keep[1:]).tail(args.max_rows)
    context["ctx_loaded"] = 1.0
    return context


def write_context(context: pd.DataFrame, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=output_file.parent,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        context.to_csv(tmp, index=False, quoting=csv.QUOTE_MINIMAL)
    tmp_path.replace(output_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh live funding/basis context for Freqtrade.")
    parser.add_argument("--coin", default=os.environ.get("FBC_CONTEXT_COIN", "ETH"))
    parser.add_argument("--pair", default=os.environ.get("FBC_CONTEXT_PAIR", "ETH/USDC:USDC"))
    parser.add_argument("--spot-pair", default=os.environ.get("FBC_CONTEXT_SPOT_PAIR", "ETH/USD"))
    parser.add_argument("--timeframe", default=os.environ.get("FBC_CONTEXT_TIMEFRAME", "5m"))
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("FT_CONTEXT_DIR", "user_data/data/context"),
        type=Path,
    )
    parser.add_argument("--refresh-secs", type=int, default=int(os.environ.get("FBC_CONTEXT_REFRESH_SECS", 300)))
    parser.add_argument("--history-hours", type=int, default=int(os.environ.get("FBC_CONTEXT_HISTORY_HOURS", 192)))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("FBC_CONTEXT_LIMIT", 1000)))
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("FBC_CONTEXT_MAX_ROWS", 2000)))
    parser.add_argument("--min-rows", type=int, default=int(os.environ.get("FBC_CONTEXT_MIN_ROWS", 300)))
    parser.add_argument("--spot-tolerance", default=os.environ.get("FBC_CONTEXT_SPOT_TOLERANCE", "10min"))
    parser.add_argument("--funding-tolerance", default=os.environ.get("FBC_CONTEXT_FUNDING_TOLERANCE", "2h"))
    parser.add_argument("--basis-window", type=int, default=int(os.environ.get("FBC_CONTEXT_BASIS_WINDOW", 288)))
    parser.add_argument(
        "--funding-z-window-hours",
        type=int,
        default=int(os.environ.get("FBC_CONTEXT_FUNDING_Z_WINDOW_HOURS", 168)),
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = parse_args()
    output_file = args.output_dir / f"{pair_to_filename(args.pair)}_{args.timeframe}.csv"

    while True:
        try:
            context = build_context(args)
            if len(context) < args.min_rows:
                raise RuntimeError(
                    f"context has {len(context)} rows, below required minimum {args.min_rows}"
                )
            write_context(context, output_file)
            latest = context["date"].max()
            LOG.info("Wrote %s rows to %s; latest=%s", len(context), output_file, latest)
            if args.once:
                return 0
        except Exception:
            LOG.exception("Could not refresh funding/basis context")
            if args.once:
                return 1
        time.sleep(args.refresh_secs)


if __name__ == "__main__":
    raise SystemExit(main())
