import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from hl_engine.adapters.zmq import execution_client
from hl_engine.adapters.zmq.execution_client import (
    ZmqRestExecClient,
    _build_order_status_report,
    _is_buy_side,
    _order_needs_submitted,
)


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


def test_order_side_detection_uses_enum_not_numeric_value():
    assert OrderSide.BUY.value == 1
    assert OrderSide.SELL.value == 2
    assert _is_buy_side(OrderSide.BUY) is True
    assert _is_buy_side(OrderSide.SELL) is False


def test_order_status_report_uses_cached_order_fields():
    client_order_id = ClientOrderId("C-001")
    order = SimpleNamespace(
        client_order_id=client_order_id,
        venue_order_id=None,
        instrument_id=InstrumentId.from_str("BTC-USD.HYPERLIQUID"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        status=OrderStatus.FILLED,
        quantity=Quantity.from_str("0.10"),
        filled_qty=Quantity.from_str("0.10"),
        has_price=True,
        price=Price.from_str("100000.0"),
        avg_px=None,
        ts_init=111,
        ts_last=222,
    )

    report = _build_order_status_report(
        account_id=AccountId("HYPERLIQUID-vclimax-btc"),
        order=order,
        client_order_id=client_order_id,
        venue_order_id=VenueOrderId("42"),
        ts_init=333,
        fallback_ts_ns=123_456_789,
    )

    assert report.client_order_id == client_order_id
    assert report.venue_order_id == VenueOrderId("42")
    assert report.order_status == OrderStatus.FILLED
    assert report.quantity == Quantity.from_str("0.10")
    assert report.filled_qty == Quantity.from_str("0.10")


def test_order_needs_submitted_only_for_unknown_or_initialized_orders():
    client_order_id = ClientOrderId("C-002")

    assert _order_needs_submitted(SimpleNamespace(order=lambda order_id: None), client_order_id)
    assert _order_needs_submitted(
        SimpleNamespace(order=lambda order_id: SimpleNamespace(status=OrderStatus.INITIALIZED)),
        client_order_id,
    )
    assert not _order_needs_submitted(
        SimpleNamespace(order=lambda order_id: SimpleNamespace(status=OrderStatus.SUBMITTED)),
        client_order_id,
    )
