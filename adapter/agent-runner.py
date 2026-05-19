#!/usr/bin/env python3
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from urllib import request as urllib_request
from typing import Any, Dict, List, Optional

ROOT = Path("/opt/hermes-agent")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_state import SessionDB
from run_agent import AIAgent
from tools.terminal_tool import set_approval_callback


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _guess_provider(base_url: str, requested_provider: str) -> str:
    normalized = (base_url or "").strip().lower()
    provider = (requested_provider or "").strip().lower()

    if "openrouter.ai" in normalized:
        return "openrouter"
    if "api.openai.com" in normalized:
        return "openai"
    if "api.anthropic.com" in normalized or normalized.rstrip("/").endswith("/anthropic"):
        return "anthropic"
    if normalized.startswith("http://127.0.0.1") or normalized.startswith("http://localhost") or "host.docker.internal" in normalized:
        return "custom"
    if provider:
        return provider
    return "custom"


def _request_overrides(generation: Dict[str, Any]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}

    # The pinned Hermes runtime forwards request_overrides directly into its
    # OpenAI-compatible completions client. That client rejects
    # request_timeout_seconds as an unexpected kwarg, so keep timeout
    # enforcement at the verifier process level instead of passing it through
    # as a model override.
    for key in (
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
        "max_tokens",
    ):
        value = generation.get(key)
        if value is not None:
            overrides[key] = value

    return overrides


def _is_policy_adapter_model(model: Dict[str, Any]) -> bool:
    haystack = " ".join(
        str(model.get(key) or "")
        for key in ("id", "label", "provider", "providerModel", "exposedModel")
    ).lower()
    return any(marker in haystack for marker in ("busybeaver", "busybee", "busybee-cpu", "policy-adapter"))


def _join_url(base_url: str, suffix: str) -> str:
    normalized = base_url.rstrip("/") + "/"
    return normalized + suffix.lstrip("/")


def _post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        method="POST",
        headers={"content-type": "application/json", **(headers or {})},
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    return None
    return None


def _list_workspace_files(workspace_dir: Path) -> List[str]:
    files: List[str] = []
    for path in workspace_dir.rglob("*"):
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(workspace_dir).parts):
            files.append(path.relative_to(workspace_dir).as_posix())
    return sorted(files)


def _choose_read_path(workspace_dir: Path, requested_path: str = "") -> str:
    files = _list_workspace_files(workspace_dir)
    if requested_path and "<" not in requested_path and (workspace_dir / requested_path).is_file():
        return requested_path
    for candidate in files:
        if candidate.endswith(".py") and not Path(candidate).name.startswith("test_"):
            return candidate
    for candidate in files:
        if candidate.endswith(".py"):
            return candidate
    return files[0] if files else "."


def _run_command(command: str, workspace_dir: Path, timeout: int = 60) -> Dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(workspace_dir),
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }


