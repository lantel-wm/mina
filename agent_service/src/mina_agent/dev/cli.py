from __future__ import annotations

import argparse
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

from mina_agent.debug import load_debug_index, resolve_turn_bundle
from mina_agent.dev.scenarios import HeadlessScenario, ObservedScenarioResult, ScenarioAssertions, evaluate_assertions, load_scenarios


DEFAULT_SCENARIO_DIR = Path("testing/headless/scenarios")
DEFAULT_WORLD_TEMPLATE_DIR = Path("testing/headless/world_templates")
DEFAULT_OUTPUT_ROOT = Path("tmp/headless")
DEFAULT_AGENT_HOST = "127.0.0.1"
DEFAULT_AGENT_PORT = 0
DEFAULT_SERVER_READY_TIMEOUT = 180.0
DEFAULT_AGENT_READY_TIMEOUT = 30.0
DEFAULT_TURN_TIMEOUT = 180.0


@dataclass(slots=True)
class TurnRunResult:
    turn_id: str
    message: str
    bundle_dir: Path
    final_status: str
    final_reply: str
    confirmation_expected: bool
    selected_capability_ids: list[str]
    started_at: str | None
    ended_at: str | None


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

    def wait_for_substring(self, substring: str, timeout: float) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.poll() is not None:
                raise RuntimeError(f"{self._name} exited before emitting expected output: {substring}")
            remaining = max(deadline - time.time(), 0.1)
            try:
                line = self._queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
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

    def lines(self) -> list[str]:
        return list(self._lines)

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
        for name in ("mina-dev", "logs", "world", "server.properties", "eula.txt", "ops.json", "whitelist.json"):
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
        self._cleanup_backup()

    def _cleanup_backup(self) -> None:
        try:
            shutil.rmtree(self._backup_dir)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mina headless testing and trace tooling.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run-headless", help="Run headless Mina scenarios against the server bridge.")
    run_parser.add_argument("--scenario-dir", default=str(DEFAULT_SCENARIO_DIR))
    run_parser.add_argument("--world-template-dir", default=str(DEFAULT_WORLD_TEMPLATE_DIR))
    run_parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    run_parser.add_argument("--scenario-id", action="append", default=[])
    run_parser.add_argument("--agent-mode", choices=["real", "stub"], default="real")
    run_parser.add_argument("--agent-port", type=int, default=DEFAULT_AGENT_PORT)
    run_parser.add_argument("--server-ready-timeout", type=float, default=DEFAULT_SERVER_READY_TIMEOUT)
    run_parser.add_argument("--agent-ready-timeout", type=float, default=DEFAULT_AGENT_READY_TIMEOUT)
    run_parser.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT)
    run_parser.add_argument("--keep-going", action="store_true")

    recent_parser = subparsers.add_parser("recent-turns", help="List recent debug-traced turns.")
    recent_parser.add_argument("--debug-dir", default="agent_service/data/debug")
    recent_parser.add_argument("--limit", type=int, default=10)
    recent_parser.add_argument("--player")
    recent_parser.add_argument("--session")

    promote_parser = subparsers.add_parser("promote-trace", help="Promote a captured trace bundle into a checked-in scenario.")
    promote_parser.add_argument("--debug-dir", default="agent_service/data/debug")
    promote_parser.add_argument("--turn-id", required=True)
    promote_parser.add_argument("--scenario-id")
    promote_parser.add_argument("--world-template")
    promote_parser.add_argument("--output-dir", default=str(DEFAULT_SCENARIO_DIR))
    promote_parser.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    command = args.command or "run-headless"
    if command == "run-headless":
        return run_headless(args)
    if command == "recent-turns":
        return recent_turns(args)
    if command == "promote-trace":
        return promote_trace(args)
    parser.print_help()
    return 1


