"""
ConfigManager — загрузка/сохранение настроек бота из config.json.
Все параметры хранятся в JSON-файле и могут изменяться через Telegram-меню.
"""
import json
import os
import copy
import logging

logger = logging.getLogger(__name__)

# === ДЕФОЛТНЫЕ ЗНАЧЕНИЯ (используются при первом запуске) ===

DEFAULT_RARE_SKINS = {
    'royale_bomber': {
        'enabled': True,
        'keywords': ['королевский пилот', 'пилот', 'royale bomber', 'royalebomber'],
        'price': 1800
    },
    'eon': {
        'enabled': True,
        'keywords': ['вечность', 'эон', 'еон', 'eon'],
        'price': 1500
    },
    'double_helix': {
        'enabled': True,
        'keywords': ['double helix', 'doublehelix', 'helix', 'хеликс', 'дабл хеликс', 'экстерминатор'],
        'price': 1800
    },
    'dark_vertex': {
        'enabled': True,
        'keywords': ['тёмный вертекс', 'темный вертекс', 'dark vertex', 'darkvertex', 'черный вертекс', 'чёрный вертекс'],
        'price': 2200
    },
    'neo_versa': {
        'enabled': True,
        'keywords': ['neo versa', 'neoversa', 'нео верса', 'верса', 'амплитуда будущего', 'амплитуда'],
        'price': 1500
    },
    'rogue_spider_knight': {
        'enabled': True,
        'keywords': ['ядовитый арахнид', 'rogue spider knight', 'roguespiderknight', 'spiderknight', 'ядовитый арахнит'],
        'price': 1500
    },
    'stealth_reflex': {
        'enabled': True,
        'keywords': ['stealth reflex', 'reflex', 'инстинкт', 'скрытый инстинкт'],
        'price': 2000
    },
    'surf_strider': {
        'enabled': True,
        'keywords': ['волнолом', 'surf strider', 'surfstrider'],
        'price': 1500
    },
    'wildcat': {
        'enabled': True,
        'keywords': ['wildcat', 'wild cat', 'дикая кошка'],
        'price': 2500
    },
    'dark_skully': {
        'enabled': True,
        'keywords': ['тёмное сердечко', 'темное сердечко', 'темное сердце', 'тёмное сердце', 'dark skully'],
        'price': 1800
    },
    'huntmaster_saber': {
        'enabled': True,
        'keywords': ['главный охотник', 'huntmaster saber'],
        'price': 1500
    },
    'thrilldiver': {
        'enabled': True,
        'keywords': ['thrilldiver', 'сумрачный дайвер', 'триллдайвер', 'трилдайвер', 'трил дайвер', 'трилл дайвер'],
        'price': 1500
    },
    'freediver': {
        'enabled': True,
        'keywords': ['фридайвер', 'фри дайвер', 'freediver', 'free diver'],
        'price': 1500
    },
    'cobalt_snowfoot': {
        'enabled': True,
        'keywords': ['cobalt snowfoot', 'cobaltsnowfoot', 'cobalt', 'кобальтовый айсберг'],
        'price': 1500
    },
    'florin': {
        'enabled': True,
        'keywords': ['флорин', 'florin'],
        'price': 1500
    },
    'twitch_prime': {
        'enabled': True,
        'keywords': ['twitch prime', 'твич прайм', 'havoc', 'sub commander',
                     'trailblazer', 'опустошитель', 'заместитель командующего', 'боевая подруга'],
        'price': 1500
    },
    'black_knight': {
        'enabled': True,
        'keywords': ['чёрный рыцарь', 'черный рыцарь', 'black knight', 'блэк найт'],
        'price': 5000,
        'require_pve': False
    },
    'sparkle_specialist': {
        'enabled': True,
        'keywords': ['искромётный спец', 'искрометный спец', 'sparkle specialist', 'спаркл'],
        'price': 3000,
        'require_pve': False
    },
    'floss': {
        'enabled': True,
        'keywords': ['флосс', 'флос', 'floss', 'flos'],
        'price': 1500,
        'require_pve': False
    },
}

