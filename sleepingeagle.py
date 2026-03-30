# ====================== GRID BOT (REST) - 2 ГРИДА ======================
import os
import time
import logging
import requests
from dotenv import load_dotenv
from binance.spot import Spot

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
NUM_GRIDS = 2
INVEST_PER_GRID = 20.0
MIN_PROFIT_PERCENT = 0.8
CHECK_INTERVAL = 60            # секунд между проверками

# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
client = Spot(api_key=API_KEY, api_secret=API_SECRET)

# Расчёт уровней (арифметический)
step_price = (UPPER_PRICE - LOWER_PRICE) / (NUM_GRIDS - 1)
grid_levels = [round(LOWER_PRICE + i * step_price, 2) for i in range(NUM_GRIDS)]

# Хранилище активных ордеров: {цена: orderId}
active_buy_orders = {}
# Хранилище купленных позиций: {цена_покупки: {'qty': float, 'sell_price': float}}
buy_positions = {}

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
    except:
        pass

def get_free_usdt():
    """Возвращает свободные USDT (с учётом заблокированных под ордера)."""
    account = client.account()
    for asset in account['balances']:
        if asset['asset'] == 'USDT':
            free = float(asset['free'])
            locked = float(asset['locked'])
            return free - locked   # реально доступные для новых ордеров
    return 0.0

def get_order_status(order_id):
    """Возвращает статус ордера (NEW, FILLED, CANCELLED)."""
    try:
        order = client.get_order(symbol=SYMBOL, orderId=order_id)
        return order['status']
    except:
        return None

def place_buy(price, qty):
    """Выставить лимитный BUY ордер."""
    try:
        order = client.new_order(
            symbol=SYMBOL,
            side='BUY',
            type='LIMIT',
            quantity=qty,
            price=price,
            timeInForce='GTC'
        )
        active_buy_orders[price] = order['orderId']
        logging.info(f"✅ BUY размещён на {price}, qty={qty}")
        send_telegram(f"✅ BUY размещён на {price}, qty={qty}")
    except Exception as e:
        logging.error(f"Ошибка размещения BUY на {price}: {e}")

