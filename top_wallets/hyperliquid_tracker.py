import time
import logging
import json
import websocket
import threading
import requests
from datetime import datetime
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from hyperliquid.info import Info
from hyperliquid.utils import constants

console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

REFRESH_INTERVAL = 10  # seconds
LARGE_TRADE_THRESHOLD = 100000  # USD
FILLS_ON_STARTUP = 8  # recent fills to show per wallet on startup

base_url = constants.MAINNET_API_URL


def query_info(msg):
    url = base_url + "/info"
    response = requests.post(url, json=msg)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Error {response.status_code}: {response.text}")


def fetch_leaderboard():
    url = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                lb_data = data.get('leaderboardRows', data.get('data', []))
            else:
                lb_data = data
            if isinstance(lb_data, list):
                return lb_data
            else:
                console.print(f"[red]Unexpected data structure: {type(lb_data)}[/red]")
                return []
        else:
            console.print(f"[red]Error fetching leaderboard: Status {response.status_code}[/red]")
            return []
    except Exception as e:
        console.print(f"[red]Error fetching leaderboard:[/red] {e}")
        return []


def fmt_usd(value: float) -> str:
    abs_val = abs(value)
    if abs_val >= 1_000_000_000:
        s = f"${abs_val / 1_000_000_000:.2f}B"
    elif abs_val >= 1_000_000:
        s = f"${abs_val / 1_000_000:.2f}M"
    elif abs_val >= 1_000:
        s = f"${abs_val / 1_000:.1f}K"
    else:
        s = f"${abs_val:,.0f}"
    return f"-{s}" if value < 0 else s


def fmt_pnl(value: float) -> str:
    text = fmt_usd(value)
    color = "green" if value >= 0 else "red"
    return f"[{color}]{text}[/{color}]"


def fmt_perp_positions(positions: list) -> str:
    """Format a list of perp position dicts into a readable string."""
    if not positions:
        return "—"
    parts = []
    for p in positions:
        direction = "[green]L[/green]" if p['long'] else "[red]S[/red]"
        upnl_color = "green" if p['unrealized_pnl'] >= 0 else "red"
        upnl = f"[{upnl_color}]{fmt_usd(p['unrealized_pnl'])}[/{upnl_color}]"
        parts.append(f"{direction} {p['coin']} {fmt_usd(p['notional'])} ({upnl})")
    return "\n".join(parts)


def build_table(lb_data):
    table = Table(title="🏆 Hyperliquid Top 10", style="bold cyan", expand=True)
    table.add_column("#", justify="center", style="dim", width=3)
    table.add_column("Username", min_width=14)
    table.add_column("All-Time Vol", justify="right", min_width=12)
    table.add_column("30d PnL", justify="right", min_width=10)
    table.add_column("Acct Value", justify="right", min_width=10)
    table.add_column("Lifetime PnL", justify="right", min_width=12)
    table.add_column("Open Perps", min_width=24)
    table.add_column("Spot / USDC", min_width=18)

    for i, entry in enumerate(lb_data[:10], start=1):
        performances = {p[0]: p[1] for p in entry.get("windowPerformances", [])}
        username = entry.get("displayName") or f"{entry.get('ethAddress', 'N/A')[:8]}…{entry.get('ethAddress', 'N/A')[-6:]}"
        volume   = fmt_usd(float(performances.get('allTime', {}).get('vlm', '0')))
        pnl      = fmt_pnl(float(performances.get('month',   {}).get('pnl', '0')))
        acct     = fmt_usd(float(entry.get('accountValue', '0')))
        lifetime = fmt_pnl(float(performances.get('allTime', {}).get('pnl', '0')))
        perps    = fmt_perp_positions(entry.get('perp_positions', []))
        holdings = entry.get('holdings', '—')
        table.add_row(str(i), username, volume, pnl, acct, lifetime, perps, holdings)

    return table


