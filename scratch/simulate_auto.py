import sys
import asyncio
import os
import re
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

async def main():
    sys.stdout.reconfigure(encoding='utf-8')
    offer_id = "68759785"
    
    # 1. Load config
    config = monitor.config
    skins_dict = config.get_enabled_skins_dict()
    exclude_keywords = config.get_exclude_keywords()
    positive_keywords = config.get_positive_keywords()
    search_keywords = config.get_search_keywords(include_unconfirmed_pve=False)
    
    print("Search Keywords count:", len(search_keywords))
    print("Black Knight in search keywords:", "black knight" in search_keywords)
    
    # 2. Fetch live listings
    listings = await monitor.get_listings()
    target_item = None
    for idx, item in enumerate(listings, 1):
        href = item.get('href', '')
        if offer_id in href:
            target_item = item
            print(f"Target offer found at index {idx} in get_listings()")
            break
            
    if not target_item:
        print("Target offer not found in live listings!")
        return
        
    # Extract info from listing item
    desc_div = target_item.find('div', class_='tc-desc-text')
    price_div = target_item.find('div', class_='tc-price')
    user_div = target_item.find('div', class_='media-user-name')
    
    short_description = desc_div.get_text(strip=True) if desc_div else ""
    price_text = price_div.get_text(strip=True) if price_div else "0"
    user = user_div.get_text(strip=True) if user_div else "Неизвестный"
    short_desc_lower = monitor.normalize_match_text(short_description)
    
    print("\n--- Listing Item Level ---")
    print("Short Desc lower:", repr(short_desc_lower))
    print("Price Text:", repr(price_text))
    
    price_value = monitor.parse_price(price_text)
    print("Parsed price:", price_value)
    
    # Check exclude keywords on short description
    is_excluded = bool(monitor.contains_exclude_keyword(short_description, exclude_keywords, positive_keywords))
    print("Is excluded on short description:", is_excluded)
    if is_excluded:
        print("Exclude match:", monitor.contains_exclude_keyword(short_description, exclude_keywords, positive_keywords))
        
    # Check matched keyword
    matched_keyword = ""
    for keyword in search_keywords:
        pattern = r'\b' + re.escape(monitor.normalize_match_text(keyword)) + r'\b'
        if re.search(pattern, short_desc_lower):
            matched_keyword = keyword
            break
    print("Matched keyword on short description:", repr(matched_keyword))
    
    # Calculate effective max price
    skin_prices = [s.get('price', 0) for s in skins_dict.values()]
    effective_max_price = max(skin_prices, default=config.max_price) + config.pve_bonus
    if config.confirmed_pve_enabled:
        effective_max_price = max(effective_max_price, config.confirmed_pve_price)
    print("Effective max price:", effective_max_price)
    
    if price_value is None or price_value > effective_max_price:
        print(f"Skipped because price {price_value} > effective_max_price {effective_max_price}")
        return
        
    print("\n--- Detail Level (fetching offer details) ---")
    full_description, rating_text = await monitor.get_offer_details(target_item.get('href'))
    combined_text = short_description + " " + full_description
    
    # Check exclude keywords on combined text
    matched_exclude = monitor.contains_exclude_keyword(combined_text, exclude_keywords, positive_keywords)
    print("Is excluded on combined text:", repr(matched_exclude))
    
    # Run find_skins_in_text
    found_skins = monitor.find_skins_in_text(combined_text, skins_dict)
    print("Found skins:", found_skins)
    
    # has_pve flag
    has_pve_flag = monitor.has_pve(combined_text, include_unconfirmed=False)
    print("Has PVE Flag (confirmed only):", has_pve_flag)
    
    # Filter skins requiring PVE
    if found_skins and not has_pve_flag:
        all_skins_data = config.get_all_skins()
        filtered_skins = []
        for skin in found_skins:
            skin_cfg = all_skins_data.get(skin['id'], {})
            if skin_cfg.get('require_pve', False):
                print(f"🧟 Skin {skin['id']} requires PVE, but PVE not found — filter out")
            else:
                filtered_skins.append(skin)
        found_skins = filtered_skins
        print("Filtered found skins:", found_skins)
        
    pure_confirmed_pve_match = config.confirmed_pve_enabled and has_pve_flag
    should_skip = not found_skins and not pure_confirmed_pve_match
    print("Should skip (no skins/PVE match):", should_skip)
    if should_skip:
        return
        
    # Calculate price
    all_require_pve = found_skins and all(
        config.get_all_skins().get(s['id'], {}).get('require_pve', False) for s in found_skins
    )
    pve_for_price = False if all_require_pve else has_pve_flag
    my_max_price, price_breakdown = monitor.calculate_max_price(found_skins, pve_for_price)
    print(f"Calculated Max Price: {my_max_price} ({price_breakdown})")
    
    if price_value > my_max_price:
        print(f"Skipped because price {price_value} > my_max_price {my_max_price}")
    else:
        print("✅ SUCCESS! Offer passes all checks and would be sent!")

if __name__ == '__main__':
    asyncio.run(main())