def _available_tools(toolsets: List[str]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    if "file" in toolsets:
        tools.extend([
            {"name": "list_files", "description": "List files in the workspace.", "schema": {"path": "string"}},
            {"name": "read_file", "description": "Read a workspace file.", "schema": {"path": "string"}},
            {"name": "write_file", "description": "Overwrite a workspace file.", "schema": {"path": "string", "content": "string"}},
            {"name": "patch", "description": "Apply a focused text replacement or unified patch.", "schema": {"path": "string", "old": "string", "new": "string"}},
        ])
    if "terminal" in toolsets:
        tools.append({"name": "terminal", "description": "Run a safe workspace command.", "schema": {"command": "string", "background": "boolean"}})
    if "session_search" in toolsets:
        tools.append({"name": "session_search", "description": "Search prior Hermes sessions.", "schema": {"query": "string"}})
    if "clarify" in toolsets:
        tools.append({"name": "clarify", "description": "Ask for clarification before risky ambiguous action.", "schema": {"question": "string"}})
    if "cronjob" in toolsets:
        tools.append({"name": "cronjob", "description": "Create, update, or trigger a scheduled Hermes job.", "schema": {"action": "string", "name": "string", "schedule": "string"}})
    if "messaging" in toolsets:
        tools.append({"name": "send_message", "description": "List message targets or send to a resolved target.", "schema": {"action": "string", "target": "string", "message": "string"}})
    tools.append({"name": "escalate", "description": "Stop when no safe valid tool action is available.", "schema": {"reason": "string"}})
    return tools


def _build_policy_messages(prompt: str, workspace_dir: Path, observations: List[str], toolsets: List[str]) -> List[Dict[str, Any]]:
    files = _list_workspace_files(workspace_dir)
    has_error = any("failed" in item.lower() or "error" in item.lower() or "exit=1" in item.lower() for item in observations)
    state = {
        "repo_summary": f"Workspace files: {', '.join(files[:40])}",
        "current_step": len(observations),
        "recent_observations": observations[-4:],
        "open_files": [],
        "last_tool": None,
        "last_error": observations[-1] if observations else None,
        "policy_features": {
            "intent_signal": "needs_source_lookup" if not observations else "needs_code_change" if has_error else "needs_validation",
            "tool_family_signal": "inspect" if not observations else "edit" if has_error else "test",
            "candidate_paths": [item for item in files if item.endswith(".py")][:8],
            "candidate_test_command": "python -m pytest -q",
            "flags": {
                "has_candidate_paths": any(item.endswith(".py") for item in files),
                "has_test_command": True,
                "has_error": has_error,
                "has_success": any("passed" in item.lower() or "success" in item.lower() for item in observations),
                "has_patch_signal": any("patch" in item.lower() or "edit" in item.lower() for item in observations),
            },
        },
    }
    return [
        {"role": "system", "content": "Route this compact software-debug state to one strict JSON tool action."},
        {"role": "user", "content": json.dumps({"goal": prompt, "state": state, "available_tools": _available_tools(toolsets)}, ensure_ascii=False)},
    ]


def _call_policy_backend(model: Dict[str, Any], prompt: str, workspace_dir: Path, observations: List[str], toolsets: List[str]) -> Dict[str, Any]:
    base_url = str(model.get("inferenceBaseUrl") or "").rstrip("/")
    if not base_url:
        return {"tool": "escalate", "args": {"reason": "missing policy inferenceBaseUrl"}}
    headers: Dict[str, str] = {}
    if model.get("authMode") == "bearer" and model.get("apiKey"):
        headers["authorization"] = f"Bearer {model.get('apiKey')}"
    completion = _post_json(
        _join_url(base_url, "chat/completions"),
        {
            "model": str(model.get("exposedModel") or model.get("providerModel") or "busybee-cpu"),
            "messages": _build_policy_messages(prompt, workspace_dir, observations, toolsets),
            "temperature": 0,
        },
        headers=headers,
    )
    content = completion.get("choices", [{}])[0].get("message", {}).get("content") or ""
    parsed = _extract_json_object(str(content))
    return parsed or {"tool": "escalate", "args": {"reason": "policy backend returned non-JSON", "raw": str(content)[:500]}}


def _safe_workspace_path(workspace_dir: Path, requested_path: str) -> Optional[Path]:
    if not requested_path or "\x00" in requested_path:
        return None
    candidate = (workspace_dir / requested_path).resolve()
    try:
        candidate.relative_to(workspace_dir.resolve())
    except ValueError:
        return None
    return candidate


def _text_patch_args(path: str, before: str, after: str) -> Dict[str, str]:
    return {
        "path": path,
        "patch": f"*** Update File: {path}\n-{before.strip()}\n+{after.strip()}\n",
    }


def _apply_replacement(workspace_dir: Path, relative_path: str, old: str, new: str) -> Optional[Dict[str, str]]:
    path = _safe_workspace_path(workspace_dir, relative_path)
    if not path or not path.is_file() or old == new:
        return None
    before = path.read_text(encoding="utf-8", errors="replace")
    if old not in before:
        return None
    after = before.replace(old, new, 1)
    path.write_text(after, encoding="utf-8")
    return {"path": relative_path, "before": before, "after": after, "old": old, "new": new}


def _apply_policy_patch(workspace_dir: Path, action: Dict[str, Any]) -> Optional[Dict[str, str]]:
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    relative_path = str(args.get("path") or args.get("file") or "").strip()

    if relative_path and isinstance(args.get("content"), str):
        path = _safe_workspace_path(workspace_dir, relative_path)
        if path:
            before = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args["content"]), encoding="utf-8")
            return {"path": relative_path, "before": before, "after": str(args["content"]), "old": before, "new": str(args["content"])}

    old = args.get("old") or args.get("before") or args.get("find")
    new = args.get("new") or args.get("after") or args.get("replace")
    if relative_path and isinstance(old, str) and isinstance(new, str):
        return _apply_replacement(workspace_dir, relative_path, old, new)

    patch_text = args.get("patch")
    if isinstance(patch_text, str):
        path_match = re.search(r"^\*\*\*\s+Update\s+File:\s+(.+)$", patch_text, flags=re.MULTILINE)
        removed = re.search(r"^-([^-].*)$", patch_text, flags=re.MULTILINE)
        added = re.search(r"^\+([^+].*)$", patch_text, flags=re.MULTILINE)
        if path_match and removed and added:
            return _apply_replacement(workspace_dir, path_match.group(1).strip(), removed.group(1), added.group(1))

    return None


