from __future__ import annotations

from typing import Any


def strip_memory_citations(text: str) -> tuple[str, list[str]]:
    visible_parts: list[str] = []
    citations: list[str] = []
    cursor = 0
    open_tag = "<oai-mem-citation>"
    close_tag = "</oai-mem-citation>"

    while True:
        start = text.find(open_tag, cursor)
        if start < 0:
            visible_parts.append(text[cursor:])
            break
        visible_parts.append(text[cursor:start])
        body_start = start + len(open_tag)
        end = text.find(close_tag, body_start)
        if end < 0:
            citations.append(text[start:] + close_tag)
            cursor = len(text)
            break
        citations.append(text[start : end + len(close_tag)])
        cursor = end + len(close_tag)

    visible = "".join(visible_parts)
    visible = _cleanup_visible_text(visible)
    return visible, citations


def parse_memory_citation(citations: list[str]) -> dict[str, Any] | None:
    entries: list[dict[str, Any]] = []
    thread_ids: list[str] = []
    seen_thread_ids: set[str] = set()

    for citation in citations:
        entries_block = _extract_block(citation, "<citation_entries>", "</citation_entries>")
        if entries_block is not None:
            for line in entries_block.splitlines():
                parsed = _parse_memory_citation_entry(line)
                if parsed is not None:
                    entries.append(parsed)
        ids_block = _extract_ids_block(citation)
        if ids_block is None:
            continue
        for raw_line in ids_block.splitlines():
            line = raw_line.strip()
            if not line or line in seen_thread_ids:
                continue
            seen_thread_ids.add(line)
            thread_ids.append(line)

    if not entries and not thread_ids:
        return None
    return {
        "entries": entries,
        "thread_ids": thread_ids,
    }


def get_thread_ids_from_citations(citations: list[str]) -> list[str]:
    parsed = parse_memory_citation(citations)
    if parsed is None:
        return []
    return [str(thread_id) for thread_id in parsed.get("thread_ids", []) if str(thread_id).strip()]


def _parse_memory_citation_entry(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    location, separator, note = stripped.rpartition("|note=[")
    if not separator or not note.endswith("]"):
        return None
    note = note[:-1].strip()
    path, _, line_range = location.rpartition(":")
    if not path or "-" not in line_range:
        return None
    line_start_text, _, line_end_text = line_range.partition("-")
    try:
        line_start = int(line_start_text.strip())
        line_end = int(line_end_text.strip())
    except ValueError:
        return None
    return {
        "path": path.strip(),
        "line_start": line_start,
        "line_end": line_end,
        "note": note,
    }


def _extract_block(text: str, open_tag: str, close_tag: str) -> str | None:
    start = text.find(open_tag)
    if start < 0:
        return None
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end < 0:
        return None
    return text[start:end]


def _extract_ids_block(text: str) -> str | None:
    return _extract_block(text, "<thread_ids>", "</thread_ids>") or _extract_block(
        text,
        "<rollout_ids>",
        "</rollout_ids>",
    )


def _cleanup_visible_text(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
