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
from difflib import SequenceMatcher
from html.parser import HTMLParser
from email.header import decode_header, make_header
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters
from telegram.error import ChatMigrated, TimedOut, NetworkError, TelegramError

from aiohttp import web

# ---------- ИМПОРТ СПИСКА УЛИЦ ----------
try:
    from streets import KNOWN_STREETS, AMBIGUOUS_STREETS
except ImportError:
    try:
        from streets import KNOWN_STREETS
    except ImportError:
        KNOWN_STREETS = [
            "50 лет Октября", "50 лет ВЛКСМ", "30 лет Победы", "Московский тракт",
            "Ялуторовская", "Монтажников", "Новоселов", "Никольского", "Полевая",
            "Скандинавская", "Западно-Сибирская", "Фабричная", "Беляева", "Дружбы",
            "Мелиораторов", "Моторостроителей", "Республики", "Советская", "Ленина",
            "Гагарина", "Широтная", "Пермякова", "Мельникайте", "Федюнинского",
            "Сидора Путилова", "Николая Зелинского", "Практическая",
            "Андрея Кореневского", "Арктическая"
        ]
        logging.warning("Файл streets.py не найден, используется базовый список улиц.")
    AMBIGUOUS_STREETS = {
        "новая", "старая", "северная", "южная", "восточная", "западная",
        "центральная", "главная", "светлая", "солнечная", "лесная", "полевая",
        "садовая", "школьная", "больничная", "спортивная", "строительная",
        "заводская", "промышленная", "техническая", "парковая", "водная",
        "дружбы", "мира"
    }

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
BOT_TOKEN = os.getenv("BOT_TOKEN")
GENERAL_GROUP = int(os.getenv("GENERAL_GROUP", "-1003896694214"))

EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.mail.ru")
try:
    DUPLICATE_PHONE_TTL = timedelta(minutes=max(1, int(os.getenv("DUPLICATE_PHONE_TTL_MINUTES", "60"))))
except ValueError:
    DUPLICATE_PHONE_TTL = timedelta(hours=1)

# ЖЕСТКО ЗАДАЕМ ПОРТ 80 ДЛЯ AMVERA
WEBHOOK_PORT = 80

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

# Категории, которые принимаются только в филиале (привоз)
FILIAL_CATEGORIES = {"vacuum", "microwave", "coffee", "speaker", "console", "phone"}

