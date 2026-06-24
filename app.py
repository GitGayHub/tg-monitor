import time
import logging
import re
import requests
from bs4 import BeautifulSoup
from telegram import Bot, Update, ReplyKeyboardMarkup, KeyboardButton, MenuButtonCommands, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import random
import os
import sys
import argparse
import asyncio
import json
import base64
import subprocess
import html

from cfg import ConfigManager
from history import init_price_history_db, record_price_snapshot, record_red_flag, get_latest_top3
from handlers import register_settings_handlers

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# --- НАСТРОЙКИ ---
# Токены и репозиторий должны приходить только из переменных окружения.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')

# URLs с фильтром "только продажа" (несколько категорий Fortnite)
FUNPAY_ACCOUNTS_URL = 'https://funpay.com/lots/248/?offer_type=sell'
FUNPAY_URLS = [
    FUNPAY_ACCOUNTS_URL,  # Аккаунты для Fortnite (все платформы)
]

# GitHub API (для /sync — синхронизация config.json с репозиторием)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = os.environ.get('GITHUB_REPO') or os.environ.get('GITHUB_REPOSITORY')

CHAT_ID_FILE = 'chat_id.txt'
SEEN_IDS_FILE = 'seen_ids.txt'
SEEN_CACHE_FILE = 'seen_cache.json'
BANNED_IDS_FILE = 'banned_ids.txt'
BANNED_SELLERS_FILE = 'banned_sellers.txt'
SELLER_MAP_FILE = 'seller_map.json'
SENT_OFFERS_FILE = 'sent_offers.json'

def setup_logging(verbose=False):
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stdout,
            force=True
        )
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("telegram").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        logging.getLogger("apscheduler").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stdout,
            force=True
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Настройка логирования по умолчанию (без миллисекунд, тихий режим для библиотек)
setup_logging(verbose=False)
logger = logging.getLogger()
process_offers_lock = asyncio.Lock()
HTTP_TIMEOUT = (10, 20)

# Глобальные переменные
seen_ids = set()
seen_cache = {}
banned_ids = set()
banned_sellers = set()
seller_map = {}
seller_reverse_map = {}
check_run_count = 0

# Глобальный кэш деталей предложений (для минимизации запросов к FunPay)
# Схема: href → { 'full_description': str, 'rating_text': str, 'cached_at': float }
OFFER_DETAILS_CACHE = {}
OFFER_DETAILS_CACHE_TTL = 3600  # Время жизни кэша: 1 час (3600 секунд)
chat_id = os.environ.get('TELEGRAM_CHAT_ID')
bot_username = os.environ.get('TELEGRAM_BOT_USERNAME')
config = ConfigManager()  # Загружает из config.json или создаёт с дефолтами
init_price_history_db()

# Текущий режим работы бота (для отображения в меню)
bot_mode = {
    'mode': 'standard',       # standard | recheck | recheck_pve | pricetest
    'params': {},              # rare_price, pve_price, max_price
    'started_at': None,        # timestamp начала
}

def load_chat_id():
    global chat_id
    if chat_id: return
    try:
        with open(CHAT_ID_FILE, 'r') as f:
            chat_id = f.read().strip()
    except FileNotFoundError:
        chat_id = None

def save_chat_id(new_id):
    global chat_id
    with open(CHAT_ID_FILE, 'w') as f:
        f.write(str(new_id))
    chat_id = str(new_id)

SEEN_IDS_MAX = 15000

def load_seen_ids():
    global seen_ids, seen_cache
    try:
        with open(SEEN_IDS_FILE, 'r') as f:
            all_ids = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        all_ids = []
    
    seen_ids = set(all_ids)

    try:
        with open(SEEN_CACHE_FILE, 'r', encoding='utf-8') as f:
            seen_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        seen_cache = {}

    # Migrate legacy/existing text ids to the JSON seen_cache if missing
    migrated = False
    for oid in seen_ids:
        if oid not in seen_cache:
            seen_cache[oid] = [None, ""]
            migrated = True
    if migrated:
        save_seen_cache()

