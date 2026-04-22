"""Launcher: git pull → run bot → git push. No 'Terminate batch job?' prompt."""
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
    subprocess.run(["git"] + list(args), **kwargs)


def git_has_staged():
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO, capture_output=True)
    return r.returncode != 0


print("=== [1/3] Pulling latest state from GitHub ===")
git("add", *STATE)
if git_has_staged():
    git("commit", "-m", "Sync state before pull", visible=True)
result = subprocess.run(["git", "pull", "--rebase"], cwd=REPO)
if result.returncode != 0:
    print("\nERROR: git pull failed. Resolve conflicts manually.")
    input("Press Enter to exit...")
    sys.exit(1)

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