active_requests = {}
accepted_requests = {}
recent_phones = {}
main_loop = None

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
class HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'blockquote', 'section', 'article', 'header', 'footer'
    }
    _LINE_BREAK_TAGS = {'br', 'hr'}
    _SKIP_TAGS = {'script', 'style', 'head', 'title'}

    def __init__(self):
        super().__init__()
        self.result = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            if t in self._LINE_BREAK_TAGS or t in self._BLOCK_TAGS:
                self.result.append('\n')
            elif t in ('td', 'th'):
                self.result.append(' ')

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and t in self._BLOCK_TAGS:
            self.result.append('\n')

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            cleaned = re.sub(r'[ \t\r\n]+', ' ', data)
            self.result.append(cleaned)

    def get_text(self):
        raw = ''.join(self.result)
        raw = re.sub(r'[ \t]*\n[ \t]*', '\n', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        raw = re.sub(r'[ \t]{2,}', ' ', raw)
        return raw.strip()

def html_to_text(html_str):
    parser = HTMLTextExtractor()
    parser.feed(html_str)
    parser.close()
    return parser.get_text()

def _decode_email_payload(part):
    payload_bytes = part.get_payload(decode=True)
    if payload_bytes is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    candidates = []
    declared_charset = part.get_content_charset()
    if declared_charset:
        candidates.append(declared_charset)
    for enc in ('utf-8', 'cp1251', 'windows-1251', 'koi8-r'):
        if enc not in [c.lower() for c in candidates]:
            candidates.append(enc)
    for enc in candidates:
        try:
            return payload_bytes.decode(enc, errors='strict')
        except (UnicodeDecodeError, LookupError):
            continue
    return payload_bytes.decode('utf-8', errors='replace')

def _decode_mime_header(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)

def extract_body_text(msg):
    try:
        if msg.is_multipart():
            plain_parts = []
            html_parts = []
            for part in msg.walk():
                disp = str(part.get("Content-Disposition", "")).lower()
                if disp.startswith("attachment"):
                    continue
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    txt = _decode_email_payload(part)
                    if txt and txt.strip():
                        plain_parts.append(txt)
                elif ctype == "text/html":
                    html = _decode_email_payload(part)
                    if html and html.strip():
                        txt = html_to_text(html)
                        if txt and txt.strip():
                            html_parts.append(txt)
            if plain_parts:
                return '\n'.join(plain_parts)
            if html_parts:
                return '\n'.join(html_parts)
        else:
            payload = _decode_email_payload(msg)
            ctype = msg.get_content_type()
            if ctype == "text/plain":
                return payload
            elif ctype == "text/html":
                return html_to_text(payload)
            return payload
    except Exception as e:
        logger.error(f"extract_body_text error: {e}")
    return None

def normalize_yo(text: str) -> str:
    return text.replace('ё', 'е').replace('Ё', 'Е')

_LEADING_GREETING_RE = re.compile(
    r'^(?:алло+|здравствуй(?:те)?|добр(?:ый|ого)\s+(?:день|вечер|утро)|привет|извините|простите)[,.\s!?]*',
    re.IGNORECASE
)
_CONNECTION_CHECK_RE = re.compile(
    r'\b(?:вы\s+меня\s+слышите|меня\s+слышно|вас\s+слышно|хорошо\s+слышно|плохо\s+слышно|я\s+вас\s+слышу|да\s+слышу|алло+\s+алло+)\b[,.\s!?]*',
    re.IGNORECASE
)
_LEADING_FILLER_RE = re.compile(
    r'^(?:'
    r'(?:ну|вот|так|значит|короче(?:\s+говоря)?|смотрите|слушайте|понимаете|знаете|как\s+бы|'
    r'прям|вообще|просто|типа|собственно|в\s+общем|дело\s+в\s+том\s*,?\s*что|'
    r'у\s+меня\s+такой\s+вопрос|хотел[аи]?\s+бы\s+(?:узнать|спросить|уточнить)|'
    r'подскажите(?:\s+пожалуйста)?|скажите(?:\s+пожалуйста)?|'
    r'у\s+меня|у\s+нас|тут|там|сейчас|по\s+поводу|насчет|касательно)\b[,.\s]*|'
    r'(?:да|ага|угу|ок|окей|ладно|хорошо|нет)\s*,+\s*|'
    r'(?:да|ага|угу)\s+(?=у\s+меня|у\s+нас|тут|вот|это|сейчас)'
    r')',
    re.IGNORECASE
)
_LEADING_REQUEST_WRAPPER_RE = re.compile(
    r'^(?:хотел[аи]?\s+бы|хочу|надо|нужно|необходимо|можно|помогите|прошу)\s+'
    r'(?:вызвать\s+мастера(?:\s+на\s+дом)?|оформить\s+заявку|сдать\s+в\s+ремонт)[,:\s]*',
    re.IGNORECASE
)
_TRAILING_POLITE_RE = re.compile(
    r'[,.\s!?]*(?:спасибо(?:\s+большое)?|пожалуйста|до\s+свидания|всего\s+(?:доброго|хорошего)|подскажите|скажите)[,.\s!?]*$',
    re.IGNORECASE
)

def clean_text(text):
    if not text:
        return ""
    res = re.sub(r'\s+', ' ', text).strip()
    res = _CONNECTION_CHECK_RE.sub(' ', res)
    # Итеративно снимаем только начальные приветствия, вводные слова и прощания в конце,
    # НЕ трогая смысловые отрицания вроде "нет звука", "нет воды" в середине фразы.
    for _ in range(8):
        prev = res
        res = _LEADING_GREETING_RE.sub('', res).strip()
        res = _LEADING_FILLER_RE.sub('', res).strip()
        res = _LEADING_REQUEST_WRAPPER_RE.sub('', res).strip()
        res = _TRAILING_POLITE_RE.sub('', res).strip()
        res = re.sub(r'^[,.\s:;!?\-]+', '', res).strip()
        if res == prev:
            break
    res = re.sub(r'[,.\s;:!?]+$', '', res)
    res = re.sub(r'\s{2,}', ' ', res)
    return res.strip()

_PRICE_CLAUSE_RE = re.compile(
    r'(?:[,.;!?]*\s*(?:и\s+|а\s+)?(?:подскажите|скажите|уточните|интересует)?\s*,?\s*'
    r'(?:сколько\s+(?:примерно\s+|ориентировочно\s+)?(?:это\s+)?(?:будет\s+|может\s+)?стои[тть](?:\s+(?:выезд|диагностика|ремонт|работа|все\s+это))?|'
    r'какая\s+(?:примерно\s+)?(?:цена|стоимость)(?:\s+(?:выезда|диагностики|ремонта|работы))?|'
    r'по\s+цене\s+сориентируйте|сколь\s+стоит|почем(?:\s+ремонт|\s+выезд)?)'
    r'(?:\s+(?:ремонт|диагностика|выезд|работа))?[^.!?]*)',
    re.IGNORECASE
)
_PRICE_AMOUNT_RE = re.compile(
    r'(?:(?:от|до|около|примерно|в\s+районе|за)\s+)?\d[\d\s]*(?:рубл\w*|руб\.?|тысяч\w*|тыс\.?|₽)\b',
    re.IGNORECASE
)
_TIME_CLAUSE_RE = re.compile(
    r'(?:[,.;!?]*\s*(?:и\s+|а\s+)?'
    r'(?:когда|во\s+сколько|в\s+какое\s+время|как\s+скоро)\s+'
    r'(?:мастер|специалист|он|вы)?\s*(?:сможет|может|приедет|подъедет|будет|освободится)?[^.!?]*)',
    re.IGNORECASE
)
_SCHEDULING_ONLY_RE = re.compile(
    r'^(?:сегодня|завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)|'
    r'утром|вечером|днем|после\s+\d+|до\s+\d+|ожидаю|жду|все\s+верно|да-да|ага-ага|угу-угу|оформлял[аи]?|записали)[,.\s!?]*$',
    re.IGNORECASE
)

def strip_price_and_time_tails(text: str) -> str:
    if not text:
        return ""
    res = text
    # Если вопрос начинается с "Сколько стоит починить телевизор", убираем только "Сколько стоит", сохраняя суть!
    res = re.sub(
        r'^\s*(?:подскажите|скажите)?\s*,?\s*сколько\s+(?:примерно\s+)?(?:будет\s+)?стои[тть]\s+(?=(?:починить|отремонтировать|заменить|замена|ремонт|установка|настройка|чистка)\b)',
        '',
        res,
        flags=re.IGNORECASE
    )
    res = _PRICE_CLAUSE_RE.sub('', res)
    res = _PRICE_AMOUNT_RE.sub('', res)
    res = _TIME_CLAUSE_RE.sub('', res)
    res = re.sub(r'\s+', ' ', res).strip(' ,.;:!?-')
    return res

def is_price_question(text):
    norm = normalize_yo(text.lower())
    return bool(re.search(
        r'(\bцена\b|\bстоимость\b|\bсколько\s+(?:будет\s+)?стои[тть]\b|\bрубл[яей]\b|\bтысяч[аи]?\b|\bруб\.?\b|\bтыс\.?\b|₽|полторы\s+тысячи|\bот\s+\d+|\d+\s*(?:рубл|руб\.|тыс))',
        norm
    ))

def is_meaningful_issue(text):
    text_clean = text.strip().rstrip('.,!?;:').strip()
    norm = normalize_yo(text_clean.lower())
    if len(norm) < 3:
        return False
    if norm in {
        'да', 'нет', 'ага', 'угу', 'ок', 'окей', 'хорошо', 'спасибо', 'до свидания', 'н', 'д',
        'привет', 'здравствуйте', 'алло', 'слышно', 'понял', 'поняла', 'ясно', 'так', 'ну',
        'вот', 'это', 'там', 'тут', 'прям', 'как бы', 'просто', 'типа', 'конечно', 'ладно',
        'добрый день', 'добрый вечер', 'всего хорошего', 'всего доброго', 'все верно', 'верно',
        'правильно', 'подтверждаю', 'ожидаю', 'жду', 'выезд', 'выезд мастера', 'мастера', 'цена',
        'стоимость', 'нрзб', 'неразборчиво'
    }:
        return False
    if _SCHEDULING_ONLY_RE.match(norm):
        return False
    if re.match(r'^[\s.,!?]+$', text_clean):
        return False
    if re.match(r'^[\d\s\-\(\)]+$', text_clean):
        return False
    return True

def find_street_in_text(text):
    text_lower = normalize_yo(text.lower())
    for street in KNOWN_STREETS:
        street_norm = normalize_yo(street.lower())
        if street_norm in text_lower:
            return street
        street_clean = re.sub(r'[ьъ]', '', street_norm)
        text_clean = re.sub(r'[ьъ]', '', text_lower)
        if street_clean in text_clean:
            return street
    return None

def format_phone(raw_phone: str) -> str:
    raw = (raw_phone or "").strip()
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('7'):
        return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return raw or "не указан"

# ---------- ПАРСЕРЫ ПИСЕМ ----------
def parse_bothelp(body):
    name = brand = "не указано"
    problem = "не указана"
    name_match = re.search(r'Имя:\s*(.+)', body)
    if name_match:
        name = name_match.group(1).strip()
    brand_match = re.search(r'device_brand:\s*(.+)', body)
    if brand_match:
        brand = brand_match.group(1).strip()
    phone_match = re.search(r'phone1:\s*(\+?\d[\d\s\(\)\-]+)', body)
    if not phone_match:
        logger.info("BotHelp: номер телефона не найден, заявка будет пропущена")
        return None
    phone = format_phone(phone_match.group(1))
    prob_match = re.search(r'problem:\s*(.+)', body)
    if prob_match:
        problem = prob_match.group(1).strip()
    cat_source = f"{problem} {'' if brand == 'не указано' else brand}".strip()
    category_key = detect_category(cat_source)
    return (
        "🚨 Новая заявка (BotHelp)!\n"
        f"👤 Имя: {name}\n📋 Категория: {problem}\n🏣 Марка: {brand}\n📞 Телефон: {phone}\n"
    ), category_key

def parse_craftum(body):
    name = "не указано"
    service = "не указана"
    page = "не указана"
    name_match = re.search(r'Имя\s*\n?\s*([^\n]*)', body)
    if name_match:
        raw_name = name_match.group(1).strip()
        if raw_name and raw_name not in ('Телефон', 'Номер телефона'):
            name = raw_name
    phone_match = re.search(r'(?:Телефон|Номер телефона)\s*\n?\s*(\+?\d[\d\s\(\)\-]+)', body)
    if not phone_match:
        logger.info("Craftum: номер телефона не найден, заявка будет пропущена")
        return None
    phone = format_phone(phone_match.group(1))
    service_match = re.search(r'Какая услуга вас интересует\?\s*\n?\s*([^\n]+)', body)
    if not service_match:
        service_match = re.search(r'Выберите ремонт какой техники Вас интересует\s*\n?\s*([^\n]+)', body)
    if service_match:
        service = service_match.group(1).strip()
    page_match = re.search(r'(https://[^\s]+)', body)
    if page_match:
        page = page_match.group(1).strip()
    category_key = detect_category(service if service != "не указана" else "")
    return (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n📋 Услуга: {service}\n📞 Телефон: {phone}\n🌐 Источник: {page}\n"
    ), category_key

def parse_site(body):
    """Парсер заявок с сайта (старый формат из Web3Forms)."""
    name = "не указано"
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
    phone = format_phone(raw_phone)

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
        if category_key == "other":
            category_key = detect_category(raw_problem)

    message = (
        "🚨 Новая заявка (Сайт)!\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"📋 Категория: {category_display}\n"
        f"⚙️ Неисправность: {problem_display}\n"
        f"⏰ Удобное время: {time_display}\n"
    )
    return message, category_key


# ---------- ХЕЛПЕРЫ ДЛЯ ZVONOK ----------
ADDRESS_STOP_WORDS = {
    'десят', 'кв', 'квартира', 'подъезд', 'этаж', 'дом', 'корпус', 'строение', 'стр',
    'ул', 'улица', 'адрес', 'адресу', 'номер', 'телефон', 'мастер', 'робот', 'клиент',
    'смарт', 'телевизор', 'телевизора', 'телевизору', 'холодильник', 'холодильника',
    'холодильнику', 'шкаф', 'бош', 'bosch', 'трера', 'трара', 'крера', 'не',
    'он', 'она', 'оно', 'они', 'это', 'вот', 'ну', 'да', 'нет', 'привет', 'здравствуйте',
    'диагностика', 'ремонт', 'подключить', 'установить', 'стиральная', 'стиралка',
    'стиральной', 'машина', 'машинка', 'машинке', 'машине', 'посудомоечная', 'посудомойка',
    'микроволновка', 'кофемашина', 'пылесос', 'кондиционер', 'принтер', 'ноутбук',
    'ноутбука', 'ноутбуку', 'компьютер', 'компьютера', 'планшет', 'смартфон', 'телефона',
    'телефону', 'колонка', 'приставка', 'плойка', 'плойки', 'водонагреватель', 'водонагревателя',
    'бойлер', 'бойлера',
    # Предлоги, союзы, наречия
    'на', 'по', 'в', 'во', 'из', 'до', 'от', 'за', 'у', 'к', 'ко', 'с', 'со', 'о', 'об',
    'про', 'около', 'рядом', 'возле', 'через', 'между', 'после', 'перед', 'при', 'без',
    'уже', 'еще', 'там', 'тут', 'здесь', 'где', 'когда', 'потом', 'сейчас', 'теперь',
    'сегодня', 'вчера', 'завтра', 'послезавтра', 'примерно', 'больше', 'меньше', 'где-то',
    'будет', 'было', 'есть', 'стоит', 'цена', 'стоимость', 'приедет', 'подъедет', 'приду',
    'буду', 'осталось', 'работает', 'работал', 'работала', 'сломался', 'сломалась',
    'купили', 'брали', 'выдает', 'показывает', 'озвучили', 'сказали', 'хочу', 'надо', 'нужно',
    # Время, возраст, деньги, единицы измерения
    'год', 'года', 'лет', 'месяц', 'месяца', 'месяцев', 'неделя', 'неделю', 'недели', 'недель',
    'день', 'дня', 'дней', 'час', 'часа', 'часов', 'минута', 'минут', 'минуты', 'минуту',
    'секунд', 'секунды', 'раз', 'раза', 'вечера', 'утра', 'ночи', 'днем', 'назад',
    'рубль', 'рубля', 'рублей', 'руб', 'тысяча', 'тысячи', 'тысяч', 'тыс', 'копеек',
    'градус', 'градуса', 'градусов', 'процент', 'процента', 'процентов',
    'литр', 'литра', 'литров', 'килограмм', 'кг', 'дюйм', 'дюйма', 'дюймов', 'диагональ',
    'вольт', 'ватт', 'ампер', 'герц', 'оборотов', 'гигабайт', 'терабайт',
    # Технические термины, модели, коды
    'ошибка', 'ошибки', 'код', 'серия', 'модель', 'версия', 'канал', 'программа', 'режим',
    'кнопка', 'кнопки', 'порт', 'слот', 'блок', 'камера', 'камеры', 'поколение',
    'айфон', 'iphone', 'galaxy', 'галакси', 'redmi', 'редми', 'windows', 'виндовс',
    'android', 'андроид', 'playstation', 'плейстейшн', 'xbox', 'dyson', 'дайсон',
    'самсунг', 'samsung', 'индезит', 'indesit', 'атлант', 'lg', 'элджи', 'аристон'
}

AFTER_NUMBER_STOP_WORDS = {
    'лет', 'год', 'года', 'месяц', 'месяца', 'месяцев', 'неделю', 'недели', 'недель',
    'день', 'дня', 'дней', 'час', 'часа', 'часов', 'минут', 'минуты', 'минуту', 'секунд',
    'раз', 'раза', 'вечера', 'утра', 'ночи', 'днем', 'назад',
    'руб', 'рубл', 'рубль', 'рубля', 'рублей', 'тыс', 'тысяч', 'тысячи', 'тысяча',
    'градус', 'градуса', 'градусов', 'процент', 'процента', 'процентов',
    'литр', 'литра', 'литров', 'кг', 'килограмм', 'дюйм', 'дюйма', 'дюймов',
    'вольт', 'ватт', 'ампер', 'герц', 'оборотов', 'камерный', 'гб', 'тб', 'мб'
}

_UNIT_ADDRESS_WORDS = {
    'подъезд', 'подъезда', 'подъезде', 'этаж', 'этажа', 'этаже',
    'квартира', 'квартиры', 'квартире', 'кв', 'корпус', 'корп', 'строение', 'стр'
}

_PREPOSITION_PREFIX_RE = re.compile(
    r'^(?:на|по|в|во|из|до|от|за|у|к|ко|с|со|около|рядом|возле|адрес|адресу|улица|ул\.?)\s+',
    re.IGNORECASE
)

_NUM_UNITS = {
    'ноль': 0, 'нуль': 0,
    'один': 1, 'одна': 1, 'одно': 1, 'первый': 1, 'первая': 1, 'первое': 1, 'первом': 1, 'первого': 1,
    'два': 2, 'две': 2, 'второй': 2, 'вторая': 2, 'второе': 2, 'втором': 2, 'второго': 2,
    'три': 3, 'третий': 3, 'третья': 3, 'третье': 3, 'третьем': 3, 'третьего': 3,
    'четыре': 4, 'четвертый': 4, 'четвертая': 4, 'четвертом': 4, 'четвертого': 4,
    'пять': 5, 'пятый': 5, 'пятая': 5, 'пятом': 5, 'пятого': 5,
    'шесть': 6, 'шестой': 6, 'шестая': 6, 'шестом': 6, 'шестого': 6,
    'семь': 7, 'седьмой': 7, 'седьмая': 7, 'седьмом': 7, 'седьмого': 7,
    'восемь': 8, 'восьмой': 8, 'восьмая': 8, 'восьмом': 8, 'восьмого': 8,
    'девять': 9, 'девятый': 9, 'девятая': 9, 'девятом': 9, 'девятого': 9,
}
_NUM_TEENS = {
    'десять': 10, 'десятый': 10, 'десятая': 10, 'десятом': 10,
    'одиннадцать': 11, 'одиннадцатый': 11, 'двенадцать': 12, 'двенадцатый': 12,
    'тринадцать': 13, 'тринадцатый': 13, 'четырнадцать': 14, 'четырнадцатый': 14,
    'пятнадцать': 15, 'пятнадцатый': 15, 'шестнадцать': 16, 'шестнадцатый': 16,
    'семнадцать': 17, 'семнадцатый': 17, 'восемнадцать': 18, 'восемнадцатый': 18,
    'девятнадцать': 19, 'девятнадцатый': 19,
}
_NUM_TENS = {
    'двадцать': 20, 'двадцатый': 20, 'тридцать': 30, 'тридцатый': 30,
    'сорок': 40, 'сороковой': 40, 'пятьдесят': 50, 'пятидесятый': 50,
    'шестьдесят': 60, 'шестидесятый': 60, 'семьдесят': 70, 'семидесятый': 70,
    'восемьдесят': 80, 'восьмидесятый': 80, 'девяносто': 90, 'девяностый': 90,
}
_NUM_HUNDREDS = {
    'сто': 100, 'сотый': 100, 'двести': 200, 'триста': 300, 'четыреста': 400,
    'пятьсот': 500, 'шестьсот': 600, 'семьсот': 700, 'восемьсот': 800, 'девятьсот': 900,
}
_ALL_NUM_WORDS = {**_NUM_UNITS, **_NUM_TEENS, **_NUM_TENS, **_NUM_HUNDREDS}

def _parse_russian_number_phrase(phrase: str):
    """Преобразует русскую словесную запись числа (0..999) в строку с цифрами, напр. 'двадцать пять' -> '25'."""
    tokens = normalize_yo(phrase.lower()).split()
    total = 0
    matched_any = False
    consumed = 0
    # Порядок разрядов: сотня -> десяток -> единица (или сотня -> 10..19). Не склеиваем числа через знак препинания или повтор разряда!
    seen_hundred = False
    seen_ten = False
    seen_teen = False
    seen_unit = False
    for tok in tokens:
        clean_tok = tok.strip('.,!?;:')
        has_break_after = tok != clean_tok and any(c in tok for c in ',.!?;:')
        if clean_tok in _NUM_HUNDREDS:
            if seen_hundred or seen_ten or seen_teen or seen_unit:
                break
            total += _NUM_HUNDREDS[clean_tok]
            seen_hundred = True
        elif clean_tok in _NUM_TENS:
            if seen_ten or seen_teen or seen_unit:
                break
            total += _NUM_TENS[clean_tok]
            seen_ten = True
        elif clean_tok in _NUM_TEENS:
            if seen_ten or seen_teen or seen_unit:
                break
            total += _NUM_TEENS[clean_tok]
            seen_teen = True
        elif clean_tok in _NUM_UNITS:
            if seen_teen or seen_unit:
                break
            total += _NUM_UNITS[clean_tok]
            seen_unit = True
        else:
            break
        matched_any = True
        consumed += 1
        if has_break_after:
            break
    if not matched_any:
        return None, 0
    return str(total), consumed

def _convert_spoken_numbers_in_address_chunk(text: str) -> str:
    """Локально заменяет словесные числа в адресном фрагменте (поддерживает 'дробь', корпуса, буквы)."""
    tokens = text.split()
    out = []
    i = 0
    while i < len(tokens):
        clean_tok = normalize_yo(tokens[i].lower()).strip('.,!?;:')
        if clean_tok in _ALL_NUM_WORDS:
            val, consumed = _parse_russian_number_phrase(' '.join(tokens[i:i + 4]))
            if val is not None and consumed > 0:
                out.append(val)
                i += consumed
                continue
        if clean_tok == 'дробь' and out and out[-1].isdigit():
            out[-1] = out[-1] + '/'
            i += 1
            continue
        out.append(tokens[i])
        i += 1
    res = ' '.join(out)
    res = re.sub(r'(\d+)/\s+(\d+)', r'\1/\2', res)
    return res

def _street_to_regex(street: str) -> re.Pattern:
    words = normalize_yo(street.strip()).split()
    parts = []
    for w in words:
        w_low = w.lower()
        if w_low == '50':
            parts.append(r'(?:50|пятьдесят|пятидесяти)')
        elif w_low == '30':
            parts.append(r'(?:30|тридцать|тридцати)')
        elif w_low == 'тракт':
            parts.append(r'тракт(?:а|у|е|ом)?')
        elif w_low == 'проезд':
            parts.append(r'проезд(?:а|у|е|ом)?')
        elif w_low == 'переулок':
            parts.append(r'(?:переулок|переулка|переулку|переулке|переулком)')
        elif len(w_low) >= 5 and w_low.endswith('ая'):
            parts.append(re.escape(w_low[:-2]) + r'(?:ая|ой|ую|ою)')
        elif len(w_low) >= 5 and w_low.endswith('яя'):
            parts.append(re.escape(w_low[:-2]) + r'(?:яя|ей|юю|ею)')
        elif len(w_low) >= 5 and w_low.endswith(('ский', 'цкий', 'ний', 'ный', 'ой', 'ый', 'ий')):
            parts.append(re.escape(w_low[:-2]) + r'(?:ий|ый|ой|ого|его|ому|ему|им|ым|ом|ем)')
        elif len(w_low) >= 6 and w_low.endswith(('ого', 'его')):
            parts.append(re.escape(w_low[:-3]) + r'(?:ого|его|ому|ему|им|ым|ом|ем|ий|ый)')
        elif len(w_low) >= 6 and w_low.endswith(('ова', 'ева', 'ина')):
            parts.append(re.escape(w_low[:-1]) + r'[ао]')
        else:
            parts.append(re.escape(w_low))
    pattern = r'(?<!\w)' + r'\s+'.join(parts) + r'(?!\w)'
    return re.compile(pattern, re.IGNORECASE)

_KNOWN_STREET_PATTERNS = []
_seen_street_keys = set()
for _st in KNOWN_STREETS:
    _key = normalize_yo(_st.strip().lower())
    if _key and _key not in _seen_street_keys:
        _seen_street_keys.add(_key)
        _KNOWN_STREET_PATTERNS.append((_st, _street_to_regex(_st)))

_AMBIGUOUS_STREETS_LOWER = {normalize_yo(s.strip().lower()) for s in AMBIGUOUS_STREETS}

def _ru_phonetic_stem(word: str) -> str:
    w = normalize_yo(word.lower().strip(' ,.!?;:-"\'()'))
    w = w.replace('й', 'и').replace('ь', '').replace('ъ', '')
    w = re.sub(r'([а-я])\1+', r'\1', w)
    if len(w) >= 6:
        w = re.sub(r'(?:ая|ой|ую|ою|яя|ей|юю|ею|ого|его|ому|ему|ий|ый|ым|им|ом|ем|ов|ев|ин|[аоеуыи])$', '', w)
    return w

_PHONETIC_FIRST_GROUPS = [
    {'ш', 'щ'}, {'ф', 'в'}, {'о', 'а'}, {'е', 'и'}, {'г', 'к'}, {'з', 'с'}
]

def _first_chars_compatible(c1: str, c2: str) -> bool:
    if c1 == c2:
        return True
    return any(c1 in g and c2 in g for g in _PHONETIC_FIRST_GROUPS)

_FUZZY_STREET_INDEX = []
for _st, _ in _KNOWN_STREET_PATTERNS:
    _st_norm = normalize_yo(_st.lower())
    if _st_norm in _AMBIGUOUS_STREETS_LOWER or len(_st_norm) < 5:
        continue
    _words = _st_norm.split()
    _stems = [_ru_phonetic_stem(w) for w in _words if len(w) >= 4]
    if _stems:
        _FUZZY_STREET_INDEX.append((_st, _st_norm, ' '.join(_stems), _stems[-1]))

_UNSTICK_PREPOSITIONS_RE = re.compile(
    r'\b(на|по|в|к)(улиц[аеуы]?|широтн\w*|пермяков\w*|мельникайт\w*|республик\w*|'
    r'монтажников\w*|федюнинск\w*|червишевск\w*|московск\w*|зелинск\w*|менделеев\w*|'
    r'гнаровск\w*|логунов\w*|ростовцев\w*|ленин[аеу]?|гагарин\w*|олимпийск\w*)\b',
    re.IGNORECASE
)

def _unstick_asr_address_text(text: str) -> str:
    if not text:
        return ""
    res = normalize_yo(text)
    res = re.sub(r'\bвас[а-я]{1,3}(широтн\w*)', r'вас \1', res, flags=re.IGNORECASE)
    res = _UNSTICK_PREPOSITIONS_RE.sub(r'\1 \2', res)
    res = re.sub(r'\b(дом|корпус|подъезд|этаж|квартира)(\d+)', r'\1 \2', res, flags=re.IGNORECASE)
    return res

def _strip_street_prepositions(name: str) -> str:
    res = name.strip(' ,.')
    for _ in range(3):
        prev = res
        res = _PREPOSITION_PREFIX_RE.sub('', res).strip(' ,.')
        if res == prev:
            break
    return res

def is_valid_street_name(name):
    name = _strip_street_prepositions(name)
    if not name or len(name) < 3:
        return False
    words = normalize_yo(name.lower()).split()
    if any(w in ADDRESS_STOP_WORDS for w in words):
        return False
    if re.fullmatch(r'[\d\s\-\(\)]+', name):
        return False
    return True

def _is_non_street_domain_word(phrase: str) -> bool:
    norm = normalize_yo(phrase.lower())
    if not is_valid_street_name(norm):
        return True
    if SYMPTOM_KEYWORDS.search(norm) or GENERAL_REPAIR_KEYWORDS.search(norm):
        return True
    if _CLIENT_DEVICE_HINT_RE.search(norm) or _BRAND_HINT_RE.search(norm):
        return True
    return False

def _fuzzy_match_street(candidate: str, allow_ambiguous: bool = False):
    clean = _strip_street_prepositions(normalize_yo(candidate))
    if not clean or len(clean) < 5 or _is_non_street_domain_word(clean):
        return None
    words = clean.split()
    if not words:
        return None
    # Если кандидат многословный, проверяем как фразу целиком, так и главное слово (фамилию/название)
    cand_full_stem = ' '.join(_ru_phonetic_stem(w) for w in words if len(w) >= 3)
    cand_last_stem = _ru_phonetic_stem(words[-1])
    if len(cand_last_stem) < 4:
        return None

    best_street = None
    best_score = 0.0
    for canonical, st_norm, st_full_stem, st_last_stem in _FUZZY_STREET_INDEX:
        if not allow_ambiguous and st_norm in _AMBIGUOUS_STREETS_LOWER:
            continue
        if len(words) == 1:
            if not _first_chars_compatible(cand_last_stem[0], st_last_stem[0]):
                continue
            score = SequenceMatcher(None, cand_last_stem, st_last_stem).ratio()
        else:
            if not _first_chars_compatible(cand_full_stem[0], st_full_stem[0]):
                continue
            score = SequenceMatcher(None, cand_full_stem, st_full_stem).ratio()
        if score >= 0.82 and score > best_score:
            best_score = score
            best_street = canonical
    return best_street

_ADDRESS_MARKER_BEFORE_RE = re.compile(
    r'\b(?:улиц[аеуы]?|ул\.?|проезд\w*|переул\w*|бульвар\w*|проспект\w*|'
    r'мкр\.?|микрорайон\w*|район\w*|посел\w*|жк|адрес\w*(?:\s*:)?|'
    r'живу\s+(?:на|в|по)|нахожусь\s+(?:на|в|по)|выезд\s+(?:на|по)|дом\s+(?:на|по))\s*(?:на\s+|по\s+|в\s+)?$',
    re.IGNORECASE
)
_AMBIGUOUS_OBJECT_FOLLOW_RE = re.compile(
    r'^\s*(?:машин\w*|стирал\w*|посудомой\w*|плит\w*|холодиль\w*|духов\w*|'
    r'сторон\w*|част\w*|стен\w*|проблем\w*|причин\w*|поломк\w*|модел\w*|верси\w*|'
    r'плат\w*|кнопк\w*|вод\w*|трубк\w*|помп\w*|полос\w*|подсветк\w*|техник\w*|жизн\w*|точка)\b',
    re.IGNORECASE
)
_AMBIGUOUS_OBJECT_BEFORE_RE = re.compile(
    r'\b(?:машинка|стиралка|посудомойка|плита|холодильник|духовка|сторона|проблема|техника|помпа|вода)\s+$',
    re.IGNORECASE
)

def _is_ambiguous_street_confirmed(text: str, match: re.Match, in_address_prompt: bool = False) -> bool:
    before = text[max(0, match.start() - 36):match.start()]
    after = text[match.end():match.end() + 40]
    if _AMBIGUOUS_OBJECT_FOLLOW_RE.match(after):
        return False
    if _AMBIGUOUS_OBJECT_BEFORE_RE.search(before) and not _ADDRESS_MARKER_BEFORE_RE.search(before):
        return False
    if _ADDRESS_MARKER_BEFORE_RE.search(before):
        return True
    after_conv = _convert_spoken_numbers_in_address_chunk(after)
    hm = re.match(
        r'^\s*[,.]?\s*(?:это\s+|у\s+нас\s+)?(?:дом\s+|д\.?\s+|номер\s+|№\s*)?(\d+(?:/\d+)?[а-я]?)\b',
        after_conv, re.IGNORECASE
    )
    if hm and not _has_after_number_stop_word(after_conv, hm.end(1)):
        return True
    if in_address_prompt:
        return True
    return False

def _has_after_number_stop_word(text: str, num_end_pos: int) -> bool:
    tail = normalize_yo(text[num_end_pos:]).lstrip(' ,.-')
    m = re.match(r'^([a-zа-яё]+)(.*)', tail, re.IGNORECASE)
    if not m:
        return False
    word = m.group(1).lower()
    if word in AFTER_NUMBER_STOP_WORDS:
        return True
    if word in _UNIT_ADDRESS_WORDS:
        after_word = m.group(2)
        if re.match(r'^\.?\s+\d', after_word):
            return False
        return True
    return False

def find_streets_in_lines(lines, address_prompt_indices=None):
    prompt_set = set(address_prompt_indices or ())
    found = []
    for idx, raw_line in enumerate(lines):
        text = _unstick_asr_address_text(raw_line)
        in_prompt = idx in prompt_set

        # 1. Точный поиск по справочнику (с защитой омонимичных улиц из AMBIGUOUS_STREETS)
        for canonical, pat in _KNOWN_STREET_PATTERNS:
            canon_key = normalize_yo(canonical.lower())
            for m in pat.finditer(text):
                if canon_key in _AMBIGUOUS_STREETS_LOWER:
                    if not _is_ambiguous_street_confirmed(text, m, in_address_prompt=in_prompt):
                        continue
                found.append(canonical)
                break

        # 2. Явный маркер "улица/проезд/переулок <Название>" + fuzzy-восстановление опечаток ASR
        for m in re.finditer(r'\b(?:улиц[аеуы]?|ул\.?|проезд|переулок|пр-д|пер\.?)\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)', text, re.IGNORECASE):
            raw_cand = re.sub(r'\s+\b(?:дом|д|корпус|корп|квартира|кв|подъезд|под|этаж|эт|строение|стр)\b.*$', '', m.group(1), flags=re.IGNORECASE)
            raw_cand = _strip_street_prepositions(raw_cand)
            fuzzy = _fuzzy_match_street(raw_cand, allow_ambiguous=True)
            if fuzzy:
                found.append(fuzzy)
            elif is_valid_street_name(raw_cand) and not _is_non_street_domain_word(raw_cand):
                found.append(raw_cand)

        # 3. Нечёткий поиск после "по/на <Слово>" в прямых ответах об адресе или перед номером дома
        if in_prompt:
            cleaned_ans = _strip_street_prepositions(re.sub(r'\b(?:дом|д\.?|квартира|кв\.?)\b.*$', '', text, flags=re.IGNORECASE))
            for cand in (cleaned_ans, ' '.join(cleaned_ans.split()[:2])):
                fuzzy = _fuzzy_match_street(cand, allow_ambiguous=True)
                if fuzzy:
                    found.append(fuzzy)

        # 4. Паттерн "<Улица> <НомерДома>"
        for m in re.finditer(r'\b([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)\s+(\d+(?:/\d+)?[а-я]?)\b', text):
            if _has_after_number_stop_word(text, m.end(2)):
                continue
            cand = _strip_street_prepositions(m.group(1))
            fuzzy = _fuzzy_match_street(cand)
            if fuzzy:
                found.append(fuzzy)
            elif is_valid_street_name(cand) and not _is_non_street_domain_word(cand):
                cand_low = normalize_yo(cand.lower())
                if cand_low in _AMBIGUOUS_STREETS_LOWER and _AMBIGUOUS_OBJECT_BEFORE_RE.search(text[:m.start()]):
                    continue
                found.append(cand)

    seen = set()
    unique = []
    for s in found:
        key = normalize_yo(s.lower())
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique

ROBOT_OWN_ADDRESS_RE = re.compile(
    r'(?:мы\s+находим(?:ся)?|наш\s+(?:адрес|филиал|сервис|офис)|'
    r'привозите\s+(?:к\s+нам|в\s+филиал)|ждем\s+вас|в\s+филиале\s+на|'
    r'филиал(?:е)?\s+на\s+[ш]*широтн|зеленое\s+крыльцо|рядом\s+со\s+стоматологией|'
    r'вход\s+со\s+стороны\s+[ш]*широт|будн[ие]+\s+(?:с|от)\s+\d+|'
    r'работаем\s+с|режим\s+работы|корпус\s+1\s+дробь\s+1)',
    re.IGNORECASE
)
ROBOT_ADDRESS_CONFIRM_RE = re.compile(
    r'\b(?:проверьте|записал[аи]?|зафиксировал[аи]?|итак|ваш\s+адрес|адрес\s*(?:для\s+выезда)?\s*[:,]|улица|квартира|дом\b)\b',
    re.IGNORECASE
)
_ADDRESS_REJECTION_RE = re.compile(
    r'\b(?:(?:нет\s*,?\s*)?(?:это\s+)?(?:неправильн\w*|неверн\w*|не\s+(?:тот|наш|мой)\s+адрес|другой\s+адрес|ошибк\w*)|'
    r'адрес\s+(?:неправильн\w*|неверн\w*|не\s+тот)|не\s+называл\s+адрес)\b|^\s*нет[.,!\s]*$',
    re.IGNORECASE
)

def _classify_robot_turn(text: str) -> set:
    norm = normalize_yo(text.lower())
    intents = set()
    if re.search(r'\b(?:что\s+(?:именно\s+)?(?:случилось|сломалось|не\s+работает)|какая\s+(?:проблема|неисправность)|в\s+чем\s+(?:проблема|дело)|опишите)\b', norm):
        intents.add("ask_problem")
    if re.search(r'\b(?:какая\s+(?:техника|бытовая\s+техника)|какое\s+устройство|что\s+(?:за|у\s+вас\s+за)\s+(?:техника|прибор|устройство))\b', norm):
        intents.add("ask_device")
    if re.search(r'\b(?:марк[ауео]|бренд\w*|модел[ьи]|фирм[ауе]|производител\w*|название\s+(?:техники|консоли|устройства))\b', norm):
        intents.add("ask_brand_model")
    if re.search(r'\b(?:какой\s+адрес|назовите\s+(?:ваш\s+)?адрес|по\s+какому\s+адресу|какая\s+улиц\w*|на\s+какой\s+улиц\w*|где\s+(?:вы\s+)?находитесь|куда\s+(?:приехать|подъехать|направить\s+мастера|выехать))\b', norm):
        intents.add("ask_address")
    if re.search(r'\b(?:какой\s+дом|номер\s+дома)\b', norm):
        intents.add("ask_house")
    if re.search(r'\b(?:какая\s+квартир\w*|номер\s+квартир\w*|уточните[^.!?]{0,20}квартир\w*)\b', norm):
        intents.add("ask_flat")
    if re.search(r'\b(?:какой\s+подъезд|номер\s+подъезда)\b', norm):
        intents.add("ask_entrance")
    if re.search(r'\b(?:какой\s+этаж|на\s+каком\s+этаже)\b', norm):
        intents.add("ask_floor")
    if re.search(r'\b(?:какой\s+корпус|какое\s+строение)\b', norm):
        intents.add("ask_corpus")
    if ROBOT_ADDRESS_CONFIRM_RE.search(norm) and not ROBOT_OWN_ADDRESS_RE.search(norm) and not re.search(r'[ш]*широтн\w*[,.\s]+29\b', norm):
        intents.add("confirm_address")
    if ROBOT_OWN_ADDRESS_RE.search(norm) or re.search(r'\bпринимаем\s+только\s+в\s+(?:нашем\s+)?филиале\b', norm):
        intents.add("branch_invite")
    if re.search(r'\b(?:проверьте|давайте\s+проверим|подведем\s+итог|ваша\s+заявка)\b', norm):
        intents.add("summary")
    if re.search(r'\b(?:оформляем\s+заявку|подтверждаете|передаю\s+заявку)\b', norm):
        intents.add("ask_confirm_lead")
    return intents

def _analyze_zvonok_dialog(turns):
    ctx = {
        "safe_robot_lines": [],
        "address_client_lines": [],
        "address_prompt_client_indices": set(),
        "rejected_streets": set(),
        "model_turn_texts": set(),
        "confirmed_robot_cat_lines": [],
        "ask_problem_responses": set(),
        "branch_confirmed": False,
    }
    if not turns:
        return ctx

    # Определяем индексы клиентских строк, исключая реплики с моделью/отказом от адреса при разборе адреса
    for idx, t in enumerate(turns):
        prev_t = turns[idx - 1] if idx > 0 else None
        nxt_t = turns[idx + 1] if idx + 1 < len(turns) else None
        prev_intents = _classify_robot_turn(prev_t["text"]) if (prev_t and prev_t["role"] == "robot") else set()

        if t["role"] == "robot":
            r_norm = normalize_yo(t["text"])
            r_intents = _classify_robot_turn(t["text"])
            is_own_branch = bool(
                ROBOT_OWN_ADDRESS_RE.search(r_norm)
                or re.search(r'[ш]*широтн\w*[,.\s]+29\b', r_norm, re.IGNORECASE)
            )
            rejected_by_client = bool(
                nxt_t and nxt_t["role"] == "client"
                and _ADDRESS_REJECTION_RE.search(normalize_yo(nxt_t["text"].strip()))
            )
            if rejected_by_client:
                for st in find_streets_in_lines([t["text"]]):
                    ctx["rejected_streets"].add(normalize_yo(st.lower()))
            if not is_own_branch and not rejected_by_client and ROBOT_ADDRESS_CONFIRM_RE.search(r_norm):
                ctx["safe_robot_lines"].append(t["text"])

            if "branch_invite" in r_intents and nxt_t and nxt_t["role"] == "client":
                nxt_low = normalize_yo(nxt_t["text"].strip().lower())
                if re.match(r'^(?:хорошо|ладно|спасибо|понял|ок|окей|да|ага|угу|приеду|привезу)\b', nxt_low):
                    ctx["branch_confirmed"] = True

            if nxt_t and nxt_t["role"] == "client" and _CLIENT_CONFIRM_WORD_RE.match(normalize_yo(nxt_t["text"].strip())):
                if not ("branch_invite" in r_intents or "summary" in r_intents):
                    ctx["confirmed_robot_cat_lines"].append(t["text"])
        else:
            c_text = t["text"]
            c_norm = normalize_yo(c_text.strip())
            c_low = c_norm.lower()
            if "ask_problem" in prev_intents or "ask_device" in prev_intents:
                ctx["ask_problem_responses"].add(c_low)
            is_model_reply = (
                "ask_brand_model" in prev_intents
                and not re.search(r'\b(?:улиц[аеуы]?|ул\.?|дом\s*\d|квартир\w*|кв\.?|подъезд\w*)\b', c_low)
                and not find_streets_in_lines([c_text])
            )
            # Если предыдущая реплика была типа "Отлично. Вам удобнее?" (сбитый ASR вопрос после "сейчас подскажу модель"),
            # а сам клиент говорит короткий код ("Н 750 Джи", "А 52") без слова "улица/дом", тоже считаем это не адресом
            if not is_model_reply and idx >= 2 and turns[idx - 2]["role"] == "client":
                prev_client_low = normalize_yo(turns[idx - 2]["text"].lower())
                if re.search(r'\b(?:модел|марк|сейчас.*подскажу)\b', prev_client_low) and re.fullmatch(r'[а-яa-z0-9\s\-]{2,20}', c_low):
                    is_model_reply = True

            if is_model_reply:
                ctx["model_turn_texts"].add(c_text)
                continue

            if "ask_address" in prev_intents:
                ctx["address_prompt_client_indices"].add(len(ctx["address_client_lines"]))
            ctx["address_client_lines"].append(c_text)

    return ctx

def _extract_house_after_street(text: str, street: str):
    unglued = _unstick_asr_address_text(text)
    pat = _street_to_regex(street)
    for m in pat.finditer(unglued):
        tail = unglued[m.end():m.end() + 80]
        tail_conv = _convert_spoken_numbers_in_address_chunk(tail)
        hm = re.match(
            r'^\s*[,.]?\s*(?:это\s+|у\s+нас\s+)?(?:дом\s+|д\.?\s+|номер\s+|№\s*)?(\d+(?:/\d+)?[а-я]?)\b',
            tail_conv, re.IGNORECASE
        )
        if hm and not _has_after_number_stop_word(tail_conv, hm.end(1)):
            return hm.group(1)
    # Fallback для ASR-опечаток названия улицы (напр. "на Шыротной 29", "на Фидюнинского 62")
    street_norm = normalize_yo(street.lower())
    for tm in re.finditer(r'\b([А-Яа-яЁё\-]{5,})\b', unglued):
        matched = _fuzzy_match_street(tm.group(1), allow_ambiguous=True)
        if matched and normalize_yo(matched.lower()) == street_norm:
            tail = unglued[tm.end():tm.end() + 80]
            tail_conv = _convert_spoken_numbers_in_address_chunk(tail)
            hm = re.match(
                r'^\s*[,.]?\s*(?:это\s+|у\s+нас\s+)?(?:дом\s+|д\.?\s+|номер\s+|№\s*)?(\d+(?:/\d+)?[а-я]?)\b',
                tail_conv, re.IGNORECASE
            )
            if hm and not _has_after_number_stop_word(tail_conv, hm.end(1)):
                return hm.group(1)
    return None

def extract_address_from_dialog(client_lines, robot_lines, turns=None, dialog_ctx=None):
    ctx = dialog_ctx if dialog_ctx is not None else (_analyze_zvonok_dialog(turns) if turns else None)
    if ctx is not None:
        safe_robot_lines = ctx["safe_robot_lines"]
        addr_client_lines = ctx["address_client_lines"]
        addr_prompt_indices = ctx["address_prompt_client_indices"]
        rejected_streets = ctx["rejected_streets"]
    else:
        safe_robot_lines = [
            line for line in robot_lines
            if not ROBOT_OWN_ADDRESS_RE.search(normalize_yo(line))
            and not re.search(r'[ш]*широтн\w*[,.\s]+29\b', normalize_yo(line), re.IGNORECASE)
            and ROBOT_ADDRESS_CONFIRM_RE.search(normalize_yo(line))
        ]
        addr_client_lines = list(client_lines)
        addr_prompt_indices = set()
        rejected_streets = set()

    client_text_norm = _unstick_asr_address_text(', '.join(addr_client_lines))
    all_text_norm = _unstick_asr_address_text(', '.join(addr_client_lines + safe_robot_lines))

    client_streets = [
        s for s in find_streets_in_lines(addr_client_lines, address_prompt_indices=addr_prompt_indices)
        if normalize_yo(s.lower()) not in rejected_streets
    ]
    robot_streets = [
        s for s in find_streets_in_lines(safe_robot_lines)
        if normalize_yo(s.lower()) not in rejected_streets
    ]
    street = None
    known_lower = {normalize_yo(s.lower()) for s in KNOWN_STREETS}
    for s in client_streets:
        if normalize_yo(s.lower()) in known_lower:
            street = s
            break
    if not street:
        for s in robot_streets:
            if normalize_yo(s.lower()) in known_lower:
                street = s
                break
    if not street and client_streets:
        street = client_streets[0]

    # Конвертируем словесные числа после адресных маркеров
    conv_text = re.sub(
        r'\b(дом|д\.?|квартира|кв\.?|подъезд|этаж|корпус|корп\.?|строение|стр\.?)\s+([а-яё\s]{3,40})',
        lambda mm: mm.group(1) + ' ' + _convert_spoken_numbers_in_address_chunk(mm.group(2)),
        all_text_norm,
        flags=re.IGNORECASE
    )
    conv_text = _convert_spoken_numbers_in_address_chunk(conv_text)

    house = None
    if street:
        house = _extract_house_after_street(client_text_norm, street)
        if not house:
            house = _extract_house_after_street(all_text_norm, street)
    if not house:
        for m in re.finditer(r'\b(?:дом|д\.?)\s*(\d+(?:/\d+)?[а-я]?)\b', conv_text, re.IGNORECASE):
            if not _has_after_number_stop_word(conv_text, m.end(1)):
                house = m.group(1)
                break
    if not house and street:
        for m in re.finditer(r'\b([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)?)\s+(\d+(?:/\d+)?[а-я]?)\b', all_text_norm):
            if _has_after_number_stop_word(all_text_norm, m.end(2)):
                continue
            cand = _strip_street_prepositions(m.group(1))
            if is_valid_street_name(cand) and not _is_non_street_domain_word(cand):
                house = m.group(2)
                break

    flat = None
    for pat in (
        r'\b(?:квартира|квартиры|кв\.?)\s*(\d+[а-я]?)\b',
        r'\b(\d+[а-я]?)\s*(?:квартира|квартиры|кв\.?)\b',
    ):
        m = re.search(pat, conv_text, re.IGNORECASE)
        if m:
            flat = m.group(1)
            break

    pod = None
    for pat in (
        r'\b(?:подъезд|под\.?)\s*(\d+)\b',
        r'\b(\d+)(?:\s*-\s*[йя]|\s*[йя])?\s*(?:подъезд|подъезда|подъезде)\b',
    ):
        m = re.search(pat, conv_text, re.IGNORECASE)
        if m:
            pod = m.group(1)
            break

    et = None
    for pat in (
        r'\b(?:этаж|эт\.?)\s*(\d+)\b',
        r'\b(\d+)(?:\s*-\s*[йя]|\s*[йя])?\s*(?:этаж|этажа|этаже)\b',
    ):
        m = re.search(pat, conv_text, re.IGNORECASE)
        if m:
            et = m.group(1)
            break

    kor = None
    for pat in (
        r'\b(?:корпус|корп\.?|строение|стр\.?)\s*(\d+[а-я]?)\b',
        r'\b(\d+[а-я]?)\s*(?:корпус|корп\.?|строение|стр\.?)\b',
    ):
        m = re.search(pat, conv_text, re.IGNORECASE)
        if m:
            kor = m.group(1)
            break

    # Контекстный добор коротких числовых ответов (напр. Робот: "Какая квартира?" -> Клиент: "45")
    if turns:
        model_texts = ctx["model_turn_texts"] if ctx is not None else set()
        for idx, t in enumerate(turns[:-1]):
            if t["role"] != "robot":
                continue
            nxt = turns[idx + 1]
            if nxt["role"] != "client" or nxt["text"] in model_texts:
                continue
            r_norm = normalize_yo(t["text"].lower())
            if ROBOT_OWN_ADDRESS_RE.search(r_norm):
                continue
            c_conv = _convert_spoken_numbers_in_address_chunk(normalize_yo(nxt["text"].strip()))
            num_m = re.fullmatch(r'(\d+[а-я]?)', c_conv.strip(' .,!?'))
            if not num_m:
                continue
            val = num_m.group(1)
            if not flat and re.search(r'\bквартир', r_norm) and not re.search(r'\b(?:улиц|дом)\b', r_norm):
                flat = val
            elif not pod and re.search(r'\bподъезд', r_norm):
                pod = val
            elif not et and re.search(r'\bэтаж', r_norm):
                et = val
            elif not kor and re.search(r'\bкорпус', r_norm):
                kor = val
            elif not house and re.search(r'\b(?:номер\s+дома|какой\s+дом)\b', r_norm):
                house = val

    if not street and not house:
        return None

    parts = []
    if street:
        parts.append(street)
    if house:
        parts.append(house)
    if kor:
        parts.append(f"корпус {kor}")
    if pod:
        parts.append(f"подъезд {pod}")
    if et:
        parts.append(f"этаж {et}")
    if flat:
        parts.append(f"квартира {flat}")
    return ', '.join(parts) if parts else None


# Конкретные симптомы и поломки (нормализуется ё->е; БЕЗ названий техники и общих слов 'ремонт'/'диагностик')
SYMPTOM_KEYWORDS = re.compile(
    r'(\bне\s+(?:реагирует|работает|включается|выключается|запускается|стартует|'
    r'греет|нагревает|холодит|морозит|охлаждает|размораживает|отжимает|сливает|'
    r'откачивает|набирает|заливает|крутит|крутится|вращается|открывается|закрывается|'
    r'держит|подает|моет|полощет|сушит|печатает|заряжается|заряжает|видит|ловит|'
    r'подключается|читает|тянет|всасывает|пылесосит|наливает|мелет|взбивает|печет|показывает)\b|'
    r'\bнет\s+(?:изображения|картинки|звука|подсветки|холода|воды|нагрева|зарядки|питания|сигнала|связи|сети)\b|'
    r'\b(?:изображения|звука|подсветки|холода|воды|питания|зарядки)\s+нет\b|'
    r'\bперестал[аои]?\b|\bсломал\w*\b|\bполом\w+\b|\bнеисправн\w*\b|'
    r'\b(?:потух|погас|гаснет|сгорел\w*|перегорел\w*|накрыл\w*|вырубает\w*|отключает\w*|'
    r'перезагружает\w*|выбивает|вышибает|коротит|коротнул\w*|замкнул\w*|замыкани\w*|'
    r'искрит\w*|дымит\w*|задымил\w*|хлопнул\w*|щелкает|щелкнул\w*|подгорел\w*|'
    r'гарью|жженым|запах\w*|воняет)\b|'
    r'\b(?:течет|потек\w*|протек\w*|подтекает|капает|льет|лужа|залил[аи]?\s+вод\w*|залили)\b|'
    r'\b(?:шумит|гудит|трещит|стучит|гремит|скрипит|свистит|пищит|жужжит|вибрирует|'
    r'трясется|прыгает|скачет|дребезжит|хрипит)\b|'
    r'\b(?:мигает|моргает|полосы|полосит|рябит|помехи|искажает|пятен|пятно|битые\s+пиксели|'
    r'черный\s+экран|темный\s+экран|синий\s+экран|белый\s+экран|разбит\w*|треснул\w*|трещина|'
    r'продавил\w*|упал\w*|уронили|утопили|вырвало|отвалил\w*|оторвал\w*)\b|'
    r'\b(?:зависает|тормозит|глючит|перегрев\w*|зажевывает|замятие|застрял\w*|мажет|'
    r'бледно\s+печатает|залипает|заедает|заклинил\w*|провисла|болтается|расшатал\w*)\b|'
    r'\b(?:перемораживает|намерзает|снежная\s+шуба|иней|вздулась|утечка\s+фреона|'
    r'фреон\w*|дует\s+теплым|дренаж\s+забит|плесень)\b|'
    r'\b(?:дисплей|кнопк\w*|экран|матриц\w*|подсветк\w*|сенсор\w*|тач\w*|ошибк\w*|код\s+ошибки|'
    r'индикатор|лампочк\w*|компрессор\w*|подшипник\w*|барабан\w*|уплотнител\w*|манжет\w*|'
    r'резинк\w*|прокладк\w*|термостат\w*|тэн\w*|магнетрон\w*|слюд\w*|капучинатор\w*|'
    r'заварочный\s+блок|декальцинац\w*|лидар\w*|щетк\w*|блок\s+питания|провод\w*|разъем\w*|'
    r'гнезд\w*|аккумулятор\w*|батаре\w*|кулер\w*|термопаст\w*|прошивк\w*|вирус\w*|джойстик\w*|стик\w*)\b|'
    r'\b(?:замен[аиу]\s+(?:матрицы|экрана|стекла|дисплея|батареи|аккумулятора|разъема|'
    r'термопасты|жидкого\s+металла|подшипника|тэна|уплотнителя|компрессора|манжеты|шланга)|'
    r'заменить\s+(?:жидкий\s+металл|термопасту|матрицу|экран|стекло|дисплей|батарею|аккумулятор|разъем|подшипник|тэн|уплотнитель|компрессор|манжету|шланг)|'
    r'перепрошить|переустановить\s+(?:виндовс|windows|винду)|удалить\s+вирусы|'
    r'(?:чистка|почистить)\s+от\s+пыли|заправка\s+фреоном|заправить\s+фреон)\b)',
    re.IGNORECASE
)

GENERAL_REPAIR_KEYWORDS = re.compile(
    r'\b(?:ремонт\w*|диагностик\w*|подключ\w*|установ\w*|настро[йи]\w*|'
    r'обслуживани\w*|профилактик\w*|чистк\w*|почин\w*|замен\w*|прошив\w*)\b',
    re.IGNORECASE
)

# Оставляем алиас PROBLEM_KEYWORDS для обратной совместимости
PROBLEM_KEYWORDS = SYMPTOM_KEYWORDS

_ROBOT_SUMMARY_INTRO_RE = re.compile(
    r'^(?:(?:хорошо|отлично|ладно|спасибо|я\s+вас\s+понял[аи]?|понял[аи]?|'
    r'записал[аи]?|зафиксировал[аи]?|итак|значит|давайте\s+проверим|'
    r'проверьте(?:\s+пожалуйста)?|я\s+правильно\s+понял[аи]?(?:\s*,?\s*что)?|'
    r'подведем\s+итог|ваша\s+заявка|получается)[,:.\s]*)+',
    re.IGNORECASE
)
_ROBOT_SUMMARY_TAIL_RE = re.compile(
    r'(?:[.,;!?]+\s*|\s+)(?:назовите|подскажите|уточните|скажите|какой|какая|где|'
    r'по\s+какому\s+адресу|ваш\s+адрес|адрес(?:\s+не\s+указан|\s+для\s+выезда)?|улиц[ауе]|дом\b|квартир\w*|кв\.?|'
    r'подъезд\w*|этаж\w*|телефон\w*|номер\w*|свяжется|перезвонит|ожидайте|'
    r'филиал(?:\s+на\s+\S+(?:\s+[а-яё]\.?)?)?|мастер\s+(?:с\s+вами|перезвонит|свяжется)|все\s+верно)\b.*$',
    re.IGNORECASE
)
_ROBOT_FAREWELL_RE = re.compile(
    r'\b(?:мастер\s+(?:по\s+ремонту\s+)?(?:свяжется|перезвонит)|перезвонит\s+вам|'
    r'свяжется\s+с\s+вами|ожидайте\s+звонка|спасибо\s+за\s+обращение|всего\s+доброго|до\s+свидания)\b',
    re.IGNORECASE
)
_CLIENT_DEVICE_HINT_RE = re.compile(
    r'\b(?:холодильник\w*|морозильник\w*|стиральн\w*(?:\s+машин\w*)?|стиралк\w*|'
    r'посудомоечн\w*(?:\s+машин\w*)?|посудомойк\w*|духов\w*(?:\s+шкаф\w*)?|'
    r'варочн\w*(?:\s+панел\w*)?|электроплит\w*|плит[аыуе]|кондиционер\w*|сплит[\-\s]?систем\w*|'
    r'телевизор\w*|телик\w*|телек\w*|плазм\w*|ноутбук\w*|ноут\w*|макбук\w*|компьютер\w*|'
    r'системн\w*\s+блок\w*|моноблок\w*|монитор\w*|принтер\w*|мфу|сканер\w*|'
    r'телефон\w*|смартфон\w*|айфон\w*|планшет\w*|пылесос\w*|микроволновк\w*|свч|'
    r'кофемашин\w*|кофеварк\w*|колонк\w*|алис\w*|приставк\w*|консол\w*|playstation|xbox|ps[345]|плойк\w*|'
    r'водонагревател\w*|бойлер\w*)\b',
    re.IGNORECASE
)
_CLIENT_REFUSAL_WORD_RE = re.compile(r'^(?:нет|не\s+так|неправильно|ошибка)\b', re.IGNORECASE)
_CLIENT_CONFIRM_WORD_RE = re.compile(r'^(?:да|верно|все\s+верно|правильно|так|точно|ага|угу)\b', re.IGNORECASE)

_BRAND_HINT_RE = re.compile(
    r'\b(?:samsung|самсунг|asus|асус|apple|эпл|айфон|iphone|macbook|макбук|sony|сони|'
    r'playstation|плейстейшн|xbox|lg|элджи|bosch|бош|indesit|индезит|ariston|аристон|'
    r'atlant|атлант|haier|хайер|beko|беко|electrolux|электролюкс|candy|канди|zanussi|занусси|'
    r'siemens|сименс|whirlpool|вирпул|liebherr|либхер|bauknecht|miele|миле|gorenje|горенье|'
    r'lenovo|леново|hp|эйчпи|acer|эйсер|dell|делл|msi|huawei|хуавей|honor|хонор|'
    r'xiaomi|ксиаоми|сяоми|redmi|редми|philips|филипс|delonghi|делонги|krups|крупс|'
    r'saeco|саеко|jura|юра|nivona|нивона|dyson|дайсон|roborock|роборок|tefal|тефаль|'
    r'epson|эпсон|canon|кэнон|brother|бразер|kyocera|киосера|xerox|ксерокс|'
    r'hisense|хайсенс|tcl|тисиэл|panasonic|панасоник|toshiba|тошиба|sharp|шарп|'
    r'daikin|дайкин|mitsubishi|митсубиси|ballu|баллу|gree|гри)\b',
    re.IGNORECASE
)

def _extract_brand_hint(client_lines, robot_lines):
    for line in client_lines:
        m = _BRAND_HINT_RE.search(normalize_yo(line))
        if m:
            return m.group(0)
    for line in robot_lines:
        norm = normalize_yo(line)
        if re.search(r'\b(?:хорошо|поняла|проверьте|марка|бренд)\b', norm, re.IGNORECASE):
            m = _BRAND_HINT_RE.search(norm)
            if m:
                return m.group(0)
    return None

def _extract_device_hint(client_lines):
    for line in client_lines:
        cleaned = clean_text(strip_price_and_time_tails(line))
        norm = normalize_yo(cleaned)
        m = _CLIENT_DEVICE_HINT_RE.search(norm)
        if m:
            # Берём короткую фразу с прибором/маркой, если она компактная
            if len(cleaned) <= 45 and not re.search(r'\b(?:улиц|дом\s*\d|кв|подъезд)\b', norm, re.IGNORECASE):
                return cleaned
            return m.group(0)
    return None

def _strip_inline_address_tail(text: str) -> str:
    """Аккуратно убирает адрес в конце или начале реплики, если оставшаяся часть содержит описание проблемы."""
    res = text
    m = re.search(
        r'[,.;]?\s*(?:мой\s+адрес|адрес|живу\s+(?:на|по)|улица|ул\.?)\s+.*$',
        res, re.IGNORECASE
    )
    if m and m.start() >= 6:
        prefix = res[:m.start()].strip(' ,.;:-')
        if SYMPTOM_KEYWORDS.search(normalize_yo(prefix)):
            res = prefix

    # Если фраза начинается с адреса (напр. "На улице Пермяково 54, не морозит холодильник" / "наширотной 120, подъезд 3, стиралка не крутит")
    # и после последней адресной части следует полноценная проблема с симптомом — обрезаем адресный префикс.
    parts = [p.strip() for p in re.split(r'\s*[,;.]\s*', res) if p.strip()]
    if len(parts) >= 2:
        drop_until = 0
        for idx in range(len(parts) - 1):
            seg = _unstick_asr_address_text(parts[idx])
            seg_low = normalize_yo(seg.lower())
            has_addr_seg = bool(
                re.search(r'\b(?:улиц[аеуы]?|ул\.?|подъезд\w*|квартир\w*|кв\.?|корпус\w*|этаж\w*|дом\s+\d+)\b', seg_low)
                or (re.match(r'^(?:на|по|в)\s+', seg_low) and find_streets_in_lines([seg]))
                or (find_streets_in_lines([seg]) and re.search(r'\b\d+\b', seg_low) and not SYMPTOM_KEYWORDS.search(seg_low))
            )
            if has_addr_seg:
                drop_until = idx + 1
            else:
                break
        if drop_until > 0 and drop_until < len(parts):
            rest = ', '.join(parts[drop_until:]).strip(' ,.;:-')
            if SYMPTOM_KEYWORDS.search(normalize_yo(rest)):
                res = rest
    return res

def _is_redundant_fragment(main_text: str, extra_text: str) -> bool:
    """Проверяет, что дополнительный симптом действительно несёт новую информацию."""
    m_words = set(re.findall(r'[а-яёa-z0-9]{4,}', normalize_yo(main_text.lower())))
    e_words = set(re.findall(r'[а-яёa-z0-9]{4,}', normalize_yo(extra_text.lower())))
    if not e_words:
        return True
    new_words = e_words - m_words
    return len(new_words) == 0 or extra_text.lower() in main_text.lower()

def extract_problem_from_dialog(client_lines, robot_lines, turns=None, merged_client_lines=None, dialog_ctx=None):
    ctx = dialog_ctx if dialog_ctx is not None else (_analyze_zvonok_dialog(turns) if turns else None)
    # 1. Ищем подтверждённую/валидную сводку робота (только по SYMPTOM_KEYWORDS, не по прощанию!)
    summary_candidates = []
    if turns:
        robot_turn_pairs = []
        for idx, t in enumerate(turns):
            if t["role"] == "robot":
                nxt = turns[idx + 1]["text"] if idx + 1 < len(turns) and turns[idx + 1]["role"] == "client" else ""
                robot_turn_pairs.append((t["text"], nxt))
    else:
        robot_turn_pairs = [(line, "") for line in robot_lines]

    for r_line, next_client in robot_turn_pairs:
        r_norm = normalize_yo(r_line)
        has_intro = bool(re.search(
            r'\b(?:проверьте|давайте\s+проверим|я\s+правильно\s+понял|понял[аи]?|записал[аи]?|зафиксировал[аи]?|итак|значит|подведем\s+итог)\b',
            r_norm, re.IGNORECASE
        ))
        if not has_intro:
            continue
        if _ROBOT_FAREWELL_RE.search(r_norm) and not re.search(r'^\s*(?:проверьте|итак|записала)', r_norm, re.IGNORECASE):
            continue
        if next_client and _CLIENT_REFUSAL_WORD_RE.match(normalize_yo(next_client.strip())):
            continue

        line_clean = _ROBOT_SUMMARY_INTRO_RE.sub('', r_line).strip()
        line_clean = _ROBOT_SUMMARY_TAIL_RE.sub('', line_clean).strip(' ,.:;-')
        line_clean = clean_text(line_clean)
        line_norm = normalize_yo(line_clean)
        if not line_clean or len(line_clean) < 5:
            continue
        if not SYMPTOM_KEYWORDS.search(line_norm):
            continue
        if not is_meaningful_issue(line_clean):
            continue
        confirmed = bool(next_client and _CLIENT_CONFIRM_WORD_RE.match(normalize_yo(next_client.strip())))
        score = len(SYMPTOM_KEYWORDS.findall(line_norm)) * 3 + (2 if confirmed else 0)
        summary_candidates.append((score, confirmed, line_clean))

    # 2. Собираем и оцениваем кандидатов из реплик клиента (в т.ч. склеенных ASR-фрагментов)
    raw_pool = list(merged_client_lines or []) + list(client_lines or [])
    seen_norm = set()
    client_candidates = []

    if ctx is not None:
        ask_problem_responses = ctx["ask_problem_responses"]
    else:
        ask_problem_responses = set()
        if turns:
            for idx, t in enumerate(turns[:-1]):
                if t["role"] == "robot" and turns[idx + 1]["role"] == "client":
                    r_low = normalize_yo(t["text"].lower())
                    if re.search(r'\b(?:что\s+(?:именно\s+)?(?:случилось|сломалось|не\s+работает)|какая\s+(?:проблема|неисправность)|в\s+чем\s+(?:проблема|дело)|опишите)\b', r_low):
                        ask_problem_responses.add(normalize_yo(turns[idx + 1]["text"].strip().lower()))

    for raw_line in raw_pool:
        stripped = strip_price_and_time_tails(raw_line)
        cleaned = clean_text(stripped)
        cleaned = _strip_inline_address_tail(cleaned)
        if not cleaned or not is_meaningful_issue(cleaned):
            continue
        norm = normalize_yo(cleaned)
        norm_key = norm.lower()
        if norm_key in seen_norm:
            continue
        seen_norm.add(norm_key)

        sym_count = len(SYMPTOM_KEYWORDS.findall(norm))
        gen_count = len(GENERAL_REPAIR_KEYWORDS.findall(norm))
        is_direct_answer = normalize_yo(raw_line.strip().lower()) in ask_problem_responses

        if sym_count == 0 and gen_count == 0 and not is_direct_answer:
            continue
        if sym_count == 0 and gen_count == 0 and re.search(r'\b(?:улиц|дом\s*\d|кв\.?\s*\d|подъезд)\b', norm, re.IGNORECASE):
            continue

        has_device = 1 if _CLIENT_DEVICE_HINT_RE.search(norm) else 0
        sc = sym_count * 4 + gen_count * 1 + has_device * 2 + (1 if is_direct_answer else 0)
        if 10 <= len(cleaned) <= 95:
            sc += 1
        client_candidates.append((sc, sym_count, has_device, cleaned))

    best_client = max(client_candidates, key=lambda x: (x[0], x[1], x[2], len(x[3]))) if client_candidates else None
    best_summary = max(summary_candidates, key=lambda x: (x[0], len(x[2]))) if summary_candidates else None

    chosen = None
    used_client_primary = False
    if best_client and best_summary:
        c_score, c_sym, c_dev, c_text = best_client
        s_score, s_conf, s_text = best_summary
        if s_conf and s_score > c_score and len(s_text) >= len(c_text) * 0.6:
            chosen = s_text
        elif c_sym > 0:
            chosen = c_text
            used_client_primary = True
        else:
            chosen = s_text
    elif best_client:
        chosen = best_client[3]
        used_client_primary = True
    elif best_summary:
        chosen = best_summary[2]

    if not chosen:
        return "не указана"

    # Если клиент в отдельных репликах добавил ещё симптомы (не перекрывающиеся с основной фразой), добавим 1 уточнение
    if used_client_primary and len(client_candidates) > 1:
        for sc, sym_cnt, _, extra in sorted(client_candidates, key=lambda x: (x[0], x[1]), reverse=True):
            if extra == chosen or sym_cnt == 0:
                continue
            extra_clean = re.sub(r'^(?:а\s+ещ[её]|ещ[её]|и\s+ещ[её]|плюс|также|и)\s+', '', extra, flags=re.IGNORECASE).strip(' ,.')
            if extra_clean and not _is_redundant_fragment(chosen, extra_clean) and len(chosen) + len(extra_clean) + 2 <= 125:
                chosen = f"{chosen}; {extra_clean}"
                break

    # Если в выбранной фразе клиента нет названия техники, а в другой реплике клиент её назвал (или подтвердил у робота) — аккуратно добавим
    if not _CLIENT_DEVICE_HINT_RE.search(normalize_yo(chosen)):
        dev_hint = _extract_device_hint(client_lines)
        if not dev_hint and ctx is not None and ctx["confirmed_robot_cat_lines"]:
            for r_conf in ctx["confirmed_robot_cat_lines"]:
                m_dev = _CLIENT_DEVICE_HINT_RE.search(normalize_yo(r_conf))
                if m_dev:
                    dev_hint = m_dev.group(0).lower()
                    break
        if dev_hint and dev_hint.lower() not in chosen.lower():
            chosen = f"{dev_hint} — {chosen}"

    # Если марка названа в соседних репликах, но отсутствует в итоговой фразе — дописываем в скобках
    if not _BRAND_HINT_RE.search(normalize_yo(chosen)):
        brand_hint = _extract_brand_hint(client_lines, robot_lines)
        if brand_hint and brand_hint.lower() not in chosen.lower():
            if brand_hint.islower():
                brand_hint = brand_hint.upper() if len(brand_hint) <= 3 and brand_hint.isascii() else brand_hint.capitalize()
            chosen = f"{chosen} ({brand_hint})"

    if len(chosen) > 130:
        cut = chosen[:127].rsplit(' ', 1)[0]
        chosen = (cut if len(cut) >= 65 else chosen[:127]) + '...'
    return chosen


VOICEMAIL_PATTERNS = [
    r'\bабонент\s+(?:временно\s+)?недоступен\b',
    r'\bабонент\s+не\s+отвечает\b',
    r'\bабонент\s+не\s+может\s+ответить\b',
    r'\bоставьте\s+сообщение\s+после\s+(?:звукового\s+)?сигнала\b',
    r'\bголосовой\s+почтовый\s+ящик\b',
    r'\bголосовая\s+почта\b',
    r'\bномер\s+не\s+обслуживается\b',
    r'\bаппарат\s+абонента\s+выключен\b',
    r'\bвне\s+зоны\s+(?:действия\s+)?сети\b',
]

ROBOT_REFUSAL_PATTERNS = [
    r'это\s+не\s+та\s+техника',
    r'не\s+та\s+техника,?\s+которую\s+мы',
    r'мы\s+не\s+ремонтируем',
    r'не\s+занимаемся\s+ремонтом',
    r'не\s+входит\s+в\s+наш',
    r'не\s+наш\s+профиль',
    r'к\s+сожалению,?\s+(?:мы\s+)?не\s+сможем\s+помочь',
    r'не\s+выезжаем\s+на\s+такую\s+технику',
]

CLIENT_REFUSAL_PATTERNS = [
    r'\bошиблись\s+номером\b',
    r'\bне\s+туда\s+попали\b',
    r'\bничего\s+не\s+(?:ломалось|заказывал[аи]?|оставлял[аи]?)\b',
    r'\bу\s+меня\s+все\s+работает\b',
    r'\bя\s+уже\s+(?:починил|нашел|сделал|решил|отдал|вызвал)\b',
    r'\bуже\s+(?:починили|сделали|отремонтировали|нашли)\b',
    r'\bуже\s+не\s+актуально\b',
    r'\bне\s+актуально\b',
    r'\bне\s+звоните\b',
    r'\bудалите\s+(?:мой\s+)?номер\b',
    r'\bотказ(?:ыв)?аюсь\b',
    r'\bя\s+передумал[аи]?\b',
    r'\bмы\s+передумали\b',
]

CLIENT_STANDALONE_REFUSAL_RE = re.compile(
    r'^(?:да\s*,?\s*|нет\s*,?\s*)?(?:спасибо\s*,?\s*)?(?:мне\s+это\s+|нам\s+это\s+)?(?:пока\s+|уже\s+)?'
    r'(?:не\s+надо|не\s+нужно|не\s+интерес(?:но|ует)|я\s+подумаю|мы\s+подумаем|'
    r'перезвон(?:ите|ю)\s+(?:позже|потом)|позже\s+перезвон\w*|'
    r'не\s+могу\s+говорить|сейчас\s+неудобно(?:\s+говорить)?)[.,!?\s]*$',
    re.IGNORECASE
)

def _extract_zvonok_turns(body: str):
    norm_body = body.replace('\r\n', '\n').replace('\r', '\n')
    norm_body = re.sub(
        r'(?<=[\s.!?])(?=(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?(?:Робот|Бот|Оператор|Ассистент|AI|Клиент|Абонент|Пользователь|Собеседник)\s*:)',
        '\n',
        norm_body,
        flags=re.IGNORECASE
    )
    speaker_re = re.compile(
        r'^(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?'
        r'(Робот|Бот|Оператор|Ассистент|AI|Клиент|Абонент|Пользователь|Собеседник)\s*:\s*(.*)$',
        re.IGNORECASE
    )
    meta_re = re.compile(
        r'^(?:phone|client_phone|caller_id|call_id|campaign_id|status|duration|audio|record|recorded_at|date|time|телефон|номер|ссылка)\s*:|^https?://',
        re.IGNORECASE
    )
    turns = []
    current = None
    for raw_line in norm_body.split('\n'):
        line = raw_line.lstrip('> \t').strip()
        if not line:
            continue
        if re.fullmatch(r'[-_=]{3,}', line):
            current = None
            continue
        sm = speaker_re.match(line)
        if sm:
            tag = sm.group(1).lower()
            role = "robot" if tag in ("робот", "бот", "оператор", "ассистент", "ai") else "client"
            txt = sm.group(2).strip()
            current = {"role": role, "text": txt}
            turns.append(current)
            continue
        if meta_re.match(line):
            current = None
            continue
        if current is not None:
            current["text"] = (current["text"] + " " + line).strip()

    turns = [t for t in turns if t["text"]]
    merged_turns = []
    for t in turns:
        if merged_turns and merged_turns[-1]["role"] == t["role"]:
            prev = merged_turns[-1]["text"]
            sep = " " if prev.endswith(('.', '!', '?', ',', ';', ':')) else ", "
            merged_turns[-1]["text"] = prev + sep + t["text"]
        else:
            merged_turns.append({"role": t["role"], "text": t["text"]})

    robot_lines = [t["text"] for t in turns if t["role"] == "robot"]
    client_lines = [t["text"] for t in turns if t["role"] == "client"]
    merged_client_lines = [t["text"] for t in merged_turns if t["role"] == "client"]
    return turns, merged_turns, robot_lines, client_lines, merged_client_lines


def parse_zvonok(body):
    m = re.search(
        r'^[>\s]*(?:phone|client_phone|caller_id|телефон(?:\s+клиента)?|номер(?:\s+телефона)?)\s*:\s*(\+?\d[\d\s\-\(\)]{8,})',
        body, re.MULTILINE | re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'\b(?:phone|client_phone|caller_id|телефон(?:\s+клиента)?)\s*:\s*(\+?\d[\d\s\-\(\)]{8,})',
            body, re.IGNORECASE
        )
    if not m:
        logger.info("Zvonok: телефон не найден, заявка пропущена")
        return None

    raw_phone = m.group(1).strip()
    digits = re.sub(r'\D', '', raw_phone)
    if len(digits) < 10:
        logger.info("Zvonok: слишком короткий номер телефона, заявка пропущена")
        return None
    phone = format_phone(raw_phone)

    turns, merged_turns, robot_lines, client_lines, merged_client_lines = _extract_zvonok_turns(body)

    if not client_lines:
        logger.info("Zvonok: клиент ничего не сказал, заявка пропущена")
        return None

    dialog_ctx = _analyze_zvonok_dialog(turns)

    robot_text = ' '.join(robot_lines)
    client_text = ' '.join(client_lines)
    robot_text_norm = normalize_yo(robot_text)
    client_text_norm = normalize_yo(client_text)
    all_text_lower = normalize_yo((robot_text + ' ' + client_text).lower())

    # Автоответчики / оператор связи
    for pattern in VOICEMAIL_PATTERNS:
        if re.search(pattern, client_text_norm, re.IGNORECASE):
            if not SYMPTOM_KEYWORDS.search(client_text_norm):
                logger.info(f"Zvonok: обнаружен автоответчик ('{pattern}'), заявка пропущена")
                return None

    # Отказы робота
    for pattern in ROBOT_REFUSAL_PATTERNS:
        if re.search(pattern, robot_text_norm, re.IGNORECASE):
            logger.info(f"Zvonok: робот отказал ('{pattern}'), заявка пропущена")
            return None

    # Безусловные отказы клиента
    for pattern in CLIENT_REFUSAL_PATTERNS:
        if re.search(pattern, client_text_norm, re.IGNORECASE):
            logger.info(f"Zvonok: клиент отказался ('{pattern}'), заявка пропущена")
            return None

    # Контекстный отказ: клиент явно отказался в отдельной реплике и не дал после этого проблему/адрес
    last_client_norm = normalize_yo(merged_client_lines[-1]) if merged_client_lines else ""
    has_standalone_refusal = any(
        CLIENT_STANDALONE_REFUSAL_RE.match(normalize_yo(line.strip()))
        for line in client_lines
    )

    # Определяем категорию по речи клиента, затем по подтверждённым репликам робота
    category_key = detect_category(client_text_norm)
    if category_key == "other" and dialog_ctx["confirmed_robot_cat_lines"]:
        category_key = detect_category(normalize_yo(' '.join(dialog_ctx["confirmed_robot_cat_lines"])))
    if category_key == "other":
        safe_robot_for_cat = [
            line for line in robot_lines
            if not re.search(r'\b(?:мы\s+(?:не\s+)?ремонтируем|наша\s+компания|занимаемся\s+ремонтом)\b', normalize_yo(line), re.IGNORECASE)
        ]
        if safe_robot_for_cat:
            category_key = detect_category(normalize_yo(' '.join(safe_robot_for_cat)))

    problem = extract_problem_from_dialog(
        client_lines, robot_lines, turns=turns, merged_client_lines=merged_client_lines, dialog_ctx=dialog_ctx
    )
    address = extract_address_from_dialog(client_lines, robot_lines, turns=turns, dialog_ctx=dialog_ctx)

    if has_standalone_refusal and CLIENT_STANDALONE_REFUSAL_RE.match(last_client_norm) and not address:
        logger.info("Zvonok: финальный отказ клиента без адреса, заявка пропущена")
        return None

    if not address:
        if (
            category_key in FILIAL_CATEGORIES
            and re.search(
                r'(ждем вас|принимаем только в филиале|филиал|находимся на|ждем вас на|'
                r'привозите к нам|в филиал)',
                all_text_lower
            )
        ) or dialog_ctx["branch_confirmed"] or re.search(
            r'(принимаем только в филиале|привозите к нам|привез[ути]\s+(?:к\s+вам|в\s+филиал)|'
            r'принес[ути]\s+(?:к\s+вам|в\s+филиал)|сам\s+привезу|'
            r'(?:к\s+вам|в\s+филиал)\s+(?:можно\s+)?(?:подъехать|приехать|привезти|перевезти|подвезти|принести|сдать)|'
            r'(?:привезти|перевезти|подвезти|принести)\s+(?:ноутбук|приставк\w*|микроволновк\w*|кофемашин\w*|пылесос|телефон|техник\w*))',
            all_text_lower
        ):
            address = "филиал (привоз)"
        else:
            address = "не указано"

    if category_key in FILIAL_CATEGORIES and address != "не указано" and "широтная" in address.lower():
        if not re.search(r'\b(?:квартира|подъезд|этаж)\b', address.lower()):
            address = "филиал (привоз)"

    if problem == "не указана" and address == "не указано":
        logger.info("Zvonok: нет ни проблемы, ни адреса – заявка отклонена")
        return None

    text = (
        "🚨 Новая заявка (Звонок)!\n"
        f"📞 Телефон: {phone}\n"
        f"💬 Проблема: {problem}\n"
        f"📍 Адрес: {address}\n"
    )
    return text, category_key


# ---------- WEBHOOK: приём заявок с Cloudflare Worker ----------
def build_message_from_json(data: dict):
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
    if category_key == 'other' and problem:
        category_key = detect_category(f"{problem} {brand}".strip())

    phone = format_phone(phone)

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
    try:
        data = await request.json()
        logger.info(f"WEBHOOK получен: {data}")

        text, category_key = build_message_from_json(data)
        if not text:
            return web.json_response({'success': False, 'error': 'name и phone обязательны'}, status=400)

        phone_match = re.search(r'📞 Телефон:\s*(.+)', text)
        if phone_match:
            digits = re.sub(r'\D', '', phone_match.group(1))
            now = datetime.now()
            _cleanup_recent_phones(now)
            if digits in recent_phones and (now - recent_phones[digits]) < DUPLICATE_PHONE_TTL:
                logger.info("WEBHOOK: заявка отклонена (дубликат телефона)")
                return web.json_response({'success': True, 'skipped': 'duplicate'})
            recent_phones[digits] = now

        asyncio.create_task(send_and_dispatch(text, category_key))

        return web.json_response({'success': True})
    except Exception as e:
        logger.error(f"WEBHOOK error: {e}", exc_info=True)
        return web.json_response({'success': False, 'error': str(e)}, status=500)


async def handle_health(request):
    return web.json_response({'status': 'ok', 'service': 'dispatcher-bot'})


async def start_web_server():
    try:
        app = web.Application()
        app.router.add_post('/webhook', handle_webhook)
        app.router.add_get('/health', handle_health)
        app.router.add_get('/', handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
        await site.start()
        logger.info(f"✅ HTTP-сервер УСПЕШНО запущен на 0.0.0.0:{WEBHOOK_PORT}")
    except Exception as e:
        logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА при запуске веб-сервера: {e}", exc_info=True)

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ ----------
_CATEGORY_PRIORITY = [
    "computers", "orgtech", "cond", "refrigerators", "appliances",
    "tv", "phone", "vacuum", "microwave", "coffee", "speaker", "console"
]

_CATEGORY_RULES = {
    "computers": [
        (10, re.compile(r'\b(?:ноутбук\w*|ноудбук\w*|ноут[ауеы]?|нетбук\w*|макбук\w*|macbook\w*|компьютер\w*|комп[ауеы]?|системн\w+\s+блок\w*|системник\w*|моноблок\w*|imac|пк|монитор\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:виндовс\w*|windows|винд[ауеы]|материнск\w+\s+плат\w*|видеокарт\w*|тачпад\w*)\b', re.IGNORECASE)),
    ],
    "orgtech": [
        (10, re.compile(r'\b(?:принтер\w*|мфу\w*|сканер\w*|копир\w*|факс\w*|плоттер\w*|оргтехник\w*|ксерокс\w*|шредер\w*|термопринтер\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:лазерджет\w*|laserjet|kyocera|киосера|epson|эпсон|pantum|пантум|xerox)\b', re.IGNORECASE)),
        (2, re.compile(r'\b(?:зажевывает\s+бумаг\w*|не\s+печатает|замятие\s+бумаги)\b', re.IGNORECASE)),
    ],
    "cond": [
        (10, re.compile(r'\b(?:кондиционер\w*|кондицеонер\w*|кондер\w*|сплит[\-\s]?систем\w*|сплит|мультисплит\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:внутренн\w+\s+блок\w*|наружн\w+\s+блок\w*)\b', re.IGNORECASE)),
        (2, re.compile(r'\b(?:дует\s+теплым|дренаж\s+забит)\b', re.IGNORECASE)),
    ],
    "refrigerators": [
        (10, re.compile(r'\b(?:холодильник\w*|халадильник\w*|морозильник\w*|морозильн\w+\s+(?:камер\w*|лар[ья])|холодильн\w+\s+камер\w*|винн\w+\s+шкаф\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:морозилка\w*|атлант|стинол|либхер|liebherr|бирюса|ноу\s*фрост|no\s*frost)\b', re.IGNORECASE)),
        (2, re.compile(r'\b(?:не\s+морозит|не\s+холодит|утечк\w+\s+фреон\w*|намерзает\s+лед|снежная\s+шуба|перемораживает)\b', re.IGNORECASE)),
    ],
    "appliances": [
        (10, re.compile(r'\b(?:стиральн\w*|стиралк\w*|посудомоечн\w*|посудомойк\w*|посудамойк\w*|сушильн\w+\s+машин\w*|электроплит\w*|плит(?:а|ы|е|у|ой)|духовк\w*|духов\w+\s+шкаф\w*|варочн\w*|водонагревател\w*|бойлер\w*|бытов\w+\s+техник\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:индукционн\w*|стеклокерамик\w*)\b', re.IGNORECASE)),
        (2, re.compile(r'\b(?:не\s+отжимает|не\s+сливает|не\s+полощет|барабан\w*|манжет\w*|уплотнител\w*|резинк\w*|прокладк\w*)\b', re.IGNORECASE)),
    ],
    "phone": [
        (10, re.compile(r'\b(?:телефон\w*|смартфон\w*|планшет\w*|айфон\w*|iphone|айпад\w*|ipad|мобильник\w*|сотов\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:андроид\w*|онор|honor|технопол|tecno|техно|redmi|редми|xiaomi|сяоми|ксиаоми|poco|поко|realme|реалми)\b', re.IGNORECASE)),
    ],
    "vacuum": [
        (10, re.compile(r'\b(?:робот[\-\s]?пылесос\w*|пылесос\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:дайсон|dyson|dreame|дреам\w*|roborock|роборок)\b', re.IGNORECASE)),
        (3, re.compile(r'\b(?:не\s+всасывает|ошибка\s+лидара|не\s+видит\s+базу)\b', re.IGNORECASE)),
    ],
    "microwave": [
        (10, re.compile(r'\b(?:микроволнов\w*|микраволнов\w*|микроволна\w*|свч)\b', re.IGNORECASE)),
        (3, re.compile(r'\b(?:тарелка\s+не\s+крутится|магнетрон\w*|слюд\w*)\b', re.IGNORECASE)),
    ],
    "coffee": [
        (10, re.compile(r'\b(?:кофемашин\w*|кофе\s+машин\w*|кофеварк\w*|кофе\s+варк\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:дельонги|delonghi|saeco|саеко|jura|юра|nivona|нивона)\b', re.IGNORECASE)),
        (3, re.compile(r'\b(?:капучинатор\w*|заварочн\w+\s+блок\w*|декальцинац\w*|не\s+наливает\s+кофе)\b', re.IGNORECASE)),
    ],
    "speaker": [
        (10, re.compile(r'\b(?:умн\w+\s+колонк\w*|колонк\w*|яндекс\s+станци\w*|sberbox)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:алис[ауеы]|марус[яиею]|jbl)\b', re.IGNORECASE)),
    ],
    "console": [
        (10, re.compile(r'\b(?:игров\w+\s+приставк\w*|приставк\w*|консол\w*|playstation|сониплейстейшн\w*|плейстейшн\w*|плейстайшн\w*|плюстшн\w*|плюс\s+сейшн\w*|ps\s*[345]|п\s*с\s*[345]|xbox|икс[\-\s]?бокс\w*|nintendo|нинтендо|steam\s*deck|плойк\w*)\b', re.IGNORECASE)),
        (6, re.compile(r'\b(?:джойстик\w*|джостик\w*|геймпад\w*)\b', re.IGNORECASE)),
    ],
}

_CONTACT_PHONE_STRIP_RE = re.compile(
    r'\b(?:(?:звоните|позвоните|перезвоните|звонить|наберите|обращаться|связывайтесь)\s+(?:на|по)\s+(?:этот\s+|мой\s+|данный\s+|другой\s+)?телефон\w*|'
    r'(?:номер|контактн\w*|другой|второй)\s+телефон\w*|по\s+(?:этому\s+)?телефону|'
    r'телефон\s+(?:мужа|жены|супруг[иа]|мамы|папы|дочери|сына))\b',
    re.IGNORECASE
)
_TV_DIRECT_RE = re.compile(r'\b(?:телевизор\w*|телевизар\w*|тв|смарт[\-\s]?тв|smart\s*tv|плазм\w*|телек\w*|телик\w*)\b', re.IGNORECASE)
_TV_TARGETED_RE = re.compile(r'\b(?:к|для|от)\s+(?:телевизор\w*|тв)\b', re.IGNORECASE)
_TV_SYMPTOM_RE = re.compile(r'\b(?:пропала\s+подсветк\w*|нет\s+изображения|звук\s+есть.{0,20}экран)\b', re.IGNORECASE)

def detect_category(text: str) -> str:
    if not text:
        return "other"
    t = normalize_yo(text.lower())
    t_no_contact = _CONTACT_PHONE_STRIP_RE.sub(' ', t)
    scores = {}

    for cat, rules in _CATEGORY_RULES.items():
        hay = t_no_contact if cat == "phone" else t
        sc = 0
        for weight, pat in rules:
            if pat.search(hay):
                sc += weight
        if sc > 0:
            scores[cat] = sc

    tv_sc = 0
    tv_all = len(_TV_DIRECT_RE.findall(t))
    tv_targeted = len(_TV_TARGETED_RE.findall(t))
    if tv_all > tv_targeted:
        tv_sc += 10
    elif tv_targeted > 0:
        tv_sc += 2
    if _TV_SYMPTOM_RE.search(t):
        tv_sc += 2
    if tv_sc > 0:
        scores["tv"] = tv_sc

    if not scores:
        return "other"

    best_score = max(scores.values())
    for cat in _CATEGORY_PRIORITY:
        if scores.get(cat) == best_score:
            return cat
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
        return await bot.send_message(chat_id=chat_id, text=masked_text, reply_markup=keyboard)
    except ChatMigrated as e:
        new_id = e.migrate_to_chat_id
        for cat, groups in GROUPS.items():
            if chat_id in groups:
                groups.remove(chat_id)
                groups.append(new_id)
                break
        return await send_request_with_buttons(new_id, text, message_id)

def _is_accepted_master_message(message) -> bool:
    if message is None:
        return False
    for attr in ("text", "caption"):
        val = getattr(message, attr, None)
        if isinstance(val, str) and val.lstrip().startswith("✅ Заявку принял"):
            return True
    return False

def _record_accepted_request(message_id: int, username: str, chat_id: int = None, msg_id: int = None):
    now = datetime.now()
    cutoff = now - timedelta(hours=24)
    expired = [k for k, v in accepted_requests.items() if v.get("ts") and v["ts"] < cutoff]
    for k in expired:
        accepted_requests.pop(k, None)
    accepted_requests[message_id] = {
        "username": username,
        "chat_id": chat_id,
        "tg_message_id": msg_id,
        "ts": now,
    }

async def _delete_master_message(message=None, chat_id: int = None, msg_id: int = None) -> bool:
    if _is_accepted_master_message(message):
        logger.info("Защита от удаления: сообщение уже принято мастером")
        return False
    try:
        if message is not None:
            await message.delete()
            return True
        if chat_id is not None and msg_id is not None:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            return True
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение заявки из чата мастеров: {e}")
    return False

async def _cleanup_stale_master_message(query):
    msg = getattr(query, "message", None)
    if _is_accepted_master_message(msg):
        return
    deleted = await _delete_master_message(msg)
    if not deleted:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

async def start_timer(message_id: int):
    await asyncio.sleep(1)
    req = active_requests.get(message_id)
    if not req:
        return
    category = req.get("category", "other")
    timeout = get_timeout(category) - 1
    await asyncio.sleep(timeout)
    req = active_requests.get(message_id)
    if not req or req.get("transitioning"):
        return
    req["transitioning"] = True
    await _delete_master_message(None, req.get("chat_id"), req.get("tg_message_id"))
    req["chat_id"] = None
    req["tg_message_id"] = None
    req["current_group_index"] += 1
    groups = req["groups"]
    if req["current_group_index"] >= len(groups):
        active_requests.pop(message_id, None)
        await bot.send_message(
            chat_id=GENERAL_GROUP,
            text=f"⚠️ Никто не принял заявку:\n\n{req['text']}"
        )
    else:
        next_group = groups[req["current_group_index"]]
        try:
            sent = await send_request_with_buttons(next_group, req["text"], message_id)
        except Exception:
            active_requests.pop(message_id, None)
            raise
        if sent is not None:
            req["chat_id"] = getattr(sent, "chat_id", next_group)
            req["tg_message_id"] = getattr(sent, "message_id", None)
        req["transitioning"] = False
        req["timer"] = asyncio.create_task(start_timer(message_id))

async def handle_general_message(update, context):
    if update.effective_chat.id != GENERAL_GROUP: return
    text = update.message.text
    if "🚨 Новая заявка" not in text: return
    text = re.sub(r'^@\w+\s*', '', text)
    await dispatch_request(text)

async def handle_callback(update, context):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    try:
        message_id = int(data.split("_", 1)[1])
    except (IndexError, ValueError):
        return

    if data.startswith("accept_"):
        accepted = accepted_requests.get(message_id)
        if accepted:
            await query.answer(f"Заявку уже принял @{accepted['username']}.", show_alert=True)
            if query.message and (
                query.message.chat_id != accepted.get("chat_id")
                or query.message.message_id != accepted.get("tg_message_id")
            ):
                await _cleanup_stale_master_message(query)
            return
        if _is_accepted_master_message(getattr(query, "message", None)):
            await query.answer("Заявка уже принята.", show_alert=True)
            return
        req = active_requests.get(message_id)
        if not req or req.get("transitioning") or (
            req.get("tg_message_id") and query.message and (
                query.message.message_id != req["tg_message_id"]
                or query.message.chat_id != req.get("chat_id")
            )
        ):
            await query.answer("Заявка уже обработана или устарела.", show_alert=True)
            await _cleanup_stale_master_message(query)
            return
        req = active_requests.pop(message_id, None)
        if not req:
            await query.answer("Заявка уже обработана или устарела.", show_alert=True)
            await _cleanup_stale_master_message(query)
            return
        username = query.from_user.username or query.from_user.first_name
        chat_id = query.message.chat_id if query.message else req.get("chat_id")
        msg_id = query.message.message_id if query.message else req.get("tg_message_id")
        _record_accepted_request(message_id, username, chat_id, msg_id)
        await query.answer()
        if req["timer"]:
            req["timer"].cancel()
        full_text = req["text"]
        accepted_text = f"✅ Заявку принял @{username}\n\n{full_text}"
        try:
            await query.edit_message_text(accepted_text)
        except Exception as e:
            logger.error(f"Не удалось отредактировать принятую заявку {message_id}: {e}")
            if chat_id is not None:
                try:
                    sent_fallback = await bot.send_message(chat_id=chat_id, text=accepted_text)
                    if sent_fallback is not None:
                        msg_id = getattr(sent_fallback, "message_id", msg_id)
                        accepted_requests[message_id]["tg_message_id"] = msg_id
                except Exception as e2:
                    logger.error(f"Не удалось отправить резервное сообщение принятой заявки: {e2}")
        if chat_id is not None and msg_id is not None:
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
        accepted = accepted_requests.get(message_id)
        if accepted:
            await query.answer(f"Заявку уже принял @{accepted['username']}.", show_alert=True)
            if query.message and (
                query.message.chat_id != accepted.get("chat_id")
                or query.message.message_id != accepted.get("tg_message_id")
            ):
                await _cleanup_stale_master_message(query)
            return
        if _is_accepted_master_message(getattr(query, "message", None)):
            await query.answer("Заявка уже принята.", show_alert=True)
            return
        req = active_requests.get(message_id)
        if not req or req.get("transitioning") or (
            req.get("tg_message_id") and query.message and (
                query.message.message_id != req["tg_message_id"]
                or query.message.chat_id != req.get("chat_id")
            )
        ):
            await query.answer("Заявка уже неактивна.", show_alert=True)
            await _cleanup_stale_master_message(query)
            return
        req["transitioning"] = True
        if req["timer"]:
            req["timer"].cancel()
        await query.answer()
        await _delete_master_message(query.message)
        req["chat_id"] = None
        req["tg_message_id"] = None
        req["current_group_index"] += 1
        groups = req["groups"]
        if req["current_group_index"] >= len(groups):
            active_requests.pop(message_id, None)
            await bot.send_message(
                chat_id=GENERAL_GROUP,
                text=f"⚠️ Никто не принял заявку:\n\n{req['text']}"
            )
        else:
            next_group = groups[req["current_group_index"]]
            try:
                sent = await send_request_with_buttons(next_group, req["text"], message_id)
            except Exception:
                active_requests.pop(message_id, None)
                raise
            if sent is not None:
                req["chat_id"] = getattr(sent, "chat_id", next_group)
                req["tg_message_id"] = getattr(sent, "message_id", None)
            req["transitioning"] = False
            req["timer"] = asyncio.create_task(start_timer(message_id))

async def dispatch_request(text, category_key=None):
    if "🚨 Новая заявка" not in text:
        return

    if category_key is None:
        text_for_cat = re.sub(r'\n?📞 Телефон:.*$', '', text, flags=re.MULTILINE)
        text_for_cat = re.sub(r'\n?Телефон:.*$', '', text_for_cat, flags=re.MULTILINE)
        text_for_cat = re.sub(r'\n?Номер телефона:.*$', '', text_for_cat, flags=re.MULTILINE)
        text_for_cat = re.sub(r'\n?📍 Адрес:.*$', '', text_for_cat, flags=re.MULTILINE)
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
    while message_id in active_requests:
        message_id += 1
    req = {
        "text": text,
        "current_group_index": 0,
        "groups": groups,
        "timer": None,
        "category": category_key,
        "chat_id": None,
        "tg_message_id": None,
        "transitioning": False,
    }
    active_requests[message_id] = req
    try:
        sent = await send_request_with_buttons(groups[0], text, message_id)
    except Exception:
        active_requests.pop(message_id, None)
        raise
    if sent is not None:
        req["chat_id"] = getattr(sent, "chat_id", groups[0])
        req["tg_message_id"] = getattr(sent, "message_id", None)
    if message_id in active_requests:
        req["timer"] = asyncio.create_task(start_timer(message_id))

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
    'плойка для волос', 'мультиварка', 'мультиварки', 'мультиваркой',
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
    'газовый котел', 'газовый водонагреватель', 'газовая колонка', 'газовая духовка',
    'газовая варочная', 'теплый пол', 'домофон', 'система видеонаблюдения', 'автомагнитола',
    'фотоаппарат', 'фотоаппарата', 'фотоаппаратом', 'фотовспышка', 'фото вспышка', 'зеркалка', 'объектив',
]

_EXCLUDED_STRICT_RE = re.compile(
    r'\b(?:'
    r'вытяжк\w*|фен(?:а|у|ом|е|ы|ов)?|утюг\w*|мультиварк\w*|блендер\w*|тостер\w*|'
    r'соковыжималк\w*|(?:кухонн\w*\s+)?комбайн\w*|хлебопечк\w*|йогуртниц\w*|аэрогрил\w*|'
    r'электросушилк\w*|швейн\w+\s+машин\w*|обогревател\w*|тепловентилятор\w*|конвектор\w*|'
    r'электрочайник\w*|электрокамин\w*|умн\w+\s+час\w*|фитнес[\-\s]?браслет\w*|'
    r'заправ\w+\s+картридж\w*|припаять\s+проводок|заменить\s+вилк\w*|'
    r'настроить\s+wi[\-\s]?fi|восстановить\s+данные\s+с\s+флешк\w*|'
    r'газов\w+\s+(?:плит\w*|кот[ео]л\w*|водонагревател\w*|колонк\w*|духовк\w*|варочн\w*|поверхност\w*|обогревател\w*|конвектор\w*|горелк\w*|оборудовани\w*|техник\w*)|'
    r'на\s+газу|пропан\w*|метан\w*|пахнет\s+газом|утечк\w+\s+газа|'
    r'тепл\w+\s+пол\w*|домофон\w*|систем\w*\s+видеонаблюдения|автомагнитол\w*|'
    r'фотоаппарат\w*|фотовспышк\w*|фото\s+вспышк\w*|зеркалк\w*|объектив\w*|'
    r'колонк\w*\s+bluetooth|bluetooth\s+колонк\w*|блютуз\s+колонк\w*'
    r')\b',
    re.IGNORECASE
)
_EXCLUDED_PERIPHERAL_RE = re.compile(
    r'\b(?:роутер\w*|модем\w*|пульт\w*\s+ду|внешн\w+\s+аккумулятор\w*|флешк\w*|карт\w+\s+памяти|наушник\w*|вентилятор\w*)\b',
    re.IGNORECASE
)
_EXCLUDED_PERIPHERAL_SUBJECT_RE = re.compile(
    r'\b(?:сломал\w*|не\s+работа\w*|полом\w*|ремонт\w*|почин\w*|замен\w*|нужен)\s+'
    r'(?:роутер\w*|модем\w*|пульт\w*(?:\s+ду|\s+от\s+\w+)?|флешк\w*|наушник\w*|вентилятор\w*)\b',
    re.IGNORECASE
)

def is_excluded_tech(text: str, category_key: str = "other") -> bool:
    if not text:
        return False
    # Убираем адрес и телефон, чтобы названия улиц не давали ложных срабатываний
    clean_hay = re.sub(r'^📍\s*Адрес:.*$', '', text, flags=re.MULTILINE)
    clean_hay = re.sub(r'^📞\s*Телефон:.*$', '', clean_hay, flags=re.MULTILINE)
    norm = normalize_yo(clean_hay.lower())

    if _EXCLUDED_STRICT_RE.search(norm):
        return True

    # "плойка" может означать игровую приставку PlayStation — исключаем только при явном парикмахерском контексте
    if re.search(r'\bплойк[аиеуой]+\b', norm) and re.search(
        r'\b(?:для\s+волос|волос\w*|завивк\w*|локон\w*|кудр\w*|выпрямител\w*|утюж\w*|гофре|щипц\w*|укладк\w*)\b',
        norm
    ):
        return True

    # Периферия ("пульт ду", "флешка", "роутер", "вентилятор") исключается только если это не симптом основной техники
    if _EXCLUDED_PERIPHERAL_RE.search(norm):
        if category_key in (None, "other"):
            return True
        if _EXCLUDED_PERIPHERAL_SUBJECT_RE.search(norm):
            return True

    return False

@retry_sync(max_retries=10, delay=5, backoff=2, exceptions=(socket.gaierror, socket.timeout, ConnectionError))
def _connect_to_imap(server, email, password):
    mail = imaplib.IMAP4_SSL(server, timeout=15)
    mail.login(email, password)
    mail.select("inbox")
    return mail

def _cleanup_recent_phones(now=None):
    now = now or datetime.now()
    expired = [k for k, ts in recent_phones.items() if (now - ts) >= DUPLICATE_PHONE_TTL]
    for k in expired:
        recent_phones.pop(k, None)

def _imap_task():
    mail = None
    try:
        mail = _connect_to_imap(IMAP_SERVER, EMAIL, PASSWORD)
        status, data = mail.search(None, 'UNSEEN')
        if status == "OK":
            for num in data[0].split():
                should_mark_seen = False
                try:
                    typ, msg_data = mail.fetch(num, '(RFC822)')
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            sender = _decode_mime_header(msg.get("From", "неизвестный отправитель"))
                            subject = _decode_mime_header(msg.get("Subject", "Без темы"))
                            body = extract_body_text(msg)
                            if not body:
                                continue
                            body = re.sub(
                                r'Отправлено из мобильной Почты Mail.*?-------- Пересылаемое сообщение --------',
                                '', body, flags=re.DOTALL
                            ).strip()
                            body_low = body.lower()
                            sender_low = sender.lower()
                            subject_low = subject.lower()

                            is_zvonok = (
                                "zvonok.com" in sender_low
                                or "zvonok.com" in body_low
                                or ("phone:" in body_low and "call_id:" in body_low)
                            )
                            is_site = (
                                "proftech-service" in body_low
                                or "заявка с сайта" in body_low
                                or "proftech-service" in subject_low
                                or "заявка с сайта" in subject_low
                                or "web3forms" in sender_low
                                or "web3forms" in body_low
                            )

                            parsed = None
                            if is_zvonok:
                                should_mark_seen = True
                                parsed = parse_zvonok(body)
                            elif is_site:
                                should_mark_seen = True
                                parsed = parse_site(body)
                            elif "bothelp.io" in sender_low or "bothelp.io" in body_low:
                                should_mark_seen = True
                                parsed = parse_bothelp(body)
                            elif "craftum.org" in sender_low or "craftum.org" in body_low:
                                should_mark_seen = True
                                parsed = parse_craftum(body)
                            else:
                                continue

                            if parsed is None:
                                continue

                            final_text, category_key = parsed
                            excluded = is_excluded_tech(final_text, category_key)
                            if not excluded and is_zvonok:
                                _, _, _, c_lines, _ = _extract_zvonok_turns(body)
                                excluded = is_excluded_tech(' '.join(c_lines), category_key)
                            if excluded:
                                logger.info("Заявка отклонена фильтром EXCLUDED_TECH")
                                continue

                            phone_match = re.search(r'📞 Телефон:\s*(.+)', final_text)
                            if not phone_match:
                                phone_match = re.search(r'Телефон:\s*(\S+)', final_text)
                            if phone_match:
                                raw_phone = phone_match.group(1).strip()
                                digits = re.sub(r'\D', '', raw_phone)
                                if len(digits) >= 10:
                                    now = datetime.now()
                                    _cleanup_recent_phones(now)
                                    if digits in recent_phones and (now - recent_phones[digits]) < DUPLICATE_PHONE_TTL:
                                        logger.info("IMAP: заявка отклонена (дубликат телефона)")
                                        continue
                                    recent_phones[digits] = now

                            try:
                                asyncio.run_coroutine_threadsafe(send_and_dispatch(final_text, category_key), main_loop)
                            except Exception as e:
                                logger.error(f"run_coroutine_threadsafe: {e}")

                    if should_mark_seen:
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
