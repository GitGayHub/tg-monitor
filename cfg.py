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
    f'item_{i}': {
        'enabled': True,
        'keywords': ['keyword_placeholder'],
        'price': 1000,
        'require_pve': False,
    } for i in range(1, 20)
}

DEFAULT_CONFIRMED_PVE = ['keyword_placeholder']

DEFAULT_UNCONFIRMED_PVE = ['keyword_placeholder']

DEFAULT_NEW_PVE = ['keyword_placeholder']

DEFAULT_EXCLUDE_KEYWORDS = ['keyword_placeholder']

DEFAULT_POSITIVE_KEYWORDS = ['keyword_placeholder']

DEFAULT_EDITIONS = {
    f'edition_{i}': {
        'enabled': True,
        'keywords': ['keyword_placeholder'],
        'price': 1000,
    } for i in range(1, 4)
}

DEFAULT_PROCHEE_KEYWORDS = ['keyword_placeholder']

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
        """Ключевые слова для лота 'Game прочее' (ключи PVE и т.д.)."""
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
