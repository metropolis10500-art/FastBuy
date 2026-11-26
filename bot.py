import os
import sys
import time
import threading
import sqlite3
import logging
from datetime import datetime, timedelta

import telebot
from telebot import types
import requests

# =====================================================================
# ⚙️ НАСТРОЙКИ (ВСТАВЬ СВОИ ДАННЫЕ)
# =====================================================================
BOT_TOKEN = "9291057303:AAHQYZK22NkVMS0XcH1VSgsXTso8yqiWTGI"  # <--- Вставь токен сюда!
ADMIN_ID = 9247170451                                  # <--- Твой Telegram ID (число, узнай в @userinfobot)
BOT_USERNAME = "CashPulseRobot"               # Юзернейм бота без @
SUPPORT_LINK = "https://t.me/vladofix97"      # Линк поддержки

# Данные ЮMoney
YOOMONEY_WALLET = "4100118935779591"
YOOMONEY_TOKEN = "8BE176DEFDE0ED619164EB32845C863D58EDF5A49E9B0416A90FCEE9D298D24D"

# Настройки тарифов бота
REF_L1_REWARD = 12.50
REF_L2_REWARD = 7.20
REF_L3_REWARD = 3.40

DAILY_BONUS = 25.00
MIN_WITHDRAW = 3000.00
AD_PRICE = 5000.00
AD_DURATION_HOURS = 72

DB_PATH = "bot_database.db"

