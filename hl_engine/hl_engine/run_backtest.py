"""
APEX Trader — Backtest entry point.

Automatically detects which data types are present in the catalog and
only requests those. This means it works in two modes:

  Bars-only mode  (historical catalog built via build_historical_catalog.py)
    → OBI and microprice features are 0; strategy still signals via
      Hawkes (if trade ticks present), funding pressure, and regime.

  Full mode  (catalog accumulated via record_live_data.py)
    → All features active: OBI, microprice, TFI, Hawkes, cascade, funding.

Update CATALOG_PATH, INSTRUMENT_ID, START_TIME, END_TIME below,
or set them via environment variables.

Usage:
    python run_backtest.py
"""

import argparse
import os
import struct
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import Bar, BarSpecification, BarType, CustomData, DataType, OrderBookDelta, TradeTick
from nautilus_trader.model.enums import AccountType, AggregationSource, BarAggregation, BookType, OmsType, PriceType
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from hl_engine.config.apex_config import ApexConfig, ApexStrategyConfig, HyperliquidConfig
from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData
from hl_engine.strategy.apex_strategy import ApexStrategy

# --- Configuration ---
CATALOG_PATH = Path(os.getenv("HL_CATALOG_PATH", "data/catalog"))
INSTRUMENT_ID = os.getenv("HL_INSTRUMENT_ID", "BTC-USD.HYPERLIQUID")
STARTING_BALANCE_USDC = float(os.getenv("HL_STARTING_BALANCE", "100000"))


def _catalog_has(catalog: ParquetDataCatalog, data_cls, instrument_id: InstrumentId) -> bool:
    """Return True if the catalog contains any data of this type for this instrument."""
    try:
        data = catalog.query(data_cls, instrument_ids=[instrument_id.value], as_nautilus=False)
        return len(data) > 0
    except Exception:
        return False


def _ob_parquet_files(catalog_path: Path, instrument_id: str) -> list[Path]:
    """Return OB delta Parquet files sorted chronologically."""
    ob_dir = catalog_path / "data" / "order_book_deltas" / instrument_id
    if not ob_dir.exists():
        return []
    return sorted(ob_dir.glob("*.parquet"))


def _add_ob_data_chunked(
    catalog_path: Path,
    instrument_id: InstrumentId,
    start_ns: int,
    end_ns: int,
    engine: "BacktestEngine",
) -> int:
    """
    Load OB deltas from Parquet one row-group at a time and add to the engine
    immediately. This keeps peak memory to ~one row-group instead of the entire
    day's worth of deltas held simultaneously in Arrow + NT object form.
    """
    import gc
    import pyarrow.compute as pc
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    files = _ob_parquet_files(catalog_path, instrument_id.value)
    total = 0
    for f in files:
        pf = pq.ParquetFile(f)
        for rg_idx in range(pf.metadata.num_row_groups):
            rg_meta = pf.metadata.row_group(rg_idx)
            rg_min = rg_max = None
            for col_idx in range(rg_meta.num_columns):
                col = rg_meta.column(col_idx)
                if col.path_in_schema == "ts_event" and col.statistics is not None:
                    rg_min = col.statistics.min
                    rg_max = col.statistics.max
                    break
            if rg_min is None:
                # No statistics — fall back to reading ts_event column
                table = pf.read_row_group(rg_idx, columns=["ts_event"])
                ts_arr = table.column("ts_event")
                rg_min = pc.min(ts_arr).as_py()
                rg_max = pc.max(ts_arr).as_py()
                del table
            if rg_max < start_ns or rg_min > end_ns:
                continue

            table = pf.read_row_group(rg_idx)
            if rg_min < start_ns or rg_max > end_ns:
                mask = pc.and_(
                    pc.greater_equal(table.column("ts_event"), start_ns),
                    pc.less_equal(table.column("ts_event"), end_ns),
                )
                table = table.filter(mask)
            if len(table) == 0:
                del table
                continue

            pyo3_deltas = ArrowSerializer.deserialize(data_cls=OrderBookDelta, batch=table)
            del table
            cython_deltas = OrderBookDelta.from_pyo3_list(pyo3_deltas)
            del pyo3_deltas
            engine.add_data(cython_deltas)
            total += len(cython_deltas)
            del cython_deltas
            gc.collect()

    return total


def _bar_parquet_files(catalog_path: Path, instrument_id: str) -> list[Path]:
    """Return all 1-minute bar Parquet files sorted by filename (chronological)."""
    bar_dir = catalog_path / "data" / "bar" / f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    if not bar_dir.exists():
        return []
    return sorted(bar_dir.glob("*.parquet"))


