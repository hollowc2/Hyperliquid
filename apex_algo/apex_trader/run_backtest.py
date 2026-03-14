"""
APEX Trader — Backtest entry point.

Usage:
    python run_backtest.py

Requires historical data in a NautilusTrader catalog (Parquet format).
Update CATALOG_PATH and INSTRUMENT_ID below to match your data.
"""

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
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from apex_trader.config.apex_config import ApexConfig, ApexStrategyConfig
from apex_trader.strategy.apex_strategy import ApexStrategy

# --- Configuration ---
CATALOG_PATH = Path("data/catalog")
INSTRUMENT_ID = "BTC-USD.HYPERLIQUID"
START_TIME = "2024-01-01"
END_TIME = "2024-03-01"
STARTING_BALANCE_USDC = "100000 USDC"


def main() -> None:
    # Build a minimal ApexConfig without real credentials for backtesting
    from apex_trader.config.apex_config import HyperliquidConfig
    apex_config = ApexConfig(
        hyperliquid=HyperliquidConfig(
            wallet_address="0x0000000000000000000000000000000000000000",
            private_key="0x0000000000000000000000000000000000000000000000000000000000000000",
        )
    )

    strategy_config = ApexStrategyConfig(
        instrument_id=INSTRUMENT_ID,
        apex_config=apex_config,
    )

    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    bar_type = BarType.from_str(f"{INSTRUMENT_ID}-1-MINUTE-LAST-EXTERNAL")

    run_config = BacktestRunConfig(
        engine=BacktestEngineConfig(
            strategies=[
                strategy_config,
            ],
        ),
        venues=[
            BacktestVenueConfig(
                name="HYPERLIQUID",
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency="USDC",
                starting_balances=[STARTING_BALANCE_USDC],
                book_type=BookType.L2_MBP,
                default_leverage=Decimal("1"),
            ),
        ],
        data=[
            BacktestDataConfig(
                catalog_path=str(CATALOG_PATH),
                data_cls=OrderBookDelta,
                instrument_id=instrument_id,
                start_time=START_TIME,
                end_time=END_TIME,
            ),
            BacktestDataConfig(
                catalog_path=str(CATALOG_PATH),
                data_cls=TradeTick,
                instrument_id=instrument_id,
                start_time=START_TIME,
                end_time=END_TIME,
            ),
            BacktestDataConfig(
                catalog_path=str(CATALOG_PATH),
                data_cls=Bar,
                instrument_id=instrument_id,
                bar_spec="1-MINUTE-LAST",
                start_time=START_TIME,
                end_time=END_TIME,
            ),
        ],
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
