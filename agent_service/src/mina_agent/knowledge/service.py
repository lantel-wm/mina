from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Callable

from mina_agent.config import Settings
from mina_agent.knowledge.retriever import SemanticRetriever, SQLiteFtsSemanticRetriever
from mina_agent.knowledge.sources import FetchedPage, fetch_page, normalize_text, normalize_url
from mina_agent.knowledge.store import KnowledgeStore, SEMANTIC_PRIORITY_ORDER


PageFetcher = Callable[[str], FetchedPage]

DOMAIN_DATASETS = {
    "recipe": ["recipe"],
    "loot_table": ["loot_table"],
    "loot": ["loot_table"],
    "tag": ["tag"],
    "command": ["command"],
    "registry": ["registry_entry"],
    "registry_entry": ["registry_entry"],
    "block_state": ["block_state"],
    "local_rule": ["local_rule"],
}

FACT_DOMAIN_KEYWORDS = {
    "recipe": ("recipe", "recipes", "craft", "crafting", "配方", "合成"),
    "loot_table": ("loot", "drop", "drops", "掉落", "战利品"),
    "tag": ("tag", "tags", "标签"),
    "command": ("command", "commands", "命令", "指令"),
    "registry_entry": ("registry", "registries", "注册表"),
    "block_state": ("block state", "block states", "方块状态", "state=", "properties"),
    "local_rule": ("server rule", "server rules", "权限", "规则", "允许", "禁止"),
}

