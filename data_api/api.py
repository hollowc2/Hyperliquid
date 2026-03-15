"""
HyperliquidPublicAPI — Free, no-auth API client
================================================
Replaces the original paid hosted client with direct calls to:
  - Hyperliquid public REST: https://api.hyperliquid.xyz/info
  - Binance Futures public REST: https://fapi.binance.com/fapi/v1

No API key required. All endpoints are free and publicly accessible.

Available Methods:
-----------------
HEALTH:
- health()                              - Check HL API reachability

TICK DATA:
- get_tick_latest()                     - Current prices for TRACKED_COINS
- get_ticks(symbol, timeframe)          - Historical OHLCV candles
- get_tick_stats()                      - Synthetic stats from metaAndAssetCtxs

ORDER FLOW & TRADES:
- get_trades()                          - Recent ~250 trades (5 coins × ~50)
- get_large_trades()                    - Trades >$100k filtered from get_trades()
- get_orderflow(tf)                     - Buy/sell pressure proxy from candles [APPROX]
- get_orderflow_stats()                 - 24h volume from metaAndAssetCtxs
- get_imbalance(tf)                     - Buy/sell imbalance for a timeframe [APPROX]

LIQUIDATIONS (Hyperliquid):
- get_liquidations(tf)                  - HLP liquidator fills in window [LIMITED]
- get_liquidation_stats()               - Aggregated across timeframes

POSITIONS:
- get_positions()                       - Large positions from known HLP wallets [LIMITED]

USER DATA:
- get_user_positions(address)           - Open positions for any HL wallet
- get_user_positions_api(address)       - Delegates to get_user_positions()
- get_user_fills(address, limit)        - Historical fills for any HL wallet

HLP (HYPERLIQUIDITY PROVIDER):
- get_hlp_positions(include_strategies) - Positions across known HLP addresses
- get_hlp_trades(limit)                 - Fills from HLP addresses
- get_hlp_trade_stats()                 - Aggregated HLP trade stats
- get_hlp_liquidators()                 - Liquidator activation status
- get_hlp_deltas(hours)                 - Net exposure snapshot
- get_hlp_position_history(hours)       - Single current snapshot

STUBS (raise NotImplementedError — require sustained infrastructure or paid auth):
- get_whales()
- get_whale_addresses()
- get_events()
- get_contracts()
- get_smart_money_rankings()
- get_smart_money_leaderboard()
- get_smart_money_signals(tf)
- get_binance_liquidations(tf)          - Binance forceOrders now requires API key auth
- get_binance_liquidation_stats()       - Same; stubs out cleanly

Approximation notes:
  - get_orderflow / get_imbalance: REST OHLCV proxy, not live cumulative delta
  - get_liquidations: Only Strategy A address confirmed; ADL excluded
  - get_positions: Only known HLP wallet coverage
  - HL has no 10m candle resolution — "10m" maps to "15m"
"""

import time
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed


