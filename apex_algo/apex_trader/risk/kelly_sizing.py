"""
Kelly Criterion Position Sizing.

Uses fractional Kelly to size positions:
    f_kelly = edge / variance * kelly_fraction
    f_kelly = clip(f_kelly, 0, max_fraction)

Applies an inventory penalty to skew sizing based on current position.
"""

import math


class KellySizer:
    """
    Fractional Kelly position sizer with inventory penalty.

    Parameters
    ----------
    kelly_fraction : float
        Multiplier on full Kelly (0.25 = quarter-Kelly).
    max_kelly_fraction : float
        Hard cap on the output fraction (prevents extreme leverage).
    inventory_penalty_scale : float
        How aggressively to reduce size when already in a position.
        0.0 = no penalty, 1.0 = full penalty (no new position if fully exposed).
    max_position_usd : float
        Maximum position size in USD.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_kelly_fraction: float = 0.20,
        inventory_penalty_scale: float = 0.5,
        max_position_usd: float = 10_000.0,
    ) -> None:
        self._kelly_fraction = kelly_fraction
        self._max_kelly_fraction = max_kelly_fraction
        self._inventory_penalty_scale = inventory_penalty_scale
        self._max_position_usd = max_position_usd

    def compute_kelly_fraction(self, edge: float, variance: float) -> float:
        """
        Compute fractional Kelly allocation.

        f = edge / variance * kelly_fraction, clipped to [0, max_fraction].
        Returns 0.0 if variance is zero or edge is non-positive.
        """
        if variance <= 0.0 or edge <= 0.0:
            return 0.0
        f_full = edge / variance
        f_kelly = f_full * self._kelly_fraction
        return min(f_kelly, self._max_kelly_fraction)

    def compute_position_size(
        self,
        f_kelly: float,
        portfolio_value: float,
        price: float,
        instrument,
        current_position_usd: float = 0.0,
        direction: int = 1,
    ) -> float:
        """
        Convert Kelly fraction to a concrete position size in base units.

        Applies inventory penalty: if current_position_usd is non-zero in
        the same direction, reduce new size proportionally.

        Returns 0.0 if price is zero or instrument has no size precision.
        """
        if price <= 0.0 or portfolio_value <= 0.0:
            return 0.0

        target_notional = f_kelly * portfolio_value
        target_notional = min(target_notional, self._max_position_usd)

        # Inventory penalty: reduce if already long and buying, etc.
        inventory_factor = 1.0
        if current_position_usd != 0.0:
            # same-direction position reduces new size
            same_dir = (direction > 0 and current_position_usd > 0) or \
                       (direction < 0 and current_position_usd < 0)
            if same_dir:
                used_fraction = abs(current_position_usd) / self._max_position_usd
                inventory_factor = max(0.0, 1.0 - used_fraction * self._inventory_penalty_scale)

        adjusted_notional = target_notional * inventory_factor
        size = adjusted_notional / price

        # Round to instrument precision
        precision = instrument.size_precision if hasattr(instrument, "size_precision") else 4
        factor = 10 ** precision
        size = math.floor(size * factor) / factor

        return size

    def estimate_variance(self, vol_features, bar_interval_secs: float = 60.0) -> float:
        """
        Estimate per-bar variance from annualized volatility.

        variance_bar = (annualized_vol)^2 / bars_per_year
        """
        annualized_vol = vol_features.realized_vol_short(bar_interval_secs)
        if annualized_vol <= 0.0:
            return 1e-6  # small non-zero to avoid division by zero
        bars_per_year = (365 * 24 * 3600) / bar_interval_secs
        return (annualized_vol ** 2) / bars_per_year
