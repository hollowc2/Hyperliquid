"""
APEX Trader — Live trading entry point.

Usage:
    python run_live.py

Requires .env file with:
    HL_WALLET_ADDRESS=0x...
    HL_PRIVATE_KEY=0x...
    HL_TESTNET=false  (or true for testnet)
"""

import asyncio

from nautilus_trader.config import (
    LiveExecEngineConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig, RoutingConfig
from nautilus_trader.live.node import TradingNode

from apex_trader.adapters.hyperliquid.factories import (
    HyperliquidLiveDataClientFactory,
    HyperliquidLiveExecClientFactory,
    HyperliquidPaperExecClientFactory,
)
from apex_trader.config.apex_config import ApexConfig, ApexStrategyConfig
from apex_trader.strategy.apex_strategy import ApexStrategy


def main() -> None:
    import os
    from pathlib import Path
    from dotenv import load_dotenv

    # Load .env relative to this file so it works regardless of cwd
    _env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=_env_path, override=False)

    paper_trade = os.getenv("HL_PAPER_TRADE", "false").lower() == "true"
    print(f"paper_trade={paper_trade}  HL_PAPER_TRADE={os.getenv('HL_PAPER_TRADE')!r}")

    # Load config from environment
    apex_config = ApexConfig.from_env()

    # Pass apex_config to factories via class variables (before node.build())
    HyperliquidLiveDataClientFactory._apex_config = apex_config
    exec_factory = HyperliquidPaperExecClientFactory if paper_trade else HyperliquidLiveExecClientFactory
    exec_factory._apex_config = apex_config

    if paper_trade:
        print("*** PAPER TRADING MODE — no real orders will be sent ***")

    strategy_config = ApexStrategyConfig(
        instrument_id="BTC-USD.HYPERLIQUID",
        apex_config=apex_config,
    )

    node_config = TradingNodeConfig(
        trader_id="APEX-TRADER-001",
        data_clients={
            "HYPERLIQUID": LiveDataClientConfig(routing=RoutingConfig(default=True))
        },
        exec_clients={
            "HYPERLIQUID": LiveExecClientConfig(routing=RoutingConfig(default=True))
        },
        exec_engine=LiveExecEngineConfig(
            reconciliation=False if paper_trade else True,
        ),
    )

    node = TradingNode(config=node_config)

    # Register client factories
    node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)
    node.add_exec_client_factory("HYPERLIQUID", exec_factory)

    # Add strategy
    strategy = ApexStrategy(config=strategy_config)
    node.trader.add_strategy(strategy)

    node.build()

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nShutting down APEX Trader...")
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
