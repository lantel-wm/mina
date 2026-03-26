from __future__ import annotations

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from mina_agent.app_server import AppServerConnection, MinaAppServer
from mina_agent.core import MinaCoreEngine, ThreadManager
from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memories import MemoryPipeline
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.retrieval.wiki_store import WikiKnowledgeStore
from mina_agent.runtime.agent_services import AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.execution_manager import ExecutionManager
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.context_manager import ContextManager
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.tools import MinaToolRegistry


def create_app() -> FastAPI:
    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audit_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path, settings.data_dir)
    audit = AuditLogger(settings.audit_dir)
    debug = build_debug_recorder(settings)
    policy_engine = PolicyEngine()
    wiki_store = WikiKnowledgeStore(
        settings.wiki_db_path,
        default_limit=settings.wiki_default_limit,
        max_limit=settings.wiki_max_limit,
        section_excerpt_chars=settings.wiki_section_excerpt_chars,
        plain_text_excerpt_chars=settings.wiki_plain_text_excerpt_chars,
    )
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
        wiki_store=wiki_store,
        script_runner=ScriptRunner(settings),
        delegate_runtime=delegate_runtime,
    )
    execution_orchestrator = ExecutionOrchestrator(settings, store)
    execution_manager = ExecutionManager(capability_registry, execution_orchestrator)
    memory_manager = MemoryManager(store, memory_policy)
    thread_manager = ThreadManager(store, generate_memories=settings.memories_generate)
    tool_registry = MinaToolRegistry(capability_registry)
    memory_pipeline = MemoryPipeline(settings, store, memory_manager)
    services = AgentServices(
        settings=settings,
        store=store,
        audit=audit,
        debug=debug,
        policy_engine=policy_engine,
        capability_registry=capability_registry,
        execution_orchestrator=execution_orchestrator,
        memory_policy=memory_policy,
        task_manager=task_manager,
        context_manager=context_manager,
        deliberation_engine=deliberation_engine,
        execution_manager=execution_manager,
        memory_manager=memory_manager,
        delegate_runtime=delegate_runtime,
    )
    engine = MinaCoreEngine(
        services,
        thread_manager=thread_manager,
        tool_registry=tool_registry,
        memory_pipeline=memory_pipeline,
    )
    app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)

    app = FastAPI(title="Mina Agent Service", version="0.1.0")
    app.state.settings = settings
    app.state.app_server = app_server
    app.state.memory_pipeline = memory_pipeline

    @app.on_event("startup")
    async def kickoff_memory_pipeline() -> None:
        memory_pipeline.kickoff_background_refresh(reason="startup")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "db_path": str(settings.db_path),
            "wiki_db_path": str(settings.wiki_db_path),
            "wiki_db_exists": settings.wiki_db_path.exists(),
            "provider_configured": provider.available(),
        }

    @app.websocket("/v1/app-server/ws")
    async def app_server_ws(websocket: WebSocket) -> None:
        connection = AppServerConnection(websocket)
        await connection.accept()
        active_tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    request = await connection.receive_request()
                except WebSocketDisconnect:
                    return
                task = asyncio.create_task(app_server.handle(connection, request))
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
        finally:
            for task in list(active_tasks):
                task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            await app_server.disconnect(connection)

    return app
