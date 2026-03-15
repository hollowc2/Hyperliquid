# HyperLiquid SDK Summary

## Core Concepts

### Architecture Overview
The HyperLiquid SDK is built on a **modular, layered architecture** with clear separation of concerns:

- **API Layer** (`api.py`): Low-level HTTP communication with the blockchain
- **Info Layer** (`info.py`): Read-only data retrieval and market data subscriptions
- **Exchange Layer** (`exchange.py`): State-changing operations (trading, transfers, withdrawals)
- **WebSocket Layer** (`websocket_manager.py`): Real-time data streaming via subscriptions
- **Utilities** (`utils/`): Signing, type definitions, and constants

### Key Design Patterns

1. **Inheritance Hierarchy**:
   - `API` → base class with HTTP session management
   - `Info(API)` → extends with market data queries and WebSocket subscriptions
   - `Exchange(API)` → extends with transaction signing and state-changing operations

2. **EIP-712 Signing**: All state-changing operations use EIP-712 typed data signing for secure authentication
   - User-signed actions (transfers, delegations)
   - L1 actions (orders, cancellations, leverage updates)
   - Multi-sig support for complex authorization flows

3. **Asset Naming**: Uses a multi-tier naming system:
   - **Perps**: Named like "ETH", "BTC" (original dex: ""), or "NAME:dex" (builder-deployed)
   - **Spot**: Named like "PURR/USDC" or "@index" (e.g., "@8")
   - Internal mapping: `name_to_coin` and `coin_to_asset` for translation

4. **WebSocket-First Design**:
   - Optional skip-ws initialization for read-only clients
   - Threaded WebSocket manager for background subscriptions
   - Multiple callbacks per subscription supported

---

## Public APIs

### Info Class (Read-Only Operations)

**Initialization**:
```python
Info(base_url: Optional[str] = None, skip_ws: Optional[bool] = False,
     meta: Optional[Meta] = None, spot_meta: Optional[SpotMeta] = None,
     perp_dexs: Optional[List[str]] = None, timeout: Optional[float] = None)
```

#### Market Data Methods
- `user_state(address, dex)` - Get user positions, margin, and collateral
- `spot_user_state(address)` - Get spot balances
- `all_mids(dex)` - Get current mid prices for all coins
- `open_orders(address, dex)` - Get active orders
- `frontend_open_orders(address, dex)` - Get orders with UI metadata
- `user_fills(address)` - Get fill history
- `user_fills_by_time(address, start_time, end_time, aggregate_by_time)` - Time-filtered fills
- `l2_snapshot(name)` - Order book snapshot
- `candles_snapshot(name, interval, startTime, endTime)` - OHLCV candle data
- `meta(dex)` - Perp metadata (coins, decimals)
- `spot_meta()` - Spot asset and token metadata
- `meta_and_asset_ctxs()` - Combined metadata with prices
- `perp_dexs()` - List of builder-deployed perp dexs

#### User History & Analysis
- `user_fees(address)` - Fee schedule and volume
- `user_funding_history(user, startTime, endTime)` - Funding payments received
- `funding_history(name, startTime, endTime)` - Market funding rates
- `user_staking_summary(address)` - Staking details
- `user_staking_delegations(address)` - Delegated tokens
- `user_staking_rewards(address)` - Staking rewards history
- `delegator_history(user)` - Full staking event history
- `historical_orders(user)` - Last 2000 orders
- `user_non_funding_ledger_updates(user, startTime, endTime)` - Account activity
- `portfolio(user)` - PnL and performance metrics
- `user_twap_slice_fills(user)` - TWAP execution fills
- `user_vault_equities(user)` - Vault positions

#### Order Status
- `query_order_by_oid(user, oid)` - Get order by OID
- `query_order_by_cloid(user, cloid: Cloid)` - Get order by client-provided ID
- `query_referral_state(user)` - Referral information
- `query_sub_accounts(user)` - Sub-account list
- `query_user_to_multi_sig_signers(multi_sig_user)` - Multi-sig signers
- `query_user_dex_abstraction_state(user)` - DEX abstraction status
- `query_perp_deploy_auction_status()` - Perp deployment auction
- `query_spot_deploy_auction_status(user)` - Spot deployment status
- `extra_agents(user)` - Persistent agents for trading
- `user_role(user)` - Account type and permissions
- `user_rate_limit(user)` - API rate limit status

#### WebSocket Subscriptions
- `subscribe(subscription: Subscription, callback: Callable)` → subscription_id
- `unsubscribe(subscription: Subscription, subscription_id: int)` → bool

