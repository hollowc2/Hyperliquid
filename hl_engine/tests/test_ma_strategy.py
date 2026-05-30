from hl_engine.config.ma_config import MaCrossConfig
from hl_engine.strategy.ma_strategy import MaCrossStrategy


def test_signal_spread_bps_measures_relative_fast_slow_gap():
    assert MaCrossStrategy._signal_spread_bps(101.0, 100.0) == 100.0
    assert MaCrossStrategy._signal_spread_bps(100.0, 0.0) == 0.0


def test_ma_churn_controls_are_configurable():
    config = MaCrossConfig(
        min_signal_spread_bps=2.0,
        min_hold_bars=5,
        cooldown_bars_after_exit=3,
    )

    assert config.min_signal_spread_bps == 2.0
    assert config.min_hold_bars == 5
    assert config.cooldown_bars_after_exit == 3


def test_ma_state_snapshot_exposes_position_and_churn_controls():
    config = MaCrossConfig(
        fast_period=2,
        slow_period=3,
        bar_minutes=5,
        min_signal_spread_bps=8.0,
        min_hold_bars=12,
        cooldown_bars_after_exit=6,
    )
    strategy = MaCrossStrategy.__new__(MaCrossStrategy)
    strategy._config = config
    strategy._closes = [100.0, 102.0, 104.0]
    strategy._instrument_id = None
    strategy._signed_position_qty = -0.001
    strategy._bars_since_position_change = 7
    strategy._exit_cooldown_bars_remaining = 2
    strategy._notional_limit_halted = False
    strategy._last_signal_reason = "spread_too_small"

    state = strategy._build_state_snapshot()

    assert state["position"]["side"] == "SHORT"
    assert state["position"]["signed_qty"] == -0.001
    assert state["ma_cross"]["bar_minutes"] == 5
    assert state["ma_cross"]["min_signal_spread_bps"] == 8.0
    assert state["ma_cross"]["bars_since_position_change"] == 7
    assert state["ma_cross"]["exit_cooldown_bars_remaining"] == 2
