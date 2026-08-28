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
    text = f"<b>{movie['title']} ({movie['year']})</b>\nКод: <code>{movie['code']}</code>\nIMDb: {movie['rating']}\n\n{movie['description'][:180]}..."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=movie_actions_kb(code, callback.from_user.id))

@dp.callback_query(F.data.startswith("delete_movie:"))
async def cb_delete_movie(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    code = callback.data.split(":", 1)[1]
    await delete_movie(code)
    await callback.answer("Фильм удалён ✅")
    await cb_list(callback)

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
        await message.answer(text, parse_mode="HTML", reply_markup=movie_actions_kb(code, message.from_user.id))

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
        await add_movie_to_db(data["code"], data["title"], data["year"], data["poster"], data["description"], message.text.strip())
        await state.clear()
        count = await get_movies_count()
        await message.answer(f"✅ Фильм <b>{data['title']}</b> добавлен!\nВсего фильмов в базе: <b>{count}</b>", parse_mode="HTML")
    except asyncpg.UniqueViolationError:
        await message.answer("❌ Такой код уже существует. Попробуй другой.")

@dp.callback_query(F.data == "admin_admins")
async def cb_admins(callback: CallbackQuery):
    if not await is_super_admin(callback.from_user.id):
        return await callback.answer("Недостаточно прав", show_alert=True)
    admins = await get_admins()
    text = "👥 <b>Обычные админы:</b>\n\n"
    text += "Пока нет." if not admins else "\n".join(f"<code>{uid}</code>" for uid in admins)
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
        for uid, reason in banned:
            text += f"<code>{uid}</code>"
            if reason:
                text += f" — {reason}"
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
    if await is_banned(message.from_user.id):
        return await message.answer("🚫 Вы заблокированы в боте.")
    if not await check_sub(message.from_user.id):
        return await message.answer("Сначала подпишись на канал!", reply_markup=subscribe_kb())
    code = message.text.strip()
    movie = await get_movie(code)
    if not movie:
        return await message.answer("❌ Код не найден.")
    await increment_requests()
    caption = f"<b>{movie['title']} ({movie['year']})</b>\n\n⭐ <b>IMDb:</b> {movie['rating']}\n\n{movie['description']}"
    await message.answer_photo(photo=movie["poster"], caption=caption, parse_mode="HTML")

# ==================== FASTAPI АДМИН-ПАНЕЛЬ ====================
templates = Jinja2Templates(directory="templates")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(dp.start_polling(bot))
    logger.info("Бот и админ-панель запущены")
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# ---------- Вспомогательные функции для админки ----------
def verify_telegram_auth(data: dict) -> Optional[int]:
    check_data = data.copy()
    check_hash = check_data.pop("hash", None)
    if not check_hash:
        return None
    sorted_items = sorted(check_data.items())
    data_string = "\n".join([f"{k}={v}" for k, v in sorted_items])
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    hmac_hash = hmac.new(secret_key, data_string.encode(), hashlib.sha256).hexdigest()
    if hmac_hash == check_hash:
        return int(data.get("id", 0))
    return None

def get_user_id_from_cookie(request: Request) -> Optional[int]:
    user_id_str = request.cookies.get("user_id")
    if user_id_str and user_id_str.isdigit():
        return int(user_id_str)
    return None

# ---------- Роуты админки ----------
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/auth/telegram")
async def auth_telegram(request: Request):
    form = await request.form()
    data = dict(form)
    user_id = verify_telegram_auth(data)
    if not user_id:
        raise HTTPException(status_code=401, detail="Неверная подпись")
    if not await is_admin(user_id):
        return JSONResponse(status_code=403, content={"error": "У вас нет прав администратора"})
    response = JSONResponse({"success": True, "user_id": user_id})
    response.set_cookie(key="user_id", value=str(user_id), httponly=True, max_age=60*60*24*7)
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("user_id")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id or not await is_admin(user_id):
        return RedirectResponse(url="/", status_code=302)
    is_super = await is_super_admin(user_id)
    async with pool.acquire() as conn:
        movies_count = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests_count = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins_count = await conn.fetchval("SELECT COUNT(*) FROM admins") or 0
        bans_count = await conn.fetchval("SELECT COUNT(*) FROM bans") or 0
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user_id": user_id,
        "is_super": is_super,
        "stats": {"movies": movies_count, "requests": requests_count, "admins": admins_count, "bans": bans_count}
    })

