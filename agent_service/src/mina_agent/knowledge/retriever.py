from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mina_agent.knowledge.store import KnowledgeStore


class SemanticRetriever(ABC):
    @abstractmethod
    def search(
        self,
        *,
        minecraft_version: str,
        query: str,
        limit: int,
        source_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class SQLiteFtsSemanticRetriever(SemanticRetriever):
    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    def search(
        self,
        *,
        minecraft_version: str,
        query: str,
        limit: int,
        source_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.search_semantic_chunks(
            minecraft_version=minecraft_version,
            query=query,
            limit=limit,
            source_kinds=source_kinds,
        )