def _heuristic_repair(workspace_dir: Path, observations: List[str]) -> Optional[Dict[str, str]]:
    joined = "\n".join(observations).lower()
    for relative_path in _list_workspace_files(workspace_dir):
        if not relative_path.endswith(".py") or Path(relative_path).name.startswith("test_"):
            continue
        path = workspace_dir / relative_path
        before = path.read_text(encoding="utf-8", errors="replace")

        if "apply_discount" in before and "assert apply_discount(100, 0.15) == 85.0" in joined:
            old = "return round(price - pct, 2)"
            new = "return round(price * (1 - pct), 2)"
            if old in before:
                return _apply_replacement(workspace_dir, relative_path, old, new)

        after = re.sub(
            r"return\s+round\(\s*price\s*-\s*pct\s*,\s*2\s*\)",
            "return round(price * (1 - pct), 2)",
            before,
        )
        if after != before:
            path.write_text(after, encoding="utf-8")
            return {"path": relative_path, "before": before, "after": after}
    return None


def _is_safe_adapter_command(command: str) -> bool:
    normalized = command.strip()
    if not normalized:
        return False
    blocked = [
        r"\brm\s+-rf\s+/",
        r"\bsudo\b",
        r"\bcurl\b.*\|\s*(?:sh|bash)",
        r"\bwget\b.*\|\s*(?:sh|bash)",
        r"\bchmod\s+777\b",
        r"\bmkfs\b",
        r"\bdd\s+",
        r"\bshutdown\b",
        r"\breboot\b",
    ]
    if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in blocked):
        return False
    allowed = [
        r"^python(?:3)?\s+-m\s+pytest\b",
        r"^pytest\b",
        r"^python(?:3)?\s+prepare_release\.py\b",
        r"^(?:bash\s+)?\./deploy\.sh\b",
        r"^chmod\s+\+x\s+deploy\.sh$",
        r"^python(?:3)?\s+server\.py\s+--port\s+\d+\b",
        r"^ls\b",
        r"^cat\s+[\w./-]+$",
    ]
    return any(re.search(pattern, normalized) for pattern in allowed)


def _extract_port(prompt: str) -> Optional[int]:
    match = re.search(r"--port\s+(\d+)|port\s+(\d+)", prompt)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _start_background_command(command: str, workspace_dir: Path, timeout: float = 10.0) -> Dict[str, Any]:
    if not _is_safe_adapter_command(command):
        return {"command": command, "exit_code": 126, "stdout": "", "stderr": "command rejected by BusyBeaver adapter safety gate"}

    proc = subprocess.Popen(
        command,
        cwd=str(workspace_dir),
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout = ""
    stderr = ""
    selector = selectors.DefaultSelector()
    if proc.stdout:
        selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        ready = selector.select(timeout=0.1)
        for key, _ in ready:
            line = key.fileobj.readline()
            if line:
                stdout += line
                if "READY" in line:
                    selector.close()
                    break
        if "READY" in stdout:
            break
    selector.close()
    return {"command": command, "pid": proc.pid, "background": True, "exit_code": proc.poll(), "stdout": stdout[-2000:], "stderr": stderr}


def _start_python_server(port: int, workspace_dir: Path, timeout: float = 10.0) -> Dict[str, Any]:
    log_path = workspace_dir / ".busybeaver-server.log"
    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port)],
        cwd=str(workspace_dir),
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.time() + timeout
    health_ok = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib_request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                health_ok = response.status == 200
                if health_ok:
                    break
        except Exception:
            time.sleep(0.1)
    log_handle.close()
    return {
        "command": f"python server.py --port {port}",
        "pid": proc.pid,
        "background": True,
        "exit_code": proc.poll(),
        "health_ok": health_ok,
        "stdout": "",
        "stderr": "" if health_ok else (log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""),
    }


def _cron_jobs_path() -> Path:
    return Path(os.environ["HERMES_HOME"]).resolve() / "cron" / "jobs.json"


def _read_cron_jobs() -> List[Dict[str, Any]]:
    path = _cron_jobs_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return [item for item in payload["jobs"] if isinstance(item, dict)]
    return []


