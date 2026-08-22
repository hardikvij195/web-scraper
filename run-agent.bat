@echo off
REM Start the Lead Finder scraper agent - it picks up jobs you create in the CRM.
REM Leave this window open while you want jobs to run. Reads CRM_AGENT_TOKEN from .env.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  REM No project venv - fall back to whatever python is on PATH.
  where python >nul 2>&1 || (
    echo No python found. Run install.bat first.
    pause
    exit /b 1
  )
  set "PY=python"
)

echo Starting CRM Lead Finder agent. Keep this window open. Press Ctrl-C to stop.
"%PY%" -m webscraper agent --crm --poll 15
pause
