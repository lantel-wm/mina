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
from mina_agent.runtime.context_builder import ContextBuilder
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

        response = loop.start_turn(self._turn_request(turn_id="turn-internal-cap"))

        self.assertEqual(response.type, "final_reply")
        summary = self._load_summary(settings.debug_dir, "turn-internal-cap")
        events = self._load_events(settings.debug_dir, "turn-internal-cap")

        self.assertIn("capability_started", [event["event_type"] for event in events])
        self.assertIn("capability_finished", [event["event_type"] for event in events])
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
            debug_event_payload_chars=2000,
            enable_experimental=False,
            enable_dynamic_scripting=False,
            max_agent_steps=8,
            max_retrieval_results=4,
            script_timeout_seconds=5,
            script_memory_mb=128,
            script_max_actions=8,
        )

        store = Store(settings.db_path)
        audit = AuditLogger(settings.audit_dir)
        debug = build_debug_recorder(settings)
        policy_engine = PolicyEngine()
        retrieval_index = LocalKnowledgeIndex(store, settings.knowledge_dir)
        retrieval_index.refresh()
        capability_registry = CapabilityRegistry(
            settings=settings,
            policy_engine=policy_engine,
            retrieval_index=retrieval_index,
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
            provider=StubProvider(provider_responses),  # type: ignore[arg-type]
        )
        return AgentLoop(services), settings, services

    def _turn_request(
        self,
        *,
        turn_id: str,
        user_message: str = "hello Mina",
        visible_capabilities: list[VisibleCapabilityPayload] | None = None,
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
            limits=LimitsPayload(
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
