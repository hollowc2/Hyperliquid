"""
Orchestrator entry point.

Startup sequence:
  1. PersistenceStore.init()          — WAL SQLite, create tables
  2. load_oid_mappings()              — restore FillDispatcher state
  3. load_risk_snapshots()            — last known notionals per strategy
  4. load_fills_since(snapshot_ts)    — replay fills since snapshot
  5. GlobalRiskManager.restore()      — exact notional recovery
  6. StrategyRegistry.load()          — parse YAML configs
  7. Start asyncio.TaskGroup:
       OrchestratorDataFeed.run()     — HL WS → ZMQ PUB (port 5555)
       FillDispatcher.run()           — HL fills WS → ZMQ PUB (port 5556)
       uvicorn.Server.serve()         — FastAPI REST API (port 8000)

Environment variables:
  HL_WALLET_ADDRESS           Hyperliquid wallet address
  HL_PRIVATE_KEY              Private key for order signing
  HL_TESTNET                  true/false
  HL_RECORD_COINS             Comma-separated coins to subscribe (e.g. BTC,ETH)
  GLOBAL_NOTIONAL_CEILING_USD Max combined notional across all strategies (default 50000)
  GLOBAL_MAX_OPS              Max orders/second globally (default 10)
  STRATEGIES_DIR              Path to strategy YAML directory (default ./strategies)
  DB_PATH                     SQLite database path (default data/orchestrator.db)
  ORCHESTRATOR_HOST           Bind host for REST/ZMQ (default 0.0.0.0)
"""

import asyncio
import logging
import os

import uvicorn
import zmq.asyncio

from hl_engine.orchestrator import app as app_module
from hl_engine.orchestrator.data_feed import OrchestratorDataFeed
from hl_engine.orchestrator.docker_manager import DockerManager
from hl_engine.orchestrator.fill_dispatcher import FillDispatcher
from hl_engine.orchestrator.global_risk import GlobalRiskManager
from hl_engine.orchestrator.order_gateway import HyperliquidOrderGateway
from hl_engine.orchestrator.paper_execution import PaperExecutionEngine
from hl_engine.orchestrator.persistence import PersistenceStore
from hl_engine.orchestrator.rate_limiter import RateLimiter
from hl_engine.orchestrator.strategy_registry import StrategyRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("orchestrator")


def _build_exchange(wallet_address: str, private_key: str, testnet: bool):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants as hl_constants

    eth_account = Account.from_key(private_key)
    base_url = hl_constants.TESTNET_API_URL if testnet else hl_constants.MAINNET_API_URL
    return Exchange(account=eth_account, base_url=base_url, account_address=wallet_address)


