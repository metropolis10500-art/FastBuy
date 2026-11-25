"""Приглашай+ — all-in-one Telegram bot.

Features: three-level referrals, daily bonus, manual card/SBP withdrawals,
YooMoney ad orders with API payment verification, admin panel, user export,
and rate-limited broadcasts. Secrets belong in .env, never in this file.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
ASSETS_DIR = BASE_DIR / "assets"
MOSCOW = ZoneInfo("Europe/Moscow")

BOT_NAME = os.getenv("BOT_NAME", "Приглашай+").strip() or "Приглашай+"
BOT_USERNAME_SUGGESTION = "PriglashayPlusBot"
BOT_SHORT_DESCRIPTION = "Приглашай друзей и получай бонусы за новых участников."
BOT_DESCRIPTION = (
    "Приглашай+ — реферальная программа: 11 / 7 / 3 ₽ за новых участников при первом запуске. "
    "Ежедневный бонус — 25 ₽. Вывод от 2 500 ₽ на карту или СБП обрабатывается вручную. "
    "Рекламная рассылка в боте — 3 000 ₽; объявления публикуются после проверки администратором."
)
REVIEWS_URL = os.getenv("REVIEWS_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot_data_advanced.sqlite3").strip() or "bot_data_advanced.sqlite3"
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "").strip()
YOOMONEY_TOKEN = os.getenv("YOOMONEY_TOKEN", "").strip()
YOOMONEY_PAYMENT_TYPE = os.getenv("YOOMONEY_PAYMENT_TYPE", "AC").strip().upper()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
except ValueError:
    ADMIN_ID = 0
try:
    AD_PRICE_RUB = int(os.getenv("AD_PRICE_RUB", "3000").strip() or "3000")
except ValueError:
    AD_PRICE_RUB = 3000
try:
    PAYMENT_POLL_SECONDS = max(20, int(os.getenv("PAYMENT_POLL_SECONDS", "45")))
except ValueError:
    PAYMENT_POLL_SECONDS = 45

REFERRAL_REWARDS = (1_100, 700, 300)  # 11 / 7 / 3 RUB, integer kopeks
DAILY_BONUS = 2_500  # 25 RUB
MIN_WITHDRAWAL = 250_000  # 2,500 RUB
AD_PRICE_KOPEKS = max(1, AD_PRICE_RUB) * 100
YOOMONEY_API = "https://yoomoney.ru/api/operation-history"
YOOMONEY_FORM = "https://yoomoney.ru/quickpay/confirm.xml"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("priglashay_plus")

BTN_LINK = "🔗 Моя ссылка"
BTN_PROFILE = "👤 Профиль"
BTN_BONUS = "🎁 Бонус"
BTN_WITHDRAW = "💳 Вывод средств"
BTN_AD = f"📣 Заказать рекламу · {AD_PRICE_RUB:,} ₽".replace(",", " ")
BTN_HOW = "❓ Как это работает"
BTN_REVIEWS = "⭐ Отзывы"
BTN_SUPPORT = "🆘 Поддержка"
BTN_HOME = "🏠 Главное меню"
BTN_ADMIN = "🛠 Админ-панель"

METHOD_LABELS = {"card": "Банковская карта", "sbp": "СБП"}
TX_LABELS = {
    "referral_l1": "Реферал · уровень 1",
    "referral_l2": "Реферал · уровень 2",
    "referral_l3": "Реферал · уровень 3",
    "daily_bonus": "Ежедневный бонус",
    "withdrawal_hold": "Резерв вывода",
    "withdrawal_refund": "Возврат вывода",
    "withdrawal_paid": "Вывод отмечен выплаченным",
}


def now_moscow() -> datetime:
    return datetime.now(MOSCOW)


def iso_now() -> str:
    return now_moscow().isoformat(timespec="seconds")


def money(kopeks: int) -> str:
    sign = "−" if int(kopeks) < 0 else ""
    rubles, cents = divmod(abs(int(kopeks)), 100)
    rub = f"{rubles:,}".replace(",", " ")
    return f"{sign}{rub}{f',{cents:02d}' if cents else ''} ₽"


def esc(value: object | None) -> str:
    return html.escape(str(value or ""), quote=True)


def parse_money(text: str) -> int:
    raw = text.strip().lower()
    for suffix in ("рублей", "рубля", "рубль", "руб.", "р.", "₽"):
        raw = raw.replace(suffix, "")
    raw = raw.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        raise ValueError("Введи сумму цифрами, например 2500 или 2500,50.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Не удалось распознать сумму.") from exc
    if value <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    return int(value * 100)


def api_amount_to_kopeks(value: object) -> int:
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def payment_min_credit(price_kopeks: int, payment_type: str) -> int:
    """Allow the YooMoney fee to be deducted from the recipient's credited amount."""
    if payment_type == "AC":  # bank-card commission is described by YooMoney as up to 3%
        return int(Decimal(price_kopeks) * Decimal("0.97")) - 1
    # For wallet transfers the recipient-side fee is approximately 1%.
    return int(Decimal(price_kopeks) / Decimal("1.01")) - 2


def menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_LINK],
        [BTN_PROFILE, BTN_BONUS],
        [BTN_WITHDRAW],
        [BTN_AD],
        [BTN_HOW, BTN_REVIEWS],
        [BTN_SUPPORT],
        [BTN_HOME],
    ]
    if ADMIN_ID and user_id == ADMIN_ID:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def public_ad_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📣 Заказать рекламу · {AD_PRICE_RUB:,} ₽".replace(",", " "), callback_data="ad:start")],
            [InlineKeyboardButton("🔕 Не получать рекламные сообщения", callback_data="ads:optout")],
        ]
    )


