from __future__ import annotations

import argparse
import json
import logging

from .checkpoint import CheckpointStore
from .client import MediaWikiClient
from .crawler import WikiCrawler
from .parser import WikiParser
from .storage import FileStorage, WikiSearchDB, build_sqlite_index, verify_storage
from .utils import load_config


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minecraft Wiki local crawler")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl_parser = subparsers.add_parser("crawl", help="Crawl MediaWiki pages")
    crawl_group = crawl_parser.add_mutually_exclusive_group(required=True)
    crawl_group.add_argument("--full", action="store_true", help="Restart full crawl")
    crawl_group.add_argument("--resume", action="store_true", help="Resume crawl")

    parse_parser = subparsers.add_parser("parse", help="Parse raw pages")
    parse_group = parse_parser.add_mutually_exclusive_group(required=True)
    parse_group.add_argument("--all", action="store_true", help="Parse all raw pages")
    parse_group.add_argument("--page", help="Parse one page by title")

    index_parser = subparsers.add_parser("index", help="Build SQLite index")
    index_parser.add_argument(
        "--sqlite",
        help="Target SQLite path",
        default=None,
    )

    search_parser = subparsers.add_parser("search", help="Structured search against SQLite")
    search_parser.add_argument(
        "--sqlite",
        help="SQLite path override",
        default=None,
    )
    search_group = search_parser.add_mutually_exclusive_group(required=True)
    search_group.add_argument("--title", help="Fetch one page bundle by title")
    search_group.add_argument("--category", help="Find pages by category")
    search_group.add_argument("--template", help="Find pages by template name")
    search_group.add_argument("--backlinks", help="Find pages that link to the title")
    search_group.add_argument("--section-title", help="Find sections by section title")
    search_group.add_argument("--infobox-key", help="Find pages by infobox key/value")
    search_group.add_argument(
        "--template-param-template",
        help="Find pages by template parameter; requires --param-name",
    )
    search_parser.add_argument("--infobox-value", help="Optional infobox value filter")
    search_parser.add_argument("--param-name", help="Template parameter name")
    search_parser.add_argument("--param-value", help="Optional template parameter value")

    subparsers.add_parser("verify", help="Verify local raw/processed/indexed data")
    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def run_crawl(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    storage = FileStorage(config)
    checkpoints = CheckpointStore(config.storage.checkpoints_dir)
    with MediaWikiClient(config) as client:
        crawler = WikiCrawler(config, client, storage, checkpoints)
        if args.full:
            crawler.crawl_full()
        else:
            crawler.crawl_resume()


def run_parse(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    storage = FileStorage(config)
    parser = WikiParser(config.parse)
    if args.all:
        for raw_page, raw_path in storage.iter_raw_pages():
            processed = parser.parse_raw_page(raw_page, raw_path=str(raw_path))
            storage.save_processed_page(processed)
    else:
        match = storage.find_raw_page_by_title(args.page)
        if match is None:
            raise SystemExit(f"Raw page not found for title: {args.page}")
        raw_page, raw_path = match
        processed = parser.parse_raw_page(raw_page, raw_path=str(raw_path))
        storage.save_processed_page(processed)


def run_index(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    storage = FileStorage(config)
    sqlite_path = args.sqlite or config.storage.sqlite_path
    build_sqlite_index(storage.iter_processed_pages(), sqlite_path)


def run_verify(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    storage = FileStorage(config)
    report = verify_storage(storage, config.storage.sqlite_path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def run_search(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    sqlite_path = args.sqlite or config.storage.sqlite_path
    try:
        with WikiSearchDB(sqlite_path) as db:
            if args.title:
                payload = db.get_page_bundle(args.title)
            elif args.category:
                payload = db.find_pages_by_category(args.category)
            elif args.template:
                payload = db.find_pages_by_template(args.template)
            elif args.backlinks:
                payload = db.find_backlinks(args.backlinks)
            elif args.section_title:
                payload = db.find_sections_by_title(args.section_title)
            elif args.infobox_key:
                payload = db.find_pages_by_infobox(args.infobox_key, args.infobox_value)
            else:
                if not args.param_name:
                    raise SystemExit("--param-name is required with --template-param-template")
                payload = db.find_pages_by_template_param(
                    args.template_param_template,
                    args.param_name,
                    args.param_value,
                )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.command == "crawl":
        run_crawl(args)
    elif args.command == "parse":
        run_parse(args)
    elif args.command == "index":
        run_index(args)
    elif args.command == "verify":
        run_verify(args)
    elif args.command == "search":
        run_search(args)
    else:  # pragma: no cover
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
