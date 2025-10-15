# bot.py
"""
BUHGALTERIYA — Telegram bot
- aiogram v3 FSM-driven flows for HR / IT / Admin
- Supabase (Postgres + Storage) for persistence
- Long-polling by default; webhook-ready if WEBHOOK_URL set
"""

import os
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
from io import BytesIO
from typing import Optional

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# FastAPI for webhook mode
from fastapi import FastAPI, Request
import uvicorn

# Supabase client
from supabase import create_client, Client

load_dotenv()

# ============ Config ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "denisHr55").lstrip("@")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")  # e.g. "@mkkdko:hr,@denishr55:it,@denisHr55:admin"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # if set, webhook mode
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "receipts")  # optional

# ============ Logging ============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ Supabase ============
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase URL/KEY not set. Bot will try to operate but DB calls will fail.")
    supabase: Optional[Client] = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============ Allowed users -> map username -> role ============
allowed_map = {}
for token in ALLOWED_USERS_RAW.split(","):
    token = token.strip()
    if not token:
        continue
    if ":" in token:
        name, role = token.split(":", 1)
        allowed_map[name.lstrip("@")] = role
    else:
        allowed_map[token.lstrip("@")] = "hr"

# Always ensure admin present
allowed_map[ADMIN_USERNAME] = "admin"

# ============ Aiogram setup ============
bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# FastAPI app for webhook mode
app = FastAPI()


# ============ FSM States ============
class HRStates(StatesGroup):
    waiting_amount = State()
    waiting_photo = State()
    waiting_confirm = State()


class ITStates(StatesGroup):
    waiting_choice = State()  # received / spent
    waiting_amount = State()
    waiting_resource = State()
    waiting_photo = State()
    waiting_confirm = State()


class AdminStates(StatesGroup):
    waiting_give_to = State()
    waiting_give_amount = State()


# ============ Helpers: DB wrappers ============
def ensure_user_db(tg_id: int, username: Optional[str]):
    """
    Ensure user exists in users table. Returns user record dict or None if not allowed.
    """
    username = (username or "").lstrip("@")
    role = allowed_map.get(username)
    # if username equals ADMIN_USERNAME, make sure role admin
    if username == ADMIN_USERNAME:
        role = "admin"
    if role not in ("hr", "it", "admin"):
        return None

    if not supabase:
        # Local fallback — return a mock dict (not persisted)
        return {"id": None, "tg_id": tg_id, "username": username, "role": role}

    # check by tg_id
    res = supabase.table("users").select("*").eq("tg_id", tg_id).maybe_single().execute()
    if res.error:
        logger.error("Supabase error on select users: %s", res.error)
    if res.data:
        return res.data

    # try to find by username
    res2 = supabase.table("users").select("*").eq("username", username).maybe_single().execute()
    if res2.data:
        # if exists but no tg_id -> update
        if res2.data.get("tg_id") is None:
            supabase.table("users").update({"tg_id": tg_id}).eq("id", res2.data["id"]).execute()
            res2.data["tg_id"] = tg_id
        return res2.data

    # create
    inserted = supabase.table("users").insert({"tg_id": tg_id, "username": username, "role": role}).execute()
    if inserted.error:
        logger.error("Supabase error on insert user: %s", inserted.error)
        return None
    user = inserted.data[0]
    # create balance row
    supabase.table("balances").upsert({"user_id": user["id"], "balance": 0}).execute()
    return user


def get_user_by_username(username: str):
    username = username.lstrip("@")
    if not supabase:
        return None
    r = supabase.table("users").select("*").eq("username", username).maybe_single().execute()
    return r.data


def get_user_by_tg_id(tg_id: int):
    if not supabase:
        return None
    r = supabase.table("users").select("*").eq("tg_id", tg_id).maybe_single().execute()
    return r.data


def get_balance(user_id):
    if not supabase or not user_id:
        return Decimal("0")
    r = supabase.table("balances").select("balance").eq("user_id", user_id).maybe_single().execute()
    if r.data:
        return Decimal(str(r.data.get("balance", 0)))
    return Decimal("0")


