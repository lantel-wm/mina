from __future__ import annotations

from dataclasses import dataclass

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import DebugRecorder
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.confirmation_resolver import ConfirmationResolver
from mina_agent.runtime.context_engine import ContextEngine
from mina_agent.runtime.context_manager import ContextManager
from mina_agent.runtime.decision_engine import DecisionEngine
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.execution_manager import ExecutionManager
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.task_manager import TaskManager


@dataclass(slots=True)
class AgentServices:
    settings: Settings
    store: Store
    audit: AuditLogger
    debug: DebugRecorder
    policy_engine: PolicyEngine
    capability_registry: CapabilityRegistry
    execution_orchestrator: ExecutionOrchestrator
    memory_policy: MemoryPolicy
    confirmation_resolver: ConfirmationResolver
    context_manager: ContextManager | None = None
    context_engine: ContextEngine | None = None
    deliberation_engine: DeliberationEngine | None = None
    decision_engine: DecisionEngine | None = None
    execution_manager: ExecutionManager | None = None
    memory_manager: MemoryManager | None = None
    task_manager: TaskManager | None = None
    delegate_runtime: DelegateRuntime | None = None
