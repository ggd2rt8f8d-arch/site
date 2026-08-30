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
BOT_USERNAME = os.getenv("BOT_USERNAME", "topzfilmz_bot")
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

# ==================== ХРАНИЛИЩЕ СЕССИЙ АВТОРИЗАЦИИ ====================
auth_sessions = {}

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                author_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
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
                "is_admin": is_admin_user,
                "is_online": await is_user_online(user_id)
            }
        return {
            "display_name": f"Пользователь {user_id}",
            "username": str(user_id),
            "photo_url": None,
            "banner_url": None,
            "first_name": None,
            "last_name": None,
            "is_super": is_super,
            "is_admin": is_admin_user,
            "is_online": False
        }

async def get_user_profile(user_id: int):
    async with pool.acquire() as conn:
        user_name_exists = await conn.fetchval("SELECT 1 FROM user_names WHERE user_id = $1", user_id)
        if not user_name_exists:
            google_user = await conn.fetchrow("SELECT * FROM google_users WHERE user_id = $1", user_id)
            if google_user:
                username = google_user["name"].lower().replace(" ", "_")
                existing = await conn.fetchval("SELECT user_id FROM user_names WHERE username = $1", username)
                if existing:
                    username = f"{username}_{user_id}"
                await conn.execute(
                    """INSERT INTO user_names (user_id, username, first_name, last_name, display_name, photo_url) 
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    user_id, username, google_user["name"], None, google_user["name"], google_user["picture"]
                )
                user_exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id)
                if not user_exists:
                    await conn.execute(
                        "INSERT INTO users (user_id, username, first_name, last_name, photo_url, last_seen, is_online) VALUES ($1, $2, $3, $4, $5, NOW(), TRUE)",
                        user_id, username, google_user["name"], None, google_user["picture"]
                    )
            else:
                user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
                if user:
                    username = user["username"] or str(user_id)
                    await conn.execute(
                        """INSERT INTO user_names (user_id, username, first_name, last_name, display_name, photo_url) 
                           VALUES ($1, $2, $3, $4, $5, $6)""",
                        user_id, username, user["first_name"], user["last_name"], user["first_name"] or f"Пользователь {user_id}", user["photo_url"]
                    )
                else:
                    return None
        
        is_admin_user = await is_admin(user_id)
        is_super_user = await is_super_admin(user_id)
        is_banned_user = await is_banned(user_id)
        is_online = await is_user_online(user_id)
        
        movies_count = await conn.fetchval("SELECT movies_added FROM admin_stats WHERE user_id = $1", user_id) or 0
        warns = await conn.fetchval("SELECT COUNT(*) FROM punishments WHERE user_id = $1 AND type = 'warning' AND resolved = FALSE", user_id) or 0
        punishments = await conn.fetch("SELECT * FROM punishments WHERE user_id = $1 ORDER BY created_at DESC", user_id)
        user_name_data = await get_user_name(user_id)
        reviews_count = await conn.fetchval("SELECT COUNT(*) FROM movie_reviews WHERE user_id = $1", user_id) or 0
        friends_count = await conn.fetchval("""
            SELECT COUNT(*) FROM friends 
            WHERE (user_id = $1 OR friend_id = $1) AND status = 'accepted'
        """, user_id) or 0
        
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
            "punishments": [dict(p) for p in punishments],
            "friends_count": friends_count
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
            user_exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id)
            if not user_exists:
                username = email.split('@')[0]
                existing = await conn.fetchval("SELECT user_id FROM users WHERE username = $1", username)
                if existing:
                    username = f"{username}_{random.randint(100, 999)}"
                await conn.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name, photo_url, last_seen, is_online) VALUES ($1, $2, $3, $4, $5, NOW(), TRUE)",
                    user_id, username, name, None, picture
                )
                await save_user_data(user_id, username, name, None, name, picture, None)
            await update_user_online(user_id)
            return user_id
        
        user_id = random.randint(100000000, 999999999)
        username = email.split('@')[0]
        existing = await conn.fetchval("SELECT user_id FROM users WHERE username = $1", username)
        if existing:
            username = f"{username}_{random.randint(100, 999)}"
        
        await conn.execute(
            "INSERT INTO google_users (email, user_id, name, picture) VALUES ($1, $2, $3, $4)",
            email, user_id, name, picture
        )
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, photo_url, last_seen, is_online) VALUES ($1, $2, $3, $4, $5, NOW(), TRUE)",
            user_id, username, name, None, picture
        )
        await save_user_data(user_id, username, name, None, name, picture, None)
        return user_id

# ---------- Друзья ----------
async def send_friend_request(user_id: int, friend_id: int):
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM friends WHERE user_id = $1 AND friend_id = $2",
            user_id, friend_id
        )
        if existing:
            return False, "Запрос уже отправлен"
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

async def get_sent_friend_requests(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.friend_id, f.status, u.username, u.photo_url
            FROM friends f
            JOIN users u ON f.friend_id = u.user_id
            WHERE f.user_id = $1 AND f.status = 'pending'
        """, user_id)
        return [dict(r) for r in rows]

