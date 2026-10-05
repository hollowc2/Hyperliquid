"""Profile the Sharpe-ranked shortlist from their fills and label each wallet's style.

For each wallet in sharpe_rank.csv that passes the shortlist filter, pulls:
  - fills (userFillsByTime, ~10k most recent are retrievable)
  - current positions (clearinghouseState)
  - last 30 days of funding (userFunding)

and derives maker share, holding times (from per-coin position round trips rebuilt
via each fill's startPosition), net/gross exposure, coin mix and funding share.

Stdlib only. Raw API responses are cached for 24h in cache/.

    python profile_wallets.py                  # profile shortlist, write wallet_profiles.csv
    python profile_wallets.py --min-weeks 26 --max-dd 0.6
"""
import argparse
import bisect
import csv
import json
import os
import statistics
import sys
import time
from collections import defaultdict

from rank_sharpe import INFO_URL, OUT_FILE as RANK_FILE, http_json

HERE        = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR   = os.path.join(HERE, "cache")
OUT_FILE    = os.path.join(HERE, "wallet_profiles.csv")
CACHE_TTL_S = 24 * 3600
WEIGHT_PER_MIN = 1100      # API limit is 1200/min per IP; leave headroom
HOUR_MS     = 3_600_000
DAY_MS      = 24 * HOUR_MS


# ── API (weight-throttled, cached) ─────────────────────────────────────────────

def info(payload, weight):
    for attempt in range(6):
        try:
            out = http_json(INFO_URL, payload)
            time.sleep(weight / WEIGHT_PER_MIN * 60)
            return out
        except Exception as e:
            if attempt == 5:
                raise
            print(f"  retry {payload['type']}: {e}", file=sys.stderr)
            time.sleep(5 * 2 ** attempt)


def paginate(payload, page_size, weight):
    """Walk a startTime-paginated endpoint forward until a short page."""
    out, seen = [], set()
    start = payload.get("startTime", 0)
    while True:
        page = info({**payload, "startTime": start}, weight)
        new = [x for x in page if json.dumps(x, sort_keys=True) not in seen]
        seen.update(json.dumps(x, sort_keys=True) for x in new)
        out += new
        if len(page) < page_size or not new:
            return out
        start = page[-1]["time"]


