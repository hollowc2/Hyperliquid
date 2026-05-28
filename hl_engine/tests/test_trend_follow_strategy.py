from types import SimpleNamespace

from hl_engine.config.trend_follow_config import TrendFollowConfig
from hl_engine.strategy.trend_follow_strategy import (
    TrendBar,
    TrendFollowStrategy,
    TrendRegime,
)


def _source_bar(open_, high, low, close, volume=1.0, ts=0):
    return SimpleNamespace(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ts_event=ts,
    )


def _trend_bar(open_, high, low, close, volume=1.0, ts=0):
    return TrendBar(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ts_event=ts,
    )


def _strategy(config):
    strategy = TrendFollowStrategy.__new__(TrendFollowStrategy)
    strategy._config = config
    strategy._timeframes = TrendFollowStrategy._build_timeframe_states(config)
    return strategy


def test_aggregates_source_bars_into_15m_1h_4h_and_1d():
    config = TrendFollowConfig(
        source_bar_minutes=1,
        trade_bar_minutes=60,
        confirmation_timeframes=["4h", "1d"],
        entry_filter_timeframe="15m",
    )
    strategy = _strategy(config)

    closed = []
    for minute in range(1440):
        closed.extend(
            strategy._add_source_bar_to_timeframes(
                _source_bar(100, 101, 99, 100 + minute, volume=2.0, ts=(minute + 1) * 60)
            )
        )

    closed_names = [name for name, _ in closed]
    assert closed_names.count("15m") == 96
    assert closed_names.count("1h") == 24
    assert closed_names.count("4h") == 6
    assert closed_names.count("1d") == 1

    one_day_bar = closed[-1][1]
    assert one_day_bar.open == 100.0
    assert one_day_bar.high == 101.0
    assert one_day_bar.low == 99.0
    assert one_day_bar.close == 1539.0
    assert one_day_bar.volume == 2880.0


def test_ema_regime_classifies_bullish_bearish_and_warmup():
    assert (
        TrendFollowStrategy.classify_ema_regime([1, 2, 3, 4, 5, 6], 2, 4)
        == TrendRegime.BULLISH
    )
    assert (
        TrendFollowStrategy.classify_ema_regime([6, 5, 4, 3, 2, 1], 2, 4)
        == TrendRegime.BEARISH
    )
    assert (
        TrendFollowStrategy.classify_ema_regime([1, 2, 3], 2, 4)
        == TrendRegime.WARMING_UP
    )


def test_alignment_requires_all_timeframes_when_strict():
    assert (
        TrendFollowStrategy.classify_alignment(
            [TrendRegime.BULLISH, TrendRegime.BULLISH, TrendRegime.BULLISH],
            strict=True,
        )
        == "LONG"
    )
    assert (
        TrendFollowStrategy.classify_alignment(
            [TrendRegime.BEARISH, TrendRegime.BEARISH, TrendRegime.BEARISH],
            strict=True,
        )
        == "SHORT"
    )
    assert (
        TrendFollowStrategy.classify_alignment(
            [TrendRegime.BULLISH, TrendRegime.MIXED, TrendRegime.BULLISH],
            strict=True,
        )
        == "MIXED"
    )


def test_non_strict_alignment_can_select_majority_direction():
    assert (
        TrendFollowStrategy.classify_alignment(
            [TrendRegime.BULLISH, TrendRegime.BEARISH, TrendRegime.BEARISH],
            strict=False,
        )
        == "SHORT"
    )


def test_atr_sizing_and_stop_placement_guard_zero_distance():
    bars = [
        _trend_bar(100, 105, 95, 102),
        _trend_bar(102, 110, 101, 108),
        _trend_bar(108, 112, 100, 101),
    ]
    atr = TrendFollowStrategy.atr(bars)

    assert atr == 10.5
    assert TrendFollowStrategy.stop_price("LONG", 100.0, atr, 2.0, 0.001) == 79.0
    assert TrendFollowStrategy.stop_price("SHORT", 100.0, atr, 2.0, 0.001) == 121.0
    assert TrendFollowStrategy.stop_price("LONG", 100.0, 0.0, 2.0, 0.001) == 100.0


def test_risk_sized_quantity_rounds_down_and_rejects_zero_atr_distance():
    instrument = SimpleNamespace(
        size_increment=0.001,
        size_precision=3,
        min_quantity=0.001,
    )

    qty = TrendFollowStrategy.risk_sized_quantity(
        equity=1000.0,
        risk_fraction=0.005,
        entry_price=100.0,
        stop_price=95.0,
        instrument=instrument,
    )

    assert qty == 1.0
    assert (
        TrendFollowStrategy.risk_sized_quantity(
            equity=1000.0,
            risk_fraction=0.005,
            entry_price=100.0,
            stop_price=100.0,
            instrument=instrument,
        )
        == 0.0
    )
