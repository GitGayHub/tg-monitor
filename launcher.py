"""Launcher: git pull -> run bot -> git push. Auto-resolves state conflicts."""
import subprocess
import sys
import os
import re
import shutil
import signal
import atexit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(REPO, "app.py")
LOCK_FILE = os.path.join(REPO, ".bot.lock")
# banned_sellers.txt / seller_map.json / run_log.json пишет app.py и коммитит CI,
# но раньше их тут не было: бан продавца с телефона не доезжал до Actions, и лоты
# этого продавца продолжали приходить.
STATE_SYNC = ["seen_ids.txt", "seen_cache.json", "sent_offers.json", "banned_ids.txt",
              "banned_sellers.txt", "seller_map.json", "run_log.json", "state_meta.json",
              "config.json", "config.json.enc", "price_history.db", "mode.txt"]
STATE_PROTECTED = STATE_SYNC + ["price_history.db-shm", "price_history.db-wal"]
STATE_SET = {p.replace("\\", "/") for p in STATE_PROTECTED}
STATE_COMMIT_PREFIXES = ("Sync state after run", "Update monitor state")
TEXT_KW = {"text": True, "encoding": "utf-8", "errors": "replace"}

# `-c safe.directory=...` neutralises Git's "dubious ownership" error that
# happens when the repo folder is owned by a different user (e.g. created by
# Administrator, run by a regular account). Injecting it into every call
# means the launcher works without any one-off `git config --global` step.
_REPO_SAFE = REPO.replace("\\", "/")
GIT_BASE = ["git", "-c", f"safe.directory={_REPO_SAFE}"]

# Авторизация без записи токена на диск. Помощник — короткий shell-скрипт: сам
# токен он берёт из переменной окружения в момент запроса, поэтому не попадает
# ни в .git/config, ни в список аргументов процесса, ни в сообщения git об
# ошибках (раньше токен вписывался прямо в URL origin и светился везде).
if os.environ.get("GITHUB_TOKEN"):
    GIT_BASE += [
        "-c",
        "credential.https://github.com.helper="
        "!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f",
        "-c", "credential.https://github.com.useHttpPath=false",
    ]


def mask_secrets(text):
    """Прячет токен из вывода git.

    Страховка на случай старых копий репозитория, где токен ещё вписан в URL
    origin: git печатает такой URL в сообщениях об ошибках
    ("fatal: unable to access 'https://<токен>@github.com/...'").
    """
    if not text:
        return text
    masked = re.sub(r'(https://)[^@/\s]+(@github\.com)', r'\1***\2', text)
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        masked = masked.replace(token, '***')
    return masked


def git(*args, visible=False, timeout=None):
    kwargs = {"cwd": REPO}
    if not visible:
        kwargs["capture_output"] = True
        kwargs.update(TEXT_KW)
    if timeout:
        kwargs["timeout"] = timeout
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    kwargs["env"] = env
    return subprocess.run(GIT_BASE + list(args), **kwargs)


def git_has_staged(paths=None):
    args = ["diff", "--cached", "--quiet"]
    if paths:
        args.extend(["--", *paths])
    return git(*args).returncode != 0


def norm_path(path):
    return path.replace("\\", "/")


def upstream_ref():
    r = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ref = (r.stdout or "").strip()
    if r.returncode != 0 or not ref:
        return None
    return ref


def commit_subject(commit):
    r = git("log", "-1", "--format=%s", commit)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def commit_files(commit):
    r = git("diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if r.returncode != 0:
        return set()
    return {norm_path(line.strip()) for line in (r.stdout or "").splitlines() if line.strip()}


def is_state_commit(commit):
    files = commit_files(commit)
    subject = commit_subject(commit)
    return bool(files) and files <= STATE_SET and any(subject.startswith(p) for p in STATE_COMMIT_PREFIXES)


def local_ahead_commits(upstream):
    r = git("rev-list", "--reverse", f"{upstream}..HEAD")
    if r.returncode != 0:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]


def drop_local_state_commits():
    """If all local commits ahead of upstream are state-only, reset to upstream
    so that rebase doesn't create merge conflicts with remote state updates."""
    upstream = upstream_ref()
    if not upstream:
        return True
    commits = local_ahead_commits(upstream)
    if not commits:
        return True
    unsafe = [c for c in commits if not is_state_commit(c)]
    if unsafe:
        print(f"INFO: keeping {len(commits)} local commit(s); not all are state-only.")
        return True
    print(f"INFO: converting {len(commits)} local state sync commit(s) into working-tree changes.")
    return git("reset", "--mixed", upstream, visible=True).returncode == 0