async def search_users(query: str, current_user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT un.user_id, un.username, un.display_name, un.photo_url, u.is_online
            FROM user_names un
            JOIN users u ON un.user_id = u.user_id
            WHERE (un.username ILIKE $1 OR un.display_name ILIKE $1)
            AND un.user_id != $2
            LIMIT 20
        """, f"%{query}%", current_user_id)
        result = []
        for row in rows:
            is_friend = await conn.fetchval("""
                SELECT id FROM friends 
                WHERE (user_id = $1 AND friend_id = $2 OR user_id = $2 AND friend_id = $1) 
                AND status = 'accepted'
            """, current_user_id, row["user_id"])
            result.append({
                "user_id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "photo_url": row["photo_url"],
                "is_online": row["is_online"] or False,
                "is_friend": bool(is_friend)
            })
        return result

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
        await conn.execute(
            "INSERT INTO news (title, content, author_id) VALUES ($1, $2, $3)",
            title, content, author_id
        )
        users = await conn.fetch("SELECT user_id FROM users")
        for user in users:
            await conn.execute(
                "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
                user["user_id"], 'news', f'📰 {title}', '/news'
            )
        return True

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

async def delete_news(news_id: int, user_id: int):
    async with pool.acquire() as conn:
        # Проверяем, является ли пользователь автором или суперадмином
        is_author = await conn.fetchval("SELECT 1 FROM news WHERE id = $1 AND author_id = $2", news_id, user_id)
        is_super = await is_super_admin(user_id)
        if is_author or is_super:
            await conn.execute("DELETE FROM news WHERE id = $1", news_id)
            return True
        return False

# ---------- Сообщения ----------
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
                END as other_user_id,
                MAX(created_at) as last_time
            FROM messages
            WHERE sender_id = $1 OR receiver_id = $1
            GROUP BY other_user_id
            ORDER BY last_time DESC
        """, user_id)
        result = []
        for row in rows:
            user_data = await get_user_name(row["other_user_id"])
            user_data["user_id"] = row["other_user_id"]
            user_data["is_online"] = await is_user_online(row["other_user_id"])
            last_msg = await conn.fetchval("""
                SELECT text FROM messages 
                WHERE (sender_id = $1 AND receiver_id = $2) OR (sender_id = $2 AND receiver_id = $1)
                ORDER BY created_at DESC LIMIT 1
            """, user_id, row["other_user_id"])
            user_data["last_message"] = last_msg
            user_data["last_time"] = row["last_time"]
            result.append(user_data)
        return result

# ---------- Жалобы ----------
async def create_report(reporter_id: int, reported_id: int, reason: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reports (reporter_id, reported_id, reason) VALUES ($1, $2, $3)",
            reporter_id, reported_id, reason
        )
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
    
    # Проверяем, есть ли токен в команде
    args = message.text.split()
    if len(args) > 1:
        token = args[1]
        
        # Проверяем, существует ли сессия
        if token in auth_sessions:
            # Обновляем статус сессии
            auth_sessions[token]["status"] = "authenticated"
            auth_sessions[token]["user_id"] = message.from_user.id
            
            # Добавляем пользователя в БД если его нет
            if not await is_user_exists(message.from_user.id):
                user_data = await get_telegram_user(message.from_user.id)
                if user_data:
                    try:
                        await save_user_data(
                            message.from_user.id,
                            user_data.get("username"),
                            user_data.get("first_name"),
                            user_data.get("last_name"),
                            None,
                            user_data.get("photo_url"),
                            None
                        )
                    except ValueError:
                        await save_user_data(
                            message.from_user.id,
                            str(message.from_user.id),
                            user_data.get("first_name"),
                            user_data.get("last_name"),
                            None,
                            user_data.get("photo_url"),
                            None
                        )
            
            # Проверяем, является ли пользователь админом
            if await is_admin(message.from_user.id):
                await message.answer(
                    "✅ <b>Авторизация успешна!</b>\n\n"
                    "Вы вошли как администратор.\n"
                    "Вы можете закрыть это окно и вернуться на сайт.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌐 Перейти на сайт", url="https://ваш-сайт.com/dashboard")]
                    ])
                )
            else:
                await message.answer(
                    "✅ <b>Авторизация успешна!</b>\n\n"
                    "Вы можете закрыть это окно и вернуться на сайт.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌐 Перейти на сайт", url="https://ваш-сайт.com/dashboard")]
                    ])
                )
            return
    
    # Обычный /start без токена
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

