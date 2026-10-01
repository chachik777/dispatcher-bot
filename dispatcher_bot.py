#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import re
import imaplib
import email
import logging
import random
import os
import socket
import time
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters
from telegram.error import ChatMigrated, TimedOut, NetworkError, TelegramError

from aiohttp import web

# ---------- ИМПОРТ СПИСКА УЛИЦ ----------
try:
    from streets import KNOWN_STREETS
except ImportError:
    KNOWN_STREETS = [
        "50 лет Октября", "50 лет ВЛКСМ", "Московский тракт", "Ялуторовская", "Монтажников",
        "Новоселов", "Никольского", "Полевая", "Скандинавская", "Западно-Сибирская",
        "Фабричная", "Беляева", "Дружбы", "Миллераторов", "Мотостроителей",
        "Республики", "Советская", "Ленина", "Гагарина", "Широтная",
        "Сидора Путилова", "Путилова", "Сидорова", "Николая Зелинского",
        "Практическая", "Андрея Корневского", "Арктическая"
    ]
    logging.warning("Файл streets.py не найден, используется базовый список улиц.")

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- ДЕКОРАТОР RETRY (асинхронный) ----------
def retry(max_retries=5, delay=2, backoff=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            _delay = delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Retry failed for {func.__name__}: {e}")
                        raise
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {func.__name__}: {e}")
                    await asyncio.sleep(_delay + random.uniform(0, 0.5))
                    _delay *= backoff
            return None
        return wrapper
    return decorator

# ---------- ДЕКОРАТОР RETRY (синхронный) ----------
def retry_sync(max_retries=10, delay=5, backoff=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _delay = delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Sync retry failed for {func.__name__}: {e}")
                        raise
                    logger.warning(f"Sync retry {attempt+1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(_delay + random.uniform(0, 0.5))
                    _delay *= backoff
            return None
        return wrapper
    return decorator

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8964018097:AAHiQfOwTnwWeVQWUog5vhihmk8lfcDLA74")
GENERAL_GROUP = int(os.getenv("GENERAL_GROUP", "-1003896694214"))

EMAIL = os.getenv("EMAIL", "dir72.pk@mail.ru")
PASSWORD = os.getenv("PASSWORD", "afStBLqMmNzQtZNkc0Mv")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.mail.ru")

WEBHOOK_PORT = int(os.getenv("PORT", "80"))

GROUPS = {
    "computers": [-1004355591778, -1003976268046, -1003395683617, -1004445931308, -1003734200853],
    "appliances": [-1003975989333, -1003981596959],
    "refrigerators": [-1004352137129, -1004382888384],
    "cond": [-1004445931308, -1004486734839, -1004352137129],
    "tv": [-5402877244],
    "orgtech": [-1004360815294],
    "phone": [-1004355591778],
    "vacuum": [-1003896694214],
    "microwave": [-1003896694214],
    "coffee": [-1003896694214],
    "speaker": [-1003896694214],
    "console": [-1003896694214],
    "other": [-1003896694214],
}

CATEGORY_NAMES = {
    "computers": "компьютер",
    "appliances": "крупная бытовая техника",
    "refrigerators": "холодильник",
    "cond": "кондиционер",
    "tv": "телевизор",
    "orgtech": "оргтехника",
    "phone": "телефон",
    "vacuum": "пылесос",
    "microwave": "микроволновка",
    "coffee": "кофемашина",
    "speaker": "колонка",
    "console": "игровая приставка",
    "other": "другое"
}

active_requests = {}
recent_phones = {}
main_loop = None

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
    def handle_data(self, data):
        self.result.append(data)
    def get_text(self):
        return ' '.join(self.result).strip()

def html_to_text(html_str):
    parser = HTMLTextExtractor()
    parser.feed(html_str)
    return parser.get_text()

def extract_body_text(msg):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_payload(decode=True).decode('utf-8', errors='ignore')
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    return html_to_text(html)
        else:
            payload = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            if msg.get_content_type() == "text/plain":
                return payload
            elif msg.get_content_type() == "text/html":
                return html_to_text(payload)
    except Exception as e:
        logger.error(f"extract_body_text error: {e}")
    return None

def clean_text(text):
    text = re.sub(r'(?i)\b(здравствуйте|добрый день|добрый вечер|привет|алло|до свидания|спасибо|пожалуйста)\s*[,.]?\s*', '', text)
    text = re.sub(r'\b(вас слышно|слышно|да|нет|ага|все верно|слышу)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(сейчас|ну|так|вот|значит|это|там|тут|прям|как бы)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^[,\s]+', '', text)
    text = re.sub(r'^(по|про|насчет|касательно)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(хотел[аи]?\s+бы|хочу|надо|нужно|необходимо|планирую|собираюсь|принести|привезти|отвезти|отремонтировать|починить|исправить)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[,.\s]+$', '', text)
    return text.strip()

def is_price_question(text):
    return bool(re.search(r'(цена|стоимость|сколько стоит|рублей|тысяч|руб|тыс|₽|полторы тысячи|от \d+|\d+ рублей|\d+ тыс)', text.lower()))

def is_meaningful_issue(text):
    text_clean = text.strip().rstrip('.,!?;:').strip()
    if len(text_clean) < 3:
        return False
    if text_clean.lower() in ['да', 'нет', 'ага', 'угу', 'ок', 'окей', 'хорошо', 'спасибо', 'до свидания', 'н', 'д',
                              'привет', 'здравствуйте', 'алло', 'слышно', 'понял', 'поняла', 'ясно', 'так', 'ну',
                              'вот', 'это', 'там', 'тут', 'прям', 'как бы', 'просто', 'типа', 'конечно', 'ладно',
                              'добрый день', 'добрый вечер', 'всего хорошего', 'всего доброго']:
        return False
    if re.search(r'(во сколько|когда|подъедет|приедет|оформляла|записали|сегодня|завтра|в четверг|верно|ожидаю|жду|все верно|да-да|ага-ага|угу-угу)', text_clean, re.IGNORECASE):
        return False
    if re.match(r'^[\s.,!?]+$', text_clean):
        return False
    if re.match(r'^[\d\s\-\(\)]+$', text_clean):
        return False
    return True

def find_street_in_text(text):
    text_lower = text.lower()
    for street in KNOWN_STREETS:
        if street.lower() in text_lower:
            return street
        street_clean = re.sub(r'[ьъ]', '', street.lower())
        text_clean = re.sub(r'[ьъ]', '', text_lower)
        if street_clean in text_clean:
            return street
    return None

# ---------- ПАРСЕРЫ ПИСЕМ ----------
def parse_bothelp(body):
    name = brand = "не указано"
    phone = "не указан"
    problem = "не указана"
    name_match = re.search(r'Имя:\s*(.+)', body)
    if name_match: name = name_match.group(1).strip()
    brand_match = re.search(r'device_brand:\s*(.+)', body)
    if brand_match: brand = brand_match.group(1).strip()
    phone_match = re.search(r'phone1:\s*(\d+)', body)
    if phone_match:
        raw_phone = phone_match.group(1).strip()
        digits = re.sub(r'\D', '', raw_phone)
        if len(digits) == 11 and digits.startswith('8'): digits = '7' + digits[1:]
        elif len(digits) == 10 and digits.startswith('9'): digits = '7' + digits
        if len(digits) == 11 and digits.startswith('7'):
            phone = f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
        else:
            phone = raw_phone
    else:
        logger.info("BotHelp: номер телефона не найден, заявка будет пропущена")
        return None
    prob_match = re.search(r'problem:\s*(.+)', body)
    if prob_match: problem = prob_match.group(1).strip()
    return (
        "🚨 Новая заявка (BotHelp)!\n"
        f"👤 Имя: {name}\n📋 Категория: {problem}\n🏣 Марка: {brand}\n📞 Телефон: {phone}\n"
    ), None

def parse_craftum(body):
    name = "не указано"
    phone = "не указан"
    service = "не указана"
    page = "не указана"
    name_match = re.search(r'Имя\s*\n?\s*([^\n]*)', body)
    if name_match:
        raw_name = name_match.group(1).strip()
        if raw_name and raw_name not in ('Телефон', 'Номер телефона'): name = raw_name
    phone_match = re.search(r'(?:Телефон|Номер телефона)\s*\n?\s*(\+?\d[\d\s\(\)\-]+)', body)
    if phone_match:
        raw_phone = phone_match.group(1).strip()
        digits = re.sub(r'\D', '', raw_phone)
        if len(digits) >= 10:
            if len(digits) == 11 and digits.startswith('8'): digits = '7' + digits[1:]
            elif len(digits) == 10 and digits.startswith('9'): digits = '7' + digits
            if len(digits) == 11 and digits.startswith('7'):
                phone = f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
            else:
                phone = digits
        else:
            phone = raw_phone
    else:
        logger.info("Craftum: номер телефона не найден, заявка будет пропущена")
        return None
    service_match = re.search(r'Какая услуга вас интересует\?\s*\n?\s*([^\n]+)', body)
    if not service_match:
        service_match = re.search(r'Выберите ремонт какой техники Вас интересует\s*\n?\s*([^\n]+)', body)
    if service_match: service = service_match.group(1).strip()
    page_match = re.search(r'(https://[^\s]+)', body)
    if page_match: page = page_match.group(1).strip()
    return (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n📋 Услуга: {service}\n📞 Телефон: {phone}\n🌐 Источник: {page}\n"
    ), None

def parse_site(body):
    """Парсер заявок с сайта (старый формат из Web3Forms)."""
    name = "не указано"
    phone = "не указан"
    category_key = "other"
    category_display = "не указана"
    time_display = "не указано"
    problem_display = "не указано"

    def extract_field(field_name):
        pattern1 = re.compile(
            r'^\s*' + re.escape(field_name) + r'\s*:\s*([^\r\n]+)',
            re.MULTILINE | re.IGNORECASE
        )
        m1 = pattern1.search(body)
        if m1:
            val = m1.group(1).strip()
            if val and val.lower() != field_name.lower():
                return val
        return None

    raw = extract_field('name') or extract_field('Имя')
    if raw and len(raw) >= 2:
        name = raw
    raw_phone = extract_field('phone') or extract_field('Телефон')
    if not raw_phone:
        return None
    digits = re.sub(r'\D', '', raw_phone)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('7'):
        phone = f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    else:
        phone = raw_phone

    raw_cat = extract_field('category') or extract_field('Категория')
    if raw_cat:
        category_display = raw_cat
        category_key = detect_category(raw_cat.lower())

    raw_time = extract_field('time') or extract_field('Удобное время')
    if raw_time:
        time_display = raw_time

    raw_problem = extract_field('problem') or extract_field('Неисправность')
    if raw_problem:
        problem_display = raw_problem

    message = (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"📋 Категория: {category_display}\n"
        f"⚙️ Неисправность: {problem_display}\n"
        f"⏰ Удобное время: {time_display}\n"
    )
    return message, category_key

# ---------- WEBHOOK: приём заявок с Cloudflare Worker ----------
def build_message_from_json(data: dict):
    """Собирает текст заявки и category_key из JSON, который шлёт Worker."""
    name = data.get('name', 'не указано')
    phone = data.get('phone', 'не указан')
    category = data.get('category', 'не указана')
    problem = data.get('problem', 'не указано')
    brand = data.get('brand', '')
    age = data.get('age', '')
    time_val = data.get('time', 'не указано')
    price = data.get('price', '')
    source = data.get('source', '')

    if not name or not phone:
        return None, None

    category_key = detect_category(category.lower()) if category else 'other'

    # Форматирование телефона
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('7'):
        phone = f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"

    text = (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"📋 Категория: {category}\n"
        f"⚙️ Неисправность: {problem}\n"
    )
    if brand:
        text += f"🏷 Бренд: {brand}\n"
    if age:
        text += f"📅 Возраст: {age}\n"
    text += f"⏰ Удобное время: {time_val}\n"
    if price:
        try:
            price_num = int(float(price))
            text += f"💰 Примерная цена: от {price_num:,} ₽\n".replace(',', ' ')
        except (ValueError, TypeError):
            text += f"💰 Примерная цена: {price}\n"
    if source:
        text += f"🌐 Источник: {source}\n"

    return text, category_key


async def handle_webhook(request):
    """Принимает POST от Cloudflare Worker."""
    try:
        data = await request.json()
        logger.info(f"WEBHOOK получен: {data}")

        text, category_key = build_message_from_json(data)
        if not text:
            return web.json_response({'success': False, 'error': 'name и phone обязательны'}, status=400)

        # Дедупликация по телефону
        phone_match = re.search(r'📞 Телефон:\s*(.+)', text)
        if phone_match:
            digits = re.sub(r'\D', '', phone_match.group(1))
            now = datetime.now()
            if digits in recent_phones and (now - recent_phones[digits]) < timedelta(hours=1):
                logger.info("WEBHOOK: заявка отклонена (дубликат телефона)")
                return web.json_response({'success': True, 'skipped': 'duplicate'})
            recent_phones[digits] = now

        # Отправляем в GENERAL_GROUP и рассылаем по группам
        asyncio.create_task(send_and_dispatch(text, category_key))

        return web.json_response({'success': True})
    except Exception as e:
        logger.error(f"WEBHOOK error: {e}", exc_info=True)
        return web.json_response({'success': False, 'error': str(e)}, status=500)


async def handle_health(request):
    return web.json_response({'status': 'ok', 'service': 'dispatcher-bot'})


async def start_web_server():
    app = web.Application()
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    logger.info(f"HTTP-сервер запущен на 0.0.0.0:{WEBHOOK_PORT}")

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ (без изменений) ----------
def detect_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["виндовс", "установить", "ноутбук", "компьютер", "моноблок", "системный блок", "монитор", "пк", "залили"]):
        return "computers"
    if any(w in t for w in ["принтер", "мфу", "сканер", "копир", "факс", "плоттер", "оргтехника", "лазерджет", "мфус"]):
        return "orgtech"
    if "кондиционер" in t or "сплит" in t or "чистка кондиционера" in t:
        return "cond"
    if "холодильник" in t or "морозильник" in t:
        return "refrigerators"
    if any(w in t for w in ["стиральн", "посудомоечн", "плит", "духов", "варочн", "водонагревател", "духовой шкаф", "прокладка", "резинка", "манжет"]):
        return "appliances"
    if re.search(r'(телевизор|(?<![\w])тв(?![\w])|плазма|телек)', t):
        return "tv"
    if any(w in t for w in ["телефон", "планшет", "смартфон", "айфон", "iphone", "андроид", "мобильник", "онор", "honor", "технопол", "tecno", "техно"]):
        return "phone"
    if any(w in t for w in ["пылесос", "робот-пылесос"]):
        return "vacuum"
    if any(w in t for w in ["микроволновк", "свч"]):
        return "microwave"
    if any(w in t for w in ["кофемашин", "кофеварк"]):
        return "coffee"
    if any(w in t for w in ["колонка", "алиса", "маруся", "sberbox", "яндекс станция"]):
        return "speaker"
    if any(w in t for w in ["приставка", "xbox", "playstation", "плейстейшн", "плейстайшн", "плюстшн", "плюс сейшн", "ps4", "ps5", "nintendo", "джойстик", "геймпад", "игровая"]):
        return "console"
    return "other"

def get_timeout(category: str) -> int:
    if category == "computers": return 300
    elif category in ("appliances", "refrigerators", "cond", "tv"): return 480
    else: return 300

def mask_phone(text: str, show_last_digits: int = 0) -> str:
    pattern = r'(📞\s*Телефон:\s*)(\+?\d[\d\s\-\(\)]*)'
    match = re.search(pattern, text)
    if not match: return text
    prefix = match.group(1)
    phone_raw = match.group(2)
    digits = re.sub(r'\D', '', phone_raw)
    if len(digits) < 10: return text
    if show_last_digits > 0:
        visible = digits[-show_last_digits:]
        masked = 'X' * (len(digits) - show_last_digits) + visible
    else:
        masked = 'X' * len(digits)
    if digits.startswith('7') or digits.startswith('8'):
        formatted = f"+{digits[0]} ({digits[1:4]}) {masked[4:7]}-{masked[7:9]}-{masked[9:11]}"
    else:
        formatted = masked
    return text.replace(match.group(0), f"{prefix}{formatted}")

@retry(max_retries=3, exceptions=(TimedOut, NetworkError, TelegramError))
async def send_request_with_buttons(chat_id: int, text: str, message_id: int):
    try:
        masked_text = mask_phone(text, show_last_digits=0)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Принимаю", callback_data=f"accept_{message_id}"),
                InlineKeyboardButton("❌ Не беру", callback_data=f"reject_{message_id}")
            ]
        ])
        await bot.send_message(chat_id=chat_id, text=masked_text, reply_markup=keyboard)
    except ChatMigrated as e:
        new_id = e.migrate_to_chat_id
        for cat, groups in GROUPS.items():
            if chat_id in groups:
                groups.remove(chat_id)
                groups.append(new_id)
                break
        await send_request_with_buttons(new_id, text, message_id)

