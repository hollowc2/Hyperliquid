"""
APEX Strategy Live Monitor
==========================
Reads the JSON state file written by ApexStrategy and renders a live
terminal dashboard using `rich`.

Usage (from hl_engine/ directory):
    uv run python hl_engine/monitor.py
    uv run python hl_engine/monitor.py --state data/apex_state.json
    uv run python hl_engine/monitor.py --refresh 0.5

Multi-strategy mode (reads from orchestrator REST API):
    uv run python hl_engine/monitor.py --multi
    uv run python hl_engine/monitor.py --multi --url http://localhost:8000
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


DEFAULT_STATE_FILE = "data/apex_state.json"
DEFAULT_REFRESH_HZ = 2.0  # renders per second


def _age_str(ts_iso: str) -> str:
    """Return human-readable age of an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 2:
            return "[green]live[/green]"
        if age < 10:
            return f"[yellow]{age:.0f}s ago[/yellow]"
        return f"[red]{age:.0f}s ago[/red]"
    except Exception:
        return "?"


def _signed(val: float, precision: int = 4, suffix: str = "") -> Text:
    s = f"{val:+.{precision}f}{suffix}"
    return Text(s, style="green" if val >= 0 else "red")


def build_dashboard(state: dict, state_file: str, err: str | None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── Header ────────────────────────────────────────────────────────
    if err:
        header_text = Text(f"  APEX Monitor  |  {err}", style="bold red on dark_red")
    else:
        ts_age = _age_str(state.get("ts", ""))
        instrument = state.get("instrument", "?")
        regime = state.get("regime", "?")
        mid = state.get("mid_px", 0.0)
        regime_color = {"TRENDING": "cyan", "RANGING": "yellow", "VOLATILE": "magenta"}.get(regime, "white")
        header_text = Text()
        header_text.append(f"  APEX Monitor  ", style="bold white on navy_blue")
        header_text.append(f"  {instrument}  ", style="bold cyan")
        header_text.append(f"  ${mid:,.2f}  ", style="bold white")
        header_text.append(f"  Regime: ", style="white")
        header_text.append(f"{regime}  ", style=f"bold {regime_color}")
        header_text.append(f"  Updated: {ts_age}  ", style="dim")
    layout["header"].update(Panel(header_text, border_style="bright_blue"))

    # ── Left column: Position + Account ──────────────────────────────
    pos = state.get("position", {})
    acct_table = Table(show_header=False, box=None, padding=(0, 1))
    acct_table.add_column("key", style="dim", width=18)
    acct_table.add_column("val", justify="right")

    balance = state.get("balance", 0.0)
    acct_table.add_row("Balance", f"[bold white]${balance:,.2f}[/bold white]")
    acct_table.add_row("Trade count", str(state.get("trade_count", 0)))
    commission = state.get("total_commission", 0.0)
    acct_table.add_row("Total commission", f"[red]-${commission:,.4f}[/red]")

    if pos:
        side = pos.get("side", "?")
        side_style = "green" if side == "BUY" else "red"
        qty = pos.get("qty", 0.0)
        avg_px = pos.get("avg_px", 0.0)
        unreal = pos.get("unrealized_pnl", 0.0)
        real = pos.get("realized_pnl", 0.0)
        dur = pos.get("duration_s", 0.0)
        acct_table.add_row("", "")
        acct_table.add_row("Position side", f"[bold {side_style}]{side}[/bold {side_style}]")
        acct_table.add_row("Qty", f"{qty:.5f}")
        acct_table.add_row("Avg entry", f"${avg_px:,.2f}")
        acct_table.add_row("Unrealized PnL", str(_signed(unreal, 4, " USDC")))
        acct_table.add_row("Realized PnL", str(_signed(real, 4, " USDC")))
        acct_table.add_row("Open duration", f"{dur:.0f}s")
    else:
        acct_table.add_row("", "")
        acct_table.add_row("Position", "[dim]flat[/dim]")

    layout["left"].update(Panel(acct_table, title="Account / Position", border_style="bright_blue"))

    # ── Right column: Features ────────────────────────────────────────
    feats = state.get("features", {})
    feat_table = Table(show_header=True, box=None, padding=(0, 1))
    feat_table.add_column("Feature", style="dim", width=14)
    feat_table.add_column("Value", justify="right", width=12)
    feat_table.add_column("Bar", width=20)

    def bar(val: float, lo: float = -1.0, hi: float = 1.0, width: int = 16) -> str:
        """Mini ASCII progress bar centered at 0."""
        clamped = max(lo, min(hi, val))
        frac = (clamped - lo) / (hi - lo)
        filled = int(frac * width)
        mid_i = width // 2
        bar_chars = [" "] * width
        center = width // 2
        if filled >= center:
            for i in range(center, filled):
                bar_chars[i] = "█"
        else:
            for i in range(filled, center):
                bar_chars[i] = "█"
        bar_str = "".join(bar_chars)
        color = "green" if val >= 0 else "red"
        return f"[{color}]{''.join(bar_chars[:center])}[/{color}][dim]|[/dim][{color}]{''.join(bar_chars[center:])}[/{color}]"

    def feat_row(name: str, val: float, lo: float = -1.0, hi: float = 1.0, precision: int = 4):
        color = "green" if val >= 0 else "red"
        feat_table.add_row(name, f"[{color}]{val:+.{precision}f}[/{color}]", bar(val, lo, hi))

    feat_row("OBI", feats.get("obi", 0.0))
    feat_row("TFI", feats.get("tfi", 0.0))
    feat_row("MP drift", feats.get("mp_drift", 0.0), -0.001, 0.001, 8)
    feat_row("Hawkes", feats.get("hawkes", 0.0), 0.0, 1.0)
    feat_row("Cascade", feats.get("cascade", 0.0), 0.0, 2.0)
    feat_row("Funding", feats.get("funding", 0.0))
    feat_row("Spread", feats.get("spread", 0.0), 0.0, 0.01, 6)
    feat_row("Vol (short)", feats.get("vol_short", 0.0), 0.0, 0.01, 6)

    edge = state.get("last_edge", 0.0)
    feat_table.add_row("", "", "")
    edge_color = "green" if edge >= 0 else "red"
    feat_table.add_row("Last edge", f"[bold {edge_color}]{edge:+.4f}[/bold {edge_color}]", "")

    active = state.get("active_order")
    feat_table.add_row(
        "Active order",
        "[yellow]YES[/yellow]" if active else "[dim]none[/dim]",
        "",
    )

    layout["right"].update(Panel(feat_table, title="Features & Signal", border_style="bright_blue"))

    # ── Footer: Last order ────────────────────────────────────────────
    lo = state.get("last_order", {})
    if lo:
        side = lo.get("side", "?")
        side_style = "green" if side == "BUY" else "red"
        qty = lo.get("qty", 0.0)
        price = lo.get("price")
        lo_edge = lo.get("edge", 0.0)
        lo_regime = lo.get("regime", "?")
        price_str = f"${price:,.2f}" if price else "MARKET"
        footer_text = Text()
        footer_text.append("  Last order: ", style="dim")
        footer_text.append(f"{side} ", style=f"bold {side_style}")
        footer_text.append(f"{qty:.5f} @ {price_str}", style="white")
        footer_text.append(f"  edge={lo_edge:+.4f}", style="cyan")
        footer_text.append(f"  regime={lo_regime}", style="yellow")
    else:
        footer_text = Text("  No orders yet this session", style="dim")
    layout["footer"].update(Panel(footer_text, title="Last Submitted Order", border_style="bright_blue"))

    return layout


def load_state(path: str) -> tuple[dict, str | None]:
    try:
        with open(path) as f:
            return json.load(f), None
    except FileNotFoundError:
        return {}, f"State file not found: {path}  (is the trader running?)"
    except json.JSONDecodeError as e:
        return {}, f"JSON parse error: {e}"
    except Exception as e:
        return {}, f"Read error: {e}"


# ── Multi-strategy dashboard ──────────────────────────────────────────────────

def _fetch_multi_state(base_url: str) -> tuple[list, dict, str | None]:
    """Fetch strategy list and risk summary from orchestrator."""
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            strategies_resp = client.get("/strategies")
            strategies_resp.raise_for_status()
            risk_resp = client.get("/risk")
            risk_resp.raise_for_status()
            return strategies_resp.json(), risk_resp.json(), None
    except httpx.ConnectError:
        return [], {}, f"Cannot connect to orchestrator at {base_url}"
    except Exception as e:
        return [], {}, str(e)


def build_multi_dashboard(strategies: list, risk: dict, base_url: str, err: str | None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=5),
    )

    # ── Header ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if err:
        header_text = Text(f"  Multi-Strategy Monitor  |  {err}", style="bold red on dark_red")
    else:
        global_notional = risk.get("global_notional_usd", 0.0)
        global_ceiling = risk.get("global_ceiling_usd", 0.0)
        util = (global_notional / global_ceiling * 100) if global_ceiling else 0.0
        util_color = "green" if util < 70 else "yellow" if util < 90 else "red"
        header_text = Text()
        header_text.append("  Multi-Strategy Monitor  ", style="bold white on navy_blue")
        header_text.append(f"  {base_url}  ", style="dim")
        header_text.append(f"  Global: ${global_notional:,.0f} / ${global_ceiling:,.0f}  ", style="white")
        header_text.append(f"({util:.1f}% utilized)  ", style=f"bold {util_color}")
        header_text.append(f"  {now}  ", style="dim")
    layout["header"].update(Panel(header_text, border_style="bright_blue"))

    # ── Body: strategy table ──────────────────────────────────────────
    strat_table = Table(show_lines=True, expand=True)
    strat_table.add_column("ID", style="bold cyan", no_wrap=True)
    strat_table.add_column("Container", style="dim")
    strat_table.add_column("Status", justify="center")
    strat_table.add_column("Instance", style="dim", justify="center")
    strat_table.add_column("Notional USD", justify="right")
    strat_table.add_column("Max USD", justify="right", style="dim")
    strat_table.add_column("Utilization", justify="right")
    strat_table.add_column("Circuit Breaker", justify="center")
    strat_table.add_column("Registered At", style="dim")

    per_strategy_risk = risk.get("strategies", {})

    for s in strategies:
        sid = s.get("id", "?")
        container = s.get("container_name", "?")
        raw_status = s.get("status", "unknown")
        instance = (s.get("instance_id") or "")[:8] or "—"
        registered_at = s.get("registered_at") or "—"

        if raw_status == "running":
            status_text = Text("● running", style="green")
        elif raw_status == "stopped":
            status_text = Text("○ stopped", style="yellow")
        elif raw_status == "not_started":
            status_text = Text("— not started", style="dim")
        else:
            status_text = Text(f"? {raw_status}", style="red")

        sr = per_strategy_risk.get(sid, {})
        notional = sr.get("notional_usd", 0.0)
        max_usd = sr.get("max_position_usd", 0.0)
        util = (notional / max_usd * 100) if max_usd else 0.0
        util_color = "green" if util < 70 else "yellow" if util < 90 else "red"
        cb_open = sr.get("circuit_breaker_open", False)
        cb_text = Text("OPEN", style="bold red") if cb_open else Text("closed", style="dim green")

        strat_table.add_row(
            sid,
            container,
            status_text,
            instance,
            f"${notional:,.2f}",
            f"${max_usd:,.2f}" if max_usd else "—",
            f"[{util_color}]{util:.1f}%[/{util_color}]" if max_usd else "—",
            cb_text,
            registered_at,
        )

    if not strategies:
        strat_table.add_row("[dim]No strategies registered[/dim]", *[""] * 8)

    layout["body"].update(Panel(strat_table, title="Strategies", border_style="bright_blue"))

    # ── Footer: help text ─────────────────────────────────────────────
    help_text = Text()
    help_text.append("  Commands: ", style="dim")
    help_text.append("hl start <id>", style="cyan")
    help_text.append("  ", style="")
    help_text.append("hl stop <id>", style="cyan")
    help_text.append("  ", style="")
    help_text.append("hl logs <id>", style="cyan")
    help_text.append("  ", style="")
    help_text.append("hl risk", style="cyan")
    help_text.append("    Press [Ctrl+C] to exit", style="dim")
    layout["footer"].update(Panel(help_text, title="Controls", border_style="dim"))

    return layout


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Strategy Live Monitor")
    parser.add_argument(
        "--state",
        default=os.getenv("APEX_STATE_FILE", DEFAULT_STATE_FILE),
        help=f"Path to apex_state.json (default: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=DEFAULT_REFRESH_HZ,
        help=f"Refresh rate in Hz (default: {DEFAULT_REFRESH_HZ})",
    )
    parser.add_argument(
        "--multi",
        action="store_true",
        help="Multi-strategy mode: read from orchestrator REST API",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("ORCHESTRATOR_REST_URL", "http://localhost:8000"),
        help="Orchestrator REST URL for --multi mode (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    console = Console()
    refresh_interval = 1.0 / max(0.1, args.refresh)

    if args.multi:
        with Live(console=console, refresh_per_second=args.refresh, screen=True) as live:
            while True:
                strategies, risk_data, err = _fetch_multi_state(args.url)
                layout = build_multi_dashboard(strategies, risk_data, args.url, err)
                live.update(layout)
                time.sleep(refresh_interval)
    else:
        with Live(console=console, refresh_per_second=args.refresh, screen=True) as live:
            while True:
                state, err = load_state(args.state)
                layout = build_dashboard(state, args.state, err)
                live.update(layout)
                time.sleep(refresh_interval)


if __name__ == "__main__":
    main()
