#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
    raise Exception("API_KEY или API_SECRET не найдены")

# === НАСТРОЙКИ ===
SYMBOL = 'BTCUSDT'
LOWER_PRICE = 67000.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 2
INVEST_PER_GRID = 30.0
MIN_PROFIT_PERCENT = 0.8        # 0.8% профит
CHECK_INTERVAL = 60

# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
client = Spot(api_key=API_KEY, api_secret=API_SECRET)

# Получаем фильтры LOT_SIZE
symbol_info = client.exchange_info()
for s in symbol_info['symbols']:
    if s['symbol'] == SYMBOL:
        filters = {f['filterType']: f for f in s['filters']}
        lot = filters['LOT_SIZE']
        step_size = float(lot['stepSize'])
        min_qty = float(lot['minQty'])
        break
else:
    raise Exception(f"Символ {SYMBOL} не найден")

def round_step(value, step):
    return round(value // step * step, 8)

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
    except:
        pass

# === РАСЧЁТ УРОВНЕЙ ===
step_price = (UPPER_PRICE - LOWER_PRICE) / (NUM_GRIDS - 1)
grid_levels = [round(LOWER_PRICE + i * step_price, 2) for i in range(NUM_GRIDS)]

active_orders = {}      # цена -> orderId
buy_positions = {}      # цена_покупки -> {'qty': float, 'sell_price': float, 'sell_order_id': int}

def place_grid():
    current_price = float(client.ticker_price(SYMBOL)['price'])
    balance = client.account()
    usdt_free = float([b['free'] for b in balance['balances'] if b['asset'] == 'USDT'][0])

    for price in grid_levels:
        if price in active_orders:
            continue
        raw_qty = INVEST_PER_GRID / price
        qty = round_step(raw_qty, step_size)
        if qty < min_qty:
            continue

        # Покупка только если уровень ниже текущей цены и есть USDT
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
            logging.info(f"✅ BUY размещён на {price}, qty={qty}")
            send_telegram(f"✅ BUY размещён на {price}, qty={qty}")

def on_message(msg):
    if msg.get('e') != 'executionReport' or msg.get('X') != 'FILLED':
        return
    price = float(msg['p'])
    side = msg['S']
    order_id = msg['i']

    # Удаляем исполненный ордер из активных
    for p, oid in list(active_orders.items()):
        if oid == order_id:
            del active_orders[p]
            break

    if side == 'BUY':
        # Запоминаем позицию
        qty = float(msg['q'])
        sell_price = round(price * (1 + MIN_PROFIT_PERCENT / 100), 2)
        buy_positions[price] = {
            'qty': qty,
            'sell_price': sell_price,
            'sell_order_id': None
        }
        logging.info(f"📥 BUY исполнен на {price}, qty={qty}, готовим SELL на {sell_price}")
        send_telegram(f"📥 BUY исполнен на {price}, готовим SELL на {sell_price}")

        # Сразу выставляем SELL
        sell_order = client.new_order(
            symbol=SYMBOL,
            side='SELL',
            type='LIMIT',
            quantity=qty,
            price=sell_price,
            timeInForce='GTC'
        )
        buy_positions[price]['sell_order_id'] = sell_order['orderId']
        active_orders[sell_price] = sell_order['orderId']
        logging.info(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
        send_telegram(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")

    elif side == 'SELL':
        # Находим позицию по цене покупки
        for buy_price, pos in list(buy_positions.items()):
            if abs(pos['sell_price'] - price) < 0.01:
                profit = round(INVEST_PER_GRID * (MIN_PROFIT_PERCENT / 100), 2)
                logging.info(f"🎉 ПРИБЫЛЬ зафиксирована: +{profit} USDT на {price}")
                send_telegram(f"🎉 Прибыль +{profit}$ на {price}")
                del buy_positions[buy_price]
                break

    # Переразмещаем сетку (выставляем новые BUY, если нужно)
    place_grid()

def start_websocket():
    ws = SpotWebsocketStreamClient(on_message=on_message)
    ws.start()

threading.Thread(target=start_websocket, daemon=True).start()

logging.info(f"🚀 БОТ ЗАПУЩЕН | Диапазон {LOWER_PRICE}–{UPPER_PRICE} | {NUM_GRIDS} грида | {INVEST_PER_GRID}$ на грид | прибыль {MIN_PROFIT_PERCENT}%")
send_telegram(f"🚀 Grid Bot запущен. Диапазон {LOWER_PRICE}–{UPPER_PRICE}, {NUM_GRIDS} грида, {INVEST_PER_GRID}$ на грид, прибыль {MIN_PROFIT_PERCENT}%")

# Основной цикл (только для переразмещения, ордера уже управляются через WebSocket)
while True:
    try:
        place_grid()
        time.sleep(CHECK_INTERVAL)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        time.sleep(10)
