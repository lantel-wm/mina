from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mina_agent.config import Settings


SUMMARY_VERSION = 1
CAPTURE_VERSION = 1


@dataclass(slots=True)
class TruncationStats:
    strings_truncated: int = 0
    chars_omitted: int = 0
    list_items_omitted: int = 0
    dict_keys_omitted: int = 0
    oversize_payloads: int = 0

    def merge(self, other: "TruncationStats") -> None:
        self.strings_truncated += other.strings_truncated
        self.chars_omitted += other.chars_omitted
        self.list_items_omitted += other.list_items_omitted
        self.dict_keys_omitted += other.dict_keys_omitted
        self.oversize_payloads += other.oversize_payloads

    def as_dict(self) -> dict[str, int]:
        return {
            "strings_truncated": self.strings_truncated,
            "chars_omitted": self.chars_omitted,
            "list_items_omitted": self.list_items_omitted,
            "dict_keys_omitted": self.dict_keys_omitted,
            "oversize_payloads": self.oversize_payloads,
        }


@dataclass(slots=True)
class DebugPreviewLimits:
    string_preview_chars: int
    list_preview_items: int
    dict_preview_keys: int
    event_payload_chars: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "DebugPreviewLimits":
        return cls(
            string_preview_chars=max(settings.debug_string_preview_chars, 1),
            list_preview_items=max(settings.debug_list_preview_items, 1),
            dict_preview_keys=max(settings.debug_dict_preview_keys, 1),
            event_payload_chars=max(settings.debug_event_payload_chars, 256),
        )

    def string_preview_window(self) -> tuple[int, int]:
        tail = self.string_preview_chars // 3
        head = max(self.string_preview_chars - tail, 1)
        if head + tail > self.string_preview_chars:
            tail = max(self.string_preview_chars - head, 0)
        return head, tail


class DebugRecorder:
    def record_event(self, turn_id: str, event_type: str, payload: dict[str, Any], *, step_index: int | None = None) -> None:
        raise NotImplementedError


class NoopDebugRecorder(DebugRecorder):
    def record_event(self, turn_id: str, event_type: str, payload: dict[str, Any], *, step_index: int | None = None) -> None:
        return None