# Пути к картинкам (если файла нет — отправится красивое текстовое меню)
IMAGES = {
    "main": "images/main.jpg",
    "ref": "images/ref.jpg",
    "profile": "images/profile.jpg",
    "withdraw": "images/withdraw.jpg",
    "how": "images/how.jpg",
    "ad": "images/ad.jpg",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================== БАЗА ДАННЫХ (SQLITE3) ==================
def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                balance REAL DEFAULT 0.0,
                pending_balance REAL DEFAULT 0.0,
                paid_balance REAL DEFAULT 0.0,
                earned_ref REAL DEFAULT 0.0,
                earned_bonus REAL DEFAULT 0.0,
                created_at TEXT,
                last_bonus TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                method TEXT,
                requisites TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                label TEXT UNIQUE,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                expires_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ad_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER
            )
        """)
        conn.commit()

init_db()

# Временное хранилище шагов вывода
user_withdraw_data = {}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ================== КЛАВИАТУРЫ ==================
def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🔗 Моя ссылка", callback_data="menu_ref"))
    markup.add(
        types.InlineKeyboardButton("👤 Профиль", callback_data="menu_profile"),
        types.InlineKeyboardButton("🎁 Бонус", callback_data="claim_bonus")
    )
    markup.add(types.InlineKeyboardButton("💸 Вывод средств", callback_data="menu_withdraw"))
    markup.add(
        types.InlineKeyboardButton("📢 Заказать рекламу", callback_data="menu_ad"),
        types.InlineKeyboardButton("❓ Как это работает", callback_data="menu_how")
    )
    markup.add(types.InlineKeyboardButton("🆘 Поддержка", url=SUPPORT_LINK))
    return markup

def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))
    return markup

# ================== ОТПРАВКА МЕНЮ (С КАРТИНКОЙ ИЛИ ТЕКСТОМ) ==================
def send_menu_ui(chat_id, menu_key, text, markup, delete_message_id=None):
    if delete_message_id:
        try:
            bot.delete_message(chat_id, delete_message_id)
        except Exception:
            pass

    img_path = IMAGES.get(menu_key)
    if img_path and os.path.exists(img_path):
        try:
            with open(img_path, "rb") as photo:
                return bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
        except Exception as e:
            logging.error(f"Ошибка загрузки фото {img_path}: {e}")

    return bot.send_message(chat_id, text, reply_markup=markup)

# ================== ЮMONEY ИНТЕГРАЦИЯ ==================
def generate_yoomoney_url(label: str, amount: float = AD_PRICE) -> str:
    target = "Оплата рекламы CashPulse"
    return (
        f"https://yoomoney.ru/quickpay/confirm.xml?"
        f"receiver={YOOMONEY_WALLET}&quickpay-form=shop&targets={target}&"
        f"paymentType=SB&sum={amount}&label={label}"
    )

def check_yoomoney_payment(label: str, min_sum: float = AD_PRICE) -> bool:
    if not YOOMONEY_TOKEN or len(YOOMONEY_TOKEN) < 10:
        return False
    url = "https://yoomoney.ru/api/operation-history"
    headers = {
        "Authorization": f"Bearer {YOOMONEY_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"label": label, "records": 30}
    try:
        r = requests.post(url, headers=headers, data=data, timeout=10)
        if r.status_code == 200:
            res = r.json()
            for op in res.get("operations", []):
                if op.get("label") == label and op.get("status") == "success":
                    if float(op.get("amount", 0)) >= min_sum:
                        return True
    except Exception as e:
        logging.error(f"Ошибка проверки оплаты ЮMoney: {e}")
    return False

# ================== АВТОУДАЛЕНИЕ РЕКЛАМЫ (72 ЧАСА) ==================
def ads_cleanup_worker():
    while True:
        try:
            time.sleep(60)
            now = datetime.utcnow().isoformat()
            with db_conn() as conn:
                c = conn.cursor()
                c.execute("SELECT id, user_id FROM ads WHERE status = 'active' AND expires_at <= ?", (now,))
                expired = c.fetchall()

                for ad_id, u_id in expired:
                    c.execute("SELECT chat_id, message_id FROM ad_messages WHERE ad_id = ?", (ad_id,))
                    msgs = c.fetchall()
                    for chat_id, msg_id in msgs:
                        try:
                            bot.delete_message(chat_id, msg_id)
                        except Exception:
                            pass
                        time.sleep(0.04)

                    c.execute("UPDATE ads SET status = 'expired' WHERE id = ?", (ad_id,))
                    conn.commit()

                    try:
                        bot.send_message(u_id, "⏱ <b>Срок вашей рекламы (72 часа) истек!</b> Все посты удалены.")
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка в автоудалении рекламы: {e}")

threading.Thread(target=ads_cleanup_worker, daemon=True).start()

# ================== НАЧИСЛЕНИЕ РЕФЕРАЛОВ ==================
def process_referral(new_user_id, referrer_id):
    rewards = [(1, REF_L1_REWARD, "1-го"), (2, REF_L2_REWARD, "2-го"), (3, REF_L3_REWARD, "3-го")]
    curr = referrer_id

    with db_conn() as conn:
        c = conn.cursor()
        for level, rew, lvl_name in rewards:
            if not curr:
                break
            c.execute("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (curr,))
            row = c.fetchone()
            if not row:
                break

            c.execute("""
                UPDATE users SET balance = balance + ?, earned_ref = earned_ref + ? WHERE user_id = ?
            """, (rew, rew, curr))
            conn.commit()

            try:
                bot.send_message(curr, f"🎉 <b>Новый реферал {lvl_name} уровня!</b>\nНачислено: <b>+{rew:.2f} ₽</b>")
            except Exception:
                pass

            curr = row[1]

# ================== ОБРАБОТЧИКИ МЕНЮ ==================
def show_main(chat_id, user_id, first_name, delete_msg_id=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        balance = row[0] if row else 0.0

    text = (
        f"👋 Привет, <b>{first_name}</b>!\n\n"
        f"Приглашай друзей по своей ссылке и получай деньги за каждого:\n"
        f"• <b>{REF_L1_REWARD:.2f} ₽</b> — за друга\n"
        f"• <b>{REF_L2_REWARD:.2f} ₽</b> — за друга твоего друга\n"
        f"• <b>{REF_L3_REWARD:.2f} ₽</b> — за друга 3-го уровня\n\n"
        f"💰 Баланс: <b>{balance:.2f} ₽</b>"
    )
    send_menu_ui(chat_id, "main", text, get_main_keyboard(), delete_msg_id)

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Пользователь"
    username = message.from_user.username or ""

    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            r = int(args[1].replace("ref_", ""))
            if r != user_id:
                referrer_id = r
        except ValueError:
            pass

    is_new = False
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            is_new = True
            now = datetime.utcnow().isoformat()
            c.execute("""
                INSERT INTO users (user_id, username, first_name, referrer_id, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, first_name, referrer_id, now))
            conn.commit()

    if is_new and referrer_id:
        process_referral(user_id, referrer_id)

    show_main(message.chat.id, user_id, first_name)