def run_headless(args: argparse.Namespace) -> int:
    repo_root = find_repo_root(Path.cwd())
    scenario_dir = repo_root / Path(args.scenario_dir)
    world_template_dir = repo_root / Path(args.world_template_dir)
    output_root = repo_root / Path(args.output_root)
    active_run_dir = repo_root / "run"
    if output_root == active_run_dir or active_run_dir in output_root.parents:
        print(f"Output root must not live under the active run directory: {output_root}", file=sys.stderr)
        return 1
    agent_port = args.agent_port or find_free_port()
    scenarios = load_scenarios(scenario_dir, args.scenario_id or None)
    if not scenarios:
        print(f"No scenarios found in {scenario_dir}", file=sys.stderr)
        return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = output_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    print(f"Headless run output: {run_root}")

    failures: list[tuple[str, str]] = []
    grouped = group_scenarios(scenarios)
    for group_index, (world_template, group_scenarios_list) in enumerate(grouped.items(), start=1):
        group_root = run_root / f"{group_index:02d}_{world_template}"
        server_run_dir = group_root / "server"
        materialize_run_directory(world_template_dir, world_template, server_run_dir)
        run_swap = ActiveRunDirectory(repo_root, server_run_dir)
        run_swap.activate()

        server_env = os.environ.copy()
        server_env["MINA_AGENT_BASE_URL"] = f"http://{DEFAULT_AGENT_HOST}:{agent_port}"
        server = LineProcess(
            ["./gradlew", "runServer", "--no-daemon"],
            cwd=repo_root,
            env=server_env,
            name="server",
        )
        cleanup_root_eula = ensure_repo_root_eula(repo_root)
        server.start()
        try:
            server.wait_for_substring("Done (", args.server_ready_timeout)
        except Exception as exc:
            failures.append((world_template, f"startup_failure: server failed to start: {exc}"))
            server.terminate(timeout=5.0)
            cleanup_root_eula()
            run_swap.restore()
            if not args.keep_going:
                return 1
            continue

        known_players: set[str] = set()
        dev_log_path = active_run_dir / "mina-dev" / "turns.jsonl"
        dev_log_offset = 0
        try:
            for scenario in group_scenarios_list:
                scenario_root = group_root / scenario.scenario_id
                scenario_root.mkdir(parents=True, exist_ok=True)
                agent_data_dir = scenario_root / "agent_data"
                agent_process = start_agent_process(
                        repo_root=repo_root,
                        mode=args.agent_mode,
                        agent_data_dir=agent_data_dir,
                        port=agent_port,
                    )
                try:
                    ensure_agent_ready(
                        mode=args.agent_mode,
                        host=DEFAULT_AGENT_HOST,
                        port=agent_port,
                        timeout=args.agent_ready_timeout,
                    )
                except Exception as exc:
                    agent_process.terminate(timeout=5.0)
                    failures.append((scenario.scenario_id, f"startup_failure: agent failed to start: {exc}"))
                    if not args.keep_going:
                        server.terminate(graceful_line="stop")
                        return 1
                    continue

                try:
                    if scenario.player_name not in known_players:
                        server.send_line(f"player {scenario.player_name} spawn")
                        server.wait_for_substring(f"{scenario.player_name} joined the game", 30.0)
                        known_players.add(scenario.player_name)
                    for command in scenario.setup_commands:
                        server.send_line(command)
                        time.sleep(0.25)
                    result, dev_log_offset = run_scenario(
                        scenario=scenario,
                        server=server,
                        dev_log_path=dev_log_path,
                        dev_log_offset=dev_log_offset,
                        debug_dir=agent_data_dir / "debug",
                        timeout=args.turn_timeout,
                    )
                    failure_category, failure_detail = classify_structural_failure(result)
                    if failure_category is None:
                        failure_category, failure_detail = evaluate_assertions(scenario.assertions, build_observed_result(result))
                    if failure_category is None:
                        print(f"[PASS] {scenario.scenario_id} -> {result[-1].turn_id}")
                    else:
                        bundle_dir = result[-1].bundle_dir
                        print(
                            f"[FAIL] {scenario.scenario_id} [{failure_category}] {failure_detail}\n"
                            f"  turn_id={result[-1].turn_id}\n"
                            f"  bundle={bundle_dir}",
                            file=sys.stderr,
                        )
                        failures.append((scenario.scenario_id, f"{failure_category}: {failure_detail}"))
                        if not args.keep_going:
                            agent_process.terminate(timeout=5.0)
                            server.terminate(graceful_line="stop")
                            return 1
                except Exception as exc:
                    failures.append((scenario.scenario_id, str(exc)))
                    print(f"[FAIL] {scenario.scenario_id} {exc}", file=sys.stderr)
                    if not args.keep_going:
                        agent_process.terminate(timeout=5.0)
                        server.terminate(graceful_line="stop")
                        return 1
                finally:
                    agent_process.terminate(timeout=5.0)
        finally:
            server.terminate(graceful_line="stop")
            run_swap.sync_back()
            run_swap.restore()
            cleanup_root_eula()

    if failures:
        print("\nFailures:", file=sys.stderr)
        for scenario_id, detail in failures:
            print(f"- {scenario_id}: {detail}", file=sys.stderr)
        return 1
    return 0


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
    scenario = dict(capture.get("scenario") or {})
    suggested_assertions = ((capture.get("assertion_slots") or {}).get("suggested_assertions") or {})
    scenario["assertions"] = suggested_assertions or scenario.get("assertions") or {}
    scenario["scenario_id"] = args.scenario_id or scenario.get("scenario_id") or args.turn_id
    scenario["world_template"] = args.world_template or scenario.get("world_template")
    if not scenario.get("world_template"):
        print("Promoted scenarios require --world-template when the capture has no world_template.", file=sys.stderr)
        return 1

    output_dir = repo_root / Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{scenario['scenario_id']}.json"
    if output_path.exists() and not args.force:
        print(f"Refusing to overwrite existing scenario without --force: {output_path}", file=sys.stderr)
        return 1
    output_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output_path)
    return 0