def _load_bars_direct(catalog: "ParquetDataCatalog", instrument_id_str: str, start_ns: int, end_ns: int) -> list[Bar]:
    """
    Load 1-minute bars via the NT catalog, then deduplicate by ts_event.
    Dedup is needed because the catalog accumulates duplicate rows from repeated
    flush cycles before compaction.
    """
    bar_type_str = f"{instrument_id_str}-1-MINUTE-LAST-EXTERNAL"
    try:
        bars: list[Bar] = catalog.bars([bar_type_str], start=start_ns, end=end_ns)
    except Exception:
        return []

    # Deduplicate: keep the last bar written for each ts_event.
    seen: dict[int, Bar] = {}
    for b in bars:
        seen[b.ts_event] = b
    return sorted(seen.values(), key=lambda b: b.ts_event)


def _load_custom_data(
    catalog_path: Path,
    type_name: str,
    instrument_id: str,
    start_ns: int,
    end_ns: int,
) -> list[dict]:
    """
    Load custom Parquet rows (funding_rate / open_interest / liquidations)
    for a given instrument and time range. Returns dicts sorted by ts_event.
    """
    base = catalog_path / "custom" / type_name / instrument_id
    if not base.exists():
        return []
    files = sorted(base.glob("*.parquet"))
    if not files:
        return []

    rows: list[dict] = []
    for f in files:
        table = pq.read_table(f)
        for batch in table.to_batches():
            for i in range(batch.num_rows):
                row = {col: batch.column(col)[i].as_py() for col in table.schema.names}
                if start_ns <= row["ts_event"] <= end_ns:
                    rows.append(row)

    rows.sort(key=lambda r: r["ts_event"])
    return rows


def _rows_to_funding(rows: list[dict], instrument_id: InstrumentId) -> list[CustomData]:
    data_type = DataType(FundingRateData, metadata={"instrument_id": instrument_id})
    out = []
    for r in rows:
        obj = FundingRateData(
            instrument_id=instrument_id,
            rate=r["rate"],
            next_funding_time=r["next_funding_time"],
            open_interest=r["open_interest"],
            ts_event=r["ts_event"],
            ts_init=r["ts_init"],
        )
        out.append(CustomData(data_type, obj))
    return out


def _rows_to_oi(rows: list[dict], instrument_id: InstrumentId) -> list[CustomData]:
    data_type = DataType(OpenInterestData, metadata={"instrument_id": instrument_id})
    out = []
    for r in rows:
        obj = OpenInterestData(
            instrument_id=instrument_id,
            open_interest=r["open_interest"],
            open_interest_usd=r["open_interest_usd"],
            ts_event=r["ts_event"],
            ts_init=r["ts_init"],
        )
        out.append(CustomData(data_type, obj))
    return out


def _rows_to_liquidations(rows: list[dict], instrument_id: InstrumentId) -> list[CustomData]:
    data_type = DataType(LiquidationData, metadata={"instrument_id": instrument_id})
    out = []
    for r in rows:
        obj = LiquidationData(
            instrument_id=instrument_id,
            side=r["side"],
            quantity=r["quantity"],
            price=r["price"],
            usd_value=r["usd_value"],
            ts_event=r["ts_event"],
            ts_init=r["ts_init"],
        )
        out.append(CustomData(data_type, obj))
    return out


