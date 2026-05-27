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
  POST   /strategies/{id}/state       Strategy pushes its state dict (monitor cache)
  GET    /strategies/{id}/state       Monitor polls strategy state (404 if not pushed yet)
  GET    /reconcile/{strategy_id}     Open orders + account state for resync
  GET    /snapshot/{instrument_id}    Current L2 book snapshot
  GET    /risk                        Global risk summary
  GET    /account/{strategy_id}       HL clearinghouseState proxy
  GET    /bars                        Historical candles proxy
  GET    /health                      Liveness check
"""

import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

from hl_engine.orchestrator import metrics

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
paper_execution = None   # PaperExecutionEngine
strategy_registry = None  # StrategyRegistry
docker_manager = None    # DockerManager
data_feed = None         # OrchestratorDataFeed

# {strategy_id: {instance_id, registered_at}}
_registered_strategies: dict[str, dict] = {}
_REGISTRATION_TTL_SECS = 90.0

# strategies whose Prometheus label-sets have been pre-registered
_metrics_initialized: set[str] = set()

# Strategy state cache — dumb KV store, strategies POST here, monitor GETs here
_strategy_states: dict[str, Any] = {}

# Peak equity is process-local; paper accounts are restored from DB and then
# re-establish the peak from the next account/state update.
_peak_equity_by_strategy: dict[str, float] = {}


def _strategy_initial_balance(strategy_id: str) -> float:
    spec = strategy_registry.get(strategy_id) if strategy_registry else None
    if spec and "initial_balance_usdc" in spec.parameters:
        return float(spec.parameters["initial_balance_usdc"])
    if spec and "fallback_account_equity" in spec.parameters:
        return float(spec.parameters["fallback_account_equity"])
    if strategy_id == "vclimax-btc":
        return 1000.0
    return 10_000.0


def _top_of_book_fill_price(instrument_id: str, is_buy: bool) -> Optional[float]:
    if data_feed is None:
        return None
    coin = instrument_id.split("-")[0]
    snap = data_feed.get_snapshot(coin)
    if not snap:
        return None
    levels = snap.get("asks" if is_buy else "bids", [])
    if not levels:
        return None
    return float(levels[0][0])


def _update_drawdown(strategy_id: str, equity: float) -> None:
    peak = max(_peak_equity_by_strategy.get(strategy_id, equity), equity)
    _peak_equity_by_strategy[strategy_id] = peak
    drawdown = 0.0 if peak <= 0.0 else 100.0 * (peak - equity) / peak
    metrics.drawdown_pct.labels(strategy=strategy_id).set(drawdown)


def update_strategy_account_metrics(
    strategy_id: str,
    *,
    currency: str = "USDC",
    instrument: str = "",
    equity: Optional[float] = None,
    balance: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    unrealized_pnl: Optional[float] = None,
    net_exposure_qty: Optional[float] = None,
) -> None:
    """Update generic strategy/account Prometheus gauges."""
    if equity is not None:
        metrics.account_equity.labels(strategy=strategy_id, currency=currency).set(equity)
        _update_drawdown(strategy_id, equity)
    if balance is not None:
        metrics.account_balance.labels(strategy=strategy_id, currency=currency).set(balance)
    if realized_pnl is not None:
        metrics.realized_pnl.labels(strategy=strategy_id, currency=currency).set(realized_pnl)
    if unrealized_pnl is not None:
        metrics.unrealized_pnl.labels(strategy=strategy_id, currency=currency).set(unrealized_pnl)
    if instrument and net_exposure_qty is not None:
        metrics.net_exposure_qty.labels(strategy=strategy_id, instrument=instrument).set(net_exposure_qty)


def _update_metrics_from_strategy_state(strategy_id: str, state: dict[str, Any]) -> None:
    """Translate a pushed strategy state dict into Prometheus gauges."""
    metrics.strategy_state.labels(strategy=strategy_id).set(1)

    currency = str(state.get("currency") or "USDC")
    instrument = str(state.get("instrument") or "")
    position = state.get("position") or {}

    equity = state.get("equity", state.get("balance"))
    balance = state.get("balance", equity)
    realized = state.get("realized_pnl", position.get("realized_pnl", 0.0))
    unrealized = state.get("unrealized_pnl", position.get("unrealized_pnl", 0.0))
    qty = position.get("signed_qty", position.get("qty"))

    try:
        update_strategy_account_metrics(
            strategy_id,
            currency=currency,
            instrument=instrument,
            equity=float(equity) if equity is not None else None,
            balance=float(balance) if balance is not None else None,
            realized_pnl=float(realized) if realized is not None else None,
            unrealized_pnl=float(unrealized) if unrealized is not None else None,
            net_exposure_qty=float(qty) if qty is not None else None,
        )
    except (TypeError, ValueError):
        log.warning("Invalid numeric strategy state for %s: %r", strategy_id, state)

    vclimax = state.get("vclimax") or {}
    if vclimax:
        phase = str(vclimax.get("phase") or "")
        for known_phase in metrics._VCLIMAX_PHASES:
            metrics.vclimax_phase.labels(strategy=strategy_id, phase=known_phase).set(
                1 if phase == known_phase else 0
            )

        label_instrument = instrument or "unknown"
        for metric_name, gauge in (
            ("active_stop", metrics.vclimax_active_stop),
            ("entry_price", metrics.vclimax_entry_price),
            ("climax_high", metrics.vclimax_climax_high),
            ("climax_low", metrics.vclimax_climax_low),
        ):
            value = vclimax.get(metric_name)
            if value is not None:
                try:
                    gauge.labels(strategy=strategy_id, instrument=label_instrument).set(float(value))
                except (TypeError, ValueError):
                    pass

        bars_since = vclimax.get("bars_since_climax")
        if bars_since is not None:
            try:
                metrics.vclimax_bars_since_climax.labels(strategy=strategy_id).set(float(bars_since))
            except (TypeError, ValueError):
                pass


def _prune_stale_registrations() -> None:
    """Mark strategies stale when their periodic register heartbeat stops."""
    now = time.time()
    stale = [
        strategy_id
        for strategy_id, info in _registered_strategies.items()
        if now - float(info.get("registered_at", 0.0)) > _REGISTRATION_TTL_SECS
    ]
    for strategy_id in stale:
        _registered_strategies.pop(strategy_id, None)
        metrics.strategy_state.labels(strategy=strategy_id).set(0)
        log.warning("Strategy registration stale: %s", strategy_id)


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
    # Lazy Prometheus label-set initialisation — fires once per strategy_id
    if req.strategy_id not in _metrics_initialized:
        metrics.init_strategy_metrics(req.strategy_id)
        _metrics_initialized.add(req.strategy_id)

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
    is_buy = req.side.upper() == "BUY"
    sz = req.quantity

    # 5. Build HL order params (needed before notional estimate for MARKET orders)
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

    # Notional estimate uses limit_px (correct for both MARKET and LIMIT paths)
    notional = limit_px * sz

    paper_mode = order_gateway is None and paper_execution is not None
    is_reduce = req.is_reduce
    if paper_mode:
        paper_execution.ensure_account(req.strategy_id, _strategy_initial_balance(req.strategy_id))
        is_reduce = is_reduce or paper_execution.order_reduces_position(req.strategy_id, is_buy, sz)

    allowed, reason = await risk_manager.check_order(req.strategy_id, notional, is_reduce)
    if not allowed:
        metrics.orders_rejected.labels(strategy=req.strategy_id, reason="risk").inc()
        raise HTTPException(status_code=422, detail=reason)

    # 6. Submit to Hyperliquid (or mock in paper-trade mode)
    side_label = "buy" if is_buy else "sell"
    if order_gateway is None:
        if paper_execution is None:
            raise HTTPException(status_code=503, detail="Paper execution not initialised")

        # Paper-trade / no private key: create a synthetic oid and immediate fill.
        import random
        oid = random.randint(100_000, 999_999)
        fill_px = _top_of_book_fill_price(req.instrument_id, is_buy)
        if fill_px is None:
            if req.order_type.upper() == "LIMIT" and req.price is not None:
                fill_px = float(req.price)
            elif req.price is not None and req.price > 0:
                fill_px = float(req.price)
            else:
                raise HTTPException(status_code=422, detail="Paper orders require a live book price")
        log.info(f"[PAPER] Mock order accepted: {req.strategy_id} {req.side} {sz} {coin} @ {fill_px} → mock_oid={oid}")
    else:
        try:
            result = await order_gateway.submit_order(coin, is_buy, sz, limit_px, order_type_dict)
        except Exception as e:
            rate_limiter.record_hl_rejection(req.strategy_id)
            metrics.orders_rejected.labels(strategy=req.strategy_id, reason="hl_error").inc()
            raise HTTPException(status_code=502, detail=f"HL submission error: {e}")

        if result.get("status") != "ok":
            rate_limiter.record_hl_rejection(req.strategy_id)
            metrics.orders_rejected.labels(strategy=req.strategy_id, reason="hl_error").inc()
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
    submit_ts_ns = time.time_ns()
    rate_limiter.record_hl_success(req.strategy_id)
    persistence.mark_order_submitted(req.client_order_id, oid)
    persistence.save_oid_mapping(oid, req.strategy_id, req.client_order_id)
    if fill_dispatcher is not None:
        fill_dispatcher.register_oid(oid, req.strategy_id, req.client_order_id, notional, submit_ts_ns)
    if paper_mode:
        await paper_execution.execute_order(
            oid=oid,
            strategy_id=req.strategy_id,
            client_order_id=req.client_order_id,
            instrument_id=req.instrument_id,
            side=req.side,
            qty=sz,
            fill_px=fill_px,
            initial_balance=_strategy_initial_balance(req.strategy_id),
        )
    else:
        await risk_manager.reserve_notional(req.strategy_id, notional)

    metrics.orders_submitted.labels(
        strategy=req.strategy_id,
        side=side_label,
        order_type=req.order_type.lower(),
    ).inc()

    log.info(f"Order submitted: {req.strategy_id} {req.side} {sz} {coin} oid={oid}")
    return {"status": "submitted", "oid": oid, "client_order_id": req.client_order_id}


@app.delete("/orders/{oid}")
async def cancel_order(oid: int, strategy_id: str = "", instrument_id: str = ""):
    coin = instrument_id.split("-")[0] if instrument_id else ""
    if not coin:
        raise HTTPException(status_code=422, detail="instrument_id required for cancel")
    if order_gateway is None:
        log.info(f"[PAPER] Mock cancel: oid={oid} {strategy_id}")
        metrics.orders_canceled.labels(
            strategy=strategy_id or "unknown",
            instrument=instrument_id,
            reason="requested",
        ).inc()
        return {"status": "cancelled", "oid": oid}
    try:
        result = await order_gateway.cancel_order(coin, oid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if result.get("status") != "ok":
        raise HTTPException(status_code=422, detail=str(result))
    metrics.orders_canceled.labels(
        strategy=strategy_id or "unknown",
        instrument=instrument_id,
        reason="requested",
    ).inc()
    return {"status": "canceled", "oid": oid}


# ---------------------------------------------------------------------------
# Strategy management
# ---------------------------------------------------------------------------

@app.get("/strategies")
async def list_strategies():
    _prune_stale_registrations()
    specs = strategy_registry.list_all() if strategy_registry else []
    running = {s["strategy_id"]: s for s in (docker_manager.list_running() if docker_manager else [])}
    result = []
    for spec in specs:
        container_state = running.get(spec.id, {})
        registered = _registered_strategies.get(spec.id)
        status = container_state.get("status")
        if not status and docker_manager:
            status = docker_manager.get_status(spec.docker.container_name)
        result.append({
            "id": spec.id,
            "instrument_id": spec.instrument_id,
            "class": spec.class_path,
            "status": status or "stopped",
            "container_name": spec.docker.container_name,
            "instance_id": container_state.get("instance_id") or (registered or {}).get("instance_id"),
            "registered": registered is not None,
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
    metrics.strategy_state.labels(strategy=strategy_id).set(0)
    return {"status": "stopped" if success else "not_running", "strategy_id": strategy_id}


@app.post("/admin/strategies/{strategy_id}/clear-paper-position")
async def clear_paper_position(strategy_id: str):
    """Clear stale paper exposure and risk without deleting historical fills."""
    spec = strategy_registry.get(strategy_id) if strategy_registry else None
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id!r} not found")
    if paper_execution is None or risk_manager is None or persistence is None:
        raise HTTPException(status_code=503, detail="Paper execution is not initialised")

    account = paper_execution.clear_position(strategy_id, _strategy_initial_balance(strategy_id))
    await risk_manager.set_notional(strategy_id, 0.0)
    persistence.save_risk_snapshot(strategy_id, 0.0)
    return {
        "status": "cleared",
        "strategy_id": strategy_id,
        "paper": {
            "initial_balance": account.initial_balance,
            "balance": account.balance,
            "realized_pnl": account.realized_pnl,
            "position_qty": account.position_qty,
            "avg_price": account.avg_price,
            "cumulative_fees": account.cumulative_fees,
        },
    }


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

    # Pre-register Prometheus label combinations once; the register loop refreshes
    # every 30s and must not reset stateful gauges such as VClimax phase.
    if strategy_id not in _metrics_initialized:
        metrics.init_strategy_metrics(strategy_id)
        _metrics_initialized.add(strategy_id)
    metrics.strategy_state.labels(strategy=strategy_id).set(1)

    log.info(f"Strategy registered: {strategy_id} instance={req.instance_id[:8]}")
    return {"status": "registered", "strategy_id": strategy_id}


# ---------------------------------------------------------------------------
# Strategy state cache (monitor polling)
# ---------------------------------------------------------------------------

@app.post("/strategies/{strategy_id}/state")
async def push_strategy_state(strategy_id: str, state: dict[str, Any]):
    """Strategy pushes its state dict; orchestrator is a dumb cache."""
    _strategy_states[strategy_id] = state
    _update_metrics_from_strategy_state(strategy_id, state)
    return {"status": "ok"}


@app.get("/strategies/{strategy_id}/state")
async def get_strategy_state(strategy_id: str):
    """Monitor polls this; 404 if the strategy has never pushed state."""
    if strategy_id not in _strategy_states:
        raise HTTPException(status_code=404, detail=f"No state for {strategy_id!r}")
    return _strategy_states[strategy_id]


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
        elif paper_execution is not None:
            open_orders = []
            account_state = paper_execution.account_state(strategy_id, _strategy_initial_balance(strategy_id))
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
    _prune_stale_registrations()
    if risk_manager is None:
        raise HTTPException(status_code=503, detail="Risk manager not initialised")
    return risk_manager.get_summary()


@app.get("/account/{strategy_id}")
async def get_account(strategy_id: str):
    try:
        if order_gateway is not None:
            state = await order_gateway.get_account_state()
        elif paper_execution is not None:
            state = paper_execution.account_state(strategy_id, _strategy_initial_balance(strategy_id))
        else:
            state = {}
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
    _prune_stale_registrations()
    return {
        "status": "ok",
        "data_feed": data_feed is not None,
        "registered_strategies": list(_registered_strategies.keys()),
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Expose Prometheus metrics for scraping."""
    _prune_stale_registrations()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
