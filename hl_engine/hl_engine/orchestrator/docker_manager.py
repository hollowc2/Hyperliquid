"""
DockerManager — starts/stops strategy containers via the Docker daemon.

Requires /var/run/docker.sock to be mounted into the orchestrator container.
Each container launch generates a unique instance_id (UUID) so the orchestrator
can detect restarts vs new starts via the /register endpoint.
"""

import logging
import os
import uuid
from typing import Optional

import docker

from hl_engine.orchestrator.strategy_registry import StrategySpec

log = logging.getLogger(__name__)


class DockerManager:
    """
    Manages strategy container lifecycle via docker-py.

    Parameters
    ----------
    network_name : str
        Docker network name for strategy containers (e.g. "hl-net").
    strategies_host_path : str
        Host-side path to the strategies/ directory (mounted read-only).
    orchestrator_host : str
        Hostname or IP that strategy containers use to reach the orchestrator.
    """

    def __init__(
        self,
        network_name: str = "hl-net",
        strategies_host_path: str = "./strategies",
        orchestrator_host: str = "orchestrator",
    ) -> None:
        self._network = network_name
        self._strategies_host_path = os.path.abspath(strategies_host_path)
        self._orchestrator_host = orchestrator_host

        try:
            self._client = docker.from_env()
            log.info("DockerManager initialised (docker socket connected)")
        except docker.errors.DockerException as e:
            log.error(f"DockerManager: cannot connect to Docker daemon: {e}")
            self._client = None

        # container_name → {instance_id, container_id, start_time}
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    def start_strategy(self, spec: StrategySpec) -> Optional[str]:
        """
        Start a strategy container.

        Returns the Docker container ID on success, None on failure.
        """
        if not self._client:
            log.error("DockerManager: Docker client not available")
            return None

        instance_id = str(uuid.uuid4())
        env = {
            "STRATEGY_CONFIG": f"/strategies/{spec.id}.yml",
            "ORCHESTRATOR_REST_URL": f"http://{self._orchestrator_host}:8000",
            "ORCHESTRATOR_ZMQ_DATA": f"tcp://{self._orchestrator_host}:5555",
            "ORCHESTRATOR_ZMQ_FILLS": f"tcp://{self._orchestrator_host}:5556",
            "STRATEGY_INSTANCE_ID": instance_id,
            # Pass through read-only env vars (no private key — orders go through orchestrator)
            "HL_PAPER_TRADE": os.getenv("HL_PAPER_TRADE", "true"),
            "PYTHONUNBUFFERED": "1",
        }

        try:
            # Stop existing container with the same name if running
            self._stop_if_exists(spec.docker.container_name)

            container = self._client.containers.run(
                image=spec.docker.image,
                name=spec.docker.container_name,
                command=["python", "hl_engine/run_strategy.py"],
                environment=env,
                volumes={
                    self._strategies_host_path: {"bind": "/strategies", "mode": "ro"},
                },
                network=self._network,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
            )
            container_id = container.id[:12]
            self._state[spec.docker.container_name] = {
                "instance_id": instance_id,
                "container_id": container.id,
                "container_id_short": container_id,
                "strategy_id": spec.id,
                "start_time": container.attrs.get("State", {}).get("StartedAt", ""),
            }
            log.info(f"Started container {spec.docker.container_name} [{container_id}] instance={instance_id[:8]}")
            return container.id
        except docker.errors.DockerException as e:
            log.error(f"Failed to start container {spec.docker.container_name}: {e}")
            return None

    def stop_strategy(self, container_name: str, timeout: int = 10) -> bool:
        """Stop a strategy container. Returns True on success."""
        if not self._client:
            return False
        try:
            container = self._client.containers.get(container_name)
            container.stop(timeout=timeout)
            self._state.pop(container_name, None)
            log.info(f"Stopped container {container_name}")
            return True
        except docker.errors.NotFound:
            log.warning(f"Container {container_name} not found")
            self._state.pop(container_name, None)
            return False
        except docker.errors.DockerException as e:
            log.error(f"Error stopping {container_name}: {e}")
            return False

    def get_status(self, container_name: str) -> str:
        """Return container status string ('running', 'exited', 'not_found', etc.)."""
        if not self._client:
            return "docker_unavailable"
        try:
            container = self._client.containers.get(container_name)
            container.reload()
            return container.status
        except docker.errors.NotFound:
            return "not_found"
        except docker.errors.DockerException:
            return "unknown"

    def get_instance_id(self, container_name: str) -> Optional[str]:
        return self._state.get(container_name, {}).get("instance_id")

    def list_running(self) -> list[dict]:
        """Return list of known strategy container states."""
        result = []
        for container_name, state in self._state.items():
            result.append({
                "container_name": container_name,
                "strategy_id": state.get("strategy_id"),
                "status": self.get_status(container_name),
                "instance_id": state.get("instance_id"),
                "container_id": state.get("container_id_short"),
            })
        return result

    def stream_logs(self, container_name: str, tail: int = 100):
        """Generator yielding log lines from a container."""
        if not self._client:
            return
        try:
            container = self._client.containers.get(container_name)
            for line in container.logs(stream=True, tail=tail, follow=True):
                yield line.decode("utf-8", errors="replace").rstrip()
        except docker.errors.NotFound:
            yield f"Container {container_name} not found"
        except docker.errors.DockerException as e:
            yield f"Error streaming logs: {e}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stop_if_exists(self, container_name: str) -> None:
        if not self._client:
            return
        try:
            existing = self._client.containers.get(container_name)
            existing.stop(timeout=5)
            existing.remove(force=True)
            log.info(f"Removed existing container {container_name}")
        except docker.errors.NotFound:
            pass
