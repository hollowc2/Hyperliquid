"""
ZmqRestExecClient — NautilusTrader LiveExecutionClient that:
  - Submits orders to the orchestrator via REST (POST /orders)
  - Receives fill/cancel events from the orchestrator via ZMQ SUB (fills.{strategy_id})
  - Reconciles state on connect via GET /reconcile/{strategy_id}

Key safety properties:
  - Idempotent submission: client_order_id is sent; orchestrator deduplicates
  - Retry on 5xx only (never on 4xx — prevents double orders)
  - tenacity handles retry with exponential backoff
"""

import asyncio
import logging
from typing import Optional

import aiohttp
import zmq.asyncio
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from nautilus_trader.execution.messages import CancelOrder, SubmitOrder
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType, LiquiditySide, OmsType, OrderSide, OrderType
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity

from hl_engine.adapters.hyperliquid.constants import HYPERLIQUID_VENUE
from hl_engine.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
from hl_engine.transport.serialization import unwrap

log = logging.getLogger(__name__)


def _is_buy_side(side: OrderSide) -> bool:
    return side == OrderSide.BUY


def _is_5xx(exc: Exception) -> bool:
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status >= 500
    return False


class ZmqRestExecClient(LiveExecutionClient):
    """
    Execution client for strategy containers.

    Orders are routed through the orchestrator REST API.
    Fills arrive via ZMQ PUB socket subscribed to fills.{strategy_id}.

    Parameters
    ----------
    strategy_id : str
        Strategy ID (matches YAML config id).
    orchestrator_rest_url : str
        Orchestrator REST base URL.
    orchestrator_zmq_fills_url : str
        ZMQ PUB socket URL for fills.
    instance_id : str
        Unique instance ID for restart detection.
    account_id : AccountId
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus,
        cache,
        clock,
        instrument_provider: HyperliquidInstrumentProvider,
        strategy_id: str,
        orchestrator_rest_url: str,
        orchestrator_zmq_fills_url: str,
        instance_id: str,
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
        self._strategy_id_str = strategy_id
        self._rest_url = orchestrator_rest_url.rstrip("/")
        self._zmq_fills_url = orchestrator_zmq_fills_url
        self._instance_id = instance_id
        self._set_account_id(account_id)

        self._zmq_ctx: Optional[zmq.asyncio.Context] = None
        self._zmq_sock: Optional[zmq.asyncio.Socket] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._fill_task: Optional[asyncio.Task] = None
        self._register_task: Optional[asyncio.Task] = None
        self._register_interval_secs = 30.0

        # oid ↔ client_order_id maps
        self._order_id_map: dict[str, int] = {}       # client_order_id → oid
        self._oid_to_client_id: dict[int, str] = {}   # oid → client_order_id
        self._net_positions: dict[str, float] = {}

    # ------------------------------------------------------------------
    # NautilusTrader lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        self._http = aiohttp.ClientSession()

        # ZMQ SUB for fills
        self._zmq_ctx = zmq.asyncio.Context()
        self._zmq_sock = self._zmq_ctx.socket(zmq.SUB)
        self._zmq_sock.setsockopt(zmq.RCVHWM, 1000)
        self._zmq_sock.connect(self._zmq_fills_url)
        topic = f"fills.{self._strategy_id_str}".encode()
        self._zmq_sock.setsockopt(zmq.SUBSCRIBE, topic)
        self._log.info(f"ZMQ fills SUB connected to {self._zmq_fills_url}, topic={topic.decode()}")

        self._fill_task = self.create_task(self._fill_recv_loop())
        self._register_task = self.create_task(self._register_loop())

        # Reconcile open orders and account state
        await self._reconcile()

    async def _disconnect(self) -> None:
        if self._fill_task:
            self._fill_task.cancel()
        if self._register_task:
            self._register_task.cancel()
        if self._zmq_sock:
            self._zmq_sock.close()
        if self._zmq_ctx:
            self._zmq_ctx.term()
        if self._http:
            await self._http.close()
        self._log.info("ZMQ exec client disconnected")

    async def _register_loop(self) -> None:
        """Keep the orchestrator aware of this live strategy instance."""
        registered_once = False
        backoff = 1.0
        while True:
            try:
                async with self._http.post(
                    f"{self._rest_url}/strategies/{self._strategy_id_str}/register",
                    json={"instance_id": self._instance_id, "strategy_id": self._strategy_id_str},
                ) as resp:
                    resp.raise_for_status()
                if not registered_once:
                    self._log.info(
                        f"Registered with orchestrator: {self._strategy_id_str} instance={self._instance_id[:8]}"
                    )
                    registered_once = True
                backoff = self._register_interval_secs
                await asyncio.sleep(self._register_interval_secs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log.warning(f"Could not register with orchestrator: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._register_interval_secs)

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {order.instrument_id}")
            return

        is_buy = _is_buy_side(order.side)

        # Determine price (for market orders, use best book price + slippage hint)
        price = None
        if order.order_type == OrderType.LIMIT:
            price = float(order.price)
        elif order.order_type == OrderType.MARKET:
            # Pass reference price so orchestrator can compute slippage buffer
            book = self._cache.order_book(order.instrument_id)
            if book is not None:
                best = book.best_ask_price() if is_buy else book.best_bid_price()
                if best is not None:
                    price = float(best)

        order_payload = {
            "strategy_id": self._strategy_id_str,
            "client_order_id": order.client_order_id.value,
            "instrument_id": str(order.instrument_id),
            "side": "BUY" if is_buy else "SELL",
            "order_type": "MARKET" if order.order_type == OrderType.MARKET else "LIMIT",
            "quantity": float(order.quantity),
            "price": price,
            "time_in_force": order.time_in_force.name,
            "is_reduce": self._order_reduces_position(order.instrument_id, is_buy),
        }

        try:
            result = await self._submit_with_retry(order_payload)
        except Exception as e:
            self._log.error(f"Order submission failed after retries: {e}")
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        oid = result.get("oid")
        if oid:
            cl_id = order.client_order_id.value
            self._order_id_map[cl_id] = int(oid)
            self._oid_to_client_id[int(oid)] = cl_id
            self.generate_order_submitted(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                ts_event=self._clock.timestamp_ns(),
            )

    def _order_reduces_position(self, instrument_id, is_buy: bool) -> bool:
        """Return True when the order side opposes the cached net position."""
        net_position = self._net_positions.get(str(instrument_id), 0.0)
        if net_position != 0.0:
            return (net_position > 0.0) != is_buy

        positions = self._cache.positions_open(instrument_id=instrument_id)
        if not positions:
            return False

        position = positions[0]
        return (position.is_long and not is_buy) or (not position.is_long and is_buy)

    @retry(
        retry=retry_if_exception(_is_5xx),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=1.0),
        reraise=True,
    )
    async def _submit_with_retry(self, payload: dict) -> dict:
        async with self._http.post(
            f"{self._rest_url}/orders",
            json=payload,
        ) as resp:
            if resp.status == 429:
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=429,
                    message="Rate limit exceeded"
                )
            if resp.status == 422:
                body = await resp.json()
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=422,
                    message=str(body.get("detail", "Risk/validation rejection"))
                )
            resp.raise_for_status()  # raises on 5xx → triggers retry
            return await resp.json()

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def _cancel_order(self, command: CancelOrder) -> None:
        cl_id = command.client_order_id.value
        oid = self._order_id_map.get(cl_id)
        if oid is None:
            self._log.warning(f"No oid for client_order_id: {cl_id}")
            return

        instrument = self._cache.instrument(command.instrument_id)
        if instrument is None:
            return

        try:
            async with self._http.delete(
                f"{self._rest_url}/orders/{oid}",
                params={
                    "strategy_id": self._strategy_id_str,
                    "instrument_id": str(command.instrument_id),
                },
            ) as resp:
                resp.raise_for_status()
        except Exception as e:
            self._log.error(f"Cancel order failed: {e}")

    # ------------------------------------------------------------------
    # ZMQ fill receiver
    # ------------------------------------------------------------------

    async def _fill_recv_loop(self) -> None:
        while True:
            try:
                [_topic, frame] = await self._zmq_sock.recv_multipart()
                try:
                    _seq, _ts_ns, type_str, data = unwrap(frame)
                except ValueError:
                    continue

                if type_str == "fill":
                    self._process_fill(data)
                elif type_str == "order_cancel":
                    self._process_cancel(data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"Fill recv error: {e}")

    def _process_fill(self, data: dict) -> None:
        oid = int(data.get("oid", 0))
        client_order_id_str = data.get("client_order_id") or self._oid_to_client_id.get(oid)
        if not client_order_id_str:
            self._log.warning(f"Fill for unknown oid {oid}")
            return

        client_order_id = ClientOrderId(client_order_id_str)
        order = self._cache.order(client_order_id)
        if order is None:
            self._log.warning(f"Fill for unknown order {client_order_id_str}")
            return

        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            return

        fill_px = float(data.get("fill_px", 0.0))
        fill_sz = float(data.get("fill_sz", 0.0))
        fee = float(data.get("fee", 0.0))
        hash_ = data.get("hash", "0x0000000000000000")
        dir_ = data.get("dir", "")
        ts_event = int(data.get("ts_event_ns", 0))

        from nautilus_trader.model.currencies import USDC

        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=VenueOrderId(str(oid)),
            venue_position_id=None,
            trade_id=TradeId(hash_[:16]),
            order_side=order.side,
            order_type=order.order_type,
            last_qty=Quantity(fill_sz, instrument.size_precision),
            last_px=Price(fill_px, instrument.price_precision),
            quote_currency=USDC,
            commission=Money(fee, USDC),
            liquidity_side=LiquiditySide.MAKER if "Open" in dir_ else LiquiditySide.TAKER,
            ts_event=ts_event,
        )

        signed_qty = fill_sz if order.side == OrderSide.BUY else -fill_sz
        instrument_key = str(order.instrument_id)
        updated_qty = self._net_positions.get(instrument_key, 0.0) + signed_qty
        self._net_positions[instrument_key] = 0.0 if abs(updated_qty) < 1e-12 else updated_qty

    def _process_cancel(self, data: dict) -> None:
        oid = int(data.get("oid", 0))
        client_order_id_str = data.get("client_order_id") or self._oid_to_client_id.get(oid)
        if not client_order_id_str:
            return

        client_order_id = ClientOrderId(client_order_id_str)
        order = self._cache.order(client_order_id)
        if order is None:
            return

        self.generate_order_canceled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=VenueOrderId(str(oid)),
            ts_event=int(data.get("ts_ns", 0)),
        )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def _reconcile(self) -> None:
        """Sync open orders and account state from orchestrator on connect."""
        try:
            async with self._http.get(
                f"{self._rest_url}/reconcile/{self._strategy_id_str}"
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            # Restore oid→client_id maps for known orders
            oid_client_map = data.get("oid_client_map", {})
            for oid_str, client_order_id_str in oid_client_map.items():
                oid = int(oid_str)
                self._oid_to_client_id[oid] = client_order_id_str
                self._order_id_map[client_order_id_str] = oid

            # Generate account state from clearinghouseState
            account_state = data.get("account_state", {})
            self._restore_net_positions(account_state)
            await self._generate_account_state_from_data(account_state)

            self._log.info(
                f"Reconciled: {len(oid_client_map)} open orders restored"
            )
        except Exception as e:
            self._log.warning(f"Reconciliation failed (proceeding with empty state): {e}")
            await self._generate_account_state_from_data({})

    def _restore_net_positions(self, account_state: dict) -> None:
        for item in account_state.get("assetPositions", []):
            position = item.get("position", {})
            coin = position.get("coin")
            if not coin:
                continue
            instrument_id = f"{coin}-USD.HYPERLIQUID"
            self._net_positions[instrument_id] = float(position.get("szi", 0.0))

    async def _generate_account_state_from_data(self, state: dict) -> None:
        from nautilus_trader.model.currencies import USDC
        from nautilus_trader.model.objects import AccountBalance

        margin_summary = state.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 10_000.0))

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
