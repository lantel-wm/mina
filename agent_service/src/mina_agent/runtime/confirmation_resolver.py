from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from mina_agent.runtime.models import TaskState


ConfirmationDisposition = Literal["confirmed", "rejected", "modified"]


@dataclass(slots=True)
class ConfirmationResolution:
    disposition: ConfirmationDisposition
    reply: str | None = None
    action_payload: dict[str, Any] | None = None
    task: TaskState | None = None


class ConfirmationResolver:
    _AFFIRMATIVE = {
        "yes",
        "y",
        "ok",
        "okay",
        "sure",
        "confirm",
        "do it",
        "go ahead",
        "继续",
        "可以",
        "好",
        "好的",
        "确认",
        "行",
        "嗯",
        "同意",
        "开始吧",
    }
    _NEGATIVE = {
        "no",
        "n",
        "stop",
        "cancel",
        "don't",
        "不要",
        "不用",
        "取消",
        "算了",
        "先别",
        "停",
        "不行",
        "拒绝",
    }

    def resolve(
        self,
        *,
        user_message: str,
        pending_confirmation: dict[str, Any] | None,
        task: TaskState | None,
    ) -> ConfirmationResolution | None:
        if pending_confirmation is None:
            return None

        normalized = " ".join(user_message.strip().lower().split())
        if normalized in self._AFFIRMATIVE or normalized.startswith("确认") or normalized.startswith("继续"):
            action_payload = dict(pending_confirmation.get("action_payload", {}))
            action_payload["continuation_id"] = str(uuid.uuid4())
            return ConfirmationResolution(
                disposition="confirmed",
                reply="这一步我接着做。",
                action_payload=action_payload,
                task=self._updated_task(task, "in_progress", requires_confirmation=False),
            )

        if normalized in self._NEGATIVE or normalized.startswith("不要") or normalized.startswith("取消"):
            return ConfirmationResolution(
                disposition="rejected",
                reply="那我先停在这里，不会乱动。",
                task=self._updated_task(task, "canceled", requires_confirmation=False),
            )

        return ConfirmationResolution(
            disposition="modified",
            reply=None,
            action_payload=None,
            task=self._updated_task(task, "analyzing", requires_confirmation=False),
        )

    def _updated_task(
        self,
        task: TaskState | None,
        status: str,
        *,
        requires_confirmation: bool,
    ) -> TaskState | None:
        if task is None:
            return None
        payload = task.model_copy(deep=True)
        payload.status = status  # type: ignore[assignment]
        payload.requires_confirmation = requires_confirmation
        return payload
