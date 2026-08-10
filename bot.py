import asyncio
import base64
import json
import logging
import os
import re
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


def main_keyboard():
    kb = [
        [InlineKeyboardButton(text="Новая подборка ✨", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


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
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool, low_calories: bool, soup_salad: bool) -> dict:
    veg_status = "Только вегетарианские блюда (без мяса, птицы, рыбы)!" if vegetarian else "Разнообразные блюда (мясо, птица, рыба, овощи)."
    cal_status = "Каждое блюдо должно быть диетическим и менее калорийным (строго до 600 ккал на порцию)." if low_calories else "Калорийность блюд обычная."
    soup_status = "Разрешается предлагать сытные супы и салаты как основные блюда на ужин." if soup_salad else "Супы и салаты как основное блюдо не предлагать."

    prompt = f"""
    Ты профессиональный шеф-повар. Сгенерируй {dinners} РАЗНЫХ и УНИКАЛЬНЫХ ужинов для {persons} человек.
    Предпочтения: {veg_status} {cal_status} {soup_status}

    КРИТИЧЕСКИ ВАЖНЫЕ ТРЕБОВАНИЯ К ИНСТРУКЦИИ:
    1. Каждое блюдо должно содержать подробную пошаговую инструкцию (6-10 шагов).
    2. НА КАЖДОМ ШАГЕ ОБЯЗАТЕЛЬНО УКАЗЫВАЙ ТОЧНОЕ ВРЕМЯ (сколько минут резать, сколько минут варить, обжаривать, запекать, тушить и т.д.).
       Формат каждого шага должен быть строго с таймингом, например:
       "1. [⏱ 5 мин] Нарезка: нашинкуйте лук кубиками, а морковь натрите на крупной терке."
       "2. [⏱ 7 мин] Обжарка: разогрейте сковороду и обжаривайте лук с морковью на среднем огне до золотистого цвета."
       "3. [⏱ 15 мин] Варка: добавьте бульон, доведите до кипения и варите на медленном огне."
    3. Ингредиенты рассчитывай строго на {persons} чел.

    Верни ответ СТРОГО в формате JSON:
    {{
      "estimated_total_rub": 2000,
      "dishes": [
        {{
          "title": "Название блюда",
          "cooking_time": "30 мин",
          "equipment": "Сковорода, кастрюля, разделочная доска, нож",
          "serving": "Украсьте свежей зеленью и подавайте с долькой лимона",
          "instructions": [
            "1. [⏱ 5 мин] Подготовка и нарезка: ...",
            "2. [⏱ 8 мин] Обжарка: ...",
            "3. [⏱ 15 мин] Варка/Тушение: ..."
          ],
          "ingredients": [
            {{
              "name": "Название продукта",
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
                {"role": "system", "content": "You are a professional chef. Always output valid JSON with exact time for each cooking step."},
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
    Блюдо: "{dish['title']}".
    Замени ингредиент "{old_ingredient}" на подходящий аналог.
    Обнови подробно список ингредиентов и пошаговую инструкцию.
    
    ТРЕБОВАНИЕ: В пошаговой инструкции на КАЖДОМ шаге указывай точное время (сколько минут резать, варить, жарить и т.д.) в формате:
    "1. [⏱ 5 мин] Текст шага..."

    Верни ответ строго в формате JSON с полями: title, cooking_time, equipment, serving, instructions (массив строк), ingredients (массив объектов).
    """

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a professional chef assistant returning valid JSON with step-by-step timing."},
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
    Предложи 3 альтернативных подробных блюда взамен "{old_dish_title}" для {persons} чел. ({veg_status}).
    В пошаговой инструкции КАЖДЫЙ шаг обязан содержать точное время (сколько резать, жарить, варить).
    
    Верни ответ строго в формате JSON:
    {{
      "options": [
        {{
          "title": "Название блюда",
          "cooking_time": "25 мин",
          "equipment": "Плита, сковорода, доска",
          "serving": "Зелень, соус",
          "instructions": ["1. [⏱ 5 мин] Нарезка...", "2. [⏱ 10 мин] Обжарка..."],
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
                {"role": "system", "content": "You are a professional chef returning valid JSON with step timing."},
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
    if isinstance(instructions, list):
        inst_str = "\n".join(instructions)
    else:
        inst_str = str(instructions)

    text = (
        f"🍳 **{title.upper()}**\n"
        f"⏱ Общее время: {time_str} | 👤 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"🛠 **Оборудование:** {equipment}\n\n"
        f"📖 **Пошаговая инструкция:**\n{inst_str}\n\n"
        f"🥗 **Подача:** {serving}"
    )
    return text


# -------------------------------------------------------------------
# ХЕНДЛЕРЫ BOT AIOGRAM
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=4, vegetarian=False, low_calories=False, soup_salad=True)
    welcome_text = (
        "🤖 **Шеф-Повар Бот** 👨‍🍳🍝\n\n"
        "Я составляю меню с **точным таймингом каждого шага** (сколько резать, жарить, варить) и генерирую фото блюд!"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 **Помощь**\n\n"
        "/start — Главное меню\n"
        "/menu — Посмотреть текущий рацион\n"
        "/help — Справка\n\n"
        "Нажмите **«Новая подборка»**, чтобы сгенерировать новое меню с точным временем готовки!"
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
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="Сгенерировать новое меню 🔄", callback_data="new_selection")]
    ])

    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "settings")
async def start_settings(call: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 чел", callback_data="p_1"), InlineKeyboardButton(text="2 чел", callback_data="p_2"), InlineKeyboardButton(text="4 чел", callback_data="p_4")]
    ])
    await call.message.edit_text("👤 **Кол-во человек:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.persons)


@dp.callback_query(UserPreferences.persons, F.data.startswith("p_"))
async def process_persons(call: types.CallbackQuery, state: FSMContext):
    persons = int(call.data.split("_")[1])
    await state.update_data(persons=persons)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"), InlineKeyboardButton(text="4 ужина", callback_data="d_4"), InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("🍽 **Кол-во ужинов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    dinners = int(call.data.split("_")[1])
    await state.update_data(dinners=dinners)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🌱", callback_data="v_yes"), InlineKeyboardButton(text="Нет 🥩", callback_data="v_no")]
    ])
    await call.message.edit_text("🥗 **Вы вегетарианец?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    is_veg = (call.data == "v_yes")
    await state.update_data(vegetarian=is_veg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🥗", callback_data="c_yes"), InlineKeyboardButton(text="Без разницы 🍝", callback_data="c_any")]
    ])
    await call.message.edit_text("Сделать меню менее калорийным (до 600 ккал на порцию)?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.calories)


@dp.callback_query(UserPreferences.calories, F.data.startswith("c_"))
async def process_calories(call: types.CallbackQuery, state: FSMContext):
    low_cal = (call.data == "c_yes")
    await state.update_data(low_calories=low_cal)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🍲", callback_data="s_yes"), InlineKeyboardButton(text="Нет 🍽", callback_data="s_no")]
    ])
    await call.message.edit_text("Предлагать ли вам на ужин сытные салаты и супы как основное блюдо?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.soup_salad)


@dp.callback_query(UserPreferences.soup_salad, F.data.startswith("s_"))
async def process_soup_salad(call: types.CallbackQuery, state: FSMContext):
    soups = (call.data == "s_yes")
    await state.update_data(soup_salad=soups)
    
    user_data = await state.get_data()
    veg_status = "не предлагать" if user_data.get("vegetarian") else "предлагать"
    cal_status = "не предлагать" if not user_data.get("low_calories") else "до 600 ккал"
    soup_status = "предлагать" if user_data.get("soup_salad") else "не предлагать"

    info_text = (
        f"Учли ваши предпочтения ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons')}\n"
        f"• Кол-во ужинов: {user_data.get('dinners')}\n"
        f"• Вегетарианские блюда: {veg_status}\n"
        f"• Менее калорийные блюда: {cal_status}\n"
        f"• Супы/салаты: {soup_status}\n\n"
        f"Готовы подобрать для вас блюда 🍝"
    )
    await call.message.edit_text(info_text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.callback_query(F.data == "new_selection")
async def generate_selection(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    persons = data.get("persons", 1)
    dinners_count = data.get("dinners", 4)
    vegetarian = data.get("vegetarian", False)
    low_calories = data.get("low_calories", False)
    soup_salad = data.get("soup_salad", True)

    await call.message.answer("👨‍🍳 **Составляю подробные рецепты с таймингом шагов и генерирую фото...**")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian, low_calories, soup_salad)
    dishes = ai_data.get("dishes", [])

    if not dishes:
        await call.message.answer("Произошла ошибка при генерации. Попробуйте еще раз!", reply_markup=main_keyboard())
        return

    await state.update_data(current_dishes=dishes)

    summary_text = "🎉 **Ваше меню готово!**\n\n"

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


# -------------------------------------------------------------------
# ЗАМЕНА ИНГРЕДИЕНТОВ И БЛЮД
# -------------------------------------------------------------------
@dp.callback_query(F.data.startswith("replace_ing_select_"))
async def select_ingredient_to_replace(call: types.CallbackQuery, state: FSMContext):
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    if dish_idx >= len(dishes):
        await call.answer("Блюдо не найдено")
        return

    dish = dishes[dish_idx]
    ingredients = dish.get("ingredients", [])

    buttons = []
    for ing_idx, ing in enumerate(ingredients):
        buttons.append([InlineKeyboardButton(
            text=f"❌ {ing['name']}",
            callback_data=f"do_replace_{dish_idx}_{ing_idx}"
        )])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_replace")])

    await call.message.reply(
        f"Какой продукт из блюда **«{dish['title']}»** заменяем?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data == "cancel_replace")
async def cancel_replace(call: types.CallbackQuery):
    await call.message.delete()


@dp.callback_query(F.data.startswith("do_replace_"))
async def execute_ingredient_replacement(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    dish_idx = int(parts[2])
    ing_idx = int(parts[3])

    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    persons = data.get("persons", 1)

    if dish_idx >= len(dishes):
        await call.answer("Ошибка поиска блюда")
        return

    dish = dishes[dish_idx]
    target_ing = dish["ingredients"][ing_idx]["name"]

    await call.message.edit_text(f"⏳ Корректирую рецепт без **{target_ing}**...", parse_mode="Markdown")

    updated_dish = await replace_ingredient_in_dish(dish, target_ing)

    dishes[dish_idx] = updated_dish
    await state.update_data(current_dishes=dishes)

    res_text = f"✅ **Рецепт обновлен!**\n\n" + format_dish_text(updated_dish, dish_idx, persons)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заменить еще продукт 🔄", callback_data=f"replace_ing_select_{dish_idx}")],
        [InlineKeyboardButton(text="Заменить блюдо 🍝", callback_data=f"replace_dish_options_{dish_idx}")],
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")]
    ])

    await call.message.edit_text(res_text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("replace_dish_options_"))
async def offer_dish_replacements(call: types.CallbackQuery, state: FSMContext):
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    persons = data.get("persons", 1)
    vegetarian = data.get("vegetarian", False)

    if dish_idx >= len(dishes):
        await call.answer("Блюдо не найдено")
        return

    old_dish = dishes[dish_idx]

    await call.message.reply(
        f"⏳ Подбираю 3 варианта на замену **«{old_dish['title']}»**...",
        parse_mode="Markdown"
    )

    options = await generate_dish_replacement_options(persons, vegetarian, old_dish['title'])

    if not options:
        await call.message.answer("Не удалось сгенерировать варианты. Попробуйте еще раз!")
        return

    await state.update_data(temp_replacement_options=options, target_dish_idx=dish_idx)

    buttons = []
    for opt_idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(
            text=f"✨ {opt['title']} ({opt.get('cooking_time', '20 мин')})",
            callback_data=f"apply_dish_swap_{opt_idx}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_replace")])

    await call.message.answer(
        f"Выберите новое блюдо взамен **«{old_dish['title']}»**:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("apply_dish_swap_"))
async def apply_dish_swap(call: types.CallbackQuery, state: FSMContext):
    opt_idx = int(call.data.split("_")[-1])
    data = await state.get_data()

    dish_idx = data.get("target_dish_idx")
    dishes = data.get("current_dishes", [])
    options = data.get("temp_replacement_options", [])
    persons = data.get("persons", 1)

    if dish_idx is None or opt_idx >= len(options) or dish_idx >= len(dishes):
        await call.answer("Ошибка при замене блюда")
        return

    chosen_dish = options[opt_idx]
    
    dishes[dish_idx] = chosen_dish
    await state.update_data(current_dishes=dishes)

    await call.message.edit_text(f"🎨 Генерирую фото для **«{chosen_dish['title']}»** через YandexART...", parse_mode="Markdown")

    img_bytes = await generate_yandex_art_bytes(chosen_dish['title'])
    caption = f"🎉 **Блюдо заменено!**\n\n" + format_dish_text(chosen_dish, dish_idx, persons)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Заменить продукт 🔄", callback_data=f"replace_ing_select_{dish_idx}"),
            InlineKeyboardButton(text="Заменить блюдо 🍝", callback_data=f"replace_dish_options_{dish_idx}")
        ],
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")]
    ])

    if img_bytes:
        photo_file = BufferedInputFile(img_bytes, filename=f"dish_{dish_idx+1}.png")
        await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown", reply_markup=kb)
        await call.message.delete()
    else:
        await call.message.edit_text(caption, parse_mode="Markdown", reply_markup=kb)


