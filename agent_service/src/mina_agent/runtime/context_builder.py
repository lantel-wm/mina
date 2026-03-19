from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest


@dataclass(slots=True)
class ContextBuildResult:
    messages: list[dict[str, str]]
    sections: list[dict[str, Any]]
    message_stats: dict[str, int]
    composition: dict[str, str]


class ContextBuilder:
    def build_messages(
        self,
        request: TurnStartRequest,
        recent_turns: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        capability_descriptors: list[CapabilityDescriptor],
        observations: list[dict[str, Any]],
        pending_confirmation: dict[str, Any] | None,
    ) -> ContextBuildResult:
        system_prompt = (
            "You are Mina, a natural-language-first Minecraft server agent runtime. "
            "Your default player-facing voice is Chinese. "
            "Unless the user clearly requests another language, write final replies, effect summaries, and confirmation text in Simplified Chinese. "
            "Maintain a clear anime heroine persona with a moe, slightly tsundere voice: cute, spirited, a little proud, and warm underneath. "
            "Use natural Chinese phrasing with light touches such as soft modal particles, playful confidence, or mildly tsundere phrasing when appropriate, "
            "but do not become verbose, melodramatic, or roleplay-heavy. "
            "Stay concise, competent, and execution-focused. "
            "The persona must never override safety policy, capability boundaries, confirmation requirements, or factual accuracy. "
            "Do not use keyword routing. "
            "Choose between replying directly or calling one capability at a time. "
            "For authoritative Minecraft facts such as recipes, loot tables, tags, commands, registries, block states, and local server rules, "
            "prefer retrieval.minecraft_facts.lookup. "
            "For explanatory material such as changelogs, wiki notes, common pitfalls, and local guidance, "
            "prefer retrieval.minecraft_semantics.search. "
            "If a semantic search result says verification_required is true, you must call retrieval.minecraft_facts.lookup before final_reply. "
            "Treat model outputs as plans with assumptions, not executable commands. "
            "If the current observations already identify the thing the user asked about, prefer a final reply. "
            "If an observation already includes a block position and you still need to inspect the same block, "
            "call a capability with an explicit block_pos instead of re-reading the live target. "
            "Reply with JSON only. "
            "If you want to answer directly, return "
            '{"mode":"final_reply","final_reply":"..."} . '
            "If you need a capability, return "
            '{"mode":"call_capability","capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":false}.'
        )

        capability_payloads = [self._capability_context_payload(descriptor) for descriptor in capability_descriptors]

        sections = [
            self._section("identity.player", request.player.model_dump(), "request.player", "exact_pass_through"),
            self._section("identity.server_env", request.server_env.model_dump(), "request.server_env", "exact_pass_through"),
            self._section("limits", request.limits.model_dump(), "request.limits", "exact_pass_through"),
            self._section("scoped_snapshot", request.scoped_snapshot, "request.scoped_snapshot", "structured_summary_with_preview"),
            self._section(
                "capabilities",
                capability_payloads,
                "resolved_capability_descriptors",
                "scoped_capability_descriptors",
            ),
            self._section("recent_turns", recent_turns, "store.turns", "tail(limit=6)"),
            self._section("memories", memories, "store.memories", "tail(limit=6)"),
            self._section("observations", observations, "runtime_state.local_observations", "include_all_current_step"),
            self._section("pending_confirmation", pending_confirmation, "store.pending_confirmations", "include_if_present"),
            self._section("user_message", request.user_message, "request.user_message", "exact_pass_through"),
        ]

        user_payload = {
            "identity": {
                "player": request.player.model_dump(),
                "server_env": request.server_env.model_dump(),
            },
            "limits": request.limits.model_dump(),
            "scoped_snapshot": request.scoped_snapshot,
            "capabilities": capability_payloads,
            "recent_turns": recent_turns,
            "memories": memories,
            "observations": observations,
            "pending_confirmation": pending_confirmation,
            "user_message": request.user_message,
        }

        user_content = json.dumps(user_payload, ensure_ascii=False)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        return ContextBuildResult(
            messages=messages,
            sections=sections,
            message_stats={
                "message_count": len(messages),
                "system_chars": len(system_prompt),
                "user_chars": len(user_content),
                "total_chars": len(system_prompt) + len(user_content),
            },
            composition={
                "recent_turns": "tail(limit=6)",
                "memories": "tail(limit=6)",
                "observations": "include_all_current_step",
                "capabilities": "scoped_capability_descriptors",
                "pending_confirmation": "include_if_present",
                "scoped_snapshot": "structured_summary_with_preview",
            },
        )

    def _capability_context_payload(self, descriptor: CapabilityDescriptor) -> dict[str, Any]:
        return {
            "id": descriptor.id,
            "kind": descriptor.kind,
            "risk_class": descriptor.risk_class,
            "execution_mode": descriptor.execution_mode,
            "requires_confirmation": descriptor.requires_confirmation,
            "description": descriptor.description,
        }

    def _section(self, name: str, value: Any, source: str, strategy: str) -> dict[str, Any]:
        return {
            "name": name,
            "source": source,
            "strategy": strategy,
            "included": value is not None,
            "item_count": self._item_count(value),
            "full_chars": self._serialized_length(value),
            "preview": value,
            "truncated": False,
        }

    def _item_count(self, value: Any) -> int | None:
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        return None

    def _serialized_length(self, value: Any) -> int:
        if value is None:
            return 0
        return len(json.dumps(value, ensure_ascii=False, default=str))
