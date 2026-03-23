from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class StubAgentHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "provider_configured": False}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        payload = json.loads(raw_body.decode("utf-8"))
        user_message = payload.get("user_message") or "stub request"
        turn_id = str(payload.get("turn_id") or "stub-turn")
        response = {
            "type": "final_reply",
            "final_reply": f"stub-ok: {user_message}",
            "continuation_id": None,
            "action_request_batch": None,
            "pending_confirmation_id": None,
            "pending_confirmation_effect_summary": None,
            "trace_events": [
                {
                    "status_label": "已完成",
                    "status_tone": "success",
                    "title": "Stub agent",
                    "detail": f"Handled message: {user_message}",
                    "secondary": [],
                }
            ],
        }
        write_stub_bundle(payload, response, turn_id)
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Stub Mina agent service for headless smoke tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), StubAgentHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()

def write_stub_bundle(request_payload: dict[str, object], response_payload: dict[str, object], turn_id: str) -> None:
    stamp = datetime.now(timezone.utc)
    data_dir = Path(os.getenv("MINA_AGENT_DATA_DIR", "agent_service/data"))
    debug_dir = data_dir / "debug"
    turn_dir = debug_dir / "turns" / f"{stamp:%Y-%m-%d}" / f"{stamp:%H%M%S_%f}__stub__{turn_id}"
    turn_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "version": 1,
        "turn": {
            "turn_id": turn_id,
            "session_ref": request_payload.get("session_ref"),
            "started_at": stamp.isoformat(),
            "ended_at": stamp.isoformat(),
            "status": "completed",
            "debug_dir": str(turn_dir),
            "resume_events": [],
        },
        "user_input": {
            "user_message": request_payload.get("user_message"),
            "player": request_payload.get("player"),
            "server_env": request_payload.get("server_env"),
            "limits": request_payload.get("limits"),
            "pending_confirmation": request_payload.get("pending_confirmation"),
        },
        "capabilities": {"total": 0, "ids": [], "by_kind": {}, "by_risk_class": {}, "by_handler_kind": {}},
        "context_builds": [],
        "timeline": [],
        "prompt_artifacts": [],
        "final_reply_preview": response_payload.get("final_reply"),
        "truncation": {
            "strings_truncated": 0,
            "chars_omitted": 0,
            "list_items_omitted": 0,
            "dict_keys_omitted": 0,
            "oversize_payloads": 0,
        },
    }
    events = [
        {
            "ts": stamp.isoformat(),
            "turn_id": turn_id,
            "event_type": "turn_started",
            "step_index": None,
            "payload": request_payload,
        },
        {
            "ts": stamp.isoformat(),
            "turn_id": turn_id,
            "event_type": "turn_completed",
            "step_index": 1,
            "payload": {
                "final_reply": response_payload.get("final_reply"),
                "task_id": None,
                "pending_confirmation_id": None,
                "pending_confirmation_effect_summary": None,
            },
        },
    ]
    request_artifact = {
        "turn_id": turn_id,
        "session_ref": request_payload.get("session_ref"),
        "user_message": request_payload.get("user_message"),
        "player": request_payload.get("player"),
        "server_env": request_payload.get("server_env"),
        "limits": request_payload.get("limits"),
        "pending_confirmation": request_payload.get("pending_confirmation"),
        "task": None,
    }
    response_artifact = {
        "turn_id": turn_id,
        "type": "final_reply",
        "status": "completed",
        "final_reply": response_payload.get("final_reply"),
        "pending_confirmation_id": None,
        "pending_confirmation_effect_summary": None,
        "task_id": None,
        "reason": None,
        "error": None,
    }
    player_name = ((request_payload.get("player") or {}).get("name") if isinstance(request_payload.get("player"), dict) else None) or "Steve"
    scenario_capture = {
        "version": 1,
        "turn": {
            "turn_id": turn_id,
            "session_ref": request_payload.get("session_ref"),
            "started_at": stamp.isoformat(),
            "ended_at": stamp.isoformat(),
            "status": "completed",
            "debug_dir": str(turn_dir),
        },
        "scenario": {
            "suite": "functional",
            "scenario_id": turn_id,
            "world_template": None,
            "status": "runnable_now",
            "expectation": "required",
            "feature_flags": {
                "enable_experimental": False,
                "enable_dynamic_scripting": False,
            },
            "actors": [
                {
                    "actor_id": "player",
                    "name": player_name,
                    "role": "read_only",
                    "operator": False,
                    "experimental": False,
                    "spawn_commands": [],
                }
            ],
            "turns": [
                {
                    "actor_id": "player",
                    "message": request_payload.get("user_message"),
                    "setup_commands_before": [],
                }
            ],
            "quality_review": {
                "enabled": False,
                "judge": "codex",
                "rubric_id": None,
            },
            "setup_commands": [],
            "assertions": {
                "expected_final_status": "completed",
                "forbidden_statuses": [],
                "required_capability_ids": [],
                "forbidden_capability_ids": [],
                "confirmation_expected": False,
                "required_reply_substrings": [],
                "forbidden_reply_substrings": [],
                "max_duration_ms": None,
            },
        },
        "request_snapshot": {
            "player": request_payload.get("player"),
            "server_env": request_payload.get("server_env"),
            "limits": request_payload.get("limits"),
            "pending_confirmation": request_payload.get("pending_confirmation"),
        },
        "selected_capability_ids": [],
        "assertion_slots": {
            "observed_capability_ids": [],
            "observed_reply_preview": response_payload.get("final_reply"),
            "observed_confirmation_expected": False,
            "suggested_assertions": {
                "expected_final_status": "completed",
                "forbidden_statuses": ["failed"],
                "required_capability_ids": [],
                "forbidden_capability_ids": [],
                "confirmation_expected": False,
                "required_reply_substrings": [],
                "forbidden_reply_substrings": [],
                "max_duration_ms": None,
            },
        },
        "source_trace_refs": {
            "summary_path": str(turn_dir / "summary.json"),
            "events_path": str(turn_dir / "events.jsonl"),
            "request_start_path": str(turn_dir / "request.start.json"),
            "response_progress_path": None,
            "response_final_path": str(turn_dir / "response.final.json"),
            "prompt_artifacts": [],
        },
    }

    (turn_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (turn_dir / "events.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    (turn_dir / "request.start.json").write_text(json.dumps(request_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (turn_dir / "response.final.json").write_text(json.dumps(response_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (turn_dir / "scenario.capture.json").write_text(json.dumps(scenario_capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    index_path = debug_dir / "index.jsonl"
    entries: dict[str, dict[str, object]] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict) and isinstance(payload.get("turn_id"), str):
                entries[str(payload["turn_id"])] = payload
    entries[turn_id] = {
        "turn_id": turn_id,
        "session_ref": request_payload.get("session_ref"),
        "player_name": ((request_payload.get("player") or {}).get("name") if isinstance(request_payload.get("player"), dict) else None),
        "user_message": request_payload.get("user_message"),
        "status": "completed",
        "started_at": stamp.isoformat(),
        "ended_at": stamp.isoformat(),
        "debug_dir": str(turn_dir),
        "final_reply_preview": response_payload.get("final_reply"),
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "".join(json.dumps(entries[key], ensure_ascii=False) + "\n" for key in sorted(entries)),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
