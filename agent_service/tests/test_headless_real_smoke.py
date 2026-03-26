from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    response_content = json.dumps(
        {
            "intent": "reply",
            "mode": "final_reply",
            "final_reply": "你好，我在，今天看起来挺适合慢慢玩。",
        },
        ensure_ascii=False,
    )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("content-length", "0"))
        if length > 0:
            self.rfile.read(length)
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": self.response_content,
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _FakeOpenAIServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@unittest.skipUnless(
    os.getenv("MINA_RUN_HEADLESS_REAL_SMOKE") == "1",
    "Set MINA_RUN_HEADLESS_REAL_SMOKE=1 to run the real-mode headless smoke test.",
)
class HeadlessRealSmokeTest(unittest.TestCase):
    def test_run_real_passes_with_fake_openai_provider(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            scenario_root = Path(tmpdir) / "scenarios"
            scenario_root.mkdir(parents=True, exist_ok=True)
            output_root = Path(tmpdir) / "output"
            scenario_path = scenario_root / "real_smoke_greeting.json"
            scenario_path.write_text(
                json.dumps(
                    {
                        "suite": "real",
                        "scenario_id": "real_smoke_greeting",
                        "world_template": "overworld_day_spawn",
                        "status": "runnable_now",
                        "expectation": "target_state",
                        "actors": [{"actor_id": "player", "name": "Steve", "role": "read_only"}],
                        "turns": [{"actor_id": "player", "message": "Mina，跟我打个招呼，并自然地陪我一句。"}],
                        "assertions": {
                            "expected_final_status": "completed",
                            "forbidden_statuses": ["failed"],
                            "confirmation_expected": False,
                            "max_duration_ms": 120000,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            provider = _FakeOpenAIServer()
            provider.start()
            try:
                env = os.environ.copy()
                pythonpath = str(repo_root / "agent_service" / "src")
                if env.get("PYTHONPATH"):
                    pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
                env["PYTHONPATH"] = pythonpath
                env["MINA_BASE_URL"] = provider.base_url
                env["MINA_API_KEY"] = "test-key"
                env["MINA_MODEL"] = "fake-model"

                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "mina_agent.dev.cli",
                        "run-real",
                        "--scenario-dir",
                        str(scenario_root),
                        "--world-template-dir",
                        "testing/headless/world_templates",
                        "--output-root",
                        str(output_root),
                        "--scenario-id",
                        "real_smoke_greeting",
                        "--max-infra-failures",
                        "1",
                        "--server-ready-timeout",
                        "420",
                    ],
                    cwd=repo_root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=420,
                )
            finally:
                provider.stop()

        self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
        self.assertIn("infra_failures=0", result.stdout)
        self.assertIn("bundle ready for turn", result.stdout)


if __name__ == "__main__":
    unittest.main()
