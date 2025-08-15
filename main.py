import asyncio
import json
import logging
import random
import time
import os
import csv
from datetime import datetime, timedelta
import requests
from flask import Flask, jsonify, render_template_string
from threading import Thread
import websockets
import tenacity

# ================== تنظیمات ==================
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT']

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8136421090:AAFrb8RI6BQ2tH49YXX_5S32_W0yWfT04Cg')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '570096331')
PORT = int(os.getenv('PORT', 8080))
BINANCE_WS_BASE = 'wss://stream.binance.com:9443/stream?streams='

LOG_FILE = 'whalepulse_pro.log'
REPORT_INTERVAL = 15*60
HOURLY_REPORT_INTERVAL = 60*60
MIN_CHANGE_PERCENT = 0.1
MIN_CHANGE_VOLUME = 0.01
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5
ALERT_THRESHOLD = float(os.getenv('ALERT_THRESHOLD', '5'))
ALERT_COOLDOWN = int(os.getenv('ALERT_COOLDOWN', '900'))
CSV_FILE = os.getenv('CSV_FILE', 'market_data.csv')
CSV_SAVE_INTERVAL = int(os.getenv('CSV_SAVE_INTERVAL', '30'))
WS_PROXY = os.getenv('WS_PROXY', None)

# ================== گلوبال ==================
last_report_time = 0
last_hourly_report_time = 0
last_report_data = {}
last_alert_time = {}
last_csv_write = {}
market_state = {}
app_status = {
    'status': 'starting',
    'websocket_connected': False,
    'last_message_time': None,
    'messages_processed': 0,
    'last_telegram_send': None,
    'uptime_start': datetime.now()
}

# ================== Logging ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger('whale_ws')

