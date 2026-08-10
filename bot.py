import asyncio
import base64
import json
import logging
import os
import re
import uuid
from typing import Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiohttp import web
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

# НАСТРОЙКИ ЮKASSA (замените на свои или добавьте в Environment Variables на Render)
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "YOUR_SHOP_ID").strip()
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "YOUR_SECRET_KEY").strip()

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

if not BOT_TOKEN:
    logging.warning("BOT_TOKEN не задан!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# База данных статусов пользователей в памяти
USERS_DB: Dict[int, dict] = {}

# Клиент Groq для генерации текстового меню
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)
GROQ_MODEL = "llama-3.3-70b-versatile"


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
        [InlineKeyboardButton(text="Новая подборка ✨", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]

    if not is_full:
        kb.insert(0, [InlineKeyboardButton(text="💳 Купить подписку (299 руб)", callback_data="buy_subscription")])
    else:
        kb.insert(0, [InlineKeyboardButton(text="✨ Полный доступ активен", callback_data="sub_active")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


# -------------------------------------------------------------------
# ОПЛАТА ЧЕРЕЗ ЮKASSA
# -------------------------------------------------------------------
@dp.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(call: types.CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    
    try:
        idempotence_key = str(uuid.uuid4())
        payment = Payment.create({
            "amount": {
                "value": "299.00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{(await bot.get_me()).username}"
            },
            "capture": True,
            "description": f"Покупка полного доступа к боту (User ID: {user_id})",
            "metadata": {
                "user_id": user_id
            }
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
            "Нажмите кнопку ниже для перехода на страницу оплаты ЮKassa. После успешной оплаты нажмите **«Проверить оплату»**.",
            reply_markup=kb, parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Ошибка создания платежа ЮKassa: {e}")
        await call.message.answer("⚠️ Ошибка при создании платежа. Проверьте настройки ЮKassa.")


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
                "🎉 **Оплата прошла успешно!**\n\n"
                "Вам предоставлен **полный доступ** к боту. Приятного использования!",
                parse_mode="Markdown",
                reply_markup=main_keyboard(user_id)
            )
        elif payment.status == "pending":
            await call.answer("⏳ Платеж еще не оплачен. Завершите оплату в браузере.", show_alert=True)
        else:
            await call.answer(f"❌ Статус платежа: {payment.status}", show_alert=True)
            
    except Exception as e:
        logging.error(f"Ошибка проверки платежа: {e}")
        await call.answer("⚠️ Не удалось проверить статус платежа.", show_alert=True)


@dp.callback_query(F.data == "sub_active")
async def sub_active_alert(call: types.CallbackQuery):
    await call.answer("✨ У вас уже активирован полный доступ!", show_alert=True)


# -------------------------------------------------------------------
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ЧЕРЕЗ YANDEX CLOUD API
# -------------------------------------------------------------------
async def generate_yandex_art_bytes(dish_name_ru: str) -> Optional[bytes]:
    if not YANDEX_CLOUD_API_KEY or not YANDEX_CLOUD_FOLDER:
        logging.error("❌ YANDEX_CLOUD_API_KEY или YANDEX_CLOUD_FOLDER не заданы!")
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
        "messages": [
            {
                "weight": 1,
                "text": prompt
            }
        ]
    }

    try:
        logging.info(f"🎨 Отправка запроса на генерацию фото для '{clean_name}'...")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                if response.status != 200:
                    err_body = await response.text()
                    logging.error(f"❌ YandexART start error {response.status}: {err_body}")
                    return None
                
                res_json = await response.json()
                operation_id = res_json.get("id")

            if not operation_id:
                logging.error("❌ Не получен operation_id от YandexART")
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
                                logging.info(f"✅ Картинка для '{clean_name}' успешно получена!")
                                return base64.b64decode(b64_data)
                            else:
                                logging.error("❌ Поле image отсутствует в ответе YandexART")
                                return None

    except Exception as e:
        logging.error(f"❌ Ошибка генерации YandexART: {e}", exc_info=True)

    return None


# -------------------------------------------------------------------
# ЗАПРОСЫ К GROQ
# -------------------------------------------------------------------
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool, low_calories: bool, soup_salad: bool, budget: int) -> dict:
    veg_status = "Только вегетарианские блюда (без мяса, птицы, рыбы)!" if vegetarian else "Разнообразные блюда (мяса, птицы, рыбы, овощей)."
    cal_status = "Каждое блюдо должно быть диетическим и менее калорийным (строго до 600 ккал на порцию)." if low_calories else "Калорийность блюд обычная."
    soup_status = "Разрешается предлагать сытные супы и салаты как основные блюда на ужин." if soup_salad else "Супы и салаты как основное блюдо не предлагать."
    budget_status = f"Общая оценочная стоимость всех покупаемых продуктов должна укладываться примерно в бюджет: {budget} рублей." if budget > 0 else "Бюджет не ограничен."

    prompt = f"""
    Ты профессиональный шеф-повар. Сгенерируй {dinners} РАЗНЫХ и УНИКАЛЬНЫХ ужинов для {persons} человек.
    Предпочтения: {veg_status} {cal_status} {soup_status} {budget_status}

    КРИТИЧЕСКИ ВАЖНЫЕ ТРЕБОВАНИЯ К ИНСТРУКЦИИ:
    1. Каждое блюдо должно содержать подробную пошаговую инструкцию (6-10 шагов).
    2. НА КАЖДОМ ШАГЕ ОБЯЗАТЕЛЬНО УКАЗЫВАЙ ТОЧНОЕ ВРЕМЯ (например: "1. [⏱ 5 мин] Нарезка...").
    3. Ингредиенты рассчитывай строго на {persons} чел.
    4. ДЛЯ КАЖДОГО ИНГРЕДИЕНТА укажи поле `is_pantry` (boolean): базовые специи/масла — true, остальное — false.
    5. Для каждого ингредиента укажи `estimated_price_rub`.
    6. Для поля `category` используй: protein, garnish, vegetables, greens, dairy, nuts, bakery, spices, oil, other.

    Верни ответ СТРОГО в формате JSON:
    {{
      "estimated_total_rub": 2000,
      "dishes": [
        {{
          "title": "Название блюда",
          "cooking_time": "30 мин",
          "equipment": "Сковорода, кастрюля",
          "serving": "Подача",
          "instructions": ["1. [⏱ 5 мин] Шаг..."],
          "ingredients": [
            {{"name": "Продукт", "amount": 200, "unit": "г", "category": "protein", "is_pantry": false, "estimated_price_rub": 150}}
          ]
        }}
      ]
    }}
    """

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a professional chef. Always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return {"estimated_total_rub": 0, "dishes": []}


async def replace_ingredient_in_dish(dish: dict, old_ingredient: str) -> dict:
    prompt = f"""
    Блюдо: "{dish['title']}". Замени ингредиент "{old_ingredient}" на подходящий аналог.
    Обнови список ингредиентов и инструкцию (с таймингом "1. [⏱ 5 мин] ...").
    Верни ответ строго в формате JSON с полями: title, cooking_time, equipment, serving, instructions, ingredients.
    """
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a professional chef assistant returning valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Error replacing ingredient: {e}")
        return dish


async def generate_dish_replacement_options(persons: int, vegetarian: bool, old_dish_title: str) -> list:
    veg_status = "Вегетарианское" if vegetarian else "Любое"
    prompt = f"""
    Предложи 3 альтернативных блюда взамен "{old_dish_title}" для {persons} чел. ({veg_status}).
    Каждый шаг инструкции должен содержать тайминг.
    Верни ответ строго в формате JSON:
    {{
      "options": [
        {{
          "title": "Название",
          "cooking_time": "25 мин",
          "equipment": "Плита",
          "serving": "Подача",
          "instructions": ["1. [⏱ 5 мин] Шаг..."],
          "ingredients": [
            {{"name": "Ингредиент", "amount": 100, "unit": "г", "category": "protein", "is_pantry": false, "estimated_price_rub": 100}}
          ]
        }}
      ]
    }}
    """
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a professional chef returning valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("options", [])
    except Exception as e:
        logging.error(f"Error generating options: {e}")
        return []


def format_dish_text(dish: dict, idx: int, persons: int) -> str:
    title = dish.get("title", "Блюдо")
    time_str = dish.get("cooking_time", "15 мин")
    equipment = dish.get("equipment", "Плита")
    serving = dish.get("serving", "По вкусу")

    ing_list = dish.get("ingredients", [])
    ing_str = "\n".join([f"• {i['name']} — {i['amount']} {i['unit']}" for i in ing_list])

    instructions = dish.get("instructions", [])
    inst_str = "\n".join(instructions) if isinstance(instructions, list) else str(instructions)

    return (
        f"🍳 **{title.upper()}**\n"
        f"⏱ Общее время: {time_str} | 👤 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"🛠 **Оборудование:** {equipment}\n\n"
        f"📖 **Пошаговая инструкция:**\n{inst_str}\n\n"
        f"🥗 **Подача:** {serving}"
    )


# -------------------------------------------------------------------
# ХЕНДЛЕРЫ БОТА
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.update_data(persons=1, dinners=4, vegetarian=False, low_calories=False, soup_salad=True, budget=2500)
    welcome_text = (
        "🤖 **Шеф-Повар Бот** 👨‍🍳🍝\n\n"
        "Я составляю меню с **точным таймингом каждого шага**, делю ингредиенты на покупки, учитываю бюджет и интегрирован с ЮKassa!"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard(user_id))


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 **Помощь**\n\n"
        "/start — Главное меню\n"
        "/menu — Посмотреть текущий рацион\n"
        "/help — Справка"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    user_id = message.from_user.id

    if not dishes:
        await message.answer("У вас пока нет сохраненного меню. Нажмите /start!", reply_markup=main_keyboard(user_id))
        return

    text = "📋 **Ваше текущее меню:**\n\n"
    for idx, dish in enumerate(dishes, 1):
        text += f"**{idx}. {dish['title']}** ({dish.get('cooking_time', '15 мин')})\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="Сгенерировать новое меню 🔄", callback_data="new_selection")]
    ])

    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


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
    persons = int(call.data.split("_")[1])
    await state.update_data(persons=persons)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"), InlineKeyboardButton(text="4 ужина", callback_data="d_4"), InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("🍽 **Кол-во ужинов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    dinners = int(call.data.split("_")[1])
    await state.update_data(dinners=dinners)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🌱", callback_data="v_yes"), InlineKeyboardButton(text="Нет 🥩", callback_data="v_no")]
    ])
    await call.message.edit_text("🥗 **Вы вегетарианец?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    is_veg = (call.data == "v_yes")
    await state.update_data(vegetarian=is_veg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🥗", callback_data="c_yes"), InlineKeyboardButton(text="Без разницы 🍝", callback_data="c_any")]
    ])
    await call.message.edit_text("Сделать меню менее калорийным (до 600 ккал на порцию)?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.calories)


@dp.callback_query(UserPreferences.calories, F.data.startswith("c_"))
async def process_calories(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    low_cal = (call.data == "c_yes")
    await state.update_data(low_calories=low_cal)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🍲", callback_data="s_yes"), InlineKeyboardButton(text="Нет 🍽", callback_data="s_no")]
    ])
    await call.message.edit_text("Предлагать ли вам на ужин сытные салаты и супы как основное блюдо?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.soup_salad)


@dp.callback_query(UserPreferences.soup_salad, F.data.startswith("s_"))
async def process_soup_salad(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    soups = (call.data == "s_yes")
    await state.update_data(soup_salad=soups)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1500 руб", callback_data="b_1500"), InlineKeyboardButton(text="2500 руб", callback_data="b_2500"), InlineKeyboardButton(text="4000 руб", callback_data="b_4000")],
        [InlineKeyboardButton(text="6000 руб", callback_data="b_6000"), InlineKeyboardButton(text="Без ограничений ♾️", callback_data="b_0")]
    ])
    await call.message.edit_text("💰 **Какую сумму вы хотите примерно потратить на закупку продуктов?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.budget)


@dp.callback_query(UserPreferences.budget, F.data.startswith("b_"))
async def process_budget_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    budget = int(call.data.split("_")[1])
    await state.update_data(budget=budget)
    await finish_settings(call.message, state)


@dp.message(UserPreferences.budget, F.text)
async def process_budget_text(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ Пожалуйста, введите сумму цифрами (например: `3000`), либо нажмите кнопку выше.", parse_mode="Markdown")
        return
    budget = int(text)
    await state.update_data(budget=budget)
    await finish_settings(message, state)


async def finish_settings(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = message.chat.id if isinstance(message, types.Message) else message.from_user.id
    info_text = "Учли ваши предпочтения ✏️❤️\nГотовы подобрать для вас блюда 🍝"
    
    if isinstance(message, types.CallbackQuery):
        await message.message.edit_text(info_text, reply_markup=main_keyboard(user_id), parse_mode="Markdown")
    else:
        await message.answer(info_text, reply_markup=main_keyboard(user_id), parse_mode="Markdown")


@dp.callback_query(F.data == "new_selection")
async def generate_selection(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Генерирую меню...")
    data = await state.get_data()
    persons = data.get("persons", 1)
    dinners_count = data.get("dinners", 4)
    vegetarian = data.get("vegetarian", False)
    low_calories = data.get("low_calories", False)
    soup_salad = data.get("soup_salad", True)
    budget = data.get("budget", 2500)

    await call.message.answer("👨‍🍳 **Составляю подробные рецепты с таймингом шагов, ценами и фото в пределах бюджета...**")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian, low_calories, soup_salad, budget)
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    if not dishes:
        await call.message.answer("Произошла ошибка при генерации. Попробуйте еще раз!", reply_markup=main_keyboard(call.from_user.id))
        return

    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)

    summary_text = f"🎉 **Ваше меню готово!**\n💰 Примерная стоимость закупки: **{total_rub} руб.**\n\n"

    for idx, dish in enumerate(dishes):
        title = dish['title']
        summary_text += f"**{idx+1}.** {title} ({dish.get('cooking_time', '20 мин')})\n"

        img_bytes = await generate_yandex_art_bytes(title)
        caption = format_dish_text(dish, idx, persons)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить продукт 🔄", callback_data=f"replace_ing_select_{idx}"),
                InlineKeyboardButton(text="Заменить блюдо 🍝", callback_data=f"replace_dish_options_{idx}")
            ]
        ])

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename=f"dish_{idx+1}.png")
            await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown", reply_markup=kb)
        else:
            await call.message.answer(caption, parse_mode="Markdown", reply_markup=kb)

    kb_final = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list"),
            InlineKeyboardButton(text="Пересоздать меню 🔄", callback_data="new_selection")
        ]
    ])

    await call.message.answer(summary_text, parse_mode="Markdown", reply_markup=kb_final)