**Available Subscription Types**:
- `{"type": "allMids"}` - All coin prices
- `{"type": "l2Book", "coin": str}` - L2 order book
- `{"type": "bbo", "coin": str}` - Best bid/offer
- `{"type": "trades", "coin": str}` - Trade stream
- `{"type": "candle", "coin": str, "interval": str}` - Candlesticks
- `{"type": "userEvents", "user": str}` - Fill events
- `{"type": "userFills", "user": str}` - All fills snapshot+stream
- `{"type": "orderUpdates", "user": str}` - Order status updates
- `{"type": "userFundings", "user": str}` - Funding payments
- `{"type": "userNonFundingLedgerUpdates", "user": str}` - Account updates
- `{"type": "webData2", "user": str}` - Aggregate UI data
- `{"type": "activeAssetCtx", "coin": str}` - Active asset context
- `{"type": "activeAssetData", "user": str, "coin": str}` - User's active position data

#### Utility Methods
- `name_to_asset(name: str)` → int - Translate name to asset ID
- `disconnect_websocket()` - Clean shutdown

### Exchange Class (State-Changing Operations)

**Initialization**:
```python
Exchange(wallet: LocalAccount, base_url: Optional[str] = None,
         meta: Optional[Meta] = None, vault_address: Optional[str] = None,
         account_address: Optional[str] = None, spot_meta: Optional[SpotMeta] = None,
         perp_dexs: Optional[List[str]] = None, timeout: Optional[float] = None)
```

#### Order Management
- `order(name, is_buy, sz, limit_px, order_type, reduce_only, cloid, builder)` → response
- `bulk_orders(order_requests, builder, grouping)` → response
- `modify_order(oid|cloid, name, is_buy, sz, limit_px, order_type, reduce_only, cloid)` → response
- `bulk_modify_orders_new(modify_requests)` → response
- `market_open(name, is_buy, sz, px, slippage, cloid, builder)` → response (IoC order)
- `market_close(coin, sz, px, slippage, cloid, builder)` → response (closes entire position)
- `cancel(name, oid)` → response
- `cancel_by_cloid(name, cloid)` → response
- `bulk_cancel(cancel_requests)` → response
- `bulk_cancel_by_cloid(cancel_requests)` → response
- `schedule_cancel(time: Optional[int])` - Cancel all orders at specific time

#### Leverage & Margin
- `update_leverage(leverage, name, is_cross)` - Set leverage
- `update_isolated_margin(amount, name)` - Add margin to isolated position
- `set_expires_after(expires_after: Optional[int])` - Set action expiry window

#### Account Management
- `create_sub_account(name)` - Create sub-trading account
- `set_referrer(code)` - Set referral code
- `approve_agent(name: Optional[str])` → (response, agent_key_hex)

#### Asset Transfers
- `usd_transfer(amount, destination)` - Transfer USDC
- `spot_transfer(amount, destination, token)` - Transfer spot tokens
- `usd_class_transfer(amount, to_perp)` - Move USD between spot/perp
- `send_asset(destination, source_dex, destination_dex, token, amount)` - Cross-dex asset transfer
- `sub_account_transfer(sub_account_user, is_deposit, usd)` - Transfer to/from sub-account
- `sub_account_spot_transfer(sub_account_user, is_deposit, token, amount)` - Spot to sub-account

#### Vault Operations
- `vault_usd_transfer(vault_address, is_deposit, usd)` - Vault funding

#### Withdrawals
- `withdraw_from_bridge(amount, destination)` - Withdraw to chain

#### Multi-Sig & Authorization
- `convert_to_multi_sig_user(authorized_users, threshold)` - Enable multi-sig
- `approve_builder_fee(builder, max_fee_rate)` - Approve builder fees
- `token_delegate(validator, wei, is_undelegate)` - Delegate tokens

#### Spot Deployment
- `spot_deploy_register_token(name, sz_decimals, wei_decimals, max_gas, full_name)`
- `spot_deploy_user_genesis(token, user_and_wei, existing_token_and_wei)`
- `spot_deploy_enable_freeze_privilege(token)`
- `spot_deploy_freeze_user(token, user, freeze)`
- `spot_deploy_revoke_freeze_privilege(token)`
- `spot_deploy_enable_quote_token(token)`
- `spot_deploy_genesis(token, max_supply, no_hyperliquidity)`
- `spot_deploy_register_spot(base_token, quote_token)`
- `spot_deploy_register_hyperliquidity(spot, start_px, order_sz, n_orders, n_seeded_levels)`
- `spot_deploy_set_deployer_trading_fee_share(token, share)`

