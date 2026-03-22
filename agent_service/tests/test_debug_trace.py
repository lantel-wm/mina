from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import ProviderDecisionResult, ProviderError
from mina_agent.retrieval.index import LocalKnowledgeIndex
from mina_agent.runtime.agent_loop import AgentLoop, AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.confirmation_resolver import ConfirmationResolver
from mina_agent.runtime.context_engine import ContextEngine
from mina_agent.runtime.decision_engine import DecisionEngine
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.models import TaskState, TurnState, WorkingMemory
from mina_agent.schemas import (
    ActionResultPayload,
    LimitsPayload,
    ModelDecision,
    PlayerPayload,
    ServerEnvPayload,
    TurnResumeRequest,
    TurnStartRequest,
    VisibleCapabilityPayload,
)


class StubProvider:
    def __init__(self, responses: list[ProviderDecisionResult | Exception]) -> None:
        self._responses = list(responses)

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        if not self._responses:
            raise AssertionError("StubProvider was called more times than expected.")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DebugTraceTests(unittest.TestCase):
    def test_debug_disabled_does_not_create_trace_files(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=False,
            provider_responses=[self._final_reply_result("No debug files.")],
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-debug-disabled"))

        self.assertEqual(response.type, "final_reply")
        self.assertFalse(settings.debug_dir.exists())

    def test_debug_final_reply_writes_summary_and_events(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Ready.")],
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-final-reply"))

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-final-reply")
        events = self._load_events(settings.debug_dir, "turn-final-reply")

        self.assertEqual(summary["turn"]["status"], "completed")
        self.assertEqual(summary["final_reply_preview"], "Ready.")
        self.assertEqual(summary["user_input"]["player"]["name"], "Tester")
        self.assertEqual(summary["context_builds"][0]["step_index"], 1)
        self.assertEqual(summary["timeline"][0]["decision"]["mode"], "final_reply")
        self.assertEqual(summary["timeline"][0]["model_response"]["parse_status"], "ok")
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "turn_started",
                "capabilities_resolved",
                "context_built",
                "model_request",
                "model_response",
                "model_decision",
                "turn_completed",
            ],
        )

    def test_internal_capability_trace_records_start_and_finish(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("skill.mina_capability_guide"),
                self._final_reply_result("Guide complete."),
            ],
        )

        start_response = loop.start_turn(self._turn_request(turn_id="turn-internal-cap"))
        self.assertEqual(start_response.type, "progress_update")
        self.assertIsNotNone(start_response.continuation_id)
        response = loop.resume_turn(
            start_response.continuation_id or "",
            TurnResumeRequest(turn_id="turn-internal-cap", action_results=[]),
        )

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-internal-cap")
        events = self._load_events(settings.debug_dir, "turn-internal-cap")

        self.assertIn("capability_started", [event["event_type"] for event in events])
        self.assertIn("capability_finished", [event["event_type"] for event in events])
        self.assertIn("turn_resumed", [event["event_type"] for event in events])
        self.assertEqual(summary["timeline"][0]["capability"]["handler_kind"], "internal")
        self.assertEqual(summary["timeline"][0]["capability_result"]["status"], "succeeded")

    def test_bridge_resume_trace_records_bridge_result(self) -> None:
        visible_capabilities = [
            VisibleCapabilityPayload(
                id="game.player_snapshot.read",
                kind="tool",
                description="Read the current player snapshot.",
                risk_class="read_only",
                execution_mode="bridge",
                requires_confirmation=False,
            )
        ]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.player_snapshot.read", effect_summary="Read the player snapshot."),
                self._final_reply_result("Snapshot captured."),
            ],
        )

        start_response = loop.start_turn(
            self._turn_request(turn_id="turn-bridge", visible_capabilities=visible_capabilities)
        )

        self.assertEqual(start_response.type, "action_request_batch")
        self.assertIsNotNone(start_response.continuation_id)
        self.assertIsNotNone(start_response.action_request_batch)

        action_request = start_response.action_request_batch[0]
        resume_response = loop.resume_turn(
            start_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-bridge",
                action_results=[
                    ActionResultPayload(
                        intent_id=action_request.intent_id,
                        status="succeeded",
                        observations={"player": {"name": "Tester", "health": 20}},
                        preconditions_passed=True,
                        side_effect_summary="Read player snapshot.",
                        timing_ms=7,
                        state_fingerprint="snapshot-1",
                    )
                ],
            ),
        )

        self.assertEqual(resume_response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-bridge")
        events = self._load_events(settings.debug_dir, "turn-bridge")

        event_types = [event["event_type"] for event in events]
        self.assertIn("turn_resumed", event_types)
        self.assertIn("bridge_result", event_types)
        self.assertEqual(summary["timeline"][0]["capability"]["handler_kind"], "bridge")
        self.assertEqual(summary["timeline"][0]["capability_result"]["status"], "awaiting_bridge_result")
        self.assertEqual(summary["timeline"][0]["bridge_result"]["status"], "succeeded")

    def test_provider_failure_records_failed_turn_without_secret_leak(self) -> None:
        secret = "super-secret-key"
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                ProviderError(
                    "Temporary outage",
                    parse_status="network_error",
                    raw_response_preview="upstream unavailable",
                    latency_ms=11,
                )
            ],
            api_key=secret,
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-provider-failure"))

        self.assertEqual(response.type, "final_reply")
        summary_path = self._turn_dir(settings.debug_dir, "turn-provider-failure") / "summary.json"
        events_path = self._turn_dir(settings.debug_dir, "turn-provider-failure") / "events.jsonl"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["turn"]["status"], "failed")
        self.assertNotIn(secret, summary_path.read_text(encoding="utf-8"))
        self.assertNotIn(secret, events_path.read_text(encoding="utf-8"))

    def test_long_context_is_truncated_and_counted(self) -> None:
        loop, settings, services = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Long context handled.")],
        )
        services.store.add_memory("session-1", "note", "m" * 5000)

        response = loop.start_turn(
            self._turn_request(
                turn_id="turn-truncated",
                user_message="u" * 5000,
            )
        )

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-truncated")
        self.assertGreater(summary["truncation"]["strings_truncated"], 0)
        self.assertGreater(summary["truncation"]["chars_omitted"], 0)
        self.assertEqual(summary["turn"]["status"], "completed")

    def test_bridge_capability_schema_is_visible_in_debug_but_not_default_model_context(self) -> None:
        visible_capabilities = [self._target_block_capability()]
        loop, settings, services = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Schema loaded.")],
        )
        request = self._turn_request(
            turn_id="turn-block-schema",
            user_message="mina，这是什么？",
            visible_capabilities=visible_capabilities,
        )
        resolved_capabilities = services.capability_registry.resolve(request)
        context_result = services.context_engine.build_messages(
            request=request,
            turn_state=TurnState(
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
            ),
            capability_descriptors=[capability.descriptor for capability in resolved_capabilities],
        )
        capability_section = next(
            section for section in context_result.sections if section["name"] == "capability_brief"
        )
        target_capability = self._capability_from_payload(capability_section["preview"], "game.target_block.read")

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        events = self._load_events(settings.debug_dir, "turn-block-schema")

        capabilities_event = self._find_event(events, "capabilities_resolved")
        target_descriptor = self._capability_from_event(capabilities_event, "game.target_block.read")
        self.assertIn("block_pos", target_descriptor["args_schema"])
        self.assertIn("block_name", target_descriptor["result_schema"])
        self.assertNotIn("args_schema", target_capability)
        self.assertNotIn("result_schema", target_capability)

    def test_locked_block_position_is_injected_into_follow_up_bridge_request(self) -> None:
        visible_capabilities = [self._target_block_capability(), self._carpet_block_info_capability()]
        loop, _, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("carpet.block_info.read"),
            ],
        )

        start_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-lock-injection",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=2),
            )
        )
        first_request = start_response.action_request_batch[0]

        resume_response = loop.resume_turn(
            start_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-lock-injection",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read target block.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )

        self.assertEqual(resume_response.type, "action_request_batch")
        follow_up_request = resume_response.action_request_batch[0]
        self.assertEqual(follow_up_request.capability_id, "carpet.block_info.read")
        self.assertEqual(follow_up_request.arguments["block_pos"], {"x": 13, "y": 83, "z": -25})

    def test_python_side_bridge_budget_stops_second_bridge_action(self) -> None:
        visible_capabilities = [self._target_block_capability(), self._carpet_block_info_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("carpet.block_info.read"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-python-bridge-budget",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
            )
        )
        first_request = first_response.action_request_batch[0]

        blocked_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-python-bridge-budget",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read target block.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )

        self.assertEqual(blocked_response.type, "final_reply")
        self.assertIn("bridge action budget", blocked_response.final_reply or "")
        events = self._load_events(settings.debug_dir, "turn-python-bridge-budget")
        completed_event = self._find_event(events, "turn_completed")
        self.assertEqual(completed_event["payload"]["reason"], "bridge_budget_exhausted")

    def test_python_side_continuation_depth_stops_second_bridge_action(self) -> None:
        visible_capabilities = [self._target_block_capability(), self._carpet_block_info_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("carpet.block_info.read"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-python-cont-depth",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=1),
            )
        )
        first_request = first_response.action_request_batch[0]

        blocked_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-python-cont-depth",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read target block.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )

        self.assertEqual(blocked_response.type, "final_reply")
        self.assertIn("continuation depth", blocked_response.final_reply or "")
        events = self._load_events(settings.debug_dir, "turn-python-cont-depth")
        completed_event = self._find_event(events, "turn_completed")
        self.assertEqual(completed_event["payload"]["reason"], "continuation_depth_exhausted")

    def test_first_locked_block_is_not_overwritten_by_later_drift(self) -> None:
        visible_capabilities = [self._target_block_capability(), self._carpet_block_info_capability()]
        loop, _, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("carpet.block_info.read"),
                self._final_reply_result("你指的是坐标 (13, 83, -25) 处的橡树树叶。"),
            ],
        )

        start_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-first-lock-wins",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=2),
            )
        )
        first_request = start_response.action_request_batch[0]

        second_response = loop.resume_turn(
            start_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-first-lock-wins",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read target block.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )
        second_request = second_response.action_request_batch[0]

        final_response = loop.resume_turn(
            second_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-first-lock-wins",
                action_results=[
                    ActionResultPayload(
                        intent_id=second_request.intent_id,
                        status="succeeded",
                        observations={
                            "pos": {"x": 18, "y": 83, "z": -17},
                            "summary": "Current target drifted while the player was moving.",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read Carpet block info.",
                        timing_ms=7,
                        state_fingerprint="block-info-1",
                    )
                ],
            ),
        )

        self.assertEqual(final_response.type, "final_reply")
        self.assertIn("(13, 83, -25)", final_response.final_reply or "")
        self.assertIn("橡树树叶", final_response.final_reply or "")
        self.assertNotIn("(18, 83, -17)", final_response.final_reply or "")

    def test_two_consecutive_target_misses_still_require_model_final_reply(self) -> None:
        visible_capabilities = [self._target_block_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("game.target_block.read"),
                self._final_reply_result("我还没看清你指的是哪个方块，请停一下再试一次。"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-two-target-misses",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=2),
            )
        )
        first_request = first_response.action_request_batch[0]

        second_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-two-target-misses",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={"target_found": False},
                        preconditions_passed=True,
                        side_effect_summary="Target not found.",
                        timing_ms=5,
                        state_fingerprint="miss-1",
                    )
                ],
            ),
        )
        second_request = second_response.action_request_batch[0]

        final_response = loop.resume_turn(
            second_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-two-target-misses",
                action_results=[
                    ActionResultPayload(
                        intent_id=second_request.intent_id,
                        status="succeeded",
                        observations={"target_found": False},
                        preconditions_passed=True,
                        side_effect_summary="Target not found again.",
                        timing_ms=5,
                        state_fingerprint="miss-2",
                    )
                ],
            ),
        )

        self.assertEqual(final_response.type, "final_reply")
        self.assertIn("没看清", final_response.final_reply or "")
        events = self._load_events(settings.debug_dir, "turn-two-target-misses")
        completed_event = self._find_event(events, "turn_completed")
        self.assertNotIn("reason", completed_event["payload"])

    def test_repeated_locked_block_capability_remains_model_driven(self) -> None:
        visible_capabilities = [self._target_block_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("game.target_block.read"),
                self._final_reply_result("你刚才看的是橡树树叶。"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-locked-repeat",
                user_message="mina，这是什么？",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=2),
            )
        )
        first_request = first_response.action_request_batch[0]

        second_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-locked-repeat",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read target block.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )

        self.assertEqual(second_response.type, "action_request_batch")
        second_request = second_response.action_request_batch[0]
        self.assertEqual(second_request.capability_id, "game.target_block.read")
        self.assertEqual(second_request.arguments["block_pos"], {"x": 13, "y": 83, "z": -25})

        final_response = loop.resume_turn(
            second_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-locked-repeat",
                action_results=[
                    ActionResultPayload(
                        intent_id=second_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 13, "y": 83, "z": -25},
                            "block_id": "minecraft:oak_leaves",
                            "block_name": "橡树树叶",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read locked target block.",
                        timing_ms=6,
                        state_fingerprint="target-2",
                    )
                ],
            ),
        )

        self.assertEqual(final_response.type, "final_reply")
        self.assertIn("橡树树叶", final_response.final_reply or "")
        events = self._load_events(settings.debug_dir, "turn-locked-repeat")
        completed_event = self._find_event(events, "turn_completed")
        self.assertNotIn("reason", completed_event["payload"])

    def _build_loop(
        self,
        *,
        debug_enabled: bool,
        provider_responses: list[ProviderDecisionResult | Exception],
        api_key: str = "test-api-key",
    ) -> tuple[AgentLoop, Settings, AgentServices]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_dir = root / "data"
        settings = Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key=api_key,
            model="test-model",
            config_file=root / "config.local.json",
            data_dir=data_dir,
            db_path=data_dir / "mina_agent.db",
            knowledge_dir=data_dir / "knowledge",
            audit_dir=data_dir / "audit",
            debug_enabled=debug_enabled,
            debug_dir=data_dir / "debug",
            debug_string_preview_chars=600,
            debug_list_preview_items=5,
            debug_dict_preview_keys=20,
            debug_event_payload_chars=8000,
            enable_experimental=False,
            enable_dynamic_scripting=False,
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
        services = AgentServices(
            settings=settings,
            store=store,
            audit=audit,
            debug=debug,
            policy_engine=policy_engine,
            capability_registry=capability_registry,
            context_engine=ContextEngine(settings, store, memory_policy),
            decision_engine=DecisionEngine(StubProvider(provider_responses)),  # type: ignore[arg-type]
            execution_orchestrator=ExecutionOrchestrator(settings, store),
            memory_policy=memory_policy,
            confirmation_resolver=ConfirmationResolver(),
        )
        return AgentLoop(services), settings, services

    def _turn_request(
        self,
        *,
        turn_id: str,
        user_message: str = "hello Mina",
        visible_capabilities: list[VisibleCapabilityPayload] | None = None,
        limits: LimitsPayload | None = None,
    ) -> TurnStartRequest:
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
            scoped_snapshot={
                "player": {"name": "Tester", "health": 20},
                "world": {"dimension": "minecraft:overworld"},
                "visible_capability_ids": [cap.id for cap in visible_capabilities or []],
            },
            visible_capabilities=visible_capabilities or [],
            limits=limits
            or LimitsPayload(
                max_agent_steps=4,
                max_bridge_actions_per_turn=1,
                max_continuation_depth=1,
            ),
            user_message=user_message,
        )

    def _final_reply_result(self, final_reply: str) -> ProviderDecisionResult:
        return ProviderDecisionResult(
            decision=ModelDecision(mode="final_reply", final_reply=final_reply),
            latency_ms=9,
            raw_response_preview=json.dumps({"mode": "final_reply", "final_reply": final_reply}),
            parse_status="ok",
            model="test-model",
            temperature=0.2,
            message_count=2,
        )

    def _capability_result(
        self,
        capability_id: str,
        *,
        arguments: dict[str, Any] | None = None,
        effect_summary: str | None = None,
    ) -> ProviderDecisionResult:
        return ProviderDecisionResult(
            decision=ModelDecision(
                mode="call_capability",
                capability_id=capability_id,
                arguments=arguments or {},
                effect_summary=effect_summary or capability_id,
                requires_confirmation=False,
            ),
            latency_ms=12,
            raw_response_preview=json.dumps({"mode": "call_capability", "capability_id": capability_id}),
            parse_status="ok",
            model="test-model",
            temperature=0.2,
            message_count=2,
        )

    def _target_block_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="game.target_block.read",
            kind="tool",
            description="Inspect the block the player is targeting.",
            risk_class="read_only",
            execution_mode="bridge",
            requires_confirmation=False,
            args_schema={
                "block_pos": {
                    "type": "object",
                    "required": False,
                    "fields": {"x": "integer", "y": "integer", "z": "integer"},
                }
            },
            result_schema={
                "target_found": "boolean",
                "pos": "object{x,y,z}",
                "block_id": "string",
                "block_name": "string",
            },
        )

    def _carpet_block_info_capability(self) -> VisibleCapabilityPayload:
        return VisibleCapabilityPayload(
            id="carpet.block_info.read",
            kind="tool",
            description="Inspect detailed Carpet block diagnostics.",
            risk_class="read_only",
            execution_mode="bridge",
            requires_confirmation=False,
            args_schema={
                "block_pos": {
                    "type": "object",
                    "required": False,
                    "fields": {"x": "integer", "y": "integer", "z": "integer"},
                }
            },
            result_schema={
                "pos": "object{x,y,z}",
                "lines": "array<string>",
                "summary": "string",
            },
        )

    def _find_event(self, events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
        for event in events:
            if event["event_type"] == event_type:
                return event
        raise AssertionError(f"Event {event_type} not found.")

    def _capability_from_event(self, event: dict[str, Any], capability_id: str) -> dict[str, Any]:
        return self._capability_from_payload(event["payload"]["capabilities"], capability_id)

    def _capability_from_payload(
        self,
        capabilities: Any,
        capability_id: str,
    ) -> dict[str, Any]:
        for capability in self._preview_items(capabilities):
            if capability["id"] == capability_id:
                return capability
        raise AssertionError(f"Capability {capability_id} not found.")

    def _preview_items(self, value: Any) -> list[Any]:
        if isinstance(value, dict) and "items" in value:
            return list(value["items"])
        if isinstance(value, list):
            return value
        raise AssertionError(f"Expected preview list structure, got: {value!r}")

    def _turn_dir(self, debug_dir: Path, turn_id: str) -> Path:
        matches = list((debug_dir / "turns").glob(f"*/{turn_id}"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _load_summary(self, debug_dir: Path, turn_id: str) -> dict[str, Any]:
        return json.loads((self._turn_dir(debug_dir, turn_id) / "summary.json").read_text(encoding="utf-8"))

    def _load_events(self, debug_dir: Path, turn_id: str) -> list[dict[str, Any]]:
        events_path = self._turn_dir(debug_dir, turn_id) / "events.jsonl"
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
