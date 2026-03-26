from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect


def create_app() -> FastAPI:
    app = FastAPI(title="Mina Stub App Server", version="0.1.0")
    state: dict[str, Any] = {
        "threads": {},
        "loaded_threads": set(),
        "turns": {},
    }

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "provider_configured": False}

    @app.websocket("/v1/app-server/ws")
    async def app_server_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        while True:
            try:
                request = await websocket.receive_json()
            except WebSocketDisconnect:
                return
            response = await handle_rpc(websocket, request, state)
            await websocket.send_json(response)

    return app


async def handle_rpc(websocket: WebSocket, request: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    rpc_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return rpc_ok(
            rpc_id,
            {
                "server": "mina_stub_app_server",
                "protocol": "jsonrpc-2.0",
                "methods": [
                    "initialize",
                    "thread/start",
                    "thread/list",
                    "thread/loaded/list",
                    "thread/read",
                    "thread/resume",
                    "thread/archive",
                    "thread/unsubscribe",
                    "thread/unarchive",
                    "thread/name/set",
                    "thread/metadata/update",
                    "thread/fork",
                    "thread/compact/start",
                    "thread/shellCommand",
                    "thread/rollback",
                    "turn/start",
                    "turn/steer",
                    "turn/interrupt",
                    "command/exec",
                    "command/exec/write",
                    "command/exec/resize",
                    "command/exec/terminate",
                    "tool/result",
                    "approval/respond",
                ],
            },
        )
    if method == "thread/start":
        thread = _upsert_thread(state, params)
        state["loaded_threads"].add(thread["thread_id"])
        await websocket.send_json({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": thread}})
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "thread/resume":
        thread_id = str(params.get("thread_id"))
        thread = state["threads"].get(thread_id) or _upsert_thread(
            state,
            {"thread_id": thread_id, "player_uuid": "stub-player", "player_name": "Stub", "metadata": {}},
        )
        state["loaded_threads"].add(thread_id)
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "thread/list":
        archived = params.get("archived")
        threads = list(state["threads"].values())
        if archived is not None:
            threads = [thread for thread in threads if bool(thread.get("archived")) == bool(archived)]
        return rpc_ok(rpc_id, {"threads": threads[: int(params.get("limit") or 50)]})
    if method == "thread/loaded/list":
        return rpc_ok(rpc_id, {"thread_ids": sorted(state["loaded_threads"])})
    if method == "thread/read":
        thread_id = str(params.get("thread_id"))
        thread = dict(state["threads"].get(thread_id) or _upsert_thread(
            state,
            {"thread_id": thread_id, "player_uuid": "stub-player", "player_name": "Stub", "metadata": {}},
        ))
        if thread_id not in state["loaded_threads"]:
            thread["status"] = "notLoaded"
        if params.get("include_turns"):
            thread["turns"] = list(state["turns"].get(thread_id, []))
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "thread/archive":
        thread_id = str(params.get("thread_id"))
        thread = dict(state["threads"].get(thread_id) or {})
        thread["archived"] = True
        thread["updated_at"] = _now_iso()
        state["threads"][thread_id] = thread
        await websocket.send_json({"jsonrpc": "2.0", "method": "thread/archived", "params": {"thread_id": thread_id}})
        return rpc_ok(rpc_id, {})
    if method == "thread/unarchive":
        thread_id = str(params.get("thread_id"))
        thread = dict(state["threads"].get(thread_id) or {})
        thread["archived"] = False
        thread["updated_at"] = _now_iso()
        state["threads"][thread_id] = thread
        await websocket.send_json({"jsonrpc": "2.0", "method": "thread/unarchived", "params": {"thread_id": thread_id}})
        return rpc_ok(rpc_id, {})
    if method == "thread/unsubscribe":
        thread_id = str(params.get("thread_id"))
        if thread_id not in state["loaded_threads"]:
            return rpc_ok(rpc_id, {"status": "notLoaded"})
        state["loaded_threads"].discard(thread_id)
        await websocket.send_json(
            {"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"thread_id": thread_id, "status": "notLoaded"}}
        )
        await websocket.send_json({"jsonrpc": "2.0", "method": "thread/closed", "params": {"thread_id": thread_id}})
        return rpc_ok(rpc_id, {"status": "unsubscribed"})
    if method == "thread/name/set":
        thread_id = str(params.get("thread_id"))
        thread = dict(state["threads"].get(thread_id) or {})
        thread["name"] = params.get("name")
        thread["updated_at"] = _now_iso()
        state["threads"][thread_id] = thread
        await websocket.send_json(
            {"jsonrpc": "2.0", "method": "thread/name/updated", "params": {"thread_id": thread_id, "name": params.get("name")}}
        )
        return rpc_ok(rpc_id, {})
    if method == "thread/metadata/update":
        thread_id = str(params.get("thread_id"))
        thread = dict(state["threads"].get(thread_id) or {})
        metadata = dict(thread.get("metadata") or {})
        metadata.update(params.get("metadata") or {})
        thread["metadata"] = metadata
        thread["updated_at"] = _now_iso()
        state["threads"][thread_id] = thread
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "thread/fork":
        source_thread_id = str(params.get("source_thread_id"))
        source = dict(state["threads"].get(source_thread_id) or {})
        thread = _upsert_thread(
            state,
            {
                "thread_id": params.get("thread_id"),
                "player_uuid": params.get("player_uuid") or source.get("player_uuid") or "stub-player",
                "player_name": params.get("player_name") or source.get("player_name") or "Stub",
                "metadata": {"forked_from": source_thread_id, **(params.get("metadata") or {})},
            },
        )
        thread["turns"] = list(state["turns"].get(source_thread_id, []))
        state["turns"][thread["thread_id"]] = list(thread["turns"])
        state["loaded_threads"].add(thread["thread_id"])
        await websocket.send_json({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": thread}})
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "thread/compact/start":
        thread_id = str(params.get("thread_id"))
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "thread/compacted",
                "params": {"thread_id": thread_id, "summary": f"Stub compact summary for {thread_id}", "path": "", "transcript_path": ""},
            }
        )
        return rpc_ok(rpc_id, {})
    if method == "thread/rollback":
        thread_id = str(params.get("thread_id"))
        num_turns = max(int(params.get("num_turns") or 1), 1)
        turns = list(state["turns"].get(thread_id, []))
        if turns:
            turns = turns[:-num_turns]
        state["turns"][thread_id] = turns
        thread = dict(state["threads"].get(thread_id) or {})
        thread["turns"] = turns
        return rpc_ok(rpc_id, {"thread": thread})
    if method == "turn/start":
        turn_id = str(params.get("turn_id") or "stub-turn")
        thread_id = str(params.get("thread_id") or "stub-thread")
        user_message = str(params.get("user_message") or "stub request")
        final_reply = f"stub-ok: {user_message}"
        response_turn = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "running",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        write_stub_bundle(params, final_reply, turn_id, thread_id)
        await websocket.send_json(
            {"jsonrpc": "2.0", "method": "turn/started", "params": {"thread_id": thread_id, "turn": response_turn}}
        )
        item_id = f"assistant-{turn_id}"
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "item_kind": "assistant_message",
                    "payload": {"text": ""},
                },
            }
        )
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "item/assistantMessage/delta",
                "params": {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "delta": final_reply,
                },
            }
        )
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "item_kind": "assistant_message",
                    "payload": {"text": final_reply},
                },
            }
        )
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "thread_id": thread_id,
                    "turn": {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "status": "completed",
                        "created_at": _now_iso(),
                        "updated_at": _now_iso(),
                        "final_reply": final_reply,
                    },
                },
            }
        )
        state["turns"].setdefault(thread_id, []).append(
            {
                "turn_id": turn_id,
                "user_message": user_message,
                "final_reply": final_reply,
                "status": "completed",
                "created_at": _now_iso(),
                "items": [
                    {
                        "item_id": item_id,
                        "item_kind": "assistant_message",
                        "status": "completed",
                        "payload": {"text": final_reply},
                    }
                ],
            }
        )
        return rpc_ok(rpc_id, {"turn": response_turn})
    if method == "turn/steer":
        return rpc_ok(rpc_id, {"turn_id": params.get("expected_turn_id")})
    if method == "thread/shellCommand":
        thread_id = str(params.get("thread_id") or "stub-thread")
        turn_id = f"{thread_id}__shell"
        command = str(params.get("command") or "")
        output = subprocess.run(
            ["/bin/zsh", "-lc", command],
            capture_output=True,
            text=True,
            check=False,
        )
        await websocket.send_json(
            {"jsonrpc": "2.0", "method": "turn/started", "params": {"thread_id": thread_id, "turn": {"thread_id": thread_id, "turn_id": turn_id, "status": "running"}}}
        )
        item_id = f"cmd-{turn_id}"
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {"thread_id": thread_id, "turn_id": turn_id, "item_id": item_id, "item_kind": "command_execution", "payload": {"command": command}},
            }
        )
        if output.stdout:
            await websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "item_id": item_id,
                        "process_id": f"stub-{turn_id}",
                        "stream": "stdout",
                        "delta_base64": base64.b64encode(output.stdout.encode("utf-8")).decode("ascii"),
                        "cap_reached": False,
                    },
                }
            )
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {"thread_id": thread_id, "turn_id": turn_id, "item_id": item_id, "item_kind": "command_execution", "payload": {"exit_code": output.returncode}},
            }
        )
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"thread_id": thread_id, "turn": {"thread_id": thread_id, "turn_id": turn_id, "status": "completed", "final_reply": "shell done"}},
            }
        )
        return rpc_ok(rpc_id, {})
    if method == "command/exec":
        process_id = str(params.get("process_id") or "stub-proc")
        result = subprocess.run(
            list(params.get("command") or []),
            capture_output=True,
            text=True,
            check=False,
        )
        if params.get("stream_stdout_stderr") and result.stdout:
            await websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "command/exec/outputDelta",
                    "params": {
                        "process_id": process_id,
                        "stream": "stdout",
                        "delta_base64": base64.b64encode(result.stdout.encode("utf-8")).decode("ascii"),
                        "cap_reached": False,
                    },
                }
            )
        return rpc_ok(
            rpc_id,
            {
                "exit_code": result.returncode,
                "stdout": "" if params.get("stream_stdout_stderr") else result.stdout,
                "stderr": "" if params.get("stream_stdout_stderr") else result.stderr,
            },
        )
    if method in {"command/exec/write", "command/exec/resize", "command/exec/terminate"}:
        return rpc_ok(rpc_id, {})
    if method in {"turn/interrupt", "tool/result", "approval/respond"}:
        return rpc_ok(rpc_id, {})
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Unsupported method: {method}"},
    }


