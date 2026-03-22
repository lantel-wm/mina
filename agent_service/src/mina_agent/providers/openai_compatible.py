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


JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
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
        match = JSON_BLOCK_RE.search(content)
        if not match:
            raise ProviderError(
                "Model returned a response without a JSON decision block.",
                parse_status="missing_decision_json",
                raw_response_preview=content,
                latency_ms=latency_ms,
            )
        try:
            payload = response_model.model_validate_json(match.group(0))
        except ValidationError as exc:
            raise ProviderError(
                f"Model returned a JSON block that does not match the {response_model.__name__} schema.",
                parse_status="invalid_decision_json",
                raw_response_preview=match.group(0),
                latency_ms=latency_ms,
            ) from exc
        return ProviderStructuredResult(
            payload=payload,
            latency_ms=latency_ms,
            raw_response_preview=content,
            parse_status="ok",
            model=self._settings.model or "",
            temperature=TEMPERATURE,
            message_count=len(messages),
        )

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