class FileDebugRecorder(DebugRecorder):
    def __init__(self, debug_dir: Path, limits: DebugPreviewLimits) -> None:
        self._debug_dir = debug_dir
        self._limits = limits
        self._lock = threading.Lock()
        self._index_path = self._debug_dir / "index.jsonl"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    def record_event(self, turn_id: str, event_type: str, payload: dict[str, Any], *, step_index: int | None = None) -> None:
        with self._lock:
            stamp = datetime.now(timezone.utc)
            turn_dir = self._resolve_turn_dir(turn_id, stamp, event_type, payload)
            events_path = turn_dir / "events.jsonl"
            summary_path = turn_dir / "summary.json"

            summary = self._load_summary(summary_path, turn_id, turn_dir)
            raw_payload = _jsonable_value(payload)
            prompt_artifact = self._write_model_request_artifact(
                turn_dir,
                event_type,
                raw_payload,
                step_index,
            )
            sanitized_payload, stats = sanitize_event_payload(raw_payload, self._limits)
            self._merge_truncation(summary, stats)

            event_record = {
                "ts": stamp.isoformat(),
                "turn_id": turn_id,
                "event_type": event_type,
                "step_index": step_index,
                "payload": sanitized_payload,
            }
            if prompt_artifact is not None:
                event_record["artifact_ref"] = prompt_artifact
            self._append_jsonl(events_path, event_record)
            self._apply_summary_update(
                summary,
                event_type,
                sanitized_payload,
                raw_payload,
                stamp,
                step_index,
                prompt_artifact,
            )
            self._write_bundle_artifacts(
                turn_dir,
                turn_id,
                summary,
                event_type,
                raw_payload,
                stamp,
                step_index,
            )
            self._write_json(summary_path, summary)
            self._rewrite_index(summary, turn_dir)

    def _resolve_turn_dir(
        self,
        turn_id: str,
        stamp: datetime,
        event_type: str,
        payload: dict[str, Any],
    ) -> Path:
        turns_dir = self._debug_dir / "turns"
        existing = next((path for path in turns_dir.glob(f"*/*{turn_id}") if path.is_dir()), None)
        if existing is not None:
            existing.mkdir(parents=True, exist_ok=True)
            return existing

        turn_dir = turns_dir / f"{stamp:%Y-%m-%d}" / self._turn_dir_name(turn_id, stamp, event_type, payload)
        turn_dir.mkdir(parents=True, exist_ok=True)
        return turn_dir

    def _turn_dir_name(
        self,
        turn_id: str,
        stamp: datetime,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        label = "turn"
        if event_type == "turn_started":
            label = self._path_segment(str(payload.get("user_message") or "turn"))
        return f"{stamp:%H%M%S_%f}__{label}__{turn_id}"

    def _path_segment(self, value: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value.strip())
        normalized = normalized.strip("._-")
        return (normalized[:48] or "turn").lower()

    def _load_summary(self, summary_path: Path, turn_id: str, turn_dir: Path) -> dict[str, Any]:
        if summary_path.exists():
            return json.loads(summary_path.read_text(encoding="utf-8"))
        return {
            "version": SUMMARY_VERSION,
            "turn": {
                "turn_id": turn_id,
                "session_ref": None,
                "started_at": None,
                "ended_at": None,
                "status": "running",
                "debug_dir": str(turn_dir),
                "resume_events": [],
            },
            "user_input": None,
            "capabilities": {
                "total": 0,
                "ids": [],
                "by_kind": {},
                "by_risk_class": {},
                "by_handler_kind": {},
            },
            "context_builds": [],
            "timeline": [],
            "prompt_artifacts": [],
            "final_reply_preview": None,
            "truncation": TruncationStats().as_dict(),
        }

    def _append_jsonl(self, target: Path, payload: dict[str, Any]) -> None:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_json(self, target: Path, payload: dict[str, Any]) -> None:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _merge_truncation(self, summary: dict[str, Any], stats: TruncationStats) -> None:
        current = summary["truncation"]
        for key, value in stats.as_dict().items():
            current[key] = int(current.get(key, 0)) + value

    def _apply_summary_update(
        self,
        summary: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        raw_payload: dict[str, Any],
        stamp: datetime,
        step_index: int | None,
        prompt_artifact: dict[str, Any] | None,
    ) -> None:
        if event_type == "turn_started":
            turn = summary["turn"]
            turn["session_ref"] = payload.get("session_ref")
            turn["started_at"] = turn["started_at"] or stamp.isoformat()
            turn["status"] = "running"
            summary["user_input"] = {
                "user_message": payload.get("user_message"),
                "player": payload.get("player"),
                "server_env": payload.get("server_env"),
                "limits": payload.get("limits"),
                "pending_confirmation": payload.get("pending_confirmation"),
            }
            return

        if event_type == "turn_resumed":
            summary["turn"].setdefault("resume_events", []).append(
                {
                    "ts": stamp.isoformat(),
                    "step_index": step_index,
                    "payload": payload,
                }
            )
            return

        if event_type == "capabilities_resolved":
            summary["capabilities"] = payload
            return

        if event_type == "context_built":
            if step_index is None:
                return
            entry = self._upsert_step(summary["context_builds"], step_index)
            entry["sections"] = payload.get("sections", [])
            entry["message_stats"] = payload.get("message_stats", {})
            entry["budget_report"] = payload.get("budget_report", {})
            entry["composition"] = payload.get("composition", {})
            return

        if event_type == "model_request" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["model_request"] = {
                "message_count": raw_payload.get("message_count"),
                "message_stats": raw_payload.get("message_stats", {}),
                "provider_input_artifact": prompt_artifact,
            }
            if prompt_artifact is not None:
                self._upsert_prompt_artifact(summary, prompt_artifact)
            return

        if event_type == "context_compaction_requested" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            collection = step.setdefault("context_compactions", [])
            entry = self._upsert_pass(collection, int(payload.get("pass_index", 0)))
            entry["request"] = {
                "current_tokens": payload.get("current_tokens"),
                "target_tokens": payload.get("target_tokens"),
                "message_count": raw_payload.get("message_count"),
                "message_stats": raw_payload.get("message_stats", {}),
                "provider_input_artifact": prompt_artifact,
            }
            if prompt_artifact is not None:
                self._upsert_prompt_artifact(summary, prompt_artifact)
            return

        if event_type == "context_compaction_finished" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            collection = step.setdefault("context_compactions", [])
            entry = self._upsert_pass(collection, int(payload.get("pass_index", 0)))
            entry["response"] = payload
            return

        if event_type == "model_response" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["model_response"] = payload
            return

        if event_type == "model_decision" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["decision"] = payload
            return

        if event_type == "capability_started" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["capability"] = payload
            return

        if event_type == "capability_finished" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["capability_result"] = payload
            return

        if event_type == "bridge_result" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["bridge_result"] = payload
            return

        if event_type == "delegate_result" and step_index is not None:
            step = self._upsert_step(summary["timeline"], step_index)
            step["delegate_result"] = payload
            return

        if event_type == "turn_completed":
            summary["turn"]["ended_at"] = stamp.isoformat()
            summary["turn"]["status"] = "completed"
            summary["final_reply_preview"] = payload.get("final_reply")
            return

        if event_type == "turn_failed":
            summary["turn"]["ended_at"] = stamp.isoformat()
            summary["turn"]["status"] = "failed"
            summary["final_reply_preview"] = payload.get("final_reply")
            summary["failure"] = payload

    def _write_model_request_artifact(
        self,
        turn_dir: Path,
        event_type: str,
        payload: dict[str, Any],
        step_index: int | None,
    ) -> dict[str, Any] | None:
        if event_type not in {"model_request", "context_compaction_requested"}:
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        prompts_dir = turn_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        step_label = f"{int(step_index):03d}" if step_index is not None else "unknown"
        filename_suffix = ""
        if event_type == "context_compaction_requested":
            pass_index = int(payload.get("pass_index") or 0)
            filename_suffix = f".context_compaction_pass_{pass_index}"
        return self._write_provider_input_artifact(turn_dir, prompts_dir, payload, step_label, step_index, filename_suffix)

    def _write_provider_input_artifact(
        self,
        turn_dir: Path,
        prompts_dir: Path,
        payload: dict[str, Any],
        step_label: str,
        step_index: int | None,
        filename_suffix: str = "",
    ) -> dict[str, Any] | None:
        provider_input = payload.get("provider_input_buffer")
        if not isinstance(provider_input, dict):
            return None
        body_text = provider_input.get("body_text")
        if not isinstance(body_text, str):
            return None
        extension = str(provider_input.get("extension") or ".txt")
        if not extension.startswith("."):
            extension = f".{extension}"
        target = prompts_dir / f"step_{step_label}{filename_suffix}.provider_input{extension}"
        target.write_text(body_text, encoding="utf-8")
        return {
            "kind": "provider_input_buffer",
            "path": str(target),
            "relative_path": str(target.relative_to(turn_dir)),
            "step_index": step_index,
            "content_type": provider_input.get("content_type"),
            "buffer_kind": provider_input.get("kind"),
        }

    def _upsert_prompt_artifact(self, summary: dict[str, Any], artifact: dict[str, Any]) -> None:
        collection = summary.setdefault("prompt_artifacts", [])
        for index, existing in enumerate(collection):
            if isinstance(existing, dict) and existing.get("relative_path") == artifact.get("relative_path"):
                collection[index] = artifact
                return
        collection.append(artifact)

    def _upsert_step(self, collection: list[dict[str, Any]], step_index: int) -> dict[str, Any]:
        for entry in collection:
            if entry.get("step_index") == step_index:
                return entry
        entry = {"step_index": step_index}
        collection.append(entry)
        collection.sort(key=lambda item: int(item.get("step_index", 0)))
        return entry

    def _upsert_pass(self, collection: list[dict[str, Any]], pass_index: int) -> dict[str, Any]:
        for entry in collection:
            if entry.get("pass_index") == pass_index:
                return entry
        entry = {"pass_index": pass_index}
        collection.append(entry)
        collection.sort(key=lambda item: int(item.get("pass_index", 0)))
        return entry

    def _write_bundle_artifacts(
        self,
        turn_dir: Path,
        turn_id: str,
        summary: dict[str, Any],
        event_type: str,
        raw_payload: dict[str, Any],
        stamp: datetime,
        step_index: int | None,
    ) -> None:
        if event_type == "turn_started":
            self._write_json(
                turn_dir / "request.start.json",
                {
                    "turn_id": turn_id,
                    "session_ref": raw_payload.get("session_ref"),
                    "user_message": raw_payload.get("user_message"),
                    "player": raw_payload.get("player"),
                    "server_env": raw_payload.get("server_env"),
                    "limits": raw_payload.get("limits"),
                    "pending_confirmation": raw_payload.get("pending_confirmation"),
                    "task": raw_payload.get("task"),
                },
            )

        progress_entry = self._progress_entry_from_event(raw_payload, event_type, stamp, step_index)
        if progress_entry is not None:
            self._append_jsonl(turn_dir / "response.progress.jsonl", progress_entry)

        if event_type in {"turn_completed", "turn_failed"}:
            self._write_json(
                turn_dir / "response.final.json",
                {
                    "turn_id": turn_id,
                    "type": "final_reply",
                    "status": "completed" if event_type == "turn_completed" else "failed",
                    "final_reply": raw_payload.get("final_reply"),
                    "pending_confirmation_id": raw_payload.get("pending_confirmation_id"),
                    "pending_confirmation_effect_summary": raw_payload.get("pending_confirmation_effect_summary"),
                    "task_id": raw_payload.get("task_id"),
                    "reason": raw_payload.get("reason"),
                    "error": raw_payload.get("error"),
                },
            )

        self._write_json(turn_dir / "scenario.capture.json", self._build_scenario_capture(turn_dir, summary))

    def _progress_entry_from_event(
        self,
        raw_payload: dict[str, Any],
        event_type: str,
        stamp: datetime,
        step_index: int | None,
    ) -> dict[str, Any] | None:
        if event_type == "capability_finished" and raw_payload.get("status") == "awaiting_bridge_result":
            continuation_id = raw_payload.get("continuation_id")
            if not continuation_id:
                return None
            return {
                "ts": stamp.isoformat(),
                "step_index": step_index,
                "type": "action_request_batch",
                "continuation_id": continuation_id,
                "action_request_batch": [
                    {
                        "continuation_id": continuation_id,
                        "intent_id": raw_payload.get("intent_id"),
                        "capability_id": raw_payload.get("capability_id"),
                        "risk_class": raw_payload.get("risk_class"),
                        "effect_summary": raw_payload.get("effect_summary"),
                        "preconditions": raw_payload.get("preconditions") or [],
                        "arguments": raw_payload.get("arguments") or {},
                        "requires_confirmation": bool(raw_payload.get("requires_confirmation")),
                    }
                ],
            }

        if event_type == "turn_yielded":
            return {
                "ts": stamp.isoformat(),
                "step_index": step_index,
                "type": "progress_update",
                "continuation_id": raw_payload.get("continuation_id"),
                "reason": raw_payload.get("reason"),
                "trace_events": raw_payload.get("trace_events") or [],
            }
        return None

    def _build_scenario_capture(self, turn_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
        user_input = summary.get("user_input") or {}
        turn = summary.get("turn") or {}
        final_response = self._read_json_if_exists(turn_dir / "response.final.json")
        selected_capability_ids = self._selected_capability_ids(summary)
        observed_duration_ms = self._observed_duration_ms(turn)
        confirmation_expected = None
        if isinstance(final_response, dict):
            confirmation_expected = bool(final_response.get("pending_confirmation_id"))

        player_name = ((user_input.get("player") or {}).get("name")) or "Steve"
        scenario = {
            "suite": "real",
            "scenario_id": turn.get("turn_id"),
            "world_template": None,
            "status": "runnable_now",
            "expectation": "target_state",
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
                    "message": user_input.get("user_message"),
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
                "expected_final_status": turn.get("status") if turn.get("status") in {"completed", "failed"} else None,
                "forbidden_statuses": [],
                "required_capability_ids": [],
                "forbidden_capability_ids": [],
                "confirmation_expected": confirmation_expected,
                "required_reply_substrings": [],
                "forbidden_reply_substrings": [],
                "max_duration_ms": None,
            },
        }
        return {
            "version": CAPTURE_VERSION,
            "turn": {
                "turn_id": turn.get("turn_id"),
                "session_ref": turn.get("session_ref"),
                "started_at": turn.get("started_at"),
                "ended_at": turn.get("ended_at"),
                "status": turn.get("status"),
                "debug_dir": turn.get("debug_dir"),
            },
            "scenario": scenario,
            "request_snapshot": {
                "player": user_input.get("player"),
                "server_env": user_input.get("server_env"),
                "limits": user_input.get("limits"),
                "pending_confirmation": user_input.get("pending_confirmation"),
            },
            "selected_capability_ids": selected_capability_ids,
            "assertion_slots": {
                "observed_capability_ids": selected_capability_ids,
                "observed_reply_preview": summary.get("final_reply_preview"),
                "observed_confirmation_expected": confirmation_expected,
                "suggested_assertions": {
                    "expected_final_status": turn.get("status") if turn.get("status") in {"completed", "failed"} else None,
                    "forbidden_statuses": ["failed"] if turn.get("status") == "completed" else [],
                    "required_capability_ids": selected_capability_ids,
                    "forbidden_capability_ids": [],
                    "confirmation_expected": confirmation_expected,
                    "required_reply_substrings": [],
                    "forbidden_reply_substrings": [],
                    "max_duration_ms": observed_duration_ms,
                },
            },
            "source_trace_refs": {
                "summary_path": str(turn_dir / "summary.json"),
                "events_path": str(turn_dir / "events.jsonl"),
                "request_start_path": self._artifact_path_or_none(turn_dir / "request.start.json"),
                "response_progress_path": self._artifact_path_or_none(turn_dir / "response.progress.jsonl"),
                "response_final_path": self._artifact_path_or_none(turn_dir / "response.final.json"),
                "prompt_artifacts": [artifact.get("path") for artifact in summary.get("prompt_artifacts", []) if isinstance(artifact, dict) and artifact.get("path")],
            },
        }

    def _selected_capability_ids(self, summary: dict[str, Any]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for step in summary.get("timeline", []):
            if not isinstance(step, dict):
                continue
            for key in ("capability", "capability_result", "bridge_result"):
                payload = step.get(key)
                if not isinstance(payload, dict):
                    continue
                capability_id = payload.get("capability_id")
                if isinstance(capability_id, str) and capability_id and capability_id not in seen:
                    seen.add(capability_id)
                    ordered.append(capability_id)
            delegate_payload = step.get("delegate_result")
            delegate_capability_id = self._delegate_capability_id(delegate_payload)
            if delegate_capability_id is None:
                delegate_capability_id = self._delegate_capability_id_from_decision(step.get("decision"))
            if delegate_capability_id is not None and delegate_capability_id not in seen:
                seen.add(delegate_capability_id)
                ordered.append(delegate_capability_id)
        return ordered

    def _delegate_capability_id(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        observation = payload.get("observation")
        if isinstance(observation, dict):
            source = observation.get("source")
            if isinstance(source, str) and source.startswith("agent.") and source.endswith(".delegate"):
                return source
        delegate = payload.get("delegate")
        if isinstance(delegate, dict):
            role = delegate.get("role")
            if isinstance(role, str) and role in {"explore", "plan"}:
                return f"agent.{role}.delegate"
        return None

    def _delegate_capability_id_from_decision(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        intent = payload.get("intent")
        if intent == "delegate_explore":
            return "agent.explore.delegate"
        if intent == "delegate_plan":
            return "agent.plan.delegate"
        delegate_role = payload.get("delegate_role")
        if delegate_role == "explore":
            return "agent.explore.delegate"
        if delegate_role == "plan":
            return "agent.plan.delegate"
        delegate_request = payload.get("delegate_request")
        if isinstance(delegate_request, dict):
            role = delegate_request.get("role")
            if role == "explore":
                return "agent.explore.delegate"
            if role == "plan":
                return "agent.plan.delegate"
        return None

    def _observed_duration_ms(self, turn: dict[str, Any]) -> int | None:
        started_at = turn.get("started_at")
        ended_at = turn.get("ended_at")
        if not isinstance(started_at, str) or not isinstance(ended_at, str):
            return None
        started = _parse_iso8601(started_at)
        ended = _parse_iso8601(ended_at)
        if started is None or ended is None:
            return None
        return max(int((ended - started).total_seconds() * 1000), 0)

    def _read_json_if_exists(self, target: Path) -> dict[str, Any] | None:
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    def _artifact_path_or_none(self, target: Path) -> str | None:
        return str(target) if target.exists() else None

    def _rewrite_index(self, summary: dict[str, Any], turn_dir: Path) -> None:
        entries = {
            entry["turn_id"]: entry
            for entry in load_debug_index(self._debug_dir)
            if isinstance(entry, dict) and isinstance(entry.get("turn_id"), str)
        }
        user_input = summary.get("user_input") or {}
        player = user_input.get("player") or {}
        turn = summary.get("turn") or {}
        turn_id = turn.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return
        entries[turn_id] = {
            "turn_id": turn_id,
            "session_ref": turn.get("session_ref"),
            "player_name": player.get("name"),
            "user_message": user_input.get("user_message"),
            "status": turn.get("status"),
            "started_at": turn.get("started_at"),
            "ended_at": turn.get("ended_at"),
            "debug_dir": str(turn_dir),
            "final_reply_preview": summary.get("final_reply_preview"),
        }
        ordered = sorted(
            entries.values(),
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("turn_id") or ""),
            ),
        )
        self._index_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered),
            encoding="utf-8",
        )