def fetch_recent_fills(address: str, limit: int = FILLS_ON_STARTUP) -> list:
    try:
        fills = query_info({"type": "userFills", "user": address})
        if not fills:
            return []
        fills.sort(key=lambda f: f.get('time', 0), reverse=True)
        return fills[:limit]
    except Exception as e:
        logging.error("Failed to fetch fills for %s: %s", address, e)
        return []


def print_startup_fills(lb_data: list):
    console.print(f"\n[bold cyan]📋 Recent fills for top wallets (last {FILLS_ON_STARTUP} each)[/bold cyan]")
    for i, entry in enumerate(lb_data[:10], start=1):
        address = entry.get('ethAddress', '').lower()
        if not address:
            continue
        username = entry.get("displayName") or f"{entry['ethAddress'][:8]}…{entry['ethAddress'][-6:]}"
        fills = fetch_recent_fills(address)
        if not fills:
            continue
        console.print(f"\n  [bold]#{i} {username}[/bold]")
        for fill in fills:
            ts = datetime.fromtimestamp(fill.get('time', 0) / 1000).strftime("%m/%d %H:%M")
            coin      = fill.get('coin', '?')
            side      = fill.get('side', '?')   # 'B' buy / 'A' sell
            px        = float(fill.get('px', 0))
            sz        = float(fill.get('sz', 0))
            value     = px * sz
            closed_pnl = float(fill.get('closedPnl', 0))
            side_str  = "BUY " if side == 'B' else "SELL"
            color     = "green" if side == 'B' else "red"
            pnl_str   = f"  pnl {fmt_pnl(closed_pnl)}" if closed_pnl != 0 else ""
            console.print(f"    [{color}]{ts}  {side_str}  {coin:6}  {sz} @ {px}  ({fmt_usd(value)}){pnl_str}[/{color}]")
    console.print()


shutdown_event = threading.Event()


def ws_thread(top_accounts_shared):
    def on_message(ws, message):
        try:
            msg = json.loads(message)
            if msg.get("channel") == "trades":
                for trade in msg.get("data", []):
                    buyer, seller = trade.get("users", [None, None])
                    if not buyer or not seller:
                        continue
                    buyer_lower = buyer.lower()
                    seller_lower = seller.lower()
                    px = float(trade.get("px", 0))
                    sz = float(trade.get("sz", 0))
                    value = px * sz
                    coin = trade.get("coin", "Unknown")
                    if value > LARGE_TRADE_THRESHOLD:
                        for addr, side in [(buyer_lower, "buy"), (seller_lower, "sell")]:
                            if addr in top_accounts_shared:
                                acc = top_accounts_shared[addr]
                                ts = datetime.now().strftime("%H:%M:%S")
                                console.print(f"[bold red][{ts}] Large {side.upper()} trade by top account {acc['username']} (Rank {acc['rank']}): {coin} {sz} @ {px} value {value:,.0f} USD[/bold red]")
        except json.JSONDecodeError:
            print("Invalid JSON:", message)

    def on_error(ws, error):
        console.print(f"[red]WebSocket Error:[/red] {error}")

    def on_close(ws, close_status_code, close_msg):
        console.print("[yellow]WebSocket closed.[/yellow]")

    def on_open(ws):
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        try:
            meta = info.meta()
            coins = [asset['name'] for asset in meta.get('universe', [])]
            for coin in coins:
                sub_msg = {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin}
                }
                ws.send(json.dumps(sub_msg))
            console.print(f"[green]Subscribed to trades for {len(coins)} coins.[/green]")
        except Exception as e:
            console.print(f"[red]Error subscribing to trades:[/red] {e}")

    ws_url = "wss://api.hyperliquid.xyz/ws"
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)
    shutdown_event.wait()
    ws.close()


