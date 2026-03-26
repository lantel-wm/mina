from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .utils import (
    append_jsonl_atomic,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    load_json,
)


@dataclass(slots=True)
class AllPagesCheckpoint:
    apcontinue: str | None = None
    enumerated_count: int = 0
    enumeration_complete: bool = False
    last_success_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AllPagesCheckpoint":
        if not data:
            return cls()
        return cls(
            apcontinue=data.get("apcontinue"),
            enumerated_count=int(data.get("enumerated_count", 0)),
            enumeration_complete=bool(data.get("enumeration_complete", False)),
            last_success_time=data.get("last_success_time"),
        )


class CheckpointStore:
    def __init__(self, checkpoints_dir: str | Path) -> None:
        self.root = ensure_dir(checkpoints_dir)
        self.allpages_path = self.root / "allpages.json"
        self.discovered_titles_path = self.root / "discovered_titles.jsonl"
        self.failed_pages_path = self.root / "failed_pages.jsonl"

    def load_allpages(self) -> AllPagesCheckpoint:
        return AllPagesCheckpoint.from_dict(load_json(self.allpages_path, default={}))

    def save_allpages(self, checkpoint: AllPagesCheckpoint) -> None:
        atomic_write_json(self.allpages_path, checkpoint.to_dict())

    def reset_discovered_titles(self) -> None:
        atomic_write_text(self.discovered_titles_path, "")

    def reset_failed_pages(self) -> None:
        atomic_write_text(self.failed_pages_path, "")

    def append_discovered_title(self, page_id: int, ns: int, title: str) -> None:
        append_jsonl_atomic(
            self.discovered_titles_path,
            {"page_id": page_id, "ns": ns, "title": title},
        )

    def append_failed_page(self, payload: dict[str, Any]) -> None:
        append_jsonl_atomic(self.failed_pages_path, payload)

    def iter_discovered_titles(self) -> list[dict[str, Any]]:
        if not self.discovered_titles_path.exists():
            return []
        return [
            json.loads(item)
            for item in self.discovered_titles_path.read_text(encoding="utf-8").splitlines()
            if item.strip()
        ]

    def iter_failed_pages(self) -> list[dict[str, Any]]:
        if not self.failed_pages_path.exists():
            return []
        return [
            json.loads(item)
            for item in self.failed_pages_path.read_text(encoding="utf-8").splitlines()
            if item.strip()
        ]
