from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mina_agent.memory.store import Store
from mina_agent.providers.openai_compatible import ProviderError
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import DelegateRequest, DelegateResult, DelegateSummary


@dataclass(slots=True)
class DelegateRolePolicy:
    role: str
    description: str
    allow_bridge_actions: bool = False
    allow_nested_delegate: bool = False


class DelegateRuntime:
    _POLICIES = {
        "explore": DelegateRolePolicy(
            role="explore",
            description="Inspect available facts, summarize what is known, and surface what still needs live verification.",
        ),
        "plan": DelegateRolePolicy(
            role="plan",
            description="Produce a concise, companion-first plan that guides the player before any execution.",
        ),
    }

    def __init__(self, store: Store, deliberation_engine: DeliberationEngine | None = None) -> None:
        self._store = store
        self._deliberation_engine = deliberation_engine

    def run(self, request: DelegateRequest, turn_state: TurnState) -> DelegateResult:
        policy = self._POLICIES.get(request.role)
        if policy is None:
            return DelegateResult(
                role=request.role,
                objective=request.objective,
                summary=DelegateSummary(summary=f"Unsupported delegate role: {request.role}", stop_reason="unsupported_role"),
            )

        objective = request.objective or turn_state.task.goal
        artifacts = self._store.search_artifacts(turn_state.session_ref, objective, task_id=turn_state.task.task_id, limit=4)
        memories = self._store.search_memories(turn_state.session_ref, objective, limit=4)
        summary = self._summarize_delegate(policy, objective, turn_state, artifacts, memories, request.context_hints)

        return DelegateResult(
            role=request.role,
            objective=objective,
            summary=summary,
            task_patch=self._task_patch(policy.role, objective, turn_state, summary, artifacts),
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

    def _summarize_delegate(
        self,
        policy: DelegateRolePolicy,
        objective: str,
        turn_state: TurnState,
        artifacts: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        context_hints: list[str],
    ) -> DelegateSummary:
        if self._should_use_local_fallback(policy, turn_state, artifacts, memories, context_hints):
            return self._fallback_summary(policy, objective, turn_state, artifacts, memories)
        if self._deliberation_engine is None:
            return self._fallback_summary(policy, objective, turn_state, artifacts, memories)

        messages = self._delegate_messages(policy, objective, turn_state, artifacts, memories, context_hints)
        try:
            return self._deliberation_engine.summarize_delegate(messages).payload
        except ProviderError:
            return self._fallback_summary(policy, objective, turn_state, artifacts, memories)
        except Exception:
            return self._fallback_summary(policy, objective, turn_state, artifacts, memories)

    def _should_use_local_fallback(
        self,
        policy: DelegateRolePolicy,
        turn_state: TurnState,
        artifacts: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        context_hints: list[str],
    ) -> bool:
        if policy.role != "explore":
            return False
        if artifacts or memories or context_hints:
            return False
        if turn_state.observations:
            return False
        if turn_state.working_memory.key_facts:
            return False
        if turn_state.working_memory.completed_actions:
            return False
        return True

    def _delegate_messages(
        self,
        policy: DelegateRolePolicy,
        objective: str,
        turn_state: TurnState,
        artifacts: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        context_hints: list[str],
    ) -> list[dict[str, str]]:
        system_message = (
            f"You are Mina's isolated {policy.role} subturn.\n"
            f"Role goal: {policy.description}\n"
            "You are not the main turn. You may not call bridge actions. You may not delegate recursively.\n"
            "Use only the provided facts. If live verification is still needed, say so explicitly.\n"
            "Return JSON only matching this schema:\n"
            '{"summary":"...","unresolved_questions":["..."],"confidence":0.0,"stop_reason":"completed"}'
        )
        user_message = {
            "objective": objective,
            "policy": {
                "allow_bridge_actions": policy.allow_bridge_actions,
                "allow_nested_delegate": policy.allow_nested_delegate,
            },
            "task": turn_state.task.context_entry(),
            "working_memory": turn_state.working_memory.context_entry(),
            "recent_observations": [observation.context_entry() for observation in turn_state.observations[-6:]],
            "artifact_refs": [
                {
                    "artifact_id": artifact["artifact_id"],
                    "kind": artifact["kind"],
                    "path": artifact["path"],
                    "summary": artifact["summary"],
                }
                for artifact in artifacts
            ],
            "memory_refs": [
                {
                    "kind": memory.get("kind") or memory.get("memory_type") or "memory",
                    "summary": memory.get("summary") or memory.get("content") or "",
                    "metadata": memory.get("metadata", {}),
                }
                for memory in memories
            ],
            "context_hints": context_hints,
        }
        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": json.dumps(user_message, ensure_ascii=False, indent=2)},
        ]

    def _task_patch(
        self,
        role: str,
        objective: str,
        turn_state: TurnState,
        summary: DelegateSummary,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if role == "explore":
            return {
                "status": "analyzing",
                "summary": {
                    "delegate": "explore",
                    "objective": objective,
                    "finding_count": len(artifacts) + len(turn_state.observations[-6:]),
                    "delegate_summary": summary.summary,
                    "next_best_companion_move": "reflect findings or inspect live state if uncertainty remains",
                },
                "continuity_score": min(0.9, max(turn_state.task.continuity_score, 0.4)),
            }

        steps = [
            {"step_key": "inspect", "title": "Inspect live state", "status": "pending", "step_order": 0},
            {"step_key": "guide", "title": "Guide the player before taking over", "status": "pending", "step_order": 1},
            {"step_key": "act", "title": "Execute only if still needed and allowed", "status": "pending", "step_order": 2},
        ]
        return {
            "status": "planned",
            "steps": steps,
            "summary": {
                "delegate": "plan",
                "objective": objective,
                "delegate_summary": summary.summary,
                "next_best_step": "Inspect live state",
                "next_best_companion_move": "explain the plan in plain language before taking over",
            },
            "continuity_score": min(0.95, max(turn_state.task.continuity_score, 0.5)),
        }

    def _fallback_summary(
        self,
        policy: DelegateRolePolicy,
        objective: str,
        turn_state: TurnState,
        artifacts: list[dict[str, Any]],
        memories: list[dict[str, Any]],
    ) -> DelegateSummary:
        findings: list[str] = []
        findings.extend(observation.summary for observation in turn_state.observations[-4:])
        findings.extend(str(artifact.get("summary", "")) for artifact in artifacts[:2])
        findings.extend(str(memory.get("summary", memory.get("content", ""))) for memory in memories[:2])
        findings = [finding for finding in findings if finding]
        if policy.role == "plan":
            return DelegateSummary(
                summary=f"Plan summary for {objective}: inspect live state, guide first, then execute only if still needed.",
                unresolved_questions=["Does the player want advice or direct action?"],
                confidence=0.68 if findings else 0.52,
                stop_reason="fallback_completed",
            )
        return DelegateSummary(
            summary=f"Explore summary for {objective}: " + ("; ".join(findings[:6]) if findings else "no additional facts found"),
            unresolved_questions=[] if findings else ["Need more live inspection if exact state matters."],
            confidence=0.62 if findings else 0.35,
            stop_reason="fallback_completed",
        )
