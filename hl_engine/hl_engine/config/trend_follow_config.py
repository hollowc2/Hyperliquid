"""Config for the multi-timeframe BTC trend-following strategy."""

from msgspec import field
from nautilus_trader.config import StrategyConfig


class TrendFollowConfig(StrategyConfig):
    """Nautilus-compatible config for TrendFollowStrategy."""

    instrument_id: str = "BTC-USD.HYPERLIQUID"

    source_bar_minutes: int = 1
    trade_bar_minutes: int = 60
    confirmation_timeframes: list[str] = field(default_factory=lambda: ["4h", "1d"])
    entry_filter_timeframe: str = "15m"
    use_entry_filter: bool = True
    strict_confirmation: bool = True

    # Daily BTC catalogs in this repo are often short. These defaults keep the
    # 1d confirmation useful for research while still requiring real alignment.
    fast_ema_period: int = 3
    slow_ema_period: int = 8
    atr_period: int = 14
    atr_stop_multiple: float = 3.0
    risk_fraction: float = 0.005
    min_stop_distance_pct: float = 0.001
    allow_long: bool = True
    allow_short: bool = True
    initial_balance_usdc: float = 1000.0
    max_position_usd: float = 1000.0
