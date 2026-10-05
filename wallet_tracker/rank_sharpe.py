"""Rank Hyperliquid leaderboard wallets by the Sharpe ratio of their perp PnL.

Pulls the public leaderboard, pre-filters to active wallets, fetches each
wallet's `portfolio` history and computes Sharpe from PnL-based returns
(dPnL / prior equity), so deposits and withdrawals don't count as performance.

Two horizons:
  sharpe_30d  daily returns from the `perpMonth` series   (annualised x sqrt(365))
  sharpe_all  weekly returns from the `perpAllTime` series (annualised x sqrt(52))

Stdlib only. Portfolio responses are cached for 24h in portfolio_cache.json.

    python rank_sharpe.py                          # fetch + rank, write sharpe_rank.csv
    python rank_sharpe.py --min-account 250000 --min-month-vlm 1e7
    python rank_sharpe.py --sort sharpe_30d --top 50
"""
import argparse
import bisect
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL        = "https://api.hyperliquid.xyz/info"
HERE            = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE      = os.path.join(HERE, "portfolio_cache.json")
OUT_FILE        = os.path.join(HERE, "sharpe_rank.csv")
CACHE_TTL_S     = 24 * 3600
REQ_INTERVAL_S  = 1.1      # portfolio is weight 20; limit is 1200 weight/min per IP
DAY_MS          = 86_400_000
WEEK_MS         = 7 * DAY_MS
MIN_DAYS        = 20
MIN_WEEKS       = 26


# ── HTTP ───────────────────────────────────────────────────────────────────────

def http_json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_portfolio(address):
    for attempt in range(6):
        try:
            return http_json(INFO_URL, {"type": "portfolio", "user": address})
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(5 * 2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * 2 ** attempt)
    raise RuntimeError(f"portfolio fetch failed for {address}")


# ── Metrics ────────────────────────────────────────────────────────────────────

def grid_returns(series, step_ms):
    """Resample a portfolio window onto a fixed grid and return dPnL / prior equity."""
    av  = series["accountValueHistory"]
    pnl = series["pnlHistory"]
    if len(av) < 3 or len(av) != len(pnl):
        return []
    ts   = [p[0] for p in av]
    avs  = [float(p[1]) for p in av]
    pnls = [float(p[1]) for p in pnl]
    floor = 0.1 * statistics.median(avs)   # skip periods where the account was ~empty

    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        return avs[i], pnls[i]

    rets = []
    t = ts[0]
    prev_av, prev_pnl = at(t)
    while t + step_ms <= ts[-1]:
        t += step_ms
        cur_av, cur_pnl = at(t)
        dpnl = cur_pnl - prev_pnl
        # Capital at work: prior equity plus any net deposits made during the period.
        base = max(prev_av, cur_av - dpnl)
        if base > 0 and base >= floor:
            rets.append(dpnl / base)
        prev_av, prev_pnl = cur_av, cur_pnl
    return rets


def sharpe(rets, periods_per_year):
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    if sd == 0:
        return None
    return statistics.mean(rets) / sd * math.sqrt(periods_per_year)


def max_drawdown(rets):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd


def last_pnl(series):
    h = series.get("pnlHistory") or []
    return float(h[-1][1]) if h else 0.0


