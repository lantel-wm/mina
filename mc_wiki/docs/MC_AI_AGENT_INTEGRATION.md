# Minecraft Wiki 数据与 MC AI Agent 集成指南

本文档描述如何把当前仓库已经抓取并解析完成的 Minecraft Wiki 本地数据，接入到 Minecraft AI agent 中，作为结构化知识检索层使用。

本文档以当前代码实现和本地数据产物为准，不讨论 embedding、向量库或语义检索。

## 1. 当前数据状态

基于 2026-03-23 本地执行 `./.venv/bin/python -m src.main verify` 的结果：

- `raw_count = 27605`
- `processed_count = 27605`
- `redirect_count = 20313`
- `pages = 27605`
- `sections = 59707`
- `categories = 35993`
- `wikilinks = 217199`
- `templates = 320801`
- `template_params = 813885`
- `infobox_kv = 51777`

这意味着当前库已经足够支撑：

- 按标题精确查页
- 按分类找页面
- 按模板找页面
- 按模板参数找页面
- 按 infobox 键值筛页
- 按 section 标题定位内容
- 按 wiki 内链找反向链接
- 自动处理重定向页

注意：重定向页占比很高，因此 agent 侧必须把“标题查找默认解析重定向”视为标准行为，而不是附加功能。

## 2. 本地数据布局

当前仓库中的主要数据层如下：

- 原始页 JSON：`data/raw/pages/*.json`
- 结构化页 JSON：`data/processed/pages/*.json`
- 主查询库：`data/processed/sqlite/wiki.db`
- checkpoint：`data/raw/checkpoints/`

推荐集成时的优先级：

1. 优先使用 SQLite
2. 需要调试具体页面解析结果时读取 processed JSON
3. 需要排查解析错误或补做解析策略时读取 raw JSON

对 agent 来说，SQLite 应该是唯一主查询面。

## 3. 为什么推荐把 SQLite 当成 agent 的唯一知识入口

当前项目的目标是结构化搜索，不是通用语义检索。对 agent 而言，最稳的方式不是把整页文本扔给模型做模糊理解，而是先走结构化过滤，再取少量上下文给模型总结。

推荐原因：

- 查询稳定，可重复
- 结果可解释，便于调试
- 成本低，不依赖向量化
- 可以直接利用模板、分类、section、infobox 等 MediaWiki 原生结构
- 对“找某类方块/某类生物/某个机制相关页面”这类任务比全文检索更精确

建议的基本原则：

- 先检索，后生成
- 先缩小候选页，再读取正文
- 优先结构化字段，`plain_text` 只作为最终回答上下文

## 4. 数据库契约

当前 SQLite 主表与索引逻辑在 [src/storage.py](/Users/zhaozhiyu/Projects/mc_wiki_download/src/storage.py)。

核心表：

- `pages(page_id, title, normalized_title, ns, rev_id, is_redirect, redirect_target, plain_text, raw_path, processed_path, updated_at)`
- `sections(id, page_id, ord, level, title, text)`
- `categories(page_id, category)`
- `wikilinks(page_id, target_title, display_text)`
- `templates(page_id, template_name)`
- `template_params(id, page_id, template_name, param_name, param_value)`
- `infobox_kv(page_id, key, value)`

当前已经建立常用 B-tree 索引，适合 agent 在线查：

- `pages.title`
- `pages.normalized_title`
- `pages.redirect_target`
- `sections(page_id, ord)`
- `sections.title`
- `categories.category`
- `wikilinks.target_title`
- `templates.template_name`
- `template_params(template_name, param_name)`
- `infobox_kv(key, value)`

## 5. 已有查询入口

### 5.1 Python 入口

当前最直接的集成类是 [src/storage.py](/Users/zhaozhiyu/Projects/mc_wiki_download/src/storage.py) 里的 `WikiSearchDB`。

可直接用的方法：

- `get_page_by_title(title, resolve_redirect=True)`
- `get_page_bundle(title, resolve_redirect=True)`
- `find_pages_by_category(category)`
- `find_pages_by_template(template_name)`
- `find_pages_by_template_param(template_name, param_name, param_value=None)`
- `find_pages_by_infobox(key, value=None)`
- `find_backlinks(target_title)`
- `find_sections_by_title(section_title)`
- `table_counts()`

推荐 agent 默认使用 `get_page_bundle()`，而不是只拿 `pages` 表的一行，因为 bundle 会把页面元信息、分类、sections、links、templates、infobox 一次性取全。

### 5.2 CLI 入口

当前命令行入口在 [src/main.py](/Users/zhaozhiyu/Projects/mc_wiki_download/src/main.py)。

可直接供 agent 子进程调用：

