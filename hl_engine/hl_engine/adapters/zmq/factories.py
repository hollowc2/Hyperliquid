"""
NautilusTrader client factories for ZMQ-based data and execution clients.
Config is injected via class variables before node.build() — same pattern
as HyperliquidLiveDataClientFactory.
"""

import asyncio
import os

from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory as LiveExecutionClientFactory
from nautilus_trader.model.identifiers import AccountId, ClientId

from hl_engine.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
from hl_engine.adapters.zmq.data_client import ZmqLiveDataClient
from hl_engine.adapters.zmq.execution_client import ZmqRestExecClient
from hl_engine.adapters.hyperliquid.constants import HL_BASE_URL


class ZmqLiveDataClientFactory(LiveDataClientFactory):
    """Factory for ZmqLiveDataClient. Config set via class vars before node.build()."""

    _zmq_data_url: str = ""
    _rest_url: str = ""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus,
        cache,
        clock,
    ) -> ZmqLiveDataClient:
        zmq_url = ZmqLiveDataClientFactory._zmq_data_url or os.getenv(
            "ORCHESTRATOR_ZMQ_DATA", "tcp://localhost:5555"
        )
        rest_url = ZmqLiveDataClientFactory._rest_url or os.getenv(
            "ORCHESTRATOR_REST_URL", "http://localhost:8000"
        )

        provider = HyperliquidInstrumentProvider(base_url=HL_BASE_URL)

        return ZmqLiveDataClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            zmq_data_url=zmq_url,
            orchestrator_rest_url=rest_url,
            config=config,
        )


class ZmqRestExecClientFactory(LiveExecutionClientFactory):
    """Factory for ZmqRestExecClient. Config set via class vars before node.build()."""

    _strategy_id: str = ""
    _rest_url: str = ""
    _zmq_fills_url: str = ""
    _instance_id: str = ""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus,
        cache,
        clock,
    ) -> ZmqRestExecClient:
        strategy_id = ZmqRestExecClientFactory._strategy_id or os.getenv("STRATEGY_ID", "unknown")
        rest_url = ZmqRestExecClientFactory._rest_url or os.getenv(
            "ORCHESTRATOR_REST_URL", "http://localhost:8000"
        )
        zmq_fills_url = ZmqRestExecClientFactory._zmq_fills_url or os.getenv(
            "ORCHESTRATOR_ZMQ_FILLS", "tcp://localhost:5556"
        )
        instance_id = ZmqRestExecClientFactory._instance_id or os.getenv(
            "STRATEGY_INSTANCE_ID", "default-instance"
        )

        provider = HyperliquidInstrumentProvider(base_url=HL_BASE_URL)
        account_id = AccountId(f"{name}-{strategy_id}")

        return ZmqRestExecClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            strategy_id=strategy_id,
            orchestrator_rest_url=rest_url,
            orchestrator_zmq_fills_url=zmq_fills_url,
            instance_id=instance_id,
            account_id=account_id,
            config=config,
        )
