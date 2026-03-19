from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from mina_agent.config import Settings
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.knowledge.service import KnowledgeService
from mina_agent.policy.policy_engine import PolicyContext, PolicyEngine
from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest, VisibleCapabilityPayload


InternalExecutor = Callable[[dict[str, Any], "RuntimeState"], dict[str, Any]]


@dataclass(slots=True)
class RuntimeCapability:
    descriptor: CapabilityDescriptor
    handler_kind: str
    executor: InternalExecutor | None = None


@dataclass(slots=True)
class RuntimeState:
    request: TurnStartRequest
    local_observations: list[dict[str, Any]]
    pending_confirmation: dict[str, Any] | None


class CapabilityRegistry:
    def __init__(
        self,
        settings: Settings,
        policy_engine: PolicyEngine,
        knowledge_service: KnowledgeService,
        script_runner: ScriptRunner,
    ) -> None:
        self._settings = settings
        self._policy_engine = policy_engine
        self._knowledge_service = knowledge_service
        self._script_runner = script_runner
        self._local_capabilities = self._build_local_capabilities()

    def resolve(self, request: TurnStartRequest) -> list[RuntimeCapability]:
        context = PolicyContext(
            role=request.player.role,
            carpet_loaded=request.server_env.carpet_loaded,
            experimental_enabled=request.server_env.experimental_enabled,
            dynamic_scripting_enabled=request.server_env.dynamic_scripting_enabled,
        )

        capabilities: list[RuntimeCapability] = []
        for bridge_capability in request.visible_capabilities:
            descriptor = CapabilityDescriptor(
                id=bridge_capability.id,
                kind=bridge_capability.kind,  # type: ignore[arg-type]
                visibility_predicate="always",
                risk_class=bridge_capability.risk_class,
                execution_mode=bridge_capability.execution_mode,
                requires_confirmation=bridge_capability.requires_confirmation,
                budget_cost=1,
                args_schema=dict(bridge_capability.args_schema),
                result_schema=dict(bridge_capability.result_schema),
                description=bridge_capability.description,
            )
            capabilities.append(RuntimeCapability(descriptor=descriptor, handler_kind="bridge"))

        for capability in self._local_capabilities.values():
            if self._policy_engine.descriptor_visible(context, capability.descriptor.visibility_predicate):
                capabilities.append(capability)

        capabilities.sort(key=lambda item: (item.handler_kind, item.descriptor.id))
        return capabilities

    def get(self, capabilities: list[RuntimeCapability], capability_id: str) -> RuntimeCapability | None:
        for capability in capabilities:
            if capability.descriptor.id == capability_id:
                return capability
        return None

    def execute_internal(self, capability: RuntimeCapability, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        if capability.executor is None:
            raise RuntimeError(f"Capability {capability.descriptor.id} is not an internal capability.")
        return capability.executor(arguments, state)

    def bridge_action_request(
        self,
        capability: RuntimeCapability,
        arguments: dict[str, Any],
        effect_summary: str | None,
        requires_confirmation: bool,
    ) -> dict[str, Any]:
        continuation_id = ""
        return {
            "continuation_id": continuation_id,
            "intent_id": str(uuid.uuid4()),
            "capability_id": capability.descriptor.id,
            "risk_class": capability.descriptor.risk_class,
            "effect_summary": effect_summary or capability.descriptor.description,
            "preconditions": [],
            "arguments": arguments,
            "requires_confirmation": requires_confirmation or capability.descriptor.requires_confirmation,
        }

    def _build_local_capabilities(self) -> dict[str, RuntimeCapability]:
        return {
            "skill.mina_capability_guide": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="skill.mina_capability_guide",
                    kind="skill",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={},
                    result_schema={"summary": "string"},
                    description="Summarize Mina's currently visible capability surface and operating limits.",
                ),
                handler_kind="internal",
                executor=self._capability_guide,
            ),
            "retrieval.minecraft_facts.lookup": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="retrieval.minecraft_facts.lookup",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"query": "string", "domain_hint": "string", "subject_hint": "string"},
                    result_schema={"results": "array", "source_categories": "array"},
                    description="Look up authoritative Minecraft facts from SQLite, including recipes, loot tables, tags, commands, registries, block states, and local server rules.",
                ),
                handler_kind="internal",
                executor=self._facts_lookup,
            ),
            "retrieval.minecraft_semantics.search": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="retrieval.minecraft_semantics.search",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"query": "string"},
                    result_schema={"results": "array", "verification_required": "boolean"},
                    description="Search explanatory text in SQLite FTS, including wiki notes, changelogs, and local server guidance. Hard facts still need fact lookup verification.",
                ),
                handler_kind="internal",
                executor=self._semantic_search,
            ),
            "script.python_sandbox.execute": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="script.python_sandbox.execute",
                    kind="script",
                    visibility_predicate="dynamic_scripting_enabled",
                    risk_class="experimental_privileged",
                    execution_mode="sandboxed_subprocess",
                    requires_confirmation=True,
                    budget_cost=4,
                    args_schema={"script": "string", "inputs": "object"},
                    result_schema={"actions": "array"},
                    description="Execute a budgeted Python script in a sandboxed subprocess. Disabled by default.",
                ),
                handler_kind="internal",
                executor=self._run_script,
            ),
        }

    def _capability_guide(self, _: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        capabilities = self.resolve(state.request)
        return {
            "summary": [
                {
                    "id": capability.descriptor.id,
                    "kind": capability.descriptor.kind,
                    "risk_class": capability.descriptor.risk_class,
                    "description": capability.descriptor.description,
                }
                for capability in capabilities
            ]
        }

    def _facts_lookup(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        domain_hint = str(arguments.get("domain_hint", "")).strip() or None
        subject_hint = str(arguments.get("subject_hint", "")).strip() or None
        return self._knowledge_service.lookup_facts(query, domain_hint=domain_hint, subject_hint=subject_hint)

    def _semantic_search(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        return self._knowledge_service.search_semantics(query)

    def _run_script(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        script = str(arguments.get("script", ""))
        inputs = arguments.get("inputs", {})
        return self._script_runner.execute(script, inputs if isinstance(inputs, dict) else {})
