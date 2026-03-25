from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")
_TITLE_FALLBACK_RE = re.compile(r"[_:]+")
_ASCII_LEADING_RE = re.compile(r"^[A-Za-z0-9]")
_COMMON_SECTION_TITLES = (
    "概述",
    "获取",
    "获得",
    "合成",
    "用途",
    "用法",
    "行为",
    "掉落物",
    "生成",
)
_POSITIVE_CATEGORY_KEYWORDS = (
    "方块",
    "物品",
    "功能方块",
    "工具",
    "机制",
    "红石",
    "矿石",
    "生物群系",
    "植物",
    "实体",
    "环境",
    "储物",
    "结构",
)
_NEGATIVE_CATEGORY_KEYWORDS = (
    "电影",
    "游戏",
    "地图",
    "活动服务器",
    "社区",
    "音乐家",
    "Mojang Studios",
    "非官方名称",
    "愚人节",
    "未使用",
    "待扩充",
    "需要信息",
    "本地化模块需更新",
    "嵌入Bilibili视频的页面",
    "嵌入YouTube视频的页面",
    "即将到来",
    "即将移除",
    "指南/",
    "版本",
)
_NEGATIVE_TITLE_SNIPPETS = (
    ".png",
    "Minecraft Movie",
    "Original Motion Picture Soundtrack",
    "指南/",
)


