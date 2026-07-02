"""
Public market context adapter for Hyperliquid.

Uses free/public endpoints only:
  - Hyperliquid /info metaAndAssetCtxs, l2Book
  - Optional public Binance/Coinbase spot tickers for BTC/ETH regime context

Liquidation support is hook-based because Hyperliquid liquidation messages are
not exposed as a broad historical public REST source.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import aiohttp

from hl_engine.adapters.hyperliquid.constants import HL_BASE_URL, HL_INFO_ENDPOINT
from hl_engine.data.market_context import (
    CrossExchangeRegime,
    EventCalendarFlag,
    ExchangeTicker,
    FundingSnapshot,
    LiquidationEvent,
    MarketAssetContext,
    MarketContextSnapshot,
    OpenInterestSnapshot,
    OrderBookLevel,
    TopOfBookSnapshot,
)
from hl_engine.features.market_context_features import MarketContextFeatures

_HL_VENUE = "hyperliquid"
_BINANCE_BASE_URL = "https://api.binance.com"
_COINBASE_BASE_URL = "https://api.exchange.coinbase.com"

LiquidationHook = Callable[[LiquidationEvent], None | Awaitable[None]]


class HyperliquidMarketContextClient:
    """
    Fetches reusable point-in-time market context from public endpoints.

    Parameters
    ----------
    base_url : str
        Hyperliquid API base URL.
    session : aiohttp.ClientSession | None
        Optional caller-owned session for tests or shared connection pools.
    event_calendar : list[EventCalendarFlag] | None
        Configured key events. Public keyless event-calendar APIs are not
        reliable enough to hard-depend on; callers can source and pass flags.
    max_books : int
        Maximum symbols to sample with l2Book when symbols are not specified.
    """

    def __init__(
        self,
        base_url: str = HL_BASE_URL,
        session: aiohttp.ClientSession | None = None,
        event_calendar: list[EventCalendarFlag] | None = None,
        max_books: int = 30,
        request_timeout_secs: float = 10.0,
    ) -> None:
        self._info_url = base_url.rstrip("/") + HL_INFO_ENDPOINT
        self._session = session
        self._event_calendar = event_calendar or []
        self._max_books = max_books
        self._request_timeout_secs = request_timeout_secs
        self._liquidation_hooks: list[LiquidationHook] = []

    def add_liquidation_hook(self, hook: LiquidationHook) -> None:
        self._liquidation_hooks.append(hook)

    async def fetch_context(
        self,
        symbols: list[str] | None = None,
        include_books: bool = True,
        include_cross_exchange_regime: bool = True,
    ) -> MarketContextSnapshot:
        ts_ms = int(time.time() * 1000)
        async with _session_context(self._session, self._request_timeout_secs) as session:
            meta_ctxs = await self.fetch_meta_and_asset_contexts(session=session)
            assets = parse_meta_and_asset_contexts(meta_ctxs, ts_ms=ts_ms)

            selected_symbols = symbols or list(assets)
            if symbols is None:
                ranked = MarketContextFeatures.rank_universe(list(assets.values()))
                selected_symbols = [row.symbol for row in ranked[: self._max_books]]

            if include_books:
                await self._attach_books(session, assets, selected_symbols, ts_ms)

            universe = MarketContextFeatures.rank_universe(list(assets.values()))
            regimes = {}
            if include_cross_exchange_regime:
                regimes = await self._fetch_regimes(session, assets, ts_ms)

        active_flags = MarketContextFeatures.active_event_flags(self._event_calendar, ts_ms)
        return MarketContextSnapshot(
            ts_ms=ts_ms,
            assets=assets,
            universe=universe,
            regimes=regimes,
            event_flags=active_flags,
        )

    async def fetch_meta_and_asset_contexts(
        self,
        session: aiohttp.ClientSession | None = None,
    ) -> list:
        session = session or self._session
        if session is None:
            async with _session_context(None, self._request_timeout_secs) as owned:
                return await _post_json(owned, self._info_url, {"type": "metaAndAssetCtxs"})
        return await _post_json(session, self._info_url, {"type": "metaAndAssetCtxs"})

    async def fetch_l2_book(
        self,
        symbol: str,
        session: aiohttp.ClientSession | None = None,
    ) -> dict:
        session = session or self._session
        payload = {"type": "l2Book", "coin": symbol}
        if session is None:
            async with _session_context(None, self._request_timeout_secs) as owned:
                return await _post_json(owned, self._info_url, payload)
        return await _post_json(session, self._info_url, payload)

    async def emit_liquidations_from_web_data2(self, msg: dict) -> list[LiquidationEvent]:
        events = parse_web_data2_liquidations(msg)
        for event in events:
            for hook in self._liquidation_hooks:
                result = hook(event)
                if result is not None:
                    await result
        return events

    async def _attach_books(
        self,
        session: aiohttp.ClientSession,
        assets: dict[str, MarketAssetContext],
        symbols: list[str],
        ts_ms: int,
    ) -> None:
        tasks = [self.fetch_l2_book(symbol, session=session) for symbol in symbols if symbol in assets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for symbol, result in zip([s for s in symbols if s in assets], results, strict=False):
            if isinstance(result, Exception):
                continue
            book = parse_l2_book(symbol, result, ts_ms=ts_ms)
            asset = assets[symbol]
            assets[symbol] = MarketAssetContext(
                venue=asset.venue,
                symbol=asset.symbol,
                mark_price=asset.mark_price,
                oracle_price=asset.oracle_price,
                previous_day_price=asset.previous_day_price,
                day_notional_volume_usd=asset.day_notional_volume_usd,
                funding=asset.funding,
                open_interest=asset.open_interest,
                top_of_book=book,
                raw=asset.raw,
            )

    async def _fetch_regimes(
        self,
        session: aiohttp.ClientSession,
        assets: dict[str, MarketAssetContext],
        ts_ms: int,
    ) -> dict[str, CrossExchangeRegime]:
        regimes = {}
        for symbol in ("BTC", "ETH"):
            tickers: list[ExchangeTicker] = []
            asset = assets.get(symbol)
            if asset is not None:
                tickers.append(
                    ExchangeTicker(
                        venue=_HL_VENUE,
                        symbol=symbol,
                        price=asset.mark_price,
                        day_return=asset.day_return,
                        day_notional_volume_usd=asset.day_notional_volume_usd,
                        ts_ms=ts_ms,
                    )
                )
            external = await asyncio.gather(
                fetch_binance_ticker(session, symbol, ts_ms),
                fetch_coinbase_ticker(session, symbol, ts_ms),
                return_exceptions=True,
            )
            tickers.extend(t for t in external if isinstance(t, ExchangeTicker))
            regimes[symbol] = MarketContextFeatures.cross_exchange_regime(symbol, tickers)
        return regimes


def parse_meta_and_asset_contexts(data: list, ts_ms: int | None = None) -> dict[str, MarketAssetContext]:
    if not isinstance(data, list) or len(data) < 2:
        return {}

    universe = data[0].get("universe", [])
    asset_ctxs = data[1]
    assets = {}
    for idx, meta in enumerate(universe):
        symbol = meta.get("name")
        if not symbol:
            continue
        ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
        mark_price = _to_float(ctx.get("markPx"))
        open_interest = _to_float(ctx.get("openInterest"), 0.0)
        oi_usd = open_interest * mark_price if mark_price is not None else 0.0
        assets[symbol] = MarketAssetContext(
            venue=_HL_VENUE,
            symbol=symbol,
            mark_price=mark_price,
            oracle_price=_to_float(ctx.get("oraclePx")),
            previous_day_price=_to_float(ctx.get("prevDayPx")),
            day_notional_volume_usd=_to_float(ctx.get("dayNtlVlm"), 0.0) or 0.0,
            funding=FundingSnapshot(
                venue=_HL_VENUE,
                symbol=symbol,
                rate=_to_float(ctx.get("funding"), 0.0) or 0.0,
                next_funding_time_ms=_to_int(ctx.get("nextFundingTime")),
                ts_ms=ts_ms,
            ),
            open_interest=OpenInterestSnapshot(
                venue=_HL_VENUE,
                symbol=symbol,
                contracts=open_interest,
                notional_usd=oi_usd,
                ts_ms=ts_ms,
            ),
            raw={"meta": meta, "ctx": ctx},
        )
    return assets


def parse_l2_book(
    symbol: str,
    data: dict,
    depth_levels: int = 5,
    ts_ms: int | None = None,
) -> TopOfBookSnapshot:
    levels = data.get("levels", [[], []])
    bids = _parse_book_side(levels[0] if levels else [])
    asks = _parse_book_side(levels[1] if len(levels) > 1 else [])
    return MarketContextFeatures.top_of_book_from_levels(
        venue=_HL_VENUE,
        symbol=symbol,
        bids=bids,
        asks=asks,
        depth_levels=depth_levels,
        ts_ms=_to_int(data.get("time")) or ts_ms,
    )


def parse_web_data2_liquidations(msg: dict) -> list[LiquidationEvent]:
    data = msg.get("data", {})
    events = []
    for liq in data.get("liquidations", []):
        symbol = liq.get("coin") or liq.get("symbol")
        if not symbol:
            continue
        size = _to_float(liq.get("sz"), 0.0) or 0.0
        price = _to_float(liq.get("px"), 0.0) or 0.0
        side_raw = str(liq.get("side", "")).upper()
        side = "LONG" if side_raw in {"B", "BUY", "LONG"} else "SHORT"
        events.append(
            LiquidationEvent(
                venue=_HL_VENUE,
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                notional_usd=size * price,
                ts_ms=_to_int(liq.get("time")) or _to_int(data.get("time")),
            )
        )
    return events


async def fetch_binance_ticker(
    session: aiohttp.ClientSession,
    symbol: str,
    ts_ms: int | None = None,
) -> ExchangeTicker:
    url = f"{_BINANCE_BASE_URL}/api/v3/ticker/24hr"
    async with session.get(url, params={"symbol": f"{symbol}USDT"}) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return ExchangeTicker(
        venue="binance",
        symbol=symbol,
        price=_to_float(data.get("lastPrice")),
        day_return=(_to_float(data.get("priceChangePercent")) or 0.0) / 100.0,
        day_notional_volume_usd=_to_float(data.get("quoteVolume")),
        ts_ms=ts_ms,
    )


async def fetch_coinbase_ticker(
    session: aiohttp.ClientSession,
    symbol: str,
    ts_ms: int | None = None,
) -> ExchangeTicker:
    product = f"{symbol}-USD"
    ticker_url = f"{_COINBASE_BASE_URL}/products/{product}/ticker"
    stats_url = f"{_COINBASE_BASE_URL}/products/{product}/stats"
    async with session.get(ticker_url) as resp:
        resp.raise_for_status()
        ticker = await resp.json()
    async with session.get(stats_url) as resp:
        resp.raise_for_status()
        stats = await resp.json()

    open_px = _to_float(stats.get("open"))
    last_px = _to_float(ticker.get("price"))
    day_return = None
    if open_px is not None and open_px > 0.0 and last_px is not None:
        day_return = last_px / open_px - 1.0
    volume = _to_float(stats.get("volume"), 0.0) or 0.0
    return ExchangeTicker(
        venue="coinbase",
        symbol=symbol,
        price=last_px,
        day_return=day_return,
        day_notional_volume_usd=volume * last_px if last_px is not None else None,
        ts_ms=ts_ms,
    )


def _parse_book_level(level: dict) -> OrderBookLevel | None:
    price = _to_float(level.get("px"))
    size = _to_float(level.get("sz"))
    if price is None or size is None:
        return None
    return OrderBookLevel(price=price, size=size)


def _parse_book_side(levels: list[dict]) -> list[OrderBookLevel]:
    parsed = []
    for level in levels:
        book_level = _parse_book_level(level)
        if book_level is not None:
            parsed.append(book_level)
    return parsed


def _to_float(value, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


async def _post_json(session: aiohttp.ClientSession, url: str, payload: dict):
    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()
        return await resp.json()


class _session_context:
    def __init__(self, session: aiohttp.ClientSession | None, timeout_secs: float) -> None:
        self._session = session
        self._timeout_secs = timeout_secs
        self._owned: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)
        self._owned = aiohttp.ClientSession(timeout=timeout)
        return self._owned

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owned is not None:
            await self._owned.close()
