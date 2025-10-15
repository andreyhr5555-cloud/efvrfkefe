import os
import logging
from datetime import datetime
import asyncio
from threading import Thread

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from supabase import create_client, Client
from aiohttp import web

# --- Logging ---
logging.basicConfig(level=logging.INFO)

# --- Environment variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN = os.getenv("ADMIN_USERNAME", "denisHr55")
HR_LIST = os.getenv("HR_LIST", "mkkdko,Annahrg25,sun_crazy,dooro4ka,nuttupp,luilu_hr,Dmytry44gg,lilkalinaa_rabotka,kirusiyaaa15,VladHR27,sophie_hr,DimitryHr,karisha522,arinaa_hr").split(",")
IT_LIST = os.getenv("IT_LIST", "denishr55").split(",")
CATEGORIES_IT = ["Работа юа", "Джубл", "Телеграм", "Таргет", "Другое"]

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("BOT_TOKEN, SUPABASE_URL, or SUPABASE_KEY not set in environment variables")

# --- Supabase client ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Bot and dispatcher ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# --- FSM States ---
class ExpenseFSM(StatesGroup):
    amount = State()
    photo = State()
    cash = State()
    resource = State()
    to_admin = State()

class IncomeFSM(StatesGroup):
    amount = State()

class GiveFSM(StatesGroup):
    category = State()
    user = State()
    amount = State()

# --- Keyboards ---
def main_keyboard(username):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    if username == ADMIN:
        kb.add(KeyboardButton("Админ"))
    if username in HR_LIST or username in IT_LIST:
        kb.add(KeyboardButton("Потратил"))
        kb.add(KeyboardButton("Баланс"))
    if username in IT_LIST:
        kb.add(KeyboardButton("Пришли"))
    return kb

def admin_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Перевести пользователям"))
    kb.add(KeyboardButton("ПОПОЛНИТЬ баланс"))
    kb.add(KeyboardButton("Статистика"))
    kb.add(KeyboardButton("Балансы пользователей"))
    kb.add(KeyboardButton("Должен"))
    return kb

def resource_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    for cat in CATEGORIES_IT:
        kb.add(KeyboardButton(cat))
    kb.add(KeyboardButton("Назад"))
    return kb

# --- Database helpers ---
def get_user(username):
    resp = supabase.table("users").select("*").eq("username", username).execute()
    if resp.data:
        return resp.data[0]
    return None

def update_user_balance(username, delta):
    user = get_user(username)
    if user:
        new_balance = user["balance"] + delta
        supabase.table("users").update({"balance": new_balance}).eq("username", username).execute()
        return new_balance
    return None

def add_expense(username, amount, resource, photo_id, is_cash, to_admin):
    now = datetime.now().isoformat()
    supabase.table("expenses").insert({
        "username": username,
        "amount": amount,
        "resource": resource,
        "photo_id": photo_id,
        "is_cash": is_cash,
        "to_admin": to_admin,
        "datetime": now,
        "status": "pending"
    }).execute()

def add_income(username, amount):
    now = datetime.now().isoformat()
    supabase.table("incomes").insert({
        "username": username,
        "amount": amount,
        "datetime": now
    }).execute()

def get_user_balance(username):
    user = get_user(username)
    if user:
        return user["balance"]
    return 0

def get_all_balances():
    return supabase.table("users").select("username, balance, role").execute().data

def get_stats():
    return supabase.table("expenses").select("*").execute().data

# --- Handlers ---
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    username = msg.from_user.username
    user = get_user(username)
    if not user:
        role = "admin" if username == ADMIN else ("it" if username in IT_LIST else ("hr" if username in HR_LIST else "unknown"))
        supabase.table("users").insert({
            "username": username, "role": role, "balance": 0
        }).execute()
    await msg.answer(f"Добро пожаловать! Ваш профиль: {username}", reply_markup=main_keyboard(username))

