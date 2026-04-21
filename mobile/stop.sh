#!/usr/bin/env bash
# Gracefully stop the running bot and push state to GitHub.
# Use this if you started the bot via run.sh in another Termux session.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

echo "=== [1/2] Stopping monitor.py ==="
# pkill returns 1 if no process matched — we handle that
if pkill -f "python.*monitor.py"; then
    echo "SIGTERM sent. Waiting for graceful shutdown..."
    # Wait up to 10 seconds for python to exit
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! pgrep -f "python.*monitor.py" >/dev/null; then
            echo "Bot stopped."
            break
        fi
        sleep 1
    done
    # Force kill if still running
    if pgrep -f "python.*monitor.py" >/dev/null; then
        echo "Still running — sending SIGKILL..."
        pkill -9 -f "python.*monitor.py" || true
    fi
else
    echo "Bot is not running."
fi

echo
echo "=== [2/2] Pushing state updates to GitHub ==="
git add seen_ids.txt sent_offers.json banned_ids.txt config.json price_history.db 2>/dev/null || true
if ! git diff --cached --quiet; then
    git pull --rebase || { echo "git pull failed — resolve conflicts manually"; exit 1; }
    git commit -m "Sync state after manual run"
    git push
    echo "Done."
else
    echo "No state changes to push."
fi
