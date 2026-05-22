import asyncio
import sys
import os
from bs4 import BeautifulSoup

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import monitor

async def main():
    sys.stdout.reconfigure(encoding='utf-8')
    offer_id = "68759785"
    
    print("Fetching listings...")
    listings = await monitor.get_listings()
    print(f"Total listings fetched: {len(listings)}")
    
    target_item = None
    for item in listings:
        href = item.get('href', '')
        if offer_id in href:
            target_item = item
            break
            
    if target_item is None:
        print(f"Offer {offer_id} not found in live listings.")
        return
        
    print("\n--- Live Listing Found ---")
    desc_div = target_item.find('div', class_='tc-desc-text')
    short_desc = desc_div.get_text(strip=True) if desc_div else ""
    print("Short Description:", repr(short_desc))
    
    price_div = target_item.find('div', class_='tc-price')
    price_text = price_div.get_text(strip=True) if price_div else ""
    print("Price Text:", repr(price_text))
    
    # Run parsing
    full_desc, rating = await monitor.get_offer_details(target_item.get('href'))
    print("Full Description:", repr(full_desc))
    
    combined = short_desc + " " + (full_desc or "")
    skins_dict = monitor.config.get_all_skins()
    found_skins = monitor.find_skins_in_text(combined, skins_dict)
    print("Found Skins:", found_skins)
    
    has_confirmed = monitor.has_pve(combined, include_unconfirmed=False)
    has_unconfirmed = monitor.has_pve(combined, include_unconfirmed=True)
    print("Has Confirmed PVE:", has_confirmed)
    print("Has Unconfirmed PVE:", has_unconfirmed)

if __name__ == '__main__':
    asyncio.run(main())