def dirty_state_files():
    r = git("status", "--porcelain", "--untracked-files=no", "--", *STATE_PROTECTED)
    if r.returncode != 0:
        return []
    paths = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1].strip().strip('"')
        if norm_path(path) in STATE_SET and path not in paths:
            paths.append(path)
    return paths


def backup_files(paths):
    snapshot = {}
    for path in paths:
        full = os.path.join(REPO, path)
        if os.path.exists(full):
            with open(full, "rb") as f:
                snapshot[path] = f.read()
        else:
            snapshot[path] = None
    return snapshot


def restore_files(snapshot):
    for path, data in snapshot.items():
        full = os.path.join(REPO, path)
        if data is None:
            try:
                os.remove(full)
            except FileNotFoundError:
                pass
            continue
        os.makedirs(os.path.dirname(full) or REPO, exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)


def merge_back_state(snapshot):
    """Объединяет наше состояние с подтянутым из git вместо перезаписи.

    Раньше здесь стоял простой restore_files: наши до-пуловые копии ложились
    поверх свежих чужих, поэтому просмотренные лоты и строки истории цен,
    добавленные на другой машине или в Actions, пропадали, а лоты приходили
    повторно. Файлы без правила слияния (конфиг, mode.txt) восстанавливаем как
    раньше — там побеждает локальная версия осознанно.
    """
    if not snapshot:
        return
    try:
        if REPO not in sys.path:
            sys.path.append(REPO)
        import state_merge
    except Exception as e:
        print(f"WARNING: state_merge недоступен ({e}) — восстанавливаю локальные копии без слияния.")
        restore_files(snapshot)
        return

    import tempfile
    plain = {p: d for p, d in snapshot.items() if os.path.basename(p) not in state_merge.MERGERS}
    mergeable = {p: d for p, d in snapshot.items() if os.path.basename(p) in state_merge.MERGERS}
    restore_files(plain)

    if not mergeable:
        return
    tmp_root = tempfile.mkdtemp(prefix="state_merge_")
    try:
        ours_dir = os.path.join(tmp_root, "ours")
        theirs_dir = os.path.join(tmp_root, "theirs")
        os.makedirs(ours_dir)
        os.makedirs(theirs_dir)
        for path, data in mergeable.items():
            name = os.path.basename(path)
            if data is not None:
                with open(os.path.join(ours_dir, name), "wb") as f:
                    f.write(data)
            live = os.path.join(REPO, path)
            if os.path.exists(live):
                shutil.copy2(live, os.path.join(theirs_dir, name))

        # Метки времени нужны правилам всегда, даже если сам state_meta.json не
        # менялся: без них слияние не отличит осознанную очистку от потери данных.
        live_meta = os.path.join(REPO, state_merge.META_FILE)
        if os.path.exists(live_meta):
            for target in (ours_dir, theirs_dir):
                dest = os.path.join(target, state_merge.META_FILE)
                if not os.path.exists(dest):
                    shutil.copy2(live_meta, dest)

        # state_meta.json сливаем последним: остальные правила читают из него
        # метки очисток, и перезапись файла до них исказила бы решения.
        ordered = ([p for p in mergeable if os.path.basename(p) != state_merge.META_FILE]
                   + [p for p in mergeable if os.path.basename(p) == state_merge.META_FILE])
        for path in ordered:
            name = os.path.basename(path)
            print("  " + state_merge.merge_file(name, ours_dir, theirs_dir))
            merged = os.path.join(ours_dir, name)
            if os.path.exists(merged):
                shutil.copy2(merged, os.path.join(REPO, path))
    except Exception as e:
        print(f"WARNING: слияние состояния не удалось ({e}) — восстанавливаю локальные копии.")
        restore_files(mergeable)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def protect_state_for_sync():
    """Backup dirty state files and clean them so git rebase works cleanly."""
    paths = dirty_state_files()
    if not paths:
        return {}
    snapshot = backup_files(paths)
    print(f"INFO: protecting local state during git sync: {', '.join(paths)}")
    clean = git("checkout", "--", *paths)
    if clean.returncode != 0:
        print("WARNING: could not temporarily clean all state files; continuing with autostash.")
    return snapshot


