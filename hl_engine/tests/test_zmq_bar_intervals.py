from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId

from hl_engine.adapters.zmq.data_client import _bar_type_to_interval, _candle_open_ts_ns
from hl_engine.orchestrator.data_feed import _default_candle_intervals
from hl_engine.transport.serialization import unwrap, wrap_candle


def _bar_type(interval_minutes: int) -> BarType:
    return BarType(
        instrument_id=InstrumentId.from_str("BTC-USD.HYPERLIQUID"),
        bar_spec=BarSpecification(
            step=interval_minutes,
            aggregation=BarAggregation.MINUTE,
            price_type=PriceType.LAST,
        ),
        aggregation_source=AggregationSource.EXTERNAL,
    )


def test_wrap_candle_uses_payload_interval_in_topic():
    topic, payload = wrap_candle(
        1,
        "BTC",
        {"s": "BTC", "i": "15m", "o": "1", "h": "2", "l": "1", "c": "2", "v": "3"},
    )

    _, _, _, data = unwrap(payload)

    assert topic == b"bar.BTC-USD.HYPERLIQUID.15m"
    assert data["i"] == "15m"


def test_zmq_bar_type_to_interval_supports_15m_warmup_and_live_subscribe():
    assert _bar_type_to_interval(_bar_type(15)) == "15m"


def test_default_candle_intervals_include_live_and_trend_sources(monkeypatch):
    monkeypatch.delenv("HL_CANDLE_INTERVALS", raising=False)

    assert _default_candle_intervals() == ["1m", "15m"]


def test_default_candle_intervals_can_be_overridden(monkeypatch):
    monkeypatch.setenv("HL_CANDLE_INTERVALS", "1m,15m,15m")

    assert _default_candle_intervals() == ["1m", "15m"]


def test_bar_ts_init_uses_candle_open_for_warmup_detection():
    assert _candle_open_ts_ns({"t": 1_700_000_000_000}, fallback_ts_ns=99) == 1_700_000_000_000_000_000
    assert _candle_open_ts_ns({}, fallback_ts_ns=99) == 99
