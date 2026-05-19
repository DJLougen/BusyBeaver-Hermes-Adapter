param(
  [string]$BusyBeaverRepo = "C:\Users\basbe\Desktop\AI_Research\busybeaver",
  [string]$BusyBeeCpuRepo = "C:\Users\basbe\Desktop\AI_Research\busyBee-cpu",
  [string]$HermesRepo = "C:\Users\basbe\Desktop\AI_Research\HermesAgent-20",
  [string]$BusyBeaverModel = "GestaltLabs/BusyBeaver-50M",
  [string]$CpuModelPath = "",
  [int]$BusyBeaverPort = 8767,
  [int]$BusyBeeCpuPort = 8768,
  [string[]]$Scenario = @("HA-05", "HA-06", "HA-13", "HA-14", "HA-15", "HA-16", "HA-18", "HA-19", "HA-20"),
  [switch]$All,
  [switch]$SkipBusyBeaver,
  [switch]$SkipCpu,
  [switch]$RetrainCpu
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root ".run\policy-benchmark"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $BusyBeaverRepo "scripts\busybeaver_openai_server.py"))) {
  throw "BusyBeaver repo/server not found: $BusyBeaverRepo"
}
if (-not (Test-Path -LiteralPath (Join-Path $BusyBeeCpuRepo "scripts\serve_policy.py"))) {
  throw "busyBee-cpu repo/server not found: $BusyBeeCpuRepo"
}
if (-not (Test-Path -LiteralPath (Join-Path $HermesRepo "package.json"))) {
  throw "HermesAgent-20 checkout not found: $HermesRepo"
}

if (-not $CpuModelPath) {
  $CpuModelPath = Join-Path $BusyBeeCpuRepo "runs\hermes_policy.joblib"
}

function Stop-PortProcess([int]$Port) {
  $existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($existing) {
    Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }
}

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process, [int]$Seconds = 120) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if ($Process.HasExited) {
      return $false
    }
    try {
      $health = Invoke-RestMethod -Uri $Url -TimeoutSec 3
      if ($health.ok) {
        return $true
      }
    } catch {}
    Start-Sleep -Seconds 2
  }
  return $false
}

function Start-BusyBeaverServer {
  Stop-PortProcess $BusyBeaverPort
  $stdout = Join-Path $runDir "busybeaver-server.out"
  $stderr = Join-Path $runDir "busybeaver-server.err"
  Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
  $proc = Start-Process `
    -FilePath python `
    -ArgumentList @("scripts\busybeaver_openai_server.py", "--model", $BusyBeaverModel, "--host", "0.0.0.0", "--port", "$BusyBeaverPort", "--device", "cpu") `
    -WorkingDirectory $BusyBeaverRepo `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru
  if (-not (Wait-Health "http://127.0.0.1:$BusyBeaverPort/health" $proc)) {
    $err = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "" }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    throw "BusyBeaver server did not become healthy. $err"
  }
  return $proc
}

function Ensure-CpuModel {
  if ($RetrainCpu -or -not (Test-Path -LiteralPath $CpuModelPath)) {
    $trainPath = Join-Path $BusyBeaverRepo "data\train_v12\train.jsonl"
    if (-not (Test-Path -LiteralPath $trainPath)) {
      throw "CPU training source not found: $trainPath"
    }
    Push-Location $BusyBeeCpuRepo
    try {
      & python scripts\train_policy.py --train $trainPath --model-out $CpuModelPath
      if ($LASTEXITCODE -ne 0) {
        throw "CPU policy training failed with exit code $LASTEXITCODE"
      }
    } finally {
      Pop-Location
    }
  }
}

