# W173 (CRM T1046, 2026-10-07) - make a Lead Finder agent laptop survive an unattended reboot.
#
# Why: the "HVT Lead Finder Agent" scheduled task (install-agent-autostart.ps1) fires AT LOGON of the interactive
# user, because Playwright's headed Chrome and WhatsApp Web need a real desktop session. When Windows Update (or a
# crash) reboots the laptop on its own, it stops at the login screen, nobody logs on, the task never fires and the
# machine shows OFFLINE in the CRM until someone signs in by hand (MI 2026-10-07 16:32, DELL/ASUS/MI before).
#
# Fix: turn on Windows auto-logon for the agent's user so a reboot lands straight on the desktop and the logon task
# starts the agent. Rev 2 (2026-10-07 18:20): no Sysinternals download any more - the first version ran Autologon64
# on MI and nothing was written (auto-logon stayed OFF, no error). This version does exactly what Autologon does,
# natively: DefaultUserName / DefaultDomainName / AutoAdminLogon=1 in Winlogon, and the password as the LSA secret
# "DefaultPassword" (never the plaintext registry value), verifying every step and failing loudly.
#
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1          # asks for the Windows password
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1 -Check   # report only, change nothing
#     powershell -ExecutionPolicy Bypass -File scripts\enable-autologon.ps1 -Disable # turn auto-logon off again
#
# Trade-off: anyone who powers the laptop on reaches the desktop without a password. Keep BitLocker on and lock
# the screen when leaving - the lock screen still asks for the password; only the post-reboot logon is automatic.
# Microsoft-account users: Settings > Accounts > Sign-in options > "For improved security, only allow Windows
# Hello sign-in" must be OFF, otherwise Windows ignores AutoAdminLogon.
param(
  [switch]$Check,
  [switch]$Disable
)
$ErrorActionPreference = 'Stop'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$taskName = 'HVT Lead Finder Agent'

# --- LSA secret writer (what Sysinternals Autologon does under the hood) -------------------------------------------
$lsaSource = @"
using System;
using System.Runtime.InteropServices;
public static class HvtLsa {
  [StructLayout(LayoutKind.Sequential)] public struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)] public struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
  [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)] static extern uint LsaOpenPolicy(ref LSA_UNICODE_STRING SystemName, ref LSA_OBJECT_ATTRIBUTES ObjectAttributes, uint DesiredAccess, out IntPtr PolicyHandle);
  [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)] static extern uint LsaStorePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName, ref LSA_UNICODE_STRING PrivateData);
  [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)] static extern uint LsaRetrievePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName, out IntPtr PrivateData);
  [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)] static extern uint LsaClose(IntPtr ObjectHandle);
  [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)] static extern uint LsaFreeMemory(IntPtr Buffer);
  [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
  static LSA_UNICODE_STRING S(string s) { var u = new LSA_UNICODE_STRING(); u.Buffer = Marshal.StringToHGlobalUni(s); u.Length = (ushort)(s.Length * 2); u.MaximumLength = (ushort)((s.Length + 1) * 2); return u; }
  static IntPtr Open(uint access) { var sys = new LSA_UNICODE_STRING(); var attrs = new LSA_OBJECT_ATTRIBUTES(); attrs.Length = Marshal.SizeOf(attrs); IntPtr h; uint st = LsaOpenPolicy(ref sys, ref attrs, access, out h); if (st != 0) throw new Exception("LsaOpenPolicy failed: win32 " + LsaNtStatusToWinError(st)); return h; }
  public static void Store(string key, string value) {
    IntPtr h = Open(0x00F0FFF);   // POLICY_ALL_ACCESS (admin)
    try { var k = S(key); LSA_UNICODE_STRING v = S(value); uint st = LsaStorePrivateData(h, ref k, ref v); if (st != 0) throw new Exception("LsaStorePrivateData failed: win32 " + LsaNtStatusToWinError(st)); }
    finally { LsaClose(h); }
  }
  public static bool Exists(string key) {
    IntPtr h = Open(0x00000004 | 0x00000800);   // POLICY_GET_PRIVATE_INFORMATION | POLICY_LOOKUP_NAMES
    try { var k = S(key); IntPtr p; uint st = LsaRetrievePrivateData(h, ref k, out p); if (st != 0) return false; LsaFreeMemory(p); return true; }
    finally { LsaClose(h); }
  }
}
"@
if (-not ([System.Management.Automation.PSTypeName]'HvtLsa').Type) { Add-Type -TypeDefinition $lsaSource -ErrorAction Stop }

function Report {
  $w = Get-ItemProperty $winlogon -ErrorAction SilentlyContinue
  $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  $secretTxt = 'n/a (run as Administrator to read)'
  try { $secretTxt = $(if ([HvtLsa]::Exists('DefaultPassword')) { 'present' } else { 'missing' }) } catch { }
  Write-Host ("auto-logon : {0} (user {1}\{2}; LSA DefaultPassword secret {3})" -f ($(if ($w.AutoAdminLogon -eq '1') { 'ON' } else { 'OFF' }), $w.DefaultDomainName, $w.DefaultUserName, $secretTxt))
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
  Write-Host 'auto-logon turned OFF (the LSA secret stays but is ignored while AutoAdminLogon=0)'
  Report; exit 0
}

$user = $env:USERNAME
$domain = $env:USERDOMAIN
if (-not $domain -or $domain -eq '') { $domain = $env:COMPUTERNAME }
$secure = Read-Host -AsSecureString ("Windows password for {0}\{1} (stored as an LSA secret, never echoed)" -f $domain, $user)
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $plain) { throw 'empty password - nothing changed' }

# Verify the password really is this account's password before trusting it to the boot sequence.
Add-Type -AssemblyName System.DirectoryServices.AccountManagement
$ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext([System.DirectoryServices.AccountManagement.ContextType]::Machine)
$ok = $false
try { $ok = $ctx.ValidateCredentials($user, $plain) } catch { $ok = $false }
if (-not $ok) {
  Write-Host 'WARNING: Windows did not accept that password for this local account (a Microsoft-account PIN/Hello password cannot be validated here).'
  $go = Read-Host 'Store it anyway? (y/N)'
  if ($go -ne 'y') { $plain = $null; throw 'not stored - run again with the account password' }
}

[HvtLsa]::Store('DefaultPassword', $plain)
$plain = $null
Set-ItemProperty $winlogon -Name DefaultUserName -Value $user
Set-ItemProperty $winlogon -Name DefaultDomainName -Value $domain
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '1'
Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
Remove-ItemProperty $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue   # never keep plaintext
Start-Sleep -Seconds 1
Report
$w = Get-ItemProperty $winlogon
if ($w.AutoAdminLogon -ne '1' -or $w.DefaultUserName -ne $user -or -not [HvtLsa]::Exists('DefaultPassword')) { throw 'verification failed - auto-logon is NOT configured' }
Write-Host ''
Write-Host 'Done. Next unattended reboot lands on the desktop and the "HVT Lead Finder Agent" logon task starts the agent.'
