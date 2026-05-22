"""
Диагностика: показывает первые 20 новых кандидатов с полным анализом
почему каждый пропущен или подошёл.
"""
import sys, os, re, json, time, requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Загрузка конфига
with open('config.json', 'r', encoding='utf-8') as f:
    cfg = json.load(f)

# Загрузка seen_ids
seen = set()
try:
    with open('seen_ids.txt', 'r') as f:
        seen = set(line.strip() for line in f if line.strip())
except: pass

rare_skins = cfg.get('rare_skins', {})
editions = cfg.get('editions', {})
confirmed_pve = cfg.get('confirmed_pve_keywords', [])
unconfirmed_pve = cfg.get('unconfirmed_pve_keywords', [])
exclude_kw = cfg.get('exclude_keywords', [])
positive_kw = cfg.get('positive_keywords', [])
max_price = cfg.get('max_price', 5000)
pve_bonus = cfg.get('pve_bonus', 750)

def normalize(text):
    return re.sub(r'\s+', ' ', text.lower().replace('ё', 'е')).strip()

def find_skins(text):
    norm = normalize(text)
    found = []
    for sid, data in rare_skins.items():
        if not data.get('enabled', True):
            continue
        for kw in data['keywords']:
            if re.search(r'\b' + re.escape(normalize(kw)) + r'\b', norm):
                found.append({'id': sid, 'kw': kw, 'price': data['price'], 'req_pve': data.get('require_pve', False)})
                break
    return found

def find_editions(text):
    norm = normalize(text)
    found = []
    for eid, data in editions.items():
        if not data.get('enabled', True):
            continue
        for kw in data['keywords']:
            if normalize(kw) in norm:
                found.append({'id': eid, 'kw': kw, 'price': data['price']})
                break
    return found

def find_pve(text, confirmed_only=True):
    norm = normalize(text)
    kws = confirmed_pve if confirmed_only else confirmed_pve + unconfirmed_pve
    for kw in kws:
        if normalize(kw) in norm:
            return kw
    return None

def find_exclude(text):
    norm = normalize(text)
    for pos in positive_kw:
        if normalize(pos) in norm:
            return None  # positive overrides
    for ex in exclude_kw:
        if normalize(ex) in norm:
            return ex
    return None

def get_listings():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'ru-RU,ru;q=0.9',
        'Cookie': 'cy=rub'
    })
    url = 'https://funpay.com/lots/248/?offer_type=sell'
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    items = soup.find_all('a', class_='tc-item')
    results = []
    for item in items:
        href = item.get('href', '')
        if not href.startswith('http'):
            href = f"https://funpay.com{href}"
        oid = re.search(r'id=(\d+)', href)
        oid = oid.group(1) if oid else None
        price_el = item.find('div', class_='tc-price')
        price_text = price_el.get_text(strip=True) if price_el else '?'
        desc_el = item.find('div', class_='tc-desc-text')
        desc = desc_el.get_text(strip=True) if desc_el else ''
        try:
            price_val = float(re.sub(r'[^\d.]', '', price_text.replace(',', '.').replace(' ', '')))
        except:
            price_val = 0
        results.append({
            'id': oid, 'href': href, 'price': price_val,
            'price_text': price_text, 'desc': desc[:200]
        })
    return results

print("="*80)
print("ДИАГНОСТИКА FunPay Monitor")
print("="*80)
print(f"\nСкинов в конфиге: {len(rare_skins)} (все require_pve={all(s.get('require_pve') for s in rare_skins.values())})")
print(f"Изданий: {len(editions)}")
print(f"Max price: {max_price}₽ | PVE bonus: {pve_bonus}₽")
print(f"Seen IDs: {len(seen)}")

print(f"\n{'='*80}")
print("Загружаю лоты с FunPay...")
listings = get_listings()
print(f"Всего лотов: {len(listings)}")

# Фильтруем новые (не seen)
new_listings = [l for l in listings if l['id'] not in seen]
print(f"Новых (не в seen_ids): {len(new_listings)}")

print(f"\n{'='*80}")
print(f"ПЕРВЫЕ 20 НОВЫХ КАНДИДАТОВ — ДЕТАЛЬНЫЙ АНАЛИЗ")
print(f"{'='*80}\n")

for i, lot in enumerate(new_listings[:20], 1):
    desc = lot['desc']
    skins = find_skins(desc)
    eds = find_editions(desc)
    pve = find_pve(desc, confirmed_only=True)
    pve_any = find_pve(desc, confirmed_only=False)
    excl = find_exclude(desc)
    
    # Вердикт
    verdict_parts = []
    if not skins and not eds and not pve_any:
        verdict = "❌ Нет скинов/изданий/PVE"
    elif excl:
        verdict = f"🚫 Исключено: '{excl}'"
    elif skins:
        pve_required = [s for s in skins if s['req_pve']]
        pve_ok = [s for s in skins if not s['req_pve']]
        if pve_required and not pve:
            verdict = f"🧟 Скин требует PVE, но PVE НЕ найден в описании"
        else:
            best_price = max(s['price'] for s in skins) + (pve_bonus if pve else 0)
            if lot['price'] <= best_price:
                verdict = f"✅ ПОДХОДИТ! Цена {lot['price']}₽ <= лимит {best_price}₽"
            else:
                verdict = f"💸 Дорого: {lot['price']}₽ > лимит {best_price}₽"
    elif eds:
        best_ed = max(eds, key=lambda e: e['price'])
        if lot['price'] <= best_ed['price']:
            verdict = f"✅ ПОДХОДИТ (издание)! {lot['price']}₽ <= {best_ed['price']}₽"
        else:
            verdict = f"💸 Издание дорого: {lot['price']}₽ > {best_ed['price']}₽"
    elif pve:
        verdict = f"📗 Только PVE (confirmed_pve_enabled={cfg.get('confirmed_pve_enabled')})"
    else:
        verdict = "❌ Нет совпадений"
    
    skin_str = ', '.join(f"{s['id']}({s['price']}₽, pve={s['req_pve']})" for s in skins) if skins else '—'
    ed_str = ', '.join(f"{e['id']}({e['price']}₽)" for e in eds) if eds else '—'
    
    print(f"#{i} | ID: {lot['id']} | Цена: {lot['price_text']}")
    print(f"   Описание: {desc[:150]}...")
    print(f"   Скины:    {skin_str}")
    print(f"   Издания:  {ed_str}")
    print(f"   PVE:      confirmed={pve or '—'} | any={pve_any or '—'}")
    print(f"   Исключ.:  {excl or '—'}")
    print(f"   >>> ВЕРДИКТ: {verdict}")
    print()

# Итого по ВСЕМ лотам (быстрый скан по описанию без загрузки деталей)
print(f"{'='*80}")
print(f"СВОДКА ПО ВСЕМ {len(listings)} ЛОТАМ (по краткому описанию)")
print(f"{'='*80}")
has_skin = 0
has_skin_pve = 0
has_edition = 0
has_pve_only = 0
for lot in listings:
    s = find_skins(lot['desc'])
    e = find_editions(lot['desc'])
    p = find_pve(lot['desc'])
    if s:
        has_skin += 1
        if p: has_skin_pve += 1
    if e: has_edition += 1
    if p and not s and not e: has_pve_only += 1

print(f"  С редким скином: {has_skin}")
print(f"  С редким скином + PVE: {has_skin_pve}")
print(f"  С изданием: {has_edition}")
print(f"  Только PVE (без скинов): {has_pve_only}")
print(f"  Без совпадений: {len(listings) - has_skin - has_edition - has_pve_only}")
