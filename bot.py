"""
Bellashuv Uz - 1v1 Quiz Duel Telegram Bot
==========================================
Single-file build (auto-merged from the original modular project) containing
the full aiogram 3.x application: config, database models/engine/queries,
FSM states, filters, keyboards, middlewares, the live-duel engine, the
weekly-leaderboard scheduler, every user/admin handler, and the entrypoint.

Run with:
    pip install -r requirements.txt
    cp .env.example .env   # fill in BOT_TOKEN, ADMIN_IDS, etc.
    python bellashuv_uz_bot_full.py

See the original README.md for full feature docs, the channel-based
question-ingestion format, and admin-access-control notes.
"""

from __future__ import annotations

# --- standard library ---
import asyncio
import enum
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

# --- third-party: SQLAlchemy ---
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- third-party: APScheduler ---
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- third-party: aiogram ---
from aiogram import Bot, BaseMiddleware, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("bellashuv_uz")



# ============================================================================
# CONFIG
# (from: bot/config.py — .env ishlatilmaydi, barcha qiymatlar shu yerda
#  to'g'ridan-to'g'ri yozilgan. Pastdagi qiymatlarni o'zingiznikiga almashtiring)
# ============================================================================


@dataclass(frozen=True)
class Config:
    # 👉 @BotFather dan olingan haqiqiy bot tokenini shu yerga yozing
    bot_token: str = "8881184064:AAHlzIweaSf_RC02I81DXj3WsQ2ZfAAtTQQ"

    # 👉 botingiz username'i, "@" belgisisiz
    bot_username: str = "bellashuvtest_bot"

    # 👉 admin bo'lgan Telegram user ID'lari (bir nechta bo'lsa, vergul bilan)
    admin_ids: list[int] = field(default_factory=lambda: [8355669630, 222222222])

    # 👉 pul yechish so'rovlari yuboriladigan guruh/chat ID (masalan: -1001234567890)
    admin_chat_id: int = -1003976084050

    # 👉 admin savollarni post qiladigan xususiy kanal ID (masalan: -1004464642367)
    test_source_channel_id: int = -1009876543210

    # SQLite standart bo'yicha ishlaydi (hech narsa o'rnatish shart emas).
    # PostgreSQL uchun: "postgresql+asyncpg://user:pass@localhost:5432/bellashuv"
    database_url: str = "sqlite+aiosqlite:///bellashuv.db"

    mandatory_channel: str = ""  # legacy single-channel setting, endi MandatoryChannel jadvali ishlatiladi

    questions_per_duel: int = 5
    duel_question_timeout: int = 15
    default_lives: int = 7  # kuniga 7 ta jon (00:00 Asia/Tashkent da to'ldiriladi)

    prize_1st: int = 15000
    prize_2nd: int = 10000
    prize_3rd: int = 5000

    mini_status_price: int = 2000
    pro_status_price: int = 5000

    admin_contact: str = "@bellashuvuz_admin"

    # --- Ustoz kodi / tarif tizimi ---
    teacher_code_length: int = 8
    teacher_code_expiry_days: int = 30
    teacher_trial_days: int = 15
    teacher_monthly_duel_limit: int = 8
    teacher_monthly_test_limit: int = 80
    teacher_unlimited_1m_price: int = 3000
    teacher_gold_3m_price: int = 8000

    # --- Maxsus (2 o'quvchi) duel default parametrlari ---
    custom_duel_question_choices: tuple[int, ...] = (5, 10, 15, 20)
    custom_duel_time_choices: tuple[int, ...] = (10, 15, 20, 30)
    custom_duel_default_questions: int = 10
    custom_duel_default_time: int = 15
    custom_duel_join_reminder_minutes: int = 15

    # --- kunlik push-eslatma vaqti (Asia/Tashkent) ---
    daily_reminder_hour: int = 10
    daily_reminder_minute: int = 0

    timezone: str = "Asia/Tashkent"


config = Config()


# ============================================================================
# DATABASE MODELS
# (from: bot/database/models.py)
# ============================================================================
class Base(DeclarativeBase):
    pass


class UserStatus(str, enum.Enum):
    NORMAL = "normal"
    MINI = "mini"
    PRO = "pro"


class WithdrawalStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"


class TransactionType(str, enum.Enum):
    WITHDRAWAL = "withdrawal"
    PRIZE = "prize"
    SUBSCRIPTION = "subscription"
    REFERRAL_BONUS = "referral_bonus"
    OTHER = "other"


class DuelStatus(str, enum.Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class ChannelKind(str, enum.Enum):
    OPEN = "open"      # @username orqali
    CLOSED = "closed"  # -100... ID orqali (maxfiy kanal)


class TeacherPlan(str, enum.Enum):
    NONE = "none"          # ustoz emas
    TRIAL = "trial"        # 15 kunlik bepul cheksiz
    LIMITED = "limited"    # oylik limitlar (8 duel / 80 test)
    UNLIMITED_1M = "unlimited_1m"
    GOLD_3M = "gold_3m"


class TeacherCodeStatus(str, enum.Enum):
    UNUSED = "unused"
    USED = "used"
    EXPIRED = "expired"


class TeacherPurchaseStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CustomDuelStatus(str, enum.Enum):
    WAITING_PLAYERS = "waiting_players"
    ACTIVE = "active"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class SpecialTestStatus(str, enum.Enum):
    PENDING = "pending"
    FINISHED = "finished"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    balance: Mapped[int] = mapped_column(Integer, default=0)  # UZS
    score: Mapped[int] = mapped_column(Integer, default=0)  # ranking points, reset weekly
    lifetime_score: Mapped[int] = mapped_column(Integer, default=0)  # never reset
    lives: Mapped[int] = mapped_column(Integer, default=5)

    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    invited_count: Mapped[int] = mapped_column(Integer, default=0)

    total_duels: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.NORMAL)
    status_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # False if bot is blocked by user

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # --- Ustoz (teacher) tizimi ---
    is_teacher: Mapped[bool] = mapped_column(Boolean, default=False)
    teacher_plan: Mapped[TeacherPlan] = mapped_column(Enum(TeacherPlan), default=TeacherPlan.NONE)
    teacher_plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # LIMITED bosqichida oylik hisoblagichlar (period_reset_at ga yetganda 0 ga tushadi)
    teacher_period_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    teacher_duels_used: Mapped[int] = mapped_column(Integer, default=0)
    teacher_tests_used: Mapped[int] = mapped_column(Integer, default=0)
    # Cheksiz sovg'a qilingan bo'lsa (admin tomonidan, muddatsiz)
    teacher_gifted_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    # Umumiy statistikalar (hech qachon reset qilinmaydi)
    teacher_total_tests: Mapped[int] = mapped_column(Integer, default=0)
    teacher_total_duels: Mapped[int] = mapped_column(Integer, default=0)

    withdrawals: Mapped[list["Withdrawal"]] = relationship(back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")


class Test(Base):
    __tablename__ = "tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text)
    option_a: Mapped[str] = mapped_column(String(500))
    option_b: Mapped[str] = mapped_column(String(500))
    option_c: Mapped[str] = mapped_column(String(500))
    option_d: Mapped[str] = mapped_column(String(500))
    correct_option: Mapped[str] = mapped_column(String(1))  # "A" | "B" | "C" | "D"

    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)  # Pro-only question pool
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Agar shu test biror ustozning shaxsiy bankiga tegishli bo'lsa (umumiy
    # tasodifiy duel pooliga aralashmaydi, faqat o'sha ustoz duellarida ishlatiladi).
    teacher_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    amount: Mapped[int] = mapped_column(Integer)
    card_number: Mapped[str] = mapped_column(String(32))
    status: Mapped[WithdrawalStatus] = mapped_column(Enum(WithdrawalStatus), default=WithdrawalStatus.PENDING)
    admin_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="withdrawals")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    amount: Mapped[int] = mapped_column(Integer)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="transactions")


class Duel(Base):
    """Persisted record of a duel (used for stats + history, not live matchmaking)."""

    __tablename__ = "duels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player1_id: Mapped[int] = mapped_column(BigInteger)
    player2_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # None while waiting / bot practice
    is_bot_practice: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[DuelStatus] = mapped_column(Enum(DuelStatus), default=DuelStatus.WAITING)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    player1_score: Mapped[int] = mapped_column(Integer, default=0)
    player2_score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    """Generic key/value store, e.g. mandatory_channel = '@bellashuvuz'."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class MandatoryChannel(Base):
    """Bir yoki bir nechta majburiy obuna kanali (ochiq yoki yopiq/maxfiy)."""

    __tablename__ = "mandatory_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[ChannelKind] = mapped_column(Enum(ChannelKind))
    # OPEN uchun: "@username". CLOSED uchun: "-1001234567890" (chat_id string sifatida).
    value: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invite_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def chat_ref(self) -> str:
        """get_chat_member ga beriladigan qiymat: OPEN uchun @username, CLOSED uchun int ID."""
        return self.value


class TeacherCode(Base):
    """Admin tomonidan yaratiladigan bir martalik ustoz kodi."""

    __tablename__ = "teacher_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[TeacherCodeStatus] = mapped_column(Enum(TeacherCodeStatus), default=TeacherCodeStatus.UNUSED)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TeacherPurchase(Base):
    """Ustoz tarif so'rovi (pul yechish so'rovlariga o'xshash admin-tasdiqlash oqimi)."""

    __tablename__ = "teacher_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    plan: Mapped[TeacherPlan] = mapped_column(Enum(TeacherPlan))
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[TeacherPurchaseStatus] = mapped_column(Enum(TeacherPurchaseStatus), default=TeacherPurchaseStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CustomDuel(Base):
    """Ustoz tomonidan 2 ta aniq o'quvchi uchun yaratilgan yopiq 1v1 duel."""

    __tablename__ = "custom_duels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(BigInteger)
    token: Mapped[str] = mapped_column(String(32), unique=True)
    watch_token: Mapped[str] = mapped_column(String(32), unique=True)

    player1_username: Mapped[str] = mapped_column(String(255))
    player2_username: Mapped[str] = mapped_column(String(255))
    player1_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    player2_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    num_questions: Mapped[int] = mapped_column(Integer, default=10)
    time_per_question: Mapped[int] = mapped_column(Integer, default=15)

    status: Mapped[CustomDuelStatus] = mapped_column(Enum(CustomDuelStatus), default=CustomDuelStatus.WAITING_PLAYERS)
    player1_score: Mapped[int] = mapped_column(Integer, default=0)
    player2_score: Mapped[int] = mapped_column(Integer, default=0)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpecialTest(Base):
    """Ikkita aniq foydalanuvchiga yuboriladigan yopiq maxsus test savoli."""

    __tablename__ = "special_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    question_number: Mapped[str] = mapped_column(String(16), default="1")
    question: Mapped[str] = mapped_column(Text)
    option_1: Mapped[str] = mapped_column(String(500))
    option_2: Mapped[str] = mapped_column(String(500))
    option_3: Mapped[str] = mapped_column(String(500))
    option_4: Mapped[str] = mapped_column(String(500))
    correct_option: Mapped[int] = mapped_column(Integer)  # 1..4

    user1_id: Mapped[int] = mapped_column(BigInteger)
    user2_id: Mapped[int] = mapped_column(BigInteger)

    user1_choice: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user1_answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user2_choice: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user2_answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[SpecialTestStatus] = mapped_column(Enum(SpecialTestStatus), default=SpecialTestStatus.PENDING)
    finished_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # /qayta bilan birinchi yakunlagan
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_seconds: Mapped[float | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# DATABASE ENGINE
# (from: bot/database/engine.py)
# ============================================================================
engine = create_async_engine(config.database_url, echo=False, future=True)

async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create all tables if they don't already exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()


# ============================================================================
# DATABASE REQUESTS
# (from: bot/database/requests.py)
# ============================================================================
# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


async def get_user(user_id: int) -> User | None:
    async with async_session() as session:
        return await session.get(User, user_id)


async def get_or_create_user(
    user_id: int,
    full_name: str,
    username: str | None,
    referred_by: int | None = None,
) -> tuple[User, bool]:
    """Returns (user, created)."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user:
            # keep name/username fresh
            user.full_name = full_name
            user.username = username
            user.is_active = True
            await session.commit()
            return user, False

        user = User(
            user_id=user_id,
            full_name=full_name,
            username=username,
            lives=config.default_lives,
            referred_by=referred_by,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user, True


async def update_user_fields(user_id: int, **fields) -> None:
    async with async_session() as session:
        await session.execute(update(User).where(User.user_id == user_id).values(**fields))
        await session.commit()


async def adjust_lives(user_id: int, delta: int, floor_zero: bool = True) -> int:
    """Add/subtract lives, returns new value."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return 0
        new_val = user.lives + delta
        if floor_zero:
            new_val = max(0, new_val)
        user.lives = new_val
        await session.commit()
        return new_val


async def adjust_balance(user_id: int, delta: int) -> int:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return 0
        user.balance += delta
        await session.commit()
        return user.balance


async def register_duel_result(user_id: int, won: bool, points: int) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.total_duels += 1
        if won:
            user.wins += 1
        else:
            user.losses += 1
        user.score += points
        user.lifetime_score += points
        await session.commit()


async def apply_referral_bonus(referrer_id: int, new_user_id: int, bonus_lives: int = 1) -> None:
    async with async_session() as session:
        referrer = await session.get(User, referrer_id)
        new_user = await session.get(User, new_user_id)
        if referrer:
            referrer.lives += bonus_lives
            referrer.invited_count += 1
        if new_user:
            new_user.lives += bonus_lives
        session.add(Transaction(user_id=referrer_id, amount=0, type=TransactionType.REFERRAL_BONUS,
                                 note=f"+{bonus_lives} life for inviting {new_user_id}"))
        await session.commit()


async def set_status(user_id: int, status: UserStatus, days: int = 30) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.status = status
        user.status_expires_at = datetime.utcnow() + timedelta(days=days)
        if status == UserStatus.PRO:
            user.lives = 9999  # effectively infinite
        session.add(Transaction(
            user_id=user_id,
            amount=config.pro_status_price if status == UserStatus.PRO else config.mini_status_price,
            type=TransactionType.SUBSCRIPTION,
            note=f"Granted {status.value} status",
        ))
        await session.commit()


async def ban_user(user_id: int, banned: bool = True) -> None:
    await update_user_fields(user_id, is_banned=banned)


async def mark_inactive(user_id: int) -> None:
    await update_user_fields(user_id, is_active=False)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


async def get_top_players(limit: int = 5) -> list[User]:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.is_banned.is_(False)).order_by(User.score.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_user_rank(user_id: int) -> int:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return 0
        result = await session.execute(
            select(func.count()).select_from(User).where(User.score > user.score, User.is_banned.is_(False))
        )
        higher_count = result.scalar_one()
        return higher_count + 1


async def reset_weekly_leaderboard() -> None:
    """Called by APScheduler every Sunday 21:00. Archives nothing, just zeroes weekly score."""
    async with async_session() as session:
        await session.execute(update(User).values(score=0))
        await session.commit()


# --------------------------------------------------------------------------- #
# Tests / questions
# --------------------------------------------------------------------------- #


async def add_tests_bulk(tests: list[dict], added_by: int) -> int:
    async with async_session() as session:
        objs = [
            Test(
                question=t["question"],
                option_a=t["option_a"],
                option_b=t["option_b"],
                option_c=t["option_c"],
                option_d=t["option_d"],
                correct_option=t["correct_option"],
                is_premium=t.get("is_premium", False),
                added_by=added_by,
            )
            for t in tests
        ]
        session.add_all(objs)
        await session.commit()
        return len(objs)


async def get_random_questions(count: int, include_premium: bool = False) -> list[Test]:
    async with async_session() as session:
        query = select(Test)
        if not include_premium:
            query = query.where(Test.is_premium.is_(False))
        query = query.order_by(func.random()).limit(count)
        result = await session.execute(query)
        return list(result.scalars().all())


async def count_tests() -> int:
    async with async_session() as session:
        result = await session.execute(select(func.count()).select_from(Test))
        return result.scalar_one()


# --------------------------------------------------------------------------- #
# Withdrawals & transactions
# --------------------------------------------------------------------------- #


async def create_withdrawal(user_id: int, amount: int, card_number: str) -> Withdrawal:
    async with async_session() as session:
        w = Withdrawal(user_id=user_id, amount=amount, card_number=card_number)
        session.add(w)
        session.add(Transaction(user_id=user_id, amount=-amount, type=TransactionType.WITHDRAWAL,
                                 note="Withdrawal requested"))
        await session.commit()
        await session.refresh(w)
        return w


async def get_pending_withdrawals() -> list[Withdrawal]:
    async with async_session() as session:
        result = await session.execute(
            select(Withdrawal).where(Withdrawal.status == WithdrawalStatus.PENDING).order_by(Withdrawal.created_at)
        )
        return list(result.scalars().all())


async def resolve_withdrawal(withdrawal_id: int, approved: bool, admin_note: str | None = None) -> Withdrawal | None:
    async with async_session() as session:
        w = await session.get(Withdrawal, withdrawal_id)
        if not w:
            return None
        w.status = WithdrawalStatus.COMPLETED if approved else WithdrawalStatus.REJECTED
        w.admin_note = admin_note
        w.resolved_at = datetime.utcnow()
        if not approved:
            # refund the amount that was optimistically deducted
            user = await session.get(User, w.user_id)
            if user:
                user.balance += w.amount
        await session.commit()
        await session.refresh(w)
        return w


async def get_transaction_history(user_id: int, limit: int = 10) -> list[Transaction]:
    async with async_session() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.date.desc()).limit(limit)
        )
        return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# Duels (persisted history)
# --------------------------------------------------------------------------- #


async def create_duel_record(player1_id: int, player2_id: int | None, is_bot_practice: bool = False) -> Duel:
    async with async_session() as session:
        duel = Duel(player1_id=player1_id, player2_id=player2_id, is_bot_practice=is_bot_practice,
                     status=DuelStatus.ACTIVE)
        session.add(duel)
        await session.commit()
        await session.refresh(duel)
        return duel


async def finish_duel_record(duel_id: int, p1_score: int, p2_score: int, winner_id: int | None) -> None:
    async with async_session() as session:
        duel = await session.get(Duel, duel_id)
        if not duel:
            return
        duel.player1_score = p1_score
        duel.player2_score = p2_score
        duel.winner_id = winner_id
        duel.status = DuelStatus.FINISHED
        duel.finished_at = datetime.utcnow()
        await session.commit()


async def count_active_duels() -> int:
    async with async_session() as session:
        result = await session.execute(select(func.count()).select_from(Duel).where(Duel.status == DuelStatus.ACTIVE))
        return result.scalar_one()


# --------------------------------------------------------------------------- #
# Settings (mandatory channel, etc.)
# --------------------------------------------------------------------------- #


async def get_setting(key: str, default: str | None = None) -> str | None:
    async with async_session() as session:
        setting = await session.get(Setting, key)
        return setting.value if setting else default


async def set_setting(key: str, value: str) -> None:
    async with async_session() as session:
        setting = await session.get(Setting, key)
        if setting:
            setting.value = value
        else:
            session.add(Setting(key=key, value=value))
        await session.commit()


# --------------------------------------------------------------------------- #
# Admin statistics
# --------------------------------------------------------------------------- #


async def get_stats() -> dict:
    async with async_session() as session:
        total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        new_today = (await session.execute(
            select(func.count()).select_from(User).where(User.created_at >= today_start)
        )).scalar_one()
        active_duels = (await session.execute(
            select(func.count()).select_from(Duel).where(Duel.status == DuelStatus.ACTIVE)
        )).scalar_one()
        total_tests = (await session.execute(select(func.count()).select_from(Test))).scalar_one()
        total_paid = (await session.execute(
            select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                Withdrawal.status == WithdrawalStatus.COMPLETED
            )
        )).scalar_one()
        return {
            "total_users": total_users,
            "new_today": new_today,
            "active_duels": active_duels,
            "total_tests": total_tests,
            "total_paid": total_paid,
        }


async def get_all_active_user_ids() -> list[int]:
    async with async_session() as session:
        result = await session.execute(select(User.user_id).where(User.is_active.is_(True), User.is_banned.is_(False)))
        return [row[0] for row in result.all()]


async def find_user_by_id_or_username(identifier: str) -> User | None:
    async with async_session() as session:
        identifier = identifier.strip().lstrip("@")
        if identifier.isdigit():
            return await session.get(User, int(identifier))
        result = await session.execute(select(User).where(User.username == identifier))
        return result.scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Daily lives reset / countdown
