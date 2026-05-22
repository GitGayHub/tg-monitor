"""
Тестовый прогон: поднимает все цены в config.json x3, запускает monitor.py --once,
потом восстанавливает оригинальный config.json.
"""
import json
import shutil
import subprocess
import sys
import os

CONFIG = "config.json"
BACKUP = "config.json.bak_x3"

# 1. Бэкап
shutil.copy2(CONFIG, BACKUP)
print(f"[x3 test] Бэкап создан: {BACKUP}")

# 2. Загрузить и модифицировать
with open(CONFIG, "r", encoding="utf-8") as f:
    cfg = json.load(f)

# Умножаем все цены скинов x3
changed = []
for skin_id, skin_data in cfg.get("rare_skins", {}).items():
    old = skin_data.get("price", 0)
    skin_data["price"] = old * 3
    changed.append(f"  {skin_id}: {old} -> {old*3}")

# Умножаем цены изданий x3
for ed_id, ed_data in cfg.get("editions", {}).items():
    old = ed_data.get("price", 0)
    ed_data["price"] = old * 3
    changed.append(f"  {ed_id}: {old} -> {old*3}")

# Умножаем max_price x3
old_max = cfg.get("max_price", 5000)
cfg["max_price"] = old_max * 3
changed.append(f"  max_price: {old_max} -> {old_max*3}")

# Умножаем pve_bonus x3
old_pve = cfg.get("pve_bonus", 750)
cfg["pve_bonus"] = old_pve * 3
changed.append(f"  pve_bonus: {old_pve} -> {old_pve*3}")

# Умножаем confirmed_pve_price x3
old_cpve = cfg.get("confirmed_pve_price", 700)
cfg["confirmed_pve_price"] = old_cpve * 3
changed.append(f"  confirmed_pve_price: {old_cpve} -> {old_cpve*3}")

with open(CONFIG, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print(f"[x3 test] Цены изменены x3:")
for c in changed:
    print(c)

# 3. Запуск monitor.py --once
print("\n[x3 test] Запускаю monitor.py --once ...")
result = subprocess.run(
    [sys.executable, "monitor.py", "--once"],
    env={**os.environ},
    cwd=os.path.dirname(os.path.abspath(__file__)),
)

# 4. Восстановить конфиг
shutil.move(BACKUP, CONFIG)
print(f"\n[x3 test] Конфиг восстановлен из бэкапа")
print(f"[x3 test] Exit code: {result.returncode}")
