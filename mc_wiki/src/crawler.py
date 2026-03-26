from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .checkpoint import AllPagesCheckpoint, CheckpointStore
from .client import MediaWikiClient
from .models import AppConfig, RawPage
from .parser import parse_redirect
from .storage import FileStorage
from .utils import make_source_url, normalize_title, utc_now_iso


LOGGER = logging.getLogger(__name__)


class WikiCrawler:
    def __init__(
        self,
        config: AppConfig,
        client: MediaWikiClient,
        storage: FileStorage,
        checkpoints: CheckpointStore,
    ) -> None:
        self.config = config
        self.client = client
        self.storage = storage
        self.checkpoints = checkpoints

    def crawl_full(self) -> None:
        LOGGER.info("Starting full crawl")
        self.checkpoints.reset_discovered_titles()
        self.checkpoints.reset_failed_pages()
        checkpoint = AllPagesCheckpoint()
        self.checkpoints.save_allpages(checkpoint)
        self._enumerate_allpages(checkpoint)
        self._fetch_discovered_pages(force_refetch=True)

    def crawl_resume(self) -> None:
        LOGGER.info("Starting resume crawl")
        checkpoint = self.checkpoints.load_allpages()
        self._enumerate_allpages(checkpoint)
        self._fetch_discovered_pages(force_refetch=False)

    def _enumerate_allpages(self, checkpoint: AllPagesCheckpoint) -> None:
        while not checkpoint.enumeration_complete:
            pages, next_token = self.client.list_allpages(
                namespace=self.config.crawl.namespace,
                apcontinue=checkpoint.apcontinue,
            )
            for page in pages:
                self.checkpoints.append_discovered_title(
                    page_id=int(page["pageid"]),
                    ns=int(page["ns"]),
                    title=str(page["title"]),
                )
            checkpoint.enumerated_count += len(pages)
            checkpoint.apcontinue = next_token
            checkpoint.enumeration_complete = next_token is None
            checkpoint.last_success_time = utc_now_iso()
            self.checkpoints.save_allpages(checkpoint)
            LOGGER.info(
                "Enumerated allpages batch",
                extra={
                    "batch_count": len(pages),
                    "enumerated_count": checkpoint.enumerated_count,
                    "apcontinue": checkpoint.apcontinue,
                    "complete": checkpoint.enumeration_complete,
                },
            )

    def _fetch_discovered_pages(self, *, force_refetch: bool) -> None:
        discovered = self.checkpoints.iter_discovered_titles()
        existing_ids = set() if force_refetch else self.storage.existing_raw_page_ids()
        pending = [
            item for item in discovered if int(item["page_id"]) not in existing_ids
        ]
        LOGGER.info(
            "Building fetch queue",
            extra={
                "discovered_count": len(discovered),
                "pending_count": len(pending),
                "force_refetch": force_refetch,
            },
        )
        batch_size = max(1, self.config.crawl.page_batch_size)
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            self._fetch_batch(batch)

    def _fetch_batch(self, batch: list[dict[str, Any]]) -> None:
        titles = [str(item["title"]) for item in batch]
        LOGGER.info("Fetching page batch", extra={"titles": titles, "batch_size": len(batch)})
        try:
            pages = self.client.fetch_pages(titles)
        except Exception as exc:
            LOGGER.warning(
                "Batch fetch failed, retrying one page at a time",
                extra={"error": str(exc), "titles": titles},
            )
            if len(batch) == 1:
                self._record_failed_batch(batch, str(exc))
                return
            for item in batch:
                self._fetch_batch([item])
            return

        returned_titles = {normalize_title(str(page.get("title", ""))) for page in pages}
        for page in pages:
            self._save_raw_page(page)
        missing = [
            item for item in batch if normalize_title(str(item["title"])) not in returned_titles
        ]
        if missing:
            self._record_failed_batch(missing, "Page missing from API response")

    def _save_raw_page(self, page: dict[str, Any]) -> Path:
        title = str(page["title"])
        page_id = int(page["pageid"])
        revision = _extract_latest_revision(page)
        wikitext = _extract_revision_content(revision)
        categories = [
            normalize_title(str(item["title"]).split(":", 1)[-1])
            for item in page.get("categories", [])
        ]
        raw_page = RawPage(
            page_id=page_id,
            ns=int(page.get("ns", 0)),
            title=title,
            rev_id=int(revision["revid"]),
            timestamp=str(revision["timestamp"]),
            wikitext=wikitext,
            categories_raw=categories,
            source_url=make_source_url(self.config.wiki.base_url, title),
            redirect_target=parse_redirect(wikitext),
            crawl_time=utc_now_iso(),
        )
        path = self.storage.save_raw_page(raw_page)
        LOGGER.info(
            "Saved raw page",
            extra={
                "page_id": page_id,
                "title": title,
                "rev_id": raw_page.rev_id,
                "path": str(path),
            },
        )
        return path

    def _record_failed_batch(self, batch: list[dict[str, Any]], error: str) -> None:
        for item in batch:
            self.checkpoints.append_failed_page(
                {
                    "page_id": int(item["page_id"]),
                    "title": str(item["title"]),
                    "error": error,
                    "time": utc_now_iso(),
                }
            )


def _extract_latest_revision(page: dict[str, Any]) -> dict[str, Any]:
    revisions = page.get("revisions") or []
    if not revisions:
        raise ValueError(f"Page has no revisions: {page.get('title')}")
    return dict(revisions[0])


def _extract_revision_content(revision: dict[str, Any]) -> str:
    if "slots" in revision:
        return str(revision["slots"]["main"].get("content", ""))
    if "content" in revision:
        return str(revision["content"])
    if "*" in revision:
        return str(revision["*"])
    raise ValueError("Revision does not contain content")
