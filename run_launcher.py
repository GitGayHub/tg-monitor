"""Launcher: git pull -> run bot -> git push. Auto-resolves state conflicts."""
import subprocess
import sys
import os
import signal

REPO = os.path.dirname(os.path.abspath(__file__))
STATE = ["seen_ids.txt", "sent_offers.json", "banned_ids.txt", "config.json", "price_history.db"]


def git(*args, visible=False):
    kwargs = {"cwd": REPO}
    if not visible:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(["git"] + list(args), **kwargs)


def git_has_staged():
    return git("diff", "--cached", "--quiet").returncode != 0


def in_rebase():
    git_dir = os.path.join(REPO, ".git")
    return os.path.exists(os.path.join(git_dir, "rebase-merge")) or \
           os.path.exists(os.path.join(git_dir, "rebase-apply"))


def current_branch():
    """Return current branch name, or empty string if HEAD is detached."""
    r = git("symbolic-ref", "--short", "-q", "HEAD")
    return (r.stdout or "").strip()


def ensure_on_main():
    """If HEAD is detached, force-checkout main."""
    if not current_branch():
        print("WARNING: detached HEAD detected, switching to main...")
        git("checkout", "main", visible=True)


def resolve_state_rebase():
    """While rebasing: take remote version for state-file conflicts, continue. Aborts on other conflicts.

    NOTE: during rebase, git flips --ours/--theirs semantics:
      --ours  = the branch being rebased ONTO (upstream/remote) <-- what we want for state
      --theirs = the local commit being replayed
    """
    guard = 0
    while in_rebase() and guard < 20:
        guard += 1
        status = git("status", "--porcelain")
        unmerged = []
        for line in (status.stdout or "").splitlines():
            if len(line) >= 2 and line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                unmerged.append(line[3:].strip())
        non_state = [f for f in unmerged if f not in STATE]
        if non_state:
            print(f"ERROR: conflict in non-state files: {non_state}")
            return False
        if unmerged:
            for f in unmerged:
                # --ours during rebase = upstream (remote) version
                git("checkout", "--ours", "--", f)
                git("add", "--", f)
        env = os.environ.copy()
        env["GIT_EDITOR"] = "true"
        cont = subprocess.run(["git", "rebase", "--continue"], cwd=REPO, env=env, capture_output=True, text=True)
        if cont.returncode != 0:
            skip = subprocess.run(["git", "rebase", "--skip"], cwd=REPO, capture_output=True, text=True)
            if skip.returncode != 0 and in_rebase():
                return False
    return not in_rebase()


# Clean up any leftover rebase from a previous crash
if in_rebase():
    print("WARNING: leftover rebase detected, aborting...")
    git("rebase", "--abort", visible=True)

# Ensure we're on main (rebase --abort may leave detached HEAD in some cases)
ensure_on_main()

print("=== [1/3] Pulling latest state from GitHub ===")

# Stash uncommitted state changes so pull has a clean tree
has_state_stash = False
dirty = git("status", "--porcelain", "--", *STATE)
if (dirty.stdout or "").strip():
    stash_result = git("stash", "push", "-u", "-m", "launcher-state", "--", *STATE, visible=True)
    has_state_stash = stash_result.returncode == 0

result = subprocess.run(["git", "pull", "--rebase"], cwd=REPO)
if result.returncode != 0:
    if in_rebase():
        print("INFO: state conflict during rebase, auto-resolving (remote wins for state files)...")
        if not resolve_state_rebase():
            print("ERROR: could not auto-resolve rebase.")
            git("rebase", "--abort", visible=True)
            input("Press Enter to exit...")
            sys.exit(1)
    else:
        print("\nERROR: git pull failed. Resolve conflicts manually.")
        input("Press Enter to exit...")
        sys.exit(1)

# Restore local state stash: remote wins on conflicts
if has_state_stash:
    pop = git("stash", "pop", visible=True)
    if pop.returncode != 0:
        # Conflict from stash pop — take remote version
        for f in STATE:
            git("checkout", "--", f)
        git("stash", "drop", visible=True)

print("\n=== [2/3] Starting bot (Ctrl+C to exit) ===\n")

signal.signal(signal.SIGINT, signal.SIG_IGN)
try:
    subprocess.run([sys.executable, "monitor.py"], cwd=REPO)
finally:
    print("\n=== [3/3] Pushing state updates to GitHub ===")
    git("add", *STATE)
    if git_has_staged():
        git("commit", "-m", "Sync state after run", visible=True)
        git("push", visible=True)
        print("Done.")
    else:
        print("No state changes to push.")
