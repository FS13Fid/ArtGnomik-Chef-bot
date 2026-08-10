import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
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

if not BOT_TOKEN:
    logging.warning("BOT_TOKEN не задан!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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


def main_keyboard():
    kb = [
        [InlineKeyboardButton(text="Новая подборка 🥗", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]
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
        "messages": [
            {
                "weight": 1,
                "text": prompt
            }
        ]
    }

    try:
        logging.info(f"Отправка запроса на генерацию фото для '{clean_name}'...")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                if response.status != 200:
                    err_body = await response.text()
                    logging.error(f"YandexART start error {response.status}: {err_body}")
                    return None
                
                res_json = await response.json()
                operation_id = res_json.get("id")

            if not operation_id:
                logging.error("Не получен operation_id от YandexART")
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
                                logging.info(f"Картинка для '{clean_name}' успешно получена!")
                                return base64.b64decode(b64_data)
                            else:
                                logging.error("Поле image отсутствует в ответе YandexART")
                                return None

    except Exception as e:
        logging.error(f"Ошибка генерации YandexART: {e}", exc_info=True)

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
    2. НА КАЖДОМ ШАГЕ ОБЯЗАТЕЛЬНО УКАЗЫВАЙ ТОЧНОЕ ВРЕМЯ. Формат: "1. [ 5 мин] Текст..."
    3. Ингредиенты рассчитывай строго на {persons} чел.
    4. ДЛЯ КАЖДОГО ИНГРЕДИЕНТА укажи поле `is_pantry` (boolean): базовые специи/масла — true, остальное — false.
    5. Укажи примерную стоимость (`estimated_price_rub`) для каждого ингредиента.
    
    Верни ответ СТРОГО в формате JSON:
    {{
      "estimated_total_rub": 2000,
      "dishes": [
        {{
          "title": "Название блюда",
          "cooking_time": "30 мин",
          "equipment": "Сковорода, кастрюля",
          "serving": "Подавать горячим",
          "instructions": [
            "1. [ 5 мин] Подготовка: ...",
            "2. [ 8 мин] Обжарка: ..."
          ],
          "ingredients": [
            {{
              "name": "Продукт",
              "amount": 200,
              "unit": "г",
              "category": "protein",
              "is_pantry": false,
              "estimated_price_rub": 150
            }}
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
    Блюдо: "{dish['title']}". Замени ингредиент "{old_ingredient}" на аналог.
    Обнови список ингредиентов и пошаговую инструкцию с таймингом на каждом шаге.
    Верни JSON с полями: title, cooking_time, equipment, serving, instructions, ingredients.
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
    Каждый шаг должен содержать время. Верни JSON: {{"options": [...]}}
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
    formatted_instructions = []
    
    if isinstance(instructions, list):
        for i, step in enumerate(instructions, 1):
            clean_step = str(step).strip()
            for prefix in [f"{i}.", f"{i}."]:
                if clean_step.startswith(prefix):
                    clean_step = clean_step[len(prefix):].strip()
            if "⏱️" not in clean_step and "⏱" not in clean_step:
                clean_step = re.sub(r'\[\s*(\d+\s*мин)\s*\]', r'⏱️ \1', clean_step)
            formatted_instructions.append(f"{i}. {clean_step}")
        inst_str = "\n".join(formatted_instructions)
    else:
        inst_str = str(instructions)

    return (
        f"🍳 **{title.upper()}**\n"
        f"⏱ Общее время: {time_str} | 👤 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"🛠 **Оборудование:** {equipment}\n\n"
        f"📖 **Пошаговая инструкция:**\n{inst_str}\n\n"
        f"🥗 **Подача:** {serving}"
    )


# -------------------------------------------------------------------
# ХЕНДЛЕРЫ BOT AIOGRAM
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=4, vegetarian=False, low_calories=False, soup_salad=True, budget=2500)
    welcome_text = (
        "🤖 **Шеф-Повар Бот**\n\n"
        "Я составляю меню с **точным таймингом каждого шага**, делю ингредиенты на покупки и учитываю ваш бюджет!"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "ℹ️ **Помощь**\n\n"
        "/start — Главное меню\n"
        "/menu — Посмотреть текущий рацион\n"
        "/help — Справка\n\n"
        "Нажмите **«Новая подборка»**, чтобы сгенерировать новое меню!"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    if not dishes:
        await message.answer("У вас пока нет сохраненного меню. Нажмите /start!", reply_markup=main_keyboard())
        return

    text = "📋 **Ваше текущее меню:**\n\n"
    for idx, dish in enumerate(dishes, 1):
        text += f"**{idx}. {dish['title']}** ({dish.get('cooking_time', '15 мин')})\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
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
        [InlineKeyboardButton(text="Да ✅", callback_data="v_yes"), InlineKeyboardButton(text="Нет ❌", callback_data="v_no")]
    ])
    await call.message.edit_text("🥗 **Вы вегетарианец?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    is_veg = (call.data == "v_yes")
    await state.update_data(vegetarian=is_veg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="c_yes"), InlineKeyboardButton(text="Без разницы ⚪", callback_data="c_any")]
    ])
    await call.message.edit_text("🔥 Сделать меню менее калорийным (до 600 ккал на порцию)?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.calories)


@dp.callback_query(UserPreferences.calories, F.data.startswith("c_"))
async def process_calories(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    low_cal = (call.data == "c_yes")
    await state.update_data(low_calories=low_cal)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="s_yes"), InlineKeyboardButton(text="Нет ❌", callback_data="s_no")]
    ])
    await call.message.edit_text("🍲 Предлагать ли вам на ужин сытные салаты и супы как основное блюдо?", reply_markup=kb, parse_mode="Markdown")
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


async def finish_settings(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    info_text = (
        f"✅ **Предпочтения сохранены!**\n"
        f"• Кол-во человек: {user_data.get('persons')}\n"
        f"• Кол-во ужинов: {user_data.get('dinners')}\n"
    )
    if isinstance(message, types.CallbackQuery):
        await message.message.edit_text(info_text, reply_markup=main_keyboard(), parse_mode="Markdown")
    else:
        await message.answer(info_text, reply_markup=main_keyboard(), parse_mode="Markdown")


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

    await call.message.answer("⏳ **Составляю подробные рецепты с таймингом и фото...**")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian, low_calories, soup_salad, budget)
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    if not dishes:
        await call.message.answer("Произошла ошибка при генерации. Попробуйте еще раз!", reply_markup=main_keyboard())
        return

    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)

    summary_text = f"🎉 **Ваше меню готово!**\n Примерная стоимость закупки: **{total_rub} руб.**\n\n"

    for idx, dish in enumerate(dishes):
        title = dish['title']
        summary_text += f"**{idx+1}.** {title} ({dish.get('cooking_time', '20 мин')})\n"

        img_bytes = await generate_yandex_art_bytes(title)
        caption = format_dish_text(dish, idx, persons)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить продукт 🔄", callback_data=f"replace_ing_select_{idx}"),
                InlineKeyboardButton(text="Заменить блюдо 🔀", callback_data=f"replace_dish_options_{idx}")
            ]
        ])

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename=f"dish_{idx+1}.png")
            await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown", reply_markup=kb)
        else:
            await call.message.answer(caption, parse_mode="Markdown", reply_markup=kb)

    await call.message.answer(summary_text, parse_mode="Markdown", reply_markup=main_keyboard())


# -------------------------------------------------------------------
# ЗАПУСК БОТА
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