@dp.callback_query(F.data.startswith("replace_ing_select_"))
async def select_ingredient_to_replace(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    if not dishes or dish_idx >= len(dishes):
        await call.message.answer("⚠️ Меню устарело.", reply_markup=main_keyboard(call.from_user.id))
        return

    dish = dishes[dish_idx]
    buttons = [[InlineKeyboardButton(text=f"❌ {ing['name']}", callback_data=f"do_replace_{dish_idx}_{ing_idx}")] for ing_idx, ing in enumerate(dish.get("ingredients", []))]
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_replace")])

    await call.message.reply(f"Какой продукт из блюда **«{dish['title']}»** заменяем?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "cancel_replace")
async def cancel_replace(call: types.CallbackQuery):
    await call.answer()
    await call.message.delete()


@dp.callback_query(F.data.startswith("do_replace_"))
async def execute_ingredient_replacement(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Заменяю ингредиент...")
    parts = call.data.split("_")
    dish_idx, ing_idx = int(parts[2]), int(parts[3])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    persons = data.get("persons", 1)

    dish = dishes[dish_idx]
    target_ing = dish["ingredients"][ing_idx]["name"]

    await call.message.edit_text(f"⏳ Корректирую рецепт без **{target_ing}**...", parse_mode="Markdown")
    updated_dish = await replace_ingredient_in_dish(dish, target_ing)
    dishes[dish_idx] = updated_dish
    
    total_rub = sum(sum(i.get("estimated_price_rub", 0) for i in d.get("ingredients", []) if not i.get("is_pantry", False)) for d in dishes)
    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)

    res_text = f"✅ **Рецепт обновлен!**\n\n" + format_dish_text(updated_dish, dish_idx, persons)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")]
    ])
    await call.message.edit_text(res_text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("replace_dish_options_"))
