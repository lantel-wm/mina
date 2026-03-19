from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider
from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeState
from mina_agent.runtime.context_builder import ContextBuilder
from mina_agent.schemas import ActionRequestPayload, ActionResultPayload, ModelDecision, TurnResponse, TurnResumeRequest, TurnStartRequest


@dataclass(slots=True)
class AgentServices:
    settings: Settings
    store: Store
    audit: AuditLogger
    policy_engine: PolicyEngine
    capability_registry: CapabilityRegistry
    context_builder: ContextBuilder
    provider: OpenAICompatibleProvider


class AgentLoop:
    def __init__(self, services: AgentServices) -> None:
        self._services = services

    def start_turn(self, request: TurnStartRequest) -> TurnResponse:
        self._services.store.ensure_session(request.session_ref, request.player.name, request.player.role)
        pending_confirmation = self._services.store.get_pending_confirmation(request.session_ref)

        state = {
            "session_ref": request.session_ref,
            "turn_id": request.turn_id,
            "request": request.model_dump(),
            "step_index": 0,
            "observations": [],
            "pending_confirmation": pending_confirmation,
        }
        self._services.store.create_turn(request.turn_id, request.session_ref, request.user_message, state)
        self._services.audit.record("turn_started", {"turn_id": request.turn_id, "session_ref": request.session_ref})
        return self._advance(request, state)

    def resume_turn(self, continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        continuation = self._services.store.get_continuation(continuation_id)
        if continuation is None:
            raise KeyError(f"Unknown continuation_id: {continuation_id}")

        state = continuation.state
        turn_request = TurnStartRequest.model_validate(state["request"])
        observations = state.setdefault("observations", [])

        for result in request.action_results:
            payload = {
                "intent_id": result.intent_id,
                "capability_id": self._lookup_capability_id(state, result.intent_id),
                "status": result.status,
                "risk_class": self._lookup_risk_class(state, result.intent_id),
                "observations": result.observations,
                "side_effect_summary": result.side_effect_summary,
                "timing_ms": result.timing_ms,
                "state_fingerprint": result.state_fingerprint,
                "preconditions_passed": result.preconditions_passed,
                "error_message": result.error_message,
            }
            observations.append({"source": "bridge_result", "payload": payload})
            self._services.store.log_execution_record(
                turn_id=continuation.turn_id,
                intent_id=result.intent_id,
                capability_id=payload["capability_id"],
                risk_class=payload["risk_class"],
                status=result.status,
                observations=result.observations,
                side_effect_summary=result.side_effect_summary,
                timing_ms=result.timing_ms,
            )
            self._services.store.log_step_event(continuation.turn_id, int(state["step_index"]), "bridge_result", payload)

        state.pop("pending_action_batch", None)
        self._services.store.clear_continuation(continuation.turn_id, state)
        self._services.audit.record("turn_resumed", {"turn_id": continuation.turn_id, "continuation_id": continuation_id})
        return self._advance(turn_request, state)

    def _advance(self, request: TurnStartRequest, state: dict[str, Any]) -> TurnResponse:
        capabilities = self._services.capability_registry.resolve(request)
        runtime_state = RuntimeState(
            request=request,
            local_observations=state.setdefault("observations", []),
            pending_confirmation=state.get("pending_confirmation"),
        )

        while state["step_index"] < min(request.limits.max_agent_steps, self._services.settings.max_agent_steps):
            messages = self._services.context_builder.build_messages(
                request=request,
                recent_turns=self._services.store.list_recent_turns(request.session_ref),
                memories=self._services.store.list_memories(request.session_ref),
                capability_descriptors=[cap.descriptor for cap in capabilities],
                observations=runtime_state.local_observations,
                pending_confirmation=runtime_state.pending_confirmation,
            )

            try:
                decision = self._services.provider.decide(messages)
            except Exception as exc:
                final_reply = f"Mina agent service is online, but no model decision is available: {exc}"
                self._finalize(request.turn_id, request.session_ref, final_reply)
                return TurnResponse(type="final_reply", final_reply=final_reply)

            self._services.store.log_step_event(request.turn_id, int(state["step_index"]), "model_decision", decision.model_dump())
            self._services.audit.record("model_decision", {"turn_id": request.turn_id, "decision": decision.model_dump()})

            if decision.mode == "final_reply":
                final_reply = decision.final_reply or "I do not have a better response yet."
                self._finalize(request.turn_id, request.session_ref, final_reply)
                return TurnResponse(type="final_reply", final_reply=final_reply)

            capability = self._services.capability_registry.get(capabilities, decision.capability_id or "")
            if capability is None:
                final_reply = f"Mina selected an unknown capability: {decision.capability_id}"
                self._finalize(request.turn_id, request.session_ref, final_reply)
                return TurnResponse(type="final_reply", final_reply=final_reply)

            if capability.handler_kind == "internal":
                observation = self._services.capability_registry.execute_internal(capability, decision.arguments, runtime_state)
                runtime_state.local_observations.append(
                    {
                        "source": capability.descriptor.id,
                        "payload": observation,
                    }
                )
                state["step_index"] += 1
                self._services.store.log_step_event(request.turn_id, int(state["step_index"]), "internal_capability", observation)
                continue

            continuation_id = str(uuid.uuid4())
            action_request_payload = self._services.capability_registry.bridge_action_request(
                capability,
                decision.arguments,
                decision.effect_summary,
                decision.requires_confirmation,
            )
            action_request_payload["continuation_id"] = continuation_id

            if action_request_payload["requires_confirmation"]:
                confirmation_id = str(uuid.uuid4())
                self._services.store.put_pending_confirmation(
                    request.session_ref,
                    confirmation_id,
                    action_request_payload["effect_summary"],
                    action_request_payload,
                )
                reply = (
                    "I have a high-risk action plan ready but it requires confirmation. "
                    f"Planned effect: {action_request_payload['effect_summary']}"
                )
                self._finalize(request.turn_id, request.session_ref, reply)
                return TurnResponse(type="final_reply", final_reply=reply, pending_confirmation_id=confirmation_id)

            state["pending_action_batch"] = [action_request_payload]
            state["step_index"] += 1
            self._services.store.put_continuation(continuation_id, request.turn_id, state)
            return TurnResponse(
                type="action_request_batch",
                continuation_id=continuation_id,
                action_request_batch=[ActionRequestPayload.model_validate(action_request_payload)],
            )

        final_reply = "Mina stopped because the configured step budget was exhausted."
        self._finalize(request.turn_id, request.session_ref, final_reply)
        return TurnResponse(type="final_reply", final_reply=final_reply)

    def _finalize(self, turn_id: str, session_ref: str, final_reply: str) -> None:
        self._services.store.finish_turn(turn_id, final_reply)
        self._services.store.add_memory(session_ref, "turn_summary", final_reply)
        self._services.audit.record("turn_completed", {"turn_id": turn_id, "final_reply": final_reply})

    def _lookup_capability_id(self, state: dict[str, Any], intent_id: str) -> str:
        for payload in state.get("pending_action_batch", []):
            if payload["intent_id"] == intent_id:
                return payload["capability_id"]
        return "unknown"

    def _lookup_risk_class(self, state: dict[str, Any], intent_id: str) -> str:
        for payload in state.get("pending_action_batch", []):
            if payload["intent_id"] == intent_id:
                return payload["risk_class"]
        return "read_only"
