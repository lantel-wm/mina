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
            "You are Mina, a Minecraft server agent runtime. "
            "Do not use keyword routing. "
            "Choose between replying directly or calling one capability at a time. "
            "Treat model outputs as plans with assumptions, not executable commands. "
            "Reply with JSON only. "
            "If you want to answer directly, return "
            '{"mode":"final_reply","final_reply":"..."} . '
            "If you need a capability, return "
            '{"mode":"call_capability","capability_id":"...","arguments":{},"effect_summary":"...","requires_confirmation":false}.'
        )

        sections = [
            self._section("identity.player", request.player.model_dump(), "request.player", "exact_pass_through"),
            self._section("identity.server_env", request.server_env.model_dump(), "request.server_env", "exact_pass_through"),
            self._section("limits", request.limits.model_dump(), "request.limits", "exact_pass_through"),
            self._section("scoped_snapshot", request.scoped_snapshot, "request.scoped_snapshot", "structured_summary_with_preview"),
            self._section(
                "capabilities",
                [descriptor.model_dump() for descriptor in capability_descriptors],
                "resolved_capability_descriptors",
                "all_visible_capabilities",
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
            "capabilities": [descriptor.model_dump() for descriptor in capability_descriptors],
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
                "capabilities": "all_visible_capabilities",
                "pending_confirmation": "include_if_present",
                "scoped_snapshot": "structured_summary_with_preview",
            },
        )

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
