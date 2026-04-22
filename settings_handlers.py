"""
settings_handlers.py — Интерактивное меню настроек бота через inline-кнопки в Telegram.
Позволяет управлять скинами, PVE-словами, ценами и фильтрами прямо из чата.
"""
import logging
import math
import asyncio
import time
import html
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
from price_history import get_price_history, record_price_snapshot, get_price_summary, get_item_offers_unique

logger = logging.getLogger(__name__)

# Состояния для ConversationHandler (ввод данных)
INPUT_SKIN_PRICE, INPUT_SKIN_KEYWORDS, INPUT_NEW_SKIN_ID, INPUT_NEW_SKIN_PRICE, INPUT_NEW_SKIN_KEYWORDS = range(5)
INPUT_PVE_KEYWORD, INPUT_EXCLUDE_KEYWORD = range(5, 7)
INPUT_MAX_PRICE, INPUT_PVE_BONUS, INPUT_CHECK_INTERVAL, INPUT_DELAY_MIN, INPUT_DELAY_MAX = range(7, 12)
INPUT_RECHECK_RARE, INPUT_RECHECK_PVE = range(12, 14)
INPUT_RECHECK_SKIN_PRICE = 14
INPUT_RECHECK_ED_PRICE = 15
INPUT_CONFIRMED_PVE_PRICE = 16
INPUT_BANNED_LINK = 17

ITEMS_PER_PAGE = 8  # Элементов на странице (для пагинации)


def _get_chat_id_from_context(context):
    """Получает chat_id бота (для проверки авторизации)."""
    return context.bot_data.get('authorized_chat_id')


def _check_auth(update, context):
    """Проверяет авторизацию пользователя."""
    authorized = _get_chat_id_from_context(context)
    if authorized and str(update.effective_chat.id) != str(authorized):
        return False
    return True


def _set_input_return(context, callback_data=None, label=None, extra_buttons=None):
    if callback_data and label:
        context.user_data['input_return_callback'] = callback_data
        context.user_data['input_return_label'] = label
        if extra_buttons:
            context.user_data['input_return_extra_buttons'] = extra_buttons
        else:
            context.user_data.pop('input_return_extra_buttons', None)
    else:
        context.user_data.pop('input_return_callback', None)
        context.user_data.pop('input_return_label', None)
        context.user_data.pop('input_return_extra_buttons', None)


def _pop_input_return_markup(context, fallback_callback="set:main", fallback_label="🔙 В меню"):
    callback_data = context.user_data.pop('input_return_callback', None)
    label = context.user_data.pop('input_return_label', None)
    extra_buttons = context.user_data.pop('input_return_extra_buttons', None)
    if not callback_data or not label:
        callback_data = fallback_callback
        label = fallback_label

    keyboard = [[InlineKeyboardButton(label, callback_data=callback_data)]]
    if extra_buttons:
        for extra_label, extra_callback in extra_buttons:
            keyboard.append([InlineKeyboardButton(extra_label, callback_data=extra_callback)])
    if callback_data != "set:main":
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    return InlineKeyboardMarkup(keyboard)


def _get_input_return_markup(context, fallback_callback="set:main", fallback_label="🔙 Назад"):
    callback_data = context.user_data.get('input_return_callback') or fallback_callback
    label = context.user_data.get('input_return_label') or fallback_label
    extra_buttons = context.user_data.get('input_return_extra_buttons') or []
    keyboard = [[InlineKeyboardButton(label, callback_data=callback_data)]]
    for extra_label, extra_callback in extra_buttons:
        keyboard.append([InlineKeyboardButton(extra_label, callback_data=extra_callback)])
    if callback_data != "set:main":
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    return InlineKeyboardMarkup(keyboard)


def _get_banned_ids(context):
    return context.bot_data.setdefault('banned_ids', set())


def _save_banned_ids(context):
    banned_ids = _get_banned_ids(context)
    with open('banned_ids.txt', 'w', encoding='utf-8') as f:
        for offer_id in sorted(banned_ids):
            f.write(f"{offer_id}\n")


def _extract_offer_id(value):
    if not value:
        return None
    match = re.search(r'id=(\d+)', str(value))
    if match:
        return match.group(1)
    raw = str(value).strip()
    if raw.isdigit():
        return raw
    return None


def _offer_url(offer_id):
    return f"https://funpay.com/lots/offer?id={offer_id}"


def _make_progress_bar(done, total, width=12):
    total = max(total, 1)
    done = max(0, min(done, total))
    pct = int(done / total * 100)
    filled = min(width, int(round(done / total * width)))
    return ('▰' * filled) + ('▱' * (width - filled)), pct

def _skin_emoji(skin_id):
    sid = skin_id.lower()
    mapping = {
        'black_knight': '🛡️',
        'cobalt_snowfoot': '❄️',
        'dark_skully': '💀',
        'dark_vertex': '🌌',
        'double_helix': '🧬',
        'eon': '⚡',
        'florin': '🪙',
        'floss': '💃',
        'freediver': '🤿',
        'huntmaster_saber': '🐯',
        'neo_versa': '🚀',
        'rogue_spider_knight': '🕷️',
        'royale_bomber': '✈️',
        'sparkle_specialist': '🎇',
        'stealth_reflex': '🥷',
        'surf_strider': '🏄',
        'thrilldiver': '🌊',
        'twitch_prime': '👑',
        'wildcat': '🐆',
    }
    return mapping.get(sid, '🎮')


def _confirmed_pve_title():
    return "🛡 Подтв. PVE"


def _check_control_markup(include_home=True):
    keyboard = []
    if include_home:
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    keyboard.append([InlineKeyboardButton("⏹ Остановить текущую проверку", callback_data="set:checkstop")])
    return InlineKeyboardMarkup(keyboard)


def _build_settings_main_text(config):
    skins = config.get_all_skins()
    editions = config.get_all_editions()
    enabled = sum(1 for s in skins.values() if s.get('enabled', True))
    pve_req = sum(1 for s in skins.values() if s.get('require_pve', False))
    return (
        "⚙️ <b>Панель настроек</b>\n\n"
        f"🎮 Скинов: {enabled}/{len(skins)} | 🏆 Изданий: {len(editions)}\n"
        f"🔒 С PVE: {pve_req}/{len(skins)}\n"
        f"🚫 Фильтров: {len(config.get_exclude_keywords())}\n\n"
        "Выберите раздел:"
    )


def _build_settings_main_markup(context):
    keyboard = [
        [InlineKeyboardButton("📋 Список", callback_data="set:skins:menu"),
         InlineKeyboardButton("🚫 Фильтры", callback_data="set:filters:menu")],
        [InlineKeyboardButton("🏷️ Цены и таймеры", callback_data="set:prices:menu"),
         InlineKeyboardButton("🔎 Проверка", callback_data="set:check:menu")],
        [InlineKeyboardButton("📊 Статус", callback_data="set:status"),
         InlineKeyboardButton("📈 Статистика", callback_data="set:stats")],
    ]
    if _is_check_running(context):
        keyboard.append([InlineKeyboardButton("⏹ Остановить текущую проверку", callback_data="set:checkstop")])
    keyboard.append([
        InlineKeyboardButton("☁️ Синхронизация", callback_data="set:sync"),
        InlineKeyboardButton("⏹ Остановить бота", callback_data="set:stop"),
    ])
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="set:close")])
    return InlineKeyboardMarkup(keyboard)


def _history_item_meta(context, item_type, item_id):
    config = context.bot_data['config']
    if item_type == 'skin':
        return config.get_skin(item_id) or {}, f"{_skin_emoji(item_id)} {item_id.replace('_', ' ').title()}"
    if item_type == 'edition':
        return config.get_edition(item_id) or {}, f"🏆 {item_id.replace('_', ' ').title()}"
    if item_type == 'pve':
        if item_id == 'unconfirmed':
            return {}, "🔓 Неподтв. PVE"
        return {'price': config.confirmed_pve_price}, _confirmed_pve_title()
    return {}, item_id


def _history_back_callback(back_token, item_id=None, mode='all'):
    mapping = {
        'ms': "set:check:minprice:skins",
        'mp': "set:check:minprice:pve",
        'sd': f"set:skins:detail:{item_id}:0" if item_id else "set:skins:menu",
        'pd': "set:skins:pvdetail:0",
        'ek': f"set:skins:edkw:{item_id}" if item_id else "set:skins:pvelist:0",
        'main': "set:main",
    }
    return mapping.get(back_token, "set:main")


def _ensure_minprice_selection(context):
    config = context.bot_data['config']
    bundle = config.get_minprice_bundle()
    if 'mp_custom_skins' not in context.user_data:
        context.user_data['mp_custom_skins'] = set(bundle.get('skins', []))
    if 'mp_custom_editions' not in context.user_data:
        context.user_data['mp_custom_editions'] = set(bundle.get('editions', []))
    if 'mp_custom_confirmed_pve' not in context.user_data:
        context.user_data['mp_custom_confirmed_pve'] = bool(bundle.get('confirmed_pve', True))
    if 'mp_custom_unconfirmed_pve' not in context.user_data:
        context.user_data['mp_custom_unconfirmed_pve'] = bool(bundle.get('unconfirmed_pve', False))


def _save_minprice_selection(context):
    config = context.bot_data['config']
    config.set_minprice_bundle(
        skins=context.user_data.get('mp_custom_skins', set()),
        editions=context.user_data.get('mp_custom_editions', set()),
        confirmed_pve=context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled),
        unconfirmed_pve=context.user_data.get('mp_custom_unconfirmed_pve', False),
    )


async def _run_minprice_search_with_watchdog(context, label, search_coro_factory, heartbeat_callback=None,
                                             timeout_seconds=240, heartbeat_seconds=5):
    started = time.monotonic()
    task = asyncio.create_task(search_coro_factory())
    while True:
        if context.bot_data.get('cancel_current_check'):
            task.cancel()
            return []
        try:
            results = await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_seconds)
            return results or []
        except asyncio.TimeoutError:
            elapsed = int(time.monotonic() - started)
            if elapsed >= timeout_seconds:
                logger.warning(f"⏱️ Мин. прайс: тайм-аут шага '{label}' после {elapsed}с")
                task.cancel()
                return []
            if heartbeat_callback:
                try:
                    await heartbeat_callback(elapsed)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Ошибка мин. прайса для '{label}': {e}")
            return []


