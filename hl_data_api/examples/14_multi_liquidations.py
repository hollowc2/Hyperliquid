"""
Multi-Exchange Liquidation Comparison — Side-by-side liquidation data across exchanges.
  - Hyperliquid  — via HyperliquidPublicAPI (free, no auth)
  - Binance      — via HyperliquidPublicAPI (free, no auth)
  - Bybit        — N/A (free endpoint not available)
  - OKX          — N/A (free endpoint not available)

Run with: python examples/14_multi_liquidations.py
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import HyperliquidPublicAPI
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich import box

console = Console()

NA_STYLE = "[dim]N/A — free endpoint not available[/dim]"


# ==================== HELPERS ====================
def format_usd(value):
    if value is None or value == 0:
        return "$0"
    if abs(value) >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif abs(value) >= 1_000:
        return f"${value/1_000:.1f}K"
    return f"${value:,.0f}"


def format_count(value):
    if value is None or value == 0:
        return "0"
    if value >= 1_000_000:
        return f"{value/1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value/1_000:.1f}K"
    return f"{value:,}"


# ==================== EXCHANGE STATUS ====================
def display_exchange_status(api):
    """Show which exchanges are connected vs. N/A."""
    console.print(Panel(
        "📡 Exchange Data Availability",
        border_style="green", padding=(0, 1)
    ))

    exchanges = [
        ("💎 Hyperliquid", "cyan",    True,  "Direct HL public API"),
        ("🟡 Binance",     "dim",     False, "forceOrders requires API key auth"),
        ("🟠 Bybit",       "dim",     False, "No free liquidation endpoint"),
        ("⚪ OKX",         "dim",     False, "No free liquidation endpoint"),
    ]

    panels = []
    for name, color, available, note in exchanges:
        if available:
            status = "[green]✅ AVAILABLE[/green]"
        else:
            status = "[dim]⛔ UNAVAILABLE[/dim]"
        panels.append(Panel(
            f"[bold {color}]{name}[/bold {color}]\n{status}\n[dim]{note}[/dim]",
            border_style=color if available else "dim",
            padding=(0, 1)
        ))
    console.print(Columns(panels, equal=True, expand=True))


# ==================== TIMEFRAME COMPARISON ====================
def display_timeframe_comparison(api):
    """Side-by-side liquidation counts by timeframe."""
    console.print(Panel(
        "⏰ Liquidations by Timeframe",
        border_style="yellow", padding=(0, 1)
    ))

    table = Table(
        box=box.DOUBLE_EDGE, border_style="yellow",
        header_style="bold cyan", padding=(0, 1), expand=True
    )
    table.add_column("TF", style="bold cyan", justify="center", width=8)
    table.add_column("💎 Hyperliquid (count)", style="cyan", justify="right", width=24)
    table.add_column("🟡 Binance", style="dim", justify="center", width=22)
    table.add_column("🟠 Bybit", style="dim", justify="center", width=14)
    table.add_column("⚪ OKX", style="dim", justify="center", width=14)
    table.add_column("🔥 HL Total", style="bold red", justify="right", width=16)

    for tf in ["10m", "1h", "4h", "24h"]:
        row = [f"[bold]{tf}[/bold]"]
        combined = 0

        # Hyperliquid
        try:
            data = api.get_liquidations(tf)
            stats_data = data.get("stats", {})
            count = stats_data.get("total_count", 0)
            value = stats_data.get("total_value_usd", 0)
            count_cell = f"[cyan]{format_count(count)}[/cyan] ({format_usd(value)})"
            if stats_data.get("capped"):
                actual_coverage_min = round(stats_data.get("actual_coverage_ms", 0) / 60_000)
                count_cell += f" [dim]~{actual_coverage_min}m[/dim]"
            row.append(count_cell)
            combined += count
        except Exception:
            row.append("[dim]error[/dim]")

        # Binance / Bybit / OKX — all unavailable
        row.append(NA_STYLE)
        row.append(NA_STYLE)
        row.append(NA_STYLE)
        row.append(f"[bold red]{format_count(combined)}[/bold red]")

        table.add_row(*row)

    console.print(table)
    console.print("[dim]Binance/Bybit/OKX: no free public liquidation endpoints available.[/dim]")


# ==================== STATS COMPARISON ====================
def display_stats_comparison(api):
    """Side-by-side 24h stats for HL and Binance."""
    console.print(Panel(
        "📊 24h Liquidation Stats Comparison",
        border_style="bright_white", padding=(0, 1)
    ))

    # Hyperliquid stats
    hl_panel_lines = ["[bold cyan]💎 HYPERLIQUID[/bold cyan]", ""]
    try:
        stats = api.get_liquidation_stats()
        w24 = stats.get("windows", {}).get("24h", {})
        hl_panel_lines += [
            f"[bold cyan]Count:[/bold cyan] [white]{w24.get('total_count', 0):,}[/white]",
            f"[bold cyan]Volume:[/bold cyan] [yellow]{format_usd(w24.get('total_value_usd', 0))}[/yellow]",
            f"[bold green]Longs:[/bold green] [green]{w24.get('long_count', 0):,}[/green]",
            f"[bold red]Shorts:[/bold red] [red]{w24.get('short_count', 0):,}[/red]",
            "",
            f"[dim]Note: HLP Strategy A only; ADL excluded[/dim]",
        ]
    except Exception as e:
        hl_panel_lines.append(f"[red]Error: {e}[/red]")

    hl_panel = Panel("\n".join(hl_panel_lines), border_style="cyan", padding=(0, 1))

    bn_panel = Panel(
        "[dim]🟡 BINANCE FUTURES\n\nforceOrders endpoint now requires\nAPI key authentication.\nNo free public endpoint available.[/dim]",
        border_style="dim", padding=(0, 1)
    )

    # N/A panels
    bybit_panel = Panel(
        "[dim]🟠 BYBIT\n\nNo free public liquidation\nendpoint available.[/dim]",
        border_style="dim", padding=(0, 1)
    )
    okx_panel = Panel(
        "[dim]⚪ OKX\n\nNo free public liquidation\nendpoint available.[/dim]",
        border_style="dim", padding=(0, 1)
    )

    console.print(Columns([hl_panel, bn_panel, bybit_panel, okx_panel], equal=True, expand=True))


# ==================== TOP LIQUIDATIONS (HL + BN COMBINED) ====================
def display_top_liquidations(api):
    """Largest liquidation events from HL and Binance combined."""
    console.print(Panel(
        "🏆 Top Liquidations — HL + Binance (1h)",
        border_style="red", padding=(0, 1)
    ))

    all_liqs = []

    try:
        hl_data = api.get_liquidations("1h")
        for liq in hl_data.get("stats", {}).get("largest", []):
            liq["_exchange"] = "Hyperliquid"
            all_liqs.append(liq)
    except Exception:
        pass

    if not all_liqs:
        console.print("[dim]No liquidation events found in 1h window.[/dim]")
        return

    all_liqs.sort(key=lambda x: x.get("value_usd", x.get("value", 0)), reverse=True)

    table = Table(
        box=box.ROUNDED, border_style="red",
        header_style="bold yellow", padding=(0, 1), expand=True
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Exchange", justify="center", width=14)
    table.add_column("Coin", style="cyan", justify="center", width=8)
    table.add_column("Value", style="yellow", justify="right", width=14)
    table.add_column("Side", justify="center", width=10)
    table.add_column("Price", style="white", justify="right", width=12)
    table.add_column("⏰ Time", style="dim", width=12)

    for i, liq in enumerate(all_liqs[:20], 1):
        exchange = liq.get("_exchange", "?")
        coin = liq.get("coin", liq.get("symbol", "?")).replace("USDT", "")
        value = float(liq.get("value_usd", liq.get("value", 0)))
        side = liq.get("side", "?")
        price = float(liq.get("price", 0))
        timestamp = liq.get("timestamp", liq.get("time", 0))

        ex_style = "[cyan]💎 HL[/cyan]" if exchange == "Hyperliquid" else "[yellow]🟡 BN[/yellow]"
        side_display = "[green]📈 Long[/green]" if str(side).lower() in ["long"] else "[red]📉 Short[/red]"
        rank = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i)

        if timestamp:
            try:
                ts = timestamp / 1000 if timestamp > 1e10 else timestamp
                time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            except Exception:
                time_str = "N/A"
        else:
            time_str = "N/A"

        table.add_row(
            rank, ex_style, coin[:8],
            f"[bold]{format_usd(value)}[/bold]",
            side_display,
            f"${price:,.2f}" if price else "N/A",
            time_str
        )

    console.print(table)


# ==================== MAIN ====================
def main():
    console.rule("[bold]Multi-Exchange Liquidations[/bold]")

    console.print("Connecting...")
    api = HyperliquidPublicAPI()
    console.print("[green]Connected to Hyperliquid public API (no key required)[/green]")
    console.print()

    display_exchange_status(api)
    console.print()
    display_stats_comparison(api)
    console.print()
    display_timeframe_comparison(api)
    console.print()
    display_top_liquidations(api)

    console.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")


if __name__ == "__main__":
    main()
