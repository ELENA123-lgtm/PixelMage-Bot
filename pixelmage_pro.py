import os
import asyncio
import logging
import aiohttp
import base64
import uuid
import json
import hashlib
import sqlite3
from datetime import datetime
from collections import deque
from typing import List, Dict, Any, Union, Optional
from aiohttp import ClientTimeout
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    FSInputFile, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Проверяем переменные окружения
logger.info("=" * 50)
logger.info("ПРОВЕРКА ПЕРЕМЕННЫХ RAILWAY")
logger.info("=" * 50)

# Получаем значения
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

logger.info(f"BOT_TOKEN не пустой: {bool(BOT_TOKEN)}")
logger.info(f"AITUNNEL_API_KEY не пустой: {bool(AITUNNEL_API_KEY)}")
logger.info(f"YOOKASSA_SHOP_ID не пустой: {bool(YOOKASSA_SHOP_ID)}")
logger.info(f"YOOKASSA_SECRET_KEY не пустой: {bool(YOOKASSA_SECRET_KEY)}")

if not BOT_TOKEN or not AITUNNEL_API_KEY:
    logger.error("❌ ОШИБКА: BOT_TOKEN или AITUNNEL_API_KEY не найдены!")
    exit(1)

if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    logger.warning("⚠️ ВНИМАНИЕ: YOOKASSA ключи не найдены, включен тестовый режим оплаты")
else:
    logger.info("✅ YOOKASSA ключи найдены, реальная оплата включена")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== КОНСТАНТЫ ==========
YOUR_USER_ID = 953958006  # ⬅️ ЗАМЕНИТЕ ЭТО НА ВАШ РЕАЛЬНЫЙ TELEGRAM ID!

