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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8636610453:AAEvJuNb05_P5ALrXmebu58Q0I6zkN7-Fn4").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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").strip()

YANDEX_CLOUD_FOLDER = os.environ.get("YANDEX_CLOUD_FOLDER", "b1gqkn7qf0sab32u6ghg").strip()
YANDEX_CLOUD_API_KEY = os.environ.get("YANDEX_CLOUD_API_KEY", "AQVNy2WbsDUNV210s00DiEHqXqoxstoRlgNo6ldQ").strip()

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
        f"Профессиональная аппетитная фуд-фотография блюда {clean_name}. "
        f"Уютная домашняя сервировка, теплое мягкое освещение, вид сверху, высокая детализация, 8k."
    )

    url = "https://ai.api.cloud.yandex.net/v1/images/generations"
    headers = {
        "Authorization": f"Api-Key {YANDEX_CLOUD_API_KEY}",
        "x-folder-id": YANDEX_CLOUD_FOLDER,
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": f"art://{YANDEX_CLOUD_FOLDER}/yandexart/latest",
        "prompt": prompt,
        "size": "1024x1024"
    }

    try:
        logging.info(f"🎨 Запрос к Yandex Cloud API для блюда: '{clean_name}'...")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=60) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logging.error(f"❌ Yandex Cloud API вернул ошибку HTTP {response.status}: {error_text}")
                    return None

                data = await response.json()
                b64_data = None
                
                if "data" in data and len(data["data"]) > 0:
                    b64_data = data["data"][0].get("b64_json")

                if b64_data:
                    logging.info(f"✅ Картинка для '{clean_name}' успешно сгенерирована!")
                    return base64.b64decode(b64_data)
                else:
                    logging.error(f"❌ Ответ Yandex Cloud не содержит b64_json: {data}")

    except asyncio.TimeoutError:
        logging.error("⏳ Таймаут ожидания ответа Yandex Cloud API (60s)")
    except Exception as e:
        logging.error(f"❌ Ошибка Yandex Cloud API: {e}", exc_info=True)

    return None


