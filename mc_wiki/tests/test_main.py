from __future__ import annotations

import json
import sys

from src.main import main
from src.parser import WikiParser
from src.storage import FileStorage, build_sqlite_index

from .conftest import FIXTURES_DIR, load_raw_fixture


def _materialize_index(storage: FileStorage, parser: WikiParser, sqlite_path: str) -> None:
    for path in sorted((FIXTURES_DIR / "raw").glob("*.json")):
        raw_page = load_raw_fixture(path.name)
        raw_path = storage.save_raw_page(raw_page)
        processed = parser.parse_raw_page(raw_page, raw_path=str(raw_path))
        storage.save_processed_page(processed)
    build_sqlite_index(storage.iter_processed_pages(), sqlite_path)


def test_search_cli_title_query_outputs_page_bundle(app_config, tmp_path, capsys) -> None:
    storage = FileStorage(app_config)
    parser = WikiParser(app_config.parse)
    _materialize_index(storage, parser, app_config.storage.sqlite_path)

    old_argv = sys.argv
    try:
        sys.argv = [
            "main.py",
            "--config",
            str(tmp_path / "missing-config.yaml"),
            "search",
            "--sqlite",
            app_config.storage.sqlite_path,
            "--title",
            "镐子",
        ]
        main()
    finally:
        sys.argv = old_argv

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["page"]["title"] == "镐"
    assert payload["page"]["resolved_from"] == "镐子"