async def offer_dish_replacements(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    old_dish = dishes[dish_idx]

    options = await generate_dish_replacement_options(data.get("persons", 1), data.get("vegetarian", False), old_dish['title'])
    if not options:
        await call.message.answer("Не удалось сгенерировать варианты.")
        return

    await state.update_data(temp_replacement_options=options, target_dish_idx=dish_idx)
    buttons = [[InlineKeyboardButton(text=f"✨ {opt['title']}", callback_data=f"apply_dish_swap_{opt_idx}")] for opt_idx, opt in enumerate(options)]
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_replace")])

    await call.message.reply(f"Выберите новое блюдо взамен **«{old_dish['title']}»**:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("apply_dish_swap_"))
async def apply_dish_swap(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Применяю замену...")
    opt_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dish_idx, dishes, options = data.get("target_dish_idx"), data.get("current_dishes", []), data.get("temp_replacement_options", [])

    chosen_dish = options[opt_idx]
    dishes[dish_idx] = chosen_dish
    total_rub = sum(sum(i.get("estimated_price_rub", 0) for i in d.get("ingredients", []) if not i.get("is_pantry", False)) for d in dishes)
    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)

    img_bytes = await generate_yandex_art_bytes(chosen_dish['title'])
    caption = f"🎉 **Блюдо заменено!**\n\n" + format_dish_text(chosen_dish, dish_idx, data.get("persons", 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")]])

    if img_bytes:
        await call.message.answer_photo(photo=BufferedInputFile(img_bytes, filename=f"dish_{dish_idx+1}.png"), caption=caption, parse_mode="Markdown", reply_markup=kb)
        await call.message.delete()
    else:
        await call.message.edit_text(caption, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "get_shopping_list")
async def shopping_list(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    if not dishes:
        await call.message.answer("⚠️ Сначала сгенерируйте подборку!", reply_markup=main_keyboard(call.from_user.id))
        return

    shop_categories = {"protein": {"title": "🥩 Белок:", "items": {}}, "garnish": {"title": "🍚 Гарнир:", "items": {}}, "vegetables": {"title": "🥦 Овощи:", "items": {}}, "other": {"title": "📦 Прочее:", "items": {}}}
    for dish in dishes:
        for ing in dish.get("ingredients", []):
            name = ing["name"].capitalize()
            cat = ing.get("category", "other")
            target = shop_categories.get(cat, shop_categories["other"])["items"]
            target[name] = {"amount": ing.get("amount", 0), "unit": ing.get("unit", "")}

    res = "🛒 **Список покупок:**\n\n"
    for cat_data in shop_categories.values():
        if cat_data["items"]:
            res += f"{cat_data['title']}\n"
            for name, info in cat_data['items'].items():
                res += f"• {name} — {info['amount']} {info['unit']}\n"
            res += "\n"

    await call.message.answer(res, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В главное меню 🏠", callback_data="back_main")]]))


@dp.callback_query(F.data == "back_main")
async def back_to_main(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer("Главное меню:", reply_markup=main_keyboard(call.from_user.id))


async def handle_ping(request):
    return web.Response(text="Bot is active")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
