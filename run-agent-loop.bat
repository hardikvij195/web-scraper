@echo off
REM Supervisor: keeps the CRM Lead Finder agent alive forever.
REM If the agent crashes (network drop, Playwright hiccup) it restarts after 15s.
REM Logs to data\agent.log. Started automatically by the "HVT Lead Finder Agent"
REM scheduled task at logon; also safe to double-click.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "data" mkdir "data"

:loop
REM Self-update before every start (2026-08-26) - fast-forward only, never blocks;
REM offline just runs what is on disk. A restart is therefore also an upgrade.
git pull --ff-only -q >> "data\agent.log" 2>&1 && "%PY%" -m pip install -q -r requirements.txt >> "data\agent.log" 2>&1
echo [%date% %time%] starting agent >> "data\agent.log"
"%PY%" -m webscraper agent --crm --poll 5 >> "data\agent.log" 2>&1
echo [%date% %time%] agent exited (code %errorlevel%) - restarting in 15s >> "data\agent.log"
timeout /t 15 /nobreak >nul
goto loop
