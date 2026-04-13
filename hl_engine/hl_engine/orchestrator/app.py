"""
Orchestrator FastAPI application.

All state is injected via module-level globals (set by main.py before uvicorn starts).
This avoids circular imports and keeps the app thin.

Endpoints:
  POST   /orders                      Submit order (idempotency → rate limit → risk → HL)
  DELETE /orders/{oid}                Cancel order
  GET    /strategies                  List all strategy specs with container status
  POST   /strategies/{id}/start       Start a strategy container
  POST   /strategies/{id}/stop        Stop a strategy container
  POST   /strategies/{id}/register    Called by strategy container on connect
  GET    /reconcile/{strategy_id}     Open orders + account state for resync
  GET    /snapshot/{instrument_id}    Current L2 book snapshot
  GET    /risk                        Global risk summary
  GET    /account/{strategy_id}       HL clearinghouseState proxy
  GET    /bars                        Historical candles proxy
  GET    /health                      Liveness check
"""

import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)

app = FastAPI(title="HL Orchestrator", version="1.0.0")

# ---------------------------------------------------------------------------
# Module-level state (set by main.py)
# ---------------------------------------------------------------------------

persistence = None       # PersistenceStore
rate_limiter = None      # RateLimiter
risk_manager = None      # GlobalRiskManager
order_gateway = None     # HyperliquidOrderGateway
fill_dispatcher = None   # FillDispatcher
strategy_registry = None  # StrategyRegistry
docker_manager = None    # DockerManager
data_feed = None         # OrchestratorDataFeed

# {strategy_id: {instance_id, registered_at}}
_registered_strategies: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    strategy_id: str
    client_order_id: str
    instrument_id: str
    side: str          # BUY or SELL
    order_type: str    # MARKET or LIMIT
    quantity: float
    price: Optional[float] = None
    time_in_force: str = "IOC"
    is_reduce: bool = False


class RegisterRequest(BaseModel):
    instance_id: str
    strategy_id: str


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@app.post("/orders")
async def submit_order(req: OrderRequest):
    # 1. Idempotency check
    existing_oid = await persistence.check_order_idempotent(req.client_order_id)
    if existing_oid is not None:
        log.info(f"Idempotent order return: {req.client_order_id} → oid={existing_oid}")
        return {"status": "submitted", "oid": existing_oid, "client_order_id": req.client_order_id}

    # 2. Persist as PENDING before any external call
    persistence.save_order_pending(
        client_order_id=req.client_order_id,
        strategy_id=req.strategy_id,
        instrument_id=req.instrument_id,
        side=req.side,
        qty=req.quantity,
        price=req.price,
        order_type=req.order_type,
    )

    # 3. Rate limit check
    allowed, reason = rate_limiter.check_and_consume(req.strategy_id)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    # 4. Risk check
    coin = req.instrument_id.split("-")[0]
    # Estimate notional: use price for LIMIT, or 0 for MARKET (risk check uses qty * approx px)
    notional = (req.price or 0.0) * req.quantity
    allowed, reason = await risk_manager.check_order(req.strategy_id, notional, req.is_reduce)
    if not allowed:
        raise HTTPException(status_code=422, detail=reason)

    # 5. Build HL order params
    is_buy = req.side.upper() == "BUY"
    sz = req.quantity

    if req.order_type.upper() == "MARKET":
        # HL uses IOC limit with slippage buffer for market orders
        ref_px = req.price or 0.0
        if ref_px <= 0 and data_feed is not None:
            # Fall back to orchestrator's live L2 snapshot
            snap = data_feed.get_snapshot(coin)
            if snap:
                levels = snap.get("asks" if is_buy else "bids", [])
                if levels:
                    ref_px = float(levels[0][0])
        if ref_px <= 0:
            raise HTTPException(status_code=422, detail="Market orders require a reference price")
        slippage = 0.05
        limit_px = round(ref_px * (1 + slippage) if is_buy else ref_px * (1 - slippage), 6)
        order_type_dict = {"limit": {"tif": "Ioc"}}
    elif req.order_type.upper() == "LIMIT":
        if req.price is None:
            raise HTTPException(status_code=422, detail="LIMIT orders require a price")
        limit_px = req.price
        tif = "Gtc" if req.time_in_force.upper() in ("GTC", "GTT") else "Ioc"
        order_type_dict = {"limit": {"tif": tif}}
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported order type: {req.order_type}")

    # 6. Submit to Hyperliquid (or mock in paper-trade mode)
    if order_gateway is None:
        # Paper-trade / no private key: return a synthetic oid
        import random
        oid = random.randint(100_000, 999_999)
        log.info(f"[PAPER] Mock order accepted: {req.strategy_id} {req.side} {sz} {coin} @ {limit_px} → mock_oid={oid}")
    else:
        try:
            result = await order_gateway.submit_order(coin, is_buy, sz, limit_px, order_type_dict)
        except Exception as e:
            rate_limiter.record_hl_rejection(req.strategy_id)
            raise HTTPException(status_code=502, detail=f"HL submission error: {e}")

        if result.get("status") != "ok":
            rate_limiter.record_hl_rejection(req.strategy_id)
            err = result.get("response", {})
            raise HTTPException(status_code=422, detail=f"HL rejected order: {err}")

        # 7. Extract oid
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        oid = None
        if statuses:
            first = statuses[0]
            resting = first.get("resting") or first.get("filled")
            if resting:
                oid = int(resting.get("oid", 0))

        if oid is None:
            rate_limiter.record_hl_rejection(req.strategy_id)
            raise HTTPException(status_code=502, detail="No oid in HL response")

    # 8. Record success
    rate_limiter.record_hl_success(req.strategy_id)
    persistence.mark_order_submitted(req.client_order_id, oid)
    persistence.save_oid_mapping(oid, req.strategy_id, req.client_order_id)
    if fill_dispatcher is not None:
        fill_dispatcher.register_oid(oid, req.strategy_id, req.client_order_id, notional)
    await risk_manager.reserve_notional(req.strategy_id, notional)

    log.info(f"Order submitted: {req.strategy_id} {req.side} {sz} {coin} oid={oid}")
    return {"status": "submitted", "oid": oid, "client_order_id": req.client_order_id}


