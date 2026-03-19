from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DATASET_TABLES: dict[str, tuple[str, ...]] = {
    "recipe": (
        "minecraft_version",
        "fact_id",
        "namespace",
        "path",
        "recipe_type",
        "result_item",
        "result_count",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
    "loot_table": (
        "minecraft_version",
        "fact_id",
        "namespace",
        "path",
        "loot_type",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
    "tag": (
        "minecraft_version",
        "fact_id",
        "namespace",
        "path",
        "tag_group",
        "entry_count",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
    "command": (
        "minecraft_version",
        "fact_id",
        "command_path",
        "argument_count",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
    "registry_entry": (
        "minecraft_version",
        "fact_id",
        "registry_key",
        "entry_key",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
    "block_state": (
        "minecraft_version",
        "fact_id",
        "block_id",
        "state_count",
        "payload_json",
        "source_path",
        "checksum",
        "imported_at",
    ),
}

SEMANTIC_PRIORITY_ORDER = {
    "local_rule_text": 100,
    "local_note": 90,
    "changelog": 80,
    "wiki": 50,
}

WORD_RE = re.compile(r"[^\s]+", re.UNICODE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeStore:
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
                CREATE TABLE IF NOT EXISTS import_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT NOT NULL,
                    minecraft_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_path TEXT,
                    checksum TEXT,
                    metadata_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recipe_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    path TEXT NOT NULL,
                    recipe_type TEXT NOT NULL,
                    result_item TEXT,
                    result_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS loot_table_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    path TEXT NOT NULL,
                    loot_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS tag_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    path TEXT NOT NULL,
                    tag_group TEXT NOT NULL,
                    entry_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS command_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    command_path TEXT NOT NULL,
                    argument_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS registry_entry_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    registry_key TEXT NOT NULL,
                    entry_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS block_state_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    state_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, fact_id)
                );

                CREATE TABLE IF NOT EXISTS local_rule_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    minecraft_version TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    applies_to_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(minecraft_version, rule_id)
                );

                CREATE TABLE IF NOT EXISTS fact_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset TEXT NOT NULL,
                    minecraft_version TEXT NOT NULL,
                    source_category TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    fact_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(dataset, minecraft_version, source_category, fact_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS fact_index_fts USING fts5(
                    fact_id,
                    title,
                    search_text,
                    tokenize = 'unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS semantic_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    minecraft_version TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semantic_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    verification_required INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS semantic_chunks_fts USING fts5(
                    content,
                    title,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                """
            )

    @property
    def db_path(self) -> Path:
        return self._db_path

    def record_import_run(
        self,
        *,
        source_kind: str,
        minecraft_version: str,
        status: str,
        source_path: str | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO import_runs(
                    source_kind, minecraft_version, status, source_path, checksum, metadata_json, imported_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_kind,
                    minecraft_version,
                    status,
                    source_path,
                    checksum,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def replace_dataset_facts(self, dataset: str, minecraft_version: str, rows: list[dict[str, Any]]) -> None:
        table = f"{dataset}_facts"
        columns = DATASET_TABLES[dataset]
        with self.connection() as connection:
            connection.execute(f"DELETE FROM {table} WHERE minecraft_version = ?", (minecraft_version,))
            existing_ids = connection.execute(
                """
                SELECT id
                FROM fact_index
                WHERE dataset = ? AND minecraft_version = ? AND source_category = 'official_structured_fact'
                """,
                (dataset, minecraft_version),
            ).fetchall()
            for row in existing_ids:
                connection.execute("DELETE FROM fact_index_fts WHERE rowid = ?", (row["id"],))
            connection.execute(
                """
                DELETE FROM fact_index
                WHERE dataset = ? AND minecraft_version = ? AND source_category = 'official_structured_fact'
                """,
                (dataset, minecraft_version),
            )

            now = _utc_now()
            placeholders = ", ".join("?" for _ in columns)
            column_list = ", ".join(columns)
            for row in rows:
                payload = [row.get(column) if column != "imported_at" else now for column in columns]
                connection.execute(
                    f"INSERT INTO {table}({column_list}) VALUES({placeholders})",
                    payload,
                )
                cursor = connection.execute(
                    """
                    INSERT INTO fact_index(
                        dataset, minecraft_version, source_category, priority, fact_id, title, search_text,
                        payload_json, metadata_json, source_path, checksum, imported_at
                    )
                    VALUES(?, ?, 'official_structured_fact', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset,
                        minecraft_version,
                        int(row.get("priority", 50)),
                        row["fact_id"],
                        row.get("title", row["fact_id"]),
                        row.get("search_text", row["fact_id"]),
                        row["payload_json"],
                        json.dumps(row.get("metadata", {}), ensure_ascii=False),
                        row["source_path"],
                        row["checksum"],
                        now,
                    ),
                )
                fact_index_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO fact_index_fts(rowid, fact_id, title, search_text)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        fact_index_id,
                        row["fact_id"],
                        row.get("title", row["fact_id"]),
                        row.get("search_text", row["fact_id"]),
                    ),
                )

    def replace_local_rules(self, minecraft_version: str, rows: list[dict[str, Any]], *, source_path: str, checksum: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM local_rule_facts WHERE minecraft_version = ?", (minecraft_version,))
            connection.execute(
                """
                DELETE FROM fact_index_fts
                WHERE rowid IN (
                    SELECT id FROM fact_index
                    WHERE dataset = 'local_rule' AND minecraft_version = ? AND source_category = 'local_rule'
                )
                """,
                (minecraft_version,),
            )
            connection.execute(
                """
                DELETE FROM fact_index
                WHERE dataset = 'local_rule' AND minecraft_version = ? AND source_category = 'local_rule'
                """,
                (minecraft_version,),
            )

            now = _utc_now()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO local_rule_facts(
                        minecraft_version, rule_id, priority, scope, effect, applies_to_json,
                        payload_json, source_path, checksum, imported_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        minecraft_version,
                        row["rule_id"],
                        int(row["priority"]),
                        row["scope"],
                        row["effect"],
                        json.dumps(row.get("applies_to", {}), ensure_ascii=False),
                        row["payload_json"],
                        source_path,
                        checksum,
                        now,
                    ),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO fact_index(
                        dataset, minecraft_version, source_category, priority, fact_id, title, search_text,
                        payload_json, metadata_json, source_path, checksum, imported_at
                    )
                    VALUES('local_rule', ?, 'local_rule', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        minecraft_version,
                        int(row["priority"]),
                        row["rule_id"],
                        row.get("title", row["rule_id"]),
                        row.get("search_text", row["rule_id"]),
                        row["payload_json"],
                        json.dumps(row.get("metadata", {}), ensure_ascii=False),
                        source_path,
                        checksum,
                        now,
                    ),
                )
                fact_index_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO fact_index_fts(rowid, fact_id, title, search_text)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        fact_index_id,
                        row["rule_id"],
                        row.get("title", row["rule_id"]),
                        row.get("search_text", row["rule_id"]),
                    ),
                )

    def replace_semantic_document(
        self,
        *,
        doc_path: str,
        title: str,
        source_kind: str,
        minecraft_version: str,
        priority: int,
        checksum: str,
        metadata: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> bool:
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT id, checksum FROM semantic_documents WHERE doc_path = ?",
                (doc_path,),
            ).fetchone()
            if existing is not None and existing["checksum"] == checksum:
                return False

            if existing is not None:
                chunk_ids = connection.execute(
                    "SELECT id FROM semantic_chunks WHERE doc_id = ?",
                    (existing["id"],),
                ).fetchall()
                for row in chunk_ids:
                    connection.execute("DELETE FROM semantic_chunks_fts WHERE rowid = ?", (row["id"],))
                connection.execute("DELETE FROM semantic_chunks WHERE doc_id = ?", (existing["id"],))
                connection.execute("DELETE FROM semantic_documents WHERE id = ?", (existing["id"],))

            cursor = connection.execute(
                """
                INSERT INTO semantic_documents(
                    doc_path, title, source_kind, minecraft_version, priority, checksum, metadata_json, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_path,
                    title,
                    source_kind,
                    minecraft_version,
                    priority,
                    checksum,
                    json.dumps(metadata, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            doc_id = int(cursor.lastrowid)
            for chunk in chunks:
                chunk_cursor = connection.execute(
                    """
                    INSERT INTO semantic_chunks(
                        doc_id, chunk_index, content, token_count, verification_required, metadata_json
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        chunk["chunk_index"],
                        chunk["content"],
                        int(chunk["token_count"]),
                        1 if chunk.get("verification_required", False) else 0,
                        json.dumps(chunk.get("metadata", {}), ensure_ascii=False),
                    ),
                )
                chunk_id = int(chunk_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO semantic_chunks_fts(rowid, content, title)
                    VALUES(?, ?, ?)
                    """,
                    (
                        chunk_id,
                        chunk["content"],
                        title,
                    ),
                )
            return True

    def search_facts(
        self,
        *,
        minecraft_version: str,
        query: str,
        limit: int,
        datasets: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized = query.strip().lower()
        if not normalized:
            return []

        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        dataset_clause = ""
        params: list[Any] = [minecraft_version]
        if datasets:
            placeholders = ", ".join("?" for _ in datasets)
            dataset_clause = f" AND dataset IN ({placeholders})"
            params.extend(datasets)

        with self.connection() as connection:
            exact_rows = connection.execute(
                f"""
                SELECT *,
                    CASE
                        WHEN lower(fact_id) = ? THEN 0
                        WHEN lower(title) = ? THEN 1
                        WHEN lower(fact_id) LIKE ? THEN 2
                        WHEN lower(search_text) LIKE ? THEN 3
                        ELSE 4
                    END AS match_rank
                FROM fact_index
                WHERE minecraft_version = ?
                    {dataset_clause}
                    AND (
                        lower(fact_id) = ?
                        OR lower(title) = ?
                        OR lower(fact_id) LIKE ?
                        OR lower(search_text) LIKE ?
                    )
                ORDER BY match_rank ASC, {self._source_rank_sql()}, priority DESC, fact_id ASC
                LIMIT ?
                """,
                [
                    normalized,
                    normalized,
                    f"%{normalized}%",
                    f"%{normalized}%",
                    *params,
                    normalized,
                    normalized,
                    f"%{normalized}%",
                    f"%{normalized}%",
                    limit,
                ],
            ).fetchall()

            for row in exact_rows:
                key = (row["dataset"], row["source_category"], row["fact_id"])
                seen.add(key)
                results.append(self._fact_row_to_payload(row, match_type="exact"))
            if len(results) >= limit:
                return results[:limit]

            fts_query = _fts_query(query)
            if not fts_query:
                return results

            fts_rows = connection.execute(
                f"""
                SELECT fi.*, bm25(fact_index_fts) AS score
                FROM fact_index_fts
                JOIN fact_index fi ON fi.id = fact_index_fts.rowid
                WHERE fact_index_fts MATCH ?
                    AND fi.minecraft_version = ?
                    {dataset_clause}
                ORDER BY {self._source_rank_sql('fi')}, fi.priority DESC, score ASC, fi.fact_id ASC
                LIMIT ?
                """,
                [fts_query, minecraft_version, *(datasets or []), max(limit * 2, limit)],
            ).fetchall()

        for row in fts_rows:
            key = (row["dataset"], row["source_category"], row["fact_id"])
            if key in seen:
                continue
            seen.add(key)
            results.append(self._fact_row_to_payload(row, match_type="fts"))
            if len(results) >= limit:
                break
        return results

    def search_semantic_chunks(
        self,
        *,
        minecraft_version: str,
        query: str,
        limit: int,
        source_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        fts_query = _fts_query(query)
        if not fts_query:
            return []

        source_clause = ""
        params: list[Any] = [fts_query, minecraft_version, minecraft_version]
        if source_kinds:
            placeholders = ", ".join("?" for _ in source_kinds)
            source_clause = f" AND d.source_kind IN ({placeholders})"
            params.extend(source_kinds)
        params.append(limit)

        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    c.id,
                    c.chunk_index,
                    c.content,
                    c.token_count,
                    c.verification_required,
                    c.metadata_json,
                    d.doc_path,
                    d.title,
                    d.source_kind,
                    d.minecraft_version,
                    d.priority,
                    d.metadata_json AS doc_metadata_json,
                    bm25(semantic_chunks_fts) AS score
                FROM semantic_chunks_fts
                JOIN semantic_chunks c ON c.id = semantic_chunks_fts.rowid
                JOIN semantic_documents d ON d.id = c.doc_id
                WHERE semantic_chunks_fts MATCH ?
                    AND (d.minecraft_version = ? OR d.minecraft_version = '')
                    AND d.minecraft_version IN (?, '')
                    {source_clause}
                ORDER BY d.priority DESC, score ASC, d.title ASC, c.chunk_index ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._semantic_row_to_payload(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.connection() as connection:
            latest_import = connection.execute(
                """
                SELECT source_kind, minecraft_version, status, imported_at, metadata_json
                FROM import_runs
                WHERE status = 'completed'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            semantic_doc_count = int(connection.execute("SELECT COUNT(*) FROM semantic_documents").fetchone()[0])
            semantic_chunk_count = int(connection.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0])
            fact_counts = connection.execute(
                """
                SELECT dataset, COUNT(*) AS count
                FROM fact_index
                GROUP BY dataset
                ORDER BY dataset
                """
            ).fetchall()
        return {
            "db_path": str(self._db_path),
            "semantic_document_count": semantic_doc_count,
            "semantic_chunk_count": semantic_chunk_count,
            "fact_counts": {row["dataset"]: row["count"] for row in fact_counts},
            "last_successful_import": None
            if latest_import is None
            else {
                "source_kind": latest_import["source_kind"],
                "minecraft_version": latest_import["minecraft_version"],
                "status": latest_import["status"],
                "imported_at": latest_import["imported_at"],
                "metadata": json.loads(latest_import["metadata_json"]),
            },
        }

    def _fact_row_to_payload(self, row: sqlite3.Row, *, match_type: str) -> dict[str, Any]:
        metadata = json.loads(row["metadata_json"])
        return {
            "dataset": row["dataset"],
            "minecraft_version": row["minecraft_version"],
            "source_category": row["source_category"],
            "source_label": _source_label(row["source_category"]),
            "priority": row["priority"],
            "fact_id": row["fact_id"],
            "title": row["title"],
            "search_text": row["search_text"],
            "payload": json.loads(row["payload_json"]),
            "metadata": metadata,
            "source_path": row["source_path"],
            "match_type": match_type,
        }

    def _semantic_row_to_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        chunk_metadata = json.loads(row["metadata_json"])
        doc_metadata = json.loads(row["doc_metadata_json"])
        return {
            "doc_path": row["doc_path"],
            "title": row["title"],
            "source_kind": row["source_kind"],
            "source_label": _semantic_source_label(row["source_kind"]),
            "source_category": "semantic_text",
            "chunk_index": row["chunk_index"],
            "content": row["content"],
            "score": row["score"],
            "priority": row["priority"],
            "minecraft_version": row["minecraft_version"],
            "verification_required": bool(row["verification_required"]),
            "metadata": {
                **doc_metadata,
                **chunk_metadata,
            },
        }

    def _source_rank_sql(self, alias: str = "fact_index") -> str:
        return (
            f"CASE {alias}.source_category "
            "WHEN 'local_rule' THEN 0 "
            "WHEN 'official_structured_fact' THEN 1 "
            "ELSE 2 END"
        )


def _fts_query(query: str) -> str:
    tokens = [token.strip().replace('"', '""') for token in WORD_RE.findall(query) if token.strip()]
    if not tokens:
        return ""
    return " AND ".join(f'"{token}"' for token in tokens[:8])


def _source_label(source_category: str) -> str:
    return {
        "local_rule": "本地服务器规则",
        "official_structured_fact": "官方结构化事实",
    }.get(source_category, source_category)


def _semantic_source_label(source_kind: str) -> str:
    return {
        "local_rule_text": "本地规则说明",
        "local_note": "本地说明",
        "changelog": "官方版本说明",
        "wiki": "Wiki 解释文本",
    }.get(source_kind, source_kind)
