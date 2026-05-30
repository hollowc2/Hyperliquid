"""
HyperliquidInstrumentProvider — loads CryptoPerpetual instruments from Hyperliquid REST API.

Hyperliquid is USDC-margined, so settlement_currency=USDC for all instruments.
"""

from decimal import Decimal
from typing import Optional

import aiohttp

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity

from hl_engine.adapters.hyperliquid.constants import HYPERLIQUID_VENUE, HL_INFO_ENDPOINT


class HyperliquidInstrumentProvider(InstrumentProvider):
    """
    Provides CryptoPerpetual instruments from Hyperliquid's metaAndAssetCtxs endpoint.

    Usage:
        provider = HyperliquidInstrumentProvider(base_url=..., clock=...)
        await provider.load_all_async()
    """

    def __init__(self, base_url: str, **kwargs) -> None:
        kwargs.pop("clock", None)  # removed from InstrumentProvider in 1.224.0
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")
        self._info_url = self._base_url + HL_INFO_ENDPOINT

    async def load_all_async(self, filters: Optional[dict] = None) -> None:
        """Fetch all perpetual instruments and register them with the cache."""
        async with aiohttp.ClientSession() as session:
            payload = {"type": "metaAndAssetCtxs"}
            async with session.post(self._info_url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()

        # data is [meta, assetCtxs]
        meta = data[0]
        asset_ctxs = data[1]

        universe = meta.get("universe", [])
        for idx, asset_meta in enumerate(universe):
            ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
            instrument = self._build_instrument(asset_meta, ctx)
            if instrument is not None:
                self.add(instrument)

    def _build_instrument(self, asset_meta: dict, ctx: dict) -> Optional[CryptoPerpetual]:
        """Convert Hyperliquid asset metadata into a NautilusTrader CryptoPerpetual."""
        try:
            name = asset_meta["name"]
            sz_decimals = int(asset_meta.get("szDecimals", 4))

            # Derive price precision from mark price in ctx
            mark_px_str = ctx.get("markPx", "0")
            price_precision = self._infer_price_precision(mark_px_str)

            # Size (quantity) precision from szDecimals
            size_precision = sz_decimals

            # Tick size = 10^(-price_precision), step size = 10^(-size_precision)
            tick_size = Decimal(10) ** -price_precision
            step_size = Decimal(10) ** -size_precision

            instrument_id = InstrumentId(
                symbol=Symbol(f"{name}-USD"),
                venue=HYPERLIQUID_VENUE,
            )

            return CryptoPerpetual(
                instrument_id=instrument_id,
                raw_symbol=Symbol(name),
                base_currency=self._get_or_make_currency(name),
                quote_currency=USDC,
                settlement_currency=USDC,
                is_inverse=False,
                price_precision=price_precision,
                size_precision=size_precision,
                price_increment=Price(tick_size, price_precision),
                size_increment=Quantity(step_size, size_precision),
                multiplier=Quantity(1, 0),
                lot_size=None,
                max_quantity=None,
                min_quantity=Quantity(step_size, size_precision),
                max_notional=None,
                min_notional=None,
                max_price=None,
                min_price=None,
                margin_init=Decimal("0.05"),
                margin_maint=Decimal("0.03"),
                maker_fee=Decimal("0.0002"),
                taker_fee=Decimal("0.0005"),
                ts_event=0,
                ts_init=0,
            )
        except Exception as e:
            self._log.warning(f"Failed to build instrument for {asset_meta}: {e}")
            return None

    @staticmethod
    def _infer_price_precision(px_str: str) -> int:
        """Infer decimal precision from a price string."""
        try:
            px_str = str(float(px_str))  # normalize scientific notation
            if "." in px_str:
                integer_part, decimal_part = px_str.split(".")
                decimal_part = decimal_part.rstrip("0")
                if decimal_part:
                    return len(decimal_part)
            return 2
        except (ValueError, TypeError):
            return 2

    @staticmethod
    def _get_or_make_currency(name: str):
        """Return a Currency for the base asset, falling back to a generic one."""
        from nautilus_trader.model.currencies import BTC, ETH, SOL
        _known = {"BTC": BTC, "ETH": ETH, "SOL": SOL}
        if name in _known:
            return _known[name]
        # Dynamically create a crypto currency
        from nautilus_trader.model.objects import Currency
        from nautilus_trader.model.enums import CurrencyType
        return Currency(
            code=name[:8],  # max 8 chars
            precision=8,
            iso4217=0,
            name=name,
            currency_type=CurrencyType.CRYPTO,
        )
