import os
import sys
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    CallbackQuery,
    Message
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

# =====================================================================
# ⚙️ ОСНОВНЫЕ НАСТРОЙКИ (ВСТАВЬ СВОИ ДАННЫЕ СЮДА)
# =====================================================================
BOT_TOKEN = "9291057303:AAHQYZK22NkVMS0XcH1VSgsXTso8yqiWTGI"  # <--- ВСТАВЬ ТОКЕН СЮДА!
ADMIN_ID = 9247170451                                  # <--- Твой числовой ID (узнать в @userinfobot)
BOT_USERNAME = "CashPulseRobot"               # Юзернейм твоего бота без @
SUPPORT_LINK = "https://t.me/vladofix97"      # Твой линк на саппорт

# Данные ЮMoney (для оплаты рекламы 5000 руб)
YOOMONEY_WALLET = "4100118935779591"
YOOMONEY_TOKEN = "8BE176DEFDE0ED619164EB32845C863D58EDF5A49E9B0416A90FCEE9D298D24D"

# Настройки тарифов
REF_L1_REWARD = 12.50
REF_L2_REWARD = 7.20
REF_L3_REWARD = 3.40

DAILY_BONUS = 25.00
MIN_WITHDRAW = 3000.00
AD_PRICE = 5000.00
AD_DURATION_HOURS = 72

DB_PATH = "bot_database.db"

# Пути к картинкам меню (если картинки нет - бот отправит аккуратный текст)
IMAGES = {
    "main": "images/main.jpg",
    "ref": "images/ref.jpg",
    "profile": "images/profile.jpg",
    "withdraw": "images/withdraw.jpg",
    "how": "images/how.jpg",
    "ad": "images/ad.jpg",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================== РАБОТА С БАЗОЙ ДАННЫХ (SQLITE3) ==================
def db_query(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=False):
    """Синхронный хелпер для встроенного SQLite"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        if commit:
            conn.commit()
        if fetchone:
            return cursor.fetchone()
        if fetchall:
            return cursor.fetchall()
        return cursor.lastrowid

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
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
        cursor.execute("""
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
        cursor.execute("""
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ad_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER
            )
        """)
        conn.commit()

# ================== FSM СОСТОЯНИЯ ==================
class WithdrawStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_details = State()

class AdStates(StatesGroup):
    waiting_for_post = State()

# ================== КЛАВИАТУРЫ ==================
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="menu_ref")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton(text="🎁 Бонус", callback_data="claim_bonus")
        ],
        [InlineKeyboardButton(text="💸 Вывод средств", callback_data="menu_withdraw")],
        [
            InlineKeyboardButton(text="📢 Заказать рекламу", callback_data="menu_ad"),
            InlineKeyboardButton(text="❓ Как это работает", callback_data="menu_how")
        ],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_LINK)]
    ])

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")]
    ])

# ================== ОТПРАВКА МЕНЮ ==================
async def send_menu_ui(bot: Bot, chat_id: int, menu_key: str, text: str, reply_markup: InlineKeyboardMarkup, delete_message_id: Optional[int] = None):
    if delete_message_id:
        try:
            await bot.delete_message(chat_id, delete_message_id)
        except Exception:
            pass

    img_path = IMAGES.get(menu_key)
    if img_path and os.path.exists(img_path):
        try:
            photo = FSInputFile(img_path)
            return await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка загрузки фото {img_path}: {e}")

    return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")

# ================== ИНТЕГРАЦИЯ ЮMONEY ==================
def generate_yoomoney_url(label: str, amount: float = AD_PRICE) -> str:
    target = "Оплата рекламы CashPulse"
    return (
        f"https://yoomoney.ru/quickpay/confirm.xml?"
        f"receiver={YOOMONEY_WALLET}&quickpay-form=shop&targets={target}&"
        f"paymentType=SB&sum={amount}&label={label}"
    )