def cached(name, fetch):
    path = os.path.join(CACHE_DIR, name + ".json")
    try:
        with open(path) as f:
            hit = json.load(f)
        if time.time() - hit["fetched_at"] < CACHE_TTL_S:
            return hit["data"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    data = fetch()
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump({"fetched_at": time.time(), "data": data}, f)
    os.replace(path + ".tmp", path)
    return data


def fetch_wallet(addr):
    fills = cached(f"fills_{addr}", lambda: paginate(
        {"type": "userFillsByTime", "user": addr, "startTime": 0, "aggregateByTime": True}, 2000, 120))
    state = cached(f"state_{addr}", lambda: info({"type": "clearinghouseState", "user": addr}, 2))
    since = int(time.time() * 1000) - 30 * DAY_MS
    funding = cached(f"funding_{addr}", lambda: paginate(
        {"type": "userFunding", "user": addr, "startTime": since}, 500, 45))
    return fills, state, funding


# ── Position reconstruction ────────────────────────────────────────────────────

def is_perp(fill):
    return fill["dir"] not in ("Buy", "Sell") and not fill["coin"].startswith("@") and "/" not in fill["coin"]


def signed_sz(fill):
    sz = float(fill["sz"])
    return sz if fill["side"] == "B" else -sz


def position_paths(fills, state):
    """coin -> (times, [(time_ms, position_after, px)]) including a seed at the first fill.

    Coins held now but never filled inside the fill window are seeded as constant
    from the earliest fill time, so they still count toward exposure.
    """
    paths = defaultdict(list)
    for f in sorted((f for f in fills if is_perp(f)), key=lambda f: f["time"]):
        c = f["coin"]
        if not paths[c]:
            paths[c].append((f["time"] - 1, float(f["startPosition"]), float(f["px"])))
        after = round(float(f["startPosition"]) + signed_sz(f), 10)
        paths[c].append((f["time"], after, float(f["px"])))
    t0 = min((p[0][0] for p in paths.values()), default=int(time.time() * 1000))
    for ap in state.get("assetPositions", []):
        p = ap["position"]
        if p["coin"] not in paths:
            px = float(p["positionValue"]) / abs(float(p["szi"])) if float(p["szi"]) else 0.0
            paths[p["coin"]].append((t0, float(p["szi"]), px))
    return {c: ([x[0] for x in path], path) for c, path in paths.items()}


def position_at(times_path, t):
    """Position and last-known price at time t (None before the path starts)."""
    times, path = times_path
    i = bisect.bisect_right(times, t) - 1
    return (path[i][1], path[i][2]) if i >= 0 else (None, path[0][2])


def round_trips(fills):
    """Closed flat->flat (or flip) trips per coin: [(coin, duration_ms, peak_notional)]."""
    FLAT, UNKNOWN = "flat", "unknown"
    state, peak, trips = {}, defaultdict(float), []
    for f in sorted((f for f in fills if is_perp(f)), key=lambda f: f["time"]):
        c, t, px = f["coin"], f["time"], float(f["px"])
        before = float(f["startPosition"])
        after = round(before + signed_sz(f), 10)
        if c not in state:
            state[c] = FLAT if before == 0 else UNKNOWN
        if before == 0:
            state[c], peak[c] = t, 0.0
        peak[c] = max(peak[c], abs(after) * px)
        closed = after == 0 or before * after < 0
        if closed:
            if state[c] not in (FLAT, UNKNOWN):
                trips.append((c, t - state[c], peak[c]))
            state[c], peak[c] = (t, abs(after) * px) if after != 0 else (FLAT, 0.0)
    return trips


def weighted_median(values, weights):
    pairs = sorted(zip(values, weights))
    half, acc = sum(weights) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return None


# ── Profile ────────────────────────────────────────────────────────────────────

def classify(p):
    if p["spot_share"] > 0.5:
        return "spot"
    if p["med_hold_h"] is None:
        return "unknown"
    if p["maker_share"] >= 0.7 and p["med_hold_h"] < 6:
        return "market maker"
    if p["funding_30d"] > 0 and p["funding_30d"] > 0.5 * abs(p["perp_pnl_30d"]) and p["net_gross"] < 0.5:
        return "carry / hedged"
    if p["net_gross"] < 0.3:
        return "hedged / relative value"
    if p["med_hold_h"] < 6:
        return "scalper"
    if p["med_hold_h"] < 72:
        return "short swing"
    return "swing / position"


def profile(row, fills, state, funding):
    perp = [f for f in fills if is_perp(f)]
    notional = lambda fs: sum(float(f["sz"]) * float(f["px"]) for f in fs)
    total_n = notional(fills) or 1.0
    perp_n = notional(perp) or 1.0
    t_first = min((f["time"] for f in fills), default=None)
    t_last = max((f["time"] for f in fills), default=None)
    span_d = (t_last - t_first) / DAY_MS if fills else 0

    trips = round_trips(fills)
    holds_h = [d / HOUR_MS for _, d, _ in trips]
    by_coin = defaultdict(float)
    for f in perp:
        by_coin[f["coin"]] += float(f["sz"]) * float(f["px"])
    top = sorted(by_coin.items(), key=lambda kv: -kv[1])[:3]

    opens = [f for f in perp if f["dir"].startswith("Open")]
    long_open_share = notional([f for f in opens if f["dir"] == "Open Long"]) / (notional(opens) or 1.0)

    # Net/gross exposure on a daily grid across the fill window.
    paths = position_paths(fills, state)
    nets, grosses = [], []
    if fills:
        for t in range(t_first, t_last + 1, DAY_MS):
            net = gross = 0.0
            for path in paths.values():
                pos, px = position_at(path, t)
                if pos:
                    net += pos * px
                    gross += abs(pos * px)
            if gross > 0:
                nets.append(abs(net))
                grosses.append(gross)

    ms = state.get("marginSummary", {})
    acct = float(ms.get("accountValue", 0) or 0)
    p = {
        "address":      row["address"],
        "name":         row["name"],
        "sharpe_all":   float(row["sharpe_all"]),
        "account":      float(row["account"]),
        "pnl_all":      float(row["pnl_all"]),
        "perp_pnl_30d": float(row["perp_pnl_30d"]),
        "n_fills":      len(fills),
        "window_days":  span_d,
        "fills_per_day": len(fills) / span_d if span_d else None,
        "maker_share":  notional([f for f in perp if not f["crossed"]]) / perp_n,
        "spot_share":   1 - notional(perp) / total_n,
        "hip3_share":   sum(v for c, v in by_coin.items() if ":" in c) / perp_n,
        "n_trips":      len(trips),
        "med_hold_h":   statistics.median(holds_h) if holds_h else None,
        "wmed_hold_h":  weighted_median(holds_h, [n for _, _, n in trips]) if trips else None,
        "long_open_share": long_open_share,
        "net_gross":    sum(nets) / sum(grosses) if grosses else None,
        "lev_now":      float(ms.get("totalNtlPos", 0) or 0) / acct if acct else None,
        "funding_30d":  sum(float(x["delta"]["usdc"]) for x in funding),   # + = received
        "top_coins":    " ".join(f"{c}:{v / perp_n:.0%}" for c, v in top),
    }
    if p["net_gross"] is None:
        p["net_gross"] = 1.0
    p["style"] = classify(p)
    return p


def shortlist(min_weeks, max_dd):
    with open(RANK_FILE) as f:
        rows = [r for r in csv.DictReader(f)
                if r["lb_match"] == "True" and r["sharpe_all"]
                and int(r["n_weeks"]) >= min_weeks and float(r["mdd_all"]) > -max_dd]
    return sorted(rows, key=lambda r: -float(r["sharpe_all"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-weeks", type=int, default=52)
    ap.add_argument("--max-dd", type=float, default=0.5, help="max all-time drawdown, as a fraction")
    args = ap.parse_args()

    rows = shortlist(args.min_weeks, args.max_dd)
    print(f"profiling {len(rows)} wallets", file=sys.stderr)
    profiles = []
    for i, row in enumerate(rows, 1):
        print(f"  {i}/{len(rows)} {row['address']}", file=sys.stderr)
        profiles.append(profile(row, *fetch_wallet(row["address"])))

    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(profiles[0]))
        w.writeheader()
        w.writerows(profiles)
    print(f"wrote {OUT_FILE}", file=sys.stderr)

    fmt = lambda v, s: "—" if v is None else format(v, s)
    print(f"{'#':>2} {'address':12} {'style':23} {'Sh':>4} {'fills':>5} {'win_d':>5} {'maker':>5} "
          f"{'medHold':>7} {'wHold':>7} {'long%':>5} {'net/gr':>6} {'lev':>4} {'fund30d$k':>9} top coins")
    for n, p in enumerate(profiles, 1):
        print(f"{n:>2} {p['address'][:12]} {p['style']:23} {p['sharpe_all']:4.1f} {p['n_fills']:>5} "
              f"{p['window_days']:5.0f} {p['maker_share']:5.0%} {fmt(p['med_hold_h'], '6.1f')}h "
              f"{fmt(p['wmed_hold_h'], '6.1f')}h {p['long_open_share']:5.0%} {p['net_gross']:6.2f} "
              f"{fmt(p['lev_now'], '4.1f')} {p['funding_30d'] / 1e3:9.1f} {p['top_coins']}")


if __name__ == "__main__":
    main()
