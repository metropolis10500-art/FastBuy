import os
import sys
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
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
import aiosqlite
import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ================== НАСТРОЙКИ И КОНФИГУРАЦИЯ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "CashPulseRobot")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/vladofix97")

YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "")
YOOMONEY_TOKEN = os.getenv("YOOMONEY_TOKEN", "")

# Цены рефералов (чуть выше оригинала)
REF_L1_REWARD = 12.50
REF_L2_REWARD = 7.20
REF_L3_REWARD = 3.40

DAILY_BONUS = 25.00
MIN_WITHDRAW = 3000.00
AD_PRICE = 5000.00
AD_DURATION_HOURS = 72

DB_PATH = "cashpulse.db"

# Пути к изображениям меню
IMAGES = {
    "main": os.getenv("IMG_MAIN", "images/main.jpg"),
    "ref": os.getenv("IMG_REF", "images/ref.jpg"),
    "profile": os.getenv("IMG_PROFILE", "images/profile.jpg"),
    "withdraw": os.getenv("IMG_WITHDRAW", "images/withdraw.jpg"),
    "how": os.getenv("IMG_HOW", "images/how.jpg"),
    "ad": os.getenv("IMG_AD", "images/ad.jpg"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================== FSM СОСТОЯНИЯ ==================
class WithdrawStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_details = State()

class AdStates(StatesGroup):
    waiting_for_post = State()

# ================== БАЗА ДАННЫХ ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
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
                created_at TIMESTAMP,
                last_bonus TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                method TEXT,
                requisites TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                label TEXT UNIQUE,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP,
                expires_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ad_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER
            )
        """)
        await db.commit()

# ================== ВСПОМОГАТЕЛЬНЫЙ ОТПРАВЩИК МЕНЮ ==================
async def send_menu_ui(bot: Bot, chat_id: int, menu_key: str, text: str, reply_markup: InlineKeyboardMarkup, delete_message_id: Optional[int] = None):
    """Отправляет фото с меню, если картинка существует. Если нет — отправляет просто текст."""
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
            logging.error(f"Ошибка отправки фото {img_path}: {e}")

    return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")

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

# ================== ЮMONEY ИНТЕГРАЦИЯ ==================
def generate_yoomoney_url(label: str, amount: float = AD_PRICE) -> str:
    target = "Оплата рекламы в боте CashPulse"
    return (
        f"https://yoomoney.ru/quickpay/confirm.xml?"
        f"receiver={YOOMONEY_WALLET}&quickpay-form=shop&targets={target}&"
        f"paymentType=SB&sum={amount}&label={label}"
    )

async def check_yoomoney_payment(label: str, min_sum: float = AD_PRICE) -> bool:
    if not YOOMONEY_TOKEN:
        return False
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
        logging.error(f"Ошибка проверки оплаты ЮMoney: {e}")
    return False

# ================== ФОНОВАЯ ОЧИСТКА РЕКЛАМЫ ЧЕРЕЗ 72 ЧАСА ==================
async def ads_cleanup_loop(bot: Bot):
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.utcnow()
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id, user_id FROM ads WHERE status = 'active' AND expires_at <= ?",
                    (now.isoformat(),)
                ) as cursor:
                    expired_ads = await cursor.fetchall()

                for ad_id, user_id in expired_ads:
                    async with db.execute(
                        "SELECT chat_id, message_id FROM ad_messages WHERE ad_id = ?",
                        (ad_id,)
                    ) as cur_msgs:
                        msgs = await cur_msgs.fetchall()

                    for c_id, m_id in msgs:
                        try:
                            await bot.delete_message(chat_id=c_id, message_id=m_id)
                        except Exception:
                            pass
                        await asyncio.sleep(0.04)

                    await db.execute("UPDATE ads SET status = 'expired' WHERE id = ?", (ad_id,))
                    await db.commit()

                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text="⏱ <b>Срок вашей рекламы (72 часа) истек!</b>\nВсе рекламные сообщения успешно удалены из бота."
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка в цикле автоудаления рекламы: {e}")

# ================== РЕФЕРАЛЬНЫЕ НАЧИСЛЕНИЯ ==================
async def process_referral(bot: Bot, new_user_id: int, referrer_id: int):
    rewards = [
        (1, REF_L1_REWARD, "1-го уровня"),
        (2, REF_L2_REWARD, "2-го уровня"),
        (3, REF_L3_REWARD, "3-го уровня")
    ]
    current_ref_id = referrer_id

    async with aiosqlite.connect(DB_PATH) as db:
        for level, reward, lvl_text in rewards:
            if not current_ref_id:
                break
            async with db.execute("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (current_ref_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    break

            await db.execute("""
                UPDATE users 
                SET balance = balance + ?, earned_ref = earned_ref + ?
                WHERE user_id = ?
            """, (reward, reward, current_ref_id))

            try:
                await bot.send_message(
                    chat_id=current_ref_id,
                    text=f"🎉 <b>Новый реферал {lvl_text}!</b>\nНачислено на баланс: <b>+{reward:.2f} ₽</b>"
                )
            except Exception:
                pass

            current_ref_id = row[1]
        await db.commit()

# ================== ХЭНДЛЕРЫ ==================
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
            possible_ref = int(args[1].replace("ref_", ""))
            if possible_ref != user_id:
                referrer_id = possible_ref
        except ValueError:
            referrer_id = None

    is_new = False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                is_new = True
                now = datetime.utcnow().isoformat()
                await db.execute("""
                    INSERT INTO users (user_id, username, first_name, referrer_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, username, first_name, referrer_id, now))
                await db.commit()

    if is_new and referrer_id:
        await process_referral(bot, user_id, referrer_id)

    await show_main_menu(bot, message.chat.id, user_id, first_name)

