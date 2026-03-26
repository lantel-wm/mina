from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mina_agent.debug.recorder import DebugPreviewLimits, FileDebugRecorder


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

    def test_tool_request_progress_entry_uses_thread_item_shape(self) -> None:
        recorder = FileDebugRecorder(
            Path(tempfile.mkdtemp()),
            DebugPreviewLimits(string_preview_chars=120, list_preview_items=4, dict_preview_keys=4, event_payload_chars=600),
        )

        entry = recorder._progress_entry_from_event(  # noqa: SLF001
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "item_id": "item-1",
                "tool_id": "game.player_snapshot.read",
                "arguments": {},
                "risk_class": "read_only",
            },
            "tool_requested",
            datetime.now(timezone.utc),
            1,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "tool_request")
        self.assertEqual(entry["tool_call"]["tool_id"], "game.player_snapshot.read")

    def test_selected_capabilities_include_new_tool_request_and_result_shape(self) -> None:
        recorder = FileDebugRecorder(
            Path(tempfile.mkdtemp()),
            DebugPreviewLimits(string_preview_chars=120, list_preview_items=4, dict_preview_keys=4, event_payload_chars=600),
        )
        summary = {
            "timeline": [
                {"tool_request": {"tool_id": "world.player_state.read"}},
                {"tool_result": {"tool_id": "game.target_block.read"}},
            ]
        }

        selected = recorder._selected_capability_ids(summary)  # noqa: SLF001

        self.assertEqual(selected, ["world.player_state.read", "game.target_block.read"])

    def test_turn_completed_bundle_records_thread_id(self) -> None:
        recorder = FileDebugRecorder(
            Path(tempfile.mkdtemp()),
            DebugPreviewLimits(string_preview_chars=120, list_preview_items=4, dict_preview_keys=4, event_payload_chars=600),
        )
        recorder.record_event(
            "turn-1",
            "turn_started",
            {
                "thread_id": "thread-1",
                "user_message": "hello Mina",
                "player": {"name": "Tester"},
                "server_env": {"dedicated": True},
                "limits": {"max_agent_steps": 4},
                "task": {"task_id": "task-1"},
            },
        )
        recorder.record_event(
            "turn-1",
            "turn_completed",
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "final_reply": "Ready.",
                "task_id": "task-1",
            },
        )

        turn_dir = next((Path(recorder._debug_dir) / "turns").glob("*/*turn-1"))  # noqa: SLF001
        final_payload = json.loads((turn_dir / "response.final.json").read_text(encoding="utf-8"))

        self.assertEqual(final_payload["thread_id"], "thread-1")
        self.assertEqual(final_payload["final_reply"], "Ready.")


if __name__ == "__main__":
    unittest.main()
