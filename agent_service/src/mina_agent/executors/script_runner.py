from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from mina_agent.config import Settings


class ScriptRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def execute(self, script: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.enable_dynamic_scripting:
            raise PermissionError("Dynamic scripting is disabled.")

        with tempfile.TemporaryDirectory(prefix="mina-script-") as tmpdir:
            temp_dir = Path(tmpdir)
            script_path = temp_dir / "script.py"
            payload_path = temp_dir / "payload.json"
            script_path.write_text(script, encoding="utf-8")
            payload_path.write_text(json.dumps(arguments), encoding="utf-8")

            bootstrap = textwrap.dedent(
                """
                import builtins
                import json
                import os
                import resource
                import runpy
                import socket
                import sys
                from pathlib import Path

                resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
                resource.setrlimit(resource.RLIMIT_AS, ({mem}, {mem}))

                def _deny(*args, **kwargs):
                    raise RuntimeError("network access is disabled in Mina script sandbox")

                socket.socket = _deny
                builtins.__dict__["MINA_SCRIPT_ACTIONS"] = []

                payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
                globals_dict = {{
                    "__name__": "__main__",
                    "INPUTS": payload,
                    "emit_action": lambda action: builtins.__dict__["MINA_SCRIPT_ACTIONS"].append(action),
                }}

                exec(Path(sys.argv[1]).read_text(encoding="utf-8"), globals_dict, globals_dict)
                if len(builtins.__dict__["MINA_SCRIPT_ACTIONS"]) > {max_actions}:
                    raise RuntimeError("script emitted too many actions")
                print(json.dumps({{"ok": True, "actions": builtins.__dict__["MINA_SCRIPT_ACTIONS"]}}))
                """
            ).format(
                mem=self._settings.script_memory_mb * 1024 * 1024,
                max_actions=self._settings.script_max_actions,
            )

            result = subprocess.run(
                [sys_executable(), "-I", "-S", "-c", bootstrap, str(script_path), str(payload_path)],
                cwd=temp_dir,
                env={"PYTHONHASHSEED": "0"},
                capture_output=True,
                text=True,
                timeout=self._settings.script_timeout_seconds,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "sandbox execution failed")
            return json.loads(result.stdout)


def sys_executable() -> str:
    return os.environ.get("MINA_AGENT_SCRIPT_PYTHON", "/usr/bin/python3")