# -------------------------------------------------------------------
# СПИСОК ПОКУПОК
# -------------------------------------------------------------------
@dp.callback_query(F.data == "get_shopping_list")
async def shopping_list(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    persons = data.get("persons", 1)
    dishes = data.get("current_dishes", [])

    if not dishes:
        await call.answer("Сначала сгенерируйте подборку!")
        return

    categories = {
        "protein": {"title": "🥩 Белок / Основные продукты", "items": {}},
        "garnish": {"title": "🍚 Гарниры и крупы", "items": {}},
        "vegetables": {"title": "🥦 Овощи и зелень", "items": {}},
        "dairy": {"title": "🥛 Молочные продукты", "items": {}},
        "bakery": {"title": "🥖 Хлеб и выпечка", "items": {}},
        "other": {"title": "📦 Прочее", "items": {}}
    }

    pantry_items: Dict[str, Dict] = {}

    for dish in dishes:
        for ing in dish.get("ingredients", []):
            name = ing["name"].capitalize()
            amount = ing.get("amount", 0)
            unit = ing.get("unit", "")
            cat = ing.get("category", "other")
            is_pantry = ing.get("is_pantry", False)

            target_dict = pantry_items if is_pantry else categories.get(cat, categories["other"])["items"]

            if name in target_dict:
                if isinstance(amount, (int, float)):
                    target_dict[name]["amount"] += amount
            else:
                target_dict[name] = {"amount": amount, "unit": unit}

    res = f"🛒 **Список покупок ({len(dishes)} бл., {persons} чел.)**\n\n"

    for cat_key, cat_data in categories.items():
        if cat_data["items"]:
            res += f"{cat_data['title']}:\n"
            for name, info in cat_data["items"].items():
                amt = info["amount"]
                amt_str = f"{amt:.1f}".rstrip('0').rstrip('.') if isinstance(amt, float) else str(amt)
                res += f"• {name} — {amt_str} {info['unit']}\n"
            res += "\n"

    if pantry_items:
        res += "🏠 **Обычно есть дома:**\n"
        for name, info in pantry_items.items():
            amt = info["amount"]
            amt_str = f"{amt:.1f}".rstrip('0').rstrip('.') if isinstance(amt, float) else str(amt)
            res += f"• {name} — {amt_str} {info['unit']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В главное меню 🏠", callback_data="back_main")]
    ])

    await call.message.answer(res, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "back_main")
async def back_to_main(call: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    veg_status = "не предлагать" if user_data.get("vegetarian") else "предлагать"
    cal_status = "не предлагать" if not user_data.get("low_calories") else "до 600 ккал"
    soup_status = "предлагать" if user_data.get("soup_salad") else "не предлагать"

    info_text = (
        f"Учли ваши предпочтения ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons', 1)}\n"
        f"• Кол-во ужинов: {user_data.get('dinners', 4)}\n"
        f"• Вегетарианские блюда: {veg_status}\n"
        f"• Менее калорийные блюда: {cal_status}\n"
        f"• Супы/салаты: {soup_status}\n\n"
        f"Готовы подобрать для вас блюда 🍝"
    )
    await call.message.answer(info_text, reply_markup=main_keyboard(), parse_mode="Markdown")


async def handle_ping(request):
    return web.Response(text="Bot is active")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
