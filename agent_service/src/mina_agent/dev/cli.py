from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

from mina_agent.debug import load_debug_index, resolve_turn_bundle
from mina_agent.dev.scenarios import (
    ActorSpec,
    HeadlessScenario,
    INFRA_FAILURE_CATEGORIES,
    ObservedScenarioResult,
    QualityReviewResult,
    ScenarioAssertions,
    ScenarioLoadResult,
    SuiteName,
    evaluate_assertions,
    is_infra_failure,
    load_scenarios,
)


DEFAULT_FUNCTIONAL_SCENARIO_DIR = Path("testing/headless/functional/scenarios")
DEFAULT_REAL_SCENARIO_DIR = Path("testing/headless/real/scenarios")
DEFAULT_WORLD_TEMPLATE_DIR = Path("testing/headless/world_templates")
DEFAULT_FUNCTIONAL_OUTPUT_ROOT = Path("tmp/headless/functional")
DEFAULT_REAL_OUTPUT_ROOT = Path("tmp/headless/real")
DEFAULT_AGENT_HOST = "127.0.0.1"
DEFAULT_AGENT_PORT = 0
DEFAULT_SERVER_READY_TIMEOUT = 180.0
DEFAULT_AGENT_READY_TIMEOUT = 30.0
DEFAULT_TURN_TIMEOUT = 180.0
DEFAULT_PROGRESS_HEARTBEAT = 5.0
REVIEW_SERVER_FILES = (Path("mina-dev/turns.jsonl"), Path("logs/latest.log"))
REVIEW_BUNDLE_FILENAMES = frozenset(
    {
        "summary.json",
        "events.jsonl",
        "request.start.json",
        "response.progress.jsonl",
        "response.final.json",
        "scenario.capture.json",
    }
)


@dataclass(slots=True)
class TurnRunResult:
    turn_id: str
    actor_id: str
    message: str
    bundle_dir: Path
    final_status: str
    final_reply: str
    confirmation_expected: bool
    selected_capability_ids: list[str]
    started_at: str | None
    ended_at: str | None


@dataclass(slots=True)
class ScenarioExecutionRecord:
    scenario_id: str
    suite: str
    expectation: str
    runnable_status: str
    outcome: str
    category: str | None
    detail: str | None
    infra_failure: bool
    turn_ids: list[str]
    bundle_dirs: list[str]
    quality_review: dict[str, Any] | None


class LineProcess:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str], name: str) -> None:
        self._command = command
        self._cwd = cwd
        self._env = env
        self._name = name
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._lines: list[str] = []
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            self._command,
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            normalized = line.rstrip("\n")
            self._lines.append(normalized)
            self._queue.put(normalized)

    def send_line(self, line: str) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"{self._name} is not running")
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

    def wait_for_substring(self, substring: str, timeout: float, *, progress_label: str | None = None) -> str:
        started = time.time()
        deadline = started + timeout
        next_heartbeat = started + DEFAULT_PROGRESS_HEARTBEAT
        while time.time() < deadline:
            if self.poll() is not None:
                raise RuntimeError(f"{self._name} exited before emitting expected output: {substring}")
            remaining = max(deadline - time.time(), 0.1)
            try:
                line = self._queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if progress_label is not None and time.time() >= next_heartbeat:
                    last_line = self._lines[-1] if self._lines else None
                    suffix = f" | last output: {last_line}" if last_line else ""
                    log_status(
                        f"{progress_label}... {int(time.time() - started)}s elapsed / {int(timeout)}s timeout{suffix}"
                    )
                    next_heartbeat = time.time() + DEFAULT_PROGRESS_HEARTBEAT
                continue
            if substring in line:
                return line
        raise TimeoutError(f"Timed out waiting for {self._name} output: {substring}")

    def terminate(self, *, graceful_line: str | None = None, timeout: float = 20.0) -> None:
        if self._process is None:
            return
        if graceful_line is not None and self._process.poll() is None:
            try:
                self.send_line(graceful_line)
            except Exception:
                pass
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_group(signal.SIGTERM)
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._terminate_group(signal.SIGKILL)
                self._process.wait(timeout=5.0)

    def poll(self) -> int | None:
        if self._process is None:
            return None
        return self._process.poll()

    def _terminate_group(self, sig: int) -> None:
        if self._process is None:
            return
        try:
            os.killpg(self._process.pid, sig)
        except ProcessLookupError:
            return


