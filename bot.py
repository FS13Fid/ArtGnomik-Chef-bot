import asyncio
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
POLZA_API_KEY = os.environ.get("POLZA_API_KEY", "pza_CAHvoksXc1MMKJI7j6ooRDOfaeG4sjv-").strip()

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

# Клиент Polza AI для генерации изображений
polza_client = AsyncOpenAI(
    base_url="https://api.polza.ai/v1",
    api_key=POLZA_API_KEY
) if POLZA_API_KEY else None


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


# Генерация изображения через Polza.ai
async def generate_image_bytes(dish_name_en: str) -> Optional[bytes]:
    if not polza_client:
        logging.error("POLZA_API_KEY не установлен!")
        return None

    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', dish_name_en).strip()
    prompt = (
        f"A professional top-view food photograph of {clean_name}. "
        f"Cozy aesthetic home dinner setting, warm lighting, appetizing presentation, highly detailed, 8k quality."
    )

    try:
        response = await polza_client.images.generate(
            model="dall-e-3",  # или актуальное имя модели из панели Polza.ai
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )
        image_url = response.data[0].url

        # Скачиваем сгенерированное изображение по URL
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logging.error(f"Polza AI Error: {e}")

    return None


# Запрос к Groq с категоризацией ингредиентов и расчетом цен
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool) -> dict:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Любые блюда (мясо, птица, рыба)."

    prompt = f"""
    Ты шеф-повар. Сгенерируй меню из {dinners} уникальных ужинов для {persons} человек.
    Предпочтения: {veg_status}

    Верни ответ строго в формате JSON:
    {{
      "estimated_total_rub": 1850,
      "dishes": [
        {{
          "title": "Уникальное название блюда на русском",
          "title_en": "English search term for food image generation",
          "cooking_time": "25 мин",
          "recipe": "Краткое пошаговое описание приготовления",
          "ingredients": [
            {{
              "name": "Название продукта",
              "amount": 150,
              "unit": "г",
              "category": "protein", 
              "is_pantry": false,
              "estimated_price_rub": 180
            }}
          ]
        }}
      ]
    }}

    Правила оценки стоимости:
    - estimated_price_rub: примерная стоимость указанного количества продукта в российских рублях.
    - estimated_total_rub: примерная итоговая сумма всей корзины покупок (без учета базовых продуктов из "is_pantry").

    Категории ингредиентов ("category"):
    - "protein": Белок (мясо, рыба, птица, фарш)
    - "garnish": Гарнир/крупы (макароны, рис, картофель)
    - "vegetables": Овощи и зелень (грибы, томаты, лук, зелень)
    - "dairy": Молочные продукты и сыры (сливки, сыр, творог)
    - "bakery": Хлеб/лаваш
    - "other": Прочее

    Значение "is_pantry":
    - true: если это базовая приправа/масло/мука/соль, которая обычно есть дома.
    - false: если это покупной продукт для рецепта.
    """

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a culinary assistant that output JSON only."},
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
                    "title": "Спагетти Болоньезе",
                    "title_en": "spaghetti bolognese",
                    "cooking_time": "25 мин",
                    "recipe": "Обжарьте фарш с томатами и подавайте со спагетти.",
                    "ingredients": [
                        {"name": "Мясной фарш", "amount": 150, "unit": "г", "category": "protein", "is_pantry": False, "estimated_price_rub": 180},
                        {"name": "Спагетти", "amount": 100, "unit": "г", "category": "garnish", "is_pantry": False, "estimated_price_rub": 60},
                        {"name": "Оливковое масло", "amount": 1, "unit": "ст.л.", "category": "other", "is_pantry": True, "estimated_price_rub": 0}
                    ]
                }
            ]
        }


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=4, vegetarian=False)
    welcome_text = (
        "Привет! Я **Art Gnomik Chef** 🧙‍♂️🍝\n\n"
        "Я формирую идеальное меню на неделю с генерацией фотографий блюд через Polza AI, удобным списком покупок и расчётом стоимости!"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())


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

    await call.message.answer("🤖 Составляю подборку ужинов, генерируем фото через Polza AI...")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian)
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    await state.update_data(current_dishes=dishes, total_rub=total_rub)

    summary_text = "**Обновили список блюд** 🍿\n\n"

    for idx, dish in enumerate(dishes, 1):
        dish_en = dish.get("title_en", "meal")
        time_str = dish.get("cooking_time", "25 мин")
        title = dish['title']

        summary_text += f"**{idx}** {title} ({time_str})\n"

        img_bytes = await generate_image_bytes(dish_en)
        caption = f"**День {idx}: {title}**\n⏱ {time_str} | 👤 На {persons} перс.\n\n📖 **Рецепт:**\n{dish['recipe']}"

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename=f"dish_{idx}.jpg")
            await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown")
        else:
            await call.message.answer(caption, parse_mode="Markdown")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Список продуктов 🛒", callback_data="get_shopping_list"),
            InlineKeyboardButton(text="Заменить блюдо 🍝", callback_data="new_selection")
        ]
    ])

    await call.message.answer(summary_text, parse_mode="Markdown", reply_markup=kb)


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

    res += f"💳 **Примерная стоимость корзины покупок:**\n"
    res += f"~ {min_price} – {max_price} ₽ *(оценка на основе средних цен супермаркетов)*"

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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
