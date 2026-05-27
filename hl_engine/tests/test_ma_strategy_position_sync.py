from hl_engine.strategy.ma_strategy import _extract_signed_position_qty


def test_extract_signed_position_qty_prefers_paper_state() -> None:
    account_state = {
        "assetPositions": [{"position": {"coin": "BTC", "szi": "0.2"}}],
        "paper": {"position_qty": "-0.013"},
    }

    assert _extract_signed_position_qty(account_state, "BTC-USD.HYPERLIQUID") == -0.013


def test_extract_signed_position_qty_falls_back_to_asset_position() -> None:
    account_state = {
        "assetPositions": [
            {"position": {"coin": "ETH", "szi": "1.0"}},
            {"position": {"coin": "BTC", "szi": "0.004"}},
        ],
    }

    assert _extract_signed_position_qty(account_state, "BTC-USD.HYPERLIQUID") == 0.004


def test_extract_signed_position_qty_returns_zero_when_flat() -> None:
    assert _extract_signed_position_qty({"assetPositions": []}, "BTC-USD.HYPERLIQUID") == 0.0
