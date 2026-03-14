"""
Market Regime Classifier.

Classifies current market state into one of five regimes:
  TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_LIQUIDITY

Regime determines whether the strategy is allowed to open new positions.
"""

from enum import Enum


class RegimeState(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"


class RegimeDetector:
    """
    Rule-based market regime classifier using volatility ratio, trend strength,
    and book liquidity depth.

    Detection priority (highest to lowest):
      1. LOW_LIQUIDITY  — book depth below minimum threshold
      2. HIGH_VOL       — vol_short / vol_long > vol_ratio_threshold
      3. TRENDING_UP/DOWN — |trend_strength| > trend_threshold
      4. RANGING        — all other conditions

    Parameters
    ----------
    vol_ratio_threshold : float
        Ratio of short-to-long vol that triggers HIGH_VOL regime.
    trend_threshold : float
        T-statistic magnitude for trending regime (from linregress).
    min_liquidity_usd : float
        Minimum total book depth (bid + ask USD) below which LOW_LIQUIDITY triggers.
    """

    def __init__(
        self,
        vol_ratio_threshold: float = 1.5,
        trend_threshold: float = 2.0,
        min_liquidity_usd: float = 50_000.0,
    ) -> None:
        self._vol_ratio_threshold = vol_ratio_threshold
        self._trend_threshold = trend_threshold
        self._min_liquidity_usd = min_liquidity_usd
        self._state: RegimeState = RegimeState.RANGING

    def update(
        self,
        vol_short: float,
        vol_long: float,
        trend_strength: float,
        book_depth_usd: float,
    ) -> RegimeState:
        """
        Classify and store current regime.

        Parameters
        ----------
        vol_short : annualized realized vol over short window
        vol_long : annualized realized vol over long window
        trend_strength : t-statistic from linear regression of log returns
        book_depth_usd : total bid + ask USD depth
        """
        # Priority 1: liquidity
        if book_depth_usd < self._min_liquidity_usd:
            self._state = RegimeState.LOW_LIQUIDITY
            return self._state

        # Priority 2: elevated volatility
        if vol_long > 0.0 and (vol_short / vol_long) > self._vol_ratio_threshold:
            self._state = RegimeState.HIGH_VOL
            return self._state

        # Priority 3: trend
        if trend_strength > self._trend_threshold:
            self._state = RegimeState.TRENDING_UP
        elif trend_strength < -self._trend_threshold:
            self._state = RegimeState.TRENDING_DOWN
        else:
            self._state = RegimeState.RANGING

        return self._state

    @property
    def state(self) -> RegimeState:
        return self._state

    def is_tradeable(self) -> bool:
        """
        Returns False during regimes that are too risky for new position entry.
        LOW_LIQUIDITY and HIGH_VOL are not tradeable by default.
        """
        return self._state not in (RegimeState.LOW_LIQUIDITY, RegimeState.HIGH_VOL)
