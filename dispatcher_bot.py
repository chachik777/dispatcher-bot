#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import re
import imaplib
import email
import logging
import random
import os
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters
from telegram.error import ChatMigrated, TimedOut, NetworkError, TelegramError

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ (только в stdout) ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- ДЕКОРАТОР RETRY ----------
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

# ---------- КОНФИГУРАЦИЯ (через переменные окружения) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8964018097:AAHiQfOwTnwWeVQWUog5vhihmk8lfcDLA74")
GENERAL_GROUP = int(os.getenv("GENERAL_GROUP", "-1003896694214"))

EMAIL = os.getenv("EMAIL", "dir72.pk@mail.ru")
PASSWORD = os.getenv("PASSWORD", "afStBLqMmNzQtZNkc0Mv")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.mail.ru")

# Группы можно оставить в коде, либо вынести в переменную, но пока оставим.
GROUPS = {
    "computers": [
        -1004355591778,   # Даня
        -1003395683617,   # Витя
        -1004445931308,   # Игорь
        -1003734200853,   # Денис
        -1003976268046    # Александр
    ],
    "appliances": [-1003975989333, -1003981596959],
    "refrigerators": [-1004352137129, -1004382888384],
    "cond": [-1004445931308, -1004486734839, -1004352137129],
    "tv": [-1004445931308],
    "orgtech": [-1004360815294],
    "other": [-1003896694214]
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
    text = re.sub(r'(?i)\b(здравствуйте|добрый день|добрый вечер|привет)\s*[,.]?\s*', '', text)
    text = re.sub(r'\b(алло|вас слышно|слышно|да|нет|ага|все верно|спасибо|слышу)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(сейчас|ну|так|вот|значит|это|там|тут|прям|как бы)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^[,\s]+', '', text)
    text = re.sub(r'^(по|про|насчет|касательно)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(хотел[аи]?\s+бы|хочу|надо|нужно|необходимо|планирую|собираюсь|принести|привезти|отвезти|отремонтировать|починить|исправить)\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[,.\s]+$', '', text)
    return text.strip()

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
        else: phone = raw_phone
    prob_match = re.search(r'problem:\s*(.+)', body)
    if prob_match: problem = prob_match.group(1).strip()
    return (
        "🛠 Новая заявка (BotHelp)!\n"
        f"👤 Имя: {name}\n📱 Категория: {problem}\n🏷 Марка: {brand}\n📞 Телефон: {phone}\n"
    )

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
            else: phone = digits
        else: phone = raw_phone
    service_match = re.search(r'Какая услуга вас интересует\?\s*\n?\s*([^\n]+)', body)
    if not service_match:
        service_match = re.search(r'Выберите ремонт какой техники Вас интересует\s*\n?\s*([^\n]+)', body)
    if service_match: service = service_match.group(1).strip()
    page_match = re.search(r'(https://[^\s]+)', body)
    if page_match: page = page_match.group(1).strip()
    return (
        "🛠 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n📱 Услуга: {service}\n📞 Телефон: {phone}\n🌐 Источник: {page}\n"
    )

# ---------- ПАРСИНГ ZVONOK (КОРРЕКТНЫЙ) ----------
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
            "🛠 Новая заявка (Zvonok)!\n"
            f"📞 Телефон: {phone}\n"
            f"⚠️ Разговор не распознан или прерван\n"
        )

    client_lines = re.findall(r"'client'.*?'text':\s*'([^']*)'", body)
    robot_lines = re.findall(r"'robot'.*?'text':\s*'([^']*)'", body)

    if not client_lines:
        return (
            "🛠 Новая заявка (Zvonok)!\n"
            f"📞 Телефон: {phone}\n"
            f"⚠️ Не удалось извлечь реплики клиента\n"
        )

    refusal_phrases = re.compile(r'(не ремонтируем|не занимаемся|только целиком|платы мы не ремонтируем)', re.IGNORECASE)
    if any(refusal_phrases.search(r) for r in robot_lines):
        return None

    warranty_phrases = re.compile(r'(гарантия|гарантийный случай|мастер уже был|по гарантии)', re.IGNORECASE)
    is_warranty = any(warranty_phrases.search(line) for line in client_lines + robot_lines)

    category = "не указано"
    brand = "не указано"
    problem = "не указано"
    address = "не указано"
    time = "не указано"

    all_text = ' '.join(client_lines + robot_lines)
    all_text_lower = all_text.lower()

    # Категория
    if re.search(r'\b(ноутбук|компьютер|моноблок|системный блок|системник)\b', all_text_lower):
        category = "компьютер"
    else:
        tech_keywords = {
            'холодильник': ['холодильник', 'морозильник', 'морозил'],
            'стиральная машина': ['стиральн', 'стиралк'],
            'посудомоечная машина': ['посудомоечн'],
            'плита': ['плит', 'варочн', 'духов'],
            'кондиционер': ['кондиционер', 'сплит'],
            'телевизор': ['телевизор', 'тв', 'плазма'],
            'оргтехника': ['принтер', 'мфу', 'сканер', 'копир', 'факс', 'плоттер'],
            'водонагреватель': ['водонагревател', 'бойлер'],
            'пылесос': ['пылесос'],
        }
        for cat, words in tech_keywords.items():
            if any(w in all_text_lower for w in words):
                category = cat
                break
        if category == "не указано":
            old_words = ['кондиционер', 'холодильник', 'стиральн', 'посудомоечн', 'плит', 'духов',
                         'ноутбук', 'компьютер', 'принтер', 'мфу', 'телефон', 'пылесос', 'телевизор',
                         'варочн', 'морозил', 'системный блок', 'системник', 'вытяжк', 'водонагревател']
            for w in old_words:
                if w in all_text_lower:
                    category = w
                    break

    # Марка
    brand_aliases = {
        'хаер': 'Haier', 'haier': 'Haier',
        'аристон': 'Ariston', 'ariston': 'Ariston',
        'bosch': 'Bosch', 'samsung': 'Samsung', 'lg': 'LG',
        'indesit': 'Indesit', 'whirlpool': 'Whirlpool', 'electrolux': 'Electrolux',
        'хисенс': 'Hisense', 'hisense': 'Hisense',
        'beko': 'Beko', 'zanussi': 'Zanussi', 'hotpoint': 'Hotpoint',
        'siemens': 'Siemens', 'miele': 'Miele', 'gorenje': 'Gorenje',
        'liebherr': 'Liebherr', 'sharp': 'Sharp', 'panasonic': 'Panasonic',
        'toshiba': 'Toshiba', 'hitachi': 'Hitachi', 'mitsubishi': 'Mitsubishi',
        'york': 'York', 'daewoo': 'Daewoo', 'hyundai': 'Hyundai',
        'vitek': 'Vitek', 'redmond': 'Redmond', 'tefal': 'Tefal',
        'асус': 'Asus', 'asus': 'Asus', 'acer': 'Acer', 'lenovo': 'Lenovo',
        'hp': 'HP', 'dell': 'Dell'
    }

    found_brand = None
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
            'grundig', 'loewe', 'bang & olufsen', 'аристон', 'ariston', 'hotpoint', 'саратов'
        ]
        text_for_brand = all_text_lower
        if category != "не указано":
            for word in category.split():
                if len(word) > 2:
                    text_for_brand = re.sub(r'\b' + re.escape(word) + r'\b', '', text_for_brand)
        text_for_brand = re.sub(r'\b(ремонт|заявка|машина|холодильник|плита|телевизор|кондиционер|ноутбук|компьютер|принтер)\b', '', text_for_brand)
        for b in known_brands:
            if b in text_for_brand:
                if b.lower() in brand_aliases:
                    brand = brand_aliases[b.lower()]
                else:
                    brand = b
                break

    if brand == "не указано":
        for line in client_lines:
            words = re.findall(r'\b[А-Яа-яA-Za-z]{2,10}\b', line)
            for w in words:
                if len(w) >= 2 and not w.lower() in ['это', 'мой', 'улица', 'дом', 'кп', 'снт', 'частный', 'вас', 'все']:
                    brand = w
                    break
            if brand != "не указано":
                break

    # Неисправность
    problem_pattern = re.compile(
        r'(не запускается|не работает|не включается|не греет|не холодит|не морозит|сломалась|сломался|неисправность|'
        r'моргает|шумит|течёт|не держит|не охлаждает|не реагирует|не открывается|не закрывается|не крутит|не сливает|'
        r'не нагревает|не показывает|нет изображения|нет звука|не заряжается|не печатает|залипает|глючит|зависает|'
        r'выдаёт ошибку|горит индикатор|мигает индикатор|проблема|поломка|сбой|не отжимает|не выключается|не включается|'
        r'не морозит|не холодит|плохо холодит|плохо морозит|течёт вода|вода не греется|вода не сливается|'
        r'не сушит|не вращается|не держит температуру|черный экран|цветные полоски|полосы на экране)',
        re.IGNORECASE
    )
    full_client_text = ' '.join(client_lines)
    if brand != "не указано":
        full_client_text = re.sub(re.escape(brand), '', full_client_text, flags=re.IGNORECASE)
    if category != "не указано":
        full_client_text = re.sub(re.escape(category), '', full_client_text, flags=re.IGNORECASE)

    match = problem_pattern.search(full_client_text)
    if not match:
        match = problem_pattern.search(all_text)

    if match:
        start = match.start()
        end_match = re.search(r'[.,!?]', all_text[start+len(match.group()):])
        if end_match:
            end = start + len(match.group()) + end_match.start()
        else:
            end = min(start + 100, len(all_text))
        problem = all_text[start:end].strip()
        if len(problem) > 120:
            problem = problem[:120] + '...'
    else:
        for line in client_lines:
            line_clean = clean_text(line)
            if (line_clean and len(line_clean.split()) >= 2 and
                not re.search(r'(улица|ул\.?|дом|кв\.?|\d{7,}|здравствуйте|привет|добрый|спасибо|алло|да|нет)', line_clean, re.IGNORECASE)):
                problem = line_clean
                break
        if problem == "не указано":
            problem = "не указана"

    # Адрес
    address_parts = []
    address_keywords = r'(улица|ул\.?|проезд|бульвар|переулок|шоссе|проспект|пр-т|тракт|дом|корпус|квартира|кв\.?|деревня|поселок|село|станция|микрорайон|мкр\.?|набережная|площадь|кп\s|снт\s|днп\s|частный дом|коттедж)'
    for line in client_lines:
        if re.search(address_keywords, line, re.IGNORECASE):
            clean_line = re.sub(r'[.,\s]+$', '', line.strip())
            address_parts.append(clean_line)

    if not address_parts:
        for line in reversed(robot_lines):
            if re.search(r'(адрес|улица|дом|кв\.?|кп|снт)', line, re.IGNORECASE):
                addr_match = re.search(r'(?:адрес\s*[:;]?\s*)(.+?)(?:[.,!?]|$)', line, re.IGNORECASE)
                if addr_match:
                    clean_addr = addr_match.group(1).strip()
                    address_parts.append(clean_addr)
                    break

    if address_parts:
        unique = []
        for p in address_parts:
            if p not in unique:
                unique.append(p)
        address_parts = unique

        has_house = any(re.search(r'дом\s*номер\s*\d+', p, re.IGNORECASE) for p in address_parts)
        if has_house:
            filtered = []
            for p in address_parts:
                if re.search(r'улица\s*(?:тоже\s*)?(?:номер|№)', p, re.IGNORECASE):
                    continue
                filtered.append(p)
            address_parts = filtered

        address_parts = [re.sub(r'\bастный\b', 'частный', p, flags=re.IGNORECASE) for p in address_parts]

        full_address = ' '.join(address_parts)
        full_address = re.sub(r'(дом\s*номер\s*\d+)\s*\1', r'\1', full_address, flags=re.IGNORECASE)
        full_address = re.sub(r'[.,]\s*[.,]', ',', full_address)
        full_address = re.sub(r'\s+', ' ', full_address).strip()
        full_address = re.sub(r'\.$', '', full_address)
        full_address = re.sub(r'\bдом\s*номер\s*(\d+)\b', r'дом \1', full_address, flags=re.IGNORECASE)
        full_address = re.sub(r'\bкп\s*', 'КП ', full_address, flags=re.IGNORECASE)
        address = full_address

    # Время
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

    message = (
        "🛠 Новая заявка (Zvonok)!\n"
        + ("⚠️ Гарантийный случай\n" if is_warranty else "") +
        f"📱 Категория: {category}\n"
        f"🏷 Марка: {brand}\n"
        f"⚙️ Неисправность: {problem}\n"
        f"📍 Адрес: {address}\n"
        f"📞 Телефон: {phone}\n"
        f"🕒 Время: {time}\n"
    )
    return message

