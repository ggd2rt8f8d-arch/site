import os
import asyncio
import logging
import asyncpg
import random
import json
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== ПЕРЕМЕННЫЕ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_ID = os.getenv("BOT_ID")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@topzfilmz")
DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-secret-key")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

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
        # Основные таблицы
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                year INTEGER,
                poster TEXT,
                description TEXT,
                rating TEXT,
                banner TEXT,
                added_by BIGINT,
                director TEXT,
                writers TEXT,
                genres TEXT,
                budget TEXT,
                box_office_us TEXT,
                box_office_world TEXT,
                cast_list TEXT,
                country TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                photo_url TEXT,
                last_seen TIMESTAMP DEFAULT NOW(),
                is_online BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                user_id BIGINT PRIMARY KEY,
                reason TEXT DEFAULT '',
                expires_at TIMESTAMP
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS punishments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                reason TEXT,
                issued_by BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_by BIGINT,
                resolved_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_reviews (
                id SERIAL PRIMARY KEY,
                movie_code TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                rating INTEGER,
                text TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_comments (
                id SERIAL PRIMARY KEY,
                movie_code TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_stats (
                user_id BIGINT PRIMARY KEY,
                movies_added INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_names (
                user_id BIGINT PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                display_name TEXT,
                photo_url TEXT,
                banner_url TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_comments (
                id SERIAL PRIMARY KEY,
                target_user_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_ratings (
                movie_code TEXT PRIMARY KEY,
                total_rating INTEGER DEFAULT 0,
                votes_count INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS super_admins (user_id BIGINT PRIMARY KEY)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS google_users (
                email TEXT PRIMARY KEY,
                user_id BIGINT UNIQUE,
                name TEXT,
                picture TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Таблицы для друзей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friends (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                friend_id BIGINT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, friend_id)
            )
        """)
        # Таблицы для уведомлений
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                link TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Таблица новостей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                author_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Таблица жалоб
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                reporter_id BIGINT NOT NULL,
                reported_id BIGINT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Таблица для чатов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL,
                receiver_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Добавляем колонки
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='added_by') THEN
                    ALTER TABLE movies ADD COLUMN added_by BIGINT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='banner') THEN
                    ALTER TABLE movies ADD COLUMN banner TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='country') THEN
                    ALTER TABLE movies ADD COLUMN country TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='director') THEN
                    ALTER TABLE movies ADD COLUMN director TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='writers') THEN
                    ALTER TABLE movies ADD COLUMN writers TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='genres') THEN
                    ALTER TABLE movies ADD COLUMN genres TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='budget') THEN
                    ALTER TABLE movies ADD COLUMN budget TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='box_office_us') THEN
                    ALTER TABLE movies ADD COLUMN box_office_us TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='box_office_world') THEN
                    ALTER TABLE movies ADD COLUMN box_office_world TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='movies' AND column_name='cast_list') THEN
                    ALTER TABLE movies ADD COLUMN cast_list TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bans' AND column_name='expires_at') THEN
                    ALTER TABLE bans ADD COLUMN expires_at TIMESTAMP;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_names' AND column_name='photo_url') THEN
                    ALTER TABLE user_names ADD COLUMN photo_url TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_names' AND column_name='banner_url') THEN
                    ALTER TABLE user_names ADD COLUMN banner_url TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_names' AND column_name='display_name') THEN
                    ALTER TABLE user_names ADD COLUMN display_name TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_names' AND column_name='username') THEN
                    ALTER TABLE user_names ADD COLUMN username TEXT UNIQUE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_seen') THEN
                    ALTER TABLE users ADD COLUMN last_seen TIMESTAMP DEFAULT NOW();
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_online') THEN
                    ALTER TABLE users ADD COLUMN is_online BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='username') THEN
                    ALTER TABLE users ADD COLUMN username TEXT UNIQUE;
                END IF;
            END $$;
        """)

        # Триггеры
        await conn.execute("""
            CREATE OR REPLACE FUNCTION update_admin_stats()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.added_by IS NOT NULL THEN
                    INSERT INTO admin_stats (user_id, movies_added)
                    VALUES (NEW.added_by, 1)
                    ON CONFLICT (user_id) DO UPDATE
                    SET movies_added = admin_stats.movies_added + 1;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        await conn.execute("DROP TRIGGER IF EXISTS trigger_update_admin_stats ON movies;")
        await conn.execute("""
            CREATE TRIGGER trigger_update_admin_stats
            AFTER INSERT ON movies
            FOR EACH ROW
            EXECUTE FUNCTION update_admin_stats();
        """)

    logger.info("База данных инициализирована")

async def get_pool():
    return pool

# ==================== ХРАНИЛИЩЕ КОДОВ ====================
verification_codes = {}

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- FSM ----------
class ReviewState(StatesGroup):
    waiting_review = State()
    waiting_rating = State()

class AddMovie(StatesGroup):
    code = State()
    title = State()
    year = State()
    poster = State()
    description = State()
    rating = State()
    banner = State()

class EditMovie(StatesGroup):
    waiting_value = State()

class BanUser(StatesGroup):
    waiting_id = State()

class AddAdmin(StatesGroup):
    waiting_id = State()

# ---------- Функции БД ----------
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

async def add_movie_to_db(code, title, year, poster, description, rating, banner, user_id=None, 
                           director=None, writers=None, genres=None, budget=None, 
                           box_office_us=None, box_office_world=None, cast_list=None, country=None):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO movies (code, title, year, poster, description, rating, banner, added_by, 
               director, writers, genres, budget, box_office_us, box_office_world, cast_list, country) 
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)""",
            code, title, year, poster, description, rating, banner, user_id, 
            director, writers, genres, budget, box_office_us, box_office_world, cast_list, country
        )

async def update_movie_field(code: str, field: str, value):
    allowed = {"title", "year", "poster", "description", "rating", "banner", "director", "writers", 
               "genres", "budget", "box_office_us", "box_office_world", "cast_list", "country"}
    if field not in allowed:
        return
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE movies SET {field} = $1 WHERE code = $2", value, code)

async def delete_movie(code: str):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM movies WHERE code = $1", code)
        await conn.execute("DELETE FROM movie_ratings WHERE movie_code = $1", code)
        await conn.execute("DELETE FROM movie_reviews WHERE movie_code = $1", code)
        await conn.execute("DELETE FROM movie_comments WHERE movie_code = $1", code)

async def is_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMIN_IDS:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM admins WHERE user_id = $1", user_id) is not None

async def is_super_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMIN_IDS:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM super_admins WHERE user_id = $1", user_id) is not None

async def is_user_exists(user_id: int) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id) is not None

async def add_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None, photo_url: str = None):
    async with pool.acquire() as conn:
        if username:
            existing = await conn.fetchval("SELECT user_id FROM users WHERE username = $1 AND user_id != $2", username, user_id)
            if existing:
                raise ValueError("Username already taken")
        await conn.execute(
            """INSERT INTO users (user_id, username, first_name, last_name, photo_url, last_seen, is_online) 
               VALUES ($1, $2, $3, $4, $5, NOW(), TRUE) 
               ON CONFLICT (user_id) DO UPDATE 
               SET username = $2, first_name = $3, last_name = $4, photo_url = $5, last_seen = NOW(), is_online = TRUE""",
            user_id, username, first_name, last_name, photo_url
        )
        await save_user_data(user_id, username, first_name, last_name, first_name or username, photo_url, None)

async def update_user_online(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_seen = NOW(), is_online = TRUE WHERE user_id = $1",
            user_id
        )

async def is_user_online(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_online, last_seen FROM users WHERE user_id = $1",
            user_id
        )
        if row:
            if row["is_online"] and row["last_seen"]:
                delta = datetime.now() - row["last_seen"]
                if delta.total_seconds() > 300:
                    await conn.execute("UPDATE users SET is_online = FALSE WHERE user_id = $1", user_id)
                    return False
                return True
        return False

async def add_admin(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

async def remove_admin(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM super_admins WHERE user_id = $1", user_id)

async def get_admins_with_stats():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.user_id, 
                   COALESCE(ast.movies_added, 0) as movies_count,
                   COALESCE((
                       SELECT COUNT(*) FROM punishments 
                       WHERE user_id = a.user_id 
                       AND type = 'warning' 
                       AND resolved = FALSE
                   ), 0) as warns
            FROM admins a
            LEFT JOIN admin_stats ast ON a.user_id = ast.user_id
            ORDER BY a.user_id
        """)
        return [dict(r) for r in rows]

async def ban_user(user_id: int, reason: str = "", duration_hours: int = 0):
    async with pool.acquire() as conn:
        if duration_hours > 0:
            await conn.execute(
                "INSERT INTO bans (user_id, reason, expires_at) VALUES ($1, $2, NOW() + ($3 || ' hours')::INTERVAL) ON CONFLICT (user_id) DO UPDATE SET reason = $2, expires_at = NOW() + ($3 || ' hours')::INTERVAL",
                user_id, reason, duration_hours
            )
        else:
            await conn.execute(
                "INSERT INTO bans (user_id, reason, expires_at) VALUES ($1, $2, NULL) ON CONFLICT (user_id) DO UPDATE SET reason = $2, expires_at = NULL",
                user_id, reason
            )

async def unban_user(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE user_id = $1", user_id)

async def is_banned(user_id: int) -> bool:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at < NOW()")
        return await conn.fetchval("SELECT 1 FROM bans WHERE user_id = $1", user_id) is not None

async def get_banned_users():
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at < NOW()")
        rows = await conn.fetch("SELECT user_id, reason, expires_at FROM bans")
        return [dict(r) for r in rows]

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

# ---------- Telegram API ----------
async def get_telegram_user(user_id: int):
    try:
        chat = await bot.get_chat(user_id)
        user_info = {
            "id": user_id,
            "username": chat.username,
            "first_name": chat.first_name,
            "last_name": chat.last_name,
            "photo_url": None
        }
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.photos:
                file = photos.photos[0][-1]
                file_info = await bot.get_file(file.file_id)
                user_info["photo_url"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        except:
            pass
        return user_info
    except Exception as e:
        logger.error(f"Ошибка получения данных пользователя {user_id}: {e}")
        return None

async def send_verification_code(user_id: int):
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=5)
    verification_codes[user_id] = {"code": code, "expires_at": expires_at}
    try:
        await bot.send_message(
            user_id,
            f"🔐 <b>Код подтверждения</b>\n\n"
            f"Ваш код для входа в админ-панель:\n\n"
            f"<code>{code}</code>\n\n"
            f"⏳ Код действителен <b>5 минут</b>.",
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки кода: {e}")
        return False

# ---------- Отзывы и рейтинг ----------
async def add_review(movie_code: str, user_id: int, rating: int, text: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO movie_reviews (movie_code, user_id, rating, text) VALUES ($1, $2, $3, $4)",
            movie_code, user_id, rating, text
        )
        await conn.execute("""
            INSERT INTO movie_ratings (movie_code, total_rating, votes_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (movie_code) DO UPDATE
            SET total_rating = movie_ratings.total_rating + $2,
                votes_count = movie_ratings.votes_count + 1
        """, movie_code, rating)
        await update_user_online(user_id)

async def get_reviews(movie_code: str, limit: int = 10):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM movie_reviews WHERE movie_code = $1 ORDER BY created_at DESC LIMIT $2",
            movie_code, limit
        )
        return [dict(r) for r in rows]

async def get_movie_tpz(movie_code: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_rating, votes_count FROM movie_ratings WHERE movie_code = $1",
            movie_code
        )
        if row and row["votes_count"] > 0:
            return round(row["total_rating"] / row["votes_count"], 1), row["votes_count"]
        return None, 0

async def delete_review(review_id: int, user_id: int):
    async with pool.acquire() as conn:
        review = await conn.fetchrow("SELECT movie_code, rating FROM movie_reviews WHERE id = $1 AND user_id = $2", review_id, user_id)
        if review:
            await conn.execute("DELETE FROM movie_reviews WHERE id = $1", review_id)
            await conn.execute("""
                UPDATE movie_ratings 
                SET total_rating = total_rating - $1,
                    votes_count = votes_count - 1
                WHERE movie_code = $2
            """, review["rating"], review["movie_code"])
            await conn.execute("DELETE FROM movie_ratings WHERE movie_code = $1 AND votes_count = 0", review["movie_code"])
            return True
        return False

async def delete_profile_comment(comment_id: int, user_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM profile_comments WHERE id = $1 AND (author_id = $2 OR $2 IN (SELECT user_id FROM super_admins))",
            comment_id, user_id
        )
        return result != "DELETE 0"

async def get_reviews_count(movie_code: str):
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM movie_reviews WHERE movie_code = $1", movie_code) or 0

async def get_user_review(movie_code: str, user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM movie_reviews WHERE movie_code = $1 AND user_id = $2", movie_code, user_id)

# ---------- Профили ----------
async def save_user_data(user_id: int, username: str = None, first_name: str = None, last_name: str = None, 
                          display_name: str = None, photo_url: str = None, banner_url: str = None):
    async with pool.acquire() as conn:
        if username:
            existing = await conn.fetchval("SELECT user_id FROM user_names WHERE username = $1 AND user_id != $2", username, user_id)
            if existing:
                raise ValueError("Username already taken")
        await conn.execute(
            """INSERT INTO user_names (user_id, username, first_name, last_name, display_name, photo_url, banner_url, updated_at) 
               VALUES ($1, $2, $3, $4, $5, $6, $7, NOW()) 
               ON CONFLICT (user_id) DO UPDATE 
               SET username = $2, first_name = $3, last_name = $4, display_name = $5, photo_url = $6, banner_url = $7, updated_at = NOW()""",
            user_id, username, first_name, last_name, display_name, photo_url, banner_url
        )
        await update_user_online(user_id)

async def get_user_name(user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, first_name, last_name, display_name, photo_url, banner_url FROM user_names WHERE user_id = $1",
            user_id
        )
        is_super = user_id in SUPER_ADMIN_IDS or await is_super_admin(user_id)
        is_admin_user = await is_admin(user_id)
        if row:
            display = row["display_name"] or row["first_name"] or f"Пользователь {user_id}"
            username = row["username"] or str(user_id)
            return {
                "display_name": display,
                "username": username,
                "photo_url": row["photo_url"],
                "banner_url": row["banner_url"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "is_super": is_super,
                "is_admin": is_admin_user
            }
        return {
            "display_name": f"Пользователь {user_id}",
            "username": str(user_id),
            "photo_url": None,
            "banner_url": None,
            "first_name": None,
            "last_name": None,
            "is_super": is_super,
            "is_admin": is_admin_user
        }

async def get_user_profile(user_id: int):
    async with pool.acquire() as conn:
        is_admin_user = await is_admin(user_id)
        is_super_user = await is_super_admin(user_id)
        is_banned_user = await is_banned(user_id)
        is_online = await is_user_online(user_id)
        
        movies_count = await conn.fetchval("SELECT movies_added FROM admin_stats WHERE user_id = $1", user_id) or 0
        warns = await conn.fetchval("SELECT COUNT(*) FROM punishments WHERE user_id = $1 AND type = 'warning' AND resolved = FALSE", user_id) or 0
        punishments = await conn.fetch("SELECT * FROM punishments WHERE user_id = $1 ORDER BY created_at DESC", user_id)
        user_name_data = await get_user_name(user_id)
        reviews_count = await conn.fetchval("SELECT COUNT(*) FROM movie_reviews WHERE user_id = $1", user_id) or 0
        
        return {
            "user_id": user_id,
            "display_name": user_name_data["display_name"],
            "username": user_name_data["username"],
            "photo_url": user_name_data["photo_url"],
            "banner_url": user_name_data["banner_url"],
            "is_admin": is_admin_user,
            "is_super_admin": is_super_user,
            "is_banned": is_banned_user,
            "is_online": is_online,
            "movies_count": movies_count,
            "warns": warns,
            "reviews_count": reviews_count,
            "total_punishments": len(punishments),
            "punishments": [dict(p) for p in punishments]
        }

async def add_punishment(user_id: int, ptype: str, reason: str, issued_by: int, duration_hours: int = 0):
    async with pool.acquire() as conn:
        if duration_hours > 0:
            await conn.execute(
                "INSERT INTO punishments (user_id, type, reason, issued_by, expires_at) VALUES ($1, $2, $3, $4, NOW() + ($5 || ' hours')::INTERVAL)",
                user_id, ptype, reason, issued_by, duration_hours
            )
        else:
            await conn.execute(
                "INSERT INTO punishments (user_id, type, reason, issued_by) VALUES ($1, $2, $3, $4)",
                user_id, ptype, reason, issued_by
            )
        if ptype in ("ban", "permanent_ban"):
            await ban_user(user_id, reason, duration_hours if ptype == "ban" else 0)

async def resolve_punishment(punishment_id: int, resolved_by: int):
    async with pool.acquire() as conn:
        punishment = await conn.fetchrow("SELECT user_id, type FROM punishments WHERE id = $1", punishment_id)
        if punishment:
            await conn.execute("UPDATE punishments SET resolved = TRUE, resolved_by = $1, resolved_at = NOW() WHERE id = $2", resolved_by, punishment_id)
            if punishment["type"] in ("ban", "permanent_ban"):
                await unban_user(punishment["user_id"])
            return True
        return False

async def add_profile_comment(target_user_id: int, author_id: int, text: str):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO profile_comments (target_user_id, author_id, text) VALUES ($1, $2, $3)", target_user_id, author_id, text)
        await update_user_online(author_id)

async def get_profile_comments(target_user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM profile_comments WHERE target_user_id = $1 ORDER BY created_at DESC", target_user_id)
        return [dict(r) for r in rows]

# ---------- Google Auth ----------
async def get_or_create_user_from_google(email: str, name: str, picture: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM google_users WHERE email = $1", email)
        if row:
            user_id = row["user_id"]
            await update_user_online(user_id)
            return user_id
        
        user_id = random.randint(100000000, 999999999)
        # Генерируем уникальный username из email
        username = email.split('@')[0]
        # Проверяем уникальность
        existing = await conn.fetchval("SELECT user_id FROM users WHERE username = $1", username)
        if existing:
            username = f"{username}_{random.randint(100, 999)}"
        
        await conn.execute(
            "INSERT INTO google_users (email, user_id, name, picture) VALUES ($1, $2, $3, $4)",
            email, user_id, name, picture
        )
        await save_user_data(user_id, username, name, None, name, picture, None)
        await add_user(user_id, username, name, None, picture)
        return user_id

# ---------- Друзья ----------
async def send_friend_request(user_id: int, friend_id: int):
    async with pool.acquire() as conn:
        # Проверяем, не отправлял ли уже запрос
        existing = await conn.fetchval(
            "SELECT id FROM friends WHERE user_id = $1 AND friend_id = $2",
            user_id, friend_id
        )
        if existing:
            return False, "Запрос уже отправлен"
        # Проверяем, не являются ли уже друзьями
        existing_friend = await conn.fetchval(
            "SELECT id FROM friends WHERE user_id = $1 AND friend_id = $2 AND status = 'accepted'",
            user_id, friend_id
        )
        if existing_friend:
            return False, "Вы уже друзья"
        
        await conn.execute(
            "INSERT INTO friends (user_id, friend_id, status) VALUES ($1, $2, 'pending')",
            user_id, friend_id
        )
        # Создаем уведомление для получателя
        await conn.execute(
            "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
            friend_id, 'friend_request', f'Пользователь отправил вам запрос в друзья', f'/profile/{user_id}'
        )
        return True, "Запрос отправлен"

async def accept_friend_request(user_id: int, friend_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE friends SET status = 'accepted' WHERE user_id = $1 AND friend_id = $2 AND status = 'pending'",
            friend_id, user_id
        )
        if result != "UPDATE 0":
            await conn.execute(
                "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
                friend_id, 'friend_accepted', f'Пользователь принял ваш запрос в друзья', f'/profile/{user_id}'
            )
            return True
        return False

async def decline_friend_request(user_id: int, friend_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM friends WHERE user_id = $1 AND friend_id = $2 AND status = 'pending'",
            friend_id, user_id
        )
        return result != "DELETE 0"

async def get_friends(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.friend_id, f.status, u.username, u.photo_url, u.is_online
            FROM friends f
            JOIN users u ON f.friend_id = u.user_id
            WHERE f.user_id = $1 AND f.status = 'accepted'
            UNION
            SELECT f.user_id as friend_id, f.status, u.username, u.photo_url, u.is_online
            FROM friends f
            JOIN users u ON f.user_id = u.user_id
            WHERE f.friend_id = $1 AND f.status = 'accepted'
        """, user_id)
        return [dict(r) for r in rows]

async def get_friend_requests(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.user_id, f.status, u.username, u.photo_url
            FROM friends f
            JOIN users u ON f.user_id = u.user_id
            WHERE f.friend_id = $1 AND f.status = 'pending'
        """, user_id)
        return [dict(r) for r in rows]

async def search_users(query: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, username, first_name, last_name, photo_url
            FROM users
            WHERE username ILIKE $1 OR first_name ILIKE $1
            LIMIT 20
        """, f"%{query}%")
        return [dict(r) for r in rows]

# ---------- Уведомления ----------
async def get_notifications(user_id: int, unread_only: bool = False):
    async with pool.acquire() as conn:
        query = "SELECT * FROM notifications WHERE user_id = $1"
        params = [user_id]
        if unread_only:
            query += " AND is_read = FALSE"
        query += " ORDER BY created_at DESC LIMIT 50"
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

async def mark_notification_read(notification_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notifications SET is_read = TRUE WHERE id = $1",
            notification_id
        )

async def mark_all_notifications_read(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notifications SET is_read = TRUE WHERE user_id = $1",
            user_id
        )

async def get_unread_count(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM notifications WHERE user_id = $1 AND is_read = FALSE",
            user_id
        ) or 0

# ---------- Новости ----------
async def create_news(title: str, content: str, author_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO news (title, content, author_id) VALUES ($1, $2, $3)",
            title, content, author_id
        )
        # Уведомляем всех пользователей
        users = await conn.fetch("SELECT user_id FROM users")
        for user in users:
            await conn.execute(
                "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
                user["user_id"], 'news', f'📰 {title}', f'/news'
            )
        return result

async def get_news(limit: int = 20):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT n.*, u.username, u.photo_url
            FROM news n
            JOIN users u ON n.author_id = u.user_id
            ORDER BY n.created_at DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]

# ---------- Сообщения (чат) ----------
async def send_message(sender_id: int, receiver_id: int, text: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (sender_id, receiver_id, text) VALUES ($1, $2, $3)",
            sender_id, receiver_id, text
        )
        await conn.execute(
            "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
            receiver_id, 'message', f'Новое сообщение от пользователя', f'/chat/{sender_id}'
        )
        return True

async def get_messages(user_id: int, other_user_id: int, limit: int = 50):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM messages 
            WHERE (sender_id = $1 AND receiver_id = $2) OR (sender_id = $2 AND receiver_id = $1)
            ORDER BY created_at DESC
            LIMIT $3
        """, user_id, other_user_id, limit)
        # Отмечаем сообщения как прочитанные
        await conn.execute(
            "UPDATE messages SET is_read = TRUE WHERE sender_id = $1 AND receiver_id = $2 AND is_read = FALSE",
            other_user_id, user_id
        )
        return [dict(r) for r in reversed(rows)]

async def get_chat_users(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT 
                CASE 
                    WHEN sender_id = $1 THEN receiver_id
                    ELSE sender_id
                END as other_user_id
            FROM messages
            WHERE sender_id = $1 OR receiver_id = $1
            ORDER BY created_at DESC
        """, user_id)
        result = []
        for row in rows:
            user_data = await get_user_name(row["other_user_id"])
            user_data["user_id"] = row["other_user_id"]
            user_data["is_online"] = await is_user_online(row["other_user_id"])
            result.append(user_data)
        return result

# ---------- Жалобы ----------
async def create_report(reporter_id: int, reported_id: int, reason: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reports (reporter_id, reported_id, reason) VALUES ($1, $2, $3)",
            reporter_id, reported_id, reason
        )
        # Уведомляем суперадминов
        for admin_id in SUPER_ADMIN_IDS:
            await conn.execute(
                "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
                admin_id, 'report', f'🚨 Новая жалоба на пользователя', f'/profile/{reported_id}'
            )
        return True

# ---------- Клавиатуры ----------
def subscribe_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ])

def admin_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔧 Админ-панель")]], resize_keyboard=True)

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

def movie_review_kb(code: str, has_reviews: bool = True):
    buttons = []
    if has_reviews:
        buttons.append([InlineKeyboardButton(text="⭐ Отзывы", callback_data=f"reviews:{code}")])
    else:
        buttons.append([InlineKeyboardButton(text="✏️ Оставить отзыв", callback_data=f"write_review:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def review_back_kb(code: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"movie_back:{code}")],
        [InlineKeyboardButton(text="✏️ Оставить отзыв", callback_data=f"write_review:{code}")]
    ])

def review_only_back_kb(code: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"movie_back:{code}")]
    ])