# -------------------------------------------------------------------
# ЗАПРОСЫ К GROQ (ПОДРОБНЫЕ РЕЦЕПТЫ + ZERO WASTE)
# -------------------------------------------------------------------
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool) -> dict:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Любые блюда (мясо, птица, рыба, морепродукты)."

    prompt = f"""
    Ты шеф-повар и эксперт по минимизации пищевых отходов (Food Waste Optimizer). 
    Сгенерируй меню из {dinners} уникальных ужинов для {persons} человек.
    Предпочтения: {veg_status}

    ГЛАВНЫЕ ПРАВИЛА:
    1. Ингредиенты оптимизированы без остатков (скоропортящиеся продукты, такие как зелень, творожный сыр, лаваш, сливки, томаты, задействуются в 2-3 блюдах).
    2. Каждый рецепт ДОЛЖЕН БЫТЬ ПОДРОБНЫМ и структурированным по шагам! Включай список оборудования и варианты подачи.

    Верни ответ строго в формате JSON:
    {{
      "estimated_total_rub": 1850,
      "dishes": [
        {{
          "title": "Сливочный ролл с сёмгой",
          "cooking_time": "10 мин",
          "equipment": "Плита/Доска",
          "serving": "Кунжут, Соевый соус",
          "instructions": [
            "1. Разверните лист лаваша на столе.",
            "2. Равномерно смажьте поверхность тонким слоем творожного сыра.",
            "3. Разложите листья салата фриллис.",
            "4. Тонко нарежьте сёмгу и равномерно распределите по всей поверхности.",
            "5. По желанию посыпьте кунжутом.",
            "6. Сверните лаваш в плотный рулет.",
            "7. По желанию уберите в холодильник на 30 минут, чтобы рулет уплотнился.",
            "8. Нарежьте на кусочки и подавайте."
          ],
          "ingredients": [
            {{
              "name": "Семга слабосоленая",
              "amount": {100 * persons},
              "unit": "г",
              "category": "protein",
              "is_pantry": false,
              "estimated_price_rub": 350
            }},
            {{
              "name": "Творожный сыр",
              "amount": {60 * persons},
              "unit": "г",
              "category": "dairy",
              "is_pantry": false,
              "estimated_price_rub": 120
            }},
            {{
              "name": "Армянский лаваш",
              "amount": {0.5 * persons},
              "unit": "лист",
              "category": "bakery",
              "is_pantry": false,
              "estimated_price_rub": 50
            }},
            {{
              "name": "Салат фриллис",
              "amount": 1,
              "unit": "пучок",
              "category": "vegetables",
              "is_pantry": false,
              "estimated_price_rub": 80
            }},
            {{
              "name": "Кунжут",
              "amount": 1,
              "unit": "ч.л.",
              "category": "other",
              "is_pantry": true,
              "estimated_price_rub": 0
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
                {"role": "system", "content": "You are a professional chef outputting detailed recipes in strict JSON format."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return {
            "estimated_total_rub": 1200,
            "dishes": [
                {
                    "title": "Сливочный ролл с сёмгой",
                    "cooking_time": "10 мин",
                    "equipment": "Разделочная доска, нож",
                    "serving": "Кунжут, Соевый соус",
                    "instructions": [
                        "1. Разверните лист лаваша на столе.",
                        "2. Равномерно смажьте поверхность тонким слоем творожного сыра (не толстым, просто чтобы покрыть лаваш).",
                        "3. Разложите листья салата.",
                        "4. Тонко нарежьте сёмгу и равномерно распределите по всей поверхности.",
                        "5. По желанию посыпьте кунжутом.",
                        "6. Сверните лаваш в плотный рулет.",
                        "7. По желанию уберите в холодильник на 30 минут, чтобы рулет уплотнился (можно есть и сразу).",
                        "8. Нарежьте на кусочки и подавайте."
                    ],
                    "ingredients": [
                        {"name": "Семга слабосоленая", "amount": 100 * persons, "unit": "г", "category": "protein", "is_pantry": False, "estimated_price_rub": 350},
                        {"name": "Творожный сыр", "amount": 60 * persons, "unit": "г", "category": "dairy", "is_pantry": False, "estimated_price_rub": 120},
                        {"name": "Армянский лаваш", "amount": 0.5 * persons, "unit": "лист", "category": "bakery", "is_pantry": False, "estimated_price_rub": 50},
                        {"name": "Салат фриллис", "amount": 1, "unit": "пучок", "category": "vegetables", "is_pantry": False, "estimated_price_rub": 80},
                        {"name": "Кунжут", "amount": 1, "unit": "ч.л.", "category": "other", "is_pantry": True, "estimated_price_rub": 0}
                    ]
                }
            ]
        }


async def replace_ingredient_in_dish(dish: dict, old_ingredient: str) -> dict:
    prompt = f"""
    У нас есть блюдо: "{dish['title']}".
    Текущая инструкция: {json.dumps(dish.get('instructions', []), ensure_ascii=False)}
    Текущие ингредиенты: {json.dumps(dish['ingredients'], ensure_ascii=False)}

    Задача: Замени ингредиент "{old_ingredient}" на адекватный аналог.
    Обнови детальную инструкцию приготовления и список ингредиентов.

    Верни ответ строго в формате JSON:
    {{
      "title": "Название блюда (можно изменить под замену)",
      "cooking_time": "{dish.get('cooking_time', '15 мин')}",
      "equipment": "{dish.get('equipment', 'Плита')}",
      "serving": "{dish.get('serving', 'Свежая зелень')}",
      "replacement_note": "Заменили {old_ingredient} на <новый продукт>",
      "instructions": [
        "1. Подробный шаг 1...",
        "2. Подробный шаг 2..."
      ],
      "ingredients": [
        {{
          "name": "Название продукта",
          "amount": 150,
          "unit": "г",
          "category": "protein/garnish/vegetables/dairy/bakery/other",
          "is_pantry": false,
          "estimated_price_rub": 180
        }}
      ]
    }}
    """

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are an expert chef assistant returning JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Error replacing ingredient: {e}")
        return dish


async def generate_dish_replacement_options(persons: int, vegetarian: bool, old_dish_title: str) -> list:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Любые блюда (мясо, птица, рыба, морепродукты)."

    prompt = f"""
    Пользователь хочет заменить блюдо "{old_dish_title}".
    Предложи 3 альтернативных подробных варианта ужина на {persons} чел.
    Требования: {veg_status}

    Верни ответ строго в формате JSON:
    {{
      "options": [
        {{
          "title": "Название альтернативного блюда",
          "cooking_time": "20 мин",
          "equipment": "Плита/Сковорода",
          "serving": "Зелень, соус",
          "instructions": [
            "1. Подробный шаг 1...",
            "2. Подробный шаг 2..."
          ],
          "ingredients": [
            {{
              "name": "Название продукта",
              "amount": 200,
              "unit": "г",
              "category": "protein",
              "is_pantry": false,
              "estimated_price_rub": 200
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
                {"role": "system", "content": "You are a chef providing detailed alternative meal options in JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("options", [])
    except Exception as e:
        logging.error(f"Error generating dish options: {e}")
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
        f"⏱ {time_str} | 👤 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"🛠 **Оборудование:** {equipment}\n\n"
        f"📖 **Инструкция:**\n{inst_str}\n\n"
        f"🥗 **Подача:** {serving}"
    )
    return text


# -------------------------------------------------------------------
# ХЕНДЛЕРЫ BOT AIOGRAM
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=4, vegetarian=False)
    welcome_text = (
        "🤖 **Art Gnomik Chef & Food Waste Optimizer** 🧙‍♂️🍝\n\n"
        "Я генерирую подробные пошаговые рецепты на неделю без остатков продуктов и создаю фото блюд через YandexART!"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 **Food Waste Optimizer — Помощь**\n\n"
        "Я помогу составить оптимальное меню на неделю с подробными рецептами и фото.\n\n"
        "**Команды:**\n"
        "/start — Начать подбор меню\n"
        "/menu — Показать текущее сохраненное меню\n"
        "/help — Эта справка\n\n"
        "**Как это работает:**\n"
        "1. Нажмите «Новая подборка» или «Настроить фильтрацию»\n"
        "2. Укажите количество человек и ужинов\n"
        "3. Получите подробные рецепты (с оборудованием, развернутыми шагами и вариантами подачи)!"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    if not dishes:
        await message.answer("У вас пока нет сохраненного меню. Нажмите /start!", reply_markup=main_keyboard())
        return

    text = "📋 **Ваше текущее меню на неделю:**\n\n"
    for idx, dish in enumerate(dishes, 1):
        text += f"**{idx}. {dish['title']}** ({dish.get('cooking_time', '15 мин')})\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть список покупок 🛒", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="Сгенерировать новое меню 🔄", callback_data="new_selection")]
    ])

    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "settings")
async def start_settings(call: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 чел", callback_data="p_1"), InlineKeyboardButton(text="2 чел", callback_data="p_2"), InlineKeyboardButton(text="4 чел", callback_data="p_4")]
    ])
    await call.message.edit_text("Укажите количество человек:", reply_markup=kb)
    await state.set_state(UserPreferences.persons)


@dp.callback_query(UserPreferences.persons, F.data.startswith("p_"))
async def process_persons(call: types.CallbackQuery, state: FSMContext):
    persons = int(call.data.split("_")[1])
    await state.update_data(persons=persons)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"), InlineKeyboardButton(text="4 ужина", callback_data="d_4"), InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("Сколько ужинов планируем?", reply_markup=kb)
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    dinners = int(call.data.split("_")[1])
    await state.update_data(dinners=dinners)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🌱", callback_data="v_yes"), InlineKeyboardButton(text="Нет 🥩", callback_data="v_no")]
    ])
    await call.message.edit_text("Только вегетарианское меню?", reply_markup=kb)
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    is_veg = (call.data == "v_yes")
    await state.update_data(vegetarian=is_veg)
    user_data = await state.get_data()
    veg_status = "да 🌱" if user_data.get("vegetarian") else "нет 🥩"

    info_text = (
        f"Настройки сохранены! ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons')}\n"
        f"• Кол-во ужинов: {user_data.get('dinners')}\n"
        f"• Вегетарианские блюда: {veg_status}"
    )
    await call.message.edit_text(info_text, reply_markup=main_keyboard())


