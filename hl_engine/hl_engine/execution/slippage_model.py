"""
Slippage Model — estimates execution costs from order book state.

Provides:
  - Book-walk slippage for a given quantity and side
  - Queue position fill probability for passive limit orders
"""

from typing import Tuple


class SlippageModel:
    """
    Estimates slippage and queue fill probability from the live order book.
    """

    @staticmethod
    def estimate_limit_slippage(book, quantity: float, is_buy: bool) -> float:
        """
        Walk the book to estimate slippage for a marketable order of `quantity`.

        Returns slippage as a fraction of the arrival mid-price (e.g., 0.001 = 0.1%).
        Returns 0.0 if book is empty.
        """
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid is None or best_ask is None:
            return 0.0

        mid = (float(best_bid) + float(best_ask)) / 2.0
        if mid == 0.0:
            return 0.0

        # Walk the relevant side of the book
        levels = book.asks(50) if is_buy else book.bids(50)

        arrival_px = float(best_ask) if is_buy else float(best_bid)
        remaining = quantity
        total_cost = 0.0
        total_filled = 0.0

        for level in levels:
            px = float(level.price)
            sz = float(level.size)
            fill = min(remaining, sz)
            total_cost += fill * px
            total_filled += fill
            remaining -= fill
            if remaining <= 0.0:
                break

        if total_filled == 0.0:
            return 0.0

        avg_px = total_cost / total_filled
        slippage = abs(avg_px - arrival_px) / mid
        return slippage

    @staticmethod
    def estimate_queue_position(book, price: float, is_buy: bool, our_size: float) -> float:
        """
        Estimate probability of fill for a passive limit order at `price`.

        Uses a simple model: P(fill) = our_size / (our_size + queue_ahead)
        where queue_ahead is the total size at the same price level.

        Returns a probability in [0, 1].
        """
        if our_size <= 0.0:
            return 0.0

        levels = book.bids(20) if is_buy else book.asks(20)

        for level in levels:
            if abs(float(level.price) - price) < 1e-8:
                level_size = float(level.size)
                # Simple queue model: uniform distribution assumption
                prob = our_size / (our_size + level_size)
                return min(1.0, prob)

        # Price level not in book — likely empty, so high fill probability
        return 0.9
