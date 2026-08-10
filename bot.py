import asyncio
import logging
import os
import random
from typing import Dict, List

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

# -------------------------------------------------------------------
# ВСТАВЬТЕ СЮДА ВАШ ТОКЕН ОТ BOTFATHER
# -------------------------------------------------------------------
BOT_TOKEN = "8636610453:AAEvJuNb05_P5ALrXmebu58Q0I6zkN7-Fn4"

# База данных рецептов
RECIPES_DB = [
    {
        "id": 1,
        "title": "Паста Карбонара 🍝",
        "vegetarian": False,
        "recipe": "1. Отварите спагетти.\n2. Обжарьте бекон.\n3. Смешайте желтки с сыром и солью.\n4. Соедините все вместе на сковороде.",
        "ingredients": {
            "Спагетти": (100, "г"),
            "Бекон": (50, "г"),
            "Яйца": (1, "шт"),
            "Сыр Пармезан": (30, "г")
        }
    },
    {
        "id": 2,
        "title": "Запеченная курица с овощами 🍗",
        "vegetarian": False,
        "recipe": "1. Нарежьте курицу и овощи.\n2. Добавьте специи и оливковое масло.\n3. Запекайте в духовке 35 минут при 180°C.",
        "ingredients": {
            "Куриное филе": (200, "г"),
            "Картофель": (150, "г"),
            "Кабачок": (100, "г"),
            "Оливковое масло": (10, "мл")
        }
    },
    {
        "id": 3,
        "title": "Овощное рагу с нутом 🥗",
        "vegetarian": True,
        "recipe": "1. Обжарьте лук и морковь.\n2. Добавьте нут и томаты в собственном соку.\n3. Тушите 20 минут.",
        "ingredients": {
            "Нут вареный": (150, "г"),
            "Томаты в с/с": (100, "г"),
            "Морковь": (1, "шт"),
            "Лук репчатый": (1, "шт")
        }
    },
    {
        "id": 4,
        "title": "Стейк из лосося с рисом 🐟",
        "vegetarian": False,
        "recipe": "1. Отварите рис.\n2. Обжарьте стейк лосося по 3-4 минуты с каждой стороны.\n3. Подавайте с лимоном.",
        "ingredients": {
            "Стейк лосося": (1, "шт"),
            "Рис": (80, "г"),
            "Лимон": (0.5, "шт")
        }
    },
    {
        "id": 5,
        "title": "Греческий салат с фетой 🥗",
        "vegetarian": True,
        "recipe": "1. Крупно нарежьте огурцы, томаты и перец.\n2. Добавьте маслины и сыр Фета.\n3. Заправьте оливковым маслом.",
        "ingredients": {
            "Огурцы": (1, "шт"),
            "Помидоры": (1, "шт"),
            "Перец болгарский": (0.5, "шт"),
            "Сыр Фета": (80, "г"),
            "Маслины": (30, "г")
        }
    }
]


# FSM Состояния
class UserPreferences(StatesGroup):
    persons = State()
    dinners = State()
    vegetarian = State()


