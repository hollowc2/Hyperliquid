from nautilus_trader.config import StrategyConfig


class MaCrossConfig(StrategyConfig):
    """Config for the MA crossover smoke-test strategy."""

    instrument_id: str = "BTC-USD.HYPERLIQUID"
    fast_period: int = 10
    slow_period: int = 30
    bar_minutes: int = 1  # bar aggregation in minutes (e.g. 1, 5, 30)
    initial_balance_usdc: float = 10_000.0
    min_signal_spread_bps: float = 0.0
    min_hold_bars: int = 0
    cooldown_bars_after_exit: int = 0
