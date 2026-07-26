# ============================================================
# Скрипт перепривязки к GitHub аккаунту
# Использование:
#   .\switch_github.ps1 -Token "ghp_НОВЫЙ_ТОКЕН"
#   .\switch_github.ps1 -Token "ghp_xxx" -Repo "GitGayHub/tg-monitor"
# ============================================================
param(
    [Parameter(Mandatory=$true)]
    [string]$Token,
    [string]$Repo = "GitGayHub/tg-monitor"
)

$RepoPath = $PSScriptRoot
$ErrorActionPreference = "Stop"

Write-Host "=== Перепривязка GitHub ===" -ForegroundColor Cyan
Write-Host "Repo: $Repo" -ForegroundColor Yellow

# 1. Обновляем git remote (убираем старый токен, ставим новый)
Write-Host "`n[1/3] Обновляю git remote..." -ForegroundColor Green
Push-Location $RepoPath
try {
    git remote remove origin 2>$null
    git remote add origin "https://${Token}@github.com/${Repo}.git"
    Write-Host "  Remote установлен: origin -> github.com/${Repo}" -ForegroundColor Gray
} finally {
    Pop-Location
}

# 2. Обновляем set_env.example.bat
Write-Host "`n[2/3] Обновляю set_env.example.bat..." -ForegroundColor Green
$batPath = Join-Path $RepoPath "set_env.example.bat"
if (Test-Path $batPath) {
    # CONFIG_PASSPHRASE обязателен: без него лаунчер откатывается на пуш
    # незашифрованного config.json, а раньше эта строка тут терялась.
    $batContent = @"
@echo off
REM === Fill in your values and save as set_env.bat ===
set TELEGRAM_BOT_TOKEN=your_bot_token_here
set TELEGRAM_CHAT_ID=your_telegram_chat_id
set GITHUB_TOKEN=your_github_pat_optional
set GITHUB_REPOSITORY=$Repo
set CONFIG_PASSPHRASE=your_secret_passphrase_here
"@
    Set-Content -Path $batPath -Value $batContent -Encoding ASCII
    Write-Host "  $batPath обновлён" -ForegroundColor Gray
}

# 3. Обновляем mobile/set_env.example.sh
Write-Host "`n[3/3] Обновляю mobile/set_env.example.sh..." -ForegroundColor Green
$shPath = Join-Path $RepoPath "mobile\set_env.example.sh"
if (Test-Path $shPath) {
    $shContent = @"
#!/bin/bash
# === Fill in your values and save as set_env.sh ===
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
export TELEGRAM_CHAT_ID="your_telegram_chat_id"
export GITHUB_REPOSITORY="$Repo"
export CONFIG_PASSPHRASE="your_secret_passphrase_here"
# Optional — only needed for /sync button in Telegram
# export GITHUB_TOKEN="your_github_pat_optional"
"@
    Set-Content -Path $shPath -Value $shContent -Encoding ASCII
    Write-Host "  $shPath обновлён" -ForegroundColor Gray
}

Write-Host "`n=== Готово! ===" -ForegroundColor Cyan
Write-Host "Не забудь обновить GH_PAT в Secrets репозитория:" -ForegroundColor Yellow
Write-Host "  https://github.com/$Repo/settings/secrets/actions" -ForegroundColor White
Write-Host "`nПроверить связь: git -C `"$RepoPath`" fetch" -ForegroundColor Gray
