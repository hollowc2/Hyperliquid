"""Hourly net positioning of a basket of directional swing wallets, and whether it
predicts forward returns.

Basket = wallets in wallet_profiles.csv labelled "swing / position" or "short swing"
with net/gross exposure >= 0.5 (i.e. directional, not hedged). Positions are rebuilt
from cached fills (run profile_wallets.py first). A wallet only counts from the start
of its fill window, since its positions before that are unknown.

Per coin and hour:
  exposure  mean over wallets of net notional / wallet account value
  breadth   (wallets long - wallets short) / wallets with known positions
  net_usd   summed net notional across the basket

Then for each coin, on non-overlapping samples every H hours:
  IC       Spearman corr of signal (level, and 24h change) vs forward H-hour return
  follow   annualised Sharpe of holding sign(breadth) for H hours, vs buy & hold

    python basket_positioning.py
    python basket_positioning.py --coins BTC ETH HYPE --horizons 24 72 168
    python basket_positioning.py --addresses 0xabc... 0xdef...
"""
import argparse
import csv
import math
import os
import statistics
import sys
import time

from profile_wallets import HOUR_MS, OUT_FILE as PROFILE_FILE, cached, fetch_wallet, info, position_at, position_paths

HERE       = os.path.dirname(os.path.abspath(__file__))
OUT_FILE   = os.path.join(HERE, "basket_positioning.csv")
BASKET_STYLES = ("swing / position", "short swing")


def load_basket(addresses):
    with open(PROFILE_FILE) as f:
        rows = list(csv.DictReader(f))
    if addresses:
        return [r for r in rows if r["address"] in addresses]
    return [r for r in rows if r["style"] in BASKET_STYLES and float(r["net_gross"]) >= 0.5]


def fetch_candles(coin, start_ms, end_ms):
    """Hourly open prices {hour_ms: open}."""
    def fetch():
        out, t = [], start_ms
        while t < end_ms:
            page = info({"type": "candleSnapshot",
                         "req": {"coin": coin, "interval": "1h", "startTime": t, "endTime": end_ms}}, 100)
            if not page:
                break
            out += page
            t = page[-1]["t"] + HOUR_MS
        return out
    hour_now = end_ms // HOUR_MS
    return {c["t"]: float(c["o"]) for c in cached(f"candles_{coin}_{start_ms // HOUR_MS}_{hour_now}", fetch)}


def wallet_series(fills, state):
    """(window_start_ms, coin -> (times, path)) for one wallet."""
    paths = position_paths(fills, state)
    start = min((f["time"] for f in fills), default=None)
    return start, paths


def basket_frame(basket, coins, hours, prices):
    wallets = []
    for r in basket:
        fills, state = fetch_wallet(r["address"])[:2]
        start, paths = wallet_series(fills, state)
        if start is not None:
            wallets.append((r["address"], float(r["account"]), start, paths))

    rows = []
    for h in hours:
        live = [w for w in wallets if w[2] <= h]
        if not live:
            continue
        for c in coins:
            px = prices[c].get(h)
            if px is None:
                continue
            expo, net_usd, longs, shorts = [], 0.0, 0, 0
            for _, acct, _, paths in live:
                if c in paths:
                    pos, _ = position_at(paths[c], h)
                    if pos is None:                  # before this coin's first fill
                        pos = paths[c][1][0][1]      # = that fill's startPosition
                else:
                    pos = 0.0
                expo.append(pos * px / acct if acct else 0.0)
                net_usd += pos * px
                longs += pos > 0
                shorts += pos < 0
            rows.append({"hour": h, "coin": c, "price": px, "n_wallets": len(live),
                         "exposure": statistics.mean(expo), "breadth": (longs - shorts) / len(live),
                         "net_usd": net_usd})
    return rows


def spearman(x, y):
    if len(x) < 10 or len(set(x)) < 3:
        return None
    return statistics.correlation(x, y, method="ranked")


