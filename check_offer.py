import sys
import asyncio
import os
import requests
from bs4 import BeautifulSoup

# Ensure correct folder import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from monitor.py if it is importable, or we can just import the key functions.
# Let's read the code from monitor.py and call its functions.
import app

async def main():
    sys.stdout.reconfigure(encoding='utf-8')
    offer_id = "68759785"
    url = f"https://funpay.com/lots/offer?id={offer_id}"
    
    # Check seen_ids
    seen = False
    if os.path.exists("seen_ids.txt"):
        with open("seen_ids.txt", "r") as f:
            for line in f:
                if line.strip() == offer_id:
                    seen = True
                    break
    print(f"Is {offer_id} in seen_ids.txt?", seen)
    
    # Fetch details
    print(f"Fetching offer details for {offer_id}...")
    full_description, rating_text = await monitor.get_offer_details(url)
    
    # Price
    session = monitor.build_http_session()
    response = session.get(url, headers={'User-Agent': monitor.get_random_user_agent()})
    soup = BeautifulSoup(response.text, 'html.parser')
    price_div = soup.find('span', class_='payment-value')
    price_text = price_div.text.strip() if price_div else "0"
    price_val = float(price_text.replace(" ", "").replace("₽", "")) if price_div else 0.0
    
    print("\n--- Offer Details ---")
    print("Price:", price_val)
    print("Rating Text:", rating_text)
    print("Full Description (first 200 chars):", repr(full_description[:200]) if full_description else None)
    
    # Let's run find_skins_in_text and has_pve
    skins_dict = monitor.config.get_all_skins()
    combined_text = (full_description + " " + "Black Knight").lower() # simulate description
    print("\n--- Running checks on combined_text ---")
    
    found_skins = monitor.find_skins_in_text(combined_text, skins_dict)
    print("Found skins:", found_skins)
    
    has_pve_flag = monitor.has_pve(combined_text, include_unconfirmed=False)
    has_unconfirmed_pve = monitor.has_pve(combined_text, include_unconfirmed=True)
    print("Has Confirmed PVE:", has_pve_flag)
    print("Has Unconfirmed PVE:", has_unconfirmed_pve)
    
    # Let's run the main logic check
    for skin in found_skins:
        skin_cfg = skins_dict.get(skin['id'], {})
        print(f"Skin: {skin['id']}, require_pve: {skin_cfg.get('require_pve', False)}")

if __name__ == '__main__':
    asyncio.run(main())
