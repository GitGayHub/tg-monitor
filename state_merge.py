"""Слияние файлов состояния вместо перезаписи чужой версии своей.

Состояние пишут три стороны: локальный запуск под launcher.py, GitHub Actions и
второй компьютер. Раньше обе стороны после git pull накатывали свои
до-пуловые копии поверх чужих (`cp` в monitor.yml, restore_files в launcher.py),
поэтому побеждал тот, кто запушил последним: чужие просмотренные лоты
исчезали и приходили повторные уведомления, а из price_history.db пропадали
строки другой стороны.

Здесь каждый файл объединяется по своему смыслу: списки — объединением,
словари — по ключу с выбором более свежей записи, база — вставкой недостающих
строк по естественному ключу.

Использование:
    python state_merge.py <ours_dir> <theirs_dir> [файл ...]

`ours_dir` — рабочая копия (в неё пишется результат), `theirs_dir` — версия,
пришедшая из git. Файлы, отсутствующие с любой стороны, просто пропускаются.
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

LINE_FILES = ("seen_ids.txt", "banned_ids.txt", "banned_sellers.txt")
DICT_FILES = ("seen_cache.json", "seller_map.json")
RUN_LOG_MAX = 50
META_FILE = "state_meta.json"

# Бан-листы правит только пользователь, и удаление из них объединением выразить
# нельзя. Для них берём целиком ту сторону, которую правили позже.
LWW_FILES = ("banned_ids.txt", "banned_sellers.txt")
# Файлы, которые сбрасываются одной кнопкой «сброс мониторинга».
SEEN_STATE_KEY = "seen_state"
SEEN_STATE_FILES = ("seen_ids.txt", "seen_cache.json", "sent_offers.json")


def _meta(directory):
    data = _read_json(os.path.join(directory, META_FILE), {})
    if not isinstance(data, dict):
        data = {}
    modified = data.get("modified_at") or {}
    cleared = data.get("cleared_at") or {}
    return (modified if isinstance(modified, dict) else {},
            cleared if isinstance(cleared, dict) else {})


def _ts(mapping, key):
    try:
        return float(mapping.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _cleared_side(ours_dir, theirs_dir, key):
    """Какая сторона осознанно очистила состояние позже правок другой стороны.

    Возвращает 'ours', 'theirs' или None (обычное объединение).
    """
    ours_mod, ours_clr = _meta(ours_dir)
    theirs_mod, theirs_clr = _meta(theirs_dir)
    ours_cleared = _ts(ours_clr, key)
    theirs_cleared = _ts(theirs_clr, key)
    if ours_cleared and ours_cleared > _ts(theirs_mod, key) and ours_cleared >= theirs_cleared:
        return "ours"
    if theirs_cleared and theirs_cleared > _ts(ours_mod, key) and theirs_cleared > ours_cleared:
        return "theirs"
    return None


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_text(path, text):
    tmp = path + ".merge_tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def merge_lines(ours_path, theirs_path):
    """Объединение построчных списков id с сохранением порядка."""
    theirs = _read_text(theirs_path).splitlines() if os.path.exists(theirs_path) else []
    ours = _read_text(ours_path).splitlines() if os.path.exists(ours_path) else []
    seen = set()
    merged = []
    for line in theirs + ours:
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            merged.append(line)
    _write_text(ours_path, "".join(l + "\n" for l in merged))
    return len(merged)


def _seen_entry_rank(value):
    """Насколько запись информативна: с ценой лучше, чем заглушка [None, ""]."""
    if not isinstance(value, list) or not value:
        return -1
    price = value[0]
    desc = value[-1] if len(value) > 1 else ""
    return (1 if price is not None else 0) + (1 if desc else 0)


def merge_seen_cache(ours_path, theirs_path):
    ours = _read_json(ours_path, {})
    theirs = _read_json(theirs_path, {})
    if not isinstance(ours, dict):
        ours = {}
    if not isinstance(theirs, dict):
        theirs = {}
    merged = dict(theirs)
    for oid, value in ours.items():
        if oid not in merged or _seen_entry_rank(value) >= _seen_entry_rank(merged[oid]):
            merged[oid] = value
    _write_text(ours_path, json.dumps(merged, ensure_ascii=False, indent=2))
    return len(merged)


def merge_plain_dict(ours_path, theirs_path):
    """seller_map: ключи — случайные id, конфликтов по смыслу нет."""
    ours = _read_json(ours_path, {})
    theirs = _read_json(theirs_path, {})
    merged = dict(theirs) if isinstance(theirs, dict) else {}
    if isinstance(ours, dict):
        merged.update(ours)
    _write_text(ours_path, json.dumps(merged, ensure_ascii=False, indent=1))
    return len(merged)


def merge_sent_offers(ours_path, theirs_path):
    """При конфликте оставляем запись с более поздним timestamp."""
    ours = _read_json(ours_path, {})
    theirs = _read_json(theirs_path, {})
    if not isinstance(ours, dict):
        ours = {}
    if not isinstance(theirs, dict):
        theirs = {}

    def stamp(rec):
        if isinstance(rec, dict):
            try:
                return float(rec.get("timestamp") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    merged = dict(theirs)
    for oid, rec in ours.items():
        if oid not in merged or stamp(rec) >= stamp(merged[oid]):
            merged[oid] = rec
    _write_text(ours_path, json.dumps(merged, ensure_ascii=False, indent=2))
    return len(merged)


def merge_run_log(ours_path, theirs_path):
    """Диагностический журнал: объединяем записи и оставляем последние 50.

    Порядок обязателен хронологический: monitor.yml берёт logs[-1] как самую
    свежую запись, чтобы отличить cron-job.org от самозапуска.
    """
    ours = _read_json(ours_path, [])
    theirs = _read_json(theirs_path, [])
    if not isinstance(ours, list):
        ours = []
    if not isinstance(theirs, list):
        theirs = []
    merged = []
    seen = set()
    for entry in theirs + ours:
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    # time записан как "ГГГГ-ММ-ДД ЧЧ:ММ UTC" — сортируется как строка.
    merged.sort(key=lambda e: str((e or {}).get("time", "")) if isinstance(e, dict) else "")
    merged = merged[-RUN_LOG_MAX:]
    _write_text(ours_path, json.dumps(merged, ensure_ascii=False, indent=1))
    return len(merged)


SNAPSHOT_KEY_COLS = ("recorded_at", "item_type", "item_id", "mode", "source")
RED_FLAG_KEY_COLS = ("recorded_at", "item_name", "href", "reason")


def _recorded_at_ts(value):
    """'ДД.ММ.ГГГГ ЧЧ:ММ' → unix-время. None, если разобрать не удалось."""
    try:
        return datetime.strptime(str(value), "%d.%m.%Y %H:%M").timestamp()
    except (TypeError, ValueError):
        return None


def merge_price_history(ours_path, theirs_path, snapshots_cleared_at=0.0, red_flags_cleared_at=0.0):
    """Вставляет в нашу базу снимки и красные флаги, которых в ней нет.

    id автоинкрементные и на обеих сторонах расходятся, поэтому сравниваем по
    естественному ключу, а офферы переносим вместе с новым id снимка.

    Строки, записанные не позже осознанной очистки истории, не переносим —
    иначе кнопка «очистить историю» отменялась бы следующим же слиянием.
    """
    if not os.path.exists(theirs_path):
        return 0
    if not os.path.exists(ours_path):
        shutil.copy2(theirs_path, ours_path)
        return 0

    added_snapshots = 0
    added_flags = 0
    conn = sqlite3.connect(ours_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS theirs", (theirs_path,))
        try:
            have = {
                tuple(row[c] for c in SNAPSHOT_KEY_COLS)
                for row in conn.execute(
                    f"SELECT {', '.join(SNAPSHOT_KEY_COLS)} FROM main.price_snapshots"
                )
            }
            their_rows = conn.execute(
                "SELECT id, recorded_at, item_type, item_id, item_name, mode, source, result_count"
                " FROM theirs.price_snapshots ORDER BY id"
            ).fetchall()
            for row in their_rows:
                key = tuple(row[c] for c in SNAPSHOT_KEY_COLS)
                if key in have:
                    continue
                if snapshots_cleared_at:
                    row_ts = _recorded_at_ts(row["recorded_at"])
                    if row_ts is not None and row_ts <= snapshots_cleared_at:
                        continue
                have.add(key)
                cur = conn.execute(
                    "INSERT INTO main.price_snapshots"
                    " (recorded_at, item_type, item_id, item_name, mode, source, result_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["recorded_at"], row["item_type"], row["item_id"], row["item_name"],
                     row["mode"], row["source"], row["result_count"]),
                )
                new_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO main.price_snapshot_offers"
                    " (snapshot_id, rank_num, price_value, price_text, seller, href, matched_kw)"
                    " SELECT ?, rank_num, price_value, price_text, seller, href, matched_kw"
                    " FROM theirs.price_snapshot_offers WHERE snapshot_id = ?",
                    (new_id, row["id"]),
                )
                added_snapshots += 1

            have_flags = {
                tuple(r[c] for c in RED_FLAG_KEY_COLS)
                for r in conn.execute(
                    f"SELECT {', '.join(RED_FLAG_KEY_COLS)} FROM main.red_flags"
                )
            }
            for row in conn.execute(
                "SELECT recorded_at, item_name, price_text, href, seller, reason"
                " FROM theirs.red_flags ORDER BY id"
            ).fetchall():
                key = tuple(row[c] for c in RED_FLAG_KEY_COLS)
                if key in have_flags:
                    continue
                if red_flags_cleared_at:
                    row_ts = _recorded_at_ts(row["recorded_at"])
                    if row_ts is not None and row_ts <= red_flags_cleared_at:
                        continue
                have_flags.add(key)
                conn.execute(
                    "INSERT INTO main.red_flags"
                    " (recorded_at, item_name, price_text, href, seller, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (row["recorded_at"], row["item_name"], row["price_text"],
                     row["href"], row["seller"], row["reason"]),
                )
                added_flags += 1
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE theirs")
    finally:
        conn.close()
    return added_snapshots + added_flags


def merge_meta(ours_path, theirs_path):
    """Метки времени: по каждому ключу берём максимум с двух сторон."""
    ours = _read_json(ours_path, {})
    theirs = _read_json(theirs_path, {})
    merged = {"modified_at": {}, "cleared_at": {}}
    for section in ("modified_at", "cleared_at"):
        for source in (theirs, ours):
            values = (source or {}).get(section) or {}
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if value > merged[section].get(key, 0):
                    merged[section][key] = value
    _write_text(ours_path, json.dumps(merged, ensure_ascii=False, indent=1))
    return len(merged["modified_at"]) + len(merged["cleared_at"])


MERGERS = {
    "seen_cache.json": merge_seen_cache,
    "sent_offers.json": merge_sent_offers,
    "seller_map.json": merge_plain_dict,
    "run_log.json": merge_run_log,
    "price_history.db": merge_price_history,
    META_FILE: merge_meta,
}
for _name in LINE_FILES:
    MERGERS[_name] = merge_lines

DEFAULT_FILES = tuple(MERGERS)


def merge_file(name, ours_dir, theirs_dir):
    """Сливает один файл. Возвращает текст результата для лога."""
    merger = MERGERS.get(name)
    if merger is None:
        return f"{name}: нет правила слияния, пропуск"
    ours_path = os.path.join(ours_dir, name)
    theirs_path = os.path.join(theirs_dir, name)
    if not os.path.exists(ours_path) and not os.path.exists(theirs_path):
        return f"{name}: нет ни с одной стороны, пропуск"
    if not os.path.exists(ours_path):
        shutil.copy2(theirs_path, ours_path)
        return f"{name}: взят из git (локального нет)"
    if not os.path.exists(theirs_path):
        return f"{name}: нет версии из git, оставлена локальная"

    # ── Осознанное удаление важнее объединения ───────────────────────────────
    # Бан-листы: удаление нельзя выразить объединением, поэтому берём целиком
    # ту сторону, которую правили позже.
    if name in LWW_FILES:
        ours_mod, _ = _meta(ours_dir)
        theirs_mod, _ = _meta(theirs_dir)
        ours_ts, theirs_ts = _ts(ours_mod, name), _ts(theirs_mod, name)
        if theirs_ts > ours_ts:
            shutil.copy2(theirs_path, ours_path)
            return f"{name}: версия из git новее, взята целиком (правки списка не откатываются)"
        if ours_ts > theirs_ts:
            return f"{name}: локальная версия новее, оставлена целиком"
        # Времени правок нет ни у одной стороны — старое поведение (объединение).

    # Сброс мониторинга: очистившая сторона побеждает.
    if name in SEEN_STATE_FILES:
        side = _cleared_side(ours_dir, theirs_dir, SEEN_STATE_KEY)
        if side == "ours":
            return f"{name}: локально было очищено — объединение пропущено"
        if side == "theirs":
            shutil.copy2(theirs_path, ours_path)
            return f"{name}: очищено на другой стороне — взята её версия"

    kwargs = {}
    if name == "price_history.db":
        # Очистка истории должна пережить слияние: строки не позже очистки не
        # переносим. Берём максимум с двух сторон — метки времени сходятся.
        _, ours_clr = _meta(ours_dir)
        _, theirs_clr = _meta(theirs_dir)
        kwargs["snapshots_cleared_at"] = max(_ts(ours_clr, "price_history"), _ts(theirs_clr, "price_history"))
        kwargs["red_flags_cleared_at"] = max(_ts(ours_clr, "red_flags"), _ts(theirs_clr, "red_flags"))

    try:
        count = merger(ours_path, theirs_path, **kwargs)
    except Exception as e:
        # Молчаливая потеря состояния хуже, чем шумный отказ от слияния:
        # оставляем локальную версию как есть и сообщаем об этом.
        return f"{name}: ОШИБКА слияния ({e}), оставлена локальная версия"
    return f"{name}: объединено, элементов/добавлено {count}"


def main():
    if len(sys.argv) < 3:
        print("Usage: python state_merge.py <ours_dir> <theirs_dir> [файл ...]")
        return 1
    ours_dir, theirs_dir = sys.argv[1], sys.argv[2]
    names = sys.argv[3:] or list(DEFAULT_FILES)
    # Метки времени нужны остальным правилам, поэтому сливаем их последними,
    # а читаем — из исходных каталогов до перезаписи.
    names = [n for n in names if n != META_FILE] + ([META_FILE] if META_FILE in names or not sys.argv[3:] else [])
    for name in names:
        print("  " + merge_file(name, ours_dir, theirs_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
