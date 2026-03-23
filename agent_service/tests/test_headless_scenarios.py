from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mina_agent.dev.scenarios import HeadlessScenario, ObservedScenarioResult, ScenarioAssertions, evaluate_assertions, load_scenarios


class HeadlessScenarioAssertionsTest(unittest.TestCase):
    def test_legacy_single_actor_shape_upgrades_to_new_schema(self) -> None:
        scenario = HeadlessScenario.model_validate(
            {
                "suite": "real",
                "scenario_id": "legacy_case",
                "world_template": "overworld_day_spawn",
                "player_name": "Steve",
                "message": "hello",
                "follow_up_messages": ["again"],
                "assertions": {"expected_final_status": "completed"},
            }
        )

        self.assertEqual(scenario.actors[0].actor_id, "player")
        self.assertEqual(scenario.actors[0].name, "Steve")
        self.assertEqual([turn.message for turn in scenario.turns], ["hello", "again"])
        self.assertEqual(scenario.expectation, "target_state")

    def test_load_scenarios_separates_runnable_planned_and_known_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.json").write_text(
                json.dumps(
                    {
                        "suite": "real",
                        "scenario_id": "runnable",
                        "world_template": "overworld_day_spawn",
                        "status": "runnable_now",
                        "expectation": "target_state",
                        "actors": [{"actor_id": "player", "name": "Steve", "role": "read_only"}],
                        "turns": [{"actor_id": "player", "message": "hello"}],
                    }
                ),
                encoding="utf-8",
            )
            (root / "b.json").write_text(
                json.dumps(
                    {
                        "suite": "real",
                        "scenario_id": "planned_case",
                        "world_template": "overworld_day_spawn",
                        "status": "planned",
                        "expectation": "target_state",
                        "actors": [{"actor_id": "player", "name": "Steve", "role": "read_only"}],
                        "turns": [{"actor_id": "player", "message": "hello"}],
                    }
                ),
                encoding="utf-8",
            )
            (root / "c.json").write_text(
                json.dumps(
                    {
                        "suite": "real",
                        "scenario_id": "known_issue_case",
                        "world_template": "overworld_day_spawn",
                        "status": "runnable_now",
                        "expectation": "known_issue",
                        "actors": [{"actor_id": "player", "name": "Steve", "role": "read_only"}],
                        "turns": [{"actor_id": "player", "message": "hello"}],
                    }
                ),
                encoding="utf-8",
            )

            result = load_scenarios(root, suite="real", include_known_issues=False)

            self.assertEqual([scenario.scenario_id for scenario in result.runnable], ["runnable"])
            self.assertEqual([scenario.scenario_id for scenario in result.planned], ["planned_case"])
            self.assertEqual([scenario.scenario_id for scenario in result.known_issues], ["known_issue_case"])

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

    def test_required_capability_group_accepts_any_member(self) -> None:
        assertions = ScenarioAssertions(required_capability_groups=[["observe.poi", "world.poi.read"]])
        observed = ObservedScenarioResult(
            final_status="completed",
            selected_capability_ids=["world.poi.read"],
            confirmation_expected=False,
            final_reply="Ready.",
            duration_ms=200,
        )

        category, detail = evaluate_assertions(assertions, observed)

        self.assertIsNone(category)
        self.assertIsNone(detail)

    def test_missing_required_capability_group_is_reported_explicitly(self) -> None:
        assertions = ScenarioAssertions(required_capability_groups=[["observe.technical", "carpet.observability.read"]])
        observed = ObservedScenarioResult(
            final_status="completed",
            selected_capability_ids=[],
            confirmation_expected=False,
            final_reply="Ready.",
            duration_ms=200,
        )

        category, detail = evaluate_assertions(assertions, observed)

        self.assertEqual(category, "missing_required_capability")
        self.assertIn("observe.technical | carpet.observability.read", detail or "")

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
