from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import WebSocket

from mina_agent.app_server.command_runner import AppCommandRunner
from mina_agent.core import MinaCoreEngine, ThreadManager
from mina_agent.protocol import (
    ApprovalResponse,
    CommandExecParams,
    CommandExecResizeParams,
    CommandExecTerminateParams,
    CommandExecWriteParams,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    ThreadArchiveParams,
    ThreadCompactParams,
    ThreadForkParams,
    ThreadListParams,
    ThreadMetadataUpdateParams,
    ThreadNameSetParams,
    ThreadReadParams,
    ThreadRollbackParams,
    ThreadResumeParams,
    ThreadShellCommandParams,
    ThreadStartParams,
    ThreadUnsubscribeParams,
    ToolCallResultSubmission,
    TurnSteerParams,
    TurnStartParams,
)


class AppServerConnection:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._send_lock = None
        self.initialized = False
        self._subscribed_threads: set[str] = set()

    async def accept(self) -> None:
        await self._websocket.accept()
        self._send_lock = asyncio.Lock()

    async def receive_request(self) -> JsonRpcRequest:
        payload = await self._websocket.receive_json()
        return JsonRpcRequest.model_validate(payload)

    async def send_response(
        self,
        rpc_id: int | str,
        *,
        result: dict[str, Any] | None = None,
        error: JsonRpcError | None = None,
    ) -> None:
        async with self._send_lock:
            await self._websocket.send_json(
                JsonRpcResponse(id=rpc_id, result=result, error=error).model_dump(exclude_none=True)
            )

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                }
            )

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


