# Mina Headless Testing Workflow

## 概述

Mina 的 headless 测试现在明确分成两条线：

- `functional`：确定性工程测试，硬门禁，任何失败都算回归
- `real`：真实模型目标状态评估，统一使用真实 LLM，全量跑 `real` 场景，允许部分行为失败，但不允许基础设施失败

两条线都使用同一套 server-in-loop 机制：

- 真实 Fabric server 启动
- 用 Carpet fake player 代替 GUI 客户端
- 通过 `execute as <player> run mina <message>` 触发 Mina
- 自动生成 Java turn log 和 Python debug bundle

这套流程的目标是替代手动开游戏、手动输入 `/mina`、手动定位 trace 的旧测试方式。

## 一次性准备

在仓库根目录 [mina](/Users/zhaozhiyu/Projects/mina) 下执行：

```bash
cd /Users/zhaozhiyu/Projects/mina
./.venv/bin/python -m pip install -e agent_service
```

推荐统一使用：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli ...
```

如果已经通过 editable install 安装过，也可以直接用：

```bash
./.venv/bin/mina-dev ...
```

## 目录结构

场景目录：

- functional: [testing/headless/functional/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/functional/scenarios)
- real: [testing/headless/real/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/real/scenarios)

世界模板目录：

- [testing/headless/world_templates](/Users/zhaozhiyu/Projects/mina/testing/headless/world_templates)

默认输出目录：

- functional: `tmp/headless/functional/<timestamp>/`
- real: `tmp/headless/real/<timestamp>/`

Java 侧 turn log：

- 运行期间写到活动 `run/mina-dev/turns.jsonl`
- 场景结束后同步到本次输出目录里的 `server/mina-dev/turns.jsonl`

Python 侧 debug bundle：

- `<scenario-output>/agent_data/debug/index.jsonl`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/request.start.json`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/response.progress.jsonl`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/response.final.json`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/scenario.capture.json`

不要把 `--output-root` 放到 `run/` 目录下面。

## 运行 Functional Suite

`functional` 只跑功能性场景，默认用 stub agent，适合验证：

- server 启动与 fake player 触发链路
- Java turn log 写入
- Python debug bundle 生成
- 多 turn capture
- headless runner 基础流程

直接运行：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-functional
```

只跑某个功能场景：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-functional \
  --scenario-id functional_stub_companion_smoke
```

常用参数：

- `--scenario-dir`：默认 `testing/headless/functional/scenarios`
- `--world-template-dir`：默认 `testing/headless/world_templates`
- `--output-root`：默认 `tmp/headless/functional`
- `--agent-mode`：默认 `stub`，也可以显式改成 `real`
- `--agent-port`：默认自动选择空闲端口
- `--server-ready-timeout`
- `--agent-ready-timeout`
- `--turn-timeout`

退出码语义：

- 只要有任何 `infra_failure` 或 `behavior_gap`，`run-functional` 就返回非零

## 运行 Real Suite

`real` 套件统一使用真实模型，不再分层。它描述的是 Mina 的目标状态，而不是当前硬门禁。

先设置模型环境变量：

```bash
cd /Users/zhaozhiyu/Projects/mina
export MINA_API_KEY='...'
export MINA_BASE_URL='https://api.deepseek.com/v1'
export MINA_MODEL='deepseek-chat'
```

运行整套 `real` 场景：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real
```

只跑某一个真实场景：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real \
  --scenario-id real_companion_greeting_day
```

已知问题场景默认不会执行；如果要一起跑：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real \
  --include-known-issues
```

如果希望任何行为缺口都返回非零：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real \
  --strict-real
