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

import os
from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
)
from nautilus_trader.model.data import Bar, BarType, OrderBookDelta, TradeTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from apex_trader.config.apex_config import ApexConfig, ApexStrategyConfig
from apex_trader.strategy.apex_strategy import ApexStrategy

# --- Configuration ---
CATALOG_PATH = Path(os.getenv("HL_CATALOG_PATH", "data/catalog"))
INSTRUMENT_ID = os.getenv("HL_INSTRUMENT_ID", "BTC-USD.HYPERLIQUID")
START_TIME = os.getenv("HL_START_DATE", "2024-01-01")
END_TIME = os.getenv("HL_END_DATE", "2024-03-01")
STARTING_BALANCE_USDC = os.getenv("HL_STARTING_BALANCE", "100000 USDC")


def _catalog_has(catalog: ParquetDataCatalog, data_cls, instrument_id: InstrumentId) -> bool:
    """Return True if the catalog contains any data of this type for this instrument."""
    try:
        data = catalog.query(data_cls, instrument_ids=[instrument_id.value], as_nautilus=False)
        return len(data) > 0
    except Exception:
        return False


def main() -> None:
    catalog = ParquetDataCatalog(str(CATALOG_PATH))
    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    bar_type = BarType.from_str(f"{INSTRUMENT_ID}-1-MINUTE-LAST-EXTERNAL")

    has_ob = _catalog_has(catalog, OrderBookDelta, instrument_id)
    has_trades = _catalog_has(catalog, TradeTick, instrument_id)
    has_bars = _catalog_has(catalog, Bar, instrument_id)

    print(f"Catalog data available for {INSTRUMENT_ID}:")
    print(f"  OrderBookDelta : {'YES' if has_ob else 'NO  (OBI/microprice features will be 0)'}")
    print(f"  TradeTick      : {'YES' if has_trades else 'NO  (TFI/Hawkes features will be 0)'}")
    print(f"  Bar            : {'YES' if has_bars else 'NO  (WARNING: bars required for regime/volatility)'}")

    if not has_bars:
        print(
            "\nNo bar data found in catalog. Run build_historical_catalog.py first.\n"
            f"  python build_historical_catalog.py"
        )
        return

    # Build minimal ApexConfig (no real credentials needed for backtesting)
    from apex_trader.config.apex_config import HyperliquidConfig
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

    # Build data configs — only include types present in the catalog
    data_configs = []

    if has_ob:
        data_configs.append(
            BacktestDataConfig(
                catalog_path=str(CATALOG_PATH),
                data_cls=OrderBookDelta,
                instrument_id=instrument_id,
                start_time=START_TIME,
                end_time=END_TIME,
            )
        )

    if has_trades:
        data_configs.append(
            BacktestDataConfig(
                catalog_path=str(CATALOG_PATH),
                data_cls=TradeTick,
                instrument_id=instrument_id,
                start_time=START_TIME,
                end_time=END_TIME,
            )
        )

    data_configs.append(
        BacktestDataConfig(
            catalog_path=str(CATALOG_PATH),
            data_cls=Bar,
            instrument_id=instrument_id,
            bar_spec="1-MINUTE-LAST",
            start_time=START_TIME,
            end_time=END_TIME,
        )
    )

    run_config = BacktestRunConfig(
        engine=BacktestEngineConfig(
            strategies=[strategy_config],
        ),
        venues=[
            BacktestVenueConfig(
                name="HYPERLIQUID",
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency="USDC",
                starting_balances=[STARTING_BALANCE_USDC],
                book_type=BookType.L2_MBP if has_ob else BookType.L1_MBP,
                default_leverage=Decimal("1"),
            ),
        ],
        data=data_configs,
    )

    node = BacktestNode(configs=[run_config])
    results = node.run()

    for result in results:
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(result)


if __name__ == "__main__":
    main()
