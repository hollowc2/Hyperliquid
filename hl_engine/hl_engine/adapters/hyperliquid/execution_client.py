"""
HyperliquidLiveExecutionClient — order submission, cancellation, and fill processing.

Uses hyperliquid-python-sdk's Exchange for EVM order signing.
WebSocket fills come via userFills channel on the data client's WS connection.
"""

import asyncio
import json
from decimal import Decimal
from typing import Optional

import websockets
from websockets.connection import State as WsState

from nautilus_trader.execution.messages import SubmitOrder, CancelOrder
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType, LiquiditySide, OmsType, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity

from hl_engine.adapters.hyperliquid.constants import (
    HYPERLIQUID_VENUE,
    HL_PING_INTERVAL_SECS,
    WS_TYPE_USER_FILLS,
    WS_TYPE_ORDER_UPDATES,
)


class HyperliquidLiveExecutionClient(LiveExecutionClient):
    """
    Execution client for Hyperliquid perpetual futures.

    - LIMIT orders → GTC limit via SDK exchange.order()
    - MARKET orders → IOC limit with 5% slippage buffer (HL has no true market order)
    - Fill events → generate_order_filled() from userFills WS channel
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus,
        cache,
        clock,
        instrument_provider,
        exchange,  # hyperliquid.exchange.Exchange instance
        ws_url: str,
        wallet_address: str,
        account_id: AccountId,
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
        self._exchange = exchange
        self._ws_url = ws_url
        self._wallet_address = wallet_address
        self._set_account_id(account_id)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

        # Map NautilusTrader client_order_id → Hyperliquid oid (int)
        self._order_id_map: dict[str, int] = {}
        # Reverse map: oid → client_order_id
        self._oid_to_client_id: dict[int, str] = {}

    # ------------------------------------------------------------------
    # NautilusTrader lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        self._ws = await websockets.connect(self._ws_url, ping_interval=None)
        self._ping_task = self.create_task(self._ping_loop())
        self._recv_task = self.create_task(self._recv_loop())

        # Subscribe to user-specific channels
        await self._subscribe({"type": WS_TYPE_USER_FILLS, "user": self._wallet_address})
        await self._subscribe({"type": WS_TYPE_ORDER_UPDATES, "user": self._wallet_address})

        # Fetch current open orders and generate initial account state
        await self._sync_open_orders()
        await self._generate_account_state()

        self._log.info(f"Hyperliquid execution client connected (wallet: {self._wallet_address})")

    async def _disconnect(self) -> None:
        if self._ping_task:
            self._ping_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {order.instrument_id}")
            return

        coin = instrument.raw_symbol.value
        is_buy = order.side.value == "BUY"  # OrderSide.BUY
        sz = float(order.quantity)

        try:
            if order.order_type == OrderType.LIMIT:
                limit_px = float(order.price)
                result = self._exchange.order(
                    name=coin,
                    is_buy=is_buy,
                    sz=sz,
                    limit_px=limit_px,
                    order_type={"limit": {"tif": "Gtc"}},
                )
            elif order.order_type == OrderType.MARKET:
                # Hyperliquid uses IOC limit with slippage buffer for market orders
                book = self._cache.order_book(order.instrument_id)
                if book is not None:
                    best_ask = float(book.best_ask_price()) if not is_buy else None
                    best_bid = float(book.best_bid_price()) if is_buy else None
                    ref_px = best_ask if is_buy else best_bid
                else:
                    ref_px = None

                if ref_px is None:
                    self._log.error("Cannot submit market order: no book price available")
                    return

                slippage = 0.05
                limit_px = ref_px * (1 + slippage) if is_buy else ref_px * (1 - slippage)
                limit_px = round(limit_px, instrument.price_precision)

                result = self._exchange.order(
                    name=coin,
                    is_buy=is_buy,
                    sz=sz,
                    limit_px=limit_px,
                    order_type={"limit": {"tif": "Ioc"}},
                )
            else:
                self._log.error(f"Unsupported order type: {order.order_type}")
                return

            # Parse response
            if result.get("status") == "ok":
                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses:
                    first = statuses[0]
                    resting = first.get("resting") or first.get("filled")
                    if resting:
                        oid = int(resting.get("oid", 0))
                        cl_ord_id = order.client_order_id.value
                        self._order_id_map[cl_ord_id] = oid
                        self._oid_to_client_id[oid] = cl_ord_id
                        self.generate_order_submitted(
                            strategy_id=command.strategy_id,
                            instrument_id=order.instrument_id,
                            client_order_id=order.client_order_id,
                            ts_event=self._clock.timestamp_ns(),
                        )
            else:
                error = result.get("response", {})
                self._log.error(f"Order submission failed: {error}")

        except Exception as e:
            self._log.error(f"Exception submitting order: {e}")

    async def _cancel_order(self, command: CancelOrder) -> None:
        cl_ord_id = command.client_order_id.value
        oid = self._order_id_map.get(cl_ord_id)
        if oid is None:
            self._log.warning(f"No HL oid for client_order_id: {cl_ord_id}")
            return

        instrument = self._cache.instrument(command.instrument_id)
        if instrument is None:
            return

        try:
            result = self._exchange.cancel(
                name=instrument.raw_symbol.value,
                oid=oid,
            )
            if result.get("status") == "ok":
                self.generate_order_canceled(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=command.client_order_id,
                    venue_order_id=VenueOrderId(str(oid)),
                    ts_event=self._clock.timestamp_ns(),
                )
        except Exception as e:
            self._log.error(f"Exception canceling order: {e}")

    # ------------------------------------------------------------------
    # WebSocket receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    channel = msg.get("channel", "")
                    if channel == WS_TYPE_USER_FILLS:
                        self._handle_user_fills(msg)
                    elif channel == WS_TYPE_ORDER_UPDATES:
                        self._handle_order_updates(msg)
                except Exception as e:
                    self._log.error(f"Exec WS error: {e}")
        except websockets.ConnectionClosed:
            self._log.warning("Execution WebSocket closed")

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(HL_PING_INTERVAL_SECS)
            if self._ws and self._ws.state is WsState.OPEN:
                await self._ws.send(json.dumps({"method": "ping"}))

    async def _subscribe(self, sub: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    # ------------------------------------------------------------------
    # Fill / order update handlers
    # ------------------------------------------------------------------

    def _handle_user_fills(self, msg: dict) -> None:
        fills = msg.get("data", [])
        if isinstance(fills, dict):
            fills = fills.get("fills", [])

        processed = 0
        for fill in fills:
            oid = fill.get("oid")
            coin = fill.get("coin", "")
            cl_ord_id_str = self._oid_to_client_id.get(int(oid)) if oid else None
            if cl_ord_id_str is None:
                continue

            client_order_id = ClientOrderId(cl_ord_id_str)
            order = self._cache.order(client_order_id)
            if order is None:
                continue

            instrument = self._cache.instrument(order.instrument_id)
            if instrument is None:
                continue

            fill_px = float(fill.get("px", 0.0))
            fill_sz = float(fill.get("sz", 0.0))
            fee = float(fill.get("fee", 0.0))
            is_buy = fill.get("side", "B") == "B"
            ts_event = int(fill.get("time", 0)) * 1_000_000

            from nautilus_trader.model.currencies import USDC

            self.generate_order_filled(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId(str(oid)),
                venue_position_id=None,
                trade_id=TradeId(fill.get("hash", "0x")[:16]),
                order_side=order.side,
                order_type=order.order_type,
                last_qty=Quantity(fill_sz, instrument.size_precision),
                last_px=Price(fill_px, instrument.price_precision),
                quote_currency=USDC,
                commission=Money(fee, USDC),
                liquidity_side=LiquiditySide.MAKER if fill.get("dir", "") == "Open Long" else LiquiditySide.TAKER,
                ts_event=ts_event,
            )
            processed += 1

        if processed:
            # Re-fetch real account value from HL so the monitor reflects current balance
            self.create_task(self._generate_account_state())

    def _handle_order_updates(self, msg: dict) -> None:
        updates = msg.get("data", [])
        for update in updates:
            status = update.get("status", "")
            if status == "canceled":
                oid = update.get("order", {}).get("oid")
                cl_ord_id_str = self._oid_to_client_id.get(int(oid)) if oid else None
                if cl_ord_id_str:
                    client_order_id = ClientOrderId(cl_ord_id_str)
                    order = self._cache.order(client_order_id)
                    if order:
                        self.generate_order_canceled(
                            strategy_id=order.strategy_id,
                            instrument_id=order.instrument_id,
                            client_order_id=client_order_id,
                            venue_order_id=VenueOrderId(str(oid)),
                            ts_event=self._clock.timestamp_ns(),
                        )

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------

    async def _sync_open_orders(self) -> None:
        """Fetch open orders from REST and populate local maps."""
        import aiohttp
        from hl_engine.adapters.hyperliquid.constants import HL_INFO_ENDPOINT

        info_url = self._ws_url.replace("wss://", "https://").replace("/ws", "") + HL_INFO_ENDPOINT
        try:
            async with aiohttp.ClientSession() as session:
                payload = {"type": "openOrders", "user": self._wallet_address}
                async with session.post(info_url, json=payload) as resp:
                    resp.raise_for_status()
                    orders = await resp.json()
            for o in orders:
                oid = int(o.get("oid", 0))
                # We can't reconstruct client_order_id from existing orders,
                # but at least we register them for potential future cancellation
                self._oid_to_client_id.setdefault(oid, f"hl_{oid}")
        except Exception as e:
            self._log.warning(f"Could not sync open orders: {e}")

    async def _generate_account_state(self) -> None:
        """Fetch account balances and generate initial AccountState event."""
        import aiohttp
        from hl_engine.adapters.hyperliquid.constants import HL_INFO_ENDPOINT
        from nautilus_trader.model.currencies import USDC
        from nautilus_trader.model.objects import AccountBalance

        info_url = self._ws_url.replace("wss://", "https://").replace("/ws", "") + HL_INFO_ENDPOINT
        try:
            async with aiohttp.ClientSession() as session:
                payload = {"type": "clearinghouseState", "user": self._wallet_address}
                async with session.post(info_url, json=payload) as resp:
                    resp.raise_for_status()
                    state = await resp.json()

            margin_summary = state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0.0))

            self.generate_account_state(
                balances=[
                    AccountBalance(
                        total=Money(account_value, USDC),
                        locked=Money(0.0, USDC),
                        free=Money(account_value, USDC),
                    )
                ],
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        except Exception as e:
            self._log.warning(f"Could not generate account state: {e}")


# Avoid circular import — import here
from nautilus_trader.model.identifiers import TradeId  # noqa: E402
