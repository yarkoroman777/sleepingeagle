# ====================== GRID BOT (BTCUSDT) - ИСПРАВЛЕННЫЙ ======================
# Исправлена ошибка LOT_SIZE, читает .env, 3 грида, 20 USDT, 0.8%

import os
import time
import logging
import threading
import requests
from dotenv import load_dotenv
from binance.spot import Spot
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient

load_dotenv()
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not API_KEY or not API_SECRET:
    raise Exception("API_KEY или API_SECRET не найдены в .env")

# ==================== ПАРАМЕТРЫ ====================
SYMBOL = 'BTCUSDT'
LOWER_PRICE = 65000.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 3
INVEST_PER_GRID = 20.0          # USDT на один уровень
MIN_PROFIT_PERCENT = 0.8        # 0.8% прибыли

# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
client = Spot(api_key=API_KEY, api_secret=API_SECRET)

# Получаем информацию о фильтрах для символа
symbol_info = client.exchange_info()
for s in symbol_info['symbols']:
    if s['symbol'] == SYMBOL:
        filters = {f['filterType']: f for f in s['filters']}
        lot_size_filter = filters['LOT_SIZE']
        min_qty = float(lot_size_filter['minQty'])
        step_size = float(lot_size_filter['stepSize'])
        break
else:
    raise Exception(f"Символ {SYMBOL} не найден")

def round_step(value, step):
    return round(value // step * step, 8)

# Расчёт уровней (арифметический)
step_price = (UPPER_PRICE - LOWER_PRICE) / (NUM_GRIDS - 1)
grid_levels = [round(LOWER_PRICE + i * step_price, 2) for i in range(NUM_GRIDS)]

active_orders = {}      # цена -> orderId
buy_prices = {}         # цена_покупки -> количество

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
    except:
        pass

def place_grid():
    current_price = float(client.ticker_price(SYMBOL)['price'])
    balance = client.account()
    usdt_free = float([b['free'] for b in balance['balances'] if b['asset'] == 'USDT'][0])

    for price in grid_levels:
        if price in active_orders:
            continue

        # Количество с учётом LOT_SIZE
        raw_qty = INVEST_PER_GRID / price
        qty = round_step(raw_qty, step_size)
        if qty < min_qty:
            logging.warning(f"Сумма {INVEST_PER_GRID} USDT при цене {price} даёт {qty} BTC (меньше {min_qty}), пропускаем")
            continue

        # Покупка
        if price < current_price and usdt_free >= INVEST_PER_GRID:
            order = client.new_order(
                symbol=SYMBOL,
                side='BUY',
                type='LIMIT',
                quantity=qty,
                price=price,
                timeInForce='GTC'
            )
            active_orders[price] = order['orderId']
            buy_prices[price] = qty
            logging.info(f"✅ BUY размещён на {price}, qty={qty}")
            send_telegram(f"✅ BUY размещён на {price}, qty={qty}")

        # Продажа
        for buy_price in list(buy_prices.keys()):
            sell_price = round(buy_price * (1 + MIN_PROFIT_PERCENT / 100), 2)
            if price >= sell_price and sell_price not in active_orders:
                qty_sell = buy_prices[buy_price]
                order = client.new_order(
                    symbol=SYMBOL,
                    side='SELL',
                    type='LIMIT',
                    quantity=qty_sell,
                    price=sell_price,
                    timeInForce='GTC'
                )
                active_orders[sell_price] = order['orderId']
                logging.info(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
                send_telegram(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
                break

def on_message(msg):
    if msg.get('e') == 'executionReport' and msg.get('X') == 'FILLED':
        price = float(msg['p'])
        side = msg['S']
        order_id = msg['i']

        for p, oid in list(active_orders.items()):
            if oid == order_id:
                del active_orders[p]
                break

        if side == 'SELL':
            for buy_price, qty in list(buy_prices.items()):
                target = buy_price * (1 + MIN_PROFIT_PERCENT / 100)
                if abs(target - price) < 0.01:
                    profit = round(INVEST_PER_GRID * (MIN_PROFIT_PERCENT / 100), 2)
                    logging.info(f"🎉 ПРИБЫЛЬ зафиксирована: +{profit} USDT на {price}")
                    send_telegram(f"🎉 Прибыль +{profit}$ на {price}")
                    del buy_prices[buy_price]
                    break

        place_grid()

def start_websocket():
    ws = SpotWebsocketStreamClient(on_message=on_message)
    ws.user_data_stream()

threading.Thread(target=start_websocket, daemon=True).start()

logging.info(f"🚀 БОТ ЗАПУЩЕН | Диапазон {LOWER_PRICE}–{UPPER_PRICE} | {NUM_GRIDS} грида | {INVEST_PER_GRID}$ на грид | прибыль {MIN_PROFIT_PERCENT}%")
send_telegram(f"🚀 Grid Bot запущен. Диапазон {LOWER_PRICE}–{UPPER_PRICE}, {NUM_GRIDS} грида, {INVEST_PER_GRID}$ на грид, прибыль {MIN_PROFIT_PERCENT}%")

while True:
    try:
        place_grid()
        time.sleep(20)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        time.sleep(10)
