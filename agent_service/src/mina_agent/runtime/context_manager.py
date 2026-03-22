from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.runtime.context_pack import ContextPack, ContextSlot, TrimPolicy
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest


@dataclass(slots=True)
class ContextBuildResult:
    messages: list[dict[str, str]]
    sections: list[dict[str, Any]]
    message_stats: dict[str, int]
    composition: dict[str, str]
    recovery_refs: list[dict[str, Any]]
    budget_report: dict[str, int]
    active_context_slots: list[str]


class ContextManager:
    _TRIM_POLICY = TrimPolicy(
        priority_order=(
            "capability_brief",
            "recoverable_history",
            "scene_slice",
            "task_focus",
            "confirmation_loop",
        ),
        hard_floor_chars=320,
    )

    def __init__(self, settings: Settings, store: Store, memory_policy: MemoryPolicy) -> None:
        self._settings = settings
        self._store = store
        self._memory_policy = memory_policy

    def build_messages(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability_descriptors: list[CapabilityDescriptor],
    ) -> ContextBuildResult:
        normalized_snapshot = self._normalize_snapshot(request.scoped_snapshot)
        session_turns = self._store.list_turns(request.session_ref)
        compacted_history = self._compact_history(request.session_ref, session_turns, turn_state)
        retrieved_memory = self._memory_policy.summarize_for_context(
            self._store.search_memories(request.session_ref, request.user_message, limit=6)
        )
        session_summary = self._store.get_session_summary(request.session_ref)
        recent_dialogue_memory = self._build_recent_dialogue_memory(session_summary)
        recovery_refs = self._collect_recovery_refs(turn_state, compacted_history, session_summary, retrieved_memory)

        pack = ContextPack(
            slots=[
                self._slot(
                    "stable_core",
                    "system",
                    "core.instructions",
                    "stable_cached_text",
                    self._stable_core_text(),
                    priority=100,
                ),
                self._slot(
                    "runtime_policy",
                    "system",
                    "runtime.policy+persona",
                    "dynamic_structured_reminder",
                    self._runtime_policy_payload(request, turn_state),
                    priority=95,
                ),
                self._slot(
                    "scene_slice",
                    "user",
                    "request.scoped_snapshot",
                    "structured_slice",
                    self._build_scene_slice(normalized_snapshot),
                    priority=85,
                ),
                self._slot(
                    "task_focus",
                    "user",
                    "turn_state.working_memory+task",
                    "structured_summary",
                    self._build_task_focus(turn_state),
                    priority=80,
                ),
                self._slot(
                    "confirmation_loop",
                    "user",
                    "turn_state.pending_confirmation",
                    "structured_loop",
                    self._build_confirmation_loop(turn_state),
                    priority=78,
                ),
                self._slot(
                    "recoverable_history",
                    "user",
                    "memory+history+refs",
                    "recoverable_recall",
                    {
                        "session_summary": session_summary,
                        "recent_dialogue_memory": recent_dialogue_memory,
                        "memories": [candidate.context_entry() for candidate in retrieved_memory],
                        "history": compacted_history,
                        "recovery_refs": recovery_refs,
                    },
                    priority=55,
                    recoverable=True,
                ),
                self._slot(
                    "capability_brief",
                    "user",
                    "resolved_capability_descriptors",
                    "minimal_capability_catalog",
                    [self._capability_payload(descriptor) for descriptor in capability_descriptors],
                    priority=40,
                ),
            ],
            trim_policy=self._TRIM_POLICY,
        )
        trimmed_pack = self._trim_pack(pack)

        system_content = self._render_slots([slot for slot in trimmed_pack.active_slots() if slot.role == "system"])
        user_content = self._render_slots([slot for slot in trimmed_pack.active_slots() if slot.role == "user"])
        messages = [{"role": "system", "content": system_content}, {"role": "user", "content": user_content}]
        total_chars = len(system_content) + len(user_content)

        return ContextBuildResult(
            messages=messages,
            sections=[slot.summary_entry() for slot in trimmed_pack.active_slots()],
            message_stats={
                "message_count": len(messages),
                "system_chars": len(system_content),
                "user_chars": len(user_content),
                "total_chars": total_chars,
            },
            composition={slot.name: slot.strategy for slot in trimmed_pack.active_slots()},
            recovery_refs=recovery_refs,
            budget_report={"budget": self._settings.context_char_budget, "used": total_chars},
            active_context_slots=[slot.name for slot in trimmed_pack.active_slots()],
        )

    def _stable_core_text(self) -> str:
        return (
            "You are Mina, a natural-language-first Minecraft companion runtime.\n"
            "Companionship comes before execution, and execution must serve player enjoyment.\n"
            "Default to grounded Simplified Chinese replies when action is unnecessary.\n"
            "Treat every action as a plan with assumptions; re-check live state instead of trusting stale context.\n"
            "Prefer guidance, retrieval, or isolated delegate exploration before execution when uncertainty is high.\n"
            "Delegate roles are strict: companion decides, explore inspects, plan proposes, bridge actions execute only in the main turn.\n"
            "Delegate turns may not call bridge actions and may not delegate recursively.\n"
            "Do not delegate explore repeatedly when no new facts were found. If live inspection is still needed and a visible read capability matches, call it directly.\n"
            "Never invent capability ids. Use an id from capability_brief exactly.\n"
            "Return JSON only.\n"
            'Reply/guide with {"intent":"reply","final_reply":"..."} or {"intent":"guide","final_reply":"..."}.\n'
            'Inspect/retrieve/execute with {"intent":"execute","capability_request":{"capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":false}}.\n'
            'Delegate with {"intent":"delegate_explore","delegate_role":"explore","delegate_objective":"..."} or {"intent":"delegate_plan","delegate_role":"plan","delegate_objective":"..."}.\n'
            'When confirmation is still needed for an executable capability, use {"intent":"await_confirmation","capability_request":{"capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":true},"confirmation_request":{"effect_summary":"...","reason":"..."}}.\n'
            'If `active_task_candidate` is present, set `"task_selection":"reuse_active"` when the user is clearly continuing it; otherwise set `"task_selection":"keep_current"`.'
        )

    def _runtime_policy_payload(self, request: TurnStartRequest, turn_state: TurnState) -> dict[str, Any]:
        return {
            "language": "Simplified Chinese by default",
            "server_env": request.server_env.model_dump(),
            "player_role": request.player.role,
            "limits": request.limits.model_dump(),
            "task": turn_state.task.context_entry(),
            "active_task_candidate": (
                turn_state.active_task_candidate.context_entry() if turn_state.active_task_candidate is not None else None
            ),
            "persona": {
                "style": "gentle, attentive, concise, situationally playful",
                "voice_rules": [
                    "Guide before taking over when direct execution is not necessary.",
                    "Do not overtalk or over-roleplay.",
                    "Use natural Simplified Chinese unless the user clearly asks for another language.",
                ],
            },
            "notes": [
                "Prefer read capabilities for world truth.",
                "Use recovery refs instead of repeating long content.",
                "Delegate exploration or planning when it reduces uncertainty without polluting the main context.",
                "Bridge actions remain in the main turn only.",
            ],
        }

    def _normalize_snapshot(self, scoped_snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "player": self._coerce_mapping(scoped_snapshot.get("player")),
            "world": self._coerce_mapping(scoped_snapshot.get("world")),
            "target_block": self._coerce_mapping(scoped_snapshot.get("target_block") or scoped_snapshot.get("target")),
            "recent_events": scoped_snapshot.get("recent_events") if isinstance(scoped_snapshot.get("recent_events"), list) else [],
            "server_rules_refs": self._coerce_mapping(scoped_snapshot.get("server_rules_refs")),
            "risk_state": self._coerce_mapping(scoped_snapshot.get("risk_state")),
        }

    def _build_scene_slice(self, normalized_snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "player": self._slice_part(normalized_snapshot.get("player")),
            "world": self._slice_part(normalized_snapshot.get("world")),
            "target_block": self._slice_part(normalized_snapshot.get("target_block")),
            "recent_events": self._slice_recent_events(normalized_snapshot.get("recent_events")),
            "server_rules_refs": self._slice_part(normalized_snapshot.get("server_rules_refs")),
            "risk_state": self._slice_part(normalized_snapshot.get("risk_state")),
        }

    def _build_task_focus(self, turn_state: TurnState) -> dict[str, Any]:
        active_observations = sorted(turn_state.observations, key=lambda item: item.salience, reverse=True)[:4]
        observation_refs = [observation.context_entry() for observation in turn_state.observations[-6:]]
        turn_state.working_memory.active_observations = active_observations
        turn_state.working_memory.observation_refs = observation_refs
        turn_state.working_memory.recovery_refs = self._collect_observation_recovery_refs(turn_state)
        return {
            "task": turn_state.task.context_entry(),
            "active_task_candidate": (
                turn_state.active_task_candidate.context_entry() if turn_state.active_task_candidate is not None else None
            ),
            "working_memory": turn_state.working_memory.context_entry(),
            "current_trigger": {
                "user_message": TurnStartRequest.model_validate(turn_state.request).user_message,
                "pending_confirmation": turn_state.pending_confirmation,
            },
        }

    def _build_confirmation_loop(self, turn_state: TurnState) -> dict[str, Any]:
        pending = turn_state.pending_confirmation
        if pending is None:
            return {"pending": False}
        return {
            "pending": True,
            "confirmation_id": pending.get("confirmation_id"),
            "effect_summary": pending.get("effect_summary"),
            "open_loops": list(turn_state.working_memory.open_loops),
        }

    def _compact_history(
        self,
        session_ref: str,
        recent_turns: list[dict[str, Any]],
        turn_state: TurnState,
    ) -> dict[str, Any]:
        if len(recent_turns) <= self._settings.context_recent_full_turns:
            return {
                "current_trigger": {"turn_id": turn_state.turn_id},
                "recent_turns": recent_turns,
                "session_compact_summary": None,
                "recovery_refs": [],
            }

        older_turns = recent_turns[: -self._settings.context_recent_full_turns]
        recent_tail = recent_turns[-self._settings.context_recent_full_turns :]
        summary_lines = [
            "Mina Compact Summary",
            "",
            "1. Task Continuity",
            f"- Current task: {turn_state.task.goal}",
            f"- Current task status: {turn_state.task.status}",
            "",
            "2. Earlier Turns",
        ]
        for turn in older_turns:
            summary_lines.append(
                f"- {turn['created_at']}: user={turn['user_message']!r}; status={turn['status']}; reply={turn.get('final_reply') or ''!r}"
            )
        summary_lines.extend(
            [
                "",
                "3. Side Effects And Preferences",
                "- Confirm exact effects or preferences via artifacts, memory search, or transcript before acting.",
                "",
                "4. Recovery Rule",
                f"- Read the full transcript at {self._store.session_dir(session_ref) / 'transcript.jsonl'} when exact wording matters.",
            ]
        )
        compact_summary = "\n".join(summary_lines)
        summary_record = self._store.write_compact_summary(
            session_ref,
            compact_summary,
            metadata={"older_turn_count": len(older_turns), "task_id": turn_state.task.task_id},
        )
        return {
            "current_trigger": {"turn_id": turn_state.turn_id},
            "recent_turns": recent_tail,
            "session_compact_summary": {
                "text": compact_summary,
                "path": summary_record["path"],
                "transcript_path": summary_record["transcript_path"],
            },
            "recovery_refs": [
                {"kind": "compact_summary", "path": summary_record["path"]},
                {"kind": "transcript", "path": summary_record["transcript_path"]},
            ],
        }

    def _collect_recovery_refs(
        self,
        turn_state: TurnState,
        compacted_history: dict[str, Any],
        session_summary: dict[str, Any] | None,
        retrieved_memory: list[Any],
    ) -> list[dict[str, Any]]:
        refs = self._collect_observation_recovery_refs(turn_state)
        refs.extend(compacted_history.get("recovery_refs", []))
        if session_summary and session_summary.get("transcript_path"):
            refs.append({"kind": "session_summary", "path": session_summary["transcript_path"]})
        for memory in retrieved_memory:
            refs.extend(memory.context_entry().get("artifact_refs", []))
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ref in refs:
            key = json.dumps(ref, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(ref)
        return unique

    def _collect_observation_recovery_refs(self, turn_state: TurnState) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for observation in turn_state.observations[-8:]:
            if observation.artifact_ref is not None:
                refs.append(observation.artifact_ref.context_ref())
        return refs

    def _build_recent_dialogue_memory(self, session_summary: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(session_summary, dict):
            return {"available": False}
        metadata = session_summary.get("metadata")
        if not isinstance(metadata, dict):
            return {"available": False}
        recent_window = metadata.get("recent_dialogue_window")
        window = [entry for entry in recent_window if isinstance(entry, dict)] if isinstance(recent_window, list) else []
        last_dialogue_turn = metadata.get("last_dialogue_turn") or metadata.get("recent_dialogue_turn")
        active_dialogue_loop = metadata.get("active_dialogue_loop")
        last_dialogue_resolution = metadata.get("last_dialogue_resolution")
        continuity_hint = metadata.get("continuity_hint")
        if not window and not isinstance(last_dialogue_turn, dict) and not isinstance(active_dialogue_loop, dict):
            return {"available": False}
        if not window and isinstance(last_dialogue_turn, dict):
            window = [last_dialogue_turn]
        return {
            "available": True,
            "recent_dialogue_window": window[-3:],
            "last_dialogue_turn": last_dialogue_turn,
            "active_dialogue_loop": active_dialogue_loop,
            "last_dialogue_resolution": last_dialogue_resolution,
            "continuity_hint": continuity_hint,
        }

    def _capability_payload(self, descriptor: CapabilityDescriptor) -> dict[str, Any]:
        return {
            "id": descriptor.id,
            "kind": descriptor.kind,
            "risk_class": descriptor.risk_class,
            "execution_mode": descriptor.execution_mode,
            "requires_confirmation": descriptor.requires_confirmation,
            "description": descriptor.description,
        }

    def _trim_pack(self, pack: ContextPack) -> ContextPack:
        while pack.total_chars() > self._settings.context_char_budget:
            progressed = False
            slot_by_name = {slot.name: slot for slot in pack.active_slots()}
            for slot_name in pack.trim_policy.priority_order:
                slot = slot_by_name.get(slot_name)
                if slot is None:
                    continue
                if self._reduce_slot(slot):
                    progressed = True
                    break
            if not progressed:
                break

        if pack.total_chars() > self._settings.context_char_budget:
            for slot_name in pack.trim_policy.priority_order:
                slot = next((item for item in pack.active_slots() if item.name == slot_name), None)
                if slot is None:
                    continue
                self._preview_truncate_slot(slot, pack.trim_policy.hard_floor_chars)
                if pack.total_chars() <= self._settings.context_char_budget:
                    break
        return pack

    def _reduce_slot(self, slot: ContextSlot) -> bool:
        before = slot.full_chars
        if slot.name == "capability_brief":
            slot.content = self._reduce_capability_brief(slot.content)
        elif slot.name == "recoverable_history":
            slot.content = self._reduce_recoverable_history(slot.content)
        elif slot.name == "scene_slice":
            slot.content = self._shrink_nested(slot.content, max_list_items=2, max_dict_keys=6, max_str_chars=260)
        elif slot.name == "task_focus":
            slot.content = self._shrink_nested(slot.content, max_list_items=3, max_dict_keys=6, max_str_chars=220)
        elif slot.name == "confirmation_loop":
            slot.content = self._shrink_nested(slot.content, max_list_items=2, max_dict_keys=4, max_str_chars=180)
        else:
            return False
        after = slot.full_chars
        if after < before:
            slot.truncated = True
            return True
        return False

    def _reduce_capability_brief(self, content: Any) -> Any:
        if not isinstance(content, list):
            return content
        if len(content) > 6:
            return content[:6]
        reduced: list[dict[str, Any]] = []
        changed = False
        for entry in content:
            if not isinstance(entry, dict):
                reduced.append(entry)
                continue
            description = str(entry.get("description", ""))
            next_entry = dict(entry)
            if len(description) > 96:
                next_entry["description"] = description[:96]
                changed = True
            reduced.append(next_entry)
        return reduced if changed else content

    def _reduce_recoverable_history(self, content: Any) -> Any:
        if not isinstance(content, dict):
            return content
        next_content = dict(content)
        history = dict(next_content.get("history", {})) if isinstance(next_content.get("history"), dict) else {}
        recent_turns = list(history.get("recent_turns", [])) if isinstance(history.get("recent_turns"), list) else []
        memories = list(next_content.get("memories", [])) if isinstance(next_content.get("memories"), list) else []
        if len(recent_turns) > 1:
            history["recent_turns"] = recent_turns[-max(1, len(recent_turns) - 1) :]
            next_content["history"] = history
            return next_content
        if len(memories) > 2:
            next_content["memories"] = memories[:2]
            return next_content
        compact_summary = history.get("session_compact_summary")
        if isinstance(compact_summary, dict) and "text" in compact_summary:
            history["session_compact_summary"] = {
                "path": compact_summary.get("path"),
                "transcript_path": compact_summary.get("transcript_path"),
                "summary_available": True,
            }
            next_content["history"] = history
            return next_content
        recovery_refs = next_content.get("recovery_refs")
        return {
            "session_summary": self._compact_session_summary(next_content.get("session_summary")),
            "recent_dialogue_memory": next_content.get("recent_dialogue_memory"),
            "history": {"current_trigger": history.get("current_trigger"), "recovery_available": True},
            "recovery_refs": recovery_refs,
        }

    def _compact_session_summary(self, session_summary: Any) -> Any:
        if not isinstance(session_summary, dict):
            return session_summary
        compacted = {"summary": session_summary.get("summary")}
        if session_summary.get("transcript_path"):
            compacted["transcript_path"] = session_summary.get("transcript_path")
        metadata = session_summary.get("metadata")
        if isinstance(metadata, dict):
            compacted["metadata"] = {
                key: metadata[key]
                for key in (
                    "topic",
                    "task_id",
                    "status",
                    "next_best_companion_move",
                    "last_dialogue_turn",
                    "recent_dialogue_turn",
                    "active_dialogue_loop",
                    "continuity_hint",
                )
                if key in metadata
            }
        return compacted

    def _preview_truncate_slot(self, slot: ContextSlot, target_chars: int) -> None:
        serialized = json.dumps(slot.content, ensure_ascii=False, default=str)
        if len(serialized) <= target_chars:
            return
        slot.content = {"preview": serialized[:target_chars], "truncated": True, "full_chars": len(serialized)}
        slot.truncated = True

    def _slice_part(self, value: Any) -> Any:
        if isinstance(value, dict):
            keys = list(value.keys())[:8]
            return {key: self._slice_part(value[key]) for key in keys}
        if isinstance(value, list):
            return [self._slice_part(item) for item in value[:4]]
        if isinstance(value, str) and len(value) > 400:
            return {"preview": value[:400], "truncated": True, "full_chars": len(value)}
        return value

    def _slice_recent_events(self, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        return [self._slice_part(item) for item in value[:4]]

    def _shrink_nested(
        self,
        value: Any,
        *,
        max_list_items: int,
        max_dict_keys: int,
        max_str_chars: int,
    ) -> Any:
        if isinstance(value, dict):
            keys = list(value.keys())[:max_dict_keys]
            reduced = {key: self._shrink_nested(value[key], max_list_items=max_list_items, max_dict_keys=max_dict_keys, max_str_chars=max_str_chars) for key in keys}
            if len(value) > len(keys):
                reduced["truncated"] = True
            return reduced
        if isinstance(value, list):
            reduced = [
                self._shrink_nested(item, max_list_items=max_list_items, max_dict_keys=max_dict_keys, max_str_chars=max_str_chars)
                for item in value[:max_list_items]
            ]
            if len(value) > len(reduced):
                reduced.append({"truncated": True, "omitted_items": len(value) - len(reduced)})
            return reduced
        if isinstance(value, str) and len(value) > max_str_chars:
            return {"preview": value[:max_str_chars], "truncated": True, "full_chars": len(value)}
        return value

    def _render_slots(self, slots: list[ContextSlot]) -> str:
        lines: list[str] = []
        for slot in slots:
            if not slot.included:
                continue
            lines.append(f"[{slot.name}]")
            lines.append(json.dumps(slot.content, ensure_ascii=False, indent=2, default=str))
        return "\n\n".join(lines)

    def _slot(
        self,
        name: str,
        role: str,
        source: str,
        strategy: str,
        content: Any,
        *,
        priority: int,
        recoverable: bool = False,
    ) -> ContextSlot:
        return ContextSlot(
            name=name,
            role=role,  # type: ignore[arg-type]
            source=source,
            strategy=strategy,
            content=content,
            priority=priority,
            recoverable=recoverable,
        )

    def _coerce_mapping(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return dict(value)
        return None