DEFAULT_CONFIRMED_PVE = [
    'og stw', 'og pve', 'старое пве', 'old pve', 'save the world',
    'олд пве', 'олд ств', 'олд stw', 'олд pve',
    'old stw', 'old save the world', 'старое ств',
    'лидер в розовом', 'боевая раскраска', 'rose team leader', 'warpaint',
    'фарм вбаксов', 'фарм в-баксов', 'фарм в баксов',
    'с фармом вбаксов', 'с фармом в-баксов', 'с фармом в баксов',
    'с вбаксами', 'с в-баксами', 'с в баксами',
    'фармит вбаксы', 'фармит в-баксы', 'фармит в баксы',
    'фарм v-bucks', 'фарм vbucks', 'farm vbucks', 'farm v-bucks',
    'фарм v bucks', 'farm v bucks',
    'vbucks farm', 'v-bucks farm',
    "founder's edition", 'founders edition', 'founder edition',
    'founder pack', 'founders pack', "founder's pack", 'founder',
    'фаундер', 'фаундерс',
    'издание основателя', 'набор основателя', 'набор основателей',
    'пак основателя', 'паки основателя', 'паки основателей',
]
# ВАЖНО: не добавлять голое 'основатель' — это имя скина (EN: Foundation),
# оно даст ложные срабатывания на лотах со скином, а не с Founder Pack.

DEFAULT_UNCONFIRMED_PVE = ['stw', 'pve', 'пве']

DEFAULT_NEW_PVE = [
    'новое пве', 'новый пве', 'новая пве',
    'новое ств', 'новый ств', 'новая ств',
    'новое stw', 'новый stw', 'new stw',
    'новое pve', 'новый pve', 'new pve',
    'новое save the world', 'новый save the world', 'new save the world',
    'new save the world edition'
]

