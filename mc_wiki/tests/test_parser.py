from __future__ import annotations

import json
from pathlib import Path

from src.parser import (
    WikiParser,
    clean_plain_text,
    extract_infobox,
    extract_links,
    extract_sections,
    extract_templates,
    parse_redirect,
)

from .conftest import FIXTURES_DIR, load_fixture_json, load_raw_fixture


def test_parse_redirect_supports_english_and_chinese_aliases() -> None:
    assert parse_redirect("#REDIRECT [[镐]]") == "镐"
    assert parse_redirect("#重定向 [[镐#获取|镐子]]") == "镐"
    assert parse_redirect("普通正文") is None


def test_extract_sections_returns_flat_ordered_sections() -> None:
    wikitext = "前言\n== 获取 ==\n内容一\n=== 历史 ===\n内容二\n"
    sections = extract_sections(wikitext)
    assert [(section.level, section.title, section.ord) for section in sections] == [
        (1, "获取", 1),
        (2, "历史", 2),
    ]
    assert sections[0].text == "内容一"
    assert sections[1].text == "内容二"


def test_extract_templates_links_infobox_and_plain_text() -> None:
    raw_page = load_raw_fixture("00000001.json")
    templates = extract_templates(raw_page.wikitext)
    infobox = extract_infobox(templates)
    links = extract_links(raw_page.wikitext)
    plain_text = clean_plain_text(raw_page.wikitext)

    assert templates[0].name == "信息框/物品"
    assert infobox == {"stackable": "否", "durability": "1561"}
    assert [link.target_title for link in links] == ["工具", "钻石矿石", "工作台", "黑曜石"]
    assert "Category:" not in plain_text
    assert "来源" not in plain_text


def test_parse_raw_page_matches_snapshot(app_config) -> None:
    parser = WikiParser(app_config.parse)
    raw_page = load_raw_fixture("00000001.json")
    processed = parser.parse_raw_page(raw_page, raw_path="/tmp/raw/00000001.json")
    actual = processed.to_dict()
    actual.pop("processed_time")
    actual.pop("raw_path")

    expected = load_fixture_json("expected/diamond_pickaxe_processed.json")
    assert actual == expected
