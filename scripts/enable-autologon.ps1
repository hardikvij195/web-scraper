# W173 (CRM T1046, 2026-10-07) - make a Lead Finder agent laptop survive an unattended reboot.
#
# Why: the "HVT Lead Finder Agent" scheduled task (install-agent-autostart.ps1) fires AT LOGON of the interactive
# user, because Playwright's headed Chrome and WhatsApp Web need a real desktop session. When Windows Update (or a
# crash) reboots the laptop on its own, it stops at the login screen, nobody logs on, the task never fires and the
# machine shows OFFLINE in the CRM until someone signs in by hand (MI 2026-10-07 16:32, DELL/ASUS/MI before).
#
# Fix: turn on Windows auto-logon for the agent's user so a reboot lands straight on the desktop and the logon task
# starts the agent. The password is stored by Sysinternals Autologon as an LSA secret (not the plaintext registry
# DefaultPassword value). Run ONCE per agent laptop, in an elevated PowerShell, from the web-scraper folder:
#
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1          # asks for the Windows password
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1 -Check   # report only, change nothing
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1 -Disable # turn auto-logon off again
#
# Trade-off: anyone who powers the laptop on reaches the desktop without a password. Keep BitLocker on and lock
# the screen when leaving - the lock screen still asks for the password; only the post-reboot logon is automatic.
param(
  [switch]$Check,
  [switch]$Disable
)
$ErrorActionPreference = 'Stop'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$taskName = 'HVT Lead Finder Agent'

function Report {
  $w = Get-ItemProperty $winlogon -ErrorAction SilentlyContinue
  $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Write-Host ("auto-logon : {0} (user {1}\{2})" -f ($(if ($w.AutoAdminLogon -eq '1') { 'ON' } else { 'OFF' }), $w.DefaultDomainName, $w.DefaultUserName))
  if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Host ("agent task : {0}, last run {1}, result {2}, logon type {3}" -f $t.State, $i.LastRunTime, $i.LastTaskResult, $t.Principal.LogonType)
  } else {
    Write-Host "agent task : NOT installed - run scripts\install-agent-autostart.ps1 first"
  }
  Write-Host ("last boot  : {0}" -f (Get-CimInstance Win32_OperatingSystem).LastBootUpTime)
}

if ($Check) { Report; exit 0 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run this in an elevated (Administrator) PowerShell.' }

if ($Disable) {
  Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '0'
  Remove-ItemProperty $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
  Write-Host 'auto-logon turned OFF (LSA secret, if any, is ignored while AutoAdminLogon=0)'
  Report; exit 0
}

# Sysinternals Autologon writes the password as the LSA "DefaultPassword" secret and sets AutoAdminLogon=1.
$tools = Join-Path $env:TEMP 'hvt-autologon'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$exe = Join-Path $tools 'Autologon64.exe'
if (-not (Test-Path $exe)) {
  Write-Host 'downloading Sysinternals Autologon...'
  Invoke-WebRequest -Uri 'https://live.sysinternals.com/Autologon64.exe' -OutFile $exe -UseBasicParsing
}
$user = $env:USERNAME
$domain = $env:USERDOMAIN
$secure = Read-Host -AsSecureString ("Windows password for {0}\{1} (stored as an LSA secret, never echoed)" -f $domain, $user)
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $plain) { throw 'empty password - nothing changed' }
& $exe /accepteula $user $domain $plain
$plain = $null
Start-Sleep -Seconds 1
Report
Write-Host ''
Write-Host 'Done. Next unattended reboot lands on the desktop and the "HVT Lead Finder Agent" logon task starts the agent.'
Write-Host 'Also recommended on each laptop: Settings > System > Power - never sleep on AC (the agent already blocks sleep while it runs, W149).'
