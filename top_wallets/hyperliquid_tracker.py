import time
import logging
import json
import os
import threading
import requests
import websocket
from datetime import datetime
from collections import defaultdict

from rich.text import Text
from rich.table import Table as RichTable

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static
from textual.containers import ScrollableContainer
from textual.binding import Binding
from textual import work, on

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ── Constants ──────────────────────────────────────────────────────────────────

REFRESH_INTERVAL    = 60        # seconds between full refreshes
LARGE_TRADE_THRESHOLD = 100_000 # USD
FILLS_ON_STARTUP    = 8
MIN_PERP_NOTIONAL   = 50_000    # USD — hide positions below this in detail panel
MIN_FILL_VALUE      = 5_000     # USD — hide fills below this in startup summary
CACHE_FILE = os.path.join(os.path.dirname(__file__), "wallet_cache.json")

base_url = constants.MAINNET_API_URL

# ── Data helpers ───────────────────────────────────────────────────────────────

def query_info(msg):
    response = requests.post(base_url + "/info", json=msg)
    if response.status_code == 200:
        return response.json()
    raise Exception(f"Error {response.status_code}: {response.text}")


def fetch_leaderboard() -> list:
    url = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    try:
        response = requests.get(url)
        if response.status_code != 200:
            return []
        data = response.json()
        lb = data.get('leaderboardRows', data.get('data', data)) if isinstance(data, dict) else data
        return lb if isinstance(lb, list) else []
    except Exception:
        return []


def enrich_wallet(entry: dict, address: str, mids: dict):
    account_value = float(entry.get('accountValue', '0'))
    if account_value == 0:
        entry['holdings'] = '—'
        entry['perp_positions'] = []
        return

    notional_by_coin = defaultdict(float)
    perp_positions = []

    sub_accounts = query_info({"type": "subAccounts", "user": address}) or []
    for sub_address in [address] + [s['subAccountUser'] for s in sub_accounts]:
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
                'coin': coin, 'long': szi > 0,
                'notional': notional, 'unrealized_pnl': unrealized_pnl,
            })

        notional_by_coin['USDC'] += float(state.get('withdrawable', '0'))

        spot_state = query_info({"type": "spotClearinghouseState", "user": sub_address})
        for balance in spot_state.get('balances', []):
            coin = balance.get('coin')
            total = float(balance.get('total', '0'))
            if total <= 0:
                continue
            notional_by_coin[coin] += total if coin == 'USDC' else total * float(mids.get(coin, '0'))

    perp_positions.sort(key=lambda p: p['notional'], reverse=True)
    entry['perp_positions'] = perp_positions

    perp_coins = {p['coin'] for p in perp_positions}
    spot_holdings = [
        f"{coin}({(n/account_value*100):.0f}%)"
        for coin, n in sorted(notional_by_coin.items())
        if coin not in perp_coins and (n / account_value * 100) >= 1
    ]
    entry['holdings'] = ', '.join(spot_holdings) or '—'


# ── Cache ──────────────────────────────────────────────────────────────────────

def save_cache(lb_data: list):
    wallets = {
        entry['ethAddress'].lower(): {
            'perp_positions': entry.get('perp_positions', []),
            'holdings': entry.get('holdings', '—'),
        }
        for entry in lb_data[:10]
        if entry.get('ethAddress')
    }
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump({'saved_at': time.time(), 'wallets': wallets}, f)
    except Exception as e:
        logging.warning("Cache save failed: %s", e)


def load_cache() -> tuple[dict, float | None]:
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data.get('wallets', {}), data.get('saved_at')
    except FileNotFoundError:
        return {}, None
    except Exception as e:
        logging.warning("Cache load failed: %s", e)
        return {}, None


def inject_cache(lb_data: list, cache: dict):
    for entry in lb_data[:10]:
        addr = entry.get('ethAddress', '').lower()
        if addr in cache and 'perp_positions' not in entry:
            entry['perp_positions'] = cache[addr]['perp_positions']
            entry['holdings']       = cache[addr]['holdings']


# ── Formatting ─────────────────────────────────────────────────────────────────

def fmt_usd(value: float) -> str:
    abs_val = abs(value)
    if   abs_val >= 1_000_000_000: s = f"${abs_val/1_000_000_000:.2f}B"
    elif abs_val >= 1_000_000:     s = f"${abs_val/1_000_000:.2f}M"
    elif abs_val >= 1_000:         s = f"${abs_val/1_000:.1f}K"
    else:                          s = f"${abs_val:,.0f}"
    return f"-{s}" if value < 0 else s


def fmt_pnl_text(value: float) -> Text:
    return Text(fmt_usd(value), style="green" if value >= 0 else "red")


# ── Textual App ────────────────────────────────────────────────────────────────

COL_KEYS = ["rank", "username", "vol", "pnl_30d", "acct", "lifetime", "perps", "spot"]

