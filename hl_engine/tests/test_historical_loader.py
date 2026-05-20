import pytest

from hl_engine.data.historical_loader import _post_json_with_retry


class FakeResponse:
    def __init__(self, status: int, payload: object = None, headers: dict[str, str] | None = None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = responses
        self.calls = 0

    def post(self, url, json):
        response = self._responses[self.calls]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_post_json_with_retry_retries_429_then_returns_json(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("hl_engine.data.historical_loader.asyncio.sleep", fake_sleep)

    session = FakeSession(
        [
            FakeResponse(429, {"detail": "rate limited"}),
            FakeResponse(200, {"ok": True}),
        ]
    )

    result = await _post_json_with_retry(
        session,
        "https://api.hyperliquid.xyz/info",
        {"type": "metaAndAssetCtxs"},
        request_label="instrument metadata",
    )

    assert result == {"ok": True}
    assert session.calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_post_json_with_retry_honors_retry_after_header(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("hl_engine.data.historical_loader.asyncio.sleep", fake_sleep)

    session = FakeSession(
        [
            FakeResponse(429, {"detail": "rate limited"}, headers={"Retry-After": "3"}),
            FakeResponse(200, {"ok": True}),
        ]
    )

    result = await _post_json_with_retry(
        session,
        "https://api.hyperliquid.xyz/info",
        {"type": "fundingHistory"},
        request_label="funding history",
    )

    assert result == {"ok": True}
    assert session.calls == 2
    assert sleeps == [3.0]
