from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import DebugRecorder, build_debug_recorder, load_debug_index, lookup_debug_index, resolve_turn_bundle
from mina_agent.debug.recorder import DebugPreviewLimits, FileDebugRecorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.providers.openai_compatible import ProviderDecisionResult, ProviderError
from mina_agent.retrieval.wiki_store import WikiKnowledgeStore
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


class _DebugCompactingProvider:
    def __init__(self) -> None:
        self._compact_calls = 0

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        return ProviderDecisionResult(
            decision=ModelDecision(mode="final_reply", final_reply="Compaction complete."),
            latency_ms=8,
            raw_response_preview='{"mode":"final_reply"}',
            parse_status="ok",
            model="test-model",
            temperature=0.2,
            message_count=len(messages),
        )

    def complete_json_value(self, messages, *, expected_root_types=None):
        self._compact_calls += 1
        payload = {
            "session_summary": {"summary": "compacted"},
            "memories": [],
            "history": {"older_turn_count": 0, "recovery_available": True},
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
                "model": "test-model",
                "temperature": 0.2,
                "message_count": len(messages),
            },
        )()

    def estimate_prompt_tokens(self, messages):
        system_content = messages[0]["content"] if messages else ""
        if "You are Mina's context compactor." in system_content:
            return {
                "model": "test-model",
                "encoding_name": "o200k_base",
                "message_count": len(messages),
                "message_tokens": [120, 100],
                "total_tokens": 220,
            }
        if self._compact_calls >= 1:
            return {
                "model": "test-model",
                "encoding_name": "o200k_base",
                "message_count": len(messages),
                "message_tokens": [700, 900],
                "total_tokens": 1600,
            }
        return {
            "model": "test-model",
            "encoding_name": "o200k_base",
            "message_count": len(messages),
            "message_tokens": [70000, 70000],
            "total_tokens": 140000,
        }


