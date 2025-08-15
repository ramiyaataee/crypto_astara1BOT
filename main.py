import os
import csv
import json
import asyncio
import logging
import aiohttp
import time
from datetime import datetime
from threading import Thread
from python_telegram_bot import Bot

# ======== تنظیمات ========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
CSV_FILE = os.getenv("CSV_FILE", "market_data.csv")
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", 5))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", 900))  # ثانیه
WS_PROXY = os.getenv("WS_PROXY", None)

SYMBOLS = ['btcusdt', 'ethusdt', 'solusdt', 'xrpusdt', 'adausdt']

# ======== Logger ========
logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger()

# ======== Telegram Bot ========
bot = Bot(token=TELEGRAM_TOKEN)
last_alert_time = {}

async def send_telegram_alert(symbol, price):
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < ALERT_COOLDOWN:
        return
    msg = f"🚨 ALERT: {symbol.upper()} قیمت {price} رسید!"
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    last_alert_time[symbol] = now
    logger.info(f"Alert sent for {symbol}: {price}")

# ======== CSV ========
def write_csv(data):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=data.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(data)

# ======== WebSocket Handler ========
async def binance_ws(symbol):
    url = f"wss://stream.binance.com:9443/ws/{symbol}@ticker"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, proxy=WS_PROXY) as ws:
                    logger.info(f"Connected to {symbol} WebSocket")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            price = float(data.get("c", 0))
                            row = {
                                "timestamp": datetime.utcnow().isoformat(),
                                "symbol": symbol,
                                "price": price
                            }
                            write_csv(row)
                            if price >= ALERT_THRESHOLD:
                                await send_telegram_alert(symbol, price)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
        except Exception as e:
            logger.warning(f"{symbol} WS disconnected: {e}")
            await asyncio.sleep(5)  # reconnect delay

# ======== Run All ========
async def main():
    tasks = [binance_ws(sym) for sym in SYMBOLS]
    await asyncio.gather(*tasks)

def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    t = Thread(target=start_loop, args=(loop,), daemon=True)
    t.start()
    logger.info("Crypto bot started.")
    # Keep main thread alive for Render
    while True:
        time.sleep(60)
