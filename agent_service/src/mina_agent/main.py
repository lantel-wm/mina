from __future__ import annotations

from typing import Any, Awaitable, Callable


class MissingDependencyApp:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] != "http":
            return
        body = f"Mina agent service dependencies are missing: {self._exception}".encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})


try:
    from mina_agent.api.app import create_app

    app = create_app()
except Exception as exc:  # pragma: no cover - import-time dependency guard
    app = MissingDependencyApp(exc)