def save_seen_cache():
    global seen_cache
    if len(seen_cache) > SEEN_IDS_MAX:
        keys = list(seen_cache.keys())
        trimmed_keys = keys[-SEEN_IDS_MAX:]
        seen_cache = {k: seen_cache[k] for k in trimmed_keys}
    try:
        with open(SEEN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(seen_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Ошибка сохранения seen_cache: {e}")

def save_seen_id(offer_id, price=None, description=None):
    global seen_ids, seen_cache
    seen_ids.add(offer_id)
    seen_cache[offer_id] = [price, description or ""]
    try:
        with open(SEEN_IDS_FILE, 'a') as f:
            f.write(offer_id + '\n')
    except Exception as e:
        logger.warning(f"Ошибка сохранения seen_ids.txt: {e}")
    save_seen_cache()

def load_banned_ids():
    global banned_ids
    try:
        with open(BANNED_IDS_FILE, 'r') as f:
            banned_ids = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        banned_ids = set()

def save_banned_ids():
    with open(BANNED_IDS_FILE, 'w') as f:
        for offer_id in sorted(banned_ids):
            f.write(offer_id + '\n')

def clear_banned_ids():
    global banned_ids
    removed = len(banned_ids)
    banned_ids.clear()
    with open(BANNED_IDS_FILE, 'w') as f:
        f.write('')
    return removed

def load_banned_sellers():
    global banned_sellers
    try:
        with open(BANNED_SELLERS_FILE, 'r', encoding='utf-8') as f:
            banned_sellers = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        banned_sellers = set()

def save_banned_sellers():
    try:
        with open(BANNED_SELLERS_FILE, 'w', encoding='utf-8') as f:
            for seller in sorted(banned_sellers):
                f.write(seller + '\n')
    except Exception as e:
        logger.warning(f"Ошибка сохранения banned_sellers: {e}")

def clear_banned_sellers():
    global banned_sellers
    removed = len(banned_sellers)
    banned_sellers.clear()
    try:
        with open(BANNED_SELLERS_FILE, 'w', encoding='utf-8') as f:
            f.write('')
    except Exception as e:
        logger.warning(f"Ошибка очистки banned_sellers: {e}")
    return removed

def load_seller_map():
    global seller_map, seller_reverse_map
    try:
        with open(SELLER_MAP_FILE, 'r', encoding='utf-8') as f:
            seller_map = json.load(f)
            seller_reverse_map = {v: k for k, v in seller_map.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        seller_map = {}
        seller_reverse_map = {}

def save_seller_map():
    try:
        with open(SELLER_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(seller_map, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"Ошибка сохранения seller_map: {e}")

def get_or_create_seller_id(seller_name):
    if seller_name in seller_reverse_map:
        return seller_reverse_map[seller_name]
    import uuid
    short_id = uuid.uuid4().hex[:12]
    seller_map[short_id] = seller_name
    seller_reverse_map[seller_name] = short_id
    save_seller_map()
    return short_id

def get_seller_name_by_id(short_id):
    return seller_map.get(short_id)

def load_sent_offers():
    global sent_offers, sent_by_seller
    try:
        with open(SENT_OFFERS_FILE, 'r', encoding='utf-8') as f:
            sent_offers = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sent_offers = {}
    
    # Rebuild sent_by_seller index
    sent_by_seller = {}
    for oid, sent in sent_offers.items():
        sent['offer_id'] = oid
        seller = sent.get('seller')
        if seller:
            seller_key = seller.strip().lower()
            sent_by_seller.setdefault(seller_key, []).append(sent)

def save_sent_offer(offer_id, price, description, seller=None):
    global sent_offers, sent_by_seller
    record = {
        'offer_id': offer_id,
        'price': price,
        'description': description,
        'timestamp': time.time(),
        'seller': seller
    }
    sent_offers[offer_id] = record
    if seller:
        seller_key = seller.strip().lower()
        sent_by_seller.setdefault(seller_key, []).append(record)
    with open(SENT_OFFERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sent_offers, f, ensure_ascii=False, indent=2)


def clear_monitoring_state():
    """Reset seen_ids + sent_offers so auto-monitoring re-sends everything. For testing."""
    global seen_ids, seen_cache, sent_offers, sent_by_seller
    seen_ids.clear()
    seen_cache.clear()
    with open(SEEN_IDS_FILE, 'w') as f:
        f.write('')
    with open(SEEN_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)
    sent_offers.clear()
    sent_by_seller.clear()
    with open(SENT_OFFERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)
    logger.info("🧹 Сброс мониторинга: seen_ids, seen_cache, sent_offers, sent_by_seller очищены")

def parse_price(price_text):
    if not price_text:
        return None
    cleaned = re.sub(r'[^\d.,]', '', price_text)
    cleaned = cleaned.replace(',', '.')
    parts = cleaned.split('.')
    if len(parts) > 2:
        cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_offer_id(value):
    if not value:
        return None
    match = re.search(r'id=(\d+)', str(value))
    if match:
        return match.group(1)
    raw = str(value).strip()
    if re.fullmatch(r'\d+', raw):
        return raw
    return None


def build_ban_link(offer_id):
    if not offer_id or not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=ban_{offer_id}"


def normalize_match_text(text):
    if not text:
        return ""
    text = str(text).lower().replace('ё', 'е')
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def is_recently_sent(offer_id, seller, description, price_value, max_days=7):
    """Returns True if this offer (same ID or an identical re-post by the same seller)
    was already sent within max_days days and the price hasn't dropped significantly (>= 2%)."""
    global sent_offers, sent_by_seller
    now = time.time()
    seconds_limit = max_days * 24 * 3600
    norm_desc = normalize_match_text(description)

    # Gather all matching previously sent records
    matching_prices = []

    # 1. Check by exact offer_id
    if offer_id in sent_offers:
        sent = sent_offers[offer_id]
        sent_time = sent.get('timestamp', 0)
        if now - sent_time < seconds_limit:
            # Skip completely if the exact same offer ID was already sent
            return True

    # 2. Check by seller (re-post detection)
    if seller:
        seller_key = seller.strip().lower()
        if seller_key in sent_by_seller:
            for sent in sent_by_seller[seller_key]:
                sent_time = sent.get('timestamp', 0)
                if now - sent_time < seconds_limit:
                    if normalize_match_text(sent.get('description', '')) == norm_desc:
                        price = sent.get('price')
                        if price is not None:
                            matching_prices.append(price)
    else:
        # Fallback for empty/unknown seller: compare description against all sent offers
        for oid, sent in sent_offers.items():
            sent_time = sent.get('timestamp', 0)
            if now - sent_time < seconds_limit:
                sent_seller = sent.get('seller')
                if not sent_seller:
                    if normalize_match_text(sent.get('description', '')) == norm_desc:
                        price = sent.get('price')
                        if price is not None:
                            matching_prices.append(price)

    # If no matching offers were sent, then it is NOT recently sent
    if not matching_prices:
        return False

    # Find the minimum price among all sent matches
    min_sent_price = min(matching_prices)

    # Skip if current price is higher or equal to the minimum sent price
    if price_value >= min_sent_price:
        return True

    # If the price dropped, check if it dropped by at least 10% to filter out minor fluctuations
    price_drop = min_sent_price - price_value
    percent_drop = price_drop / min_sent_price
    if percent_drop < 0.10:
        return True

    return False


def contains_exclude_keyword(text, exclude_keywords, positive_keywords=None):
    """Returns the first matching exclude phrase, or None.
    If positive_keywords is provided and matches, exclude is overridden (returns None).
    Exception: if the positive match is a substring of the exclude match
    (e.g. 'full access' inside 'no full access'), the exclude still wins."""
    normalized_text = normalize_match_text(text)

    # Robust regex negation check for "no email/no access/no changes" to prevent any bypasses
    negation_patterns = [
        r'(?:не|нет|без|нету|no|not|without|оставляю|остается|остаётся|у продавца)\s+(?:доступ\w*\s+)?(?:к\s+)?(?:родительск\w*\s+)?(?:почт\w+|mail|email|емейл\w*|мыл\w+|данн\w*|перепривяз\w*|смен\w*)',
        r'(?:почт\w+|mail|email|емейл\w*|мыл\w*|парол\w*|данн\w*|перепривяз\w*|смен\w*)\s+(?:от\s+почты\s+)?(?:к\s+аккаунт\w*\s+)?(?:нет|нету|невозможна|у\s+продавца|себе|остается|остаётся|оставля\w*|оставл\w*)',
        r'(?:почт\w+|mail|email|емейл\w*|мыл\w*|парол\w*|данн\w*|перепривяз\w*|смен\w*)\s+(?:от\s+почты\s+)?(?:к\s+аккаунт\w*\s+)?(?:не|нет|без)\s+(?:идет|идёт|передается|передаётся|дается|даётся|предоставляется|доступна|будет|отдаю|даю|меняется|меняются|возможна|включен\w*|смен\w*|перепривяз\w*|доступ\w*)'
    ]
    for pattern in negation_patterns:
        match = re.search(pattern, normalized_text)
        if match:
            # Critical blockers found, skip positive keyword overrides
            return match.group(0)

    # First: find if any exclude keyword matches
    matched_exclude = None
    normalized_exclude_str = None
    for exclude_word in exclude_keywords:
        ne = normalize_match_text(exclude_word)
        if ne and ne in normalized_text:
            matched_exclude = exclude_word
            normalized_exclude_str = ne
            break
    if matched_exclude is None:
        return None
    
    # Absolute blocker check: if exclude is critical (mail/linking/login), ignore positive overrides
    blocker_terms = ["почт", "mail", "привяз", "мыл", "эпик", "epic", "вход", "данные", "логин", "парол", "доступ", "емейл"]
    if any(term in normalized_exclude_str for term in blocker_terms):
        return matched_exclude

    # Second: check if a positive keyword overrides the exclude
    if positive_keywords:
        for pos_word in positive_keywords:
            normalized_pos = normalize_match_text(pos_word)
            if normalized_pos and normalized_pos in normalized_text:
                # Don't let positive override if it's a substring of the matched exclude
                # e.g. 'full access' should NOT override 'no full access'
                if normalized_pos in normalized_exclude_str:
                    continue
                # Negation check: if positive is preceded by negation (не, нет, без, no, not, without), it cannot override
                pattern = r'\b(?:не|нет|без|no|not|without)\s+' + re.escape(normalized_pos)
                if re.search(pattern, normalized_text):
                    continue
                return None  # Positive overrides exclude
    return matched_exclude


def _keyword_word_match(normalized_kw, text):
    """Word-boundary match: 'еон' matches 'еон' but not 'неоновая'."""
    if not normalized_kw:
        return False
    pattern = r'(?:^|\b|\s|[^\w])' + re.escape(normalized_kw) + r'(?:$|\b|\s|[^\w])'
    return bool(re.search(pattern, text))


def _keywords_match_text(keywords, normalized_text):
    for kw in keywords:
        normalized_kw = normalize_match_text(kw)
        if normalized_kw and _keyword_word_match(normalized_kw, normalized_text):
            return kw
    return None


def find_skins_in_text(text, skins_dict=None):
    """Находит все редкие скины в тексте. Использует skins_dict из config."""
    if skins_dict is None:
        skins_dict = config.get_enabled_skins_dict()
    # Normalize: replace zero-width chars with space, collapse multiple spaces
    text_lower = re.sub(r'[\u200b\u200c\u200d\ufeff]', ' ', text.lower())
    text_lower = re.sub(r'\s+', ' ', text_lower)
    found_skins = []

    for skin_id, skin_data in skins_dict.items():
        for keyword in skin_data['keywords']:
            kw_lower = keyword.lower()
            if kw_lower in ('еон', 'eon', 'эон'):
                pattern = r'\b(?<![нn])' + re.escape(kw_lower) + r'(?![оo])\b'
            else:
                pattern = r'\b' + re.escape(kw_lower) + r'\b'
            if re.search(pattern, text_lower):

                found_skins.append({
                    'id': skin_id,
                    'keyword': keyword,
                    'price': skin_data['price'],
                    'require_pve': skin_data.get('require_pve', False)
                })
                break
    return found_skins

def has_pve(text, include_unconfirmed=False):
    """Проверяет, есть ли PVE в тексте. Читает слова из config."""
    normalized_text = normalize_match_text(text)
    keywords = config.get_all_pve() if include_unconfirmed else config.get_confirmed_pve()
    for keyword in keywords:
        if normalize_match_text(keyword) in normalized_text:
            return True
    return False

def has_new_pve(text):
    """Проверяет, указано ли НОВОЕ PVE/STW (исключаем)."""
    text_lower = text.lower()
    for keyword in config.get_new_pve():
        if keyword.lower() in text_lower:
            return True
    return False

def get_main_feature(found_skins, has_pve_flag, rare_override=None):
    if found_skins:
        def skin_price(skin):
            return rare_override if rare_override is not None else skin['price']
        best_skin = max(found_skins, key=skin_price)
        return best_skin['keyword']
    if has_pve_flag:
        return "PVE"
    return "Нет"

def calculate_max_price(found_skins, has_pve_flag, rare_override=None, pve_override=None):
    """Рассчитывает максимальную цену — сумма 2-х самых дорогих секций.
    
    PVE бонус добавляется ТОЛЬКО если:
    - Есть подтверждённый PVE в лоте
    - Хотя бы один скин НЕ требует обяз. PVE (require_pve=False)
    Если скин требует PVE — его цена уже включает PVE.
    """
    pve_bonus = config.pve_bonus
    items = []

    # BP S2 иерархия: Black Knight > Sparkle Specialist > Floss
    bp_ids = {'black_knight', 'sparkle_specialist', 'floss'}
    found_bp = {s['id'] for s in found_skins if s['id'] in bp_ids}
    bp_remove = set()
    if 'black_knight' in found_bp:
        bp_remove.update({'sparkle_specialist', 'floss'})
    elif 'sparkle_specialist' in found_bp:
        bp_remove.add('floss')
    if bp_remove:
        found_skins = [s for s in found_skins if s['id'] not in bp_remove]

    twitch_skins = []
    other_skins = []

    for skin in found_skins:
        if skin['id'] == 'twitch_prime':
            twitch_skins.append(skin)
        else:
            other_skins.append(skin)
            skin_price = rare_override if rare_override is not None else skin['price']
            items.append({'name': skin['keyword'], 'price': skin_price})

    # PVE бонус: только для скинов без обяз. PVE, у которых нашёлся подтв. PVE
    has_non_pve_skin = any(not s.get('require_pve', False) for s in found_skins)

    pve_used = False
    if twitch_skins:
        if has_pve_flag:
            twitch_price = pve_override if pve_override is not None else 1600
            items.append({'name': 'Twitch Prime + PVE', 'price': twitch_price})
            pve_used = True

    if has_pve_flag and not pve_used and has_non_pve_skin:
        if pve_override is not None:
            items.append({'name': 'PVE бонус', 'price': pve_override})
        elif len(items) > 0:
            items.append({'name': 'PVE бонус', 'price': pve_bonus})
        else:
            items.append({'name': 'PVE', 'price': 1000})

    if not items:
        return 0, "Нет ценных слотов"

    items.sort(key=lambda x: x['price'], reverse=True)
    top_items = items[:2]
    total_price = sum(item['price'] for item in top_items)
    description = f"{total_price}₽"
    return total_price, description

def get_random_user_agent():
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    return random.choice(user_agents)


def make_progress_bar(done, total, width=12):
    total = max(total, 1)
    done = max(0, min(done, total))
    pct = int(done / total * 100)
    filled = min(width, int(round(done / total * width)))
    return ('▰' * filled) + ('▱' * (width - filled)), pct


def main_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔎 Проверка")],
            [KeyboardButton("⏹ Стоп")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def requests_retry_get(session, url, max_retries=4, initial_backoff=3, **kwargs):
    """Makes a GET request with automatic retries for 5xx/429 status codes or network exceptions."""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                logger.warning(f"Повторный запрос ({attempt}/{max_retries}) к {url}...")
            response = session.get(url, **kwargs)
            if response.status_code in (502, 503, 504, 429, 500):
                response.raise_for_status()
            return response
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Не удалось выполнить GET-запрос к {url} после {max_retries} попыток: {e}")
                raise e
            sleep_time = (initial_backoff * (2 ** (attempt - 1))) + random.uniform(1.0, 3.0)
            logger.warning(f"Ошибка запроса к {url}: {e}. Ожидание {sleep_time:.1f}с перед повтором...")
            time.sleep(sleep_time)

def build_http_session():
    session = requests.Session()
    ua_list = [
        (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            {
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        ),
        (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            {
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="119", "Google Chrome";v="119"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        ),
        (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
            {
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Microsoft Edge";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        ),
        (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            {
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"macOS"',
            }
        )
    ]
    ua, sec_headers = random.choice(ua_list)
    headers = {
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cookie': 'cy=rub'
    }
    headers.update(sec_headers)
    session.headers.update(headers)
    return session


def set_check_progress(context, **kwargs):
    if not context:
        return
    progress = context.bot_data.get('current_check_progress') or {}
    progress.update(kwargs)
    context.bot_data['current_check_progress'] = progress


def clear_check_progress(context):
    if not context:
        return
    context.bot_data.pop('current_check_progress', None)
    context.bot_data['cancel_current_check'] = False


def _clone_skin_states_for_snapshot(config_obj):
    skins = config_obj.get_all_skins()
    return {
        sid: {
            'enabled': skin.get('enabled', True),
            'price': skin.get('price', 0),
            'require_pve': skin.get('require_pve', False),
        }
        for sid, skin in skins.items()
    }


def _clone_edition_states_for_snapshot(config_obj):
    editions = config_obj.get_all_editions()
    return {
        eid: {
            'enabled': edition.get('enabled', True),
            'price': edition.get('price', 0),
        }
        for eid, edition in editions.items()
    }


def _calculate_auto_max_price(config_obj):
    enabled_skins = config_obj.get_enabled_skins_dict()
    skin_prices = [skin.get('price', 0) for skin in enabled_skins.values()]
    return max(skin_prices, default=config_obj.max_price) + config_obj.pve_bonus


def build_recheck_run_snapshot(config_obj, *, display_mode, bot_mode_key, search_mode=None,
                               include_unconfirmed_pve=False, premium_only=False,
                               max_price_override=None, rare_override=None, pve_override=None,
                               chat_id_value=None, log_view=None,
                               confirmed_pve_enabled_override=None,
                               confirmed_pve_price_override=None):
    search_mode = search_mode or config_obj.search_mode
    auto_max_price = _calculate_auto_max_price(config_obj)
    effective_max_price = max_price_override if max_price_override is not None else auto_max_price
    confirmed_pve_enabled = config_obj.confirmed_pve_enabled if confirmed_pve_enabled_override is None else bool(confirmed_pve_enabled_override)
    confirmed_pve_price = config_obj.confirmed_pve_price if confirmed_pve_price_override is None else int(confirmed_pve_price_override)
    if confirmed_pve_enabled:
        effective_max_price = max(effective_max_price, confirmed_pve_price)

    if log_view is None:
        if premium_only:
            log_view = 'premium'
        elif search_mode == 'pve_only':
            log_view = 'pve'
        else:
            log_view = 'skins'

    snapshot = {
        'run_id': str(int(time.time() * 1000)),
        'chat_id': str(chat_id_value) if chat_id_value is not None else None,
        'display_mode': display_mode,
        'bot_mode_key': bot_mode_key,
        'search_mode': search_mode,
        'include_unconfirmed_pve': include_unconfirmed_pve,
        'premium_only': premium_only,
        'max_price_override': max_price_override,
        'effective_max_price': effective_max_price,
        'rare_override': rare_override,
        'pve_override': pve_override,
        'confirmed_pve_enabled_override': confirmed_pve_enabled,
        'confirmed_pve_price_override': confirmed_pve_price,
        'log_view': log_view,
        'skin_states': _clone_skin_states_for_snapshot(config_obj),
        'edition_states': _clone_edition_states_for_snapshot(config_obj),
        'positions': [],
    }

    if premium_only or log_view == 'premium':
        for eid in ['super_deluxe', 'limited', 'ultimate']:
            edition = config_obj.get_edition(eid)
            if not edition or not edition.get('enabled', True):
                continue
            snapshot['positions'].append({
                'type': 'edition',
                'id': eid,
                'name': eid.replace('_', ' ').title(),
                'keywords': list(edition.get('keywords', [eid])),
                'limit_price': edition.get('price', 0),
            })
    elif log_view == 'pve':
        snapshot['positions'].append({
            'type': 'pve',
            'id': '__pve__',
            'name': 'Неподтв. PVE' if include_unconfirmed_pve else 'Подтв. PVE',
            'limit_price': effective_max_price,
            'keywords_any': list(config_obj.get_all_pve()),
            'keywords_confirmed': list(config_obj.get_confirmed_pve()),
        })
    else:
        for sid, skin in config_obj.get_all_skins().items():
            if not skin.get('enabled', True):
                continue
            snapshot['positions'].append({
                'type': 'skin',
                'id': sid,
                'name': sid.replace('_', ' ').title(),
                'keywords': list(skin.get('keywords', [sid])),
                'limit_price': rare_override if rare_override is not None else skin.get('price', 0),
                'require_pve': skin.get('require_pve', False),
            })
        if confirmed_pve_enabled:
            snapshot['positions'].append({
                'type': 'pve',
                'id': '__pve__',
                'name': 'Подтв. PVE',
                'limit_price': confirmed_pve_price,
                'keywords_any': list(config_obj.get_all_pve()),
                'keywords_confirmed': list(config_obj.get_confirmed_pve()),
            })

    snapshot['process_kwargs'] = {
        'skip_seen': False,
        'candidate_limit': None,
        'include_unconfirmed_pve': include_unconfirmed_pve,
        'premium_only': premium_only,
    }
    if max_price_override is not None:
        snapshot['process_kwargs']['max_price_override'] = max_price_override
    if rare_override is not None:
        snapshot['process_kwargs']['rare_override'] = rare_override
    if pve_override is not None:
        snapshot['process_kwargs']['pve_override'] = pve_override
    if confirmed_pve_enabled_override is not None:
        snapshot['process_kwargs']['confirmed_pve_enabled_override'] = bool(confirmed_pve_enabled_override)
    if confirmed_pve_price_override is not None:
        snapshot['process_kwargs']['confirmed_pve_price_override'] = int(confirmed_pve_price_override)

    return snapshot


def _format_snapshot_price(price_value):
    if price_value is None:
        return "—"
    if isinstance(price_value, float) and price_value.is_integer():
        price_value = int(price_value)
    return f"{price_value}₽"


def _pick_min_result(results):
    return results[0] if results else None


def init_recheck_log_state(snapshot):
    state = {}
    for position in snapshot.get('positions', []):
        state[position['id']] = {
            'type': position['type'],
            'id': position['id'],
            'name': position['name'],
            'limit_price': position.get('limit_price'),
            'require_pve': position.get('require_pve', False),
            'any_offer': None,
            'pve_offer': None,
            'sent_count': 0,
        }
    return state


def update_recheck_log_offer(entry, key, price_value, price_text, href):
    current = entry.get(key)
    if current is not None and current.get('price') is not None and current['price'] <= price_value:
        return
    entry[key] = {
        'price': price_value,
        'price_text': price_text,
        'href': href,
    }


def _record_cached_recheck_history(position, item_state):
    any_offer = item_state.get('any_offer')
    pve_offer = item_state.get('pve_offer')

    if position['type'] == 'edition':
        record_price_snapshot(
            'edition',
            position['id'],
            position['name'],
            'any',
            [any_offer] if any_offer else [],
            source='recheck_log'
        )
        return

    if position['type'] == 'pve':
        record_price_snapshot(
            'pve',
            position['id'],
            position['name'],
            'confirmed',
            [offer for offer in (pve_offer, any_offer) if offer],
            source='recheck_log'
        )
        return

    history_mode = 'pve' if position.get('require_pve', False) else 'any'
    results_for_history = [pve_offer] if history_mode == 'pve' else [any_offer]
    if history_mode != 'pve' and not any_offer and pve_offer:
        results_for_history = [pve_offer]
    record_price_snapshot(
        'skin',
        position['id'],
        position['name'],
        history_mode,
        [offer for offer in results_for_history if offer],
        source='recheck_log'
    )


def build_cached_recheck_log(snapshot):
    positions = snapshot.get('positions', [])
    sent_position_ids = set(snapshot.get('sent_position_ids', []))
    log_state = snapshot.get('log_state') or {}
    diagnostics = []

    for position in positions:
        item_state = log_state.get(position['id'], {})
        limit_price = position.get('limit_price')
        any_offer = item_state.get('any_offer')
        pve_offer = item_state.get('pve_offer')
        sent_count = item_state.get('sent_count', 0)

        if position['type'] == 'edition':
            if sent_count or position['id'] in sent_position_ids:
                reason_text = f"✅ Отправлено: {max(sent_count, 1)}"
            elif any_offer and any_offer['price'] <= limit_price:
                reason_text = "✅ Подходит по цене"
            elif any_offer:
                reason_text = "💸 Дороже вашей цены"
            else:
                reason_text = "❌ Не найдено"

            diagnostics.append({
                'type': 'edition',
                'id': position['id'],
                'name': position['name'],
                'my_price_text': _format_snapshot_price(limit_price),
                'any_offer': any_offer,
                'pve_offer': None,
                'reason_text': reason_text,
            })
            continue

        if position['type'] == 'pve':
            if sent_count or position['id'] in sent_position_ids:
                reason_text = f"✅ Отправлено: {max(sent_count, 1)}"
            elif pve_offer and pve_offer['price'] <= limit_price:
                reason_text = "✅ Подходит по цене"
            elif pve_offer:
                reason_text = "💸 С PVE дороже вашей цены"
            elif any_offer:
                reason_text = "❔ Есть только неподтвержденное PVE"
            else:
                reason_text = "❌ Не найдено"

            diagnostics.append({
                'type': 'pve',
                'id': position['id'],
                'name': position['name'],
                'my_price_text': _format_snapshot_price(limit_price),
                'any_offer': any_offer,
                'pve_offer': pve_offer,
                'reason_text': reason_text,
            })
            continue

        require_pve = position.get('require_pve', False)
        if sent_count or position['id'] in sent_position_ids:
            reason_text = f"✅ Отправлено: {max(sent_count, 1)}"
        elif require_pve:
            if pve_offer and pve_offer['price'] <= limit_price:
                reason_text = "✅ Подходит по цене"
            elif pve_offer:
                reason_text = "💸 С PVE дороже вашей цены"
            elif any_offer:
                reason_text = "🔒 Есть только без PVE"
            else:
                reason_text = "❌ Не найдено"
        else:
            cheapest_offer = any_offer or pve_offer
            if cheapest_offer and cheapest_offer['price'] <= limit_price:
                reason_text = "✅ Подходит по цене"
            elif any_offer:
                reason_text = "💸 Без PVE дороже вашей цены"
            elif pve_offer:
                reason_text = "💸 С PVE дороже вашей цены"
            else:
                reason_text = "❌ Не найдено"

        diagnostics.append({
            'type': 'skin',
            'id': position['id'],
            'name': position['name'],
            'my_price_text': _format_snapshot_price(limit_price),
            'any_offer': any_offer,
            'pve_offer': pve_offer,
            'reason_text': reason_text,
            'require_pve': require_pve,
        })

    return diagnostics


async def build_cached_recheck_log_async(snapshot, progress_callback=None):
    positions = snapshot.get('positions', [])
    total = len(positions)
    log_state = snapshot.setdefault('log_state', init_recheck_log_state(snapshot))

    for idx, position in enumerate(positions, start=1):
        if progress_callback:
            await progress_callback(idx - 1, total, position['name'])

        item_state = log_state.setdefault(position['id'], {
            'type': position['type'],
            'id': position['id'],
            'name': position['name'],
            'limit_price': position.get('limit_price'),
            'require_pve': position.get('require_pve', False),
            'any_offer': None,
            'pve_offer': None,
            'sent_count': 0,
        })

        if position['type'] == 'edition':
            if not item_state.get('any_offer'):
                any_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=False))
                if any_result:
                    update_recheck_log_offer(item_state, 'any_offer', any_result['price'], any_result['price_text'], any_result['href'])
            _record_cached_recheck_history(position, item_state)

        elif position['type'] == 'pve':
            if not item_state.get('any_offer'):
                any_result = _pick_min_result(await search_min_price(position['keywords_any'], require_pve=False))
                if any_result:
                    update_recheck_log_offer(item_state, 'any_offer', any_result['price'], any_result['price_text'], any_result['href'])
            if not item_state.get('pve_offer'):
                pve_result = _pick_min_result(await search_min_price(position['keywords_confirmed'], require_pve=False))
                if pve_result:
                    update_recheck_log_offer(item_state, 'pve_offer', pve_result['price'], pve_result['price_text'], pve_result['href'])
            _record_cached_recheck_history(position, item_state)

        else:
            if not item_state.get('any_offer'):
                any_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=False))
                if any_result:
                    update_recheck_log_offer(item_state, 'any_offer', any_result['price'], any_result['price_text'], any_result['href'])
            if not item_state.get('pve_offer'):
                pve_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=True))
                if pve_result:
                    update_recheck_log_offer(item_state, 'pve_offer', pve_result['price'], pve_result['price_text'], pve_result['href'])
            _record_cached_recheck_history(position, item_state)

        if progress_callback:
            await progress_callback(idx, total, position['name'])

    snapshot['log_state'] = log_state
    snapshot['log_items'] = build_cached_recheck_log(snapshot)
    return snapshot['log_items']


def build_running_check_status(context):
    progress = context.bot_data.get('current_check_progress') or {}
    mode_info = context.bot_data.get('bot_mode', {}) or {}
    mode = mode_info.get('mode', 'standard')
    params = mode_info.get('params', {}) or {}
    auto_mode_label = 'По списку'
    running_now = params.get('display_mode') or ('Автомониторинг' if mode == 'standard' else 'Проверка')
    target_label = params.get('target_label')
    done = progress.get('done', 0)
    total = max(progress.get('total', 1), 1)
    sent = progress.get('sent', 0)
    stage = progress.get('stage') or "Подготовка"
    current = progress.get('current') or "Ожидание данных"
    bar, pct = make_progress_bar(done, total)

    target_line = f"🧩 Цель: {target_label}\n" if target_label else ""
    text = (
        f"⏳ <b>Сейчас уже идёт проверка</b>\n\n"
        f"🔍 Автомониторинг: {auto_mode_label}\n"
        f"⚙️ Запущен сейчас: {running_now}\n"
        f"{target_line}"
        f"📍 Этап: {stage}\n"
        f"{bar} {pct}%\n"
        f"📦 Прогресс: {done}/{total}\n"
        f"🔎 Сейчас: {current}\n"
        f"✅ Отправлено: {sent}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
        [InlineKeyboardButton("⏹ Завершить принудительно", callback_data="set:checkstop")]
    ])
    return text, keyboard

def _sync_get_listings():
    """Синхронный HTTP-запрос главных страниц (вызывается через asyncio.to_thread)."""
    all_items = []
    seen_hrefs = set()
    with build_http_session() as session:
        for url in FUNPAY_URLS:
            source_lot = 'prochee' if '1098' in url else 'accounts'
            try:
                response = requests_retry_get(session, url, timeout=HTTP_TIMEOUT)
                soup = BeautifulSoup(response.text, 'html.parser')
                items = soup.find_all('a', class_='tc-item')
                for item in items:
                    href = item.get('href', '')
                    if href not in seen_hrefs:
                        seen_hrefs.add(href)
                        item['data-source-lot'] = source_lot
                        all_items.append(item)
                logger.debug(f"📄 {url} [{source_lot}]: {len(items)} предложений")
                if url != FUNPAY_URLS[-1]:
                    time.sleep(random.uniform(0.5, 1.0))
            except Exception as e:
                logger.error(f"Ошибка при запросе к {url}: {e}")
    return all_items

async def get_listings():
    """Получает список товаров — НЕ блокирует event loop."""
    return await asyncio.to_thread(_sync_get_listings)

def _sync_get_offer_details(offer_url):
    """Синхронный HTTP-запрос деталей предложения (вызывается через asyncio.to_thread)."""
    try:
        delay = random.uniform(config.request_delay_min, config.request_delay_max)
        time.sleep(delay)
        with build_http_session() as session:
            session.headers.update({'Referer': 'https://funpay.com/'})
            response = requests_retry_get(session, offer_url, timeout=HTTP_TIMEOUT)
            soup = BeautifulSoup(response.text, 'html.parser')

        full_description = ""

        # Удаляем отзывы из HTML, чтобы слова из отзывов не попадали в описание
        review_classes = ['review-list', 'review-container', 'review-item',
                          'review-item-text', 'reviews-filter']
        for cls in review_classes:
            for el in soup.find_all(True, class_=cls):
                el.decompose()

        all_text_blocks = soup.find_all(['div', 'p', 'span'])
        for block in all_text_blocks:
            text = block.get_text(strip=True)
            if 'ПОДРОБНОЕ ОПИСАНИЕ' in text.upper() or 'КРАТКОЕ ОПИСАНИЕ' in text.upper():
                parent = block.find_parent('div')
                if parent:
                    full_description = parent.get_text(separator=' ', strip=True)
                    break

        if not full_description:
            desc_block = (
                soup.find('div', class_='offer-description') or
                soup.find('div', class_='lot-description') or
                soup.find('div', class_='param-item') or
                soup.find('div', class_='lot-info')
            )
            if desc_block:
                full_description = desc_block.get_text(separator=' ', strip=True)

        if not full_description:
            main_content = soup.find('div', class_='content') or soup.find('main') or soup.find('body')
            if main_content:
                full_description = main_content.get_text(separator=' ', strip=True)

        rating_score = None
        reviews_count = None
        reviews_period = None

        page_text = soup.get_text()

        rating_match = re.search(r'(\d+[.,]?\d*)\s*из\s*5', page_text)
        if rating_match:
            try:
                rating_score = float(rating_match.group(1).replace(',', '.'))
            except:
                pass

        reviews_match = re.search(r'(\d+)\s*(?:отзыв\w*)\s*(?:за\s*(.+?(?:год|лет|года|месяц\w*|недел\w*|дн\w*)))?', page_text, re.IGNORECASE)
        if reviews_match:
            reviews_count = int(reviews_match.group(1))
            if reviews_match.group(2):
                reviews_period = reviews_match.group(2).strip()

        rating_elem = soup.find('span', class_='rating-mini-stars') or soup.find('div', class_='rating-stars')
        if rating_elem and not rating_score:
            data_rating = rating_elem.get('data-rating')
            if data_rating:
                try:
                    rating_score = float(data_rating)
                except:
                    pass

        rating_text = ""
        if rating_score and rating_score > 0:
            rating_text = f"{rating_score} из 5"
            if reviews_count and reviews_count > 0:
                rating_text += f" ({reviews_count} отзыв"
                if reviews_count % 10 == 1 and reviews_count % 100 != 11:
                    rating_text += ""
                elif 2 <= reviews_count % 10 <= 4 and not (12 <= reviews_count % 100 <= 14):
                    rating_text += "а"
                else:
                    rating_text += "ов"
                if reviews_period:
                    rating_text += f" за {reviews_period}"
                rating_text += ")"
        elif reviews_count and reviews_count > 0:
            rating_text = f"{reviews_count} отзывов"
            if reviews_period:
                rating_text += f" за {reviews_period}"
        else:
            rating_text = "❗ 0 отзывов"

        return full_description, rating_text

    except Exception as e:
        logger.error(f"Ошибка при загрузке деталей {offer_url}: {e}")
        return None, "Ошибка загрузки"

async def get_offer_details(offer_url):
    """Получает детали предложения с кэшированием."""
    now = time.time()
    if offer_url in OFFER_DETAILS_CACHE:
        cached = OFFER_DETAILS_CACHE[offer_url]
        if now - cached['cached_at'] < OFFER_DETAILS_CACHE_TTL:
            return cached['full_description'], cached['rating_text']

    full_desc, rating = await asyncio.to_thread(_sync_get_offer_details, offer_url)

    if full_desc is not None and full_desc != "Ошибка загрузки":
        OFFER_DETAILS_CACHE[offer_url] = {
            'full_description': full_desc,
            'rating_text': rating,
            'cached_at': now
        }

    return full_desc, rating


def _sync_search_min_price(keywords, require_pve=False, exclude_confirmed_pve=False):
    """Ищет минимальную цену по ВСЕМ ключевым словам через поиск FunPay."""
    all_results = {}  # href → result (дедупликация)
    normalized_keywords = [normalize_match_text(kw) for kw in keywords if normalize_match_text(kw)]
    details_cache = {}

    def get_offer_texts(href):
        cached = details_cache.get(href)
        if cached is not None:
            return cached
        full_description, _ = _sync_get_offer_details(href)
        normalized_full = normalize_match_text(full_description or "")
        details_cache[href] = (full_description or "", normalized_full)
        return details_cache[href]

    with build_http_session() as session:
        for kw in keywords:
            search_url = f'{FUNPAY_ACCOUNTS_URL}&search={requests.utils.quote(kw)}'
            try:
                response = requests_retry_get(session, search_url, timeout=HTTP_TIMEOUT)
                soup = BeautifulSoup(response.text, 'html.parser')
                items = soup.find_all('a', class_='tc-item')

                for item in items:
                    href = item.get('href', '')
                    if href.startswith('/'):
                        href = f"https://funpay.com{href}"

                    if href in all_results:
                        continue

                    desc_div = item.find('div', class_='tc-desc-text')
                    price_div = item.find('div', class_='tc-price')
                    user_div = item.find('div', class_='media-user-name')

                    desc = desc_div.get_text(" ", strip=True) if desc_div else ""
                    price_text = price_div.get_text(strip=True) if price_div else ""
                    seller = user_div.get_text(strip=True) if user_div else "?"
                    item_text = normalize_match_text(item.get_text(" ", strip=True))

                    if 'аренда' in item_text and 'продажа' not in item_text:
                        continue

                    exclude_kws = config.get_exclude_keywords()
                    positive_kws = config.get_positive_keywords()
                    if contains_exclude_keyword(item_text, exclude_kws, positive_kws):
                        continue

                    price_value = parse_price(price_text)

                    if price_value is not None and price_value > 0:
                        normalized_kw = normalize_match_text(kw)
                        if normalized_kw and _keyword_word_match(normalized_kw, item_text):
                            # Быстрый путь: собираем кандидатов по короткому тексту,
                            # полные описания загружаем позже только для топ-N.
                            all_results[href] = {
                                'price': price_value,
                                'price_text': price_text,
                                'description': desc[:200],
                                'seller': seller,
                                'href': href,
                                'matched_kw': kw
                            }

                logger.debug(f"Мин. прайс: '{kw}' → {len(items)} предложений")
                if kw != keywords[-1]:
                    time.sleep(random.uniform(0.5, 1.0))

            except Exception as e:
                logger.error(f"Ошибка поиска мин. цены для '{kw}': {e}")

        # Fallback: только раздел аккаунтов. Мин. прайс не должен подтягивать мусор из "Прочее".
        current_best = min((r['price'] for r in all_results.values()), default=None)
        for list_url in [FUNPAY_ACCOUNTS_URL]:
            try:
                response = requests_retry_get(session, list_url, timeout=HTTP_TIMEOUT)
                soup = BeautifulSoup(response.text, 'html.parser')
                items = soup.find_all('a', class_='tc-item')

                for item in items:
                    href = item.get('href', '')
                    if href.startswith('/'):
                        href = f"https://funpay.com{href}"
                    if not href or href in all_results:
                        continue

                    desc_div = item.find('div', class_='tc-desc-text')
                    price_div = item.find('div', class_='tc-price')
                    user_div = item.find('div', class_='media-user-name')

                    desc = desc_div.get_text(" ", strip=True) if desc_div else ""
                    item_text = normalize_match_text(item.get_text(" ", strip=True))
                    price_text = price_div.get_text(strip=True) if price_div else ""
                    seller = user_div.get_text(strip=True) if user_div else "?"

                    if 'аренда' in item_text and 'продажа' not in item_text:
                        continue
                    if contains_exclude_keyword(item_text, config.get_exclude_keywords(), config.get_positive_keywords()):
                        continue

                    matched_kw = _keywords_match_text(normalized_keywords, item_text)
                    if not matched_kw:
                        continue

                    price_value = parse_price(price_text)
                    if price_value is None or price_value <= 0:
                        continue
                    if current_best is not None and price_value >= current_best:
                        continue

                    # Collect candidate; full description validated later with top-N
                    all_results[href] = {
                        'price': price_value,
                        'price_text': price_text,
                        'description': desc[:200],
                        'seller': seller,
                        'href': href,
                        'matched_kw': matched_kw if isinstance(matched_kw, str) else keywords[0],
                    }
                    current_best = min(current_best, price_value) if current_best is not None else price_value

            except Exception as e:
                logger.error(f"Ошибка fallback-скана мин. цены для '{list_url}': {e}")

    results = sorted(all_results.values(), key=lambda x: x['price'])

    # Validate top candidates by loading full descriptions (both PVE and non-PVE).
    # This keeps the search fast: only top-N get HTTP requests, not all matches.
    exclude_kws = config.get_exclude_keywords()
    positive_kws = config.get_positive_keywords()
    pve_tokens = [normalize_match_text(pk) for pk in config.get_confirmed_pve()] if (require_pve or exclude_confirmed_pve) else []
    validated = []
    for candidate in results[:8]:
        href = candidate['href']
        if href not in details_cache:
            try:
                get_offer_texts(href)
                time.sleep(random.uniform(0.25, 0.5))
            except Exception as e:
                logger.warning(f"Мін. прайс: не удалось открыть описание {href}: {e}")
        full_desc, full_text = details_cache.get(href, ("", ""))
        if full_text and contains_exclude_keyword(full_text, exclude_kws, positive_kws):
            logger.debug(f"Мін. прайс: отфильтровано по описанию — {href}")
            continue

        combined = f"{normalize_match_text(candidate.get('description',''))} {full_text}"
        if require_pve:
            if not any(token in combined for token in pve_tokens if token):
                continue
        if exclude_confirmed_pve:
            if any(token in combined for token in pve_tokens if token):
                continue

        if full_desc:
            candidate['description'] = full_desc[:200]
        validated.append(candidate)
        if len(validated) >= 1:
            break
    return validated


async def search_min_price(keywords, require_pve=False, exclude_confirmed_pve=False):
    """Поиск минимальной цены по всем ключевым словам — НЕ блокирует event loop."""
    return await asyncio.to_thread(_sync_search_min_price, keywords, require_pve, exclude_confirmed_pve)


def _sync_diagnostic_search(skin_searches, time_budget=120):
    """Быстрый пакетный поиск для диагностического отчёта.

    ВАЖНО: FunPay игнорирует параметр &search= в URL — поиск на сайте
    работает через JavaScript на клиенте. Поэтому мы:
    1. Загружаем страницу ОДИН раз (получаем все ~2000 лотов)
    2. Сканируем ВСЕ лоты на ВСЕ ключевые слова всех скинов одновременно
    3. Валидируем самые дешёвые кандидаты (открываем детали)

    Args:
        skin_searches: список (skin_id, keywords, require_pve)
        time_budget: макс. секунд на все поиски
    Returns:
        dict: {skin_id: {'validated': [...], 'best_without_pve': {...} или None}}
    """
    start_time = time.monotonic()
    results = {}

    exclude_kws = config.get_exclude_keywords()
    positive_kws = config.get_positive_keywords()
    pve_confirmed = config.get_confirmed_pve()

    # Построим индекс: normalized_keyword → [(skin_id, require_pve), ...]
    keyword_index = {}  # normalized_kw → [(sid, require_pve)]
    for sid, keywords, require_pve in skin_searches:
        for kw in keywords:
            nkw = normalize_match_text(kw)
            if nkw and len(nkw) >= 3:  # Игнорируем слишком короткие
                keyword_index.setdefault(nkw, []).append((sid, require_pve))
        results[sid] = {'validated': [], 'best_without_pve': None}

    # Кандидаты: sid → list of {price, price_text, desc, seller, href}
    candidates_map = {}  # sid → [{...}, ...]

    with build_http_session() as session:
        session.headers.update({'Referer': 'https://funpay.com/'})

        # === ШАГ 1: Загрузить ВСЕ лоты одним запросом (с ретраями) ===
        try:
            response = requests_retry_get(session, FUNPAY_ACCOUNTS_URL, timeout=30)
            soup = BeautifulSoup(response.text, 'html.parser')
            all_items = soup.find_all('a', class_='tc-item')
            if not all_items:
                logger.error("Диаг: не удалось найти лоты на странице")
                return None
            logger.info(f"Диаг: загружено {len(all_items)} лотов за {time.monotonic() - start_time:.1f}с")
        except Exception as e:
            logger.error(f"Диаг: не удалось загрузить лоты после всех попыток: {e}")
            return None

        # === ШАГ 2: Сканируем ВСЕ лоты, ищем ВСЕ скины одновременно ===
        for item in all_items:
            href = item.get('href', '')
            if href.startswith('/'):
                href = f"https://funpay.com{href}"

            desc_div = item.find('div', class_='tc-desc-text')
            desc = desc_div.get_text(" ", strip=True) if desc_div else ""
            item_text = normalize_match_text(desc)

            # Быстрая проверка: аренда
            if 'аренда' in item_text and 'продажа' not in item_text:
                continue

            # Быстрая проверка: исключения по краткому тексту
            if contains_exclude_keyword(item_text, exclude_kws, positive_kws):
                continue

            # Извлекаем цену
            price_div = item.find('div', class_='tc-price')
            price_text = price_div.get_text(strip=True) if price_div else ""
            price_value = parse_price(price_text)
            if price_value is None or price_value <= 0:
                continue

            user_div = item.find('div', class_='media-user-name')
            seller = user_div.get_text(strip=True) if user_div else "?"

            # Проверяем ВСЕ ключевые слова — какие скины упоминаются?
            matched_sids = set()
            matched_kw_for_sid = {}
            for nkw, sid_list in keyword_index.items():
                if nkw in ('еон', 'eon', 'эон'):
                    pattern = r'(?:^|\b|\s|[^\w])(?<![нn])' + re.escape(nkw) + r'(?![оo])(?:$|\b|\s|[^\w])'
                    matches = bool(re.search(pattern, item_text))
                else:
                    pattern = r'(?:^|\b|\s|[^\w])' + re.escape(nkw) + r'(?:$|\b|\s|[^\w])'
                    matches = bool(re.search(pattern, item_text))
                
                if matches:
                    for sid, _ in sid_list:
                        if sid not in matched_sids:
                            matched_sids.add(sid)
                            matched_kw_for_sid[sid] = nkw


            # Добавляем кандидата для каждого совпавшего скина
            for sid in matched_sids:
                candidates_map.setdefault(sid, []).append({
                    'price': price_value,
                    'price_text': price_text,
                    'description': desc[:200],
                    'seller': seller,
                    'href': href,
                    'matched_kw': matched_kw_for_sid.get(sid, '?')
                })

        scan_time = time.monotonic() - start_time
        found_sids = [sid for sid, cands in candidates_map.items() if cands]
        logger.info(f"Диаг: скан {len(all_items)} лотов за {scan_time:.1f}с → "
                     f"найдено {len(found_sids)}/{len(skin_searches)} скинов")

        # === ШАГ 3: Валидация — открываем детали самых дешёвых кандидатов ===
        edition_ids = set(config.get_all_editions().keys())
        for sid, keywords, require_pve in skin_searches:
            if time.monotonic() - start_time >= time_budget:
                logger.warning(f"⏱ Диагностика: лимит {time_budget}с исчерпан")
                break

            cands = candidates_map.get(sid, [])
            if not cands:
                status_icon = '❌'
                logger.info(f"Диаг [{sid}]: {status_icon} — 0 кандидатов ({time.monotonic() - start_time:.1f}с)")
                continue

            sorted_cands = sorted(cands, key=lambda x: x['price'])
            pve_tokens = [normalize_match_text(pk) for pk in pve_confirmed]
            is_pve_entry = sid in ('__pve__', '__unconfirmed_pve__')
            is_edition = sid in edition_ids

            best_with_pve = None
            best_without_pve = None
            max_check = 5

            for candidate in sorted_cands[:max_check]:
                if time.monotonic() - start_time >= time_budget:
                    break
                href = candidate['href']
                try:
                    time.sleep(0.15)
                    resp = requests_retry_get(session, href, timeout=HTTP_TIMEOUT)
                    detail_soup = BeautifulSoup(resp.text, 'html.parser')

                    # Удаляем отзывы
                    for cls in ['review-list', 'review-container', 'review-item']:
                        for el in detail_soup.find_all(True, class_=cls):
                            el.decompose()

                    full_description = ""
                    for block in detail_soup.find_all(['div', 'p', 'span']):
                        text = block.get_text(strip=True)
                        if 'ПОДРОБНОЕ ОПИСАНИЕ' in text.upper() or 'КРАТКОЕ ОПИСАНИЕ' in text.upper():
                            parent = block.find_parent('div')
                            if parent:
                                full_description = parent.get_text(separator=' ', strip=True)
                                break
                    if not full_description:
                        desc_block = (
                            detail_soup.find('div', class_='offer-description') or
                            detail_soup.find('div', class_='lot-description') or
                            detail_soup.find('div', class_='param-item') or
                            detail_soup.find('div', class_='lot-info')
                        )
                        if desc_block:
                            full_description = desc_block.get_text(separator=' ', strip=True)

                    full_text = normalize_match_text(full_description or "")
                except Exception as e:
                    logger.warning(f"Диаг [{sid}]: не удалось открыть {href}: {e}")
                    continue

                if full_text and contains_exclude_keyword(full_text, exclude_kws, positive_kws):
                    continue

                combined = f"{normalize_match_text(candidate.get('description', ''))} {full_text}"
                if not is_pve_entry and not is_edition:
                    # Verify that the skin is actually present in the combined description
                    # using the proper find_skins_in_text logic!
                    skins_dict_for_validation = config.get_enabled_skins_dict()
                    if sid in skins_dict_for_validation:
                        found_skins = find_skins_in_text(combined, {sid: skins_dict_for_validation[sid]})
                        if not found_skins:
                            logger.debug(f"Диаг [{sid}]: отфильтровано по валидации скина — {href}")
                            continue

                has_pve_flag = any(token in combined for token in pve_tokens if token)

                cand_copy = candidate.copy()

                if full_description:
                    cand_copy['description'] = full_description[:200]

                if is_edition:
                    best_with_pve = cand_copy
                    break

                if sid == '__pve__':
                    if has_pve(combined, include_unconfirmed=False):
                        best_with_pve = cand_copy
                        break
                    continue

                if sid == '__unconfirmed_pve__':
                    if has_pve(combined, include_unconfirmed=True) and not has_pve(combined, include_unconfirmed=False):
                        best_with_pve = cand_copy
                        break
                    continue

                if has_pve_flag:
                    if best_with_pve is None:
                        best_with_pve = cand_copy
                else:
                    if best_without_pve is None:
                        best_without_pve = cand_copy

                # Если нашли обе позиции, прекращаем поиск для этого скина
                if best_with_pve is not None and best_without_pve is not None:
                    break

            results[sid] = {
                'validated': [best_with_pve] if best_with_pve else [],
                'best_without_pve': best_without_pve
            }
            status_icon = '✅' if best_with_pve else ('⚠️ без PVE' if best_without_pve else '❌')
            n_cands = len(cands)
            logger.info(f"Диаг [{sid}]: {status_icon} ({n_cands} канд, {time.monotonic() - start_time:.1f}с)")

    elapsed = time.monotonic() - start_time
    logger.info(f"🔍 Диагностический поиск: {len(results)}/{len(skin_searches)} скинов за {elapsed:.1f}с")
    return results


async def diagnostic_search(skin_searches, time_budget=120):
    """Async обёртка для диагностического поиска."""
    return await asyncio.to_thread(_sync_diagnostic_search, skin_searches, time_budget)



async def process_offers(bot_instance=None, context=None, skip_seen=True, max_price_override=None,
                         rare_override=None, pve_override=None, candidate_limit=15,
                         include_unconfirmed_pve=False, premium_only=False,
                         confirmed_pve_enabled_override=None, confirmed_pve_price_override=None):
    """Основная функция обработки предложений."""
    global seen_ids, chat_id, bot_mode

    if not chat_id:
        logger.info("Chat ID не найден. Запустите /start в боте.")
        return 0

    if process_offers_lock.locked():
        if skip_seen:
            logger.info("⏭️ Фоновая проверка пропущена: предыдущая ещё не завершилась")
            return 0

        current_origin = context.bot_data.get('current_check_origin') if context else None
        if current_origin == 'background':
            logger.info("⏹️ Ручная проверка вытесняет автомониторинг")
            if context:
                context.bot_data['cancel_current_check'] = True

            wait_started = time.monotonic()
            while process_offers_lock.locked():
                await asyncio.sleep(0.2)
                if time.monotonic() - wait_started > 45:
                    logger.info("⏳ Не удалось вовремя остановить автомониторинг для ручной проверки")
                    return -1

            logger.info("✅ Автомониторинг остановлен, запускаю ручную проверку")
        else:
            logger.info("⏳ Ручная проверка не запущена: уже выполняется другая проверка")
            return -1

    async with process_offers_lock:
        return await _process_offers_impl(
            bot_instance=bot_instance,
            context=context,
            skip_seen=skip_seen,
            max_price_override=max_price_override,
            rare_override=rare_override,
            pve_override=pve_override,
            candidate_limit=candidate_limit,
            include_unconfirmed_pve=include_unconfirmed_pve,
            premium_only=premium_only,
            confirmed_pve_enabled_override=confirmed_pve_enabled_override,
            confirmed_pve_price_override=confirmed_pve_price_override
        )


async def _process_offers_impl(bot_instance=None, context=None, skip_seen=True, max_price_override=None,
                               rare_override=None, pve_override=None, candidate_limit=10,
                               include_unconfirmed_pve=False, premium_only=False,
                               confirmed_pve_enabled_override=None, confirmed_pve_price_override=None):
    """Реальная обработка предложений под внешней блокировкой."""
    progress_msg = None
    progress_chat_id = chat_id if not skip_seen else None
    progress_bot = context.bot if context else (bot_instance if bot_instance else None)
    last_progress_update = 0.0
    run_snapshot = None
    log_state = None
    log_keyword_map = {}

    def cancelled():
        return bool(context and context.bot_data.get('cancel_current_check'))

    async def update_progress_message(title, stage, done, total, sent, current, force=False):
        nonlocal progress_msg, last_progress_update
        if not progress_msg or not progress_bot:
            return
        if context and context.bot_data.get('checkstop_pending'):
            return
        now = time.monotonic()
        if not force and (now - last_progress_update) < 1.0:
            return
        bar, pct = make_progress_bar(done, total)
        try:
            await progress_bot.edit_message_text(
                chat_id=progress_chat_id,
                message_id=progress_msg.message_id,
                text=(
                    f"{title}\n\n"
                    f"📍 Этап: {stage}\n"
                    f"{bar} {pct}%\n"
                    f"📦 Прогресс: {done}/{max(total, 1)}\n"
                    f"🔎 Сейчас: {current}\n"
                    f"✅ Отправлено: {sent}"
                ),
                parse_mode='HTML'
            )
            last_progress_update = now
        except Exception:
            pass

    if context:
        context.bot_data['cancel_current_check'] = False
        context.bot_data['current_check_sent_positions'] = set()
        context.bot_data['current_check_origin'] = 'background' if skip_seen else 'manual'
        run_snapshot = (context.bot_data.get('bot_mode', {}).get('params') or {}).get('run_snapshot')
        if run_snapshot and not skip_seen:
            log_state = init_recheck_log_state(run_snapshot)
            context.bot_data['current_check_log_state'] = log_state
            for position in run_snapshot.get('positions', []):
                for keyword in position.get('keywords', []):
                    log_keyword_map.setdefault(keyword.lower(), set()).add(position['id'])

    try:
        mode_parts = []
        if not skip_seen:
            mode_parts.append("RECHECK")
        if include_unconfirmed_pve:
            mode_parts.append("+PVE")
        mode = " ".join(mode_parts) if mode_parts else "СТАНДАРТ"
        if skip_seen:
            global check_run_count
            check_run_count += 1
            logger.info(f"🔍 Проверка предложений #{check_run_count}... [{mode}] (макс. цена авто)")
        else:
            logger.info(f"🔍 Проверка предложений... [{mode}] (макс. цена авто)")

        if premium_only:
            search_keywords = config.get_premium_pve()
            logger.debug(f"🏆 PREMIUM режим: ищу только издания ({len(search_keywords)} слов)")
        else:
            search_keywords = config.get_search_keywords(include_unconfirmed_pve=include_unconfirmed_pve)

        x5_mode = config.x5_mode
        is_git = os.environ.get('GITHUB_ACTIONS') == 'true'
        source_text = "GitHub автомониторинг" if is_git else "Локальный автомониторинг"
        test_summary_mode = config.data.get('test_summary_mode', False) or os.environ.get('TEST_SUMMARY_MODE') == 'true' or _get_mode_txt_value()

        search_mode = config.search_mode
        confirmed_pve_only = search_mode == 'pve_only'
        skins_dict = config.get_enabled_skins_dict()
        
        summary_stats = {}
        if premium_only:
            editions = config.get_all_editions()
            for ed_id, ed in editions.items():
                if ed.get('enabled', True):
                    summary_stats[ed_id] = {
                        'name': ed_id.replace('_', ' ').title(),
                        'status': 'Не найдено',
                        'min_price': None
                    }
        else:
            # Порядок в отчёте: неподтв. PVE → подтв. PVE → издания → скины
            # 1. Неподтверждённое PVE
            if search_mode in ('pve_only', 'skins_pve') or confirmed_pve_only:
                summary_stats['__unconfirmed_pve__'] = {
                    'name': '🧟 Неподтв. PVE',
                    'status': 'Не найдено',
                    'min_price': None
                }
            # 2. Подтверждённое PVE
            if search_mode in ('pve_only', 'skins_pve') or confirmed_pve_only:
                summary_stats['__pve__'] = {
                    'name': '🧟 Подтв. PVE',
                    'status': 'Не найдено',
                    'min_price': None
                }
            # 3. Издания (в нужном порядке)
            edition_order = ['super_deluxe', 'limited', 'ultimate']
            editions = config.get_all_editions()
            for ed_id in edition_order:
                if ed_id in editions and editions[ed_id].get('enabled', True):
                    summary_stats[ed_id] = {
                        'name': ed_id.replace('_', ' ').title(),
                        'status': 'Не найдено',
                        'min_price': None
                    }
            # 4. Скины
            for sid, skin in skins_dict.items():
                summary_stats[sid] = {
                    'name': sid.replace('_', ' ').title(),
                    'status': 'Не найдено',
                    'min_price': None
                }

        exclude_keywords = config.get_exclude_keywords()
        positive_keywords = config.get_positive_keywords()
        confirmed_pve_enabled_effective = config.confirmed_pve_enabled if confirmed_pve_enabled_override is None else bool(confirmed_pve_enabled_override)
        confirmed_pve_price_effective = config.confirmed_pve_price if confirmed_pve_price_override is None else int(confirmed_pve_price_override)
        unconfirmed_pve_price_effective = config.unconfirmed_pve_price

        if max_price_override is not None:
            effective_max_price = max_price_override
        elif confirmed_pve_only:
            effective_max_price = confirmed_pve_price_effective
            if include_unconfirmed_pve:
                effective_max_price = max(effective_max_price, unconfirmed_pve_price_effective)
        else:
            skin_prices = [s.get('price', 0) for s in skins_dict.values()]
            effective_max_price = max(skin_prices, default=config.max_price) + config.pve_bonus
            if confirmed_pve_enabled_effective:
                effective_max_price = max(effective_max_price, confirmed_pve_price_effective)
            if include_unconfirmed_pve:
                effective_max_price = max(effective_max_price, unconfirmed_pve_price_effective)

        if x5_mode:
            effective_max_price *= 5

        if rare_override is not None or pve_override is not None or max_price_override is not None:
            logger.info(f"PRICETEST: rare={rare_override}, pve={pve_override}, max_price={effective_max_price}")

        # Create progress message BEFORE loading so user sees activity immediately
        if progress_chat_id and progress_bot:
            try:
                progress_msg = await progress_bot.send_message(
                    chat_id=progress_chat_id,
                    text=(
                        "🔄 <b>Перепроверка</b>\n\n"
                        "📍 Этап: Загрузка списка лотов\n"
                        "▱▱▱▱▱▱▱▱▱▱▱▱ 0%\n"
                        "📦 Прогресс: …/…\n"
                        "🔎 Сейчас: Получаю список с FunPay\n"
                        "✅ Отправлено: 0"
                    ),
                    parse_mode='HTML'
                )
            except Exception:
                progress_msg = None

        set_check_progress(
            context,
            stage="Загрузка списка лотов",
            done=0,
            total=1,
            current="Получаю список лотов с FunPay",
            sent=0,
        )

        try:
            listings = await asyncio.wait_for(get_listings(), timeout=35)
        except asyncio.TimeoutError:
            logger.error("⏱️ Тайм-аут при получении списка лотов")
            if progress_msg and progress_bot:
                try:
                    await progress_bot.edit_message_text(
                        chat_id=progress_chat_id,
                        message_id=progress_msg.message_id,
                        text="❌ Тайм-аут при загрузке списка лотов",
                    )
                except Exception:
                    pass
            return 0

        if cancelled():
            logger.info("⏹️ Проверка остановлена пользователем во время загрузки списка")
            return -2

        total_listings = len(listings)
        # Update progress: list loaded
        await update_progress_message(
            "🔄 <b>Перепроверка</b>",
            "Отбор лотов",
            0,
            max(total_listings, 1),
            0,
            f"Список загружен: {total_listings} лотов, начинаю отбор",
            force=True,
        )
        already_seen_count = 0
        banned_count = 0
        candidates = []
        all_listing_hrefs = set()  # all hrefs on current page (for staleness check)

        # Build keyword maps for auto price tracking
        auto_price_map = {}   # skin_id → [offers] (mode=any)
        auto_pve_map = {}     # skin_id → [offers] (mode=pve)
        auto_edition_map = {} # edition_id → [offers]
        auto_pve_confirmed = []  # top-3 cheapest confirmed-PVE offers
        auto_pve_unconfirmed = []  # top-3 cheapest unconfirmed-PVE offers
        auto_pve_seen = False    # True if any confirmed PVE offer seen
        kw_to_skin = {}
        kw_to_edition = {}
        if skip_seen:  # only for background auto-monitoring
            for sid, skin in skins_dict.items():
                for kw in skin.get('keywords', []):
                    kw_to_skin[kw.lower()] = (sid, sid.replace('_', ' ').title())
            for eid, ed in config.get_all_editions().items():
                if ed.get('enabled', True):
                    for kw in ed.get('keywords', []):
                        kw_to_edition[kw.lower()] = (eid, eid.replace('_', ' ').title())

        for idx, item in enumerate(listings, start=1):
            if cancelled():
                logger.info("⏹️ Проверка остановлена пользователем во время отбора кандидатов")
                return -2

            set_check_progress(
                context,
                stage="Отбор лотов",
                done=idx,
                total=max(total_listings, 1),
                current=f"Проверяю лот {idx}/{max(total_listings, 1)} в общем списке",
                sent=0,
            )
            await update_progress_message(
                "🔄 <b>Перепроверка</b>",
                "Отбор лотов",
                idx,
                max(total_listings, 1),
                0,
                f"Проверяю лот {idx}/{max(total_listings, 1)} в общем списке",
            )

            href = item.get('href')
            if not href:
                continue
            if href.startswith('/'):
                href = f"https://funpay.com{href}"
            all_listing_hrefs.add(href)

            offer_id_match = re.search(r'id=(\d+)', href)
            if not offer_id_match:
                continue
            offer_id = offer_id_match.group(1)

            if offer_id in banned_ids:
                banned_count += 1
                continue

            user = user_div.get_text(strip=True) if user_div else "Неизвестный"
            if user in banned_sellers:
                logger.info(f"👤 Пропуск: продавец {user} забанен")
                continue

            desc_div = item.find('div', class_='tc-desc-text')
            price_div = item.find('div', class_='tc-price')
            user_div = item.find('div', class_='media-user-name')

            short_description = desc_div.get_text(strip=True) if desc_div else ""
            price_text = price_div.get_text(strip=True) if price_div else "Нет цены"
            price_value = parse_price(price_text)
            user = user_div.get_text(strip=True) if user_div else "Неизвестный"
            short_desc_lower = normalize_match_text(short_description)

            already_seen = False
            if skip_seen and offer_id in seen_cache:
                cached_price, cached_desc = seen_cache[offer_id]
                if cached_price == price_value and cached_desc == short_description:
                    already_seen = True

            if already_seen:
                already_seen_count += 1
                # Continue processing to update auto_price_map for stats,
                # but will skip notification logic below.

            if 'аренда' in short_desc_lower and 'продажа' not in short_desc_lower:
                if skip_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id, price_value, short_description)
                continue

            source_lot = item.get('data-source-lot', 'accounts')
            item_keywords = config.get_prochee_keywords() if source_lot == 'prochee' else search_keywords

            matched_keyword = ""
            for keyword in item_keywords:
                pattern = r'\b' + re.escape(normalize_match_text(keyword)) + r'\b'
                if re.search(pattern, short_desc_lower):
                    matched_keyword = keyword
                    break

            # --- Auto price tracking: detect skins BEFORE exclude filter ---
            is_excluded = bool(contains_exclude_keyword(short_description, exclude_keywords, positive_keywords))
            # Auto-tracking only for accounts page (not 'prochee' misc items)
            if price_value is not None and kw_to_skin and source_lot != 'prochee':
                matched_skins = {}  # sid → sname
                for kw, (sid, sname) in kw_to_skin.items():
                    if sid in matched_skins:
                        continue
                    pattern_kw = r'\b' + re.escape(kw) + r'\b'
                    if re.search(pattern_kw, short_desc_lower):
                        matched_skins[sid] = sname
                if matched_skins:
                    best_sid = max(
                        matched_skins.keys(),
                        key=lambda s: skins_dict.get(s, {}).get('price', 0)
                    )
                    best_sname = matched_skins[best_sid]
                    # Always mark skin as "seen by auto" (for 📡 icon)
                    is_pve = has_pve(short_desc_lower, include_unconfirmed=False)
                    if is_pve:
                        auto_pve_map.setdefault(best_sid, [])
                    else:
                        auto_price_map.setdefault(best_sid, [])
                    # Only record price if NOT excluded (no "без почты" etc.)
                    if not is_excluded:
                        entry = {'price': price_value, 'price_text': price_text,
                                 'href': href, 'seller': user, 'name': best_sname}
                        target = auto_pve_map if is_pve else auto_price_map
                        lst = target[best_sid]
                        lst.append(entry)
                        lst.sort(key=lambda x: x['price'])
                        if len(lst) > 3:
                            lst.pop()

            # Track editions independently (offer can have both skin + edition)
            if price_value is not None and kw_to_edition and source_lot != 'prochee':
                matched_eds = {}
                for kw, (eid, ename) in kw_to_edition.items():
                    if eid in matched_eds:
                        continue
                    pattern_kw = r'\b' + re.escape(kw) + r'\b'
                    if re.search(pattern_kw, short_desc_lower):
                        matched_eds[eid] = ename
                if matched_eds:
                    tier = {'super_deluxe': 1, 'limited': 2, 'ultimate': 3}
                    best_eid = max(matched_eds.keys(), key=lambda e: tier.get(e, 0))
                    best_ename = matched_eds[best_eid]
                    auto_edition_map.setdefault(best_eid, [])
                    if not already_seen:
                        logger.debug(f"📡 Издание {best_eid}: {price_text}, excl={is_excluded}, desc={short_description[:50]}")
                    if not is_excluded:
                        entry = {'price': price_value, 'price_text': price_text,
                                 'href': href, 'seller': user, 'name': best_ename}
                        auto_edition_map[best_eid].append(entry)
                        auto_edition_map[best_eid].sort(key=lambda x: x['price'])
                        if len(auto_edition_map[best_eid]) > 3:
                            auto_edition_map[best_eid].pop()

            # Track confirmed & unconfirmed PVE (cheapest accounts with PVE)
            if price_value is not None and skip_seen and source_lot != 'prochee':
                has_confirmed = has_pve(short_desc_lower, include_unconfirmed=False)
                has_any_pve = has_pve(short_desc_lower, include_unconfirmed=True)
                if has_confirmed:
                    auto_pve_seen = True
                    if not is_excluded:
                        auto_pve_confirmed.append(
                            {'price': price_value, 'price_text': price_text,
                             'href': href, 'seller': user, 'name': 'STW'})
                        auto_pve_confirmed.sort(key=lambda x: x['price'])
                        if len(auto_pve_confirmed) > 3:
                            auto_pve_confirmed.pop()
                elif has_any_pve:
                    # Unconfirmed PVE: has 'pve'/'stw' keyword but no confirmed keyword
                    if not is_excluded:
                        auto_pve_unconfirmed.append(
                            {'price': price_value, 'price_text': price_text,
                             'href': href, 'seller': user, 'name': 'STW'})
                        auto_pve_unconfirmed.sort(key=lambda x: x['price'])
                        if len(auto_pve_unconfirmed) > 3:
                            auto_pve_unconfirmed.pop()

            if not matched_keyword:
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id, price_value, short_description)
                continue

            if is_excluded:
                if not already_seen:
                    logger.debug(f"🚫 Исключено в кратком описании: {short_description[:40]}...")
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id, price_value, short_description)
                continue

            if log_state and price_value is not None:
                for position_id in log_keyword_map.get(matched_keyword.lower(), ()):
                    log_entry = log_state.get(position_id)
                    if log_entry:
                        update_recheck_log_offer(log_entry, 'any_offer', price_value, price_text, href)
            if price_value is None or price_value > effective_max_price:
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id, price_value, short_description)
                continue

            # Skip notification for already-seen offers, but auto_price_map is already updated
            if already_seen:
                continue

            matched_skins_list = []
            if source_lot != 'prochee':
                for sid, skin in skins_dict.items():
                    for kw in skin.get('keywords', []):
                        pattern_kw = r'\b' + re.escape(normalize_match_text(kw)) + r'\b'
                        if re.search(pattern_kw, short_desc_lower):
                            matched_skins_list.append(sid)
                            break

            candidates.append({
                'offer_id': offer_id,
                'href': href,
                'short_description': short_description,
                'price_text': price_text,
                'price_value': price_value,
                'user': user,
                'matched_keyword': matched_keyword,
                'source_lot': source_lot,
                'matched_skins': matched_skins_list,
            })


        new_candidates = len(candidates)
        new_offers_count = total_listings - already_seen_count - banned_count
        logger.info(
            f"📊 Статистика: Всего на сайте: {total_listings} | Новых объявлений: {new_offers_count} | "
            f"Забанено: {banned_count} | Новых кандидатов: {new_candidates}"
        )

        def candidate_sort_key(c):
            return c['price_value']

        candidates.sort(key=candidate_sort_key)
        limit = len(candidates) if candidate_limit is None else min(candidate_limit, len(candidates))
        sent_count = 0

        set_check_progress(
            context,
            stage="Загрузка карточек",
            done=0,
            total=max(limit, 1),
            current="Кандидаты отобраны, начинаю проверку карточек",
            sent=sent_count,
        )
        await update_progress_message(
            "🔄 <b>Перепроверка</b>",
            "Загрузка карточек",
            0,
            max(limit, 1),
            sent_count,
            "Кандидаты отобраны, начинаю проверку карточек",
            force=True,
        )

        logger.info(f"🔢 Обрабатываю кандидатов: {limit}/{len(candidates)}")

        for idx, candidate in enumerate(candidates[:limit], start=1):
            if cancelled():
                logger.info("⏹️ Проверка остановлена пользователем во время обработки кандидатов")
                return -2

            offer_id = candidate['offer_id']
            href = candidate['href']
            short_preview = candidate['short_description'][:60] or f"Лот {idx}"

            set_check_progress(
                context,
                stage="Проверка карточек",
                done=idx,
                total=max(limit, 1),
                current=f"Лот {idx}/{max(limit, 1)}: {short_preview}",
                sent=sent_count,
            )
            await update_progress_message(
                "🔄 <b>Перепроверка</b>",
                "Проверка карточек",
                idx,
                max(limit, 1),
                sent_count,
                f"Лот {idx}/{max(limit, 1)}: {short_preview}",
            )

            await asyncio.sleep(0)
            logger.info(f"Загружаю детали: {href}")

            def _mark_seen_permanent():
                if not skip_seen:
                    return
                try:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id, candidate['price_value'], candidate['short_description'])
                except Exception as e:
                    logger.warning(f"Не удалось сохранить seen_id для отклонённого лота: {e}")

            if is_recently_sent(offer_id, candidate['user'], candidate['short_description'], candidate['price_value']):
                logger.debug(f"⏳ Пропуск: лот {offer_id} от {candidate['user']} (или аналогичный) уже отправлялся в последние 7 дней")
                _mark_seen_permanent()
                continue

            try:
                full_description, rating_text = await asyncio.wait_for(
                    get_offer_details(href),
                    timeout=max(20, config.request_delay_max + 20)
                )
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Тайм-аут деталей лота: {href}")
                continue

            if full_description is None:
                logger.warning(f"Не удалось загрузить детали для {href}")
                continue

            combined_text = candidate['short_description'] + " " + full_description
            matched_exclude = contains_exclude_keyword(combined_text, exclude_keywords, positive_keywords)
            if matched_exclude:
                logger.info(f"🚫 Исключено ('{matched_exclude}'): {candidate['short_description'][:40]}...")
                try:
                    record_red_flag(
                        item_name=candidate.get('short_description', '')[:80],
                        price_text=candidate.get('price_text', ''),
                        href=candidate.get('href', ''),
                        seller=candidate.get('user', ''),
                        reason=str(matched_exclude),
                    )
                except Exception:
                    pass
                _mark_seen_permanent()
                continue

            if premium_only:
                combined_lower = combined_text.lower()
                editions = config.get_all_editions()
                edition_priority = ['ultimate', 'limited', 'super_deluxe']
                matched_edition = None
                for ed_id in edition_priority:
                    ed = editions.get(ed_id, {})
                    if not ed.get('enabled', True):
                        continue
                    if any(kw.lower() in combined_lower for kw in ed.get('keywords', [])):
                        matched_edition = ed_id
                        break
                if not matched_edition:
                    matched_edition = 'unknown'

                found_skins = find_skins_in_text(combined_text, skins_dict)
                has_pve_flag = True
                seller_price = candidate['price_value']
                if log_state and matched_edition in log_state:
                    update_recheck_log_offer(log_state[matched_edition], 'any_offer', seller_price, candidate['price_text'], href)
                
                ed_price_original = editions.get(matched_edition, {}).get('price', effective_max_price / 5 if x5_mode else effective_max_price)
                ed_price_x5 = ed_price_original * 5 if x5_mode else ed_price_original
                
                if seller_price > ed_price_x5:
                    logger.info(f"💸 Дорого для {matched_edition}: {seller_price}₽ > {ed_price_x5}₽")
                    if matched_edition in summary_stats:
                        stat = summary_stats[matched_edition]
                        if stat['min_price'] is None or seller_price < stat['min_price']:
                            stat['min_price'] = seller_price
                            stat['href'] = candidate.get('href', '')
                        if stat['status'].startswith('Не найдено') or stat['status'].startswith('💸 Слишком дорого'):
                            stat['status'] = f"💸 Слишком дорого (мин: {seller_price}₽, лимит: {ed_price_x5}₽)"
                    _mark_seen_permanent()
                    continue
                
                if matched_edition in summary_stats:
                    summary_stats[matched_edition]['status'] = f"✅ Отправлен (Цена: {seller_price}₽)"
                    summary_stats[matched_edition]['href'] = candidate.get('href', '')
                
                original_my_max_price = ed_price_original
                x5_my_max_price = ed_price_x5
                passed_without_x5 = (seller_price <= ed_price_original)
                price_breakdown = f"🏆 {matched_edition.replace('_', ' ').title()} до {ed_price_x5}₽ (оригинал: {ed_price_original}₽)" if x5_mode else f"🏆 {matched_edition.replace('_', ' ').title()} до {ed_price_original}₽"
            else:
                if search_mode != 'skins_only' and has_new_pve(combined_text):
                    logger.info(f"⛔ Пропуск (новое PVE/STW): {candidate['short_description'][:40]}...")
                    _mark_seen_permanent()
                    continue

                found_skins = find_skins_in_text(combined_text, skins_dict)
                use_unconfirmed = include_unconfirmed_pve
                has_pve_flag = has_pve(combined_text, include_unconfirmed=use_unconfirmed)
                has_confirmed_pve_diag = has_pve(combined_text, include_unconfirmed=False)
                has_any_pve_diag = has_pve(combined_text, include_unconfirmed=True)

                if found_skins and not has_pve_flag:
                    all_skins_data = config.get_all_skins()
                    filtered_skins = []
                    for skin in found_skins:
                        skin_cfg = all_skins_data.get(skin['id'], {})
                        if skin_cfg.get('require_pve', False):
                            logger.info(f"🧟 Скин {skin['id']} требует PVE, но PVE не найден — пропускаю скин")
                            if skin['id'] in summary_stats:
                                summary_stats[skin['id']]['status'] = "❌ Найден только без PVE"
                                summary_stats[skin['id']]['href'] = candidate.get('href', '')
                                if summary_stats[skin['id']].get('min_price') is None:
                                    summary_stats[skin['id']]['min_price'] = candidate.get('price', 0)
                        else:
                            filtered_skins.append(skin)
                    found_skins = filtered_skins

                if log_state:
                    seller_price = candidate['price_value']
                    for skin in found_skins:
                        log_entry = log_state.get(skin['id'])
                        if not log_entry:
                            continue
                        update_recheck_log_offer(log_entry, 'any_offer', seller_price, candidate['price_text'], href)
                        if has_confirmed_pve_diag:
                            update_recheck_log_offer(log_entry, 'pve_offer', seller_price, candidate['price_text'], href)
                    if '__pve__' in log_state:
                        if has_any_pve_diag:
                            update_recheck_log_offer(log_state['__pve__'], 'any_offer', seller_price, candidate['price_text'], href)
                        if has_confirmed_pve_diag:
                            update_recheck_log_offer(log_state['__pve__'], 'pve_offer', seller_price, candidate['price_text'], href)

                pure_confirmed_pve_match = (confirmed_pve_only or confirmed_pve_enabled_effective) and has_confirmed_pve_diag
                pure_unconfirmed_pve_match = include_unconfirmed_pve and has_pve_flag and not has_confirmed_pve_diag
                should_skip = not found_skins and not pure_confirmed_pve_match and not pure_unconfirmed_pve_match

                if should_skip:
                    logger.info(f"⏭️ Пропуск (нет ценных скинов/PVE): {candidate['short_description'][:40]}...")
                    _mark_seen_permanent()
                    continue

                if include_unconfirmed_pve and has_pve_flag:
                    has_confirmed = has_pve(combined_text, include_unconfirmed=False)
                    if has_confirmed:
                        logger.info(f"✅ Пропуск (подтв. PVE, уже в мониторинге): {candidate['short_description'][:40]}...")
                        _mark_seen_permanent()
                        continue

                all_require_pve = found_skins and all(
                    config.get_all_skins().get(s['id'], {}).get('require_pve', False) for s in found_skins
                )
                pve_for_price = False if all_require_pve else has_pve_flag

                if not found_skins and pure_confirmed_pve_match:
                    original_my_max_price = max_price_override if max_price_override is not None and confirmed_pve_only else confirmed_pve_price_effective
                    original_price_breakdown = f"Подтв. PVE до {original_my_max_price}₽"
                elif not found_skins and pure_unconfirmed_pve_match:
                    original_my_max_price = max_price_override if max_price_override is not None else config.unconfirmed_pve_price
                    original_price_breakdown = f"PVE до {original_my_max_price}₽"
                else:
                    original_my_max_price, original_price_breakdown = calculate_max_price(
                        found_skins,
                        pve_for_price,
                        rare_override=rare_override,
                        pve_override=pve_override,
                    )

                seller_price = candidate['price_value']
                x5_my_max_price = original_my_max_price * 5 if x5_mode else original_my_max_price
                
                if x5_mode:
                    price_breakdown = f"{original_price_breakdown} (x5 = {x5_my_max_price}₽)"
                else:
                    price_breakdown = original_price_breakdown

                if seller_price > x5_my_max_price:
                    logger.info(f"💸 Слишком дорого: {seller_price}₽ > {x5_my_max_price}₽ ({price_breakdown})")
                    for skin in found_skins:
                        if skin['id'] in summary_stats:
                            stat = summary_stats[skin['id']]
                            if stat['min_price'] is None or seller_price < stat['min_price']:
                                stat['min_price'] = seller_price
                                stat['href'] = candidate.get('href', '')
                            if stat['status'].startswith('Не найдено') or stat['status'].startswith('💸 Слишком дорого'):
                                stat['status'] = f"💸 Слишком дорого (мин: {seller_price}₽, лимит: {x5_my_max_price}₽)"
                    if not found_skins and pure_confirmed_pve_match:
                        if '__pve__' in summary_stats:
                            stat = summary_stats['__pve__']
                            if stat['min_price'] is None or seller_price < stat['min_price']:
                                stat['min_price'] = seller_price
                                stat['href'] = candidate.get('href', '')
                            if stat['status'].startswith('Не найдено') or stat['status'].startswith('💸 Слишком дорого'):
                                stat['status'] = f"💸 Слишком дорого (мин: {seller_price}₽, лимит: {x5_my_max_price}₽)"
                    if not found_skins and pure_unconfirmed_pve_match:
                        if '__unconfirmed_pve__' in summary_stats:
                            stat = summary_stats['__unconfirmed_pve__']
                            if stat['min_price'] is None or seller_price < stat['min_price']:
                                stat['min_price'] = seller_price
                                stat['href'] = candidate.get('href', '')
                            if stat['status'].startswith('Не найдено') or stat['status'].startswith('💸 Слишком дорого'):
                                stat['status'] = f"💸 Слишком дорого (мин: {seller_price}₽, лимит: {x5_my_max_price}₽)"
                    _mark_seen_permanent()
                    continue

                for skin in found_skins:
                    if skin['id'] in summary_stats:
                        summary_stats[skin['id']]['status'] = f"✅ Отправлен (Цена: {seller_price}₽)"
                        summary_stats[skin['id']]['href'] = candidate.get('href', '')
                if not found_skins:
                    if pure_confirmed_pve_match and '__pve__' in summary_stats:
                        summary_stats['__pve__']['status'] = f"✅ Отправлен (Цена: {seller_price}₽)"
                        summary_stats['__pve__']['href'] = candidate.get('href', '')
                    if pure_unconfirmed_pve_match and '__unconfirmed_pve__' in summary_stats:
                        summary_stats['__unconfirmed_pve__']['status'] = f"✅ Отправлен (Цена: {seller_price}₽)"
                        summary_stats['__unconfirmed_pve__']['href'] = candidate.get('href', '')

                passed_without_x5 = (seller_price <= original_my_max_price)

            # === ФИНАЛЬНАЯ ЗАЩИТА: не отправлять скины без PVE если require_pve ===
            if not premium_only and found_skins:
                has_confirmed_final = has_pve(combined_text, include_unconfirmed=False)
                has_any_final = has_pve(combined_text, include_unconfirmed=True)
                all_require = all(
                    config.get_all_skins().get(s['id'], {}).get('require_pve', False)
                    for s in found_skins
                )
                if all_require and not has_confirmed_final:
                    # Все скины требуют подтверждённое PVE — неподтверждённое не считается
                    if not has_any_final:
                        logger.info(f"🛡️ Защита: все скины require_pve но PVE не найдено — пропуск: {candidate['short_description'][:50]}")
                    else:
                        logger.info(f"🛡️ Защита: все скины require_pve, PVE только неподтверждённое — пропуск: {candidate['short_description'][:50]}")
                    _mark_seen_permanent()
                    continue

            rating_emoji = "⭐" if "из 5" in rating_text else "❓"
            skins_list = ", ".join([s['keyword'] for s in found_skins]) if found_skins else "Нет"
            main_feature = get_main_feature(found_skins, has_pve_flag, rare_override=rare_override)
            has_confirmed_pve = has_pve_flag if premium_only else has_pve(combined_text, include_unconfirmed=False)
            has_any_pve = has_pve_flag if premium_only else has_pve(combined_text, include_unconfirmed=True)

            if has_confirmed_pve:
                pve_text = "Да"
            elif has_any_pve:
                pve_text = "Неподтвержденное"
            else:
                pve_text = "Нет"

            # Get or create seller ID for the ban link
            seller_id = get_or_create_seller_id(candidate['user'])
            ban_seller_href = f"https://t.me/{bot_username}?start=banseller_{seller_id}" if bot_username else None
            ban_line = f"🚫 <a href='{ban_seller_href}'>Бан</a>" if ban_seller_href else f"🚫 Бан: /banseller {candidate['user']}"

            hide_href = f"https://t.me/{bot_username}?start=ban_{offer_id}" if bot_username else None
            hide_line = f"❌ <a href='{hide_href}'>Скрыть</a>" if hide_href else f"❌ Скрыть: /ban {offer_id}"

            link_line = f"🔗 <a href='{href}'>Ссылка</a>"
            desc_escaped = html.escape(candidate['short_description'])

            # PVE Checkmark / Cross logic
            pve_emoji = "✅" if has_any_pve else "❌"

            # Seller status check
            is_seller_ok = True
            if not rating_text or "0 отзывов" in rating_text or "Ошибка" in rating_text or "❗" in rating_text:
                is_seller_ok = False
            seller_status_emoji = "👌" if is_seller_ok else "❗"

            if os.environ.get("GITHUB_ACTIONS") == "true":
                source_line = "🤖 GitHub автомониторинг"
            else:
                source_line = "💻 Локальный автомониторинг"

            # Determine display name and emoji for the main feature
            from handlers import _skin_emoji as _get_skin_emoji
            edition_ids = set(config.get_all_editions().keys())
            if premium_only and matched_edition:
                display_title = f"🏆 {matched_edition.replace('_', ' ').title()}"
            elif found_skins:
                def skin_price(skin):
                    return rare_override if rare_override is not None else skin['price']
                main_skin = max(found_skins, key=skin_price)
                skin_id = main_skin['id']
                skin_emoji = _get_skin_emoji(skin_id) or "🎮"
                skin_name = skin_id.replace('_', ' ').title()
                display_title = f"{skin_emoji} {skin_name}"
            elif has_confirmed_pve:
                display_title = "🧟 Подтв. PVE"
            elif has_any_pve:
                display_title = "🧟 Неподтв. PVE"
            else:
                if matched_keyword:
                    display_title = f"🔔 {matched_keyword.title()}"
                else:
                    display_title = "🔔 Найдено предложение!"

            # Format value inside the table: limit price from the config
            # (which is x5_my_max_price or original_my_max_price)
            limit_val_for_align = x5_my_max_price if x5_mode else original_my_max_price
            limit_val = f"{limit_val_for_align}₽".rjust(8)

            # Emojis inside the <code> block for perfect alignment
            limit_row = f"<code>💸 Лимит       │ {limit_val}</code>"
            pve_row = f"<code>🧟   PVE       │         </code>{pve_emoji}"
            seller_row = f"<code>👤 Продавец    │         </code>{seller_status_emoji}"
            skins_row = f"<code>🎮 Основное    │ </code>{skins_list}"

            if x5_mode:
                passed_str = "Да" if passed_without_x5 else "Нет, цена выше сильно"
                msg = (
                    f"<b>{display_title}</b>\n\n"
                    f"💰 <b>Цена:</b> <a href='{href}'><b>{candidate['price_text']}</b></a>\n\n"
                    f"⚙️ <code>Режим:     х5 режим</code>\n"
                    f"🤔 <code>Без х5:    {passed_str}</code>\n\n"
                    f"{limit_row}\n"
                    f"{pve_row}\n"
                    f"{seller_row}\n"
                    f"{skins_row}\n\n"
                    f"📌 <b>Описание:</b> <i>{desc_escaped}</i>\n\n"
                    f"{hide_line}  │  {ban_line}  │  {link_line}\n\n"
                    f"{source_line}"
                )
            else:
                msg = (
                    f"<b>{display_title}</b>\n\n"
                    f"💰 <b>Цена:</b> <a href='{href}'><b>{candidate['price_text']}</b></a>\n\n"
                    f"{limit_row}\n"
                    f"{pve_row}\n"
                    f"{seller_row}\n"
                    f"{skins_row}\n\n"
                    f"📌 <b>Описание:</b> <i>{desc_escaped}</i>\n\n"
                    f"{hide_line}  │  {ban_line}  │  {link_line}\n\n"
                    f"{source_line}"
                )

            try:
                if context:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_web_page_preview=True)
                elif bot_instance:
                    await bot_instance.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_web_page_preview=True)

                logger.info(f"✅ Отправлено: {skins_list} - {candidate['price_value']}₽")
                sent_count += 1
                if log_state:
                    if premium_only and matched_edition in log_state:
                        log_state[matched_edition]['sent_count'] += 1
                    else:
                        for skin in found_skins:
                            if skin['id'] in log_state:
                                log_state[skin['id']]['sent_count'] += 1
                        if not found_skins and has_pve_flag and '__pve__' in log_state:
                            log_state['__pve__']['sent_count'] += 1
                if context:
                    sent_positions = context.bot_data.get('current_check_sent_positions', set())
                    if premium_only and matched_edition:
                        sent_positions.add(matched_edition)
                    else:
                        for skin in found_skins:
                            sent_positions.add(skin['id'])
                        if not found_skins and (pure_confirmed_pve_match or pure_unconfirmed_pve_match):
                            sent_positions.add('__pve__')
                    context.bot_data['current_check_sent_positions'] = sent_positions
                set_check_progress(
                    context,
                    stage="Отправка результата",
                    done=idx,
                    total=max(limit, 1),
                    current=f"Отправил подходящий лот: {short_preview}",
                    sent=sent_count,
                )

                seen_ids.add(offer_id)
                try:
                    save_seen_id(offer_id, candidate['price_value'], candidate['short_description'])
                except Exception as e:
                    logger.warning(f"Не удалось сохранить seen_id: {e}")

                try:
                    save_sent_offer(offer_id, candidate['price_value'], candidate['short_description'], candidate['user'])
                except Exception as e:
                    logger.warning(f"Не удалось сохранить в историю: {e}")
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")

        if run_snapshot and log_state is not None:
            run_snapshot['log_state'] = log_state
            run_snapshot['log_items'] = build_cached_recheck_log(run_snapshot)

        if sent_count > 0:
            logger.info(f"✅ Итого отправлено: {sent_count} новых предложений")
        else:
            logger.info("ℹ️ Новых подходящих предложений не найдено")

        # Save auto-monitoring prices to history
        # Empty lists → snapshot with 0 results (source=auto, shows "—" with 📡)
        if skip_seen and kw_to_skin:
            def _auto_name(sid):
                return sid.replace('_', ' ').title()

            def _save_auto(item_type, item_id, name, mode, offers):
                record_price_snapshot(
                    item_type, item_id, name, mode,
                    [{'price': o['price'], 'price_text': o['price_text'],
                      'href': o['href'], 'seller': o['seller']} for o in offers],
                    source='auto'
                )

            # --- Validate top offers by loading full descriptions ---
            # Only check cheapest (first) offer per map to minimize HTTP requests
            validated_cache = {}  # href → True/False
            cache_hits = 0

            async def _validate_offer(offer):
                nonlocal cache_hits
                href = offer.get('href', '')
                if href in validated_cache:
                    return validated_cache[href]
                if href in OFFER_DETAILS_CACHE:
                    if time.time() - OFFER_DETAILS_CACHE[href]['cached_at'] < OFFER_DETAILS_CACHE_TTL:
                        cache_hits += 1
                try:
                    full_desc, _ = await asyncio.wait_for(
                        get_offer_details(href), timeout=15
                    )
                    if full_desc:
                        excluded = contains_exclude_keyword(full_desc, exclude_keywords, positive_keywords)
                        if excluded:
                            logger.debug(f"🚫 Авто-валидация: исключён по описанию ('{excluded}'): {href}")
                            validated_cache[href] = False
                            return False
                except Exception as e:
                    logger.debug(f"⚠️ Авто-валидация: не удалось загрузить {href}: {e}")
                validated_cache[href] = True
                return True

            async def _validate_cheapest(offers):
                """Validate cheapest offers. Remove excluded ones from the top."""
                if test_summary_mode:
                    return list(offers)
                result = list(offers)
                while result:
                    if await _validate_offer(result[0]):
                        break  # cheapest is valid
                    result = result[1:]  # drop excluded, check next
                return result

            # Validate only the #1 cheapest offer per map (fast: ~20 requests max)
            total_auto_keys = len(auto_price_map) + len(auto_pve_map) + len(auto_edition_map)
            if auto_pve_confirmed:
                total_auto_keys += 1
            if auto_pve_unconfirmed:
                total_auto_keys += 1

            if total_auto_keys > 0:
                logger.info("🔍 Авто-валидация: проверка цен для авто-мониторинга...")

            for sid in list(auto_price_map.keys()):
                auto_price_map[sid] = await _validate_cheapest(auto_price_map[sid])
            for sid in list(auto_pve_map.keys()):
                auto_pve_map[sid] = await _validate_cheapest(auto_pve_map[sid])
            for eid in list(auto_edition_map.keys()):
                auto_edition_map[eid] = await _validate_cheapest(auto_edition_map[eid])
            auto_pve_confirmed = await _validate_cheapest(auto_pve_confirmed)
            auto_pve_unconfirmed = await _validate_cheapest(auto_pve_unconfirmed)

            if validated_cache:
                n_checked = len(validated_cache)
                n_removed = sum(1 for v in validated_cache.values() if not v)
                logger.info(f"🔍 Авто-валидация: проверено {n_checked} описаний (из кэша: {cache_hits}), исключено {n_removed}")

            parts = []

            def _old_offer_gone(item_type, item_id, mode):
                """Check if previously saved offer is no longer on the listing page."""
                # If listing was truncated (2000 items), we can't be sure the offer is gone
                if total_listings >= 2000:
                    return False
                try:
                    top = get_latest_top3(item_type, item_id, mode)
                    if top:
                        old_href = top[0].get('href')
                        if old_href and old_href not in all_listing_hrefs:
                            return True  # offer was sold/removed
                except Exception:
                    pass
                return False

            # Save skins: write if offers found, or clear if old offer is gone
            all_skin_ids = set()
            for kw, (sid, sname) in kw_to_skin.items():
                all_skin_ids.add(sid)
            for sid in all_skin_ids:
                if auto_price_map.get(sid):
                    _save_auto('skin', sid, _auto_name(sid), 'any', auto_price_map[sid])
                elif _old_offer_gone('skin', sid, 'any'):
                    _save_auto('skin', sid, _auto_name(sid), 'any', [])
                if auto_pve_map.get(sid):
                    _save_auto('skin', sid, _auto_name(sid), 'pve', auto_pve_map[sid])
                elif _old_offer_gone('skin', sid, 'pve'):
                    _save_auto('skin', sid, _auto_name(sid), 'pve', [])
            n_any = sum(1 for sid in all_skin_ids if auto_price_map.get(sid))
            n_pve = sum(1 for sid in all_skin_ids if auto_pve_map.get(sid))
            parts.append(f"скины: {n_any}/{len(all_skin_ids)} без PVE, {n_pve}/{len(all_skin_ids)} с PVE")

            # Save editions: write if offers found, or clear if old offer is gone
            all_edition_ids = set()
            for kw, (eid, ename) in kw_to_edition.items():
                all_edition_ids.add(eid)
            for eid in all_edition_ids:
                if auto_edition_map.get(eid):
                    _save_auto('edition', eid, _auto_name(eid), 'any', auto_edition_map[eid])
                elif _old_offer_gone('edition', eid, 'any'):
                    _save_auto('edition', eid, _auto_name(eid), 'any', [])
            n_ed = sum(1 for eid in all_edition_ids if auto_edition_map.get(eid))
            parts.append(f"издания: {n_ed}/{len(all_edition_ids)}")

            # Save STW: write if offers found, or clear if old offer is gone
            if auto_pve_confirmed:
                _save_auto('pve', 'confirmed', 'STW', 'confirmed', auto_pve_confirmed)
            elif _old_offer_gone('pve', 'confirmed', 'confirmed'):
                _save_auto('pve', 'confirmed', 'STW', 'confirmed', [])
            if auto_pve_unconfirmed:
                _save_auto('pve', 'unconfirmed', 'STW', 'unconfirmed', auto_pve_unconfirmed)
            elif _old_offer_gone('pve', 'unconfirmed', 'unconfirmed'):
                _save_auto('pve', 'unconfirmed', 'STW', 'unconfirmed', [])
            parts.append(f"STW подтв: {'да' if auto_pve_confirmed else '—'}, неподтв: {'да' if auto_pve_unconfirmed else '—'}")

            logger.debug(f"📈 Авто-мониторинг: {', '.join(parts)}")

        if test_summary_mode:
            logger.info("🔍 Тестовый режим: быстрый поиск скрытых позиций...")
            search_list = []
            # Импортируем дефолтные ключевые слова из cfg для объединения
            from cfg import DEFAULT_RARE_SKINS
            for sid, stat in summary_stats.items():
                stat['best_with_pve'] = None
                stat['best_without_pve'] = None
                
                # В режиме статистики всегда делаем поиск для всех активных позиций
                keywords = []
                require_pve = False
                if sid == '__pve__':
                    keywords = config.get_confirmed_pve()
                elif sid == '__unconfirmed_pve__':
                    # Используем все PVE ключевые слова (подтв + неподтв)
                    keywords = list(config.get_confirmed_pve()) + list(config.get_unconfirmed_pve())
                elif sid in skins_dict:
                    skin_cfg = skins_dict[sid]
                    keywords = list(skin_cfg.get('keywords', []))
                    require_pve = skin_cfg.get('require_pve', False)
                    # Добавляем дефолтные ключевые слова из cfg.py
                    if sid in DEFAULT_RARE_SKINS:
                        default_kws = DEFAULT_RARE_SKINS[sid].get('keywords', [])
                        existing_lower = {k.lower() for k in keywords}
                        for dk in default_kws:
                            if dk.lower() not in existing_lower:
                                keywords.append(dk)
                else:
                    editions = config.get_all_editions()
                    if sid in editions:
                        ed_cfg = editions[sid]
                        keywords = ed_cfg.get('keywords', [])
                        require_pve = ed_cfg.get('require_pve', False)
                if keywords:
                    search_list.append((sid, keywords, require_pve))

            if search_list:
                try:
                    diag_results = await diagnostic_search(search_list, time_budget=120)
                    if diag_results is None:
                        logger.error("Диагностический поиск вернул None (ошибка загрузки лотов). Создаем фиктивные результаты.")
                        diag_results = {}
                        for item in search_list:
                            sid = item[0]
                            diag_results[sid] = {
                                'validated': [],
                                'best_with_pve': None,
                                'best_without_pve': None,
                                'error': True
                            }
                    for sid, diag_result in diag_results.items():
                        stat = summary_stats[sid]
                        validated = diag_result['validated']
                        best_no_pve = diag_result.get('best_without_pve')
                        
                        stat['best_with_pve'] = validated[0] if validated else None
                        stat['best_without_pve'] = best_no_pve
                        stat['error'] = diag_result.get('error', False)
                except Exception as e:
                    logger.error(f"Ошибка диагностического поиска: {e}")

            report_lines = []

            # Determine which sids are editions
            edition_ids = set(config.get_all_editions().keys())

            from handlers import _skin_emoji as _get_skin_emoji
            for sid, stat in summary_stats.items():
                name = stat['name']
                if name and name[0].isalpha():
                    name = name[0].upper() + name[1:]

                # Add skin emoji prefix to name
                if sid in ('__pve__', '__unconfirmed_pve__'):
                    skin_icon = ''
                elif sid in edition_ids:
                    skin_icon = '🏆'
                else:
                    skin_icon = _get_skin_emoji(sid)
                if skin_icon:
                    name = f"{skin_icon} {name}"
                
                # Check limits
                original_limit = 0
                skin_require_pve = False
                if sid == '__pve__':
                    original_limit = config.confirmed_pve_price
                elif sid == '__unconfirmed_pve__':
                    original_limit = max_price_override if max_price_override is not None else config.unconfirmed_pve_price
                elif sid in skins_dict:
                    original_limit = skins_dict[sid].get('price', 0)
                    skin_require_pve = skins_dict[sid].get('require_pve', False)
                else:
                    original_limit = config.get_all_editions().get(sid, {}).get('price', 0)
                
                limit_price = original_limit * 5 if x5_mode else original_limit
                
                best_with_pve = stat.get('best_with_pve')
                best_without_pve = stat.get('best_without_pve')
                
                is_edition = sid in edition_ids
                is_pve_pos = sid in ('__pve__', '__unconfirmed_pve__')

                if is_pve_pos or is_edition:
                    # PVE-позиции и издания — только одна строка (всегда с PVE)
                    item_lines = []
                    item_lines.append(f"<b>{name}</b>")
                    item_lines.append("")
                    item_lines.append(f"💸 Лимит: {limit_price}₽")
                    item_lines.append("")
                    if best_with_pve:
                        p = best_with_pve['price']
                        h = best_with_pve['href']
                        verdict = "✅ Подходит" if p <= limit_price else "🟣 Дорого"
                        p_str = f"{int(p)}₽"
                        p_display = p_str.rjust(7)
                        spaces_len = 10 if int(p) < 100 else 9
                        spaces_str = " " * spaces_len
                        item_lines.append(f"🧟 <code>+PVE   {p_display}  │ </code>{verdict}\n🔗 <code>{spaces_str}</code><a href=\"{html.escape(h)}\"><b>*ТЫК*</b></a>")
                    else:
                        p_display = "---".rjust(7)
                        verdict = "⚠️ Ошибка 502" if stat.get('error') else "❌ Не найдено"
                        item_lines.append(f"🧟 <code>+PVE   {p_display}  │ </code>{verdict}")
                    report_lines.append("\n".join(item_lines))
                else:
                    # Скины — показываем PVE и без PVE
                    item_lines = []
                    item_lines.append(f"<b>{name}</b>")
                    item_lines.append("")
                    item_lines.append(f"💸 Лимит: {limit_price}₽")
                    item_lines.append("")
                    
                    pve_str = f"{int(best_with_pve['price'])}₽" if best_with_pve else None
                    nopve_str = f"{int(best_without_pve['price'])}₽" if best_without_pve else None
                    
                    pve_dashes = "---"
                    nopve_dashes = "---"
                    
                    max_len = 7
                    
                    pve_display = pve_str.rjust(max_len) if pve_str else pve_dashes.rjust(max_len)
                    nopve_display = nopve_str.rjust(max_len) if nopve_str else nopve_dashes.rjust(max_len)
                    
                    # 1. PVE Block
                    pve_p = best_with_pve['price'] if best_with_pve else None
                    nopve_p = best_without_pve['price'] if best_without_pve else None
                    pve_ok = (pve_p is None) or (int(pve_p) < 100)
                    nopve_ok = (nopve_p is None) or (int(nopve_p) < 100)
                    spaces_len = 10 if (pve_ok and nopve_ok) else 9
                    spaces_str = " " * spaces_len

                    pve_lines = []
                    if best_with_pve:
                        p = best_with_pve['price']
                        h = best_with_pve['href']
                        verdict = "✅ Подходит" if p <= limit_price else "🟣 Дорого"
                        pve_lines.append(f"🧟 <code>+PVE   {pve_display}  │ </code>{verdict}")
                        pve_lines.append(f"🔗 <code>{spaces_str}</code><a href=\"{html.escape(h)}\"><b>*ТЫК*</b></a>")
                    else:
                        verdict = "⚠️ Ошибка 502" if stat.get('error') else "❌ Не найдено"
                        pve_lines.append(f"🧟 <code>+PVE   {pve_display}  │ </code>{verdict}")
                    
                    # 2. no-PVE Block
                    nopve_lines = []
                    if best_without_pve:
                        p = best_without_pve['price']
                        h = best_without_pve['href']
                        if skin_require_pve:
                            verdict = "🟣 Требуется PVE"
                        else:
                            verdict = "✅ Подходит" if p <= limit_price else "🟣 Дорого"
                        nopve_lines.append(f"👤 <code>-PVE   {nopve_display}  │ </code>{verdict}")
                        nopve_lines.append(f"🔗 <code>{spaces_str}</code><a href=\"{html.escape(h)}\"><b>*ТЫК*</b></a>")
                    else:
                        verdict = "⚠️ Ошибка 502" if stat.get('error') else "❌ Не найдено"
                        nopve_lines.append(f"👤 <code>-PVE   {nopve_display}  │ </code>{verdict}")
                        
                    # Combine PVE and no-PVE blocks directly with a blank line in between
                    item_lines.append("\n".join(pve_lines))
                    item_lines.append("")
                    item_lines.append("\n".join(nopve_lines))
                    
                    report_lines.append("\n".join(item_lines))

            # Добавляем в самый конец источник мониторинга
            is_git = os.environ.get('GITHUB_ACTIONS') == 'true'
            source_line = "📋 <b>Автомониторинг: Git 🤖</b>" if is_git else "📋 <b>Автомониторинг: Локальный 💻</b>"
            report_lines.append(source_line)
                
            # Разделяем отчет на части (чанки) по 8 позиций, чтобы избежать лимита Telegram в 100 HTML-сущностей
            chunk_size = 8
            content_lines = report_lines[:-1]
            footer_line = report_lines[-1]
            chunks = [content_lines[i:i + chunk_size] for i in range(0, len(content_lines), chunk_size)]
            if chunks:
                chunks[-1].append(footer_line)
            else:
                chunks = [[footer_line]]

            logger.info(f"📊 Отправляю диагностический отчет в Telegram ({len(chunks)} частей)...")
            for idx, chunk in enumerate(chunks, 1):
                report_msg = "\n\n<code>───────────────────────────────</code>\n\n".join(chunk)
                if idx > 1:
                    report_msg = f"<code>───────────────────────────────</code>\n\n" + report_msg
                try:
                    if context:
                        await context.bot.send_message(chat_id=chat_id, text=report_msg, parse_mode='HTML', disable_web_page_preview=True)
                    elif bot_instance:
                        await bot_instance.send_message(chat_id=chat_id, text=report_msg, parse_mode='HTML', disable_web_page_preview=True)
                    await asyncio.sleep(2.0)  # пауза для сохранения порядка сообщений и избежания флуд-лимитов
                except Exception as e:
                    logger.error(f"Ошибка отправки части {idx} диагностического отчета: {e}")

            logger.info("📊 Тестовый режим: отчет отправлен, история не сбрасывается")

        return sent_count

    finally:
        if progress_msg and progress_bot:
            try:
                await progress_bot.delete_message(
                    chat_id=progress_chat_id,
                    message_id=progress_msg.message_id
                )
            except Exception:
                pass
        if context:
            context.bot_data.pop('current_check_origin', None)
            context.bot_data.pop('current_check_log_state', None)
        clear_check_progress(context)

