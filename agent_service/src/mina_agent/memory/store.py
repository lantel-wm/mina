from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ContinuationState:
    continuation_id: str
    turn_id: str
    state: dict[str, Any]


class Store:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_ref TEXT NOT NULL UNIQUE,
                    last_player_name TEXT,
                    last_role TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_reply TEXT,
                    runtime_state_json TEXT,
                    pending_continuation_id TEXT,
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
                """
            )

    def ensure_session(self, session_ref: str, player_name: str, role: str) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_ref, last_player_name, last_role, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_ref) DO UPDATE SET
                    last_player_name = excluded.last_player_name,
                    last_role = excluded.last_role,
                    updated_at = excluded.updated_at
                """,
                (session_ref, player_name, role, now, now),
            )

    def create_turn(self, turn_id: str, session_ref: str, user_message: str, runtime_state: dict[str, Any]) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO turns(turn_id, session_ref, user_message, status, runtime_state_json, created_at, updated_at)
                VALUES(?, ?, ?, 'running', ?, ?, ?)
                """,
                (turn_id, session_ref, user_message, json.dumps(runtime_state, ensure_ascii=False), now, now),
            )

    def update_turn_state(self, turn_id: str, runtime_state: dict[str, Any], *, pending_continuation_id: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET runtime_state_json = ?, pending_continuation_id = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (json.dumps(runtime_state, ensure_ascii=False), pending_continuation_id, _utc_now(), turn_id),
            )

    def finish_turn(self, turn_id: str, final_reply: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = 'completed', final_reply = ?, pending_continuation_id = NULL, updated_at = ?
                WHERE turn_id = ?
                """,
                (final_reply, _utc_now(), turn_id),
            )

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
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO step_events(turn_id, step_index, event_type, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (turn_id, step_index, event_type, json.dumps(payload, ensure_ascii=False), _utc_now()),
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
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO execution_records(
                    turn_id, intent_id, capability_id, risk_class, status,
                    observations_json, side_effect_summary, timing_ms, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _utc_now(),
                ),
            )

    def list_recent_turns(self, session_ref: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT user_message, final_reply, status, created_at
                FROM turns
                WHERE session_ref = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_ref, limit),
            ).fetchall()
        return [dict(row) for row in rows][::-1]

    def put_continuation(self, continuation_id: str, turn_id: str, state: dict[str, Any]) -> None:
        state = dict(state)
        state["continuation_id"] = continuation_id
        self.update_turn_state(turn_id, state, pending_continuation_id=continuation_id)

    def get_continuation(self, continuation_id: str) -> ContinuationState | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT turn_id, runtime_state_json
                FROM turns
                WHERE pending_continuation_id = ?
                """,
                (continuation_id,),
            ).fetchone()
        if row is None:
            return None
        return ContinuationState(
            continuation_id=continuation_id,
            turn_id=row["turn_id"],
            state=json.loads(row["runtime_state_json"]),
        )

    def clear_continuation(self, turn_id: str, state: dict[str, Any]) -> None:
        self.update_turn_state(turn_id, state, pending_continuation_id=None)

    def get_pending_confirmation(self, session_ref: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT confirmation_id, effect_summary, action_payload_json, status
                FROM pending_confirmations
                WHERE session_ref = ? AND status = 'pending'
                """,
                (session_ref,),
            ).fetchone()
        if row is None:
            return None
        return {
            "confirmation_id": row["confirmation_id"],
            "effect_summary": row["effect_summary"],
            "action_payload": json.loads(row["action_payload_json"]),
            "status": row["status"],
        }

    def put_pending_confirmation(self, session_ref: str, confirmation_id: str, effect_summary: str, action_payload: dict[str, Any]) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO pending_confirmations(
                    session_ref, confirmation_id, effect_summary, action_payload_json, status, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(session_ref) DO UPDATE SET
                    confirmation_id = excluded.confirmation_id,
                    effect_summary = excluded.effect_summary,
                    action_payload_json = excluded.action_payload_json,
                    status = 'pending',
                    updated_at = excluded.updated_at
                """,
                (session_ref, confirmation_id, effect_summary, json.dumps(action_payload, ensure_ascii=False), now, now),
            )

    def clear_pending_confirmation(self, session_ref: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE pending_confirmations
                SET status = 'resolved', updated_at = ?
                WHERE session_ref = ? AND status = 'pending'
                """,
                (_utc_now(), session_ref),
            )

    def add_memory(self, session_ref: str, kind: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO memories(session_ref, kind, content, metadata_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (session_ref, kind, content, json.dumps(metadata or {}, ensure_ascii=False), _utc_now()),
            )

    def list_memories(self, session_ref: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT kind, content, metadata_json, created_at
                FROM memories
                WHERE session_ref = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_ref, limit),
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
