import os
import sqlite3
import threading
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(__file__), "price_history.db")
_DB_LOCK = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_price_history_db():
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_snapshot_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    rank_num INTEGER NOT NULL,
                    price_value REAL,
                    price_text TEXT,
                    seller TEXT,
                    href TEXT,
                    matched_kw TEXT,
                    FOREIGN KEY(snapshot_id) REFERENCES price_snapshots(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_snapshots_item ON price_snapshots(item_type, item_id, recorded_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_offers_snapshot ON price_snapshot_offers(snapshot_id, rank_num)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS red_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    item_name TEXT,
                    price_text TEXT,
                    href TEXT,
                    seller TEXT,
                    reason TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_red_flags_date ON red_flags(recorded_at DESC)"
            )


def record_price_snapshot(item_type, item_id, item_name, mode, results, source="minprice"):
    init_price_history_db()
    safe_results = list(results or [])[:3]
    recorded_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    recorded_day = recorded_at.split(" ", 1)[0]

    with _DB_LOCK:
        with _connect() as conn:
            last_same_day = conn.execute(
                """
                SELECT ps.id, ps.recorded_at, ps.source, pso.price_value, pso.href
                FROM price_snapshots ps
                LEFT JOIN price_snapshot_offers pso
                  ON pso.snapshot_id = ps.id AND pso.rank_num = 1
                WHERE ps.item_type = ? AND ps.item_id = ? AND ps.mode = ?
                  AND substr(ps.recorded_at, 1, 10) = ?
                ORDER BY ps.id DESC
                LIMIT 1
                """,
                (item_type, item_id, mode, recorded_day),
            ).fetchone()

            current_top = safe_results[0] if safe_results else None
            if last_same_day:
                prev_source = last_same_day["source"]
                prev_href = last_same_day["href"]
                prev_price = last_same_day["price_value"]
                curr_href = current_top.get("href") if current_top else None
                curr_price = current_top.get("price") if current_top else None

                # Only dedupe if source matches — different source must always write
                # (so source='auto' can override stale source='minprice' records)
                if prev_source == source and prev_href == curr_href:
                    if prev_price is None and curr_price is None:
                        return None
                    if prev_price is not None and curr_price is not None and abs(prev_price - curr_price) < 100:
                        return None

            cur = conn.execute(
                """
                INSERT INTO price_snapshots(recorded_at, item_type, item_id, item_name, mode, source, result_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (recorded_at, item_type, item_id, item_name, mode, source, len(safe_results)),
            )
            snapshot_id = cur.lastrowid
            for idx, result in enumerate(safe_results, start=1):
                conn.execute(
                    """
                    INSERT INTO price_snapshot_offers(snapshot_id, rank_num, price_value, price_text, seller, href, matched_kw)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        idx,
                        result.get("price"),
                        result.get("price_text"),
                        result.get("seller"),
                        result.get("href"),
                        result.get("matched_kw"),
                    ),
                )
    return snapshot_id


def record_red_flag(item_name, price_text, href, seller, reason):
    init_price_history_db()
    recorded_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO red_flags(recorded_at, item_name, price_text, href, seller, reason) VALUES (?,?,?,?,?,?)",
                (recorded_at, item_name, price_text, href, seller, reason),
            )


def get_red_flags(limit=50):
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT recorded_at, item_name, price_text, href, seller, reason FROM red_flags ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def clear_red_flags():
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM red_flags")


def clear_all_price_history():
    """Delete all data from price_history tables."""
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM price_snapshot_offers")
            conn.execute("DELETE FROM price_snapshots")


def get_price_summary():
    """Return aggregate stats: total snapshots, latest min price per item, date range."""
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
            first = conn.execute("SELECT MIN(recorded_at) FROM price_snapshots").fetchone()[0]
            last = conn.execute("SELECT MAX(recorded_at) FROM price_snapshots").fetchone()[0]
            rows = conn.execute(
                """
                SELECT ps.id, ps.item_type, ps.item_id, ps.item_name, ps.mode,
                       pso.price_value, pso.price_text, pso.href, ps.recorded_at, ps.source
                FROM price_snapshots ps
                LEFT JOIN price_snapshot_offers pso ON pso.snapshot_id = ps.id AND pso.rank_num = 1
                WHERE ps.id IN (
                    SELECT MAX(id) FROM price_snapshots
                    GROUP BY item_type, item_id, mode
                )
                ORDER BY ps.id DESC
                """
            ).fetchall()
    return {
        'total_snapshots': total,
        'first_date': first,
        'last_date': last,
        'latest_prices': [dict(r) for r in rows],
    }


def get_item_offers_unique(item_type, item_id, limit=80):
    """Get recent unique offers (by href) for an item, newest first."""
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT pso.price_value, pso.price_text, pso.href, pso.seller,
                       ps.recorded_at, ps.mode, ps.item_name
                FROM price_snapshots ps
                JOIN price_snapshot_offers pso ON pso.snapshot_id = ps.id AND pso.rank_num = 1
                WHERE ps.item_type = ? AND ps.item_id = ?
                ORDER BY ps.id DESC
                LIMIT ?
                """,
                (item_type, item_id, limit),
            ).fetchall()
    seen = set()
    unique = []
    for r in rows:
        d = dict(r)
        href = d.get('href') or ''
        key = (href, d.get('mode', '')) if href else (str(d.get('price_value')), d.get('mode', ''))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def get_latest_top3(item_type, item_id, mode='any'):
    """Get top-3 offers from the latest snapshot for an item+mode."""
    init_price_history_db()
    with _DB_LOCK:
        with _connect() as conn:
            snap = conn.execute(
                "SELECT id, recorded_at, source FROM price_snapshots WHERE item_type=? AND item_id=? AND mode=? ORDER BY id DESC LIMIT 1",
                (item_type, item_id, mode),
            ).fetchone()
            if not snap:
                return []
            rows = conn.execute(
                "SELECT rank_num, price_value, price_text, seller, href FROM price_snapshot_offers WHERE snapshot_id=? ORDER BY rank_num",
                (snap['id'],),
            ).fetchall()
    return [dict(r) for r in rows]


def get_price_history(item_type, item_id, mode=None, limit=12):
    init_price_history_db()
    params = [item_type, item_id]
    query = """
        SELECT id, recorded_at, item_type, item_id, item_name, mode, source, result_count
        FROM price_snapshots
        WHERE item_type = ? AND item_id = ?
    """
    if mode and mode != "all":
        query += " AND mode = ?"
        params.append(mode)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _DB_LOCK:
        with _connect() as conn:
            snapshots = [dict(row) for row in conn.execute(query, params).fetchall()]
            for snapshot in snapshots:
                offers = conn.execute(
                    """
                    SELECT rank_num, price_value, price_text, seller, href, matched_kw
                    FROM price_snapshot_offers
                    WHERE snapshot_id = ?
                    ORDER BY rank_num ASC
                    """,
                    (snapshot["id"],),
                ).fetchall()
                snapshot["offers"] = [dict(row) for row in offers]
    return snapshots
