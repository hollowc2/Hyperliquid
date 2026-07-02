"""
Pure feature helpers for market context snapshots.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev

from hl_engine.data.market_context import (
    CrossExchangeRegime,
    EventCalendarFlag,
    ExchangeTicker,
    LiquidationEvent,
    MarketAssetContext,
    OrderBookLevel,
    TopOfBookSnapshot,
    UniverseAssetRank,
)


class MarketContextFeatures:
    """Stateless helpers for public market context data."""

    @staticmethod
    def top_of_book_from_levels(
        venue: str,
        symbol: str,
        bids: list[OrderBookLevel],
        asks: list[OrderBookLevel],
        depth_levels: int = 5,
        ts_ms: int | None = None,
    ) -> TopOfBookSnapshot:
        bid = bids[0] if bids else None
        ask = asks[0] if asks else None
        bid_depth = sum(level.notional_usd for level in bids[:depth_levels])
        ask_depth = sum(level.notional_usd for level in asks[:depth_levels])
        return TopOfBookSnapshot(
            venue=venue,
            symbol=symbol,
            bid_price=bid.price if bid else None,
            bid_size=bid.size if bid else None,
            ask_price=ask.price if ask else None,
            ask_size=ask.size if ask else None,
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
            ts_ms=ts_ms,
        )

    @staticmethod
    def rank_universe(
        assets: list[MarketAssetContext],
        min_day_volume_usd: float = 0.0,
        max_spread_bps: float | None = None,
    ) -> list[UniverseAssetRank]:
        """
        Rank assets by public liquidity signals.

        The score is intentionally simple and explainable: log 24h notional,
        log OI, and log sampled book depth, with a spread penalty when top of
        book is available.
        """
        rows: list[tuple[str, float, UniverseAssetRank]] = []
        for asset in assets:
            if asset.day_notional_volume_usd < min_day_volume_usd:
                continue

            spread_bps = asset.top_of_book.spread_bps if asset.top_of_book else None
            if max_spread_bps is not None and spread_bps is not None and spread_bps > max_spread_bps:
                continue

            oi_usd = asset.open_interest.notional_usd if asset.open_interest else 0.0
            book_depth = asset.top_of_book.depth_usd if asset.top_of_book else 0.0
            funding_rate = asset.funding.rate if asset.funding else None
            spread_penalty = max(spread_bps or 0.0, 0.0) / 10.0
            score = (
                math.log1p(max(asset.day_notional_volume_usd, 0.0))
                + 0.75 * math.log1p(max(oi_usd, 0.0))
                + 0.50 * math.log1p(max(book_depth, 0.0))
                - spread_penalty
            )
            rows.append(
                (
                    asset.symbol,
                    score,
                    UniverseAssetRank(
                        symbol=asset.symbol,
                        rank=0,
                        score=score,
                        day_notional_volume_usd=asset.day_notional_volume_usd,
                        open_interest_usd=oi_usd,
                        book_depth_usd=book_depth,
                        spread_bps=spread_bps,
                        funding_rate=funding_rate,
                    ),
                )
            )

        rows.sort(key=lambda row: (-row[1], row[0]))
        return [
            UniverseAssetRank(
                symbol=row.symbol,
                rank=idx,
                score=row.score,
                day_notional_volume_usd=row.day_notional_volume_usd,
                open_interest_usd=row.open_interest_usd,
                book_depth_usd=row.book_depth_usd,
                spread_bps=row.spread_bps,
                funding_rate=row.funding_rate,
            )
            for idx, (_, _, row) in enumerate(rows, start=1)
        ]

    @staticmethod
    def cross_exchange_regime(
        base_symbol: str,
        tickers: list[ExchangeTicker],
        primary_venue: str | None = "hyperliquid",
    ) -> CrossExchangeRegime:
        prices = {
            ticker.venue: ticker.price
            for ticker in tickers
            if ticker.price is not None and ticker.price > 0.0
        }
        returns = [
            ticker.day_return
            for ticker in tickers
            if ticker.day_return is not None and math.isfinite(ticker.day_return)
        ]

        basis_bps = None
        if primary_venue and primary_venue in prices and len(prices) > 1:
            others = [price for venue, price in prices.items() if venue != primary_venue]
            reference = mean(others)
            if reference > 0.0:
                basis_bps = (prices[primary_venue] / reference - 1.0) * 10_000.0

        dispersion_bps = None
        if len(prices) > 1:
            avg_price = mean(prices.values())
            if avg_price > 0.0:
                dispersion_bps = pstdev(prices.values()) / avg_price * 10_000.0

        avg_return = mean(returns) if returns else None
        risk_on_score = 0.0 if avg_return is None else max(min(avg_return * 100.0, 1.0), -1.0)
        if dispersion_bps is not None:
            risk_on_score -= min(dispersion_bps / 100.0, 0.5)

        return CrossExchangeRegime(
            base_symbol=base_symbol,
            venue_prices=prices,
            primary_venue=primary_venue,
            primary_basis_bps=basis_bps,
            average_day_return=avg_return,
            dispersion_bps=dispersion_bps,
            risk_on_score=risk_on_score,
        )

    @staticmethod
    def active_event_flags(flags: list[EventCalendarFlag], ts_ms: int) -> list[EventCalendarFlag]:
        return [flag for flag in flags if flag.is_active(ts_ms)]

    @staticmethod
    def liquidation_notional(events: list[LiquidationEvent], symbol: str | None = None) -> float:
        return sum(
            event.notional_usd
            for event in events
            if symbol is None or event.symbol == symbol
        )