# --------------------------------------------------------------------------- #


async def reset_daily_lives(default_value: int) -> int:
    """Har kuni 00:00 (Asia/Tashkent) da chaqiriladi. PRO va cheksiz ustozlar
    bundan mustasno (ular allaqachon 9999 ga o'rnatilgan / limitsiz)."""
    async with async_session() as session:
        result = await session.execute(
            update(User)
            .where(User.status != UserStatus.PRO)
            .values(lives=default_value)
        )
        await session.commit()
        return result.rowcount or 0


def seconds_until_next_midnight(tz_name: str) -> int:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.utcnow()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((tomorrow - now).total_seconds()))


def format_hm(total_seconds: int) -> str:
    hours, rem = divmod(max(0, total_seconds), 3600)
    minutes = rem // 60
    return f"{hours} soat {minutes} daqiqa"


# --------------------------------------------------------------------------- #
# Mandatory channels (ko'p kanalli)
# --------------------------------------------------------------------------- #


async def add_mandatory_channel(kind: ChannelKind, value: str, title: str | None, invite_link: str | None,
                                 added_by: int) -> MandatoryChannel:
    async with async_session() as session:
        ch = MandatoryChannel(kind=kind, value=value, title=title, invite_link=invite_link, added_by=added_by)
        session.add(ch)
        await session.commit()
        await session.refresh(ch)
        return ch


async def remove_mandatory_channel(channel_id: int) -> bool:
    async with async_session() as session:
        ch = await session.get(MandatoryChannel, channel_id)
        if not ch:
            return False
        await session.delete(ch)
        await session.commit()
        return True


async def get_mandatory_channels() -> list[MandatoryChannel]:
    async with async_session() as session:
        result = await session.execute(select(MandatoryChannel).order_by(MandatoryChannel.id))
        return list(result.scalars().all())


async def get_mandatory_channel(channel_id: int) -> MandatoryChannel | None:
    async with async_session() as session:
        return await session.get(MandatoryChannel, channel_id)


# --------------------------------------------------------------------------- #
# Teacher codes
# --------------------------------------------------------------------------- #


def _generate_teacher_code(length: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(length))


async def create_teacher_code(created_by: int, length: int) -> TeacherCode:
    async with async_session() as session:
        for _ in range(20):
            code = _generate_teacher_code(length)
            existing = await session.get(TeacherCode, code)
            if not existing:
                break
        else:
            raise RuntimeError("Kod generatsiya qilishda xatolik (juda ko'p urinish).")
        tc = TeacherCode(code=code, created_by=created_by)
        session.add(tc)
        await session.commit()
        await session.refresh(tc)
        return tc


async def get_teacher_code(code: str) -> TeacherCode | None:
    async with async_session() as session:
        return await session.get(TeacherCode, code.strip().upper())


async def redeem_teacher_code(code: str, user_id: int, expiry_days: int, trial_days: int) -> tuple[bool, str]:
    """Kodni tekshiradi, ishlatadi va foydalanuvchini Ustoz maqomiga o'tkazadi."""
    async with async_session() as session:
        tc = await session.get(TeacherCode, code.strip().upper())
        if not tc:
            return False, "❌ Bunday kod topilmadi."
        if tc.status == TeacherCodeStatus.USED:
            return False, "❌ Bu kod allaqachon ishlatilgan."
        if tc.status == TeacherCodeStatus.EXPIRED or (datetime.utcnow() - tc.created_at).days > expiry_days:
            tc.status = TeacherCodeStatus.EXPIRED
            await session.commit()
            return False, "❌ Bu kodning muddati o'tgan (30 kun ichida ishlatilmagan)."

        user = await session.get(User, user_id)
        if not user:
            return False, "❌ Foydalanuvchi topilmadi."

        tc.status = TeacherCodeStatus.USED
        tc.used_by = user_id
        tc.used_at = datetime.utcnow()

        user.is_teacher = True
        user.teacher_plan = TeacherPlan.TRIAL
        user.teacher_plan_expires_at = datetime.utcnow() + timedelta(days=trial_days)
        user.teacher_duels_used = 0
        user.teacher_tests_used = 0

        await session.commit()
        return True, "✅ Kod muvaffaqiyatli faollashtirildi!"


async def expire_stale_teacher_codes(expiry_days: int) -> int:
    async with async_session() as session:
        cutoff = datetime.utcnow() - timedelta(days=expiry_days)
        result = await session.execute(
            update(TeacherCode)
            .where(TeacherCode.status == TeacherCodeStatus.UNUSED, TeacherCode.created_at < cutoff)
            .values(status=TeacherCodeStatus.EXPIRED)
        )
        await session.commit()
        return result.rowcount or 0


# --------------------------------------------------------------------------- #
# Teacher plan / limit management
# --------------------------------------------------------------------------- #


def _teacher_effective_plan(user: User) -> TeacherPlan:
    """Trial/limited muddati o'tganini hisobga olib joriy holatni qaytaradi (lazy check uchun)."""
    now = datetime.utcnow()
    if user.teacher_gifted_unlimited:
        return TeacherPlan.UNLIMITED_1M
    if user.teacher_plan in (TeacherPlan.TRIAL, TeacherPlan.UNLIMITED_1M, TeacherPlan.GOLD_3M):
        if user.teacher_plan_expires_at and now >= user.teacher_plan_expires_at:
            return TeacherPlan.LIMITED
    return user.teacher_plan


async def sync_teacher_plan_state(user_id: int) -> User | None:
    """Har bir ustoz-panel kirishida chaqiriladi: muddati o'tgan trial/tarifni
    LIMITED holatiga o'tkazadi va agar LIMITED bo'lsa oylik hisoblagichlarni
    kerak bo'lsa nolga tushiradi."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_teacher:
            return user
        now = datetime.utcnow()

        if not user.teacher_gifted_unlimited and user.teacher_plan in (
            TeacherPlan.TRIAL, TeacherPlan.UNLIMITED_1M, TeacherPlan.GOLD_3M
        ) and user.teacher_plan_expires_at and now >= user.teacher_plan_expires_at:
            user.teacher_plan = TeacherPlan.LIMITED
            user.teacher_period_reset_at = now + timedelta(days=30)
            user.teacher_duels_used = 0
            user.teacher_tests_used = 0

        if user.teacher_plan == TeacherPlan.LIMITED and not user.teacher_gifted_unlimited:
            if not user.teacher_period_reset_at:
                user.teacher_period_reset_at = now + timedelta(days=30)
            elif now >= user.teacher_period_reset_at:
                user.teacher_duels_used = 0
                user.teacher_tests_used = 0
                user.teacher_period_reset_at = now + timedelta(days=30)

        await session.commit()
        await session.refresh(user)
        return user


def teacher_has_unlimited(user: User) -> bool:
    if user.teacher_gifted_unlimited:
        return True
    plan = _teacher_effective_plan(user)
    return plan in (TeacherPlan.TRIAL, TeacherPlan.UNLIMITED_1M, TeacherPlan.GOLD_3M)


def teacher_can_create_duel(user: User) -> bool:
    if teacher_has_unlimited(user):
        return True
    return user.teacher_duels_used < config.teacher_monthly_duel_limit


def teacher_can_add_test(user: User) -> bool:
    if teacher_has_unlimited(user):
        return True
    return user.teacher_tests_used < config.teacher_monthly_test_limit


async def increment_teacher_duel_usage(user_id: int) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.teacher_total_duels += 1
        if not teacher_has_unlimited(user):
            user.teacher_duels_used += 1
        await session.commit()


async def increment_teacher_test_usage(user_id: int, count: int = 1) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.teacher_total_tests += count
        if not teacher_has_unlimited(user):
            user.teacher_tests_used += count
        await session.commit()


async def set_teacher_gift(user_id: int, unlimited: bool, days: int | None, duel_limit: int | None,
                            test_limit: int | None) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.is_teacher = True
        if unlimited:
            user.teacher_gifted_unlimited = True
            user.teacher_plan = TeacherPlan.UNLIMITED_1M
            if days:
                user.teacher_plan_expires_at = datetime.utcnow() + timedelta(days=days)
        else:
            user.teacher_plan = TeacherPlan.LIMITED
            user.teacher_period_reset_at = datetime.utcnow() + timedelta(days=30)
            if duel_limit is not None:
                user.teacher_duels_used = max(0, user.teacher_duels_used - duel_limit) if False else 0
                # gift qo'shimcha limit sifatida: mavjud limitga qo'shiladi (used'ni kamaytiramiz)
            if duel_limit is not None:
                pass
        await session.commit()


async def grant_teacher_plan(user_id: int, plan: TeacherPlan) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.is_teacher = True
        user.teacher_plan = plan
        days = 30 if plan == TeacherPlan.UNLIMITED_1M else 90
        user.teacher_plan_expires_at = datetime.utcnow() + timedelta(days=days)
        user.teacher_gifted_unlimited = False
        await session.commit()


async def get_teacher_stats_revenue() -> dict:
    async with async_session() as session:
        rows = await session.execute(
            select(TeacherPurchase.plan, func.count(), func.coalesce(func.sum(TeacherPurchase.price), 0))
            .where(TeacherPurchase.status == TeacherPurchaseStatus.APPROVED)
            .group_by(TeacherPurchase.plan)
        )
        data = {plan.value: {"count": cnt, "total": total} for plan, cnt, total in rows.all()}
        total_all = sum(v["total"] for v in data.values())
        return {"by_plan": data, "total": total_all}


# --------------------------------------------------------------------------- #
# Teacher purchases (admin-approval flow, like withdrawals)
# --------------------------------------------------------------------------- #


async def create_teacher_purchase(user_id: int, plan: TeacherPlan, price: int) -> TeacherPurchase:
    async with async_session() as session:
        p = TeacherPurchase(user_id=user_id, plan=plan, price=price)
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


async def get_pending_teacher_purchases() -> list[TeacherPurchase]:
    async with async_session() as session:
        result = await session.execute(
            select(TeacherPurchase).where(TeacherPurchase.status == TeacherPurchaseStatus.PENDING)
            .order_by(TeacherPurchase.created_at)
        )
        return list(result.scalars().all())


async def resolve_teacher_purchase(purchase_id: int, approved: bool) -> TeacherPurchase | None:
    async with async_session() as session:
        p = await session.get(TeacherPurchase, purchase_id)
        if not p:
            return None
        p.status = TeacherPurchaseStatus.APPROVED if approved else TeacherPurchaseStatus.REJECTED
        p.resolved_at = datetime.utcnow()
        await session.commit()
        await session.refresh(p)
        return p


# --------------------------------------------------------------------------- #
# Teacher test bank
# --------------------------------------------------------------------------- #


async def add_teacher_test(teacher_id: int, test: dict) -> Test:
    async with async_session() as session:
        t = Test(
            question=test["question"],
            option_a=test["option_a"],
            option_b=test["option_b"],
            option_c=test["option_c"],
            option_d=test["option_d"],
            correct_option=test["correct_option"],
            added_by=teacher_id,
            teacher_id=teacher_id,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


async def get_teacher_tests(teacher_id: int, limit: int = 50) -> list[Test]:
    async with async_session() as session:
        result = await session.execute(
            select(Test).where(Test.teacher_id == teacher_id).order_by(Test.id.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def count_teacher_tests(teacher_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(Test).where(Test.teacher_id == teacher_id)
        )
        return result.scalar_one()


async def delete_teacher_test(test_id: int, teacher_id: int) -> bool:
    async with async_session() as session:
        t = await session.get(Test, test_id)
        if not t or t.teacher_id != teacher_id:
            return False
        await session.delete(t)
        await session.commit()
        return True


async def update_teacher_test(test_id: int, teacher_id: int, **fields) -> bool:
    async with async_session() as session:
        t = await session.get(Test, test_id)
        if not t or t.teacher_id != teacher_id:
            return False
        for k, v in fields.items():
            setattr(t, k, v)
        await session.commit()
        return True


async def get_teacher_random_questions(teacher_id: int, count: int) -> list[Test]:
    async with async_session() as session:
        result = await session.execute(
            select(Test).where(Test.teacher_id == teacher_id).order_by(func.random()).limit(count)
        )
        return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# Custom duels (teacher-created 1v1 for two named students)
# --------------------------------------------------------------------------- #


async def create_custom_duel(teacher_id: int, p1_username: str, p2_username: str,
                              num_questions: int, time_per_question: int) -> CustomDuel:
    async with async_session() as session:
        cd = CustomDuel(
            teacher_id=teacher_id,
            token=uuid.uuid4().hex[:10],
            watch_token=uuid.uuid4().hex[:10],
            player1_username=p1_username.lstrip("@").lower(),
            player2_username=p2_username.lstrip("@").lower(),
            num_questions=num_questions,
            time_per_question=time_per_question,
        )
        session.add(cd)
        await session.commit()
        await session.refresh(cd)
        return cd


async def get_custom_duel_by_token(token: str) -> CustomDuel | None:
    async with async_session() as session:
        result = await session.execute(select(CustomDuel).where(CustomDuel.token == token))
        return result.scalar_one_or_none()


async def get_custom_duel_by_watch_token(token: str) -> CustomDuel | None:
    async with async_session() as session:
        result = await session.execute(select(CustomDuel).where(CustomDuel.watch_token == token))
        return result.scalar_one_or_none()


async def get_custom_duel(duel_id: int) -> CustomDuel | None:
    async with async_session() as session:
        return await session.get(CustomDuel, duel_id)


async def set_custom_duel_player(duel_id: int, slot: int, user_id: int) -> CustomDuel | None:
    async with async_session() as session:
        cd = await session.get(CustomDuel, duel_id)
        if not cd:
            return None
        if slot == 1:
            cd.player1_id = user_id
        else:
            cd.player2_id = user_id
        if cd.player1_id and cd.player2_id:
            cd.status = CustomDuelStatus.ACTIVE
            cd.started_at = datetime.utcnow()
        await session.commit()
        await session.refresh(cd)
        return cd


async def finish_custom_duel(duel_id: int, p1_score: int, p2_score: int, winner_id: int | None) -> None:
    async with async_session() as session:
        cd = await session.get(CustomDuel, duel_id)
        if not cd:
            return
        cd.player1_score = p1_score
        cd.player2_score = p2_score
        cd.winner_id = winner_id
        cd.status = CustomDuelStatus.FINISHED
        cd.finished_at = datetime.utcnow()
        await session.commit()


async def get_teacher_custom_duels(teacher_id: int, limit: int = 50) -> list[CustomDuel]:
    async with async_session() as session:
        result = await session.execute(
            select(CustomDuel).where(CustomDuel.teacher_id == teacher_id)
            .order_by(CustomDuel.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_teacher_custom_duels_by_date(teacher_id: int, date_from: datetime | None,
                                            date_to: datetime | None) -> list[CustomDuel]:
    async with async_session() as session:
        query = select(CustomDuel).where(CustomDuel.teacher_id == teacher_id,
                                          CustomDuel.status == CustomDuelStatus.FINISHED)
        if date_from:
            query = query.where(CustomDuel.finished_at >= date_from)
        if date_to:
            query = query.where(CustomDuel.finished_at <= date_to)
        query = query.order_by(CustomDuel.finished_at.desc())
        result = await session.execute(query)
        return list(result.scalars().all())


async def count_active_custom_duels() -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(CustomDuel).where(CustomDuel.status == CustomDuelStatus.ACTIVE)
        )
        return result.scalar_one()


# --------------------------------------------------------------------------- #
# Special (2-user) tests
# --------------------------------------------------------------------------- #


async def create_special_test(created_by: int, q_number: str, question: str, opt1: str, opt2: str, opt3: str,
                               opt4: str, correct: int, user1_id: int, user2_id: int) -> SpecialTest:
    async with async_session() as session:
        st = SpecialTest(
            created_by=created_by, question_number=q_number, question=question,
            option_1=opt1, option_2=opt2, option_3=opt3, option_4=opt4,
            correct_option=correct, user1_id=user1_id, user2_id=user2_id,
        )
        session.add(st)
        await session.commit()
        await session.refresh(st)
        return st


async def get_special_test(test_id: int) -> SpecialTest | None:
    async with async_session() as session:
        return await session.get(SpecialTest, test_id)


async def set_special_test_choice(test_id: int, user_id: int, choice: int) -> SpecialTest | None:
    async with async_session() as session:
        st = await session.get(SpecialTest, test_id)
        if not st or st.status != SpecialTestStatus.PENDING:
            return st
        now = datetime.utcnow()
        if user_id == st.user1_id:
            st.user1_choice = choice
            st.user1_answered_at = now
        elif user_id == st.user2_id:
            st.user2_choice = choice
            st.user2_answered_at = now
        await session.commit()
        await session.refresh(st)
        return st


async def finalize_special_test(test_id: int, user_id: int) -> tuple[SpecialTest | None, bool]:
    """Returns (test, was_first_to_finish). Agar test allaqachon yopilgan bo'lsa was_first=False."""
    async with async_session() as session:
        st = await session.get(SpecialTest, test_id)
        if not st:
            return None, False
        if st.status == SpecialTestStatus.FINISHED:
            return st, False

        choice = st.user1_choice if user_id == st.user1_id else st.user2_choice
        if choice is None:
            return st, False  # hali javob tanlamagan

        st.status = SpecialTestStatus.FINISHED
        st.finished_by = user_id
        st.finished_at = datetime.utcnow()
        answered_at = st.user1_answered_at if user_id == st.user1_id else st.user2_answered_at
        if answered_at:
            st.response_seconds = int((st.finished_at - st.created_at).total_seconds())
        await session.commit()
        await session.refresh(st)
        return st, True


# --------------------------------------------------------------------------- #
# `db` namespace
# In the original modular project every handler/middleware imported this
# file as `from bot.database import requests as db` and called functions as
# `db.get_user(...)`, `db.get_or_create_user(...)`, etc. Since everything now
# lives in one flat module (no more separate `bot.database.requests` module
# to import), this SimpleNamespace recreates that same `db.xxx(...)` call
# surface so every call site below keeps working unmodified.
# --------------------------------------------------------------------------- #
import types as _types  # noqa: E402  (local, just for this namespace helper)

db = _types.SimpleNamespace(
    get_user=get_user,
    get_or_create_user=get_or_create_user,
    update_user_fields=update_user_fields,
    adjust_lives=adjust_lives,
    adjust_balance=adjust_balance,
    register_duel_result=register_duel_result,
    apply_referral_bonus=apply_referral_bonus,
    set_status=set_status,
    ban_user=ban_user,
    mark_inactive=mark_inactive,
    get_top_players=get_top_players,
    get_user_rank=get_user_rank,
    reset_weekly_leaderboard=reset_weekly_leaderboard,
    add_tests_bulk=add_tests_bulk,
    get_random_questions=get_random_questions,
    count_tests=count_tests,
    create_withdrawal=create_withdrawal,
    get_pending_withdrawals=get_pending_withdrawals,
    resolve_withdrawal=resolve_withdrawal,
    get_transaction_history=get_transaction_history,
    create_duel_record=create_duel_record,
    finish_duel_record=finish_duel_record,
    count_active_duels=count_active_duels,
    get_setting=get_setting,
    set_setting=set_setting,
    get_stats=get_stats,
    get_all_active_user_ids=get_all_active_user_ids,
    find_user_by_id_or_username=find_user_by_id_or_username,
    # daily lives
    reset_daily_lives=reset_daily_lives,
    # mandatory channels
    add_mandatory_channel=add_mandatory_channel,
    remove_mandatory_channel=remove_mandatory_channel,
    get_mandatory_channels=get_mandatory_channels,
    get_mandatory_channel=get_mandatory_channel,
    # teacher codes
    create_teacher_code=create_teacher_code,
    get_teacher_code=get_teacher_code,
    redeem_teacher_code=redeem_teacher_code,
    expire_stale_teacher_codes=expire_stale_teacher_codes,
    # teacher plan
    sync_teacher_plan_state=sync_teacher_plan_state,
    increment_teacher_duel_usage=increment_teacher_duel_usage,
    increment_teacher_test_usage=increment_teacher_test_usage,
    set_teacher_gift=set_teacher_gift,
    grant_teacher_plan=grant_teacher_plan,
    get_teacher_stats_revenue=get_teacher_stats_revenue,
    # teacher purchases
    create_teacher_purchase=create_teacher_purchase,
    get_pending_teacher_purchases=get_pending_teacher_purchases,
    resolve_teacher_purchase=resolve_teacher_purchase,
    # teacher test bank
    add_teacher_test=add_teacher_test,
    get_teacher_tests=get_teacher_tests,
    count_teacher_tests=count_teacher_tests,
    delete_teacher_test=delete_teacher_test,
    update_teacher_test=update_teacher_test,
    get_teacher_random_questions=get_teacher_random_questions,
    # custom duels
    create_custom_duel=create_custom_duel,
    get_custom_duel_by_token=get_custom_duel_by_token,
    get_custom_duel_by_watch_token=get_custom_duel_by_watch_token,
    get_custom_duel=get_custom_duel,
    set_custom_duel_player=set_custom_duel_player,
    finish_custom_duel=finish_custom_duel,
    get_teacher_custom_duels=get_teacher_custom_duels,
    get_teacher_custom_duels_by_date=get_teacher_custom_duels_by_date,
    count_active_custom_duels=count_active_custom_duels,
    # special tests
    create_special_test=create_special_test,
    get_special_test=get_special_test,
    set_special_test_choice=set_special_test_choice,
    finalize_special_test=finalize_special_test,
)