```

常用参数：

- `--scenario-dir`：默认 `testing/headless/real/scenarios`
- `--world-template-dir`：默认 `testing/headless/world_templates`
- `--output-root`：默认 `tmp/headless/real`
- `--agent-port`：默认自动选择空闲端口
- `--server-ready-timeout`
- `--agent-ready-timeout`
- `--turn-timeout`
- `--include-known-issues`
- `--strict-real`
- `--max-infra-failures`

退出码语义：

- 出现 `startup_failure`、`missing_accepted_turn`、`timeout`、`missing_trace_bundle` 等基础设施失败时，返回非零
- `expectation=required` 的 `real` 场景失败时，返回非零
- `expectation=target_state` 或 `expectation=known_issue` 的行为缺口，默认只记入报告，不让整套命令失败
- 传 `--strict-real` 后，任何行为缺口都会返回非零

## Real Suite 报告产物

`run-real` 每次运行都会在输出根目录生成：

- `summary.json`
- `failing_cases.json`
- `target_state_gaps.json`
- `scorecard.md`

其中：

- `summary.json`：完整记录、计数、planned/known issue 数量
- `failing_cases.json`：所有 infra failure 和 behavior gap
- `target_state_gaps.json`：只保留 `target_state`/`known_issue` 的行为缺口
- `scorecard.md`：按 `Infra Failures`、`Required Failures`、`Target-State Gaps`、`Known Issues Still Reproducing` 汇总

## 查看最近 Turn

查看默认 debug 目录里的最近 turn：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns --limit 10
```

按玩家过滤：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns \
  --player Steve \
  --limit 20
```

按 session 过滤：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns \
  --session <session_ref> \
  --limit 20
```

查看某次 headless run 的 turn，需要显式指向那次输出目录里的 `agent_data/debug`：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns \
  --debug-dir /Users/zhaozhiyu/Projects/mina/tmp/headless/functional/20260323_120000/01_overworld_day_spawn__exp0_dyn0__xxxxxxx/functional_stub_companion_smoke/agent_data/debug \
  --limit 20
```

## 从 Trace 提升成场景

`promote-trace` 用来把某个 turn 的 bundle 直接转成 checked-in 场景文件。

典型流程：

1. 跑出一个问题 turn
2. 记下 `turn_id`
3. 用 `recent-turns` 或输出目录定位 `debug_dir`
4. 运行 `promote-trace`
5. 按需要手动收紧 assertions
6. 重新执行该场景

示例：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli promote-trace \
  --debug-dir /Users/zhaozhiyu/Projects/mina/agent_service/data/debug \
  --turn-id 170fbc82-b0fc-484c-9f05-e00a1f099916 \
  --suite real \
  --scenario-id promoted_case \
  --world-template overworld_day_spawn
```

如果目标文件已存在，追加：

```bash
--force
```

输出目录规则：

- `--suite functional` 默认写到 [testing/headless/functional/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/functional/scenarios)
- `--suite real` 默认写到 [testing/headless/real/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/real/scenarios)
- 也可以显式传 `--output-dir`

提升规则：

- legacy/manual trace 会自动升级到新 schema
- 如果 capture 里只有旧结构，CLI 会用 `assertion_slots.suggested_assertions` 预填新场景的 `assertions`
- 如果 capture 里没有 `world_template`，必须手动传 `--world-template`

## 场景 Schema

当前 headless 场景统一使用这套结构：

```json
{
  "suite": "real",
  "scenario_id": "real_companion_greeting_day",
  "world_template": "overworld_day_spawn",
  "status": "runnable_now",
  "expectation": "target_state",
  "feature_flags": {
    "enable_experimental": false,
    "enable_dynamic_scripting": false
  },
  "actors": [
    {
      "actor_id": "player",
      "name": "Steve",
      "role": "read_only",
      "operator": false,
      "experimental": false,
      "spawn_commands": []
    }
  ],
  "turns": [
    {
      "actor_id": "player",
      "message": "Mina，跟我打个招呼。",
      "setup_commands_before": []
    }
  ],
  "quality_review": {
    "enabled": false,
    "judge": "codex",
    "rubric_id": null
  },
  "setup_commands": [],
  "assertions": {
    "expected_final_status": "completed",
    "forbidden_statuses": ["failed"],
    "required_capability_ids": [],
    "forbidden_capability_ids": [],
    "confirmation_expected": false,
    "required_reply_substrings": [],
    "forbidden_reply_substrings": [],
    "max_duration_ms": 120000
  }
}
```

字段含义：

- `suite`：`functional` 或 `real`
- `status`：`runnable_now` 或 `planned`
- `expectation`：
  - `required`：失败应被视为硬失败
  - `target_state`：目标状态，允许暂时失败
  - `known_issue`：已知问题，默认跳过，只有 `--include-known-issues` 才会跑
