"""
NautilusTrader client factories for Hyperliquid data and execution clients.
"""

import asyncio

from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory as LiveExecutionClientFactory
from nautilus_trader.model.identifiers import AccountId, ClientId

from hl_engine.adapters.hyperliquid.data_client import HyperliquidLiveMarketDataClient
from hl_engine.adapters.hyperliquid.execution_client import HyperliquidLiveExecutionClient
from hl_engine.adapters.hyperliquid.paper_execution_client import HyperliquidPaperExecClient
from hl_engine.adapters.hyperliquid.providers import HyperliquidInstrumentProvider


class HyperliquidLiveDataClientFactory(LiveDataClientFactory):
    """Factory for HyperliquidLiveMarketDataClient."""

    _apex_config = None  # set by run_live.py before node.build()

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus,
        cache,
        clock,
    ) -> HyperliquidLiveMarketDataClient:
        apex_config = HyperliquidLiveDataClientFactory._apex_config
        hl_config = apex_config.hyperliquid if apex_config else None

        base_url = hl_config.base_url if hl_config else "https://api.hyperliquid.xyz"
        ws_url = hl_config.ws_url if hl_config else "wss://api.hyperliquid.xyz/ws"
        wallet_address = hl_config.wallet_address if hl_config else None

        provider = HyperliquidInstrumentProvider(
            base_url=base_url,
        )

        return HyperliquidLiveMarketDataClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            base_url=base_url,
            ws_url=ws_url,
            wallet_address=wallet_address,
            config=config,
        )


class HyperliquidLiveExecClientFactory(LiveExecutionClientFactory):
    """Factory for HyperliquidLiveExecutionClient."""

    _apex_config = None  # set by run_live.py before node.build()

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus,
        cache,
        clock,
    ) -> HyperliquidLiveExecutionClient:
        apex_config = HyperliquidLiveExecClientFactory._apex_config
        hl_config = apex_config.hyperliquid if apex_config else None

        base_url = hl_config.base_url if hl_config else "https://api.hyperliquid.xyz"
        ws_url = hl_config.ws_url if hl_config else "wss://api.hyperliquid.xyz/ws"
        wallet_address = hl_config.wallet_address if hl_config else ""
        private_key = hl_config.private_key if hl_config else ""

        if not private_key:
            raise RuntimeError(
                "HL_PRIVATE_KEY is not set. "
                "For paper trading set HL_PAPER_TRADE=true in your .env file."
            )

        # Build the Hyperliquid SDK Exchange (handles EVM signing)
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants as hl_constants
        from eth_account import Account

        eth_account = Account.from_key(private_key)
        sdk_base_url = (
            hl_constants.TESTNET_API_URL
            if (hl_config and hl_config.testnet)
            else hl_constants.MAINNET_API_URL
        )
        exchange = Exchange(
            account=eth_account,
            base_url=sdk_base_url,
            account_address=wallet_address,
        )

        provider = HyperliquidInstrumentProvider(
            base_url=base_url,
        )

        account_id = AccountId(f"{name}-{wallet_address[:8]}")

        return HyperliquidLiveExecutionClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            exchange=exchange,
            ws_url=ws_url,
            wallet_address=wallet_address,
            account_id=account_id,
            config=config,
        )


class HyperliquidPaperExecClientFactory(LiveExecutionClientFactory):
    """Factory for HyperliquidPaperExecClient (no private key required)."""

    _apex_config = None  # set by run_live.py before node.build()

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus,
        cache,
        clock,
    ) -> HyperliquidPaperExecClient:
        apex_config = HyperliquidPaperExecClientFactory._apex_config
        hl_config = apex_config.hyperliquid if apex_config else None
        base_url = hl_config.base_url if hl_config else "https://api.hyperliquid.xyz"

        provider = HyperliquidInstrumentProvider(base_url=base_url)
        account_id = AccountId(f"{name}-PAPER")

        return HyperliquidPaperExecClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            account_id=account_id,
            paper_balance_usdc=10_000.0,
            config=config,
        )