async def main() -> None:
    # ------------------------------------------------------------------
    # Config from environment
    # ------------------------------------------------------------------
    from dotenv import load_dotenv
    load_dotenv(override=False)

    wallet_address = os.environ.get("HL_WALLET_ADDRESS", "")
    private_key = os.environ.get("HL_PRIVATE_KEY", "")
    testnet = os.getenv("HL_TESTNET", "false").lower() == "true"
    coins_raw = os.getenv("HL_RECORD_COINS", "BTC")
    coins = [c.strip() for c in coins_raw.split(",") if c.strip()]
    global_ceiling = float(os.getenv("GLOBAL_NOTIONAL_CEILING_USD", "50000"))
    global_max_ops = float(os.getenv("GLOBAL_MAX_OPS", "10"))
    strategies_dir = os.getenv("STRATEGIES_DIR", "strategies")
    # STRATEGIES_HOST_PATH is the host-absolute path used by DockerManager when
    # mounting the strategies directory into strategy containers via the Docker socket.
    # Defaults to STRATEGIES_DIR so bare (non-Docker) setups work unchanged.
    strategies_host_path = os.getenv("STRATEGIES_HOST_PATH", strategies_dir)
    data_host_path = os.getenv("DATA_HOST_PATH", "data")
    db_path = os.getenv("DB_PATH", "data/orchestrator.db")
    bind_host = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
    network_name = os.getenv("DOCKER_NETWORK", "hl-net")

    from hl_engine.adapters.hyperliquid.constants import (
        HL_BASE_URL, HL_TESTNET_BASE_URL, HL_WS_URL, HL_TESTNET_WS_URL
    )
    base_url = HL_TESTNET_BASE_URL if testnet else HL_BASE_URL
    ws_url = HL_TESTNET_WS_URL if testnet else HL_WS_URL
    info_url = base_url + "/info"

    log.info(f"Orchestrator starting — coins={coins} ceiling=${global_ceiling:,.0f}")

    # ------------------------------------------------------------------
    # 1. Persistence
    # ------------------------------------------------------------------
    store = PersistenceStore(db_path)
    await store.init()

    # ------------------------------------------------------------------
    # 2-5. Recovery
    # ------------------------------------------------------------------
    oid_mappings = await store.load_oid_mappings()
    risk_snapshots = await store.load_risk_snapshots()

    fills_by_strategy: dict[str, list[dict]] = {}
    for strategy_id, snap in risk_snapshots.items():
        fills_by_strategy[strategy_id] = await store.load_fills_since(strategy_id, snap["ts"])

    risk_mgr = GlobalRiskManager(global_ceiling)
    risk_mgr.restore(risk_snapshots, fills_by_strategy)

    rate_lim = RateLimiter(global_max_ops)

    # ------------------------------------------------------------------
    # 6. Strategy registry
    # ------------------------------------------------------------------
    registry = StrategyRegistry(strategies_dir)
    registry.load()

    # ------------------------------------------------------------------
    # 7. Order gateway + Docker manager
    # ------------------------------------------------------------------
    docker_mgr = DockerManager(
        network_name=network_name,
        strategies_host_path=strategies_host_path,
        data_host_path=data_host_path,
        orchestrator_host="orchestrator",
    )

    paper_trade = os.getenv("HL_PAPER_TRADE", "true").lower() == "true"
    if paper_trade or not private_key:
        log.warning("PAPER TRADE mode or no private key — order gateway disabled (returning mock responses)")
        gateway = None
    else:
        exchange = _build_exchange(wallet_address, private_key, testnet)
        gateway = HyperliquidOrderGateway(exchange, base_url, wallet_address)

    # ------------------------------------------------------------------
    # 8. ZMQ sockets
    # ------------------------------------------------------------------
    zmq_ctx = zmq.asyncio.Context()

    data_pub = zmq_ctx.socket(zmq.PUB)
    data_pub.setsockopt(zmq.SNDHWM, 1000)
    data_pub.bind(f"tcp://{bind_host}:5555")

    fills_pub = zmq_ctx.socket(zmq.PUB)
    fills_pub.setsockopt(zmq.SNDHWM, 1000)
    fills_pub.bind(f"tcp://{bind_host}:5556")

    log.info("ZMQ sockets bound on ports 5555 (data) and 5556 (fills)")

    paper_exec = PaperExecutionEngine(
        persistence=store,
        risk_manager=risk_mgr,
        zmq_fills_pub=fills_pub,
    )
    await paper_exec.restore_from_db()

    # ------------------------------------------------------------------
    # 9. Data feed and fill dispatcher
    # ------------------------------------------------------------------
    feed = OrchestratorDataFeed(
        coins=coins,
        zmq_pub=data_pub,
        ws_url=ws_url,
        info_url=info_url,
        wallet_address=wallet_address or None,
    )

    if gateway and wallet_address:
        dispatcher = FillDispatcher(
            wallet_address=wallet_address,
            zmq_fills_pub=fills_pub,
            persistence=store,
            risk_manager=risk_mgr,
            ws_url=ws_url,
        )
        await dispatcher.restore_from_db()
        # Populate oid→strategy from loaded oid_mappings
        for oid, info in oid_mappings.items():
            dispatcher._oid_to_strategy[oid] = info["strategy_id"]
            dispatcher._oid_to_client_id[oid] = info["client_order_id"]
    else:
        dispatcher = None

    # ------------------------------------------------------------------
    # 10. Wire FastAPI app globals
    # ------------------------------------------------------------------
    app_module.persistence = store
    app_module.rate_limiter = rate_lim
    app_module.risk_manager = risk_mgr
    app_module.order_gateway = gateway
    app_module.fill_dispatcher = dispatcher
    app_module.paper_execution = paper_exec
    app_module.strategy_registry = registry
    app_module.docker_manager = docker_mgr
    app_module.data_feed = feed

    # ------------------------------------------------------------------
    # 11. Periodic risk snapshot task
    # ------------------------------------------------------------------
    async def _risk_snapshot_loop():
        while True:
            await asyncio.sleep(30)
            summary = risk_mgr.get_summary()
            for sid, info in summary["strategies"].items():
                store.save_risk_snapshot(sid, info["notional_usd"])

    # ------------------------------------------------------------------
    # 12. Run everything
    # ------------------------------------------------------------------
    uvicorn_config = uvicorn.Config(
        app=app_module.app,
        host=bind_host,
        port=int(os.getenv("ORCHESTRATOR_PORT", "8000")),
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    log.info("Orchestrator ready — starting tasks")

    tasks_to_run = [
        asyncio.create_task(feed.run(), name="data_feed"),
        asyncio.create_task(server.serve(), name="rest_api"),
        asyncio.create_task(_risk_snapshot_loop(), name="risk_snapshots"),
    ]
    if dispatcher:
        tasks_to_run.append(asyncio.create_task(dispatcher.run(), name="fill_dispatcher"))

    try:
        await asyncio.gather(*tasks_to_run)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Orchestrator shutting down…")
        for t in tasks_to_run:
            t.cancel()
    finally:
        data_pub.close()
        fills_pub.close()
        zmq_ctx.term()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
