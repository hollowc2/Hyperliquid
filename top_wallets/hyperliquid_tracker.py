import time
import logging
import json
import os
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
MIN_PERP_NOTIONAL = 50_000  # USD — hide perp positions below this size
MIN_FILL_VALUE = 5_000     # USD — hide fills below this size in startup summary
CACHE_FILE = os.path.join(os.path.dirname(__file__), "wallet_cache.json")

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


# ── Cache ──────────────────────────────────────────────────────────────────────

def save_cache(lb_data: list):
    wallets = {}
    for entry in lb_data[:10]:
        addr = entry.get('ethAddress', '').lower()
        if not addr:
            continue
        wallets[addr] = {
            'perp_positions': entry.get('perp_positions', []),
            'holdings': entry.get('holdings', '—'),
        }
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump({'saved_at': time.time(), 'wallets': wallets}, f)
    except Exception as e:
        logging.warning("Failed to save cache: %s", e)


def load_cache() -> tuple[dict, float | None]:
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data.get('wallets', {}), data.get('saved_at')
    except FileNotFoundError:
        return {}, None
    except Exception as e:
        logging.warning("Failed to load cache: %s", e)
        return {}, None


def inject_cache(lb_data: list, cache: dict):
    """Copy cached perp_positions/holdings into lb_data entries that lack live data."""
    for entry in lb_data[:10]:
        addr = entry.get('ethAddress', '').lower()
        if addr in cache and 'perp_positions' not in entry:
            entry['perp_positions'] = cache[addr]['perp_positions']
            entry['holdings'] = cache[addr]['holdings']


# ── Formatting ─────────────────────────────────────────────────────────────────

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
    positions = [p for p in positions if p['notional'] >= MIN_PERP_NOTIONAL]
    if not positions:
        return "—"
    parts = []
    for p in positions:
        direction = "[green]L[/green]" if p['long'] else "[red]S[/red]"
        upnl_color = "green" if p['unrealized_pnl'] >= 0 else "red"
        upnl = f"[{upnl_color}]{fmt_usd(p['unrealized_pnl'])}[/{upnl_color}]"
        parts.append(f"{direction} {p['coin']} {fmt_usd(p['notional'])} ({upnl})")
    return "\n".join(parts)


def build_table(lb_data, status: str = ""):
    title = f"🏆 Hyperliquid Top 10   {status}" if status else "🏆 Hyperliquid Top 10"
    table = Table(title=title, style="bold cyan", expand=True)
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


# ── Fills ──────────────────────────────────────────────────────────────────────

def fetch_recent_fills(address: str, limit: int = FILLS_ON_STARTUP) -> list:
    try:
        fills = query_info({"type": "userFills", "user": address})
        if not fills:
            return []
        fills.sort(key=lambda f: f.get('time', 0), reverse=True)
        # Fetch extra so we have enough after filtering by MIN_FILL_VALUE
        return fills[:limit * 5]
    except Exception as e:
        logging.error("Failed to fetch fills for %s: %s", address, e)
        return []


def print_startup_fills(lb_data: list):
    console.print(f"\n[bold cyan]📋 Recent fills for top wallets (≥{fmt_usd(MIN_FILL_VALUE)}, last {FILLS_ON_STARTUP} each)[/bold cyan]")
    for i, entry in enumerate(lb_data[:10], start=1):
        address = entry.get('ethAddress', '').lower()
        if not address:
            continue
        username = entry.get("displayName") or f"{entry['ethAddress'][:8]}…{entry['ethAddress'][-6:]}"
        all_fills = fetch_recent_fills(address)
        fills = [f for f in all_fills if float(f.get('px', 0)) * float(f.get('sz', 0)) >= MIN_FILL_VALUE][:FILLS_ON_STARTUP]
        if not fills:
            continue
        console.print(f"\n  [bold]#{i} {username}[/bold]")
        for fill in fills:
            ts = datetime.fromtimestamp(fill.get('time', 0) / 1000).strftime("%m/%d %H:%M")
            coin       = fill.get('coin', '?')
            side       = fill.get('side', '?')
            px         = float(fill.get('px', 0))
            sz         = float(fill.get('sz', 0))
            value      = px * sz
            closed_pnl = float(fill.get('closedPnl', 0))
            side_str   = "BUY " if side == 'B' else "SELL"
            color      = "green" if side == 'B' else "red"
            pnl_str    = f"  pnl {fmt_pnl(closed_pnl)}" if closed_pnl != 0 else ""
            console.print(f"    [{color}]{ts}  {side_str}  {coin:6}  {sz} @ {px}  ({fmt_usd(value)}){pnl_str}[/{color}]")
    console.print()


