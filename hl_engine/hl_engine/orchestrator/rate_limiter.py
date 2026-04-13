"""
Rate limiter and circuit breaker for order submission.

RateLimiter:
  - Per-strategy token bucket (max_orders_per_second from YAML)
  - Global token bucket (GLOBAL_MAX_OPS env var, default 10/s)
  - check_and_consume(strategy_id) -> (allowed: bool, reason: str)

CircuitBreaker:
  - Tracks consecutive HL rejections per strategy
  - Opens (blocks all orders) after N consecutive rejections in a time window
  - Auto-closes after a cooldown period
"""

import asyncio
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

_CONSECUTIVE_REJECT_THRESHOLD = 5
_REJECT_WINDOW_SECS = 60.0
_CIRCUIT_OPEN_DURATION_SECS = 300.0  # 5 minutes


class _TokenBucket:
    """Simple token bucket rate limiter (synchronous — fast, no lock needed for single token)."""

    def __init__(self, rate: float) -> None:
        self._rate = max(rate, 0.001)
        self._tokens = float(rate)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def consume(self) -> bool:
        """Consume one token. Returns True if allowed, False if rate exceeded."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class _CircuitBreaker:
    """Per-strategy circuit breaker tracking HL rejection streaks."""

    def __init__(self) -> None:
        self._consecutive_rejects = 0
        self._first_reject_ts: Optional[float] = None
        self._open_until: Optional[float] = None

    def is_open(self) -> bool:
        if self._open_until is None:
            return False
        if time.monotonic() >= self._open_until:
            # Auto-close
            self._open_until = None
            self._consecutive_rejects = 0
            self._first_reject_ts = None
            return False
        return True

    def record_rejection(self, strategy_id: str) -> None:
        now = time.monotonic()
        if self._first_reject_ts is None:
            self._first_reject_ts = now
        elif now - self._first_reject_ts > _REJECT_WINDOW_SECS:
            # Reset window
            self._consecutive_rejects = 0
            self._first_reject_ts = now

        self._consecutive_rejects += 1
        if self._consecutive_rejects >= _CONSECUTIVE_REJECT_THRESHOLD:
            self._open_until = now + _CIRCUIT_OPEN_DURATION_SECS
            log.warning(
                f"Circuit breaker OPENED for strategy {strategy_id!r} — "
                f"{self._consecutive_rejects} consecutive HL rejections in {_REJECT_WINDOW_SECS}s. "
                f"Orders blocked for {_CIRCUIT_OPEN_DURATION_SECS}s."
            )

    def record_success(self) -> None:
        self._consecutive_rejects = 0
        self._first_reject_ts = None


class RateLimiter:
    """
    Per-strategy + global rate limiter with circuit breakers.

    Parameters
    ----------
    global_max_ops : float
        Max orders per second across ALL strategies combined.
    """

    def __init__(self, global_max_ops: float = 10.0) -> None:
        self._global_bucket = _TokenBucket(global_max_ops)
        self._strategy_buckets: dict[str, _TokenBucket] = {}
        self._circuit_breakers: dict[str, _CircuitBreaker] = {}
        self._global_lock = asyncio.Lock()

    def configure_strategy(self, strategy_id: str, max_ops_per_second: float) -> None:
        """Register per-strategy rate limit (called when strategy registers)."""
        self._strategy_buckets[strategy_id] = _TokenBucket(max_ops_per_second)
        self._circuit_breakers[strategy_id] = _CircuitBreaker()
        log.debug(f"RateLimiter: strategy {strategy_id!r} → {max_ops_per_second}/s")

    def check_and_consume(self, strategy_id: str) -> tuple[bool, str]:
        """
        Check rate limits and consume a token.

        Returns (True, "") if allowed, (False, reason) if denied.
        Note: global bucket uses a lock-free approximation (monotonic token refill).
        """
        # Circuit breaker check
        cb = self._circuit_breakers.get(strategy_id)
        if cb and cb.is_open():
            return False, f"Circuit breaker open for strategy {strategy_id!r}"

        # Per-strategy bucket
        bucket = self._strategy_buckets.get(strategy_id)
        if bucket and not bucket.consume():
            return False, f"Per-strategy rate limit exceeded for {strategy_id!r}"

        # Global bucket
        if not self._global_bucket.consume():
            # Refund the per-strategy token if global fails
            if bucket:
                bucket._tokens = min(bucket._rate, bucket._tokens + 1.0)
            return False, "Global order rate limit exceeded"

        return True, ""

    def record_hl_rejection(self, strategy_id: str) -> None:
        """Call when HL returns a 4xx/5xx. May open circuit breaker."""
        cb = self._circuit_breakers.get(strategy_id)
        if cb:
            cb.record_rejection(strategy_id)

    def record_hl_success(self, strategy_id: str) -> None:
        """Call on successful order submission."""
        cb = self._circuit_breakers.get(strategy_id)
        if cb:
            cb.record_success()
