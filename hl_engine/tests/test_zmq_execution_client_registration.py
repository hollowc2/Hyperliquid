import asyncio

import aiohttp
import pytest

from hl_engine.adapters.zmq import execution_client
from hl_engine.adapters.zmq.execution_client import ZmqRestExecClient


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=self.status,
                message="error",
            )


class _FlakySession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._attempts = 0

    def post(self, url: str, json: dict):
        self.calls.append((url, json))
        self._attempts += 1
        if self._attempts == 1:
            raise aiohttp.ClientConnectionError("orchestrator unavailable")
        return _FakeResponse()


@pytest.mark.asyncio
async def test_register_loop_retries_and_refreshes(monkeypatch):
    client = ZmqRestExecClient.__new__(ZmqRestExecClient)
    client._http = _FlakySession()
    client._rest_url = "http://orchestrator:8000"
    client._strategy_id_str = "vclimax-btc"
    client._instance_id = "instance-1234"
    client._register_interval_secs = 30.0
    monkeypatch.setattr(ZmqRestExecClient, "_log", execution_client.log, raising=False)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        if len(sleep_calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(execution_client.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await client._register_loop()

    assert client._http.calls[0][0] == "http://orchestrator:8000/strategies/vclimax-btc/register"
    assert client._http.calls[0][1] == {
        "instance_id": "instance-1234",
        "strategy_id": "vclimax-btc",
    }
    assert sleep_calls == [1.0, 30.0]
