from hl_engine.config.apex_config import ApexStrategyConfig
from hl_engine.config.ma_config import MaCrossConfig
from hl_engine.config.v_climax_reversal_config import VClimaxReversalConfig
from hl_engine.orchestrator.strategy_registry import StrategyRegistry
from hl_engine.run_strategy import _build_strategy_config


def test_strategy_registry_loads_vclimax_btc_config():
    registry = StrategyRegistry("strategies")
    registry.load()

    spec = registry.get("vclimax-btc")

    assert spec is not None
    assert spec.class_path == "hl_engine.strategy.v_climax_reversal_strategy.VClimaxReversalStrategy"
    assert spec.config_class_path == "hl_engine.config.v_climax_reversal_config.VClimaxReversalConfig"
    assert spec.instrument_id == "BTC-USD.HYPERLIQUID"
    assert spec.parameters["fallback_account_equity"] == 1000.0
    assert spec.risk.max_position_usd == 1000.0
    assert spec.docker.container_name == "strategy-vclimax-btc"


def test_run_strategy_builds_vclimax_config_with_1000_equity():
    config = _build_strategy_config(
        VClimaxReversalConfig,
        {"fallback_account_equity": 1000.0},
        "BTC-USD.HYPERLIQUID",
    )

    assert config.fallback_account_equity == 1000.0
    assert config.instrument_id == "BTC-USD.HYPERLIQUID"


def test_apex_and_ma_configs_use_1000_paper_initial_balance():
    registry = StrategyRegistry("strategies")
    registry.load()

    apex = registry.get("apex-btc")
    ma = registry.get("ma-cross-btc")

    assert apex is not None
    assert ma is not None
    assert apex.parameters["initial_balance_usdc"] == 1000.0
    assert ma.parameters["initial_balance_usdc"] == 1000.0
    assert apex.risk.max_position_usd == 1000.0
    assert ma.risk.max_position_usd == 1000.0

    apex_config = _build_strategy_config(
        ApexStrategyConfig,
        apex.parameters,
        apex.instrument_id,
    )
    ma_config = _build_strategy_config(
        MaCrossConfig,
        ma.parameters,
        ma.instrument_id,
    )

    assert apex_config.initial_balance_usdc == 1000.0
    assert ma_config.initial_balance_usdc == 1000.0
