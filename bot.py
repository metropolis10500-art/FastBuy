import os
import asyncio
import logging
import sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_NOT_MEMBER, ADMINISTRATOR
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ChatMemberUpdated
)
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.client.default import DefaultBotProperties

# ================= ЗАГРУЗКА НАСТРОЕК ИЗ .ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "@admin")
BASE_PRICE_PER_SUB = float(os.getenv("BASE_PRICE_PER_SUB", 2.5))
USD_RATE = float(os.getenv("USD_RATE", 92.0))

if not BOT_TOKEN:
    raise ValueError("Ошибка: Не указан BOT_TOKEN в файле .env!")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_NAME = "fastbuy_tg.db"

# ================= БАЗА ДАННЫХ (SQLite) =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            payouts_count INTEGER DEFAULT 0,
            payouts_sum REAL DEFAULT 0.0,
            rating INTEGER DEFAULT 55
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            deal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            channel_id INTEGER,
            channel_title TEXT,
            subs_count INTEGER,
            price REAL,
            payment_method TEXT,
            payment_details TEXT,
            status TEXT DEFAULT 'analyzed'
        )
    """)
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str = "User"):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username or "User"))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cur.fetchone()
    conn.close()
    return {
        "user_id": user[0], "username": user[1], "balance": user[2],
        "payouts_count": user[3], "payouts_sum": user[4], "rating": user[5]
    }

# ================= МАШИНА СОСТОЯНИЙ (FSM) =================
class SellStates(StatesGroup):
    manual_check = State()
    choosing_method = State()
    entering_details = State()

class FeedbackState(StatesGroup):
    writing_message = State()

class WithdrawState(StatesGroup):
    entering_requisites = State()

# ================= КЛАВИАТУРЫ =================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Продать Канал", callback_data="sell_channel")],
        [InlineKeyboardButton(text="➕ Купить Канал", callback_data="buy_channel")],
        [
            InlineKeyboardButton(text="🛒 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [InlineKeyboardButton(text="💭 Обратная Связь", callback_data="feedback")]
    ])

def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❕ Вывод средств", callback_data="withdraw")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
    ])

def sell_channel_kb(bot_username: str) -> InlineKeyboardMarkup:
    # DeepLink для быстрого добавления бота в админы канала с нужными правами
    add_bot_url = f"https://t.me/{bot_username}?startchannel=true&admin=ban_users+manage_chat"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота", url=add_bot_url)],
        [InlineKeyboardButton(text="✅ Бот добавлен", callback_data="manual_check_channel")],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
    ])

# ================= ГЛАВНОЕ МЕНЮ И ПРОФИЛЬ =================
def get_welcome_text(user: dict) -> str:
    return (
        "👋 <b>Добро пожаловать в FastBuy TG!</b>\n\n"
        "⚡ <b>FastBuy TG</b> — это автоматизированная площадка для быстрой оценки "
        "и моментального выкупа Telegram-каналов с аудиторией.\n\n"
        "<blockquote>🤖 <b>Авто-оценка 24/7:</b> Расчет цены за 10 секунд\n"
        "⚡ <b>Выплаты:</b> СБП и любые банковские карты\n"
        "🛡 <b>Безопасность:</b> Сделки под защитой системы</blockquote>\n\n"
        f"💰 Ваш баланс: <b>{user['balance']:.2f}₽</b>\n"
        f"🚩 Рейтинг продавца: <b>{user['rating']}%</b>\n\n"
        "👇 Главное меню:"
    )

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(get_welcome_text(user), reply_markup=main_menu_kb())

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(get_welcome_text(user), reply_markup=main_menu_kb())

@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    user = get_or_create_user(callback.from_user.id)
    balance_usd = user['balance'] / USD_RATE
    payouts_usd = user['payouts_sum'] / USD_RATE
    
    text = (
        "👤 <b>Ваш Профиль:</b>\n\n"
        f"💰 Баланс: <b>{user['balance']:.2f}₽</b> ({balance_usd:.2f}$)\n"
        f"ℹ️ Выплаты: <b>{user['payouts_count']} шт.</b> | {user['payouts_sum']:.2f}₽ ({payouts_usd:.2f}$)\n\n"
        f"🚩 Рейтинг продавца: <b>{user['rating']}%</b>\n"
        "<blockquote>Рейтинг зависит от качества вашего трафика в проданных "
        "каналах, чем выше ваш рейтинг – тем более выгодные "
        "условия при продаже</blockquote>\n\n"
        "👇 Выберите действие:"
    )
    await callback.message.edit_text(text, reply_markup=profile_kb())

@dp.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(price) FROM deals WHERE status = 'completed'")
    deals_data = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    conn.close()

    total_deals = (deals_data[0] or 0) + 410
    total_sum = (deals_data[1] or 0.0) + 2150000.0
    total_users = users_count + 1680

    text = (
        "📊 <b>Статистика площадки FastBuy TG:</b>\n\n"
        f"👥 Продавцов и покупателей: <b>{total_users} чел.</b>\n"
        f"🤝 Выкуплено каналов: <b>{total_deals} шт.</b>\n"
        f"💸 Выплачено клиентам: <b>{total_sum:,.2f}₽</b>\n"
        f"⏱ Среднее время выплаты: <b>4 мин. 30 сек.</b>\n\n"
        "<blockquote>Мы ежедневно выкупаем каналы любых объемов в автоматическом режиме 24/7!</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="main_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "buy_channel")
async def cb_buy_channel(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT channel_title, subs_count, price FROM deals WHERE status = 'completed' ORDER BY deal_id DESC LIMIT 6")
    channels = cur.fetchall()
    conn.close()

    text = "🛍 <b>Биржа готовых каналов FastBuy TG:</b>\n\n"
    if not channels:
        text += "<i>На данный момент все проверенные каналы раскуплены. Загляните чуть позже!</i>\n"
    else:
        for idx, ch in enumerate(channels, 1):
            sell_price = ch[2] * 1.25  # Наценка магазина 25%
            text += f"{idx}. 📢 <b>{ch[0]}</b>\n   ├ 👥 Подписчиков: <b>{ch[1]}</b>\n   └ 💰 Цена: <b>{sell_price:.0f}₽</b>\n\n"
    
    text += f"💬 Для покупки канала напишите менеджеру: {MANAGER_USERNAME}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="main_menu")]])
    await callback.message.edit_text(text, reply_markup=kb)

# ================= ПРОДАЖА И АВТО-АНАЛИЗ КАНАЛА =================
@dp.callback_query(F.data == "sell_channel")
async def cb_sell_channel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    text = (
        "🛒 <b>Продажа и оценка канала</b>\n\n"
        "Чтобы выставить канал на продажу и получить моментальную оценку "
        "стоимости, добавьте бота в администраторы канала:\n\n"
        "• Нажмите кнопку <b>«➕ Добавить бота»</b> ниже\n"
        "• Выберите свой канал из списка\n"
        "• Подтвердите назначение администратором\n\n"
        "<blockquote>Алгоритм FastBuy TG мгновенно посчитает подписчиков, "
        "оценит параметры канала и предложит цену выкупа</blockquote>\n\n"
        "💸 После добавления бот автоматически пришлёт вам оценочную стоимость.\n\n"
        "➕ Если бот уже был добавлен ранее, а сообщение с оценкой не пришло "
        "само — нажмите <b>«✅ Бот добавлен»</b> и запустите проверку вручную."
    )
    await callback.message.edit_text(text, reply_markup=sell_channel_kb(bot_info.username))

async def analyze_and_send_offer(user_id: int, username: str, chat_id: int, chat_title: str, chat_username: str = None):
    """Универсальная функция оценки канала и формирования предложения о выкупе"""
    user = get_or_create_user(user_id, username)
    try:
        subs_count = await bot.get_chat_member_count(chat_id)
    except Exception:
        subs_count = 0

    # Расчет стоимости: Базовая ставка * Подписчики * (Рейтинг / 100) * Бонус за открытый канал
    type_multiplier = 1.15 if chat_username else 1.0
    estimated_price = round(subs_count * BASE_PRICE_PER_SUB * (user["rating"] / 100) * type_multiplier, 2)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deals (seller_id, channel_id, channel_title, subs_count, price, status) VALUES (?, ?, ?, ?, ?, 'analyzed')",
        (user_id, chat_id, chat_title, subs_count, estimated_price)
    )
    deal_id = cur.lastrowid
    conn.commit()
    conn.close()

    ch_type = "Публичный (+15% к цене)" if chat_username else "Приватный"
    text = (
        f"✅ <b>Анализ канала завершен!</b>\n\n"
        f"📢 Канал: <b>{chat_title}</b>\n"
        f"🌐 Тип: <b>{ch_type}</b>\n"
        f"👥 Подписчиков: <b>{subs_count} чел.</b>\n"
        f"🚩 Ваш рейтинг продавца: <b>{user['rating']}%</b>\n\n"
        f"💰 <b>Оценочная стоимость канала: {estimated_price:.2f}₽</b>\n\n"
        "<blockquote>Мы готовы выкупить ваш канал прямо сейчас! Нажмите кнопку ниже, чтобы указать реквизиты (СБП или Карта) для получения выплаты.</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💸 Продать канал за {estimated_price:.2f}₽", callback_data=f"start_sell_{deal_id}")],
        [InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")]
    ])
    await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)

# 1. Автоматическая проверка при добавлении бота в канал
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> ADMINISTRATOR))
async def bot_added_to_channel(event: ChatMemberUpdated):
    if event.chat.type != "channel":
        return
    await analyze_and_send_offer(
        user_id=event.from_user.id,
        username=event.from_user.username,
        chat_id=event.chat.id,
        chat_title=event.chat.title,
        chat_username=event.chat.username
    )

# 2. Ручная проверка по кнопке «✅ Бот добавлен»
@dp.callback_query(F.data == "manual_check_channel")
async def cb_manual_check(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SellStates.manual_check)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="sell_channel")]])
    await callback.message.edit_text(
        "🔍 <b>Ручная проверка канала</b>\n\n"
        "Если вы уже добавили бота в администраторы канала, просто <b>перешлите сюда любое сообщение (пост) из вашего канала</b> или отправьте его <code>@username</code>:",
        reply_markup=kb
    )

@dp.message(SellStates.manual_check)
async def process_manual_check(message: Message, state: FSMContext):
    chat_id = None
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        chat_id = message.forward_from_chat.id
    elif message.text and (message.text.startswith("@") or message.text.startswith("-100")):
        chat_id = message.text.strip()
    else:
        return await message.answer("❌ Пожалуйста, перешлите пост из вашего канала или отправьте его @username.")

    try:
        chat = await bot.get_chat(chat_id)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            return await message.answer("❌ Бот найден в канале, но он не назначен администратором! Добавьте его в админы и попробуйте снова.")
        
        await state.clear()
        await analyze_and_send_offer(
            user_id=message.from_user.id,
            username=message.from_user.username,
            chat_id=chat.id,
            chat_title=chat.title,
            chat_username=chat.username
        )
    except Exception:
        await message.answer("❌ Не удалось получить доступ к каналу. Убедитесь, что вы добавили бота в администраторы канала!")

# ================= СБОР РЕКВИЗИТОВ (СБП И КАРТА) =================
@dp.callback_query(F.data.startswith("start_sell_"))
async def cb_start_sell(callback: CallbackQuery, state: FSMContext):
    deal_id = int(callback.data.split("_")[2])
    await state.update_data(deal_id=deal_id)
    await state.set_state(SellStates.choosing_method)

    text = (
        "💳 <b>Куда отправить выплату за канал?</b>\n\n"
        "Выберите удобный способ получения денежных средств:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 СБП (По номеру телефона)", callback_data="paymethod_СБП"),
            InlineKeyboardButton(text="💳 Банковская Карта", callback_data="paymethod_КАРТА")
        ],
        [InlineKeyboardButton(text="Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(SellStates.choosing_method, F.data.startswith("paymethod_"))
async def cb_choose_paymethod(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    await state.update_data(payment_method=method)
    await state.set_state(SellStates.entering_details)

    if method == "СБП":
        prompt = (
            "📱 <b>Выплата по СБП (Система быстрых платежей)</b>\n\n"
            "Пришлите ваши реквизиты для выплаты одним сообщением (<b>номер телефона и название банка</b>):\n\n"
            "📌 <i>Пример:</i> <code>+79991234567 Сбербанк / Т-Банк</code>"
        )
    else:
        prompt = (
            "💳 <b>Выплата на Банковскую карту</b>\n\n"
            "Пришлите ваши реквизиты для выплаты одним сообщением (<b>номер карты, телефон СБП и название банка</b>):\n\n"
            "📌 <i>Пример:</i> <code>2202 2000 0000 0000 Сбербанк (+79991234567)</code>"
        )

    await callback.message.edit_text(prompt)

@dp.message(SellStates.entering_details)
async def process_payment_details(message: Message, state: FSMContext):
    details = message.text.strip()
    data = await state.get_data()
    deal_id = data["deal_id"]
    method = data["payment_method"]

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "UPDATE deals SET payment_method = ?, payment_details = ?, status = 'waiting_transfer' WHERE deal_id = ?",
        (method, details, deal_id)
    )
    cur.execute("SELECT channel_title, subs_count, price FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    conn.commit()
    conn.close()

    await state.clear()

    text = (
        "✅ <b>Ваши реквизиты приняты!</b>\n\n"
        f"📢 Канал: <b>{deal[0]}</b> ({deal[1]} подп.)\n"
        f"💰 Сумма к выплате: <b>{deal[2]:.2f}₽</b>\n"
        f"🏦 Реквизиты ({method}): <code>{details}</code>\n\n"
        "🛡 <b>Финальный шаг для завершения сделки:</b>\n"
        "1. Зайдите в настройки вашего канала -> <b>Администраторы</b>.\n"
        "2. Выдайте нашему боту все права администратора (включая <i>«Блокировка пользователей»</i>).\n"
        f"3. Передайте права <b>Владельца канала</b> нашему гарант-менеджеру: {MANAGER_USERNAME}\n\n"
        "👇 Сразу после передачи прав нажмите кнопку <b>«✅ Я передал канал»</b> — система проверит права и отправит вам выплату!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я передал канал (Получить выплату)", callback_data=f"verify_transfer_{deal_id}")],
        [InlineKeyboardButton(text="Назад в меню", callback_data="main_menu")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("verify_transfer_"))
async def cb_verify_transfer(callback: CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT seller_id, channel_id, channel_title, subs_count, price, payment_method, payment_details FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    conn.close()

    if not deal:
        return await callback.answer("Сделка не найдена!", show_alert=True)

    await callback.message.edit_text(
        "⏳ <b>Проверка передачи канала запущена!</b>\n\n"
        f"📢 Канал: <b>{deal[2]}</b>\n"
        f"💰 Сумма выплаты: <b>{deal[4]:.2f}₽</b>\n"
        f"💳 Ваши реквизиты ({deal[5]}): <code>{deal[6]}</code>\n\n"
        "<blockquote>Ожидайте! Как только система подтвердит смену владельца, средства моментально поступят по вашим реквизитам.</blockquote>"
    )

    # Уведомление админу с кнопками управления и авто-кика!
    admin_text = (
        f"🔥 <b>ЗАЯВКА НА ВЫКУП КАНАЛА #{deal_id}</b>\n\n"
        f"👤 Продавец: <a href='tg://user?id={deal[0]}'>Пользователь {deal[0]}</a>\n"
        f"📢 Канал: <b>{deal[2]}</b> (ID: <code>{deal[1]}</code>)\n"
        f"👥 Подписчиков: <b>{deal[3]} чел.</b>\n"
        f"💰 Сумма к выплате: <b>{deal[4]:.2f}₽</b>\n\n"
        f"🏦 <b>Реквизиты продавца ({deal[5]}):</b>\n"
        f"<code>{deal[6]}</code>\n\n"
        "👇 <i>Панель управления сделкой:</i>"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👢 Проверить и КИКНУТЬ продавца из канала", callback_data=f"admin_kick_{deal_id}")],
        [InlineKeyboardButton(text="✅ Выплачено на реквизиты (+ Авто-кик)", callback_data=f"admin_pay_direct_{deal_id}")],
        [InlineKeyboardButton(text="💼 Зачислить на баланс в боте (+ Авто-кик)", callback_data=f"admin_pay_balance_{deal_id}")],
        [InlineKeyboardButton(text="❌ Отклонить сделку", callback_data=f"admin_reject_{deal_id}")]
    ])
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения админу: {e}")

# ================= АДМИН-ДЕЙСТВИЯ: АВТО-КИК ВЛАДЕЛЬЦА И ВЫПЛАТЫ =================
async def kick_seller_from_channel(channel_id: int, seller_id: int) -> tuple[bool, str]:
    """Проверяет статус продавца и автоматически удаляет его из канала"""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=seller_id)
        if member.status == ChatMemberStatus.CREATOR:
            return False, "Продавец всё ещё числится Создателем (Владельцем) канала! Сначала он должен передать права владельца."
        
        # Кикаем (блокируем и разблокируем, чтобы удалить из канала и снять админку)
        await bot.ban_chat_member(chat_id=channel_id, user_id=seller_id)
        return True, "✅ Продавец успешно кикнут и удален из канала!"
    except Exception as e:
        return False, f"Ошибка кика (проверьте права бота в канале): {e}"

@dp.callback_query(F.data.startswith("admin_kick_"))
async def cb_admin_kick(callback: CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT seller_id, channel_id FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    conn.close()

    success, msg = await kick_seller_from_channel(deal[1], deal[0])
    await callback.answer(msg, show_alert=True)

@dp.callback_query(F.data.startswith("admin_pay_"))
async def cb_admin_complete_deal(callback: CallbackQuery):
    parts = callback.data.split("_")
    pay_type = parts[2]  # 'direct' (на реквизиты) или 'balance' (на баланс бота)
    deal_id = int(parts[3])

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT seller_id, channel_id, channel_title, price, payment_method, payment_details FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    seller_id, channel_id, channel_title, price, method, details = deal

    # Автоматический кик бывшего владельца при успешной продаже
    kicked, kick_msg = await kick_seller_from_channel(channel_id, seller_id)

    cur.execute("UPDATE deals SET status = 'completed' WHERE deal_id = ?", (deal_id,))
    if pay_type == "balance":
        cur.execute(
            "UPDATE users SET balance = balance + ?, payouts_count = payouts_count + 1, payouts_sum = payouts_sum + ?, rating = MIN(100, rating + 5) WHERE user_id = ?",
            (price, price, seller_id)
        )
        seller_notify = (
            f"🎉 <b>Сделка по каналу «{channel_title}» успешно завершена!</b>\n\n"
            f"💰 На ваш баланс в боте зачислено: <b>{price:.2f}₽</b>\n"
            f"🚩 Ваш рейтинг продавца вырос на <b>+5%</b>!\n\n"
            "Вы можете вывести средства в разделе «🛒 Профиль» -> «❕ Вывод средств»."
        )
    else:
        cur.execute(
            "UPDATE users SET payouts_count = payouts_count + 1, payouts_sum = payouts_sum + ?, rating = MIN(100, rating + 5) WHERE user_id = ?",
            (price, seller_id)
        )
        seller_notify = (
            f"🎉 <b>Сделка по каналу «{channel_title}» успешно завершена!</b>\n\n"
            f"⚡ Выплата в размере <b>{price:.2f}₽</b> отправлена по вашим реквизитам:\n"
            f"🏦 <b>{method}:</b> <code>{details}</code>\n\n"
            f"🚩 Ваш рейтинг продавца повышен на <b>+5%</b>! Спасибо за работу с FastBuy TG."
        )

    conn.commit()
    conn.close()

    await callback.message.edit_text(
        f"✅ <b>Сделка #{deal_id} закрыта!</b>\n"
        f"📢 Канал: <b>{channel_title}</b>\n"
        f"💸 Сумма: <b>{price:.2f}₽</b> ({'На баланс бота' if pay_type == 'balance' else f'{method}: {details}'})\n"
        f"👢 Авто-кик продавца: <b>{kick_msg}</b>"
    )
    await bot.send_message(seller_id, seller_notify)

@dp.callback_query(F.data.startswith("admin_reject_"))
async def cb_admin_reject(callback: CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE deals SET status = 'rejected' WHERE deal_id = ?", (deal_id,))
    cur.execute("SELECT seller_id, channel_title FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"❌ Сделка #{deal_id} (Канал: {deal[1]}) отклонена.")
    await bot.send_message(deal[0], f"❌ Сделка по продаже канала <b>«{deal[1]}»</b> была отменена (права на канал не были переданы).")

# ================= ВЫВОД С БАЛАНСА И ОБРАТНАЯ СВЯЗЬ =================
@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext):
    user = get_or_create_user(callback.from_user.id)
    if user["balance"] <= 0:
        return await callback.answer("❌ На вашем балансе пока нет средств для вывода (0.00₽)", show_alert=True)
    
    await state.set_state(WithdrawState.entering_requisites)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="profile")]])
    await callback.message.edit_text(
        f"💸 <b>Вывод средств с баланса</b>\n\n"
        f"Доступно к выводу: <b>{user['balance']:.2f}₽</b>\n\n"
        "Пришлите ваши реквизиты одним сообщением (СБП или номер карты + номер телефона + название банка):",
        reply_markup=kb
    )

@dp.message(WithdrawState.entering_requisites)
async def process_withdraw(message: Message, state: FSMContext):
    user = get_or_create_user(message.from_user.id)
    amount = user["balance"]
    if amount <= 0:
        await state.clear()
        return await message.answer("Ваш баланс равен 0₽.", reply_markup=main_menu_kb())

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (user["user_id"],))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(
        f"✅ <b>Заявка на вывод {amount:.2f}₽ успешно создана!</b>\n"
        f"Реквизиты: <code>{message.text}</code>\n\n"
        "Выплата поступит в течение нескольких минут.",
        reply_markup=main_menu_kb()
    )
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"💳 <b>ЗАЯВКА НА ВЫВОД С БАЛАНСА</b>\n\n"
            f"👤 Пользователь: <a href='tg://user?id={user['user_id']}'>ID {user['user_id']}</a>\n"
            f"💰 Сумма: <b>{amount:.2f}₽</b>\n"
            f"🏦 Реквизиты: <code>{message.text}</code>"
        )

@dp.callback_query(F.data == "feedback")
async def cb_feedback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackState.writing_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="main_menu")]])
    await callback.message.edit_text(
        "💭 <b>Служба поддержки FastBuy TG</b>\n\n"
        "Напишите ваш вопрос или предложение одним сообщением, и наш менеджер ответит вам в ближайшее время:",
        reply_markup=kb
    )

@dp.message(FeedbackState.writing_message)
async def process_feedback(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Ваше обращение отправлено! Ожидайте ответа оператора.", reply_markup=main_menu_kb())
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"📩 <b>Обращение в поддержку</b>\n"
            f"От: <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a> (ID: <code>{message.from_user.id}</code>)\n\n"
            f"Текст: {message.text}"
        )

# ================= КОМАНДЫ АДМИНИСТРАТОРА (/admin) =================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    u_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM deals WHERE status = 'waiting_transfer'")
    d_cnt = cur.fetchone()[0]
    conn.close()

    text = (
        "👑 <b>Админ-панель FastBuy TG</b>\n\n"
        f"👥 Пользователей в базе: <b>{u_cnt}</b>\n"
        f"⏳ Активных сделок на проверке: <b>{d_cnt}</b>\n\n"
        "<b>Быстрые команды:</b>\n"
        "• <code>/setrating ID ПРОЦЕНТ</code> — изменить рейтинг продавца\n"
        "• <code>/addbalance ID СУММА</code> — пополнить баланс юзера\n"
        "• <code>/send ID ТЕКСТ</code> — написать сообщение пользователю от лица бота"
    )
    await message.answer(text)

@dp.message(Command("setrating"))
async def cmd_setrating(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, rating = message.text.split()
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("UPDATE users SET rating = ? WHERE user_id = ?", (int(rating), int(uid)))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Рейтинг продавца <code>{uid}</code> установлен на <b>{rating}%</b>!")
    except Exception:
        await message.answer("Формат команды: <code>/setrating ID ПРОЦЕНТ</code>")

@dp.message(Command("addbalance"))
async def cmd_addbalance(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, amount = message.text.split()
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (float(amount), int(uid)))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Баланс пользователя <code>{uid}</code> пополнен на <b>{amount}₽</b>!")
        await bot.send_message(int(uid), f"💰 Ваш баланс пополнен администратором на <b>{float(amount):.2f}₽</b>!")
    except Exception:
        await message.answer("Формат команды: <code>/addbalance ID СУММА</code>")

@dp.message(Command("send"))
async def cmd_send_to_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split(maxsplit=2)
        uid = int(parts[1])
        reply_text = parts[2]
        await bot.send_message(uid, f"👨‍💻 <b>Сообщение от поддержки FastBuy TG:</b>\n\n{reply_text}")
        await message.answer(f"✅ Сообщение отправлено пользователю <code>{uid}</code>!")
    except Exception:
        await message.answer("Формат команды: <code>/send ID ТЕКСТ_СООБЩЕНИЯ</code>")

# ================= ЗАПУСК БОТА =================
async def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    print("⚡ Бот FastBuy TG успешно запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
