from __future__ import annotations

from mina_agent.providers.openai_compatible import ProviderDecisionResult
from mina_agent.runtime.decision_engine import DecisionEngine
from mina_agent.schemas import CapabilityRequest, ConfirmationRequest, DelegateRequest, ModelDecision


class DeliberationEngine:
    def __init__(self, decision_engine: DecisionEngine) -> None:
        self._decision_engine = decision_engine

    def decide(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        result = self._decision_engine.decide(messages)
        result.decision = self.normalize(result.decision)
        return result

    def normalize(self, decision: ModelDecision) -> ModelDecision:
        if decision.intent is None:
            if decision.mode == "final_reply":
                decision.intent = "reply"
            elif decision.mode == "call_capability":
                decision.intent = "execute"
        if decision.capability_request is None and decision.capability_id:
            decision.capability_request = CapabilityRequest(
                capability_id=decision.capability_id,
                arguments=dict(decision.arguments),
                effect_summary=decision.effect_summary,
                requires_confirmation=decision.requires_confirmation,
            )
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