async def start_timer(message_id: int):
    await asyncio.sleep(1)
    req = active_requests.get(message_id)
    if not req: return
    category = req.get("category", "other")
    timeout = get_timeout(category) - 1
    await asyncio.sleep(timeout)
    req = active_requests.get(message_id)
    if not req: return
    req["current_group_index"] += 1
    groups = req["groups"]
    if req["current_group_index"] >= len(groups):
        await bot.send_message(
            chat_id=GENERAL_GROUP,
            text=f"⚠️ Никто не принял заявку:\n\n{req['text']}"
        )
        del active_requests[message_id]
    else:
        next_group = groups[req["current_group_index"]]
        await send_request_with_buttons(next_group, req["text"], message_id)
        task = asyncio.create_task(start_timer(message_id))
        req["timer"] = task

async def handle_general_message(update, context):
    if update.effective_chat.id != GENERAL_GROUP: return
    text = update.message.text
    if "🚨 Новая заявка" not in text: return
    text = re.sub(r'^@\w+\s*', '', text)
    await dispatch_request(text)

async def handle_callback(update, context):
    query = update.callback_query
    data = query.data
    await query.answer()
    message_id = int(data.split("_")[1])
    if data.startswith("accept_"):
        req = active_requests.pop(message_id, None)
        if not req:
            await query.edit_message_text("Заявка уже обработана.")
            return
        if req["timer"]: req["timer"].cancel()
        username = query.from_user.username or query.from_user.first_name
        chat_id = query.message.chat_id
        msg_id = query.message.message_id
        full_text = req["text"]
        await query.edit_message_text(f"✅ Заявку принял @{username}\n\n{full_text}")
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=msg_id, disable_notification=True)
        except Exception as e:
            logger.error(f"Не удалось закрепить сообщение: {e}")
        try:
            await bot.send_message(
                chat_id=GENERAL_GROUP,
                text=f"✅ Мастер @{username} принял заявку:\n{full_text}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление в общую группу: {e}")
    elif data.startswith("reject_"):
        req = active_requests.get(message_id)
        if not req:
            await query.edit_message_text("Заявка уже неактивна.")
            return
        if req["timer"]: req["timer"].cancel()
        try: await query.message.delete()
        except Exception as e: logger.error(f"Не удалось удалить сообщение: {e}")
        req["current_group_index"] += 1
        groups = req["groups"]
        if req["current_group_index"] >= len(groups):
            await bot.send_message(
                chat_id=GENERAL_GROUP,
                text=f"⚠️ Никто не принял заявку:\n\n{req['text']}"
            )
            del active_requests[message_id]
        else:
            next_group = groups[req["current_group_index"]]
            await send_request_with_buttons(next_group, req["text"], message_id)
            task = asyncio.create_task(start_timer(message_id))
            req["timer"] = task