def undo_last_state_commit():
    """If push fails, undo the state commit but keep files as working-tree changes."""
    if not is_state_commit("HEAD"):
        print("WARNING: push failed, but HEAD is not a launcher state commit; keeping commit.")
        return False
    r = git("reset", "--mixed", "HEAD~1", visible=True)
    if r.returncode == 0:
        print("INFO: local state commit undone; state files kept as working-tree changes.")
        return True
    return False


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            **TEXT_KW,
        )
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                lines = f.read().splitlines()
            if lines and lines[0].strip() == str(os.getpid()):
                os.remove(LOCK_FILE)
    except OSError:
        pass


def is_our_bot_process(pid):
    """Проверяет, что процесс — это действительно наш бот из этого каталога.

    После перезагрузки PID из старого .bot.lock достаётся постороннему процессу,
    и прежняя проверка «процесс с таким PID существует» приводила к taskkill
    случайной чужой программы при каждом запуске.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    repo_marker = os.path.basename(REPO).lower()

    def _run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, **TEXT_KW)
            return (r.stdout or "")
        except (OSError, subprocess.SubprocessError):
            # wmic отсутствует в свежих сборках Windows и роняет запуск лаунчера,
            # если не поймать здесь.
            return ""

    cmdline = ""
    if os.name == "nt":
        cmdline = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid_int}\").CommandLine"])
        if not cmdline.strip():
            cmdline = _run(["wmic", "process", "where", f"ProcessId={pid_int}",
                            "get", "CommandLine", "/value"])
    else:
        try:
            with open(f"/proc/{pid_int}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            cmdline = _run(["ps", "-p", str(pid_int), "-o", "args="])

    cmdline = cmdline.lower()
    if cmdline.strip():
        return repo_marker in cmdline and ("app.py" in cmdline or "launcher.py" in cmdline)

    # Командную строку узнать не удалось. Ориентируемся хотя бы на имя процесса:
    # чужую непитоновскую программу не трогаем, питоновскую считаем своим ботом,
    # чтобы не оставить два экземпляра с дублями уведомлений.
    if os.name == "nt":
        name = _run(["tasklist", "/FI", f"PID eq {pid_int}", "/FO", "CSV", "/NH"]).lower()
    else:
        name = _run(["ps", "-p", str(pid_int), "-o", "comm="]).lower()
    return "python" in name


def acquire_lock():
    """Prevent multiple bot instances from running simultaneously. Kills stale ones."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pids = [line.strip() for line in f.read().splitlines() if line.strip()]
        except OSError:
            pids = []
        for old_pid in pids:
            if not old_pid or old_pid == str(os.getpid()) or not pid_alive(old_pid):
                continue
            if not is_our_bot_process(old_pid):
                print(f"INFO: PID {old_pid} из .bot.lock занят посторонним процессом — не трогаю его.")
                continue
            print(f"Stopping old bot process PID {old_pid}...")
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", old_pid], capture_output=True)
                else:
                    os.kill(int(old_pid), signal.SIGTERM)
            except Exception as e:
                print(f"WARNING: could not stop old process: {e}")
        try:
            os.remove(LOCK_FILE)
            stale_cleared = True
        except OSError as e:
            stale_cleared = False
            print(f"WARNING: не удалось удалить старый .bot.lock: {e}")
    else:
        stale_cleared = True

    # Создаём файл атомарно: два лаунчера, стартовавшие одновременно, раньше оба
    # проходили проверку os.path.exists и поднимали по боту — уведомления дублировались.
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):
        # PermissionError: на Windows удалённый файл может оставаться в состоянии
        # delete-pending, пока его держит антивирус или индексатор.
        if not stale_cleared:
            # Старый файл удалить не удалось, живого бота мы не нашли — не блокируем
            # запуск навсегда, просто перезаписываем, как было раньше.
            print("WARNING: перезаписываю .bot.lock, который не удалось удалить.")
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        else:
            print("ERROR: другой лаунчер уже запускается (.bot.lock занят). Выходим.")
            sys.exit(1)
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()}\n")
    atexit.register(release_lock)


def in_rebase():
    git_dir = os.path.join(REPO, ".git")
    return os.path.exists(os.path.join(git_dir, "rebase-merge")) or \
           os.path.exists(os.path.join(git_dir, "rebase-apply"))


def current_branch():
    r = git("symbolic-ref", "--short", "-q", "HEAD")
    return (r.stdout or "").strip()


def ensure_on_main():
    if not current_branch():
        print("WARNING: detached HEAD detected, switching to main...")
        git("checkout", "main", visible=True)


