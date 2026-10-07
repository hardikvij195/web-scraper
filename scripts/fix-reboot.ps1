# fix-reboot.ps1 - one-shot "survive reboots" setup for a Lead Finder agent laptop (W173 + W175, 2026-10-07).
# Self-elevating: paste the one-liner from the CRM into ANY PowerShell window; this script re-opens itself as
# Administrator (UAC prompt), enables Windows auto-logon (asks for the Windows password once, stored as an LSA
# secret by Sysinternals Autologon), hardens the power plan (never sleep/hibernate, lid does nothing), and
# prints the final state. Use -Check to only print the state.
#
#   one-liner (any PowerShell, any agent laptop):
#   powershell -NoProfile -ExecutionPolicy Bypass -Command '$r=@("C:\hv-technologies\web-scraper","D:\1 - Repos\hv-technologies\web-scraper","D:\5 - Repositories\Hv Technologies\web-scraper")|?{Test-Path $_}|select -First 1; cd $r; git pull --ff-only; .\scripts\fix-reboot.ps1'
param([switch]$Check)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Check) {
  Write-Host "== $env:COMPUTERNAME - reboot survival state =="
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'enable-autologon.ps1') -Check
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'set-agent-power.ps1') -Check
  exit 0
}

if (-not $isAdmin) {
  Write-Host 'Re-opening as Administrator (accept the UAC prompt)...'
  Start-Process powershell -Verb RunAs -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
  exit 0
}

Set-Location $root
Write-Host "== $env:COMPUTERNAME - step 1/2: auto-logon (you will be asked for this laptop's Windows password once) =="
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'enable-autologon.ps1')
Write-Host ''
Write-Host "== $env:COMPUTERNAME - step 2/2: power plan =="
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'set-agent-power.ps1')
Write-Host ''
Write-Host '== final state =='
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'enable-autologon.ps1') -Check
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'set-agent-power.ps1') -Check
Write-Host ''
Write-Host 'If the first line says "auto-logon : ON" and the verdict says OK, this laptop is done.'
Write-Host 'The CRM shows the same under Lead Finder > Systems > this machine > reboot_survival within a few minutes.'
