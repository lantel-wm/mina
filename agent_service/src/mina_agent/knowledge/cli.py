from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mina_agent.config import Settings
from mina_agent.knowledge.service import KnowledgeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mina-knowledge", description="Manage Mina's local knowledge base.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export-vanilla", help="Export vanilla structured data from a local server jar.")
    export_parser.add_argument("--server-jar", required=True, type=Path)
    export_parser.add_argument("--version", default=None)

    import_parser = subparsers.add_parser("import-facts", help="Import exported vanilla facts into knowledge.sqlite.")
    import_parser.add_argument("--version", default=None)

    subparsers.add_parser("fetch-changelogs", help="Fetch changelog pages defined in the manifest.")
    subparsers.add_parser("fetch-wiki", help="Fetch wiki pages defined in the manifest.")
    subparsers.add_parser("index-semantics", help="Index local explanatory documents into SQLite FTS.")

    rebuild_parser = subparsers.add_parser("rebuild-all", help="Run export, fact import, fetch, and semantic indexing.")
    rebuild_parser.add_argument("--server-jar", required=True, type=Path)
    rebuild_parser.add_argument("--version", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.load()
    service = KnowledgeService(settings)

    if args.command == "export-vanilla":
        result = service.export_vanilla(args.server_jar, version=args.version)
    elif args.command == "import-facts":
        result = service.import_facts(version=args.version)
    elif args.command == "fetch-changelogs":
        result = service.fetch_changelogs()
    elif args.command == "fetch-wiki":
        result = service.fetch_wiki()
    elif args.command == "index-semantics":
        result = service.index_semantics()
    elif args.command == "rebuild-all":
        result = service.rebuild_all(server_jar=args.server_jar, version=args.version)
    else:  # pragma: no cover - argparse enforces subcommand choices
        parser.error(f"Unknown command: {args.command}")
        return 2

    _emit(result)
    return 0


def _emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