@app.delete("/orders/{oid}")
async def cancel_order(oid: int, strategy_id: str = "", instrument_id: str = ""):
    coin = instrument_id.split("-")[0] if instrument_id else ""
    if not coin:
        raise HTTPException(status_code=422, detail="instrument_id required for cancel")
    if order_gateway is None:
        log.info(f"[PAPER] Mock cancel: oid={oid} {strategy_id}")
        return {"status": "cancelled", "oid": oid}
    try:
        result = await order_gateway.cancel_order(coin, oid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if result.get("status") != "ok":
        raise HTTPException(status_code=422, detail=str(result))
    return {"status": "canceled", "oid": oid}


# ---------------------------------------------------------------------------
# Strategy management
# ---------------------------------------------------------------------------

@app.get("/strategies")
async def list_strategies():
    specs = strategy_registry.list_all() if strategy_registry else []
    running = {s["strategy_id"]: s for s in (docker_manager.list_running() if docker_manager else [])}
    result = []
    for spec in specs:
        container_state = running.get(spec.id, {})
        result.append({
            "id": spec.id,
            "instrument_id": spec.instrument_id,
            "class": spec.class_path,
            "status": container_state.get("status", "stopped"),
            "container_name": spec.docker.container_name,
            "instance_id": container_state.get("instance_id"),
            "registered": spec.id in _registered_strategies,
            "risk_limit_usd": spec.risk.max_position_usd,
        })
    return result


@app.post("/strategies/{strategy_id}/start")
async def start_strategy(strategy_id: str):
    spec = strategy_registry.get(strategy_id) if strategy_registry else None
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id!r} not found")
    container_id = docker_manager.start_strategy(spec)
    if container_id is None:
        raise HTTPException(status_code=500, detail="Failed to start container")
    return {"status": "started", "strategy_id": strategy_id, "container_id": container_id[:12]}


