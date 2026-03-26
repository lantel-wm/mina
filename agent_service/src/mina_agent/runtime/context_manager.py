from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.runtime.context_pack import ContextPack, ContextSlot, TrimPolicy
from mina_agent.runtime.memory_policy import MemoryPolicy
from mina_agent.runtime.prompt_token_estimator import PromptTokenEstimator
from mina_agent.runtime.models import ObservationRef, TurnState
from mina_agent.schemas import CapabilityDescriptor, ContextCompactionResult, TurnStartRequest


@dataclass(slots=True)
class ContextBuildResult:
    messages: list[dict[str, str]]
    sections: list[dict[str, Any]]
    message_stats: dict[str, Any]
    composition: dict[str, str]
    recovery_refs: list[dict[str, Any]]
    budget_report: dict[str, Any]
    active_context_slots: list[str]
    pack: ContextPack
    protected_slots: list[str]


@dataclass(slots=True)
class CompactionTarget:
    path: str
    slot_name: str
    content: Any
    estimated_tokens: int
    local_rules: tuple[str, ...]
    expected_root_types: tuple[type[Any], ...]


@dataclass(slots=True)
class CompactionPrompt:
    target: CompactionTarget
    messages: list[dict[str, str]]


class ContextOverflowError(RuntimeError):
    def __init__(self, *, budget_tokens: int, used_tokens: int, protected_slots: list[str]) -> None:
        super().__init__("Context overflow: required context exceeds hard budget.")
        self.budget_tokens = budget_tokens
        self.used_tokens = used_tokens
        self.protected_slots = protected_slots


