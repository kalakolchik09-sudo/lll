import asyncio
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

logging.basicConfig(level=logging.INFO)
router = Router()
bot = Bot(BOT_TOKEN)

PENDING: dict[int, TelegramClient] = {}  # клиенты в процессе входа
TASKS: dict[int, asyncio.Task] = {}  # активные рассылки

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect("data.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    expires_at INTEGER DEFAULT 0,
    unlimited INTEGER DEFAULT 0,
    session TEXT,
    mode TEXT DEFAULT 'normal',
    interval INTEGER DEFAULT 600,
    normal_text TEXT DEFAULT '',
    safe_texts TEXT DEFAULT '[]',
    targets TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS keys(
    key TEXT PRIMARY KEY,
    days INTEGER,
    created_at INTEGER,
    used_by INTEGER,
    used_at INTEGER
);
"""
)


def get_user(uid: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO users(tg_id) VALUES(?)", (uid,))
    db.commit()
    return db.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()


def set_user(uid: int, **kw):
    get_user(uid)
    cols = ", ".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE users SET {cols} WHERE tg_id=?", (*kw.values(), uid))
    db.commit()


def has_license(uid: int) -> bool:
    u = get_user(uid)
    return bool(u["unlimited"]) or u["expires_at"] > time.time()


def license_str(uid: int) -> str:
    u = get_user(uid)
    if u["unlimited"]:
        return "♾ без ограничений"
    if u["expires_at"] > time.time():
        return "до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(u["expires_at"]))
    return "нет"


def activate_key(uid: int, key: str) -> str | None:
    row = db.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
    if not row or row["used_by"]:
        return None
    u = get_user(uid)
    if row["days"] == -1:
        set_user(uid, unlimited=1)
    elif not u["unlimited"]:
        base = max(time.time(), u["expires_at"])
        set_user(uid, expires_at=int(base + row["days"] * 86400))
    db.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=?", (uid, int(time.time()), key))
    db.commit()
    return "навсегда" if row["days"] == -1 else f"{row['days']} дн."


def new_key(days: int) -> str:
    raw = secrets.token_hex(8).upper()
    key = "-".join(raw[i : i + 4] for i in range(0, 16, 4))
    db.execute("INSERT INTO keys(key, days, created_at) VALUES(?,?,?)", (key, days, int(time.time())))
    db.commit()
    return key


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class S(StatesGroup):
    key = State()
    phone = State()
    code = State()
    pwd = State()
    text = State()
    safe = State()
    interval = State()
    targets = State()
    gen_qty = State()
    del_key = State()


# ───────────────────────── КЛАВИАТУРЫ ─────────────────────────
def main_kb(uid: int):
    u = get_user(uid)
    b = InlineKeyboardBuilder()
    b.button(text="👤 Аккаунт" + (" ✅" if u["session"] else ""), callback_data="m:acc")
    b.button(text="🔑 Активировать ключ", callback_data="m:key")
    b.button(
        text="🔄 Режим: " + ("🛡 Безопасная" if u["mode"] == "safe" else "Обычная"),
        callback_data="m:mode",
    )
    b.button(text="✏️ Текст(ы)", callback_data="m:text")
    b.button(text="⏱ Интервал", callback_data="m:int")
    b.button(text="📋 Чаты", callback_data="m:targets")
    if uid in TASKS and not TASKS[uid].done():
        b.button(text="⏹ Остановить", callback_data="m:stop")
    else:
        b.button(text="▶️ Запустить", callback_data="m:start")
    if uid in ADMIN_IDS:
        b.button(text="⚙️ Админ-панель", callback_data="a:menu")
    b.adjust(1, 1, 1, 2, 1, 1, 1)
    return b.as_markup()


def status_text(uid: int) -> str:
    u = get_user(uid)
    targets = json.loads(u["targets"])
    safe = json.loads(u["safe_texts"])
    running = uid in TASKS and not TASKS[uid].done()
    return (
        "📨 <b>Панель рассылки</b>\n\n"
        f"Лицензия: {license_str(uid)}\n"
        f"Аккаунт: {'подключён' if u['session'] else 'не подключён'}\n"
        f"Режим: {'🛡 безопасная' if u['mode'] == 'safe' else 'обычная'}\n"
        f"Интервал: {u['interval']} сек"
        + (" (±20%)" if u["mode"] == "safe" else "")
        + f"\nТекст (обычный): {'задан' if u['normal_text'] else 'нет'}\n"
        f"Тексты (безопасный): {len(safe)}/3\n"
        f"Чатов: {len(targets)}\n"
        f"Статус: {'🟢 работает' if running else '🔴 остановлена'}"
    )


async def show_menu(m: Message):
    await m.answer(status_text(m.chat.id), reply_markup=main_kb(m.chat.id), parse_mode="HTML")


def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать ключи", callback_data="a:gen")
    b.button(text="📜 Список ключей", callback_data="a:list")
    b.button(text="🗑 Удалить ключ", callback_data="a:del")
    b.button(text="👥 Пользователи", callback_data="a:users")
    b.button(text="⬅️ Назад", callback_data="m:back")
    b.adjust(2, 2, 1)
    return b.as_markup()


# ───────────────────────── ОБЩИЕ ХЕНДЛЕРЫ ─────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m)


@router.callback_query(F.data == "m:back")
async def back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer(status_text(c.from_user.id), reply_markup=main_kb(c.from_user.id), parse_mode="HTML")
    await c.answer()


def need_license(fn):
    async def wrapper(c: CallbackQuery, state: FSMContext):
        if not has_license(c.from_user.id):
            await c.answer("Нужна активная лицензия. Активируйте ключ.", show_alert=True)
            return
        await fn(c, state)

    return wrapper


@router.callback_query(F.data == "m:key")
async def ask_key(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.key)
    await c.message.answer("Отправьте ключ активации:")
    await c.answer()


@router.message(S.key)
async def got_key(m: Message, state: FSMContext):
    res = activate_key(m.from_user.id, m.text.strip().upper())
    await state.clear()
    await m.answer(f"✅ Ключ активирован: {res}" if res else "❌ Ключ неверный или уже использован.")
    await show_menu(m)


# ───────────────────────── ВХОД В АККАУНТ ─────────────────────────
@router.callback_query(F.data == "m:acc")
@need_license
async def acc(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if get_user(uid)["session"]:
        b = InlineKeyboardBuilder()
        b.button(text="🚪 Выйти из аккаунта", callback_data="m:logout")
        b.button(text="⬅️ Назад", callback_data="m:back")
        b.adjust(1)
        await c.message.answer("Аккаунт уже подключён.", reply_markup=b.as_markup())
    else:
        await state.set_state(S.phone)
        await c.message.answer("Отправьте номер телефона в формате +380XXXXXXXXX:")
    await c.answer()


@router.callback_query(F.data == "m:logout")
async def logout(c: CallbackQuery):
    uid = c.from_user.id
    if uid in TASKS:
        TASKS[uid].cancel()
    set_user(uid, session=None)
    await c.message.answer("Аккаунт отключён.")
    await c.answer()


@router.message(S.phone)
async def got_phone(m: Message, state: FSMContext):
    uid = m.from_user.id
    phone = m.text.strip().replace(" ", "")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        await m.answer(f"Ошибка: {e}")
        return
    PENDING[uid] = client
    await state.update_data(phone=phone, hash=sent.phone_code_hash)
    await state.set_state(S.code)
    await m.answer(
        "Код отправлен в Telegram. Введите его <b>через дефисы</b>, например <code>1-2-3-4-5</code> "
        "(если отправить код целиком, Telegram может его заблокировать).",
        parse_mode="HTML",
    )


async def finish_login(uid: int, m: Message, state: FSMContext):
    client = PENDING.pop(uid)
    set_user(uid, session=client.session.save())
    await client.disconnect()
    await state.clear()
    await m.answer("✅ Аккаунт подключён.")
    await show_menu(m)


@router.message(S.code)
async def got_code(m: Message, state: FSMContext):
    uid = m.from_user.id
    client = PENDING.get(uid)
    data = await state.get_data()
    code = re.sub(r"\D", "", m.text)
    try:
        await client.sign_in(data["phone"], code, phone_code_hash=data["hash"])
    except SessionPasswordNeededError:
        await state.set_state(S.pwd)
        await m.answer("Включена двухфакторная защита. Введите пароль:")
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await m.answer("Код неверный или просрочен. Попробуйте ещё раз (или /start и заново).")
        return
    await finish_login(uid, m, state)


@router.message(S.pwd)
async def got_pwd(m: Message, state: FSMContext):
    uid = m.from_user.id
    try:
        await PENDING[uid].sign_in(password=m.text)
    except Exception as e:
        await m.answer(f"Неверный пароль: {e}")
        return
    try:
        await m.delete()  # убираем пароль из чата
    except Exception:
        pass
    await finish_login(uid, m, state)


# ───────────────────────── НАСТРОЙКИ ─────────────────────────
@router.callback_query(F.data == "m:mode")
async def toggle_mode(c: CallbackQuery):
    uid = c.from_user.id
    new = "safe" if get_user(uid)["mode"] == "normal" else "normal"
    set_user(uid, mode=new)
    await c.message.edit_text(status_text(uid), reply_markup=main_kb(uid), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data == "m:text")
async def ask_text(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if get_user(uid)["mode"] == "safe":
        await state.set_state(S.safe)
        await state.update_data(texts=[])
        await c.message.answer("🛡 Безопасный режим. Отправьте текст №1 из 3:")
    else:
        await state.set_state(S.text)
        await c.message.answer("Отправьте текст рассылки:")
    await c.answer()


@router.message(S.text)
async def got_text(m: Message, state: FSMContext):
    set_user(m.from_user.id, normal_text=m.html_text)
    await state.clear()
    await m.answer("✅ Текст сохранён.")
    await show_menu(m)


@router.message(S.safe)
async def got_safe(m: Message, state: FSMContext):
    texts = (await state.get_data())["texts"] + [m.html_text]
    if len(texts) < 3:
        await state.update_data(texts=texts)
        await m.answer(f"Текст №{len(texts)} принят. Отправьте текст №{len(texts) + 1} из 3:")
        return
    set_user(m.from_user.id, safe_texts=json.dumps(texts))
    await state.clear()
    await m.answer("✅ Все 3 текста сохранены.")
    await show_menu(m)


@router.callback_query(F.data == "m:int")
async def ask_int(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.interval)
    await c.message.answer("Интервал между кругами рассылки в секундах (минимум 30):")
    await c.answer()


@router.message(S.interval)
async def got_int(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text) < 30:
        await m.answer("Введите число не меньше 30.")
        return
    set_user(m.from_user.id, interval=int(m.text))
    await state.clear()
    await m.answer("✅ Интервал сохранён.")
    await show_menu(m)


@router.callback_query(F.data == "m:targets")
async def ask_targets(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.targets)
    await c.message.answer(
        "Отправьте список чатов — по одному в строке: <code>@username</code>, "
        "ссылку <code>t.me/username</code> или числовой ID.\n"
        "Рассылка идёт только в чаты, где вы состоите и можете писать.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(S.targets)
async def got_targets(m: Message, state: FSMContext):
    items = [x.strip() for x in m.text.splitlines() if x.strip()]
    set_user(m.from_user.id, targets=json.dumps(items))
    await state.clear()
    await m.answer(f"✅ Сохранено чатов: {len(items)}")
    await show_menu(m)


# ───────────────────────── РАССЫЛКА ─────────────────────────
def parse_target(t: str):
    t = t.strip()
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    t = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", t).lstrip("@")
    return t


async def broadcast(uid: int):
    u = get_user(uid)
    client = TelegramClient(StringSession(u["session"]), API_ID, API_HASH)
    await client.connect()
    last_text = None
    try:
        if not await client.is_user_authorized():
            await bot.send_message(uid, "❌ Сессия аккаунта недействительна. Войдите заново.")
            return
        while True:
            if not has_license(uid):
                await bot.send_message(uid, "⛔ Лицензия закончилась, рассылка остановлена.")
                return
            u = get_user(uid)  # настройки перечитываются каждый круг
            targets = json.loads(u["targets"])
            safe = json.loads(u["safe_texts"])
            safe_mode = u["mode"] == "safe"
            sent = failed = 0
            for t in targets:
                if safe_mode:
                    if len(safe) < 3:
                        await bot.send_message(uid, "⚠️ В безопасном режиме нужно 3 текста.")
                        return
                    text = random.choice([x for x in safe if x != last_text] or safe)
                    last_text = text
                else:
                    text = u["normal_text"]
                    if not text:
                        await bot.send_message(uid, "⚠️ Не задан текст рассылки.")
                        return
                try:
                    await client.send_message(parse_target(t), text, parse_mode="html")
                    sent += 1
                except FloodWaitError as e:
                    await bot.send_message(uid, f"⏳ FloodWait: жду {e.seconds} сек.")
                    await asyncio.sleep(e.seconds + 5)
                except Exception as e:
                    failed += 1
                    logging.warning("send to %s failed: %s", t, e)
                await asyncio.sleep(random.uniform(3, 7))  # пауза между чатами
            await bot.send_message(uid, f"✅ Круг завершён: отправлено {sent}, ошибок {failed}.")
            wait = u["interval"]
            if safe_mode:
                wait *= random.uniform(0.8, 1.2)  # ±20%
            await asyncio.sleep(wait)
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()


@router.callback_query(F.data == "m:start")
@need_license
async def start_bc(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    u = get_user(uid)
    if not u["session"]:
        await c.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    if not json.loads(u["targets"]):
        await c.answer("Добавьте чаты.", show_alert=True)
        return
    if uid in TASKS and not TASKS[uid].done():
        await c.answer("Уже запущена.")
        return
    TASKS[uid] = asyncio.create_task(broadcast(uid))
    await c.message.edit_text(status_text(uid), reply_markup=main_kb(uid), parse_mode="HTML")
    await c.answer("Запущено")


@router.callback_query(F.data == "m:stop")
async def stop_bc(c: CallbackQuery):
    uid = c.from_user.id
    if uid in TASKS:
        TASKS[uid].cancel()
    await asyncio.sleep(0.3)
    await c.message.edit_text(status_text(uid), reply_markup=main_kb(uid), parse_mode="HTML")
    await c.answer("Остановлено")


# ───────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────
def is_admin(c: CallbackQuery) -> bool:
    return c.from_user.id in ADMIN_IDS


@router.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id in ADMIN_IDS:
        await m.answer("⚙️ Админ-панель", reply_markup=admin_kb())


@router.callback_query(F.data == "a:menu")
async def admin_menu(c: CallbackQuery):
    if is_admin(c):
        await c.message.answer("⚙️ Админ-панель", reply_markup=admin_kb())
    await c.answer()


@router.callback_query(F.data == "a:gen")
async def admin_gen(c: CallbackQuery):
    if not is_admin(c):
        return
    b = InlineKeyboardBuilder()
    for d in (1, 2, 3, 4, 30, 360):
        b.button(text=f"{d} дн.", callback_data=f"a:days:{d}")
    b.button(text="♾ Без ограничений", callback_data="a:days:-1")
    b.adjust(3, 3, 1)
    await c.message.answer("На какой срок ключ?", reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("a:days:"))
async def admin_days(c: CallbackQuery, state: FSMContext):
    if not is_admin(c):
        return
    await state.set_state(S.gen_qty)
    await state.update_data(days=int(c.data.split(":")[2]))
    await c.message.answer("Сколько ключей создать? (1–50)")
    await c.answer()


@router.message(S.gen_qty)
async def admin_qty(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 50:
        await m.answer("Введите число от 1 до 50.")
        return
    days = (await state.get_data())["days"]
    keys = [new_key(days) for _ in range(int(m.text))]
    await state.clear()
    label = "без ограничений" if days == -1 else f"{days} дн."
    await m.answer(f"Ключи ({label}):\n\n" + "\n".join(f"<code>{k}</code>" for k in keys), parse_mode="HTML")


@router.callback_query(F.data == "a:list")
async def admin_list(c: CallbackQuery):
    if not is_admin(c):
        return
    rows = db.execute("SELECT * FROM keys ORDER BY created_at DESC LIMIT 40").fetchall()
    if not rows:
        await c.message.answer("Ключей нет.")
    else:
        lines = []
        for r in rows:
            d = "∞" if r["days"] == -1 else f"{r['days']}д"
            st = f"использован {r['used_by']}" if r["used_by"] else "свободен"
            lines.append(f"<code>{r['key']}</code> | {d} | {st}")
        await c.message.answer("\n".join(lines), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data == "a:del")
async def admin_del(c: CallbackQuery, state: FSMContext):
    if not is_admin(c):
        return
    await state.set_state(S.del_key)
    await c.message.answer("Отправьте ключ для удаления:")
    await c.answer()


@router.message(S.del_key)
async def admin_del_do(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    cur = db.execute("DELETE FROM keys WHERE key=?", (m.text.strip().upper(),))
    db.commit()
    await state.clear()
    await m.answer("🗑 Удалён." if cur.rowcount else "Ключ не найден.")


@router.callback_query(F.data == "a:users")
async def admin_users(c: CallbackQuery):
    if not is_admin(c):
        return
    rows = db.execute("SELECT * FROM users").fetchall()
    active = [r for r in rows if r["unlimited"] or r["expires_at"] > time.time()]
    lines = [f"Всего: {len(rows)}, с лицензией: {len(active)}\n"]
    for r in active[:40]:
        exp = "∞" if r["unlimited"] else time.strftime("%d.%m.%Y", time.localtime(r["expires_at"]))
        run = "🟢" if r["tg_id"] in TASKS and not TASKS[r["tg_id"]].done() else "⚪"
        lines.append(f"{run} <code>{r['tg_id']}</code> — до {exp}")
    await c.message.answer("\n".join(lines), parse_mode="HTML")
    await c.answer()


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
