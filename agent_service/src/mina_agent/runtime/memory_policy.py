from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mina_agent.runtime.models import ArtifactRef, MemoryCandidate, TaskState


@dataclass(slots=True)
class MemoryWriteResult:
    semantic_writes: list[dict[str, Any]]
    episodic_writes: list[dict[str, Any]]


class MemoryPolicy:
    def derive_writes(
        self,
        *,
        session_ref: str,
        task: TaskState,
        user_message: str,
        final_reply: str,
        observations: list[dict[str, Any]],
        pending_confirmation_resolved: str | None = None,
        artifact_refs: list[ArtifactRef] | None = None,
        status: str = "completed",
    ) -> MemoryWriteResult:
        semantic_writes: list[dict[str, Any]] = []
        episodic_writes: list[dict[str, Any]] = []

        if task.status in {"completed", "failed", "canceled"}:
            episodic_writes.append(
                {
                    "summary": self._episode_summary(task, user_message, final_reply, status, pending_confirmation_resolved),
                    "tags": [task.task_type, task.status, status],
                    "task_id": task.task_id,
                    "artifact_refs": [artifact.model_dump() for artifact in artifact_refs or []],
                    "metadata": {"goal": task.goal},
                }
            )

        if pending_confirmation_resolved is not None:
            episodic_writes.append(
                {
                    "summary": f"User {pending_confirmation_resolved} a pending plan for task {task.task_id}: {task.goal}",
                    "tags": ["confirmation", pending_confirmation_resolved, task.task_type],
                    "task_id": task.task_id,
                    "artifact_refs": [artifact.model_dump() for artifact in artifact_refs or []],
                    "metadata": {"goal": task.goal},
                }
            )

        return MemoryWriteResult(semantic_writes=semantic_writes, episodic_writes=episodic_writes)

    def summarize_for_context(self, memories: list[dict[str, Any]]) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        for memory in memories:
            summary = memory.get("summary") or memory.get("content") or ""
            artifact_refs: list[ArtifactRef] = []
            for artifact in memory.get("artifact_refs", []):
                try:
                    artifact_refs.append(ArtifactRef.model_validate(artifact))
                except Exception:
                    continue
            candidates.append(
                MemoryCandidate(
                    memory_kind=str(memory.get("kind") or memory.get("memory_type") or "memory"),
                    summary=str(summary),
                    score=memory.get("score"),
                    metadata=memory.get("metadata", {}),
                    artifact_refs=artifact_refs,
                )
            )
        return candidates

    def _episode_summary(
        self,
        task: TaskState,
        user_message: str,
        final_reply: str,
        status: str,
        pending_confirmation_resolved: str | None,
    ) -> str:
        if pending_confirmation_resolved == "confirmed":
            return f"User confirmed the plan for {task.goal} and Mina continued execution."
        if pending_confirmation_resolved == "rejected":
            return f"User rejected the pending plan for {task.goal}."
        if pending_confirmation_resolved == "modified":
            return f"User changed the pending plan for {task.goal}: {user_message}"
        return f"Task {task.goal} finished with status {status}. Reply summary: {final_reply}"
