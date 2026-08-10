import asyncio
import json
import logging
import os
import re
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiohttp import web
from google import genai
from google.genai import types as genai_types
from openai import AsyncOpenAI

# -------------------------------------------------------------------
# НАСТРОЙКИ И КЛЮЧИ (Из Environment Variables на Render)
# -------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Клиент Groq для генерации текста и рецептов
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)
GROQ_MODEL = "llama-3.3-70b-versatile"

# Клиент Google GenAI (Imagen 3 / Gemini)
google_client = genai.Client(api_key=GEMINI_API_KEY)


# FSM Состояния
class UserPreferences(StatesGroup):
    persons = State()
    dinners = State()
    vegetarian = State()


def main_keyboard():
    kb = [
        [InlineKeyboardButton(text="Новая подборка ✨ (Groq + Google AI)", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# Генерация изображения блюда через Google AI (Imagen 3)
async def generate_google_image_bytes(dish_name_en: str) -> Optional[bytes]:
    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', dish_name_en).strip()
    prompt = (
        f"A professional photo of a delicious food dish: {clean_name}. "
        f"Cozy aesthetic home dinner setting, warm lighting, top view, appetizing food photography, highly detailed."
    )
    
    try:
        # Вызов генерации изображения Imagen 3 через SDK Google GenAI
        result = await asyncio.to_thread(
            google_client.models.generate_images,
            model='imagen-3.0-generate-002',
            prompt=prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="4:3",
                output_mime_type="image/jpeg",
            )
        )
        
        if result.generated_images:
            return result.generated_images[0].image.image_bytes
    except Exception as e:
        logging.error(f"Google Imagen API Error: {e}")
        
    return None


# Быстрая генерация меню через Groq
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool) -> dict:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Любые блюда (мясо, птица, рыба)."

    prompt = f"""
    Ты шеф-повар. Сгенерируй случайное меню из {dinners} уникальных ужинов для {persons} человек.
    Предпочтения: {veg_status}

    Верни ответ строго в формате JSON, соблюдая следующую структуру:
    {{
      "dishes": [
        {{
          "title": "Уникальное название блюда на русском с эмодзи",
          "title_en": "Very short clear English search term/description for image generation (e.g., 'creamy salmon pasta', 'baked potato with cheese', 'spaghetti bolognese')",
          "cooking_time": "30 мин",
          "recipe": "Краткое пошаговое описание приготовления",
          "ingredients": [
            {{"name": "Название продукта", "amount": 150, "unit": "г"}}
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
                {"role": "system", "content": "You are a helpful culinary assistant that responds only in JSON format."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )

        content = response.choices[0].message.content
        return json.loads(content)

    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return {
            "dishes": [{
                "title": "Быстрые спагетти с сыром 🧀",
                "title_en": "spaghetti with cheese",
                "cooking_time": "15 мин",
                "recipe": "Отварите пасту до состояния al dente и посыпьте тертым сыром.",
                "ingredients": [{"name": "Спагетти", "amount": 100, "unit": "г"}, {"name": "Сыр", "amount": 50, "unit": "г"}]
            }]
        }


# -------------------------------------------------------------------
# ХЭНДЛЕРЫ BOT AIOgram
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=3, vegetarian=False)

    welcome_text = (
        "Привет! Я **Art Gnomik Chef** 🧙‍♂️🍝\n\n"
        "Я составляю меню с помощью **Groq AI** и генерирую красивые фотографии блюд через **Google AI (Imagen 3)**!"
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
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"), InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("Сколько ужинов планируем на неделю?", reply_markup=kb)
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
    dinners_count = data.get("dinners", 3)
    vegetarian = data.get("vegetarian", False)

    await call.message.answer("🤖 Составляю меню и генерирую сочные фото через Google AI...")

    ai_data = await generate_groq_menu(persons, dinners_count, vegetarian)
    dishes = ai_data.get("dishes", [])
    
    await state.update_data(current_dishes=dishes)

    for idx, dish in enumerate(dishes, 1):
        dish_en = dish.get("title_en", "delicious meal")
        cooking_time = dish.get("cooking_time", "30 мин")
        
        # Генерация изображения через Google Imagen 3
        img_bytes = await generate_google_image_bytes(dish_en)
        
        caption = (
            f"**День {idx}: {dish['title']}**\n"
            f"⏱ Время приготовления: {cooking_time} | 👤 На {persons} перс.\n\n"
            f"📖 **Рецепт:**\n{dish['recipe']}"
        )
        
        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename=f"dish_{idx}.jpg")
            await call.message.answer_photo(photo=photo_file, caption=caption, parse_mode="Markdown")
        else:
            await call.message.answer(caption, parse_mode="Markdown")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Сформировать список продуктов", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="🔄 Сгенерировать новые рецепты", callback_data="new_selection")]
    ])

    await call.message.answer("✨ Меню сформировано!", reply_markup=kb)


@dp.callback_query(F.data == "get_shopping_list")
async def shopping_list(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    persons = data.get("persons", 1)
    dishes = data.get("current_dishes", [])

    if not dishes:
        await call.answer("Сначала сгенерируйте подборку!")
        return

    shopping_cart: Dict[str, Dict] = {}

    for dish in dishes:
        for ing in dish.get("ingredients", []):
            name = ing["name"].capitalize()
            amount = ing.get("amount", 0)
            unit = ing.get("unit", "шт")

            if name in shopping_cart:
                shopping_cart[name]["amount"] += amount
            else:
                shopping_cart[name] = {"amount": amount, "unit": unit}

    result_text = f"🛒 **Список продуктов в магазин ({persons} чел.):**\n\n"
    for ing_name, info in shopping_cart.items():
        amt = info["amount"]
        amt_str = f"{amt:.1f}".rstrip('0').rstrip('.') if isinstance(amt, float) else str(amt)
        result_text += f"• {ing_name}: **{amt_str} {info['unit']}**\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В главное меню 🏠", callback_data="back_main")]
    ])

    await call.message.answer(result_text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "back_main")
async def back_to_main(call: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    veg_status = "да 🌱" if user_data.get("vegetarian") else "нет 🥩"

    info_text = (
        f"Текущие параметры ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons', 1)}\n"
        f"• Кол-во ужинов: {user_data.get('dinners', 3)}\n"
        f"• Вегетарианские блюда: {veg_status}"
    )

    await call.message.answer(info_text, reply_markup=main_keyboard())


# Web server для поддержки вебхуков / пингов Render
async def handle_ping(request):
    return web.Response(text="Art Gnomik is running with Google AI!")


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
