# ====================== GRID BOT + WEB INTERFACE ======================
import os
import time
import logging
import threading
import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from binance.spot import Spot

load_dotenv()
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if not API_KEY or not API_SECRET:
    raise Exception("API_KEY или API_SECRET не найдены в .env")

# ==================== ПАРАМЕТРЫ (по умолчанию) ====================
SYMBOL = 'BTCUSDT'
LOWER_PRICE = 66500.0
UPPER_PRICE = 70000.0
NUM_GRIDS = 2
INVEST_PER_GRID = 20.0
MIN_PROFIT_PERCENT = 0.8
CHECK_INTERVAL = 60

# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
client = Spot(api_key=API_KEY, api_secret=API_SECRET)

# Глобальные переменные для состояния
active_buy_orders = {}      # цена -> orderId
buy_positions = {}          # цена_покупки -> {'qty': float, 'sell_price': float, 'sell_order_id': int}
grid_levels = []
step_size = 0.0
min_qty = 0.0
bot_running = True
log_messages = []           # храним последние сообщения лога

# Инициализация Flask
app = Flask(__name__)

# ---- Вспомогательные функции ----
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
    except:
        pass

def add_log(msg):
    logging.info(msg)
    log_messages.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}")
    if len(log_messages) > 100:
        log_messages.pop(0)

def get_free_usdt():
    """Возвращает свободные USDT (с учётом заблокированных)."""
    account = client.account()
    for asset in account['balances']:
        if asset['asset'] == 'USDT':
            free = float(asset['free'])
            locked = float(asset['locked'])
            return free - locked
    return 0.0

def get_current_price():
    ticker = client.ticker_price(SYMBOL)
    return float(ticker['price'])

def get_order_status(order_id):
    try:
        order = client.get_order(symbol=SYMBOL, orderId=order_id)
        return order['status']
    except:
        return None

def recalc_grid():
    """Пересчитывает уровни грида на основе текущих настроек."""
    global grid_levels
    step_price = (UPPER_PRICE - LOWER_PRICE) / (NUM_GRIDS - 1)
    grid_levels = [round(LOWER_PRICE + i * step_price, 2) for i in range(NUM_GRIDS)]
    add_log(f"Грид пересчитан: {grid_levels}")

def init_filters():
    global step_size, min_qty
    info = client.exchange_info()
    for s in info['symbols']:
        if s['symbol'] == SYMBOL:
            filters = {f['filterType']: f for f in s['filters']}
            lot = filters['LOT_SIZE']
            step_size = float(lot['stepSize'])
            min_qty = float(lot['minQty'])
            break

def place_buy(price, qty):
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
        add_log(f"✅ BUY размещён на {price}, qty={qty}")
        send_telegram(f"✅ BUY размещён на {price}, qty={qty}")
    except Exception as e:
        add_log(f"Ошибка BUY на {price}: {e}")

