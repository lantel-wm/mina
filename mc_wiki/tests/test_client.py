from __future__ import annotations

import httpx

from src.client import MediaWikiClient


def test_client_recovers_from_bad_configured_api_path(app_config) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.url.params.get("action", "")))
        if request.url.path == "/w/api.php" and request.url.params.get("meta") == "siteinfo":
            return httpx.Response(
                301,
                headers={"location": "https://example.test/w/Api.php?action=query"},
            )
        if request.url.path == "/w/api.php" and request.url.params.get("list") == "allpages":
            return httpx.Response(
                301,
                headers={"location": "https://example.test/w/Api.php?action=query"},
            )
        if request.url.path == "/w/Api.php":
            return httpx.Response(404, text="not found")
        if request.url.path == "/api.php" and request.url.params.get("meta") == "siteinfo":
            return httpx.Response(
                200,
                json={"query": {"general": {"sitename": "Minecraft Wiki"}}},
                headers={"content-type": "application/json; charset=utf-8"},
            )
        if request.url.path == "/api.php" and request.url.params.get("list") == "allpages":
            return httpx.Response(
                200,
                json={
                    "query": {
                        "allpages": [{"pageid": 1, "ns": 0, "title": "钻石镐"}]
                    }
                },
                headers={"content-type": "application/json; charset=utf-8"},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    app_config.wiki.base_url = "https://example.test"
    app_config.wiki.api_path = "/w/api.php"
    client = MediaWikiClient(app_config, transport=httpx.MockTransport(handler))
    pages, next_token = client.list_allpages(namespace=0)

    assert pages == [{"pageid": 1, "ns": 0, "title": "钻石镐"}]
    assert next_token is None
    assert client.api_url == "https://example.test/api.php"
    assert ("/api.php", "query") in requests
