from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from mina_agent.schemas import MinaBaseModel


TaskStatus = Literal[
    "pending",
    "analyzing",
    "planned",
    "awaiting_confirmation",
    "in_progress",
    "blocked",
    "completed",
    "failed",
    "canceled",
]


class ArtifactRef(MinaBaseModel):
    artifact_id: str
    session_ref: str
    task_id: str | None = None
    turn_id: str | None = None
    kind: str
    path: str
    summary: str
    content_type: str
    char_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    def context_ref(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "path": self.path,
            "summary": self.summary,
        }


class ObservationRef(MinaBaseModel):
    observation_id: str
    source: str
    summary: str
    preview: Any | None = None
    keys: list[str] = Field(default_factory=list)
    artifact_ref: ArtifactRef | None = None
    salience: float = 0.5
    recovery_hint: str | None = None
    scope_tags: list[str] = Field(default_factory=list)
    created_at: str | None = None

    def context_entry(self) -> dict[str, Any]:
        payload = {
            "observation_id": self.observation_id,
            "source": self.source,
            "summary": self.summary,
            "salience": self.salience,
        }
        if self.preview is not None:
            payload["preview"] = self.preview
        if self.keys:
            payload["keys"] = self.keys
        if self.recovery_hint is not None:
            payload["recovery_hint"] = self.recovery_hint
        if self.scope_tags:
            payload["scope_tags"] = self.scope_tags
        if self.artifact_ref is not None:
            payload["artifact_ref"] = self.artifact_ref.context_ref()
        return payload


class WorkingMemory(MinaBaseModel):
    primary_goal: str = ""
    focus: str = ""
    current_status: str = ""
    completed_actions: list[str] = Field(default_factory=list)
    key_facts: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    next_best_step: str = ""
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    active_observations: list[ObservationRef] = Field(default_factory=list)
    observation_refs: list[dict[str, Any]] = Field(default_factory=list)
    recovery_refs: list[dict[str, Any]] = Field(default_factory=list)
    open_loops: list[str] = Field(default_factory=list)
    companion_state: dict[str, Any] = Field(default_factory=dict)

    def context_entry(self) -> dict[str, Any]:
        return {
            "primary_goal": self.primary_goal,
            "focus": self.focus or self.primary_goal,
            "current_status": self.current_status,
            "completed_actions": self.completed_actions,
            "key_facts": self.key_facts,
            "blockers": self.blockers,
            "pending_questions": self.pending_questions,
            "next_best_step": self.next_best_step,
            "artifact_refs": [artifact.context_ref() for artifact in self.artifact_refs],
            "active_observations": [observation.context_entry() for observation in self.active_observations],
            "observation_refs": self.observation_refs,
            "recovery_refs": self.recovery_refs,
            "open_loops": self.open_loops,
            "companion_state": self.companion_state,
        }


class BlockSubjectLock(MinaBaseModel):
    pos: dict[str, int]
    block_name: str | None = None
    block_id: str | None = None
    summary: str | None = None
    target_found: bool | None = None

    def block_pos(self) -> dict[str, int]:
        return {
            "x": int(self.pos["x"]),
            "y": int(self.pos["y"]),
            "z": int(self.pos["z"]),
        }


class TaskStepState(MinaBaseModel):
    step_key: str
    title: str
    status: str = "pending"
    detail: str | None = None
    step_order: int = 0


class TaskState(MinaBaseModel):
    task_id: str
    task_type: str
    owner_player: str
    goal: str
    status: TaskStatus
    priority: str = "normal"
    risk_class: str = "read_only"
    requires_confirmation: bool = False
    constraints: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    steps: list[TaskStepState] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    parent_task_id: str | None = None
    origin_turn_id: str | None = None
    continuity_score: float = 0.0
    last_active_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def context_entry(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "goal": self.goal,
            "status": self.status,
            "priority": self.priority,
            "risk_class": self.risk_class,
            "requires_confirmation": self.requires_confirmation,
            "constraints": self.constraints,
            "steps": [step.model_dump() for step in self.steps],
            "artifacts": [artifact.context_ref() for artifact in self.artifacts],
            "summary": self.summary,
            "parent_task_id": self.parent_task_id,
            "origin_turn_id": self.origin_turn_id,
            "continuity_score": self.continuity_score,
            "last_active_at": self.last_active_at,
        }


class ReminderBlock(MinaBaseModel):
    name: str
    role: Literal["system", "user"]
    source: str
    strategy: str
    content: Any
    included: bool = True
    full_chars: int = 0
    truncated: bool = False
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)

    def summary_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "source": self.source,
            "strategy": self.strategy,
            "included": self.included,
            "full_chars": self.full_chars,
            "truncated": self.truncated,
            "preview": self.content,
            "artifact_refs": [artifact.context_ref() for artifact in self.artifact_refs],
        }


class MemoryCandidate(MinaBaseModel):
    memory_kind: str
    summary: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)

    def context_entry(self) -> dict[str, Any]:
        return {
            "memory_kind": self.memory_kind,
            "summary": self.summary,
            "score": self.score,
            "metadata": self.metadata,
            "artifact_refs": [artifact.context_ref() for artifact in self.artifact_refs],
        }


class TurnState(MinaBaseModel):
    session_ref: str
    turn_id: str
    request: dict[str, Any]
    step_index: int = 0
    continuation_depth: int = 0
    bridge_action_count: int = 0
    task: TaskState
    working_memory: WorkingMemory = Field(default_factory=WorkingMemory)
    observations: list[ObservationRef] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    active_task_candidate: TaskState | None = None
    block_subject_lock: BlockSubjectLock | None = None
    pending_action_batch: list[dict[str, Any]] = Field(default_factory=list)
    delegate_history: list[dict[str, Any]] = Field(default_factory=list)
    runtime_notes: list[str] = Field(default_factory=list)

    def to_runtime_dict(self) -> dict[str, Any]:
        return self.model_dump()
