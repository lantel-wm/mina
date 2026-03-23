from __future__ import annotations

import json
import random
import uuid
from time import perf_counter
from typing import Any

from mina_agent.providers.openai_compatible import ProviderDecisionResult, ProviderError
from mina_agent.runtime.agent_services import AgentServices
from mina_agent.runtime.capability_registry import RuntimeState
from mina_agent.runtime.context_manager import ContextManager, ContextOverflowError
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.runtime.delegate_runtime import DelegateRuntime
from mina_agent.runtime.execution_manager import ExecutionManager
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.models import (
    ObservationRef,
    TurnState,
    WorkingMemory,
)
from mina_agent.runtime.task_manager import TaskManager
from mina_agent.schemas import (
    ActionRequestPayload,
    ActionResultPayload,
    TraceChipPayload,
    TraceEventPayload,
    TurnResponse,
    TurnResumeRequest,
    TurnStartRequest,
)


class TurnPipeline:
    _INTERNAL_START_STATUS_LABELS = (
        "我在看",
        "我来核对",
        "再看一下",
        "替你比对",
        "我在确认",
    )

    def __init__(self, services: AgentServices) -> None:
        self._services = services
        self._task_manager = services.task_manager or TaskManager(services.store)
        self._context_manager = services.context_manager or services.context_engine or ContextManager(
            services.settings,
            services.store,
            services.memory_policy,
        )
        self._deliberation_engine = services.deliberation_engine
        if self._deliberation_engine is None:
            if services.decision_engine is None:
                raise ValueError("AgentServices.deliberation_engine is required.")
            self._deliberation_engine = DeliberationEngine(services.decision_engine)  # type: ignore[arg-type]
        self._execution_manager = services.execution_manager or ExecutionManager(
            services.capability_registry,
            services.execution_orchestrator,
        )
        self._memory_manager = services.memory_manager or MemoryManager(services.store, services.memory_policy)
        self._delegate_runtime = services.delegate_runtime or DelegateRuntime(services.store, self._deliberation_engine)

    def _stage_bootstrap_start(self, request: TurnStartRequest) -> TurnState:
        self._services.store.ensure_session(request.session_ref, request.player.name, request.player.role)
        pending_confirmation = self._services.store.get_pending_confirmation(request.session_ref)
        task = self._task_manager.prepare_task(request, pending_confirmation)
        active_task_candidate = self._task_manager.load_active_task_candidate(
            request,
            pending_confirmation,
            current_task_id=task.task_id,
        )
        turn_state = TurnState(
            session_ref=request.session_ref,
            turn_id=request.turn_id,
            request=request.model_dump(),
            task=task,
            working_memory=WorkingMemory(
                primary_goal=task.goal,
                focus=task.goal,
                current_status="analyzing",
                next_best_step="Inspect, guide, or reply based on the current trigger.",
                companion_state={"stance": "present", "mode": "companion_first"},
            ),
            pending_confirmation=pending_confirmation,
            active_task_candidate=active_task_candidate,
        )
        self._services.store.create_turn(
            request.turn_id,
            request.session_ref,
            request.user_message,
            turn_state.to_runtime_dict(),
            task_id=task.task_id,
        )
        self._services.audit.record(
            "turn_started",
            {
                "turn_id": request.turn_id,
                "session_ref": request.session_ref,
                "task_id": task.task_id,
            },
        )
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
                "task": task.context_entry(),
            },
        )
        return turn_state

    def _stage_bootstrap_resume(
        self,
        continuation_id: str,
        request: TurnResumeRequest,
    ) -> tuple[Any, TurnState, TurnStartRequest, int]:
        continuation = self._services.store.get_continuation(continuation_id)
        if continuation is None:
            raise KeyError(f"Unknown continuation_id: {continuation_id}")

        turn_state = TurnState.model_validate(continuation.state)
        turn_request = TurnStartRequest.model_validate(turn_state.request)
        current_step = max(turn_state.step_index, 1)
        self._services.debug.record_event(
            continuation.turn_id,
            "turn_resumed",
            {
                "continuation_id": continuation_id,
                "action_result_count": len(request.action_results),
                "intent_ids": [result.intent_id for result in request.action_results],
                "statuses": [result.status for result in request.action_results],
                "task_id": turn_state.task.task_id,
            },
            step_index=current_step,
        )
        return continuation, turn_state, turn_request, current_step

    def _stage_context_assemble(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        capabilities: list[Any],
        current_step: int,
    ) -> Any:
        context_result = self._context_manager.build_messages(
            request=request,
            turn_state=turn_state,
            capability_descriptors=[capability.descriptor for capability in capabilities],
        )
        context_result = self._fit_context_to_budget(
            request=request,
            turn_state=turn_state,
            current_step=current_step,
            context_result=context_result,
        )
        self._services.debug.record_event(
            request.turn_id,
            "context_built",
            {
                "sections": context_result.sections,
                "message_stats": context_result.message_stats,
                "budget_report": context_result.budget_report,
                "composition": context_result.composition,
                "task_id": turn_state.task.task_id,
            },
            step_index=current_step,
        )
        self._services.debug.record_event(
            request.turn_id,
            "model_request",
            {
                "message_count": len(context_result.messages),
                "message_stats": context_result.message_stats,
                "messages": context_result.messages,
                "provider_input_buffer": self._deliberation_engine.debug_request_buffer(context_result.messages),
            },
            step_index=current_step,
        )
        return context_result

    def _fit_context_to_budget(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        current_step: int,
        context_result: Any,
    ) -> Any:
        current_result = context_result
        max_compaction_passes = 2
        for pass_index in range(0, max_compaction_passes + 1):
            token_estimate = self._deliberation_engine.estimate_prompt_tokens(current_result.messages)
            current_result = self._apply_token_estimate(
                current_result,
                token_estimate=token_estimate,
                compaction_passes=pass_index,
            )
            if bool(current_result.budget_report.get("within_budget")):
                return current_result
            if pass_index >= max_compaction_passes:
                raise ContextOverflowError(
                    budget_tokens=self._services.settings.context_token_budget,
                    used_tokens=int(current_result.budget_report.get("used_tokens", 0)),
                    protected_slots=list(current_result.protected_slots),
                )

            current_tokens = int(current_result.budget_report.get("used_tokens", 0))
            target_tokens = max(1024, int(self._services.settings.context_token_budget * 0.85))
            compaction_request = self._context_manager.build_compaction_request(
                current_result,
                current_tokens=current_tokens,
                target_tokens=target_tokens,
                pass_index=pass_index + 1,
            )
            if compaction_request is None:
                raise ContextOverflowError(
                    budget_tokens=self._services.settings.context_token_budget,
                    used_tokens=current_tokens,
                    protected_slots=list(current_result.protected_slots),
                )
            compact_messages = compaction_request.messages
            compact_request_stats = self._deliberation_engine.estimate_prompt_tokens(compact_messages)
            self._services.debug.record_event(
                request.turn_id,
                "context_compaction_requested",
                {
                    "pass_index": pass_index + 1,
                    "current_tokens": current_tokens,
                    "target_tokens": target_tokens,
                    "target_path": compaction_request.target.path,
                    "message_count": len(compact_messages),
                    "message_stats": self._message_stats_from_token_estimate(compact_messages, compact_request_stats),
                    "messages": compact_messages,
                    "provider_input_buffer": self._deliberation_engine.debug_request_buffer(compact_messages),
                },
                step_index=current_step,
            )
            compact_result = self._deliberation_engine.compact_target(
                compact_messages,
                expected_root_types=compaction_request.target.expected_root_types,
            )
            self._services.debug.record_event(
                request.turn_id,
                "context_compaction_finished",
                {
                    "pass_index": pass_index + 1,
                    "current_tokens": current_tokens,
                    "target_tokens": target_tokens,
                    "target_path": compaction_request.target.path,
                    "parse_status": compact_result.parse_status,
                    "raw_response_preview": compact_result.raw_response_preview,
                    "result": compact_result.value,
                },
                step_index=current_step,
            )
            current_result = self._context_manager.apply_compaction_target(
                current_result,
                target_path=compaction_request.target.path,
                replacement=compact_result.value,
                compaction_passes=pass_index + 1,
            )
        return current_result

    def _apply_token_estimate(
        self,
        context_result: Any,
        *,
        token_estimate: dict[str, Any],
        compaction_passes: int,
    ) -> Any:
        message_tokens = token_estimate.get("message_tokens")
        if not isinstance(message_tokens, list):
            message_tokens = []
        system_tokens = int(message_tokens[0]) if len(message_tokens) > 0 else 0
        user_tokens = int(message_tokens[1]) if len(message_tokens) > 1 else 0
        total_tokens = int(token_estimate.get("total_tokens") or (system_tokens + user_tokens))
        context_result.message_stats.update(
            {
                "encoding_name": token_estimate.get("encoding_name"),
                "system_tokens": system_tokens,
                "user_tokens": user_tokens,
                "total_tokens": total_tokens,
            }
        )
        context_result.budget_report = {
            "budget_tokens": self._services.settings.context_token_budget,
            "used_tokens": total_tokens,
            "compaction_passes": compaction_passes,
            "within_budget": total_tokens <= self._services.settings.context_token_budget,
        }
        return context_result

    def _message_stats_from_token_estimate(
        self,
        messages: list[dict[str, str]],
        token_estimate: dict[str, Any],
    ) -> dict[str, Any]:
        system_chars = len(messages[0]["content"]) if len(messages) > 0 else 0
        user_chars = len(messages[1]["content"]) if len(messages) > 1 else 0
        message_tokens = token_estimate.get("message_tokens")
        if not isinstance(message_tokens, list):
            message_tokens = []
        return {
            "message_count": len(messages),
            "system_chars": system_chars,
            "user_chars": user_chars,
            "total_chars": system_chars + user_chars,
            "encoding_name": token_estimate.get("encoding_name"),
            "system_tokens": int(message_tokens[0]) if len(message_tokens) > 0 else 0,
            "user_tokens": int(message_tokens[1]) if len(message_tokens) > 1 else 0,
            "total_tokens": int(token_estimate.get("total_tokens") or 0),
        }

    def _stage_deliberate(self, messages: list[dict[str, str]]) -> ProviderDecisionResult:
        return self._deliberation_engine.decide(messages)

    def _stage_finalize(
        self,
        turn_id: str,
        session_ref: str,
        final_reply: str,
        *,
        status: str,
        turn_state: TurnState,
        request: TurnStartRequest,
        pending_confirmation_resolved: str | None = None,
        preserve_task_status: bool = False,
    ) -> None:
        self._stage_finalize(
            turn_id,
            session_ref,
            final_reply,
            status=status,
            turn_state=turn_state,
            request=request,
            pending_confirmation_resolved=pending_confirmation_resolved,
            preserve_task_status=preserve_task_status,
        )

    def _bridge_budget_reply(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        trace_events: list[TraceEventPayload],
        step_index: int,
        reason: str,
    ) -> TurnResponse:
        return self._return_final_reply(
            request.turn_id,
            request.session_ref,
            reason,
            trace_events,
            status="completed",
            step_index=step_index,
            debug_payload={"reason": "bridge_budget_exhausted"},
            turn_state=turn_state,
            request=request,
        )

    def _emit_bridge_action_batch(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability: Any,
        action_request_payload: dict[str, Any],
        trace_events: list[TraceEventPayload],
        current_step: int,
        confirmation_resolution: str | None = None,
        source_capability_id: str | None = None,
    ) -> TurnResponse:
        if turn_state.bridge_action_count >= request.limits.max_bridge_actions_per_turn:
            return self._bridge_budget_reply(
                request=request,
                turn_state=turn_state,
                trace_events=trace_events,
                step_index=current_step,
                reason="Mina stopped because the bridge action budget was exhausted.",
            )
        if turn_state.continuation_depth >= request.limits.max_continuation_depth:
            return self._return_final_reply(
                request.turn_id,
                request.session_ref,
                "Mina stopped because the continuation depth limit was exhausted.",
                trace_events,
                status="completed",
                step_index=current_step,
                debug_payload={"reason": "continuation_depth_exhausted"},
                turn_state=turn_state,
                request=request,
            )

        continuation_id = action_request_payload.get("continuation_id") or str(uuid.uuid4())
        action_request_payload["continuation_id"] = continuation_id
        pending_payload = dict(action_request_payload)
        decision_capability_id = str(source_capability_id or capability.descriptor.id)
        self._record_capability_call(
            turn_state,
            capability_id=decision_capability_id,
            arguments=action_request_payload["arguments"],
        )
        if source_capability_id is not None:
            pending_payload["source_capability_id"] = source_capability_id
        turn_state.pending_action_batch = [pending_payload]
        turn_state.step_index = max(turn_state.step_index + 1, current_step)
        turn_state.continuation_depth += 1
        turn_state.bridge_action_count += 1
        turn_state.task.status = "in_progress"
        turn_state.task.requires_confirmation = False
        turn_state.working_memory.current_status = "awaiting_bridge_result"
        self._task_manager.sync_task(turn_state.task)
        self._services.store.put_continuation(
            continuation_id,
            request.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )

        payload = self._bridge_capability_finished_payload(
            capability,
            action_request_payload,
            "awaiting_bridge_result",
            current_step,
            turn_state.task.task_id,
            continuation_id=continuation_id,
        )
        if confirmation_resolution is not None:
            payload["confirmation_resolution"] = confirmation_resolution
        self._services.debug.record_event(
            request.turn_id,
            "capability_finished",
            payload,
            step_index=current_step,
        )
        return TurnResponse(
            type="action_request_batch",
            continuation_id=continuation_id,
            action_request_batch=[ActionRequestPayload.model_validate(action_request_payload)],
            trace_events=trace_events,
        )

    def _emit_progress_update(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        trace_events: list[TraceEventPayload],
        current_step: int,
        progress_reason: str,
    ) -> TurnResponse:
        continuation_id = str(uuid.uuid4())
        turn_state.pending_action_batch = []
        self._services.store.put_continuation(
            continuation_id,
            request.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        self._services.debug.record_event(
            request.turn_id,
            "turn_yielded",
            {
                "continuation_id": continuation_id,
                "reason": progress_reason,
                "step_index": turn_state.step_index,
                "task_id": turn_state.task.task_id,
                "trace_events": [event.model_dump() for event in trace_events],
            },
            step_index=current_step,
        )
        return TurnResponse(
            type="progress_update",
            continuation_id=continuation_id,
            trace_events=trace_events,
        )

    def start_turn(self, request: TurnStartRequest) -> TurnResponse:
        turn_state = self._stage_bootstrap_start(request)
        pending_confirmation = turn_state.pending_confirmation
        task = turn_state.task

        resolution = self._services.confirmation_resolver.resolve(
            user_message=request.user_message,
            pending_confirmation=pending_confirmation,
            task=task,
        )
        if resolution is not None:
            self._services.store.clear_pending_confirmation(request.session_ref)
            turn_state.pending_confirmation = None
            if resolution.task is not None:
                turn_state.task = resolution.task
                self._task_manager.sync_task(turn_state.task)
            if resolution.disposition == "confirmed" and resolution.action_payload is not None:
                action_payload = dict(resolution.action_payload)
                capabilities = self._services.capability_registry.resolve(request)
                capability = self._execution_manager.resolve_capability(capabilities, action_payload["capability_id"])
                if capability is not None and capability.handler_kind == "internal":
                    execution_response = self._execute_confirmed_internal_capability(
                        request=request,
                        turn_state=turn_state,
                        capability=capability,
                        action_payload=action_payload,
                    )
                    if execution_response is not None:
                        return execution_response
                    return self._advance(request, turn_state)
                if capability is None:
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        "确认后的计划引用了不可用的能力。",
                        [],
                        status="failed",
                        step_index=1,
                        debug_payload={"reason": "unknown_confirmed_capability"},
                        turn_state=turn_state,
                        request=request,
                    )
                return self._emit_bridge_action_batch(
                    request=request,
                    turn_state=turn_state,
                    capability=capability,
                    action_request_payload=action_payload,
                    trace_events=[
                        TraceEventPayload(
                            status_label="已确认",
                            status_tone="success",
                            title="继续执行计划",
                            detail=resolution.reply,
                            secondary=[TraceChipPayload(label="确认已处理", tone="success")],
                        )
                    ],
                    current_step=1,
                    confirmation_resolution="confirmed",
                )
            if resolution.disposition == "rejected":
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    resolution.reply or "这一步我先停下了。",
                    [],
                    status="completed",
                    step_index=0,
                    pending_confirmation_resolved="rejected",
                    turn_state=turn_state,
                    request=request,
                )
            turn_state.working_memory.current_status = "replanning"
            turn_state.runtime_notes.append(f"Pending confirmation was modified by the user: {request.user_message}")

        return self._advance(request, turn_state)

    def resume_turn(self, continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        continuation, turn_state, turn_request, current_step = self._stage_bootstrap_resume(continuation_id, request)

        if request.action_results:
            for result in request.action_results:
                capability_id = self._lookup_capability_id(turn_state, result.intent_id)
                action_payload = self._lookup_action_payload(turn_state, result.intent_id)
                observation = self._execution_manager.register_observation(
                    turn_state,
                    source=capability_id,
                    payload=result.observations,
                    kind="bridge_result",
                )
                if action_payload is not None:
                    self._record_capability_observation(
                        turn_state,
                        capability_id=capability_id,
                        arguments=action_payload.get("arguments", {}),
                        observations=result.observations,
                    )
                payload = {
                    "intent_id": result.intent_id,
                    "capability_id": capability_id,
                    "risk_class": self._lookup_risk_class(turn_state, result.intent_id),
                    "status": result.status,
                    "preconditions_passed": result.preconditions_passed,
                    "side_effect_summary": result.side_effect_summary,
                    "timing_ms": result.timing_ms,
                    "state_fingerprint": result.state_fingerprint,
                    "error_message": result.error_message,
                    "observations": result.observations,
                    "task_id": turn_state.task.task_id,
                    "artifact_ref": observation.artifact_ref.context_ref() if observation.artifact_ref else None,
                }
                self._services.store.log_execution_record(
                    turn_id=continuation.turn_id,
                    intent_id=result.intent_id,
                    capability_id=capability_id,
                    risk_class=payload["risk_class"],
                    status=result.status,
                    observations=result.observations,
                    side_effect_summary=result.side_effect_summary,
                    timing_ms=result.timing_ms,
                    task_id=turn_state.task.task_id,
                    state_fingerprint=result.state_fingerprint,
                    artifact_refs=[observation.artifact_ref.model_dump()] if observation.artifact_ref else [],
                )
                self._services.store.log_step_event(continuation.turn_id, current_step, "bridge_result", payload)
                self._services.debug.record_event(
                    continuation.turn_id,
                    "bridge_result",
                    payload,
                    step_index=current_step,
                )
            turn_state.working_memory.current_status = "bridge_result_received"
        else:
            turn_state.working_memory.current_status = "progress_update_resumed"
        turn_state.pending_action_batch = []
        self._services.store.clear_continuation(continuation.turn_id, turn_state.to_runtime_dict(), task_id=turn_state.task.task_id)
        self._services.audit.record("turn_resumed", {"turn_id": continuation.turn_id, "continuation_id": continuation_id})
        return self._advance(turn_request, turn_state)

    def _advance(self, request: TurnStartRequest, turn_state: TurnState) -> TurnResponse:
        capabilities = self._services.capability_registry.resolve(request)
        runtime_state = RuntimeState(
            request=request,
            turn_state=turn_state,
            pending_confirmation=turn_state.pending_confirmation,
        )
        trace_events: list[TraceEventPayload] = []
        self._services.debug.record_event(
            request.turn_id,
            "capabilities_resolved",
            self._capabilities_resolved_payload(capabilities),
        )

        while turn_state.step_index < min(request.limits.max_agent_steps, self._services.settings.max_agent_steps):
            current_step = turn_state.step_index + 1
            try:
                context_result = self._stage_context_assemble(request, turn_state, capabilities, current_step)
            except ContextOverflowError as exc:
                overflow_payload = {
                    "budget_tokens": exc.budget_tokens,
                    "used_tokens": exc.used_tokens,
                    "protected_slots": exc.protected_slots,
                    "task_id": turn_state.task.task_id,
                }
                self._services.store.log_step_event(
                    request.turn_id,
                    current_step,
                    "context_overflow",
                    overflow_payload,
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "context_overflow",
                    overflow_payload,
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    "这一轮需要的完整上下文超过了当前硬性预算，我先停下，避免在缺失关键信息的情况下继续推理。",
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={"reason": "context_overflow", **overflow_payload},
                    turn_state=turn_state,
                    request=request,
                )
            except ProviderError as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "context_compaction_failed",
                    {
                        "parse_status": exc.parse_status,
                        "raw_response_preview": exc.raw_response_preview,
                        "error": str(exc),
                        "task_id": turn_state.task.task_id,
                    },
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    f"上下文压缩失败了，我这一轮先停下。{exc}",
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={"reason": "context_compaction_failed", "error": str(exc)},
                    turn_state=turn_state,
                    request=request,
                )
            except Exception as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "context_compaction_failed",
                    {
                        "parse_status": "unexpected_context_compaction_error",
                        "raw_response_preview": str(exc),
                        "error": str(exc),
                        "task_id": turn_state.task.task_id,
                    },
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    f"上下文压缩流程出错了，我这一轮先停下。{exc}",
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={"reason": "unexpected_context_compaction_error", "error": str(exc)},
                    turn_state=turn_state,
                    request=request,
                )
            try:
                provider_result = self._stage_deliberate(context_result.messages)
            except ProviderError as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "model_response",
                    {
                        "latency_ms": exc.latency_ms,
                        "parse_status": exc.parse_status,
                        "raw_response_preview": exc.raw_response_preview,
                        "error": str(exc),
                    },
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    f"Mina agent service is online, but no model decision is available: {exc}",
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={
                        "reason": "provider_error",
                        "parse_status": exc.parse_status,
                        "error": str(exc),
                    },
                    turn_state=turn_state,
                    request=request,
                )
            except Exception as exc:
                self._services.debug.record_event(
                    request.turn_id,
                    "model_response",
                    {
                        "latency_ms": 0,
                        "parse_status": "unexpected_provider_error",
                        "raw_response_preview": str(exc),
                        "error": str(exc),
                    },
                    step_index=current_step,
                )
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    f"Mina agent service is online, but no model decision is available: {exc}",
                    trace_events,
                    status="failed",
                    step_index=current_step,
                    debug_payload={"reason": "unexpected_provider_error", "error": str(exc)},
                    turn_state=turn_state,
                    request=request,
                )

            self._services.debug.record_event(
                request.turn_id,
                "model_response",
                self._model_response_payload(provider_result),
                step_index=current_step,
            )
            decision = provider_result.decision
            self._services.store.log_step_event(request.turn_id, turn_state.step_index, "model_decision", decision.model_dump())
            self._services.audit.record("model_decision", {"turn_id": request.turn_id, "decision": decision.model_dump()})
            self._services.debug.record_event(
                request.turn_id,
                "model_decision",
                {**decision.model_dump(), "task_id": turn_state.task.task_id},
                step_index=current_step,
            )
            selection_payload = self._task_manager.apply_task_selection(request.turn_id, turn_state, decision)
            if selection_payload is not None:
                self._services.store.update_turn_state(
                    request.turn_id,
                    turn_state.to_runtime_dict(),
                    task_id=turn_state.task.task_id,
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "task_selected",
                    selection_payload,
                    step_index=turn_state.step_index,
                )
            self._task_manager.classify_task_patch(turn_state, decision)

            if decision.intent in {"reply", "guide"} or decision.mode == "final_reply":
                final_reply = decision.final_reply or "I do not have a better response yet."
                return self._return_final_reply(
                    request.turn_id,
                    request.session_ref,
                    final_reply,
                    trace_events,
                    status="completed",
                    step_index=current_step,
                    turn_state=turn_state,
                    request=request,
                )

            capability_request = decision.capability_request
            if decision.intent == "await_confirmation":
                if capability_request is None or not capability_request.capability_id:
                    detail = (
                        decision.confirmation_request.effect_summary
                        if decision.confirmation_request is not None
                        else "这一步要先确认，不过我还没把要执行的动作整理成可继续的计划。"
                    )
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        detail,
                        trace_events
                        + [
                            TraceEventPayload(
                                status_label="待确认",
                                status_tone="warning",
                                title="需要先确认",
                                detail=detail,
                                secondary=[TraceChipPayload(label=f"第 {current_step} 步", tone="muted")],
                            )
                        ],
                        status="completed",
                        step_index=current_step,
                        preserve_task_status=True,
                        turn_state=turn_state,
                        request=request,
                    )

                capability = self._execution_manager.resolve_capability(capabilities, capability_request.capability_id)
                if capability is None or capability.handler_kind != "bridge":
                    detail = (
                        decision.confirmation_request.effect_summary
                        if decision.confirmation_request is not None
                        else capability_request.effect_summary
                        or "这一步要先确认。"
                    )
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        f"{detail} 不过我现在还不能把这一步安全地挂起继续执行。",
                        trace_events
                        + [
                            TraceEventPayload(
                                status_label="待确认",
                                status_tone="warning",
                                title="需要先确认",
                                detail=detail,
                                secondary=[TraceChipPayload(label=f"第 {current_step} 步", tone="muted")],
                            )
                        ],
                        status="completed",
                        step_index=current_step,
                        preserve_task_status=True,
                        turn_state=turn_state,
                        request=request,
                    )

                resolved_arguments = self._execution_manager.resolve_arguments(
                    turn_state,
                    capability,
                    capability_request.arguments,
                )
                continuation_id = str(uuid.uuid4())
                action_request_payload = self._execution_manager.bridge_action_request(
                    capability,
                    resolved_arguments,
                    (
                        decision.confirmation_request.effect_summary
                        if decision.confirmation_request is not None
                        else capability_request.effect_summary
                    ),
                    True,
                )
                action_request_payload["continuation_id"] = continuation_id
                return self._queue_pending_confirmation(
                    request=request,
                    turn_state=turn_state,
                    trace_events=trace_events,
                    current_step=current_step,
                    capability=capability,
                    action_request_payload=action_request_payload,
                )

            if decision.intent in {"delegate_explore", "delegate_plan"} and decision.delegate_request is not None:
                delegate_blocked, delegate_response = self._handle_repeated_delegate(
                    request=request,
                    turn_state=turn_state,
                    delegate_request=decision.delegate_request.model_dump(),
                    current_step=current_step,
                )
                if delegate_response is not None:
                    return delegate_response
                if delegate_blocked:
                    continue
                delegate_result = self._delegate_runtime.run(decision.delegate_request, turn_state)
                self._task_manager.apply_task_patch(turn_state, delegate_result.task_patch)
                observation = self._execution_manager.register_observation(
                    turn_state,
                    source=f"agent.{delegate_result.role}.delegate",
                    payload={
                        "summary": delegate_result.summary.summary,
                        "delegate_result": delegate_result.model_dump(),
                        "artifact_refs": delegate_result.artifact_refs,
                        "task_patch": delegate_result.task_patch,
                    },
                    kind="delegate_result",
                )
                turn_state.delegate_history.append(delegate_result.model_dump())
                turn_state.last_delegate_role = delegate_result.role
                turn_state.last_delegate_fact_revision = self._non_delegate_fact_revision(turn_state)
                turn_state.step_index += 1
                turn_state.working_memory.current_status = f"delegate_{delegate_result.role}_completed"
                self._task_manager.sync_task(turn_state.task)
                self._services.store.update_turn_state(
                    request.turn_id,
                    turn_state.to_runtime_dict(),
                    task_id=turn_state.task.task_id,
                )
                self._services.store.log_step_event(
                    request.turn_id,
                    turn_state.step_index,
                    "delegate_result",
                    delegate_result.model_dump(),
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "delegate_result",
                    {"delegate": delegate_result.model_dump(), "observation": observation.context_entry()},
                    step_index=current_step,
                )
                trace_events.append(self._internal_finish_trace_stub(delegate_result.summary.summary, current_step))
                if self._services.settings.yield_after_internal_steps:
                    return self._emit_progress_update(
                        request=request,
                        turn_state=turn_state,
                        trace_events=trace_events,
                        current_step=current_step,
                        progress_reason=f"delegate_{delegate_result.role}_completed",
                    )
                continue

            capability = self._execution_manager.resolve_capability(
                capabilities,
                capability_request.capability_id if capability_request is not None else (decision.capability_id or ""),
            )
            if capability is None:
                unknown_capability_id = capability_request.capability_id if capability_request is not None else (decision.capability_id or "")
                unknown_attempts = self._record_unknown_capability_attempt(
                    request=request,
                    turn_state=turn_state,
                    capability_id=unknown_capability_id,
                    current_step=current_step,
                )
                if unknown_attempts >= 2:
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        "我不会执行不存在的能力。这一步先停下，我会改用当前确实可见的能力或直接回答。",
                        trace_events,
                        status="failed",
                        step_index=current_step,
                        debug_payload={
                            "reason": "unknown_capability",
                            "capability_id": unknown_capability_id,
                            "unknown_capability_attempts": unknown_attempts,
                        },
                        turn_state=turn_state,
                        request=request,
                    )
                continue

            resolved_arguments = self._execution_manager.resolve_arguments(
                turn_state,
                capability,
                capability_request.arguments if capability_request is not None else decision.arguments,
            )
            capability_blocked, capability_response = self._handle_repeated_capability(
                request=request,
                turn_state=turn_state,
                capability=capability,
                resolved_arguments=resolved_arguments,
                current_step=current_step,
            )
            if capability_response is not None:
                return capability_response
            if capability_blocked:
                continue
            self._services.debug.record_event(
                request.turn_id,
                "capability_started",
                self._capability_started_payload(capability, decision, resolved_arguments, turn_state.task.task_id),
                step_index=current_step,
            )

            requires_confirmation = (
                getattr(capability_request, "requires_confirmation", False)
                or getattr(decision, "requires_confirmation", False)
                or capability.descriptor.requires_confirmation
            )
            effect_summary = (
                getattr(capability_request, "effect_summary", None)
                or getattr(decision, "effect_summary", None)
                or capability.descriptor.description
            )
            if requires_confirmation:
                action_request_payload = self._execution_manager.bridge_action_request(
                    capability,
                    resolved_arguments,
                    effect_summary,
                    True,
                )
                if capability.handler_kind == "bridge":
                    action_request_payload["continuation_id"] = str(uuid.uuid4())
                return self._queue_pending_confirmation(
                    request=request,
                    turn_state=turn_state,
                    trace_events=trace_events,
                    current_step=current_step,
                    capability=capability,
                    action_request_payload=action_request_payload,
                )

            if capability.handler_kind == "internal":
                self._record_capability_call(
                    turn_state,
                    capability_id=capability.descriptor.id,
                    arguments=resolved_arguments,
                )
                trace_events.append(self._internal_start_trace(capability, resolved_arguments, current_step))
                started = perf_counter()
                try:
                    observation_payload = self._execution_manager.execute_internal(
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
                            "task_id": turn_state.task.task_id,
                        },
                        step_index=current_step,
                    )
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        f"Mina internal capability failed: {exc}",
                        trace_events,
                        status="failed",
                        step_index=current_step,
                        debug_payload={
                            "reason": "internal_capability_error",
                            "capability_id": capability.descriptor.id,
                            "error": str(exc),
                        },
                        turn_state=turn_state,
                        request=request,
                    )

                latency_ms = int((perf_counter() - started) * 1000)
                observation = self._execution_manager.register_observation(
                    turn_state,
                    source=capability.descriptor.id,
                    payload=observation_payload,
                    kind="internal_capability",
                )
                self._record_capability_observation(
                    turn_state,
                    capability_id=capability.descriptor.id,
                    arguments=resolved_arguments,
                    observations=observation_payload,
                )
                self._task_manager.apply_task_patch(turn_state, observation_payload.get("task_patch"))
                turn_state.step_index += 1
                turn_state.working_memory.current_status = "internal_capability_completed"
                turn_state.working_memory.next_best_step = "Continue reasoning with the new observation."
                self._task_manager.sync_task(turn_state.task)
                self._services.store.update_turn_state(
                    request.turn_id,
                    turn_state.to_runtime_dict(),
                    task_id=turn_state.task.task_id,
                )
                self._services.store.log_step_event(
                    request.turn_id,
                    turn_state.step_index,
                    "internal_capability",
                    observation.model_dump(),
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "capability_finished",
                    self._internal_capability_finished_payload(capability, observation, latency_ms, turn_state.task.task_id),
                    step_index=current_step,
                )
                trace_events.append(self._internal_finish_trace(capability, observation, turn_state.step_index))
                if self._services.settings.yield_after_internal_steps:
                    return self._emit_progress_update(
                        request=request,
                        turn_state=turn_state,
                        trace_events=trace_events,
                        current_step=current_step,
                        progress_reason=f"internal_capability:{capability.descriptor.id}",
                    )
                continue

            if capability.handler_kind == "bridge_proxy":
                self._record_capability_call(
                    turn_state,
                    capability_id=capability.descriptor.id,
                    arguments=resolved_arguments,
                )
                trace_events.append(self._internal_start_trace(capability, resolved_arguments, current_step))
                started = perf_counter()
                try:
                    proxy_payload = self._execution_manager.execute_internal(
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
                            "task_id": turn_state.task.task_id,
                        },
                        step_index=current_step,
                    )
                    return self._return_final_reply(
                        request.turn_id,
                        request.session_ref,
                        f"Mina bridge proxy capability failed: {exc}",
                        trace_events,
                        status="failed",
                        step_index=current_step,
                        debug_payload={
                            "reason": "bridge_proxy_capability_error",
                            "capability_id": capability.descriptor.id,
                            "error": str(exc),
                        },
                        turn_state=turn_state,
                        request=request,
                    )

                latency_ms = int((perf_counter() - started) * 1000)
                if proxy_payload.get("_proxy_mode") == "bridge":
                    action_request_payload = self._execution_manager.bridge_action_request(
                        capability,
                        proxy_payload.get("arguments", resolved_arguments),
                        proxy_payload.get("effect_summary") or effect_summary,
                        False,
                    )
                    return self._emit_bridge_action_batch(
                        request=request,
                        turn_state=turn_state,
                        capability=capability,
                        action_request_payload=action_request_payload,
                        trace_events=trace_events,
                        current_step=current_step,
                        source_capability_id=capability.descriptor.id,
                    )

                observation_payload = proxy_payload.get("payload")
                if not isinstance(observation_payload, dict):
                    observation_payload = {
                        key: value
                        for key, value in proxy_payload.items()
                        if not str(key).startswith("_")
                    }
                observation = self._execution_manager.register_observation(
                    turn_state,
                    source=capability.descriptor.id,
                    payload=observation_payload,
                    kind="bridge_proxy_capability",
                )
                self._record_capability_observation(
                    turn_state,
                    capability_id=capability.descriptor.id,
                    arguments=resolved_arguments,
                    observations=observation_payload,
                )
                self._task_manager.apply_task_patch(turn_state, observation_payload.get("task_patch"))
                turn_state.step_index += 1
                turn_state.working_memory.current_status = "bridge_proxy_capability_completed"
                turn_state.working_memory.next_best_step = "Continue reasoning with the refreshed or ambient world observation."
                self._task_manager.sync_task(turn_state.task)
                self._services.store.update_turn_state(
                    request.turn_id,
                    turn_state.to_runtime_dict(),
                    task_id=turn_state.task.task_id,
                )
                self._services.store.log_step_event(
                    request.turn_id,
                    turn_state.step_index,
                    "bridge_proxy_capability",
                    observation.model_dump(),
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "capability_finished",
                    self._internal_capability_finished_payload(capability, observation, latency_ms, turn_state.task.task_id),
                    step_index=current_step,
                )
                trace_events.append(self._internal_finish_trace(capability, observation, turn_state.step_index))
                if self._services.settings.yield_after_internal_steps:
                    return self._emit_progress_update(
                        request=request,
                        turn_state=turn_state,
                        trace_events=trace_events,
                        current_step=current_step,
                        progress_reason=f"bridge_proxy_capability:{capability.descriptor.id}",
                    )
                continue

            action_request_payload = self._execution_manager.bridge_action_request(
                capability,
                resolved_arguments,
                effect_summary,
                False,
            )
            return self._emit_bridge_action_batch(
                request=request,
                turn_state=turn_state,
                capability=capability,
                action_request_payload=action_request_payload,
                trace_events=trace_events,
                current_step=current_step,
            )

        return self._return_final_reply(
            request.turn_id,
            request.session_ref,
            "Mina stopped because the configured step budget was exhausted.",
            trace_events,
            status="completed",
            step_index=turn_state.step_index,
            debug_payload={"reason": "step_budget_exhausted"},
            turn_state=turn_state,
            request=request,
        )

    def _prepare_task(self, request: TurnStartRequest, pending_confirmation: dict[str, Any] | None) -> TaskState:
        return self._task_manager.prepare_task(request, pending_confirmation)

    def _load_active_task_candidate(
        self,
        request: TurnStartRequest,
        pending_confirmation: dict[str, Any] | None,
        *,
        current_task_id: str | None = None,
    ) -> TaskState | None:
        return self._task_manager.load_active_task_candidate(
            request,
            pending_confirmation,
            current_task_id=current_task_id,
        )

    def _task_state_from_record(self, record: dict[str, Any]) -> TaskState:
        return self._task_manager.task_state_from_record(record)

    def _sync_task(self, task: TaskState) -> None:
        self._task_manager.sync_task(task)

    def _apply_task_patch(self, turn_state: TurnState, patch: dict[str, Any] | None) -> None:
        self._task_manager.apply_task_patch(turn_state, patch)

    def _record_memory_writes(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        *,
        final_reply: str,
        status: str,
        pending_confirmation_resolved: str | None = None,
    ) -> None:
        self._memory_manager.record_turn_memories(
            request,
            turn_state,
            final_reply=final_reply,
            status=status,
            pending_confirmation_resolved=pending_confirmation_resolved,
        )

    def _apply_task_selection(self, turn_id: str, turn_state: TurnState, decision: Any) -> None:
        payload = self._task_manager.apply_task_selection(turn_id, turn_state, decision)
        if payload is None:
            return
        self._services.store.update_turn_state(
            turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        self._services.debug.record_event(
            turn_id,
            "task_selected",
            payload,
            step_index=turn_state.step_index,
        )

    def _return_final_reply(
        self,
        turn_id: str,
        session_ref: str,
        final_reply: str,
        trace_events: list[TraceEventPayload],
        *,
        status: str,
        step_index: int,
        debug_payload: dict[str, Any] | None = None,
        pending_confirmation_id: str | None = None,
        pending_confirmation_effect_summary: str | None = None,
        pending_confirmation_resolved: str | None = None,
        preserve_task_status: bool = False,
        turn_state: TurnState,
        request: TurnStartRequest,
    ) -> TurnResponse:
        self._finalize(
            turn_id,
            session_ref,
            final_reply,
            status=status,
            turn_state=turn_state,
            request=request,
            pending_confirmation_resolved=pending_confirmation_resolved,
            preserve_task_status=preserve_task_status,
        )
        payload = {"final_reply": final_reply, "task_id": turn_state.task.task_id}
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
            pending_confirmation_effect_summary=pending_confirmation_effect_summary,
            trace_events=trace_events,
        )

    def _finalize(
        self,
        turn_id: str,
        session_ref: str,
        final_reply: str,
        *,
        status: str,
        turn_state: TurnState,
        request: TurnStartRequest,
        pending_confirmation_resolved: str | None = None,
        preserve_task_status: bool = False,
    ) -> None:
        if (
            status == "completed"
            and not preserve_task_status
            and turn_state.task.status not in {"completed", "canceled"}
        ):
            turn_state.task.status = "completed"
        if status == "failed":
            turn_state.task.status = "failed"
        self._task_manager.sync_task(turn_state.task)
        self._services.store.finish_turn(turn_id, final_reply, status=status)
        self._record_memory_writes(
            request,
            turn_state,
            final_reply=final_reply,
            status=status,
            pending_confirmation_resolved=pending_confirmation_resolved,
        )
        self._services.audit.record(
            "turn_completed" if status == "completed" else "turn_failed",
            {"turn_id": turn_id, "final_reply": final_reply, "task_id": turn_state.task.task_id},
        )

    def _lookup_capability_id(self, turn_state: TurnState, intent_id: str) -> str:
        for payload in turn_state.pending_action_batch:
            if payload["intent_id"] == intent_id:
                return str(payload.get("source_capability_id") or payload["capability_id"])
        return "unknown"

    def _lookup_action_payload(self, turn_state: TurnState, intent_id: str) -> dict[str, Any] | None:
        for payload in turn_state.pending_action_batch:
            if payload["intent_id"] == intent_id:
                return payload
        return None

    def _lookup_risk_class(self, turn_state: TurnState, intent_id: str) -> str:
        for payload in turn_state.pending_action_batch:
            if payload["intent_id"] == intent_id:
                return payload["risk_class"]
        return "read_only"

    def _non_delegate_fact_revision(self, turn_state: TurnState) -> int:
        return sum(1 for observation in turn_state.observations if not observation.source.startswith("agent."))

    def _capability_fingerprint(self, capability_id: str, arguments: dict[str, Any]) -> str:
        return f"{capability_id}::{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"

    def _record_capability_call(
        self,
        turn_state: TurnState,
        *,
        capability_id: str,
        arguments: dict[str, Any],
    ) -> str:
        fingerprint = self._capability_fingerprint(capability_id, arguments)
        turn_state.capability_call_counts[fingerprint] = turn_state.capability_call_counts.get(fingerprint, 0) + 1
        return fingerprint

    def _record_capability_observation(
        self,
        turn_state: TurnState,
        *,
        capability_id: str,
        arguments: dict[str, Any],
        observations: dict[str, Any],
    ) -> None:
        fingerprint = self._capability_fingerprint(capability_id, arguments)
        turn_state.capability_latest_observations[fingerprint] = dict(observations)

    def _capability_observation_is_ambiguous(self, capability_id: str, observations: dict[str, Any] | None) -> bool:
        if not isinstance(observations, dict) or not observations:
            return True
        if capability_id == "game.target_block.read":
            if observations.get("target_found") is False:
                return True
            if observations.get("target_found") is True:
                return False
        if isinstance(observations.get("summary"), str) and observations["summary"].strip():
            return False
        if isinstance(observations.get("message"), str) and observations["message"].strip():
            return False
        if isinstance(observations.get("block_name"), str) and observations["block_name"].strip():
            return False
        if isinstance(observations.get("block_id"), str) and observations["block_id"].strip():
            return False
        results = observations.get("results")
        if isinstance(results, list) and results:
            return False
        return True

    def _handle_repeated_capability(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability: Any,
        resolved_arguments: dict[str, Any],
        current_step: int,
    ) -> tuple[bool, TurnResponse | None]:
        capability_id = capability.descriptor.id
        fingerprint = self._capability_fingerprint(capability_id, resolved_arguments)
        prior_count = turn_state.capability_call_counts.get(fingerprint, 0)
        latest_observation = turn_state.capability_latest_observations.get(fingerprint)
        if prior_count <= 0:
            return False, None
        allow_one_retry = self._capability_observation_is_ambiguous(capability_id, latest_observation)
        if allow_one_retry and prior_count == 1:
            return False, None

        loop_hits = turn_state.capability_replan_counts.get(fingerprint, 0) + 1
        turn_state.capability_replan_counts[fingerprint] = loop_hits
        note = (
            f"Repeated capability blocked: {capability_id} with identical resolved arguments. "
            "Use the fresh observation you already have, change strategy, or reply directly."
        )
        turn_state.runtime_notes.append(note)

        if loop_hits == 1:
            turn_state.step_index += 1
            turn_state.working_memory.current_status = "replanning_after_repeated_capability"
            turn_state.working_memory.next_best_step = "Answer from the existing observation or choose a different capability."
            turn_state.working_memory.open_loops = [note]
            self._task_manager.sync_task(turn_state.task)
            self._services.store.update_turn_state(
                request.turn_id,
                turn_state.to_runtime_dict(),
                task_id=turn_state.task.task_id,
            )
            payload = {
                "reason": "repeated_capability_same_fingerprint",
                "capability_id": capability_id,
                "arguments": resolved_arguments,
                "task_id": turn_state.task.task_id,
                "runtime_note": note,
                "loop_hits": loop_hits,
            }
            self._services.store.log_step_event(
                request.turn_id,
                turn_state.step_index,
                "capability_repeat_rejected",
                payload,
            )
            self._services.debug.record_event(
                request.turn_id,
                "capability_rejected",
                payload,
                step_index=current_step,
            )
            return True, None

        return True, self._return_final_reply(
            request.turn_id,
            request.session_ref,
            "我已经拒绝了重复的同一读取请求，但模型仍然没有改用已有观察或换一种方式。这一轮先停下，避免继续空转。",
            [],
            status="failed",
            step_index=current_step,
            debug_payload={
                "reason": "repeated_capability_loop_guard",
                "capability_id": capability_id,
                "arguments": resolved_arguments,
                "loop_hits": loop_hits,
            },
            turn_state=turn_state,
            request=request,
        )

    def _handle_repeated_delegate(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        delegate_request: dict[str, Any],
        current_step: int,
    ) -> tuple[bool, TurnResponse | None]:
        role = str(delegate_request.get("role") or "").strip()
        if not role:
            return False, None
        current_fact_revision = self._non_delegate_fact_revision(turn_state)
        if turn_state.last_delegate_role != role or current_fact_revision != turn_state.last_delegate_fact_revision:
            return False, None

        loop_hits = turn_state.delegate_replan_counts.get(role, 0) + 1
        turn_state.delegate_replan_counts[role] = loop_hits
        note = (
            f"Repeated delegate blocked: {role} was requested again without new live facts. "
            "Use the delegate result you already have or switch to a visible read capability."
        )
        turn_state.runtime_notes.append(note)

        if loop_hits == 1:
            turn_state.step_index += 1
            turn_state.working_memory.current_status = "replanning_after_repeated_delegate"
            turn_state.working_memory.next_best_step = "Use the existing delegate result or inspect live state directly."
            turn_state.working_memory.open_loops = [note]
            self._task_manager.sync_task(turn_state.task)
            self._services.store.update_turn_state(
                request.turn_id,
                turn_state.to_runtime_dict(),
                task_id=turn_state.task.task_id,
            )
            payload = {
                "reason": "repeated_delegate_without_new_facts",
                "delegate_role": role,
                "task_id": turn_state.task.task_id,
                "runtime_note": note,
                "loop_hits": loop_hits,
            }
            self._services.store.log_step_event(
                request.turn_id,
                turn_state.step_index,
                "delegate_repeat_rejected",
                payload,
            )
            self._services.debug.record_event(
                request.turn_id,
                "delegate_rejected",
                payload,
                step_index=current_step,
            )
            return True, None

        final_reply = (
            "我已经拒绝了没有新事实支撑的重复委托，但模型仍然没有改用现有结果或换一种办法。"
            " 这一轮先停下，避免继续空转。"
        )
        return True, self._return_final_reply(
            request.turn_id,
            request.session_ref,
            final_reply,
            [],
            status="failed",
            step_index=current_step,
            debug_payload={
                "reason": "repeated_delegate_loop_guard",
                "delegate_role": role,
                "loop_hits": loop_hits,
            },
            turn_state=turn_state,
            request=request,
        )

    def _record_unknown_capability_attempt(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability_id: str,
        current_step: int,
    ) -> int:
        normalized_id = str(capability_id or "").strip() or "<empty>"
        note = (
            f"Unknown capability requested: {normalized_id}. "
            "Use an exact id from capability_brief or reply without executing a capability."
        )
        turn_state.runtime_notes.append(note)
        turn_state.step_index += 1
        turn_state.working_memory.current_status = "replanning_after_unknown_capability"
        turn_state.working_memory.next_best_step = "Choose an exact id from capability_brief or answer without a capability."
        turn_state.working_memory.open_loops = [note]
        self._task_manager.sync_task(turn_state.task)
        self._services.store.update_turn_state(
            request.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        self._services.store.log_step_event(
            request.turn_id,
            turn_state.step_index,
            "unknown_capability_rejected",
            {
                "capability_id": normalized_id,
                "note": note,
                "task_id": turn_state.task.task_id,
            },
        )
        self._services.debug.record_event(
            request.turn_id,
            "capability_rejected",
            {
                "reason": "unknown_capability",
                "capability_id": normalized_id,
                "task_id": turn_state.task.task_id,
                "runtime_note": note,
                "step_index": turn_state.step_index,
            },
            step_index=current_step,
        )
        return sum(1 for runtime_note in turn_state.runtime_notes if runtime_note.startswith("Unknown capability requested:"))

    def _execute_confirmed_internal_capability(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability: Any,
        action_payload: dict[str, Any],
    ) -> TurnResponse | None:
        resolved_arguments = self._execution_manager.resolve_arguments(
            turn_state,
            capability,
            dict(action_payload.get("arguments", {})),
        )
        runtime_state = RuntimeState(
            request=request,
            turn_state=turn_state,
            pending_confirmation=None,
        )
        self._services.debug.record_event(
            request.turn_id,
            "capability_started",
            {
                "capability_id": capability.descriptor.id,
                "handler_kind": capability.handler_kind,
                "kind": capability.descriptor.kind,
                "risk_class": capability.descriptor.risk_class,
                "execution_mode": capability.descriptor.execution_mode,
                "requires_confirmation": True,
                "effect_summary": action_payload.get("effect_summary") or capability.descriptor.description,
                "arguments": resolved_arguments,
                "task_id": turn_state.task.task_id,
                "confirmation_resolution": "confirmed",
            },
            step_index=1,
        )
        started = perf_counter()
        try:
            observation_payload = self._execution_manager.execute_internal(
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
                    "task_id": turn_state.task.task_id,
                    "confirmation_resolution": "confirmed",
                },
                step_index=1,
            )
            return self._return_final_reply(
                request.turn_id,
                request.session_ref,
                f"Mina internal capability failed: {exc}",
                [],
                status="failed",
                step_index=1,
                debug_payload={
                    "reason": "internal_capability_error",
                    "capability_id": capability.descriptor.id,
                    "error": str(exc),
                    "confirmation_resolution": "confirmed",
                },
                turn_state=turn_state,
                request=request,
            )

        latency_ms = int((perf_counter() - started) * 1000)
        observation = self._execution_manager.register_observation(
            turn_state,
            source=capability.descriptor.id,
            payload=observation_payload,
            kind="internal_capability",
        )
        self._task_manager.apply_task_patch(turn_state, observation_payload.get("task_patch"))
        turn_state.step_index = max(turn_state.step_index, 1)
        turn_state.working_memory.current_status = "internal_capability_completed"
        turn_state.working_memory.next_best_step = "Continue reasoning with the new observation."
        self._task_manager.sync_task(turn_state.task)
        self._services.store.update_turn_state(
            request.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        self._services.store.log_step_event(
            request.turn_id,
            turn_state.step_index,
            "internal_capability",
            observation.model_dump(),
        )
        self._services.debug.record_event(
            request.turn_id,
            "capability_finished",
            self._internal_capability_finished_payload(capability, observation, latency_ms, turn_state.task.task_id),
            step_index=1,
        )
        if self._services.settings.yield_after_internal_steps:
            return self._emit_progress_update(
                request=request,
                turn_state=turn_state,
                trace_events=[self._internal_finish_trace(capability, observation, turn_state.step_index)],
                current_step=1,
                progress_reason=f"confirmed_internal_capability:{capability.descriptor.id}",
            )
        return None

    def _queue_pending_confirmation(
        self,
        *,
        request: TurnStartRequest,
        turn_state: TurnState,
        trace_events: list[TraceEventPayload],
        current_step: int,
        capability: Any,
        action_request_payload: dict[str, Any],
    ) -> TurnResponse:
        confirmation_id = str(uuid.uuid4())
        turn_state.task.status = "awaiting_confirmation"
        turn_state.task.requires_confirmation = True
        turn_state.working_memory.current_status = "awaiting_confirmation"
        turn_state.working_memory.open_loops = [
            f"Wait for the player's confirmation about: {action_request_payload['effect_summary']}"
        ]
        self._task_manager.sync_task(turn_state.task)
        self._services.store.put_pending_confirmation(
            request.session_ref,
            confirmation_id,
            action_request_payload["effect_summary"],
            action_request_payload,
            task_id=turn_state.task.task_id,
        )
        self._services.debug.record_event(
            request.turn_id,
            "capability_finished",
            self._bridge_capability_finished_payload(
                capability,
                action_request_payload,
                "awaiting_confirmation",
                current_step,
                turn_state.task.task_id,
                confirmation_id=confirmation_id,
            ),
            step_index=current_step,
        )
        return self._return_final_reply(
            request.turn_id,
            request.session_ref,
            "这一步我已经替你想好了，不过要先等你确认。",
            trace_events
            + [
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
            ],
            status="completed",
            step_index=current_step,
            pending_confirmation_id=confirmation_id,
            pending_confirmation_effect_summary=action_request_payload["effect_summary"],
            preserve_task_status=True,
            turn_state=turn_state,
            request=request,
        )

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
                    "domain": descriptor.domain,
                    "preferred": descriptor.preferred,
                    "semantic_level": descriptor.semantic_level,
                    "freshness_hint": descriptor.freshness_hint,
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
        task_id: str,
    ) -> dict[str, Any]:
        capability_request = getattr(decision, "capability_request", None)
        return {
            "capability_id": capability.descriptor.id,
            "handler_kind": capability.handler_kind,
            "kind": capability.descriptor.kind,
            "risk_class": capability.descriptor.risk_class,
            "execution_mode": capability.descriptor.execution_mode,
            "domain": capability.descriptor.domain,
            "preferred": capability.descriptor.preferred,
            "semantic_level": capability.descriptor.semantic_level,
            "freshness_hint": capability.descriptor.freshness_hint,
            "requires_confirmation": (
                getattr(capability_request, "requires_confirmation", False)
                or getattr(decision, "requires_confirmation", False)
                or capability.descriptor.requires_confirmation
            ),
            "effect_summary": (
                getattr(capability_request, "effect_summary", None)
                or getattr(decision, "effect_summary", None)
                or capability.descriptor.description
            ),
            "arguments": arguments,
            "task_id": task_id,
        }

    def _internal_capability_finished_payload(
        self,
        capability: Any,
        observation: ObservationRef,
        latency_ms: int,
        task_id: str,
    ) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "capability_id": capability.descriptor.id,
            "handler_kind": capability.handler_kind,
            "kind": capability.descriptor.kind,
            "latency_ms": latency_ms,
            "task_id": task_id,
            "observation": observation.context_entry(),
        }

    def _bridge_capability_finished_payload(
        self,
        capability: Any,
        action_request_payload: dict[str, Any],
        status: str,
        step_index: int,
        task_id: str,
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
            "task_id": task_id,
        }
        if continuation_id is not None:
            payload["continuation_id"] = continuation_id
        if confirmation_id is not None:
            payload["confirmation_id"] = confirmation_id
        return payload

    def _internal_start_trace(self, capability: Any, arguments: dict[str, Any], step_index: int) -> TraceEventPayload:
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
        observation: ObservationRef,
        step_index: int,
    ) -> TraceEventPayload:
        secondary = [
            TraceChipPayload(label=f"第 {step_index} 步", tone="muted"),
            TraceChipPayload(label=self._kind_label(capability.descriptor.kind), tone="muted"),
        ]
        return TraceEventPayload(
            status_label="已完成",
            status_tone="success",
            title=self._capability_title(capability.descriptor.id),
            detail=observation.summary,
            secondary=secondary,
        )

    def _internal_finish_trace_stub(self, detail: str, step_index: int) -> TraceEventPayload:
        return TraceEventPayload(
            status_label="已完成",
            status_tone="success",
            title="委托结果已返回",
            detail=detail,
            secondary=[TraceChipPayload(label=f"第 {step_index} 步", tone="muted")],
        )

    def _capability_title(self, capability_id: str) -> str:
        return {
            "artifact.read": "读取外置资料",
            "artifact.search": "搜索外置资料",
            "memory.search": "检索记忆",
            "task.inspect": "查看任务状态",
            "agent.explore.delegate": "委托探索",
            "agent.plan.delegate": "委托规划",
            "retrieval.local_knowledge.search": "检索本地知识",
            "skill.mina_capability_guide": "整理可见能力",
            "script.python_sandbox.execute": "准备脚本执行",
        }.get(capability_id, capability_id)

    def _kind_label(self, kind: str) -> str:
        return {
            "retrieval": "检索",
            "skill": "技能",
            "script": "脚本",
            "tool": "工具",
            "agent": "委托",
        }.get(kind, "内部")

    def _internal_start_detail(self, capability_id: str, arguments: dict[str, Any]) -> str:
        if capability_id == "artifact.read":
            return "我先把之前外置的资料重新读回来。"
        if capability_id == "artifact.search":
            return "我先从外置资料里找找线索。"
        if capability_id == "memory.search":
            return "我先把相关记忆翻出来看看。"
        if capability_id == "task.inspect":
            return "我先看看这件事现在做到哪一步了。"
        if capability_id == "agent.explore.delegate":
            return f"我先把探索部分单独理一下：{arguments.get('objective', '')}"
        if capability_id == "agent.plan.delegate":
            return f"我先把规划单独理一下：{arguments.get('objective', '')}"
        if capability_id == "retrieval.local_knowledge.search":
            return "正在检索本地知识库。"
        if capability_id == "skill.mina_capability_guide":
            return "我先把现在能用的内容理一下。"
        if capability_id == "script.python_sandbox.execute":
            return "我先把这一步该怎么做整理清楚。"
        return "我先替你确认一下。"


class TurnService:
    def __init__(self, services: AgentServices) -> None:
        self._pipeline = TurnPipeline(services)

    def start_turn(self, request: TurnStartRequest) -> TurnResponse:
        return self._pipeline.start_turn(request)

    def resume_turn(self, continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        return self._pipeline.resume_turn(continuation_id, request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pipeline, name)
