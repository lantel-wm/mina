from __future__ import annotations

import json
import re
import socket
import subprocess
from shutil import which
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser


WHITESPACE_RE = re.compile(r"\s+")
SKIP_TAGS = {"head", "script", "style", "nav", "footer", "noscript"}
SKIP_ATTR_TOKENS = {
    "nav",
    "navigation",
    "navbar",
    "sidebar",
    "footer",
    "header",
    "breadcrumbs",
    "breadcrumb",
    "toc",
    "infobox",
    "catlinks",
    "menu",
    "search",
    "vector-header",
    "mw-navigation",
    "page-actions",
    "interlanguage",
    "language",
}
PREFERRED_ATTR_TOKENS = {
    "main",
    "content",
    "article",
    "article-body",
    "article-content",
    "mw-parser-output",
    "page-content",
    "entry-content",
}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


@dataclass(slots=True)
class FetchedPage:
    url: str
    title: str
    content: str
    links: list[str]


class HtmlTextExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_depth = 0
        self._preferred_depth = 0
        self._saw_preferred_region = False
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._current_parts: list[str] = []
        self._blocks: list[str] = []
        self._links: list[str] = []
        self._bullet_depth = 0
        self._stack: list[tuple[str, bool, bool, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._stack.append((tag, False, False, False))
            self._title_depth += 1
            return
        attributes = {key.lower(): value for key, value in attrs}
        skip_started = tag in SKIP_TAGS or _attr_matches(attributes, SKIP_ATTR_TOKENS)
        preferred_started = False
        if not skip_started and (
            tag in {"main", "article"} or attributes.get("role", "").lower() == "main" or _attr_matches(attributes, PREFERRED_ATTR_TOKENS)
        ):
            preferred_started = True
            self._preferred_depth += 1
            self._saw_preferred_region = True
        list_started = tag in {"ul", "ol"}
        self._stack.append((tag, skip_started, preferred_started, list_started))
        if skip_started:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        href = attributes.get("href")
        if tag == "a" and href and self._should_collect():
            resolved = normalize_url(urllib.parse.urljoin(self._base_url, href))
            if resolved:
                self._links.append(resolved)
        if list_started:
            self._bullet_depth += 1
        if tag in BLOCK_TAGS:
            self._flush_block(force=False)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._close_tag(tag)
            if self._title_depth:
                self._title_depth -= 1
            return
        self._close_tag(tag)
        if self._skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush_block(force=True)

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            text = normalize_text(data)
            if text:
                self._title_parts.append(text)
            return
        if self._skip_depth:
            return
        if not self._should_collect() and not self._title_depth:
            return
        text = normalize_text(data)
        if not text:
            return
        self._current_parts.append(text)

    def build(self) -> tuple[str, str, list[str]]:
        self._flush_block(force=True)
        title = normalize_text(" ".join(self._title_parts)) or "Untitled"
        content = "\n\n".join(block for block in self._blocks if block)
        deduped_links = list(dict.fromkeys(self._links))
        return title, content, deduped_links

    def _close_tag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            stack_tag, skip_started, preferred_started, list_started = self._stack[index]
            if stack_tag != tag:
                continue
            for _, skipped, preferred, listed in self._stack[index:]:
                if skipped and self._skip_depth:
                    self._skip_depth -= 1
                if preferred and self._preferred_depth:
                    self._preferred_depth -= 1
                if listed and self._bullet_depth:
                    self._bullet_depth -= 1
            del self._stack[index:]
            return

    def _should_collect(self) -> bool:
        return not self._saw_preferred_region or self._preferred_depth > 0

    def _flush_block(self, *, force: bool) -> None:
        if not self._current_parts:
            return
        block = normalize_text(" ".join(self._current_parts))
        self._current_parts.clear()
        if not block:
            return
        if force and self._bullet_depth:
            block = f"- {block}"
        self._blocks.append(block)


def fetch_page(url: str, *, timeout: int = 30) -> FetchedPage:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("minecraft.wiki"):
        try:
            return _fetch_mediawiki_page(url, timeout=timeout)
        except Exception:
            return _fetch_with_curl(url, timeout=timeout)

    request = urllib.request.Request(url, headers=BROWSER_HEADERS)
    errors: list[Exception] = []
    html = ""
    for request_timeout in _retry_timeouts(timeout):
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            errors.append(exc)
    else:
        if errors:
            raise errors[-1]
        raise RuntimeError(f"Failed to fetch page: {url}")
    extractor = HtmlTextExtractor(url)
    extractor.feed(html)
    title, content, links = extractor.build()
    return FetchedPage(url=url, title=title, content=content, links=links)


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    cleaned = parsed._replace(fragment="", query=parsed.query or "")
    return urllib.parse.urlunparse(cleaned)


def _attr_matches(attributes: dict[str, str | None], tokens: set[str]) -> bool:
    values = [
        attributes.get("class") or "",
        attributes.get("id") or "",
        attributes.get("role") or "",
        attributes.get("aria-label") or "",
    ]
    haystack = " ".join(value.lower() for value in values if value).strip()
    if not haystack:
        return False
    return any(token in haystack for token in tokens)


def _retry_timeouts(timeout: int) -> tuple[int, ...]:
    base = max(timeout, 20)
    retry = max(base * 2, 45)
    if retry == base:
        return (base,)
    return (base, retry)


def _fetch_mediawiki_page(url: str, *, timeout: int) -> FetchedPage:
    parsed = urllib.parse.urlparse(url)
    title = _mediawiki_title_from_url(parsed)
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "extracts|links",
            "explaintext": "1",
            "exsectionformat": "plain",
            "pllimit": "max",
            "redirects": "1",
            "titles": title,
        }
    )
    api_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/api.php", "", params, ""))
    request = urllib.request.Request(api_url, headers=BROWSER_HEADERS)
    errors: list[Exception] = []
    payload: dict[str, object] | None = None
    for request_timeout in _retry_timeouts(timeout):
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = json.loads(response.read().decode(charset, errors="replace"))
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as exc:
            errors.append(exc)
    if payload is None:
        if errors:
            raise errors[-1]
        raise RuntimeError(f"Failed to fetch MediaWiki page: {url}")

    pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
    page = pages[0] if isinstance(pages, list) and pages else {}
    resolved_title = str(page.get("title", title)) if isinstance(page, dict) else title
    extract = str(page.get("extract", "")) if isinstance(page, dict) else ""
    links_payload = page.get("links", []) if isinstance(page, dict) else []
    links: list[str] = []
    if isinstance(links_payload, list):
        for item in links_payload:
            if not isinstance(item, dict):
                continue
            link_title = str(item.get("title", "")).strip()
            if not link_title:
                continue
            links.append(_mediawiki_link(parsed, link_title))

    content = re.sub(r"\n{3,}", "\n\n", extract).strip()
    return FetchedPage(url=url, title=resolved_title, content=content, links=links)


