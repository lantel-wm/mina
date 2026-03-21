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
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TaskState, TaskStepState, TurnState, WorkingMemory
from mina_agent.runtime.turn_service import TurnService
from mina_agent.schemas import LimitsPayload, ModelDecision, PlayerPayload, ServerEnvPayload, TurnStartRequest, VisibleCapabilityPayload


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
                "runtime_reminder",
                "situation_snapshot",
                "working_memory",
                "retrieved_long_term_memory",
                "capability_catalog",
                "recent_conversation_trigger",
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

        self.assertEqual(writes.semantic_writes, [])
        self.assertEqual(len(writes.episodic_writes), 1)

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

    def test_context_engine_trims_large_situation_snapshot_to_budget(self) -> None:
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
        situation_section = next(section for section in result.sections if section["name"] == "situation_snapshot")
        self.assertTrue(situation_section["truncated"])

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
        history_section = next(section for section in result.sections if section["name"] == "recent_conversation_trigger")
        recent_turns = history_section["preview"]["history"]["recent_turns"]
        self.assertEqual([turn["user_message"] for turn in recent_turns], ["message 18", "message 19"])

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

    def _build_runtime(self):
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
            enable_dynamic_scripting=False,
            max_agent_steps=8,
            max_retrieval_results=4,
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
