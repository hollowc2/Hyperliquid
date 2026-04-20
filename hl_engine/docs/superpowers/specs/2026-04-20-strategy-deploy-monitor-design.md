# Strategy Deployment & Textual Monitor Design

**Date:** 2026-04-20  
**Status:** Approved

## Overview

Two deliverables:
1. Deploy `apex-btc` as a paper-trading Docker container via the orchestrator's existing DockerManager
2. Rewrite `monitor.py` as a Textual TUI with a strategy selector and per-strategy detail panel

---

## Part 1 — Deployment (apex-btc)

### Approach
Use the existing `POST /strategies/apex-btc/start` orchestrator endpoint. DockerManager launches `strategy-apex-btc` container with `HL_PAPER_TRADE=true` inherited from `.env`, `restart_policy: unless-stopped`. No docker-compose changes needed — orchestrator owns the lifecycle.

### Pre-flight check
Confirm `.env` contains `HL_PAPER_TRADE=true` before calling start. If absent, the strategy attempts live orders.

### Invocation
```bash
curl -s -X POST http://localhost:8100/strategies/apex-btc/start
# or
hl start apex-btc
```

### Container environment (set by DockerManager)
| Var | Value |
|-----|-------|
| `STRATEGY_CONFIG` | `/strategies/apex-btc.yml` |
| `ORCHESTRATOR_REST_URL` | `http://<orchestrator_host>:8000` |
| `ORCHESTRATOR_ZMQ_DATA` | `tcp://<orchestrator_host>:5555` |
| `ORCHESTRATOR_ZMQ_FILLS` | `tcp://<orchestrator_host>:5556` |
| `STRATEGY_INSTANCE_ID` | generated UUID |
| `HL_PAPER_TRADE` | `true` (from `.env`) |

---

## Part 2 — Orchestrator State Endpoints

### New endpoints in `hl_engine/orchestrator/app.py`

```
POST /strategies/{strategy_id}/state   — strategy pushes its state dict
GET  /strategies/{strategy_id}/state   — monitor polls it
```

### Storage
`_strategy_states: dict[str, Any] = {}` — plain in-memory dict. POST overwrites, GET returns (404 if nothing pushed yet). No persistence, no schema validation — orchestrator is a dumb cache.

### Error contract
- Strategy: fire-and-forget via `asyncio.create_task()`. Log warning on failure, never block signal path.
- Monitor: show "no state" panel gracefully on 404.

---

## Part 3 — ApexStrategy State Push

### Change
In `ApexStrategy`, wherever `apex_state.json` is written, also POST the same state dict to `{ORCHESTRATOR_REST_URL}/strategies/apex-btc/state` as a non-blocking async task.

### Timing
Inside the existing 100ms-throttled `_on_data` handler — no new timer.

### State dict shape
Unchanged from current `apex_state.json` schema:
- `ts`, `instrument`, `regime`, `mid_px`
- `balance`, `trade_count`, `total_commission`
- `position` (side, qty, avg_px, unrealized_pnl, realized_pnl, duration_s)
- `features` (obi, tfi, mp_drift, hawkes, cascade, funding, spread, vol_short)
- `last_edge`, `active_order`, `last_order`

### Transition
Keep writing `apex_state.json` in parallel until Textual monitor is validated. Drop file write after.

---

## Part 4 — Textual Monitor

### Layout
```
┌─────────────────────────────────────────────────────┐
│  Header: orchestrator URL · global notional · time  │
├──────────────────┬──────────────────────────────────┤
│  Strategy List   │  Detail Panel                    │
│  (left sidebar)  │                                  │
│  ● apex-btc      │  [strategy-specific content]     │
│  ○ ma-cross-btc  │                                  │
│                  │                                  │
├──────────────────┴──────────────────────────────────┤
│  Footer: keybinds · circuit breaker status          │
└─────────────────────────────────────────────────────┘
```

### Strategy List (left, ~30% width)
- `ListView` widget populated from `GET /strategies` polled at 1Hz
- Color coding: green `●` = running + registered, yellow `●` = running unregistered, grey `○` = stopped
- Arrow keys or click to select; fires `StrategySelected` message

### Detail Panel (right, ~70% width)
Polls `GET /strategies/{id}/state` at 2Hz via `set_interval`.

**If state available (strategy pushed data):**
- Left sub-panel: Account / Position (balance, trade count, commission, position side/qty/avg/PnL/duration)
- Right sub-panel: Features & Signal (OBI, TFI, MP drift, Hawkes, Cascade, Funding, Spread, Vol — with ASCII bar chart), last edge, active order

**If no state (404 or strategy not running):**
- Generic panel: strategy ID, status, instance ID, risk utilization, circuit breaker state — sourced from `/strategies` and `/risk`

### Header
Polls `/risk` at 1Hz: global notional, ceiling, utilization %, current UTC time.

### Keybinds (functional, not just help text)
- `↑`/`↓` — navigate strategy list
- `s` — call `POST /strategies/{selected_id}/start` on orchestrator
- `x` — call `POST /strategies/{selected_id}/stop` on orchestrator
- `q` / `Ctrl+C` — quit

### Footer
Static display of the above keybind hints.

### CLI
```bash
uv run python hl_engine/monitor.py --multi
uv run python hl_engine/monitor.py --multi --url http://localhost:8100
```
Old `--state` single-strategy mode remains unchanged as a separate code path.

### Dependencies
Add `textual>=0.80` to `pyproject.toml`.

---

## File Changes Summary

| File | Change |
|------|--------|
| `hl_engine/orchestrator/app.py` | Add `POST/GET /strategies/{id}/state` (~20 lines) |
| `hl_engine/strategy/apex_strategy.py` | Add async state push in `_on_data` |
| `hl_engine/monitor.py` | Rewrite `--multi` mode as Textual app; keep `--state` path |
| `pyproject.toml` | Add `textual>=0.80` |

---

## Out of Scope
- Persisting strategy state across orchestrator restarts
- State push for `ma-cross-btc` (no equivalent state dict; generic panel covers it)
- Removing `apex_state.json` file write (deferred until monitor is validated)