# ==================== ХЭНДЛЕРЫ БОТА ====================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await update_user_online(message.from_user.id)
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
    await update_user_online(callback.from_user.id)
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
    await update_user_online(message.from_user.id)
    if not await is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа")
    await message.answer("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_main_kb(message.from_user.id))

# ... (остальные хэндлеры бота остаются без изменений, просто добавляем update_user_online)

@dp.message(StateFilter(None), F.text)
async def handle_code(message: Message):
    await update_user_online(message.from_user.id)
    if await is_banned(message.from_user.id):
        return await message.answer("🚫 Вы заблокированы в боте.")
    if not await check_sub(message.from_user.id):
        return await message.answer("Сначала подпишись на канал!", reply_markup=subscribe_kb())
    code = message.text.strip()
    movie = await get_movie(code)
    if not movie:
        return await message.answer("❌ Код не найден.")
    await increment_requests()
    
    reviews_count = await get_reviews_count(code)
    has_reviews = reviews_count > 0
    
    tpz, votes = await get_movie_tpz(code)
    tpz_text = f"⭐ TPZ: {tpz} ({votes} оценок)" if tpz else "⭐ Оценок пока нет"
    
    caption = (
        f"<b>{movie['title']} ({movie['year']})</b>\n\n"
        f"⭐ <b>IMDb:</b> {movie['rating']}\n"
        f"{tpz_text}\n\n"
        f"{movie['description']}"
    )
    await message.answer_photo(photo=movie["poster"], caption=caption, parse_mode="HTML", reply_markup=movie_review_kb(code, has_reviews))


# ==================== FASTAPI ====================
templates = Jinja2Templates(directory="templates")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(dp.start_polling(bot))
    logger.info("Бот и админ-панель запущены")
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# ---------- Вспомогательные функции ----------
def get_user_id_from_cookie(request: Request) -> Optional[int]:
    user_id_str = request.cookies.get("user_id")
    if user_id_str and user_id_str.isdigit():
        return int(user_id_str)
    return None

async def check_auth(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    return user_id

async def check_admin(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id or not await is_admin(user_id):
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    return user_id

async def check_super_admin(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id or not await is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Только суперадмин")
    await update_user_online(user_id)
    return user_id

def get_user_or_none(request: Request) -> Optional[int]:
    return get_user_id_from_cookie(request)

# ---------- Роуты входа ----------
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "BOT_TOKEN": BOT_TOKEN,
        "BOT_ID": BOT_ID,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID
    })

@app.post("/auth/request-code")
async def request_code(request: Request, user_id: int = Form(...)):
    if not await is_user_exists(user_id):
        await add_user(user_id)
    if user_id in verification_codes:
        stored = verification_codes[user_id]
        if datetime.now() < stored["expires_at"]:
            return JSONResponse(status_code=400, content={"error": "Код уже отправлен. Проверьте Telegram."})
    success = await send_verification_code(user_id)
    if success:
        return {"success": True, "message": "Код отправлен в Telegram"}
    else:
        return JSONResponse(status_code=500, content={"error": "Не удалось отправить код"})

@app.post("/auth/verify-code")
async def verify_code(request: Request, user_id: int = Form(...), code: str = Form(...)):
    stored = verification_codes.get(user_id)
    if not stored:
        return JSONResponse(status_code=400, content={"error": "Код не запрошен или истёк"})
    if datetime.now() > stored["expires_at"]:
        del verification_codes[user_id]
        return JSONResponse(status_code=400, content={"error": "Код истёк. Запросите новый"})
    if stored["code"] != code:
        return JSONResponse(status_code=400, content={"error": "Неверный код"})
    del verification_codes[user_id]
    user_data = await get_telegram_user(user_id)
    if user_data:
        try:
            await save_user_data(
                user_id,
                user_data.get("username"),
                user_data.get("first_name"),
                user_data.get("last_name"),
                None,
                user_data.get("photo_url"),
                None
            )
        except ValueError:
            # Если username занят, используем ID
            await save_user_data(
                user_id,
                str(user_id),
                user_data.get("first_name"),
                user_data.get("last_name"),
                None,
                user_data.get("photo_url"),
                None
            )
    await update_user_online(user_id)
    response = JSONResponse({"success": True})
    response.set_cookie(key="user_id", value=str(user_id), httponly=True, max_age=60*60*24*7)
    return response

@app.post("/auth/google")
async def google_auth(request: Request, data: dict):
    try:
        email = data.get("email")
        name = data.get("name")
        picture = data.get("picture")
        
        if not email:
            raise HTTPException(status_code=400, detail="Email не указан")
        
        user_id = await get_or_create_user_from_google(email, name, picture)
        
        if user_id in SUPER_ADMIN_IDS:
            await add_admin(user_id)
        
        await update_user_online(user_id)
        response = JSONResponse({"success": True, "user_id": user_id})
        response.set_cookie(key="user_id", value=str(user_id), httponly=True, max_age=60*60*24*7)
        return response
    except Exception as e:
        logger.error(f"Google auth error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/check-auth")
async def check_auth_api(request: Request):
    user_id = get_user_id_from_cookie(request)
    if user_id:
        await update_user_online(user_id)
        is_admin_user = await is_admin(user_id)
        is_super_user = await is_super_admin(user_id)
        unread_count = await get_unread_count(user_id)
        return {"authenticated": True, "user_id": user_id, "is_admin": is_admin_user, "is_super": is_super_user, "unread_count": unread_count}
    return {"authenticated": False}

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("user_id")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=302)
    
    await update_user_online(user_id)
    
    if not await is_user_exists(user_id):
        await add_user(user_id)
        user_data = await get_telegram_user(user_id)
        if user_data:
            try:
                await save_user_data(
                    user_id,
                    user_data.get("username"),
                    user_data.get("first_name"),
                    user_data.get("last_name"),
                    None,
                    user_data.get("photo_url"),
                    None
                )
            except ValueError:
                await save_user_data(
                    user_id,
                    str(user_id),
                    user_data.get("first_name"),
                    user_data.get("last_name"),
                    None,
                    user_data.get("photo_url"),
                    None
                )
    
    is_admin_user = await is_admin(user_id)
    is_super_user = await is_super_admin(user_id)
    unread_count = await get_unread_count(user_id)
    
    async with pool.acquire() as conn:
        movies_count = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests_count = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins_count = await conn.fetchval("SELECT COUNT(*) FROM admins") or 0
        bans_count = await conn.fetchval("SELECT COUNT(*) FROM bans") or 0
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
    
    user_name_data = await get_user_name(user_id)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user_id": user_id,
        "display_name": user_name_data["display_name"],
        "username": user_name_data["username"],
        "photo_url": user_name_data["photo_url"],
        "banner_url": user_name_data["banner_url"],
        "is_admin": is_admin_user,
        "is_super": is_super_user,
        "unread_count": unread_count,
        "stats": {
            "movies": movies_count,
            "requests": requests_count,
            "admins": admins_count,
            "bans": bans_count,
            "users": users_count
        }
    })

