"""
Strategy container entry point.

Reads STRATEGY_CONFIG env var pointing to a YAML file, dynamically imports
the strategy class, and runs it in a NautilusTrader TradingNode using
ZMQ data and REST exec clients.

Required environment variables:
  STRATEGY_CONFIG            Path to strategy YAML file
  ORCHESTRATOR_REST_URL      http://orchestrator:8000
  ORCHESTRATOR_ZMQ_DATA      tcp://orchestrator:5555
  ORCHESTRATOR_ZMQ_FILLS     tcp://orchestrator:5556
  STRATEGY_INSTANCE_ID       Unique UUID for this container run
  HL_PAPER_TRADE             true/false
"""

import importlib
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_strategy")


def _import_class(dotted_path: str):
    """Import a class from a dotted module path like 'hl_engine.strategy.ma_strategy.MaCrossStrategy'."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _build_strategy_config(config_class, parameters: dict, instrument_id: str):
    """Build a NautilusTrader StrategyConfig subclass from a dict of parameters."""
    # Add instrument_id if the config accepts it
    import inspect
    sig = inspect.signature(config_class.__init__)
    kwargs = dict(parameters)
    if "instrument_id" in sig.parameters:
        kwargs.setdefault("instrument_id", instrument_id)
    return config_class(**kwargs)


def main() -> None:
    load_dotenv(override=False)

    config_path = os.environ.get("STRATEGY_CONFIG")
    if not config_path:
        raise RuntimeError("STRATEGY_CONFIG environment variable not set")

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_file}")

    import yaml
    with open(config_file) as f:
        spec = yaml.safe_load(f)

    strategy_id = str(spec["id"])
    class_path = str(spec["class"])
    config_class_path = str(spec["config_class"])
    instrument_id = str(spec["instrument_id"])
    parameters = dict(spec.get("parameters", {}))

    log.info(f"Loading strategy: {strategy_id} ({class_path})")

    strategy_class = _import_class(class_path)
    config_class = _import_class(config_class_path)
    strategy_config = _build_strategy_config(config_class, parameters, instrument_id)

    # Read env config
    rest_url = os.getenv("ORCHESTRATOR_REST_URL", "http://localhost:8000")
    zmq_data_url = os.getenv("ORCHESTRATOR_ZMQ_DATA", "tcp://localhost:5555")
    zmq_fills_url = os.getenv("ORCHESTRATOR_ZMQ_FILLS", "tcp://localhost:5556")
    instance_id = os.getenv("STRATEGY_INSTANCE_ID", "default-instance")

    from hl_engine.adapters.zmq.factories import ZmqLiveDataClientFactory, ZmqRestExecClientFactory

    # Inject config via class variables (same pattern as run_live.py)
    ZmqLiveDataClientFactory._zmq_data_url = zmq_data_url
    ZmqLiveDataClientFactory._rest_url = rest_url
    ZmqRestExecClientFactory._strategy_id = strategy_id
    ZmqRestExecClientFactory._rest_url = rest_url
    ZmqRestExecClientFactory._zmq_fills_url = zmq_fills_url
    ZmqRestExecClientFactory._instance_id = instance_id

    from nautilus_trader.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig, RoutingConfig
    from nautilus_trader.live.node import TradingNode

    node_config = TradingNodeConfig(
        trader_id=f"STRATEGY-{strategy_id.upper()}-001",
        data_clients={
            "HYPERLIQUID": LiveDataClientConfig(routing=RoutingConfig(default=True))
        },
        exec_clients={
            "HYPERLIQUID": LiveExecClientConfig(routing=RoutingConfig(default=True))
        },
        exec_engine=LiveExecEngineConfig(reconciliation=False),
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("HYPERLIQUID", ZmqLiveDataClientFactory)
    node.add_exec_client_factory("HYPERLIQUID", ZmqRestExecClientFactory)

    strategy = strategy_class(config=strategy_config)
    node.trader.add_strategy(strategy)

    node.build()

    log.info(f"Strategy container started: {strategy_id} instance={instance_id[:8]}")

    try:
        node.run()
    except KeyboardInterrupt:
        log.info(f"Strategy container stopping: {strategy_id}")
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
