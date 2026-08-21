@echo off
REM Link a WhatsApp account for number verification (scan the QR once).
REM Usage: double-click, then type an account label when asked (e.g. spare1).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet — run install.bat first.
  pause
  exit /b 1
)

set /p NAME="WhatsApp account label (e.g. spare1): "
if "%NAME%"=="" set NAME=spare1
".venv\Scripts\python.exe" -m webscraper wa-login %NAME%
pause