# ==================== API ====================

# ---------- Модели ----------
class MovieCreate(BaseModel):
    code: str
    title: str
    year: int
    poster: str
    description: str
    rating: str
    banner: Optional[str] = None
    director: Optional[str] = None
    writers: Optional[str] = None
    genres: Optional[str] = None
    budget: Optional[str] = None
    box_office_us: Optional[str] = None
    box_office_world: Optional[str] = None
    cast_list: Optional[str] = None
    country: Optional[str] = None

class MovieUpdate(BaseModel):
    title: Optional[str] = None
    year: Optional[int] = None
    poster: Optional[str] = None
    description: Optional[str] = None
    rating: Optional[str] = None
    banner: Optional[str] = None
    director: Optional[str] = None
    writers: Optional[str] = None
    genres: Optional[str] = None
    budget: Optional[str] = None
    box_office_us: Optional[str] = None
    box_office_world: Optional[str] = None
    cast_list: Optional[str] = None
    country: Optional[str] = None

class AdminAdd(BaseModel):
    user_id: int

class BanAdd(BaseModel):
    user_id: int
    reason: str = ""
    duration_hours: int = 0

class PunishData(BaseModel):
    user_id: int
    type: str
    reason: str = ""
    duration_hours: int = 0

class ReviewData(BaseModel):
    rating: int
    text: str

