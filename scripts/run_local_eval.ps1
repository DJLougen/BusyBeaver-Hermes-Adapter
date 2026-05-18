param(
  [Parameter(Mandatory = $true)]
  [string]$BusyBeaverRepo,

  [Parameter(Mandatory = $true)]
  [string]$HermesRepo,

  [string]$Model = "runs\eval_v10_fast_ckpt200",
  [int]$Port = 8767,
  [switch]$All
)

$ErrorActionPreference = "Stop"

$serverScript = Join-Path $BusyBeaverRepo "scripts\busybeaver_openai_server.py"
if (-not (Test-Path -LiteralPath $serverScript)) {
  throw "BusyBeaver server script not found: $serverScript"
}
if (-not (Test-Path -LiteralPath (Join-Path $HermesRepo "package.json"))) {
  throw "HermesAgent-20 checkout not found: $HermesRepo"
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
  Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}

$logDir = Join-Path $BusyBeaverRepo "runs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdout = Join-Path $logDir "busybeaver_adapter_server.out"
$stderr = Join-Path $logDir "busybeaver_adapter_server.err"
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

$server = Start-Process `
  -FilePath python `
  -ArgumentList @("scripts\busybeaver_openai_server.py", "--model", $Model, "--host", "0.0.0.0", "--port", "$Port", "--device", "cpu") `
  -WorkingDirectory $BusyBeaverRepo `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -PassThru `
  -WindowStyle Hidden

$ready = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    if ($health.ok) {
      $ready = $true
      break
    }
  } catch {}
  Start-Sleep -Seconds 2
}

if (-not $ready) {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  Write-Output "BusyBeaver server did not become ready."
  if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout }
  if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr }
  exit 1
}

try {
  $common = @(
    "run", "dev:run", "--",
    "--provider", "busybeaver",
    "--model", "BusyBeaver-50M",
    "--provider-model", "BusyBeaver-50M",
    "--label", "BusyBeaver-50M",
    "--base-url", "http://host.docker.internal:$Port/v1",
    "--auth-mode", "none"
  )

  if ($All) {
    $args = $common + @("--all")
  } else {
    $scenarios = @("HA-05", "HA-06", "HA-13", "HA-14", "HA-15", "HA-16", "HA-18", "HA-19", "HA-20")
    $scenarioArgs = @()
    foreach ($scenario in $scenarios) {
      $scenarioArgs += @("--scenario", $scenario)
    }
    $args = $common + $scenarioArgs + @("--verbose")
  }

  Push-Location $HermesRepo
  try {
    & npm @args
  } finally {
    Pop-Location
  }
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
