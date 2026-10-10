# stop-agent.ps1 - deliberately STOP this machine's Lead Finder agent (CRM T1102, 2026-10-11).
# The CRM Systems card shows it per machine ("Stop (this machine)"), pasteable in any terminal:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "cd '<root>'; git pull --ff-only; .\scripts\stop-agent.ps1"
# Twin: scripts/start-agent.ps1 starts it back (jobs resume by themselves).
#
# 1) writes data\agent.stop (the supervisor loop exits instead of restarting the agent)
# 2) waits up to 20 s for the loop / agent to exit on their own (skip with -Hard)
# 3) kills what is left: the run-agent-loop.bat cmd loop, its agent-autostart.vbs wscript, the python
#    agent, and Chrome / node processes whose command line contains THIS folder (Playwright's browser +
#    driver; never the user's own Chrome, which does not run from here)
# 4) tells the CRM (lead-finder-agent action self_stop): this machine is switched off (= CRM "Stop agent")
#    and its claimed / running jobs go back to the queue unpinned, WITHOUT a T1101 "lost" mark.
#    If that call fails the agent is still stopped and a warning is printed.
# -DryRun prints what it would do and changes nothing. Windows PowerShell 5.1 compatible (no && || ?: ??).
param([switch]$Hard, [switch]$DryRun)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stop = Join-Path $root 'data\agent.stop'

function ReadDotEnv($path) {
  # Same source the agent uses: webscraper/config.py load_dotenv(ROOT/.env); process env wins (python-dotenv
  # does not override variables that are already set).
  $m = @{}
  if (Test-Path $path) {
    foreach ($line in Get-Content $path) {
      $t = $line.Trim()
      if ($t -eq '' -or $t.StartsWith('#')) { continue }
      if ($t.StartsWith('export ')) { $t = $t.Substring(7).Trim() }
      $i = $t.IndexOf('=')
      if ($i -lt 1) { continue }
      $k = $t.Substring(0, $i).Trim()
      $v = $t.Substring($i + 1).Trim()
      if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
      $m[$k] = $v
    }
  }
  return $m
}
function EnvVal($envMap, $name) {
  $p = [Environment]::GetEnvironmentVariable($name)
  if ($p) { return $p }
  if ($envMap.ContainsKey($name)) { return $envMap[$name] }
  return ''
}
function CrmTarget {
  # Mirrors `python -m webscraper agent --crm` (cli.py): token CRM_AGENT_TOKEN (fallback AGENT_TOKEN);
  # base CRM_FUNCTIONS_URL, else VITE_SUPABASE_URL + /functions/v1, else the HVT project. Device =
  # data\device_name (agent.py _device_name), else LEAD_FINDER_DEVICE, else the hostname.
  $e = ReadDotEnv (Join-Path $root '.env')
  $token = EnvVal $e 'CRM_AGENT_TOKEN'
  if (-not $token) { $token = EnvVal $e 'AGENT_TOKEN' }
  $base = EnvVal $e 'CRM_FUNCTIONS_URL'
  if (-not $base) {
    $supa = EnvVal $e 'VITE_SUPABASE_URL'
    if ($supa) { $base = $supa.TrimEnd('/') + '/functions/v1' } else { $base = 'https://fyfhkjxewzbyxdwspkuc.supabase.co/functions/v1' }
  }
  $dev = ''
  $dn = Join-Path $root 'data\device_name'
  if (Test-Path $dn) { $dev = (Get-Content $dn -Raw).Trim() }
  if (-not $dev) { $dev = EnvVal $e 'LEAD_FINDER_DEVICE' }
  if (-not $dev) { $dev = $env:COMPUTERNAME }
  return @{ token = $token; url = $base.TrimEnd('/') + '/lead-finder-agent'; device = $dev }
}
function AgentProcs {
  # Supervisor loop + python agent (same filter as fix-agent.ps1) + the hidden autostart wscript, plus
  # Chrome / node / Playwright browsers started from THIS folder only.
  # Everything is scoped to THIS root (another checkout on the same PC is left alone): the loop's command
  # line carries the .bat's full path; the agent is `python -m webscraper agent` with no path, so it counts
  # only as a child of one of our loops, or when its python / command line lives under this root.
  $me = $PID
  $rootLike = '*' + $root + '*'
  $all = @(Get-CimInstance Win32_Process)
  $loops = @($all | Where-Object { ($_.Name -ieq 'cmd.exe') -and ($_.CommandLine -like '*run-agent-loop.bat*') -and ($_.CommandLine -like $rootLike) })
  $loopIds = @($loops | ForEach-Object { $_.ProcessId })
  $all | Where-Object {
    ($_.ProcessId -ne $me) -and (
      ($loopIds -contains $_.ProcessId) -or
      (($_.CommandLine -like '*webscraper agent*') -and (($loopIds -contains $_.ParentProcessId) -or ($_.CommandLine -like $rootLike) -or ($_.ExecutablePath -like $rootLike))) -or
      (($_.Name -ieq 'wscript.exe') -and ($_.CommandLine -like '*agent-autostart.vbs*') -and ($_.CommandLine -like $rootLike)) -or
      ((@('chrome.exe', 'node.exe', 'chromium.exe', 'headless_shell.exe', 'chrome-headless-shell.exe') -contains $_.Name.ToLower()) -and ($_.CommandLine -like $rootLike))
    )
  }
}

