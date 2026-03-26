from __future__ import annotations

import asyncio
import base64
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mina_agent.protocol import (
    CommandExecParams,
    CommandExecResizeParams,
    CommandExecTerminateParams,
    CommandExecWriteParams,
)


OutputEmitter = Callable[[str, str, bool], Awaitable[None]]


@dataclass(slots=True)
class CommandSession:
    process_id: str
    process: asyncio.subprocess.Process
    stream_stdout_stderr: bool
    stream_stdin: bool
    tty: bool
    size: dict[str, int] | None = None


class AppCommandRunner:
    def __init__(self) -> None:
        self._sessions: dict[tuple[int, str], CommandSession] = {}
        self._lock = asyncio.Lock()

    async def exec_command(
        self,
        *,
        connection_key: int,
        params: CommandExecParams,
        output_emitter: OutputEmitter,
    ) -> dict[str, Any]:
        if params.tty:
            raise RuntimeError("PTY mode is not supported by Mina app-server yet.")
        if params.disable_timeout and params.timeout_ms is not None:
            raise RuntimeError("disable_timeout cannot be combined with timeout_ms.")
        if params.disable_output_cap and params.output_bytes_cap is not None:
            raise RuntimeError("disable_output_cap cannot be combined with output_bytes_cap.")
        if (params.stream_stdin or params.stream_stdout_stderr) and not params.process_id:
            raise RuntimeError("process_id is required for streamed command/exec sessions.")

        env = self._build_env(params.env)
        cwd = params.cwd or os.getcwd()
        process_id = params.process_id or f"cmd_{uuid.uuid4().hex[:12]}"
        process = await asyncio.create_subprocess_exec(
            *params.command,
            cwd=str(Path(cwd)),
            env=env,
            stdin=asyncio.subprocess.PIPE if (params.stream_stdin or params.process_id) else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session = CommandSession(
            process_id=process_id,
            process=process,
            stream_stdout_stderr=bool(params.stream_stdout_stderr),
            stream_stdin=bool(params.stream_stdin),
            tty=bool(params.tty),
            size=params.size.model_dump() if params.size is not None else None,
        )
        if params.process_id is not None:
            async with self._lock:
                self._sessions[(connection_key, params.process_id)] = session

        timeout_s: float | None
        if params.disable_timeout:
            timeout_s = None
        elif params.timeout_ms is not None:
            timeout_s = max(params.timeout_ms / 1000.0, 0.001)
        else:
            timeout_s = None

        try:
            if params.stream_stdout_stderr:
                await self._wait_streamed(
                    session,
                    output_emitter=output_emitter,
                    output_bytes_cap=None if params.disable_output_cap else params.output_bytes_cap,
                    timeout_s=timeout_s,
                )
                stdout = ""
                stderr = ""
            else:
                stdout, stderr = await self._wait_buffered(
                    session,
                    output_bytes_cap=None if params.disable_output_cap else params.output_bytes_cap,
                    timeout_s=timeout_s,
                )
            return {
                "exit_code": int(process.returncode or 0),
                "stdout": stdout,
                "stderr": stderr,
            }
        finally:
            if params.process_id is not None:
                async with self._lock:
                    self._sessions.pop((connection_key, params.process_id), None)

    async def write(self, *, connection_key: int, params: CommandExecWriteParams) -> None:
        session = await self._require_session(connection_key, params.process_id)
        if not session.stream_stdin:
            raise RuntimeError(f"command/exec session {params.process_id} does not accept stdin streaming.")
        if params.delta_base64:
            payload = base64.b64decode(params.delta_base64)
            if session.process.stdin is None:
                raise RuntimeError(f"command/exec session {params.process_id} has no writable stdin.")
            session.process.stdin.write(payload)
            await session.process.stdin.drain()
        if params.close_stdin and session.process.stdin is not None:
            session.process.stdin.close()
            await session.process.stdin.wait_closed()

    async def terminate(self, *, connection_key: int, params: CommandExecTerminateParams) -> None:
        session = await self._require_session(connection_key, params.process_id)
        if session.process.returncode is None:
            session.process.terminate()

    async def resize(self, *, connection_key: int, params: CommandExecResizeParams) -> None:
        session = await self._require_session(connection_key, params.process_id)
        session.size = params.size.model_dump()

    async def close_connection(self, connection_key: int) -> None:
        async with self._lock:
            sessions = [
                session
                for (key, _), session in list(self._sessions.items())
                if key == connection_key
            ]
            for session_key in [pair for pair in self._sessions if pair[0] == connection_key]:
                self._sessions.pop(session_key, None)
        for session in sessions:
            if session.process.returncode is None:
                session.process.terminate()

    async def _require_session(self, connection_key: int, process_id: str) -> CommandSession:
        async with self._lock:
            session = self._sessions.get((connection_key, process_id))
        if session is None:
            raise KeyError(f"Unknown process_id: {process_id}")
        return session

    async def _wait_buffered(
        self,
        session: CommandSession,
        *,
        output_bytes_cap: int | None,
        timeout_s: float | None,
    ) -> tuple[str, str]:
        async def _communicate() -> tuple[bytes, bytes]:
            stdout, stderr = await session.process.communicate()
            return stdout or b"", stderr or b""

        if timeout_s is None:
            stdout_bytes, stderr_bytes = await _communicate()
        else:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(_communicate(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                if session.process.returncode is None:
                    session.process.terminate()
                raise RuntimeError("command/exec timed out") from exc
        if output_bytes_cap is not None:
            stdout_bytes = stdout_bytes[:output_bytes_cap]
            stderr_bytes = stderr_bytes[:output_bytes_cap]
        return stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")

    async def _wait_streamed(
        self,
        session: CommandSession,
        *,
        output_emitter: OutputEmitter,
        output_bytes_cap: int | None,
        timeout_s: float | None,
    ) -> None:
        if session.process.stdout is None or session.process.stderr is None:
            raise RuntimeError("command/exec streaming requires stdout and stderr pipes.")

        async def _read_stream(stream_name: str, reader: asyncio.StreamReader) -> None:
            emitted_bytes = 0
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                cap_reached = False
                if output_bytes_cap is not None:
                    remaining = max(output_bytes_cap - emitted_bytes, 0)
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                        cap_reached = True
                    emitted_bytes += len(chunk)
                await output_emitter(
                    stream_name,
                    base64.b64encode(chunk).decode("ascii"),
                    cap_reached,
                )
                if cap_reached:
                    break

        async def _wait() -> None:
            await asyncio.gather(
                _read_stream("stdout", session.process.stdout),
                _read_stream("stderr", session.process.stderr),
            )
            await session.process.wait()

        if timeout_s is None:
            await _wait()
            return
        try:
            await asyncio.wait_for(_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            if session.process.returncode is None:
                session.process.terminate()
            raise RuntimeError("command/exec timed out") from exc

    def _build_env(self, overrides: dict[str, Any] | None) -> dict[str, str]:
        env = dict(os.environ)
        if overrides is None:
            return env
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env
