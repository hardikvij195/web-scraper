# fix-agent.ps1 - one-shot "fix this machine's Lead Finder agent" (W182 / CRM T1078, 2026-10-09).
# The CRM Systems page shows this one-liner per machine with a copy icon; paste it DIRECTLY into any
# PowerShell window on that laptop (do not wrap it in powershell -Command '...': Windows strips the
# nested quotes and the parser fails with "Missing argument in parameter list"):
#   cd "<root>"; git pull --ff-only; powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\fix-agent.ps1
# Add -Check at the end to only print identity + evidence and change nothing.
#
# What it does: 1) identity  2) evidence (log tail, stop sentinel, scheduled task, live processes)
# 3) fix: drop data\agent.stop, kill stale loop/agent processes, git pull + pip with timeouts, (re)register,
# enable and run the "HVT Lead Finder Agent" task  4) verdict: wait up to 90 s for a fresh "agent up (" line.
# Exit code 0 = AGENT UP, 1 = not up (the NOT UP block is what to send back).
# Windows PowerShell 5.1 compatible: no &&, ||, ?:, ?? anywhere in this file.
param([switch]$Check)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$task = 'HVT Lead Finder Agent'
$log = Join-Path $root 'data\agent.log'
$stop = Join-Path $root 'data\agent.stop'
$alive = Join-Path $root 'data\agent.alive'

function Step($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }
function AgeMin($path) {
  if (-not (Test-Path $path)) { return -1 }
  return [int]((Get-Date) - (Get-Item $path).LastWriteTime).TotalMinutes
}
function AgentProcs {
  # Same filter as install-agent.ps1 step 5/7: the supervisor cmd.exe and the python agent.
  Get-CimInstance Win32_Process | Where-Object {
    (($_.Name -ieq 'cmd.exe') -and ($_.CommandLine -like '*run-agent-loop.bat*')) -or ($_.CommandLine -like '*webscraper agent*')
  }
}
function TailSince($path, $offset) {
  if (-not (Test-Path $path)) { return '' }
  $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
  try {
    if ($fs.Length -le $offset) { return '' }
    $null = $fs.Seek($offset, 'Begin')
    $sr = New-Object IO.StreamReader($fs)
    return $sr.ReadToEnd()
  } finally { $fs.Dispose() }
}

Step "1/4 identity"
$rev = ''
try { $rev = (cmd /c "git rev-parse --short HEAD 2>nul" | Out-String).Trim() } catch {}
$ver = '?'
if (Test-Path 'VERSION') { $ver = (Get-Content 'VERSION' -Raw).Trim() }
$dev = '(no data\device_name)'
if (Test-Path 'data\device_name') { $dev = (Get-Content 'data\device_name' -Raw).Trim() }
Write-Host "machine     : $env:COMPUTERNAME  (user $env:USERNAME)"
Write-Host "root        : $root"
Write-Host "git rev     : $rev   VERSION $ver"
Write-Host "device_name : $dev"
Write-Host "time        : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