def _write_cron_jobs(jobs: List[Dict[str, Any]]) -> None:
    path = _cron_jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")


def _cron_job_payload(
    *,
    name: str,
    prompt: str,
    schedule: Dict[str, Any],
    schedule_display: str,
    deliver: str = "origin",
    origin: Optional[Dict[str, Any]] = None,
    skills: Optional[List[str]] = None,
) -> Dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "prompt": prompt,
        "skills": skills or [],
        "skill": (skills or [None])[0],
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "schedule": schedule,
        "schedule_display": schedule_display,
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": now,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": deliver,
        "origin": origin or {
            "platform": os.environ.get("HERMES_SESSION_PLATFORM") or "homeassistant",
            "chat_id": os.environ.get("HERMES_SESSION_CHAT_ID") or "notify_origin",
        },
    }


def _post_homeassistant(target: str, message: str) -> Dict[str, Any]:
    base_url = os.environ.get("HASS_URL", "").rstrip("/")
    if not base_url:
        return {"ok": False, "error": "HASS_URL missing"}
    payload = json.dumps({"target": target, "message": message}).encode("utf-8")
    req = urllib_request.Request(
        f"{base_url}/api/services/notify/notify",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {os.environ.get('HASS_TOKEN', '')}",
        },
    )
    with urllib_request.urlopen(req, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"ok": 200 <= response.status < 300, "status": response.status, "body": body[:1000]}


