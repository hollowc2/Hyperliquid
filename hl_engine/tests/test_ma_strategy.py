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
