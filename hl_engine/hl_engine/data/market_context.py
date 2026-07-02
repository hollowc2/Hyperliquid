"""
Reusable market context models for public/free data sources.

These types intentionally avoid NautilusTrader dependencies so they can be
used by adapters, strategies, ranking jobs, and tests without engine runtime
objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    size: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class TopOfBookSnapshot:
    venue: str
    symbol: str
    bid_price: float | None = None
    bid_size: float | None = None
    ask_price: float | None = None
    ask_size: float | None = None
    bid_depth_usd: float = 0.0
    ask_depth_usd: float = 0.0
    ts_ms: int | None = None

    @property
    def mid_price(self) -> float | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_bps(self) -> float | None:
        mid = self.mid_price
        if mid is None or mid <= 0.0:
            return None
        return (self.ask_price - self.bid_price) / mid * 10_000.0

    @property
    def depth_usd(self) -> float:
        return self.bid_depth_usd + self.ask_depth_usd


@dataclass(frozen=True, slots=True)
class FundingSnapshot:
    venue: str
    symbol: str
    rate: float
    next_funding_time_ms: int | None = None
    ts_ms: int | None = None


@dataclass(frozen=True, slots=True)
class OpenInterestSnapshot:
    venue: str
    symbol: str
    contracts: float
    notional_usd: float
    ts_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    venue: str
    symbol: str
    side: str
    size: float
    price: float
    notional_usd: float
    ts_ms: int | None = None


@dataclass(frozen=True, slots=True)
class MarketAssetContext:
    venue: str
    symbol: str
    mark_price: float | None = None
    oracle_price: float | None = None
    previous_day_price: float | None = None
    day_notional_volume_usd: float = 0.0
    funding: FundingSnapshot | None = None
    open_interest: OpenInterestSnapshot | None = None
    top_of_book: TopOfBookSnapshot | None = None
    raw: dict = field(default_factory=dict)

    @property
    def day_return(self) -> float | None:
        if (
            self.mark_price is None
            or self.previous_day_price is None
            or self.previous_day_price <= 0.0
        ):
            return None
        return self.mark_price / self.previous_day_price - 1.0

    @property
    def liquidity_usd(self) -> float:
        book_depth = self.top_of_book.depth_usd if self.top_of_book else 0.0
        return self.day_notional_volume_usd + book_depth


@dataclass(frozen=True, slots=True)
class ExchangeTicker:
    venue: str
    symbol: str
    price: float | None = None
    day_return: float | None = None
    day_notional_volume_usd: float | None = None
    ts_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CrossExchangeRegime:
    base_symbol: str
    venue_prices: dict[str, float]
    primary_venue: str | None = None
    primary_basis_bps: float | None = None
    average_day_return: float | None = None
    dispersion_bps: float | None = None
    risk_on_score: float = 0.0


@dataclass(frozen=True, slots=True)
class EventCalendarFlag:
    name: str
    starts_at_ms: int
    ends_at_ms: int
    source: str = "configured"
    importance: str = "medium"

    def is_active(self, ts_ms: int) -> bool:
        return self.starts_at_ms <= ts_ms <= self.ends_at_ms


@dataclass(frozen=True, slots=True)
class UniverseAssetRank:
    symbol: str
    rank: int
    score: float
    day_notional_volume_usd: float
    open_interest_usd: float
    book_depth_usd: float
    spread_bps: float | None
    funding_rate: float | None


@dataclass(frozen=True, slots=True)
class MarketContextSnapshot:
    ts_ms: int
    assets: dict[str, MarketAssetContext]
    universe: list[UniverseAssetRank] = field(default_factory=list)
    regimes: dict[str, CrossExchangeRegime] = field(default_factory=dict)
    liquidations: list[LiquidationEvent] = field(default_factory=list)
    event_flags: list[EventCalendarFlag] = field(default_factory=list)


class MarketContextSource(Protocol):
    async def fetch_context(self, symbols: list[str] | None = None) -> MarketContextSnapshot:
        """Fetch a point-in-time context snapshot."""
