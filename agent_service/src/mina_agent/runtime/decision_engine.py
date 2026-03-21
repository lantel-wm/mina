from __future__ import annotations

from mina_agent.providers.openai_compatible import OpenAICompatibleProvider, ProviderDecisionResult


class DecisionEngine:
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        self._provider = provider

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        return self._provider.decide(messages)