@bot.callback_query_handler(func=lambda call: call.data == "menu_main")
def cb_main(call):
    bot.answer_callback_query(call.id)
    show_main(call.message.chat.id, call.from_user.id, call.from_user.first_name, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_ref")
def cb_ref(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
        total_friends = c.fetchone()[0]
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%"))
        today_friends = c.fetchone()[0]

    text = (
        f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"За каждого друга, который запустит бота по ссылке, — <b>{REF_L1_REWARD:.2f} ₽</b> и ещё по цепочке:\n"
        f"• 2 уровень (друзья друзей) — <b>{REF_L2_REWARD:.2f} ₽</b>\n"
        f"• 3 уровень — <b>{REF_L3_REWARD:.2f} ₽</b>\n\n"
        f"👥 Друзей: <b>{total_friends}</b> (сегодня +{today_friends})\n\n"
        f"Поделитесь ссылкой в чатах, с друзьями или в своём канале 👇"
    )
    share_url = f"https://t.me/share/url?url={ref_link}&text=Зарабатывай+деньги+в+Telegram!"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📨 Поделиться ссылкой", url=share_url))
    markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))
    send_menu_ui(call.message.chat.id, "ref", text, markup, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_profile")
def cb_profile(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT balance, pending_balance, paid_balance, earned_ref, earned_bonus, created_at FROM users WHERE user_id = ?", (user_id,))
        u = c.fetchone()
        if not u:
            return

        balance, pending, paid, earned_ref, earned_bonus, created_at = u
        reg_date = str(created_at).split("T")[0] if created_at else "Недавно"
        total_earned = earned_ref + earned_bonus

        c.execute("SELECT user_id FROM users WHERE referrer_id = ?", (user_id,))
        l1_ids = [r[0] for r in c.fetchall()]
        l1_count = len(l1_ids)

        l2_ids = []
        if l1_ids:
            q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l1_ids))})"
            c.execute(q, tuple(l1_ids))
            l2_ids = [r[0] for r in c.fetchall()]
        l2_count = len(l2_ids)

        l3_ids = []
        if l2_ids:
            q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l2_ids))})"
            c.execute(q, tuple(l2_ids))
            l3_ids = [r[0] for r in c.fetchall()]
        l3_count = len(l3_ids)

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%"))
        today_friends = c.fetchone()[0]

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 В боте с {reg_date}\n\n"
        f"💰 Баланс: <b>{balance:.2f} ₽</b>\n"
        f"⏳ На выводе: <b>{pending:.2f} ₽</b>\n"
        f"✅ Выплачено: <b>{paid:.2f} ₽</b>\n\n"
        f"📈 Заработано всего: <b>{total_earned:.2f} ₽</b>\n"
        f"• за друзей: <b>{earned_ref:.2f} ₽</b>\n"
        f"• за бонусы: <b>{earned_bonus:.2f} ₽</b>\n\n"
        f"👥 Друзья (сегодня +{today_friends}):\n"
        f"1 уровень: <b>{l1_count}</b> — <b>{l1_count * REF_L1_REWARD:.2f} ₽</b>\n"
        f"2 уровень: <b>{l2_count}</b> — <b>{l2_count * REF_L2_REWARD:.2f} ₽</b>\n"
        f"3 уровень: <b>{l3_count}</b> — <b>{l3_count * REF_L3_REWARD:.2f} ₽</b>"
    )
    send_menu_ui(call.message.chat.id, "profile", text, get_back_keyboard(), call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "claim_bonus")
