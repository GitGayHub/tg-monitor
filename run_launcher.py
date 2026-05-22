"""Launcher: git pull -> run bot -> git push. Auto-resolves state conflicts."""
import subprocess
import sys
import os
import signal
import atexit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(REPO, "monitor.py")
LOCK_FILE = os.path.join(REPO, ".bot.lock")
STATE_SYNC = ["seen_ids.txt", "sent_offers.json", "banned_ids.txt", "config.json", "price_history.db"]
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
                pid = f.read().strip()
            if pid == str(os.getpid()):
                os.remove(LOCK_FILE)
    except OSError:
        pass


def acquire_lock():
    """Prevent multiple bot instances from running simultaneously. Kills stale ones."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = f.read().strip()
        except OSError:
            old_pid = ""
        if old_pid and old_pid != str(os.getpid()) and pid_alive(old_pid):
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
        except OSError:
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
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


def resolve_state_rebase():
    """Auto-resolve rebase conflicts in state files by taking remote version."""
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


def sync_from_remote():
    print("=== [1/3] Pulling latest state from GitHub ===")
    # Auto-fix remote URL: replace embedded stale token with GITHUB_TOKEN from env
    gh_token = os.environ.get('GITHUB_TOKEN', '')
    remote_url = git("remote", "get-url", "origin")
    remote_str = (remote_url.stdout or "").strip()
    if gh_token and remote_str:
        import re
        # Replace any embedded token (ghp_xxx or github_pat_xxx) with current one
        fixed = re.sub(r'https://[^@]*@github\.com', f'https://{gh_token}@github.com', remote_str)
        if fixed != remote_str:
            print(f"  Fixing remote URL (old token -> new)...")
            git("remote", "set-url", "origin", fixed)
            remote_str = fixed
    if remote_str:
        # Show URL with masked token
        masked = re.sub(r'(https://)[^@]*(@github\.com)', r'\1***\2', remote_str)
        print(f"  Git remote: {masked}")
    else:
        print("  ⚠️ Git remote: not configured")
    try:
        fetch = git("fetch", "--prune", visible=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("\n⚠️ WARNING: git fetch timed out — starting bot with local state.")
        return
    if fetch.returncode != 0:
        print("\n⚠️ WARNING: git fetch failed — starting bot with local state.")
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
            restore_files(state_snapshot)
            print("INFO: local state restored after git sync.")


# ═══════════════════════════════════════════════════════════════════════════════
# Main execution
# ═══════════════════════════════════════════════════════════════════════════════

acquire_lock()

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
        git("rebase", "--abort", visible=True)

ensure_on_main()

# Persistently enable autostash so any future `git pull --rebase` auto-handles dirty trees.
git("config", "rebase.autoStash", "true")

sync_from_remote()

print("\n=== [2/3] Starting bot (Ctrl+C to exit) ===\n")

signal.signal(signal.SIGINT, signal.SIG_IGN)
try:
    subprocess.run([sys.executable, MONITOR], cwd=REPO)
finally:
    print("\n=== [3/3] Pushing state updates to GitHub ===")
    try:
        git("add", *STATE_SYNC)
        if git_has_staged(STATE_SYNC):
            commit = git("commit", "-m", "Sync state after run", "--", *STATE_SYNC, visible=True)
            if commit.returncode != 0:
                print("WARNING: git commit failed — state left as working-tree changes.")
                raise SystemExit(0)
            push = git("push", visible=True, timeout=30)
            if push.returncode != 0:
                print("Push rejected — pulling remote changes and retrying...")
                # Undo commit, pull, re-commit, push
                git("reset", "--mixed", "HEAD~1")
                # Pull with rebase
                env = os.environ.copy()
                env["GIT_TERMINAL_PROMPT"] = "0"
                try:
                    subprocess.run(GIT_BASE + ["pull", "--rebase", "--autostash"], cwd=REPO, env=env, timeout=60)
                except subprocess.TimeoutExpired:
                    print("WARNING: pull timed out — state kept locally.")
                    raise SystemExit(0)
                # Re-commit and push
                git("add", *STATE_SYNC)
                if git_has_staged(STATE_SYNC):
                    commit2 = git("commit", "-m", "Sync state after run", "--", *STATE_SYNC, visible=True)
                    if commit2.returncode != 0:
                        print("WARNING: re-commit failed — state left as working-tree changes.")
                        raise SystemExit(0)
                    push2 = git("push", visible=True, timeout=30)
                    if push2.returncode != 0:
                        print("WARNING: git push failed again — remote not updated.")
                        undo_last_state_commit()
                    else:
                        print("Done.")
                else:
                    print("No state changes after pull.")
            else:
                print("Done.")
        else:
            print("No state changes to push.")
    except Exception as e:
        print(f"WARNING: state sync skipped: {e}")