```bash
./.venv/bin/python -m src.main search --title "按钮"
./.venv/bin/python -m src.main search --category "生物"
./.venv/bin/python -m src.main search --template "信息框/方块"
./.venv/bin/python -m src.main search --template-param-template "信息框/物品" --param-name stackable --param-value 否
./.venv/bin/python -m src.main search --infobox-key image
./.venv/bin/python -m src.main search --backlinks "红石"
./.venv/bin/python -m src.main search --section-title "获取"
```

返回值统一为 JSON。

如果 agent 当前运行环境不方便直接 import Python 模块，优先走这个 CLI。

## 6. 推荐的 agent 集成方式

最推荐的是加一层很薄的“知识查询工具适配层”，不要让 LLM 直接写 SQL。

推荐结构：

1. Minecraft agent 接收问题
2. 路由器判断是否需要 wiki 知识
3. 调用结构化查询工具
4. 工具返回 JSON 结果
5. agent 只基于返回结果做总结、对比、解释

不要让模型自由拼 SQL 的原因：

- 容易发散成低价值全文扫描
- SQL 质量不稳定
- 容易把 `plain_text` 当主索引滥用
- 结构化字段的价值会被浪费

更稳的做法是只暴露有限个工具：

- `wiki_get_page(title)`
- `wiki_find_by_category(category, limit)`
- `wiki_find_by_template(template_name, limit)`
- `wiki_find_by_template_param(template_name, param_name, param_value=None, limit)`
- `wiki_find_by_infobox(key, value=None, limit)`
- `wiki_find_backlinks(target_title, limit)`
- `wiki_find_sections(section_title, limit)`

如果后续确实需要复杂组合查询，再新增一个“受控 SQL 工具”，但默认不开放。

## 7. 推荐的工具契约

下面是一组足够给 MC AI agent 用的工具定义。

### `wiki_get_page`

输入：

```json
{
  "title": "按钮"
}
```

输出：

```json
{
  "page": {
    "page_id": 45,
    "title": "按钮",
    "normalized_title": "按钮",
    "is_redirect": 0,
    "redirect_target": null,
    "plain_text": "...",
    "rev_id": 1279954
  },
  "categories": ["Java版独有信息", "基岩版独有信息", "方块", "红石", "红石机制"],
  "sections": [
    {"ord": 1, "level": 1, "title": "方块列表", "text": "..."},
    {"ord": 2, "level": 1, "title": "用途", "text": "..."}
  ],
  "wikilinks": [
    {"target_title": "红石", "display_text": "红石"}
  ],
  "templates": [
    {"name": "BlockLink", "params": {"1": "Polished Blackstone Button"}}
  ],
  "infobox": {}
}
```

语义：

- 用于“解释某个具体概念”
- 默认解析重定向
- 最适合答“X 是什么”“X 怎么获得”“X 有什么用途”

### `wiki_find_by_category`

输入：

```json
{
  "category": "生物",
  "limit": 20
}
```

输出是 `pages` 列表。

语义：

- 用于“有哪些 X”
- 适合“所有属于某类的页面”

### `wiki_find_by_template_param`

输入：

```json
{
  "template_name": "信息框/物品",
  "param_name": "stackable",
  "param_value": "否",
  "limit": 50
}
```

语义：

- 用于“找满足结构化属性的页面”
- 比全文关键词匹配更稳

### `wiki_find_by_infobox`

输入：

```json
{
  "key": "image",
  "value": null,
  "limit": 50
}
```

语义：

- 用于“按信息框字段筛选”
- 适合方块、物品、生物属性筛查

### `wiki_find_backlinks`

输入：

```json
{
  "target_title": "红石",
  "limit": 20
}
```

语义：

- 用于“哪些页面提到了/链接到了某个概念”
- 适合扩展上下文、找相关机制

### `wiki_find_sections`

输入：

```json
{
  "section_title": "获取",
  "limit": 20
}
```

语义：

- 用于“找拥有某类 section 的页面”
- 适合集中处理“获取”“用途”“行为”“掉落物”“历史”这类结构

## 8. 推荐的检索流程

### 8.1 实体解释类问题

适用问题：

- “按钮是什么？”
- “苦力怕会掉什么？”
- “钻石镐怎么获得？”

推荐流程：

1. 先 `wiki_get_page(title)`
2. 优先读取 `sections`
3. 若有明确目标 section，优先用 section 文本回答
4. 若没有合适 section，再回退到 `plain_text`

### 8.2 列表枚举类问题

适用问题：

- “有哪些生物？”
- “哪些页面属于红石机制？”
- “有哪些不可堆叠物品？”

推荐流程：

1. 若问题天然对应分类，先查 category
2. 若问题天然对应模板或 infobox 字段，先查 template / infobox
3. 拿到候选页后，最多再对前 N 个结果调用 `wiki_get_page`
4. 用 agent 做去重、分组、总结

### 8.3 关系扩展类问题

适用问题：

