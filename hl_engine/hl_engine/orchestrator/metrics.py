"""
Prometheus metrics for the HL orchestrator.

All metrics are defined here and imported by the modules that update them.
The /metrics endpoint in app.py serves these via prometheus_client.generate_latest().

Metrics exported:
  Counters:
    hl_orders_submitted_total{strategy, side, order_type}
    hl_orders_rejected_total{strategy, reason}
      reason = rate_limit | risk | circuit_breaker | hl_error
    hl_fills_total{strategy, side}
    hl_circuit_breaker_trips_total{strategy}

  Gauges:
    hl_notional_reserved_usd{strategy}
    hl_global_notional_usd
    hl_global_ceiling_usd
    hl_circuit_breaker_open{strategy}   (0=closed, 1=open)

  Histograms:
    hl_fill_latency_ms{strategy}        (order submit → fill received)
"""

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

orders_submitted = Counter(
    "hl_orders_submitted_total",
    "Total orders successfully submitted to Hyperliquid",
    ["strategy", "side", "order_type"],
)

orders_rejected = Counter(
    "hl_orders_rejected_total",
    "Total orders rejected before reaching Hyperliquid",
    ["strategy", "reason"],
)

fills_total = Counter(
    "hl_fills_total",
    "Total fills received from Hyperliquid",
    ["strategy", "side"],
)

circuit_breaker_trips = Counter(
    "hl_circuit_breaker_trips_total",
    "Number of times the circuit breaker has opened per strategy",
    ["strategy"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

notional_reserved = Gauge(
    "hl_notional_reserved_usd",
    "Current notional exposure reserved per strategy (USD)",
    ["strategy"],
)

global_notional = Gauge(
    "hl_global_notional_usd",
    "Total notional exposure across all strategies (USD)",
)

global_ceiling = Gauge(
    "hl_global_ceiling_usd",
    "Configured global notional ceiling (USD)",
)

circuit_breaker_open = Gauge(
    "hl_circuit_breaker_open",
    "1 if the circuit breaker is currently open for this strategy, 0 if closed",
    ["strategy"],
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

fill_latency = Histogram(
    "hl_fill_latency_ms",
    "Latency from order submission to fill receipt (milliseconds)",
    ["strategy"],
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)
