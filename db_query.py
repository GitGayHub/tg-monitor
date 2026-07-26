import os
import sqlite3
import sys

# Путь от файла, а не от текущей папки: иначе sqlite молча создаёт пустую базу
# рядом и запрос падает на "no such table".
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'price_history.db')

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    conn = sqlite3.connect(DB_PATH)
    try:
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
    finally:
        conn.close()

if __name__ == '__main__':
    main()
