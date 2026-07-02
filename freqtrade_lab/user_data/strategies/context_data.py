"""
Optional external context loader for Freqtrade strategies.

The helper intentionally reads local files only. Fetch jobs can write free-source
snapshots into user_data/data/context, while offline backtests keep working when
the directory is empty or FT_CONTEXT_ENABLED is unset.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

CONTEXT_ENABLED = os.environ.get("FT_CONTEXT_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CONTEXT_DIR = os.environ.get("FT_CONTEXT_DIR", "user_data/data/context")
CONTEXT_MAX_STALENESS_HOURS = int(os.environ.get("FT_CONTEXT_MAX_STALENESS_HOURS", 36))
CONTEXT_FEAR_MIN = float(os.environ.get("FT_CONTEXT_FEAR_MIN", 20))
CONTEXT_STABLECOIN_MIN_CHG_1D = float(
    os.environ.get("FT_CONTEXT_STABLECOIN_MIN_CHG_1D", -0.01)
)
CONTEXT_DXY_MAX_CHG_1D = float(os.environ.get("FT_CONTEXT_DXY_MAX_CHG_1D", 0.015))
CONTEXT_VIX_MAX_CHG_1D = float(os.environ.get("FT_CONTEXT_VIX_MAX_CHG_1D", 0.10))
CONTEXT_FUNDING_ABS_MAX = float(os.environ.get("FT_CONTEXT_FUNDING_ABS_MAX", 0.0015))

DEFAULT_CONTEXT_COLUMNS: dict[str, float | bool] = {
    "ctx_loaded": 0.0,
    "ctx_fear_greed": 50.0,
    "ctx_btc_ret_1h": 0.0,
    "ctx_btc_ret_1d": 0.0,
    "ctx_eth_btc_ret_1d": 0.0,
    "ctx_total_crypto_mcap_ret_1d": 0.0,
    "ctx_stablecoin_mcap_ret_1d": 0.0,
    "ctx_defillama_tvl_ret_1d": 0.0,
    "ctx_dxy_ret_1d": 0.0,
    "ctx_vix_ret_1d": 0.0,
    "ctx_fred_liquidity_z": 0.0,
    "ctx_funding_rate": 0.0,
    "ctx_risk_on_ok": True,
    "ctx_risk_off_ok": True,
    "ctx_stress_block": False,
    "ctx_funding_neutral": True,
}

SOURCE_FILES = {
    "binance": ("binance.csv", "binance.json", "binance.parquet"),
    "coinbase": ("coinbase.csv", "coinbase.json", "coinbase.parquet"),
    "coingecko": ("coingecko.csv", "coingecko.json", "coingecko.parquet"),
    "alternative_me": ("alternative_me.csv", "alternative_me.json", "alternative_me.parquet"),
    "fred": ("fred.csv", "fred.json", "fred.parquet"),
    "yahoo": ("yahoo.csv", "yahoo.json", "yahoo.parquet"),
    "defillama": ("defillama.csv", "defillama.json", "defillama.parquet"),
    "hyperliquid": ("hyperliquid.csv", "hyperliquid.json", "hyperliquid.parquet"),
    "context": ("context.csv", "context.json", "context.parquet"),
}


def add_optional_context(
    dataframe: pd.DataFrame,
    pair: str | None = None,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """
    Merge local external context onto a Freqtrade OHLCV dataframe.

    Returns the input dataframe with ctx_* columns. Missing files or disabled
    context produce neutral defaults, so strategies can always reference the
    columns safely.
    """
    dataframe = dataframe.copy()
    _apply_default_context(dataframe)

    if not CONTEXT_ENABLED or dataframe.empty or "date" not in dataframe.columns:
        return dataframe

    context = load_context_dataframe(pair=pair, timeframe=timeframe)
    if context.empty:
        return dataframe

    base = dataframe.assign(_ctx_row_order=np.arange(len(dataframe))).sort_values("date")
    context = context.sort_values("date")
    merged = pd.merge_asof(
        base,
        context,
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(hours=CONTEXT_MAX_STALENESS_HOURS),
        suffixes=("", "_ctx_file"),
    )

    for column in DEFAULT_CONTEXT_COLUMNS:
        file_column = f"{column}_ctx_file"
        if file_column in merged.columns:
            merged[column] = merged[file_column].combine_first(merged[column])
            merged = merged.drop(columns=[file_column])

    _finalize_context_flags(merged)
    merged = merged.sort_values("_ctx_row_order").drop(columns=["_ctx_row_order"])
    merged.index = dataframe.index
    return merged


def load_context_dataframe(
    pair: str | None = None,
    timeframe: str | None = None,
    context_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Load and combine all known local context source files."""
    root = _context_root(context_dir)
    if not root.exists():
        return pd.DataFrame()

    frames = []
    for path in _candidate_files(root, pair=pair, timeframe=timeframe):
        frame = _read_context_file(path)
        if frame.empty:
            continue
        frames.append(_normalize_context_frame(frame, source=path.stem))

    if not frames:
        return pd.DataFrame()

    combined = frames[0]
    for frame in frames[1:]:
        combined = pd.merge_asof(
            combined.sort_values("date"),
            frame.sort_values("date"),
            on="date",
            direction="nearest",
            tolerance=pd.Timedelta(minutes=1),
            suffixes=("", "_dup"),
        )
        dupes = [column for column in combined.columns if column.endswith("_dup")]
        for dupe in dupes:
            original = dupe.removesuffix("_dup")
            combined[original] = combined[original].combine_first(combined[dupe])
        if dupes:
            combined = combined.drop(columns=dupes)

    _apply_default_context(combined)
    _finalize_context_flags(combined)
    return combined.sort_values("date")