async def show_main_menu(bot: Bot, chat_id: int, user_id: int, first_name: str, delete_msg_id: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
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

# ----------------- РЕФЕРАЛЬНЫЙ ЭКРАН -----------------
@dp.callback_query(F.data == "menu_ref")
async def cb_ref(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cur:
            total_friends = (await cur.fetchone())[0]

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%")) as cur:
            today_friends = (await cur.fetchone())[0]

    text = (
        f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"За каждого друга, который запустит бота по ссылке, — <b>{REF_L1_REWARD:.2f} ₽</b> и ещё по цепочке:\n"
        f"• 2 уровень (друзья друзей) — <b>{REF_L2_REWARD:.2f} ₽</b>\n"
        f"• 3 уровень — <b>{REF_L3_REWARD:.2f} ₽</b>\n\n"
        f"👥 Друзей: <b>{total_friends}</b> (сегодня +{today_friends})\n\n"
        f"Поделитесь ссылкой в чатах, с друзьями или в своём канале 👇"
    )

    share_text = f"Зарабатывай реальные деньги прямо в Telegram! Переходи: {ref_link}"
    share_url = f"https://t.me/share/url?url={ref_link}&text={share_text}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Поделиться", url=share_url)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")]
    ])

    await send_menu_ui(bot, call.message.chat.id, "ref", text, kb, call.message.message_id)

