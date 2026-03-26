from __future__ import annotations

from pathlib import Path

from src.parser import WikiParser
from src.storage import FileStorage, WikiSearchDB, build_sqlite_index, verify_storage

from .conftest import FIXTURES_DIR, load_raw_fixture


def _materialize_processed_dataset(storage: FileStorage, parser: WikiParser) -> None:
    fixture_raw_dir = FIXTURES_DIR / "raw"
    for path in sorted(fixture_raw_dir.glob("*.json")):
        raw_page = load_raw_fixture(path.name)
        raw_path = storage.save_raw_page(raw_page)
        processed = parser.parse_raw_page(raw_page, raw_path=str(raw_path))
        storage.save_processed_page(processed)


def test_build_sqlite_index_and_query_paths(app_config) -> None:
    storage = FileStorage(app_config)
    parser = WikiParser(app_config.parse)
    _materialize_processed_dataset(storage, parser)

    sqlite_path = build_sqlite_index(storage.iter_processed_pages(), app_config.storage.sqlite_path)

    with WikiSearchDB(sqlite_path) as db:
        assert db.get_page_by_title("钻石镐")["title"] == "钻石镐"
        assert db.get_page_by_title("镐子")["title"] == "镐"
        assert {row["title"] for row in db.find_pages_by_category("工具")} >= {"钻石镐", "镐"}
        assert {row["title"] for row in db.find_pages_by_template("信息框/物品")} >= {"钻石镐", "镐"}
        assert [row["title"] for row in db.find_pages_by_template_param("信息框/物品", "durability", "1561")] == ["钻石镐"]
        assert [row["title"] for row in db.find_pages_by_infobox("durability", "1561")] == ["钻石镐"]
        assert [row["title"] for row in db.find_backlinks("黑曜石")] == ["钻石镐"]
        section_rows = db.find_sections_by_title("获取")
        assert any(row["page_title"] == "钻石镐" for row in section_rows)
        bundle = db.get_page_bundle("钻石镐")
        assert bundle is not None
        assert bundle["page"]["title"] == "钻石镐"
        assert bundle["categories"] == ["可合成物品", "工具"]
        assert bundle["infobox"]["durability"] == "1561"
        assert db.table_counts()["pages"] == 5


def test_verify_storage_reports_counts(app_config) -> None:
    storage = FileStorage(app_config)
    parser = WikiParser(app_config.parse)
    _materialize_processed_dataset(storage, parser)
    build_sqlite_index(storage.iter_processed_pages(), app_config.storage.sqlite_path)

    report = verify_storage(storage, app_config.storage.sqlite_path)
    assert report["raw_count"] == 5
    assert report["processed_count"] == 5
    assert report["missing_processed_page_ids"] == []
    assert report["redirect_count"] == 1
    assert report["sqlite_counts"]["pages"] == 5
    assert report["query_smoke"]["title_lookup"] is True
