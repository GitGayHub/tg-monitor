# -*- coding: utf-8 -*-
import json
import os
import re
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(ROOT, "scratch", "once_run.log")
if not os.path.exists(log_path):
    log_path = os.path.join(ROOT, "scratch", "once_run.log")

with open(log_path, "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

def has(pat):
    return [ln.rstrip() for ln in lines if re.search(pat, ln)]

print("=== KEY LINES ===")
for ln in has(r"Статистика|Итого отправлено|Done \(sent|Обрабатываю кандидатов|лимит времени|Seen IDs"):
    m = re.search(r" - (?:root - )?(?:INFO|DEBUG|WARNING) - (.+)$", ln)
    print(m.group(1) if m else ln)

print("\n=== COUNTS ===")
print("fake price skips:", len(has(r"Пропуск фейк-цены")))
print("recently-sent skips:", len(has(r"уже отправлялся")))
print("sent OK lines:", len(has(r"✅ Отправлено:")))
print("too expensive:", len(has(r"Слишком дорого")))
print("excluded:", len(has(r"🚫 Исключено")))
print("no value skip:", len(has(r"нет ценных")))

print("\n=== SENT (from log) ===")
for ln in has(r"✅ Отправлено:"):
    m = re.search(r"✅ Отправлено: .+$", ln)
    if m:
        print(m.group(0))

sent_path = os.path.join(ROOT, "sent_offers.json")
with open(sent_path, "r", encoding="utf-8") as f:
    sent = json.load(f)

print("\n=== sent_offers.json ===")
print("count:", len(sent))
for oid, v in sorted(sent.items(), key=lambda x: x[1].get("timestamp", 0)):
    desc = (v.get("description") or "")[:70]
    print(f"{oid} | {v.get('price')} | {v.get('seller')} | {desc}")

prices = [v.get("price") or 0 for v in sent.values()]
print("min sent price:", min(prices) if prices else None)
print("any below 111:", any(p < 111 for p in prices))

by_s = defaultdict(list)
for oid, v in sent.items():
    by_s[v.get("seller") or "?"].append((oid, v))
print("\n=== multi-send sellers ===")
for s, items in by_s.items():
    if len(items) > 1:
        print(f"{s}: {len(items)}")
        for oid, v in items:
            print(" ", oid, v.get("price"), (v.get("description") or "")[:60])

# fake price examples
fake = has(r"Пропуск фейк-цены")
print("\n=== fake price sample (first 10) ===")
for ln in fake[:10]:
    m = re.search(r"💸 Пропуск фейк-цены .+$", ln)
    print(m.group(0) if m else ln[-120:])

# prices of fakes
fake_prices = []
for ln in fake:
    m = re.search(r"фейк-цены ([\d.]+)₽", ln)
    if m:
        fake_prices.append(float(m.group(1)))
if fake_prices:
    print(f"fake filtered: n={len(fake_prices)} min={min(fake_prices)} max={max(fake_prices)}")
    # check if any 1.xx style
    under_5 = sum(1 for p in fake_prices if p < 5)
    print(f"of those under 5₽: {under_5}")

seen_ids = open(os.path.join(ROOT, "seen_ids.txt"), encoding="utf-8").read().strip().splitlines()
print("\nseen_ids count:", len([x for x in seen_ids if x.strip()]))