def cb_bonus(call):
    user_id = call.from_user.id
    now = datetime.utcnow()

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT last_bonus FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        last_bonus = row[0] if row else None

        if last_bonus:
            lb_time = datetime.fromisoformat(last_bonus)
            if now - lb_time < timedelta(hours=24):
                diff = timedelta(hours=24) - (now - lb_time)
                h, rem = divmod(int(diff.total_seconds()), 3600)
                m, _ = divmod(rem, 60)
                return bot.answer_callback_query(call.id, f"⏳ Бонус уже получен! Следующий через {h} ч. {m} мин.", show_alert=True)

        c.execute("""
            UPDATE users SET balance = balance + ?, earned_bonus = earned_bonus + ?, last_bonus = ? WHERE user_id = ?
        """, (DAILY_BONUS, DAILY_BONUS, now.isoformat(), user_id))
        conn.commit()

    bot.answer_callback_query(call.id, f"🎉 Вы получили ежедневный бонус {DAILY_BONUS:.2f} ₽!", show_alert=True)
    show_main(call.message.chat.id, user_id, call.from_user.first_name, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_withdraw")
def cb_withdraw(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT balance, pending_balance FROM users WHERE user_id = ?", (user_id,))
        balance, pending = c.fetchone()
        c.execute("SELECT id, amount, status FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        last_w = c.fetchone()

    last_app_text = ""
    if last_w:
        status_icon = "⏳ в обработке" if last_w[2] == "pending" else ("✅ выплачено" if last_w[2] == "paid" else "❌ отменено")
        last_app_text = f"Последние заявки:\n#{last_w[0]} • <b>{last_w[1]:.2f} ₽</b> • {status_icon}\n\n"

    base_text = (
        f"💸 <b>Вывод средств</b>\n\n"
        f"💰 Доступно: <b>{balance:.2f} ₽</b>\n"
        f"⏳ На выводе: <b>{pending:.2f} ₽</b>\n\n"
        f"Минимальная сумма — <b>{MIN_WITHDRAW:.0f} ₽</b>. Без комиссий.\n"
        f"Способы: банковская карта или СБП.\n\n"
        f"{last_app_text}"
    )

    markup = types.InlineKeyboardMarkup()
    if balance < MIN_WITHDRAW:
        diff = MIN_WITHDRAW - balance
        text = base_text + f"🔒 До вывода не хватает <b>{diff:.2f} ₽</b> — приглашайте друзей!"
        markup.add(types.InlineKeyboardButton("🔗 Моя ссылка", callback_data="menu_ref"))
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))
    else:
        text = base_text + "Выберите способ вывода средств 👇"
        markup.add(
            types.InlineKeyboardButton("💳 На карту", callback_data="w_method:card"),
            types.InlineKeyboardButton("⚡ Через СБП", callback_data="w_method:sbp")
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))

    send_menu_ui(call.message.chat.id, "withdraw", text, markup, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("w_method:"))
def cb_withdraw_select(call):
    bot.answer_callback_query(call.id)
    method = call.data.split(":")[1]
    name = "💳 Банковская карта" if method == "card" else "⚡ СБП"
    user_withdraw_data[call.from_user.id] = {"method": name}

    msg = bot.send_message(call.message.chat.id, f"Вы выбрали: <b>{name}</b>\n\nВведите сумму (минимум {MIN_WITHDRAW:.0f} ₽):")
    bot.register_next_step_handler(msg, step_withdraw_amount)

def step_withdraw_amount(message):
    user_id = message.from_user.id
    try:
        amount = float(message.text.replace(",", "."))
    except (ValueError, AttributeError):
        msg = bot.send_message(message.chat.id, "⚠️ Введите число (например 3000):")
        return bot.register_next_step_handler(msg, step_withdraw_amount)

    if amount < MIN_WITHDRAW:
        msg = bot.send_message(message.chat.id, f"⚠️ Минимальная сумма вывода {MIN_WITHDRAW:.0f} ₽:")
        return bot.register_next_step_handler(msg, step_withdraw_amount)

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance = c.fetchone()[0]

    if amount > balance:
        return bot.send_message(message.chat.id, f"⚠️ Недостаточно средств. Баланс: <b>{balance:.2f} ₽</b>")

    user_withdraw_data[user_id]["amount"] = amount
    msg = bot.send_message(message.chat.id, "Введите ваши реквизиты (номер карты или телефон/банк для СБП):")
    bot.register_next_step_handler(msg, step_withdraw_details)