@dp.callback_query(F.data == "new_selection")
async def generate_selection(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    persons = data.get("persons", 1)
    dinners_count = data.get("dinners", 4)
    vegetarian = data.get("vegetarian", False)

    await call.message.answer("♻️ **Генерирую подробные рецепты и фото через YandexART...**")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian)
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    await state.update_data(current_dishes=dishes, total_rub=total_rub)

    summary_text = "**Подборка ужинов готова!** 🍿\n\n"

    for idx, dish in enumerate(dishes):
        title = dish['title']
        summary_text += f"**{idx+1}.** {title} ({dish.get('cooking_time', '15 мин')})\n"

        img_bytes = await generate_yandex_art_bytes(title)
        caption = format_dish_text(dish, idx, persons)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить продукт 🔄", callback_data=f"replace_ing_select_{idx}"),
                InlineKeyboardButton(text="Заменить это блюдо 🍝", callback_data=f"replace_dish_options_{idx}")
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
            InlineKeyboardButton(text="Заменить ВСЮ подборку 🔄", callback_data="new_selection")
        ]
    ])

    await call.message.answer(summary_text, parse_mode="Markdown", reply_markup=kb_final)


# -------------------------------------------------------------------
# ВЫБОР И ЗАМЕНА ИНГРЕДИЕНТОВ
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
        f"Какой продукт из блюда **«{dish['title']}»** вы хотите заменить?",
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

    await call.message.edit_text(f"⏳ Пересчитываю детальный рецепт для **{target_ing}** в блюде «{dish['title']}»...", parse_mode="Markdown")

    updated_dish = await replace_ingredient_in_dish(dish, target_ing)

    dishes[dish_idx] = updated_dish
    await state.update_data(current_dishes=dishes)

    res_text = f"✅ **Замена выполнена!**\n\n" + format_dish_text(updated_dish, dish_idx, persons)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заменить еще продукт 🔄", callback_data=f"replace_ing_select_{dish_idx}")],
        [InlineKeyboardButton(text="Заменить само блюдо 🍝", callback_data=f"replace_dish_options_{dish_idx}")],
        [InlineKeyboardButton(text="Обновленный список покупок 🛒", callback_data="get_shopping_list")]
    ])

    await call.message.edit_text(res_text, parse_mode="Markdown", reply_markup=kb)