def _parse_ts(ts: str) -> int:
    """Convert YYYY-MM-DD string to nanosecond timestamp."""
    from datetime import datetime, timezone
    dt = datetime.strptime(ts, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def _resample_bars(bars_1m, bar_minutes: int, instrument):
    """Resample 1-minute Bar objects to a larger OHLCV bar period using pandas."""
    import pandas as pd
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
    from nautilus_trader.model.objects import Price, Quantity

    records = [
        {
            "ts": b.ts_event,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        }
        for b in bars_1m
    ]
    df = pd.DataFrame(records)
    df.index = pd.to_datetime(df["ts"], unit="ns", utc=True)
    df = df.sort_index()

    resampled = df.resample(f"{bar_minutes}min", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()

    target_bar_type = BarType(
        instrument_id=instrument.id,
        bar_spec=BarSpecification(
            step=bar_minutes,
            aggregation=BarAggregation.MINUTE,
            price_type=PriceType.LAST,
        ),
        aggregation_source=AggregationSource.EXTERNAL,
    )

    out = []
    for ts, row in resampled.iterrows():
        ts_ns = int(ts.timestamp() * 1e9)
        out.append(
            Bar(
                bar_type=target_bar_type,
                open=Price(row["open"], instrument.price_precision),
                high=Price(row["high"], instrument.price_precision),
                low=Price(row["low"], instrument.price_precision),
                close=Price(row["close"], instrument.price_precision),
                volume=Quantity(row["volume"], instrument.size_precision),
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
        )
    return out


def _catalog_date_range(catalog_path: Path, instrument_id: str) -> tuple[str, str]:
    """Return (first_date, last_date) strings from bar parquet filenames, or defaults."""
    files = _bar_parquet_files(catalog_path, instrument_id)
    if not files:
        return ("2026-04-12", "2026-04-19")
    # Compacted files are named YYYY-MM-DD.parquet
    names = sorted(f.stem for f in files if f.stem.count("-") == 2)
    if names:
        return (names[0], names[-1])
    return ("2026-04-12", "2026-04-19")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    default_start, default_end = _catalog_date_range(CATALOG_PATH, INSTRUMENT_ID)

    parser = argparse.ArgumentParser(description="APEX Trader backtest runner")
    parser.add_argument(
        "--strategy",
        choices=["apex", "ma", "vclimax"],
        default="apex",
        help=(
            "Strategy to run: 'apex' (full ApexStrategy), "
            "'ma' (MA crossover smoke test), or 'vclimax' (V-climax reversal)"
        ),
    )
    parser.add_argument(
        "--start",
        default=os.getenv("HL_START_DATE", default_start),
        help=f"Start date YYYY-MM-DD (default: {default_start}, earliest available bar data)",
    )
    parser.add_argument(
        "--end",
        default=os.getenv("HL_END_DATE", default_end),
        help=f"End date YYYY-MM-DD (default: {default_end}, latest available bar data)",
    )
    parser.add_argument(
        "--bar-minutes",
        type=int,
        default=1,
        help="Bar aggregation in minutes for the MA strategy (default: 1). "
             "Values > 1 resample 1-min catalog bars.",
    )
    parser.add_argument(
        "--with-ob",
        action="store_true",
        default=False,
        help="Load L2 order book data (enables OBI/microprice features). "
             "Uses ~4-8 GB RAM per week of data — limit date range accordingly.",
    )
    parser.add_argument(
        "--vclimax-waterfall-drop-pct",
        type=float,
        default=None,
        help="Override v-climax waterfall drop threshold, e.g. 0.01 for 1%.",
    )
    parser.add_argument(
        "--vclimax-volume-multiple",
        type=float,
        default=None,
        help="Override v-climax volume multiple threshold.",
    )
    args = parser.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)

    needs_apex_data = args.strategy == "apex"
    needs_ob_data = args.strategy in {"apex", "vclimax"}

    has_ob = needs_ob_data and args.with_ob and len(_ob_parquet_files(CATALOG_PATH, INSTRUMENT_ID)) > 0
    has_trades = needs_apex_data and _catalog_has(catalog, TradeTick, instrument_id)
    has_bars = len(_bar_parquet_files(CATALOG_PATH, INSTRUMENT_ID)) > 0

    start_ns = _parse_ts(args.start)
    end_ns = _parse_ts(args.end)

    print(f"Backtest window: {args.start} → {args.end}")

    # Funding and OI are loaded without a lower time bound so that historical
    # records written by build_historical_catalog.py pre-seed the FundingModel
    # before the first bar arrives (warmup). Liquidations are backtest-window only.
    if needs_apex_data:
        funding_rows = _load_custom_data(CATALOG_PATH, "funding_rate", INSTRUMENT_ID, 0, end_ns)
        oi_rows = _load_custom_data(CATALOG_PATH, "open_interest", INSTRUMENT_ID, 0, end_ns)
        liq_rows = _load_custom_data(CATALOG_PATH, "liquidations", INSTRUMENT_ID, start_ns, end_ns)
    else:
        funding_rows = []
        oi_rows = []
        liq_rows = []

    print(f"Catalog data available for {INSTRUMENT_ID}:")
    print(f"  OrderBookDelta : {'YES' if has_ob else 'NO  (OBI/microprice features will be 0)'}")
    print(f"  TradeTick      : {'YES' if has_trades else 'NO  (TFI/Hawkes features will be 0)'}")
    print(f"  Bar            : {'YES' if has_bars else 'NO  (WARNING: bars required for regime/volatility)'}")
    print(f"  FundingRateData: {'YES (' + str(len(funding_rows)) + ' events)' if funding_rows else 'NO  (funding_pressure feature will be 0)'}")
    print(f"  OpenInterestData:{' YES (' + str(len(oi_rows)) + ' events)' if oi_rows else ' NO  (OI growth signal will be 0)'}")
    print(f"  LiquidationData: {'YES (' + str(len(liq_rows)) + ' events)' if liq_rows else 'NO  (cascade_score=0, cascade mode never triggers)'}")

    if not has_bars:
        print(
            "\nNo bar data found in catalog. Run build_historical_catalog.py first.\n"
            "  python build_historical_catalog.py"
        )
        return

    # --- Engine ---
    engine = BacktestEngine(config=BacktestEngineConfig())

    # --- Venue ---
    engine.add_venue(
        venue=Venue("HYPERLIQUID"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDC,
        starting_balances=[Money(STARTING_BALANCE_USDC, USDC)],
        book_type=BookType.L2_MBP if has_ob else BookType.L1_MBP,
        default_leverage=Decimal("20"),  # HL BTC default; affects margin math not risk limits
    )

    # --- Instruments ---
    instruments = catalog.instruments(instrument_ids=[INSTRUMENT_ID])
    for inst in instruments:
        engine.add_instrument(inst)

    # --- Data ---
    if has_ob:
        print("  Loading OB deltas (chunked by row-group)...")
        ob_count = _add_ob_data_chunked(CATALOG_PATH, instrument_id, start_ns, end_ns, engine)
        print(f"  OrderBookDelta : {ob_count:,} deltas loaded")

    if has_trades:
        trade_data = catalog.trade_ticks(
            instrument_ids=[INSTRUMENT_ID],
            start=start_ns,
            end=end_ns,
        )
        if trade_data:
            engine.add_data(trade_data)
            print(f"  TradeTick      : {len(trade_data)} ticks loaded")
        else:
            print("  TradeTick      : 0 ticks loaded for selected window")

    bar_data_1m = _load_bars_direct(catalog, INSTRUMENT_ID, start_ns, end_ns)
    print(f"  Bars (1m)      : {len(bar_data_1m)} bars loaded")

    # For the MA strategy with bar_minutes > 1, resample before adding to engine.
    # The v-climax strategy consumes 1m bars and aggregates internally.
    bar_minutes = args.bar_minutes if args.strategy == "ma" else 1
    if bar_minutes > 1:
        inst = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
        bar_data = _resample_bars(bar_data_1m, bar_minutes, inst)
        print(f"  Resampled {len(bar_data_1m)} 1-min bars → {len(bar_data)} {bar_minutes}-min bars")
    else:
        bar_data = bar_data_1m
    engine.add_data(bar_data)

    # --- Custom data (funding, OI, liquidations) ---
    # CustomData has no instrument_id visible to NT, so client_id is required.
    hl_client_id = ClientId("HYPERLIQUID")
    if funding_rows:
        engine.add_data(_rows_to_funding(funding_rows, instrument_id), client_id=hl_client_id)
    if oi_rows:
        engine.add_data(_rows_to_oi(oi_rows, instrument_id), client_id=hl_client_id)
    if liq_rows:
        engine.add_data(_rows_to_liquidations(liq_rows, instrument_id), client_id=hl_client_id)

    # --- Strategy ---
    if args.strategy == "ma":
        from hl_engine.config.ma_config import MaCrossConfig
        from hl_engine.strategy.ma_strategy import MaCrossStrategy
        strategy = MaCrossStrategy(config=MaCrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_minutes=bar_minutes,
        ))
    elif args.strategy == "vclimax":
        from hl_engine.config.v_climax_reversal_config import VClimaxReversalConfig
        from hl_engine.strategy.v_climax_reversal_strategy import VClimaxReversalStrategy

        vclimax_kwargs = {"instrument_id": INSTRUMENT_ID}
        if args.vclimax_waterfall_drop_pct is not None:
            vclimax_kwargs["waterfall_drop_pct"] = args.vclimax_waterfall_drop_pct
        if args.vclimax_volume_multiple is not None:
            vclimax_kwargs["volume_multiple"] = args.vclimax_volume_multiple
        strategy = VClimaxReversalStrategy(config=VClimaxReversalConfig(**vclimax_kwargs))
    else:
        # Build minimal ApexConfig (no real credentials needed for backtesting)
        apex_config = ApexConfig(
            hyperliquid=HyperliquidConfig(
                wallet_address="0x0000000000000000000000000000000000000000",
                private_key="0x" + "0" * 64,
            )
        )
        strategy_config = ApexStrategyConfig(
            instrument_id=INSTRUMENT_ID,
            apex_config=apex_config,
        )
        strategy = ApexStrategy(config=strategy_config)
    engine.add_strategy(strategy)

    # --- Run ---
    engine.run()

    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(engine.get_result())


if __name__ == "__main__":
    main()
