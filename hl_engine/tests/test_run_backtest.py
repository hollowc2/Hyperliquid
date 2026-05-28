from hl_engine.run_backtest import (
    NS_PER_DAY,
    _bar_parquet_files,
    _bar_type_label,
    _bounded_warmup_start_ns,
    _catalog_date_range,
    _parse_ts,
)


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


def test_bar_type_label_supports_coarser_trend_source_bars():
    assert _bar_type_label(1) == "1-MINUTE"
    assert _bar_type_label(15) == "15-MINUTE"


def test_bar_parquet_files_uses_requested_source_interval(tmp_path):
    bar_dir = (
        tmp_path
        / "data"
        / "bar"
        / "BTC-USD.HYPERLIQUID-15-MINUTE-LAST-EXTERNAL"
    )
    bar_dir.mkdir(parents=True)
    expected = bar_dir / "2026-01-01.parquet"
    expected.touch()

    assert _bar_parquet_files(tmp_path, "BTC-USD.HYPERLIQUID", 15) == [expected]
    assert _catalog_date_range(tmp_path, "BTC-USD.HYPERLIQUID", 15) == (
        "2026-01-01",
        "2026-01-01",
    )
