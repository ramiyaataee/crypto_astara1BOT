import asyncio
import json
import logging
import time
import requests
import websockets
from flask import Flask
from threading import Thread

# تنظیمات لاگ‌گیری
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

MAX_RETRIES = 10  # حداکثر تلاش قبل از fallback
PING_INTERVAL = 20  # ثانیه
BACKOFF_BASE = 2  # ضریب backoff

# Flask app برای Render
app = Flask(__name__)

@app.route("/")
def home():
    return "Crypto Bot is running on Render ✅"

async def fetch_rest_data():
    """دریافت قیمت از REST API به عنوان fallback"""
    try:
        r = requests.get(BINANCE_REST_URL, timeout=5)
        if r.status_code == 200:
            data = r.json()
            logging.info(f"[REST Fallback] BTCUSDT Price: {data['price']}")
        else:
            logging.error(f"[REST Fallback] HTTP {r.status_code}")
    except Exception as e:
        logging.error(f"[REST Fallback Error] {e}")

async def connect_ws():
    """اتصال به WebSocket با reconnect خودکار"""
    retries = 0
    while True:
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=None) as ws:
                logging.info("✅ Connected to Binance WebSocket")
                retries = 0  # موفقیت، ریست شمارنده
                
                # تسک heartbeat
                async def heartbeat():
                    while True:
                        try:
                            await ws.ping()
                            await asyncio.sleep(PING_INTERVAL)
                        except Exception:
                            break
                        
                asyncio.create_task(heartbeat())

                # خواندن پیام‌ها
                async for msg in ws:
                    data = json.loads(msg)
                    logging.info(f"[WS] BTCUSDT Price: {data['c']}")

        except websockets.exceptions.InvalidStatusCode as e:
            logging.error(f"[WebSocket Error] Invalid status code: {e.status_code}")
            if e.status_code == 403:
                logging.error("Access forbidden (403). Possible IP restriction.")
        except Exception as e:
            logging.error(f"[WebSocket Error] {e}")

        retries += 1
        if retries >= MAX_RETRIES:
            logging.warning("🔄 Max retries reached. Switching to REST API polling.")
            while True:
                await fetch_rest_data()
                await asyncio.sleep(5)
        
        wait_time = BACKOFF_BASE ** retries
        logging.info(f"Reconnecting in {wait_time} seconds...")
        await asyncio.sleep(wait_time)

def start_ws_loop():
    """اجرای حلقه asyncio در یک Thread جداگانه"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(connect_ws())

# شروع WebSocket در یک Thread
Thread(target=start_ws_loop, daemon=True).start()

if __name__ == "__main__":
    # اجرای Flask روی Render
    app.run(host="0.0.0.0", port=5000)
