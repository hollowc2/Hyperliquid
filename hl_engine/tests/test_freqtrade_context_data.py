import importlib.util
from pathlib import Path

import pandas as pd


def _load_context_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "freqtrade_lab"
        / "user_data"
        / "strategies"
        / "context_data.py"
    )
    spec = importlib.util.spec_from_file_location("context_data", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_normalizes_funding_basis_context_columns():
    context_data = _load_context_module()
    raw = pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000, 1_700_000_300_000],
            "basis_pct": [0.001, 0.002],
            "basis_z": [0.5, 1.5],
            "funding_rate": [0.00001, 0.00002],
            "funding_8h_mean": [0.000011, 0.000012],
            "funding_24h_mean": [0.000009, 0.000010],
            "funding_z": [0.2, 0.4],
        }
    )

    normalized = context_data._normalize_context_frame(raw, "ETH_USDC_USDC_5m")

    assert normalized["ctx_loaded"].tolist() == [1.0, 1.0]
    assert normalized["ctx_basis_pct"].tolist() == [0.001, 0.002]
    assert normalized["ctx_basis_z"].tolist() == [0.5, 1.5]
    assert normalized["ctx_funding_rate"].tolist() == [0.00001, 0.00002]
    assert normalized["ctx_funding_8h_mean"].tolist() == [0.000011, 0.000012]
    assert normalized["ctx_funding_24h_mean"].tolist() == [0.000009, 0.000010]
    assert normalized["ctx_funding_z"].tolist() == [0.2, 0.4]


def test_no_context_fallback_adds_neutral_funding_basis_columns():
    context_data = _load_context_module()
    context_data.CONTEXT_ENABLED = False
    dataframe = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, freq="5min"),
            "open": [1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )

    with_context = context_data.add_optional_context(dataframe, "ETH/USDC:USDC", "5m")

    for column in (
        "ctx_basis_pct",
        "ctx_basis_z",
        "ctx_funding_8h_mean",
        "ctx_funding_24h_mean",
        "ctx_funding_z",
    ):
        assert column in with_context
        assert with_context[column].tolist() == [0.0, 0.0, 0.0]
    assert with_context["ctx_loaded"].tolist() == [0.0, 0.0, 0.0]
