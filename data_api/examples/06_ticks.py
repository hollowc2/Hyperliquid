"""
Tick Data — Live crypto prices and tick data for tracked symbols.
"""

import sys
import os
from datetime import datetime

# Add parent directory to path so we can import api.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api import HyperliquidPublicAPI

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.layout import Layout
from rich import box
from rich.align import Align


SYMBOLS = ["BTC", "ETH", "HYPE", "SOL", "XRP"]
SYMBOL_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "HYPE": "Hyperliquid",
    "SOL": "Solana",
    "XRP": "XRP"
}


def format_price(price):
    """Format price with commas and proper decimals"""
    if price is None:
        return "N/A"
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    else:
        return f"${price:,.6f}"


def create_sparkline(prices, width=20):
    """Create ASCII sparkline from price data"""
    if not prices or len(prices) < 2:
        return "▄" * width

    min_price = min(prices)
    max_price = max(prices)

    if max_price == min_price:
        return "▄" * min(len(prices), width)

    chars = "▁▂▃▄▅▆▇█"
    sparkline = ""

    # Sample prices to fit width
    step = max(1, len(prices) // width)
    sampled = prices[::step][:width]

    for price in sampled:
        normalized = (price - min_price) / (max_price - min_price)
        index = int(normalized * (len(chars) - 1))
        sparkline += chars[index]

    return sparkline


def main():
    """Tick data dashboard entry point"""
    console = Console()

    console.rule("[bold]Tick Data[/bold]")
    console.print("[dim]Connecting to Hyperliquid public API...[/dim]")
    api = HyperliquidPublicAPI()
    console.print(f"[green]Connected (no key required)[/green]")
    console.print("[dim]Fetching tick data...[/dim]")

    stats_data = api.get_tick_stats()
    latest_data = api.get_tick_latest()
    # Get symbol stats from stats_data
    symbol_stats = stats_data.get('symbol_stats', {}) if stats_data else {}
    latest_prices = latest_data.get('prices', {}) if latest_data else {}

    # ==================== COLLECTION STATS ====================
    if stats_data:
        collector = stats_data.get('collector_stats', {})
        ticks_collected = collector.get('ticks_collected', 0)
        stats_content = Text()
        stats_content.append("📊 Ticks: ", style="cyan")
        stats_content.append(f"{ticks_collected:,}", style="bold yellow")
        stats_content.append("  Symbols: ", style="cyan")
        stats_content.append(f"{', '.join(SYMBOLS)}", style="bold white")
        console.print(Panel(
            stats_content,
            title="[bold yellow]Collection Stats[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1)
        ))

    # ==================== LIVE PRICES TABLE ====================
    price_table = Table(
        title="[bold magenta]Live Prices[/bold magenta]",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold magenta",
        show_lines=False,
        padding=(0, 1)
    )

    price_table.add_column("Symbol", style="bold white", justify="center", width=8)
    price_table.add_column("Name", style="cyan", width=12)
    price_table.add_column("Current Price", style="bold green", justify="right", width=16)
    price_table.add_column("📉 24h Low", style="red", justify="right", width=14)
    price_table.add_column("📈 24h High", style="green", justify="right", width=14)
    price_table.add_column("📊 Sparkline", justify="center", width=22)
    price_table.add_column("Ticks", style="dim", justify="right", width=8)

    for symbol in SYMBOLS:
        name = SYMBOL_NAMES.get(symbol, symbol)

        # Get current price from latest
        current_price = latest_prices.get(symbol, 0)

        # Get stats for this symbol
        sym_stats = symbol_stats.get(symbol, {})
        min_price = sym_stats.get('min_price', 0)
        max_price = sym_stats.get('max_price', 0)
        tick_count = sym_stats.get('tick_count', 0)

        # Create mini sparkline from min/current/max
        if min_price and max_price and current_price:
            # Simple 3-point sparkline
            prices = [min_price, (min_price + max_price) / 2, current_price]
            sparkline = create_sparkline(prices, 10)
            if current_price >= (min_price + max_price) / 2:
                sparkline_display = f"[green]{sparkline}[/]"
            else:
                sparkline_display = f"[red]{sparkline}[/]"
        else:
            sparkline_display = "[dim]▄▄▄▄▄▄▄▄▄▄[/]"

        price_table.add_row(
            f"[bold]{symbol}[/]",
            name,
            format_price(current_price),
            format_price(min_price) if min_price else "N/A",
            format_price(max_price) if max_price else "N/A",
            sparkline_display,
            f"{tick_count:,}"
        )
    console.print(price_table)

    # ==================== RECENT TICKS TABLE ====================
    console.print("[bold magenta]Recent Tick Data[/bold magenta]")

    for symbol in SYMBOLS:
        tick_response = api.get_ticks(symbol.lower(), "1h")
        if tick_response and isinstance(tick_response, dict):
            ticks = tick_response.get('ticks', [])
            if ticks:
                recent_ticks = ticks[-10:] if len(ticks) >= 10 else ticks
                tick_table = Table(
                    title=f"[bold cyan]{symbol}[/] ({len(recent_ticks)} ticks)",
                    box=box.SIMPLE,
                    border_style="dim",
                    header_style="bold white",
                    show_lines=False,
                    padding=(0, 1)
                )
                tick_table.add_column("Time", style="dim", width=10)
                tick_table.add_column("Price", style="bold green", justify="right", width=14)
                tick_table.add_column("Chg", justify="right", width=10)
                prev_price = None
                for tick in recent_ticks:
                    price = tick.get('price', 0)
                    timestamp = tick.get('datetime', tick.get('timestamp', ''))
                    try:
                        if isinstance(timestamp, str):
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            time_str = dt.strftime("%H:%M:%S")
                        elif isinstance(timestamp, (int, float)):
                            dt = datetime.fromtimestamp(timestamp / 1000)
                            time_str = dt.strftime("%H:%M:%S")
                        else:
                            time_str = "N/A"
                    except:
                        time_str = "N/A"
                    if prev_price:
                        change = price - prev_price
                        pct = (change / prev_price) * 100 if prev_price else 0
                        if change > 0:
                            change_str = f"[green]+{pct:.3f}%[/]"
                        elif change < 0:
                            change_str = f"[red]{pct:.3f}%[/]"
                        else:
                            change_str = "[dim]0.000%[/]"
                    else:
                        change_str = "[dim]—[/]"
                    tick_table.add_row(time_str, format_price(price), change_str)
                    prev_price = price
                console.print(tick_table)

    # ==================== FOOTER ====================
    console.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")


if __name__ == "__main__":
    main()
