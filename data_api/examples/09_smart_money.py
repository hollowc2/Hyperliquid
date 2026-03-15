"""
Smart Money — Leaderboard, wallet rankings, and trading signals across timeframes.
"""

import sys
import os
from datetime import datetime

# Add parent directory to path for api import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api import HyperliquidPublicAPI

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box
from rich.columns import Columns

# Initialize Rich console
console = Console()


def format_pnl(pnl):
    """Format PnL with color and $ formatting"""
    if pnl is None:
        return Text("N/A", style="dim")

    if isinstance(pnl, str):
        pnl = float(pnl.replace(',', '').replace('$', ''))

    # Format with commas
    formatted = f"${abs(pnl):,.0f}"

    if pnl >= 0:
        return Text(f"+{formatted}", style="bold green")
    else:
        return Text(f"-{formatted}", style="bold red")


def format_pnl_large(pnl, rank=None):
    """Format PnL for leaderboard with special styling for top 3"""
    if pnl is None:
        return Text("N/A", style="dim")

    if isinstance(pnl, str):
        pnl = float(pnl.replace(',', '').replace('$', ''))

    formatted = f"${abs(pnl):,.0f}"

    # Special styling for top 3
    if rank and rank <= 3:
        if pnl >= 0:
            return Text(f"+{formatted}", style="bold yellow")
        else:
            return Text(f"-{formatted}", style="bold red")
    else:
        if pnl >= 0:
            return Text(f"+{formatted}", style="bold green")
        else:
            return Text(f"-{formatted}", style="bold red")


def get_rank_emoji(rank):
    """Get emoji for rank position"""
    if rank == 1:
        return "🥇"
    elif rank == 2:
        return "🥈"
    elif rank == 3:
        return "🥉"
    else:
        return "  "


def create_leaderboard_table(leaderboard_data):
    """Create the Top 10 Performers leaderboard table"""
    table = Table(
        title="🏆 Top 10 Smart Money Performers",
        box=box.ROUNDED,
        border_style="yellow",
        header_style="bold magenta",
        title_style="bold yellow",
        padding=(0, 1),
    )

    table.add_column("Rank", justify="center", style="bold", width=6)
    table.add_column("", justify="center", width=3)  # Medal emoji
    table.add_column("Address", justify="left", style="cyan", width=45)
    table.add_column("💰 Total PnL", justify="right", width=18)
    table.add_column("Win Rate", justify="center", width=10)
    table.add_column("Trades", justify="center", width=8)

    # Extract leaderboard list
    if isinstance(leaderboard_data, dict):
        leaders = leaderboard_data.get('leaderboard', leaderboard_data.get('top', leaderboard_data.get('traders', [])))
    elif isinstance(leaderboard_data, list):
        leaders = leaderboard_data
    else:
        leaders = []

    # Show top 10
    for i, leader in enumerate(leaders[:10], 1):
        # Extract data with fallbacks
        address = leader.get('address', leader.get('wallet', leader.get('user', 'Unknown')))
        pnl = leader.get('pnl', leader.get('total_pnl', leader.get('totalPnl', leader.get('allTime', 0))))
        win_rate = leader.get('win_rate', leader.get('winRate', leader.get('win_pct', None)))
        trades = leader.get('trades', leader.get('total_trades', leader.get('tradeCount', 'N/A')))

        # Format values
        rank_emoji = get_rank_emoji(i)
        full_addr = address if address else "Unknown"
        formatted_pnl = format_pnl_large(pnl, rank=i)

        # Format win rate
        if win_rate is not None:
            if isinstance(win_rate, (int, float)):
                if win_rate > 1:  # Already percentage
                    win_rate_str = f"{win_rate:.1f}%"
                else:  # Decimal
                    win_rate_str = f"{win_rate * 100:.1f}%"
            else:
                win_rate_str = str(win_rate)
        else:
            win_rate_str = "N/A"

        # Format trades
        if isinstance(trades, (int, float)):
            trades_str = f"{int(trades):,}"
        else:
            trades_str = str(trades)

        # Row styling for top 3
        row_style = "bold" if i <= 3 else ""

        table.add_row(
            f"#{i}",
            rank_emoji,
            full_addr,
            formatted_pnl,
            win_rate_str,
            trades_str,
            style=row_style,
        )

    return table