class ActiveRunDirectory:
    def __init__(self, repo_root: Path, materialized_run_dir: Path) -> None:
        self._repo_root = repo_root
        self._materialized_run_dir = materialized_run_dir
        self._active_run_dir = repo_root / "run"
        self._backup_dir = Path(tempfile.mkdtemp(prefix="mina-run-backup-"))
        self._had_existing_run = self._active_run_dir.exists()

    def activate(self) -> None:
        if self._had_existing_run:
            shutil.move(str(self._active_run_dir), str(self._backup_dir / "run"))
        shutil.copytree(self._materialized_run_dir, self._active_run_dir)
        backup_run_dir = self._backup_dir / "run"
        for name in ("mods", ".fabric", "config"):
            source = backup_run_dir / name
            target = self._active_run_dir / name
            if source.exists() and not target.exists():
                shutil.copytree(source, target)

    def sync_back(self) -> None:
        for name in ("mina-dev", "logs", "world", "server.properties", "eula.txt", "ops.json", "whitelist.json", "config"):
            source = self._active_run_dir / name
            target = self._materialized_run_dir / name
            if not source.exists():
                continue
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def restore(self) -> None:
        if self._active_run_dir.exists():
            shutil.rmtree(self._active_run_dir)
        backup_run_dir = self._backup_dir / "run"
        if backup_run_dir.exists():
            shutil.move(str(backup_run_dir), str(self._active_run_dir))
        try:
            shutil.rmtree(self._backup_dir)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mina headless testing and trace tooling.")
    subparsers = parser.add_subparsers(dest="command")

    functional_parser = subparsers.add_parser("run-functional", help="Run deterministic functional headless tests.")
    functional_parser.add_argument("--scenario-dir", default=str(DEFAULT_FUNCTIONAL_SCENARIO_DIR))
    functional_parser.add_argument("--world-template-dir", default=str(DEFAULT_WORLD_TEMPLATE_DIR))
    functional_parser.add_argument("--output-root", default=str(DEFAULT_FUNCTIONAL_OUTPUT_ROOT))
    functional_parser.add_argument("--scenario-id", action="append", default=[])
    functional_parser.add_argument("--agent-mode", choices=["stub", "real"], default="stub")
    functional_parser.add_argument("--agent-port", type=int, default=DEFAULT_AGENT_PORT)
    functional_parser.add_argument("--server-ready-timeout", type=float, default=DEFAULT_SERVER_READY_TIMEOUT)
    functional_parser.add_argument("--agent-ready-timeout", type=float, default=DEFAULT_AGENT_READY_TIMEOUT)
    functional_parser.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT)

    real_parser = subparsers.add_parser("run-real", help="Run the full real-model headless evaluation suite.")
    real_parser.add_argument("--scenario-dir", default=str(DEFAULT_REAL_SCENARIO_DIR))
    real_parser.add_argument("--world-template-dir", default=str(DEFAULT_WORLD_TEMPLATE_DIR))
    real_parser.add_argument("--output-root", default=str(DEFAULT_REAL_OUTPUT_ROOT))
    real_parser.add_argument("--scenario-id", action="append", default=[])
    real_parser.add_argument("--agent-port", type=int, default=DEFAULT_AGENT_PORT)
    real_parser.add_argument("--server-ready-timeout", type=float, default=DEFAULT_SERVER_READY_TIMEOUT)
    real_parser.add_argument("--agent-ready-timeout", type=float, default=DEFAULT_AGENT_READY_TIMEOUT)
    real_parser.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT)
    real_parser.add_argument("--strict-real", action="store_true")
    real_parser.add_argument("--include-known-issues", action="store_true")
    real_parser.add_argument("--max-infra-failures", type=int, default=1)
    real_parser.add_argument("--keep-full-artifacts", action="store_true")

    recent_parser = subparsers.add_parser("recent-turns", help="List recent debug-traced turns.")
    recent_parser.add_argument("--debug-dir", default="agent_service/data/debug")
    recent_parser.add_argument("--limit", type=int, default=10)
    recent_parser.add_argument("--player")
    recent_parser.add_argument("--session")

    promote_parser = subparsers.add_parser("promote-trace", help="Promote a captured trace bundle into a checked-in scenario.")
    promote_parser.add_argument("--debug-dir", default="agent_service/data/debug")
    promote_parser.add_argument("--turn-id", required=True)
    promote_parser.add_argument("--suite", choices=["functional", "real"], default="real")
    promote_parser.add_argument("--scenario-id")
    promote_parser.add_argument("--world-template")
    promote_parser.add_argument("--output-dir")
    promote_parser.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "run-functional":
        return run_functional(args)
    if args.command == "run-real":
        return run_real(args)
    if args.command == "recent-turns":
        return recent_turns(args)
    if args.command == "promote-trace":
        return promote_trace(args)
    parser.print_help()
    return 1


def run_functional(args: argparse.Namespace) -> int:
    records, _, _ = execute_suite(
        suite="functional",
        scenario_dir=Path(args.scenario_dir),
        world_template_dir=Path(args.world_template_dir),
        output_root=Path(args.output_root),
        scenario_ids=args.scenario_id,
        agent_mode=args.agent_mode,
        agent_port=args.agent_port,
        server_ready_timeout=args.server_ready_timeout,
        agent_ready_timeout=args.agent_ready_timeout,
        turn_timeout=args.turn_timeout,
        include_known_issues=False,
        max_infra_failures=1,
    )
    failed = [record for record in records if record.outcome in {"infra_failure", "behavior_gap"}]
    print_suite_summary("functional", records)
    return 1 if failed else 0


def run_real(args: argparse.Namespace) -> int:
    records, run_root, load_result = execute_suite(
        suite="real",
        scenario_dir=Path(args.scenario_dir),
        world_template_dir=Path(args.world_template_dir),
        output_root=Path(args.output_root),
        scenario_ids=args.scenario_id,
        agent_mode="real",
        agent_port=args.agent_port,
        server_ready_timeout=args.server_ready_timeout,
        agent_ready_timeout=args.agent_ready_timeout,
        turn_timeout=args.turn_timeout,
        include_known_issues=args.include_known_issues,
        max_infra_failures=max(args.max_infra_failures, 1),
    )
    write_real_reports(run_root, records, load_result)
    if args.keep_full_artifacts:
        log_status("Keeping full run-real artifacts because --keep-full-artifacts was set")
    else:
        log_status("Pruning run-real output to review-only artifacts")
        prune_real_review_artifacts(run_root)
        log_status("run-real output pruned to review-only artifacts")
    print_suite_summary("real", records, run_root=run_root)

    infra_failures = [record for record in records if record.outcome == "infra_failure"]
    required_behavior_failures = [
        record
        for record in records
        if record.outcome == "behavior_gap" and record.expectation == "required"
    ]
    any_behavior_failures = [record for record in records if record.outcome == "behavior_gap"]

    if infra_failures:
        return 1
    if required_behavior_failures:
        return 1
    if args.strict_real and any_behavior_failures:
        return 1
    return 0


