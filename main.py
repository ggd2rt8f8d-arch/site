import os
import asyncio
import logging
import hashlib
import hmac
import asyncpg
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@topzfilmz")
DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-secret-key")

super_admins_str = os.getenv("SUPER_ADMIN_IDS", "")
SUPER_ADMIN_IDS = [int(x.strip()) for x in super_admins_str.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан!")

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================
pool: asyncpg.Pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                year INTEGER,
                poster TEXT,
                description TEXT,
                rating TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                user_id BIGINT PRIMARY KEY,
                reason TEXT DEFAULT ''
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            INSERT INTO stats (key, value) VALUES ('total_requests', 0)
            ON CONFLICT (key) DO NOTHING
        """)
    logger.info("База данных инициализирована")

async def get_pool():
    return pool

# ==================== БОТ (aiogram) ====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- FSM ----------
class AddMovie(StatesGroup):
    code = State()
    title = State()
    year = State()
    poster = State()
    description = State()
    rating = State()

class EditMovie(StatesGroup):
    waiting_value = State()

class BanUser(StatesGroup):
    waiting_id = State()

class AddAdmin(StatesGroup):
    waiting_id = State()

# ---------- Функции БД для бота ----------
async def get_movie(code: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM movies WHERE code = $1", code)
        if row:
            return dict(row)
        return None

async def get_all_movies():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, title, year FROM movies ORDER BY code")
        return [(r["code"], r["title"], r["year"]) for r in rows]

async def get_movies_count():
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM movies") or 0

async def add_movie_to_db(code, title, year, poster, description, rating):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO movies (code, title, year, poster, description, rating) VALUES ($1, $2, $3, $4, $5, $6)",
            code, title, year, poster, description, rating
        )

async def update_movie_field(code: str, field: str, value):
    allowed = {"title", "year", "poster", "description", "rating"}
    if field not in allowed:
        return
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE movies SET {field} = $1 WHERE code = $2", value, code)

async def delete_movie(code: str):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM movies WHERE code = $1", code)

async def is_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMIN_IDS:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM admins WHERE user_id = $1", user_id) is not None

async def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS

async def add_admin(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

async def remove_admin(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)

async def get_admins():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
        return [r["user_id"] for r in rows]

async def ban_user(user_id: int, reason: str = ""):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bans (user_id, reason) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET reason = $2",
            user_id, reason
        )

async def unban_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE user_id = $1", user_id)

async def is_banned(user_id: int) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM bans WHERE user_id = $1", user_id) is not None

async def get_banned_users():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, reason FROM bans")
        return [(r["user_id"], r["reason"]) for r in rows]

async def increment_requests():
    async with pool.acquire() as conn:
        await conn.execute("UPDATE stats SET value = value + 1 WHERE key = 'total_requests'")

async def get_total_requests():
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0

async def check_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    except Exception:
        return False

# ---------- Клавиатуры ----------
def subscribe_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ])

def admin_reply_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔧 Админ-панель")]],
        resize_keyboard=True
    )

def admin_main_kb(user_id: int):
    buttons = [
        [InlineKeyboardButton(text="📋 Список фильмов", callback_data="admin_list")],
        [InlineKeyboardButton(text="➕ Добавить фильм", callback_data="admin_add")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
    ]
    if is_super_admin(user_id):
        buttons.append([InlineKeyboardButton(text="👥 Управление админами", callback_data="admin_admins")])
        buttons.append([InlineKeyboardButton(text="🚫 Баны", callback_data="admin_bans")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def movie_actions_kb(code: str, user_id: int):
    buttons = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_title:{code}")],
        [InlineKeyboardButton(text="📅 Год", callback_data=f"edit_year:{code}")],
        [InlineKeyboardButton(text="🖼 Обложка", callback_data=f"edit_poster:{code}")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"edit_description:{code}")],
        [InlineKeyboardButton(text="⭐ Рейтинг", callback_data=f"edit_rating:{code}")],
    ]
    if is_super_admin(user_id):
        buttons.append([InlineKeyboardButton(text="🗑 Удалить фильм", callback_data=f"delete_movie:{code}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Хэндлеры бота ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if await is_banned(message.from_user.id):
        return await message.answer("🚫 Вы заблокированы в боте.")
    is_user_admin = await is_admin(message.from_user.id)
    if await check_sub(message.from_user.id):
        text = "Привет! 👋\nВведи код фильма:"
        if is_user_admin:
            await message.answer(text, reply_markup=admin_reply_kb())
        else:
            await message.answer(text, reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Подпишись на канал, чтобы пользоваться ботом:", reply_markup=subscribe_kb())

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    if await check_sub(callback.from_user.id):
        is_user_admin = await is_admin(callback.from_user.id)
        text = "✅ Подписка подтверждена!\nВведи код фильма:"
        if is_user_admin:
            await callback.message.answer(text, reply_markup=admin_reply_kb())
        else:
            await callback.message.answer(text)
        await callback.message.delete()
    else:
        await callback.answer("Ты ещё не подписан!", show_alert=True)

@dp.message(F.text == "🔧 Админ-панель")
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа")
    await message.answer("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_main_kb(message.from_user.id))

@dp.callback_query(F.data == "admin_close")
async def cb_close(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data == "admin_back")
async def cb_back(callback: CallbackQuery):
    await callback.message.edit_text("
