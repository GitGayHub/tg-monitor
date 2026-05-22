import sqlite3
import sys

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    conn = sqlite3.connect('price_history.db')
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT s.id, s.recorded_at, s.item_id, s.item_name, s.mode, o.price_value, o.price_text, o.href
        FROM price_snapshots s
        JOIN price_snapshot_offers o ON s.id = o.snapshot_id
        ORDER BY s.id DESC
        LIMIT 40
    """)
    print("Latest snapshots for all items:")
    for r in cursor.fetchall():
        print(r)

if __name__ == '__main__':
    main()
