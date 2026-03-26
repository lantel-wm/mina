from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import AppConfig


LOGGER = logging.getLogger(__name__)


class MediaWikiAPIError(RuntimeError):
    """Terminal MediaWiki API error."""


class RetryableMediaWikiAPIError(MediaWikiAPIError):
    """Retryable MediaWiki API error."""


class MediaWikiClient:
    def __init__(
        self,
        config: AppConfig,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.wiki.base_url.rstrip("/")
        self.api_url = f"{self.base_url}{config.wiki.api_path}"
        self.retry_times = config.crawl.retry_times
        self.retry_backoff_sec = config.crawl.retry_backoff_sec
        if "your_email@example.com" in config.wiki.user_agent:
            LOGGER.warning(
                "Configured User-Agent still contains placeholder contact information"
            )
        self._client = httpx.Client(
            timeout=config.wiki.timeout_sec,
            headers={"User-Agent": config.wiki.user_agent},
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MediaWikiClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        request_params = {
            "format": "json",
            "formatversion": 2,
            "maxlag": self.config.wiki.maxlag,
            **params,
        }
        last_error: Exception | None = None
        for attempt in range(self.retry_times + 1):
            try:
                current_api_url = self.api_url
                LOGGER.info(
                    "Requesting MediaWiki API",
                    extra={
                        "api_url": current_api_url,
                        "action": request_params.get("action"),
                        "params": self._summarize_params(request_params),
                        "attempt": attempt + 1,
                    },
                )
                response = self._client.get(current_api_url, params=request_params)
                LOGGER.info(
                    "Received MediaWiki API response",
                    extra={
                        "status_code": response.status_code,
                        "api_url": current_api_url,
                        "action": request_params.get("action"),
                    },
                )
                if response.is_redirect or response.status_code == 404:
                    discovered = self._recover_api_url(response)
                    if discovered and discovered != current_api_url:
                        self.api_url = discovered
                        LOGGER.warning(
                            "Switched MediaWiki API endpoint after failed response",
                            extra={
                                "old_api_url": current_api_url,
                                "new_api_url": discovered,
                                "status_code": response.status_code,
                            },
                        )
                        continue
                if response.status_code in {429, 502, 503, 504} or response.status_code >= 500:
                    raise RetryableMediaWikiAPIError(
                        f"HTTP {response.status_code} for MediaWiki API request"
                    )
                if response.is_redirect:
                    raise MediaWikiAPIError(
                        "MediaWiki API endpoint redirected unexpectedly to "
                        f"{response.headers.get('location')}"
                    )
                response.raise_for_status()
                payload = response.json()
                error = payload.get("error")
                if error:
                    code = str(error.get("code", "unknown"))
                    if code in {"maxlag", "ratelimited", "readonly"}:
                        raise RetryableMediaWikiAPIError(str(error.get("info", code)))
                    raise MediaWikiAPIError(str(error.get("info", code)))
                return payload
            except (httpx.TransportError, RetryableMediaWikiAPIError) as exc:
                last_error = exc
                if not self._should_retry(exc, attempt):
                    break
                delay = self.retry_backoff_sec * (2**attempt)
                LOGGER.warning(
                    "Retrying MediaWiki API request after error",
                    extra={
                        "error": str(exc),
                        "attempt": attempt + 1,
                        "sleep_sec": delay,
                    },
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if not self._should_retry(exc, attempt):
                    break
                delay = self.retry_backoff_sec * (2**attempt)
                LOGGER.warning(
                    "Retrying MediaWiki API request after error",
                    extra={
                        "error": str(exc),
                        "attempt": attempt + 1,
                        "sleep_sec": delay,
                    },
                )
                time.sleep(delay)
        if last_error:
            raise last_error
        raise MediaWikiAPIError("MediaWiki API request failed without a specific error")

    def list_allpages(
        self,
        *,
        namespace: int,
        apcontinue: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "action": "query",
            "list": "allpages",
            "apnamespace": namespace,
            "aplimit": "max",
        }
        if apcontinue:
            params["apcontinue"] = apcontinue
        payload = self._request(params)
        pages = payload.get("query", {}).get("allpages", [])
        next_token = payload.get("continue", {}).get("apcontinue")
        return list(pages), next_token

    def fetch_pages(self, titles: Iterable[str]) -> list[dict[str, Any]]:
        title_list = list(titles)
        if not title_list:
            return []
        payload = self._request(
            {
                "action": "query",
                "prop": "revisions|categories",
                "titles": "|".join(title_list),
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "cllimit": "max",
            }
        )
        return list(payload.get("query", {}).get("pages", []))

    def _recover_api_url(self, response: httpx.Response) -> str | None:
        extra_paths: list[str] = []
        location = response.headers.get("location")
        if location:
            parsed = urlsplit(location)
            if parsed.path:
                extra_paths.append(parsed.path)
        return self._discover_api_url(extra_paths)

    def _discover_api_url(self, extra_paths: Iterable[str] | None = None) -> str | None:
        probe_params = {
            "action": "query",
            "meta": "siteinfo",
            "siprop": "general",
            "format": "json",
            "formatversion": 2,
        }
        candidates = self._candidate_api_urls(extra_paths)
        for candidate in candidates:
            try:
                LOGGER.info(
                    "Probing MediaWiki API endpoint",
                    extra={"candidate_api_url": candidate},
                )
                response = self._client.get(candidate, params=probe_params)
                if response.status_code != 200:
                    continue
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    continue
                payload = response.json()
                if payload.get("query", {}).get("general"):
                    return candidate
            except (httpx.HTTPError, ValueError):
                continue
        return None

    def _candidate_api_urls(self, extra_paths: Iterable[str] | None = None) -> list[str]:
        seen: set[str] = set()
        candidates: list[str] = []
        paths = [self.config.wiki.api_path]
        if extra_paths:
            paths.extend(extra_paths)
        paths.extend(["/api.php", "/w/api.php"])
        for path in paths:
            normalized = str(path).strip()
            if not normalized:
                continue
            if normalized.startswith("http://") or normalized.startswith("https://"):
                candidate = normalized
            else:
                if not normalized.startswith("/"):
                    normalized = "/" + normalized
                candidate = f"{self.base_url}{normalized}"
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
        return candidates

    def _should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.retry_times:
            return False
        if isinstance(error, httpx.HTTPStatusError):
            status_code = error.response.status_code
            return status_code == 429 or status_code >= 500
        if isinstance(error, MediaWikiAPIError) and not isinstance(
            error, RetryableMediaWikiAPIError
        ):
            return False
        return True

    @staticmethod
    def _summarize_params(params: dict[str, Any]) -> str:
        interesting = {
            key: value
            for key, value in params.items()
            if key not in {"format", "formatversion"}
        }
        return ", ".join(f"{key}={value}" for key, value in sorted(interesting.items()))
