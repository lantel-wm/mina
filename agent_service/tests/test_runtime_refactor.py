from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.providers.openai_compatible import ProviderDecisionResult, ProviderError
from mina_agent.retrieval.index import LocalKnowledgeIndex
from mina_agent.runtime.agent_loop import AgentLoop, AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeState
from mina_agent.runtime.confirmation_resolver import ConfirmationResolver
from mina_agent.runtime.context_manager import ContextBuildResult
from mina_agent.runtime.context_pack import ContextPack, ContextSlot, TrimPolicy
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
    ContextCompactionResult,
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
        settings, store, _, _, context_engine, _, _, _ = self._build_runtime(context_recent_full_turns=2)
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
                "observation_brief",
                "task_focus",
                "confirmation_loop",
                "dialogue_continuity",
                "dialogue_history",
                "recoverable_history",
                "capability_brief",
            ],
        )
        compact_summary = store.get_session_summary(request.session_ref)
        self.assertIsNotNone(compact_summary)
        self.assertIn("Mina Compact Summary", compact_summary["summary"])
        self.assertLessEqual(result.message_stats["total_tokens"], settings.context_token_budget)

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

    def test_prepare_task_reuses_latest_session_task_by_default(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "继续整理主基地箱子",
            status="completed",
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

        self.assertEqual(prepared.task_id, active_task["task_id"])
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

    def test_follow_up_turn_reuses_existing_session_task_without_provisional_task(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = self._build_runtime()
        active_task = store.create_task(
            "session-1",
            "Tester",
            "整理主基地箱子",
            status="completed",
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
                                    task_selection="keep_current",
                                    final_reply="我接着刚才那件事继续看。",
                                ),
                                latency_ms=10,
                                raw_response_preview='{"mode":"final_reply","task_selection":"keep_current"}',
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
        self.assertEqual(task_ids, [active_task["task_id"]])

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

    def test_unknown_capability_replans_once_before_using_visible_capability(self) -> None:
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
                                        capability_id="observe.biome",
                                        arguments={},
                                        effect_summary="Read the current biome.",
                                        requires_confirmation=False,
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"execute","capability_id":"observe.biome"}',
                                parse_status="ok",
                                model="test-model",
                                temperature=0.2,
                                message_count=2,
                            ),
                            ProviderDecisionResult(
                                decision=ModelDecision(
                                    intent="execute",
                                    capability_request=CapabilityRequest(
                                        capability_id="world.scene.read",
                                        arguments={},
                                        effect_summary="Read Mina's current structured scene summary.",
                                        requires_confirmation=False,
                                    ),
                                ),
                                latency_ms=10,
                                raw_response_preview='{"intent":"execute","capability_id":"world.scene.read"}',
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
        request = self._turn_request(turn_id="turn-unknown-capability-replan")
        request.visible_capabilities = [self._world_scene_capability()]

        response = loop.start_turn(request)

        self.assertEqual(response.type, "action_request_batch")
        self.assertIsNotNone(response.action_request_batch)
        action_request = response.action_request_batch[0]
        self.assertEqual(action_request.capability_id, "world.scene.read")
        turn_state = store.get_turn_state(request.turn_id)
        self.assertIsNotNone(turn_state)
        self.assertIn(
            "Unknown capability requested: observe.biome. Use an exact id from capability_brief or reply without executing a capability.",
            turn_state["runtime_notes"],
        )

    def test_capability_brief_keeps_full_exact_id_list_even_under_budget_pressure(self) -> None:
        settings, _, _, _, context_engine, _, _, _ = self._build_runtime()
        settings.context_token_budget = 9000
        request = self._turn_request(turn_id="turn-capability-brief-ids")
        request.scoped_snapshot = {
            "player": {
                "name": "Tester",
                "inventory_brief": {
                    "summary": "A" * 900,
                    "shortages": {"needs_food": True, "needs_torches": True},
                },
            },
            "world": {"dimension": "minecraft:overworld", "biome": "minecraft:forest"},
            "scene": {"location_kind": "surface", "hostile_summary": {"summary": "B" * 700}},
        }
        request.visible_capabilities = [
            VisibleCapabilityPayload(
                id=f"capability.test.{index}",
                kind="tool",
                description=f"Capability {index}",
                risk_class="read_only",
                execution_mode="bridge",
                requires_confirmation=False,
                args_schema={},
                result_schema={},
            )
            for index in range(8)
        ]

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=request.visible_capabilities,
        )

        capability_section = next(section for section in result.sections if section["name"] == "capability_brief")
        self.assertFalse(capability_section["truncated"])
        self.assertEqual(
            capability_section["preview"],
            [f"capability.test.{index}" for index in range(8)],
        )

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

    def test_context_engine_keeps_large_scene_snapshot_uncompressed_under_default_budget(self) -> None:
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

        self.assertLessEqual(result.message_stats["total_tokens"], settings.context_token_budget)
        scene_section = next(section for section in result.sections if section["name"] == "scene_slice")
        self.assertFalse(scene_section["truncated"])
        self.assertEqual(
            scene_section["preview"]["recent_events"][0]["payload"]["body"],
            "x" * 8000,
        )

    def test_context_engine_raises_explicit_context_overflow_when_core_context_cannot_fit(self) -> None:
        settings, _, _, _, context_engine, _, _, _ = self._build_runtime(context_token_budget=2500)
        request = self._turn_request(turn_id="turn-context-overflow")
        request.scoped_snapshot = {
            "player": {"name": "Tester"},
            "recent_events": [
                {
                    "kind": "mina_reply_sent",
                    "payload": {
                        "body": "x" * 12000,
                        "message": "y" * 12000,
                    },
                }
            ],
        }

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        self.assertFalse(result.budget_report["within_budget"])
        self.assertGreater(result.message_stats["total_tokens"], settings.context_token_budget)

    def test_context_engine_compacts_full_session_history_not_sliding_window(self) -> None:
        settings, store, _, _, context_engine, _, _, _ = self._build_runtime(context_recent_full_turns=2)
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
        dialogue_history = next(section for section in result.sections if section["name"] == "dialogue_history")
        recent_turns = dialogue_history["preview"]["turns"]
        self.assertEqual([turn["user_message"] for turn in recent_turns], ["message 18", "message 19"])
        history_section = next(section for section in result.sections if section["name"] == "recoverable_history")
        self.assertEqual(history_section["preview"]["history"]["older_turn_count"], 18)

    def test_context_engine_uses_db_recent_dialogue_history_for_last_32_turns(self) -> None:
        _, store, _, _, context_engine, _, _, _ = self._build_runtime(context_recent_full_turns=32)
        request = self._turn_request(turn_id="turn-history-db-window")
        for index in range(40):
            store.create_turn(
                f"db-turn-{index}",
                request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_db_turn_{index}",
            )
            store.finish_turn(f"db-turn-{index}", f"reply {index}")

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        dialogue_history = next(section for section in result.sections if section["name"] == "dialogue_history")
        turns = dialogue_history["preview"]["turns"]
        self.assertEqual(len(turns), 32)
        self.assertEqual(turns[0]["user_message"], "message 8")
        self.assertEqual(turns[-1]["assistant_reply"], "reply 39")

    def test_context_engine_prefers_db_turns_when_transcript_is_incomplete(self) -> None:
        _, store, _, _, context_engine, _, _, _ = self._build_runtime(context_recent_full_turns=10)
        request = self._turn_request(turn_id="turn-history-db-over-transcript")
        for index in range(10):
            store.create_turn(
                f"db-only-{index}",
                request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_db_only_{index}",
            )
            store.finish_turn(f"db-only-{index}", f"reply {index}")

        transcript_path = store.session_dir(request.session_ref) / "transcript.jsonl"
        transcript_lines = transcript_path.read_text(encoding="utf-8").splitlines()
        transcript_path.write_text("\n".join(transcript_lines[:6]) + "\n", encoding="utf-8")

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        dialogue_history = next(section for section in result.sections if section["name"] == "dialogue_history")
        turns = dialogue_history["preview"]["turns"]
        self.assertEqual(len(turns), 10)
        self.assertEqual(turns[0]["user_message"], "message 0")
        self.assertEqual(turns[-1]["assistant_reply"], "reply 9")

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

    def test_context_engine_includes_new_world_semantic_snapshot_sections(self) -> None:
        _, _, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-semantic-scene")
        request.scoped_snapshot = {
            "player": {"name": "Tester", "inventory_brief": {"shortages": {"needs_food": True}}},
            "world": {"dimension": "minecraft:overworld"},
            "scene": {"location_kind": "cave", "worth_alerting": True},
            "interactables": {"containers": [{"block_id": "minecraft:chest"}]},
            "social": {"is_alone": False, "nearby_players": [{"name": "Alex"}]},
            "technical": {"carpet_loaded": True, "logger_count": 2},
            "risk_state": {"level": "high"},
        }

        result = context_engine.build_messages(
            request=request,
            turn_state=self._turn_state(request),
            capability_descriptors=[],
        )

        scene_section = next(section for section in result.sections if section["name"] == "scene_slice")
        self.assertEqual(scene_section["preview"]["scene"]["location_kind"], "cave")
        self.assertEqual(scene_section["preview"]["interactables"]["containers"][0]["block_id"], "minecraft:chest")
        self.assertEqual(scene_section["preview"]["social"]["nearby_players"][0]["name"], "Alex")
        self.assertEqual(scene_section["preview"]["technical"]["logger_count"], 2)

    def test_capability_registry_prefers_observe_tools_before_bridge_and_internal_tools(self) -> None:
        _, _, _, capability_registry, _, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-observe-priority")
        request.visible_capabilities = [
            self._world_player_state_capability(),
            self._world_scene_capability(),
        ]

        resolved = capability_registry.resolve(request)
        leading_ids = [capability.descriptor.id for capability in resolved[:4]]
        leading_handlers = [capability.handler_kind for capability in resolved[:4]]

        self.assertEqual(leading_ids[:2], ["observe.player", "observe.scene"])
        self.assertEqual(leading_handlers[:2], ["bridge_proxy", "bridge_proxy"])
        self.assertEqual(leading_ids[2:], ["world.player_state.read", "world.scene.read"])

    def test_bridge_proxy_uses_ambient_snapshot_for_observe_player(self) -> None:
        _, _, _, capability_registry, _, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-observe-player")
        request.visible_capabilities = [self._world_player_state_capability()]
        request.scoped_snapshot = {
            "player": {
                "core_status": {"health": 7.0, "hunger": 5},
                "inventory_brief": {"shortages": {"needs_food": True}},
            }
        }
        runtime_state = RuntimeState(
            request=request,
            turn_state=self._turn_state(request),
            pending_confirmation=None,
        )

        capability = capability_registry.get(capability_registry.resolve(request), "observe.player")
        self.assertIsNotNone(capability)
        result = capability_registry.execute_internal(capability, {}, runtime_state)  # type: ignore[arg-type]

        self.assertEqual(result["_proxy_mode"], "observation")
        self.assertEqual(result["payload"]["core_status"]["health"], 7.0)
        self.assertTrue(result["payload"]["inventory_brief"]["shortages"]["needs_food"])

    def test_bridge_proxy_falls_back_to_live_bridge_for_observe_poi(self) -> None:
        _, _, _, capability_registry, _, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-observe-poi")
        request.visible_capabilities = [self._world_poi_capability()]
        runtime_state = RuntimeState(
            request=request,
            turn_state=self._turn_state(request),
            pending_confirmation=None,
        )

        capability = capability_registry.get(capability_registry.resolve(request), "observe.poi")
        self.assertIsNotNone(capability)
        result = capability_registry.execute_internal(
            capability,  # type: ignore[arg-type]
            {"kind": "structure", "query": "minecraft:village", "radius": 256},
            runtime_state,
        )

        self.assertEqual(result["_proxy_mode"], "bridge")
        self.assertEqual(result["bridge_target_id"], "world.poi.read")
        self.assertEqual(result["arguments"]["query"], "minecraft:village")

    def test_execution_orchestrator_uses_semantic_world_summaries(self) -> None:
        _, _, _, _, _, orchestrator, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-world-summary")
        turn_state = self._turn_state(request)

        scene_observation = orchestrator.register_observation(
            turn_state,
            source="observe.scene",
            payload={"risk_state": {"level": "high", "highest_threat": {"name": "Creeper"}}},
        )
        inventory_observation = orchestrator.register_observation(
            turn_state,
            source="observe.inventory",
            payload={"shortages": {"needs_food": True, "needs_torches": False}},
        )

        self.assertEqual(scene_observation.summary, "Scene risk is high; highest nearby threat is Creeper.")
        self.assertIn("scene", scene_observation.scope_tags)
        self.assertEqual(inventory_observation.summary, "Inventory pressure detected: food.")
        self.assertIn("inventory", inventory_observation.scope_tags)

    def test_scene_summary_prefers_environment_summary_with_biome_signals(self) -> None:
        _, _, _, _, _, orchestrator, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-world-location-summary")
        turn_state = self._turn_state(request)

        scene_observation = orchestrator.register_observation(
            turn_state,
            source="world.scene.read",
            payload={
                "location_kind": "surface",
                "biome": "minecraft:dark_forest",
                "environment_summary": (
                    "The player appears to be on surface terrain in minecraft:dark_forest; "
                    "support block is minecraft:dark_oak_leaves with 12 nearby leaf blocks, "
                    "2 nearby log blocks, sky visible=true, drop to ground=5."
                ),
                "risk_state": {"level": "low"},
            },
        )

        self.assertEqual(
            scene_observation.summary,
            "The player appears to be on surface terrain in minecraft:dark_forest; support block is minecraft:dark_oak_leaves with 12 nearby leaf blocks, 2 nearby log blocks, sky visible=true, drop to ground=5. Current scene risk is low.",
        )

    def test_memory_manager_preserves_compact_summary_and_transcript_path(self) -> None:
        settings, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime(context_recent_full_turns=2)
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
        continuity_section = next(section for section in context_result.sections if section["name"] == "dialogue_continuity")
        self.assertEqual(
            continuity_section["preview"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看它的具体信息吗？",
        )

    def test_recent_dialogue_memory_survives_compact_history_rewrite(self) -> None:
        _, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime()
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-recent-dialogue-compact-1")
        first_request.user_message = "where am i"
        first_state = self._turn_state(first_request)
        first_state.task.status = "completed"
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="你在黑森林里。需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
            status="completed",
        )

        for index in range(4):
            store.create_turn(
                f"past-compact-{index}",
                first_request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_past_compact_{index}",
            )
            store.finish_turn(f"past-compact-{index}", f"reply {index}")

        second_request = self._turn_request(turn_id="turn-recent-dialogue-compact-2")
        second_request.user_message = "需要"
        context_result = context_engine.build_messages(
            request=second_request,
            turn_state=self._turn_state(second_request),
            capability_descriptors=[],
        )

        continuity_section = next(section for section in context_result.sections if section["name"] == "dialogue_continuity")
        self.assertEqual(
            continuity_section["preview"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
        )
        summary = store.get_session_summary(first_request.session_ref)
        self.assertIsNotNone(summary)
        self.assertEqual(
            summary["metadata"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
        )

    def test_dialogue_continuity_survives_context_trimming_for_brief_follow_up(self) -> None:
        settings, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime(context_recent_full_turns=4)
        settings.context_token_budget = 15000
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-dialogue-continuity-1")
        first_request.user_message = "where am i"
        first_state = self._turn_state(first_request)
        first_state.task.status = "completed"
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="你在黑森林里。需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
            status="completed",
        )

        for index in range(10):
            store.create_turn(
                f"past-dialogue-{index}",
                first_request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_past_dialogue_{index}",
            )
            reply = "reply " + ("x" * 700 if index < 6 else "short")
            store.finish_turn(f"past-dialogue-{index}", reply)

        second_request = self._turn_request(turn_id="turn-dialogue-continuity-2")
        second_request.user_message = "需要"
        context_result = context_engine.build_messages(
            request=second_request,
            turn_state=self._turn_state(second_request),
            capability_descriptors=[],
        )

        self.assertIn("dialogue_continuity", [section["name"] for section in context_result.sections])
        self.assertIn("dialogue_history", [section["name"] for section in context_result.sections])
        dialogue_history_section = next(section for section in context_result.sections if section["name"] == "dialogue_history")
        self.assertFalse(dialogue_history_section["truncated"])
        self.assertIn("turns", dialogue_history_section["preview"])
        user_content = context_result.messages[1]["content"]
        self.assertIn('"active_dialogue_loop"', user_content)
        self.assertIn("需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？", user_content)
        self.assertIn('"turns"', user_content)
        self.assertIn('"assistant_reply"', user_content)

    def test_explicit_new_question_still_receives_raw_dialogue_continuity_signal(self) -> None:
        _, store, _, _, context_engine, _, memory_policy, _ = self._build_runtime()
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-dialogue-new-question-1")
        first_request.user_message = "where am i"
        first_state = self._turn_state(first_request)
        first_state.task.status = "completed"
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="你在黑森林里。需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
            status="completed",
        )

        second_request = self._turn_request(turn_id="turn-dialogue-new-question-2")
        second_request.user_message = "what is this"
        context_result = context_engine.build_messages(
            request=second_request,
            turn_state=self._turn_state(second_request),
            capability_descriptors=[],
        )

        continuity_section = next(section for section in context_result.sections if section["name"] == "dialogue_continuity")
        self.assertTrue(continuity_section["preview"]["available"])
        self.assertEqual(
            continuity_section["preview"]["active_dialogue_loop"]["prompt"],
            "需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
        )
        user_content = context_result.messages[1]["content"]
        self.assertIn('"active_dialogue_loop"', user_content)
        self.assertIn("[dialogue_history]", user_content)
        self.assertIn("what is this", user_content)

    def test_observation_brief_exposes_latest_target_read_for_answering(self) -> None:
        _, _, _, _, context_engine, orchestrator, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-observation-brief")
        request.user_message = "what is this"
        turn_state = self._turn_state(request)
        turn_state.task.goal = "what is this"

        orchestrator.register_observation(
            turn_state,
            source="game.target_block.read",
            payload={
                "target_found": True,
                "pos": {"x": -3, "y": 84, "z": 2},
                "block_id": "minecraft:dark_oak_leaves",
                "block_name": "Dark Oak Leaves",
            },
            kind="bridge_result",
        )

        context_result = context_engine.build_messages(
            request=request,
            turn_state=turn_state,
            capability_descriptors=[],
        )

        observation_section = next(section for section in context_result.sections if section["name"] == "observation_brief")
        self.assertTrue(observation_section["preview"]["available"])
        self.assertEqual(
            observation_section["preview"]["block_subject_lock"]["block_name"],
            "Dark Oak Leaves",
        )
        self.assertEqual(
            observation_section["preview"]["latest_observations"][0]["summary"],
            "Dark Oak Leaves",
        )
        self.assertEqual(
            observation_section["preview"]["latest_observations"][0]["payload"]["block_id"],
            "minecraft:dark_oak_leaves",
        )
        self.assertEqual(
            observation_section["preview"]["latest_observations"][0]["payload"]["pos"]["y"],
            84,
        )
        user_content = context_result.messages[1]["content"]
        self.assertIn("[observation_brief]", user_content)
        self.assertIn("Dark Oak Leaves", user_content)

    def test_context_overflow_fails_before_provider_call(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = (
            self._build_runtime(context_token_budget=2500)
        )
        provider = _OverflowAfterCompactionProvider()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(provider),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        request = self._turn_request(turn_id="turn-context-overflow-loop")
        request.scoped_snapshot = {
            "player": {"name": "Tester"},
            "recent_events": [
                {
                    "kind": "mina_reply_sent",
                    "payload": {
                        "body": "x" * 12000,
                        "message": "y" * 12000,
                    },
                }
            ],
        }

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        self.assertIn("超过了当前硬性预算", response.final_reply or "")
        self.assertEqual(provider.decide_calls, 0)
        self.assertEqual(provider.compact_calls, 2)
        latest_turn = store.list_turns(request.session_ref, limit=1)[0]
        self.assertEqual(latest_turn["status"], "failed")

    def test_openai_provider_estimate_prompt_tokens_uses_model_auto_encoding(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override=None)
        provider = OpenAICompatibleProvider(settings)

        provider_estimate = provider.estimate_prompt_tokens(
            [
                {"role": "system", "content": "hello"},
                {"role": "user", "content": "你好，Mina"},
            ]
        )

        self.assertEqual(provider_estimate["encoding_name"], "o200k_base")
        self.assertGreater(provider_estimate["total_tokens"], 0)

    def test_openai_provider_estimate_prompt_tokens_respects_encoding_override(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override="cl100k_base")
        provider = OpenAICompatibleProvider(settings)

        estimate = provider.estimate_prompt_tokens(
            [
                {"role": "system", "content": "hello"},
                {"role": "user", "content": "world"},
            ]
        )

        self.assertEqual(estimate["encoding_name"], "cl100k_base")

    def test_openai_provider_uses_configured_request_timeout(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override="o200k_base")
        settings.model_request_timeout_seconds = 77
        provider = OpenAICompatibleProvider(settings)
        seen: dict[str, int] = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return (
                    b'{"choices":[{"message":{"content":"{\\"mode\\":\\"final_reply\\",\\"final_reply\\":\\"ok\\"}"}}]}'
                )

        def fake_urlopen(request, timeout=0):
            seen["timeout"] = timeout
            return _Response()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = provider.decide(
                [
                    {"role": "system", "content": "hello"},
                    {"role": "user", "content": "world"},
                ]
            )

        self.assertEqual(seen["timeout"], 77)
        self.assertEqual(result.decision.final_reply, "ok")

    def test_openai_provider_complete_json_value_accepts_code_fenced_array(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override="o200k_base")
        provider = OpenAICompatibleProvider(settings)

        with mock.patch.object(
            provider,
            "_request_content",
            return_value=("```json\n[{\"kind\":\"entity_loaded\"}]\n```", 12),
        ):
            result = provider.complete_json_value(
                [
                    {"role": "system", "content": "compact"},
                    {"role": "user", "content": "target"},
                ],
                expected_root_types=(list,),
            )

        self.assertEqual(result.value, [{"kind": "entity_loaded"}])

    def test_openai_provider_complete_json_value_skips_compaction_wrapper_and_prefers_last_candidate(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override="o200k_base")
        provider = OpenAICompatibleProvider(settings)
        content = (
            '{"pass_index":1,"current_tokens":60000,"target_tokens":50000,"target_path":"recoverable_history","content":{"summary":"too long"}}\n'
            '{"summary":"short"}'
        )

        with mock.patch.object(provider, "_request_content", return_value=(content, 12)):
            result = provider.complete_json_value(
                [
                    {"role": "system", "content": "compact"},
                    {"role": "user", "content": "target"},
                ],
                expected_root_types=(dict,),
            )

        self.assertEqual(result.value, {"summary": "short"})

    def test_openai_provider_complete_json_value_raises_for_invalid_expected_shape(self) -> None:
        settings = self._settings_for_provider_test(model="gpt-5-mini", encoding_override="o200k_base")
        provider = OpenAICompatibleProvider(settings)

        with mock.patch.object(provider, "_request_content", return_value=('{"summary":"short"}', 12)):
            with self.assertRaises(ProviderError) as exc_info:
                provider.complete_json_value(
                    [
                        {"role": "system", "content": "compact"},
                        {"role": "user", "content": "target"},
                    ],
                    expected_root_types=(list,),
                )

        self.assertEqual(exc_info.exception.parse_status, "invalid_json_value")

    def test_compaction_messages_use_compact_json_and_targeted_slots(self) -> None:
        settings, store, _, _, context_engine, _, _, _ = self._build_runtime(context_token_budget=50000)
        pack = ContextPack(
            slots=[
                ContextSlot(
                    name="runtime_policy",
                    role="system",
                    source="test",
                    strategy="dynamic_structured_reminder",
                    content={"notes": "y" * 12000},
                    priority=95,
                ),
                ContextSlot(
                    name="scene_slice",
                    role="user",
                    source="test",
                    strategy="structured_slice",
                    content={
                        "player": {"name": "Tester"},
                        "world": {"dimension": "minecraft:overworld"},
                        "target_block": {"name": "Dark Oak Leaves"},
                        "risk_state": {"level": "low"},
                        "technical": {"details": "t" * 12000},
                        "social": {"details": "s" * 12000},
                    },
                    priority=85,
                ),
                ContextSlot(
                    name="task_focus",
                    role="user",
                    source="test",
                    strategy="structured_summary",
                    content={"active_observations": ["o" * 8000]},
                    priority=80,
                ),
                ContextSlot(
                    name="recoverable_history",
                    role="user",
                    source="test",
                    strategy="recoverable_recall",
                    content={"summary": "x" * 24000},
                    priority=55,
                ),
            ],
            trim_policy=TrimPolicy(priority_order=("recoverable_history", "runtime_policy", "scene_slice", "task_focus")),
        )
        context_result = ContextBuildResult(
            messages=[],
            sections=[],
            message_stats={},
            composition={},
            recovery_refs=[],
            budget_report={},
            active_context_slots=[],
            pack=pack,
            protected_slots=context_engine._protected_slot_refs(),
        )

        compaction_request = context_engine.build_compaction_request(
            context_result,
            current_tokens=52000,
            target_tokens=50000,
            pass_index=1,
        )
        self.assertIsNotNone(compaction_request)
        assert compaction_request is not None
        user_content = compaction_request.messages[1]["content"]
        payload = json.loads(user_content)

        self.assertNotIn("\n", user_content)
        self.assertEqual(compaction_request.target.path, "recoverable_history")
        self.assertEqual(payload["target_path"], "recoverable_history")
        self.assertIn("content", payload)
        self.assertIn("local_rules", payload)
        self.assertNotIn("compactable_slots", payload)
        self.assertNotIn("protected_slots", payload)

    def test_apply_compaction_target_replaces_only_scene_slice_branch(self) -> None:
        _, _, _, _, context_engine, _, _, _ = self._build_runtime()
        pack = ContextPack(
            slots=[
                ContextSlot(
                    name="scene_slice",
                    role="user",
                    source="test",
                    strategy="structured_slice",
                    content={
                        "player": {"name": "Tester"},
                        "world": {"dimension": "minecraft:overworld"},
                        "target_block": {"block_name": "Dark Oak Leaves"},
                        "risk_state": {"level": "low"},
                        "recent_events": [{"kind": "old"}],
                        "social": {"is_alone": True},
                    },
                    priority=85,
                )
            ],
            trim_policy=TrimPolicy(priority_order=("scene_slice",)),
        )
        context_result = ContextBuildResult(
            messages=[],
            sections=[],
            message_stats={},
            composition={},
            recovery_refs=[],
            budget_report={},
            active_context_slots=[],
            pack=pack,
            protected_slots=context_engine._protected_slot_refs(),
        )

        compacted = context_engine.apply_compaction_target(
            context_result,
            target_path="scene_slice.recent_events",
            replacement=[{"kind": "new"}],
            compaction_passes=1,
        )

        scene_section = next(section for section in compacted.sections if section["name"] == "scene_slice")
        self.assertEqual(scene_section["preview"]["recent_events"], [{"kind": "new"}])
        self.assertEqual(scene_section["preview"]["player"]["name"], "Tester")
        self.assertEqual(scene_section["preview"]["risk_state"]["level"], "low")
        self.assertEqual(scene_section["preview"]["social"]["is_alone"], True)

    def test_context_engine_deterministically_slims_runtime_policy_and_task_focus(self) -> None:
        _, _, _, _, context_engine, _, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-compact-shape")
        request.scoped_snapshot = {
            "player": {"name": "Tester"},
            "world": {"dimension": "minecraft:overworld"},
            "recent_events": [{"kind": f"event-{index}"} for index in range(20)],
        }
        turn_state = self._turn_state(request)
        turn_state.task.artifacts = []
        turn_state.task.steps = []
        turn_state.task.summary = {
            "player_intent": "hello",
            "next_best_companion_move": "reply briefly",
            "delegate": "explore",
            "finding_count": 2,
            "ignored": "x",
        }
        turn_state.working_memory.artifact_refs = []
        turn_state.working_memory.active_observations = []
        turn_state.working_memory.observation_refs = [{"foo": "bar"}]
        turn_state.working_memory.recovery_refs = [{"kind": "artifact"}]
        turn_state.working_memory.key_facts = ["safe"]

        result = context_engine.build_messages(
            request=request,
            turn_state=turn_state,
            capability_descriptors=[],
        )

        runtime_policy = next(section for section in result.sections if section["name"] == "runtime_policy")["preview"]
        task_focus = next(section for section in result.sections if section["name"] == "task_focus")["preview"]
        scene_slice = next(section for section in result.sections if section["name"] == "scene_slice")["preview"]

        self.assertNotIn("artifacts", runtime_policy["task"])
        self.assertNotIn("steps", runtime_policy["task"])
        self.assertEqual(runtime_policy["task"]["summary"]["player_intent"], "hello")
        self.assertNotIn("ignored", runtime_policy["task"]["summary"])
        self.assertNotIn("artifact_refs", task_focus["working_memory"])
        self.assertNotIn("observation_refs", task_focus["working_memory"])
        self.assertNotIn("recovery_refs", task_focus["working_memory"])
        self.assertEqual(len(scene_slice["recent_events"]), 12)

    def test_compaction_runs_before_main_decision_and_preserves_dialogue_context(self) -> None:
        settings, store, policy_engine, capability_registry, context_engine, orchestrator, memory_policy, resolver = (
            self._build_runtime(context_token_budget=2500, context_recent_full_turns=4)
        )
        provider = _OnePassContinuityCompactionProvider()
        loop = AgentLoop(
            AgentServices(
                settings=settings,
                store=store,
                audit=AuditLogger(settings.audit_dir),
                debug=build_debug_recorder(settings),
                policy_engine=policy_engine,
                capability_registry=capability_registry,
                context_engine=context_engine,
                decision_engine=DecisionEngine(provider),
                execution_orchestrator=orchestrator,
                memory_policy=memory_policy,
                confirmation_resolver=resolver,
            )
        )
        memory_manager = MemoryManager(store, memory_policy)

        first_request = self._turn_request(turn_id="turn-compaction-continuity-1")
        first_request.user_message = "where am i"
        first_state = self._turn_state(first_request)
        first_state.task.status = "completed"
        memory_manager.record_turn_memories(
            first_request,
            first_state,
            final_reply="你在黑森林里。需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？",
            status="completed",
        )
        for index in range(12):
            store.create_turn(
                f"turn-compaction-continuity-past-{index}",
                first_request.session_ref,
                f"message {index}",
                {},
                task_id=f"task_compaction_continuity_{index}",
            )
            store.finish_turn(f"turn-compaction-continuity-past-{index}", "reply " + ("x" * 600))

        second_request = self._turn_request(turn_id="turn-compaction-continuity-2")
        second_request.user_message = "需要"

        response = loop.start_turn(second_request)

        self.assertEqual(response.type, "final_reply")
        self.assertEqual(response.final_reply, "我继续帮你看附近情况。")
        self.assertEqual(provider.compact_calls, 1)
        self.assertEqual(provider.decide_calls, 1)

    def test_apply_compaction_result_preserves_observation_brief_target_payload(self) -> None:
        _, _, _, _, context_engine, orchestrator, _, _ = self._build_runtime()
        request = self._turn_request(turn_id="turn-observation-compact-preserve")
        request.user_message = "what is this"
        turn_state = self._turn_state(request)
        turn_state.task.goal = "what is this"

        orchestrator.register_observation(
            turn_state,
            source="game.target_block.read",
            payload={
                "target_found": True,
                "pos": {"x": -3, "y": 84, "z": 2},
                "block_id": "minecraft:dark_oak_leaves",
                "block_name": "Dark Oak Leaves",
            },
            kind="bridge_result",
        )

        context_result = context_engine.build_messages(
            request=request,
            turn_state=turn_state,
            capability_descriptors=[],
        )
        compacted = context_engine.apply_compaction_result(
            context_result,
            ContextCompactionResult(
                slot_replacements={
                    "recoverable_history": {
                        "session_summary": {"summary": "compacted"},
                        "memories": [],
                        "history": {"older_turn_count": 3, "recovery_available": True},
                        "recovery_refs": [],
                    }
                },
                rationale="Compact older recall only.",
                target_tokens=1024,
            ),
            compaction_passes=1,
        )

        observation_section = next(section for section in compacted.sections if section["name"] == "observation_brief")
        self.assertEqual(
            observation_section["preview"]["latest_observations"][0]["payload"]["block_id"],
            "minecraft:dark_oak_leaves",
        )
        self.assertIn("Dark Oak Leaves", compacted.messages[1]["content"])

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
        first_state = store.get_turn_state(first_request.turn_id)
        second_state = store.get_turn_state(second_request.turn_id)
        self.assertIsNotNone(first_state)
        self.assertIsNotNone(second_state)
        self.assertEqual(second_state["task"]["task_id"], first_state["task"]["task_id"])
        self.assertEqual(second_state["task"]["goal"], first_state["task"]["goal"])

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

    def _build_runtime(
        self,
        *,
        enable_dynamic_scripting: bool = False,
        context_token_budget: int = 120000,
        context_recent_full_turns: int = 32,
    ):
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
            context_token_budget=context_token_budget,
            context_recent_full_turns=context_recent_full_turns,
            context_tokenizer_encoding_override=None,
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

    def _settings_for_provider_test(self, *, model: str, encoding_override: str | None) -> Settings:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_dir = root / "data"
        return Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key="test-api-key",
            model=model,
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
            enable_dynamic_scripting=False,
            max_agent_steps=8,
            max_retrieval_results=4,
            yield_after_internal_steps=True,
            context_token_budget=120000,
            context_recent_full_turns=32,
            context_tokenizer_encoding_override=encoding_override,
            artifact_inline_char_budget=1200,
            script_timeout_seconds=5,
            script_memory_mb=128,
            script_max_actions=8,
        )

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

    def _world_player_state_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="world.player_state.read",
            kind="tool",
            description="Read Mina's structured player-state view.",
            risk_class="read_only",
            execution_mode="server_main_thread",
            requires_confirmation=False,
            args_schema={},
            result_schema={"player": "object"},
            domain="world",
            preferred=True,
            semantic_level="semantic",
            freshness_hint="ambient",
        )

    def _world_scene_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="world.scene.read",
            kind="tool",
            description="Read Mina's structured scene summary.",
            risk_class="read_only",
            execution_mode="server_main_thread",
            requires_confirmation=False,
            args_schema={},
            result_schema={"scene": "object"},
            domain="world",
            preferred=True,
            semantic_level="semantic",
            freshness_hint="ambient",
        )

    def _world_poi_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="world.poi.read",
            kind="tool",
            description="Locate nearby structures, biomes, or points of interest.",
            risk_class="read_only",
            execution_mode="server_main_thread",
            requires_confirmation=False,
            args_schema={"kind": "string", "query": "string", "radius": "integer"},
            result_schema={"poi": "object"},
            domain="world",
            preferred=True,
            semantic_level="semantic",
            freshness_hint="live",
        )


if __name__ == "__main__":
    unittest.main()


class _UnexpectedProvider:
    def decide(self, messages):
        raise AssertionError("Provider should not be called for rejected pending confirmation.")


class _OverflowAfterCompactionProvider:
    def __init__(self) -> None:
        self.decide_calls = 0
        self.compact_calls = 0

    def decide(self, messages):
        self.decide_calls += 1
        raise AssertionError("Main model decide() should not run after context overflow.")

    def complete_json_value(self, messages, *, expected_root_types=None):
        self.compact_calls += 1
        payload = {
            "session_summary": {"summary": "compacted"},
            "memories": [],
            "history": {"older_turn_count": 1, "recovery_available": True},
            "recovery_refs": [],
        }
        return type(
            "ValueResult",
            (),
            {
                "value": payload,
                "latency_ms": 9,
                "raw_response_preview": json.dumps(payload, ensure_ascii=False),
                "parse_status": "ok",
                "model": "compact-test-model",
                "temperature": 0.2,
                "message_count": len(messages),
            },
        )()

    def estimate_prompt_tokens(self, messages):
        system_content = messages[0]["content"] if messages else ""
        if "You are Mina's context compactor." in system_content:
            return {
                "model": "compact-test-model",
                "encoding_name": "o200k_base",
                "message_count": len(messages),
                "message_tokens": [120, 120],
                "total_tokens": 240,
            }
        return {
            "model": "compact-test-model",
            "encoding_name": "o200k_base",
            "message_count": len(messages),
            "message_tokens": [2500, 2500],
            "total_tokens": 5000,
        }


class _OnePassContinuityCompactionProvider:
    def __init__(self) -> None:
        self.decide_calls = 0
        self.compact_calls = 0

    def decide(self, messages):
        self.decide_calls += 1
        user_content = messages[1]["content"]
        if '"active_dialogue_loop"' not in user_content:
            raise AssertionError("dialogue_continuity was lost after compaction.")
        if '"assistant_reply"' not in user_content:
            raise AssertionError("dialogue_history was lost after compaction.")
        if "需要我帮你看看更具体的情况，比如附近有什么可用的资源或安全路径吗？" not in user_content:
            raise AssertionError("The open follow-up prompt was not preserved after compaction.")
        return ProviderDecisionResult(
            decision=ModelDecision(
                mode="final_reply",
                final_reply="我继续帮你看附近情况。",
            ),
            latency_ms=10,
            raw_response_preview='{"mode":"final_reply"}',
            parse_status="ok",
            model="compact-test-model",
            temperature=0.2,
            message_count=len(messages),
        )

    def complete_json_value(self, messages, *, expected_root_types=None):
        self.compact_calls += 1
        payload = {
            "session_summary": {"summary": "compacted"},
            "memories": [],
            "history": {"older_turn_count": 12, "recovery_available": True},
            "recovery_refs": [],
        }
        return type(
            "ValueResult",
            (),
            {
                "value": payload,
                "latency_ms": 9,
                "raw_response_preview": json.dumps(payload, ensure_ascii=False),
                "parse_status": "ok",
                "model": "compact-test-model",
                "temperature": 0.2,
                "message_count": len(messages),
            },
        )()

    def estimate_prompt_tokens(self, messages):
        system_content = messages[0]["content"] if messages else ""
        if "You are Mina's context compactor." in system_content:
            return {
                "model": "compact-test-model",
                "encoding_name": "o200k_base",
                "message_count": len(messages),
                "message_tokens": [150, 220],
                "total_tokens": 370,
            }
        if self.compact_calls >= 1:
            return {
                "model": "compact-test-model",
                "encoding_name": "o200k_base",
                "message_count": len(messages),
                "message_tokens": [700, 900],
                "total_tokens": 1600,
            }
        return {
            "model": "compact-test-model",
            "encoding_name": "o200k_base",
            "message_count": len(messages),
            "message_tokens": [2000, 1800],
            "total_tokens": 3800,
        }


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
        if "[dialogue_continuity]" not in user_content:
            raise AssertionError("dialogue_continuity was not included in the model context.")
        if "[dialogue_history]" not in user_content:
            raise AssertionError("dialogue_history was not included in the model context.")
        if '"active_dialogue_loop"' not in user_content:
            raise AssertionError("active_dialogue_loop was not included in the model context.")
        if '"assistant_reply"' not in user_content:
            raise AssertionError("assistant_reply was not included in the recent dialogue history.")
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
