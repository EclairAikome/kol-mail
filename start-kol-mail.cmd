@echo off
rem Open KOL Mail in the default browser. The app itself runs hidden in the background under the scheduled task
rem "kol-mail" (service\run.ps1); this only starts that task when it is not serving yet.
rem Log: %LOCALAPPDATA%\kol-mail\server.log
set "PORT=8765"
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "try { (Get-Content -Raw -Encoding UTF8 '%~dp0data\settings.json' | ConvertFrom-Json).port } catch { }"`) do if not "%%p"=="" set "PORT=%%p"
set "URL=http://127.0.0.1:%PORT%/"
call :listening && goto open

echo Starting KOL Mail in the background...
schtasks /run /tn "kol-mail" >nul 2>&1
if errorlevel 1 (
  echo Could not start the scheduled task "kol-mail". Run install.ps1 again.
  pause
  exit /b 1
)
for /l %%i in (1,1,60) do (
  call :listening && goto open
  ping -n 2 127.0.0.1 >nul
)
echo KOL Mail did not come up within 60 s. See %LOCALAPPDATA%\kol-mail\server.log
pause
exit /b 1

:open
start "" "%URL%"
exit /b 0

:listening
netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:%PORT% .*LISTENING" >nul
exit /b %errorlevel%