#### Perp Deployment
- `perp_deploy_register_asset(dex, max_gas, coin, sz_decimals, oracle_px, margin_table_id, only_isolated, schema)`
- `perp_deploy_set_oracle(dex, oracle_pxs, all_mark_pxs, external_perp_pxs)`

#### Properties
- `wallet: LocalAccount` - Signing wallet
- `vault_address: Optional[str]` - Vault/sub-account override
- `account_address: Optional[str]` - Account override (for agents)
- `info: Info` - Embedded Info instance
- `base_url: str` - API endpoint
- `expires_after: Optional[int]` - Action expiry in ms

### API Class (Low-Level)

**Methods**:
- `post(url_path, payload)` → dict - HTTP POST request
- `_handle_exception(response)` - Raise appropriate errors

**Properties**:
- `base_url: str` - API endpoint
- `session: requests.Session` - HTTP session
- `timeout: Optional[float]` - Request timeout

---

## Object Lifecycle

### Info Lifecycle
```
Info() initialization:
  1. Create HTTP session
  2. If not skip_ws: Start WebsocketManager thread
  3. Load spot metadata (spot_meta())
  4. Build spot coin→asset mappings (indices 10000+)
  5. Load perp metadata for each dex (meta())
  6. Build perp coin→asset mappings (indices 0-9999, or 110000+ for builders)

Usage:
  - subscribe() queues or sends subscription to WebSocket
  - All query methods POST to /info endpoint

Shutdown:
  - disconnect_websocket() stops WebSocket thread
```

### Exchange Lifecycle
```
Exchange() initialization:
  1. Store wallet (eth_account.Account for signing)
  2. Create embedded Info instance (skip_ws=True always)
  3. Set optional vault_address, account_address overrides

Usage:
  - order(), cancel(), etc. methods:
    a. Build action dict
    b. Get current timestamp (ms since epoch)
    c. Sign action via EIP-712
    d. POST to /exchange with action+signature+nonce
    e. Return response (typically {"status": "ok"|"err", "response": ...})
```

### WebsocketManager Lifecycle
```
WebsocketManager() initialization:
  1. Create WebSocketApp with handlers
  2. Create ping sender thread
  3. Create stop_event flag

run() lifecycle:
  1. Start ping sender thread
  2. run_forever() connects WebSocket and handles messages
  3. Ping sender sends {"method": "ping"} every 50ms
  4. on_message() routes incoming data to subscriptions

Subscription lifecycle:
  1. subscribe() called with subscription dict + callback
  2. If ws_ready: send immediately, else queue
  3. on_open() sends queued subscriptions after connection
  4. incoming messages matched to subscription via identifier
  5. all callbacks for that subscription called with message

Unsubscription:
  1. unsubscribe() removes callback from active_subscriptions
  2. If no more callbacks for identifier: send unsubscribe to server

Shutdown:
  1. stop() sets stop_event
  2. WebSocket closes
  3. Ping sender joins
```

### Cloid (Client Order ID) Lifecycle
```
Cloid creation:
  - Cloid("0x...") - 16-byte hex string
  - Cloid.from_int(int) - Convert int to 16-byte hex
  - Cloid.from_str(str) - Validate hex string

Usage:
  - Include in order request: order["cloid"] = cloid
  - Query: info.query_order_by_cloid(user, cloid)
  - Cancel: exchange.cancel_by_cloid(coin, cloid)
```

---

## Error Handling Patterns

### Exception Hierarchy
```
Exception
  └─ Error (hyperliquid.utils.error)
      ├─ ClientError(4xx HTTP)
      │   Properties: status_code, error_code, error_message, header, error_data
      │
      └─ ServerError(5xx HTTP)
          Properties: status_code, message
```

### Error Scenarios

**ClientError (4xx)**:
- Malformed requests
- Invalid signatures
- Order validation failures
- Insufficient funds
- Leverage violations

**ServerError (5xx)**:
- Exchange maintenance
- Temporary outages

**RuntimeError**:
- `disconnect_websocket()` called when `skip_ws=True`
- `subscribe()`/`unsubscribe()` called when `skip_ws=True`

**NotImplementedError**:
- Multiple `userEvents` subscriptions
- Multiple `orderUpdates` subscriptions
- `unsubscribe()` before WebSocket connected

**ValueError** (in signing):
- Float precision issues during sign/wire conversion
- Invalid Cloid format

### Usage Pattern
```python
try:
    order = exchange.order("ETH", True, 1, 1000, {"limit": {"tif": "Gtc"}})
    if order["status"] == "ok":
        # Success
        pass
    else:
        # API error in response
        print(order["response"]["error"])
except ClientError as e:
    print(f"Client error {e.status_code}: {e.error_message}")
except ServerError as e:
    print(f"Server error {e.status_code}: {e.message}")
```