DEFAULT_EXCLUDE_KEYWORDS = [
    # --- Почта отсутствует / недоступна ---
    'без почты', 'почты нет', 'почты нету', 'нет почты',
    'почты не будет', 'почты не даю', 'почты не будет в комплекте',
    'почту не дам', 'почту не отдам', 'почту не передам',
    'доступ без почты', 'доступа к почте нет', 'доступа к почте нету',
    'доступа к почте не', 'к почте доступа нет', 'к почте доступа нету',
    'почта недоступна', 'почта не доступна', 'к почте нет доступа',
    'к почте нету доступа', 'к почте не имею доступа',
    'доступ к почте нету', 'доступ к почте нет', 'доступа к почте нету',
    'доступ к почте утерян', 'доступа к почте утерян',
    'без доступа к почте', 'почту не отдаю', 'почту не даю',
    'почта не передается', 'почта не передаётся', 'почту не передаю',
    'почту не передам', 'не передаю почту', 'не передам почту',
    'почта не в комплекте', 'почта не прилагается',
    'почта не входит в комплект', 'почта не входит в стоимость',
    'no email', 'without email', 'без мыла',
    'no mail', 'mail is mine', 'email is mine',
    'потерял доступ к почте', 'потеряла доступ к почте',
    'потерял доступ к почте поэтому', 'потеряла доступ к почте поэтому',
    'потерял досутп к почте', 'потеряла досутп к почте',
    'потерян доступ к почте', 'утерян доступ к почте',
    'утратил доступ к почте', 'утратила доступ к почте',
    'почта утеряна', 'почта потеряна', 'забыл почту', 'забыла почту',
    'аккаунт продается без нее', 'аккаунт продаётся без неё',
    'продается без нее', 'продаётся без неё',
    'аккаунт продается без нее из за этого', 'аккаунт продаётся без неё из за этого',
    'аккаунт продается без нее, из за этого', 'аккаунт продаётся без неё, из за этого',
    'продается без нее из за этого', 'продаётся без неё из за этого',
    'without access to email', 'lost access to email',
    'email не передается', 'email не передаётся',
    'email не передам', 'email не дам', 'email не отдам',
    'без емейла', 'емейл не даю', 'емейла нет', 'емейла нету',
    'mail не даю', 'mail не отдаю', 'mail не передам',
    'почта не включена', 'почта не входит',
    # --- Почта остаётся у продавца ---
    'почта остается у продавца', 'почта остаётся у продавца',
    'почта у продавца', 'почта моя', 'почта остается моей', 'почта остаётся моей',
    'почта привязана ко мне', 'почта моя останется',
    'я оставляю почту', 'почту оставляю себе', 'почту оставлю себе',
    # --- Нет данных от почты ---
    'данных от почты нет', 'данных от почты нету', 'нет данных от почты',
    'данных от почты у меня нету', 'данных почты нет',
    'данные от почты не выдаю', 'данные от почты не передаются',
    # --- Нельзя сменить пароль/почту ---
    'пароль не поменять', 'почту не поменять',
    'не можете поменять пароль', 'не можете поменять почту',
    'не сможете поменять пароль', 'не сможете поменять почту',
    'смена почты недоступна', 'смена почты не доступна',
    'смена почты невозможна', 'смена почты не возможна',
    'почту сменить нельзя', 'нельзя сменить почту',
    'невозможно сменить почту', 'сменить почту нельзя',
    'смена пароля невозможна', 'сменить пароль нельзя',
    'нельзя сменить пароль', 'пароль сменить нельзя',
    'пароль менять нельзя', 'почту менять нельзя',
    'доступ к почте не включен', 'доступ к почте не предоставляется',
    'доступ к почте не передается', 'доступ к почте не передаётся',
    # --- Только Epic ---
    'только эпик', 'only epic', 'получаете только эпик',
    'только epic', 'передаю только эпик',
    'вы получаете только эпик', 'отдаю только эпик',
    'только логин эпик', 'только данные эпик',
    'epic only', 'только вход в epic', 'только вход в эпик',
    'только данные от epic', 'только данные от эпик',
    # --- Перепривязка отсутствует ---
    'нет перепривязки', 'нет перепревязки',
    'без перепривязки', 'без перепревязки',
    'перепривязка невозможна', 'перепревязка невозможна',
    'нельзя перепривязать', 'нельзя перепревязать',
    'перепривязки нет', 'перепревязки нет', 'перепривязки нету',
    'не перепривязать', 'не перепревязать',
    'перепривязка недоступна', 'перепревязка недоступна',
    'без смены привязки', 'смена привязки невозможна',
    'привязку не сменить', 'привязка не меняется',
    'не отвяжешь', 'отвязать не получится',
    # --- Нет смены данных ---
    'нет смены данных', 'смена данных невозможна',
    'нельзя сменить данные', 'данные не меняются',
    'смена невозможна', 'без смены данных',
    'смены данных нет', 'данные сменить нельзя',
    'нет возможности сменить', 'невозможно сменить',
    'данные менять нельзя', 'нельзя менять данные',
    # --- Родительская почта / пин ---
    'доступа к родительской почте нету',
    'доступа к родительской почте нет',
    'нет доступа к родительской почте',
    'без родительской почты',
    'родительской почты нет', 'родительской почты нету',
    'пин-кода нету', 'пин-кода нет',
    'пин кода нету', 'пин кода нет',
    'нет пин-кода', 'нет пин кода',
    'без пин-кода', 'без пин кода',
    'доступа к родительской почте и пин-коду нету',
    'доступа к родительской почте и пин коду нету',
    # --- Аренда ---
    'аренда', 'for rent', 'rental', 'в аренду', 'сдаю в аренду', 'под аренду',
    # --- Сленг / хитрые формулировки ---
    'только поиграть', 'дается только для игры', 'даю только поиграть',
    'эпик онли', 'только ярлык', 'только логин', 'только пароль',
    'просто логин пароль', 'только вход', 'только данные для входа',
    'пароль и логин', 'логин пароль без почты',
    'почты в продаже нет', 'почта не в продаже',
    'почта при аккаунте не идет', 'почта к аккаунту не идёт',
    'почту получить нельзя', 'получить почту нельзя',
    # --- Нет полного доступа (EN) ---
    'no full access', 'non-full-access', 'non full access',
    'unverified', 'no email access',
]

