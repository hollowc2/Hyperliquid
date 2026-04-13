"""
HyperliquidOrderGateway — wraps the Hyperliquid SDK Exchange for async order submission.

The HL SDK uses synchronous `requests` internally, so all calls are wrapped in
asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from hl_engine.adapters.hyperliquid.constants import HL_INFO_ENDPOINT

log = logging.getLogger(__name__)


class HyperliquidOrderGateway:
    """
    Async wrapper around the Hyperliquid Python SDK Exchange.

    Parameters
    ----------
    exchange : hyperliquid.exchange.Exchange
        Pre-built SDK exchange instance (handles EVM signing).
    base_url : str
        HL REST base URL for info queries.
    wallet_address : str
        Wallet address (for account state queries).
    """

    def __init__(self, exchange, base_url: str, wallet_address: str) -> None:
        self._exchange = exchange
        self._info_url = base_url.rstrip("/") + HL_INFO_ENDPOINT
        self._wallet_address = wallet_address
        # Serialize SDK calls (requests.Session is not thread-safe for concurrent calls)
        self._lock = asyncio.Lock()

    async def submit_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type_dict: dict,
    ) -> dict:
        """
        Submit an order via the SDK.

        Returns the raw SDK response dict.
        Raises on network/signing errors.
        """
        async with self._lock:
            result = await asyncio.to_thread(
                self._exchange.order,
                coin,
                is_buy,
                sz,
                limit_px,
                order_type_dict,
            )
        return result

    async def cancel_order(self, coin: str, oid: int) -> dict:
        """Cancel an order by oid."""
        async with self._lock:
            result = await asyncio.to_thread(
                self._exchange.cancel,
                coin,
                oid,
            )
        return result

    async def get_open_orders(self) -> list[dict]:
        """Fetch current open orders for the wallet."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._info_url,
                json={"type": "openOrders", "user": self._wallet_address},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_account_state(self) -> dict:
        """Fetch clearinghouse state (balances + positions)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._info_url,
                json={"type": "clearinghouseState", "user": self._wallet_address},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_l2_book(self, coin: str) -> dict:
        """Fetch a full L2 book snapshot from REST (for periodic refresh)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._info_url,
                json={"type": "l2Book", "coin": coin},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