def build_debug_recorder(settings: Settings) -> DebugRecorder:
    if not settings.debug_enabled:
        return NoopDebugRecorder()
    return FileDebugRecorder(settings.debug_dir, DebugPreviewLimits.from_settings(settings))


def load_debug_index(debug_dir: Path | str) -> list[dict[str, Any]]:
    target = Path(debug_dir) / "index.jsonl"
    if not target.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def lookup_debug_index(debug_dir: Path | str, turn_id: str) -> dict[str, Any] | None:
    for entry in reversed(load_debug_index(debug_dir)):
        if entry.get("turn_id") == turn_id:
            return entry
    return None


def resolve_turn_bundle(debug_dir: Path | str, turn_id: str) -> Path | None:
    entry = lookup_debug_index(debug_dir, turn_id)
    if isinstance(entry, dict) and entry.get("debug_dir"):
        return Path(str(entry["debug_dir"]))
    turns_dir = Path(debug_dir) / "turns"
    if not turns_dir.exists():
        return None
    return next((path for path in turns_dir.glob(f"*/*{turn_id}") if path.is_dir()), None)


def sanitize_event_payload(payload: dict[str, Any], limits: DebugPreviewLimits) -> tuple[dict[str, Any], TruncationStats]:
    stats = TruncationStats()
    sanitized = _sanitize_value(payload, limits, stats, root=True)
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= limits.event_payload_chars:
        return sanitized, stats

    stats.oversize_payloads += 1
    stats.chars_omitted += len(serialized) - limits.event_payload_chars
    preview = _truncate_text(serialized, limits)
    return (
        {
            "preview": preview,
            "full_chars": len(serialized),
            "preview_chars": len(preview),
            "truncated": True,
            "reason": "event_payload_chars_exceeded",
        },
        stats,
    )