# --- Позитивные фразы: если найдены, аккаунт ТОЧНО с почтой/перепривязкой ---
# Имеют приоритет над DEFAULT_EXCLUDE_KEYWORDS (whitelist > blacklist).
# Например, "перепривяжу почту" → позитив → НЕ исключается, даже если есть
# другое негативное слово в описании.
DEFAULT_POSITIVE_KEYWORDS = [
    # --- Перепривязка возможна ---
    'перепривяжу', 'перепривяжем', 'перепривяжу почту',
    'перепривяжу на вас', 'перепривяжу под вас', 'перепривяжу на вашу',
    'делаю перепривязку', 'перепривязка есть', 'перепривязка возможна',
    'перепривязка доступна', 'с перепривязкой', 'перепривязка входит',
    'перепривязку сделаю', 'сделаю перепривязку', 'выполню перепривязку',
    'перепривязка включена', 'перепривязку включаю',
    'можно перепривязать', 'можете перепривязать', 'сможете перепривязать',
    'перепривязать можно', 'свободная перепривязка', 'полная перепривязка',
    'перепривяжу без проблем', 'перепривязка без проблем',
    # --- Почта передаётся ---
    'почта в комплекте', 'почта есть', 'почта имеется', 'с почтой',
    'передаю почту', 'отдаю почту', 'выдам почту', 'выдаю почту',
    'даю почту', 'отдам почту', 'передам почту', 'дам почту',
    'почта передается', 'почта передаётся', 'почта будет передана',
    'почта прилагается', 'почта включена', 'почта входит',
    'почта входит в комплект', 'почта входит в стоимость',
    'почта к аккаунту', 'почта к акку', 'почта идёт к аккаунту',
    'полный доступ к почте', 'есть доступ к почте', 'доступ к почте есть',
    'доступ к почте имеется', 'доступ к почте передается',
    'доступ к почте передаётся', 'с доступом к почте',
    'включая почту', 'вместе с почтой',
    'данные от почты', 'с данными от почты', 'с данными почты',
    'пароль от почты', 'с паролем от почты',
    # --- Смена пароля/почты возможна ---
    'можно сменить почту', 'можете сменить почту', 'сможете сменить почту',
    'смена почты возможна', 'смена почты доступна', 'смена почты есть',
    'почту сменить можно', 'можно менять почту',
    'можно сменить пароль', 'смена пароля возможна', 'смена пароля доступна',
    'пароль сменить можно', 'пароль можете сменить',
    'смените пароль', 'смените почту',
    # --- Родительская почта / пин ---
    'с родительской почтой', 'родительская почта есть',
    'полный доступ', 'с пин-кодом', 'пин-код есть', 'пин-код имеется',
    'с пин кодом', 'пин код есть',
    # --- Английские позитивы ---
    'with email', 'email included', 'full access', 'with mail',
    'can change email', 'email change available', 'rebind available',
    # --- Полный доступ / продажа ---
    'полный фулл', 'фулл доступ', 'full access', 'полный пакет',
    'навсегда ваш', 'аккаунт навсегда', 'продажа навсегда',
    'полное владение', 'передача прав',
]

DEFAULT_EDITIONS = {
    'super_deluxe': {
        'enabled': True,
        'keywords': ['super deluxe', 'супер делюкс', 'super deluxe edition',
                     'pve super deluxe', 'stw super deluxe', 'делюкс', 'deluxe'],
        'price': 3000
    },
    'limited': {
        'enabled': True,
        'keywords': ['limited edition', 'лимитед эдишн', 'лимитед издание', 'лимитированное издание',
                     'лимитка', 'pve limited', 'pve лимитед', 'stw limited', 'лимитед', 'limited'],
        'price': 3000
    },
    'ultimate': {
        'enabled': True,
        'keywords': ['ultimate edition', 'ультимейт эдишн', 'ультимейт издание', 'ультимейт',
                     'ultimate', 'pve ultimate', 'stw ultimate',
                     'nocturno', 'ноктурно', 'ноктюрно', 'ноктюрн'],
        'price': 5000
    }
}

