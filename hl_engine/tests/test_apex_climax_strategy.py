from collections import deque
from types import SimpleNamespace

from hl_engine.config.v_climax_reversal_config import VClimaxReversalConfig
from hl_engine.strategy.v_climax_reversal_strategy import (
    VClimaxReversalStrategy,
    ClimaxPhase,
    StrategyBar,
)


def _bar(open_, high, low, close, volume=100.0, ts=0):
    return StrategyBar(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ts_event=ts,
    )


def _strategy(config=None):
    s = VClimaxReversalStrategy.__new__(VClimaxReversalStrategy)
    s._config = config or VClimaxReversalConfig()
    s._bars = deque(maxlen=32)
    return s


def test_aggregates_two_one_minute_bars():
    config = VClimaxReversalConfig(bar_minutes=2, source_bar_minutes=1)
    s = _strategy(config)
    s._source_bucket = []

    first = SimpleNamespace(
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        ts_event=60,
    )
    second = SimpleNamespace(
        open=100.5,
        high=102.0,
        low=98.0,
        close=99.5,
        volume=15.0,
        ts_event=120,
    )

    assert s._add_source_bar(first) is None
    closed = s._add_source_bar(second)

    assert closed == StrategyBar(100.0, 102.0, 98.0, 99.5, 25.0, 120)


def test_detects_climax_with_current_window_low_and_prior_volume_sma():
    config = VClimaxReversalConfig(
        lookback_bars=10,
        atr_period=10,
        waterfall_drop_pct=0.02,
        volume_multiple=2.5,
    )
    s = _strategy(config)
    bars = [_bar(100, 101, 99, 100, 100, i) for i in range(10)]
    bars.append(_bar(100, 100.2, 96.0, 97.0, 260, 10))
    s._bars.extend(bars)

    climax = s._detect_climax()

    assert climax is not None
    assert climax.high == 100.2
    assert climax.low == 96.0


def test_rejects_waterfall_when_current_bar_is_not_window_low():
    config = VClimaxReversalConfig(lookback_bars=10, atr_period=10)
    s = _strategy(config)
    bars = [_bar(100, 105, 95, 100, 100, i) for i in range(10)]
    bars.append(_bar(100, 103, 96, 102, 300, 10))
    s._bars.extend(bars)

    assert s._detect_climax() is None


def test_initial_stop_enforces_minimum_distance_from_entry_reference():
    stop = VClimaxReversalStrategy._initial_stop(
        entry_ref=100.0,
        climax_low=99.95,
        atr=0.01,
        atr_stop_multiple=1.0,
        min_stop_distance_pct=0.002,
    )

    assert stop == 99.8


def test_phase_2_requires_net_profit_after_round_trip_fees():
    assert not VClimaxReversalStrategy._is_net_profitable(100.09, 100.0, 0.001)
    assert VClimaxReversalStrategy._is_net_profitable(100.10, 100.0, 0.001)


def test_trailing_stop_only_ratchets_up():
    s = _strategy()
    s._active_stop = 98.0

    s._raise_stop(97.0)
    assert s._active_stop == 98.0

    s._raise_stop(99.0)
    assert s._active_stop == 99.0


def test_pending_entry_expires_after_ttl_completed_bars():
    config = VClimaxReversalConfig(pending_entry_ttl_bars=1)
    s = _strategy(config)
    s._phase = ClimaxPhase.PENDING_ENTRY
    s._bars_since_climax = 1
    s._climax = SimpleNamespace(expires_after_bar_count=1)

    s._on_strategy_bar(_bar(100, 101, 99, 100))

    assert s._phase == ClimaxPhase.SEARCHING
    assert s._climax is None


def test_round_quantity_down_respects_increment_and_minimum():
    instrument = SimpleNamespace(
        size_increment=0.001,
        size_precision=3,
        min_quantity=0.001,
    )

    assert VClimaxReversalStrategy._round_quantity_down(0.0129, instrument) == 0.012
    assert VClimaxReversalStrategy._round_quantity_down(0.0009, instrument) == 0.0
