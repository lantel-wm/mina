from __future__ import annotations

from typing import Any

from mina_agent.memory.store import Store
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import DelegateRequest, DelegateResult, DelegateSummary


class DelegateRuntime:
    def __init__(self, store: Store) -> None:
        self._store = store

    def run(self, request: DelegateRequest, turn_state: TurnState) -> DelegateResult:
        if request.role == "explore":
            return self._run_explore(request, turn_state)
        if request.role == "plan":
            return self._run_plan(request, turn_state)
        return DelegateResult(
            role=request.role,
            objective=request.objective,
            summary=DelegateSummary(summary=f"Unsupported delegate role: {request.role}", stop_reason="unsupported_role"),
        )

    def _run_explore(self, request: DelegateRequest, turn_state: TurnState) -> DelegateResult:
        objective = request.objective or turn_state.task.goal
        artifacts = self._store.search_artifacts(turn_state.session_ref, objective, task_id=turn_state.task.task_id, limit=4)
        memories = self._store.search_memories(turn_state.session_ref, objective, limit=4)
        findings: list[str] = []
        for observation in turn_state.observations[-4:]:
            findings.append(observation.summary)
        for artifact in artifacts[:2]:
            findings.append(artifact["summary"])
        for memory in memories[:2]:
            findings.append(str(memory.get("summary", "")))
        findings = [finding for finding in findings if finding]
        return DelegateResult(
            role="explore",
            objective=objective,
            summary=DelegateSummary(
                summary=f"Explore summary for {objective}: " + ("; ".join(findings[:6]) if findings else "no additional facts found"),
                unresolved_questions=[] if findings else ["Need more live inspection if exact state matters."],
                confidence=0.62 if findings else 0.35,
                stop_reason="completed",
            ),
            task_patch={
                "status": "analyzing",
                "summary": {
                    "delegate": "explore",
                    "objective": objective,
                    "finding_count": len(findings),
                    "next_best_companion_move": "reflect findings back to the player or inspect live state",
                },
                "continuity_score": min(0.9, max(turn_state.task.continuity_score, 0.4)),
            },
            artifact_refs=[
                {
                    "artifact_id": artifact["artifact_id"],
                    "kind": artifact["kind"],
                    "path": artifact["path"],
                    "summary": artifact["summary"],
                }
                for artifact in artifacts
            ],
        )

    def _run_plan(self, request: DelegateRequest, turn_state: TurnState) -> DelegateResult:
        objective = request.objective or turn_state.task.goal
        steps = [
            {"step_key": "inspect", "title": "Inspect live state", "status": "pending", "step_order": 0},
            {"step_key": "guide", "title": "Guide the player with the safest next move", "status": "pending", "step_order": 1},
            {"step_key": "act", "title": "Execute only if still needed", "status": "pending", "step_order": 2},
        ]
        return DelegateResult(
            role="plan",
            objective=objective,
            summary=DelegateSummary(
                summary=f"Plan summary for {objective}: inspect live state, guide first, then execute only if needed.",
                unresolved_questions=["Does the user want advice or direct action?"],
                confidence=0.74,
                stop_reason="completed",
            ),
            task_patch={
                "status": "planned",
                "steps": steps,
                "summary": {
                    "delegate": "plan",
                    "objective": objective,
                    "next_best_step": "Inspect live state",
                    "next_best_companion_move": "explain the plan in plain language before taking over",
                },
                "continuity_score": min(0.95, max(turn_state.task.continuity_score, 0.5)),
            },
        )
