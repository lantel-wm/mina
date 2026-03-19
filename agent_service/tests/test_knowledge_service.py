from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mina_agent.config import Settings
from mina_agent.knowledge.service import KnowledgeService
from mina_agent.knowledge.sources import FetchedPage


class KnowledgeServiceTests(unittest.TestCase):
    def test_build_export_command_uses_official_bundler_entrypoint(self) -> None:
        command = KnowledgeService.build_export_command(Path("/tmp/server.jar"), "--reports")
        self.assertEqual(
            command,
            [
                "java",
                "-DbundlerMainClass=net.minecraft.data.Main",
                "-jar",
                "/tmp/server.jar",
                "--reports",
            ],
        )

    def test_export_vanilla_is_idempotent_when_manifest_exists(self) -> None:
        settings = self._settings()
        service = KnowledgeService(
            settings,
            subprocess_runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runner should not execute")),
        )
        raw_root = settings.knowledge_cache_dir / "vanilla" / settings.minecraft_version / "raw"
        (raw_root / "server").mkdir(parents=True, exist_ok=True)
        (raw_root / "reports").mkdir(parents=True, exist_ok=True)
        (raw_root / "export_manifest.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "version": settings.minecraft_version,
                    "outputs": {"server": "server", "reports": "reports"},
                }
            ),
            encoding="utf-8",
        )

        result = service.export_vanilla(Path("/tmp/server.jar"))

        self.assertEqual(result["status"], "skipped")
        self.assertTrue((raw_root / "export_manifest.json").exists())

    def test_fetch_changelogs_writes_markdown_files(self) -> None:
        settings = self._settings()
        manifest_path = settings.knowledge_dir / "manifests" / "changelogs.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "version": "1.21.11",
                            "url": "https://example.test/changelog-1-21-11",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pages = {
            "https://example.test/changelog-1-21-11": FetchedPage(
                url="https://example.test/changelog-1-21-11",
                title="Minecraft Java Edition 1.21.11",
                content="Technical changes and gameplay fixes.",
                links=[],
            )
        }
        service = KnowledgeService(settings, page_fetcher=lambda url: pages[url])

        result = service.fetch_changelogs()

        output_path = settings.knowledge_dir / "changelogs" / "1.21.11.md"
        self.assertEqual(result["count"], 1)
        self.assertTrue(output_path.exists())
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("Minecraft Java Edition 1.21.11", content)
        self.assertIn("Technical changes and gameplay fixes.", content)

    def test_fetch_changelogs_continues_on_single_page_failure(self) -> None:
        settings = self._settings()
        manifest_path = settings.knowledge_dir / "manifests" / "changelogs.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {"version": "1.21.11", "url": "https://example.test/ok"},
                        {"version": "1.21.10", "url": "https://example.test/fail"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        def page_fetcher(url: str) -> FetchedPage:
            if url.endswith("/fail"):
                raise TimeoutError("timed out")
            return FetchedPage(url=url, title="Ok Page", content="Body", links=[])

        service = KnowledgeService(settings, page_fetcher=page_fetcher)

        result = service.fetch_changelogs()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue((settings.knowledge_dir / "changelogs" / "1.21.11.md").exists())

    def test_fetch_wiki_respects_depth_and_page_limits(self) -> None:
        settings = self._settings()
        manifest_path = settings.knowledge_dir / "manifests" / "wiki_roots.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "roots": [
                        {
                            "root_id": "mechanics",
                            "url": "https://minecraft.wiki/w/Mechanics",
                            "max_depth": 1,
                            "max_pages": 2,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pages = {
            "https://minecraft.wiki/w/Mechanics": FetchedPage(
                url="https://minecraft.wiki/w/Mechanics",
                title="Mechanics",
                content="Overview",
                links=[
                    "https://minecraft.wiki/w/Redstone",
                    "https://minecraft.wiki/w/Automation",
                ],
            ),
            "https://minecraft.wiki/w/Redstone": FetchedPage(
                url="https://minecraft.wiki/w/Redstone",
                title="Redstone",
                content="Signals",
                links=["https://minecraft.wiki/w/Deep_Page"],
            ),
            "https://minecraft.wiki/w/Automation": FetchedPage(
                url="https://minecraft.wiki/w/Automation",
                title="Automation",
                content="Machines",
                links=[],
            ),
            "https://minecraft.wiki/w/Deep_Page": FetchedPage(
                url="https://minecraft.wiki/w/Deep_Page",
                title="Deep Page",
                content="Should not be fetched",
                links=[],
            ),
        }
        service = KnowledgeService(settings, page_fetcher=lambda url: pages[url])

        result = service.fetch_wiki()

        wiki_root = settings.knowledge_dir / "wiki" / "mechanics"
        written_files = sorted(path.name for path in wiki_root.glob("*.md"))
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(written_files), 2)
        self.assertNotIn("Deep_Page.md", written_files)

    def test_import_facts_and_lookup_across_domains(self) -> None:
        settings = self._settings()
        self._write_local_rules(settings)
        self._write_vanilla_export(settings, include_block_states=False)
        service = KnowledgeService(settings)
        service.import_local_rules()

        summary = service.import_facts()

        self.assertEqual(summary["datasets"]["recipe"]["count"], 1)
        self.assertEqual(summary["datasets"]["loot_table"]["count"], 1)
        self.assertEqual(summary["datasets"]["tag"]["count"], 1)
        self.assertEqual(summary["datasets"]["command"]["count"], 3)
        self.assertEqual(summary["datasets"]["registry_entry"]["count"], 2)
        self.assertEqual(summary["datasets"]["block_state"]["status"], "not_indexed")

        recipe = service.lookup_facts("minecraft:oak_planks", domain_hint="recipe")
        loot = service.lookup_facts("minecraft:blocks/stone", domain_hint="loot_table")
        tag = service.lookup_facts("minecraft:items/planks", domain_hint="tag")
        command = service.lookup_facts("/time set", domain_hint="command")
        registry = service.lookup_facts("minecraft:block/minecraft:stone", domain_hint="registry")
        local_rule = service.lookup_facts("creative flight", domain_hint="local_rule")
        block_state = service.lookup_facts("minecraft:stone", domain_hint="block_state")

        self.assertEqual(recipe["results"][0]["dataset"], "recipe")
        self.assertEqual(loot["results"][0]["dataset"], "loot_table")
        self.assertEqual(tag["results"][0]["dataset"], "tag")
        self.assertEqual(command["results"][0]["dataset"], "command")
        self.assertEqual(registry["results"][0]["dataset"], "registry_entry")
        self.assertEqual(local_rule["results"][0]["source_category"], "local_rule")
        self.assertEqual(block_state["result_count"], 0)
        self.assertIn("block_state", block_state["not_indexed"])

    def test_import_facts_fails_when_core_exports_are_missing(self) -> None:
        settings = self._settings()
        raw_root = settings.knowledge_cache_dir / "vanilla" / settings.minecraft_version / "raw"
        (raw_root / "server").mkdir(parents=True, exist_ok=True)
        (raw_root / "reports").mkdir(parents=True, exist_ok=True)
        service = KnowledgeService(settings)

        with self.assertRaisesRegex(RuntimeError, "No core facts were discovered"):
            service.import_facts()

    def test_lookup_facts_prefers_exact_match_before_substring_hits(self) -> None:
        settings = self._settings()
        self._write_vanilla_export(settings, include_block_states=True)
        service = KnowledgeService(settings)
        service.import_facts()

        result = service.lookup_facts("minecraft:stone", domain_hint="block_state")

        self.assertGreaterEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["fact_id"], "minecraft:stone")

    def test_index_semantics_uses_sqlite_fts_and_preserves_priority(self) -> None:
        settings = self._settings()
        self._write_local_rules(settings)
        (settings.knowledge_dir / "wiki" / "mobility").mkdir(parents=True, exist_ok=True)
        (settings.knowledge_dir / "wiki" / "mobility" / "flight.md").write_text(
            "# Flight\n\n通常怎么做：可以用鞘翅飞行，但要注意耐久。\n",
            encoding="utf-8",
        )
        service = KnowledgeService(settings)
        service.import_local_rules()
        service.index_semantics()

        result = service.search_semantics("flight")

        self.assertGreaterEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["source_kind"], "local_rule_text")
        self.assertTrue(result["verification_required"])
        self.assertIn("local_rule", result["fact_domains"])

    def _settings(self) -> Settings:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_dir = root / "data"
        return Settings(
            host="127.0.0.1",
            port=8787,
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="test-model",
            config_file=root / "config.local.json",
            data_dir=data_dir,
            db_path=data_dir / "mina_agent.db",
            knowledge_dir=data_dir / "knowledge",
            knowledge_db_path=data_dir / "knowledge.sqlite",
            knowledge_cache_dir=data_dir / "knowledge_cache",
            audit_dir=data_dir / "audit",
            debug_enabled=False,
            debug_dir=data_dir / "debug",
            debug_string_preview_chars=600,
            debug_list_preview_items=5,
            debug_dict_preview_keys=20,
            debug_event_payload_chars=2000,
            enable_experimental=False,
            enable_dynamic_scripting=False,
            max_agent_steps=8,
            max_retrieval_results=4,
            minecraft_version="1.21.11",
            wiki_fetch_max_depth=2,
            wiki_fetch_max_pages_per_root=20,
            script_timeout_seconds=5,
            script_memory_mb=128,
            script_max_actions=8,
        )

    def _write_local_rules(self, settings: Settings) -> None:
        (settings.knowledge_dir / "local").mkdir(parents=True, exist_ok=True)
        (settings.knowledge_dir / "local" / "server_rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "rule_id": "creative_flight_disabled",
                            "title": "Creative flight disabled",
                            "priority": 120,
                            "scope": "server",
                            "effect": "Creative flight is disabled on this server.",
                            "applies_to": {"roles": ["read_only", "low_risk"]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (settings.knowledge_dir / "local" / "server_rules.md").write_text(
            "# Server Rules\n\nCreative flight is disabled on this server. 常见坑：不要假设创造飞行一定可用。\n",
            encoding="utf-8",
        )

    def _write_vanilla_export(self, settings: Settings, *, include_block_states: bool) -> None:
        raw_root = settings.knowledge_cache_dir / "vanilla" / settings.minecraft_version / "raw"
        server_root = raw_root / "server" / "data" / "minecraft"
        reports_root = raw_root / "reports"
        (server_root / "recipes").mkdir(parents=True, exist_ok=True)
        (server_root / "loot_tables" / "blocks").mkdir(parents=True, exist_ok=True)
        (server_root / "tags" / "items").mkdir(parents=True, exist_ok=True)
        reports_root.mkdir(parents=True, exist_ok=True)

        (server_root / "recipes" / "oak_planks.json").write_text(
            json.dumps(
                {
                    "type": "minecraft:crafting_shapeless",
                    "ingredients": [{"item": "minecraft:oak_log"}],
                    "result": {"id": "minecraft:oak_planks", "count": 4},
                }
            ),
            encoding="utf-8",
        )
        (server_root / "loot_tables" / "blocks" / "stone.json").write_text(
            json.dumps(
                {
                    "type": "minecraft:block",
                    "pools": [{"rolls": 1, "entries": [{"type": "minecraft:item", "name": "minecraft:cobblestone"}]}],
                }
            ),
            encoding="utf-8",
        )
        (server_root / "tags" / "items" / "planks.json").write_text(
            json.dumps({"values": ["minecraft:oak_planks", "minecraft:birch_planks"]}),
            encoding="utf-8",
        )
        (reports_root / "commands.json").write_text(
            json.dumps(
                {
                    "type": "root",
                    "children": {
                        "time": {
                            "type": "literal",
                            "children": {
                                "set": {
                                    "type": "literal",
                                    "children": {
                                        "day": {"type": "argument", "parser": "minecraft:time"}
                                    },
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (reports_root / "registries.json").write_text(
            json.dumps(
                {
                    "minecraft:block": {
                        "entries": {
                            "minecraft:stone": {"protocol_id": 1},
                            "minecraft:oak_planks": {"protocol_id": 2},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        if include_block_states:
            (reports_root / "blocks.json").write_text(
                json.dumps(
                    {
                        "minecraft:stone": {
                            "states": [{"id": 0}],
                            "properties": {},
                        }
                    }
                ),
                encoding="utf-8",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
