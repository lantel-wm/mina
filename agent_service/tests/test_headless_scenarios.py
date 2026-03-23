from __future__ import annotations

import unittest

from mina_agent.dev.scenarios import ObservedScenarioResult, ScenarioAssertions, evaluate_assertions


class HeadlessScenarioAssertionsTest(unittest.TestCase):
    def test_missing_required_capability_is_reported_explicitly(self) -> None:
        assertions = ScenarioAssertions(expected_final_status="completed", required_capability_ids=["game.player_snapshot.read"])
        observed = ObservedScenarioResult(
            final_status="completed",
            selected_capability_ids=[],
            confirmation_expected=False,
            final_reply="Ready.",
            duration_ms=200,
        )

        category, detail = evaluate_assertions(assertions, observed)

        self.assertEqual(category, "missing_required_capability")
        self.assertIn("game.player_snapshot.read", detail or "")

    def test_reply_assertion_failure_is_reported_explicitly(self) -> None:
        assertions = ScenarioAssertions(required_reply_substrings=["hello"], forbidden_reply_substrings=["forbidden"])
        observed = ObservedScenarioResult(
            final_status="completed",
            selected_capability_ids=[],
            confirmation_expected=False,
            final_reply="Ready.",
            duration_ms=200,
        )

        category, detail = evaluate_assertions(assertions, observed)

        self.assertEqual(category, "reply_assertion_failure")
        self.assertIn("required reply substring", detail or "")

    def test_timeout_category_is_used_for_duration_overrun(self) -> None:
        assertions = ScenarioAssertions(max_duration_ms=100)
        observed = ObservedScenarioResult(
            final_status="completed",
            selected_capability_ids=[],
            confirmation_expected=False,
            final_reply="Ready.",
            duration_ms=150,
        )

        category, detail = evaluate_assertions(assertions, observed)

        self.assertEqual(category, "timeout")
        self.assertIn("150 ms", detail or "")


if __name__ == "__main__":
    unittest.main()