CSS = """
Screen {
    layout: vertical;
}

DataTable#summary {
    height: 1fr;
}

ScrollableContainer#perp-panel {
    height: 14;
    border: solid $accent;
    padding: 0 1;
}

ScrollableContainer#perp-panel.hidden {
    display: none;
}
"""


class HyperliquidTracker(App):
    TITLE   = "🏆 Hyperliquid Top 10"
    CSS     = CSS
    BINDINGS = [
        Binding("q", "quit",           "Quit"),
        Binding("p", "toggle_perps",   "Toggle Perps"),
        Binding("r", "manual_refresh", "Refresh"),
    ]

    def __init__(self):
        super().__init__()
        self._lb_data: list       = []
        self._top_accounts: dict  = {}
        self._info                = None
        self._shutdown            = threading.Event()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="summary", cursor_type="row", zebra_stripes=True)
        with ScrollableContainer(id="perp-panel"):
            yield Static(id="perp-content")
        yield Footer()

    def on_mount(self) -> None:
        dt = self.query_one("#summary", DataTable)
        dt.add_column("#",            key="rank",     width=4)
        dt.add_column("Username",     key="username", width=22)
        dt.add_column("All-Time Vol", key="vol",      width=14)
        dt.add_column("30d PnL",      key="pnl_30d",  width=12)
        dt.add_column("Acct Value",   key="acct",     width=12)
        dt.add_column("Lifetime PnL", key="lifetime", width=14)
        dt.add_column("Perps",        key="perps",    width=20)
        dt.add_column("Spot / USDC",  key="spot",     width=22)

        self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        threading.Thread(target=self._ws_worker, daemon=True).start()
        self.do_refresh()
        self.set_interval(REFRESH_INTERVAL, self.do_refresh)

    async def on_unmount(self) -> None:
        self._shutdown.set()

    # ── Data refresh ───────────────────────────────────────────────────────────

    @work(thread=True)
    def do_refresh(self) -> None:
        self.call_from_thread(self._set_status, "fetching leaderboard...")
        lb_data = fetch_leaderboard()
        if not lb_data:
            self.call_from_thread(self._set_status, "⚠ no leaderboard data")
            return

        cache, saved_at = load_cache()
        if cache:
            inject_cache(lb_data, cache)
            age = datetime.fromtimestamp(saved_at).strftime("%m/%d %H:%M") if saved_at else "?"
            self.call_from_thread(self._init_table, lb_data, f"cached {age} — refreshing...")
        else:
            self.call_from_thread(self._init_table, lb_data, "fetching wallet data...")

        mids         = self._info.all_mids()
        top_accounts = {}

        for i, entry in enumerate(lb_data[:10]):
            address = entry.get('ethAddress', '').lower()
            if not address:
                continue
            username = entry.get("displayName") or f"{entry['ethAddress'][:8]}…{entry['ethAddress'][-6:]}"
            top_accounts[address] = {'username': username, 'rank': i + 1}

            remaining = 9 - i
            self.call_from_thread(
                self._set_status,
                f"fetching {i+1}/10: {username}..." + (f"  ({remaining} left)" if remaining else "")
            )
            try:
                enrich_wallet(entry, address, mids)
            except Exception as e:
                logging.error("enrich error %s: %s", username, e)
                entry.setdefault('perp_positions', [])
                entry.setdefault('holdings', 'Error')

            self.call_from_thread(self._update_row, i, entry)

        self._lb_data      = lb_data
        self._top_accounts = top_accounts
        save_cache(lb_data)
        ts = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self._set_status, f"live {ts}")

    # ── Table helpers ──────────────────────────────────────────────────────────

    def _init_table(self, lb_data: list, status: str = "") -> None:
        dt = self.query_one("#summary", DataTable)
        dt.clear()
        for i, entry in enumerate(lb_data[:10], start=1):
            dt.add_row(*self._entry_to_row(i, entry), key=str(i))
        self.sub_title = status
        if lb_data:
            self._update_perp_panel(lb_data[0])

    def _update_row(self, idx: int, entry: dict) -> None:
        dt      = self.query_one("#summary", DataTable)
        row_key = str(idx + 1)
        values  = self._entry_to_row(idx + 1, entry)
        for col_key, value in zip(COL_KEYS, values):
            try:
                dt.update_cell(row_key, col_key, value, update_width=False)
            except Exception:
                pass
        # Refresh detail panel if this row is selected
        try:
            if dt.cursor_row == idx:
                self._update_perp_panel(entry)
        except Exception:
            pass

    def _entry_to_row(self, rank: int, entry: dict) -> tuple:
        perf     = {p[0]: p[1] for p in entry.get("windowPerformances", [])}
        username = (entry.get("displayName") or
                    f"{entry.get('ethAddress','N/A')[:8]}…{entry.get('ethAddress','N/A')[-6:]}")
        vol      = fmt_usd(float(perf.get('allTime', {}).get('vlm', '0')))
        pnl_30d  = fmt_pnl_text(float(perf.get('month',   {}).get('pnl', '0')))
        acct     = fmt_usd(float(entry.get('accountValue', '0')))
        lifetime = fmt_pnl_text(float(perf.get('allTime', {}).get('pnl', '0')))

        sig = [p for p in entry.get('perp_positions', []) if p['notional'] >= MIN_PERP_NOTIONAL]
        if sig:
            net_upnl  = sum(p['unrealized_pnl'] for p in sig)
            perp_cell = Text()
            perp_cell.append(f"{len(sig)} pos  ", style="dim")
            perp_cell.append(fmt_usd(net_upnl), style="green" if net_upnl >= 0 else "red")
        else:
            perp_cell = Text("—", style="dim")

        spot = entry.get('holdings', '—')
        return (str(rank), username, vol, pnl_30d, acct, lifetime, perp_cell, spot)

    # ── Perp detail panel ──────────────────────────────────────────────────────

    @on(DataTable.RowHighlighted, "#summary")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or not self._lb_data:
            return
        try:
            self._update_perp_panel(self._lb_data[int(str(event.row_key)) - 1])
        except (ValueError, IndexError):
            pass

    def _update_perp_panel(self, entry: dict) -> None:
        username  = (entry.get("displayName") or
                     f"{entry.get('ethAddress','N/A')[:8]}…{entry.get('ethAddress','N/A')[-6:]}")
        positions = [p for p in entry.get('perp_positions', []) if p['notional'] >= MIN_PERP_NOTIONAL]
        content   = self.query_one("#perp-content", Static)

        if not positions:
            content.update(f"[dim]{username}  —  no significant open positions[/dim]")
            return

        total_notional = sum(p['notional']      for p in positions)
        net_upnl       = sum(p['unrealized_pnl'] for p in positions)
        upnl_style     = "green" if net_upnl >= 0 else "red"

        t = RichTable(
            title=(f"[bold]{username}[/bold]  ·  {len(positions)} positions  ·  "
                   f"{fmt_usd(total_notional)} notional  ·  "
                   f"net uPnL [{upnl_style}]{fmt_usd(net_upnl)}[/{upnl_style}]"),
            expand=True, box=None, show_edge=False, padding=(0, 1),
        )
        t.add_column("Dir",      width=3)
        t.add_column("Coin",     min_width=8)
        t.add_column("Notional", justify="right", min_width=10)
        t.add_column("uPnL",     justify="right", min_width=10)

        for p in positions:
            upnl = p['unrealized_pnl']
            t.add_row(
                Text("L", style="bold green") if p['long'] else Text("S", style="bold red"),
                p['coin'],
                fmt_usd(p['notional']),
                Text(fmt_usd(upnl), style="green" if upnl >= 0 else "red"),
            )

        content.update(t)

    # ── Actions ────────────────────────────────────────────────────────────────

    def action_toggle_perps(self) -> None:
        self.query_one("#perp-panel").toggle_class("hidden")

    def action_manual_refresh(self) -> None:
        self.do_refresh()

    def _set_status(self, msg: str) -> None:
        self.sub_title = msg

    # ── WebSocket ──────────────────────────────────────────────────────────────

    def _ws_worker(self) -> None:
        def on_message(ws, message):
            try:
                msg = json.loads(message)
                if msg.get("channel") != "trades":
                    return
                for trade in msg.get("data", []):
                    buyer, seller = trade.get("users", [None, None])
                    if not buyer or not seller:
                        continue
                    value = float(trade.get("px", 0)) * float(trade.get("sz", 0))
                    coin  = trade.get("coin", "?")
                    if value > LARGE_TRADE_THRESHOLD:
                        for addr, side in [(buyer.lower(), "BUY"), (seller.lower(), "SELL")]:
                            if addr in self._top_accounts:
                                acc  = self._top_accounts[addr]
                                text = (f"🐋 {acc['username']} (#{acc['rank']}) "
                                        f"{side} {coin}  {fmt_usd(value)}")
                                self.call_from_thread(
                                    self.notify, text, severity="warning", timeout=8
                                )
            except Exception:
                pass

        def on_open(ws):
            try:
                meta  = Info(constants.MAINNET_API_URL, skip_ws=True).meta()
                coins = [a['name'] for a in meta.get('universe', [])]
                for coin in coins:
                    ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin}
                    }))
            except Exception:
                pass

        ws_url = "wss://api.hyperliquid.xyz/ws"
        while not self._shutdown.is_set():
            ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=10)
            if not self._shutdown.is_set():
                time.sleep(5)  # reconnect delay


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    HyperliquidTracker().run()