# ============================================================================
# FSM STATES
# (from: bot/states/states.py)
# ============================================================================
class WithdrawalStates(StatesGroup):
    waiting_amount = State()
    waiting_card = State()
    confirm = State()


class BroadcastStates(StatesGroup):
    waiting_content = State()
    confirm = State()


class MandatoryChannelStates(StatesGroup):
    waiting_channel_username = State()


class TestParserStates(StatesGroup):
    waiting_confirmation = State()  # holds parsed batch in FSM data pending admin confirmation


class GiftSubscriptionStates(StatesGroup):
    waiting_user_identifier = State()
    waiting_status_choice = State()


class WithdrawalResolveStates(StatesGroup):
    waiting_reject_reason = State()


class MandatoryChannelsStates(StatesGroup):
    waiting_open_username = State()
    waiting_closed_id = State()


class TeacherCodeRedeemStates(StatesGroup):
    waiting_code = State()


class TeacherAddTestStates(StatesGroup):
    waiting_test_text = State()


class TeacherEditTestStates(StatesGroup):
    waiting_new_text = State()


class TeacherGiftStates(StatesGroup):
    waiting_identifier = State()
    waiting_mode = State()
    waiting_value = State()


class CustomDuelCreateStates(StatesGroup):
    waiting_player1 = State()
    waiting_player2 = State()
    waiting_params_confirm = State()


class TeacherResultsStates(StatesGroup):
    waiting_date_filter = State()


class SpecialTestStates(StatesGroup):
    waiting_user_ids = State()
    waiting_question_block = State()


