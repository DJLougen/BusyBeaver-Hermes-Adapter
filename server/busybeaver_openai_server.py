from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--busybeaver-repo", default=os.environ.get("BUSYBEAVER_REPO", ""))
_known, _ = _bootstrap.parse_known_args()

_root = Path(__file__).resolve().parents[1]
_candidates = [
    Path(_known.busybeaver_repo) if _known.busybeaver_repo else None,
    Path(os.environ["BUSYBEAVER_REPO"]) if os.environ.get("BUSYBEAVER_REPO") else None,
    _root,
    _root.parent / "busybeaver",
    Path.cwd(),
]
for _candidate in _candidates:
    if _candidate and (_candidate / "busybeaver").exists():
        sys.path.insert(0, str(_candidate))
        break

from busybeaver.harness.policy_model import DEFAULT_REPO_ID, BusyBeaverPolicy
from busybeaver.harness.validator import strict_json, validate_tool_call
from busybeaver.schemas import default_state


PLACEHOLDERS = {
    "<PATH_FROM_STATE>",
    "<FILE_FROM_STATE>",
    "<COMMAND_FROM_STATE>",
    "<TEST_COMMAND_FROM_STATE>",
}


def messages_to_goal(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = str(message.get("content") or "")
            try:
                payload = json.loads(content)
                if isinstance(payload, dict) and isinstance(payload.get("goal"), str):
                    return payload["goal"]
            except Exception:
                pass
            return content
    return ""


def messages_to_state(messages: list[dict[str, Any]]) -> dict[str, Any]:
    state = default_state()
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("state"), dict):
            return payload["state"]

    observations = []
    for message in messages[-6:]:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role in {"assistant", "tool", "system"} and content:
            observations.append(f"{role}: {content[:1200]}")
    state["recent_observations"] = observations[-4:]
    state["policy_features"] = {
        "intent_signal": "needs_source_lookup",
        "tool_family_signal": "inspect",
        "candidate_paths": [],
        "candidate_test_command": "",
        "flags": {
            "has_candidate_paths": False,
            "has_test_command": False,
            "has_error": any("error" in item.lower() or "failed" in item.lower() for item in observations),
            "has_success": any("pass" in item.lower() or "success" in item.lower() for item in observations),
            "has_patch_signal": any("patch" in item.lower() or "edit" in item.lower() for item in observations),
        },
    }
    return state


def messages_to_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except Exception:
            continue
        tools = payload.get("available_tools") if isinstance(payload, dict) else None
        if isinstance(tools, list):
            return [tool for tool in tools if isinstance(tool, dict)]
    return None


def _policy_features(state: dict[str, Any]) -> dict[str, Any]:
    features = state.get("policy_features")
    return features if isinstance(features, dict) else {}


def _first_candidate_path(state: dict[str, Any]) -> str | None:
    paths = _policy_features(state).get("candidate_paths")
    if isinstance(paths, list):
        for path in paths:
            if isinstance(path, str) and path.strip():
                return path.strip()
    observations = state.get("recent_observations") or []
    for observation in observations:
        if not isinstance(observation, str):
            continue
        for token in observation.replace("\\", "/").split():
            if "/" in token and "." in token:
                return token.strip(".,:;()[]{}'\"")
    return None


def _candidate_test_command(state: dict[str, Any]) -> str | None:
    command = _policy_features(state).get("candidate_test_command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    observations = state.get("recent_observations") or []
    for observation in observations:
        if isinstance(observation, str) and "pytest" in observation:
            return "python -m pytest -q"
    return None


def repair_action_from_state(action: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    repaired = json.loads(json.dumps(action))
    args = repaired.get("args")
    if not isinstance(args, dict):
        return repaired

    for key, value in list(args.items()):
        if not isinstance(value, str) or value not in PLACEHOLDERS:
            continue
        if "path" in key.lower() or "file" in key.lower():
            replacement = _first_candidate_path(state)
        elif "command" in key.lower() or "cmd" in key.lower():
            replacement = _candidate_test_command(state)
        else:
            replacement = _first_candidate_path(state) or _candidate_test_command(state)
        if replacement:
            args[key] = replacement
    return repaired


class BusyBeaverOpenAIServer(ThreadingHTTPServer):
    policy: BusyBeaverPolicy
    exposed_model: str


class Handler(BaseHTTPRequestHandler):
    server: BusyBeaverOpenAIServer

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "model": self.server.exposed_model})
            return
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": self.server.exposed_model, "object": "model", "created": 0, "owned_by": "busybeaver"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        messages = body.get("messages") or []
        if not isinstance(messages, list):
            self._json(400, {"error": {"message": "messages must be a list"}})
            return
        goal = messages_to_goal(messages)
        state = messages_to_state(messages)
        tools = messages_to_tools(messages)
        result = self.server.policy.predict(goal=goal, state=state, available_tools=tools)
        if result["ok"]:
            action = repair_action_from_state(result["action"], state)
            available_tools = tools or self.server.policy.registry.list()
            valid, obj, reasons = validate_tool_call(action, available_tools)
            output = strict_json(obj if valid and obj else action)
            if not valid:
                output = strict_json({"tool": "escalate", "args": {"reason": "; ".join(reasons)}, "confidence": 0.0, "state_update": "BusyBeaver output failed validation after state repair."})
        else:
            output = strict_json({"tool": "escalate", "args": {"reason": "; ".join(result["errors"])}, "confidence": 0.0, "state_update": "BusyBeaver output failed validation."})
        self._json(
            200,
            {
                "id": f"chatcmpl-busybeaver-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self.server.exposed_model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": output}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve BusyBeaver-50M through a minimal OpenAI-compatible chat endpoint.")
    parser.add_argument("--busybeaver-repo", default=os.environ.get("BUSYBEAVER_REPO", ""), help="Path to the BusyBeaver source checkout.")
    parser.add_argument("--model", default=DEFAULT_REPO_ID)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--exposed-model", default="BusyBeaver-50M")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    server = BusyBeaverOpenAIServer((args.host, args.port), Handler)
    server.policy = BusyBeaverPolicy(args.model, device=args.device, token=os.environ.get("HF_TOKEN"))
    server.exposed_model = args.exposed_model
    print(f"BusyBeaver OpenAI-compatible server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
