# ======== Optimized WebSocket Handler for Render ========

import asyncio
import json
import logging
import random
import time
import requests
import websockets

PING_INTERVAL = 15  # ثانیه، heartbeat داخلی برای جلوگیری از idle disconnect
REST_FALLBACK_INTERVAL = 30  # ثانیه، فاصله گرفتن از WebSocket در صورت قطع طولانی
MAX_403_BACKOFF = 300  # ثانیه، backoff حداکثر برای 403

async def websocket_heartbeat(ws):
    """Task جداگانه برای ارسال ping داخلی هر PING_INTERVAL ثانیه"""
    while True:
        try:
            await ws.ping()
        except Exception as e:
            logger.warning(f"⚠️ Heartbeat failed: {e}")
            break
        await asyncio.sleep(PING_INTERVAL)

async def rest_fallback(symbols):
    """داده‌ها را از REST API در صورت قطع طولانی WebSocket دریافت می‌کند"""
    url = "https://api.binance.com/api/v3/ticker/24hr"
    while True:
        try:
            for sym in symbols:
                r = requests.get(url, params={'symbol': sym}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    price = float(data.get('lastPrice', 0))
                    volume = float(data.get('volume', 0))
                    price_change_percent = float(data.get('priceChangePercent', 0))
                    now_ts = time.time()
                    # بروزرسانی وضعیت بازار
                    market_state[sym] = {
                        'price': price,
                        'volume': volume,
                        'price_change_percent': price_change_percent,
                        'updated_at': datetime.now().isoformat()
                    }
                    maybe_save_csv(sym, price, volume, price_change_percent, now_ts)
                    maybe_alert(sym, price, price_change_percent, now_ts)
            await asyncio.sleep(REST_FALLBACK_INTERVAL)
        except Exception as e:
            logger.error(f"💥 REST fallback error: {e}")
            await asyncio.sleep(REST_FALLBACK_INTERVAL)

async def connect_and_run_optimized(uri):
    """WebSocket اصلی با مدیریت کامل خطا و heartbeat"""
    global last_report_time, last_hourly_report_time, last_report_data
    attempt = 0
    max_attempts = 50
    while attempt < max_attempts:
        try:
            logger.info(f"🔌 Connecting to {uri} (attempt {attempt+1}/{max_attempts})")
            async with websockets.connect(
                uri,
                ping_interval=None,  # غیرفعال کردن ping داخلی websockets
                ping_timeout=None,
                max_size=None,
                close_timeout=5
            ) as ws:
                app_status['websocket_connected'] = True
                app_status['status'] = 'running'
                logger.info('✅ WebSocket connected successfully')

                # اجرای heartbeat جداگانه
                heartbeat_task = asyncio.create_task(websocket_heartbeat(ws))

                message_count = 0
                current_data = {}

                async for message in ws:
                    message_count += 1
                    app_status['messages_processed'] += 1
                    app_status['last_message_time'] = datetime.now().isoformat()
                    try:
                        msg = json.loads(message)
                        data = msg.get('data') or msg
                        if not isinstance(data, dict) or data.get('e') != '24hrTicker':
                            continue

                        symbol = data.get('s')
                        if symbol not in SYMBOLS:
                            continue

                        volume = float(data.get('v', 0))
                        price = float(data.get('c', 0))
                        price_change_percent = float(data.get('P', 0))
                        now_ts = time.time()

                        # بروزرسانی وضعیت بازار
                        market_state[symbol] = {
                            'price': price,
                            'volume': volume,
                            'price_change_percent': price_change_percent,
                            'updated_at': datetime.now().isoformat()
                        }

                        current_data[symbol] = {
                            'volume': volume,
                            'price': price,
                            'price_change_percent': price_change_percent
                        }

                        # CSV و هشدارها
                        maybe_save_csv(symbol, price, volume, price_change_percent, now_ts)
                        maybe_alert(symbol, price, price_change_percent, now_ts)

                        # گزارش‌ها
                        if len(current_data) >= len(SYMBOLS):
                            if now_ts - last_report_time >= REPORT_INTERVAL and should_send_report(current_data):
                                try:
                                    msg_text = build_report_message(current_data)
                                    if send_to_telegram(msg_text):
                                        last_report_data = current_data.copy()
                                        last_report_time = now_ts
                                        logger.info("📊 گزارش 15 دقیقه ارسال شد")
                                except Exception as e:
                                    logger.error(f"❌ Error sending 15min report: {e}")

                            if now_ts - last_hourly_report_time >= HOURLY_REPORT_INTERVAL:
                                try:
                                    msg_text = build_report_message(current_data)
                                    send_to_telegram(msg_text)
                                    last_hourly_report_time = now_ts
                                    logger.info("✅ گزارش ساعتی ارسال شد")
                                except Exception as e:
                                    logger.error(f"❌ Error sending hourly report: {e}")

                        if message_count % 200 == 0:
                            logger.info(f"Processed {message_count} WS messages. Symbols tracked: {len(current_data)}")

                    except Exception as e:
                        logger.error(f'❌ Error processing WS message: {e}')

        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 403:
                attempt += 1
                backoff = min(MAX_403_BACKOFF, (2 ** attempt) + random.uniform(0,5))
                logger.warning(f"🚫 HTTP 403 detected, backing off {backoff:.1f}s before retry...")
                await asyncio.sleep(backoff)
                continue
            else:
                raise
        except Exception as e:
            attempt += 1
            backoff = min(300, (2 ** attempt) + random.uniform(0,5))
            app_status['websocket_connected'] = False
            app_status['status'] = f'reconnecting_attempt_{attempt}'
            logger.error(f"💥 WebSocket error: {e}")
            logger.info(f"⏳ Waiting {backoff:.1f}s before reconnect...")
            await asyncio.sleep(backoff)

    # اگر همه تلاش‌ها ناموفق بود، fallback به REST
    logger.error("❌ Max reconnect attempts reached, switching to REST fallback...")
    app_status['status'] = 'rest_fallback'
    await rest_fallback(SYMBOLS)