@dp.callback_query(F.data == "admin_close")
async def cb_close(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data == "admin_back")
async def cb_back(callback: CallbackQuery):
    await callback.message.edit_text("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_main_kb(callback.from_user.id))

@dp.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    movies_count = await get_movies_count()
    requests_count = await get_total_requests()
    text = f"📊 <b>Статистика бота</b>\n\n🎬 Фильмов в базе: <b>{movies_count}</b>\n🔍 Всего запросов: <b>{requests_count}</b>"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))

@dp.callback_query(F.data == "admin_list")
async def cb_list(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    movies = await get_all_movies()
    if not movies:
        await callback.message.edit_text("База пустая.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
        ]))
        return
    buttons = []
    for code, title, year in movies:
        buttons.append([InlineKeyboardButton(text=f"{code} — {title} ({year})", callback_data=f"movie:{code}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await callback.message.edit_text("📋 <b>Список фильмов:</b>\nВыбери фильм:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("movie:"))
async def cb_movie(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    movie = await get_movie(code)
    if not movie:
        await callback.answer("Фильм не найден", show_alert=True)
        return
    
    reviews_count = await get_reviews_count(code)
    has_reviews = reviews_count > 0
    
    tpz, votes = await get_movie_tpz(code)
    tpz_text = f"⭐ TPZ: {tpz} ({votes} оценок)" if tpz else "⭐ Оценок пока нет"
    
    text = (
        f"<b>{movie['title']} ({movie['year']})</b>\n"
        f"Код: <code>{movie['code']}</code>\n"
        f"IMDb: {movie['rating']}\n"
        f"{tpz_text}\n\n"
        f"{movie['description'][:180]}..."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=movie_review_kb(code, has_reviews)
    )

@dp.callback_query(F.data.startswith("reviews:"))
async def cb_reviews(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    reviews = await get_reviews(code, 3)
    
    if not reviews:
        await callback.message.edit_text(
            "📝 Отзывов пока нет. Будь первым!",
            reply_markup=review_back_kb(code)
        )
        return
    
    for review in reviews[:3]:
        user_name_data = await get_user_name(review["user_id"])
        display_name = user_name_data["display_name"]
        is_super = user_name_data.get("is_super", False)
        name_html = f'<span style="color:#f5a623;font-weight:bold;">{display_name}</span>' if is_super else display_name
        review_text = (
            f"⭐ <b>{review['rating']}/10</b>\n"
            f"👤 {name_html}\n"
            f"💬 {review['text']}\n"
            f"🕐 {review['created_at'].strftime('%d.%m.%Y %H:%M')}"
        )
        await callback.message.answer(review_text, parse_mode="HTML")
    
    movie = await get_movie(code)
    tpz, votes = await get_movie_tpz(code)
    tpz_text = f"⭐ TPZ: {tpz} ({votes} оценок)" if tpz else "⭐ Оценок пока нет"
    
    text = (
        f"<b>{movie['title']} ({movie['year']})</b>\n"
        f"Код: <code>{movie['code']}</code>\n"
        f"IMDb: {movie['rating']}\n"
        f"{tpz_text}\n\n"
        f"{movie['description'][:180]}..."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=review_back_kb(code)
    )

@dp.callback_query(F.data.startswith("write_review:"))
async def cb_write_review(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    await state.update_data(movie_code=code)
    await callback.message.edit_text(
        "✏️ Напишите ваш отзыв на фильм (текст):",
        reply_markup=review_only_back_kb(code)
    )
    await state.set_state(ReviewState.waiting_review)

@dp.message(ReviewState.waiting_review)
async def process_review_text(message: Message, state: FSMContext):
    await state.update_data(review_text=message.text.strip())
    data = await state.get_data()
    code = data.get("movie_code")
    await message.answer("⭐ Оцените фильм от 1 до 10:\n(Просто напишите число)")
    await state.set_state(ReviewState.waiting_rating)

@dp.message(ReviewState.waiting_rating)
async def process_review_rating(message: Message, state: FSMContext):
    try:
        rating = int(message.text.strip())
        if rating < 1 or rating > 10:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 1 до 10:")
        return
    
    data = await state.get_data()
    code = data.get("movie_code")
    review_text = data.get("review_text")
    
    await add_review(code, message.from_user.id, rating, review_text)
    await state.clear()
    
    movie = await get_movie(code)
    tpz, votes = await get_movie_tpz(code)
    tpz_text = f"⭐ TPZ: {tpz} ({votes} оценок)" if tpz else "⭐ Оценок пока нет"
    
    text = f"✅ Отзыв добавлен!\n\n<b>{movie['title']} ({movie['year']})</b>\n{tpz_text}"
    await message.answer(text, parse_mode="HTML", reply_markup=movie_review_kb(code, True))

@dp.callback_query(F.data.startswith("movie_back:"))
async def cb_movie_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    code = callback.data.split(":", 1)[1]
    movie = await get_movie(code)
    reviews_count = await get_reviews_count(code)
    has_reviews = reviews_count > 0
    
    tpz, votes = await get_movie_tpz(code)
    tpz_text = f"⭐ TPZ: {tpz} ({votes} оценок)" if tpz else "⭐ Оценок пока нет"
    
    text = (
        f"<b>{movie['title']} ({movie['year']})</b>\n"
        f"Код: <code>{movie['code']}</code>\n"
        f"IMDb: {movie['rating']}\n"
        f"{tpz_text}\n\n"
        f"{movie['description'][:180]}..."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=movie_review_kb(code, has_reviews)
    )

@dp.callback_query(F.data == "admin_add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text("Введи <b>код</b> фильма:", parse_mode="HTML")
    await state.set_state(AddMovie.code)

@dp.message(AddMovie.code)
async def add_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if await get_movie(code):
        return await message.answer("Такой код уже есть. Введи другой:")
    await state.update_data(code=code)
    await message.answer("Введи <b>название</b>:", parse_mode="HTML")
    await state.set_state(AddMovie.title)

@dp.message(AddMovie.title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Введи <b>год</b>:", parse_mode="HTML")
    await state.set_state(AddMovie.year)

@dp.message(AddMovie.year)
async def add_year(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        return await message.answer("Год должен быть числом:")
    await state.update_data(year=int(message.text.strip()))
    await message.answer("Отправь <b>обложку</b>:\n• Фотографию\n• Или прямую ссылку", parse_mode="HTML")
    await state.set_state(AddMovie.poster)

@dp.message(AddMovie.poster)
async def add_poster(message: Message, state: FSMContext):
    if message.photo:
        poster = message.photo[-1].file_id
    elif message.text:
        poster = message.text.strip()
    else:
        return await message.answer("Отправь фото или ссылку:")
    await state.update_data(poster=poster)
    await message.answer("Отправь <b>баннер</b> (широкое изображение):", parse_mode="HTML")
    await state.set_state(AddMovie.banner)

@dp.message(AddMovie.banner)
async def add_banner(message: Message, state: FSMContext):
    if message.photo:
        banner = message.photo[-1].file_id
    elif message.text:
        banner = message.text.strip()
    else:
        return await message.answer("Отправь фото или ссылку для баннера:")
    await state.update_data(banner=banner)
    await message.answer("Краткое <b>описание</b>:", parse_mode="HTML")
    await state.set_state(AddMovie.description)

@dp.message(AddMovie.description)
async def add_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("Оценка <b>IMDb</b>:", parse_mode="HTML")
    await state.set_state(AddMovie.rating)

@dp.message(AddMovie.rating)
async def add_rating(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        await add_movie_to_db(
            data["code"], data["title"], data["year"],
            data["poster"], data["description"], message.text.strip(),
            data["banner"], message.from_user.id
        )
        await state.clear()
        count = await get_movies_count()
        await message.answer(f"✅ Фильм <b>{data['title']}</b> добавлен!\nВсего фильмов в базе: <b>{count}</b>", parse_mode="HTML")
    except asyncpg.UniqueViolationError:
        await message.answer("❌ Такой код уже существует. Попробуй другой.")

@dp.callback_query(F.data.startswith("edit_"))
async def cb_edit_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":", 1)
    field = parts[0].replace("edit_", "")
    code = parts[1]
    await state.update_data(edit_code=code, edit_field=field)
    await callback.message.edit_text(f"Введи новое значение для <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(EditMovie.waiting_value)

@dp.message(EditMovie.waiting_value)
async def process_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    code = data["edit_code"]
    field = data["edit_field"]
    if field == "poster":
        if message.photo:
            value = message.photo[-1].file_id
        elif message.text:
            value = message.text.strip()
        else:
            return await message.answer("Отправь фото или ссылку:")
    else:
        if not message.text:
            return await message.answer("Отправь текстом:")
        value = message.text.strip()
        if field == "year":
            if not value.isdigit():
                return await message.answer("Год должен быть числом:")
            value = int(value)
    await update_movie_field(code, field, value)
    await state.clear()
    await message.answer("✅ Обновлено!")
    movie = await get_movie(code)
    if movie:
        text = f"<b>{movie['title']} ({movie['year']})</b>\nКод: <code>{movie['code']}</code>\nIMDb: {movie['rating']}"
        await message.answer(text, parse_mode="HTML", reply_markup=movie_review_kb(code, True))

@dp.callback_query(F.data.startswith("delete_movie:"))
async def cb_delete_movie(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    code = callback.data.split(":", 1)[1]
    await delete_movie(code)
    await callback.answer("Фильм удалён ✅")
    await cb_list(callback)

@dp.callback_query(F.data == "admin_admins")
async def cb_admins(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    admins = await get_admins_with_stats()
    text = "👥 <b>Обычные админы:</b>\n\n"
    if not admins:
        text += "Пока нет."
    else:
        for a in admins:
            name_data = await get_user_name(a["user_id"])
            display_name = name_data["display_name"]
            text += f"👤 {display_name} (<code>{a['user_id']}</code>)"
            text += f" — 🎬 {a['movies_count']} | ⚠️ {a['warns']}/3\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Назначить админа", callback_data="add_admin")],
        [InlineKeyboardButton(text="➖ Снять админа", callback_data="remove_admin")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_admin")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Введи Telegram ID нового админа:")
    await state.set_state(AddAdmin.waiting_id)
    await state.update_data(action="add")

@dp.callback_query(F.data == "remove_admin")
async def cb_remove_admin(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Введи Telegram ID админа для снятия:")
    await state.set_state(AddAdmin.waiting_id)
    await state.update_data(action="remove")

@dp.message(AddAdmin.waiting_id)
async def process_admin(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("ID должен быть числом. Попробуй ещё раз:")
    uid = int(message.text.strip())
    data = await state.get_data()
    await state.clear()
    if data.get("action") == "add":
        await add_admin(uid)
        await message.answer(f"✅ <code>{uid}</code> теперь админ", parse_mode="HTML")
    else:
        await remove_admin(uid)
        await message.answer(f"✅ <code>{uid}</code> снят с админки", parse_mode="HTML")

@dp.callback_query(F.data == "admin_bans")
async def cb_bans(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="ban_user")],
        [InlineKeyboardButton(text="✅ Разбанить", callback_data="unban_user")],
        [InlineKeyboardButton(text="📋 Список забаненных", callback_data="list_bans")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text("🚫 <b>Управление банами</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "list_bans")
async def cb_list_bans(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return
    banned = await get_banned_users()
    if not banned:
        text = "Список банов пуст."
    else:
        text = "🚫 <b>Забаненные:</b>\n\n"
        for b in banned:
            name_data = await get_user_name(b["user_id"])
            display_name = name_data["display_name"]
            text += f"👤 {display_name} (<code>{b['user_id']}</code>)"
            if b["reason"]:
                text += f" — {b['reason']}"
            if b["expires_at"]:
                text += f" ⏳ до {b['expires_at']}"
            text += "\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_bans")]
    ]))

@dp.callback_query(F.data == "ban_user")
async def cb_ban(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Введи Telegram ID для бана:")
    await state.set_state(BanUser.waiting_id)
    await state.update_data(action="ban")

@dp.callback_query(F.data == "unban_user")
async def cb_unban(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Введи Telegram ID для разбана:")
    await state.set_state(BanUser.waiting_id)
    await state.update_data(action="unban")

@dp.message(BanUser.waiting_id)
async def process_ban(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("ID должен быть числом:")
    uid = int(message.text.strip())
    data = await state.get_data()
    await state.clear()
    if data.get("action") == "ban":
        await ban_user(uid)
        await message.answer(f"🚫 <code>{uid}</code> забанен", parse_mode="HTML")
    else:
        await unban_user(uid)
        await message.answer(f"✅ <code>{uid}</code> разбанен", parse_mode="HTML")

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

# ==================== АВТОРИЗАЦИЯ ЧЕРЕЗ БОТА ====================

@app.post("/api/auth/init")
async def auth_init(request: Request, data: dict):
    """Инициализация авторизации через бота"""
    token = data.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Token required")
    
    # Сохраняем токен со статусом 'waiting'
    auth_sessions[token] = {
        "status": "waiting",
        "created_at": datetime.now(),
        "user_id": None
    }
    
    # Автоматически очищаем старые сессии (старше 10 минут)
    expired_tokens = []
    for t, session in auth_sessions.items():
        if datetime.now() - session["created_at"] > timedelta(minutes=10):
            expired_tokens.append(t)
    for t in expired_tokens:
        del auth_sessions[t]
    
    logger.info(f"Auth session initialized: {token}")
    return {"success": True}

@app.post("/api/auth/status")
async def auth_status(request: Request, data: dict):
    """Проверка статуса авторизации"""
    token = data.get("token")
    if not token:
        return {"status": "error", "error": "Token required"}
    
    session = auth_sessions.get(token)
    if not session:
        return {"status": "error", "error": "Session not found"}
    
    # Проверяем срок действия (5 минут)
    if datetime.now() - session["created_at"] > timedelta(minutes=5):
        auth_sessions[token]["status"] = "expired"
        return {"status": "expired"}
    
    if session["status"] == "authenticated":
        return {
            "authenticated": True,
            "user_id": session.get("user_id")
        }
    elif session["status"] == "expired":
        return {"status": "expired"}
    else:
        return {"status": "waiting"}

@app.post("/api/auth/complete")
async def auth_complete(request: Request, data: dict):
    """Завершение авторизации (установка cookie)"""
    user_id = data.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID required")
    
    response = JSONResponse({"success": True})
    response.set_cookie(
        key="user_id",
        value=str(user_id),
        httponly=True,
        max_age=60*60*24*7,
        secure=False,
        samesite="lax"
    )
    return response

# ---------- Роуты входа ----------
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "BOT_TOKEN": BOT_TOKEN,
        "BOT_ID": BOT_ID,
        "BOT_USERNAME": BOT_USERNAME,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID
    })

@app.post("/auth/request-code")
async def request_code(request: Request, user_id: int = Form(...)):
    # Устаревший метод, оставлен для совместимости
    if not await is_user_exists(user_id):
        await add_user(user_id)
    return JSONResponse(status_code=400, content={"error": "Используйте вход через бота"})

@app.post("/auth/verify-code")
async def verify_code(request: Request, user_id: int = Form(...), code: str = Form(...)):
    # Устаревший метод, оставлен для совместимости
    return JSONResponse(status_code=400, content={"error": "Используйте вход через бота"})

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
    
    profile = await get_user_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    return profile

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
        "is_admin": name_data.get("is_admin", False),
        "is_online": name_data.get("is_online", False)
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

@app.get("/api/friends/sent")
async def api_get_sent_friend_requests(request: Request):
    user_id = await check_auth(request)
    return await get_sent_friend_requests(user_id)

@app.get("/api/search")
async def api_search_users(request: Request, q: str):
    user_id = get_user_id_from_cookie(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if not q or len(q) < 2:
        return []
    return await search_users(q, user_id)

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

@app.delete("/api/news/{news_id}")
async def api_delete_news(request: Request, news_id: int):
    user_id = await check_super_admin(request)
    success = await delete_news(news_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Новость не найдена или нет прав")
    return {"success": True}

# ---------- Сообщения ----------
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
