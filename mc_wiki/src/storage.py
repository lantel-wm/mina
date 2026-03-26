from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import (
    AppConfig,
    ProcessedPage,
    RawPage,
    processed_page_path,
    raw_page_path,
)
from .utils import atomic_write_json, ensure_dir, list_json_files, load_json, normalize_title


LOGGER = logging.getLogger(__name__)


class FileStorage:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.raw_dir = ensure_dir(config.storage.raw_dir)
        self.processed_dir = ensure_dir(config.storage.processed_dir)

    def raw_page_path(self, page_id: int) -> Path:
        return raw_page_path(self.raw_dir, page_id)

    def processed_page_path(self, page_id: int) -> Path:
        return processed_page_path(self.processed_dir, page_id)

    def save_raw_page(self, page: RawPage) -> Path:
        path = self.raw_page_path(page.page_id)
        atomic_write_json(path, page.to_dict())
        return path

    def load_raw_page(self, path: str | Path) -> RawPage:
        return RawPage.from_dict(load_json(path))

    def iter_raw_pages(self) -> list[tuple[RawPage, Path]]:
        return [(self.load_raw_page(path), path) for path in list_json_files(self.raw_dir)]

    def find_raw_page_by_title(self, title: str) -> tuple[RawPage, Path] | None:
        normalized = normalize_title(title)
        for raw_page, path in self.iter_raw_pages():
            if normalize_title(raw_page.title) == normalized:
                return raw_page, path
        return None

    def existing_raw_page_ids(self) -> set[int]:
        existing: set[int] = set()
        for path in list_json_files(self.raw_dir):
            try:
                existing.add(int(path.stem))
            except ValueError:
                continue
        return existing

    def save_processed_page(self, page: ProcessedPage) -> Path:
        path = self.processed_page_path(page.page_id)
        atomic_write_json(path, page.to_dict())
        return path

    def load_processed_page(self, path: str | Path) -> ProcessedPage:
        return ProcessedPage.from_dict(load_json(path))

    def iter_processed_pages(self) -> list[tuple[ProcessedPage, Path]]:
        return [
            (self.load_processed_page(path), path)
            for path in list_json_files(self.processed_dir)
        ]


def build_sqlite_index(
    pages: Iterable[tuple[ProcessedPage, Path]],
    sqlite_path: str | Path,
) -> Path:
    destination = Path(sqlite_path)
    ensure_dir(destination.parent)
    with tempfile.NamedTemporaryFile(
        prefix=destination.stem + ".",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as handle:
        temp_db_path = Path(handle.name)
    try:
        connection = sqlite3.connect(temp_db_path)
        connection.row_factory = sqlite3.Row
        _create_schema(connection)
        _insert_pages(connection, list(pages))
        connection.commit()
        connection.close()
        os.replace(temp_db_path, destination)
    finally:
        if temp_db_path.exists():
            temp_db_path.unlink(missing_ok=True)
    return destination


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;

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
        CREATE INDEX idx_sections_page_ord ON sections(page_id, ord);
        CREATE INDEX idx_sections_title ON sections(title);
        CREATE INDEX idx_categories_category ON categories(category);
        CREATE INDEX idx_wikilinks_target_title ON wikilinks(target_title);
        CREATE INDEX idx_templates_template_name ON templates(template_name);
        CREATE INDEX idx_template_params_lookup ON template_params(template_name, param_name);
        CREATE INDEX idx_infobox_kv_lookup ON infobox_kv(key, value);
        """
    )


def _insert_pages(
    connection: sqlite3.Connection,
    pages: list[tuple[ProcessedPage, Path]],
) -> None:
    for page, processed_path in pages:
        connection.execute(
            """
            INSERT INTO pages (
                page_id, title, normalized_title, ns, rev_id, is_redirect,
                redirect_target, plain_text, raw_path, processed_path, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page.page_id,
                page.title,
                page.normalized_title,
                page.ns,
                page.rev_id,
                1 if page.is_redirect else 0,
                page.redirect_target,
                page.plain_text,
                page.raw_path,
                str(processed_path),
                page.processed_time,
            ),
        )
        connection.executemany(
            "INSERT INTO sections (page_id, ord, level, title, text) VALUES (?, ?, ?, ?, ?)",
            [
                (page.page_id, section.ord, section.level, section.title, section.text)
                for section in page.sections
            ],
        )
        connection.executemany(
            "INSERT INTO categories (page_id, category) VALUES (?, ?)",
            [(page.page_id, category) for category in page.categories],
        )
        connection.executemany(
            "INSERT INTO wikilinks (page_id, target_title, display_text) VALUES (?, ?, ?)",
            [
                (page.page_id, link.target_title, link.display_text)
                for link in page.wikilinks
            ],
        )
        connection.executemany(
            "INSERT INTO templates (page_id, template_name) VALUES (?, ?)",
            [(page.page_id, template.name) for template in page.templates],
        )
        connection.executemany(
            """
            INSERT INTO template_params (page_id, template_name, param_name, param_value)
            VALUES (?, ?, ?, ?)
            """,
            [
                (page.page_id, template.name, key, value)
                for template in page.templates
                for key, value in template.params.items()
            ],
        )
        connection.executemany(
            "INSERT INTO infobox_kv (page_id, key, value) VALUES (?, ?, ?)",
            [(page.page_id, key, value) for key, value in page.infobox.items()],
        )