def evaluate(rows, coin, horizon):
    by_h = {r["hour"]: r for r in rows if r["coin"] == coin}
    hs = sorted(by_h)
    if not hs:
        return None
    lvl, chg, fwd, follow, hold = [], [], [], [], []
    h = hs[0] + 24 * HOUR_MS
    while h + horizon * HOUR_MS <= hs[-1]:
        now, prev, fut = by_h.get(h), by_h.get(h - 24 * HOUR_MS), by_h.get(h + horizon * HOUR_MS)
        if now and prev and fut:
            r = fut["price"] / now["price"] - 1
            lvl.append(now["exposure"])
            chg.append(now["exposure"] - prev["exposure"])
            fwd.append(r)
            follow.append(math.copysign(1, now["breadth"]) * r if now["breadth"] else 0.0)
            hold.append(r)
        h += horizon * HOUR_MS

    def sharpe(rets):
        if len(rets) < 2 or statistics.stdev(rets) == 0:
            return None
        return statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(365 * 24 / horizon)

    n = len(fwd)
    active = [x for x in follow if x != 0]
    ic_l, ic_c = spearman(lvl, fwd), spearman(chg, fwd)
    return {"coin": coin, "horizon_h": horizon, "n": n,
            "ic_level": ic_l, "t_level": ic_l * math.sqrt(n) if ic_l is not None else None,
            "ic_change": ic_c, "t_change": ic_c * math.sqrt(n) if ic_c is not None else None,
            "in_mkt": len(active) / n if n else None,
            "hit_rate": sum(x > 0 for x in active) / len(active) if active else None,
            "sharpe_follow": sharpe(follow), "sharpe_hold": sharpe(hold),
            "days": (hs[-1] - hs[0]) / (24 * HOUR_MS),
            "avg_wallets": statistics.mean(r["n_wallets"] for r in by_h.values())}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "HYPE"])
    ap.add_argument("--horizons", nargs="+", type=int, default=[24, 72])
    ap.add_argument("--addresses", nargs="+")
    ap.add_argument("--min-wallets", type=int, default=3,
                    help="drop hours where fewer basket wallets have known positions")
    args = ap.parse_args()

    basket = load_basket(args.addresses)
    print(f"basket: {len(basket)} wallets", file=sys.stderr)
    for r in basket:
        print(f"  {r['address']}  {r['style']:17} hold {float(r['med_hold_h']):6.1f}h  "
              f"net/gross {float(r['net_gross']):.2f}  acct ${float(r['account']) / 1e6:.2f}M", file=sys.stderr)

    starts = []
    for r in basket:
        fills = fetch_wallet(r["address"])[0]
        if fills:
            starts.append(min(f["time"] for f in fills))
    now_h = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    # Series starts once min_wallets have known positions.
    first = sorted(starts)[min(args.min_wallets, len(starts)) - 1] // HOUR_MS * HOUR_MS
    hours = list(range(first, now_h + 1, HOUR_MS))
    prices = {c: fetch_candles(c, first, now_h) for c in args.coins}

    rows = basket_frame(basket, args.coins, hours, prices)
    rows = [r for r in rows if r["n_wallets"] >= args.min_wallets]
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT_FILE}", file=sys.stderr)

    fmt = lambda v, s: "—" if v is None else format(v, s)
    print(f"{'coin':5} {'H':>4} {'days':>5} {'n':>4} {'IC_lvl':>6} {'t':>5} {'IC_chg':>6} {'t':>5} "
          f"{'inMkt':>5} {'hit':>5} {'Sh_follow':>9} {'Sh_hold':>7} {'wallets':>7}")
    for c in args.coins:
        for hz in args.horizons:
            e = evaluate(rows, c, hz)
            if not e:
                continue
            print(f"{c:5} {hz:>4} {e['days']:5.0f} {e['n']:>4} {fmt(e['ic_level'], '6.2f')} "
                  f"{fmt(e['t_level'], '5.1f')} {fmt(e['ic_change'], '6.2f')} {fmt(e['t_change'], '5.1f')} "
                  f"{fmt(e['in_mkt'], '5.0%')} {fmt(e['hit_rate'], '5.0%')} {fmt(e['sharpe_follow'], '9.2f')} "
                  f"{fmt(e['sharpe_hold'], '7.2f')} {e['avg_wallets']:7.1f}")

    latest = {r["coin"]: r for r in rows if r["hour"] == rows[-1]["hour"]}
    print("\nlatest basket positioning:")
    for c, r in latest.items():
        print(f"  {c:5} exposure {r['exposure']:+.2f}x equity  breadth {r['breadth']:+.2f}  "
              f"net ${r['net_usd'] / 1e6:+.2f}M  ({r['n_wallets']} wallets)")


if __name__ == "__main__":
    main()
