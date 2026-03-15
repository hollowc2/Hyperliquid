# Hyperliquid Liquidation Tracker

> **Status: Pre-implementation — research & planning only. No code has been written yet.**

## Project Goal
Implement a fully asynchronous Python service that captures liquidation events in real-time from Hyperliquid, normalizes them into a canonical format, stores raw events in JSONL format, maintains rolling aggregates across multiple time windows (1m, 5m, 15m, 1h), and exposes data via an internal interface.

**This repo currently contains only:**
- Research findings on available data sources and constraints
- Architecture decision options (pending selection)
- A full software spec ready to implement once an approach is chosen

**Reference Documents:**
- `docs/hyperliquid_liquidation_outline.md` - Complete software specification
- `docs/sdk_summary.md` - Comprehensive SDK documentation
- `docs/AI_CONTEXT.md` - Development guidelines

---

## Key Research Findings

### 1. Hyperliquid Data Sources

#### Official WebSocket API
- **Endpoint:** `wss://api.hyperliquid.xyz/ws`
- **Available Subscription Types:** 19 types including `trades`, `userFills`, `userEvents`, `userNonFundingLedgerUpdates`
- **Problem:** No public liquidation feed. All liquidation data requires a user address.
- **Rate Limits:**
  - 100 WebSocket connections per IP
  - 1000 subscriptions per IP
  - **10 unique user subscriptions per IP** ⚠️ CRITICAL CONSTRAINT
  - 1200 API request weight per minute
  - 2000 messages/minute to Hyperliquid

#### Hyperliquid Node (Self-Hosted)
- **Data Access:** Full, uncapped real-time fills via `--write-fills` flag
- **Infrastructure:** 16 vCPUs, 64 GB RAM, 500 GB SSD (non-validator)
- **Storage:** ~100 GB logs/day (can archive/delete old)
- **Output:** Fills written to `~/hl/data/node_fills/hourly/{date}/{hour}`
- **Additional:** `liquidatable` info endpoint exposes accounts about to be liquidated
- **Setup:** Ubuntu 24.04 required, ~30 min setup