class Store:
    """SQLite storage/business rules. All money is integer kopeks."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def init_db(self) -> None:
        c = self.conn()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    joined_at TEXT NOT NULL,
                    referrer_id INTEGER REFERENCES users(telegram_id),
                    balance_kopeks INTEGER NOT NULL DEFAULT 0 CHECK(balance_kopeks >= 0),
                    total_earned_kopeks INTEGER NOT NULL DEFAULT 0 CHECK(total_earned_kopeks >= 0),
                    total_paid_kopeks INTEGER NOT NULL DEFAULT 0 CHECK(total_paid_kopeks >= 0),
                    bonus_last_date TEXT,
                    last_active_at TEXT NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    banned INTEGER NOT NULL DEFAULT 0,
                    ads_opt_out INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    kind TEXT NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    related_id TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    amount_kopeks INTEGER NOT NULL CHECK(amount_kopeks > 0),
                    method TEXT NOT NULL CHECK(method IN ('card','sbp')),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')),
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    processed_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS ad_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    price_kopeks INTEGER NOT NULL,
                    label TEXT NOT NULL UNIQUE,
                    payment_url TEXT NOT NULL,
                    payment_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'awaiting_payment',
                    payment_operation_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    paid_at TEXT,
                    content_chat_id INTEGER,
                    content_message_id INTEGER,
                    content_text TEXT,
                    content_file_id TEXT,
                    reviewed_at TEXT,
                    reviewed_by INTEGER,
                    broadcast_id INTEGER,
                    rejection_note TEXT
                );
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    order_id INTEGER REFERENCES ad_orders(id),
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    target_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
                CREATE INDEX IF NOT EXISTS idx_users_activity ON users(last_active_at);
                CREATE INDEX IF NOT EXISTS idx_transactions_user_kind ON transactions(user_id,kind);
                CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
                CREATE INDEX IF NOT EXISTS idx_ads_status ON ad_orders(status);
                CREATE INDEX IF NOT EXISTS idx_ads_user ON ad_orders(user_id,status);
                """
            )
            # Light migration for an earlier database made by the starter project.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            for name, ddl in (
                ("last_active_at", "TEXT NOT NULL DEFAULT ''"),
                ("blocked", "INTEGER NOT NULL DEFAULT 0"),
                ("banned", "INTEGER NOT NULL DEFAULT 0"),
                ("ads_opt_out", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in cols:
                    c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
            c.execute("UPDATE users SET last_active_at=joined_at WHERE last_active_at='' OR last_active_at IS NULL")
            ad_cols = {r["name"] for r in c.execute("PRAGMA table_info(ad_orders)").fetchall()}
            for name, ddl in (("content_text", "TEXT"), ("content_file_id", "TEXT")):
                if name not in ad_cols:
                    c.execute(f"ALTER TABLE ad_orders ADD COLUMN {name} {ddl}")
        finally:
            c.close()

    @staticmethod
    def credit(c: sqlite3.Connection, user_id: int, amount: int, kind: str, desc: str, now: str) -> None:
        c.execute(
            "UPDATE users SET balance_kopeks=balance_kopeks+?, total_earned_kopeks=total_earned_kopeks+? WHERE telegram_id=?",
            (amount, amount, user_id),
        )
        c.execute(
            "INSERT INTO transactions(user_id,kind,amount_kopeks,description,created_at) VALUES(?,?,?,?,?)",
            (user_id, kind, amount, desc, now),
        )

    def register_user(self, user_id: int, username: str | None, first_name: str, ref_candidate: int | None, now: str) -> dict:
        rewards: list[dict] = []
        with self.tx() as c:
            old = c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if old:
                c.execute(
                    "UPDATE users SET username=?,first_name=?,last_active_at=?,blocked=0 WHERE telegram_id=?",
                    (username, first_name or "", now, user_id),
                )
                return {"created": False, "rewards": []}
            referrer = None
            if ref_candidate and ref_candidate != user_id:
                exists = c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (ref_candidate,)).fetchone()
                if exists:
                    referrer = ref_candidate
            c.execute(
                "INSERT INTO users(telegram_id,username,first_name,joined_at,referrer_id,last_active_at) VALUES(?,?,?,?,?,?)",
                (user_id, username, first_name or "", now, referrer, now),
            )
            parent_id = referrer
            kinds = ("referral_l1", "referral_l2", "referral_l3")
            for level, (amount, kind) in enumerate(zip(REFERRAL_REWARDS, kinds), 1):
                if parent_id is None:
                    break
                parent = c.execute("SELECT referrer_id FROM users WHERE telegram_id=?", (parent_id,)).fetchone()
                if not parent:
                    break
                self.credit(c, parent_id, amount, kind, f"Новый пользователь #{user_id}, уровень {level}", now)
                rewards.append({"user_id": parent_id, "amount_kopeks": amount, "level": level})
                parent_id = parent["referrer_id"]
        return {"created": True, "rewards": rewards}

    def touch_user(self, user_id: int, now: str) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE users SET last_active_at=?,blocked=0 WHERE telegram_id=?", (now, user_id))
        finally:
            c.close()

    def get_user(self, user_id: int) -> dict | None:
        c = self.conn()
        try:
            row = c.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def profile(self, user_id: int) -> dict | None:
        c = self.conn()
        try:
            u = c.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not u:
                return None
            counts = {1: 0, 2: 0, 3: 0}
            for row in c.execute(
                """WITH RECURSIVE team(uid,level) AS (
                       SELECT telegram_id,1 FROM users WHERE referrer_id=?
                       UNION ALL SELECT u.telegram_id,t.level+1 FROM users u JOIN team t ON u.referrer_id=t.uid WHERE t.level<3
                   ) SELECT level,COUNT(*) n FROM team GROUP BY level""",
                (user_id,),
            ):
                counts[int(row["level"])] = int(row["n"])
            earnings = {1: 0, 2: 0, 3: 0}
            kind_levels = {"referral_l1": 1, "referral_l2": 2, "referral_l3": 3}
            for row in c.execute(
                "SELECT kind,COALESCE(SUM(amount_kopeks),0) amount FROM transactions WHERE user_id=? AND kind LIKE 'referral_l%' GROUP BY kind",
                (user_id,),
            ):
                earnings[kind_levels[row["kind"]]] = int(row["amount"])
            pending = c.execute(
                "SELECT COALESCE(SUM(amount_kopeks),0) amount FROM withdrawals WHERE user_id=? AND status='pending'",
                (user_id,),
            ).fetchone()["amount"]
            return {
                "telegram_id": int(u["telegram_id"]), "username": u["username"], "first_name": u["first_name"],
                "joined_at": u["joined_at"], "referrer_id": u["referrer_id"],
                "balance_kopeks": int(u["balance_kopeks"]), "total_earned_kopeks": int(u["total_earned_kopeks"]),
                "total_paid_kopeks": int(u["total_paid_kopeks"]), "pending_kopeks": int(pending or 0),
                "bonus_last_date": u["bonus_last_date"], "last_active_at": u["last_active_at"],
                "blocked": int(u["blocked"]), "banned": int(u["banned"]), "ads_opt_out": int(u["ads_opt_out"]),
                "level_counts": counts, "level_earnings": earnings,
            }
        finally:
            c.close()

    def claim_bonus(self, user_id: int, day: str, now: str) -> dict:
        with self.tx() as c:
            u = c.execute("SELECT balance_kopeks,bonus_last_date FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not u:
                return {"status": "missing"}
            if u["bonus_last_date"] == day:
                return {"status": "already", "balance": int(u["balance_kopeks"])}
            c.execute(
                "UPDATE users SET balance_kopeks=balance_kopeks+?,total_earned_kopeks=total_earned_kopeks+?,bonus_last_date=? WHERE telegram_id=?",
                (DAILY_BONUS, DAILY_BONUS, day, user_id),
            )
            c.execute(
                "INSERT INTO transactions(user_id,kind,amount_kopeks,description,created_at) VALUES(?,?,?,?,?)",
                (user_id, "daily_bonus", DAILY_BONUS, "Ежедневный бонус", now),
            )
            balance = c.execute("SELECT balance_kopeks FROM users WHERE telegram_id=?", (user_id,)).fetchone()[0]
            return {"status": "claimed", "balance": int(balance)}

    def create_withdrawal(self, user_id: int, amount: int, method: str, now: str) -> dict:
        if method not in METHOD_LABELS or amount <= 0:
            return {"status": "invalid"}
        with self.tx() as c:
            u = c.execute("SELECT balance_kopeks FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not u:
                return {"status": "missing"}
            balance = int(u["balance_kopeks"])
            if amount < MIN_WITHDRAWAL:
                return {"status": "below_minimum"}
            if amount > balance:
                return {"status": "insufficient", "balance": balance}
            c.execute("UPDATE users SET balance_kopeks=balance_kopeks-? WHERE telegram_id=?", (amount, user_id))
            cur = c.execute(
                "INSERT INTO withdrawals(user_id,amount_kopeks,method,status,created_at) VALUES(?,?,?,'pending',?)",
                (user_id, amount, method, now),
            )
            c.execute(
                "INSERT INTO transactions(user_id,kind,amount_kopeks,related_id,description,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, "withdrawal_hold", -amount, str(cur.lastrowid), f"Резерв заявки #{cur.lastrowid}", now),
            )
            return {"status": "created", "id": int(cur.lastrowid), "balance": balance - amount}

    def settle_withdrawal(self, wd_id: int, action: str, admin_id: int, now: str) -> dict:
        with self.tx() as c:
            w = c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
            if not w:
                return {"status": "not_found"}
            if w["status"] != "pending":
                return {"status": "already", "current": w["status"]}
            user_id, amount = int(w["user_id"]), int(w["amount_kopeks"])
            if action == "paid":
                c.execute("UPDATE withdrawals SET status='paid',processed_at=?,processed_by=? WHERE id=?", (now, admin_id, wd_id))
                c.execute("UPDATE users SET total_paid_kopeks=total_paid_kopeks+? WHERE telegram_id=?", (amount, user_id))
                c.execute(
                    "INSERT INTO transactions(user_id,kind,amount_kopeks,related_id,description,created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, "withdrawal_paid", 0, str(wd_id), f"Заявка #{wd_id} отмечена выплаченной", now),
                )
            elif action == "rejected":
                c.execute("UPDATE withdrawals SET status='rejected',processed_at=?,processed_by=? WHERE id=?", (now, admin_id, wd_id))
                c.execute("UPDATE users SET balance_kopeks=balance_kopeks+? WHERE telegram_id=?", (amount, user_id))
                c.execute(
                    "INSERT INTO transactions(user_id,kind,amount_kopeks,related_id,description,created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, "withdrawal_refund", amount, str(wd_id), f"Возврат заявки #{wd_id}", now),
                )
            else:
                return {"status": "invalid"}
            return {"status": action, "user_id": user_id, "amount": amount, "method": w["method"]}

    def get_withdrawals(self, limit: int = 20) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT w.*,u.username,u.first_name FROM withdrawals w JOIN users u ON u.telegram_id=w.user_id
                   WHERE w.status='pending' ORDER BY w.id ASC LIMIT ?""", (max(1, min(limit, 50)),)
            ).fetchall()]
        finally:
            c.close()

    def create_ad_order(self, user_id: int, label: str, payment_url: str, payment_type: str, now: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO ad_orders(user_id,price_kopeks,label,payment_url,payment_type,status,created_at) VALUES(?,?,?,?,?,'awaiting_payment',?)",
                (user_id, AD_PRICE_KOPEKS, label, payment_url, payment_type, now),
            )
            return int(cur.lastrowid)

    def active_ad_order(self, user_id: int) -> dict | None:
        c = self.conn()
        try:
            row = c.execute(
                """SELECT * FROM ad_orders WHERE user_id=? AND status IN
                   ('awaiting_payment','awaiting_content','pending_review','broadcasting') ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def pending_payment_orders(self, limit: int = 20) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM ad_orders WHERE status='awaiting_payment' ORDER BY id ASC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()]
        finally:
            c.close()

    def get_ad_order(self, order_id: int) -> dict | None:
        c = self.conn()
        try:
            row = c.execute(
                """SELECT a.*,u.username,u.first_name FROM ad_orders a JOIN users u ON u.telegram_id=a.user_id
                   WHERE a.id=?""", (order_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def list_ad_orders(self, limit: int = 20) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT a.*,u.username,u.first_name FROM ad_orders a JOIN users u ON u.telegram_id=a.user_id
                   ORDER BY a.id DESC LIMIT ?""", (max(1, min(limit, 50)),)
            ).fetchall()]
        finally:
            c.close()

    def mark_ad_paid(self, order_id: int, op_id: str | None, admin_id: int | None, now: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM ad_orders WHERE id=? AND status='awaiting_payment'", (order_id,)).fetchone()
            if not row:
                return None
            if op_id:
                used = c.execute("SELECT id FROM ad_orders WHERE payment_operation_id=?", (op_id,)).fetchone()
                if used:
                    return None
            c.execute(
                "UPDATE ad_orders SET status='awaiting_content',paid_at=?,payment_operation_id=? WHERE id=? AND status='awaiting_payment'",
                (now, op_id or f"manual-{admin_id}-{order_id}", order_id),
            )
            result = dict(row)
            result["status"] = "awaiting_content"
            result["paid_at"] = now
            return result

    def set_ad_content(self, order_id: int, user_id: int, chat_id: int, message_id: int, content_text: str, file_id: str | None, now: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                """UPDATE ad_orders SET status='pending_review',content_chat_id=?,content_message_id=?,content_text=?,content_file_id=?
                   WHERE id=? AND user_id=? AND status='awaiting_content'""",
                (chat_id, message_id, content_text, file_id, order_id, user_id),
            )
            return cur.rowcount == 1

    def claim_ad_for_broadcast(self, order_id: int, admin_id: int, now: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM ad_orders WHERE id=? AND status='pending_review'", (order_id,)).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE ad_orders SET status='broadcasting',reviewed_at=?,reviewed_by=? WHERE id=? AND status='pending_review'",
                (now, admin_id, order_id),
            )
            result = dict(row)
            result["status"] = "broadcasting"
            return result

    def reject_ad_order(self, order_id: int, admin_id: int, note: str, now: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM ad_orders WHERE id=? AND status='pending_review'", (order_id,)).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE ad_orders SET status='rejected',reviewed_at=?,reviewed_by=?,rejection_note=? WHERE id=?",
                (now, admin_id, note, order_id),
            )
            return dict(row)

    def cancel_unpaid_ad_order(self, order_id: int, admin_id: int, now: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM ad_orders WHERE id=? AND status='awaiting_payment'", (order_id,)).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE ad_orders SET status='cancelled',reviewed_at=?,reviewed_by=? WHERE id=? AND status='awaiting_payment'",
                (now, admin_id, order_id),
            )
            return dict(row)

    def finish_ad_order(self, order_id: int, broadcast_id: int, now: str) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE ad_orders SET status='broadcasted',broadcast_id=? WHERE id=? AND status='broadcasting'", (broadcast_id, order_id))
        finally:
            c.close()

    def create_broadcast(self, admin_id: int, source_chat_id: int, source_message_id: int, order_id: int | None, targets: int, now: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO broadcasts(admin_id,order_id,source_chat_id,source_message_id,created_at,status,target_count) VALUES(?,?,?,?,?,'running',?)",
                (admin_id, order_id, source_chat_id, source_message_id, now, targets),
            )
            bid = int(cur.lastrowid)
            if order_id:
                c.execute("UPDATE ad_orders SET broadcast_id=? WHERE id=?", (bid, order_id))
            return bid

    def update_broadcast(self, bid: int, sent: int, failed: int) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE broadcasts SET sent_count=?,failed_count=? WHERE id=?", (sent, failed, bid))
        finally:
            c.close()

    def finish_broadcast(self, bid: int, sent: int, failed: int) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE broadcasts SET status='done',sent_count=?,failed_count=? WHERE id=?", (sent, failed, bid))
        finally:
            c.close()

    def broadcast_targets(self, admin_id: int) -> list[int]:
        c = self.conn()
        try:
            return [int(r[0]) for r in c.execute(
                "SELECT telegram_id FROM users WHERE telegram_id<>? AND blocked=0 AND banned=0 AND ads_opt_out=0 ORDER BY telegram_id",
                (admin_id,),
            ).fetchall()]
        finally:
            c.close()

    def mark_blocked(self, user_id: int) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE users SET blocked=1 WHERE telegram_id=?", (user_id,))
        finally:
            c.close()

    def set_ads_optout(self, user_id: int, opt_out: bool) -> None:
        c = self.conn()
        try:
            c.execute("UPDATE users SET ads_opt_out=? WHERE telegram_id=?", (int(opt_out), user_id))
        finally:
            c.close()

    def toggle_ban(self, user_id: int) -> int | None:
        with self.tx() as c:
            row = c.execute("SELECT banned FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not row:
                return None
            new_value = 0 if row["banned"] else 1
            c.execute("UPDATE users SET banned=? WHERE telegram_id=?", (new_value, user_id))
            return new_value

    def users_page(self, offset: int = 0, limit: int = 10, search: str = "") -> list[dict]:
        c = self.conn()
        try:
            q = search.strip().lstrip("@").lower()
            if q:
                if q.isdigit():
                    rows = c.execute(
                        """SELECT * FROM users WHERE CAST(telegram_id AS TEXT) LIKE ? OR lower(COALESCE(username,'')) LIKE ?
                           ORDER BY joined_at DESC LIMIT ? OFFSET ?""",
                        (f"%{q}%", f"%{q}%", limit, max(0, offset)),
                    ).fetchall()
                else:
                    rows = c.execute(
                        """SELECT * FROM users WHERE lower(COALESCE(username,'')) LIKE ? OR lower(first_name) LIKE ?
                           ORDER BY joined_at DESC LIMIT ? OFFSET ?""",
                        (f"%{q}%", f"%{q}%", limit, max(0, offset)),
                    ).fetchall()
            else:
                rows = c.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, max(0, offset))).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def user_count(self, search: str = "") -> int:
        c = self.conn()
        try:
            q = search.strip().lstrip("@").lower()
            if not q:
                return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if q.isdigit():
                row = c.execute(
                    "SELECT COUNT(*) FROM users WHERE CAST(telegram_id AS TEXT) LIKE ? OR lower(COALESCE(username,'')) LIKE ?",
                    (f"%{q}%", f"%{q}%"),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) FROM users WHERE lower(COALESCE(username,'')) LIKE ? OR lower(first_name) LIKE ?",
                    (f"%{q}%", f"%{q}%"),
                ).fetchone()
            return int(row[0])
        finally:
            c.close()

    def top_referrers(self, limit: int = 10) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT u.telegram_id,u.username,u.first_name,u.total_earned_kopeks,
                          COUNT(child.telegram_id) AS direct_count
                   FROM users u LEFT JOIN users child ON child.referrer_id=u.telegram_id
                   GROUP BY u.telegram_id ORDER BY direct_count DESC,u.total_earned_kopeks DESC LIMIT ?""",
                (limit,),
            ).fetchall()]
        finally:
            c.close()

    def recent_transactions(self, limit: int = 15) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT t.*,u.username,u.first_name FROM transactions t JOIN users u ON u.telegram_id=t.user_id
                   ORDER BY t.id DESC LIMIT ?""", (limit,)
            ).fetchall()]
        finally:
            c.close()

    def users_for_export(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT u.telegram_id,u.username,u.first_name,u.joined_at,u.last_active_at,u.referrer_id,
                          u.balance_kopeks,u.total_earned_kopeks,u.total_paid_kopeks,u.blocked,u.banned,u.ads_opt_out,
                          (SELECT COUNT(*) FROM users x WHERE x.referrer_id=u.telegram_id) AS level1_referrals,
                          (SELECT COUNT(*) FROM users x WHERE x.referrer_id IN
                            (SELECT y.telegram_id FROM users y WHERE y.referrer_id=u.telegram_id)) AS level2_referrals,
                          (SELECT COUNT(*) FROM users x WHERE x.referrer_id IN
                            (SELECT y.telegram_id FROM users y WHERE y.referrer_id IN
                              (SELECT z.telegram_id FROM users z WHERE z.referrer_id=u.telegram_id))) AS level3_referrals
                   FROM users u ORDER BY u.joined_at DESC"""
            ).fetchall()]
        finally:
            c.close()

    def admin_stats(self, now: datetime) -> dict[str, int]:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
        week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
        month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")
        c = self.conn()
        try:
            user = c.execute(
                """SELECT COUNT(*) total, SUM(balance_kopeks) balances, SUM(total_earned_kopeks) earned,
                          SUM(total_paid_kopeks) paid,
                          SUM(CASE WHEN joined_at>=? THEN 1 ELSE 0 END) new_today,
                          SUM(CASE WHEN last_active_at>=? THEN 1 ELSE 0 END) active_24h,
                          SUM(CASE WHEN last_active_at>=? THEN 1 ELSE 0 END) active_7d,
                          SUM(CASE WHEN last_active_at>=? THEN 1 ELSE 0 END) active_30d,
                          SUM(blocked) blocked, SUM(banned) banned, SUM(ads_opt_out) opted_out
                   FROM users""",
                (today, day_ago, week_ago, month_ago),
            ).fetchone()
            w = c.execute(
                "SELECT COUNT(*) n,COALESCE(SUM(amount_kopeks),0) amount FROM withdrawals WHERE status='pending'"
            ).fetchone()
            paid_wd = c.execute("SELECT COALESCE(SUM(amount_kopeks),0) amount FROM withdrawals WHERE status='paid'").fetchone()["amount"]
            referral = c.execute("SELECT COALESCE(SUM(amount_kopeks),0) amount FROM transactions WHERE kind LIKE 'referral_l%' AND amount_kopeks>0").fetchone()["amount"]
            bonus = c.execute("SELECT COALESCE(SUM(amount_kopeks),0) amount FROM transactions WHERE kind='daily_bonus'").fetchone()["amount"]
            ads = c.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN paid_at IS NOT NULL THEN 1 ELSE 0 END) paid_count,
                          SUM(CASE WHEN paid_at IS NOT NULL THEN price_kopeks ELSE 0 END) revenue,
                          SUM(CASE WHEN status='awaiting_payment' THEN 1 ELSE 0 END) awaiting_payment,
                          SUM(CASE WHEN status='pending_review' THEN 1 ELSE 0 END) pending_review,
                          SUM(CASE WHEN status='broadcasted' THEN 1 ELSE 0 END) broadcasted
                   FROM ad_orders"""
            ).fetchone()
            b = c.execute("SELECT COALESCE(SUM(sent_count),0) sent,COUNT(*) campaigns FROM broadcasts WHERE status='done'").fetchone()
            return {
                "users": int(user["total"] or 0), "balances": int(user["balances"] or 0), "earned": int(user["earned"] or 0),
                "paid": int(user["paid"] or 0), "new_today": int(user["new_today"] or 0), "active_24h": int(user["active_24h"] or 0),
                "active_7d": int(user["active_7d"] or 0), "active_30d": int(user["active_30d"] or 0),
                "blocked": int(user["blocked"] or 0), "banned": int(user["banned"] or 0), "opted_out": int(user["opted_out"] or 0),
                "pending_withdrawals": int(w["n"] or 0), "pending_withdrawal_kopeks": int(w["amount"] or 0),
                "paid_withdrawal_kopeks": int(paid_wd or 0), "referral_kopeks": int(referral or 0), "bonus_kopeks": int(bonus or 0),
                "ads_total": int(ads["total"] or 0), "ads_paid_count": int(ads["paid_count"] or 0),
                "ads_revenue": int(ads["revenue"] or 0), "ads_awaiting_payment": int(ads["awaiting_payment"] or 0),
                "ads_pending_review": int(ads["pending_review"] or 0), "ads_broadcasted": int(ads["broadcasted"] or 0),
                "broadcast_sent": int(b["sent"] or 0), "campaigns": int(b["campaigns"] or 0),
            }
        finally:
            c.close()


def store(context: ContextTypes.DEFAULT_TYPE) -> Store:
    return context.application.bot_data["store"]


def payment_url(label: str) -> str:
    if not YOOMONEY_WALLET:
        return ""
    params = {
        "receiver": YOOMONEY_WALLET,
        "quickpay-form": "button",
        "paymentType": YOOMONEY_PAYMENT_TYPE,
        "sum": f"{AD_PRICE_RUB}.00",
        "label": label,
    }
    return YOOMONEY_FORM + "?" + urlencode(params)


def ad_order_keyboard(order: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"💳 Оплатить {AD_PRICE_RUB:,} ₽".replace(",", " "), url=order["payment_url"])]]
    )


async def send_banner(message, asset_name: str, caption: str, reply_markup=None, parse_mode=ParseMode.HTML) -> None:
    asset = ASSETS_DIR / asset_name
    try:
        with asset.open("rb") as photo:
            await message.reply_photo(photo=photo, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
    except (OSError, TelegramError):
        log.exception("Banner send failed: %s", asset_name)
        await message.reply_text(caption, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=True)


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    cached = context.application.bot_data.get("bot_username")
    if cached:
        return cached
    me = await context.bot.get_me()
    if not me.username:
        raise RuntimeError("У бота не задан username в BotFather.")
    context.application.bot_data["bot_username"] = me.username
    return me.username


async def activity_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and not user.is_bot:
        store(context).touch_user(user.id, iso_now())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    ref = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref = int(context.args[0][4:])
        except ValueError:
            ref = None
    result = store(context).register_user(user.id, user.username, user.first_name or "", ref, iso_now())
    record = store(context).get_user(user.id)
    if record and record["banned"]:
        await message.reply_text("Доступ к боту ограничен. Если считаешь это ошибкой, отправь команду /support.")
        return
    for reward in result["rewards"]:
        try:
            await context.bot.send_message(
                reward["user_id"],
                f"🎉 Новый участник впервые запустил бота. Начислено {money(reward['amount_kopeks'])} за {reward['level']} уровень.",
            )
        except TelegramError:
            log.info("Could not notify referrer %s", reward["user_id"])
    profile = store(context).profile(user.id)
    if result["created"]:
        caption = (
            f"👋 <b>Привет, {esc(user.first_name or 'друг')}!</b>\n\n"
            f"Добро пожаловать в «{esc(BOT_NAME)}» — приглашай новых участников и следи за начислениями прямо в Telegram.\n\n"
            "💸 Награды за первый запуск нового пользователя: 11 / 7 / 3 ₽ за три уровня.\n"
            "🎁 Бонус — 25 ₽ раз в день.\n"
            f"📣 Реклама в рассылке этого бота — {AD_PRICE_RUB:,} ₽; перед публикацией объявление проверяет администратор.\n"
            "💳 Вывод от 2 500 ₽ на карту или СБП обрабатывается вручную.\n\n"
            "Нажми кнопку ниже, чтобы начать. Все суммы и статистика в профиле — по фактическим данным аккаунта."
        ).replace(",", " ")
        asset = "welcome.png"
    else:
        caption = (
            f"👋 <b>С возвращением, {esc(user.first_name or 'друг')}!</b>\n\n"
            f"💰 Доступно: <b>{money(profile['balance_kopeks'] if profile else 0)}</b>\n"
            "Выбери раздел в меню ниже."
        )
        asset = "menu.png"
    await send_banner(message, asset, caption, menu_keyboard(user.id))


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    profile = store(context).profile(user.id)
    caption = (
        f"👋 <b>Главное меню · {esc(user.first_name or 'друг')}</b>\n\n"
        "💸 Реферальные уровни: 11 / 7 / 3 ₽\n"
        "🎁 Бонус: 25 ₽ раз в день\n"
        f"📣 Реклама в рассылке этого бота: {AD_PRICE_RUB:,} ₽\n"
        f"💰 Доступно: <b>{money(profile['balance_kopeks'] if profile else 0)}</b>\n\n"
        "Выбери действие ниже."
    ).replace(",", " ")
    await send_banner(message, "menu.png", caption, menu_keyboard(user.id))


async def show_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    username = await get_bot_username(context)
    link = f"https://t.me/{username}?start=ref_{user.id}"
    profile = store(context).profile(user.id)
    if not profile:
        await message.reply_text("Сначала нажми /start.", reply_markup=menu_keyboard(user.id))
        return
    share = "https://t.me/share/url?" + urlencode({"url": link, "text": f"Присоединяйся к {BOT_NAME} по моей ссылке!"})
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📣 Поделиться ссылкой", url=share)]])
    caption = (
        "🔗 <b>Твоя реферальная ссылка</b>\n\n"
        f"<code>{esc(link)}</code>\n\n"
        "За каждого нового пользователя, впервые запустившего бота по ссылке:\n"
        "• 11 ₽ — 1-й уровень\n• 7 ₽ — 2-й уровень\n• 3 ₽ — 3-й уровень\n\n"
        f"👥 Команда: {profile['level_counts'][1]} / {profile['level_counts'][2]} / {profile['level_counts'][3]}\n"
        "Повторные запуски одного Telegram-аккаунта не дают новую награду."
    )
    await send_banner(message, "referral.png", caption, keyboard)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    p = store(context).profile(user.id)
    if not p:
        await message.reply_text("Сначала нажми /start.", reply_markup=menu_keyboard(user.id))
        return
    caption = (
        f"👤 <b>Профиль · {esc(user.first_name or 'пользователь')}</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n📅 В боте с: {esc(p['joined_at'][:10])}\n"
        f"🕒 Последняя активность: {esc(p['last_active_at'][:16].replace('T', ' '))}\n\n"
        f"💰 Доступно: <b>{money(p['balance_kopeks'])}</b>\n"
        f"⏳ На рассмотрении: {money(p['pending_kopeks'])}\n"
        f"🏅 Начислено всего: {money(p['total_earned_kopeks'])}\n"
        f"✅ Отмечено выплаченным: {money(p['total_paid_kopeks'])}\n\n"
        f"👥 1 уровень: {p['level_counts'][1]} · {money(p['level_earnings'][1])}\n"
        f"👥 2 уровень: {p['level_counts'][2]} · {money(p['level_earnings'][2])}\n"
        f"👥 3 уровень: {p['level_counts'][3]} · {money(p['level_earnings'][3])}\n\n"
        f"🎁 Бонус: {money(DAILY_BONUS)} раз в календарный день по Москве."
    )
    await send_banner(message, "profile.png", caption, menu_keyboard(user.id))


async def claim_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    now = now_moscow()
    result = store(context).claim_bonus(user.id, now.date().isoformat(), now.isoformat(timespec="seconds"))
    if result["status"] == "missing":
        await message.reply_text("Сначала нажми /start.", reply_markup=menu_keyboard(user.id))
    elif result["status"] == "already":
        await message.reply_text(f"⏳ Бонус {money(DAILY_BONUS)} уже получен сегодня.\n💰 Доступно: {money(result['balance'])}", reply_markup=menu_keyboard(user.id))
    else:
        await message.reply_text(f"🎁 Начислено {money(DAILY_BONUS)}!\n💰 Доступно: {money(result['balance'])}", reply_markup=menu_keyboard(user.id))


async def show_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    p = store(context).profile(user.id)
    if not p:
        await message.reply_text("Сначала нажми /start.", reply_markup=menu_keyboard(user.id))
        return
    text = (
        "💳 <b>Вывод средств</b>\n\n"
        f"💰 Доступно: <b>{money(p['balance_kopeks'])}</b>\n⏳ На рассмотрении: {money(p['pending_kopeks'])}\n"
        f"Минимум: <b>{money(MIN_WITHDRAWAL)}</b>\nСпособы: карта или СБП. Заявки проверяются вручную.\n\n"
    )
    if p["balance_kopeks"] < MIN_WITHDRAWAL:
        text += f"До минимума не хватает {money(MIN_WITHDRAWAL - p['balance_kopeks'])}. После достижения минимума можно будет создать заявку и передать реквизиты администратору через бота."
        await send_banner(message, "withdrawal.png", text, menu_keyboard(user.id))
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Банковская карта", callback_data="withdraw:method:card")],
        [InlineKeyboardButton("📱 СБП", callback_data="withdraw:method:sbp")],
    ])
    await send_banner(message, "withdrawal.png", text + "Выбери способ. Реквизиты запрашиваются только после создания заявки; не отправляй CVV, PIN или коды из SMS.", keyboard)


async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if not REVIEWS_URL:
        await message.reply_text(
            "⭐ Ссылка на отзывы пока не настроена. Добавь REVIEWS_URL в .env; бот не будет показывать вымышленные отзывы.",
            reply_markup=menu_keyboard(update.effective_user.id),
        )
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Открыть отзывы", url=REVIEWS_URL)]])
    await message.reply_text("⭐ Официальная страница отзывов:", reply_markup=keyboard)


async def show_how(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    caption = (
        "❓ <b>Как это работает</b>\n\n"
        "1️⃣ Возьми ссылку в разделе «Моя ссылка».\n"
        "2️⃣ Начисление за нового участника происходит при первом запуске бота по ссылке: 11 / 7 / 3 ₽ за три уровня.\n"
        "3️⃣ Бонус 25 ₽ — один раз за календарный день по Москве.\n"
        f"4️⃣ Вывод от {money(MIN_WITHDRAWAL)} на карту или СБП: заявка рассматривается вручную.\n"
        f"5️⃣ Заказ рекламного сообщения в рассылке этого бота — {AD_PRICE_RUB} ₽. После оплаты отправь одно текстовое сообщение или изображение с подписью. Администратор проверит объявление перед рассылкой.\n\n"
        "Повторные запуски того же аккаунта и самоприглашения не засчитываются. Рекламную рассылку можно отключить кнопкой под рекламой."
    )
    await send_banner(message, "how_it_works.png", caption, menu_keyboard(update.effective_user.id))


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if context.user_data.get("withdraw_state") == "amount":
        context.user_data.pop("withdraw_state", None)
        context.user_data.pop("withdraw_method", None)
    context.user_data["support_state"] = "waiting"
    withdrawal_id = context.user_data.get("support_withdrawal_id")
    case_text = f" по заявке на вывод #{withdrawal_id}" if withdrawal_id else ""
    await message.reply_text(
        f"🆘 Напиши вопрос{case_text} одним текстовым сообщением — оно будет передано администратору через бота.\n"
        "Не отправляй пароли, CVV/CVC, PIN-коды или коды из SMS. /cancel — отмена."
    )


async def forward_support_message(message, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Отправь обращение текстом одним сообщением.")
        return
    if len(text) > 3000:
        await message.reply_text("Сообщение слишком длинное. Сократи его до 3 000 символов.")
        return
    withdrawal_id = context.user_data.get("support_withdrawal_id")
    title = f"Реквизиты/обращение по заявке на вывод #{withdrawal_id}" if withdrawal_id else "Обращение в поддержку"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Ответить пользователю", callback_data=f"support:reply:{user.id}")]])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🆘 <b>{title}</b>\n"
            f"Пользователь: <a href=\"tg://user?id={user.id}\">{esc(user.full_name)}</a> · ID <code>{user.id}</code>\n\n"
            f"<pre>{esc(text)}</pre>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    context.user_data.pop("support_state", None)
    context.user_data.pop("support_withdrawal_id", None)
    await message.reply_text("✅ Сообщение передано администратору. Если понадобится, ответ придёт в этот чат.")


async def create_ad_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    rec = store(context).get_user(user.id)
    if rec and rec["banned"]:
        await message.reply_text("Доступ ограничен. Если считаешь это ошибкой, отправь команду /support.")
        return
    active = store(context).active_ad_order(user.id)
    if active:
        if active["status"] == "awaiting_payment":
            await message.reply_text(
                f"У тебя уже есть ожидающий заказ рекламы <b>#{active['id']}</b> на {money(active['price_kopeks'])}. Оплати по этой кнопке:",
                parse_mode=ParseMode.HTML,
                reply_markup=ad_order_keyboard(active),
            )
        elif active["status"] == "awaiting_content":
            await message.reply_text(f"Оплата заказа #{active['id']} подтверждена. Отправь одним сообщением рекламный текст или картинку с подписью.")
        else:
            await message.reply_text(f"Заказ рекламы #{active['id']} сейчас в статусе «{esc(active['status'])}». Новую заявку можно создать после его обработки.", parse_mode=ParseMode.HTML)
        return
    if not YOOMONEY_WALLET:
        await message.reply_text("Заказ рекламы пока не настроен. Попробуй позже.")
        return
    label = "VB-AD-" + secrets.token_hex(8).upper()
    pay_url = payment_url(label)
    order_id = store(context).create_ad_order(user.id, label, pay_url, YOOMONEY_PAYMENT_TYPE, iso_now())
    order = store(context).get_ad_order(order_id)
    text = (
        f"📣 <b>Заказ рекламы #{order_id}</b>\n\n"
        f"Стоимость: <b>{money(AD_PRICE_KOPEKS)}</b>.\n"
        "После оплаты бот сверит поступление по уникальной метке заказа. Затем отправь одним сообщением текст (можно со ссылкой) или изображение с подписью.\n"
        "Перед публикацией объявление проверит администратор. Неподходящий материал не будет разослан; возврат при отклонении оформляется вручную. По вопросу отправь /support.\n\n"
        f"Метка платежа: <code>{esc(label)}</code>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ad_order_keyboard(order))


async def payment_check(client: httpx.AsyncClient, order: dict) -> tuple[dict | None, str | None]:
    if not YOOMONEY_TOKEN:
        return None, "YOOMONEY_TOKEN не задан"
    headers = {"Authorization": f"Bearer {YOOMONEY_TOKEN}"}
    # YooMoney documents POST /api/operation-history with the operation-history scope.
    response = await client.post(
        YOOMONEY_API,
        headers=headers,
        data={"records": "100", "label": order["label"]},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        return None, str(payload["error"])
    for op in payload.get("operations", []):
        if op.get("label") != order["label"]:
            continue
        if op.get("status") != "success" or op.get("direction") != "in" or not op.get("operation_id"):
            continue
        received = api_amount_to_kopeks(op.get("amount", 0))
        required = payment_min_credit(int(order["price_kopeks"]), order.get("payment_type", YOOMONEY_PAYMENT_TYPE))
        if received >= required:
            return op, None
    return None, None


async def verify_pending_payments(context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = store(context).pending_payment_orders(20)
    if not orders or not YOOMONEY_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        for order in orders:
            try:
                op, error = await payment_check(client, order)
                if error:
                    raise RuntimeError(error)
                if not op:
                    continue
                paid = store(context).mark_ad_paid(order["id"], str(op.get("operation_id", "")), None, iso_now())
                if not paid:
                    continue
                await context.bot.send_message(
                    chat_id=paid["user_id"],
                    text=(
                        f"✅ Оплата по заказу <b>#{paid['id']}</b> найдена.\n"
                        "Теперь отправь ОДНО сообщение: рекламный текст (можно со ссылкой) или изображение с подписью. "
                        "После проверки администратор решит, допускать ли его к рассылке."
                    ),
                    parse_mode=ParseMode.HTML,
                )
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"💳 YooMoney подтвердил оплату заказа рекламы <b>#{paid['id']}</b> · {money(paid['price_kopeks'])}.",
                    parse_mode=ParseMode.HTML,
                )
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response else 0
                log.warning("YooMoney API HTTP error %s for order %s", code, order["id"])
                if code in (401, 403) and not context.application.bot_data.get("yoomoney_auth_warned"):
                    context.application.bot_data["yoomoney_auth_warned"] = True
                    try:
                        await context.bot.send_message(
                            ADMIN_ID,
                            "⚠️ YooMoney API отклонил токен. Проверь .env и право operation-history; заказы можно сверить вручную в админке.",
                        )
                    except TelegramError:
                        pass
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                log.warning("YooMoney check failed for order %s: %s", order["id"], exc)
            except TelegramError:
                log.exception("Could not notify user/admin about paid ad order %s", order["id"])


async def verify_one_order(order_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    order = store(context).get_ad_order(order_id)
    if not order or order["status"] != "awaiting_payment" or not YOOMONEY_TOKEN:
        return False
    async with httpx.AsyncClient(timeout=15.0) as client:
        op, error = await payment_check(client, order)
    if error or not op:
        return False
    paid = store(context).mark_ad_paid(order_id, str(op.get("operation_id", "")), None, iso_now())
    if not paid:
        return False
    await context.bot.send_message(
        paid["user_id"],
        f"✅ Оплата заказа рекламы #{order_id} подтверждена. Отправь одним сообщением текст или картинку с подписью.",
    )
    return True


async def process_ad_content(message, user, context: ContextTypes.DEFAULT_TYPE, order: dict) -> None:
    if message.media_group_id:
        await message.reply_text("Для заказа можно отправить одно изображение за раз, не альбом. Отправь текст или одну картинку с подписью.")
        return
    if not message.text and not (message.photo and message.caption):
        await message.reply_text("Отправь рекламный текст (можно со ссылкой) или одну картинку с подписью.")
        return
    if message.text and len(message.text) > 3900:
        await message.reply_text("Текст объявления слишком длинный. Уменьши его до 3 900 символов.")
        return
    if message.caption and len(message.caption) > 900:
        await message.reply_text("Подпись к картинке слишком длинная. Уменьши её до 900 символов.")
        return
    content_text = message.text or message.caption or ""
    file_id = message.photo[-1].file_id if message.photo else None
    ok = store(context).set_ad_content(
        order["id"], user.id, message.chat_id, message.message_id, content_text, file_id, iso_now()
    )
    if not ok:
        await message.reply_text("Не удалось сохранить объявление. Попробуй позже или отправь /support.")
        return
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить и разослать", callback_data=f"ad:approve:{order['id']}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"ad:reject:{order['id']}")],
    ])
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"🧾 <b>Реклама #{order['id']} на проверке</b>\nПользователь: <a href=\"tg://user?id={user.id}\">{esc(user.full_name)}</a> · ID <code>{user.id}</code>\nОплата подтверждена: {money(order['price_kopeks'])}.\nНиже — присланное объявление.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb,
        )
        if file_id:
            await context.bot.send_photo(ADMIN_ID, photo=file_id, caption=content_text or None)
        else:
            await context.bot.send_message(ADMIN_ID, text=content_text)
    except TelegramError:
        log.exception("Could not send ad content to admin for order %s", order["id"])
    await message.reply_text(f"✅ Объявление по заказу #{order['id']} получено и отправлено на проверку. Рассылка начнётся только после одобрения администратора.")


async def notify_ad_content_waiting(order: dict, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.send_message(
            user_id,
            f"✅ Оплата заказа рекламы #{order['id']} подтверждена. Отправь одним сообщением текст или изображение с подписью.",
        )
    except TelegramError:
        log.exception("Could not notify user %s after payment", user_id)


async def run_broadcast(app: Application, admin_id: int, source_chat_id: int, source_message_id: int, order_id: int | None = None) -> None:
    st: Store = app.bot_data["store"]
    targets = st.broadcast_targets(admin_id)
    ad_content = st.get_ad_order(order_id) if order_id else None
    bid = st.create_broadcast(admin_id, source_chat_id, source_message_id, order_id, len(targets), iso_now())
    sent = failed = 0
    markup = public_ad_keyboard()

    async def send_to(target_id: int) -> None:
        if ad_content:
            if ad_content.get("content_file_id"):
                await app.bot.send_photo(
                    chat_id=target_id, photo=ad_content["content_file_id"],
                    caption=ad_content.get("content_text") or None, reply_markup=markup,
                )
            else:
                await app.bot.send_message(
                    chat_id=target_id, text=ad_content.get("content_text") or " ", reply_markup=markup,
                    disable_web_page_preview=False,
                )
        else:
            await app.bot.copy_message(
                chat_id=target_id, from_chat_id=source_chat_id,
                message_id=source_message_id, reply_markup=markup,
            )

    for index, target in enumerate(targets, start=1):
        delivered = False
        try:
            await send_to(target)
            delivered = True
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await send_to(target)
                delivered = True
            except TelegramError:
                failed += 1
        except Forbidden:
            st.mark_blocked(target)
            failed += 1
        except (BadRequest, TelegramError):
            failed += 1
            log.info("Broadcast copy failed for user %s", target)
        if delivered:
            sent += 1
        if index % 25 == 0:
            st.update_broadcast(bid, sent, failed)
        # Conservative pace; RetryAfter is also handled above.
        await asyncio.sleep(0.04)
    st.finish_broadcast(bid, sent, failed)
    if order_id:
        st.finish_ad_order(order_id, bid, iso_now())
    try:
        await app.bot.send_message(
            admin_id,
            f"📣 Рассылка #{bid} завершена. Доставлено: {sent}, ошибок: {failed}, адресатов на старте: {len(targets)}.",
        )
    except TelegramError:
        log.exception("Could not send broadcast completion to admin")
    if order_id:
        order = st.get_ad_order(order_id)
        if order:
            try:
                await app.bot.send_message(
                    order["user_id"],
                    f"📣 Заказ рекламы #{order_id} разослан. Доставлено сообщений: {sent}.",
                )
            except TelegramError:
                pass


async def admin_home(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Полная статистика", callback_data="admin:stats"), InlineKeyboardButton("👥 Пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton("🔎 Найти пользователя", callback_data="admin:search"), InlineKeyboardButton("🏆 Топ приглашений", callback_data="admin:top")],
        [InlineKeyboardButton("💸 Заявки на вывод", callback_data="admin:withdrawals"), InlineKeyboardButton("📣 Заказы рекламы", callback_data="admin:ads")],
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="admin:broadcast")],
        [InlineKeyboardButton("📜 Последние операции", callback_data="admin:recent"), InlineKeyboardButton("📥 Экспорт CSV", callback_data="admin:export")],
    ])
    token_status = "✅ API-проверка включена" if YOOMONEY_TOKEN else "⚠️ API-токен не задан"
    wallet_status = "✅ кошелёк задан" if YOOMONEY_WALLET else "⚠️ кошелёк не задан"
    await message.reply_text(
        f"🛠 <b>Админ-панель · {esc(BOT_NAME)}</b>\n{token_status} · {wallet_status}\n\nВыбери раздел:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def admin_stats_view(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context).admin_stats(now_moscow())
    body = (
        "📊 <b>Сводная статистика</b>\n\n"
        f"👥 Пользователи всего: <b>{s['users']}</b> · новые сегодня: {s['new_today']}\n"
        f"🟢 Активные: 24 ч — {s['active_24h']}; 7 дней — {s['active_7d']}; 30 дней — {s['active_30d']}\n"
        f"🚫 Заблокировали бота: {s['blocked']} · ограничены админом: {s['banned']} · отказались от рекламы: {s['opted_out']}\n\n"
        f"💰 Балансы пользователей: {money(s['balances'])}\n"
        f"🏅 Начислено всего: {money(s['earned'])} (рефералы {money(s['referral_kopeks'])}, бонусы {money(s['bonus_kopeks'])})\n"
        f"✅ Выводы, отмеченные выплаченными: {money(s['paid'])}\n"
        f"⏳ Ожидают выводы: {s['pending_withdrawals']} на {money(s['pending_withdrawal_kopeks'])}\n\n"
        f"📣 Заказы рекламы: {s['ads_total']} · оплачено: {s['ads_paid_count']} на {money(s['ads_revenue'])}\n"
        f"💳 Ждут оплаты: {s['ads_awaiting_payment']} · на проверке: {s['ads_pending_review']} · разослано: {s['ads_broadcasted']}\n"
        f"📨 Всего доставок в рассылках: {s['broadcast_sent']} · кампаний завершено: {s['campaigns']}"
    )
    await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:home")]]))


async def admin_users_view(message, context: ContextTypes.DEFAULT_TYPE, offset: int = 0, search: str = "") -> None:
    st = store(context)
    rows = st.users_page(offset, 10, search)
    total = st.user_count(search)
    if not rows:
        await message.reply_text("Пользователи не найдены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]))
        return
    kb_rows = []
    for u in rows:
        name = u["first_name"] or ("@" + u["username"] if u["username"] else str(u["telegram_id"]))
        flag = " ⛔" if u["banned"] else ""
        kb_rows.append([InlineKeyboardButton(f"{name[:24]} · {u['telegram_id']}{flag}", callback_data=f"admin:user:{u['telegram_id']}:{offset}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin:users:{max(0, offset-10)}"))
    nav.append(InlineKeyboardButton(f"{offset+1}–{min(offset+len(rows), total)} / {total}", callback_data="admin:noop"))
    if offset + 10 < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin:users:{offset+10}"))
    kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔎 Поиск", callback_data="admin:search"), InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")])
    await message.reply_text("👥 <b>База пользователей</b> · сортировка по дате регистрации", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))


async def admin_user_detail(message, context: ContextTypes.DEFAULT_TYPE, user_id: int, offset: int = 0) -> None:
    p = store(context).profile(user_id)
    if not p:
        await message.reply_text("Пользователь не найден.")
        return
    user = store(context).get_user(user_id)
    name = user.get("first_name") or "—"
    username = "@" + user["username"] if user.get("username") else "—"
    ref = f"<code>{p['referrer_id']}</code>" if p["referrer_id"] else "нет"
    status = "ограничен" if p["banned"] else "активен"
    opt = "отписан от рекламы" if p["ads_opt_out"] else "получает рекламные рассылки"
    text = (
        f"👤 <b>Пользователь {user_id}</b>\nИмя: {esc(name)} · {esc(username)}\n"
        f"Статус: {status}; {opt}; blocked={p['blocked']}\n"
        f"Регистрация: {esc(p['joined_at'])}\nПоследняя активность: {esc(p['last_active_at'])}\nПригласил: {ref}\n\n"
        f"Баланс: {money(p['balance_kopeks'])}\nНачислено: {money(p['total_earned_kopeks'])}\nВыплачено: {money(p['total_paid_kopeks'])}\n"
        f"Команда: {p['level_counts'][1]} / {p['level_counts'][2]} / {p['level_counts'][3]}\n"
        f"Реферальные начисления по уровням: {money(p['level_earnings'][1])} / {money(p['level_earnings'][2])} / {money(p['level_earnings'][3])}\n"
        f"Бонус последний: {esc(p['bonus_last_date'] or 'нет')}"
    )
    toggle = "✅ Снять ограничение" if p["banned"] else "⛔ Ограничить"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"admin:toggleban:{user_id}:{offset}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data=f"admin:users:{offset}"), InlineKeyboardButton("🏠 Панель", callback_data="admin:home")],
    ])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def admin_withdrawals_view(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = store(context).get_withdrawals(20)
    if not items:
        await message.reply_text("💸 Ожидающих заявок на вывод нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")]]))
        return
    lines = ["💸 <b>Ожидающие выводы</b>\nРеквизиты, отправленные пользователями, приходят администратору отдельными сообщениями через бота."]
    buttons = []
    for w in items:
        display = w["first_name"] or ("@" + w["username"] if w["username"] else str(w["user_id"]))
        lines.append(f"\n<b>#{w['id']}</b> · {money(w['amount_kopeks'])} · {METHOD_LABELS[w['method']]} · {esc(display)} · ID <code>{w['user_id']}</code>")
        buttons.append([
            InlineKeyboardButton(f"✅ Выплачено #{w['id']}", callback_data=f"wd:paid:{w['id']}"),
            InlineKeyboardButton(f"↩️ Отклонить #{w['id']}", callback_data=f"wd:rejected:{w['id']}"),
        ])
    buttons.append([InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")])
    await message.reply_text("".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_ads_view(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = store(context).list_ad_orders(20)
    if not orders:
        await message.reply_text("📣 Заказов рекламы пока нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")]]))
        return
    lines = ["📣 <b>Последние заказы рекламы</b>"]
    buttons = []
    for o in orders:
        who = o["first_name"] or ("@" + o["username"] if o["username"] else str(o["user_id"]))
        lines.append(f"\n<b>#{o['id']}</b> · {esc(o['status'])} · {money(o['price_kopeks'])} · {esc(who)} · ID <code>{o['user_id']}</code>\nМетка: <code>{esc(o['label'])}</code>")
        if o["status"] == "awaiting_payment":
            buttons.append([
                InlineKeyboardButton(f"🔎 Проверить оплату #{o['id']}", callback_data=f"ad:verify:{o['id']}"),
                InlineKeyboardButton(f"✅ Оплачено вручную #{o['id']}", callback_data=f"ad:manual:{o['id']}"),
            ])
            buttons.append([InlineKeyboardButton(f"🗑 Закрыть неоплаченный #{o['id']}", callback_data=f"ad:cancel:{o['id']}")])
        elif o["status"] == "pending_review":
            buttons.append([
                InlineKeyboardButton(f"✅ Разослать #{o['id']}", callback_data=f"ad:approve:{o['id']}"),
                InlineKeyboardButton(f"❌ Отклонить #{o['id']}", callback_data=f"ad:reject:{o['id']}"),
            ])
    buttons.append([InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")])
    await message.reply_text("".join(lines)[:4000], parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_top_view(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store(context).top_referrers(10)
    lines = ["🏆 <b>Топ прямых приглашений</b>"]
    for i, u in enumerate(rows, 1):
        name = u["first_name"] or ("@" + u["username"] if u["username"] else str(u["telegram_id"]))
        lines.append(f"\n{i}. {esc(name)} · ID <code>{u['telegram_id']}</code> · друзей {u['direct_count']} · начислено {money(u['total_earned_kopeks'])}")
    await message.reply_text("".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")]]))


async def admin_recent_view(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store(context).recent_transactions(20)
    lines = ["📜 <b>Последние операции</b>"]
    for r in rows:
        kind = TX_LABELS.get(r["kind"], r["kind"])
        name = r["first_name"] or ("@" + r["username"] if r["username"] else str(r["user_id"]))
        lines.append(f"\n<code>#{r['id']}</code> {esc(kind)} · {money(r['amount_kopeks'])} · {esc(name)} · {esc(r['created_at'][:16])}")
    await message.reply_text("".join(lines)[:4000], parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Панель", callback_data="admin:home")]]))


async def export_users(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store(context).users_for_export()
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(["telegram_id", "username", "first_name", "joined_at", "last_active_at", "referrer_id", "balance_rub", "earned_rub", "paid_rub", "level1_referrals", "level2_referrals", "level3_referrals", "blocked", "banned", "ads_opt_out"])
    def csv_safe(value):
        value = str(value or "")
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for r in rows:
        writer.writerow([
            r["telegram_id"], csv_safe(r["username"]), csv_safe(r["first_name"]), csv_safe(r["joined_at"]), csv_safe(r["last_active_at"]), r["referrer_id"],
            f"{r['balance_kopeks']/100:.2f}", f"{r['total_earned_kopeks']/100:.2f}", f"{r['total_paid_kopeks']/100:.2f}",
            r["level1_referrals"], r["level2_referrals"], r["level3_referrals"], r["blocked"], r["banned"], r["ads_opt_out"],
        ])
    data = io.BytesIO(text.getvalue().encode("utf-8-sig"))
    data.name = "priglashay_plus_users.csv"
    await message.reply_document(document=data, filename=data.name, caption=f"Экспортировано пользователей: {len(rows)}")


async def begin_broadcast(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = "broadcast_wait"
    await message.reply_text("📢 Пришли одно рекламное сообщение для рассылки: текст или одну картинку с подписью. Бот покажет предпросмотр, отправит только после твоего подтверждения. /cancel — отменить.")


async def preview_broadcast(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message.media_group_id or (not message.text and not (message.photo and message.caption)):
        await message.reply_text("Поддерживается текст или одна картинка с подписью. Альбомы/прочие файлы не поддерживаются; попробуй снова или /cancel.")
        return
    context.user_data["broadcast_source_chat_id"] = message.chat_id
    context.user_data["broadcast_source_message_id"] = message.message_id
    context.user_data["admin_state"] = "broadcast_confirm"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Подтвердить и разослать", callback_data="broadcast:confirm")],
        [InlineKeyboardButton("↩️ Отменить", callback_data="broadcast:cancel")],
    ])
    await context.bot.copy_message(
        chat_id=message.chat_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
        reply_markup=kb,
    )
    await message.reply_text("Предпросмотр выше. Рассылка пойдёт только подписанным на рекламу пользователям, которые не заблокировали бота.")


async def begin_admin_search(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = "search_user"
    await message.reply_text("Введи Telegram ID, username или часть имени. /cancel — отмена.")


async def admin_message_state(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("admin_state") == "support_reply":
        target_id = context.user_data.get("support_reply_user_id")
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("Напиши ответ обычным текстом или нажми /cancel.")
            return True
        if len(text) > 3500:
            await message.reply_text("Ответ слишком длинный. Сократи его до 3 500 символов.")
            return True
        try:
            await context.bot.send_message(int(target_id), "🆘 Ответ администратора:\n\n" + text)
        except (TelegramError, TypeError, ValueError):
            await message.reply_text("Не удалось отправить ответ. Проверь, что пользователь не заблокировал бота.")
        else:
            await message.reply_text("✅ Ответ отправлен пользователю.")
        context.user_data.pop("admin_state", None)
        context.user_data.pop("support_reply_user_id", None)
        return True
    if context.user_data.get("admin_state") == "broadcast_wait":
        await preview_broadcast(message, context)
        return True
    if context.user_data.get("admin_state") == "search_user":
        query = (message.text or "").strip()
        context.user_data.pop("admin_state", None)
        if not query:
            await message.reply_text("Введи ID или username текстом.")
            return True
        context.user_data["admin_user_search"] = query
        await admin_users_view(message, context, 0, query)
        return True
    return False


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or user.is_bot:
        return
    if user.id == ADMIN_ID and await admin_message_state(message, context):
        return

    text = (message.text or "").strip()
    route = {
        BTN_LINK: show_link,
        BTN_PROFILE: show_profile,
        BTN_BONUS: claim_bonus,
        BTN_WITHDRAW: show_withdrawal,
        BTN_AD: create_ad_order,
        BTN_HOW: show_how,
        BTN_REVIEWS: show_reviews,
        BTN_SUPPORT: show_support,
        BTN_HOME: show_home,
        BTN_ADMIN: admin_command,
    }
    if text == BTN_SUPPORT:
        await show_support(update, context)
        return
    if context.user_data.get("support_state") and text not in route:
        await forward_support_message(message, user, context)
        return

    record = store(context).get_user(user.id)
    if record and record["banned"] and user.id != ADMIN_ID:
        await message.reply_text("Доступ к боту ограничен. Если считаешь это ошибкой, отправь команду /support.")
        return

    if context.user_data.get("withdraw_state") == "amount":
        if text in route:
            await message.reply_text("Сначала введи сумму вывода или нажми /cancel, чтобы выйти из заявки.")
        elif message.text:
            await menu_text_handler(update, context)
        else:
            await message.reply_text("Введи сумму текстом или нажми /cancel.")
        return
    if text in route:
        context.user_data.pop("support_state", None)
        await route[text](update, context)
        return
    active = store(context).active_ad_order(user.id)
    if active and active["status"] == "awaiting_content":
        await process_ad_content(message, user, context, active)
        return
    if user.id == ADMIN_ID and context.user_data.get("admin_state") == "broadcast_confirm":
        await message.reply_text("Нажми кнопку в предпросмотре или /cancel.")
        return
    await message.reply_text("Выбери действие кнопкой в меню или нажми /help.", reply_markup=menu_keyboard(user.id))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("admin_state", None)
    context.user_data.pop("broadcast_source_chat_id", None)
    context.user_data.pop("broadcast_source_message_id", None)
    context.user_data.pop("withdraw_state", None)
    context.user_data.pop("withdraw_method", None)
    context.user_data.pop("support_state", None)
    context.user_data.pop("support_reply_user_id", None)
    if update.effective_message:
        await update.effective_message.reply_text("Действие отменено.", reply_markup=menu_keyboard(update.effective_user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if user.id != ADMIN_ID:
        await message.reply_text("Эта команда доступна только администратору.")
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await message.reply_text("Открой админ-панель в личном чате с ботом.")
        return
    await admin_home(message, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Команды: /start — открыть бота, /cancel — отмена ввода, /support — написать администратору через бота, /ads_off — отказаться от рекламных рассылок, /ads_on — снова включить.",
            reply_markup=menu_keyboard(update.effective_user.id),
        )


async def optout_command(update: Update, context: ContextTypes.DEFAULT_TYPE, opted_out: bool) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    store(context).set_ads_optout(user.id, opted_out)
    await message.reply_text("🔕 Рекламные рассылки отключены." if opted_out else "🔔 Рекламные рассылки включены.", reply_markup=menu_keyboard(user.id))


async def ads_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await optout_command(update, context, True)


async def ads_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await optout_command(update, context, False)


async def admin_callback(data: str, query, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = query.message
    if data == "admin:home":
        await admin_home(message, context)
    elif data == "admin:stats":
        await admin_stats_view(message, context)
    elif data.startswith("admin:users:"):
        try:
            offset = int(data.split(":")[2])
        except ValueError:
            offset = 0
        await admin_users_view(message, context, offset)
    elif data == "admin:search":
        await begin_admin_search(message, context)
    elif data.startswith("admin:user:"):
        parts = data.split(":")
        uid = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
        await admin_user_detail(message, context, uid, offset)
    elif data.startswith("admin:toggleban:"):
        parts = data.split(":")
        uid, offset = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        new_value = store(context).toggle_ban(uid)
        await admin_user_detail(message, context, uid, offset)
    elif data == "admin:withdrawals":
        await admin_withdrawals_view(message, context)
    elif data == "admin:ads":
        await admin_ads_view(message, context)
    elif data == "admin:top":
        await admin_top_view(message, context)
    elif data == "admin:recent":
        await admin_recent_view(message, context)
    elif data == "admin:export":
        await export_users(message, context)
    elif data == "admin:broadcast":
        await begin_broadcast(message, context)
    elif data == "admin:noop":
        return


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data

    if data == "ad:start":
        await query.answer()
        await create_ad_order(update, context)
        return
    if data == "ads:optout":
        await query.answer()
        store(context).set_ads_optout(query.from_user.id, True)
        await query.message.reply_text("🔕 Ты отписался от рекламных рассылок. Вернуть их можно командой /ads_on.")
        return

    if data.startswith("support:reply:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Только администратор.", show_alert=True)
            return
        try:
            target_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный ID пользователя.", show_alert=True)
            return
        await query.answer()
        context.user_data["admin_state"] = "support_reply"
        context.user_data["support_reply_user_id"] = target_id
        await query.message.reply_text(f"Напиши ответ пользователю {target_id}. /cancel — отмена.")
        return

    if data.startswith("admin:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Недостаточно прав.", show_alert=True)
            return
        await query.answer()
        try:
            await admin_callback(data, query, context)
        except (ValueError, IndexError) as exc:
            log.warning("Bad admin callback %s: %s", data, exc)
            await query.message.reply_text("Не удалось открыть раздел.")
        return

    if data.startswith("withdraw:method:"):
        method = data.rsplit(":", 1)[1]
        if method not in METHOD_LABELS:
            await query.answer("Неизвестный способ вывода.", show_alert=True)
            return
        await query.answer()
        context.user_data["withdraw_state"] = "amount"
        context.user_data["withdraw_method"] = method
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        profile = store(context).profile(query.from_user.id)
        await query.message.reply_text(
            f"Выбран способ: {METHOD_LABELS[method]}. Введи сумму в рублях; минимум {money(MIN_WITHDRAWAL)}, доступно {money(profile['balance_kopeks'] if profile else 0)}. /cancel — отмена.",
            reply_markup=menu_keyboard(query.from_user.id),
        )
        return

    if data.startswith("wd:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Только администратор.", show_alert=True)
            return
        _, action, raw_id = data.split(":", 2)
        result = store(context).settle_withdrawal(int(raw_id), action, ADMIN_ID, iso_now())
        if result["status"] == "already":
            await query.answer("Заявка уже обработана.", show_alert=True)
            return
        if result["status"] != action:
            await query.answer("Заявка не найдена.", show_alert=True)
            return
        await query.answer("Статус обновлён.")
        if action == "paid":
            user_text = f"✅ Заявка #{raw_id} отмечена администратором как выплаченная: {money(result['amount'])}. Если перевод не пришёл, отправь /support."
        else:
            user_text = f"↩️ Заявка #{raw_id} отклонена; {money(result['amount'])} возвращены на баланс. Если есть вопросы, отправь /support."
        try:
            await context.bot.send_message(result["user_id"], user_text)
        except TelegramError:
            pass
        await query.message.reply_text("Заявка обработана. Фактический перевод по кнопке не выполняется; выплати вручную перед отметкой «выплачено».")
        return

    if data.startswith("ad:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Только администратор.", show_alert=True)
            return
        await query.answer()
        _, action, raw_id = data.split(":", 2)
        order_id = int(raw_id)
        if action == "verify":
            found = await verify_one_order(order_id, context)
            await query.message.reply_text("✅ Оплата подтверждена; пользователю отправлена инструкция." if found else "Оплата не найдена. Проверь метку в YooMoney; при необходимости отметь вручную.")
            return
        if action == "cancel":
            cancelled = store(context).cancel_unpaid_ad_order(order_id, ADMIN_ID, iso_now())
            if not cancelled:
                await query.message.reply_text("Заказ уже оплачен или обработан — закрыть его нельзя.")
                return
            await query.message.reply_text(f"Неоплаченный заказ #{order_id} закрыт. Перед закрытием убедись, что перевод не поступил.")
            try:
                await context.bot.send_message(cancelled["user_id"], f"Заказ рекламы #{order_id} закрыт администратором. Если ты уже оплатил, отправь /support и укажи метку {cancelled['label']}.")
            except TelegramError:
                pass
            return
        if action == "manual":
            paid = store(context).mark_ad_paid(order_id, None, ADMIN_ID, iso_now())
            if not paid:
                await query.message.reply_text("Заказ не ожидает оплаты или уже обработан.")
                return
            await query.message.reply_text(f"Оплату заказа #{order_id} отмечена вручную. Перед подтверждением сверь поступление в кошельке.")
            await notify_ad_content_waiting(paid, paid["user_id"], context)
            return
        if action == "approve":
            if not store(context).broadcast_targets(ADMIN_ID):
                await query.message.reply_text("Нет адресатов, которые разрешили рекламную рассылку. Заказ оставлен на проверке.")
                return
            order = store(context).claim_ad_for_broadcast(order_id, ADMIN_ID, iso_now())
            if not order:
                await query.message.reply_text("Заказ не найден или уже обработан.")
                return
            if not (order.get("content_text") or order.get("content_file_id")):
                await query.message.reply_text("У заказа нет сохранённого объявления; остановил публикацию.")
                return
            await query.message.reply_text(f"✅ Заказ #{order_id} одобрен. Запускаю рассылку подписанным пользователям…")
            context.application.create_task(
                run_broadcast(context.application, ADMIN_ID, order["content_chat_id"], order["content_message_id"], order_id),
                name=f"ad-broadcast-{order_id}",
            )
            return
        if action == "reject":
            order = store(context).reject_ad_order(order_id, ADMIN_ID, "Отклонено администратором", iso_now())
            if not order:
                await query.message.reply_text("Заказ не найден или уже обработан.")
                return
            await query.message.reply_text(f"Заказ #{order_id} отклонён. Деньги бот не возвращает автоматически: свяжись с заказчиком и оформи возврат вручную, если он положен.")
            try:
                await context.bot.send_message(order["user_id"], f"❌ Объявление по заказу #{order_id} не прошло проверку. Возврат не автоматический. По вопросу отправь /support.")
            except TelegramError:
                pass
            return

    if data.startswith("broadcast:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Только администратор.", show_alert=True)
            return
        await query.answer()
        action = data.split(":", 1)[1]
        if action == "cancel":
            context.user_data.pop("admin_state", None)
            context.user_data.pop("broadcast_source_chat_id", None)
            context.user_data.pop("broadcast_source_message_id", None)
            await query.message.reply_text("Рассылка отменена.")
            return
        if action == "confirm":
            source_chat = context.user_data.get("broadcast_source_chat_id")
            source_message = context.user_data.get("broadcast_source_message_id")
            if not source_chat or not source_message:
                await query.message.reply_text("Предпросмотр устарел. Создай рассылку заново через /admin.")
                return
            context.user_data.pop("admin_state", None)
            context.user_data.pop("broadcast_source_chat_id", None)
            context.user_data.pop("broadcast_source_message_id", None)
            targets = store(context).broadcast_targets(ADMIN_ID)
            if not targets:
                await query.message.reply_text("Нет доступных подписчиков для рекламной рассылки.")
                return
            await query.message.reply_text(f"🚀 Запустил рассылку на {len(targets)} пользователей. Результат пришлю сюда.")
            context.application.create_task(
                run_broadcast(context.application, ADMIN_ID, int(source_chat), int(source_message), None),
                name="admin-broadcast",
            )
            return

    if data == "ads:optin":
        await query.answer()
        store(context).set_ads_optout(query.from_user.id, False)
        await query.message.reply_text("🔔 Рекламные рассылки снова включены.")


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the amount step in the card/SBP withdrawal wizard."""
    user, message = update.effective_user, update.effective_message
    if not user or not message or not message.text:
        return
    if context.user_data.get("withdraw_state") != "amount":
        return
    raw = message.text.strip()
    if raw in {BTN_HOME, BTN_LINK, BTN_PROFILE, BTN_BONUS, BTN_WITHDRAW, BTN_AD, BTN_HOW, BTN_REVIEWS, BTN_SUPPORT, BTN_ADMIN}:
        await message.reply_text("Сначала введи сумму или нажми /cancel, чтобы выйти из заявки на вывод.")
        return
    try:
        amount = parse_money(raw)
    except ValueError as exc:
        await message.reply_text(str(exc) + " /cancel — отмена.")
        return
    method = context.user_data.get("withdraw_method")
    result = store(context).create_withdrawal(user.id, amount, method, iso_now())
    if result["status"] == "below_minimum":
        await message.reply_text(f"Минимум — {money(MIN_WITHDRAWAL)}. Введи другую сумму или /cancel.")
        return
    if result["status"] == "insufficient":
        await message.reply_text(f"Доступно {money(result['balance'])}. Введи сумму не выше баланса или /cancel.")
        return
    if result["status"] != "created":
        await message.reply_text("Не удалось создать заявку. Нажми /cancel и повтори.")
        return
    context.user_data.pop("withdraw_state", None)
    context.user_data.pop("withdraw_method", None)
    wd_id = result["id"]
    context.user_data["support_state"] = "waiting"
    context.user_data["support_withdrawal_id"] = wd_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отметить выплаченным", callback_data=f"wd:paid:{wd_id}")],
        [InlineKeyboardButton("↩️ Отклонить и вернуть", callback_data=f"wd:rejected:{wd_id}")],
    ])
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💸 <b>Заявка на вывод #{wd_id}</b>\nПользователь: <a href=\"tg://user?id={user.id}\">{esc(user.full_name)}</a> · ID <code>{user.id}</code>\nСумма: {money(amount)} · способ: {METHOD_LABELS.get(method, method)}\nРеквизиты пользователь отправит отдельным сообщением через бота.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except TelegramError:
        log.exception("Cannot send withdrawal to admin")
    await message.reply_text(
        f"✅ Заявка #{wd_id} создана. Сумма зарезервирована.\n\n"
        + ("Отправь сюда номер карты и имя владельца" if method == "card" else "Отправь сюда номер телефона и название банка для СБП")
        + ". Сообщение будет передано администратору через бота. Не отправляй CVV/CVC, срок действия карты, PIN, пароль или коды из SMS.",
    )


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == ADMIN_ID and update.effective_message:
        await admin_stats_view(update.effective_message, context)


