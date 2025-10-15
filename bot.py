import os
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI, Request
from supabase import create_client, Client
from dotenv import load_dotenv

# =========================
# Настройки окружения
# =========================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")

ALLOWED_ADMIN = "@denisHr55"
HR_USERS = [
    "@mkkdko", "@Annahrg25", "@sun_crazy", "@dooro4ka", "@nuttupp",
    "@luilu_hr", "@Dmytry44gg", "@lilkalinaa_rabotka", "@kirusiyaaa15",
    "@VladHR27", "@sophie_hr", "@DimitryHr", "@karisha522", "@arinaa_hr"
]
IT_USERS = ["@denishr55"]

IT_CATEGORIES = ["Работа юа", "Джубл", "Телеграм", "Таргет", "Другое"]

# =========================
# Инициализация
# =========================
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# FSM: Пошаговое внесение расхода
# =========================
class SpendForm(StatesGroup):
    waiting_for_category = State()  # IT только
    waiting_for_amount = State()
    waiting_for_comment = State()
    waiting_for_photo = State()

# =========================
# Вспомогательные функции
# =========================
def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

async def ensure_user_in_db(username: str, role: str):
    existing = supabase.table("users").select("*").eq("username", username).execute()
    if not existing.data:
        supabase.table("users").insert({
            "username": username,
            "role": role,
            "balance": 0,
            "created_at": now_str()
        }).execute()

async def get_balance(username: str) -> float:
    data = supabase.table("users").select("balance").eq("username", username).execute()
    return data.data[0]["balance"] if data.data else 0

async def update_balance(username: str, new_balance: float):
    supabase.table("users").update({"balance": new_balance}).eq("username", username).execute()

# =========================
# /start
# =========================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    username = f"@{message.from_user.username}" if message.from_user.username else f"id{message.from_user.id}"
    await state.clear()

    if username == ALLOWED_ADMIN:
        role = "admin"
        await ensure_user_in_db(username, role)
        kb = [
            [types.KeyboardButton(text="💰 ДАЛ ДЕНЕГ")],
            [types.KeyboardButton(text="📊 Статистика"), types.KeyboardButton(text="💸 Должен")],
        ]
        await message.answer("🔑 <b>Админ-панель</b>", reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))
    elif username in HR_USERS:
        role = "hr"
        await ensure_user_in_db(username, role)
        kb = [
            [types.KeyboardButton(text="💵 Потратил"), types.KeyboardButton(text="📥 Пришли")],
            [types.KeyboardButton(text="💰 Баланс")]
        ]
        await message.answer("👩‍💼 HR панель", reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))
    elif username in IT_USERS:
        role = "it"
        await ensure_user_in_db(username, role)
        kb = [
            [types.KeyboardButton(text="💵 Потратил"), types.KeyboardButton(text="📥 Пришли")],
            [types.KeyboardButton(text="💰 Баланс")]
        ]
        await message.answer("💻 IT панель", reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))
    else:
        await message.answer("❌ У вас нет доступа к этому боту.")

# =========================
# Баланс
# =========================
@dp.message(F.text == "💰 Баланс")
async def balance_handler(message: Message):
    username = f"@{message.from_user.username}"
    balance = await get_balance(username)
    await message.answer(f"💰 Текущий баланс: <b>{balance} грн</b>")

# =========================
# Пополнение
# =========================
@dp.message(F.text == "📥 Пришли")
async def plus_balance(message: Message):
    username = f"@{message.from_user.username}"
    await message.answer("Введите сумму для пополнения:")

    @dp.message()
    async def add_sum(msg: Message):
        try:
            amount = float(msg.text)
            old_balance = await get_balance(username)
            new_balance = old_balance + amount
            await update_balance(username, new_balance)
            await msg.answer(f"✅ Баланс пополнен на {amount} грн.\n💰 Новый баланс: {new_balance} грн.")
        except ValueError:
            await msg.answer("❌ Введите корректное число.")
        dp.message.handlers.pop()  # очистка

