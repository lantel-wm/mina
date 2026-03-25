# Mina

Mina is a Minecraft 1.21.11 dedicated-server agent runtime built as:

- a Fabric server mod that stays a thin bridge
- an external Python agent service that owns orchestration, memory, retrieval, model calls, and policy-aware continuation flow

## Current MVP shape

- Fabric side exposes a single `/mina <message>` natural-language entrypoint.
- Java talks to the Python service over local HTTP/JSON.
- Java collects scoped player/world context, exposes structured read capabilities, enforces policy, and executes on Minecraft's server thread.
- Python owns the agent loop and can either return a final reply or ask Java to execute a structured capability batch and resume the turn.

## Java mod

The Fabric mod now includes:

- dedicated-server-only command registration
- async turn coordination so model/network work never blocks the main thread
- a scoped context collector
- a visibility-aware capability registry
- an execution guard that re-checks risk, capability visibility, budgets, and preconditions
- Carpet-backed structured read capabilities:
  - `game.player_snapshot.read`
  - `game.target_block.read`
  - `server.rules.read`
  - `carpet.block_info.read`
  - `carpet.distance.measure`
  - `carpet.mobcaps.read`

Build:

```bash
./gradlew build --no-daemon
```

## Python agent service

The external runtime lives in `agent_service/` and includes:

- FastAPI HTTP entrypoints
- pydantic request/response schemas
- SQLite-backed sessions, turns, step events, execution records, memories, pending confirmations, and document chunks
- a unified capability registry covering tool, skill, retrieval, and script kinds
- a continuation-based agent loop
- a structured Minecraft Wiki knowledge layer backed by `mc_wiki/data/processed/sqlite/wiki.db`
- an OpenAI-compatible provider adapter
- a disabled-by-default sandboxed script runner scaffold for future experimental use

Use the repository virtual environment at `.venv`:

```bash
./.venv/bin/python -m pip install -e agent_service
./.venv/bin/python -m uvicorn mina_agent.main:app --app-dir agent_service/src --host 127.0.0.1 --port 8787
```

If you want local overrides, copy:

- `config/mina.properties.example` to `config/mina.properties`
- `agent_service/config.example.json` to `agent_service/config.local.json`

Python-side agent debug tracing is disabled by default. To enable structured per-turn debug logs for the agent loop, set either:

- `"debug_enabled": true` in `agent_service/config.local.json`
- or `MINA_AGENT_DEBUG_ENABLED=1` in the service environment

When enabled, the service writes compact debug traces under `agent_service/data/debug/turns/<YYYY-MM-DD>/<turn_id>/`:

- `summary.json` for coding-agent-friendly turn summaries
- `events.jsonl` for step-by-step structured trace events

The debug recorder now also writes executable bundle artifacts per turn:

- `request.start.json` for the normalized turn-start request
- `response.progress.jsonl` for emitted action batches and progress updates
- `response.final.json` for the final reply payload
- `scenario.capture.json` for a promotion-ready scenario bundle

It also keeps a global lookup file at `agent_service/data/debug/index.jsonl`, so a turn can be resolved directly from its `turn_id`.

## Headless Regression Workflow

Mina now supports a headless `server-in-loop` workflow that uses Carpet fake players plus `execute as <player> run mina ...` instead of a GUI Minecraft client.

Detailed usage is documented in [docs/headless_testing_workflow.md](/Users/zhaozhiyu/Projects/mina/docs/headless_testing_workflow.md).

Install the editable Python package once:

```bash
./.venv/bin/python -m pip install -e agent_service
```

Run the deterministic functional suite:

```bash
./.venv/bin/python -m mina_agent.dev.cli run-functional
```

Run the full real-model target-state suite:

```bash
./.venv/bin/python -m mina_agent.dev.cli run-real
```

By default, `run-real` prunes its output down to review-essential artifacts only. Use `--keep-full-artifacts` if you explicitly want the full server world, prompt buffers, SQLite DBs, and other deep-debug outputs.

```bash
./.venv/bin/python -m mina_agent.dev.cli run-real --keep-full-artifacts
```

Run the real suite in strict mode so any behavior gap returns non-zero:

```bash
./.venv/bin/python -m mina_agent.dev.cli run-real --strict-real
```

Run only the dedicated wiki real-scenario category:

```bash
./.venv/bin/python -m mina_agent.dev.cli run-real --scenario-category wiki
```

Run the included functional smoke scenario against the local stub agent:

```bash
./.venv/bin/python -m mina_agent.dev.cli run-functional --scenario-id functional_stub_companion_smoke
```

Headless outputs are stored under `tmp/headless/functional/<timestamp>/` or `tmp/headless/real/<timestamp>/`. Each scenario run keeps:

- an isolated Fabric run dir
- an isolated Python `agent_data/` directory
- Java-side turn logs under `<server-run-dir>/mina-dev/turns.jsonl`
- Python-side debug bundles under `<scenario-run-dir>/agent_data/debug/turns/...`

Real-suite runs also emit `summary.json`, `failing_cases.json`, `target_state_gaps.json`, and `scorecard.md` at the run root.
By default they are pruned to review-only artifacts after the run finishes.
Those review-only artifacts now include per-turn prompt buffers so prompt quality can be reviewed by default.

Useful developer commands:

```bash
./.venv/bin/python -m mina_agent.dev.cli recent-turns --limit 10
./.venv/bin/python -m mina_agent.dev.cli promote-trace --suite real --turn-id <turn_id> --scenario-id <new_case> --world-template overworld_day_spawn
```

Scenario files live under `testing/headless/functional/scenarios/` and `testing/headless/real/scenarios/`. World template metadata lives under `testing/headless/world_templates/`.

## Local wiki knowledge

Mina now treats `mc_wiki/data/processed/sqlite/wiki.db` as the authoritative local knowledge source for Minecraft facts.
The `mc_wiki` crawler and parser remain the data pipeline; the agent service only reads the processed SQLite contract.