class CommentData(BaseModel):
    text: str

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    banner_url: Optional[str] = None

class SupportMessage(BaseModel):
    subject: str
    message: str

class RoleUpdate(BaseModel):
    user_id: int
    role: str

class FriendRequest(BaseModel):
    user_id: int

class MessageData(BaseModel):
    text: str

class ReportData(BaseModel):
    reason: str

class NewsData(BaseModel):
    title: str
    content: str

# ---------- Фильмы ----------
@app.get("/api/movies")
async def api_movies(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, title, year, poster, rating, banner FROM movies ORDER BY code")
        result = []
        for row in rows:
            tpz, votes = await get_movie_tpz(row["code"])
            result.append({
                "code": row["code"],
                "title": row["title"],
                "year": row["year"],
                "poster": row["poster"],
                "rating": row["rating"],
                "banner": row["banner"],
                "tpz": tpz,
                "votes": votes
            })
        return result

@app.get("/api/movies/{code}")
async def api_movie(request: Request, code: str):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM movies WHERE code = $1", code)
        if row:
            tpz, votes = await get_movie_tpz(code)
            result = dict(row)
            result["tpz"] = tpz
            result["votes"] = votes
            return result
        raise HTTPException(status_code=404, detail="Фильм не найден")

@app.post("/api/movies")
async def api_add_movie(request: Request, data: MovieCreate):
    user_id = await check_admin(request)
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO movies (code, title, year, poster, description, rating, banner, added_by, 
                   director, writers, genres, budget, box_office_us, box_office_world, cast_list, country) 
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)""",
                data.code, data.title, data.year, data.poster, data.description, data.rating, data.banner, user_id,
                data.director, data.writers, data.genres, data.budget, data.box_office_us, data.box_office_world, data.cast_list, data.country
            )
            await update_user_online(user_id)
            return {"success": True, "code": data.code}
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Фильм с таким кодом уже существует")
        except Exception as e:
            logger.error(f"Error adding movie: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/movies/{code}")
async def api_update_movie(request: Request, code: str, data: MovieUpdate):
    await check_admin(request)
    async with pool.acquire() as conn:
        for field, value in data.model_dump(exclude_unset=True).items():
            if value is not None:
                await conn.execute(f"UPDATE movies SET {field} = $1 WHERE code = $2", value, code)
        return {"success": True}

@app.delete("/api/movies/{code}")
async def api_delete_movie(request: Request, code: str):
    await check_super_admin(request)
    await delete_movie(code)
    return {"success": True}

# ---------- Отзывы ----------
@app.post("/api/movies/{code}/reviews")
async def api_add_review(request: Request, code: str, data: ReviewData):
    user_id = await check_auth(request)
    existing = await get_user_review(code, user_id)
    if existing:
        raise HTTPException(status_code=400, detail="Вы уже оставляли отзыв на этот фильм")
    await add_review(code, user_id, data.rating, data.text)
    return {"success": True}

@app.get("/api/movies/{code}/reviews")
async def api_get_reviews(request: Request, code: str):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    return await get_reviews(code)

@app.delete("/api/reviews/{review_id}")
async def api_delete_review(request: Request, review_id: int):
    user_id = await check_auth(request)
    is_super = await is_super_admin(user_id)
    if not is_super:
        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT user_id FROM movie_reviews WHERE id = $1", review_id)
            if owner != user_id:
                raise HTTPException(status_code=403, detail="Нельзя удалить чужой отзыв")
    success = await delete_review(review_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Отзыв не найден")
    return {"success": True}

# ---------- Комментарии ----------
@app.delete("/api/profile/comments/{comment_id}")
async def api_delete_profile_comment(request: Request, comment_id: int):
    user_id = await check_auth(request)
    success = await delete_profile_comment(comment_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    return {"success": True}

# ---------- Админы ----------
@app.get("/api/admins")
async def api_admins(request: Request):
    await check_admin(request)
    admins = await get_admins_with_stats()
    result = []
    for a in admins:
        name_data = await get_user_name(a["user_id"])
        is_online = await is_user_online(a["user_id"])
        result.append({
            "user_id": a["user_id"],
            "display_name": name_data["display_name"],
            "username": name_data["username"],
            "photo_url": name_data["photo_url"],
            "is_online": is_online,
            "is_super": name_data.get("is_super", False),
            "movies_count": a["movies_count"],
            "warns": a["warns"]
        })
    return result

@app.post("/api/admins")
async def api_add_admin(request: Request, data: AdminAdd):
    await check_super_admin(request)
    await add_admin(data.user_id)
    return {"success": True}

@app.delete("/api/admins/{user_id}")
async def api_remove_admin(request: Request, user_id: int):
    await check_super_admin(request)
    await remove_admin(user_id)
    return {"success": True}

# ---------- Баны ----------
@app.get("/api/bans")
async def api_bans(request: Request):
    await check_super_admin(request)
    banned = await get_banned_users()
    result = []
    for b in banned:
        name_data = await get_user_name(b["user_id"])
        is_online = await is_user_online(b["user_id"])
        result.append({
            "user_id": b["user_id"],
            "display_name": name_data["display_name"],
            "photo_url": name_data["photo_url"],
            "is_online": is_online,
            "reason": b["reason"],
            "expires_at": b["expires_at"]
        })
    return result

@app.post("/api/bans")
async def api_add_ban(request: Request, data: BanAdd):
    await check_super_admin(request)
    await ban_user(data.user_id, data.reason, data.duration_hours)
    return {"success": True}

@app.delete("/api/bans/{user_id}")
async def api_remove_ban(request: Request, user_id: int):
    await check_super_admin(request)
    await unban_user(user_id)
    return {"success": True}

# ---------- Профили ----------
@app.get("/api/profile/{user_id}")
async def api_profile(request: Request, user_id: int):
    current_user = get_user_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(current_user)
    return await get_user_profile(user_id)

@app.put("/api/profile/{user_id}")
async def api_update_profile(request: Request, user_id: int, data: ProfileUpdate):
    current_user = await check_auth(request)
    if current_user != user_id:
        raise HTTPException(status_code=403, detail="Нельзя редактировать чужой профиль")
    async with pool.acquire() as conn:
        if data.display_name is not None:
            await conn.execute("UPDATE user_names SET display_name = $1 WHERE user_id = $2", data.display_name, user_id)
        if data.username is not None:
            try:
                await conn.execute("UPDATE user_names SET username = $1 WHERE user_id = $2", data.username, user_id)
                await conn.execute("UPDATE users SET username = $1 WHERE user_id = $2", data.username, user_id)
            except asyncpg.UniqueViolationError:
                raise HTTPException(status_code=400, detail="Этот юзернейм уже занят")
        if data.photo_url is not None:
            await conn.execute("UPDATE user_names SET photo_url = $1 WHERE user_id = $2", data.photo_url, user_id)
            await conn.execute("UPDATE users SET photo_url = $1 WHERE user_id = $2", data.photo_url, user_id)
        if data.banner_url is not None:
            await conn.execute("UPDATE user_names SET banner_url = $1 WHERE user_id = $2", data.banner_url, user_id)
        await conn.execute("UPDATE user_names SET updated_at = NOW() WHERE user_id = $1", user_id)
    await update_user_online(user_id)
    return {"success": True}

# ---------- Наказания ----------
@app.post("/api/punish")
async def api_punish(request: Request, data: PunishData):
    issued_by = await check_super_admin(request)
    if await is_super_admin(data.user_id):
        raise HTTPException(status_code=403, detail="Нельзя наказывать суперадмина")
    await add_punishment(data.user_id, data.type, data.reason, issued_by, data.duration_hours)
    return {"success": True}

@app.post("/api/punish/{punishment_id}/resolve")
async def api_resolve_punishment(request: Request, punishment_id: int):
    resolved_by = await check_super_admin(request)
    success = await resolve_punishment(punishment_id, resolved_by)
    if not success:
        raise HTTPException(status_code=404, detail="Наказание не найдено")
    return {"success": True}

# ---------- Комментарии к профилям ----------
@app.get("/api/profile/{user_id}/comments")
async def api_get_profile_comments(request: Request, user_id: int):
    current_user = get_user_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(current_user)
    comments = await get_profile_comments(user_id)
    result = []
    for c in comments:
        user_data = await get_user_name(c["author_id"])
        c["author_display_name"] = user_data["display_name"]
        c["author_photo_url"] = user_data["photo_url"]
        c["author_is_super"] = user_data.get("is_super", False)
        c["author_is_admin"] = user_data.get("is_admin", False)
        result.append(c)
    return result

@app.post("/api/profile/{user_id}/comments")
async def api_add_profile_comment(request: Request, user_id: int, data: CommentData):
    author_id = await check_auth(request)
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Текст комментария обязателен")
    await add_profile_comment(user_id, author_id, data.text.strip())
    return {"success": True}

# ---------- Имена ----------
@app.get("/api/user/{user_id}/name")
async def api_user_name(request: Request, user_id: int):
    current_user = get_user_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(current_user)
    name_data = await get_user_name(user_id)
    return {
        "display_name": name_data["display_name"],
        "username": name_data["username"],
        "photo_url": name_data["photo_url"],
        "banner_url": name_data["banner_url"],
        "is_super": name_data.get("is_super", False),
        "is_admin": name_data.get("is_admin", False)
    }

# ---------- Пользователи ----------
@app.get("/api/users")
async def api_users(request: Request):
    await check_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, u.first_name, u.last_name, u.photo_url, u.created_at, u.is_online,
                   CASE WHEN a.user_id IS NOT NULL THEN true ELSE false END as is_admin,
                   CASE WHEN sa.user_id IS NOT NULL THEN true ELSE false END as is_super
            FROM users u
            LEFT JOIN admins a ON u.user_id = a.user_id
            LEFT JOIN super_admins sa ON u.user_id = sa.user_id
            ORDER BY u.created_at DESC
        """)
        result = []
        for row in rows:
            name_data = await get_user_name(row["user_id"])
            result.append({
                "user_id": row["user_id"],
                "display_name": name_data["display_name"],
                "username": name_data["username"],
                "photo_url": name_data["photo_url"],
                "is_admin": row["is_admin"],
                "is_super": row["is_super"],
                "is_online": row["is_online"] or False,
                "created_at": row["created_at"]
            })
        return result

