from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from mina_agent.memory.store import Store


WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")


class LocalKnowledgeIndex:
    def __init__(self, store: Store, knowledge_dir: Path) -> None:
        self._store = store
        self._knowledge_dir = knowledge_dir
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)

    def refresh(self) -> None:
        for path in sorted(self._knowledge_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".json"}:
                continue
            content = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunks = self._chunk_document(path, content)
            self._store.replace_document(str(path), path.stem, checksum, chunks)

    def search(self, query: str, limit: int = 4) -> list[dict[str, Any]]:
        query_terms = set(_terms(query))
        if not query_terms:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self._store.list_document_chunks():
            terms = set(_terms(chunk["content"]))
            overlap = query_terms & terms
            if not overlap:
                continue
            score = len(overlap) / max(len(query_terms), 1)
            scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[dict[str, Any]] = []
        for score, chunk in scored[:limit]:
            results.append(
                {
                    "doc_path": chunk["doc_path"],
                    "title": chunk["title"],
                    "chunk_index": chunk["chunk_index"],
                    "content": chunk["content"],
                    "score": round(score, 4),
                    "metadata": chunk["metadata"],
                }
            )
        return results

    def _chunk_document(self, path: Path, content: str) -> list[dict[str, Any]]:
        parts = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
        if not parts:
            parts = [content.strip()]

        chunks: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            chunks.append(
                {
                    "chunk_index": index,
                    "content": part,
                    "token_count": len(_terms(part)),
                    "metadata": {"source_path": str(path)},
                }
            )
        return chunks


def _terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD_RE.finditer(text)]