- `feature_flags`：控制实验能力面
- `actors`：多人场景/权限场景配置
- `turns`：场景内的多 turn 对话
- `quality_review`：是否启用外部 Codex 评审
- `setup_commands`：场景级预设命令
- `assertions`：结构、能力、弱文本、时长约束

## Assertion 语义

- `expected_final_status`：通常为 `completed`
- `forbidden_statuses`：禁止的最终状态
- `required_capability_ids`：必须出现的 capability id
- `forbidden_capability_ids`：不允许出现的 capability id
- `confirmation_expected`：是否应该进入确认态
- `required_reply_substrings`：最终回复必须包含的片段
- `forbidden_reply_substrings`：最终回复不应包含的片段
- `max_duration_ms`：总耗时上限

`real` 套件故意不做整段回复快照对比，而是采用结构断言、能力断言和弱文本断言。

## 质量评审

部分 `real` 场景可以启用 Codex 质量评审：

```json
"quality_review": {
  "enabled": true,
  "judge": "codex",
  "rubric_id": "companion_quality_golden"
}
```

当前 runner 通过环境变量 `MINA_CODEX_REVIEW_CMD` 调用外部评审命令。未配置时，这类场景会记录为 `skipped_unavailable`，不会导致整套 `real` 失败。

## 世界模板

当前模板池包括：

- `overworld_day_spawn`
- `overworld_night_danger`
- `cave_underground`
- `village_social`
- `home_base_storage`
- `technical_carpet_lab`
- `nether_entry`
- `experimental_sandbox_lab`

每个模板目录下至少需要 `template.json`。如果模板目录还包含 `world/`、`config/`，runner 会一起物化到临时 run dir。

## Runner 的实际执行方式

对每个分组后的模板配置，runner 会：

1. 按 `world_template + feature_flags + actor role profile` 分组
2. 为该组物化一个隔离的 server run dir
3. 临时接管活动 `run/`
4. 启动 Fabric server
5. 启动 stub agent 或真实 agent
6. 生成所需 fake player
7. 执行场景级和 turn 级 setup commands
8. 提交 Mina turn
9. 等待 Java 侧 `accepted -> completed/failed`
10. 等待 Python bundle 出现
11. 把 server 产物同步回本次输出目录
12. 恢复原始 `run/`

之所以采用“接管活动 `run/`”而不是直接改 Loom runDir，是为了规避 `eula` 和停服流程上的不稳定行为。

## 常见失败类别

- `startup_failure`
- `missing_accepted_turn`
- `timeout`
- `missing_trace_bundle`
- `runtime_exception`
- `unknown_capability_rejection`
- `missing_required_capability`
- `reply_assertion_failure`
- `quality_review_failure`

其中前四类默认视为基础设施失败。

## 排查顺序

场景失败时，优先查看：

1. runner 终端输出中的 `[FAIL] ... turn_id=... bundle=...`
2. `<scenario-output>/server/mina-dev/turns.jsonl`
3. `<scenario-output>/agent_data/debug/index.jsonl`
4. turn bundle 里的 `response.final.json`
5. turn bundle 里的 `scenario.capture.json`
6. `<scenario-output>/server/logs/latest.log`

## 日常推荐用法

快速回归 headless 基建：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-functional
```

针对某个 functional case 调试：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-functional \
  --scenario-id functional_followup_multiturn_bundle
```

评估当前真实模型状态：

```bash
export MINA_API_KEY='...'
export MINA_BASE_URL='https://api.deepseek.com/v1'
export MINA_MODEL='deepseek-chat'
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real
```

严格模式下把所有目标状态缺口都当失败：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-real --strict-real
```

从失败 trace 生成新回归场景：

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns --limit 10
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli promote-trace \
  --turn-id <turn_id> \
  --suite real \
  --scenario-id <new_case> \
  --world-template overworld_day_spawn
```

## 运行注意事项

- 不要在 headless runner 执行时手动再开一个 `./gradlew runServer`
- runner 执行期间会暂时接管活动 `run/`
- 本地健康检查会显式绕过 HTTP 代理；否则某些代理环境会错误拦截 `127.0.0.1`
- `stub` 模式只验证链路，不验证真实回复质量
- fake player 在离线模式下可能先尝试 Mojang profile 查询，日志里偶尔会出现 403 或超时，但通常不会影响最终生成
