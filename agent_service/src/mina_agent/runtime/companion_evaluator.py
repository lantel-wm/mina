from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.protocol import CompanionEvaluateParams
from mina_agent.runtime.deliberation_engine import DeliberationEngine
from mina_agent.schemas import CompanionEvaluateDecision


class CompanionEvaluator:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        deliberation_engine: DeliberationEngine,
    ) -> None:
        self._settings = settings
        self._store = store
        self._deliberation_engine = deliberation_engine

    def evaluate(self, params: CompanionEvaluateParams) -> CompanionEvaluateDecision:
        if not params.signals:
            return CompanionEvaluateDecision(action="drop", selected_signal_ids=[], reason="no_signals")

        thread = self._store.get_thread(params.thread_id)
        player_memory = self._player_memory_snapshot(
            player_uuid=str(params.context.player.uuid),
            signal_text=self._signal_query_text(params),
        )
        recent_events = params.context.scoped_snapshot.get("recent_events")
        recent_turns = self._store.list_recent_thread_turns(params.thread_id, limit=4)
        messages = self._build_messages(
            params=params,
            thread=thread or {},
            player_memory=player_memory,
            recent_turns=recent_turns,
            recent_events=recent_events if isinstance(recent_events, list) else [],
        )
        try:
            result = self._deliberation_engine.evaluate_companion(messages)
            decision = result.payload
        except Exception:
            return self._fallback_decision(params)

        if not decision.selected_signal_ids:
            if decision.action == "drop":
                decision.selected_signal_ids = [params.signals[0].signal_id]
            elif decision.action == "start_turn":
                decision.selected_signal_ids = [signal.signal_id for signal in params.signals[:1]]
        if decision.action == "start_turn" and not str(decision.synthetic_user_message or "").strip():
            decision.synthetic_user_message = self._fallback_synthetic_message(params)
        if decision.action == "defer" and (decision.defer_seconds is None or decision.defer_seconds < 1):
            decision.defer_seconds = 30
        return decision

    def _fallback_decision(self, params: CompanionEvaluateParams) -> CompanionEvaluateDecision:
        top = params.signals[0]
        if top.kind in {"danger_warning", "death_followup"}:
            return CompanionEvaluateDecision(
                action="start_turn",
                selected_signal_ids=[top.signal_id],
                synthetic_user_message=self._fallback_synthetic_message(params),
                reason="fallback_high_priority",
            )
        if top.kind == "player_join_greeting" and self._has_useful_memory_hint(params):
            return CompanionEvaluateDecision(
                action="start_turn",
                selected_signal_ids=[top.signal_id],
                synthetic_user_message=self._fallback_synthetic_message(params),
                reason="fallback_join_with_memory",
            )
        if top.kind in {"advancement_celebration", "milestone_encouragement"}:
            return CompanionEvaluateDecision(
                action="start_turn",
                selected_signal_ids=[signal.signal_id for signal in params.signals[: min(2, len(params.signals))]],
                synthetic_user_message=self._fallback_synthetic_message(params),
                reason="fallback_positive_bundle",
            )
        return CompanionEvaluateDecision(
            action="drop",
            selected_signal_ids=[top.signal_id],
            reason="fallback_restrained_drop",
        )

    def _fallback_synthetic_message(self, params: CompanionEvaluateParams) -> str:
        return (
            "Produce one brief companion-first proactive message for the current player based on the selected companion signals. "
            "Keep it concise, natural, and low-interruption."
        )

    def _has_useful_memory_hint(self, params: CompanionEvaluateParams) -> bool:
        companion_state = params.companion_state if isinstance(params.companion_state, dict) else {}
        last_turn = companion_state.get("last_companion_turn_at")
        return last_turn is None

    def _signal_query_text(self, params: CompanionEvaluateParams) -> str:
        parts: list[str] = []
        for signal in params.signals[:4]:
            parts.append(signal.kind)
            parts.append(json.dumps(signal.payload, ensure_ascii=False, sort_keys=True))
        return "\n".join(parts)

    def _player_memory_snapshot(self, *, player_uuid: str, signal_text: str) -> dict[str, Any]:
        root = self._player_memory_root(player_uuid)
        summary_path = root / "memory_summary.md"
        memory_path = root / "MEMORY.md"
        summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        hits = self._search_memory_blocks(memory_path, signal_text)
        return {
            "available": bool(summary_text.strip() or hits),
            "memory_summary": summary_text[:3000],
            "memory_hits": hits,
        }

    def _player_memory_root(self, player_uuid: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", player_uuid.strip())[:96] or "unknown"
        return self._settings.data_dir / "memories" / "players" / safe

    def _search_memory_blocks(self, memory_index_path: Path, query: str, *, max_hits: int = 2) -> list[dict[str, Any]]:
        if not memory_index_path.exists():
            return []
        text = memory_index_path.read_text(encoding="utf-8")
        blocks = [block.strip() for block in text.split("# Task Group:") if block.strip()]
        query_terms = {
            match.group(0).lower()
            for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", query)
        }
        if not query_terms:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for block in blocks:
            block_terms = {
                match.group(0).lower()
                for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", block)
            }
            overlap = len(query_terms & block_terms)
            if overlap <= 0:
                continue
            first_line, _, remainder = block.partition("\n")
            scored.append(
                (
                    overlap,
                    {
                        "task_group": first_line.strip(),
                        "excerpt": remainder[:800],
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [payload for _, payload in scored[:max_hits]]

    def _build_messages(
        self,
        *,
        params: CompanionEvaluateParams,
        thread: dict[str, Any],
        player_memory: dict[str, Any],
        recent_turns: list[dict[str, Any]],
        recent_events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        system_message = (
            "You are Mina's proactive companion evaluator.\n"
            "Decide whether Mina should proactively start a companion-only turn right now.\n"
            "Be restrained. Prefer silence unless the signal is timely, helpful, and worth interrupting the player's attention later.\n"
            "If the player currently has no active turn, you may still choose drop or defer when the signal is weak or stale.\n"
            "Danger and death follow-up are highest priority. Positive encouragement may bundle up to 2 signals.\n"
            "Return JSON only with keys: action, selected_signal_ids, defer_seconds, synthetic_user_message, reason.\n"
            "If action=start_turn, synthetic_user_message must be a neutral scheduling instruction, not the final player-facing message.\n"
            "If action=defer, provide defer_seconds.\n"
            "If action=drop, selected_signal_ids may be empty or include the signals to remove."
        )
        user_payload = {
            "thread_id": params.thread_id,
            "thread_metadata": thread.get("metadata", {}) if isinstance(thread, dict) else {},
            "signals": [signal.model_dump() for signal in params.signals],
            "context": params.context.model_dump(),
            "companion_state": params.companion_state,
            "delivery_constraints": params.delivery_constraints.model_dump(),
            "player_memory": player_memory,
            "recent_turns": recent_turns,
            "recent_events": recent_events[-8:],
        }
        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ]
