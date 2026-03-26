from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.protocol import (
    CompanionEvaluateContextPayload,
    CompanionEvaluateParams,
    PlayerContext,
    ServerEnvContext,
)
from mina_agent.runtime.companion_evaluator import CompanionEvaluator
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.schemas import (
    CompanionDeliveryConstraintsPayload,
    CompanionEvaluateDecision,
    CompanionSignalPayload,
)


class CompanionEvaluatorTests(unittest.TestCase):
    def test_evaluator_can_start_turn_for_join_with_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="http://example.invalid",
                api_key="test",
                model="test-model",
                config_file=root / "config.json",
                data_dir=root / "data",
                db_path=root / "data" / "mina_agent.db",
                wiki_db_path=root / "wiki.db",
                audit_dir=root / "audit",
                debug_enabled=False,
                debug_dir=root / "debug",
                debug_string_preview_chars=600,
                debug_list_preview_items=5,
                debug_dict_preview_keys=20,
                debug_event_payload_chars=2000,
                enable_experimental=False,
                enable_dynamic_scripting=False,
                max_agent_steps=4,
                max_retrieval_results=4,
                wiki_default_limit=4,
                wiki_max_limit=8,
                wiki_section_excerpt_chars=400,
                wiki_plain_text_excerpt_chars=400,
                yield_after_internal_steps=False,
                context_token_budget=20000,
                context_recent_full_turns=8,
                context_tokenizer_encoding_override=None,
                artifact_inline_char_budget=1200,
                script_timeout_seconds=5,
                script_memory_mb=128,
                script_max_actions=8,
                model_request_timeout_seconds=30,
            )
            store = Store(settings.db_path, settings.data_dir)
            player_root = settings.data_dir / "memories" / "players" / "player-1"
            player_root.mkdir(parents=True, exist_ok=True)
            (player_root / "memory_summary.md").write_text("Tester likes warm greetings.\n", encoding="utf-8")
            (player_root / "MEMORY.md").write_text(
                "# Task Group: greeting_style\nscope: greeting style\napplies_to: player_uuid=player-1\n\n## Reusable knowledge\n- Tester likes warm greetings.\n",
                encoding="utf-8",
            )

            class _FakeProvider:
                def complete_json(self, messages, response_model):
                    payload = response_model(
                        action="start_turn",
                        selected_signal_ids=["signal-1"],
                        synthetic_user_message="Produce one brief companion-first proactive greeting.",
                        reason="memory_backed_join_greeting",
                    )
                    return SimpleNamespace(
                        payload=payload,
                        raw_response_preview="{}",
                        latency_ms=1,
                        parse_status="ok",
                        model="test-model",
                        temperature=0.2,
                        message_count=len(messages),
                    )

            evaluator = CompanionEvaluator(
                settings,
                store,
                DeliberationEngine(_FakeProvider()),
            )
            params = CompanionEvaluateParams(
                thread_id="thread-1",
                signals=[
                    CompanionSignalPayload(
                        signal_id="signal-1",
                        kind="player_join_greeting",
                        importance="low",
                        occurred_at="2026-03-26T00:00:00+00:00",
                        payload={"session_join_tick": 123},
                    )
                ],
                context=CompanionEvaluateContextPayload(
                    player=PlayerContext(
                        uuid="player-1",
                        name="Tester",
                        role="read_only",
                        dimension="minecraft:overworld",
                        position={"x": 0, "y": 64, "z": 0},
                    ),
                    server_env=ServerEnvContext(
                        dedicated=True,
                        motd="Test",
                        current_players=1,
                        max_players=10,
                        carpet_loaded=False,
                        experimental_enabled=False,
                        dynamic_scripting_enabled=False,
                    ),
                    scoped_snapshot={},
                ),
                companion_state={},
                delivery_constraints=CompanionDeliveryConstraintsPayload(),
            )

            decision = evaluator.evaluate(params)

            self.assertEqual(
                decision,
                CompanionEvaluateDecision(
                    action="start_turn",
                    selected_signal_ids=["signal-1"],
                    synthetic_user_message="Produce one brief companion-first proactive greeting.",
                    reason="memory_backed_join_greeting",
                ),
            )


if __name__ == "__main__":
    unittest.main()