# ========== БАЗА ДАННЫХ ==========
def init_db():
    """Инициализация всех баз данных"""
    try:
        # База для кэша
        conn = sqlite3.connect('bot_cache.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS image_cache
                     (prompt_hash TEXT PRIMARY KEY,
                      file_path TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_stats
                     (user_id INTEGER PRIMARY KEY,
                      requests_count INTEGER DEFAULT 0,
                      total_images INTEGER DEFAULT 0,
                      last_request TIMESTAMP)''')
        conn.commit()
        conn.close()
        
        # База для платежей
        conn = sqlite3.connect('payments.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS payments
                     (user_id INTEGER,
                      amount REAL,
                      payment_id TEXT,
                      status TEXT,
                      yookassa_payment_id TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_balance
                     (user_id INTEGER PRIMARY KEY,
                      images_left INTEGER DEFAULT 0,
                      total_spent REAL DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS payment_history
                     (user_id INTEGER,
                      amount REAL,
                      description TEXT,
                      status TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()
        conn.close()
        logger.info("✅ Базы данных инициализированы")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")

init_db()

# ========== ОЧЕРЕДЬ ЗАПРОСОВ ==========
request_queue = deque()
queue_lock = asyncio.Lock()
PROCESSING_LIMIT = 3
MAX_PROMPTS_PER_BATCH = 5

# ========== ФУНКЦИИ КЭША ==========
def get_cached_image(prompt: str) -> Optional[str]:
    """Получает изображение из кэша"""
    try:
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        conn = sqlite3.connect('bot_cache.db')
        c = conn.cursor()
        c.execute("SELECT file_path FROM image_cache WHERE prompt_hash = ?", (prompt_hash,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Ошибка получения из кэша: {e}")
        return None

def save_to_cache(prompt: str, file_path: str):
    """Сохраняет изображение в кэш"""
    try:
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        conn = sqlite3.connect('bot_cache.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO image_cache (prompt_hash, file_path) VALUES (?, ?)",
                  (prompt_hash, file_path))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка сохранения в кэш: {e}")

def update_user_stats(user_id: int, images_count: int = 1):
    """Обновляет статистику пользователя"""
    try:
        conn = sqlite3.connect('bot_cache.db')
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO user_stats 
                     (user_id, requests_count, total_images, last_request) 
                     VALUES (?, COALESCE((SELECT requests_count FROM user_stats WHERE user_id = ?), 0) + 1,
                             COALESCE((SELECT total_images FROM user_stats WHERE user_id = ?), 0) + ?,
                             ?)''',
                  (user_id, user_id, user_id, images_count, datetime.now()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка обновления статистики: {e}")

def enhance_edit_prompt(original_prompt: str) -> str:
    """Автоматически улучшаем промпт для сохранения лиц"""
    keywords_for_background = ['фон', 'background', 'задний план', 'пейзаж', 'окружение', 'пейзаж', 'обстановка']
    keywords_for_style = ['стиль', 'style', 'в стиле', 'как', 'похоже на', 'стилизация']
    keywords_for_clothing = ['одежда', 'костюм', 'платье', 'футболка', 'clothing', 'outfit', 'наряд', 'форма']
    keywords_for_addition = ['добавь', 'добавить', 'add', 'положи', 'размести', 'вставь']
    keywords_for_removal = ['убери', 'удалить', 'remove', 'убери', 'сотри', 'убери']

    prompt_lower = original_prompt.lower()

    if any(keyword in prompt_lower for keyword in keywords_for_background):
        return (
            f"Change ONLY the background to: {original_prompt}. "
            f"Keep ALL people EXACTLY the same. "
            f"Preserve facial features, hair, clothing, poses, body positions. "
            f"Only the background should change, people remain identical."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_clothing):
        return (
            f"Change clothing/style to: {original_prompt}. "
            f"But keep faces 100% identical. "
            f"Preserve facial features, expressions, hairstyle. "
            f"Only modify clothing, accessories, outfit."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_addition):
        return (
            f"Add to the image: {original_prompt}. "
            f"Do NOT change existing people. "
            f"Keep faces, bodies, clothing exactly as they are. "
            f"Only add new elements to the scene."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_removal):
        return (
            f"Remove from the image: {original_prompt}. "
            f"Keep all people unchanged. "
            f"Preserve faces, features, poses. "
            f"Only remove specified elements."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_style):
        return (
            f"Apply this artistic style to the image: {original_prompt}. "
            f"Try to keep faces recognizable. "
            f"Maintain general composition, subjects, and poses. "
            f"Preserve the essence of the original photo."
        )
    else:
        return (
            f"{original_prompt}. "
            f"Try to preserve faces and people if possible. "
            f"Keep facial features similar. "
            f"Maintain the original composition and subjects."
        )

# ========== СОСТОЯНИЯ FSM ==========
class Form(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_batch_prompts = State()
    waiting_for_edit_prompt = State()
    waiting_for_photo = State()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard(user_id: int = None):
    """Основная клавиатура с кнопками - кнопка админа только для вас"""
    # Базовая клавиатура для всех пользователей
    buttons = [
        [KeyboardButton(text="🎨 Создать"), KeyboardButton(text="📝 Пакет промптов")],
        [KeyboardButton(text="✏️ Редактировать"), KeyboardButton(text="ℹ️ Помощь")],
        [KeyboardButton(text="💰 Цены/Оплата"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🚪 /start"), KeyboardButton(text="⬅️ Назад")]
    ]
    
    # Добавляем кнопку админа ТОЛЬКО для вас
    if user_id == YOUR_USER_ID:
        # Вставляем строку с админ-кнопкой перед последней строкой
        buttons.insert(-1, [KeyboardButton(text="👑 Админ-панель")])
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard

def get_cancel_keyboard():
    """Клавиатура для отмены"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True
    )

def get_payment_keyboard():
    """Клавиатура для оплаты"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Я оплатил")],
            [KeyboardButton(text="🔄 Проверить оплату")],
            [KeyboardButton(text="⬅️ Назад")]
        ],
        resize_keyboard=True
    )

# ========== БАЛАНС И ОПЛАТА ==========
async def check_balance(user_id: int) -> int:
    """Проверяет баланс пользователя"""
    try:
        conn = sqlite3.connect('payments.db')
        c = conn.cursor()
        c.execute("SELECT images_left FROM user_balance WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Ошибка проверки баланса: {e}")
        return 0

async def deduct_balance(user_id: int, amount: int = 1) -> bool:
    """Списывает изображения с баланса"""
    try:
        conn = sqlite3.connect('payments.db')
        c = conn.cursor()
        
        # Проверяем баланс
        c.execute("SELECT images_left FROM user_balance WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        
        if not result or result[0] < amount:
            conn.close()
            return False
        
        # Списание
        c.execute("UPDATE user_balance SET images_left = images_left - ? WHERE user_id = ?", 
                  (amount, user_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка списания баланса: {e}")
        return False

async def add_balance(user_id: int, images_to_add: int, amount: float):
    """Добавляет изображения на баланс"""
    try:
        conn = sqlite3.connect('payments.db')
        c = conn.cursor()
        
        # Проверяем существует ли пользователь
        c.execute("SELECT images_left FROM user_balance WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        
        if result is None:
            # Создаем новую запись
            c.execute("INSERT INTO user_balance (user_id, images_left, total_spent) VALUES (?, ?, ?)",
                      (user_id, images_to_add, amount))
        else:
            # Обновляем существующую запись
            c.execute("UPDATE user_balance SET images_left = images_left + ?, total_spent = total_spent + ? WHERE user_id = ?",
                      (images_to_add, amount, user_id))
        
        # Добавляем в историю платежей
        c.execute("INSERT INTO payment_history (user_id, amount, description, status) VALUES (?, ?, ?, ?)",
                  (user_id, amount, f"Пополнение: {images_to_add} изображений", 'completed'))
        
        conn.commit()
        conn.close()
        logger.info(f"✅ Баланс добавлен: user_id={user_id}, images={images_to_add}, amount={amount}")
    except Exception as e:
        logger.error(f"Ошибка добавления баланса: {e}")

def get_images_count_by_amount(amount: float) -> int:
    """Сколько изображений дать за сумму - ПРАВИЛЬНЫЕ ЦЕНЫ"""
    if amount == 39.0:    # Редактирование - 39 руб
        return 1
    elif amount == 29.0:  # Генерация - 29 руб (привлекательная цена)
        return 1
    elif amount == 99.0:  # Пакет - 99 руб за 5 (выгодно!)
        return 5
    elif amount == 199.0: # Большой пакет - 199 руб за 15 (очень выгодно!)
        return 15
    return 0

# ========== ЮKASSA ОПЛАТА ==========
async def create_yookassa_payment(user_id: int, amount: float, description: str):
    """Создает платеж в ЮKassa"""
    
    # Если ключей нет, используем тестовый режим
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return await create_test_payment(user_id, amount, description)
    
    try:
        import yookassa
        from yookassa import Payment, Configuration
        
        # Настройка
        Configuration.account_id = YOOKASSA_SHOP_ID
        Configuration.secret_key = YOOKASSA_SECRET_KEY
        
        # Уникальный ID платежа
        payment_id = f"{user_id}_{int(datetime.now().timestamp())}"
        
        # Данные платежа
        payment_data = {
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB"
            },
            "payment_method_data": {
                "type": "bank_card"  # Оплата картой
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{BOT_TOKEN.split(':')[0]}"  # ID бота
            },
            "capture": True,
            "description": description,
            "metadata": {
                "user_id": user_id,
                "images_to_add": get_images_count_by_amount(amount)
            }
        }
        
        # Создаем платеж
        payment = Payment.create(payment_data, payment_id)
        
        # Сохраняем в БД
        conn = sqlite3.connect('payments.db')
        c = conn.cursor()
        c.execute('''INSERT INTO payments 
                     (user_id, amount, payment_id, yookassa_payment_id, status, created_at) 
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (user_id, amount, payment_id, payment.id, 'pending', datetime.now()))
        conn.commit()
        conn.close()
        
        return {
            "success": True,
            "payment_url": payment.confirmation.confirmation_url,
            "payment_id": payment.id,
            "amount": amount
        }
        
    except Exception as e:
        logger.error(f"Ошибка создания платежа ЮKassa: {e}")
        # Если ошибка, переключаемся на тестовый режим
        return await create_test_payment(user_id, amount, description)

async def create_test_payment(user_id: int, amount: float, description: str):
    """Тестовый режим оплаты (если нет ключей ЮKassa)"""
    images_to_add = get_images_count_by_amount(amount)
    
    # Зачисляем изображения
    await add_balance(user_id, images_to_add, amount)
    
    # Сохраняем в историю
    conn = sqlite3.connect('payments.db')
    c = conn.cursor()
    payment_id = f"test_{uuid.uuid4().hex}"
    c.execute("INSERT INTO payments (user_id, amount, payment_id, status) VALUES (?, ?, ?, ?)",
              (user_id, amount, payment_id, 'completed'))
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "test_mode": True,
        "images_added": images_to_add,
        "amount": amount
    }

async def check_payment_status(payment_id: str):
    """Проверяет статус платежа"""
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return None
    
    try:
        import yookassa
        from yookassa import Payment
        
        payment = Payment.find_one(payment_id)
        return payment.status
    except:
        return None

# ========== ФУНКЦИЯ РЕДАКТИРОВАНИЯ ИЗОБРАЖЕНИЙ ==========
async def edit_image_api(photo_bytes: bytes, edit_prompt: str) -> Dict[str, Any]:
    """Редактирует загруженное фото через AI Tunnel API"""
    temp_file_name = f"temp_upload_{uuid.uuid4().hex}.png"
    with open(temp_file_name, "wb") as f:
        f.write(photo_bytes)

    API_URL = "https://api.aitunnel.ru/v1/images/edits"
    headers = {"Authorization": f"Bearer {AITUNNEL_API_KEY}", "Accept": "application/json"}
    timeout = ClientTimeout(total=120)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logger.info(f"✏️ Редактирую фото: '{edit_prompt[:50]}...'")

            with open(temp_file_name, 'rb') as image_file:
                form_data = aiohttp.FormData()
                form_data.add_field('model', 'flux.2-pro')
                form_data.add_field('prompt', edit_prompt)
                form_data.add_field('n', '1')
                form_data.add_field('size', '1024x1024')
                form_data.add_field('response_format', 'b64_json')
                form_data.add_field('image', image_file, filename='image.png', content_type='image/png')

                async with session.post(API_URL, headers=headers, data=form_data) as response:
                    response_text = await response.text()

                    if response.status == 200:
                        result = await response.json()
                        logger.info("✅ API редактирования вернуло ответ")

                        if 'data' in result and result['data']:
                            if 'b64_json' in result['data'][0]:
                                image_data = result['data'][0]['b64_json']
                            elif 'url' in result['data'][0] and result['data'][0]['url'].startswith('data:image/'):
                                base64_data = result['data'][0]['url'].split('base64,')[1]
                                image_data = base64_data
                            else:
                                return {"success": False, "error": "invalid_response", "message": "Неверный формат ответа API"}

                            image_bytes = base64.b64decode(image_data)
                            file_name = f"edited_{uuid.uuid4().hex}.png"
                            with open(file_name, "wb") as f:
                                f.write(image_bytes)

                            logger.info(f"✅ Изображение сохранено: {file_name}")
                            return {"success": True, "file_path": file_name}
                        else:
                            return {"success": False, "error": "no_data", "message": "API не вернул данные"}
                    else:
                        logger.error(f"❌ Ошибка API {response.status}: {response_text}")
                        try:
                            error_json = json.loads(response_text)
                            error_msg = error_json.get('error', {}).get('message', response_text)
                        except:
                            error_msg = response_text[:200]
                        return {"success": False, "error": f"api_error_{response.status}", "message": f"Ошибка API: {error_msg}"}

    except asyncio.TimeoutError:
        logger.error("❌ Таймаут при редактировании")
        return {"success": False, "error": "timeout", "message": "Таймаут при обработке запроса"}
    except Exception as e:
        logger.exception(f"💥 Ошибка при редактировании: {e}")
        return {"success": False, "error": "unexpected_error", "message": f"Внутренняя ошибка: {str(e)}"}
    finally:
        try:
            if os.path.exists(temp_file_name):
                os.remove(temp_file_name)
        except:
            pass

# ========== ФУНКЦИЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ==========
async def generate_images_api(prompts: List[str]) -> Dict[str, Any]:
    """Генерирует изображения через AI Tunnel API"""
    if not prompts:
        return {"error": "no_prompts", "message": "Нет промптов для генерации"}

    if len(prompts) > 10:
        return {"error": "too_many_images", "message": f"Слишком много промптов ({len(prompts)} > 10)"}

    cached_images = {}
    uncached_prompts = []

    for prompt in prompts:
        cached = get_cached_image(prompt)
        if cached and os.path.exists(cached):
            cached_images[prompt] = cached
        else:
            uncached_prompts.append(prompt)

    if not uncached_prompts and cached_images:
        return {
            "success": True,
            "from_cache": True,
            "results": [{"prompt": p, "file_paths": [cached_images[p]], "from_cache": True} for p in prompts],
            "cached_count": len(cached_images)
        }

    API_URL = "https://api.aitunnel.ru/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {AITUNNEL_API_KEY}",
        "Content-Type": "application/json"
    }

    all_results = []

    for prompt in uncached_prompts:
        data = {
            "model": "flux.2-pro",
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "num_images": 1
        }

        timeout = ClientTimeout(total=120)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info(f"🔄 Генерирую изображение для: {prompt[:50]}...")

                async with session.post(API_URL, headers=headers, json=data) as response:
                    if response.status == 200:
                        result = await response.json()

                        if 'data' in result and isinstance(result['data'], list):
                            file_paths = []

                            for idx, item in enumerate(result['data']):
                                if 'url' in item and item['url'].startswith('data:image/'):
                                    if 'base64,' in item['url']:
                                        base64_data = item['url'].split('base64,')[1]
                                        image_bytes = base64.b64decode(base64_data)

                                        file_name = f"generated_{uuid.uuid4().hex}_{idx}.png"
                                        with open(file_name, "wb") as f:
                                            f.write(image_bytes)

                                        file_paths.append(file_name)
                                elif 'b64_json' in item:
                                    image_bytes = base64.b64decode(item['b64_json'])
                                    file_name = f"generated_{uuid.uuid4().hex}_{idx}.png"
                                    with open(file_name, "wb") as f:
                                        f.write(image_bytes)
                                    file_paths.append(file_name)

                            if file_paths:
                                save_to_cache(prompt, file_paths[0])
                                all_results.append({
                                    "prompt": prompt,
                                    "file_paths": file_paths,
                                    "from_cache": False
                                })
                                logger.info(f"✅ Успешно сгенерирован промпт: {prompt[:50]}")
                            else:
                                all_results.append({
                                    "prompt": prompt,
                                    "error": "no_images",
                                    "message": "API не вернул изображения"
                                })
                        else:
                            all_results.append({
                                "prompt": prompt,
                                "error": "invalid_response",
                                "message": "Неверный ответ от API"
                            })
                    else:
                        error_text = await response.text()
                        logger.error(f"❌ Ошибка API {response.status} для промпта: {prompt[:50]}")
                        all_results.append({
                            "prompt": prompt,
                            "error": "api_error",
                            "message": f"Ошибка API: {response.status}"
                        })

        except Exception as e:
            logger.error(f"❌ Ошибка генерации для промпта '{prompt}': {e}")
            all_results.append({
                "prompt": prompt,
                "error": "processing_error",
                "message": str(e)[:100]
            })

    for prompt in cached_images:
        all_results.append({
            "prompt": prompt,
            "file_paths": [cached_images[prompt]],
            "from_cache": True
        })

    successful_results = [r for r in all_results if "file_paths" in r]

    return {
        "success": len(successful_results) > 0,
        "from_cache": False,
        "results": all_results,
        "cached_count": len(cached_images),
        "total_requested": len(prompts),
        "total_received": len(successful_results)
    }

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Команда /start"""
    welcome_text = (
        "🎨 <b>PixelMage Pro 2.0</b>\n\n"
        "<b>Продвинутый генератор изображений с реальной оплатой</b>\n\n"
        "<b>Основные функции:</b>\n"
        "🎨 <b>Создать</b> - одно изображение по промпту\n"
        "📝 <b>Пакет промптов</b> - до 5 промптов → до 5 изображений за раз\n"
        "✏️ <b>Редактировать</b> - изменить фон, стиль или элементы на фото\n\n"
        "<i>💡 Для использования нужны изображения на балансе</i>\n"
        "<i>💡 Пополнить баланс можно через 💰 Цены/Оплата</i>\n\n"
        "<b>💰 Реальная оплата через ЮKassa:</b>\n"
        "• Безопасно и надежно\n"
        "• Карты, СБП, ЮMoney\n"
        "• Мгновенное зачисление\n\n"
        "<i>Выберите действие ниже:</i>"
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "🚪 /start")
async def btn_start_again(message: types.Message, state: FSMContext):
    """Повторный запуск через кнопку"""
    await state.clear()
    await cmd_start(message)

@dp.message(F.text == "⬅️ Назад")
async def cancel_action(message: types.Message, state: FSMContext):
    """Отмена текущего действия"""
    await state.clear()
    await message.answer("✅ Возвращаюсь в главное меню", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Command("price"))
@dp.message(F.text == "💰 Цены/Оплата")
async def cmd_price(message: types.Message):
    """Показывает цены"""
    user_id = message.from_user.id
    balance = await check_balance(user_id)
    
    text = (
        "🎨 <b>Тарифы PixelMage Pro</b>\n\n"
        f"💰 <b>Ваш баланс:</b> {balance} изображений\n\n"
        "🖼 <b>Генерация изображений:</b>\n"
        "• 🎟 1 редактирование — <b>39 руб.</b>\n"
        "• 💰 1 генерация — <b>29 руб.</b>\n"
        "• 📦 Пакет 5 промптов — <b>99 руб.</b> (экономия 46 руб!)\n"
        "• 🎁 Большой пакет 15 промптов — <b>199 руб.</b> (экономия 236 руб!)\n\n"
        "💳 <b>Как оплатить:</b>\n"
        "1. Нажмите на нужную кнопку с ценой\n"
        "2. Перейдите по ссылке для оплаты\n"
        "3. Оплатите картой, СБП или ЮMoney\n"
        "4. Вернитесь в бота и нажмите ✅ Я оплатил\n\n"
        "<i>После оплаты изображения зачислятся автоматически</i>"
    )
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎟 1 редактирование - 39 руб"), KeyboardButton(text="💰 1 генерация - 29 руб")],
            [KeyboardButton(text="📦 Пакет 5 промптов - 99 руб"), KeyboardButton(text="🎁 Большой пакет 15 - 199 руб")],
            [KeyboardButton(text="📊 Мой баланс"), KeyboardButton(text="⬅️ Назад")]
        ],
        resize_keyboard=True
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(F.text == "📊 Мой баланс")
async def btn_my_balance(message: types.Message):
    """Показать баланс"""
    user_id = message.from_user.id
    
    conn = sqlite3.connect('payments.db')
    c = conn.cursor()
    c.execute("SELECT images_left, total_spent FROM user_balance WHERE user_id = ?", (user_id,))
    balance_data = c.fetchone()
    
    # Получаем историю платежей
    c.execute("SELECT amount, description, status, created_at FROM payment_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (user_id,))
    history = c.fetchall()
    conn.close()
    
    if balance_data:
        images_left, total_spent = balance_data
        text = (
            f"💰 <b>Ваш баланс</b>\n\n"
            f"• Доступно изображений: <b>{images_left}</b>\n"
            f"• Всего потрачено: <b>{total_spent} руб.</b>\n\n"
        )
        
        if history:
            text += "📋 <b>Последние платежи:</b>\n"
            for amount, description, status, created_at in history:
                status_icon = "✅" if status == 'completed' else "⏳"
                text += f"• {status_icon} {amount} руб. - {description}\n"
    else:
        text = (
            f"💰 <b>Ваш баланс</b>\n\n"
            f"• Доступно изображений: <b>0</b>\n"
            f"• Всего потрачено: <b>0 руб.</b>\n\n"
            f"<i>У вас пока нет оплаченных изображений</i>\n"
            f"<i>Используйте кнопки ниже для пополнения</i>"
        )
    
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))

# ========== КНОПКИ ОПЛАТЫ ==========
@dp.message(F.text.startswith("🎟"))
async def btn_buy_edit(message: types.Message):
    """Покупка редактирования (39 руб)"""
    await create_payment_menu(message, 39.0, "1 редактирование изображения")

@dp.message(F.text.startswith("💰"))
async def btn_buy_generate(message: types.Message):
    """Покупка генерации (29 руб)"""
    await create_payment_menu(message, 29.0, "1 генерация изображения")

@dp.message(F.text.startswith("📦"))
async def btn_buy_batch(message: types.Message):
    """Покупка пакета (99 руб)"""
    await create_payment_menu(message, 99.0, "Пакет 5 промптов")

@dp.message(F.text.startswith("🎁"))
async def btn_buy_big_batch(message: types.Message):
    """Покупка большого пакета (199 руб)"""
    await create_payment_menu(message, 199.0, "Большой пакет 15 промптов")

async def create_payment_menu(message: types.Message, amount: float, description: str):
    """Создает меню оплаты"""
    user_id = message.from_user.id
    
    # Создаем платеж в ЮKassa
    result = await create_yookassa_payment(user_id, amount, description)
    
    if not result.get("success"):
        await message.answer(
            f"❌ Ошибка при создании платежа: {result.get('error', 'Неизвестная ошибка')}",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    if result.get("test_mode"):
        # Тестовый режим - уже зачислено
        await message.answer(
            f"✅ <b>ТЕСТОВЫЙ РЕЖИМ</b>\n\n"
            f"<b>Услуга:</b> {description}\n"
            f"<b>Сумма:</b> {amount} руб.\n"
            f"<b>Зачислено:</b> {result['images_added']} изображений\n\n"
            f"<i>В тестовом режиме оплата не требуется</i>\n"
            f"<i>Теперь можете использовать бота!</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        # Реальный платеж
        payment_url = result.get("payment_url")
        
        await message.answer(
            f"💳 <b>Счет на оплату</b>\n\n"
            f"<b>Услуга:</b> {description}\n"
            f"<b>Сумма:</b> {amount} руб.\n"
            f"<b>Получите:</b> {get_images_count_by_amount(amount)} изображений\n\n"
            f"<b>Для оплаты:</b>\n"
            f"1. Нажмите на ссылку ниже 👇\n"
            f"2. Оплатите через СБП или карту\n"
            f"3. Вернитесь в бота\n"
            f"4. Нажмите <b>✅ Я оплатил</b>\n\n"
            f"🔗 <a href='{payment_url}'>Оплатить {amount} руб.</a>\n\n"
            f"<i>После успешной оплаты изображения автоматически зачислятся на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_payment_keyboard()
        )

@dp.message(F.text == "✅ Я оплатил")
async def btn_payment_done(message: types.Message):
    """Проверка оплаты"""
    user_id = message.from_user.id
    
    # Ищем последний ожидающий платеж пользователя
    conn = sqlite3.connect('payments.db')
    c = conn.cursor()
    c.execute("SELECT payment_id, yookassa_payment_id, amount FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (user_id,))
    payment_data = c.fetchone()
    conn.close()
    
    if not payment_data:
        await message.answer(
            "ℹ️ У вас нет ожидающих платежей.\n\n"
            "Если вы только что оплатили, подождите 1-2 минуты.\n"
            "Система обрабатывает платежи автоматически.",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    payment_id, yookassa_payment_id, amount = payment_data
    
    # Проверяем статус в ЮKassa
    if yookassa_payment_id:
        status = await check_payment_status(yookassa_payment_id)
        
        if status == 'succeeded':
            # Зачисляем изображения
            images_to_add = get_images_count_by_amount(amount)
            await add_balance(user_id, images_to_add, amount)
            
            # Обновляем статус платежа
            conn = sqlite3.connect('payments.db')
            c = conn.cursor()
            c.execute("UPDATE payments SET status = 'completed' WHERE payment_id = ?", (payment_id,))
            c.execute("INSERT INTO payment_history (user_id, amount, description, status) VALUES (?, ?, ?, ?)",
                      (user_id, amount, f"Покупка {images_to_add} изображений", 'completed'))
            conn.commit()
            conn.close()
            
            balance = await check_balance(user_id)
            
            await message.answer(
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"<b>Зачислено:</b> {images_to_add} изображений\n"
                f"<b>Ваш баланс:</b> {balance} изображений\n\n"
                f"<i>Теперь можете использовать функции бота</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
        elif status == 'pending':
            await message.answer(
                "⏳ <b>Платеж еще обрабатывается</b>\n\n"
                "Обычно это занимает 1-2 минуты.\n"
                "Попробуйте нажать <b>✅ Я оплатил</b> через минуту.",
                parse_mode="HTML",
                reply_markup=get_payment_keyboard()
            )
        else:
            await message.answer(
                f"❌ <b>Платеж не найден или отменен</b>\n\n"
                f"Статус: {status}\n\n"
                f"Попробуйте оплатить снова или обратитесь в поддержку.",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
    else:
        # Тестовый режим
        await message.answer(
            "ℹ️ <b>ТЕСТОВЫЙ РЕЖИМ</b>\n\n"
            "В тестовом режиме оплата не требуется.\n"
            "Изображения уже зачислены на ваш баланс.\n"
            "Используйте кнопку 📊 Мой баланс для проверки.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )

@dp.message(F.text == "🔄 Проверить оплату")
async def btn_check_payment(message: types.Message):
    """Проверка оплаты"""
    await btn_payment_done(message)

# ========== ОСНОВНЫЕ КНОПКИ ==========
@dp.message(F.text == "🎨 Создать")
async def btn_single(message: types.Message, state: FSMContext):
    """Одно изображение"""
    user_id = message.from_user.id
    balance = await check_balance(user_id)
    
    if balance <= 0:
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Чтобы создать изображение:\n"
            "1. Нажмите 💰 Цены/Оплата\n"
            "2. Выберите тариф\n"
            "3. Оплатите необходимое количество\n\n"
            f"<i>Ваш баланс: {balance} изображений</i>\n"
            f"<i>💡 Совет: Пакет 5 промптов за 99 руб выгоднее!</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    await message.answer(
        "✍️ <b>Введите описание изображения:</b>\n\n"
        "<i>Пример: космический пейзаж с планетами</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_prompt)

@dp.message(F.text == "📝 Пакет промптов")
async def btn_batch(message: types.Message, state: FSMContext):
    """Пакетная обработка промптов"""
    user_id = message.from_user.id
    balance = await check_balance(user_id)
    
    if balance < 1:
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Для пакетной обработки нужно минимум 1 изображение\n"
            f"<i>Ваш баланс: {balance} изображений</i>\n\n"
            "💡 <b>Совет:</b> Возьмите пакет 5 промптов за 99 руб - это выгоднее!",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    await message.answer(
        "📝 <b>Введите до 5 промптов через точку с запятой:</b>\n\n"
        "<i>Пример: космический кот; фэнтези замок; неоновый город</i>\n"
        "<i>Каждый промпт → одно изображение</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_batch_prompts)

@dp.message(F.text == "✏️ Редактировать")
async def btn_edit(message: types.Message, state: FSMContext):
    """Редактирование фото"""
    user_id = message.from_user.id
    balance = await check_balance(user_id)
    
    if balance <= 0:
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Для редактирования фото нужно 1 изображение\n"
            f"<i>Ваш баланс: {balance} изображений</i>\n\n"
            "💡 <b>Совет:</b> Купите пакет - будет дешевле в пересчете на одно изображение!",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return
    
    await message.answer(
        "✏️ <b>Редактирование фото (улучшенная версия)</b>\n\n"
        "📤 <b>Загрузите фото для редактирования:</b>\n\n"
        "<i>Что лучше всего работает:</i>\n"
        "• Замена фона (лучше всего сохраняет лица) 🏆\n"
        "• Добавление элементов к фото\n"
        "• Изменение стиля изображения\n"
        "• Удаление объектов с фото\n\n"
        "<i>⚠️ AI постарается сохранить лица, но результат не гарантирован</i>\n"
        "<i>Поддерживаются: JPG, PNG</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_photo)

@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка"""
    help_text = (
        "📋 <b>PixelMage Pro - Полная справка</b>\n\n"
        "<b>🎨 Создать (один промпт):</b>\n"
        "• Стоимость: 1 изображение с баланса\n"
        "• Введите описание изображения\n"
        "• Используется кэш для повторных запросов\n\n"
        "<b>📝 Пакет промптов (до 5):</b>\n"
        "• Каждый промпт = 1 изображение с баланса\n"
        "• Введите до 5 промптов через точку с запятой\n"
        "• Эффективная пакетная обработка\n\n"
        "<b>✏️ Редактировать (улучшенная версия):</b>\n"
        "• Стоимость: 1 изображение с баланса\n"
        "• Загрузите фото как образец\n"
        "• Введите, что изменить (фон, стиль, элементы)\n"
        "• AI старается сохранить лица людей\n\n"
        "<b>💰 <u>ВЫГОДНЫЕ ТАРИФЫ:</u></b>\n"
        "• 🎟 1 редактирование: <b>39 руб.</b>\n"
        "• 💰 1 генерация: <b>29 руб.</b>\n"
        "• 📦 Пакет 5 промптов: <b>99 руб.</b> (экономия 46 руб!)\n"
        "• 🎁 Большой пакет 15 промптов: <b>199 руб.</b> (экономия 236 руб!)\n\n"
        "<b>💳 Оплата:</b> Безопасно через ЮKassa\n"
        "<b>📊 Статистика:</b> Ваша активность и баланс\n\n"
        "<b>Примеры промптов:</b>\n"
        "• космический кот в скафандре\n"
        "• портрет эльфа; фэнтези арт; магический лес\n"
        "• поменяй фон на пляж"
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика пользователя"""
    user_id = message.from_user.id
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()

    c.execute("SELECT requests_count, total_images, last_request FROM user_stats WHERE user_id = ?", (user_id,))
    user_stats = c.fetchone()

    c.execute("SELECT COUNT(*) FROM image_cache")
    cache_count = c.fetchone()[0]

    conn.close()
    
    balance = await check_balance(user_id)

    if user_stats:
        requests_count, total_images, last_request = user_stats
        stats_text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"<b>Текущий баланс:</b> {balance} изображений\n"
            f"<b>Запросов:</b> {requests_count}\n"
            f"<b>Изображений создано:</b> {total_images}\n"
            f"<b>Последний запрос:</b> {last_request}\n"
            f"<b>Изображений в кэше:</b> {cache_count}\n\n"
            f"<i>Кэш экономит деньги на повторные запросы!</i>"
        )
    else:
        stats_text = (
            f"📊 <b>Статистика</b>\n\n"
            f"<b>Текущий баланс:</b> {balance} изображений\n"
            f"Вы еще не создавали изображений\n"
            f"<b>Изображений в кэше бота:</b> {cache_count}\n\n"
            f"Попробуйте создать первое изображение!"
        )

    await message.answer(stats_text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))

# ========== КНОПКА АДМИН-ПАНЕЛИ ==========
@dp.message(F.text == "👑 Админ-панель")
async def btn_admin_panel(message: types.Message):
    """Обработка кнопки админ-панели"""
    # Вызываем ту же функцию, что и для команды /admin
    await cmd_admin(message)

# ========== ОБРАБОТКА СОСТОЯНИЙ ==========
@dp.message(StateFilter(Form.waiting_for_prompt))
async def process_single_prompt(message: types.Message, state: FSMContext):
    """Обработка одиночного промпта"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard(message.from_user.id))
        return

    prompt = message.text.strip()
    if not prompt:
        await message.answer("⚠️ Введите описание изображения")
        return

    if len(prompt) > 1000:
        await message.answer("⚠️ Промпт слишком длинный (макс. 1000 символов)")
        return

    # Проверяем и списываем баланс
    user_id = message.from_user.id
    if not await deduct_balance(user_id, 1):
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Пополните баланс через 💰 Цены/Оплата",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()
        return

    await message.answer(
        f"🎨 <b>Генерирую:</b> <i>{prompt}</i>\n"
        f"⏳ Подождите 20-30 секунд...\n",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            await state.clear()
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api([prompt])

        if result.get("success"):
            update_user_stats(message.from_user.id, 1)
            await handle_generation_results(message, result)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}\n\n"
                f"<i>Изображение возвращено на баланс</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            # Возвращаем изображение на баланс при ошибке
            await add_balance(user_id, 1, 0)

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}\n\n"
            f"<i>Изображение возвращено на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        # Возвращаем изображение на баланс при ошибке
        await add_balance(user_id, 1, 0)
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

        await state.clear()

@dp.message(StateFilter(Form.waiting_for_batch_prompts))
async def process_batch_prompts(message: types.Message, state: FSMContext):
    """Обработка пакета промптов"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard(message.from_user.id))
        return

    prompts_text = message.text.strip()
    if not prompts_text:
        await message.answer("⚠️ Введите промпты через точку с запятой")
        return

    prompts = []
    for p in prompts_text.split(';'):
        p = p.strip()
        if p:
            prompts.append(p)

    if not prompts:
        await message.answer("⚠️ Не найдено валидных промптов")
        return

    if len(prompts) > MAX_PROMPTS_PER_BATCH:
        prompts = prompts[:MAX_PROMPTS_PER_BATCH]
        await message.answer(f"⚠️ Будут обработаны первые {MAX_PROMPTS_PER_BATCH} промптов")

    for i, prompt in enumerate(prompts):
        if len(prompt) > 1000:
            await message.answer(f"⚠️ Промпт #{i + 1} слишком длинный (макс. 1000 символов)")
            return

    user_id = message.from_user.id
    # Проверяем и списываем баланс за все промпты
    if not await deduct_balance(user_id, len(prompts)):
        await message.answer(
            f"❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            f"Нужно: {len(prompts)} изображений\n"
            f"Пополните баланс через 💰 Цены/Оплата",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()
        return

    prompt_preview = "\n".join([f"• {p[:30]}{'...' if len(p) > 30 else ''}" for p in prompts[:3]])
    if len(prompts) > 3:
        prompt_preview += f"\n• ... и еще {len(prompts) - 3} промптов"

    await message.answer(
        f"📦 <b>Обрабатываю {len(prompts)} промптов:</b>\n"
        f"{prompt_preview}\n"
        f"⏳ Это займет {len(prompts) * 15} секунд...",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            await state.clear()
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api(prompts)

        if result.get("success"):
            successful_count = result.get("total_received", 0)
            update_user_stats(message.from_user.id, successful_count)
            await handle_generation_results(message, result, is_batch=True)
            
            # Возвращаем неиспользованные изображения
            failed_count = len(prompts) - successful_count
            if failed_count > 0:
                await add_balance(user_id, failed_count, 0)
                await message.answer(
                    f"📊 <b>Возвращено на баланс:</b> {failed_count} изображений\n"
                    f"<i>За неудавшиеся генерации</i>",
                    parse_mode="HTML"
                )
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}\n\n"
                f"<i>Все изображения возвращены на баланс</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            # Возвращаем все изображения при ошибке
            await add_balance(user_id, len(prompts), 0)

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}\n\n"
            f"<i>Все изображения возвращены на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        # Возвращаем все изображения при ошибке
        await add_balance(user_id, len(prompts), 0)
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

        await state.clear()

@dp.message(StateFilter(Form.waiting_for_photo), F.photo)
async def process_edit_photo(message: types.Message, state: FSMContext):
    """Обработка загруженного фото"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard(message.from_user.id))
        return

    user_id = message.from_user.id
    # Проверяем и списываем баланс ДО загрузки фото
    if not await deduct_balance(user_id, 1):
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Пополните баланс через 💰 Цены/Оплата",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()
        return

    try:
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)

        temp_file = f"temp_edit_{uuid.uuid4().hex}.jpg"
        await bot.download_file(file.file_path, temp_file)

        with open(temp_file, "rb") as f:
            photo_bytes = f.read()

        await state.update_data(photo_bytes=photo_bytes)

        await message.answer(
            "✍️ <b>Что изменить на фото?</b>\n\n"
            "<i>Примеры (с сохранением лиц):</i>\n"
            "• поменяй фон на пляж 🏝️\n"
            "• добавь солнцезащитные очки 😎\n"
            "• убери человека справа 🚫\n"
            "• сделай в стиле пиксель-арт 🎮\n"
            "• поменяй время суток на ночь 🌙\n\n"
            "<i>💡 Для лучшего результата:</i>\n"
            "• Указывайте конкретные изменения\n"
            "• Для замены фона лица сохраняются лучше всего\n"
            "• AI постарается сохранить оригинальные лица\n\n"
            "<i>Или нажмите ⬅️ Назад</i>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(Form.waiting_for_edit_prompt)

        try:
            os.remove(temp_file)
        except:
            pass

    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        # Возвращаем изображение при ошибке
        await add_balance(user_id, 1, 0)
        await message.answer(
            f"❌ <b>Ошибка загрузки фото:</b> {str(e)[:100]}\n\n"
            f"<i>Изображение возвращено на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()

@dp.message(StateFilter(Form.waiting_for_photo), ~F.photo)
async def process_no_photo(message: types.Message, state: FSMContext):
    """Если пользователь отправил не фото в режиме ожидания фото"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    await message.answer(
        "⚠️ Пожалуйста, отправьте фото для редактирования!\n\n"
        "Или нажмите ⬅️ Назад чтобы вернуться в меню.",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(StateFilter(Form.waiting_for_edit_prompt))
async def process_edit_request(message: types.Message, state: FSMContext):
    """Обработка запроса на редактирование"""
    if message.text == "⬅️ Назад":
        user_id = message.from_user.id
        # Возвращаем изображение при отмене
        await add_balance(user_id, 1, 0)
        await state.clear()
        await message.answer(
            "⬅️ Возвращаюсь в главное меню\n\n"
            "<i>Изображение возвращено на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")
    edit_prompt = message.text.strip()

    if not photo_bytes:
        await message.answer("❌ Фото не загружено", reply_markup=get_main_keyboard(message.from_user.id))
        await state.clear()
        return

    if not edit_prompt:
        await message.answer("⚠️ Введите, что изменить на фото")
        return

    enhanced_prompt = enhance_edit_prompt(edit_prompt)

    await message.answer(
        f"✏️ <b>Редактирую (стараюсь сохранить лица):</b> <i>{edit_prompt[:80]}</i>\n"
        f"⏳ Подождите 20-30 секунд...",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    result = await edit_image_api(photo_bytes, enhanced_prompt)

    if result.get("success"):
        file_path = result.get("file_path")

        if file_path and os.path.exists(file_path):
            try:
                photo = FSInputFile(file_path)
                await message.answer_photo(
                    photo,
                    caption=f"✅ Отредактировано: {edit_prompt[:100]}",
                    reply_markup=get_main_keyboard(message.from_user.id)
                )

                try:
                    os.remove(file_path)
                except:
                    pass

            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")
                await message.answer(
                    "✅ Редактирование завершено, но не удалось отправить фото",
                    reply_markup=get_main_keyboard(message.from_user.id)
                )
        else:
            user_id = message.from_user.id
            # Возвращаем изображение при ошибке
            await add_balance(user_id, 1, 0)
            await message.answer(
                "❌ Ошибка при сохранении файла\n\n"
                "<i>Изображение возвращено на баланс</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
    else:
        error_type = result.get("error", "unknown")
        error_msg = result.get("message", "Неизвестная ошибка")
        user_id = message.from_user.id
        
        # Возвращаем изображение при ошибке
        await add_balance(user_id, 1, 0)

        if "400" in error_type:
            user_msg = (
                "⚠️ <b>Не удалось отредактировать фото</b>\n\n"
                "Возможные причины:\n"
                "• Промпт слишком сложный\n"
                "• API не понял запрос\n"
                "• Попробуйте упростить описание\n\n"
                "<i>Изображение возвращено на баланс</i>"
            )
        elif "rate_limit" in error_type or "429" in error_type:
            user_msg = "⏳ Превышен лимит запросов. Попробуйте через 1-2 минуты.\n\n<i>Изображение возвращено на баланс</i>"
        elif "timeout" in error_type:
            user_msg = "⏳ Превышено время ожидания. Попробуйте позже.\n\n<i>Изображение возвращено на баланс</i>"
        else:
            user_msg = f"❌ Ошибка редактирования: {error_msg}\n\n<i>Изображение возвращено на баланс</i>"

        await message.answer(
            user_msg,
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )

    await state.clear()

async def handle_generation_results(message: types.Message, result: Dict[str, Any],
                                    is_batch: bool = False):
    """Универсальная обработка результатов генерации"""
    if not result.get("success"):
        error_msg = result.get("message", "Неизвестная ошибка")
        await message.answer(
            f"❌ <b>Ошибка:</b> {error_msg}\n\n"
            f"<i>Попробуйте упростить промпт или использовать другую функцию</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    results = result.get("results", [])
    cached_count = result.get("cached_count", 0)
    total_requested = result.get("total_requested", 0)
    total_received = result.get("total_received", 0)

    if not results:
        await message.answer(
            "❌ Нет результатов генерации\n"
            "Попробуйте другой промпт",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    if cached_count > 0:
        await message.answer(f"⚡ Использовано из кэша: {cached_count}", parse_mode="HTML")

    successful_results = [r for r in results if "file_paths" in r and not r.get("error")]

    for res in successful_results:
        prompt = res.get("prompt", "Без названия")
        file_paths = res.get("file_paths", [])
        from_cache = res.get("from_cache", False)

        if not file_paths:
            continue

        for i, file_path in enumerate(file_paths):
            try:
                photo = FSInputFile(file_path)
                caption = f"✅ {prompt[:100]}"
                if from_cache:
                    caption += " (из кэша)"
                if len(file_paths) > 1:
                    caption += f" [{i + 1}/{len(file_paths)}]"

                await message.answer_photo(
                    photo,
                    caption=caption,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")

        if not from_cache:
            for file_path in file_paths:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except:
                    pass

    error_results = [r for r in results if r.get("error")]
    if error_results:
        error_msg = "⚠️ <b>Частичные ошибки:</b>\n"
        for res in error_results[:3]:
            error_msg += f"• {res.get('prompt', '?')[:30]}: {res.get('message', 'Ошибка')}\n"

        if len(error_results) > 3:
            error_msg += f"<i>... и еще {len(error_results) - 3} ошибок</i>"

        await message.answer(error_msg, parse_mode="HTML")

    success_count = len(successful_results)

    if is_batch:
        summary = f"📦 <b>Пакетная обработка завершена:</b> {success_count}/{total_requested} успешно"
    else:
        summary = f"🎨 <b>Генерация завершена:</b> {success_count} изображений"

    if cached_count > 0:
        summary += f", {cached_count} из кэша"

    balance = await check_balance(message.from_user.id)
    summary += f"\n💰 <b>Ваш баланс:</b> {balance} изображений"
    
    # Добавляем подсказку про выгоду
    if balance < 3:
        summary += "\n\n💡 <b>Совет:</b> Возьмите пакет 5 промптов за 99 руб - это выгоднее!"
    
    summary += "\n\n✅ <i>Готово! Что создаем дальше?</i>"

    await message.answer(summary, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))

# ========== ТЕКСТОВЫЕ КОМАНДЫ ==========
@dp.message(Command("generate"))
async def cmd_generate_text(message: types.Message):
    """Текстовая команда /generate"""
    prompt = message.text.replace('/generate', '', 1).strip()
    if not prompt:
        await message.answer(
            "📝 <b>Использование:</b> /generate <описание>\n\n"
            "<b>Пример:</b> /generate космический кот в скафандре\n\n"
            "<i>Или используйте кнопку 🎨 Создать</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    user_id = message.from_user.id
    if not await deduct_balance(user_id, 1):
        await message.answer(
            "❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            "Пополните баланс через 💰 Цены/Оплата",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    await message.answer(
        f"🎨 <b>Генерирую:</b> <i>{prompt}</i>\n⏳ Подождите...",
        parse_mode="HTML"
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api([prompt])

        if result.get("success"):
            update_user_stats(message.from_user.id, 1)
            await handle_generation_results(message, result)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}\n\n"
                f"<i>Изображение возвращено на баланс</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            await add_balance(user_id, 1, 0)

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}\n\n"
            f"<i>Изображение возвращено на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await add_balance(user_id, 1, 0)
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

@dp.message(Command("batch"))
async def cmd_batch_text(message: types.Message):
    """Текстовая команда /batch"""
    prompts_text = message.text.replace('/batch', '', 1).strip()

    if not prompts_text:
        await message.answer(
            "📝 <b>Использование:</b> /batch <промпт1>; <промпт2>; ...\n\n"
            "<b>Пример:</b> /batch космический кот; фэнтези замок; неоновый город\n"
            "<b>Максимум:</b> 5 промптов за раз\n\n"
            "<i>Или используйте кнопку 📝 Пакет промптов</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    prompts = []
    for p in prompts_text.split(';'):
        p = p.strip()
        if p:
            prompts.append(p)

    if not prompts:
        await message.answer("⚠️ Не найдено валидных промптов")
        return

    if len(prompts) > MAX_PROMPTS_PER_BATCH:
        prompts = prompts[:MAX_PROMPTS_PER_BATCH]
        await message.answer(f"⚠️ Будут обработаны первые {MAX_PROMPTS_PER_BATCH} промптов")

    user_id = message.from_user.id
    if not await deduct_balance(user_id, len(prompts)):
        await message.answer(
            f"❌ <b>Недостаточно изображений на балансе!</b>\n\n"
            f"Нужно: {len(prompts)} изображений\n"
            f"Пополните баланс через 💰 Цены/Оплата",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        return

    await message.answer(
        f"📦 <b>Обрабатываю {len(prompts)} промптов:</b>\n"
        f"<i>{' • '.join(p[:20] + '...' if len(p) > 20 else p for p in prompts)}</i>\n"
        f"⏳ Это займет {len(prompts) * 15} секунд...",
        parse_mode="HTML"
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api(prompts)

        if result.get("success"):
            successful_count = result.get("total_received", 0)
            update_user_stats(message.from_user.id, successful_count)
            await handle_generation_results(message, result, is_batch=True)
            
            failed_count = len(prompts) - successful_count
            if failed_count > 0:
                await add_balance(user_id, failed_count, 0)
                await message.answer(
                    f"📊 <b>Возвращено на баланс:</b> {failed_count} изображений\n"
                    f"<i>За неудавшиеся генерации</i>",
                    parse_mode="HTML"
                )
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}\n\n"
                f"<i>Все изображения возвращены на баланс</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            await add_balance(user_id, len(prompts), 0)

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}\n\n"
            f"<i>Все изображения возвращены на баланс</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await add_balance(user_id, len(prompts), 0)
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

# ========== АДМИН ПАНЕЛЬ ==========
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Админ-панель (только для вас)"""
    
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ Доступ запрещен", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        # Получаем статистику из БД
        conn_cache = sqlite3.connect('bot_cache.db')
        conn_payments = sqlite3.connect('payments.db')
        
        c_cache = conn_cache.cursor()
        c_payments = conn_payments.cursor()
        
        # 1. Статистика пользователей
        c_payments.execute("SELECT COUNT(DISTINCT user_id) FROM user_balance WHERE images_left > 0")
        active_users = c_payments.fetchone()
        active_users = active_users[0] if active_users else 0
        
        c_payments.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'completed'")
        total_users = c_payments.fetchone()
        total_users = total_users[0] if total_users else 0
        
        # 2. Статистика генераций
        c_cache.execute("SELECT COUNT(*) FROM user_stats")
        total_requests = c_cache.fetchone()
        total_requests = total_requests[0] if total_requests else 0
        
        c_cache.execute("SELECT SUM(total_images) FROM user_stats")
        successful_generations = c_cache.fetchone()
        successful_generations = successful_generations[0] if successful_generations else 0
        
        # 3. Статистика по платежам
        c_payments.execute("SELECT SUM(amount) FROM payments WHERE status = 'completed'")
        total_income = c_payments.fetchone()
        total_income = total_income[0] if total_income else 0.0
        
        c_payments.execute("SELECT COUNT(*) FROM payments WHERE status = 'completed'")
        total_payments_count = c_payments.fetchone()
        total_payments_count = total_payments_count[0] if total_payments_count else 0
        
        # 4. Кэш
        c_cache.execute("SELECT COUNT(*) FROM image_cache")
        cache_count = c_cache.fetchone()
        cache_count = cache_count[0] if cache_count else 0
        
        conn_cache.close()
        conn_payments.close()
        
        # Рассчет успешности
        success_rate = 100.0 if total_requests == 0 else (successful_generations / total_requests * 100)
        
        # Проверка API ключа
        api_key_status = "✅ есть" if AITUNNEL_API_KEY else "❌ нет"
        yookassa_status = "✅ включена" if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY else "⏸ тестовый режим"
        
        # Формируем сообщение
        text = (
            f"👑 <b>АДМИН ПАНЕЛЬ</b>\n\n"
            
            f"👥 <b>Пользователи:</b>\n"
            f"• Всего: {total_users}\n"
            f"• С балансом: {active_users}\n\n"
            
            f"🎨 <b>Генерации:</b>\n"
            f"• Всего запросов: {total_requests}\n"
            f"• Успешно: {successful_generations}\n"
            f"• Ошибок: {max(0, total_requests - successful_generations)}\n"
            f"• Успешность: {success_rate:.1f}%\n\n"
            
            f"💰 <b>Финансы:</b>\n"
            f"• Всего поступлений: {total_income} руб.\n"
            f"• Количество платежей: {total_payments_count}\n\n"
            
            f"🔧 <b>Система:</b>\n"
            f"• API ключ: {api_key_status}\n"
            f"• Оплата: {yookassa_status}\n"
            f"• Изображений в кэше: {cache_count}\n"
            f"• Бот работает: ✅ стабильно"
        )
        
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))
    
    except Exception as e:
        logger.error(f"Ошибка админ-панели: {e}")
        await message.answer(
            f"❌ Ошибка при получении статистики: {str(e)[:100]}",
            reply_markup=get_main_keyboard(message.from_user.id)
        )

# ========== ОБРАБОТЧИК ЛЮБЫХ СООБЩЕНИЙ ==========
@dp.message()
async def handle_any_message(message: types.Message, state: FSMContext):
    """Обработчик любых сообщений"""
    current_state = await state.get_state()

    if current_state is None:
        await message.answer(
            "🤖 Я тебя не понял. Используй кнопки или команды!\n\n"
            "Попробуй:\n"
            "/start - перезапустить бота\n"
            "/help - показать справку\n"
            "/price - цены на услуги\n"
            "Или выбери действие из меню ниже 👇",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer(
            "⚠️ Пожалуйста, используй кнопки для текущего действия.\n"
            "Или нажми '⬅️ Назад' чтобы вернуться в меню.",
            reply_markup=get_cancel_keyboard()
        )

# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("=" * 50)
    logger.info("🚀 PIXELMAGE PRO 2.0 ЗАПУЩЕН")
    logger.info("=" * 50)
    logger.info("💰 АТТРАКТИВНЫЕ ЦЕНЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ:")
    logger.info("• Генерация: 29 руб")
    logger.info("• Редактирование: 39 руб")
    logger.info("• Пакет 5 промптов: 99 руб (экономия 46 руб!)")
    logger.info("• Большой пакет 15 промптов: 199 руб (экономия 236 руб!)")
    
    if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
        logger.info("💰 СИСТЕМА ОПЛАТЫ: РЕАЛЬНАЯ (ЮKassa)")
        logger.info(f"Shop ID: {YOOKASSA_SHOP_ID[:10]}...")
    else:
        logger.info("💰 СИСТЕМА ОПЛАТЫ: ТЕСТОВЫЙ РЕЖИМ")
        logger.info("⚠️ Для реальной оплаты добавьте переменные YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")
    
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 PixelMage Pro 2.0 запускается...")
    print("=" * 50)
    print("💰 АТТРАКТИВНЫЕ ЦЕНЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ:")
    print("• 🎨 1 генерация: 29 руб")
    print("• ✏️ 1 редактирование: 39 руб")
    print("• 📦 Пакет 5 промптов: 99 руб (экономия 46 руб!)")
    print("• 🎁 Большой пакет 15 промптов: 199 руб (экономия 236 руб!)")
    print("=" * 50)
    
    if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
        print("✅ РЕАЛЬНАЯ ОПЛАТА ВКЛЮЧЕНА")
        print("• Прием платежей через ЮKassa")
        print("• Карты, СБП, ЮMoney")
        print("• Автоматическое зачисление")
    else:
        print("⚠️ ТЕСТОВЫЙ РЕЖИМ ОПЛАТЫ")
        print("• Для реальных платежей добавьте в Railway:")
        print("  YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")
    
    print("=" * 50)
    print(f"👑 Админ-панель доступна только для ID: {YOUR_USER_ID}")
    print("Отправьте /start в Telegram чтобы начать")
    print("=" * 50)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
