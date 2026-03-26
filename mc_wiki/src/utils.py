from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .models import AppConfig, CrawlConfig, ParseConfig, StorageConfig, WikiConfig


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def atomic_write_text(path: str | Path, text: str) -> None:
    destination = Path(path)
    ensure_dir(destination.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, destination)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_json(path: str | Path, default: Any | None = None) -> Any:
    source = Path(path)
    if not source.exists():
        return deepcopy(default)
    return json.loads(source.read_text(encoding="utf-8"))


def append_jsonl_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    ensure_dir(destination.parent)
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    with source.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def default_config_dict() -> dict[str, Any]:
    return AppConfig().to_dict()


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or "config.yaml")
    payload: dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to read config.yaml. Install project dependencies first."
            ) from exc
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must deserialize to a mapping: {config_path}")
        payload = loaded
    merged = _deep_merge(default_config_dict(), payload)
    return AppConfig(
        wiki=WikiConfig(**merged["wiki"]),
        crawl=CrawlConfig(**merged["crawl"]),
        parse=ParseConfig(**merged["parse"]),
        storage=StorageConfig(**merged["storage"]),
    )


def normalize_title(title: str) -> str:
    return " ".join(title.replace("_", " ").split())


def make_source_url(base_url: str, title: str) -> str:
    safe_title = quote(normalize_title(title).replace(" ", "_"), safe=":/")
    return f"{base_url.rstrip('/')}/wiki/{safe_title}"


def list_json_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.suffix == ".json")
