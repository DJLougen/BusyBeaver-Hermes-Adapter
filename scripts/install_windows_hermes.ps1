param(
  [string]$HermesHome = "",
  [string]$BusyBeaverRepo = "C:\Users\basbe\Desktop\AI_Research\busybeaver",
  [string]$Model = "GestaltLabs/BusyBeaver-50M",
  [int]$Port = 8767,
  [switch]$StartServer
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $HermesHome) {
  if (Get-Command hermes -ErrorAction SilentlyContinue) {
    $configPath = (& hermes config path).Trim()
    if ($configPath) {
      $HermesHome = Split-Path -Parent $configPath
    }
  }
  if (-not $HermesHome) {
    $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
  }
}

$skillSource = Join-Path $root "hermes-skill\toolforge\busybeaver-policy\SKILL.md"
$skillDestDir = Join-Path $HermesHome "skills\toolforge\busybeaver-policy"
$skillDest = Join-Path $skillDestDir "SKILL.md"
$binDir = Join-Path $HermesHome "bin"
$shimDest = Join-Path $binDir "busybeaver-policy.ps1"
$invokeScript = Join-Path $root "scripts\invoke_busybeaver_policy.ps1"
$serverUrl = "http://127.0.0.1:$Port/v1/chat/completions"

if (-not (Test-Path -LiteralPath $skillSource)) {
  throw "Hermes BusyBeaver skill source not found: $skillSource"
}
if (-not (Test-Path -LiteralPath $HermesHome)) {
  throw "Hermes home not found: $HermesHome"
}

New-Item -ItemType Directory -Force -Path $skillDestDir | Out-Null
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillDest -Force

$shim = @"
param(
  [Parameter(Mandatory = `$true)]
  [string]`$Goal,
  [string]`$StateJson = "",
  [string]`$StateFile = "",
  [switch]`$Pretty
)
& "$invokeScript" -Goal `$Goal -StateJson `$StateJson -StateFile `$StateFile -ServerUrl "$serverUrl" -Pretty:`$Pretty
"@
Set-Content -LiteralPath $shimDest -Value $shim -Encoding UTF8

if (Get-Command hermes -ErrorAction SilentlyContinue) {
  hermes config set skills.config.busybeaver.server_url $serverUrl | Out-Null
  hermes config set skills.config.busybeaver.model BusyBeaver-50M | Out-Null
  hermes config set skills.config.busybeaver.model_path $Model | Out-Null
  hermes config set skills.config.busybeaver.adapter_repo $root | Out-Null
  hermes config set skills.config.busybeaver.busybeaver_repo $BusyBeaverRepo | Out-Null
}

if ($StartServer) {
  & (Join-Path $root "scripts\start_busybeaver_server.ps1") -BusyBeaverRepo $BusyBeaverRepo -Model $Model -Port $Port
}

Write-Output "Installed BusyBeaver into Windows Hermes."
Write-Output "  skill: $skillDest"
Write-Output "  command: $shimDest"
Write-Output "  server: $serverUrl"
Write-Output "  model: $Model"
