"""
Hawkes Process — O(1) recursive intensity estimation.

The recursive form avoids summing all historical events:
    λ(t) = μ + (λ(t⁻) - μ) * exp(-β * Δt) + α

This updates in constant time per event, suitable for tick-level processing.
"""

import math


class HawkesProcess:
    """
    Self-exciting Hawkes process with exponential kernel.

    Parameters
    ----------
    mu : float
        Background (baseline) intensity.
    alpha : float
        Jump size per event (excitation magnitude).
    beta : float
        Decay rate of excitation (higher = faster decay).
    """

    def __init__(self, mu: float, alpha: float, beta: float) -> None:
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self._lambda_t: float = mu
        self._last_ts: float = 0.0

    def update(self, ts_ns: int) -> float:
        """
        Update intensity at event time ts_ns (nanoseconds).

        Uses recursive form: λ(t) = μ + (λ(t⁻) - μ) * exp(-β * Δt) + α

        Returns the new intensity λ(t).
        """
        ts = ts_ns / 1e9  # convert to seconds
        dt = ts - self._last_ts
        # Decay previous intensity, then add jump
        self._lambda_t = (
            self.mu
            + (self._lambda_t - self.mu) * math.exp(-self.beta * dt)
            + self.alpha
        )
        self._last_ts = ts
        return self._lambda_t

    def current_intensity(self, ts_ns: int) -> float:
        """
        Query current intensity at time ts_ns without adding an event.
        Applies decay only (no jump).
        """
        ts = ts_ns / 1e9
        dt = ts - self._last_ts
        if dt <= 0.0:
            return self._lambda_t
        return self.mu + (self._lambda_t - self.mu) * math.exp(-self.beta * dt)

    def normalized_intensity(self, ts_ns: int) -> float:
        """
        Normalized intensity: (λ(t) - μ) / μ.
        0.0 = baseline, >0 = elevated activity.
        """
        if self.mu == 0.0:
            return 0.0
        intensity = self.current_intensity(ts_ns)
        return (intensity - self.mu) / self.mu

    def reset(self) -> None:
        """Reset to baseline intensity."""
        self._lambda_t = self.mu
        self._last_ts = 0.0
