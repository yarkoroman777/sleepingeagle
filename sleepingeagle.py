import time
import logging
from binance.spot import Spot
import requests
from dotenv import load_dotenv
import os

load_dotenv()

API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOL = 'BTCUSDT'
LOWER_PRICE = 66000.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 1
INVEST_PER_GRID = 86.0
MIN_PROFIT_PERCENT = 0.85

SHIFT_THRESHOLD = 6.0
SHIFT_SIZE = 5.0

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

client = Spot(api_key=API_KEY, api_secret=API_SECRET)

active_orders = {}
buy_prices = {}

def send_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
        except:
            pass

def place_grid():
    current_price = float(client.ticker_price(SYMBOL)['price'])

    # Для 1 грида логика упрощённая
    if NUM_GRIDS == 1:
        if not active_orders:
            qty = round(INVEST_PER_GRID / current_price, 6)
            if current_price > LOWER_PRICE:
                order = client.new_order(symbol=SYMBOL, side='BUY', type='LIMIT', quantity=qty, price=LOWER_PRICE, timeInForce='GTC')
                active_orders[LOWER_PRICE] = order['orderId']
                buy_prices[LOWER_PRICE] = qty
                logging.info(f"BUY размещён на {LOWER_PRICE}")
                send_telegram(f"BUY → {LOWER_PRICE}")
    else:
        # Оставляем старую логику для нескольких гридов (на будущее)
        pass

    # Продажа только в плюсе
    for buy_price in list(buy_prices.keys()):
        sell_price = buy_price * (1 + MIN_PROFIT_PERCENT / 100)
        if current_price >= sell_price and buy_price in buy_prices:
            qty_sell = buy_prices[buy_price]
            order = client.new_order(symbol=SYMBOL, side='SELL', type='LIMIT', quantity=qty_sell, price=sell_price, timeInForce='GTC')
            active_orders[sell_price] = order['orderId']
            logging.info(f"SELL → {sell_price} (+{MIN_PROFIT_PERCENT}%)")
            send_telegram(f"SELL → {sell_price} (+{MIN_PROFIT_PERCENT}%)")
            del buy_prices[buy_price]
            break

def shift_grid(direction):
    global LOWER_PRICE, UPPER_PRICE
    shift = (UPPER_PRICE - LOWER_PRICE) * (SHIFT_SIZE / 100)
    if direction == "up":
        LOWER_PRICE += shift
        UPPER_PRICE += shift
    else:
        LOWER_PRICE -= shift
        UPPER_PRICE -= shift
    logging.info(f"Грид сдвинут {direction} → {LOWER_PRICE:.0f}-{UPPER_PRICE:.0f}")
    send_telegram(f"Грид сдвинут {direction}")

logging.info(f"БОТ ЗАПУЩЕН | 86$ | 1 грид")

while True:
    try:
        current_price = float(client.ticker_price(SYMBOL)['price'])
        if current_price > UPPER_PRICE * (1 + SHIFT_THRESHOLD / 100):
            shift_grid("up")
        elif current_price < LOWER_PRICE * (1 - SHIFT_THRESHOLD / 100):
            shift_grid("down")
        place_grid()
        time.sleep(20)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        time.sleep(10)
