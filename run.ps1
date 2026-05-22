# PowerShell launcher — no "Terminate batch job?" prompt on Ctrl+C
Set-Location $PSScriptRoot

# Kill any old bot processes from this repo
$repoPath = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$repoPattern = [Regex]::Escape($repoPath)
Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
    Where-Object { $_.CommandLine -match $repoPattern -and $_.CommandLine -match '(app|launcher)\.py' } |
    ForEach-Object {
        Write-Host "Stopping old bot process PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

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

python "$PSScriptRoot\launcher.py"