# ---------- API ----------
class MovieCreate(BaseModel):
    code: str
    title: str
    year: int
    poster: str
    description: str
    rating: str

class MovieUpdate(BaseModel):
    title: Optional[str] = None
    year: Optional[int] = None
    poster: Optional[str] = None
    description: Optional[str] = None
    rating: Optional[str] = None

class AdminAdd(BaseModel):
    user_id: int

class BanAdd(BaseModel):
    user_id: int
    reason: str = ""

async def check_admin(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id or not await is_admin(user_id):
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user_id

async def check_super_admin(request: Request):
    user_id = get_user_id_from_cookie(request)
    if not user_id or not await is_super_admin(user_id):
        raise HTTPException(status_code=403, detail="Только суперадмин")
    return user_id

@app.get("/api/movies")
async def api_movies(request: Request):
    await check_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, title, year, poster, rating FROM movies ORDER BY code")
        return [dict(row) for row in rows]

@app.get("/api/movies/{code}")
async def api_movie(request: Request, code: str):
    await check_admin(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM movies WHERE code = $1", code)
        if row:
            return dict(row)
        raise HTTPException(status_code=404, detail="Фильм не найден")

@app.post("/api/movies")
async def api_add_movie(request: Request, data: MovieCreate):
    await check_admin(request)
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO movies (code, title, year, poster, description, rating) VALUES ($1, $2, $3, $4, $5, $6)",
                data.code, data.title, data.year, data.poster, data.description, data.rating
            )
            return {"success": True, "code": data.code}
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Фильм с таким кодом уже существует")

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
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM movies WHERE code = $1", code)
        return {"success": True}

@app.get("/api/admins")
async def api_admins(request: Request):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
        return [{"user_id": r["user_id"]} for r in rows]

@app.post("/api/admins")
async def api_add_admin(request: Request, data: AdminAdd):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", data.user_id)
        return {"success": True}

@app.delete("/api/admins/{user_id}")
async def api_remove_admin(request: Request, user_id: int):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)
        return {"success": True}

@app.get("/api/bans")
async def api_bans(request: Request):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, reason FROM bans")
        return [{"user_id": r["user_id"], "reason": r["reason"]} for r in rows]

@app.post("/api/bans")
async def api_add_ban(request: Request, data: BanAdd):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bans (user_id, reason) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET reason = $2",
            data.user_id, data.reason
        )
        return {"success": True}

@app.delete("/api/bans/{user_id}")
async def api_remove_ban(request: Request, user_id: int):
    await check_super_admin(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE user_id = $1", user_id)
        return {"success": True}

@app.get("/api/stats")
async def api_stats(request: Request):
    await check_admin(request)
    async with pool.acquire() as conn:
        movies = await conn.fetchval("SELECT COUNT(*) FROM movies") or 0
        requests = await conn.fetchval("SELECT value FROM stats WHERE key = 'total_requests'") or 0
        admins = await conn.fetchval("SELECT COUNT(*) FROM admins") or 0
        bans = await conn.fetchval("SELECT COUNT(*) FROM bans") or 0
    return {"movies": movies, "requests": requests, "admins": admins, "bans": bans}

@app.post("/auth/id")
async def auth_by_id(request: Request, user_id: int = Form(...)):
    if not await is_admin(user_id):
        return HTMLResponse("У вас нет прав администратора", status_code=403)
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(key="user_id", value=str(user_id), httponly=True, max_age=60*60*24*7)
    return response

@app.get("/api/check-auth")
async def check_auth(request: Request):
    user_id = get_user_id_from_cookie(request)
    if user_id and await is_admin(user_id):
        return {"authenticated": True, "user_id": user_id}
    return {"authenticated": False}

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
