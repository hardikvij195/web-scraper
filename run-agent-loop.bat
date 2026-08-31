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

REM Starting by hand (or via the task) clears a previous CRM "Stop agent" —
REM otherwise the sentinel would keep the machine stopped for ever with no local
REM way back, since a stopped agent polls nothing and cannot be told to start.
if exist "data\agent.stop" (
  echo [%date% %time%] clearing stop sentinel - starting again >> "data\agent.log"
  del /q "data\agent.stop"
)

:loop
REM W50: the CRM's "Stop agent" drops data\agent.stop. Without this check the loop
REM restarted the agent 15s later and the folder stayed locked, which is why an old
REM folder could not be deleted after a move ("folder is still in use").
if exist "data\agent.stop" (
  echo [%date% %time%] stop sentinel present - supervisor exiting >> "data\agent.log"
  goto :eof
)
REM Self-update before every start (2026-08-26) - fast-forward only, never blocks;
REM offline just runs what is on disk. A restart is therefore also an upgrade.
git pull --ff-only -q >> "data\agent.log" 2>&1 && "%PY%" -m pip install -q -r requirements.txt >> "data\agent.log" 2>&1
echo [%date% %time%] starting agent >> "data\agent.log"
"%PY%" -m webscraper agent --crm --poll 5 >> "data\agent.log" 2>&1
echo [%date% %time%] agent exited (code %errorlevel%) - restarting in 15s >> "data\agent.log"
REM `timeout` blocks forever when there is no console (scheduled task) - 27 zombie
REM loops were found parked on it (2026-08-26). ping is a sleep that never needs stdin.
ping -n 16 127.0.0.1 >nul
goto loop
