from dataclasses import replace

import pytest

from hl_engine.adapters.hyperliquid.market_context import (
    HyperliquidMarketContextClient,
    parse_l2_book,
    parse_meta_and_asset_contexts,
    parse_web_data2_liquidations,
)
from hl_engine.data.market_context import EventCalendarFlag, ExchangeTicker
from hl_engine.features.market_context_features import MarketContextFeatures


def test_parse_meta_and_asset_contexts_builds_funding_oi_and_returns():
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [
            {
                "markPx": "65000",
                "oraclePx": "65010",
                "prevDayPx": "62500",
                "dayNtlVlm": "1250000000",
                "funding": "0.00008",
                "openInterest": "12000",
            },
            {
                "markPx": "3500",
                "prevDayPx": "3550",
                "dayNtlVlm": "850000000",
                "funding": "-0.00002",
                "openInterest": "80000",
            },
        ],
    ]

    assets = parse_meta_and_asset_contexts(payload, ts_ms=1000)

    assert assets["BTC"].funding.rate == pytest.approx(0.00008)
    assert assets["BTC"].open_interest.notional_usd == pytest.approx(780_000_000)
    assert assets["BTC"].day_return == pytest.approx(0.04)
    assert assets["ETH"].funding.rate == pytest.approx(-0.00002)


def test_parse_l2_book_computes_spread_and_depth():
    book = parse_l2_book(
        "BTC",
        {
            "time": 1234,
            "levels": [
                [{"px": "100.0", "sz": "2.0"}, {"px": "99.5", "sz": "3.0"}],
                [{"px": "100.5", "sz": "1.5"}, {"px": "101.0", "sz": "4.0"}],
            ],
        },
        depth_levels=2,
    )

    assert book.bid_price == 100.0
    assert book.ask_price == 100.5
    assert book.spread_bps == pytest.approx(49.8753117207)
    assert book.depth_usd == pytest.approx(1053.25)
    assert book.ts_ms == 1234


def test_rank_universe_prefers_liquidity_and_penalizes_wide_spreads():
    assets = parse_meta_and_asset_contexts(
        [
            {"universe": [{"name": "BTC"}, {"name": "ALT"}]},
            [
                {
                    "markPx": "100",
                    "dayNtlVlm": "1000000",
                    "funding": "0",
                    "openInterest": "5000",
                },
                {
                    "markPx": "10",
                    "dayNtlVlm": "100000",
                    "funding": "0",
                    "openInterest": "100",
                },
            ],
        ]
    )
    assets["BTC"] = replace(
        assets["BTC"],
        top_of_book=parse_l2_book(
            "BTC",
            {"levels": [[{"px": "99.9", "sz": "20"}], [{"px": "100.1", "sz": "20"}]]},
        ),
    )

    ranks = MarketContextFeatures.rank_universe(list(assets.values()))

    assert [rank.symbol for rank in ranks] == ["BTC", "ALT"]
    assert ranks[0].rank == 1
    assert ranks[0].book_depth_usd > 0.0


def test_cross_exchange_regime_computes_basis_dispersion_and_score():
    regime = MarketContextFeatures.cross_exchange_regime(
        "BTC",
        [
            ExchangeTicker("hyperliquid", "BTC", price=101.0, day_return=0.02),
            ExchangeTicker("binance", "BTC", price=100.0, day_return=0.01),
            ExchangeTicker("coinbase", "BTC", price=99.0, day_return=0.015),
        ],
    )

    assert regime.primary_basis_bps == pytest.approx(150.7537688442)
    assert regime.average_day_return == pytest.approx(0.015)
    assert regime.dispersion_bps is not None
    assert regime.risk_on_score > 0.0


def test_event_flags_and_liquidation_parser():
    active = EventCalendarFlag("FOMC", starts_at_ms=100, ends_at_ms=200)
    inactive = EventCalendarFlag("CPI", starts_at_ms=300, ends_at_ms=400)

    assert MarketContextFeatures.active_event_flags([active, inactive], 150) == [active]

    events = parse_web_data2_liquidations(
        {
            "data": {
                "liquidations": [
                    {"coin": "BTC", "side": "B", "sz": "0.5", "px": "60000", "time": 123}
                ]
            }
        }
    )

    assert events[0].side == "LONG"
    assert events[0].notional_usd == pytest.approx(30_000)


@pytest.mark.asyncio
async def test_liquidation_hooks_receive_parsed_events():
    received = []
    client = HyperliquidMarketContextClient()
    client.add_liquidation_hook(received.append)

    events = await client.emit_liquidations_from_web_data2(
        {"data": {"liquidations": [{"coin": "ETH", "side": "S", "sz": "2", "px": "3000"}]}}
    )

    assert events == received
    assert received[0].side == "SHORT"
