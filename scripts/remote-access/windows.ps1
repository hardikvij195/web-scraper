# W214 (CRM T1555): remote access on a Lead Finder Windows machine. Called by webscraper/remote_access.py:
#   windows.ps1 remote_setup|remote_on|remote_off|claude_on|claude_off
# The RustDesk password arrives in $env:HVT_RD_PASSWORD only (never argv, never echoed).
# Every line starting "RESULT:" becomes part of the CRM command_result.
param([string]$Cmd = "")
$ErrorActionPreference = "Continue"
$script:Fail = 0
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path        # web-scraper checkout
$Work = (Resolve-Path (Join-Path $Root "..")).Path                   # its parent (hv-technologies folder)
$Device = if ($env:HVT_DEVICE) { $env:HVT_DEVICE } else { $env:COMPUTERNAME }
$RcTask = "HVT Claude Remote Control"
$RcLoop = Join-Path $env:LOCALAPPDATA "hvt-claude-rc.ps1"
$RcLog = Join-Path $env:LOCALAPPDATA "hvt-claude-rc.log"
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

function Res($t) { Write-Output "RESULT: $t" }
function Owner($t) { Write-Output "RESULT: OWNER: $t" }

function RdExe {
    foreach ($p in @("$env:ProgramFiles\RustDesk\rustdesk.exe", "${env:ProgramFiles(x86)}\RustDesk\rustdesk.exe",
                     "$env:LOCALAPPDATA\Programs\RustDesk\rustdesk.exe")) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

function Install-RustDesk {
    if (RdExe) { Res "RustDesk already installed"; return $true }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id RustDesk.RustDesk -e --silent --scope machine --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
        if (RdExe) { Res "RustDesk installed (winget)"; return $true }
    }
    try {
        $rel = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/rustdesk/rustdesk/releases/latest"
        $asset = $rel.assets | Where-Object { $_.name -match "x86_64\.exe$" } | Select-Object -First 1
        $tmp = Join-Path $env:TEMP $asset.name
        Invoke-WebRequest -UseBasicParsing $asset.browser_download_url -OutFile $tmp
        Start-Process $tmp -ArgumentList "--silent-install" -Wait
        Start-Sleep 10
    } catch { Res "RustDesk download FAILED ($($_.Exception.Message))" }
    if (RdExe) { Res "RustDesk installed (official .exe, silent)"; return $true }
    Res "RustDesk install FAILED"; $script:Fail = 1; return $false
}

function Set-TomlOption($file, $k, $v) {
    New-Item -ItemType Directory -Force (Split-Path $file) | Out-Null
    $t = if (Test-Path $file) { [IO.File]::ReadAllText($file) } else { "rendezvous_server = ''`nnat_type = 1`nserial = 0`n`n[options]`n" }
    if ($t -notmatch "(?m)^\[options\]") { $t += "`n[options]`n" }
    if ($t -match "(?m)^$([regex]::Escape($k)) = .*$") { $t = $t -replace "(?m)^$([regex]::Escape($k)) = .*$", "$k = '$v'" }
    else { $t = $t -replace "(?m)^\[options\]\r?$", "[options]`n$k = '$v'" }
    [IO.File]::WriteAllText($file, $t, (New-Object Text.UTF8Encoding $false))
}

function Configure-RustDesk {
    $exe = RdExe
    if ($IsAdmin) {
        & $exe --install-service | Out-Null; Start-Sleep 5
        sc.exe config RustDesk start= auto | Out-Null
    } else { Owner "agent is not elevated: RustDesk service install/--option need admin (run remote_setup again after the agent runs as admin, or set it in RustDesk Settings)" }
    $opts = [ordered]@{ "direct-server" = "Y"; "direct-access-port" = "21118"; "approve-mode" = "password";
                        "verification-method" = "use-permanent-password"; "allow-auto-disconnect" = "N" }
    $files = @("$env:APPDATA\RustDesk\config\RustDesk2.toml")
    if ($IsAdmin) { $files += "C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\RustDesk\config\RustDesk2.toml" }
    if ($IsAdmin) { sc.exe stop RustDesk | Out-Null; Start-Sleep 2 }
    foreach ($k in $opts.Keys) {
        if ($IsAdmin) { & $exe --option $k $opts[$k] | Out-Null }
        foreach ($f in $files) { try { Set-TomlOption $f $k $opts[$k] } catch {} }
    }
    Res "RustDesk options set: direct IP access on 21118, password-only approve"
    if ($env:HVT_RD_PASSWORD) {
        & $exe --password $env:HVT_RD_PASSWORD | Out-Null
        if ($LASTEXITCODE -eq 0 -or $IsAdmin) { Res "RustDesk permanent password set" }
        else { Res "RustDesk permanent password may NOT be set (needs admin)"; Owner "set it in RustDesk > Settings > Security"; $script:Fail = 1 }
    }
    RustDesk-On
}

function RustDesk-On {
    $exe = RdExe
    if (-not $exe) { Res "RustDesk not installed - send remote_setup first"; $script:Fail = 1; return }
    Remove-Item (Join-Path $Root "data\remote.off") -ErrorAction SilentlyContinue
    $svc = Get-Service RustDesk -ErrorAction SilentlyContinue
    if ($svc) {
        if ($IsAdmin) { sc.exe config RustDesk start= auto | Out-Null }
        Start-Service RustDesk -ErrorAction SilentlyContinue
    }
    if (-not (Get-Process rustdesk -ErrorAction SilentlyContinue)) { Start-Process $exe -ArgumentList "--tray" -WindowStyle Hidden }
    Start-Sleep 4
    $svc = Get-Service RustDesk -ErrorAction SilentlyContinue
    if (($svc -and $svc.Status -eq "Running") -or (Get-Process rustdesk -ErrorAction SilentlyContinue)) {
        Res "RustDesk running ($(if ($svc) { 'service ' + $svc.Status } else { 'no service, user process' })) - connect to $(Ts-Ip)"
    } else { Res "RustDesk did NOT start"; $script:Fail = 1 }
}