def clean_leftover_rebase():
    """Force remove rebase folders if git gets stuck."""
    git_dir = os.path.join(REPO, ".git")
    for folder in ["rebase-merge", "rebase-apply"]:
        path = os.path.join(git_dir, folder)
        if os.path.exists(path):
            print(f"INFO: cleaning leftover rebase folder {folder}...")
            git("rebase", "--abort")
            if os.path.exists(path):
                try:
                    shutil.rmtree(path)
                except Exception as e:
                    print(f"WARNING: could not remove {path}: {e}")


def merge_conflicted_state_file(rel_path):
    """Сливает обе стороны конфликта rebase вместо выбора одной.

    Раньше здесь безусловно бралась сторона --theirs (при rebase это локальный
    коммит), поэтому состояние, пришедшее из GitHub, просто отбрасывалось.
    Ступень :2 — версия upstream (то, что в origin), ступень :3 — наш коммит.
    """
    name = os.path.basename(rel_path)
    try:
        if REPO not in sys.path:
            sys.path.append(REPO)
        import state_merge
    except Exception:
        return False
    if name not in state_merge.MERGERS:
        return False

    import tempfile
    tmp_root = tempfile.mkdtemp(prefix="rebase_merge_")
    try:
        ours_dir = os.path.join(tmp_root, "ours")
        theirs_dir = os.path.join(tmp_root, "theirs")
        os.makedirs(ours_dir)
        os.makedirs(theirs_dir)
        for stage, target in ((2, ours_dir), (3, theirs_dir)):
            r = subprocess.run(
                GIT_BASE + ["show", f":{stage}:{rel_path}"],
                cwd=REPO, capture_output=True,
            )
            if r.returncode != 0:
                return False
            with open(os.path.join(target, name), "wb") as f:
                f.write(r.stdout)
        # Метки очисток берём из рабочей копии — иначе слияние решит, что
        # удаление было потерей данных, и вернёт строки обратно.
        live_meta = os.path.join(REPO, state_merge.META_FILE)
        if os.path.exists(live_meta) and name != state_merge.META_FILE:
            for target in (ours_dir, theirs_dir):
                shutil.copy2(live_meta, os.path.join(target, state_merge.META_FILE))
        print("  " + state_merge.merge_file(name, ours_dir, theirs_dir))
        shutil.copy2(os.path.join(ours_dir, name), os.path.join(REPO, rel_path))
        return True
    except Exception as e:
        print(f"WARNING: не удалось слить {rel_path} при rebase: {e}")
        return False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def resolve_state_rebase():
    """Auto-resolve rebase conflicts in state files by merging both sides."""
    guard = 0
    while in_rebase() and guard < 20:
        guard += 1
        status = git("status", "--porcelain")
        unmerged = []
        for line in (status.stdout or "").splitlines():
            if len(line) >= 2 and line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                unmerged.append(line[3:].strip())
        non_state = [f for f in unmerged if norm_path(f) not in STATE_SET]
        if non_state:
            print(f"ERROR: conflict in non-state files: {non_state}")
            return False
        if unmerged:
            for f in unmerged:
                if not merge_conflicted_state_file(f):
                    # Слияние невозможно (нет правила, удаление файла и т.п.) —
                    # оставляем локальную версию, как было раньше.
                    pick = git("checkout", "--theirs", "--", f)
                    if pick.returncode != 0:
                        git("checkout", "--ours", "--", f)
                git("add", "--", f)
        env = os.environ.copy()
        env["GIT_EDITOR"] = "true"
        cont = subprocess.run(GIT_BASE + ["rebase", "--continue"], cwd=REPO, env=env, capture_output=True, **TEXT_KW)
        if cont.returncode != 0:
            skip = subprocess.run(GIT_BASE + ["rebase", "--skip"], cwd=REPO, capture_output=True, **TEXT_KW)
            if skip.returncode != 0 and in_rebase():
                return False
    return not in_rebase()


