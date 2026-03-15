# 📘 Hyperliquid Liquidation Tracker — Async Software Specification

## 1. Project Overview

**Goal:**  
Implement a fully asynchronous Python service that connects to Hyperliquid’s WebSocket API, captures liquidation events in real time, normalizes them into a canonical format, stores raw events, maintains rolling aggregates across multiple time windows, and exposes the data via an internal interface suitable for future API or UI layers.

This system must be:
- Fully async (asyncio)
- Event-driven
- Resilient to disconnects
- Modular and testable
- Minimal but production-quality

---

## 2. Non-Goals (Explicit)

- No UI implementation
- No ML / inference
- No authentication or billing
- No multi-exchange support (for now)
- No cloud deployment config

---

## 3. Technology Stack

- **Language:** Python 3.11+
- **Async Runtime:** asyncio
- **WebSocket Client:** websockets
- **Serialization:** stdlib json
- **Storage (Phase 1):** append-only JSONL
- **Storage (Phase 2 – optional hooks):** PostgreSQL / ClickHouse
- **Process Model:** single async event loop
- **Logging:** standard logging module

---

## 4. High-Level Architecture

```
┌────────────────────────┐
│ Hyperliquid WS Client  │
└───────────┬────────────┘
            ↓
┌────────────────────────┐
│ Message Router         │
└───────────┬────────────┘
            ↓
┌────────────────────────┐
│ Liquidation Parser     │
└───────────┬────────────┘
            ↓
┌────────────────────────┐
│ Event Normalizer       │
└───────────┬────────────┘
            ↓
┌──────────────┬───────────────┐
│ Raw Store    │ Rolling Agg    │
└──────────────┴───────────────┘
```

All components must be **loosely coupled** and communicate via **async queues**.

---

## 5. Canonical Data Model

### 5.1 LiquidationEvent (immutable)

```python
@dataclass(frozen=True)
class LiquidationEvent:
    exchange: str              # "hyperliquid"
    symbol: str                # e.g. "BTC"
    side: str                  # "LONG_LIQ" | "SHORT_LIQ"
    price: float
    size: float                # base asset
    usd_value: float
    timestamp: int             # unix ms
```

This is the **only** format used internally beyond ingestion.

---

## 6. Directory Structure

```
liquidation_tracker/
│
├── main.py
├── config.py
├── models.py
├── ws/
│   ├── client.py
│   └── reconnect.py
│
├── ingest/
│   ├── router.py
│   ├── parser.py
│
├── storage/
│   ├── raw_writer.py
│   └── interfaces.py
│
├── aggregates/
│   ├── windows.py
│   └── manager.py
│
├── utils/
│   ├── time.py
│   └── logging.py
│
└── README.md
```

---

## 7. Configuration (`config.py`)

```python
WS_URL = "wss://api.hyperliquid.xyz/ws"

TIME_WINDOWS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

RAW_EVENT_PATH = "./data/liquidations.jsonl"

RECONNECT_DELAY = 5  # seconds
```

---

## 8. WebSocket Client (`ws/client.py`)

### Responsibilities
- Establish WS connection
- Subscribe to `userFills`
- Forward raw messages to router
- Handle graceful shutdown

### Requirements
- Must never block event loop
- Must reconnect on disconnect
- Must log connection state changes

---

## 9. Message Router (`ingest/router.py`)

### Responsibilities
- Accept raw JSON messages
- Route by `channel`
- Drop irrelevant messages
- Forward candidate liquidation messages to parser

### Interface
```python
async def route_message(msg: dict) -> None
```

---

## 10. Liquidation Parser (`ingest/parser.py`)

### Responsibilities
- Identify liquidation fills
- Convert raw exchange schema into `LiquidationEvent`
- Reject malformed or partial events

### Rules
- Ignore non-liquidation fills
- Infer side:
  - Sell → LONG_LIQ
  - Buy → SHORT_LIQ
- Compute `usd_value = price * size`

### Interface
```python
def parse(msg: dict) -> list[LiquidationEvent]
```

---

## 11. Raw Event Storage (`storage/raw_writer.py`)

### Responsibilities
- Append-only persistence
- Durable writes
- Non-blocking I/O

### Requirements
- Write JSONL
- Flush periodically
- Never block ingestion pipeline

### Interface
```python
async def write_event(event: LiquidationEvent) -> None
```

---

## 12. Rolling Aggregation System

### 12.1 Window Definition (`aggregates/windows.py`)

Each window:
- Fixed duration (seconds)
- Resets automatically
- Tracks:
  - total_usd
  - long_usd
  - short_usd
  - count

---

### 12.2 Aggregation Manager (`aggregates/manager.py`)

### Responsibilities
- Maintain per-symbol aggregates
- Update all windows per event
- Reset windows on expiration

### Internal Structure
```python
aggregates[symbol][window] = {
    "total_usd": float,
    "longs": float,
    "shorts": float,
    "count": int,
    "window_start": int
}
```

---

## 13. Async Coordination Model

### Queues
- `raw_msg_queue`
- `event_queue`

### Flow
```
WS → raw_msg_queue → router → parser → event_queue
   → raw_writer
   → aggregator
```

No component may call another directly.

---

## 14. Main Orchestrator (`main.py`)

### Responsibilities
- Initialize queues
- Start background tasks
- Handle shutdown signals
- Keep process alive

### Required Tasks
- WebSocket listener
- Router worker
- Storage worker
- Aggregation worker

---

## 15. Logging

- Structured logs
- One log line per:
  - connection
  - disconnect
  - parse failure
  - write failure

---

## 16. Failure & Recovery

- WS reconnect with backoff
- Skip malformed messages
- Never crash on bad input
- Log all failures

---

## 17. Extensibility Requirements

The following must be easy to add without refactor:
- New exchange
- New aggregation window
- Redis or DB-backed storage
- HTTP API layer

---

## 18. Success Criteria

The system is considered correct if:
- It runs indefinitely without memory growth
- Liquidations are captured in real time
- Aggregates update correctly
- No ingestion stalls occur
- Code is readable and modular

---

## 19. Instruction Footer (for Claude Code)

> Implement this specification exactly.  
> Do not invent features.  
> Do not collapse modules.  
> Prefer clarity over cleverness.  
> Use async/await everywhere appropriate.  
> Include docstrings and type hints.

