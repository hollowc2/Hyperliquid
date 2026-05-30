"""
hl — CLI tool for managing the multi-strategy trading platform.

Commands:
  hl start <id>      Start a strategy container via orchestrator
  hl stop <id>       Stop a strategy container
  hl status          Show all strategy container statuses
  hl logs <id>       Stream logs from a strategy container
  hl risk            Show global risk summary

All commands communicate with the orchestrator REST API.
ORCHESTRATOR_REST_URL env var sets the base URL (default: http://localhost:8000).
"""

import os

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    name="hl",
    help="Manage Hyperliquid multi-strategy trading platform",
    no_args_is_help=True,
)
console = Console()


def _base_url() -> str:
    return os.getenv("ORCHESTRATOR_REST_URL", "http://localhost:8000").rstrip("/")


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=15.0)


def _err(msg: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(1)


@app.command()
def start(
    strategy_id: str = typer.Argument(..., help="Strategy ID from YAML config (e.g. ma-cross-btc)")
) -> None:
    """Start a strategy container."""
    with _client() as client:
        try:
            resp = client.post(f"/strategies/{strategy_id}/start")
            resp.raise_for_status()
            data = resp.json()
            container = data.get("container_name", strategy_id)
            instance = data.get("instance_id", "")[:8]
            console.print(f"[green]Started[/green] [bold]{container}[/bold]  instance=[dim]{instance}[/dim]")
        except httpx.HTTPStatusError as e:
            _err(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")


@app.command()
def stop(
    strategy_id: str = typer.Argument(..., help="Strategy ID to stop")
) -> None:
    """Stop a strategy container."""
    with _client() as client:
        try:
            resp = client.post(f"/strategies/{strategy_id}/stop")
            resp.raise_for_status()
            data = resp.json()
            console.print(f"[yellow]Stopped[/yellow] [bold]{data.get('container_name', strategy_id)}[/bold]")
        except httpx.HTTPStatusError as e:
            _err(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")


@app.command()
def status() -> None:
    """Show status of all known strategies."""
    with _client() as client:
        try:
            resp = client.get("/strategies")
            resp.raise_for_status()
            strategies = resp.json()
        except httpx.HTTPStatusError as e:
            _err(f"HTTP {e.response.status_code}: {e.response.text}")
            return
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")
            return

    table = Table(title="Strategy Status", show_lines=False)
    table.add_column("ID", style="bold cyan", no_wrap=True)
    table.add_column("Container", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Instance", style="dim", justify="center")
    table.add_column("Registered At", style="dim")

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

        table.add_row(sid, container, status_text, instance, registered_at)

    console.print(table)


@app.command()
def logs(
    strategy_id: str = typer.Argument(..., help="Strategy ID to stream logs from"),
    tail: int = typer.Option(50, "--tail", "-n", help="Number of recent log lines to show"),
) -> None:
    """Stream logs from a strategy container."""
    with _client() as client:
        try:
            resp = client.get(f"/strategies/{strategy_id}/logs", params={"tail": tail})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            _err(f"HTTP {e.response.status_code}: {e.response.text}")
            return
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")
            return

    log_lines = data.get("logs", "")
    if not log_lines:
        console.print(f"[dim]No logs available for {strategy_id}[/dim]")
        return

    console.print(f"[bold]Logs for [cyan]{strategy_id}[/cyan] (last {tail} lines):[/bold]")
    console.print(log_lines)


@app.command()
def risk() -> None:
    """Show global risk summary."""
    with _client() as client:
        try:
            resp = client.get("/risk")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            _err(f"HTTP {e.response.status_code}: {e.response.text}")
            return
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")
            return

    # Global totals
    global_notional = data.get("global_notional_usd", 0.0)
    global_ceiling = data.get("global_ceiling_usd", 0.0)
    utilization = (global_notional / global_ceiling * 100) if global_ceiling else 0.0
    util_color = "green" if utilization < 70 else "yellow" if utilization < 90 else "red"

    console.print()
    console.print(
        f"  Global Notional: [bold white]${global_notional:,.2f}[/bold white]  /  "
        f"${global_ceiling:,.2f}  "
        f"[{util_color}]({utilization:.1f}% utilized)[/{util_color}]"
    )
    console.print()

    # Per-strategy breakdown
    per_strategy = data.get("strategies", {})
    if not per_strategy:
        console.print("[dim]  No active strategies[/dim]")
        return

    table = Table(title="Per-Strategy Risk", show_lines=False)
    table.add_column("Strategy", style="bold cyan")
    table.add_column("Notional USD", justify="right")
    table.add_column("Max USD", justify="right", style="dim")
    table.add_column("Utilization", justify="right")
    table.add_column("Circuit Breaker", justify="center")

    for sid, s in per_strategy.items():
        notional = s.get("notional_usd", 0.0)
        max_usd = s.get("max_position_usd", 0.0)
        util = (notional / max_usd * 100) if max_usd else 0.0
        util_col = "green" if util < 70 else "yellow" if util < 90 else "red"
        cb_open = s.get("circuit_breaker_open", False)
        cb_text = Text("OPEN", style="bold red") if cb_open else Text("closed", style="green")

        table.add_row(
            sid,
            f"${notional:,.2f}",
            f"${max_usd:,.2f}",
            f"[{util_col}]{util:.1f}%[/{util_col}]",
            cb_text,
        )

    console.print(table)
    console.print()


@app.command()
def health() -> None:
    """Check orchestrator health."""
    with _client() as client:
        try:
            resp = client.get("/health")
            resp.raise_for_status()
            data = resp.json()
            console.print(f"[green]✓[/green] Orchestrator healthy  |  {data}")
        except httpx.ConnectError:
            _err(f"Cannot connect to orchestrator at {_base_url()}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
