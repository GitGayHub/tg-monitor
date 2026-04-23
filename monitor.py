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

from config_manager import ConfigManager
from price_history import init_price_history_db, record_price_snapshot, record_red_flag, get_latest_top3
from settings_handlers import register_settings_handlers

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# --- НАСТРОЙКИ ---
# Токены и репозиторий должны приходить только из переменных окружения.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')

# URLs с фильтром "только продажа" (несколько категорий Fortnite)
FUNPAY_ACCOUNTS_URL = 'https://funpay.com/lots/248/?offer_type=sell'
FUNPAY_OTHER_URL = 'https://funpay.com/lots/1098/?offer_type=sell'
FUNPAY_URLS = [
    FUNPAY_ACCOUNTS_URL,  # Аккаунты для Fortnite (все платформы)
]

# GitHub API (для /sync — синхронизация config.json с репозиторием)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = os.environ.get('GITHUB_REPO') or os.environ.get('GITHUB_REPOSITORY')

CHAT_ID_FILE = 'chat_id.txt'
SEEN_IDS_FILE = 'seen_ids.txt'
SENT_OFFERS_FILE = 'sent_offers.json'
BANNED_IDS_FILE = 'banned_ids.txt'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', stream=sys.stdout, force=True)
logger = logging.getLogger()
process_offers_lock = asyncio.Lock()
HTTP_TIMEOUT = (10, 20)

# Глобальные переменные
seen_ids = set()
sent_offers = {}
banned_ids = set()
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
    global seen_ids
    try:
        with open(SEEN_IDS_FILE, 'r') as f:
            all_ids = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        seen_ids = set()
        return
    if len(all_ids) > SEEN_IDS_MAX:
        trimmed = all_ids[-SEEN_IDS_MAX:]
        logger.info(f"🧹 Очистка seen_ids: {len(all_ids)} → {len(trimmed)}")
        with open(SEEN_IDS_FILE, 'w') as f:
            f.write('\n'.join(trimmed) + '\n')
        seen_ids = set(trimmed)
    else:
        seen_ids = set(all_ids)

def save_seen_id(offer_id):
    with open(SEEN_IDS_FILE, 'a') as f:
        f.write(offer_id + '\n')

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

def clear_seen_ids():
    global seen_ids
    seen_ids = set()
    with open(SEEN_IDS_FILE, 'w') as f:
        f.write('')

def load_sent_offers():
    global sent_offers
    try:
        with open(SENT_OFFERS_FILE, 'r', encoding='utf-8') as f:
            sent_offers = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sent_offers = {}

def save_sent_offer(offer_id, price, description):
    global sent_offers
    sent_offers[offer_id] = {
        'price': price,
        'description': description,
        'timestamp': time.time()
    }
    with open(SENT_OFFERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sent_offers, f, ensure_ascii=False, indent=2)

def is_offer_changed(offer_id, new_price, new_description):
    if offer_id not in sent_offers:
        return True
    old = sent_offers[offer_id]
    if abs(old.get('price', 0) - new_price) > 1:
        return True
    return False

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


def contains_positive_keyword(text, positive_keywords):
    """Returns the first matching positive phrase, or None.
    Positive phrases indicate email/rebind IS available (whitelist)."""
    normalized_text = normalize_match_text(text)
    for pos_word in (positive_keywords or []):
        normalized_pos = normalize_match_text(pos_word)
        if normalized_pos and normalized_pos in normalized_text:
            return pos_word
    return None


