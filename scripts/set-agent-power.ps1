# W175 (CRM T1046/T1047, 2026-10-07) - stop an agent laptop from sleeping under the Lead Finder agent.
#
# Why: 4 - DELL went OFFLINE 16:40-16:59 while its own log said "this machine was asleep/suspended for ~417s"
# and "cloud unreachable: getaddrinfo failed". The agent's W149 keep-awake (SetThreadExecutionState) stops the
# idle timer, but it cannot override a lid-close action, a battery policy or hibernation. This script sets the
# active power plan so the laptop never sleeps or hibernates on AC or battery and does nothing when the lid
# closes, and keeps the network adapter awake. Run ONCE per agent laptop in an elevated PowerShell:
#
#     powershell -ExecutionPolicy Bypass -File scripts\set-agent-power.ps1          # apply
#     powershell -ExecutionPolicy Bypass -File scripts\set-agent-power.ps1 -Check   # report only
#
# Pair it with scripts\enable-autologon.ps1 (W173) so a reboot also comes back on its own.
param([switch]$Check)
$ErrorActionPreference = 'Stop'

function Val($sub, $setting) {
  $out = powercfg /query SCHEME_CURRENT $sub $setting 2>$null
  $acM = ($out | Select-String 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)' | Select-Object -First 1)
  $dcM = ($out | Select-String 'Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)' | Select-Object -First 1)
  $ac = if ($acM) { [convert]::ToInt32($acM.Matches[0].Groups[1].Value, 16) } else { -1 }
  $dc = if ($dcM) { [convert]::ToInt32($dcM.Matches[0].Groups[1].Value, 16) } else { -1 }
  return @{ ac = $ac; dc = $dc }
}
function Report {
  $sleep = Val 'SUB_SLEEP' 'STANDBYIDLE'
  $hib = Val 'SUB_SLEEP' 'HIBERNATEIDLE'
  $lid = Val 'SUB_BUTTONS' 'LIDACTION'
  $lidText = @{ -1 = 'n/a (no lid)'; 0 = 'do nothing'; 1 = 'sleep'; 2 = 'hibernate'; 3 = 'shut down' }
  Write-Host ("sleep after (s)     : AC {0}  battery {1}   (0 = never)" -f $sleep.ac, $sleep.dc)
  Write-Host ("hibernate after (s) : AC {0}  battery {1}   (0 = never)" -f $hib.ac, $hib.dc)
  Write-Host ("lid close           : AC {0}  battery {1}" -f $lidText[[int]$lid.ac], $lidText[[int]$lid.dc])
  $ok = ($sleep.ac -eq 0 -and $sleep.dc -le 0 -and $hib.ac -le 0 -and $hib.dc -le 0 -and $lid.ac -le 0 -and $lid.dc -le 0)
  Write-Host ("verdict             : {0}" -f $(if ($ok) { 'OK - this laptop will not sleep under the agent' } else { 'NOT hardened - run without -Check (elevated)' }))
}

if ($Check) { Report; exit 0 }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run this in an elevated (Administrator) PowerShell.' }

powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
powercfg /hibernate off 2>$null
# Network adapters: do not let Windows power them down (the agent lost DNS while suspended).
Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object {
  try { Disable-NetAdapterPowerManagement -Name $_.Name -ErrorAction Stop; Write-Host ("adapter power mgmt off: {0}" -f $_.Name) } catch {}
}
Report
Write-Host ''
Write-Host 'Done. Keep the laptop plugged in; the agent (W149) still blocks the idle timer while it runs.'
