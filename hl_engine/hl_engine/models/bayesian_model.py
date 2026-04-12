"""
Bayesian Edge Model — combines features into a probability-of-edge estimate.

Uses a log-odds linear model with sigmoid activation:
    log_odds = Σ wᵢ * fᵢ
    p_model = sigmoid(log_odds)
    p_market = 0.5 - spread_cost / 2
    edge = p_model - p_market

A positive edge means the model predicts the trade will be profitable
after accounting for transaction costs (spread).
"""

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class FeatureVector:
    """Container for all model input features."""
    obi: float = 0.0           # Order Book Imbalance [-1, 1]
    tfi: float = 0.0           # Trade Flow Imbalance [-1, 1]
    mp_drift_norm: float = 0.0 # Microprice drift / mid (normalized)
    hawkes_norm: float = 0.0   # Normalized Hawkes intensity [0, ∞)
    cascade_score: float = 0.0 # Cascade score (signed)
    funding_pressure: float = 0.0  # Funding pressure [-3, 3]
    spread: float = 0.0        # Relative spread (for cost model)


class BayesianEdgeModel:
    """
    Log-odds edge model combining order book, trade flow, and macro signals.

    The model computes:
        log_odds = w1*OBI + w2*TFI + w3*mp_drift_norm
                 + w4*hawkes_norm + w5*cascade + w6*funding
        p_model = sigmoid(log_odds)
        p_market = 0.5 - spread/2   (no-edge prior accounting for spread cost)
        edge = p_model - p_market   (positive = favorable trade)

    Weights should be calibrated offline using historical data.
    """

    def __init__(
        self,
        w1: float = 0.30,  # OBI weight
        w2: float = 0.30,  # TFI weight
        w3: float = 0.20,  # microprice drift weight
        w4: float = 0.10,  # Hawkes intensity weight
        w5: float = 0.05,  # cascade score weight
        w6: float = 0.05,  # funding pressure weight
    ) -> None:
        self.w1 = w1
        self.w2 = w2
        self.w3 = w3
        self.w4 = w4
        self.w5 = w5
        self.w6 = w6

    def compute_edge(self, features: FeatureVector) -> float:
        """
        Compute directional edge given feature vector.

        Returns a value in approximately (-0.5, 0.5) where:
          > threshold → buy signal
          < -threshold → sell signal
          near 0 → no edge
        """
        log_odds = (
            self.w1 * features.obi
            + self.w2 * features.tfi
            + self.w3 * features.mp_drift_norm
            + self.w4 * features.hawkes_norm
            + self.w5 * features.cascade_score
            + self.w6 * features.funding_pressure
        )

        p_model = self._sigmoid(log_odds)
        # Market null hypothesis: 50/50 adjusted for half-spread cost
        p_market = 0.5 - features.spread / 2.0
        edge = p_model - p_market
        return edge

    def compute_direction(self, features: FeatureVector) -> int:
        """
        Returns +1 (long), -1 (short), or 0 (no signal).
        Uses raw log_odds sign before spread adjustment.
        """
        log_odds = (
            self.w1 * features.obi
            + self.w2 * features.tfi
            + self.w3 * features.mp_drift_norm
            + self.w4 * features.hawkes_norm
            + self.w5 * features.cascade_score
            + self.w6 * features.funding_pressure
        )
        if log_odds > 0:
            return 1
        elif log_odds < 0:
            return -1
        return 0

    @staticmethod
    def _sigmoid(x: float) -> float:
        """Numerically stable sigmoid."""
        if x >= 0:
            return 1.0 / (1.0 + math.exp(-x))
        else:
            exp_x = math.exp(x)
            return exp_x / (1.0 + exp_x)
