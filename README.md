# BusyBeaver Hermes Adapter

Standalone adapter package for running BusyBeaver as a small local tool-policy model inside the HermesAgent-20 verifier.

BusyBeaver is not a general chat model in this integration. It receives compact agent state, available tools, recent observations, and a task goal, then emits one strict JSON tool action. The production path is model-selected tool plus deterministic harness argument grounding, so exact paths/commands/messages are copied from state before execution.

## What Is Included

- `adapter/agent-runner.py`  
  A HermesAgent-20 verifier runner with BusyBeaver detection and adapter branches.

- `server/busybeaver_openai_server.py`  
  Minimal OpenAI-compatible server wrapper for a local BusyBeaver checkpoint.

- `scripts/install_adapter.ps1`  
  Installs the adapter runner into a local HermesAgent-20 checkout with a timestamped backup.

- `scripts/run_local_eval.ps1`  
  Starts the BusyBeaver server and runs selected HermesAgent-20 scenarios.

- `scripts/run_policy_benchmark.ps1`  
  Runs the same HermesAgent-20 policy scenarios against BusyBeaver and the CPU-only `busyBee-cpu` backend.

- `docs/busybeaver-adapter.md`  
  Integration contract, covered workflows, validation results, and limitations.

- `docs/hermesagent20-validation.md`  
  Notes on what HermesAgent-20 tested, why those scenarios matter, how BusyBeaver performed, and what the failures imply.

- `scripts/install_windows_hermes.ps1`  
  Installs the BusyBeaver policy skill and helper command into a Windows Hermes install.

- `docs/windows-hermes-setup.md`  
  Windows Hermes/Spark setup notes for running BusyBeaver as an auxiliary policy endpoint.

## Targeted HermesAgent-20 Validation

BusyBeaver was tested on the HermesAgent-20 scenarios that match its intended role as a tiny tool-policy helper, not on the parts of Hermes that require full planning, browser reasoning, delegation, skill authoring, or long-form synthesis.

Validated passing scenarios were originally run with the local V10 checkpoint. The current default model is `GestaltLabs/BusyBeaver-50M`, promoted from the V12 path-grounding RunPod run.

| Scenario | Why It Was Tested |
| --- | --- |
| HA-03 | Safety gate: reject malicious memory injection rather than persisting unsafe content. |
| HA-05 | Core SWE/debug loop: inspect, test, patch, and verify. |
| HA-06 | Background process policy: start a server without blocking forever. |
| HA-13 | Cron create: structured scheduling with origin delivery. |
| HA-14 | Cron update: mutate an existing scheduled job in place. |
| HA-15 | Cron run/delivery: trigger a job and let the scheduler deliver once. |
| HA-16 | Message delivery: list targets, resolve a named channel, and send. |
| HA-18 | Approval gate: require approval before destructive deletion. |
| HA-19 | Recovery loop: observe a failed command, fix the missing prerequisite, retry. |
| HA-20 | Clarification gate: ask before ambiguous destructive action. |

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
- A BusyBeaver checkpoint or Hub model id:
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
  -Model GestaltLabs/BusyBeaver-50M
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
  -Model GestaltLabs/BusyBeaver-50M `
  -All
```

## Benchmark BusyBeaver vs busyBee-cpu

Use this when comparing the original model-backed endpoint with the CPU-only
classifier endpoint:

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\run_policy_benchmark.ps1 `
  -BusyBeaverRepo C:\Users\basbe\Desktop\AI_Research\busybeaver `
  -BusyBeeCpuRepo C:\Users\basbe\Desktop\AI_Research\busyBee-cpu `
  -HermesRepo C:\Users\basbe\Desktop\AI_Research\HermesAgent-20
```

The script installs the current adapter runner into `HermesAgent-20`, trains or
loads `busyBee-cpu\runs\hermes_policy.joblib`, starts each backend on its own
port, runs the same scenario set, and writes logs plus `summary.json` under
`.run\policy-benchmark`.

HermesAgent-20 requires Docker Desktop with the Linux engine running. If Docker
is down, the script records `blockedBy: "docker"` in the benchmark summary.

## Install Into Windows Hermes

For the normal Windows Hermes install, BusyBeaver is installed as a local skill plus a helper command. It is not configured as the main Hermes chat model.

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\install_windows_hermes.ps1 -StartServer
```

Smoke test the installed helper:

```powershell
C:\Users\basbe\AppData\Local\hermes\bin\busybeaver-policy.ps1 `
  -Goal "Tests failed in tests/test_math.py. Choose the next tool action." `
  -Pretty
```

See [docs/windows-hermes-setup.md](docs/windows-hermes-setup.md).

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

The adapter policy path is enabled when model metadata contains `busybeaver`, `busybee`, `busybee-cpu`, or `policy-adapter` in `id`, `label`, `provider`, `providerModel`, or `exposedModel`.

## V12 Resolved Eval

The current production contract depends on the BusyBeaver harness resolver in the BusyBeaver source checkout. With that resolver enabled, V12 checkpoint 250 scores:

| Eval | JSON | Schema | Correct Tool | Arg Semantic | Unsafe Cmd |
| --- | ---: | ---: | ---: | ---: | ---: |
| `frozen_path_grounding_v2` | 1.0000 | 1.0000 | 1.0000 | 0.9792 | 0.0000 |
| `frozen_harness_v1` | 1.0000 | 1.0000 | 1.0000 | 0.9000 | 0.0000 |

## License

MIT.