async def check_funpay_job(context: ContextTypes.DEFAULT_TYPE):
    """Задача для JobQueue"""
    bot_mode_state = context.bot_data.get('bot_mode', {}) or {}
    current_mode = bot_mode_state.get('mode', 'standard')
    if current_mode != 'standard':
        # Watchdog: если режим не-standard, но реально ничего не работает уже >15 мин — сбрасываем.
        started_at = bot_mode_state.get('started_at')
        stale = False
        if not process_offers_lock.locked():
            if started_at is None:
                stale = True
            else:
                try:
                    if time.time() - float(started_at) > 15 * 60:
                        stale = True
                except Exception:
                    stale = True
        if stale:
            logger.warning(
                f"⚠️ Watchdog: режим '{current_mode}' завис без активной задачи — сбрасываю в standard"
            )
            bot_mode_state['mode'] = 'standard'
            bot_mode_state['params'] = {}
            bot_mode_state['started_at'] = None
            context.bot_data['bot_mode'] = bot_mode_state
            context.bot_data.pop('current_check_progress', None)
            context.bot_data.pop('current_check_origin', None)
            context.bot_data['cancel_current_check'] = False
        else:
            logger.debug("⏭️ Фоновая проверка пропущена: выполняется ручной режим")
            return
    await process_offers(context=context, skip_seen=True)

