#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import logging
import threading
import requests
from flask import Flask, render_template_string, request, redirect, url_for
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
LOWER_PRICE = 68000.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 2
INVEST_PER_GRID = 40.0
MIN_PROFIT_PERCENT = 0.8
CHECK_INTERVAL = 60

# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)
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

# === Глобальные переменные ===
active_orders = {}
buy_positions = {}
bot_running = True
log_messages = []

def add_log(msg):
    logger.info(msg)
    log_messages.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}")
    if len(log_messages) > 100:
        log_messages.pop(0)

def recalc_grid():
    global grid_levels
    step_price = (UPPER_PRICE - LOWER_PRICE) / (NUM_GRIDS - 1)
    grid_levels = [round(LOWER_PRICE + i * step_price, 2) for i in range(NUM_GRIDS)]
    add_log(f"Грид пересчитан: {grid_levels}")

recalc_grid()

def get_balances():
    """Возвращает словарь с балансами USDT и BTC."""
    account = client.account()
    result = {'USDT_free': 0, 'USDT_locked': 0, 'BTC_free': 0, 'BTC_locked': 0}
    for asset in account['balances']:
        if asset['asset'] == 'USDT':
            result['USDT_free'] = float(asset['free'])
            result['USDT_locked'] = float(asset['locked'])
        elif asset['asset'] == 'BTC':
            result['BTC_free'] = float(asset['free'])
            result['BTC_locked'] = float(asset['locked'])
    return result

def get_free_usdt():
    """Возвращает свободные USDT (без заблокированных под ордера)."""
    balances = get_balances()
    return balances['USDT_free']

def get_current_price():
    ticker = client.ticker_price(SYMBOL)
    return float(ticker['price'])

def cancel_all_orders():
    try:
        orders = client.get_open_orders(symbol=SYMBOL)
        for o in orders:
            client.cancel_order(symbol=SYMBOL, orderId=o['orderId'])
        active_orders.clear()
        add_log("✅ Все ордера отменены")
        send_telegram("✅ Все ордера отменены")
    except Exception as e:
        add_log(f"Ошибка при отмене ордеров: {e}")

def place_grid():
    current = get_current_price()
    free_usdt = get_free_usdt()

    for price in grid_levels:
        if price in active_orders:
            continue
        if price >= current:
            continue
        if free_usdt < INVEST_PER_GRID:
            add_log(f"⚠️ Недостаточно USDT для BUY на {price}, нужно {INVEST_PER_GRID}, доступно {free_usdt}")
            continue

        raw_qty = INVEST_PER_GRID / price
        qty = round_step(raw_qty, step_size)
        if qty < min_qty:
            add_log(f"⚠️ Сумма {INVEST_PER_GRID} USDT даёт {qty} BTC (< {min_qty}), пропускаем {price}")
            continue

        try:
            order = client.new_order(
                symbol=SYMBOL,
                side='BUY',
                type='LIMIT',
                quantity=qty,
                price=price,
                timeInForce='GTC'
            )
            active_orders[price] = order['orderId']
            add_log(f"✅ BUY размещён на {price}, qty={qty}")
            send_telegram(f"✅ BUY размещён на {price}, qty={qty}")
            free_usdt -= INVEST_PER_GRID
        except Exception as e:
            add_log(f"Ошибка при размещении BUY на {price}: {e}")

