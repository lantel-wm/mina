from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from mina_agent.config import Settings
from mina_agent.schemas import ModelDecision


JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.base_url and self._settings.api_key and self._settings.model)

    def decide(self, messages: list[dict[str, str]]) -> ModelDecision:
        if not self.available():
            raise RuntimeError("OpenAI-compatible provider is not configured.")

        body = {
            "model": self._settings.model,
            "temperature": 0.2,
            "messages": messages,
        }
        request = urllib.request.Request(
            self._settings.base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Model request failed: {exc}") from exc

        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        match = JSON_BLOCK_RE.search(content)
        if not match:
            raise RuntimeError(f"Model returned non-JSON content: {content}")
        return ModelDecision.model_validate_json(match.group(0))
