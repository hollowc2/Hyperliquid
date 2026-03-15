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

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static, ContentSwitcher, Label
from textual.containers import Container
from textual.binding import Binding
from textual import work, on

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ── Constants ──────────────────────────────────────────────────────────────────

REFRESH_INTERVAL      = 60
LARGE_TRADE_THRESHOLD = 100_000
MIN_PERP_NOTIONAL     = 50_000
CACHE_FILE = os.path.join(os.path.dirname(__file__), "wallet_cache.json")

base_url = constants.MAINNET_API_URL

# ── Data helpers ───────────────────────────────────────────────────────────────

def query_info(msg):
    r = requests.post(base_url + "/info", json=msg)
    if r.status_code == 200:
        return r.json()
    raise Exception(f"HTTP {r.status_code}: {r.text}")


def fetch_leaderboard() -> list:
    try:
        r = requests.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
        if r.status_code != 200:
            return []
        data = r.json()
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
    perp_positions   = []

    sub_accounts = query_info({"type": "subAccounts", "user": address}) or []
    for sub_addr in [address] + [s['subAccountUser'] for s in sub_accounts]:
        state = query_info({"type": "clearinghouseState", "user": sub_addr})
        for pos in state.get('assetPositions', []):
            p    = pos.get('position', {})
            coin = p.get('coin')
            if not coin:
                continue
            szi = float(p.get('szi', '0'))
            if szi == 0:
                continue
            notional      = abs(float(p.get('positionValue', '0')))
            unrealized_pnl = float(p.get('unrealizedPnl', '0'))
            notional_by_coin[coin] += notional
            perp_positions.append({
                'coin': coin, 'long': szi > 0,
                'notional': notional, 'unrealized_pnl': unrealized_pnl,
            })
        notional_by_coin['USDC'] += float(state.get('withdrawable', '0'))

        spot = query_info({"type": "spotClearinghouseState", "user": sub_addr})
        for bal in spot.get('balances', []):
            coin  = bal.get('coin')
            total = float(bal.get('total', '0'))
            if total <= 0:
                continue
            notional_by_coin[coin] += (
                total if coin == 'USDC' else total * float(mids.get(coin, '0'))
            )

    perp_positions.sort(key=lambda p: p['notional'], reverse=True)
    entry['perp_positions'] = perp_positions

    perp_coins   = {p['coin'] for p in perp_positions}
    spot_holdings = [
        f"{coin}({n/account_value*100:.0f}%)"
        for coin, n in sorted(notional_by_coin.items())
        if coin not in perp_coins and n / account_value * 100 >= 1
    ]
    entry['holdings'] = ', '.join(spot_holdings) or '—'


# ── Cache ──────────────────────────────────────────────────────────────────────

def save_cache(lb_data: list):
    try:
        wallets = {
            e['ethAddress'].lower(): {
                'perp_positions': e.get('perp_positions', []),
                'holdings':       e.get('holdings', '—'),
            }
            for e in lb_data[:10] if e.get('ethAddress')
        }
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
    a = abs(value)
    if   a >= 1_000_000_000: s = f"${a/1_000_000_000:.2f}B"
    elif a >= 1_000_000:     s = f"${a/1_000_000:.2f}M"
    elif a >= 1_000:         s = f"${a/1_000:.1f}K"
    else:                    s = f"${a:,.0f}"
    return f"-{s}" if value < 0 else s


def fmt_pnl(value: float) -> Text:
    return Text(fmt_usd(value), style="green" if value >= 0 else "red")


# ── Constants / keys ───────────────────────────────────────────────────────────

SUMMARY_COLS = ["rank", "username", "vol", "pnl_30d", "acct", "lifetime", "perps", "spot"]
PERP_COLS    = ["dir", "coin", "notional", "upnl"]


# ── App ────────────────────────────────────────────────────────────────────────

CSS = """
Screen {
    layout: vertical;
}

DataTable#summary {
    height: 2fr;
}

#perp-panel {
    height: 3fr;
    border: solid $accent;
    layout: vertical;
}

#perp-panel.hidden {
    display: none;
}

#perp-header {
    height: 2;
    padding: 0 1;
    background: $boost;
    color: $text-muted;
}

ContentSwitcher {
    height: 1fr;
}

DataTable.perp-table {
    height: 1fr;
}
"""


