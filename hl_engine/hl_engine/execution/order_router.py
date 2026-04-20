"""
Order Router — decides order type, price, and TIF based on market conditions.

Returns an OrderTypeDecision dataclass that the strategy uses to build orders.
"""

from dataclasses import dataclass
from typing import Optional

from nautilus_trader.model.enums import OrderType, TimeInForce


@dataclass
class OrderTypeDecision:
    """Result of routing decision."""
    order_type: OrderType
    price: Optional[float]           # None for MARKET orders
    time_in_force: TimeInForce
    post_only: bool                  # True → maker only (GTC limit)


class OrderRouter:
    """
    Smart order routing logic.

    Routing rules (in priority order):
      1. Cascade mode → MARKET (IOC with slippage buffer at execution layer)
      2. Good queue probability → LIMIT GTC post-only at best bid/ask
      3. Poor queue probability → LIMIT GTC one tick inside spread
    """

    def __init__(self, min_queue_prob: float = 0.3) -> None:
        self._min_queue_prob = min_queue_prob

    def route(
        self,
        book,
        instrument,
        quantity: float,
        is_buy: bool,
        is_cascade_mode: bool,
        slippage_model=None,
    ) -> OrderTypeDecision:
        """
        Determine optimal order type and price.

        Parameters
        ----------
        book : NautilusTrader OrderBook
        instrument : NautilusTrader Instrument
        quantity : float
            Order quantity in base units.
        is_buy : bool
        is_cascade_mode : bool
            If True, use market order for immediate fill.
        slippage_model : SlippageModel, optional
            Used to estimate queue fill probability.
        """
        # Rule 1: cascade → urgent market order
        if is_cascade_mode:
            return OrderTypeDecision(
                order_type=OrderType.MARKET,
                price=None,
                time_in_force=TimeInForce.IOC,
                post_only=False,
            )

        # Get best prices
        best_bid_price = book.best_bid_price()
        best_ask_price = book.best_ask_price()

        if best_bid_price is None or best_ask_price is None:
            # No book data → market order
            return OrderTypeDecision(
                order_type=OrderType.MARKET,
                price=None,
                time_in_force=TimeInForce.IOC,
                post_only=False,
            )

        best_bid = float(best_bid_price)
        best_ask = float(best_ask_price)

        # Passive limit price (join best bid/ask)
        passive_px = best_bid if is_buy else best_ask

        # Check queue probability at passive price
        queue_prob = 0.5  # default
        if slippage_model is not None:
            queue_prob = slippage_model.estimate_queue_position(
                book=book,
                price=passive_px,
                is_buy=is_buy,
                our_size=quantity,
            )

        if queue_prob >= self._min_queue_prob:
            # Rule 2: good queue → post at best bid/ask
            price = self._round_to_precision(passive_px, instrument.price_precision)
            return OrderTypeDecision(
                order_type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.GTC,
                post_only=True,
            )
        else:
            # Rule 3: poor queue → improve price by one tick
            tick = float(instrument.price_increment)
            if is_buy:
                improved_px = best_bid + tick
                improved_px = min(improved_px, best_ask - tick)  # don't cross spread
            else:
                improved_px = best_ask - tick
                improved_px = max(improved_px, best_bid + tick)

            price = self._round_to_precision(improved_px, instrument.price_precision)
            return OrderTypeDecision(
                order_type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.GTC,
                post_only=False,  # may cross the spread by one tick
            )

    def route_no_book(self, is_buy: bool, is_cascade_mode: bool, ref_px: float = 0.0) -> OrderTypeDecision:
        """MARKET IOC fallback used when no live order book is available."""
        return OrderTypeDecision(
            order_type=OrderType.MARKET,
            price=None,
            time_in_force=TimeInForce.IOC,
            post_only=False,
        )

    @staticmethod
    def _round_to_precision(value: float, precision: int) -> float:
        factor = 10 ** precision
        return round(value * factor) / factor