#### Foundation Non-Validating Node (Free but Restricted)
- **Requirements:**
  - Stake 10,000 HYPE tokens
  - Tier 1+ Maker Rebate status (>0.5% of 14-day weighted maker volume)
  - 98% uptime as reliable peer
  - [Application form](https://docs.google.com/forms/d/e/1FAIpQLSeZrUJuJ5_osJuy-YnHCycvb3yTmulhIo6_jPgGPzZVWIxP8g/viewform)
- **Data:** Same as self-hosted node

#### Paid Services
- **Dwellir HypeRPC gRPC:** `StreamFills` - all platform fills with liquidation indicators
- **CoinGlass API:** Wallet position data (Standard plan+, no free tier)
- **Chainstack, others:** RPC node access ($$)

#### S3 Historical Data (Free but Delayed)
- **Buckets:** `hyperliquid-archive`, `hl-mainnet-node-data`
- **Contents:** `node_fills_by_block`, `node_fills`, L2 snapshots
- **Format:** LZ4 compressed
- **Cost:** You pay S3 transfer costs
- **Issue:** Not real-time, may have gaps

---

### 2. Liquidation Event Detection

#### From Official WebSocket
**Indirect detection via `userFills` (requires user address):**
- `dir` field: "Close Long" or "Close Short" indicates position closure
- `closedPnl` field: Negative values suggest forced liquidation
- Combined with position context to confirm

**Better approach: `userNonFundingLedgerUpdates`**
- Explicit ledger events including liquidations
- Still requires user address subscription

#### From Node `--write-fills`
**Direct detection:**
- `liquidatedUser` field present in fill data
- `dir` is "Close Long" or "Close Short"
- User address matches `liquidatedUser` (victim, not liquidator)
- Fill data includes: coin, price (px), size (sz), side, time, startPosition, closedPnl, fee

---

### 3. Constraints & Limitations

| Constraint | Impact | Solution |
|-----------|--------|----------|
| **No public liquidation feed** | Can't subscribe to all liquidations | Use node, polling, or heuristics |
| **10-user WebSocket limit** | Can't track 1000s of addresses via WS | Use polling or node |
| **Wallet list not exposed** | No API endpoint for high-value addresses | Use third-party leaderboard data (CoinGlass, Hyperdash) or scrape |
| **Rate limits (1200 weight/min)** | Limited API calls | Use WebSocket where possible |
| **No liquidation webhook/stream** | Must actively monitor | Continuous polling or node |

---

### 4. Viable Architectures

### **Architecture A: Self-Hosted Node (BEST) ✅**
```
Node (--write-fills) → File stream → Parser → Storage + Aggregates
```
- ✅ Real-time, no rate limits, all liquidations captured
- ✅ Direct access to liquidatedUser field
- ✅ `liquidatable` endpoint for at-risk positions
- ❌ Infrastructure cost ($50-200/month for 16 vCPU box)
- ❌ 100 GB/day storage

**Timeline:** Setup ~30 min, implementation straightforward

### **Architecture B: Hybrid Polling + WebSocket (FEASIBLE)**
```
Address Poller (clearinghouseState) → Position Tracker
        ↓
Trades WebSocket (public) → Trade Matcher
        ↓
Liquidation Detector (cross-reference) → Storage + Aggregates
```
- ✅ Free (within rate limits)
- ✅ Can track ~3000 whale addresses periodically
- ⚠️ Position detection (not real-time), may miss small liquidations
- ⚠️ Heuristics-based, less reliable

**Requirements:**
- Wallet list source (CoinGlass paid, or scrape leaderboard)
- Poll frequency: 5-30 sec per address
- Trade matching logic to confirm liquidations

**Timeline:** Implementation ~2-3 days, many edge cases

### **Architecture C: Top-10 WebSocket + Polling (HYBRID)**
```
Top-10 whales (WebSocket userFills) + Remaining (Polling)
        ↓
Rotate subscriptions over time
        ↓
Storage + Aggregates
```
- ✅ Real-time for top traders
- ⚠️ Lag for others due to rotation
- ⚠️ Complex subscription management

**Timeline:** Implementation ~2 days

### **Architecture D: Paid gRPC (SIMPLE)**
```
Dwellir StreamFills → Direct liquidation detection
```
- ✅ Simplest implementation
- ✅ All liquidations, real-time
- ❌ Paid service (~$50-500/month)

---

### 5. Wallet Discovery

**Challenge:** No official API endpoint for trader rankings/wallet addresses

**Options:**
1. **CoinGlass API** (Paid, Standard+ plan)
   - Returns wallet addresses filtered by position size
   - Real-time updates
   - Cost: ~$50-100/month for Standard plan

2. **Scrape Hyperliquid Leaderboard** (Free but unsupported)
   - `app.hyperliquid.xyz/leaderboard` - UI data only
   - Can reverse-engineer API if it exists

3. **Third-party analytics** (Hyperdash, Hyperdash.info)
   - Data visualization but not raw API access

4. **Manual list** (Free but limited)
   - Maintain hardcoded list of whale addresses
   - Update periodically from leaderboard UI

---

### 6. Implementation Complexity Comparison

| Aspect | Node | Hybrid Poll+WS | Top-10 Rotate | Paid gRPC |
|--------|------|----------------|---------------|-----------|
| **Setup** | 30 min | 5 min | 5 min | 5 min |
| **Infrastructure** | $$$$ | Free | Free | Paid |
| **Implementation** | 1-2 days | 2-3 days | 2 days | 1 day |
| **Reliability** | 99%+ | 70-80% | 85-90% | 99%+ |
| **Real-time** | ✅ | ⚠️ | ✅/⚠️ | ✅ |
| **Scale** | Unlimited | ~3000 wallets | ~100 wallets | Unlimited |

---

## What We Stopped At

We have **three viable free approaches** ready to implement, pending your decision:

### **DECISION NEEDED:**

1. **Can you provision a node?** (16 vCPU, 64GB RAM, $50-200/month)
   - **IF YES:** Use Architecture A (Node) - simplest, most reliable
   - **IF NO:** Choose B or C below

2. **If no node, which is your priority?**
   - **Coverage:** Architecture B (Hybrid Polling) - tracks ~3000 whales
   - **Simplicity:** Keep using official WebSocket only + basic heuristics
   - **Top traders:** Architecture C (Rotate subscriptions) - real-time top 10

### **Remaining Unknowns:**

1. **Wallet discovery:** Where to get list of high-value addresses?
   - Pay for CoinGlass API?
   - Scrape leaderboard?
   - Manual list?

2. **Detection threshold:** What USD liquidation amount should we track?
   - All liquidations?
   - Only >$10K?
   - Only >$100K?

3. **Storage capacity:** How long to keep raw JSONL?
   - Rolling 7 days?
   - Rolling 30 days?
   - Archive to cold storage?

---

## Next Steps

1. **Decide on architecture** (Node vs Polling vs Hybrid)
2. **Decide on wallet source** (CoinGlass vs scrape vs manual)
3. **Decide on liquidation threshold**
4. **Implement project structure** (create config.py, models.py, etc.)
5. **Implement data ingestion** (based on chosen architecture)
6. **Implement storage & aggregation**
7. **Test & deploy**

---

## Resources

- **SDK Documentation:** `docs/sdk_summary.md`
- **Complete Spec:** `docs/hyperliquid_liquidation_outline.md`
- **Development Guidelines:** `docs/AI_CONTEXT.md`
- **Official Docs:** https://hyperliquid.gitbook.io/hyperliquid-docs/
- **Foundation Node Application:** https://docs.google.com/forms/d/e/1FAIpQLSeZrUJuJ5_osJuy-YnHCycvb3yTmulhIo6_jPgGPzZVWIxP8g/viewform
- **Chainstack Hyperliquid:** https://chainstack.com/build-better-with-hyperliquid/
- **Dwellir gRPC:** https://www.dwellir.com/docs/hyperliquid/trade-data

---

## Summary

We've thoroughly researched Hyperliquid's data infrastructure and identified that **there's no free, public liquidation feed** from the official API. Your options are:

1. **Run your own node** (best but infrastructure $$)
2. **Hybrid polling approach** (free but less reliable)
3. **Use paid services** (simple but $$)

The project is ready to implement once you choose an approach and answer the three decision questions above.