class HyperliquidTracker(App):
    TITLE    = "🏆 Hyperliquid Top 10"
    CSS      = CSS
    BINDINGS = [
        Binding("q", "quit",           "Quit"),
        Binding("p", "toggle_perps",   "Toggle Perps"),
        Binding("r", "manual_refresh", "Refresh"),
    ]

    def __init__(self):
        super().__init__()
        self._lb_data: list      = []
        self._top_accounts: dict = {}
        self._info               = None
        self._shutdown           = threading.Event()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="summary", cursor_type="row", zebra_stripes=True)
        with Container(id="perp-panel"):
            yield Static(id="perp-header")
            with ContentSwitcher(id="perp-switcher", initial="perp-1"):
                for i in range(1, 11):
                    yield DataTable(id=f"perp-{i}", classes="perp-table",
                                    cursor_type="none", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        # Summary table columns
        dt = self.query_one("#summary", DataTable)
        dt.add_column("#",            key="rank",     width=4)
        dt.add_column("Username",     key="username", width=44)
        dt.add_column("All-Time Vol", key="vol",      width=14)
        dt.add_column("30d PnL",      key="pnl_30d",  width=12)
        dt.add_column("Acct Value",   key="acct",     width=12)
        dt.add_column("Lifetime PnL", key="lifetime", width=14)
        dt.add_column("Perps",        key="perps",    width=20)
        dt.add_column("Spot / USDC",  key="spot",     width=22)

        # Perp detail table columns (one per wallet slot)
        for i in range(1, 11):
            pdt = self.query_one(f"#perp-{i}", DataTable)
            pdt.add_column("Dir",      key="dir",      width=3)
            pdt.add_column("Coin",     key="coin",     width=10)
            pdt.add_column("Notional", key="notional", width=14)
            pdt.add_column("uPnL",     key="upnl",     width=14)

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
            self.call_from_thread(self._set_status, "⚠ no data")
            return

        cache, saved_at = load_cache()
        if cache:
            inject_cache(lb_data, cache)
            age = datetime.fromtimestamp(saved_at).strftime("%m/%d %H:%M") if saved_at else "?"
            self.call_from_thread(self._init_summary, lb_data, f"cached {age} — refreshing...")
            # Pre-fill perp panels from cache immediately
            for i, entry in enumerate(lb_data[:10], start=1):
                if entry.get('perp_positions') is not None:
                    self.call_from_thread(self._populate_perp_table, i, entry)
        else:
            self.call_from_thread(self._init_summary, lb_data, "fetching wallet data...")

        mids         = self._info.all_mids()
        top_accounts = {}

        for i, entry in enumerate(lb_data[:10]):
            address = entry.get('ethAddress', '').lower()
            if not address:
                continue
            username = (entry.get("displayName") or
                        entry['ethAddress'])
            top_accounts[address] = {'username': username, 'rank': i + 1}

            remaining = 9 - i
            self.call_from_thread(
                self._set_status,
                f"fetching {i+1}/10: {username}..." +
                (f"  ({remaining} left)" if remaining else "")
            )
            try:
                enrich_wallet(entry, address, mids)
            except Exception as e:
                logging.error("enrich %s: %s", username, e)
                entry.setdefault('perp_positions', [])
                entry.setdefault('holdings', 'Error')

            # Update summary row + pre-build perp table
            self.call_from_thread(self._update_summary_row, i, entry)
            self.call_from_thread(self._populate_perp_table, i + 1, entry)

        self._lb_data      = lb_data
        self._top_accounts = top_accounts
        save_cache(lb_data)
        self.call_from_thread(self._set_status,
                               f"live {datetime.now().strftime('%H:%M:%S')}")

    # ── Summary table ──────────────────────────────────────────────────────────

    def _init_summary(self, lb_data: list, status: str = "") -> None:
        dt = self.query_one("#summary", DataTable)
        dt.clear()
        for i, entry in enumerate(lb_data[:10], start=1):
            dt.add_row(*self._summary_row(i, entry), key=str(i))
        self.sub_title = status
        # Show first wallet's perp header
        if lb_data:
            self._update_perp_header(lb_data[0])

    def _update_summary_row(self, idx: int, entry: dict) -> None:
        dt      = self.query_one("#summary", DataTable)
        row_key = str(idx + 1)
        for col_key, value in zip(SUMMARY_COLS, self._summary_row(idx + 1, entry)):
            try:
                dt.update_cell(row_key, col_key, value, update_width=False)
            except Exception:
                pass
        # Refresh header if this row is active
        try:
            if dt.cursor_row == idx:
                self._update_perp_header(entry)
        except Exception:
            pass

    def _summary_row(self, rank: int, entry: dict) -> tuple:
        perf     = {p[0]: p[1] for p in entry.get("windowPerformances", [])}
        username = (entry.get("displayName") or
                    entry.get('ethAddress', 'N/A'))
        vol      = fmt_usd(float(perf.get('allTime', {}).get('vlm', '0')))
        pnl_30d  = fmt_pnl(float(perf.get('month',   {}).get('pnl', '0')))
        acct     = fmt_usd(float(entry.get('accountValue', '0')))
        lifetime = fmt_pnl(float(perf.get('allTime',  {}).get('pnl', '0')))

        sig = [p for p in entry.get('perp_positions', []) if p['notional'] >= MIN_PERP_NOTIONAL]
        if sig:
            net_upnl  = sum(p['unrealized_pnl'] for p in sig)
            perp_cell = Text()
            perp_cell.append(f"{len(sig)} pos  ", style="dim")
            perp_cell.append(fmt_usd(net_upnl), style="green" if net_upnl >= 0 else "red")
        else:
            perp_cell = Text("—", style="dim")

        return (str(rank), username, vol, pnl_30d, acct, lifetime, perp_cell,
                entry.get('holdings', '—'))

    # ── Perp detail (pre-built per wallet, instant switch) ─────────────────────

    def _populate_perp_table(self, rank: int, entry: dict) -> None:
        """Build the perp DataTable for this wallet slot. Called once per enrich."""
        pdt       = self.query_one(f"#perp-{rank}", DataTable)
        positions = [p for p in entry.get('perp_positions', [])
                     if p['notional'] >= MIN_PERP_NOTIONAL]
        pdt.clear()
        for p in positions:
            upnl = p['unrealized_pnl']
            pdt.add_row(
                Text("L", style="bold green") if p['long'] else Text("S", style="bold red"),
                p['coin'],
                fmt_usd(p['notional']),
                Text(fmt_usd(upnl), style="green" if upnl >= 0 else "red"),
            )

    def _update_perp_header(self, entry: dict) -> None:
        """Update the one-line stat strip above the perp table."""
        username  = (entry.get("displayName") or
                     entry.get('ethAddress', 'N/A'))
        positions = [p for p in entry.get('perp_positions', [])
                     if p['notional'] >= MIN_PERP_NOTIONAL]
        if not positions:
            self.query_one("#perp-header", Static).update(
                f"[bold]{username}[/bold]  [dim]no significant open positions[/dim]"
            )
            return
        total  = sum(p['notional']       for p in positions)
        net    = sum(p['unrealized_pnl'] for p in positions)
        color  = "green" if net >= 0 else "red"
        self.query_one("#perp-header", Static).update(
            f"[bold]{username}[/bold]  "
            f"[dim]{len(positions)} positions  ·  "
            f"{fmt_usd(total)} notional  ·  "
            f"net uPnL [{color}]{fmt_usd(net)}[/{color}][/dim]"
        )

    @on(DataTable.RowHighlighted, "#summary")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or not self._lb_data:
            return
        try:
            rank  = int(str(event.row_key))
            entry = self._lb_data[rank - 1]
            # Instant switch — just shows/hides pre-built tables
            self.query_one("#perp-switcher", ContentSwitcher).current = f"perp-{rank}"
            self._update_perp_header(entry)
        except (ValueError, IndexError):
            pass

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
                                self.call_from_thread(
                                    self.notify,
                                    f"🐋 {acc['username']} (#{acc['rank']}) {side} {coin} {fmt_usd(value)}",
                                    severity="warning", timeout=8,
                                )
            except Exception:
                pass

        def on_open(ws):
            try:
                coins = [a['name'] for a in
                         Info(constants.MAINNET_API_URL, skip_ws=True).meta().get('universe', [])]
                for coin in coins:
                    ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    }))
            except Exception:
                pass

        ws_url = "wss://api.hyperliquid.xyz/ws"
        while not self._shutdown.is_set():
            websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message
                                   ).run_forever(ping_interval=20, ping_timeout=10)
            if not self._shutdown.is_set():
                time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    HyperliquidTracker().run()
