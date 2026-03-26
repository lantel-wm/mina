from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]


@dataclass(slots=True)
class WikiConfig:
    base_url: str = "https://zh.minecraft.wiki"
    api_path: str = "/api.php"
    user_agent: str = "MinaWikiMirror/0.1 (contact: your_email@example.com)"
    timeout_sec: int = 30
    maxlag: int = 1


@dataclass(slots=True)
class CrawlConfig:
    namespace: int = 0
    page_batch_size: int = 30
    retry_times: int = 5
    retry_backoff_sec: float = 2.0
    save_raw_json: bool = True


@dataclass(slots=True)
class ParseConfig:
    extract_infobox: bool = True
    extract_sections: bool = True
    extract_links: bool = True
    extract_categories: bool = True


@dataclass(slots=True)
class StorageConfig:
    raw_dir: str = "data/raw/pages"
    processed_dir: str = "data/processed/pages"
    sqlite_path: str = "data/processed/sqlite/wiki.db"
    checkpoints_dir: str = "data/raw/checkpoints"


@dataclass(slots=True)
class AppConfig:
    wiki: WikiConfig = field(default_factory=WikiConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    parse: ParseConfig = field(default_factory=ParseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(slots=True)
class RawPage:
    page_id: int
    ns: int
    title: str
    rev_id: int
    timestamp: str
    wikitext: str
    categories_raw: list[str]
    source_url: str
    redirect_target: str | None
    crawl_time: str

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "RawPage":
        return cls(
            page_id=int(data["page_id"]),
            ns=int(data.get("ns", 0)),
            title=str(data["title"]),
            rev_id=int(data["rev_id"]),
            timestamp=str(data["timestamp"]),
            wikitext=str(data.get("wikitext", "")),
            categories_raw=[str(item) for item in data.get("categories_raw", [])],
            source_url=str(data.get("source_url", "")),
            redirect_target=(
                str(data["redirect_target"])
                if data.get("redirect_target") is not None
                else None
            ),
            crawl_time=str(data.get("crawl_time", "")),
        )


@dataclass(slots=True)
class SectionRecord:
    level: int
    title: str
    text: str
    ord: int

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "SectionRecord":
        return cls(
            level=int(data["level"]),
            title=str(data["title"]),
            text=str(data.get("text", "")),
            ord=int(data["ord"]),
        )


@dataclass(slots=True)
class TemplateRecord:
    name: str
    params: dict[str, str]

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "TemplateRecord":
        params = {
            str(key): str(value)
            for key, value in dict(data.get("params", {})).items()
        }
        return cls(name=str(data["name"]), params=params)


@dataclass(slots=True)
class WikiLinkRecord:
    target_title: str
    display_text: str

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "WikiLinkRecord":
        return cls(
            target_title=str(data["target_title"]),
            display_text=str(data.get("display_text", "")),
        )


@dataclass(slots=True)
class ProcessedPage:
    page_id: int
    ns: int
    title: str
    normalized_title: str
    rev_id: int
    is_redirect: bool
    redirect_target: str | None
    categories: list[str]
    templates: list[TemplateRecord]
    wikilinks: list[WikiLinkRecord]
    sections: list[SectionRecord]
    infobox: dict[str, str]
    plain_text: str
    raw_path: str
    processed_time: str
    source_ref: dict[str, Any]

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["templates"] = [template.to_dict() for template in self.templates]
        payload["wikilinks"] = [link.to_dict() for link in self.wikilinks]
        payload["sections"] = [section.to_dict() for section in self.sections]
        return payload

    @classmethod
    def from_dict(cls, data: JsonDict) -> "ProcessedPage":
        return cls(
            page_id=int(data["page_id"]),
            ns=int(data.get("ns", 0)),
            title=str(data["title"]),
            normalized_title=str(data["normalized_title"]),
            rev_id=int(data["rev_id"]),
            is_redirect=bool(data.get("is_redirect", False)),
            redirect_target=(
                str(data["redirect_target"])
                if data.get("redirect_target") is not None
                else None
            ),
            categories=[str(item) for item in data.get("categories", [])],
            templates=[
                TemplateRecord.from_dict(item) for item in data.get("templates", [])
            ],
            wikilinks=[
                WikiLinkRecord.from_dict(item) for item in data.get("wikilinks", [])
            ],
            sections=[SectionRecord.from_dict(item) for item in data.get("sections", [])],
            infobox={
                str(key): str(value)
                for key, value in dict(data.get("infobox", {})).items()
            },
            plain_text=str(data.get("plain_text", "")),
            raw_path=str(data.get("raw_path", "")),
            processed_time=str(data.get("processed_time", "")),
            source_ref=dict(data.get("source_ref", {})),
        )

    @property
    def processed_filename(self) -> str:
        return f"{self.page_id:08d}.json"


def raw_page_path(raw_dir: str | Path, page_id: int) -> Path:
    return Path(raw_dir) / f"{page_id:08d}.json"


def processed_page_path(processed_dir: str | Path, page_id: int) -> Path:
    return Path(processed_dir) / f"{page_id:08d}.json"