# ================== Flask ==================
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="fa">
<head>
<meta charset="UTF-8">
<title>🐋 داشبورد WhalePulse-Pro</title>
<style>
body {font-family: Tahoma, sans-serif; background:#1c1c1c; color:#f1f1f1;}
h1 {color:#00ffff;}
table {width:90%;margin:auto;border-collapse:collapse;}
th, td {padding:10px;text-align:center;border:1px solid #444;}
th {background:#222;color:#0f0;}
tr:nth-child(even) {background:#2c2c2c;}
tr:nth-child(odd) {background:#1c1c1c;}
.arrow-up {color:#0f0;} .arrow-down {color:#f00;}
</style>
<script>
async function fetchData(){
    let resp = await fetch('/api/market');
    let data = await resp.json();
    let table = '<table><tr><th>نماد</th><th>قیمت</th><th>تغییر %</th><th>حجم</th><th>آخرین بروزرسانی</th></tr>';
    for(let sym in data){
        let arrow = data[sym].price_change_percent>=0 ? '📈' : '📉';
        let cls = data[sym].price_change_percent>=0 ? 'arrow-up' : 'arrow-down';
        table += `<tr><td>${sym}</td><td>${data[sym].price}</td><td class="${cls}">${arrow} ${data[sym].price_change_percent.toFixed(2)}%</td><td>${data[sym].volume}</td><td>${data[sym].updated_at}</td></tr>`;
    }
    table += '</table>';
    document.getElementById('dashboard').innerHTML = table;
}
setInterval(fetchData, 5000);
window.onload = fetchData;
</script>
</head>
<body>
<h1>🐋 داشبورد زنده WhalePulse-Pro</h1>
<div id="dashboard"></div>
</body>
</html>
"""

@app.route('/')
def home():
    uptime = datetime.now() - app_status['uptime_start']
    return f"""
    <h1>🐋 WhalePulse-Pro</h1>
    <p><strong>وضعیت:</strong> {app_status['status']}</p>
    <p><strong>WebSocket:</strong> {'🟢 متصل' if app_status['websocket_connected'] else '🔴 قطع'}</p>
    <p><strong>زمان آنلاین:</strong> {uptime}</p>
    <p><strong>پیام پردازش شده:</strong> {app_status['messages_processed']}</p>
    <p><strong>نمادها:</strong> {', '.join(SYMBOLS)}</p>
    <hr>
    <a href="/status">📊 JSON وضعیت</a> | 
    <a href="/health">🏥 Health Check</a> | 
    <a href="/test">🧪 تست تلگرام</a> |
    <a href="/dashboard">📈 داشبورد زنده</a>
    """

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy' if app_status['websocket_connected'] else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        'uptime_seconds': (datetime.now() - app_status['uptime_start']).total_seconds()
    })

@app.route('/status')
def status():
    return jsonify({
        **app_status,
        'uptime_start': app_status['uptime_start'].isoformat(),
        'symbols': SYMBOLS,
        'telegram_configured': bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        'alert_threshold': ALERT_THRESHOLD,
        'alert_cooldown_sec': ALERT_COOLDOWN
    })

@app.route('/test')
def test_telegram():
    try:
        if test_telegram_bot():
            send_to_telegram("🧪 پیام تستی از WhalePulse-Pro!")
            return jsonify({'success': True, 'message': 'تلگرام تست موفق بود!'})
        return jsonify({'success': False, 'message': 'تلگرام تست ناموفق'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/market')
def api_market():
    return jsonify(market_state)

@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ================== Telegram ==================
def test_telegram_bot():
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe'
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            logger.info(f"✅ Bot info: {resp.json().get('result', {}).get('username','Unknown')}")
            return True
        logger.error(f"❌ Bot test failed: {resp.status_code}")
        return False
    except Exception as e:
        logger.error(f"❌ Bot test exception: {e}")
        return False

@tenacity.retry(stop=tenacity.stop_after_attempt(RETRY_ATTEMPTS), wait=tenacity.wait_fixed(RETRY_DELAY))
def send_to_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning('⚠️ Telegram token or chat id not configured.')
        return False
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code == 200:
        app_status['last_telegram_send'] = datetime.now().isoformat()
        logger.info('✅ پیام تلگرام ارسال شد.')
        return True
    raise Exception(f"Telegram send failed {resp.status_code}")

# ================== CSV & Alerts ==================
def ensure_csv_header():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['timestamp','symbol','price','volume','price_change_percent'])

def append_csv_row(symbol, price, volume, change_percent):
    ensure_csv_header()
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([datetime.now().isoformat(), symbol, price, volume, change_percent])

def maybe_save_csv(symbol, price, volume, change_percent, now_ts):
    last = last_csv_write.get(symbol, 0)
    if now_ts - last >= CSV_SAVE_INTERVAL:
        append_csv_row(symbol, price, volume, change_percent)
        last_csv_write[symbol] = now_ts

def maybe_alert(symbol, price, change_percent, now_ts):
    if abs(change_percent) >= ALERT_THRESHOLD:
        last = last_alert_time.get(symbol, 0)
        if now_ts - last >= ALERT_COOLDOWN:
            send_to_telegram(f"🚨 {symbol} {change_percent:+.2f}%\n💵 {price}")
            last_alert_time[symbol] = now_ts

def should_send_report(new_data):
    global last_report_data
    if not last_report_data: return True
    for sym, vals in new_data.items():
        last_vals = last_report_data.get(sym)
        if not last_vals: return True
        if abs(vals['price_change_percent'] - last_vals['price_change_percent']) >= MIN_CHANGE_PERCENT:
            return True
        if last_vals['volume'] > 0 and abs(vals['volume'] - last_vals['volume']) / last_vals['volume'] >= MIN_CHANGE_VOLUME:
            return True
    return False

def build_report_message(data):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg = f"🐋 <b>WhalePulse-Pro گزارش بازار</b>\n⏰ {now_str}\n\n"
    for sym, vals in data.items():
        arrow = "📈" if vals['price_change_percent'] >= 0 else "📉"
        msg += f"{sym} {arrow} {vals['price_change_percent']:+.2f}%\nقیمت: {vals['price']}\nحجم: {vals['volume']}\n\n"
    return msg

# ================== WebSocket ==================
def build_stream_path(symbols):
    return BINANCE_WS_BASE + "/".join([s.lower() + "@ticker" for s in symbols])

async def connect_and_run(uri):
    global last_report_time, last_hourly_report_time, last_report_data
    connect_kwargs = {"ping_interval":30,"ping_timeout":10,"max_size":None,"close_timeout":5}
    if WS_PROXY:
        connect_kwargs['http_proxy_host'] = WS_PROXY.split(":")[1].replace("//","")
        connect_kwargs['http_proxy_port'] = int(WS_PROXY.split(":")[2])
    async with websockets.connect(uri, **connect_kwargs) as ws:
        app_status['websocket_connected'] = True
        app_status['status'] = 'running'
        current_data = {}
        message_count = 0
        async for message in ws:
            try:
                msg = json.loads(message)
                data = msg.get('data') or msg
                if not isinstance(data, dict) or data.get('e') != '24hrTicker': continue
                symbol = data.get('s')
                if symbol not in SYMBOLS: continue
                price = float(data.get('c',0))
                volume = float(data.get('v',0))
                price_change_percent = float(data.get('P',0))
                now_ts = time.time()
                market_state[symbol] = {'price':price,'volume':volume,'price_change_percent':price_change_percent,'updated_at':datetime.now().isoformat()}
                current_data[symbol] = {'price':price,'volume':volume,'price_change_percent':price_change_percent}
                maybe_save_csv(symbol, price, volume, price_change_percent, now_ts)
                maybe_alert(symbol, price, price_change_percent, now_ts)
                if len(current_data) >= len(SYMBOLS):
                    if now_ts - last_report_time >= REPORT_INTERVAL and should_send_report(current_data):
                        send_to_telegram(build_report_message(current_data))
                        last_report_data = current_data.copy()
                        last_report_time = now_ts
                    if now_ts - last_hourly_report_time >= HOURLY_REPORT_INTERVAL:
                        send_to_telegram(build_report_message(current_data))
                        last_hourly_report_time = now_ts
                message_count += 1
            except Exception as e:
                logger.error(f'❌ WS processing error: {e}')

async def watcher_loop():
    uri = build_stream_path(SYMBOLS)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID: test_telegram_bot()
    attempt = 0
    while True:
        try:
            attempt += 1
            backoff = min(300, 2**min(attempt,8)) + random.uniform(0,5)
            await connect_and_run(uri)
            attempt = 0
        except Exception as e:
            app_status['websocket_connected'] = False
            app_status['status'] = f'reconnecting_{attempt}'
            logger.error(f"💥 WebSocket error: {e}")
            await asyncio.sleep(backoff)

def run_websocket_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(watcher_loop())

# ================== Main ==================
if __name__ == "__main__":
    ensure_csv_header()
    Thread(target=run_websocket_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True, use_reloader=False)
