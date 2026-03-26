# Minecraft Wiki Local

Offline crawler and structured indexer for MediaWiki-based Minecraft Wiki content.

## Docs

- Agent integration guide: [docs/MC_AI_AGENT_INTEGRATION.md](/Users/zhaozhiyu/Projects/mc_wiki_download/docs/MC_AI_AGENT_INTEGRATION.md)

## Commands

```bash
python -m src.main crawl --full
python -m src.main crawl --resume
python -m src.main parse --all
python -m src.main parse --page "钻石镐"
python -m src.main index --sqlite data/processed/sqlite/wiki.db
python -m src.main search --title "钻石镐"
python -m src.main search --category "工具"
python -m src.main search --template "信息框/物品"
python -m src.main search --infobox-key durability --infobox-value 1561
python -m src.main search --backlinks "黑曜石"
python -m src.main search --section-title "获取"
python -m src.main search --template-param-template "信息框/物品" --param-name stackable --param-value 否
python -m src.main verify
```

## Notes

- SQLite is the primary query surface for agents. JSON files are kept as raw/processed artifacts for re-parse and debugging.
- `search --title` returns a structured page bundle with page metadata, categories, sections, links, templates, and infobox fields.
