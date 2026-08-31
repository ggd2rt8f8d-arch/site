import os
import asyncio
import logging
import asyncpg
import random
import json
# ==================== EMAIL ====================
import socket
import ssl
import smtplib
import secrets
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr

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
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000")

# Email настройки
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

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

# ==================== ХРАНИЛИЩА ====================
auth_sessions = {}
email_verification_codes = {}
movie_id_counter = 10000001

# ==================== EMAIL ====================
def send_verification_email(email: str, code: str):
    """Отправка кода подтверждения на email (упрощенная версия для Railway)"""
    # На Railway SMTP часто заблокирован, поэтому просто логируем код
    logger.info(f"📧 [RAILWAY] Код для {email}: {code}")
    
    # Пытаемся отправить через SMTP если настроен
    if SMTP_USER and SMTP_PASSWORD:
        try:
            import socket
            import ssl
            
            logger.info(f"Попытка отправки email на {email} через {SMTP_HOST}:{SMTP_PORT}")
            
            msg = MIMEMultipart('alternative')
            msg['From'] = SMTP_FROM
            msg['To'] = email
            msg['Subject'] = 'Код подтверждения — Movie Admin'
            
            text = f"Код подтверждения регистрации: {code}\n\nКод действителен 10 минут."
            
            html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; background: #0d0d0d; color: #d4d4d4; padding: 40px;">
                <div style="max-width: 500px; margin: 0 auto; background: #1a1a1a; border-radius: 16px; padding: 30px; border: 1px solid #2a2a2a;">
                    <h1 style="color: #e8e8e8; font-weight: 300; text-align: center;">Movie Admin</h1>
                    <p style="color: #ccc; text-align: center;">Код подтверждения регистрации</p>
                    <div style="background: #0d0d0d; border-radius: 12px; padding: 20px; text-align: center; margin: 20px 0;">
                        <span style="font-size: 36px; font-weight: 700; color: #0088cc; letter-spacing: 8px;">{code}</span>
                    </div>
                    <p style="color: #888; font-size: 14px; text-align: center;">Код действителен <b>10 минут</b>.</p>
                </div>
            </body>
            </html>
            """
            
            part1 = MIMEText(text, 'plain')
            part2 = MIMEText(html, 'html')
            msg.attach(part1)
            msg.attach(part2)
            
            # Пробуем подключиться с таймаутом
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
            
            logger.info(f"✅ Email успешно отправлен на {email}")
            return True
        except socket.timeout:
            logger.warning(f"⏰ Таймаут SMTP, код для {email}: {code}")
            return False
        except Exception as e:
            logger.warning(f"⚠️ SMTP ошибка: {e}, код для {email}: {code}")
            return False
    
    return True  # Возвращаем True, так как код сохранен в лог

# ==================== БАЗА ДАННЫХ — ИНИЦИАЛИЗАЦИЯ ====================
async def init_db():
    global pool, movie_id_counter
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        # Таблица пользователей с Movie ID
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                movie_id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                first_name TEXT,
                last_name TEXT,
                photo_url TEXT,
                banner_url TEXT,
                is_verified BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                is_super_admin BOOLEAN DEFAULT FALSE,
                is_banned BOOLEAN DEFAULT FALSE,
                ban_reason TEXT,
                ban_expires_at TIMESTAMP,
                last_seen TIMESTAMP DEFAULT NOW(),
                is_online BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Проверяем существование колонок
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='movie_id') THEN
                    ALTER TABLE users ADD COLUMN movie_id INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='email') THEN
                    ALTER TABLE users ADD COLUMN email TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='username') THEN
                    ALTER TABLE users ADD COLUMN username TEXT UNIQUE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='password_hash') THEN
                    ALTER TABLE users ADD COLUMN password_hash TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='display_name') THEN
                    ALTER TABLE users ADD COLUMN display_name TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='first_name') THEN
                    ALTER TABLE users ADD COLUMN first_name TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_name') THEN
                    ALTER TABLE users ADD COLUMN last_name TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='photo_url') THEN
                    ALTER TABLE users ADD COLUMN photo_url TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='banner_url') THEN
                    ALTER TABLE users ADD COLUMN banner_url TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_verified') THEN
                    ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_admin') THEN
                    ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_super_admin') THEN
                    ALTER TABLE users ADD COLUMN is_super_admin BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_banned') THEN
                    ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='ban_reason') THEN
                    ALTER TABLE users ADD COLUMN ban_reason TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='ban_expires_at') THEN
                    ALTER TABLE users ADD COLUMN ban_expires_at TIMESTAMP;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_seen') THEN
                    ALTER TABLE users ADD COLUMN last_seen TIMESTAMP DEFAULT NOW();
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_online') THEN
                    ALTER TABLE users ADD COLUMN is_online BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='created_at') THEN
                    ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT NOW();
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='updated_at') THEN
                    ALTER TABLE users ADD COLUMN updated_at TIMESTAMP DEFAULT NOW();
                END IF;
            END $$;
        """)
        
        # Создаем остальные таблицы
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                year INTEGER,
                poster TEXT,
                description TEXT,
                rating TEXT,
                banner TEXT,
                added_by INTEGER,
                director TEXT,
                writers TEXT,
                genres TEXT,
                budget TEXT,
                box_office_us TEXT,
                box_office_world TEXT,
                cast_list TEXT,
                country TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_stats (
                user_id INTEGER PRIMARY KEY,
                movies_added INTEGER DEFAULT 0
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
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                reason TEXT,
                issued_by INTEGER,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_by INTEGER,
                resolved_at TIMESTAMP
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_reviews (
                id SERIAL PRIMARY KEY,
                movie_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                rating INTEGER,
                text TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_comments (
                id SERIAL PRIMARY KEY,
                movie_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
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
            CREATE TABLE IF NOT EXISTS profile_comments (
                id SERIAL PRIMARY KEY,
                target_user_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friends (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                friend_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, friend_id)
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
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
                author_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                reporter_id INTEGER NOT NULL,
                reported_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS google_users (
                email TEXT PRIMARY KEY,
                user_id INTEGER UNIQUE,
                name TEXT,
                picture TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Получаем текущий максимальный Movie ID
        max_id = await conn.fetchval("SELECT MAX(movie_id) FROM users")
        if max_id:
            movie_id_counter = max_id + 1
        else:
            movie_id_counter = 10000001

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

# ==================== ФУНКЦИИ БД ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ====================

async def get_user_by_movie_id(movie_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE movie_id = $1", movie_id)
        return dict(row) if row else None

async def get_user_by_email(email: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
        return dict(row) if row else None

async def get_user_by_username(username: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)
        return dict(row) if row else None

async def create_user(email: str, password_hash: str, username: str = None, display_name: str = None):
    global movie_id_counter
    async with pool.acquire() as conn:
        movie_id = movie_id_counter
        movie_id_counter += 1
        
        await conn.execute("""
            INSERT INTO users (movie_id, email, password_hash, username, display_name, is_verified)
            VALUES ($1, $2, $3, $4, $5, TRUE)
        """, movie_id, email, password_hash, username, display_name or username or email.split('@')[0])
        
        return movie_id

async def update_user_profile(movie_id: int, data: dict):
    async with pool.acquire() as conn:
        fields = []
        values = []
        for key, value in data.items():
            if value is not None:
                fields.append(f"{key} = ${len(values) + 1}")
                values.append(value)
        values.append(movie_id)
        if fields:
            await conn.execute(
                f"UPDATE users SET {', '.join(fields)}, updated_at = NOW() WHERE movie_id = ${len(values)}",
                *values
            )
        return True

async def is_admin_user(movie_id: int) -> bool:
    if movie_id in SUPER_ADMIN_IDS:
        return True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_admin FROM users WHERE movie_id = $1", movie_id)
        return row and row["is_admin"]

async def is_super_admin_user(movie_id: int) -> bool:
    if movie_id in SUPER_ADMIN_IDS:
        return True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_super_admin FROM users WHERE movie_id = $1", movie_id)
        return row and row["is_super_admin"]

async def is_banned_user(movie_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_banned, ban_expires_at FROM users WHERE movie_id = $1",
            movie_id
        )
        if row:
            if row["ban_expires_at"] and row["ban_expires_at"] < datetime.now():
                await conn.execute(
                    "UPDATE users SET is_banned = FALSE, ban_reason = NULL, ban_expires_at = NULL WHERE movie_id = $1",
                    movie_id
                )
                return False
            return row["is_banned"]
        return False

async def update_user_online_by_id(movie_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_seen = NOW(), is_online = TRUE WHERE movie_id = $1",
            movie_id
        )

async def is_user_online_by_id(movie_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_online, last_seen FROM users WHERE movie_id = $1",
            movie_id
        )
        if row:
            if row["is_online"] and row["last_seen"]:
                delta = datetime.now() - row["last_seen"]
                if delta.total_seconds() > 300:
                    await conn.execute("UPDATE users SET is_online = FALSE WHERE movie_id = $1", movie_id)
                    return False
                return True
        return False

async def get_user_profile_data(movie_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE movie_id = $1", movie_id)
        if row:
            return dict(row)
        return None

async def add_punishment_by_id(user_id: int, ptype: str, reason: str, issued_by: int, duration_hours: int = 0):
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
            await conn.execute(
                "UPDATE users SET is_banned = TRUE, ban_reason = $1, ban_expires_at = $2 WHERE movie_id = $3",
                reason, datetime.now() + timedelta(hours=duration_hours) if duration_hours > 0 else None, user_id
            )

# ==================== ФУНКЦИИ БД ДЛЯ ФИЛЬМОВ ====================

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

async def increment_requests():
    async with pool.acquire() as conn:
        await conn.execute("UPDATE stats SET value = value + 1 WHERE key = 'total_requests'")

async def get_total_requests():
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0

# ==================== ФУНКЦИИ БД ДЛЯ ОТЗЫВОВ ====================

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

async def get_reviews_count(movie_code: str):
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM movie_reviews WHERE movie_code = $1", movie_code) or 0

async def get_user_review(movie_code: str, user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM movie_reviews WHERE movie_code = $1 AND user_id = $2", movie_code, user_id)

# ==================== ФУНКЦИИ БД ДЛЯ ПРОФИЛЯ ====================

async def add_profile_comment(target_user_id: int, author_id: int, text: str):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO profile_comments (target_user_id, author_id, text) VALUES ($1, $2, $3)", target_user_id, author_id, text)

async def get_profile_comments(target_user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM profile_comments WHERE target_user_id = $1 ORDER BY created_at DESC", target_user_id)
        return [dict(r) for r in rows]

async def delete_profile_comment(comment_id: int, user_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM profile_comments WHERE id = $1 AND (author_id = $2 OR $2 IN (SELECT movie_id FROM users WHERE is_super_admin = TRUE))",
            comment_id, user_id
        )
        return result != "DELETE 0"

# ==================== ФУНКЦИИ БД ДЛЯ ДРУЗЕЙ ====================

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
            JOIN users u ON f.friend_id = u.movie_id
            WHERE f.user_id = $1 AND f.status = 'accepted'
            UNION
            SELECT f.user_id as friend_id, f.status, u.username, u.photo_url, u.is_online
            FROM friends f
            JOIN users u ON f.user_id = u.movie_id
            WHERE f.friend_id = $1 AND f.status = 'accepted'
        """, user_id)
        return [dict(r) for r in rows]

async def get_friend_requests(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.user_id, f.status, u.username, u.photo_url
            FROM friends f
            JOIN users u ON f.user_id = u.movie_id
            WHERE f.friend_id = $1 AND f.status = 'pending'
        """, user_id)
        return [dict(r) for r in rows]

async def get_sent_friend_requests(user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.friend_id, f.status, u.username, u.photo_url
            FROM friends f
            JOIN users u ON f.friend_id = u.movie_id
            WHERE f.user_id = $1 AND f.status = 'pending'
        """, user_id)
        return [dict(r) for r in rows]

async def search_users(query: str, current_user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT u.movie_id, u.username, u.display_name, u.photo_url, u.is_online
            FROM users u
            WHERE (u.username ILIKE $1 OR u.display_name ILIKE $1)
            AND u.movie_id != $2
            LIMIT 20
        """, f"%{query}%", current_user_id)
        result = []
        for row in rows:
            is_friend = await conn.fetchval("""
                SELECT id FROM friends 
                WHERE (user_id = $1 AND friend_id = $2 OR user_id = $2 AND friend_id = $1) 
                AND status = 'accepted'
            """, current_user_id, row["movie_id"])
            result.append({
                "user_id": row["movie_id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "photo_url": row["photo_url"],
                "is_online": row["is_online"] or False,
                "is_friend": bool(is_friend)
            })
        return result

# ==================== ФУНКЦИИ БД ДЛЯ НОВОСТЕЙ ====================

async def create_news(title: str, content: str, author_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO news (title, content, author_id) VALUES ($1, $2, $3)",
            title, content, author_id
        )
        users = await conn.fetch("SELECT movie_id FROM users")
        for user in users:
            await conn.execute(
                "INSERT INTO notifications (user_id, type, content, link) VALUES ($1, $2, $3, $4)",
                user["movie_id"], 'news', f'📰 {title}', '/news'
            )
        return True

async def get_news(limit: int = 20):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT n.*, u.username, u.photo_url
            FROM news n
            JOIN users u ON n.author_id = u.movie_id
            ORDER BY n.created_at DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]

async def delete_news(news_id: int, user_id: int):
    async with pool.acquire() as conn:
        is_author = await conn.fetchval("SELECT 1 FROM news WHERE id = $1 AND author_id = $2", news_id, user_id)
        is_super = await is_super_admin_user(user_id)
        if is_author or is_super:
            await conn.execute("DELETE FROM news WHERE id = $1", news_id)
            return True
        return False

# ==================== ФУНКЦИИ БД ДЛЯ СООБЩЕНИЙ ====================

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
            user_data = await get_user_profile_data(row["other_user_id"])
            if user_data:
                result.append({
                    "user_id": row["other_user_id"],
                    "display_name": user_data.get("display_name") or f"User {row['other_user_id']}",
                    "username": user_data.get("username"),
                    "photo_url": user_data.get("photo_url"),
                    "is_online": user_data.get("is_online", False),
                    "last_message": await conn.fetchval("""
                        SELECT text FROM messages 
                        WHERE (sender_id = $1 AND receiver_id = $2) OR (sender_id = $2 AND receiver_id = $1)
                        ORDER BY created_at DESC LIMIT 1
                    """, user_id, row["other_user_id"]),
                    "last_time": row["last_time"]
                })
        return result

# ==================== ФУНКЦИИ БД ДЛЯ УВЕДОМЛЕНИЙ ====================

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

# ==================== ФУНКЦИИ БД ДЛЯ ЖАЛОБ ====================

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
    if is_super_admin_user(user_id):
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

# ---------- ХЭНДЛЕРЫ БОТА ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    # Проверяем, есть ли токен в команде
    args = message.text.split()
    if len(args) > 1:
        token = args[1]
        
        # Проверяем, существует ли сессия
        if token in auth_sessions:
            # Обновляем статус сессии
            auth_sessions[token]["status"] = "authenticated"
            auth_sessions[token]["user_id"] = message.from_user.id
            
            await message.answer(
                "✅ <b>Авторизация успешна!</b>\n\n"
                "Вы можете закрыть это окно и вернуться на сайт.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 Перейти на сайт", url=f"{SITE_URL}/dashboard")]
                ])
            )
            return
    
    # Обычный /start без токена
    text = (
        "🎬 <b>Movie Admin Bot</b>\n\n"
        "Этот бот используется для поиска фильмов.\n"
        "Введи код фильма, чтобы получить информацию.\n\n"
        "🔐 Для входа в админ-панель используйте веб-сайт."
    )
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    await callback.answer("Подписка проверена!", show_alert=True)

@dp.message(F.text == "🔧 Админ-панель")
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    # Для упрощения, используем ID из Telegram
    user_id = message.from_user.id
    # Проверяем, есть ли пользователь в БД
    user = await get_user_by_movie_id(user_id)
    if not user or not user.get("is_admin"):
        return await message.answer("⛔ Нет доступа")
    await message.answer("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_main_kb(user_id))

@dp.callback_query(F.data == "admin_close")
async def cb_close(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data == "admin_back")
async def cb_back(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_main_kb(user_id))

@dp.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user_by_movie_id(user_id)
    if not user or not user.get("is_admin"):
        return
    movies_count = await get_movies_count()
    requests_count = await get_total_requests()
    text = f"📊 <b>Статистика бота</b>\n\n🎬 Фильмов в базе: <b>{movies_count}</b>\n🔍 Всего запросов: <b>{requests_count}</b>"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))

@dp.callback_query(F.data == "admin_list")
async def cb_list(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user_by_movie_id(user_id)
    if not user or not user.get("is_admin"):
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
        user_data = await get_user_profile_data(review["user_id"])
        display_name = user_data.get("display_name") if user_data else f"User {review['user_id']}"
        is_super = user_data.get("is_super_admin", False) if user_data else False
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
    user_id = callback.from_user.id
    user = await get_user_by_movie_id(user_id)
    if not user or not user.get("is_admin"):
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
    user_id = callback.from_user.id
    user = await get_user_by_movie_id(user_id)
    if not user or not user.get("is_admin"):
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
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    code = callback.data.split(":", 1)[1]
    await delete_movie(code)
    await callback.answer("Фильм удалён ✅")
    await cb_list(callback)

@dp.callback_query(F.data == "admin_admins")
async def cb_admins(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    # Получаем всех админов
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_admin = TRUE OR is_super_admin = TRUE")
        text = "👥 <b>Администраторы:</b>\n\n"
        if not rows:
            text += "Пока нет."
        else:
            for row in rows:
                display_name = row["display_name"] or f"User {row['movie_id']}"
                role = "⭐ Создатель" if row["is_super_admin"] else "👑 Админ"
                text += f"👤 {display_name} (<code>{row['movie_id']}</code>) — {role}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Назначить админа", callback_data="add_admin")],
        [InlineKeyboardButton(text="➖ Снять админа", callback_data="remove_admin")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_admin")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return
    await state.clear()
    await callback.message.edit_text("Введи Movie ID нового админа:")
    await state.set_state(AddAdmin.waiting_id)
    await state.update_data(action="add")

@dp.callback_query(F.data == "remove_admin")
async def cb_remove_admin(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return
    await state.clear()
    await callback.message.edit_text("Введи Movie ID админа для снятия:")
    await state.set_state(AddAdmin.waiting_id)
    await state.update_data(action="remove")

@dp.message(AddAdmin.waiting_id)
async def process_admin(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("ID должен быть числом. Попробуй ещё раз:")
    uid = int(message.text.strip())
    data = await state.get_data()
    await state.clear()
    
    async with pool.acquire() as conn:
        if data.get("action") == "add":
            await conn.execute("UPDATE users SET is_admin = TRUE WHERE movie_id = $1", uid)
            await message.answer(f"✅ <code>{uid}</code> теперь админ", parse_mode="HTML")
        else:
            await conn.execute("UPDATE users SET is_admin = FALSE, is_super_admin = FALSE WHERE movie_id = $1", uid)
            await message.answer(f"✅ <code>{uid}</code> снят с админки", parse_mode="HTML")

@dp.callback_query(F.data == "admin_bans")
async def cb_bans(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
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
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_banned = TRUE")
        if not rows:
            text = "Список банов пуст."
        else:
            text = "🚫 <b>Забаненные:</b>\n\n"
            for row in rows:
                display_name = row["display_name"] or f"User {row['movie_id']}"
                text += f"👤 {display_name} (<code>{row['movie_id']}</code>)"
                if row["ban_reason"]:
                    text += f" — {row['ban_reason']}"
                if row["ban_expires_at"]:
                    text += f" ⏳ до {row['ban_expires_at']}"
                text += "\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_bans")]
    ]))

@dp.callback_query(F.data == "ban_user")
async def cb_ban(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return
    await state.clear()
    await callback.message.edit_text("Введи Movie ID для бана:")
    await state.set_state(BanUser.waiting_id)
    await state.update_data(action="ban")

@dp.callback_query(F.data == "unban_user")
async def cb_unban(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await is_super_admin_user(user_id):
        return
    await state.clear()
    await callback.message.edit_text("Введи Movie ID для разбана:")
    await state.set_state(BanUser.waiting_id)
    await state.update_data(action="unban")

@dp.message(BanUser.waiting_id)
async def process_ban(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("ID должен быть числом:")
    uid = int(message.text.strip())
    data = await state.get_data()
    await state.clear()
    
    async with pool.acquire() as conn:
        if data.get("action") == "ban":
            await conn.execute("UPDATE users SET is_banned = TRUE, ban_reason = 'Забанен администратором' WHERE movie_id = $1", uid)
            await message.answer(f"🚫 <code>{uid}</code> забанен", parse_mode="HTML")
        else:
            await conn.execute("UPDATE users SET is_banned = FALSE, ban_reason = NULL, ban_expires_at = NULL WHERE movie_id = $1", uid)
            await message.answer(f"✅ <code>{uid}</code> разбанен", parse_mode="HTML")

@dp.message(StateFilter(None), F.text)
async def handle_code(message: Message):
    # Проверяем, есть ли пользователь в БД
    user = await get_user_by_movie_id(message.from_user.id)
    if not user:
        await message.answer(
            "🔐 Для использования бота необходимо зарегистрироваться на сайте.\n"
            f"Перейдите по ссылке: {SITE_URL}/register",
            parse_mode="HTML"
        )
        return
    
    if user.get("is_banned"):
        return await message.answer("🚫 Вы заблокированы в боте.")
    
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
def get_movie_id_from_cookie(request: Request) -> Optional[int]:
    movie_id_str = request.cookies.get("movie_id")
    if movie_id_str and movie_id_str.isdigit():
        return int(movie_id_str)
    return None

async def check_auth(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    user = await get_user_profile_data(movie_id)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    if user.get("is_banned"):
        raise HTTPException(status_code=403, detail="Пользователь заблокирован")
    await update_user_online_by_id(movie_id)
    return movie_id

async def check_admin(request: Request):
    movie_id = await check_auth(request)
    if not await is_admin_user(movie_id):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return movie_id

async def check_super_admin(request: Request):
    movie_id = await check_auth(request)
    if not await is_super_admin_user(movie_id):
        raise HTTPException(status_code=403, detail="Только суперадмин")
    return movie_id

# ==================== АВТОРИЗАЦИЯ ====================

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID
    })

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request
    })

@app.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request):
    return templates.TemplateResponse("verify_email.html", {
        "request": request
    })

@app.get("/setup-profile", response_class=HTMLResponse)
async def setup_profile_page(request: Request):
    return templates.TemplateResponse("setup_profile.html", {
        "request": request
    })

# ---------- API регистрации ----------
@app.post("/api/auth/send-code")
async def send_verification_code(request: Request, data: dict):
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")
    
    # Проверяем, не занят ли email
    existing = await get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=400, detail="Этот email уже зарегистрирован")
    
    # Генерируем код
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=10)
    
    # Сохраняем код
    email_verification_codes[email] = {
        "code": code,
        "expires_at": expires_at
    }
    
    # Логируем код (всегда виден в логах Railway)
    logger.info(f"📧 Код для {email}: {code}")
    
    # Пытаемся отправить email, но не ждем результата
    try:
        import threading
        thread = threading.Thread(target=send_verification_email, args=(email, code))
        thread.daemon = True
        thread.start()
    except Exception as e:
        logger.warning(f"Ошибка запуска потока отправки: {e}")
    
    # Всегда возвращаем успех и показываем код в ответе (для отладки)
    return {
        "success": True,
        "message": "Код отправлен на email (проверьте логи Railway)",
        "debug_code": code  # Показываем код на странице для разработки
    }
        
