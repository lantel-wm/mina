from __future__ import annotations

from typing import Any

from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeCapability, RuntimeState
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.models import TurnState


class ExecutionManager:
    def __init__(self, capability_registry: CapabilityRegistry, execution_orchestrator: ExecutionOrchestrator) -> None:
        self._capability_registry = capability_registry
        self._execution_orchestrator = execution_orchestrator

    def resolve_capability(self, capabilities: list[RuntimeCapability], capability_id: str) -> RuntimeCapability | None:
        return self._capability_registry.get(capabilities, capability_id)

    def resolve_arguments(
        self,
        turn_state: TurnState,
        capability: RuntimeCapability,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self._execution_orchestrator.resolve_capability_arguments(
            turn_state,
            capability.descriptor.args_schema,
            arguments,
        )

    def execute_internal(
        self,
        capability: RuntimeCapability,
        arguments: dict[str, Any],
        runtime_state: RuntimeState,
    ) -> dict[str, Any]:
        return self._capability_registry.execute_internal(capability, arguments, runtime_state)

    def register_observation(
        self,
        turn_state: TurnState,
        *,
        source: str,
        payload: dict[str, Any],
        kind: str = "observation",
    ):
        return self._execution_orchestrator.register_observation(turn_state, source=source, payload=payload, kind=kind)

    def bridge_action_request(
        self,
        capability: RuntimeCapability,
        arguments: dict[str, Any],
        effect_summary: str | None,
        requires_confirmation: bool,
    ) -> dict[str, Any]:
        return self._capability_registry.bridge_action_request(
            capability,
            arguments,
            effect_summary,
            requires_confirmation,
        )
