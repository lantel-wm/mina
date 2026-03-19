from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MinaBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlayerPayload(MinaBaseModel):
    uuid: str
    name: str
    role: str
    dimension: str
    position: dict[str, Any]


class ServerEnvPayload(MinaBaseModel):
    dedicated: bool
    motd: str
    current_players: int
    max_players: int
    carpet_loaded: bool
    experimental_enabled: bool
    dynamic_scripting_enabled: bool


class VisibleCapabilityPayload(MinaBaseModel):
    id: str
    kind: str
    description: str
    risk_class: str
    execution_mode: str
    requires_confirmation: bool
    args_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)


class LimitsPayload(MinaBaseModel):
    max_agent_steps: int
    max_bridge_actions_per_turn: int
    max_continuation_depth: int


class PendingConfirmationPayload(MinaBaseModel):
    confirmation_id: str
    effect_summary: str


class TurnStartRequest(MinaBaseModel):
    session_ref: str
    turn_id: str
    player: PlayerPayload
    server_env: ServerEnvPayload
    scoped_snapshot: dict[str, Any]
    visible_capabilities: list[VisibleCapabilityPayload]
    limits: LimitsPayload
    pending_confirmation: PendingConfirmationPayload | None = None
    user_message: str


class PreconditionPayload(MinaBaseModel):
    path: str
    expected: Any
    reason: str | None = None


class ActionRequestPayload(MinaBaseModel):
    continuation_id: str
    intent_id: str
    capability_id: str
    risk_class: str
    effect_summary: str
    preconditions: list[PreconditionPayload] = Field(default_factory=list)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False


class ActionResultPayload(MinaBaseModel):
    intent_id: str
    status: str
    observations: dict[str, Any] = Field(default_factory=dict)
    preconditions_passed: bool
    side_effect_summary: str
    timing_ms: int
    state_fingerprint: str | None = None
    error_message: str | None = None


class TurnResumeRequest(MinaBaseModel):
    turn_id: str
    action_results: list[ActionResultPayload]


class TraceChipPayload(MinaBaseModel):
    label: str
    tone: str


class TraceEventPayload(MinaBaseModel):
    status_label: str
    status_tone: str
    title: str
    detail: str | None = None
    secondary: list[TraceChipPayload] = Field(default_factory=list)


class TurnResponse(MinaBaseModel):
    type: Literal["final_reply", "action_request_batch"]
    final_reply: str | None = None
    continuation_id: str | None = None
    action_request_batch: list[ActionRequestPayload] | None = None
    pending_confirmation_id: str | None = None
    trace_events: list[TraceEventPayload] = Field(default_factory=list)


class CapabilityDescriptor(MinaBaseModel):
    id: str
    kind: Literal["tool", "skill", "retrieval", "script"]
    visibility_predicate: str
    risk_class: str
    execution_mode: str
    requires_confirmation: bool
    budget_cost: int
    args_schema: dict[str, Any]
    result_schema: dict[str, Any]
    description: str


class ModelDecision(MinaBaseModel):
    mode: Literal["final_reply", "call_capability"]
    final_reply: str | None = None
    capability_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    effect_summary: str | None = None
    requires_confirmation: bool = False
    confidence: float | None = None
    notes: str | None = None