async def dispatch_request(text, category_key=None):
    if "🚨 Новая заявка" not in text:
        return

    if category_key is None:
        text_for_cat = re.sub(r'\n?📞 Телефон:.*$', '', text, flags=re.MULTILINE)
        text_for_cat = re.sub(r'\n?Телефон:.*$', '', text_for_cat, flags=re.MULTILINE)
        text_for_cat = re.sub(r'\n?Номер телефона:.*$', '', text_for_cat, flags=re.MULTILINE)
        category_key = detect_category(text_for_cat)

    if category_key == "orgtech":
        org_group = GROUPS["orgtech"][0]
        try:
            await bot.send_message(chat_id=org_group, text=text)
            logger.info(f"Заявка отправлена в группу оргтехники: {org_group}")
        except Exception as e:
            logger.error(f"Не удалось отправить заявку в группу оргтехники ({org_group}): {e}")
            try:
                await bot.send_message(chat_id=GENERAL_GROUP, text=text)
                logger.info("Заявка отправлена в общую группу (fallback для оргтехники).")
            except Exception as e2:
                logger.error(f"Не удалось отправить заявку даже в общую группу: {e2}")
        return

    groups = GROUPS.get(category_key, GROUPS["other"])
    message_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    task = asyncio.create_task(start_timer(message_id))
    active_requests[message_id] = {
        "text": text,
        "current_group_index": 0,
        "groups": groups,
        "timer": task,
        "category": category_key
    }
    await send_request_with_buttons(groups[0], text, message_id)