- “哪些页面和红石有关？”
- “和按钮关联的机制有哪些？”

推荐流程：

1. 先 `wiki_get_page("按钮")`
2. 取 `wikilinks`
3. 如需要反向扩展，再 `wiki_find_backlinks("按钮")`
4. 从结果中按分类或模板做二次筛选

### 8.4 多跳问答

适用问题：

- “哪些红石相关方块可以通过合成获得？”
- “会掉落火药的敌对生物有哪些？”

推荐流程：

1. 先用 category/template 缩小候选集
2. 再对候选页取 bundle
3. 只在 agent 内做轻量逻辑过滤
4. 不要一上来全表扫描 `plain_text`

## 9. Python 集成示例

下面是最推荐的集成方式：在 agent 进程里直接 import 查询类。

```python
from src.storage import WikiSearchDB


class MinecraftWikiTool:
    def __init__(self, sqlite_path: str = "data/processed/sqlite/wiki.db") -> None:
        self.sqlite_path = sqlite_path

    def get_page(self, title: str) -> dict | None:
        with WikiSearchDB(self.sqlite_path) as db:
            return db.get_page_bundle(title)

    def find_by_category(self, category: str, limit: int = 20) -> list[dict]:
        with WikiSearchDB(self.sqlite_path) as db:
            return db.find_pages_by_category(category)[:limit]

    def find_by_template(self, template_name: str, limit: int = 20) -> list[dict]:
        with WikiSearchDB(self.sqlite_path) as db:
            return db.find_pages_by_template(template_name)[:limit]

    def find_by_template_param(
        self,
        template_name: str,
        param_name: str,
        param_value: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        with WikiSearchDB(self.sqlite_path) as db:
            return db.find_pages_by_template_param(
                template_name,
                param_name,
                param_value,
            )[:limit]

    def find_by_infobox(
        self,
        key: str,
        value: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        with WikiSearchDB(self.sqlite_path) as db:
            return db.find_pages_by_infobox(key, value)[:limit]
```

如果 agent 是 Python 实现，这就是首选方案。

## 10. CLI / 子进程集成示例

如果 agent 是 Java、TypeScript、Go 或游戏内脚本桥，最简单的方式是把当前 CLI 当成外部工具。

Python 例子：

```python
import json
import subprocess


def wiki_search_title(title: str) -> dict | None:
    result = subprocess.run(
        [
            "./.venv/bin/python",
            "-m",
            "src.main",
            "search",
            "--title",
            title,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)
```

优点：

- 不需要 agent 和仓库代码深度耦合
- 部署简单
- 语言无关

缺点：

- 每次调用都会启动一个 Python 进程
- 高频调用下延迟更高

如果你的 MC AI agent 会在一次会话中多次查 wiki，建议改成长驻 Python 服务或 MCP 工具。

## 11. MCP / Tool Server 接法

如果你的 agent 支持 MCP 或类似 tool protocol，建议把当前查询封成单独 server。

推荐只开放以下工具：

- `wiki.get_page`
- `wiki.find_by_category`
- `wiki.find_by_template`
- `wiki.find_by_template_param`
- `wiki.find_by_infobox`
- `wiki.find_backlinks`
- `wiki.find_sections`

每个工具都应当：

- 返回 JSON
- 支持 `limit`
- 在结果过多时截断
- 对空结果返回 `[]` 或 `null`
- 不返回原始 wikitext

推荐限制：

- 默认 `limit = 10`
- 最大 `limit = 100`
- `get_page` 返回完整 bundle
- 其余查询默认只返回 `pages` 基本字段，必要时再二次取 bundle

## 12. Agent 提示词建议

建议在系统提示或工具使用说明中明确以下规则：

1. 回答 Minecraft 知识问题前，优先调用 wiki 工具
2. 对具体实体名，先用 `wiki_get_page`
3. 对“有哪些/哪一些/属于某类”的问题，优先 category、template、infobox 查询
4. 不要把 `plain_text` 当成全文搜索库
5. 当页面是重定向时，信任工具返回的已解析目标页
6. 对结果过多的问题，先缩小条件，再回答
7. 没查到时明确说“本地 wiki 库中未命中”，不要编造

一个实用的内部策略是：

```text
如果问题看起来是在问“某个具体页面”，先查 title。
如果问题看起来是在问“某类对象”，先查 category/template/infobox。
如果问题看起来是在问“和某概念相关的页面”，先查 backlinks。
如果问题聚焦某类内容段落，如获取、用途、掉落物，优先查 section。
```

## 13. 面向 agent 的返回后处理建议

agent 不应该把查询结果原样复读给用户，而应该做轻量整理。

推荐后处理：

- 对页面列表只保留 `title`、`page_id`、必要时保留一小段 `plain_text`
- 对 `sections` 优先选择与问题最相关的 section
- 对 `templates` 不要整页全展开，优先抽取目标模板
- 对 `wikilinks` 可用于后续多跳查询，不必一次性全部展示

