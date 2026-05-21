from hl_engine.run_backtest import NS_PER_DAY, _bounded_warmup_start_ns, _parse_ts


def test_bounded_warmup_start_clamps_at_zero():
    assert _bounded_warmup_start_ns(NS_PER_DAY, 7) == 0


def test_bounded_warmup_start_uses_requested_days():
    start_ns = 10 * NS_PER_DAY

    assert _bounded_warmup_start_ns(start_ns, 3) == 7 * NS_PER_DAY


def test_bounded_warmup_start_treats_negative_days_as_zero():
    start_ns = 10 * NS_PER_DAY

    assert _bounded_warmup_start_ns(start_ns, -3) == start_ns


def test_parse_ts_accepts_intraday_iso_timestamp():
    assert _parse_ts("2026-04-13T01:00:00") - _parse_ts("2026-04-13") == 3_600 * 1_000_000_000
