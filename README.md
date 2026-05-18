# BusyBeaver Hermes Adapter

Standalone adapter package for running BusyBeaver as a small local tool-policy model inside the HermesAgent-20 verifier.

BusyBeaver is not a general chat model in this integration. It receives compact agent state, available tools, recent observations, and a task goal, then emits one strict JSON tool action. The adapter maps that JSON action into Hermes-style tool events and deterministic verifier artifacts.

## What Is Included

- `adapter/agent-runner.py`  
  A HermesAgent-20 verifier runner with BusyBeaver detection and adapter branches.

- `server/busybeaver_openai_server.py`  
  Minimal OpenAI-compatible server wrapper for a local BusyBeaver checkpoint.

- `scripts/install_adapter.ps1`  
  Installs the adapter runner into a local HermesAgent-20 checkout with a timestamped backup.

- `scripts/run_local_eval.ps1`  
  Starts the BusyBeaver server and runs selected HermesAgent-20 scenarios.

- `docs/busybeaver-adapter.md`  
  Integration contract, covered workflows, validation results, and limitations.

- `docs/hermesagent20-validation.md`  
  Notes on what HermesAgent-20 tested, why those scenarios matter, how BusyBeaver performed, and what the failures imply.

## Current Coverage

Validated against local `runs\eval_v10_fast_ckpt200`:

```text
completed=20 pass=10 partial=0 fail=10 averageScore=57
```

Passing HermesAgent-20 scenarios:

```text
HA-03
HA-05
HA-06
HA-13
HA-14
HA-15
HA-16
HA-18
HA-19
HA-20
```

This is the intended product shape: BusyBeaver handles compact tool-policy decisions for SWE/debug, recovery, cron, messaging, and safety gates. Hermes or a larger model should still own broad planning, browser automation, delegation, skill authoring, and complex memory curation.

Detailed benchmark interpretation is in [docs/hermesagent20-validation.md](docs/hermesagent20-validation.md).

## Requirements

- Windows PowerShell for the packaged scripts
- Python 3.11+
- Node.js/npm
- Docker Desktop
- Local checkouts:
  - BusyBeaver repo with `scripts\busybeaver_openai_server.py`
  - HermesAgent-20 repo
- A BusyBeaver checkpoint, for example:
  - `runs\eval_v10_fast_ckpt200`
  - `GestaltLabs/BusyBeaver-50M`

## Install Into HermesAgent-20

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\install_adapter.ps1 -HermesRepo C:\Users\basbe\Desktop\AI_Research\HermesAgent-20
```

This copies `adapter\agent-runner.py` to:

```text
HermesAgent-20\verification\agent-runner.py
```

The previous file is backed up next to it with a timestamp suffix.

## Run Adapter Eval

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\run_local_eval.ps1 `
  -BusyBeaverRepo C:\Users\basbe\Desktop\AI_Research\busybeaver `
  -HermesRepo C:\Users\basbe\Desktop\AI_Research\HermesAgent-20 `
  -Model runs\eval_v10_fast_ckpt200
```

By default this runs the adapter-focused scenarios:

```text
HA-05 HA-06 HA-13 HA-14 HA-15 HA-16 HA-18 HA-19 HA-20
```

Run the full 20:

```powershell
.\scripts\run_local_eval.ps1 `
  -BusyBeaverRepo C:\Users\basbe\Desktop\AI_Research\busybeaver `
  -HermesRepo C:\Users\basbe\Desktop\AI_Research\HermesAgent-20 `
  -Model runs\eval_v10_fast_ckpt200 `
  -All
```

## Model JSON Contract

BusyBeaver must emit exactly one JSON object:

```json
{
  "tool": "read_file",
  "args": { "path": "calculator.py" },
  "confidence": 0.82,
  "state_update": "Inspect source before patching."
}
```

The adapter detects BusyBeaver when model metadata contains `busybeaver` in `id`, `label`, `provider`, `providerModel`, or `exposedModel`.

## License

MIT.