async def send_and_dispatch(text, category_key=None):
    logger.info("send_and_dispatch вызван")
    if "⚠️" in text:
        try:
            await bot.send_message(chat_id=GENERAL_GROUP, text=text)
            logger.info("сообщение об ошибке отправлено в общую группу")
        except Exception as e:
            logger.error(f"отправка ошибки в общую группу не удалась: {e}")
        return
    try:
        await bot.send_message(chat_id=GENERAL_GROUP, text=text)
        logger.info("сообщение в общую группу отправлено успешно")
    except Exception as e:
        logger.error(f"отправка в общую группу не удалась: {e}")
        return
    await dispatch_request(text, category_key)

# ---------- ПРОВЕРКА ПОЧТЫ ----------
EXCLUDED_TECH = [
    'вытяжка', 'вытяжки', 'вытяжкой', 'фен', 'фена', 'феном', 'утюг', 'утюга', 'утюгом',
    'плойка', 'плойки', 'плойкой', 'мультиварка', 'мультиварки', 'мультиваркой',
    'блендер', 'блендера', 'блендером', 'тостер', 'тостера', 'тостером',
    'соковыжималка', 'соковыжималки', 'соковыжималкой', 'кухонный комбайн', 'комбайна', 'комбайном',
    'хлебопечка', 'хлебопечки', 'хлебопечкой', 'йогуртница', 'йогуртницы', 'йогуртницей',
    'аэрогриль', 'аэрогриля', 'аэрогрилем', 'электросушилка', 'электросушилки', 'электросушилкой',
    'швейная машинка', 'швейной машинки', 'швейной машинкой', 'вентилятор', 'вентилятора', 'вентилятором',
    'обогреватель', 'обогревателя', 'обогревателем', 'тепловентилятор', 'тепловентилятора', 'тепловентилятором',
    'конвектор', 'конвектора', 'конвектором', 'электрочайник', 'электрочайника', 'электрочайником',
    'электрокамин', 'электрокамина', 'электрокамином', 'колонка bluetooth', 'наушники', 'умные часы',
    'фитнес-браслет', 'роутер', 'модем', 'пульт ду', 'пульта ду', 'внешний аккумулятор', 'флешка',
    'карта памяти', 'заправка картриджа', 'заправить картридж', 'припаять проводок', 'заменить вилку',
    'настроить wi-fi', 'восстановить данные с флешки', 'газовая плита', 'газовой плиты',
    'газовый котел', 'водонагреватель', 'теплый пол', 'домофон', 'система видеонаблюдения', 'автомагнитола'
]