DEFAULT_PROCHEE_KEYWORDS = [
    'ключ pve', 'ключ пве', 'pve access', 'pve key', 'pve ключ',
    'доступ к pve', 'доступ pve', 'пве доступ',
    'save the world key', 'stw key', 'ключ stw',
    'ключ save the world', 'доступ к stw', 'доступ stw'
]

DEFAULT_CONFIG = {
    'rare_skins': DEFAULT_RARE_SKINS,
    'confirmed_pve_keywords': DEFAULT_CONFIRMED_PVE,
    'unconfirmed_pve_keywords': DEFAULT_UNCONFIRMED_PVE,
    'new_pve_keywords': DEFAULT_NEW_PVE,
    'exclude_keywords': DEFAULT_EXCLUDE_KEYWORDS,
    'positive_keywords': DEFAULT_POSITIVE_KEYWORDS,
    'editions': DEFAULT_EDITIONS,
    'prochee_keywords': DEFAULT_PROCHEE_KEYWORDS,
    'max_price': 5000,
    'confirmed_pve_enabled': True,
    'confirmed_pve_price': 700,
    'unconfirmed_pve_price': 450,
    'minprice_bundle': {
        'skins': list(DEFAULT_RARE_SKINS.keys()),
        'editions': list(DEFAULT_EDITIONS.keys()),
        'confirmed_pve': True,
        'unconfirmed_pve': False,
    },
    'pve_bonus': 750,
    'check_interval': 120,
    'request_delay_min': 3,
    'request_delay_max': 7,
    'search_mode': 'skins_pve',  # skins_pve | skins_only | pve_only | all_pve
}


