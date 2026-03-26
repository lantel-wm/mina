from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from datetime import datetime, timezone

from mina_agent.memory.store import Store
from mina_agent.protocol import (
    ApprovalResponse,
    ThreadCompactParams,
    ThreadMetadataUpdateParams,
    ThreadRecord,
    ThreadForkParams,
    ThreadRollbackParams,
    ThreadStartParams,
    ToolCallResultSubmission,
    TurnSteerInput,
    TurnSteerParams,
    TurnRecord,
)


Emitter = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass(slots=True)
class ActiveTurnHandle:
    thread_id: str
    turn_id: str
    emitter: Emitter
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    tool_waiters: dict[str, asyncio.Future[ToolCallResultSubmission]] = field(default_factory=dict)
    approval_waiters: dict[str, asyncio.Future[ApprovalResponse]] = field(default_factory=dict)
    pending_steers: list[TurnSteerInput] = field(default_factory=list)

    def register_tool_waiter(self, item_id: str) -> asyncio.Future[ToolCallResultSubmission]:
        future: asyncio.Future[ToolCallResultSubmission] = asyncio.get_running_loop().create_future()
        self.tool_waiters[item_id] = future
        return future

    def register_approval_waiter(self, approval_id: str) -> asyncio.Future[ApprovalResponse]:
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        self.approval_waiters[approval_id] = future
        return future

    def add_steers(self, items: list[TurnSteerInput]) -> None:
        self.pending_steers.extend(items)

    def drain_steers(self) -> list[TurnSteerInput]:
        items = list(self.pending_steers)
        self.pending_steers.clear()
        return items


