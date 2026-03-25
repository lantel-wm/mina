from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mina_agent.dev import cli
from mina_agent.dev.cli import ScenarioExecutionRecord
from mina_agent.dev.scenarios import HeadlessScenario, ScenarioLoadResult


class HeadlessCliTests(unittest.TestCase):
    def test_recent_turns_filters_by_player(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            debug_dir = Path(tmpdir) / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "index.jsonl").write_text(
                "".join(
                    [
                        json.dumps(
                            {
                                "turn_id": "turn-a",
                                "session_ref": "session-1",
                                "player_name": "Steve",
                                "user_message": "hello",
                                "status": "completed",
                                "started_at": "2026-03-23T10:00:00Z",
                                "debug_dir": "/tmp/a",
                            }
                        )
                        + "\n",
                        json.dumps(
                            {
                                "turn_id": "turn-b",
                                "session_ref": "session-2",
                                "player_name": "Alex",
                                "user_message": "world",
                                "status": "completed",
                                "started_at": "2026-03-23T10:01:00Z",
                                "debug_dir": "/tmp/b",
                            }
                        )
                        + "\n",
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.recent_turns(
                    argparse.Namespace(
                        debug_dir=str(debug_dir),
                        limit=10,
                        player="Steve",
                        session=None,
                    )
                )

        self.assertEqual(code, 0)
        self.assertIn("turn-a", stdout.getvalue())
        self.assertNotIn("turn-b", stdout.getvalue())

    def test_promote_trace_seeds_assertions_from_legacy_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            debug_dir = root / "debug"
            bundle_dir = debug_dir / "turns" / "2026-03-23" / "120000__stub__turn-legacy"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "scenario.capture.json").write_text(
                json.dumps(
                    {
                        "scenario": {
                            "scenario_id": "turn-legacy",
                            "world_template": None,
                            "player_name": "Tester",
                            "message": "hello Mina",
                            "follow_up_messages": [],
                            "setup_commands": [],
                            "assertions": {
                                "expected_final_status": "completed",
                                "required_capability_ids": [],
                            },
                        },
                        "assertion_slots": {
                            "suggested_assertions": {
                                "expected_final_status": "completed",
                                "forbidden_statuses": ["failed"],
                                "required_capability_ids": ["game.player_snapshot.read"],
                                "forbidden_capability_ids": [],
                                "confirmation_expected": False,
                                "required_reply_substrings": [],
                                "forbidden_reply_substrings": [],
                                "max_duration_ms": 1234,
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (debug_dir / "index.jsonl").write_text(
                json.dumps({"turn_id": "turn-legacy", "debug_dir": str(bundle_dir)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            output_dir = root / "scenarios"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), mock.patch("mina_agent.dev.cli.find_repo_root", return_value=root):
                code = cli.promote_trace(
                    argparse.Namespace(
                        debug_dir="debug",
                        turn_id="turn-legacy",
                        suite="real",
                        scenario_id="promoted_case",
                        world_template="overworld_day_spawn",
                        output_dir="scenarios",
                        force=False,
                    )
                )

            promoted = json.loads((output_dir / "promoted_case.json").read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(promoted["suite"], "real")
        self.assertEqual(promoted["scenario_id"], "promoted_case")
        self.assertEqual(promoted["actors"][0]["name"], "Tester")
        self.assertEqual(promoted["turns"][0]["message"], "hello Mina")
        self.assertEqual(promoted["assertions"]["required_capability_ids"], ["game.player_snapshot.read"])
        self.assertEqual(promoted["assertions"]["max_duration_ms"], 1234)
        self.assertIn("promoted_case.json", stdout.getvalue())

    def test_defer_quality_review_never_runs_llm_judgement_during_run(self) -> None:
        scenario = HeadlessScenario.model_validate(
            {
                "suite": "real",
                "scenario_id": "real_codex_case",
                "world_template": "overworld_day_spawn",
                "quality_review": {
                    "enabled": True,
                    "judge": "codex",
                    "rubric_id": "companion_quality_golden",
                },
                "actors": [{"actor_id": "player", "name": "Steve", "role": "read_only"}],
                "turns": [{"actor_id": "player", "message": "hello"}],
            }
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cli.defer_quality_review(scenario)

        self.assertEqual(result.status, "deferred_user_review")
        self.assertIn("deferred", result.rationale or "")
        self.assertIn("quality review deferred", stdout.getvalue())

    def test_wait_for_dev_log_entry_preserves_following_lines_for_next_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            turns_log = Path(tmpdir) / "turns.jsonl"
            turns_log.write_text(
                "".join(
                    [
                        json.dumps(
                            {
                                "turn_id": "turn-1",
                                "status": "accepted",
                                "player_name": "Steve",
                                "user_message": "hello",
                            }
                        )
                        + "\n",
                        json.dumps(
                            {
                                "turn_id": "turn-1",
                                "status": "completed",
                                "player_name": "Steve",
                                "user_message": "hello",
                            }
                        )
                        + "\n",
                    ]
                ),
                encoding="utf-8",
            )

            accepted, offset = cli.wait_for_dev_log_entry(
                turns_log,
                0,
                1.0,
                lambda entry: entry.get("status") == "accepted",
                "accepted turn",
                "missing_accepted_turn",
            )
            completed, final_offset = cli.wait_for_dev_log_entry(
                turns_log,
                offset,
                1.0,
                lambda entry: entry.get("status") == "completed",
                "completed turn",
                "timeout",
            )

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(completed["status"], "completed")
        self.assertGreater(final_offset, offset)

    def test_run_functional_returns_nonzero_when_any_case_fails(self) -> None:
        args = argparse.Namespace(
            scenario_dir="unused",
            world_template_dir="unused",
            output_root="unused",
            scenario_id=[],
            agent_mode="stub",
            agent_port=0,
            server_ready_timeout=1.0,
            agent_ready_timeout=1.0,
            turn_timeout=1.0,
        )
        records = [
            ScenarioExecutionRecord(
                scenario_id="functional_case",
                suite="functional",
                expectation="required",
                runnable_status="runnable_now",
                outcome="behavior_gap",
                category="reply_assertion_failure",
                detail="bad reply",
                infra_failure=False,
                turn_ids=[],
                bundle_dirs=[],
                quality_review=None,
            )
        ]
        with mock.patch("mina_agent.dev.cli.execute_suite", return_value=(records, Path("/tmp/run"), ScenarioLoadResult([], [], []))):
            self.assertEqual(cli.run_functional(args), 1)

    def test_print_suite_summary_includes_real_report_paths(self) -> None:
        records = [
            ScenarioExecutionRecord(
                scenario_id="target_gap",
                suite="real",
                expectation="target_state",
                runnable_status="runnable_now",
                outcome="behavior_gap",
                category="reply_assertion_failure",
                detail="gap",
                infra_failure=False,
                turn_ids=[],
                bundle_dirs=[],
                quality_review=None,
                scenario_category="wiki",
            )
        ]

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.print_suite_summary("real", records, run_root=Path("/tmp/mina-real"))

        rendered = stdout.getvalue()
        self.assertIn("real summary: passed=0 infra_failures=0 behavior_gaps=1", rendered)
        self.assertIn("/tmp/mina-real/scorecard.md", rendered)
        self.assertIn("/tmp/mina-real/target_state_gaps.json", rendered)
        self.assertIn("real scenario categories: wiki=1", rendered)

    def test_run_real_exit_code_matches_failure_policy(self) -> None:
        args = argparse.Namespace(
            scenario_dir="unused",
            world_template_dir="unused",
            output_root="unused",
            scenario_id=[],
            agent_port=0,
            server_ready_timeout=1.0,
            agent_ready_timeout=1.0,
            turn_timeout=1.0,
            strict_real=False,
            include_known_issues=False,
            max_infra_failures=1,
            keep_full_artifacts=False,
        )
        base_load = ScenarioLoadResult([], [], [])
        cases = [
            (
                [
                    ScenarioExecutionRecord(
                        scenario_id="target_gap",
                        suite="real",
                        expectation="target_state",
                        runnable_status="runnable_now",
                        outcome="behavior_gap",
                        category="reply_assertion_failure",
                        detail="gap",
                        infra_failure=False,
                        turn_ids=[],
                        bundle_dirs=[],
                        quality_review=None,
                    )
                ],
                False,
                0,
            ),
            (
                [
                    ScenarioExecutionRecord(
                        scenario_id="required_gap",
                        suite="real",
                        expectation="required",
                        runnable_status="runnable_now",
                        outcome="behavior_gap",
                        category="reply_assertion_failure",
                        detail="gap",
                        infra_failure=False,
                        turn_ids=[],
                        bundle_dirs=[],
                        quality_review=None,
                    )
                ],
                False,
                1,
            ),
            (
                [
                    ScenarioExecutionRecord(
                        scenario_id="infra",
                        suite="real",
                        expectation="target_state",
                        runnable_status="runnable_now",
                        outcome="infra_failure",
                        category="startup_failure",
                        detail="agent boot failed",
                        infra_failure=True,
                        turn_ids=[],
                        bundle_dirs=[],
                        quality_review=None,
                    )
                ],
                False,
                1,
            ),
            (
                [
                    ScenarioExecutionRecord(
                        scenario_id="strict_gap",
                        suite="real",
                        expectation="target_state",
                        runnable_status="runnable_now",
                        outcome="behavior_gap",
                        category="reply_assertion_failure",
                        detail="gap",
                        infra_failure=False,
                        turn_ids=[],
                        bundle_dirs=[],
                        quality_review=None,
                    )
                ],
                True,
                1,
            ),
        ]

        for records, strict_real, expected in cases:
            args.strict_real = strict_real
            with self.subTest(records=records, strict_real=strict_real):
                with (
                    mock.patch("mina_agent.dev.cli.execute_suite", return_value=(records, Path("/tmp/run"), base_load)),
                    mock.patch("mina_agent.dev.cli.write_real_reports"),
                    mock.patch("mina_agent.dev.cli.prune_real_review_artifacts"),
                ):
                    self.assertEqual(cli.run_real(args), expected)

    def test_run_real_prunes_artifacts_by_default(self) -> None:
        args = argparse.Namespace(
            scenario_dir="unused",
            world_template_dir="unused",
            output_root="unused",
            scenario_id=[],
            agent_port=0,
            server_ready_timeout=1.0,
            agent_ready_timeout=1.0,
            turn_timeout=1.0,
            strict_real=False,
            include_known_issues=False,
            max_infra_failures=1,
            keep_full_artifacts=False,
        )

        with (
            mock.patch("mina_agent.dev.cli.execute_suite", return_value=([], Path("/tmp/run"), ScenarioLoadResult([], [], []))),
            mock.patch("mina_agent.dev.cli.write_real_reports"),
            mock.patch("mina_agent.dev.cli.prune_real_review_artifacts") as prune_mock,
        ):
            self.assertEqual(cli.run_real(args), 0)

        prune_mock.assert_called_once_with(Path("/tmp/run"))

    def test_run_real_can_keep_full_artifacts(self) -> None:
        args = argparse.Namespace(
            scenario_dir="unused",
            world_template_dir="unused",
            output_root="unused",
            scenario_id=[],
            agent_port=0,
            server_ready_timeout=1.0,
            agent_ready_timeout=1.0,
            turn_timeout=1.0,
            strict_real=False,
            include_known_issues=False,
            max_infra_failures=1,
            keep_full_artifacts=True,
        )

        with (
            mock.patch("mina_agent.dev.cli.execute_suite", return_value=([], Path("/tmp/run"), ScenarioLoadResult([], [], []))),
            mock.patch("mina_agent.dev.cli.write_real_reports"),
            mock.patch("mina_agent.dev.cli.prune_real_review_artifacts") as prune_mock,
        ):
            self.assertEqual(cli.run_real(args), 0)

        prune_mock.assert_not_called()

    def test_prune_real_review_artifacts_keeps_only_review_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir)
            server_dir = run_root / "01_group" / "server"
            (server_dir / "mina-dev").mkdir(parents=True, exist_ok=True)
            (server_dir / "logs").mkdir(parents=True, exist_ok=True)
            (server_dir / "world" / "region").mkdir(parents=True, exist_ok=True)
            (server_dir / "mina-dev" / "turns.jsonl").write_text("turn\n", encoding="utf-8")
            (server_dir / "logs" / "latest.log").write_text("latest\n", encoding="utf-8")
            (server_dir / "logs" / "debug.log").write_text("debug\n", encoding="utf-8")
            (server_dir / "world" / "region" / "r.0.0.mca").write_bytes(b"world")
            (server_dir / "server.properties").write_text("motd=x\n", encoding="utf-8")

            bundle_dir = (
                run_root
                / "01_group"
                / "scenario_a"
                / "agent_data"
                / "debug"
                / "turns"
                / "2026-03-23"
                / "120000__turn"
            )
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (run_root / "01_group" / "scenario_a" / "agent_data" / "debug" / "index.jsonl").write_text(
                '{"turn_id":"turn"}\n',
                encoding="utf-8",
            )
            for filename in (
                "summary.json",
                "events.jsonl",
                "request.start.json",
                "response.progress.jsonl",
                "response.final.json",
                "scenario.capture.json",
            ):
                (bundle_dir / filename).write_text(filename + "\n", encoding="utf-8")
            (bundle_dir / "prompts").mkdir(parents=True, exist_ok=True)
            (bundle_dir / "prompts" / "step_001.provider_input.json").write_text("prompt\n", encoding="utf-8")
            (run_root / "01_group" / "scenario_a" / "agent_data" / "mina_agent.db").write_text("db\n", encoding="utf-8")
            (run_root / "01_group" / "scenario_a" / "agent_data" / "sessions" / "abc").mkdir(parents=True, exist_ok=True)
            (run_root / "01_group" / "scenario_a" / "agent_data" / "sessions" / "abc" / "events.jsonl").write_text(
                "session\n",
                encoding="utf-8",
            )

            cli.prune_real_review_artifacts(run_root)

            self.assertTrue((server_dir / "mina-dev" / "turns.jsonl").exists())
            self.assertTrue((server_dir / "logs" / "latest.log").exists())
            self.assertFalse((server_dir / "logs" / "debug.log").exists())
            self.assertFalse((server_dir / "world").exists())
            self.assertFalse((server_dir / "server.properties").exists())

            pruned_agent_data = run_root / "01_group" / "scenario_a" / "agent_data"
            self.assertTrue((pruned_agent_data / "debug" / "index.jsonl").exists())
            self.assertTrue((bundle_dir / "summary.json").exists())
            self.assertTrue((bundle_dir / "response.final.json").exists())
            self.assertTrue((bundle_dir / "scenario.capture.json").exists())
            self.assertTrue((bundle_dir / "prompts" / "step_001.provider_input.json").exists())
            self.assertFalse((pruned_agent_data / "mina_agent.db").exists())
            self.assertFalse((pruned_agent_data / "sessions").exists())

    def test_resolve_template_asset_dir_can_inherit_world_from_source_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "base" / "world").mkdir(parents=True, exist_ok=True)
            (root / "base" / "template.json").write_text("{}", encoding="utf-8")
            (root / "derived").mkdir(parents=True, exist_ok=True)
            (root / "derived" / "template.json").write_text(
                json.dumps({"world_source_template": "base"}),
                encoding="utf-8",
            )

            resolved = cli.resolve_template_asset_dir(
                root,
                "derived",
                json.loads((root / "derived" / "template.json").read_text(encoding="utf-8")),
                asset_name="world",
            )

        self.assertEqual(resolved, root / "base" / "world")

    def test_write_real_reports_generates_gap_files_and_scorecard(self) -> None:
        records = [
            ScenarioExecutionRecord(
                scenario_id="passed_case",
                suite="real",
                expectation="target_state",
                runnable_status="runnable_now",
                outcome="passed",
                category=None,
                detail=None,
                infra_failure=False,
                turn_ids=["turn-1"],
                bundle_dirs=["/tmp/turn-1"],
                quality_review=None,
            ),
            ScenarioExecutionRecord(
                scenario_id="target_gap",
                suite="real",
                expectation="target_state",
                runnable_status="runnable_now",
                outcome="behavior_gap",
                category="reply_assertion_failure",
                detail="reply too weak",
                infra_failure=False,
                turn_ids=["turn-2"],
                bundle_dirs=["/tmp/turn-2"],
                quality_review=None,
            ),
            ScenarioExecutionRecord(
                scenario_id="planned_case",
                suite="real",
                expectation="target_state",
                runnable_status="planned",
                outcome="skipped_planned",
                category=None,
                detail=None,
                infra_failure=False,
                turn_ids=[],
                bundle_dirs=[],
                quality_review=None,
            ),
        ]
        load_result = ScenarioLoadResult(runnable=[], planned=[object()], known_issues=[])
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir)
            cli.write_real_reports(run_root, records, load_result)

            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            failing_cases = json.loads((run_root / "failing_cases.json").read_text(encoding="utf-8"))
            target_state_gaps = json.loads((run_root / "target_state_gaps.json").read_text(encoding="utf-8"))
            scorecard = (run_root / "scorecard.md").read_text(encoding="utf-8")

        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(summary["counts"]["behavior_gaps"], 1)
        self.assertEqual(summary["counts"]["skipped_planned"], 1)
        self.assertEqual(summary["runnable_count"], 2)
        self.assertEqual(summary["planned_count"], 1)
        self.assertEqual(summary["known_issue_count"], 0)
        self.assertEqual(len(failing_cases), 1)
        self.assertEqual(failing_cases[0]["scenario_id"], "target_gap")
        self.assertEqual(len(target_state_gaps), 1)
        self.assertIn("## Target-State Gaps", scorecard)
        self.assertIn("target_gap: reply_assertion_failure: reply too weak", scorecard)

    def test_suite_counts_summarizes_outcomes(self) -> None:
        counts = cli.suite_counts(
            [
                ScenarioExecutionRecord(
                    scenario_id="passed",
                    suite="functional",
                    expectation="required",
                    runnable_status="runnable_now",
                    outcome="passed",
                    category=None,
                    detail=None,
                    infra_failure=False,
                    turn_ids=[],
                    bundle_dirs=[],
                    quality_review=None,
                ),
                ScenarioExecutionRecord(
                    scenario_id="infra",
                    suite="functional",
                    expectation="required",
                    runnable_status="runnable_now",
                    outcome="infra_failure",
                    category="startup_failure",
                    detail="x",
                    infra_failure=True,
                    turn_ids=[],
                    bundle_dirs=[],
                    quality_review=None,
                ),
                ScenarioExecutionRecord(
                    scenario_id="gap",
                    suite="functional",
                    expectation="required",
                    runnable_status="runnable_now",
                    outcome="behavior_gap",
                    category="reply_assertion_failure",
                    detail="x",
                    infra_failure=False,
                    turn_ids=[],
                    bundle_dirs=[],
                    quality_review=None,
                ),
                ScenarioExecutionRecord(
                    scenario_id="planned",
                    suite="functional",
                    expectation="required",
                    runnable_status="planned",
                    outcome="skipped_planned",
                    category=None,
                    detail=None,
                    infra_failure=False,
                    turn_ids=[],
                    bundle_dirs=[],
                    quality_review=None,
                ),
                ScenarioExecutionRecord(
                    scenario_id="known",
                    suite="real",
                    expectation="known_issue",
                    runnable_status="runnable_now",
                    outcome="skipped_known_issue",
                    category=None,
                    detail=None,
                    infra_failure=False,
                    turn_ids=[],
                    bundle_dirs=[],
                    quality_review=None,
                ),
            ]
        )

        self.assertEqual(
            counts,
            {
                "passed": 1,
                "infra_failures": 1,
                "behavior_gaps": 1,
                "skipped_planned": 1,
                "skipped_known_issue": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