class MinaAppServer:
    def __init__(self, *, thread_manager: ThreadManager, engine: MinaCoreEngine) -> None:
        self._thread_manager = thread_manager
        self._engine = engine
        self._store = thread_manager._store
        self._command_runner = AppCommandRunner()
        self._thread_connections: dict[str, set[AppServerConnection]] = {}

    async def handle(self, connection: AppServerConnection, request: JsonRpcRequest) -> None:
        try:
            result = await self._dispatch(connection, request)
        except KeyError as exc:
            await connection.send_response(
                request.id,
                error=JsonRpcError(code=-32004, message=str(exc)),
            )
            return
        except RuntimeError as exc:
            await connection.send_response(
                request.id,
                error=JsonRpcError(code=-32010, message=str(exc)),
            )
            return
        except Exception as exc:
            await connection.send_response(
                request.id,
                error=JsonRpcError(code=-32603, message=str(exc)),
            )
            return

        await connection.send_response(request.id, result=result)

    async def _dispatch(self, connection: AppServerConnection, request: JsonRpcRequest) -> dict[str, Any]:
        if request.method == "initialize":
            if connection.initialized:
                raise RuntimeError("Already initialized")
            connection.initialized = True
            return {
                "server": "mina_app_server",
                "protocol": "jsonrpc-2.0",
                "methods": [
                    "initialize",
                    "thread/start",
                    "thread/list",
                    "thread/loaded/list",
                    "thread/read",
                    "thread/resume",
                    "thread/archive",
                    "thread/unsubscribe",
                    "thread/unarchive",
                    "thread/name/set",
                    "thread/metadata/update",
                    "thread/fork",
                    "thread/compact/start",
                    "thread/shellCommand",
                    "thread/rollback",
                    "turn/start",
                    "turn/steer",
                    "turn/interrupt",
                    "command/exec",
                    "command/exec/write",
                    "command/exec/resize",
                    "command/exec/terminate",
                    "tool/result",
                    "approval/respond",
                ],
            }

        if not connection.initialized:
            raise RuntimeError("Not initialized")

        if request.method == "thread/start":
            params = ThreadStartParams.model_validate(request.params)
            thread = await self._thread_manager.start_thread(params)
            await self._subscribe(connection, thread.thread_id)
            await self._broadcast_thread_notification(thread.thread_id, "thread/started", {"thread": thread.model_dump()})
            await self._broadcast_thread_notification(
                thread.thread_id,
                "thread/status/changed",
                {
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "status_detail": self._status_detail(thread.status),
                    "archived": thread.archived,
                },
            )
            return {"thread": thread.model_dump()}

        if request.method == "thread/list":
            params = ThreadListParams.model_validate(request.params)
            threads = await self._thread_manager.list_threads(
                limit=params.limit,
                archived=params.archived,
                search_term=params.search_term,
            )
            return {"threads": [thread.model_dump() for thread in threads]}

        if request.method == "thread/loaded/list":
            thread_ids = await self._thread_manager.list_loaded_threads()
            return {"thread_ids": thread_ids}

        if request.method == "thread/read":
            params = ThreadReadParams.model_validate(request.params)
            thread = await self._thread_manager.read_thread(params.thread_id, include_turns=params.include_turns)
            return {"thread": thread}

        if request.method == "thread/resume":
            params = ThreadResumeParams.model_validate(request.params)
            thread = await self._thread_manager.resume_thread(params.thread_id)
            await self._subscribe(connection, thread.thread_id)
            await self._broadcast_thread_notification(
                thread.thread_id,
                "thread/status/changed",
                {
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "status_detail": self._status_detail(thread.status),
                    "archived": thread.archived,
                },
            )
            return {"thread": thread.model_dump()}

        if request.method == "thread/archive":
            params = ThreadArchiveParams.model_validate(request.params)
            await self._thread_manager.archive_thread(params.thread_id)
            await self._broadcast_thread_notification(params.thread_id, "thread/archived", {"thread_id": params.thread_id})
            thread = await self._thread_manager.resume_thread(params.thread_id)
            await self._broadcast_thread_notification(
                params.thread_id,
                "thread/status/changed",
                {
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "status_detail": self._status_detail(thread.status),
                    "archived": thread.archived,
                },
            )
            return {}

        if request.method == "thread/unsubscribe":
            params = ThreadUnsubscribeParams.model_validate(request.params)
            loaded = await self._thread_manager.is_thread_loaded(params.thread_id)
            if not loaded:
                return {"status": "notLoaded"}
            if not connection.is_subscribed(params.thread_id):
                return {"status": "notSubscribed"}
            unloaded = await self._unsubscribe(connection, params.thread_id)
            if unloaded:
                await connection.send_notification(
                    "thread/status/changed",
                    {
                        "thread_id": params.thread_id,
                        "status": "notLoaded",
                        "status_detail": self._status_detail("notLoaded"),
                        "archived": False,
                    },
                )
                await connection.send_notification("thread/closed", {"thread_id": params.thread_id})
            return {"status": "unsubscribed"}

        if request.method == "thread/unarchive":
            params = ThreadArchiveParams.model_validate(request.params)
            await self._thread_manager.unarchive_thread(params.thread_id)
            await self._broadcast_thread_notification(params.thread_id, "thread/unarchived", {"thread_id": params.thread_id})
            thread = await self._thread_manager.resume_thread(params.thread_id)
            await self._broadcast_thread_notification(
                params.thread_id,
                "thread/status/changed",
                {
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "status_detail": self._status_detail(thread.status),
                    "archived": thread.archived,
                },
            )
            return {}

        if request.method == "thread/name/set":
            params = ThreadNameSetParams.model_validate(request.params)
            await self._thread_manager.set_thread_name(params.thread_id, params.name)
            await self._broadcast_thread_notification(
                params.thread_id,
                "thread/name/updated",
                {"thread_id": params.thread_id, "name": params.name},
            )
            return {}

        if request.method == "thread/metadata/update":
            params = ThreadMetadataUpdateParams.model_validate(request.params)
            thread = await self._thread_manager.update_thread_metadata(params)
            return {"thread": thread.model_dump()}

        if request.method == "thread/fork":
            params = ThreadForkParams.model_validate(request.params)
            thread = await self._thread_manager.fork_thread(params)
            await self._subscribe(connection, thread.thread_id)
            await self._broadcast_thread_notification(thread.thread_id, "thread/started", {"thread": thread.model_dump()})
            await self._broadcast_thread_notification(
                thread.thread_id,
                "thread/status/changed",
                {
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "status_detail": self._status_detail(thread.status),
                    "archived": thread.archived,
                },
            )
            return {"thread": thread.model_dump()}

        if request.method == "thread/compact/start":
            params = ThreadCompactParams.model_validate(request.params)
            compacted = await self._thread_manager.compact_thread(params)
            await self._broadcast_thread_notification(params.thread_id, "thread/compacted", compacted)
            return {}

        if request.method == "thread/shellCommand":
            params = ThreadShellCommandParams.model_validate(request.params)
            await self._start_thread_shell_command(connection, params)
            return {}

        if request.method == "thread/rollback":
            params = ThreadRollbackParams.model_validate(request.params)
            thread = await self._engine.rollback_thread(params)
            return {"thread": thread}

        if request.method == "turn/start":
            params = TurnStartParams.model_validate(request.params)
            await self._subscribe(connection, params.thread_id)
            turn = await self._engine.start_turn(params, self._thread_emitter(params.thread_id))
            return {"turn": turn.model_dump()}

        if request.method == "turn/steer":
            params = TurnSteerParams.model_validate(request.params)
            turn_id = await self._engine.submit_steer(params)
            return {"turn_id": turn_id}

        if request.method == "turn/interrupt":
            thread_id = str(request.params["thread_id"])
            turn_id = str(request.params["turn_id"])
            await self._engine.interrupt_turn(thread_id, turn_id)
            return {}

        if request.method == "command/exec":
            params = CommandExecParams.model_validate(request.params)
            return await self._command_runner.exec_command(
                connection_key=id(connection),
                params=params,
                output_emitter=lambda stream, delta_base64, cap_reached: connection.send_notification(
                    "command/exec/outputDelta",
                    {
                        "process_id": params.process_id,
                        "stream": stream,
                        "delta_base64": delta_base64,
                        "cap_reached": cap_reached,
                    },
                ),
            )

        if request.method == "command/exec/write":
            params = CommandExecWriteParams.model_validate(request.params)
            await self._command_runner.write(connection_key=id(connection), params=params)
            return {}

        if request.method == "command/exec/resize":
            params = CommandExecResizeParams.model_validate(request.params)
            await self._command_runner.resize(connection_key=id(connection), params=params)
            return {}

        if request.method == "command/exec/terminate":
            params = CommandExecTerminateParams.model_validate(request.params)
            await self._command_runner.terminate(connection_key=id(connection), params=params)
            return {}

        if request.method == "tool/result":
            submission = ToolCallResultSubmission.model_validate(request.params)
            await self._engine.submit_tool_result(submission)
            return {}

        if request.method == "approval/respond":
            response = ApprovalResponse.model_validate(request.params)
            await self._engine.submit_approval(response)
            return {}

        raise RuntimeError(f"Unsupported method: {request.method}")

    async def disconnect(self, connection: AppServerConnection) -> None:
        for thread_id in connection.subscribed_threads():
            await self._unsubscribe(connection, thread_id)
        await self._command_runner.close_connection(id(connection))

    async def _subscribe(self, connection: AppServerConnection, thread_id: str) -> None:
        await self._thread_manager.subscribe_thread(thread_id)
        connection.subscribe_thread(thread_id)
        self._thread_connections.setdefault(thread_id, set()).add(connection)

    async def _unsubscribe(self, connection: AppServerConnection, thread_id: str) -> bool:
        if not connection.unsubscribe_thread(thread_id):
            return False
        subscribers = self._thread_connections.get(thread_id)
        if subscribers is not None:
            subscribers.discard(connection)
            if not subscribers:
                self._thread_connections.pop(thread_id, None)
        return await self._thread_manager.unsubscribe_thread(thread_id)

    def _thread_emitter(self, thread_id: str):
        async def _emit(method: str, params: dict[str, Any]) -> None:
            if method == "thread/status/changed" and "status_detail" not in params and "status" in params:
                params = dict(params)
                params["status_detail"] = self._status_detail(str(params["status"]))
            await self._broadcast_thread_notification(thread_id, method, params)

        return _emit

    async def _broadcast_thread_notification(self, thread_id: str, method: str, params: dict[str, Any]) -> None:
        subscribers = list(self._thread_connections.get(thread_id, set()))
        for subscriber in subscribers:
            await subscriber.send_notification(method, params)

    async def _start_thread_shell_command(
        self,
        connection: AppServerConnection,
        params: ThreadShellCommandParams,
    ) -> None:
        await self._subscribe(connection, params.thread_id)
        active_turn = await self._thread_manager.get_active_turn(params.thread_id)
        emitter = self._thread_emitter(params.thread_id)
        if active_turn is None:
            turn_id = f"{params.thread_id}__shell_{uuid.uuid4().hex[:8]}"
            turn_record, handle = await self._thread_manager.open_turn(
                thread_id=params.thread_id,
                turn_id=turn_id,
                emitter=emitter,
            )
            self._store.create_thread_turn(
                turn_id,
                params.thread_id,
                f"!{params.command}",
                {"shell_command": params.command, "source": "thread_shell_command"},
                task_id=None,
            )
            await emitter(
                "turn/started",
                {
                    "thread_id": params.thread_id,
                    "turn": turn_record.model_dump(),
                },
            )
            background_task = asyncio.create_task(
                self._run_shell_command_turn(
                    thread_id=params.thread_id,
                    turn_id=turn_id,
                    emitter=emitter,
                    command=params.command,
                    complete_turn=True,
                )
            )
            await self._thread_manager.attach_task(params.thread_id, background_task)
            return
        asyncio.create_task(
            self._run_shell_command_turn(
                thread_id=params.thread_id,
                turn_id=active_turn.turn_id,
                emitter=emitter,
                command=params.command,
                complete_turn=False,
            )
        )

    async def _run_shell_command_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        emitter,
        command: str,
        complete_turn: bool,
    ) -> None:
        item_id = f"cmd_{uuid.uuid4().hex}"
        process_id = f"shell_{uuid.uuid4().hex[:12]}"
        payload = {
            "source": "userShell",
            "command": command,
            "process_id": process_id,
        }
        self._store.create_turn_item(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            item_kind="command_execution",
            payload=payload,
            status="started",
        )
        await emitter(
            "item/started",
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "item_id": item_id,
                "item_kind": "command_execution",
                "payload": payload,
            },
        )
        try:
            result = await self._command_runner.exec_command(
                connection_key=id(self),
                params=CommandExecParams(
                    command=["/bin/zsh", "-lc", command],
                    process_id=process_id,
                    stream_stdout_stderr=True,
                ),
                output_emitter=lambda stream, delta_base64, cap_reached: emitter(
                    "item/commandExecution/outputDelta",
                    {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "item_id": item_id,
                        "process_id": process_id,
                        "stream": stream,
                        "delta_base64": delta_base64,
                        "cap_reached": cap_reached,
                    },
                ),
            )
            completed_payload = {
                **payload,
                **result,
                "status": "completed",
            }
            self._store.update_turn_item(item_id, status="completed", payload=completed_payload)
            await emitter(
                "item/completed",
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "item_kind": "command_execution",
                    "payload": completed_payload,
                },
            )
            if complete_turn:
                self._store.finish_thread_turn(turn_id, f"Shell command finished: {command}", status="completed")
                await emitter(
                    "turn/completed",
                    {
                        "thread_id": thread_id,
                        "turn": {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "status": "completed",
                            "final_reply": f"Shell command finished: {command}",
                        },
                    },
                )
                await self._thread_manager.complete_turn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status="completed",
                    final_reply=f"Shell command finished: {command}",
                )
        except Exception as exc:
            failed_payload = {
                **payload,
                "status": "failed",
                "error": str(exc),
            }
            self._store.update_turn_item(item_id, status="completed", payload=failed_payload)
            await emitter(
                "item/completed",
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "item_kind": "command_execution",
                    "payload": failed_payload,
                },
            )
            if complete_turn:
                self._store.finish_thread_turn(turn_id, str(exc), status="failed")
                await emitter(
                    "turn/failed",
                    {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "message": "Shell command failed.",
                        "detail": str(exc),
                    },
                )
                await self._thread_manager.complete_turn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status="failed",
                    final_reply=str(exc),
                )

    def _status_detail(self, status: str) -> dict[str, Any]:
        if status == "notLoaded":
            return {"type": "notLoaded"}
        if status in {"running", "active"}:
            return {"type": "active", "active_flags": []}
        if status == "failed":
            return {"type": "systemError"}
        return {"type": "idle"}
