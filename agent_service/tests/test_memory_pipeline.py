from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mina_agent.config import Settings
from mina_agent.memories import MemoryPipeline
from mina_agent.memory.store import Store
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.task_manager import TaskManager


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
            self.assertTrue((settings.data_dir / "memories" / "raw_memories.md").exists())
            self.assertTrue((settings.data_dir / "memories" / "rollout_summaries" / "thread-1.md").exists())
            phase2_state = store.read_memory_pipeline_state("phase2")
            self.assertIsNotNone(phase2_state)
            self.assertEqual(phase2_state["selected_thread_ids"], ["thread-1"])

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


if __name__ == "__main__":
    unittest.main()
