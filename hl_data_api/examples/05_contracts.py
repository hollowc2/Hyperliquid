#!/usr/bin/env python3
"""
Contract Registry — Hyperliquid contract metadata and high-value contract tracking.
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
from rich.layout import Layout
from rich.columns import Columns
from rich import box

# Initialize Rich console
console = Console()

# Contract type colors
TYPE_COLORS = {
    "system": "bright_cyan",
    "trading": "bright_green",
    "token": "bright_yellow",
    "bridge": "bright_magenta",
    "oracle": "bright_red",
    "staking": "orange1",
    "governance": "purple",
    "nft": "pink1",
    "defi": "cyan",
    "default": "white"
}


def get_type_color(contract_type: str) -> str:
    """Get color for contract type"""
    if not contract_type:
        return TYPE_COLORS["default"]
    contract_type = contract_type.lower()
    for key, color in TYPE_COLORS.items():
        if key in contract_type:
            return color
    return TYPE_COLORS["default"]


def create_stats_panel(contracts_data: dict) -> Panel:
    """Create statistics panel"""
    contracts = contracts_data.get('contracts', [])
    high_value_count = contracts_data.get('high_value_count', 0)
    active_count = sum(1 for c in contracts if c.get('is_active', c.get('active', False)))

    # Count by type
    type_counts = {}
    for contract in contracts:
        ctype = contract.get('type', contract.get('contract_type', 'unknown'))
        type_counts[ctype] = type_counts.get(ctype, 0) + 1

    stats_text = Text()
    stats_text.append("📊 ", style="bright_yellow")
    stats_text.append("TOTAL CONTRACTS: ", style="dim white")
    stats_text.append(f"{len(contracts):,}", style="bold bright_green")
    stats_text.append("  |  ", style="dim white")
    stats_text.append("⭐ ", style="bright_yellow")
    stats_text.append("HIGH VALUE: ", style="dim white")
    stats_text.append(f"{high_value_count:,}", style="bold bright_magenta")
    stats_text.append("  |  ", style="dim white")
    stats_text.append("🔥 ", style="bright_red")
    stats_text.append("ACTIVE: ", style="dim white")
    stats_text.append(f"{active_count:,}", style="bold bright_cyan")

    return Panel(
        stats_text,
        box=box.ROUNDED,
        border_style="bright_yellow",
        title="[bold bright_cyan]📈 Registry Stats[/bold bright_cyan]",
        padding=(0, 1)
    )


def create_type_legend() -> Panel:
    """Create contract type legend"""
    legend_text = Text()
    legend_text.append("CONTRACT TYPES: ", style="bold white")

    type_icons = {
        "system": "⚙️",
        "trading": "💹",
        "token": "🪙",
        "bridge": "🌉",
        "oracle": "🔮",
        "staking": "💎",
        "governance": "🏛️",
        "nft": "🖼️",
        "defi": "🏦"
    }

    for i, (type_name, color) in enumerate(TYPE_COLORS.items()):
        if type_name == "default":
            continue
        icon = type_icons.get(type_name, "📄")
        legend_text.append(f"{icon} ", style=color)
        legend_text.append(type_name.upper(), style=f"bold {color}")
        if i < len(TYPE_COLORS) - 2:
            legend_text.append("  ", style="dim white")

    return Panel(
        legend_text,
        box=box.ROUNDED,
        border_style="dim white",
        padding=(0, 1)
    )


def create_contracts_table(contracts_data: dict) -> Table:
    """Create the main contracts table"""
    table = Table(
        box=box.ROUNDED,
        border_style="bright_cyan",
        header_style="bold bright_magenta",
        title="[bold bright_yellow]Contract Registry[/bold bright_yellow]",
        title_style="bold",
        padding=(0, 1)
    )

    # Add columns
    table.add_column("Status", justify="center", style="dim", width=8)
    table.add_column("Address", style="cyan", width=46)
    table.add_column("Name", style="bold white", width=20)
    table.add_column("Type", justify="center", width=12)
    table.add_column("Description", style="dim white", width=40)
    table.add_column("Activity", justify="center", width=12)

    contracts = contracts_data.get('contracts', [])

    for contract in contracts:
        # Get contract data
        address = contract.get('address', contract.get('contract_address', 'Unknown'))
        name = contract.get('name', contract.get('contract_name', 'Unnamed'))
        ctype = contract.get('type', contract.get('contract_type', 'unknown'))
        description = contract.get('description', contract.get('desc', 'No description'))
        is_high_value = contract.get('is_high_value', contract.get('high_value', False))
        is_active = contract.get('is_active', contract.get('active', False))

        # Activity info
        activity = contract.get('activity', contract.get('activity_tracking', {}))
        if isinstance(activity, dict):
            tx_count = activity.get('tx_count', activity.get('transactions', 0))
            activity_str = f"{tx_count:,} txns" if tx_count else "—"
        else:
            activity_str = str(activity) if activity else "—"

        # Status icons
        status_parts = []
        if is_high_value:
            status_parts.append("⭐")
        if is_active:
            status_parts.append("🔥")
        if not status_parts:
            status_parts.append("📜")
        status = " ".join(status_parts)

        # Get type color
        type_color = get_type_color(ctype)

        # Truncate description if too long
        if description and len(description) > 38:
            description = description[:35] + "..."

        # Add row with styling
        table.add_row(
            status,
            Text(address if address else "N/A", style="bright_cyan"),
            Text(name[:18] + "..." if len(name) > 20 else name, style="bold white" if is_high_value else "white"),
            Text(ctype.upper() if ctype else "—", style=f"bold {type_color}"),
            Text(description or "—", style="dim"),
            Text(activity_str, style="bright_green" if is_active else "dim")
        )

    return table


def create_high_value_panel(contracts_data: dict) -> Panel:
    """Create high-value contracts highlight panel"""
    contracts = contracts_data.get('contracts', [])
    high_value_contracts = [c for c in contracts if c.get('is_high_value', c.get('high_value', False))]

    if not high_value_contracts:
        content = Text("No high-value contracts found", style="dim italic")
    else:
        content = Text()
        for i, contract in enumerate(high_value_contracts[:5]):
            name = contract.get('name', contract.get('contract_name', 'Unnamed'))
            address = contract.get('address', contract.get('contract_address', ''))
            ctype = contract.get('type', contract.get('contract_type', 'unknown'))

            content.append("⭐ ", style="bright_yellow")
            content.append(f"{name}", style="bold bright_cyan")
            content.append(f" ({ctype})", style=f"dim {get_type_color(ctype)}")
            content.append(f"\n   {address if address else 'N/A'}", style="dim white")
            if i < min(len(high_value_contracts), 5) - 1:
                content.append("\n")

    return Panel(
        content,
        box=box.ROUNDED,
        border_style="bright_yellow",
        title="[bold bright_yellow]⭐ High Value Contracts[/bold bright_yellow]",
        padding=(0, 1)
    )


def main():
    """Contract registry entry point"""
    console.rule("[bold]Contract Registry[/bold]")

    console.print("[dim]Connecting to Hyperliquid public API...[/dim]")
    api = HyperliquidPublicAPI()
    console.print("[dim]Connected (no key required)[/dim]")
    console.print("[dim]Fetching contract registry...[/dim]")

    try:
        contracts_data = api.get_contracts()
    except NotImplementedError as e:
        console.print(Panel(
            f"[yellow]ℹ️  {e}[/yellow]",
            border_style="yellow",
            title="[yellow]Contract Registry Unavailable[/yellow]",
            padding=(0, 1)
        ))
        return

    if not contracts_data:
        console.print(Panel(
            "[bold red]No contract data returned from API[/bold red]",
            border_style="red",
            title="📜 Data Error",
            padding=(0, 1)
        ))
        return

    console.print(create_stats_panel(contracts_data))
    console.print(create_type_legend())
    console.print(create_high_value_panel(contracts_data))
    console.print(create_contracts_table(contracts_data))
    console.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")


if __name__ == "__main__":
    main()
