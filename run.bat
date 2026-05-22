@echo off
cd /d %~dp0

if exist set_env.bat (
    call set_env.bat
) else (
    echo ERROR: set_env.bat not found. Copy set_env.example.bat to set_env.bat and fill in tokens.
    exit /b 1
)

python launcher.py