Step "2/4 evidence"
if (Test-Path $log) {
  Write-Host "--- last 40 lines of data\agent.log (modified $(AgeMin $log) min ago) ---"
  Get-Content $log -Tail 40 | ForEach-Object { Write-Host $_ }
  Write-Host "--- end of log ---"
} else {
  Write-Host "no agent.log"
}
if (Test-Path $stop) { Write-Host "agent.stop  : PRESENT (a CRM Stop was pressed - the loop refuses to start while it exists)" } else { Write-Host "agent.stop  : absent" }
$aliveAge = AgeMin $alive
if ($aliveAge -lt 0) { Write-Host "agent.alive : missing (agent older than W182, or never polled the CRM)" } else { Write-Host "agent.alive : touched $aliveAge min ago (healthy = under 1 min)" }
$registered = $false
$q = cmd /c "schtasks /Query /TN `"$task`" /FO LIST /V 2>nul"
if ($LASTEXITCODE -eq 0) {
  $registered = $true
  Write-Host "scheduled task '$task':"
  $q | Where-Object { $_ -match '^(Status|Last Run Time|Last Result|Scheduled Task State|Logon Mode|Run As User):' } | ForEach-Object { Write-Host "  $($_.Trim())" }
} else {
  Write-Host "scheduled task '$task': NOT REGISTERED"
}
$procs = @(AgentProcs)
if ($procs.Count -eq 0) {
  Write-Host "live processes: none (no supervisor loop, no agent)"
} else {
  Write-Host "live processes:"
  foreach ($p in $procs) {
    $mins = [int]((Get-Date) - $p.CreationDate).TotalMinutes
    Write-Host ("  PID {0,-6} {1,-10} started {2:dd-MM HH:mm:ss}  alive {3} min" -f $p.ProcessId, $p.Name, $p.CreationDate, $mins)
    Write-Host "         $($p.CommandLine)"
  }
}

if ($Check) {
  Write-Host ""
  Write-Host "(-Check: nothing changed)"
  exit 0
}

Step "3/4 fix"
if (Test-Path $stop) { Remove-Item $stop -Force; Write-Host "removed data\agent.stop" }
foreach ($p in $procs) {
  try {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    Write-Host "killed PID $($p.ProcessId) ($($p.Name))"
  } catch {
    Write-Host "could not kill PID $($p.ProcessId): $($_.Exception.Message)"
  }
}
$env:GIT_TERMINAL_PROMPT = '0'
Write-Host "git pull --ff-only (60 s stall timeout) ..."
cmd /c "git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 pull --ff-only 2>&1" | ForEach-Object { Write-Host "  $_" }
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
Write-Host "pip install -r requirements.txt (timeout 30 s, 2 retries) ..."
cmd /c "`"$py`" -m pip install -q --timeout 30 --retries 2 -r requirements.txt 2>&1" | ForEach-Object { Write-Host "  $_" }
$offset = 0
if (Test-Path $log) { $offset = (Get-Item $log).Length }
if (-not $registered) {
  Write-Host "registering the scheduled task ..."
  powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install-agent-autostart.ps1')
}
cmd /c "schtasks /Change /TN `"$task`" /ENABLE >nul 2>&1"
Write-Host "schtasks /Run '$task' ..."
cmd /c "schtasks /Run /TN `"$task`" 2>&1" | ForEach-Object { Write-Host "  $_" }

Step "4/4 verdict"
$upLine = ''
for ($i = 0; $i -lt 18; $i++) {
  Start-Sleep -Seconds 5
  $new = TailSince $log $offset
  $hit = @($new -split "`r?`n" | Where-Object { $_ -like '*agent up (*' })
  if ($hit.Count -gt 0) { $upLine = $hit[$hit.Count - 1]; break }
  Write-Host -NoNewline "."
}
Write-Host ""
if ($upLine -ne '') {
  Write-Host "AGENT UP" -ForegroundColor Green
  Write-Host $upLine
  Write-Host "The CRM card for this machine turns online within ~1 minute."
  exit 0
}
Write-Host "NOT UP - no 'agent up (' line within 90 s. Send everything between the lines below:" -ForegroundColor Yellow
Write-Host "-------- NOT UP: $env:COMPUTERNAME  root=$root  rev=$rev  VERSION=$ver --------"
if (Test-Path $log) { Get-Content $log -Tail 15 | ForEach-Object { Write-Host $_ } } else { Write-Host "no agent.log" }
$after = @(AgentProcs)
Write-Host ("processes now: " + $(if ($after.Count -eq 0) { 'none' } else { ($after | ForEach-Object { "$($_.Name)#$($_.ProcessId)" }) -join ', ' }))
Write-Host "hint: run  python -m webscraper doctor  in this folder and send its output too."
Write-Host "-------- end --------"
Write-Host "The CRM card for this machine turns online within ~1 minute."
exit 1