async def post_init(app: Application) -> None:
    app.bot_data["store"] = Store(BASE_DIR / DB_PATH)
    if app.job_queue:
        app.job_queue.run_repeating(verify_pending_payments, interval=PAYMENT_POLL_SECONDS, first=10, name="yoomoney-ad-verifier")
        log.info("YooMoney verifier scheduled every %s sec", PAYMENT_POLL_SECONDS)
    else:
        log.warning("PTB JobQueue unavailable; install python-telegram-bot[job-queue].")
    try:
        me = await app.bot.get_me()
        app.bot_data["bot_username"] = me.username
        log.info("Started @%s with admin %s", me.username, ADMIN_ID)
    except TelegramError:
        log.exception("Could not get bot identity")
    try:
        await app.bot.set_my_name(BOT_NAME)
        await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        await app.bot.set_my_description(BOT_DESCRIPTION)
    except TelegramError:
        log.exception("Could not update bot name/description; set them manually in @BotFather")


def main() -> None:
    if not os.getenv("BOT_TOKEN", "").strip():
        raise SystemExit("Заполни BOT_TOKEN в .env (токен не присылай в чат).")
    if ADMIN_ID <= 0:
        raise SystemExit("Заполни числовой ADMIN_ID в .env.")
    if YOOMONEY_PAYMENT_TYPE not in ("AC", "PC"):
        raise SystemExit("YOOMONEY_PAYMENT_TYPE должен быть AC или PC.")
    if not YOOMONEY_WALLET:
        log.warning("YOOMONEY_WALLET не задан: заказ рекламы не будет доступен.")
    if not YOOMONEY_TOKEN:
        log.warning("YOOMONEY_TOKEN не задан: автоматическая проверка платежей отключена.")

    app = Application.builder().token(os.getenv("BOT_TOKEN").strip()).post_init(post_init).build()
    # Ensure the store exists before handlers or activity tracking touch it.
    app.bot_data["store"] = Store(BASE_DIR / DB_PATH)
    app.bot_data["admin_id"] = ADMIN_ID

    app.add_handler(TypeHandler(Update, activity_tracker, block=False), group=-1)
    app.add_handler(CommandHandler("start", start), group=0)
    app.add_handler(CommandHandler("admin", admin_command), group=0)
    app.add_handler(CommandHandler("stats", admin_stats_command), group=0)
    app.add_handler(CommandHandler("cancel", cancel_command), group=0)
    app.add_handler(CommandHandler("help", help_command), group=0)
    app.add_handler(CommandHandler("support", show_support), group=0)
    app.add_handler(CommandHandler("ads_off", ads_off_command), group=0)
    app.add_handler(CommandHandler("ads_on", ads_on_command), group=0)
    app.add_handler(CallbackQueryHandler(callback_router), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router), group=1)
    log.info("Starting %s", BOT_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
