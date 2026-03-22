from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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


@dataclass(slots=True)
class ContinuationState:
    continuation_id: str
    turn_id: str
    state: dict[str, Any]


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

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    session_ref TEXT NOT NULL,
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
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    task_id TEXT,
                    artifact_refs_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_ref TEXT NOT NULL UNIQUE,
                    summary TEXT NOT NULL,
                    transcript_path TEXT,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(connection, "turns", "task_id", "TEXT")
            self._ensure_column(connection, "execution_records", "task_id", "TEXT")
            self._ensure_column(connection, "execution_records", "state_fingerprint", "TEXT")
            self._ensure_column(connection, "execution_records", "artifact_refs_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "pending_confirmations", "task_id", "TEXT")
            self._ensure_column(connection, "tasks", "parent_task_id", "TEXT")
            self._ensure_column(connection, "tasks", "origin_turn_id", "TEXT")
            self._ensure_column(connection, "tasks", "continuity_score", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tasks", "last_active_at", "TEXT")

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def create_turn(
        self,
        turn_id: str,
        session_ref: str,
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
                    turn_id, session_ref, task_id, user_message, status, runtime_state_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (turn_id, session_ref, task_id, user_message, json.dumps(runtime_state, ensure_ascii=False), now, now),
            )
        self.append_transcript_event(
            session_ref,
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
        pending_continuation_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE turns
                SET runtime_state_json = ?, pending_continuation_id = ?, task_id = COALESCE(?, task_id), updated_at = ?
                WHERE turn_id = ?
                """,
                (json.dumps(runtime_state, ensure_ascii=False), pending_continuation_id, task_id, _utc_now(), turn_id),
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
                SET status = ?, final_reply = ?, pending_continuation_id = NULL, updated_at = ?
                WHERE turn_id = ?
                """,
                (status, final_reply, _utc_now(), turn_id),
            )
            row = connection.execute(
                "SELECT session_ref, task_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is not None:
            self.append_transcript_event(
                row["session_ref"],
                {
                    "ts": _utc_now(),
                    "turn_id": turn_id,
                    "task_id": row["task_id"],
                    "role": "assistant",
                    "status": status,
                    "content": final_reply,
                },
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
                "SELECT session_ref, task_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is not None:
            self.append_session_event(
                row["session_ref"],
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

    def list_turns(self, session_ref: str, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT turn_id, task_id, user_message, final_reply, status, created_at
                    FROM turns
                    WHERE session_ref = ?
                    ORDER BY id ASC
                    """,
                    (session_ref,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT turn_id, task_id, user_message, final_reply, status, created_at
                    FROM turns
                    WHERE session_ref = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_ref, limit),
                ).fetchall()
                rows = rows[::-1]
        return [dict(row) for row in rows]

    def list_recent_turns(self, session_ref: str, limit: int = 12) -> list[dict[str, Any]]:
        return self.list_turns(session_ref, limit=limit)

    def put_continuation(self, continuation_id: str, turn_id: str, state: dict[str, Any], *, task_id: str | None = None) -> None:
        state = dict(state)
        state["continuation_id"] = continuation_id
        self.update_turn_state(turn_id, state, pending_continuation_id=continuation_id, task_id=task_id)

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
        state = json.loads(row["runtime_state_json"])
        state.pop("continuation_id", None)
        return ContinuationState(
            continuation_id=continuation_id,
            turn_id=row["turn_id"],
            state=state,
        )

    def clear_continuation(self, turn_id: str, state: dict[str, Any], *, task_id: str | None = None) -> None:
        self.update_turn_state(turn_id, state, pending_continuation_id=None, task_id=task_id)

    def get_pending_confirmation(self, session_ref: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT confirmation_id, effect_summary, action_payload_json, status, task_id
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
            "task_id": row["task_id"],
        }

    def put_pending_confirmation(
        self,
        session_ref: str,
        confirmation_id: str,
        effect_summary: str,
        action_payload: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO pending_confirmations(
                    session_ref, confirmation_id, effect_summary, action_payload_json, task_id, status, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(session_ref) DO UPDATE SET
                    confirmation_id = excluded.confirmation_id,
                    effect_summary = excluded.effect_summary,
                    action_payload_json = excluded.action_payload_json,
                    task_id = excluded.task_id,
                    status = 'pending',
                    updated_at = excluded.updated_at
                """,
                (session_ref, confirmation_id, effect_summary, json.dumps(action_payload, ensure_ascii=False), task_id, now, now),
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

    def add_semantic_memory(
        self,
        session_ref: str,
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
                WHERE session_ref = ? AND memory_type = ? AND memory_key = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (session_ref, memory_type, memory_key),
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
                    memory_id, session_ref, memory_type, memory_key, value, summary, confidence, metadata_json, updated_at, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    session_ref,
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

    def add_episodic_memory(
        self,
        session_ref: str,
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
            "session_ref": session_ref,
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
                    memory_id, session_ref, summary, tags_json, task_id, artifact_refs_json, metadata_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    session_ref,
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

    def list_semantic_memories(self, session_ref: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, memory_type, memory_key, value, summary, confidence, metadata_json, updated_at, created_at
                FROM semantic_memories
                WHERE session_ref = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (session_ref, limit),
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

    def list_episodic_memories(self, session_ref: str, limit: int = 6) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, summary, tags_json, task_id, artifact_refs_json, metadata_json, created_at
                FROM episodic_memories
                WHERE session_ref = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_ref, limit),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "kind": "episodic",
                "summary": row["summary"],
                "tags": json.loads(row["tags_json"]),
                "task_id": row["task_id"],
                "artifact_refs": json.loads(row["artifact_refs_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search_memories(self, session_ref: str, query: str, limit: int = 6) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for memory in self.list_semantic_memories(session_ref, limit=24):
            score = _score_text(query, memory["summary"], memory["value"], memory["memory_key"])
            if score > 0:
                scored.append((score, memory))
        for memory in self.list_episodic_memories(session_ref, limit=24):
            score = _score_text(query, memory["summary"], " ".join(memory["tags"]))
            if score > 0:
                scored.append((score, memory))
        for memory in self.list_memories(session_ref, limit=24):
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
            for memory in self.list_episodic_memories(session_ref, limit=max(limit, 6)):
                if _has_recent_dialogue_signal(memory):
                    scored.append((0.05, memory))
        results: list[dict[str, Any]] = []
        for score, payload in scored[:limit]:
            entry = dict(payload)
            entry["score"] = round(score, 4)
            results.append(entry)
        return results

    def create_task(
        self,
        session_ref: str,
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
            "session_ref": session_ref,
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
                    task_id, session_ref, task_type, owner_player, goal, status, priority, risk_class,
                    requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                    constraints_json, artifacts_json, summary_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    session_ref,
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
                SELECT task_id, session_ref, task_type, owner_player, goal, status, priority, risk_class,
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

    def get_active_task(self, session_ref: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, session_ref, task_type, owner_player, goal, status, priority, risk_class,
                       requires_confirmation, parent_task_id, origin_turn_id, continuity_score, last_active_at,
                       constraints_json, artifacts_json, summary_json, created_at, updated_at
                FROM tasks
                WHERE session_ref = ? AND status IN ('pending', 'analyzing', 'planned', 'awaiting_confirmation', 'in_progress', 'blocked')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_ref,),
            ).fetchone()
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

    def write_artifact(
        self,
        session_ref: str,
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
            target_dir = self._data_dir / "sessions" / _safe_segment(session_ref) / "observations"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{artifact_id}{extension}"
        if "json" in content_type:
            target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target_path.write_text(str(payload), encoding="utf-8")
        char_count = len(target_path.read_text(encoding="utf-8"))
        record = {
            "artifact_id": artifact_id,
            "session_ref": session_ref,
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
                    artifact_id, session_ref, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    session_ref,
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
                SELECT artifact_id, session_ref, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
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
            "session_ref": row["session_ref"],
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

    def list_artifacts(self, session_ref: str, *, task_id: str | None = None, limit: int = 24) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if task_id is None:
                rows = connection.execute(
                    """
                    SELECT artifact_id, session_ref, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                    FROM artifacts
                    WHERE session_ref = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_ref, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT artifact_id, session_ref, task_id, turn_id, kind, artifact_path, summary, content_type, char_count, metadata_json, created_at
                    FROM artifacts
                    WHERE session_ref = ? AND task_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_ref, task_id, limit),
                ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            records.append(
                {
                    "artifact_id": row["artifact_id"],
                    "session_ref": row["session_ref"],
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

    def search_artifacts(
        self,
        session_ref: str,
        query: str,
        *,
        task_id: str | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for artifact in self.list_artifacts(session_ref, task_id=task_id, limit=48):
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

    def upsert_session_summary(
        self,
        session_ref: str,
        summary: str,
        *,
        transcript_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO session_summaries(session_ref, summary, transcript_path, metadata_json, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_ref) DO UPDATE SET
                    summary = excluded.summary,
                    transcript_path = excluded.transcript_path,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (session_ref, summary, transcript_path, json.dumps(metadata or {}, ensure_ascii=False), now),
            )

    def get_session_summary(self, session_ref: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT session_ref, summary, transcript_path, metadata_json, updated_at
                FROM session_summaries
                WHERE session_ref = ?
                """,
                (session_ref,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_ref": row["session_ref"],
            "summary": row["summary"],
            "transcript_path": row["transcript_path"],
            "metadata": json.loads(row["metadata_json"]),
            "updated_at": row["updated_at"],
        }

    def write_compact_summary(
        self,
        session_ref: str,
        summary: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transcript_path = self.session_dir(session_ref) / "transcript.jsonl"
        self.upsert_session_summary(
            session_ref,
            summary,
            transcript_path=str(transcript_path),
            metadata=metadata,
        )
        target = self.session_dir(session_ref) / "compact_summary.md"
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

    def session_dir(self, session_ref: str) -> Path:
        target = self._data_dir / "sessions" / _safe_segment(session_ref)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def append_transcript_event(self, session_ref: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.session_dir(session_ref) / "transcript.jsonl", payload)

    def append_session_event(self, session_ref: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.session_dir(session_ref) / "events.jsonl", payload)

    def append_episode_log(self, payload: dict[str, Any]) -> None:
        target = self._data_dir / "memory" / "episodes.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(target, payload)

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _task_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "session_ref": row["session_ref"],
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
            "artifacts": json.loads(row["artifacts_json"]),
            "summary": json.loads(row["summary_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
