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
    domain: str = "general"
    preferred: bool = False
    semantic_level: Literal["semantic", "raw", "diagnostic"] = "raw"
    freshness_hint: Literal["ambient", "live"] = "live"


class LimitsPayload(MinaBaseModel):
    max_agent_steps: int
    max_bridge_actions_per_turn: int
    max_continuation_depth: int


class PendingConfirmationPayload(MinaBaseModel):
    confirmation_id: str
    effect_summary: str


CompanionSignalKind = Literal[
    "player_join_greeting",
    "danger_warning",
    "death_followup",
    "advancement_celebration",
    "milestone_encouragement",
    "repetition_comfort",
]


class CompanionSignalPayload(MinaBaseModel):
    signal_id: str
    kind: CompanionSignalKind
    importance: Literal["high", "medium", "low"]
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CompanionDeliveryConstraintsPayload(MinaBaseModel):
    style: Literal["restrained"] = "restrained"
    interrupt_policy: Literal["never"] = "never"
    max_selected_signals: int = 2


class CompanionTriggerPayload(MinaBaseModel):
    mode: Literal["proactive_companion"] = "proactive_companion"
    primary_signal: CompanionSignalPayload
    supporting_signals: list[CompanionSignalPayload] = Field(default_factory=list)
    synthetic: bool = True
    occurred_at: str
    importance: Literal["high", "medium", "low"]
    delivery_constraints: CompanionDeliveryConstraintsPayload = Field(
        default_factory=CompanionDeliveryConstraintsPayload
    )


class TurnStartRequest(MinaBaseModel):
    thread_id: str
    turn_id: str
    player: PlayerPayload
    server_env: ServerEnvPayload
    scoped_snapshot: dict[str, Any]
    visible_capabilities: list[VisibleCapabilityPayload]
    limits: LimitsPayload
    companion_trigger: CompanionTriggerPayload | None = None
    pending_confirmation: PendingConfirmationPayload | None = None
    user_message: str


class PreconditionPayload(MinaBaseModel):
    path: str
    expected: Any
    reason: str | None = None


class CapabilityDescriptor(MinaBaseModel):
    id: str
    kind: Literal["tool", "skill", "retrieval", "script", "agent"]
    visibility_predicate: str
    risk_class: str
    execution_mode: str
    requires_confirmation: bool
    budget_cost: int
    args_schema: dict[str, Any]
    result_schema: dict[str, Any]
    description: str
    domain: str = "general"
    preferred: bool = False
    semantic_level: Literal["semantic", "raw", "diagnostic"] = "raw"
    freshness_hint: Literal["ambient", "live"] = "live"


AgentRole = Literal["companion", "explore", "plan", "action"]
DecisionIntent = Literal[
    "reply",
    "guide",
    "inspect",
    "retrieve",
    "delegate_explore",
    "delegate_plan",
    "execute",
    "await_confirmation",
]


class CapabilityRequest(MinaBaseModel):
    capability_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    effect_summary: str | None = None
    requires_confirmation: bool = False


class ConfirmationRequest(MinaBaseModel):
    effect_summary: str
    reason: str | None = None


class DelegateRequest(MinaBaseModel):
    role: AgentRole
    objective: str
    context_hints: list[str] = Field(default_factory=list)


class DelegateSummary(MinaBaseModel):
    summary: str
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    stop_reason: str | None = None


class DelegateResult(MinaBaseModel):
    role: AgentRole
    objective: str
    summary: DelegateSummary
    task_patch: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)


class ContextCompactionResult(MinaBaseModel):
    slot_replacements: dict[str, Any] = Field(default_factory=dict)
    dropped_slots: list[str] = Field(default_factory=list)
    rationale: str | None = None
    target_tokens: int | None = None


class CompanionEvaluateDecision(MinaBaseModel):
    action: Literal["start_turn", "defer", "drop"]
    selected_signal_ids: list[str] = Field(default_factory=list)
    defer_seconds: int | None = None
    synthetic_user_message: str | None = None
    reason: str | None = None


class ModelDecision(MinaBaseModel):
    mode: Literal["final_reply", "call_capability"] | None = None
    intent: DecisionIntent | None = None
    task_selection: Literal["keep_current", "reuse_active"] | None = None
    response_style: Literal["companion", "guide", "concise", "neutral"] | None = None
    final_reply: str | None = None
    capability_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    effect_summary: str | None = None
    requires_confirmation: bool = False
    delegate_role: AgentRole | None = None
    delegate_objective: str | None = None
    capability_request: CapabilityRequest | None = None
    delegate_request: DelegateRequest | None = None
    task_update: dict[str, Any] = Field(default_factory=dict)
    confirmation_request: ConfirmationRequest | None = None
    confidence: float | None = None
    notes: str | None = None
