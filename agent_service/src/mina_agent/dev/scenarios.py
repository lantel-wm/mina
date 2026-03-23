from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ScenarioAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_final_status: str | None = None
    forbidden_statuses: list[str] = Field(default_factory=list)
    required_capability_ids: list[str] = Field(default_factory=list)
    forbidden_capability_ids: list[str] = Field(default_factory=list)
    confirmation_expected: bool | None = None
    required_reply_substrings: list[str] = Field(default_factory=list)
    forbidden_reply_substrings: list[str] = Field(default_factory=list)
    max_duration_ms: int | None = None


class HeadlessScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    world_template: str
    player_name: str
    setup_commands: list[str] = Field(default_factory=list)
    message: str
    follow_up_messages: list[str] = Field(default_factory=list)
    assertions: ScenarioAssertions = Field(default_factory=ScenarioAssertions)


@dataclass(slots=True)
class ObservedScenarioResult:
    final_status: str
    selected_capability_ids: list[str]
    confirmation_expected: bool
    final_reply: str
    duration_ms: int | None


def load_scenario(path: Path) -> HeadlessScenario:
    return HeadlessScenario.model_validate_json(path.read_text(encoding="utf-8"))


def load_scenarios(scenarios_dir: Path, scenario_ids: list[str] | None = None) -> list[HeadlessScenario]:
    selected = set(scenario_ids or [])
    paths = sorted(scenarios_dir.glob("*.json"))
    scenarios = [load_scenario(path) for path in paths]
    if not selected:
        return scenarios
    return [scenario for scenario in scenarios if scenario.scenario_id in selected]


def evaluate_assertions(assertions: ScenarioAssertions, observed: ObservedScenarioResult) -> tuple[str | None, str | None]:
    if assertions.expected_final_status is not None and observed.final_status != assertions.expected_final_status:
        return "runtime_exception", f"expected status {assertions.expected_final_status}, got {observed.final_status}"

    if observed.final_status in assertions.forbidden_statuses:
        return "runtime_exception", f"forbidden status {observed.final_status} was returned"

    observed_capabilities = set(observed.selected_capability_ids)
    missing_required = [capability_id for capability_id in assertions.required_capability_ids if capability_id not in observed_capabilities]
    if missing_required:
        return "missing_required_capability", f"missing required capability ids: {', '.join(missing_required)}"

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

    return None, None
