@echo off
cd /d %~dp0

REM Load env vars from set_env.bat (not in git)
if exist set_env.bat (
    call set_env.bat
) else (
    echo ERROR: set_env.bat not found. Copy set_env.example.bat to set_env.bat and fill in tokens.
    pause
    exit /b 1
)

echo === [1/3] Pulling latest state from GitHub ===
git pull --rebase
if errorlevel 1 (
    echo.
    echo ERROR: git pull failed. Resolve conflicts manually.
    pause
    exit /b 1
)

echo.
echo === [2/3] Starting bot (use /stop in Telegram or Ctrl+C to exit) ===
echo.
python monitor.py

echo.
echo === [3/3] Pushing state updates to GitHub ===
git add seen_ids.txt sent_offers.json banned_ids.txt config.json price_history.db 2>nul
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Sync state after manual run"
    git push
    echo Done.
) else (
    echo No state changes to push.
)

pause