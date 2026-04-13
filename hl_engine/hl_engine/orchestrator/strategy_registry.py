"""
StrategyRegistry — loads and validates strategy YAML config files.

YAML format:
  id: ma-cross-btc
  class: hl_engine.strategy.ma_strategy.MaCrossStrategy
  config_class: hl_engine.config.ma_config.MaCrossConfig
  instrument_id: BTC-USD.HYPERLIQUID
  parameters:
    fast_period: 10
    slow_period: 30
    bar_minutes: 1
  risk:
    max_position_usd: 1000
    max_leverage: 2.0
  rate_limit:
    max_orders_per_second: 2
  docker:
    image: hl-engine:latest
    container_name: strategy-ma-cross-btc
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_position_usd: float = 5000.0
    max_leverage: float = 3.0


@dataclass
class RateLimitConfig:
    max_orders_per_second: float = 2.0


@dataclass
class DockerConfig:
    image: str = "hl-engine:latest"
    container_name: str = ""


@dataclass
class StrategySpec:
    id: str
    class_path: str
    config_class_path: str
    instrument_id: str
    parameters: dict = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)


class StrategyRegistry:
    """Loads StrategySpec objects from a directory of YAML files."""

    def __init__(self, strategies_dir: str | Path) -> None:
        self._dir = Path(strategies_dir)
        self._specs: dict[str, StrategySpec] = {}

    def load(self) -> None:
        """Read all *.yml / *.yaml files in the strategies directory."""
        self._specs.clear()
        for path in sorted(self._dir.glob("*.yml")) + sorted(self._dir.glob("*.yaml")):
            try:
                spec = _parse_yaml(path)
                self._specs[spec.id] = spec
                log.info(f"Loaded strategy spec: {spec.id} ({path.name})")
            except Exception as e:
                log.error(f"Failed to load strategy config {path}: {e}")

        log.info(f"StrategyRegistry: {len(self._specs)} strategies loaded from {self._dir}")

    def get(self, strategy_id: str) -> Optional[StrategySpec]:
        return self._specs.get(strategy_id)

    def list_all(self) -> list[StrategySpec]:
        return list(self._specs.values())

    def ids(self) -> list[str]:
        return list(self._specs.keys())


def _parse_yaml(path: Path) -> StrategySpec:
    with open(path) as f:
        data = yaml.safe_load(f)

    spec_id = str(data["id"])
    class_path = str(data["class"])
    config_class_path = str(data["config_class"])
    instrument_id = str(data["instrument_id"])
    parameters = dict(data.get("parameters", {}))

    risk_raw = data.get("risk", {})
    risk = RiskConfig(
        max_position_usd=float(risk_raw.get("max_position_usd", 5000.0)),
        max_leverage=float(risk_raw.get("max_leverage", 3.0)),
    )

    rl_raw = data.get("rate_limit", {})
    rate_limit = RateLimitConfig(
        max_orders_per_second=float(rl_raw.get("max_orders_per_second", 2.0)),
    )

    docker_raw = data.get("docker", {})
    docker = DockerConfig(
        image=str(docker_raw.get("image", "hl-engine:latest")),
        container_name=str(docker_raw.get("container_name", f"strategy-{spec_id}")),
    )

    return StrategySpec(
        id=spec_id,
        class_path=class_path,
        config_class_path=config_class_path,
        instrument_id=instrument_id,
        parameters=parameters,
        risk=risk,
        rate_limit=rate_limit,
        docker=docker,
    )