def contains_exclude_keyword(text, exclude_keywords, positive_keywords=None):
    """Returns the first matching exclude phrase, or None.
    If positive_keywords is provided and matches, exclude is overridden (returns None).
    This whitelist behavior fixes false positives like 'перепривяжу почту'."""
    normalized_text = normalize_match_text(text)
    # Whitelist: if any positive phrase is found, don't exclude
    if positive_keywords:
        for pos_word in positive_keywords:
            normalized_pos = normalize_match_text(pos_word)
            if normalized_pos and normalized_pos in normalized_text:
                return None
    for exclude_word in exclude_keywords:
        normalized_exclude = normalize_match_text(exclude_word)
        if normalized_exclude and normalized_exclude in normalized_text:
            return exclude_word
    return None


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
    text_lower = text.lower()
    found_skins = []

    for skin_id, skin_data in skins_dict.items():
        for keyword in skin_data['keywords']:
            pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
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
    details = [f"{item['name']}({item['price']}₽)" for item in top_items]
    if len(items) > 2:
        ignored_count = len(items) - 2
        details.append(f"+ещё {ignored_count} шт.(не учтено)")
    description = " + ".join(details) + f" = {total_price}₽"
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


def build_http_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Cookie': 'cy=rub'
    })
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


async def build_recheck_log(snapshot, progress_callback=None):
    positions = snapshot.get('positions', [])
    total = len(positions)
    sent_position_ids = set(snapshot.get('sent_position_ids', []))
    diagnostics = []

    for idx, position in enumerate(positions, start=1):
        if progress_callback:
            await progress_callback(idx - 1, total, position['name'])

        if position['type'] == 'edition':
            any_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=False))
            record_price_snapshot('edition', position['id'], position['name'], 'any', [any_result] if any_result else [], source='recheck_log')
            limit_price = position['limit_price']
            if position['id'] in sent_position_ids:
                status = "✅ Отправлено"
            elif any_result and any_result['price'] <= limit_price:
                status = "✅ Отправлено"
            elif any_result:
                status = "💸 Дороже лимита"
            else:
                status = "❌ Не найдено"

            diagnostics.append({
                'type': 'edition',
                'id': position['id'],
                'name': position['name'],
                'limit_text': _format_snapshot_price(limit_price),
                'min_any_text': any_result['price_text'] if any_result else "—",
                'min_pve_text': None,
                'status': status,
            })

        elif position['type'] == 'pve':
            any_result = _pick_min_result(await search_min_price(position['keywords_any'], require_pve=False))
            confirmed_result = _pick_min_result(await search_min_price(position['keywords_confirmed'], require_pve=False))
            results_for_history = [r for r in (confirmed_result, any_result) if r]
            record_price_snapshot('pve', position['id'], position['name'], 'confirmed', results_for_history, source='recheck_log')
            limit_price = position['limit_price']

            if position['id'] in sent_position_ids:
                status = "✅ Отправлено"
            elif confirmed_result and confirmed_result['price'] <= limit_price:
                status = "✅ Отправлено"
            elif confirmed_result:
                status = "💸 Дороже лимита"
            elif any_result:
                status = "❔ Нет лотов с PVE"
            else:
                status = "❌ Не найдено"

            diagnostics.append({
                'type': 'pve',
                'id': position['id'],
                'name': position['name'],
                'limit_text': _format_snapshot_price(limit_price),
                'min_any_text': any_result['price_text'] if any_result else "—",
                'min_pve_text': confirmed_result['price_text'] if confirmed_result else "—",
                'status': status,
            })

        else:
            any_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=False))
            pve_result = _pick_min_result(await search_min_price(position['keywords'], require_pve=True))
            history_mode = 'pve' if position.get('require_pve', False) else 'any'
            results_for_history = [pve_result] if history_mode == 'pve' else [any_result]
            if history_mode != 'pve' and not any_result and pve_result:
                results_for_history = [pve_result]
            record_price_snapshot('skin', position['id'], position['name'], history_mode, [r for r in results_for_history if r], source='recheck_log')
            limit_price = position['limit_price']
            require_pve = position.get('require_pve', False)

            if position['id'] in sent_position_ids:
                status = "✅ Отправлено"
            elif require_pve:
                if pve_result and pve_result['price'] <= limit_price:
                    status = "✅ Отправлено"
                elif pve_result:
                    status = "💸 Дороже лимита"
                elif any_result:
                    status = "🔒 Только без PVE"
                else:
                    status = "❌ Не найдено"
            else:
                if any_result and any_result['price'] <= limit_price:
                    status = "✅ Отправлено"
                elif any_result:
                    status = "💸 Дороже лимита"
                elif pve_result and pve_result['price'] <= limit_price:
                    status = "✅ Отправлено"
                elif pve_result:
                    status = "💸 Дороже лимита"
                else:
                    status = "❌ Не найдено"

            diagnostics.append({
                'type': 'skin',
                'id': position['id'],
                'name': position['name'],
                'limit_text': _format_snapshot_price(limit_price),
                'min_any_text': any_result['price_text'] if any_result else "—",
                'min_pve_text': pve_result['price_text'] if pve_result else "—",
                'status': status,
                'require_pve': require_pve,
            })

        if progress_callback:
            await progress_callback(idx, total, position['name'])

    return diagnostics


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
                response = session.get(url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                items = soup.find_all('a', class_='tc-item')
                for item in items:
                    href = item.get('href', '')
                    if href not in seen_hrefs:
                        seen_hrefs.add(href)
                        item['data-source-lot'] = source_lot
                        all_items.append(item)
                logger.info(f"📄 {url} [{source_lot}]: {len(items)} предложений")
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
            response = session.get(offer_url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
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
            rating_text = "Нет рейтинга"

        return full_description, rating_text

    except Exception as e:
        logger.error(f"Ошибка при загрузке деталей {offer_url}: {e}")
        return None, "Ошибка загрузки"

async def get_offer_details(offer_url):
    """Получает детали предложения — НЕ блокирует event loop."""
    return await asyncio.to_thread(_sync_get_offer_details, offer_url)


def _sync_search_min_price(keywords, require_pve=False):
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
                response = session.get(search_url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
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

                logger.info(f"Мин. прайс: '{kw}' → {len(items)} предложений")
                if kw != keywords[-1]:
                    time.sleep(random.uniform(0.5, 1.0))

            except Exception as e:
                logger.error(f"Ошибка поиска мин. цены для '{kw}': {e}")

        # Fallback: только раздел аккаунтов. Мин. прайс не должен подтягивать мусор из "Прочее".
        current_best = min((r['price'] for r in all_results.values()), default=None)
        for list_url in [FUNPAY_ACCOUNTS_URL]:
            try:
                response = session.get(list_url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
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
    pve_tokens = [normalize_match_text(pk) for pk in config.get_confirmed_pve()] if require_pve else []
    validated = []
    for candidate in results[:12]:
        href = candidate['href']
        if href not in details_cache:
            try:
                get_offer_texts(href)
                time.sleep(random.uniform(0.25, 0.5))
            except Exception as e:
                logger.warning(f"Мін. прайс: не удалось открыть описание {href}: {e}")
        full_desc, full_text = details_cache.get(href, ("", ""))
        if full_text and contains_exclude_keyword(full_text, exclude_kws, positive_kws):
            logger.info(f"Мін. прайс: отфильтровано по описанию — {href}")
            continue
        if require_pve:
            combined = f"{normalize_match_text(candidate.get('description',''))} {full_text}"
            if not any(token in combined for token in pve_tokens if token):
                continue
        if full_desc:
            candidate['description'] = full_desc[:200]
        validated.append(candidate)
        if len(validated) >= 3:
            break
    return validated


async def search_min_price(keywords, require_pve=False):
    """Поиск минимальной цены по всем ключевым словам — НЕ блокирует event loop."""
    return await asyncio.to_thread(_sync_search_min_price, keywords, require_pve)


async def search_min_price_single(keyword, require_pve=False):
    """Поиск по одному ключевому слову — для прогресс-бара."""
    return await asyncio.to_thread(_sync_search_min_price, [keyword], require_pve)


async def process_offers(bot_instance=None, context=None, skip_seen=True, max_price_override=None,
                         rare_override=None, pve_override=None, candidate_limit=10,
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
        logger.info(f"🔍 Проверка предложений... [{mode}] (макс. цена авто)")

        if premium_only:
            search_keywords = config.get_premium_pve()
            logger.info(f"🏆 PREMIUM режим: ищу только издания ({len(search_keywords)} слов)")
        else:
            search_keywords = config.get_search_keywords(include_unconfirmed_pve=include_unconfirmed_pve)

        skins_dict = config.get_enabled_skins_dict()
        exclude_keywords = config.get_exclude_keywords()
        positive_keywords = config.get_positive_keywords()
        search_mode = config.search_mode
        confirmed_pve_only = search_mode == 'pve_only'
        confirmed_pve_enabled_effective = config.confirmed_pve_enabled if confirmed_pve_enabled_override is None else bool(confirmed_pve_enabled_override)
        confirmed_pve_price_effective = config.confirmed_pve_price if confirmed_pve_price_override is None else int(confirmed_pve_price_override)

        if max_price_override is not None:
            effective_max_price = max_price_override
        elif confirmed_pve_only:
            effective_max_price = confirmed_pve_price_effective
        else:
            skin_prices = [s.get('price', 0) for s in skins_dict.values()]
            effective_max_price = max(skin_prices, default=config.max_price) + config.pve_bonus
            if confirmed_pve_enabled_effective:
                effective_max_price = max(effective_max_price, confirmed_pve_price_effective)

        if rare_override is not None or pve_override is not None or max_price_override is not None:
            logger.info(f"PRICETEST: rare={rare_override}, pve={pve_override}, max_price={effective_max_price}")

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
            return 0

        if cancelled():
            logger.info("⏹️ Проверка остановлена пользователем во время загрузки списка")
            return -2

        if progress_chat_id and progress_bot:
            try:
                progress_msg = await progress_bot.send_message(
                    chat_id=progress_chat_id,
                    text=(
                        "🔄 <b>Перепроверка</b>\n\n"
                        "📍 Этап: Отбор лотов\n"
                        "▱▱▱▱▱▱▱▱▱▱▱▱ 0%\n"
                        f"📦 Прогресс: 0/{max(len(listings), 1)}\n"
                        "🔎 Сейчас: Список лотов получен, начинаю отбор\n"
                        "✅ Отправлено: 0"
                    ),
                    parse_mode='HTML'
                )
            except Exception:
                progress_msg = None

        total_listings = len(listings)
        already_seen_count = 0
        banned_count = 0
        candidates = []
        all_listing_hrefs = set()  # all hrefs on current page (for staleness check)

        # Build keyword maps for auto price tracking
        auto_price_map = {}   # skin_id → [offers] (mode=any)
        auto_pve_map = {}     # skin_id → [offers] (mode=pve)
        auto_edition_map = {} # edition_id → [offers]
        auto_pve_confirmed = []  # top-3 cheapest confirmed-PVE offers
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

            already_seen = skip_seen and offer_id in seen_ids
            if already_seen:
                already_seen_count += 1
                # Continue processing to update auto_price_map for stats,
                # but will skip notification logic below.

            desc_div = item.find('div', class_='tc-desc-text')
            price_div = item.find('div', class_='tc-price')
            user_div = item.find('div', class_='media-user-name')

            short_description = desc_div.get_text(strip=True) if desc_div else ""
            price_text = price_div.get_text(strip=True) if price_div else "Нет цены"
            user = user_div.get_text(strip=True) if user_div else "Неизвестный"
            short_desc_lower = normalize_match_text(short_description)

            if 'аренда' in short_desc_lower and 'продажа' not in short_desc_lower:
                if skip_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id)
                continue

            source_lot = item.get('data-source-lot', 'accounts')
            item_keywords = config.get_prochee_keywords() if source_lot == 'prochee' else search_keywords

            matched_keyword = ""
            for keyword in item_keywords:
                pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
                if re.search(pattern, short_desc_lower):
                    matched_keyword = keyword
                    break

            # --- Auto price tracking: detect skins BEFORE exclude filter ---
            price_value = parse_price(price_text)
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

            # Track confirmed PVE (cheapest accounts with confirmed PVE)
            if price_value is not None and skip_seen and source_lot != 'prochee':
                if has_pve(short_desc_lower, include_unconfirmed=False):
                    auto_pve_seen = True
                    if not is_excluded:
                        auto_pve_confirmed.append(
                            {'price': price_value, 'price_text': price_text,
                             'href': href, 'seller': user, 'name': 'STW'})
                        auto_pve_confirmed.sort(key=lambda x: x['price'])
                        if len(auto_pve_confirmed) > 3:
                            auto_pve_confirmed.pop()

            if not matched_keyword:
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id)
                continue

            if is_excluded:
                if not already_seen:
                    logger.info(f"🚫 Исключено в кратком описании: {short_description[:40]}...")
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id)
                continue

            if log_state and price_value is not None:
                for position_id in log_keyword_map.get(matched_keyword.lower(), ()):
                    log_entry = log_state.get(position_id)
                    if log_entry:
                        update_recheck_log_offer(log_entry, 'any_offer', price_value, price_text, href)
            if price_value is None or price_value > effective_max_price:
                if skip_seen and not already_seen:
                    seen_ids.add(offer_id)
                    save_seen_id(offer_id)
                continue

            # Skip notification for already-seen offers, but auto_price_map is already updated
            if already_seen:
                continue

            candidates.append({
                'offer_id': offer_id,
                'href': href,
                'short_description': short_description,
                'price_text': price_text,
                'price_value': price_value,
                'user': user,
                'matched_keyword': matched_keyword,
                'source_lot': source_lot,
            })

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

            async def _validate_offer(offer):
                """Load full description and check exclude keywords. Returns True if OK."""
                href = offer.get('href', '')
                if href in validated_cache:
                    return validated_cache[href]
                try:
                    full_desc, _ = await asyncio.wait_for(
                        get_offer_details(href), timeout=15
                    )
                    if full_desc:
                        excluded = contains_exclude_keyword(full_desc, exclude_keywords, positive_keywords)
                        if excluded:
                            logger.info(f"🚫 Авто-валидация: исключён по описанию ('{excluded}'): {href}")
                            validated_cache[href] = False
                            return False
                except Exception as e:
                    logger.debug(f"⚠️ Авто-валидация: не удалось загрузить {href}: {e}")
                validated_cache[href] = True
                return True

            async def _validate_offers_list(offers):
                """Validate top offers, remove excluded, return cleaned list."""
                if not offers:
                    return offers
                cleaned = []
                for offer in offers:
                    if await _validate_offer(offer):
                        cleaned.append(offer)
                return cleaned

            # Validate cheapest offers in all maps
            for sid in list(auto_price_map.keys()):
                auto_price_map[sid] = await _validate_offers_list(auto_price_map[sid])
            for sid in list(auto_pve_map.keys()):
                auto_pve_map[sid] = await _validate_offers_list(auto_pve_map[sid])
            for eid in list(auto_edition_map.keys()):
                auto_edition_map[eid] = await _validate_offers_list(auto_edition_map[eid])
            auto_pve_confirmed = await _validate_offers_list(auto_pve_confirmed)

            if validated_cache:
                n_checked = len(validated_cache)
                n_removed = sum(1 for v in validated_cache.values() if not v)
                logger.info(f"🔍 Авто-валидация: проверено {n_checked} описаний, исключено {n_removed}")

            parts = []

            def _old_offer_gone(item_type, item_id, mode):
                """Check if previously saved offer is no longer on the listing page."""
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
            parts.append(f"STW: {'да' if auto_pve_confirmed else '—'}")

            logger.info(f"📈 Авто-мониторинг: {', '.join(parts)}")

        new_candidates = len(candidates)
        logger.info(
            f"📊 Статистика: Всего на сайте: {total_listings} | Уже просмотрено: {already_seen_count} | "
            f"Забанено: {banned_count} | Новых кандидатов: {new_candidates}"
        )

        candidates.sort(key=lambda x: x['price_value'])
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
                ed_price = editions.get(matched_edition, {}).get('price', effective_max_price)
                if seller_price > ed_price:
                    logger.info(f"💸 Дорого для {matched_edition}: {seller_price}₽ > {ed_price}₽")
                    continue
                price_breakdown = f"🏆 {matched_edition.replace('_', ' ').title()} до {ed_price}₽"
            else:
                if search_mode != 'skins_only' and has_new_pve(combined_text):
                    logger.info(f"⛔ Пропуск (новое PVE/STW): {candidate['short_description'][:40]}...")
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

                pure_unconfirmed_pve_match = include_unconfirmed_pve and max_price_override is not None and has_pve_flag
                pure_confirmed_pve_match = (confirmed_pve_only or confirmed_pve_enabled_effective) and has_confirmed_pve_diag
                should_skip = not found_skins and not pure_confirmed_pve_match and not pure_unconfirmed_pve_match

                if should_skip:
                    logger.info(f"⏭️ Пропуск (нет ценных скинов/PVE): {candidate['short_description'][:40]}...")
                    continue

                if include_unconfirmed_pve and has_pve_flag:
                    has_confirmed = has_pve(combined_text, include_unconfirmed=False)
                    if has_confirmed:
                        logger.info(f"✅ Пропуск (подтв. PVE, уже в мониторинге): {candidate['short_description'][:40]}...")
                        continue

                all_require_pve = found_skins and all(
                    config.get_all_skins().get(s['id'], {}).get('require_pve', False) for s in found_skins
                )
                pve_for_price = False if all_require_pve else has_pve_flag

                if not found_skins and pure_confirmed_pve_match:
                    my_max_price = max_price_override if max_price_override is not None and confirmed_pve_only else confirmed_pve_price_effective
                    price_breakdown = f"Подтв. PVE до {my_max_price}₽"
                elif not found_skins and pure_unconfirmed_pve_match:
                    my_max_price = effective_max_price
                    price_breakdown = f"PVE до {effective_max_price}₽"
                else:
                    my_max_price, price_breakdown = calculate_max_price(
                        found_skins,
                        pve_for_price,
                        rare_override=rare_override,
                        pve_override=pve_override,
                    )

                seller_price = candidate['price_value']
                if seller_price > my_max_price:
                    logger.info(f"💸 Слишком дорого: {seller_price}₽ > {my_max_price}₽ ({price_breakdown})")
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

            ban_href = build_ban_link(offer_id)
            ban_line = f"🚫 <a href='{ban_href}'>Бан</a>" if ban_href else f"🚫 Бан: /ban {offer_id}"
            msg = (
                f"🔔 <b>Найдено предложение!</b>\n\n"
                f"⭐ <b>Главное:</b> {main_feature}\n"
                f"💰 <b>Цена:</b> <a href='{href}'>{candidate['price_text']}</a>\n"
                f"🧟 <b>PVE:</b> {pve_text}\n"
                f"📊 <b>Оценка:</b> {price_breakdown}\n"
                f"📌 <b>Название:</b> {candidate['short_description']}\n"
                f"🎮 <b>Скины:</b> {skins_list}\n"
                f"👤 <b>Продавец:</b> {candidate['user']}\n"
                f"{rating_emoji} <b>Рейтинг:</b> {rating_text}\n"
                f"🔗 <a href='{href}'>Ссылка на товар</a>"
            )
            link_line = f"🔗 <a href='{href}'>Ссылка</a>"
            msg = msg.replace(f"🔗 <a href='{href}'>Ссылка на товар</a>", f"{ban_line}\n{link_line}")

            try:
                if context:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                elif bot_instance:
                    await bot_instance.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')

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
                    save_seen_id(offer_id)
                except Exception as e:
                    logger.warning(f"Не удалось сохранить seen_id: {e}")

                try:
                    save_sent_offer(offer_id, candidate['price_value'], candidate['short_description'])
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
            logger.info("⏭️ Фоновая проверка пропущена: выполняется ручной режим")
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

    max_price_override = max(rare_price, pve_price) if pve_price is not None else rare_price
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
        f"📊 <b>Оценка:</b> топ-2 слота\n"
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
        effective_max = max(effective_max, config.confirmed_pve_price + config.pve_bonus)

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


def _sync_config_to_github():
    """Пушит config.json в GitHub через Contents API."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json"
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

    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')

    payload = {
        'message': '🔄 Sync config.json from Telegram bot',
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
        from settings_handlers import settings_command
        await settings_command(update, context)

    elif text in ("🔎 Проверка", "🔄 Перепроверка", "💰 Мін. прайс", "💰 Мин. прайс", "Recheck", "Min price", "Min Price", "Check"):
        from settings_handlers import _show_check_menu_as_new_message
        await _show_check_menu_as_new_message(update, context)

    elif text in ("⏹ Стоп", "Stop"):
        await stop_command(update, context)

async def run_once():
    global bot_username
    """Запуск один раз и выход, для GitHub Actions / Cron."""
    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: не задан TELEGRAM_BOT_TOKEN")
        return

    load_chat_id()
    load_seen_ids()
    load_sent_offers()
    load_banned_ids()

    if not chat_id:
        print("ОШИБКА: Chat ID не найден. Установите переменную окружения TELEGRAM_CHAT_ID")
        return

    print("--- Запуск в режиме ONE-SHOT (GitHub Actions) ---")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        bot_username = (await bot.get_me()).username
    except Exception:
        pass
    await process_offers(bot_instance=bot, skip_seen=True)
    print("--- Готово ---")

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
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
        return

    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: не задан TELEGRAM_BOT_TOKEN")
        return

    load_chat_id()
    load_seen_ids()
    load_sent_offers()
    load_banned_ids()

    enabled_skins = config.get_enabled_skins()
    skin_names = [sid.replace('_', ' ').title() for sid in enabled_skins.keys()]

    print("--- FunPay Monitor Bot ---")
    print(f"Скинов активно: {len(enabled_skins)}/{len(config.get_all_skins())}")
    print(f"Подтв. PVE-слов: {len(config.get_confirmed_pve())}")
    print(f"Неподтв. PVE (по /recheck ++pve): {', '.join(config.get_unconfirmed_pve())}")
    print(f"Исключаю: {len(config.get_exclude_keywords())} фраз-красных флагов")
    skins = config.get_all_skins()
    enabled = {sid: s for sid, s in skins.items() if s.get('enabled', True)}
    max_skin_price = max((s.get('price', 0) for s in enabled.values()), default=0) + config.pve_bonus
    print(f"Макс. цена (авто): {max_skin_price}₽ (макс. скин + PVE бонус)")
    print(f"Уже просмотрено: {len(seen_ids)} товаров")
    print("Запустите бота и напишите ему /start в Telegram.")
    print("Управление — через меню кнопок в Telegram.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("stop", stop_command))

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

    # Регистрируем панель настроек
    register_settings_handlers(application, config, chat_id, seen_ids, banned_ids)

    job_queue = application.job_queue
    job_queue.run_repeating(check_funpay_job, interval=config.check_interval, first=10)

    import signal as _signal
    _signal.signal(_signal.SIGINT, lambda s, f: os._exit(0))
    application.run_polling(drop_pending_updates=True, stop_signals=None)

if __name__ == "__main__":
    main()
