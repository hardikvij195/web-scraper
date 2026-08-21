@echo off
REM One-time setup for the Lead Finder scraper agent (Windows).
REM Creates a local venv, installs deps, installs the Chromium browser Playwright needs.
REM Safe to re-run — it just updates.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.11+ from https://python.org and re-run this.
  pause
  exit /b 1
)

echo [1/3] Creating virtual environment (.venv)...
if not exist ".venv" python -m venv .venv

echo [2/3] Installing Python packages...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [3/3] Installing the Chromium browser...
call ".venv\Scripts\python.exe" -m playwright install chromium

if not exist ".env" (
  echo # Paste the agent token from the CRM Lead Finder ^> Setup tab:> .env
  echo CRM_AGENT_TOKEN=>> .env
  echo.
  echo Created .env — open it and paste your CRM_AGENT_TOKEN, then run run-agent.bat
) else (
  echo.
  echo Setup done. Make sure CRM_AGENT_TOKEN is set in .env, then run run-agent.bat
)
pause
