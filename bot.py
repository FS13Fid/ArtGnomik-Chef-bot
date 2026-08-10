import asyncio
import json
import logging
import os
import uuid
from typing import Dict

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from openai import AsyncOpenAI
import segmind

# Импортируем официальный SDK ЮKassa
from yookassa import Configuration, Payment

# -------------------------------------------------------------------
# НАСТРОЙКИ И КЛЮЧИ (БЕЗОПАСНОЕ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ)
# -------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
SEGMIND_API_KEY = os.environ.get("SEGMIND_API_KEY", "").strip()

# Устанавливаем ключ для Segmind (Kandinsky)
if SEGMIND_API_KEY:
    segmind.api_key = SEGMIND_API_KEY

# ОСНОВНАЯ И РЕЗЕРВНАЯ МОДЕЛИ (Groq)
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)
GROQ_MODEL = "llama-3.3-70b-versatile"

# РЕЗЕРВНЫЙ КЛИЕНТ (вторая модель Groq для подстраховки)
reserve_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)
RESERVE_MODEL = "llama-3.1-8b-instant"

# НАСТРОЙКИ ЮKASSA
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "").strip()

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

if not BOT_TOKEN:
    logging.warning("BOT_TOKEN не задан!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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
        [InlineKeyboardButton(text="Список покупок 🛒", callback_data="get_shopping_list")],
        [InlineKeyboardButton(text="Настроить фильтрацию ⚙️", callback_data="settings")]
    ]

    if not is_full:
        kb.insert(0, [InlineKeyboardButton(text="💳 Купить подписку (10 руб)", callback_data="buy_subscription")])
    else:
        kb.insert(0, [InlineKeyboardButton(text="✨ Полный доступ активен", callback_data="sub_active")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


def check_access(user_id: int) -> bool:
    return USERS_DB.get(user_id, {}).get("is_full", True)


# -------------------------------------------------------------------
# ГЕНЕРАЦИЯ КАРТИНОК ЧЕРЕЗ KANDINSKY (SEGMIND)
# -------------------------------------------------------------------
async def fetch_dish_image(dish_name: str) -> str:
    """Генерирует уникальную картинку блюда через модель Kandinsky 2.2."""
    try:
        prompt_text = f"Professional food photography of {dish_name}, restaurant quality, appetizing, highly detailed, 4k"
        
        # Запускаем генерацию в отдельном потоке, так как segmind синхронный
        result = await asyncio.to_thread(
            segmind.run,
            "kandinsky2.2-txt2img",
            prompt=prompt_text,
            negative_prompt="lowres, text, error, cropped, worst quality, low quality, blurry",
            samples=1,
            num_inference_steps=25,
            img_width=512,
            img_height=512,
            base64=False
        )
        
        if result and "output" in result:
            output_url = result["output"][0] if isinstance(result["output"], list) else result["output"]
            return output_url
            
    except Exception as e:
        logging.error(f"Ошибка генерации картинки через Kandinsky: {e}")

    # Надежная резервная база на случай ошибки API
    name_lower = dish_name.lower()
    if "суп" in name_lower or "борщ" in name_lower or "бульон" in name_lower or "щи" in name_lower:
        return "https://images.unsplash.com/photo-1547592166-23ac45744acd?w=800&auto=format&fit=crop&q=80"
    elif "салат" in name_lower:
        return "https://images.unsplash.com/photo-1540420773420-3366772f4999?w=800&auto=format&fit=crop&q=80"
    elif "паст" in name_lower or "спагетти" in name_lower or "макарон" in name_lower:
        return "https://images.unsplash.com/photo-1621996346565-e3d5d6281298?w=800&auto=format&fit=crop&q=80"
    elif "куриц" in name_lower or "индейк" in name_lower or "филе" in name_lower:
        return "https://images.unsplash.com/photo-1604908176997-125f2596f378?w=800&auto=format&fit=crop&q=80"
    elif "мяс" in name_lower or "стейк" in name_lower or "говядин" in name_lower or "свинин" in name_lower:
        return "https://images.unsplash.com/photo-1544025162-d76694265947?w=800&auto=format&fit=crop&q=80"
    elif "рыб" in name_lower or "лосось" in name_lower or "треск" in name_lower:
        return "https://images.unsplash.com/photo-1519708227418-c8fd9a32b7a2?w=800&auto=format&fit=crop&q=80"
    
    return "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=800&auto=format&fit=crop&q=80"


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
                "value": "10.00",
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
            [InlineKeyboardButton(text="💳 Оплатить 10 руб", url=confirmation_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_payment_{payment_id}")]
        ])

        await call.message.answer(
            "💳 **Счет на оплату создан!**\n\n"
            "Нажмите кнопку ниже для перехода на страницу оплаты ЮKassa. После успешной оплаты нажмите **«Проверить оплату»**.",
            reply_markup=kb, parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Ошибка создания платежа ЮKassa: {e}")
        await call.message.answer("❌ Ошибка при создании платежа. Проверьте настройки ЮKassa.")


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
                "✅ **Оплата прошла успешно!**\n\n"
                "Вам предоставлен **полный доступ** к боту. Приятного использования!",
                parse_mode="Markdown",
                reply_markup=main_keyboard(user_id)
            )
        elif payment.status == "pending":
            await call.answer("⏳ Платеж еще не оплачен. Завершите оплату в браузере.", show_alert=True)
        else:
            await call.answer(f"⚠️ Статус платежа: {payment.status}", show_alert=True)

    except Exception as e:
        logging.error(f"Ошибка проверки платежа: {e}")
        await call.answer("❌ Не удалось проверить статус платежа.", show_alert=True)


@dp.callback_query(F.data == "sub_active")
async def sub_active_alert(call: types.CallbackQuery):
    await call.answer("У вас уже активирован полный доступ!", show_alert=True)


# -------------------------------------------------------------------
# ГЕНЕРАЦИЯ МЕНЮ ЧЕРЕЗ GROQ (С РЕЗЕРВОМ)
# -------------------------------------------------------------------
async def generate_groq_menu(persons: int, dinners: int, vegetarian: bool, low_calories: bool, soup_salad: bool,
                             budget: int) -> dict:
    veg_status = "Только вегетарианские блюда!" if vegetarian else "Разнообразные блюда."
    cal_status = "Каждое блюдо до 600 ккал." if low_calories else ""
    soup_status = "Разрешены супы и салаты." if soup_salad else ""
    budget_status = f"Бюджет на все меню: до {budget} руб." if budget > 0 else ""

    prompt = f"""
    Ты профессиональный шеф-повар. Составь меню из {dinners} уникальных ужинов для {persons} человек.
    {veg_status} {cal_status} {soup_status} {budget_status}

    Верни ответ СТРОГО в формате JSON со следующей структурой:
    {{
      "estimated_total_rub": 2500,
      "dishes": [
        {{
          "title": "Название блюда (на русском)",
          "cooking_time": "30 мин",
          "equipment": "Сковорода, плита",
          "serving": "Подавать в горячем виде со свежей зеленью",
          "instructions": [
            "1. [⏱ 10 мин] Первый шаг приготовления...",
            "2. [⏱ 20 мин] Второй шаг приготовления..."
          ],
          "ingredients": [
            {{
              "name": "Куриное филе",
              "amount": 500,
              "unit": "г",
              "category": "protein",
              "is_pantry": false,
              "estimated_price_rub": 350
            }}
          ],
          "recipe_price": 600
        }}
      ]
    }}
    Категории ингредиентов (поле category): "protein", "garnish", "dairy", "vegetables", "pantry", "other".
    Если продукт базовый (соль, перец, растительное масло), ставь "is_pantry": true и "estimated_price_rub": 0.
    """

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.warning(f"Основная модель Groq недоступна ({e}), переключаемся на резервную Llama-3.1-8b...")
        try:
            response = await reserve_client.chat.completions.create(
                model=RESERVE_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            return json.loads(response.choices[0].message.content)
        except Exception as ex:
            logging.error(f"Резервная модель Groq тоже выдала ошибку: {ex}")
            return {
                "estimated_total_rub": 2000,
                "dishes": [
                    {
                        "title": "Паста с томатным соусом",
                        "cooking_time": "20 мин",
                        "equipment": "Кастрюля, сковорода",
                        "serving": "Подавать теплой",
                        "instructions": ["1. [⏱ 10 мин] Отварить макароны.", "2. [⏱ 10 мин] Обжарить с соусом."],
                        "ingredients": [{"name": "Макароны", "amount": 400, "unit": "г", "category": "garnish", "is_pantry": False, "estimated_price_rub": 100}],
                        "recipe_price": 300
                    }
                ]
            }


async def replace_ingredient_in_dish(dish: dict, old_ingredient: str) -> dict:
    prompt = f'Замени ингредиент "{old_ingredient}" в блюде "{dish["title"]}" на подходящий аналог, сохранив структуру рецепта. Верни точно такой же JSON-объект блюда со всеми полями (title, cooking_time, equipment, serving, instructions, ingredients, recipe_price).'
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        try:
            response = await reserve_client.chat.completions.create(
                model=RESERVE_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            return json.loads(response.choices[0].message.content)
        except Exception:
            pass
        return dish


async def generate_dish_replacement_options(persons: int, vegetarian: bool, old_dish_title: str) -> list:
    veg_text = "Вегетарианское." if vegetarian else ""
    prompt = f'Предложи 3 альтернативных блюда взамен "{old_dish_title}" для {persons} человек. {veg_text} Верни JSON с ключом "options" — списком объектов блюд в том же формате, что и основной рецепт.'
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5
        )
        return json.loads(response.choices[0].message.content).get("options", [])
    except Exception:
        try:
            response = await reserve_client.chat.completions.create(
                model=RESERVE_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5
            )
            return json.loads(response.choices[0].message.content).get("options", [])
        except Exception:
            pass
        return []


def format_dish_text(dish: dict, idx: int, persons: int) -> str:
    title = dish.get("title", "Блюдо")
    time_str = dish.get("cooking_time", "15 мин")
    equipment = dish.get("equipment", "Плита")
    serving = dish.get("serving", "По вкусу")
    ing_str = "\n".join([f"• {i['name']} — {i['amount']} {i['unit']}" for i in dish.get("ingredients", [])])
    inst_str = "\n".join(dish.get("instructions", []))

    return (
        f"🍳 **{title.upper()}**\n"
        f"⏱ Время: {time_str} | 👥 На {persons} чел.\n\n"
        f"🛒 **Ингредиенты:**\n{ing_str}\n\n"
        f"⚙️ **Оборудование:** {equipment}\n\n"
        f"📖 **Инструкция:**\n{inst_str}\n\n"
        f"🍽 **Подача:** {serving}"
    )


# -------------------------------------------------------------------
# ХЕНДЛЕРЫ БОТА
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.update_data(persons=1, dinners=4, vegetarian=False, low_calories=False, soup_salad=True, budget=2500)
    welcome_text = "👨‍🍳 **Шеф-Повар Бот**\n\nДобро пожаловать! Настройте параметры и сгенерируйте персональное меню."
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=main_keyboard(user_id))


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("ℹ️ Используйте /start для доступа к главному меню.", parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not check_access(user_id):
        await message.answer("🔒 **Доступ заблокирован!**\n\nДля просмотра меню необходимо приобрести подписку.",
                             parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    if not dishes:
        await message.answer("У вас пока нет сохраненного меню.", reply_markup=main_keyboard(user_id))
        return

    text = "📋 **Ваше текущее меню:**\n\n"
    for idx, dish in enumerate(dishes, 1):
        text += f"**{idx}. {dish['title']}**\n"
    await message.answer(text, parse_mode="Markdown")


@dp.callback_query(F.data == "main_menu")
async def back_to_main_menu(call: types.CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    await call.message.answer("🏠 **Главное меню:**", parse_mode="Markdown", reply_markup=main_keyboard(user_id))


@dp.callback_query(F.data == "settings")
async def start_settings(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer("Требуется покупка подписки!", show_alert=True)
        return

    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 чел", callback_data="p_1"),
         InlineKeyboardButton(text="2 чел", callback_data="p_2"),
         InlineKeyboardButton(text="3 чел", callback_data="p_3")],
        [InlineKeyboardButton(text="4 чел", callback_data="p_4"),
         InlineKeyboardButton(text="5 чел", callback_data="p_5"),
         InlineKeyboardButton(text="6 чел", callback_data="p_6")]
    ])
    await call.message.edit_text("👥 **Кол-во человек:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.persons)


@dp.callback_query(UserPreferences.persons, F.data.startswith("p_"))
async def process_persons(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(persons=int(call.data.split("_")[1]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 ужина", callback_data="d_3"),
         InlineKeyboardButton(text="4 ужина", callback_data="d_4"),
         InlineKeyboardButton(text="5 ужинов", callback_data="d_5")]
    ])
    await call.message.edit_text("🍽 **Кол-во ужинов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.dinners)


@dp.callback_query(UserPreferences.dinners, F.data.startswith("d_"))
async def process_dinners(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(dinners=int(call.data.split("_")[1]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="v_yes"),
         InlineKeyboardButton(text="Нет ❌", callback_data="v_no")]
    ])
    await call.message.edit_text("🌱 **Вы вегетарианец?**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.vegetarian)


@dp.callback_query(UserPreferences.vegetarian, F.data.startswith("v_"))
async def process_veg(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(vegetarian=(call.data == "v_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="c_yes"),
         InlineKeyboardButton(text="Без разницы 🤷‍♂️", callback_data="c_any")]
    ])
    await call.message.edit_text("🥗 Сделать меню менее калорийным (до 600 ккал)?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.calories)


@dp.callback_query(UserPreferences.calories, F.data.startswith("c_"))
async def process_calories(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(low_calories=(call.data == "c_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да ✅", callback_data="s_yes"),
         InlineKeyboardButton(text="Нет ❌", callback_data="s_no")]
    ])
    await call.message.edit_text("🍲 Предлагать ли супы и салаты как основное блюдо?", reply_markup=kb,
                                 parse_mode="Markdown")
    await state.set_state(UserPreferences.soup_salad)


@dp.callback_query(UserPreferences.soup_salad, F.data.startswith("s_"))
async def process_soup_salad(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(soup_salad=(call.data == "s_yes"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1500 руб", callback_data="b_1500"),
         InlineKeyboardButton(text="2500 руб", callback_data="b_2500"),
         InlineKeyboardButton(text="4000 руб", callback_data="b_4000")],
        [InlineKeyboardButton(text="Без ограничений ♾️", callback_data="b_0")]
    ])
    await call.message.edit_text("💰 **Бюджет на закупку продуктов:**", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(UserPreferences.budget)


@dp.callback_query(UserPreferences.budget, F.data.startswith("b_"))
async def process_budget_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(budget=int(call.data.split("_")[1]))
    await finish_settings(call.message, state)


async def finish_settings(message: types.Message, state: FSMContext):
    user_id = message.chat.id if isinstance(message, types.Message) else message.from_user.id
    info_text = "✅ Настройки сохранены!\nТеперь вы можете сгенерировать меню."
    if isinstance(message, types.CallbackQuery):
        await message.message.edit_text(info_text, reply_markup=main_keyboard(user_id), parse_mode="Markdown")
    else:
        await message.answer(info_text, reply_markup=main_keyboard(user_id), parse_mode="Markdown")


@dp.callback_query(F.data == "new_selection")
async def generate_selection(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if not check_access(user_id):
        await call.answer("Функция доступна только после оплаты подписки!", show_alert=True)
        await call.message.answer(
            "🔒 **Требуется подписка**\n\nДля генерации персонального меню оформите подписку кнопкой ниже.",
            parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    await call.answer("Генерирую персональное меню и рисую уникальные фото через Kandinsky...")
    data = await state.get_data()

    ai_data = await generate_groq_menu(
        data.get("persons", 1),
        data.get("dinners", 4),
        data.get("vegetarian", False),
        data.get("low_calories", False),
        data.get("soup_salad", True),
        data.get("budget", 2500)
    )
    dishes = ai_data.get("dishes", [])
    total_rub = ai_data.get("estimated_total_rub", 0)

    if not dishes:
        await call.message.answer("❌ Произошла ошибка при генерации. Попробуйте еще раз!",
                                  reply_markup=main_keyboard(user_id))
        return

    await state.update_data(current_dishes=dishes, estimated_total_rub=total_rub)
    summary_text = f"✨ **Ваше меню готово!**\n💰 Примерная стоимость: **{total_rub} руб.**\n\n"

    for idx, dish in enumerate(dishes):
        title = dish['title']
        summary_text += f"**{idx + 1}.** {title}\n"
        caption = format_dish_text(dish, idx, data.get("persons", 1))

        # Генерируем картинку через Kandinsky
        image_url = await fetch_dish_image(title)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Заменить продукт", callback_data=f"replace_ing_select_{idx}"),
                InlineKeyboardButton(text="🔁 Заменить блюдо", callback_data=f"replace_dish_options_{idx}")
            ]
        ])

        try:
            await call.message.answer_photo(photo=image_url, caption=caption, parse_mode="Markdown", reply_markup=kb)
            continue
        except Exception as e:
            logging.error(f"Не удалось отправить фото: {e}")

        await call.message.answer(caption, parse_mode="Markdown", reply_markup=kb)

    kb_final = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="get_shopping_list"),
         InlineKeyboardButton(text="🔄 Пересоздать меню", callback_data="new_selection")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")]
    ])
    await call.message.answer(summary_text, parse_mode="Markdown", reply_markup=kb_final)


@dp.callback_query(F.data.startswith("replace_ing_select_"))
async def select_ingredient_to_replace(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer("Требуется подписка!", show_alert=True)
        return
    await call.answer()
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    if not dishes or dish_idx >= len(dishes):
        return

    dish = dishes[dish_idx]
    buttons = [[InlineKeyboardButton(text=f"• {ing['name']}", callback_data=f"do_replace_{dish_idx}_{ing_idx}")] for
               ing_idx, ing in enumerate(dish.get("ingredients", []))]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_replace")])
    await call.message.reply(f"Какой продукт заменяем в блюде **«{dish['title']}»**?", parse_mode="Markdown",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "cancel_replace")
async def cancel_replace(call: types.CallbackQuery):
    await call.answer()
    await call.message.delete()


@dp.callback_query(F.data.startswith("do_replace_"))
async def execute_ingredient_replacement(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        return
    await call.answer("Заменяю ингредиент...")
    parts = call.data.split("_")
    dish_idx, ing_idx = int(parts[2]), int(parts[3])
    data = await state.get_data()
    dishes = data.get("current_dishes", [])

    dish = dishes[dish_idx]
    target_ing = dish["ingredients"][ing_idx]["name"]
    updated_dish = await replace_ingredient_in_dish(dish, target_ing)
    dishes[dish_idx] = updated_dish

    await state.update_data(current_dishes=dishes)
    res_text = f"✅ **Рецепт обновлен!**\n\n" + format_dish_text(updated_dish, dish_idx, data.get("persons", 1))
    await call.message.edit_text(res_text, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("replace_dish_options_"))
async def offer_dish_replacements(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer("Требуется подписка!", show_alert=True)
        return
    await call.answer()
    dish_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    old_dish = data.get("current_dishes", [])[dish_idx]

    options = await generate_dish_replacement_options(data.get("persons", 1), data.get("vegetarian", False),
                                                      old_dish['title'])
    if not options:
        await call.message.answer("❌ Не удалось сгенерировать варианты.")
        return

    await state.update_data(temp_replacement_options=options, target_dish_idx=dish_idx)
    buttons = [[InlineKeyboardButton(text=f"🍽 {opt['title']}", callback_data=f"apply_dish_swap_{opt_idx}")] for
               opt_idx, opt in enumerate(options)]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_replace")])
    await call.message.reply("Выберите новое блюдо:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("apply_dish_swap_"))
async def apply_dish_swap(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        return
    await call.answer("Применяю замену...")
    opt_idx = int(call.data.split("_")[-1])
    data = await state.get_data()
    dish_idx, dishes, options = data.get("target_dish_idx"), data.get("current_dishes", []), data.get(
        "temp_replacement_options", [])

    dishes[dish_idx] = options[opt_idx]
    await state.update_data(current_dishes=dishes)

    caption = f"✅ **Блюдо заменено!**\n\n" + format_dish_text(dishes[dish_idx], dish_idx, data.get("persons", 1))
    await call.message.edit_text(caption, parse_mode="Markdown")


@dp.callback_query(F.data == "get_shopping_list")
async def shopping_list(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer("Требуется подписка!", show_alert=True)
        return
    await call.answer()
    data = await state.get_data()
    dishes = data.get("current_dishes", [])
    if not dishes:
        await call.message.answer("❌ Сначала сгенерируйте подборку!")
        return

    dinners_count = len(dishes)
    persons_count = data.get("persons", 1)
    total_rub = data.get("estimated_total_rub", 0)

    shop_categories = {
        "protein": {"title": "🥩 Белок:", "items": {}},
        "garnish": {"title": "🍚 Гарнир:", "items": {}},
        "dairy": {"title": "🧀 Молочка:", "items": {}},
        "vegetables": {"title": "🍅 Овощи:", "items": {}},
        "other": {"title": "📦 Прочее:", "items": {}}
    }

    pantry_categories = {
        "oil": {"title": "🧈 Масло:", "items": {}},
        "spices": {"title": "🧂 Приправа:", "items": {}},
        "other_pantry": {"title": "📦 Прочее дома:", "items": {}}
    }

    for dish in dishes:
        for ing in dish.get("ingredients", []):
            name = ing["name"].capitalize()
            amount = ing.get("amount", 0)
            unit = ing.get("unit", "")
            is_pantry = ing.get("is_pantry", False)
            cat = ing.get("category", "other")

            if is_pantry:
                if "масло" in name.lower():
                    target_dict = pantry_categories["oil"]["items"]
                elif "перец" in name.lower() or "соль" in name.lower() or "приправ" in name.lower():
                    target_dict = pantry_categories["spices"]["items"]
                else:
                    target_dict = pantry_categories["other_pantry"]["items"]
            else:
                target_dict = shop_categories.get(cat, shop_categories["other"])["items"]

            if name in target_dict:
                target_dict[name]["amount"] += amount
            else:
                target_dict[name] = {"amount": amount, "unit": unit}

    res = f"📦 Список покупок ({dinners_count} бл., {persons_count} чел.)\n\n"

    for cat_data in shop_categories.values():
        if cat_data["items"]:
            res += f"{cat_data['title']}\n"
            for name, info in cat_data['items'].items():
                res += f"• {name} — {info['amount']} {info['unit']}\n"
            res += "\n"

    has_pantry_items = any(cat["items"] for cat in pantry_categories.values())
    if has_pantry_items:
        res += "🏠 Скорее всего есть у вас дома:\n\n"
        for cat_data in pantry_categories.values():
            if cat_data["items"]:
                res += f"{cat_data['title']}\n"
                for name, info in cat_data['items'].items():
                    res += f"• {name} — {info['amount']} {info['unit']}\n"
                res += "\n"

    res += f"💳 Примерная стоимость корзины покупок: {total_rub} руб."

    kb_shop = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")]
    ])

    await call.message.answer(res, parse_mode="Markdown", reply_markup=kb_shop)


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
