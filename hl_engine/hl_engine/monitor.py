"""
APEX Strategy Live Monitor
==========================
Reads the JSON state file written by ApexStrategy and renders a live
terminal dashboard using `rich`.

Usage (from hl_engine/ directory):
    uv run python hl_engine/monitor.py
    uv run python hl_engine/monitor.py --state data/apex_state.json
    uv run python hl_engine/monitor.py --refresh 0.5

Multi-strategy mode (Textual TUI, reads from orchestrator REST API):
    uv run python hl_engine/monitor.py --multi
    uv run python hl_engine/monitor.py --multi --url http://localhost:8100
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
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


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


def _ascii_bar(val: float, lo: float = -1.0, hi: float = 1.0, width: int = 16) -> str:
    """Centered ASCII bar for feature visualization."""
    clamped = max(lo, min(hi, val))
    frac = (clamped - lo) / (hi - lo)
    filled = int(frac * width)
    center = width // 2
    bar_chars = [" "] * width
    if filled >= center:
        for i in range(center, min(filled, width)):
            bar_chars[i] = "█"
    else:
        for i in range(max(0, filled), center):
            bar_chars[i] = "█"
    color = "green" if val >= 0 else "red"
    left_half = "".join(bar_chars[:center])
    right_half = "".join(bar_chars[center:])
    return f"[{color}]{left_half}[/{color}][dim]|[/dim][{color}]{right_half}[/{color}]"


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
        header_text.append("  Updated: ", style="dim")
        header_text.append_text(Text.from_markup(ts_age))
        header_text.append("  ", style="dim")
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


# ── Textual TUI (--multi mode) ────────────────────────────────────────────────

class OrchestratorApp(App):
    """Textual TUI for multi-strategy monitoring."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #top-bar {
        height: 3;
        background: navy;
        color: white;
        padding: 0 1;
        content-align: left middle;
    }
    #main-body {
        height: 1fr;
    }
    #strategy-list-pane {
        width: 30%;
        height: 100%;
        overflow-y: auto;
    }
    #detail-pane {
        width: 1fr;
        height: 100%;
        overflow-y: auto;
    }
    #bottom-bar {
        height: 3;
        padding: 0 1;
        content-align: left middle;
        border-top: solid $accent;
    }
    """

    BINDINGS = [
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("s", "start_strategy", "Start"),
        ("x", "stop_strategy", "Stop"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._strategies: list = []
        self._risk: dict = {}
        self._selected_index: int = 0
        self._strategy_state: dict = {}
        self._state_avail: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="top-bar")
        with Horizontal(id="main-body"):
            yield Static("", id="strategy-list-pane")
            yield Static("", id="detail-pane")
        yield Static("", id="bottom-bar")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._poll_overview)
        self.set_interval(0.5, self._poll_state)
        self._render_footer()

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_overview(self) -> None:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=3.0) as client:
                sr = await client.get("/strategies")
                sr.raise_for_status()
                rr = await client.get("/risk")
                rr.raise_for_status()
                self._strategies = sr.json()
                self._risk = rr.json()
        except Exception:
            pass
        self._render_header()
        self._render_strategy_list()

    async def _poll_state(self) -> None:
        s = self._selected_strategy()
        if not s:
            self._render_detail(None)
            return
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=3.0) as client:
                resp = await client.get(f"/strategies/{s['id']}/state")
                if resp.status_code == 200:
                    self._strategy_state = resp.json()
                    self._state_avail = True
                else:
                    self._strategy_state = {}
                    self._state_avail = False
        except Exception:
            self._strategy_state = {}
            self._state_avail = False
        self._render_detail(s)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_header(self) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        txt = Text()
        if not self._risk:
            txt.append("  Multi-Strategy Monitor  ", style="bold white on navy_blue")
            txt.append(f"  {self._base_url}  ", style="dim")
            txt.append(f"  {now}  ", style="dim")
        else:
            notional = self._risk.get("global_notional_usd", 0.0)
            ceiling = self._risk.get("global_ceiling_usd", 0.0)
            util = (notional / ceiling * 100) if ceiling else 0.0
            util_color = "green" if util < 70 else "yellow" if util < 90 else "red"
            txt.append("  Multi-Strategy Monitor  ", style="bold white on navy_blue")
            txt.append(f"  {self._base_url}  ", style="dim")
            txt.append(f"  Global: ${notional:,.0f} / ${ceiling:,.0f}  ", style="white")
            txt.append(f"({util:.1f}%)  ", style=f"bold {util_color}")
            txt.append(f"  {now}  ", style="dim")
        self.query_one("#top-bar", Static).update(txt)

    def _render_strategy_list(self) -> None:
        lines = Text()
        for i, s in enumerate(self._strategies):
            is_sel = (i == self._selected_index)
            status = s.get("status", "stopped")
            registered = s.get("registered", False)
            sid = s.get("id", "?")

            if status == "running" and registered:
                dot, dot_style = "●", "bold green"
            elif status == "running":
                dot, dot_style = "●", "bold yellow"
            else:
                dot, dot_style = "○", "dim white"

            cursor = "▶ " if is_sel else "  "
            name_style = "bold white on blue" if is_sel else "white"
            lines.append(cursor, style="bold cyan" if is_sel else "dim")
            lines.append(f"{dot} ", style=dot_style)
            lines.append(f"{sid}\n", style=name_style)

        if not self._strategies:
            lines.append("  No strategies found\n", style="dim")

        panel = Panel(lines, title="Strategies", border_style="bright_blue")
        self.query_one("#strategy-list-pane", Static).update(panel)

    def _render_detail(self, strategy: dict | None) -> None:
        if strategy is None:
            panel = Panel(
                Text("No strategy selected", style="dim"),
                title="Detail",
                border_style="dim",
            )
            self.query_one("#detail-pane", Static).update(panel)
            return

        sid = strategy.get("id", "?")
        content = (
            self._build_state_detail(self._strategy_state)
            if self._state_avail and self._strategy_state
            else self._build_generic_detail(strategy)
        )
        panel = Panel(content, title=f"[bold]{sid}[/bold]", border_style="bright_blue")
        self.query_one("#detail-pane", Static).update(panel)

    def _build_state_detail(self, state: dict):
        """Two-column detail: Account/Position + Features/Signal."""
        pos = state.get("position", {})
        left = Table(show_header=False, box=None, padding=(0, 1))
        left.add_column("k", style="dim", width=16)
        left.add_column("v", justify="right")

        balance = state.get("balance", 0.0)
        left.add_row("Balance", f"[bold white]${balance:,.2f}[/bold white]")
        left.add_row("Trades", str(state.get("trade_count", 0)))
        commission = state.get("total_commission", 0.0)
        left.add_row("Commission", f"[red]-${commission:,.4f}[/red]")

        if pos:
            side = pos.get("side", "?")
            sc = "green" if side == "BUY" else "red"
            left.add_row("", "")
            left.add_row("Side", f"[bold {sc}]{side}[/bold {sc}]")
            left.add_row("Qty", f"{pos.get('qty', 0.0):.5f}")
            left.add_row("Avg entry", f"${pos.get('avg_px', 0.0):,.2f}")
            unreal = pos.get("unrealized_pnl", 0.0)
            real = pos.get("realized_pnl", 0.0)
            left.add_row("Unreal PnL", str(_signed(unreal, 4, " USDC")))
            left.add_row("Real PnL", str(_signed(real, 4, " USDC")))
            left.add_row("Duration", f"{pos.get('duration_s', 0.0):.0f}s")
        else:
            left.add_row("", "")
            left.add_row("Position", "[dim]flat[/dim]")

        left_panel = Panel(left, title="Account / Position", border_style="blue")

        feats = state.get("features", {})
        right = Table(show_header=True, box=None, padding=(0, 1))
        right.add_column("Feature", style="dim", width=12)
        right.add_column("Value", justify="right", width=12)
        right.add_column("Bar", width=18)

        def _feat_row(name: str, val: float, lo: float = -1.0, hi: float = 1.0, prec: int = 4):
            c = "green" if val >= 0 else "red"
            right.add_row(name, f"[{c}]{val:+.{prec}f}[/{c}]", _ascii_bar(val, lo, hi))

        _feat_row("OBI", feats.get("obi", 0.0))
        _feat_row("TFI", feats.get("tfi", 0.0))
        _feat_row("MP drift", feats.get("mp_drift", 0.0), -0.001, 0.001, 8)
        _feat_row("Hawkes", feats.get("hawkes", 0.0), 0.0, 1.0)
        _feat_row("Cascade", feats.get("cascade", 0.0), 0.0, 2.0)
        _feat_row("Funding", feats.get("funding", 0.0))
        _feat_row("Spread", feats.get("spread", 0.0), 0.0, 0.01, 6)
        _feat_row("Vol short", feats.get("vol_short", 0.0), 0.0, 0.01, 6)

        edge = state.get("last_edge", 0.0)
        right.add_row("", "", "")
        ec = "green" if edge >= 0 else "red"
        right.add_row("Last edge", f"[bold {ec}]{edge:+.4f}[/bold {ec}]", "")
        active = state.get("active_order")
        right.add_row(
            "Active order",
            "[yellow]YES[/yellow]" if active else "[dim]none[/dim]",
            "",
        )

        right_panel = Panel(right, title="Features & Signal", border_style="blue")
        return Columns([left_panel, right_panel])

    def _build_generic_detail(self, strategy: dict):
        """Generic detail panel shown when no state has been pushed."""
        t = Table(show_header=False, box=None, padding=(0, 1))
        t.add_column("k", style="dim", width=18)
        t.add_column("v")
        t.add_row("ID", f"[bold cyan]{strategy.get('id', '?')}[/bold cyan]")
        t.add_row("Status", strategy.get("status", "?"))
        instance = strategy.get("instance_id") or "—"
        t.add_row("Instance", instance[:16])
        t.add_row("Container", strategy.get("container_name", "?"))
        t.add_row("Class", strategy.get("class", "?"))
        registered = strategy.get("registered", False)
        t.add_row("Registered", "[green]Yes[/green]" if registered else "[dim]No[/dim]")
        max_usd = strategy.get("risk_limit_usd", 0.0)
        t.add_row("Risk limit", f"${max_usd:,.0f}" if max_usd else "—")
        t.add_row("", "")
        t.add_row("State", "[dim]No state pushed yet[/dim]")
        return t

    def _render_footer(self) -> None:
        txt = Text()
        txt.append("  ↑↓", style="bold cyan")
        txt.append(" navigate  ", style="dim")
        txt.append("s", style="bold cyan")
        txt.append(" start  ", style="dim")
        txt.append("x", style="bold cyan")
        txt.append(" stop  ", style="dim")
        txt.append("q / Ctrl+C", style="bold cyan")
        txt.append(" quit", style="dim")
        self.query_one("#bottom-bar", Static).update(txt)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _selected_strategy(self) -> dict | None:
        if not self._strategies:
            return None
        idx = max(0, min(self._selected_index, len(self._strategies) - 1))
        return self._strategies[idx]

    def action_cursor_up(self) -> None:
        if self._strategies and self._selected_index > 0:
            self._selected_index -= 1
            self._render_strategy_list()

    def action_cursor_down(self) -> None:
        if self._strategies and self._selected_index < len(self._strategies) - 1:
            self._selected_index += 1
            self._render_strategy_list()

    async def action_start_strategy(self) -> None:
        s = self._selected_strategy()
        if s:
            try:
                async with httpx.AsyncClient(base_url=self._base_url, timeout=5.0) as client:
                    await client.post(f"/strategies/{s['id']}/start")
            except Exception:
                pass

    async def action_stop_strategy(self) -> None:
        s = self._selected_strategy()
        if s:
            try:
                async with httpx.AsyncClient(base_url=self._base_url, timeout=5.0) as client:
                    await client.post(f"/strategies/{s['id']}/stop")
            except Exception:
                pass


# ── Entry point ───────────────────────────────────────────────────────────────

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
        help="Multi-strategy mode: Textual TUI reading from orchestrator REST API",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("ORCHESTRATOR_REST_URL", "http://localhost:8000"),
        help="Orchestrator REST URL for --multi mode (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    if args.multi:
        OrchestratorApp(base_url=args.url).run()
    else:
        console = Console()
        refresh_interval = 1.0 / max(0.1, args.refresh)
        with Live(console=console, refresh_per_second=args.refresh, screen=True) as live:
            try:
                while True:
                    state, err = load_state(args.state)
                    layout = build_dashboard(state, args.state, err)
                    live.update(layout)
                    time.sleep(refresh_interval)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
