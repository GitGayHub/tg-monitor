#!/usr/bin/env bash
# Manual run on Linux/Termux/macOS with auto pull/push state sync.
set -e

# Go to mobile/ first to load env, then to repo root for the bot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Load env vars from mobile/set_env.sh (not in git)
if [ -f "$SCRIPT_DIR/set_env.sh" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/set_env.sh"
else
    echo "ERROR: mobile/set_env.sh not found. Copy mobile/set_env.example.sh to mobile/set_env.sh and fill in tokens."
    exit 1
fi

cd "$REPO_DIR"

echo "=== [1/3] Pulling latest state from GitHub ==="
# Commit any local changes first (e.g. config.json written by bot) to avoid rebase conflicts
git add seen_ids.txt sent_offers.json banned_ids.txt config.json price_history.db 2>/dev/null || true
if ! git diff --cached --quiet; then
    git commit -m "Sync state before pull"
fi
git pull --rebase || { echo "git pull failed"; exit 1; }

echo
echo "=== [2/3] Starting bot (use /stop in Telegram or Ctrl+C to exit) ==="
echo
python monitor.py || true

echo
echo "=== [3/3] Pushing state updates to GitHub ==="
git add seen_ids.txt sent_offers.json banned_ids.txt config.json price_history.db 2>/dev/null || true
if ! git diff --cached --quiet; then
    git commit -m "Sync state after manual run"
    git push
    echo "Done."
else
    echo "No state changes to push."
fi
