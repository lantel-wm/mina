from __future__ import annotations

from fastapi import FastAPI, HTTPException

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.retrieval.index import LocalKnowledgeIndex
from mina_agent.runtime.agent_loop import AgentLoop
from mina_agent.runtime.agent_services import AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.confirmation_resolver import ConfirmationResolver
from mina_agent.runtime.context_manager import ContextManager
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.execution_manager import ExecutionManager
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.schemas import TurnResumeRequest, TurnResponse, TurnStartRequest


def create_app() -> FastAPI:
    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
    settings.audit_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path, settings.data_dir)
    audit = AuditLogger(settings.audit_dir)
    debug = build_debug_recorder(settings)
    policy_engine = PolicyEngine()
    retrieval_index = LocalKnowledgeIndex(store, settings.knowledge_dir)
    retrieval_index.refresh()
    provider = OpenAICompatibleProvider(settings)
    memory_policy = MemoryPolicy()
    task_manager = TaskManager(store)
    context_manager = ContextManager(settings, store, memory_policy)
    deliberation_engine = DeliberationEngine(provider)
    delegate_runtime = DelegateRuntime(store, deliberation_engine)
    capability_registry = CapabilityRegistry(
        settings=settings,
        store=store,
        policy_engine=policy_engine,
        retrieval_index=retrieval_index,
        script_runner=ScriptRunner(settings),
        delegate_runtime=delegate_runtime,
    )
    execution_orchestrator = ExecutionOrchestrator(settings, store)
    execution_manager = ExecutionManager(capability_registry, execution_orchestrator)
    memory_manager = MemoryManager(store, memory_policy)
    services = AgentServices(
        settings=settings,
        store=store,
        audit=audit,
        debug=debug,
        policy_engine=policy_engine,
        capability_registry=capability_registry,
        execution_orchestrator=execution_orchestrator,
        memory_policy=memory_policy,
        confirmation_resolver=ConfirmationResolver(),
        task_manager=task_manager,
        context_manager=context_manager,
        deliberation_engine=deliberation_engine,
        execution_manager=execution_manager,
        memory_manager=memory_manager,
        delegate_runtime=delegate_runtime,
    )
    agent_loop = AgentLoop(services)

    app = FastAPI(title="Mina Agent Service", version="0.1.0")
    app.state.settings = settings
    app.state.agent_loop = agent_loop

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "db_path": str(settings.db_path),
            "knowledge_dir": str(settings.knowledge_dir),
            "provider_configured": provider.available(),
        }

    @app.post("/v1/agent/turns", response_model=TurnResponse)
    async def start_turn(request: TurnStartRequest) -> TurnResponse:
        return agent_loop.start_turn(request)

    @app.post("/v1/agent/turns/{continuation_id}/resume", response_model=TurnResponse)
    async def resume_turn(continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        try:
            return agent_loop.resume_turn(continuation_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
