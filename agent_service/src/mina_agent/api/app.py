from __future__ import annotations

from fastapi import FastAPI, HTTPException

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.knowledge.service import KnowledgeService
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.runtime.agent_loop import AgentLoop, AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.context_builder import ContextBuilder
from mina_agent.schemas import TurnResumeRequest, TurnResponse, TurnStartRequest


def create_app() -> FastAPI:
    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
    settings.knowledge_cache_dir.mkdir(parents=True, exist_ok=True)
    settings.knowledge_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.audit_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path)
    audit = AuditLogger(settings.audit_dir)
    debug = build_debug_recorder(settings)
    policy_engine = PolicyEngine()
    knowledge_service = KnowledgeService(settings)
    knowledge_service.bootstrap_runtime_indexes()
    capability_registry = CapabilityRegistry(
        settings=settings,
        policy_engine=policy_engine,
        knowledge_service=knowledge_service,
        script_runner=ScriptRunner(settings),
    )
    services = AgentServices(
        settings=settings,
        store=store,
        audit=audit,
        debug=debug,
        policy_engine=policy_engine,
        capability_registry=capability_registry,
        context_builder=ContextBuilder(),
        provider=OpenAICompatibleProvider(settings),
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
            "knowledge_db_path": str(settings.knowledge_db_path),
            "knowledge_cache_dir": str(settings.knowledge_cache_dir),
            "knowledge_status": knowledge_service.status(),
            "provider_configured": services.provider.available(),
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