Write-Host "stop-agent: $root  (machine $env:COMPUTERNAME)"
$t = CrmTarget
Write-Host "CRM: $($t.url)  device '$($t.device)'  token $(if ($t.token) { 'found' } else { 'MISSING (.env CRM_AGENT_TOKEN)' })"
$procs = @(AgentProcs)
if ($DryRun) {
  Write-Host "(-DryRun) would write $stop"
  if ($procs.Count -eq 0) { Write-Host "(-DryRun) no agent processes running from here" }
  foreach ($p in $procs) { Write-Host "(-DryRun) would stop PID $($p.ProcessId) $($p.Name): $($p.CommandLine)" }
  Write-Host "(-DryRun) would POST action self_stop for '$($t.device)'"
  exit 0
}

if (-not (Test-Path (Join-Path $root 'data'))) { New-Item -ItemType Directory -Path (Join-Path $root 'data') | Out-Null }
Set-Content -Path $stop -Value "stopped by hand (scripts/stop-agent.ps1) $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding ASCII
Write-Host "wrote data\agent.stop"

if (-not $Hard -and $procs.Count -gt 0) {
  Write-Host -NoNewline "waiting up to 20 s for a graceful exit "
  for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 2
    $procs = @(AgentProcs)
    if ($procs.Count -eq 0) { break }
    Write-Host -NoNewline "."
  }
  Write-Host ""
}
$killed = @()
# python agent + browsers first, then the loop + wscript (the loop exits on the sentinel anyway)
$order = @($procs | Where-Object { @('cmd.exe', 'wscript.exe') -notcontains $_.Name.ToLower() }) + @($procs | Where-Object { @('cmd.exe', 'wscript.exe') -contains $_.Name.ToLower() })
foreach ($p in $order) {
  try {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    $killed += "$($p.Name)#$($p.ProcessId)"
  } catch {
    if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { Write-Host "could not kill PID $($p.ProcessId) ($($p.Name)): $($_.Exception.Message)" }
  }
}

$crm = ''
if (-not $t.token) {
  $crm = 'WARNING: no CRM_AGENT_TOKEN in .env - the CRM was NOT told. Press Stop agent on the Systems card, or its jobs wait for auto-heal.'
} else {
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $body = @{ action = 'self_stop'; device = $t.device } | ConvertTo-Json -Compress
    $r = Invoke-RestMethod -Method Post -Uri $t.url -Headers @{ 'X-Agent-Token' = $t.token } -ContentType 'application/json' -Body $body -TimeoutSec 30
    $n = @($r.requeued).Count
    $crm = "CRM told: '$($t.device)' switched off; $n running job(s) put back in the queue (no T1101 loss)."
  } catch {
    $crm = "WARNING: could not tell the CRM ($($_.Exception.Message)). The agent is stopped anyway; press Stop agent on the Systems card so its jobs move now."
  }
}

Write-Host ""
Write-Host "STOPPED" -ForegroundColor Green
if ($killed.Count -eq 0) { Write-Host "killed: nothing (no agent processes were left running)" } else { Write-Host "killed: $($killed -join ', ')" }
Write-Host $crm
Write-Host "Start it back with:  .\scripts\start-agent.ps1   (or the Start command on the CRM Systems card)"
exit 0
