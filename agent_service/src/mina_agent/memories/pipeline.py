from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mina_agent.config import Settings
from mina_agent.memory.store import Store
from mina_agent.runtime.memory_manager import MemoryManager
from mina_agent.runtime.models import TurnState
from mina_agent.schemas import TurnStartRequest


class MemoryPipeline:
    _PHASE1_KEY = "phase1"
    _PHASE2_KEY = "phase2"

    def __init__(self, settings: Settings, store: Store, memory_manager: MemoryManager) -> None:
        self._settings = settings
        self._store = store
        self._memory_manager = memory_manager
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
        outputs_by_thread = {
            entry["thread_id"]: entry
            for entry in self._store.list_memory_phase1_outputs(limit=512)
        }
        produced: list[dict[str, Any]] = []
        for thread in self._store.list_threads(limit=256, archived=False):
            thread_id = str(thread["thread_id"])
            thread_detail = self._store.read_thread(thread_id, include_turns=True)
            if thread_detail is None:
                continue
            turns = [turn for turn in thread_detail.get("turns", []) if isinstance(turn, dict)]
            if not turns:
                continue
            source_updated_at = max(
                str(turn.get("updated_at") or turn.get("created_at") or "")
                for turn in turns
            )
            if outputs_by_thread.get(thread_id, {}).get("source_updated_at") == source_updated_at:
                produced.append(outputs_by_thread[thread_id])
                continue
            extracted = self._extract_thread_memory(thread_detail, turns)
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

    def _extract_thread_memory(self, thread: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, str]:
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

    def _run_phase2(self, phase1_outputs: list[dict[str, Any]], reason: str) -> None:
        memories_root = self._settings.data_dir / "memories"
        rollout_summaries_dir = memories_root / "rollout_summaries"
        rollout_summaries_dir.mkdir(parents=True, exist_ok=True)

        current_outputs = self._store.list_memory_phase1_outputs(limit=128)
        selected = current_outputs[: min(len(current_outputs), 32)]
        selected_ids = [str(entry["thread_id"]) for entry in selected]
        previous_state = self._store.read_memory_pipeline_state(self._PHASE2_KEY) or {}
        previous_ids = [str(item) for item in previous_state.get("selected_thread_ids", []) if str(item).strip()]

        raw_memories_lines = ["# Raw Memories", ""]
        for entry in selected:
            thread_id = str(entry["thread_id"])
            raw_memories_lines.append(f"## {thread_id}")
            raw_memories_lines.append("")
            raw_memories_lines.append(str(entry["raw_memory"]))
            raw_memories_lines.append("")

            summary_target = rollout_summaries_dir / f"{thread_id}.md"
            summary_target.write_text(
                f"# {thread_id}\n\n{entry['rollout_summary']}\n",
                encoding="utf-8",
            )

        (memories_root / "raw_memories.md").write_text(
            "\n".join(raw_memories_lines).strip() + "\n",
            encoding="utf-8",
        )

        removed = sorted(set(previous_ids) - set(selected_ids))
        added = sorted(set(selected_ids) - set(previous_ids))
        retained = sorted(set(selected_ids) & set(previous_ids))
        (memories_root / "phase2_selection.json").write_text(
            __import__("json").dumps(
                {
                    "reason": reason,
                    "selected_thread_ids": selected_ids,
                    "added": added,
                    "retained": retained,
                    "removed": removed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._store.mark_memory_phase1_selected(
            selected_ids,
            source_updated_at_by_thread={
                str(entry["thread_id"]): str(entry["source_updated_at"])
                for entry in selected
            },
        )
        self._store.write_memory_pipeline_state(
            self._PHASE2_KEY,
            {
                "reason": reason,
                "selected_thread_ids": selected_ids,
                "added": added,
                "retained": retained,
                "removed": removed,
            },
        )

    def _rollout_slug(self, thread: dict[str, Any]) -> str:
        raw = str(thread.get("name") or thread.get("player_name") or thread.get("thread_id") or "thread").strip()
        safe = "".join(ch.lower() if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
        safe = safe.strip("._-")
        return safe[:96] or "thread"
