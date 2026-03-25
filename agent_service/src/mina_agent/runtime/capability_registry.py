from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from mina_agent.config import Settings
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyContext, PolicyEngine
from mina_agent.retrieval.wiki_store import WikiKnowledgeStore
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.models import TaskStepState, TurnState
from mina_agent.runtime.semantic_tools import SEMANTIC_BRIDGE_PROXY_SPECS, SemanticBridgeProxySpec
from mina_agent.schemas import CapabilityDescriptor, DelegateRequest, TurnStartRequest


InternalExecutor = Callable[[dict[str, Any], "RuntimeState"], dict[str, Any]]


@dataclass(slots=True)
class RuntimeCapability:
    descriptor: CapabilityDescriptor
    handler_kind: str
    executor: InternalExecutor | None = None
    bridge_target_id: str | None = None


@dataclass(slots=True)
class RuntimeState:
    request: TurnStartRequest
    turn_state: TurnState
    pending_confirmation: dict[str, Any] | None


class CapabilityRegistry:
    _CAPABILITY_ALIASES = {
        "entity.scan_nearby": "game.nearby_entities.read",
        "entity.nearby.read": "game.nearby_entities.read",
        "nearby_entities.read": "game.nearby_entities.read",
        "nearby.entities.read": "game.nearby_entities.read",
        "game.nearby_entities.scan": "game.nearby_entities.read",
        "minecraft.entity.scan": "game.nearby_entities.read",
        "minecraft.entities.scan": "game.nearby_entities.read",
        "minecraft.entity.nearby.scan": "game.nearby_entities.read",
    }

    def __init__(
        self,
        settings: Settings,
        store: Store,
        policy_engine: PolicyEngine,
        wiki_store: WikiKnowledgeStore,
        script_runner: ScriptRunner,
        delegate_runtime: DelegateRuntime | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._policy_engine = policy_engine
        self._wiki_store = wiki_store
        self._script_runner = script_runner
        self._delegate_runtime = delegate_runtime or DelegateRuntime(store)
        self._local_capabilities = self._build_local_capabilities()

    def resolve(self, request: TurnStartRequest) -> list[RuntimeCapability]:
        context = PolicyContext(
            role=request.player.role,
            carpet_loaded=request.server_env.carpet_loaded,
            experimental_enabled=request.server_env.experimental_enabled,
            dynamic_scripting_enabled=request.server_env.dynamic_scripting_enabled,
        )

        bridge_capabilities: list[RuntimeCapability] = []
        visible_bridge_ids: set[str] = set()
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
                domain=bridge_capability.domain,
                preferred=bridge_capability.preferred,
                semantic_level=bridge_capability.semantic_level,
                freshness_hint=bridge_capability.freshness_hint,
            )
            visible_bridge_ids.add(descriptor.id)
            bridge_capabilities.append(RuntimeCapability(descriptor=descriptor, handler_kind="bridge"))

        capabilities: list[RuntimeCapability] = self._build_bridge_proxy_capabilities(visible_bridge_ids)
        for capability in self._local_capabilities.values():
            if self._policy_engine.descriptor_visible(context, capability.descriptor.visibility_predicate):
                capabilities.append(capability)
        capabilities.extend(bridge_capabilities)

        capabilities.sort(key=self._sort_key)
        return capabilities

    def get(self, capabilities: list[RuntimeCapability], capability_id: str) -> RuntimeCapability | None:
        normalized_id = str(capability_id or "").strip()
        if not normalized_id:
            return None
        alias_id = self._CAPABILITY_ALIASES.get(normalized_id)
        for capability in capabilities:
            if capability.descriptor.id == normalized_id:
                return capability
            if alias_id is not None and capability.descriptor.id == alias_id:
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
        bridge_capability_id = capability.bridge_target_id or capability.descriptor.id
        return {
            "continuation_id": "",
            "intent_id": str(uuid.uuid4()),
            "capability_id": bridge_capability_id,
            "risk_class": capability.descriptor.risk_class,
            "effect_summary": effect_summary or capability.descriptor.description,
            "preconditions": [],
            "arguments": arguments,
            "requires_confirmation": requires_confirmation or capability.descriptor.requires_confirmation,
        }

    def _sort_key(self, capability: RuntimeCapability) -> tuple[int, int, str]:
        handler_priority = {"bridge_proxy": 0, "bridge": 1, "internal": 2}
        return (
            handler_priority.get(capability.handler_kind, 9),
            0 if capability.descriptor.preferred else 1,
            capability.descriptor.id,
        )

    def _build_bridge_proxy_capabilities(self, visible_bridge_ids: set[str]) -> list[RuntimeCapability]:
        capabilities: list[RuntimeCapability] = []
        for spec in SEMANTIC_BRIDGE_PROXY_SPECS:
            if spec.bridge_target_id not in visible_bridge_ids:
                continue
            capabilities.append(
                RuntimeCapability(
                    descriptor=CapabilityDescriptor(
                        id=spec.capability_id,
                        kind="tool",
                        visibility_predicate="always",
                        risk_class="read_only",
                        execution_mode="bridge_proxy",
                        requires_confirmation=False,
                        budget_cost=1,
                        args_schema=dict(spec.args_schema),
                        result_schema=dict(spec.result_schema),
                        description=spec.description,
                        domain=spec.domain,
                        preferred=True,
                        semantic_level="semantic",
                        freshness_hint=spec.freshness_hint,  # type: ignore[arg-type]
                    ),
                    handler_kind="bridge_proxy",
                    executor=self._bridge_proxy_executor(spec),
                    bridge_target_id=spec.bridge_target_id,
                )
            )
        return capabilities

    def _bridge_proxy_executor(self, spec: SemanticBridgeProxySpec) -> InternalExecutor:
        def _execute(arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
            snapshot = state.request.scoped_snapshot if isinstance(state.request.scoped_snapshot, dict) else {}
            if spec.freshness_hint == "ambient":
                ambient_payload = spec.snapshot_reader(snapshot, arguments)
                if isinstance(ambient_payload, dict) and ambient_payload:
                    return {
                        "_proxy_mode": "observation",
                        "payload": ambient_payload,
                        "bridge_target_id": spec.bridge_target_id,
                    }
            return {
                "_proxy_mode": "bridge",
                "bridge_target_id": spec.bridge_target_id,
                "arguments": dict(arguments),
                "effect_summary": spec.description,
            }

        return _execute

    def _build_local_capabilities(self) -> dict[str, RuntimeCapability]:
        return {
            "artifact.read": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="artifact.read",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"artifact_id": "string"},
                    result_schema={"artifact": "object", "content": "string"},
                    description="Read a previously offloaded artifact by artifact_id.",
                ),
                handler_kind="internal",
                executor=self._artifact_read,
            ),
            "artifact.search": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="artifact.search",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"query": "string"},
                    result_schema={"results": "array"},
                    description="Search offloaded task and session artifacts.",
                ),
                handler_kind="internal",
                executor=self._artifact_search,
            ),
            "memory.search": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="memory.search",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"query": "string"},
                    result_schema={"results": "array"},
                    description="Search semantic and episodic memory for relevant facts.",
                ),
                handler_kind="internal",
                executor=self._memory_search,
            ),
            "task.inspect": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="task.inspect",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={},
                    result_schema={"task": "object"},
                    description="Inspect the current task state, summary, and subtasks.",
                ),
                handler_kind="internal",
                executor=self._task_inspect,
            ),
            "agent.explore.delegate": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="agent.explore.delegate",
                    kind="agent",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=2,
                    args_schema={"objective": "string"},
                    result_schema={"summary": "string", "artifact_refs": "array", "task_patch": "object"},
                    description="Run an isolated read-only exploration pass and return only a compact summary.",
                    preferred=True,
                ),
                handler_kind="internal",
                executor=self._explore_delegate,
            ),
            "agent.plan.delegate": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="agent.plan.delegate",
                    kind="agent",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=2,
                    args_schema={"objective": "string"},
                    result_schema={"summary": "string", "task_patch": "object"},
                    description="Run an isolated planning pass and return a task patch without direct execution.",
                    preferred=True,
                ),
                handler_kind="internal",
                executor=self._plan_delegate,
            ),
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
                    description="Summarize Mina's visible capability surface, including schemas on demand.",
                ),
                handler_kind="internal",
                executor=self._capability_guide,
            ),
            "wiki.page.get": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.page.get",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"title": "string"},
                    result_schema={"page": "object", "sections": "array", "section_titles": "array"},
                    description="Resolve a Minecraft Wiki page by title or redirect and return a compact structured bundle.",
                    preferred=True,
                ),
                handler_kind="internal",
                executor=self._wiki_page_get,
            ),
            "wiki.category.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.category.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"category": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages by category and return compact page cards.",
                    preferred=True,
                ),
                handler_kind="internal",
                executor=self._wiki_category_find,
            ),
            "wiki.template.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.template.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"template_name": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages by template and return compact page cards.",
                ),
                handler_kind="internal",
                executor=self._wiki_template_find,
            ),
            "wiki.template_param.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.template_param.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"template_name": "string", "param_name": "string", "param_value": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages by template parameter and return compact page cards.",
                ),
                handler_kind="internal",
                executor=self._wiki_template_param_find,
            ),
            "wiki.infobox.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.infobox.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"key": "string", "value": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages by infobox key or key/value and return compact page cards.",
                ),
                handler_kind="internal",
                executor=self._wiki_infobox_find,
            ),
            "wiki.backlinks.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.backlinks.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"title": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages that link to a target page and return compact page cards.",
                ),
                handler_kind="internal",
                executor=self._wiki_backlinks_find,
            ),
            "wiki.section.find": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="wiki.section.find",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"section_title": "string", "limit": "integer"},
                    result_schema={"results": "array"},
                    description="Find Minecraft Wiki pages with a matching section title and return compact section matches.",
                ),
                handler_kind="internal",
                executor=self._wiki_section_find,
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

    def _artifact_read(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        artifact_id = str(arguments.get("artifact_id", "")).strip()
        if not artifact_id:
            return {"found": False, "error": "artifact_id is required"}
        artifact = self._store.get_artifact(artifact_id)
        if artifact is None:
            return {"found": False, "artifact_id": artifact_id}
        max_chars = max(self._settings.artifact_inline_char_budget * 3, 2400)
        content = artifact["content"]
        preview = content if len(content) <= max_chars else content[:max_chars]
        return {
            "found": True,
            "artifact": {key: value for key, value in artifact.items() if key != "content"},
            "content": preview,
            "truncated": len(content) > len(preview),
        }

    def _artifact_search(self, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        task_results = self._store.search_artifacts(
            state.request.session_ref,
            query,
            task_id=state.turn_state.task.task_id,
            limit=self._settings.max_retrieval_results,
        )
        session_results = self._store.search_artifacts(
            state.request.session_ref,
            query,
            task_id=None,
            limit=max(self._settings.max_retrieval_results * 2, self._settings.max_retrieval_results),
        )
        merged_results: list[dict[str, Any]] = []
        seen_artifact_ids: set[str] = set()
        for result in task_results + session_results:
            artifact_id = str(result.get("artifact_id", ""))
            if artifact_id in seen_artifact_ids:
                continue
            seen_artifact_ids.add(artifact_id)
            merged_results.append(result)
            if len(merged_results) >= self._settings.max_retrieval_results:
                break
        return {
            "results": merged_results
        }

    def _memory_search(self, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        return {"results": self._store.search_memories(state.request.session_ref, query, limit=self._settings.max_retrieval_results)}

    def _task_inspect(self, _: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        task = self._store.get_task(state.turn_state.task.task_id) or state.turn_state.task.model_dump()
        task["steps"] = self._store.list_task_steps(state.turn_state.task.task_id)
        return {"task": task}

    def _explore_delegate(self, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        objective = str(arguments.get("objective", "")).strip() or state.turn_state.task.goal
        result = self._delegate_runtime.run(
            DelegateRequest(role="explore", objective=objective),
            state.turn_state,
        )
        return {
            "summary": result.summary.summary,
            "delegate_result": result.model_dump(),
            "artifact_refs": result.artifact_refs,
            "task_patch": result.task_patch,
        }

    def _plan_delegate(self, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        objective = str(arguments.get("objective", "")).strip() or state.turn_state.task.goal
        result = self._delegate_runtime.run(
            DelegateRequest(role="plan", objective=objective),
            state.turn_state,
        )
        return {
            "summary": result.summary.summary,
            "delegate_result": result.model_dump(),
            "task_patch": result.task_patch,
        }

    def _capability_guide(self, _: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        capabilities = self.resolve(state.request)
        return {
            "summary": [
                {
                    "id": capability.descriptor.id,
                    "kind": capability.descriptor.kind,
                    "risk_class": capability.descriptor.risk_class,
                    "domain": capability.descriptor.domain,
                    "preferred": capability.descriptor.preferred,
                    "semantic_level": capability.descriptor.semantic_level,
                    "freshness_hint": capability.descriptor.freshness_hint,
                    "description": capability.descriptor.description,
                    "args_schema": capability.descriptor.args_schema,
                    "result_schema": capability.descriptor.result_schema,
                }
                for capability in capabilities
            ]
        }

    def _wiki_page_get(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        return self._wiki_store.get_page(str(arguments.get("title", "")).strip())

    def _wiki_category_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        return self._wiki_store.find_by_category(
            str(arguments.get("category", "")).strip(),
            self._wiki_limit(arguments),
        )

    def _wiki_template_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        return self._wiki_store.find_by_template(
            str(arguments.get("template_name", "")).strip(),
            self._wiki_limit(arguments),
        )

    def _wiki_template_param_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        raw_param_value = arguments.get("param_value")
        param_value = str(raw_param_value).strip() if raw_param_value not in (None, "") else None
        return self._wiki_store.find_by_template_param(
            str(arguments.get("template_name", "")).strip(),
            str(arguments.get("param_name", "")).strip(),
            param_value,
            self._wiki_limit(arguments),
        )

    def _wiki_infobox_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        raw_value = arguments.get("value")
        value = str(raw_value).strip() if raw_value not in (None, "") else None
        return self._wiki_store.find_by_infobox(
            str(arguments.get("key", "")).strip(),
            value,
            self._wiki_limit(arguments),
        )

    def _wiki_backlinks_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        return self._wiki_store.find_backlinks(
            str(arguments.get("title", "")).strip(),
            self._wiki_limit(arguments),
        )

    def _wiki_section_find(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        return self._wiki_store.find_sections(
            str(arguments.get("section_title", "")).strip(),
            self._wiki_limit(arguments),
        )

    def _wiki_limit(self, arguments: dict[str, Any]) -> int | None:
        value = arguments.get("limit")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    def _run_script(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        script = str(arguments.get("script", ""))
        inputs = arguments.get("inputs", {})
        return self._script_runner.execute(script, inputs if isinstance(inputs, dict) else {})
