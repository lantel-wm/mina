from __future__ import annotations

from src.checkpoint import AllPagesCheckpoint, CheckpointStore


def test_checkpoint_round_trip(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = AllPagesCheckpoint(
        apcontinue="ApcontinueToken",
        enumerated_count=123,
        enumeration_complete=False,
        last_success_time="2026-03-23T12:00:00Z",
    )
    store.save_allpages(checkpoint)
    store.append_discovered_title(page_id=1, ns=0, title="钻石镐")
    store.append_failed_page({"page_id": 2, "title": "苦力怕", "error": "boom"})

    loaded = store.load_allpages()
    assert loaded == checkpoint
    assert store.iter_discovered_titles() == [{"page_id": 1, "ns": 0, "title": "钻石镐"}]
    assert store.iter_failed_pages() == [{"page_id": 2, "title": "苦力怕", "error": "boom"}]


def test_checkpoint_reset_clears_jsonl_logs(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    store.append_discovered_title(page_id=1, ns=0, title="钻石镐")
    store.append_failed_page({"page_id": 1, "title": "钻石镐", "error": "boom"})

    store.reset_discovered_titles()
    store.reset_failed_pages()

    assert store.iter_discovered_titles() == []
    assert store.iter_failed_pages() == []
