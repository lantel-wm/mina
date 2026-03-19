from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from mina_agent.audit.logger import AuditLogger
from mina_agent.config import Settings
from mina_agent.debug import DebugRecorder
from mina_agent.memory.store import Store
from mina_agent.policy.policy_engine import PolicyEngine
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider, ProviderDecisionResult, ProviderError
from mina_agent.runtime.capability_registry import CapabilityRegistry, RuntimeState
from mina_agent.runtime.context_builder import ContextBuilder
from mina_agent.schemas import (
    ActionRequestPayload,
    ActionResultPayload,
    TraceChipPayload,
    TraceEventPayload,
    TurnResponse,
    TurnResumeRequest,
    TurnStartRequest,
)


@dataclass(slots=True)
class AgentServices:
    settings: Settings
    store: Store
    audit: AuditLogger
    debug: DebugRecorder
    policy_engine: PolicyEngine
    capability_registry: CapabilityRegistry
    context_builder: ContextBuilder
    provider: OpenAICompatibleProvider


class AgentLoop:
    _INTERNAL_START_STATUS_LABELS = (
        "占星中",
        "读牌中",
        "观测命轨中",
        "聆听预兆中",
        "解读星象中",
    )

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
            "block_subject_lock": None,
        }
        self._services.store.create_turn(request.turn_id, request.session_ref, request.user_message, state)
        self._services.audit.record("turn_started", {"turn_id": request.turn_id, "session_ref": request.session_ref})
        self._services.debug.record_event(
            request.turn_id,
            "turn_started",
            {
                "session_ref": request.session_ref,
                "user_message": request.user_message,
                "player": request.player.model_dump(),
                "server_env": request.server_env.model_dump(),
                "limits": request.limits.model_dump(),
                "pending_confirmation": pending_confirmation,
            },
        )
        return self._advance(request, state)

    def resume_turn(self, continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        continuation = self._services.store.get_continuation(continuation_id)
        if continuation is None:
            raise KeyError(f"Unknown continuation_id: {continuation_id}")

        state = continuation.state
        turn_request = TurnStartRequest.model_validate(state["request"])
        observations = state.setdefault("observations", [])
        state.setdefault("block_subject_lock", None)
        step_index = int(state.get("step_index", 0))

        self._services.debug.record_event(
            continuation.turn_id,
            "turn_resumed",
            {
                "continuation_id": continuation_id,
                "action_result_count": len(request.action_results),
                "intent_ids": [result.intent_id for result in request.action_results],
                "statuses": [result.status for result in request.action_results],
            },
            step_index=step_index,
        )

        for result in request.action_results:
            capability_id = self._lookup_capability_id(state, result.intent_id)
            payload = {
                "intent_id": result.intent_id,
                "capability_id": capability_id,
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
            self._update_block_subject_state(state, capability_id, result.observations)
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
            self._services.store.log_step_event(continuation.turn_id, step_index, "bridge_result", payload)
            self._services.debug.record_event(
                continuation.turn_id,
                "bridge_result",
                self._bridge_result_payload(result, state),
                step_index=step_index,
            )

        state.pop("pending_action_batch", None)
        self._services.store.clear_continuation(continuation.turn_id, state)
        self._services.audit.record("turn_resumed", {"turn_id": continuation.turn_id, "continuation_id": continuation_id})
        return self._advance(turn_request, state)

    def _advance(self, request: TurnStartRequest, state: dict[str, Any]) -> TurnResponse:
        state.setdefault("observations", [])
        state.setdefault("block_subject_lock", None)
        capabilities = self._services.capability_registry.resolve(request)
        runtime_state = RuntimeState(
            request=request,
            local_observations=state.setdefault("observations", []),
            pending_confirmation=state.get("pending_confirmation"),
        )
        trace_events: list[TraceEventPayload] = []
        self._services.debug.record_event(
            request.turn_id,
            "capabilities_resolved",
            self._capabilities_resolved_payload(capabilities),
        )

        while state["step_index"] < min(request.limits.max_agent_steps, self._services.settings.max_agent_steps):
            current_step = int(state["step_index"]) + 1
            context_result = self._services.context_builder.build_messages(
                request=request,
                recent_turns=self._services.store.list_recent_turns(request.session_ref),
                memories=self._services.store.list_memories(request.session_ref),
                capability_descriptors=[cap.descriptor for cap in capabilities],
                observations=runtime_state.local_observations,
                pending_confirmation=runtime_state.pending_confirmation,
            )
            self._services.debug.record_event(
                request.turn_id,
                "context_built",
                {
                    "sections": context_result.sections,
                    "message_stats": context_result.message_stats,
                    "composition": context_result.composition,
                },
                step_index=current_step,
            )
            self._services.debug.record_event(
                request.turn_id,
                "model_request",
                {
                    "model": self._services.settings.model or "",
                    "message_count": len(context_result.messages),
                    "message_stats": context_result.message_stats,
                    "messages": context_result.messages,
                },
                step_index=current_step,
            )

            try:
                provider_result = self._services.provider.decide(context_result.messages)
            except ProviderError as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "model_response",
                    {
                        "model": self._services.settings.model or "",
                        "latency_ms": exc.latency_ms,
                        "parse_status": exc.parse_status,
                        "raw_response_preview": exc.raw_response_preview,
                        "error": str(exc),
                    },
                    step_index=current_step,
                )
                final_reply = f"Mina agent service is online, but no model decision is available: {exc}"
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    final_reply,
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={
                        "reason": "provider_error",
                        "parse_status": exc.parse_status,
                        "latency_ms": exc.latency_ms,
                        "error": str(exc),
                        "raw_response_preview": exc.raw_response_preview,
                    },
                )
            except Exception as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "model_response",
                    {
                        "model": self._services.settings.model or "",
                        "latency_ms": 0,
                        "parse_status": "unexpected_provider_error",
                        "raw_response_preview": str(exc),
                        "error": str(exc),
                    },
                    step_index=current_step,
                )
                final_reply = f"Mina agent service is online, but no model decision is available: {exc}"
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    final_reply,
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={
                        "reason": "unexpected_provider_error",
                        "error": str(exc),
                    },
                )

            self._services.debug.record_event(
                request.turn_id,
                "model_response",
                self._model_response_payload(provider_result),
                step_index=current_step,
            )

            decision = provider_result.decision
            self._services.store.log_step_event(request.turn_id, int(state["step_index"]), "model_decision", decision.model_dump())
            self._services.audit.record("model_decision", {"turn_id": request.turn_id, "decision": decision.model_dump()})
            self._services.debug.record_event(request.turn_id, "model_decision", decision.model_dump(), step_index=current_step)

            if decision.mode == "final_reply":
                guard_payload = self._semantic_verification_guard(runtime_state.local_observations)
                if guard_payload is not None:
                    runtime_state.local_observations.append(
                        {
                            "source": "runtime.guard.semantic_verification",
                            "payload": guard_payload,
                        }
                    )
                    state["step_index"] += 1
                    self._services.store.log_step_event(
                        request.turn_id,
                        int(state["step_index"]),
                        "runtime_guard",
                        guard_payload,
                    )
                    self._services.debug.record_event(
                        request.turn_id,
                        "runtime_guard",
                        guard_payload,
                        step_index=current_step,
                    )
                    continue
                final_reply = decision.final_reply or "I do not have a better response yet."
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    final_reply,
                    trace_events,
                    status="completed",
                    step_index=current_step,
                )

            capability = self._services.capability_registry.get(capabilities, decision.capability_id or "")
            if capability is None:
                final_reply = f"Mina selected an unknown capability: {decision.capability_id}"
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    final_reply,
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={
                        "reason": "unknown_capability",
                        "capability_id": decision.capability_id,
                        "decision": decision.model_dump(),
                    },
                )

            resolved_arguments = self._resolve_capability_arguments(capability, decision.arguments, state)
            self._services.debug.record_event(
                request.turn_id,
                "capability_started",
                self._capability_started_payload(capability, decision, resolved_arguments),
                step_index=current_step,
            )

            if capability.handler_kind == "internal":
                trace_events.append(self._internal_start_trace(capability, resolved_arguments, current_step))
                started = perf_counter()
                try:
                    observation = self._services.capability_registry.execute_internal(
                        capability,
                        resolved_arguments,
                        runtime_state,
                    )
                except Exception as exc:
                    latency_ms = int((perf_counter() - started) * 1000)
                    self._services.debug.record_event(
                        request.turn_id,
                        "capability_finished",
                        {
                            "status": "failed",
                            "capability_id": capability.descriptor.id,
                            "handler_kind": capability.handler_kind,
                            "latency_ms": latency_ms,
                            "error": str(exc),
                        },
                        step_index=current_step,
                    )
                    final_reply = f"Mina internal capability failed: {exc}"
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        final_reply,
                        trace_events,
                        status="failed",
                        step_index=current_step,
                        debug_payload={
                            "reason": "internal_capability_error",
                            "capability_id": capability.descriptor.id,
                            "latency_ms": latency_ms,
                            "error": str(exc),
                        },
                    )

                latency_ms = int((perf_counter() - started) * 1000)
                runtime_state.local_observations.append(
                    {
                        "source": capability.descriptor.id,
                        "payload": observation,
                    }
                )
                state["step_index"] += 1
                self._services.store.log_step_event(request.turn_id, int(state["step_index"]), "internal_capability", observation)
                self._services.debug.record_event(
                    request.turn_id,
                    "capability_finished",
                    self._internal_capability_finished_payload(capability, observation, latency_ms),
                    step_index=current_step,
                )
                trace_events.append(self._internal_finish_trace(capability, observation, int(state["step_index"])))
                continue

            continuation_id = str(uuid.uuid4())
            action_request_payload = self._services.capability_registry.bridge_action_request(
                capability,
                resolved_arguments,
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
                trace_events.append(
                    TraceEventPayload(
                        status_label="待确认",
                        status_tone="warning",
                        title=self._capability_title(capability.descriptor.id),
                        detail=action_request_payload["effect_summary"],
                        secondary=[
                            TraceChipPayload(label=f"第 {current_step} 步", tone="muted"),
                            TraceChipPayload(label="高风险计划", tone="warning"),
                        ],
                    )
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "capability_finished",
                    self._bridge_capability_finished_payload(
                        capability,
                        action_request_payload,
                        "awaiting_confirmation",
                        current_step,
                        confirmation_id=confirmation_id,
                    ),
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    reply,
                    trace_events,
                    status="completed",
                    step_index=current_step,
                    pending_confirmation_id=confirmation_id,
                )

            state["pending_action_batch"] = [action_request_payload]
            state["step_index"] += 1
            self._services.store.put_continuation(continuation_id, request.turn_id, state)
            self._services.debug.record_event(
                request.turn_id,
                "capability_finished",
                self._bridge_capability_finished_payload(
                    capability,
                    action_request_payload,
                    "awaiting_bridge_result",
                    current_step,
                    continuation_id=continuation_id,
                ),
                step_index=current_step,
            )
            return TurnResponse(
                type="action_request_batch",
                continuation_id=continuation_id,
                action_request_batch=[ActionRequestPayload.model_validate(action_request_payload)],
                trace_events=trace_events,
            )

        final_reply = "Mina stopped because the configured step budget was exhausted."
        return self._return_final_reply(
            request.turn_id,
            request.session_ref,
            final_reply,
            trace_events,
            status="completed",
            step_index=int(state["step_index"]),
            debug_payload={"reason": "step_budget_exhausted"},
        )

    def _return_final_reply(
        self,
        turn_id: str,
        session_ref: str,
        final_reply: str,
        trace_events: list[TraceEventPayload],
        *,
        status: str,
        step_index: int | None,
        debug_payload: dict[str, Any] | None = None,
        pending_confirmation_id: str | None = None,
    ) -> TurnResponse:
        self._finalize(turn_id, session_ref, final_reply, status=status)
        payload = {"final_reply": final_reply}
        if debug_payload is not None:
            payload.update(debug_payload)
        self._services.debug.record_event(
            turn_id,
            "turn_completed" if status == "completed" else "turn_failed",
            payload,
            step_index=step_index,
        )
        return TurnResponse(
            type="final_reply",
            final_reply=final_reply,
            pending_confirmation_id=pending_confirmation_id,
            trace_events=trace_events,
        )

    def _finalize(self, turn_id: str, session_ref: str, final_reply: str, *, status: str = "completed") -> None:
        self._services.store.finish_turn(turn_id, final_reply, status=status)
        if status == "completed":
            self._services.store.add_memory(session_ref, "turn_summary", final_reply)
        self._services.audit.record(
            "turn_completed" if status == "completed" else "turn_failed",
            {"turn_id": turn_id, "final_reply": final_reply},
        )

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

    def _capabilities_resolved_payload(self, capabilities: list[Any]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_risk_class: dict[str, int] = {}
        by_handler_kind: dict[str, int] = {}
        descriptors: list[dict[str, Any]] = []

        for capability in capabilities:
            descriptor = capability.descriptor
            by_kind[descriptor.kind] = by_kind.get(descriptor.kind, 0) + 1
            by_risk_class[descriptor.risk_class] = by_risk_class.get(descriptor.risk_class, 0) + 1
            by_handler_kind[capability.handler_kind] = by_handler_kind.get(capability.handler_kind, 0) + 1
            descriptors.append(
                {
                    "id": descriptor.id,
                    "kind": descriptor.kind,
                    "risk_class": descriptor.risk_class,
                    "execution_mode": descriptor.execution_mode,
                    "requires_confirmation": descriptor.requires_confirmation,
                    "handler_kind": capability.handler_kind,
                    "description": descriptor.description,
                    "args_schema": descriptor.args_schema,
                    "result_schema": descriptor.result_schema,
                }
            )

        return {
            "total": len(capabilities),
            "ids": [capability.descriptor.id for capability in capabilities],
            "by_kind": by_kind,
            "by_risk_class": by_risk_class,
            "by_handler_kind": by_handler_kind,
            "capabilities": descriptors,
        }

    def _model_response_payload(self, provider_result: ProviderDecisionResult) -> dict[str, Any]:
        return {
            "model": provider_result.model,
            "temperature": provider_result.temperature,
            "message_count": provider_result.message_count,
            "latency_ms": provider_result.latency_ms,
            "parse_status": provider_result.parse_status,
            "raw_response_preview": provider_result.raw_response_preview,
        }

    def _capability_started_payload(
        self,
        capability: Any,
        decision: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "capability_id": capability.descriptor.id,
            "handler_kind": capability.handler_kind,
            "kind": capability.descriptor.kind,
            "risk_class": capability.descriptor.risk_class,
            "execution_mode": capability.descriptor.execution_mode,
            "requires_confirmation": decision.requires_confirmation or capability.descriptor.requires_confirmation,
            "effect_summary": decision.effect_summary or capability.descriptor.description,
            "arguments": arguments,
        }

    def _internal_capability_finished_payload(
        self,
        capability: Any,
        observation: dict[str, Any],
        latency_ms: int,
    ) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "capability_id": capability.descriptor.id,
            "handler_kind": capability.handler_kind,
            "kind": capability.descriptor.kind,
            "latency_ms": latency_ms,
            "observation": self._debug_observation(capability.descriptor.id, observation),
        }

    def _bridge_capability_finished_payload(
        self,
        capability: Any,
        action_request_payload: dict[str, Any],
        status: str,
        step_index: int,
        *,
        continuation_id: str | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "step_index": step_index,
            "capability_id": capability.descriptor.id,
            "handler_kind": capability.handler_kind,
            "kind": capability.descriptor.kind,
            "risk_class": action_request_payload["risk_class"],
            "effect_summary": action_request_payload["effect_summary"],
            "requires_confirmation": action_request_payload["requires_confirmation"],
            "arguments": action_request_payload["arguments"],
            "preconditions": action_request_payload["preconditions"],
            "intent_id": action_request_payload["intent_id"],
        }
        if continuation_id is not None:
            payload["continuation_id"] = continuation_id
        if confirmation_id is not None:
            payload["confirmation_id"] = confirmation_id
        return payload

    def _bridge_result_payload(self, result: ActionResultPayload, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent_id": result.intent_id,
            "capability_id": self._lookup_capability_id(state, result.intent_id),
            "risk_class": self._lookup_risk_class(state, result.intent_id),
            "status": result.status,
            "preconditions_passed": result.preconditions_passed,
            "side_effect_summary": result.side_effect_summary,
            "timing_ms": result.timing_ms,
            "state_fingerprint": result.state_fingerprint,
            "error_message": result.error_message,
            "observations": result.observations,
        }

    def _update_block_subject_state(
        self,
        state: dict[str, Any],
        capability_id: str,
        observations: dict[str, Any],
    ) -> None:
        if not self._bridge_capability_supports_block_pos(state, capability_id):
            return

        if capability_id == "game.target_block.read":
            target_found = observations.get("target_found")
            if target_found is False and state.get("block_subject_lock") is None:
                return

        pos = observations.get("pos")
        if not self._is_block_pos(pos):
            return

        lock = state.get("block_subject_lock")

        if lock is None:
            lock = {
                "pos": {"x": int(pos["x"]), "y": int(pos["y"]), "z": int(pos["z"])},
            }
            state["block_subject_lock"] = lock

        if not self._same_block_pos(lock.get("pos"), pos):
            return

        for key in ("block_name", "block_id", "summary"):
            value = observations.get(key)
            if isinstance(value, str) and value.strip():
                lock[key] = value.strip()

        target_found = observations.get("target_found")
        if isinstance(target_found, bool):
            lock["target_found"] = target_found

    def _resolve_capability_arguments(
        self,
        capability: Any,
        arguments: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_arguments = dict(arguments)
        if not self._capability_supports_block_pos(capability):
            return resolved_arguments
        if self._is_block_pos(resolved_arguments.get("block_pos")):
            return resolved_arguments

        lock = state.get("block_subject_lock")
        if not isinstance(lock, dict):
            return resolved_arguments

        locked_pos = lock.get("pos")
        if not self._is_block_pos(locked_pos):
            return resolved_arguments

        resolved_arguments["block_pos"] = {
            "x": int(locked_pos["x"]),
            "y": int(locked_pos["y"]),
            "z": int(locked_pos["z"]),
        }
        return resolved_arguments

    def _capability_supports_block_pos(self, capability: Any) -> bool:
        args_schema = getattr(capability.descriptor, "args_schema", {})
        return isinstance(args_schema, dict) and "block_pos" in args_schema

    def _bridge_capability_supports_block_pos(self, state: dict[str, Any], capability_id: str) -> bool:
        request_payload = state.get("request", {})
        if not isinstance(request_payload, dict):
            return False

        visible_capabilities = request_payload.get("visible_capabilities", [])
        if not isinstance(visible_capabilities, list):
            return False

        for capability in visible_capabilities:
            if not isinstance(capability, dict) or capability.get("id") != capability_id:
                continue
            args_schema = capability.get("args_schema", {})
            return isinstance(args_schema, dict) and "block_pos" in args_schema
        return False

    def _is_block_pos(self, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and all(key in value for key in ("x", "y", "z"))
            and all(isinstance(value[key], (int, float)) for key in ("x", "y", "z"))
        )

    def _same_block_pos(self, left: Any, right: Any) -> bool:
        if not self._is_block_pos(left) or not self._is_block_pos(right):
            return False
        return (
            int(left["x"]) == int(right["x"])
            and int(left["y"]) == int(right["y"])
            and int(left["z"]) == int(right["z"])
        )

    def _debug_observation(self, capability_id: str, observation: dict[str, Any]) -> dict[str, Any]:
        if capability_id not in {"retrieval.minecraft_facts.lookup", "retrieval.minecraft_semantics.search"}:
            return observation
        if capability_id == "retrieval.minecraft_facts.lookup":
            return {
                "result_count": observation.get("result_count"),
                "source_labels": observation.get("source_labels", []),
                "not_indexed": observation.get("not_indexed", []),
                "results": [
                    {
                        "dataset": item.get("dataset"),
                        "fact_id": item.get("fact_id"),
                        "title": item.get("title"),
                        "source_label": item.get("source_label"),
                        "match_type": item.get("match_type"),
                    }
                    for item in observation.get("results", [])
                ],
            }
        return {
            "result_count": observation.get("result_count"),
            "verification_required": observation.get("verification_required"),
            "fact_domains": observation.get("fact_domains", []),
            "results": [
                {
                    "doc_path": item.get("doc_path"),
                    "title": item.get("title"),
                    "source_kind": item.get("source_kind"),
                    "chunk_index": item.get("chunk_index"),
                    "verification_required": item.get("verification_required"),
                    "score": item.get("score"),
                    "content_preview": item.get("content", ""),
                }
                for item in observation.get("results", [])
            ],
        }

    def _internal_start_trace(
        self,
        capability: Any,
        arguments: dict[str, Any],
        step_index: int,
    ) -> TraceEventPayload:
        return TraceEventPayload(
            status_label=random.choice(self._INTERNAL_START_STATUS_LABELS),
            status_tone="info",
            title=self._capability_title(capability.descriptor.id),
            detail=self._internal_start_detail(capability.descriptor.id, arguments),
            secondary=[
                TraceChipPayload(label=f"第 {step_index} 步", tone="muted"),
                TraceChipPayload(label=self._kind_label(capability.descriptor.kind), tone="muted"),
            ],
        )

    def _internal_finish_trace(
        self,
        capability: Any,
        observation: dict[str, Any],
        step_index: int,
    ) -> TraceEventPayload:
        secondary = [
            TraceChipPayload(label=f"第 {step_index} 步", tone="muted"),
            TraceChipPayload(label=self._kind_label(capability.descriptor.kind), tone="muted"),
        ]

        result_count = self._observation_count_label(capability.descriptor.id, observation)
        if result_count is not None:
            secondary.append(TraceChipPayload(label=result_count, tone="info"))

        return TraceEventPayload(
            status_label="已完成",
            status_tone="success",
            title=self._capability_title(capability.descriptor.id),
            detail=self._internal_finish_detail(capability.descriptor.id, observation),
            secondary=secondary,
        )

    def _capability_title(self, capability_id: str) -> str:
        return {
            "retrieval.minecraft_facts.lookup": "查询结构化事实",
            "retrieval.minecraft_semantics.search": "检索解释性资料",
            "skill.mina_capability_guide": "整理可见能力",
            "script.python_sandbox.execute": "准备脚本执行",
        }.get(capability_id, capability_id)

    def _kind_label(self, kind: str) -> str:
        return {
            "retrieval": "检索",
            "skill": "技能",
            "script": "脚本",
            "tool": "工具",
        }.get(kind, "内部")

    def _internal_start_detail(self, capability_id: str, arguments: dict[str, Any]) -> str:
        if capability_id == "retrieval.minecraft_facts.lookup":
            return "正在查询 SQLite 中的结构化事实。"
        if capability_id == "retrieval.minecraft_semantics.search":
            return "正在检索 SQLite FTS 中的解释性资料。"
        if capability_id == "skill.mina_capability_guide":
            return "正在整理当前会话可见的能力与限制。"
        if capability_id == "script.python_sandbox.execute":
            return "正在准备受预算控制的沙箱脚本。"
        return "Mina 正在执行一个内部步骤。"

    def _internal_finish_detail(self, capability_id: str, observation: dict[str, Any]) -> str:
        if capability_id == "retrieval.minecraft_facts.lookup":
            count = len(observation.get("results", []))
            return f"已完成结构化事实查询，命中 {count} 条结果。"
        if capability_id == "retrieval.minecraft_semantics.search":
            count = len(observation.get("results", []))
            return f"已完成解释性资料检索，找到 {count} 条相关资料。"
        if capability_id == "skill.mina_capability_guide":
            count = len(observation.get("summary", []))
            return f"已整理 {count} 个当前可见能力。"
        if capability_id == "script.python_sandbox.execute":
            count = len(observation.get("actions", []))
            return f"脚本准备完成，生成了 {count} 个结构化动作意图。"
        return "内部步骤已完成，结果已返回给 Mina。"

    def _observation_count_label(self, capability_id: str, observation: dict[str, Any]) -> str | None:
        if capability_id in {"retrieval.minecraft_facts.lookup", "retrieval.minecraft_semantics.search"}:
            return f"{len(observation.get('results', []))} 条结果"
        if capability_id == "skill.mina_capability_guide":
            return f"{len(observation.get('summary', []))} 个能力"
        if capability_id == "script.python_sandbox.execute":
            return f"{len(observation.get('actions', []))} 个动作"
        return None

    def _semantic_verification_guard(self, observations: list[dict[str, Any]]) -> dict[str, Any] | None:
        pending_payload: dict[str, Any] | None = None
        for observation in observations:
            source = observation.get("source")
            payload = observation.get("payload", {})
            if source == "retrieval.minecraft_semantics.search" and isinstance(payload, dict):
                if payload.get("verification_required"):
                    pending_payload = payload
            elif source == "retrieval.minecraft_facts.lookup" and pending_payload is not None:
                pending_payload = None
        if pending_payload is None:
            return None
        return {
            "reason": "semantic_results_require_fact_lookup",
            "required_capability_id": "retrieval.minecraft_facts.lookup",
            "fact_domains": pending_payload.get("fact_domains", []),
            "detail": "解释性检索结果涉及可核验的硬事实，必须先回查 SQLite 事实库。",
        }
