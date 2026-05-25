import json
import os
import sys

# Add parent dir to path so we can import config_crypt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config_crypt

config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
enc_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json.enc")

print(f"Reading {config_path}")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

# Original skin prices
original_skin_prices = {
    'royale_bomber': 1800,
    'eon': 1500,
    'double_helix': 1800,
    'dark_vertex': 2200,
    'neo_versa': 1500,
    'rogue_spider_knight': 1500,
    'stealth_reflex': 2000,
    'surf_strider': 1500,
    'wildcat': 2500,
    'dark_skully': 1800,
    'huntmaster_saber': 1500,
    'thrilldiver': 1500,
    'freediver': 1500,
    'cobalt_snowfoot': 1500,
    'florin': 1500,
    'twitch_prime': 1500,
    'black_knight': 5000,
    'sparkle_specialist': 3000,
    'floss': 1500,
}

# Original edition prices
original_edition_prices = {
    'super_deluxe': 1500,
    'limited': 2000,
    'ultimate': 2250,
}

# 1. Restore skin prices
for skin_id, skin_data in config.get("rare_skins", {}).items():
    orig_p = original_skin_prices.get(skin_id)
    if orig_p is not None:
        skin_data["price"] = orig_p
        print(f"Skin {skin_id} restored to: {skin_data['price']}")

# 2. Restore edition prices
for ed_id, ed_data in config.get("editions", {}).items():
    orig_p = original_edition_prices.get(ed_id)
    if orig_p is not None:
        ed_data["price"] = orig_p
        print(f"Edition {ed_id} restored to: {ed_data['price']}")

# 3. Restore max_price and confirmed_pve_price
config["max_price"] = 5000
print(f"max_price restored to: {config['max_price']}")

config["confirmed_pve_price"] = 700
print(f"confirmed_pve_price restored to: {config['confirmed_pve_price']}")

# 4. Disable x5_mode and test_summary_mode by default
config["x5_mode"] = False
print("x5_mode in config.json set to: False")

config["test_summary_mode"] = True
print("test_summary_mode in config.json set to: True")

# Save config.json
with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
print("Saved config.json")

# Encrypt it
passphrase = "FunPayBotPass2026Secure"
with open(config_path, "rb") as f:
    decrypted = f.read()

encrypted = config_crypt.encrypt(decrypted, passphrase)
with open(enc_path, "wb") as f:
    f.write(encrypted)
print("Encrypted config.json.enc successfully")
