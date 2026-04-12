"""
Volatility and trend features — rolling log returns computed from OHLCV bars.
"""

import math
from collections import deque
from typing import Optional

import numpy as np

from nautilus_trader.model.data import Bar


class VolatilityFeatures:
    """
    Stateful rolling volatility and trend feature extractor.

    Maintains two deques of log returns: short window and long window.
    """

    def __init__(self, short_window: int, long_window: int) -> None:
        self._short_window = short_window
        self._long_window = long_window
        self._log_returns: deque[float] = deque(maxlen=long_window)
        self._last_close: Optional[float] = None

    def update(self, bar: Bar) -> None:
        """Append log return from bar close price."""
        close = float(bar.close)
        if self._last_close is not None and self._last_close > 0.0:
            lr = math.log(close / self._last_close)
            self._log_returns.append(lr)
        self._last_close = close

    def realized_vol_short(self, bar_interval_secs: float = 60.0) -> float:
        """
        Annualized realized volatility over the short window.
        Returns 0.0 if insufficient data.
        """
        if len(self._log_returns) < self._short_window:
            return 0.0
        returns = list(self._log_returns)[-self._short_window:]
        return self._annualize(returns, bar_interval_secs)

    def realized_vol_long(self, bar_interval_secs: float = 60.0) -> float:
        """
        Annualized realized volatility over the long window.
        Returns 0.0 if insufficient data.
        """
        if len(self._log_returns) < self._long_window:
            return 0.0
        returns = list(self._log_returns)
        return self._annualize(returns, bar_interval_secs)

    def trend_strength(self) -> float:
        """
        Trend strength as the t-statistic of a linear regression of log returns
        over the long window. Positive = uptrend, negative = downtrend.
        Returns 0.0 if insufficient data.
        """
        if len(self._log_returns) < max(10, self._long_window // 2):
            return 0.0

        from scipy import stats

        returns = list(self._log_returns)
        x = np.arange(len(returns), dtype=float)
        result = stats.linregress(x, returns)
        # t-statistic = slope / stderr
        if result.stderr == 0.0:
            return 0.0
        return result.slope / result.stderr

    @staticmethod
    def _annualize(returns: list, bar_interval_secs: float) -> float:
        """Convert per-bar std to annualized vol."""
        if len(returns) < 2:
            return 0.0
        bars_per_year = (365 * 24 * 3600) / bar_interval_secs
        std = float(np.std(returns, ddof=1))
        return std * math.sqrt(bars_per_year)
