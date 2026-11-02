@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0\.."

set REMOTE_USER=root
set REMOTE_HOST=YOUR_SERVER_IP
set REMOTE_ROOT=/var/www/tools

if "%REMOTE_HOST%"=="YOUR_SERVER_IP" (
  echo [ERROR] Please edit tools\deploy.bat and set REMOTE_HOST first.
  pause
  exit /b 1
)

echo == Generating version.json ==
python tools\make_version.py
if errorlevel 1 (
  echo [ERROR] Failed. Fix the version mismatch above and retry.
  pause
  exit /b 1
)

echo == Uploading to %REMOTE_HOST%:%REMOTE_ROOT% ==
scp -r index.html sw.js version.json apps %REMOTE_USER%@%REMOTE_HOST%:%REMOTE_ROOT%

echo == Done. Users will see the update prompt on next refresh. ==
pause