WIKI_FILE_SUFFIXES = {".md", ".txt"}
JSON_SUFFIX = ".json"
BLOCK_REPORT_NAMES = {"blocks.json", "block_states.json"}


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: KnowledgeStore | None = None,
        semantic_retriever: SemanticRetriever | None = None,
        page_fetcher: PageFetcher | None = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or KnowledgeStore(settings.knowledge_db_path)
        self._semantic_retriever = semantic_retriever or SQLiteFtsSemanticRetriever(self._store)
        self._page_fetcher = page_fetcher or fetch_page
        self._subprocess_runner = subprocess_runner or subprocess.run

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    def bootstrap_runtime_indexes(self) -> None:
        self.ensure_layout()
        self.import_local_rules()
        self.index_semantics()

    def ensure_layout(self) -> None:
        self._settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._settings.knowledge_cache_dir.mkdir(parents=True, exist_ok=True)
        (self._settings.knowledge_dir / "local").mkdir(parents=True, exist_ok=True)
        (self._settings.knowledge_dir / "manifests").mkdir(parents=True, exist_ok=True)
        (self._settings.knowledge_dir / "changelogs").mkdir(parents=True, exist_ok=True)
        (self._settings.knowledge_dir / "wiki").mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        stats = self._store.stats()
        stats.update(
            {
                "knowledge_dir": str(self._settings.knowledge_dir),
                "knowledge_db_path": str(self._settings.knowledge_db_path),
                "knowledge_cache_dir": str(self._settings.knowledge_cache_dir),
            }
        )
        return stats

    def lookup_facts(
        self,
        query: str,
        *,
        domain_hint: str | None = None,
        subject_hint: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del subject_hint  # Reserved for future targeted lookups.
        datasets = DOMAIN_DATASETS.get((domain_hint or "").strip().lower())
        results = self._store.search_facts(
            minecraft_version=self._settings.minecraft_version,
            query=query,
            limit=limit or self._settings.max_retrieval_results,
            datasets=datasets,
        )
        source_categories = list(dict.fromkeys(result["source_category"] for result in results))
        stats = self._store.stats()
        not_indexed: list[str] = []
        for dataset in datasets or []:
            if stats["fact_counts"].get(dataset, 0) == 0:
                not_indexed.append(dataset)
        return {
            "query": query,
            "domain_hint": domain_hint,
            "result_count": len(results),
            "results": results,
            "source_categories": source_categories,
            "source_labels": [_source_category_label(item) for item in source_categories],
            "verification_required": False,
            "not_indexed": not_indexed,
        }

    def search_semantics(
        self,
        query: str,
        *,
        source_kinds: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        results = self._semantic_retriever.search(
            minecraft_version=self._settings.minecraft_version,
            query=query,
            limit=limit or self._settings.max_retrieval_results,
            source_kinds=source_kinds,
        )
        fact_domains = sorted(
            {
                domain
                for result in results
                for domain in result.get("metadata", {}).get("fact_domains", [])
                if isinstance(domain, str)
            }
        )
        verification_required = any(bool(result.get("verification_required")) for result in results)
        return {
            "query": query,
            "result_count": len(results),
            "results": results,
            "source_categories": ["semantic_text"] if results else [],
            "source_labels": ["解释性文本"] if results else [],
            "verification_required": verification_required,
            "fact_domains": fact_domains,
        }

    def export_vanilla(self, server_jar: Path, *, version: str | None = None) -> dict[str, Any]:
        version = version or self._settings.minecraft_version
        self.ensure_layout()
        server_jar = server_jar.expanduser().resolve()
        raw_root = self._settings.knowledge_cache_dir / "vanilla" / version / "raw"
        manifest_path = raw_root / "export_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") == "completed":
                outputs = manifest.get("outputs", {})
                if self._outputs_exist(raw_root, outputs):
                    return {
                        "status": "skipped",
                        "version": version,
                        "manifest_path": str(manifest_path),
                        "outputs": outputs,
                    }

        outputs = {
            "server": "server",
            "reports": "reports",
        }
        raw_root.mkdir(parents=True, exist_ok=True)
        command_records: list[dict[str, Any]] = []
        for mode, relative_dir in outputs.items():
            output_dir = raw_root / relative_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            command = self.build_export_command(server_jar, "--server" if mode == "server" else "--reports")
            completed = self._subprocess_runner(
                command,
                cwd=output_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            (raw_root / f"{mode}.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
            (raw_root / f"{mode}.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
            command_records.append(
                {
                    "mode": mode,
                    "command": command,
                    "cwd": str(output_dir),
                    "returncode": completed.returncode,
                }
            )
            if completed.returncode != 0:
                manifest = {
                    "status": "failed",
                    "version": version,
                    "server_jar": str(server_jar),
                    "outputs": outputs,
                    "commands": command_records,
                }
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                self._store.record_import_run(
                    source_kind="vanilla_export",
                    minecraft_version=version,
                    status="failed",
                    source_path=str(server_jar),
                    metadata={"command_records": command_records},
                )
                raise RuntimeError(f"Vanilla export failed for {mode}: exit code {completed.returncode}")

        manifest = {
            "status": "completed",
            "version": version,
            "server_jar": str(server_jar),
            "outputs": outputs,
            "commands": command_records,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self._store.record_import_run(
            source_kind="vanilla_export",
            minecraft_version=version,
            status="completed",
            source_path=str(server_jar),
            metadata={"command_records": command_records},
        )
        return {
            "status": "completed",
            "version": version,
            "manifest_path": str(manifest_path),
            "outputs": outputs,
        }

    def import_local_rules(self) -> dict[str, Any]:
        self.ensure_layout()
        rule_path = self._settings.knowledge_dir / "local" / "server_rules.json"
        if not rule_path.exists():
            return {"status": "skipped", "reason": "missing_local_rule_file", "path": str(rule_path)}

        payload = json.loads(rule_path.read_text(encoding="utf-8"))
        rule_items = payload.get("rules", payload if isinstance(payload, list) else [])
        if not isinstance(rule_items, list):
            raise ValueError("knowledge/local/server_rules.json must contain a top-level rules array.")

        checksum = _sha256_text(rule_path.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = []
        for item in rule_items:
            if not isinstance(item, dict):
                continue
            rule_id = str(item.get("rule_id", "")).strip()
            if not rule_id:
                continue
            priority = int(item.get("priority", 100))
            scope = str(item.get("scope", "server")).strip() or "server"
            effect = str(item.get("effect", "")).strip()
            if not effect:
                continue
            applies_to = item.get("applies_to", {})
            title = str(item.get("title", rule_id)).strip() or rule_id
            search_text = " ".join(
                filter(
                    None,
                    [
                        rule_id,
                        title,
                        scope,
                        effect,
                        " ".join(_flatten_text(applies_to)),
                    ],
                )
            )
            rows.append(
                {
                    "rule_id": rule_id,
                    "priority": priority,
                    "scope": scope,
                    "effect": effect,
                    "applies_to": applies_to if isinstance(applies_to, dict) else {"value": applies_to},
                    "payload_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                    "title": title,
                    "search_text": search_text,
                    "metadata": {"source_kind": "local_rule"},
                }
            )

        self._store.replace_local_rules(
            self._settings.minecraft_version,
            rows,
            source_path=str(rule_path),
            checksum=checksum,
        )
        self._store.record_import_run(
            source_kind="local_rules",
            minecraft_version=self._settings.minecraft_version,
            status="completed",
            source_path=str(rule_path),
            checksum=checksum,
            metadata={"count": len(rows)},
        )
        return {"status": "completed", "count": len(rows), "path": str(rule_path)}

    def import_facts(self, *, version: str | None = None) -> dict[str, Any]:
        version = version or self._settings.minecraft_version
        raw_root = self._settings.knowledge_cache_dir / "vanilla" / version / "raw"
        server_root = raw_root / "server"
        reports_root = raw_root / "reports"
        if not server_root.exists() and not reports_root.exists():
            raise FileNotFoundError(f"No vanilla export found under {raw_root}")

        summary: dict[str, Any] = {"version": version, "datasets": {}}
        dataset_rows = {
            "recipe": _dedupe_fact_rows(self._load_recipe_rows(server_root, version)),
            "loot_table": _dedupe_fact_rows(self._load_loot_rows(server_root, version)),
            "tag": _dedupe_fact_rows(self._load_tag_rows(server_root, version)),
            "command": _dedupe_fact_rows(self._load_command_rows(reports_root, version)),
            "registry_entry": _dedupe_fact_rows(self._load_registry_rows(reports_root, version)),
            "block_state": _dedupe_fact_rows(self._load_block_state_rows(reports_root, version)),
        }
        core_total = sum(
            len(dataset_rows[name])
            for name in ("recipe", "loot_table", "tag", "command", "registry_entry")
        )
        if core_total == 0:
            raise RuntimeError(
                f"No core facts were discovered under {raw_root}. "
                "Make sure export-vanilla completed successfully before importing."
            )
        for dataset, rows in dataset_rows.items():
            if dataset == "block_state" and not rows:
                summary["datasets"][dataset] = {"count": 0, "status": "not_indexed"}
                continue
            self._store.replace_dataset_facts(dataset, version, rows)
            summary["datasets"][dataset] = {"count": len(rows), "status": "completed"}
            self._store.record_import_run(
                source_kind=f"vanilla_{dataset}",
                minecraft_version=version,
                status="completed",
                source_path=str(raw_root),
                metadata={"count": len(rows)},
            )
        return summary

    def fetch_changelogs(self) -> dict[str, Any]:
        self.ensure_layout()
        manifest_path = self._settings.knowledge_dir / "manifests" / "changelogs.json"
        manifest = _load_manifest_entries(manifest_path, "entries")
        target_dir = self._settings.knowledge_dir / "changelogs"
        written: list[str] = []
        written_set: set[str] = set()
        failures: list[dict[str, str]] = []
        for entry in manifest:
            version = str(entry.get("version", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not version or not url:
                continue
            try:
                page = self._page_fetcher(url)
            except Exception as exc:
                failures.append({"version": version, "url": url, "error": str(exc)})
                continue
            content = _compose_markdown(page.title, url, page.content)
            output_path = target_dir / f"{version}.md"
            output_path.write_text(content, encoding="utf-8")
            output_key = str(output_path)
            if output_key not in written_set:
                written.append(output_key)
                written_set.add(output_key)
        if not written and failures:
            raise RuntimeError(f"Failed to fetch changelogs: {failures[0]['url']} -> {failures[0]['error']}")
        status = "completed" if not failures else "partial"
        self._store.record_import_run(
            source_kind="changelog_fetch",
            minecraft_version=self._settings.minecraft_version,
            status=status,
            source_path=str(manifest_path),
            metadata={"count": len(written), "failures": failures},
        )
        return {"status": status, "count": len(written), "paths": written, "failures": failures}

    def fetch_wiki(self) -> dict[str, Any]:
        self.ensure_layout()
        manifest_path = self._settings.knowledge_dir / "manifests" / "wiki_roots.json"
        roots = _load_manifest_entries(manifest_path, "roots")
        written: list[str] = []
        written_set: set[str] = set()
        failures: list[dict[str, str]] = []
        for root in roots:
            root_id = str(root.get("root_id", "")).strip()
            start_url = normalize_url(str(root.get("url", "")).strip())
            if not root_id or not start_url:
                continue
            max_depth = int(root.get("max_depth", self._settings.wiki_fetch_max_depth))
            max_pages = int(root.get("max_pages", self._settings.wiki_fetch_max_pages_per_root))
            queue: deque[tuple[str, int]] = deque([(start_url, 0)])
            visited: set[str] = set()
            target_dir = self._settings.knowledge_dir / "wiki" / root_id
            target_dir.mkdir(parents=True, exist_ok=True)
            while queue and len(visited) < max_pages:
                current_url, depth = queue.popleft()
                current_url = normalize_url(current_url)
                if not current_url or current_url in visited or depth > max_depth:
                    continue
                visited.add(current_url)
                try:
                    page = self._page_fetcher(current_url)
                except Exception as exc:
                    failures.append({"root_id": root_id, "url": current_url, "error": str(exc)})
                    continue
                slug = _wiki_slug(current_url)
                output_path = target_dir / f"{slug}.md"
                output_path.write_text(_compose_markdown(page.title, current_url, page.content), encoding="utf-8")
                output_key = str(output_path)
                if output_key not in written_set:
                    written.append(output_key)
                    written_set.add(output_key)
                if depth >= max_depth:
                    continue
                for link in page.links:
                    if _is_followable_wiki_link(start_url, link):
                        queue.append((link, depth + 1))
        if not written and failures:
            raise RuntimeError(f"Failed to fetch wiki pages: {failures[0]['url']} -> {failures[0]['error']}")
        status = "completed" if not failures else "partial"
        self._store.record_import_run(
            source_kind="wiki_fetch",
            minecraft_version=self._settings.minecraft_version,
            status=status,
            source_path=str(manifest_path),
            metadata={"count": len(written), "failures": failures},
        )
        return {"status": status, "count": len(written), "paths": written, "failures": failures}

    def index_semantics(self) -> dict[str, Any]:
        self.ensure_layout()
        indexed = 0
        for path in self._iter_semantic_source_files():
            content = path.read_text(encoding="utf-8")
            checksum = _sha256_text(content)
            source_kind = self._semantic_source_kind(path)
            minecraft_version = self._semantic_version(path)
            priority = SEMANTIC_PRIORITY_ORDER.get(source_kind, 50)
            metadata = {
                "source_kind": source_kind,
                "url_or_path": str(path),
                "priority": priority,
                "minecraft_version": minecraft_version,
            }
            chunks = _chunk_semantic_document(content, source_kind=source_kind, metadata=metadata)
            if self._store.replace_semantic_document(
                doc_path=str(path),
                title=_document_title(path, content),
                source_kind=source_kind,
                minecraft_version=minecraft_version,
                priority=priority,
                checksum=checksum,
                metadata=metadata,
                chunks=chunks,
            ):
                indexed += 1
        self._store.record_import_run(
            source_kind="semantic_index",
            minecraft_version=self._settings.minecraft_version,
            status="completed",
            source_path=str(self._settings.knowledge_dir),
            metadata={"indexed_documents": indexed},
        )
        return {"status": "completed", "indexed_documents": indexed}

    def rebuild_all(self, *, server_jar: Path, version: str | None = None) -> dict[str, Any]:
        version = version or self._settings.minecraft_version
        return {
            "export": self.export_vanilla(server_jar, version=version),
            "facts": self.import_facts(version=version),
            "changelogs": self.fetch_changelogs(),
            "wiki": self.fetch_wiki(),
            "semantics": self.index_semantics(),
        }

    @staticmethod
    def build_export_command(server_jar: Path, mode_flag: str) -> list[str]:
        return [
            "java",
            "-DbundlerMainClass=net.minecraft.data.Main",
            "-jar",
            str(server_jar),
            mode_flag,
        ]

    def _outputs_exist(self, raw_root: Path, outputs: dict[str, Any]) -> bool:
        for relative_dir in outputs.values():
            output_dir = raw_root / str(relative_dir)
            if not output_dir.exists():
                return False
        return True

    def _load_recipe_rows(self, server_root: Path, version: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in _find_data_pack_json(server_root, {"recipe", "recipes"}):
            payload = _read_json(path)
            namespace, relative = _namespace_relative(path, {"recipe", "recipes"})
            fact_id = f"{namespace}:{relative}"
            result_item, result_count = _recipe_result_summary(payload)
            checksum = _sha256_bytes(path.read_bytes())
            rows.append(
                {
                    "minecraft_version": version,
                    "fact_id": fact_id,
                    "namespace": namespace,
                    "path": relative,
                    "recipe_type": str(payload.get("type", "")),
                    "result_item": result_item,
                    "result_count": result_count,
                    "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "source_path": str(path),
                    "checksum": checksum,
                    "title": result_item or fact_id,
                    "search_text": " ".join(filter(None, [fact_id, result_item or "", *_flatten_text(payload)])),
                    "metadata": {"source_kind": "recipe"},
                    "priority": 60,
                }
            )
        return rows

    def _load_loot_rows(self, server_root: Path, version: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in _find_data_pack_json(server_root, {"loot_table", "loot_tables"}):
            payload = _read_json(path)
            namespace, relative = _namespace_relative(path, {"loot_table", "loot_tables"})
            fact_id = f"{namespace}:{relative}"
            checksum = _sha256_bytes(path.read_bytes())
            rows.append(
                {
                    "minecraft_version": version,
                    "fact_id": fact_id,
                    "namespace": namespace,
                    "path": relative,
                    "loot_type": str(payload.get("type", "")),
                    "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "source_path": str(path),
                    "checksum": checksum,
                    "title": fact_id,
                    "search_text": " ".join(filter(None, [fact_id, *_flatten_text(payload)])),
                    "metadata": {"source_kind": "loot_table"},
                    "priority": 60,
                }
            )
        return rows

    def _load_tag_rows(self, server_root: Path, version: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in _find_data_pack_json(server_root, {"tags"}):
            payload = _read_json(path)
            namespace, relative = _namespace_relative(path, {"tags"})
            parts = relative.split("/", 1)
            tag_group = parts[0] if parts else ""
            fact_id = f"{namespace}:{relative}"
            values = payload.get("values", [])
            checksum = _sha256_bytes(path.read_bytes())
            rows.append(
                {
                    "minecraft_version": version,
                    "fact_id": fact_id,
                    "namespace": namespace,
                    "path": relative,
                    "tag_group": tag_group,
                    "entry_count": len(values) if isinstance(values, list) else 0,
                    "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "source_path": str(path),
                    "checksum": checksum,
                    "title": fact_id,
                    "search_text": " ".join(filter(None, [fact_id, tag_group, *_flatten_text(payload)])),
                    "metadata": {"source_kind": "tag"},
                    "priority": 60,
                }
            )
        return rows

    def _load_command_rows(self, reports_root: Path, version: str) -> list[dict[str, Any]]:
        command_file = _find_named_json(reports_root, {"commands.json"})
        if command_file is None:
            return []
        payload = _read_json(command_file)
        checksum = _sha256_bytes(command_file.read_bytes())
        rows: list[dict[str, Any]] = []

        def visit(node: Any, path_parts: list[str], argument_count: int) -> None:
            if not isinstance(node, dict):
                return
            node_type = str(node.get("type", ""))
            current_argument_count = argument_count + (1 if node_type == "argument" else 0)
            if path_parts:
                command_path = " ".join(path_parts)
                fact_id = f"/{command_path}"
                rows.append(
                    {
                        "minecraft_version": version,
                        "fact_id": fact_id,
                        "command_path": command_path,
                        "argument_count": current_argument_count,
                        "payload_json": json.dumps(node, ensure_ascii=False, sort_keys=True),
                        "source_path": str(command_file),
                        "checksum": checksum,
                        "title": fact_id,
                        "search_text": " ".join(filter(None, [fact_id, *_flatten_text(node)])),
                        "metadata": {"source_kind": "command"},
                        "priority": 60,
                    }
                )
            children = node.get("children", {})
            if isinstance(children, dict):
                for child_name, child_node in children.items():
                    visit(child_node, [*path_parts, str(child_name)], current_argument_count)

        visit(payload, [], 0)
        return rows

    def _load_registry_rows(self, reports_root: Path, version: str) -> list[dict[str, Any]]:
        registry_file = _find_named_json(reports_root, {"registries.json"})
        if registry_file is None:
            return []
        payload = _read_json(registry_file)
        checksum = _sha256_bytes(registry_file.read_bytes())
        rows: list[dict[str, Any]] = []
        if not isinstance(payload, dict):
            return rows
        for registry_key, registry_payload in payload.items():
            for entry_key, entry_payload in _registry_entries(registry_payload):
                fact_id = f"{registry_key}/{entry_key}"
                rows.append(
                    {
                        "minecraft_version": version,
                        "fact_id": fact_id,
                        "registry_key": str(registry_key),
                        "entry_key": str(entry_key),
                        "payload_json": json.dumps(entry_payload, ensure_ascii=False, sort_keys=True),
                        "source_path": str(registry_file),
                        "checksum": checksum,
                        "title": str(entry_key),
                        "search_text": " ".join(filter(None, [fact_id, str(registry_key), str(entry_key), *_flatten_text(entry_payload)])),
                        "metadata": {"source_kind": "registry_entry"},
                        "priority": 60,
                    }
                )
        return rows

    def _load_block_state_rows(self, reports_root: Path, version: str) -> list[dict[str, Any]]:
        block_file = _find_named_json(reports_root, BLOCK_REPORT_NAMES)
        if block_file is None:
            return []
        payload = _read_json(block_file)
        if not isinstance(payload, dict):
            return []
        checksum = _sha256_bytes(block_file.read_bytes())
        rows: list[dict[str, Any]] = []
        for block_id, block_payload in payload.items():
            if not isinstance(block_payload, dict):
                continue
            states = block_payload.get("states", [])
            rows.append(
                {
                    "minecraft_version": version,
                    "fact_id": str(block_id),
                    "block_id": str(block_id),
                    "state_count": len(states) if isinstance(states, list) else 0,
                    "payload_json": json.dumps(block_payload, ensure_ascii=False, sort_keys=True),
                    "source_path": str(block_file),
                    "checksum": checksum,
                    "title": str(block_id),
                    "search_text": " ".join(filter(None, [str(block_id), *_flatten_text(block_payload)])),
                    "metadata": {"source_kind": "block_state"},
                    "priority": 60,
                }
            )
        return rows

    def _iter_semantic_source_files(self) -> list[Path]:
        paths: list[Path] = []
        for path in sorted(self._settings.knowledge_dir.glob("*")):
            if path.is_file() and path.suffix.lower() in WIKI_FILE_SUFFIXES:
                paths.append(path)
        for directory in ("local", "changelogs", "wiki"):
            root = self._settings.knowledge_dir / directory
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in WIKI_FILE_SUFFIXES:
                    paths.append(path)
        return paths

    def _semantic_source_kind(self, path: Path) -> str:
        relative = path.relative_to(self._settings.knowledge_dir)
        parts = relative.parts
        if parts and parts[0] == "local":
            if path.name == "server_rules.md":
                return "local_rule_text"
            return "local_note"
        if parts and parts[0] == "changelogs":
            return "changelog"
        if parts and parts[0] == "wiki":
            return "wiki"
        return "local_note"

    def _semantic_version(self, path: Path) -> str:
        relative = path.relative_to(self._settings.knowledge_dir)
        if relative.parts and relative.parts[0] == "changelogs":
            return path.stem
        return self._settings.minecraft_version


def _source_category_label(source_category: str) -> str:
    return {
        "local_rule": "本地服务器规则",
        "official_structured_fact": "官方结构化事实",
        "semantic_text": "解释性文本",
    }.get(source_category, source_category)


def _load_manifest_entries(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get(key, [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _compose_markdown(title: str, url: str, content: str) -> str:
    body = content.strip()
    header = [f"# {title.strip() or 'Untitled'}", "", f"Source: {url}", ""]
    if body:
        header.append(body)
    return "\n".join(header).strip() + "\n"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_data_pack_json(root: Path, dataset_segments: set[str]) -> list[Path]:
    if not root.exists():
        return []
    matches: list[Path] = []
    for path in sorted(root.rglob("*.json")):
        if _dataset_root_index(path.parts, dataset_segments) is None:
            continue
        matches.append(path)
    return matches


def _namespace_relative(path: Path, dataset_segments: set[str]) -> tuple[str, str]:
    parts = list(path.parts)
    dataset_root = _dataset_root_index(parts, dataset_segments)
    if dataset_root is None:
        return "minecraft", path.stem
    namespace = parts[dataset_root + 1]
    dataset_index = next(
        index for index in range(dataset_root + 2, len(parts)) if parts[index] in dataset_segments
    )
    relative_parts = parts[dataset_index + 1 :]
    relative_path = "/".join(relative_parts)
    if relative_path.endswith(JSON_SUFFIX):
        relative_path = relative_path[: -len(JSON_SUFFIX)]
    return namespace, relative_path


def _dataset_root_index(parts: tuple[str, ...] | list[str], dataset_segments: set[str]) -> int | None:
    candidates = [index for index, part in enumerate(parts) if part == "data"]
    for data_index in reversed(candidates):
        if data_index + 2 >= len(parts):
            continue
        if any(parts[index] in dataset_segments for index in range(data_index + 2, len(parts))):
            return data_index
    return None


def _recipe_result_summary(payload: dict[str, Any]) -> tuple[str | None, int]:
    result = payload.get("result")
    if isinstance(result, str):
        return result, 1
    if isinstance(result, dict):
        item = result.get("id") or result.get("item") or result.get("name")
        count = result.get("count", 1)
        return str(item) if item is not None else None, int(count) if isinstance(count, int) else 1
    return None, 1


def _find_named_json(root: Path, names: set[str]) -> Path | None:
    if not root.exists():
        return None
    for path in sorted(root.rglob("*.json")):
        if path.name in names:
            return path
    return None


def _registry_entries(payload: Any) -> list[tuple[str, Any]]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if isinstance(entries, dict):
        return [(str(key), value) for key, value in entries.items()]
    if any(":" in str(key) for key in payload.keys()):
        return [(str(key), value) for key, value in payload.items()]
    return []


def _flatten_text(value: Any) -> list[str]:
    flattened: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            flattened.append(str(key))
            flattened.extend(_flatten_text(item))
    elif isinstance(value, list):
        for item in value:
            flattened.extend(_flatten_text(item))
    elif isinstance(value, (str, int, float, bool)):
        flattened.append(str(value))
    return [normalize_text(item) for item in flattened if normalize_text(str(item))]


def _chunk_semantic_document(content: str, *, source_kind: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    if not paragraphs:
        paragraphs = [content.strip()]
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        chunk_text = "\n\n".join(current).strip()
        fact_domains = _infer_fact_domains(chunk_text)
        requires_verification = bool(fact_domains) and source_kind in {"local_rule_text", "changelog", "wiki"}
        chunks.append(
            {
                "chunk_index": len(chunks),
                "content": chunk_text,
                "token_count": len(chunk_text.split()),
                "verification_required": requires_verification,
                "metadata": {
                    **metadata,
                    "fact_domains": fact_domains,
                    "verification_required": requires_verification,
                },
            }
        )
        current = []
        current_chars = 0

    for paragraph in paragraphs:
        addition = len(paragraph)
        if current and current_chars + addition > 800:
            flush()
        current.append(paragraph)
        current_chars += addition
    flush()
    return chunks


def _infer_fact_domains(text: str) -> list[str]:
    lowered = text.lower()
    domains = [
        domain
        for domain, keywords in FACT_DOMAIN_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    ]
    return sorted(dict.fromkeys(domains))


def _document_title(path: Path, content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.stem
    return path.stem


def _wiki_slug(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/") or "index"
    slug = path.rsplit("/", 1)[-1] or "index"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug).strip("-")
    if parsed.query:
        query_hash = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{query_hash}"
    return slug or "index"


def _is_followable_wiki_link(root_url: str, candidate_url: str) -> bool:
    parsed_root = urllib.parse.urlparse(root_url)
    parsed_candidate = urllib.parse.urlparse(candidate_url)
    if parsed_candidate.scheme not in {"http", "https"}:
        return False
    if parsed_candidate.netloc != parsed_root.netloc:
        return False
    if parsed_candidate.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
        return False
    if "action=" in parsed_candidate.query:
        return False
    return True


def _dedupe_fact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        fact_id = str(row.get("fact_id", "")).strip()
        if not fact_id or fact_id in deduped:
            continue
        deduped[fact_id] = row
    return list(deduped.values())