def set_balance(user_id, new_balance: Decimal):
    if not supabase or not user_id:
        return
    supabase.table("balances").update({"balance": str(new_balance), "updated_at": datetime.utcnow().isoformat()}).eq("user_id", user_id).execute()


def change_balance(user_id, delta: Decimal):
    if not supabase or not user_id:
        return
    cur = get_balance(user_id)
    new = cur + delta
    set_balance(user_id, new)
    # write transaction
    supabase.table("transactions").insert({
        "from_user": None if delta > 0 else None,
        "to_user": None if delta < 0 else None,
        "amount": str(abs(delta)),
        "type": "credit" if delta > 0 else "debit",
        "created_at": datetime.utcnow().isoformat()
    }).execute()


def insert_expense(user_id, username, role, amount: Decimal, resource: Optional[str], image_url: Optional[str], note: Optional[str]):
    if not supabase:
        return None
    row = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "amount": str(amount),
        "currency": "UAH",
        "resource": resource,
        "image_url": image_url,
        "note": note,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }
    res = supabase.table("expenses").insert(row).execute()
    if res.error:
        logger.error("Insert expense error: %s", res.error)
        return None
    return res.data[0]


def mark_expense_paid(expense_id, paid_by_user_id):
    if not supabase:
        return
    supabase.table("expenses").update({"status": "paid", "paid_at": datetime.utcnow().isoformat(), "paid_by": paid_by_user_id}).eq("id", expense_id).execute()


# ============ Helpers: keyboards ============
def hr_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Потратил", callback_data="hr:spent")],
        [InlineKeyboardButton("Баланс", callback_data="balance")],
    ])


def it_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Пришли", callback_data="it:received")],
        [InlineKeyboardButton("Потратил", callback_data="it:spent")],
        [InlineKeyboardButton("Баланс", callback_data="balance")],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("ДАЛ ДЕНЕГ", callback_data="admin:give")],
        [InlineKeyboardButton("Статистика", callback_data="admin:stats")],
    ])


def admin_expense_actions(expense_id: str, username: str, amount: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Оплатил", callback_data=f"admin:paid:{expense_id}")],
        [InlineKeyboardButton("Отложить (Должен)", callback_data=f"admin:due:{expense_id}")],
    ])


# ============ Storage helper ============
def upload_receipt_to_supabase(file_bytes: bytes, filename: str) -> Optional[str]:
    """
    Uploads file to Supabase Storage bucket and returns public URL (if successful).
    Requires SUPABASE_STORAGE_BUCKET to exist and public or signed URLs logic.
    """
    if not supabase:
        return None
    try:
        # Use a path with timestamp to avoid conflicts
        key = f"receipts/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{filename}"
        res = supabase.storage().from_(SUPABASE_STORAGE_BUCKET).upload(key, file_bytes)
        if res.error:
            logger.error("Supabase storage upload error: %s", res.error)
            return None
        # make public URL (works if bucket is public) - otherwise generate signed URL
        public_url = supabase.storage().from_(SUPABASE_STORAGE_BUCKET).get_public_url(key)
        return public_url.get("publicURL") or public_url.get("public_url")
    except Exception as e:
        logger.exception("Storage upload failed: %s", e)
        return None


# ============ Utilities ============
def parse_amount(text: str) -> Optional[Decimal]:
    if not text:
        return None
    # extract first number-like token
    s = ""
    for ch in text:
        if ch.isdigit() or ch == "." or ch == ",":
            s += "." if ch == "," else ch
        elif s:
            break
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ============ Handlers ============
@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message, state: FSMContext):
    user = ensure_user_db(message.from_user.id, message.from_user.username)
    if not user:
        await message.answer("Доступ запрещён. Свяжитесь с администратором.")
        return
    role = user["role"]
    if role == "hr":
        await message.answer("Добро пожаловать, HR. Вы в своём аккаунте.", reply_markup=hr_keyboard())
    elif role == "it":
        await message.answer("Добро пожаловать, IT. Вы в своём аккаунте.", reply_markup=it_keyboard())
    elif role == "admin":
        await message.answer("Добро пожаловать, Админ.", reply_markup=admin_keyboard())
    # reset any FSM
    await state.clear()


