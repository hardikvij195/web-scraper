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
  [string]$Dir = "$env:USERPROFILE\hvt-lead-finder-agent",
  [string]$Repo = "https://github.com/hardikvij195/web-scraper.git"
)
$ErrorActionPreference = "Stop"
if (-not $Device) { $Device = $env:COMPUTERNAME }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Refresh-Path {
  $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH", "User")
}

Step "1/6 tools (git, python 3.11+)"
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

Step "2/6 code -> $Dir"
if (Test-Path (Join-Path $Dir ".git")) {
  git -C $Dir pull --ff-only
  if (-not $?) { Write-Host "pull failed (local changes?) - continuing with the current checkout" }
} else {
  git clone --depth 1 $Repo $Dir
}
Set-Location $Dir

Step "3/6 python deps"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  $args = @(); if ($py[1]) { $args += $py[1] }; $args += @("-m", "venv", ".venv")
  & $py[0] @args
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

Step "4/6 .env"
if (-not (Test-Path ".env")) { New-Item -ItemType File ".env" | Out-Null }
$lines = @(Get-Content ".env" | Where-Object { $_ -notmatch '^(CRM_AGENT_TOKEN|LEAD_FINDER_DEVICE)=' })
$lines += "CRM_AGENT_TOKEN=$Token"
$lines += "LEAD_FINDER_DEVICE=$Device"
Set-Content ".env" $lines -Encoding utf8

Step "5/6 autostart (scheduled task) + start"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install-agent-autostart.ps1"

Step "6/6 self-check"
.\.venv\Scripts\python.exe -m webscraper doctor
Write-Host ""
Write-Host "Done. This machine is '$Device' in the CRM's Run-on list within ~10 s. Log: $Dir\data\agent.log" -ForegroundColor Green
Write-Host "WhatsApp verification also needs: .venv\Scripts\python.exe -m webscraper wa-login main"
