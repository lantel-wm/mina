from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from mina_agent.schemas import LimitsPayload, MinaBaseModel
from mina_agent.schemas import CompanionDeliveryConstraintsPayload, CompanionSignalPayload, CompanionTriggerPayload


JsonRpcId = int | str


class JsonRpcError(MinaBaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(MinaBaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(MinaBaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None


class JsonRpcNotification(MinaBaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class PlayerContext(MinaBaseModel):
    uuid: str
    name: str
    role: str
    dimension: str
    position: dict[str, Any]


class ServerEnvContext(MinaBaseModel):
    dedicated: bool
    motd: str
    current_players: int
    max_players: int
    carpet_loaded: bool
    experimental_enabled: bool
    dynamic_scripting_enabled: bool


class PreconditionSpec(MinaBaseModel):
    path: str
    expected: Any
    reason: str | None = None


class ExternalToolSpec(MinaBaseModel):
    id: str
    kind: str = "tool"
    description: str
    risk_class: str
    execution_mode: str
    requires_confirmation: bool
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    freshness: str = "live"
    preconditions: list[PreconditionSpec] = Field(default_factory=list)
    domain: str = "general"
    preferred: bool = False
    semantic_level: str = "raw"


class TurnContextPayload(MinaBaseModel):
    player: PlayerContext
    server_env: ServerEnvContext
    scoped_snapshot: dict[str, Any]
    tool_specs: list[ExternalToolSpec]
    limits: LimitsPayload
    companion_trigger: CompanionTriggerPayload | None = None


class ThreadStartParams(MinaBaseModel):
    thread_id: str
    player_uuid: str
    player_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadResumeParams(MinaBaseModel):
    thread_id: str


class ThreadListParams(MinaBaseModel):
    limit: int = 50
    archived: bool | None = None
    search_term: str | None = None


class ThreadReadParams(MinaBaseModel):
    thread_id: str
    include_turns: bool = False


class ThreadArchiveParams(MinaBaseModel):
    thread_id: str


class ThreadUnsubscribeParams(MinaBaseModel):
    thread_id: str


class ThreadNameSetParams(MinaBaseModel):
    thread_id: str
    name: str


class ThreadMetadataUpdateParams(MinaBaseModel):
    thread_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadShellCommandParams(MinaBaseModel):
    thread_id: str
    command: str = Field(min_length=1)


class ThreadForkParams(MinaBaseModel):
    source_thread_id: str
    thread_id: str
    player_uuid: str | None = None
    player_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadCompactParams(MinaBaseModel):
    thread_id: str


class ThreadRollbackParams(MinaBaseModel):
    thread_id: str
    num_turns: int = Field(ge=1)


class TurnStartParams(MinaBaseModel):
    thread_id: str
    turn_id: str
    user_message: str
    context: TurnContextPayload


class CompanionEvaluateContextPayload(MinaBaseModel):
    player: PlayerContext
    server_env: ServerEnvContext
    scoped_snapshot: dict[str, Any]


class CompanionEvaluateParams(MinaBaseModel):
    thread_id: str
    signals: list[CompanionSignalPayload] = Field(default_factory=list)
    context: CompanionEvaluateContextPayload
    companion_state: dict[str, Any] = Field(default_factory=dict)
    delivery_constraints: CompanionDeliveryConstraintsPayload = Field(
        default_factory=CompanionDeliveryConstraintsPayload
    )


class CommandExecTerminalSize(MinaBaseModel):
    cols: int = Field(ge=0)
    rows: int = Field(ge=0)


class CommandExecParams(MinaBaseModel):
    command: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, Any] | None = None
    process_id: str | None = None
    stream_stdin: bool | None = None
    stream_stdout_stderr: bool | None = None
    tty: bool | None = None
    timeout_ms: int | None = None
    disable_timeout: bool | None = None
    output_bytes_cap: int | None = Field(default=None, ge=0)
    disable_output_cap: bool | None = None
    size: CommandExecTerminalSize | None = None


class CommandExecWriteParams(MinaBaseModel):
    process_id: str
    delta_base64: str | None = None
    close_stdin: bool | None = None


class CommandExecTerminateParams(MinaBaseModel):
    process_id: str


class CommandExecResizeParams(MinaBaseModel):
    process_id: str
    size: CommandExecTerminalSize


class TurnSteerInput(MinaBaseModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class TurnSteerParams(MinaBaseModel):
    thread_id: str
    expected_turn_id: str
    input: list[TurnSteerInput] = Field(min_length=1)


class ToolCallRequest(MinaBaseModel):
    item_id: str
    thread_id: str
    turn_id: str
    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_class: str
    execution_mode: str
    effect_summary: str
    requires_confirmation: bool = False
    preconditions: list[PreconditionSpec] = Field(default_factory=list)
    source_tool_id: str | None = None


class ToolCallResultSubmission(MinaBaseModel):
    thread_id: str
    turn_id: str
    item_id: str
    tool_id: str
    status: str
    observations: dict[str, Any] = Field(default_factory=dict)
    preconditions_passed: bool = True
    side_effect_summary: str
    timing_ms: int
    state_fingerprint: str | None = None
    error_message: str | None = None


class ApprovalRequest(MinaBaseModel):
    approval_id: str
    item_id: str
    thread_id: str
    turn_id: str
    effect_summary: str
    reason: str | None = None
    risk_class: str
    tool_call: ToolCallRequest


class ApprovalResponse(MinaBaseModel):
    thread_id: str
    turn_id: str
    approval_id: str
    approved: bool
    reason: str | None = None


class ThreadRecord(MinaBaseModel):
    thread_id: str
    player_uuid: str
    player_name: str
    status: str
    status_detail: dict[str, Any] | None = None
    memory_mode: str | None = None
    archived: bool = False
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class TurnRecord(MinaBaseModel):
    thread_id: str
    turn_id: str
    status: str
    created_at: str
    updated_at: str
    final_reply: str | None = None


class ItemStartedPayload(MinaBaseModel):
    thread_id: str
    turn_id: str
    item_id: str
    item_kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ItemCompletedPayload(MinaBaseModel):
    thread_id: str
    turn_id: str
    item_id: str
    item_kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WarningPayload(MinaBaseModel):
    thread_id: str | None = None
    turn_id: str | None = None
    message: str
    detail: str | None = None


class TurnFailedPayload(MinaBaseModel):
    thread_id: str
    turn_id: str
    message: str
    detail: str | None = None