---

## Known Limitations

### Documented Constraints

1. **WebSocket Multiplexing**:
   - Cannot subscribe to `userEvents` multiple times
   - Cannot subscribe to `orderUpdates` multiple times
   - Comment suggests ideally messages would include user for multiplexing

2. **Agent Restrictions**:
   - Agents cannot perform internal transfers (only trading)
   - Must verify agent is created from main wallet, not another agent
   - Agents are the wallet signing the transaction, different from account_address

3. **Order Type Constraints**:
   - `reduce_only` orders require existing position
   - `market_close()` fails if no position exists in specified coin
   - Trigger orders have different precision requirements

4. **Perp vs Spot**:
   - Perps use internal dex naming ("", or "dex:name" format)
   - Spot uses pair naming ("BASE/QUOTE") or "@index"
   - Asset ID ranges: perps 0-9999, spot 10000+, builder perps 110000+

5. **Float Precision**:
   - `float_to_wire()` raises error if rounding would change value by ≥1e-12
   - Limits decimal places in orders/transfers
   - Max 8 decimals for perp wire format, varies for spot

6. **API Rate Limits**:
   - User can check via `user_rate_limit(user)`
   - No built-in retry logic in SDK
   - Single timeout parameter for all requests

7. **Metadata Caching**:
   - Metadata loaded once at Info initialization
   - Changes to available assets not reflected until Info recreated
   - Builder-deployed dexs must be explicitly listed in `perp_dexs` parameter

8. **Vault Address Behavior**:
   - When set, operations execute on behalf of vault
   - `usd_transfer` and `spot_transfer` don't support vault_address (None required)
   - Sub-account operations use different parameter names

9. **Action Expiry**:
   - `expires_after` (in ms) not supported on user-signed actions
   - Must be None for: `usd_transfer`, `spot_transfer`, `token_delegate`, etc.
   - Only works with L1-signed actions (orders, cancels, leverage)

10. **Multi-Sig Limitations**:
    - Requires explicit setup: `convert_to_multi_sig_user()`
    - Signers must be sorted addresses
    - Multi-sig users can't create agents themselves

11. **Spot Deployment**:
    - Requires governance/builder permissions
    - Complex multi-step process (genesis → register → hyperliquidity)
    - Token freeze/thaw privileges must be explicitly enabled

12. **WebSocket Connection**:
    - Background thread, no guarantee of connection timing
    - First message may not arrive immediately after subscribe()
    - No automatic reconnection on network failure (relies on websocket-client library)

13. **Order History**:
    - `historical_orders()` returns max 2000 most recent
    - `user_twap_slice_fills()` returns max 2000 most recent
    - Must use paginated queries or rely on timestamps

14. **Multi-Dex Operations**:
    - Cross-dex transfers via `send_asset()` (source_dex, destination_dex)
    - Default dex = "" (original), spot = "spot"
    - Builder dexs referenced by name

### Inferred Limitations

1. **No Built-in Order Validation**: SDK doesn't validate orders before sending; relies on server rejection
2. **No State Caching**: Every Info call hits the API; no local cache
3. **No Batch Subscriptions**: Must call subscribe() for each subscription
4. **Limited Error Detail**: ClientError.error_data optional; not all errors include structured data
5. **Single Account per Instance**: Exchange instance tied to single wallet
6. **Synchronous Only**: No async/await support; blocking HTTP calls
7. **No Retry Logic**: Failed requests don't auto-retry
8. **String Amounts**: Some operations use float, others string (inconsistent API)
9. **Order Book Limited to L2**: Only L2 snapshots available, not full depth
10. **No Order Book Aggregation**: Multiple L2 subscriptions needed for multiple coins

---

## Summary Table

| Aspect | Details |
|--------|---------|
| **Primary Use** | Trading, data retrieval, account management on Hyperliquid DEX |
| **Architecture** | Layered (API → Info/Exchange) with WebSocket thread |
| **Auth** | EIP-712 signing via eth_account.LocalAccount |
| **Data Flow** | Requests to /info (queries) and /exchange (trades); WebSocket for streams |
| **Scaling** | Millions of subscriptions possible; 10ms order confirmation typical |
| **Security** | Client-side signing; private keys never leave wallet |
| **Backwards Compat** | Semantic versioning; may have breaking changes in major versions |

---

This SDK provides production-ready access to Hyperliquid's trading infrastructure with emphasis on security (client-side signing), flexibility (multiple auth modes), and real-time data (WebSocket subscriptions).
