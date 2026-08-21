@echo off
REM Start the Lead Finder scraper agent — it picks up jobs you create in the CRM.
REM Leave this window open while you want jobs to run. Reads CRM_AGENT_TOKEN from .env.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet — run install.bat first.
  pause
  exit /b 1
)

echo Starting CRM Lead Finder agent. Keep this window open. Press Ctrl-C to stop.
".venv\Scripts\python.exe" -m webscraper agent --crm --poll 15
pause