def execute_suite(
    *,
    suite: SuiteName,
    scenario_dir: Path,
    world_template_dir: Path,
    output_root: Path,
    scenario_ids: list[str],
    agent_mode: str,
    agent_port: int,
    server_ready_timeout: float,
    agent_ready_timeout: float,
    turn_timeout: float,
    include_known_issues: bool,
    max_infra_failures: int,
) -> tuple[list[ScenarioExecutionRecord], Path, ScenarioLoadResult]:
    repo_root = find_repo_root(Path.cwd())
    scenario_dir = repo_root / scenario_dir
    world_template_dir = repo_root / world_template_dir
    output_root = repo_root / output_root
    active_run_dir = repo_root / "run"
    if output_root == active_run_dir or active_run_dir in output_root.parents:
        raise RuntimeError(f"Output root must not live under the active run directory: {output_root}")

    effective_port = agent_port or find_free_port()
    load_result = load_scenarios(
        scenario_dir,
        suite=suite,
        scenario_ids=scenario_ids or None,
        include_known_issues=include_known_issues,
    )
    if not load_result.runnable and not load_result.planned and not load_result.known_issues:
        raise RuntimeError(f"No scenarios found in {scenario_dir}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = output_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    print(f"{suite} run output: {run_root}", flush=True)
    log_status(
        f"Loaded {suite} scenarios: runnable={len(load_result.runnable)} planned={len(load_result.planned)} "
        f"known_issues={len(load_result.known_issues)}"
    )

    records: list[ScenarioExecutionRecord] = []
    for planned in load_result.planned:
        records.append(
            ScenarioExecutionRecord(
                scenario_id=planned.scenario_id,
                suite=planned.suite,
                expectation=planned.expectation,
                runnable_status=planned.status,
                outcome="skipped_planned",
                category=None,
                detail=None,
                infra_failure=False,
                turn_ids=[],
                bundle_dirs=[],
                quality_review=None,
            )
        )
    for known_issue in load_result.known_issues:
        records.append(
            ScenarioExecutionRecord(
                scenario_id=known_issue.scenario_id,
                suite=known_issue.suite,
                expectation=known_issue.expectation,
                runnable_status=known_issue.status,
                outcome="skipped_known_issue",
                category=None,
                detail=None,
                infra_failure=False,
                turn_ids=[],
                bundle_dirs=[],
                quality_review=None,
            )
        )

    grouped = group_scenarios(load_result.runnable)
    infra_failures = 0
    total_groups = len(grouped)
    total_runnable = len(load_result.runnable)
    scenario_counter = 0
    for group_index, (group_key, group_scenarios_list) in enumerate(grouped.items(), start=1):
        world_template = group_scenarios_list[0].world_template
        group_root = run_root / f"{group_index:02d}_{group_key}"
        server_run_dir = group_root / "server"
        log_status(
            f"[{group_index}/{total_groups}] Preparing group {group_key} "
            f"(template={world_template}, scenarios={len(group_scenarios_list)})"
        )
        prepare_server_run_directory(server_run_dir, world_template_dir, world_template, group_scenarios_list)
        run_swap = ActiveRunDirectory(repo_root, server_run_dir)
        run_swap.activate()
        cleanup_root_eula = ensure_repo_root_eula(repo_root)

        server_env = os.environ.copy()
        server_env["MINA_AGENT_BASE_URL"] = f"http://{DEFAULT_AGENT_HOST}:{effective_port}"
        server_env["MINA_CONFIG_FILE"] = str((active_run_dir / "config" / "mina.properties").resolve())
        server = LineProcess(
            ["./gradlew", "runServer", "--no-daemon"],
            cwd=repo_root,
            env=server_env,
            name="server",
        )
        log_status(f"[{group_index}/{total_groups}] Starting Fabric server")
        server.start()
        group_failed = False
        try:
            server.wait_for_substring(
                "Done (",
                server_ready_timeout,
                progress_label=f"[{group_index}/{total_groups}] Waiting for Fabric server startup",
            )
            log_status(f"[{group_index}/{total_groups}] Fabric server ready")
            active_dev_log = active_run_dir / "mina-dev" / "turns.jsonl"
            dev_log_offset = 0
            known_actors: set[str] = set()
            for scenario in group_scenarios_list:
                scenario_counter += 1
                scenario_root = group_root / scenario.scenario_id
                scenario_root.mkdir(parents=True, exist_ok=True)
                agent_data_dir = scenario_root / "agent_data"
                log_status(
                    f"Starting scenario {scenario_counter}/{total_runnable}: {scenario.scenario_id} "
                    f"({len(scenario.turns)} turn(s), actors={', '.join(actor.name for actor in scenario.actors)})"
                )
                agent = start_agent_process(repo_root=repo_root, mode=agent_mode, agent_data_dir=agent_data_dir, port=effective_port)
                try:
                    log_status(f"Scenario {scenario.scenario_id}: starting {agent_mode} agent on port {effective_port}")
                    ensure_agent_ready(
                        mode=agent_mode,
                        host=DEFAULT_AGENT_HOST,
                        port=effective_port,
                        timeout=agent_ready_timeout,
                        progress_label=f"Scenario {scenario.scenario_id}: waiting for agent health",
                    )
                    log_status(f"Scenario {scenario.scenario_id}: agent ready")
                    result, dev_log_offset = run_scenario(
                        scenario=scenario,
                        server=server,
                        known_actors=known_actors,
                        dev_log_path=active_dev_log,
                        dev_log_offset=dev_log_offset,
                        debug_dir=agent_data_dir / "debug",
                        timeout=turn_timeout,
                    )
                    quality_review = defer_quality_review(scenario)
                    observed = build_observed_result(result, quality_review)
                    failure_category, failure_detail = classify_structural_failure(result)
                    if failure_category is None:
                        failure_category, failure_detail = evaluate_assertions(scenario.assertions, observed)
                    if failure_category is None:
                        print(f"[PASS] {scenario.scenario_id} -> {result[-1].turn_id}", flush=True)
                        records.append(
                            ScenarioExecutionRecord(
                                scenario_id=scenario.scenario_id,
                                suite=scenario.suite,
                                expectation=scenario.expectation,
                                runnable_status=scenario.status,
                                outcome="passed",
                                category=None,
                                detail=None,
                                infra_failure=False,
                                turn_ids=[item.turn_id for item in result],
                                bundle_dirs=[str(item.bundle_dir) for item in result],
                                quality_review=quality_review.model_dump(by_alias=True) if quality_review is not None else None,
                            )
                        )
                    else:
                        infra = is_infra_failure(failure_category)
                        if infra:
                            infra_failures += 1
                            outcome = "infra_failure"
                        else:
                            outcome = "behavior_gap"
                        print(
                            f"[FAIL] {scenario.scenario_id} [{failure_category}] {failure_detail}\n"
                            f"  turn_id={result[-1].turn_id}\n"
                            f"  bundle={result[-1].bundle_dir}",
                            file=sys.stderr,
                            flush=True,
                        )
                        records.append(
                            ScenarioExecutionRecord(
                                scenario_id=scenario.scenario_id,
                                suite=scenario.suite,
                                expectation=scenario.expectation,
                                runnable_status=scenario.status,
                                outcome=outcome,
                                category=failure_category,
                                detail=failure_detail,
                                infra_failure=infra,
                                turn_ids=[item.turn_id for item in result],
                                bundle_dirs=[str(item.bundle_dir) for item in result],
                                quality_review=quality_review.model_dump(by_alias=True) if quality_review is not None else None,
                            )
                        )
                        if infra and infra_failures >= max_infra_failures:
                            group_failed = True
                            break
                except Exception as exc:  # noqa: BLE001
                    category, detail = normalize_exception(exc)
                    infra = is_infra_failure(category)
                    if infra:
                        infra_failures += 1
                    print(f"[FAIL] {scenario.scenario_id} [{category}] {detail}", file=sys.stderr, flush=True)
                    records.append(
                        ScenarioExecutionRecord(
                            scenario_id=scenario.scenario_id,
                            suite=scenario.suite,
                            expectation=scenario.expectation,
                            runnable_status=scenario.status,
                            outcome="infra_failure" if infra else "behavior_gap",
                            category=category,
                            detail=detail,
                            infra_failure=infra,
                            turn_ids=[],
                            bundle_dirs=[],
                            quality_review=None,
                        )
                    )
                    if infra and infra_failures >= max_infra_failures:
                        group_failed = True
                        break
                finally:
                    agent.terminate(timeout=5.0)
            if group_failed:
                break
        finally:
            server.terminate(graceful_line="stop")
            run_swap.sync_back()
            run_swap.restore()
            cleanup_root_eula()

    return records, run_root, load_result


def run_scenario(
    *,
    scenario: HeadlessScenario,
    server: LineProcess,
    known_actors: set[str],
    dev_log_path: Path,
    dev_log_offset: int,
    debug_dir: Path,
    timeout: float,
) -> tuple[list[TurnRunResult], int]:
    for actor in scenario.actors:
        ensure_actor_spawned(server, actor, known_actors)
    for command in scenario.setup_commands:
        server.send_line(command)
        time.sleep(0.2)

    results: list[TurnRunResult] = []
    current_offset = dev_log_offset
    for turn_index, turn in enumerate(scenario.turns):
        actor = scenario.actor(turn.actor_id)
        log_status(
            f"Scenario {scenario.scenario_id}: turn {turn_index + 1}/{len(scenario.turns)} "
            f"as {actor.name}: {truncate_for_status(turn.message)}"
        )
        for command in turn.setup_commands_before:
            server.send_line(command)
            time.sleep(0.2)
        normalized_message = normalize_message(turn.message)
        current_offset = read_jsonl_offset(dev_log_path)
        server.send_line(f"execute as {actor.name} run mina {normalized_message}")
        accepted, current_offset = wait_for_dev_log_entry(
            dev_log_path,
            current_offset,
            timeout,
            lambda entry: (
                entry.get("status") == "accepted"
                and entry.get("player_name") == actor.name
                and entry.get("user_message") == normalized_message
            ),
            "accepted turn",
            "missing_accepted_turn",
            progress_label=f"Scenario {scenario.scenario_id}: waiting for accepted turn {turn_index + 1}",
        )
        turn_id = str(accepted["turn_id"])
        log_status(f"Scenario {scenario.scenario_id}: accepted turn {turn_index + 1} -> {turn_id}")
        completed, current_offset = wait_for_dev_log_entry(
            dev_log_path,
            current_offset,
            timeout,
            lambda entry: entry.get("turn_id") == turn_id and entry.get("status") in {"completed", "failed"},
            "completed turn",
            "timeout",
            progress_label=f"Scenario {scenario.scenario_id}: waiting for completed turn {turn_index + 1}",
        )
        bundle_dir = wait_for_bundle(debug_dir, turn_id, timeout)
        if bundle_dir is None:
            raise RuntimeError(f"missing_trace_bundle: no bundle for turn {turn_id}")
        log_status(f"Scenario {scenario.scenario_id}: bundle ready for turn {turn_index + 1} -> {bundle_dir}")
        enrich_capture(bundle_dir, scenario, turn_index)
        final_payload = json.loads((bundle_dir / "response.final.json").read_text(encoding="utf-8"))
        capture = json.loads((bundle_dir / "scenario.capture.json").read_text(encoding="utf-8"))
        turn_result = TurnRunResult(
            turn_id=turn_id,
            actor_id=turn.actor_id,
            message=normalized_message,
            bundle_dir=bundle_dir,
            final_status=str(final_payload.get("status") or "unknown"),
            final_reply=str(final_payload.get("final_reply") or ""),
            confirmation_expected=bool(final_payload.get("pending_confirmation_id")),
            selected_capability_ids=list(capture.get("selected_capability_ids") or []),
            started_at=accepted.get("started_at"),
            ended_at=completed.get("ended_at"),
        )
        if turn.assertions_override is not None:
            override_review = QualityReviewResult(status="skipped_disabled")
            observed = ObservedScenarioResult(
                final_status=turn_result.final_status,
                selected_capability_ids=turn_result.selected_capability_ids,
                confirmation_expected=turn_result.confirmation_expected,
                final_reply=turn_result.final_reply,
                duration_ms=duration_ms(turn_result.started_at, turn_result.ended_at),
                quality_review=override_review,
            )
            category, detail = evaluate_assertions(turn.assertions_override, observed)
            if category is not None:
                raise RuntimeError(f"{category}: turn-level assertion failed for {scenario.scenario_id}: {detail}")
        results.append(turn_result)
    return results, current_offset


def ensure_actor_spawned(server: LineProcess, actor: ActorSpec, known_actors: set[str]) -> None:
    if actor.actor_id in known_actors:
        return
    log_status(f"Spawning fake player {actor.name} (role={actor.role})")
    server.send_line(f"player {actor.name} spawn")
    server.wait_for_substring(
        f"{actor.name} joined the game",
        30.0,
        progress_label=f"Waiting for fake player {actor.name} to join",
    )
    for command in actor.spawn_commands:
        server.send_line(command)
        time.sleep(0.2)
    known_actors.add(actor.actor_id)
    log_status(f"Fake player {actor.name} ready")


def build_observed_result(results: list[TurnRunResult], quality_review: QualityReviewResult | None) -> ObservedScenarioResult:
    selected_capability_ids: list[str] = []
    seen: set[str] = set()
    for result in results:
        for capability_id in result.selected_capability_ids:
            if capability_id not in seen:
                seen.add(capability_id)
                selected_capability_ids.append(capability_id)
    return ObservedScenarioResult(
        final_status=results[-1].final_status,
        selected_capability_ids=selected_capability_ids,
        confirmation_expected=results[-1].confirmation_expected,
        final_reply=results[-1].final_reply,
        duration_ms=duration_ms(results[0].started_at, results[-1].ended_at),
        quality_review=quality_review,
    )


def defer_quality_review(scenario: HeadlessScenario) -> QualityReviewResult:
    if not scenario.quality_review.enabled:
        return QualityReviewResult(status="skipped_disabled")
    log_status(
        f"Scenario {scenario.scenario_id}: quality review deferred. "
        "run-real never performs Codex/LLM judgement during execution; review it manually after the suite finishes."
    )
    return QualityReviewResult(
        status="deferred_user_review",
        rationale="Manual Codex review is intentionally deferred until after run-real completes.",
    )


def classify_structural_failure(results: list[TurnRunResult]) -> tuple[str | None, str | None]:
    for result in results:
        events_path = result.bundle_dir / "events.jsonl"
        for event in read_jsonl(events_path):
            payload = event.get("payload") or {}
            if event.get("event_type") == "capability_rejected" and payload.get("reason") == "unknown_capability":
                return "unknown_capability_rejection", f"unknown capability selected in turn {result.turn_id}"
        final_payload = json.loads((result.bundle_dir / "response.final.json").read_text(encoding="utf-8"))
        if final_payload.get("reason") == "unknown_capability":
            return "unknown_capability_rejection", f"unknown capability selected in turn {result.turn_id}"
    if results[-1].final_status == "failed":
        final_payload = json.loads((results[-1].bundle_dir / "response.final.json").read_text(encoding="utf-8"))
        reason = final_payload.get("reason") or final_payload.get("error") or results[-1].final_reply
        return "runtime_exception", f"final turn failed: {reason}"
    return None, None


def recent_turns(args: argparse.Namespace) -> int:
    debug_dir = Path(args.debug_dir)
    entries = load_debug_index(debug_dir)
    filtered = [
        entry
        for entry in entries
        if (not args.player or entry.get("player_name") == args.player)
        and (not args.session or entry.get("session_ref") == args.session)
    ]
    for entry in filtered[-args.limit:]:
        print(
            f"{entry.get('started_at') or '-'} "
            f"{entry.get('status') or '-'} "
            f"{entry.get('turn_id') or '-'} "
            f"{entry.get('player_name') or '-'} "
            f"{entry.get('user_message') or '-'} "
            f"{entry.get('debug_dir') or '-'}"
        )
    return 0


def promote_trace(args: argparse.Namespace) -> int:
    repo_root = find_repo_root(Path.cwd())
    debug_dir = repo_root / Path(args.debug_dir)
    bundle_dir = resolve_turn_bundle(debug_dir, args.turn_id)
    if bundle_dir is None:
        print(f"Could not resolve turn bundle for {args.turn_id}", file=sys.stderr)
        return 1

    capture_path = bundle_dir / "scenario.capture.json"
    if not capture_path.exists():
        print(f"Scenario capture missing: {capture_path}", file=sys.stderr)
        return 1

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    raw_scenario = dict(capture.get("scenario") or {})
    suggested_assertions = ((capture.get("assertion_slots") or {}).get("suggested_assertions") or {})
    if should_seed_assertions_from_capture(raw_scenario) and isinstance(suggested_assertions, dict):
        raw_scenario["assertions"] = suggested_assertions
    raw_scenario["suite"] = args.suite
    raw_scenario["expectation"] = "required" if args.suite == "functional" else "target_state"
    raw_scenario["status"] = "runnable_now"
    raw_scenario["quality_review"] = raw_scenario.get("quality_review") or {"enabled": False}
    if args.world_template is not None:
        raw_scenario["world_template"] = args.world_template
    if not raw_scenario.get("world_template"):
        print("Promoted scenarios require --world-template when the capture has no world_template.", file=sys.stderr)
        return 1
    if args.scenario_id is not None:
        raw_scenario["scenario_id"] = args.scenario_id
    scenario = HeadlessScenario.model_validate(raw_scenario)

    if args.output_dir is not None:
        output_dir = repo_root / Path(args.output_dir)
    elif args.suite == "functional":
        output_dir = repo_root / DEFAULT_FUNCTIONAL_SCENARIO_DIR
    else:
        output_dir = repo_root / DEFAULT_REAL_SCENARIO_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{scenario.scenario_id}.json"
    if output_path.exists() and not args.force:
        print(f"Refusing to overwrite existing scenario without --force: {output_path}", file=sys.stderr)
        return 1
    output_path.write_text(json.dumps(scenario.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output_path)
    return 0


def start_agent_process(*, repo_root: Path, mode: str, agent_data_dir: Path, port: int) -> LineProcess:
    env = os.environ.copy()
    env["MINA_AGENT_DEBUG_ENABLED"] = "1"
    env["MINA_AGENT_DATA_DIR"] = str(agent_data_dir)
    env["MINA_AGENT_PORT"] = str(port)
    if mode == "stub":
        command = [sys.executable, "-m", "mina_agent.dev.stub_agent", "--host", DEFAULT_AGENT_HOST, "--port", str(port)]
    else:
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "mina_agent.main:app",
            "--app-dir",
            str(repo_root / "agent_service" / "src"),
            "--host",
            DEFAULT_AGENT_HOST,
            "--port",
            str(port),
        ]
    process = LineProcess(command, cwd=repo_root, env=env, name="agent")
    process.start()
    return process


def should_seed_assertions_from_capture(raw_scenario: dict[str, Any]) -> bool:
    return not {"suite", "actors", "turns"}.issubset(raw_scenario.keys())


def ensure_agent_ready(*, mode: str, host: str, port: int, timeout: float, progress_label: str | None = None) -> None:
    started = time.time()
    deadline = started + timeout
    next_heartbeat = started + DEFAULT_PROGRESS_HEARTBEAT
    last_error: Exception | None = None
    opener = build_opener(ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(f"http://{host}:{port}/healthz", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if mode == "real" and not payload.get("provider_configured"):
                raise RuntimeError("provider_configured=false")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if progress_label is not None and time.time() >= next_heartbeat:
                detail = f" last error: {last_error}" if last_error is not None else ""
                log_status(f"{progress_label}... {int(time.time() - started)}s elapsed / {int(timeout)}s timeout.{detail}")
                next_heartbeat = time.time() + DEFAULT_PROGRESS_HEARTBEAT
            time.sleep(0.2)
    raise RuntimeError(f"startup_failure: agent health check failed: {last_error}")


def wait_for_dev_log_entry(
    path: Path,
    offset: int,
    timeout: float,
    predicate: Any,
    label: str,
    failure_category: str,
    progress_label: str | None = None,
) -> tuple[dict[str, Any], int]:
    started = time.time()
    deadline = started + timeout
    current_offset = offset
    next_heartbeat = started + DEFAULT_PROGRESS_HEARTBEAT
    while time.time() < deadline:
        matched = False
        while True:
            entry, next_offset = read_next_jsonl_entry(path, current_offset)
            if entry is None:
                break
            current_offset = next_offset
            matched = True
            if predicate(entry):
                return entry, current_offset
        if not matched and progress_label is not None and time.time() >= next_heartbeat:
            log_status(f"{progress_label}... {int(time.time() - started)}s elapsed / {int(timeout)}s timeout")
            next_heartbeat = time.time() + DEFAULT_PROGRESS_HEARTBEAT
        time.sleep(0.1)
    raise RuntimeError(f"{failure_category}: did not observe {label} in {path}")


def wait_for_bundle(debug_dir: Path, turn_id: str, timeout: float) -> Path | None:
    started = time.time()
    deadline = started + timeout
    next_heartbeat = started + DEFAULT_PROGRESS_HEARTBEAT
    while time.time() < deadline:
        bundle_dir = resolve_turn_bundle(debug_dir, turn_id)
        if bundle_dir is not None and (bundle_dir / "scenario.capture.json").exists() and (bundle_dir / "response.final.json").exists():
            return bundle_dir
        if time.time() >= next_heartbeat:
            log_status(f"Waiting for debug bundle {turn_id}... {int(time.time() - started)}s elapsed / {int(timeout)}s timeout")
            next_heartbeat = time.time() + DEFAULT_PROGRESS_HEARTBEAT
        time.sleep(0.1)
    return None


def enrich_capture(bundle_dir: Path, scenario: HeadlessScenario, turn_index: int) -> None:
    capture_path = bundle_dir / "scenario.capture.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["scenario"] = scenario.model_dump()
    capture["source_turn_index"] = turn_index
    capture_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def group_scenarios(scenarios: list[HeadlessScenario]) -> dict[str, list[HeadlessScenario]]:
    grouped: dict[str, list[HeadlessScenario]] = {}
    for scenario in scenarios:
        grouped.setdefault(scenario_group_key(scenario), []).append(scenario)
    return grouped


def scenario_group_key(scenario: HeadlessScenario) -> str:
    role_key = ",".join(f"{actor.name}:{actor.role}:{int(actor.operator)}:{int(actor.experimental)}" for actor in scenario.actors)
    feature_key = f"exp{int(scenario.feature_flags.enable_experimental)}_dyn{int(scenario.feature_flags.enable_dynamic_scripting)}"
    return f"{scenario.world_template}__{feature_key}__{short_hash(role_key)}"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def prepare_server_run_directory(
    server_run_dir: Path,
    world_template_root: Path,
    world_template: str,
    scenarios: list[HeadlessScenario],
) -> None:
    materialize_run_directory(world_template_root, world_template, server_run_dir)
    write_mina_properties(server_run_dir, scenarios)


def materialize_run_directory(world_template_root: Path, template_id: str, run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    template_dir = world_template_root / template_id
    repo_root = world_template_root.parents[2]
    seed_run_dir = repo_root / "run"
    metadata_path = template_dir / "template.json"
    if not metadata_path.exists():
        raise RuntimeError(f"startup_failure: missing world template metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for name, source in (
        ("world", resolve_template_asset_dir(world_template_root, template_id, metadata, asset_name="world")),
        ("config", template_dir / "config"),
    ):
        if source.exists():
            shutil.copytree(source, run_dir / name)
    for name, fallback in (
        ("eula.txt", "#By changing the setting below to TRUE you are indicating your agreement to our EULA (https://aka.ms/MinecraftEULA).\neula=TRUE\n"),
        ("ops.json", "[]\n"),
        ("whitelist.json", "[]\n"),
        ("banned-ips.json", "[]\n"),
        ("banned-players.json", "[]\n"),
    ):
        source = seed_run_dir / name
        target = run_dir / name
        if source.exists():
            shutil.copy2(source, target)
        else:
            target.write_text(fallback, encoding="utf-8")
    base_server_properties = load_properties_file(seed_run_dir / "server.properties")
    base_server_properties.update({str(key): str(value) for key, value in (metadata.get("server_properties") or {}).items()})
    write_server_properties(run_dir / "server.properties", base_server_properties)


def resolve_template_asset_dir(
    world_template_root: Path,
    template_id: str,
    metadata: dict[str, Any],
    *,
    asset_name: str,
    seen: set[str] | None = None,
) -> Path:
    template_dir = world_template_root / template_id
    direct = template_dir / asset_name
    if direct.exists():
        return direct

    source_template = str(metadata.get(f"{asset_name}_source_template") or metadata.get("world_source_template") or "").strip()
    if not source_template:
        return direct
    if seen is None:
        seen = set()
    if template_id in seen:
        raise RuntimeError(f"startup_failure: cyclic template source detected for {template_id}")
    seen.add(template_id)

    source_template_dir = world_template_root / source_template
    source_metadata_path = source_template_dir / "template.json"
    if not source_metadata_path.exists():
        raise RuntimeError(f"startup_failure: missing source template metadata: {source_metadata_path}")
    source_metadata = json.loads(source_metadata_path.read_text(encoding='utf-8'))
    return resolve_template_asset_dir(world_template_root, source_template, source_metadata, asset_name=asset_name, seen=seen)


def write_mina_properties(run_dir: Path, scenarios: list[HeadlessScenario]) -> None:
    config_dir = run_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "mina.properties"
    lines = [
        "# Mina headless suite config",
        f"mina.enable_experimental={str(scenarios[0].feature_flags.enable_experimental).lower()}",
        f"mina.enable_dynamic_scripting={str(scenarios[0].feature_flags.enable_dynamic_scripting).lower()}",
    ]
    for actor in merged_actors(scenarios):
        if actor.role != "read_only":
            lines.append(f"role.override.{offline_player_uuid(actor.name)}={actor.role}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_ops_file(run_dir / "ops.json", merged_actors(scenarios))


def merged_actors(scenarios: list[HeadlessScenario]) -> list[ActorSpec]:
    merged: dict[str, ActorSpec] = {}
    for scenario in scenarios:
        for actor in scenario.actors:
            merged.setdefault(actor.name, actor)
    return list(merged.values())


def write_ops_file(target: Path, actors: list[ActorSpec]) -> None:
    ops = []
    for actor in actors:
        if actor.operator or actor.experimental:
            ops.append(
                {
                    "uuid": offline_player_uuid(actor.name),
                    "name": actor.name,
                    "level": 4,
                    "bypassesPlayerLimit": False,
                }
            )
    target.write_text(json.dumps(ops, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def offline_player_uuid(name: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def ensure_repo_root_eula(repo_root: Path) -> Any:
    target = repo_root / "eula.txt"
    if target.exists():
        return lambda: None
    source = repo_root / "run" / "eula.txt"
    if source.exists():
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        target.write_text(
            "#By changing the setting below to TRUE you are indicating your agreement to our EULA (https://aka.ms/MinecraftEULA).\n"
            "eula=TRUE\n",
            encoding="utf-8",
        )

    def cleanup() -> None:
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    return cleanup


def write_server_properties(target: Path, overrides: dict[str, Any]) -> None:
    properties: dict[str, str] = {
        "accepts-transfers": "false",
        "allow-flight": "false",
        "difficulty": "hard",
        "enable-query": "false",
        "enable-rcon": "false",
        "enforce-secure-profile": "false",
        "gamemode": "survival",
        "level-name": "world",
        "level-type": "minecraft:normal",
        "max-players": "20",
        "motd": "mina headless",
        "online-mode": "false",
        "op-permission-level": "4",
        "pause-when-empty-seconds": "0",
        "pvp": "true",
        "server-ip": "",
        "server-port": "25565",
        "simulation-distance": "12",
        "spawn-protection": "0",
        "view-distance": "12",
    }
    for key, value in overrides.items():
        properties[str(key)] = str(value)
    lines = ["# Mina headless server properties"]
    lines.extend(f"{key}={properties[key]}" for key in sorted(properties))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_properties_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def write_real_reports(run_root: Path, records: list[ScenarioExecutionRecord], load_result: ScenarioLoadResult) -> None:
    del load_result
    runnable_count = sum(1 for record in records if record.runnable_status == "runnable_now")
    planned_count = sum(1 for record in records if record.runnable_status == "planned")
    known_issue_count = sum(1 for record in records if record.expectation == "known_issue")
    summary = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "counts": {
            "passed": sum(1 for record in records if record.outcome == "passed"),
            "infra_failures": sum(1 for record in records if record.outcome == "infra_failure"),
            "behavior_gaps": sum(1 for record in records if record.outcome == "behavior_gap"),
            "skipped_planned": sum(1 for record in records if record.outcome == "skipped_planned"),
            "skipped_known_issue": sum(1 for record in records if record.outcome == "skipped_known_issue"),
        },
        "runnable_count": runnable_count,
        "planned_count": planned_count,
        "known_issue_count": known_issue_count,
        "records": [asdict(record) for record in records],
    }
    (run_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failing_cases = [asdict(record) for record in records if record.outcome in {"infra_failure", "behavior_gap"}]
    (run_root / "failing_cases.json").write_text(json.dumps(failing_cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    target_state_gaps = [
        asdict(record)
        for record in records
        if record.outcome == "behavior_gap" and record.expectation in {"target_state", "known_issue"}
    ]
    (run_root / "target_state_gaps.json").write_text(json.dumps(target_state_gaps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Real Suite Scorecard",
        "",
        f"- Passed: {summary['counts']['passed']}",
        f"- Infra failures: {summary['counts']['infra_failures']}",
        f"- Behavior gaps: {summary['counts']['behavior_gaps']}",
        f"- Skipped planned: {summary['counts']['skipped_planned']}",
        f"- Skipped known issues: {summary['counts']['skipped_known_issue']}",
        "",
        "## Infra Failures",
    ]
    infra = [record for record in records if record.outcome == "infra_failure"]
    if infra:
        lines.extend([f"- {record.scenario_id}: {record.category}: {record.detail}" for record in infra])
    else:
        lines.append("- none")
    lines.extend(["", "## Required Failures"])
    required_failures = [record for record in records if record.outcome == "behavior_gap" and record.expectation == "required"]
    if required_failures:
        lines.extend([f"- {record.scenario_id}: {record.category}: {record.detail}" for record in required_failures])
    else:
        lines.append("- none")
    lines.extend(["", "## Target-State Gaps"])
    if target_state_gaps:
        lines.extend([f"- {record['scenario_id']}: {record['category']}: {record['detail']}" for record in target_state_gaps])
    else:
        lines.append("- none")
    lines.extend(["", "## Known Issues Still Reproducing"])
    known_issue_failures = [record for record in records if record.outcome == "behavior_gap" and record.expectation == "known_issue"]
    if known_issue_failures:
        lines.extend([f"- {record.scenario_id}: {record.category}: {record.detail}" for record in known_issue_failures])
    else:
        lines.append("- none")
    (run_root / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_next_jsonl_entry(path: Path, offset: int) -> tuple[dict[str, Any] | None, int]:
    if not path.exists():
        return None, offset
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        line = handle.readline()
        new_offset = handle.tell()
    if not line.strip():
        return None, offset
    return json.loads(line), new_offset


def read_jsonl_offset(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_message(message: str) -> str:
    if "\n" in message or "\r" in message:
        return " ".join(part for part in message.splitlines() if part.strip())
    return message


def truncate_for_status(message: str, limit: int = 64) -> str:
    normalized = normalize_message(message)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "..."


def duration_ms(started_at: str | None, ended_at: str | None) -> int | None:
    start = parse_iso8601(started_at)
    end = parse_iso8601(ended_at)
    if start is None or end is None:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_AGENT_HOST, 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def parse_iso8601(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def normalize_exception(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    if ": " in message:
        category, detail = message.split(": ", 1)
        if category in INFRA_FAILURE_CATEGORIES or category.endswith("_failure") or category in {
            "unknown_capability_rejection",
            "reply_assertion_failure",
            "missing_required_capability",
            "quality_review_failure",
            "runtime_exception",
        }:
            return category, detail
    return "runtime_exception", message


def log_status(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def suite_counts(records: list[ScenarioExecutionRecord]) -> dict[str, int]:
    return {
        "passed": sum(1 for record in records if record.outcome == "passed"),
        "infra_failures": sum(1 for record in records if record.outcome == "infra_failure"),
        "behavior_gaps": sum(1 for record in records if record.outcome == "behavior_gap"),
        "skipped_planned": sum(1 for record in records if record.outcome == "skipped_planned"),
        "skipped_known_issue": sum(1 for record in records if record.outcome == "skipped_known_issue"),
    }


def print_suite_summary(suite: str, records: list[ScenarioExecutionRecord], *, run_root: Path | None = None) -> None:
    counts = suite_counts(records)
    print(
        (
            f"{suite} summary: "
            f"passed={counts['passed']} "
            f"infra_failures={counts['infra_failures']} "
            f"behavior_gaps={counts['behavior_gaps']} "
            f"skipped_planned={counts['skipped_planned']} "
            f"skipped_known_issue={counts['skipped_known_issue']}"
        ),
        flush=True,
    )
    if run_root is not None and suite == "real":
        print(f"real reports: {run_root / 'scorecard.md'}", flush=True)
        print(f"real target-state gaps: {run_root / 'target_state_gaps.json'}", flush=True)


def prune_real_review_artifacts(run_root: Path) -> None:
    for entry in sorted(run_root.iterdir()):
        if not entry.is_dir():
            continue
        prune_real_group_dir(entry)


def prune_real_group_dir(group_dir: Path) -> None:
    server_dir = group_dir / "server"
    if server_dir.exists():
        rebuild_tree(server_dir, collect_selected_files(server_dir, REVIEW_SERVER_FILES))
    for scenario_dir in sorted(group_dir.iterdir()):
        if not scenario_dir.is_dir() or scenario_dir.name == "server":
            continue
        agent_data_dir = scenario_dir / "agent_data"
        if agent_data_dir.exists():
            rebuild_tree(agent_data_dir, collect_review_agent_files(agent_data_dir))


def collect_selected_files(root: Path, relative_paths: tuple[Path, ...]) -> dict[Path, bytes]:
    preserved: dict[Path, bytes] = {}
    for rel_path in relative_paths:
        source = root / rel_path
        if source.exists() and source.is_file():
            preserved[rel_path] = source.read_bytes()
    return preserved


def collect_review_agent_files(agent_data_dir: Path) -> dict[Path, bytes]:
    preserved: dict[Path, bytes] = {}
    index_path = agent_data_dir / "debug" / "index.jsonl"
    if index_path.exists():
        preserved[Path("debug/index.jsonl")] = index_path.read_bytes()

    turns_root = agent_data_dir / "debug" / "turns"
    if turns_root.exists():
        for turn_dir in sorted(turns_root.glob("*/*")):
            if not turn_dir.is_dir():
                continue
            rel_turn_dir = turn_dir.relative_to(agent_data_dir)
            for filename in REVIEW_BUNDLE_FILENAMES:
                source = turn_dir / filename
                if source.exists() and source.is_file():
                    preserved[rel_turn_dir / filename] = source.read_bytes()
            prompts_dir = turn_dir / "prompts"
            if prompts_dir.exists():
                for prompt_file in sorted(prompts_dir.rglob("*")):
                    if prompt_file.is_file():
                        preserved[prompt_file.relative_to(agent_data_dir)] = prompt_file.read_bytes()
    return preserved


def rebuild_tree(root: Path, preserved: dict[Path, bytes]) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    for rel_path, content in preserved.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "build.gradle").exists() and (candidate / "agent_service" / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"Could not locate repo root from {start}")


if __name__ == "__main__":
    raise SystemExit(main())