# ── WebSocket ──────────────────────────────────────────────────────────────────

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
                ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
            console.print(f"[green]Subscribed to trades for {len(coins)} coins.[/green]")
        except Exception as e:
            console.print(f"[red]Error subscribing to trades:[/red] {e}")

    ws_url = "wss://api.hyperliquid.xyz/ws"
    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message,
                                on_error=on_error, on_close=on_close)
    ws.run_forever(ping_interval=20, ping_timeout=10)
    shutdown_event.wait()
    ws.close()


# ── Main ───────────────────────────────────────────────────────────────────────

def enrich_wallet(entry: dict, address: str, mids: dict):
    """Fetch and attach perp_positions + holdings to a single leaderboard entry."""
    account_value = float(entry.get('accountValue', '0'))
    if account_value == 0:
        entry['holdings'] = '—'
        entry['perp_positions'] = []
        return

    notional_by_coin = defaultdict(float)
    perp_positions = []

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
            notional = abs(float(position.get('positionValue', '0')))
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

    perp_positions.sort(key=lambda p: p['notional'], reverse=True)
    entry['perp_positions'] = perp_positions

    perp_coins = {p['coin'] for p in perp_positions}
    spot_holdings = []
    for coin, notional in sorted(notional_by_coin.items()):
        if coin in perp_coins:
            continue
        perc = (notional / account_value) * 100 if account_value > 0 else 0
        if perc >= 1:
            spot_holdings.append(f"{coin}({perc:.0f}%)")
    entry['holdings'] = ', '.join(spot_holdings) or '—'


def main():
    console.print("[bold yellow]Starting Hyperliquid Tracker...[/bold yellow]")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    top_accounts_shared = {}
    ws_t = threading.Thread(target=ws_thread, args=(top_accounts_shared,), daemon=True)
    ws_t.start()

    # Load and display cache immediately
    cache, saved_at = load_cache()
    first_run = True

    try:
        while not shutdown_event.is_set():
            lb_data = fetch_leaderboard()
            if not lb_data:
                console.print("[yellow]No leaderboard data received.[/yellow]")
            else:
                top_accounts = {}
                mids = info.all_mids()

                # On first cycle: seed table from cache so it shows instantly
                if first_run and cache:
                    inject_cache(lb_data, cache)
                    age = datetime.fromtimestamp(saved_at).strftime("%m/%d %H:%M") if saved_at else "?"
                    console.clear()
                    console.print(build_table(lb_data, f"[dim](cached {age} — refreshing...)[/dim]"))

                for i, entry in enumerate(lb_data[:10]):
                    address = entry.get('ethAddress', '').lower()
                    if not address:
                        continue
                    username = entry.get("displayName") or f"{entry['ethAddress'][:8]}…{entry['ethAddress'][-6:]}"
                    top_accounts[address] = {'username': username, 'rank': i + 1}
                    console.print(f"[dim]Fetching wallet {i+1}/10: {username}...[/dim]")
                    try:
                        enrich_wallet(entry, address, mids)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        console.print(f"[red]Error fetching state for {username}: {e}[/red]")
                        if 'perp_positions' not in entry:
                            entry['holdings'] = 'Error'
                            entry['perp_positions'] = []

                    # Redraw table after each wallet completes so progress is visible
                    remaining = 10 - (i + 1)
                    status = f"[dim](updating... {remaining} wallet{'s' if remaining != 1 else ''} remaining)[/dim]" if remaining else ""
                    console.clear()
                    console.print(build_table(lb_data, status))

                top_accounts_shared.clear()
                top_accounts_shared.update(top_accounts)

                # Final clean display with live timestamp
                ts = datetime.now().strftime("%H:%M:%S")
                console.clear()
                console.print(build_table(lb_data, f"[dim](live {ts})[/dim]"))
                save_cache(lb_data)

                if first_run:
                    console.print("[dim]Fetching recent fills...[/dim]")
                    print_startup_fills(lb_data)
                    first_run = False

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