class WikiSearchDB:
    def __init__(self, sqlite_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.connection = sqlite3.connect(self.sqlite_path)
        self.connection.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "WikiSearchDB":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        row = self.connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'pages'
            """
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"SQLite index is not initialized at {self.sqlite_path}. Run the index command first."
            )

    def get_page_by_title(
        self,
        title: str,
        *,
        resolve_redirect: bool = True,
    ) -> dict[str, Any] | None:
        normalized = normalize_title(title)
        row = self.connection.execute(
            """
            SELECT * FROM pages
            WHERE title = ? OR normalized_title = ?
            ORDER BY title = ? DESC
            LIMIT 1
            """,
            (title, normalized, title),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if resolve_redirect and result["is_redirect"] and result["redirect_target"]:
            target = self.get_page_by_title(result["redirect_target"], resolve_redirect=False)
            if target:
                target["resolved_from"] = result["title"]
                return target
        return result

    def find_pages_by_category(self, category: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT p.* FROM pages p
            JOIN categories c ON c.page_id = p.page_id
            WHERE c.category = ?
            ORDER BY p.title
            """,
            (normalize_title(category),),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_pages_by_template(self, template_name: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT p.* FROM pages p
            JOIN templates t ON t.page_id = p.page_id
            WHERE t.template_name = ?
            ORDER BY p.title
            """,
            (normalize_title(template_name),),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_pages_by_template_param(
        self,
        template_name: str,
        param_name: str,
        param_value: str | None = None,
    ) -> list[dict[str, Any]]:
        if param_value is None:
            rows = self.connection.execute(
                """
                SELECT DISTINCT p.* FROM pages p
                JOIN template_params tp ON tp.page_id = p.page_id
                WHERE tp.template_name = ? AND tp.param_name = ?
                ORDER BY p.title
                """,
                (normalize_title(template_name), normalize_title(param_name)),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT DISTINCT p.* FROM pages p
                JOIN template_params tp ON tp.page_id = p.page_id
                WHERE tp.template_name = ? AND tp.param_name = ? AND tp.param_value = ?
                ORDER BY p.title
                """,
                (
                    normalize_title(template_name),
                    normalize_title(param_name),
                    param_value,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_pages_by_infobox(self, key: str, value: str | None = None) -> list[dict[str, Any]]:
        if value is None:
            rows = self.connection.execute(
                """
                SELECT DISTINCT p.* FROM pages p
                JOIN infobox_kv i ON i.page_id = p.page_id
                WHERE i.key = ?
                ORDER BY p.title
                """,
                (normalize_title(key),),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT DISTINCT p.* FROM pages p
                JOIN infobox_kv i ON i.page_id = p.page_id
                WHERE i.key = ? AND i.value = ?
                ORDER BY p.title
                """,
                (normalize_title(key), value),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_backlinks(self, target_title: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT p.* FROM pages p
            JOIN wikilinks w ON w.page_id = p.page_id
            WHERE w.target_title = ?
            ORDER BY p.title
            """,
            (normalize_title(target_title),),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_sections_by_title(self, section_title: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT p.title AS page_title, s.* FROM sections s
            JOIN pages p ON p.page_id = s.page_id
            WHERE s.title = ?
            ORDER BY p.title, s.ord
            """,
            (normalize_title(section_title),),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_page_bundle(
        self,
        title: str,
        *,
        resolve_redirect: bool = True,
    ) -> dict[str, Any] | None:
        page = self.get_page_by_title(title, resolve_redirect=resolve_redirect)
        if page is None:
            return None
        page_id = int(page["page_id"])
        categories = [
            row["category"]
            for row in self.connection.execute(
                "SELECT category FROM categories WHERE page_id = ? ORDER BY category",
                (page_id,),
            ).fetchall()
        ]
        sections = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT ord, level, title, text
                FROM sections
                WHERE page_id = ?
                ORDER BY ord
                """,
                (page_id,),
            ).fetchall()
        ]
        wikilinks = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT target_title, display_text
                FROM wikilinks
                WHERE page_id = ?
                ORDER BY target_title, display_text
                """,
                (page_id,),
            ).fetchall()
        ]
        templates: dict[str, dict[str, str]] = {}
        for row in self.connection.execute(
            """
            SELECT template_name, param_name, param_value
            FROM template_params
            WHERE page_id = ?
            ORDER BY template_name, id
            """,
            (page_id,),
        ).fetchall():
            template_name = str(row["template_name"])
            templates.setdefault(template_name, {})[str(row["param_name"])] = str(
                row["param_value"]
            )
        infobox = {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute(
                """
                SELECT key, value
                FROM infobox_kv
                WHERE page_id = ?
                ORDER BY key
                """,
                (page_id,),
            ).fetchall()
        }
        return {
            "page": page,
            "categories": categories,
            "sections": sections,
            "wikilinks": wikilinks,
            "templates": [
                {"name": name, "params": params} for name, params in templates.items()
            ],
            "infobox": infobox,
        }

    def table_counts(self) -> dict[str, int]:
        tables = [
            "pages",
            "sections",
            "categories",
            "wikilinks",
            "templates",
            "template_params",
            "infobox_kv",
        ]
        return {
            table: int(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }


def verify_storage(
    storage: FileStorage,
    sqlite_path: str | Path,
) -> dict[str, Any]:
    raw_pages = storage.iter_raw_pages()
    processed_pages = storage.iter_processed_pages()
    raw_ids = {page.page_id for page, _ in raw_pages}
    processed_ids = {page.page_id for page, _ in processed_pages}
    redirect_count = sum(1 for page, _ in processed_pages if page.is_redirect)
    report: dict[str, Any] = {
        "raw_count": len(raw_pages),
        "processed_count": len(processed_pages),
        "missing_processed_page_ids": sorted(raw_ids - processed_ids),
        "orphan_processed_page_ids": sorted(processed_ids - raw_ids),
        "redirect_count": redirect_count,
        "sqlite_counts": {},
        "query_smoke": {},
    }
    db_path = Path(sqlite_path)
    if db_path.exists():
        with WikiSearchDB(db_path) as db:
            report["sqlite_counts"] = db.table_counts()
            first_page = next(iter(processed_pages), (None, None))[0]
            if first_page:
                report["query_smoke"]["title_lookup"] = (
                    db.get_page_by_title(first_page.title) is not None
                )
            first_category_page = next(
                ((page, path) for page, path in processed_pages if page.categories),
                (None, None),
            )[0]
            if first_category_page:
                report["query_smoke"]["category_lookup"] = bool(
                    db.find_pages_by_category(first_category_page.categories[0])
                )
            first_section_page = next(
                ((page, path) for page, path in processed_pages if page.sections),
                (None, None),
            )[0]
            if first_section_page:
                report["query_smoke"]["section_lookup"] = bool(
                    db.find_sections_by_title(first_section_page.sections[0].title)
                )
            first_redirect = next(
                ((page, path) for page, path in processed_pages if page.is_redirect),
                (None, None),
            )[0]
            if first_redirect:
                report["query_smoke"]["redirect_lookup"] = (
                    db.get_page_by_title(first_redirect.title) is not None
                )
    return report
