from __future__ import annotations

from typing import Any

from mina_agent.memory.store import Store
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import TurnStartRequest


class MemoryManager:
    _RECENT_DIALOGUE_TURNS = 4

    def __init__(self, store: Store, memory_policy: MemoryPolicy) -> None:
        self._store = store
        self._memory_policy = memory_policy

    def record_turn_memories(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        *,
        final_reply: str,
        status: str,
        pending_confirmation_resolved: str | None = None,
    ) -> None:
        writes = self._memory_policy.derive_writes(
            thread_id=request.thread_id,
            task=turn_state.task,
            user_message=request.user_message,
            final_reply=final_reply,
            observations=[observation.context_entry() for observation in turn_state.observations],
            pending_confirmation_resolved=pending_confirmation_resolved,
            artifact_refs=turn_state.task.artifacts,
            status=status,
        )
        for semantic in writes.semantic_writes:
            self._store.add_thread_semantic_memory(
                request.thread_id,
                semantic["memory_type"],
                semantic["memory_key"],
                semantic["value"],
                semantic["summary"],
                confidence=float(semantic.get("confidence", 1.0)),
                metadata=semantic.get("metadata"),
            )
        for episode in writes.episodic_writes:
            self._store.add_thread_episodic_memory(
                request.thread_id,
                episode["summary"],
                tags=list(episode.get("tags", [])),
                task_id=episode.get("task_id"),
                artifact_refs=list(episode.get("artifact_refs", [])),
                metadata=episode.get("metadata"),
            )
        if writes.session_summary is not None:
            existing_summary = self._store.get_thread_summary(request.thread_id)
            existing_metadata = dict(existing_summary.get("metadata", {})) if existing_summary is not None else {}
            merged_metadata = dict(existing_metadata)
            merged_metadata.update(writes.session_summary)
            recent_dialogue_turn = writes.session_summary.get("recent_dialogue_turn")
            recent_dialogue_window = self._merge_recent_dialogue_window(existing_metadata, recent_dialogue_turn)
            if recent_dialogue_window:
                merged_metadata["recent_dialogue_window"] = recent_dialogue_window
                merged_metadata["last_dialogue_turn"] = recent_dialogue_window[-1]
            previous_loop = existing_metadata.get("active_dialogue_loop")
            if isinstance(previous_loop, dict) and request.user_message.strip():
                merged_metadata["last_dialogue_resolution"] = {
                    "assistant_prompt": previous_loop.get("prompt"),
                    "player_reply": request.user_message.strip(),
                }
            if merged_metadata.get("active_dialogue_loop") is None:
                merged_metadata.pop("active_dialogue_loop", None)
                merged_metadata.pop("continuity_hint", None)
            preserve_existing_summary = (
                existing_summary is not None
                and (
                    bool(existing_summary.get("transcript_path"))
                    or str(existing_summary.get("summary", "")).startswith("Mina Compact Summary")
                    or bool(existing_summary.get("metadata", {}).get("older_turn_count"))
                )
            )
            self._store.upsert_thread_summary(
                request.thread_id,
                (
                    existing_summary["summary"]
                    if preserve_existing_summary and existing_summary is not None
                    else writes.session_summary["topic"]
                ),
                transcript_path=existing_summary.get("transcript_path") if existing_summary is not None else None,
                metadata=merged_metadata,
            )

    def session_memory_snapshot(self, request: TurnStartRequest, turn_state: TurnState) -> dict[str, Any]:
        return {
            "topic": request.user_message,
            "task_id": turn_state.task.task_id,
            "current_status": turn_state.task.status,
            "next_best_companion_move": turn_state.task.summary.get("next_best_companion_move"),
        }

    def _merge_recent_dialogue_window(
        self,
        existing_metadata: dict[str, Any],
        recent_dialogue_turn: Any,
    ) -> list[dict[str, Any]]:
        existing_window = existing_metadata.get("recent_dialogue_window")
        window = [entry for entry in existing_window if isinstance(entry, dict)] if isinstance(existing_window, list) else []
        if isinstance(recent_dialogue_turn, dict):
            window.append(recent_dialogue_turn)
        return window[-self._RECENT_DIALOGUE_TURNS :]