async def check_yoomoney_payment(label: str, min_sum: float = AD_PRICE) -> bool:
    if not YOOMONEY_TOKEN or len(YOOMONEY_TOKEN) < 10:
        return False
    import aiohttp
    url = "https://yoomoney.ru/api/operation-history"
    headers = {
        "Authorization": f"Bearer {YOOMONEY_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"label": label, "records": 30}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    for op in res.get("operations", []):
                        if op.get("label") == label and op.get("status") == "success":
                            if float(op.get("amount", 0)) >= min_sum:
                                return True
    except Exception as e:
        logging.error(f"Ошибка ЮMoney: {e}")
    return False

# ================== АВТОУДАЛЕНИЕ РЕКЛАМЫ (72 ЧАСА) ==================
async def ads_cleanup_loop(bot: Bot):
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.utcnow().isoformat()
            expired_ads = db_query("SELECT id, user_id FROM ads WHERE status = 'active' AND expires_at <= ?", (now,), fetchall=True)

            for ad_id, user_id in expired_ads:
                msgs = db_query("SELECT chat_id, message_id FROM ad_messages WHERE ad_id = ?", (ad_id,), fetchall=True)
                for c_id, m_id in msgs:
                    try:
                        await bot.delete_message(chat_id=c_id, message_id=m_id)
                    except Exception:
                        pass
                    await asyncio.sleep(0.04)

                db_query("UPDATE ads SET status = 'expired' WHERE id = ?", (ad_id,), commit=True)
                try:
                    await bot.send_message(user_id, "⏱ <b>Срок действия рекламы (72ч) истек!</b> Сообщения удалены.", parse_mode="HTML")
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Ошибка в автоудалении рекламы: {e}")

# ================== РЕФЕРАЛЫ ==================
async def process_referral(bot: Bot, new_user_id: int, referrer_id: int):
    rewards = [(1, REF_L1_REWARD, "1-го"), (2, REF_L2_REWARD, "2-го"), (3, REF_L3_REWARD, "3-го")]
    curr = referrer_id

    for level, rew, lvl_name in rewards:
        if not curr:
            break
        row = db_query("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (curr,), fetchone=True)
        if not row:
            break

        db_query("""
            UPDATE users SET balance = balance + ?, earned_ref = earned_ref + ? WHERE user_id = ?
        """, (rew, rew, curr), commit=True)

        try:
            await bot.send_message(curr, f"🎉 <b>Новый реферал {lvl_name} уровня!</b>\nНачислено: <b>+{rew:.2f} ₽</b>", parse_mode="HTML")
        except Exception:
            pass

        curr = row[1]

# ================== ОБРАБОТЧИКИ БОТА ==================
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
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

    user = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    is_new = False
    if not user:
        is_new = True
        now = datetime.utcnow().isoformat()
        db_query("""
            INSERT INTO users (user_id, username, first_name, referrer_id, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, first_name, referrer_id, now), commit=True)

    if is_new and referrer_id:
        await process_referral(bot, user_id, referrer_id)

    await show_main_menu(bot, message.chat.id, user_id, first_name)

async def show_main_menu(bot: Bot, chat_id: int, user_id: int, first_name: str, delete_msg_id: Optional[int] = None):
    row = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    balance = row[0] if row else 0.0

    text = (
        f"👋 Привет, <b>{first_name}</b>!\n\n"
        f"Приглашай друзей по своей ссылке и получай деньги за каждого:\n"
        f"• <b>{REF_L1_REWARD:.2f} ₽</b> — за друга\n"
        f"• <b>{REF_L2_REWARD:.2f} ₽</b> — за друга твоего друга\n"
        f"• <b>{REF_L3_REWARD:.2f} ₽</b> — за друга 3-го уровня\n\n"
        f"💰 Баланс: <b>{balance:.2f} ₽</b>"
    )
    await send_menu_ui(bot, chat_id, "main", text, get_main_keyboard(), delete_msg_id)

@dp.callback_query(F.data == "menu_main")
async def cb_main(call: CallbackQuery, bot: Bot):
    await call.answer()
    await show_main_menu(bot, call.message.chat.id, call.from_user.id, call.from_user.first_name, call.message.message_id)

@dp.callback_query(F.data == "menu_ref")
async def cb_ref(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    total_friends = db_query("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,), fetchone=True)[0]
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_friends = db_query("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%"), fetchone=True)[0]

    text = (
        f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"За каждого друга, который запустит бота по ссылке, — <b>{REF_L1_REWARD:.2f} ₽</b> и ещё по цепочке:\n"
        f"• 2 уровень (друзья друзей) — <b>{REF_L2_REWARD:.2f} ₽</b>\n"
        f"• 3 уровень — <b>{REF_L3_REWARD:.2f} ₽</b>\n\n"
        f"👥 Друзей: <b>{total_friends}</b> (сегодня +{today_friends})\n\n"
        f"Поделитесь ссылкой в чатах, с друзьями или в своём канале 👇"
    )
    share_url = f"https://t.me/share/url?url={ref_link}&text=Зарабатывай+в+Telegram!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")]
    ])
    await send_menu_ui(bot, call.message.chat.id, "ref", text, kb, call.message.message_id)

@dp.callback_query(F.data == "menu_profile")
async def cb_profile(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id
    u = db_query("SELECT balance, pending_balance, paid_balance, earned_ref, earned_bonus, created_at FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not u:
        return

    balance, pending, paid, earned_ref, earned_bonus, created_at = u
    reg_date = str(created_at).split("T")[0] if created_at else "Недавно"
    total_earned = earned_ref + earned_bonus

    l1_ids = [r[0] for r in db_query("SELECT user_id FROM users WHERE referrer_id = ?", (user_id,), fetchall=True)]
    l1_count = len(l1_ids)

    l2_ids = []
    if l1_ids:
        q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l1_ids))})"
        l2_ids = [r[0] for r in db_query(q, tuple(l1_ids), fetchall=True)]
    l2_count = len(l2_ids)

    l3_ids = []
    if l2_ids:
        q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l2_ids))})"
        l3_ids = [r[0] for r in db_query(q, tuple(l2_ids), fetchall=True)]
    l3_count = len(l3_ids)

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_friends = db_query("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%"), fetchone=True)[0]

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
        f"1 уровень: <b>{l1_count}</b> — заработано <b>{l1_count * REF_L1_REWARD:.2f} ₽</b>\n"
        f"2 уровень: <b>{l2_count}</b> — заработано <b>{l2_count * REF_L2_REWARD:.2f} ₽</b>\n"
        f"3 уровень: <b>{l3_count}</b> — заработано <b>{l3_count * REF_L3_REWARD:.2f} ₽</b>"
    )
    await send_menu_ui(bot, call.message.chat.id, "profile", text, get_back_keyboard(), call.message.message_id)

@dp.callback_query(F.data == "claim_bonus")
async def cb_bonus(call: CallbackQuery):
    user_id = call.from_user.id
    now = datetime.utcnow()
    row = db_query("SELECT last_bonus FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    last_bonus = row[0] if row else None

    if last_bonus:
        lb_time = datetime.fromisoformat(last_bonus)
        if now - lb_time < timedelta(hours=24):
            diff = timedelta(hours=24) - (now - lb_time)
            h, rem = divmod(int(diff.total_seconds()), 3600)
            m, _ = divmod(rem, 60)
            return await call.answer(f"⏳ Бонус уже получен! Следующий через {h} ч. {m} мин.", show_alert=True)

    db_query("""
        UPDATE users SET balance = balance + ?, earned_bonus = earned_bonus + ?, last_bonus = ? WHERE user_id = ?
    """, (DAILY_BONUS, DAILY_BONUS, now.isoformat(), user_id), commit=True)

    await call.answer(f"🎉 Вы получили ежедневный бонус {DAILY_BONUS:.2f} ₽!", show_alert=True)
    await show_main_menu(call.bot, call.message.chat.id, user_id, call.from_user.first_name, call.message.message_id)

@dp.callback_query(F.data == "menu_withdraw")
async def cb_withdraw(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id
    balance, pending = db_query("SELECT balance, pending_balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    last_w = db_query("SELECT id, amount, status FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,), fetchone=True)

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

    if balance < MIN_WITHDRAW:
        diff = MIN_WITHDRAW - balance
        text = base_text + f"🔒 До вывода не хватает <b>{diff:.2f} ₽</b> — приглашайте друзей!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="menu_ref")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")]
        ])
    else:
        text = base_text + "Выберите способ вывода средств 👇"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 На карту", callback_data="w_method:card"),
                InlineKeyboardButton(text="⚡ Через СБП", callback_data="w_method:sbp")
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")]
        ])

    await send_menu_ui(bot, call.message.chat.id, "withdraw", text, kb, call.message.message_id)

@dp.callback_query(F.data.startswith("w_method:"))
async def cb_withdraw_select(call: CallbackQuery, state: FSMContext):
    await call.answer()
    method = call.data.split(":")[1]
    name = "💳 Банковская карта" if method == "card" else "⚡ СБП"
    await state.update_data(method=name)
    await state.set_state(WithdrawStates.waiting_for_amount)
    await call.message.answer(f"Вы выбрали: <b>{name}</b>\n\nВведите сумму (минимум {MIN_WITHDRAW:.0f} ₽):", parse_mode="HTML")

@dp.message(WithdrawStates.waiting_for_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("⚠️ Введите число (например 3000):")

    if amount < MIN_WITHDRAW:
        return await message.answer(f"⚠️ Минимальная сумма вывода {MIN_WITHDRAW:.0f} ₽:")

    balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
    if amount > balance:
        return await message.answer(f"⚠️ Недостаточно средств. Баланс: <b>{balance:.2f} ₽</b>", parse_mode="HTML")

    await state.update_data(amount=amount)
    await state.set_state(WithdrawStates.waiting_for_details)
    await message.answer("Введите ваши реквизиты (номер карты или телефон/банк для СБП):")

@dp.message(WithdrawStates.waiting_for_details)
async def process_withdraw_details(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    details = message.text
    data = await state.get_data()
    amount, method = data["amount"], data["method"]
    await state.clear()

    now = datetime.utcnow().isoformat()
    db_query("UPDATE users SET balance = balance - ?, pending_balance = pending_balance + ? WHERE user_id = ?", (amount, amount, user_id), commit=True)
    w_id = db_query("INSERT INTO withdrawals (user_id, amount, method, requisites, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (user_id, amount, method, details, now), commit=True)

    await message.answer(f"✅ <b>Заявка #{w_id} на {amount:.2f} ₽ создана!</b>\nСтатус: ⏳ В обработке", parse_mode="HTML")

    if ADMIN_ID > 0:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплачено", callback_data=f"adm_pay:{w_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject:{w_id}")
            ]
        ])
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚨 <b>Заявка на вывод #{w_id}!</b>\nID: <code>{user_id}</code> (@{message.from_user.username})\nСумма: <b>{amount:.2f} ₽</b>\nМетод: {method}\nРеквизиты: <code>{details}</code>",
                reply_markup=admin_kb,
                parse_mode="HTML"
            )
        except Exception:
            pass

@dp.callback_query(F.data.startswith("adm_pay:"))
async def cb_admin_pay(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет доступа", show_alert=True)
    w_id = int(call.data.split(":")[1])
    row = db_query("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,), fetchone=True)
    if not row or row[2] != 'pending':
        return await call.answer("Уже обработано!", show_alert=True)
    u_id, amount, _ = row
    db_query("UPDATE users SET pending_balance = pending_balance - ?, paid_balance = paid_balance + ? WHERE user_id = ?", (amount, amount, u_id), commit=True)
    db_query("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (w_id,), commit=True)
    await call.message.edit_text(call.message.text + "\n\n🟢 <b>ВЫПЛАЧЕНО</b>", parse_mode="HTML")
    try:
        await bot.send_message(u_id, f"✅ Ваша заявка #{w_id} на сумму {amount:.2f} ₽ успешно выплачена!", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm_reject:"))
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет доступа", show_alert=True)
    w_id = int(call.data.split(":")[1])
    row = db_query("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,), fetchone=True)
    if not row or row[2] != 'pending':
        return await call.answer("Уже обработано!", show_alert=True)
    u_id, amount, _ = row
    db_query("UPDATE users SET pending_balance = pending_balance - ?, balance = balance + ? WHERE user_id = ?", (amount, amount, u_id), commit=True)
    db_query("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (w_id,), commit=True)
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>ОТКЛОНЕНО</b>", parse_mode="HTML")
    try:
        await bot.send_message(u_id, f"❌ Заявка #{w_id} на {amount:.2f} ₽ отклонена. Средства возвращены на баланс.", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "menu_how")
async def cb_how(call: CallbackQuery, bot: Bot):
    await call.answer()
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
    await send_menu_ui(bot, call.message.chat.id, "how", text, get_back_keyboard(), call.message.message_id)

@dp.callback_query(F.data == "menu_ad")
async def cb_ad_menu(call: CallbackQuery, bot: Bot, state: FSMContext):
    await call.answer()
    text = (
        f"📢 <b>Заказ рекламы</b>\n\n"
        f"• Охват: Рассылка всем пользователям бота\n"
        f"• Срок: <b>72 часа</b> (после удаляется ботом)\n"
        f"• Цена: <b>{AD_PRICE:.0f} ₽</b>\n\n"
        f"Отправьте рекламный пост прямо сюда (текст, фото или пересылку):"
    )
    await state.set_state(AdStates.waiting_for_post)
    await send_menu_ui(bot, call.message.chat.id, "ad", text, get_back_keyboard(), call.message.message_id)

@dp.message(AdStates.waiting_for_post)
async def process_ad_post(message: Message, state: FSMContext):
    user_id = message.from_user.id
    label = f"ad_{user_id}_{int(datetime.utcnow().timestamp())}"
    now = datetime.utcnow().isoformat()
    ad_id = db_query("INSERT INTO ads (user_id, label, source_chat_id, source_message_id, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (user_id, label, message.chat.id, message.message_id, now), commit=True)
    await state.clear()

    pay_url = generate_yoomoney_url(label, AD_PRICE)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {AD_PRICE:.0f} ₽", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_ad:{ad_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="menu_main")]
    ])
    await message.reply(f"✅ <b>Рекламный пост принят (Заказ #{ad_id})</b>\n\nОплатите счет и нажмите кнопку «Проверить оплату».", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("check_ad:"))
async def cb_check_ad(call: CallbackQuery, bot: Bot):
    ad_id = int(call.data.split(":")[1])
    row = db_query("SELECT label, status, source_chat_id, source_message_id FROM ads WHERE id = ?", (ad_id,), fetchone=True)
    if not row:
        return await call.answer("Заказ не найден!", show_alert=True)
    label, status, s_chat_id, s_msg_id = row

    if status == 'active':
        return await call.answer("Этот заказ уже запущен!", show_alert=True)

    await call.answer("Проверяем платеж...")
    if not await check_yoomoney_payment(label, AD_PRICE):
        return await call.message.answer("❌ Оплата еще не поступила в ЮMoney. Подождите пару минут.", parse_mode="HTML")

    expires_at = (datetime.utcnow() + timedelta(hours=AD_DURATION_HOURS)).isoformat()
    db_query("UPDATE ads SET status = 'active', expires_at = ? WHERE id = ?", (expires_at, ad_id), commit=True)
    users = db_query("SELECT user_id FROM users", fetchall=True)

    await call.message.answer("🎉 <b>Оплата подтверждена! Запуск рассылки...</b>", parse_mode="HTML")
    count = 0
    for (u_id,) in users:
        try:
            sent = await bot.copy_message(chat_id=u_id, from_chat_id=s_chat_id, message_id=s_msg_id)
            db_query("INSERT INTO ad_messages (ad_id, chat_id, message_id) VALUES (?, ?, ?)", (ad_id, u_id, sent.message_id), commit=True)
            count += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await call.message.answer(f"🚀 Разослано {count} пользователям. Сообщения удалятся через 72 часа.")

# ================== СТАРТ ==================
async def main():
    if "СЮДА_ВСТАВЬ" in BOT_TOKEN or len(BOT_TOKEN) < 20:
        logging.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        logging.error("ОШИБКА: ТЫ НЕ ВСТАВИЛ ТОКЕН БОТА В main.py (СТРОКА 17)!")
        logging.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        # Не закрываем процесс сразу, чтобы контейнер не падал в failed и ты видел лог
        while True:
            await asyncio.sleep(10)

    init_db()
    bot = Bot(token=BOT_TOKEN)
    asyncio.create_task(ads_cleanup_loop(bot))
    logging.info("✅ CashPulse успешно запущен и работает!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}")
        # Задержка при ошибке, чтобы контейнер не падал мгновенно
        import time
        time.sleep(30)
