from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models import AppConfig, CrawlConfig, ParseConfig, RawPage, StorageConfig, WikiConfig


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        wiki=WikiConfig(
            base_url="https://example.test",
            api_path="/w/api.php",
            user_agent="TestWikiMirror/0.1 (contact: test@example.com)",
            timeout_sec=5,
            maxlag=1,
        ),
        crawl=CrawlConfig(
            namespace=0,
            page_batch_size=2,
            retry_times=1,
            retry_backoff_sec=0,
            save_raw_json=True,
        ),
        parse=ParseConfig(),
        storage=StorageConfig(
            raw_dir=str(tmp_path / "raw"),
            processed_dir=str(tmp_path / "processed"),
            sqlite_path=str(tmp_path / "sqlite" / "wiki.db"),
            checkpoints_dir=str(tmp_path / "checkpoints"),
        ),
    )


def load_fixture_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_raw_fixture(name: str) -> RawPage:
    return RawPage.from_dict(load_fixture_json(f"raw/{name}"))