function RustDesk-Off {
    New-Item -ItemType Directory -Force (Join-Path $Root "data") | Out-Null
    Set-Content (Join-Path $Root "data\remote.off") (Get-Date -Format s)
    $svc = Get-Service RustDesk -ErrorAction SilentlyContinue
    if ($svc) {
        if ($IsAdmin) { sc.exe config RustDesk start= demand | Out-Null }
        Stop-Service RustDesk -Force -ErrorAction SilentlyContinue
    }
    Get-Process rustdesk -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 2
    $svc = Get-Service RustDesk -ErrorAction SilentlyContinue
    if (($svc -and $svc.Status -eq "Running") -or (Get-Process rustdesk -ErrorAction SilentlyContinue)) {
        Res "RustDesk still running (stopping the service needs admin)"; $script:Fail = 1
    } else { Res "RustDesk stopped - screens disabled (LF agent still polling; remote_on brings it back)" }
}

function Ts-Exe {
    foreach ($p in @("$env:ProgramFiles\Tailscale\tailscale.exe")) { if (Test-Path $p) { return $p } }
    $c = Get-Command tailscale -ErrorAction SilentlyContinue; if ($c) { return $c.Source }; return $null
}
function Ts-Ip { $t = Ts-Exe; if ($t) { (& $t ip -4 2>$null | Select-Object -First 1) } else { "?" } }

function Tailscale-Unattended {
    $t = Ts-Exe
    if (-not $t) { Res "Tailscale not found"; Owner "install + log in to Tailscale"; $script:Fail = 1; return }
    & $t set --unattended=true 2>$null | Out-Null
    $un = $LASTEXITCODE -eq 0
    if ($IsAdmin) { sc.exe config Tailscale start= auto | Out-Null }
    Res "Tailscale $(Ts-Ip); unattended (runs before login) $(if ($un) { 'ON' } else { 'NOT set' })"
    if (-not $un) { Owner "Tailscale tray > Preferences > Run unattended" }
}

function No-Sleep {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Res "sleep + hibernate on AC disabled (powercfg)"
}

function Claude-Exe {
    foreach ($p in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:APPDATA\npm\claude.cmd")) { if (Test-Path $p) { return $p } }
    $c = Get-Command claude -ErrorAction SilentlyContinue; if ($c) { return $c.Source }; return $null
}

function Install-Claude {
    $c = Claude-Exe
    if ($c) { Res "Claude Code present ($(& $c --version 2>$null | Select-Object -First 1))"; return }
    try { Invoke-Expression (Invoke-RestMethod -UseBasicParsing "https://claude.ai/install.ps1") | Out-Null } catch {}
    if (-not (Claude-Exe) -and (Get-Command npm -ErrorAction SilentlyContinue)) { npm i -g @anthropic-ai/claude-code | Out-Null }
    if (Claude-Exe) { Res "Claude Code installed"; Owner "log in once: open a terminal, run 'claude', /login" }
    else { Res "Claude Code install FAILED"; $script:Fail = 1 }
}

function Claude-On {
    $c = Claude-Exe
    if (-not $c) { Res "Claude Code not installed - send remote_setup"; $script:Fail = 1; return }
    # Restart loop: remote-control exits on network loss / update; come back after 60 s.
    $loop = @"
`$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath '$Work'
while (`$true) {
    "`$(Get-Date -Format s) starting remote-control" | Add-Content -LiteralPath '$RcLog'
    & '$c' remote-control --name '$Device' --permission-mode bypassPermissions *>> '$RcLog'
    Start-Sleep 60
}
"@
    [IO.File]::WriteAllText($RcLoop, $loop, (New-Object Text.UTF8Encoding $true))
    $arg = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RcLoop`""
    $act = New-ScheduledTaskAction -Execute "conhost.exe" -Argument $arg -WorkingDirectory $Work
    $trg = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
           -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $RcTask -Action $act -Trigger $trg -Settings $set -Force | Out-Null
    Start-ScheduledTask -TaskName $RcTask
    Start-Sleep 10
    $st = (Get-ScheduledTask -TaskName $RcTask -ErrorAction SilentlyContinue).State
    if ($st -eq "Running") { Res "always-on Claude running: remote-control '$Device' in $Work (task '$RcTask', at logon)" }
    else {
        $tail = if (Test-Path $RcLog) { ((Get-Content $RcLog -Tail 3) -join " ") } else { "" }
        Res "always-on Claude NOT running (task $st) $tail"; $script:Fail = 1
    }
    Owner "if claude.ai/code does not list '$Device': in a terminal run 'cd `"$Work`"; claude' once (login + trust folder)"
}

function Claude-Off {
    Stop-ScheduledTask -TaskName $RcTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $RcTask -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*hvt-claude-rc.ps1*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*remote-control*" -and $_.CommandLine -like "*claude*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Res "always-on Claude stopped (task removed; claude_on brings it back)"
}

switch ($Cmd) {
    "remote_setup" {
        if (Install-RustDesk) { Configure-RustDesk }
        Tailscale-Unattended; No-Sleep; Install-Claude; Claude-On
        Res "agent elevated: $IsAdmin"
    }
    "remote_on" { RustDesk-On }
    "remote_off" { RustDesk-Off }
    "claude_on" { Claude-On }
    "claude_off" { Claude-Off }
    default { Res "unknown command '$Cmd'"; $script:Fail = 1 }
}
exit $script:Fail
