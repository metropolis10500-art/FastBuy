#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 ДругоКэш — реферальный бот «Зарабатывай на друзьях»
 Один файл. Всё в БД SQLite рядом с ботом (data/bot.db).

 Запуск:   python bot.py
 Настройки: файл .env (см. .env.example)

 Зависимости: pip install -r requirements.txt
 (aiogram>=3.20, aiohttp, python-dotenv)
=====================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

# =====================================================================
#  1. КОНФИГУРАЦИЯ (.env)
# =====================================================================

load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env_str(name, str(default))))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    return env_str(name, "1" if default else "0").lower() in ("1", "true", "yes", "on", "да")


def rubles_to_kop(value: float) -> int:
    """12.345 -> 1234 (копейки). Округление до копейки."""
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return 0


BOT_TOKEN = env_str("BOT_TOKEN")
ADMIN_IDS: list[int] = [
    int(x) for x in env_str("ADMIN_IDS", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
]

BOT_NAME = env_str("BOT_NAME", "ДругоКэш")

# --- Поддержка (ваша личная ссылка, никак не связана с названием бота) ---
SUPPORT_URL = env_str("SUPPORT_URL", "https://t.me/vladofix97")
SUPPORT_USERNAME = env_str("SUPPORT_USERNAME", "@vladofix97")

# --- Цены за друзей (в рублях). На скриншотах было 9,30 / 5,70 / 2,10 ---
PRICE_1 = rubles_to_kop(env_float("REF_L1", 12))
PRICE_2 = rubles_to_kop(env_float("REF_L2", 7))
PRICE_3 = rubles_to_kop(env_float("REF_L3", 3))

# --- Ежедневный бонус ---
BONUS_AMOUNT = rubles_to_kop(env_float("BONUS_AMOUNT", 25))
BONUS_COOLDOWN_HOURS = env_int("BONUS_COOLDOWN_HOURS", 24)

# --- Вывод средств ---
MIN_WITHDRAW = rubles_to_kop(env_float("MIN_WITHDRAW", 3000))
WITHDRAW_COMMISSION = env_float("WITHDRAW_COMMISSION_PERCENT", 0)  # %, по умолчанию 0

# --- Реклама ---
AD_PRICE = rubles_to_kop(env_float("AD_PRICE", 5000))
AD_DURATION_HOURS = env_int("AD_DURATION_HOURS", 72)
AD_CHANNEL_ID = env_str("AD_CHANNEL_ID")  # @channel или -100..., куда бот публикует рекламу
AD_MAX_TEXT = env_int("AD_MAX_TEXT", 900)

# --- ЮMoney ---
YOOMONEY_WALLET = env_str("YOOMONEY_WALLET")
YOOMONEY_TOKEN = env_str("YOOMONEY_TOKEN")
YOOMONEY_RECEIVER = env_str("YOOMONEY_RECEIVER") or YOOMONEY_WALLET
YOOMONEY_POLL_SECONDS = env_int("YOOMONEY_POLL_SECONDS", 30)
YOOMONEY_AUTOPAYOUT = env_bool("YOOMONEY_AUTOPAYOUT", False)
TOPUP_MIN = rubles_to_kop(env_float("TOPUP_MIN", 500))
TOPUP_MAX = rubles_to_kop(env_float("TOPUP_MAX", 50000))

# --- Прочее ---
DB_PATH = env_str("DB_PATH", "data/bot.db")
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()
TZ_OFFSET = env_int("TZ_OFFSET", 3)  # Москва = +3
MSK = timezone(timedelta(hours=TZ_OFFSET))
REF_ALERT_PER_HOUR = env_int("REF_ALERT_PER_HOUR", 0)  # 0 = не уведомлять админов о всплесках

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("referral-bot")

# =====================================================================
#  2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def money(sum_kop: int | None) -> str:
    """1234 -> '12,34'"""
    sum_kop = int(sum_kop or 0)
    return f"{sum_kop / 100:,.2f}".replace(",", " ").replace(".", ",")


def rub(sum_kop: int | None) -> str:
    return f"{money(sum_kop)} ₽"


def fmt_date(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y")


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def human_left(target: datetime | None) -> str:
    """Сколько осталось до target."""
    if not target:
        return "сейчас"
    left = int((target - now_utc()).total_seconds())
    if left <= 0:
        return "сейчас"
    h, m = divmod(left // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def esc(text: str | None) -> str:
    """Экранирование для HTML-разметки."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def btn(text: str, cb: str | None = None, *, url: str | None = None,
        copy: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    """Универсальная кнопка: callback / ссылка / «скопировать текст»."""
    kwargs: dict[str, Any] = {"text": text}
    if style:
        kwargs["style"] = style
    if cb:
        kwargs["callback_data"] = cb
    elif url:
        kwargs["url"] = url
    elif copy:
        kwargs["copy_text"] = CopyTextButton(text=copy)
    return InlineKeyboardButton(**kwargs)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


# =====================================================================
#  3. БАЗА ДАННЫХ (SQLite)
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    joined_at     TEXT NOT NULL,
    referrer_id   INTEGER,
    balance       INTEGER NOT NULL DEFAULT 0,   -- копейки
    earned_ref    INTEGER NOT NULL DEFAULT 0,   -- заработано на друзьях
    earned_bonus  INTEGER NOT NULL DEFAULT 0,   -- заработано на бонусах
    paid_out      INTEGER NOT NULL DEFAULT 0,   -- выплачено всего
    bonus_last    TEXT,
    is_banned     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referrer_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,   -- списано с баланса
    net_amount   INTEGER NOT NULL,   -- к выплате после комиссии
    method       TEXT NOT NULL,      -- card / sbp / yoomoney
    details      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/paid/rejected
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    admin_chat   INTEGER,
    admin_msg    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, status);

CREATE TABLE IF NOT EXISTS ads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    price        INTEGER NOT NULL,
    text         TEXT,
    link         TEXT,
    media_type   TEXT,               -- photo / None
    media_id     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected/posted/expired/cancelled
    created_at   TEXT NOT NULL,
    posted_at    TEXT,
    expires_at   TEXT,
    payload      TEXT,               -- JSON со списком [chat_id, message_id]
    admin_chat   INTEGER,
    admin_msg    INTEGER
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    label       TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/paid
    created_at  TEXT NOT NULL,
    paid_at     TEXT,
    op_id       TEXT
);

CREATE TABLE IF NOT EXISTS images (
    key     TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    author     TEXT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    user_id    INTEGER,
    amount     INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events ON events(kind, created_at);
"""


class Database:
    """Простая обёртка над sqlite3. Запросы быстрые, локальный файл."""

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------- низкий уровень ----------
    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def ex(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid or cur.rowcount

    # ---------- пользователи ----------
    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def create_user(self, user_id: int, username: str | None, first_name: str | None,
                    referrer_id: int | None) -> None:
        self.ex(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at, referrer_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, iso(now_utc()), referrer_id),
        )

    def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        self.ex("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id))

    def add_balance(self, user_id: int, amount: int, kind: str = "other") -> None:
        field = {"ref": "earned_ref", "bonus": "earned_bonus"}.get(kind)
        if field:
            self.ex(f"UPDATE users SET balance = balance + ?, {field} = {field} + ? WHERE user_id = ?",
                    (amount, amount, user_id))
        else:
            self.ex("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))

    def spend_balance(self, user_id: int, amount: int) -> bool:
        """Атомарное списание. True — если хватило денег."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, user_id, amount),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def set_banned(self, user_id: int, banned: bool) -> None:
        self.ex("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(banned), user_id))

    def all_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self.q("SELECT user_id FROM users")]

    def count_users(self) -> int:
        return int(self.q1("SELECT COUNT(*) c FROM users")["c"])

    def count_users_since(self, dt: datetime) -> int:
        return int(self.q1("SELECT COUNT(*) c FROM users WHERE joined_at >= ?", (iso(dt),))["c"])

    def sum_balances(self) -> int:
        return int(self.q1("SELECT COALESCE(SUM(balance),0) s FROM users")["s"])

    def sum_paid_out(self) -> int:
        return int(self.q1("SELECT COALESCE(SUM(paid_out),0) s FROM users")["s"])

    def sum_pending_withdraw(self) -> int:
        return int(self.q1("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='pending'")["s"])

    # ---------- уровни / друзья ----------
    def level_stats(self, user_id: int) -> dict[int, int]:
        """{1: кол-во, 2: кол-во, 3: кол-во} по трём уровням."""
        rows = self.q(
            """
            WITH RECURSIVE lvl(uid, depth) AS (
                SELECT user_id, 1 FROM users WHERE referrer_id = ?
                UNION ALL
                SELECT u.user_id, lvl.depth + 1
                FROM users u JOIN lvl ON u.referrer_id = lvl.uid
                WHERE lvl.depth < 3
            )
            SELECT depth, COUNT(*) AS cnt FROM lvl GROUP BY depth
            """,
            (user_id,),
        )
        res = {1: 0, 2: 0, 3: 0}
        for r in rows:
            if r["depth"] in res:
                res[int(r["depth"])] = int(r["cnt"])
        return res

    def friends_today(self, user_id: int) -> int:
        return int(self.q1(
            "SELECT COUNT(*) c FROM users WHERE referrer_id = ? AND date(joined_at) = date('now')",
            (user_id,),
        )["c"])

    def refs_last_hour(self, user_id: int) -> int:
        since = iso(now_utc() - timedelta(hours=1))
        return int(self.q1(
            "SELECT COUNT(*) c FROM users WHERE referrer_id = ? AND joined_at >= ?",
            (user_id, since),
        )["c"])

    # ---------- вывод средств ----------
    def create_withdrawal(self, user_id: int, amount: int, net_amount: int,
                          method: str, details: str) -> int:
        return self.ex(
            "INSERT INTO withdrawals (user_id, amount, net_amount, method, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount, net_amount, method, details, iso(now_utc())),
        )

    def get_withdrawal(self, wd_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM withdrawals WHERE id = ?", (wd_id,))

    def pending_withdraw_sum(self, user_id: int) -> int:
        return int(self.q1(
            "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE user_id = ? AND status='pending'",
            (user_id,),
        )["s"])

    def pending_withdraw_total(self, user_id: int) -> int:
        return int(self.q1(
            "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE user_id = ? AND status='paid'",
            (user_id,),
        )["s"])

    def user_withdrawals(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                      (user_id, limit))

    def set_withdrawal_status(self, wd_id: int, status: str) -> None:
        self.ex("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                (status, iso(now_utc()), wd_id))

    def set_withdrawal_msg(self, wd_id: int, chat_id: int, msg_id: int) -> None:
        self.ex("UPDATE withdrawals SET admin_chat = ?, admin_msg = ? WHERE id = ?",
                (chat_id, msg_id, wd_id))

    def add_paid_out(self, user_id: int, amount: int) -> None:
        self.ex("UPDATE users SET paid_out = paid_out + ? WHERE user_id = ?", (amount, user_id))

    def pending_withdrawals(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id ASC LIMIT ?", (limit,))

    # ---------- бонус ----------
    def bonus_available_at(self, user_id: int) -> datetime:
        row = self.get_user(user_id)
        last = parse_iso(row["bonus_last"]) if row else None
        if not last:
            return now_utc()
        return last + timedelta(hours=BONUS_COOLDOWN_HOURS)

    def claim_bonus(self, user_id: int, amount: int) -> bool:
        """Начисляет бонус, если 24 часа прошли. Атомарно через условие в SQL."""
        threshold = iso(now_utc() - timedelta(hours=BONUS_COOLDOWN_HOURS))
        with self.lock:
            cur = self.conn.execute(
                "UPDATE users SET balance = balance + ?, earned_bonus = earned_bonus + ?, bonus_last = ? "
                "WHERE user_id = ? AND is_banned = 0 AND (bonus_last IS NULL OR bonus_last <= ?)",
                (amount, amount, iso(now_utc()), user_id, threshold),
            )
            self.conn.commit()
            return cur.rowcount == 1

    # ---------- картинки меню ----------
    def get_image(self, key: str) -> Optional[str]:
        row = self.q1("SELECT file_id FROM images WHERE key = ?", (key,))
        return row["file_id"] if row else None

    def set_image(self, key: str, file_id: str) -> None:
        self.ex("INSERT INTO images (key, file_id) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET file_id = excluded.file_id", (key, file_id))

    def del_image(self, key: str) -> None:
        self.ex("DELETE FROM images WHERE key = ?", (key,))

    def images_map(self) -> dict[str, str]:
        return {r["key"]: r["file_id"] for r in self.q("SELECT key, file_id FROM images")}

    # ---------- отзывы ----------
    def add_review(self, author: str, text: str, user_id: int | None = None) -> int:
        return self.ex("INSERT INTO reviews (user_id, author, text, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, author, text, iso(now_utc())))

    def del_review(self, review_id: int) -> None:
        self.ex("DELETE FROM reviews WHERE id = ?", (review_id,))

    def reviews(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,))

    def reviews_count(self) -> int:
        return int(self.q1("SELECT COUNT(*) c FROM reviews")["c"])

    # ---------- реклама ----------
    def create_ad(self, user_id: int, price: int, text: str | None, link: str | None,
                  media_type: str | None, media_id: str | None) -> int:
        return self.ex(
            "INSERT INTO ads (user_id, price, text, link, media_type, media_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, price, text, link, media_type, media_id, iso(now_utc())),
        )

    def get_ad(self, ad_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM ads WHERE id = ?", (ad_id,))

    def set_ad_status(self, ad_id: int, status: str, **fields: Any) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(ad_id)
        self.ex(f"UPDATE ads SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def set_ad_msg(self, ad_id: int, chat_id: int, msg_id: int) -> None:
        self.ex("UPDATE ads SET admin_chat = ?, admin_msg = ? WHERE id = ?", (chat_id, msg_id, ad_id))

    def ads_by_status(self, status: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM ads WHERE status = ? ORDER BY id ASC LIMIT ?", (status, limit))

    def ads_to_expire(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM ads WHERE status IN ('posted','approved') AND expires_at IS NOT NULL "
                      "AND expires_at <= ?", (iso(now_utc()),))

    def user_active_ad(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM ads WHERE user_id = ? AND status IN ('pending','approved','posted') "
                       "ORDER BY id DESC LIMIT 1", (user_id,))

    # ---------- платежи ЮMoney ----------
    def create_payment(self, user_id: int, amount: int, label: str) -> int:
        return self.ex("INSERT INTO payments (user_id, amount, label, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, amount, label, iso(now_utc())))

    def get_payment(self, label: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM payments WHERE label = ?", (label,))

    def pending_payments(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM payments WHERE status = 'pending' ORDER BY id ASC")

    def mark_payment_paid(self, label: str, op_id: str | None, amount: int | None = None) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE payments SET status = 'paid', paid_at = ?, op_id = ? "
                "WHERE label = ? AND status = 'pending'",
                (iso(now_utc()), op_id, label),
            )
            self.conn.commit()
            if cur.rowcount != 1:
                return False
            row = self.q1("SELECT user_id, amount FROM payments WHERE label = ?", (label,))
        self.add_balance(int(row["user_id"]), int(amount or row["amount"]))
        return True

    def user_payments(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                      (user_id, limit))

    # ---------- события ----------
    def log_event(self, kind: str, user_id: int | None = None, amount: int | None = None) -> None:
        self.ex("INSERT INTO events (kind, user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                (kind, user_id, amount, iso(now_utc())))


db = Database(DB_PATH)


# =====================================================================
#  4. БОТ, РОУТЕРЫ, ЭКРАНЫ
# =====================================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

BOT_USERNAME: str = ""  # заполняется при старте (нужно для реф-ссылки)

# Ключи картинок для меню — их можно задать в админке, подгрузив фото
IMAGE_KEYS = {
    "main": "🏠 Главное меню",
    "link": "🔗 Моя ссылка",
    "profile": "👤 Профиль",
    "bonus": "🎁 Бонус",
    "withdraw": "💸 Вывод средств",
    "reviews": "⭐ Отзывы",
    "howto": "❓ Как это работает",
    "ads": "📢 Заказать рекламу",
}

CAPTION_LIMIT = 1000  # у фото ограничение подписи (1024), берём с запасом


def ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


# ---------------------------- экраны ---------------------------------


async def send_screen(target: Message, key: str, text: str, markup: InlineKeyboardMarkup) -> None:
    """Отправить новый экран (с картинкой меню, если она загружена)."""
    file_id = db.get_image(key)
    if file_id and len(text) <= CAPTION_LIMIT:
        try:
            await target.answer_photo(file_id, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            log.warning("Не удалось отправить фото (%s): %s", key, e)
    await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def edit_screen(cb: CallbackQuery, key: str, text: str, markup: InlineKeyboardMarkup) -> None:
    """Плавно перерисовать текущий экран (фото <-> текст)."""
    msg = cb.message
    if msg is None:
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup,
                                  disable_web_page_preview=True)
        return
    file_id = db.get_image(key)
    text = text[:4000]
    try:
        if file_id and len(text) <= CAPTION_LIMIT:
            if getattr(msg, "photo", None):
                await msg.edit_media(
                    InputMediaPhoto(media=file_id, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=markup,
                )
                return
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            await msg.answer_photo(file_id, caption=text, reply_markup=markup)
            return

        if getattr(msg, "photo", None):
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
            return

        await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        log.warning("edit_screen fallback (%s): %s", key, e)
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------------------------- клавиатуры -----------------------------


def main_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("🔗 Моя ссылка", "menu:link", style="success")],
        [btn("👤 Профиль", "menu:profile", style="primary"),
         btn("🎁 Бонус", "menu:bonus", style="success")],
        [btn("💸 Вывод средств", "menu:withdraw", style="primary")],
        [btn("⭐ Отзывы", "menu:reviews", style="primary"),
         btn("❓ Как это работает", "menu:howto", style="primary")],
        [btn("📢 Заказать рекламу", "menu:ads", style="success")],
        [btn("🆘 Поддержка", url=SUPPORT_URL, style="danger")],
    ]
    if is_admin(user_id):
        rows.append([btn("⚙️ Админ-панель", "admin:home", style="danger")])
    return kb(*rows)


def back_kb(to: str = "menu:main") -> list[InlineKeyboardButton]:
    return [btn("◀️ Назад", to, style="primary")]


# ---------------------------- тексты ---------------------------------


def user_levels(user_id: int) -> dict[str, int]:
    """Считает друзей и заработок по уровням."""
    lv = db.level_stats(user_id)
    return {
        "c1": lv[1], "c2": lv[2], "c3": lv[3],
        "e1": lv[1] * PRICE_1, "e2": lv[2] * PRICE_2, "e3": lv[3] * PRICE_3,
    }


def text_main(user_id: int, first_name: str) -> str:
    row = db.get_user(user_id)
    balance = int(row["balance"]) if row else 0
    return (
        f"👋 <b>Привет, {esc(first_name)}!</b>\n\n"
        f"Приглашай друзей по своей ссылке и получай деньги за каждого:\n\n"
        f" • <b>{money(PRICE_1)} ₽</b> — за друга\n"
        f" • <b>{money(PRICE_2)} ₽</b> — за друга твоего друга\n"
        f" • <b>{money(PRICE_3)} ₽</b> — за друга 3-го уровня\n\n"
        f"💰 <b>Баланс: {money(balance)} ₽</b>"
    )


def text_link(user_id: int) -> str:
    lv = user_levels(user_id)
    total = lv["c1"] + lv["c2"] + lv["c3"]
    return (
        f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{ref_link(user_id)}</code>\n\n"
        f"За каждого друга, который запустит бота по ссылке — <b>{money(PRICE_1)} ₽</b> "
        f"и ещё по цепочке:\n"
        f"• 2 уровень (друзья друзей) — <b>{money(PRICE_2)} ₽</b>\n"
        f"• 3 уровень — <b>{money(PRICE_3)} ₽</b>\n\n"
        f"👥 Друзей: <b>{total}</b> (сегодня +{db.friends_today(user_id)})\n\n"
        f"Поделитесь ссылкой в чатах, с друзьями или в своём канале 👇"
    )


def text_link_kb(user_id: int) -> InlineKeyboardMarkup:
    link = ref_link(user_id)
    share = "https://t.me/share/url?" + urlencode(
        {"url": link, "text": "Зарабатываю на друзьях в Telegram 💸 Заходи по моей ссылке:"}
    )
    return kb(
        [btn("📤 Поделиться", url=share, style="success")],
        [btn("📋 Скопировать ссылку", copy=link, style="primary")],
        back_kb(),
    )


def text_profile(user_id: int) -> str:
    row = db.get_user(user_id)
    if not row:
        return "Профиль не найден."
    lv = user_levels(user_id)
    pending = db.pending_withdraw_sum(user_id)
    total_earned = int(row["earned_ref"]) + int(row["earned_bonus"])
    return (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 В боте с {fmt_date(parse_iso(row['joined_at']))}\n\n"
        f"💰 Баланс: <b>{money(row['balance'])} ₽</b>\n"
        f"⏳ На выводе: <b>{money(pending)} ₽</b>\n"
        f"✅ Выплачено: <b>{money(row['paid_out'])} ₽</b>\n\n"
        f"🧾 Заработано всего: <b>{money(total_earned)} ₽</b>\n"
        f"• за друзей: {money(row['earned_ref'])} ₽\n"
        f"• за бонусы: {money(row['earned_bonus'])} ₽\n\n"
        f"👥 <b>Друзья</b> (сегодня +{db.friends_today(user_id)})\n"
        f"1 уровень: {lv['c1']} — заработано {money(lv['e1'])} ₽\n"
        f"2 уровень: {lv['c2']} — заработано {money(lv['e2'])} ₽\n"
        f"3 уровень: {lv['c3']} — заработано {money(lv['e3'])} ₽"
    )


def text_bonus(user_id: int) -> str:
    available = db.bonus_available_at(user_id)
    left = int((available - now_utc()).total_seconds())
    row = db.get_user(user_id)
    if left > 0:
        status = f"⏳ Следующий бонус через <b>{human_left(available)}</b>"
    else:
        status = "✅ Бонус доступен — забирайте!"
    return (
        f"🎁 <b>Ежедневный бонус</b>\n\n"
        f"Каждый день вы можете бесплатно получить <b>{money(BONUS_AMOUNT)} ₽</b> "
        f"на баланс.\n\n"
        f"{status}\n\n"
        f"💰 Баланс: <b>{money(row['balance'] if row else 0)} ₽</b>"
    )


def bonus_kb(user_id: int) -> InlineKeyboardMarkup:
    available = db.bonus_available_at(user_id)
    if available <= now_utc():
        return kb([btn(f"🎁 Забрать {money(BONUS_AMOUNT)} ₽", "bonus:claim", style="success")],
                  back_kb())
    return kb([btn(f"⏳ Через {human_left(available)}", "bonus:noop", style="primary")], back_kb())


def text_howto() -> str:
    return (
        f"❓ <b>Как это работает</b>\n\n"
        f"1️⃣ Берите свою ссылку в разделе «🔗 Моя ссылка»\n"
        f"2️⃣ Делитесь ей с друзьями, в чатах или своём канале\n"
        f"3️⃣ Получаете деньги на баланс:\n"
        f"• <b>{money(PRICE_1)} ₽</b> за каждого друга, который запустил бота по вашей ссылке\n"
        f"• <b>{money(PRICE_2)} ₽</b> за каждого, кого пригласил ваш друг (2 уровень)\n"
        f"• <b>{money(PRICE_3)} ₽</b> за третий уровень\n"
        f"4️⃣ Дополнительно можно забрать <b>{money(BONUS_AMOUNT)} ₽</b> в разделе «🎁 Бонус» — "
        f"раз в сутки\n\n"
        f"💸 <b>Вывод</b> — от <b>{money(MIN_WITHDRAW)} ₽</b> на банковскую карту, СБП или ЮMoney. "
        f"Заявки обрабатываются вручную{', без комиссии' if WITHDRAW_COMMISSION <= 0 else ''}.\n\n"
        f"⚠️ Друг засчитывается, только если он впервые запускает бота. "
        f"Накрутка фейковыми аккаунтами — бан без выплаты."
    )


def text_reviews() -> str:
    rows = db.reviews(10)
    if not rows:
        return ("⭐ <b>Отзывы</b>\n\nПока отзывов нет — станьте первым! "
                f"Напишите нам: {SUPPORT_USERNAME}")
    parts = ["⭐ <b>Отзывы наших участников</b>\n"]
    for r in rows:
        date = fmt_date(parse_iso(r["created_at"]))
        parts.append(f"👤 <b>{esc(r['author'] or 'Участник')}</b> · {date}\n{esc(r['text'])}\n")
    return "\n".join(parts)


def text_ads(user_id: int) -> str:
    row = db.get_user(user_id)
    balance = int(row["balance"]) if row else 0
    active = db.user_active_ad(user_id)
    extra = ""
    if active:
        if active["status"] == "pending":
            extra = "\n\n⏳ У вас есть заявка на модерации."
        elif active["status"] in ("approved", "posted"):
            extra = f"\n\n📌 Ваша реклама активна до {fmt_dt(parse_iso(active['expires_at']))}."
    return (
        f"📢 <b>Заказать рекламу</b>\n\n"
        f"Ваше объявление будет опубликовано в нашем канале и провисит "
        f"<b>{AD_DURATION_HOURS} часа</b> — после этого бот удалит его автоматически.\n\n"
        f"💵 Стоимость: <b>{money(AD_PRICE)} ₽</b> за {AD_DURATION_HOURS} ч\n"
        f"💰 Ваш баланс: <b>{money(balance)} ₽</b>{extra}\n\n"
        f"📝 Что нужно: текст объявления (до {AD_MAX_TEXT} символов), ссылка и/или картинка."
    )


def ads_kb(user_id: int) -> InlineKeyboardMarkup:
    row = db.get_user(user_id)
    balance = int(row["balance"]) if row else 0
    rows = [[btn(f"📢 Заказать за {money(AD_PRICE)} ₽", "ads:order", style="success")]]
    if balance < AD_PRICE:
        rows.append([btn("💳 Пополнить баланс", "topup:start", style="primary")])
    rows.append(back_kb())
    return kb(*rows)


def text_withdraw(user_id: int) -> str:
    row = db.get_user(user_id)
    balance = int(row["balance"]) if row else 0
    pending = db.pending_withdraw_sum(user_id)
    wds = db.user_withdrawals(user_id, 5)
    if wds:
        status_map = {"pending": "⏳ в обработке", "paid": "✅ выплачено", "rejected": "❌ отклонено"}
        lines = [
            f"#{w['id']} · {money(w['net_amount'])} ₽ · {status_map.get(w['status'], w['status'])}"
            for w in wds
        ]
        last = "Последние заявки:\n" + "\n".join(lines)
    else:
        last = "Заявок пока не было."

    commission = (f"Комиссия {WITHDRAW_COMMISSION:g}%." if WITHDRAW_COMMISSION > 0
                  else "Без комиссии.")
    shortfall = MIN_WITHDRAW - balance
    if shortfall > 0:
        tail = (f"🔒 До вывода не хватает <b>{money(shortfall)} ₽</b> — "
                f"приглашайте друзей!")
    else:
        tail = "✅ Минимальная сумма набрана — можно подать заявку!"

    return (
        f"💸 <b>Вывод средств</b>\n\n"
        f"💰 Доступно: <b>{money(balance)} ₽</b>\n"
        f"⏳ На выводе: <b>{money(pending)} ₽</b>\n\n"
        f"Минимальная сумма — <b>{money(MIN_WITHDRAW)} ₽</b>. {commission}\n"
        f"Способы: банковская карта, СБП, ЮMoney.\n\n"
        f"{last}\n\n"
        f"{tail}"
    )


def withdraw_kb(user_id: int) -> InlineKeyboardMarkup:
    row = db.get_user(user_id)
    balance = int(row["balance"]) if row else 0
    rows = []
    if balance >= MIN_WITHDRAW:
        rows.append([btn("💳 На карту", "wd:card", style="primary"),
                     btn("🏦 СБП", "wd:sbp", style="primary")])
        rows.append([btn("🟣 ЮMoney", "wd:yoomoney", style="primary")])
    else:
        rows.append([btn("🔗 Моя ссылка", "menu:link", style="success")])
    rows.append(back_kb())
    return kb(*rows)


# =====================================================================
#  5. СОСТОЯНИЯ (FSM)
# =====================================================================


class WithdrawFlow(StatesGroup):
    amount = State()
    details = State()
    confirm = State()


class AdFlow(StatesGroup):
    content = State()
    link = State()
    confirm = State()


class TopUpFlow(StatesGroup):
    amount = State()


class AdminFlow(StatesGroup):
    image = State()
    review = State()
    broadcast = State()
    balance = State()
    ban = State()


METHODS = {
    "card": "💳 Банковская карта",
    "sbp": "🏦 СБП",
    "yoomoney": "🟣 ЮMoney",
}


# =====================================================================
#  6. УВЕДОМЛЕНИЯ
# =====================================================================


async def send_to_admins(text: str, markup: InlineKeyboardMarkup | None = None) -> list[tuple[int, int]]:
    sent: list[tuple[int, int]] = []
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(admin_id, text, reply_markup=markup,
                                         disable_web_page_preview=True)
            sent.append((admin_id, msg.message_id))
        except TelegramAPIError as e:
            log.warning("Не смог написать админу %s: %s", admin_id, e)
    return sent


async def safe_send(user_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramForbiddenError:
        log.info("Пользователь %s заблокировал бота", user_id)
    except TelegramAPIError as e:
        log.warning("Не смог написать %s: %s", user_id, e)
    return False


# =====================================================================
#  7. СТАРТ / РЕГИСТРАЦИЯ / РЕФЕРАЛЫ
# =====================================================================


async def credit_referrers(new_user_id: int, referrer_id: int | None) -> None:
    """Начисляет деньги по цепочке из 3 уровней."""
    if not referrer_id:
        return
    prices = {1: PRICE_1, 2: PRICE_2, 3: PRICE_3}
    current = referrer_id
    depth = 1
    while current and depth <= 3:
        parent = db.get_user(current)
        if parent is None or int(parent["is_banned"]):
            break
        amount = prices[depth]
        if amount > 0:
            db.add_balance(current, amount, kind="ref")
            db.log_event("ref_reward", current, amount)
            if depth == 1:
                await safe_send(
                    current,
                    f"🎉 <b>По вашей ссылке зашёл друг!</b>\n\n"
                    f"💰 Начислено: <b>+{money(amount)} ₽</b>\n"
                    f"💳 Баланс: <b>{money(db.get_user(current)['balance'])} ₽</b>",
                )
            if REF_ALERT_PER_HOUR and db.refs_last_hour(current) >= REF_ALERT_PER_HOUR:
                await send_to_admins(
                    f"⚠️ <b>Подозрительная активность</b>\n"
                    f"Пользователь <code>{current}</code> получил "
                    f"{db.refs_last_hour(current)} друзей за час."
                )
        current = parent["referrer_id"]
        depth += 1


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return

    payload = (command.args or "").strip()
    referrer_id: int | None = None
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
        except ValueError:
            referrer_id = None

    existing = db.get_user(user.id)
    is_new = existing is None

    if is_new:
        # защита от самоприглашения и «пустых» рефереров
        if referrer_id == user.id or (referrer_id and db.get_user(referrer_id) is None):
            referrer_id = None
        db.create_user(user.id, user.username, user.first_name, referrer_id)
        db.log_event("new_user", user.id)
        await credit_referrers(user.id, referrer_id)
    else:
        db.touch_user(user.id, user.username, user.first_name)

    row = db.get_user(user.id)
    if row and int(row["is_banned"]):
        await message.answer(
            "🚫 <b>Доступ ограничен</b>\n\nВаш аккаунт заблокирован за нарушение правил. "
            f"Если это ошибка — напишите {SUPPORT_USERNAME}"
        )
        return

    text = text_main(user.id, user.first_name or "друг")
    if is_new and referrer_id:
        text = ("🎉 <b>Добро пожаловать!</b>\nВы запустили бота по приглашению друга.\n\n" + text)
    await send_screen(message, "main", text, main_kb(user.id))
    if is_new:
        db.log_event("start", user.id)


# =====================================================================
#  8. МЕНЮ (экраны)
# =====================================================================


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    user = cb.from_user
    db.touch_user(user.id, user.username, user.first_name)
    row = db.get_user(user.id)
    if row and int(row["is_banned"]):
        await cb.answer("🚫 Аккаунт заблокирован", show_alert=True)
        return
    await edit_screen(cb, "main", text_main(user.id, user.first_name or "друг"), main_kb(user.id))


@router.callback_query(F.data == "menu:link")
async def cb_link(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "link", text_link(cb.from_user.id), text_link_kb(cb.from_user.id))


@router.callback_query(F.data == "menu:profile")
async def cb_profile(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "profile", text_profile(cb.from_user.id), kb(back_kb()))


@router.callback_query(F.data == "menu:howto")
async def cb_howto(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "howto", text_howto(), kb(back_kb()))


@router.callback_query(F.data == "menu:reviews")
async def cb_reviews(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "reviews", text_reviews(),
                      kb([btn("✍️ Оставить отзыв", url=SUPPORT_URL, style="primary")], back_kb()))


@router.callback_query(F.data == "menu:bonus")
async def cb_bonus(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "bonus", text_bonus(cb.from_user.id), bonus_kb(cb.from_user.id))


@router.callback_query(F.data == "bonus:noop")
async def cb_bonus_noop(cb: CallbackQuery) -> None:
    await cb.answer(f"Бонус будет доступен через {human_left(db.bonus_available_at(cb.from_user.id))}",
                    show_alert=True)


@router.callback_query(F.data == "bonus:claim")
async def cb_bonus_claim(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    if db.claim_bonus(uid, BONUS_AMOUNT):
        db.log_event("bonus", uid, BONUS_AMOUNT)
        await cb.answer(f"🎁 Бонус {money(BONUS_AMOUNT)} ₽ зачислен на баланс!", show_alert=True)
    else:
        await cb.answer(
            f"⏳ Бонус уже получен. Следующий — через {human_left(db.bonus_available_at(uid))}",
            show_alert=True,
        )
    await edit_screen(cb, "bonus", text_bonus(uid), bonus_kb(uid))


@router.callback_query(F.data == "menu:ads")
async def cb_ads(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "ads", text_ads(cb.from_user.id), ads_kb(cb.from_user.id))


@router.callback_query(F.data == "menu:support")
async def cb_support(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_send(cb.from_user.id,
                    f"🆘 <b>Поддержка</b>\n\nНапишите нам: {SUPPORT_USERNAME}",
                    kb([btn("🆘 Написать в поддержку", url=SUPPORT_URL, style="danger")]))


# =====================================================================
#  9. ВЫВОД СРЕДСТВ
# =====================================================================


@router.callback_query(F.data == "menu:withdraw")
async def cb_withdraw(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await edit_screen(cb, "withdraw", text_withdraw(cb.from_user.id), withdraw_kb(cb.from_user.id))


@router.callback_query(F.data.in_({"wd:card", "wd:sbp", "wd:yoomoney"}))
async def cb_withdraw_method(cb: CallbackQuery, state: FSMContext) -> None:
    method = (cb.data or "").split(":", 1)[1]
    if method not in METHODS:
        await cb.answer("Неизвестный способ", show_alert=True)
        return
    if method == "yoomoney" and not YOOMONEY_WALLET:
        await cb.answer("ЮMoney пока не настроен", show_alert=True)
        return

    uid = cb.from_user.id
    row = db.get_user(uid)
    balance = int(row["balance"]) if row else 0
    if balance < MIN_WITHDRAW:
        await cb.answer(f"Минимум для вывода — {money(MIN_WITHDRAW)} ₽", show_alert=True)
        return
    if row and int(row["is_banned"]):
        await cb.answer("🚫 Вывод недоступен: аккаунт заблокирован", show_alert=True)
        return

    await state.set_state(WithdrawFlow.amount)
    await state.update_data(method=method)
    prompt = (
        f"{METHODS[method]}\n\n"
        f"💰 Доступно к выводу: <b>{money(balance)} ₽</b>\n"
        f"Минимальная сумма — <b>{money(MIN_WITHDRAW)} ₽</b>\n\n"
        f"Введите сумму вывода в рублях (например: <code>{money(max(MIN_WITHDRAW, balance))}</code>):"
    )
    await cb.bot.send_message(cb.from_user.id, prompt,
                              reply_markup=kb([btn("❌ Отмена", "wd:cancel", style="danger")]))
    await cb.answer()


@router.callback_query(F.data == "wd:cancel")
async def cb_withdraw_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Отменено")
    await edit_screen(cb, "withdraw", text_withdraw(cb.from_user.id), withdraw_kb(cb.from_user.id))


@router.message(WithdrawFlow.amount, F.text)
async def wd_amount(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    raw = (message.text or "").replace(" ", "").replace(",", ".").replace("₽", "").strip()
    try:
        amount = rubles_to_kop(Decimal(raw))
    except (InvalidOperation, ValueError):
        amount = 0
    if amount <= 0:
        await message.answer("❗️ Введите сумму числом, например: <code>3000</code>",
                            reply_markup=kb([btn("❌ Отмена", "wd:cancel", style="danger")]))
        return

    row = db.get_user(uid)
    balance = int(row["balance"]) if row else 0
    if amount < MIN_WITHDRAW:
        await message.answer(f"❗️ Минимальная сумма — <b>{money(MIN_WITHDRAW)} ₽</b>. Попробуйте ещё раз.")
        return
    if amount > balance:
        await message.answer(f"❗️ На балансе только <b>{money(balance)} ₽</b>. Введите сумму меньше.")
        return

    data = await state.get_data()
    method = data.get("method", "card")
    await state.update_data(amount=amount)
    await state.set_state(WithdrawFlow.details)

    hints = {
        "card": "Введите <b>номер карты</b> (16–19 цифр) получателя:",
        "sbp": "Введите <b>номер телефона</b>, привязанный к СБП (например: +79991234567):",
        "yoomoney": "Введите <b>номер кошелька ЮMoney</b> (например: 410011234567890):",
    }
    await message.answer(
        f"{METHODS[method]}\n\n{hints[method]}",
        reply_markup=kb([btn("❌ Отмена", "wd:cancel", style="danger")]),
    )


@router.message(WithdrawFlow.details, F.text)
async def wd_details(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    method = data.get("method", "card")
    amount = int(data.get("amount", 0))
    raw = (message.text or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    details = ""
    if method == "card":
        if not (16 <= len(digits) <= 19):
            await message.answer("❗️ Номер карты должен содержать 16–19 цифр. Введите ещё раз:")
            return
        details = digits
    elif method == "sbp":
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not (len(digits) == 11 and digits.startswith("7")):
            await message.answer("❗️ Введите номер телефона в формате +79991234567 (или 89991234567):")
            return
        details = "+" + digits
    else:
        if not (11 <= len(digits) <= 20):
            await message.answer("❗️ Введите номер кошелька ЮMoney (11–20 цифр):")
            return
        details = digits

    net = int(round(amount * (1 - WITHDRAW_COMMISSION / 100)))
    await state.update_data(details=details, net=net)
    await state.set_state(WithdrawFlow.confirm)

    text = (
        f"🧾 <b>Проверьте заявку</b>\n\n"
        f"Способ: {METHODS[method]}\n"
        f"Сумма: <b>{money(amount)} ₽</b>\n"
        f"К выплате: <b>{money(net)} ₽</b>\n"
        f"Реквизиты: <code>{esc(details)}</code>\n\n"
        f"Заявка обрабатывается вручную — обычно до 24 часов."
    )
    await message.answer(text, reply_markup=kb(
        [btn("✅ Подтвердить", "wd:submit", style="success")],
        [btn("❌ Отмена", "wd:cancel", style="danger")],
    ))


@router.callback_query(F.data == "wd:submit", WithdrawFlow.confirm)
async def wd_submit(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    data = await state.get_data()
    method = data.get("method", "card")
    amount = int(data.get("amount", 0))
    net = int(data.get("net", amount))
    details = str(data.get("details", ""))

    if amount < MIN_WITHDRAW or not details:
        await cb.answer("Заявка устарела, начните заново", show_alert=True)
        await state.clear()
        return
    if not db.spend_balance(uid, amount):
        await cb.answer("Недостаточно средств на балансе", show_alert=True)
        await state.clear()
        return

    wd_id = db.create_withdrawal(uid, amount, net, method, details)
    db.log_event("withdraw_request", uid, amount)
    await state.clear()

    user = cb.from_user
    uname = f"@{user.username}" if user.username else "—"
    admin_text = (
        f"💸 <b>Новая заявка на вывод #{wd_id}</b>\n\n"
        f"👤 {esc(user.full_name)} ({uname})\n"
        f"🆔 <code>{uid}</code>\n"
        f"🏷 Способ: {METHODS.get(method, method)}\n"
        f"💵 Сумма: <b>{money(amount)} ₽</b>\n"
        f"➡️ К выплате: <b>{money(net)} ₽</b>\n"
        f"📄 Реквизиты: <code>{esc(details)}</code>"
    )
    sent = await send_to_admins(admin_text, kb(
        [btn("✅ Выплачено", f"wdok:{wd_id}", style="success"),
         btn("❌ Отклонить", f"wdno:{wd_id}", style="danger")],
    ))
    if sent:
        db.set_withdrawal_msg(wd_id, sent[0][0], sent[0][1])

    await cb.bot.send_message(
        cb.from_user.id,
        f"✅ <b>Заявка #{wd_id} создана</b>\n\n"
        f"Сумма: <b>{money(net)} ₽</b>\n"
        f"Способ: {METHODS.get(method, method)}\n\n"
        f"Мы сообщим, когда выплата будет выполнена.",
        reply_markup=kb([btn("🔗 Моя ссылка", "menu:link", style="success")], back_kb()),
    )
    await cb.answer("Заявка отправлена")


# =====================================================================
#  10. РЕКЛАМА (5000 ₽ / 72 часа)
# =====================================================================


@router.callback_query(F.data == "ads:order")
async def ads_order(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    row = db.get_user(uid)
    balance = int(row["balance"]) if row else 0
    if row and int(row["is_banned"]):
        await cb.answer("🚫 Аккаунт заблокирован", show_alert=True)
        return
    active = db.user_active_ad(uid)
    if active:
        await cb.answer("У вас уже есть активная заявка на рекламу", show_alert=True)
        return
    if balance < AD_PRICE:
        await cb.answer(
            f"Недостаточно средств: нужно {money(AD_PRICE)} ₽, у вас {money(balance)} ₽",
            show_alert=True,
        )
        return

    await state.set_state(AdFlow.content)
    await cb.bot.send_message(
        cb.from_user.id,
        f"📢 <b>Новое объявление</b>\n\n"
        f"Отправьте текст объявления (до {AD_MAX_TEXT} символов) или фото с подписью.\n\n"
        f"Стоимость размещения — <b>{money(AD_PRICE)} ₽</b> за {AD_DURATION_HOURS} часа.",
        reply_markup=kb([btn("❌ Отмена", "ads:cancel", style="danger")]),
    )
    await cb.answer()


@router.callback_query(F.data == "ads:cancel")
async def ads_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Отменено")
    await edit_screen(cb, "ads", text_ads(cb.from_user.id), ads_kb(cb.from_user.id))


@router.message(AdFlow.content)
async def ads_content(message: Message, state: FSMContext) -> None:
    media_type = None
    media_id = None
    text = (message.text or message.caption or "").strip()

    if message.photo:
        media_type = "photo"
        media_id = message.photo[-1].file_id

    if not text and not media_id:
        await message.answer("❗️ Пришлите текст объявления или фото с подписью.")
        return
    if len(text) > AD_MAX_TEXT:
        await message.answer(f"❗️ Слишком длинно: {len(text)} символов, максимум {AD_MAX_TEXT}.")
        return

    await state.update_data(text=text, media_type=media_type, media_id=media_id)
    await state.set_state(AdFlow.link)
    await message.answer(
        "🔗 Теперь пришлите ссылку, куда вести пользователя "
        "(например: <code>https://t.me/your_channel</code>).\n\n"
        "Если ссылка не нужна — отправьте <code>-</code>",
        reply_markup=kb([btn("❌ Отмена", "ads:cancel", style="danger")]),
    )


@router.message(AdFlow.link, F.text)
async def ads_link(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    link = "" if raw in ("-", "нет", "Нет") else raw
    if link and not (link.startswith("http://") or link.startswith("https://") or link.startswith("@")):
        await message.answer("❗️ Ссылка должна начинаться с https:// или @username")
        return
    if link.startswith("@"):
        link = "https://t.me/" + link[1:]

    data = await state.get_data()
    await state.update_data(link=link)
    await state.set_state(AdFlow.confirm)

    preview = (
        f"📢 <b>Предпросмотр объявления</b>\n\n"
        f"{esc(data.get('text') or '(без текста)')}\n\n"
        f"🔗 {esc(link) if link else '— (без ссылки)'}\n"
        f"⏱ Срок размещения: <b>{AD_DURATION_HOURS} часа</b>\n\n"
        f"С вашего баланса будет списано <b>{money(AD_PRICE)} ₽</b>."
    )
    if data.get("media_id"):
        await message.answer_photo(data["media_id"], caption=preview, reply_markup=kb(
            [btn(f"✅ Оплатить {money(AD_PRICE)} ₽", "ads:confirm", style="success")],
            [btn("❌ Отмена", "ads:cancel", style="danger")],
        ))
    else:
        await message.answer(preview, reply_markup=kb(
            [btn(f"✅ Оплатить {money(AD_PRICE)} ₽", "ads:confirm", style="success")],
            [btn("❌ Отмена", "ads:cancel", style="danger")],
        ))


@router.callback_query(F.data == "ads:confirm", AdFlow.confirm)
async def ads_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    data = await state.get_data()
    if not db.spend_balance(uid, AD_PRICE):
        await cb.answer("Недостаточно средств на балансе", show_alert=True)
        await state.clear()
        return

    ad_id = db.create_ad(uid, AD_PRICE, data.get("text"), data.get("link"),
                         data.get("media_type"), data.get("media_id"))
    db.log_event("ad_order", uid, AD_PRICE)
    await state.clear()

    user = cb.from_user
    uname = f"@{user.username}" if user.username else "—"
    header = (
        f"📢 <b>Новая заявка на рекламу #{ad_id}</b>\n\n"
        f"👤 {esc(user.full_name)} ({uname})\n"
        f"🆔 <code>{uid}</code>\n"
        f"💵 Оплачено: <b>{money(AD_PRICE)} ₽</b> · срок {AD_DURATION_HOURS} ч\n"
        f"🔗 {esc(data.get('link') or '—')}\n\n"
        f"——— текст объявления ———\n{esc(data.get('text') or '(без текста)')}"
    )
    control = kb(
        [btn("✅ Опубликовать", f"adok:{ad_id}", style="success"),
         btn("❌ Отклонить", f"adno:{ad_id}", style="danger")],
    )
    sent: list[tuple[int, int]] = []
    for admin_id in ADMIN_IDS:
        try:
            if data.get("media_id"):
                await bot.send_photo(admin_id, data["media_id"], caption=header[:1000])
            else:
                await bot.send_message(admin_id, header, disable_web_page_preview=True)
            msg = await bot.send_message(admin_id, "⬆️ Заявка на рекламу: выберите действие",
                                        reply_markup=control)
            sent.append((admin_id, msg.message_id))
        except TelegramAPIError as e:
            log.warning("Реклама: не смог отправить админу %s: %s", admin_id, e)
    if sent:
        db.set_ad_msg(ad_id, sent[0][0], sent[0][1])

    await cb.bot.send_message(
        cb.from_user.id,
        f"✅ <b>Заявка на рекламу #{ad_id} оплачена и отправлена на модерацию.</b>\n\n"
        f"Обычно проверка занимает до 24 часов. После публикации объявление провисит "
        f"{AD_DURATION_HOURS} часа, затем бот удалит его автоматически.",
        reply_markup=kb(back_kb("menu:ads")),
    )
    await cb.answer("Оплачено")


# =====================================================================
#  11. ПОПОЛНЕНИЕ БАЛАНСА (ЮMoney)
# =====================================================================


@router.callback_query(F.data == "topup:start")
async def topup_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not (YOOMONEY_RECEIVER and YOOMONEY_TOKEN):
        await cb.answer("Пополнение пока недоступно", show_alert=True)
        return
    await state.set_state(TopUpFlow.amount)
    await cb.bot.send_message(
        cb.from_user.id,
        f"💳 <b>Пополнение баланса</b>\n\n"
        f"Отправьте сумму пополнения в рублях (от {money(TOPUP_MIN)} до {money(TOPUP_MAX)} ₽).",
        reply_markup=kb([btn("❌ Отмена", "topup:cancel", style="danger")]),
    )
    await cb.answer()


@router.callback_query(F.data == "topup:cancel")
async def topup_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Отменено")
    await edit_screen(cb, "ads", text_ads(cb.from_user.id), ads_kb(cb.from_user.id))


@router.message(TopUpFlow.amount, F.text)
async def topup_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").replace(",", ".").replace("₽", "").strip()
    try:
        amount = rubles_to_kop(Decimal(raw))
    except (InvalidOperation, ValueError):
        await message.answer("❗️ Введите сумму числом, например: <code>5000</code>")
        return
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        await message.answer(f"❗️ Сумма должна быть от {money(TOPUP_MIN)} до {money(TOPUP_MAX)} ₽")
        return

    uid = message.from_user.id
    label = f"topup_{uid}_{int(now_utc().timestamp())}"
    db.create_payment(uid, amount, label)
    await state.clear()

    url = yoomoney_quickpay_url(amount, label)
    await message.answer(
        f"💳 <b>Оплата {money(amount)} ₽</b>\n\n"
        f"1. Нажмите «Оплатить» и переведите сумму через ЮMoney\n"
        f"2. Вернитесь в бота и нажмите «Я оплатил»\n\n"
        f"⚡️ Баланс пополнится автоматически в течение минуты после оплаты.",
        reply_markup=kb(
            [btn("💳 Оплатить", url=url, style="success")],
            [btn("✅ Я оплатил", f"topup:check:{label}", style="primary")],
            back_kb("menu:ads"),
        ),
    )


@router.callback_query(F.data.startswith("topup:check:"))
async def topup_check(cb: CallbackQuery) -> None:
    label = (cb.data or "").split(":", 2)[2]
    payment = db.get_payment(label)
    if payment is None:
        await cb.answer("Платёж не найден", show_alert=True)
        return
    if payment["status"] == "paid":
        await cb.answer("✅ Платёж уже зачислен", show_alert=True)
        return
    await cb.answer("Проверяем…")
    ok = await yoomoney_check_label(label)
    if ok:
        await cb.message.answer(f"✅ Баланс пополнен на <b>{money(payment['amount'])} ₽</b>",
                                reply_markup=kb(back_kb("menu:ads")))
    else:
        await cb.message.answer(
            "⏳ Платёж пока не найден. Если вы только что оплатили — подождите минуту "
            "и нажмите «Я оплатил» снова.",
            reply_markup=kb([btn("✅ Я оплатил", f"topup:check:{label}", style="primary")],
                            back_kb("menu:ads")),
        )




# =====================================================================
#  12. ЮMONEY: оплата, проверка платежей, автовыплата
# =====================================================================

YOOMONEY_API = "https://yoomoney.ru/api"


def yoomoney_quickpay_url(amount_kop: int, label: str) -> str:
    """Ссылка на форму оплаты ЮMoney (быстрые платежи)."""
    params = {
        "receiver": YOOMONEY_RECEIVER,
        "quickpay-form": "button",
        "targets": f"Пополнение баланса {BOT_NAME}",
        "paymentType": "AC",  # AC — карта, PC — кошелёк
        "sum": f"{amount_kop / 100:.2f}",
        "label": label,
    }
    return "https://yoomoney.ru/quickpay/confirm?" + urlencode(params)


async def yoomoney_api(method: str, data: dict[str, str]) -> dict[str, Any]:
    """Запрос к API ЮMoney с Bearer-токеном."""
    headers = {
        "Authorization": f"Bearer {YOOMONEY_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        async with session.post(f"{YOOMONEY_API}/{method}", headers=headers, data=data) as resp:
            try:
                return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                return {"error": f"http_{resp.status}"}


async def yoomoney_check_label(label: str) -> bool:
    """Ищет входящий платёж с нужной меткой и зачисляет его на баланс."""
    if not YOOMONEY_TOKEN:
        return False
    try:
        res = await yoomoney_api("operation-history", {"records": "50", "type": "deposition"})
    except Exception as e:  # noqa: BLE001
        log.warning("ЮMoney: ошибка запроса истории: %s", e)
        return False

    if res.get("error"):
        log.warning("ЮMoney: %s", res.get("error"))
        return False

    for op in res.get("operations", []):
        if op.get("status") != "success":
            continue
        if op.get("label") != label:
            continue
        if op.get("direction", "incoming") != "incoming":
            continue
        amount = rubles_to_kop(op.get("amount", 0))
        if db.mark_payment_paid(label, str(op.get("operation_id")), amount):
            payment = db.get_payment(label)
            if payment:
                db.log_event("topup", int(payment["user_id"]), amount)
                await safe_send(
                    int(payment["user_id"]),
                    f"✅ <b>Баланс пополнен на {money(amount)} ₽</b>\n\n"
                    f"Теперь вы можете заказать рекламу в разделе 📢",
                    kb([btn("📢 Заказать рекламу", "menu:ads", style="success")]),
                )
            return True
    return False


async def yoomoney_poll_payments() -> int:
    """Один запрос истории -> зачисляет все найденные платежи. Возвращает кол-во зачислений."""
    pending = db.pending_payments()
    if not pending or not YOOMONEY_TOKEN:
        return 0
    labels = {str(p["label"]) for p in pending}
    try:
        res = await yoomoney_api("operation-history", {"records": "50", "type": "deposition"})
    except Exception as e:  # noqa: BLE001
        log.warning("ЮMoney: ошибка запроса истории: %s", e)
        return 0
    if res.get("error"):
        log.warning("ЮMoney: %s", res.get("error"))
        return 0

    credited = 0
    for op in res.get("operations", []):
        label = str(op.get("label") or "")
        if label not in labels:
            continue
        if op.get("status") != "success" or op.get("direction", "incoming") != "incoming":
            continue
        amount = rubles_to_kop(op.get("amount", 0))
        if db.mark_payment_paid(label, str(op.get("operation_id")), amount):
            credited += 1
            payment = db.get_payment(label)
            if payment:
                db.log_event("topup", int(payment["user_id"]), amount)
                await safe_send(
                    int(payment["user_id"]),
                    f"✅ <b>Баланс пополнен на {money(amount)} ₽</b>\n\n"
                    f"Теперь вы можете заказать рекламу в разделе 📢",
                    kb([btn("📢 Заказать рекламу", "menu:ads", style="success")]),
                )
    return credited


async def yoomoney_poll_loop() -> None:
    """Фоновый опрос платежей — зачисление без нажатия «Я оплатил»."""
    while True:
        try:
            await yoomoney_poll_payments()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ЮMoney: ошибка в цикле опроса")
        await asyncio.sleep(max(15, YOOMONEY_POLL_SECONDS))


async def yoomoney_payout(wallet: str, amount_kop: int, label: str) -> tuple[bool, str]:
    """
    Автовыплата на кошелёк ЮMoney (p2p).
    Работает только если YOOMONEY_AUTOPAYOUT=true и у токена есть права на платежи.
    """
    if not (YOOMONEY_TOKEN and YOOMONEY_AUTOPAYOUT):
        return False, "автовыплата выключена"
    request = await yoomoney_api("request-payment", {
        "pattern_id": "p2p",
        "to": wallet,
        "amount": f"{amount_kop / 100:.2f}",
        "comment": f"Выплата {label}",
        "label": label,
        "test_payment": "false",
    })
    if request.get("status") != "success":
        return False, str(request.get("error", "request-payment failed"))
    process = await yoomoney_api("process-payment", {"request_id": str(request.get("request_id"))})
    if process.get("status") == "success":
        return True, "ok"
    return False, str(process.get("error", "process-payment failed"))


# =====================================================================
#  13. АДМИН-ПАНЕЛЬ
# =====================================================================


def admin_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("📊 Статистика", "admin:stats", style="primary")],
        [btn("🖼 Картинки меню", "admin:images", style="primary"),
         btn("⭐ Отзывы", "admin:reviews", style="primary")],
        [btn("📢 Реклама", "admin:ads", style="primary"),
         btn("💸 Заявки на вывод", "admin:payouts", style="primary")],
        [btn("📣 Рассылка", "admin:broadcast", style="primary"),
         btn("💰 Начислить", "admin:balance", style="success")],
        [btn("🚫 Бан / разбан", "admin:ban", style="danger")],
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("⚙️ <b>Админ-панель</b>\nВыберите раздел:", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    if getattr(cb.message, "photo", None):
        await cb.message.delete()
        await cb.message.answer("⚙️ <b>Админ-панель</b>\nВыберите раздел:", reply_markup=admin_kb())
    else:
        await cb.message.edit_text("⚙️ <b>Админ-панель</b>\nВыберите раздел:", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer()
    day_ago = now_utc() - timedelta(days=1)
    week_ago = now_utc() - timedelta(days=7)
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{db.count_users()}</b>\n"
        f"🆕 За 24 часа: <b>{db.count_users_since(day_ago)}</b>\n"
        f"🆕 За 7 дней: <b>{db.count_users_since(week_ago)}</b>\n\n"
        f"💰 Сумма балансов: <b>{money(db.sum_balances())} ₽</b>\n"
        f"⏳ В заявках на вывод: <b>{money(db.sum_pending_withdraw())} ₽</b>\n"
        f"✅ Выплачено всего: <b>{money(db.sum_paid_out())} ₽</b>\n\n"
        f"🖼 Картинок загружено: <b>{len(db.images_map())}/{len(IMAGE_KEYS)}</b>\n"
        f"📢 Реклама на модерации: <b>{len(db.ads_by_status('pending'))}</b>\n"
        f"💸 Заявок на вывод: <b>{len(db.pending_withdrawals())}</b>"
    )
    markup = kb([btn("🔄 Обновить", "admin:stats", style="primary")],
                [btn("◀️ В админку", "admin:home", style="primary")])
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


# ------------------------- картинки меню -----------------------------


@router.callback_query(F.data == "admin:images")
async def cb_admin_images(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    images = db.images_map()
    rows = []
    for key, title in IMAGE_KEYS.items():
        mark = "✅" if key in images else "➖"
        rows.append([btn(f"{mark} {title}", f"img:set:{key}", style="primary")])
    rows.append([btn("◀️ В админку", "admin:home", style="primary")])
    try:
        await cb.message.edit_text(
            "🖼 <b>Картинки меню</b>\n\n"
            "Нажмите на раздел и отправьте фото — оно будет показываться над меню.\n"
            "✅ — картинка загружена, ➖ — пока текст без картинки.",
            reply_markup=kb(*rows),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("img:set:"))
async def cb_admin_image_set(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    key = (cb.data or "").split(":", 2)[2]
    if key not in IMAGE_KEYS:
        await cb.answer("Неизвестный раздел", show_alert=True)
        return
    await state.set_state(AdminFlow.image)
    await state.update_data(image_key=key)
    await cb.answer()
    await cb.message.answer(
        f"🖼 Раздел: <b>{IMAGE_KEYS[key]}</b>\n\n"
        f"Отправьте фото (до 5 МБ), которое будет стоять над этим меню.\n"
        f"Рекомендуемый размер — 900×330 px.",
        reply_markup=kb([btn("🗑 Удалить картинку", f"img:del:{key}", style="danger")],
                        [btn("❌ Отмена", "admin:images", style="primary")]),
    )


@router.message(AdminFlow.image, F.photo)
async def admin_image_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("image_key")
    if key not in IMAGE_KEYS:
        await state.clear()
        return
    db.set_image(key, message.photo[-1].file_id)
    await state.clear()
    await message.answer(
        f"✅ Картинка для раздела <b>{IMAGE_KEYS[key]}</b> сохранена.",
        reply_markup=kb([btn("🖼 К картинкам", "admin:images", style="primary")]),
    )


@router.callback_query(F.data.startswith("img:del:"))
async def cb_admin_image_del(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    key = (cb.data or "").split(":", 2)[2]
    db.del_image(key)
    await state.clear()
    await cb.answer("Картинка удалена")
    await cb_admin_images(cb, state)


# --------------------------- отзывы ----------------------------------


@router.callback_query(F.data == "admin:reviews")
async def cb_admin_reviews(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    rows = db.reviews(10)
    lines = [f"⭐ <b>Отзывы</b> (всего: {db.reviews_count()})\n"]
    buttons = []
    for r in rows:
        short = (r["text"][:60] + "…") if len(r["text"]) > 60 else r["text"]
        lines.append(f"#{r['id']} · <b>{esc(r['author'])}</b>: {esc(short)}")
        buttons.append([btn(f"🗑 Удалить #{r['id']}", f"rev:del:{r['id']}", style="danger")])
    if not rows:
        lines.append("Пока пусто.")
    markup = kb([btn("➕ Добавить отзыв", "rev:add", style="success")],
                *buttons,
                [btn("◀️ В админку", "admin:home", style="primary")])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data == "rev:add")
async def cb_review_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminFlow.review)
    await cb.answer()
    await cb.message.answer(
        "✍️ Отправьте отзыв в формате:\n<code>Имя | Текст отзыва</code>\n\n"
        "Например: <code>Алексей | Вывел 3000 ₽ за 2 часа, всё честно</code>",
        reply_markup=kb([btn("❌ Отмена", "admin:reviews", style="primary")]),
    )


@router.message(AdminFlow.review, F.text)
async def admin_review_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if "|" in raw:
        author, text = [p.strip() for p in raw.split("|", 1)]
    else:
        author, text = "Участник", raw
    if not text:
        await message.answer("Отзыв пустой, попробуйте ещё раз.")
        return
    db.add_review(author or "Участник", text)
    await state.clear()
    await message.answer(f"✅ Отзыв добавлен от <b>{esc(author)}</b>",
                         reply_markup=kb([btn("⭐ К отзывам", "admin:reviews", style="primary")]))


@router.callback_query(F.data.startswith("rev:del:"))
async def cb_review_del(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    db.del_review(int((cb.data or "").split(":")[2]))
    await cb.answer("Удалено")
    await cb_admin_reviews(cb, state)


# ---------------------- заявки на вывод ------------------------------


@router.callback_query(F.data == "admin:payouts")
async def cb_admin_payouts(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    rows = db.pending_withdrawals(10)
    if not rows:
        text = "💸 <b>Заявки на вывод</b>\n\nАктивных заявок нет 🎉"
        markup = kb([btn("◀️ В админку", "admin:home", style="primary")])
    else:
        lines = ["💸 <b>Заявки на вывод</b>\n"]
        buttons = []
        for w in rows:
            user = db.get_user(int(w["user_id"]))
            uname = f"@{user['username']}" if user and user["username"] else "—"
            lines.append(
                f"#{w['id']} · <code>{w['user_id']}</code> {uname}\n"
                f"   {METHODS.get(w['method'], w['method'])} · <b>{money(w['net_amount'])} ₽</b> → "
                f"<code>{esc(w['details'])}</code>"
            )
            buttons.append([
                btn(f"✅ #{w['id']}", f"wdok:{w['id']}", style="success"),
                btn(f"❌ #{w['id']}", f"wdno:{w['id']}", style="danger"),
            ])
        text = "\n".join(lines)
        markup = kb(*buttons, [btn("◀️ В админку", "admin:home", style="primary")])
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("wdok:"))
async def cb_wd_ok(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    wd_id = int((cb.data or "").split(":")[1])
    wd = db.get_withdrawal(wd_id)
    if wd is None or wd["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    net = int(wd["net_amount"])
    method = str(wd["method"])
    paid_by_bot = False
    if method == "yoomoney" and YOOMONEY_AUTOPAYOUT:
        ok, err = await yoomoney_payout(str(wd["details"]), net, f"wd_{wd_id}")
        paid_by_bot = ok
        if not ok:
            await cb.answer(f"Автовыплата не удалась: {err}. Отмечаю вручную.", show_alert=True)

    db.set_withdrawal_status(wd_id, "paid")
    db.add_paid_out(int(wd["user_id"]), net)
    db.log_event("withdraw_paid", int(wd["user_id"]), net)
    await safe_send(
        int(wd["user_id"]),
        f"✅ <b>Выплата по заявке #{wd_id} выполнена</b>\n\n"
        f"Сумма: <b>{money(net)} ₽</b>\n"
        f"Способ: {METHODS.get(method, method)}\n"
        f"Реквизиты: <code>{esc(str(wd['details']))}</code>\n\n"
        f"Спасибо, что с нами! 💚",
    )
    await cb.answer("Отмечено как выплачено")
    suffix = "✅ выплачено" + (" (ботом)" if paid_by_bot else "")
    await _mark_admin_message(cb, suffix)


@router.callback_query(F.data.startswith("wdno:"))
async def cb_wd_no(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    wd_id = int((cb.data or "").split(":")[1])
    wd = db.get_withdrawal(wd_id)
    if wd is None or wd["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    db.set_withdrawal_status(wd_id, "rejected")
    db.add_balance(int(wd["user_id"]), int(wd["amount"]))
    db.log_event("withdraw_rejected", int(wd["user_id"]), int(wd["amount"]))
    await safe_send(
        int(wd["user_id"]),
        f"❌ <b>Заявка #{wd_id} отклонена</b>\n\n"
        f"Сумма <b>{money(wd['amount'])} ₽</b> возвращена на баланс.\n"
        f"Если это ошибка — напишите {SUPPORT_USERNAME}",
    )
    await cb.answer("Заявка отклонена, деньги возвращены")
    await _mark_admin_message(cb, "❌ отклонено (деньги возвращены)")


async def _mark_admin_message(cb: CallbackQuery, status_line: str) -> None:
    """Дописывает статус в сообщение-заявку и убирает кнопки."""
    msg = cb.message
    if msg is None:
        return
    base = msg.html_text if msg.caption is None else msg.caption
    new_text = f"{base}\n\n<b>{status_line}</b>"
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(caption=new_text[:1024], reply_markup=None)
        else:
            await msg.edit_text(new_text[:4000], reply_markup=None, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass


# --------------------------- реклама (модерация) ---------------------


async def post_ad(ad_row: sqlite3.Row) -> tuple[bool, str, list]:
    """Публикует объявление в канал. Возвращает (успех, ошибка, [[chat, msg], ...])."""
    if not AD_CHANNEL_ID:
        return False, "AD_CHANNEL_ID не задан в .env", []
    ad_text = ad_row["text"] or ""
    link = ad_row["link"]
    markup = kb([btn("🔗 Перейти", url=link, style="success")]) if link else None
    try:
        if ad_row["media_type"] == "photo" and ad_row["media_id"]:
            msg = await bot.send_photo(AD_CHANNEL_ID, ad_row["media_id"],
                                       caption=ad_text[:1000] or None, reply_markup=markup)
        else:
            msg = await bot.send_message(AD_CHANNEL_ID, ad_text, reply_markup=markup,
                                         disable_web_page_preview=False)
    except TelegramAPIError as e:
        return False, str(e), []
    return True, "", [[msg.chat.id, msg.message_id]]


@router.callback_query(F.data == "admin:ads")
async def cb_admin_ads(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    pending = db.ads_by_status("pending", 10)
    active = db.ads_by_status("posted", 10)
    lines = ["📢 <b>Реклама</b>\n"]
    buttons = []
    if pending:
        lines.append("<b>На модерации:</b>")
        for ad in pending:
            lines.append(f"#{ad['id']} · <code>{ad['user_id']}</code> · {money(ad['price'])} ₽")
            buttons.append([btn(f"✅ #{ad['id']}", f"adok:{ad['id']}", style="success"),
                            btn(f"❌ #{ad['id']}", f"adno:{ad['id']}", style="danger")])
    else:
        lines.append("На модерации: пусто")
    if active:
        lines.append("\n<b>Сейчас в канале:</b>")
        for ad in active:
            lines.append(f"#{ad['id']} · до {fmt_dt(parse_iso(ad['expires_at']))}")
    lines.append(f"\n💰 Цена: {money(AD_PRICE)} ₽ · срок {AD_DURATION_HOURS} ч\n"
                 f"📢 Канал: {AD_CHANNEL_ID or '❗️ не задан (AD_CHANNEL_ID)'}")
    markup = kb(*buttons, [btn("◀️ В админку", "admin:home", style="primary")])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.startswith("adok:"))
async def cb_ad_ok(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    ad_id = int((cb.data or "").split(":")[1])
    ad = db.get_ad(ad_id)
    if ad is None or ad["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    ok, err, payload = await post_ad(ad)
    if not ok:
        await cb.answer(f"Не удалось опубликовать: {err}", show_alert=True)
        return

    expires = iso(now_utc() + timedelta(hours=AD_DURATION_HOURS))
    db.set_ad_status(ad_id, "posted", posted_at=iso(now_utc()), expires_at=expires,
                     payload=json.dumps(payload))
    db.log_event("ad_posted", int(ad["user_id"]), int(ad["price"]))
    channel_url = (f"https://t.me/{AD_CHANNEL_ID.lstrip('@')}"
                   if AD_CHANNEL_ID.startswith("@") else None)
    await safe_send(
        int(ad["user_id"]),
        f"📢 <b>Ваша реклама #{ad_id} опубликована!</b>\n\n"
        f"Она провисит до <b>{fmt_dt(parse_iso(expires))}</b>, после чего бот удалит её автоматически.",
        kb([btn("📢 Перейти в канал", url=channel_url, style="primary")]) if channel_url else None,
    )
    await cb.answer("Опубликовано")
    await _mark_admin_message(cb, f"✅ опубликовано до {fmt_dt(parse_iso(expires))}")


@router.callback_query(F.data.startswith("adno:"))
async def cb_ad_no(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    ad_id = int((cb.data or "").split(":")[1])
    ad = db.get_ad(ad_id)
    if ad is None or ad["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    db.set_ad_status(ad_id, "rejected")
    db.add_balance(int(ad["user_id"]), int(ad["price"]))
    db.log_event("ad_rejected", int(ad["user_id"]), int(ad["price"]))
    await safe_send(
        int(ad["user_id"]),
        f"❌ <b>Объявление #{ad_id} отклонено модерацией</b>\n\n"
        f"Сумма <b>{money(ad['price'])} ₽</b> возвращена на баланс.\n"
        f"Причина может быть в правилах канала. Вопросы — {SUPPORT_USERNAME}",
    )
    await cb.answer("Отклонено, деньги возвращены")
    await _mark_admin_message(cb, "❌ отклонено (деньги возвращены)")


# ------------------- рассылка / начисления / бан ----------------------


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminFlow.broadcast)
    await cb.answer()
    await cb.message.answer(
        f"📣 <b>Рассылка</b>\n\nОтправьте текст — он уйдёт всем "
        f"({db.count_users()} чел.). Поддерживается HTML-разметка.",
        reply_markup=kb([btn("❌ Отмена", "admin:home", style="primary")]),
    )


@router.message(AdminFlow.broadcast, F.text)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text = message.html_text
    users = db.all_user_ids()
    status = await message.answer(f"⏳ Отправляю… 0/{len(users)}")
    sent = failed = 0
    for i, uid in enumerate(users, start=1):
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
        except TelegramAPIError:
            failed += 1
        if i % 25 == 0:
            try:
                await status.edit_text(f"⏳ Отправляю… {i}/{len(users)}")
            except TelegramBadRequest:
                pass
            await asyncio.sleep(1)
    await status.edit_text(f"✅ Готово: доставлено {sent}, не доставлено {failed}",
                          reply_markup=kb([btn("◀️ В админку", "admin:home", style="primary")]))


@router.callback_query(F.data == "admin:balance")
async def cb_admin_balance(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminFlow.balance)
    await cb.answer()
    await cb.message.answer(
        "💰 <b>Начислить / списать</b>\n\n"
        "Формат: <code>ID сумма</code>\n"
        "Например: <code>123456789 500</code> — начислить 500 ₽\n"
        "<code>123456789 -500</code> — списать 500 ₽",
        reply_markup=kb([btn("❌ Отмена", "admin:home", style="primary")]),
    )


@router.message(AdminFlow.balance, F.text)
async def admin_balance_apply(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").replace(",", ".").split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        await message.answer("❗️ Формат: <code>ID сумма</code>")
        return
    uid = int(parts[0])
    try:
        amount = rubles_to_kop(Decimal(parts[1]))
    except (InvalidOperation, ValueError):
        await message.answer("❗️ Не понял сумму")
        return
    user = db.get_user(uid)
    if user is None:
        await message.answer("❗️ Пользователь не найден")
        return
    if amount < 0 and int(user["balance"]) + amount < 0:
        await message.answer("❗️ Нельзя списать больше баланса")
        return
    db.add_balance(uid, amount)
    db.log_event("admin_balance", uid, amount)
    await state.clear()
    await message.answer(
        f"✅ {'Начислено' if amount >= 0 else 'Списано'} <b>{money(abs(amount))} ₽</b>\n"
        f"👤 <code>{uid}</code> — баланс теперь <b>{money(db.get_user(uid)['balance'])} ₽</b>",
        reply_markup=kb([btn("◀️ В админку", "admin:home", style="primary")]),
    )
    if amount > 0:
        await safe_send(uid, f"💰 Вам начислено <b>{money(amount)} ₽</b> на баланс администратором.")


@router.callback_query(F.data == "admin:ban")
async def cb_admin_ban(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminFlow.ban)
    await cb.answer()
    await cb.message.answer(
        "🚫 <b>Бан / разбан</b>\n\nОтправьте ID пользователя — статус переключится.",
        reply_markup=kb([btn("❌ Отмена", "admin:home", style="primary")]),
    )


@router.message(AdminFlow.ban, F.text)
async def admin_ban_apply(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❗️ Отправьте числовой ID")
        return
    uid = int(raw)
    user = db.get_user(uid)
    if user is None:
        await message.answer("❗️ Пользователь не найден")
        return
    banned = not bool(int(user["is_banned"]))
    db.set_banned(uid, banned)
    db.log_event("ban" if banned else "unban", uid)
    await state.clear()
    await message.answer(
        f"{'🚫 Заблокирован' if banned else '✅ Разблокирован'}: <code>{uid}</code>",
        reply_markup=kb([btn("◀️ В админку", "admin:home", style="primary")]),
    )
    if banned:
        await safe_send(uid, "🚫 Ваш аккаунт заблокирован за нарушение правил.")
    else:
        await safe_send(uid, "✅ Ваш аккаунт разблокирован.")


# =====================================================================
#  14. ФОНОВЫЕ ЗАДАЧИ
# =====================================================================


async def ad_expiry_loop() -> None:
    """Каждую минуту снимает рекламу, у которой истёк срок (72 часа)."""
    while True:
        try:
            for ad in db.ads_to_expire():
                payload: list = []
                if ad["payload"]:
                    try:
                        payload = json.loads(ad["payload"])
                    except json.JSONDecodeError:
                        payload = []
                for chat_id, msg_id in payload:
                    try:
                        # chat_id может быть числом (-100...) или @username
                        await bot.delete_message(chat_id, int(msg_id))
                    except (TelegramAPIError, TypeError, ValueError) as e:
                        log.info("Реклама #%s: не удалось удалить сообщение: %s", ad["id"], e)
                db.set_ad_status(int(ad["id"]), "expired")
                db.log_event("ad_expired", int(ad["user_id"]), int(ad["price"]))
                await safe_send(
                    int(ad["user_id"]),
                    f"⏱ <b>Срок размещения рекламы #{ad['id']} истёк.</b>\n\n"
                    f"Бот удалил объявление из канала. Хотите продлить — "
                    f"закажите новое в разделе 📢",
                    kb([btn("📢 Заказать рекламу", "menu:ads", style="success")]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле снятия рекламы")
        await asyncio.sleep(60)


# =====================================================================
#  15. ЗАПУСК
# =====================================================================


@dp.errors()
async def on_error(event: Any) -> bool:
    exception = getattr(event, "exception", event)
    log.error("Ошибка при обработке обновления: %r", exception, exc_info=exception)
    return True


async def main() -> None:
    global BOT_USERNAME
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env (см. .env.example)")

    try:
        me = await bot.get_me()
    except TelegramAPIError as e:
        await bot.session.close()
        raise SystemExit(
            "Не удалось подключиться к Telegram. Проверьте BOT_TOKEN в .env\n"
            f"Ответ Telegram: {e}"
        )
    BOT_USERNAME = me.username or ""
    log.info("Бот @%s запущен. Админы: %s", BOT_USERNAME, ADMIN_IDS or "не заданы!")

    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="help", description="❓ Как это работает"),
    ])
    if ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    [BotCommand(command="start", description="🏠 Главное меню"),
                     BotCommand(command="admin", description="⚙️ Админ-панель")],
                    scope={"type": "chat", "chat_id": admin_id},
                )
            except TelegramAPIError as e:
                log.warning("Не смог установить команды админу %s: %s", admin_id, e)

    await bot.delete_webhook(drop_pending_updates=True)

    tasks = [
        asyncio.create_task(ad_expiry_loop()),
        asyncio.create_task(yoomoney_poll_loop()),
    ]
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await send_screen(message, "howto", text_howto(), kb(back_kb()))


@router.message()
async def fallback(message: Message) -> None:
    """Любое другое сообщение — мягко возвращаем в главное меню."""
    if not message.from_user:
        return
    row = db.get_user(message.from_user.id)
    if row is None:
        await message.answer("Нажмите /start, чтобы начать 👇")
        return
    await send_screen(message, "main",
                      text_main(message.from_user.id, message.from_user.first_name or "друг"),
                      main_kb(message.from_user.id))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        print(f"\nБот остановлен: {e}")
