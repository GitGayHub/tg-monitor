# -*- coding: utf-8 -*-
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "scratch/config_origin.json"
c = json.load(open(path, encoding="utf-8"))
print("file:", path)
print("search_mode:", c.get("search_mode"))
print("confirmed_pve_enabled:", c.get("confirmed_pve_enabled"))
print("confirmed_pve_price:", c.get("confirmed_pve_price"))
print("unconfirmed_pve_price:", c.get("unconfirmed_pve_price"))
print("min_price:", c.get("min_price"))
print("max_price:", c.get("max_price"))
print("pve_bonus:", c.get("pve_bonus"))
print("check_interval:", c.get("check_interval"))
print("x5_mode:", c.get("x5_mode"))
print()
print("=== SKINS ===")
for sid, s in c.get("rare_skins", {}).items():
    print(
        f"  {sid}: price={s.get('price')} require_pve={s.get('require_pve')} "
        f"enabled={s.get('enabled', True)} kws={len(s.get('keywords', []))}"
    )
print()
print("=== EDITIONS ===")
for eid, e in c.get("editions", {}).items():
    print(f"  {eid}: price={e.get('price')} enabled={e.get('enabled', True)}")
print()
print("confirmed_pve_keywords:", len(c.get("confirmed_pve_keywords", [])))
print("unconfirmed_pve_keywords:", c.get("unconfirmed_pve_keywords"))
print("minprice_bundle:", c.get("minprice_bundle"))
