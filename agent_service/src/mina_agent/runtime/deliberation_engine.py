from __future__ import annotations

from typing import Any, TypeVar

import json

from mina_agent.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderDecisionResult,
    ProviderStructuredResult,
    ProviderValueResult,
)
from mina_agent.schemas import (
    CompanionEvaluateDecision,
    CapabilityRequest,
    ConfirmationRequest,
    DelegateRequest,
    DelegateSummary,
    ModelDecision,
)

ModelT = TypeVar("ModelT")


class DeliberationEngine:
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        self._provider = provider

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        result = self._provider.decide(messages)
        result.decision = self.normalize(result.decision)
        return result

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

    def estimate_prompt_tokens(self, messages: list[dict[str, str]]) -> dict[str, int | str]:
        estimator = getattr(self._provider, "estimate_prompt_tokens", None)
        if callable(estimator):
            payload = estimator(messages)
            if isinstance(payload, dict):
                return payload
        return {
            "model": "",
            "encoding_name": "cl100k_base",
            "message_count": len(messages),
            "message_tokens": [0 for _ in messages],
            "total_tokens": 0,
        }

    def summarize_delegate(self, messages: list[dict[str, str]]) -> ProviderStructuredResult[DelegateSummary]:
        complete_json = getattr(self._provider, "complete_json", None)
        if complete_json is None:
            raise AttributeError("Provider does not support structured delegate summaries.")
        return complete_json(messages, DelegateSummary)

    def evaluate_companion(self, messages: list[dict[str, str]]) -> ProviderStructuredResult[CompanionEvaluateDecision]:
        return self.complete_structured(messages, CompanionEvaluateDecision)

    def complete_structured(self, messages: list[dict[str, str]], response_model: type[ModelT]):
        complete_json = getattr(self._provider, "complete_json", None)
        if complete_json is None:
            raise AttributeError("Provider does not support structured responses.")
        return complete_json(messages, response_model)

    def compact_target(
        self,
        messages: list[dict[str, str]],
        *,
        expected_root_types: tuple[type[Any], ...] | None = None,
    ) -> ProviderValueResult:
        complete_json_value = getattr(self._provider, "complete_json_value", None)
        if complete_json_value is None:
            raise AttributeError("Provider does not support target-scoped context compaction.")
        return complete_json_value(messages, expected_root_types=expected_root_types)

    def normalize(self, decision: ModelDecision) -> ModelDecision:
        if (
            decision.capability_request is not None
            and not str(decision.capability_request.capability_id or "").strip()
            and decision.capability_id
        ):
            decision.capability_request = CapabilityRequest(
                capability_id=decision.capability_id,
                arguments=dict(decision.capability_request.arguments or decision.arguments),
                effect_summary=decision.capability_request.effect_summary or decision.effect_summary,
                requires_confirmation=(
                    decision.capability_request.requires_confirmation or decision.requires_confirmation
                ),
            )
        if decision.capability_request is not None and decision.capability_id is None:
            capability_id = str(decision.capability_request.capability_id or "").strip()
            if capability_id:
                decision.capability_id = capability_id
        if decision.capability_request is None and decision.capability_id:
            decision.capability_request = CapabilityRequest(
                capability_id=decision.capability_id,
                arguments=dict(decision.arguments),
                effect_summary=decision.effect_summary,
                requires_confirmation=decision.requires_confirmation,
            )
        if decision.intent is None:
            if decision.mode == "final_reply":
                decision.intent = "reply"
            elif (
                decision.mode == "call_capability"
                or (decision.capability_request is not None and bool(decision.capability_request.capability_id))
                or bool(decision.capability_id)
            ):
                decision.intent = "execute"
            elif decision.delegate_request is not None:
                decision.intent = "delegate_plan" if decision.delegate_request.role == "plan" else "delegate_explore"
            elif decision.delegate_role is not None:
                decision.intent = "delegate_plan" if decision.delegate_role == "plan" else "delegate_explore"
        if decision.intent == "delegate_explore" and decision.delegate_request is None:
            decision.delegate_request = DelegateRequest(
                role="explore",
                objective=decision.delegate_objective or decision.final_reply or "",
            )
            decision.delegate_role = "explore"
        if decision.intent == "delegate_plan" and decision.delegate_request is None:
            decision.delegate_request = DelegateRequest(
                role="plan",
                objective=decision.delegate_objective or decision.final_reply or "",
            )
            decision.delegate_role = "plan"
        if decision.intent == "await_confirmation" and decision.confirmation_request is None and decision.effect_summary:
            decision.confirmation_request = ConfirmationRequest(
                effect_summary=decision.effect_summary,
                reason=decision.notes,
            )
        if decision.intent in {"reply", "guide"} and decision.final_reply is None:
            decision.final_reply = "我先陪你把这件事理清楚。"
        return decision