# =========================
# FSM: Потратил
# =========================
@dp.message(F.text == "💵 Потратил")
async def spent_start(message: Message, state: FSMContext):
    username = f"@{message.from_user.username}"
    if username in IT_USERS:
        kb = [[types.KeyboardButton(text=c)] for c in IT_CATEGORIES]
        await message.answer("Выберите категорию расхода:", reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))
        await state.set_state(SpendForm.waiting_for_category)
    else:
        await message.answer("Введите сумму, которую вы потратили:")
        await state.set_state(SpendForm.waiting_for_amount)

@dp.message(SpendForm.waiting_for_category)
async def process_category(message: Message, state: FSMContext):
    if message.text not in IT_CATEGORIES:
        await message.answer("❌ Выберите категорию из кнопок.")
        return
    await state.update_data(category=message.text)
    await message.answer("Введите сумму расхода:")
    await state.set_state(SpendForm.waiting_for_amount)

@dp.message(SpendForm.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        await state.update_data(amount=amount)
        await message.answer("Опишите, на что были потрачены деньги:")
        await state.set_state(SpendForm.waiting_for_comment)
    except ValueError:
        await message.answer("❌ Введите корректное число.")

@dp.message(SpendForm.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    await message.answer("📸 Отправьте фото или скрин чека, либо напишите 'нет'.")
    await state.set_state(SpendForm.waiting_for_photo)

@dp.message(SpendForm.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    data = await state.get_data()
    username = f"@{message.from_user.username}"
    amount = data['amount']
    comment = data['comment']
    category = data.get("category", None)

    # Списание с баланса
    old_balance = await get_balance(username)
    new_balance = old_balance - amount
    await update_balance(username, new_balance)

    supabase.table("expenses").insert({
        "username": username,
        "role": "it" if username in IT_USERS else "hr",
        "category": category,
        "amount": amount,
        "comment": comment,
        "photo_id": file_id,
        "created_at": now_str()
    }).execute()

    await message.answer(f"✅ Расход {amount} грн добавлен и списан с баланса.\n💰 Новый баланс: {new_balance} грн.")
    await bot.send_photo(chat_id=ALLOWED_ADMIN, photo=file_id,
                         caption=f"💵 <b>Расход от {username}</b>\n💰 {amount} грн\n📝 {comment}\n📂 {category or 'HR'}")
    await state.clear()

@dp.message(SpendForm.waiting_for_photo)
async def process_no_photo(message: Message, state: FSMContext):
    if message.text.lower() == "нет":
        data = await state.get_data()
        username = f"@{message.from_user.username}"
        amount = data['amount']
        comment = data['comment']
        category = data.get("category", None)

        old_balance = await get_balance(username)
        new_balance = old_balance - amount
        await update_balance(username, new_balance)

        supabase.table("expenses").insert({
            "username": username,
            "role": "it" if username in IT_USERS else "hr",
            "category": category,
            "amount": amount,
            "comment": comment,
            "photo_id": None,
            "created_at": now_str()
        }).execute()

        await message.answer(f"✅ Расход {amount} грн добавлен без фото и списан с баланса.\n💰 Новый баланс: {new_balance} грн.")
        await bot.send_message(chat_id=ALLOWED_ADMIN,
                               text=f"💵 <b>Расход от {username}</b>\n💰 {amount} грн\n📝 {comment}\n📂 {category or 'HR'}")
        await state.clear()
    else:
        await message.answer("❌ Отправьте фото или напишите 'нет'.")

# =========================
# FastAPI для Render
# =========================
app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok", "time": now_str()}

@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

# =========================
# Локальный запуск
# =========================
async def main():
    print("🚀 Бот запущен и готов к работе...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if os.getenv("RENDER", "false").lower() == "true":
        import uvicorn
        uvicorn.run("bot:app", host="0.0.0.0", port=8080)
    else:
        asyncio.run(main())