一个实用规则：

- 首次回答最多使用 1 到 3 个页面
- 如果用户继续追问，再扩展到更多页面

## 14. 常见任务的查询映射

“X 是什么”

- `wiki_get_page(X)`

“X 怎么获得”

- `wiki_get_page(X)` 后优先找 `sections.title in {"获取", "获得", "合成", "生成"}`

“X 有什么用途”

- `wiki_get_page(X)` 后优先找 `sections.title in {"用途", "用法", "行为"}`

“有哪些 Y”

- 先尝试 `wiki_find_by_category(Y)`
- 不合适时尝试 `wiki_find_by_template(...)`

“哪些页面和 X 相关”

- `wiki_find_backlinks(X)`

“哪些物品不可堆叠”

- `wiki_find_by_template_param("信息框/物品", "stackable", "否")`

“哪些页面有获取段落”

- `wiki_find_sections("获取")`

## 15. SQL 直连示例

如果你确实需要在 agent 外围写更复杂的组合逻辑，可以在非 LLM 代码里写 SQL。

示例：查分类为“生物”的非重定向页。

```sql
SELECT p.page_id, p.title, p.plain_text
FROM pages p
JOIN categories c ON c.page_id = p.page_id
WHERE c.category = '生物' AND p.is_redirect = 0
ORDER BY p.title;
```

示例：查“信息框/物品”里 `stackable = 否` 的页。

```sql
SELECT DISTINCT p.page_id, p.title
FROM pages p
JOIN template_params tp ON tp.page_id = p.page_id
WHERE tp.template_name = '信息框/物品'
  AND tp.param_name = 'stackable'
  AND tp.param_value = '否'
  AND p.is_redirect = 0
ORDER BY p.title;
```

示例：查页面“按钮”的所有 section。

```sql
SELECT s.ord, s.level, s.title, s.text
FROM sections s
JOIN pages p ON p.page_id = s.page_id
WHERE p.title = '按钮'
ORDER BY s.ord;
```

建议：

- SQL 写在外围服务里，不要让模型自由生成
- 查询默认加 `p.is_redirect = 0`
- 真正需要跳转时再调用标题解析

## 16. 生产注意事项

### 16.1 标题解析

由于当前库里重定向页很多，标题查找必须默认走重定向解析。当前 `get_page_bundle()` 已经这样实现。

### 16.2 限流

对 agent 而言，单次回复不要触发大量 bundle 查询。推荐：

- 首轮最多 1 到 3 次 `get_page_bundle`
- 首轮最多 1 次列表查询
- 不要对数百条列表结果逐页取详情

### 16.3 结果截断

当列表结果很多时：

- 只给 agent 前 10 到 20 个候选
- 必要时按标题排序或按业务规则筛掉明显噪声

### 16.4 数据更新

如果你重新抓取并重建索引：

```bash
./.venv/bin/python -m src.main crawl --resume
./.venv/bin/python -m src.main parse --all
./.venv/bin/python -m src.main index
```

对 agent 来说，数据库路径不变时通常不需要改代码，只需重启长驻连接或重新打开 SQLite 连接。

### 16.5 异常处理

建议 agent tool 层对以下情况做显式处理：

- 数据库不存在
- 数据库未初始化
- 查询结果为空
- 输入标题为空
- `limit` 超出上限

当前 CLI 在未建库时会返回清晰错误信息，而不是 SQLite traceback。

## 17. 推荐的最小集成方案

如果你要尽快把这份库接进 MC AI agent，最低成本方案如下：

1. 保持当前抓取与建库流程不变
2. 在 agent 工程里新增一个 Python 工具进程或直接 import 当前仓库
3. 只暴露 `get_page`、`find_by_category`、`find_by_template_param`、`find_backlinks`
4. 让 agent 先调用工具，再生成自然语言回答
5. 严格限制首轮查询次数和返回条数

这套最小方案已经足以覆盖大多数 Minecraft 知识问答。

## 18. 后续增强建议

如果之后要继续增强，但仍不想做 embedding，推荐优先级如下：

1. 增加组合查询 API，例如“category + template_param”
2. 增加 section 名称白名单映射，例如把“获取/获得/合成”归为 acquisition
3. 增加结果排序策略，例如优先非空 section、优先非重定向、优先正文更完整的页面
4. 增加长驻 HTTP/MCP 服务，避免重复启动 Python 子进程
5. 为高频问题建立专用工具，例如 `wiki_get_acquisition(title)`、`wiki_get_drops(title)`

如果未来发现“结构化字段筛完后仍需要正文关键词兜底”，再单独加 FTS 或其他全文检索层，不要在当前 agent 接入阶段提前复杂化。
