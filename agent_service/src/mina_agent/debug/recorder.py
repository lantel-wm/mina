from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mina_agent.config import Settings


SUMMARY_VERSION = 1


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
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    def record_event(self, turn_id: str, event_type: str, payload: dict[str, Any], *, step_index: int | None = None) -> None:
        stamp = datetime.now(timezone.utc)
        turn_dir = self._resolve_turn_dir(turn_id, stamp, event_type, payload)
        events_path = turn_dir / "events.jsonl"
        summary_path = turn_dir / "summary.json"

        summary = self._load_summary(summary_path, turn_id, turn_dir)
        sanitized_payload, stats = sanitize_event_payload(payload, self._limits)
        self._merge_truncation(summary, stats)

        event_record = {
            "ts": stamp.isoformat(),
            "turn_id": turn_id,
            "event_type": event_type,
            "step_index": step_index,
            "payload": sanitized_payload,
        }
        self._append_jsonl(events_path, event_record)
        self._apply_summary_update(summary, event_type, sanitized_payload, stamp, step_index)
        self._write_json(summary_path, summary)

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
        stamp: datetime,
        step_index: int | None,
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
            entry["composition"] = payload.get("composition", {})
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

    def _upsert_step(self, collection: list[dict[str, Any]], step_index: int) -> dict[str, Any]:
        for entry in collection:
            if entry.get("step_index") == step_index:
                return entry
        entry = {"step_index": step_index}
        collection.append(entry)
        collection.sort(key=lambda item: int(item.get("step_index", 0)))
        return entry


def build_debug_recorder(settings: Settings) -> DebugRecorder:
    if not settings.debug_enabled:
        return NoopDebugRecorder()
    return FileDebugRecorder(settings.debug_dir, DebugPreviewLimits.from_settings(settings))


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


def _truncate_text(value: str, limits: DebugPreviewLimits) -> str:
    if len(value) <= limits.string_preview_chars:
        return value
    head, tail = limits.string_preview_window()
    if tail <= 0:
        return value[:head]
    return value[:head] + value[-tail:]
