"""
HyperliquidPaperExecClient — simulates order fills from live market data.

No private key, no SDK, no signing. Fills are simulated at best bid/ask.
"""

import asyncio
import uuid
from decimal import Decimal

from nautilus_trader.execution.messages import CancelOrder, SubmitOrder
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AccountType, LiquiditySide, OmsType, OrderType
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Money, Price, Quantity

from hl_engine.adapters.hyperliquid.constants import HYPERLIQUID_VENUE

_PAPER_TAKER_FEE = 0.0005  # 0.05% taker fee


class HyperliquidPaperExecClient(LiveExecutionClient):
    """
    Paper trading execution client for Hyperliquid.

    Simulates immediate fills at best bid/ask from the live order book.
    Account state starts with `paper_balance_usdc` USDC.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus,
        cache,
        clock,
        instrument_provider,
        account_id: AccountId,
        paper_balance_usdc: float = 10_000.0,
        config=None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=HYPERLIQUID_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        self._set_account_id(account_id)
        self._paper_balance = paper_balance_usdc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        self._generate_paper_account_state()
        self._log.info(
            f"Paper trading active — balance: {self._paper_balance:.2f} USDC (no real orders)"
        )

    async def _disconnect(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Order handling
    # ------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {order.instrument_id}")
            return

        is_buy = order.side.value == "BUY"

        # Determine fill price
        fill_px = self._get_fill_price(order, instrument, is_buy)
        if fill_px is None:
            self._log.warning(
                f"[PAPER] No book price for {order.instrument_id} — order rejected"
            )
            return

        fill_qty = float(order.quantity)
        fee = fill_px * fill_qty * _PAPER_TAKER_FEE
        venue_order_id = VenueOrderId(f"PAPER-{uuid.uuid4().hex[:12].upper()}")

        self._log.info(
            f"[PAPER] {'BUY' if is_buy else 'SELL'} {fill_qty} @ {fill_px:.4f} "
            f"fee={fee:.4f} USDC  ({order.order_type.name})"
        )

        self.generate_order_submitted(
            strategy_id=command.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=TradeId(f"PAPER-{uuid.uuid4().hex[:16].upper()}"),
            order_side=order.side,
            order_type=order.order_type,
            last_qty=Quantity(fill_qty, instrument.size_precision),
            last_px=Price(fill_px, instrument.price_precision),
            quote_currency=USDC,
            commission=Money(fee, USDC),
            liquidity_side=LiquiditySide.TAKER,
            ts_event=self._clock.timestamp_ns(),
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        order = self._cache.order(command.client_order_id)
        if order is None:
            return
        self.generate_order_canceled(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=VenueOrderId(f"PAPER-CANCEL-{uuid.uuid4().hex[:8].upper()}"),
            ts_event=self._clock.timestamp_ns(),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_fill_price(self, order, instrument, is_buy: bool):
        """Return fill price from order book or limit price."""
        if order.order_type == OrderType.LIMIT:
            return float(order.price)

        # Market order: use best ask (buy) or best bid (sell)
        book = self._cache.order_book(order.instrument_id)
        if book is not None:
            try:
                px = float(book.best_ask_price()) if is_buy else float(book.best_bid_price())
                if px and px > 0:
                    return px
            except Exception:
                pass

        # Fallback: last trade price from cache
        last = self._cache.price(order.instrument_id, price_type=None)
        if last:
            return float(last)

        return None

    def _generate_paper_account_state(self) -> None:
        self.generate_account_state(
            balances=[
                AccountBalance(
                    total=Money(self._paper_balance, USDC),
                    locked=Money(0.0, USDC),
                    free=Money(self._paper_balance, USDC),
                )
            ],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )
