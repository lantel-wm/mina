from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import ProviderDecisionResult
from mina_agent.retrieval.index import LocalKnowledgeIndex
from mina_agent.runtime.agent_loop import AgentLoop, AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeState
from mina_agent.runtime.confirmation_resolver import ConfirmationResolver
from mina_agent.runtime.context_engine import ContextEngine
from mina_agent.runtime.decision_engine import DecisionEngine
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TaskState, TaskStepState, TurnState, WorkingMemory
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.runtime.turn_service import TurnService
from mina_agent.schemas import (
    CapabilityRequest,
    ConfirmationRequest,
    DelegateRequest,
    DelegateSummary,
    LimitsPayload,
    ModelDecision,
    PlayerPayload,
    ServerEnvPayload,
    TurnResumeRequest,
    TurnStartRequest,
    VisibleCapabilityPayload,
)


class RuntimeRefactorTests(unittest.TestCase):
    def test_context_engine_uses_expected_block_order_and_compacts_history(self) -> None:
        settings, store, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-context")
        for index in range(5):
            store.create_turn(
                f"past-{index}",
                request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_past_{index}",
            )
            store.finish_turn(f"past-{index}", f"reply {index}")

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        self.assertEqual(
            [section["name"] for section in result.sections],
            [
                "stable_core",
                "runtime_policy",
                "scene_slice",
                "task_focus",
                "confirmation_loop",
                "recoverable_history",
                "capability_brief",
            ],
        )
        compact_summary = store.get_session_summary(request.session_ref)
        self.assertIsNotNone(compact_summary)
        self.assertIn("Mina Compact Summary", compact_summary["summary"])
        self.assertLessEqual(result.message_stats["total_chars"], settings.context_char_budget)

    def test_pending_confirmation_confirm_returns_action_batch_without_model_call(self) -> None:
        settings, store, policy_engine, capability_registry, _, orchestrator, _, resolver = self._build_runtime()
        task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="awaiting_confirmation",
            requires_confirmation=True,
        )
        action_payload = {
            "continuation_id": "",
            "intent_id": "intent-1",
            "capability_id": "game.player_snapshot.read",
            "risk_class": "read_only",
            "effect_summary": "Read the player snapshot.",
            "preconditions": [],
            "arguments": {},
            "requires_confirmation": True,
        }
        store.put_pending_confirmation(
            "session-1",
            "confirm-1",
            "Read the player snapshot.",
            action_payload,
            task_id=task["task_id"],
        )

        resolution = resolver.resolve(
            user_message="确认",
            pending_confirmation=store.get_pending_confirmation("session-1"),
            task=TaskState(
                task_id=task["task_id"],
                task_type=task["task_type"],
                owner_player=task["owner_player"],
                goal=task["goal"],
                status=task["status"],
                requires_confirmation=task["requires_confirmation"],
            ),
        )

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.disposition, "confirmed")
        self.assertIsNotNone(resolution.action_payload)
        self.assertNotEqual(resolution.action_payload["continuation_id"], "")

    def test_rejected_pending_confirmation_finishes_turn_without_provider_call(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_UnexpectedProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="awaiting_confirmation",
            requires_confirmation=True,
        )
        store.put_pending_confirmation(
            "session-1",
            "confirm-2",
            "Move the rare item.",
            {
                "continuation_id": "",
                "intent_id": "intent-2",
                "capability_id": "game.player_snapshot.read",
                "risk_class": "read_only",
                "effect_summary": "Read the player snapshot.",
                "preconditions": [],
                "arguments": {},
                "requires_confirmation": True,
            },
            task_id=task["task_id"],
        )

        request = self._turn_request(turn_id="turn-reject")
        request.user_message = "不要"
        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        self.assertIn("先停", response.final_reply or "")
        self.assertIsNone(store.get_pending_confirmation("session-1"))
        self.assertGreaterEqual(len(store.list_episodic_memories("session-1")), 1)

    def test_awaiting_confirmation_task_is_not_finalized_as_completed(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    mode="call_capability",
                                    capability_id="game.player_snapshot.read",
                                    arguments={},
                                    effect_summary="Read the player snapshot.",
                                    requires_confirmation=True,
                                ),
                                latency_ms=10,
                                raw_response_preview='{"mode":"call_capability"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            )
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-awaiting-confirmation")
        request.visible_capabilities = [
            VisibleCapabilityPayload(
                id="game.player_snapshot.read",
                kind="tool",
                description="Read the current player snapshot.",
                risk_class="read_only",
                execution_mode="bridge",
                requires_confirmation=False,
            )
        ]

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        self.assertIsNotNone(response.pending_confirmation_id)
        active_task = store.get_active_task(request.session_ref)
        self.assertIsNotNone(active_task)
        self.assertEqual(active_task["status"], "awaiting_confirmation")
        self.assertEqual(store.list_episodic_memories(request.session_ref, limit=10), [])

    def test_model_await_confirmation_intent_opens_pending_confirmation_flow(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="await_confirmation",
                                    capability_request=CapabilityRequest(
                                        capability_id="game.player_snapshot.read",
                                        arguments={},
                                        effect_summary="Read the player snapshot after confirmation.",
                                        requires_confirmation=True,
                                    ),
                                    confirmation_request=ConfirmationRequest(
                                        effect_summary="Read the player snapshot after confirmation.",
                                        reason="Need explicit approval before continuing.",
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"await_confirmation"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            )
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-await-confirm-intent")
        request.visible_capabilities = [
            VisibleCapabilityPayload(
                id="game.player_snapshot.read",
                kind="tool",
                description="Read the current player snapshot.",
                risk_class="read_only",
                execution_mode="bridge",
                requires_confirmation=False,
            )
        ]

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        self.assertIsNotNone(response.pending_confirmation_id)
        self.assertEqual(response.pending_confirmation_effect_summary, "Read the player snapshot after confirmation.")
        pending = store.get_pending_confirmation(request.session_ref)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action_payload"]["capability_id"], "game.player_snapshot.read")
        active_task = store.get_active_task(request.session_ref)
        self.assertIsNotNone(active_task)
        self.assertEqual(active_task["status"], "awaiting_confirmation")

    def test_internal_capability_requires_confirmation_before_execution(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime(
            enable_dynamic_scripting=True
        )
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="execute",
                                    capability_request=CapabilityRequest(
                                        capability_id="script.python_sandbox.execute",
                                        arguments={
                                            "script": "emit_action({'kind': 'noop'})",
                                            "inputs": {},
                                        },
                                        effect_summary="Execute a sandboxed script.",
                                        requires_confirmation=True,
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"execute"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            ),
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="reply",
                                    final_reply="脚本已经按确认后的计划执行了。",
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"reply"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            ),
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        capability_registry._local_capabilities["script.python_sandbox.execute"].executor = (  # type: ignore[attr-defined]
            lambda arguments, state: {
                "summary": "Executed confirmed sandbox script.",
                "task_patch": {
                    "summary": {
                        "next_best_step": "Reply with the script result",
                        "next_best_companion_move": "explain what changed after the confirmed script run",
                    }
                },
            }
        )
        request = self._turn_request(turn_id="turn-internal-confirm-start")
        request.player.role = "experimental"
        request.server_env.dynamic_scripting_enabled = True

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        self.assertIsNotNone(response.pending_confirmation_id)
        pending = store.get_pending_confirmation(request.session_ref)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action_payload"]["capability_id"], "script.python_sandbox.execute")

        confirm_request = self._turn_request(turn_id="turn-internal-confirm-finish")
        confirm_request.player.role = "experimental"
        confirm_request.server_env.dynamic_scripting_enabled = True
        confirm_request.user_message = "确认"

        confirmed = loop.start_turn(confirm_request)

        self.assertEqual(confirmed.type, "progress_update")
        self.assertIsNotNone(confirmed.continuation_id)
        finished = loop.resume_turn(
            confirmed.continuation_id or "",
            TurnResumeRequest(turn_id="turn-internal-confirm-finish", action_results=[]),
        )

        self.assertEqual(finished.type, "final_reply")
        self.assertEqual(finished.final_reply, "脚本已经按确认后的计划执行了。")
        self.assertIsNone(store.get_pending_confirmation(request.session_ref))

    def test_prepare_task_creates_new_task_even_when_active_task_exists(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "继续整理主基地箱子",
            status="in_progress",
        )
        turn_service = TurnService(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_UnexpectedProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-reuse-active-task")
        request.user_message = "继续整理主基地箱子"

        prepared = turn_service._prepare_task(request, pending_confirmation=None)  # type: ignore[attr-defined]

        self.assertNotEqual(prepared.task_id, active_task["task_id"])
        self.assertEqual(prepared.goal, request.user_message)
        self.assertEqual(prepared.status, "analyzing")

    def test_load_active_task_candidate_returns_existing_task_for_model_selection(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="in_progress",
        )
        turn_service = TurnService(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_UnexpectedProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-new-topic")
        request.user_message = "那个方块是什么"

        candidate = turn_service._load_active_task_candidate(request, pending_confirmation=None)  # type: ignore[attr-defined]

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.task_id, active_task["task_id"])
        self.assertEqual(candidate.status, "in_progress")

    def test_load_active_task_candidate_reloads_persisted_steps_with_updated_at_columns(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="in_progress",
        )
        store.replace_task_steps(
            active_task["task_id"],
            [
                {
                    "step_key": "scan",
                    "title": "扫描箱子",
                    "status": "in_progress",
                    "detail": "正在读取附近容器",
                }
            ],
        )
        turn_service = TurnService(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_UnexpectedProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-reload-steps")
        request.user_message = "继续整理主基地箱子"

        candidate = turn_service._load_active_task_candidate(request, pending_confirmation=None)  # type: ignore[attr-defined]

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.task_id, active_task["task_id"])
        self.assertEqual(len(candidate.steps), 1)
        self.assertEqual(candidate.steps[0].step_key, "scan")
        self.assertEqual(candidate.steps[0].title, "扫描箱子")

    def test_model_can_reuse_active_task_candidate_for_follow_up_turn(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="in_progress",
            summary={"next_best_step": "扫描箱子"},
        )
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    mode="final_reply",
                                    task_selection="reuse_active",
                                    final_reply="我接着刚才那件事继续看。",
                                ),
                                latency_ms=10,
                                raw_response_preview='{"mode":"final_reply","task_selection":"reuse_active"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            )
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-reuse-by-model")
        request.user_message = "接着刚才那个箱子继续"

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        state = store.get_turn_state(request.turn_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["task"]["task_id"], active_task["task_id"])
        self.assertEqual(store.get_task(active_task["task_id"])["status"], "completed")
        with store.connection() as connection:
            task_ids = [
                row["task_id"]
                for row in connection.execute(
                    "SELECT task_id FROM tasks WHERE session_ref = ? ORDER BY created_at ASC",
                    (request.session_ref,),
                ).fetchall()
            ]
        provisional_task_id = next(task_id for task_id in task_ids if task_id != active_task["task_id"])
        provisional_task = store.get_task(provisional_task_id)
        self.assertIsNotNone(provisional_task)
        self.assertEqual(provisional_task["status"], "canceled")
        self.assertEqual(provisional_task["summary"]["superseded_by_task_id"], active_task["task_id"])

    def test_unknown_nearby_entity_alias_is_mapped_to_visible_capability(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="execute",
                                    capability_request=CapabilityRequest(
                                        capability_id="entity.scan_nearby",
                                        arguments={"radius": 64, "entity_type": "monster"},
                                        effect_summary="Scan nearby monsters around the player.",
                                        requires_confirmation=False,
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"execute","capability_id":"entity.scan_nearby"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            )
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-nearby-entity-alias")
        request.visible_capabilities = [self._nearby_entities_capability()]

        response = loop.start_turn(request)

        self.assertEqual(response.type, "action_request_batch")
        self.assertIsNotNone(response.action_request_batch)
        action_request = response.action_request_batch[0]
        self.assertEqual(action_request.capability_id, "game.nearby_entities.read")
        self.assertEqual(action_request.arguments["radius"], 64)
        self.assertEqual(action_request.arguments["entity_type"], "monster")

    def test_minecraft_entity_scan_alias_is_mapped_to_nearby_entities_capability(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="execute",
                                    capability_request=CapabilityRequest(
                                        capability_id="minecraft.entity.scan",
                                        arguments={"player_name": "Tester", "radius": 32},
                                        effect_summary="Scan nearby entities around the player.",
                                        requires_confirmation=False,
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"execute","capability_id":"minecraft.entity.scan"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            )
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-minecraft-entity-scan-alias")
        request.visible_capabilities = [self._nearby_entities_capability()]

        response = loop.start_turn(request)

        self.assertEqual(response.type, "action_request_batch")
        self.assertIsNotNone(response.action_request_batch)
        action_request = response.action_request_batch[0]
        self.assertEqual(action_request.capability_id, "game.nearby_entities.read")
        self.assertEqual(action_request.arguments["radius"], 32)
        self.assertEqual(action_request.arguments["player_name"], "Tester")

    def test_sync_task_clears_persisted_steps_when_plan_is_reset(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        task_record = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="planned",
        )
        turn_service = TurnService(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_UnexpectedProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        task = turn_service._task_state_from_record(task_record)  # type: ignore[attr-defined]
        task.steps = [
            TaskStepState.model_validate(
                {
                    "step_key": "scan",
                    "title": "扫描箱子",
                    "status": "planned",
                }
            )
        ]
        turn_service._sync_task(task)  # type: ignore[attr-defined]

        self.assertEqual(len(store.list_task_steps(task.task_id)), 1)

        task.steps = []
        turn_service._sync_task(task)  # type: ignore[attr-defined]

        self.assertEqual(store.list_task_steps(task.task_id), [])
        task_record = store.get_task(task.task_id)
        self.assertIsNotNone(task_record)
        reloaded = turn_service._task_state_from_record(task_record)  # type: ignore[attr-defined]
        self.assertEqual(reloaded.steps, [])

    def test_observation_is_offloaded_and_can_be_read_back(self) -> None:
        _, store, _, _, _, orchestrator, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-artifact")
        turn_state = self._turn_state(request)

        observation = orchestrator.register_observation(
            turn_state,
            source="artifact.search",
            payload={
                "results": [{"path": "/tmp/example.json", "summary": "first"} for _ in range(6)],
                "summary": "Search result payload",
            },
        )

        self.assertIsNotNone(observation.artifact_ref)
        artifact = store.get_artifact(observation.artifact_ref.artifact_id)
        self.assertIsNotNone(artifact)
        self.assertIn("Search result payload", artifact["content"])

    def test_memory_policy_does_not_infer_semantic_memory_from_keywords(self) -> None:
        _, _, _, _, _, _, memory_policy, _ = self._build_runtime()
        task = TaskState(
            task_id="task-filter",
            task_type="user_request",
            owner_player="Tester",
            goal="记住以后不要自动移动稀有物品",
            status="completed",
        )
        writes = memory_policy.derive_writes(
            session_ref="session-1",
            task=task,
            user_message="记住以后不要自动移动稀有物品",
            final_reply="我记住了，以后先确认。",
            observations=[],
            status="completed",
        )

        self.assertEqual(len(writes.semantic_writes), 1)
        self.assertEqual(writes.semantic_writes[0]["memory_type"], "player_preference")
        self.assertEqual(len(writes.episodic_writes), 1)

    def test_memory_policy_uses_stable_preference_memory_key(self) -> None:
        _, _, _, _, _, _, memory_policy, _ = self._build_runtime()
        task = TaskState(
            task_id="task-stable-pref",
            task_type="user_request",
            owner_player="Tester",
            goal="记住以后不要自动移动稀有物品",
            status="completed",
        )

        first = memory_policy.derive_writes(
            session_ref="session-1",
            task=task,
            user_message="记住以后不要自动移动稀有物品",
            final_reply="我记住了，以后先确认。",
            observations=[],
            status="completed",
        )
        second = memory_policy.derive_writes(
            session_ref="session-1",
            task=task,
            user_message="记住以后不要自动移动稀有物品",
            final_reply="我记住了，以后先确认。",
            observations=[],
            status="completed",
        )

        self.assertEqual(
            first.semantic_writes[0]["memory_key"],
            "pref:8236af63bc5f00bb",
        )
        self.assertEqual(
            first.semantic_writes[0]["memory_key"],
            second.semantic_writes[0]["memory_key"],
        )

    def test_memory_policy_preserves_artifact_refs_in_retrieved_context(self) -> None:
        _, store, _, _, _, _, memory_policy, _ = self._build_runtime()
        task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="completed",
        )
        artifact = store.write_artifact(
            "session-1",
            task["task_id"],
            "turn-memory-artifact",
            "observation",
            {"summary": "rare item chest scan"},
            "rare item chest scan",
        )
        store.add_episodic_memory(
            "session-1",
            "Rare items were found in the east storage wing.",
            tags=["storage", "rare_items"],
            task_id=task["task_id"],
            artifact_refs=[artifact],
            metadata={"source": "scan"},
        )

        candidates = memory_policy.summarize_for_context(store.search_memories("session-1", "rare items", limit=6))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0].artifact_refs), 1)
        self.assertEqual(candidates[0].artifact_refs[0].artifact_id, artifact["artifact_id"])
        self.assertEqual(candidates[0].context_entry()["artifact_refs"][0]["path"], artifact["path"])

    def test_add_semantic_memory_upserts_existing_key_instead_of_appending_duplicates(self) -> None:
        _, store, _, _, _, _, _, _ = self._build_runtime()

        first_id = store.add_semantic_memory(
            "session-1",
            "player_preference",
            "pref:same",
            "Prefer confirmation before moving rare items.",
            "Ask before moving rare items.",
        )
        second_id = store.add_semantic_memory(
            "session-1",
            "player_preference",
            "pref:same",
            "Prefer confirmation before moving rare items.",
            "Always ask before moving rare items.",
        )

        semantic = [
            memory
            for memory in store.list_semantic_memories("session-1", limit=10)
            if memory["memory_type"] == "player_preference" and memory["memory_key"] == "pref:same"
        ]
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(semantic), 1)
        self.assertEqual(semantic[0]["summary"], "Always ask before moving rare items.")

    def test_memory_policy_does_not_treat_simple_rejection_as_player_preference(self) -> None:
        _, _, _, _, _, _, memory_policy, _ = self._build_runtime()
        task = TaskState(
            task_id="task-reject",
            task_type="user_request",
            owner_player="Tester",
            goal="移动稀有物品",
            status="canceled",
        )

        writes = memory_policy.derive_writes(
            session_ref="session-1",
            task=task,
            user_message="不要",
            final_reply="那我先停下。",
            observations=[],
            pending_confirmation_resolved="rejected",
            status="completed",
        )

        self.assertEqual([write for write in writes.semantic_writes if write["memory_type"] == "player_preference"], [])

    def test_memory_policy_does_not_treat_rule_question_as_player_preference(self) -> None:
        _, _, _, _, _, _, memory_policy, _ = self._build_runtime()
        task = TaskState(
            task_id="task-rule-question",
            task_type="user_request",
            owner_player="Tester",
            goal="服务器规则是什么",
            status="completed",
        )

        writes = memory_policy.derive_writes(
            session_ref="session-1",
            task=task,
            user_message="服务器规则是什么？",
            final_reply="这个服务器不能破坏别人的建筑。",
            observations=[],
            status="completed",
        )

        self.assertEqual([write for write in writes.semantic_writes if write["memory_type"] == "player_preference"], [])

    def test_context_engine_trims_large_scene_snapshot_to_budget(self) -> None:
        settings, _, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-large-snapshot")
        request.scoped_snapshot = {
            "player": {"name": "Tester"},
            "recent_events": [
                {
                    "kind": "mina_reply_sent",
                    "payload": {
                        "body": "x" * 8000,
                        "message": "y" * 8000,
                    },
                }
            ],
        }

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        self.assertLessEqual(result.message_stats["total_chars"], settings.context_char_budget)
        scene_section = next(section for section in result.sections if section["name"] == "scene_slice")
        self.assertTrue(scene_section["truncated"])

    def test_context_engine_compacts_full_session_history_not_sliding_window(self) -> None:
        settings, store, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-history-full-compact")
        for index in range(20):
            store.create_turn(
                f"past-long-{index}",
                request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_past_long_{index}",
            )
            store.finish_turn(f"past-long-{index}", f"reply {index}")

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        compact_summary = store.get_session_summary(request.session_ref)
        self.assertIsNotNone(compact_summary)
        self.assertIn("message 0", compact_summary["summary"])
        self.assertIn("message 17", compact_summary["summary"])
        history_section = next(section for section in result.sections if section["name"] == "recoverable_history")
        recent_turns = history_section["preview"]["history"]["recent_turns"]
        self.assertEqual([turn["user_message"] for turn in recent_turns], ["message 18", "message 19"])

    def test_context_engine_normalizes_target_block_and_server_rules_refs(self) -> None:
        _, _, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-normalized-scene")
        request.scoped_snapshot = {
            "player": {"name": "Tester"},
            "world": {"dimension": "minecraft:overworld"},
            "target_block": {"target_found": True, "block_name": "橡树树叶"},
            "server_rules_refs": {"server_properties_path": "/tmp/server.properties"},
        }

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        scene_section = next(section for section in result.sections if section["name"] == "scene_slice")
        self.assertEqual(scene_section["preview"]["target_block"]["block_name"], "橡树树叶")
        self.assertEqual(scene_section["preview"]["server_rules_refs"]["server_properties_path"], "/tmp/server.properties")

    def test_memory_manager_preserves_compact_summary_and_transcript_path(self) -> None:
        settings, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-summary-preserve")
        for index in range(5):
            store.create_turn(
                f"past-preserve-{index}",
                request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_past_preserve_{index}",
            )
            store.finish_turn(f"past-preserve-{index}", f"reply {index}")

        turn_state = self._turn_state(request)
        context_engine.build_messages(
            request=request,
            turn_state=turn_state,
            capability_descriptors=[],
        )
        before = store.get_session_summary(request.session_ref)
        self.assertIsNotNone(before)
        self.assertIn("Mina Compact Summary", before["summary"])
        self.assertIsNotNone(before["transcript_path"])

        memory_manager = MemoryManager(store, memory_policy)
        memory_manager.record_turn_memories(
            request,
            turn_state,
            final_reply="reply",
            status="completed",
        )

        after = store.get_session_summary(request.session_ref)
        self.assertIsNotNone(after)
        self.assertEqual(after["summary"], before["summary"])
        self.assertEqual(after["transcript_path"], before["transcript_path"])
        self.assertEqual(after["metadata"]["topic"], request.user_message)

    def test_memory_manager_updates_plain_session_summary_for_short_sessions(self) -> None:
        _, store, _, _, _, _, memory_policy, _ = self._build_runtime()
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-short-summary-1")
        first_request.user_message = "first topic"
        first_state = self._turn_state(first_request)
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="reply one",
            status="completed",
        )

        second_request = self._turn_request(turn_id="turn-short-summary-2")
        second_request.user_message = "second topic"
        second_state = self._turn_state(second_request)
        memory_manager.record_turn_memories(
            second_request,
            second_state,
            final_reply="reply two",
            status="completed",
        )

        summary = store.get_session_summary(first_request.session_ref)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["summary"], "second topic")
        self.assertIsNone(summary["transcript_path"])
        self.assertEqual(summary["metadata"]["topic"], "second topic")

    def test_recent_dialogue_memory_is_persisted_for_brief_follow_up_turns(self) -> None:
        _, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime()
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-recent-dialogue-1")
        first_request.user_message = "what is this"
        first_state = self._turn_state(first_request)
        first_state.task.status = "completed"
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="这是深色橡木树叶。需要我帮你看看它的具体信息吗？",
            status="completed",
        )

        summary = store.get_session_summary(first_request.session_ref)
        self.assertIsNotNone(summary)
        self.assertEqual(
            summary["metadata"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看它的具体信息吗？",
        )
        self.assertEqual(len(summary["metadata"]["recent_dialogue_window"]), 1)

        retrieved = store.search_memories(first_request.session_ref, "需要", limit=6)
        self.assertGreaterEqual(len(retrieved), 1)
        self.assertEqual(
            retrieved[0]["metadata"]["open_follow_up"]["prompt"],
            "需要我帮你看看它的具体信息吗？",
        )

        second_request = self._turn_request(turn_id="turn-recent-dialogue-2")
        second_request.user_message = "需要"
        context_result = context_engine.build_messages(
            request=second_request,
            turn_state=self._turn_state(second_request),
            capability_descriptors=[],
        )
        history_section = next(section for section in context_result.sections if section["name"] == "recoverable_history")
        self.assertEqual(
            history_section["preview"]["recent_dialogue_memory"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看它的具体信息吗？",
        )

    def test_model_sees_recent_dialogue_memory_for_brief_follow_up_turn(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(_RecentDialogueMemoryProvider()),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )

        first_request = self._turn_request(turn_id="turn-brief-follow-up-first")
        first_request.user_message = "what is this"
        first_response = loop.start_turn(first_request)

        self.assertEqual(first_response.type, "final_reply")
        self.assertIn("深色橡木树叶", first_response.final_reply or "")

        second_request = self._turn_request(turn_id="turn-brief-follow-up-second")
        second_request.user_message = "需要"
        second_response = loop.start_turn(second_request)

        self.assertEqual(second_response.type, "final_reply")
        self.assertEqual(second_response.final_reply, "好，我继续讲这个方块的具体信息。")

    def test_artifact_search_finds_session_artifacts_from_previous_task(self) -> None:
        settings, store, policy_engine, capability_registry, _, _, _, _ = self._build_runtime()
        old_task = store.create_task(
            "session-1",
            "Tester",
            "old task",
            status="completed",
        )
        artifact = store.write_artifact(
            "session-1",
            old_task["task_id"],
            "turn-old",
            "observation",
            {"summary": "diamond chest inventory"},
            "diamond chest inventory",
        )
        current_request = self._turn_request(turn_id="turn-new-task")
        current_task = store.create_task(
            "session-1",
            "Tester",
            "new task",
            status="analyzing",
        )
        runtime_state = RuntimeState(
            request=current_request,
            turn_state=TurnState(
                session_ref=current_request.session_ref,
                turn_id=current_request.turn_id,
                request=current_request.model_dump(),
                task=TaskState(
                    task_id=current_task["task_id"],
                    task_type=current_task["task_type"],
                    owner_player=current_task["owner_player"],
                    goal=current_task["goal"],
                    status=current_task["status"],
                ),
                working_memory=WorkingMemory(primary_goal=current_request.user_message),
            ),
            pending_confirmation=None,
        )
        capability = capability_registry.get(capability_registry.resolve(current_request), "artifact.search")

        result = capability_registry.execute_internal(capability, {"query": "diamond chest"}, runtime_state)  # type: ignore[arg-type]

        self.assertIn(artifact["artifact_id"], [item["artifact_id"] for item in result["results"]])

    def test_prepare_task_uses_neutral_default_type_without_keyword_routing(self) -> None:
        _, store, _, _, _, _, _, _ = self._build_runtime()
        manager = TaskManager(store)
        follow_up_request = self._turn_request(turn_id="turn-follow-up-keyword")
        follow_up_request.user_message = "继续整理主基地箱子"
        guidance_request = self._turn_request(turn_id="turn-guidance-keyword")
        guidance_request.user_message = "帮我看一下这个方块是什么"

        follow_up_task = manager.prepare_task(follow_up_request, None)
        guidance_task = manager.prepare_task(guidance_request, None)

        self.assertEqual(follow_up_task.task_type, "conversation_thread")
        self.assertEqual(guidance_task.task_type, "conversation_thread")

    def test_delegate_runtime_uses_isolated_structured_delegate_summary(self) -> None:
        _, store, _, _, _, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-delegate-model")
        turn_state = self._turn_state(request)
        turn_state.working_memory.key_facts = ["玩家正在看一个方块"]
        store.write_artifact(
            request.session_ref,
            turn_state.task.task_id,
            request.turn_id,
            "observation",
            {"summary": "oak leaves near the player"},
            "oak leaves near the player",
        )
        runtime = DelegateRuntime(
            store,
            DeliberationEngine(
                _StructuredStubProvider(
                    DelegateSummary(
                        summary="Explore summary for the target block: likely oak leaves, but live confirmation may still help.",
                        unresolved_questions=["Is the player still targeting the same block?"],
                        confidence=0.74,
                        stop_reason="completed",
                    )
                )
            ),
        )

        result = runtime.run(DelegateRequest(role="explore", objective="看看这个方块"), turn_state)

        self.assertEqual(result.role, "explore")
        self.assertIn("likely oak leaves", result.summary.summary)
        self.assertEqual(result.summary.unresolved_questions, ["Is the player still targeting the same block?"])
        self.assertEqual(result.task_patch["summary"]["delegate"], "explore")
        self.assertEqual(result.task_patch["status"], "analyzing")

    def test_delegate_runtime_skips_submodel_for_empty_explore_context(self) -> None:
        _, store, _, _, _, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-empty-delegate")
        turn_state = self._turn_state(request)
        runtime = DelegateRuntime(store, DeliberationEngine(_UnexpectedStructuredProvider()))

        result = runtime.run(
            DelegateRequest(role="explore", objective="扫描玩家周围半径32格内的实体"),
            turn_state,
        )

        self.assertEqual(result.role, "explore")
        self.assertIn("no additional facts found", result.summary.summary)
        self.assertEqual(result.summary.stop_reason, "fallback_completed")

    def test_delegate_step_returns_progress_update_before_final_reply(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(
                    _SequenceProvider(
                        [
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="delegate_explore",
                                    delegate_role="explore",
                                    delegate_objective="看看周围有没有现成线索",
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"delegate_explore"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            ),
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="reply",
                                    final_reply="我先把现成线索整理给你了。",
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"reply"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            ),
                        ]
                    )
                ),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-delegate-progress")

        first_response = loop.start_turn(request)

        self.assertEqual(first_response.type, "progress_update")
        self.assertIsNotNone(first_response.continuation_id)
        self.assertTrue(first_response.trace_events)

        second_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(turn_id="turn-delegate-progress", action_results=[]),
        )

        self.assertEqual(second_response.type, "final_reply")
        self.assertEqual(second_response.final_reply, "我先把现成线索整理给你了。")

    def _build_runtime(self, *, enable_dynamic_scripting: bool = False):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_dir = root / "data"
        settings = Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key="test-api-key",
            model="test-model",
            config_file=root / "config.local.json",
            data_dir=data_dir,
            db_path=data_dir / "mina_agent.db",
            knowledge_dir=data_dir / "knowledge",
            audit_dir=data_dir / "audit",
            debug_enabled=False,
            debug_dir=data_dir / "debug",
            debug_string_preview_chars=600,
            debug_list_preview_items=5,
            debug_dict_preview_keys=20,
            debug_event_payload_chars=4000,
            enable_experimental=False,
            enable_dynamic_scripting=enable_dynamic_scripting,
            max_agent_steps=8,
            max_retrieval_results=4,
            yield_after_internal_steps=True,
            context_char_budget=12000,
            context_recent_turn_limit=12,
            context_recent_full_turns=2,
            artifact_inline_char_budget=1200,
            script_timeout_seconds=5,
            script_memory_mb=128,
            script_max_actions=8,
        )
        store = Store(settings.db_path, settings.data_dir)
        audit = AuditLogger(settings.audit_dir)
        debug = build_debug_recorder(settings)
        policy_engine = PolicyEngine()
        retrieval_index = LocalKnowledgeIndex(store, settings.knowledge_dir)
        retrieval_index.refresh()
        capability_registry = CapabilityRegistry(
            settings=settings,
            store=store,
            policy_engine=policy_engine,
            retrieval_index=retrieval_index,
            script_runner=ScriptRunner(settings),
        )
        memory_policy = MemoryPolicy()
        context_engine = ContextEngine(settings, store, memory_policy)
        orchestrator = ExecutionOrchestrator(settings, store)
        resolver = ConfirmationResolver()
        return settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver

    def _turn_request(self, *, turn_id: str) -> TurnStartRequest:
        return TurnStartRequest(
            session_ref="session-1",
            turn_id=turn_id,
            player=PlayerPayload(
                uuid="player-uuid",
                name="Tester",
                role="read_only",
                dimension="minecraft:overworld",
                position={"x": 0.0, "y": 64.0, "z": 0.0},
            ),
            server_env=ServerEnvPayload(
                dedicated=True,
                motd="Test Server",
                current_players=1,
                max_players=10,
                carpet_loaded=True,
                experimental_enabled=False,
                dynamic_scripting_enabled=False,
            ),
            scoped_snapshot={"player": {"name": "Tester"}, "world": {"dimension": "minecraft:overworld"}},
            visible_capabilities=[],
            limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=1, max_continuation_depth=1),
            user_message="mina，帮我看一下",
        )

    def _turn_state(self, request: TurnStartRequest) -> TurnState:
        return TurnState(
            session_ref=request.session_ref,
            turn_id=request.turn_id,
            request=request.model_dump(),
            task=TaskState(
                task_id="task-test",
                task_type="user_request",
                owner_player=request.player.name,
                goal=request.user_message,
                status="analyzing",
            ),
            working_memory=WorkingMemory(primary_goal=request.user_message),
        )

    def _nearby_entities_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="game.nearby_entities.read",
            kind="tool",
            description="List nearby entities around the player within a radius, optionally filtered by entity category.",
            risk_class="read_only",
            execution_mode="bridge",
            requires_confirmation=False,
            args_schema={"radius": "number", "entity_type": "string", "limit": "integer"},
            result_schema={"radius": "number", "filter": "string", "count": "integer", "entities": "array<object>"},
        )


if __name__ == "__main__":
    unittest.main()


class _UnexpectedProvider:
    def decide(self, messages):
        raise AssertionError("Provider should not be called for rejected pending confirmation.")


class _SequenceProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    def decide(self, messages):
        if not self._responses:
            raise AssertionError("Provider was called more times than expected.")
        return self._responses.pop(0)


class _StructuredStubProvider:
    def __init__(self, payload):
        self._payload = payload

    def decide(self, messages):
        raise AssertionError("Structured delegate test should not call decide().")

    def complete_json(self, messages, response_model):
        return type(
            "StructuredResult",
            (),
            {
                "payload": self._payload,
                "latency_ms": 9,
                "raw_response_preview": self._payload.model_dump_json(),
                "parse_status": "ok",
                "model": "delegate-test-model",
                "temperature": 0.2,
                "message_count": len(messages),
            },
        )()


class _RecentDialogueMemoryProvider:
    def __init__(self) -> None:
        self._calls = 0

    def decide(self, messages):
        self._calls += 1
        if self._calls == 1:
            return ProviderDecisionResult(
                decision=ModelDecision(
                    mode="final_reply",
                    final_reply="这是深色橡木树叶。需要我帮你看看它的具体信息吗？",
                ),
                latency_ms=10,
                raw_response_preview='{"mode":"final_reply"}',
                parse_status="ok",
                model="test-model",
                temperature=0.2,
                message_count=len(messages),
            )
        user_content = messages[1]["content"]
        if '"recent_dialogue_memory"' not in user_content:
            raise AssertionError("recent_dialogue_memory was not included in the model context.")
        if '"active_dialogue_loop"' not in user_content:
            raise AssertionError("active_dialogue_loop was not included in the model context.")
        if "需要我帮你看看它的具体信息吗？" not in user_content:
            raise AssertionError("The last follow-up prompt was not preserved in recent dialogue memory.")
        return ProviderDecisionResult(
            decision=ModelDecision(
                mode="final_reply",
                final_reply="好，我继续讲这个方块的具体信息。",
            ),
            latency_ms=10,
            raw_response_preview='{"mode":"final_reply"}',
            parse_status="ok",
            model="test-model",
            temperature=0.2,
            message_count=len(messages),
        )


class _UnexpectedStructuredProvider:
    def decide(self, messages):
        raise AssertionError("Unexpected decide() call.")

    def complete_json(self, messages, response_model):
        raise AssertionError("Delegate runtime should have used local fallback instead of submodel summarization.")
