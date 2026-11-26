import asyncio
import logging
import os
import random
import sqlite3
import string
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "vladofix97").lstrip("@")
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "")
YOOMONEY_TOKEN = os.getenv("YOOMONEY_TOKEN", "")

REF_LVL1 = float(os.getenv("REF_LVL1", "12.50"))
REF_LVL2 = float(os.getenv("REF_LVL2", "6.80"))
REF_LVL3 = float(os.getenv("REF_LVL3", "3.20"))
DAILY_BONUS = float(os.getenv("DAILY_BONUS", "25.00"))
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "4000.00"))
AD_PRICE = float(os.getenv("AD_PRICE", "5000.00"))
AD_DURATION_HOURS = int(os.getenv("AD_DURATION_HOURS", "72"))

BOT_TITLE = "Приглашай и зарабатывай💵💳"
DB_PATH = "bot_database.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
router = Router()

# Названия разделов для админ-панели (картинки)
SECTIONS = {
    "main": "🏠 Главное меню",
    "link": "🔗 Моя ссылка",
    "profile": "👤 Профиль",
    "bonus": "🎁 Бонус",
    "withdraw": "💸 Вывод средств",
    "how_it_works": "❓ Как это работает",
    "ads": "📢 Заказать рекламу",
}


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                reg_date TEXT,
                referrer_id INTEGER DEFAULT 0,
                balance REAL DEFAULT 0.0,
                earned_refs REAL DEFAULT 0.0,
                earned_bonus REAL DEFAULT 0.0,
                lvl1_count INTEGER DEFAULT 0,
                lvl2_count INTEGER DEFAULT 0,
                lvl3_count INTEGER DEFAULT 0,
                lvl1_earned REAL DEFAULT 0.0,
                lvl2_earned REAL DEFAULT 0.0,
                lvl3_earned REAL DEFAULT 0.0,
                last_bonus INTEGER DEFAULT 0,
                ref_code TEXT UNIQUE
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS referrals_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inviter_id INTEGER,
                new_user_id INTEGER,
                level INTEGER,
                date TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                method TEXT,
                details TEXT,
                status TEXT DEFAULT '⏳ в обработке',
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS menu_images (
                section TEXT PRIMARY KEY,
                file_id TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ads_orders (
                label TEXT PRIMARY KEY,
                user_id INTEGER,
                photo_id TEXT,
                html_text TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ads_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                delete_at INTEGER
            )
        """)
        conn.commit()


def generate_ref_code(length=6) -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=length))
        with sqlite3.connect(DB_PATH) as conn:
            res = conn.execute("SELECT 1 FROM users WHERE ref_code = ?", (code,)).fetchone()
            if not res:
                return code


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_user_by_ref_code(ref_code: str) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE ref_code = ?", (ref_code,)).fetchone()


def get_section_image(section: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT file_id FROM menu_images WHERE section = ?", (section,)).fetchone()
        return row[0] if row else None


def set_section_image(section: str, file_id: Optional[str]):
    with sqlite3.connect(DB_PATH) as conn:
        if file_id:
            conn.execute(
                "INSERT INTO menu_images (section, file_id) VALUES (?, ?) "
                "ON CONFLICT(section) DO UPDATE SET file_id = excluded.file_id",
                (section, file_id),
            )
        else:
            conn.execute("DELETE FROM menu_images WHERE section = ?", (section,))
        conn.commit()


def get_today_refs(user_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM referrals_log WHERE inviter_id = ? AND level = 1 AND date = ?",
            (user_id, today),
        ).fetchone()
        return row[0] if row else 0


def get_user_withdrawals_info(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sum_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id = ? AND status = '⏳ в обработке'",
            (user_id,),
        ).fetchone()
        on_withdraw = sum_row[0] if sum_row else 0.0
        last_reqs = conn.execute(
            "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        return on_withdraw, last_reqs


# ==================== ФОРМАТИРОВАНИЕ И ОТРИСОВКА ====================
def fmt_money(val: float) -> str:
    if round(val, 2) == 0:
        return "0"
    if round(val, 2) == int(val) and val >= 1000 and val % 100 == 0:
        return f"{int(val):,}".replace(",", " ")
    return f"{val:,.2f}".replace(",", " ").replace(".", ",")


async def render_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    section: str,
    message: Optional[Message] = None,
):
    """Умная отправка/редактирование меню с поддержкой картинок под каждый раздел"""
    photo_id = get_section_image(section)

    if message:
        try:
            if photo_id and message.photo:
                await message.edit_media(
                    media=InputMediaPhoto(media=photo_id, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=reply_markup,
                )
                return
            elif not photo_id and not message.photo:
                await message.edit_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            else:
                await message.delete()
        except TelegramBadRequest:
            try:
                await message.delete()
            except Exception:
                pass

    if photo_id:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ==================== КЛАВИАТУРЫ И ТЕКСТЫ ====================
def kb_main(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="menu:link")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
            InlineKeyboardButton(text="🎁 Бонус", callback_data="menu:bonus"),
        ],
        [InlineKeyboardButton(text="💸 Вывод средств", callback_data="menu:withdraw")],
        [
            InlineKeyboardButton(text="📢 Заказать рекламу", callback_data="menu:ads"),
            InlineKeyboardButton(text="❓ Как это работает", callback_data="menu:how_it_works"),
        ],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]]
    )


def get_main_text(user: sqlite3.Row) -> str:
    return (
        f"👋 Привет, <b>{user['first_name']}</b>!\n\n"
        f"Приглашай друзей по своей ссылке и получай деньги за каждого:\n"
        f"<blockquote>• <b>{fmt_money(REF_LVL1)} ₽</b> — за друга\n"
        f"• <b>{fmt_money(REF_LVL2)} ₽</b> — за друга твоего друга\n"
        f"• <b>{fmt_money(REF_LVL3)} ₽</b> — за друга 3-го уровня</blockquote>\n\n"
        f"💰 Баланс: <b>{fmt_money(user['balance'])} ₽</b>"
    )


# ==================== СОСТОЯНИЯ FSM ====================
class UserStates(StatesGroup):
    withdraw_details = State()
    ad_create_post = State()


class AdminStates(StatesGroup):
    upload_photo = State()
    set_balance_user = State()


# ==================== РЕГИСТРАЦИЯ И /START ====================
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or "Пользователь"

    user = get_user(user_id)
    if not user:
        ref_code = generate_ref_code()
        reg_date = datetime.now().strftime("%Y-%m-%d")
        referrer_id = 0

        # Проверяем реферальный аргумент (например: /start p_ABCDEF)
        args = command.args
        if args and args.startswith("p_"):
            inviter_code = args[2:]
            inviter = get_user_by_ref_code(inviter_code)
            if inviter and inviter["user_id"] != user_id:
                referrer_id = inviter["user_id"]

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, reg_date, referrer_id, ref_code) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, first_name, reg_date, referrer_id, ref_code),
            )
            conn.commit()

        # Начисление по 3-м уровням реферальной системы
        if referrer_id:
            await process_referral_rewards(bot, referrer_id, user_id, first_name)

        user = get_user(user_id)
    else:
        # Обновляем имя и username, если пользователь их менял
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
            conn.commit()
        user = get_user(user_id)

    await render_screen(
        bot=bot,
        chat_id=user_id,
        text=get_main_text(user),
        reply_markup=kb_main(user_id),
        section="main",
    )


async def process_referral_rewards(bot: Bot, lvl1_id: int, new_user_id: int, new_user_name: str):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 1 Уровень
        lvl1_user = conn.execute("SELECT * FROM users WHERE user_id = ?", (lvl1_id,)).fetchone()
        if not lvl1_user:
            return

        conn.execute(
            "UPDATE users SET balance = balance + ?, earned_refs = earned_refs + ?, "
            "lvl1_count = lvl1_count + 1, lvl1_earned = lvl1_earned + ? WHERE user_id = ?",
            (REF_LVL1, REF_LVL1, REF_LVL1, lvl1_id),
        )
        conn.execute(
            "INSERT INTO referrals_log (inviter_id, new_user_id, level, date) VALUES (?, ?, 1, ?)",
            (lvl1_id, new_user_id, today),
        )

        # 2 Уровень
        lvl2_id = lvl1_user["referrer_id"]
        lvl2_user = None
        if lvl2_id:
            lvl2_user = conn.execute("SELECT * FROM users WHERE user_id = ?", (lvl2_id,)).fetchone()
            if lvl2_user:
                conn.execute(
                    "UPDATE users SET balance = balance + ?, earned_refs = earned_refs + ?, "
                    "lvl2_count = lvl2_count + 1, lvl2_earned = lvl2_earned + ? WHERE user_id = ?",
                    (REF_LVL2, REF_LVL2, REF_LVL2, lvl2_id),
                )
                conn.execute(
                    "INSERT INTO referrals_log (inviter_id, new_user_id, level, date) VALUES (?, ?, 2, ?)",
                    (lvl2_id, new_user_id, today),
                )

        # 3 Уровень
        lvl3_id = lvl2_user["referrer_id"] if lvl2_user else 0
        if lvl3_id:
            lvl3_user = conn.execute("SELECT * FROM users WHERE user_id = ?", (lvl3_id,)).fetchone()
            if lvl3_user:
                conn.execute(
                    "UPDATE users SET balance = balance + ?, earned_refs = earned_refs + ?, "
                    "lvl3_count = lvl3_count + 1, lvl3_earned = lvl3_earned + ? WHERE user_id = ?",
                    (REF_LVL3, REF_LVL3, REF_LVL3, lvl3_id),
                )
                conn.execute(
                    "INSERT INTO referrals_log (inviter_id, new_user_id, level, date) VALUES (?, ?, 3, ?)",
                    (lvl3_id, new_user_id, today),
                )
        conn.commit()

    # Уведомления пригласившим
    try:
        await bot.send_message(
            lvl1_id,
            f"🎉 У вас новый друг 1-го уровня (<b>{new_user_name}</b>)!\n"
            f"💰 На ваш баланс начислено: <b>+{fmt_money(REF_LVL1)} ₽</b>",
        )
    except Exception:
        pass

    if lvl2_id:
        try:
            await bot.send_message(
                lvl2_id,
                f"👥 У вас новый реферал 2-го уровня!\n"
                f"💰 На ваш баланс начислено: <b>+{fmt_money(REF_LVL2)} ₽</b>",
            )
        except Exception:
            pass

    if lvl3_id:
        try:
            await bot.send_message(
                lvl3_id,
                f"👥 У вас новый реферал 3-го уровня!\n"
                f"💰 На ваш баланс начислено: <b>+{fmt_money(REF_LVL3)} ₽</b>",
            )
        except Exception:
            pass


# ==================== НАВИГАЦИЯ ПО МЕНЮ ====================
@router.callback_query(F.data == "menu:main")
async def cb_menu_main(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    user = get_user(call.from_user.id)
    if not user:
        return
    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=get_main_text(user),
        reply_markup=kb_main(call.from_user.id),
        section="main",
        message=call.message,
    )
    await call.answer()


@router.callback_query(F.data == "menu:link")
async def cb_menu_link(call: CallbackQuery, bot: Bot):
    user = get_user(call.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=p_{user['ref_code']}"
    today_refs = get_today_refs(user["user_id"])

    share_text = f"Зарабатывай реальные деньги за приглашение друзей в Telegram! Заходи 👇"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_text)}"

    text = (
        f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"За каждого друга, который запустит бота по ссылке, — <b>{fmt_money(REF_LVL1)} ₽</b> и ещё по цепочке:\n"
        f"• 2 уровень (друзья друзей) — <b>{fmt_money(REF_LVL2)} ₽</b>\n"
        f"• 3 уровень — <b>{fmt_money(REF_LVL3)} ₽</b>\n\n"
        f"👥 Друзей: <b>{user['lvl1_count']}</b> (сегодня +{today_refs})\n\n"
        f"Поделитесь ссылкой в чатах, с друзьями или в своём канале 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", url=share_url)],
            [InlineKeyboardButton(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=ref_link))],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")],
        ]
    )

    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb,
        section="link",
        message=call.message,
    )
    await call.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_menu_profile(call: CallbackQuery, bot: Bot):
    user = get_user(call.from_user.id)
    today_refs = get_today_refs(user["user_id"])
    on_withdraw, _ = get_user_withdrawals_info(user["user_id"])
    total_earned = user["earned_refs"] + user["earned_bonus"]

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"📅 В боте с {user['reg_date']}\n\n"
        f"💰 Баланс: <b>{fmt_money(user['balance'])} ₽</b>\n"
        f"⏳ На выводе: <b>{fmt_money(on_withdraw)} ₽</b>\n"
        f"✅ Выплачено: <b>0 ₽</b>\n\n"
        f"📈 Заработано всего: <b>{fmt_money(total_earned)} ₽</b>\n"
        f"• за друзей: {fmt_money(user['earned_refs'])} ₽\n"
        f"• за бонусы: {fmt_money(user['earned_bonus'])} ₽\n\n"
        f"👥 <b>Друзья</b> (сегодня +{today_refs}):\n"
        f"1 уровень: <b>{user['lvl1_count']}</b> — заработано {fmt_money(user['lvl1_earned'])} ₽\n"
        f"2 уровень: <b>{user['lvl2_count']}</b> — заработано {fmt_money(user['lvl2_earned'])} ₽\n"
        f"3 уровень: <b>{user['lvl3_count']}</b> — заработано {fmt_money(user['lvl3_earned'])} ₽"
    )

    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb_back(),
        section="profile",
        message=call.message,
    )
    await call.answer()


@router.callback_query(F.data == "menu:bonus")
async def cb_menu_bonus(call: CallbackQuery, bot: Bot):
    user = get_user(call.from_user.id)
    now = int(time.time())
    elapsed = now - user["last_bonus"]

    if elapsed >= 86400:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE users SET balance = balance + ?, earned_bonus = earned_bonus + ?, last_bonus = ? "
                "WHERE user_id = ?",
                (DAILY_BONUS, DAILY_BONUS, now, user["user_id"]),
            )
            conn.commit()
        user = get_user(call.from_user.id)
        text = (
            f"🎁 <b>Ежедневный бонус</b>\n\n"
            f"✅ Вы успешно получили ежедневный бонус: <b>+{fmt_money(DAILY_BONUS)} ₽</b>!\n"
            f"💰 Ваш текущий баланс: <b>{fmt_money(user['balance'])} ₽</b>\n\n"
            f"⏳ Следующий бонус будет доступен ровно через 24 часа."
        )
        await call.answer(f"🎉 +{fmt_money(DAILY_BONUS)} ₽ зачислено на баланс!", show_alert=True)
    else:
        rem = 86400 - elapsed
        hours = rem // 3600
        minutes = (rem % 3600) // 60
        text = (
            f"🎁 <b>Ежедневный бонус</b>\n\n"
            f"⏳ Вы уже забирали бонус за последние 24 часа!\n"
            f"Сумма ежедневного бонуса: <b>{fmt_money(DAILY_BONUS)} ₽</b>\n\n"
            f"До следующего получения осталось: <b>{hours} ч. {minutes} мин.</b>"
        )
        await call.answer("⏳ Бонус уже получен, приходите позже!")

    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb_back(),
        section="bonus",
        message=call.message,
    )


@router.callback_query(F.data == "menu:withdraw")
async def cb_menu_withdraw(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    user = get_user(call.from_user.id)
    on_withdraw, last_reqs = get_user_withdrawals_info(user["user_id"])

    reqs_text = ""
    if last_reqs:
        reqs_lines = [f"#{r['id']} · {fmt_money(r['amount'])} ₽ · {r['status']}" for r in last_reqs]
        reqs_text = "\n<b>Последние заявки:</b>\n" + "\n".join(reqs_lines) + "\n"

    buttons = []
    if user["balance"] < MIN_WITHDRAW:
        diff = MIN_WITHDRAW - user["balance"]
        footer = f"\n🔒 До вывода не хватает <b>{fmt_money(diff)} ₽</b> — приглашайте друзей!"
    else:
        footer = "\n✅ Доступен вывод средств! Выберите удобный способ получения:"
        buttons.append(
            [
                InlineKeyboardButton(text="💳 Банковская карта", callback_data="withdraw_method:card"),
                InlineKeyboardButton(text="⚡️ СБП", callback_data="withdraw_method:sbp"),
            ]
        )

    buttons.append([InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="menu:link")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")])

    text = (
        f"💸 <b>Вывод средств</b>\n\n"
        f"💰 Доступно: <b>{fmt_money(user['balance'])} ₽</b>\n"
        f"⏳ На выводе: <b>{fmt_money(on_withdraw)} ₽</b>\n\n"
        f"Минимальная сумма — <b>{fmt_money(MIN_WITHDRAW)} ₽</b>. Без комиссий.\n"
        f"Способы: банковская карта или СБП.\n"
        f"{reqs_text}"
        f"{footer}"
    )

    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        section="withdraw",
        message=call.message,
    )
    await call.answer()


@router.callback_query(F.data.startswith("withdraw_method:"))
async def cb_withdraw_method(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    if user["balance"] < MIN_WITHDRAW:
        await call.answer("Недостаточно средств для вывода!", show_alert=True)
        return

    method_code = call.data.split(":")[1]
    method_name = "💳 Банковская карта" if method_code == "card" else "⚡️ СБП"
    prompt = (
        "Введите номер вашей банковской карты (16 цифр):"
        if method_code == "card"
        else "Введите номер телефона и название банка для перевода по СБП:"
    )

    await state.update_data(method_name=method_name)
    await state.set_state(UserStates.withdraw_details)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:withdraw")]]
    )
    await call.message.answer(
        f"💸 <b>Оформление заявки на вывод</b>\n\n"
        f"Способ: <b>{method_name}</b>\n"
        f"Сумма к выводу: <b>{fmt_money(user['balance'])} ₽</b>\n\n"
        f"✏️ {prompt}",
        reply_markup=kb,
    )
    await call.answer()


@router.message(UserStates.withdraw_details)
async def process_withdraw_details(message: Message, state: FSMContext, bot: Bot):
    details = (message.text or "").strip()
    if len(details) < 5:
        await message.answer("⚠️ Пожалуйста, введите корректные реквизиты:")
        return

    data = await state.get_data()
    method_name = data.get("method_name", "Карта / СБП")
    await state.clear()

    user = get_user(message.from_user.id)
    if not user or user["balance"] < MIN_WITHDRAW:
        await message.answer("❌ Недостаточно средств на балансе.")
        return

    amount = user["balance"]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (user["user_id"],))
        cursor.execute(
            "INSERT INTO withdrawals (user_id, amount, method, details, status, created_at) "
            "VALUES (?, ?, ?, ?, '⏳ в обработке', ?)",
            (user["user_id"], amount, method_name, details, now_str),
        )
        req_id = cursor.lastrowid
        conn.commit()

    await message.answer(
        f"✅ <b>Заявка #{req_id} успешно создана!</b>\n\n"
        f"💰 Сумма: <b>{fmt_money(amount)} ₽</b>\n"
        f"🏦 Способ: <b>{method_name}</b>\n"
        f"📋 Реквизиты: <code>{details}</code>\n"
        f"Статус: <b>⏳ в обработке</b>"
    )

    user = get_user(message.from_user.id)
    await render_screen(
        bot=bot,
        chat_id=message.from_user.id,
        text=get_main_text(user),
        reply_markup=kb_main(message.from_user.id),
        section="main",
    )


@router.callback_query(F.data == "menu:how_it_works")
async def cb_how_it_works(call: CallbackQuery, bot: Bot):
    text = (
        f"❓ <b>Как это работает</b>\n\n"
        f"1️⃣ Берёте свою ссылку в разделе «🔗 Моя ссылка»\n"
        f"2️⃣ Делитесь ей с друзьями, в чатах или своём канале\n"
        f"3️⃣ Получаете деньги на баланс:\n"
        f"• <b>{fmt_money(REF_LVL1)} ₽</b> за каждого друга, который запустил бота по вашей ссылке\n"
        f"• <b>{fmt_money(REF_LVL2)} ₽</b> за каждого, кого пригласил ваш друг (2 уровень)\n"
        f"• <b>{fmt_money(REF_LVL3)} ₽</b> за 3 уровень\n"
        f"4️⃣ Дополнительно можно зарабатывать в разделе «🎁 Бонус» — <b>по {fmt_money(DAILY_BONUS)} ₽ каждые сутки</b>\n\n"
        f"💸 <b>Вывод</b> — от {fmt_money(MIN_WITHDRAW)} ₽ на банковскую карту или по СБП. "
        f"Заявки обрабатываются вручную, без комиссий.\n\n"
        f"⚠️ Друг засчитывается, только если он впервые запускает бота. "
        f"Накрутка фейковыми аккаунтами — бан и отказ в выплате."
    )
    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb_back(),
        section="how_it_works",
        message=call.message,
    )
    await call.answer()


# ==================== ЗАКАЗ РЕКЛАМЫ (ЮMONEY + АВТОУДАЛЕНИЕ 72 ЧАСА) ====================
def build_yoomoney_pay_url(label: str, amount: float) -> str:
    params = {
        "receiver": YOOMONEY_WALLET,
        "quickpay-form": "button",
        "paymentType": "AC",
        "sum": f"{amount:.2f}",
        "label": label,
        "targets": f"Реклама в боте на {AD_DURATION_HOURS} ч.",
    }
    return f"https://yoomoney.ru/quickpay/confirm.xml?{urllib.parse.urlencode(params)}"


async def check_yoomoney_payment(label: str) -> bool:
    if not YOOMONEY_TOKEN:
        return False
    url = "https://yoomoney.ru/api/operation-history"
    headers = {
        "Authorization": f"Bearer {YOOMONEY_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"label": label, "records": "5"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=10) as resp:
                if resp.status != 200:
                    return False
                result = await resp.json()
                operations = result.get("operations", [])
                for op in operations:
                    if op.get("status") == "success" and op.get("label") == label:
                        return True
    except Exception as e:
        logging.error(f"Ошибка проверки ЮMoney: {e}")
    return False


@router.callback_query(F.data == "menu:ads")
async def cb_menu_ads(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    text = (
        f"📢 <b>Заказать рекламу в боте</b>\n\n"
        f"Хотите привлечь живую аудиторию в свой проект, Telegram-канал или бота?\n\n"
        f"🔥 <b>Преимущества размещения:</b>\n"
        f"• Формат: <b>Рассылка вашего поста всем пользователям бота в ЛС</b>\n"
        f"• Поддержка фото, форматирования текста, эмодзи и ссылок\n"
        f"• Срок размещения: <b>{AD_DURATION_HOURS} часа</b> (после чего бот автоматически удаляет рекламу у всех)\n"
        f"• Стоимость: <b>{fmt_money(AD_PRICE)} ₽</b>\n"
        f"• Оплата: <b>ЮMoney / Банковская карта</b> (автоматический запуск после оплаты)\n\n"
        f"Нажмите кнопку ниже, чтобы создать рекламный пост 👇"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Создать рекламный пост ({fmt_money(AD_PRICE)} ₽)", callback_data="ads:create")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")],
        ]
    )
    await render_screen(
        bot=bot,
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb,
        section="ads",
        message=call.message,
    )
    await call.answer()


@router.callback_query(F.data == "ads:create")
async def cb_ads_create(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.ad_create_post)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:ads")]]
    )
    await call.message.answer(
        "📝 <b>Отправьте ваш рекламный пост</b>\n\n"
        "Вы можете отправить:\n"
        "• Обычное текстовое сообщение со ссылками\n"
        "• Картинку (фото) с описанием и ссылками\n\n"
        "<i>Отправьте сообщение в том виде, в котором его должны увидеть пользователи:</i>",
        reply_markup=kb,
    )
    await call.answer()


@router.message(UserStates.ad_create_post)
async def process_ad_post(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id if message.photo else None
    html_text = message.html_text or ""

    if not photo_id and not html_text:
        await message.answer("⚠️ Пожалуйста, отправьте текст или фото с текстом!")
        return

    await state.clear()
    label = f"ad_{message.from_user.id}_{int(time.time())}"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ads_orders (label, user_id, photo_id, html_text, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (label, message.from_user.id, photo_id, html_text, int(time.time())),
        )
        conn.commit()

    pay_url = build_yoomoney_pay_url(label, AD_PRICE)
    buttons = [
        [InlineKeyboardButton(text=f"💳 Оплатить {fmt_money(AD_PRICE)} ₽ (ЮMoney / Карта)", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"ads:check:{label}")],
    ]
    if message.from_user.id == ADMIN_ID:
        buttons.append(
            [InlineKeyboardButton(text="⚡️ Запустить без оплаты (Админ)", callback_data=f"ads:force:{label}")]
        )
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu:ads")])

    await message.answer(
        f"✅ <b>Рекламный пост сохранён и готов к рассылке!</b>\n\n"
        f"💰 К оплате: <b>{fmt_money(AD_PRICE)} ₽</b>\n"
        f"⏱ Срок размещения: <b>{AD_DURATION_HOURS} часа</b>\n\n"
        f"1. Нажмите кнопку <b>«Оплатить»</b> и совершите перевод.\n"
        f"2. После оплаты нажмите <b>«Проверить оплату»</b> — бот автоматически запустит рассылку всем пользователям.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("ads:check:") | F.data.startswith("ads:force:"))
async def cb_ads_check_payment(call: CallbackQuery, bot: Bot):
    parts = call.data.split(":")
    action = parts[1]
    label = parts[2]

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        order = conn.execute("SELECT * FROM ads_orders WHERE label = ?", (label,)).fetchone()

    if not order:
        await call.answer("❌ Заказ не найден.", show_alert=True)
        return

    if order["status"] == "paid":
        await call.answer("✅ Этот заказ уже оплачен и запущен!", show_alert=True)
        return

    if action == "force" and call.from_user.id == ADMIN_ID:
        is_paid = True
    else:
        is_paid = await check_yoomoney_payment(label)

    if not is_paid:
        await call.answer(
            "⏳ Оплата ещё не найдена. Если вы уже оплатили, подождите 15-30 секунд и нажмите ещё раз.",
            show_alert=True,
        )
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE ads_orders SET status = 'paid' WHERE label = ?", (label,))
        conn.commit()

    await call.message.edit_text(
        f"✅ <b>Оплата подтверждена!</b>\n\n"
        f"🚀 Рассылка вашего рекламного поста запущена.\n"
        f"⏳ Ровно через <b>{AD_DURATION_HOURS} часа</b> бот автоматически удалит рекламные сообщения у всех пользователей."
    )
    await call.answer()

    # Запускаем фоновую рассылку
    asyncio.create_task(broadcast_ad_post(bot, order))


async def broadcast_ad_post(bot: Bot, order: sqlite3.Row):
    with sqlite3.connect(DB_PATH) as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()

    sent_count = 0
    delete_at = int(time.time()) + (AD_DURATION_HOURS * 3600)

    for (uid,) in users:
        try:
            if order["photo_id"]:
                sent_msg = await bot.send_photo(
                    chat_id=uid,
                    photo=order["photo_id"],
                    caption=order["html_text"],
                    parse_mode=ParseMode.HTML,
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id=uid,
                    text=order["html_text"],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )

            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO ads_messages (label, chat_id, message_id, delete_at) VALUES (?, ?, ?, ?)",
                    (order["label"], uid, sent_msg.message_id, delete_at),
                )
                conn.commit()
            sent_count += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            order["user_id"],
            f"📊 <b>Рассылка вашей рекламы завершена!</b>\n"
            f"📬 Успешно доставлено пользователям: <b>{sent_count}</b>\n"
            f"🗑 Автоматическое удаление через: <b>{AD_DURATION_HOURS} ч.</b>",
        )
    except Exception:
        pass


async def auto_delete_expired_ads_loop(bot: Bot):
    """Фоновая задача: проверяет каждую минуту рекламные посты, которым исполнилось 72 часа, и удаляет их"""
    while True:
        try:
            now = int(time.time())
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                expired = conn.execute(
                    "SELECT * FROM ads_messages WHERE delete_at <= ?", (now,)
                ).fetchall()

            for row in expired:
                chat_id = row["chat_id"]
                msg_id = row["message_id"]
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except TelegramBadRequest:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text="<i>🗑 Срок размещения рекламы истёк (удалено ботом)</i>",
                        )
                    except Exception:
                        try:
                            await bot.edit_message_caption(
                                chat_id=chat_id,
                                message_id=msg_id,
                                caption="<i>🗑 Срок размещения рекламы истёк (удалено ботом)</i>",
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("DELETE FROM ads_messages WHERE id = ?", (row["id"],))
                    conn.commit()
                await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Ошибка в цикле удаления рекламы: {e}")

        await asyncio.sleep(60)


# ==================== АДМИН-ПАНЕЛЬ (СТАТИСТИКА, КАРТИНКИ И УПРАВЛЕНИЕ) ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await show_admin_panel(message)


@router.callback_query(F.data == "admin:main")
async def cb_admin_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await show_admin_panel(call.message, edit=True)
    await call.answer()


async def show_admin_panel(message: Message, edit: bool = False):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today_users = conn.execute("SELECT COUNT(*) FROM users WHERE reg_date = ?", (today,)).fetchone()[0]
        active_ads = conn.execute("SELECT COUNT(DISTINCT label) FROM ads_messages").fetchall()
        active_ads_count = len(active_ads)

    text = (
        f"⚙️ <b>Админ-панель бота</b>\n\n"
        f"👥 Запустили бота: <b>{total_users} чел.</b> (сегодня +{today_users})\n"
        f"📢 Активных рекламных рассылок: <b>{active_ads_count}</b>\n\n"
        f"Выберите нужный раздел управления:"
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=f"📊 Статистика пользователей ({total_users} чел.)",
                callback_data="admin:stats",
            )
        ]
    ]

    for sec_key, sec_title in SECTIONS.items():
        has_img = "✅" if get_section_image(sec_key) else "❌"
        buttons.append(
            [InlineKeyboardButton(text=f"{sec_title} (Фото: {has_img})", callback_data=f"admin:sec:{sec_key}")]
        )

    buttons.append([InlineKeyboardButton(text="💰 Изменить баланс пользователя", callback_data="admin:balance")])
    buttons.append([InlineKeyboardButton(text="◀️ Вернуться в бот", callback_data="menu:main")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit and not message.photo:
        await message.edit_text(text, reply_markup=kb)
    else:
        try:
            if edit:
                await message.delete()
        except Exception:
            pass
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today_users = conn.execute("SELECT COUNT(*) FROM users WHERE reg_date = ?", (today,)).fetchone()[0]
        ref_users = conn.execute("SELECT COUNT(*) FROM users WHERE referrer_id != 0").fetchone()[0]
        direct_users = total_users - ref_users

        total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]
        w_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM withdrawals").fetchone()
        w_count, w_sum = w_row[0], w_row[1]

        paid_ads = conn.execute("SELECT COUNT(*) FROM ads_orders WHERE status = 'paid'").fetchone()[0]
        last_users = conn.execute(
            "SELECT user_id, username, first_name, reg_date FROM users ORDER BY rowid DESC LIMIT 10"
        ).fetchall()

    users_list_text = ""
    if last_users:
        lines = []
        for i, u in enumerate(last_users, 1):
            uname = f"@{u['username']}" if u["username"] else "без юзернейма"
            lines.append(
                f"{i}. <b>{u['first_name']}</b> ({uname}) — <code>{u['user_id']}</code> [{u['reg_date']}]"
            )
        users_list_text = "\n\n📋 <b>Последние 10 запустивших бота:</b>\n" + "\n".join(lines)

    text = (
        f"📊 <b>Подробная статистика бота</b>\n\n"
        f"👥 <b>Всего запустили бота:</b> {total_users} чел.\n"
        f"🆕 <b>Запустили сегодня:</b> +{today_users} чел.\n"
        f"🔗 <b>Пришли по реф. ссылкам:</b> {ref_users} чел.\n"
        f"🚶 <b>Пришли сами (напрямую):</b> {direct_users} чел.\n\n"
        f"💰 <b>Сумма на балансах у людей:</b> {fmt_money(total_balance)} ₽\n"
        f"⏳ <b>Заявок на вывод:</b> {w_count} шт. (на {fmt_money(w_sum)} ₽)\n"
        f"📢 <b>Оплачено рекламных рассылок:</b> {paid_ads} шт."
        f"{users_list_text}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить данные", callback_data="admin:stats")],
            [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin:main")],
        ]
    )

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer("📊 Статистика обновлена")


@router.callback_query(F.data.startswith("admin:sec:"))
async def cb_admin_section(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    sec_key = call.data.split(":")[2]
    sec_title = SECTIONS.get(sec_key, sec_key)

    await state.set_state(AdminStates.upload_photo)
    await state.update_data(section=sec_key)

    buttons = []
    if get_section_image(sec_key):
        buttons.append(
            [InlineKeyboardButton(text="🗑 Удалить текущую картинку", callback_data=f"admin:del_img:{sec_key}")]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin:main")])

    await call.message.edit_text(
        f"🖼 <b>Настройка картинки: {sec_title}</b>\n\n"
        f"Просто <b>отправьте фото</b> в этот чат, и бот закрепит его за разделом «{sec_title}».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await call.answer()


@router.message(AdminStates.upload_photo, F.photo)
async def process_admin_photo(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    sec_key = data.get("section")
    file_id = message.photo[-1].file_id

    set_section_image(sec_key, file_id)
    await state.clear()
    await message.answer(f"✅ Картинка для раздела <b>{SECTIONS.get(sec_key)}</b> успешно установлена!")
    await show_admin_panel(message)


@router.callback_query(F.data.startswith("admin:del_img:"))
async def cb_admin_del_img(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    sec_key = call.data.split(":")[2]
    set_section_image(sec_key, None)
    await state.clear()
    await call.answer("🗑 Картинка удалена!", show_alert=True)
    await show_admin_panel(call.message, edit=True)


@router.callback_query(F.data == "admin:balance")
async def cb_admin_balance(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.set_balance_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:main")]]
    )
    await call.message.edit_text(
        "💰 <b>Изменение баланса</b>\n\n"
        "Отправьте ID пользователя и новый баланс через пробел.\n"
        f"Например: <code>{call.from_user.id} 5400</code>",
        reply_markup=kb,
    )
    await call.answer()


@router.message(AdminStates.set_balance_user)
async def process_admin_set_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer("⚠️ Неверный формат! Отправьте: <code>ID СУММА</code> через пробел.")
        return
    try:
        target_id = int(parts[0])
        new_bal = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Ошибка в числах! Пример: <code>123456789 4500</code>")
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, target_id))
        conn.commit()

    await state.clear()
    await message.answer(f"✅ Баланс пользователя <code>{target_id}</code> изменён на <b>{fmt_money(new_bal)} ₽</b>!")
    await show_admin_panel(message)


# ==================== ЗАПУСК БОТА ====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("ОШИБКА: Укажите BOT_TOKEN в файле .env")

    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    # Запуск фоновой задачи автоудаления рекламы через 72 часа
    asyncio.create_task(auto_delete_expired_ads_loop(bot))

    logging.info(f"Бот «{BOT_TITLE}» успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
