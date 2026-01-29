import time
import logging
import re
import requests
from bs4 import BeautifulSoup
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
import random
import os
import sys
import argparse
import asyncio

# --- НАСТРОЙКИ ---
# Пробуем взять из переменных окружения (для GitHub Actions), иначе используем хардкод
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8198440154:AAGkC_qZklMca9PI9NlHoEJy5P1Wed92c1s')

# URL с фильтром "только продажа" (не аренда)
FUNPAY_URL = 'https://funpay.com/lots/248/?offer_type=sell'

# Ключевые слова для поиска STW/PVE аккаунтов
SEARCH_KEYWORDS = [
    'og stw', 'stw', 'pve', 'og pve', 'пве', 'старое пве', 
    'фарм вбаксов', 'old pve', 'save the world'
]

# КРАСНЫЕ ФЛАГИ - ВСЕ варианты фраз про отсутствие почты/перепривязки
EXCLUDE_KEYWORDS = [
    # === НЕТ ПОЧТЫ / ДОСТУПА К ПОЧТЕ ===
    'без почты', 'почты нет', 'почты нету', 'нет почты',
    'доступ без почты', 'доступа к почте нет', 'доступа к почте нету',
    'доступа к почте не', 'к почте доступа нет',
    'почта недоступна', 'почта не доступна', 'к почте нет доступа',
    'без доступа к почте', 'почту не отдаю', 'почту не даю',
    'почта не передается', 'почта не передаётся', 'почту не передаю',
    'no email', 'without email', 'без мыла',
    'email не передается', 'email не передаётся',
    'без емейла', 'емейл не даю', 'емейла нет',
    'mail не даю', 'mail не отдаю',
    'почта не включена', 'почта не входит',
    'доступ к почте не включен', 'доступ к почте не предоставляется',
    'почта остается у продавца', 'почта остаётся у продавца',
    'почта у продавца', 'почта моя',
    
    # === ТОЛЬКО ЭПИК (без почты) ===
    'только эпик', 'only epic', 'получаете только эпик',
    'только epic', 'передаю только эпик',
    'вы получаете только эпик', 'отдаю только эпик',
    'только логин эпик', 'только данные эпик',
    
    # === НЕТ ПЕРЕПРИВЯЗКИ / СМЕНЫ ДАННЫХ ===
    'нет перепривязки', 'нет перепревязки',
    'без перепривязки', 'без перепревязки',
    'перепривязка невозможна', 'перепревязка невозможна',
    'нельзя перепривязать', 'нельзя перепревязать',
    'перепривязки нет', 'перепревязки нет',
    'не перепривязать', 'не перепревязать',
    'перепривязка недоступна', 'перепревязка недоступна',
    'нет смены данных', 'смена данных невозможна',
    'нельзя сменить данные', 'данные не меняются',
    'смена невозможна', 'без смены данных',
    'смены данных нет', 'данные сменить нельзя',
    'нет возможности сменить', 'невозможно сменить',
    
    # === АРЕНДА ===
    'аренда', 'rent', 'в аренду',
]

# Максимальная цена в рублях
MAX_PRICE = 1000

# Интервал проверки (секунды)
CHECK_INTERVAL = 120 

# Задержка между запросами к отдельным страницам (секунды)
REQUEST_DELAY_MIN = 3
REQUEST_DELAY_MAX = 7

CHAT_ID_FILE = 'chat_id.txt'
SEEN_IDS_FILE = 'seen_ids.txt'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

# Глобальные переменные
seen_ids = set()
chat_id = os.environ.get('TELEGRAM_CHAT_ID') # Пробуем взять из ENV

def load_chat_id():
    global chat_id
    if chat_id: return # Если уже есть из ENV
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

def load_seen_ids():
    global seen_ids
    try:
        with open(SEEN_IDS_FILE, 'r') as f:
            seen_ids = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        seen_ids = set()

def save_seen_id(offer_id):
    with open(SEEN_IDS_FILE, 'a') as f:
        f.write(offer_id + '\n')