# Главная клавиатура
def main_keyboard():
    kb = [
        [InlineKeyboardButton(text="Новая подборка ✨", callback_data="new_selection")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

WELCOME_TEXT = (
    "Привет 🍝\n\n"
    "Этот бот поможет вам решить три задачи сразу:\n\n"
    "🍝 тратить меньше на продукты\n"
    "🍝 экономить свое время\n"
    "🍝 готовить разнообразные ужины\n\n"
    "Мы подберём для вас ужины на неделю и сразу составим список продуктов для магазина."
)

HOW_IT_WORKS_TEXT = (
    "Как это работает 🍿\n\n"
    "~ вы выбираете, сколько ужинов хотите приготовить на неделе\n"
    "~ указываете, на сколько человек\n"
    "~ бот предлагает подборку блюд и рецепты к ним (при желании их можно заменить)\n"
    "~ после этого вы получаете готовый список продуктов для покупки"
)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.update_data(persons=1, dinners=3, vegetarian=False)

    await message.answer(WELCOME_TEXT)
    await message.answer(HOW_IT_WORKS_TEXT)

    user_data = await state.get_data()
    veg_status = "да" if user_data.get("vegetarian") else "нет"
    info_text = (
        f"Учли ваши предпочтения ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons')}\n"
        f"• Кол-во ужинов: {user_data.get('dinners')}\n"
        f"• Вегетарианские блюда: {veg_status}"
    )

    await message.answer(info_text, reply_markup=main_keyboard())


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
    await call.message.edit_text("Сколько ужинов на неделю планируем?", reply_markup=kb)
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    dinners = int(call.data.split("_")[1])
    await state.update_data(dinners=dinners)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да 🌱", callback_data="v_yes"), InlineKeyboardButton(text="Нет 🥩", callback_data="v_no")]
    ])
    await call.message.edit_text("Нужно только вегетарианское меню?", reply_markup=kb)
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    is_veg = True if call.data == "v_yes" else False
    await state.update_data(vegetarian=is_veg)

    user_data = await state.get_data()
    veg_status = "да" if user_data.get("vegetarian") else "нет"

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
    is_veg = data.get("vegetarian", False)

    available_recipes = [r for r in RECIPES_DB if not is_veg or r["vegetarian"]]

    if len(available_recipes) < dinners_count:
        selected_recipes = available_recipes
    else:
        selected_recipes = random.sample(available_recipes, dinners_count)

    await state.update_data(current_selection=[r["id"] for r in selected_recipes])

    response = f"✨ **Ваша подборка ужинов на {len(selected_recipes)} дней ({persons} чел.):**\n\n"
    for idx, recipe in enumerate(selected_recipes, 1):
        response += f"**{idx}. {recipe['title']}**\n{recipe['recipe']}\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Получить список покупок", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="🔄 Другая подборка", callback_data="new_selection")]
    ])

    await call.message.answer(response, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "get_shopping_list")
async def shopping_list(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    persons = data.get("persons", 1)
    selected_ids = data.get("current_selection", [])

    if not selected_ids:
        await call.answer("Сначала сгенерируйте подборку!")
        return

    shopping_cart: Dict[str, List] = {}

    for r_id in selected_ids:
        recipe = next((r for r in RECIPES_DB if r["id"] == r_id), None)
        if recipe:
            for ing, (amount, unit) in recipe["ingredients"].items():
                total_amount = amount * persons
                if ing in shopping_cart:
                    shopping_cart[ing][0] += total_amount
                else:
                    shopping_cart[ing] = [total_amount, unit]

    result_text = f"🛒 **Список продуктов в магазин ({persons} чел.):**\n\n"
    for ing, (amount, unit) in shopping_cart.items():
        amount_str = f"{amount:.1f}".rstrip('0').rstrip('.') if isinstance(amount, float) else str(amount)
        result_text += f"• {ing}: **{amount_str} {unit}**\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В главное меню 🏠", callback_data="back_main")]
    ])

    await call.message.answer(result_text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data == "back_main")
async def back_to_main(call: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    veg_status = "да" if user_data.get("vegetarian") else "нет"

    info_text = (
        f"Учли ваши предпочтения ✏️❤️\n"
        f"• Кол-во человек: {user_data.get('persons', 1)}\n"
        f"• Кол-во ужинов: {user_data.get('dinners', 3)}\n"
        f"• Вегетарианские блюда: {veg_status}"
    )

    await call.message.answer(info_text, reply_markup=main_keyboard())


# Функция заглушки для порта Render
async def handle_ping(request):
    return web.Response(text="Bot is live!")


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
    # Запускаем фоновый веб-сервер для удержания Render Free Tier
    await start_web_server()
    # Запускаем самого бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
