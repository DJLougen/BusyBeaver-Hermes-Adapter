param(
  [string]$BusyBeaverRepo = "C:\Users\basbe\Desktop\AI_Research\busybeaver",
  [string]$Model = "GestaltLabs/BusyBeaver-50M",
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8767,
  [string]$Device = "cpu",
  [string]$ExposedModel = "BusyBeaver-50M",
  [string]$Python = "python",
  [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$serverScript = Join-Path $root "server\busybeaver_openai_server.py"
$runDir = Join-Path $root ".run"
$outLog = Join-Path $runDir "busybeaver-server.out"
$errLog = Join-Path $runDir "busybeaver-server.err"
$pidFile = Join-Path $runDir "busybeaver-server.pid"
$healthUrl = "http://$HostName`:$Port/health"

if (-not (Test-Path -LiteralPath $serverScript)) {
  throw "BusyBeaver server script not found: $serverScript"
}
if (-not (Test-Path -LiteralPath $BusyBeaverRepo)) {
  throw "BusyBeaver repo not found: $BusyBeaverRepo"
}

New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($existing.OwningProcess)" -ErrorAction SilentlyContinue
  $cmd = if ($proc) { [string]$proc.CommandLine } else { "" }
  $isBusyBeaver = $cmd -like "*busybeaver_openai_server.py*"
  if ($ForceRestart -and $isBusyBeaver) {
    Stop-Process -Id $existing.OwningProcess -Force
    Start-Sleep -Seconds 1
  } elseif ($isBusyBeaver) {
    try {
      $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
      Write-Output "BusyBeaver server is already running."
      Write-Output "  url: http://$HostName`:$Port/v1"
      Write-Output "  pid: $($existing.OwningProcess)"
      Write-Output "  health: $($health | ConvertTo-Json -Compress)"
      exit 0
    } catch {
      throw "Port $Port is occupied by BusyBeaver process $($existing.OwningProcess), but health check failed. Re-run with -ForceRestart."
    }
  } else {
    throw "Port $Port is occupied by process $($existing.OwningProcess): $cmd"
  }
}

$env:BUSYBEAVER_REPO = $BusyBeaverRepo
$argsList = @(
  $serverScript,
  "--busybeaver-repo", $BusyBeaverRepo,
  "--model", $Model,
  "--host", $HostName,
  "--port", [string]$Port,
  "--device", $Device,
  "--exposed-model", $ExposedModel
)

$process = Start-Process -FilePath $Python -ArgumentList $argsList -WorkingDirectory $root -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id

$deadline = (Get-Date).AddSeconds(120)
do {
  Start-Sleep -Seconds 2
  if ($process.HasExited) {
    $err = if (Test-Path -LiteralPath $errLog) { Get-Content -LiteralPath $errLog -Raw } else { "" }
    throw "BusyBeaver server exited during startup. Log: $err"
  }
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
    Write-Output "BusyBeaver server started."
    Write-Output "  url: http://$HostName`:$Port/v1"
    Write-Output "  pid: $($process.Id)"
    Write-Output "  model: $Model"
    Write-Output "  health: $($health | ConvertTo-Json -Compress)"
    exit 0
  } catch {
  }
} while ((Get-Date) -lt $deadline)

$tail = if (Test-Path -LiteralPath $errLog) { Get-Content -LiteralPath $errLog -Tail 40 | Out-String } else { "" }
throw "BusyBeaver server did not become healthy at $healthUrl. Recent error log: $tail"