# ============================================================================
# ADMIN FILTER
# (from: bot/filters/admin_filter.py)
# ============================================================================
class IsAdmin(BaseFilter):
    """Router-level filter: only lets admin IDs reach the wrapped handlers."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user) and user.id in config.admin_ids


# ============================================================================
# USER KEYBOARDS
# (from: bot/keyboards/user_kb.py)
# ============================================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚔️ Duelni Boshlash", callback_data="menu_duel")
    b.button(text="👥 Do'stni Chaqirish", callback_data="menu_referral")
    b.button(text="🏆 Top Reyting", callback_data="menu_rating")
    b.button(text="📜 Qoidalar", callback_data="menu_rules")
    b.button(text="⭐ Stars Do'koni & Obuna", callback_data="menu_store")
    b.button(text="👤 Profilim", callback_data="menu_profile")
    b.button(text="🎓 Ustoz Platformasi", callback_data="menu_teacher")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


def back_to_main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
    return b.as_markup()


def duel_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎲 Tasodifiy Raqib Topish", callback_data="duel_random")
    b.button(text="👥 Do'stni Duelga Chaqirish", callback_data="duel_invite_friend")
    b.button(text="🤖 Bot Bilan Mashq Qilish", callback_data="duel_practice_bot")
    b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def cancel_queue_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Qidiruvni bekor qilish", callback_data="duel_cancel_queue")
    return b.as_markup()


def duel_invite_accept_kb(inviter_id: int, duel_token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Qabul qilish", callback_data=f"duel_accept_{inviter_id}_{duel_token}")
    b.button(text="❌ Rad etish", callback_data=f"duel_decline_{inviter_id}_{duel_token}")
    b.adjust(2)
    return b.as_markup()


def question_options_kb(duel_id: int, q_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for letter in ("A", "B", "C", "D"):
        b.button(text=letter, callback_data=f"ans_{duel_id}_{q_index}_{letter}")
    b.adjust(4)
    return b.as_markup()


def referral_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
    return b.as_markup()


def store_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✨ Mini Status ({config.mini_status_price:,} so'm)".replace(",", " "), callback_data="store_mini")
    b.button(text=f"👑 Pro Gamer ({config.pro_status_price:,} so'm)".replace(",", " "), callback_data="store_pro")
    b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
    b.adjust(1, 1, 1)
    return b.as_markup()


def store_purchase_kb(plan: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Admin bilan bog'lanish", url=f"https://t.me/{config.admin_contact.lstrip('@')}")
    b.button(text="🔙 Ortga", callback_data="menu_store")
    b.adjust(1, 1)
    return b.as_markup()


def profile_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Pulni Yechib Olish", callback_data="profile_withdraw")
    b.button(text="📜 Tranzaksiyalar Tarixi", callback_data="profile_history")
    b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
    b.adjust(1, 1, 1)
    return b.as_markup()


def withdrawal_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash", callback_data="withdraw_confirm")
    b.button(text="❌ Bekor qilish", callback_data="withdraw_cancel")
    b.adjust(2)
    return b.as_markup()


def subscription_gate_kb(channel: str) -> InlineKeyboardMarkup:
    handle = channel.lstrip("@")
    b = InlineKeyboardBuilder()
    b.button(text="📢 Kanalga o'tish", url=f"https://t.me/{handle}")
    b.button(text="✅ Tekshirish", callback_data="check_subscription")
    b.adjust(1, 1)
    return b.as_markup()


def multi_subscription_gate_kb(missing: list["MandatoryChannel"]) -> InlineKeyboardMarkup:
    """Yetishmayotgan barcha majburiy kanallar uchun alohida tugmalar + tekshirish."""
    b = InlineKeyboardBuilder()
    for ch in missing:
        if ch.invite_link:
            url = ch.invite_link
        elif ch.kind == ChannelKind.OPEN:
            url = f"https://t.me/{ch.value.lstrip('@')}"
        else:
            url = None
        label = f"📢 {ch.title or ch.value}"
        if url:
            b.button(text=label, url=url)
        else:
            b.button(text=label, callback_data=f"noop_channel_{ch.id}")
    b.button(text="✅ Tekshirish", callback_data="check_subscription")
    b.adjust(1)
    return b.as_markup()


def teacher_menu_kb(is_teacher: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if is_teacher:
        b.button(text="➕ Yangi Test Qo'shish & Bazaga Yuborish", callback_data="teacher_add_test")
        b.button(text="⚔️ 2 O'quvchi Uchun Maxsus Duel Yaratish", callback_data="teacher_create_duel")
        b.button(text="📋 Mening Tuzgan Testlarim", callback_data="teacher_my_tests")
        b.button(text="📊 O'quvchilar Natijalari (Hisobot)", callback_data="teacher_results")
        b.button(text="🧪 2 O'quvchiga Maxsus Test", callback_data="admin_special_test")
        b.button(text="💳 Tarifni Yangilash", callback_data="teacher_view_plans")
        b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
        b.adjust(1)
    else:
        b.button(text="🔑 Ustoz kodini kiritish", callback_data="teacher_enter_code")
        b.button(text="🔙 Asosiy Menyuga Qaytish", callback_data="menu_main")
        b.adjust(1)
    return b.as_markup()


def back_to_teacher_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Ustoz Panyeliga Qaytish", callback_data="menu_teacher")
    return b.as_markup()


def teacher_question_count_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in config.custom_duel_question_choices:
        b.button(text=f"{n} ta", callback_data=f"cd_qcount_{n}")
    b.adjust(len(config.custom_duel_question_choices))
    return b.as_markup()


def teacher_time_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in config.custom_duel_time_choices:
        b.button(text=f"{t} soniya", callback_data=f"cd_time_{t}")
    b.adjust(len(config.custom_duel_time_choices))
    return b.as_markup()


def custom_duel_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash va Havola Olish", callback_data="cd_confirm")
    b.button(text="❌ Bekor qilish", callback_data="cd_cancel")
    b.adjust(1, 1)
    return b.as_markup()


def teacher_test_item_kb(test_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Tahrirlash", callback_data=f"tt_edit_{test_id}")
    b.button(text="🗑 O'chirish", callback_data=f"tt_del_{test_id}")
    b.adjust(2)
    return b.as_markup()


def teacher_results_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📅 Sana bo'yicha filtrlash", callback_data="tr_filter_date")
    b.button(text="📄 Hammasi (matn)", callback_data="tr_all_text")
    b.button(text="📊 CSV eksport", callback_data="tr_export_csv")
    b.button(text="🔙 Ustoz Panyeliga Qaytish", callback_data="menu_teacher")
    b.adjust(1)
    return b.as_markup()


def teacher_plan_purchase_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"1 oy cheksiz — {config.teacher_unlimited_1m_price:,} so'm".replace(",", " "),
              callback_data="tp_buy_unlimited_1m")
    b.button(text=f"3 oylik GOLD — {config.teacher_gold_3m_price:,} so'm".replace(",", " "),
              callback_data="tp_buy_gold_3m")
    b.button(text="🔙 Ustoz Panyeliga Qaytish", callback_data="menu_teacher")
    b.adjust(1)
    return b.as_markup()


def special_test_options_kb(test_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in (1, 2, 3, 4):
        b.button(text=str(i), callback_data=f"st_ans_{test_id}_{i}")
    b.adjust(4)
    return b.as_markup()


# ============================================================================
# ADMIN KEYBOARDS
# (from: bot/keyboards/admin_kb.py)
# ============================================================================
def admin_dashboard_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistika", callback_data="admin_stats")
    b.button(text="📢 Xabar Tarqatish", callback_data="admin_broadcast")
    b.button(text="🔗 Majburiy Kanallar", callback_data="admin_channel_gate")
    b.button(text="💳 Pul Yechish So'rovlari", callback_data="admin_withdrawals")
    b.button(text="📥 Kanal Testlarini Boshqarish", callback_data="admin_tests")
    b.button(text="🚫 Ban / Unban", callback_data="admin_ban")
    b.button(text="🎁 Obuna Sovg'a Qilish", callback_data="admin_gift")
    b.button(text="🎓 Ustoz Kodi Yaratish", callback_data="admin_teacher_code")
    b.button(text="🧑‍🏫 Ustozlarni Boshqarish", callback_data="admin_teacher_manage")
    b.button(text="💰 Ustoz To'lovlari", callback_data="admin_teacher_purchases")
    b.button(text="🧪 2 Kishiga Maxsus Test", callback_data="admin_special_test")
    b.adjust(2, 2, 2, 1, 2, 1)
    return b.as_markup()


def back_to_admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Admin Panel", callback_data="admin_main")
    return b.as_markup()


def withdrawal_item_kb(withdrawal_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Pulni To'lab Berdim", callback_data=f"wd_approve_{withdrawal_id}")
    b.button(text="❌ Rad Etish", callback_data=f"wd_reject_{withdrawal_id}")
    b.adjust(2)
    return b.as_markup()


def test_batch_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash", callback_data="test_confirm_save")
    b.button(text="❌ Bekor qilish", callback_data="test_confirm_cancel")
    b.adjust(2)
    return b.as_markup()


def gift_status_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✨ Mini Status", callback_data="gift_status_mini")
    b.button(text="👑 Pro Gamer", callback_data="gift_status_pro")
    b.adjust(2)
    return b.as_markup()


def ban_action_kb(user_id: int, currently_banned: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if currently_banned:
        b.button(text="✅ Unban qilish", callback_data=f"ban_unban_{user_id}")
    else:
        b.button(text="🚫 Ban qilish", callback_data=f"ban_do_{user_id}")
    return b.as_markup()


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yuborish", callback_data="broadcast_send")
    b.button(text="❌ Bekor qilish", callback_data="broadcast_cancel")
    b.adjust(2)
    return b.as_markup()


def admin_channels_list_kb(channels: list["MandatoryChannel"]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        label = f"🗑 {'🔓' if ch.kind == ChannelKind.OPEN else '🔒'} {ch.title or ch.value}"
        b.button(text=label, callback_data=f"mc_remove_{ch.id}")
    b.button(text="➕ Ochiq kanal qo'shish (@username)", callback_data="mc_add_open")
    b.button(text="➕ Yopiq/maxfiy kanal qo'shish (ID)", callback_data="mc_add_closed")
    b.button(text="🔙 Admin Panel", callback_data="admin_main")
    b.adjust(1)
    return b.as_markup()


def teacher_purchase_item_kb(purchase_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash", callback_data=f"tpur_approve_{purchase_id}")
    b.button(text="❌ Rad etish", callback_data=f"tpur_reject_{purchase_id}")
    b.adjust(2)
    return b.as_markup()


def admin_teacher_gift_mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="♾ Cheksiz (kun bilan)", callback_data="tg_mode_unlimited")
    b.button(text="🔢 Duel/test limiti", callback_data="tg_mode_limit")
    b.adjust(1)
    return b.as_markup()


# ============================================================================
# MIDDLEWARE: USER CONTEXT
# (from: bot/middlewares/db.py)
# ============================================================================
class UserContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        db_user, created = await db.get_or_create_user(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
        )
        data["db_user"] = db_user
        data["is_new_user"] = created

        if db_user.is_banned:
            # Silently drop - banned users get no response at all.
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            return None

        return await handler(event, data)


# ============================================================================
# MIDDLEWARE: MANDATORY CHANNEL
# (from: bot/middlewares/subscription.py)
# ============================================================================
EXEMPT_COMMANDS = {"/start"}
EXEMPT_CALLBACKS = {"check_subscription"}


async def _check_membership(bot: Bot, channel: "MandatoryChannel", user_id: int) -> bool:
    """True = a'zo (yoki tekshira olmadik -> fail-open), False = a'zo emasligi aniq."""
    chat_ref: Any = channel.value
    if channel.kind == ChannelKind.CLOSED:
        try:
            chat_ref = int(channel.value)
        except ValueError:
            return True  # noto'g'ri konfiguratsiya -> fail-open
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        # Bot kanalda admin emas yoki boshqa xatolik -> fail-open, foydalanuvchini bloklamaymiz.
        return True


class MandatoryChannelMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot: Bot = data.get("bot")

        if user is None or user.id in config.admin_ids:
            return await handler(event, data)

        if isinstance(event, Message) and event.text in EXEMPT_COMMANDS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data in EXEMPT_CALLBACKS:
            return await handler(event, data)

        channels = await db.get_mandatory_channels()
        if not channels:
            return await handler(event, data)  # gate disabled

        missing: list[MandatoryChannel] = []
        for ch in channels:
            is_member = await _check_membership(bot, ch, user.id)
            if not is_member:
                missing.append(ch)

        if not missing:
            return await handler(event, data)

 text = (
        "🔒 Botdan foydalanish uchun quyidagi kanal(lar)ga a'zo bo'ling, "
        "so'ng \"✅ Tekshirish\" tugmasini bosing:\n\n"
        "BOT YARATUVCHISI : Isoqov Mironshoh\n"
        "RASMIY MANZIL : @isoqovmironshoh"
    )
    kb = multi_subscription_gate_kb(missing)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    elif isinstance(event, CallbackQuery):
        await event.answer("Avval barcha kanallarga a'zo bo'ling!", show_alert=True)
        await event.message.answer(text, reply_markup=kb)
    return None

# ============================================================================
# DUEL QUEUE / LIVE DUEL ENGINE (shared state)
# (from: bot/utils/duel_queue.py)
# ============================================================================
class DuelQueue:
    """Simple rendezvous queue: first two callers to `join()` are paired."""

    def __init__(self) -> None:
        self._waiting: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def join(self, user_id: int) -> asyncio.Future:
        """
        Returns a Future that resolves to:
            opponent_user_id (int)  - once matched
            None                    - if this user's wait was cancelled
        """
        async with self._lock:
            # Don't match a player with themself if they double-tap.
            if user_id in self._waiting:
                return self._waiting[user_id]

            if self._waiting:
                opponent_id, opponent_future = next(iter(self._waiting.items()))
                del self._waiting[opponent_id]
                my_future: asyncio.Future = asyncio.get_event_loop().create_future()
                my_future.set_result(opponent_id)
                if not opponent_future.done():
                    opponent_future.set_result(user_id)
                return my_future

            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._waiting[user_id] = fut
            return fut

    async def cancel(self, user_id: int) -> None:
        async with self._lock:
            fut = self._waiting.pop(user_id, None)
            if fut and not fut.done():
                fut.set_result(None)

    def is_waiting(self, user_id: int) -> bool:
        return user_id in self._waiting


@dataclass
class AnswerLogEntry:
    q_index: int
    letter: str | None
    is_correct: bool
    elapsed: float | None


@dataclass
class PlayerState:
    user_id: int
    score: int = 0
    answered_current: bool = False
    answer_time: float | None = None
    is_bot: bool = False
    history: list[AnswerLogEntry] = field(default_factory=list)


@dataclass
class LiveDuel:
    duel_id: int
    duel_token: str
    questions: list  # list[Test]
    players: dict[int, PlayerState]
    current_index: int = 0
    question_started_at: float = field(default_factory=time.time)
    is_bot_practice: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timeout_task: asyncio.Task | None = None
    question_timeout: int = 15
    # Agar bu ustoz tomonidan yaratilgan maxsus (custom) duel bo'lsa:
    custom_duel_id: int | None = None
    teacher_id: int | None = None
    watchers: set[int] = field(default_factory=set)

    @property
    def current_question(self):
        return self.questions[self.current_index]

    @property
    def is_last_question(self) -> bool:
        return self.current_index >= len(self.questions) - 1

    def opponent_of(self, user_id: int) -> int | None:
        for pid in self.players:
            if pid != user_id:
                return pid
        return None

    def both_answered(self) -> bool:
        return all(p.answered_current for p in self.players.values())

    def reset_for_next_question(self) -> None:
        self.current_index += 1
        self.question_started_at = time.time()
        for p in self.players.values():
            p.answered_current = False
            p.answer_time = None


class DuelManager:
    """Registry of currently-active LiveDuel sessions, keyed by duel_id."""

    def __init__(self) -> None:
        self._sessions: dict[int, LiveDuel] = {}

    def create(self, duel_id: int, player_ids: list[int], questions: list, is_bot_practice: bool = False,
               question_timeout: int | None = None, custom_duel_id: int | None = None,
               teacher_id: int | None = None) -> LiveDuel:
        players = {pid: PlayerState(user_id=pid, is_bot=(pid == -1)) for pid in player_ids}
        session = LiveDuel(
            duel_id=duel_id,
            duel_token=uuid.uuid4().hex[:8],
            questions=questions,
            players=players,
            is_bot_practice=is_bot_practice,
            question_timeout=question_timeout or config.duel_question_timeout,
            custom_duel_id=custom_duel_id,
            teacher_id=teacher_id,
        )
        self._sessions[duel_id] = session
        return session

    def get(self, duel_id: int) -> LiveDuel | None:
        return self._sessions.get(duel_id)

    def remove(self, duel_id: int) -> None:
        self._sessions.pop(duel_id, None)

    def find_by_player(self, user_id: int) -> LiveDuel | None:
        for session in self._sessions.values():
            if user_id in session.players:
                return session
        return None

    def find_by_custom_duel(self, custom_duel_id: int) -> LiveDuel | None:
        for session in self._sessions.values():
            if session.custom_duel_id == custom_duel_id:
                return session
        return None


def score_for_answer(is_correct: bool, elapsed_seconds: float, timeout: int | None = None) -> int:
    """
    Faster + correct answers earn more points.
    Correct answer: base 10 points + up to 10 bonus points for speed
    (linear falloff across the question timeout window).
    Wrong/no answer: 0 points.
    """
    if not is_correct:
        return 0
    timeout = timeout or config.duel_question_timeout
    remaining_ratio = max(0.0, (timeout - elapsed_seconds) / timeout)
    return 10 + round(10 * remaining_ratio)


# Module-level singletons used across handlers
duel_queue = DuelQueue()
duel_manager = DuelManager()

# custom_duel_id -> set(user_id) - odamlar hali boshlanmagan maxsus duelni watch_ havolasi
# orqali ochib qo'yishi mumkin; duel boshlanganda ular avtomatik session.watchers ga o'tkaziladi.
pending_watchers: dict[int, set[int]] = {}

# custom_duel_id -> asyncio.Task (15 daqiqalik "hali qo'shilmadi" eslatma vazifasi)
join_reminder_tasks: dict[int, asyncio.Task] = {}


# ============================================================================
# SCHEDULER
# (from: bot/utils/scheduler.py)
# ============================================================================
def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

    async def _weekly_reset() -> None:
        top = await db.get_top_players(limit=3)
        await db.reset_weekly_leaderboard()
        if top:
            prizes = [config.prize_1st, config.prize_2nd, config.prize_3rd]
            lines = ["🏆 Haftalik reyting yakunlandi! G'oliblar:"]
            for i, player in enumerate(top):
                lines.append(f"{i + 1}. {player.full_name} — {player.score} ball — 🎁 {prizes[i]:,} so'm".replace(",", " "))
                try:
                    await bot.send_message(
                        player.user_id,
                        f"🎉 Tabriklaymiz! Siz haftalik reytingda {i + 1}-o'rinni egalladingiz "
                        f"va {prizes[i]:,} so'm yutdingiz!".replace(",", " "),
                    )
                except Exception:
                    pass
            if config.admin_chat_id:
                try:
                    await bot.send_message(config.admin_chat_id, "\n".join(lines))
                except Exception:
                    pass

    scheduler.add_job(_weekly_reset, CronTrigger(day_of_week="sun", hour=21, minute=0))
    return scheduler


# ============================================================================
# HANDLERS: USER - DUEL
# (from: bot/handlers/user/duel.py)
# ============================================================================
duel_router = Router(name="duel")

BOT_OPPONENT_ID = -1  # sentinel "user_id" representing the practice bot

RULES_TEXT = (
    "⚔️ <b>Duel qoidalari</b>\n\n"
    f"• Har bir duelda {config.questions_per_duel} ta savol beriladi.\n"
    f"• Har bir savolga javob berish uchun {config.duel_question_timeout} soniya vaqtingiz bor.\n"
    "• Tez va to'g'ri javob ko'proq ball beradi.\n"
    "• Duelda yutqazgan o'yinchi 1 ta 🫀 jonini yo'qotadi.\n\n"
    "Quyidagilardan birini tanlang 👇"
)


def _fmt_question(session: LiveDuel) -> str:
    q = session.current_question
    total = len(session.questions)
    return (
        f"❓ Savol {session.current_index + 1}/{total}\n\n"
        f"<b>{q.question}</b>\n\n"
        f"A) {q.option_a}\n"
        f"B) {q.option_b}\n"
        f"C) {q.option_c}\n"
        f"D) {q.option_d}\n\n"
        f"⏱ {session.question_timeout} soniya vaqtingiz bor!"
    )


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #


@duel_router.callback_query(F.data == "menu_duel")
async def cb_menu_duel(callback: CallbackQuery) -> None:
    await callback.message.edit_text(RULES_TEXT, reply_markup=duel_menu_kb())
    await callback.answer()


def _has_lives(user: User) -> bool:
    return user.lives > 0


# --------------------------------------------------------------------------- #
# Random matchmaking
# --------------------------------------------------------------------------- #


def _no_lives_text() -> str:
    remaining = seconds_until_next_midnight(config.timezone)
    return (
        f"🫀 Jonlaringiz tugagan! Ertangi kungacha {format_hm(remaining)} qoldi.\n"
        "Do'stlaringizni taklif qilib +1 🫀 jon oling yoki Pro Gamer bo'ling."
    )


@duel_router.callback_query(F.data == "duel_random")
async def cb_duel_random(callback: CallbackQuery, db_user: User) -> None:
    if not _has_lives(db_user):
        await callback.answer(_no_lives_text(), show_alert=True)
        return

    if await db.count_tests() < config.questions_per_duel:
        await callback.answer("⚠️ Hozircha savollar yetarli emas. Birozdan so'ng urinib ko'ring.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("🔍 Raqib qidirilmoqda...", reply_markup=cancel_queue_kb())

    future = await duel_queue.join(callback.from_user.id)
    try:
        opponent_id = await asyncio.wait_for(future, timeout=60)
    except asyncio.TimeoutError:
        await duel_queue.cancel(callback.from_user.id)
        opponent_id = None

    if opponent_id is None:
        try:
            await callback.message.edit_text(
                "😔 Hozircha raqib topilmadi. Birozdan so'ng qayta urinib ko'ring.",
                reply_markup=duel_menu_kb(),
            )
        except TelegramBadRequest:
            pass
        return

    await _start_live_duel(callback.bot, [callback.from_user.id, opponent_id])


@duel_router.callback_query(F.data == "duel_cancel_queue")
async def cb_duel_cancel_queue(callback: CallbackQuery) -> None:
    await duel_queue.cancel(callback.from_user.id)
    await callback.answer("Qidiruv bekor qilindi.")
    await callback.message.edit_text(RULES_TEXT, reply_markup=duel_menu_kb())


# --------------------------------------------------------------------------- #
# Friend invite (deep link)
# --------------------------------------------------------------------------- #


@duel_router.callback_query(F.data == "duel_invite_friend")
async def cb_duel_invite_friend(callback: CallbackQuery, db_user: User) -> None:
    if not _has_lives(db_user):
        await callback.answer(_no_lives_text(), show_alert=True)
        return
    link = f"https://t.me/{config.bot_username}?start=duel_{callback.from_user.id}"
    await callback.message.edit_text(
        "👥 Do'stingizni ushbu havola orqali duelga taklif qiling:\n\n"
        f"<code>{link}</code>\n\n"
        "Do'stingiz havolani ochishi bilanoq siz bilan duel boshlanadi!",
        reply_markup=back_to_main_kb(),
    )
    await callback.answer()


async def handle_duel_deeplink(message: Message, inviter_id_str: str, db_user: User) -> None:
    """Called from handlers/user/start.py when someone opens a `?start=duel_<id>` link."""
    try:
        inviter_id = int(inviter_id_str)
    except ValueError:
        await message.answer("⚠️ Taklif havolasi noto'g'ri.")
        return

    if inviter_id == db_user.user_id:
        await message.answer("⚠️ O'zingizni duelga taklif qila olmaysiz.")
        return

    inviter = await db.get_user(inviter_id)
    if inviter is None:
        await message.answer("⚠️ Taklif qilgan foydalanuvchi topilmadi.")
        return

    if inviter.lives <= 0 or db_user.lives <= 0:
        await message.answer("🫀 Duel uchun ikkala tomonda ham yetarli jon bo'lishi kerak.")
        return

    if await db.count_tests() < config.questions_per_duel:
        await message.answer("⚠️ Hozircha savollar yetarli emas.")
        return

    await message.answer(f"✅ Siz {inviter.full_name} bilan duelga qo'shildingiz! Boshlanmoqda...")
    try:
        await message.bot.send_message(inviter_id, f"✅ {db_user.full_name} taklifingizni qabul qildi! Duel boshlanmoqda...")
    except Exception:
        pass

    await _start_live_duel(message.bot, [inviter_id, db_user.user_id])


# --------------------------------------------------------------------------- #
# Bot practice
# --------------------------------------------------------------------------- #


@duel_router.callback_query(F.data == "duel_practice_bot")
async def cb_duel_practice_bot(callback: CallbackQuery, db_user: User) -> None:
    if await db.count_tests() < config.questions_per_duel:
        await callback.answer("⚠️ Hozircha savollar yetarli emas.", show_alert=True)
        return
    await callback.answer()
    await _start_live_duel(callback.bot, [callback.from_user.id, BOT_OPPONENT_ID], is_bot_practice=True)


# --------------------------------------------------------------------------- #
# Core live-duel engine
# --------------------------------------------------------------------------- #


async def _start_live_duel(bot: Bot, player_ids: list[int], is_bot_practice: bool = False,
                            custom_duel: "CustomDuel | None" = None) -> None:
    if custom_duel is not None:
        questions = await db.get_teacher_random_questions(custom_duel.teacher_id, custom_duel.num_questions)
        needed = custom_duel.num_questions
    else:
        questions = await db.get_random_questions(config.questions_per_duel)
        needed = config.questions_per_duel

    if len(questions) < needed:
        for pid in player_ids:
            if pid != BOT_OPPONENT_ID:
                try:
                    await bot.send_message(pid, "⚠️ Savollar yetarli emas, duel boshlanmadi.")
                except Exception:
                    pass
        return

    real_p1 = player_ids[0]
    real_p2 = None if is_bot_practice else player_ids[1]

    if custom_duel is not None:
        duel_record_id = custom_duel.id
        timeout = custom_duel.time_per_question
    else:
        duel_record = await db.create_duel_record(real_p1, real_p2, is_bot_practice=is_bot_practice)
        duel_record_id = duel_record.id
        timeout = config.duel_question_timeout

    session = duel_manager.create(
        duel_record_id, player_ids, questions, is_bot_practice=is_bot_practice,
        question_timeout=timeout,
        custom_duel_id=custom_duel.id if custom_duel else None,
        teacher_id=custom_duel.teacher_id if custom_duel else None,
    )

    if custom_duel is not None:
        for pid in player_ids:
            try:
                await bot.send_message(pid, "🚀 Ikkalangiz ham qo'shildingiz! Maxsus duel boshlanmoqda...")
            except Exception:
                pass
        try:
            teacher = await db.get_user(custom_duel.teacher_id)
            if teacher:
                await bot.send_message(
                    custom_duel.teacher_id,
                    f"🚀 Maxsus duel #{custom_duel.id} boshlandi: "
                    f"@{custom_duel.player1_username} vs @{custom_duel.player2_username}",
                )
        except Exception:
            pass

    await _send_current_question(bot, session)


async def _broadcast_to_watchers(bot: Bot, session: LiveDuel, text: str) -> None:
    if not session.watchers:
        return
    watcher_list = list(session.watchers)
    batch_size = 25
    for i in range(0, len(watcher_list), batch_size):
        batch = watcher_list[i:i + batch_size]
        for wid in batch:
            try:
                await bot.send_message(wid, text)
            except Exception:
                pass
        await asyncio.sleep(0.05)


async def _send_current_question(bot: Bot, session: LiveDuel) -> None:
    session.question_started_at = time.time()
    text = _fmt_question(session)
    for pid in session.players:
        if pid == BOT_OPPONENT_ID:
            continue
        try:
            await bot.send_message(pid, text, reply_markup=question_options_kb(session.duel_id, session.current_index))
        except Exception:
            pass

    if session.watchers:
        await _broadcast_to_watchers(bot, session, "👀 " + text)

    if session.is_bot_practice:
        asyncio.create_task(_bot_auto_answer(bot, session.duel_id, session.current_index))

    session.timeout_task = asyncio.create_task(_question_timeout_watcher(bot, session.duel_id, session.current_index))


async def _question_timeout_watcher(bot: Bot, duel_id: int, q_index: int) -> None:
    session = duel_manager.get(duel_id)
    timeout = session.question_timeout if session else config.duel_question_timeout
    await asyncio.sleep(timeout)
    session = duel_manager.get(duel_id)
    if session is None or session.current_index != q_index:
        return  # already moved on
    async with session.lock:
        if session.current_index != q_index:
            return
        await _advance_question(bot, session)


async def _bot_auto_answer(bot: Bot, duel_id: int, q_index: int) -> None:
    """Simulated opponent for practice mode: answers after a random delay with ~65% accuracy."""
    session = duel_manager.get(duel_id)
    timeout = session.question_timeout if session else config.duel_question_timeout
    delay = random.uniform(2.5, max(3.0, timeout - 1))
    await asyncio.sleep(max(0.5, delay))
    session = duel_manager.get(duel_id)
    if session is None or session.current_index != q_index:
        return
    correct = session.current_question.correct_option
    letters = ["A", "B", "C", "D"]
    chosen = correct if random.random() < 0.65 else random.choice([l for l in letters if l != correct])
    await _submit_answer(bot, session, BOT_OPPONENT_ID, chosen)


@duel_router.callback_query(F.data.startswith("ans_"))
async def cb_answer(callback: CallbackQuery) -> None:
    _, duel_id_str, q_index_str, letter = callback.data.split("_")
    duel_id, q_index = int(duel_id_str), int(q_index_str)

    session = duel_manager.get(duel_id)
    if session is None:
        await callback.answer("⚠️ Bu duel allaqachon tugagan.", show_alert=True)
        return
    if session.current_index != q_index:
        await callback.answer("⏱ Vaqt tugadi, keyingi savolga o'ting.", show_alert=True)
        return
    if callback.from_user.id not in session.players:
        await callback.answer()
        return
    if session.players[callback.from_user.id].answered_current:
        await callback.answer("✅ Siz allaqachon javob berdingiz.")
        return

    await callback.answer("✅ Javobingiz qabul qilindi!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await _submit_answer(callback.bot, session, callback.from_user.id, letter)


async def _submit_answer(bot: Bot, session: LiveDuel, user_id: int, letter: str | None) -> None:
    async with session.lock:
        player = session.players.get(user_id)
        if player is None or player.answered_current:
            return
        elapsed = time.time() - session.question_started_at
        is_correct = bool(letter) and letter == session.current_question.correct_option
        points = score_for_answer(is_correct, elapsed, session.question_timeout)

        player.answered_current = True
        player.answer_time = elapsed
        player.score += points
        player.history.append(AnswerLogEntry(
            q_index=session.current_index, letter=letter, is_correct=is_correct, elapsed=elapsed,
        ))

        if session.both_answered():
            await _advance_question(bot, session)


async def _advance_question(bot: Bot, session: LiveDuel) -> None:
    if session.timeout_task and not session.timeout_task.done():
        session.timeout_task.cancel()

    # javob bermagan o'yinchilar uchun ham tarixga yozib qo'yamiz (statistikada nazorat qilish uchun)
    for pid, p in session.players.items():
        if not p.answered_current:
            p.history.append(AnswerLogEntry(q_index=session.current_index, letter=None, is_correct=False, elapsed=None))
            p.answered_current = True

    correct = session.current_question.correct_option
    result_lines = [f"✅ To'g'ri javob: <b>{correct}</b>"]
    for pid, p in session.players.items():
        name = "🤖 Bot" if pid == BOT_OPPONENT_ID else f"O'yinchi {pid}"
        last = p.history[-1] if p.history else None
        status = "javob berdi" if last and last.letter else "javob bermadi"
        result_lines.append(f"{name}: {status}")
    result_text = "\n".join(result_lines)

    for pid in session.players:
        if pid == BOT_OPPONENT_ID:
            continue
        try:
            await bot.send_message(pid, result_text)
        except Exception:
            pass
    if session.watchers:
        await _broadcast_to_watchers(bot, session, "👀 " + result_text)

    if session.is_last_question:
        await _finish_duel(bot, session)
        return

    session.reset_for_next_question()
    await asyncio.sleep(1.5)
    await _send_current_question(bot, session)


def _result_card_text(title: str, p1_label: str, p1_score: int, p2_label: str, p2_score: int,
                       winner_label: str | None) -> str:
    return (
        "🏆━━━━━━━━━━━━🏆\n"
        f"   {title}\n"
        "🏆━━━━━━━━━━━━🏆\n\n"
        f"👤 {p1_label}: {p1_score} ball\n"
        f"👤 {p2_label}: {p2_score} ball\n\n"
        + (f"🥇 G'olib: {winner_label}" if winner_label else "🤝 Durrang!")
    )


async def _finish_duel(bot: Bot, session: LiveDuel) -> None:
    player_ids = list(session.players.keys())
    p1_id, p2_id = player_ids[0], player_ids[1]
    p1_score = session.players[p1_id].score
    p2_score = session.players[p2_id].score

    if p1_score > p2_score:
        winner_id, loser_id = p1_id, p2_id
    elif p2_score > p1_score:
        winner_id, loser_id = p2_id, p1_id
    else:
        winner_id, loser_id = None, None

    if session.custom_duel_id is not None:
        await _finish_custom_duel_session(bot, session, p1_id, p2_id, p1_score, p2_score, winner_id)
        return

    await db.finish_duel_record(session.duel_id, p1_score, p2_score, winner_id)

    for pid in (p1_id, p2_id):
        if pid == BOT_OPPONENT_ID:
            continue
        won = pid == winner_id
        points_earned = session.players[pid].score
        await db.register_duel_result(pid, won, points_earned)
        if pid == loser_id:
            await db.adjust_lives(pid, -1)

    summary_for = lambda me_id, opp_id: (  # noqa: E731
        "🏁 <b>Duel yakunlandi!</b>\n\n"
        f"Sizning balingiz: {session.players[me_id].score}\n"
        f"Raqibingiz balli: {session.players[opp_id].score}\n\n"
        + (
            "🎉 Siz g'alaba qozondingiz!"
            if winner_id == me_id
            else ("🤝 Durrang!" if winner_id is None else "😔 Siz yutqazdingiz, 1 🫀 jon yo'qotdingiz.")
        )
    )

    for pid in (p1_id, p2_id):
        if pid == BOT_OPPONENT_ID:
            continue
        opp_id = p2_id if pid == p1_id else p1_id
        try:
            await bot.send_message(pid, summary_for(pid, opp_id), reply_markup=duel_menu_kb())
        except Exception:
            pass

    duel_manager.remove(session.duel_id)


async def _finish_custom_duel_session(bot: Bot, session: LiveDuel, p1_id: int, p2_id: int,
                                       p1_score: int, p2_score: int, winner_id: int | None) -> None:
    cd = await db.get_custom_duel(session.custom_duel_id)
    await db.finish_custom_duel(session.custom_duel_id, p1_score, p2_score, winner_id)

    p1_label = f"@{cd.player1_username}" if cd else f"O'yinchi {p1_id}"
    p2_label = f"@{cd.player2_username}" if cd else f"O'yinchi {p2_id}"
    winner_label = None
    if winner_id == p1_id:
        winner_label = p1_label
    elif winner_id == p2_id:
        winner_label = p2_label

    card = _result_card_text("MAXSUS DUEL YAKUNLANDI", p1_label, p1_score, p2_label, p2_score, winner_label)

    for pid, label in ((p1_id, p1_label), (p2_id, p2_label)):
        personal = (
            card + "\n\n" + ("🎉 Siz g'alaba qozondingiz!" if winner_id == pid else
                              ("🤝 Durrang!" if winner_id is None else "😔 Siz yutqazdingiz."))
        )
        try:
            await bot.send_message(pid, personal, reply_markup=back_to_main_kb())
        except Exception:
            pass

    if session.watchers:
        await _broadcast_to_watchers(bot, session, "🏁 " + card)

    # --- Ustozga avtomatik hisobot: har bir savolga qancha vaqtda javob berilgani ---
    if cd:
        lines = [card, "", "📋 <b>Savol-savol hisobot:</b>"]
        for idx in range(len(session.questions)):
            q_line = f"{idx + 1}-savol: "
            parts = []
            for pid, label in ((p1_id, p1_label), (p2_id, p2_label)):
                entry = next((e for e in session.players[pid].history if e.q_index == idx), None)
                if entry is None or entry.letter is None:
                    parts.append(f"{label} — javobsiz")
                else:
                    mark = "✅" if entry.is_correct else "❌"
                    parts.append(f"{label} — {mark} {entry.elapsed:.1f}s")
            q_line += " | ".join(parts)
            lines.append(q_line)
        try:
            await bot.send_message(cd.teacher_id, "\n".join(lines))
        except Exception:
            pass

    duel_manager.remove(session.duel_id)


# ============================================================================
# HANDLERS: USER - START
# (from: bot/handlers/user/start.py)
# ============================================================================
start_router = Router(name="start")


def _welcome_text(user: User) -> str:
    return (
        "⚡️ Xush kelibsiz, <b>Bellashuv Uz</b> platformasiga!\n\n"
        "Bu yerda siz boshqa foydalanuvchilar bilan bilim bellashuvida (duel) raqobatlashasiz, "
        "ball to'plab haftalik reytingda pul yutib olishingiz mumkin!\n\n"
        f"🫀 Jonlaringiz: <b>{user.lives} / {config.default_lives}</b>\n"
        f"🏆 Haftalik sovrinlar: 1-o'rin {config.prize_1st:,} so'm, "
        f"2-o'rin {config.prize_2nd:,} so'm, 3-o'rin {config.prize_3rd:,} so'm".replace(",", " ")
        + "\n\nQuyidagi menyudan birini tanlang 👇"
    )


@start_router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject, db_user: User, is_new_user: bool) -> None:
    payload = command.args or ""

    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload.removeprefix("ref_"))
        except ValueError:
            referrer_id = None
        if referrer_id and referrer_id != db_user.user_id and is_new_user:
            referrer = await db.get_user(referrer_id)
            if referrer:
                await db.apply_referral_bonus(referrer_id, db_user.user_id, bonus_lives=1)
                await message.bot.send_message(
                    referrer_id,
                    f"🎉 {db_user.full_name} sizning taklifingiz orqali botga qo'shildi! +1 🫀 jon oldingiz.",
                )
                db_user = await db.get_user(db_user.user_id)  # refresh (lives updated)

    elif payload.startswith("duel_"):
        # Friend accepted a direct duel invite deep-link; the actual pairing
        # logic lives in handlers/user/duel.py to keep this handler simple.

        await handle_duel_deeplink(message, payload.removeprefix("duel_"), db_user)
        return

    elif payload.startswith("tduel_"):
        await handle_custom_duel_join(message, payload.removeprefix("tduel_"), db_user)
        return

    elif payload.startswith("watch_"):
        await handle_custom_duel_watch(message, payload.removeprefix("watch_"), db_user)
        return

    await message.answer(_welcome_text(db_user), reply_markup=main_menu_kb())


async def handle_custom_duel_join(message: Message, token: str, db_user: User) -> None:
    cd = await db.get_custom_duel_by_token(token)
    if cd is None:
        await message.answer("⚠️ Bu maxsus duel havolasi noto'g'ri yoki eskirgan.")
        return
    if cd.status == CustomDuelStatus.FINISHED:
        await message.answer("⚠️ Bu maxsus duel allaqachon yakunlangan.")
        return
    if cd.status == CustomDuelStatus.CANCELLED:
        await message.answer("⚠️ Bu maxsus duel bekor qilingan.")
        return

    my_username = (db_user.username or "").lower()
    if my_username == cd.player1_username and not cd.player1_id:
        slot = 1
    elif my_username == cd.player2_username and not cd.player2_id:
        slot = 2
    elif db_user.user_id == cd.player1_id or db_user.user_id == cd.player2_id:
        await message.answer("✅ Siz allaqachon bu duelga qo'shilgansiz. Boshqa o'quvchini kuting.")
        return
    else:
        await message.answer(
            "⚠️ Bu havola sizga tegishli emas. Faqat "
            f"@{cd.player1_username} yoki @{cd.player2_username} qo'shilishi mumkin.\n\n"
            "Agar bu siz bo'lsangiz, Telegram profilingizda username o'rnatilganiga ishonch hosil qiling."
        )
        return

    # anti-fraud: bir xil telegram ID ikkala slotga ham qo'shila olmasin
    other_id = cd.player2_id if slot == 1 else cd.player1_id
    if other_id == db_user.user_id:
        await message.answer("⚠️ Siz o'zingiz bilan duelga qo'sha olmaysiz.")
        return

    cd = await db.set_custom_duel_player(cd.id, slot, db_user.user_id)
    await message.answer(f"✅ Siz maxsus duelga (#{cd.id}) qo'shildingiz!")

    if cd.status == CustomDuelStatus.ACTIVE:
        waiting_watchers = pending_watchers.pop(cd.id, set())
        await _start_live_duel(message.bot, [cd.player1_id, cd.player2_id], custom_duel=cd)
        if waiting_watchers:
            session = duel_manager.find_by_custom_duel(cd.id)
            if session:
                session.watchers |= waiting_watchers
    else:
        await message.answer("⏳ Ikkinchi o'quvchi qo'shilishini kutamiz...")


async def handle_custom_duel_watch(message: Message, watch_token: str, db_user: User) -> None:
    cd = await db.get_custom_duel_by_watch_token(watch_token)
    if cd is None:
        await message.answer("⚠️ Bu kuzatish havolasi noto'g'ri yoki eskirgan.")
        return
    if cd.status == CustomDuelStatus.FINISHED:
        winner = "Durrang"
        if cd.winner_id == cd.player1_id:
            winner = f"@{cd.player1_username}"
        elif cd.winner_id == cd.player2_id:
            winner = f"@{cd.player2_username}"
        await message.answer(
            f"🏁 Bu duel allaqachon yakunlangan.\n\n"
            f"@{cd.player1_username} {cd.player1_score} - {cd.player2_score} @{cd.player2_username}\n"
            f"G'olib: {winner}"
        )
        return

    session = duel_manager.find_by_custom_duel(cd.id)
    if session is not None:
        session.watchers.add(db_user.user_id)
        await message.answer("👀 Siz endi jonli tomoshabinsiz! Har bir savol/javob sizga ham yuboriladi.")
    else:
        pending_watchers.setdefault(cd.id, set()).add(db_user.user_id)
        await message.answer("👀 Duel hali boshlanmadi. Boshlanishi bilanoq sizga savollar yuborila boshlaydi.")


@start_router.message(CommandStart())
async def cmd_start(message: Message, db_user: User) -> None:
    await message.answer(_welcome_text(db_user), reply_markup=main_menu_kb())


@start_router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, db_user: User) -> None:
    await callback.message.edit_text(_welcome_text(db_user), reply_markup=main_menu_kb())
    await callback.answer()


@start_router.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery, db_user: User) -> None:
    # Reaching this handler at all means MandatoryChannelMiddleware already
    # let it through the exempt-callback allowlist; re-run the check here
    # explicitly so we give accurate feedback either way.
    channels = await db.get_mandatory_channels()
    if not channels:
        await callback.answer("✅ Tasdiqlandi!")
        await callback.message.edit_text(_welcome_text(db_user), reply_markup=main_menu_kb())
        return

    missing = []
    for ch in channels:
        is_member = await _check_membership(callback.bot, ch, callback.from_user.id)
        if not is_member:
            missing.append(ch)

    if not missing:
        await callback.answer("✅ Rahmat! Endi botdan foydalanishingiz mumkin.")
        await callback.message.edit_text(_welcome_text(db_user), reply_markup=main_menu_kb())
    else:
        await callback.answer("❌ Siz hali barcha kanallarga a'zo bo'lmagansiz.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=multi_subscription_gate_kb(missing))
        except TelegramBadRequest:
            pass


@start_router.callback_query(F.data.startswith("noop_channel_"))
async def cb_noop_channel(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Ushbu kanalga taklif havolasi yo'q, admin bilan bog'laning.", show_alert=True)


# ============================================================================
# HANDLERS: USER - REFERRAL
# (from: bot/handlers/user/referral.py)
# ============================================================================
referral_router = Router(name="referral")


@referral_router.callback_query(F.data == "menu_referral")
async def cb_menu_referral(callback: CallbackQuery, db_user: User) -> None:
    link = f"https://t.me/{config.bot_username}?start=ref_{db_user.user_id}"
    text = (
        "👥 <b>Do'stlarni Taklif Qilish</b>\n\n"
        "Har bir taklif qilingan do'stingiz uchun siz va u +1 🫀 jon olasiz!\n\n"
        f"🔗 Sizning taklif havolangiz:\n<code>{link}</code>\n\n"
        f"📊 Taklif qilingan do'stlar: <b>{db_user.invited_count}</b>\n"
        f"🫀 Joriy jonlar: <b>{db_user.lives}</b>"
    )
    await callback.message.edit_text(text, reply_markup=referral_menu_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: USER - RATING
# (from: bot/handlers/user/rating.py)
# ============================================================================
rating_router = Router(name="rating")

MEDALS = ["🥇", "🥈", "🥉"]


@rating_router.callback_query(F.data == "menu_rating")
async def cb_menu_rating(callback: CallbackQuery, db_user: User) -> None:
    top = await db.get_top_players(limit=5)
    rank = await db.get_user_rank(db_user.user_id)

    lines = ["🏆 <b>Top 5 Reyting</b>\n"]
    if not top:
        lines.append("Hozircha reytingda hech kim yo'q.")
    else:
        for i, player in enumerate(top):
            prefix = MEDALS[i] if i < 3 else f"{i + 1}."
            name = player.full_name or f"User {player.user_id}"
            lines.append(f"{prefix} {name} — {player.score} ball")

    lines.append(f"\n📍 Sizning o'rningiz: <b>{rank}</b> ({db_user.score} ball)")
    lines.append(
        f"\n🎁 Haftalik sovrinlar: 1-o'rin {config.prize_1st:,} so'm, "
        f"2-o'rin {config.prize_2nd:,} so'm, 3-o'rin {config.prize_3rd:,} so'm".replace(",", " ")
    )
    lines.append("\n⏰ Reyting har yakshanba soat 21:00 da yangilanadi.")

    await callback.message.edit_text("\n".join(lines), reply_markup=back_to_main_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: USER - RULES
# (from: bot/handlers/user/rules.py)
# ============================================================================
rules_router = Router(name="rules")


@rules_router.callback_query(F.data == "menu_rules")
async def cb_menu_rules(callback: CallbackQuery) -> None:
    text = (
        "📜 <b>Qoidalar</b>\n\n"
        f"1️⃣ Har bir duelda {config.questions_per_duel} ta savol beriladi, har biriga "
        f"{config.duel_question_timeout} soniya vaqt bor.\n"
        "2️⃣ Tez va to'g'ri javob ko'proq ball keltiradi.\n"
        f"3️⃣ Boshlang'ich jonlar soni: {config.default_lives} ta 🫀. Duelda yutqazsangiz 1 ta jon "
        "yo'qotasiz.\n"
        "4️⃣ Jonlaringiz tugasa, do'stlaringizni taklif qiling yoki Pro Gamer obunasini oling "
        "(cheksiz jon).\n"
        "5️⃣ Har yakshanba soat 21:00 da haftalik reyting yangilanadi va TOP-3 o'yinchi pul mukofoti "
        "oladi:\n"
        f"   🥇 {config.prize_1st:,} so'm\n   🥈 {config.prize_2nd:,} so'm\n   🥉 {config.prize_3rd:,} so'm\n"
        "6️⃣ Insofsizlik (bot ishlatish, muzlab qolish va h.k.) aniqlansa, hisobingiz bloklanishi "
        "mumkin."
    ).replace(",", " ")
    await callback.message.edit_text(text, reply_markup=back_to_main_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: USER - STORE
# (from: bot/handlers/user/store.py)
# ============================================================================
store_router = Router(name="store")


@store_router.callback_query(F.data == "menu_store")
async def cb_menu_store(callback: CallbackQuery, db_user: User) -> None:
    current = {
        UserStatus.NORMAL: "Oddiy",
        UserStatus.MINI: "✨ Mini Status",
        UserStatus.PRO: "👑 Pro Gamer",
    }[db_user.status]

    text = (
        "⭐ <b>Stars Do'koni & Obuna</b>\n\n"
        f"Joriy statusingiz: <b>{current}</b>\n\n"
        f"✨ <b>Mini Status</b> — {config.mini_status_price:,} so'm/UZS\n"
        "   • Maxsus belgi (badge)\n"
        "   • Kunlik duel limitisiz\n\n"
        f"👑 <b>Pro Gamer</b> — {config.pro_status_price:,} so'm/UZS\n"
        "   • Cheksiz 🫀 jonlar\n"
        "   • 2x ball\n"
        "   • Premium savollarga kirish\n\n"
        "Sotib olish uchun quyidagi tugmalardan birini tanlang 👇"
    ).replace(",", " ")
    await callback.message.edit_text(text, reply_markup=store_menu_kb())
    await callback.answer()


@store_router.callback_query(F.data == "store_mini")
async def cb_store_mini(callback: CallbackQuery) -> None:
    text = (
        "✨ <b>Mini Status</b>\n\n"
        f"Narxi: {config.mini_status_price:,} so'm/UZS\n\n"
        "Sotib olish uchun administrator bilan bog'laning. To'lovni tasdiqlagach, "
        "statusingiz faollashtiriladi."
    ).replace(",", " ")
    await callback.message.edit_text(text, reply_markup=store_purchase_kb("mini"))
    await callback.answer()


@store_router.callback_query(F.data == "store_pro")
async def cb_store_pro(callback: CallbackQuery) -> None:
    text = (
        "👑 <b>Pro Gamer</b>\n\n"
        f"Narxi: {config.pro_status_price:,} so'm/UZS\n\n"
        "Sotib olish uchun administrator bilan bog'laning. To'lovni tasdiqlagach, "
        "statusingiz faollashtiriladi."
    ).replace(",", " ")
    await callback.message.edit_text(text, reply_markup=store_purchase_kb("pro"))
    await callback.answer()


# ============================================================================
# HANDLERS: USER - PROFILE
# (from: bot/handlers/user/profile.py)
# ============================================================================
profile_router = Router(name="profile")

STATUS_LABEL = {
    UserStatus.NORMAL: "Oddiy",
    UserStatus.MINI: "✨ Mini Status",
    UserStatus.PRO: "👑 Pro Gamer",
}

TX_LABEL = {
    TransactionType.WITHDRAWAL: "💳 Pul yechish",
    TransactionType.PRIZE: "🏆 Sovrin",
    TransactionType.SUBSCRIPTION: "⭐ Obuna",
    TransactionType.REFERRAL_BONUS: "👥 Referal bonusi",
    TransactionType.OTHER: "🔹 Boshqa",
}


def _profile_text(user: User) -> str:
    win_rate = f"{(user.wins / user.total_duels * 100):.0f}%" if user.total_duels else "0%"
    lives_line = f"🫀 Jonlar: {user.lives}"
    if user.lives <= 0 and user.status != UserStatus.PRO:
        remaining = seconds_until_next_midnight(config.timezone)
        lives_line += f" (ertangi kungacha {format_hm(remaining)})"
    return (
        "👤 <b>Profilim</b>\n\n"
        f"Ism: {user.full_name}\n"
        f"Status: {STATUS_LABEL[user.status]}\n\n"
        f"⚔️ Jami dueller: {user.total_duels}\n"
        f"✅ G'alabalar: {user.wins}\n"
        f"❌ Mag'lubiyatlar: {user.losses}\n"
        f"📈 G'alaba foizi: {win_rate}\n\n"
        f"🏆 Jami ball: {user.lifetime_score}\n"
        f"{lives_line}\n"
        f"💰 Balans: {user.balance:,} so'm".replace(",", " ")
    )


@profile_router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery, db_user: User) -> None:
    await callback.message.edit_text(_profile_text(db_user), reply_markup=profile_menu_kb())
    await callback.answer()


@profile_router.callback_query(F.data == "profile_history")
async def cb_profile_history(callback: CallbackQuery, db_user: User) -> None:
    txs = await db.get_transaction_history(db_user.user_id, limit=10)
    if not txs:
        text = "📜 Tranzaksiyalar tarixi bo'sh."
    else:
        lines = ["📜 <b>So'nggi tranzaksiyalar</b>\n"]
        for tx in txs:
            sign = "+" if tx.amount >= 0 else ""
            lines.append(f"{tx.date:%d.%m.%Y} — {TX_LABEL[tx.type]} — {sign}{tx.amount:,} so'm".replace(",", " "))
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=profile_menu_kb())
    await callback.answer()


# --------------------------------------------------------------------------- #
# Withdrawal FSM
# --------------------------------------------------------------------------- #


@profile_router.callback_query(F.data == "profile_withdraw")
async def cb_profile_withdraw(callback: CallbackQuery, db_user: User, state: FSMContext) -> None:
    if db_user.balance <= 0:
        await callback.answer("💰 Balansingizda mablag' yo'q.", show_alert=True)
        return
    await state.set_state(WithdrawalStates.waiting_amount)
    await callback.message.edit_text(
        f"💳 <b>Pulni Yechib Olish</b>\n\nJoriy balans: {db_user.balance:,} so'm\n\n"
        "Yechib olmoqchi bo'lgan summani kiriting:".replace(",", " ")
    )
    await callback.answer()


@profile_router.message(WithdrawalStates.waiting_amount)
async def withdraw_amount_input(message: Message, db_user: User, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqam kiriting. Masalan: 50000")
        return
    amount = int(raw)
    if amount <= 0:
        await message.answer("⚠️ Summa noldan katta bo'lishi kerak.")
        return
    if amount > db_user.balance:
        await message.answer(f"⚠️ Balansingizda yetarli mablag' yo'q. Joriy balans: {db_user.balance:,} so'm".replace(",", " "))
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawalStates.waiting_card)
    await message.answer("💳 Endi karta raqamingizni kiriting (masalan: 8600 1234 5678 9012):")


@profile_router.message(WithdrawalStates.waiting_card)
async def withdraw_card_input(message: Message, state: FSMContext) -> None:
    card = (message.text or "").strip()
    digits = card.replace(" ", "")
    if not digits.isdigit() or not (12 <= len(digits) <= 19):
        await message.answer("⚠️ Karta raqami noto'g'ri. Qaytadan kiriting:")
        return

    data = await state.get_data()
    await state.update_data(card_number=card)
    await state.set_state(WithdrawalStates.confirm)
    await message.answer(
        "📝 <b>So'rovni tasdiqlang</b>\n\n"
        f"Summa: {data['amount']:,} so'm\n"
        f"Karta: {card}\n\n"
        "Ma'lumotlar to'g'rimi?".replace(",", " "),
        reply_markup=withdrawal_confirm_kb(),
    )


@profile_router.callback_query(WithdrawalStates.confirm, F.data == "withdraw_confirm")
async def withdraw_confirm(callback: CallbackQuery, db_user: User, state: FSMContext) -> None:
    data = await state.get_data()
    amount, card = data["amount"], data["card_number"]

    if amount > db_user.balance:
        await callback.answer("⚠️ Balansingiz yetarli emas.", show_alert=True)
        await state.clear()
        return

    await db.adjust_balance(db_user.user_id, -amount)
    withdrawal = await db.create_withdrawal(db_user.user_id, amount, card)
    await state.clear()

    await callback.message.edit_text(
        "✅ So'rovingiz qabul qilindi va admin tomonidan ko'rib chiqilmoqda.\n"
        f"Status: ⏳ Kutilmoqda\nSumma: {amount:,} so'm".replace(",", " "),
        reply_markup=back_to_main_kb(),
    )
    await callback.answer()

    if config.admin_chat_id:
        try:

            await callback.bot.send_message(
                config.admin_chat_id,
                "💳 <b>Yangi pul yechish so'rovi</b>\n\n"
                f"Foydalanuvchi: {db_user.full_name} (ID: {db_user.user_id})\n"
                f"Username: @{db_user.username or '—'}\n"
                f"Summa: {amount:,} so'm\n"
                f"Karta: {card}".replace(",", " "),
                reply_markup=withdrawal_item_kb(withdrawal.id),
            )
        except Exception:
            pass


@profile_router.callback_query(WithdrawalStates.confirm, F.data == "withdraw_cancel")
async def withdraw_cancel(callback: CallbackQuery, db_user: User, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(_profile_text(db_user), reply_markup=profile_menu_kb())
    await callback.answer("Bekor qilindi.")


# ============================================================================
# HANDLERS: USER - TEACHER PLATFORM (Ustoz Platformasi)
# (yangi funksional: ustoz kodi, ustoz paneli, maxsus duel, test banki, hisobot)
# ============================================================================
teacher_router = Router(name="teacher")

TEACHER_PLAN_LABEL = {
    TeacherPlan.NONE: "—",
    TeacherPlan.TRIAL: "🎁 15 kunlik bepul cheksiz",
    TeacherPlan.LIMITED: "🔹 Oddiy (oylik limit)",
    TeacherPlan.UNLIMITED_1M: "♾ 1 oy cheksiz",
    TeacherPlan.GOLD_3M: "👑 3 oylik GOLD",
}


async def _teacher_dashboard_text(user: User) -> str:
    total_tests = await db.count_teacher_tests(user.user_id)
    duels = await db.get_teacher_custom_duels(user.user_id, limit=1000)
    finished_duels = len([d for d in duels if d.status == CustomDuelStatus.FINISHED])

    plan_label = TEACHER_PLAN_LABEL.get(user.teacher_plan, "—")
    limit_line = ""
    if user.teacher_plan == TeacherPlan.LIMITED and not user.teacher_gifted_unlimited:
        limit_line = (
            f"\n📆 Oylik limit: {user.teacher_duels_used}/{config.teacher_monthly_duel_limit} duel, "
            f"{user.teacher_tests_used}/{config.teacher_monthly_test_limit} test"
        )
    elif user.teacher_plan_expires_at:
        limit_line = f"\n⏳ Tarif muddati: {user.teacher_plan_expires_at:%d.%m.%Y}"

    return (
        "🔐 <b>Ustoz Boshqaruv Markaziga Xush Kelibsiz!</b>\n\n"
        "Bu yerda siz o'quvchilar uchun maxsus testlar tuzishingiz, ikkita o'quvchi "
        "uchun yopiq 1v1 duellar (masalan, @Sherali va @Diyor uchun) tashkil "
        "qilishingiz mumkin.\n\n"
        f"💳 Tarif: <b>{plan_label}</b>{limit_line}\n\n"
        "📊 Statistikangiz:\n"
        f"• Jami kiritilgan testlar: {total_tests} ta\n"
        f"• O'tkazilgan maxsus duellar: {finished_duels} ta\n\n"
        "Kerakli bo'limni tanlang:"
    )


@teacher_router.callback_query(F.data == "menu_teacher")
async def cb_menu_teacher(callback: CallbackQuery, db_user: User) -> None:
    if db_user.is_teacher:
        db_user = await db.sync_teacher_plan_state(db_user.user_id) or db_user
        await callback.message.edit_text(await _teacher_dashboard_text(db_user), reply_markup=teacher_menu_kb(True))
    else:
        await callback.message.edit_text(
            "🎓 <b>Ustoz Platformasi</b>\n\n"
            "Bu bo'lim orqali siz o'quvchilaringiz uchun maxsus testlar va yopiq 1v1 "
            "duellar tashkil qilishingiz mumkin.\n\n"
            "Agar sizda ustoz kodi bo'lsa, uni faollashtiring 👇",
            reply_markup=teacher_menu_kb(False),
        )
    await callback.answer()


@teacher_router.callback_query(F.data == "teacher_enter_code")
async def cb_teacher_enter_code(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TeacherCodeRedeemStates.waiting_code)
    await callback.message.edit_text(
        "🔑 Ustoz kodini kiriting (masalan: 12345ASK):",
        reply_markup=back_to_teacher_kb(),
    )
    await callback.answer()


@teacher_router.message(TeacherCodeRedeemStates.waiting_code)
async def teacher_code_input(message: Message, state: FSMContext, db_user: User) -> None:
    code = (message.text or "").strip()
    ok, note = await db.redeem_teacher_code(code, db_user.user_id, config.teacher_code_expiry_days,
                                             config.teacher_trial_days)
    await state.clear()
    if not ok:
        await message.answer(note, reply_markup=back_to_teacher_kb())
        return
    await message.answer(
        f"{note}\n\n🎉 Endi sizda {config.teacher_trial_days} kunlik to'liq bepul cheksiz "
        "foydalanish imkoniyati bor (duel va test yuklashda limit yo'q)!",
    )
    fresh_user = await db.get_user(db_user.user_id)
    await message.answer(await _teacher_dashboard_text(fresh_user), reply_markup=teacher_menu_kb(True))


# --------------------------------------------------------------------------- #
# Teacher: add test to own bank
# --------------------------------------------------------------------------- #


@teacher_router.callback_query(F.data == "teacher_add_test")
async def cb_teacher_add_test(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    db_user = await db.sync_teacher_plan_state(db_user.user_id) or db_user
    if not teacher_can_add_test(db_user):
        await callback.answer(
            "⚠️ Oylik test yuklash limitingiz tugadi. Tarifni yangilash uchun panelga o'ting.",
            show_alert=True,
        )
        await callback.message.edit_text(
            "⚠️ Oylik test yuklash limitingiz (80 ta) tugadi.\n\nTarifni yangilang:",
            reply_markup=teacher_plan_purchase_kb(),
        )
        return
    await state.set_state(TeacherAddTestStates.waiting_test_text)
    await callback.message.edit_text(
        "➕ <b>Yangi Test Qo'shish</b>\n\n"
        "Quyidagi formatda savolni yuboring:\n\n"
        "<code>Savol matni?\nA) variant\nB) variant\nC) variant\nD) variant\n"
        "/togrijavob Javob: B</code>",
        reply_markup=back_to_teacher_kb(),
    )
    await callback.answer()


@teacher_router.message(TeacherAddTestStates.waiting_test_text)
async def teacher_test_text_input(message: Message, state: FSMContext, db_user: User) -> None:
    parsed = parse_questions_from_text((message.text or "") + "\n")
    if not parsed:
        await message.answer(
            "⚠️ Format noto'g'ri. Qaytadan urinib ko'ring:\n\n"
            "<code>Savol matni?\nA) variant\nB) variant\nC) variant\nD) variant\n"
            "/togrijavob Javob: B</code>"
        )
        return

    db_user = await db.sync_teacher_plan_state(db_user.user_id) or db_user
    if not teacher_can_add_test(db_user):
        await state.clear()
        await message.answer("⚠️ Oylik test yuklash limitingiz tugadi.", reply_markup=teacher_plan_purchase_kb())
        return

    for q in parsed:
        await db.add_teacher_test(db_user.user_id, q)
    await db.increment_teacher_test_usage(db_user.user_id, count=len(parsed))
    await state.clear()
    await message.answer(f"✅ {len(parsed)} ta test bankingizga qo'shildi!", reply_markup=back_to_teacher_kb())


# --------------------------------------------------------------------------- #
# Teacher: my tests (list / edit / delete)
# --------------------------------------------------------------------------- #


@teacher_router.callback_query(F.data == "teacher_my_tests")
async def cb_teacher_my_tests(callback: CallbackQuery, db_user: User) -> None:
    tests = await db.get_teacher_tests(db_user.user_id, limit=20)
    if not tests:
        await callback.message.edit_text("📋 Sizda hali testlar yo'q.", reply_markup=back_to_teacher_kb())
        await callback.answer()
        return
    await callback.message.edit_text(f"📋 <b>So'nggi {len(tests)} ta testingiz</b>", reply_markup=back_to_teacher_kb())
    for t in tests:
        text = f"❓ {t.question[:150]}\n✅ To'g'ri: {t.correct_option}"
        await callback.message.answer(text, reply_markup=teacher_test_item_kb(t.id))
    await callback.answer()


@teacher_router.callback_query(F.data.startswith("tt_del_"))
async def cb_teacher_test_delete(callback: CallbackQuery, db_user: User) -> None:
    test_id = int(callback.data.removeprefix("tt_del_"))
    ok = await db.delete_teacher_test(test_id, db_user.user_id)
    await callback.answer("🗑 O'chirildi." if ok else "⚠️ Topilmadi.")
    if ok:
        try:
            await callback.message.edit_text(callback.message.text + "\n\n🗑 <b>O'chirildi</b>")
        except Exception:
            pass


@teacher_router.callback_query(F.data.startswith("tt_edit_"))
async def cb_teacher_test_edit(callback: CallbackQuery, state: FSMContext) -> None:
    test_id = int(callback.data.removeprefix("tt_edit_"))
    await state.set_state(TeacherEditTestStates.waiting_new_text)
    await state.update_data(test_id=test_id)
    await callback.message.answer(
        "✏️ Yangi matnni quyidagi formatda yuboring:\n\n"
        "<code>Savol matni?\nA) variant\nB) variant\nC) variant\nD) variant\n"
        "/togrijavob Javob: B</code>"
    )
    await callback.answer()


@teacher_router.message(TeacherEditTestStates.waiting_new_text)
async def teacher_test_edit_input(message: Message, state: FSMContext, db_user: User) -> None:
    data = await state.get_data()
    test_id = data["test_id"]
    parsed = parse_questions_from_text((message.text or "") + "\n")
    await state.clear()
    if not parsed:
        await message.answer("⚠️ Format noto'g'ri, tahrirlash bekor qilindi.")
        return
    q = parsed[0]
    ok = await db.update_teacher_test(
        test_id, db_user.user_id,
        question=q["question"], option_a=q["option_a"], option_b=q["option_b"],
        option_c=q["option_c"], option_d=q["option_d"], correct_option=q["correct_option"],
    )
    await message.answer("✅ Test yangilandi!" if ok else "⚠️ Test topilmadi.", reply_markup=back_to_teacher_kb())


# --------------------------------------------------------------------------- #
# Teacher: create custom duel for 2 students
# --------------------------------------------------------------------------- #


@teacher_router.callback_query(F.data == "teacher_create_duel")
async def cb_teacher_create_duel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    db_user = await db.sync_teacher_plan_state(db_user.user_id) or db_user
    if not teacher_can_create_duel(db_user):
        await callback.message.edit_text(
            "⚠️ Oylik maxsus duel limitingiz (8 ta) tugadi.\n\nTarifni yangilang:",
            reply_markup=teacher_plan_purchase_kb(),
        )
        await callback.answer()
        return
    bank_count = await db.count_teacher_tests(db_user.user_id)
    if bank_count < 3:
        await callback.answer("⚠️ Avval test bankingizga kamida bir nechta test qo'shing.", show_alert=True)
        return
    await state.set_state(CustomDuelCreateStates.waiting_player1)
    await callback.message.edit_text(
        "✍️ Maxsus Duel Yaratish. Duelda qatnashuvchi 1-o'quvchining username'ini "
        "yuboring (Masalan: @sherali):",
        reply_markup=back_to_teacher_kb(),
    )
    await callback.answer()


@teacher_router.message(CustomDuelCreateStates.waiting_player1)
async def cd_player1_input(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip().lstrip("@").lower()
    if not username:
        await message.answer("⚠️ Iltimos, to'g'ri username kiriting.")
        return
    await state.update_data(player1_username=username)
    await state.set_state(CustomDuelCreateStates.waiting_player2)
    await message.answer(
        f"👤 1-o'quvchi: @{username} qabul qilindi. Endi 2-o'quvchining username'ini yuboring "
        "(Masalan: @diyor):"
    )


@teacher_router.message(CustomDuelCreateStates.waiting_player2)
async def cd_player2_input(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip().lstrip("@").lower()
    data = await state.get_data()
    if not username:
        await message.answer("⚠️ Iltimos, to'g'ri username kiriting.")
        return
    if username == data.get("player1_username"):
        await message.answer("⚠️ Ikkala o'quvchi bir xil bo'lishi mumkin emas. 2-o'quvchi username'ini qayta kiriting:")
        return
    await state.update_data(player2_username=username, num_questions=config.custom_duel_default_questions,
                             time_per_question=config.custom_duel_default_time)
    await state.set_state(CustomDuelCreateStates.waiting_params_confirm)
    await message.answer(
        "⚙️ Savollar sonini tanlang:", reply_markup=teacher_question_count_kb(),
    )


@teacher_router.callback_query(CustomDuelCreateStates.waiting_params_confirm, F.data.startswith("cd_qcount_"))
async def cd_qcount_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    n = int(callback.data.removeprefix("cd_qcount_"))
    await state.update_data(num_questions=n)
    await callback.message.edit_text(f"✅ Savollar soni: {n} ta\n\n⏱ Har bir savol vaqtini tanlang:",
                                      reply_markup=teacher_time_choice_kb())
    await callback.answer()


@teacher_router.callback_query(CustomDuelCreateStates.waiting_params_confirm, F.data.startswith("cd_time_"))
async def cd_time_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    t = int(callback.data.removeprefix("cd_time_"))
    await state.update_data(time_per_question=t)
    data = await state.get_data()
    await callback.message.edit_text(
        f"⚙️ Parametrlarni tanlang: Savollar soni: {data['num_questions']} ta, "
        f"Har bir savol vaqti: {t} soniya\n\n"
        f"👥 O'quvchilar: @{data['player1_username']} va @{data['player2_username']}",
        reply_markup=custom_duel_confirm_kb(),
    )
    await callback.answer()


@teacher_router.callback_query(CustomDuelCreateStates.waiting_params_confirm, F.data == "cd_cancel")
async def cd_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await callback.message.edit_text(await _teacher_dashboard_text(db_user), reply_markup=teacher_menu_kb(True))
    await callback.answer("Bekor qilindi.")


@teacher_router.callback_query(CustomDuelCreateStates.waiting_params_confirm, F.data == "cd_confirm")
async def cd_confirm(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    data = await state.get_data()
    await state.clear()

    cd = await db.create_custom_duel(
        db_user.user_id, data["player1_username"], data["player2_username"],
        data["num_questions"], data["time_per_question"],
    )
    await db.increment_teacher_duel_usage(db_user.user_id)

    join_link = f"https://t.me/{config.bot_username}?start=tduel_{cd.token}"
    watch_link = f"https://t.me/{config.bot_username}?start=watch_{cd.watch_token}"

    await callback.message.edit_text(
        "✅ <b>Maxsus Duel Muvaffaqiyatli Yaratildi!</b>\n\n"
        f"👥 Ishtirokchilar: @{cd.player1_username} va @{cd.player2_username}\n"
        f"📝 Savollar soni: {cd.num_questions} ta | ⏱ Vaqt: {cd.time_per_question} soniya\n"
        f"🔗 O'quvchilar uchun taklif havolasi:\n{join_link}\n\n"
        f"👀 Jonli kuzatish havolasi (istalgan kishi tomosha qila oladi):\n{watch_link}",
        reply_markup=back_to_teacher_kb(),
    )
    await callback.answer()

    task = asyncio.create_task(_custom_duel_join_reminder(callback.bot, cd.id))
    join_reminder_tasks[cd.id] = task


async def _custom_duel_join_reminder(bot: Bot, custom_duel_id: int) -> None:
    await asyncio.sleep(config.custom_duel_join_reminder_minutes * 60)
    cd = await db.get_custom_duel(custom_duel_id)
    if not cd or cd.status != CustomDuelStatus.WAITING_PLAYERS:
        return
    missing = []
    if not cd.player1_id:
        missing.append(f"@{cd.player1_username}")
    if not cd.player2_id:
        missing.append(f"@{cd.player2_username}")
    if missing:
        try:
            await bot.send_message(
                cd.teacher_id,
                f"⏰ Eslatma: {', '.join(missing)} hali maxsus duel havolasini bosmadi (#{cd.id}).",
            )
        except Exception:
            pass
    join_reminder_tasks.pop(custom_duel_id, None)


# --------------------------------------------------------------------------- #
# Teacher: student results / report
# --------------------------------------------------------------------------- #


@teacher_router.callback_query(F.data == "teacher_results")
async def cb_teacher_results(callback: CallbackQuery, db_user: User) -> None:
    await callback.message.edit_text(
        "📊 <b>O'quvchilar Natijalari</b>\n\nQuyidagilardan birini tanlang:",
        reply_markup=teacher_results_kb(),
    )
    await callback.answer()


def _format_duel_line(d: "CustomDuel") -> str:
    winner = "Durrang"
    if d.winner_id == d.player1_id:
        winner = f"@{d.player1_username}"
    elif d.winner_id == d.player2_id:
        winner = f"@{d.player2_username}"
    date_str = f"{d.finished_at:%d.%m.%Y %H:%M}" if d.finished_at else "-"
    return (
        f"#{d.id} | {date_str} | @{d.player1_username} {d.player1_score} - "
        f"{d.player2_score} @{d.player2_username} | G'olib: {winner}"
    )


@teacher_router.callback_query(F.data == "tr_all_text")
async def cb_teacher_results_all(callback: CallbackQuery, db_user: User) -> None:
    duels = await db.get_teacher_custom_duels_by_date(db_user.user_id, None, None)
    if not duels:
        await callback.answer("Hozircha yakunlangan dueller yo'q.", show_alert=True)
        return
    lines = ["📊 <b>Barcha natijalar</b>\n"] + [_format_duel_line(d) for d in duels[:50]]
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@teacher_router.callback_query(F.data == "tr_filter_date")
async def cb_teacher_results_filter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TeacherResultsStates.waiting_date_filter)
    await callback.message.edit_text(
        "📅 Sanani kiriting (KUN.OY.YIL formatida, masalan: 01.09.2026):",
        reply_markup=back_to_teacher_kb(),
    )
    await callback.answer()


@teacher_router.message(TeacherResultsStates.waiting_date_filter)
async def teacher_results_date_input(message: Message, state: FSMContext, db_user: User) -> None:
    raw = (message.text or "").strip()
    try:
        day = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await message.answer("⚠️ Format noto'g'ri. Masalan: 01.09.2026")
        return
    await state.clear()
    date_from = day
    date_to = day + timedelta(days=1)
    duels = await db.get_teacher_custom_duels_by_date(db_user.user_id, date_from, date_to)
    if not duels:
        await message.answer(f"📅 {raw} kuni uchun natijalar topilmadi.", reply_markup=back_to_teacher_kb())
        return
    lines = [f"📊 <b>{raw} kuni natijalari</b>\n"] + [_format_duel_line(d) for d in duels]
    await message.answer("\n".join(lines), reply_markup=back_to_teacher_kb())


@teacher_router.callback_query(F.data == "tr_export_csv")
async def cb_teacher_results_csv(callback: CallbackQuery, db_user: User) -> None:
    duels = await db.get_teacher_custom_duels_by_date(db_user.user_id, None, None)
    if not duels:
        await callback.answer("Hozircha yakunlangan dueller yo'q.", show_alert=True)
        return

    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Sana", "O'quvchi 1", "Ball 1", "O'quvchi 2", "Ball 2", "G'olib"])
    for d in duels:
        winner = "Durrang"
        if d.winner_id == d.player1_id:
            winner = d.player1_username
        elif d.winner_id == d.player2_id:
            winner = d.player2_username
        writer.writerow([
            d.id, d.finished_at.strftime("%d.%m.%Y %H:%M") if d.finished_at else "",
            d.player1_username, d.player1_score, d.player2_username, d.player2_score, winner,
        ])

    from aiogram.types import BufferedInputFile
    file_bytes = buf.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename="natijalar.csv"),
        caption="📊 O'quvchilar natijalari (CSV)",
    )
    await callback.answer()


# --------------------------------------------------------------------------- #
# Teacher: plan purchase (admin-approval flow)
# --------------------------------------------------------------------------- #


@teacher_router.callback_query(F.data == "teacher_view_plans")
async def cb_teacher_view_plans(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "💳 <b>Ustoz Tariflari</b>\n\n"
        f"♾ 1 oy cheksiz foydalanish — {config.teacher_unlimited_1m_price:,} so'm\n"
        f"👑 3 oylik GOLD obuna — {config.teacher_gold_3m_price:,} so'm\n\n"
        "Tanlang, so'rovingiz admin tomonidan ko'rib chiqiladi:".replace(",", " "),
        reply_markup=teacher_plan_purchase_kb(),
    )
    await callback.answer()


@teacher_router.callback_query(F.data.in_({"tp_buy_unlimited_1m", "tp_buy_gold_3m"}))
async def cb_teacher_buy_plan(callback: CallbackQuery, db_user: User) -> None:
    if callback.data == "tp_buy_unlimited_1m":
        plan, price = TeacherPlan.UNLIMITED_1M, config.teacher_unlimited_1m_price
        label = "1 oy cheksiz"
    else:
        plan, price = TeacherPlan.GOLD_3M, config.teacher_gold_3m_price
        label = "3 oylik GOLD"

    purchase = await db.create_teacher_purchase(db_user.user_id, plan, price)
    await callback.answer("✅ So'rovingiz adminga yuborildi!")
    await callback.message.edit_text(
        f"✅ <b>{label}</b> tarifi uchun so'rovingiz qabul qilindi va admin tomonidan "
        "ko'rib chiqilmoqda. Tasdiqlangach avtomatik faollashadi.",
        reply_markup=back_to_teacher_kb(),
    )

    if config.admin_chat_id:
        try:
            await callback.bot.send_message(
                config.admin_chat_id,
                "🎓 <b>Yangi ustoz tarif so'rovi</b>\n\n"
                f"Foydalanuvchi: {db_user.full_name} (ID: {db_user.user_id})\n"
                f"Username: @{db_user.username or '—'}\n"
                f"Tarif: {label}\nNarxi: {price:,} so'm".replace(",", " "),
                reply_markup=teacher_purchase_item_kb(purchase.id),
            )
        except Exception:
            pass


# ============================================================================
# HANDLERS: ADMIN - PANEL
# (from: bot/handlers/admin/panel.py)
# ============================================================================
admin_panel_router = Router(name="admin_panel")

DASHBOARD_TEXT = "🛠 <b>Admin Panel</b>\n\nBoshqarish uchun bo'limni tanlang:"


@admin_panel_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(DASHBOARD_TEXT, reply_markup=admin_dashboard_kb())


@admin_panel_router.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(DASHBOARD_TEXT, reply_markup=admin_dashboard_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: ADMIN - STATS
# (from: bot/handlers/admin/stats.py)
# ============================================================================
admin_stats_router = Router(name="admin_stats")


@admin_stats_router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    s = await db.get_stats()
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: {s['total_users']}\n"
        f"🆕 Bugungi yangi foydalanuvchilar: {s['new_today']}\n"
        f"⚔️ Faol dueller: {s['active_duels']}\n"
        f"📚 Bazadagi testlar: {s['total_tests']}\n"
        f"💰 To'langan mablag': {s['total_paid']:,} so'm".replace(",", " ")
    )
    await callback.message.edit_text(text, reply_markup=back_to_admin_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: ADMIN - BROADCAST
# (from: bot/handlers/admin/broadcast.py)
# ============================================================================
admin_broadcast_router = Router(name="admin_broadcast")


@admin_broadcast_router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_content)
    await callback.message.edit_text(
        "📢 <b>Xabar Tarqatish</b>\n\n"
        "Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring "
        "(matn, rasm, video - bari qo'llab-quvvatlanadi):",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@admin_broadcast_router.message(BroadcastStates.waiting_content)
async def broadcast_content_received(message: Message, state: FSMContext) -> None:
    await state.update_data(source_chat_id=message.chat.id, source_message_id=message.message_id)
    await state.set_state(BroadcastStates.confirm)
    await message.answer(
        "⬆️ Ushbu xabar barcha foydalanuvchilarga yuboriladi. Tasdiqlaysizmi?",
        reply_markup=broadcast_confirm_kb(),
    )


@admin_broadcast_router.callback_query(BroadcastStates.confirm, F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Bekor qilindi.", reply_markup=back_to_admin_kb())
    await callback.answer()


@admin_broadcast_router.callback_query(BroadcastStates.confirm, F.data == "broadcast_send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("🚀 Xabar yuborilmoqda...")

    user_ids = await db.get_all_active_user_ids()
    sent, blocked, failed = 0, 0, 0

    for user_id in user_ids:
        try:
            await callback.bot.copy_message(
                chat_id=user_id,
                from_chat_id=data["source_chat_id"],
                message_id=data["source_message_id"],
            )
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await callback.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=data["source_chat_id"],
                    message_id=data["source_message_id"],
                )
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            # user blocked the bot - mark inactive so future broadcasts skip them
            await db.mark_inactive(user_id)
            blocked += 1
        except TelegramBadRequest:
            failed += 1
        await asyncio.sleep(0.05)  # gentle rate limiting

    await callback.message.answer(
        "✅ <b>Xabar tarqatish yakunlandi</b>\n\n"
        f"Yuborildi: {sent}\nBloklangan: {blocked}\nXatolik: {failed}",
        reply_markup=back_to_admin_kb(),
    )


# ============================================================================
# HANDLERS: ADMIN - CHANNEL GATE (bir nechta ochiq/yopiq kanal)
# (from: bot/handlers/admin/channel_gate.py)
# ============================================================================
admin_channel_gate_router = Router(name="admin_channel_gate")


async def _channels_overview_text() -> tuple[str, list[MandatoryChannel]]:
    channels = await db.get_mandatory_channels()
    lines = ["🔗 <b>Majburiy Kanallar</b>\n"]
    if not channels:
        lines.append("Hozircha majburiy kanal o'rnatilmagan.")
    else:
        for ch in channels:
            kind_label = "🔓 Ochiq" if ch.kind == ChannelKind.OPEN else "🔒 Yopiq/maxfiy"
            lines.append(f"• {kind_label}: <b>{ch.title or ch.value}</b> ({ch.value})")
    lines.append(
        "\nBotni har bir kanalga admin qilib qo'yishni unutmang! Yopiq kanal uchun "
        "kanal ID raqamini kiriting (masalan: -1001234567890)."
    )
    return "\n".join(lines), channels


@admin_channel_gate_router.callback_query(F.data == "admin_channel_gate")
async def cb_admin_channel_gate(callback: CallbackQuery) -> None:
    text, channels = await _channels_overview_text()
    await callback.message.edit_text(text, reply_markup=admin_channels_list_kb(channels))
    await callback.answer()


@admin_channel_gate_router.callback_query(F.data == "mc_add_open")
async def cb_mc_add_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MandatoryChannelsStates.waiting_open_username)
    await callback.message.edit_text(
        "✏️ Ochiq kanal username'ini kiriting (masalan: @bellashuvuz):",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@admin_channel_gate_router.message(MandatoryChannelsStates.waiting_open_username)
async def mc_open_username_input(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username.startswith("@"):
        username = f"@{username}"

    try:
        chat = await message.bot.get_chat(username)
        member = await message.bot.get_chat_member(chat.id, message.bot.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ Bot ushbu kanalda admin emas! Iltimos, avval botni kanalga admin qiling, "
                "keyin qayta urinib ko'ring."
            )
            return
    except Exception:
        await message.answer("⚠️ Kanal topilmadi yoki bot unga a'zo emas. Username'ni tekshirib qayta yuboring.")
        return

    invite_link = None
    try:
        link_obj = await message.bot.create_chat_invite_link(chat_id=chat.id)
        invite_link = link_obj.invite_link
    except Exception:
        pass  # invite link yaratib bo'lmadi, ochiq kanal uchun @username orqali havola ishlatiladi

    await db.add_mandatory_channel(ChannelKind.OPEN, username, chat.title, invite_link, message.from_user.id)
    await state.clear()
    text, channels = await _channels_overview_text()
    await message.answer(f"✅ Ochiq kanal qo'shildi: {username}")
    await message.answer(text, reply_markup=admin_channels_list_kb(channels))


@admin_channel_gate_router.callback_query(F.data == "mc_add_closed")
async def cb_mc_add_closed(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MandatoryChannelsStates.waiting_closed_id)
    await callback.message.edit_text(
        "✏️ Yopiq/maxfiy kanal ID raqamini kiriting (masalan: -1001234567890):",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@admin_channel_gate_router.message(MandatoryChannelsStates.waiting_closed_id)
async def mc_closed_id_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer("⚠️ Iltimos, faqat kanal ID raqamini kiriting (masalan: -1001234567890).")
        return

    title = None
    invite_link = None
    try:
        chat = await message.bot.get_chat(chat_id)
        title = chat.title
        member = await message.bot.get_chat_member(chat_id, message.bot.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ Bot ushbu kanalda admin emas! Iltimos, avval botni kanalga admin qiling, "
                "keyin qayta urinib ko'ring."
            )
            return
        try:
            link_obj = await message.bot.create_chat_invite_link(chat_id=chat_id)
            invite_link = link_obj.invite_link
        except Exception:
            pass
    except Exception:
        await message.answer(
            "⚠️ Bot ushbu kanalni topa olmadi (lekin baribir qo'shishingiz mumkin — "
            "tekshiruv 'fail-open' rejimida ishlaydi, ya'ni xato bo'lsa foydalanuvchi bloklanmaydi)."
        )

    await db.add_mandatory_channel(ChannelKind.CLOSED, str(chat_id), title, invite_link, message.from_user.id)
    await state.clear()
    text, channels = await _channels_overview_text()
    await message.answer(f"✅ Yopiq kanal qo'shildi: {title or chat_id}")
    await message.answer(text, reply_markup=admin_channels_list_kb(channels))


@admin_channel_gate_router.callback_query(F.data.startswith("mc_remove_"))
async def cb_mc_remove(callback: CallbackQuery) -> None:
    channel_id = int(callback.data.removeprefix("mc_remove_"))
    await db.remove_mandatory_channel(channel_id)
    await callback.answer("🗑 Kanal o'chirildi.")
    text, channels = await _channels_overview_text()
    await callback.message.edit_text(text, reply_markup=admin_channels_list_kb(channels))


# ============================================================================
# HANDLERS: ADMIN - WITHDRAWALS
# (from: bot/handlers/admin/withdrawals.py)
# ============================================================================
admin_withdrawals_router = Router(name="admin_withdrawals")


@admin_withdrawals_router.callback_query(F.data == "admin_withdrawals")
async def cb_admin_withdrawals(callback: CallbackQuery) -> None:
    pending = await db.get_pending_withdrawals()
    if not pending:
        await callback.message.edit_text("💳 Kutilayotgan pul yechish so'rovlari yo'q.", reply_markup=back_to_admin_kb())
        await callback.answer()
        return

    await callback.message.edit_text(f"💳 <b>{len(pending)} ta kutilayotgan so'rov</b>", reply_markup=back_to_admin_kb())
    for w in pending:
        user = await db.get_user(w.user_id)
        name = user.full_name if user else "Noma'lum"
        username = f"@{user.username}" if user and user.username else "—"
        text = (
            f"💳 So'rov #{w.id}\n\n"
            f"Foydalanuvchi: {name} ({username})\n"
            f"ID: {w.user_id}\n"
            f"Summa: {w.amount:,} so'm\n"
            f"Karta: {w.card_number}".replace(",", " ")
        )
        await callback.message.answer(text, reply_markup=withdrawal_item_kb(w.id))
    await callback.answer()


@admin_withdrawals_router.callback_query(F.data.startswith("wd_approve_"))
async def cb_wd_approve(callback: CallbackQuery) -> None:
    withdrawal_id = int(callback.data.removeprefix("wd_approve_"))
    w = await db.resolve_withdrawal(withdrawal_id, approved=True)
    if not w:
        await callback.answer("So'rov topilmadi.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n✅ <b>To'landi</b>",
        )
    except Exception:
        pass
    await callback.answer("✅ To'lov tasdiqlandi.")
    try:
        await callback.bot.send_message(
            w.user_id,
            f"✅ Sizning {w.amount:,} so'mlik pul yechish so'rovingiz to'landi!".replace(",", " "),
        )
    except Exception:
        pass


@admin_withdrawals_router.callback_query(F.data.startswith("wd_reject_"))
async def cb_wd_reject(callback: CallbackQuery, state: FSMContext) -> None:
    withdrawal_id = int(callback.data.removeprefix("wd_reject_"))
    await state.set_state(WithdrawalResolveStates.waiting_reject_reason)
    await state.update_data(withdrawal_id=withdrawal_id)
    await callback.message.answer("❌ Rad etish sababini kiriting (foydalanuvchiga yuboriladi):")
    await callback.answer()


@admin_withdrawals_router.message(WithdrawalResolveStates.waiting_reject_reason)
async def wd_reject_reason_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    withdrawal_id = data["withdrawal_id"]
    reason = message.text or "Sabab ko'rsatilmagan"

    w = await db.resolve_withdrawal(withdrawal_id, approved=False, admin_note=reason)
    await state.clear()
    if not w:
        await message.answer("So'rov topilmadi.")
        return

    await message.answer(f"❌ So'rov #{withdrawal_id} rad etildi va mablag' foydalanuvchiga qaytarildi.")
    try:
        await message.bot.send_message(
            w.user_id,
            f"❌ Sizning {w.amount:,} so'mlik pul yechish so'rovingiz rad etildi.\n"
            f"Sabab: {reason}\n\nMablag' balansingizga qaytarildi.".replace(",", " "),
        )
    except Exception:
        pass


# ============================================================================
# HANDLERS: ADMIN - TEST PARSER (channel Q&A ingestion)
# (from: bot/handlers/admin/test_parser.py)
# ============================================================================
admin_test_parser_router = Router(name="admin_test_parser")

# In-memory buffer of raw text posted to the source channel since the last
# confirmed batch. Keyed by channel_id in case of multiple source channels.
_channel_buffer: dict[int, list[str]] = {}

QUESTION_BLOCK_RE = re.compile(
    r"(?P<question>.+?)\s*\n"
    r"A\)\s*(?P<a>.+?)\s*\n"
    r"B\)\s*(?P<b>.+?)\s*\n"
    r"C\)\s*(?P<c>.+?)\s*\n"
    r"D\)\s*(?P<d>.+?)\s*\n"
    r"/togrijavob\s+Javob:\s*(?P<correct>[A-Da-d])",
    re.MULTILINE,
)


def parse_questions_from_text(text: str) -> list[dict]:
    """Extract every well-formed question block from raw channel text."""
    results = []
    for m in QUESTION_BLOCK_RE.finditer(text):
        results.append(
            {
                "question": m.group("question").strip(),
                "option_a": m.group("a").strip(),
                "option_b": m.group("b").strip(),
                "option_c": m.group("c").strip(),
                "option_d": m.group("d").strip(),
                "correct_option": m.group("correct").upper(),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# Channel post buffering (NOT behind admin auth - channel posts have no
# from_user to check against ADMIN_IDS; access is implicitly restricted by
# who can post in the private source channel itself).
# --------------------------------------------------------------------------- #

test_channel_buffer_router = Router(name="test_channel_buffer")


@test_channel_buffer_router.channel_post(F.chat.id == config.test_source_channel_id, F.text)
async def on_channel_post(message: Message) -> None:
    if message.text.strip().startswith("/testnibazagaqoshibber"):
        return  # trigger command itself, not question content
    _channel_buffer.setdefault(message.chat.id, []).append(message.text)


# --------------------------------------------------------------------------- #
# Admin-facing flow
# --------------------------------------------------------------------------- #


def _menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Aniqlash va Tasdiqlash", callback_data="tests_parse_buffer")
    b.button(text="🗑 Buferni tozalash", callback_data="tests_clear_buffer")
    b.button(text="🔙 Admin Panel", callback_data="admin_main")
    b.adjust(1)
    return b.as_markup()


@admin_test_parser_router.callback_query(F.data == "admin_tests")
async def cb_admin_tests(callback: CallbackQuery) -> None:
    buffered = len(_channel_buffer.get(config.test_source_channel_id, []))
    total = await db.count_tests()
    text = (
        "📥 <b>Kanal Testlarini Boshqarish</b>\n\n"
        f"Bazadagi jami testlar: {total}\n"
        f"Kanaldan kutilayotgan (tasdiqlanmagan) postlar: {buffered}\n\n"
        "Format:\n<code>Savol matni?\nA) variant\nB) variant\nC) variant\nD) variant\n"
        "/togrijavob Javob: B</code>"
    )
    await callback.message.edit_text(text, reply_markup=_menu_kb())
    await callback.answer()


@admin_test_parser_router.callback_query(F.data == "tests_clear_buffer")
async def cb_tests_clear_buffer(callback: CallbackQuery) -> None:
    _channel_buffer.pop(config.test_source_channel_id, None)
    await callback.answer("🗑 Bufer tozalandi.")
    await cb_admin_tests(callback)


async def _run_parse_and_preview(message_target: Message, state: FSMContext) -> None:
    raw_texts = _channel_buffer.get(config.test_source_channel_id, [])
    full_text = "\n\n".join(raw_texts)
    parsed = parse_questions_from_text(full_text)

    if not parsed:
        await message_target.answer(
            "⚠️ Hech qanday to'g'ri formatdagi savol topilmadi. Formatni tekshiring.",
            reply_markup=back_to_admin_kb(),
        )
        return

    await state.update_data(parsed_tests=parsed)
    await state.set_state(TestParserStates.waiting_confirmation)

    preview_lines = [f"📥 <b>{len(parsed)} ta test aniqlandi</b>\n"]
    for i, q in enumerate(parsed[:5], start=1):
        preview_lines.append(f"{i}. {q['question'][:80]} (✅ {q['correct_option']})")
    if len(parsed) > 5:
        preview_lines.append(f"... va yana {len(parsed) - 5} ta")
    preview_lines.append("\nBazaga saqlashni tasdiqlaysizmi?")

    await message_target.answer("\n".join(preview_lines), reply_markup=test_batch_confirm_kb())


@admin_test_parser_router.callback_query(F.data == "tests_parse_buffer")
async def cb_tests_parse_buffer(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _run_parse_and_preview(callback.message, state)


@admin_test_parser_router.message(Command("testnibazagaqoshibber"))
async def cmd_testnibazagaqoshibber(message: Message, state: FSMContext) -> None:
    await _run_parse_and_preview(message, state)


@admin_test_parser_router.callback_query(TestParserStates.waiting_confirmation, F.data == "test_confirm_save")
async def cb_test_confirm_save(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parsed = data.get("parsed_tests", [])
    saved = await db.add_tests_bulk(parsed, added_by=callback.from_user.id)
    _channel_buffer.pop(config.test_source_channel_id, None)
    await state.clear()
    await callback.message.edit_text(f"✅ {saved} ta test bazaga muvaffaqiyatli qo'shildi!", reply_markup=back_to_admin_kb())
    await callback.answer()


@admin_test_parser_router.callback_query(TestParserStates.waiting_confirmation, F.data == "test_confirm_cancel")
async def cb_test_confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Bekor qilindi. Bufer saqlanib qoldi.", reply_markup=back_to_admin_kb())
    await callback.answer()


# ============================================================================
# HANDLERS: ADMIN - GIFT SUBSCRIPTION
# (from: bot/handlers/admin/gift_subscription.py)
# ============================================================================
admin_gift_router = Router(name="admin_gift")


@admin_gift_router.callback_query(F.data == "admin_gift")
async def cb_admin_gift(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GiftSubscriptionStates.waiting_user_identifier)
    await callback.message.edit_text(
        "🎁 <b>Obuna Sovg'a Qilish</b>\n\nFoydalanuvchi ID yoki username'ini kiriting:",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@admin_gift_router.message(GiftSubscriptionStates.waiting_user_identifier)
async def gift_user_identifier_input(message: Message, state: FSMContext) -> None:
    identifier = (message.text or "").strip()
    user = await db.find_user_by_id_or_username(identifier)
    if not user:
        await message.answer("⚠️ Foydalanuvchi topilmadi. Qaytadan kiriting yoki /admin bilan bekor qiling.")
        return

    await state.update_data(target_user_id=user.user_id, target_name=user.full_name)
    await state.set_state(GiftSubscriptionStates.waiting_status_choice)
    await message.answer(
        f"👤 {user.full_name} (ID: {user.user_id}) topildi.\n\nQaysi statusni sovg'a qilmoqchisiz?",
        reply_markup=gift_status_choice_kb(),
    )


@admin_gift_router.callback_query(GiftSubscriptionStates.waiting_status_choice, F.data.in_({"gift_status_mini", "gift_status_pro"}))
async def gift_status_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    target_user_id = data["target_user_id"]
    target_name = data["target_name"]

    status = UserStatus.MINI if callback.data == "gift_status_mini" else UserStatus.PRO
    label = "✨ Mini Status" if status == UserStatus.MINI else "👑 Pro Gamer"

    await db.set_status(target_user_id, status, days=30)
    await state.clear()

    await callback.message.edit_text(
        f"✅ {target_name} (ID: {target_user_id}) foydalanuvchisiga {label} muvaffaqiyatli berildi!",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()

    try:
        await callback.bot.send_message(
            target_user_id,
            f"🎉 Tabriklaymiz! Sizga administrator tomonidan <b>{label}</b> obunasi sovg'a qilindi!",
        )
    except Exception:
        pass


# ============================================================================
# HANDLERS: ADMIN - TEACHER CODE GENERATION
# ============================================================================
admin_teacher_code_router = Router(name="admin_teacher_code")


@admin_teacher_code_router.callback_query(F.data == "admin_teacher_code")
async def cb_admin_teacher_code(callback: CallbackQuery) -> None:
    tc = await db.create_teacher_code(callback.from_user.id, config.teacher_code_length)
    await callback.message.edit_text(
        "🎓 <b>Yangi Ustoz Kodi Yaratildi</b>\n\n"
        f"Kod: <code>{tc.code}</code>\n\n"
        f"Bu kod {config.teacher_code_expiry_days} kun ichida ishlatilmasa, avtomatik eskiradi. "
        "Kodni ustozga yuboring — u \"🎓 Ustoz Platformasi\" bo'limidan uni faollashtiradi.",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


# ============================================================================
# HANDLERS: ADMIN - TEACHER MANAGEMENT (gift unlimited/limits)
# ============================================================================
admin_teacher_manage_router = Router(name="admin_teacher_manage")


@admin_teacher_manage_router.callback_query(F.data == "admin_teacher_manage")
async def cb_admin_teacher_manage(callback: CallbackQuery, state: FSMContext) -> None:
    revenue = await db.get_teacher_stats_revenue()
    lines = ["🧑‍🏫 <b>Ustozlarni Boshqarish</b>\n", "💰 <b>Ustoz tariflaridan tushum:</b>"]
    if revenue["by_plan"]:
        for plan_key, info in revenue["by_plan"].items():
            label = TEACHER_PLAN_LABEL.get(TeacherPlan(plan_key), plan_key)
            lines.append(f"• {label}: {info['count']} marta — {info['total']:,} so'm".replace(",", " "))
        lines.append(f"\n<b>Jami: {revenue['total']:,} so'm</b>".replace(",", " "))
    else:
        lines.append("Hozircha sotib olishlar yo'q.")
    lines.append("\nIstalgan foydalanuvchiga cheksiz foydalanish yoki duel/test limiti sovg'a qilish "
                 "uchun ID/username kiriting:")

    await state.set_state(TeacherGiftStates.waiting_identifier)
    await callback.message.edit_text("\n".join(lines), reply_markup=back_to_admin_kb())
    await callback.answer()


@admin_teacher_manage_router.message(TeacherGiftStates.waiting_identifier)
async def teacher_gift_identifier_input(message: Message, state: FSMContext) -> None:
    identifier = (message.text or "").strip()
    user = await db.find_user_by_id_or_username(identifier)
    if not user:
        await message.answer("⚠️ Foydalanuvchi topilmadi. Qaytadan kiriting:")
        return
    await state.update_data(target_user_id=user.user_id, target_name=user.full_name)
    await state.set_state(TeacherGiftStates.waiting_mode)
    await message.answer(
        f"👤 {user.full_name} (ID: {user.user_id}) topildi.\n\nQanday sovg'a qilmoqchisiz?",
        reply_markup=admin_teacher_gift_mode_kb(),
    )


@admin_teacher_manage_router.callback_query(TeacherGiftStates.waiting_mode, F.data.in_({"tg_mode_unlimited", "tg_mode_limit"}))
async def teacher_gift_mode_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    mode = "unlimited" if callback.data == "tg_mode_unlimited" else "limit"
    await state.update_data(mode=mode)
    await state.set_state(TeacherGiftStates.waiting_value)
    if mode == "unlimited":
        await callback.message.edit_text("✏️ Necha kunga cheksiz foydalanish bermoqchisiz? (masalan: 30)")
    else:
        await callback.message.edit_text(
            "✏️ Qo'shimcha limitni kiriting \"duel,test\" formatida (masalan: 5,20):"
        )
    await callback.answer()


@admin_teacher_manage_router.message(TeacherGiftStates.waiting_value)
async def teacher_gift_value_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_user_id = data["target_user_id"]
    target_name = data["target_name"]
    mode = data["mode"]
    raw = (message.text or "").strip()

    if mode == "unlimited":
        if not raw.isdigit():
            await message.answer("⚠️ Iltimos, faqat kun sonini kiriting (masalan: 30).")
            return
        days = int(raw)
        await db.set_teacher_gift(target_user_id, unlimited=True, days=days, duel_limit=None, test_limit=None)
        await state.clear()
        await message.answer(
            f"✅ {target_name} (ID: {target_user_id}) foydalanuvchisiga {days} kunga cheksiz "
            "foydalanish berildi!", reply_markup=back_to_admin_kb(),
        )
    else:
        parts = raw.replace(" ", "").split(",")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await message.answer("⚠️ Format: duel,test (masalan: 5,20). Qaytadan kiriting:")
            return
        duel_limit, test_limit = int(parts[0]), int(parts[1])
        async with async_session() as session:
            user = await session.get(User, target_user_id)
            if user:
                user.is_teacher = True
                if user.teacher_plan == TeacherPlan.NONE:
                    user.teacher_plan = TeacherPlan.LIMITED
                    user.teacher_period_reset_at = datetime.utcnow() + timedelta(days=30)
                user.teacher_duels_used = max(0, user.teacher_duels_used - duel_limit)
                user.teacher_tests_used = max(0, user.teacher_tests_used - test_limit)
                await session.commit()
        await state.clear()
        await message.answer(
            f"✅ {target_name} (ID: {target_user_id}) uchun +{duel_limit} duel, +{test_limit} test "
            "limiti qo'shildi!", reply_markup=back_to_admin_kb(),
        )
    try:
        await message.bot.send_message(
            target_user_id,
            "🎉 Administrator tomonidan sizga ustoz platformasida qo'shimcha imkoniyat berildi! "
            "\"🎓 Ustoz Platformasi\" bo'limini tekshiring.",
        )
    except Exception:
        pass


# ============================================================================
# HANDLERS: ADMIN - TEACHER PURCHASE APPROVALS
# ============================================================================
admin_teacher_purchases_router = Router(name="admin_teacher_purchases")


@admin_teacher_purchases_router.callback_query(F.data == "admin_teacher_purchases")
async def cb_admin_teacher_purchases(callback: CallbackQuery) -> None:
    pending = await db.get_pending_teacher_purchases()
    if not pending:
        await callback.message.edit_text("💰 Kutilayotgan ustoz to'lov so'rovlari yo'q.", reply_markup=back_to_admin_kb())
        await callback.answer()
        return
    await callback.message.edit_text(f"💰 <b>{len(pending)} ta kutilayotgan so'rov</b>", reply_markup=back_to_admin_kb())
    for p in pending:
        user = await db.get_user(p.user_id)
        name = user.full_name if user else "Noma'lum"
        label = TEACHER_PLAN_LABEL.get(p.plan, p.plan.value)
        text = f"🎓 So'rov #{p.id}\n\nFoydalanuvchi: {name} (ID: {p.user_id})\nTarif: {label}\nNarxi: {p.price:,} so'm".replace(",", " ")
        await callback.message.answer(text, reply_markup=teacher_purchase_item_kb(p.id))
    await callback.answer()


@admin_teacher_purchases_router.callback_query(F.data.startswith("tpur_approve_"))
async def cb_tpur_approve(callback: CallbackQuery) -> None:
    purchase_id = int(callback.data.removeprefix("tpur_approve_"))
    p = await db.resolve_teacher_purchase(purchase_id, approved=True)
    if not p:
        await callback.answer("So'rov topilmadi.", show_alert=True)
        return
    await db.grant_teacher_plan(p.user_id, p.plan)
    label = TEACHER_PLAN_LABEL.get(p.plan, p.plan.value)
    try:
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Tasdiqlandi</b>")
    except Exception:
        pass
    await callback.answer("✅ Tarif faollashtirildi.")
    try:
        await callback.bot.send_message(p.user_id, f"🎉 Sizning <b>{label}</b> tarifingiz faollashtirildi!")
    except Exception:
        pass


@admin_teacher_purchases_router.callback_query(F.data.startswith("tpur_reject_"))
async def cb_tpur_reject(callback: CallbackQuery) -> None:
    purchase_id = int(callback.data.removeprefix("tpur_reject_"))
    p = await db.resolve_teacher_purchase(purchase_id, approved=False)
    if not p:
        await callback.answer("So'rov topilmadi.", show_alert=True)
        return
    try:
        await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Rad etildi</b>")
    except Exception:
        pass
    await callback.answer("❌ Rad etildi.")
    try:
        await callback.bot.send_message(p.user_id, "❌ Sizning ustoz tarif so'rovingiz rad etildi.")
    except Exception:
        pass


# ============================================================================
# HANDLERS: SPECIAL TEST (2 ta aniq foydalanuvchiga yuboriladigan yopiq test)
# Admin YOKI ustoz tomonidan ishga tushiriladi.
# ============================================================================
special_test_router = Router(name="special_test")

SPECIAL_QUESTION_RE = re.compile(
    r"^\s*(?P<num>\S+)\s*-\s*(?P<question>.+?)\s*\n"
    r"1\.\s*(?P<o1>.+?)\s*\n"
    r"2\.\s*(?P<o2>.+?)\s*\n"
    r"3\.\s*(?P<o3>.+?)\s*\n"
    r"4\.\s*(?P<o4>.+?)\s*\n"
    r"/javob\s*-\s*(?P<correct>[a-dA-D1-4])",
    re.MULTILINE | re.IGNORECASE,
)

LETTER_TO_NUM = {"a": 1, "b": 2, "c": 3, "d": 4}


async def _is_admin_or_teacher(user_id: int) -> bool:
    if user_id in config.admin_ids:
        return True
    user = await db.get_user(user_id)
    return bool(user and user.is_teacher)


@special_test_router.callback_query(F.data == "admin_special_test")
async def cb_admin_special_test_entry(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _is_admin_or_teacher(callback.from_user.id):
        await callback.answer("⚠️ Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(SpecialTestStates.waiting_user_ids)
    await callback.message.edit_text(
        "🧪 <b>2 Kishiga Maxsus Test</b>\n\n"
        "Ikkita foydalanuvchi ID'sini vergul bilan kiriting (masalan: 111111111,222222222):",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@special_test_router.message(SpecialTestStates.waiting_user_ids)
async def special_test_ids_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "")
    parts = raw.split(",")
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        await message.answer("⚠️ Format noto'g'ri. Masalan: 111111111,222222222")
        return
    id1, id2 = int(parts[0]), int(parts[1])
    if id1 == id2:
        await message.answer("⚠️ Ikkala ID bir xil bo'lishi mumkin emas.")
        return
    await state.update_data(user1_id=id1, user2_id=id2)
    await state.set_state(SpecialTestStates.waiting_question_block)
    await message.answer(
        "✅ ID'lar qabul qilindi. Endi savolni quyidagi formatda yuboring:\n\n"
        "<code>1 - O'ZBEKISTON NIMA DEGANI?\n1.Assalom\n2.Mustaqil davlat\n3.Variant\n4.Variant\n"
        "/javob - b</code>"
    )


@special_test_router.message(SpecialTestStates.waiting_question_block)
async def special_test_question_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "") + "\n"
    m = SPECIAL_QUESTION_RE.search(text)
    if not m:
        await message.answer(
            "⚠️ Format noto'g'ri. Namunaga qarab qaytadan yuboring:\n\n"
            "<code>1 - O'ZBEKISTON NIMA DEGANI?\n1.Assalom\n2.Mustaqil davlat\n3.Variant\n4.Variant\n"
            "/javob - b</code>"
        )
        return

    correct_raw = m.group("correct").lower()
    correct = LETTER_TO_NUM.get(correct_raw) or int(correct_raw)

    data = await state.get_data()
    await state.clear()

    st = await db.create_special_test(
        created_by=message.from_user.id, q_number=m.group("num"), question=m.group("question"),
        opt1=m.group("o1"), opt2=m.group("o2"), opt3=m.group("o3"), opt4=m.group("o4"),
        correct=correct, user1_id=data["user1_id"], user2_id=data["user2_id"],
    )

    question_text = (
        f"🧪 <b>Maxsus Test #{st.question_number}</b>\n\n"
        f"{st.question}\n\n"
        f"1. {st.option_1}\n2. {st.option_2}\n3. {st.option_3}\n4. {st.option_4}\n\n"
        "Javobingizni tanlang, so'ng /qayta buyrug'i bilan yakunlang:"
    )
    sent_to = []
    for uid in (data["user1_id"], data["user2_id"]):
        try:
            await message.bot.send_message(uid, question_text, reply_markup=special_test_options_kb(st.id))
            sent_to.append(uid)
        except Exception:
            pass

    await message.answer(
        f"✅ Test #{st.id} {len(sent_to)}/2 foydalanuvchiga yuborildi.", reply_markup=back_to_admin_kb(),
    )


@special_test_router.callback_query(F.data.startswith("st_ans_"))
async def cb_special_test_answer(callback: CallbackQuery) -> None:
    _, _, test_id_str, choice_str = callback.data.split("_")
    test_id, choice = int(test_id_str), int(choice_str)
    st = await db.get_special_test(test_id)
    if not st:
        await callback.answer("⚠️ Test topilmadi.", show_alert=True)
        return
    if callback.from_user.id not in (st.user1_id, st.user2_id):
        await callback.answer()
        return
    if st.status == SpecialTestStatus.FINISHED:
        await callback.answer("❌ Bu test allaqachon yechilgan.", show_alert=True)
        return
    await db.set_special_test_choice(test_id, callback.from_user.id, choice)
    await callback.answer(f"✅ Javobingiz: {choice}. Yakunlash uchun /qayta yozing.")


@special_test_router.message(Command("qayta"))
async def cmd_qayta(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(SpecialTest).where(
                SpecialTest.status == SpecialTestStatus.PENDING,
                (SpecialTest.user1_id == message.from_user.id) | (SpecialTest.user2_id == message.from_user.id),
            ).order_by(SpecialTest.created_at.desc())
        )
        st = result.scalars().first()

    if not st:
        await message.answer("⚠️ Sizda yakunlanishi kerak bo'lgan maxsus test topilmadi.")
        return

    choice = st.user1_choice if message.from_user.id == st.user1_id else st.user2_choice
    if choice is None:
        await message.answer("⚠️ Avval javob variantini tanlang, keyin /qayta yozing.")
        return

    updated, was_first = await db.finalize_special_test(st.id, message.from_user.id)
    if not was_first:
        await message.answer("❌ Bu test allaqachon yechilgan.")
        return

    is_correct = choice == updated.correct_option
    await message.answer(
        f"✅ Javobingiz qabul qilindi: {choice} — {'to‘g‘ri ✅' if is_correct else 'noto‘g‘ri ❌'}\n"
        f"⏱ Javob vaqti: {updated.response_seconds} soniya"
    )

    other_id = st.user2_id if message.from_user.id == st.user1_id else st.user1_id
    try:
        await message.bot.send_message(other_id, "❌ Bu test allaqachon yechilgan (raqibingiz birinchi bo'lib yakunladi).")
    except Exception:
        pass

    result_text = (
        f"🧪 <b>Maxsus Test #{updated.question_number} natijasi</b>\n\n"
        f"Savol: {updated.question}\n"
        f"To'g'ri javob: {updated.correct_option}\n\n"
        f"👤 Yechgan: {message.from_user.full_name} (ID: {message.from_user.id})\n"
        f"Javobi: {choice} — {'✅ to‘g‘ri' if is_correct else '❌ noto‘g‘ri'}\n"
        f"⏱ Javob vaqti: {updated.response_seconds} soniya"
    )
    if config.admin_chat_id:
        try:
            await message.bot.send_message(config.admin_chat_id, result_text)
        except Exception:
            pass


# ============================================================================
# HANDLERS: ADMIN - /live (jonli monitoring)
# ============================================================================
live_monitor_router = Router(name="live_monitor")


@live_monitor_router.message(Command("live"))
async def cmd_live(message: Message) -> None:
    active_custom = await db.count_active_custom_duels()
    active_normal = await db.count_active_duels()
    total_watchers = sum(len(s.watchers) for s in duel_manager._sessions.values())
    lines = [
        "🔴 <b>Jonli Monitoring</b>\n",
        f"⚔️ Faol oddiy dueller: {active_normal}",
        f"🎓 Faol maxsus (ustoz) dueller: {active_custom}",
        f"👀 Jami tomoshabinlar: {total_watchers}",
    ]
    await message.answer("\n".join(lines))


# ============================================================================
# HANDLERS: ADMIN - BAN/UNBAN
# (from: bot/handlers/admin/ban.py)
# ============================================================================
admin_ban_router = Router(name="admin_ban")


class _AwaitBanTarget:
    """Lightweight marker state name (kept out of states.py since it's a
    single simple text prompt with no further branching)."""


@admin_ban_router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state("admin_ban:waiting_identifier")
    await callback.message.edit_text(
        "🚫 <b>Ban / Unban</b>\n\nBan qilmoqchi (yoki bekor qilmoqchi) bo'lgan foydalanuvchi "
        "ID yoki username'ini kiriting:",
        reply_markup=back_to_admin_kb(),
    )
    await callback.answer()


@admin_ban_router.message(StateFilter("admin_ban:waiting_identifier"))
async def ban_identifier_input(message: Message, state: FSMContext) -> None:
    identifier = (message.text or "").strip()
    user = await db.find_user_by_id_or_username(identifier)
    await state.clear()
    if not user:
        await message.answer("⚠️ Foydalanuvchi topilmadi.", reply_markup=back_to_admin_kb())
        return

    status = "🚫 Banlangan" if user.is_banned else "✅ Faol"
    await message.answer(
        f"👤 {user.full_name} (ID: {user.user_id})\nHolati: {status}",
        reply_markup=ban_action_kb(user.user_id, user.is_banned),
    )


@admin_ban_router.callback_query(F.data.startswith("ban_do_"))
async def cb_ban_do(callback: CallbackQuery) -> None:
    user_id = int(callback.data.removeprefix("ban_do_"))
    await db.ban_user(user_id, banned=True)
    await callback.message.edit_text(f"🚫 Foydalanuvchi (ID: {user_id}) banlandi.", reply_markup=back_to_admin_kb())
    await callback.answer()


@admin_ban_router.callback_query(F.data.startswith("ban_unban_"))
async def cb_ban_unban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.removeprefix("ban_unban_"))
    await db.ban_user(user_id, banned=False)
    await callback.message.edit_text(f"✅ Foydalanuvchi (ID: {user_id}) uchun ban bekor qilindi.", reply_markup=back_to_admin_kb())
    await callback.answer()


# ============================================================================
# ROUTER WIRING
# (originally: bot/handlers/user/__init__.py + bot/handlers/admin/__init__.py)
# ============================================================================

user_router = Router(name="user")
user_router.include_router(start_router)
user_router.include_router(duel_router)
user_router.include_router(referral_router)
user_router.include_router(rating_router)
user_router.include_router(rules_router)
user_router.include_router(store_router)
user_router.include_router(profile_router)
user_router.include_router(teacher_router)

admin_router = Router(name="admin")
# Access control: a *filter* (not outer middleware) is the correct tool here.
# If IsAdmin() fails to match, aiogram treats this router's handlers as
# "not matched" and propagation continues on to sibling routers (i.e. the
# normal user_router still gets /start, menu taps, etc. from everyone).
# An outer middleware that silently swallows the event here would instead
# stop propagation entirely and break the bot for non-admin users.
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

admin_router.include_router(admin_panel_router)
admin_router.include_router(admin_stats_router)
admin_router.include_router(admin_broadcast_router)
admin_router.include_router(admin_channel_gate_router)
admin_router.include_router(admin_withdrawals_router)
admin_router.include_router(admin_test_parser_router)
admin_router.include_router(admin_gift_router)
admin_router.include_router(admin_teacher_code_router)
admin_router.include_router(admin_teacher_manage_router)
admin_router.include_router(admin_teacher_purchases_router)
admin_router.include_router(admin_ban_router)
admin_router.include_router(live_monitor_router)

# special_test_router alohida (IsAdmin bilan cheklanmagan) - chunki uni
# ustozlar ham (admin bo'lmasa-da) ishlatishi mumkin. Ruxsat handler ichida
# `_is_admin_or_teacher` orqali tekshiriladi.


# ============================================================================
# ENTRYPOINT
# (from: bot/main.py)
# Run with:  python bellashuv_uz_bot_full.py
# ============================================================================


async def main() -> None:
    if not config.bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    await init_db()
    logger.info("Database initialized (%s)", config.database_url)

    # --- Global middlewares (order matters: user context first, then gate) ---
    dp.message.middleware(UserContextMiddleware())
    dp.callback_query.middleware(UserContextMiddleware())
    dp.message.middleware(MandatoryChannelMiddleware())
    dp.callback_query.middleware(MandatoryChannelMiddleware())

    # --- Routers ---
    # Admin router first so /admin and admin_* callbacks are claimed before
    # the generic user router would otherwise (harmlessly) ignore them.
    dp.include_router(admin_router)
    dp.include_router(user_router)
    # Channel-post buffer for the test parser: registered directly on the
    # dispatcher (not the admin router) since channel posts carry no
    # from_user to check against ADMIN_IDS - see test_channel_buffer_router above.
    dp.include_router(test_channel_buffer_router)

    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started (weekly leaderboard reset: Sunday 21:00 Asia/Tashkent)")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot starting polling...")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
