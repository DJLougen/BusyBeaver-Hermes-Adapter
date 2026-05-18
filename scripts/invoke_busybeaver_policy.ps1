param(
  [Parameter(Mandatory = $true)]
  [string]$Goal,

  [string]$StateJson = "",
  [string]$StateFile = "",
  [string]$ServerUrl = "http://127.0.0.1:8767/v1/chat/completions",
  [string]$Model = "BusyBeaver-50M",
  [switch]$Pretty
)

$ErrorActionPreference = "Stop"

if ($StateFile) {
  $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
} elseif ($StateJson) {
  $state = $StateJson | ConvertFrom-Json
} else {
  $state = @{
    repo_summary = ""
    current_step = 0
    recent_observations = @()
    open_files = @()
    last_tool = $null
    last_error = $null
  }
}

$availableTools = @(
  @{
    name = "read_file"
    description = "Read a workspace file."
    schema = @{ path = "string" }
  },
  @{
    name = "run_tests"
    description = "Run a safe test command."
    schema = @{ command = "string" }
  },
  @{
    name = "shell"
    description = "Run a sandboxed shell command."
    schema = @{ command = "string" }
  },
  @{
    name = "escalate"
    description = "Ask the main agent or user for help."
    schema = @{ reason = "string" }
  }
)

$payload = @{
  model = $Model
  messages = @(
    @{
      role = "system"
      content = "You are BusyBeaver. Emit exactly one strict JSON tool action and no prose."
    },
    @{
      role = "user"
      content = (@{
        goal = $Goal
        state = $state
        available_tools = $availableTools
      } | ConvertTo-Json -Depth 20 -Compress)
    }
  )
} | ConvertTo-Json -Depth 30

$response = Invoke-RestMethod -Uri $ServerUrl -Method Post -ContentType "application/json" -Body $payload -TimeoutSec 120
$content = [string]$response.choices[0].message.content

$json = $content | ConvertFrom-Json
if ($Pretty) {
  $json | ConvertTo-Json -Depth 20
} else {
  $json | ConvertTo-Json -Depth 20 -Compress
}