class ConfigManager:
    """Менеджер конфигурации бота. Хранит настройки в config.json."""

    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.data = {}
        self.load()

    # ========================
    # Загрузка / Сохранение
    # ========================

    def load(self):
        """Загрузить конфиг из файла, или создать из дефолтов."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                # Дополняем отсутствующие ключи дефолтами
                for key, default_val in DEFAULT_CONFIG.items():
                    if key not in self.data:
                        self.data[key] = copy.deepcopy(default_val)
                # Добавляем enabled=True для скинов без этого поля (миграция)
                for skin_id, skin_data in self.data.get('rare_skins', {}).items():
                    if 'enabled' not in skin_data:
                        skin_data['enabled'] = True
                # Миграция: добавляем enabled для изданий
                for eid, ed_data in self.data.get('editions', {}).items():
                    if 'enabled' not in ed_data:
                        ed_data['enabled'] = True
                # Миграция: добавляем новые скины из дефолтов, если их нет
                existing_skins = self.data.get('rare_skins', {})
                for sid, sdata in DEFAULT_RARE_SKINS.items():
                    if sid not in existing_skins:
                        existing_skins[sid] = copy.deepcopy(sdata)
                        logger.info(f"Миграция: добавлен скин '{sid}' из дефолтов")
                    else:
                        # Дополняем отсутствующие ключевые слова из дефолтов
                        existing_kws_lower = {k.lower() for k in existing_skins[sid].get('keywords', [])}
                        for kw in sdata.get('keywords', []):
                            if kw.lower() not in existing_kws_lower:
                                existing_skins[sid].setdefault('keywords', []).append(kw)
                                logger.info(f"Миграция: для скина '{sid}' добавлено ключевое слово '{kw}'")
                # Миграция: добавляем недостающие confirmed PVE слова из дефолтов
                existing_confirmed = set(k.lower() for k in self.data.get('confirmed_pve_keywords', []))
                for kw in DEFAULT_CONFIRMED_PVE:
                    if kw.lower() not in existing_confirmed:
                        self.data.setdefault('confirmed_pve_keywords', []).append(kw)
                        logger.info(f"Миграция: добавлено подтв. PVE слово '{kw}'")
                existing_excludes = set(k.lower() for k in self.data.get('exclude_keywords', []))
                for kw in DEFAULT_EXCLUDE_KEYWORDS:
                    if kw.lower() not in existing_excludes:
                        self.data.setdefault('exclude_keywords', []).append(kw)
                        logger.info(f"Миграция: добавлен фильтр '{kw}'")
                self.save()
                logger.info(f"Конфиг загружен из {self.config_path}")
            except (json.JSONDecodeError, Exception) as e:
                logger.error(f"Ошибка загрузки конфига: {e}. Используем дефолт.")
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                self.save()
        else:
            logger.info(f"Конфиг не найден, создаю {self.config_path} с дефолтами.")
            self.data = copy.deepcopy(DEFAULT_CONFIG)
            self.save()

    def save(self):
        """Сохранить текущий конфиг в файл."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения конфига: {e}")

    # ========================
    # Скины
    # ========================

    def get_all_skins(self):
        """Все скины: {id: {enabled, keywords, price}}."""
        return self.data.get('rare_skins', {})

    def get_enabled_skins(self):
        """Только включённые скины (для мониторинга)."""
        return {sid: s for sid, s in self.get_all_skins().items() if s.get('enabled', True)}

    def get_skin(self, skin_id):
        """Получить скин по ID."""
        return self.data.get('rare_skins', {}).get(skin_id)

    def get_skin_ids_sorted(self):
        """Список ID скинов, отсортированных по имени."""
        return sorted(self.data.get('rare_skins', {}).keys())

    def toggle_skin(self, skin_id):
        """Переключить вкл/выкл. Возвращает новое состояние или None."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return None
        skin['enabled'] = not skin.get('enabled', True)
        self.save()
        return skin['enabled']

    def toggle_skin_pve(self, skin_id):
        """Переключить require_pve. Возвращает новое состояние или None."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return None
        skin['require_pve'] = not skin.get('require_pve', False)
        self.save()
        return skin['require_pve']

    def add_skin_keyword(self, skin_id, keyword):
        """Добавить ключевое слово к скину. Возвращает True если добавлено."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return False
        kw_lower = keyword.lower().strip()
        if not kw_lower:
            return False
        if kw_lower in [k.lower() for k in skin.get('keywords', [])]:
            return False
        skin.setdefault('keywords', []).append(kw_lower)
        self.save()
        return True

    def remove_skin_keyword(self, skin_id, keyword_index):
        """Удалить ключевое слово по индексу. Возвращает удалённое слово или None."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return None
        keywords = skin.get('keywords', [])
        if keyword_index < 0 or keyword_index >= len(keywords):
            return None
        if len(keywords) <= 1:
            return None  # Нельзя удалить последнее
        removed = keywords.pop(keyword_index)
        self.save()
        return removed

    def set_skin_price(self, skin_id, price):
        """Установить цену скина."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return False
        skin['price'] = price
        self.save()
        return True

    def set_skin_keywords(self, skin_id, keywords):
        """Установить список ключевых слов для скина."""
        skin = self.get_skin(skin_id)
        if skin is None:
            return False
        skin['keywords'] = keywords
        self.save()
        return True

    def add_skin(self, skin_id, keywords, price):
        """Добавить новый скин."""
        if skin_id in self.data.get('rare_skins', {}):
            return False
        self.data.setdefault('rare_skins', {})[skin_id] = {
            'enabled': True,
            'keywords': keywords,
            'price': price,
        }
        self.save()
        return True

    def delete_skin(self, skin_id):
        """Удалить скин."""
        if skin_id in self.data.get('rare_skins', {}):
            del self.data['rare_skins'][skin_id]
            self.save()
            return True
        return False

    # ========================
    # PVE ключевые слова
    # ========================

    def get_confirmed_pve(self):
        return self.data.get('confirmed_pve_keywords', [])

    def get_unconfirmed_pve(self):
        return self.data.get('unconfirmed_pve_keywords', [])

    def get_all_pve(self):
        return self.get_confirmed_pve() + self.get_unconfirmed_pve()

    def get_new_pve(self):
        return self.data.get('new_pve_keywords', [])

    def get_premium_pve(self):
        """Возвращает плоский список ключевых слов из включённых изданий."""
        editions = self.data.get('editions', {})
        keywords = []
        for eid, ed in editions.items():
            if ed.get('enabled', True):
                keywords.extend(ed.get('keywords', []))
        return keywords

    def get_prochee_keywords(self):
        """Ключевые слова для лота 'Fortnite прочее' (ключи PVE и т.д.)."""
        return self.data.get('prochee_keywords', [])

    def get_all_editions(self):
        return self.data.get('editions', {})

    def get_edition(self, edition_id):
        return self.data.get('editions', {}).get(edition_id)

    def toggle_edition(self, edition_id):
        ed = self.get_edition(edition_id)
        if ed:
            ed['enabled'] = not ed.get('enabled', True)
            self.save()
            return True
        return False

    def add_confirmed_pve(self, keyword):
        kw_list = self.data.setdefault('confirmed_pve_keywords', [])
        if keyword.lower() not in [k.lower() for k in kw_list]:
            kw_list.append(keyword)
            self.save()
            return True
        return False

    def remove_confirmed_pve(self, keyword):
        kw_list = self.data.get('confirmed_pve_keywords', [])
        lower_kw = keyword.lower()
        new_list = [k for k in kw_list if k.lower() != lower_kw]
        if len(new_list) < len(kw_list):
            self.data['confirmed_pve_keywords'] = new_list
            self.save()
            return True
        return False

    def add_unconfirmed_pve(self, keyword):
        kw_list = self.data.setdefault('unconfirmed_pve_keywords', [])
        if keyword.lower() not in [k.lower() for k in kw_list]:
            kw_list.append(keyword)
            self.save()
            return True
        return False

    def remove_unconfirmed_pve(self, keyword):
        kw_list = self.data.get('unconfirmed_pve_keywords', [])
        lower_kw = keyword.lower()
        new_list = [k for k in kw_list if k.lower() != lower_kw]
        if len(new_list) < len(kw_list):
            self.data['unconfirmed_pve_keywords'] = new_list
            self.save()
            return True
        return False

    # ========================
    # Фильтры исключений
    # ========================

    def get_exclude_keywords(self):
        return self.data.get('exclude_keywords', [])

    def add_exclude_keyword(self, keyword):
        kw_list = self.data.setdefault('exclude_keywords', [])
        if keyword.lower() not in [k.lower() for k in kw_list]:
            kw_list.append(keyword)
            self.save()
            return True
        return False

    def reset_exclude_keywords(self):
        """Сбрасывает фильтры-исключения к дефолтным."""
        import copy
        self.data['exclude_keywords'] = copy.deepcopy(DEFAULT_EXCLUDE_KEYWORDS)
        self.save()
        return len(self.data['exclude_keywords'])

    def remove_exclude_keyword(self, keyword):
        kw_list = self.data.get('exclude_keywords', [])
        lower_kw = keyword.lower()
        new_list = [k for k in kw_list if k.lower() != lower_kw]
        if len(new_list) < len(kw_list):
            self.data['exclude_keywords'] = new_list
            self.save()
            return True
        return False

    # ========================
    # Позитивные ключевые слова (whitelist)
    # ========================

    def get_positive_keywords(self):
        return self.data.get('positive_keywords', [])

    # ========================
    # Числовые параметры
    # ========================

    @property
    def x5_mode(self):
        import os
        return self.data.get('x5_mode', False) or os.environ.get('X5_MODE') == 'true'

    @x5_mode.setter
    def x5_mode(self, value):
        self.data['x5_mode'] = bool(value)
        self.save()

    @property
    def max_price(self):
        return self.data.get('max_price', 5000)

    @max_price.setter
    def max_price(self, value):
        self.data['max_price'] = value
        self.save()

    @property
    def confirmed_pve_price(self):
        return self.data.get('confirmed_pve_price', 700)

    @confirmed_pve_price.setter
    def confirmed_pve_price(self, value):
        self.data['confirmed_pve_price'] = value
        self.save()

    @property
    def unconfirmed_pve_price(self):
        return self.data.get('unconfirmed_pve_price', 450)

    @unconfirmed_pve_price.setter
    def unconfirmed_pve_price(self, value):
        self.data['unconfirmed_pve_price'] = value
        self.save()

    @property
    def confirmed_pve_enabled(self):
        return self.data.get('confirmed_pve_enabled', True)

    @confirmed_pve_enabled.setter
    def confirmed_pve_enabled(self, value):
        self.data['confirmed_pve_enabled'] = bool(value)
        self.save()

    @property
    def pve_bonus(self):
        return self.data.get('pve_bonus', 750)

    @pve_bonus.setter
    def pve_bonus(self, value):
        self.data['pve_bonus'] = value
        self.save()

    @property
    def check_interval(self):
        return self.data.get('check_interval', 120)

    @check_interval.setter
    def check_interval(self, value):
        self.data['check_interval'] = value
        self.save()

    @property
    def request_delay_min(self):
        return self.data.get('request_delay_min', 3)

    @request_delay_min.setter
    def request_delay_min(self, value):
        self.data['request_delay_min'] = value
        self.save()

    @property
    def request_delay_max(self):
        return self.data.get('request_delay_max', 7)

    @request_delay_max.setter
    def request_delay_max(self, value):
        self.data['request_delay_max'] = value
        self.save()

    @property
    def search_mode(self):
        return self.data.get('search_mode', 'skins_pve')
    @search_mode.setter
    def search_mode(self, value):
        if value in ('skins_pve', 'skins_only', 'pve_only', 'all_pve'):
            self.data['search_mode'] = value
            self.save()

    def get_minprice_bundle(self):
        bundle = self.data.get('minprice_bundle', {})
        return {
            'skins': list(bundle.get('skins', [])),
            'editions': list(bundle.get('editions', [])),
            'confirmed_pve': bool(bundle.get('confirmed_pve', True)),
            'unconfirmed_pve': bool(bundle.get('unconfirmed_pve', False)),
        }

    def set_minprice_bundle(self, skins=None, editions=None, confirmed_pve=None, unconfirmed_pve=None):
        bundle = self.data.setdefault('minprice_bundle', {
            'skins': [],
            'editions': [],
            'confirmed_pve': True,
            'unconfirmed_pve': False,
        })
        if skins is not None:
            bundle['skins'] = sorted(set(skins))
        if editions is not None:
            bundle['editions'] = sorted(set(editions))
        if confirmed_pve is not None:
            bundle['confirmed_pve'] = bool(confirmed_pve)
        if unconfirmed_pve is not None:
            bundle['unconfirmed_pve'] = bool(unconfirmed_pve)
        self.save()

    # ========================
    # Хелпер-методы для мониторинга
    # ========================

    def get_search_keywords(self, include_unconfirmed_pve=False):
        """Собирает ключевые слова для поиска в зависимости от search_mode."""
        mode = self.search_mode

        # Принудительное включение неподтверждённых (для recheck ++pve или режима all_pve)
        use_unconfirmed = include_unconfirmed_pve or mode == 'all_pve'

        skin_kw = []
        pve_kw = []

        if mode in ('skins_pve', 'skins_only', 'all_pve'):
            for skin in self.get_enabled_skins().values():
                skin_kw.extend(skin.get('keywords', []))

        if mode in ('skins_pve', 'pve_only', 'all_pve'):
            pve_kw = self.get_all_pve() if use_unconfirmed else self.get_confirmed_pve()

        return pve_kw + skin_kw

    def get_enabled_skins_dict(self):
        """Возвращает словарь включённых скинов в формате для find_skins_in_text."""
        result = {}
        for sid, sdata in self.get_enabled_skins().items():
            result[sid] = {
                'keywords': sdata.get('keywords', []),
                'price': sdata.get('price', 0),
                'require_pve': sdata.get('require_pve', False),
            }
        return result
