from __future__ import annotations

import json

from mina_agent.providers.openai_compatible import OpenAICompatibleProvider, ProviderDecisionResult


class DecisionEngine:
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        self._provider = provider

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