async def _show_price_history(query, context, item_type, item_id, mode='all', page=0, back_token='main'):
    _, title = _history_item_meta(context, item_type, item_id)
    history = get_price_history(item_type, item_id, None if mode == 'all' else mode, limit=30)
    per_page = 5
    total_pages = max(1, math.ceil(len(history) / per_page))
    page = max(0, min(page, total_pages - 1))
    page_items = history[page * per_page:(page + 1) * per_page]
    mode_label = {
        'all': 'все варианты',
        'any': 'без фильтра PVE',
        'pve': 'только с PVE',
        'confirmed': 'подтверждённый PVE',
        'unconfirmed': 'неподтверждённый PVE',
    }.get(mode, mode)

    text = f"📈 <b>История цен</b> ({page + 1}/{total_pages})\n\n{title}\n🔎 Режим: {mode_label}\n\n"
    if not page_items:
        text += "История пока пустая.\nСначала запустите мин. цену для этой позиции."
    else:
        for snapshot in page_items:
            date_label = html.escape(snapshot['recorded_at'].split(' ', 1)[0])
            if snapshot.get('offers'):
                top_offer = snapshot['offers'][0]
                price_markup = _format_log_offer_v2(top_offer)
                text += f"🕒 <b>{date_label}</b> — {price_markup}\n"
            else:
                text += f"🕒 <b>{date_label}</b> — —\n"
            text += "━━━━━━━━━━━━━━\n\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"set:hist:{item_type}:{item_id}:{mode}:{page - 1}:{back_token}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"set:hist:{item_type}:{item_id}:{mode}:{page + 1}:{back_token}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"set:hist:{item_type}:{item_id}:{mode}:{page}:{back_token}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=_history_back_callback(back_token, item_id=item_id, mode=mode))])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    await query.edit_message_text(text.strip(), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML', disable_web_page_preview=True)


async def _show_skins_menu(query, context):
    text = (
        "📋 <b>Список</b>\n\n"
        "Выберите раздел:"
    )
    keyboard = [
        [InlineKeyboardButton("🎮 Скины", callback_data="set:skins:list:0")],
        [InlineKeyboardButton("🔒 PVE", callback_data="set:skins:pvelist:0")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="set:main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def _main_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔎 Проверка")],
            [KeyboardButton("⏹ Стоп")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /settings — открывает главное меню настроек."""
    if not _check_auth(update, context):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    config = context.bot_data['config']
    text = _build_settings_main_text(config)
    await update.message.reply_text(text, reply_markup=_build_settings_main_markup(context), parse_mode='HTML')


async def _show_main_menu(query, context):
    """Показывает главное меню через edit_message."""
    config = context.bot_data['config']
    text = _build_settings_main_text(config)
    await query.edit_message_text(text, reply_markup=_build_settings_main_markup(context), parse_mode='HTML')


async def _show_banned_list(query, context, page=0):
    """Список забаненных лотов с пагинацией."""
    banned_ids = sorted(_get_banned_ids(context))
    total = len(banned_ids)
    per_page = 6
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    page_items = banned_ids[start:end]

    lines = [f"🚷 <b>Забаненные лоты</b> ({page + 1}/{total_pages})", ""]
    if page_items:
        lines.append(f"Всего: {total}")
        lines.append("Нажмите на ❌, чтобы убрать лот из бана:")
        lines.append("")
        for offer_id in page_items:
            lines.append(f"• <a href='{_offer_url(offer_id)}'>Лот {offer_id}</a>")
    else:
        lines.append("Список пуст.")

    keyboard = []
    for offer_id in page_items:
        keyboard.append([InlineKeyboardButton(f"❌ Убрать {offer_id}", callback_data=f"set:ban:rm:{offer_id}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"set:ban:list:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"set:ban:list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("➕ Добавить ссылкой", callback_data="set:ban:add")])
    if total:
        keyboard.append([InlineKeyboardButton("🧹 Очистить список", callback_data="set:ban:clear")])
    keyboard.append([InlineKeyboardButton("🔙 Фильтры", callback_data="set:filters:menu")])

    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# СКИНЫ — список с пагинацией
# ==============================

async def _show_skins_list(query, context, page=0):
    """Показывает список скинов с пагинацией."""
    config = context.bot_data['config']
    context.user_data['skins_section'] = 'skins'
    context.user_data['skins_last_page'] = page
    skin_ids = config.get_skin_ids_sorted()
    all_ids = skin_ids
    total = len(all_ids)
    total_pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, total)
    page_items = all_ids[start:end]

    def _short_name(name, limit=28):
        if len(name) <= limit:
            return name
        return name[:limit - 1] + "…"

    text = f"📋 <b>Скины</b> ({page + 1}/{total_pages})\n\n"
    text += "Название открывает карточку. Цена меняется отдельно.\n"

    keyboard = []
    for item_id in page_items:
        if item_id == 'pve:confirmed':
            enabled = config.confirmed_pve_enabled
            icon = "✅" if enabled else "⛔"
            keyboard.append([
                InlineKeyboardButton(_confirmed_pve_title(), callback_data=f"set:skins:pvdetail:{page}"),
            ])
            keyboard.append([
                InlineKeyboardButton(f"💰 {config.confirmed_pve_price}₽", callback_data=f"set:skins:pvprice:{page}"),
                InlineKeyboardButton(icon, callback_data=f"set:skins:pvtoggle:{page}"),
            ])
        else:
            sid = item_id
            skin = config.get_skin(sid)
            icon = "✅" if skin.get('enabled', True) else "⛔"
            pve_icon = "🔒 PVE" if skin.get('require_pve', False) else "🪄 Без"
            name = sid.replace('_', ' ').title()
            price = skin.get('price', 0)
            keyboard.append([
                InlineKeyboardButton(f"{_skin_emoji(sid)} {_short_name(name, 28)}", callback_data=f"set:skins:detail:{sid}:{page}"),
            ])
            keyboard.append([
                InlineKeyboardButton(f"💰 {price}₽", callback_data=f"set:skins:price:{sid}:list:{page}"),
                InlineKeyboardButton(icon, callback_data=f"set:skins:toggle:{sid}:{page}"),
                InlineKeyboardButton(pve_icon, callback_data=f"set:skins:pvereq:{sid}:{page}"),
                InlineKeyboardButton("🔎 Мин", callback_data=f"set:minprice:skin:{sid}:skinslist:{page}"),
            ])

    all_skins = config.get_all_skins()
    all_enabled = bool(all_skins) and all(s.get('enabled', True) for s in all_skins.values())
    all_pve = bool(all_skins) and all(s.get('require_pve', False) for s in all_skins.values())
    skins_all_btn = "⛔ Все выкл" if all_enabled else "✅ Все вкл"
    pve_all_btn = "🪄 PVE выкл" if all_pve else "🔒 PVE всем"
    keyboard.append([
        InlineKeyboardButton(skins_all_btn, callback_data=f"set:skins:alltoggle:{page}"),
        InlineKeyboardButton(pve_all_btn, callback_data=f"set:skins:pveall:{page}")
    ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("<", callback_data=f"set:skins:list:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(">", callback_data=f"set:skins:list:{page + 1}"))
    keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("➕ Добавить скин", callback_data="set:skins:add")])
    keyboard.append([InlineKeyboardButton("🔙 К разделам", callback_data="set:skins:menu")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_pve_positions_list(query, context, page=0):
    """Показывает PVE-позиции списка: подтверждённое PVE и издания."""
    config = context.bot_data['config']
    context.user_data['skins_section'] = 'pve'
    context.user_data['skins_last_page'] = page
    editions = config.get_all_editions()
    edition_order = ['super_deluxe', 'limited', 'ultimate']
    all_ids = ['pve:confirmed'] + [f'ed:{eid}' for eid in edition_order if eid in editions]
    total = len(all_ids)
    total_pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, total)
    page_items = all_ids[start:end]

    def _short_name(name, limit=28):
        return name if len(name) <= limit else name[:limit - 1] + "…"

    text = f"🔒 <b>PVE</b> ({page + 1}/{total_pages})\n\n"
    text += "Здесь только PVE-позиции списка.\n"

    keyboard = []
    for item_id in page_items:
        if item_id == 'pve:confirmed':
            enabled = config.confirmed_pve_enabled
            icon = "✅" if enabled else "⛔"
            keyboard.append([InlineKeyboardButton(_confirmed_pve_title(), callback_data=f"set:skins:pvdetail:{page}")])
            keyboard.append([
                InlineKeyboardButton(f"💰 {config.confirmed_pve_price}₽", callback_data=f"set:skins:pvprice:{page}"),
                InlineKeyboardButton(icon, callback_data=f"set:skins:pvtoggle:{page}"),
                InlineKeyboardButton("🔎 Мин", callback_data=f"set:minprice:pveconfirmed:pvlist:{page}"),
            ])
        else:
            eid = item_id[3:]
            ed = editions.get(eid, {})
            icon = "✅" if ed.get('enabled', True) else "⛔"
            name = eid.replace('_', ' ').title()
            keyboard.append([InlineKeyboardButton(f"🏆 {_short_name(name, 28)}", callback_data=f"set:skins:edkw:{eid}")])
            keyboard.append([
                InlineKeyboardButton(f"💰 {ed.get('price', 0)}₽", callback_data=f"set:skins:edprice:{eid}:{page}"),
                InlineKeyboardButton(icon, callback_data=f"set:skins:edtoggle:{eid}:{page}"),
                InlineKeyboardButton("🔎 Мин", callback_data=f"set:minprice:ed:{eid}:pvlist:{page}"),
            ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("<", callback_data=f"set:skins:pvelist:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(">", callback_data=f"set:skins:pvelist:{page + 1}"))
    keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("🔙 К разделам", callback_data="set:skins:menu")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_skin_detail(query, context, skin_id, page=0):
    """Показывает карточку скина: статус, цену и ключевые слова."""
    config = context.bot_data['config']
    skin = config.get_skin(skin_id)
    if not skin:
        await query.answer("Скин не найден")
        return
    context.user_data['skins_last_page'] = page

    icon = "✅ включен" if skin.get('enabled', True) else "⛔ выключен"
    pve_icon = "✅ только с PVE" if skin.get('require_pve', False) else "❌ PVE не обязателен"
    name = skin_id.replace('_', ' ').title()
    keywords = ', '.join(skin.get('keywords', []))

    text = (
        f"{_skin_emoji(skin_id)} <b>{name}</b>\n\n"
        f"Статус: {icon}\n"
        f"💰 Цена: {skin.get('price', 0)}₽\n"
        f"PVE: {pve_icon}\n"
        f"✏️ Ключевые слова:\n<i>{keywords}</i>"
    )

    keyboard = [
        [InlineKeyboardButton("✅/⛔ Вкл/выкл", callback_data=f"set:skins:toggle:{skin_id}:d"),
         InlineKeyboardButton("💰 Цена", callback_data=f"set:skins:price:{skin_id}:detail:{page}")],
        [InlineKeyboardButton(pve_icon, callback_data=f"set:skins:pvereq:{skin_id}:d")],
        [InlineKeyboardButton("📈 История цен", callback_data=f"set:hist:skin:{skin_id}:all:0:sd")],
        [InlineKeyboardButton("✏️ Ключ. слова", callback_data=f"set:skins:kw:{skin_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"set:skins:del:{skin_id}")],
        [InlineKeyboardButton("🔙 К списку", callback_data=f"set:skins:list:{page}")],
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
async def _show_skin_keywords(query, context, skin_id):
    """Показывает список ключевых слов скина."""
    config = context.bot_data['config']
    skin = config.get_skin(skin_id)
    if not skin:
        await query.answer("Скин не найден")
        return

    name = skin_id.replace('_', ' ').title()
    keywords = skin.get('keywords', [])

    text = (
        f"📝 <b>Ключевые слова: {name}</b>\n\n"
        f"Всего: {len(keywords)}\n"
        f"Нажмите ❌, чтобы удалить слово:\n"
    )

    keyboard = []
    for i, kw in enumerate(keywords):
        row = [InlineKeyboardButton(f"{i+1}. {kw}", callback_data="set:noop")]
        if len(keywords) > 1:
            row.append(InlineKeyboardButton("❌", callback_data=f"set:skins:kwdel:{skin_id}:{i}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("➕ Добавить слово", callback_data=f"set:skins:kwadd:{skin_id}")])
    keyboard.append([InlineKeyboardButton("🔙 К списку", callback_data="set:skins:list:0")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# PVE МЕНЮ
# ==============================

async def _show_pve_menu(query, context):
    """Меню PVE-настроек."""
    config = context.bot_data['config']

    text = (
        "🎯 <b>PVE / Save the World</b>\n\n"
        f"✅ Подтверждённых слов: {len(config.get_confirmed_pve())}\n"
        f"❓ Неподтверждённых слов: {len(config.get_unconfirmed_pve())}\n"
        f"⛔ Слов-исключений: {len(config.get_new_pve())}\n"
        f"💰 PVE бонус: {config.pve_bonus}₽\n\n"
        "Подтверждённые ищутся всегда.\n"
        "Неподтверждённые используются только в /recheck ++pve."
    )

    keyboard = [
        [InlineKeyboardButton(f"✅ Подтв. слова ({len(config.get_confirmed_pve())})",
                              callback_data="set:pve:conf:0")],
        [InlineKeyboardButton(f"❓ Неподтв. слова ({len(config.get_unconfirmed_pve())})",
                              callback_data="set:pve:unconf:0")],
        [InlineKeyboardButton(f"💰 PVE бонус: {config.pve_bonus}₽",
                              callback_data="set:pve:bonus")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_pve_keywords(query, context, pve_type, page=0):
    """Список PVE-слов с пагинацией."""
    config = context.bot_data['config']

    if pve_type == 'conf':
        keywords = config.get_confirmed_pve()
        title = "✅ Подтверждённые PVE-слова"
    else:
        keywords = config.get_unconfirmed_pve()
        title = "❓ Неподтверждённые PVE-слова"

    total = len(keywords)
    per_page = 8
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)

    text = f"🎯 <b>{title}</b> ({page + 1}/{total_pages})\n\n"
    text += "Нажмите, чтобы удалить:\n"

    keyboard = []
    for rel_idx, kw in enumerate(keywords[start:end]):
        keyboard.append([InlineKeyboardButton(
            f"🗑️ {kw}", callback_data=f"set:pve:rm:{pve_type}:{start + rel_idx}:{page}"
        )])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"set:pve:{pve_type}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"set:pve:{pve_type}:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("➕ Добавить слово", callback_data=f"set:pve:add:{pve_type}")])
    keyboard.append([InlineKeyboardButton("🔙 Фильтры", callback_data="set:filters:menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# ЦЕНЫ И ТАЙМЕРЫ
# ==============================

async def _show_prices_menu(query, context):
    """Меню цен и таймеров."""
    config = context.bot_data['config']

    text = (
        "💰 <b>Цены и таймеры</b>\n\n"
        "Нажмите, чтобы изменить:"
    )

    keyboard = [
        [InlineKeyboardButton(f"⏱ Интервал: {config.check_interval} сек", callback_data="set:num:interval")],
        [InlineKeyboardButton(
            f"⏳ Задержка: {config.request_delay_min}-{config.request_delay_max} сек",
            callback_data="set:num:delay"
        )],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# ФИЛЬТРЫ ИСКЛЮЧЕНИЙ
# ==============================

async def _show_filters_list(query, context, page=0):
    """Список фраз-исключений с пагинацией."""
    config = context.bot_data['config']
    keywords = config.get_exclude_keywords()

    total = len(keywords)
    per_page = 6
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)

    text = (
        f"🚫 <b>Фильтры-исключения</b> ({page + 1}/{total_pages})\n\n"
        f"Всего: {total} фраз\n"
        "Нажмите, чтобы удалить:"
    )

    keyboard = []
    for i, kw in enumerate(keywords[start:end]):
        display = kw[:35] + "..." if len(kw) > 35 else kw
        keyboard.append([InlineKeyboardButton(
            f"🗑️ {display}", callback_data=f"set:filt:rm:{start + i}:{page}"
        )])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"set:filters:list:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"set:filters:list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("➕ Добавить фразу", callback_data="set:filt:add")])
    keyboard.append([InlineKeyboardButton("🔄 Сбросить к дефолтным", callback_data="set:filt:reset")])
    keyboard.append([InlineKeyboardButton("🔙 Фильтры", callback_data="set:filters:menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# СТАТУС
# ==============================

async def _show_status(query, context):
    """Показывает текущий статус бота."""
    config = context.bot_data['config']
    skins = config.get_all_skins()
    enabled = sum(1 for s in skins.values() if s.get('enabled', True))
    seen_count = len(context.bot_data.get('seen_ids', set()))
    banned_count = len(context.bot_data.get('banned_ids', set()))
    mode_info = context.bot_data.get('bot_mode', {}) or {}
    progress = context.bot_data.get('current_check_progress') or {}
    state = _resolve_status_state(config, mode_info, progress)

    running_now = 'Автомониторинг' if state['mode'] == 'standard' else state['running_label']
    text = (
        "📊 <b>Статус бота</b>\n\n"
        f"🔍 Автомониторинг: {state['auto_mode_label']}\n"
        f"⚙️ Запущен сейчас: {running_now}\n"
    )

    if state['mode'] != 'standard':
        if state['target_label']:
            text += f"🧩 Цель: {state['target_label']}\n"
        if progress:
            bar, pct = _make_progress_bar(state['done'], state['total'])
            text += (
                f"📍 Этап: {state['stage']}\n"
                f"{bar} {pct}%\n"
                f"📦 Прогресс: {state['done']}/{state['total']}\n"
                f"✅ Отправлено: {state['sent']}\n"
            )
        text += "\n"

    text += (
        f"🎮 Скинов активно: {enabled}/{len(skins)}\n"
        f"🎯 PVE-слов (подтв.): {len(config.get_confirmed_pve())}\n"
        f"❓ PVE-слов (неподтв.): {len(config.get_unconfirmed_pve())}\n"
        f"🚫 Фильтров: {len(config.get_exclude_keywords())}\n"
        f"💰 Макс. цена: {config.max_price}₽\n"
        f"🎯 PVE бонус: {config.pve_bonus}₽\n"
        f"⏱ Интервал: {config.check_interval} сек\n"
        f"⏳ Задержка: {config.request_delay_min}-{config.request_delay_max} сек\n\n"
        f"👁 Просмотрено: {seen_count} товаров\n"
        f"🚷 В бан-листе: {banned_count} лотов\n"
    )

    keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def _price_link(price_txt, href):
    """Format price as hyperlink if href available."""
    safe = html.escape(price_txt or '—')
    if href:
        return f"<a href='{html.escape(href, quote=True)}'>{safe}</a>"
    return safe


async def _show_stats(query, context):
    """Показывает статистику: кол-во просмотренных, отправленных, цены из БД."""
    seen_count = len(context.bot_data.get('seen_ids', set()))
    sent_count = len(context.bot_data.get('sent_offers', {}))
    banned_count = len(context.bot_data.get('banned_ids', set()))

    summary = get_price_summary()
    text = "📈 <b>Статистика</b>\n\n"
    text += f"👁 Просмотрено: <b>{seen_count}</b> товаров\n"
    text += f"📩 Отправлено: <b>{sent_count}</b> предложений\n"
    text += f"🚷 В бан-листе: <b>{banned_count}</b>\n"
    text += f"📸 Снимков цен: <b>{summary['total_snapshots']}</b>\n"

    if summary['first_date'] and summary['last_date']:
        text += f"📅 Период: {summary['first_date']} — {summary['last_date']}\n"

    prices = summary.get('latest_prices', [])
    if prices:
        text += "\n💰 <b>Последние мин. цены:</b>\n"
        for row in prices:
            name = html.escape(row.get('item_name') or row.get('item_id', '?'))
            mode = row.get('mode', '')
            price_txt = row.get('price_text') or '—'
            href = row.get('href')
            date = row.get('recorded_at', '')
            mode_icon = '🔒' if 'pve' in mode.lower() else '🪄' if 'any' in mode.lower() else '📦'
            price_display = _price_link(price_txt, href)
            src = row.get('source', 'minprice')
            src_label = '🔍мин.прайс' if src == 'minprice' else '📡авто'
            text += f"  {mode_icon} {name} ({mode}): <b>{price_display}</b> <i>({src_label} {date})</i>\n"

    keyboard = [
        [InlineKeyboardButton("📜 История цен", callback_data="set:stats:items")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML', disable_web_page_preview=True)


async def _show_stats_items(query, context):
    """Показывает кнопки по каждому скину/PVE для истории цен."""
    summary = get_price_summary()
    prices = summary.get('latest_prices', [])

    item_buttons = {}
    for row in prices:
        key = f"{row.get('item_type', 'skin')}:{row.get('item_id', '')}"
        if key not in item_buttons:
            item_buttons[key] = row.get('item_name') or row.get('item_id', '?')

    text = "📜 <b>История цен</b>\n\nВыберите скин или PVE для просмотра уникальных предложений за последнее время:"

    keyboard = []
    row_buf = []
    for key, name in item_buttons.items():
        item_type, item_id = key.split(':', 1)
        cb = f"set:stats:hist:{item_type}:{item_id}"
        row_buf.append(InlineKeyboardButton(f"📜 {name}", callback_data=cb))
        if len(row_buf) == 2:
            keyboard.append(row_buf)
            row_buf = []
    if row_buf:
        keyboard.append(row_buf)

    if not item_buttons:
        text += "\n\nНет данных. Сначала запустите мин. прайс тест."

    keyboard.append([InlineKeyboardButton("🔙 Статистика", callback_data="set:stats")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_stats_item_history(query, context, item_type, item_id):
    """Показывает уникальные предложения для конкретного скина/PVE."""
    offers = get_item_offers_unique(item_type, item_id, limit=80)
    item_name = offers[0].get('item_name', item_id) if offers else item_id

    any_offers = [o for o in offers if 'pve' not in (o.get('mode') or '').lower()]
    pve_offers = [o for o in offers if 'pve' in (o.get('mode') or '').lower()]

    text = f"📜 <b>{html.escape(item_name)}</b> — история\n"

    if any_offers:
        text += "\n🪄 <b>Без PVE:</b>\n"
        for i, o in enumerate(any_offers[:15], 1):
            price_display = _price_link(o.get('price_text') or '—', o.get('href'))
            seller = html.escape(o.get('seller') or '?')
            date = o.get('recorded_at', '')
            text += f"  {i}. {price_display} — {seller} <i>({date})</i>\n"
        if len(any_offers) > 15:
            text += f"  <i>...ещё {len(any_offers) - 15}</i>\n"

    if pve_offers:
        text += "\n🔒 <b>С PVE:</b>\n"
        for i, o in enumerate(pve_offers[:15], 1):
            price_display = _price_link(o.get('price_text') or '—', o.get('href'))
            seller = html.escape(o.get('seller') or '?')
            date = o.get('recorded_at', '')
            text += f"  {i}. {price_display} — {seller} <i>({date})</i>\n"
        if len(pve_offers) > 15:
            text += f"  <i>...ещё {len(pve_offers) - 15}</i>\n"

    if not any_offers and not pve_offers:
        text += "\nНет данных за последние проверки."

    keyboard = [
        [InlineKeyboardButton("🔙 К списку", callback_data="set:stats:items")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML', disable_web_page_preview=True)


async def _show_confirmed_pve_detail(query, context, page=0):
    config = context.bot_data['config']
    context.user_data['skins_last_page'] = page
    keywords = config.get_confirmed_pve()
    text = (
        f"{_confirmed_pve_title()}\n\n"
        f"Статус: {'✅ включен' if config.confirmed_pve_enabled else '⛔ выключен'}\n"
        f"💰 Цена: {config.confirmed_pve_price}₽\n"
        f"🧩 Слов подтверждения: {len(keywords)}\n\n"
        "Ищет аккаунты с подтверждённым PVE как отдельную позицию списка."
    )
    keyboard = [
        [InlineKeyboardButton("✅/⛔ Вкл/выкл", callback_data=f"set:skins:pvtoggle:d:{page}")],
        [InlineKeyboardButton("💰 Цена", callback_data=f"set:skins:pvprice:detail:{page}")],
        [InlineKeyboardButton("📈 История цен", callback_data="set:hist:pve:confirmed:confirmed:0:pd")],
        [InlineKeyboardButton("🔙 К PVE", callback_data=f"set:skins:pvelist:{page}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ==============================
# ПРОВЕРКА
# ==============================

def _get_check_overview(config):
    skins = config.get_all_skins()
    enabled = {sid: s for sid, s in skins.items() if s.get('enabled', True)}
    editions = config.get_all_editions()
    enabled_editions = sum(1 for ed in editions.values() if ed.get('enabled', True))
    pve_required = sum(1 for s in enabled.values() if s.get('require_pve', False))
    return {
        'skins_total': len(skins),
        'skins_enabled': len(enabled),
        'pve_required': pve_required,
        'editions_enabled': enabled_editions,
    }


async def _show_check_menu(query, context):
    config = context.bot_data['config']
    info = _get_check_overview(config)
    running = _is_check_running(context)
    progress = context.bot_data.get('current_check_progress') or {}
    if running and progress:
        bar, pct = _make_progress_bar(progress.get('done', 0), max(progress.get('total', 1), 1))
        status_text = (
            f"⚠️ Сейчас выполняется проверка:\n"
            f"📍 Этап: {progress.get('stage', 'Подготовка')}\n"
            f"{bar} {pct}%\n"
            f"📦 Прогресс: {progress.get('done', 0)}/{max(progress.get('total', 1), 1)}\n"
            f"🔎 Сейчас: {progress.get('current', '...')}\n"
            f"✅ Отправлено: {progress.get('sent', 0)}\n\n"
        )
    elif running:
        status_text = "⚠️ Сейчас уже выполняется проверка.\n\n"
    else:
        status_text = ""
    text = (
        "🔎 <b>Проверка</b>\n\n"
        f"{status_text}"
        f"🎮 Скинов по списку: {info['skins_enabled']}/{info['skins_total']}\n"
        f"🔒 С обяз. PVE: {info['pve_required']}\n"
        f"🏆 Изданий активно: {info['editions_enabled']}\n\n"
        "Выберите действие:"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Полная перепроверка", callback_data="set:check:full")],
        [InlineKeyboardButton("💰 Мин. цена", callback_data="set:check:minprice")],
    ]
    if running:
        keyboard.append([InlineKeyboardButton("⏹ Остановить текущую проверку", callback_data="set:checkstop")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_check_menu_as_new_message(update, context):
    if not _check_auth(update, context):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    config = context.bot_data['config']
    info = _get_check_overview(config)
    running = _is_check_running(context)
    progress = context.bot_data.get('current_check_progress') or {}
    if running and progress:
        bar, pct = _make_progress_bar(progress.get('done', 0), max(progress.get('total', 1), 1))
        status_text = (
            f"⚠️ Сейчас выполняется проверка:\n"
            f"📍 Этап: {progress.get('stage', 'Подготовка')}\n"
            f"{bar} {pct}%\n"
            f"📦 Прогресс: {progress.get('done', 0)}/{max(progress.get('total', 1), 1)}\n"
            f"🔎 Сейчас: {progress.get('current', '...')}\n"
            f"✅ Отправлено: {progress.get('sent', 0)}\n\n"
        )
    elif running:
        status_text = "⚠️ Сейчас уже выполняется проверка.\n\n"
    else:
        status_text = ""
    text = (
        "🔎 <b>Проверка</b>\n\n"
        f"{status_text}"
        f"🎮 Скинов по списку: {info['skins_enabled']}/{info['skins_total']}\n"
        f"🔒 С обяз. PVE: {info['pve_required']}\n"
        f"🏆 Изданий активно: {info['editions_enabled']}\n\n"
        "Выберите действие:"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Полная перепроверка", callback_data="set:check:full")],
        [InlineKeyboardButton("💰 Мин. цена", callback_data="set:check:minprice")],
    ]
    if running:
        keyboard.append([InlineKeyboardButton("⏹ Остановить текущую проверку", callback_data="set:checkstop")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_standard_check_menu(query, context):
    text = (
        "📋 <b>Стандартная проверка</b>\n\n"
        "Использует текущий список и сохранённые цены.\n\n"
        "Выберите раздел:"
    )
    keyboard = [
        [InlineKeyboardButton("🎮 Скины", callback_data="set:check:stdskins")],
        [InlineKeyboardButton("🔒 PVE", callback_data="set:check:stdpve")],
        [InlineKeyboardButton("🔙 Назад", callback_data="set:check:menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_standard_check_skins_menu(query, context):
    text = (
        "🎮 <b>Стандартная проверка: скины</b>\n\n"
        "Подходящие ищет лоты по текущему списку скинов.\n"
        "Минималки показывает рынок по скинам."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Подходящие", callback_data="set:check:stdskins:recheck")],
        [InlineKeyboardButton("💰 Минималки", callback_data="set:check:stdskins:minprice")],
        [InlineKeyboardButton("🔙 Назад", callback_data="set:check:standard")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_standard_check_pve_menu(query, context):
    text = (
        "🔒 <b>Стандартная проверка: PVE</b>\n\n"
        "Выберите отдельную PVE-позицию:"
    )
    keyboard = [
        [InlineKeyboardButton(_confirmed_pve_title(), callback_data="set:check:stdpve:confirmed")],
        [InlineKeyboardButton("🏆 Издания", callback_data="set:check:stdpve:editions")],
        [InlineKeyboardButton("🔓 Неподтв. PVE", callback_data="set:check:stdpve:unconfirmed")],
        [InlineKeyboardButton("🔙 Назад", callback_data="set:check:standard")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_standard_check_item_menu(query, context, item_key):
    title = {
        'confirmed': _confirmed_pve_title(),
        'editions': "🏆 Издания",
        'unconfirmed': "🔓 Неподтв. PVE",
    }.get(item_key, "🔎 Проверка")
    text = f"{title}\n\nВыберите действие:"
    keyboard = []
    if item_key != 'unconfirmed':
        keyboard.append([InlineKeyboardButton("✅ Подходящие", callback_data=f"set:check:stdpve:{item_key}:recheck")])
        keyboard.append([InlineKeyboardButton("💰 Минималки", callback_data=f"set:check:stdpve:{item_key}:minprice")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Подходящие", callback_data="set:check:stdpve:unconfirmed:recheck")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="set:check:stdpve")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')



async def _show_minprice_section_menu(query, context, section='all', back_cb='set:check:menu'):
    config = context.bot_data['config']
    _ensure_minprice_selection(context)
    context.user_data['mp_back_cb'] = back_cb
    context.user_data['mp_custom_view'] = section
    if section == 'skins':
        text = "💰 <b>Мин. цена: скины</b>"
    elif section == 'pve':
        text = "💰 <b>Мин. цена: PVE</b>"
    else:
        text = "💰 <b>Мін. прайс тест</b>\n\nВыберите позицию для поиска минимальной цены на FunPay:"

    keyboard = []
    if section in ('all', 'skins'):
        selected = context.user_data.get('mp_custom_skins', set())
        skin_ids = sorted(config.get_all_skins().keys())
        if section == 'skins':
            skin_ids = [sid for sid in skin_ids if config.get_skin(sid).get('enabled', True)]
        for i in range(0, len(skin_ids), 2):
            pair = skin_ids[i:i + 2]
            name_row = []
            select_row = []
            for sid in pair:
                name = sid.replace('_', ' ').title()
                sel_icon = "✅ В набор" if sid in selected else "⬜ В набор"
                name_row.append(InlineKeyboardButton(f"{_skin_emoji(sid)} {name}", callback_data=f"set:minprice:skin:{sid}"))
                select_row.append(InlineKeyboardButton(sel_icon, callback_data=f"set:minprice:csel:{sid}"))
            keyboard.append(name_row)
            keyboard.append(select_row)

    if section in ('all', 'pve'):
        selected_eds = context.user_data.get('mp_custom_editions', set())
        selected_confirmed_pve = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
        selected_unconfirmed_pve = context.user_data.get('mp_custom_unconfirmed_pve', False)
        keyboard.append([InlineKeyboardButton(_confirmed_pve_title(), callback_data="set:minprice:pveconfirmed")])
        keyboard.append([
            InlineKeyboardButton("✅ В набор" if selected_confirmed_pve else "⬜ В набор", callback_data="set:minprice:cpvepos")
        ])
        keyboard.append([InlineKeyboardButton("🔓 Неподтв. PVE", callback_data="set:minprice:pveunconfirmed")])
        keyboard.append([
            InlineKeyboardButton("✅ В набор" if selected_unconfirmed_pve else "⬜ В набор", callback_data="set:minprice:cpveunconfirmed")
        ])
        for eid in ['super_deluxe', 'limited', 'ultimate']:
            if config.get_edition(eid):
                keyboard.append([InlineKeyboardButton(f"🏆 {eid.replace('_', ' ').title()}", callback_data=f"set:minprice:ed:{eid}")])
                keyboard.append([
                    InlineKeyboardButton("✅ В набор" if eid in selected_eds else "⬜ В набор", callback_data=f"set:minprice:cedsel:{eid}")
                ])

    if section == 'all':
        keyboard.append([InlineKeyboardButton("🔎 Кастом поиск", callback_data="set:minprice:custom")])
    elif section == 'skins':
        all_skin_ids = [sid for sid in sorted(config.get_all_skins().keys()) if config.get_skin(sid).get('enabled', True)]
        selected = context.user_data.get('mp_custom_skins', set())
        all_selected = bool(all_skin_ids) and all(sid in selected for sid in all_skin_ids)
        keyboard.append([
            InlineKeyboardButton("⛔ Убрать все" if all_selected else "✅ Добавить все", callback_data="set:minprice:cskinsall"),
            InlineKeyboardButton("🔒 К PVE", callback_data="set:check:minprice:pve"),
        ])
        total_selected = (
            len(context.user_data.get('mp_custom_skins', set())) +
            len(context.user_data.get('mp_custom_editions', set())) +
            (1 if context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled) else 0) +
            (1 if context.user_data.get('mp_custom_unconfirmed_pve', False) else 0)
        )
        if total_selected:
            keyboard.append([InlineKeyboardButton(f"▶️ Искать набор ({total_selected})", callback_data="set:minprice:crun")])
    elif section == 'pve':
        selected_eds = context.user_data.get('mp_custom_editions', set())
        selected_confirmed_pve = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
        selected_unconfirmed_pve = context.user_data.get('mp_custom_unconfirmed_pve', False)
        edition_ids = [eid for eid in ['super_deluxe', 'limited', 'ultimate'] if config.get_edition(eid)]
        all_selected = selected_confirmed_pve and selected_unconfirmed_pve and all(eid in selected_eds for eid in edition_ids)
        total_selected = (
            len(context.user_data.get('mp_custom_skins', set())) +
            len(selected_eds) +
            (1 if selected_confirmed_pve else 0) +
            (1 if selected_unconfirmed_pve else 0)
        )
        keyboard.append([
            InlineKeyboardButton("⛔ Убрать все" if all_selected else "✅ Добавить все", callback_data="set:minprice:cpveallpos"),
            InlineKeyboardButton("🎮 К скинам", callback_data="set:check:minprice:skins"),
        ])
        if total_selected:
            keyboard.append([InlineKeyboardButton(f"▶️ Искать набор ({total_selected})", callback_data="set:minprice:crun")])

    keyboard.append([
        InlineKeyboardButton("🔙 Назад", callback_data=back_cb),
        InlineKeyboardButton("🏠 Главное меню", callback_data="set:main"),
    ])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def _seed_standard_skin_recheck(context, config):
    context.user_data['custom_recheck_mode'] = 'list'
    context.user_data['recheck_skins'] = {
        sid: {'enabled': skin.get('enabled', True), 'price': skin.get('price', 0)}
        for sid, skin in config.get_all_skins().items()
    }
    context.user_data['recheck_editions'] = {}
    context.user_data['recheck_confirmed_pve'] = {'enabled': False, 'price': config.confirmed_pve_price}


def _seed_standard_confirmed_pve_recheck(context, config):
    context.user_data['custom_recheck_mode'] = 'confirmed_pve'
    context.user_data['recheck_skins'] = {}
    context.user_data['recheck_editions'] = {}
    context.user_data['recheck_confirmed_pve'] = {'enabled': True, 'price': config.confirmed_pve_price}


def _seed_standard_editions_recheck(context, config):
    context.user_data['custom_recheck_mode'] = 'premium'
    context.user_data['recheck_skins'] = {}
    context.user_data['recheck_confirmed_pve'] = {'enabled': False, 'price': config.confirmed_pve_price}
    context.user_data['recheck_editions'] = {
        eid: {'enabled': ed.get('enabled', True), 'price': ed.get('price', 0)}
        for eid, ed in config.get_all_editions().items()
    }


async def _start_full_recheck(query, context, config):
    if _is_check_running(context):
        await _show_current_check_status(query, context)
        return

    bot_mode = context.bot_data.get('bot_mode', {})
    process_fn = context.bot_data.get('process_offers')
    build_snapshot_fn = context.bot_data.get('build_recheck_snapshot')
    chat_id = _get_chat_id_from_context(context)

    bot_mode['mode'] = 'recheck'
    bot_mode['params'] = {
        'display_mode': "Перепроверка: по списку",
        'target_label': "По списку",
        'restore_mode': config.search_mode,
        'run_snapshot': build_snapshot_fn(
            config,
            display_mode='По списку',
            bot_mode_key='recheck',
            search_mode='skins_pve',
            chat_id_value=chat_id,
        )
    }
    bot_mode['started_at'] = time.time()
    await query.edit_message_text(
        "📋 <b>Полная перепроверка запущена!</b>\n"
        "🎯 Режим: по текущему списку\n"
        "⚠️ Может занять несколько минут...",
        parse_mode='HTML',
        reply_markup=_check_control_markup()
    )
    asyncio.create_task(_run_recheck_task(
        chat_id, context, process_fn,
        skip_seen=False, candidate_limit=None
    ))

async def _show_recheck_menu(query, context):
    await _show_check_menu(query, context)



async def _show_minprice_menu(query, context):
    """Показывает меню мін. прайс теста."""
    config = context.bot_data['config']
    text = (
        "💰 <b>Мін. прайс тест</b>\n\n"
        "Выберите позицию для поиска минимальной цены на FunPay:"
    )

    keyboard = []
    skins = config.get_all_skins()
    skin_row = []
    for sid in sorted(skins.keys()):
        name = sid.replace('_', ' ').title()
        skin_row.append(InlineKeyboardButton(f"{_skin_emoji(sid)} {name}", callback_data=f"set:minprice:skinpick:{sid}"))
        if len(skin_row) == 2:
            keyboard.append(skin_row)
            skin_row = []
    if skin_row:
        keyboard.append(skin_row)

    keyboard.append([InlineKeyboardButton(_confirmed_pve_title(), callback_data="set:minprice:pveconfirmed")])

    for eid in ['super_deluxe', 'limited', 'ultimate']:
        if not config.get_edition(eid):
            continue
        name = eid.replace('_', ' ').title()
        keyboard.append([InlineKeyboardButton(f"🏆 {name}", callback_data=f"set:minprice:ed:{eid}")])

    keyboard.append([InlineKeyboardButton("🔎 Кастом поиск", callback_data="set:minprice:custom")])
    keyboard.append([
        InlineKeyboardButton("🔙 Назад", callback_data="set:recheck:menu"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="set:main"),
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_minprice_menu_as_new_message(update, context):
    """Показывает меню мін. прайс как новое сообщение."""
    if not _check_auth(update, context):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return

    config = context.bot_data['config']
    text = (
        "💰 <b>Мін. прайс тест</b>\n\n"
        "Выберите позицию для поиска минимальной цены на FunPay:"
    )

    keyboard = []
    skins = config.get_all_skins()
    skin_row = []
    for sid in sorted(skins.keys()):
        name = sid.replace('_', ' ').title()
        skin_row.append(InlineKeyboardButton(f"{_skin_emoji(sid)} {name}", callback_data=f"set:minprice:skinpick:{sid}"))
        if len(skin_row) == 2:
            keyboard.append(skin_row)
            skin_row = []
    if skin_row:
        keyboard.append(skin_row)

    keyboard.append([InlineKeyboardButton(_confirmed_pve_title(), callback_data="set:minprice:pveconfirmed")])

    for eid in ['super_deluxe', 'limited', 'ultimate']:
        if not config.get_edition(eid):
            continue
        name = eid.replace('_', ' ').title()
        keyboard.append([InlineKeyboardButton(f"🏆 {name}", callback_data=f"set:minprice:ed:{eid}")])

    keyboard.append([InlineKeyboardButton("🔎 Кастом поиск", callback_data="set:minprice:custom")])
    keyboard.append([
        InlineKeyboardButton("🔙 Назад", callback_data="set:recheck:menu"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="set:main"),
    ])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _show_minprice_skin_mode(query, context, skin_id):
    config = context.bot_data['config']
    skin = config.get_skin(skin_id)
    if not skin:
        await query.answer("Скин не найден", show_alert=True)
        return

    name = skin_id.replace('_', ' ').title()
    default_pve = skin.get('require_pve', False)
    note = "По умолчанию у этого скина включён фильтр PVE." if default_pve else "По умолчанию PVE для этого скина не обязателен."
    text = (
        f"{_skin_emoji(skin_id)} <b>{name}</b>\n\n"
        f"{note}\n\n"
        "Выберите режим поиска:"
    )
    keyboard = [
        [InlineKeyboardButton("🔒 Только с PVE", callback_data=f"set:minprice:skin:{skin_id}:pve")],
        [InlineKeyboardButton("🪄 Без фильтра PVE", callback_data=f"set:minprice:skin:{skin_id}:any")],
        [InlineKeyboardButton("🔙 Назад", callback_data="set:recheck:minprice"), InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def _show_minprice_custom(query, context):
    """Показывает меню кастомного мін. прайс поиска."""
    config = context.bot_data['config']
    view = context.user_data.get('mp_custom_view', 'all')
    selected = context.user_data.get('mp_custom_skins', set())
    pve_map = context.user_data.get('mp_custom_pve', {})

    skins = config.get_all_skins()
    editions = config.get_all_editions()

    # Инициализация изданий если нет
    if 'mp_custom_editions' not in context.user_data:
        context.user_data['mp_custom_editions'] = set(editions.keys())
    selected_eds = context.user_data.get('mp_custom_editions', set())
    selected_confirmed_pve = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
    context.user_data['mp_custom_confirmed_pve'] = selected_confirmed_pve

    # Инициализация PVE из конфига если нет
    for sid in skins:
        if sid not in pve_map:
            pve_map[sid] = skins[sid].get('require_pve', False)
    context.user_data['mp_custom_pve'] = pve_map

    if view == 'skins':
        selected_eds = set()
        selected_confirmed_pve = False
        pve_count = sum(1 for sid in selected if pve_map.get(sid, False))
        total_items = len(selected)
        text = (
            "💰 <b>Кастомные минималки: скины</b>\n\n"
            f"Выбрано скинов: {len(selected)}"
        )
        if selected:
            text += f" | С PVE: {pve_count}/{len(selected)}"
        text += "\n\nВыберите скины:"
        back_cb = "set:check:customskins"
    elif view == 'pve':
        selected = set()
        pve_count = 0
        total_items = len(selected_eds) + (1 if selected_confirmed_pve else 0)
        text = (
            "💰 <b>Кастомные минималки: PVE</b>\n\n"
            f"Выбрано: {len(selected_eds)} изд. + {'1 PVE' if selected_confirmed_pve else '0 PVE'}\n\n"
            "Выберите PVE-позиции:"
        )
        back_cb = "set:check:custompve"
    else:
        pve_count = sum(1 for sid in selected if pve_map.get(sid, False))
        total_items = len(selected) + len(selected_eds) + (1 if selected_confirmed_pve else 0)
        text = (
            "🔎 <b>Кастом мін. прайс</b>\n\n"
            f"Выбрано: {total_items} ({len(selected)} скинов + {len(selected_eds)} изд. + {'1 PVE' if selected_confirmed_pve else '0 PVE'})"
        )
        if selected:
            text += f" | С PVE: {pve_count}/{len(selected)}"
        text += "\n\nВыберите скины и PVE:"
        back_cb = "set:check:custom"

    keyboard = []
    if view in ('all', 'skins'):
        for sid in sorted(skins.keys()):
            name = sid.replace('_', ' ').title()
            if len(name) > 10:
                name = name[:9] + '.'
            sel_icon = "✅" if sid in selected else "⬜"
            pve_icon = "🔒 PVE" if pve_map.get(sid, False) else "🪄 Без"
            keyboard.append([
                InlineKeyboardButton(f"{sel_icon} {_skin_emoji(sid)} {name}", callback_data=f"set:minprice:csel:{sid}"),
                InlineKeyboardButton(pve_icon, callback_data=f"set:minprice:cpve:{sid}"),
            ])

    if view in ('all', 'pve'):
        keyboard.append([
            InlineKeyboardButton(
                f"{'✅' if selected_confirmed_pve else '⬜'} {_confirmed_pve_title()}",
                callback_data="set:minprice:cpvepos"
            ),
        ])

    if view in ('all', 'pve'):
        EDITION_ORDER = ['super_deluxe', 'limited', 'ultimate']
        for eid in EDITION_ORDER:
            if eid not in editions:
                continue
            name = eid.replace('_', ' ').title()
            sel_icon = "✅" if eid in selected_eds else "⬜"
            keyboard.append([
                InlineKeyboardButton(f"{sel_icon} 🏆 {name}", callback_data=f"set:minprice:cedsel:{eid}"),
            ])

    if view in ('all', 'skins'):
        all_skin_ids = set(skins.keys())
        all_skins_selected = selected == all_skin_ids and len(all_skin_ids) > 0
        keyboard.append([
            InlineKeyboardButton("🧹 Снять скины" if all_skins_selected else "🎮 Все скины", callback_data="set:minprice:cskinsall"),
            InlineKeyboardButton("🧹 Очистить всё", callback_data="set:minprice:csnone"),
        ])
        keyboard.append([
            InlineKeyboardButton("🔒 Все PVE", callback_data="set:minprice:cpveall"),
            InlineKeyboardButton("🪄 Без PVE", callback_data="set:minprice:cpvenone"),
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🧹 Очистить всё", callback_data="set:minprice:csnone"),
        ])

    # Запуск
    if total_items > 0:
        keyboard.append([InlineKeyboardButton(f"🔎 Искать ({total_items} позиций)", callback_data="set:minprice:crun")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=back_cb), InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def _launch_minprice_bundle_run(query, context, config):
    if _is_check_running(context):
        await _show_current_check_status(query, context)
        return
    _ensure_minprice_selection(context)
    view = context.user_data.get('mp_custom_view', 'all')
    selected = context.user_data.get('mp_custom_skins', set())
    selected_eds = context.user_data.get('mp_custom_editions', set())
    selected_confirmed_pve = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
    selected_unconfirmed_pve = context.user_data.get('mp_custom_unconfirmed_pve', False)
    bot_mode = context.bot_data.get('bot_mode', {})
    total_items = len(selected) + len(selected_eds) + (1 if selected_confirmed_pve else 0) + (1 if selected_unconfirmed_pve else 0)
    if total_items == 0:
        await query.answer("Выберите хотя бы одну позицию", show_alert=True)
        return

    chat_id = query.message.chat_id
    progress_message_id = query.message.message_id
    skins = config.get_all_skins()
    editions = config.get_all_editions()
    total_steps = (len(selected) * 2) + len(selected_eds) + (1 if selected_confirmed_pve else 0) + (1 if selected_unconfirmed_pve else 0)
    await query.edit_message_text(
        f"💰 <b>Кастомный мін. прайс</b>\n\n"
        f"🎮 Скинов: {len(selected)} | 🏆 Изданий: {len(selected_eds)} | 🛡 Подтв. PVE: {'да' if selected_confirmed_pve else 'нет'} | 🔓 Неподтв. PVE: {'да' if selected_unconfirmed_pve else 'нет'}\n\n"
        f"{_make_progress_bar(0, max(total_steps, 1))[0]} 0%\n"
        f"📦 Проверено: 0/{total_items}\n"
        f"🧭 Подготавливаю поиск...",
        parse_mode='HTML',
        reply_markup=_check_control_markup()
    )

    target_bits = []
    if selected:
        target_bits.append(f"скины: {len(selected)}")
    if selected_eds:
        target_bits.append(f"издания: {len(selected_eds)}")
    if selected_confirmed_pve:
        target_bits.append("подтв. PVE")
    if selected_unconfirmed_pve:
        target_bits.append("неподтв. PVE")
    bot_mode['mode'] = 'pricetest'
    bot_mode['params'] = {
        'display_mode': (
            'Мин. прайс: all'
            if view == 'all' else
            ('Мин. прайс: кастомный PVE' if view == 'pve' else 'Мин. прайс: кастомный скины')
        ),
        'target_label': ", ".join(target_bits) if target_bits else 'кастомный набор',
        'restore_mode': config.search_mode,
    }
    bot_mode['started_at'] = time.time()

    async def _run_custom_search():
        from monitor import search_min_price
        done = 0
        step_done = 0
        summary_data = []

        async def _update_progress(current_label):
            if context.bot_data.get('checkstop_pending'):
                return
            bar, pct = _make_progress_bar(step_done, max(total_steps, 1))
            bot_mode['params']['target_label'] = current_label
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text=(
                        f"💰 <b>Кастомный мін. прайс</b>\n\n"
                        f"🎮 Скинов: {len(selected)} | 🏆 Изданий: {len(selected_eds)} | 🛡 Подтв. PVE: {'да' if selected_confirmed_pve else 'нет'} | 🔓 Неподтв. PVE: {'да' if selected_unconfirmed_pve else 'нет'}\n\n"
                        f"{bar} {pct}%\n"
                        f"📦 Проверено: {done}/{total_items}\n"
                        f"🔎 Сейчас: <b>{current_label}</b>"
                    ),
                    parse_mode='HTML',
                    reply_markup=_check_control_markup()
                )
            except Exception:
                pass

        async def _search_step(label, keywords, require_pve=False):
            return await _run_minprice_search_with_watchdog(
                context,
                label,
                lambda: search_min_price(keywords, require_pve=require_pve),
                heartbeat_callback=lambda elapsed: _update_progress(f"{label} ({elapsed}с)")
            )

        async def _send_skin_partial_result(label, any_results, pve_results, item_id):
            text = _build_skin_minprice_text(label, any_results, pve_results, done=done, total=total_items, expanded=False)
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Показать ещё", callback_data=f"set:minprice:show3:skin:{item_id}")
            ]])
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
            except Exception:
                pass

        async def _send_simple_partial_result(label, results, cache_type, cache_id):
            text = _build_simple_minprice_text(label, results, done=done, total=total_items, expanded=False)
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Показать ещё", callback_data=f"set:minprice:show3:{cache_type}:{cache_id}")
            ]])
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
            except Exception:
                pass

        async def _stop_bundle():
            context.bot_data['cancel_current_check'] = False
            bot_mode['mode'] = 'standard'
            bot_mode['params'] = {}
            bot_mode['started_at'] = None
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_id,
                text="⏹ <b>Кастомный мін. прайс остановлен</b>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 К выбору", callback_data="set:check:minprice")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
                ])
            )

        for sid in sorted(selected):
            if context.bot_data.get('cancel_current_check'):
                await _stop_bundle()
                return
            skin = skins.get(sid)
            if not skin:
                continue
            name = sid.replace('_', ' ').title()
            await _update_progress(f"🎮 {name} • без PVE")
            any_results = await _search_step(f"🎮 {name} • без PVE", skin.get('keywords', [sid]), require_pve=False)
            step_done += 1
            await _update_progress(f"🎮 {name} • с PVE")
            pve_results = await _search_step(f"🎮 {name} • с PVE", skin.get('keywords', [sid]), require_pve=True)
            step_done += 1
            done += 1
            await _update_progress(f"🎮 {name}")
            any_best = any_results[0] if any_results else None
            pve_best = pve_results[0] if pve_results else None
            no_any = _same_min_offer(any_best, pve_best) or not any_results
            any_markup = "—" if no_any else _format_log_offer_v2(any_best)
            pve_markup = _format_log_offer_v2(pve_best)
            record_price_snapshot('skin', sid, name, 'any', any_results, source='custom_minprice')
            record_price_snapshot('skin', sid, name, 'pve', pve_results, source='custom_minprice')
            _cache_minprice_top3(context, 'skin', sid, f"🎮 {name}", any_results=any_results, pve_results=pve_results)
            summary_data.append(('skin', f"🎮 {name}", any_markup, pve_markup))
            await _send_skin_partial_result(f"🎮 {name}", any_results, pve_results, sid)

        if selected_confirmed_pve:
            if context.bot_data.get('cancel_current_check'):
                await _stop_bundle()
                return
            await _update_progress(_confirmed_pve_title())
            results = await _search_step(_confirmed_pve_title(), config.get_confirmed_pve(), require_pve=False)
            step_done += 1
            done += 1
            await _update_progress(_confirmed_pve_title())
            best_offer = results[0] if results else None
            price_markup = _format_log_offer_v2(best_offer)
            record_price_snapshot('pve', 'confirmed', _confirmed_pve_title(), 'confirmed', results, source='custom_minprice')
            _cache_minprice_top3(context, 'pveconfirmed', 'confirmed', _confirmed_pve_title(), any_results=results)
            summary_data.append(('simple', _confirmed_pve_title(), price_markup, ''))
            await _send_simple_partial_result(_confirmed_pve_title(), results, 'pveconfirmed', 'confirmed')

        if selected_unconfirmed_pve:
            if context.bot_data.get('cancel_current_check'):
                await _stop_bundle()
                return
            await _update_progress("🔓 Неподтв. PVE")
            results = await _search_step("🔓 Неподтв. PVE", config.get_unconfirmed_pve(), require_pve=False)
            step_done += 1
            done += 1
            await _update_progress("🔓 Неподтв. PVE")
            best_offer = results[0] if results else None
            price_markup = _format_log_offer_v2(best_offer)
            record_price_snapshot('pve', 'unconfirmed', "Неподтв. PVE", 'unconfirmed', results, source='custom_minprice')
            _cache_minprice_top3(context, 'pveunconfirmed', 'unconfirmed', "🔓 Неподтв. PVE", any_results=results)
            summary_data.append(('simple', "🔓 Неподтв. PVE", price_markup, ''))
            await _send_simple_partial_result("🔓 Неподтв. PVE", results, 'pveunconfirmed', 'unconfirmed')

        for eid in ['super_deluxe', 'limited', 'ultimate']:
            if context.bot_data.get('cancel_current_check'):
                await _stop_bundle()
                return
            if eid not in selected_eds:
                continue
            ed = editions.get(eid)
            if not ed:
                continue
            name = eid.replace('_', ' ').title()
            await _update_progress(f"🏆 {name}")
            results = await _search_step(f"🏆 {name}", ed.get('keywords', [eid]), require_pve=False)
            step_done += 1
            done += 1
            await _update_progress(f"🏆 {name}")
            best_offer = results[0] if results else None
            price_markup = _format_log_offer_v2(best_offer)
            record_price_snapshot('edition', eid, name, 'all', results, source='custom_minprice')
            _cache_minprice_top3(context, 'ed', eid, f"🏆 {name}", any_results=results)
            summary_data.append(('simple', f"🏆 {name}", price_markup, ''))
            await _send_simple_partial_result(f"🏆 {name}", results, 'ed', eid)

        summary = "📊 <b>Сводка мін. цен:</b>\n\n"
        for item_kind, label, first_markup, second_markup in summary_data:
            if item_kind == 'skin':
                summary += (
                    f"{label}\n"
                    f"🪄 Без PVE: <b>{first_markup}</b>\n"
                    f"🔒 С PVE: <b>{second_markup}</b>\n\n"
                )
            else:
                summary += f"{label} — <b>{first_markup}</b>\n"
        bar, pct = _make_progress_bar(step_done, max(total_steps, 1))
        keyboard = [
            [InlineKeyboardButton("🔄 Повторить", callback_data="set:minprice:crun")],
            [InlineKeyboardButton("🔙 К выбору", callback_data="set:check:minprice")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
        ]
        final_text = f"{summary}\n{bar} {pct}%\n✅ Проверено {done} позиций."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        except Exception:
            await context.bot.send_message(chat_id, final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None

    asyncio.create_task(_run_custom_search())



def _clone_skin_restore_state(config):
    return {
        sid: {
            'enabled': skin.get('enabled', True),
            'price': skin.get('price', 0),
            'require_pve': skin.get('require_pve', False),
        }
        for sid, skin in config.get_all_skins().items()
    }


def _clone_edition_restore_state(config):
    return {
        eid: {
            'enabled': edition.get('enabled', True),
            'price': edition.get('price', 0),
        }
        for eid, edition in config.get_all_editions().items()
    }


def _get_last_recheck_result(context, run_id=None, chat_id=None):
    result = context.bot_data.get('last_recheck_result')
    if not result:
        return None
    if run_id and result.get('run_id') != run_id:
        return None
    if chat_id is not None and str(result.get('chat_id')) != str(chat_id):
        return None
    return result


def _build_recheck_result_text(result):
    return (
        f"{result['title']}\n"
        f"📨 Отправлено {result['sent_count']} подходящих предложений."
    )


def _build_recheck_result_markup(result):
    run_id = result['run_id']
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Посмотреть лог", callback_data=f"set:recheck:showlog:{run_id}:0")],
        [InlineKeyboardButton("🔄 Повторить", callback_data=f"set:recheck:repeat:{run_id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
    ])


async def _send_recheck_result_message(chat_id, context, sent_count, snapshot, title):
    result = {
        'run_id': snapshot['run_id'],
        'chat_id': str(chat_id),
        'title': title,
        'sent_count': sent_count,
        'snapshot': snapshot,
        'log_items': snapshot.get('log_items'),
    }
    context.bot_data['last_recheck_result'] = result
    await context.bot.send_message(
        chat_id=chat_id,
        text=_build_recheck_result_text(result),
        reply_markup=_build_recheck_result_markup(result),
        parse_mode='HTML'
    )


def _build_recheck_log_progress_text(result, done, total, current):
    bar, pct = _make_progress_bar(done, total)
    return (
        "📋 <b>Готовлю лог перепроверки</b>\n\n"
        f"🔄 Режим: {result['snapshot'].get('display_mode', 'Перепроверка')}\n"
        f"{bar} {pct}%\n"
        f"📦 Прогресс: {done}/{max(total, 1)}\n"
        f"🔎 Сейчас: {current}"
    )
 
 
def _search_mode_label(mode_key):
    return 'По списку'


def _running_mode_label(mode_key):
    return {
        'standard': 'Автомониторинг',
        'recheck': 'Перепроверка',
        'recheck_pve': 'Перепроверка',
        'recheck_premium': 'Перепроверка',
        'pricetest': 'Мин. прайс',
    }.get(mode_key, mode_key or 'Автомониторинг')


def _resolve_status_state(config, bot_mode, progress):
    params = bot_mode.get('params', {}) or {}
    mode = bot_mode.get('mode', 'standard')
    auto_mode_key = params.get('restore_mode') if mode != 'standard' and params.get('restore_mode') else getattr(config, 'search_mode', 'standard')
    auto_mode_label = _search_mode_label(auto_mode_key)
    running_label = params.get('display_mode') or (auto_mode_label if mode == 'standard' else _running_mode_label(mode))
    target_label = params.get('target_label')
    return {
        'mode': mode,
        'auto_mode_label': auto_mode_label,
        'running_label': running_label,
        'target_label': target_label,
        'stage': progress.get('stage') or "Подготовка",
        'done': progress.get('done', 0),
        'total': max(progress.get('total', 1), 1),
        'sent': progress.get('sent', 0),
        'current': progress.get('current') or "Ожидание данных",
    }


async def _show_recheck_log(query, context, run_id, page=0, refresh=False):
    result = _get_last_recheck_result(context, run_id=run_id, chat_id=query.message.chat_id)
    if not result:
        await query.edit_message_text("❌ Лог уже недоступен.")
        return

    build_log_fn = context.bot_data.get('build_recheck_log')
    if not build_log_fn:
        await query.edit_message_text("❌ Генератор лога не найден.")
        return

    if refresh or result.get('log_items') is None:
        total = len(result['snapshot'].get('positions', []))
        progress_state = {'last_done': None}

        async def progress_callback(done, total_items, current):
            if progress_state['last_done'] == done:
                return
            progress_state['last_done'] = done
            try:
                await query.edit_message_text(
                    _build_recheck_log_progress_text(result, done, total_items, current),
                    parse_mode='HTML'
                )
            except Exception:
                pass

        result['log_items'] = await build_log_fn(result['snapshot'], progress_callback=progress_callback)
        context.bot_data['last_recheck_result'] = result

    text, markup = _build_recheck_log_page_v2(result, page)
    await query.edit_message_text(text, reply_markup=markup, parse_mode='HTML')


def _format_log_offer_v2(offer):
    if not offer:
        return "—"
    price_text = html.escape(offer.get('price_text') or "—")
    href = offer.get('href')
    if href:
        return f"<a href='{html.escape(href, quote=True)}'>{price_text}</a>"
    return price_text


def _same_min_offer(a, b):
    """True if two offers are effectively the same (same price → no separate 'без PVE' offer)."""
    if not a or not b:
        return False
    ap, bp = a.get('price'), b.get('price')
    if ap is not None and bp is not None:
        try:
            return float(ap) == float(bp)
        except (TypeError, ValueError):
            pass
    return (a.get('price_text') or '') == (b.get('price_text') or '')


def _build_skin_minprice_text(name, any_results, pve_results, done=None, total=None, expanded=False):
    """Build skin minprice message. If any_best == pve_best → dash for 'без PVE'."""
    any_best = any_results[0] if any_results else None
    pve_best = pve_results[0] if pve_results else None
    no_any = _same_min_offer(any_best, pve_best) or not any_results

    lines = [f"💰 <b>{name}</b>"]
    if expanded:
        lines.append("")
        lines.append("🪄 <b>Без PVE:</b>")
        if no_any:
            lines.append("—")
        else:
            for idx, offer in enumerate(any_results[:3], start=1):
                lines.append(f"{idx}. {_format_log_offer_v2(offer)}")
        lines.append("")
        lines.append("🔒 <b>С PVE:</b>")
        if pve_results:
            for idx, offer in enumerate(pve_results[:3], start=1):
                lines.append(f"{idx}. {_format_log_offer_v2(offer)}")
        else:
            lines.append("—")
    else:
        any_display = "—" if no_any else _format_log_offer_v2(any_best)
        pve_display = _format_log_offer_v2(pve_best)
        lines.append(f"🪄 Без PVE: <b>{any_display}</b>")
        lines.append(f"🔒 С PVE: <b>{pve_display}</b>")

    if done is not None and total is not None:
        lines.append(f"📦 Готово: {done}/{total}")
    return "\n".join(lines)


def _build_simple_minprice_text(label, results, done=None, total=None, expanded=False):
    """Build simple (non-skin) minprice message."""
    lines = [f"💰 <b>{label}</b>"]
    if expanded:
        lines.append("")
        lines.append("📉 <b>Варианты:</b>")
        if results:
            for idx, offer in enumerate(results[:3], start=1):
                lines.append(f"{idx}. {_format_log_offer_v2(offer)}")
        else:
            lines.append("—")
    else:
        price_markup = _format_log_offer_v2(results[0] if results else None)
        lines.append(f"📉 Мин. цена: <b>{price_markup}</b>")
    if done is not None and total is not None:
        lines.append(f"📦 Готово: {done}/{total}")
    return "\n".join(lines)


def _cache_minprice_top3(context, item_type, item_id, name, any_results=None, pve_results=None):
    cache = context.user_data.setdefault('minprice_top3_cache', {})
    cache[f"{item_type}:{item_id}"] = {
        'item_type': item_type,
        'item_id': item_id,
        'name': name,
        'any_results': list(any_results or [])[:3],
        'pve_results': list(pve_results or [])[:3],
    }


def _render_top3_block(title, offers):
    lines = [f"{title}"]
    if not offers:
        lines.append("—")
        return "\n".join(lines)
    for idx, offer in enumerate(offers[:3], start=1):
        price_markup = _format_log_offer_v2(offer)
        seller = html.escape(offer.get('seller') or "?")
        matched_kw = html.escape(offer.get('matched_kw') or "—")
        lines.append(f"{idx}. {price_markup}")
        lines.append(f"👤 {seller}")
        lines.append(f"🧩 {matched_kw}")
    return "\n".join(lines)


async def _send_minprice_top3(query, context, item_type, item_id):
    cache = context.user_data.get('minprice_top3_cache', {})
    payload = cache.get(f"{item_type}:{item_id}")
    if not payload:
        await query.answer("Сначала запустите мін. цену для этой позиции", show_alert=True)
        return

    name = payload.get('name') or item_id
    any_results = payload.get('any_results') or []
    pve_results = payload.get('pve_results') or []

    if item_type == 'skin':
        text = _build_skin_minprice_text(name, any_results, pve_results, expanded=True)
    else:
        text = _build_simple_minprice_text(name, any_results, expanded=True)

    try:
        await query.edit_message_text(
            text=text,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await query.answer()


def _build_recheck_log_page_v2(result, page=0):
    items = result.get('log_items') or []
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * ITEMS_PER_PAGE
    page_items = items[start:start + ITEMS_PER_PAGE]

    text = (
        f"📋 <b>Лог перепроверки</b> ({page + 1}/{total_pages})\n\n"
        f"🔄 Режим: {result['snapshot'].get('display_mode', 'Перепроверка')}\n"
        f"📨 Отправлено: {result['sent_count']}\n\n"
    )

    if not page_items:
        text += "❌ Лог пуст."
    else:
        for item in page_items:
            title_icon = "🏆" if item['type'] == 'edition' else ("🔒" if item['type'] == 'pve' else _skin_emoji(item['id']))
            any_label = "📉 Найдено" if item['type'] == 'edition' else "🪄 Без PVE"
            text += (
                f"{title_icon} <b>{html.escape(item['name'])}</b>\n"
                f"💰 <b>Цена моя:</b> {item.get('my_price_text', item.get('limit_text', '—'))}\n"
                f"{any_label}: {_format_log_offer_v2(item.get('any_offer'))}\n"
            )
            if item['type'] != 'edition':
                text += f"🔒 С PVE: {_format_log_offer_v2(item.get('pve_offer'))}\n"
            text += (
                f"📌 <b>Причина:</b> {html.escape(item.get('reason_text', item.get('status', '—')))}\n"
                "━━━━━━━━━━━━━━\n\n"
            )

    nav = []
    run_id = result['run_id']
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"set:recheck:showlog:{run_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="set:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"set:recheck:showlog:{run_id}:{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔄 Обновить экран", callback_data=f"set:recheck:refreshlog:{run_id}:{page}")])
    keyboard.append([InlineKeyboardButton("🔙 К итогу", callback_data=f"set:recheck:summary:{run_id}")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")])
    return text.strip(), InlineKeyboardMarkup(keyboard)


def _build_recheck_start_text(snapshot):
    display_mode = snapshot.get('display_mode', 'Перепроверка')
    if snapshot.get('bot_mode_key') == 'pricetest':
        return (
            "🧪 <b>Мин. прайс тест запущен повторно!</b>\n\n"
            f"🎯 Режим: {display_mode}\n"
            "⚠️ Может занять несколько минут..."
        )
    return (
        "🔄 <b>Перепроверка запущена!</b>\n"
        f"🎯 Режим: {display_mode}\n"
        "⚠️ Может занять несколько минут..."
    )


async def _repeat_recheck_from_result(query, context, result):
    config = context.bot_data['config']
    bot_mode = context.bot_data.get('bot_mode', {})
    process_fn = context.bot_data.get('process_offers')
    build_snapshot_fn = context.bot_data.get('build_recheck_snapshot')
    snapshot = result['snapshot']

    if _is_check_running(context):
        await _show_current_check_status(query, context)
        return

    restore_mode = config.search_mode
    restore_skins = _clone_skin_restore_state(config)
    restore_editions = _clone_edition_restore_state(config)

    for sid, data in snapshot.get('skin_states', {}).items():
        skin = config.get_skin(sid)
        if skin:
            skin['enabled'] = data.get('enabled', True)
            skin['price'] = data.get('price', 0)
            skin['require_pve'] = data.get('require_pve', False)

    for eid, data in snapshot.get('edition_states', {}).items():
        edition = config.get_edition(eid)
        if edition:
            edition['enabled'] = data.get('enabled', True)
            edition['price'] = data.get('price', 0)

    config.search_mode = snapshot.get('search_mode', config.search_mode)

    new_snapshot = build_snapshot_fn(
        config,
        display_mode=snapshot.get('display_mode', 'Перепроверка'),
        bot_mode_key=snapshot.get('bot_mode_key', 'recheck'),
        search_mode=snapshot.get('search_mode', config.search_mode),
        include_unconfirmed_pve=snapshot.get('include_unconfirmed_pve', False),
        premium_only=snapshot.get('premium_only', False),
        max_price_override=snapshot.get('max_price_override'),
        rare_override=snapshot.get('rare_override'),
        pve_override=snapshot.get('pve_override'),
        confirmed_pve_enabled_override=snapshot.get('confirmed_pve_enabled_override'),
        confirmed_pve_price_override=snapshot.get('confirmed_pve_price_override'),
        chat_id_value=query.message.chat_id,
        log_view=snapshot.get('log_view'),
    )

    params = {
        'display_mode': snapshot.get('display_mode', 'Перепроверка'),
        'restore_mode': restore_mode,
        'restore_skins': restore_skins,
        'restore_editions': restore_editions,
        'run_snapshot': new_snapshot,
    }
    if snapshot.get('bot_mode_key') == 'pricetest':
        params['rare_price'] = snapshot.get('rare_override')
        params['pve_price'] = snapshot.get('pve_override')
        params['max_price'] = snapshot.get('max_price_override')

    bot_mode['mode'] = snapshot.get('bot_mode_key', 'recheck')
    bot_mode['params'] = params
    bot_mode['started_at'] = time.time()

    await query.edit_message_text(_build_recheck_start_text(new_snapshot), parse_mode='HTML', reply_markup=_check_control_markup())
    asyncio.create_task(_run_recheck_task(
        query.message.chat_id,
        context,
        process_fn,
        **snapshot.get('process_kwargs', {})
    ))


async def _run_recheck_task(chat_id, context, process_fn, **kwargs):
    """Фоновая задача recheck (через asyncio.create_task)."""
    bot_mode = context.bot_data.get('bot_mode', {})
    try:
        sent_count = await process_fn(context=context, **kwargs)
        if sent_count == -1:
            text, keyboard = _get_current_check_status(context)
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode='HTML')
            return
        if sent_count == -2:
            context.bot_data.pop('current_check_sent_positions', None)
            bot_mode['mode'] = 'standard'
            bot_mode['params'] = {}
            bot_mode['started_at'] = None
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏹ Текущая проверка остановлена принудительно."
            )
            return
        snapshot = bot_mode.get('params', {}).get('run_snapshot')
        sent_position_ids = sorted(context.bot_data.pop('current_check_sent_positions', set()))
        # Восстанавливаем режим поиска если был изменён для recheck
        config = context.bot_data['config']
        restore_mode = bot_mode.get('params', {}).get('restore_mode')
        if restore_mode:
            config.search_mode = restore_mode
        # Восстанавливаем скины если были изменены для кастомного recheck
        restore_skins = bot_mode.get('params', {}).get('restore_skins')
        if restore_skins:
            for sid, data in restore_skins.items():
                skin = config.get_skin(sid)
                if skin:
                    if isinstance(data, dict):
                        skin['enabled'] = data.get('enabled', True)
                        skin['price'] = data.get('price', 0)
                    else:
                        skin['enabled'] = data
            config.save()
        # Восстанавливаем издания
        restore_editions = bot_mode.get('params', {}).get('restore_editions')
        if restore_editions:
            for eid, data in restore_editions.items():
                ed = config.get_edition(eid)
                if ed and isinstance(data, dict):
                    ed['enabled'] = data.get('enabled', True)
                    ed['price'] = data.get('price', 0)
            config.save()
        restore_confirmed_pve = bot_mode.get('params', {}).get('restore_confirmed_pve')
        if restore_confirmed_pve:
            config.confirmed_pve_enabled = restore_confirmed_pve.get('enabled', True)
            config.confirmed_pve_price = restore_confirmed_pve.get('price', config.confirmed_pve_price)
        # Сбрасываем режим
        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None
        if snapshot:
            snapshot['sent_position_ids'] = sent_position_ids
            title = "✅ Мин. прайс тест завершён!" if snapshot.get('bot_mode_key') == 'pricetest' else "✅ Перепроверка завершена!"
            await _send_recheck_result_message(
                chat_id=chat_id,
                context=context,
                sent_count=sent_count,
                snapshot=snapshot,
                title=title
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Перепроверка завершена!\n📨 Отправлено {sent_count} подходящих предложений."
            )
    except Exception as e:
        # Восстанавливаем режим поиска при ошибке тоже
        config = context.bot_data['config']
        context.bot_data.pop('current_check_sent_positions', None)
        restore_mode = bot_mode.get('params', {}).get('restore_mode')
        if restore_mode:
            config.search_mode = restore_mode
        # Восстанавливаем скины при ошибке тоже
        restore_skins = bot_mode.get('params', {}).get('restore_skins')
        if restore_skins:
            for sid, data in restore_skins.items():
                skin = config.get_skin(sid)
                if skin:
                    if isinstance(data, dict):
                        skin['enabled'] = data.get('enabled', True)
                        skin['price'] = data.get('price', 0)
                    else:
                        skin['enabled'] = data
            config.save()
        # Восстанавливаем издания при ошибке тоже
        restore_editions = bot_mode.get('params', {}).get('restore_editions')
        if restore_editions:
            for eid, data in restore_editions.items():
                ed = config.get_edition(eid)
                if ed and isinstance(data, dict):
                    ed['enabled'] = data.get('enabled', True)
                    ed['price'] = data.get('price', 0)
            config.save()
        restore_confirmed_pve = bot_mode.get('params', {}).get('restore_confirmed_pve')
        if restore_confirmed_pve:
            config.confirmed_pve_enabled = restore_confirmed_pve.get('enabled', True)
            config.confirmed_pve_price = restore_confirmed_pve.get('price', config.confirmed_pve_price)
        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None
        logger.error(f"Ошибка recheck: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка при recheck: {e}"
            )
        except:
            pass

def _get_current_check_status(context):
    config = context.bot_data['config']
    bot_mode = context.bot_data.get('bot_mode', {}) or {}
    progress = context.bot_data.get('current_check_progress') or {}
    state = _resolve_status_state(config, bot_mode, progress)
    running_now = 'Автомониторинг' if state['mode'] == 'standard' else state['running_label']
    bar, pct = _make_progress_bar(state['done'], state['total'])

    target_line = f"🧩 Цель: {state['target_label']}\n" if state['target_label'] else ''
    text = (
        "⏳ <b>Сейчас уже идёт проверка</b>\n\n"
        f"🔍 Автомониторинг: {state['auto_mode_label']}\n"
        f"⚙️ Запущен сейчас: {running_now}\n"
        f"{target_line}"
        f"📍 Этап: {state['stage']}\n"
        f"{bar} {pct}%\n"
        f"📦 Прогресс: {state['done']}/{state['total']}\n"
        f"🔎 Сейчас: {state['current']}\n"
        f"✅ Отправлено: {state['sent']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
        [InlineKeyboardButton("⏹ Завершить принудительно", callback_data="set:checkstop")],
    ])
    return text, keyboard


async def _show_current_check_status(query, context):
    text, keyboard = _get_current_check_status(context)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')


def _is_check_running(context):
    bot_mode = context.bot_data.get('bot_mode', {})
    if bot_mode.get('mode', 'standard') != 'standard':
        return True
    if context.bot_data.get('current_check_origin') == 'background':
        return False
    return bool(context.bot_data.get('current_check_progress'))


async def _show_recheck_menu_as_new_message(update, context):
    await _show_check_menu_as_new_message(update, context)


# ==============================
# ОСНОВНОЙ РОУТЕР CALLBACK
# ==============================

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Роутер для всех callback_data, начинающихся с 'set:'."""
    query = update.callback_query
    await query.answer()

    if not _check_auth(update, context):
        await query.answer("❌ Не авторизованы", show_alert=True)
        return

    data = query.data
    config = context.bot_data['config']
    parts = data.split(':')

    # === Навигация ===
    if data == "set:main":
        await _show_main_menu(query, context)

    elif data == "set:close":
        await query.edit_message_text("✅ Настройки закрыты.")

    elif data == "set:stop":
        keyboard = [
            [InlineKeyboardButton("✅ Да, остановить", callback_data="set:stop:confirm")],
            [InlineKeyboardButton("🔙 Нет, продолжить", callback_data="set:stop:cancel")],
        ]
        await query.edit_message_text(
            "⏹ <b>Закончить сейчас?</b>\n"
            "Текущая работа будет остановлена.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "set:stop:confirm":
        await query.edit_message_text("⏹ <b>Остановка бота...</b>", parse_mode='HTML')
        import os
        asyncio.get_event_loop().call_later(1, lambda: os._exit(0))

    elif data == "set:stop:cancel":
        await _show_main_menu(query, context)

    elif data == "set:checkstop":
        context.bot_data['checkstop_pending'] = True
        bot_mode = context.bot_data.get('bot_mode', {}) or {}
        progress = context.bot_data.get('current_check_progress') or {}
        mode_key = bot_mode.get('mode', 'standard')
        mode_labels = {
            'standard': 'Автомониторинг',
            'pricetest': 'Мин. прайс',
            'recheck': 'Полная перепроверка',
            'recheck_pve': 'Перепроверка (неподтв. PVE)',
        }
        label = mode_labels.get(mode_key, mode_key)
        target = (bot_mode.get('params') or {}).get('target_label') or progress.get('stage') or '—'
        info_text = (
            "⚠️ <b>Остановить текущую проверку?</b>\n\n"
            f"⚙️ Запущено: <b>{label}</b>\n"
            f"🎯 Цель: {target}\n"
            f"📦 Прогресс: {progress.get('done', 0)}/{max(progress.get('total', 1), 1)}\n\n"
            "Вы уверены?"
        )
        await query.edit_message_text(
            info_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, остановить", callback_data="set:checkstop:confirm")],
                [InlineKeyboardButton("↩️ Отмена", callback_data="set:checkstop:cancel")],
            ])
        )

    elif data == "set:checkstop:confirm":
        context.bot_data.pop('checkstop_pending', None)
        context.bot_data['cancel_current_check'] = True
        await query.edit_message_text(
            "⏹ <b>Останавливаю текущую проверку...</b>\n"
            "Она завершится после текущего шага.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")]
            ])
        )

    elif data == "set:checkstop:cancel":
        context.bot_data.pop('checkstop_pending', None)
        await _show_main_menu(query, context)

    elif data.startswith("set:hist:"):
        item_type = parts[2] if len(parts) > 2 else 'skin'
        item_id = parts[3] if len(parts) > 3 else ''
        mode = parts[4] if len(parts) > 4 else 'all'
        page = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        back_token = parts[6] if len(parts) > 6 else 'main'
        await _show_price_history(query, context, item_type, item_id, mode=mode, page=page, back_token=back_token)

    elif data == "set:noop":
        pass

    elif data == "set:status":
        await _show_status(query, context)

    elif data == "set:stats":
        await _show_stats(query, context)

    elif data == "set:stats:items":
        await _show_stats_items(query, context)

    elif data.startswith("set:stats:hist:"):
        parts = data.split(":")
        if len(parts) >= 5:
            s_item_type = parts[3]
            s_item_id = ":".join(parts[4:])
            await _show_stats_item_history(query, context, s_item_type, s_item_id)

    elif data == "set:sync":
        sync_fn = context.bot_data.get('sync_fn')
        if sync_fn:
            await query.edit_message_text("🔄 Синхронизирую config.json с GitHub...")
            try:
                result = await asyncio.to_thread(sync_fn)
                if result:
                    await query.edit_message_text("✅ config.json успешно загружен в GitHub!")
                else:
                    await query.edit_message_text("ℹ️ config.json не изменился, пуш не нужен.")
            except Exception as e:
                await query.edit_message_text(f"❌ Ошибка синхронизации: {e}")
        else:
            await query.edit_message_text("❌ GitHub не настроен: нет GITHUB_TOKEN/GITHUB_REPO.")

    # === Unified Check ===
    elif parts[1] == 'check':
        action = parts[2] if len(parts) > 2 else 'menu'
        if action == 'menu':
            await _show_check_menu(query, context)
        elif action in ('custom', 'customskins', 'custompve'):
            await _show_check_menu(query, context)
        elif action == 'full':
            await _start_full_recheck(query, context, config)
        elif action == 'minprice':
            section = parts[3] if len(parts) > 3 else ''
            if section == 'skins':
                await _show_minprice_section_menu(query, context, section='skins', back_cb='set:check:menu')
            elif section == 'pve':
                await _show_minprice_section_menu(query, context, section='pve', back_cb='set:check:menu')
            elif section == 'all':
                context.user_data['mp_custom_view'] = 'all'
                context.user_data['mp_custom_skins'] = set(config.get_all_skins().keys())
                context.user_data['mp_custom_editions'] = set(config.get_all_editions().keys())
                context.user_data['mp_custom_confirmed_pve'] = True
                context.user_data['mp_custom_unconfirmed_pve'] = True
                _save_minprice_selection(context)
                await _launch_minprice_bundle_run(query, context, config)
            else:
                text = (
                    "💰 <b>Мин. цена</b>\n\n"
                    "Выберите раздел:"
                )
                keyboard = [
                    [InlineKeyboardButton("🎮 Скины", callback_data="set:check:minprice:skins")],
                    [InlineKeyboardButton("🔒 PVE", callback_data="set:check:minprice:pve")],
                    [InlineKeyboardButton("🌐 All", callback_data="set:check:minprice:all")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="set:check:menu")],
                ]
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif action == 'standard':
            await _show_standard_check_menu(query, context)
        elif action == 'stdskins':
            subaction = parts[3] if len(parts) > 3 else ''
            if subaction == 'recheck':
                if _is_check_running(context):
                    await _show_current_check_status(query, context)
                    return
                if not any(s.get('enabled', True) for s in config.get_all_skins().values()):
                    await query.answer("❌ Включите хотя бы один скин в списке", show_alert=True)
                    return
                _seed_standard_skin_recheck(context, config)
                await _launch_custom_recheck(query, context, config, pve_price=None, from_callback=True)
            elif subaction == 'minprice':
                await _show_minprice_section_menu(query, context, section='skins', back_cb='set:check:stdskins')
            else:
                await _show_standard_check_skins_menu(query, context)
        elif action == 'stdpve':
            item = parts[3] if len(parts) > 3 else ''
            subaction = parts[4] if len(parts) > 4 else ''
            if item == 'confirmed' and subaction == 'recheck':
                if _is_check_running(context):
                    await _show_current_check_status(query, context)
                    return
                _seed_standard_confirmed_pve_recheck(context, config)
                await _launch_custom_recheck(query, context, config, pve_price=None, from_callback=True)
            elif item == 'editions' and subaction == 'recheck':
                if _is_check_running(context):
                    await _show_current_check_status(query, context)
                    return
                if not any(ed.get('enabled', True) for ed in config.get_all_editions().values()):
                    await query.answer("❌ Включите хотя бы одно издание в списке", show_alert=True)
                    return
                _seed_standard_editions_recheck(context, config)
                await _launch_custom_recheck(query, context, config, pve_price=None, from_callback=True)
            elif item == 'unconfirmed' and subaction == 'recheck':
                if _is_check_running(context):
                    await _show_current_check_status(query, context)
                    return
                context.user_data['input_state'] = INPUT_RECHECK_PVE
                context.user_data['recheck_direct_unconfirmed_pve'] = True
                _set_input_return(context, 'set:check:stdpve', '🔙 Назад')
                await query.edit_message_text(
                    "🔓 <b>Неподтв. PVE</b>\n\n"
                    "Будет искать любые упоминания PVE (пве/stw/pve) без привязки к скинам.\n\n"
                    "Введите <b>макс. цену</b> (₽) или нажмите кнопку ниже:",
                    parse_mode='HTML',
                    reply_markup=_get_input_return_markup(context, 'set:check:stdpve', '🔙 Назад')
                )
            elif item in ('confirmed', 'editions') and subaction == 'minprice':
                await _show_minprice_section_menu(query, context, section='pve', back_cb='set:check:stdpve')
            elif item in ('confirmed', 'editions', 'unconfirmed'):
                await _show_standard_check_item_menu(query, context, item)
            else:
                await _show_standard_check_pve_menu(query, context)

    # === Recheck ===
    elif parts[1] == 'recheck':
        action = parts[2] if len(parts) > 2 else 'menu'
        bot_mode = context.bot_data.get('bot_mode', {})
        process_fn = context.bot_data.get('process_offers')
        build_snapshot_fn = context.bot_data.get('build_recheck_snapshot')
        chat_id = _get_chat_id_from_context(context)

        if action == 'menu':
            await _show_recheck_menu(query, context)
        elif action in ('custom', 'cm', 'cmpve', 'cmunconfirmed', 'cmtoggle',
                        'cmpvetoggle', 'cmpveprice', 'cmpvesave', 'cmpverun', 'cmprice',
                        'cmsave', 'cmgo', 'edtoggle', 'edprice', 'edsave', 'edgo'):
            await _show_check_menu(query, context)
        elif action == 'showlog':
            run_id = parts[3] if len(parts) > 3 else ''
            page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            await _show_recheck_log(query, context, run_id, page=page, refresh=False)

        elif action == 'refreshlog':
            run_id = parts[3] if len(parts) > 3 else ''
            page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            await _show_recheck_log(query, context, run_id, page=page, refresh=True)

        elif action == 'summary':
            run_id = parts[3] if len(parts) > 3 else ''
            result = _get_last_recheck_result(context, run_id=run_id, chat_id=query.message.chat_id)
            if not result:
                await query.edit_message_text("❌ Итог перепроверки уже недоступен.")
                return
            await query.edit_message_text(
                _build_recheck_result_text(result),
                reply_markup=_build_recheck_result_markup(result),
                parse_mode='HTML'
            )

        elif action == 'repeat':
            run_id = parts[3] if len(parts) > 3 else ''
            result = _get_last_recheck_result(context, run_id=run_id, chat_id=query.message.chat_id)
            if not result:
                await query.edit_message_text("❌ Повтор уже недоступен.")
                return
            await _repeat_recheck_from_result(query, context, result)

        elif action == 'standard':
            await _start_full_recheck(query, context, config)

        elif action == 'minprice':
            await _show_minprice_menu(query, context)

    # === Переключатели списка ===
    elif parts[1] == 'minprice':
        item_type = parts[2] if len(parts) > 2 else ''
        item_id = parts[3] if len(parts) > 3 else ''

        if item_type == 'custom':
            _ensure_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'skins')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_section_menu(query, context, section='skins', back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            return

        elif item_type == 'csel':
            _ensure_minprice_selection(context)
            selected = context.user_data.get('mp_custom_skins', set())
            if item_id in selected:
                selected.discard(item_id)
            else:
                selected.add(item_id)
            context.user_data['mp_custom_skins'] = selected
            _save_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cedsel':
            _ensure_minprice_selection(context)
            selected_eds = context.user_data.get('mp_custom_editions', set())
            if item_id in selected_eds:
                selected_eds.discard(item_id)
            else:
                selected_eds.add(item_id)
            context.user_data['mp_custom_editions'] = selected_eds
            _save_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cskinsall':
            _ensure_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view == 'skins':
                all_skins = {sid for sid in config.get_all_skins().keys() if config.get_skin(sid).get('enabled', True)}
            else:
                all_skins = set(config.get_all_skins().keys())
            selected = context.user_data.get('mp_custom_skins', set())
            if selected == all_skins:
                context.user_data['mp_custom_skins'] = set()
                await query.answer("🧹 Все скины сняты")
            else:
                context.user_data['mp_custom_skins'] = set(all_skins)
                await query.answer("✅ Выбраны все скины")
            _save_minprice_selection(context)
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'csall':
            context.user_data['mp_custom_skins'] = set(config.get_all_skins().keys())
            context.user_data['mp_custom_editions'] = set(config.get_all_editions().keys())
            context.user_data['mp_custom_confirmed_pve'] = True
            _save_minprice_selection(context)
            await query.answer("✅ Выбраны все позиции")
            await _show_minprice_custom(query, context)
            return

        elif item_type == 'csnone':
            view = context.user_data.get('mp_custom_view', 'all')
            if view == 'skins':
                context.user_data['mp_custom_skins'] = set()
            elif view == 'pve':
                context.user_data['mp_custom_editions'] = set()
                context.user_data['mp_custom_confirmed_pve'] = False
            else:
                context.user_data['mp_custom_skins'] = set()
                context.user_data['mp_custom_editions'] = set()
                context.user_data['mp_custom_confirmed_pve'] = False
            _save_minprice_selection(context)
            await query.answer("⬜ Выбор очищен")
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpve':
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpveall':
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpvenone':
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpvepos':
            current = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
            context.user_data['mp_custom_confirmed_pve'] = not current
            _save_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpveunconfirmed':
            current = context.user_data.get('mp_custom_unconfirmed_pve', False)
            context.user_data['mp_custom_unconfirmed_pve'] = not current
            _save_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'cpveallpos':
            current_selected = context.user_data.get('mp_custom_editions', set())
            edition_ids = {eid for eid in ['super_deluxe', 'limited', 'ultimate'] if config.get_edition(eid)}
            current_confirmed = context.user_data.get('mp_custom_confirmed_pve', config.confirmed_pve_enabled)
            current_unconfirmed = context.user_data.get('mp_custom_unconfirmed_pve', False)
            all_selected = current_confirmed and current_unconfirmed and all(eid in current_selected for eid in edition_ids)
            if all_selected:
                context.user_data['mp_custom_confirmed_pve'] = False
                context.user_data['mp_custom_unconfirmed_pve'] = False
                context.user_data['mp_custom_editions'] = set()
                await query.answer("⛔ Все PVE-позиции убраны")
            else:
                context.user_data['mp_custom_confirmed_pve'] = True
                context.user_data['mp_custom_unconfirmed_pve'] = True
                context.user_data['mp_custom_editions'] = set(edition_ids)
                await query.answer("✅ Все PVE-позиции добавлены")
            _save_minprice_selection(context)
            view = context.user_data.get('mp_custom_view', 'all')
            if view in ('skins', 'pve'):
                await _show_minprice_section_menu(query, context, section=view, back_cb=context.user_data.get('mp_back_cb', 'set:check:menu'))
            else:
                await _show_minprice_custom(query, context)
            return

        elif item_type == 'crun':
            await _launch_minprice_bundle_run(query, context, config)
            return

        elif item_type == 'show3':
            show_kind = parts[3] if len(parts) > 3 else ''
            show_id = parts[4] if len(parts) > 4 else ''
            await _send_minprice_top3(query, context, show_kind, show_id)
            return

        elif item_type == 'skinpick':
            await _show_minprice_section_menu(query, context, section='skins', back_cb='set:check:menu')
            return

        elif item_type == 'pveconfirmed':
            item_id = 'confirmed'
            all_keywords = config.get_confirmed_pve()
            name = "Подтв. PVE"
            require_pve = False
            dual_mode = False
            history_item_type = 'pve'
            history_mode = 'confirmed'
        elif item_type == 'pveunconfirmed':
            item_id = 'unconfirmed'
            all_keywords = config.get_unconfirmed_pve()
            name = "Неподтв. PVE"
            require_pve = False
            dual_mode = False
            history_item_type = 'pve'
            history_mode = 'unconfirmed'

        elif item_type == 'skin':
            skin = config.get_skin(item_id)
            if not skin:
                await query.answer("Скин не найден", show_alert=True)
                return
            all_keywords = skin.get('keywords', [item_id])
            name = item_id.replace('_', ' ').title()
            require_pve = False
            dual_mode = True
            history_item_type = 'skin'
            history_mode = 'all'
        elif item_type == 'ed':
            edition = config.get_edition(item_id)
            if not edition:
                await query.answer("Издание не найдено", show_alert=True)
                return
            all_keywords = edition.get('keywords', [item_id])
            name = item_id.replace('_', ' ').title()
            require_pve = False
            dual_mode = False
            history_item_type = 'edition'
            history_mode = 'all'
        else:
            return

        kw_list = ', '.join(all_keywords[:4])
        if len(all_keywords) > 4:
            kw_list += f" +ещё {len(all_keywords) - 4}"
        origin_kind = parts[4] if len(parts) > 4 else ''
        origin_page = parts[5] if len(parts) > 5 else ''
        if _is_check_running(context):
            await _show_current_check_status(query, context)
            return
        bot_mode = context.bot_data.get('bot_mode', {})
        bot_mode['mode'] = 'pricetest'
        bot_mode['params'] = {
            'display_mode': f"Мин. прайс: {name}",
            'target_label': name,
            'restore_mode': config.search_mode,
        }
        bot_mode['started_at'] = time.time()
        total_steps = 2 if dual_mode else 1
        await query.edit_message_text(
            f"💰 <b>Мін. прайс: {name}</b>\n\n"
            f"🧩 Ключевые слова: {kw_list}\n\n"
            f"{_make_progress_bar(0, total_steps)[0]} 0%\n"
            f"🔎 Проверяю: 0/{total_steps}\n"
            f"🧭 Сейчас: подготовка",
            parse_mode='HTML',
            reply_markup=_check_control_markup()
        )

        from monitor import search_min_price
        msg = query.message

        async def _edit_single_progress(done_steps, current_label):
            if context.bot_data.get('checkstop_pending'):
                return
            bar, pct = _make_progress_bar(done_steps, total_steps)
            try:
                await msg.edit_text(
                    f"💰 <b>Мін. прайс: {name}</b>\n\n"
                    f"🧩 Ключевые слова: {kw_list}\n\n"
                    f"{bar} {pct}%\n"
                    f"🔎 Проверяю: {done_steps}/{total_steps}\n"
                    f"🧭 Сейчас: {current_label}",
                    parse_mode='HTML',
                    reply_markup=_check_control_markup()
                )
            except Exception:
                pass

        async def _stop_single():
            context.bot_data['cancel_current_check'] = False
            bot_mode['mode'] = 'standard'
            bot_mode['params'] = {}
            bot_mode['started_at'] = None
            await msg.edit_text(
                f"⏹ <b>Мин. прайс остановлен</b>\n\n{name}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data="set:check:minprice:pve" if item_type in ('pveconfirmed', 'pveunconfirmed', 'ed') else "set:check:minprice:skins")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
                ])
            )

        if context.bot_data.get('cancel_current_check'):
            await _stop_single()
            return

        any_results = await _run_minprice_search_with_watchdog(
            context,
            f"{name} • без PVE",
            lambda: search_min_price(all_keywords, require_pve=False),
            heartbeat_callback=lambda elapsed: _edit_single_progress(0, f"без PVE ({elapsed}с)")
        )
        if dual_mode:
            await _edit_single_progress(1, "с PVE")
            if context.bot_data.get('cancel_current_check'):
                await _stop_single()
                return
            pve_results = await _run_minprice_search_with_watchdog(
                context,
                f"{name} • с PVE",
                lambda: search_min_price(all_keywords, require_pve=True),
                heartbeat_callback=lambda elapsed: _edit_single_progress(1, f"с PVE ({elapsed}с)")
            )
        else:
            pve_results = []

        if dual_mode:
            record_price_snapshot('skin', item_id, name, 'any', any_results, source='single_minprice')
            record_price_snapshot('skin', item_id, name, 'pve', pve_results, source='single_minprice')
            _cache_minprice_top3(context, 'skin', item_id, name, any_results=any_results, pve_results=pve_results)
            any_best = any_results[0] if any_results else None
            pve_best = pve_results[0] if pve_results else None
            no_any = _same_min_offer(any_best, pve_best) or not any_results
            any_markup = "—" if no_any else _format_log_offer_v2(any_best)
            pve_markup = _format_log_offer_v2(pve_best)
            text = (
                f"💰 <b>Мін. прайс: {name}</b>\n\n"
                f"🪄 Без PVE: <b>{any_markup}</b>\n"
                f"🔒 С PVE: <b>{pve_markup}</b>"
            )
            history_button = InlineKeyboardButton("📈 История цен", callback_data=f"set:hist:skin:{item_id}:all:0:ms")
            show3_button = InlineKeyboardButton("📋 Показать ещё", callback_data=f"set:minprice:show3:skin:{item_id}")
        else:
            record_price_snapshot(history_item_type, item_id, name, history_mode, any_results, source='single_minprice')
            _cache_minprice_top3(context, item_type, item_id, name, any_results=any_results, pve_results=[])
            price_markup = _format_log_offer_v2(any_results[0] if any_results else None)
            text = (
                f"💰 <b>Мін. прайс: {name}</b>\n\n"
                f"📉 Мин. цена: <b>{price_markup}</b>"
            )
            history_button = InlineKeyboardButton("📈 История цен", callback_data=f"set:hist:{history_item_type}:{item_id}:{history_mode}:0:mp")
            show3_button = InlineKeyboardButton("📋 Показать ещё", callback_data=f"set:minprice:show3:{item_type}:{item_id}")

        repeat_cb = (
            "set:minprice:pveconfirmed" if item_type == 'pveconfirmed' else
            ("set:minprice:pveunconfirmed" if item_type == 'pveunconfirmed' else
             (f"set:minprice:ed:{item_id}" if item_type == 'ed' else f"set:minprice:skin:{item_id}"))
        )
        if origin_kind == 'skinslist' and origin_page.isdigit():
            back_cb = f"set:skins:list:{origin_page}"
            repeat_cb = (
                f"set:minprice:skin:{item_id}:skinslist:{origin_page}"
                if item_type == 'skin' else repeat_cb
            )
        elif origin_kind == 'pvlist' and origin_page.isdigit():
            back_cb = f"set:skins:pvelist:{origin_page}"
            if item_type == 'pveconfirmed':
                repeat_cb = f"set:minprice:pveconfirmed:pvlist:{origin_page}"
            elif item_type == 'pveunconfirmed':
                repeat_cb = f"set:minprice:pveunconfirmed:pvlist:{origin_page}"
            elif item_type == 'ed':
                repeat_cb = f"set:minprice:ed:{item_id}:pvlist:{origin_page}"
        else:
            back_cb = "set:check:minprice:pve" if item_type in ('pveconfirmed', 'pveunconfirmed', 'ed') else "set:check:minprice:skins"
        keyboard = [
            [InlineKeyboardButton("🔄 Повторить", callback_data=repeat_cb)],
            [show3_button],
            [history_button],
            [InlineKeyboardButton("🔙 Назад", callback_data=back_cb), InlineKeyboardButton("🏠 Главное меню", callback_data="set:main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML', disable_web_page_preview=True)
        bot_mode['mode'] = 'standard'
        bot_mode['params'] = {}
        bot_mode['started_at'] = None

    elif parts[1] == 'skins':
        action = parts[2]

        if action == 'menu':
            await _show_skins_menu(query, context)

        if action == 'list':
            page = int(parts[3]) if len(parts) > 3 else 0
            await _show_skins_list(query, context, page)

        elif action == 'pvelist':
            page = int(parts[3]) if len(parts) > 3 else 0
            await _show_pve_positions_list(query, context, page)

        elif action == 'pvdetail':
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await _show_confirmed_pve_detail(query, context, page)

        elif action == 'pvtoggle':
            ret_marker = parts[3] if len(parts) > 3 else '0'
            config.confirmed_pve_enabled = not config.confirmed_pve_enabled
            await query.answer(
                f"{'✅' if config.confirmed_pve_enabled else '⛔'} {_confirmed_pve_title()}: "
                f"{'включен' if config.confirmed_pve_enabled else 'выключен'}"
            )
            if ret_marker == 'd':
                detail_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else context.user_data.get('skins_last_page', 0)
                await _show_confirmed_pve_detail(query, context, detail_page)
            else:
                await _show_pve_positions_list(query, context, int(ret_marker) if ret_marker.isdigit() else 0)

        elif action == 'pvprice':
            ret_marker = parts[3] if len(parts) > 3 else '0'
            context.user_data['input_state'] = INPUT_CONFIRMED_PVE_PRICE
            context.user_data['editing_confirmed_pve'] = True
            if ret_marker == 'detail':
                detail_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else context.user_data.get('skins_last_page', 0)
                _set_input_return(
                    context,
                    f"set:skins:pvdetail:{detail_page}",
                    f"🔙 Назад к {_confirmed_pve_title()}",
                    extra_buttons=[("🔒 К PVE", f"set:skins:pvelist:{detail_page}")]
                )
            else:
                ret_page = int(ret_marker) if ret_marker.isdigit() else 0
                _set_input_return(context, f"set:skins:pvelist:{ret_page}", "🔙 К PVE")
            await query.edit_message_text(
                f"💸 <b>Введите новую цену для {_confirmed_pve_title()}</b>\n\n"
                f"Текущая: {config.confirmed_pve_price}₽\n"
                f"Отправьте число или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, "set:skins:pvelist:0", "🔙 К PVE")
            )

        elif action == 'toggle':
            skin_id = parts[3]
            new_state = config.toggle_skin(skin_id)
            if new_state is not None:
                name = skin_id.replace('_', ' ').title()
                await query.answer(f"{'✅' if new_state else '⛔'} {name}: {'включён' if new_state else 'выключен'}")
            if len(parts) > 4 and parts[4] == 'd':
                await _show_skin_detail(query, context, skin_id, context.user_data.get('skins_last_page', 0))
            else:
                await _show_skins_list(query, context, int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0)

        elif action == 'detail':
            detail_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else context.user_data.get('skins_last_page', 0)
            await _show_skin_detail(query, context, parts[3], detail_page)

        elif action == 'pvereq':
            skin_id = parts[3]
            ret_marker = parts[4] if len(parts) > 4 else '0'
            new_state = config.toggle_skin_pve(skin_id)
            if new_state is not None:
                name = skin_id.replace('_', ' ').title()
                await query.answer(f"{'🔒' if new_state else '🪄'} {name}: {'только PVE' if new_state else 'без фильтра PVE'}")
            if ret_marker == 'd':
                await _show_skin_detail(query, context, skin_id, context.user_data.get('skins_last_page', 0))
            else:
                await _show_skins_list(query, context, int(ret_marker) if ret_marker.isdigit() else 0)

        elif action == 'pveall':
            ret_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            all_skins = config.get_all_skins()
            new_val = not all(s.get('require_pve', False) for s in all_skins.values())
            for sid in all_skins:
                all_skins[sid]['require_pve'] = new_val
            config.save()
            await query.answer("✅ Для всех включён PVE" if new_val else "🪄 Для всех отключён PVE")
            await _show_skins_list(query, context, ret_page)

        elif action == 'alltoggle':
            ret_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            all_skins = config.get_all_skins()
            new_val = not all(s.get('enabled', True) for s in all_skins.values())
            for sid in all_skins:
                all_skins[sid]['enabled'] = new_val
            config.save()
            await query.answer("✅ Все скины включены" if new_val else "⛔ Все скины выключены")
            await _show_skins_list(query, context, ret_page)

        elif action == 'edtoggle':
            eid = parts[3]
            ed = config.get_edition(eid)
            if ed:
                ed['enabled'] = not ed.get('enabled', True)
                config.save()
                name = eid.replace('_', ' ').title()
                await query.answer(f"{'✅' if ed['enabled'] else '⛔'} {name}: {'включено' if ed['enabled'] else 'выключено'}")
            await _show_pve_positions_list(query, context, int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0)

        elif action == 'edprice':
            eid = parts[3]
            ret_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            context.user_data['editing_edition_id'] = eid
            context.user_data['input_state'] = INPUT_RECHECK_ED_PRICE
            _set_input_return(context, f"set:skins:pvelist:{ret_page}", "🔙 К PVE")
            ed = config.get_edition(eid) or {}
            name = eid.replace('_', ' ').title()
            await query.edit_message_text(
                f"💸 <b>Введите новую цену для {name}</b>\n\nТекущая: {ed.get('price', 0)}₽\nОтправьте число или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, f"set:skins:pvelist:{ret_page}", "🔙 К PVE")
            )

        elif action == 'edkw':
            eid = parts[3]
            ed = config.get_edition(eid)
            if not ed:
                await query.answer("Издание не найдено")
                return
            name = eid.replace('_', ' ').title()
            keywords = ed.get('keywords', [])
            text = f"✏️ <b>Ключевые слова: 🏆 {name}</b>\n\nВсего: {len(keywords)}"
            keyboard = []
            for i, kw in enumerate(keywords):
                row = [InlineKeyboardButton(f"{i + 1}. {kw}", callback_data='set:noop')]
                if len(keywords) > 1:
                    row.append(InlineKeyboardButton("🗑", callback_data=f"set:skins:edkwdel:{eid}:{i}"))
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("📈 История цен", callback_data=f"set:hist:edition:{eid}:all:0:ek")])
            keyboard.append([InlineKeyboardButton("➕ Добавить слово", callback_data=f"set:skins:edkwadd:{eid}")])
            keyboard.append([InlineKeyboardButton("🔙 К PVE", callback_data='set:skins:pvelist:0'), InlineKeyboardButton("🏠 Главное меню", callback_data='set:main')])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        elif action == 'edkwdel':
            eid = parts[3]
            kw_index = int(parts[4]) if len(parts) > 4 else -1
            ed = config.get_edition(eid)
            if ed:
                keywords = ed.get('keywords', [])
                if 0 <= kw_index < len(keywords) and len(keywords) > 1:
                    removed = keywords.pop(kw_index)
                    config.save()
                    await query.answer(f"🗑 Удалено: {removed}")
                else:
                    await query.answer("Нельзя удалить последнее слово", show_alert=True)
            await handle_settings_callback(update, context)
            return

        elif action == 'edkwadd':
            eid = parts[3]
            context.user_data['editing_edition_id'] = eid
            context.user_data['editing_skin_id'] = f'ed:{eid}'
            context.user_data['input_state'] = INPUT_SKIN_KEYWORDS
            _set_input_return(context, f"set:skins:edkw:{eid}", "🔙 К словам издания")
            name = eid.replace('_', ' ').title()
            await query.edit_message_text(
                f"✏️ <b>Добавить слово для издания {name}</b>\n\nОтправьте новое слово или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, f"set:skins:edkw:{eid}", "🔙 К словам издания")
            )

        elif action == 'price':
            skin_id = parts[3]
            context.user_data['editing_skin_id'] = skin_id
            context.user_data['input_state'] = INPUT_SKIN_PRICE
            name = skin_id.replace('_', ' ').title()
            skin = config.get_skin(skin_id) or {}
            if len(parts) > 4 and parts[4] == 'detail':
                detail_page = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else context.user_data.get('skins_last_page', 0)
                context.user_data['skins_last_page'] = detail_page
                list_cb = f"set:skins:list:{detail_page}"
                _set_input_return(
                    context,
                    f"set:skins:detail:{skin_id}:{detail_page}",
                    f"🔙 Назад к {name}",
                    extra_buttons=[("📋 К списку", list_cb)]
                )
                back_cb, back_label = f"set:skins:detail:{skin_id}:{detail_page}", f"🔙 Назад к {name}"
            else:
                ret_page = int(parts[5]) if len(parts) > 5 and parts[4] == 'list' and parts[5].isdigit() else 0
                context.user_data['skins_last_page'] = ret_page
                _set_input_return(context, f"set:skins:list:{ret_page}", "🔙 К списку")
                back_cb, back_label = f"set:skins:list:{ret_page}", "🔙 К списку"
            await query.edit_message_text(
                f"💸 <b>Введите новую цену для {name}</b>\n\nТекущая: {skin.get('price', 0)}₽\nОтправьте число или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, back_cb, back_label)
            )

        elif action == 'kw':
            await _show_skin_keywords(query, context, parts[3])

        elif action == 'kwdel':
            skin_id = parts[3]
            removed = config.remove_skin_keyword(skin_id, int(parts[4]) if len(parts) > 4 else -1)
            if removed:
                await query.answer(f"🗑 Удалено: {removed}")
            else:
                await query.answer("Нельзя удалить последнее слово", show_alert=True)
            await _show_skin_keywords(query, context, skin_id)

        elif action == 'kwadd':
            skin_id = parts[3]
            context.user_data['editing_skin_id'] = skin_id
            context.user_data['input_state'] = INPUT_SKIN_KEYWORDS
            _set_input_return(context, f"set:skins:kw:{skin_id}", "🔙 К словам скина")
            name = skin_id.replace('_', ' ').title()
            await query.edit_message_text(
                f"✏️ <b>Добавить ключевое слово в {name}</b>\n\nОтправьте новое слово или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, f"set:skins:kw:{skin_id}", "🔙 К словам скина")
            )

        elif action == 'del':
            skin_id = parts[3]
            name = skin_id.replace('_', ' ').title()
            keyboard = [[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"set:skins:delok:{skin_id}"), InlineKeyboardButton("🔙 Нет", callback_data=f"set:skins:detail:{skin_id}")]]
            await query.edit_message_text(f"🗑 <b>Удалить {name}?</b>\n\nЭто действие необратимо.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        elif action == 'delok':
            skin_id = parts[3]
            name = skin_id.replace('_', ' ').title()
            if config.delete_skin(skin_id):
                await query.answer(f"🗑 {name} удалён", show_alert=True)
            await _show_skins_list(query, context, 0)

        elif action == 'add':
            context.user_data['input_state'] = INPUT_NEW_SKIN_ID
            _set_input_return(context, 'set:skins:list:0', '🔙 К списку')
            await query.edit_message_text(
                "➕ <b>Добавление нового скина</b>\n\n"
                "Шаг 1/3: отправьте <b>ID</b> скина (eng, можно _, без пробелов)\n"
                "Пример: <code>galaxy_scout</code>\n\n"
                "Или нажмите кнопку ниже:",
                parse_mode='HTML',
                reply_markup=_get_input_return_markup(context, 'set:skins:list:0', '🔙 К списку')
            )

    # === PVE ===
    elif parts[1] == 'pve':
        action = parts[2]
        if action == 'menu':
            await _show_pve_menu(query, context)
        elif action in ('conf', 'unconf'):
            page = int(parts[3]) if len(parts) > 3 else 0
            await _show_pve_keywords(query, context, action, page)
        elif action == 'rm':
            pve_type = parts[3]
            idx = int(parts[4])
            page = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
            keywords = config.get_confirmed_pve() if pve_type == 'conf' else config.get_unconfirmed_pve()
            if 0 <= idx < len(keywords):
                kw = keywords[idx]
                if pve_type == 'conf':
                    config.remove_confirmed_pve(kw)
                else:
                    config.remove_unconfirmed_pve(kw)
                await query.answer(f"Удалено: {kw}")
            await _show_pve_keywords(query, context, pve_type, page)
        elif action == 'add':
            pve_type = parts[3] if len(parts) > 3 else 'conf'
            context.user_data['input_state'] = INPUT_PVE_KEYWORD
            context.user_data['pve_type'] = pve_type
            label = 'подтвержденное' if pve_type == 'conf' else 'неподтвержденное'
            await query.edit_message_text(
                f"➕ <b>Добавить {label} PVE-слово</b>\n\nОтправьте слово или /cancel:",
                parse_mode='HTML'
            )
        elif action == 'bonus':
            context.user_data['input_state'] = INPUT_PVE_BONUS
            _set_input_return(context, 'set:pve:menu', '🔙 К PVE')
            await query.edit_message_text(f"💸 <b>PVE бонус</b>\n\nТекущий: {config.pve_bonus}₽\n\nОтправьте новое значение или /cancel:", parse_mode='HTML')

    # === Prices ===
    elif parts[1] == 'prices':
        if parts[2] == 'menu':
            await _show_prices_menu(query, context)

    # === Numeric settings ===
    elif parts[1] == 'num':
        param = parts[2]
        if param == 'max_price':
            context.user_data['input_state'] = INPUT_MAX_PRICE
            _set_input_return(context, 'set:prices:menu', '🔙 К ценам')
            await query.edit_message_text(f"💸 <b>Макс. цена</b>\n\nТекущая: {config.max_price}₽\n\nОтправьте число или /cancel:", parse_mode='HTML')
        elif param == 'confirmed_pve_price':
            context.user_data['input_state'] = INPUT_CONFIRMED_PVE_PRICE
            _set_input_return(context, 'set:prices:menu', '🔙 К ценам')
            await query.edit_message_text(
                f"🛡 <b>Цена для подтверждённого PVE</b>\n\n"
                f"Текущая: {config.confirmed_pve_price}₽\n\n"
                "Отправьте число или /cancel:",
                parse_mode='HTML'
            )
        elif param == 'pve_bonus':
            context.user_data['input_state'] = INPUT_PVE_BONUS
            _set_input_return(context, 'set:prices:menu', '🔙 К ценам')
            await query.edit_message_text(f"💸 <b>PVE бонус</b>\n\nТекущий: {config.pve_bonus}₽\n\nОтправьте число или /cancel:", parse_mode='HTML')
        elif param == 'interval':
            context.user_data['input_state'] = INPUT_CHECK_INTERVAL
            _set_input_return(context, 'set:prices:menu', '🔙 К ценам')
            await query.edit_message_text(f"⏱ <b>Интервал проверки</b>\n\nТекущий: {config.check_interval} сек\n\nОтправьте число или /cancel:", parse_mode='HTML')
        elif param == 'delay':
            context.user_data['input_state'] = INPUT_DELAY_MIN
            _set_input_return(context, 'set:prices:menu', '🔙 К ценам')
            await query.edit_message_text(f"🐢 <b>Задержка запросов</b>\n\nТекущая: {config.request_delay_min}-{config.request_delay_max} сек\n\nОтправьте минимальную задержку или /cancel:", parse_mode='HTML')

    # === Filters ===
    elif parts[1] == 'filters':
        action = parts[2] if len(parts) > 2 else 'menu'
        if action == 'menu':
            exclude_count = len(config.get_exclude_keywords())
            conf_count = len(config.get_confirmed_pve())
            unconf_count = len(config.get_unconfirmed_pve())
            banned_count = len(_get_banned_ids(context))
            text = (
                "🚫 <b>Фильтры</b>\n\n"
                f"📝 Фразы без почты: {exclude_count}\n"
                f"✅ PVE подтв.: {conf_count}\n"
                f"❓ PVE неподтв.: {unconf_count}\n\n"
                "Выберите раздел:"
            )
            keyboard = [
                [InlineKeyboardButton(f"📝 Фразы без почты ({exclude_count})", callback_data='set:filters:list:0')],
                [InlineKeyboardButton(f"✅ PVE подтв. ({conf_count})", callback_data='set:pve:conf:0')],
                [InlineKeyboardButton(f"❓ PVE неподтв. ({unconf_count})", callback_data='set:pve:unconf:0')],
                [InlineKeyboardButton('🏠 Главное меню', callback_data='set:main')],
            ]
            text += f"\n🚷 Забаненные лоты: {banned_count}"
            keyboard.insert(-1, [InlineKeyboardButton(f"🚷 Забаненные ({banned_count})", callback_data='set:ban:list:0')])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif action == 'list':
            page = int(parts[3]) if len(parts) > 3 else 0
            await _show_filters_list(query, context, page)
    elif parts[1] == 'ban':
        action = parts[2] if len(parts) > 2 else 'list'
        if action == 'list':
            page = int(parts[3]) if len(parts) > 3 else 0
            await _show_banned_list(query, context, page)
        elif action == 'add':
            context.user_data['input_state'] = INPUT_BANNED_LINK
            _set_input_return(context, 'set:ban:list:0', '🔙 К бан-листу')
            await query.edit_message_text(
                '🚷 <b>Добавить в бан</b>\n\nОтправьте ссылку на лот или ID лота.\n\nПример:\n<code>https://funpay.com/lots/offer?id=65475872</code>\n\nИли /cancel',
                parse_mode='HTML'
            )
        elif action == 'rm':
            offer_id = parts[3] if len(parts) > 3 else ''
            page = int(parts[4]) if len(parts) > 4 else 0
            banned_ids = _get_banned_ids(context)
            if offer_id in banned_ids:
                banned_ids.remove(offer_id)
                _save_banned_ids(context)
                await query.answer(f"Убран из бана: {offer_id}")
            else:
                await query.answer("Лот уже убран из бана")
            await _show_banned_list(query, context, page)
        elif action == 'clear':
            _get_banned_ids(context).clear()
            _save_banned_ids(context)
            await query.answer("Бан-лист очищен")
            await _show_banned_list(query, context, 0)
    elif parts[1] == 'filt':
        if parts[2] == 'rm':
            idx = int(parts[3])
            page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            keywords = config.get_exclude_keywords()
            if 0 <= idx < len(keywords):
                kw = keywords[idx]
                config.remove_exclude_keyword(kw)
                await query.answer(f"Удалено: {kw[:30]}")
            await _show_filters_list(query, context, page)
        elif parts[2] == 'add':
            context.user_data['input_state'] = INPUT_EXCLUDE_KEYWORD
            _set_input_return(context, 'set:filters:list:0', '🔙 К фильтрам')
            await query.edit_message_text(
                '➕ <b>Добавить фразу-фильтр</b>\n\nОтправьте фразу или /cancel:',
                parse_mode='HTML'
            )


# ==============================
# TEXT INPUT
# ==============================

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовый ввод пользователя в меню настроек."""
    if not _check_auth(update, context):
        return

    state = context.user_data.get('input_state')
    if state is None:
        return

    config = context.bot_data['config']
    text = update.message.text.strip()

    if text.lower() == '/cancel':
        context.user_data.pop('input_state', None)
        context.user_data.pop('editing_skin_id', None)
        context.user_data.pop('editing_edition_id', None)
        context.user_data.pop('pve_type', None)
        context.user_data.pop('new_skin_id', None)
        context.user_data.pop('new_skin_price', None)
        context.user_data.pop('recheck_rare_price', None)
        context.user_data.pop('recheck_direct_unconfirmed_pve', None)
        await update.message.reply_text('↩️ Отменено.', reply_markup=_pop_input_return_markup(context, 'set:main', '🔙 Назад'))
        return

    if state == INPUT_SKIN_PRICE:
        try:
            price = int(text)
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        skin_id = context.user_data.get('editing_skin_id')
        name = skin_id.replace('_', ' ').title()
        config.set_skin_price(skin_id, price)
        context.user_data.pop('input_state', None)
        context.user_data.pop('editing_skin_id', None)
        await update.message.reply_text(f'✅ Цена {name}: {price}₽', reply_markup=_pop_input_return_markup(context, 'set:skins:list:0', '🔙 К списку'))

    elif state == INPUT_SKIN_KEYWORDS:
        keyword = text.strip().lower()
        if not keyword:
            await update.message.reply_text('❌ Введите слово или /cancel:')
            return
        skin_id = context.user_data.get('editing_skin_id')
        context.user_data.pop('input_state', None)
        context.user_data.pop('editing_skin_id', None)
        if skin_id and skin_id.startswith('ed:'):
            eid = skin_id[3:]
            ed = config.get_edition(eid)
            if ed and keyword not in ed.get('keywords', []):
                ed.setdefault('keywords', []).append(keyword)
                config.save()
            await update.message.reply_text(f'✅ Слово сохранено: {keyword}', reply_markup=_pop_input_return_markup(context, f'set:skins:edkw:{eid}', '🔙 К словам издания'))
        else:
            config.add_skin_keyword(skin_id, keyword)
            await update.message.reply_text(f'✅ Слово сохранено: {keyword}', reply_markup=_pop_input_return_markup(context, f'set:skins:kw:{skin_id}', '🔙 К словам скина'))

    elif state == INPUT_NEW_SKIN_ID:
        skin_id = text.lower().replace(' ', '_')
        if config.get_skin(skin_id):
            await update.message.reply_text('❌ Такой ID уже существует. Введите другой или /cancel:')
            return
        context.user_data['new_skin_id'] = skin_id
        context.user_data['input_state'] = INPUT_NEW_SKIN_PRICE
        await update.message.reply_text(f'🆔 ID сохранён: <code>{skin_id}</code>\n\nШаг 2/3: введите цену в ₽', parse_mode='HTML')

    elif state == INPUT_NEW_SKIN_PRICE:
        try:
            price = int(text)
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        context.user_data['new_skin_price'] = price
        context.user_data['input_state'] = INPUT_NEW_SKIN_KEYWORDS
        await update.message.reply_text(f'💸 Цена: {price}₽\n\nШаг 3/3: введите ключевые слова через запятую')

    elif state == INPUT_NEW_SKIN_KEYWORDS:
        keywords = [kw.strip() for kw in text.split(',') if kw.strip()]
        if not keywords:
            await update.message.reply_text('❌ Введите хотя бы одно ключевое слово или /cancel:')
            return
        skin_id = context.user_data.get('new_skin_id')
        price = context.user_data.get('new_skin_price')
        config.add_skin(skin_id, keywords, price)
        context.user_data.pop('input_state', None)
        context.user_data.pop('new_skin_id', None)
        context.user_data.pop('new_skin_price', None)
        await update.message.reply_text('✅ Скин добавлен.', reply_markup=_pop_input_return_markup(context, 'set:skins:list:0', '🔙 К списку'))

    elif state == INPUT_PVE_KEYWORD:
        keyword = text.lower().strip()
        pve_type = context.user_data.get('pve_type', 'conf')
        if pve_type == 'conf':
            config.add_confirmed_pve(keyword)
        else:
            config.add_unconfirmed_pve(keyword)
        context.user_data.pop('input_state', None)
        context.user_data.pop('pve_type', None)
        await update.message.reply_text(f'✅ PVE-слово сохранено: {keyword}', reply_markup=_pop_input_return_markup(context, 'set:pve:menu', '🔙 К PVE'))

    elif state == INPUT_EXCLUDE_KEYWORD:
        config.add_exclude_keyword(text)
        context.user_data.pop('input_state', None)
        await update.message.reply_text(f'✅ Фраза добавлена: {text}', reply_markup=_pop_input_return_markup(context, 'set:filters:list:0', '🔙 К фильтрам'))

    elif state == INPUT_BANNED_LINK:
        offer_id = _extract_offer_id(text)
        if not offer_id:
            await update.message.reply_text('❌ Отправьте ссылку на лот FunPay или ID. Или /cancel:')
            return
        banned_ids = _get_banned_ids(context)
        already_banned = offer_id in banned_ids
        banned_ids.add(offer_id)
        _save_banned_ids(context)
        context.user_data.pop('input_state', None)
        await update.message.reply_text(
            (
                f'🚫 Лот добавлен в бан: {offer_id}'
                if not already_banned else
                f'🚫 Лот уже был в бане: {offer_id}'
            ),
            reply_markup=_pop_input_return_markup(context, 'set:ban:list:0', '🔙 К бан-листу')
        )

    elif state == INPUT_MAX_PRICE:
        try:
            val = int(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        config.max_price = val
        context.user_data.pop('input_state', None)
        await update.message.reply_text(f'✅ Макс. цена: {val}₽', reply_markup=_pop_input_return_markup(context, 'set:prices:menu', '🔙 К ценам'))

    elif state == INPUT_CONFIRMED_PVE_PRICE:
        try:
            val = int(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        context.user_data.pop('input_state', None)
        if context.user_data.pop('editing_confirmed_pve_custom', None):
            rc_pve = context.user_data.get('recheck_confirmed_pve', {
                'enabled': config.confirmed_pve_enabled,
                'price': config.confirmed_pve_price,
            })
            rc_pve['price'] = val
            context.user_data['recheck_confirmed_pve'] = rc_pve
            context.user_data['custom_recheck_mode'] = 'confirmed_pve'
            await update.message.reply_text(
                f'✅ Цена {_confirmed_pve_title()}: {val}₽',
                reply_markup=_pop_input_return_markup(context, 'set:recheck:cmpve', '🔙 Назад')
            )
        else:
            config.confirmed_pve_price = val
            context.user_data.pop('editing_confirmed_pve', None)
            await update.message.reply_text(
                f'✅ Цена подтверждённого PVE: {val}₽',
                reply_markup=_pop_input_return_markup(context, 'set:prices:menu', '🔙 К ценам')
            )

    elif state == INPUT_PVE_BONUS:
        try:
            val = int(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        config.pve_bonus = val
        context.user_data.pop('input_state', None)
        await update.message.reply_text(f'✅ PVE бонус: {val}₽', reply_markup=_pop_input_return_markup(context, 'set:prices:menu', '🔙 К ценам'))

    elif state == INPUT_CHECK_INTERVAL:
        try:
            val = int(text)
            if val < 30:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите число не меньше 30 или /cancel:')
            return
        config.check_interval = val
        context.user_data.pop('input_state', None)
        await update.message.reply_text(f'✅ Интервал проверки: {val} сек', reply_markup=_pop_input_return_markup(context, 'set:prices:menu', '🔙 К ценам'))

    elif state == INPUT_DELAY_MIN:
        try:
            val = int(text)
            if val < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите число не меньше 1 или /cancel:')
            return
        config.request_delay_min = val
        context.user_data['input_state'] = INPUT_DELAY_MAX
        await update.message.reply_text(f'✅ Мин. задержка: {val} сек\n\nТеперь введите максимальную задержку.')

    elif state == INPUT_DELAY_MAX:
        try:
            val = int(text)
            if val < config.request_delay_min:
                raise ValueError
        except ValueError:
            await update.message.reply_text(f'❌ Введите число не меньше {config.request_delay_min} или /cancel:')
            return
        config.request_delay_max = val
        context.user_data.pop('input_state', None)
        await update.message.reply_text(f'✅ Задержка: {config.request_delay_min}-{val} сек', reply_markup=_pop_input_return_markup(context, 'set:prices:menu', '🔙 К ценам'))

    elif state == INPUT_RECHECK_ED_PRICE:
        try:
            val = int(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        eid = context.user_data.pop('editing_edition_id', None)
        context.user_data.pop('input_state', None)
        if eid:
            ed = config.get_edition(eid)
            if ed:
                ed['price'] = val
                config.save()
        await update.message.reply_text(f'✅ Цена издания обновлена: {val}₽', reply_markup=_pop_input_return_markup(context, 'set:skins:list:0', '🔙 К списку'))

    elif state == INPUT_RECHECK_SKIN_PRICE:
        try:
            val = int(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или /cancel:')
            return
        skin_id = context.user_data.pop('editing_recheck_skin', None)
        context.user_data.pop('input_state', None)
        rc_skins = context.user_data.get('recheck_skins', {})
        if skin_id and skin_id in rc_skins:
            rc_skins[skin_id]['price'] = val
        await update.message.reply_text(f'✅ Цена сохранена: {val}₽')

    elif state == INPUT_RECHECK_RARE:
        try:
            val = int(text)
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите число не меньше 0 или /cancel:')
            return
        rc_skins = context.user_data.get('recheck_skins', {})
        if val > 0 and rc_skins:
            for s in rc_skins.values():
                if s['enabled']:
                    s['price'] = val
        context.user_data['recheck_rare_price'] = val
        custom_mode = context.user_data.get('custom_recheck_mode', 'list')
        if custom_mode in ('premium', 'list', 'confirmed_pve'):
            context.user_data.pop('input_state', None)
            await _launch_custom_recheck(update, context, config, pve_price=None)
        else:
            context.user_data.pop('input_state', None)
            await _launch_custom_recheck(update, context, config, pve_price=None)

    elif state == INPUT_RECHECK_PVE:
        try:
            val = int(text)
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text('❌ Введите положительное число или 0. Или /cancel:')
            return
        context.user_data.pop('input_state', None)

        if context.user_data.pop('recheck_direct_unconfirmed_pve', False):
            bot_mode = context.bot_data.get('bot_mode', {})
            process_fn = context.bot_data.get('process_offers')
            build_snapshot_fn = context.bot_data.get('build_recheck_snapshot')
            chat_id = _get_chat_id_from_context(context)
            bot_mode['mode'] = 'recheck_pve'
            bot_mode['params'] = {
                'restore_mode': config.search_mode,
                'display_mode': 'Перепроверка: Неподтв. PVE',
                'target_label': 'Неподтв. PVE',
                'run_snapshot': build_snapshot_fn(
                    config,
                    display_mode='Неподтв. PVE',
                    bot_mode_key='recheck_pve',
                    search_mode='skins_pve',
                    include_unconfirmed_pve=True,
                    max_price_override=val,
                    chat_id_value=chat_id,
                    log_view='pve',
                )
            }
            bot_mode['started_at'] = time.time()
            await update.message.reply_text(
                f'🔓 <b>Перепроверка: неподтв. PVE</b>\n\n💸 Макс. цена: {val}₽\n⏳ Поиск запущен...',
                parse_mode='HTML'
            )
            asyncio.create_task(_run_recheck_task(chat_id, context, process_fn, skip_seen=False, candidate_limit=None, include_unconfirmed_pve=True, max_price_override=val))
        else:
            await _show_check_menu_as_new_message(update, context)



# ==============================
# РЕГИСТРАЦИЯ ХАНДЛЕРОВ
# ==============================

def register_settings_handlers(application, config, chat_id_ref, seen_ids_ref, banned_ids_ref):
    """Регистрирует все хандлеры настроек в приложении."""
    # Сохраняем config в bot_data для доступа из хандлеров
    application.bot_data['config'] = config
    application.bot_data['authorized_chat_id'] = chat_id_ref
    application.bot_data['seen_ids'] = seen_ids_ref
    application.bot_data['banned_ids'] = banned_ids_ref
    application.bot_data['send_recheck_result_message'] = _send_recheck_result_message

    # Команда /settings
    application.add_handler(CommandHandler("settings", settings_command))

    # Callback handler для всех кнопок set:*
    application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r'^set:'))

    # Текстовый ввод (ловим текст когда есть активное input_state)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_input
    ), group=1)  # group=1 чтобы не конфликтовал с другими хандлерами
