"""
Liquidation Cascade Predictor.

Combines liquidation event flow and open interest shocks to detect
conditions conducive to cascade liquidations.
"""

from collections import deque

from apex_trader.data.types import LiquidationData, OpenInterestData


class LiquidationCascadeModel:
    """
    Tracks liquidation events and OI changes to compute a cascade score.

    cascade_score = 2.0 * liq_imbalance + 1.5 * oi_shock + 1.0 * proximity

    A high absolute cascade score indicates elevated cascade risk.
    """

    def __init__(self, cascade_threshold: float = 1.5, window: int = 100) -> None:
        self._cascade_threshold = cascade_threshold
        self._window = window
        # Rolling liquidation event buffer: (side, usd_value)
        self._liquidations: deque[tuple[str, float]] = deque(maxlen=window)
        # Rolling OI history for shock calculation
        self._oi_history: deque[float] = deque(maxlen=20)

    def update_liquidation(self, data: LiquidationData) -> None:
        """Ingest a LiquidationData event."""
        self._liquidations.append((data.side, data.usd_value))

    def update_oi(self, data: OpenInterestData) -> None:
        """Ingest an OpenInterestData snapshot."""
        self._oi_history.append(data.open_interest)

    def compute_cascade_score(self) -> float:
        """
        Compute cascade score:
            liq_imbalance = (long_liqs - short_liqs) / total_liqs
            oi_shock = |ΔOI / OI_prev|
            proximity = min(1.0, oi_shock * 10)
            score = 2.0 * liq_imbalance + 1.5 * oi_shock + 1.0 * proximity
        """
        if not self._liquidations:
            return 0.0

        long_liqs = sum(v for side, v in self._liquidations if side == "LONG")
        short_liqs = sum(v for side, v in self._liquidations if side == "SHORT")
        total_liqs = long_liqs + short_liqs

        liq_imbalance = (long_liqs - short_liqs) / total_liqs if total_liqs > 0 else 0.0

        oi_shock = 0.0
        if len(self._oi_history) >= 2:
            prev_oi = self._oi_history[-2]
            curr_oi = self._oi_history[-1]
            if prev_oi > 0.0:
                oi_shock = abs((curr_oi - prev_oi) / prev_oi)

        proximity = min(1.0, oi_shock * 10.0)

        cascade_score = 2.0 * liq_imbalance + 1.5 * oi_shock + 1.0 * proximity
        return cascade_score

    def is_cascade_mode(self) -> bool:
        """Returns True when cascade conditions are elevated."""
        return abs(self.compute_cascade_score()) > self._cascade_threshold