@app.post("/strategies/{strategy_id}/stop")
async def stop_strategy(strategy_id: str):
    spec = strategy_registry.get(strategy_id) if strategy_registry else None
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id!r} not found")
    success = docker_manager.stop_strategy(spec.docker.container_name)
    _registered_strategies.pop(strategy_id, None)
    return {"status": "stopped" if success else "not_running", "strategy_id": strategy_id}


@app.post("/strategies/{strategy_id}/register")
async def register_strategy(strategy_id: str, req: RegisterRequest):
    """Called by strategy container on connect. Detects restarts via instance_id."""
    existing = _registered_strategies.get(strategy_id)
    if existing and existing["instance_id"] != req.instance_id:
        log.warning(
            f"Strategy {strategy_id!r} restarted: old instance {existing['instance_id'][:8]} "
            f"→ new instance {req.instance_id[:8]}"
        )
        # Configure per-strategy limits for new instance
    _registered_strategies[strategy_id] = {
        "instance_id": req.instance_id,
        "registered_at": time.time(),
    }

    # Configure rate limiter and risk manager for this strategy
    spec = strategy_registry.get(strategy_id) if strategy_registry else None
    if spec:
        rate_limiter.configure_strategy(strategy_id, spec.rate_limit.max_orders_per_second)
        risk_manager.configure_strategy(strategy_id, spec.risk.max_position_usd)

    log.info(f"Strategy registered: {strategy_id} instance={req.instance_id[:8]}")
    return {"status": "registered", "strategy_id": strategy_id}


# ---------------------------------------------------------------------------
# Data / state queries
# ---------------------------------------------------------------------------

@app.get("/reconcile/{strategy_id}")
async def reconcile(strategy_id: str):
    """Return open orders + account state for a strategy (for NT reconciliation)."""
    try:
        if order_gateway is not None:
            open_orders = await order_gateway.get_open_orders()
            account_state = await order_gateway.get_account_state()
        else:
            open_orders = []
            account_state = {}

        # Filter orders that belong to this strategy
        oid_to_client = fill_dispatcher._oid_to_client_id if fill_dispatcher else {}
        strategy_oids = {
            oid for oid, sid in (fill_dispatcher._oid_to_strategy.items() if fill_dispatcher else {}).items()
            if sid == strategy_id
        }
        strategy_orders = [o for o in open_orders if int(o.get("oid", -1)) in strategy_oids]
        return {
            "strategy_id": strategy_id,
            "open_orders": strategy_orders,
            "account_state": account_state,
            "oid_client_map": {str(oid): cid for oid, cid in oid_to_client.items() if oid in strategy_oids},
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/snapshot/{instrument_id:path}")
async def get_snapshot(instrument_id: str):
    """Return current L2 book snapshot for subscriber resync."""
    coin = instrument_id.split("-")[0] if "-" in instrument_id else instrument_id
    if data_feed is None:
        raise HTTPException(status_code=503, detail="Data feed not running")
    snapshot = data_feed.get_snapshot(coin)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"No snapshot available for {coin}")
    return snapshot


@app.get("/risk")
async def get_risk():
    if risk_manager is None:
        raise HTTPException(status_code=503, detail="Risk manager not initialised")
    return risk_manager.get_summary()


@app.get("/account/{strategy_id}")
async def get_account(strategy_id: str):
    try:
        state = await order_gateway.get_account_state()
        return {"strategy_id": strategy_id, "account_state": state}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/bars")
async def get_bars(coin: str, interval: str = "1m", limit: int = 200):
    """Historical candle proxy for ZmqDataClient._request_bars()."""
    import time as _time
    import aiohttp
    from hl_engine.adapters.hyperliquid.constants import HL_BASE_URL, HL_INFO_ENDPOINT
    now_ms = int(_time.time() * 1000)
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": 0, "endTime": now_ms},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(HL_BASE_URL + HL_INFO_ENDPOINT, json=payload) as resp:
            resp.raise_for_status()
            candles = await resp.json()
    candles = candles[-limit:] if limit and len(candles) > limit else candles
    return {"coin": coin, "interval": interval, "candles": candles}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "data_feed": data_feed is not None,
        "registered_strategies": list(_registered_strategies.keys()),
        "ts": time.time(),
    }