def step_withdraw_details(message):
    user_id = message.from_user.id
    details = message.text
    data = user_withdraw_data.get(user_id, {})
    amount = data.get("amount", 0)
    method = data.get("method", "Карта")

    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance - ?, pending_balance = pending_balance + ? WHERE user_id = ?", (amount, amount, user_id))
        c.execute("INSERT INTO withdrawals (user_id, amount, method, requisites, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (user_id, amount, method, details, now))
        w_id = c.lastrowid
        conn.commit()

    bot.send_message(message.chat.id, f"✅ <b>Заявка #{w_id} на {amount:.2f} ₽ создана!</b>\nСтатус: ⏳ В обработке")

    if ADMIN_ID > 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Выплачено", callback_data=f"adm_pay:{w_id}"),
            types.InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_reject:{w_id}")
        )
        try:
            bot.send_message(
                ADMIN_ID,
                f"🚨 <b>Заявка на вывод #{w_id}!</b>\nID: <code>{user_id}</code> (@{message.from_user.username})\nСумма: <b>{amount:.2f} ₽</b>\nМетод: {method}\nРеквизиты: <code>{details}</code>",
                reply_markup=markup
            )
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_pay:"))
def cb_adm_pay(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    w_id = int(call.data.split(":")[1])
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,))
        row = c.fetchone()
        if not row or row[2] != 'pending':
            return bot.answer_callback_query(call.id, "Уже обработано!", show_alert=True)
        u_id, amount, _ = row
        c.execute("UPDATE users SET pending_balance = pending_balance - ?, paid_balance = paid_balance + ? WHERE user_id = ?", (amount, amount, u_id))
        c.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (w_id,))
        conn.commit()

    bot.edit_message_text(call.message.text + "\n\n🟢 <b>ВЫПЛАЧЕНО</b>", call.message.chat.id, call.message.message_id)
    try:
        bot.send_message(u_id, f"✅ Ваша заявка #{w_id} на сумму {amount:.2f} ₽ успешно выплачена!")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_reject:"))
def cb_adm_reject(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    w_id = int(call.data.split(":")[1])
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,))
        row = c.fetchone()
        if not row or row[2] != 'pending':
            return bot.answer_callback_query(call.id, "Уже обработано!", show_alert=True)
        u_id, amount, _ = row
        c.execute("UPDATE users SET pending_balance = pending_balance - ?, balance = balance + ? WHERE user_id = ?", (amount, amount, u_id))
        c.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (w_id,))
        conn.commit()

    bot.edit_message_text(call.message.text + "\n\n🔴 <b>ОТКЛОНЕНО</b>", call.message.chat.id, call.message.message_id)
    try:
        bot.send_message(u_id, f"❌ Заявка #{w_id} на {amount:.2f} ₽ отклонена. Средства возвращены на баланс.")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "menu_how")
def cb_how(call):
    bot.answer_callback_query(call.id)
    text = (
        f"❓ <b>Как это работает</b>\n\n"
        f"1️⃣ Берёте ссылку в разделе «🔗 Моя ссылка»\n"
        f"2️⃣ Отправляете её друзьям или в каналы\n"
        f"3️⃣ Зарабатываете по цепочке:\n"
        f"• <b>{REF_L1_REWARD:.2f} ₽</b> за друга\n"
        f"• <b>{REF_L2_REWARD:.2f} ₽</b> за 2-й уровень\n"
        f"• <b>{REF_L3_REWARD:.2f} ₽</b> за 3-й уровень\n"
        f"4️⃣ Ежедневный бонус: <b>{DAILY_BONUS:.0f} ₽</b> в разделе «🎁 Бонус»!\n\n"
        f"💸 Вывод от {MIN_WITHDRAW:.0f} ₽ на Карту или СБП."
    )
    send_menu_ui(call.message.chat.id, "how", text, get_back_keyboard(), call.message.message_id)