def main():
    console.print("[bold yellow]Starting Hyperliquid Tracker...[/bold yellow]")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    top_accounts_shared = {}
    ws_t = threading.Thread(target=ws_thread, args=(top_accounts_shared,), daemon=True)
    ws_t.start()

    first_run = True

    try:
        while not shutdown_event.is_set():
            lb_data = fetch_leaderboard()
            if lb_data:
                top_accounts = {}
                mids = info.all_mids()
                for i, entry in enumerate(lb_data[:10]):
                    address = entry.get('ethAddress', '').lower()
                    if not address:
                        continue
                    username = entry.get("displayName") or f"{entry['ethAddress'][:8]}…{entry['ethAddress'][-6:]}"
                    top_accounts[address] = {'username': username, 'rank': i + 1}
                    console.print(f"[dim]Fetching wallet {i+1}/10: {username}...[/dim]")
                    try:
                        account_value = float(entry.get('accountValue', '0'))
                        if account_value == 0:
                            entry['holdings'] = '—'
                            entry['perp_positions'] = []
                            continue

                        notional_by_coin = defaultdict(float)
                        perp_positions = []  # [{coin, long, notional, unrealized_pnl}]

                        sub_accounts = query_info({"type": "subAccounts", "user": address})
                        if sub_accounts is None:
                            sub_accounts = []
                        all_addresses = [address] + [s['subAccountUser'] for s in sub_accounts]

                        for sub_address in all_addresses:
                            state = query_info({"type": "clearinghouseState", "user": sub_address})

                            for pos in state.get('assetPositions', []):
                                position = pos.get('position', {})
                                coin = position.get('coin')
                                if not coin:
                                    continue
                                szi = float(position.get('szi', '0'))
                                if szi == 0:
                                    continue
                                mark_px = float(position.get('markPx', '0'))
                                notional = abs(szi) * mark_px
                                unrealized_pnl = float(position.get('unrealizedPnl', '0'))
                                notional_by_coin[coin] += notional
                                perp_positions.append({
                                    'coin': coin,
                                    'long': szi > 0,
                                    'notional': notional,
                                    'unrealized_pnl': unrealized_pnl,
                                })

                            withdrawable = float(state.get('withdrawable', '0'))
                            notional_by_coin['USDC'] += withdrawable

                            spot_state = query_info({"type": "spotClearinghouseState", "user": sub_address})
                            for balance in spot_state.get('balances', []):
                                coin = balance.get('coin')
                                total = float(balance.get('total', '0'))
                                if total <= 0:
                                    continue
                                if coin == 'USDC':
                                    notional_by_coin['USDC'] += total
                                else:
                                    price = float(mids.get(coin, '0'))
                                    notional_by_coin[coin] += total * price

                        # Perp positions sorted by notional desc
                        perp_positions.sort(key=lambda p: p['notional'], reverse=True)
                        entry['perp_positions'] = perp_positions

                        # Spot/USDC holdings (exclude perp coins, keep spot + USDC)
                        perp_coins = {p['coin'] for p in perp_positions}
                        spot_holdings = []
                        for coin, notional in sorted(notional_by_coin.items()):
                            if coin in perp_coins:
                                continue  # already shown in perps column
                            perc = (notional / account_value) * 100 if account_value > 0 else 0
                            if perc >= 1:
                                spot_holdings.append(f"{coin}({perc:.0f}%)")
                        entry['holdings'] = ', '.join(spot_holdings) or '—'

                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        console.print(f"[red]Error fetching state for {username}: {e}[/red]")
                        entry['holdings'] = 'Error'
                        entry['perp_positions'] = []

                top_accounts_shared.clear()
                top_accounts_shared.update(top_accounts)

                console.clear()
                table = build_table(lb_data)
                console.print(table)

                if first_run:
                    console.print("[dim]Fetching recent fills...[/dim]")
                    print_startup_fills(lb_data)
                    first_run = False
            else:
                console.print("[yellow]No leaderboard data received.[/yellow]")

            for _ in range(REFRESH_INTERVAL * 10):
                if shutdown_event.is_set():
                    break
                time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        console.print("[bold yellow]Shutting down... GG.[/bold yellow]")


if __name__ == "__main__":
    main()
