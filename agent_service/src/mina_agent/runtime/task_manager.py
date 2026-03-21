from __future__ import annotations

import re
from typing import Any

from mina_agent.memory.store import Store
from mina_agent.runtime.models import ArtifactRef, TaskState, TaskStepState, TurnState
from mina_agent.schemas import ModelDecision, TurnStartRequest


class TaskManager:
    def __init__(self, store: Store) -> None:
        self._store = store

    def prepare_task(self, request: TurnStartRequest, pending_confirmation: dict[str, Any] | None) -> TaskState:
        if pending_confirmation is not None and pending_confirmation.get("task_id"):
            existing = self._store.get_task(str(pending_confirmation["task_id"]))
            if existing is not None:
                return self.task_state_from_record(existing)

        task_record = self._store.create_task(
            request.session_ref,
            request.player.name,
            request.user_message,
            task_type="conversation_thread",
            status="analyzing",
            priority="normal",
            risk_class="read_only",
            origin_turn_id=request.turn_id,
            last_active_at=request.turn_id,
            summary={
                "created_from": "turn_start",
                "player_intent": request.user_message,
                "mina_stance": "companionship_first",
                "next_best_companion_move": "understand the situation before acting",
            },
        )
        return self.task_state_from_record(task_record)

    def load_active_task_candidate(
        self,
        request: TurnStartRequest,
        pending_confirmation: dict[str, Any] | None,
    ) -> TaskState | None:
        if pending_confirmation is not None:
            return None
        active_task = self._store.get_active_task(request.session_ref)
        if active_task is None:
            return None
        candidate = self.task_state_from_record(active_task)
        candidate.continuity_score = self._continuity_score(request.user_message, candidate)
        return candidate

    def task_state_from_record(self, record: dict[str, Any]) -> TaskState:
        artifacts = [ArtifactRef.model_validate(artifact) for artifact in record.get("artifacts", [])]
        allowed_step_fields = set(TaskStepState.model_fields.keys())
        steps = [
            TaskStepState.model_validate({key: value for key, value in step.items() if key in allowed_step_fields})
            for step in self._store.list_task_steps(record["task_id"])
        ]
        return TaskState(
            task_id=record["task_id"],
            task_type=record["task_type"],
            owner_player=record["owner_player"],
            goal=record["goal"],
            status=record["status"],
            priority=record["priority"],
            risk_class=record["risk_class"],
            requires_confirmation=record["requires_confirmation"],
            constraints=record.get("constraints", []),
            artifacts=artifacts,
            steps=steps,
            summary=record.get("summary", {}),
            parent_task_id=record.get("parent_task_id"),
            origin_turn_id=record.get("origin_turn_id"),
            continuity_score=float(record.get("continuity_score", 0.0) or 0.0),
            last_active_at=record.get("last_active_at") or record.get("updated_at"),
            created_at=record.get("created_at"),
            updated_at=record.get("updated_at"),
        )

    def sync_task(self, task: TaskState) -> None:
        self._store.update_task(
            task.task_id,
            goal=task.goal,
            status=task.status,
            priority=task.priority,
            risk_class=task.risk_class,
            requires_confirmation=task.requires_confirmation,
            parent_task_id=task.parent_task_id,
            origin_turn_id=task.origin_turn_id,
            continuity_score=task.continuity_score,
            last_active_at=task.last_active_at or task.updated_at,
            constraints=task.constraints,
            artifacts=[artifact.model_dump() for artifact in task.artifacts],
            summary=task.summary,
        )
        task.steps = [
            TaskStepState.model_validate(step)
            for step in self._store.replace_task_steps(task.task_id, [step.model_dump() for step in task.steps])
        ]

    def apply_task_patch(self, turn_state: TurnState, patch: dict[str, Any] | None) -> None:
        if not isinstance(patch, dict):
            return
        status = patch.get("status")
        if isinstance(status, str):
            turn_state.task.status = status  # type: ignore[assignment]
        task_type = patch.get("task_type")
        if isinstance(task_type, str):
            turn_state.task.task_type = task_type
        steps = patch.get("steps")
        if isinstance(steps, list):
            turn_state.task.steps = [TaskStepState.model_validate(step) for step in steps]
        summary = patch.get("summary")
        if isinstance(summary, dict):
            turn_state.task.summary.update(summary)
            next_step = summary.get("next_best_step") or summary.get("next_best_companion_move")
            if isinstance(next_step, str):
                turn_state.working_memory.next_best_step = next_step
        continuity_score = patch.get("continuity_score")
        if isinstance(continuity_score, (int, float)):
            turn_state.task.continuity_score = float(continuity_score)

    def apply_task_selection(self, turn_id: str, turn_state: TurnState, decision: ModelDecision) -> dict[str, Any] | None:
        selection = getattr(decision, "task_selection", None)
        if selection != "reuse_active":
            if selection == "keep_current":
                turn_state.active_task_candidate = None
            return None

        candidate = turn_state.active_task_candidate
        if candidate is None or candidate.task_id == turn_state.task.task_id:
            turn_state.active_task_candidate = None
            return None

        provisional_task = turn_state.task
        provisional_summary = dict(provisional_task.summary)
        provisional_summary.update(
            {
                "superseded_by_task_id": candidate.task_id,
                "superseded_reason": "model_selected_active_task",
            }
        )
        self._store.update_task(
            provisional_task.task_id,
            status="canceled",
            summary=provisional_summary,
        )

        turn_state.task = candidate.model_copy(deep=True)
        turn_state.active_task_candidate = None
        turn_state.working_memory.primary_goal = turn_state.task.goal
        turn_state.working_memory.focus = turn_state.task.goal
        turn_state.working_memory.current_status = turn_state.task.status
        next_best_step = str(
            turn_state.task.summary.get("next_best_step", turn_state.task.summary.get("next_best_companion_move", ""))
        ).strip()
        if next_best_step:
            turn_state.working_memory.next_best_step = next_best_step
        return {
            "selection": "reuse_active",
            "task_id": turn_state.task.task_id,
            "superseded_task_id": provisional_task.task_id,
        }

    def classify_task_patch(self, turn_state: TurnState, decision: ModelDecision) -> None:
        task_update = decision.task_update
        if task_update:
            self.apply_task_patch(turn_state, task_update)

    def _continuity_score(self, message: str, task: TaskState) -> float:
        tokens = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", message.lower()))
        goal_tokens = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", task.goal.lower()))
        overlap = len(tokens & goal_tokens)
        score = min(overlap * 0.15, 0.45)
        if task.status in {"analyzing", "planned", "in_progress", "blocked", "awaiting_confirmation"}:
            score += 0.2
        return min(score, 1.0)
