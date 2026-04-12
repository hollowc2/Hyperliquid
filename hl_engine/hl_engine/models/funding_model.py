"""
Funding Pressure Model.

Computes a composite funding pressure signal combining:
  - Funding rate z-score (relative to recent history)
  - Price momentum (from strategy)
  - OI growth rate

Result is clipped to [-3.0, 3.0].
"""

from collections import deque


class FundingPressureModel:
    """
    Tracks rolling funding rates and OI to compute a directional pressure signal.

    A positive value suggests longs are paying (bullish pressure may ease),
    a negative value suggests shorts are paying (bearish pressure may ease).
    """

    def __init__(self, history_len: int = 168) -> None:
        # 168 = 1 week of 8-hour funding periods
        self._funding_history: deque[float] = deque(maxlen=history_len)
        self._oi_history: deque[float] = deque(maxlen=history_len)

    def update_funding(self, rate: float) -> None:
        """Record a new funding rate observation."""
        self._funding_history.append(rate)

    def update_oi(self, open_interest: float) -> None:
        """Record a new open interest observation."""
        self._oi_history.append(open_interest)

    def compute_pressure(self, price_momentum: float = 0.0) -> float:
        """
        Compute funding pressure signal.

        funding_z = (rate - mean) / std
        oi_growth = (oi[-1] - oi[-2]) / oi[-2]
        pressure = funding_z * price_momentum * oi_growth
        Clipped to [-3.0, 3.0].

        Returns 0.0 if insufficient data.
        """
        if len(self._funding_history) < 2:
            return 0.0

        import numpy as np

        rates = list(self._funding_history)
        mean = float(np.mean(rates))
        std = float(np.std(rates, ddof=1))

        if std == 0.0:
            return 0.0

        current_rate = rates[-1]
        funding_z = (current_rate - mean) / std

        oi_growth = 0.0
        if len(self._oi_history) >= 2:
            prev_oi = self._oi_history[-2]
            curr_oi = self._oi_history[-1]
            if prev_oi > 0.0:
                oi_growth = (curr_oi - prev_oi) / prev_oi

        pressure = funding_z * price_momentum * oi_growth
        return float(max(-3.0, min(3.0, pressure)))

    @property
    def current_funding_rate(self) -> float:
        """Most recent funding rate, or 0.0 if no data."""
        return self._funding_history[-1] if self._funding_history else 0.0
