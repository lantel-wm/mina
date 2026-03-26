from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_terms(text: str) -> set[str]:
    return {match.group(0).lower() for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", text)}


def _score_text(query: str, *parts: str) -> float:
    query_terms = _normalize_terms(query)
    if not query_terms:
        return 0.0
    haystack_terms: set[str] = set()
    for part in parts:
        haystack_terms.update(_normalize_terms(part))
    overlap = query_terms & haystack_terms
    if not overlap:
        return 0.0
    return len(overlap) / max(len(query_terms), 1)


def _is_brief_follow_up_query(query: str) -> bool:
    stripped = query.strip()
    if not stripped:
        return False
    terms = _normalize_terms(stripped)
    return len(stripped) <= 8 and len(terms) <= 1


def _has_recent_dialogue_signal(memory: dict[str, Any]) -> bool:
    metadata = memory.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("recent_dialogue_turn")
        or metadata.get("dialogue_turn")
        or metadata.get("open_follow_up")
    )


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:96] or "unknown"


class Store:
    def __init__(self, db_path: Path, data_dir: Path | None = None) -> None:
        self._db_path = db_path
        self._data_dir = data_dir or db_path.parent
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    user_message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_reply TEXT,
                    runtime_state_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS step_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    side_effect_summary TEXT NOT NULL,
                    timing_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_ref TEXT NOT NULL UNIQUE,
                    thread_id TEXT,
                    confirmation_id TEXT NOT NULL UNIQUE,
                    effect_summary TEXT NOT NULL,
                    action_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    task_type TEXT NOT NULL,
                    owner_player TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    requires_confirmation INTEGER NOT NULL,
                    parent_task_id TEXT,
                    origin_turn_id TEXT,
                    continuity_score REAL NOT NULL DEFAULT 0,
                    last_active_at TEXT,
                    constraints_json TEXT NOT NULL,
                    artifacts_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    step_order INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    task_id TEXT,
                    turn_id TEXT,
                    kind TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semantic_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    memory_type TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    thread_id TEXT,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    task_id TEXT,
                    artifact_refs_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    transcript_path TEXT,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL UNIQUE,
                    player_uuid TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    name TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turn_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    item_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_phase1_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL UNIQUE,
                    raw_memory TEXT NOT NULL,
                    rollout_summary TEXT NOT NULL,
                    rollout_slug TEXT,
                    source_updated_at TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_usage TEXT,
                    selected_for_phase2 INTEGER NOT NULL DEFAULT 0,
                    selected_for_phase2_source_updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_pipeline_state (
                    pipeline_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_summaries_thread_id ON thread_summaries(thread_id)"
            )
            self._ensure_column(connection, "threads", "archived", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "threads", "name", "TEXT")
            self._ensure_column(connection, "turns", "task_id", "TEXT")
            self._ensure_column(connection, "turns", "thread_id", "TEXT")
            self._ensure_column(connection, "execution_records", "task_id", "TEXT")
            self._ensure_column(connection, "execution_records", "state_fingerprint", "TEXT")
            self._ensure_column(connection, "execution_records", "artifact_refs_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "pending_confirmations", "task_id", "TEXT")
            self._ensure_column(connection, "pending_confirmations", "thread_id", "TEXT")
            self._ensure_column(connection, "memories", "thread_id", "TEXT")
            self._ensure_column(connection, "tasks", "thread_id", "TEXT")
            self._ensure_column(connection, "tasks", "parent_task_id", "TEXT")
            self._ensure_column(connection, "tasks", "origin_turn_id", "TEXT")
            self._ensure_column(connection, "tasks", "continuity_score", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tasks", "last_active_at", "TEXT")
            self._ensure_column(connection, "artifacts", "thread_id", "TEXT")
            self._ensure_column(connection, "semantic_memories", "thread_id", "TEXT")
            self._ensure_column(connection, "episodic_memories", "thread_id", "TEXT")
            self._migrate_session_summaries(connection)
            self._backfill_thread_id(connection, "turns")
            self._backfill_thread_id(connection, "pending_confirmations")
            self._backfill_thread_id(connection, "memories")
            self._backfill_thread_id(connection, "tasks")
            self._backfill_thread_id(connection, "artifacts")
            self._backfill_thread_id(connection, "semantic_memories")
            self._backfill_thread_id(connection, "episodic_memories")

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _backfill_thread_id(self, connection: sqlite3.Connection, table: str) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "thread_id" not in columns or "session_ref" not in columns:
            return
        connection.execute(
            f"UPDATE {table} SET thread_id = session_ref WHERE (thread_id IS NULL OR thread_id = '') AND session_ref IS NOT NULL"
        )

    def _migrate_session_summaries(self, connection: sqlite3.Connection) -> None:
        table_names = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "session_summaries" not in table_names:
            return
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(session_summaries)").fetchall()
        }
        has_thread_id = "thread_id" in columns
        select_thread_expr = "thread_id" if has_thread_id else "session_ref"
        rows = connection.execute(
            f"""
            SELECT {select_thread_expr} AS thread_id, summary, transcript_path, metadata_json, updated_at
            FROM session_summaries
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO thread_summaries(thread_id, summary, transcript_path, metadata_json, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    summary = excluded.summary,
                    transcript_path = excluded.transcript_path,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    row["thread_id"],
                    row["summary"],
                    row["transcript_path"],
                    row["metadata_json"],
                    row["updated_at"],
                ),
            )

    def ensure_thread(
        self,
        thread_id: str,
        *,
        player_uuid: str,
        player_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO threads(thread_id, player_uuid, player_name, status, archived, name, metadata_json, created_at, updated_at)
                VALUES(?, ?, ?, 'idle', 0, NULL, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    player_uuid = excluded.player_uuid,
                    player_name = excluded.player_name,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, player_uuid, player_name, json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT thread_id, player_uuid, player_name, status, archived, name, metadata_json, created_at, updated_at
                FROM threads
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "thread_id": row["thread_id"],
            "player_uuid": row["player_uuid"],
            "player_name": row["player_name"],
            "status": row["status"],
            "archived": bool(row["archived"]),
            "name": row["name"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_threads(
        self,
        *,
        limit: int = 50,
        archived: bool | None = None,
        search_term: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = ["1 = 1"]
        params: list[Any] = []
        if archived is not None:
            clauses.append("archived = ?")
            params.append(1 if archived else 0)
        if search_term is not None and search_term.strip():
            clauses.append("(thread_id LIKE ? OR player_name LIKE ? OR COALESCE(name, '') LIKE ?)")
            needle = f"%{search_term.strip()}%"
            params.extend([needle, needle, needle])
        params.append(max(int(limit), 1))
        query = f"""
            SELECT thread_id, player_uuid, player_name, status, archived, name, metadata_json, created_at, updated_at
            FROM threads
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
        """
        with self.connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            {
                "thread_id": row["thread_id"],
                "player_uuid": row["player_uuid"],
                "player_name": row["player_name"],
                "status": row["status"],
                "archived": bool(row["archived"]),
                "name": row["name"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any] | None:
        thread = self.get_thread(thread_id)
        if thread is None:
            return None
        if not include_turns:
            return thread
        turns = self.list_thread_turns(thread_id)
        for turn in turns:
            turn["items"] = self.list_turn_items(thread_id, turn["turn_id"])
        return {
            **thread,
            "turns": turns,
        }

    def fork_thread(
        self,
        *,
        source_thread_id: str,
        thread_id: str,
        player_uuid: str | None = None,
        player_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = self.get_thread(source_thread_id)
        if source is None:
            raise KeyError(f"Unknown thread_id: {source_thread_id}")
        self.ensure_thread(
            thread_id,
            player_uuid=player_uuid or str(source["player_uuid"]),
            player_name=player_name or str(source["player_name"]),
            metadata={
                **dict(source.get("metadata", {})),
                **(metadata or {}),
                "forked_from": source_thread_id,
            },
        )
        turns = self.list_thread_turns(source_thread_id)
        for index, turn in enumerate(turns, start=1):
            cloned_turn_id = f"{thread_id}__fork_{index:03d}"
            self.create_thread_turn(
                cloned_turn_id,
                thread_id,
                str(turn.get("user_message") or ""),
                {},
                task_id=None,
            )
            if str(turn.get("status") or "") != "running":
                self.finish_thread_turn(
                    cloned_turn_id,
                    str(turn.get("final_reply") or ""),
                    status=str(turn.get("status") or "completed"),
                )
            for item_index, item in enumerate(self.list_turn_items(source_thread_id, str(turn["turn_id"])), start=1):
                self.create_turn_item(
                    thread_id=thread_id,
                    turn_id=cloned_turn_id,
                    item_id=f"{cloned_turn_id}__item_{item_index:03d}",
                    item_kind=str(item.get("item_kind") or "item"),
                    payload=dict(item.get("payload") or {}),
                    status=str(item.get("status") or "completed"),
                )
        return self.get_thread(thread_id) or {"thread_id": thread_id}

    def archive_thread(self, thread_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE threads
                SET archived = 1, updated_at = ?
                WHERE thread_id = ?
                """,
                (_utc_now(), thread_id),
            )

    def unarchive_thread(self, thread_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE threads
                SET archived = 0, updated_at = ?
                WHERE thread_id = ?
                """,
                (_utc_now(), thread_id),
            )

    def set_thread_name(self, thread_id: str, name: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE threads
                SET name = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (name, _utc_now(), thread_id),
            )

    def update_thread_metadata(self, thread_id: str, metadata_patch: dict[str, Any]) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        merged_metadata = dict(thread.get("metadata", {}))
        merged_metadata.update(metadata_patch)
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE threads
                SET metadata_json = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (json.dumps(merged_metadata, ensure_ascii=False), _utc_now(), thread_id),
            )
        return self.get_thread(thread_id) or thread

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        turns = self.list_thread_turns(thread_id)
        summary = self._build_compact_summary_text(thread_id, turns)
        summary_record = self.write_compact_summary(
            thread_id,
            summary,
            metadata={"turn_count": len(turns)},
        )
        return {
            "thread_id": thread_id,
            "summary": summary,
            "path": summary_record["path"],
            "transcript_path": summary_record["transcript_path"],
        }

    def rollback_thread(self, thread_id: str, *, num_turns: int) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        turns = self.list_thread_turns(thread_id)
        if not turns:
            raise RuntimeError(f"Thread {thread_id} has no turns to roll back.")
        remove_count = min(max(num_turns, 1), len(turns))
        removed_turns = turns[-remove_count:]
        removed_turn_ids = [str(turn["turn_id"]) for turn in removed_turns]
        removed_task_ids = {
            str(turn["task_id"])
            for turn in removed_turns
            if turn.get("task_id")
        }
        placeholders = ",".join("?" for _ in removed_turn_ids)
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                f"DELETE FROM turn_items WHERE thread_id = ? AND turn_id IN ({placeholders})",
                (thread_id, *removed_turn_ids),
            )
            connection.execute(
                f"DELETE FROM step_events WHERE turn_id IN ({placeholders})",
                tuple(removed_turn_ids),
            )
            connection.execute(
                f"DELETE FROM execution_records WHERE turn_id IN ({placeholders})",
                tuple(removed_turn_ids),
            )
            connection.execute(
                f"DELETE FROM turns WHERE thread_id = ? AND turn_id IN ({placeholders})",
                (thread_id, *removed_turn_ids),
            )
            connection.execute(
                f"DELETE FROM artifacts WHERE thread_id = ? AND turn_id IN ({placeholders})",
                (thread_id, *removed_turn_ids),
            )
            if removed_task_ids:
                task_placeholders = ",".join("?" for _ in removed_task_ids)
                connection.execute(
                    f"DELETE FROM task_steps WHERE task_id IN ({task_placeholders})",
                    tuple(removed_task_ids),
                )
                connection.execute(
                    f"DELETE FROM episodic_memories WHERE thread_id = ? AND task_id IN ({task_placeholders})",
                    (thread_id, *removed_task_ids),
                )
                connection.execute(
                    f"DELETE FROM artifacts WHERE thread_id = ? AND task_id IN ({task_placeholders})",
                    (thread_id, *removed_task_ids),
                )
                connection.execute(
                    f"DELETE FROM tasks WHERE thread_id = ? AND task_id IN ({task_placeholders})",
                    (thread_id, *removed_task_ids),
                )
            connection.execute(
                "DELETE FROM memory_phase1_outputs WHERE thread_id = ?",
                (thread_id,),
            )
            connection.execute(
                """
                UPDATE threads
                SET status = 'idle', updated_at = ?
                WHERE thread_id = ?
                """,
                (now, thread_id),
            )
        self._rewrite_thread_logs_after_rollback(
            thread_id,
            removed_turn_ids=removed_turn_ids,
            payload={
                "ts": now,
                "event": "thread_rollback",
                "thread_id": thread_id,
                "num_turns": remove_count,
                "removed_turn_ids": removed_turn_ids,
            },
        )
        remaining_turns = self.list_thread_turns(thread_id)
        self.write_compact_summary(
            thread_id,
            self._build_compact_summary_text(thread_id, remaining_turns),
            metadata={
                "turn_count": len(remaining_turns),
                "rollback": {
                    "rolled_back_at": now,
                    "num_turns": remove_count,
                    "removed_turn_ids": removed_turn_ids,
                },
            },
        )
        return self.read_thread(thread_id, include_turns=True) or {
            **thread,
            "status": "idle",
            "turns": [],
        }

    def set_thread_status(self, thread_id: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE threads
                SET status = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (status, _utc_now(), thread_id),
            )

    def ensure_turn_record(self, thread_id: str, turn_id: str) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, session_ref, thread_id, task_id, user_message, status, runtime_state_json, created_at, updated_at
                )
                VALUES(?, ?, ?, NULL, '', 'running', '{}', ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    session_ref = excluded.session_ref,
                    thread_id = excluded.thread_id,
                    status = 'running',
                    updated_at = excluded.updated_at
                """,
                (turn_id, thread_id, thread_id, now, now),
            )

    def get_turn_record(self, turn_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT turn_id, thread_id, status, created_at, updated_at, final_reply
                FROM turns
                WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "final_reply": row["final_reply"],
        }

    def finish_turn_record(self, turn_id: str, *, status: str, final_reply: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = ?, final_reply = COALESCE(?, final_reply), updated_at = ?
                WHERE turn_id = ?
                """,
                (status, final_reply, _utc_now(), turn_id),
            )

    def create_turn_item(
        self,
        *,
        thread_id: str,
        turn_id: str,
        item_id: str,
        item_kind: str,
        payload: dict[str, Any],
        status: str = "started",
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO turn_items(item_id, thread_id, turn_id, item_kind, status, payload_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    thread_id,
                    turn_id,
                    item_kind,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.append_rollout_event(
            thread_id,
            {
                "ts": now,
                "turn_id": turn_id,
                "item_id": item_id,
                "item_kind": item_kind,
                "status": status,
                "payload": payload,
            },
        )

    def update_turn_item(self, item_id: str, *, status: str, payload: dict[str, Any]) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turn_items
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (status, json.dumps(payload, ensure_ascii=False), now, item_id),
            )

    def list_turn_items(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT item_id, thread_id, turn_id, item_kind, status, payload_json, created_at, updated_at
                FROM turn_items
                WHERE thread_id = ? AND turn_id = ?
                ORDER BY id ASC
                """,
                (thread_id, turn_id),
            ).fetchall()
        return [
            {
                "item_id": row["item_id"],
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "item_kind": row["item_kind"],
                "status": row["status"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def create_thread_turn(
        self,
        turn_id: str,
        thread_id: str,
        user_message: str,
        runtime_state: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, session_ref, thread_id, task_id, user_message, status, runtime_state_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (turn_id, thread_id, thread_id, task_id, user_message, json.dumps(runtime_state, ensure_ascii=False), now, now),
            )
        self.append_thread_transcript_event(
            thread_id,
            {
                "ts": now,
                "turn_id": turn_id,
                "task_id": task_id,
                "role": "user",
                "content": user_message,
            },
        )

    def update_turn_state(
        self,
        turn_id: str,
        runtime_state: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET runtime_state_json = ?, task_id = COALESCE(?, task_id), updated_at = ?
                WHERE turn_id = ?
                """,
                (json.dumps(runtime_state, ensure_ascii=False), task_id, _utc_now(), turn_id),
            )

    def finish_turn(
        self,
        turn_id: str,
        final_reply: str,
        *,
        status: str = "completed",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = ?, final_reply = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (status, final_reply, _utc_now(), turn_id),
            )
            row = connection.execute(
                "SELECT thread_id, task_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is not None:
            self.append_thread_transcript_event(
                row["thread_id"],
                {
                    "ts": _utc_now(),
                    "turn_id": turn_id,
                    "task_id": row["task_id"],
                    "role": "assistant",
                    "status": status,
                    "content": final_reply,
                },
            )

    def finish_thread_turn(
        self,
        turn_id: str,
        final_reply: str,
        *,
        status: str = "completed",
    ) -> None:
        self.finish_turn(turn_id, final_reply, status=status)

    def get_turn_state(self, turn_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT runtime_state_json FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None or row["runtime_state_json"] is None:
            return None
        return json.loads(row["runtime_state_json"])

    def log_step_event(self, turn_id: str, step_index: int, event_type: str, payload: dict[str, Any]) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO step_events(turn_id, step_index, event_type, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (turn_id, step_index, event_type, json.dumps(payload, ensure_ascii=False), now),
            )
            row = connection.execute(
                "SELECT thread_id, task_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is not None:
            self.append_thread_event(
                row["thread_id"],
                {
                    "ts": now,
                    "turn_id": turn_id,
                    "task_id": row["task_id"],
                    "step_index": step_index,
                    "event_type": event_type,
                    "payload": payload,
                },
            )

    def log_execution_record(
        self,
        turn_id: str,
        intent_id: str,
        capability_id: str,
        risk_class: str,
        status: str,
        observations: dict[str, Any],
        side_effect_summary: str,
        timing_ms: int,
        *,
        task_id: str | None = None,
        state_fingerprint: str | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO execution_records(
                    turn_id, intent_id, capability_id, risk_class, status, observations_json,
                    side_effect_summary, timing_ms, task_id, state_fingerprint, artifact_refs_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    intent_id,
                    capability_id,
                    risk_class,
                    status,
                    json.dumps(observations, ensure_ascii=False),
                    side_effect_summary,
                    timing_ms,
                    task_id,
                    state_fingerprint,
                    json.dumps(artifact_refs or [], ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def list_thread_turns(self, thread_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT turn_id, task_id, user_message, final_reply, status, created_at
                    FROM turns
                    WHERE thread_id = ?
                    ORDER BY id ASC
                    """,
                    (thread_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT turn_id, task_id, user_message, final_reply, status, created_at
                    FROM turns
                    WHERE thread_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (thread_id, limit),
                ).fetchall()
                rows = rows[::-1]
        return [dict(row) for row in rows]

    def list_recent_thread_turns(self, thread_id: str, limit: int = 12) -> list[dict[str, Any]]:
        return self.list_thread_turns(thread_id, limit=limit)

    def add_thread_memory(self, thread_id: str, kind: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO memories(session_ref, thread_id, kind, content, metadata_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (thread_id, thread_id, kind, content, json.dumps(metadata or {}, ensure_ascii=False), _utc_now()),
            )

    def list_thread_memories(self, thread_id: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT kind, content, metadata_json, created_at
                FROM memories
                WHERE thread_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [
            {
                "kind": row["kind"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ][::-1]

    def add_thread_semantic_memory(
        self,
        thread_id: str,
        memory_type: str,
        memory_key: str,
        value: str,
        summary: str,
        *,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        now = _utc_now()
        with self.connection() as connection:
            existing_rows = connection.execute(
                """
                SELECT id, memory_id, created_at
                FROM semantic_memories
                WHERE thread_id = ? AND memory_type = ? AND memory_key = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (thread_id, memory_type, memory_key),
            ).fetchall()
            if existing_rows:
                primary = existing_rows[0]
                connection.execute(
                    """
                    UPDATE semantic_memories
                    SET value = ?, summary = ?, confidence = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        value,
                        summary,
                        confidence,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        now,
                        primary["id"],
                    ),
                )
                duplicate_ids = [row["id"] for row in existing_rows[1:]]
                if duplicate_ids:
                    placeholders = ", ".join("?" for _ in duplicate_ids)
                    connection.execute(
                        f"DELETE FROM semantic_memories WHERE id IN ({placeholders})",
                        duplicate_ids,
                    )
                return str(primary["memory_id"])

            memory_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO semantic_memories(
                    memory_id, session_ref, thread_id, memory_type, memory_key, value, summary, confidence, metadata_json, updated_at, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    thread_id,
                    thread_id,
                    memory_type,
                    memory_key,
                    value,
                    summary,
                    confidence,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return memory_id

    def add_thread_episodic_memory(
        self,
        thread_id: str,
        summary: str,
        *,
        tags: list[str] | None = None,
        task_id: str | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        now = _utc_now()
        memory_id = str(uuid.uuid4())
        payload = {
            "memory_id": memory_id,
            "thread_id": thread_id,
            "summary": summary,
            "tags": tags or [],
            "task_id": task_id,
            "artifact_refs": artifact_refs or [],
            "metadata": metadata or {},
            "created_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO episodic_memories(
                    memory_id, session_ref, thread_id, summary, tags_json, task_id, artifact_refs_json, metadata_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    thread_id,
                    thread_id,
                    summary,
                    json.dumps(tags or [], ensure_ascii=False),
                    task_id,
                    json.dumps(artifact_refs or [], ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
        self.append_episode_log(payload)
        return memory_id

    def list_thread_semantic_memories(self, thread_id: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, memory_type, memory_key, value, summary, confidence, metadata_json, updated_at, created_at
                FROM semantic_memories
                WHERE thread_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "kind": "semantic",
                "memory_type": row["memory_type"],
                "memory_key": row["memory_key"],
                "value": row["value"],
                "summary": row["summary"],
                "confidence": row["confidence"],
                "metadata": json.loads(row["metadata_json"]),
                "updated_at": row["updated_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_thread_episodic_memories(self, thread_id: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, summary, tags_json, task_id, artifact_refs_json, metadata_json, created_at
                FROM episodic_memories
                WHERE thread_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "kind": "episodic",
                "summary": row["summary"],
                "tags": json.loads(row["tags_json"]),
                "task_id": row["task_id"],
                "artifact_refs": self._normalize_artifact_list(json.loads(row["artifact_refs_json"])),
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search_thread_memories(self, thread_id: str, query: str, limit: int = 6) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for memory in self.list_thread_semantic_memories(thread_id, limit=24):
            score = _score_text(query, memory["summary"], memory["value"], memory["memory_key"])
            if score > 0:
                scored.append((score, memory))
        for memory in self.list_thread_episodic_memories(thread_id, limit=24):
            score = _score_text(query, memory["summary"], " ".join(memory["tags"]))
            if score > 0:
                scored.append((score, memory))
        for memory in self.list_thread_memories(thread_id, limit=24):
            score = _score_text(query, memory["content"], memory["kind"])
            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "kind": f"legacy:{memory['kind']}",
                            "summary": memory["content"],
                            "metadata": memory["metadata"],
                            "created_at": memory["created_at"],
                        },
                    )
                )
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored and _is_brief_follow_up_query(query):
            for memory in self.list_thread_episodic_memories(thread_id, limit=max(limit, 6)):
                if _has_recent_dialogue_signal(memory):
                    scored.append((0.05, memory))
        results: list[dict[str, Any]] = []
        for score, payload in scored[:limit]:
            entry = dict(payload)
            entry["score"] = round(score, 4)
            results.append(entry)
        return results

    def create_thread_task(
        self,
        thread_id: str,
        owner_player: str,
        goal: str,
        *,
        task_type: str = "user_request",
        status: str = "pending",
        priority: str = "normal",
        risk_class: str = "read_only",
        requires_confirmation: bool = False,
        parent_task_id: str | None = None,
        origin_turn_id: str | None = None,
        continuity_score: float = 0.0,
        last_active_at: str | None = None,
        constraints: list[str] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        payload = {
            "task_id": task_id,
            "thread_id": thread_id,
            "task_type": task_type,
            "owner_player": owner_player,
            "goal": goal,
            "status": status,
            "priority": priority,
            "risk_class": risk_class,
            "requires_confirmation": requires_confirmation,
            "parent_task_id": parent_task_id,
            "origin_turn_id": origin_turn_id,
            "continuity_score": continuity_score,
            "last_active_at": last_active_at or now,
            "constraints": constraints or [],
            "artifacts": [],
            "summary": summary or {},
            "created_at": now,
            "updated_at": now,
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, session_ref, thread_id, task_type, owner_player, goal, status, priority, risk_class,
                    requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                    constraints_json, artifacts_json, summary_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    thread_id,
                    thread_id,
                    task_type,
                    owner_player,
                    goal,
                    status,
                    priority,
                    risk_class,
                    1 if requires_confirmation else 0,
                    parent_task_id,
                    origin_turn_id,
                    continuity_score,
                    last_active_at or now,
                    json.dumps(constraints or [], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    json.dumps(summary or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return payload

    def update_task(
        self,
        task_id: str,
        *,
        goal: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        risk_class: str | None = None,
        requires_confirmation: bool | None = None,
        parent_task_id: str | None = None,
        origin_turn_id: str | None = None,
        continuity_score: float | None = None,
        last_active_at: str | None = None,
        constraints: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET goal = ?, status = ?, priority = ?, risk_class = ?, requires_confirmation = ?,
                    parent_task_id = ?, origin_turn_id = ?, continuity_score = ?, last_active_at = ?,
                    constraints_json = ?, artifacts_json = ?, summary_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    goal if goal is not None else current["goal"],
                    status if status is not None else current["status"],
                    priority if priority is not None else current["priority"],
                    risk_class if risk_class is not None else current["risk_class"],
                    1 if (requires_confirmation if requires_confirmation is not None else current["requires_confirmation"]) else 0,
                    parent_task_id if parent_task_id is not None else current.get("parent_task_id"),
                    origin_turn_id if origin_turn_id is not None else current.get("origin_turn_id"),
                    continuity_score if continuity_score is not None else current.get("continuity_score", 0.0),
                    last_active_at if last_active_at is not None else current.get("last_active_at"),
                    json.dumps(constraints if constraints is not None else current["constraints"], ensure_ascii=False),
                    json.dumps(artifacts if artifacts is not None else current["artifacts"], ensure_ascii=False),
                    json.dumps(summary if summary is not None else current["summary"], ensure_ascii=False),
                    _utc_now(),
                    task_id,
                ),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, thread_id, task_type, owner_player, goal, status, priority, risk_class,
                       requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                       constraints_json, artifacts_json, summary_json, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._task_row_to_dict(row)

    def get_active_thread_task(self, thread_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, thread_id, task_type, owner_player, goal, status, priority, risk_class,
                       requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                       constraints_json, artifacts_json, summary_json, created_at, updated_at
                FROM tasks
                WHERE thread_id = ? AND status IN ('pending', 'analyzing', 'planned', 'awaiting_confirmation', 'in_progress', 'blocked')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return self._task_row_to_dict(row)

    def get_latest_thread_task(
        self,
        thread_id: str,
        *,
        excluded_statuses: Iterable[str] | None = ("failed", "canceled"),
    ) -> dict[str, Any] | None:
        clauses = ["thread_id = ?"]
        params: list[Any] = [thread_id]
        normalized_excluded = [str(status) for status in (excluded_statuses or ()) if str(status).strip()]
        if normalized_excluded:
            placeholders = ", ".join("?" for _ in normalized_excluded)
            clauses.append(f"status NOT IN ({placeholders})")
            params.extend(normalized_excluded)
        query = f"""
            SELECT task_id, thread_id, task_type, owner_player, goal, status, priority, risk_class,
                   requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                   constraints_json, artifacts_json, summary_json, created_at, updated_at
            FROM tasks
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC
            LIMIT 1
        """
        with self.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return self._task_row_to_dict(row)

    def replace_task_steps(self, task_id: str, steps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        now = _utc_now()
        normalized: list[dict[str, Any]] = []
        with self.connection() as connection:
            connection.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
            for index, step in enumerate(steps):
                step_key = str(step.get("step_key") or step.get("name") or f"step_{index + 1}")
                title = str(step.get("title") or step.get("name") or step_key)
                status = str(step.get("status") or "pending")
                detail = step.get("detail")
                connection.execute(
                    """
                    INSERT INTO task_steps(task_id, step_key, title, status, detail, step_order, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, step_key, title, status, detail, index, now),
                )
                normalized.append(
                    {
                        "step_key": step_key,
                        "title": title,
                        "status": status,
                        "detail": detail,
                        "step_order": index,
                    }
                )
        return normalized

    def list_task_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT step_key, title, status, detail, step_order, updated_at
                FROM task_steps
                WHERE task_id = ?
                ORDER BY step_order ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def write_thread_artifact(
        self,
        thread_id: str,
        task_id: str | None,
        turn_id: str | None,
        kind: str,
        payload: Any,
        summary: str,
        *,
        content_type: str = "application/json",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = f"artifact_{uuid.uuid4().hex[:12]}"
        extension = ".json" if "json" in content_type else ".txt"
        if task_id is not None:
            target_dir = self._data_dir / "tasks" / _safe_segment(task_id) / "artifacts"
        else:
            target_dir = self._data_dir / "sessions" / _safe_segment(thread_id) / "observations"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{artifact_id}{extension}"
        if "json" in content_type:
            target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target_path.write_text(str(payload), encoding="utf-8")
        char_count = len(target_path.read_text(encoding="utf-8"))
        record = {
            "artifact_id": artifact_id,
            "thread_id": thread_id,
            "task_id": task_id,
            "turn_id": turn_id,
            "kind": kind,
            "path": str(target_path),
            "summary": summary,
            "content_type": content_type,
            "char_count": char_count,
            "metadata": metadata or {},
            "created_at": _utc_now(),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, session_ref, thread_id, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    thread_id,
                    thread_id,
                    task_id,
                    turn_id,
                    kind,
                    str(target_path),
                    summary,
                    content_type,
                    char_count,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    record["created_at"],
                ),
            )
        return record

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT artifact_id, thread_id, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                FROM artifacts
                WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        path = Path(row["artifact_path"])
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return {
            "artifact_id": row["artifact_id"],
            "thread_id": row["thread_id"],
            "task_id": row["task_id"],
            "turn_id": row["turn_id"],
            "kind": row["kind"],
            "path": row["artifact_path"],
            "summary": row["summary"],
            "content_type": row["content_type"],
            "char_count": row["char_count"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "content": content,
        }

    def list_thread_artifacts(self, thread_id: str, *, task_id: str | None = None, limit: int = 24) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if task_id is None:
                rows = connection.execute(
                    """
                    SELECT artifact_id, thread_id, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                    FROM artifacts
                    WHERE thread_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (thread_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT artifact_id, thread_id, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                    FROM artifacts
                    WHERE thread_id = ? AND task_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (thread_id, task_id, limit),
                ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            records.append(
                {
                    "artifact_id": row["artifact_id"],
                    "thread_id": row["thread_id"],
                    "task_id": row["task_id"],
                    "turn_id": row["turn_id"],
                    "kind": row["kind"],
                    "path": row["artifact_path"],
                    "summary": row["summary"],
                    "content_type": row["content_type"],
                    "char_count": row["char_count"],
                    "metadata": json.loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                }
            )
        return records

    def search_thread_artifacts(
        self,
        thread_id: str,
        query: str,
        *,
        task_id: str | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for artifact in self.list_thread_artifacts(thread_id, task_id=task_id, limit=48):
            path = Path(artifact["path"])
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            score = _score_text(query, artifact["summary"], artifact["kind"], content[:4000])
            if score <= 0:
                continue
            preview = content[:800]
            scored.append(
                (
                    score,
                    {
                        **artifact,
                        "content_preview": preview,
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[dict[str, Any]] = []
        for score, payload in scored[:limit]:
            entry = dict(payload)
            entry["score"] = round(score, 4)
            results.append(entry)
        return results

    def upsert_thread_summary(
        self,
        thread_id: str,
        summary: str,
        *,
        transcript_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO thread_summaries(thread_id, summary, transcript_path, metadata_json, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    summary = excluded.summary,
                    transcript_path = excluded.transcript_path,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, summary, transcript_path, json.dumps(metadata or {}, ensure_ascii=False), now),
            )

    def get_thread_summary(self, thread_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT thread_id, summary, transcript_path, metadata_json, updated_at
                FROM thread_summaries
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "thread_id": row["thread_id"],
            "summary": row["summary"],
            "transcript_path": row["transcript_path"],
            "metadata": json.loads(row["metadata_json"]),
            "updated_at": row["updated_at"],
        }

    def upsert_memory_phase1_output(
        self,
        *,
        thread_id: str,
        raw_memory: str,
        rollout_summary: str,
        rollout_slug: str | None,
        source_updated_at: str,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_phase1_outputs(
                    thread_id, raw_memory, rollout_summary, rollout_slug,
                    source_updated_at, generated_at, usage_count, last_usage,
                    selected_for_phase2, selected_for_phase2_source_updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 0, NULL, 0, NULL)
                ON CONFLICT(thread_id) DO UPDATE SET
                    raw_memory = excluded.raw_memory,
                    rollout_summary = excluded.rollout_summary,
                    rollout_slug = excluded.rollout_slug,
                    source_updated_at = excluded.source_updated_at,
                    generated_at = excluded.generated_at
                """,
                (thread_id, raw_memory, rollout_summary, rollout_slug, source_updated_at, now),
            )

    def list_memory_phase1_outputs(self, *, limit: int = 64) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT thread_id, raw_memory, rollout_summary, rollout_slug, source_updated_at,
                       generated_at, usage_count, last_usage, selected_for_phase2,
                       selected_for_phase2_source_updated_at
                FROM memory_phase1_outputs
                ORDER BY generated_at DESC, thread_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_memory_phase1_selected(
        self,
        thread_ids: list[str],
        *,
        source_updated_at_by_thread: dict[str, str] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE memory_phase1_outputs
                SET selected_for_phase2 = 0
                """
            )
            for thread_id in thread_ids:
                connection.execute(
                    """
                    UPDATE memory_phase1_outputs
                    SET selected_for_phase2 = 1,
                        selected_for_phase2_source_updated_at = ?
                    WHERE thread_id = ?
                    """,
                    ((source_updated_at_by_thread or {}).get(thread_id), thread_id),
                )

    def read_memory_pipeline_state(self, pipeline_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT state_json, updated_at
                FROM memory_pipeline_state
                WHERE pipeline_key = ?
                """,
                (pipeline_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["state_json"])
        payload["updated_at"] = row["updated_at"]
        return payload

    def write_memory_pipeline_state(self, pipeline_key: str, state: dict[str, Any]) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_pipeline_state(pipeline_key, state_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(pipeline_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (pipeline_key, json.dumps(state, ensure_ascii=False), now),
            )

    def write_compact_summary(
        self,
        thread_id: str,
        summary: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transcript_path = self.thread_dir(thread_id) / "transcript.jsonl"
        existing_summary = self.get_thread_summary(thread_id)
        merged_metadata = dict(existing_summary.get("metadata", {})) if isinstance(existing_summary, dict) else {}
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        self.upsert_thread_summary(
            thread_id,
            summary,
            transcript_path=str(transcript_path),
            metadata=merged_metadata,
        )
        target = self.thread_dir(thread_id) / "compact_summary.md"
        target.write_text(summary, encoding="utf-8")
        return {
            "path": str(target),
            "transcript_path": str(transcript_path),
            "summary": summary,
        }

    def replace_document(self, doc_path: str, title: str, checksum: str, chunks: list[dict[str, Any]]) -> None:
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT id, checksum FROM documents WHERE doc_path = ?",
                (doc_path,),
            ).fetchone()
            if existing is not None and existing["checksum"] == checksum:
                return

            if existing is not None:
                connection.execute("DELETE FROM document_chunks WHERE doc_id = ?", (existing["id"],))
                connection.execute("DELETE FROM documents WHERE id = ?", (existing["id"],))

            cursor = connection.execute(
                """
                INSERT INTO documents(doc_path, title, checksum, chunk_count, updated_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (doc_path, title, checksum, len(chunks), _utc_now()),
            )
            doc_id = int(cursor.lastrowid)

            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO document_chunks(doc_id, chunk_index, content, token_count, metadata_json)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        chunk["chunk_index"],
                        chunk["content"],
                        chunk["token_count"],
                        json.dumps(chunk.get("metadata", {}), ensure_ascii=False),
                    ),
                )

    def list_document_chunks(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.doc_path, d.title, c.chunk_index, c.content, c.token_count, c.metadata_json
                FROM documents d
                JOIN document_chunks c ON c.doc_id = d.id
                ORDER BY d.doc_path, c.chunk_index
                """
            ).fetchall()
        return [
            {
                "doc_path": row["doc_path"],
                "title": row["title"],
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "token_count": row["token_count"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def thread_dir(self, thread_id: str) -> Path:
        target = self._data_dir / "threads" / _safe_segment(thread_id)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def append_thread_transcript_event(self, thread_id: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.thread_dir(thread_id) / "transcript.jsonl", payload)

    def append_thread_event(self, thread_id: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.thread_dir(thread_id) / "events.jsonl", payload)

    def append_rollout_event(self, thread_id: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.thread_dir(thread_id) / "rollout.jsonl", payload)

    def append_episode_log(self, payload: dict[str, Any]) -> None:
        target = self._data_dir / "memory" / "episodes.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(target, payload)

    def _build_compact_summary_text(self, thread_id: str, turns: list[dict[str, Any]]) -> str:
        lines = [
            "Mina Compact Summary",
            "",
            f"Thread: {thread_id}",
            "",
            "Recent Turns",
        ]
        if not turns:
            lines.append("- No turns recorded.")
            return "\n".join(lines)
        for turn in turns[-12:]:
            lines.append(
                f"- {turn['created_at']}: user={turn.get('user_message')!r}; "
                f"status={turn.get('status')}; reply={turn.get('final_reply')!r}"
            )
        return "\n".join(lines)

    def _rewrite_thread_logs_after_rollback(
        self,
        thread_id: str,
        *,
        removed_turn_ids: list[str],
        payload: dict[str, Any],
    ) -> None:
        removed = set(removed_turn_ids)
        thread_dir = self.thread_dir(thread_id)
        for file_name in ("transcript.jsonl", "events.jsonl", "rollout.jsonl"):
            path = thread_dir / file_name
            entries = self._read_jsonl(path)
            if entries:
                entries = [
                    entry
                    for entry in entries
                    if str(entry.get("turn_id") or "") not in removed
                ]
            if file_name == "rollout.jsonl":
                entries.append(payload)
            self._write_jsonl(path, entries)

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _task_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "thread_id": row["thread_id"],
            "task_type": row["task_type"],
            "owner_player": row["owner_player"],
            "goal": row["goal"],
            "status": row["status"],
            "priority": row["priority"],
            "risk_class": row["risk_class"],
            "requires_confirmation": bool(row["requires_confirmation"]),
            "parent_task_id": row["parent_task_id"],
            "origin_turn_id": row["origin_turn_id"],
            "continuity_score": float(row["continuity_score"] or 0.0),
            "last_active_at": row["last_active_at"],
            "constraints": json.loads(row["constraints_json"]),
            "artifacts": self._normalize_artifact_list(json.loads(row["artifacts_json"])),
            "summary": json.loads(row["summary_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _normalize_artifact_list(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized.append(self._normalize_artifact_payload(item))
        return normalized

    def _normalize_artifact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        thread_id = normalized.get("thread_id")
        if (thread_id is None or str(thread_id).strip() == "") and normalized.get("session_ref") is not None:
            normalized["thread_id"] = normalized.get("session_ref")
        normalized.pop("session_ref", None)
        return normalized