# -------------------------------------------------------------------
# ВЫБОР И ЗАМЕНА ОДНОГО БЛЮДА НА ВЫБОР (3 ВАРИАНТА)
# -------------------------------------------------------------------
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
        f"⏳ Подбираю 3 альтернативных варианта на замену **«{old_dish['title']}»**...",
        parse_mode="Markdown"
    )

    options = await generate_dish_replacement_options(persons, vegetarian, old_dish['title'])

    if not options:
        await call.message.answer("Не удалось сгенерировать замену. Попробуйте еще раз!")
        return

    await state.update_data(temp_replacement_options=options, target_dish_idx=dish_idx)

    buttons = []
    for opt_idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(
            text=f"✨ {opt['title']} ({opt.get('cooking_time', '15 мин')})",
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
    caption = f"🎉 **Блюдо успешно заменено!**\n\n" + format_dish_text(chosen_dish, dish_idx, persons)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Заменить продукт 🔄", callback_data=f"replace_ing_select_{dish_idx}"),
            InlineKeyboardButton(text="Заменить это блюдо 🍝", callback_data=f"replace_dish_options_{dish_idx}")
        ],
        [InlineKeyboardButton(text="Обновленный список покупок 🛒", callback_data="get_shopping_list")]
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
        "protein": {"title": "🥩 Белок", "items": {}},
        "garnish": {"title": "🍚 Гарнир", "items": {}},
        "vegetables": {"title": "🥦 Овощи и зелень", "items": {}},
        "dairy": {"title": "🥛 Молочка", "items": {}},
        "bakery": {"title": "🥖 Хлеб", "items": {}},
        "other": {"title": "📦 Прочее", "items": {}}
    }

    pantry_items: Dict[str, Dict] = {}
    calculated_total = 0

    for dish in dishes:
        for ing in dish.get("ingredients", []):
            name = ing["name"].capitalize()
            amount = ing.get("amount", 0)
            unit = ing.get("unit", "")
            cat = ing.get("category", "other")
            is_pantry = ing.get("is_pantry", False)
            price = ing.get("estimated_price_rub", 0)

            target_dict = pantry_items if is_pantry else categories.get(cat, categories["other"])["items"]

            if not is_pantry:
                calculated_total += price

            if name in target_dict:
                if isinstance(amount, (int, float)):
                    target_dict[name]["amount"] += amount
                target_dict[name]["price"] += price
            else:
                target_dict[name] = {"amount": amount, "unit": unit, "price": price}

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
        res += "🏠 **Скорее всего есть у вас дома:**\n\n"
        for name, info in pantry_items.items():
            amt = info["amount"]
            amt_str = f"{amt:.1f}".rstrip('0').rstrip('.') if isinstance(amt, float) else str(amt)
            res += f"• {name} — {amt_str} {info['unit']}\n"
        res += "\n"

    min_price = int(calculated_total * 0.9) if calculated_total > 0 else 1200
    max_price = int(calculated_total * 1.15) if calculated_total > 0 else 1600

    res += f"💳 **Примерная стоимость корзины:**\n"
    res += f"~ {min_price} – {max_price} ₽ *(оценка на основе цен супермаркетов)*\n\n"
    res += "💡 *Меню оптимизировано для минимизации остатков!*"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В главное меню 🏠", callback_data="back_main")]
    ])

    await call.message.answer(res, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "back_main")
async def back_to_main(call: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    veg_status = "да 🌱" if user_data.get("vegetarian") else "нет 🥩"

    info_text = (
        f"Текущие параметры ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons', 1)}\n"
        f"• Кол-во ужинов: {user_data.get('dinners', 4)}\n"
        f"• Вегетарианские блюда: {veg_status}"
    )
    await call.message.answer(info_text, reply_markup=main_keyboard())


async def handle_ping(request):
    return web.Response(text="Art Gnomik Chef Service OK")


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