# ---------- ЛОГИКА ДИСПЕТЧЕРА ----------
def detect_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["виндовс", "установить"]): return "computers"
    if any(w in t for w in ["принтер", "мфу", "сканер", "копир", "факс", "плоттер"]): return "orgtech"
    if any(w in t for w in ["ноутбук", "компьютер", "моноблок", "системный блок"]): return "computers"
    if "холодильник" in t or "морозильник" in t: return "refrigerators"
    if any(w in t for w in ["стиральн", "посудомоечн", "плит", "духов", "варочн", "водонагревател"]): return "appliances"
    if "кондиционер" in t: return "cond"
    if "телевизор" in t: return "tv"
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
    if "🛠 Новая заявка" not in text: return
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

async def dispatch_request(text):
    if "🛠 Новая заявка" not in text: return
    text = re.sub(r'^@\w+\s*', '', text)
    category = detect_category(text)
    if category == "orgtech":
        org_group = GROUPS["orgtech"][0]
        await bot.send_message(chat_id=org_group, text=text)
        return
    groups = GROUPS.get(category, GROUPS["other"])
    message_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    task = asyncio.create_task(start_timer(message_id))
    active_requests[message_id] = {
        "text": text,
        "current_group_index": 0,
        "groups": groups,
        "timer": task,
        "category": category
    }
    await send_request_with_buttons(groups[0], text, message_id)

