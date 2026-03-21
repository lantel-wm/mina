from __future__ import annotations

from typing import Any

from mina_agent.memory.store import Store
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import TurnStartRequest


class MemoryManager:
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
            session_ref=request.session_ref,
            task=turn_state.task,
            user_message=request.user_message,
            final_reply=final_reply,
            observations=[observation.context_entry() for observation in turn_state.observations],
            pending_confirmation_resolved=pending_confirmation_resolved,
            artifact_refs=turn_state.task.artifacts,
            status=status,
        )
        for semantic in writes.semantic_writes:
            self._store.add_semantic_memory(
                request.session_ref,
                semantic["memory_type"],
                semantic["memory_key"],
                semantic["value"],
                semantic["summary"],
                confidence=float(semantic.get("confidence", 1.0)),
                metadata=semantic.get("metadata"),
            )
        for episode in writes.episodic_writes:
            self._store.add_episodic_memory(
                request.session_ref,
                episode["summary"],
                tags=list(episode.get("tags", [])),
                task_id=episode.get("task_id"),
                artifact_refs=list(episode.get("artifact_refs", [])),
                metadata=episode.get("metadata"),
            )
        if writes.session_summary is not None:
            existing_summary = self._store.get_session_summary(request.session_ref)
            merged_metadata = dict(existing_summary.get("metadata", {})) if existing_summary is not None else {}
            merged_metadata.update(writes.session_summary)
            preserve_existing_summary = (
                existing_summary is not None
                and (
                    bool(existing_summary.get("transcript_path"))
                    or str(existing_summary.get("summary", "")).startswith("Mina Compact Summary")
                    or bool(existing_summary.get("metadata", {}).get("older_turn_count"))
                )
            )
            self._store.upsert_session_summary(
                request.session_ref,
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