async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /recheck — перепроверка. Флаг ++pve включает неподтверждённые PVE."""
    global chat_id, bot_mode
    user_chat_id = update.effective_chat.id

    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    args = context.args if context.args else []
    include_unconfirmed_pve = '++pve' in [a.lower() for a in args]
    search_mode_label = 'По списку'

    # Устанавливаем режим
    bot_mode['mode'] = 'recheck_pve' if include_unconfirmed_pve else 'recheck'
    display_mode = 'Перепроверка: Неподтв. PVE' if include_unconfirmed_pve else f"Перепроверка: {search_mode_label}"
    bot_mode['params'] = {
        'display_mode': display_mode,
        'target_label': 'Неподтв. PVE' if include_unconfirmed_pve else search_mode_label,
        'restore_mode': config.search_mode,
        'run_snapshot': build_recheck_run_snapshot(
            config,
            display_mode='Неподтв. PVE' if include_unconfirmed_pve else search_mode_label,
            bot_mode_key=bot_mode['mode'],
            search_mode='skins_pve',
            include_unconfirmed_pve=include_unconfirmed_pve,
            chat_id_value=user_chat_id,
        ),
    }
    bot_mode['started_at'] = time.time()

    pve_note = "\n🔓 Включены неподтверждённые PVE (generic pve/stw/пве)" if include_unconfirmed_pve else ""
    await update.message.reply_text(
        f"🔄 Начинаю полную перепроверку всех предложений...{pve_note}\n"
        "⚠️ Это может занять несколько минут из-за задержек между запросами."
    )

    sent_count = await process_offers(context=context, skip_seen=False, candidate_limit=None,
                                      include_unconfirmed_pve=include_unconfirmed_pve)

    if sent_count == -1:
        text, keyboard = build_running_check_status(context)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        return

    if sent_count == -2:
        context.bot_data.pop('current_check_sent_positions', None)
        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None
        await update.message.reply_text("⏹ Текущая проверка остановлена принудительно.")
        return

    snapshot = bot_mode.get('params', {}).get('run_snapshot')
    summary_fn = context.bot_data.get('send_recheck_result_message')
    sent_position_ids = sorted(context.bot_data.pop('current_check_sent_positions', set()))

    # Сбрасываем режим
    bot_mode['mode'] = 'standard'
    bot_mode['params'] = {}
    bot_mode['started_at'] = None

    if summary_fn and snapshot:
        snapshot['sent_position_ids'] = sent_position_ids
        await summary_fn(
            chat_id=user_chat_id,
            context=context,
            sent_count=sent_count,
            snapshot=snapshot,
            title="✅ Перепроверка завершена!",
        )
    else:
        await update.message.reply_text(
            f"✅ Перепроверка завершена!\n"
            f"📨 Отправлено {sent_count} подходящих предложений."
        )

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ban — добавляет лоты в бан-лист или очищает его."""
    global chat_id, banned_ids
    user_chat_id = update.effective_chat.id

    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    args = context.args if context.args else []
    if not args:
        await update.message.reply_text(
            "Использование: /ban <url1>, <url2>\n"
            "Или: /ban clear"
        )
        return

    if len(args) == 1 and args[0].lower() == 'clear':
        removed = clear_banned_ids()
        await update.message.reply_text(f"✅ Бан-лист очищен. Удалено: {removed}")
        return

    raw = " ".join(args)
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    ids_to_add = []

    for part in parts:
        offer_id = extract_offer_id(part)
        if offer_id:
            ids_to_add.append(offer_id)

    if not ids_to_add:
        await update.message.reply_text("❌ Не нашёл валидных ID или ссылок. Пример: /ban https://funpay.com/lots/offer?id=62283710")
        return

    before = len(banned_ids)
    banned_ids.update(ids_to_add)
    added = len(banned_ids) - before
    save_banned_ids()

    await update.message.reply_text(
        f"✅ Добавлено в бан: {added}\n"
        f"Всего в бан-листе: {len(banned_ids)}"
    )