@retry_sync(max_retries=10, delay=5, backoff=2, exceptions=(socket.gaierror, socket.timeout, ConnectionError))
def _connect_to_imap(server, email, password):
    socket.setdefaulttimeout(15)
    mail = imaplib.IMAP4_SSL(server)
    mail.login(email, password)
    mail.select("inbox")
    return mail

def _imap_task():
    mail = None
    try:
        mail = _connect_to_imap(IMAP_SERVER, EMAIL, PASSWORD)
        status, data = mail.search(None, 'UNSEEN')
        if status == "OK":
            for num in data[0].split():
                try:
                    typ, msg_data = mail.fetch(num, '(RFC822)')
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            sender = msg.get("From", "неизвестный отправитель")
                            subject = msg.get("Subject", "Без темы")
                            body = extract_body_text(msg)
                            if body:
                                body = re.sub(r'Отправлено из мобильной Почты Mail.*?-------- Пересылаемое сообщение --------', '', body, flags=re.DOTALL).strip()
                                is_zvonok = ("zvonok.com" in sender.lower() or "zvonok.com" in body.lower() or ("phone:" in body and "call_id:" in body))
                                is_site = (
                                    "proftech-service" in body.lower()
                                    or "заявка с сайта" in body.lower()
                                    or "proftech-service" in subject.lower()
                                    or "заявка с сайта" in subject.lower()
                                    or "web3forms" in sender.lower()
                                    or "web3forms" in body.lower()
                                )
                                if is_zvonok:
                                    parsed = parse_zvonok(body)
                                    if parsed is None:
                                        continue
                                    final_text, category_key = parsed
                                elif is_site:
                                    parsed = parse_site(body)
                                    if parsed is None:
                                        continue
                                    final_text, category_key = parsed
                                elif "bothelp.io" in sender.lower() or "bothelp.io" in body.lower():
                                    parsed = parse_bothelp(body)
                                    if parsed is None:
                                        continue
                                    final_text, category_key = parsed
                                elif "craftum.org" in sender.lower() or "craftum.org" in body.lower():
                                    parsed = parse_craftum(body)
                                    if parsed is None:
                                        continue
                                    final_text, category_key = parsed
                                else:
                                    continue
                                if any(term in final_text.lower() for term in EXCLUDED_TECH):
                                    continue
                                phone_match = re.search(r'📞 Телефон:\s*(.+)', final_text)
                                if not phone_match:
                                    phone_match = re.search(r'Телефон:\s*(\S+)', final_text)
                                if phone_match:
                                    raw_phone = phone_match.group(1).strip()
                                    digits = re.sub(r'\D', '', raw_phone)
                                    if len(digits) >= 10:
                                        now = datetime.now()
                                        if digits in recent_phones and (now - recent_phones[digits]) < timedelta(hours=1):
                                            continue
                                        recent_phones[digits] = now
                                try:
                                    asyncio.run_coroutine_threadsafe(send_and_dispatch(final_text, category_key), main_loop)
                                except Exception as e:
                                    logger.error(f"run_coroutine_threadsafe: {e}")
                    mail.store(num, '+FLAGS', '\\Seen')
                except Exception as e:
                    logger.error(f"Ошибка обработки письма {num}: {e}")
        mail.close()
    except Exception as e:
        logger.error(f"IMAP task error: {e}", exc_info=True)
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

async def check_mail_async():
    try:
        async with asyncio.timeout(90):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _imap_task)
    except asyncio.TimeoutError:
        logger.error("check_mail_async timeout (>90 sec)")
    except Exception as e:
        logger.error(f"check_mail_async error: {e}", exc_info=True)

async def poll_mail():
    while True:
        try:
            await check_mail_async()
        except Exception as e:
            logger.critical(f"poll_mail crashed: {e}", exc_info=True)
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(60)

# ---------- ЗАПУСК БОТА ----------
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).build()
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_general_message))
application.add_handler(CallbackQueryHandler(handle_callback))

async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    await application.initialize()
    await application.start()
    asyncio.create_task(poll_mail())
    asyncio.create_task(start_web_server())
    await application.updater.start_polling(
        poll_interval=0.5,
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query", "channel_post", "edited_channel_post"]
    )
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
