from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SuiteName = Literal["functional", "real"]
ScenarioStatus = Literal["runnable_now", "planned"]
ScenarioExpectation = Literal["required", "target_state", "known_issue"]
QualityJudge = Literal["codex"]

INFRA_FAILURE_CATEGORIES = frozenset(
    {
        "startup_failure",
        "missing_accepted_turn",
        "timeout",
        "missing_trace_bundle",
    }
)


class ScenarioAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_final_status: str | None = None
    forbidden_statuses: list[str] = Field(default_factory=list)
    required_capability_ids: list[str] = Field(default_factory=list)
    required_capability_groups: list[list[str]] = Field(default_factory=list)
    forbidden_capability_ids: list[str] = Field(default_factory=list)
    confirmation_expected: bool | None = None
    required_reply_substrings: list[str] = Field(default_factory=list)
    forbidden_reply_substrings: list[str] = Field(default_factory=list)
    max_duration_ms: int | None = None


class FeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_experimental: bool = False
    enable_dynamic_scripting: bool = False


class QualityReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    judge: QualityJudge = "codex"
    rubric_id: str | None = None


class ActorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str
    name: str
    role: str = "read_only"
    operator: bool = False
    experimental: bool = False
    spawn_commands: list[str] = Field(default_factory=list)


class TurnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str
    message: str
    setup_commands_before: list[str] = Field(default_factory=list)
    assertions_override: ScenarioAssertions | None = None


class HeadlessScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: SuiteName
    scenario_id: str
    world_template: str
    status: ScenarioStatus = "runnable_now"
    expectation: ScenarioExpectation = "required"
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    actors: list[ActorSpec]
    turns: list[TurnSpec]
    quality_review: QualityReviewConfig = Field(default_factory=QualityReviewConfig)
    setup_commands: list[str] = Field(default_factory=list)
    assertions: ScenarioAssertions = Field(default_factory=ScenarioAssertions)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "actors" in value and "turns" in value and "suite" in value:
            return value

        player_name = str(value.get("player_name") or "Steve")
        actor_id = "player"
        message = str(value.get("message") or "")
        follow_up_messages = list(value.get("follow_up_messages") or [])
        turns = [{"actor_id": actor_id, "message": message}]
        turns.extend({"actor_id": actor_id, "message": str(item)} for item in follow_up_messages)

        return {
            "suite": value.get("suite") or "real",
            "scenario_id": value["scenario_id"],
            "world_template": value["world_template"],
            "status": value.get("status") or "runnable_now",
            "expectation": value.get("expectation") or ("target_state" if value.get("suite") == "real" else "required"),
            "feature_flags": value.get("feature_flags") or {},
            "actors": value.get("actors")
            or [
                {
                    "actor_id": actor_id,
                    "name": player_name,
                    "role": value.get("role") or "read_only",
                    "operator": bool(value.get("operator", False)),
                    "experimental": bool(value.get("experimental", False)),
                    "spawn_commands": [],
                }
            ],
            "turns": value.get("turns") or turns,
            "quality_review": value.get("quality_review") or {"enabled": False},
            "setup_commands": value.get("setup_commands") or [],
            "assertions": value.get("assertions") or {},
        }

    def actor(self, actor_id: str) -> ActorSpec:
        for actor in self.actors:
            if actor.actor_id == actor_id:
                return actor
        raise KeyError(f"Unknown actor_id {actor_id} in scenario {self.scenario_id}")


class QualityReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "skipped_unavailable", "skipped_disabled", "review_error", "deferred_user_review"]
    judge: QualityJudge = "codex"
    pass_: bool | None = Field(default=None, alias="pass")
    grounded: bool | None = None
    companion_like: bool | None = None
    restrained: bool | None = None
    useful: bool | None = None
    rationale: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ObservedScenarioResult:
    final_status: str
    selected_capability_ids: list[str]
    confirmation_expected: bool
    final_reply: str
    duration_ms: int | None
    quality_review: QualityReviewResult | None = None


@dataclass(slots=True)
class ScenarioLoadResult:
    runnable: list[HeadlessScenario]
    planned: list[HeadlessScenario]
    known_issues: list[HeadlessScenario]


def load_scenario(path: Path) -> HeadlessScenario:
    return HeadlessScenario.model_validate_json(path.read_text(encoding="utf-8"))


def load_scenarios(
    scenarios_dir: Path,
    *,
    suite: SuiteName | None = None,
    scenario_ids: list[str] | None = None,
    include_known_issues: bool = False,
) -> ScenarioLoadResult:
    selected = set(scenario_ids or [])
    runnable: list[HeadlessScenario] = []
    planned: list[HeadlessScenario] = []
    known_issues: list[HeadlessScenario] = []
    for path in sorted(scenarios_dir.rglob("*.json")):
        scenario = load_scenario(path)
        if suite is not None and scenario.suite != suite:
            continue
        if selected and scenario.scenario_id not in selected:
            continue
        if scenario.status == "planned":
            planned.append(scenario)
            continue
        if scenario.expectation == "known_issue":
            if include_known_issues:
                runnable.append(scenario)
            else:
                known_issues.append(scenario)
            continue
        runnable.append(scenario)
    return ScenarioLoadResult(runnable=runnable, planned=planned, known_issues=known_issues)


def evaluate_assertions(assertions: ScenarioAssertions, observed: ObservedScenarioResult) -> tuple[str | None, str | None]:
    if assertions.expected_final_status is not None and observed.final_status != assertions.expected_final_status:
        return "runtime_exception", f"expected status {assertions.expected_final_status}, got {observed.final_status}"

    if observed.final_status in assertions.forbidden_statuses:
        return "runtime_exception", f"forbidden status {observed.final_status} was returned"

    observed_capabilities = set(observed.selected_capability_ids)
    missing_required = [capability_id for capability_id in assertions.required_capability_ids if capability_id not in observed_capabilities]
    if missing_required:
        return "missing_required_capability", f"missing required capability ids: {', '.join(missing_required)}"

    for group in assertions.required_capability_groups:
        if not any(capability_id in observed_capabilities for capability_id in group):
            return "missing_required_capability", f"missing required capability group: {' | '.join(group)}"

    used_forbidden = [capability_id for capability_id in assertions.forbidden_capability_ids if capability_id in observed_capabilities]
    if used_forbidden:
        return "runtime_exception", f"forbidden capability ids were used: {', '.join(used_forbidden)}"

    if assertions.confirmation_expected is not None and observed.confirmation_expected != assertions.confirmation_expected:
        return "runtime_exception", (
            f"expected confirmation_required={assertions.confirmation_expected}, "
            f"got {observed.confirmation_expected}"
        )

    for fragment in assertions.required_reply_substrings:
        if fragment not in observed.final_reply:
            return "reply_assertion_failure", f"required reply substring not found: {fragment}"

    for fragment in assertions.forbidden_reply_substrings:
        if fragment and fragment in observed.final_reply:
            return "reply_assertion_failure", f"forbidden reply substring was present: {fragment}"

    if assertions.max_duration_ms is not None and observed.duration_ms is not None and observed.duration_ms > assertions.max_duration_ms:
        return "timeout", f"scenario exceeded max duration: {observed.duration_ms} ms > {assertions.max_duration_ms} ms"

    if observed.quality_review is not None and observed.quality_review.status == "failed":
        rationale = observed.quality_review.rationale or "quality review failed"
        return "quality_review_failure", rationale

    return None, None


def is_infra_failure(category: str | None) -> bool:
    return category in INFRA_FAILURE_CATEGORIES
