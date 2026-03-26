from __future__ import annotations

import asyncio
import uuid
from time import perf_counter
from typing import Any

from mina_agent.core.thread_manager import ActiveTurnHandle, ThreadManager
from mina_agent.memories import MemoryPipeline
from mina_agent.protocol import (
    ApprovalRequest,
    ApprovalResponse,
    ItemCompletedPayload,
    ItemStartedPayload,
    ThreadRollbackParams,
    ToolCallRequest,
    ToolCallResultSubmission,
    TurnFailedPayload,
    TurnRecord,
    TurnSteerParams,
    TurnStartParams,
    WarningPayload,
)
from mina_agent.providers.openai_compatible import ProviderError
from mina_agent.runtime.agent_services import AgentServices
from mina_agent.runtime.capability_registry import RuntimeState
from mina_agent.runtime.context_manager import ContextOverflowError
from mina_agent.runtime.models import TaskState, TurnState, WorkingMemory
from mina_agent.schemas import ModelDecision, PlayerPayload, ServerEnvPayload
from mina_agent.tools import MinaToolRegistry


class TurnInterrupted(RuntimeError):
    pass


class MinaCoreEngine:
    def __init__(
        self,
        services: AgentServices,
        *,
        thread_manager: ThreadManager,
        tool_registry: MinaToolRegistry,
        memory_pipeline: MemoryPipeline,
    ) -> None:
        self._services = services
        self._thread_manager = thread_manager
        self._tool_registry = tool_registry
        self._memory_pipeline = memory_pipeline
        self._task_manager = services.task_manager
        self._context_manager = services.context_manager
        self._deliberation_engine = services.deliberation_engine
        self._execution_manager = services.execution_manager
        self._delegate_runtime = services.delegate_runtime
        if (
            self._task_manager is None
            or self._context_manager is None
            or self._deliberation_engine is None
            or self._execution_manager is None
            or self._delegate_runtime is None
        ):
            raise ValueError("MinaCoreEngine requires fully initialized AgentServices.")

    async def start_turn(
        self,
        params: TurnStartParams,
        emitter,
    ) -> TurnRecord:
        record, handle = await self._thread_manager.open_turn(
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            emitter=emitter,
        )
        task = asyncio.create_task(self._run_turn(params, handle))
        await self._thread_manager.attach_task(params.thread_id, task)
        return record

    async def submit_tool_result(self, submission: ToolCallResultSubmission) -> None:
        await self._thread_manager.submit_tool_result(submission)

    async def submit_approval(self, response: ApprovalResponse) -> None:
        await self._thread_manager.submit_approval(response)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self._thread_manager.interrupt_turn(thread_id, turn_id)

    async def submit_steer(self, params: TurnSteerParams) -> str:
        return await self._thread_manager.submit_steer(params)

    async def rollback_thread(self, params: ThreadRollbackParams) -> dict[str, object]:
        return await self._thread_manager.rollback_thread(params)

    async def _run_turn(self, params: TurnStartParams, handle: ActiveTurnHandle) -> None:
        player = self._player_payload(params)
        server_env = self._server_env_payload(params)
        resolved_tools = self._tool_registry.resolve_tools(
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            user_message=params.user_message,
            player=player,
            server_env=server_env,
            scoped_snapshot=params.context.scoped_snapshot,
            tool_specs=params.context.tool_specs,
            limits=params.context.limits,
        )
        request = resolved_tools.legacy_request
        self._services.store.ensure_thread(
            params.thread_id,
            player_uuid=player.uuid,
            player_name=player.name,
            metadata={"role": player.role},
        )
        task = self._task_manager.prepare_task(request, None)
        active_task_candidate = self._task_manager.load_active_task_candidate(
            request,
            None,
            current_task_id=task.task_id,
        )
        turn_state = TurnState(
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            request=request.model_dump(),
            task=task,
            working_memory=WorkingMemory(
                primary_goal=task.goal,
                focus=task.goal,
                current_status="analyzing",
                next_best_step="Inspect, guide, or reply based on the current trigger.",
                companion_state={"stance": "present", "mode": "companion_first"},
            ),
            active_task_candidate=active_task_candidate,
        )
        self._services.store.create_thread_turn(
            params.turn_id,
            params.thread_id,
            params.user_message,
            turn_state.to_runtime_dict(),
            task_id=task.task_id,
        )
        self._services.audit.record(
            "turn_started",
            {
                "turn_id": params.turn_id,
                "thread_id": params.thread_id,
                "task_id": task.task_id,
            },
        )
        self._services.debug.record_event(
            params.turn_id,
            "turn_started",
            {
                "thread_id": params.thread_id,
                "user_message": params.user_message,
                "player": player.model_dump(),
                "server_env": server_env.model_dump(),
                "limits": params.context.limits.model_dump(),
                "task": task.context_entry(),
            },
        )
        await handle.emitter(
            "turn/started",
            {
                "thread_id": params.thread_id,
                "turn": {
                    "thread_id": params.thread_id,
                    "turn_id": params.turn_id,
                    "status": "running",
                },
            },
        )
        await handle.emitter(
            "thread/status/changed",
            {"thread_id": params.thread_id, "status": "running", "archived": False},
        )
        await self._record_user_item(handle, params)
        try:
            final_reply = await self._advance_turn(params, handle, turn_state, resolved_tools.capabilities)
        except TurnInterrupted:
            await handle.emitter(
                "turn/failed",
                TurnFailedPayload(
                    thread_id=params.thread_id,
                    turn_id=params.turn_id,
                    message="Turn interrupted.",
                ).model_dump(),
            )
            await handle.emitter(
                "thread/status/changed",
                {"thread_id": params.thread_id, "status": "idle", "archived": False},
            )
            await self._thread_manager.complete_turn(
                thread_id=params.thread_id,
                turn_id=params.turn_id,
                status="interrupted",
                final_reply="",
            )
            return
        except Exception as exc:
            await self._emit_warning(
                handle,
                message="Mina turn failed.",
                detail=str(exc),
            )
            self._services.debug.record_event(
                params.turn_id,
                "turn_failed",
                {
                    "thread_id": params.thread_id,
                    "turn_id": params.turn_id,
                    "final_reply": str(exc),
                    "error": str(exc),
                    "task_id": turn_state.task.task_id,
                },
            )
            self._services.store.finish_thread_turn(params.turn_id, str(exc), status="failed")
            await handle.emitter(
                "turn/failed",
                TurnFailedPayload(
                    thread_id=params.thread_id,
                    turn_id=params.turn_id,
                    message="Mina turn failed.",
                    detail=str(exc),
                ).model_dump(),
            )
            await handle.emitter(
                "thread/status/changed",
                {"thread_id": params.thread_id, "status": "idle", "archived": False},
            )
            await self._thread_manager.complete_turn(
                thread_id=params.thread_id,
                turn_id=params.turn_id,
                status="failed",
                final_reply=str(exc),
            )
            return

        self._task_manager.sync_task(turn_state.task)
        self._services.store.finish_thread_turn(params.turn_id, final_reply, status="completed")
        self._memory_pipeline.record_completed_turn(
            request,
            turn_state,
            final_reply=final_reply,
            status="completed",
        )
        self._services.debug.record_event(
            params.turn_id,
            "turn_completed",
            {
                "thread_id": params.thread_id,
                "turn_id": params.turn_id,
                "final_reply": final_reply,
                "task_id": turn_state.task.task_id,
            },
        )
        await handle.emitter(
            "turn/completed",
            {
                "thread_id": params.thread_id,
                "turn": {
                    "thread_id": params.thread_id,
                    "turn_id": params.turn_id,
                    "status": "completed",
                    "final_reply": final_reply,
                },
            },
        )
        await handle.emitter(
            "thread/status/changed",
            {"thread_id": params.thread_id, "status": "idle", "archived": False},
        )
        await self._thread_manager.complete_turn(
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            status="completed",
            final_reply=final_reply,
        )

    async def _advance_turn(
        self,
        params: TurnStartParams,
        handle: ActiveTurnHandle,
        turn_state: TurnState,
        capabilities: list[Any],
    ) -> str:
        request = self._tool_registry.resolve_tools(
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            user_message=params.user_message,
            player=self._player_payload(params),
            server_env=self._server_env_payload(params),
            scoped_snapshot=params.context.scoped_snapshot,
            tool_specs=params.context.tool_specs,
            limits=params.context.limits,
        ).legacy_request
        unknown_attempts = 0
        while turn_state.step_index < min(request.limits.max_agent_steps, self._services.settings.max_agent_steps):
            self._check_interrupted(handle)
            await self._consume_pending_steers(handle, turn_state)
            effective_user_message = str(turn_state.request.get("user_message") or params.user_message)
            request = self._tool_registry.resolve_tools(
                thread_id=params.thread_id,
                turn_id=params.turn_id,
                user_message=effective_user_message,
                player=self._player_payload(params),
                server_env=self._server_env_payload(params),
                scoped_snapshot=params.context.scoped_snapshot,
                tool_specs=params.context.tool_specs,
                limits=params.context.limits,
            ).legacy_request
            turn_state.request = request.model_dump()
            current_step = turn_state.step_index + 1
            try:
                context_result = self._context_manager.build_messages(
                    request=request,
                    turn_state=turn_state,
                    capability_descriptors=[capability.descriptor for capability in capabilities],
                )
            except ContextOverflowError as exc:
                raise RuntimeError(
                    f"Context overflow: {exc.used_tokens}/{exc.budget_tokens} protected={exc.protected_slots}"
                ) from exc

            try:
                decision_result = await asyncio.to_thread(self._deliberation_engine.decide, context_result.messages)
            except ProviderError as exc:
                raise RuntimeError(f"Model call failed: {exc}") from exc
            decision = decision_result.decision
            self._task_manager.apply_task_selection(request.turn_id, turn_state, decision)
            self._task_manager.classify_task_patch(turn_state, decision)
            if decision.intent in {"reply", "guide"} or decision.mode == "final_reply":
                final_reply = decision.final_reply or "我先陪你把这件事理清楚。"
                await self._emit_assistant_message(handle, params, final_reply)
                return final_reply

            if decision.intent in {"delegate_explore", "delegate_plan"} and decision.delegate_request is not None:
                delegate_item = await self._start_item(
                    handle,
                    item_kind="delegate_summary",
                    payload={
                        "role": decision.delegate_request.role,
                        "objective": decision.delegate_request.objective,
                    },
                )
                delegate_result = await asyncio.to_thread(self._delegate_runtime.run, decision.delegate_request, turn_state)
                self._task_manager.apply_task_patch(turn_state, delegate_result.task_patch)
                self._execution_manager.register_observation(
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
                turn_state.step_index += 1
                turn_state.working_memory.current_status = f"delegate_{delegate_result.role}_completed"
                self._task_manager.sync_task(turn_state.task)
                self._services.store.update_turn_state(
                    request.turn_id,
                    turn_state.to_runtime_dict(),
                    task_id=turn_state.task.task_id,
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "delegate_result",
                    delegate_result.model_dump(),
                    step_index=current_step,
                )
                await self._complete_item(
                    handle,
                    delegate_item,
                    item_kind="delegate_summary",
                    payload=delegate_result.model_dump(),
                )
                continue

            capability_request = decision.capability_request
            capability_id = (
                capability_request.capability_id
                if capability_request is not None and capability_request.capability_id
                else (decision.capability_id or "")
            )
            capability = self._execution_manager.resolve_capability(capabilities, capability_id)
            if capability is None:
                unknown_attempts += 1
                await self._emit_warning(
                    handle,
                    message=f"Unknown tool selected: {capability_id or '<empty>'}",
                    detail="Use an exact id from the authoritative tool list.",
                )
                self._services.debug.record_event(
                    request.turn_id,
                    "warning",
                    {
                        "thread_id": params.thread_id,
                        "turn_id": params.turn_id,
                        "message": f"Unknown tool selected: {capability_id or '<empty>'}",
                        "detail": "Use an exact id from the authoritative tool list.",
                    },
                    step_index=current_step,
                )
                turn_state.step_index += 1
                if unknown_attempts >= 2:
                    final_reply = "我不会执行不存在的能力。这一步先停下，我会改用当前确实可见的能力或直接回答。"
                    await self._emit_assistant_message(handle, params, final_reply)
                    return final_reply
                continue

            resolved_arguments = self._execution_manager.resolve_arguments(
                turn_state,
                capability,
                capability_request.arguments if capability_request is not None else decision.arguments,
            )
            requires_confirmation = (
                capability.descriptor.requires_confirmation
                or getattr(capability_request, "requires_confirmation", False)
                or bool(decision.requires_confirmation)
            )
            effect_summary = (
                getattr(capability_request, "effect_summary", None)
                or getattr(decision, "effect_summary", None)
                or capability.descriptor.description
            )
            if requires_confirmation:
                approved = await self._await_approval(
                    handle,
                    capability=capability,
                    params=params,
                    effect_summary=effect_summary,
                    arguments=resolved_arguments,
                    step_index=turn_state.step_index + 1,
                )
                if not approved:
                    final_reply = "这一步我先停下，不会直接替你动手。"
                    await self._emit_assistant_message(handle, params, final_reply)
                    return final_reply

            runtime_state = RuntimeState(
                request=request,
                turn_state=turn_state,
                pending_confirmation=None,
            )
            if capability.handler_kind in {"internal", "bridge_proxy"}:
                final_reply = await self._run_local_capability(
                    handle,
                    params,
                    turn_state,
                    runtime_state,
                    capability,
                    resolved_arguments,
                    effect_summary,
                )
                if final_reply is not None:
                    return final_reply
                continue

            await self._run_external_tool_call(
                handle,
                turn_state,
                capability=capability,
                thread_id=params.thread_id,
                turn_id=params.turn_id,
                arguments=resolved_arguments,
                effect_summary=effect_summary,
            )
            turn_state.step_index += 1
            self._task_manager.sync_task(turn_state.task)
            self._services.store.update_turn_state(
                request.turn_id,
                turn_state.to_runtime_dict(),
                task_id=turn_state.task.task_id,
            )

        final_reply = "Mina stopped because the configured step budget was exhausted."
        await self._emit_assistant_message(handle, params, final_reply)
        return final_reply

    async def _run_local_capability(
        self,
        handle: ActiveTurnHandle,
        params: TurnStartParams,
        turn_state: TurnState,
        runtime_state: RuntimeState,
        capability: Any,
        resolved_arguments: dict[str, Any],
        effect_summary: str,
    ) -> str | None:
        item_id = await self._start_item(
            handle,
            item_kind="tool_call",
            payload={
                "tool_id": capability.descriptor.id,
                "arguments": resolved_arguments,
                "effect_summary": effect_summary,
                "handler_kind": capability.handler_kind,
            },
        )
        self._services.debug.record_event(
            params.turn_id,
            "tool_requested",
            {
                "thread_id": params.thread_id,
                "turn_id": params.turn_id,
                "item_id": item_id,
                "tool_id": capability.descriptor.id,
                "arguments": resolved_arguments,
                "handler_kind": capability.handler_kind,
                "effect_summary": effect_summary,
            },
            step_index=turn_state.step_index + 1,
        )
        started = perf_counter()
        observation_payload = await asyncio.to_thread(
            self._execution_manager.execute_internal,
            capability,
            resolved_arguments,
            runtime_state,
        )
        latency_ms = int((perf_counter() - started) * 1000)
        if capability.handler_kind == "bridge_proxy" and observation_payload.get("_proxy_mode") == "bridge":
            await self._complete_item(
                handle,
                item_id,
                item_kind="tool_call",
                payload={
                    "tool_id": capability.descriptor.id,
                    "status": "proxied_to_external",
                    "latency_ms": latency_ms,
                },
            )
            await self._run_external_tool_call(
                handle,
                turn_state,
                capability=capability,
                thread_id=params.thread_id,
                turn_id=params.turn_id,
                arguments=observation_payload.get("arguments", resolved_arguments),
                effect_summary=observation_payload.get("effect_summary") or effect_summary,
                source_tool_id=capability.descriptor.id,
            )
            turn_state.step_index += 1
            return None

        if capability.handler_kind == "bridge_proxy" and isinstance(observation_payload.get("payload"), dict):
            observation_payload = observation_payload["payload"]

        observation = self._execution_manager.register_observation(
            turn_state,
            source=capability.descriptor.id,
            payload=observation_payload,
            kind="internal_capability",
        )
        self._task_manager.apply_task_patch(turn_state, observation_payload.get("task_patch"))
        turn_state.step_index += 1
        turn_state.working_memory.current_status = "internal_capability_completed"
        turn_state.working_memory.next_best_step = "Continue reasoning with the new observation."
        self._task_manager.sync_task(turn_state.task)
        self._services.store.update_turn_state(
            params.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        await self._complete_item(
            handle,
            item_id,
            item_kind="tool_call",
            payload={
                "tool_id": capability.descriptor.id,
                "status": "completed",
                "latency_ms": latency_ms,
                "observation": observation.context_entry(),
            },
        )
        self._services.debug.record_event(
            params.turn_id,
            "tool_result",
            {
                "thread_id": params.thread_id,
                "turn_id": params.turn_id,
                "item_id": item_id,
                "tool_id": capability.descriptor.id,
                "status": "completed",
                "latency_ms": latency_ms,
                "observation": observation.context_entry(),
            },
            step_index=turn_state.step_index,
        )
        return None

    async def _run_external_tool_call(
        self,
        handle: ActiveTurnHandle,
        turn_state: TurnState,
        *,
        capability: Any,
        thread_id: str,
        turn_id: str,
        arguments: dict[str, Any],
        effect_summary: str,
        source_tool_id: str | None = None,
    ) -> None:
        item_id = str(uuid.uuid4())
        tool_request = ToolCallRequest(
            item_id=item_id,
            thread_id=thread_id,
            turn_id=turn_id,
            tool_id=capability.bridge_target_id or capability.descriptor.id,
            arguments=arguments,
            risk_class=capability.descriptor.risk_class,
            execution_mode=capability.descriptor.execution_mode,
            effect_summary=effect_summary,
            requires_confirmation=False,
            preconditions=[],
            source_tool_id=source_tool_id,
        )
        await self._start_item(
            handle,
            item_kind="tool_call",
            payload=tool_request.model_dump(),
            item_id=item_id,
        )
        self._services.debug.record_event(
            turn_id,
            "tool_requested",
            tool_request.model_dump(),
            step_index=turn_state.step_index + 1,
        )
        await handle.emitter("item/toolCall/requested", tool_request.model_dump())
        future = handle.register_tool_waiter(item_id)
        submission = await future
        observation = self._execution_manager.register_observation(
            turn_state,
            source=source_tool_id or tool_request.tool_id,
            payload=submission.observations,
            kind="bridge_result",
        )
        await self._complete_item(
            handle,
            item_id,
            item_kind="tool_call",
            payload={
                **submission.model_dump(),
                "observation": observation.context_entry(),
            },
        )
        self._services.debug.record_event(
            turn_id,
            "tool_result",
            {
                **submission.model_dump(),
                "observation": observation.context_entry(),
            },
            step_index=turn_state.step_index + 1,
        )
        await handle.emitter(
            "item/toolCall/completed",
            {
                **submission.model_dump(),
                "observation": observation.context_entry(),
            },
        )

    async def _await_approval(
        self,
        handle: ActiveTurnHandle,
        *,
        capability: Any,
        params: TurnStartParams,
        effect_summary: str,
        arguments: dict[str, Any],
        step_index: int,
    ) -> bool:
        item_id = str(uuid.uuid4())
        tool_call = ToolCallRequest(
            item_id=item_id,
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            tool_id=capability.bridge_target_id or capability.descriptor.id,
            arguments=arguments,
            risk_class=capability.descriptor.risk_class,
            execution_mode=capability.descriptor.execution_mode,
            effect_summary=effect_summary,
            requires_confirmation=True,
            preconditions=[],
        )
        approval_id = str(uuid.uuid4())
        approval = ApprovalRequest(
            approval_id=approval_id,
            item_id=item_id,
            thread_id=params.thread_id,
            turn_id=params.turn_id,
            effect_summary=effect_summary,
            reason="Tool requires confirmation before execution.",
            risk_class=capability.descriptor.risk_class,
            tool_call=tool_call,
        )
        await self._start_item(
            handle,
            item_kind="approval_request",
            payload=approval.model_dump(),
            item_id=item_id,
        )
        self._services.debug.record_event(
            params.turn_id,
            "approval_requested",
            approval.model_dump(),
            step_index=step_index,
        )
        await handle.emitter("approval/requested", approval.model_dump())
        future = handle.register_approval_waiter(approval_id)
        response = await future
        await self._complete_item(
            handle,
            item_id,
            item_kind="approval_request",
            payload={
                **approval.model_dump(),
                "approved": response.approved,
                "reason": response.reason,
            },
        )
        self._services.debug.record_event(
            params.turn_id,
            "approval_resolved",
            {
                **approval.model_dump(),
                "approved": response.approved,
                "reason": response.reason,
            },
            step_index=step_index,
        )
        return response.approved

    async def _record_user_item(self, handle: ActiveTurnHandle, params: TurnStartParams) -> None:
        item_id = await self._start_item(
            handle,
            item_kind="user_message",
            payload={"text": params.user_message},
        )
        await self._complete_item(
            handle,
            item_id,
            item_kind="user_message",
            payload={"text": params.user_message},
        )

    async def _consume_pending_steers(self, handle: ActiveTurnHandle, turn_state: TurnState) -> None:
        steer_inputs = handle.drain_steers()
        if not steer_inputs:
            return
        steer_text = "\n".join(
            item.text.strip()
            for item in steer_inputs
            if item.type == "text" and item.text.strip()
        ).strip()
        if not steer_text:
            return
        existing_message = str(turn_state.request.get("user_message") or "").strip()
        if existing_message:
            turn_state.request["user_message"] = f"{existing_message}\n\n[Mid-turn steer]\n{steer_text}"
        else:
            turn_state.request["user_message"] = steer_text
        turn_state.runtime_notes.append(f"Latest user steer: {steer_text}")
        turn_state.working_memory.focus = steer_text
        turn_state.working_memory.next_best_step = "Respect the latest mid-turn user steer before continuing."
        item_id = await self._start_item(
            handle,
            item_kind="user_message",
            payload={
                "source": "turn_steer",
                "input": [item.model_dump() for item in steer_inputs],
                "text": steer_text,
            },
        )
        await self._complete_item(
            handle,
            item_id,
            item_kind="user_message",
            payload={
                "source": "turn_steer",
                "input": [item.model_dump() for item in steer_inputs],
                "text": steer_text,
            },
        )
        self._services.store.update_turn_state(
            turn_state.turn_id,
            turn_state.to_runtime_dict(),
            task_id=turn_state.task.task_id,
        )
        self._services.debug.record_event(
            turn_state.turn_id,
            "turn_steered",
            {
                "thread_id": handle.thread_id,
                "turn_id": handle.turn_id,
                "input": [item.model_dump() for item in steer_inputs],
                "text": steer_text,
            },
            step_index=turn_state.step_index + 1,
        )

    async def _emit_assistant_message(self, handle: ActiveTurnHandle, params: TurnStartParams, message: str) -> None:
        item_id = await self._start_item(
            handle,
            item_kind="assistant_message",
            payload={"text": ""},
        )
        for chunk in self._message_chunks(message):
            await handle.emitter(
                "item/assistantMessage/delta",
                {
                    "thread_id": params.thread_id,
                    "turn_id": params.turn_id,
                    "item_id": item_id,
                    "delta": chunk,
                },
            )
        await self._complete_item(
            handle,
            item_id,
            item_kind="assistant_message",
            payload={"text": message},
        )

    async def _emit_warning(self, handle: ActiveTurnHandle, *, message: str, detail: str | None = None) -> None:
        payload = WarningPayload(
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            message=message,
            detail=detail,
        ).model_dump()
        self._services.debug.record_event(
            handle.turn_id,
            "warning",
            payload,
        )
        await handle.emitter("warning", payload)

    async def _start_item(
        self,
        handle: ActiveTurnHandle,
        *,
        item_kind: str,
        payload: dict[str, Any],
        item_id: str | None = None,
    ) -> str:
        effective_item_id = item_id or str(uuid.uuid4())
        self._services.store.create_turn_item(
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            item_id=effective_item_id,
            item_kind=item_kind,
            payload=payload,
            status="started",
        )
        await handle.emitter(
            "item/started",
            ItemStartedPayload(
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
                item_id=effective_item_id,
                item_kind=item_kind,
                payload=payload,
            ).model_dump(),
        )
        return effective_item_id

    async def _complete_item(
        self,
        handle: ActiveTurnHandle,
        item_id: str,
        *,
        item_kind: str,
        payload: dict[str, Any],
    ) -> None:
        self._services.store.update_turn_item(item_id, status="completed", payload=payload)
        await handle.emitter(
            "item/completed",
            ItemCompletedPayload(
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
                item_id=item_id,
                item_kind=item_kind,
                payload=payload,
            ).model_dump(),
        )

    def _check_interrupted(self, handle: ActiveTurnHandle) -> None:
        if handle.interrupted.is_set():
            raise TurnInterrupted()

    def _player_payload(self, params: TurnStartParams) -> PlayerPayload:
        return PlayerPayload.model_validate(params.context.player.model_dump())

    def _server_env_payload(self, params: TurnStartParams) -> ServerEnvPayload:
        return ServerEnvPayload.model_validate(params.context.server_env.model_dump())

    def _message_chunks(self, message: str, *, size: int = 24) -> list[str]:
        if not message:
            return [""]
        return [message[index:index + size] for index in range(0, len(message), size)]
