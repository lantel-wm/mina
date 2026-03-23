from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from mina_agent.runtime.turn_service import TurnPipeline


class TurnServiceHelperTests(unittest.TestCase):
    def test_restore_recent_block_subject_lock_from_same_task(self) -> None:
        pipeline = TurnPipeline.__new__(TurnPipeline)
        store = mock.Mock()
        store.list_turns.return_value = [
            {"turn_id": "old-turn", "task_id": "task-1", "status": "completed"},
            {"turn_id": "other-turn", "task_id": "task-2", "status": "completed"},
        ]
        store.get_turn_state.return_value = {
            "block_subject_lock": {
                "pos": {"x": 1, "y": 64, "z": 2},
                "block_name": "音符盒",
                "block_id": "minecraft:note_block",
                "target_found": True,
            }
        }
        pipeline._services = SimpleNamespace(store=store)
        turn_state = SimpleNamespace(
            session_ref="session-1",
            task=SimpleNamespace(task_id="task-1"),
            block_subject_lock=None,
        )

        pipeline._restore_recent_block_subject_lock(turn_state)  # noqa: SLF001

        self.assertIsNotNone(turn_state.block_subject_lock)
        self.assertEqual(turn_state.block_subject_lock.pos, {"x": 1, "y": 64, "z": 2})
        self.assertEqual(turn_state.block_subject_lock.block_id, "minecraft:note_block")

    def test_observe_technical_result_is_not_treated_as_ambiguous(self) -> None:
        pipeline = TurnPipeline.__new__(TurnPipeline)

        ambiguous = pipeline._capability_observation_is_ambiguous(  # noqa: SLF001
            "observe.technical",
            {
                "carpet_loaded": True,
                "script_server_running": True,
                "fake_player_count": 1,
            },
        )

        self.assertFalse(ambiguous)


if __name__ == "__main__":
    unittest.main()
