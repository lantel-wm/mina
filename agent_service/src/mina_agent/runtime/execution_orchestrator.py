from __future__ import annotations

import json
import uuid
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.runtime.models import ArtifactRef, BlockSubjectLock, ObservationRef, TurnState


class ExecutionOrchestrator:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store

    def register_observation(
        self,
        turn_state: TurnState,
        *,
        source: str,
        payload: dict[str, Any],
        kind: str = "observation",
    ) -> ObservationRef:
        summary = self._summarize_observation(source, payload)
        artifact_record = self._store.write_artifact(
            turn_state.session_ref,
            turn_state.task.task_id,
            turn_state.turn_id,
            kind,
            payload,
            summary,
            metadata={"source": source},
        )
        artifact_ref = ArtifactRef.model_validate(artifact_record)
        preview = self._build_preview(payload)
        observation = ObservationRef(
            observation_id=f"obs_{uuid.uuid4().hex[:12]}",
            source=source,
            summary=summary,
            preview=preview,
            keys=sorted(payload.keys())[:12],
            artifact_ref=artifact_ref,
            created_at=artifact_ref.created_at,
        )
        turn_state.observations.append(observation)
        turn_state.task.artifacts.append(artifact_ref)
        turn_state.working_memory.artifact_refs.append(artifact_ref)
        turn_state.working_memory.completed_actions.append(summary)
        self._update_block_subject_lock(turn_state, source, payload)
        return observation

    def resolve_capability_arguments(
        self,
        turn_state: TurnState,
        capability_args_schema: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_arguments = dict(arguments)
        if "block_pos" not in capability_args_schema:
            return resolved_arguments
        if self._is_block_pos(resolved_arguments.get("block_pos")):
            return resolved_arguments
        if turn_state.block_subject_lock is None:
            return resolved_arguments
        resolved_arguments["block_pos"] = turn_state.block_subject_lock.block_pos()
        return resolved_arguments

    def observation_refs(self, turn_state: TurnState) -> list[dict[str, Any]]:
        return [observation.context_entry() for observation in turn_state.observations]

    def _build_preview(self, payload: dict[str, Any]) -> Any:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        if len(serialized) <= self._settings.artifact_inline_char_budget:
            return payload
        preview = serialized[: self._settings.artifact_inline_char_budget]
        return {
            "preview": preview,
            "full_chars": len(serialized),
            "truncated": True,
        }

    def _summarize_observation(self, source: str, payload: dict[str, Any]) -> str:
        if source == "retrieval.local_knowledge.search":
            return f"Retrieved {len(payload.get('results', []))} local knowledge results."
        if source == "memory.search":
            return f"Retrieved {len(payload.get('results', []))} memory results."
        if source == "artifact.search":
            return f"Found {len(payload.get('results', []))} artifact matches."
        if source == "task.inspect":
            task = payload.get("task", {})
            return f"Inspected task {task.get('task_id', '')}."
        if source in {"agent.explore.delegate", "agent.plan.delegate"}:
            return str(payload.get("summary") or source)
        if source.endswith(".read"):
            for key in ("summary", "block_name", "block_id", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return f"Read result from {source}."
        for key in ("summary", "side_effect_summary", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"Captured observation from {source}."

    def _update_block_subject_lock(
        self,
        turn_state: TurnState,
        capability_id: str,
        observations: dict[str, Any],
    ) -> None:
        pos = observations.get("pos")
        if not self._is_block_pos(pos):
            return

        if turn_state.block_subject_lock is None:
            turn_state.block_subject_lock = BlockSubjectLock(
                pos={
                    "x": int(pos["x"]),
                    "y": int(pos["y"]),
                    "z": int(pos["z"]),
                }
            )

        if not self._same_block_pos(turn_state.block_subject_lock.pos, pos):
            return

        if isinstance(observations.get("block_name"), str) and observations["block_name"].strip():
            turn_state.block_subject_lock.block_name = observations["block_name"].strip()
        if isinstance(observations.get("block_id"), str) and observations["block_id"].strip():
            turn_state.block_subject_lock.block_id = observations["block_id"].strip()
        if isinstance(observations.get("summary"), str) and observations["summary"].strip():
            turn_state.block_subject_lock.summary = observations["summary"].strip()
        if isinstance(observations.get("target_found"), bool):
            turn_state.block_subject_lock.target_found = observations["target_found"]

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