# ---------- Callback queries ----------
@dp.callback_query(lambda c: c.data == "hr:spent")
async def handle_hr_spent(cb: types.CallbackQuery, state: FSMContext):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "hr":
        await cb.answer("Только HR.", show_alert=True)
        return
    await state.set_state(HRStates.waiting_amount)
    await cb.message.answer("Отправь сумму (например: 1500) — затем прикрепи фото чека или напиши 'наличка'.")
    await cb.answer()


@dp.callback_query(lambda c: c.data == "it:received")
async def handle_it_received(cb: types.CallbackQuery, state: FSMContext):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "it":
        await cb.answer("Только IT.", show_alert=True)
        return
    await state.set_state(ITStates.waiting_amount)
    # mark flow as 'received' via context
    await state.update_data(it_action="received")
    await cb.message.answer("Отправь сумму, которая пришла на баланс (например: 5000).")
    await cb.answer()


@dp.callback_query(lambda c: c.data == "it:spent")
async def handle_it_spent(cb: types.CallbackQuery, state: FSMContext):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "it":
        await cb.answer("Только IT.", show_alert=True)
        return
    await state.set_state(ITStates.waiting_resource)
    await state.update_data(it_action="spent")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Работа юа", callback_data="res:Работа юа")],
        [InlineKeyboardButton("Джубл", callback_data="res:Джубл")],
        [InlineKeyboardButton("Телеграм", callback_data="res:Телеграм")],
        [InlineKeyboardButton("Таргет", callback_data="res:Таргет")],
        [InlineKeyboardButton("Другое", callback_data="res:Другое")],
    ])
    await cb.message.answer("Выберите ресурс:", reply_markup=kb)
    await cb.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("res:"))
async def handle_it_resource_select(cb: types.CallbackQuery, state: FSMContext):
    data = cb.data.split(":", 1)[1]
    await state.update_data(resource=data)
    await state.set_state(ITStates.waiting_amount)
    await cb.message.answer(f"Ресурс: {data}. Теперь отправь сумму и прикрепи фото чека (если есть).")
    await cb.answer()


@dp.callback_query(lambda c: c.data == "balance")
async def handle_balance(cb: types.CallbackQuery):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user:
        await cb.answer("Доступ запрещён.")
        return
    bal = get_balance(user["id"])
    await cb.message.answer(f"Текущий баланс @{user['username']}: {bal} UAH")
    await cb.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("admin:paid:"))
async def handle_admin_paid(cb: types.CallbackQuery):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "admin":
        await cb.answer("Только админ.", show_alert=True)
        return
    expense_id = cb.data.split(":", 2)[2]
    # find expense
    if not supabase:
        await cb.answer("DB unavailable.", show_alert=True)
        return
    res = supabase.table("expenses").select("*").eq("id", expense_id).maybe_single().execute()
    if not res.data:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return
    exp = res.data
    if exp["status"] == "paid":
        await cb.answer("Уже оплачено.", show_alert=True)
        return
    # transfer: admin -> user
    amount = Decimal(str(exp["amount"]))
    admin_row = get_user_by_tg_id(cb.from_user.id)
    target_row = get_user_by_username(exp["username"])
    # update balances
    if admin_row and admin_row.get("id"):
        change_balance(admin_row["id"], -amount)
    if target_row and target_row.get("id"):
        change_balance(target_row["id"], amount)
    mark_expense_paid(expense_id, admin_row["id"] if admin_row else None)
    # notify user
    if target_row and target_row.get("tg_id"):
        await bot.send_message(chat_id=target_row["tg_id"], text=f"Вам зачислено {amount} UAH по заявке. Баланс обновлён.")
    await cb.answer("Отмечено как оплачено.")
    await cb.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(lambda c: c.data and c.data.startswith("admin:due:"))
async def handle_admin_due(cb: types.CallbackQuery):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "admin":
        await cb.answer("Только админ.", show_alert=True)
        return
    expense_id = cb.data.split(":", 2)[2]
    if not supabase:
        await cb.answer("DB unavailable.", show_alert=True)
        return
    # mark as due (leave pending but notify)
    supabase.table("expenses").update({"status": "pending"}).eq("id", expense_id).execute()
    res = supabase.table("expenses").select("*").eq("id", expense_id).maybe_single().execute()
    if res.data:
        target = get_user_by_username(res.data["username"])
        if target and target.get("tg_id"):
            await bot.send_message(chat_id=target["tg_id"], text=f"Ваша заявка ({res.data['amount']} UAH) отмечена как ожидающая оплаты.")
    await cb.answer("Отложено (Должен).")
    await cb.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(lambda c: c.data == "admin:give")