class ThreadManager:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._active_turns: dict[str, ActiveTurnHandle] = {}
        self._loaded_threads: set[str] = set()
        self._thread_subscribers: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def start_thread(self, params: ThreadStartParams) -> ThreadRecord:
        self._store.ensure_thread(
            params.thread_id,
            player_uuid=params.player_uuid,
            player_name=params.player_name,
            metadata=params.metadata,
        )
        self._loaded_threads.add(params.thread_id)
        return ThreadRecord.model_validate(self._store.get_thread(params.thread_id))

    async def resume_thread(self, thread_id: str) -> ThreadRecord:
        record = self._store.get_thread(thread_id)
        if record is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        self._loaded_threads.add(thread_id)
        return ThreadRecord.model_validate(record)

    async def list_threads(
        self,
        *,
        limit: int = 50,
        archived: bool | None = None,
        search_term: str | None = None,
    ) -> list[ThreadRecord]:
        return [
            ThreadRecord.model_validate(self._present_thread_record(record))
            for record in self._store.list_threads(limit=limit, archived=archived, search_term=search_term)
        ]

    async def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, object]:
        record = self._store.read_thread(thread_id, include_turns=include_turns)
        if record is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        return self._present_thread_payload(record)

    async def list_loaded_threads(self) -> list[str]:
        return sorted(self._loaded_threads)

    async def subscribe_thread(self, thread_id: str) -> None:
        async with self._lock:
            self._loaded_threads.add(thread_id)
            self._thread_subscribers[thread_id] = self._thread_subscribers.get(thread_id, 0) + 1

    async def unsubscribe_thread(self, thread_id: str) -> bool:
        async with self._lock:
            count = self._thread_subscribers.get(thread_id, 0)
            if count <= 1:
                self._thread_subscribers.pop(thread_id, None)
            else:
                self._thread_subscribers[thread_id] = count - 1
            if self._thread_subscribers.get(thread_id, 0) == 0 and thread_id not in self._active_turns:
                self._loaded_threads.discard(thread_id)
                return True
            return False

    async def is_thread_loaded(self, thread_id: str) -> bool:
        async with self._lock:
            return thread_id in self._loaded_threads

    async def get_active_turn(self, thread_id: str) -> ActiveTurnHandle | None:
        async with self._lock:
            handle = self._active_turns.get(thread_id)
            if handle is None or handle.interrupted.is_set():
                return None
            return handle

    async def archive_thread(self, thread_id: str) -> None:
        if self._store.get_thread(thread_id) is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        self._store.archive_thread(thread_id)

    async def unarchive_thread(self, thread_id: str) -> None:
        if self._store.get_thread(thread_id) is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        self._store.unarchive_thread(thread_id)

    async def set_thread_name(self, thread_id: str, name: str) -> None:
        if self._store.get_thread(thread_id) is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        self._store.set_thread_name(thread_id, name)

    async def update_thread_metadata(self, params: ThreadMetadataUpdateParams) -> ThreadRecord:
        record = self._store.update_thread_metadata(params.thread_id, params.metadata)
        return ThreadRecord.model_validate(self._present_thread_record(record))

    async def fork_thread(self, params: ThreadForkParams) -> ThreadRecord:
        record = self._store.fork_thread(
            source_thread_id=params.source_thread_id,
            thread_id=params.thread_id,
            player_uuid=params.player_uuid,
            player_name=params.player_name,
            metadata=params.metadata,
        )
        self._loaded_threads.add(params.thread_id)
        return ThreadRecord.model_validate(record)

    async def compact_thread(self, params: ThreadCompactParams) -> dict[str, object]:
        if self._store.get_thread(params.thread_id) is None:
            raise KeyError(f"Unknown thread_id: {params.thread_id}")
        return self._store.compact_thread(params.thread_id)

    async def rollback_thread(self, params: ThreadRollbackParams) -> dict[str, object]:
        async with self._lock:
            existing = self._active_turns.get(params.thread_id)
            if existing is not None and not existing.interrupted.is_set():
                raise RuntimeError(f"Thread {params.thread_id} has an active turn and cannot be rolled back.")
        self._loaded_threads.add(params.thread_id)
        return self._store.rollback_thread(params.thread_id, num_turns=params.num_turns)

    async def open_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        emitter: Emitter,
    ) -> tuple[TurnRecord, ActiveTurnHandle]:
        async with self._lock:
            existing = self._active_turns.get(thread_id)
            if existing is not None and not existing.interrupted.is_set():
                raise RuntimeError(f"Thread {thread_id} already has an active turn.")
            self._store.set_thread_status(thread_id, "running")
            self._loaded_threads.add(thread_id)
            handle = ActiveTurnHandle(thread_id=thread_id, turn_id=turn_id, emitter=emitter)
            self._active_turns[thread_id] = handle
        now = datetime.now(timezone.utc).isoformat()
        return TurnRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            status="running",
            created_at=now,
            updated_at=now,
        ), handle

    async def attach_task(self, thread_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            handle = self._active_turns.get(thread_id)
            if handle is not None:
                handle.task = task

    async def submit_tool_result(self, submission: ToolCallResultSubmission) -> None:
        handle = self._active_turns.get(submission.thread_id)
        if handle is None or handle.turn_id != submission.turn_id:
            raise KeyError(f"No active turn for thread {submission.thread_id}.")
        future = handle.tool_waiters.get(submission.item_id)
        if future is None or future.done():
            raise KeyError(f"Unknown tool item_id: {submission.item_id}")
        future.set_result(submission)

    async def submit_approval(self, response: ApprovalResponse) -> None:
        handle = self._active_turns.get(response.thread_id)
        if handle is None or handle.turn_id != response.turn_id:
            raise KeyError(f"No active turn for thread {response.thread_id}.")
        future = handle.approval_waiters.get(response.approval_id)
        if future is None or future.done():
            raise KeyError(f"Unknown approval_id: {response.approval_id}")
        future.set_result(response)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        handle = self._active_turns.get(thread_id)
        if handle is None or handle.turn_id != turn_id:
            raise KeyError(f"No active turn for thread {thread_id}.")
        handle.interrupted.set()

    async def submit_steer(self, params: TurnSteerParams) -> str:
        handle = self._active_turns.get(params.thread_id)
        if handle is None:
            raise RuntimeError(f"Thread {params.thread_id} has no active turn.")
        if handle.turn_id != params.expected_turn_id:
            raise RuntimeError(
                f"Active turn mismatch for thread {params.thread_id}: expected {params.expected_turn_id}, got {handle.turn_id}."
            )
        if handle.interrupted.is_set():
            raise RuntimeError(f"Thread {params.thread_id} is already interrupted.")
        handle.add_steers(list(params.input))
        return handle.turn_id

    async def complete_turn(self, *, thread_id: str, turn_id: str, status: str, final_reply: str | None = None) -> None:
        async with self._lock:
            handle = self._active_turns.get(thread_id)
            if handle is not None and handle.turn_id == turn_id:
                self._active_turns.pop(thread_id, None)
            self._store.finish_turn_record(turn_id, status=status, final_reply=final_reply)
            self._store.set_thread_status(thread_id, "idle")
            if self._thread_subscribers.get(thread_id, 0) == 0:
                self._loaded_threads.discard(thread_id)

    def _present_thread_record(self, record: dict[str, object]) -> dict[str, object]:
        payload = dict(record)
        payload["status"] = self._external_thread_status(str(record["thread_id"]), str(record.get("status") or "idle"))
        payload["status_detail"] = self._status_detail(str(payload["status"]))
        return payload

    def _present_thread_payload(self, record: dict[str, object]) -> dict[str, object]:
        payload = dict(record)
        payload["status"] = self._external_thread_status(str(record["thread_id"]), str(record.get("status") or "idle"))
        payload["status_detail"] = self._status_detail(str(payload["status"]))
        return payload

    def _external_thread_status(self, thread_id: str, stored_status: str) -> str:
        handle = self._active_turns.get(thread_id)
        if handle is not None and not handle.interrupted.is_set():
            return stored_status
        if thread_id in self._loaded_threads:
            return stored_status
        return "notLoaded"

    def _status_detail(self, status: str) -> dict[str, object]:
        if status == "notLoaded":
            return {"type": "notLoaded"}
        if status in {"running", "active"}:
            return {"type": "active", "active_flags": []}
        if status == "failed":
            return {"type": "systemError"}
        return {"type": "idle"}
