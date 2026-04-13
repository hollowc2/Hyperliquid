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
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import Bar, BarType, CustomData, DataType, OrderBookDelta, TradeTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from hl_engine.config.apex_config import ApexConfig, ApexStrategyConfig, HyperliquidConfig
from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData
from hl_engine.strategy.apex_strategy import ApexStrategy

# --- Configuration ---
CATALOG_PATH = Path(os.getenv("HL_CATALOG_PATH", "data/catalog"))
INSTRUMENT_ID = os.getenv("HL_INSTRUMENT_ID", "BTC-USD.HYPERLIQUID")
START_TIME = os.getenv("HL_START_DATE", "2024-01-01")
END_TIME = os.getenv("HL_END_DATE", "2024-03-01")
STARTING_BALANCE_USDC = float(os.getenv("HL_STARTING_BALANCE", "100000"))


def _catalog_has(catalog: ParquetDataCatalog, data_cls, instrument_id: InstrumentId) -> bool:
    """Return True if the catalog contains any data of this type for this instrument."""
    try:
        data = catalog.query(data_cls, instrument_ids=[instrument_id.value], as_nautilus=False)
        return len(data) > 0
    except Exception:
        return False


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


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Trader backtest runner")
    parser.add_argument(
        "--strategy",
        choices=["apex", "ma"],
        default="apex",
        help="Strategy to run: 'apex' (full ApexStrategy) or 'ma' (MA crossover smoke test)",
    )
    parser.add_argument(
        "--bar-minutes",
        type=int,
        default=1,
        help="Bar aggregation in minutes for the MA strategy (default: 1). "
             "Values > 1 resample 1-min catalog bars.",
    )
    args = parser.parse_args()

    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)

    has_ob = _catalog_has(catalog, OrderBookDelta, instrument_id)
    has_trades = _catalog_has(catalog, TradeTick, instrument_id)
    has_bars = _catalog_has(catalog, Bar, instrument_id)

    start_ns = _parse_ts(START_TIME)
    end_ns = _parse_ts(END_TIME)

    funding_rows = _load_custom_data(CATALOG_PATH, "funding_rate", INSTRUMENT_ID, start_ns, end_ns)
    oi_rows = _load_custom_data(CATALOG_PATH, "open_interest", INSTRUMENT_ID, start_ns, end_ns)
    liq_rows = _load_custom_data(CATALOG_PATH, "liquidations", INSTRUMENT_ID, start_ns, end_ns)

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
        default_leverage=Decimal("1"),
    )

    # --- Instruments ---
    instruments = catalog.instruments(instrument_ids=[INSTRUMENT_ID])
    for inst in instruments:
        engine.add_instrument(inst)

    # --- Data ---
    if has_ob:
        ob_data = catalog.order_book_deltas(
            instrument_ids=[INSTRUMENT_ID],
            start=start_ns,
            end=end_ns,
        )
        engine.add_data(ob_data)

    if has_trades:
        trade_data = catalog.trade_ticks(
            instrument_ids=[INSTRUMENT_ID],
            start=start_ns,
            end=end_ns,
        )
        engine.add_data(trade_data)

    bar_type_1m = BarType.from_str(f"{INSTRUMENT_ID}-1-MINUTE-LAST-EXTERNAL")
    bar_data_1m = catalog.bars(
        instrument_ids=[INSTRUMENT_ID],
        bar_types=[str(bar_type_1m)],
        start=start_ns,
        end=end_ns,
    )

    # For the MA strategy with bar_minutes > 1, resample before adding to engine
    bar_minutes = args.bar_minutes if args.strategy == "ma" else 1
    if bar_minutes > 1:
        inst = catalog.instruments(instrument_ids=[INSTRUMENT_ID])[0]
        bar_data = _resample_bars(bar_data_1m, bar_minutes, inst)
        print(f"  Resampled {len(bar_data_1m)} 1-min bars → {len(bar_data)} {bar_minutes}-min bars")
    else:
        bar_data = bar_data_1m
    engine.add_data(bar_data)

    # --- Custom data (funding, OI, liquidations) ---
    if funding_rows:
        engine.add_data(_rows_to_funding(funding_rows, instrument_id))
    if oi_rows:
        engine.add_data(_rows_to_oi(oi_rows, instrument_id))
    if liq_rows:
        engine.add_data(_rows_to_liquidations(liq_rows, instrument_id))

    # --- Strategy ---
    if args.strategy == "ma":
        from hl_engine.config.ma_config import MaCrossConfig
        from hl_engine.strategy.ma_strategy import MaCrossStrategy
        strategy = MaCrossStrategy(config=MaCrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_minutes=bar_minutes,
        ))
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
