"""
GlobalRiskManager — combined notional tracking across all strategies.

Enforces two layers of risk:
  1. Per-strategy limit (max_position_usd from YAML config)
  2. Global ceiling (GLOBAL_NOTIONAL_CEILING_USD env var)

Recovery on restart:
  1. Load last risk_snapshots from SQLite
  2. Replay fills since snapshot to get exact notional
  Call restore(snapshots, fills_by_strategy) before any orders.
"""

import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)


class GlobalRiskManager:
    """
    Tracks per-strategy notional exposure and enforces a global ceiling.

    Thread-safety: all mutations protected by asyncio.Lock.
    """

    def __init__(self, global_ceiling_usd: float) -> None:
        self._global_ceiling = global_ceiling_usd
        self._strategy_notionals: dict[str, float] = {}
        self._strategy_limits: dict[str, float] = {}   # strategy_id → max_position_usd
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def configure_strategy(self, strategy_id: str, max_position_usd: float) -> None:
        """Register per-strategy limit (from YAML config)."""
        self._strategy_limits[strategy_id] = max_position_usd
        self._strategy_notionals.setdefault(strategy_id, 0.0)

    def restore(
        self,
        snapshots: dict[str, dict],
        fills_by_strategy: dict[str, list[dict]],
    ) -> None:
        """
        Exact notional recovery on startup.

        snapshots: {strategy_id: {notional, ts}}
        fills_by_strategy: {strategy_id: [{notional_delta}]} — fills since snapshot
        """
        for strategy_id, snap in snapshots.items():
            notional = float(snap["notional"])
            for fill in fills_by_strategy.get(strategy_id, []):
                notional += float(fill["notional_delta"])
            self._strategy_notionals[strategy_id] = notional
            log.info(f"Risk restored: {strategy_id} → notional=${notional:.2f}")

    # ------------------------------------------------------------------
    # Order checks
    # ------------------------------------------------------------------

    async def check_order(
        self,
        strategy_id: str,
        order_notional: float,
        is_reduce: bool = False,
    ) -> tuple[bool, str]:
        """
        Check whether an order is allowed under risk limits.

        Returns (True, "") if allowed, (False, reason) if denied.
        Reduce-only orders bypass notional add checks.
        """
        if is_reduce:
            return True, ""

        async with self._lock:
            current = self._strategy_notionals.get(strategy_id, 0.0)
            strategy_limit = self._strategy_limits.get(strategy_id)

            if strategy_limit is not None and current + order_notional > strategy_limit:
                return (
                    False,
                    f"Strategy notional limit exceeded: ${current + order_notional:.0f} > ${strategy_limit:.0f}",
                )

            total_notional = sum(self._strategy_notionals.values())
            if total_notional + order_notional > self._global_ceiling:
                return (
                    False,
                    f"Global notional ceiling exceeded: ${total_notional + order_notional:.0f} > ${self._global_ceiling:.0f}",
                )

        return True, ""

    async def reserve_notional(self, strategy_id: str, notional: float) -> None:
        """Reserve notional when an order is submitted (before fill)."""
        async with self._lock:
            self._strategy_notionals[strategy_id] = (
                self._strategy_notionals.get(strategy_id, 0.0) + notional
            )

    async def record_fill(self, strategy_id: str, notional_delta: float) -> None:
        """Update notional after a fill (positive = added, negative = closed)."""
        async with self._lock:
            current = self._strategy_notionals.get(strategy_id, 0.0)
            self._strategy_notionals[strategy_id] = max(0.0, current + notional_delta)

    async def release_notional(self, strategy_id: str, notional: float) -> None:
        """Release reserved notional on cancel or rejection."""
        async with self._lock:
            current = self._strategy_notionals.get(strategy_id, 0.0)
            self._strategy_notionals[strategy_id] = max(0.0, current - notional)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_summary(self) -> dict:
        total = sum(self._strategy_notionals.values())
        return {
            "global_ceiling_usd": self._global_ceiling,
            "total_notional_usd": round(total, 2),
            "ceiling_utilization_pct": round(100.0 * total / self._global_ceiling, 1) if self._global_ceiling else 0.0,
            "strategies": {
                sid: {
                    "notional_usd": round(n, 2),
                    "limit_usd": self._strategy_limits.get(sid),
                    "utilization_pct": round(
                        100.0 * n / self._strategy_limits[sid], 1
                    ) if self._strategy_limits.get(sid) else None,
                }
                for sid, n in self._strategy_notionals.items()
            },
        }