# ================== ЗАКАЗ РЕКЛАМЫ (ЮMONEY + АВТОУДАЛЕНИЕ) ==================
@bot.callback_query_handler(func=lambda call: call.data == "menu_ad")
def cb_ad_menu(call):
    bot.answer_callback_query(call.id)
    text = (
        f"📢 <b>Заказ рекламы</b>\n\n"
        f"• Охват: Рассылка всем пользователям бота\n"
        f"• Срок: <b>72 часа</b> (после удаляется ботом)\n"
        f"• Цена: <b>{AD_PRICE:.0f} ₽</b>\n\n"
        f"Отправьте рекламный пост прямо сюда (текст, фото или пересылку):"
    )
    send_menu_ui(call.message.chat.id, "ad", text, get_back_keyboard(), call.message.message_id)
    bot.register_next_step_handler(call.message, step_process_ad)

def step_process_ad(message):
    user_id = message.from_user.id
    label = f"ad_{user_id}_{int(datetime.utcnow().timestamp())}"
    now = datetime.utcnow().isoformat()

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO ads (user_id, label, source_chat_id, source_message_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (user_id, label, message.chat.id, message.message_id, now))
        ad_id = c.lastrowid
        conn.commit()

    pay_url = generate_yoomoney_url(label, AD_PRICE)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"💳 Оплатить {AD_PRICE:.0f} ₽", url=pay_url))
    markup.add(types.InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_ad:{ad_id}"))
    markup.add(types.InlineKeyboardButton("◀️ Главное меню", callback_data="menu_main"))

    bot.reply_to(message, f"✅ <b>Рекламный пост принят (Заказ #{ad_id})</b>\n\nОплатите счет и нажмите кнопку ниже.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_ad:"))
def cb_check_ad(call):
    ad_id = int(call.data.split(":")[1])
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT label, status, source_chat_id, source_message_id FROM ads WHERE id = ?", (ad_id,))
        row = c.fetchone()
        if not row:
            return bot.answer_callback_query(call.id, "Заказ не найден!", show_alert=True)
        label, status, s_chat_id, s_msg_id = row

    if status == 'active':
        return bot.answer_callback_query(call.id, "Этот заказ уже оплачен и запущен!", show_alert=True)

    bot.answer_callback_query(call.id, "Проверяем оплату в ЮMoney...")
    if not check_yoomoney_payment(label, AD_PRICE):
        return bot.send_message(call.message.chat.id, "❌ Оплата еще не поступила. Подождите 1-2 минуты и нажмите снова.")

    expires_at = (datetime.utcnow() + timedelta(hours=AD_DURATION_HOURS)).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE ads SET status = 'active', expires_at = ? WHERE id = ?", (expires_at, ad_id))
        c.execute("SELECT user_id FROM users")
        users = c.fetchall()
        conn.commit()

    bot.send_message(call.message.chat.id, "🎉 <b>Оплата подтверждена! Запуск рассылки...</b>")
    count = 0
    with db_conn() as conn:
        c = conn.cursor()
        for (u_id,) in users:
            try:
                sent = bot.copy_message(chat_id=u_id, from_chat_id=s_chat_id, message_id=s_msg_id)
                c.execute("INSERT INTO ad_messages (ad_id, chat_id, message_id) VALUES (?, ?, ?)", (ad_id, u_id, sent.message_id))
                count += 1
                time.sleep(0.04)
            except Exception:
                pass
        conn.commit()

    bot.send_message(call.message.chat.id, f"🚀 Разослано {count} пользователям. Сообщения удалятся через 72 часа.")

# ================== СТАРТ ==================
if __name__ == "__main__":
    if "СЮДА_ВСТАВЬ" in BOT_TOKEN or len(BOT_TOKEN) < 20:
        logging.error("ОШИБКА: ТЫ НЕ ВСТАВИЛ ТОКЕН БОТА В main.py (СТРОКА 18)!")
        while True:
            time.sleep(10)

    logging.info("✅ Бот успешно запустился и слушает входящие запросы!")
    bot.infinity_polling(skip_pending=True)
