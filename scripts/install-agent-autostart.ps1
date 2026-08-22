# Registers the "HVT Lead Finder Agent" scheduled task so the scraper agent is
# always running: a CRM Lead Finder job starts on its own, with nothing to launch
# by hand. Re-runnable - it replaces any existing task of the same name.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-agent-autostart.ps1
#
# Uninstall:  Unregister-ScheduledTask -TaskName 'HVT Lead Finder Agent' -Confirm:$false

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vbs  = Join-Path $root 'scripts\agent-autostart.vbs'
$name = 'HVT Lead Finder Agent'
$me   = "$env:COMPUTERNAME\$env:USERNAME"

if (-not (Test-Path $vbs)) { throw "missing $vbs" }

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
  -Argument "`"$vbs`"" -WorkingDirectory $root

# At logon, plus a 10-minute re-check so the supervisor comes back even if the
# whole task was killed. MultipleInstances=IgnoreNew keeps it to one agent.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$repeat  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 10) `
  -RepetitionDuration ([TimeSpan]::FromDays(3650))
$trigger.Repetition = $repeat.Repetition

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999

$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description 'Keeps the HVT CRM Lead Finder scraper agent running so jobs created in the CRM start on their own.' | Out-Null

Write-Output "registered: $name"
