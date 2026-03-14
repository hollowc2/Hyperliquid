import os
import urllib.request
import json
import time
import schedule

from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

# Telegram configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HL_WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")

# Alert threshold configuration
LIQ_DISTANCE_THRESHOLD = float(os.getenv("LIQ_DISTANCE_THRESHOLD", 5.0))        # % distance to liquidation for alert
TOTAL_RISK_THRESHOLD = float(os.getenv("TOTAL_RISK_THRESHOLD", 1000.0))          # USD
POSITION_RISK_PERCENT_THRESHOLD = float(os.getenv("POSITION_RISK_PERCENT_THRESHOLD", 5.0))  # % of withdrawable

def post_info_request(payload):
    """
    Helper to post to Hyperliquid /info endpoint.
    """
    url = "https://api.hyperliquid.xyz/info"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req)
        return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"API error: {e.code} {e.reason}. Payload: {payload}")
        raise

def get_user_positions(user_address):
    payload = {"type": "clearinghouseState", "user": user_address}
    return post_info_request(payload)

def get_open_orders(user_address):
    payload = {"type": "frontendOpenOrders", "user": user_address}
    return post_info_request(payload)

def get_user_fills(user_address):
    payload = {"type": "userFills", "user": user_address}
    return post_info_request(payload)

def calculate_historical_pnl_and_drawdown(user_address):
    """
    Fetches user fills and calculates cumulative realized PNL curve, max drawdown %.
    Assumes PNL starts from 0; this is historical realized PNL only (no unrealized).
    """
    fills_data = get_user_fills(user_address)
    # Sort fills by time (ascending)
    fills = sorted(fills_data, key=lambda f: f['time'])
    
    cumulative_pnl = 0.0
    pnl_curve = [0.0]  # Starting equity curve
    peak = 0.0
    max_dd = 0.0
    
    for fill in fills:
        closed_pnl = float(fill.get('closedPnl', 0.0))
        cumulative_pnl += closed_pnl
        pnl_curve.append(cumulative_pnl)
        
        # Update max drawdown
        if cumulative_pnl > peak:
            peak = cumulative_pnl
        dd = (peak - cumulative_pnl) / peak if peak != 0 else 0
        if dd > max_dd:
            max_dd = dd
    
    max_dd_percent = max_dd * 100
    print(f"Historical Cumulative PNL (final): ${cumulative_pnl:.2f}")
    print(f"Max Drawdown: {max_dd_percent:.2f}%")
    
    return cumulative_pnl, max_dd_percent, pnl_curve

def send_telegram_alert(message):
    """
    Sends an alert message to Telegram using the configured bot.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; skipping alert.")
        return
    encoded_message = quote(message)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={encoded_message}"
    try:
        urllib.request.urlopen(url)
        print("Telegram alert sent.")
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

def check_and_alert(user_address):
    """
    Runs the risk calculation, historical PNL, checks for alerts, and sends notifications if needed.
    """
    # Calculate historical PNL and drawdown
    calculate_historical_pnl_and_drawdown(user_address)
    
    # Calculate current risk (as before)
    pos_data = get_user_positions(user_address)
    positions = pos_data.get('assetPositions', [])
    
    orders = get_open_orders(user_address)
    
    # Extract margin health from pos_data
    margin_summary = pos_data.get('marginSummary', {})
    withdrawable = float(pos_data.get('withdrawable', 0))
    account_value = float(margin_summary.get('accountValue', 0))
    effective_leverage = account_value / withdrawable if withdrawable > 0 else 0
    
    total_risk = 0.0
    total_liq_risk = 0.0
    liq_distances = []
    alerts = []
    
    for pos_item in positions:
        position = pos_item['position']
        coin = position['coin']
        szi = float(position['szi'])
        if szi == 0:
            continue
        entry_px = float(position['entryPx'])
        is_long = szi > 0
        liq_px_str = position.get('liquidationPx')
        liq_px = float(liq_px_str) if liq_px_str else None
        
        # Find relevant stop loss orders
        relevant_stops = []
        closing_side = "A" if is_long else "B"
        for order in orders:
            if order.get('coin') != coin or not order.get('isTrigger', False) or not order.get('reduceOnly', False) or order.get('side') != closing_side:
                continue
            condition = order.get('triggerCondition', 'N/A').lower()
            is_lte = any(term in condition for term in ['<=', '<', 'lte', 'below', 'less'])
            is_gte = any(term in condition for term in ['>=', '>', 'gte', 'above', 'greater'])
            if (is_long and is_lte) or (not is_long and is_gte):
                relevant_stops.append(order)
        
        # Risk calculation
        pos_risk = 0.0
        covered_sz = 0.0
        for stop in relevant_stops:
            stop_px = float(stop['triggerPx'])
            stop_sz = float(stop['sz'])
            distance = abs(entry_px - stop_px)
            pos_risk += stop_sz * distance
            covered_sz += stop_sz
        
        remaining_sz = abs(szi) - covered_sz
        if remaining_sz > 0 and liq_px is not None:
            liq_distance = abs(entry_px - liq_px)
            pos_risk += remaining_sz * liq_distance
        
        total_risk += pos_risk
        
        # 1% rule check
        if withdrawable > 0 and pos_risk > (POSITION_RISK_PERCENT_THRESHOLD / 100) * withdrawable:
            warning = f"Warning: {coin} position risk (${pos_risk:.2f}) exceeds {POSITION_RISK_PERCENT_THRESHOLD}% of withdrawable (${withdrawable:.2f})"
            print(warning)
            alerts.append(warning)
        
        # Liquidation metrics
        if liq_px is not None:
            liq_pct_distance = abs((liq_px - entry_px) / entry_px) * 100
            liq_distances.append((coin, liq_pct_distance))
            liq_risk = abs(szi) * abs(entry_px - liq_px)
            total_liq_risk += liq_risk
            
            # Liquidation alert check
            if liq_pct_distance < LIQ_DISTANCE_THRESHOLD:
                alert = f"Alert: {coin} nearing liquidation ({liq_pct_distance:.2f}% distance)"
                print(alert)
                alerts.append(alert)
    
    # Total risk alert check
    if total_risk > TOTAL_RISK_THRESHOLD:
        alert = f"Alert: Total risk exceeds threshold (${total_risk:.2f} > ${TOTAL_RISK_THRESHOLD:.2f})"
        print(alert)
        alerts.append(alert)
    
    # Output current metrics
    print(f"Margin Health: Withdrawable ${withdrawable:.2f}, Account Value ${account_value:.2f}, Effective Leverage {effective_leverage:.2f}x")
    print("Liquidation Distances:")
    for coin, dist in liq_distances:
        print(f"  {coin}: {dist:.2f}% price change to liquidation")
    print(f"Total Liquidation Risk: ${total_liq_risk:.2f}")
    print(f"Total risk on the wallet: ${total_risk:.2f}")
    
    # Send alerts if any
    if alerts:
        send_telegram_alert("\n".join(alerts))

address = HL_WALLET_ADDRESS

# Run once immediately
check_and_alert(address)

# Schedule periodic checks (e.g., every 5 minutes)
schedule.every(5).minutes.do(check_and_alert, user_address=address)

# Run the scheduler loop
while True:
    schedule.run_pending()
    time.sleep(1)