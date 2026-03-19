from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, audit_dir: Path) -> None:
        self._audit_dir = audit_dir
        self._audit_dir.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        stamp = datetime.now(timezone.utc)
        line = {
            "ts": stamp.isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        target = self._audit_dir / f"{stamp:%Y-%m-%d}.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