def strip_token_from_remote():
    """Убирает токен из URL origin.

    Раньше токен вписывался прямо в remote URL, то есть лежал открытым текстом
    в .git/config и попадал в сообщения git об ошибках. Теперь авторизация идёт
    через credential helper (см. GIT_BASE), который читает GITHUB_TOKEN из
    окружения в момент запроса и на диск ничего не пишет.
    """
    remote_url = git("remote", "get-url", "origin")
    remote_str = (remote_url.stdout or "").strip()
    if not remote_str:
        return ""
    # Чистим URL только если есть чем авторизоваться взамен: на машине, где
    # GITHUB_TOKEN в окружении не задан, токен из URL — единственный способ
    # попасть в origin, и его удаление сломало бы синхронизацию.
    if not os.environ.get("GITHUB_TOKEN"):
        return remote_str
    clean = re.sub(r'https://[^@/\s]+@github\.com', 'https://github.com', remote_str)
    if clean != remote_str:
        print("  Убираю токен из URL origin (авторизация через credential helper)...")
        if git("remote", "set-url", "origin", clean).returncode == 0:
            remote_str = clean
    return remote_str


def recover_from_rewritten_history():
    """Восстанавливается, если история origin была переписана (force-push).

    Без этого локальная ветка и origin/main расходятся без общего предка, и
    обычный rebase пытается переиграть поверх новой базы все локальные коммиты
    (их тут десятки тысяч) — процесс зависает и конфликтует. Здесь мы жёстко
    переходим на версию origin, сохранив файлы состояния через слияние.
    """
    upstream = upstream_ref()
    if not upstream:
        return False

    base = git("merge-base", "HEAD", upstream)
    diverged = base.returncode != 0 or not (base.stdout or "").strip()
    if not diverged:
        return False

    print("\n⚠️  История на GitHub была переписана — общий предок отсутствует.")
    print("   Перехожу на версию из origin, состояние сохраняю слиянием.")
    snapshot = backup_files([p for p in STATE_SYNC if os.path.exists(os.path.join(REPO, p))])
    reset = git("reset", "--hard", upstream, visible=True)
    if reset.returncode != 0:
        print("   ❌ Не удалось перейти на версию origin — оставляю всё как есть.")
        restore_files(snapshot)
        return False
    merge_back_state(snapshot)
    print("   ✅ Локальная копия синхронизирована с новой историей.")
    return True


def sync_from_remote():
    print("=== [1/3] Pulling latest state from GitHub ===")
    remote_str = strip_token_from_remote()
    if remote_str:
        print(f"  Git remote: {mask_secrets(remote_str)}")
    else:
        print("  ⚠️ Git remote: not configured")
    try:
        # 30с не хватало: репозиторий растёт от коммитов состояния каждые ~5 минут,
        # и на медленном канале fetch отваливался, тихо отключая синхронизацию.
        # Вывод забираем себе, чтобы вычистить токен из сообщения об ошибке.
        fetch = git("fetch", "--prune", timeout=120)
    except subprocess.TimeoutExpired:
        print("\n⚠️ WARNING: git fetch timed out — starting bot with local state.")
        return
    if fetch.returncode != 0:
        print("\n⚠️ WARNING: git fetch failed — starting bot with local state.")
        print("   " + mask_secrets((fetch.stderr or "").strip())[:400])
        return
    print("  ✅ git fetch OK")

    # Если историю переписали, обычный rebase здесь бесполезен и опасен.
    if recover_from_rewritten_history():
        return

    if not drop_local_state_commits():
        print("WARNING: could not clean local state commits — continuing with normal rebase.")

    state_snapshot = protect_state_for_sync()
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        upstream = upstream_ref()
        if upstream:
            try:
                result = subprocess.run(GIT_BASE + ["rebase", "--autostash", upstream], cwd=REPO, env=env, timeout=60)
            except subprocess.TimeoutExpired:
                print("\n⚠️ WARNING: git rebase timed out — starting bot with local state.")
                return
        else:
            try:
                result = subprocess.run(GIT_BASE + ["pull", "--rebase", "--autostash"], cwd=REPO, env=env, timeout=60)
            except subprocess.TimeoutExpired:
                print("\n⚠️ WARNING: git pull timed out — starting bot with local state.")
                return
        if result.returncode != 0:
            if in_rebase():
                print("INFO: state conflict during rebase, auto-resolving...")
                if not resolve_state_rebase():
                    print("WARNING: could not auto-resolve rebase, aborting and continuing without sync.")
                    git("rebase", "--abort", visible=True)
            else:
                print("\nWARNING: git pull failed — starting bot with local state.")
    finally:
        if state_snapshot:
            print("INFO: merging local state with the version pulled from GitHub...")
            merge_back_state(state_snapshot)

    # Разрешаем любые конфликты слияния/autostash в рабочей директории
    status = git("status", "--porcelain")
    unmerged = []
    for line in (status.stdout or "").splitlines():
        if len(line) >= 2 and ('U' in line[:2] or line[:2] in ('AA', 'DD')):
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[-1].strip()
            unmerged.append(path_part.strip('"'))
            
    if unmerged:
        print(f"INFO: resolving conflicts in working tree: {unmerged}")
        for f in unmerged:
            pick = git("checkout", "--theirs", "--", f)
            if pick.returncode != 0:
                git("checkout", "--ours", "--", f)
            git("add", "--", f)
            git("reset", "--", f)



