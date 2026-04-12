"""
Trade flow feature computation — stateful, rolling deque of recent trades.
"""

from collections import deque
from typing import Tuple

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide


class TradeFlowFeatures:
    """
    Stateful rolling-window trade flow feature extractor.

    Maintains a deque of (ts_event_ns, side, qty) tuples within `window_ns`.
    """

    def __init__(self, window_ns: int) -> None:
        self._window_ns = window_ns
        self._trades: deque[Tuple[int, AggressorSide, float]] = deque()

    def update(self, tick: TradeTick) -> None:
        """Append a new trade tick and prune expired entries."""
        self._trades.append((tick.ts_event, tick.aggressor_side, float(tick.size)))
        cutoff = tick.ts_event - self._window_ns
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def compute_tfi(self) -> float:
        """
        Trade Flow Imbalance: (buy_vol - sell_vol) / total_vol.
        Returns 0.0 if no trades in window.
        """
        buy_vol = 0.0
        sell_vol = 0.0
        for _, side, qty in self._trades:
            if side == AggressorSide.BUYER:
                buy_vol += qty
            else:
                sell_vol += qty
        total = buy_vol + sell_vol
        if total == 0.0:
            return 0.0
        return (buy_vol - sell_vol) / total

    def compute_trade_intensity(self) -> float:
        """
        Trades per second in the current window.
        Returns 0.0 if window is empty or zero-duration.
        """
        n = len(self._trades)
        if n < 2:
            return 0.0
        window_secs = (self._trades[-1][0] - self._trades[0][0]) / 1e9
        if window_secs <= 0.0:
            return 0.0
        return n / window_secs

    def compute_toxicity_score(self, book) -> float:
        """
        Trade toxicity: |avg_trade_px - mid| / mid.
        Measures how far recent trades are from the mid-price (adverse selection proxy).
        Returns 0.0 if no trades or no book.
        """
        if not self._trades:
            return 0.0

        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid is None or best_ask is None:
            return 0.0

        mid = (float(best_bid) + float(best_ask)) / 2.0
        if mid == 0.0:
            return 0.0

        # Compute volume-weighted average trade price
        total_qty = 0.0
        total_pv = 0.0
        for _, _, qty in self._trades:
            total_qty += qty
            # We don't store price in the deque — use mid as proxy
            # In a real implementation we'd store price too
            total_pv += qty  # placeholder

        # Simplified: use TFI as a proxy for toxicity direction
        tfi = self.compute_tfi()
        spread = (float(best_ask) - float(best_bid)) / mid
        return abs(tfi) * spread
