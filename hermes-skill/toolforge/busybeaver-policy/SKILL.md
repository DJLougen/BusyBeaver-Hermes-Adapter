---
name: busybeaver-policy
description: Use BusyBeaver-50M as a local strict-JSON tool-policy helper underneath Hermes/Spark when debugging, recovering from tool errors, or choosing the next compact tool action.
version: 0.1.0
author: GestaltLabs
license: MIT
metadata:
  hermes:
    tags: [busybeaver, tool-policy, debugging, harness, strict-json]
    category: toolforge
    related_skills: [systematic-debugging, test-driven-development, toolforge]
    config:
      - key: busybeaver.server_url
        description: BusyBeaver OpenAI-compatible chat completions endpoint
        default: "http://127.0.0.1:8767/v1/chat/completions"
        prompt: BusyBeaver endpoint
      - key: busybeaver.model
        description: BusyBeaver exposed model name
        default: "BusyBeaver-50M"
        prompt: BusyBeaver model label
      - key: busybeaver.model_path
        description: Local checkpoint path or Hugging Face model id
        default: "GestaltLabs/BusyBeaver-50M"
        prompt: BusyBeaver model path
---

# BusyBeaver Policy

BusyBeaver is a tiny tool-policy model for Hermes harness use. It is not a main chat model and should not replace Hermes/Spark planning.

Use BusyBeaver when the next step should be a compact, analyzable tool action:

- inspect source after a failing test
- choose between read, search, test, shell, memory, clarify, or escalate
- recover after a command error
- gate destructive actions behind approval or clarification
- produce state/action traces for debugging the harness

Do not use BusyBeaver for open-ended prose, broad planning, browser reasoning, full report writing, or unsandboxed shell execution.

## Runtime

The Windows install script creates a local command:

```powershell
C:\Users\basbe\AppData\Local\hermes\bin\busybeaver-policy.ps1 -Goal "Inspect the failing test before editing." -Pretty
```

It calls the configured BusyBeaver endpoint:

```text
http://127.0.0.1:8767/v1/chat/completions
```

If the endpoint is not running, start it from the adapter repo:

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\start_busybeaver_server.ps1
```

## Input Contract

Pass a compact state object with:

- `repo_summary`
- `current_step`
- `recent_observations`
- `open_files`
- `last_tool`
- `last_error`
- optional `policy_features`

The user message sent to the endpoint should be a JSON object:

```json
{
  "goal": "Fix the failing calculator test.",
  "state": {
    "repo_summary": "Files: calculator.py, test_calculator.py",
    "current_step": 0,
    "recent_observations": ["pytest failed in test_calculator.py"],
    "open_files": [],
    "last_tool": "run_tests",
    "last_error": "AssertionError"
  },
  "available_tools": [
    {
      "name": "read_file",
      "description": "Read a workspace file.",
      "schema": { "path": "string" }
    }
  ]
}
```

## Output Contract

BusyBeaver returns exactly one JSON object:

```json
{"tool":"read_file","args":{"path":"calculator.py"},"confidence":0.82,"state_update":"Inspect source before patching."}
```

Hermes should treat the output as advisory policy. Validate the selected tool against the active Hermes tool registry before any side effect. Keep shell actions dry-run or sandboxed unless the user explicitly approves.

The production endpoint uses BusyBeaver's harness argument resolver after model generation. The model selects the tool; the resolver grounds exact file paths, commands, message fields, cron fields, and memory values from `state.recent_observations` and `state.policy_features` before returning the action.

## Intended Boundary

BusyBeaver helps Hermes choose low-level tool policy. Hermes/Spark still owns user intent, decomposition, safety decisions, artifact quality, and final response synthesis.