def resolve_config_after_sync():
    """Smart config resolution: compare local config.json with GitHub's
    config.json.enc and use whichever is newer.  Prevents stale config when
    switching between PCs."""
    passphrase = os.environ.get("CONFIG_PASSPHRASE")
    enc_path = os.path.join(REPO, "config.json.enc")
    cfg_path = os.path.join(REPO, "config.json")

    if not passphrase:
        if not os.path.exists(cfg_path) and os.path.exists(enc_path):
            print("⚠️  WARNING: config.json.enc exists but CONFIG_PASSPHRASE is not set!")
            print("   Set CONFIG_PASSPHRASE in set_env.bat to decrypt the config.")
        return

    # Reset config.json.enc to the latest git HEAD version.
    # protect_state_for_sync() may have restored an old backup over the
    # freshly-pulled version — undo that so we compare against the real remote.
    r = git("checkout", "HEAD", "--", "config.json.enc")
    if r.returncode != 0:
        print("  ⚠️  Could not reset config.json.enc to HEAD, using working-tree version.")

    if not os.path.exists(enc_path):
        print("  No config.json.enc found — skipping config resolution.")
        return

    print("Resolving config: local config.json vs GitHub config.json.enc...")

    # ── Decrypt remote .enc ──────────────────────────────────────────────────
    try:
        if REPO not in sys.path:
            sys.path.append(REPO)
        import config_crypt
        with open(enc_path, "rb") as f:
            remote_config = config_crypt.decrypt(f.read(), passphrase)
    except Exception as e:
        print(f"  ⚠️  Error decrypting config.json.enc: {e}")
        return

    # ── No local config → just use remote ────────────────────────────────────
    if not os.path.exists(cfg_path):
        print("  No local config.json found — using GitHub version.")
        with open(cfg_path, "wb") as f:
            f.write(remote_config)
        print("  ✅ Config decrypted from GitHub.")
        return

    # ── Read local config ────────────────────────────────────────────────────
    with open(cfg_path, "rb") as f:
        local_config = f.read()

    if local_config == remote_config:
        print("  ✅ Config identical on both sides — no update needed.")
        return

    # ── Content differs — compare timestamps ─────────────────────────────────
    # Remote: last git-commit time that touched config.json.enc
    r = git("log", "-1", "--format=%ct", "--", "config.json.enc")
    remote_ts = 0
    if r.returncode == 0 and (r.stdout or "").strip():
        try:
            remote_ts = int((r.stdout or "").strip())
        except ValueError:
            pass

    # Local: file modification time of config.json
    try:
        local_ts = int(os.path.getmtime(cfg_path))
    except OSError:
        local_ts = 0

    import datetime
    remote_dt = datetime.datetime.fromtimestamp(remote_ts).strftime("%Y-%m-%d %H:%M:%S") if remote_ts else "unknown"
    local_dt  = datetime.datetime.fromtimestamp(local_ts).strftime("%Y-%m-%d %H:%M:%S")  if local_ts  else "unknown"

    print(f"  Local  config.json    : {local_dt}")
    print(f"  GitHub config.json.enc: {remote_dt}")

    if remote_ts >= local_ts:
        print(f"  ✅ GitHub version is NEWER — updating local config.json")
        with open(cfg_path, "wb") as f:
            f.write(remote_config)
    else:
        print(f"  ✅ Local version is NEWER — keeping local config.json")


# ═══════════════════════════════════════════════════════════════════════════════
# Main execution
# ═══════════════════════════════════════════════════════════════════════════════

acquire_lock()
clean_leftover_rebase()

# ═══ Pre-flight validation ═══
print("=== [0/3] Validating tokens ===")
import urllib.request, urllib.error, json as _json

tg_token = os.environ.get('TELEGRAM_BOT_TOKEN')
if not tg_token:
    print("FATAL: TELEGRAM_BOT_TOKEN not set in environment!")
    print("Check set_env.bat — copy from set_env.example.bat and fill in real values.")
    sys.exit(1)