def wallet_metrics(portfolio, lb_pnl_30d):
    windows = dict(portfolio)
    pf_pnl_30d = last_pnl(windows.get("month", {}))
    # Leaderboard PnL that the portfolio endpoint can't see -> subaccounts / vault equity.
    lb_match = abs(pf_pnl_30d - lb_pnl_30d) <= 0.25 * max(abs(lb_pnl_30d), abs(pf_pnl_30d)) + 1_000
    d = grid_returns(windows["perpMonth"], DAY_MS) if "perpMonth" in windows else []
    w = grid_returns(windows["perpAllTime"], WEEK_MS) if "perpAllTime" in windows else []
    first_ts = windows.get("perpAllTime", {}).get("accountValueHistory", [[None]])[0][0]
    return {
        "lb_match":   lb_match,
        "perp_pnl_30d": last_pnl(windows.get("perpMonth", {})),
        "sharpe_30d": sharpe(d, 365) if len(d) >= MIN_DAYS else None,
        "sharpe_all": sharpe(w, 52) if len(w) >= MIN_WEEKS else None,
        "n_days":     len(d),
        "n_weeks":    len(w),
        "ret_30d":    math.prod(1 + r for r in d) - 1 if d else None,
        "mdd_30d":    max_drawdown(d) if d else None,
        "mdd_all":    max_drawdown(w) if w else None,
        "win_days":   sum(r > 0 for r in d) / len(d) if d else None,
        "history_days": (time.time() * 1000 - first_ts) / DAY_MS if first_ts else None,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def candidates(rows, min_account, min_month_vlm):
    out = []
    for r in rows:
        perf = dict(r["windowPerformances"])
        if (float(r["accountValue"]) >= min_account
                and float(perf["allTime"]["pnl"]) > 0
                and float(perf["month"]["vlm"]) >= min_month_vlm):
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-account", type=float, default=100_000)
    ap.add_argument("--min-month-vlm", type=float, default=10_000_000)
    ap.add_argument("--sort", choices=["sharpe_all", "sharpe_30d"], default="sharpe_all")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--refresh", action="store_true", help="ignore cached portfolios")
    args = ap.parse_args()

    rows = http_json(LEADERBOARD_URL, timeout=60)["leaderboardRows"]
    cands = candidates(rows, args.min_account, args.min_month_vlm)
    print(f"{len(rows)} leaderboard wallets, {len(cands)} candidates", file=sys.stderr)

    cache = load_cache()
    now = time.time()
    results = []
    for i, r in enumerate(cands, 1):
        addr = r["ethAddress"]
        hit = cache.get(addr)
        if args.refresh or not hit or now - hit["fetched_at"] > CACHE_TTL_S:
            try:
                hit = {"fetched_at": time.time(), "portfolio": fetch_portfolio(addr)}
            except Exception as e:
                print(f"  skip {addr}: {e}", file=sys.stderr)
                continue
            cache[addr] = hit
            time.sleep(REQ_INTERVAL_S)
            if i % 50 == 0:
                save_cache(cache)
                print(f"  {i}/{len(cands)}", file=sys.stderr)

        perf = dict(r["windowPerformances"])
        acct = float(r["accountValue"])
        results.append({
            "address":   addr,
            "name":      r.get("displayName") or "",
            "account":   acct,
            "pnl_30d":   float(perf["month"]["pnl"]),
            "pnl_all":   float(perf["allTime"]["pnl"]),
            "vlm_30d":   float(perf["month"]["vlm"]),
            "turnover_30d": float(perf["month"]["vlm"]) / acct if acct else None,
            **wallet_metrics(hit["portfolio"], float(perf["month"]["pnl"])),
        })
    save_cache(cache)

    key = args.sort
    results.sort(key=lambda x: (x["lb_match"], x[key] is not None, x[key] or 0), reverse=True)
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    print(f"{sum(not x['lb_match'] for x in results)} wallets don't match the leaderboard "
          "(subaccounts/vaults) and are ranked last", file=sys.stderr)
    print(f"wrote {len(results)} rows to {OUT_FILE}", file=sys.stderr)

    fmt = lambda v, s: "—" if v is None else format(v, s)
    print(f"{'#':>3} {'address':42} {'name':14} {'Sh_all':>6} {'Sh_30d':>6} {'wks':>3} "
          f"{'ret30d':>7} {'mdd_all':>7} {'acct$M':>7} {'pnlAll$M':>8} {'perp30d$M':>9} {'turn30d':>7}")
    for n, x in enumerate(results[:args.top], 1):
        print(f"{n:>3} {x['address']:42} {x['name'][:14]:14} {fmt(x['sharpe_all'], '6.2f')} "
              f"{fmt(x['sharpe_30d'], '6.2f')} {x['n_weeks']:>3} {fmt(x['ret_30d'], '7.1%')} "
              f"{fmt(x['mdd_all'], '7.1%')} {x['account'] / 1e6:7.2f} {x['pnl_all'] / 1e6:8.2f} "
              f"{x['perp_pnl_30d'] / 1e6:9.2f} {fmt(x['turnover_30d'], '7.0f')}")


if __name__ == "__main__":
    main()
