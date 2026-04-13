"""
SQLite-backed persistence for the orchestrator.

Design:
- WAL journal mode for concurrent reads + writes.
- All writes are fire-and-forget: enqueued to _write_queue, consumed by a
  single _writer_task. Callers never block on writes.
- Critical reads (startup recovery) use a direct aiosqlite connection BEFORE
  the writer task starts.

Tables:
  orders       client_order_id (PK), strategy_id, instrument_id, side,
               qty, price, order_type, status, oid, ts
  fills        id (auto), oid, strategy_id, fill_px, fill_sz, fee, hash,
               notional_delta, ts_event_ns, ts_stored
  oid_map      oid (PK), strategy_id, client_order_id, ts
  risk_snaps   strategy_id (PK), notional, ts
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL,
    instrument_id   TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL,
    order_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    oid             INTEGER,
    ts              INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    oid             INTEGER NOT NULL,
    strategy_id     TEXT NOT NULL,
    fill_px         REAL NOT NULL,
    fill_sz         REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0.0,
    hash            TEXT,
    notional_delta  REAL NOT NULL DEFAULT 0.0,
    ts_event_ns     INTEGER NOT NULL,
    ts_stored       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oid_map (
    oid             INTEGER PRIMARY KEY,
    strategy_id     TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    ts              INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_snaps (
    strategy_id     TEXT PRIMARY KEY,
    notional        REAL NOT NULL DEFAULT 0.0,
    ts              INTEGER NOT NULL
);
"""


class PersistenceStore:
    """Thread-safe, async-friendly SQLite store for orchestrator state."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._write_queue: asyncio.Queue[tuple] = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create tables and start the background writer task."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_DDL)
            await db.commit()
        self._writer_task = asyncio.create_task(self._run_writer(), name="db_writer")
        self._started = True
        log.info(f"PersistenceStore initialised: {self._db_path}")

    async def close(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()

    # ------------------------------------------------------------------
    # Write helpers (fire-and-forget enqueue)
    # ------------------------------------------------------------------

    def _enqueue(self, sql: str, params: tuple = ()) -> None:
        self._write_queue.put_nowait((sql, params))

    def save_order_pending(
        self,
        client_order_id: str,
        strategy_id: str,
        instrument_id: str,
        side: str,
        qty: float,
        price: Optional[float],
        order_type: str,
    ) -> None:
        self._enqueue(
            """INSERT OR IGNORE INTO orders
               (client_order_id, strategy_id, instrument_id, side, qty, price, order_type, status, ts)
               VALUES (?,?,?,?,?,?,?,'PENDING',?)""",
            (client_order_id, strategy_id, instrument_id, side, qty, price, order_type, time.time_ns()),
        )

    def mark_order_submitted(self, client_order_id: str, oid: int) -> None:
        self._enqueue(
            "UPDATE orders SET status='SUBMITTED', oid=? WHERE client_order_id=?",
            (oid, client_order_id),
        )

    def mark_order_filled(self, client_order_id: str) -> None:
        self._enqueue(
            "UPDATE orders SET status='FILLED' WHERE client_order_id=?",
            (client_order_id,),
        )

    def save_oid_mapping(self, oid: int, strategy_id: str, client_order_id: str) -> None:
        self._enqueue(
            "INSERT OR REPLACE INTO oid_map (oid, strategy_id, client_order_id, ts) VALUES (?,?,?,?)",
            (oid, strategy_id, client_order_id, time.time_ns()),
        )

    def save_fill(
        self,
        oid: int,
        strategy_id: str,
        fill_px: float,
        fill_sz: float,
        fee: float,
        hash_: str,
        notional_delta: float,
        ts_event_ns: int,
    ) -> None:
        self._enqueue(
            """INSERT INTO fills
               (oid, strategy_id, fill_px, fill_sz, fee, hash, notional_delta, ts_event_ns, ts_stored)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (oid, strategy_id, fill_px, fill_sz, fee, hash_, notional_delta, ts_event_ns, time.time_ns()),
        )

    def save_risk_snapshot(self, strategy_id: str, notional: float) -> None:
        self._enqueue(
            "INSERT OR REPLACE INTO risk_snaps (strategy_id, notional, ts) VALUES (?,?,?)",
            (strategy_id, notional, time.time_ns()),
        )

    # ------------------------------------------------------------------
    # Read helpers (direct reads for startup recovery — before writer starts)
    # ------------------------------------------------------------------

    async def load_oid_mappings(self) -> dict[int, dict]:
        """Return {oid: {strategy_id, client_order_id}} for all known oids."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT oid, strategy_id, client_order_id FROM oid_map") as cur:
                rows = await cur.fetchall()
        return {row[0]: {"strategy_id": row[1], "client_order_id": row[2]} for row in rows}

    async def check_order_idempotent(self, client_order_id: str) -> Optional[int]:
        """Return existing oid if this client_order_id was already submitted, else None."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT oid FROM orders WHERE client_order_id=? AND status IN ('SUBMITTED','FILLED')",
                (client_order_id,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def load_risk_snapshots(self) -> dict[str, dict]:
        """Return {strategy_id: {notional, ts}} for startup risk recovery."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT strategy_id, notional, ts FROM risk_snaps") as cur:
                rows = await cur.fetchall()
        return {row[0]: {"notional": row[1], "ts": row[2]} for row in rows}

    async def load_fills_since(self, strategy_id: str, since_ts: int) -> list[dict]:
        """Return fills for a strategy after a given timestamp (for risk replay)."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT notional_delta FROM fills WHERE strategy_id=? AND ts_stored > ?",
                (strategy_id, since_ts),
            ) as cur:
                rows = await cur.fetchall()
        return [{"notional_delta": row[0]} for row in rows]

    # ------------------------------------------------------------------
    # Background writer task
    # ------------------------------------------------------------------

    async def _run_writer(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            while True:
                try:
                    sql, params = await self._write_queue.get()
                    try:
                        await db.execute(sql, params)
                        await db.commit()
                    except aiosqlite.Error as e:
                        log.error(f"DB write error: {e} | sql={sql!r} params={params!r}")
                    finally:
                        self._write_queue.task_done()
                except asyncio.CancelledError:
                    break
        log.info("DB writer task stopped")