try:
    req = urllib.request.Request(f"https://api.telegram.org/bot{tg_token}/getMe")
    resp = urllib.request.urlopen(req, timeout=10)
    data = _json.loads(resp.read())
    if data.get("ok"):
        bot_info = data.get("result", {})
        print(f"  Telegram: ✅ @{bot_info.get('username', '?')}")
    else:
        print(f"  Telegram: ❌ getMe failed: {data}")
        sys.exit(1)
except urllib.error.HTTPError as e:
    if e.code == 401:
        print(f"  Telegram: ❌ TOKEN INVALID (401) — update TELEGRAM_BOT_TOKEN in set_env.bat")
    elif e.code == 404:
        print(f"  Telegram: ❌ TOKEN NOT FOUND (404) — bot deleted or token wrong. Update set_env.bat")
    else:
        print(f"  Telegram: ❌ HTTP {e.code}")
    sys.exit(1)
except Exception as e:
    print(f"  Telegram: ⚠️ Network error: {e}")
    # Don't exit — might be temporary network issue

chat_id_env = os.environ.get('TELEGRAM_CHAT_ID')
if not chat_id_env:
    print("  Chat ID: ⚠️ not set in env (will try chat_id.txt)")
else:
    print(f"  Chat ID: {chat_id_env}")

gh_token = os.environ.get('GITHUB_TOKEN')
if gh_token:
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        user = _json.loads(resp.read()).get("login", "?")
        print(f"  GitHub:    ✅ {user}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  GitHub:    ❌ TOKEN EXPIRED (401) — update GITHUB_TOKEN in set_env.bat")
        else:
            print(f"  GitHub:    ⚠️ HTTP {e.code}")
    except Exception as e:
        print(f"  GitHub:    ⚠️ {e}")
else:
    print("  GitHub:    ⚠️ not set (git may prompt for password)")

print()

if in_rebase():
    print("WARNING: leftover rebase detected, trying auto-repair...")
    if not resolve_state_rebase():
        clean_leftover_rebase()

ensure_on_main()

# Persistently enable autostash so any future `git pull --rebase` auto-handles dirty trees.
git("config", "rebase.autoStash", "true")

sync_from_remote()

resolve_config_after_sync()


print("\n=== [2/3] Starting bot (Ctrl+C to exit) ===\n")

os.environ["BOT_RUNNING_UNDER_LAUNCHER"] = "1"
# Ctrl+C должен гасить бота, а не лаунчер: лаунчеру нужно дожить до блока
# finally, где состояние уходит на GitHub.
signal.signal(signal.SIGINT, signal.SIG_IGN)


