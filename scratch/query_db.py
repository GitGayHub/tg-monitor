import sqlite3
import sys

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    conn = sqlite3.connect('price_history.db')
    c = conn.cursor()
    
    # Let's inspect the schema of both tables
    c.execute("PRAGMA table_info(price_snapshots)")
    print("price_snapshots schema:", c.fetchall())
    c.execute("PRAGMA table_info(price_snapshot_offers)")
    print("price_snapshot_offers schema:", c.fetchall())
    
    # Query for the target offer
    c.execute("""
        SELECT s.id, s.recorded_at, s.item_type, s.item_id, s.item_name, s.mode, s.source, 
               o.price_value, o.price_text, o.seller, o.href
        FROM price_snapshots s
        JOIN price_snapshot_offers o ON s.id = o.snapshot_id
        WHERE o.href LIKE '%68759785%'
    """)
    rows = c.fetchall()
    print(f"\nFound {len(rows)} snapshot entries for offer 68759785:")
    for row in rows:
        print(row)
        
if __name__ == '__main__':
    main()