def rpc_ok(rpc_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_thread(state: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(params.get("thread_id"))
    existing = dict(state["threads"].get(thread_id) or {})
    now = _now_iso()
    thread = {
        "thread_id": thread_id,
        "player_uuid": params.get("player_uuid") or existing.get("player_uuid") or "stub-player",
        "player_name": params.get("player_name") or existing.get("player_name") or "Stub",
        "status": existing.get("status") or "idle",
        "archived": existing.get("archived", False),
        "name": existing.get("name"),
        "metadata": params.get("metadata") or existing.get("metadata") or {},
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    state["threads"][thread_id] = thread
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(description="Stub Mina app-server for headless smoke tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def write_stub_bundle(request_payload: dict[str, object], final_reply: str, turn_id: str, thread_id: str) -> None:
    stamp = datetime.now(timezone.utc)
    data_dir = Path(os.getenv("MINA_AGENT_DATA_DIR", "agent_service/data"))
    debug_dir = data_dir / "debug"
    turn_dir = debug_dir / "turns" / f"{stamp:%Y-%m-%d}" / f"{stamp:%H%M%S_%f}__stub__{turn_id}"
    turn_dir.mkdir(parents=True, exist_ok=True)

    context = dict(request_payload.get("context") or {}) if isinstance(request_payload.get("context"), dict) else {}
    summary = {
        "version": 1,
        "turn": {
            "turn_id": turn_id,
            "thread_id": thread_id,
            "started_at": stamp.isoformat(),
            "ended_at": stamp.isoformat(),
            "status": "completed",
            "debug_dir": str(turn_dir),
            "resume_events": [],
        },
        "user_input": {
            "thread_id": thread_id,
            "user_message": request_payload.get("user_message"),
            "player": context.get("player"),
            "server_env": context.get("server_env"),
            "limits": (context.get("limits") or {}),
            "pending_confirmation": None,
        },
        "capabilities": {"total": 0, "ids": [], "by_kind": {}, "by_risk_class": {}, "by_handler_kind": {}},
        "context_builds": [],
        "timeline": [],
        "prompt_artifacts": [],
        "final_reply_preview": final_reply,
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
            "payload": {
                "thread_id": thread_id,
                "user_message": request_payload.get("user_message"),
                "player": context.get("player"),
                "server_env": context.get("server_env"),
                "limits": context.get("limits"),
                "task": None,
            },
        },
        {
            "ts": stamp.isoformat(),
            "turn_id": turn_id,
            "event_type": "turn_completed",
            "step_index": 1,
            "payload": {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "final_reply": final_reply,
                "task_id": None,
            },
        },
    ]
    request_artifact = {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "user_message": request_payload.get("user_message"),
        "player": context.get("player"),
        "server_env": context.get("server_env"),
        "limits": context.get("limits"),
        "pending_confirmation": None,
        "task": None,
    }
    response_artifact = {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "type": "final_reply",
        "status": "completed",
        "final_reply": final_reply,
        "task_id": None,
        "reason": None,
        "error": None,
    }
    player_name = ((context.get("player") or {}).get("name") if isinstance(context.get("player"), dict) else None) or "Steve"
    scenario_capture = {
        "version": 1,
        "turn": {
            "turn_id": turn_id,
            "thread_id": thread_id,
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
            "player": context.get("player"),
            "server_env": context.get("server_env"),
            "limits": context.get("limits"),
            "pending_confirmation": None,
        },
        "selected_capability_ids": [],
        "assertion_slots": {
            "observed_capability_ids": [],
            "observed_reply_preview": final_reply,
            "observed_confirmation_expected": False,
            "suggested_assertions": {
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
        "source_trace_refs": {
            "summary_path": str(turn_dir / "summary.json"),
            "events_path": str(turn_dir / "events.jsonl"),
            "request_start_path": str(turn_dir / "request.start.json"),
            "response_progress_path": None,
            "response_final_path": str(turn_dir / "response.final.json"),
            "prompt_artifacts": [],
        },
    }

    (turn_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (turn_dir / "events.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")
    (turn_dir / "request.start.json").write_text(json.dumps(request_artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    (turn_dir / "response.final.json").write_text(json.dumps(response_artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    (turn_dir / "scenario.capture.json").write_text(json.dumps(scenario_capture, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