def place_sell(buy_price, qty, sell_price):
    """Выставить лимитный SELL ордер (если ещё нет)."""
    try:
        order = client.new_order(
            symbol=SYMBOL,
            side='SELL',
            type='LIMIT',
            quantity=qty,
            price=sell_price,
            timeInForce='GTC'
        )
        # Запоминаем, что SELL ордер создан (в buy_positions добавим поле order_id)
        for pos in buy_positions.values():
            if abs(pos['buy_price'] - buy_price) < 0.01:
                pos['sell_order_id'] = order['orderId']
                break
        logging.info(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
        send_telegram(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
    except Exception as e:
        logging.error(f"Ошибка размещения SELL на {sell_price}: {e}")

def check_orders():
    """Проверяет статусы активных BUY ордеров и открытых позиций."""
    # Проверяем BUY ордера
    for price, order_id in list(active_buy_orders.items()):
        status = get_order_status(order_id)
        if status == 'FILLED':
            # Ордер исполнился, получаем количество
            order = client.get_order(symbol=SYMBOL, orderId=order_id)
            qty = float(order['executedQty'])
            sell_price = round(price * (1 + MIN_PROFIT_PERCENT / 100), 2)
            buy_positions[price] = {
                'qty': qty,
                'sell_price': sell_price,
                'sell_order_id': None
            }
            del active_buy_orders[price]
            logging.info(f"📥 BUY исполнен на {price}, qty={qty}, готовим SELL на {sell_price}")
            send_telegram(f"📥 BUY исполнен на {price}, готовим SELL на {sell_price}")
            # Сразу выставляем SELL (если цена уже не выше)
            if sell_price <= get_current_price():
                place_sell(price, qty, sell_price)
        elif status in ('CANCELLED', 'EXPIRED'):
            del active_buy_orders[price]
            logging.info(f"❌ BUY ордер на {price} отменён/истёк")

    # Проверяем позиции, где SELL ордер ещё не выставлен
    for buy_price, pos in list(buy_positions.items()):
        if pos['sell_order_id'] is None:
            # Выставляем SELL, если текущая цена >= цели
            current = get_current_price()
            if current >= pos['sell_price']:
                place_sell(buy_price, pos['qty'], pos['sell_price'])
        else:
            # Проверяем статус SELL ордера
            status = get_order_status(pos['sell_order_id'])
            if status == 'FILLED':
                profit = round(INVEST_PER_GRID * (MIN_PROFIT_PERCENT / 100), 2)
                logging.info(f"🎉 ПРИБЫЛЬ зафиксирована: +{profit} USDT на {pos['sell_price']}")
                send_telegram(f"🎉 Прибыль +{profit}$ на {pos['sell_price']}")
                del buy_positions[buy_price]
            elif status in ('CANCELLED', 'EXPIRED'):
                del buy_positions[buy_price]
                logging.info(f"❌ SELL ордер на {pos['sell_price']} отменён")

def get_current_price():
    """Возвращает текущую цену символа."""
    ticker = client.ticker_price(SYMBOL)
    return float(ticker['price'])

def place_grid():
    """Выставляет BUY ордера на уровни, которые ниже текущей цены и ещё не выставлены."""
    current = get_current_price()
    free_usdt = get_free_usdt()

    for price in grid_levels:
        if price in active_buy_orders:
            continue

        # Пропускаем уровни, которые выше текущей цены (покупать не будем)
        if price >= current:
            continue

        # Проверяем, достаточно ли USDT
        if free_usdt < INVEST_PER_GRID:
            logging.warning(f"Недостаточно USDT для BUY на {price}, нужно {INVEST_PER_GRID}, доступно {free_usdt}")
            continue

        # Рассчитываем количество с учётом LOT_SIZE
        raw_qty = INVEST_PER_GRID / price
        # Получаем фильтры LOT_SIZE (один раз, кэшируем)
        if not hasattr(place_grid, 'step_size'):
            info = client.exchange_info()
            for s in info['symbols']:
                if s['symbol'] == SYMBOL:
                    filters = {f['filterType']: f for f in s['filters']}
                    lot = filters['LOT_SIZE']
                    place_grid.step_size = float(lot['stepSize'])
                    place_grid.min_qty = float(lot['minQty'])
                    break
        step = place_grid.step_size
        min_qty = place_grid.min_qty
        qty = round(raw_qty // step * step, 8)
        if qty < min_qty:
            logging.warning(f"Сумма {INVEST_PER_GRID} USDT даёт {qty} BTC (< {min_qty}), пропускаем")
            continue

        place_buy(price, qty)
        free_usdt -= INVEST_PER_GRID   # уменьшаем локальную копию, чтобы не выставить два ордера за раз

logging.info(f"🚀 БОТ ЗАПУЩЕН (REST) | Диапазон {LOWER_PRICE}–{UPPER_PRICE} | {NUM_GRIDS} грида | {INVEST_PER_GRID}$ на грид | прибыль {MIN_PROFIT_PERCENT}% | интервал {CHECK_INTERVAL} сек")
send_telegram(f"🚀 Grid Bot запущен (REST). Диапазон {LOWER_PRICE}–{UPPER_PRICE}, {NUM_GRIDS} грида, {INVEST_PER_GRID}$ на грид, прибыль {MIN_PROFIT_PERCENT}%")

while True:
    try:
        # Сначала проверяем статусы уже существующих ордеров
        check_orders()
        # Затем выставляем новые BUY, если нужно
        place_grid()
        time.sleep(CHECK_INTERVAL)
    except Exception as e:
        logging.error(f"Ошибка в основном цикле: {e}")
        time.sleep(10)
