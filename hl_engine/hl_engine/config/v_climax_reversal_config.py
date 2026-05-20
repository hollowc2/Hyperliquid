"""Config for the V-climax reversal strategy."""

from nautilus_trader.config import StrategyConfig


class VClimaxReversalConfig(StrategyConfig):
    """Nautilus-compatible config for VClimaxReversalStrategy."""

    instrument_id: str = "BTC-USD.HYPERLIQUID"

    # Hyperliquid has no native 2m candle in this adapter, so the strategy
    # subscribes to 1m bars and aggregates internally.
    bar_minutes: int = 2
    source_bar_minutes: int = 1

    lookback_bars: int = 10
    waterfall_drop_pct: float = 0.02
    volume_multiple: float = 2.5
    atr_period: int = 10
    atr_stop_multiple: float = 1.0
    min_stop_distance_pct: float = 0.002
    entry_slippage_cap_pct: float = 0.003
    risk_fraction: float = 0.005
    pending_entry_ttl_bars: int = 10
    round_trip_taker_fee_pct: float = 0.001

    fallback_account_equity: float = 10_000.0
