"""
FillDispatcher — receives fills from Hyperliquid userFills WS and routes them
to the appropriate strategy container via ZMQ fills PUB socket.

Maintains oid → strategy_id mapping (restored from SQLite on startup,
updated on each new order submission via register_oid()).
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets
from websockets.connection import State as WsState

import zmq.asyncio

from hl_engine.adapters.hyperliquid.constants import (
    HL_PING_INTERVAL_SECS,
    HL_WS_URL,
    WS_TYPE_ORDER_UPDATES,
    WS_TYPE_USER_FILLS,
)
from hl_engine.orchestrator.global_risk import GlobalRiskManager
from hl_engine.orchestrator.persistence import PersistenceStore
from hl_engine.transport.serialization import wrap_fill, wrap_order_cancel

log = logging.getLogger(__name__)


class FillDispatcher:
    """
    Connects to Hyperliquid userFills WebSocket and fans out fill events
    to strategy containers via ZMQ fills PUB socket.

    Parameters
    ----------
    wallet_address : str
        Wallet address for userFills subscription.
    zmq_fills_pub : zmq.asyncio.Socket
        Bound ZMQ PUB socket for fills (tcp://*:5556).
    persistence : PersistenceStore
        For persisting fills and restoring oid→strategy mapping on restart.
    risk_manager : GlobalRiskManager
        For updating notional after fills.
    ws_url : str
        Hyperliquid WebSocket URL.
    """

    def __init__(
        self,
        wallet_address: str,
        zmq_fills_pub: "zmq.asyncio.Socket",
        persistence: PersistenceStore,
        risk_manager: GlobalRiskManager,
        ws_url: str = HL_WS_URL,
    ) -> None:
        self._wallet_address = wallet_address
        self._zmq_fills_pub = zmq_fills_pub
        self._persistence = persistence
        self._risk_manager = risk_manager
        self._ws_url = ws_url

        # oid → strategy_id  (loaded from SQLite at startup, updated on new orders)
        self._oid_to_strategy: dict[int, str] = {}
        # oid → client_order_id  (for notional release on cancel)
        self._oid_to_client_id: dict[int, str] = {}
        # order notional reserved (for release on cancel/rejection)
        self._oid_to_notional: dict[int, float] = {}

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_oid(
        self,
        oid: int,
        strategy_id: str,
        client_order_id: str,
        notional: float,
    ) -> None:
        """Called by the order endpoint after successful submission."""
        self._oid_to_strategy[oid] = strategy_id
        self._oid_to_client_id[oid] = client_order_id
        self._oid_to_notional[oid] = notional

    async def restore_from_db(self) -> None:
        """Load oid→strategy mapping from SQLite (called on startup before run())."""
        mappings = await self._persistence.load_oid_mappings()
        for oid, info in mappings.items():
            self._oid_to_strategy[oid] = info["strategy_id"]
            self._oid_to_client_id[oid] = info["client_order_id"]
        log.info(f"FillDispatcher restored {len(mappings)} oid mappings from DB")

    async def run(self) -> None:
        """Start the fills WS loop. Runs until cancelled."""
        self._running = True
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._ws_loop(), name="fills_ws")
                tg.create_task(self._ping_loop(), name="fills_ping")
        except* (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self._ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    log.info("FillDispatcher connected to HL WS")
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            channel = msg.get("channel", "")
                            if channel == WS_TYPE_USER_FILLS:
                                await self._handle_user_fills(msg)
                            elif channel == WS_TYPE_ORDER_UPDATES:
                                await self._handle_order_updates(msg)
                        except Exception as e:
                            log.error(f"FillDispatcher WS error: {e}")
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning(f"FillDispatcher WS disconnected: {e}. Reconnecting in 5s…")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"FillDispatcher unexpected error: {e}. Reconnecting in 10s…")
                await asyncio.sleep(10)

    async def _subscribe(self, ws) -> None:
        for sub_type in (WS_TYPE_USER_FILLS, WS_TYPE_ORDER_UPDATES):
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": sub_type, "user": self._wallet_address},
            }))
        log.info(f"FillDispatcher subscribed (wallet {self._wallet_address[:8]}…)")

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HL_PING_INTERVAL_SECS)
            if self._ws and self._ws.state is WsState.OPEN:
                try:
                    await self._ws.send(json.dumps({"method": "ping"}))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Fill handling
    # ------------------------------------------------------------------

    async def _handle_user_fills(self, msg: dict) -> None:
        fills = msg.get("data", [])
        if isinstance(fills, dict):
            fills = fills.get("fills", [])

        for fill in fills:
            oid = fill.get("oid")
            if oid is None:
                continue
            oid = int(oid)

            strategy_id = self._oid_to_strategy.get(oid)
            if strategy_id is None:
                log.debug(f"FillDispatcher: unknown oid {oid} — not our order")
                continue

            fill_px = float(fill.get("px", 0.0))
            fill_sz = float(fill.get("sz", 0.0))
            fee = float(fill.get("fee", 0.0))
            is_buy = fill.get("side", "B") == "B"
            notional_delta = fill_px * fill_sz * (1 if is_buy else -1)
            ts_event_ns = int(fill.get("time", 0)) * 1_000_000
            hash_ = fill.get("hash", "0x0000000000000000")
            client_order_id = self._oid_to_client_id.get(oid, f"hl_{oid}")

            # Persist fill
            self._persistence.save_fill(
                oid=oid,
                strategy_id=strategy_id,
                fill_px=fill_px,
                fill_sz=fill_sz,
                fee=fee,
                hash_=hash_,
                notional_delta=abs(notional_delta),
                ts_event_ns=ts_event_ns,
            )
            self._persistence.mark_order_filled(client_order_id)

            # Update risk
            await self._risk_manager.record_fill(strategy_id, abs(notional_delta))

            # Publish to strategy
            fill_data = {
                "oid": oid,
                "client_order_id": client_order_id,
                "fill_px": fill_px,
                "fill_sz": fill_sz,
                "fee": fee,
                "hash": hash_,
                "dir": fill.get("dir", ""),
                "ts_event_ns": ts_event_ns,
            }
            topic, payload = wrap_fill(strategy_id, fill_data)
            await self._zmq_fills_pub.send_multipart([topic, payload])
            log.info(f"Fill dispatched: {strategy_id} oid={oid} px={fill_px} sz={fill_sz}")

    async def _handle_order_updates(self, msg: dict) -> None:
        updates = msg.get("data", [])
        for update in updates:
            status = update.get("status", "")
            if status != "canceled":
                continue
            oid = update.get("order", {}).get("oid")
            if oid is None:
                continue
            oid = int(oid)
            strategy_id = self._oid_to_strategy.get(oid)
            if strategy_id is None:
                continue

            client_order_id = self._oid_to_client_id.get(oid, f"hl_{oid}")
            notional = self._oid_to_notional.get(oid, 0.0)

            # Release reserved notional
            if notional > 0:
                await self._risk_manager.release_notional(strategy_id, notional)

            cancel_data = {
                "oid": oid,
                "client_order_id": client_order_id,
                "ts_ns": time.time_ns(),
            }
            topic, payload = wrap_order_cancel(strategy_id, cancel_data)
            await self._zmq_fills_pub.send_multipart([topic, payload])
            log.info(f"Cancel dispatched: {strategy_id} oid={oid}")
