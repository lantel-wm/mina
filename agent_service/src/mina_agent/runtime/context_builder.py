from __future__ import annotations

from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.runtime.context_engine import ContextBuildResult, ContextEngine
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TaskState, TurnState, WorkingMemory
from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest


class ContextBuilder:
    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        memory_policy: MemoryPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._memory_policy = memory_policy or MemoryPolicy()

    def build_messages(
        self,
        request: TurnStartRequest,
        recent_turns: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        capability_descriptors: list[CapabilityDescriptor],
        observations: list[dict[str, Any]],
        pending_confirmation: dict[str, Any] | None,
        *,
        turn_state: TurnState | None = None,
    ) -> ContextBuildResult:
        if self._settings is None or self._store is None:
            return self._legacy_build_messages(
                request,
                recent_turns,
                memories,
                capability_descriptors,
                observations,
                pending_confirmation,
            )

        effective_turn_state = turn_state or TurnState(
            session_ref=request.session_ref,
            turn_id=request.turn_id,
            request=request.model_dump(),
            task=TaskState(
                task_id="task_legacy",
                task_type="user_request",
                owner_player=request.player.name,
                goal=request.user_message,
                status="analyzing",
            ),
            working_memory=WorkingMemory(primary_goal=request.user_message),
            pending_confirmation=pending_confirmation,
        )
        return ContextEngine(self._settings, self._store, self._memory_policy).build_messages(
            request=request,
            turn_state=effective_turn_state,
            capability_descriptors=capability_descriptors,
        )

    def _legacy_build_messages(
        self,
        request: TurnStartRequest,
        recent_turns: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        capability_descriptors: list[CapabilityDescriptor],
        observations: list[dict[str, Any]],
        pending_confirmation: dict[str, Any] | None,
    ) -> ContextBuildResult:
        from mina_agent.runtime.models import ReminderBlock  # local import to keep wrapper light

        payload = {
            "stable_core": "You are Mina. Reply in Chinese by default. Observe before acting.",
            "runtime_reminder": {
                "limits": request.limits.model_dump(),
                "pending_confirmation": pending_confirmation,
            },
            "situation_snapshot": request.scoped_snapshot,
            "working_memory": {
                "observations": observations,
            },
            "retrieved_long_term_memory": memories,
            "capability_catalog": [
                {
                    "id": descriptor.id,
                    "kind": descriptor.kind,
                    "risk_class": descriptor.risk_class,
                    "description": descriptor.description,
                }
                for descriptor in capability_descriptors
            ],
            "recent_conversation_trigger": {
                "recent_turns": recent_turns,
                "user_message": request.user_message,
            },
        }
        system_content = payload["stable_core"] + "\n\n" + str(payload["runtime_reminder"])
        user_content = str(
            {
                "situation_snapshot": payload["situation_snapshot"],
                "working_memory": payload["working_memory"],
                "retrieved_long_term_memory": payload["retrieved_long_term_memory"],
                "capability_catalog": payload["capability_catalog"],
                "recent_conversation_trigger": payload["recent_conversation_trigger"],
            }
        )
        return ContextBuildResult(
            messages=[{"role": "system", "content": system_content}, {"role": "user", "content": user_content}],
            sections=[
                ReminderBlock(
                    name=name,
                    role="system" if index < 2 else "user",
                    source="legacy.wrapper",
                    strategy="legacy",
                    content=value,
                    full_chars=len(str(value)),
                ).summary_entry()
                for index, (name, value) in enumerate(payload.items())
            ],
            message_stats={
                "message_count": 2,
                "system_chars": len(system_content),
                "user_chars": len(user_content),
                "total_chars": len(system_content) + len(user_content),
            },
            composition={
                "stable_core": "legacy",
                "runtime_reminder": "legacy",
                "situation_snapshot": "legacy",
                "working_memory": "legacy",
                "retrieved_long_term_memory": "legacy",
                "capability_catalog": "legacy",
                "recent_conversation_trigger": "legacy",
            },
            recovery_refs=[],
            budget_report={"budget": len(system_content) + len(user_content), "used": len(system_content) + len(user_content)},
            active_context_slots=list(payload.keys()),
        )
