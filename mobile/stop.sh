#!/usr/bin/env bash
# Gracefully stop the running bot and trigger state sync to GitHub.
# Use this if you started the bot via run.sh in another Termux session.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

echo "=== Stopping app.py (sending SIGINT) ==="
# We target app.py. When app.py exits, its parent launcher.py will run its finally block
# and automatically push the updated state (including encrypted config) to GitHub.
if pkill -2 -f "python.*app.py"; then
    echo "SIGINT sent. Waiting for launcher to complete state sync..."
    for i in {1..15}; do
        if ! pgrep -f "python.*(app|launcher)\.py" >/dev/null; then
            echo "Bot stopped and state synced successfully."
            exit 0
        fi
        sleep 1
    done
    echo "Warning: Bot processes are still running. Sending SIGKILL..."
    pkill -9 -f "python.*(app|launcher)\.py" || true
else
    echo "Bot is not running."
fi