# ----------------- ПРОФИЛЬ -----------------
@dp.callback_query(F.data == "menu_profile")
async def cb_profile(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT balance, pending_balance, paid_balance, earned_ref, earned_bonus, created_at 
            FROM users WHERE user_id = ?
        """, (user_id,)) as cur:
            u = await cur.fetchone()

        if not u:
            return

        balance, pending, paid, earned_ref, earned_bonus, created_at = u
        reg_date = created_at.split("T")[0] if "T" in created_at else created_at.split()[0]
        total_earned = earned_ref + earned_bonus

        # Уровни рефералов
        async with db.execute("SELECT user_id FROM users WHERE referrer_id = ?", (user_id,)) as cur:
            l1_ids = [row[0] for row in await cur.fetchall()]
        l1_count = len(l1_ids)
        l1_earned = l1_count * REF_L1_REWARD

        l2_ids = []
        if l1_ids:
            q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l1_ids))})"
            async with db.execute(q, l1_ids) as cur:
                l2_ids = [row[0] for row in await cur.fetchall()]
        l2_count = len(l2_ids)
        l2_earned = l2_count * REF_L2_REWARD

        l3_ids = []
        if l2_ids:
            q = f"SELECT user_id FROM users WHERE referrer_id IN ({','.join(['?']*len(l2_ids))})"
            async with db.execute(q, l2_ids) as cur:
                l3_ids = [row[0] for row in await cur.fetchall()]
        l3_count = len(l3_ids)
        l3_earned = l3_count * REF_L3_REWARD

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND created_at LIKE ?", (user_id, f"{today_str}%")) as cur:
            today_friends = (await cur.fetchone())[0]

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
        f"1 уровень: <b>{l1_count}</b> — заработано <b>{l1_earned:.2f} ₽</b>\n"
        f"2 уровень: <b>{l2_count}</b> — заработано <b>{l2_earned:.2f} ₽</b>\n"
        f"3 уровень: <b>{l3_count}</b> — заработано <b>{l3_earned:.2f} ₽</b>"
    )

    await send_menu_ui(bot, call.message.chat.id, "profile", text, get_back_keyboard(), call.message.message_id)

# ----------------- БОНУС -----------------
@dp.callback_query(F.data == "claim_bonus")
async def cb_bonus(call: CallbackQuery):
    user_id = call.from_user.id
    now = datetime.utcnow()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_bonus FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            last_bonus = row[0] if row else None

        can_claim = False
        if not last_bonus:
            can_claim = True
        else:
            lb_time = datetime.fromisoformat(last_bonus)
            if now - lb_time >= timedelta(hours=24):
                can_claim = True
            else:
                diff = timedelta(hours=24) - (now - lb_time)
                hours, remainder = divmod(int(diff.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                await call.answer(
                    f"⏳ Бонус уже получен! До следующего: {hours} ч. {minutes} мин.",
                    show_alert=True
                )
                return

        if can_claim:
            await db.execute("""
                UPDATE users 
                SET balance = balance + ?, earned_bonus = earned_bonus + ?, last_bonus = ?
                WHERE user_id = ?
            """, (DAILY_BONUS, DAILY_BONUS, now.isoformat(), user_id))
            await db.commit()

            await call.answer(f"🎉 Вы получили ежедневный бонус {DAILY_BONUS:.2f} ₽!", show_alert=True)
            # Обновим главное меню
            await show_main_menu(call.bot, call.message.chat.id, user_id, call.from_user.first_name, call.message.message_id)

# ----------------- ВЫВОД СРЕДСТВ -----------------
@dp.callback_query(F.data == "menu_withdraw")
async def cb_withdraw(call: CallbackQuery, bot: Bot):
    await call.answer()
    user_id = call.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance, pending_balance FROM users WHERE user_id = ?", (user_id,)) as cur:
            balance, pending = await cur.fetchone()

        async with db.execute("SELECT id, amount, status FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)) as cur:
            last_w = await cur.fetchone()

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
async def cb_withdraw_select_method(call: CallbackQuery, state: FSMContext):
    await call.answer()
    method = call.data.split(":")[1]
    method_name = "💳 Банковская карта" if method == "card" else "⚡ Система быстрых платежей (СБП)"

    await state.update_data(method=method_name)
    await state.set_state(WithdrawStates.waiting_for_amount)

    await call.message.answer(
        f"Вы выбрали: <b>{method_name}</b>\n\n"
        f"Введите сумму для вывода (минимум <b>{MIN_WITHDRAW:.0f} ₽</b>):",
        parse_mode="HTML"
    )

@dp.message(WithdrawStates.waiting_for_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("⚠️ Введите корректное число (например: 3000):")

    if amount < MIN_WITHDRAW:
        return await message.answer(f"⚠️ Минимальная сумма вывода составляет {MIN_WITHDRAW:.0f} ₽:")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
            balance = (await cur.fetchone())[0]

    if amount > balance:
        return await message.answer(f"⚠️ Недостаточно средств на балансе. Доступно: <b>{balance:.2f} ₽</b>", parse_mode="HTML")

    await state.update_data(amount=amount)
    await state.set_state(WithdrawStates.waiting_for_details)

    data = await state.get_data()
    req_hint = "номер карты" if "карта" in data['method'].lower() else "номер телефона и банк"
    await message.answer(f"Введите ваши реквизиты ({req_hint}):")

@dp.message(WithdrawStates.waiting_for_details)
async def process_withdraw_details(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    details = message.text
    data = await state.get_data()
    amount = data["amount"]
    method = data["method"]
    await state.clear()

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users 
            SET balance = balance - ?, pending_balance = pending_balance + ?
            WHERE user_id = ?
        """, (amount, amount, user_id))

        cur = await db.execute("""
            INSERT INTO withdrawals (user_id, amount, method, requisites, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (user_id, amount, method, details, now))
        w_id = cur.lastrowid
        await db.commit()

    await message.answer(
        f"✅ <b>Заявка #{w_id} на сумму {amount:.2f} ₽ успешно создана!</b>\n\n"
        f"Метод: {method}\n"
        f"Реквизиты: <code>{details}</code>\n"
        f"Статус: ⏳ В обработке\n\n"
        f"Выплаты обычно занимают от 10 минут до 24 часов.",
        parse_mode="HTML"
    )

    if ADMIN_ID:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выплачено", callback_data=f"adm_pay:{w_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject:{w_id}")
            ]
        ])
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🚨 <b>Новая заявка на вывод #{w_id}!</b>\n\n"
                    f"Пользователь: <code>{user_id}</code> (@{message.from_user.username})\n"
                    f"Сумма: <b>{amount:.2f} ₽</b>\n"
                    f"Метод: <b>{method}</b>\n"
                    f"Реквизиты: <code>{details}</code>"
                ),
                reply_markup=admin_kb,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")

# ----------------- АДМИН-ДЕЙСТВИЯ ПО ВЫВОДУ -----------------
@dp.callback_query(F.data.startswith("adm_pay:"))
async def cb_admin_pay(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет доступа", show_alert=True)

    w_id = int(call.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,)) as cur:
            row = await cur.fetchone()
            if not row or row[2] != 'pending':
                return await call.answer("Заявка уже обработана!", show_alert=True)
            u_id, amount, _ = row

        await db.execute("""
            UPDATE users 
            SET pending_balance = pending_balance - ?, paid_balance = paid_balance + ?
            WHERE user_id = ?
        """, (amount, amount, u_id))
        await db.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (w_id,))
        await db.commit()

    await call.message.edit_text(call.message.text + "\n\n🟢 <b>ВЫПЛАЧЕНО</b>", parse_mode="HTML")
    try:
        await bot.send_message(u_id, f"✅ <b>Ваша заявка #{w_id} на сумму {amount:.2f} ₽ успешно выплачена!</b>", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm_reject:"))
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет доступа", show_alert=True)

    w_id = int(call.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (w_id,)) as cur:
            row = await cur.fetchone()
            if not row or row[2] != 'pending':
                return await call.answer("Заявка уже обработана!", show_alert=True)
            u_id, amount, _ = row

        await db.execute("""
            UPDATE users 
            SET pending_balance = pending_balance - ?, balance = balance + ?
            WHERE user_id = ?
        """, (amount, amount, u_id))
        await db.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (w_id,))
        await db.commit()

    await call.message.edit_text(call.message.text + "\n\n🔴 <b>ОТКЛОНЕНО (Средства возвращены)</b>", parse_mode="HTML")
    try:
        await bot.send_message(u_id, f"❌ Ваша заявка #{w_id} на сумму {amount:.2f} ₽ была отклонена. Деньги возвращены на баланс.", parse_mode="HTML")
    except Exception:
        pass

# ----------------- КАК ЭТО РАБОТАЕТ -----------------
@dp.callback_query(F.data == "menu_how")
async def cb_how(call: CallbackQuery, bot: Bot):
    await call.answer()
    text = (
        f"❓ <b>Как это работает</b>\n\n"
        f"1️⃣ Берёте свою ссылку в разделе «🔗 Моя ссылка»\n"
        f"2️⃣ Делитесь ей с друзьями, в чатах или своём канале\n"
        f"3️⃣ Получаете деньги на баланс:\n"
        f"• <b>{REF_L1_REWARD:.2f} ₽</b> за каждого друга, который запустил бота по вашей ссылке\n"
        f"• <b>{REF_L2_REWARD:.2f} ₽</b> за каждого, кого пригласил ваш друг (2 уровень)\n"
        f"• <b>{REF_L3_REWARD:.2f} ₽</b> за 3 уровень\n"
        f"4️⃣ Дополнительно можно заработать в разделе «🎁 Бонус» — каждые 24 часа по <b>{DAILY_BONUS:.0f} ₽</b>\n\n"
        f"💸 <b>Вывод</b> — от <b>{MIN_WITHDRAW:.0f} ₽</b> на банковскую карту или по СБП. Заявки обрабатываются быстро и без комиссий.\n\n"
        f"⚠️ Друг засчитывается, только если он впервые запускает бота. Накрутка фейковыми аккаунтами запрещена."
    )
    await send_menu_ui(bot, call.message.chat.id, "how", text, get_back_keyboard(), call.message.message_id)

# ----------------- ЗАКАЗАТЬ РЕКЛАМУ (ЮMONEY + АВТОУДАЛЕНИЕ 72 ЧАСА) -----------------
@dp.callback_query(F.data == "menu_ad")
async def cb_ad_menu(call: CallbackQuery, bot: Bot, state: FSMContext):
    await call.answer()
    text = (
        f"📢 <b>Заказ рекламы в CashPulse</b>\n\n"
        f"🌟 <b>Условия размещения:</b>\n"
        f"• Охват: Рассылка всем активным пользователям бота\n"
        f"• Время показа: <b>ровно 72 часа</b> (после этого бот автоматически удалит пост из всех диалогов)\n"
        f"• Стоимость: <b>{AD_PRICE:.0f} ₽</b>\n\n"
        f"Отправьте боту готовый рекламный пост прямо сейчас (текст, картинку с подписью или перешлите сообщение):"
    )
    await state.set_state(AdStates.waiting_for_post)
    await send_menu_ui(bot, call.message.chat.id, "ad", text, get_back_keyboard(), call.message.message_id)

@dp.message(AdStates.waiting_for_post)
async def process_ad_post(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    source_chat_id = message.chat.id
    source_message_id = message.message_id

    label = f"ad_{user_id}_{int(datetime.utcnow().timestamp())}"
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO ads (user_id, label, source_chat_id, source_message_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (user_id, label, source_chat_id, source_message_id, now))
        ad_id = cur.lastrowid
        await db.commit()

    await state.clear()

    payment_url = generate_yoomoney_url(label, AD_PRICE)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {AD_PRICE:.0f} ₽", url=payment_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_ad:{ad_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="menu_main")]
    ])

    await message.reply(
        f"✅ <b>Рекламный пост принят!</b>\n\n"
        f"Заказ <code>#{ad_id}</code>\n"
        f"К оплате: <b>{AD_PRICE:.0f} ₽</b>\n\n"
        f"1. Оплатите счет по кнопке ниже через ЮMoney / Карту.\n"
        f"2. После оплаты нажмите кнопку «🔄 Проверить оплату».\n"
        f"3. Рассылка начнется мгновенно, а через 72 часа бот сам удалит пост.",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_ad:"))
async def cb_check_ad(call: CallbackQuery, bot: Bot):
    ad_id = int(call.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT label, status, source_chat_id, source_message_id FROM ads WHERE id = ?", (ad_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return await call.answer("Заказ не найден!", show_alert=True)
            label, status, s_chat_id, s_msg_id = row

    if status == 'active':
        return await call.answer("Этот заказ уже оплачен и запущен!", show_alert=True)

    await call.answer("Проверяем платеж в ЮMoney...")
    is_paid = await check_yoomoney_payment(label, AD_PRICE)

    if not is_paid:
        return await call.message.answer(
            "❌ <b>Оплата пока не обнаружена!</b>\nЕсли вы только что оплатили, подождите 1 минуту и нажмите кнопку снова.",
            parse_mode="HTML"
        )

    # Запускаем рассылку
    expires_at = (datetime.utcnow() + timedelta(hours=AD_DURATION_HOURS)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE ads SET status = 'active', expires_at = ? WHERE id = ?", (expires_at, ad_id))
        await db.commit()

        async with db.execute("SELECT user_id FROM users") as cur:
            users = await cur.fetchall()

    await call.message.answer("🎉 <b>Оплата подтверждена! Запускаем рассылку...</b>", parse_mode="HTML")

    count = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for (u_id,) in users:
            try:
                sent = await bot.copy_message(chat_id=u_id, from_chat_id=s_chat_id, message_id=s_msg_id)
                await db.execute("INSERT INTO ad_messages (ad_id, chat_id, message_id) VALUES (?, ?, ?)", (ad_id, u_id, sent.message_id))
                count += 1
                await asyncio.sleep(0.04) # защита от лимитов Telegram
            except Exception:
                pass
        await db.commit()

    await call.message.answer(
        f"🚀 <b>Реклама успешно разослана!</b>\n\n"
        f"Получателей: {count}\n"
        f"Пост будет автоматически удален ботом через <b>72 часа</b>.",
        parse_mode="HTML"
    )

# ================== СТАРТ БОТА ==================
async def main():
    if not BOT_TOKEN:
        print("ОШИБКА: Задайте BOT_TOKEN в файле .env!")
        sys.exit(1)

    await init_db()
    bot = Bot(token=BOT_TOKEN)

    # Запуск фонового процесса очистки объявлений каждые 60 сек
    asyncio.create_task(ads_cleanup_loop(bot))

    logging.info("Бот CashPulse успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
