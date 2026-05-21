"""
Prometheus metrics for the HL orchestrator.

All metrics are defined here and imported by the modules that update them.
The /metrics endpoint in app.py serves these via prometheus_client.generate_latest().

Metrics exported:
  Counters:
    hl_orders_submitted_total{strategy, side, order_type}
    hl_orders_rejected_total{strategy, reason}
      reason = rate_limit | risk | circuit_breaker | hl_error
    hl_orders_canceled_total{strategy, instrument, reason}
    hl_fills_total{strategy, side}
    hl_circuit_breaker_trips_total{strategy}
    hl_commissions_paid_usd_total{strategy, currency}

  Gauges:
    hl_strategy_state{strategy}         (1=running/registered, 0=stopped)
    hl_account_equity_usd{strategy, currency}
    hl_account_balance_usd{strategy, currency}
    hl_realized_pnl_usd{strategy, currency}
    hl_unrealized_pnl_usd{strategy, currency}
    hl_drawdown_pct{strategy}
    hl_net_exposure_qty{strategy, instrument}
    hl_notional_reserved_usd{strategy}
    hl_global_notional_usd
    hl_global_ceiling_usd
    hl_circuit_breaker_open{strategy}   (0=closed, 1=open)
    hl_vclimax_phase{strategy, phase}
    hl_vclimax_active_stop{strategy, instrument}
    hl_vclimax_entry_price{strategy, instrument}
    hl_vclimax_climax_high{strategy, instrument}
    hl_vclimax_climax_low{strategy, instrument}
    hl_vclimax_bars_since_climax{strategy}

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

orders_canceled = Counter(
    "hl_orders_canceled_total",
    "Total orders canceled by strategy",
    ["strategy", "instrument", "reason"],
)

fills_total = Counter(
    "hl_fills_total",
    "Total fills received from Hyperliquid",
    ["strategy", "side"],
)

commissions_paid = Counter(
    "hl_commissions_paid_usd_total",
    "Total commissions paid by strategy",
    ["strategy", "currency"],
)

circuit_breaker_trips = Counter(
    "hl_circuit_breaker_trips_total",
    "Number of times the circuit breaker has opened per strategy",
    ["strategy"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

strategy_state = Gauge(
    "hl_strategy_state",
    "Strategy runtime state: 1 if running/registered, 0 if stopped",
    ["strategy"],
)

account_equity = Gauge(
    "hl_account_equity_usd",
    "Current account equity for the strategy-attached wallet",
    ["strategy", "currency"],
)

account_balance = Gauge(
    "hl_account_balance_usd",
    "Current account balance for the strategy-attached wallet",
    ["strategy", "currency"],
)

realized_pnl = Gauge(
    "hl_realized_pnl_usd",
    "Current realized PnL by strategy",
    ["strategy", "currency"],
)

unrealized_pnl = Gauge(
    "hl_unrealized_pnl_usd",
    "Current unrealized PnL by strategy",
    ["strategy", "currency"],
)

drawdown_pct = Gauge(
    "hl_drawdown_pct",
    "Current drawdown percentage from peak equity by strategy",
    ["strategy"],
)

net_exposure_qty = Gauge(
    "hl_net_exposure_qty",
    "Current signed net exposure quantity by strategy and instrument",
    ["strategy", "instrument"],
)

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

vclimax_phase = Gauge(
    "hl_vclimax_phase",
    "VClimax phase as one-hot gauges by strategy",
    ["strategy", "phase"],
)

vclimax_active_stop = Gauge(
    "hl_vclimax_active_stop",
    "Current VClimax active stop price",
    ["strategy", "instrument"],
)

vclimax_entry_price = Gauge(
    "hl_vclimax_entry_price",
    "Current VClimax entry price",
    ["strategy", "instrument"],
)

vclimax_climax_high = Gauge(
    "hl_vclimax_climax_high",
    "Last VClimax detected climax high",
    ["strategy", "instrument"],
)

vclimax_climax_low = Gauge(
    "hl_vclimax_climax_low",
    "Last VClimax detected climax low",
    ["strategy", "instrument"],
)

vclimax_bars_since_climax = Gauge(
    "hl_vclimax_bars_since_climax",
    "Bars elapsed since the active VClimax event",
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

# ---------------------------------------------------------------------------
# Initialisation helper
# ---------------------------------------------------------------------------

_SIDES = ("buy", "sell")
_ORDER_TYPES = ("limit", "market")
_REJECT_REASONS = ("rate_limit", "risk", "circuit_breaker", "hl_error")
_CURRENCIES = ("USDC", "USD")
_VCLIMAX_PHASES = (
    "SEARCHING",
    "PENDING_ENTRY",
    "ENTERING",
    "IN_POSITION_PHASE_1",
    "IN_POSITION_PHASE_2",
    "EXITING",
)


def init_strategy_metrics(strategy_id: str) -> None:
    """
    Pre-register all strategy-scoped label combinations so they appear in
    /metrics as zero-valued series immediately after a strategy connects,
    rather than only after the first event fires.

    Call this whenever a new strategy_id is registered with the orchestrator.
    """
    for side in _SIDES:
        fills_total.labels(strategy=strategy_id, side=side)
        for order_type in _ORDER_TYPES:
            orders_submitted.labels(strategy=strategy_id, side=side, order_type=order_type)

    for reason in _REJECT_REASONS:
        orders_rejected.labels(strategy=strategy_id, reason=reason)

    strategy_state.labels(strategy=strategy_id)
    drawdown_pct.labels(strategy=strategy_id)
    vclimax_bars_since_climax.labels(strategy=strategy_id)

    for currency in _CURRENCIES:
        commissions_paid.labels(strategy=strategy_id, currency=currency)
        account_equity.labels(strategy=strategy_id, currency=currency)
        account_balance.labels(strategy=strategy_id, currency=currency)
        realized_pnl.labels(strategy=strategy_id, currency=currency)
        unrealized_pnl.labels(strategy=strategy_id, currency=currency)

    for phase in _VCLIMAX_PHASES:
        vclimax_phase.labels(strategy=strategy_id, phase=phase).set(0)

    circuit_breaker_trips.labels(strategy=strategy_id)
    circuit_breaker_open.labels(strategy=strategy_id).set(0)

    fill_latency.labels(strategy=strategy_id)
