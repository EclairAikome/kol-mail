# Keeps KOL Mail running in the background without a console window. Started at logon by the scheduled task
# "kol-mail" (created by install.ps1); restarts the app 15 s after it exits, which is also how a self-update takes
# effect. The log lives in %LOCALAPPDATA%\kol-mail because it can contain mail subjects.
param(
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'kol-mail\venv\Scripts\python.exe')
)

$app = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA 'kol-mail'
$log = Join-Path $logDir 'server.log'
New-Item -ItemType Directory -Force $logDir | Out-Null
$env:PYTHONIOENCODING = 'utf-8'

function Get-Port {
    $settings = Join-Path $app 'data\settings.json'
    try { $p = (Get-Content -Raw -Encoding UTF8 $settings | ConvertFrom-Json).port; if ($p) { return [int]$p } } catch {}
    return 8765
}

function Test-PortBusy([int]$port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Set-Location $app
while ($true) {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) {
        Move-Item -Force $log "$log.1"
    }
    $port = Get-Port
    # Another copy already serves the port. A second one would also log in to the Telegram session twice.
    if (Test-PortBusy $port) {
        Add-Content -Encoding UTF8 $log "[$(Get-Date -Format s)] port $port is in use by another process, checking again in 60 s"
        while (Test-PortBusy $port) { Start-Sleep -Seconds 60 }
    }
    # A forced kill can leave mcp-email-server's catalog -wal/-shm with loose permissions, after which the mail
    # backend refuses to connect. Lock them down again first.
    cmd /c "`"$Python`" -I -X utf8 `"$PSScriptRoot\repair-catalog-sidecars.py`" >> `"$log`" 2>&1"
    Add-Content -Encoding UTF8 $log "[$(Get-Date -Format s)] starting kol-mail on 127.0.0.1:$port"
    cmd /c "`"$Python`" -I -X utf8 app.py >> `"$log`" 2>&1"
    $code = $LASTEXITCODE
    Add-Content -Encoding UTF8 $log "[$(Get-Date -Format s)] exited with code $code, restarting in 15 s"
    # an error exit (a restart for an update exits with 0) goes to the maintainer's POPO group, with the log's end;
    # not -1, a process killed from outside (Stop-Process, an installer replacing this copy): that is a stop, as run.sh
    # treats SIGKILL
    if ($code -ne 0 -and $code -ne -1) { cmd /c "`"$Python`" -I -X utf8 autoreport.py exited $code `"$log`" >> `"$log`" 2>&1" }
    Start-Sleep -Seconds 15
}
