from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mina_agent.retrieval.wiki_store import WikiKnowledgeStore


class WikiKnowledgeStoreTests(unittest.TestCase):
    def test_backlinks_exposes_redirect_resolution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "wiki.db"
            _seed_wiki_store_db(db_path)
            store = WikiKnowledgeStore(db_path, default_limit=8, max_limit=20)

            result = store.find_backlinks("红石", 8)

        self.assertTrue(result["redirect_resolved"])
        self.assertEqual(result["requested_title"], "红石")
        self.assertEqual(result["resolved_title"], "红石粉")
        self.assertIn("红石粉", result["redirect_note"])
        self.assertEqual([item["title"] for item in result["results"][:2]], ["信标", "TNT"])

    def test_category_ranking_prefers_gameplay_block_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "wiki.db"
            _seed_wiki_store_db(db_path)
            store = WikiKnowledgeStore(db_path, default_limit=8, max_limit=20)

            result = store.find_by_category("方块", 8)
            titles = [item["title"] for item in result["results"]]

        self.assertEqual(titles[0], "信标")
        self.assertIn("TNT", titles[:6])
        self.assertIn("压力板", titles[:4])
        self.assertGreater(titles.index("A Minecraft Movie"), titles.index("信标"))
        self.assertGreater(titles.index("南瓜种子"), titles.index("TNT"))

    def test_infobox_ranking_downranks_media_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "wiki.db"
            _seed_wiki_store_db(db_path)
            store = WikiKnowledgeStore(db_path, default_limit=8, max_limit=20)

            result = store.find_by_infobox("light", "15", 8)
            titles = [item["title"] for item in result["results"]]

        self.assertEqual(titles[:2], ["信标", "火把"])
        self.assertGreater(titles.index("A Minecraft Movie"), titles.index("信标"))


def _seed_wiki_store_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE pages (
                page_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                normalized_title TEXT NOT NULL,
                ns INTEGER NOT NULL,
                rev_id INTEGER NOT NULL,
                is_redirect INTEGER NOT NULL,
                redirect_target TEXT,
                plain_text TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                processed_path TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL,
                ord INTEGER NOT NULL,
                level INTEGER NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE TABLE categories (
                page_id INTEGER NOT NULL,
                category TEXT NOT NULL
            );
            CREATE TABLE wikilinks (
                page_id INTEGER NOT NULL,
                target_title TEXT NOT NULL,
                display_text TEXT NOT NULL
            );
            CREATE TABLE templates (
                page_id INTEGER NOT NULL,
                template_name TEXT NOT NULL
            );
            CREATE TABLE template_params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL,
                template_name TEXT NOT NULL,
                param_name TEXT NOT NULL,
                param_value TEXT NOT NULL
            );
            CREATE TABLE infobox_kv (
                page_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL
            );
            CREATE INDEX idx_pages_title ON pages(title);
            CREATE INDEX idx_pages_normalized_title ON pages(normalized_title);
            CREATE INDEX idx_pages_redirect_target ON pages(redirect_target);
            CREATE INDEX idx_categories_category ON categories(category);
            CREATE INDEX idx_wikilinks_target_title ON wikilinks(target_title);
            CREATE INDEX idx_infobox_kv_lookup ON infobox_kv(key, value);
            """
        )
        connection.executemany(
            """
            INSERT INTO pages(
                page_id, title, normalized_title, ns, rev_id, is_redirect, redirect_target,
                plain_text, raw_path, processed_path, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "红石", "红石", 0, 1, 1, "红石粉", "红石重定向到红石粉。", "raw/1", "processed/1", "2026-03-24T00:00:00Z"),
                (2, "红石粉", "红石粉", 0, 2, 0, None, "红石粉是重要的红石材料。", "raw/2", "processed/2", "2026-03-24T00:00:00Z"),
                (3, "信标", "信标", 0, 3, 0, None, "信标是一种功能方块，可以提供状态效果并发出光柱。", "raw/3", "processed/3", "2026-03-24T00:00:00Z"),
                (4, "TNT", "TNT", 0, 4, 0, None, "TNT 是一种能够爆炸的方块，常见于红石装置和陷阱。", "raw/4", "processed/4", "2026-03-24T00:00:00Z"),
                (5, "压力板", "压力板", 0, 5, 0, None, "压力板是常见的红石方块，可以探测实体。", "raw/5", "processed/5", "2026-03-24T00:00:00Z"),
                (6, "南瓜种子", "南瓜种子", 0, 6, 0, None, "南瓜种子主要是物品，但条目里也提到了南瓜茎。", "raw/6", "processed/6", "2026-03-24T00:00:00Z"),
                (7, "A Minecraft Movie", "A Minecraft Movie", 0, 7, 0, None, "电影条目，和游戏内方块玩法无关。", "raw/7", "processed/7", "2026-03-24T00:00:00Z"),
                (8, "火把", "火把", 0, 8, 0, None, "火把是一种常见光源方块。", "raw/8", "processed/8", "2026-03-24T00:00:00Z"),
            ],
        )
        connection.executemany(
            "INSERT INTO categories(page_id, category) VALUES (?, ?)",
            [
                (2, "红石"),
                (2, "方块"),
                (3, "方块"),
                (3, "功能方块"),
                (3, "结构"),
                (4, "方块"),
                (4, "红石"),
                (5, "方块"),
                (5, "机制"),
                (5, "红石"),
                (6, "方块"),
                (6, "物品"),
                (6, "植物"),
                (7, "方块"),
                (7, "电影"),
                (8, "方块"),
            ],
        )
        connection.executemany(
            "INSERT INTO wikilinks(page_id, target_title, display_text) VALUES (?, ?, ?)",
            [
                (3, "红石粉", "红石粉"),
                (4, "红石粉", "红石粉"),
                (7, "红石粉", "红石粉"),
            ],
        )
        connection.executemany(
            "INSERT INTO infobox_kv(page_id, key, value) VALUES (?, ?, ?)",
            [
                (3, "light", "15"),
                (7, "light", "15"),
                (8, "light", "15"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
