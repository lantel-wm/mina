from __future__ import annotations

import json

from mina_agent.providers.openai_compatible import OpenAICompatibleProvider, ProviderDecisionResult
from mina_agent.runtime.prompt_token_estimator import PromptTokenEstimator


class DecisionEngine:
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        self._provider = provider
        self._fallback_token_estimator = PromptTokenEstimator(None)

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        return self._provider.decide(messages)

    def debug_request_buffer(self, messages: list[dict[str, str]]) -> dict[str, str]:
        builder = getattr(self._provider, "debug_request_buffer", None)
        if callable(builder):
            payload = builder(messages)
            if isinstance(payload, dict):
                return payload
        return {
            "kind": "provider_decide_messages",
            "content_type": "application/json",
            "extension": ".json",
            "body_text": json.dumps(messages, ensure_ascii=False, indent=2),
        }

    def complete_json(self, messages, response_model):
        completer = getattr(self._provider, "complete_json", None)
        if callable(completer):
            return completer(messages, response_model)
        raise AttributeError("Provider does not support structured JSON completions.")

    def estimate_prompt_tokens(self, messages: list[dict[str, str]]) -> dict[str, int | str]:
        estimator = getattr(self._provider, "estimate_prompt_tokens", None)
        if callable(estimator):
            estimate = estimator(messages)
            if isinstance(estimate, dict):
                return estimate
        fallback = self._fallback_token_estimator.estimate_messages(messages)
        return {
            "model": "",
            "encoding_name": fallback.encoding_name,
            "message_count": len(messages),
            "message_tokens": fallback.per_message_tokens,
            "total_tokens": fallback.total_tokens,
        }