@app.post("/api/auth/verify-code")
async def verify_email_code(request: Request, data: dict):
    email = data.get("email")
    code = data.get("code")
    password = data.get("password")
    
    if not email or not code or not password:
        raise HTTPException(status_code=400, detail="Все поля обязательны")
    
    # Проверяем код
    stored = email_verification_codes.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="Код не запрошен или истек")
    
    if datetime.now() > stored["expires_at"]:
        del email_verification_codes[email]
        raise HTTPException(status_code=400, detail="Код истек. Запросите новый")
    
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Неверный код")
    
    # Удаляем использованный код
    del email_verification_codes[email]
    
    # Хешируем пароль
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    # Создаем пользователя (временно без имени)
    movie_id = await create_user(
        email=email,
        password_hash=password_hash,
        username=None,
        display_name=None
    )
    
    # Сохраняем movie_id в сессии для завершения регистрации
    auth_sessions[f"setup_{email}"] = {
        "movie_id": movie_id,
        "email": email,
        "created_at": datetime.now()
    }
    
    return {"success": True, "movie_id": movie_id}

@app.post("/api/auth/setup-profile")
async def setup_profile(request: Request, data: dict):
    movie_id = data.get("movie_id")
    username = data.get("username")
    display_name = data.get("display_name")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    photo_url = data.get("photo_url")
    banner_url = data.get("banner_url")
    
    if not movie_id:
        raise HTTPException(status_code=400, detail="Movie ID обязателен")
    
    # Проверяем, существует ли пользователь
    user = await get_user_profile_data(movie_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    # Проверяем уникальность username
    if username:
        existing = await get_user_by_username(username)
        if existing and existing["movie_id"] != movie_id:
            raise HTTPException(status_code=400, detail="Этот юзернейм уже занят")
    
    # Обновляем профиль
    update_data = {}
    if username:
        update_data["username"] = username
    if display_name:
        update_data["display_name"] = display_name
    if first_name:
        update_data["first_name"] = first_name
    if last_name:
        update_data["last_name"] = last_name
    if photo_url:
        update_data["photo_url"] = photo_url
    if banner_url:
        update_data["banner_url"] = banner_url
    
    if update_data:
        await update_user_profile(movie_id, update_data)
    
    # Устанавливаем cookie
    response = JSONResponse({"success": True, "movie_id": movie_id})
    response.set_cookie(
        key="movie_id",
        value=str(movie_id),
        httponly=True,
        max_age=60*60*24*7,
        secure=False,
        samesite="lax"
    )
    return response

# ---------- API входа ----------
@app.post("/api/auth/login")
async def login(request: Request, data: dict):
    email_or_username = data.get("email_or_username")
    password = data.get("password")
    
    if not email_or_username or not password:
        raise HTTPException(status_code=400, detail="Все поля обязательны")
    
    # Ищем пользователя по email или username
    user = await get_user_by_email(email_or_username)
    if not user:
        user = await get_user_by_username(email_or_username)
    
    if not user:
        raise HTTPException(status_code=400, detail="Пользователь не найден")
    
    # Проверяем пароль
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    if user["password_hash"] != password_hash:
        raise HTTPException(status_code=400, detail="Неверный пароль")
    
    # Проверяем бан
    if user.get("is_banned"):
        ban_reason = user.get("ban_reason", "Без причины")
        ban_expires = user.get("ban_expires_at")
        if ban_expires and ban_expires > datetime.now():
            raise HTTPException(status_code=403, detail=f"Пользователь заблокирован до {ban_expires.strftime('%d.%m.%Y %H:%M')}. Причина: {ban_reason}")
        elif ban_expires and ban_expires <= datetime.now():
            async with pool.acquire() as conn:
                await conn.execute("UPDATE users SET is_banned = FALSE, ban_reason = NULL, ban_expires_at = NULL WHERE movie_id = $1", user["movie_id"])
        else:
            raise HTTPException(status_code=403, detail=f"Пользователь заблокирован. Причина: {ban_reason}")
    
    # Обновляем онлайн
    await update_user_online_by_id(user["movie_id"])
    
    # Устанавливаем cookie
    response = JSONResponse({"success": True, "movie_id": user["movie_id"]})
    response.set_cookie(
        key="movie_id",
        value=str(user["movie_id"]),
        httponly=True,
        max_age=60*60*24*7,
        secure=False,
        samesite="lax"
    )
    return response

@app.post("/api/auth/check-username")
async def check_username(request: Request, data: dict):
    username = data.get("username")
    if not username:
        return {"available": False, "message": "Username обязателен"}
    
    existing = await get_user_by_username(username)
    return {"available": not bool(existing)}

@app.get("/api/auth/me")
async def get_current_user(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        return {"authenticated": False}
    
    user = await get_user_profile_data(movie_id)
    if not user:
        return {"authenticated": False}
    
    if user.get("is_banned"):
        return {"authenticated": False, "banned": True}
    
    await update_user_online_by_id(movie_id)
    
    return {
        "authenticated": True,
        "movie_id": user["movie_id"],
        "email": user["email"],
        "username": user["username"],
        "display_name": user["display_name"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "photo_url": user["photo_url"],
        "banner_url": user["banner_url"],
        "is_admin": user.get("is_admin", False),
        "is_super_admin": user.get("is_super_admin", False)
    }

@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("movie_id")
    return response

# ---------- API Google Auth ----------
@app.post("/auth/google")
async def google_auth(request: Request, data: dict):
    try:
        email = data.get("email")
        name = data.get("name")
        picture = data.get("picture")
        
        if not email:
            raise HTTPException(status_code=400, detail="Email не указан")
        
        # Ищем пользователя
        user = await get_user_by_email(email)
        
        if not user:
            # Создаем нового пользователя
            random_password = secrets.token_urlsafe(16)
            password_hash = hashlib.sha256(random_password.encode()).hexdigest()
            
            username = email.split('@')[0]
            # Проверяем уникальность username
            existing = await get_user_by_username(username)
            if existing:
                username = f"{username}_{random.randint(100, 999)}"
            
            movie_id = await create_user(
                email=email,
                password_hash=password_hash,
                username=username,
                display_name=name or username
            )
            
            user = await get_user_profile_data(movie_id)
        
        if user.get("is_banned"):
            raise HTTPException(status_code=403, detail="Пользователь заблокирован")
        
        await update_user_online_by_id(user["movie_id"])
        
        response = JSONResponse({"success": True, "movie_id": user["movie_id"]})
        response.set_cookie(
            key="movie_id",
            value=str(user["movie_id"]),
            httponly=True,
            max_age=60*60*24*7,
            secure=False,
            samesite="lax"
        )
        return response
    except Exception as e:
        logger.error(f"Google auth error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== API ДАШБОРДА ====================

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        return RedirectResponse(url="/", status_code=302)
    
    user = await get_user_profile_data(movie_id)
    if not user:
        return RedirectResponse(url="/", status_code=302)
    
    if user.get("is_banned"):
        return RedirectResponse(url="/", status_code=302)
    
    await update_user_online_by_id(movie_id)
    
    is_admin_user = await is_admin_user(movie_id)
    is_super_user = await is_super_admin_user(movie_id)
    unread_count = await get_unread_count(movie_id)
    
    async with pool.acquire() as conn:
        movies_count = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests_count = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin = TRUE OR is_super_admin = TRUE") or 0
        bans_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE") or 0
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user_id": movie_id,
        "display_name": user["display_name"] or f"User {movie_id}",
        "username": user["username"],
        "photo_url": user["photo_url"],
        "banner_url": user["banner_url"],
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

# ==================== API ФИЛЬМОВ ====================

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

@app.get("/api/movies")
async def api_movies(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(movie_id)
    
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
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(movie_id)
    
    row = await get_movie(code)
    if row:
        tpz, votes = await get_movie_tpz(code)
        result = row
        result["tpz"] = tpz
        result["votes"] = votes
        return result
    raise HTTPException(status_code=404, detail="Фильм не найден")

@app.post("/api/movies")
async def api_add_movie(request: Request, data: MovieCreate):
    movie_id = await check_admin(request)
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO movies (code, title, year, poster, description, rating, banner, added_by, 
                   director, writers, genres, budget, box_office_us, box_office_world, cast_list, country) 
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)""",
                data.code, data.title, data.year, data.poster, data.description, data.rating, data.banner, movie_id,
                data.director, data.writers, data.genres, data.budget, data.box_office_us, data.box_office_world, data.cast_list, data.country
            )
            await update_user_online_by_id(movie_id)
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

# ==================== API ОТЗЫВОВ ====================

class ReviewData(BaseModel):
    rating: int
    text: str

@app.post("/api/movies/{code}/reviews")
async def api_add_review(request: Request, code: str, data: ReviewData):
    movie_id = await check_auth(request)
    existing = await get_user_review(code, movie_id)
    if existing:
        raise HTTPException(status_code=400, detail="Вы уже оставляли отзыв на этот фильм")
    await add_review(code, movie_id, data.rating, data.text)
    return {"success": True}

@app.get("/api/movies/{code}/reviews")
async def api_get_reviews(request: Request, code: str):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(movie_id)
    return await get_reviews(code)

@app.delete("/api/reviews/{review_id}")
async def api_delete_review(request: Request, review_id: int):
    movie_id = await check_auth(request)
    is_super = await is_super_admin_user(movie_id)
    if not is_super:
        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT user_id FROM movie_reviews WHERE id = $1", review_id)
            if owner != movie_id:
                raise HTTPException(status_code=403, detail="Нельзя удалить чужой отзыв")
    success = await delete_review(review_id, movie_id)
    if not success:
        raise HTTPException(status_code=404, detail="Отзыв не найден")
    return {"success": True}

# ==================== API ПРОФИЛЯ ====================

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    banner_url: Optional[str] = None

@app.get("/api/profile/{user_id}")
async def api_profile(request: Request, user_id: int):
    current_user = get_movie_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(current_user)
    
    user = await get_user_profile_data(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    return {
        "movie_id": user["movie_id"],
        "display_name": user["display_name"] or f"User {user['movie_id']}",
        "username": user["username"],
        "photo_url": user["photo_url"],
        "banner_url": user["banner_url"],
        "is_admin": user.get("is_admin", False),
        "is_super_admin": user.get("is_super_admin", False),
        "is_banned": user.get("is_banned", False),
        "is_online": user.get("is_online", False),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "created_at": user["created_at"]
    }

@app.put("/api/profile/{user_id}")
async def api_update_profile(request: Request, user_id: int, data: ProfileUpdate):
    current_user = await check_auth(request)
    if current_user != user_id:
        raise HTTPException(status_code=403, detail="Нельзя редактировать чужой профиль")
    
    update_data = {}
    if data.display_name is not None:
        update_data["display_name"] = data.display_name
    if data.username is not None:
        existing = await get_user_by_username(data.username)
        if existing and existing["movie_id"] != user_id:
            raise HTTPException(status_code=400, detail="Этот юзернейм уже занят")
        update_data["username"] = data.username
    if data.photo_url is not None:
        update_data["photo_url"] = data.photo_url
    if data.banner_url is not None:
        update_data["banner_url"] = data.banner_url
    
    if update_data:
        await update_user_profile(user_id, update_data)
    
    await update_user_online_by_id(user_id)
    return {"success": True}

@app.get("/api/user/{user_id}/name")
async def api_user_name(request: Request, user_id: int):
    current_user = get_movie_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(current_user)
    
    user = await get_user_profile_data(user_id)
    if not user:
        return {
            "display_name": f"User {user_id}",
            "username": str(user_id),
            "photo_url": None,
            "banner_url": None,
            "is_super": False,
            "is_admin": False,
            "is_online": False
        }
    
    return {
        "display_name": user["display_name"] or f"User {user_id}",
        "username": user["username"] or str(user_id),
        "photo_url": user["photo_url"],
        "banner_url": user["banner_url"],
        "is_super": user.get("is_super_admin", False),
        "is_admin": user.get("is_admin", False),
        "is_online": user.get("is_online", False)
    }

# ==================== API СТАТИСТИКИ ====================

@app.get("/api/stats")
async def api_stats(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(movie_id)
    
    async with pool.acquire() as conn:
        movies = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin = TRUE OR is_super_admin = TRUE") or 0
        bans = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE") or 0
        users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
    return {"movies": movies, "requests": requests, "admins": admins, "bans": bans, "users": users}

# ==================== API КОММЕНТАРИЕВ ====================

class CommentData(BaseModel):
    text: str

@app.get("/api/profile/{user_id}/comments")
async def api_get_profile_comments(request: Request, user_id: int):
    current_user = get_movie_id_from_cookie(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(current_user)
    comments = await get_profile_comments(user_id)
    result = []
    for c in comments:
        user_data = await get_user_profile_data(c["author_id"])
        c["author_display_name"] = user_data["display_name"] if user_data else f"User {c['author_id']}"
        c["author_photo_url"] = user_data["photo_url"] if user_data else None
        c["author_is_super"] = user_data.get("is_super_admin", False) if user_data else False
        c["author_is_admin"] = user_data.get("is_admin", False) if user_data else False
        result.append(c)
    return result

@app.post("/api/profile/{user_id}/comments")
async def api_add_profile_comment(request: Request, user_id: int, data: CommentData):
    author_id = await check_auth(request)
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Текст комментария обязателен")
    await add_profile_comment(user_id, author_id, data.text.strip())
    return {"success": True}

@app.delete("/api/profile/comments/{comment_id}")
async def api_delete_profile_comment(request: Request, comment_id: int):
    movie_id = await check_auth(request)
    success = await delete_profile_comment(comment_id, movie_id)
    if not success:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    return {"success": True}

# ==================== API АДМИНОВ ====================

@app.get("/api/admins")
async def api_admins(request: Request):
    await check_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_admin = TRUE OR is_super_admin = TRUE")
        result = []
        for row in rows:
            is_online = await is_user_online_by_id(row["movie_id"])
            result.append({
                "user_id": row["movie_id"],
                "display_name": row["display_name"] or f"User {row['movie_id']}",
                "username": row["username"],
                "photo_url": row["photo_url"],
                "is_online": is_online,
                "is_super": row.get("is_super_admin", False),
                "movies_count": await conn.fetchval("SELECT movies_added FROM admin_stats WHERE user_id = $1", row["movie_id"]) or 0,
                "warns": 0
            })
        return result

@app.post("/api/admins")
async def api_add_admin(request: Request, data: dict):
    await check_super_admin(request)
    user_id = data.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID required")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin = TRUE WHERE movie_id = $1", user_id)
    return {"success": True}

@app.delete("/api/admins/{user_id}")
async def api_remove_admin(request: Request, user_id: int):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin = FALSE, is_super_admin = FALSE WHERE movie_id = $1", user_id)
    return {"success": True}

# ==================== API БАНОВ ====================

@app.get("/api/bans")
async def api_bans(request: Request):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_banned = TRUE")
        result = []
        for row in rows:
            is_online = await is_user_online_by_id(row["movie_id"])
            result.append({
                "user_id": row["movie_id"],
                "display_name": row["display_name"] or f"User {row['movie_id']}",
                "photo_url": row["photo_url"],
                "is_online": is_online,
                "reason": row["ban_reason"] or "",
                "expires_at": row["ban_expires_at"]
            })
        return result

@app.post("/api/bans")
async def api_add_ban(request: Request, data: dict):
    await check_super_admin(request)
    user_id = data.get("user_id")
    reason = data.get("reason", "Забанен администратором")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID required")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = TRUE, ban_reason = $1 WHERE movie_id = $2", reason, user_id)
    return {"success": True}

@app.delete("/api/bans/{user_id}")
async def api_remove_ban(request: Request, user_id: int):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = FALSE, ban_reason = NULL, ban_expires_at = NULL WHERE movie_id = $1", user_id)
    return {"success": True}

# ==================== API ДРУЗЕЙ ====================

class FriendRequestData(BaseModel):
    user_id: int

@app.post("/api/friends/request")
async def api_send_friend_request(request: Request, data: FriendRequestData):
    movie_id = await check_auth(request)
    if movie_id == data.user_id:
        raise HTTPException(status_code=400, detail="Нельзя добавить себя в друзья")
    success, message = await send_friend_request(movie_id, data.user_id)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message}

@app.post("/api/friends/accept/{friend_id}")
async def api_accept_friend_request(request: Request, friend_id: int):
    movie_id = await check_auth(request)
    success = await accept_friend_request(movie_id, friend_id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось принять запрос")
    return {"success": True}

@app.post("/api/friends/decline/{friend_id}")
async def api_decline_friend_request(request: Request, friend_id: int):
    movie_id = await check_auth(request)
    success = await decline_friend_request(movie_id, friend_id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось отклонить запрос")
    return {"success": True}

@app.get("/api/friends")
async def api_get_friends(request: Request):
    movie_id = await check_auth(request)
    return await get_friends(movie_id)

@app.get("/api/friends/requests")
async def api_get_friend_requests(request: Request):
    movie_id = await check_auth(request)
    return await get_friend_requests(movie_id)

@app.get("/api/friends/sent")
async def api_get_sent_friend_requests(request: Request):
    movie_id = await check_auth(request)
    return await get_sent_friend_requests(movie_id)

@app.get("/api/search")
async def api_search_users(request: Request, q: str):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if not q or len(q) < 2:
        return []
    return await search_users(q, movie_id)

# ==================== API УВЕДОМЛЕНИЙ ====================

@app.get("/api/notifications")
async def api_get_notifications(request: Request, unread_only: bool = False):
    movie_id = await check_auth(request)
    return await get_notifications(movie_id, unread_only)

@app.post("/api/notifications/read/{notification_id}")
async def api_mark_notification_read(request: Request, notification_id: int):
    movie_id = await check_auth(request)
    await mark_notification_read(notification_id)
    return {"success": True}

@app.post("/api/notifications/read-all")
async def api_mark_all_notifications_read(request: Request):
    movie_id = await check_auth(request)
    await mark_all_notifications_read(movie_id)
    return {"success": True}

@app.get("/api/notifications/unread")
async def api_get_unread_count(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        return {"count": 0}
    return {"count": await get_unread_count(movie_id)}

# ==================== API НОВОСТЕЙ ====================

class NewsData(BaseModel):
    title: str
    content: str

@app.get("/api/news")
async def api_get_news(request: Request):
    movie_id = get_movie_id_from_cookie(request)
    if not movie_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    await update_user_online_by_id(movie_id)
    return await get_news()

@app.post("/api/news")
async def api_create_news(request: Request, data: NewsData):
    movie_id = await check_super_admin(request)
    await create_news(data.title, data.content, movie_id)
    return {"success": True}

@app.delete("/api/news/{news_id}")
async def api_delete_news(request: Request, news_id: int):
    movie_id = await check_super_admin(request)
    success = await delete_news(news_id, movie_id)
    if not success:
        raise HTTPException(status_code=404, detail="Новость не найдена или нет прав")
    return {"success": True}

# ==================== API СООБЩЕНИЙ ====================

class MessageData(BaseModel):
    text: str

@app.post("/api/messages/{receiver_id}")
async def api_send_message(request: Request, receiver_id: int, data: MessageData):
    movie_id = await check_auth(request)
    if movie_id == receiver_id:
        raise HTTPException(status_code=400, detail="Нельзя отправить сообщение себе")
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")
    await send_message(movie_id, receiver_id, data.text.strip())
    return {"success": True}

@app.get("/api/messages/{other_user_id}")
async def api_get_messages(request: Request, other_user_id: int):
    movie_id = await check_auth(request)
    return await get_messages(movie_id, other_user_id)

@app.get("/api/chats")
async def api_get_chats(request: Request):
    movie_id = await check_auth(request)
    return await get_chat_users(movie_id)

# ==================== API ЖАЛОБ ====================

class ReportData(BaseModel):
    reason: str

@app.post("/api/report/{user_id}")
async def api_report_user(request: Request, user_id: int, data: ReportData):
    reporter_id = await check_auth(request)
    if reporter_id == user_id:
        raise HTTPException(status_code=400, detail="Нельзя пожаловаться на себя")
    if not data.reason or not data.reason.strip():
        raise HTTPException(status_code=400, detail="Укажите причину жалобы")
    await create_report(reporter_id, user_id, data.reason.strip())
    return {"success": True}

# ==================== API ПОДДЕРЖКИ ====================

class SupportMessage(BaseModel):
    subject: str
    message: str

@app.post("/api/support")
async def api_support(request: Request, data: SupportMessage):
    movie_id = await check_auth(request)
    user = await get_user_profile_data(movie_id)
    display_name = user["display_name"] if user else f"User {movie_id}"
    for admin_id in SUPER_ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆘 <b>Новое сообщение в поддержку!</b>\n\n"
                f"👤 <b>От:</b> {display_name} (<code>{movie_id}</code>)\n"
                f"📋 <b>Тема:</b> {data.subject}\n"
                f"💬 <b>Сообщение:</b>\n{data.message}",
                parse_mode="HTML"
            )
        except:
            pass
    return {"success": True}

# ==================== API ПОЛЬЗОВАТЕЛЕЙ ====================

@app.get("/api/users")
async def api_users(request: Request):
    await check_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users ORDER BY created_at DESC")
        result = []
        for row in rows:
            is_online = await is_user_online_by_id(row["movie_id"])
            result.append({
                "user_id": row["movie_id"],
                "display_name": row["display_name"] or f"User {row['movie_id']}",
                "username": row["username"],
                "photo_url": row["photo_url"],
                "is_admin": row.get("is_admin", False),
                "is_super": row.get("is_super_admin", False),
                "is_online": is_online,
                "created_at": row["created_at"]
            })
        return result

# ==================== API РОЛЕЙ ====================

class RoleUpdate(BaseModel):
    user_id: int
    role: str

@app.post("/api/roles")
async def api_update_role(request: Request, data: RoleUpdate):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        if data.role == 'super_admin':
            await conn.execute("UPDATE users SET is_admin = TRUE, is_super_admin = TRUE WHERE movie_id = $1", data.user_id)
        elif data.role == 'admin':
            await conn.execute("UPDATE users SET is_admin = TRUE, is_super_admin = FALSE WHERE movie_id = $1", data.user_id)
        else:
            await conn.execute("UPDATE users SET is_admin = FALSE, is_super_admin = FALSE WHERE movie_id = $1", data.user_id)
    return {"success": True}

@app.get("/api/roles")
async def api_get_roles(request: Request):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT movie_id, username, display_name, photo_url,
                   CASE WHEN is_super_admin THEN 'super_admin'
                        WHEN is_admin THEN 'admin'
                        ELSE 'user' END as role
            FROM users
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in rows]

# ==================== API НАКАЗАНИЙ ====================

class PunishData(BaseModel):
    user_id: int
    type: str
    reason: str = ""
    duration_hours: int = 0

@app.post("/api/punish")
async def api_punish(request: Request, data: PunishData):
    issued_by = await check_super_admin(request)
    if await is_super_admin_user(data.user_id):
        raise HTTPException(status_code=403, detail="Нельзя наказывать суперадмина")
    await add_punishment_by_id(data.user_id, data.type, data.reason, issued_by, data.duration_hours)
    return {"success": True}

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
