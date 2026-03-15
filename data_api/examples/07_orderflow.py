"""
Order Flow — Real-time order flow intelligence across timeframes and coins.
"""
import sys, os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api import HyperliquidPublicAPI
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()


def format_volume(value):
    """Format volume with K/M/B suffixes"""
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value/1_000:.1f}K"
    else:
        return f"${value:.2f}"


def format_delta(value):
    """Format delta with color indication"""
    if value > 0:
        return f"[green]+{format_volume(value)}[/]"
    elif value < 0:
        return f"[red]{format_volume(value)}[/]"
    else:
        return f"[dim]$0[/]"


def create_pressure_bar(buy_pressure, width=20):
    """Create visual buy/sell pressure bar"""
    pct = buy_pressure * 100
    filled = int(width * buy_pressure)
    empty = width - filled

    if pct >= 55:
        color = "green"
        label = "BUYERS"
    elif pct <= 45:
        color = "red"
        label = "SELLERS"
    else:
        color = "yellow"
        label = "NEUTRAL"

    bar = f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"
    return bar, f"{pct:.1f}%", label


def main():
    console.rule("[bold]Order Flow[/bold]")
    api = HyperliquidPublicAPI()
    console.print("[green]Connected to Hyperliquid public API[/]")
    stats = api.get_orderflow_stats()
    orderflow = api.get_orderflow()
    if not stats or not orderflow:
        console.print("[red]Failed to fetch order flow data[/]")
        return

    # OVERVIEW STATS
    total_trades = stats.get('total_trades')  # may be None
    total_volume = stats.get('total_volume_usd', 0)
    buy_volume = stats.get('buy_volume_usd', 0)
    sell_volume = stats.get('sell_volume_usd', 0)
    trades_per_sec = stats.get('trades_per_second')  # may be None
    overall_buy_pct = (buy_volume / total_volume * 100) if total_volume > 0 else 50

    overview = Text()
    overview.append("Vol: ", style="cyan")
    overview.append(f"{format_volume(total_volume)}", style="bold yellow")
    overview.append(" | Buy: ", style="green")
    overview.append(f"{format_volume(buy_volume)} ({overall_buy_pct:.1f}%)", style="bold green")
    overview.append(" | Sell: ", style="red")
    overview.append(f"{format_volume(sell_volume)} ({100-overall_buy_pct:.1f}%)", style="bold red")

    console.print(Panel(overview, title="[bold yellow]Order Flow Overview[/bold yellow]", border_style="yellow", box=box.ROUNDED, padding=(0, 1)))

    # TIMEFRAME TABLE
    windows = orderflow.get('windows', {})
    tf_table = Table(title="[bold magenta]Order Flow by Timeframe[/bold magenta]", box=box.SIMPLE, border_style="cyan", header_style="bold white", padding=(0, 1))
    tf_table.add_column("TF", style="bold cyan", justify="center", width=4)
    tf_table.add_column("Buy Pressure", justify="center", width=26)
    tf_table.add_column("Delta", style="white", justify="right", width=12)
    tf_table.add_column("Side", justify="center", width=8)

    for tf in ['5m', '15m', '1h', '4h']:
        data = windows.get(tf, {})
        buy_pressure = data.get('buy_pressure', 0.5)
        delta = data.get('cumulative_delta', 0)
        dominant = data.get('dominant_side', 'NEUTRAL')
        bar, pct_str, _ = create_pressure_bar(buy_pressure)
        if dominant == 'BUY':
            side_display = "[bold green]BUY[/]"
        elif dominant == 'SELL':
            side_display = "[bold red]SELL[/]"
        else:
            side_display = "[yellow]NEUT[/]"
        tf_table.add_row(tf, f"{bar} {pct_str}", format_delta(delta), side_display)

    console.print(tf_table)

    # PER COIN TABLE
    by_coin = orderflow.get('by_coin', {})
    coin_table = Table(title="[bold magenta]Order Flow by Coin[/bold magenta]", box=box.SIMPLE, border_style="cyan", header_style="bold white", padding=(0, 1))
    coin_table.add_column("Coin", style="bold white", justify="center", width=6)
    coin_table.add_column("Buy Pressure", justify="center", width=26)
    coin_table.add_column("Delta", justify="right", width=12)
    coin_table.add_column("Bias", justify="center", width=8)

    coin_emojis = {'BTC': '₿', 'ETH': 'Ξ', 'HYPE': '~', 'SOL': '◎', 'XRP': '✕'}
    for coin in ['BTC', 'ETH', 'HYPE', 'SOL', 'XRP']:
        data = by_coin.get(coin, {})
        buy_pressure = data.get('buy_pressure', 0.5)
        delta = data.get('cumulative_delta', 0)
        bar, pct_str, bias = create_pressure_bar(buy_pressure)
        if bias == "BUYERS":
            bias_display = "[bold green]BULL[/]"
        elif bias == "SELLERS":
            bias_display = "[bold red]BEAR[/]"
        else:
            bias_display = "[yellow]NEUT[/]"
        coin_table.add_row(f"{coin_emojis.get(coin, '•')}{coin}", f"{bar} {pct_str}", format_delta(delta), bias_display)

    console.print(coin_table)

    # RECENT TRADES
    trades_data = api.get_trades()
    trades = trades_data.get('trades', []) if trades_data else []
    if trades:
        trades_table = Table(title="[bold magenta]Recent Trades[/bold magenta]", box=box.SIMPLE, border_style="cyan", header_style="bold white", padding=(0, 1))
        trades_table.add_column("Time", style="dim", width=8)
        trades_table.add_column("Coin", style="bold white", justify="center", width=5)
        trades_table.add_column("Side", justify="center", width=5)
        trades_table.add_column("Size", justify="right", width=10)
        trades_table.add_column("Price", justify="right", width=12)
        trades_table.add_column("Value", style="yellow", justify="right", width=10)

        for trade in trades[:15]:
            timestamp = trade.get('timestamp', '')
            coin = trade.get('coin', '?')
            side = trade.get('side', '?')
            size = trade.get('size', 0)
            price = trade.get('price', 0)
            value = trade.get('value_usd', 0)
            try:
                if isinstance(timestamp, (int, float)):
                    time_str = datetime.fromtimestamp(timestamp / 1000 if timestamp > 1e10 else timestamp).strftime("%H:%M:%S")
                else:
                    dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
                    time_str = dt.strftime("%H:%M:%S")
            except:
                time_str = "N/A"
            side_display = "[green]BUY[/]" if side == 'BUY' else "[red]SELL[/]"
            if price >= 1000:
                price_str = f"${price:,.2f}"
            elif price >= 1:
                price_str = f"${price:,.4f}"
            else:
                price_str = f"${price:,.6f}"
            trades_table.add_row(time_str, coin, side_display, f"{size:,.4f}", price_str, format_volume(value))

        console.print(trades_table)

    console.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")

if __name__ == "__main__":
    main()
