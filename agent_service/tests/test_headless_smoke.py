from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.getenv("MINA_RUN_HEADLESS_SMOKE") == "1", "Set MINA_RUN_HEADLESS_SMOKE=1 to run the headless smoke test.")
class HeadlessSmokeTest(unittest.TestCase):
    def test_stub_agent_headless_scenario_passes(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            pythonpath = str(repo_root / "agent_service" / "src")
            if env.get("PYTHONPATH"):
                pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
            env["PYTHONPATH"] = pythonpath

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mina_agent.dev.cli",
                    "run-functional",
                    "--scenario-dir",
                    "testing/headless/functional/scenarios",
                    "--world-template-dir",
                    "testing/headless/world_templates",
                    "--output-root",
                    tmpdir,
                    "--scenario-id",
                    "functional_stub_companion_smoke",
                ],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=360,
            )

            self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
            self.assertIn("[PASS] functional_stub_companion_smoke", result.stdout)


if __name__ == "__main__":
    unittest.main()
