from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mina_agent.config import Settings
from mina_agent.memories import MemoryPipeline
from mina_agent.memory.store import Store
from mina_agent.runtime.context_manager import ContextManager
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.runtime.models import TaskState, TurnState, WorkingMemory
from mina_agent.schemas import LimitsPayload, PlayerPayload, ServerEnvPayload, TurnStartRequest


class MemoryPipelineTests(unittest.TestCase):
    def test_refresh_now_writes_phase_outputs_and_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
            store.ensure_thread(
                "thread-1",
                player_uuid="player-1",
                player_name="Tester",
                metadata={"role": "read_only"},
            )
            store.create_thread_turn("turn-1", "thread-1", "hello Mina", {"state": "running"})
            store.finish_thread_turn("turn-1", "hello player", status="completed")

            pipeline = MemoryPipeline(settings, store, MemoryManager(store, MemoryPolicy()))
            pipeline.refresh_now(reason="test")

            phase1_outputs = store.list_memory_phase1_outputs(limit=10)
            self.assertEqual(phase1_outputs[0]["thread_id"], "thread-1")
            self.assertIn("hello Mina", phase1_outputs[0]["raw_memory"])
            player_root = pipeline.player_memory_root("player-1")
            self.assertTrue((player_root / "raw_memories.md").exists())
            rollout_summaries = list((player_root / "rollout_summaries").glob("*.md"))
            self.assertEqual(len(rollout_summaries), 1)
            self.assertIn("thread-1", rollout_summaries[0].name)
            self.assertTrue((player_root / "MEMORY.md").exists())
            self.assertTrue((player_root / "memory_summary.md").exists())
            phase2_state = store.read_memory_pipeline_state("phase2")
            self.assertIsNotNone(phase2_state)
            self.assertEqual(phase2_state["processed_players"], ["player-1"])
            player_phase2_state = store.read_memory_pipeline_state("phase2:player-1")
            self.assertIsNotNone(player_phase2_state)
            self.assertEqual(player_phase2_state["selected_thread_ids"], ["thread-1"])

    def test_task_manager_normalizes_legacy_artifact_session_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
            task = store.create_thread_task("thread-1", "Tester", "hello Mina")
            store.update_task(
                task["task_id"],
                artifacts=[
                    {
                        "artifact_id": "artifact_legacy",
                        "session_ref": "thread-1",
                        "kind": "observation",
                        "path": "/tmp/legacy.json",
                        "summary": "legacy artifact",
                        "content_type": "application/json",
                        "char_count": 12,
                    }
                ],
            )

            state = TaskManager(store).task_state_from_record(store.get_task(task["task_id"]) or {})

            self.assertEqual(len(state.artifacts), 1)
            self.assertEqual(state.artifacts[0].thread_id, "thread-1")
            self.assertEqual(state.artifacts[0].artifact_id, "artifact_legacy")

    def test_memory_pipeline_isolates_players_into_separate_memory_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
            store.ensure_thread("thread-a", player_uuid="player-a", player_name="Alice", metadata={})
            store.create_thread_turn("turn-a", "thread-a", "hello from alice", {"state": "running"})
            store.finish_thread_turn("turn-a", "reply to alice", status="completed")
            store.ensure_thread("thread-b", player_uuid="player-b", player_name="Bob", metadata={})
            store.create_thread_turn("turn-b", "thread-b", "hello from bob", {"state": "running"})
            store.finish_thread_turn("turn-b", "reply to bob", status="completed")

            pipeline = MemoryPipeline(settings, store, MemoryManager(store, MemoryPolicy()))
            pipeline.refresh_now(reason="test")

            alice_root = pipeline.player_memory_root("player-a")
            bob_root = pipeline.player_memory_root("player-b")
            alice_memory = (alice_root / "raw_memories.md").read_text(encoding="utf-8")
            bob_memory = (bob_root / "raw_memories.md").read_text(encoding="utf-8")

            self.assertIn("hello from alice", alice_memory)
            self.assertNotIn("hello from bob", alice_memory)
            self.assertIn("hello from bob", bob_memory)
            self.assertNotIn("hello from alice", bob_memory)

    def test_context_manager_reads_only_current_players_memory_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
            pipeline = MemoryPipeline(settings, store, MemoryManager(store, MemoryPolicy()))
            alice_root = pipeline.player_memory_root("player-a")
            bob_root = pipeline.player_memory_root("player-b")
            (alice_root / "memory_summary.md").write_text("Alice memory summary: village plans\n", encoding="utf-8")
            (alice_root / "MEMORY.md").write_text(
                "# Task Group: alice_village\nscope: Alice only\napplies_to: player_uuid=player-a\n\n## Task 1: village plans\n\n### rollout_summary_files\n- rollout_summaries/thread-a.md\n\n### keywords\n- village, plan\n",
                encoding="utf-8",
            )
            (bob_root / "memory_summary.md").write_text("Bob memory summary: nether prep\n", encoding="utf-8")
            (bob_root / "MEMORY.md").write_text(
                "# Task Group: bob_nether\nscope: Bob only\napplies_to: player_uuid=player-b\n\n## Task 1: nether prep\n\n### rollout_summary_files\n- rollout_summaries/thread-b.md\n\n### keywords\n- nether, prep\n",
                encoding="utf-8",
            )

            manager = ContextManager(settings, store, MemoryPolicy())
            request = TurnStartRequest(
                thread_id="thread-a",
                turn_id="turn-a",
                player=PlayerPayload(
                    uuid="player-a",
                    name="Alice",
                    role="read_only",
                    dimension="minecraft:overworld",
                    position={"x": 0, "y": 64, "z": 0},
                ),
                server_env=ServerEnvPayload(
                    dedicated=True,
                    motd="Test",
                    current_players=1,
                    max_players=10,
                    carpet_loaded=False,
                    experimental_enabled=False,
                    dynamic_scripting_enabled=False,
                ),
                scoped_snapshot={},
                visible_capabilities=[],
                limits=LimitsPayload(max_agent_steps=4, max_bridge_actions_per_turn=2, max_continuation_depth=1),
                user_message="Can you plan the village work?",
            )

            payload = manager._build_player_memory_read_path(request)

            self.assertTrue(payload["available"])
            self.assertEqual(payload["player_uuid"], "player-a")
            self.assertIn("Alice memory summary", payload["memory_summary"])
            self.assertNotIn("Bob memory summary", payload["memory_summary"])

    def test_phase2_provider_can_override_fallback_memory_files(self) -> None:
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
            store.ensure_thread("thread-1", player_uuid="player-1", player_name="Tester", metadata={})
            store.create_thread_turn("turn-1", "thread-1", "hello Mina", {"state": "running"})
            store.finish_thread_turn("turn-1", "hello player", status="completed")

            class _FakePhase2Provider:
                def available(self) -> bool:
                    return True

                def complete_json(self, messages, response_model):
                    payload = response_model(
                        memory_summary_md="# Memory Summary\n\ncustom summary\n",
                        memory_md="# Mina Player Memory\n\ncustom memory\n",
                    )
                    return SimpleNamespace(
                        payload=payload,
                        raw_response_preview="{}",
                        latency_ms=1,
                    )

            pipeline = MemoryPipeline(
                settings,
                store,
                MemoryManager(store, MemoryPolicy()),
                phase2_provider=_FakePhase2Provider(),
            )
            pipeline.refresh_now(reason="test")

            player_root = pipeline.player_memory_root("player-1")
            self.assertEqual((player_root / "memory_summary.md").read_text(encoding="utf-8"), "# Memory Summary\n\ncustom summary\n")
            self.assertEqual((player_root / "MEMORY.md").read_text(encoding="utf-8"), "# Mina Player Memory\n\ncustom memory\n")
            phase2_runs = list((player_root / "phase2_runs").glob("*"))
            self.assertEqual(len(phase2_runs), 1)
            self.assertTrue((phase2_runs[0] / "session_meta.json").exists())
            self.assertTrue((phase2_runs[0] / "prompt.messages.json").exists())
            self.assertTrue((phase2_runs[0] / "response.structured.json").exists())
            self.assertTrue((phase2_runs[0] / "rollout.jsonl").exists())

    def test_phase1_uses_rollout_evidence_and_completed_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
            store.ensure_thread("thread-1", player_uuid="player-1", player_name="Tester", metadata={})
            store.create_thread_turn("turn-1", "thread-1", "what is an observer", {"state": "running"})
            store.create_turn_item(
                thread_id="thread-1",
                turn_id="turn-1",
                item_id="user-item-1",
                item_kind="user_message",
                payload={"text": "what is an observer"},
                status="started",
            )
            store.update_turn_item(
                "user-item-1",
                status="completed",
                payload={"text": "what is an observer"},
            )
            store.create_turn_item(
                thread_id="thread-1",
                turn_id="turn-1",
                item_id="tool-item-1",
                item_kind="tool_call",
                payload={"tool_id": "wiki.page.get", "arguments": {"title": "Observer"}},
                status="started",
            )
            store.update_turn_item(
                "tool-item-1",
                status="completed",
                payload={
                    "tool_id": "wiki.page.get",
                    "status": "completed",
                    "observation": {"page": {"title": "Observer"}, "summary": "Detects block updates"},
                },
            )
            store.create_turn_item(
                thread_id="thread-1",
                turn_id="turn-1",
                item_id="assistant-item-1",
                item_kind="assistant_message",
                payload={"text": ""},
                status="started",
            )
            store.update_turn_item(
                "assistant-item-1",
                status="completed",
                payload={"text": "Observers detect block updates."},
            )
            store.finish_thread_turn("turn-1", "Observers detect block updates.", status="completed")

            pipeline = MemoryPipeline(settings, store, MemoryManager(store, MemoryPolicy()))
            pipeline.refresh_now(reason="test")

            phase1_outputs = store.list_memory_phase1_outputs(limit=10)
            self.assertIn("assistant: Observers detect block updates.", phase1_outputs[0]["raw_memory"])
            self.assertIn("tool_result: wiki.page.get", phase1_outputs[0]["raw_memory"])
            rollout_events = store.read_thread_rollout_events("thread-1")
            self.assertTrue(
                any(
                    event.get("item_id") == "assistant-item-1" and event.get("status") == "completed"
                    for event in rollout_events
                )
            )

    def test_phase1_provider_can_override_local_extraction(self) -> None:
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
            store.ensure_thread("thread-1", player_uuid="player-1", player_name="Tester", metadata={})
            store.create_thread_turn("turn-1", "thread-1", "hello Mina", {"state": "running"})
            store.create_turn_item(
                thread_id="thread-1",
                turn_id="turn-1",
                item_id="user-item-1",
                item_kind="user_message",
                payload={"text": "hello Mina"},
                status="started",
            )
            store.update_turn_item(
                "user-item-1",
                status="completed",
                payload={"text": "hello Mina"},
            )
            store.finish_thread_turn("turn-1", "hello player", status="completed")

            class _FakePhase1Provider:
                def available(self) -> bool:
                    return True

                def complete_json(self, messages, response_model):
                    payload = response_model(
                        raw_memory="custom raw memory",
                        rollout_summary="custom rollout summary",
                        rollout_slug="custom-slug",
                    )
                    return SimpleNamespace(
                        payload=payload,
                        raw_response_preview="{}",
                        latency_ms=1,
                    )

            pipeline = MemoryPipeline(
                settings,
                store,
                MemoryManager(store, MemoryPolicy()),
                phase1_provider=_FakePhase1Provider(),
            )
            pipeline.refresh_now(reason="test")

            phase1_outputs = store.list_memory_phase1_outputs(limit=10)
            self.assertEqual(phase1_outputs[0]["raw_memory"], "custom raw memory")
            self.assertEqual(phase1_outputs[0]["rollout_summary"], "custom rollout summary")
            self.assertEqual(phase1_outputs[0]["rollout_slug"], "custom-slug")

    def test_phase2_job_skips_rebuild_when_selection_fingerprint_is_unchanged(self) -> None:
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
            store.ensure_thread("thread-1", player_uuid="player-1", player_name="Tester", metadata={})
            store.create_thread_turn("turn-1", "thread-1", "hello Mina", {"state": "running"})
            store.finish_thread_turn("turn-1", "hello player", status="completed")

            class _CountingPhase2Provider:
                def __init__(self) -> None:
                    self.calls = 0

                def available(self) -> bool:
                    return True

                def complete_json(self, messages, response_model):
                    self.calls += 1
                    payload = response_model(
                        memory_summary_md=f"# Memory Summary\n\nsummary {self.calls}\n",
                        memory_md=f"# Mina Player Memory\n\nmemory {self.calls}\n",
                    )
                    return SimpleNamespace(payload=payload, raw_response_preview="{}", latency_ms=1)

            provider = _CountingPhase2Provider()
            pipeline = MemoryPipeline(
                settings,
                store,
                MemoryManager(store, MemoryPolicy()),
                phase2_provider=provider,
            )

            pipeline.refresh_now(reason="first")
            pipeline.refresh_now(reason="second")
            self.assertEqual(provider.calls, 1)

            store.create_thread_turn("turn-2", "thread-1", "another hello", {"state": "running"})
            store.finish_thread_turn("turn-2", "another reply", status="completed")
            pipeline.refresh_now(reason="third")
            self.assertEqual(provider.calls, 2)

    def test_polluted_thread_moves_previous_phase2_baseline_into_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = Settings(
                host="127.0.0.1",
                port=8787,
                base_url="",
                api_key=None,
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
                memories_pollute_on_wiki=True,
            )
            store = Store(settings.db_path, settings.data_dir)
            store.ensure_thread("thread-1", player_uuid="player-1", player_name="Tester", metadata={})
            store.create_thread_turn("turn-1", "thread-1", "tell me about villagers", {"state": "running"})
            store.finish_thread_turn("turn-1", "villagers trade", status="completed")

            pipeline = MemoryPipeline(settings, store, MemoryManager(store, MemoryPolicy()))
            pipeline.refresh_now(reason="initial")

            selection_before = store.get_player_phase2_input_selection(
                "player-1",
                limit=settings.memories_max_raw_memories_for_consolidation,
                max_unused_days=settings.memories_max_unused_days,
            )
            self.assertEqual([entry["thread_id"] for entry in selection_before["selected"]], ["thread-1"])
            self.assertTrue(store.mark_thread_memory_mode_polluted("thread-1"))
            self.assertEqual(store.get_thread_memory_mode("thread-1"), "polluted")

            selection_after = store.get_player_phase2_input_selection(
                "player-1",
                limit=settings.memories_max_raw_memories_for_consolidation,
                max_unused_days=settings.memories_max_unused_days,
            )
            self.assertEqual(selection_after["selected"], [])
            self.assertEqual(selection_after["retained_thread_ids"], [])
            self.assertEqual(len(selection_after["removed"]), 1)
            self.assertEqual(selection_after["removed"][0]["thread_id"], "thread-1")

    def test_phase2_success_rewrites_baseline_per_player_not_globally(self) -> None:
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
            store.ensure_thread("thread-a", player_uuid="player-a", player_name="Alice", metadata={})
            store.create_thread_turn("turn-a1", "thread-a", "alice turn one", {"state": "running"})
            store.finish_thread_turn("turn-a1", "alice reply one", status="completed")
            store.ensure_thread("thread-b", player_uuid="player-b", player_name="Bob", metadata={})
            store.create_thread_turn("turn-b1", "thread-b", "bob turn one", {"state": "running"})
            store.finish_thread_turn("turn-b1", "bob reply one", status="completed")

            class _CountingPhase2Provider:
                def __init__(self) -> None:
                    self.calls = 0

                def available(self) -> bool:
                    return True

                def complete_json(self, messages, response_model):
                    self.calls += 1
                    payload = response_model(
                        memory_summary_md="# Memory Summary\n\nsummary\n",
                        memory_md="# Mina Player Memory\n\nmemory\n",
                    )
                    return SimpleNamespace(payload=payload, raw_response_preview="{}", latency_ms=1)

            provider = _CountingPhase2Provider()
            pipeline = MemoryPipeline(
                settings,
                store,
                MemoryManager(store, MemoryPolicy()),
                phase2_provider=provider,
            )
            pipeline.refresh_now(reason="initial")
            self.assertEqual(provider.calls, 2)

            selection_b_before = store.get_player_phase2_input_selection(
                "player-b",
                limit=settings.memories_max_raw_memories_for_consolidation,
                max_unused_days=settings.memories_max_unused_days,
            )
            self.assertEqual(selection_b_before["retained_thread_ids"], ["thread-b"])

            store.create_thread_turn("turn-a2", "thread-a", "alice turn two", {"state": "running"})
            store.finish_thread_turn("turn-a2", "alice reply two", status="completed")
            pipeline.refresh_now(reason="alice_changed")

            self.assertEqual(provider.calls, 3)
            selection_b_after = store.get_player_phase2_input_selection(
                "player-b",
                limit=settings.memories_max_raw_memories_for_consolidation,
                max_unused_days=settings.memories_max_unused_days,
            )
            self.assertEqual(selection_b_after["retained_thread_ids"], ["thread-b"])


if __name__ == "__main__":
    unittest.main()
