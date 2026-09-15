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

# ---------- ИМПОРТ СПИСКА УЛИЦ ----------
try:
    from streets import KNOWN_STREETS
except ImportError:
    KNOWN_STREETS = [
        "50 лет Октября", "50 лет ВЛКСМ", "Московский тракт", "Ялуторовская", "Монтажников",
        "Новоселов", "Никольского", "Полевая", "Скандинавская", "Западно-Сибирская",
        "Фабричная", "Беляева", "Дружбы", "Миллераторов", "Мотостроителей",
        "Республики", "Советская", "Ленина", "Гагарина", "Широтная",
        "Сидора Путилова", "Путилова", "Сидорова", "Николая Зелинского"
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

GROUPS = {
    "computers": [
        -1004355591778,   # Даня
        -1003976268046,   # Александр
        -1003395683617,   # Витя
        -1004445931308,   # Игорь
        -1003734200853    # Денис
    ],
    "appliances": [
        -1003975989333,   # Евгений
        -1003981596959    # Александр Лобанов
    ],
    "refrigerators": [-1004352137129, -1004382888384],
    "cond": [-1004445931308, -1004486734839, -1004352137129],
    "tv": [-5402877244],   # Стас
    "orgtech": [-1004360815294],   # Эдик
    "phone": [-1004355591778],     # Даня
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
    text_clean = text.strip()
    text_clean = text_clean.rstrip('.,!?;:')
    text_clean = text_clean.strip()
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

def is_fake_address(text):
    text_lower = text.lower()
    fake_keywords = [
        'ждем вас', 'филиал', 'вход', 'крыльцо', 'этаж', 'поднимитесь', 'подняться',
        'принимаются', 'выезд не предусмотрен', 'марку', 'помните', 'назовите', 'продиктуйте',
        'микроволнов', 'принтер', 'ноутбук', 'компьютер', 'телевизор', 'холодильник',
        'стиральная', 'посудомоечная', 'кофемашина', 'пылесос', 'кондиционер',
        'мастер перезвонит', 'заявка', 'ремонт', 'диагностика',
        'автоответчик', 'не понял', 'с кем разговариваю',
        'год', 'по-моему', 'очередь', 'наверное', 'переехали', 'поставили',
        'ждем вас в филиале', 'корпус 1 дробь 1', 'зеленое крыльцо', 'со стороны широтной',
        'целый бок', 'можно', 'привезти', 'хо', 'дисе написано', 'сервис близко',
        'запишу номер', 'проверьте', 'подскажите', 'мастер приехал', 'выезд мастера', 'сегодня',
        'номер телефона', 'квартира', 'дом', 'улица',
        'на 2', 'течение 1', 'во сколько', 'подъедет', 'оформляла заявку', 'в четверг',
        'широтная 29', 'широтную 29', 'корпус 1', 'во 2', 'микроны во 2', '1 корпус', 'корпус 1 дробь 1',
        'широтной 29 корпус', 'широтной 29', 'м 225', 'стоит 3', '3 ?',
        'родной 29', 'каждые 10', 'До 9', 'с 9', 'Тут 2', 'Дубль', 'заречный проезд',
        'вот, я смотрю', 'сейчас возлесите', 'возлесите', 'лесобаза', 'лесобазе',
        'газовиков', 'заречная', 'заречном', 'мне ближе', 'пешком', 'типа до свидания',
        'всего доброго', 'дольше', 'привезу.', 'танца', '50 лет',
        'плейстейшн', 'плейстайшн', 'плюстшн', 'плюс сейшн', 'playstation', 'ps4', 'ps5'
    ]
    for kw in fake_keywords:
        if kw in text_lower:
            return True
    return False

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

def extract_address_smart(text):
    text = text.strip()
    if not text:
        return None

    def add_extra_parts(base, source_text):
        result = base
        korpus_match = re.search(r'корпус\s*(\d+[а-я]?)', source_text, re.IGNORECASE)
        if korpus_match:
            result += f", корпус {korpus_match.group(1)}"
        podezd_match = re.search(r'подъезд\s*(\d+)', source_text, re.IGNORECASE)
        if podezd_match:
            result += f", подъезд {podezd_match.group(1)}"
        etazh_match = re.search(r'этаж\s*(\d+)', source_text, re.IGNORECASE)
        if etazh_match:
            result += f", этаж {etazh_match.group(1)}"
        flat_match = re.search(r'(?:квартира|кв\.?)\s*(\d+[а-я]?)', source_text, re.IGNORECASE)
        if flat_match:
            result += f", квартира {flat_match.group(1)}"
        else:
            after_house = re.search(r'\b' + re.escape(result.split(',')[0].split()[-1]) + r'\s+(\d+)\b', source_text, re.IGNORECASE)
            if after_house:
                result += f", квартира {after_house.group(1)}"
        return result

    street = find_street_in_text(text)
    if street:
        pattern = re.compile(r'(?:' + re.escape(street) + r')\s*[,.:]?\s*(\d+[а-я]?)', re.IGNORECASE)
        match = pattern.search(text)
        if match:
            house = match.group(1)
            base = f"{street} {house}"
            return add_extra_parts(base, text)
        else:
            pattern2 = re.compile(r'(?:' + re.escape(street) + r')\s*[,.:]?\s*(\d+)', re.IGNORECASE)
            match2 = pattern2.search(text)
            if match2:
                base = f"{street} {match2.group(1)}"
                return add_extra_parts(base, text)

    if re.search(r'\b(?:улица|ул\.?)\b', text, re.IGNORECASE):
        m = re.search(r'\b(?:улица|ул\.?)\s*([А-Яа-я\-]+(?:\s+[А-Яа-я\-]+)*)\s*[,.]?\s*(?:дом\s*)?(\d+[а-я]?)?', text, re.IGNORECASE)
        if m:
            street_name = m.group(1).strip()
            house = m.group(2)
            if house:
                base = f"{street_name} {house}"
                return add_extra_parts(base, text)
            else:
                return street_name

    patterns_with_street = [
        r'(?:улица|ул\.?)\s*([А-Яа-я\-]+\s+\d+[а-я]?)',
        r'([А-Яа-я\-]+\s+\d+[а-я]?)\s*(?:корпус|кв\.?|квартира)',
        r'([А-Яа-я\-]+\s+\d+[а-я]?)\s*,\s*(?:квартира|кв\.?)\s*\d+',
        r'(?:дом|корпус)\s*(\d+[а-я]?)',
    ]
    for pattern in patterns_with_street:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            part = match.group(0).strip()
            if re.search(r'\b(playstation|плейстейшн|плейстайшн|плюстшн|плюс сейшн|ps4|ps5|xbox|nintendo)\b', part, re.IGNORECASE):
                continue
            if not re.search(r'\b(руб|тыс|цена|стоимость)\b', part, re.IGNORECASE):
                flat_match = re.search(r'(?:квартира|кв\.?)\s*(\d+[а-я]?)', text, re.IGNORECASE)
                if flat_match:
                    part += f", квартира {flat_match.group(1)}"
                return part

    simple_pattern = re.compile(r'\b([А-Яа-я]+\s+\d+[а-я]?)\b')
    match = simple_pattern.search(text)
    if match:
        part = match.group(0)
        if re.search(r'\b(playstation|плейстейшн|плейстайшн|плюстшн|плюс сейшн|ps4|ps5|xbox|nintendo)\b', part, re.IGNORECASE):
            return None
        if not re.search(r'\b(руб|тыс|цена|стоимость)\b', part, re.IGNORECASE):
            if not re.search(r'\b(ноутбук|компьютер|принтер|телевизор|кондиционер|холодильник|стиральн|посудомоечн|пылесос|кофемашин|микроволновк|колонка|приставка|телефон|планшет)\b', part, re.IGNORECASE):
                flat_match = re.search(r'(?:квартира|кв\.?)\s*(\d+[а-я]?)', text, re.IGNORECASE)
                if flat_match:
                    part += f", квартира {flat_match.group(1)}"
                return part

    flat_match = re.search(r'(?:квартира|кв\.?)\s*(\d+[а-я]?)', text, re.IGNORECASE)
    if flat_match:
        return f"квартира {flat_match.group(1)}"

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
        else: name = "не указано"
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
    """Парсер заявок с нашего сайта proftech-service (Web3Forms)."""
    name = "не указано"
    phone = "не указан"
    category_key = "other"
    category_display = "не указана"
    brand = "не указано"
    problem = "не указана"
    address = "не указано"
    time_wish = "не указано"

    # Проверяем, что это письмо действительно с нашего сайта
    if ("proftech-service" not in body.lower()
        and "заявка с сайта" not in body.lower()
        and "web3forms" not in body.lower()):
        return None

    # Web3Forms формат:
    #   "Name\nфывфыв\n\nPhone\n+7 (312) 312-31-23\n\nCategory\nТелевизор\n..."
    def extract_field(field_name):
        # Вариант 1: "Field\nvalue" (с любым количеством пустых строк между ними)
        pattern = re.compile(
            r'^\s*' + re.escape(field_name) + r'\s*:?\s*\n+\s*([^\n]+)',
            re.MULTILINE | re.IGNORECASE
        )
        match = pattern.search(body)
        if match:
            val = match.group(1).strip()
            if val and val.lower() != field_name.lower():
                return val
        # Вариант 2: "Field: value" (одной строкой)
        pattern2 = re.compile(
            r'^\s*' + re.escape(field_name) + r'\s*:\s*([^\n]+)',
            re.MULTILINE | re.IGNORECASE
        )
        match2 = pattern2.search(body)
        if match2:
            val = match2.group(1).strip()
            if val and val.lower() != field_name.lower():
                return val
        # Вариант 3: "Field value" через пробел
        pattern3 = re.compile(
            r'^\s*' + re.escape(field_name) + r'\s+([^\n]+)',
            re.MULTILINE | re.IGNORECASE
        )
        match3 = pattern3.search(body)
        if match3:
            val = match3.group(1).strip()
            if val and val.lower() != field_name.lower():
                return val
        return None

    # Имя (name)
    raw = extract_field('name') or extract_field('имя')
    if raw and len(raw) >= 2 and raw.lower() not in ('телефон', 'имя', 'не указано', ''):
        name = raw

    # Телефон (phone)
    raw_phone = extract_field('phone') or extract_field('телефон')
    if not raw_phone:
        logger.info(f"Сайт: номер телефона не найден. Тело письма (первые 500 символов): {body[:500]}")
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

    # Категория (category)
    raw_cat = extract_field('category') or extract_field('категория')
    if raw_cat and raw_cat.lower() not in ('не указана', ''):
        category_display = raw_cat
        raw_cat_low = raw_cat.lower()
        cat_map = [
            ('компьютер', 'computers'), ('ноутбук', 'computers'), ('моноблок', 'computers'), ('пк', 'computers'),
            ('стиральн', 'appliances'), ('посудомоечн', 'appliances'), ('плита', 'appliances'),
            ('духовой шкаф', 'appliances'), ('варочн', 'appliances'),
            ('холодильник', 'refrigerators'),
            ('кондиционер', 'cond'),
            ('телевизор', 'tv'),
            ('принтер', 'orgtech'), ('мфу', 'orgtech'), ('оргтехника', 'orgtech'),
            ('телефон', 'phone'), ('смартфон', 'phone'), ('планшет', 'phone'),
            ('пылесос', 'vacuum'),
            ('микроволновк', 'microwave'),
            ('кофемашин', 'coffee'),
            ('колонка', 'speaker'),
            ('приставка', 'console'),
        ]
        for key, val in cat_map:
            if key in raw_cat_low:
                category_key = val
                break

    # Марка (brand)
    raw = extract_field('brand') or extract_field('марка')
    if raw and raw.lower() not in ('не указано', 'не знаю', ''):
        brand = raw

    # Проблема (problem)
    raw = extract_field('problem') or extract_field('проблема')
    if raw:
        problem = raw

    # Адрес (address)
    raw = extract_field('address') or extract_field('адрес')
    if raw and raw.lower() not in ('не указано', ''):
        address = raw

    # Время (time)
    raw = extract_field('time') or extract_field('удобное время')
    if raw and raw.lower() not in ('не указано', ''):
        time_wish = raw

    message = (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"📋 Категория: {category_display}\n"
        f"🏣 Марка: {brand}\n"
        f"⚙️ Неисправность: {problem}\n"
        f"📭 Адрес: {address}\n"
        f"🕒 Время: {time_wish}\n"
    )
    return message, category_key

# ---------- ОСНОВНАЯ ФУНКЦИЯ ПАРСИНГА ZVONOK ----------
def parse_zvonok(body):
    # ---------- Извлечение телефона ----------
    phone = "не указан"
    header_phone_match = re.search(r'Телефон:\s*([+\d\s]+)', body)
    header_phone = header_phone_match.group(1).strip() if header_phone_match else None

    all_phones = re.findall(r'(\+?\d[\d\s\-]{5,})', body)
    last_transcript_phone = None
    if all_phones:
        for p in reversed(all_phones):
            digits = re.sub(r'\D', '', p)
            if len(digits) >= 10:
                last_transcript_phone = digits
                break

    if last_transcript_phone and len(last_transcript_phone) > 11:
        last_transcript_phone = None

    if last_transcript_phone:
        phone = last_transcript_phone
    else:
        phone = header_phone if header_phone else "не указан"
    if phone != "не указан":
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 11 and digits.startswith('8'):
            digits = '7' + digits[1:]
        elif len(digits) == 10 and digits.startswith('9'):
            digits = '7' + digits
        if len(digits) == 11 and digits.startswith('7'):
            phone = f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
        else:
            phone = digits

    if "{ct_transcribing}" in body or "Разговор:" not in body:
        return (
            "🚨 Новая заявка (Zvonok)!\n"
            f"📞 Телефон: {phone}\n"
            f"⚠️ Разговор не распознан или прерван\n"
        ), "other"

    client_lines = re.findall(r"'client'.*?'text':\s*'([^']*)'", body)
    robot_lines = re.findall(r"'robot'.*?'text':\s*'([^']*)'", body)

    if not client_lines and not robot_lines:
        talk_match = re.search(r'Разговор:\s*(.*?)(?:\n\n|$)', body, re.DOTALL)
        if talk_match:
            talk_text = talk_match.group(1)
            lines = talk_text.split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('Робот:'):
                    robot_lines.append(line.replace('Робот:', '').strip())
                elif line.startswith('Клиент:'):
                    client_lines.append(line.replace('Клиент:', '').strip())
                elif re.match(r'^Робот\s*:', line):
                    robot_lines.append(re.sub(r'^Робот\s*:', '', line).strip())
                elif re.match(r'^Клиент\s*:', line):
                    client_lines.append(re.sub(r'^Клиент\s*:', '', line).strip())
        if not client_lines and not robot_lines:
            robot_matches = re.findall(r'Робот\s*:\s*(.*?)(?=\s*(?:Робот|Клиент|$))', body, re.DOTALL)
            client_matches = re.findall(r'Клиент\s*:\s*(.*?)(?=\s*(?:Робот|Клиент|$))', body, re.DOTALL)
            robot_lines = [m.strip() for m in robot_matches]
            client_lines = [m.strip() for m in client_matches]

    if not client_lines:
        return (
            "🚨 Новая заявка (Zvonok)!\n"
            f"📞 Телефон: {phone}\n"
            f"⚠️ Не удалось извлечь реплики клиента\n"
        ), "other"

    # ---------- Проверка отказа ----------
    refusal_phrases = re.compile(
        r'(не ремонтируем|не занимаемся|только целиком|платы мы не ремонтируем|'
        r'не входит|не в наши услуги|слесарными работами|открутить|гайку|'
        r'не занимаемся такими работами|это не наш профиль|'
        r'мы не ремонтируем|не входит в нашу специализацию|мы не делаем|'
        r'к сожалению, не ремонтируем|не обслуживаем|'
        r'стройкой мы не занимаемся|сейчас мы не занимаемся|'
        r'настройкой не занимаемся|не сможем помочь|не подскажем|'
        r'только ремонт|ремонтом занимаемся|не ремонтируем такие|'
        r'не принимаем такие|не работаем с такими|'
        r'не чин)'
    )
    if any(refusal_phrases.search(r) for r in robot_lines):
        logger.info("Заявка отклонена (отказная фраза робота)")
        return None

    warranty_phrases = re.compile(r'(гарантия|гарантийный случай|мастер уже был|по гарантии)', re.IGNORECASE)
    is_warranty = any(warranty_phrases.search(line) for line in client_lines + robot_lines)

    # ---------- ИНИЦИАЛИЗАЦИЯ ----------
    category_key = "other"
    brand = "не указано"
    problem = "не указано"
    address = "не указано"
    time = "не указано"

    client_text = ' '.join(client_lines)
    client_text_lower = client_text.lower()
    all_text = client_text + ' ' + ' '.join(robot_lines)
    all_text_lower = all_text.lower()

    # ---------- 1. ОПРЕДЕЛЕНИЕ КАТЕГОРИИ ----------
    def detect_category_from_text(text_lower):
        if re.search(r'(игровая приставка|приставка|playstation|плейстейшн|плейстайшн|плюстшн|плюс сейшн|ps4|ps5|xbox|nintendo|джойстик|геймпад|игровая штука|игровая)', text_lower):
            return "console"
        elif re.search(r'(ноутбук|компьютер|моноблок|системный блок|системник|ноут|комп|монитор)', text_lower):
            return "computers"
        elif re.search(r'(принтер|мфу|сканер|копир|оргтехника|лазерджет|мфус|мпф|мфу-сканер|копировальный аппарат)', text_lower):
            return "orgtech"
        elif re.search(r'(кондиционер|сплит|сплит-система|чистка кондиционера|заправка кондиционера)', text_lower):
            return "cond"
        elif re.search(r'(холодильник|морозильник|морозил)', text_lower):
            return "refrigerators"
        elif re.search(r'(стиральн|посудомоечн|плит|духов|варочн|водонагревател(ь)?|духовой шкаф|прокладка|уплотнительн|резинк|манжет)', text_lower):
            return "appliances"
        elif re.search(r'(телевизор|(?<![\w])тв(?![\w])|плазма|телек)', text_lower):
            return "tv"
        elif re.search(r'(телефон|смартфон|айфон|iphone|андроид|мобильник|планшет|электронная книга|онор|honor|технопол|tecno|техно)', text_lower):
            return "phone"
        elif re.search(r'(пылесос|робот-пылесос|моющий пылесос)', text_lower):
            return "vacuum"
        elif re.search(r'(микроволновк|свч|микроволновая печь)', text_lower):
            return "microwave"
        elif re.search(r'(кофемашин|кофеварк|кофе-машин|кофейный аппарат)', text_lower):
            return "coffee"
        elif re.search(r'(колонка|умная колонка|алиса|маруся|sberbox|яндекс станция|яндекс колонка)', text_lower):
            return "speaker"
        return None

    category_key = detect_category_from_text(client_text_lower)
    if category_key is None:
        category_key = detect_category_from_text(all_text_lower)
    if category_key is None:
        category_key = "other"

    # ---------- 2. МАРКА ----------
    brand_aliases = {
        'хаер': 'Haier', 'haier': 'Haier',
        'хайр': 'Haier',
        'аристон': 'Ariston', 'ariston': 'Ariston',
        'bosch': 'Bosch', 'samsung': 'Samsung', 'lg': 'LG', 'элджи': 'LG', 'элжи': 'LG', 'эл джи': 'LG',
        'оджи': 'LG', 'олджи': 'LG',
        'indesit': 'Indesit', 'whirlpool': 'Whirlpool', 'electrolux': 'Electrolux',
        'электролюкс': 'Electrolux',
        'хисенс': 'Hisense', 'hisense': 'Hisense',
        'beko': 'Beko', 'беко': 'Beko', 'бэко': 'Beko',
        'zanussi': 'Zanussi', 'hotpoint': 'Hotpoint',
        'siemens': 'Siemens', 'miele': 'Miele', 'gorenje': 'Gorenje',
        'горение': 'Gorenje',
        'liebherr': 'Liebherr', 'sharp': 'Sharp', 'panasonic': 'Panasonic',
        'toshiba': 'Toshiba', 'hitachi': 'Hitachi', 'mitsubishi': 'Mitsubishi',
        'мицубиши': 'Mitsubishi', 'митсубиши': 'Mitsubishi', 'не судишийтих': 'Mitsubishi',
        'york': 'York', 'daewoo': 'Daewoo', 'hyundai': 'Hyundai',
        'хундай': 'Hyundai',
        'vitek': 'Vitek', 'redmond': 'Redmond', 'tefal': 'Tefal',
        'асус': 'Asus', 'asus': 'Asus', 'acer': 'Acer', 'lenovo': 'Lenovo',
        'hp': 'HP', 'эйчпи': 'HP', 'хп': 'HP', 'dell': 'Dell',
        'хонор': 'Honor', 'honor': 'Honor', 'онор': 'Honor',
        'аско': 'Asko', 'asko': 'Asko',
        'индезит': 'Indesit', 'индивидит': 'Indesit',
        'макбук': 'MacBook', 'macbook': 'MacBook',
        'люкс': 'Lux', 'lux': 'Lux',
        'пропус': 'Prolux', 'prolux': 'Prolux',
        'эпсон': 'Epson', 'epson': 'Epson',
        'грюндик': 'Grundig', 'grundig': 'Grundig', 'грандиг': 'Grundig',
        'асустуф': 'Asus TUF', 'асус туф': 'Asus TUF',
        'тошиба': 'Toshiba', 'тощи': 'Toshiba',
        'дриме': 'Dreame', 'dreame': 'Dreame',
        'дект': 'Daikin', 'daikin': 'Daikin',
        'заной': 'Zanussi', 'zanussi': 'Zanussi',
        'катана': 'MSI Katana', 'msi': 'MSI',
        'сиоми': 'Xiaomi', 'xiaomi': 'Xiaomi',
        'бирбса': 'Biryusa', 'biryusa': 'Biryusa',
        'кенди': 'Candy', 'candy': 'Candy',
        'асины': 'Asus',
        'лазерджет': 'HP LaserJet', 'лазарджет': 'HP LaserJet',
        'дольше': "De'Longhi", 'делонги': "De'Longhi",
        'самсун': 'Samsung', 'хун': 'Hyundai',
        'танца': 'Toshiba',
        'днс': 'DNS', 'dns': 'DNS',
        'технопол': 'Tecno', 'tecno': 'Tecno', 'техно': 'Tecno',
        'киви': 'KIVI', 'kivi': 'KIVI',
        'бош': 'Bosch',
        'вестель': 'Vestel', 'вестел': 'Vestel', 'vestel': 'Vestel',
        'плейстейшн': 'PlayStation', 'плейстайшн': 'PlayStation',
        'плюстшн': 'PlayStation', 'плюс сейшн': 'PlayStation',
        'playstation': 'PlayStation', 'ps4': 'PlayStation 4', 'ps5': 'PlayStation 5',
        'xbox': 'Xbox', 'nintendo': 'Nintendo',
    }

    found_brand = None
    for line in client_lines:
        line_lower = line.lower()
        for alias, canonical in brand_aliases.items():
            if alias in line_lower:
                found_brand = canonical
                break
        if found_brand:
            break

    client_says_unknown_brand = re.search(r'(самостоятельно собранн|самодельн|не помню|не знаю|марку не помню|марку не знаю|свой сбор)', client_text_lower)
    if not found_brand and not client_says_unknown_brand:
        for alias, canonical in brand_aliases.items():
            if alias in all_text_lower:
                found_brand = canonical
                break

    if found_brand:
        brand = found_brand
    else:
        known_brands = [
            'lg', 'samsung', 'bosch', 'indesit', 'whirlpool', 'electrolux', 'haier', 'sharp',
            'panasonic', 'tcl', 'lenovo', 'hp', 'canon', 'epson', 'xerox', 'kyocera', 'brother',
            'ricoh', 'dell', 'acer', 'asus', 'msi', 'gigabyte', 'huawei', 'xiaomi', 'meizu',
            'sony', 'philips', 'thomson', 'jvc', 'akai', 'york', 'mitsubishi', 'toshiba',
            'sanyo', 'hitachi', 'fujitsu', 'nec', 'siemens', 'aeg', 'zanussi', 'ardes',
            'candy', 'hoover', 'beko', 'vitek', 'redmond', 'tefal', 'moulinex', 'kitchenaid',
            'smeg', 'de\'longhi', 'gorenje', 'liebherr', 'kaiser', 'miele', 'ariete',
            'clatronic', 'exq', 'gaggia', 'saeco', 'krups', 'nespresso', 'dolce gusto',
            'bork', 'kiv', 'midea', 'hisense', 'хисенс', 'hyundai', 'daewoo', 'rowenta',
            'grundig', 'loewe', 'bang & olufsen', 'аристон', 'ariston', 'hotpoint', 'саратов',
            'honor', 'онор', 'tecno', 'технопол', 'техно', 'kivi', 'киви', 'бош', 'элджи', 'элжи', 'эл джи',
            'оджи', 'олджи', 'беко', 'бэко', 'вестель', 'вестел', 'vestel'
        ]
        text_for_brand = client_text_lower if client_text_lower.strip() else all_text_lower
        if category_key != "other":
            for word in CATEGORY_NAMES.get(category_key, '').split():
                if len(word) > 2:
                    text_for_brand = re.sub(r'\b' + re.escape(word) + r'\b', '', text_for_brand)
        text_for_brand = re.sub(r'\b(ремонт|заявка|машина|холодильник|плита|телевизор|кондиционер|ноутбук|компьютер|принтер|неисправность|поломка|сломался|сломалась|не работает|не включается|шумит|течёт|морозит|холодит|греет|отжимает|сливает|крутит|сушит|вращается|держится|охлаждает)\b', '', text_for_brand)

        for b in known_brands:
            if b in text_for_brand:
                if b.lower() in brand_aliases:
                    brand = brand_aliases[b.lower()]
                else:
                    brand = b
                break

    # ---------- 3. НЕИСПРАВНОСТЬ ----------
    problem_pattern = re.compile(
        r'(не запускается|не работает|не включается|не греет|не холодит|не морозит|сломалась|сломался|неисправность|'
        r'моргает|шумит|течёт|не держит|не охлаждает|не реагирует|не открывается|не закрывается|не крутит|не сливает|'
        r'не нагревает|не показывает|нет изображения|нет звука|не заряжается|не печатает|залипает|глючит|зависает|'
        r'выдаёт ошибку|горит индикатор|мигает индикатор|мигает|индикатор|проблема|поломка|сбой|не отжимает|не выключается|не включается|'
        r'не морозит|не холодит|плохо холодит|плохо морозит|течёт вода|вода не греется|вода не сливается|'
        r'не сушит|не вращается|не держит температуру|черный экран|цветные полоски|полосы на экране|'
        r'перелив|не дает пену|нет пены|пена не образуется|замена экрана|разбит экран|треснул экран|'
        r'прошивка|переустановка|переустановить|перешить|настроить|компрессор|не охлаждает|не морозит|не холодит|'
        r'не захватывает|захватывает бумагу|не подает бумагу|зажевывает бумагу|не печатает|полосы при печати|'
        r'модуль|почистить|чистка|замена стика|замена подшипника|замена клавиатуры|протечка|слабо охлаждает|'
        r'перегорела|не работает кнопка|не реагирует|не видит сеть|не ходит|не капает|нет холода|печатает|'
        r'не подаёт воду|не подает воду|нет воды|не моет|экран не показывает|заправка|чистка|не холодит|'
        r'провод|блок питания|оторвался|питание|провод от блока питания|оторвался провод|не работает компьютер|'
        r'проблемы с блоком питания|не включается компьютер|сломался телевизор|не работает телевизор|'
        r'холодильник не работает|не морозит холодильник|шумит компьютер|вентилятор|подшипник|'
        r'барабан|прокладка|резинка|уплотнитель|он не включается|не включается|сломалась игровая|'
        r'ошибка|картридж|плохо печатает|цветная печать|плохо печатает цветным|печатает полосами|'
        r'разъем|сетевой кабель|входное отверстие|антенное гнездо|не закрывается|не держит заряд|'
        r'перегорел|не выводится изображение|изображение пропало|звук есть|нет изображения|'
        r'обновлен|обновление|обновляется|потух|погас|потух экран|экран потух|починил|починить|чинил|чинить|'
        r'заряжа|перестал(а|о|и)?\s+(работать|заряжаться|включаться|греть|морозить|охлаждать|заряжать)|'
        r'дверь|открывается|сама открывается|перестала держать|дверь перестала|'
        r'установк[аи]|установить|windows|виндовс|драйвер|программ|'
        r'замена стекла|'
        r'застря(л|ла|ло|ли|ть|вш)|застревание'
        r')',
        re.IGNORECASE
    )

    problem_candidates = []
    for line in client_lines:
        line_clean = line.replace('?', ' ')
        if is_price_question(line_clean):
            continue
        if not is_meaningful_issue(line_clean):
            continue
        cleaned = re.sub(r'\b(вот так вот|по зачем|конечно|просто|типа|так|ну|это|там|тут|прям|как бы|значит)\b', '', line_clean, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned and problem_pattern.search(cleaned):
            problem_candidates.append(cleaned)

    final_problem = None
    if problem_candidates:
        for line in problem_candidates:
            if problem_pattern.search(line):
                final_problem = line
                break

    # ---------- 4. АДРЕС ----------
    full_client_text = client_text
    full_client_text = re.sub(r'\b(здравствуйте|алло|до свидания|спасибо|пожалуйста|да|нет|ага|угу|ок|хорошо|всего доброго)\b', '', full_client_text, flags=re.IGNORECASE)
    full_client_text = re.sub(r'\s+', ' ', full_client_text).strip()

    extracted_addr = extract_address_smart(full_client_text)

    if extracted_addr:
        address = extracted_addr
    else:
        if re.search(r'(ждем вас|принимаем только в филиале|филиал|находимся на|ждем вас на|привозите к нам|в филиал)', all_text_lower):
            address = "филиал (привоз)"
        else:
            address = "не указано"

    filial_categories = ["vacuum", "microwave", "coffee", "phone", "speaker", "console"]
    if category_key in filial_categories and address != "не указано" and "широтная" in address.lower():
        address = "филиал (привоз)"

    if not final_problem and address == "не указано":
        logger.info("Нет ни проблемы, ни адреса – заявка отклонена")
        return None

    if final_problem:
        problem = final_problem
        problem = problem.strip()
        if len(problem) > 120:
            problem = problem[:120] + '...'
    else:
        problem = "не указана"

    # ---------- 5. ВРЕМЯ ----------
    time = "не указано"
    time_patterns = [
        r'(с\s*(\d{1,2})\s*(?:до|по)\s*(\d{1,2})\s*(?:часов?|ч\.?))',
        r'(в\s*(\d{1,2})\s*(?:часов?|ч\.?))',
        r'(после\s*(\d{1,2})\s*(?:часов?|ч\.?))',
        r'(утром|днём|вечером|ночью|сегодня|завтра|послезавтра)',
        r'(\d{1,2}\s*[:-]\s*\d{2})',
        r'(в\s*(\d{1,2})\s*(?:часов?|ч\.?)\s*(?:утра|дня|вечера))',
    ]
    for line in client_lines:
        line_lower = line.lower()
        for pattern in time_patterns:
            m = re.search(pattern, line_lower)
            if m:
                time = m.group(0).strip()
                break
        if time != "не указано":
            break
    if time == "не указано":
        for line in robot_lines:
            line_lower = line.lower()
            if re.search(r'(время|выезд|приедет|подъедет)', line_lower):
                for pattern in time_patterns:
                    m = re.search(pattern, line_lower)
                    if m:
                        time = m.group(0).strip()
                        break
                if time != "не указано":
                    break

    category_display = CATEGORY_NAMES.get(category_key, "другое")
    message = (
        "🚨 Новая заявка (Zvonok)!\n"
        + ("⚠️ Гарантийный случай\n" if is_warranty else "") +
        f"📋 Категория: {category_display}\n"
        f"🏣 Марка: {brand}\n"
        f"⚙️ Неисправность: {problem}\n"
        f"📭 Адрес: {address}\n"
        f"📞 Телефон: {phone}\n"
        f"🕒 Время: {time}\n"
    )
    return message, category_key

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ ----------
def detect_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["виндовс", "установить", "ноутбук", "компьютер", "моноблок", "системный блок", "монитор"]):
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
                                        logger.info("Заявка с сайта без телефона, пропускаем")
                                        continue
                                    final_text, category_key = parsed
                                elif "bothelp.io" in sender.lower() or "bothelp.io" in body.lower():
                                    parsed = parse_bothelp(body)
                                    if parsed is None:
                                        logger.info("Заявка BotHelp без телефона, пропускаем")
                                        continue
                                    final_text, category_key = parsed
                                elif "craftum.org" in sender.lower() or "craftum.org" in body.lower():
                                    parsed = parse_craftum(body)
                                    if parsed is None:
                                        logger.info("Заявка Craftum без телефона, пропускаем")
                                        continue
                                    final_text, category_key = parsed
                                else:
                                    logger.info(f"Письмо от {sender} не относится к Zvonok/BotHelp/Craftum/Сайт, пропускаем")
                                    continue
                                logger.info(f"DEBUG: final_text = {final_text[:150]}")
                                if any(term in final_text.lower() for term in EXCLUDED_TECH):
                                    logger.info("заявка отклонена (исключённая техника)")
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
                                            logger.info("заявка отклонена (дубликат телефона)")
                                            continue
                                        recent_phones[digits] = now
                                logger.info("вызываю asyncio.run_coroutine_threadsafe...")
                                try:
                                    asyncio.run_coroutine_threadsafe(send_and_dispatch(final_text, category_key), main_loop)
                                except Exception as e:
                                    logger.error(f"run_coroutine_threadsafe: {e}")
                            else:
                                logger.info("Письмо без тела, пропускаем")
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
    await application.updater.start_polling(poll_interval=0.5, drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
