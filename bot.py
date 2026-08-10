import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import uuid

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from openai import AsyncOpenAI

# Импортируем официальный SDK ЮKassa
from yookassa import Configuration, Payment

# -------------------------------------------------------------------
# НАСТРОЙКИ И КЛЮЧИ
# -------------------------------------------------------------------
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", 
    "8636610453:AAEvJuNb05_P5ALrXmebu58Q0I6zkN7-Fn4"
).strip()

GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY", 
    "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"
).strip()

YANDEX_CLOUD_FOLDER = os.environ.get(
    "YANDEX_CLOUD_FOLDER", 
    "b1gqkn7qf0sab32u6ghg"
).strip()

YANDEX_CLOUD_API_KEY = os.environ.get(
    "YANDEX_CLOUD_API_KEY", 
    "AQVNy2WbsDUNV210s00DiEHqXqoxstoRlgNo6ldQ"
).strip()

# НАСТРОЙКИ ЮKASSA
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "YOUR_SHOP_ID").strip()
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "YOUR_SECRET_KEY").strip()

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

if not BOT_TOKEN:
    logging.warning("BOT_TOKEN не задан!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)
GROQ_MODEL = "llama-3.3-70b-versatile"

USERS_DB: Dict[int, dict] = {}


class UserPreferences(StatesGroup):
    persons = State()
    dinners = State()
    vegetarian = State()
    calories = State()
    soup_salad = State()
    budget = State()


def main_keyboard(user_id: int):
    user_info = USERS_DB.get(user_id, {})
    is_full = user_info.get("is_full", False)

    kb = [
        [InlineKeyboardButton(text="Новая подборка 🥗", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]
    
    if not is_full:
        kb.insert(0, [InlineKeyboardButton(text="💳 Купить полный доступ (299 руб)", callback_data="buy_subscription")])
    else:
        kb.insert(0, [InlineKeyboardButton(text="✨ Полный доступ активен", callback_data="sub_active")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


# -------------------------------------------------------------------
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ЧЕРЕЗ YANDEX CLOUD API
# -------------------------------------------------------------------
async def generate_yandex_art_bytes(dish_name_ru: str) -> Optional[bytes]:
    if not YANDEX_CLOUD_API_KEY or not YANDEX_CLOUD_FOLDER:
        logging.error("YANDEX_CLOUD_API_KEY или YANDEX_CLOUD_FOLDER не заданы!")
        return None

    clean_name = re.sub(r'[^а-яА-Яa-zA-Z0-9\s]', '', dish_name_ru).strip()
    prompt = (
        f"Аппетитная фуд-фотография блюда: {clean_name}. "
        f"Красивая ресторанная сервировка, крупный план, аппетитные текстуры, профессиональный свет, 8k."
    )

    url = "https://ai.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
    headers = {
        "Authorization": f"Api-Key {YANDEX_CLOUD_API_KEY}",
        "x-folder-id": YANDEX_CLOUD_FOLDER,
        "Content-Type": "application/json"
    }
    
    payload = {
        "modelUri": f"art://{YANDEX_CLOUD_FOLDER}/yandexart/latest",
        "generationOptions": {
            "seed": int(asyncio.get_event_loop().time() * 1000) % 100000
        },
        "messages": [{"weight": 1, "text": prompt}]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                if response.status != 200:
                    return None
                res_json = await response.json()
                operation_id = res_json.get("id")

            if not operation_id:
                return None

            poll_url = f"https://llm.api.cloud.yandex.net/operations/{operation_id}"
            for _ in range(12):
                await asyncio.sleep(2)
                async with session.get(poll_url, headers=headers) as poll_resp:
                    if poll_resp.status == 200:
                        poll_json = await poll_resp.json()
                        if poll_json.get("done", False):
                            b64_data = poll_json.get("response", {}).get("image")
                            if b64_data:
                                return base64.b64decode(b64_data)
    except Exception as e:
        logging.error(f"Ошибка генерации YandexART: {e}")

    return None


# -------------------------------------------------------------------
# ЗАПРОСЫ К GROQ
# -------------------------------------------------------------------
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool, low_calories: bool, soup_salad: bool, budget: int) -> dict:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Разнообразные блюда."
    cal_status = "Строго до 600 ккал на порцию." if low_calories else "Калорийность обычная."
    soup_status = "Разрешать сытные супы/салаты." if soup_salad else "Супы/салаты не предлагать."
    budget_status = f"Бюджет: {budget} рублей." if budget > 0 else "Бюджет не ограничен."

    prompt = f"""
    Ты профессиональный шеф-повар. Сгенерируй {dinners} РАЗНЫХ ужинов для {persons} человек.
    Предпочтения: {veg_status} {cal_status} {soup_status} {budget_status}
    Требования:
    1. Подробные инструкции (6-10 шагов).
    2. НА КАЖДОМ ШАГЕ укажи точное время. Формат: "1. [ 5 мин] Текст..."
    3. Укажи `is_pantry` (boolean) и `estimated_price_rub` для ингредиентов.
    Верни строго JSON:
    {{
      "estimated_total_rub": 2000,
      "dishes": [
        {{
          "title": "Название",
          "cooking_time": "30 мин",
          "equipment": "Сковорода",
          "serving": "Подавать горячим",
          "instructions": ["1. [ 5 мин] Шаг..."],
          "ingredients": [{{"name": "Продукт", "amount": 200, "unit": "г", "category": "protein", "is_pantry": false, "estimated_price_rub": 150}}]
        }}
      ]
    }}
    """
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a professional chef. Output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return {"estimated_total_rub": 0, "dishes": []}


def format_dish_text(dish: dict, idx: int, persons: int) -> str:
    title = dish.get("title", "Блюдо")
    time_str = dish.get("cooking_time", "15 мин")
    equipment = dish.get("equipment", "Плита")
    serving = dish.get("serving", "По вкусу")

    ing_list = dish.get("ingredients", [])
    ing_str = "\n".join([f"• {i['name']} — {i['amount']} {i['unit']}" for i in ing_list])

    instructions = dish.get("instructions", [])
    formatted_instructions = []
    if isinstance(instructions, list):
        for i, step in enumerate(instructions, 1):
            clean_step = str(step).strip()
            if "⏱️" not in clean_step and "⏱" not in clean_step:
                clean_step = re.sub(r'\[\s*(\d+\s*мин)\s*]', r'⏱️ \1', clean_step)
            formatted_instructions.append(f"{i}. {clean_step}")
        inst_str = "\n".join(formatted_instructions)
    else:
        inst_str = str(instructions)

    return (
        f"🍳 **{title.upper()}**\n"
        f"⏱ Общее время: {time_str} | 👤 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"🛠 **Оборудование:** {equipment}\n\n"
        f"📖 **Инструкция:**\n{inst_str}\n\n"
        f"🥗 **Подача:** {serving}"
    )


# -------------------------------------------------------------------
# ОПЛАТА ЮKASSA
# -------------------------------------------------------------------
@dp.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(call: types.CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    
    try:
        idempotence_key = str(uuid.uuid4())
        payment = Payment.create({
            "amount": {"value": "299.00", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{(await bot.get_me()).username}"
            },
            "capture": True,
            "description": f"Покупка полного доступа (User ID: {user_id})",
            "metadata": {"user_id": user_id}
        }, idempotence_key)
        
        confirmation_url = payment.confirmation.confirmation_url
        payment_id = payment.id
        
        if user_id not in USERS_DB:
            USERS_DB[user_id] = {}
        USERS_DB[user_id]["last_payment_id"] = payment_id

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Оплатить 299 руб", url=confirmation_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_payment_{payment_id}")]
        ])
        
        await call.message.answer(
            "💳 **Счет на оплату создан!**\n\n"
            "Нажмите кнопку ниже для оплаты через ЮKassa. После оплаты нажмите **«Проверить оплату»**.",
            reply_markup=kb, parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка создания платежа: {e}")
        await call.message.answer("⚠️ Ошибка создания платежа. Проверьте настройки ЮKassa.")


@dp.callback_query(F.data.startswith("check_payment_"))
async def process_check_payment(call: types.CallbackQuery):
    payment_id = call.data.split("_")[2]
    user_id = call.from_user.id
    
    try:
        payment = Payment.find_one(payment_id)
        if payment.status == "succeeded":
            if user_id not in USERS_DB:
                USERS_DB[user_id] = {}
            USERS_DB[user_id]["is_full"] = True
            
            await call.message.edit_text(
                "🎉 **Оплата прошла успешно!** Вам предоставлен полный доступ.",
                parse_mode="Markdown",
                reply_markup=main_keyboard(user_id)
            )
        elif payment.status == "pending":
            await call.answer("⏳ Платеж еще не оплачен.", show_alert=True)
        else:
            await call.answer(f"❌ Статус: {payment.status}", show_alert=True)
    except Exception as e:
        logging.error(f"Ошибка проверки: {e}")
        await call.answer("⚠️ Не удалось проверить статус.", show_alert=True)


@dp.callback_query(F.data == "sub_active")
async def sub_active_alert(call: types.CallbackQuery):
    await call.answer("✨ У вас уже активирован полный доступ!", show_alert=True)


# -------------------------------------------------------------------
# КОМАНДЫ И НАСТРОЙКИ
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.update_data(persons=1, dinners=4, vegetarian=False, low_calories=False, soup_salad=True, budget=2500)
    await message.answer(
        "🤖 **Шеф-Повар Бот**\n\nЯ составляю меню с точным таймингом и бюджетом!",
        reply_markup=main_keyboard(user_id), parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("/start — Главное меню\n/menu — Текущий рацион", parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    if not dishes:
        await message.answer("У вас пока нет меню. Нажмите /start", reply_markup=main_keyboard(user_id))
        return
    text = "📋 **Ваше текущее меню:**\n\n"
    for idx, dish in enumerate(dishes, 1):
        text += f"**{idx}. {dish['title']}**\n"
    await message.answer(text, parse_mode="Markdown")


@dp.callback_query(F.data == "settings")
async def start_settings(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 чел", callback_data="p_1"), InlineKeyboardButton(text="2 чел", callback_data="p_2"), InlineKeyboardButton(text="3 чел", callback_data="p_3")],
        [InlineKeyboardButton(text="4 чел", callback_data="p_4"), InlineKeyboardButton(text="5 чел", callback_data="p_5"), InlineKeyboardButton(text="6 чел", callback_data="p_6")]
    ])
    await call.message.edit_text("👤 **Кол-во человек:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.persons)


@dp.callback_query(UserPreferences.persons, F.data.startswith("p_"))
async def process_persons(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(persons=int(call.data.split("_")[1]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"), InlineKeyboardButton(text="4 ужина", callback_data="d_4"), InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("🍽 **Кол-во ужинов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(dinners=int(call.data.split("_")[1]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="v_yes"), InlineKeyboardButton(text="Нет ❌", callback_data="v_no")]
    ])
    await call.message.edit_text("🥗 **Вы вегетарианец?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(vegetarian=(call.data == "v_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="c_yes"), InlineKeyboardButton(text="Без разницы ⚪", callback_data="c_any")]
    ])
    await call.message.edit_text("🔥 Менее калорийное меню (до 600 ккал)?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.calories)


@dp.callback_query(UserPreferences.calories, F.data.startswith("c_"))
async def process_calories(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(low_calories=(call.data == "c_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="s_yes"), InlineKeyboardButton(text="Нет ❌", callback_data="s_no")]
    ])
    await call.message.edit_text("🍲 Предлагать супы и салаты?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.soup_salad)


@dp.callback_query(UserPreferences.soup_salad, F.data.startswith("s_"))
async def process_soup_salad(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(soup_salad=(call.data == "s_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1500 руб", callback_data="b_1500"), InlineKeyboardButton(text="2500 руб", callback_data="b_2500"), InlineKeyboardButton(text="4000 руб", callback_data="b_4000")],
        [InlineKeyboardButton(text="Без ограничений ♾️", callback_data="b_0")]
    ])
    await call.message.edit_text("💰 **Бюджет на закупку продуктов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.budget)


@dp.callback_query(UserPreferences.budget, F.data.startswith("b_"))
async def process_budget_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(budget=int(call.data.split("_")[1]))
    user_id = call.from_user.id
    await call.message.edit_text("✅ **Настройки успешно сохранены!**", reply_markup=main_keyboard(user_id), parse_mode="Markdown")
    await state.clear()


@dp.callback_query(F.data == "new_selection")
async def generate_selection(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    await call.answer("Генерирую меню...")
    data = await state.get_data()
    
    persons = data.get("persons", 1)
    dinners_count = data.get("dinners", 4)
    vegetarian = data.get("vegetarian", False)
    low_calories = data.get("low_calories", False)
    soup_salad = data.get("soup_salad", True)
    budget = data.get("budget", 2500)

    await call.message.answer("⏳ **Составляю рецепты с таймингом и фото...**")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian, low_calories, soup_salad, budget)
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    if not dishes:
        await call.message.answer("Ошибка генерации. Попробуйте снова.", reply_markup=main_keyboard(user_id))
        return

    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)

    for idx, dish in enumerate(dishes):
        title = dish['title']
        img_bytes = await generate_yandex_art_bytes(title)
        caption = format_dish_text(dish, idx, persons)

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename=f"dish_{idx+1}.png")
            await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown")
        else:
            await call.message.answer(caption, parse_mode="Markdown")

    await call.message.answer(f"🎉 **Меню готово!** Примерная стоимость: **{total_rub} руб.**", reply_markup=main_keyboard(user_id), parse_mode="Markdown")


# -------------------------------------------------------------------
# ЗАПУСК
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
