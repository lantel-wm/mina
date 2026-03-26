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
        artifact_record = self._store.write_thread_artifact(
            turn_state.thread_id,
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
            payload=payload,
            preview=preview,
            keys=sorted(payload.keys())[:12],
            artifact_ref=artifact_ref,
            salience=self._estimate_salience(source, payload),
            recovery_hint=artifact_ref.path if artifact_ref is not None else None,
            scope_tags=self._scope_tags(source, payload),
            created_at=artifact_ref.created_at,
        )
        turn_state.observations.append(observation)
        turn_state.task.artifacts.append(artifact_ref)
        turn_state.working_memory.artifact_refs.append(artifact_ref)
        turn_state.working_memory.completed_actions.append(summary)
        turn_state.working_memory.active_observations = sorted(
            turn_state.observations,
            key=lambda item: item.salience,
            reverse=True,
        )[:4]
        turn_state.working_memory.observation_refs = [
            {
                "observation_id": item.observation_id,
                "source": item.source,
                "summary": item.summary,
                "artifact_ref": item.artifact_ref.context_ref() if item.artifact_ref else None,
            }
            for item in turn_state.observations[-6:]
        ]
        turn_state.working_memory.recovery_refs = [
            item.artifact_ref.context_ref()
            for item in turn_state.observations[-6:]
            if item.artifact_ref is not None
        ]
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
        if source == "wiki.page.get":
            page = payload.get("page", {})
            title = page.get("title")
            resolved_from = page.get("resolved_from")
            if isinstance(title, str) and title.strip():
                if isinstance(resolved_from, str) and resolved_from.strip():
                    return f"Retrieved wiki page {title} (resolved from {resolved_from})."
                return f"Retrieved wiki page {title}."
            return "Retrieved a wiki page."
        if source.startswith("wiki.") and source.endswith(".find"):
            if source == "wiki.backlinks.find" and payload.get("redirect_resolved"):
                requested = payload.get("requested_title")
                resolved = payload.get("resolved_title")
                return (
                    f"Retrieved {len(payload.get('results', []))} wiki backlinks after resolving "
                    f"{requested} to {resolved}."
                )
            return f"Retrieved {len(payload.get('results', []))} wiki results."
        if source == "memory.search":
            return f"Retrieved {len(payload.get('results', []))} memory results."
        if source == "artifact.search":
            return f"Found {len(payload.get('results', []))} artifact matches."
        if source == "task.inspect":
            task = payload.get("task", {})
            return f"Inspected task {task.get('task_id', '')}."
        if source in {"agent.explore.delegate", "agent.plan.delegate"}:
            return str(payload.get("summary") or source)
        semantic_summary = self._semantic_world_summary(source, payload)
        if semantic_summary is not None:
            return semantic_summary
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

    def _estimate_salience(self, source: str, payload: dict[str, Any]) -> float:
        score = 0.4
        if source.startswith("agent."):
            score += 0.25
        if source.endswith(".read") or source.startswith("observe."):
            score += 0.1
        if source in {"observe.scene", "world.scene.read", "world.threats.read", "world.environment.read"}:
            score += 0.1
        if payload.get("task_patch"):
            score += 0.15
        if payload.get("error") or payload.get("error_message"):
            score += 0.2
        return min(score, 1.0)

    def _scope_tags(self, source: str, payload: dict[str, Any]) -> list[str]:
        tags = [source.split(".")[0]]
        if "block_name" in payload or "block_id" in payload:
            tags.append("block")
        if "player" in payload:
            tags.append("player")
        if source in {"observe.scene", "world.scene.read", "world.threats.read", "world.environment.read"}:
            tags.append("scene")
        if source in {"observe.inventory", "world.inventory.read"}:
            tags.append("inventory")
        if source in {"observe.poi", "world.poi.read"}:
            tags.append("poi")
        if source in {"observe.technical", "carpet.observability.read", "carpet.fake_player.read", "carpet.rules.read"}:
            tags.append("technical")
        if "results" in payload:
            tags.append("retrieval")
        return tags

    def _semantic_world_summary(self, source: str, payload: dict[str, Any]) -> str | None:
        if source in {"observe.scene", "world.scene.read"}:
            risk_state = payload.get("risk_state")
            location_kind = payload.get("location_kind")
            biome = payload.get("biome")
            environment_summary = payload.get("environment_summary")
            if isinstance(risk_state, dict):
                level = str(risk_state.get("level") or "unknown")
                highest = risk_state.get("highest_threat")
                if isinstance(environment_summary, str) and environment_summary.strip():
                    if isinstance(highest, dict) and highest.get("name"):
                        return f"{environment_summary.strip()} Current scene risk is {level}; highest nearby threat is {highest['name']}."
                    return f"{environment_summary.strip()} Current scene risk is {level}."
                location_clause = ""
                if isinstance(location_kind, str) and location_kind.strip():
                    location_clause = f" at {location_kind.replace('_', ' ')}"
                if isinstance(biome, str) and biome.strip():
                    location_clause += f" in {biome}"
                if isinstance(highest, dict) and highest.get("name"):
                    return f"Scene risk is {level}{location_clause}; highest nearby threat is {highest['name']}."
                return f"Scene risk is {level}{location_clause}."
        if source in {"world.threats.read", "world.environment.read"}:
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        if source in {"observe.inventory", "world.inventory.read"}:
            shortages = payload.get("shortages")
            if isinstance(shortages, dict):
                missing = [
                    key.removeprefix("needs_").replace("_", " ")
                    for key, needed in shortages.items()
                    if bool(needed)
                ]
                if missing:
                    return "Inventory pressure detected: " + ", ".join(missing) + "."
                return "Inventory brief shows no urgent shortage."
        if source in {"observe.poi", "world.poi.read"}:
            for key in ("structure", "biome", "poi"):
                value = payload.get(key)
                if isinstance(value, dict) and value.get("found"):
                    label = value.get("tag") or value.get("biome") or value.get("type") or key
                    return f"Located nearby {key}: {label}."
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
            return "No nearby structure, biome, or point of interest was confirmed."
        if source in {"observe.social", "world.social.read"}:
            summary = payload.get("party_summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        if source in {"observe.technical", "carpet.observability.read", "carpet.fake_player.read", "carpet.rules.read"}:
            if payload.get("carpet_loaded") is False:
                return "Carpet observability is unavailable on this server."
            logger_names = payload.get("logger_names")
            if isinstance(logger_names, list):
                fake_player_count = payload.get("fake_player_count") or payload.get("count") or 0
                return f"Technical observability is available with {len(logger_names)} loggers and {fake_player_count} fake players."
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
            return "Technical observability snapshot captured."
        return None