class WikiKnowledgeStore:
    def __init__(
        self,
        sqlite_path: Path,
        *,
        default_limit: int = 8,
        max_limit: int = 20,
        section_excerpt_chars: int = 600,
        plain_text_excerpt_chars: int = 800,
    ) -> None:
        self._sqlite_path = Path(sqlite_path)
        self._default_limit = max(1, default_limit)
        self._max_limit = max(self._default_limit, max_limit)
        self._section_excerpt_chars = max(120, section_excerpt_chars)
        self._plain_text_excerpt_chars = max(120, plain_text_excerpt_chars)
        self._candidate_pool_limit = max(self._max_limit * 8, 96)

    @property
    def sqlite_path(self) -> Path:
        return self._sqlite_path

    def get_page(self, title: str) -> dict[str, Any]:
        normalized_title = self._normalize_title(title)
        if not normalized_title:
            return {"found": False, "error": "title is required"}

        page = self._get_page_row(normalized_title, resolve_redirect=True)
        if page is None:
            return {
                "found": False,
                "title": normalized_title,
                "normalized_title": self._normalize_lookup_title(normalized_title),
            }

        page_id = int(page["page_id"])
        with self._connect() as connection:
            categories = [
                row["category"]
                for row in connection.execute(
                    "SELECT category FROM categories WHERE page_id = ? ORDER BY category",
                    (page_id,),
                ).fetchall()
            ]
            sections = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT ord, level, title, text
                    FROM sections
                    WHERE page_id = ?
                    ORDER BY ord
                    """,
                    (page_id,),
                ).fetchall()
            ]
            infobox_rows = connection.execute(
                """
                SELECT key, value
                FROM infobox_kv
                WHERE page_id = ?
                ORDER BY key
                """,
                (page_id,),
            ).fetchall()

        selected_sections = self._select_sections(sections)
        infobox = {
            str(row["key"]): self._normalize_text(str(row["value"]))
            for row in infobox_rows[:12]
        }
        page_payload = {
            "page_id": page["page_id"],
            "title": page["title"],
            "normalized_title": page["normalized_title"],
            "ns": page["ns"],
            "rev_id": page["rev_id"],
            "resolved_from": page.get("resolved_from"),
        }
        return {
            "found": True,
            "title": normalized_title,
            "normalized_title": self._normalize_lookup_title(normalized_title),
            "page": page_payload,
            "plain_text_excerpt": self._excerpt(page.get("plain_text", ""), self._plain_text_excerpt_chars),
            "categories": categories[:12],
            "section_titles": [section["title"] for section in sections[:20]],
            "sections": selected_sections,
            "infobox": infobox,
            "result_count": 1,
        }

    def find_by_category(self, category: str, limit: int | None = None) -> dict[str, Any]:
        normalized = self._normalize_title(category)
        if not normalized:
            return {"results": [], "result_count": 0, "error": "category is required"}
        rows = self._query_pages(
            """
            SELECT p.*
            FROM pages p
            JOIN categories c ON c.page_id = p.page_id
            WHERE c.category = ? AND p.is_redirect = 0
            ORDER BY p.title
            LIMIT ?
            """,
            (normalized, self._candidate_pool(self.clamp_limit(limit))),
        )
        ranked = self._rank_page_rows("category", normalized, rows, limit=self.clamp_limit(limit))
        return self._list_payload("category", normalized, ranked)

    def find_by_template(self, template_name: str, limit: int | None = None) -> dict[str, Any]:
        normalized = self._normalize_title(template_name)
        if not normalized:
            return {"results": [], "result_count": 0, "error": "template_name is required"}
        rows = self._query_pages(
            """
            SELECT DISTINCT p.*
            FROM pages p
            JOIN templates t ON t.page_id = p.page_id
            WHERE t.template_name = ? AND p.is_redirect = 0
            ORDER BY p.title
            LIMIT ?
            """,
            (normalized, self._candidate_pool(self.clamp_limit(limit))),
        )
        ranked = self._rank_page_rows("template", normalized, rows, limit=self.clamp_limit(limit))
        return self._list_payload("template", normalized, ranked)

    def find_by_template_param(
        self,
        template_name: str,
        param_name: str,
        param_value: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        normalized_template = self._normalize_title(template_name)
        normalized_name = self._normalize_title(param_name)
        if not normalized_template or not normalized_name:
            return {"results": [], "result_count": 0, "error": "template_name and param_name are required"}
        query = """
            SELECT DISTINCT p.*
            FROM pages p
            JOIN template_params tp ON tp.page_id = p.page_id
            WHERE tp.template_name = ? AND tp.param_name = ? AND p.is_redirect = 0
        """
        params: list[Any] = [normalized_template, normalized_name]
        if param_value is not None and str(param_value).strip():
            query += " AND tp.param_value = ?"
            params.append(str(param_value).strip())
        query += " ORDER BY p.title LIMIT ?"
        params.append(self._candidate_pool(self.clamp_limit(limit)))
        rows = self._query_pages(query, tuple(params))
        ranked = self._rank_page_rows(
            "template_param",
            normalized_template,
            rows,
            limit=self.clamp_limit(limit),
        )
        return self._list_payload(
            "template_param",
            normalized_template,
            ranked,
            param_name=normalized_name,
            param_value=str(param_value).strip() if param_value is not None else None,
        )

    def find_by_infobox(self, key: str, value: str | None = None, limit: int | None = None) -> dict[str, Any]:
        normalized_key = self._normalize_title(key)
        if not normalized_key:
            return {"results": [], "result_count": 0, "error": "key is required"}
        query = """
            SELECT DISTINCT p.*
            FROM pages p
            JOIN infobox_kv i ON i.page_id = p.page_id
            WHERE i.key = ? AND p.is_redirect = 0
        """
        params: list[Any] = [normalized_key]
        if value is not None and str(value).strip():
            query += " AND i.value = ?"
            params.append(str(value).strip())
        query += " ORDER BY p.title LIMIT ?"
        params.append(self._candidate_pool(self.clamp_limit(limit)))
        rows = self._query_pages(query, tuple(params))
        ranked = self._rank_page_rows("infobox", normalized_key, rows, limit=self.clamp_limit(limit))
        return self._list_payload(
            "infobox",
            normalized_key,
            ranked,
            value=str(value).strip() if value is not None else None,
        )

    def find_backlinks(self, title: str, limit: int | None = None) -> dict[str, Any]:
        normalized = self._normalize_title(title)
        if not normalized:
            return {"results": [], "result_count": 0, "error": "title is required"}
        target_page = self._get_page_row(normalized, resolve_redirect=True)
        target_title = target_page["title"] if target_page is not None else normalized
        requested_title = normalized
        rows = self._query_pages(
            """
            SELECT DISTINCT p.*
            FROM pages p
            JOIN wikilinks w ON w.page_id = p.page_id
            WHERE w.target_title = ? AND p.is_redirect = 0
            ORDER BY p.title
            LIMIT ?
            """,
            (target_title, self._candidate_pool(self.clamp_limit(limit))),
        )
        ranked = self._rank_page_rows("backlinks", target_title, rows, limit=self.clamp_limit(limit))
        return self._list_payload(
            "backlinks",
            target_title,
            ranked,
            requested_title=requested_title,
            resolved_title=target_title,
            redirect_resolved=target_title != requested_title,
            redirect_note=(
                f"Requested title {requested_title} resolves to canonical wiki page {target_title}."
                if target_title != requested_title
                else None
            ),
        )

    def find_sections(self, section_title: str, limit: int | None = None) -> dict[str, Any]:
        normalized = self._normalize_title(section_title)
        if not normalized:
            return {"results": [], "result_count": 0, "error": "section_title is required"}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.page_id, p.title, p.plain_text, s.title AS section_title, s.text AS section_text
                FROM sections s
                JOIN pages p ON p.page_id = s.page_id
                WHERE s.title = ? AND p.is_redirect = 0
                ORDER BY p.title, s.ord
                LIMIT ?
                """,
                (normalized, self._candidate_pool(self.clamp_limit(limit))),
            ).fetchall()
        ranked_rows = self._rank_section_rows(normalized, [dict(row) for row in rows], limit=self.clamp_limit(limit))
        results = [
            {
                "page_id": row["page_id"],
                "title": row["title"],
                "excerpt": self._excerpt(row["plain_text"], 220),
                "matched_section_title": row["section_title"],
                "matched_section_excerpt": self._excerpt(row["section_text"], self._section_excerpt_chars),
            }
            for row in ranked_rows
        ]
        return {
            "query_type": "section",
            "section_title": normalized,
            "results": results,
            "result_count": len(results),
        }

    def clamp_limit(self, value: int | None) -> int:
        if value is None:
            return self._default_limit
        return max(1, min(int(value), self._max_limit))

    def _query_pages(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _list_payload(self, query_type: str, query_value: str, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "query_type": query_type,
            "query": query_value,
            **extra,
            "results": [self._page_card(row) for row in rows],
            "result_count": len(rows),
        }

    def _page_card(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "page_id": row["page_id"],
            "title": row["title"],
            "excerpt": self._excerpt(row.get("plain_text", ""), 220),
        }
        categories = row.get("_categories")
        if isinstance(categories, list) and categories:
            payload["categories"] = categories[:6]
        return payload

    def _candidate_pool(self, limit: int) -> int:
        return max(limit * 8, min(self._candidate_pool_limit, 160))

    def _rank_page_rows(
        self,
        query_type: str,
        query_value: str,
        rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not rows:
            return rows
        categories_by_page = self._categories_for_page_ids(int(row["page_id"]) for row in rows)
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for row in rows:
            page_id = int(row["page_id"])
            categories = categories_by_page.get(page_id, [])
            enriched = dict(row)
            enriched["_categories"] = categories
            score = self._page_score(query_type, query_value, enriched, categories)
            text_len = len(str(row.get("plain_text", "")))
            scored.append((score, text_len, enriched))
        scored.sort(key=lambda item: (item[0], item[1], item[2]["title"]), reverse=True)
        return [item[2] for item in scored[:limit]]

    def _rank_section_rows(self, section_title: str, rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for row in rows:
            title = str(row.get("title", ""))
            score = 0
            if "/" in title:
                score -= 10
            if _ASCII_LEADING_RE.match(title):
                score -= 3
            score += min(len(str(row.get("section_text", ""))) // 120, 12)
            if str(row.get("section_title", "")).strip() == section_title:
                score += 8
            scored.append((score, len(str(row.get("plain_text", ""))), row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]["title"]), reverse=True)
        return [item[2] for item in scored[:limit]]

    def _categories_for_page_ids(self, page_ids: Any) -> dict[int, list[str]]:
        ids = sorted({int(page_id) for page_id in page_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT page_id, category FROM categories WHERE page_id IN ({placeholders}) ORDER BY category",
                tuple(ids),
            ).fetchall()
        categories_by_page: dict[int, list[str]] = {page_id: [] for page_id in ids}
        for row in rows:
            categories_by_page[int(row["page_id"])].append(str(row["category"]))
        return categories_by_page

    def _page_score(
        self,
        query_type: str,
        query_value: str,
        row: dict[str, Any],
        categories: list[str],
    ) -> int:
        title = str(row.get("title", ""))
        score = 0
        text_length = len(str(row.get("plain_text", "")))
        score += min(text_length // 300, 18)
        if query_type == "category" and query_value in categories:
            score += 14
        if query_type == "backlinks":
            score += 6
        joined_categories = " | ".join(categories)
        for keyword in _POSITIVE_CATEGORY_KEYWORDS:
            if any(keyword in category for category in categories):
                score += 4
        for keyword in _NEGATIVE_CATEGORY_KEYWORDS:
            if keyword in joined_categories or keyword in title:
                score -= 10
        for snippet in _NEGATIVE_TITLE_SNIPPETS:
            if snippet in title:
                score -= 12
        if "/" in title:
            score -= 8
        if _ASCII_LEADING_RE.match(title):
            score -= 6
        if "方块" in categories and query_type in {"category", "infobox", "template", "template_param", "backlinks"}:
            score += 4
        if "物品" in categories and query_type in {"template_param", "infobox", "backlinks"}:
            score += 3
        if query_type == "category" and query_value == "方块":
            if "功能方块" in categories:
                score += 8
            if "机制" in categories or "红石" in categories or "红石机制" in categories:
                score += 8
            if "矿石" in categories or "结构" in categories or "环境" in categories:
                score += 5
            if "物品" in categories:
                score -= 10
            if "植物" in categories:
                score -= 4
        if query_type == "category" and query_value == "物品" and "方块" in categories:
            score -= 6
        return score

    def _get_page_row(self, title: str, *, resolve_redirect: bool) -> dict[str, Any] | None:
        normalized = self._normalize_lookup_title(title)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM pages
                WHERE title = ? OR normalized_title = ?
                ORDER BY title = ? DESC
                LIMIT 1
                """,
                (title, normalized, title),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT *
                    FROM pages
                    WHERE lower(title) = lower(?) OR lower(normalized_title) = lower(?)
                    ORDER BY lower(title) = lower(?) DESC
                    LIMIT 1
                    """,
                    (title, normalized, title),
                ).fetchone()
            if row is None:
                return None
            page = dict(row)
            if resolve_redirect and int(page["is_redirect"]) and page["redirect_target"]:
                target = self._get_page_row(str(page["redirect_target"]), resolve_redirect=False)
                if target is not None:
                    target["resolved_from"] = page["title"]
                    return target
            return page

    def _select_sections(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prioritized: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        for preferred in _COMMON_SECTION_TITLES:
            for section in sections:
                title = str(section.get("title", "")).strip()
                if title != preferred or title in seen_titles:
                    continue
                prioritized.append(section)
                seen_titles.add(title)
        for section in sections:
            title = str(section.get("title", "")).strip()
            if not title or title in seen_titles:
                continue
            prioritized.append(section)
            seen_titles.add(title)
        return [
            {
                "ord": int(section["ord"]),
                "level": int(section["level"]),
                "title": section["title"],
                "excerpt": self._excerpt(section["text"], self._section_excerpt_chars),
            }
            for section in prioritized[:4]
        ]

    def _connect(self) -> sqlite3.Connection:
        if not self._sqlite_path.exists():
            raise FileNotFoundError(f"Minecraft wiki DB not found at {self._sqlite_path}")
        connection = sqlite3.connect(f"file:{self._sqlite_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _normalize_title(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if ":" in text:
            text = text.split(":", 1)[1]
        text = _TITLE_FALLBACK_RE.sub(" ", text)
        return self._normalize_lookup_title(text)

    def _normalize_lookup_title(self, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", str(value).replace("_", " ").strip())

    def _normalize_text(self, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", str(value or "").strip())

    def _excerpt(self, value: str, limit: int) -> str:
        text = self._normalize_text(value)
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"