def create_rankings_panel(rankings_data):
    """Create Smart vs Dumb Money panel"""
    # Extract data
    if isinstance(rankings_data, dict):
        smart_money = rankings_data.get('smart_money', rankings_data.get('top', rankings_data.get('smart', [])))
        dumb_money = rankings_data.get('dumb_money', rankings_data.get('bottom', rankings_data.get('dumb', [])))
    else:
        smart_money = []
        dumb_money = []

    smart_count = len(smart_money) if isinstance(smart_money, list) else 0
    dumb_count = len(dumb_money) if isinstance(dumb_money, list) else 0

    # Create content
    content = Text()
    content.append("Smart Money\n", style="bold green")
    content.append(f"   {smart_count} Wallets Tracked\n", style="green")
    content.append("Dumb Money\n", style="bold red")
    content.append(f"   {dumb_count} Wallets Tracked", style="red")

    panel = Panel(
        Align.center(content),
        title="[bold cyan]📊 Wallet Rankings[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    )

    return panel


def create_signals_table(signals_10m, signals_1h, signals_24h):
    """Create trading signals table"""
    table = Table(
        title="📡 Smart Money Trading Signals",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold magenta",
        padding=(0, 1),
    )

    table.add_column("Timeframe", justify="center", style="bold yellow", width=12)
    table.add_column("Signal", justify="center", width=15)
    table.add_column("Coin", justify="center", style="cyan", width=10)
    table.add_column("Direction", justify="center", width=12)
    table.add_column("Confidence", justify="center", width=12)
    table.add_column("Smart $ Flow", justify="right", width=15)

    # Process each timeframe
    timeframes = [
        ("10m", signals_10m),
        ("1h", signals_1h),
        ("24h", signals_24h),
    ]

    for tf_name, signals_data in timeframes:
        # Extract signals list
        if isinstance(signals_data, dict):
            signals = signals_data.get('signals', signals_data.get('data', []))
        elif isinstance(signals_data, list):
            signals = signals_data
        else:
            signals = []

        if not signals:
            # No signals for this timeframe
            table.add_row(
                f"⏰ {tf_name}",
                Text("No Signals", style="dim"),
                "-",
                "-",
                "-",
                "-",
            )
        else:
            # Show top signal for each timeframe
            for signal in signals[:2]:  # Show up to 2 signals per timeframe
                coin = signal.get('coin', signal.get('symbol', signal.get('asset', 'N/A')))
                direction = signal.get('direction', signal.get('side', signal.get('signal', 'N/A')))
                confidence = signal.get('confidence', signal.get('strength', signal.get('score', None)))
                flow = signal.get('flow', signal.get('volume', signal.get('smart_flow', None)))

                # Format direction with color
                if direction and isinstance(direction, str):
                    direction_upper = direction.upper()
                    if 'BUY' in direction_upper or 'LONG' in direction_upper:
                        direction_text = Text("BUY", style="bold green")
                    elif 'SELL' in direction_upper or 'SHORT' in direction_upper:
                        direction_text = Text("SELL", style="bold red")
                    else:
                        direction_text = Text(direction, style="yellow")
                else:
                    direction_text = Text("N/A", style="dim")

                # Format confidence
                if confidence is not None:
                    if isinstance(confidence, (int, float)):
                        if confidence <= 1:
                            conf_pct = confidence * 100
                        else:
                            conf_pct = confidence
                        conf_text = f"{conf_pct:.0f}%"
                    else:
                        conf_text = str(confidence)
                else:
                    conf_text = "N/A"

                # Format flow
                if flow is not None:
                    if isinstance(flow, (int, float)):
                        flow_text = f"${abs(flow):,.0f}"
                    else:
                        flow_text = str(flow)
                else:
                    flow_text = "N/A"

                table.add_row(
                    f"⏰ {tf_name}",
                    Text("ACTIVE", style="bold green"),
                    str(coin),
                    direction_text,
                    conf_text,
                    flow_text,
                )

    return table


def create_summary_stats(leaderboard_data, rankings_data):
    """Create summary statistics panel"""
    # Calculate total PnL from top 10
    if isinstance(leaderboard_data, dict):
        leaders = leaderboard_data.get('leaderboard', leaderboard_data.get('top', leaderboard_data.get('traders', [])))
    elif isinstance(leaderboard_data, list):
        leaders = leaderboard_data
    else:
        leaders = []

    total_pnl = 0
    for leader in leaders[:10]:
        pnl = leader.get('pnl', leader.get('total_pnl', leader.get('totalPnl', leader.get('allTime', 0))))
        if isinstance(pnl, (int, float)):
            total_pnl += pnl
        elif isinstance(pnl, str):
            total_pnl += float(pnl.replace(',', '').replace('$', ''))

    # Extract rankings counts
    if isinstance(rankings_data, dict):
        smart_money = rankings_data.get('smart_money', rankings_data.get('top', rankings_data.get('smart', [])))
        dumb_money = rankings_data.get('dumb_money', rankings_data.get('bottom', rankings_data.get('dumb', [])))
    else:
        smart_money = []
        dumb_money = []

    smart_count = len(smart_money) if isinstance(smart_money, list) else 0
    dumb_count = len(dumb_money) if isinstance(dumb_money, list) else 0

    # Create summary content
    content = Text()
    content.append("Top 10 Combined PnL: ", style="bold white")
    content.append(f"${total_pnl:,.0f}\n", style="bold green" if total_pnl >= 0 else "bold red")
    content.append(f"Smart Wallets: {smart_count}   |   ", style="green")
    content.append(f"Dumb Wallets: {dumb_count}", style="red")

    panel = Panel(
        Align.center(content),
        title="[bold yellow]📊 Summary Statistics[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    )

    return panel


def main():
    """Smart money dashboard entry point"""
    console.rule("[bold]Smart Money[/bold]")

    # Initialize API
    api = HyperliquidPublicAPI()
    console.print("[dim]Connected to Hyperliquid public API (no key required)[/dim]")
    console.print("[dim]Fetching smart money data...[/dim]")

    leaderboard_data = {}
    console.print("[dim]  Fetching leaderboard...[/dim]")
    try:
        leaderboard_data = api.get_smart_money_leaderboard()
    except NotImplementedError as e:
        console.print(f"[yellow]  ℹ️  Leaderboard: {e}[/yellow]")

    rankings_data = {}
    console.print("[dim]  Fetching rankings...[/dim]")
    try:
        rankings_data = api.get_smart_money_rankings()
    except NotImplementedError as e:
        console.print(f"[yellow]  ℹ️  Rankings: {e}[/yellow]")

    signals_10m = {}
    console.print("[dim]  Fetching 10m signals...[/dim]")
    try:
        signals_10m = api.get_smart_money_signals("10m")
    except NotImplementedError as e:
        console.print(f"[yellow]  ℹ️  Signals (10m): {e}[/yellow]")

    signals_1h = {}
    console.print("[dim]  Fetching 1h signals...[/dim]")
    try:
        signals_1h = api.get_smart_money_signals("1h")
    except NotImplementedError as e:
        console.print(f"[yellow]  ℹ️  Signals (1h): {e}[/yellow]")

    signals_24h = {}
    console.print("[dim]  Fetching 24h signals...[/dim]")
    try:
        signals_24h = api.get_smart_money_signals("24h")
    except NotImplementedError as e:
        console.print(f"[yellow]  ℹ️  Signals (24h): {e}[/yellow]")

    console.print("[bold green]Fetch complete (stub methods shown above)[/bold green]")
    console.print("─" * 80)

    # Leaderboard table
    console.print(create_leaderboard_table(leaderboard_data))

    # Rankings and Summary side by side
    console.print(Columns([
        create_rankings_panel(rankings_data),
        create_summary_stats(leaderboard_data, rankings_data),
    ], equal=True, expand=True))

    # Signals table
    console.print(create_signals_table(signals_10m, signals_1h, signals_24h))

    console.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")


if __name__ == "__main__":
    main()
