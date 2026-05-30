from types import SimpleNamespace

from hl_engine.config.trend_follow_config import TrendFollowConfig
from hl_engine.strategy.trend_follow_strategy import (
    TrendBar,
    TrendFollowStrategy,
    TrendRegime,
)


def _source_bar(open_, high, low, close, volume=1.0, ts=0, ts_init=None):
    return SimpleNamespace(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ts_event=ts,
        ts_init=ts if ts_init is None else ts_init,
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


def test_capped_risk_sized_quantity_respects_max_notional():
    instrument = SimpleNamespace(
        size_increment=0.001,
        size_precision=3,
        min_quantity=0.001,
    )

    qty = TrendFollowStrategy.capped_risk_sized_quantity(
        equity=1000.0,
        risk_fraction=0.005,
        entry_price=73_900.0,
        stop_price=74_000.0,
        max_position_usd=1000.0,
        instrument=instrument,
    )

    assert qty == 0.013
    assert qty * 73_900.0 <= 1000.0


def test_extract_orchestrator_position_prefers_paper_state():
    strategy = _strategy(TrendFollowConfig())

    position = strategy._extract_orchestrator_position(
        {
            "paper": {"position_qty": "-0.01254", "avg_price": "73907"},
            "assetPositions": [],
        }
    )

    assert position == {"signed_qty": -0.01254, "avg_px": 73907.0}


def test_live_historical_warmup_detection_uses_ts_init_not_event_time():
    config = TrendFollowConfig()
    strategy = _strategy(config)
    strategy._skip_historical_warmup_orders = True
    strategy._live_started_ns = 1_000_000_000_000

    assert strategy._is_historical_warmup_bar(
        _source_bar(100, 101, 99, 100, ts=990_000_000_000)
    )

    live_bar = _source_bar(100, 101, 99, 100, ts=990_000_000_000)
    live_bar.ts_init = strategy._live_started_ns + 1

    assert not strategy._is_historical_warmup_bar(live_bar)


def test_regime_invalidation_respects_min_hold_trade_bars(monkeypatch):
    config = TrendFollowConfig(min_hold_trade_bars=2)
    strategy = _strategy(config)
    strategy._active_side = "LONG"
    strategy._stop_price = 90.0
    strategy._bars_since_entry = 0
    strategy._entry_cooldown_trade_bars = 0
    strategy._active_entry_order_id = None
    strategy._active_exit_order_id = None
    exits = []

    monkeypatch.setattr(strategy, "_trail_stop", lambda bar: None)
    monkeypatch.setattr(strategy, "_aligned_signal", lambda: "MIXED")
    monkeypatch.setattr(strategy, "_check_bar_stop", lambda bar: None)
    monkeypatch.setattr(strategy, "_submit_exit", lambda reason: exits.append(reason))

    strategy._on_trade_bar(_trend_bar(100, 101, 99, 100))
    assert exits == []

    strategy._on_trade_bar(_trend_bar(100, 101, 99, 100))
    assert exits == ["regime_invalidated"]


def test_entry_cooldown_blocks_new_trend_entries(monkeypatch):
    config = TrendFollowConfig(cooldown_trade_bars_after_exit=2)
    strategy = _strategy(config)
    strategy._active_side = "FLAT"
    strategy._active_entry_order_id = None
    strategy._active_exit_order_id = None
    strategy._entry_cooldown_trade_bars = 2
    submitted = []

    monkeypatch.setattr(strategy, "_trail_stop", lambda bar: None)
    monkeypatch.setattr(strategy, "_aligned_signal", lambda: "LONG")
    monkeypatch.setattr(strategy, "_entry_filter_allows", lambda side: True)
    monkeypatch.setattr(strategy, "_initial_stop", lambda side, entry_price: entry_price - 10.0)
    monkeypatch.setattr(strategy, "_compute_order_quantity", lambda entry_price, stop_price: 1.0)
    monkeypatch.setattr(strategy, "_submit_entry", lambda side, qty: submitted.append((side, qty)))

    strategy._on_trade_bar(_trend_bar(100, 101, 99, 100))

    assert submitted == []
    assert strategy._last_signal_reason == "entry_cooldown"