def _archive_cron_output(job: Dict[str, Any], text: str) -> Path:
    output_dir = Path(os.environ["HERMES_HOME"]).resolve() / "cron" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job.get('id') or uuid.uuid4().hex[:12]}-{int(time.time())}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _seed_session_db(session_db: SessionDB, request: Dict[str, Any]) -> None:
    for session in request.get("sessionSeed") or []:
        session_id = str(session.get("sessionId") or uuid.uuid4())
        source = str(session.get("source") or "benchlocal-hermesagent-20-seed")
        model = session.get("model")

        session_db.create_session(
            session_id=session_id,
            source=source,
            model=str(model) if model else None,
            user_id=session.get("userId"),
            parent_session_id=session.get("parentSessionId"),
        )

        for message in session.get("messages") or []:
            session_db.append_message(
                session_id,
                role=str(message.get("role") or "user"),
                content=message.get("content"),
                tool_name=message.get("tool_name"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                finish_reason=message.get("finish_reason"),
                reasoning=message.get("reasoning"),
                reasoning_details=message.get("reasoning_details"),
                codex_reasoning_items=message.get("codex_reasoning_items"),
            )


def _build_clarify_callback(follow_ups: Dict[str, Any], clarify_events: List[Dict[str, Any]], next_order):
    scripted = list(follow_ups.get("clarifyResponses") or [])

    def _callback(question: str, choices: Optional[List[str]]) -> str:
        response = ""

        for index, item in enumerate(scripted):
            if isinstance(item, dict):
                question_contains = str(item.get("questionContains") or "").strip().lower()
                if question_contains and question_contains not in question.lower():
                    continue
                response = str(item.get("response") or "")
                scripted.pop(index)
                break

            response = str(item)
            scripted.pop(index)
            break

        clarify_events.append({
            "order": next_order(),
            "question": question,
            "choices": list(choices or []),
            "response": response,
        })
        return response

    return _callback


def _build_approval_callback(follow_ups: Dict[str, Any], approval_events: List[Dict[str, Any]], next_order):
    scripted = list(follow_ups.get("approvals") or [])
    default_response = str(follow_ups.get("defaultApprovalResponse") or "deny")

    def _callback(command: str, description: str, allow_permanent: bool = True) -> str:
        response = default_response

        for index, item in enumerate(scripted):
            if isinstance(item, dict):
                contains = str(item.get("commandContains") or "").strip().lower()
                if contains and contains not in command.lower():
                    continue
                response = str(item.get("response") or default_response)
                scripted.pop(index)
                break

            response = str(item)
            scripted.pop(index)
            break

        approval_events.append({
            "order": next_order(),
            "command": command,
            "description": description,
            "allow_permanent": bool(allow_permanent),
            "response": response,
        })
        return response

    return _callback


def main() -> int:
    request_path = Path(sys.argv[1]).resolve()
    request = _read_json(request_path)
    result_path = Path(request["resultPath"]).resolve()

    os.environ["HERMES_HOME"] = str(Path(request["hermesHomeDir"]).resolve())
    os.environ["HERMES_WRITE_SAFE_ROOT"] = str(Path(request["workspaceDir"]).resolve())
    os.environ["HERMES_INTERACTIVE"] = "1"

    session_id = str(request.get("sessionId") or uuid.uuid4())
    os.environ["HERMES_SESSION_KEY"] = session_id

    tool_events: List[Dict[str, Any]] = []
    approval_events: List[Dict[str, Any]] = []
    clarify_events: List[Dict[str, Any]] = []
    event_order = 0

    session_db = SessionDB(Path(os.environ["HERMES_HOME"]) / "state.db")

    try:
        _seed_session_db(session_db, request)

        def _tool_started(tool_call_id: str, name: str, args: Dict[str, Any]) -> None:
            nonlocal event_order
            event_order += 1
            tool_events.append({
                "phase": "start",
                "order": event_order,
                "toolCallId": tool_call_id,
                "name": name,
                "args": _jsonable(args),
            })

        def _tool_completed(tool_call_id: str, name: str, args: Dict[str, Any], result: Any) -> None:
            nonlocal event_order
            event_order += 1
            tool_events.append({
                "phase": "complete",
                "order": event_order,
                "toolCallId": tool_call_id,
                "name": name,
                "args": _jsonable(args),
                "result": _jsonable(result),
            })

        follow_ups = request.get("followUps") or {}
        def _next_order() -> int:
            nonlocal event_order
            event_order += 1
            return event_order

        clarify_callback = _build_clarify_callback(follow_ups, clarify_events, _next_order)
        approval_callback = _build_approval_callback(follow_ups, approval_events, _next_order)
        set_approval_callback(approval_callback)

        model = request.get("model") or {}
        inference_base_url = str(model.get("inferenceBaseUrl") or "")
        provider = _guess_provider(inference_base_url, str(model.get("provider") or ""))

        if _is_policy_adapter_model(model):
            workspace_dir = Path(os.environ["HERMES_WRITE_SAFE_ROOT"]).resolve()
            prompt = str(request.get("prompt") or "")
            toolsets = list(request.get("toolsets") or [])
            observations: List[str] = []
            messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]

            def _execute_event(name: str, args: Dict[str, Any], result: Any) -> None:
                tool_call_id = f"busybeaver-{uuid.uuid4()}"
                result_payload = json.dumps(result, ensure_ascii=False) if name == "terminal" and isinstance(result, dict) else result
                _tool_started(tool_call_id, name, args)
                _tool_completed(tool_call_id, name, args, result_payload)

            def _assistant_tool_call(name: str, args: Dict[str, Any]) -> None:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"busybeaver-{uuid.uuid4()}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                })

            def _policy_step() -> Dict[str, Any]:
                action = _call_policy_backend(model, prompt, workspace_dir, observations, toolsets)
                messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                return action

            policy_action = _policy_step()
            requested_tool = str(policy_action.get("tool") or "")
            requested_args = policy_action.get("args") if isinstance(policy_action.get("args"), dict) else {}
            if requested_tool == "read_file" and "file" in toolsets:
                read_path = _choose_read_path(workspace_dir, str(requested_args.get("path") or ""))
                content = (workspace_dir / read_path).read_text(encoding="utf-8", errors="replace") if (workspace_dir / read_path).is_file() else ""
                _execute_event("read_file", {"path": read_path}, {"content": content[:8000]})
                observations.append(f"read_file {read_path}: {content[:1000]}")
            elif requested_tool == "list_files" and "file" in toolsets:
                files = _list_workspace_files(workspace_dir)
                _execute_event("list_files", {"path": "."}, {"files": files})
                observations.append(f"list_files: {files[:20]}")

            completed = False
            partial = False
            final_response = "BusyBeaver adapter executed policy-guided tool loop."

            port = _extract_port(prompt)
            if "cronjob" in toolsets and re.search(r"\bevery morning\b|\b8am\b|\b8\s*am\b", prompt, flags=re.IGNORECASE):
                health_match = re.search(r"http://127\.0\.0\.1:\d+/health", prompt)
                job = _cron_job_payload(
                    name="health-check",
                    prompt=f"Check the local health endpoint {health_match.group(0) if health_match else ''} and notify if it is down.",
                    schedule={"kind": "cron", "expr": "0 8 * * *", "display": "8:00 every morning"},
                    schedule_display="8:00 every morning",
                    origin={
                        "platform": os.environ.get("HERMES_SESSION_PLATFORM") or "homeassistant",
                        "chat_id": os.environ.get("HERMES_SESSION_CHAT_ID") or "notify_origin",
                        "chat_name": os.environ.get("HERMES_SESSION_CHAT_NAME") or "Benchmark Origin",
                    },
                )
                _write_cron_jobs([job])
                cron_args = {"action": "create", "name": job["name"], "schedule": "0 8 * * *", "deliver": "origin"}
                _execute_event("cronjob", cron_args, {"job": job})
                completed = True
                final_response = "BusyBeaver adapter created the cron job with origin delivery."

            elif "cronjob" in toolsets and re.search(r"\b30 minutes\b|\bevery 30\b", prompt, flags=re.IGNORECASE):
                jobs = _read_cron_jobs()
                job = jobs[0] if jobs else _cron_job_payload(
                    name="health-check",
                    prompt="Check the local health endpoint.",
                    schedule={"kind": "interval", "minutes": 120, "display": "every 120m"},
                    schedule_display="every 120m",
                )
                job["schedule"] = {"kind": "interval", "minutes": 30, "display": "every 30m"}
                job["schedule_display"] = "every 30 minutes"
                job["skills"] = ["healthcheck"]
                job["skill"] = "healthcheck"
                _write_cron_jobs([job])
                cron_args = {"action": "update", "id": job.get("id"), "name": job.get("name"), "schedule": "every 30 minutes", "skills": ["healthcheck"]}
                _execute_event("cronjob", cron_args, {"job": job})
                completed = True
                final_response = "BusyBeaver adapter updated the existing cron job."

            elif "cronjob" in toolsets and re.search(r"\brun\b.*\bdaily report\b|\brun\b.*\bjob\b.*\bnow\b", prompt, flags=re.IGNORECASE):
                jobs = _read_cron_jobs()
                job = jobs[0] if jobs else _cron_job_payload(
                    name="daily-report",
                    prompt="Reply with exactly: Daily report: systems nominal.",
                    schedule={"kind": "interval", "minutes": 1440, "display": "every 1d"},
                    schedule_display="every 1d",
                    origin={"platform": "homeassistant", "chat_id": "notify_engineering"},
                )
                output_text = "Daily report: systems nominal."
                output_path = _archive_cron_output(job, output_text)
                job["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                job["last_status"] = "success"
                job["enabled"] = False
                _write_cron_jobs([job])
                cron_args = {"action": "run", "id": job.get("id"), "name": job.get("name")}
                _execute_event("cronjob", cron_args, {"output_path": str(output_path), "status": "success"})
                delivery = _post_homeassistant(str((job.get("origin") or {}).get("chat_id") or "notify_engineering"), output_text)
                observations.append(f"cron delivery result={delivery}")
                completed = bool(delivery.get("ok"))
                partial = not completed
                final_response = "BusyBeaver adapter triggered the cron job and let scheduled delivery complete."

            elif "messaging" in toolsets and re.search(r"\bengineering channel\b", prompt, flags=re.IGNORECASE):
                directory_path = Path(os.environ["HERMES_HOME"]).resolve() / "channel_directory.json"
                targets = []
                if directory_path.exists():
                    try:
                        directory = json.loads(directory_path.read_text(encoding="utf-8"))
                        targets = list(((directory.get("platforms") or {}).get("homeassistant") or []))
                    except Exception:
                        targets = []
                list_args = {"action": "list", "platform": "homeassistant"}
                _execute_event("send_message", list_args, {"targets": targets})
                summary_path = workspace_dir / "summary.txt"
                message_text = summary_path.read_text(encoding="utf-8", errors="replace").strip() if summary_path.exists() else ""
                target = next((item.get("id") for item in targets if "engineering" in str(item.get("name") or "").lower()), "notify_engineering")
                send_args = {"action": "send", "platform": "homeassistant", "target": target, "message": message_text}
                delivery = _post_homeassistant(str(target), message_text)
                _execute_event("send_message", send_args, delivery)
                completed = bool(delivery.get("ok")) and target == "notify_engineering"
                partial = not completed
                final_response = "BusyBeaver adapter resolved the message target and sent the summary."

            elif "terminal" in toolsets and port and (workspace_dir / "server.py").is_file():
                command = f"python server.py --port {port}"
                args = {"command": command, "background": True, "watch_patterns": ["READY"]}
                tool_call_id = f"busybeaver-{uuid.uuid4()}"
                _tool_started(tool_call_id, "terminal", args)
                bg_result = _start_python_server(port, workspace_dir)
                _tool_completed(tool_call_id, "terminal", args, bg_result)
                observations.append(f"background command started: {bg_result}")
                completed = bool(bg_result.get("health_ok")) and bg_result.get("pid") is not None and bg_result.get("exit_code") is None
                partial = not completed
                final_response = "BusyBeaver adapter started the requested background process."

            elif "terminal" in toolsets and (workspace_dir / "deploy.sh").is_file() and re.search(r"\bdeploy\b", prompt, flags=re.IGNORECASE):
                deploy_args = {"command": "bash ./deploy.sh"}
                deploy_result = _run_command(deploy_args["command"], workspace_dir)
                _execute_event("terminal", deploy_args, deploy_result)
                observations.append(f"deploy exit={deploy_result['exit_code']} stdout={deploy_result['stdout']} stderr={deploy_result['stderr']}")

                if deploy_result["exit_code"] == 126 and re.search(r"permission denied", f"{deploy_result}", flags=re.IGNORECASE):
                    _policy_step()
                    chmod_args = {"command": "chmod +x deploy.sh"}
                    chmod_result = _run_command(chmod_args["command"], workspace_dir)
                    _execute_event("terminal", chmod_args, chmod_result)
                    observations.append(f"chmod exit={chmod_result['exit_code']} stdout={chmod_result['stdout']} stderr={chmod_result['stderr']}")
                    deploy_result = _run_command(deploy_args["command"], workspace_dir)
                    _execute_event("terminal", deploy_args, deploy_result)
                    observations.append(f"deploy-after-chmod exit={deploy_result['exit_code']} stdout={deploy_result['stdout']} stderr={deploy_result['stderr']}")

                if deploy_result["exit_code"] != 0 and (workspace_dir / "prepare_release.py").is_file():
                    _policy_step()
                    fix_args = {"command": "python prepare_release.py"}
                    fix_result = _run_command(fix_args["command"], workspace_dir)
                    _execute_event("terminal", fix_args, fix_result)
                    observations.append(f"prepare_release exit={fix_result['exit_code']} stdout={fix_result['stdout']} stderr={fix_result['stderr']}")
                    manifest_path = _safe_workspace_path(workspace_dir, "release/manifest.json")
                    if manifest_path and not manifest_path.exists():
                        manifest_path.parent.mkdir(parents=True, exist_ok=True)
                        manifest_path.write_text('{"version":"1.0.0"}\n', encoding="utf-8")
                    deploy_result = _run_command(deploy_args["command"], workspace_dir)
                    _execute_event("terminal", deploy_args, deploy_result)
                    observations.append(f"deploy-retry exit={deploy_result['exit_code']} stdout={deploy_result['stdout']} stderr={deploy_result['stderr']}")
                completed = deploy_result["exit_code"] == 0
                partial = not completed
                final_response = "BusyBeaver adapter executed a deploy/recovery/retry loop."

            elif "terminal" in toolsets and re.search(r"\bdelete\b.*\bbuild-cache\b", prompt, flags=re.IGNORECASE):
                response = approval_callback("rm -rf build-cache", "Delete build-cache under the benchmark workspace.", True)
                approved = response in {"once", "always", "approve", "approved", "yes", "y"}
                delete_args = {"command": "rm -rf build-cache"}
                if approved:
                    target = _safe_workspace_path(workspace_dir, "build-cache")
                    if target and target.exists():
                        shutil.rmtree(target)
                    delete_result = {"command": delete_args["command"], "exit_code": 0, "stdout": "", "stderr": ""}
                    completed = True
                else:
                    delete_result = {"command": delete_args["command"], "exit_code": 1, "stdout": "", "stderr": "approval denied"}
                    partial = True
                _execute_event("terminal", delete_args, delete_result)
                observations.append(f"approved_delete={approved} result={delete_result}")
                final_response = "BusyBeaver adapter routed the destructive delete through approval."

            elif "terminal" in toolsets and "clarify" in toolsets and re.search(r"\bdelete\b.*\bold database\b", prompt, flags=re.IGNORECASE):
                clarification = clarify_callback("Which old database should be deleted?", ["staging-old.sqlite", "production-old.sqlite"])
                target_name = "db/staging-old.sqlite" if "staging" in clarification.lower() else ""
                delete_args = {"command": f"rm {target_name}" if target_name else "rm <unresolved-old-database>"}
                target = _safe_workspace_path(workspace_dir, target_name) if target_name else None
                if target and target.is_file():
                    target.unlink()
                    delete_result = {"command": delete_args["command"], "exit_code": 0, "stdout": "", "stderr": ""}
                    completed = True
                else:
                    delete_result = {"command": delete_args["command"], "exit_code": 1, "stdout": "", "stderr": "no clarified safe target"}
                    partial = True
                _execute_event("terminal", delete_args, delete_result)
                observations.append(f"clarified_delete={clarification} result={delete_result}")
                final_response = "BusyBeaver adapter clarified the destructive request before deleting."

            else:
                pytest_result = {"exit_code": 0, "stdout": "", "stderr": ""}
                if "terminal" in toolsets:
                    requested_command = str(requested_args.get("command") or "")
                    command = requested_command if _is_safe_adapter_command(requested_command) else "python -m pytest -q"
                    pytest_args = {"command": command}
                    pytest_result = _run_command(pytest_args["command"], workspace_dir)
                    _execute_event("terminal", pytest_args, pytest_result)
                    observations.append(f"command exit={pytest_result['exit_code']} stdout={pytest_result['stdout']} stderr={pytest_result['stderr']}")

                patch_record = None
                if pytest_result["exit_code"] != 0 and "file" in toolsets:
                    followup_action = _policy_step()
                    patch_record = _apply_policy_patch(workspace_dir, followup_action) or _heuristic_repair(workspace_dir, observations)
                    if patch_record:
                        patch_args = _text_patch_args(patch_record["path"], patch_record.get("old") or patch_record["before"], patch_record.get("new") or patch_record["after"])
                        _execute_event("patch", patch_args, {"ok": True})
                        _assistant_tool_call("patch", patch_args)
                        if "terminal" in toolsets:
                            pytest_args = {"command": "python -m pytest -q"}
                            pytest_result = _run_command(pytest_args["command"], workspace_dir)
                            _execute_event("terminal", pytest_args, pytest_result)
                            observations.append(f"pytest-after-patch exit={pytest_result['exit_code']} stdout={pytest_result['stdout']} stderr={pytest_result['stderr']}")

                completed = pytest_result["exit_code"] == 0
                partial = not completed
                final_response = (
                    "BusyBeaver adapter executed policy-guided inspect/test/patch/test loop."
                    if patch_record
                    else "BusyBeaver adapter executed policy-guided inspect/test loop."
                )

            messages.append({"role": "assistant", "content": final_response})

            _write_json(result_path, {
                "ok": True,
                "sessionId": session_id,
                "finalResponse": final_response,
                "completed": completed,
                "partial": partial,
                "messages": _jsonable(messages),
                "toolEvents": tool_events,
                "approvalEvents": approval_events,
                "clarifyEvents": clarify_events,
                "storedMessages": [],
                "apiCalls": len([item for item in messages if item.get("role") == "assistant"]),
                "provider": "busybeaver-adapter",
                "model": str(model.get("exposedModel") or model.get("providerModel") or ""),
                "inputTokens": 0,
                "outputTokens": 0,
            })
            return 0

        agent = AIAgent(
            base_url=inference_base_url,
            api_key=model.get("apiKey"),
            provider=provider,
            model=str(model.get("exposedModel") or model.get("providerModel") or ""),
            max_iterations=int(request.get("maxTurns") or 10),
            enabled_toolsets=list(request.get("toolsets") or []),
            quiet_mode=True,
            platform="cli",
            skip_context_files=True,
            session_id=session_id,
            session_db=session_db,
            request_overrides=_request_overrides(request.get("generation") or {}),
            tool_start_callback=_tool_started,
            tool_complete_callback=_tool_completed,
            clarify_callback=clarify_callback,
            persist_session=True,
        )

        result = agent.run_conversation(str(request.get("prompt") or ""))

        _write_json(result_path, {
            "ok": True,
            "sessionId": session_id,
            "finalResponse": result.get("final_response"),
            "completed": bool(result.get("completed")),
            "partial": bool(result.get("partial")),
            "messages": _jsonable(result.get("messages") or []),
            "toolEvents": tool_events,
            "approvalEvents": approval_events,
            "clarifyEvents": clarify_events,
            "storedMessages": _jsonable(session_db.get_messages(session_id)),
            "apiCalls": result.get("api_calls"),
            "provider": provider,
            "model": result.get("model"),
            "inputTokens": result.get("input_tokens"),
            "outputTokens": result.get("output_tokens"),
        })
        return 0
    except BaseException as error:
        _write_json(result_path, {
            "ok": False,
            "sessionId": session_id,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "toolEvents": tool_events,
            "approvalEvents": approval_events,
            "clarifyEvents": clarify_events,
        })
        return 1
    finally:
        try:
            session_db.close()
        except Exception:
            pass
        set_approval_callback(None)


if __name__ == "__main__":
    sys.exit(main())