# --- Income ---
@dp.message_handler(lambda msg: msg.text == "Пришли" and (msg.from_user.username in IT_LIST or msg.from_user.username == ADMIN))
async def it_income(msg: types.Message):
    await msg.answer("Введите сумму пополнения:", reply_markup=types.ReplyKeyboardRemove())
    await IncomeFSM.amount.set()

@dp.message_handler(state=IncomeFSM.amount)
async def process_income(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text.replace(",", "."))
        username = msg.from_user.username
        add_income(username, amount)
        update_user_balance(username, amount)
        await msg.answer(f"Баланс пополнен на {amount}. Новый баланс: {get_user_balance(username)}", reply_markup=main_keyboard(username))
    except Exception:
        await msg.answer("Ошибка ввода суммы! Попробуйте снова.")
    await state.finish()

# --- Expense IT/HR ---
@dp.message_handler(lambda msg: msg.text == "Потратил" and msg.from_user.username in IT_LIST)
async def it_expense(msg: types.Message):
    await msg.answer("Выберите ресурс:", reply_markup=resource_keyboard())
    await ExpenseFSM.resource.set()

@dp.message_handler(lambda msg: msg.text == "Потратил" and msg.from_user.username in HR_LIST)
async def hr_expense(msg: types.Message):
    await msg.answer("Введите сумму расхода:", reply_markup=types.ReplyKeyboardRemove())
    await ExpenseFSM.amount.set()

@dp.message_handler(state=ExpenseFSM.resource)
async def it_expense_resource(msg: types.Message, state: FSMContext):
    if msg.text not in CATEGORIES_IT:
        await msg.answer("Выберите категорию из списка.")
        return
    await state.update_data(resource=msg.text)
    await msg.answer("Введите сумму расхода:", reply_markup=types.ReplyKeyboardRemove())
    await ExpenseFSM.amount.set()

@dp.message_handler(state=ExpenseFSM.amount)
async def expense_amount(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text.replace(",", "."))
        await state.update_data(amount=amount)
        await msg.answer("Прикрепите фото или напишите 'наличка'")
        await ExpenseFSM.photo.set()
    except Exception:
        await msg.answer("Ошибка ввода суммы! Попробуйте снова.")

@dp.message_handler(content_types=['photo'], state=ExpenseFSM.photo)
async def expense_photo(msg: types.Message, state: FSMContext):
    file_id = msg.photo[-1].file_id
    await state.update_data(photo_id=file_id, is_cash=False)
    await msg.answer("Отправить админу? (да/нет)")
    await ExpenseFSM.to_admin.set()

@dp.message_handler(lambda msg: msg.text.lower() == "наличка", state=ExpenseFSM.photo)
async def expense_cash(msg: types.Message, state: FSMContext):
    await state.update_data(photo_id=None, is_cash=True)
    await msg.answer("Отправить админу? (да/нет)")
    await ExpenseFSM.to_admin.set()

@dp.message_handler(state=ExpenseFSM.to_admin)
async def expense_to_admin(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    username = msg.from_user.username
    amount = data.get("amount")
    resource = data.get("resource", "HR-расход")
    photo_id = data.get("photo_id")
    is_cash = data.get("is_cash")
    to_admin = msg.text.lower() == "да"
    add_expense(username, amount, resource, photo_id, is_cash, to_admin)
    update_user_balance(username, -amount)
    if to_admin:
        text = f"Новый расход от @{username} на {amount} грн, категория: {resource}"
        if photo_id:
            await bot.send_photo(chat_id=f"@{ADMIN}", photo=photo_id, caption=text)
        else:
            await bot.send_message(chat_id=f"@{ADMIN}", text=text + " (наличка)")
        await msg.answer("Отправлено админу!", reply_markup=main_keyboard(username))
    else:
        await msg.answer("Расход списан с баланса.", reply_markup=main_keyboard(username))
    await state.finish()

# --- User balance ---
@dp.message_handler(lambda msg: msg.text == "Баланс" and (msg.from_user.username in HR_LIST + IT_LIST or msg.from_user.username == ADMIN))
async def user_balance(msg: types.Message):
    balance = get_user_balance(msg.from_user.username)
    await msg.answer(f"Ваш баланс: {balance} грн", reply_markup=main_keyboard(msg.from_user.username))

# --- Admin: Give money ---
@dp.message_handler(lambda msg: msg.text == "Перевести пользователям" and msg.from_user.username == ADMIN)
async def admin_give_money(msg: types.Message):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("hr"), KeyboardButton("it"))
    await msg.answer("Выберите категорию:", reply_markup=kb)
    await GiveFSM.category.set()