def _context_root(context_dir: str | Path | None = None) -> Path:
    raw = Path(context_dir or CONTEXT_DIR)
    if raw.is_absolute():
        return raw

    for base in (Path.cwd(), Path("/freqtrade")):
        candidate = base / raw
        if candidate.exists():
            return candidate
    return Path.cwd() / raw


def _candidate_files(root: Path, pair: str | None, timeframe: str | None) -> Iterable[Path]:
    safe_pair = (pair or "").replace("/", "_").replace(":", "_")
    names: list[str] = []
    for variants in SOURCE_FILES.values():
        names.extend(variants)
    if safe_pair:
        names.extend(
            f"{source}.{ext}"
            for source in (safe_pair, f"{safe_pair}_{timeframe}" if timeframe else safe_pair)
            for ext in ("csv", "json", "parquet")
        )

    seen = set()
    for name in names:
        path = root / name
        if path.exists() and path not in seen:
            seen.add(path)
            yield path


def _read_context_file(path: Path) -> pd.DataFrame:
    try:
        if path.suffix == ".csv":
            return pd.read_csv(path)
        if path.suffix == ".json":
            try:
                return pd.read_json(path)
            except ValueError:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    payload = payload.get("data", payload.get("prices", payload))
                return pd.DataFrame(payload)
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
    except Exception as exc:
        logger.warning("Could not load context file %s: %s", path, exc)
    return pd.DataFrame()


