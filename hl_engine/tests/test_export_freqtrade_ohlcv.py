import importlib.util
from pathlib import Path


def _load_export_module():
    path = Path(__file__).resolve().parents[1] / "export_freqtrade_ohlcv.py"
    spec = importlib.util.spec_from_file_location("export_freqtrade_ohlcv", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_coin_defaults_match_hyperliquid_freqtrade_pair():
    export = _load_export_module()

    assert export.coin_to_instrument_id("eth") == "ETH-USD.HYPERLIQUID"
    assert export.coin_to_freqtrade_pair("eth") == "ETH/USDC:USDC"
    assert export.pair_to_filename("ETH/USDC:USDC") == "ETH_USDC_USDC"


def test_freqtrade_open_timestamp_uses_candle_open_ms():
    export = _load_export_module()

    close_ms = 1_700_000_300_000
    close_ns = close_ms * 1_000_000

    assert export.freqtrade_open_timestamp_ms(close_ns, "5m") == 1_700_000_000_001


def test_futures_output_path_uses_freqtrade_futures_subdirectory(tmp_path):
    export = _load_export_module()

    path = export.freqtrade_ohlcv_path(tmp_path, "ETH/USDC:USDC", "5m", "futures")

    assert path == tmp_path / "futures" / "ETH_USDC_USDC-5m-futures.json"


def test_resample_5m_rows_to_1h_ohlcv():
    export = _load_export_module()
    rows = [
        [3_600_000, 10.0, 11.0, 9.0, 10.5, 1.0],
        [3_900_000, 10.5, 12.0, 10.0, 11.5, 2.0],
        [7_200_000, 11.5, 13.0, 11.0, 12.0, 3.0],
    ]

    assert export.resample_ohlcv_rows(rows, "5m", "1h") == [
        [3_600_000, 10.0, 12.0, 9.0, 11.5, 3.0],
        [7_200_000, 11.5, 13.0, 11.0, 12.0, 3.0],
    ]
