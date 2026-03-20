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
- a local knowledge index under `agent_service/data/knowledge/`
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

## Local knowledge seed

The repo now seeds a minimal local knowledge base in `agent_service/data/knowledge/` so retrieval can start small instead of waiting for a full Minecraft corpus.
