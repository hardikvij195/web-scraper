# start-agent.ps1 - START this machine's Lead Finder agent back after a stop (CRM T1102, 2026-10-11).
# The CRM Systems card shows it per machine ("Start (this machine, jobs resume)"), pasteable in any terminal:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "cd '<root>'; git pull --ff-only; .\scripts\start-agent.ps1"
# Twin of scripts/stop-agent.ps1. No pull / pip reinstall here - that is fix-agent.ps1's job.
#
# 1) removes data\agent.stop
# 2) tells the CRM (lead-finder-agent action self_start): this machine is switched ON again, so the
#    restarted agent is offered jobs (a CRM "Stop agent" leaves enabled=false, and an agent started only
#    locally would sit online + parked with nothing to do)
# 3) runs the "HVT Lead Finder Agent" scheduled task (registers it first if missing)
# 4) waits up to 60 s for a fresh "agent up (" line in data\agent.log -> STARTED, else NOT UP + log tail
# -DryRun prints what it would do and changes nothing. Windows PowerShell 5.1 compatible (no && || ?: ??).
param([switch]$DryRun)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$task = 'HVT Lead Finder Agent'
$stop = Join-Path $root 'data\agent.stop'
$log = Join-Path $root 'data\agent.log'

function ReadDotEnv($path) {
  # Same source the agent uses: webscraper/config.py load_dotenv(ROOT/.env); process env wins.
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
  # Mirrors `python -m webscraper agent --crm` (cli.py) - see stop-agent.ps1.
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

Write-Host "start-agent: $root  (machine $env:COMPUTERNAME)"
$t = CrmTarget
Write-Host "CRM: $($t.url)  device '$($t.device)'  token $(if ($t.token) { 'found' } else { 'MISSING (.env CRM_AGENT_TOKEN)' })"
$null = cmd /c "schtasks /Query /TN `"$task`" >nul 2>&1"
$registered = ($LASTEXITCODE -eq 0)
if ($DryRun) {
  Write-Host "(-DryRun) data\agent.stop: $(if (Test-Path $stop) { 'present - would remove' } else { 'absent' })"
  Write-Host "(-DryRun) would POST action self_start for '$($t.device)'"
  Write-Host "(-DryRun) scheduled task '$task': $(if ($registered) { 'registered - would run it' } else { 'NOT registered - would register it via install-agent-autostart.ps1, then run it' })"
  exit 0
}

if (Test-Path $stop) { Remove-Item $stop -Force; Write-Host "removed data\agent.stop" } else { Write-Host "data\agent.stop: absent" }

$crm = ''
if (-not $t.token) {
  $crm = 'WARNING: no CRM_AGENT_TOKEN in .env - the CRM was NOT told. If the card says Stopped, press Start agent on it.'
} else {
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $body = @{ action = 'self_start'; device = $t.device } | ConvertTo-Json -Compress
    $null = Invoke-RestMethod -Method Post -Uri $t.url -Headers @{ 'X-Agent-Token' = $t.token } -ContentType 'application/json' -Body $body -TimeoutSec 30
    $crm = "CRM told: '$($t.device)' switched on - it takes jobs again."
  } catch {
    $crm = "WARNING: could not tell the CRM ($($_.Exception.Message)). If the card says Stopped, press Start agent on it."
  }
}
Write-Host $crm

$offset = 0
if (Test-Path $log) { $offset = (Get-Item $log).Length }
if (-not $registered) {
  Write-Host "registering the scheduled task ..."
  powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install-agent-autostart.ps1')
}
cmd /c "schtasks /Change /TN `"$task`" /ENABLE >nul 2>&1"
Write-Host "schtasks /Run '$task' ..."
cmd /c "schtasks /Run /TN `"$task`" 2>&1" | ForEach-Object { Write-Host "  $_" }

$upLine = ''
for ($i = 0; $i -lt 12; $i++) {
  Start-Sleep -Seconds 5
  $new = TailSince $log $offset
  $hit = @($new -split "`r?`n" | Where-Object { $_ -like '*agent up (*' })
  if ($hit.Count -gt 0) { $upLine = $hit[$hit.Count - 1]; break }
  Write-Host -NoNewline "."
}
Write-Host ""
if ($upLine -ne '') {
  Write-Host "STARTED" -ForegroundColor Green
  Write-Host $upLine
  Write-Host "Its jobs resume by themselves (in-flight ones first). The CRM card turns online within ~1 minute."
  exit 0
}
Write-Host "NOT UP - no 'agent up (' line within 60 s. Last 15 lines of data\agent.log:" -ForegroundColor Yellow
if (Test-Path $log) { Get-Content $log -Tail 15 | ForEach-Object { Write-Host $_ } } else { Write-Host "no agent.log" }
Write-Host "If it stays down, paste the Fix command from the CRM Systems card (pull + reinstall + restart)."
exit 1
