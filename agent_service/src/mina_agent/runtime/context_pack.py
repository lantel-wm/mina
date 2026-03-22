from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from mina_agent.runtime.models import ArtifactRef


ContextRole = Literal["system", "user"]


@dataclass(slots=True)
class TrimPolicy:
    priority_order: tuple[str, ...]
    hard_floor_chars: int = 320


@dataclass(slots=True)
class ContextSlot:
    name: str
    role: ContextRole
    source: str
    strategy: str
    content: Any
    priority: int
    recoverable: bool = False
    included: bool = True
    truncated: bool = False
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    @property
    def full_chars(self) -> int:
        serialized = self.content if isinstance(self.content, str) else json.dumps(self.content, ensure_ascii=False, default=str)
        return len(serialized)

    def summary_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "source": self.source,
            "strategy": self.strategy,
            "priority": self.priority,
            "recoverable": self.recoverable,
            "included": self.included,
            "full_chars": self.full_chars,
            "truncated": self.truncated or _contains_truncation_marker(self.content),
            "preview": self.content,
            "artifact_refs": [artifact.context_ref() for artifact in self.artifact_refs],
        }


@dataclass(slots=True)
class ContextPack:
    slots: list[ContextSlot]
    trim_policy: TrimPolicy

    def active_slots(self) -> list[ContextSlot]:
        return [slot for slot in self.slots if slot.included]

    def total_chars(self) -> int:
        return sum(slot.full_chars for slot in self.active_slots())


def _contains_truncation_marker(content: Any) -> bool:
    if isinstance(content, dict):
        if content.get("truncated") is True:
            return True
        return any(_contains_truncation_marker(value) for value in content.values())
    if isinstance(content, list):
        return any(_contains_truncation_marker(item) for item in content)
    return False
