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
LOWER_PRICE = 62000.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 2
INVEST_PER_GRID = 43.0
MIN_PROFIT_PERCENT = 0.85

SHIFT_THRESHOLD = 6.0
SHIFT_SIZE = 5.0

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

client = Spot(api_key=API_KEY, api_secret=API_SECRET)

def recalculate_grid(lower, upper):
    step = (upper - lower) / (NUM_GRIDS - 1)
    return [round(lower + i * step, 2) for i in range(NUM_GRIDS)]

grid_levels = recalculate_grid(LOWER_PRICE, UPPER_PRICE)
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
    for price in grid_levels:
        if price in active_orders:
            continue
        qty = round(INVEST_PER_GRID / price, 6)
        if price < current_price:
            order = client.new_order(symbol=SYMBOL, side='BUY', type='LIMIT', quantity=qty, price=price, timeInForce='GTC')
            active_orders[price] = order['orderId']
            buy_prices[price] = qty
            logging.info(f"BUY → {price}")
            send_telegram(f"BUY → {price}")
        else:
            for buy_price in list(buy_prices.keys()):
                if price >= buy_price * (1 + MIN_PROFIT_PERCENT / 100):
                    qty_sell = buy_prices[buy_price]
                    order = client.new_order(symbol=SYMBOL, side='SELL', type='LIMIT', quantity=qty_sell, price=price, timeInForce='GTC')
                    active_orders[price] = order['orderId']
                    logging.info(f"SELL → {price} (+{MIN_PROFIT_PERCENT}%)")
                    send_telegram(f"SELL → {price} (+{MIN_PROFIT_PERCENT}%)")
                    break

def shift_grid(direction):
    global grid_levels
    shift = (UPPER_PRICE - LOWER_PRICE) * (SHIFT_SIZE / 100)
    if direction == "up":
        new_lower = LOWER_PRICE + shift
        new_upper = UPPER_PRICE + shift
    else:
        new_lower = LOWER_PRICE - shift
        new_upper = UPPER_PRICE - shift
    grid_levels = recalculate_grid(new_lower, new_upper)
    logging.info(f"Грид сдвинут {direction}")
    send_telegram(f"Грид сдвинут {direction}")

logging.info(f"БОТ ЗАПУЩЕН | 86$ | 2 грида по 43$")

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
