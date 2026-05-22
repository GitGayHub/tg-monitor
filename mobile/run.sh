#!/usr/bin/env bash
# Manual run on Linux/Termux/macOS with auto pull/push state sync.

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

# Run the cross-platform launcher which handles git sync, decryption, running, encryption, committing and pushing
python launcher.py
