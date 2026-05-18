param(
  [Parameter(Mandatory = $true)]
  [string]$HermesRepo,

  [string]$AdapterPath = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $AdapterPath) {
  $AdapterPath = Join-Path $root "adapter\agent-runner.py"
}

$target = Join-Path $HermesRepo "verification\agent-runner.py"
if (-not (Test-Path -LiteralPath $AdapterPath)) {
  throw "Adapter file not found: $AdapterPath"
}
if (-not (Test-Path -LiteralPath $target)) {
  throw "Hermes agent runner not found: $target"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$target.busybeaver-backup-$stamp"
Copy-Item -LiteralPath $target -Destination $backup -Force
Copy-Item -LiteralPath $AdapterPath -Destination $target -Force

Write-Output "Installed BusyBeaver adapter:"
Write-Output "  target: $target"
Write-Output "  backup: $backup"