def _restore_default_sigint():
    """Возвращает дочернему процессу обычную реакцию на SIGINT.

    На Linux/Android диспозиция SIG_IGN наследуется через fork+exec, и CPython
    в таком случае вообще не ставит свой обработчик — app.py переставал
    реагировать на Ctrl+C и на `pkill -2` из mobile/stop.sh. Дальше stop.sh
    добивал процессы через -9 вместе с лаунчером, и состояние не пушилось.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


try:
    popen_kwargs = {}
    if os.name != "nt":
        popen_kwargs["preexec_fn"] = _restore_default_sigint
    proc = subprocess.Popen([sys.executable, MONITOR] + sys.argv[1:], cwd=REPO, **popen_kwargs)
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(f"{os.getpid()}\n{proc.pid}\n")
    except OSError as e:
        print(f"WARNING: could not update lock file with child PID: {e}")
    proc.wait()
finally:
    print("\n=== [3/3] Pushing state updates to GitHub ===")
    try:
        # Re-encrypt config if passphrase is provided
        passphrase = os.environ.get("CONFIG_PASSPHRASE")
        if passphrase and os.path.exists(os.path.join(REPO, "config.json")):
            print("Encrypting config.json...")
            try:
                sys.path.append(REPO)
                import config_crypt
                with open(os.path.join(REPO, "config.json"), "rb") as f:
                    plaintext = f.read()
                # Перезаписываем только при реальном изменении настроек: соль
                # случайна, поэтому иначе каждый запуск давал новый 23-КБ блоб,
                # несжимаемый дельтой (163 МБ истории при неизменном конфиге).
                if config_crypt.encrypt_to_file(plaintext, passphrase, os.path.join(REPO, "config.json.enc")):
                    print("  Config encrypted successfully.")
                else:
                    print("  Config unchanged — re-encryption skipped.")
            except Exception as e:
                print(f"  ⚠️ Error encrypting config: {e}")
        
        # Ensure remote URL has token for non-interactive push
        gh_token = os.environ.get('GITHUB_TOKEN', '')
        if gh_token:
            remote_url = git("remote", "get-url", "origin")
            remote_str = (remote_url.stdout or "").strip()
            if remote_str:
                fixed = re.sub(r'https://(?:[^@]+@)?github\.com', f'https://{gh_token}@github.com', remote_str)
                if fixed != remote_str:
                    git("remote", "set-url", "origin", fixed)

        files_to_sync = list(STATE_SYNC)
        if not passphrase:
            if "config.json.enc" in files_to_sync:
                files_to_sync.remove("config.json.enc")
            if "config.json" not in files_to_sync:
                files_to_sync.append("config.json")
            print("No passphrase set, staging unencrypted config.json directly...")
            git("add", "-f", "config.json")
        else:
            if "config.json" in files_to_sync:
                files_to_sync.remove("config.json")
        # Resolve any conflicts in working directory before committing state
        status = git("status", "--porcelain")
        unmerged = []
        for line in (status.stdout or "").splitlines():
            if len(line) >= 2 and ('U' in line[:2] or line[:2] in ('AA', 'DD')):
                path_part = line[3:].strip()
                if " -> " in path_part:
                    path_part = path_part.split(" -> ")[-1].strip()
                unmerged.append(path_part.strip('"'))
        
        if unmerged:
            print(f"INFO: resolving conflicts in working tree before state commit: {unmerged}")
            for f in unmerged:
                pick = git("checkout", "--theirs", "--", f)
                if pick.returncode != 0:
                    git("checkout", "--ours", "--", f)
                git("add", "--", f)
                git("reset", "--", f)

        git("add", *files_to_sync)
        if git_has_staged(files_to_sync):
            commit = git("commit", "-m", "Sync state after run", "--", *files_to_sync, visible=True)
            if commit.returncode != 0:
                print("WARNING: git commit failed — state left as working-tree changes.")
                raise SystemExit(0)

            # Retry push up to 5 times with increasing delay
            import time as _time
            pushed = False
            for attempt in range(1, 6):
                push = git("push", visible=True, timeout=30)
                if push.returncode == 0:
                    print("Done.")
                    pushed = True
                    break
                delay = attempt * 3  # 3, 6, 9, 12, 15 seconds
                print(f"Push attempt {attempt}/5 failed — pull & retry in {delay}s...")
                _time.sleep(delay)
                # Backup state, pull remote, restore state, re-commit
                state_snapshot = backup_files(dirty_state_files() or files_to_sync)
                env = os.environ.copy()
                env["GIT_TERMINAL_PROMPT"] = "0"
                try:
                    pull = subprocess.run(GIT_BASE + ["pull", "--rebase"], cwd=REPO, env=env, timeout=60)
                    if pull.returncode != 0:
                        if in_rebase():
                            print("INFO: state conflict during pull rebase, auto-resolving...")
                            if not resolve_state_rebase():
                                print("WARNING: could not auto-resolve rebase, aborting.")
                                clean_leftover_rebase()
                                break
                except subprocess.TimeoutExpired:
                    print("WARNING: pull timed out.")
                    break
                # Merge our local state with whatever remote had (not overwrite)
                if state_snapshot:
                    merge_back_state(state_snapshot)
                # Re-encrypt config if needed, otherwise stage config.json directly
                if passphrase and os.path.exists(os.path.join(REPO, "config.json")):
                    try:
                        with open(os.path.join(REPO, "config.json"), "rb") as f:
                            config_crypt.encrypt_to_file(f.read(), passphrase,
                                                         os.path.join(REPO, "config.json.enc"))
                    except Exception:
                        pass
                elif not passphrase and os.path.exists(os.path.join(REPO, "config.json")):
                    git("add", "-f", "config.json")
                # Re-add and amend/re-commit
                git("add", *files_to_sync)
                if git_has_staged(files_to_sync):
                    git("commit", "-m", "Sync state after run", "--", *files_to_sync)

            if not pushed:
                print("WARNING: all push attempts failed — remote not updated.")
                undo_last_state_commit()
        else:
            print("No state changes to push.")
    except Exception as e:
        print(f"WARNING: state sync skipped: {e}")

