# Windows Hermes Setup

This setup installs BusyBeaver as a local policy helper for the active Windows Hermes home reported by `hermes config path`, usually `C:\Users\basbe\AppData\Local\hermes`.

It does not switch Hermes' main model to BusyBeaver. BusyBeaver stays underneath Hermes/Spark as a strict-JSON tool-policy endpoint.

## Install

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\install_windows_hermes.ps1 -StartServer
```

Installed files:

- `C:\Users\basbe\AppData\Local\hermes\skills\toolforge\busybeaver-policy\SKILL.md`
- `C:\Users\basbe\AppData\Local\hermes\bin\busybeaver-policy.ps1`

Hermes config keys:

- `skills.config.busybeaver.server_url`
- `skills.config.busybeaver.model`
- `skills.config.busybeaver.model_path`
- `skills.config.busybeaver.adapter_repo`
- `skills.config.busybeaver.busybeaver_repo`

## Start Server

```powershell
cd C:\Users\basbe\Desktop\AI_Research\BusyBeaver-Hermes-Adapter
.\scripts\start_busybeaver_server.ps1
```

Default endpoint:

```text
http://127.0.0.1:8767/v1/chat/completions
```

## Smoke Test

```powershell
C:\Users\basbe\AppData\Local\hermes\bin\busybeaver-policy.ps1 `
  -Goal "Tests failed in tests/test_math.py. Choose the next tool action." `
  -StateJson '{"repo_summary":"","current_step":0,"recent_observations":["pytest failed in tests/test_math.py"],"open_files":[],"last_tool":"run_tests","last_error":"AssertionError"}' `
  -Pretty
```

Expected output is one JSON object with `tool`, `args`, `confidence`, and `state_update`.

The local OpenAI wrapper also repairs common BusyBeaver placeholder values such as `<PATH_FROM_STATE>` using `state.policy_features.candidate_paths` before returning the action to Hermes.

## Hermes Use

Inside Hermes/Spark, use the `busybeaver-policy` skill when you want a compact next-action recommendation. The skill tells Hermes to call the local command or endpoint, then validate the returned action before execution.