class HyperliquidPublicAPI:
    """Free, no-auth Hyperliquid + Binance Futures API client."""

    HL_URL = "https://api.hyperliquid.xyz/info"
    BN_URL = "https://fapi.binance.com/fapi/v1"

    TRACKED_COINS = ["BTC", "ETH", "HYPE", "SOL", "XRP"]

    BINANCE_SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    ]

    # Known HLP vault addresses.
    # Strategy A confirmed from existing codebase.
    # Others marked as unknown — methods skip sentinel addresses.
    HLP_ADDRESSES = {
        "HLP Strategy A":   "0x010461c14e146ac35fe42271bdc1134ee31c703a",
        "HLP Strategy B":   "0x_UNKNOWN_VERIFY",
        "HLP Liquidator 1": "0x_UNKNOWN_VERIFY",
        "HLP Liquidator 2": "0x_UNKNOWN_VERIFY",
        "HLP Liquidator 3": "0x_UNKNOWN_VERIFY",
        "HLP Liquidator 4": "0x_UNKNOWN_VERIFY",
        "HLP Strategy X":   "0x_UNKNOWN_VERIFY",
    }

    # Timeframe → (HL candle resolution, lookback_ms)
    # NOTE: HL has no 10m candles — "10m" maps to "15m" (closest available)
    TIMEFRAME_MAP = {
        "5m":  ("5m",   300_000),
        "10m": ("15m",  600_000),
        "15m": ("15m",  900_000),
        "1h":  ("1h",   3_600_000),
        "4h":  ("4h",   14_400_000),
        "12h": ("12h",  43_200_000),
        "24h": ("1d",   86_400_000),
        "2d":  ("1d",   172_800_000),
        "7d":  ("1d",   604_800_000),
        "14d": ("1d",   1_209_600_000),
        "30d": ("1d",   2_592_000_000),
    }

    _SENTINEL = "0x_UNKNOWN_VERIFY"

    def __init__(self, **kwargs):
        # Accept and ignore legacy kwargs (e.g., api_key=) so old call sites don't crash
        self.session = requests.Session()
        self._cache = {}  # {key: (inserted_at, data)}

    # ==================== TRANSPORT PRIMITIVES ====================

    def _hl_post(self, payload: dict):
        """POST to Hyperliquid public API. Raises on non-2xx."""
        resp = self.session.post(self.HL_URL, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _bn_get(self, endpoint: str, params: dict = None):
        """GET from Binance Futures public API."""
        url = f"{self.BN_URL}/{endpoint}"
        resp = self.session.get(url, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _cached(self, key: str, ttl: int, fn):
        """Return cached result if fresh (TTL in seconds), else call fn() and cache."""
        now = time.time()
        if key in self._cache:
            inserted_at, data = self._cache[key]
            if now - inserted_at < ttl:
                return data
        result = fn()
        self._cache[key] = (now, result)
        return result

    # ==================== HEALTH ====================

    def health(self):
        """Check API health (uses HL meta endpoint)."""
        self._hl_post({"type": "meta"})
        return {"status": "ok", "source": "hyperliquid_public"}

    # ==================== TICK DATA ====================

    def get_tick_latest(self):
        """Get latest prices for TRACKED_COINS from Hyperliquid allMids. TTL: 15s."""
        def _fetch():
            raw = self._hl_post({"type": "allMids"})
            prices = {}
            for coin in self.TRACKED_COINS:
                val = raw.get(coin)
                if val is not None:
                    try:
                        prices[coin] = float(val)
                    except (ValueError, TypeError):
                        pass
            return {"prices": prices}
        return self._cached("tick_latest", 15, _fetch)

    def get_ticks(self, symbol: str = "btc", timeframe: str = "1h"):
        """
        Get historical OHLCV candle data for a symbol.

        Args:
            symbol: btc, eth, hype, sol, xrp (case-insensitive)
            timeframe: 5m, 10m, 1h, 4h, 24h, 7d (see TIMEFRAME_MAP)
                       NOTE: "10m" maps to "15m" — HL has no 10m candles.

        Returns:
            dict with 'ticks' list; each tick has price (close), timestamp (ms), o, h, l, c, v
        """
        coin = symbol.upper()
        resolution, lookback_ms = self.TIMEFRAME_MAP.get(timeframe, ("1h", 3_600_000))
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback_ms
        cache_key = f"ticks_{coin}_{timeframe}"

        def _fetch():
            raw = self._hl_post({
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": resolution,
                    "startTime": start_ms,
                    "endTime": now_ms,
                }
            })
            ticks = []
            for c in (raw or []):
                ticks.append({
                    "t": c.get("t"),
                    "o": float(c.get("o", 0)),
                    "h": float(c.get("h", 0)),
                    "l": float(c.get("l", 0)),
                    "c": float(c.get("c", 0)),
                    "v": float(c.get("v", 0)),
                    "price": float(c.get("c", 0)),      # close price as canonical price
                    "timestamp": c.get("t"),             # ms timestamp
                })
            return {"ticks": ticks, "symbol": coin, "timeframe": timeframe, "count": len(ticks)}
        return self._cached(cache_key, 60, _fetch)

    def get_tick_stats(self):
        """
        Synthetic tick stats derived from metaAndAssetCtxs. TTL: 30s.
        Returns symbols list + per-coin min/max/volume from 24h context.
        """
        def _fetch():
            raw = self._hl_post({"type": "metaAndAssetCtxs"})
            meta, asset_ctxs = raw[0], raw[1]
            universe = meta.get("universe", [])
            symbol_map = {i: info["name"] for i, info in enumerate(universe)}

            symbol_stats = {}
            total_ticks = 0
            for i, ctx in enumerate(asset_ctxs):
                coin = symbol_map.get(i, f"COIN_{i}")
                if coin not in self.TRACKED_COINS:
                    continue
                prev_px = float(ctx.get("prevDayPx", 0) or 0)
                mark_px = float(ctx.get("markPx", 0) or 0)
                volume = float(ctx.get("dayNtlVlm", 0) or 0)
                lo = min(prev_px, mark_px) if prev_px and mark_px else 0
                hi = max(prev_px, mark_px) if prev_px and mark_px else 0
                tick_count = max(1, int(volume / max(mark_px, 1)))  # proxy count
                total_ticks += tick_count
                symbol_stats[coin] = {
                    "min_price": lo,
                    "max_price": hi,
                    "tick_count": tick_count,
                }
            return {
                "symbols": self.TRACKED_COINS,
                "symbol_stats": symbol_stats,
                "collector_stats": {"ticks_collected": total_ticks},
            }
        return self._cached("tick_stats", 30, _fetch)

    # ==================== ORDER FLOW & TRADES ====================

    def get_trades(self):
        """
        Get recent trades for TRACKED_COINS via recentTrades endpoint. TTL: 15s.
        Merges ~50 trades/coin → ~250 total (REST snapshot limitation, not 500).
        """
        def _fetch():
            all_trades = []
            for coin in self.TRACKED_COINS:
                try:
                    raw = self._hl_post({"type": "recentTrades", "coin": coin})
                    for t in (raw or []):
                        side = t.get("side", "")
                        px = float(t.get("px", 0))
                        sz = float(t.get("sz", 0))
                        all_trades.append({
                            "coin": coin,
                            "side": "BUY" if side == "B" else "SELL",
                            "px": px,
                            "sz": sz,
                            "price": px,
                            "size": sz,
                            "value_usd": px * sz,
                            "timestamp": t.get("time"),
                            "time": t.get("time"),
                        })
                except Exception:
                    continue
            all_trades.sort(key=lambda x: x.get("time", 0), reverse=True)
            return {"trades": all_trades, "total_trades": len(all_trades)}
        return self._cached("recent_trades", 15, _fetch)

    def get_large_trades(self):
        """
        Get large trades >$100k. Filtered from get_trades(). TTL: same as get_trades().
        NOTE: Same depth limitation as get_trades() (~250 total source trades).
        """
        data = self.get_trades()
        trades = data.get("trades", []) if isinstance(data, dict) else data
        large = [t for t in trades if t.get("value_usd", 0) >= 100_000]
        return {"trades": large, "total": len(large)}

    def _compute_orderflow_for_coins(self, tf: str = "1h"):
        """
        Compute buy/sell pressure from candleSnapshot for all TRACKED_COINS.
        Approximation: candles where close >= open = buy volume; close < open = sell volume.
        NOTE: REST snapshot approximation — not live cumulative delta.
        """
        resolution, lookback_ms = self.TIMEFRAME_MAP.get(tf, ("1h", 3_600_000))
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback_ms

        results = {}
        for coin in self.TRACKED_COINS:
            try:
                candles = self._hl_post({
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": resolution,
                            "startTime": start_ms, "endTime": now_ms}
                })
                buy_vol = sell_vol = 0.0
                for c in (candles or []):
                    vol = float(c.get("v", 0))
                    o, cl = float(c.get("o", 0)), float(c.get("c", 0))
                    if cl >= o:
                        buy_vol += vol
                    else:
                        sell_vol += vol
                total_vol = buy_vol + sell_vol
                bp = (buy_vol / total_vol) if total_vol > 0 else 0.5
                delta = buy_vol - sell_vol
                dominant = "BUY" if bp > 0.55 else ("SELL" if bp < 0.45 else "NEUTRAL")
                results[coin] = {
                    "buy_pressure": bp,
                    "cumulative_delta": delta,
                    "dominant_side": dominant,
                    "buy_volume": buy_vol,
                    "sell_volume": sell_vol,
                }
            except Exception:
                results[coin] = {
                    "buy_pressure": 0.5, "cumulative_delta": 0,
                    "dominant_side": "NEUTRAL", "buy_volume": 0, "sell_volume": 0,
                }
        return results

    def get_orderflow(self, tf: str = "1h"):
        """
        Get order flow imbalance by timeframe and coin. TTL: 60s.
        NOTE: REST approximation using OHLCV candle buy/sell proxy — not live cumulative delta.
        Returns dict with 'windows' (5m/15m/1h/4h) and 'by_coin' for the requested tf.
        """
        cache_key = f"orderflow_{tf}"
        def _fetch():
            windows = {}
            for window_tf in ["5m", "15m", "1h", "4h"]:
                per_coin = self._compute_orderflow_for_coins(window_tf)
                all_buy = sum(v["buy_volume"] for v in per_coin.values())
                all_sell = sum(v["sell_volume"] for v in per_coin.values())
                total = all_buy + all_sell
                bp = (all_buy / total) if total > 0 else 0.5
                windows[window_tf] = {
                    "buy_pressure": bp,
                    "cumulative_delta": all_buy - all_sell,
                    "dominant_side": "BUY" if bp > 0.55 else ("SELL" if bp < 0.45 else "NEUTRAL"),
                    "buy_volume": all_buy,
                    "sell_volume": all_sell,
                }
            by_coin = self._compute_orderflow_for_coins(tf)
            return {"windows": windows, "by_coin": by_coin}
        return self._cached(cache_key, 60, _fetch)

    def get_orderflow_stats(self):
        """
        Order flow service stats derived from metaAndAssetCtxs 24h volume. TTL: 30s.
        NOTE: trades_per_second and exact buy/sell split unavailable from REST snapshots.
        Buy/sell split approximated from 1h candle proxy.
        """
        def _fetch():
            raw = self._hl_post({"type": "metaAndAssetCtxs"})
            meta, asset_ctxs = raw[0], raw[1]
            universe = meta.get("universe", [])
            symbol_map = {i: info["name"] for i, info in enumerate(universe)}
            total_vol = 0.0
            for i, ctx in enumerate(asset_ctxs):
                if symbol_map.get(i) not in self.TRACKED_COINS:
                    continue
                total_vol += float(ctx.get("dayNtlVlm", 0) or 0)

            # Get actual buy/sell split from 1h candle proxy
            per_coin = self._compute_orderflow_for_coins("1h")
            buy_vol_1h = sum(v["buy_volume"] for v in per_coin.values())
            sell_vol_1h = sum(v["sell_volume"] for v in per_coin.values())
            total_1h = buy_vol_1h + sell_vol_1h
            buy_ratio = buy_vol_1h / total_1h if total_1h > 0 else 0.5

            return {
                "total_trades": None,  # unavailable via REST
                "total_volume_usd": total_vol,
                "buy_volume_usd": total_vol * buy_ratio,
                "sell_volume_usd": total_vol * (1 - buy_ratio),
                "trades_per_second": None,
                "source": "hyperliquid_public",
                "note": "24h volume from metaAndAssetCtxs; buy/sell split approximated from 1h candles",
            }
        return self._cached("orderflow_stats", 30, _fetch)

    def get_imbalance(self, timeframe: str = "1h"):
        """
        Get buy/sell imbalance for a given timeframe. TTL: 60s.
        NOTE: REST approximation using OHLCV candle proxy — not live order book data.
        """
        cache_key = f"imbalance_{timeframe}"
        def _fetch():
            per_coin = self._compute_orderflow_for_coins(timeframe)
            all_buy = sum(v["buy_volume"] for v in per_coin.values())
            all_sell = sum(v["sell_volume"] for v in per_coin.values())
            total = all_buy + all_sell
            ratio = (all_buy / total) if total > 0 else 0.5
            return {
                "buy_volume": all_buy,
                "sell_volume": all_sell,
                "ratio": ratio,
                "imbalance": ratio - 0.5,
                "timeframe": timeframe,
                "by_coin": per_coin,
            }
        return self._cached(cache_key, 60, _fetch)

    # ==================== LIQUIDATIONS ====================

    def get_liquidations(self, timeframe: str = "1h"):
        """
        Get Hyperliquid liquidations for the given timeframe. TTL: 60s.
        Aggregates userFillsByTime from known HLP Liquidator addresses.

        NOTE: Only HLP-handled liquidations captured. ADL events excluded.
              Only Strategy A address is confirmed — coverage is limited to that wallet.
              Liquidator addresses (1-4) are unverified and return no data.
        """
        _, lookback_ms = self.TIMEFRAME_MAP.get(timeframe, ("1h", 3_600_000))
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback_ms
        cache_key = f"liquidations_{timeframe}"

        def _fetch():
            all_fills = []
            for name, addr in self.HLP_ADDRESSES.items():
                if addr == self._SENTINEL:
                    continue
                try:
                    end_cursor = now_ms
                    for _ in range(5):  # max 5 pages per address
                        fills = self._hl_post({
                            "type": "userFillsByTime",
                            "user": addr,
                            "startTime": start_ms,
                            "endTime": end_cursor,
                        })
                        if not fills:
                            break
                        for f in fills:
                            px = float(f.get("px", 0))
                            sz = float(f.get("sz", 0))
                            side = f.get("side", "")
                            liq_side = "short" if side == "B" else "long"
                            all_fills.append({
                                "coin": f.get("coin", ""),
                                "side": liq_side,
                                "value_usd": px * sz,
                                "price": px,
                                "size": sz,
                                "timestamp": f.get("time"),
                                "address": addr,
                            })
                        if len(fills) < 2000:
                            break  # Got all available fills in window
                        # Paginate backwards: next page ends before the oldest fill we have
                        end_cursor = fills[0]["time"] - 1
                        if end_cursor <= start_ms:
                            break
                except Exception:
                    continue

            # Deduplicate by (coin, timestamp, size) in case of overlap
            seen = set()
            unique_fills = []
            for f in all_fills:
                key = (f["coin"], f["timestamp"], f["size"])
                if key not in seen:
                    seen.add(key)
                    unique_fills.append(f)
            all_fills = unique_fills

            long_fills = [f for f in all_fills if f["side"] == "long"]
            short_fills = [f for f in all_fills if f["side"] == "short"]
            total_value = sum(f["value_usd"] for f in all_fills)
            long_value = sum(f["value_usd"] for f in long_fills)
            short_value = sum(f["value_usd"] for f in short_fills)

            by_coin = {}
            for f in all_fills:
                coin = f["coin"]
                if coin not in by_coin:
                    by_coin[coin] = {"count": 0, "total_value": 0, "long_value": 0, "short_value": 0}
                by_coin[coin]["count"] += 1
                by_coin[coin]["total_value"] += f["value_usd"]
                if f["side"] == "long":
                    by_coin[coin]["long_value"] += f["value_usd"]
                else:
                    by_coin[coin]["short_value"] += f["value_usd"]

            largest = sorted(all_fills, key=lambda x: x["value_usd"], reverse=True)[:20]

            # Actual coverage: oldest fill timestamp vs requested start
            actual_oldest = min((f["timestamp"] for f in all_fills), default=now_ms)
            actual_coverage_ms = now_ms - actual_oldest
            capped = len(all_fills) > 0 and (now_ms - actual_oldest) < (lookback_ms * 0.9)

            return {
                "stats": {
                    "total_count": len(all_fills),
                    "total_value_usd": total_value,
                    "long_count": len(long_fills),
                    "short_count": len(short_fills),
                    "long_value_usd": long_value,
                    "short_value_usd": short_value,
                    "by_coin": by_coin,
                    "largest": largest,
                    "capped": capped,
                    "actual_coverage_ms": actual_coverage_ms,
                },
                "timeframe": timeframe,
                "note": "Only HLP Strategy A fills (liquidator addresses unverified). ADL excluded.",
            }
        return self._cached(cache_key, 60, _fetch)

    def get_liquidation_stats(self):
        """
        Aggregated liquidation stats across standard timeframes. TTL: 60s.
        NOTE: Same limitations as get_liquidations().
        """
        def _fetch():
            windows = {}
            for tf in ["10m", "1h", "4h", "12h", "24h"]:
                try:
                    data = self.get_liquidations(tf)
                    windows[tf] = data.get("stats", {})
                except Exception:
                    windows[tf] = {}
            return {"windows": windows}
        return self._cached("liquidation_stats", 60, _fetch)

    # ==================== POSITIONS ====================

    def get_positions(self):
        """
        Get large positions (>=$200k) from known HLP addresses. TTL: 60s.
        NOTE: Coverage limited to confirmed HLP wallets (Strategy A only currently).
        """
        def _fetch():
            longs = []
            shorts = []
            for name, addr in self.HLP_ADDRESSES.items():
                if addr == self._SENTINEL:
                    continue
                try:
                    data = self._hl_post({"type": "clearinghouseState", "user": addr})
                    for ap in data.get("assetPositions", []):
                        p = ap.get("position", {})
                        size = float(p.get("szi", 0))
                        if size == 0:
                            continue
                        pos_value = abs(float(p.get("positionValue", 0)))
                        if pos_value < 200_000:
                            continue
                        entry_px = float(p.get("entryPx", 0) or 0)
                        liq_px = float(p.get("liquidationPx", 0) or 0)
                        current_px = pos_value / abs(size) if size else 0
                        distance_pct = (abs(liq_px - current_px) / current_px * 100
                                        if current_px and liq_px else 100)
                        lev = p.get("leverage", {})
                        leverage_val = lev.get("value", 0) if isinstance(lev, dict) else 0
                        pos_obj = {
                            "address": addr,
                            "coin": p.get("coin", ""),
                            "value": pos_value,
                            "entry_price": entry_px,
                            "liq_price": liq_px,
                            "distance_pct": distance_pct,
                            "pnl": float(p.get("unrealizedPnl", 0)),
                            "leverage": float(leverage_val),
                            "strategy": name,
                        }
                        if size > 0:
                            longs.append(pos_obj)
                        else:
                            shorts.append(pos_obj)
                except Exception:
                    continue
            return {
                "total_positions": len(longs) + len(shorts),
                "total_longs": len(longs),
                "total_shorts": len(shorts),
                "longs": longs,
                "shorts": shorts,
                "min_position_value": 200_000,
                "note": "Coverage limited to confirmed HLP addresses only.",
            }
        return self._cached("positions", 60, _fetch)

    # ==================== USER DATA ====================

    def get_user_positions(self, address: str):
        """
        Get all open positions for a Hyperliquid wallet address.

        Args:
            address: Hyperliquid wallet address (e.g., "0x...")

        Returns:
            dict with 'assetPositions' list and 'marginSummary'
        """
        return self._hl_post({"type": "clearinghouseState", "user": address})

    def get_user_positions_api(self, address: str):
        """Delegate to get_user_positions() — proxy no longer needed."""
        return self.get_user_positions(address)

    def get_user_fills(self, address: str, limit: int = 100):
        """
        Get historical fills for a Hyperliquid wallet.

        Args:
            address: Hyperliquid wallet address
            limit: Number of most recent fills to return (default: 100, use -1 for ALL)

        Returns:
            dict with 'fills', 'total', 'limit', 'address'
        """
        raw = self._hl_post({"type": "userFills", "user": address})
        fills = raw if isinstance(raw, list) else []
        total = len(fills)
        if limit != -1 and limit > 0:
            fills = fills[-limit:]
        return {"fills": fills, "total": total, "limit": limit, "address": address}

    # ==================== HLP (HYPERLIQUIDITY PROVIDER) ====================

    def _get_hlp_strategy_data(self, name: str, addr: str):
        """Fetch clearinghouse state for one HLP address and return normalized dict."""
        data = self._hl_post({"type": "clearinghouseState", "user": addr})
        margin = data.get("marginSummary", {})
        account_value = float(margin.get("accountValue", 0))
        positions = []
        total_pnl = 0.0
        for ap in data.get("assetPositions", []):
            p = ap.get("position", {})
            size = float(p.get("szi", 0))
            if size == 0:
                continue
            pos_value = abs(float(p.get("positionValue", 0)))
            pnl = float(p.get("unrealizedPnl", 0))
            total_pnl += pnl
            positions.append({
                "coin": p.get("coin", ""),
                "size": size,
                "position_value": pos_value,
                "unrealized_pnl": pnl,
                "entry_price": float(p.get("entryPx", 0) or 0),
            })
        return {
            "name": name,
            "account_value": account_value,
            "total_pnl": total_pnl,
            "position_count": len(positions),
            "positions": positions,
        }

    def get_hlp_positions(self, include_strategies: bool = True):
        """
        Get HLP positions across all confirmed strategy addresses. TTL: 60s.
        NOTE: Only Strategy A address is confirmed. Other strategies return no data
              until their addresses are verified and updated in HLP_ADDRESSES.

        Returns:
            dict with 'hlp_summary', 'combined_net_positions', 'strategies', 'exposure'
        """
        cache_key = f"hlp_positions_{include_strategies}"

        def _fetch():
            valid = [(n, a) for n, a in self.HLP_ADDRESSES.items() if a != self._SENTINEL]

            strategy_map = {}
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = {ex.submit(self._get_hlp_strategy_data, n, a): n for n, a in valid}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        strategy_map[name] = future.result()
                    except Exception:
                        strategy_map[name] = {
                            "name": name, "account_value": 0, "total_pnl": 0,
                            "position_count": 0, "positions": [],
                        }

            # Preserve original ordering
            strategies = []
            for name in self.HLP_ADDRESSES:
                if name in strategy_map:
                    strategies.append(strategy_map[name])

            total_value = sum(s["account_value"] for s in strategies)
            total_positions = sum(s["position_count"] for s in strategies)
            total_pnl = sum(s["total_pnl"] for s in strategies)

            net_by_coin = {}
            for strategy in strategies:
                for pos in strategy["positions"]:
                    coin = pos["coin"]
                    if coin not in net_by_coin:
                        net_by_coin[coin] = {"coin": coin, "net_size": 0, "long_value": 0, "short_value": 0}
                    size = pos["size"]
                    value = pos["position_value"]
                    net_by_coin[coin]["net_size"] += size
                    if size > 0:
                        net_by_coin[coin]["long_value"] += value
                    else:
                        net_by_coin[coin]["short_value"] += value

            combined_net = []
            for coin, d in net_by_coin.items():
                net_value = d["long_value"] - d["short_value"]
                combined_net.append({
                    "coin": coin,
                    "net_size": d["net_size"],
                    "net_value": net_value,
                    "long_value": d["long_value"],
                    "short_value": d["short_value"],
                })
            combined_net.sort(key=lambda x: abs(x["net_value"]), reverse=True)

            total_long_val = sum(p["long_value"] for p in combined_net)
            total_short_val = sum(p["short_value"] for p in combined_net)
            net_delta = total_long_val - total_short_val

            result = {
                "hlp_summary": {
                    "total_account_value": total_value,
                    "total_positions": total_positions,
                    "total_pnl": total_pnl,
                    "strategy_count": len(self.HLP_ADDRESSES),
                    "net_exposure_delta": net_delta,
                },
                "combined_net_positions": combined_net,
                "exposure": {
                    "net_delta": net_delta,
                    "total_long": total_long_val,
                    "total_short": total_short_val,
                },
            }
            if include_strategies:
                result["strategies"] = strategies
            return result
        return self._cached(cache_key, 60, _fetch)

    def get_hlp_trades(self, limit: int = 100):
        """
        Get historical HLP trade fills across confirmed strategy addresses. TTL: 60s.

        Returns:
            dict with 'trades' list, 'total', 'strategies'
        """
        cache_key = f"hlp_trades_{limit}"

        def _fetch():
            all_trades = []
            valid = [(n, a) for n, a in self.HLP_ADDRESSES.items() if a != self._SENTINEL]
            for name, addr in valid:
                try:
                    fills = self._hl_post({"type": "userFills", "user": addr})
                    for f in (fills or []):
                        px = float(f.get("px", 0))
                        sz = float(f.get("sz", 0))
                        all_trades.append({
                            "timestamp": f.get("time"),
                            "strategy_name": name,
                            "coin": f.get("coin", ""),
                            "side": f.get("side", ""),
                            "size": sz,
                            "price": px,
                            "usd_value": px * sz,
                            "fee": float(f.get("fee", 0)),
                            "closedPnl": float(f.get("closedPnl", 0)),
                        })
                except Exception:
                    continue

            all_trades.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            total = len(all_trades)
            if limit > 0:
                all_trades = all_trades[:limit]
            strategies = list({t["strategy_name"] for t in all_trades})
            return {"trades": all_trades, "total": total, "strategies": strategies}
        return self._cached(cache_key, 60, _fetch)

    def get_hlp_trade_stats(self):
        """
        Aggregate HLP trade volume and fee statistics from available fills. TTL: 60s.

        Returns:
            dict with 'stats' key containing total_trades, data_range, by_strategy
        """
        def _fetch():
            data = self.get_hlp_trades(limit=2000)
            trades = data.get("trades", [])
            total = data.get("total", 0)
            by_strategy = {}
            first_ts = last_ts = None
            for t in trades:
                name = t["strategy_name"]
                if name not in by_strategy:
                    by_strategy[name] = {"strategy_name": name, "volume": 0.0, "fees": 0.0, "count": 0}
                by_strategy[name]["volume"] += t["usd_value"]
                by_strategy[name]["fees"] += t["fee"]
                by_strategy[name]["count"] += 1
                ts = t.get("timestamp")
                if ts:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
            return {
                "stats": {
                    "total_trades": total,
                    "data_range": {"first": first_ts, "last": last_ts},
                    "by_strategy": list(by_strategy.values()),
                }
            }
        return self._cached("hlp_trade_stats", 60, _fetch)

    def get_hlp_liquidators(self):
        """
        Check HLP Liquidator address activity (position presence = active). TTL: 60s.
        NOTE: No event log available via public REST — events list will always be empty.
        Liquidator addresses (1-4) are currently unverified in HLP_ADDRESSES.
        """
        def _fetch():
            liquidators = []
            for name, addr in self.HLP_ADDRESSES.items():
                if "Liquidator" not in name:
                    continue
                if addr == self._SENTINEL:
                    liquidators.append({
                        "name": name, "status": "unknown",
                        "address": addr, "note": "Address unverified",
                    })
                    continue
                try:
                    data = self._hl_post({"type": "clearinghouseState", "user": addr})
                    has_positions = any(
                        float(ap.get("position", {}).get("szi", 0)) != 0
                        for ap in data.get("assetPositions", [])
                    )
                    liquidators.append({
                        "name": name,
                        "status": "active" if has_positions else "idle",
                        "address": addr,
                    })
                except Exception:
                    liquidators.append({"name": name, "status": "unknown", "address": addr})
            return {"liquidators": liquidators, "events": []}
        return self._cached("hlp_liquidators", 60, _fetch)

    def get_hlp_deltas(self, hours: int = 24):
        """
        Get HLP net exposure delta — single current snapshot only. TTL: 60s.
        NOTE: Historical delta time series is unavailable via public REST.
              change_24h will always be null (requires persistent monitoring).
        """
        def _fetch():
            pos_data = self.get_hlp_positions(include_strategies=False)
            summary = pos_data.get("hlp_summary", {})
            net_delta = summary.get("net_exposure_delta", 0)
            return {
                "current": net_delta,
                "change_24h": None,
                "deltas": [{"net_delta": net_delta, "timestamp": int(time.time() * 1000)}],
                "note": "Single snapshot — historical delta time series unavailable via public REST.",
            }
        return self._cached("hlp_deltas", 60, _fetch)

    def get_hlp_position_history(self, hours: int = 24):
        """
        Returns single current position snapshot. TTL: not cached (fresh each call).
        NOTE: Historical position snapshots unavailable via public REST.
        """
        pos_data = self.get_hlp_positions(include_strategies=True)
        return {
            "snapshots": [{
                "timestamp": int(time.time() * 1000),
                "positions": pos_data.get("combined_net_positions", []),
            }],
            "interval": "REST snapshot — no history available",
        }

    # ==================== BINANCE LIQUIDATIONS ====================

    def get_binance_liquidations(self, timeframe: str = "1h"):
        """
        STUB: Binance /fapi/v1/forceOrders now requires API key authentication.
        The endpoint was previously public but Binance revoked unauthenticated access.
        """
        raise NotImplementedError(
            "Binance /fapi/v1/forceOrders now requires API key authentication. "
            "The public liquidation endpoint is no longer available without credentials."
        )

    def get_binance_liquidation_stats(self):
        """
        STUB: Binance liquidation data requires API key authentication.
        See get_binance_liquidations() for details.
        """
        raise NotImplementedError(
            "Binance /fapi/v1/forceOrders now requires API key authentication. "
            "The public liquidation endpoint is no longer available without credentials."
        )

    # ==================== STUBS ====================

    def get_whales(self):
        """STUB: Requires continuous monitoring of all trade activity."""
        raise NotImplementedError(
            "Requires continuous monitoring of all trade activity — not available via public REST. "
            "Use get_large_trades() as an approximation."
        )

    def get_whale_addresses(self):
        """STUB: Requires a curated whale address database."""
        raise NotImplementedError(
            "Requires a curated whale address database. Not replicable from free APIs. "
            "HLP_ADDRESSES constant provides a minimal seed list."
        )

    def get_events(self):
        """STUB: Requires a full blockchain event indexer."""
        raise NotImplementedError(
            "Requires a full blockchain event indexer. Not available via public APIs."
        )

    def get_contracts(self):
        """STUB: Requires a proprietary contract registry."""
        raise NotImplementedError(
            "Requires a proprietary contract registry. Not available via public APIs."
        )

    def get_smart_money_rankings(self):
        """STUB: Requires sustained PnL tracking across thousands of wallets."""
        raise NotImplementedError(
            "Requires sustained PnL tracking across thousands of wallets. "
            "Not replicable from snapshots."
        )

    def get_smart_money_leaderboard(self):
        """
        Attempts Hyperliquid leaderboard endpoint; raises NotImplementedError if unavailable.
        Returns partial substitute if HL public leaderboard endpoint is accessible.
        """
        try:
            return self._hl_post({"type": "leaderboard"})
        except Exception:
            raise NotImplementedError(
                "Requires sustained PnL tracking across thousands of wallets. "
                "HL public leaderboard endpoint not available. Not replicable from snapshots."
            )

    def get_smart_money_signals(self, timeframe: str = "1h"):
        """STUB: Requires a sustained signal pipeline."""
        raise NotImplementedError(
            f"Requires a sustained signal pipeline for timeframe '{timeframe}'. "
            "Not replicable from public REST snapshots."
        )