@dp.message_handler(state=GiveFSM.category)
async def admin_give_category(msg: types.Message, state: FSMContext):
    cat = msg.text.strip().lower()
    if cat not in ["hr", "it"]:
        await msg.answer("Выберите hr или it")
        return
    await state.update_data(category=cat)
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    lst = HR_LIST if cat == "hr" else IT_LIST
    for u in lst:
        kb.add(KeyboardButton(u))
    await msg.answer("Выберите пользователя:", reply_markup=kb)
    await GiveFSM.user.set()

@dp.message_handler(state=GiveFSM.user)
async def admin_give_user(msg: types.Message, state: FSMContext):
    user = msg.text.replace("@", "")
    data = await state.get_data()
    cat = data.get("category")
    if cat == "hr" and user not in HR_LIST:
        await msg.answer("Выберите пользователя из HR списка")
        return
    if cat == "it" and user not in IT_LIST:
        await msg.answer("Выберите пользователя из IT списка")
        return
    await state.update_data(user=user)
    await msg.answer("Введите сумму:")
    await GiveFSM.amount.set()

@dp.message_handler(state=GiveFSM.amount)
async def admin_give_amount(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text.replace(",", "."))
        data = await state.get_data()
        user = data.get("user")
        update_user_balance(user, amount)
        update_user_balance(ADMIN, -amount)
        await bot.send_message(chat_id=f"@{user}", text=f"Вам поступило {amount} грн от Админа. Новый баланс: {get_user_balance(user)}")
        await msg.answer(f"Вы выдали {amount} грн @{user}. Баланс пользователя: {get_user_balance(user)}", reply_markup=admin_keyboard())
    except Exception:
        await msg.answer("Ошибка ввода суммы! Попробуйте снова.")
    await state.finish()

# --- Admin: Balances and stats ---
@dp.message_handler(lambda msg: msg.text == "Балансы пользователей" and msg.from_user.username == ADMIN)
async def admin_balances(msg: types.Message):
    balances = get_all_balances()
    out = "Балансы пользователей:\n"
    for u in balances:
        out += f"@{u['username']} ({u['role']}): {u['balance']} грн\n"
    await msg.answer(out)

@dp.message_handler(lambda msg: msg.text == "Статистика" and msg.from_user.username == ADMIN)
async def admin_stats(msg: types.Message):
    stats = get_stats()
    out = "Статистика расходов:\n"
    total = 0
    for s in stats:
        out += f"@{s['username']} | {s['datetime']} | {s['amount']} грн | {s['resource']}\n"
        total += s["amount"]
    out += f"\nВсего потрачено: {total} грн"
    await msg.answer(out)

# --- Healthcheck endpoint ---
async def healthcheck(request):
    return web.Response(text="OK")

def setup_healthcheck(app):
    app.router.add_get("/", healthcheck)

# --- Keep-alive polling task ---
async def keep_alive():
    while True:
        try:
            await bot.get_me()
            await asyncio.sleep(600)
        except Exception:
            await asyncio.sleep(60)

# --- Main ---
if __name__ == '__main__':
    port = int(os.getenv("PORT", 8000))
    app = web.Application()
    setup_healthcheck(app)

    # Запуск веб-сервера Render в отдельном потоке
    Thread(target=lambda: web.run_app(app, host="0.0.0.0", port=port)).start()

    # Запуск polling
    loop = asyncio.get_event_loop()
    loop.create_task(keep_alive())
    executor.start_polling(dp, skip_updates=True)
