from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from mina_agent.runtime.models import ArtifactRef, MemoryCandidate, TaskState


@dataclass(slots=True)
class MemoryWriteResult:
    semantic_writes: list[dict[str, Any]]
    episodic_writes: list[dict[str, Any]]
    session_summary: dict[str, Any] | None = None


class MemoryPolicy:
    _FOLLOW_UP_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*")
    _FOLLOW_UP_PREFIXES = (
        "需要我",
        "需要我帮",
        "要我",
        "要我帮",
        "要不要",
        "要不要我",
        "想让我",
        "还要我",
        "要不要继续",
    )
    _FOLLOW_UP_ENDINGS = ("吗", "呢", "吧")

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
        dialogue_turn = self._build_dialogue_turn(
            task=task,
            user_message=user_message,
            final_reply=final_reply,
            status=status,
        )
        open_follow_up = dialogue_turn.get("open_follow_up")

        player_preference = self._extract_player_preference(user_message, final_reply, pending_confirmation_resolved)
        if player_preference is not None:
            semantic_writes.append(player_preference)

        if task.status in {"completed", "failed", "canceled"}:
            episodic_writes.append(
                {
                    "summary": self._episode_summary(task, user_message, final_reply, status, pending_confirmation_resolved),
                    "tags": [task.task_type, task.status, status],
                    "task_id": task.task_id,
                    "artifact_refs": [artifact.model_dump() for artifact in artifact_refs or []],
                    "metadata": {
                        "goal": task.goal,
                        "recent_dialogue_turn": True,
                        "dialogue_turn": dialogue_turn,
                        "open_follow_up": open_follow_up,
                    },
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

        session_summary = {
            "topic": task.goal,
            "task_id": task.task_id,
            "status": status,
            "next_best_companion_move": task.summary.get("next_best_companion_move") or task.summary.get("next_best_step"),
            "last_user_message": user_message,
            "last_assistant_reply": final_reply,
            "recent_dialogue_turn": dialogue_turn,
            "active_dialogue_loop": open_follow_up,
        }
        if open_follow_up is not None:
            session_summary["continuity_hint"] = (
                "If the next player reply is brief or elliptical, resolve it against Mina's latest follow-up prompt first."
            )
        return MemoryWriteResult(
            semantic_writes=semantic_writes,
            episodic_writes=episodic_writes,
            session_summary=session_summary,
        )

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

    def _extract_player_preference(
        self,
        user_message: str,
        final_reply: str,
        pending_confirmation_resolved: str | None,
    ) -> dict[str, Any] | None:
        normalized = user_message.strip()
        if pending_confirmation_resolved == "rejected":
            return None
        if normalized in {"不要", "不用", "算了", "停下"}:
            return None
        if "规则" in normalized and "?" in normalized:
            return None
        if not any(token in normalized for token in ("以后", "记住", "别再", "不要自动", "先确认")):
            return None
        return {
            "memory_type": "player_preference",
            "memory_key": self._stable_memory_key("pref", normalized),
            "value": normalized,
            "summary": final_reply or normalized,
            "confidence": 0.72,
            "metadata": {"source": "turn_memory_policy"},
        }

    def _stable_memory_key(self, prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{digest}"

    def _build_dialogue_turn(
        self,
        *,
        task: TaskState,
        user_message: str,
        final_reply: str,
        status: str,
    ) -> dict[str, Any]:
        turn = {
            "task_id": task.task_id,
            "task_goal": task.goal,
            "task_status": status,
            "user_message": user_message,
            "assistant_reply": final_reply,
        }
        open_follow_up = self._extract_open_follow_up(final_reply)
        if open_follow_up is not None:
            turn["open_follow_up"] = open_follow_up
        return turn

    def _extract_open_follow_up(self, final_reply: str) -> dict[str, Any] | None:
        text = self._normalize_dialogue_text(final_reply)
        if not text:
            return None
        sentences = [segment.strip() for segment in self._FOLLOW_UP_SPLIT_RE.split(text) if segment.strip()]
        if not sentences:
            sentences = [text]
        candidate = next((sentence for sentence in reversed(sentences) if self._looks_like_open_follow_up(sentence)), None)
        if candidate is None:
            return None
        kind = "offer_help" if any(prefix in candidate for prefix in self._FOLLOW_UP_PREFIXES) else "follow_up_question"
        return {
            "prompt": candidate,
            "kind": kind,
            "expects_brief_reply": True,
        }

    def _looks_like_open_follow_up(self, sentence: str) -> bool:
        normalized = sentence.strip()
        if not normalized:
            return False
        if "?" in normalized or "？" in normalized:
            return True
        if any(prefix in normalized for prefix in self._FOLLOW_UP_PREFIXES):
            return True
        return normalized.endswith(self._FOLLOW_UP_ENDINGS)

    def _normalize_dialogue_text(self, text: str) -> str:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        return " ".join(lines).strip()
