"""
TimescaleDB sink for Hyperliquid live market data.

Creates hypertables on first connect and provides bulk-insert helpers
for trades, L2 order book snapshots, and 1-minute OHLCV bars.

Schema
------
  trades      — trade ticks        (ts, coin, price, size, side, trade_id)
  l2_book     — L2 snapshots       (ts, coin, side, level, price, size)
  bars_1m     — 1-minute OHLCV     (ts, coin, open, high, low, close, volume)

Usage
-----
    sink = TimescaleSink(dsn="postgresql://user:pass@localhost/market_data")
    await sink.connect()
    await sink.init_schema()
    ...
    await sink.insert_trades(rows)
    await sink.close()
"""

import logging
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    ts         TIMESTAMPTZ NOT NULL,
    coin       TEXT        NOT NULL,
    price      FLOAT8      NOT NULL,
    size       FLOAT8      NOT NULL,
    side       CHAR(1)     NOT NULL,   -- 'B' buyer-aggressor, 'S' seller-aggressor
    trade_id   TEXT
);

CREATE TABLE IF NOT EXISTS l2_book (
    ts         TIMESTAMPTZ NOT NULL,
    coin       TEXT        NOT NULL,
    side       CHAR(1)     NOT NULL,   -- 'B' bid, 'A' ask
    level      SMALLINT    NOT NULL,   -- 0-indexed from best price
    price      FLOAT8      NOT NULL,
    size       FLOAT8      NOT NULL
);

CREATE TABLE IF NOT EXISTS bars_1m (
    ts         TIMESTAMPTZ NOT NULL,   -- bar close time
    coin       TEXT        NOT NULL,
    open       FLOAT8      NOT NULL,
    high       FLOAT8      NOT NULL,
    low        FLOAT8      NOT NULL,
    close      FLOAT8      NOT NULL,
    volume     FLOAT8      NOT NULL
);
"""

_HYPERTABLES = ["trades", "l2_book", "bars_1m"]

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades  (coin, ts DESC);
CREATE INDEX IF NOT EXISTS idx_l2_coin     ON l2_book (coin, ts DESC);
CREATE INDEX IF NOT EXISTS idx_bars_coin   ON bars_1m (coin, ts DESC);
"""


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


class TimescaleSink:
    """
    Manages an asyncpg connection pool and exposes bulk-insert methods
    for each data type.

    Parameters
    ----------
    dsn : str
        asyncpg-compatible DSN, e.g.
        ``postgresql://user:pass@localhost:5432/market_data``
    min_size, max_size : int
        Connection pool sizing.
    """

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 5) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max
        )
        log.info("TimescaleDB pool connected.")

    async def init_schema(self) -> None:
        """Create tables and hypertables if they don't already exist."""
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)
            for table in _HYPERTABLES:
                try:
                    await conn.execute(
                        "SELECT create_hypertable($1, 'ts', if_not_exists => TRUE)",
                        table,
                    )
                    log.info(f"Hypertable ready: {table}")
                except Exception as exc:
                    # Already a hypertable or extension not loaded — log and continue
                    log.warning(f"create_hypertable({table}): {exc}")
            await conn.execute(_INDEXES)
        log.info("Schema initialised.")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            log.info("TimescaleDB pool closed.")

    # ------------------------------------------------------------------
    # Bulk inserts
    # ------------------------------------------------------------------

    async def insert_trades(self, rows: list[tuple]) -> None:
        """
        Parameters
        ----------
        rows : list of (ts_ms: int, coin: str, price: float, size: float,
                        side: str, trade_id: str)
        """
        if not rows:
            return
        records = [
            (_ms_to_dt(r[0]), r[1], r[2], r[3], r[4], r[5])
            for r in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO trades (ts, coin, price, size, side, trade_id) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                records,
            )

    async def insert_l2(self, rows: list[tuple]) -> None:
        """
        Parameters
        ----------
        rows : list of (ts_ms: int, coin: str, side: str, level: int,
                        price: float, size: float)
        """
        if not rows:
            return
        records = [
            (_ms_to_dt(r[0]), r[1], r[2], r[3], r[4], r[5])
            for r in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO l2_book (ts, coin, side, level, price, size) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                records,
            )

    async def insert_bars(self, rows: list[tuple]) -> None:
        """
        Parameters
        ----------
        rows : list of (ts_ms: int, coin: str, open: float, high: float,
                        low: float, close: float, volume: float)
        """
        if not rows:
            return
        records = [
            (_ms_to_dt(r[0]), r[1], r[2], r[3], r[4], r[5], r[6])
            for r in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO bars_1m (ts, coin, open, high, low, close, volume) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                records,
            )
