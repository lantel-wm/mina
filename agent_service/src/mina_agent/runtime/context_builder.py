from __future__ import annotations

import json
from typing import Any

from mina_agent.schemas import CapabilityDescriptor, TurnStartRequest


class ContextBuilder:
    def build_messages(
        self,
        request: TurnStartRequest,
        recent_turns: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        capability_descriptors: list[CapabilityDescriptor],
        observations: list[dict[str, Any]],
        pending_confirmation: dict[str, Any] | None,
    ) -> list[dict[str, str]]:
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

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
