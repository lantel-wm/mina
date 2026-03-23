from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from mina_agent.config import Settings
from mina_agent.runtime.prompt_token_estimator import PromptTokenEstimator
from mina_agent.schemas import ModelDecision


CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
JSON_ROOT_START_CHARS = frozenset('{["-0123456789tfn')
TEMPERATURE = 0.2
ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(slots=True)
class ProviderDecisionResult:
    decision: ModelDecision
    latency_ms: int
    raw_response_preview: str
    parse_status: str
    model: str
    temperature: float
    message_count: int


@dataclass(slots=True)
class ProviderStructuredResult(Generic[ModelT]):
    payload: ModelT
    latency_ms: int
    raw_response_preview: str
    parse_status: str
    model: str
    temperature: float
    message_count: int


@dataclass(slots=True)
class ProviderValueResult:
    value: Any
    latency_ms: int
    raw_response_preview: str
    parse_status: str
    model: str
    temperature: float
    message_count: int


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        parse_status: str,
        raw_response_preview: str = "",
        latency_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.parse_status = parse_status
        self.raw_response_preview = raw_response_preview
        self.latency_ms = latency_ms


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_estimator = PromptTokenEstimator(
            settings.model,
            settings.context_tokenizer_encoding_override,
        )

    def available(self) -> bool:
        return bool(self._settings.base_url and self._settings.api_key and self._settings.model)

    def debug_request_buffer(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "kind": "openai_chat_completions_request",
            "content_type": "application/json",
            "extension": ".json",
            "body_text": self._render_request_body(messages),
        }

    def estimate_prompt_tokens(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        estimate = self._token_estimator.estimate_messages(messages)
        return {
            "model": self._settings.model or "",
            "encoding_name": estimate.encoding_name,
            "message_count": len(messages),
            "message_tokens": estimate.per_message_tokens,
            "total_tokens": estimate.total_tokens,
        }

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        result = self.complete_json(messages, ModelDecision)
        return ProviderDecisionResult(
            decision=result.payload,
            latency_ms=result.latency_ms,
            raw_response_preview=result.raw_response_preview,
            parse_status=result.parse_status,
            model=result.model,
            temperature=result.temperature,
            message_count=result.message_count,
        )

    def complete_json(self, messages: list[dict[str, str]], response_model: type[ModelT]) -> ProviderStructuredResult[ModelT]:
        content, latency_ms = self._request_content(messages)
        candidates = self._json_candidates(content, exhaustive=True)
        if not candidates:
            raise ProviderError(
                "Model returned a response without a JSON decision block.",
                parse_status="missing_decision_json",
                raw_response_preview=content,
                latency_ms=latency_ms,
            )
        last_candidate_text = content
        if response_model is ModelDecision:
            scored_candidates: list[tuple[int, int, str, ModelT]] = []
            for index, (candidate_text, candidate_value) in enumerate(candidates):
                last_candidate_text = candidate_text
                try:
                    payload = response_model.model_validate(candidate_value)
                except ValidationError:
                    continue
                score = self._model_decision_candidate_score(payload)
                if score <= 0:
                    continue
                scored_candidates.append((score, index, candidate_text, payload))

            if scored_candidates:
                _, _, _, payload = max(scored_candidates, key=lambda item: (item[0], item[1]))
                return ProviderStructuredResult(
                    payload=payload,
                    latency_ms=latency_ms,
                    raw_response_preview=content,
                    parse_status="ok",
                    model=self._settings.model or "",
                    temperature=TEMPERATURE,
                    message_count=len(messages),
                )
            raise ProviderError(
                f"Model returned a JSON block that does not match the {response_model.__name__} schema.",
                parse_status="invalid_decision_json",
                raw_response_preview=last_candidate_text,
                latency_ms=latency_ms,
            )
        for candidate_text, candidate_value in candidates:
            last_candidate_text = candidate_text
            try:
                payload = response_model.model_validate(candidate_value)
            except ValidationError:
                continue
            return ProviderStructuredResult(
                payload=payload,
                latency_ms=latency_ms,
                raw_response_preview=content,
                parse_status="ok",
                model=self._settings.model or "",
                temperature=TEMPERATURE,
                message_count=len(messages),
            )
        raise ProviderError(
            f"Model returned a JSON block that does not match the {response_model.__name__} schema.",
            parse_status="invalid_decision_json",
            raw_response_preview=last_candidate_text,
            latency_ms=latency_ms,
        )

    def _model_decision_candidate_score(self, decision: ModelDecision) -> int:
        score = 0
        if decision.intent is not None:
            score += 6
        if decision.mode is not None:
            score += 4
        if decision.final_reply:
            score += 6
        if decision.capability_request is not None and decision.capability_request.capability_id:
            score += 6
        if decision.delegate_request is not None and decision.delegate_request.objective:
            score += 6
        if decision.confirmation_request is not None and decision.confirmation_request.effect_summary:
            score += 3
        if decision.capability_id:
            score += 2
        if decision.delegate_role is not None:
            score += 2
        if decision.task_selection is not None:
            score += 1
        if decision.task_update:
            score += 1
        return score

    def complete_json_value(
        self,
        messages: list[dict[str, str]],
        *,
        expected_root_types: tuple[type[Any], ...] | None = None,
    ) -> ProviderValueResult:
        content, latency_ms = self._request_content(messages)
        candidates = self._json_candidates(content, exhaustive=False)
        if not candidates:
            candidates = self._json_candidates(content, exhaustive=True)
        if not candidates:
            raise ProviderError(
                "Model returned a response without a JSON value.",
                parse_status="missing_json_value",
                raw_response_preview=content,
                latency_ms=latency_ms,
            )

        last_candidate_text = content
        for candidate_text, candidate_value in candidates:
            last_candidate_text = candidate_text
            if self._looks_like_compaction_request_wrapper(candidate_value):
                continue
            if expected_root_types is not None and not isinstance(candidate_value, expected_root_types):
                continue
            return ProviderValueResult(
                value=candidate_value,
                latency_ms=latency_ms,
                raw_response_preview=content,
                parse_status="ok",
                model=self._settings.model or "",
                temperature=TEMPERATURE,
                message_count=len(messages),
            )

        raise ProviderError(
            "Model returned JSON, but it did not match the expected compacted value shape.",
            parse_status="invalid_json_value",
            raw_response_preview=last_candidate_text,
            latency_ms=latency_ms,
        )

    def _json_candidates(self, content: str, *, exhaustive: bool) -> list[tuple[str, Any]]:
        decoder = json.JSONDecoder()
        candidates: list[tuple[int, int, str, Any]] = []
        seen: set[tuple[int, int]] = set()

        for match in CODE_BLOCK_RE.finditer(content):
            block = match.group(1).strip()
            if not block:
                continue
            try:
                value, end = decoder.raw_decode(block)
            except json.JSONDecodeError:
                continue
            if block[end:].strip():
                continue
            key = (match.start(1), match.start(1) + end)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((match.start(1), match.start(1) + end, block[:end], value))

        for start in self._candidate_starts(content, exhaustive=exhaustive):
            snippet = content[start:]
            try:
                value, end = decoder.raw_decode(snippet)
            except json.JSONDecodeError:
                continue
            key = (start, start + end)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((start, start + end, snippet[:end], value))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return [(text, value) for _, _, text, value in candidates]

    def _candidate_starts(self, content: str, *, exhaustive: bool) -> list[int]:
        if exhaustive:
            return [index for index, char in enumerate(content) if char in JSON_ROOT_START_CHARS]

        starts: list[int] = []
        offset = 0
        for line in content.splitlines(keepends=True):
            stripped = line.lstrip(" \t\r")
            if stripped and stripped[0] in JSON_ROOT_START_CHARS:
                candidate_start = offset + (len(line) - len(stripped))
                starts.append(candidate_start)
            offset += len(line)
        if content and content[0] in JSON_ROOT_START_CHARS and 0 not in starts:
            starts.append(0)
        return starts

    def _looks_like_compaction_request_wrapper(self, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        return {"pass_index", "current_tokens", "target_tokens", "target_path", "content"}.issubset(value)

    def _request_content(self, messages: list[dict[str, str]]) -> tuple[str, int]:
        if not self.available():
            raise ProviderError("OpenAI-compatible provider is not configured.", parse_status="provider_unavailable")

        request_body = self._render_request_body(messages)
        request = urllib.request.Request(
            self._settings.base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
            },
            data=request_body.encode("utf-8"),
            method="POST",
        )
        started = perf_counter()

        try:
            with urllib.request.urlopen(request, timeout=self._settings.model_request_timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            response_body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(
                f"Model request failed with status {exc.code}: {exc.reason}",
                parse_status="http_error",
                raw_response_preview=response_body,
                latency_ms=latency_ms,
            ) from exc
        except urllib.error.URLError as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            raise ProviderError(
                f"Model request failed: {exc}",
                parse_status="network_error",
                raw_response_preview=str(exc),
                latency_ms=latency_ms,
            ) from exc

        latency_ms = int((perf_counter() - started) * 1000)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Model returned invalid JSON response body.",
                parse_status="invalid_response_json",
                raw_response_preview=raw_body,
                latency_ms=latency_ms,
            ) from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "Model returned an unexpected response shape.",
                parse_status="unexpected_response_shape",
                raw_response_preview=raw_body,
                latency_ms=latency_ms,
            ) from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content), latency_ms

    def _render_request_body(self, messages: list[dict[str, str]]) -> str:
        body = {
            "model": self._settings.model,
            "temperature": TEMPERATURE,
            "messages": messages,
        }
        return json.dumps(body)