class ContextManager:
    _PROTECTED_SLOT_NAMES = (
        "capability_brief",
        "dialogue_history",
        "dialogue_continuity",
        "observation_brief",
        "memory_citation_rules",
    )
    _SCENE_SLICE_PROTECTED_KEYS = ("player", "world", "target_block", "risk_state")
    _COMPACTION_CANDIDATE_SLOT_NAMES = (
        "recoverable_history",
        "player_memory",
        "runtime_policy",
        "scene_slice",
        "task_focus",
    )
    _COMPACTION_TARGET_PRIORITY = (
        "recoverable_history",
        "player_memory",
        "task_focus",
        "scene_slice.recent_events",
        "scene_slice.social",
        "scene_slice.technical",
        "scene_slice.interactables",
        "scene_slice.server_rules_refs",
        "runtime_policy",
    )
    _TRIM_POLICY = TrimPolicy(
        priority_order=(
            "capability_brief",
            "dialogue_continuity",
            "dialogue_history",
            "recoverable_history",
            "player_memory",
            "scene_slice",
            "task_focus",
            "confirmation_loop",
            "runtime_policy",
        ),
        hard_floor_chars=320,
    )

    def __init__(self, settings: Settings, store: Store, memory_policy: MemoryPolicy) -> None:
        self._settings = settings
        self._store = store
        self._memory_policy = memory_policy
        self._token_estimator = PromptTokenEstimator(
            settings.model,
            settings.context_tokenizer_encoding_override,
        )

    def build_messages(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        capability_descriptors: list[CapabilityDescriptor],
    ) -> ContextBuildResult:
        normalized_snapshot = self._normalize_snapshot(request.scoped_snapshot)
        session_turns = self._store.list_thread_turns(request.thread_id)
        recent_turns = self._store.list_recent_thread_turns(
            request.thread_id,
            limit=self._settings.context_recent_full_turns,
        )
        compacted_history = self._compact_history(request.thread_id, session_turns, turn_state)
        retrieved_memory = self._memory_policy.summarize_for_context(
            self._store.search_thread_memories(request.thread_id, request.user_message, limit=6)
        )
        session_summary = self._store.get_thread_summary(request.thread_id)
        recent_dialogue_memory = self._build_recent_dialogue_memory(session_summary)
        player_memory = self._build_player_memory_read_path(request)
        recovery_refs = self._collect_recovery_refs(
            turn_state,
            compacted_history,
            session_summary,
            retrieved_memory,
            player_memory,
        )

        pack = ContextPack(
            slots=[
                self._slot(
                    "stable_core",
                    "system",
                    "core.instructions",
                    "stable_cached_text",
                    self._stable_core_text(),
                    priority=100,
                ),
                self._slot(
                    "runtime_policy",
                    "system",
                    "runtime.policy+persona",
                    "dynamic_structured_reminder",
                    self._runtime_policy_payload(request, turn_state),
                    priority=95,
                ),
                self._slot(
                    "scene_slice",
                    "user",
                    "request.scoped_snapshot",
                    "structured_slice",
                    self._build_scene_slice(normalized_snapshot),
                    priority=85,
                ),
                self._slot(
                    "observation_brief",
                    "user",
                    "turn_state.observations+block_subject_lock",
                    "structured_live_observation_brief",
                    self._build_observation_brief(turn_state),
                    priority=82,
                ),
                self._slot(
                    "task_focus",
                    "user",
                    "turn_state.working_memory+task",
                    "structured_summary",
                    self._build_task_focus(turn_state),
                    priority=80,
                ),
                self._slot(
                    "confirmation_loop",
                    "user",
                    "turn_state.pending_confirmation",
                    "structured_loop",
                    self._build_confirmation_loop(turn_state),
                    priority=78,
                ),
                self._slot(
                    "dialogue_continuity",
                    "user",
                    "session_summary.active_dialogue_loop",
                    "structured_dialogue_continuity",
                    self._build_dialogue_continuity(recent_dialogue_memory),
                    priority=57,
                ),
                self._slot(
                    "dialogue_history",
                    "user",
                    "db.turns",
                    "structured_recent_turn_history",
                    self._build_dialogue_history(recent_turns),
                    priority=56,
                ),
                self._slot(
                    "recoverable_history",
                    "user",
                    "memory+history+refs",
                    "recoverable_recall",
                    {
                        "session_summary": self._compact_session_summary(session_summary),
                        "memories": [candidate.context_entry() for candidate in retrieved_memory],
                        "history": compacted_history,
                        "recovery_refs": recovery_refs,
                    },
                    priority=55,
                    recoverable=True,
                ),
                self._slot(
                    "player_memory",
                    "user",
                    "player_memory_root.read_path",
                    "player_scoped_progressive_disclosure",
                    player_memory,
                    priority=54,
                    recoverable=True,
                ),
                self._slot(
                    "memory_citation_rules",
                    "system",
                    "player_memory.citation_rules",
                    "memory_citation_contract",
                    self._memory_citation_rules(player_memory),
                    priority=53,
                ),
                self._slot(
                    "capability_brief",
                    "user",
                    "resolved_capability_descriptors",
                    "exact_capability_id_list",
                    [descriptor.id for descriptor in capability_descriptors],
                    priority=40,
                ),
            ],
            trim_policy=self._TRIM_POLICY,
        )
        return self._render_context_pack(pack, recovery_refs=recovery_refs, compaction_passes=0)

    def build_compaction_request(
        self,
        context_result: ContextBuildResult,
        *,
        current_tokens: int,
        target_tokens: int,
        pass_index: int,
    ) -> CompactionPrompt | None:
        target = self._select_compaction_target(
            context_result,
            current_tokens=current_tokens,
            target_tokens=target_tokens,
        )
        if target is None:
            return None

        system_lines = [
            "You are Mina's context compactor.",
            "Reduce token usage while preserving factual meaning for one target only.",
            f"Compact only `{target.path}`.",
            "Never invent new facts, capabilities, coordinates, identities, or live observations.",
            "Never return the slot name, rationale, markdown, or any wrapper object.",
            f"Return only a JSON {self._json_shape_name(target.content)} for `{target.path}`.",
        ]
        user_payload: dict[str, Any] = {
            "pass_index": pass_index,
            "current_tokens": current_tokens,
            "target_tokens": target_tokens,
            "target_path": target.path,
            "content": target.content,
        }
        if target.local_rules:
            user_payload["local_rules"] = list(target.local_rules)
        messages = [
            {"role": "system", "content": "\n".join(system_lines)},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            },
        ]
        return CompactionPrompt(target=target, messages=messages)

    def build_compaction_messages(
        self,
        context_result: ContextBuildResult,
        *,
        current_tokens: int,
        target_tokens: int,
        pass_index: int,
    ) -> list[dict[str, str]]:
        request = self.build_compaction_request(
            context_result,
            current_tokens=current_tokens,
            target_tokens=target_tokens,
            pass_index=pass_index,
        )
        return request.messages if request is not None else []

    def _select_compaction_target(
        self,
        context_result: ContextBuildResult,
        *,
        current_tokens: int,
        target_tokens: int,
    ) -> CompactionTarget | None:
        del current_tokens, target_tokens
        priority_order = {path: index for index, path in enumerate(self._COMPACTION_TARGET_PRIORITY)}
        candidates = self._compaction_targets(context_result)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (priority_order.get(item.path, len(priority_order)), -item.estimated_tokens))
        return candidates[0]

    def _compaction_targets(self, context_result: ContextBuildResult) -> list[CompactionTarget]:
        slot_by_name = {slot.name: slot for slot in context_result.pack.active_slots()}
        candidates: list[CompactionTarget] = []
        for path in self._COMPACTION_TARGET_PRIORITY:
            slot_name, _, branch_name = path.partition(".")
            slot = slot_by_name.get(slot_name)
            if slot is None or not slot.included:
                continue
            if branch_name:
                if slot_name != "scene_slice" or not isinstance(slot.content, dict):
                    continue
                content = slot.content.get(branch_name)
            else:
                if slot.name not in self._COMPACTION_CANDIDATE_SLOT_NAMES:
                    continue
                content = slot.content
            if not self._has_compactable_content(content):
                continue
            serialized = self._serialize_compaction_payload(content)
            candidates.append(
                CompactionTarget(
                    path=path,
                    slot_name=slot_name,
                    content=content,
                    estimated_tokens=self._estimate_text_tokens(serialized),
                    local_rules=self._local_rules_for_target(path),
                    expected_root_types=self._expected_root_types(content),
                )
            )
        return candidates

    def _has_compactable_content(self, content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, (list, dict, str)):
            return bool(content)
        return True

    def _serialize_compaction_payload(self, payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def _estimate_text_tokens(self, text: str) -> int:
        estimate = self._token_estimator.estimate_messages([{"role": "user", "content": text}])
        return estimate.total_tokens

    def _expected_root_types(self, content: Any) -> tuple[type[Any], ...]:
        if isinstance(content, dict):
            return (dict,)
        if isinstance(content, list):
            return (list,)
        if isinstance(content, str):
            return (str,)
        if isinstance(content, bool):
            return (bool,)
        if content is None:
            return (type(None),)
        if isinstance(content, int):
            return (int,)
        if isinstance(content, float):
            return (float, int)
        return (type(content),)

    def _json_shape_name(self, content: Any) -> str:
        if isinstance(content, dict):
            return "object"
        if isinstance(content, list):
            return "array"
        if isinstance(content, str):
            return "string"
        if isinstance(content, bool):
            return "boolean"
        if content is None:
            return "null"
        if isinstance(content, (int, float)):
            return "number"
        return "value"

    def _local_rules_for_target(self, target_path: str) -> tuple[str, ...]:
        if target_path == "recoverable_history":
            return (
                "Prefer short summaries, counts, and recovery refs over long prose.",
                "Keep path and transcript refs if present.",
                "Drop or shrink memories before changing recovery availability facts.",
            )
        if target_path == "task_focus":
            return (
                "Keep the current task header and current trigger intact.",
                "Keep only high-signal working-memory facts needed for the next reply or action.",
                "Drop duplicated artifact, observation, and recovery reference lists first.",
            )
        if target_path == "scene_slice.recent_events":
            return (
                "Prefer the newest and highest-importance events.",
                "Preserve exact timestamps, ids, and retained event payload facts.",
            )
        if target_path.startswith("scene_slice."):
            return (
                "Keep only facts that materially help immediate scene reasoning.",
                "Drop empty lists, redundant summaries, and repeated low-signal details first.",
            )
        if target_path == "runtime_policy":
            return (
                "Keep language, player role, limits, task header, and essential persona guidance.",
                "Drop repeated task artifacts, long notes, and non-essential reminders first.",
            )
        return ()

    def apply_compaction_target(
        self,
        context_result: ContextBuildResult,
        *,
        target_path: str,
        replacement: Any,
        compaction_passes: int,
    ) -> ContextBuildResult:
        pack = copy.deepcopy(context_result.pack)
        slot_by_name = {slot.name: slot for slot in pack.slots}
        slot_name, _, branch_name = target_path.partition(".")
        slot = slot_by_name.get(slot_name)
        if slot is None or not slot.included:
            return self._render_context_pack(
                pack,
                recovery_refs=context_result.recovery_refs,
                compaction_passes=compaction_passes,
            )

        if branch_name:
            if isinstance(slot.content, dict):
                updated = dict(slot.content)
                updated[branch_name] = replacement
                slot.content = updated
        else:
            slot.content = replacement
        slot.truncated = True
        slot.strategy = f"{slot.strategy}+llm_compacted"

        return self._render_context_pack(
            pack,
            recovery_refs=context_result.recovery_refs,
            compaction_passes=compaction_passes,
        )

    def apply_compaction_result(
        self,
        context_result: ContextBuildResult,
        compaction: ContextCompactionResult,
        *,
        compaction_passes: int,
    ) -> ContextBuildResult:
        pack = copy.deepcopy(context_result.pack)
        slot_by_name = {slot.name: slot for slot in pack.slots}

        for slot_name in compaction.dropped_slots:
            if slot_name in self._PROTECTED_SLOT_NAMES:
                continue
            slot = slot_by_name.get(slot_name)
            if slot is None:
                continue
            slot.included = False
            slot.truncated = True
            slot.strategy = f"{slot.strategy}+llm_compacted"

        for slot_name, replacement in compaction.slot_replacements.items():
            if slot_name in self._PROTECTED_SLOT_NAMES:
                continue
            slot = slot_by_name.get(slot_name)
            if slot is None or not slot.included:
                continue
            if slot_name == "scene_slice":
                replacement = self._merge_scene_slice_compaction(slot.content, replacement)
            slot.content = replacement
            slot.truncated = True
            slot.strategy = f"{slot.strategy}+llm_compacted"

        return self._render_context_pack(
            pack,
            recovery_refs=context_result.recovery_refs,
            compaction_passes=compaction_passes,
        )

    def _render_context_pack(
        self,
        pack: ContextPack,
        *,
        recovery_refs: list[dict[str, Any]],
        compaction_passes: int,
    ) -> ContextBuildResult:
        active_slots = pack.active_slots()
        system_content = self._render_slots([slot for slot in active_slots if slot.role == "system"])
        user_content = self._render_slots([slot for slot in active_slots if slot.role == "user"])
        messages = [{"role": "system", "content": system_content}, {"role": "user", "content": user_content}]
        char_stats = {
            "message_count": len(messages),
            "system_chars": len(system_content),
            "user_chars": len(user_content),
            "total_chars": len(system_content) + len(user_content),
        }
        token_estimate = self._token_estimator.estimate_messages(messages)
        message_tokens = token_estimate.per_message_tokens + [0, 0]
        system_tokens = message_tokens[0]
        user_tokens = message_tokens[1]
        total_tokens = token_estimate.total_tokens

        return ContextBuildResult(
            messages=messages,
            sections=[slot.summary_entry() for slot in active_slots],
            message_stats={
                **char_stats,
                "encoding_name": token_estimate.encoding_name,
                "system_tokens": system_tokens,
                "user_tokens": user_tokens,
                "total_tokens": total_tokens,
            },
            composition={slot.name: slot.strategy for slot in active_slots},
            recovery_refs=recovery_refs,
            budget_report={
                "budget_tokens": self._settings.context_token_budget,
                "used_tokens": total_tokens,
                "compaction_passes": compaction_passes,
                "within_budget": total_tokens <= self._settings.context_token_budget,
            },
            active_context_slots=[slot.name for slot in active_slots],
            pack=pack,
            protected_slots=self._protected_slot_refs(),
        )

    def _merge_scene_slice_compaction(self, original: Any, replacement: Any) -> Any:
        if not isinstance(original, dict):
            return replacement
        if not isinstance(replacement, dict):
            replacement = {}
        merged = dict(replacement)
        for key in self._SCENE_SLICE_PROTECTED_KEYS:
            merged[key] = original.get(key)
        for key in ("recent_events", "server_rules_refs"):
            if key not in merged and key in original:
                merged[key] = original.get(key)
        return merged

    def _protected_slot_refs(self) -> list[str]:
        return [
            *self._PROTECTED_SLOT_NAMES,
            "scene_slice.player",
            "scene_slice.world",
            "scene_slice.target_block",
            "scene_slice.risk_state",
        ]

    def _stable_core_text(self) -> str:
        return (
            "You are Mina, a natural-language-first Minecraft companion runtime.\n"
            "Companionship comes before execution, and execution must serve player enjoyment.\n"
            "Default to grounded Simplified Chinese replies when action is unnecessary.\n"
            "Treat every action as a plan with assumptions; re-check live state instead of trusting stale context.\n"
            "Prefer guidance, retrieval, or isolated delegate exploration before execution when uncertainty is high.\n"
            "Delegate roles are strict: companion decides, explore inspects, plan proposes, bridge actions execute only in the main turn.\n"
            "Delegate turns may not call bridge actions and may not delegate recursively.\n"
            "Do not delegate explore repeatedly when no new facts were found. If live inspection is still needed and a visible read capability matches, call it directly.\n"
            "capability_brief is the authoritative exact list of callable capability ids for this turn.\n"
            "dialogue_history is the authoritative recent conversation history sourced from persisted DB turns.\n"
            "dialogue_continuity contains raw recent follow-up context signals such as Mina's last open question. Whether the current player message is related is for you to judge.\n"
            "observation_brief contains the latest live read results and any locked target subject for this turn.\n"
            "Never invent capability ids. Use an id from capability_brief exactly.\n"
            "Unknown capability ids are invalid. If no visible capability matches, do not guess an id; reply, guide, or delegate_plan instead.\n"
            "Do not call the same capability again with the same resolved arguments after you already have a fresh result. Answer from that observation or change strategy.\n"
            "When a direct target inspection capability is visible and the player is asking what they are currently looking at, prefer that live read before delegate_explore.\n"
            "If observation_brief already identifies the current target block or entity, answer directly instead of rereading the same target.\n"
            "When the player explicitly asks you to use wiki or local Minecraft knowledge, prefer wiki.page.get for a specific concept if it is visible.\n"
            "When the player asks for category/property/list-style Minecraft knowledge, prefer wiki.category.find, wiki.template.find, wiki.template_param.find, wiki.infobox.find, wiki.backlinks.find, or wiki.section.find when one matches the request.\n"
            "When observation_brief already identifies a current block or entity and the player then asks what it is used for, how to get it, or how it behaves, prefer wiki.page.get with that observed English name if it is visible.\n"
            'wiki.page.get uses {"title":"..."} and can resolve redirects, including English page names and names derived from ids without the minecraft: prefix.\n'
            'wiki.category.find uses {"category":"...","limit":8}; wiki.template_param.find uses {"template_name":"...","param_name":"...","param_value":"...","limit":8}; wiki.section.find uses {"section_title":"...","limit":8}.\n'
            "If a wiki result reports that the requested title resolved to a different canonical page, explicitly mention that redirect resolution in the reply before summarizing the result.\n"
            "If a structured wiki retrieval returns zero results, do not keep retrying minor synonym or parameter-name variants more than once. Explain the limitation, narrow the query, or ask for a concrete page instead.\n"
            "For broad wiki list results, prefer a few representative gameplay-relevant pages over mechanically echoing the first titles in alphabetical order.\n"
            "When the player explicitly asks you to first explore on your own before answering, prefer agent.explore.delegate if it is visible.\n"
            "When the player explicitly asks for a plan but says not to take over, prefer agent.plan.delegate if it is visible.\n"
            "When the player asks for technical state, diagnostics, or observability and observe.technical or carpet.observability.read is visible, prefer one of those capabilities over replying from scene hints alone.\n"
            "If observe.technical or carpet.observability.read already produced a fresh snapshot this turn, answer from that snapshot instead of rereading the same technical state.\n"
            "When the player asks how far they are from the thing they are currently looking at and distance measurement is visible, inspect the target if needed and then use carpet.distance.measure.\n"
            "If a target read already produced a locked target or observation_brief identifies the target, use carpet.distance.measure next instead of rereading the same target.\n"
            "If a target read already returned target_found=false once for this same question, do not repeat the same target read again unless the player clearly changed aim; explain what is missing instead.\n"
            "When the player asks for more detail about a just-identified target block and Carpet block diagnostics are visible, prefer carpet.block_info.read.\n"
            "If a follow-up asks for more detail about the same target and observation_brief has a locked block subject, use carpet.block_info.read next instead of a generic fallback reply.\n"
            "When the player asks what nearby place is worth visiting and a POI capability is visible, prefer observe.poi or world.poi.read over delegate_explore.\n"
            "When the player sounds panicked or asks whether the current night situation is safe, and a scene or threats capability is visible, prefer a fresh threat read before reassuring them.\n"
            "If you relied on player_memory, append exactly one <oai-mem-citation> block as the very last content of final_reply.\n"
            "The citation block is hidden from the player and will be stripped before display, so keep all visible wording before it.\n"
            "Use citation_entries lines formatted as relative_path:start-end|note=[short note].\n"
            "Use thread_ids (or rollout_ids) with one relevant thread id per line.\n"
            "Return JSON only.\n"
            'Reply/guide with {"intent":"reply","final_reply":"..."} or {"intent":"guide","final_reply":"..."}.\n'
            'Inspect/retrieve/execute with {"intent":"execute","capability_request":{"capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":false}}.\n'
            'Delegate with {"intent":"delegate_explore","delegate_role":"explore","delegate_objective":"..."} or {"intent":"delegate_plan","delegate_role":"plan","delegate_objective":"..."}.\n'
            'When confirmation is still needed for an executable capability, use {"intent":"await_confirmation","capability_request":{"capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":true},"confirmation_request":{"effect_summary":"...","reason":"..."}}.\n'
            'If `active_task_candidate` is present, set `"task_selection":"reuse_active"` when the user is clearly continuing it; otherwise set `"task_selection":"keep_current"`.'
        )

    def _runtime_policy_payload(self, request: TurnStartRequest, turn_state: TurnState) -> dict[str, Any]:
        return {
            "language": "Simplified Chinese by default",
            "server_env": request.server_env.model_dump(),
            "player_role": request.player.role,
            "limits": request.limits.model_dump(),
            "task": self._task_header_view(turn_state.task),
            "active_task_candidate": (
                self._task_header_view(turn_state.active_task_candidate)
                if turn_state.active_task_candidate is not None
                else None
            ),
            "persona": {
                "style": "gentle, attentive, concise, situationally playful",
                "voice_rules": [
                    "Guide before taking over when direct execution is not necessary.",
                    "Do not overtalk or over-roleplay.",
                    "Use natural Simplified Chinese unless the user clearly asks for another language.",
                ],
            },
            "notes": [
                "Prefer read capabilities for world truth.",
                "Use recovery refs instead of repeating long content.",
                "Delegate exploration or planning when it reduces uncertainty without polluting the main context.",
                "Bridge actions remain in the main turn only.",
            ],
            "runtime_notes": turn_state.runtime_notes[-4:],
        }

    def _normalize_snapshot(self, scoped_snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "player": self._coerce_mapping(scoped_snapshot.get("player")),
            "world": self._coerce_mapping(scoped_snapshot.get("world")),
            "scene": self._coerce_mapping(scoped_snapshot.get("scene")),
            "interactables": self._coerce_mapping(scoped_snapshot.get("interactables")),
            "social": self._coerce_mapping(scoped_snapshot.get("social")),
            "technical": self._coerce_mapping(scoped_snapshot.get("technical")),
            "target_block": self._coerce_mapping(scoped_snapshot.get("target_block") or scoped_snapshot.get("target")),
            "recent_events": scoped_snapshot.get("recent_events") if isinstance(scoped_snapshot.get("recent_events"), list) else [],
            "server_rules_refs": self._coerce_mapping(scoped_snapshot.get("server_rules_refs")),
            "risk_state": self._coerce_mapping(scoped_snapshot.get("risk_state")),
        }

    def _build_scene_slice(self, normalized_snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "player": normalized_snapshot.get("player"),
            "world": normalized_snapshot.get("world"),
            "scene": normalized_snapshot.get("scene"),
            "interactables": normalized_snapshot.get("interactables"),
            "social": normalized_snapshot.get("social"),
            "technical": normalized_snapshot.get("technical"),
            "target_block": normalized_snapshot.get("target_block"),
            "recent_events": list(normalized_snapshot.get("recent_events") or [])[-12:],
            "server_rules_refs": normalized_snapshot.get("server_rules_refs"),
            "risk_state": normalized_snapshot.get("risk_state"),
        }

    def _build_task_focus(self, turn_state: TurnState) -> dict[str, Any]:
        active_observations = sorted(turn_state.observations, key=lambda item: item.salience, reverse=True)
        observation_refs = [observation.context_entry() for observation in turn_state.observations]
        turn_state.working_memory.active_observations = active_observations
        turn_state.working_memory.observation_refs = observation_refs
        turn_state.working_memory.recovery_refs = self._collect_observation_recovery_refs(turn_state)
        return {
            "task": self._task_header_view(turn_state.task),
            "active_task_candidate": (
                self._task_header_view(turn_state.active_task_candidate)
                if turn_state.active_task_candidate is not None
                else None
            ),
            "working_memory": self._task_focus_working_memory_view(turn_state),
            "current_trigger": {
                "user_message": TurnStartRequest.model_validate(turn_state.request).user_message,
                "pending_confirmation": turn_state.pending_confirmation,
            },
        }

    def _build_observation_brief(self, turn_state: TurnState) -> dict[str, Any]:
        latest_observations: list[dict[str, Any]] = []
        for observation in reversed(turn_state.observations[-6:]):
            entry = self._observation_brief_entry(observation)
            entry["created_at"] = observation.created_at
            latest_observations.append(entry)
        block_subject_lock = turn_state.block_subject_lock.model_dump() if turn_state.block_subject_lock is not None else None
        return {
            "available": bool(latest_observations or block_subject_lock or turn_state.runtime_notes),
            "latest_observations": latest_observations,
            "block_subject_lock": block_subject_lock,
            "runtime_notes": turn_state.runtime_notes[-4:],
        }

    def _observation_brief_entry(self, observation: ObservationRef) -> dict[str, Any]:
        entry = observation.context_entry()
        if not self._is_delegate_observation_source(observation.source):
            return entry
        compact = {
            "observation_id": observation.observation_id,
            "source": observation.source,
            "summary": observation.summary,
            "salience": observation.salience,
        }
        role = self._delegate_role_from_source(observation.source)
        payload = observation.payload if isinstance(observation.payload, dict) else {}
        if role is not None:
            compact["delegate_role"] = role
        if observation.artifact_ref is not None:
            compact["artifact_ref"] = observation.artifact_ref.context_ref()
        unresolved = None
        delegate_result = payload.get("delegate_result")
        if isinstance(delegate_result, dict):
            summary = delegate_result.get("summary")
            if isinstance(summary, dict):
                unresolved = summary.get("unresolved_questions")
        if isinstance(unresolved, list) and unresolved:
            compact["pending_questions"] = unresolved[:3]
        elif isinstance(unresolved, dict):
            items = unresolved.get("items")
            if isinstance(items, list) and items:
                compact["pending_questions"] = items[:3]
        return compact

    def _build_confirmation_loop(self, turn_state: TurnState) -> dict[str, Any]:
        pending = turn_state.pending_confirmation
        if pending is None:
            return {"pending": False}
        return {
            "pending": True,
            "confirmation_id": pending.get("confirmation_id"),
            "effect_summary": pending.get("effect_summary"),
            "open_loops": list(turn_state.working_memory.open_loops),
        }

    def _compact_history(
        self,
        thread_id: str,
        thread_turns: list[dict[str, Any]],
        turn_state: TurnState,
    ) -> dict[str, Any]:
        if len(thread_turns) <= self._settings.context_recent_full_turns:
            return {
                "current_trigger": {"turn_id": turn_state.turn_id},
                "older_turn_count": 0,
                "session_compact_summary": None,
                "recovery_refs": [],
            }

        older_turns = thread_turns[: -self._settings.context_recent_full_turns]
        summary_lines = [
            "Mina Compact Summary",
            "",
            "1. Task Continuity",
            f"- Current task: {turn_state.task.goal}",
            f"- Current task status: {turn_state.task.status}",
            "",
            "2. Earlier Turns",
        ]
        for turn in older_turns:
            summary_lines.append(
                f"- {turn['created_at']}: user={turn['user_message']!r}; status={turn['status']}; reply={turn.get('final_reply') or ''!r}"
            )
        summary_lines.extend(
            [
                "",
                "3. Side Effects And Preferences",
                "- Confirm exact effects or preferences via artifacts, memory search, or transcript before acting.",
                "",
                "4. Recovery Rule",
                f"- Read the full transcript at {self._store.thread_dir(thread_id) / 'transcript.jsonl'} when exact wording matters.",
            ]
        )
        compact_summary = "\n".join(summary_lines)
        summary_record = self._store.write_compact_summary(
            thread_id,
            compact_summary,
            metadata={"older_turn_count": len(older_turns), "task_id": turn_state.task.task_id},
        )
        return {
            "current_trigger": {"turn_id": turn_state.turn_id},
            "older_turn_count": len(older_turns),
            "session_compact_summary": {
                "summary_excerpt": self._summary_excerpt(compact_summary),
                "path": summary_record["path"],
                "transcript_path": summary_record["transcript_path"],
            },
            "recovery_refs": [
                {"kind": "compact_summary", "path": summary_record["path"]},
                {"kind": "transcript", "path": summary_record["transcript_path"]},
            ],
        }

    def _collect_recovery_refs(
        self,
        turn_state: TurnState,
        compacted_history: dict[str, Any],
        session_summary: dict[str, Any] | None,
        retrieved_memory: list[Any],
        player_memory: dict[str, Any],
    ) -> list[dict[str, Any]]:
        refs = self._collect_observation_recovery_refs(turn_state)
        refs.extend(compacted_history.get("recovery_refs", []))
        if session_summary and session_summary.get("transcript_path"):
            refs.append({"kind": "session_summary", "path": session_summary["transcript_path"]})
        for memory in retrieved_memory:
            refs.extend(memory.context_entry().get("artifact_refs", []))
        if isinstance(player_memory, dict):
            refs.extend(player_memory.get("recovery_refs", []))
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ref in refs:
            key = json.dumps(ref, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(ref)
        return unique

    def _build_player_memory_read_path(self, request: TurnStartRequest) -> dict[str, Any]:
        if not self._settings.memories_use:
            return {"available": False, "disabled": True}
        player_uuid = str(request.player.uuid).strip()
        root = self._player_memory_root(player_uuid)
        memory_summary_path = root / "memory_summary.md"
        memory_index_path = root / "MEMORY.md"
        if not memory_summary_path.exists() and not memory_index_path.exists():
            return {"available": False}

        summary_ref = self._memory_summary_ref(memory_summary_path)
        memory_hits = self._search_memory_blocks(memory_index_path, request.user_message)
        recovery_refs = [
            ref
            for ref in [
                {"kind": "memory_summary", "path": str(memory_summary_path)} if memory_summary_path.exists() else None,
                {"kind": "memory_index", "path": str(memory_index_path)} if memory_index_path.exists() else None,
            ]
            if ref is not None
        ]
        return {
            "available": True,
            "player_uuid": player_uuid,
            "player_name": request.player.name,
            "memory_root": str(root),
            "memory_summary": str(summary_ref.get("excerpt") or ""),
            "memory_summary_ref": summary_ref,
            "memory_hits": memory_hits,
            "recovery_refs": recovery_refs,
        }

    def _memory_summary_ref(self, path: Path, *, max_lines: int = 24) -> dict[str, Any]:
        text = self._read_text(path)
        raw_lines = text.splitlines()
        excerpt_lines = raw_lines[:max_lines]
        return {
            "path": str(path),
            "citation_path": path.name,
            "line_start": 1,
            "line_end": len(excerpt_lines),
            "excerpt": "\n".join(excerpt_lines).strip(),
            "truncated": len(raw_lines) > len(excerpt_lines),
        }

    def _player_memory_root(self, player_uuid: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", player_uuid.strip())[:96] or "unknown"
        return self._settings.data_dir / "memories" / "players" / safe

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _search_memory_blocks(self, memory_index_path: Path, query: str, *, max_hits: int = 2) -> list[dict[str, Any]]:
        text = self._read_text(memory_index_path)
        if not text.strip():
            return []
        blocks = self._memory_index_blocks(text)
        query_terms = {
            match.group(0).lower()
            for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", query)
        }
        if not query_terms:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for block in blocks:
            block_text = str(block["text"])
            first_line, _, remainder = block_text.partition("\n")
            block_terms = {
                match.group(0).lower()
                for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", block_text)
            }
            overlap = len(query_terms & block_terms)
            if overlap <= 0:
                continue
            scored.append(
                (
                    overlap,
                    {
                        "task_group": first_line.strip(),
                        "excerpt": self._summary_excerpt(remainder.strip(), max_chars=420),
                        "path": str(memory_index_path),
                        "citation_path": memory_index_path.name,
                        "line_start": block["line_start"],
                        "line_end": block["line_end"],
                        "thread_ids": self._extract_thread_ids_from_memory_block(block_text),
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [payload for _, payload in scored[:max_hits]]

    def _memory_index_blocks(self, text: str) -> list[dict[str, Any]]:
        lines = text.splitlines()
        blocks: list[dict[str, Any]] = []
        current_lines: list[str] = []
        current_start: int | None = None
        for index, line in enumerate(lines, start=1):
            if line.startswith("# Task Group:"):
                if current_lines and current_start is not None:
                    blocks.append(
                        {
                            "text": "\n".join(current_lines).strip(),
                            "line_start": current_start,
                            "line_end": index - 1,
                        }
                    )
                current_lines = [line.removeprefix("# Task Group:").strip()]
                current_start = index
                continue
            if current_start is not None:
                current_lines.append(line)
        if current_lines and current_start is not None:
            blocks.append(
                {
                    "text": "\n".join(current_lines).strip(),
                    "line_start": current_start,
                    "line_end": len(lines),
                }
            )
        return blocks

    def _extract_thread_ids_from_memory_block(self, block_text: str) -> list[str]:
        thread_ids: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"thread_id=([A-Za-z0-9._:-]+)", block_text):
            thread_id = match.group(1).strip()
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            thread_ids.append(thread_id)
        return thread_ids

    def _memory_citation_rules(self, player_memory: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(player_memory, dict) or not player_memory.get("available"):
            return {"enabled": False}
        return {
            "enabled": True,
            "use_when": "Only when player_memory materially informed the reply.",
            "format": {
                "wrapper": "<oai-mem-citation>...</oai-mem-citation>",
                "citation_entries": "relative_path:start-end|note=[short note]",
                "thread_ids": "one thread id per line",
            },
            "example": (
                "<oai-mem-citation>\n"
                "<citation_entries>\n"
                "MEMORY.md:10-18|note=[player preference]\n"
                "memory_summary.md:1-8|note=[recent continuity]\n"
                "</citation_entries>\n"
                "<thread_ids>\n"
                "thread-123\n"
                "</thread_ids>\n"
                "</oai-mem-citation>"
            ),
        }

    def _collect_observation_recovery_refs(self, turn_state: TurnState) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for observation in turn_state.observations[-8:]:
            if observation.artifact_ref is not None:
                ref = observation.artifact_ref.context_ref()
                if self._is_delegate_observation_source(observation.source):
                    ref = {key: ref[key] for key in ("artifact_id", "kind", "path") if key in ref}
                refs.append(ref)
        return refs

    def _build_recent_dialogue_memory(self, session_summary: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(session_summary, dict):
            return {"available": False}
        metadata = session_summary.get("metadata")
        if not isinstance(metadata, dict):
            return {"available": False}
        recent_window = metadata.get("recent_dialogue_window")
        window = [entry for entry in recent_window if isinstance(entry, dict)] if isinstance(recent_window, list) else []
        last_dialogue_turn = metadata.get("last_dialogue_turn") or metadata.get("recent_dialogue_turn")
        active_dialogue_loop = metadata.get("active_dialogue_loop")
        last_dialogue_resolution = metadata.get("last_dialogue_resolution")
        continuity_hint = metadata.get("continuity_hint")
        if not window and not isinstance(last_dialogue_turn, dict) and not isinstance(active_dialogue_loop, dict):
            return {"available": False}
        if not window and isinstance(last_dialogue_turn, dict):
            window = [last_dialogue_turn]
        return {
            "available": True,
            "recent_dialogue_window": window[-3:],
            "last_dialogue_turn": last_dialogue_turn,
            "active_dialogue_loop": active_dialogue_loop,
            "last_dialogue_resolution": last_dialogue_resolution,
            "continuity_hint": continuity_hint,
        }

    def _build_dialogue_continuity(self, recent_dialogue_memory: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(recent_dialogue_memory, dict) or not recent_dialogue_memory.get("available"):
            return {"available": False}
        active_dialogue_loop = recent_dialogue_memory.get("active_dialogue_loop")
        if not isinstance(active_dialogue_loop, dict):
            return {"available": False}
        payload = {
            "available": True,
            "active_dialogue_loop": active_dialogue_loop,
        }
        last_dialogue_turn = recent_dialogue_memory.get("last_dialogue_turn")
        if isinstance(last_dialogue_turn, dict):
            payload["last_dialogue_turn"] = last_dialogue_turn
        recent_window = recent_dialogue_memory.get("recent_dialogue_window")
        if isinstance(recent_window, list):
            payload["recent_dialogue_window"] = recent_window[-2:]
        last_dialogue_resolution = recent_dialogue_memory.get("last_dialogue_resolution")
        if isinstance(last_dialogue_resolution, dict):
            payload["last_dialogue_resolution"] = last_dialogue_resolution
        continuity_hint = recent_dialogue_memory.get("continuity_hint")
        if continuity_hint is not None:
            payload["continuity_hint"] = continuity_hint
        return payload

    def _build_dialogue_history(self, recent_turns: list[dict[str, Any]]) -> dict[str, Any]:
        turns: list[dict[str, Any]] = []
        for turn in recent_turns:
            if not isinstance(turn, dict):
                continue
            turns.append(
                {
                    "turn_id": turn.get("turn_id"),
                    "task_id": turn.get("task_id"),
                    "created_at": turn.get("created_at"),
                    "user_message": turn.get("user_message"),
                    "assistant_reply": turn.get("final_reply"),
                    "status": turn.get("status"),
                }
            )
        return {
            "source": "db.turns",
            "window_size": self._settings.context_recent_full_turns,
            "available": bool(turns),
            "turns": turns,
        }

    def _compact_session_summary(self, session_summary: Any) -> Any:
        if not isinstance(session_summary, dict):
            return session_summary
        compacted = {"summary": self._summary_excerpt(session_summary.get("summary"))}
        if session_summary.get("transcript_path"):
            compacted["transcript_path"] = session_summary.get("transcript_path")
        metadata = session_summary.get("metadata")
        if isinstance(metadata, dict):
            compacted["metadata"] = {
                key: metadata[key]
                for key in (
                    "topic",
                    "task_id",
                    "status",
                    "next_best_companion_move",
                    "older_turn_count",
                )
                if key in metadata
            }
        return compacted

    def _render_slots(self, slots: list[ContextSlot]) -> str:
        lines: list[str] = []
        for slot in slots:
            if not slot.included:
                continue
            lines.append(f"[{slot.name}]")
            lines.append(json.dumps(slot.content, ensure_ascii=False, separators=(",", ":"), default=str))
        return "\n\n".join(lines)

    def _task_summary_view(self, summary: Any) -> dict[str, Any]:
        if not isinstance(summary, dict):
            return {}
        allowed_keys = (
            "player_intent",
            "mina_stance",
            "next_best_companion_move",
            "delegate",
            "objective",
            "finding_count",
        )
        return {key: summary[key] for key in allowed_keys if key in summary}

    def _task_header_view(self, task: Any) -> dict[str, Any] | None:
        if task is None:
            return None
        payload = {
            "task_id": getattr(task, "task_id", None),
            "task_type": getattr(task, "task_type", None),
            "goal": getattr(task, "goal", None),
            "status": getattr(task, "status", None),
            "priority": getattr(task, "priority", None),
            "risk_class": getattr(task, "risk_class", None),
            "requires_confirmation": getattr(task, "requires_confirmation", None),
            "continuity_score": getattr(task, "continuity_score", None),
            "summary": self._task_summary_view(getattr(task, "summary", None)),
        }
        return {key: value for key, value in payload.items() if value not in (None, {}, [])}

    def _task_focus_working_memory_view(self, turn_state: TurnState) -> dict[str, Any]:
        working_memory = turn_state.working_memory
        return {
            "primary_goal": working_memory.primary_goal,
            "focus": working_memory.focus or working_memory.primary_goal,
            "current_status": working_memory.current_status,
            "completed_actions": self._summarize_text_list(working_memory.completed_actions),
            "key_facts": self._summarize_text_list(working_memory.key_facts),
            "blockers": self._summarize_text_list(working_memory.blockers),
            "pending_questions": self._summarize_text_list(working_memory.pending_questions),
            "next_best_step": working_memory.next_best_step,
            "open_loops": self._summarize_text_list(working_memory.open_loops),
            "companion_state": working_memory.companion_state,
        }

    def _summary_excerpt(self, text: Any, *, max_chars: int = 320) -> str | None:
        if not isinstance(text, str):
            return None
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 1] + "…"

    def _is_delegate_observation_source(self, source: Any) -> bool:
        return isinstance(source, str) and source.startswith("agent.") and source.endswith(".delegate")

    def _delegate_role_from_source(self, source: Any) -> str | None:
        if not self._is_delegate_observation_source(source):
            return None
        parts = str(source).split(".")
        if len(parts) != 3:
            return None
        return parts[1]

    def _summarize_text_list(self, values: Any, *, max_items: int = 4, max_chars: int = 220) -> list[Any]:
        if not isinstance(values, list):
            return []
        summarized: list[Any] = []
        for item in values[-max_items:]:
            if isinstance(item, str):
                summarized.append(self._summary_excerpt(item, max_chars=max_chars) or item)
            else:
                summarized.append(item)
        return summarized

    def _slot(
        self,
        name: str,
        role: str,
        source: str,
        strategy: str,
        content: Any,
        *,
        priority: int,
        recoverable: bool = False,
    ) -> ContextSlot:
        return ContextSlot(
            name=name,
            role=role,  # type: ignore[arg-type]
            source=source,
            strategy=strategy,
            content=content,
            priority=priority,
            recoverable=recoverable,
        )

    def _coerce_mapping(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return dict(value)
        return None
