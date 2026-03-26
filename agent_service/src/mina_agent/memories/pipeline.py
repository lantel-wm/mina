from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.providers.openai_compatible import OpenAICompatibleProvider, ProviderError
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import MinaBaseModel
from mina_agent.schemas import TurnStartRequest


class ConsolidatedPlayerMemory(MinaBaseModel):
    memory_summary_md: str
    memory_md: str


class ExtractedRolloutMemory(MinaBaseModel):
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None = None


class MemoryPipeline:
    _PHASE1_KEY = "phase1"
    _PHASE2_KEY = "phase2"

    def __init__(
        self,
        settings: Settings,
        store: Store,
        memory_manager: MemoryManager,
        *,
        phase1_provider: OpenAICompatibleProvider | None = None,
        phase2_provider: OpenAICompatibleProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._memory_manager = memory_manager
        self._phase1_provider = phase1_provider or OpenAICompatibleProvider(settings)
        self._phase2_provider = phase2_provider or OpenAICompatibleProvider(settings)
        self._refresh_lock = asyncio.Lock()
        self._background_task: asyncio.Task[None] | None = None

    def record_completed_turn(
        self,
        request: TurnStartRequest,
        turn_state: TurnState,
        *,
        final_reply: str,
        status: str,
    ) -> None:
        self._memory_manager.record_turn_memories(
            request,
            turn_state,
            final_reply=final_reply,
            status=status,
        )
        self.kickoff_background_refresh(reason="turn_completed")

    def kickoff_background_refresh(self, *, reason: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = loop.create_task(self._refresh_async(reason=reason))

    async def refresh_async(self, *, reason: str = "manual") -> None:
        await self._refresh_async(reason=reason)

    def refresh_now(self, *, reason: str = "manual") -> None:
        phase1_outputs = self._run_phase1(reason=reason)
        self._run_phase2(phase1_outputs, reason=reason)

    async def _refresh_async(self, *, reason: str) -> None:
        async with self._refresh_lock:
            phase1_outputs = await asyncio.to_thread(self._run_phase1, reason=reason)
            await asyncio.to_thread(self._run_phase2, phase1_outputs, reason)

    def _run_phase1(self, *, reason: str) -> list[dict[str, Any]]:
        if not self._settings.memories_generate:
            self._store.write_memory_pipeline_state(
                self._PHASE1_KEY,
                {
                    "reason": reason,
                    "thread_count": 0,
                    "skipped": "memories_generate_disabled",
                },
            )
            return []
        outputs_by_thread = {
            entry["thread_id"]: entry
            for entry in self._store.list_memory_phase1_outputs(limit=512)
        }
        produced: list[dict[str, Any]] = []
        for thread in self._store.list_threads(limit=256, archived=False):
            thread_id = str(thread["thread_id"])
            if str(thread.get("memory_mode") or "enabled") != "enabled":
                continue
            thread_detail = self._store.read_thread(thread_id, include_turns=True)
            if thread_detail is None:
                continue
            turns = [turn for turn in thread_detail.get("turns", []) if isinstance(turn, dict)]
            if not turns:
                continue
            rollout_events = self._store.read_thread_rollout_events(thread_id)
            source_updated_at = self._source_updated_at(
                thread=thread,
                turns=turns,
                rollout_events=rollout_events,
            )
            if outputs_by_thread.get(thread_id, {}).get("source_updated_at") == source_updated_at:
                produced.append(outputs_by_thread[thread_id])
                continue
            extracted = self._extract_thread_memory(
                thread_detail,
                turns,
                rollout_events=rollout_events,
            )
            self._store.upsert_memory_phase1_output(
                thread_id=thread_id,
                raw_memory=extracted["raw_memory"],
                rollout_summary=extracted["rollout_summary"],
                rollout_slug=extracted["rollout_slug"],
                source_updated_at=source_updated_at,
            )
            produced.append(
                {
                    "thread_id": thread_id,
                    "raw_memory": extracted["raw_memory"],
                    "rollout_summary": extracted["rollout_summary"],
                    "rollout_slug": extracted["rollout_slug"],
                    "source_updated_at": source_updated_at,
                }
            )
        self._store.write_memory_pipeline_state(
            self._PHASE1_KEY,
            {
                "reason": reason,
                "thread_count": len(produced),
            },
        )
        return produced

    def _source_updated_at(
        self,
        *,
        thread: dict[str, Any],
        turns: list[dict[str, Any]],
        rollout_events: list[dict[str, Any]],
    ) -> str:
        candidates = [str(thread.get("updated_at") or "")]
        candidates.extend(
            str(turn.get("updated_at") or turn.get("created_at") or "")
            for turn in turns
            if isinstance(turn, dict)
        )
        candidates.extend(
            str(event.get("ts") or "")
            for event in rollout_events
            if isinstance(event, dict)
        )
        normalized = [candidate for candidate in candidates if candidate.strip()]
        if not normalized:
            return ""
        return max(normalized)

    def _extract_thread_memory(
        self,
        thread: dict[str, Any],
        turns: list[dict[str, Any]],
        *,
        rollout_events: list[dict[str, Any]],
    ) -> dict[str, str]:
        rollout_path = self._store.thread_dir(str(thread["thread_id"])) / "rollout.jsonl"
        rollout_contents = self._render_rollout_contents_for_stage1(
            thread,
            turns,
            rollout_events,
        )
        if rollout_contents.strip():
            extracted = self._extract_thread_memory_with_provider(
                thread=thread,
                rollout_path=rollout_path,
                rollout_contents=rollout_contents,
            )
            if extracted is not None:
                return extracted
            local_from_rollout = self._extract_thread_memory_from_rollout(
                thread,
                rollout_contents=rollout_contents,
                rollout_events=rollout_events,
            )
            if local_from_rollout is not None:
                return local_from_rollout
        return self._extract_thread_memory_from_turns(thread, turns)

    def _extract_thread_memory_from_turns(
        self,
        thread: dict[str, Any],
        turns: list[dict[str, Any]],
    ) -> dict[str, str]:
        player_name = str(thread.get("player_name") or "Player")
        latest_turn = turns[-1]
        lines = [
            f"Thread {thread['thread_id']}",
            f"Player: {player_name}",
            f"Status: {thread.get('status')}",
            "",
            "Turns:",
        ]
        for turn in turns[-8:]:
            user_message = str(turn.get("user_message") or "").strip()
            final_reply = str(turn.get("final_reply") or "").strip()
            status = str(turn.get("status") or "")
            lines.append(f"- user: {user_message}")
            if final_reply:
                lines.append(f"  assistant: {final_reply}")
            if status:
                lines.append(f"  status: {status}")
        rollout_summary = (
            f"{player_name}: {str(latest_turn.get('user_message') or '').strip()} -> "
            f"{str(latest_turn.get('final_reply') or '').strip() or latest_turn.get('status') or 'completed'}"
        ).strip()
        rollout_slug = self._rollout_slug(thread)
        return {
            "raw_memory": "\n".join(lines).strip(),
            "rollout_summary": rollout_summary,
            "rollout_slug": rollout_slug,
        }

    def _extract_thread_memory_with_provider(
        self,
        *,
        thread: dict[str, Any],
        rollout_path: Path,
        rollout_contents: str,
    ) -> dict[str, str] | None:
        if not self._phase1_provider.available():
            return None
        messages = self._build_phase1_messages(
            thread=thread,
            rollout_path=rollout_path,
            rollout_contents=rollout_contents,
        )
        try:
            result = self._phase1_provider.complete_json(messages, ExtractedRolloutMemory)
            payload = result.payload
            raw_memory = str(payload.raw_memory or "").strip()
            rollout_summary = str(payload.rollout_summary or "").strip()
            rollout_slug = str(payload.rollout_slug or "").strip() or self._rollout_slug(thread)
            if not raw_memory or not rollout_summary:
                return None
            return {
                "raw_memory": raw_memory,
                "rollout_summary": rollout_summary,
                "rollout_slug": rollout_slug,
            }
        except Exception:
            return None

    def _build_phase1_messages(
        self,
        *,
        thread: dict[str, Any],
        rollout_path: Path,
        rollout_contents: str,
    ) -> list[dict[str, str]]:
        player_uuid = str(thread.get("player_uuid") or "")
        player_name = str(thread.get("player_name") or "Player")
        system_prompt = (
            "You are Mina's memory writing agent for phase 1.\n"
            "Convert one rollout into JSON with `raw_memory`, `rollout_summary`, and `rollout_slug`.\n"
            "Use rollout evidence only. Do not invent facts. Do not preserve secrets.\n"
            "Focus on durable player-scoped memory: stable preferences, repeated behavior, companion continuity, "
            "useful world knowledge, and proven guidance patterns.\n"
            "If signal is weak, return concise but non-empty fields when there is still some reusable learning.\n"
            "Return JSON only."
        )
        user_payload = {
            "thread_id": str(thread.get("thread_id") or ""),
            "player_uuid": player_uuid,
            "player_name": player_name,
            "rollout_path": str(rollout_path),
            "rendered_rollout": rollout_contents[:60000],
            "output_schema": {
                "raw_memory": "detailed markdown raw memory for this rollout",
                "rollout_summary": "compact summary line for routing and indexing",
                "rollout_slug": "short slug for rollout summary filenames",
            },
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ]

    def _extract_thread_memory_from_rollout(
        self,
        thread: dict[str, Any],
        *,
        rollout_contents: str,
        rollout_events: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        canonical_items = self._canonical_rollout_items(rollout_events)
        if not canonical_items:
            return None
        player_name = str(thread.get("player_name") or "Player")
        lines = [
            f"Thread {thread['thread_id']}",
            f"Player: {player_name}",
            f"Status: {thread.get('status')}",
            "",
            "Rendered rollout:",
            rollout_contents.strip(),
        ]
        latest_user = ""
        latest_assistant = ""
        for item in canonical_items:
            item_kind = str(item.get("item_kind") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item_kind == "user_message":
                latest_user = str(payload.get("text") or payload.get("content") or "").strip() or latest_user
            elif item_kind == "assistant_message":
                latest_assistant = str(payload.get("text") or payload.get("content") or "").strip() or latest_assistant
        if not latest_user:
            return None
        rollout_summary = f"{player_name}: {latest_user} -> {latest_assistant or thread.get('status') or 'completed'}".strip()
        return {
            "raw_memory": "\n".join(lines).strip(),
            "rollout_summary": rollout_summary,
            "rollout_slug": self._rollout_slug(thread),
        }

    def _render_rollout_contents_for_stage1(
        self,
        thread: dict[str, Any],
        turns: list[dict[str, Any]],
        rollout_events: list[dict[str, Any]],
    ) -> str:
        canonical_items = self._canonical_rollout_items(rollout_events)
        if not canonical_items:
            return self._render_turns_fallback_for_stage1(thread, turns)
        lines: list[str] = []
        current_turn_id: str | None = None
        for item in canonical_items:
            turn_id = str(item.get("turn_id") or "")
            if turn_id != current_turn_id:
                current_turn_id = turn_id
                lines.extend(["", f"## Turn {turn_id}"])
            item_kind = str(item.get("item_kind") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item_kind == "user_message":
                text = str(payload.get("text") or "").strip()
                if text:
                    lines.append(f"user: {text}")
            elif item_kind == "assistant_message":
                text = str(payload.get("text") or "").strip()
                if text:
                    lines.append(f"assistant: {text}")
            elif item_kind == "tool_call":
                tool_id = str(payload.get("tool_id") or "")
                arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                status = str(item.get("status") or "")
                if status == "completed":
                    observation = payload.get("observation")
                    observation_preview = (
                        json.dumps(observation, ensure_ascii=False, sort_keys=True)[:500]
                        if isinstance(observation, dict)
                        else ""
                    )
                    lines.append(
                        f"tool_result: {tool_id} status={payload.get('status') or status}"
                        + (f" observation={observation_preview}" if observation_preview else "")
                    )
                else:
                    lines.append(
                        f"tool_call: {tool_id} arguments={json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                    )
            elif item_kind == "approval_request":
                effect_summary = str(payload.get("effect_summary") or "").strip()
                if effect_summary:
                    lines.append(f"approval_request: {effect_summary}")
            elif item_kind:
                text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                lines.append(f"{item_kind}: {text[:500]}")
        rendered = "\n".join(line for line in lines if line is not None).strip()
        return rendered or self._render_turns_fallback_for_stage1(thread, turns)

    def _render_turns_fallback_for_stage1(self, thread: dict[str, Any], turns: list[dict[str, Any]]) -> str:
        lines = [
            f"Thread {thread['thread_id']}",
            f"Player: {thread.get('player_name') or 'Player'}",
            "",
        ]
        for turn in turns[-8:]:
            user_message = str(turn.get("user_message") or "").strip()
            final_reply = str(turn.get("final_reply") or "").strip()
            status = str(turn.get("status") or "")
            if user_message:
                lines.append(f"user: {user_message}")
            if final_reply:
                lines.append(f"assistant: {final_reply}")
            if status:
                lines.append(f"turn_status: {status}")
            lines.append("")
        return "\n".join(lines).strip()

    def _canonical_rollout_items(self, rollout_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items_by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in rollout_events:
            item_id = str(event.get("item_id") or "")
            if not item_id:
                continue
            existing = items_by_id.get(item_id)
            if existing is None:
                items_by_id[item_id] = dict(event)
                order.append(item_id)
                continue
            merged = dict(existing)
            merged.update(event)
            if "payload" in event:
                merged["payload"] = event["payload"]
            items_by_id[item_id] = merged
        return [items_by_id[item_id] for item_id in order]

    def _run_phase2(self, phase1_outputs: list[dict[str, Any]], reason: str) -> None:
        if not self._settings.memories_generate:
            self._store.write_memory_pipeline_state(
                self._PHASE2_KEY,
                {
                    "reason": reason,
                    "player_count": 0,
                    "skipped": "memories_generate_disabled",
                },
            )
            return
        all_outputs = self._store.list_memory_phase1_outputs(limit=512)
        players = {
            str(entry.get("player_uuid") or "").strip()
            for entry in all_outputs
            if str(entry.get("player_uuid") or "").strip()
        }
        processed_players: list[str] = []
        for player_uuid in sorted(players):
            selection = self._store.get_player_phase2_input_selection(
                player_uuid,
                limit=self._settings.memories_max_raw_memories_for_consolidation,
                max_unused_days=self._settings.memories_max_unused_days,
            )
            selected = [entry for entry in selection["selected"] if isinstance(entry, dict)]
            previous_selected = [
                entry for entry in selection["previous_selected"] if isinstance(entry, dict)
            ]
            retained_ids = {
                str(thread_id)
                for thread_id in selection.get("retained_thread_ids", [])
                if str(thread_id).strip()
            }
            selected_ids = [str(entry["thread_id"]) for entry in selected]
            added = [thread_id for thread_id in selected_ids if thread_id not in retained_ids]
            retained = [thread_id for thread_id in selected_ids if thread_id in retained_ids]
            removed = [
                item for item in selection.get("removed", [])
                if isinstance(item, dict)
            ]
            desired_fingerprint = self._selection_fingerprint(selected)
            claim = self._store.try_claim_player_phase2_job(
                player_uuid,
                desired_fingerprint=desired_fingerprint,
            )
            if claim.get("status") != "claimed":
                continue
            ownership_token = str(claim["ownership_token"])
            player_root = self.player_memory_root(player_uuid)
            try:
                self._store.heartbeat_player_phase2_job(player_uuid, ownership_token=ownership_token)
                self._write_player_memory_files(
                    player_root=player_root,
                    player_uuid=player_uuid,
                    selected_entries=selected,
                    previous_selected_entries=previous_selected,
                    reason=reason,
                    selected_ids=selected_ids,
                    added=added,
                    retained=retained,
                    removed=removed,
                )
                self._store.write_memory_pipeline_state(
                    f"{self._PHASE2_KEY}:{player_uuid}",
                    {
                        "reason": reason,
                        "selected_thread_ids": selected_ids,
                        "added": added,
                        "retained": retained,
                        "removed": removed,
                    },
                )
                self._store.mark_player_phase2_job_succeeded(
                    player_uuid,
                    ownership_token=ownership_token,
                    completed_fingerprint=desired_fingerprint,
                )
                self._store.mark_player_memory_phase1_selected(player_uuid, selected)
            except Exception as exc:
                self._store.mark_player_phase2_job_failed(
                    player_uuid,
                    ownership_token=ownership_token,
                    error=str(exc),
                )
                raise
            processed_players.append(player_uuid)
        self._store.write_memory_pipeline_state(
            self._PHASE2_KEY,
            {
                "reason": reason,
                "player_count": len(players),
                "processed_players": processed_players,
            },
        )

    def player_memory_root(self, player_uuid: str) -> Path:
        target = self._settings.data_dir / "memories" / "players" / self._safe_segment(player_uuid)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_player_memory_files(
        self,
        *,
        player_root: Path,
        player_uuid: str,
        selected_entries: list[dict[str, Any]],
        previous_selected_entries: list[dict[str, Any]],
        reason: str,
        selected_ids: list[str],
        added: list[str],
        retained: list[str],
        removed: list[dict[str, Any]],
    ) -> None:
        rollout_summaries_dir = player_root / "rollout_summaries"
        rollout_summaries_dir.mkdir(parents=True, exist_ok=True)
        player_name = (
            str(selected_entries[0].get("player_name") or previous_selected_entries[0].get("player_name") or "Player")
            if selected_entries or previous_selected_entries
            else "Player"
        )
        artifact_entries = self._artifact_entries_for_phase2(
            selected_entries,
            previous_selected_entries,
        )
        rollout_summary_files = self._sync_rollout_summaries(
            rollout_summaries_dir,
            player_uuid=player_uuid,
            entries=artifact_entries,
        )
        raw_memories_lines = ["# Raw Memories", "", f"player_uuid: {player_uuid}", f"player_name: {player_name}", ""]
        memory_blocks: list[str] = [
            "# Mina Player Memory",
            "",
            f"player_uuid: {player_uuid}",
            f"player_name: {player_name}",
            "scope: Durable memory for one player only. Never reuse across different players.",
            "applies_to: player-scoped only; reuse_rule=reuse only for future conversations with the same player_uuid.",
            "",
        ]
        summary_lines = [
            "# Memory Summary",
            "",
            f"player_uuid: {player_uuid}",
            f"player_name: {player_name}",
            f"selected_thread_count: {len(selected_entries)}",
            "",
            "Use only for this player. Do not cross-apply to other players.",
            "",
            "Recent highlights:",
        ]

        if not artifact_entries:
            raw_memories_lines.append("No raw memories yet.")
        if not selected_entries:
            memory_blocks.append("No durable player memory yet.")
            summary_lines.append("- No memory entries yet.")
        for entry in artifact_entries:
            thread_id = str(entry["thread_id"])
            rollout_summary = str(entry.get("rollout_summary") or "").strip()
            raw_memory = str(entry.get("raw_memory") or "").strip()
            source_updated_at = str(entry.get("source_updated_at") or "")
            raw_memories_lines.extend(
                [
                    f"## Thread `{thread_id}`",
                    f"rollout_summary_file: {rollout_summary_files[thread_id, source_updated_at, str(entry.get('rollout_slug') or '')]}",
                    f"updated_at: {source_updated_at}",
                    "",
                    raw_memory or "No raw memory.",
                    "",
                ]
            )
        for index, entry in enumerate(selected_entries, start=1):
            thread_id = str(entry["thread_id"])
            rollout_summary = str(entry.get("rollout_summary") or "").strip()
            source_updated_at = str(entry.get("source_updated_at") or "")
            rollout_summary_file = rollout_summary_files[
                thread_id,
                source_updated_at,
                str(entry.get("rollout_slug") or ""),
            ]
            summary_lines.append(
                f"- thread_id={thread_id}: {rollout_summary or 'No rollout summary.'}"
            )
            keywords = ", ".join(self._keywords_for_entry(entry))
            task_title = rollout_summary or f"Thread {thread_id}"
            memory_blocks.extend(
                [
                    f"# Task Group: player_{self._safe_segment(player_name)}_thread_{index}",
                    "scope: Durable notes distilled from one player's thread history.",
                    (
                        f"applies_to: player_uuid={player_uuid}; thread_id={thread_id}; "
                        "reuse_rule=safe only for future conversations with this same player."
                    ),
                    "",
                    f"## Task 1: {task_title}",
                    "",
                    "### rollout_summary_files",
                    f"- rollout_summaries/{rollout_summary_file} (player_uuid={player_uuid}, thread_id={thread_id}, updated_at={source_updated_at})",
                    "",
                    "### keywords",
                    f"- {keywords}",
                    "",
                    "## Reusable knowledge",
                    f"- {rollout_summary or 'No reusable summary extracted.'}",
                    "",
                ]
            )

        (player_root / "raw_memories.md").write_text("\n".join(raw_memories_lines).strip() + "\n", encoding="utf-8")
        (player_root / "phase2_selection.json").write_text(
            json.dumps(
                {
                    "reason": reason,
                    "player_uuid": player_uuid,
                    "selected_thread_ids": selected_ids,
                    "selected_inputs_this_run": len(selected_entries),
                    "newly_added_since_last_successful_phase2": len(added),
                    "retained_from_last_successful_phase2": len(retained),
                    "removed_from_last_successful_phase2": len(removed),
                    "added": added,
                    "retained": retained,
                    "removed": removed,
                    "phase2_input_selection": self._render_phase2_input_selection(
                        selected_entries,
                        retained,
                        removed,
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fallback_memory_md = "\n".join(memory_blocks).strip() + "\n"
        fallback_memory_summary_md = "\n".join(summary_lines).strip() + "\n"
        consolidated = self._generate_consolidated_player_memory(
            player_root=player_root,
            player_uuid=player_uuid,
            player_name=player_name,
            selected_entries=selected_entries,
            selected_ids=selected_ids,
            added=added,
            retained=retained,
            removed=removed,
            fallback_memory_md=fallback_memory_md,
            fallback_memory_summary_md=fallback_memory_summary_md,
        )
        (player_root / "MEMORY.md").write_text(consolidated.memory_md, encoding="utf-8")
        (player_root / "memory_summary.md").write_text(consolidated.memory_summary_md, encoding="utf-8")

    def _artifact_entries_for_phase2(
        self,
        selected_entries: list[dict[str, Any]],
        previous_selected_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in [*selected_entries, *previous_selected_entries]:
            key = (
                str(entry.get("thread_id") or ""),
                str(entry.get("source_updated_at") or ""),
                str(entry.get("rollout_slug") or ""),
            )
            if key in seen or not key[0]:
                continue
            seen.add(key)
            merged.append(dict(entry))
        return merged

    def _sync_rollout_summaries(
        self,
        rollout_summaries_dir: Path,
        *,
        player_uuid: str,
        entries: list[dict[str, Any]],
    ) -> dict[tuple[str, str, str], str]:
        desired: dict[tuple[str, str, str], str] = {}
        for entry in entries:
            thread_id = str(entry.get("thread_id") or "")
            source_updated_at = str(entry.get("source_updated_at") or "")
            rollout_slug = str(entry.get("rollout_slug") or "")
            key = (thread_id, source_updated_at, rollout_slug)
            file_name = self._rollout_summary_file_name(
                thread_id=thread_id,
                source_updated_at=source_updated_at,
                rollout_slug=rollout_slug,
            )
            desired[key] = file_name
            summary_target = rollout_summaries_dir / file_name
            summary_target.write_text(
                "\n".join(
                    [
                        f"thread_id: {thread_id}",
                        f"player_uuid: {player_uuid}",
                        f"updated_at: {source_updated_at}",
                        f"rollout_slug: {rollout_slug or 'thread'}",
                        "",
                        str(entry.get("rollout_summary") or "").strip() or "No rollout summary.",
                    ]
                ).strip()
                + "\n",
                encoding="utf-8",
            )
        expected_files = set(desired.values())
        for existing in rollout_summaries_dir.glob("*.md"):
            if existing.name not in expected_files:
                existing.unlink()
        return desired

    def _rollout_summary_file_name(
        self,
        *,
        thread_id: str,
        source_updated_at: str,
        rollout_slug: str,
    ) -> str:
        timestamp = re.sub(r"[^0-9A-Za-z]+", "-", source_updated_at).strip("-") or "updated"
        slug = self._safe_segment(rollout_slug or thread_id or "thread")
        return f"{self._safe_segment(thread_id)}__{timestamp}__{slug}.md"

    def _rollout_slug(self, thread: dict[str, Any]) -> str:
        raw = str(thread.get("name") or thread.get("player_name") or thread.get("thread_id") or "thread").strip()
        safe = "".join(ch.lower() if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
        safe = safe.strip("._-")
        return safe[:96] or "thread"

    def _keywords_for_entry(self, entry: dict[str, Any], *, limit: int = 8) -> list[str]:
        text = f"{entry.get('rollout_summary') or ''}\n{entry.get('raw_memory') or ''}"
        seen: list[str] = []
        for match in re.finditer(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", text):
            token = match.group(0).lower()
            if token in seen:
                continue
            seen.append(token)
            if len(seen) >= limit:
                break
        return seen or ["player_memory"]

    def _safe_segment(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        return cleaned[:96] or "unknown"

    def _selection_fingerprint(self, entries: list[dict[str, Any]]) -> str:
        payload = [
            {
                "thread_id": str(entry.get("thread_id") or ""),
                "source_updated_at": str(entry.get("source_updated_at") or ""),
                "rollout_slug": str(entry.get("rollout_slug") or ""),
            }
            for entry in entries
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _generate_consolidated_player_memory(
        self,
        *,
        player_root: Path,
        player_uuid: str,
        player_name: str,
        selected_entries: list[dict[str, Any]],
        selected_ids: list[str],
        added: list[str],
        retained: list[str],
        removed: list[dict[str, Any]],
        fallback_memory_md: str,
        fallback_memory_summary_md: str,
    ) -> ConsolidatedPlayerMemory:
        if not self._phase2_provider.available():
            return ConsolidatedPlayerMemory(
                memory_md=fallback_memory_md,
                memory_summary_md=fallback_memory_summary_md,
            )

        raw_memories = self._read_with_cap(player_root / "raw_memories.md", max_chars=60_000)
        existing_memory_md = self._read_with_cap(player_root / "MEMORY.md", max_chars=24_000)
        existing_memory_summary = self._read_with_cap(player_root / "memory_summary.md", max_chars=12_000)
        messages = self._build_phase2_messages(
            player_root=player_root,
            player_uuid=player_uuid,
            player_name=player_name,
            selected_entries=selected_entries,
            selected_ids=selected_ids,
            added=added,
            retained=retained,
            removed=removed,
            raw_memories=raw_memories,
            existing_memory_md=existing_memory_md,
            existing_memory_summary=existing_memory_summary,
        )
        try:
            result = self._phase2_provider.complete_json(messages, ConsolidatedPlayerMemory)
            payload = result.payload
            if not payload.memory_md.strip() or not payload.memory_summary_md.strip():
                raise ProviderError(
                    "Phase-2 consolidation returned empty files.",
                    parse_status="empty_phase2_output",
                    raw_response_preview=result.raw_response_preview,
                    latency_ms=result.latency_ms,
                )
            return self._record_phase2_agent_run(
                player_root=player_root,
                player_uuid=player_uuid,
                player_name=player_name,
                messages=messages,
                selection={
                    "selected_thread_ids": selected_ids,
                    "added": added,
                    "retained": retained,
                    "removed": removed,
                },
                result_payload=payload,
                fallback_payload=None,
                provider_result={
                    "latency_ms": result.latency_ms,
                    "parse_status": getattr(result, "parse_status", "ok"),
                    "raw_response_preview": result.raw_response_preview,
                    "model": getattr(result, "model", self._settings.model or ""),
                    "temperature": getattr(result, "temperature", 0.2),
                    "message_count": getattr(result, "message_count", len(messages)),
                },
                error=None,
            )
        except Exception as exc:
            fallback = ConsolidatedPlayerMemory(
                memory_md=fallback_memory_md,
                memory_summary_md=fallback_memory_summary_md,
            )
            return self._record_phase2_agent_run(
                player_root=player_root,
                player_uuid=player_uuid,
                player_name=player_name,
                messages=messages,
                selection={
                    "selected_thread_ids": selected_ids,
                    "added": added,
                    "retained": retained,
                    "removed": removed,
                },
                result_payload=None,
                fallback_payload=fallback,
                provider_result=None,
                error=str(exc),
            )

    def _build_phase2_messages(
        self,
        *,
        player_root: Path,
        player_uuid: str,
        player_name: str,
        selected_entries: list[dict[str, Any]],
        selected_ids: list[str],
        added: list[str],
        retained: list[str],
        removed: list[dict[str, Any]],
        raw_memories: str,
        existing_memory_md: str,
        existing_memory_summary: str,
    ) -> list[dict[str, str]]:
        system_prompt = (
            "You are Mina's memory consolidation agent.\n"
            "Your job is to consolidate one player's raw memories into durable, file-based memory.\n"
            "Important: memory is strictly player-scoped. Never mix facts, preferences, or habits across different players.\n"
            "Optimize for future companion quality: preserve stable preferences, social continuity, recurring play style, "
            "trusted guidance patterns, and reusable world-specific knowledge.\n"
            "Do not store secrets. Do not invent facts. Prefer compact, navigable markdown.\n"
            "Treat removed phase-2 inputs as evidence that stale or polluted material should be deleted from durable memory.\n"
            "Return only JSON with keys `memory_summary_md` and `memory_md`.\n"
            "`memory_summary_md` must be concise, navigational, and always mention that it is valid only for this player_uuid.\n"
            "`memory_md` must be a durable handbook for this player, grouped into task groups with rollout summary file references.\n"
            "If signal is weak, still return minimal non-empty files rather than empty strings."
        )
        user_payload = {
            "memory_root": str(player_root),
            "player_uuid": player_uuid,
            "player_name": player_name,
            "phase2_input_selection_text": self._render_phase2_input_selection(
                selected_entries=selected_entries,
                retained_thread_ids=retained,
                removed_entries=removed,
            ),
            "phase2_input_selection": self._render_phase2_input_selection_from_ids(
                player_root=player_root,
                selected_ids=selected_ids,
                added=added,
                retained=retained,
                removed=removed,
            ),
            "existing_memory_summary_md": existing_memory_summary,
            "existing_memory_md": existing_memory_md,
            "raw_memories_md": raw_memories,
            "output_requirements": {
                "memory_summary_md": [
                    "Markdown only.",
                    "State that memory is valid only for this player_uuid.",
                    "Summarize stable preferences, recent continuity cues, and high-value recurring knowledge.",
                ],
                "memory_md": [
                    "Markdown only.",
                    "Use task-grouped structure.",
                    "Include rollout summary file references relative to the player's memory root.",
                    "Do not mention other players.",
                ],
            },
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ]

    def _record_phase2_agent_run(
        self,
        *,
        player_root: Path,
        player_uuid: str,
        player_name: str,
        messages: list[dict[str, str]],
        selection: dict[str, Any],
        result_payload: ConsolidatedPlayerMemory | None,
        fallback_payload: ConsolidatedPlayerMemory | None,
        provider_result: dict[str, Any] | None,
        error: str | None,
    ) -> ConsolidatedPlayerMemory:
        run_dir = self._new_phase2_run_dir(player_root)
        self._write_json(
            run_dir / "session_meta.json",
            {
                "source": "subagent.memory_consolidation",
                "agent_role": "memory_consolidation",
                "player_uuid": player_uuid,
                "player_name": player_name,
                "memory_root": str(player_root),
                "memories_generate": False,
                "allow_bridge_actions": False,
                "allow_nested_delegate": False,
            },
        )
        self._write_json(run_dir / "selection.json", selection)
        self._write_json(run_dir / "prompt.messages.json", {"messages": messages})
        debug_request_builder = getattr(self._phase2_provider, "debug_request_buffer", None)
        if callable(debug_request_builder):
            try:
                debug_request = debug_request_builder(messages)
            except Exception:
                debug_request = None
            if isinstance(debug_request, dict):
                self._write_json(run_dir / "provider_request.json", debug_request)
        rollout_path = run_dir / "rollout.jsonl"
        self._append_jsonl(
            rollout_path,
            {
                "ts": self._utc_now(),
                "type": "session_meta",
                "payload": {
                    "source": "subagent.memory_consolidation",
                    "agent_role": "memory_consolidation",
                    "player_uuid": player_uuid,
                    "player_name": player_name,
                },
            },
        )
        for index, message in enumerate(messages):
            self._append_jsonl(
                rollout_path,
                {
                    "ts": self._utc_now(),
                    "type": "message",
                    "index": index,
                    "role": message.get("role", "user"),
                    "content": str(message.get("content") or ""),
                },
            )
        if provider_result is not None and result_payload is not None:
            self._write_json(run_dir / "response.meta.json", provider_result)
            self._write_json(run_dir / "response.structured.json", result_payload.model_dump())
            self._append_jsonl(
                rollout_path,
                {
                    "ts": self._utc_now(),
                    "type": "assistant_result",
                    "status": "completed",
                    "payload": result_payload.model_dump(),
                },
            )
            return result_payload

        payload = fallback_payload or ConsolidatedPlayerMemory(
            memory_md="# Mina Player Memory\n\nNo durable memory yet.\n",
            memory_summary_md="# Memory Summary\n\nNo durable memory yet.\n",
        )
        self._write_json(run_dir / "response.fallback.json", payload.model_dump())
        if error:
            (run_dir / "error.txt").write_text(error, encoding="utf-8")
        self._append_jsonl(
            rollout_path,
            {
                "ts": self._utc_now(),
                "type": "assistant_result",
                "status": "fallback_completed",
                "error": error,
                "payload": payload.model_dump(),
            },
        )
        return payload

    def _new_phase2_run_dir(self, player_root: Path) -> Path:
        runs_dir = player_root / "phase2_runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = runs_dir / f"{stamp}__{uuid.uuid4().hex[:8]}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _render_phase2_input_selection(
        self,
        selected_entries: list[dict[str, Any]],
        retained_thread_ids: list[str],
        removed_entries: list[dict[str, Any]],
    ) -> str:
        retained = len(retained_thread_ids)
        added = len(selected_entries) - retained
        selected_lines = (
            "\n".join(
                self._render_selected_input_line(
                    entry,
                    retained=str(entry.get("thread_id") or "") in set(retained_thread_ids),
                )
                for entry in selected_entries
            )
            if selected_entries
            else "- none"
        )
        removed_lines = (
            "\n".join(self._render_removed_input_line(entry) for entry in removed_entries)
            if removed_entries
            else "- none"
        )
        return (
            f"- selected inputs this run: {len(selected_entries)}\n"
            f"- newly added since the last successful Phase 2 run: {added}\n"
            f"- retained from the last successful Phase 2 run: {retained}\n"
            f"- removed from the last successful Phase 2 run: {len(removed_entries)}\n\n"
            f"Current selected Phase 1 inputs:\n{selected_lines}\n\n"
            f"Removed from the last successful Phase 2 selection:\n{removed_lines}\n"
        )

    def _render_phase2_input_selection_from_ids(
        self,
        *,
        player_root: Path,
        selected_ids: list[str],
        added: list[str],
        retained: list[str],
        removed: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "memory_root": str(player_root),
            "selected_thread_ids": selected_ids,
            "added_thread_ids": added,
            "retained_thread_ids": retained,
            "removed": removed,
        }

    def _render_selected_input_line(self, entry: dict[str, Any], *, retained: bool) -> str:
        status = "retained" if retained else "added"
        thread_id = str(entry.get("thread_id") or "")
        source_updated_at = str(entry.get("source_updated_at") or "")
        rollout_slug = str(entry.get("rollout_slug") or "")
        rollout_summary_file = self._rollout_summary_file_name(
            thread_id=thread_id,
            source_updated_at=source_updated_at,
            rollout_slug=rollout_slug,
        )
        return (
            f"- [{status}] thread_id={thread_id}, "
            f"rollout_summary_file=rollout_summaries/{rollout_summary_file}"
        )

    def _render_removed_input_line(self, entry: dict[str, Any]) -> str:
        thread_id = str(entry.get("thread_id") or "")
        source_updated_at = str(entry.get("source_updated_at") or "")
        rollout_slug = str(entry.get("rollout_slug") or "")
        rollout_summary_file = self._rollout_summary_file_name(
            thread_id=thread_id,
            source_updated_at=source_updated_at,
            rollout_slug=rollout_slug,
        )
        return (
            f"- thread_id={thread_id}, "
            f"rollout_summary_file=rollout_summaries/{rollout_summary_file}"
        )

    def _read_with_cap(self, path: Path, *, max_chars: int) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        if len(text) <= max_chars:
            return text
        head = text[: max_chars // 2]
        tail = text[-(max_chars - len(head)) :]
        return head + "\n...\n" + tail