def place_sell(buy_price, qty, sell_price):
    try:
        order = client.new_order(
            symbol=SYMBOL,
            side='SELL',
            type='LIMIT',
            quantity=qty,
            price=sell_price,
            timeInForce='GTC'
        )
        # Сохраняем sell_order_id в позицию
        for pos in buy_positions.values():
            if abs(pos['buy_price'] - buy_price) < 0.01:
                pos['sell_order_id'] = order['orderId']
                break
        add_log(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
        send_telegram(f"💰 SELL размещён на {sell_price} (+{MIN_PROFIT_PERCENT}%)")
    except Exception as e:
        add_log(f"Ошибка SELL на {sell_price}: {e}")

def cancel_all_orders():
    """Отменяет все открытые ордера по паре."""
    try:
        orders = client.get_open_orders(symbol=SYMBOL)
        for o in orders:
            client.cancel_order(symbol=SYMBOL, orderId=o['orderId'])
        active_buy_orders.clear()
        add_log("✅ Все ордера отменены")
        send_telegram("✅ Все ордера отменены")
    except Exception as e:
        add_log(f"Ошибка при отмене ордеров: {e}")

def place_grid():
    """Выставляет BUY ордера на все уровни, которые ниже текущей цены."""
    current = get_current_price()
    free_usdt = get_free_usdt()

    for price in grid_levels:
        if price in active_buy_orders:
            continue
        if price >= current:
            continue
        if free_usdt < INVEST_PER_GRID:
            add_log(f"⚠️ Недостаточно USDT для BUY на {price}, нужно {INVEST_PER_GRID}, доступно {free_usdt}")
            continue

        raw_qty = INVEST_PER_GRID / price
        qty = round(raw_qty // step_size * step_size, 8)
        if qty < min_qty:
            add_log(f"⚠️ Сумма {INVEST_PER_GRID} USDT даёт {qty} BTC (< {min_qty}), пропускаем {price}")
            continue

        place_buy(price, qty)
        free_usdt -= INVEST_PER_GRID

def check_orders():
    """Проверяет статусы ордеров и позиции."""
    # Проверяем BUY ордера
    for price, oid in list(active_buy_orders.items()):
        status = get_order_status(oid)
        if status == 'FILLED':
            order = client.get_order(symbol=SYMBOL, orderId=oid)
            qty = float(order['executedQty'])
            sell_price = round(price * (1 + MIN_PROFIT_PERCENT / 100), 2)
            buy_positions[price] = {
                'buy_price': price,
                'qty': qty,
                'sell_price': sell_price,
                'sell_order_id': None
            }
            del active_buy_orders[price]
            add_log(f"📥 BUY исполнен на {price}, qty={qty}, готовим SELL на {sell_price}")
            send_telegram(f"📥 BUY исполнен на {price}, готовим SELL на {sell_price}")
            # Если цена уже выше цели, сразу продаём
            if get_current_price() >= sell_price:
                place_sell(price, qty, sell_price)
        elif status in ('CANCELLED', 'EXPIRED'):
            del active_buy_orders[price]
            add_log(f"❌ BUY ордер на {price} отменён/истёк")

    # Проверяем позиции
    for buy_price, pos in list(buy_positions.items()):
        if pos['sell_order_id'] is None:
            # Выставляем SELL, если цена >= цели
            current = get_current_price()
            if current >= pos['sell_price']:
                place_sell(buy_price, pos['qty'], pos['sell_price'])
        else:
            status = get_order_status(pos['sell_order_id'])
            if status == 'FILLED':
                profit = round(INVEST_PER_GRID * (MIN_PROFIT_PERCENT / 100), 2)
                add_log(f"🎉 ПРИБЫЛЬ зафиксирована: +{profit} USDT на {pos['sell_price']}")
                send_telegram(f"🎉 Прибыль +{profit}$ на {pos['sell_price']}")
                del buy_positions[buy_price]
            elif status in ('CANCELLED', 'EXPIRED'):
                add_log(f"❌ SELL ордер на {pos['sell_price']} отменён")
                del buy_positions[buy_price]

def bot_loop():
    global bot_running
    init_filters()
    recalc_grid()
    add_log(f"🚀 БОТ ЗАПУЩЕН (REST+WEB) | Диапазон {LOWER_PRICE}–{UPPER_PRICE} | {NUM_GRIDS} грида | {INVEST_PER_GRID}$ на грид | прибыль {MIN_PROFIT_PERCENT}% | интервал {CHECK_INTERVAL} сек")
    send_telegram(f"🚀 Grid Bot с веб-интерфейсом запущен")
    while bot_running:
        try:
            check_orders()
            place_grid()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            add_log(f"Ошибка в боте: {e}")
            time.sleep(10)

# ---- Flask маршруты ----
@app.route('/')
def index():
    # Получаем текущие данные
    current_price = get_current_price()
    free_usdt = get_free_usdt()
    # Собираем список активных BUY ордеров для отображения
    buys = []
    for price, oid in active_buy_orders.items():
        buys.append({'price': price, 'order_id': oid})
    # Позиции
    positions = []
    for pos in buy_positions.values():
        positions.append({
            'buy_price': pos['buy_price'],
            'qty': pos['qty'],
            'sell_price': pos['sell_price'],
            'sell_order_id': pos['sell_order_id'] if pos['sell_order_id'] else 'pending'
        })
    # Логи (последние 20)
    logs = log_messages[-20:]

    html = '''
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
            .grid { display: flex; flex-wrap: wrap; gap: 10px; }
            .grid-item { background: #333; padding: 8px; border-radius: 4px; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { border: 1px solid #0f0; padding: 5px; text-align: left; }
            th { background: #0f0; color: #111; }
            form { display: inline; margin-right: 5px; }
            input, button { background: #222; color: #0f0; border: 1px solid #0f0; padding: 5px; margin: 2px; }
            button:hover { background: #0f0; color: #111; cursor: pointer; }
            a { color: #0f0; text-decoration: none; }
        </style>
    </head>
    <body>
    <div class="container">
        <h1>🤖 Grid Bot Controller</h1>
        <div class="card">
            <h2>📊 Статус</h2>
            <p>Текущая цена BTC: <strong>{{ price }} USDT</strong></p>
            <p>Свободно USDT: <strong>{{ free }} USDT</strong></p>
            <p>Активных BUY ордеров: {{ buys|length }}</p>
            <p>Открытых позиций: {{ positions|length }}</p>
        </div>

        <div class="card">
            <h2>⚙️ Настройки бота</h2>
            <form method="post" action="/update_config">
                <table>
                    <tr><td>Нижняя граница:</td><td><input type="number" step="100" name="lower_price" value="{{ lower }}"></td></tr>
                    <tr><td>Верхняя граница:</td><td><input type="number" step="100" name="upper_price" value="{{ upper }}"></td></tr>
                    <tr><td>Количество гридов:</td><td><input type="number" step="1" name="num_grids" value="{{ num }}"></td></tr>
                    <tr><td>Инвестиция на грид (USDT):</td><td><input type="number" step="5" name="invest" value="{{ invest }}"></td></tr>
                    <tr><td>Профит %:</td><td><input type="number" step="0.1" name="profit" value="{{ profit }}"></td></tr>
                    <tr><td>Интервал (сек):</td><td><input type="number" step="10" name="interval" value="{{ interval }}"></td></tr>
                </table>
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
                <tr><td>{{ p.buy_price }}</td><td>{{ p.qty }}</td><td>{{ p.sell_price }}</td><td>{{ p.sell_order_id }}</td></tr>
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
    return render_template_string(html,
        price=current_price,
        free=free_usdt,
        buys=buys,
        positions=positions,
        lower=LOWER_PRICE,
        upper=UPPER_PRICE,
        num=NUM_GRIDS,
        invest=INVEST_PER_GRID,
        profit=MIN_PROFIT_PERCENT,
        interval=CHECK_INTERVAL,
        logs=logs
    )

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
    # Принудительно размещаем сетку (очищаем активные ордера и выставляем заново)
    cancel_all_orders()
    active_buy_orders.clear()
    place_grid()
    add_log("🔁 Принудительное размещение сетки выполнено")
    send_telegram("🔁 Принудительное размещение сетки выполнено")
    return redirect(url_for('index'))

# ---- Запуск ----
if __name__ == '__main__':
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    # Запускаем веб-сервер
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
