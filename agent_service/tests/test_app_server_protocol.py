from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from mina_agent.app_server import MinaAppServer
from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.core import MinaCoreEngine, ThreadManager
from mina_agent.debug import build_debug_recorder
from mina_agent.executors.script_runner import ScriptRunner
from mina_agent.memories import MemoryPipeline
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.protocol import (
    ApprovalResponse,
    ExternalToolSpec,
    JsonRpcRequest,
    PlayerContext,
    ServerEnvContext,
    ThreadStartParams,
    ToolCallResultSubmission,
    TurnContextPayload,
    TurnStartParams,
)
from mina_agent.providers.openai_compatible import ProviderDecisionResult
from mina_agent.retrieval.wiki_store import WikiKnowledgeStore
from mina_agent.runtime.agent_services import AgentServices
from mina_agent.runtime.capability_registry import CapabilityRegistry
from mina_agent.runtime.context_manager import ContextManager
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.execution_manager import ExecutionManager
from mina_agent.runtime.execution_orchestrator import ExecutionOrchestrator
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.schemas import LimitsPayload, ModelDecision
from mina_agent.tools import MinaToolRegistry


class AppServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_turn_emits_streamed_assistant_message(self) -> None:
        engine, thread_manager, events = self._build_engine(
            [
                ModelDecision(mode="final_reply", intent="reply", final_reply="你好，我在。"),
            ]
        )
        await thread_manager.start_thread(
            ThreadStartParams(
                thread_id="thread-reply",
                player_uuid="player-1",
                player_name="Tester",
            )
        )

        await engine.start_turn(self._turn_params("thread-reply", "turn-reply"), self._emitter(events))
        await self._wait_for_method(events, "turn/completed")

        methods = [method for method, _ in events]
        self.assertIn("turn/started", methods)
        self.assertIn("item/assistantMessage/delta", methods)
        self.assertIn("turn/completed", methods)
        self.assertIn("thread/status/changed", methods)

    async def test_external_tool_round_trip_completes_turn(self) -> None:
        engine, thread_manager, events = self._build_engine(
            [
                ModelDecision(
                    mode="call_capability",
                    capability_id="game.player_snapshot.read",
                    arguments={},
                    effect_summary="Read player snapshot.",
                ),
                ModelDecision(mode="final_reply", intent="reply", final_reply="我已经看过你的状态了。"),
            ]
        )
        await thread_manager.start_thread(
            ThreadStartParams(
                thread_id="thread-tool",
                player_uuid="player-1",
                player_name="Tester",
            )
        )

        await engine.start_turn(self._turn_params("thread-tool", "turn-tool"), self._emitter(events))
        _, requested = await self._wait_for_method(events, "item/toolCall/requested")
        await engine.submit_tool_result(
            ToolCallResultSubmission(
                thread_id="thread-tool",
                turn_id="turn-tool",
                item_id=str(requested["item_id"]),
                tool_id=str(requested["tool_id"]),
                status="executed",
                observations={"health": 20},
                preconditions_passed=True,
                side_effect_summary="Read current player snapshot.",
                timing_ms=5,
            )
        )
        await self._wait_for_method(events, "turn/completed")

        methods = [method for method, _ in events]
        self.assertIn("item/toolCall/completed", methods)
        self.assertIn("turn/completed", methods)

    async def test_approval_response_unblocks_tool_execution(self) -> None:
        engine, thread_manager, events = self._build_engine(
            [
                ModelDecision(
                    mode="call_capability",
                    capability_id="game.player_snapshot.read",
                    arguments={},
                    effect_summary="Read player snapshot.",
                    requires_confirmation=True,
                ),
                ModelDecision(mode="final_reply", intent="reply", final_reply="确认后我已经做完了。"),
            ]
        )
        await thread_manager.start_thread(
            ThreadStartParams(
                thread_id="thread-approval",
                player_uuid="player-1",
                player_name="Tester",
            )
        )

        await engine.start_turn(self._turn_params("thread-approval", "turn-approval"), self._emitter(events))
        _, approval = await self._wait_for_method(events, "approval/requested")
        await engine.submit_approval(
            ApprovalResponse(
                thread_id="thread-approval",
                turn_id="turn-approval",
                approval_id=str(approval["approval_id"]),
                approved=True,
                reason="确认",
            )
        )
        _, requested = await self._wait_for_method(events, "item/toolCall/requested")
        await engine.submit_tool_result(
            ToolCallResultSubmission(
                thread_id="thread-approval",
                turn_id="turn-approval",
                item_id=str(requested["item_id"]),
                tool_id=str(requested["tool_id"]),
                status="executed",
                observations={"health": 20},
                preconditions_passed=True,
                side_effect_summary="Read current player snapshot.",
                timing_ms=5,
            )
        )
        await self._wait_for_method(events, "turn/completed")

        methods = [method for method, _ in events]
        self.assertIn("approval/requested", methods)
        self.assertIn("item/toolCall/requested", methods)
        self.assertIn("turn/completed", methods)

    async def test_app_server_thread_management_methods(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [ModelDecision(mode="final_reply", intent="reply", final_reply="hi")]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=2,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-manage",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=3, method="thread/name/set", params={"thread_id": "thread-manage", "name": "Companion"}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=4, method="thread/archive", params={"thread_id": "thread-manage"}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=5, method="thread/list", params={"archived": True, "limit": 10}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=55, method="thread/loaded/list", params={}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=56,
                method="thread/metadata/update",
                params={"thread_id": "thread-manage", "metadata": {"mood": "playful"}},
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=6, method="thread/unarchive", params={"thread_id": "thread-manage"}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=7, method="thread/read", params={"thread_id": "thread-manage", "include_turns": False}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=8, method="thread/unsubscribe", params={"thread_id": "thread-manage"}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=9, method="thread/read", params={"thread_id": "thread-manage", "include_turns": False}),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(id=10, method="thread/unsubscribe", params={"thread_id": "thread-manage"}),
        )

        archived_list = connection.responses[5]["result"]["threads"]
        self.assertEqual(archived_list[0]["thread_id"], "thread-manage")
        self.assertTrue(archived_list[0]["archived"])
        self.assertIn("thread-manage", connection.responses[55]["result"]["thread_ids"])
        read_thread = connection.responses[7]["result"]["thread"]
        self.assertEqual(read_thread["name"], "Companion")
        self.assertFalse(read_thread["archived"])
        self.assertEqual(read_thread["status_detail"]["type"], "idle")
        self.assertEqual(connection.responses[56]["result"]["thread"]["metadata"]["mood"], "playful")
        self.assertEqual(connection.responses[8]["result"]["status"], "unsubscribed")
        self.assertEqual(connection.responses[9]["result"]["thread"]["status"], "notLoaded")
        self.assertEqual(connection.responses[9]["result"]["thread"]["status_detail"]["type"], "notLoaded")
        self.assertEqual(connection.responses[10]["result"]["status"], "notLoaded")
        self.assertIn("thread/archived", [method for method, _ in connection.notifications])
        self.assertIn("thread/unarchived", [method for method, _ in connection.notifications])
        self.assertIn("thread/status/changed", [method for method, _ in connection.notifications])
        self.assertIn("thread/closed", [method for method, _ in connection.notifications])

    async def test_app_server_thread_fork_and_compact_methods(self) -> None:
        engine, thread_manager, events = self._build_engine(
            [ModelDecision(mode="final_reply", intent="reply", final_reply="hi")]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=2,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-source",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await engine.start_turn(self._turn_params("thread-source", "turn-source"), self._emitter(events))
        await self._wait_for_method(events, "turn/completed")

        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=3,
                method="thread/fork",
                params={
                    "source_thread_id": "thread-source",
                    "thread_id": "thread-forked",
                },
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=4,
                method="thread/read",
                params={"thread_id": "thread-forked", "include_turns": True},
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=5,
                method="thread/compact/start",
                params={"thread_id": "thread-source"},
            ),
        )

        forked = connection.responses[4]["result"]["thread"]
        self.assertEqual(forked["thread_id"], "thread-forked")
        self.assertEqual(len(forked["turns"]), 1)
        self.assertIn("thread/compacted", [method for method, _ in connection.notifications])

    async def test_app_server_turn_steer_method_records_mid_turn_input(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [
                ModelDecision(
                    mode="call_capability",
                    capability_id="game.player_snapshot.read",
                    arguments={},
                    effect_summary="Read player snapshot.",
                ),
                ModelDecision(mode="final_reply", intent="reply", final_reply="我会按新的方向继续。"),
            ]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=2,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-steer",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=3,
                method="turn/start",
                params=self._turn_params("thread-steer", "turn-steer").model_dump(),
            ),
        )
        _, requested = await self._wait_for_method(connection.notifications, "item/toolCall/requested")
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=4,
                method="turn/steer",
                params={
                    "thread_id": "thread-steer",
                    "expected_turn_id": "turn-steer",
                    "input": [{"type": "text", "text": "Actually keep it brief."}],
                },
            ),
        )
        self.assertEqual(connection.responses[4]["result"]["turn_id"], "turn-steer")
        await engine.submit_tool_result(
            ToolCallResultSubmission(
                thread_id="thread-steer",
                turn_id="turn-steer",
                item_id=str(requested["item_id"]),
                tool_id=str(requested["tool_id"]),
                status="executed",
                observations={"health": 20},
                preconditions_passed=True,
                side_effect_summary="Read current player snapshot.",
                timing_ms=5,
            )
        )
        await self._wait_for_method(connection.notifications, "turn/completed")

        thread = await thread_manager.read_thread("thread-steer", include_turns=True)
        items = thread["turns"][0]["items"]
        steer_items = [
            item
            for item in items
            if item["item_kind"] == "user_message" and item["payload"].get("source") == "turn_steer"
        ]
        self.assertEqual(len(steer_items), 1)
        self.assertEqual(steer_items[0]["payload"]["text"], "Actually keep it brief.")

    async def test_app_server_thread_rollback_method_prunes_turns(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [
                ModelDecision(mode="final_reply", intent="reply", final_reply="first"),
                ModelDecision(mode="final_reply", intent="reply", final_reply="second"),
            ]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=2,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-rollback",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=3,
                method="turn/start",
                params=self._turn_params("thread-rollback", "turn-one").model_dump(),
            ),
        )
        await self._wait_for_method(connection.notifications, "turn/completed")
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=4,
                method="turn/start",
                params=self._turn_params("thread-rollback", "turn-two").model_dump(),
            ),
        )
        for _ in range(100):
            completed = [
                payload
                for method, payload in connection.notifications
                if method == "turn/completed" and payload.get("turn", {}).get("turn_id") == "turn-two"
            ]
            if completed:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Timed out waiting for second turn completion")

        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=5,
                method="thread/rollback",
                params={"thread_id": "thread-rollback", "num_turns": 1},
            ),
        )
        rolled_back = connection.responses[5]["result"]["thread"]
        self.assertEqual([turn["turn_id"] for turn in rolled_back["turns"]], ["turn-one"])

        rollout_path = thread_manager._store.thread_dir("thread-rollback") / "rollout.jsonl"  # type: ignore[attr-defined]
        self.assertIn("thread_rollback", rollout_path.read_text(encoding="utf-8"))

    async def test_thread_notifications_broadcast_to_all_subscribers(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [ModelDecision(mode="final_reply", intent="reply", final_reply="broadcast hi")]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        first = _FakeConnection()
        second = _FakeConnection()

        await app_server.handle(first, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(second, JsonRpcRequest(id=2, method="initialize"))
        await app_server.handle(
            first,
            JsonRpcRequest(
                id=3,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-broadcast",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await app_server.handle(
            second,
            JsonRpcRequest(id=4, method="thread/resume", params={"thread_id": "thread-broadcast"}),
        )
        await app_server.handle(
            first,
            JsonRpcRequest(
                id=5,
                method="turn/start",
                params=self._turn_params("thread-broadcast", "turn-broadcast").model_dump(),
            ),
        )
        await self._wait_for_method(first.notifications, "turn/completed")
        await self._wait_for_method(second.notifications, "turn/completed")

        self.assertIn("item/assistantMessage/delta", [method for method, _ in first.notifications])
        self.assertIn("item/assistantMessage/delta", [method for method, _ in second.notifications])

    async def test_command_exec_streams_output_and_accepts_stdin(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [ModelDecision(mode="final_reply", intent="reply", final_reply="unused")]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        command_task = asyncio.create_task(
            app_server.handle(
                connection,
                JsonRpcRequest(
                    id=2,
                    method="command/exec",
                    params={
                        "command": ["/bin/sh", "-lc", "printf ready; cat"],
                        "process_id": "proc-1",
                        "stream_stdin": True,
                        "stream_stdout_stderr": True,
                    },
                ),
            )
        )
        await self._wait_for_method(connection.notifications, "command/exec/outputDelta")
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=3,
                method="command/exec/write",
                params={
                    "process_id": "proc-1",
                    "delta_base64": base64.b64encode(b" world").decode("ascii"),
                    "close_stdin": True,
                },
            ),
        )
        await command_task
        result = connection.responses[2]["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "")
        deltas = [
            payload
            for method, payload in connection.notifications
            if method == "command/exec/outputDelta"
        ]
        decoded = b"".join(base64.b64decode(payload["delta_base64"]) for payload in deltas)
        self.assertIn(b"ready", decoded)
        self.assertIn(b" world", decoded)

    async def test_thread_shell_command_runs_as_standalone_turn(self) -> None:
        engine, thread_manager, _ = self._build_engine(
            [ModelDecision(mode="final_reply", intent="reply", final_reply="unused")]
        )
        app_server = MinaAppServer(thread_manager=thread_manager, engine=engine)
        connection = _FakeConnection()

        await app_server.handle(connection, JsonRpcRequest(id=1, method="initialize"))
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=2,
                method="thread/start",
                params=ThreadStartParams(
                    thread_id="thread-shell",
                    player_uuid="player-1",
                    player_name="Tester",
                ).model_dump(),
            ),
        )
        await app_server.handle(
            connection,
            JsonRpcRequest(
                id=3,
                method="thread/shellCommand",
                params={"thread_id": "thread-shell", "command": "printf shell-ok"},
            ),
        )
        await self._wait_for_method(connection.notifications, "turn/completed")
        self.assertIn("item/commandExecution/outputDelta", [method for method, _ in connection.notifications])
        thread = await thread_manager.read_thread("thread-shell", include_turns=True)
        self.assertEqual(len(thread["turns"]), 1)
        items = thread["turns"][0]["items"]
        self.assertTrue(any(item["item_kind"] == "command_execution" for item in items))

    def _build_engine(self, decisions: list[ModelDecision]) -> tuple[MinaCoreEngine, ThreadManager, list[tuple[str, dict]]]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
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
        policy_engine = PolicyEngine()
        memory_policy = MemoryPolicy()
        provider = _SequenceProvider(decisions)
        capability_registry = CapabilityRegistry(
            settings=settings,
            store=store,
            policy_engine=policy_engine,
            wiki_store=WikiKnowledgeStore(settings.wiki_db_path),
            script_runner=ScriptRunner(settings),
            delegate_runtime=DelegateRuntime(store, DeliberationEngine(provider)),
        )
        execution_orchestrator = ExecutionOrchestrator(settings, store)
        memory_manager = MemoryManager(store, memory_policy)
        services = AgentServices(
            settings=settings,
            store=store,
            audit=AuditLogger(settings.audit_dir),
            debug=build_debug_recorder(settings),
            policy_engine=policy_engine,
            capability_registry=capability_registry,
            execution_orchestrator=execution_orchestrator,
            memory_policy=memory_policy,
            context_manager=ContextManager(settings, store, memory_policy),
            deliberation_engine=DeliberationEngine(provider),
            execution_manager=ExecutionManager(capability_registry, execution_orchestrator),
            memory_manager=memory_manager,
            task_manager=TaskManager(store),
            delegate_runtime=DelegateRuntime(store, DeliberationEngine(provider)),
        )
        thread_manager = ThreadManager(store)
        memory_pipeline = MemoryPipeline(settings, store, memory_manager)
        memory_pipeline.kickoff_background_refresh = lambda *, reason: None  # type: ignore[method-assign]
        engine = MinaCoreEngine(
            services,
            thread_manager=thread_manager,
            tool_registry=MinaToolRegistry(capability_registry),
            memory_pipeline=memory_pipeline,
        )
        return engine, thread_manager, []

    def _turn_params(self, thread_id: str, turn_id: str) -> TurnStartParams:
        return TurnStartParams(
            thread_id=thread_id,
            turn_id=turn_id,
            user_message="hello Mina",
            context=TurnContextPayload(
                player=PlayerContext(
                    uuid="player-1",
                    name="Tester",
                    role="read_only",
                    dimension="minecraft:overworld",
                    position={"x": 0, "y": 64, "z": 0},
                ),
                server_env=ServerEnvContext(
                    dedicated=True,
                    motd="Test",
                    current_players=1,
                    max_players=10,
                    carpet_loaded=False,
                    experimental_enabled=False,
                    dynamic_scripting_enabled=False,
                ),
                scoped_snapshot={"player": {"health": 20}},
                tool_specs=[
                    ExternalToolSpec(
                        id="game.player_snapshot.read",
                        description="Read player snapshot.",
                        risk_class="read_only",
                        execution_mode="server_main_thread",
                        requires_confirmation=False,
                        input_schema={},
                        output_schema={},
                    )
                ],
                limits=LimitsPayload(
                    max_agent_steps=4,
                    max_bridge_actions_per_turn=2,
                    max_continuation_depth=1,
                ),
            ),
        )

    def _emitter(self, events: list[tuple[str, dict]]):
        async def _emit(method: str, payload: dict) -> None:
            events.append((method, payload))

        return _emit

    async def _wait_for_method(self, events: list[tuple[str, dict]], method: str) -> tuple[str, dict]:
        for _ in range(100):
            for event_method, payload in events:
                if event_method == method:
                    return event_method, payload
            await asyncio.sleep(0.01)
        raise AssertionError(f"Timed out waiting for method {method}")


class _SequenceProvider:
    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = list(decisions)

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        if not self._decisions:
            raise AssertionError("No provider decisions left.")
        decision = self._decisions.pop(0)
        return ProviderDecisionResult(
            decision=decision,
            latency_ms=1,
            raw_response_preview="{}",
            parse_status="ok",
            model="test-model",
            temperature=0.2,
            message_count=len(messages),
        )

    def estimate_prompt_tokens(self, messages: list[dict[str, str]]) -> dict[str, object]:
        return {
            "model": "test-model",
            "encoding_name": "cl100k_base",
            "message_count": len(messages),
            "message_tokens": [10 for _ in messages],
            "total_tokens": max(1, len(messages) * 10),
        }


class _FakeConnection:
    def __init__(self) -> None:
        self.initialized = False
        self.responses: dict[int | str, dict] = {}
        self.notifications: list[tuple[str, dict]] = []
        self._subscribed_threads: set[str] = set()

    async def send_response(self, rpc_id, *, result=None, error=None) -> None:
        self.responses[rpc_id] = {"result": result, "error": error.model_dump() if error is not None else None}

    async def send_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))

    def subscribe_thread(self, thread_id: str) -> None:
        self._subscribed_threads.add(thread_id)

    def unsubscribe_thread(self, thread_id: str) -> bool:
        if thread_id not in self._subscribed_threads:
            return False
        self._subscribed_threads.remove(thread_id)
        return True

    def is_subscribed(self, thread_id: str) -> bool:
        return thread_id in self._subscribed_threads

    def subscribed_threads(self) -> list[str]:
        return sorted(self._subscribed_threads)
