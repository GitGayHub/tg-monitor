@echo off
cd /d %~dp0

if exist set_env.bat (
    call set_env.bat
) else (
    echo ERROR: set_env.bat not found. Copy set_env.example.bat to set_env.bat and fill in tokens.
    exit /b 1
)

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe launcher.py
) else (
    python launcher.py
)