from __future__ import annotations

from dataclasses import dataclass

from mina_agent.protocol import ExternalToolSpec
from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeCapability
from mina_agent.schemas import (
    LimitsPayload,
    PlayerPayload,
    ServerEnvPayload,
    CompanionTriggerPayload,
    TurnStartRequest,
    VisibleCapabilityPayload,
)


@dataclass(slots=True)
class ResolvedTools:
    legacy_request: TurnStartRequest
    capabilities: list[RuntimeCapability]


class MinaToolRegistry:
    def __init__(self, capability_registry: CapabilityRegistry) -> None:
        self._capability_registry = capability_registry

    def resolve_tools(
        self,
        *,
        thread_id: str,
        turn_id: str,
        user_message: str,
        player: PlayerPayload,
        server_env: ServerEnvPayload,
        scoped_snapshot: dict[str, object],
        tool_specs: list[ExternalToolSpec],
        limits: LimitsPayload,
        companion_trigger: CompanionTriggerPayload | None = None,
    ) -> ResolvedTools:
        legacy_request = TurnStartRequest(
            thread_id=thread_id,
            turn_id=turn_id,
            player=player,
            server_env=server_env,
            scoped_snapshot=dict(scoped_snapshot),
            visible_capabilities=(
                []
                if companion_trigger is not None and companion_trigger.mode == "proactive_companion"
                else [self._to_visible_tool(spec) for spec in tool_specs]
            ),
            limits=limits,
            companion_trigger=companion_trigger,
            user_message=user_message,
        )
        capabilities = (
            []
            if companion_trigger is not None and companion_trigger.mode == "proactive_companion"
            else self._capability_registry.resolve(legacy_request)
        )
        return ResolvedTools(
            legacy_request=legacy_request,
            capabilities=capabilities,
        )

    def _to_visible_tool(self, spec: ExternalToolSpec) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id=spec.id,
            kind=spec.kind,
            description=spec.description,
            risk_class=spec.risk_class,
            execution_mode=spec.execution_mode,
            requires_confirmation=spec.requires_confirmation,
            args_schema=dict(spec.input_schema),
            result_schema=dict(spec.output_schema),
            domain=spec.domain,
            preferred=spec.preferred,
            semantic_level=spec.semantic_level,  # type: ignore[arg-type]
            freshness_hint=spec.freshness,  # type: ignore[arg-type]
        )
