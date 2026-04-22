# PowerShell launcher — no "Terminate batch job?" prompt on Ctrl+C
Set-Location $PSScriptRoot

# Load env vars from set_env.bat
if (Test-Path ".\set_env.bat") {
    $vars = cmd /c "call `"$PSScriptRoot\set_env.bat`" > nul 2>&1 && set"
    foreach ($v in $vars) {
        if ($v -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
} else {
    Write-Error "set_env.bat not found. Copy set_env.example.bat to set_env.bat and fill in tokens."
    exit 1
}

python run_launcher.py