def _fetch_with_curl(url: str, *, timeout: int) -> FetchedPage:
    if which("curl") is None:
        raise RuntimeError("curl is not available for fallback fetching.")
    completed = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-A",
            BROWSER_HEADERS["User-Agent"],
            "-H",
            f"Accept: {BROWSER_HEADERS['Accept']}",
            "-H",
            f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
            "--max-time",
            str(max(timeout, 20)),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        error_text = (completed.stderr or completed.stdout or "curl fetch failed").strip()
        raise RuntimeError(error_text)
    extractor = HtmlTextExtractor(url)
    extractor.feed(completed.stdout)
    title, content, links = extractor.build()
    return FetchedPage(url=url, title=title, content=content, links=links)


def _mediawiki_title_from_url(parsed: urllib.parse.ParseResult) -> str:
    path = urllib.parse.unquote(parsed.path)
    if path.startswith("/w/"):
        raw = path[len("/w/") :]
    elif path.startswith("/wiki/"):
        raw = path[len("/wiki/") :]
    else:
        raw = path.strip("/")
    raw = raw.replace("_", " ").strip()
    return raw or "Main Page"


def _mediawiki_link(parsed: urllib.parse.ParseResult, title: str) -> str:
    slug = urllib.parse.quote(title.replace(" ", "_"), safe=":_()/,-")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, f"/w/{slug}", "", "", ""))