def run_scenario(
    *,
    scenario: HeadlessScenario,
    server: LineProcess,
    dev_log_path: Path,
    dev_log_offset: int,
    debug_dir: Path,
    timeout: float,
) -> tuple[list[TurnRunResult], int]:
    results: list[TurnRunResult] = []
    current_offset = dev_log_offset
    for turn_index, message in enumerate([scenario.message, *scenario.follow_up_messages]):
        normalized_message = normalize_message(message)
        current_offset = read_jsonl_offset(dev_log_path)
        server.send_line(f"execute as {scenario.player_name} run mina {normalized_message}")
        accepted, current_offset = wait_for_dev_log_entry(
            dev_log_path,
            current_offset,
            timeout,
            lambda entry: (
                entry.get("status") == "accepted"
                and entry.get("player_name") == scenario.player_name
                and entry.get("user_message") == normalized_message
            ),
            "accepted turn",
            "missing_accepted_turn",
        )
        turn_id = str(accepted["turn_id"])
        completed, current_offset = wait_for_dev_log_entry(
            dev_log_path,
            current_offset,
            timeout,
            lambda entry: entry.get("turn_id") == turn_id and entry.get("status") in {"completed", "failed"},
            "completed turn",
            "timeout",
        )
        bundle_dir = wait_for_bundle(debug_dir, turn_id, timeout)
        if bundle_dir is None:
            raise RuntimeError(f"missing_trace_bundle: no bundle for turn {turn_id}")
        enrich_capture(bundle_dir, scenario, turn_index)
        final_payload = json.loads((bundle_dir / "response.final.json").read_text(encoding="utf-8"))
        capture = json.loads((bundle_dir / "scenario.capture.json").read_text(encoding="utf-8"))
        results.append(
            TurnRunResult(
                turn_id=turn_id,
                message=normalized_message,
                bundle_dir=bundle_dir,
                final_status=str(final_payload.get("status") or "unknown"),
                final_reply=str(final_payload.get("final_reply") or ""),
                confirmation_expected=bool(final_payload.get("pending_confirmation_id")),
                selected_capability_ids=list(capture.get("selected_capability_ids") or []),
                started_at=accepted.get("started_at"),
                ended_at=completed.get("ended_at"),
            )
        )
    return results, current_offset


def build_observed_result(results: list[TurnRunResult]) -> ObservedScenarioResult:
    selected_capability_ids: list[str] = []
    seen: set[str] = set()
    for result in results:
        for capability_id in result.selected_capability_ids:
            if capability_id not in seen:
                seen.add(capability_id)
                selected_capability_ids.append(capability_id)
    first_started = parse_iso8601(results[0].started_at)
    last_ended = parse_iso8601(results[-1].ended_at)
    duration_ms = None
    if first_started is not None and last_ended is not None:
        duration_ms = max(int((last_ended - first_started).total_seconds() * 1000), 0)
    return ObservedScenarioResult(
        final_status=results[-1].final_status,
        selected_capability_ids=selected_capability_ids,
        confirmation_expected=results[-1].confirmation_expected,
        final_reply=results[-1].final_reply,
        duration_ms=duration_ms,
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


def ensure_agent_ready(*, mode: str, host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
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
            time.sleep(0.2)
    raise RuntimeError(f"agent health check failed: {last_error}")


def wait_for_dev_log_entry(
    path: Path,
    offset: int,
    timeout: float,
    predicate: Any,
    label: str,
    failure_category: str,
) -> tuple[dict[str, Any], int]:
    deadline = time.time() + timeout
    current_offset = offset
    while time.time() < deadline:
        entries, current_offset = read_new_jsonl_entries(path, current_offset)
        for entry in entries:
            if predicate(entry):
                return entry, current_offset
        time.sleep(0.1)
    raise RuntimeError(f"{failure_category}: did not observe {label} in {path}")


def wait_for_bundle(debug_dir: Path, turn_id: str, timeout: float) -> Path | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        bundle_dir = resolve_turn_bundle(debug_dir, turn_id)
        if bundle_dir is not None and (bundle_dir / "scenario.capture.json").exists() and (bundle_dir / "response.final.json").exists():
            return bundle_dir
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
        grouped.setdefault(scenario.world_template, []).append(scenario)
    return grouped


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
    for name in ("world", "config"):
        source = template_dir / name
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
        "white-list": "false",
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


def read_new_jsonl_entries(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        chunk = handle.read()
        new_offset = handle.tell()
    if not chunk:
        return [], new_offset
    entries = [json.loads(line) for line in chunk.splitlines() if line.strip()]
    return entries, new_offset


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


def find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "build.gradle").exists() and (candidate / "agent_service" / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"Could not locate repo root from {start}")


if __name__ == "__main__":
    raise SystemExit(main())