class DebugTraceTests(unittest.TestCase):
    def test_delegate_results_are_counted_as_selected_capabilities(self) -> None:
        recorder = FileDebugRecorder(
            Path(tempfile.mkdtemp()),
            DebugPreviewLimits(string_preview_chars=120, list_preview_items=4, dict_preview_keys=4, event_payload_chars=600),
        )
        summary = {
            "timeline": [
                {"capability": {"capability_id": "game.target_block.read"}},
                {"delegate_result": {"delegate": {"role": "explore"}}},
                {"delegate_result": {"observation": {"source": "agent.plan.delegate"}}},
            ]
        }

        selected = recorder._selected_capability_ids(summary)  # noqa: SLF001

        self.assertEqual(
            selected,
            ["game.target_block.read", "agent.explore.delegate", "agent.plan.delegate"],
        )

    def test_delegate_decision_fallback_is_counted_when_delegate_payload_is_truncated(self) -> None:
        recorder = FileDebugRecorder(
            Path(tempfile.mkdtemp()),
            DebugPreviewLimits(string_preview_chars=120, list_preview_items=4, dict_preview_keys=4, event_payload_chars=600),
        )
        summary = {
            "timeline": [
                {
                    "decision": {"intent": "delegate_explore", "delegate_role": "explore"},
                    "delegate_result": {
                        "preview": '{"delegate":{"role":"explore","objective":"..."}}',
                        "truncated": True,
                    },
                },
                {
                    "decision": {"intent": "delegate_plan", "delegate_role": "plan"},
                    "delegate_result": {
                        "preview": '{"delegate":{"role":"plan","objective":"..."}}',
                        "truncated": True,
                    },
                },
            ]
        }

        selected = recorder._selected_capability_ids(summary)  # noqa: SLF001

        self.assertEqual(selected, ["agent.explore.delegate", "agent.plan.delegate"])

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

    def test_debug_writes_bundle_artifacts_and_index_for_final_reply(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Ready.")],
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-bundle-final"))

        self.assertEqual(response.type, "final_reply")
        turn_dir = self._turn_dir(settings.debug_dir, "turn-bundle-final")
        request_artifact = json.loads((turn_dir / "request.start.json").read_text(encoding="utf-8"))
        final_artifact = json.loads((turn_dir / "response.final.json").read_text(encoding="utf-8"))
        capture = json.loads((turn_dir / "scenario.capture.json").read_text(encoding="utf-8"))
        index_entries = load_debug_index(settings.debug_dir)
        index_entry = lookup_debug_index(settings.debug_dir, "turn-bundle-final")

        self.assertEqual(request_artifact["turn_id"], "turn-bundle-final")
        self.assertEqual(request_artifact["user_message"], "hello Mina")
        self.assertEqual(final_artifact["type"], "final_reply")
        self.assertEqual(final_artifact["status"], "completed")
        self.assertEqual(final_artifact["final_reply"], "Ready.")
        self.assertEqual(capture["scenario"]["suite"], "real")
        self.assertEqual(capture["scenario"]["scenario_id"], "turn-bundle-final")
        self.assertEqual(capture["scenario"]["actors"][0]["name"], "Tester")
        self.assertEqual(capture["scenario"]["turns"][0]["message"], "hello Mina")
        self.assertEqual(capture["turn"]["status"], "completed")
        self.assertEqual(capture["assertion_slots"]["observed_reply_preview"], "Ready.")
        self.assertEqual(index_entry["status"], "completed")
        self.assertEqual(index_entry["player_name"], "Tester")
        self.assertTrue(any(entry["turn_id"] == "turn-bundle-final" for entry in index_entries))
        self.assertEqual(resolve_turn_bundle(settings.debug_dir, "turn-bundle-final"), turn_dir)

    def test_bridge_turn_writes_progress_artifact_for_action_batch(self) -> None:
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
            self._turn_request(turn_id="turn-progress-artifact", visible_capabilities=visible_capabilities)
        )

        self.assertEqual(start_response.type, "action_request_batch")
        turn_dir = self._turn_dir(settings.debug_dir, "turn-progress-artifact")
        progress_entries = self._load_jsonl(turn_dir / "response.progress.jsonl")
        self.assertEqual(progress_entries[0]["type"], "action_request_batch")
        self.assertEqual(progress_entries[0]["action_request_batch"][0]["capability_id"], "game.player_snapshot.read")

    def test_debug_saves_raw_provider_input_for_model_request(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Prompt saved.")],
        )

        response = loop.start_turn(
            self._turn_request(
                turn_id="turn-full-prompt-markdown",
                user_message="u" * 2000,
            )
        )

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-full-prompt-markdown")
        events = self._load_events(settings.debug_dir, "turn-full-prompt-markdown")
        prompt_artifact = summary["timeline"][0]["model_request"]["provider_input_artifact"]
        self.assertEqual(prompt_artifact["relative_path"], "prompts/step_001.provider_input.json")
        self.assertTrue(prompt_artifact["path"].endswith(".json"))
        prompt_path = Path(prompt_artifact["path"])
        self.assertTrue(prompt_path.exists())
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("u" * 2000, prompt_text)
        model_request_event = self._find_event(events, "model_request")
        self.assertEqual(model_request_event["artifact_ref"]["relative_path"], "prompts/step_001.provider_input.json")
        raw_payload = json.loads(prompt_text)
        self.assertEqual(len(raw_payload), 2)
        self.assertEqual(raw_payload[1]["role"], "user")
        self.assertIn("u" * 2000, raw_payload[1]["content"])

    def test_openai_provider_debug_request_buffer_matches_exact_http_body(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        wiki_db_path = root / "data" / "wiki.db"
        _seed_wiki_db(wiki_db_path)
        settings = Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key="test-api-key",
            model="test-model",
            config_file=root / "config.local.json",
            data_dir=root / "data",
            db_path=root / "data" / "mina_agent.db",
            wiki_db_path=wiki_db_path,
            audit_dir=root / "data" / "audit",
            debug_enabled=False,
            debug_dir=root / "data" / "debug",
            debug_string_preview_chars=600,
            debug_list_preview_items=5,
            debug_dict_preview_keys=20,
            debug_event_payload_chars=8000,
            enable_experimental=False,
            enable_dynamic_scripting=False,
            max_agent_steps=8,
            max_retrieval_results=4,
            wiki_default_limit=8,
            wiki_max_limit=20,
            wiki_section_excerpt_chars=600,
            wiki_plain_text_excerpt_chars=800,
            yield_after_internal_steps=True,
            context_token_budget=120000,
            context_recent_full_turns=32,
            context_tokenizer_encoding_override=None,
            artifact_inline_char_budget=1200,
            script_timeout_seconds=5,
            script_memory_mb=128,
            script_max_actions=8,
        )
        provider = OpenAICompatibleProvider(settings)
        messages = [
            {"role": "system", "content": "hello"},
            {"role": "user", "content": "你好"},
        ]

        buffer = provider.debug_request_buffer(messages)

        self.assertEqual(buffer["content_type"], "application/json")
        self.assertEqual(
            buffer["body_text"],
            json.dumps(
                {
                    "model": "test-model",
                    "temperature": 0.2,
                    "messages": messages,
                }
            ),
        )

    def test_debug_records_context_compaction_request_and_response(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[],
            provider_override=_DebugCompactingProvider(),
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-context-compaction-debug"))

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-context-compaction-debug")
        compaction = summary["timeline"][0]["context_compactions"][0]
        artifact = compaction["request"]["provider_input_artifact"]
        self.assertEqual(artifact["relative_path"], "prompts/step_001.context_compaction_pass_1.provider_input.json")
        self.assertEqual(compaction["response"]["pass_index"], 1)
        self.assertEqual(compaction["response"]["target_path"], "recoverable_history")
        self.assertIn("session_summary", compaction["response"]["result"])
        self.assertEqual(summary["context_builds"][0]["budget_report"]["compaction_passes"], 1)

    def test_debug_turn_directory_uses_time_prefixed_human_readable_name(self) -> None:
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[self._final_reply_result("Ready.")],
        )

        response = loop.start_turn(self._turn_request(turn_id="turn-readable-dir"))

        self.assertEqual(response.type, "final_reply")
        turn_dir = self._turn_dir(settings.debug_dir, "turn-readable-dir")
        self.assertRegex(turn_dir.name, r"^\d{6}_\d{6}__")
        self.assertIn("hello_mina", turn_dir.name)
        self.assertTrue(turn_dir.name.endswith("turn-readable-dir"))

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
        self.assertIn("game.target_block.read", self._preview_items(capability_section["preview"]))

        response = loop.start_turn(request)

        self.assertEqual(response.type, "final_reply")
        events = self._load_events(settings.debug_dir, "turn-block-schema")

        capabilities_event = self._find_event(events, "capabilities_resolved")
        target_descriptor = self._capability_from_event(capabilities_event, "game.target_block.read")
        self.assertIn("block_pos", target_descriptor["args_schema"])
        self.assertIn("block_name", target_descriptor["result_schema"])

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

    def test_repeated_same_locked_capability_is_replanned_then_stopped(self) -> None:
        visible_capabilities = [self._target_block_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                self._capability_result("game.target_block.read"),
                self._capability_result("game.target_block.read"),
                self._capability_result("game.target_block.read"),
                self._capability_result("game.target_block.read"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-repeat-loop-guard",
                user_message="what is this",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=8, max_bridge_actions_per_turn=6, max_continuation_depth=6),
            )
        )
        first_request = first_response.action_request_batch[0]

        second_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-repeat-loop-guard",
                action_results=[
                    ActionResultPayload(
                        intent_id=first_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 0, "y": 77, "z": 25},
                            "block_id": "minecraft:red_mushroom_block",
                            "block_name": "Red Mushroom Block",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read targeted block state.",
                        timing_ms=6,
                        state_fingerprint="target-1",
                    )
                ],
            ),
        )
        self.assertEqual(second_response.type, "action_request_batch")
        second_request = second_response.action_request_batch[0]
        self.assertEqual(second_request.arguments["block_pos"], {"x": 0, "y": 77, "z": 25})

        final_response = loop.resume_turn(
            second_response.continuation_id or "",
            TurnResumeRequest(
                turn_id="turn-repeat-loop-guard",
                action_results=[
                    ActionResultPayload(
                        intent_id=second_request.intent_id,
                        status="succeeded",
                        observations={
                            "target_found": True,
                            "pos": {"x": 0, "y": 77, "z": 25},
                            "block_id": "minecraft:red_mushroom_block",
                            "block_name": "Red Mushroom Block",
                        },
                        preconditions_passed=True,
                        side_effect_summary="Read locked target block.",
                        timing_ms=5,
                        state_fingerprint="target-2",
                    )
                ],
            ),
        )

        self.assertEqual(final_response.type, "final_reply")
        self.assertIn("重复的同一读取请求", final_response.final_reply or "")
        events = self._load_events(settings.debug_dir, "turn-repeat-loop-guard")
        rejected_event = self._find_event(events, "capability_rejected")
        self.assertEqual(rejected_event["payload"]["reason"], "repeated_capability_same_fingerprint")
        failed_event = self._find_event(events, "turn_failed")
        self.assertEqual(failed_event["payload"]["reason"], "repeated_capability_loop_guard")

    def test_repeated_delegate_without_new_facts_forces_model_to_choose_new_step(self) -> None:
        visible_capabilities = [self._target_block_capability()]
        loop, settings, _ = self._build_loop(
            debug_enabled=True,
            provider_responses=[
                ProviderDecisionResult(
                    decision=ModelDecision(
                        intent="delegate_explore",
                        delegate_role="explore",
                        delegate_objective="先看看玩家指着什么",
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
                        intent="delegate_explore",
                        delegate_role="explore",
                        delegate_objective="再委托一次看看玩家指着什么",
                    ),
                    latency_ms=10,
                    raw_response_preview='{"intent":"delegate_explore"}',
                    parse_status="ok",
                    model="test-model",
                    temperature=0.2,
                    message_count=2,
                ),
                self._capability_result("game.target_block.read"),
            ],
        )

        first_response = loop.start_turn(
            self._turn_request(
                turn_id="turn-repeat-delegate-guard",
                user_message="what is this",
                visible_capabilities=visible_capabilities,
                limits=LimitsPayload(max_agent_steps=6, max_bridge_actions_per_turn=2, max_continuation_depth=2),
            )
        )

        self.assertEqual(first_response.type, "progress_update")
        second_response = loop.resume_turn(
            first_response.continuation_id or "",
            TurnResumeRequest(turn_id="turn-repeat-delegate-guard", action_results=[]),
        )

        self.assertEqual(second_response.type, "action_request_batch")
        self.assertEqual(second_response.action_request_batch[0].capability_id, "game.target_block.read")
        events = self._load_events(settings.debug_dir, "turn-repeat-delegate-guard")
        rejected_event = self._find_event(events, "delegate_rejected")
        self.assertEqual(rejected_event["payload"]["reason"], "repeated_delegate_without_new_facts")

    def _build_loop(
        self,
        *,
        debug_enabled: bool,
        provider_responses: list[ProviderDecisionResult | Exception],
        api_key: str = "test-api-key",
        provider_override: object | None = None,
    ) -> tuple[AgentLoop, Settings, AgentServices]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_dir = root / "data"
        wiki_db_path = data_dir / "wiki.db"
        _seed_wiki_db(wiki_db_path)
        settings = Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key=api_key,
            model="test-model",
            config_file=root / "config.local.json",
            data_dir=data_dir,
            db_path=data_dir / "mina_agent.db",
            wiki_db_path=wiki_db_path,
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
            wiki_default_limit=8,
            wiki_max_limit=20,
            wiki_section_excerpt_chars=600,
            wiki_plain_text_excerpt_chars=800,
            yield_after_internal_steps=True,
            context_token_budget=120000,
            context_recent_full_turns=32,
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
        wiki_store = WikiKnowledgeStore(
            settings.wiki_db_path,
            default_limit=settings.wiki_default_limit,
            max_limit=settings.wiki_max_limit,
            section_excerpt_chars=settings.wiki_section_excerpt_chars,
            plain_text_excerpt_chars=settings.wiki_plain_text_excerpt_chars,
        )
        capability_registry = CapabilityRegistry(
            settings=settings,
            store=store,
            policy_engine=policy_engine,
            wiki_store=wiki_store,
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
            decision_engine=DecisionEngine(provider_override or StubProvider(provider_responses)),  # type: ignore[arg-type]
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
            if isinstance(capability, dict) and capability.get("id") == capability_id:
                return capability
        raise AssertionError(f"Capability {capability_id} not found.")

    def _preview_items(self, value: Any) -> list[Any]:
        if isinstance(value, dict) and "items" in value:
            return list(value["items"])
        if isinstance(value, list):
            return value
        raise AssertionError(f"Expected preview list structure, got: {value!r}")

    def _turn_dir(self, debug_dir: Path, turn_id: str) -> Path:
        matches = list((debug_dir / "turns").glob(f"*/*{turn_id}"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _load_summary(self, debug_dir: Path, turn_id: str) -> dict[str, Any]:
        return json.loads((self._turn_dir(debug_dir, turn_id) / "summary.json").read_text(encoding="utf-8"))

    def _load_events(self, debug_dir: Path, turn_id: str) -> list[dict[str, Any]]:
        events_path = self._turn_dir(debug_dir, turn_id) / "events.jsonl"
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _load_jsonl(self, target: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]

def _seed_wiki_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE pages (
                page_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                normalized_title TEXT NOT NULL,
                ns INTEGER NOT NULL,
                rev_id INTEGER NOT NULL,
                is_redirect INTEGER NOT NULL,
                redirect_target TEXT,
                plain_text TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                processed_path TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL,
                ord INTEGER NOT NULL,
                level INTEGER NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE TABLE categories (
                page_id INTEGER NOT NULL,
                category TEXT NOT NULL
            );
            CREATE TABLE wikilinks (
                page_id INTEGER NOT NULL,
                target_title TEXT NOT NULL,
                display_text TEXT NOT NULL
            );
            CREATE TABLE templates (
                page_id INTEGER NOT NULL,
                template_name TEXT NOT NULL
            );
            CREATE TABLE template_params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL,
                template_name TEXT NOT NULL,
                param_name TEXT NOT NULL,
                param_value TEXT NOT NULL
            );
            CREATE TABLE infobox_kv (
                page_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL
            );
            CREATE INDEX idx_pages_title ON pages(title);
            CREATE INDEX idx_pages_normalized_title ON pages(normalized_title);
            """
        )
        connection.executemany(
            """
            INSERT INTO pages(
                page_id, title, normalized_title, ns, rev_id, is_redirect, redirect_target,
                plain_text, raw_path, processed_path, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "镐", "镐", 0, 1001, 0, None, "镐是工具。", "raw/1.json", "processed/1.json", "2026-03-23T00:00:00Z"),
                (2, "钻石镐", "钻石镐", 0, 1002, 1, "镐", "钻石镐重定向。", "raw/2.json", "processed/2.json", "2026-03-23T00:00:00Z"),
                (3, "树叶", "树叶", 0, 1003, 0, None, "树叶是方块。", "raw/3.json", "processed/3.json", "2026-03-23T00:00:00Z"),
                (4, "Dark Oak Leaves", "Dark Oak Leaves", 0, 1004, 1, "树叶", "Dark Oak Leaves redirects to 树叶。", "raw/4.json", "processed/4.json", "2026-03-23T00:00:00Z"),
            ],
        )
        connection.executemany(
            "INSERT INTO sections(page_id, ord, level, title, text) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 1, "用途", "镐可用于挖掘矿石。"),
                (3, 1, 1, "用途", "树叶可用于装饰。"),
            ],
        )
        connection.executemany(
            "INSERT INTO categories(page_id, category) VALUES (?, ?)",
            [
                (1, "工具"),
                (3, "方块"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