# ---------- Роли ----------
@app.post("/api/roles")
async def api_update_role(request: Request, data: RoleUpdate):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        if data.role == 'super_admin':
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", data.user_id)
            await conn.execute("INSERT INTO super_admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", data.user_id)
        elif data.role == 'admin':
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", data.user_id)
            await conn.execute("DELETE FROM super_admins WHERE user_id = $1", data.user_id)
        else:
            await conn.execute("DELETE FROM admins WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM super_admins WHERE user_id = $1", data.user_id)
    return {"success": True}

@app.get("/api/roles")
async def api_get_roles(request: Request):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, u.first_name, u.photo_url,
                   CASE WHEN sa.user_id IS NOT NULL THEN 'super_admin'
                        WHEN a.user_id IS NOT NULL THEN 'admin'
                        ELSE 'user' END as role
            FROM users u
            LEFT JOIN admins a ON u.user_id = a.user_id
            LEFT JOIN super_admins sa ON u.user_id = sa.user_id
            ORDER BY u.created_at DESC
        """)
        return [dict(row) for row in rows]

# ---------- Поддержка ----------
@app.post("/api/support")
async def api_support(request: Request, data: SupportMessage):
    user_id = await check_auth(request)
    name_data = await get_user_name(user_id)
    display_name = name_data["display_name"]
    for admin_id in SUPER_ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆘 <b>Новое сообщение в поддержку!</b>\n\n"
                f"👤 <b>От:</b> {display_name} (<code>{user_id}</code>)\n"
                f"📋 <b>Тема:</b> {data.subject}\n"
                f"💬 <b>Сообщение:</b>\n{data.message}",
                parse_mode="HTML"
            )
        except:
            pass
    return {"success": True}

# ---------- Статистика ----------
@app.get("/api/stats")
async def api_stats(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    
    async with pool.acquire() as conn:
        movies = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins = await conn.fetchval("SELECT COUNT(*) FROM admins") or 0
        bans = await conn.fetchval("SELECT COUNT(*) FROM bans") or 0
        users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
    return {"movies": movies, "requests": requests, "admins": admins, "bans": bans, "users": users}

# ---------- Друзья ----------
@app.post("/api/friends/request")
async def api_send_friend_request(request: Request, data: FriendRequest):
    user_id = await check_auth(request)
    if user_id == data.user_id:
        raise HTTPException(status_code=400, detail="Нельзя добавить себя в друзья")
    success, message = await send_friend_request(user_id, data.user_id)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message}

@app.post("/api/friends/accept/{friend_id}")
async def api_accept_friend_request(request: Request, friend_id: int):
    user_id = await check_auth(request)
    success = await accept_friend_request(user_id, friend_id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось принять запрос")
    return {"success": True}

@app.post("/api/friends/decline/{friend_id}")
async def api_decline_friend_request(request: Request, friend_id: int):
    user_id = await check_auth(request)
    success = await decline_friend_request(user_id, friend_id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось отклонить запрос")
    return {"success": True}

@app.get("/api/friends")
async def api_get_friends(request: Request):
    user_id = await check_auth(request)
    return await get_friends(user_id)

@app.get("/api/friends/requests")
async def api_get_friend_requests(request: Request):
    user_id = await check_auth(request)
    return await get_friend_requests(user_id)

@app.get("/api/search")
async def api_search_users(request: Request, q: str):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if not q or len(q) < 2:
        return []
    return await search_users(q)

# ---------- Уведомления ----------
@app.get("/api/notifications")
async def api_get_notifications(request: Request, unread_only: bool = False):
    user_id = await check_auth(request)
    return await get_notifications(user_id, unread_only)

@app.post("/api/notifications/read/{notification_id}")
async def api_mark_notification_read(request: Request, notification_id: int):
    user_id = await check_auth(request)
    await mark_notification_read(notification_id)
    return {"success": True}

@app.post("/api/notifications/read-all")
async def api_mark_all_notifications_read(request: Request):
    user_id = await check_auth(request)
    await mark_all_notifications_read(user_id)
    return {"success": True}

@app.get("/api/notifications/unread")
async def api_get_unread_count(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        return {"count": 0}
    return {"count": await get_unread_count(user_id)}

# ---------- Новости ----------
@app.get("/api/news")
async def api_get_news(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online(user_id)
    return await get_news()

@app.post("/api/news")
async def api_create_news(request: Request, data: NewsData):
    user_id = await check_super_admin(request)
    await create_news(data.title, data.content, user_id)
    return {"success": True}

# ---------- Сообщения (чат) ----------
@app.post("/api/messages/{receiver_id}")
async def api_send_message(request: Request, receiver_id: int, data: MessageData):
    user_id = await check_auth(request)
    if user_id == receiver_id:
        raise HTTPException(status_code=400, detail="Нельзя отправить сообщение себе")
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")
    await send_message(user_id, receiver_id, data.text.strip())
    return {"success": True}

@app.get("/api/messages/{other_user_id}")
async def api_get_messages(request: Request, other_user_id: int):
    user_id = await check_auth(request)
    return await get_messages(user_id, other_user_id)

@app.get("/api/chats")
async def api_get_chats(request: Request):
    user_id = await check_auth(request)
    return await get_chat_users(user_id)

# ---------- Жалобы ----------
@app.post("/api/report/{user_id}")
async def api_report_user(request: Request, user_id: int, data: ReportData):
    reporter_id = await check_auth(request)
    if reporter_id == user_id:
        raise HTTPException(status_code=400, detail="Нельзя пожаловаться на себя")
    if not data.reason or not data.reason.strip():
        raise HTTPException(status_code=400, detail="Укажите причину жалобы")
    await create_report(reporter_id, user_id, data.reason.strip())
    return {"success": True}


# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
