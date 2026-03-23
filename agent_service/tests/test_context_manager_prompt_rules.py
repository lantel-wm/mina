from __future__ import annotations

import unittest

from mina_agent.runtime.models import ArtifactRef, ObservationRef
from mina_agent.runtime.context_manager import ContextManager


class ContextManagerPromptRuleTests(unittest.TestCase):
    def test_stable_core_contains_review_driven_capability_preferences(self) -> None:
        manager = ContextManager.__new__(ContextManager)

        stable_core = manager._stable_core_text()  # noqa: SLF001

        self.assertIn("prefer one of those capabilities over replying from scene hints alone", stable_core)
        self.assertIn("answer from that snapshot instead of rereading the same technical state", stable_core)
        self.assertIn("use carpet.distance.measure next instead of rereading the same target", stable_core)
        self.assertIn("do not repeat the same target read again", stable_core)
        self.assertIn("use carpet.block_info.read next instead of a generic fallback reply", stable_core)
        self.assertIn("prefer a fresh threat read before reassuring them", stable_core)

    def test_working_memory_view_summarizes_long_completed_actions(self) -> None:
        manager = ContextManager.__new__(ContextManager)
        summarized = manager._summarize_text_list(  # noqa: SLF001
            [
                "x" * 400,
                "keep me short",
                "y" * 400,
                "z" * 400,
                "tail",
            ],
            max_items=3,
            max_chars=32,
        )

        self.assertEqual(len(summarized), 3)
        self.assertEqual(summarized[-1], "tail")
        self.assertTrue(summarized[0].endswith("…"))
        self.assertLessEqual(len(summarized[0]), 32)

    def test_delegate_observation_brief_entry_is_compacted(self) -> None:
        manager = ContextManager.__new__(ContextManager)
        observation = ObservationRef(
            observation_id="obs_1",
            source="agent.plan.delegate",
            summary="delegate summary",
            payload={
                "summary": "delegate summary",
                "delegate_result": {
                    "role": "plan",
                    "summary": {
                        "summary": "delegate summary",
                        "unresolved_questions": ["q1", "q2", "q3", "q4"],
                    },
                },
                "task_patch": {"summary": {"delegate_summary": "delegate summary"}},
            },
            preview={"summary": "delegate summary", "delegate_result": {"role": "plan"}},
            artifact_ref=ArtifactRef(
                artifact_id="artifact_1",
                session_ref="session_1",
                kind="delegate_result",
                path="/tmp/artifact.json",
                summary="delegate summary",
                content_type="application/json",
            ),
        )

        entry = manager._observation_brief_entry(observation)  # noqa: SLF001

        self.assertEqual(entry["source"], "agent.plan.delegate")
        self.assertEqual(entry["delegate_role"], "plan")
        self.assertEqual(entry["summary"], "delegate summary")
        self.assertEqual(entry["pending_questions"], ["q1", "q2", "q3"])
        self.assertNotIn("payload", entry)
        self.assertNotIn("preview", entry)


if __name__ == "__main__":
    unittest.main()
