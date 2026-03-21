from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from mina_agent.config import Settings
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyContext, PolicyEngine
from mina_agent.retrieval.index import LocalKnowledgeIndex
from mina_agent.runtime.models import TaskStepState, TurnState
from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest


InternalExecutor = Callable[[dict[str, Any], "RuntimeState"], dict[str, Any]]


@dataclass(slots=True)
class RuntimeCapability:
    descriptor: CapabilityDescriptor
    handler_kind: str
    executor: InternalExecutor | None = None


@dataclass(slots=True)
class RuntimeState:
    request: TurnStartRequest
    turn_state: TurnState
    pending_confirmation: dict[str, Any] | None


class CapabilityRegistry:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        policy_engine: PolicyEngine,
        retrieval_index: LocalKnowledgeIndex,
        script_runner: ScriptRunner,
    ) -> None:
        self._settings = settings
        self._store = store
        self._policy_engine = policy_engine
        self._retrieval_index = retrieval_index
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
        return {
            "continuation_id": "",
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
            "retrieval.local_knowledge.search": RuntimeCapability(
                descriptor=CapabilityDescriptor(
                    id="retrieval.local_knowledge.search",
                    kind="retrieval",
                    visibility_predicate="read_only_plus",
                    risk_class="read_only",
                    execution_mode="internal",
                    requires_confirmation=False,
                    budget_cost=1,
                    args_schema={"query": "string"},
                    result_schema={"results": "array"},
                    description="Search Mina's local knowledge directory and return the most relevant chunks.",
                ),
                handler_kind="internal",
                executor=self._knowledge_search,
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
        artifacts = self._store.search_artifacts(
            state.request.session_ref,
            objective,
            task_id=state.turn_state.task.task_id,
            limit=4,
        )
        memories = self._store.search_memories(state.request.session_ref, objective, limit=4)
        findings: list[str] = []
        for observation in state.turn_state.observations[-4:]:
            findings.append(observation.summary)
        for artifact in artifacts[:2]:
            findings.append(artifact["summary"])
        for memory in memories[:2]:
            findings.append(str(memory.get("summary", "")))
        findings = [finding for finding in findings if finding]
        summary = (
            f"Explore summary for {objective}: "
            + ("; ".join(findings[:6]) if findings else "no additional facts found")
        )
        return {
            "summary": summary,
            "artifact_refs": [
                {
                    "artifact_id": artifact["artifact_id"],
                    "kind": artifact["kind"],
                    "path": artifact["path"],
                    "summary": artifact["summary"],
                }
                for artifact in artifacts
            ],
            "task_patch": {
                "status": "analyzing",
                "summary": {
                    "delegate": "explore",
                    "objective": objective,
                    "finding_count": len(findings),
                },
            },
        }

    def _plan_delegate(self, arguments: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
        objective = str(arguments.get("objective", "")).strip() or state.turn_state.task.goal
        steps = [
            TaskStepState(step_key="inspect", title="Inspect live state", status="pending", step_order=0),
            TaskStepState(step_key="decide", title="Decide the safest next move", status="pending", step_order=1),
            TaskStepState(step_key="act", title="Execute or reply", status="pending", step_order=2),
        ]
        summary = f"Plan summary for {objective}: {', '.join(step.title for step in steps)}."
        return {
            "summary": summary,
            "task_patch": {
                "status": "planned",
                "steps": [step.model_dump() for step in steps],
                "summary": {
                    "delegate": "plan",
                    "objective": objective,
                    "next_best_step": steps[0].title if steps else "",
                },
            },
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
                    "args_schema": capability.descriptor.args_schema,
                    "result_schema": capability.descriptor.result_schema,
                }
                for capability in capabilities
            ]
        }

    def _knowledge_search(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        results = self._retrieval_index.search(query, limit=self._settings.max_retrieval_results)
        return {"results": results, "result_count": len(results)}

    def _run_script(self, arguments: dict[str, Any], _: RuntimeState) -> dict[str, Any]:
        script = str(arguments.get("script", ""))
        inputs = arguments.get("inputs", {})
        return self._script_runner.execute(script, inputs if isinstance(inputs, dict) else {})