async def banseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /banseller — добавляет продавца в бан-лист или очищает его."""
    global chat_id, banned_sellers
    user_chat_id = update.effective_chat.id
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    args = context.args if context.args else []
    if not args:
        await update.message.reply_text(
            "Использование: /banseller <username>\n"
            "Или: /banseller clear"
        )
        return

    if len(args) == 1 and args[0].lower() == 'clear':
        removed = clear_banned_sellers()
        await update.message.reply_text(f"✅ Бан-лист продавцов очищен. Удалено: {removed}")
        return

    seller_name = " ".join(args).strip()
    if not seller_name:
        await update.message.reply_text("❌ Неверное имя продавца.")
        return

    already_banned = seller_name in banned_sellers
    banned_sellers.add(seller_name)
    save_banned_sellers()

    await update.message.reply_text(
        f"✅ Продавец добавлен в бан-лист: <code>{seller_name}</code>\n"
        f"Всего забанено продавцов: {len(banned_sellers)}",
        parse_mode='HTML'
    )

async def unbanseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /unbanseller — удаляет продавца из бан-листа."""
    global chat_id, banned_sellers
    user_chat_id = update.effective_chat.id
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    args = context.args if context.args else []
    if not args:
        await update.message.reply_text("Использование: /unbanseller <username>")
        return

    seller_name = " ".join(args).strip()
    if seller_name in banned_sellers:
        banned_sellers.remove(seller_name)
        save_banned_sellers()
        await update.message.reply_text(f"✅ Продавец удалён из бан-листа: <code>{seller_name}</code>", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ Продавец <code>{seller_name}</code> не найден в бан-листе.", parse_mode='HTML')

async def pricetest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /pricetest — разовая проверка с тестовыми ценами."""
    global chat_id, bot_mode
    user_chat_id = update.effective_chat.id

    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    args = context.args if context.args else []
    if len(args) < 1 or len(args) > 2:
        await update.message.reply_text(
            "Использование: /pricetest 8000 3500\n"
            "Можно указать только одну цену: /pricetest 8000"
        )
        return

    try:
        rare_price = int(args[0])
        if rare_price <= 0:
            raise ValueError
        pve_price = None
        if len(args) == 2:
            pve_price = int(args[1])
            if pve_price <= 0:
                raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Пример: /pricetest 8000 3500")
        return

    max_price_override = (rare_price + pve_price) if pve_price is not None else rare_price
    pve_text = f"{pve_price}₽" if pve_price is not None else f"стандартные {config.pve_bonus}/1000₽"

    # Устанавливаем режим
    bot_mode['mode'] = 'pricetest'
    bot_mode['params'] = {
        'rare_price': rare_price,
        'pve_price': pve_price,
        'max_price': max_price_override,
        'display_mode': 'Мин. прайс: командный тест',
        'target_label': f"редкие {rare_price}₽" if pve_price is None else f"редкие {rare_price}₽ / PVE {pve_price}₽",
        'restore_mode': config.search_mode,
        'run_snapshot': build_recheck_run_snapshot(
            config,
            display_mode='Мин. прайс тест',
            bot_mode_key='pricetest',
            search_mode='skins_pve',
            max_price_override=max_price_override,
            rare_override=rare_price,
            pve_override=pve_price,
            chat_id_value=user_chat_id,
            log_view='skins',
        ),
    }
    bot_mode['started_at'] = time.time()

    await update.message.reply_text(
        "🧪 Мин. прайс тест запущен!\n"
        f"💎 Редкие скины: {rare_price}₽\n"
        f"🧩 PVE: {pve_text}\n"
        f"📈 Временная макс. цена: {max_price_override}₽\n"
        "⚠️ Режим разовой перепроверки (как /recheck)."
    )

    sent_count = await process_offers(
        context=context,
        skip_seen=False,
        max_price_override=max_price_override,
        rare_override=rare_price,
        pve_override=pve_price,
        candidate_limit=None
    )

    if sent_count == -1:
        text, keyboard = build_running_check_status(context)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        return

    if sent_count == -2:
        context.bot_data.pop('current_check_sent_positions', None)
        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None
        await update.message.reply_text("⏹ Текущая проверка остановлена принудительно.")
        return

    snapshot = bot_mode.get('params', {}).get('run_snapshot')
    summary_fn = context.bot_data.get('send_recheck_result_message')
    sent_position_ids = sorted(context.bot_data.pop('current_check_sent_positions', set()))

    # Сбрасываем режим
    bot_mode['mode'] = 'standard'
    bot_mode['params'] = {}
    bot_mode['started_at'] = None

    if summary_fn and snapshot:
        snapshot['sent_position_ids'] = sent_position_ids
        await summary_fn(
            chat_id=user_chat_id,
            context=context,
            sent_count=sent_count,
            snapshot=snapshot,
            title="✅ Мин. прайс тест завершён!",
        )
    else:
        await update.message.reply_text(
            f"✅ Мин. прайс тест завершён!\n"
            f"📨 Отправлено {sent_count} подходящих предложений."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help — показывает справку."""
    skins_dict = config.get_all_skins()
    skins_list = "\n".join(
        f"  {'✅' if data.get('enabled', True) else '❌'} {sid.replace('_', ' ').title()}: {data.get('price', 0)}₽"
        for sid, data in skins_dict.items()
    )

    help_text = (
        f"📖 <b>Справка FunPay Monitor</b>\n\n"
        f"Бот мониторит аккаунты Fortnite на FunPay и присылает уведомления "
        f"о выгодных предложениях с редкими скинами и/или Save the World (PVE).\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Команды:</b>\n\n"
        f"/start — запустить бота\n"
        f"/help — эта справка\n"
        f"/settings — ⚙️ панель настроек\n"
        f"/recheck — перепроверить всё заново\n"
        f"/recheck ++pve — то же + generic pve/stw/пве\n"
        f"/pricetest <i>цена</i> [<i>pve</i>] — тест цен\n"
        f"/ban <i>ссылки</i> — забанить лоты\n"
        f"/ban clear — очистить бан-лист\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Текущие настройки:</b>\n\n"
        f"💰 Макс. цена: {config.max_price}₽\n"
        f"🕐 Интервал: {config.check_interval} сек\n"
        f"⏳ Задержка: {config.request_delay_min}-{config.request_delay_max} сек\n"
        f"🚫 Фильтров: {len(config.get_exclude_keywords())}\n"
        f"🚷 В бан-листе: {len(banned_ids)}\n"
        f"👁 Просмотрено: {len(seen_ids)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Скины:</b>\n\n"
        f"{skins_list}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>PVE:</b> бонус {config.pve_bonus}₽ / 1000₽ (solo)\n"
        f"📊 <b>Наша цена:</b> топ-2 слота\n"
    )

    await update.message.reply_text(help_text, parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_username
    user_chat_id = update.effective_chat.id
    save_chat_id(user_chat_id)
    # Обновляем authorized_chat_id для settings
    context.bot_data['authorized_chat_id'] = str(user_chat_id)
    if not bot_username:
        try:
            bot_username = (await context.bot.get_me()).username
            context.bot_data['bot_username'] = bot_username
        except Exception:
            pass

    # Постоянная клавиатура внизу чата
    keyboard = main_reply_keyboard()

    args = context.args if context.args else []
    if args:
        if args[0].startswith('banseller_'):
            short_id = args[0].replace('banseller_', '', 1)
            seller_name = get_seller_name_by_id(short_id)
            if seller_name:
                already_banned = seller_name in banned_sellers
                banned_sellers.add(seller_name)
                save_banned_sellers()
                await update.message.reply_text(
                    (
                        f"👤 Продавец забанен: <code>{seller_name}</code>"
                        if not already_banned else
                        f"👤 Продавец уже в бан-листе: <code>{seller_name}</code>"
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text("❌ Продавец не найден в базе соответствий.", reply_markup=keyboard)
            return

        ban_offer_id = extract_offer_id(args[0].replace('ban_', '', 1)) if args[0].startswith('ban_') else None
        if ban_offer_id:
            already_banned = ban_offer_id in banned_ids
            banned_ids.add(ban_offer_id)
            save_banned_ids()
            await update.message.reply_text(
                (
                    f"🚫 Лот забанен: <code>{ban_offer_id}</code>"
                    if not already_banned else
                    f"🚫 Лот уже в бан-листе: <code>{ban_offer_id}</code>"
                ),
                parse_mode='HTML',
                reply_markup=keyboard
            )
            return

    # Compute effective max price (same logic as auto-monitoring)
    skins = config.get_all_skins()
    enabled = {sid: s for sid, s in skins.items() if s.get('enabled', True)}
    max_skin_price = max((s.get('price', 0) for s in enabled.values()), default=config.max_price)
    effective_max = max_skin_price + config.pve_bonus
    if config.confirmed_pve_enabled:
        effective_max = max(effective_max, config.confirmed_pve_price)

    await update.message.reply_text(
        f"✅ Бот активирован! Ваш ID: {user_chat_id}.\n\n"
        f"🔍 Ищу аккаунты Fortnite с подтверждённым STW/PVE и редкими скинами.\n"
        f"🚫 Исключаю {len(config.get_exclude_keywords())} фраз про отсутствие почты.\n"
        f"📦 Только продажа, без аренды.\n"
        f"💰 Максимальная цена: {effective_max}₽ (макс. скин {max_skin_price}₽ + PVE бонус {config.pve_bonus}₽).\n"
        f"⏱ Проверка каждые {config.check_interval} секунд.\n\n"
        f"👇 Используйте кнопки ниже для управления.",
        parse_mode='HTML',
        reply_markup=keyboard
    )
    logger.info(f"Пользователь зарегистрирован: {user_chat_id}")

    try:
        await context.bot.set_chat_menu_button(
            chat_id=user_chat_id,
            menu_button=MenuButtonCommands()
        )
    except Exception as e:
        logger.warning(f"Не удалось обновить кнопку меню: {e}")


async def validate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /validate — проверяет все ключи и подключения."""
    global chat_id
    user_chat_id = update.effective_chat.id
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    msg = await update.message.reply_text("🔍 Проверяю подключения...")

    lines = ["<b>📋 Диагностика бота</b>\n"]

    # Telegram
    tg_status, token_id, actual_id = _validate_telegram_bot()
    lines.append(f"<b>Telegram:</b> {tg_status}")

    # GitHub PAT
    gh_pat = _validate_github_pat()
    lines.append(f"<b>GH Token:</b> {gh_pat}")

    # GitHub Actions
    remote_repo = _get_git_remote_repo()
    env_repo = _normalize_github_repo(GITHUB_REPO) if GITHUB_REPO else "—"
    remote_ok = not remote_repo.startswith("⚠️") and remote_repo != "—"
    repo_for_check = remote_repo if remote_ok else env_repo
    gh_actions = _validate_github_actions(repo_for_check)
    lines.append(f"<b>GH Actions:</b> {gh_actions}")

    # Repo info
    lines.append(f"<b>Remote:</b> {remote_repo}")
    lines.append(f"<b>Env repo:</b> {env_repo}")
    if remote_ok and env_repo != "—":
        match = "✅ Совпадают" if remote_repo.lower() == env_repo.lower() else "⚠️ НЕ СОВПАДАЮТ"
        lines.append(f"<b>Repo match:</b> {match}")

    # Chat ID
    lines.append(f"<b>Chat ID:</b> {chat_id or '❌ не задан'}")

    # Config stats
    enabled_skins = config.get_enabled_skins()
    lines.append(f"<b>Скинов:</b> {len(enabled_skins)}/{len(config.get_all_skins())}")
    lines.append(f"<b>Seen IDs:</b> {len(seen_ids)}")
    lines.append(f"<b>Banned:</b> {len(banned_ids)}")

    await msg.edit_text("\n".join(lines), parse_mode='HTML')


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /sync — синхронизирует config.json с GitHub."""
    global chat_id
    user_chat_id = update.effective_chat.id
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await update.message.reply_text(
            "❌ GitHub не настроен.\n\n"
            "Установите переменные окружения:\n"
            "<code>GITHUB_TOKEN</code> â€” Personal Access Token\n"
            "<code>GITHUB_REPO</code> â€” owner/repo\n\n"
            "📌 Создать PAT: GitHub → Settings → Developer settings → Tokens",
            parse_mode='HTML'
        )
        return

    await update.message.reply_text("🔄 Синхронизирую config.json с GitHub...")

    try:
        result = await asyncio.to_thread(_sync_config_to_github)
        if result:
            await update.message.reply_text("✅ config.json успешно запушен в GitHub!")
        else:
            await update.message.reply_text("ℹ️ config.json не изменился, пуш не нужен.")
    except Exception as e:
        logger.error(f"Ошибка синхронизации: {e}")
        await update.message.reply_text(f"❌ Ошибка синхронизации: {e}")


def _get_mode_txt_value():
    mode_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mode.txt')
    if os.path.exists(mode_file):
        try:
            with open(mode_file, 'r', encoding='utf-8') as f:
                return f.read().strip().lower() == 'statistics'
        except Exception:
            pass
    return False

def _git_commit_and_push(files_to_sync, commit_msg):
    """Коммитит и пушит файлы напрямую через локальный Git CLI.
    Используется, когда бот запущен под лаунчером."""
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir_safe = repo_dir.replace("\\", "/")
    git_base = ["git", "-c", f"safe.directory={repo_dir_safe}"]
    
    def git_run(*args):
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            return subprocess.run(
                git_base + list(args),
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env
            )
        except FileNotFoundError:
            class DummyResult:
                returncode = 127
                stdout = ""
                stderr = "Git executable not found in PATH"
            return DummyResult()

    # 1. Исправляем remote URL с использованием GITHUB_TOKEN для неинтерактивного пуша
    gh_token = os.environ.get('GITHUB_TOKEN', '')
    if gh_token:
        r_url = git_run("remote", "get-url", "origin")
        r_str = (r_url.stdout or "").strip()
        if r_str:
            fixed = re.sub(r'https://(?:[^@]+@)?github\.com', f'https://{gh_token}@github.com', r_str)
            if fixed != r_str:
                git_run("remote", "set-url", "origin", fixed)

    # 1.5 Разрешаем любые конфликты слияния/autostash в рабочей директории ДО коммита
    status = git_run("status", "--porcelain")
    unmerged = []
    for line in (status.stdout or "").splitlines():
        if len(line) >= 2 and ('U' in line[:2] or line[:2] in ('AA', 'DD')):
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[-1].strip()
            unmerged.append(path_part.strip('"'))
    
    if unmerged:
        logger.info(f"Resolving conflicts on paths before commit: {unmerged}")
        for f in unmerged:
            pick = git_run("checkout", "--theirs", "--", f)
            if pick.returncode != 0:
                git_run("checkout", "--ours", "--", f)
            git_run("add", "--", f)
            git_run("reset", "--", f)

    # 2. Добавляем файлы
    git_run("add", *files_to_sync)

    # 3. Проверяем, есть ли изменения в индексе
    diff = git_run("diff", "--cached", "--quiet")
    if diff.returncode != 0:
        # Есть изменения для коммита
        commit = git_run("commit", "-m", commit_msg, "--", *files_to_sync)
        if commit.returncode != 0:
            err_msg = (commit.stderr or "").strip() or (commit.stdout or "").strip()
            raise Exception(f"Git commit failed: {err_msg}")
        
        # 4. Пушим изменения
        push = git_run("push")
        if push.returncode == 0:
            logger.info(f"Git push successful for: {files_to_sync}")
            return True
        else:
            logger.info(f"First git push failed, trying to pull & rebase... Error: {push.stderr.strip()}")
            # Пробуем сделать pull --rebase и повторить пуш
            pull = git_run("pull", "--rebase", "--autostash")
            if pull.returncode != 0:
                # Конфликт ребейса
                resolved = True
                for attempt in range(10):
                    status = git_run("status", "--porcelain")
                    unmerged = []
                    for line in (status.stdout or "").splitlines():
                        if len(line) >= 2 and line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                            unmerged.append(line[3:].strip().strip('"'))
                    if not unmerged:
                        break
                    
                    for f in unmerged:
                        # Так как мы хотим, чтобы наши настройки победили, берем --theirs при rebase
                        pick = git_run("checkout", "--theirs", "--", f)
                        if pick.returncode != 0:
                            git_run("checkout", "--ours", "--", f)
                        git_run("add", "--", f)
                    
                    env = os.environ.copy()
                    env["GIT_EDITOR"] = "true"
                    env["GIT_TERMINAL_PROMPT"] = "0"
                    cont = subprocess.run(git_base + ["rebase", "--continue"], cwd=repo_dir, capture_output=True, text=True, env=env)
                    if cont.returncode == 0:
                        break
                else:
                    resolved = False
                
                if not resolved:
                    git_run("rebase", "--abort")
                    raise Exception(f"Git rebase conflict could not be resolved automatically. Pull error: {pull.stderr.strip()}")

            # Разрешаем любые конфликты слияния/autostash в рабочей директории
            status = git_run("status", "--porcelain")
            unmerged = []
            for line in (status.stdout or "").splitlines():
                if len(line) >= 2 and ('U' in line[:2] or line[:2] in ('AA', 'DD')):
                    path_part = line[3:].strip()
                    if " -> " in path_part:
                        path_part = path_part.split(" -> ")[-1].strip()
                    unmerged.append(path_part.strip('"'))
            
            if unmerged:
                logger.info(f"Resolving conflicts on paths: {unmerged}")
                for f in unmerged:
                    git_run("checkout", "--theirs", "--", f)
                    git_run("add", "--", f)
                    git_run("reset", "--", f)

            # Пробуем пушить снова
            push = git_run("push")
            if push.returncode == 0:
                logger.info(f"Git push successful after rebase for: {files_to_sync}")
                return True
            else:
                raise Exception(f"Git push failed after rebase: {push.stderr.strip()}")
    else:
        logger.info(f"No changes staged for: {files_to_sync}")
        return False

def _sync_mode_to_github():
    """Пушит plaintext mode.txt в GitHub через Contents API (или напрямую через Git если под лаунчером)."""
    if os.path.exists('mode.txt'):
        with open('mode.txt', 'r', encoding='utf-8') as f:
            content = f.read().strip()
    else:
        content = 'normal'

    if os.environ.get("BOT_RUNNING_UNDER_LAUNCHER") == "1":
        logger.info("Бот запущен под лаунчером. Выполняю прямую синхронизацию mode.txt через Git...")
        return _git_commit_and_push(["mode.txt"], f"Toggle auto-monitoring mode to {content}")

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/mode.txt"
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }

    sha = None
    resp = requests.get(api_url, headers=headers, timeout=15)
    if resp.status_code == 200:
        sha = resp.json().get('sha')
    elif resp.status_code != 404:
        raise Exception(f"GitHub API ошибка ({resp.status_code}): {resp.text[:200]}")

    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')

    payload = {
        'message': f'🔄 Toggle auto-monitoring mode to {content}',
        'content': encoded,
    }
    if sha:
        payload['sha'] = sha

    resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        return True
    else:
        raise Exception(f"GitHub PUT ошибка ({resp.status_code}): {resp.text[:200]}")

def _sync_config_to_github():
    """Пушит config в GitHub через Contents API (или напрямую через Git если под лаунчером).
    Если CONFIG_PASSPHRASE задан — шифрует и пушит config.json.enc.
    Если нет — пушит config.json напрямую."""
    passphrase = os.environ.get("CONFIG_PASSPHRASE")

    # Ре-шифруем конфиг локально перед синхронизацией, если есть пароль
    if passphrase and os.path.exists("config.json"):
        try:
            import config_crypt
            with open("config.json", "rb") as f:
                content = f.read()
            
            # Проверяем, изменился ли контент
            has_changes = True
            if os.path.exists("config.json.enc"):
                try:
                    with open("config.json.enc", "rb") as f_enc:
                        existing_enc = f_enc.read()
                    existing_dec = config_crypt.decrypt(existing_enc, passphrase)
                    if existing_dec == content:
                        has_changes = False
                except Exception:
                    pass
            
            if has_changes:
                encrypted_data = config_crypt.encrypt(content, passphrase)
                with open("config.json.enc", "wb") as f:
                    f.write(encrypted_data)
                logger.info("config.json зашифрован в config.json.enc для синхронизации.")
            else:
                logger.info("Конфиг не изменился, шифрование пропущено.")
        except Exception as e:
            logger.error(f"Ошибка шифрования конфига перед синхронизацией: {e}")
            raise

    if os.environ.get("BOT_RUNNING_UNDER_LAUNCHER") == "1":
        logger.info("Бот запущен под лаунчером. Выполняю прямую синхронизацию config через Git...")
        if passphrase:
            files = ["config.json.enc"]
        else:
            files = ["config.json"]
        return _git_commit_and_push(files, "Sync config from Telegram bot")

    if passphrase:
        target_file = "config.json.enc"
    else:
        target_file = "config.json"

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{target_file}"
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }

    # Получаем текущий SHA файла (нужен для обновления)
    sha = None
    resp = requests.get(api_url, headers=headers, timeout=15)
    if resp.status_code == 200:
        sha = resp.json().get('sha')
    elif resp.status_code != 404:
        raise Exception(f"GitHub API ошибка ({resp.status_code}): {resp.text[:200]}")

    # Читаем локальный config.json
    with open('config.json', 'r', encoding='utf-8') as f:
        content = f.read()

    if passphrase:
        import config_crypt
        encrypted_data = config_crypt.encrypt(content.encode('utf-8'), passphrase)
        encoded = base64.b64encode(encrypted_data).decode('ascii')
    else:
        encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')

    payload = {
        'message': f'🔄 Sync {target_file} from Telegram bot',
        'content': encoded,
    }
    if sha:
        payload['sha'] = sha

    resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        return True
    else:
        raise Exception(f"GitHub PUT ошибка ({resp.status_code}): {resp.text[:200]}")



async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop — остановка бота через Telegram."""
    global chat_id
    user_chat_id = update.effective_chat.id
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы.")
        return

    await update.message.reply_text(
        "⏹ <b>Закончить сейчас?</b>\n"
        "Текущая работа будет остановлена.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, остановить", callback_data="set:stop:confirm")],
            [InlineKeyboardButton("🔙 Нет, продолжить", callback_data="set:stop:cancel")],
        ])
    )


async def handle_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок постоянной клавиатуры."""
    # Очищаем незавершённый ввод, если был.
    context.user_data.pop('input_state', None)
    context.user_data.pop('editing_skin_id', None)
    context.user_data.pop('editing_edition_id', None)
    context.user_data.pop('input_return_callback', None)
    context.user_data.pop('input_return_label', None)
    context.user_data.pop('recheck_rare_price', None)

    text = update.message.text.strip()
    english_alias = text in ("Settings", "Recheck", "Min price", "Min Price", "Check", "Stop")

    if english_alias:
        await update.message.reply_text("⌨️ Клавиатура обновлена.", reply_markup=main_reply_keyboard())

    if text in ("⚙️ Настройки", "Settings"):
        from handlers import settings_command
        await settings_command(update, context)

    elif text in ("🔎 Проверка", "🔄 Перепроверка", "💰 Мін. прайс", "💰 Мин. прайс", "Recheck", "Min price", "Min Price", "Check"):
        from handlers import _show_check_menu_as_new_message
        await _show_check_menu_as_new_message(update, context)

    elif text in ("⏹ Стоп", "Stop"):
        await stop_command(update, context)

# ═══════════════════════════════════════════════════════════════════════════════
# Startup diagnostics helpers (adapted from eSearch)
# ═══════════════════════════════════════════════════════════════════════════════

def _humanize_age(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _short_secret(value):
    if not value:
        return "❌ не задан"
    if len(value) <= 12:
        return "******"
    return f"{value[:6]}…{value[-4:]}"


def _normalize_github_repo(value):
    if not value:
        return "—"
    raw = value.strip().replace("\\", "/")
    if "github.com" in raw:
        tail = raw.split("github.com", 1)[1].lstrip("/:").split("?", 1)[0]
        # Strip token from URL if present (user:token@github.com)
        if "@" in raw.split("github.com")[0]:
            pass  # tail is already clean
        parts = tail.split("/")
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
            return repo[:-4] if repo.endswith(".git") else repo
    raw = raw[:-4] if raw.endswith(".git") else raw
    return raw


def _get_git_remote_repo():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_dir}", "remote", "get-url", "origin"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode != 0:
            return "⚠️ remote not found"
        return _normalize_github_repo(result.stdout.strip())
    except Exception as e:
        return f"⚠️ {e}"


def _validate_telegram_bot():
    """Validate Telegram bot token by calling getMe. Returns (status_str, token_bot_id, actual_bot_id)."""
    if not TELEGRAM_BOT_TOKEN:
        return "❌ не задан", "—", "—"
    token_bot_id = TELEGRAM_BOT_TOKEN.split(":", 1)[0] if ":" in TELEGRAM_BOT_TOKEN else "?"
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe")
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if not data.get("ok"):
            return "❌ getMe ok=false", token_bot_id, "—"
        bot_data = data.get("result", {})
        actual_id = str(bot_data.get("id", "—"))
        username = bot_data.get("username") or "?"
        match = "✅" if actual_id == token_bot_id else "⚠️"
        return f"{match} @{username} id={actual_id}", token_bot_id, actual_id
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "❌ СЛЕТЕЛ/INVALID (401 Unauthorized)", token_bot_id, "—"
        return f"⚠️ HTTP {e.code}", token_bot_id, "—"
    except Exception as e:
        return f"⚠️ {e}", token_bot_id, "—"


def _validate_github_pat():
    """Validate GitHub token by calling /user. Returns status string."""
    if not GITHUB_TOKEN:
        return "❌ не задан"
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return f"✅ {data.get('login', '?')}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "❌ СЛЕТЕЛ (401 Unauthorized)"
        return f"⚠️ HTTP {e.code}"
    except Exception as e:
        return f"⚠️ {e}"


def _validate_github_actions(repo):
    """Query latest workflow run status. Returns human-readable summary."""
    if not repo or repo == "—":
        return "— нет репозитория"
    if not GITHUB_TOKEN:
        return "⚠️ нет GH Token"
    try:
        import urllib.request, urllib.error
        from datetime import datetime, timezone
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/runs?per_page=1",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        runs = data.get("workflow_runs") or []
        if not runs:
            return "— нет запусков"
        run = runs[0]
        status = run.get("status") or "?"
        conclusion = run.get("conclusion")
        name = run.get("name") or "workflow"
        created = run.get("created_at") or run.get("run_started_at")
        age_str = ""
        if created:
            try:
                dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                age_str = f" {_humanize_age(age)} ago"
            except Exception:
                pass
        if status in ("queued", "in_progress", "waiting", "requested"):
            return f"🔄 {status}{age_str} ({name})"
        emoji = {
            "success": "✅",
            "failure": "❌",
            "cancelled": "🚫",
            "skipped": "⏭️",
            "timed_out": "⏱️",
            "neutral": "➖",
            "action_required": "⚠️",
        }.get(conclusion, "⚠️")
        return f"{emoji} {conclusion or status}{age_str} ({name})"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "❌ 401 — токен не имеет доступа"
        if e.code == 404:
            return "❌ 404 — репозиторий или Actions не найдены"
        return f"⚠️ HTTP {e.code}"
    except Exception as e:
        return f"⚠️ {e}"


def _log_startup_banner(mode):
    """Print full diagnostic banner at startup (both local and GitHub Actions)."""
    remote_repo = _get_git_remote_repo()
    env_repo = _normalize_github_repo(GITHUB_REPO) if GITHUB_REPO else "—"
    remote_ok = not remote_repo.startswith("⚠️") and remote_repo != "—"

    tg_status, token_bot_id, actual_bot_id = _validate_telegram_bot()
    gh_pat_user = _validate_github_pat()
    gh_actions = _validate_github_actions(remote_repo if remote_ok else env_repo)

    enabled_skins = config.get_enabled_skins()
    skin_prices = [s.get('price', 0) for s in enabled_skins.values()]
    max_price = max(skin_prices, default=config.max_price) + config.pve_bonus

    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║           FunPay Monitor Bot                ║")
    logger.info("╠══════════════════════════════════════════════╣")
    logger.info("  Mode:       %s", mode)
    logger.info("  Repo:       %s", remote_repo)
    logger.info("  Env repo:   %s", env_repo)
    if remote_ok and env_repo != "—":
        match = "✅" if remote_repo.lower() == env_repo.lower() else "⚠️ НЕСОВПАДЕНИЕ"
        logger.info("  Repo match: %s", match)
    logger.info("  GH Token:   %s", gh_pat_user)
    logger.info("  GH Actions: %s", gh_actions)
    logger.info("  Telegram:   %s", tg_status)
    logger.info("  Chat ID:    %s", chat_id or "❌ не задан")
    logger.info("╠══════════════════════════════════════════════╣")
    logger.info("  Скинов:     %d/%d", len(enabled_skins), len(config.get_all_skins()))
    logger.info("  PVE-слов:   %d подтв.", len(config.get_confirmed_pve()))
    logger.info("  Исключений: %d фраз", len(config.get_exclude_keywords()))
    logger.info("  Макс. цена: %d₽", max_price)
    logger.info("  Seen IDs:   %d", len(seen_ids))
    logger.info("  Banned IDs: %d", len(banned_ids))
    logger.info("  Интервал:   %ds", config.check_interval)
    logger.info("╚══════════════════════════════════════════════╝")


async def run_once(verbose=False):
    global bot_username
    """Запуск один раз и выход, для GitHub Actions / Cron."""
    setup_logging(verbose=verbose)
    try:
        env_chat = os.environ.get('TELEGRAM_CHAT_ID')
        if env_chat:
            import json
            sent_offers = {}
            if os.path.exists('sent_offers.json'):
                try:
                    with open('sent_offers.json', 'r', encoding='utf-8') as f:
                        sent_offers = json.load(f)
                except Exception:
                    sent_offers = {}
            sent_offers['_telegram_chat_id'] = str(env_chat)
            with open('sent_offers.json', 'w', encoding='utf-8') as f:
                json.dump(sent_offers, f, indent=2)
    except Exception:
        pass
    if not TELEGRAM_BOT_TOKEN:
        print("FATAL: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    load_chat_id()
    load_seen_ids()
    load_sent_offers()
    load_banned_ids()
    load_banned_sellers()
    load_seller_map()

    if not chat_id:
        print("FATAL: Chat ID not found. Set TELEGRAM_CHAT_ID env var")
        sys.exit(1)

    _log_startup_banner("ONE-SHOT")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        me = await bot.get_me()
        bot_username = me.username
        logger.info("Bot connected: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("FATAL: Telegram token invalid or network error: %s", e)
        print(f"FATAL: Cannot connect to Telegram API: {e}")
        sys.exit(1)

    # Fetch pending updates from Telegram when bot was offline
    try:
        logger.info("Checking pending Telegram updates...")
        updates = await bot.get_updates(offset=0, timeout=5)
        for update in updates:
            if update.message and update.message.text:
                text = update.message.text
                if text.startswith('/start'):
                    parts = text.split()
                    if len(parts) > 1:
                        payload = parts[1]
                        if payload.startswith('ban_'):
                            ban_offer_id = extract_offer_id(payload.replace('ban_', '', 1))
                            if ban_offer_id:
                                banned_ids.add(ban_offer_id)
                                save_banned_ids()
                                logger.info(f"🚫 [Telegram Update] Добавлен лот в бан: {ban_offer_id}")
                        elif payload.startswith('banseller_'):
                            short_id = payload.replace('banseller_', '', 1)
                            seller_name = get_seller_name_by_id(short_id)
                            if seller_name:
                                banned_sellers.add(seller_name)
                                save_banned_sellers()
                                logger.info(f"👤 [Telegram Update] Добавлен продавец в бан: {seller_name}")
        if updates:
            last_id = updates[-1].update_id
            await bot.get_updates(offset=last_id + 1, limit=1)
            logger.info(f"Cleared {len(updates)} pending updates.")
    except Exception as e:
        logger.warning(f"Не удалось получить обновления Telegram: {e}")

    sent = await process_offers(bot_instance=bot, skip_seen=True)
    logger.info("=== Done (sent: %s) ===", sent)

async def post_init(application):
    global bot_username
    """Expose /start in Telegram's left menu for quick keyboard restore."""
    try:
        bot_username = (await application.bot.get_me()).username
        application.bot_data['bot_username'] = bot_username
    except Exception as e:
        logger.warning(f"Не удалось получить username бота: {e}")
    await application.bot.set_my_commands([
        BotCommand("start", "Запуск / обновить клавиатуру"),
    ])
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Telegram menu button set to commands mode")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Запустить один раз и выйти')
    parser.add_argument('--verbose', '-v', action='store_true', help='Подробный вывод логов')
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if args.once:
        asyncio.run(run_once(verbose=args.verbose))
        return

    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: не задан TELEGRAM_BOT_TOKEN")
        return

    load_chat_id()
    load_seen_ids()
    load_sent_offers()
    load_banned_ids()
    load_banned_sellers()
    load_seller_map()

    _log_startup_banner("LOCAL")
    print("Запустите бота и напишите ему /start в Telegram.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("banseller", banseller_cmd))
    application.add_handler(CommandHandler("unbanseller", unbanseller_cmd))
    application.add_handler(CommandHandler("recheck", recheck))
    application.add_handler(CommandHandler("pricetest", pricetest))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("validate", validate_command))

    # Обработчик кнопок постоянной клавиатуры.
    button_filter = filters.Regex(r'^(⚙️ Настройки|Settings|🔎 Проверка|🔄 Перепроверка|Recheck|💰 Мін\. прайс|💰 Мин\. прайс|Min price|Min Price|Check|⏹ Стоп|Stop)$')
    application.add_handler(MessageHandler(button_filter, handle_button_text))

    # Экспортируем process_offers и bot_mode для settings_handlers
    application.bot_data['process_offers'] = process_offers
    application.bot_data['bot_mode'] = bot_mode
    application.bot_data['build_recheck_snapshot'] = build_recheck_run_snapshot
    application.bot_data['build_recheck_log'] = build_cached_recheck_log_async
    if GITHUB_TOKEN and GITHUB_REPO:
        application.bot_data['sync_fn'] = _sync_config_to_github
        application.bot_data['sync_mode_fn'] = _sync_mode_to_github

    # Регистрируем панель настроек
    register_settings_handlers(application, config, chat_id, seen_ids, banned_ids)

    job_queue = application.job_queue
    job_queue.run_repeating(check_funpay_job, interval=config.check_interval, first=10)

    logger.info("🤖 Бот запущен, запуск мониторинга...")

    import signal as _signal
    _signal.signal(_signal.SIGINT, lambda s, f: os._exit(0))
    application.run_polling(drop_pending_updates=True, stop_signals=None)

if __name__ == "__main__":
    main()