function Start-CpuServer {
  Ensure-CpuModel
  Stop-PortProcess $BusyBeeCpuPort
  $stdout = Join-Path $runDir "busybee-cpu-server.out"
  $stderr = Join-Path $runDir "busybee-cpu-server.err"
  Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
  $proc = Start-Process `
    -FilePath python `
    -ArgumentList @("scripts\serve_policy.py", "--model", $CpuModelPath, "--host", "0.0.0.0", "--port", "$BusyBeeCpuPort", "--exposed-model", "busybee-cpu") `
    -WorkingDirectory $BusyBeeCpuRepo `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru
  if (-not (Wait-Health "http://127.0.0.1:$BusyBeeCpuPort/health" $proc)) {
    $err = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "" }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    throw "busyBee-cpu server did not become healthy. $err"
  }
  return $proc
}

function Build-ScenarioArgs {
  if ($All) {
    return @("--all")
  }
  $args = @()
  foreach ($item in $Scenario) {
    $args += @("--scenario", $item)
  }
  return $args
}

function Run-HermesBenchmark([string]$Name, [string]$Provider, [string]$Model, [string]$BaseUrl, [int]$Port) {
  $logPath = Join-Path $runDir "$Name.log"
  $errPath = Join-Path $runDir "$Name.err"
  $jsonPath = Join-Path $runDir "$Name.jsonl"
  Remove-Item -LiteralPath $logPath, $errPath, $jsonPath -Force -ErrorAction SilentlyContinue

  $common = @(
    "run", "dev:run", "--",
    "--provider", $Provider,
    "--model", $Model,
    "--provider-model", $Model,
    "--label", $Model,
    "--base-url", $BaseUrl,
    "--auth-mode", "none",
    "--json"
  ) + (Build-ScenarioArgs)

  Push-Location $HermesRepo
  try {
    $proc = Start-Process `
      -FilePath "npm.cmd" `
      -ArgumentList $common `
      -WorkingDirectory $HermesRepo `
      -RedirectStandardOutput $logPath `
      -RedirectStandardError $errPath `
      -NoNewWindow `
      -Wait `
      -PassThru
    $exitCode = $proc.ExitCode
  } finally {
    Pop-Location
  }

  if (Test-Path -LiteralPath $errPath) {
    Add-Content -LiteralPath $logPath -Value "`n--- stderr ---"
    Get-Content -LiteralPath $errPath | Add-Content -LiteralPath $logPath
  }

  $summary = Select-String -Path $logPath -Pattern "completed=.*averageScore=.*" | Select-Object -Last 1
  $dockerError = Select-String -Path $logPath -Pattern "dockerDesktopLinuxEngine|docker build|error during connect" | Select-Object -First 1
  $result = @{
    name = $Name
    provider = $Provider
    model = $Model
    port = $Port
    exitCode = $exitCode
    summary = if ($summary) { $summary.Line } else { "" }
    blockedBy = if ($dockerError) { "docker" } else { "" }
    error = if ($dockerError) { $dockerError.Line } else { "" }
    log = $logPath
  }
  $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jsonPath
  return $result
}

Write-Output "Installing current adapter runner into HermesAgent-20..."
& (Join-Path $root "scripts\install_adapter.ps1") -HermesRepo $HermesRepo | Out-Host

$processes = @()
$results = @()
try {
  if (-not $SkipBusyBeaver) {
    Write-Output "Starting BusyBeaver backend on port $BusyBeaverPort..."
    $processes += Start-BusyBeaverServer
    $results += Run-HermesBenchmark "with-busybeaver" "busybeaver" "BusyBeaver-50M" "http://host.docker.internal:$BusyBeaverPort/v1" $BusyBeaverPort
  }

  if (-not $SkipCpu) {
    Write-Output "Starting busyBee-cpu backend on port $BusyBeeCpuPort..."
    $processes += Start-CpuServer
    $results += Run-HermesBenchmark "with-busybee-cpu" "busybee-cpu" "busybee-cpu" "http://host.docker.internal:$BusyBeeCpuPort/v1" $BusyBeeCpuPort
  }
} finally {
  foreach ($proc in $processes) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  }
}

$summaryPath = Join-Path $runDir "summary.json"
$results | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath
Write-Output "Benchmark summary:"
$results | ConvertTo-Json -Depth 6
Write-Output "Logs: $runDir"
