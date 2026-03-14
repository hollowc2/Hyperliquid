"""
Order book feature computation — stateless methods operating on a NautilusTrader OrderBook.
"""

from typing import Tuple


class OrderBookFeatures:
    """
    Pure stateless order book feature extractor.
    All methods accept a NautilusTrader OrderBook object.
    """

    @staticmethod
    def compute_obi(book, depth: int = 5) -> float:
        """
        Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol).
        Returns 0.0 if no liquidity on either side.
        """
        bids = book.bids(depth)
        asks = book.asks(depth)

        bid_vol = sum(float(level.size) for level in bids)
        ask_vol = sum(float(level.size) for level in asks)

        total = bid_vol + ask_vol
        if total == 0.0:
            return 0.0
        return (bid_vol - ask_vol) / total

    @staticmethod
    def compute_microprice(book) -> Tuple[float, float]:
        """
        Microprice: size-weighted mid = (ask * bid_size + bid * ask_size) / (bid_size + ask_size).

        Returns (microprice, drift) where drift = microprice - mid.
        Returns (0.0, 0.0) if book is empty.
        """
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid is None or best_ask is None:
            return 0.0, 0.0

        bid_px = float(best_bid)
        ask_px = float(best_ask)

        bid_sz = float(book.best_bid_size()) or 0.0
        ask_sz = float(book.best_ask_size()) or 0.0

        denom = bid_sz + ask_sz
        if denom == 0.0:
            mid = (bid_px + ask_px) / 2.0
            return mid, 0.0

        microprice = (ask_px * bid_sz + bid_px * ask_sz) / denom
        mid = (bid_px + ask_px) / 2.0
        drift = microprice - mid
        return microprice, drift

    @staticmethod
    def compute_spread(book) -> float:
        """
        Relative spread: (ask - bid) / mid.
        Returns 0.0 if book is empty.
        """
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid is None or best_ask is None:
            return 0.0

        bid_px = float(best_bid)
        ask_px = float(best_ask)
        mid = (bid_px + ask_px) / 2.0
        if mid == 0.0:
            return 0.0
        return (ask_px - bid_px) / mid

    @staticmethod
    def compute_book_depth_usd(book, depth_levels: int = 10) -> Tuple[float, float]:
        """
        Total USD value of bid and ask sides up to depth_levels.

        Returns (bid_usd, ask_usd).
        """
        bids = book.bids(depth_levels)
        asks = book.asks(depth_levels)

        bid_usd = sum(float(lvl.price) * float(lvl.size) for lvl in bids)
        ask_usd = sum(float(lvl.price) * float(lvl.size) for lvl in asks)

        return bid_usd, ask_usd
