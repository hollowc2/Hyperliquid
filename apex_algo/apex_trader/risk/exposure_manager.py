"""
Exposure Manager — pre-order risk checks.

All checks return (allowed: bool, reason: str).
"""

from typing import Tuple


class ExposureManager:
    """
    Hard risk limit enforcement before order submission.

    Checks (in order):
      1. Max notional position size
      2. Max leverage (total_notional / account_equity)
      3. Max drawdown (drawdown_limit)
      4. Reduce-only mode if drawdown > secondary threshold

    Parameters are sourced from RiskConfig.
    """

    def __init__(
        self,
        max_position_usd: float,
        max_leverage: float,
        drawdown_limit: float,
        drawdown_reduce_threshold: float,
    ) -> None:
        self._max_position_usd = max_position_usd
        self._max_leverage = max_leverage
        self._drawdown_limit = drawdown_limit
        self._drawdown_reduce_threshold = drawdown_reduce_threshold

        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0
        self._total_notional: float = 0.0

    def update_equity(self, equity: float) -> None:
        """Update current account equity and track peak."""
        self._current_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def update_notional(self, total_notional_usd: float) -> None:
        """Update total open notional exposure."""
        self._total_notional = total_notional_usd

    def check_new_order(
        self,
        order_notional_usd: float,
        is_reduce: bool = False,
    ) -> Tuple[bool, str]:
        """
        Run all pre-order risk checks.

        Parameters
        ----------
        order_notional_usd : float
            USD value of the prospective order.
        is_reduce : bool
            If True, only apply drawdown check (skip size/leverage for reduces).

        Returns (True, "") if allowed, (False, reason_str) if blocked.
        """
        # Always check drawdown (blocks ALL new orders including reduces at limit)
        drawdown_check, drawdown_reason = self.check_drawdown()
        if not drawdown_check:
            return False, drawdown_reason

        if is_reduce:
            return True, ""

        # Notional limit
        would_be_notional = self._total_notional + order_notional_usd
        if would_be_notional > self._max_position_usd:
            return False, (
                f"Notional limit: {would_be_notional:.0f} > {self._max_position_usd:.0f} USD"
            )

        # Leverage limit
        if self._current_equity > 0.0:
            leverage = would_be_notional / self._current_equity
            if leverage > self._max_leverage:
                return False, (
                    f"Leverage limit: {leverage:.2f}x > {self._max_leverage:.2f}x"
                )

        return True, ""

    def check_drawdown(self) -> Tuple[bool, str]:
        """
        Check if current drawdown exceeds hard limit.
        """
        if self._peak_equity <= 0.0:
            return True, ""
        drawdown = (self._peak_equity - self._current_equity) / self._peak_equity
        if drawdown >= self._drawdown_limit:
            return False, (
                f"Drawdown limit: {drawdown:.1%} >= {self._drawdown_limit:.1%}"
            )
        return True, ""

    def check_reduce_only(self) -> bool:
        """
        Returns True if drawdown has exceeded the reduce-only threshold.
        In reduce-only mode, new position entries are blocked.
        """
        if self._peak_equity <= 0.0:
            return False
        drawdown = (self._peak_equity - self._current_equity) / self._peak_equity
        return drawdown >= self._drawdown_reduce_threshold

    @property
    def current_drawdown(self) -> float:
        """Current drawdown fraction [0, 1]."""
        if self._peak_equity <= 0.0:
            return 0.0
        return max(0.0, (self._peak_equity - self._current_equity) / self._peak_equity)
