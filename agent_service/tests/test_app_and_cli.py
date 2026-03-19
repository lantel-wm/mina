from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from mina_agent.api.app import create_app
from mina_agent.knowledge.cli import build_parser


class AppAndCliTests(unittest.TestCase):
    def test_cli_parser_accepts_new_knowledge_commands(self) -> None:
        parser = build_parser()

        export_args = parser.parse_args(["export-vanilla", "--server-jar", "server.jar", "--version", "1.21.11"])
        rebuild_args = parser.parse_args(["rebuild-all", "--server-jar", "server.jar"])
        semantic_args = parser.parse_args(["index-semantics"])

        self.assertEqual(export_args.command, "export-vanilla")
        self.assertEqual(str(export_args.server_jar), "server.jar")
        self.assertEqual(export_args.version, "1.21.11")
        self.assertEqual(rebuild_args.command, "rebuild-all")
        self.assertEqual(str(rebuild_args.server_jar), "server.jar")
        self.assertEqual(semantic_args.command, "index-semantics")

    def test_healthz_reports_knowledge_status(self) -> None:
        previous_config = os.environ.get("MINA_AGENT_CONFIG_FILE")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                config_path = root / "config.local.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "data_dir": str(root / "data"),
                            "db_path": str(root / "data" / "mina_agent.db"),
                            "knowledge_dir": str(root / "data" / "knowledge"),
                            "knowledge_db_path": str(root / "data" / "knowledge.sqlite"),
                            "knowledge_cache_dir": str(root / "data" / "knowledge_cache"),
                            "audit_dir": str(root / "data" / "audit"),
                            "debug_dir": str(root / "data" / "debug"),
                            "minecraft_version": "1.21.11",
                        }
                    ),
                    encoding="utf-8",
                )
                os.environ["MINA_AGENT_CONFIG_FILE"] = str(config_path)
                app = create_app()
                health_route = next(route for route in app.routes if getattr(route, "path", None) == "/healthz")

                payload = asyncio.run(health_route.endpoint())

                self.assertTrue(payload["ok"])
                self.assertIn("knowledge_status", payload)
                self.assertIn("knowledge_db_path", payload["knowledge_status"])
                self.assertEqual(payload["knowledge_status"]["semantic_document_count"], 0)
        finally:
            if previous_config is None:
                os.environ.pop("MINA_AGENT_CONFIG_FILE", None)
            else:
                os.environ["MINA_AGENT_CONFIG_FILE"] = previous_config


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