async def handle_admin_give(cb: types.CallbackQuery, state: FSMContext):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "admin":
        await cb.answer("Только админ.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_give_to)
    await cb.message.answer("Кому выдали деньги? Укажи @username (например: @mkkdko).")
    await cb.answer()


@dp.callback_query(lambda c: c.data == "admin:stats")
async def handle_admin_stats(cb: types.CallbackQuery):
    user = ensure_user_db(cb.from_user.id, cb.from_user.username)
    if not user or user["role"] != "admin":
        await cb.answer("Только админ.", show_alert=True)
        return
    if not supabase:
        await cb.answer("DB unavailable.", show_alert=True)
        return
    users = supabase.table("users").select("*").execute().data or []
    balances = supabase.table("balances").select("*").execute().data or []
    bmap = {b["user_id"]: b["balance"] for b in balances}
    lines = []
    for u in users:
        lines.append(f"@{u['username']} ({u['role']}): {bmap.get(u['id'], 0)} UAH")
    await cb.message.answer("Статистика по балансам:\n" + "\n".join(lines))
    await cb.answer()


# ---------- Message handlers for FSM flows ----------
@dp.message()
async def main_message_handler(message: types.Message, state: FSMContext):
    """
    Central message handler that dispatches based on current FSM state.
    Also handles casual messages like '/help'.
    """
    # help / commands
    text = (message.text or "").strip()
    if text.startswith("/help"):
        await message.answer("Используйте /start для входа. Кнопки доступны в меню.")
        return

    user = ensure_user_db(message.from_user.id, message.from_user.username)
    if not user:
        await message.answer("Доступ запрещён.")
        return

    state_name = await state.get_state()
    # HR flow
    if state_name == HRStates.waiting_amount.state:
        amount = parse_amount(text)
        if not amount:
            await message.answer("Не удалось распознать сумму. Отправь цифры (например: 1500).")
            return
        await state.update_data(amount=str(amount))
        await state.set_state(HRStates.waiting_photo)
        await message.answer("Прикрепи фото чека или напиши 'наличка'. Если нет — отправь 'нет'.")
        return

    if state_name == HRStates.waiting_photo.state:
        # accept photo or 'наличка' or 'нет'
        img_url = None
        note = None
        if message.photo:
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            bio = BytesIO()
            await photo.download(bio)
            bio.seek(0)
            filename = f"{message.from_user.id}_{photo.file_unique_id}.jpg"
            # upload to supabase storage if available
            img_url = upload_receipt_to_supabase(bio.read(), filename)
        elif text.lower() in ("наличка", "наличка.", "нал"):
            note = "наличка"
        elif text.lower() in ("нет", "no"):
            note = None
        else:
            # treat free text as note
            note = text

        data = await state.get_data()
        amount = Decimal(str(data.get("amount", "0")))
        exp = insert_expense(user_id=user.get("id"), username=user.get("username"), role=user.get("role"),
                             amount=amount, resource=None, image_url=img_url, note=note)
        # notify admin
        admin = get_user_by_username(ADMIN_USERNAME)
        caption = f"Новая расходка от @{user['username']} ({user['role']})\nСумма: {amount} UAH\nДата: {datetime.utcnow().isoformat()}"
        if admin and admin.get("tg_id"):
            if img_url:
                # send link + action buttons
                await bot.send_message(chat_id=admin["tg_id"], text=caption, reply_markup=admin_expense_actions(exp["id"], user["username"], str(amount)))
                await bot.send_message(chat_id=admin["tg_id"], text=f"Чек: {img_url}")
            else:
                await bot.send_message(chat_id=admin["tg_id"], text=caption, reply_markup=admin_expense_actions(exp["id"], user["username"], str(amount)))
        await message.answer("Заявка отправлена админу.")
        await state.clear()
        return

    # IT flow
    if state_name == ITStates.waiting_amount.state:
        data = await state.get_data()
        action = data.get("it_action")
        amount = parse_amount(text)
        if not amount:
            await message.answer("Не удалось распознать сумму. Отправь цифры (например: 1500).")
            return
        # if received -> add to balance immediately
        if action == "received":
            change_balance(user.get("id"), amount)
            await message.answer(f"Баланс пополнен на {amount} UAH. Текущий баланс: {get_balance(user.get('id'))} UAH")
            await state.clear()
            return
        # if spent -> proceed to photo/confirm
        resource = data.get("resource")
        # optionally parse photo from this same message
        img_url = None
        if message.photo:
            photo = message.photo[-1]
            bio = BytesIO()
            await photo.download(bio)
            bio.seek(0)
            filename = f"{message.from_user.id}_{photo.file_unique_id}.jpg"
            img_url = upload_receipt_to_supabase(bio.read(), filename)
        exp = insert_expense(user_id=user.get("id"), username=user.get("username"), role=user.get("role"),
                             amount=amount, resource=resource, image_url=img_url, note=None)
        # subtract from balance immediately (as requested)
        change_balance(user.get("id"), -amount)
        # notify admin
        admin = get_user_by_username(ADMIN_USERNAME)
        caption = f"Новая расходка IT от @{user['username']} ({resource})\nСумма: {amount} UAH\nДата: {datetime.utcnow().isoformat()}"
        if admin and admin.get("tg_id"):
            if img_url:
                await bot.send_message(chat_id=admin["tg_id"], text=caption, reply_markup=admin_expense_actions(exp["id"], user["username"], str(amount)))
                await bot.send_message(chat_id=admin["tg_id"], text=f"Чек: {img_url}")
            else:
                await bot.send_message(chat_id=admin["tg_id"], text=caption, reply_markup=admin_expense_actions(exp["id"], user["username"], str(amount)))
        await message.answer(f"Расход зарегистрирован и списан с баланса. Текущий баланс: {get_balance(user.get('id'))} UAH")
        await state.clear()
        return

    # Admin give flow
    if state_name == AdminStates.waiting_give_to.state:
        # expecting @username
        target = text.strip().split()[0]
        if not target.startswith("@"):
            await message.answer("Укажи username в формате @username.")
            return
        await state.update_data(give_to=target.lstrip("@"))
        await state.set_state(AdminStates.waiting_give_amount)
        await message.answer("Укажи сумму, которую дал (например: 1500).")
        return

    if state_name == AdminStates.waiting_give_amount.state:
        amount = parse_amount(text)
        if not amount:
            await message.answer("Не удалось распознать сумму. Отправь цифры (например: 1500).")
            return
        data = await state.get_data()
        to_username = data.get("give_to")
        # find DB rows
        admin_row = get_user_by_tg_id(message.from_user.id)
        target_row = get_user_by_username(to_username)
        if admin_row and admin_row.get("id"):
            change_balance(admin_row["id"], -amount)
        if target_row and target_row.get("id"):
            change_balance(target_row["id"], amount)
            if target_row.get("tg_id"):
                await bot.send_message(chat_id=target_row["tg_id"], text=f"Вам зачислено {amount} UAH от админа. Баланс: {get_balance(target_row['id'])} UAH")
        await message.answer("Операция выполнена.")
        await state.clear()
        return

    # Default: no FSM state matched
    await message.answer("Не распознал запрос. Используйте /start и кнопки меню.")


# ============ FastAPI webhook endpoint ============
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram webhook receiver for Render / web deployments.
    Use TELEGRAM_BOT_TOKEN to set webhook with Bot API:
    setWebhook(WEBHOOK_URL)
    """
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.feed_update(update)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ============ Startup / Run ============
if __name__ == "__main__":
    if WEBHOOK_URL:
        # set webhook (optional): can set externally via curl or Telegram Bot API
        logger.info("Running in webhook mode on port %s", PORT)
        uvicorn.run(app, host=HOST, port=PORT)
    else:
        logger.info("Running long-polling mode")
        dp.run_polling(bot)
