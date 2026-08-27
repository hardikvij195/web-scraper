<#
One-click installer for the HVT Lead Finder agent on Windows.
Downloaded from the CRM (Lead Finder -> Setup -> "Install agent on this computer"),
which embeds the token + device name; also runnable by hand:
  powershell -ExecutionPolicy Bypass -File install-agent.ps1 -Token wsk_... [-Device "Office PC"] [-Dir "$env:USERPROFILE\hvt-lead-finder-agent"] [-Repo URL]
Idempotent: re-running updates the checkout, deps and .env, then restarts the agent.
#>
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$Device = "",
  # Empty = auto-detect: an earlier install (the scheduled task's folder), else a checkout
  # containing webscrapergent.py at/above the current folder or this script, else
  # %USERPROFILE%\hvt-lead-finder-agent. Pass -Dir to force a folder.
  [string]$Dir = "",
  [string]$Repo = "https://github.com/hardikvij195/web-scraper.git",
  # The CRM's Supabase URL. A CLONED tenant CRM lives on its own project; without this the
  # agent would talk to HVT's (the built-in default) and never see the tenant's jobs.
  [string]$CrmUrl = "",
  # Skip the trailing WhatsApp QR step (e.g. unattended re-runs).
  [switch]$SkipWaLogin
)
$ErrorActionPreference = "Stop"
if (-not $Device) { $Device = $env:COMPUTERNAME }
function Is-Checkout($d) { $d -and (Test-Path (Join-Path $d "webscrapergent.py")) }
if (-not $Dir) {
  $task = Get-ScheduledTask -TaskName "HVT Lead Finder Agent" -ErrorAction SilentlyContinue
  $wd = if ($task) { $task.Actions[0].WorkingDirectory } else { $null }
  if (Is-Checkout $wd) { $Dir = $wd; Write-Host "using the existing install: $Dir" }
}
if (-not $Dir) {
  foreach ($start in @((Get-Location).Path, $PSScriptRoot)) {
    $d = $start
    while ($d -and -not $Dir) { if (Is-Checkout $d) { $Dir = $d }; $d = Split-Path -Parent $d }
  }
  if ($Dir) { Write-Host "using the checkout found at: $Dir" }
}
if (-not $Dir) { $Dir = "$env:USERPROFILE\hvt-lead-finder-agent" }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Refresh-Path {
  $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH", "User")
}

Step "1/7 tools (git, python 3.11+)"
$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  if (-not $hasWinget) { throw "git missing and winget unavailable - install Git from git-scm.com and re-run" }
  winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
  Refresh-Path
}
$py = $null
foreach ($c in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python", $null))) {
  try {
    $args = @(); if ($c[1]) { $args += $c[1] }
    $args += @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
    $v = & $c[0] @args 2>$null
    if ($v -and [version]$v -ge [version]"3.11") { $py = $c; break }
  } catch {}
}
if (-not $py) {
  if (-not $hasWinget) { throw "python 3.11+ missing and winget unavailable - install from python.org and re-run" }
  winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
  Refresh-Path
  $py = @("py", "-3.13")
}
Write-Host ("using " + ($py -join " "))

Step "2/7 code -> $Dir"
# An existing checkout is PULLED, never re-cloned. `.git` is a FILE inside a mono-repo
# submodule, and a submodule sits on a detached HEAD where `git pull` refuses to run —
# put it on main tracking origin/main once (2026-08-26).
$isRepo = $false
if (Test-Path $Dir) { git -C $Dir rev-parse --is-inside-work-tree 2>$null | Out-Null; $isRepo = ($LASTEXITCODE -eq 0) }
if ($isRepo) {
  git -C $Dir symbolic-ref -q HEAD 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { git -C $Dir fetch -q origin main; git -C $Dir checkout -q -B main origin/main }
  git -C $Dir pull --ff-only
} elseif ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
  if (-not (Test-Path (Join-Path $Dir "webscraper\agent.py"))) {
    Write-Host "ERROR: $Dir exists, is not empty, and is not the web-scraper repo. Pick another -Dir or run: git submodule update --init" -ForegroundColor Red; exit 1
  }
  Write-Host "$Dir holds the scraper but is not a git checkout - using it as is (no updates via git)" -ForegroundColor Yellow
  if (-not $?) { Write-Host "pull failed (local changes?) - continuing with the current checkout" }
} else {
  git clone --depth 1 $Repo $Dir
}
Set-Location $Dir

Step "3/7 python deps"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  $args = @(); if ($py[1]) { $args += $py[1] }; $args += @("-m", "venv", ".venv")
  & $py[0] @args
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

Step "4/7 .env"
if (-not (Test-Path ".env")) { New-Item -ItemType File ".env" | Out-Null }
$drop = if ($CrmUrl) { '^(CRM_AGENT_TOKEN|LEAD_FINDER_DEVICE|VITE_SUPABASE_URL)=' } else { '^(CRM_AGENT_TOKEN|LEAD_FINDER_DEVICE)=' }
$lines = @(Get-Content ".env" | Where-Object { $_ -notmatch $drop })
$lines += "CRM_AGENT_TOKEN=$Token"
$lines += "LEAD_FINDER_DEVICE=$Device"
if ($CrmUrl) { $lines += "VITE_SUPABASE_URL=$CrmUrl" }
Set-Content ".env" $lines -Encoding utf8

Step "5/7 autostart (scheduled task) + start"
# Re-running the installer is the "update + restart" path for a machine whose agent
# predates remote commands (2026-08-26). An old loop/agent left alive would keep the
# stale code (and the autostart guard would then refuse to start a new loop).
Get-CimInstance Win32_Process | Where-Object {
  ($_.CommandLine -like "*run-agent-loop.bat*") -or ($_.CommandLine -like "*webscraper agent*")
} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install-agent-autostart.ps1"

Step "6/7 self-check"
.\.venv\Scripts\python.exe -m webscraper doctor
Write-Host ""
Write-Host "Done. This machine is '$Device' in the CRM's Run-on list within ~10 s. Log: $Dir\data\agent.log" -ForegroundColor Green
# 7/7 - WhatsApp link, INLINE, so the one-time QR scan happens before the window closes.
# Skipped when this machine already holds a linked account (re-runs/updates).
$linked = & .\.venv\Scripts\python.exe -c "from webscraper.store import Store; from webscraper.wa_verify import profile_dir; print(int(any(not a['disabled'] and (profile_dir(a['name'])/'Default').exists() for a in Store().list_wa_accounts())))" 2>$null
if ($SkipWaLogin) {
  Write-Host "WhatsApp link skipped (-SkipWaLogin). Later: double-click wa-login.bat in $Dir" -ForegroundColor Yellow
} elseif ("$linked".Trim() -eq "1") {
  Write-Host "WhatsApp already linked on this machine - skipping the QR step." -ForegroundColor Green
} else {
  Step "7/7 WhatsApp link (one-time QR scan)"
  Write-Host "A WhatsApp Web window opens now. On the phone: WhatsApp > Linked devices > Link a device > scan the QR (2 min)." -ForegroundColor Yellow
  .\.venv\Scripts\python.exe -m webscraper wa-login main
  if ($LASTEXITCODE -ne 0) { Write-Host "wa-login did not finish - re-run it any time: double-click wa-login.bat in $Dir" -ForegroundColor Yellow }
  Write-Host "(Do NOT run bare 'python -m webscraper ...' - system Python lacks the deps; use .venv\Scripts\python.exe or the .bat)"
}