def _normalize_context_frame(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    frame = _normalize_date_column(frame)
    if frame.empty:
        return frame

    aliases = {
        "fear_greed": ("fear_greed", "fear_and_greed", "value"),
        "btc_close": ("btc_close", "btcusd_close", "btcusdt_close"),
        "eth_btc": ("eth_btc", "ethbtc", "eth_btc_close"),
        "total_crypto_mcap": ("total_crypto_mcap", "market_cap", "total_market_cap"),
        "stablecoin_mcap": ("stablecoin_mcap", "stablecoins_mcap", "stablecoin_market_cap"),
        "defillama_tvl": ("defillama_tvl", "tvl", "chain_tvl"),
        "dxy": ("dxy", "dx-y.ny", "usdollar"),
        "vix": ("vix", "^vix", "vix_close"),
        "fred_liquidity": ("fred_liquidity", "walcl", "rrp", "liquidity"),
        "funding_rate": ("funding_rate", "funding", "predicted_funding_rate"),
    }

    normalized = pd.DataFrame({"date": frame["date"]})
    for target, options in aliases.items():
        column = next((name for name in options if name in frame.columns), None)
        if column:
            normalized[target] = pd.to_numeric(frame[column], errors="coerce")

    passthrough = [column for column in frame.columns if column.startswith("ctx_")]
    for column in passthrough:
        normalized[column] = frame[column]

    if "fear_greed" in normalized.columns:
        normalized["ctx_fear_greed"] = normalized["fear_greed"]
    if "funding_rate" in normalized.columns:
        normalized["ctx_funding_rate"] = normalized["funding_rate"]

    _add_returns(normalized, "btc_close", "ctx_btc_ret_1h", periods=12)
    _add_returns(normalized, "btc_close", "ctx_btc_ret_1d", periods=288)
    _add_returns(normalized, "eth_btc", "ctx_eth_btc_ret_1d", periods=288)
    _add_returns(normalized, "total_crypto_mcap", "ctx_total_crypto_mcap_ret_1d", periods=1)
    _add_returns(normalized, "stablecoin_mcap", "ctx_stablecoin_mcap_ret_1d", periods=1)
    _add_returns(normalized, "defillama_tvl", "ctx_defillama_tvl_ret_1d", periods=1)
    _add_returns(normalized, "dxy", "ctx_dxy_ret_1d", periods=1)
    _add_returns(normalized, "vix", "ctx_vix_ret_1d", periods=1)

    if "fred_liquidity" in normalized.columns:
        rolling = normalized["fred_liquidity"].rolling(52, min_periods=12)
        normalized["ctx_fred_liquidity_z"] = (
            (normalized["fred_liquidity"] - rolling.mean()) / rolling.std(ddof=0)
        )

    keep = ["date"] + [column for column in normalized.columns if column.startswith("ctx_")]
    normalized = normalized[keep].dropna(how="all", subset=keep[1:])
    if not normalized.empty:
        normalized["ctx_loaded"] = 1.0
    logger.debug("Loaded %s context rows from %s", len(normalized), source)
    return normalized


def _normalize_date_column(frame: pd.DataFrame) -> pd.DataFrame:
    date_column = next(
        (
            column
            for column in ("date", "datetime", "timestamp", "time", "t")
            if column in frame.columns
        ),
        None,
    )
    if date_column is None:
        return pd.DataFrame()

    values = frame[date_column]
    numeric = pd.to_numeric(values, errors="coerce")
    unit = "ms" if numeric.dropna().gt(10_000_000_000).any() else "s"
    parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    parsed = parsed.combine_first(pd.to_datetime(values, utc=True, errors="coerce"))

    frame["date"] = parsed.dt.tz_convert(None)
    return frame.dropna(subset=["date"]).sort_values("date")


def _add_returns(
    frame: pd.DataFrame,
    source_column: str,
    target_column: str,
    periods: int,
) -> None:
    if source_column not in frame.columns:
        return
    frame[target_column] = frame[source_column].pct_change(periods=periods)


def _apply_default_context(dataframe: pd.DataFrame) -> None:
    for column, default in DEFAULT_CONTEXT_COLUMNS.items():
        if column not in dataframe.columns:
            dataframe[column] = default


def _finalize_context_flags(dataframe: pd.DataFrame) -> None:
    dataframe["ctx_risk_on_ok"] = (
        (dataframe["ctx_fear_greed"].fillna(50) >= CONTEXT_FEAR_MIN)
        & (
            dataframe["ctx_stablecoin_mcap_ret_1d"].fillna(0)
            >= CONTEXT_STABLECOIN_MIN_CHG_1D
        )
        & (dataframe["ctx_dxy_ret_1d"].fillna(0) <= CONTEXT_DXY_MAX_CHG_1D)
        & (dataframe["ctx_vix_ret_1d"].fillna(0) <= CONTEXT_VIX_MAX_CHG_1D)
    )
    dataframe["ctx_risk_off_ok"] = (
        dataframe["ctx_fear_greed"].fillna(50) <= (100 - CONTEXT_FEAR_MIN)
    ) | (dataframe["ctx_btc_ret_1d"].fillna(0) <= 0)
    dataframe["ctx_stress_block"] = (
        (dataframe["ctx_fear_greed"].fillna(50) < CONTEXT_FEAR_MIN)
        | (dataframe["ctx_stablecoin_mcap_ret_1d"].fillna(0) < CONTEXT_STABLECOIN_MIN_CHG_1D)
        | (dataframe["ctx_vix_ret_1d"].fillna(0) > CONTEXT_VIX_MAX_CHG_1D)
    )
    dataframe["ctx_funding_neutral"] = (
        dataframe["ctx_funding_rate"].fillna(0).abs() <= CONTEXT_FUNDING_ABS_MAX
    )
    dataframe["ctx_loaded"] = dataframe["ctx_loaded"].fillna(0.0)
