from __future__ import annotations

import math
import re
from dataclasses import dataclass


try:
    import tiktoken  # type: ignore
except Exception:  # pragma: no cover - exercised through fallback paths
    tiktoken = None


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_NON_SPACE_SYMBOL_RE = re.compile(r"[^\s]")


@dataclass(slots=True)
class PromptTokenEstimate:
    encoding_name: str
    model: str | None
    per_message_tokens: list[int]
    total_tokens: int


class PromptTokenEstimator:
    def __init__(self, model: str | None, encoding_override: str | None = None) -> None:
        self._model = model
        self._encoding_name = self._resolve_encoding_name(model, encoding_override)
        self._encoding = self._load_encoding(self._encoding_name)

    @property
    def encoding_name(self) -> str:
        return self._encoding_name

    def estimate_messages(self, messages: list[dict[str, str]]) -> PromptTokenEstimate:
        per_message_tokens: list[int] = []
        for message in messages:
            content = str(message.get("content") or "")
            role = str(message.get("role") or "")
            token_count = 4 + self._count_text_tokens(role) + self._count_text_tokens(content)
            per_message_tokens.append(token_count)
        total_tokens = sum(per_message_tokens) + 2
        return PromptTokenEstimate(
            encoding_name=self._encoding_name,
            model=self._model,
            per_message_tokens=per_message_tokens,
            total_tokens=total_tokens,
        )

    def _count_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))

        tokens = 0
        index = 0
        while index < len(text):
            ascii_match = _ASCII_WORD_RE.match(text, index)
            if ascii_match is not None:
                tokens += max(1, math.ceil((ascii_match.end() - ascii_match.start()) / 4))
                index = ascii_match.end()
                continue
            cjk_match = _CJK_RE.match(text, index)
            if cjk_match is not None:
                tokens += 1
                index = cjk_match.end()
                continue
            symbol_match = _NON_SPACE_SYMBOL_RE.match(text, index)
            if symbol_match is not None:
                if not text[index].isspace():
                    tokens += 1
                index = symbol_match.end()
                continue
            index += 1
        return tokens

    def _load_encoding(self, encoding_name: str):
        if tiktoken is None:
            return None
        try:
            return tiktoken.get_encoding(encoding_name)
        except Exception:
            return None

    def _resolve_encoding_name(self, model: str | None, encoding_override: str | None) -> str:
        if encoding_override:
            return encoding_override
        normalized = (model or "").strip().lower()
        if any(
            token in normalized
            for token in (
                "gpt-5",
                "gpt-4o",
                "o1",
                "o3",
                "o4",
                "omni",
            )
        ):
            return "o200k_base"
        return "cl100k_base"