def _sanitize_value(value: Any, limits: DebugPreviewLimits, stats: TruncationStats, *, root: bool = False) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= limits.string_preview_chars:
            return value
        head, tail = limits.string_preview_window()
        stats.strings_truncated += 1
        stats.chars_omitted += len(value) - min(len(value), head + tail)
        preview = _truncate_text(value, limits)
        return {
            "preview": preview,
            "full_chars": len(value),
            "preview_chars": len(preview),
            "truncated": True,
        }
    if isinstance(value, dict):
        items = list(value.items())
        omitted = max(len(items) - limits.dict_preview_keys, 0)
        if omitted:
            stats.dict_keys_omitted += omitted
        target: dict[str, Any] = {}
        for key, nested in items[: limits.dict_preview_keys]:
            target[str(key)] = _sanitize_value(nested, limits, stats)
        if omitted:
            target["_truncation"] = {
                "total_keys": len(items),
                "omitted_keys": omitted,
                "truncated": True,
            }
        return target
    if isinstance(value, (list, tuple, set)):
        sequence = list(value)
        omitted = max(len(sequence) - limits.list_preview_items, 0)
        if omitted:
            stats.list_items_omitted += omitted
        return {
            "items": [_sanitize_value(item, limits, stats) for item in sequence[: limits.list_preview_items]],
            "total_items": len(sequence),
            "omitted_items": omitted,
            "truncated": omitted > 0,
        }
    return _sanitize_value(str(value), limits, stats)


def _jsonable_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_value(item) for item in value]
    return str(value)


def _truncate_text(value: str, limits: DebugPreviewLimits) -> str:
    if len(value) <= limits.string_preview_chars:
        return value
    head, tail = limits.string_preview_window()
    if tail <= 0:
        return value[:head]
    return value[:head] + value[-tail:]


def _parse_iso8601(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
