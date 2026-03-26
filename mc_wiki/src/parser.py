from __future__ import annotations

import logging
import re
from typing import Any

try:
    import mwparserfromhell
except ImportError:  # pragma: no cover
    mwparserfromhell = None

from .models import (
    ParseConfig,
    ProcessedPage,
    RawPage,
    SectionRecord,
    TemplateRecord,
    WikiLinkRecord,
)
from .utils import normalize_title, utc_now_iso


LOGGER = logging.getLogger(__name__)

REDIRECT_RE = re.compile(
    r"^\s*#(?:redirect|重定向)\s*\[\[(?P<target>[^\]]+)\]\]",
    re.IGNORECASE,
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
REF_RE = re.compile(
    r"<ref\b[^>]*/\s*>|<ref\b[^>]*>.*?</ref\s*>",
    re.IGNORECASE | re.DOTALL,
)
HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
WHITESPACE_RE = re.compile(r"[ \t]+")
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
FILE_NAMESPACES = {"file", "image", "文件", "图像"}
CATEGORY_NAMESPACES = {"category", "分类"}


def _require_mwparserfromhell() -> Any:
    if mwparserfromhell is None:
        raise RuntimeError(
            "mwparserfromhell is required for parsing. Install project dependencies first."
        )
    return mwparserfromhell


def parse_redirect(wikitext: str) -> str | None:
    cleaned = COMMENT_RE.sub("", wikitext).lstrip()
    match = REDIRECT_RE.match(cleaned)
    if not match:
        return None
    target = match.group("target").split("|", 1)[0].split("#", 1)[0].strip()
    return normalize_title(target) or None


def _preclean_wikitext(wikitext: str) -> str:
    without_comments = COMMENT_RE.sub("", wikitext)
    return REF_RE.sub("", without_comments)


def _parse_code(wikitext: str) -> Any:
    parser = _require_mwparserfromhell()
    return parser.parse(_preclean_wikitext(wikitext))


def _strip_markup(text: str, *, drop_templates: bool = False) -> str:
    parser = _require_mwparserfromhell()
    code = parser.parse(_preclean_wikitext(text))
    if drop_templates:
        for template in list(code.filter_templates(recursive=False)):
            code.replace(template, "")
    for wikilink in list(code.filter_wikilinks(recursive=True)):
        namespace = _namespace_prefix(str(wikilink.title))
        if namespace in FILE_NAMESPACES or namespace in CATEGORY_NAMESPACES:
            code.replace(wikilink, "")
    plain = code.strip_code(normalize=True, collapse=True)
    return _normalize_plain_text(plain)


def _namespace_prefix(title: str) -> str:
    normalized = normalize_title(title)
    if ":" not in normalized:
        return ""
    return normalized.split(":", 1)[0].strip().lower()


def _normalize_plain_text(text: str) -> str:
    compact = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [WHITESPACE_RE.sub(" ", line).strip() for line in compact.split("\n")]
    kept_lines: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and previous_blank:
            continue
        kept_lines.append(line)
        previous_blank = is_blank
    filtered = "\n".join(kept_lines)
    return MULTI_NEWLINE_RE.sub("\n\n", filtered).strip()


def clean_plain_text(wikitext: str) -> str:
    code = _parse_code(wikitext)
    for template in list(code.filter_templates(recursive=False)):
        code.replace(template, "")
    for wikilink in list(code.filter_wikilinks(recursive=True)):
        namespace = _namespace_prefix(str(wikilink.title))
        if namespace in FILE_NAMESPACES or namespace in CATEGORY_NAMESPACES:
            code.replace(wikilink, "")
    plain = code.strip_code(normalize=True, collapse=True)
    return _normalize_plain_text(plain)


def extract_sections(wikitext: str) -> list[SectionRecord]:
    matches = list(HEADING_RE.finditer(wikitext))
    sections: list[SectionRecord] = []
    for index, match in enumerate(matches, start=1):
        level = max(1, len(match.group(1)) - 1)
        title = _strip_markup(match.group(2))
        body_start = match.end()
        body_end = matches[index].start() if index < len(matches) else len(wikitext)
        body = wikitext[body_start:body_end]
        sections.append(
            SectionRecord(
                level=level,
                title=title,
                text=clean_plain_text(body),
                ord=index,
            )
        )
    return sections


def extract_templates(wikitext: str) -> list[TemplateRecord]:
    code = _parse_code(wikitext)
    templates: list[TemplateRecord] = []
    for template in code.filter_templates(recursive=True):
        name = normalize_title(str(template.name).strip())
        params: dict[str, str] = {}
        positional_index = 1
        for param in template.params:
            key = normalize_title(str(param.name).strip()) if param.showkey else str(positional_index)
            if not param.showkey:
                positional_index += 1
            params[key] = _strip_markup(str(param.value), drop_templates=True)
        templates.append(TemplateRecord(name=name, params=params))
    return templates


def extract_links(wikitext: str) -> list[WikiLinkRecord]:
    code = _parse_code(wikitext)
    links: list[WikiLinkRecord] = []
    for link in code.filter_wikilinks(recursive=True):
        raw_title = str(link.title).strip()
        if not raw_title:
            continue
        target_title = normalize_title(raw_title.split("|", 1)[0].split("#", 1)[0])
        if not target_title:
            continue
        namespace = _namespace_prefix(target_title)
        if namespace in FILE_NAMESPACES or namespace in CATEGORY_NAMESPACES:
            continue
        display = _strip_markup(str(link.text or link.title))
        links.append(
            WikiLinkRecord(
                target_title=target_title,
                display_text=display or target_title,
            )
        )
    return links


def extract_categories(wikitext: str, categories_raw: list[str] | None = None) -> list[str]:
    code = _parse_code(wikitext)
    categories: list[str] = []
    seen: set[str] = set()
    for link in code.filter_wikilinks(recursive=True):
        raw_title = normalize_title(str(link.title))
        namespace = _namespace_prefix(raw_title)
        if namespace not in CATEGORY_NAMESPACES:
            continue
        category = normalize_title(raw_title.split(":", 1)[1].split("|", 1)[0])
        if category and category not in seen:
            categories.append(category)
            seen.add(category)
    for category in categories_raw or []:
        normalized = normalize_title(category.replace("Category:", "").replace("分类:", ""))
        if normalized and normalized not in seen:
            categories.append(normalized)
            seen.add(normalized)
    return categories


def extract_infobox(templates: list[TemplateRecord]) -> dict[str, str]:
    for template in templates:
        normalized = normalize_title(template.name).lower()
        if "信息框" in template.name or "infobox" in normalized:
            return dict(template.params)
    return {}


class WikiParser:
    def __init__(self, config: ParseConfig) -> None:
        self.config = config

    def parse_raw_page(self, raw_page: RawPage, *, raw_path: str = "") -> ProcessedPage:
        redirect_target = parse_redirect(raw_page.wikitext) or raw_page.redirect_target
        is_redirect = redirect_target is not None
        templates = [] if is_redirect else extract_templates(raw_page.wikitext)
        categories = (
            extract_categories(raw_page.wikitext, raw_page.categories_raw)
            if self.config.extract_categories
            else list(raw_page.categories_raw)
        )
        sections = (
            [] if is_redirect or not self.config.extract_sections else extract_sections(raw_page.wikitext)
        )
        wikilinks = (
            [] if is_redirect or not self.config.extract_links else extract_links(raw_page.wikitext)
        )
        infobox = (
            extract_infobox(templates)
            if self.config.extract_infobox and not is_redirect
            else {}
        )
        plain_text = "" if is_redirect else clean_plain_text(raw_page.wikitext)
        return ProcessedPage(
            page_id=raw_page.page_id,
            ns=raw_page.ns,
            title=raw_page.title,
            normalized_title=normalize_title(raw_page.title),
            rev_id=raw_page.rev_id,
            is_redirect=is_redirect,
            redirect_target=redirect_target,
            categories=categories,
            templates=templates,
            wikilinks=wikilinks,
            sections=sections,
            infobox=infobox,
            plain_text=plain_text,
            raw_path=raw_path,
            processed_time=utc_now_iso(),
            source_ref={
                "rev_id": raw_page.rev_id,
                "timestamp": raw_page.timestamp,
                "source_url": raw_page.source_url,
            },
        )