def clear_seen_ids():
    """Очищает список просмотренных ID"""
    global seen_ids
    seen_ids = set()
    with open(SEEN_IDS_FILE, 'w') as f:
        f.write('')

def parse_price(price_text):
    """Извлекает числовое значение цены из текста"""
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

def get_random_user_agent():
    """Возвращает случайный User-Agent для обхода блокировок"""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    return random.choice(user_agents)

def get_listings():
    """Получает список товаров с главной страницы"""
    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Cookie': 'cy=rub'
    }
    try:
        response = requests.get(FUNPAY_URL, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('a', class_='tc-item')
        return items
    except Exception as e:
        logger.error(f"Ошибка при запросе к сайту: {e}")
        return []

def get_offer_details(offer_url):
    """Получает детальную информацию о предложении (описание, рейтинг)"""
    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Cookie': 'cy=rub',
        'Referer': 'https://funpay.com/'
    }
    
    try:
        # Случайная задержка перед запросом
        delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
        time.sleep(delay)
        
        response = requests.get(offer_url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Получаем полное описание - ищем разные варианты блоков
        full_description = ""
        
        # Способ 1: Ищем секцию "ПОДРОБНОЕ ОПИСАНИЕ" по тексту заголовка
        all_text_blocks = soup.find_all(['div', 'p', 'span'])
        for block in all_text_blocks:
            text = block.get_text(strip=True)
            if 'ПОДРОБНОЕ ОПИСАНИЕ' in text.upper() or 'КРАТКОЕ ОПИСАНИЕ' in text.upper():
                # Берём родительский контейнер
                parent = block.find_parent('div')
                if parent:
                    full_description = parent.get_text(separator=' ', strip=True)
                    break
        
        # Способ 2: Ищем по классам
        if not full_description:
            desc_block = (
                soup.find('div', class_='offer-description') or 
                soup.find('div', class_='lot-description') or
                soup.find('div', class_='param-item') or
                soup.find('div', class_='lot-info')
            )
            if desc_block:
                full_description = desc_block.get_text(separator=' ', strip=True)
        
        # Способ 3: Берём весь текст со страницы (крайний случай)
        if not full_description:
            # Ищем основной контент страницы
            main_content = soup.find('div', class_='content') or soup.find('main') or soup.find('body')
            if main_content:
                full_description = main_content.get_text(separator=' ', strip=True)

        
        # Получаем рейтинг продавца - ПОЛНАЯ ИНФОРМАЦИЯ
        rating_score = None  # Оценка (4.9, 5.0 и т.д.)
        reviews_count = None  # Количество отзывов
        reviews_period = None  # Период (за 4 года и т.д.)
        
        # Ищем информацию о продавце на странице
        page_text = soup.get_text()
        
        # Ищем оценку в формате "X.X из 5" или "X из 5"
        rating_match = re.search(r'(\d+[.,]?\d*)\s*из\s*5', page_text)
        if rating_match:
            try:
                rating_score = float(rating_match.group(1).replace(',', '.'))
            except:
                pass
        
        # Ищем количество отзывов и период
        reviews_match = re.search(r'(\d+)\s*(?:отзыв\w*)\s*(?:за\s*(.+?(?:год|лет|года|месяц\w*|недел\w*|дн\w*)))?', page_text, re.IGNORECASE)
        if reviews_match:
            reviews_count = int(reviews_match.group(1))
            if reviews_match.group(2):
                reviews_period = reviews_match.group(2).strip()
        
        # Также ищем через атрибуты элементов
        rating_elem = soup.find('span', class_='rating-mini-stars') or soup.find('div', class_='rating-stars')
        if rating_elem and not rating_score:
            data_rating = rating_elem.get('data-rating')
            if data_rating:
                try:
                    rating_score = float(data_rating)
                except:
                    pass
        
        # Формируем текст рейтинга
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

async def process_offers(bot_instance=None, context=None, skip_seen=True):
    """Основная функция обработки предложений. Может работать с bot или context."""
    global seen_ids, chat_id
    
    if not chat_id:
        logger.info("Chat ID не найден. Запустите /start в боте.")
        return 0

    logger.info("Проверка новых предложений...")
    listings = get_listings()
    
    # Собираем кандидатов для проверки
    candidates = []
    
    for item in listings:
        href = item.get('href')
        if not href:
            continue
        if href.startswith('/'):
            href = f"https://funpay.com{href}"
        
        # Извлекаем ID из URL
        offer_id_match = re.search(r'id=(\d+)', href)
        if not offer_id_match:
            continue
        offer_id = offer_id_match.group(1)
        
        # Пропускаем уже просмотренные (если не режим recheck)
        if skip_seen and offer_id in seen_ids:
            continue
        
        # Получаем базовую информацию с главной страницы
        desc_div = item.find('div', class_='tc-desc-text')
        price_div = item.find('div', class_='tc-price')
        user_div = item.find('div', class_='media-user-name')

        short_description = desc_div.get_text(strip=True) if desc_div else ""
        price_text = price_div.get_text(strip=True) if price_div else "Нет цены"
        user = user_div.get_text(strip=True) if user_div else "Неизвестный"
        
        short_desc_lower = short_description.lower()
        
        # Быстрая проверка на аренду
        if 'аренда' in short_desc_lower and 'продажа' not in short_desc_lower:
            if skip_seen:
                seen_ids.add(offer_id)
                save_seen_id(offer_id)
            continue
        
        # Проверка ключевых слов STW/PVE на главной
        found_keyword = False
        matched_keyword = ""
        for keyword in SEARCH_KEYWORDS:
            if keyword.lower() in short_desc_lower:
                found_keyword = True
                matched_keyword = keyword
                break
        
        if not found_keyword:
            if skip_seen:
                seen_ids.add(offer_id)
                save_seen_id(offer_id)
            continue
        
        # Проверка цены
        price_value = parse_price(price_text)
        if price_value is None or price_value > MAX_PRICE:
            if skip_seen:
                seen_ids.add(offer_id)
                save_seen_id(offer_id)
            continue
        
        # Добавляем в кандидаты для детальной проверки
        candidates.append({
            'offer_id': offer_id,
            'href': href,
            'short_description': short_description,
            'price_text': price_text,
            'price_value': price_value,
            'user': user,
            'matched_keyword': matched_keyword
        })
    
    logger.info(f"Найдено {len(candidates)} кандидатов для проверки")
    
    sent_count = 0
    
    # Проверяем каждого кандидата детально (с задержкой)
    # Для режима Cron (один запуск) проверяем всех кандидатов, но не более 10 за раз чтобы не висеть вечно
    limit = 10 
    
    for candidate in candidates[:limit]:  
        offer_id = candidate['offer_id']
        href = candidate['href']
        
        # Помечаем как просмотренный (только если не recheck)
        if skip_seen:
            seen_ids.add(offer_id)
            save_seen_id(offer_id)
        
        logger.info(f"Загружаю детали: {href}")
        
        # Получаем полное описание и рейтинг
        full_description, rating_text = get_offer_details(href)
        
        if full_description is None:
            logger.warning(f"Не удалось загрузить детали для {href}")
            continue
        
        full_desc_lower = full_description.lower()
        
        # Проверка КРАСНЫХ ФЛАГОВ в полном описании
        excluded = False
        excluded_reason = ""
        for exclude_word in EXCLUDE_KEYWORDS:
            if exclude_word.lower() in full_desc_lower:
                logger.info(f"🚫 Исключено ('{exclude_word}'): {candidate['short_description'][:40]}...")
                excluded = True
                excluded_reason = exclude_word
                break
        
        if excluded:
            continue
        
        # Всё проверки пройдены - отправляем уведомление!
        rating_emoji = "⭐" if "из 5" in rating_text else "❓"
        
        # Используем короткое описание с главной страницы (название)
        title = candidate['short_description']
        
        msg = (
            f"🔔 <b>Найден STW/PVE аккаунт до {MAX_PRICE}₽!</b>\n\n"
            f"🎮 <b>Тип:</b> {candidate['matched_keyword'].upper()}\n"
            f"📌 <b>Название:</b> {title}\n"
            f"💰 <b>Цена:</b> {candidate['price_text']}\n"
            f"👤 <b>Продавец:</b> {candidate['user']}\n"
            f"{rating_emoji} <b>Рейтинг:</b> {rating_text}\n"
            f"🔗 <a href='{href}'>Ссылка на товар</a>"
        )
        
        try:
            if context:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
            elif bot_instance:
                await bot_instance.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                
            logger.info(f"✅ Отправлено: {candidate['matched_keyword']} - {candidate['price_value']}₽ - {rating_text}")
            sent_count += 1
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
    
    return sent_count

async def check_funpay_job(context: ContextTypes.DEFAULT_TYPE):
    """Задача для JobQueue"""
    await process_offers(context=context, skip_seen=True)

async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /recheck - проверяет ВСЕ заново, включая уже просмотренные"""
    global chat_id
    user_chat_id = update.effective_chat.id
    
    if str(user_chat_id) != str(chat_id):
        await update.message.reply_text("❌ Вы не авторизованы. Напишите /start")
        return
    
    await update.message.reply_text(
        "🔄 Начинаю полную перепроверку всех предложений...\n"
        "⚠️ Это может занять несколько минут из-за задержек между запросами."
    )
    
    sent_count = await process_offers(context=context, skip_seen=False)
    
    await update.message.reply_text(
        f"✅ Перепроверка завершена!\n"
        f"📨 Отправлено {sent_count} подходящих предложений."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_chat_id = update.effective_chat.id
    save_chat_id(user_chat_id)
    await update.message.reply_text(
        f"✅ Бот активирован! Ваш ID: {user_chat_id}.\n\n"
        f"🔍 Ищу аккаунты Fortnite с STW/PVE\n\n"
        f"🚫 Исключаю {len(EXCLUDE_KEYWORDS)} фраз про отсутствие почты\n"
        f"📦 Только ПРОДАЖА (не аренда)\n"
        f"💰 Максимальная цена: {MAX_PRICE}₽\n"
        f"⭐ Показываю оценку и количество отзывов\n"
        f"⏱ Проверка каждые {CHECK_INTERVAL} секунд.\n\n"
        f"📋 <b>Команды:</b>\n"
        f"/recheck - перепроверить ВСЕ заново\n\n"
        f"⚠️ Задержка {REQUEST_DELAY_MIN}-{REQUEST_DELAY_MAX} сек между запросами для безопасности.",
        parse_mode='HTML'
    )
    logger.info(f"Пользователь зарегистрирован: {user_chat_id}")

async def run_once():
    """Запуск один раз и выход (для GitHub Actions / Cron)"""
    load_chat_id()
    load_seen_ids()
    
    if not chat_id:
        print("ОШИБКА: Chat ID не найден. Установите переменную окружения TELEGRAM_CHAT_ID")
        return

    print("--- Запуск в режиме ONE-SHOT (GitHub Actions) ---")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await process_offers(bot_instance=bot, skip_seen=True)
    print("--- Готово ---")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Запустить один раз и выйти')
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
        return

    load_chat_id()
    load_seen_ids()
    
    print("--- FunPay Monitor Bot ---")
    print(f"Ищу: {', '.join(SEARCH_KEYWORDS)}")
    print(f"Исключаю: {len(EXCLUDE_KEYWORDS)} фраз-красных флагов")
    print(f"Макс. цена: {MAX_PRICE}₽")
    print(f"Уже просмотрено: {len(seen_ids)} товаров")
    print("Запустите бота и напишите ему /start в Telegram.")
    print("Команда /recheck - перепроверить всё заново")
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("recheck", recheck))
    
    job_queue = application.job_queue
    job_queue.run_repeating(check_funpay_job, interval=CHECK_INTERVAL, first=10)
    
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