async def send_and_dispatch(text):
    logger.info("send_and_dispatch вызван")
    try:
        await bot.send_message(chat_id=GENERAL_GROUP, text=text)
        logger.info("сообщение в общую группу отправлено успешно")
    except Exception as e:
        logger.error(f"отправка в общую группу не удалась: {e}")
        return
    await dispatch_request(text)

# ---------- ПРОВЕРКА ПОЧТЫ (С ТАЙМАУТАМИ) ----------
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

def _imap_task():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")
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
                                if is_zvonok:
                                    final_text = parse_zvonok(body)
                                    if final_text is None: continue
                                elif "bothelp.io" in sender.lower() or "bothelp.io" in body.lower():
                                    final_text = parse_bothelp(body)
                                elif "craftum.org" in sender.lower() or "craftum.org" in body.lower():
                                    final_text = parse_craftum(body)
                                else:
                                    final_text = f"📩 Письмо от {sender}\n📌 Тема: {subject}\n\n{body}"
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
                                    asyncio.run_coroutine_threadsafe(send_and_dispatch(final_text), main_loop)
                                except Exception as e:
                                    logger.error(f"run_coroutine_threadsafe: {e}")
                            else:
                                final_text = f"📩 Письмо от {sender}\n📌 Тема: {subject}\n⚠️ Текст не найден."
                                asyncio.run_coroutine_threadsafe(send_and_dispatch(final_text), main_loop)
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
        async with asyncio.timeout(30):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _imap_task)
    except asyncio.TimeoutError:
        logger.error("check_mail_async timeout (>30 sec)")
    except Exception as e:
        logger.error(f"check_mail_async error: {e}", exc_info=True)

async def poll_mail():
    while True:
        try:
            await check_mail_async()
        except Exception as e:
            logger.critical(f"poll_mail crashed: {e}", exc_info=True)
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(30)

# ---------- ЗАПУСК БОТА ----------
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_general_message))
application.add_handler(CallbackQueryHandler(handle_callback))

async def main():
    global main_loop
    main_loop = asyncio.get_event_loop()
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