# backwards-compatibility alias
MoonDevAPI = HyperliquidPublicAPI


# ==================== TEST SUITE ====================
def test_all():
    """HyperliquidPublicAPI Test Suite — no API key required."""

    print("=" * 60)
    print("HyperliquidPublicAPI Test Suite")
    print("No API key required.")
    print("=" * 60)

    api = HyperliquidPublicAPI()
    print()

    # 1. HEALTH
    print("=" * 60)
    print("1. HEALTH CHECK")
    print("=" * 60)
    try:
        h = api.health()
        print(f"✅ Health: {h}")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 2. LIQUIDATIONS
    print("=" * 60)
    print("2. LIQUIDATION DATA")
    print("=" * 60)
    for tf in ["1h", "4h", "24h"]:
        try:
            data = api.get_liquidations(tf)
            stats = data.get("stats", {})
            count = stats.get("total_count", 0)
            value = stats.get("total_value_usd", 0)
            print(f"✅ {tf}: {count} liqs | ${value:,.0f}")
        except Exception as e:
            print(f"❌ {tf}: {e}")
    print()

    # 3. POSITIONS
    print("=" * 60)
    print("3. LARGE POSITIONS ($200k+)")
    print("=" * 60)
    try:
        positions = api.get_positions()
        print(f"✅ {positions.get('total_positions', 0)} large positions")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 4. STUBS
    print("=" * 60)
    print("4. STUB METHODS")
    print("=" * 60)
    for stub_name, stub_fn in [
        ("get_whales", api.get_whales),
        ("get_whale_addresses", api.get_whale_addresses),
        ("get_events", api.get_events),
        ("get_contracts", api.get_contracts),
        ("get_smart_money_rankings", api.get_smart_money_rankings),
    ]:
        try:
            stub_fn()
            print(f"⚠️  {stub_name}: did NOT raise (unexpected)")
        except NotImplementedError as e:
            print(f"✅ {stub_name}: stub OK — {str(e)[:60]}")
        except Exception as e:
            print(f"❌ {stub_name}: {e}")
    print()

    # 5. TICK DATA
    print("=" * 60)
    print("5. TICK DATA")
    print("=" * 60)
    try:
        stats = api.get_tick_stats()
        print(f"✅ Tick stats: {stats.get('symbols')}")
    except Exception as e:
        print(f"❌ Tick stats: {e}")
    try:
        latest = api.get_tick_latest()
        prices = latest.get("prices", {})
        for sym, px in prices.items():
            print(f"   {sym}: ${px:,.2f}")
    except Exception as e:
        print(f"❌ Latest prices: {e}")
    for sym in ["BTC", "ETH"]:
        try:
            ticks = api.get_ticks(sym, "1h")
            print(f"✅ {sym} 1h: {ticks.get('count', 0)} candles")
        except Exception as e:
            print(f"❌ {sym}: {e}")
    print()

    # 6. ORDER FLOW
    print("=" * 60)
    print("6. ORDER FLOW")
    print("=" * 60)
    try:
        stats = api.get_orderflow_stats()
        print(f"✅ Order flow stats: vol=${stats.get('total_volume_usd', 0):,.0f}")
    except Exception as e:
        print(f"❌ {e}")
    try:
        trades = api.get_trades()
        print(f"✅ Recent trades: {len(trades.get('trades', []))} trades")
    except Exception as e:
        print(f"❌ {e}")
    try:
        large = api.get_large_trades()
        print(f"✅ Large trades (>$100k): {len(large.get('trades', []))} trades")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 7. USER POSITIONS
    print("=" * 60)
    print("7. USER POSITIONS")
    print("=" * 60)
    test_address = "0x010461c14e146ac35fe42271bdc1134ee31c703a"
    try:
        data = api.get_user_positions(test_address)
        positions = data.get("assetPositions", [])
        print(f"✅ {len(positions)} positions for {test_address[:10]}...")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 8. USER FILLS
    print("=" * 60)
    print("8. USER FILLS")
    print("=" * 60)
    try:
        data = api.get_user_fills(test_address, limit=10)
        print(f"✅ {data.get('total', 0)} total fills; showing {len(data.get('fills', []))}")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 9. HLP
    print("=" * 60)
    print("9. HLP POSITIONS")
    print("=" * 60)
    try:
        hlp = api.get_hlp_positions(include_strategies=False)
        summary = hlp.get("hlp_summary", {})
        print(f"✅ HLP: ${summary.get('total_account_value', 0):,.0f} | "
              f"{summary.get('total_positions', 0)} positions")
    except Exception as e:
        print(f"❌ {e}")
    print()

    # 10. BINANCE LIQUIDATIONS
    print("=" * 60)
    print("10. BINANCE LIQUIDATIONS")
    print("=" * 60)
    try:
        stats = api.get_binance_liquidation_stats()
        print(f"✅ Binance: {stats.get('total_count', 0)} liqs | "
              f"${stats.get('total_volume', 0):,.0f}")
    except Exception as e:
        print(f"❌ {e}")
    print()

    print("=" * 60)
    print("Test complete. GG.")
    print("=" * 60)


if __name__ == "__main__":
    test_all()
