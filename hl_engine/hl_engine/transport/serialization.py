"""
ZMQ message serialization for the multi-strategy platform.

Orchestrator side: wrap_* functions build msgpack frames from raw WS data.
Strategy side: unwrap() extracts the envelope; handlers reconstruct NT objects.

Frame format (ZMQ multipart):
  Frame 0: topic bytes  e.g. b"orderbook.BTC-USD.HYPERLIQUID"
  Frame 1: msgpack({ "seq": int, "ts_ns": int, "type": str, "d": {...} })

Types:
  l2book        OrderBookDelta batch (orchestrator → strategy)
  trade         TradeTick
  candle        Bar
  asset_ctx     FundingRateData + OpenInterestData
  liquidation   LiquidationData
  fill          Fill/cancel event (orchestrator → strategy)
  heartbeat     Liveness pulse from orchestrator
"""

import time

import msgspec.msgpack


# ---------------------------------------------------------------------------
# Wrap helpers (orchestrator side — pure dicts, no NT imports)
# ---------------------------------------------------------------------------

def wrap_l2book(
    seq: int,
    coin: str,
    time_ms: int,
    levels: list,
    is_snapshot: bool,
) -> tuple[bytes, bytes]:
    """Package an L2 book message for ZMQ publish."""
    topic = f"orderbook.{coin}-USD.HYPERLIQUID".encode()
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": time.time_ns(),
        "type": "l2book",
        "d": {
            "coin": coin,
            "time": time_ms,
            "levels": levels,
            "is_snapshot": is_snapshot,
        },
    })
    return topic, payload


def wrap_trade(seq: int, coin: str, trade: dict) -> tuple[bytes, bytes]:
    """Package a single trade for ZMQ publish."""
    topic = f"trades.{coin}-USD.HYPERLIQUID".encode()
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": time.time_ns(),
        "type": "trade",
        "d": {
            "coin": coin,
            "side": trade.get("side", "B"),
            "px": trade.get("px", "0"),
            "sz": trade.get("sz", "0"),
            "time": trade.get("time", 0),
            "hash": trade.get("hash", "0x0000000000000000"),
        },
    })
    return topic, payload


def wrap_candle(seq: int, coin: str, candle: dict) -> tuple[bytes, bytes]:
    """Package a candle for ZMQ publish."""
    interval = str(candle.get("i", "1m"))
    topic = f"bar.{coin}-USD.HYPERLIQUID.{interval}".encode()
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": time.time_ns(),
        "type": "candle",
        "d": {
            "coin": coin,
            "s": candle.get("s", coin),
            "o": candle.get("o", "0"),
            "h": candle.get("h", "0"),
            "l": candle.get("l", "0"),
            "c": candle.get("c", "0"),
            "v": candle.get("v", "0"),
            "t": candle.get("t", 0),
            "T": candle.get("T", candle.get("t", 0)),
            "i": candle.get("i", "1m"),
        },
    })
    return topic, payload


def wrap_asset_ctx(seq: int, coin: str, ctx: dict) -> tuple[bytes, bytes]:
    """Package funding/OI data for ZMQ publish."""
    topic = f"funding.{coin}-USD.HYPERLIQUID".encode()
    ts_ns = time.time_ns()
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": ts_ns,
        "type": "asset_ctx",
        "d": {
            "coin": coin,
            "funding": float(ctx.get("funding", 0.0)),
            "nextFundingTime": int(ctx.get("nextFundingTime", 0)),
            "openInterest": float(ctx.get("openInterest", 0.0)),
            "markPx": float(ctx.get("markPx", 0.0)),
        },
    })
    return topic, payload


def wrap_liquidation(seq: int, coin: str, liq: dict, ts_ns: int) -> tuple[bytes, bytes]:
    """Package a liquidation event for ZMQ publish."""
    topic = f"liquidation.{coin}-USD.HYPERLIQUID".encode()
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": ts_ns,
        "type": "liquidation",
        "d": {
            "coin": coin,
            "side": liq.get("side", ""),
            "sz": float(liq.get("sz", 0.0)),
            "px": float(liq.get("px", 0.0)),
        },
    })
    return topic, payload


def wrap_heartbeat(seq: int) -> tuple[bytes, bytes]:
    """Package a heartbeat pulse."""
    topic = b"heartbeat"
    payload = msgspec.msgpack.encode({
        "seq": seq,
        "ts_ns": time.time_ns(),
        "type": "heartbeat",
        "d": {},
    })
    return topic, payload


def wrap_fill(strategy_id: str, fill_data: dict) -> tuple[bytes, bytes]:
    """Package a fill event for delivery to a specific strategy."""
    topic = f"fills.{strategy_id}".encode()
    payload = msgspec.msgpack.encode({
        "seq": 0,
        "ts_ns": time.time_ns(),
        "type": "fill",
        "d": fill_data,
    })
    return topic, payload


def wrap_order_cancel(strategy_id: str, cancel_data: dict) -> tuple[bytes, bytes]:
    """Package an order cancellation event."""
    topic = f"fills.{strategy_id}".encode()
    payload = msgspec.msgpack.encode({
        "seq": 0,
        "ts_ns": time.time_ns(),
        "type": "order_cancel",
        "d": cancel_data,
    })
    return topic, payload


# ---------------------------------------------------------------------------
# Unwrap helper (strategy side — returns raw dict for further processing)
# ---------------------------------------------------------------------------

def unwrap(frame: bytes) -> tuple[int, int, str, dict]:
    """
    Decode a ZMQ payload frame.

    Returns (seq, ts_ns, type_str, data_dict).
    Raises ValueError on malformed input.
    """
    try:
        msg: dict = msgspec.msgpack.decode(frame)
        return (
            int(msg["seq"]),
            int(msg["ts_ns"]),
            str(msg["type"]),
            dict(msg.get("d", {})),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Malformed ZMQ frame: {exc}") from exc
