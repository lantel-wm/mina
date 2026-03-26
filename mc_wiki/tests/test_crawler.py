from __future__ import annotations

import json

import httpx

from src.checkpoint import CheckpointStore
from src.client import MediaWikiClient
from src.crawler import WikiCrawler
from src.storage import FileStorage


def test_crawler_full_creates_raw_pages_and_checkpoint(app_config) -> None:
    allpages_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal allpages_calls
        params = dict(request.url.params)
        if params.get("list") == "allpages":
            allpages_calls += 1
            if "apcontinue" not in params:
                return httpx.Response(
                    200,
                    json={
                        "continue": {"apcontinue": "NEXT"},
                        "query": {
                            "allpages": [
                                {"pageid": 1, "ns": 0, "title": "钻石镐"},
                                {"pageid": 2, "ns": 0, "title": "黑曜石"},
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "query": {
                        "allpages": [
                            {"pageid": 3, "ns": 0, "title": "镐"},
                        ]
                    }
                },
            )
        if params.get("prop") == "revisions|categories":
            title_list = params["titles"].split("|")
            page_map = {
                "钻石镐": {
                    "pageid": 1,
                    "ns": 0,
                    "title": "钻石镐",
                    "categories": [{"title": "Category:工具"}],
                    "revisions": [
                        {
                            "revid": 11,
                            "timestamp": "2026-03-20T12:00:00Z",
                            "slots": {"main": {"content": "'''钻石镐'''\n[[Category:工具]]"}},
                        }
                    ],
                },
                "黑曜石": {
                    "pageid": 2,
                    "ns": 0,
                    "title": "黑曜石",
                    "categories": [{"title": "Category:方块"}],
                    "revisions": [
                        {
                            "revid": 22,
                            "timestamp": "2026-03-20T12:00:00Z",
                            "slots": {"main": {"content": "'''黑曜石'''\n[[Category:方块]]"}},
                        }
                    ],
                },
                "镐": {
                    "pageid": 3,
                    "ns": 0,
                    "title": "镐",
                    "categories": [{"title": "Category:工具"}],
                    "revisions": [
                        {
                            "revid": 33,
                            "timestamp": "2026-03-20T12:00:00Z",
                            "slots": {"main": {"content": "'''镐'''\n[[Category:工具]]"}},
                        }
                    ],
                },
            }
            return httpx.Response(
                200,
                json={"query": {"pages": [page_map[title] for title in title_list]}},
            )
        raise AssertionError(f"Unexpected request params: {params}")

    storage = FileStorage(app_config)
    checkpoints = CheckpointStore(app_config.storage.checkpoints_dir)
    client = MediaWikiClient(app_config, transport=httpx.MockTransport(handler))
    crawler = WikiCrawler(app_config, client, storage, checkpoints)

    crawler.crawl_full()

    assert allpages_calls == 2
    assert len(storage.iter_raw_pages()) == 3
    checkpoint = checkpoints.load_allpages()
    assert checkpoint.enumeration_complete is True
    assert checkpoint.enumerated_count == 3
    assert len(checkpoints.iter_discovered_titles()) == 3