def check_orders():
    for price, oid in list(active_orders.items()):
        try:
            order = client.get_order(symbol=SYMBOL, orderId=oid)
            if order['status'] == 'FILLED':
                qty = float(order['executedQty'])
                sell_price = round(price * (1 + MIN_PROFIT_PERCENT / 100), 2)
                buy_positions[price] = {
                    'qty': qty,
                    'sell_price': sell_price,
                    'sell_order_id': None
                }
                del active_orders[price]
                add_log(f"📥 BUY исполнен на {price}, qty={qty}, готовим SELL на {sell_price}")
                send_telegram(f"📥 BUY исполнен на {price}, готовим SELL на {sell_price}")

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
                add_log(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
                send_telegram(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
            elif order['status'] in ('CANCELLED', 'EXPIRED'):
                del active_orders[price]
                add_log(f"❌ BUY ордер на {price} отменён/истёк")
        except Exception as e:
            add_log(f"Ошибка при проверке BUY ордера {price}: {e}")

    for buy_price, pos in list(buy_positions.items()):
        if pos['sell_order_id'] is None:
            continue
        try:
            order = client.get_order(symbol=SYMBOL, orderId=pos['sell_order_id'])
            if order['status'] == 'FILLED':
                profit = round(INVEST_PER_GRID * (MIN_PROFIT_PERCENT / 100), 2)
                add_log(f"🎉 ПРИБЫЛЬ зафиксирована: +{profit} USDT на {pos['sell_price']}")
                send_telegram(f"🎉 Прибыль +{profit}$ на {pos['sell_price']}")
                del buy_positions[buy_price]
            elif order['status'] in ('CANCELLED', 'EXPIRED'):
                add_log(f"❌ SELL ордер на {pos['sell_price']} отменён")
                del buy_positions[buy_price]
        except Exception as e:
            add_log(f"Ошибка при проверке SELL ордера {pos['sell_price']}: {e}")

def bot_loop():
    while bot_running:
        try:
            check_orders()
            place_grid()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            add_log(f"Ошибка в боте: {e}")
            time.sleep(10)

# === Flask веб-интерфейс ===
app = Flask(__name__)

HTML_TEMPLATE = '''
<!doctype html>
<html>
<head>
    <title>Grid Bot Control</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="15">
    <style>
        body { font-family: monospace; background: #111; color: #0f0; padding: 20px; }
        .container { max-width: 1200px; margin: auto; }
        .card { background: #222; border: 1px solid #0f0; margin: 10px 0; padding: 15px; border-radius: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #0f0; padding: 5px; text-align: left; }
        th { background: #0f0; color: #111; }
        input, button { background: #222; color: #0f0; border: 1px solid #0f0; padding: 5px; margin: 2px; }
        button:hover { background: #0f0; color: #111; cursor: pointer; }
    </style>
</head>
<body>
<div class="container">
    <h1>🤖 Grid Bot Controller</h1>
    <div class="card">
        <h2>📊 Балансы</h2>
        <p><strong>USDT (общий):</strong> {{ usdt_total }}<br>
        <strong>USDT (свободный):</strong> {{ usdt_free }}<br>
        <strong>USDT (заблокирован):</strong> {{ usdt_locked }}</p>
        <p><strong>BTC (свободный):</strong> {{ btc_free }}<br>
        <strong>BTC (заблокирован):</strong> {{ btc_locked }}</p>
        <p>Текущая цена BTC: <strong>{{ price }} USDT</strong></p>
    </div>
    <div class="card">
        <h2>⚙️ Настройки бота</h2>
        <form method="post" action="/update_config">
            <label>Нижняя граница: <input type="number" step="100" name="lower_price" value="{{ lower }}"></label><br>
            <label>Верхняя граница: <input type="number" step="100" name="upper_price" value="{{ upper }}"></label><br>
            <label>Количество гридов: <input type="number" step="1" name="num_grids" value="{{ num }}"></label><br>
            <label>Инвестиция на грид (USDT): <input type="number" step="5" name="invest" value="{{ invest }}"></label><br>
            <label>Профит %: <input type="number" step="0.1" name="profit" value="{{ profit }}"></label><br>
            <label>Интервал (сек): <input type="number" step="10" name="interval" value="{{ interval }}"></label><br>
            <button type="submit">💾 Сохранить настройки</button>
        </form>
        <form method="post" action="/cancel_orders" style="margin-top:10px;">
            <button type="submit">❌ Отменить все ордера</button>
        </form>
        <form method="post" action="/place_grid" style="margin-top:10px;">
            <button type="submit">🔄 Переразместить сетку</button>
        </form>
    </div>
    <div class="card">
        <h2>📈 Активные BUY ордера</h2>
        {% if buys %}
        <table>
            <tr><th>Цена (USDT)</th><th>Order ID</th></tr>
            {% for b in buys %}
            <tr><td>{{ b.price }}</td><td>{{ b.order_id }}</td></tr>
            {% endfor %}
        </table>
        {% else %}
        <p>Нет активных BUY ордеров</p>
        {% endif %}
    </div>
    <div class="card">
        <h2>📦 Открытые позиции (куплено, ждём продажи)</h2>
        {% if positions %}
        <table>
            <tr><th>Цена покупки</th><th>Кол-во BTC</th><th>Цель продажи</th><th>SELL ID</th></tr>
            {% for p in positions %}
            <tr><td>{{ p.buy_price }}</td><td>{{ p.qty }}</td><td>{{ p.sell_price }}</td><td>{{ p.sell_order_id or 'pending' }}</td></tr>
            {% endfor %}
        </table>
        {% else %}
        <p>Нет открытых позиций</p>
        {% endif %}
    </div>
    <div class="card">
        <h2>📜 Последние события</h2>
        <pre>{% for log in logs %}{{ log }}\n{% endfor %}</pre>
    </div>
</div>
</body>
</html>
'''

@app.route('/')
def index():
    price = get_current_price()
    balances = get_balances()
    buys = [{'price': p, 'order_id': oid} for p, oid in active_orders.items()]
    positions = [{'buy_price': bp, 'qty': pos['qty'], 'sell_price': pos['sell_price'], 'sell_order_id': pos['sell_order_id']}
                 for bp, pos in buy_positions.items()]
    logs = log_messages[-20:]
    return render_template_string(HTML_TEMPLATE,
        usdt_total=balances['USDT_free'] + balances['USDT_locked'],
        usdt_free=balances['USDT_free'],
        usdt_locked=balances['USDT_locked'],
        btc_free=balances['BTC_free'],
        btc_locked=balances['BTC_locked'],
        price=price, buys=buys, positions=positions,
        lower=LOWER_PRICE, upper=UPPER_PRICE, num=NUM_GRIDS, invest=INVEST_PER_GRID,
        profit=MIN_PROFIT_PERCENT, interval=CHECK_INTERVAL, logs=logs)

@app.route('/update_config', methods=['POST'])
def update_config():
    global LOWER_PRICE, UPPER_PRICE, NUM_GRIDS, INVEST_PER_GRID, MIN_PROFIT_PERCENT, CHECK_INTERVAL
    try:
        LOWER_PRICE = float(request.form.get('lower_price', LOWER_PRICE))
        UPPER_PRICE = float(request.form.get('upper_price', UPPER_PRICE))
        NUM_GRIDS = int(request.form.get('num_grids', NUM_GRIDS))
        INVEST_PER_GRID = float(request.form.get('invest', INVEST_PER_GRID))
        MIN_PROFIT_PERCENT = float(request.form.get('profit', MIN_PROFIT_PERCENT))
        CHECK_INTERVAL = int(request.form.get('interval', CHECK_INTERVAL))
        if NUM_GRIDS < 2:
            NUM_GRIDS = 2
        if LOWER_PRICE >= UPPER_PRICE:
            LOWER_PRICE, UPPER_PRICE = UPPER_PRICE-1000, UPPER_PRICE
        recalc_grid()
        add_log(f"Настройки обновлены: диапазон {LOWER_PRICE}–{UPPER_PRICE}, гридов {NUM_GRIDS}, инвест {INVEST_PER_GRID}, профит {MIN_PROFIT_PERCENT}%, интервал {CHECK_INTERVAL} сек")
        send_telegram(f"⚙️ Настройки обновлены: диапазон {LOWER_PRICE}–{UPPER_PRICE}, гридов {NUM_GRIDS}, инвест {INVEST_PER_GRID}, профит {MIN_PROFIT_PERCENT}%")
    except Exception as e:
        add_log(f"Ошибка при обновлении настроек: {e}")
    return redirect(url_for('index'))

@app.route('/cancel_orders', methods=['POST'])
def cancel_orders():
    cancel_all_orders()
    return redirect(url_for('index'))

@app.route('/place_grid', methods=['POST'])
def force_place_grid():
    cancel_all_orders()
    active_orders.clear()
    place_grid()
    add_log("🔁 Принудительное размещение сетки выполнено")
    send_telegram("🔁 Принудительное размещение сетки выполнено")
    return redirect(url_for('index'))

# === Запуск ===
if __name__ == '__main__':
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
