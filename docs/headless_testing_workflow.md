# Mina Headless Testing Workflow

## Overview

Mina now supports a headless `server-in-loop` testing workflow:

- no GUI Minecraft client is required
- the Fabric server still runs for real
- Carpet fake players are used as the automation actors
- Mina is triggered via `execute as <player> run mina <message>`
- Java-side turn logs and Python-side debug bundles are produced automatically

This workflow is intended to replace the old manual loop of:

1. start the Fabric server manually
2. start the Python agent manually
3. join with a real client
4. type `/mina ...`
5. manually hunt for the right debug trace

## One-Time Setup

Run from the repo root [mina](/Users/zhaozhiyu/Projects/mina):

```bash
cd /Users/zhaozhiyu/Projects/mina
./.venv/bin/python -m pip install -e agent_service
```

Recommended invocation style:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli ...
```

If you have already reinstalled the editable package, you can also use:

```bash
./.venv/bin/mina-dev ...
```

## Important Paths

Scenario files:

- [testing/headless/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/scenarios)

World template metadata:

- [testing/headless/world_templates](/Users/zhaozhiyu/Projects/mina/testing/headless/world_templates)

Java-side dev turn log during a run:

- `run/mina-dev/turns.jsonl`

Python-side debug bundle during a scenario run:

- `<scenario-output>/agent_data/debug/index.jsonl`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/request.start.json`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/response.progress.jsonl`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/response.final.json`
- `<scenario-output>/agent_data/debug/turns/<date>/<turn_dir>/scenario.capture.json`

Default headless output root:

- `tmp/headless/<timestamp>/`

Do not place `--output-root` inside `run/`.

## Running Real-Model Headless Regression

Set the model provider variables first:

```bash
cd /Users/zhaozhiyu/Projects/mina
export MINA_API_KEY='...'
export MINA_BASE_URL='https://api.deepseek.com/v1'
export MINA_MODEL='deepseek-chat'
```

Run the default scenario set:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-headless
```

Run selected scenarios only:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-headless \
  --scenario-id companion_smoke
```

Useful `run-headless` options:

- `--scenario-dir`: defaults to `testing/headless/scenarios`
- `--world-template-dir`: defaults to `testing/headless/world_templates`
- `--output-root`: defaults to `tmp/headless`
- `--agent-port`: defaults to auto-selecting a free local port
- `--server-ready-timeout`: server boot timeout
- `--agent-ready-timeout`: agent boot timeout
- `--turn-timeout`: per-turn timeout
- `--keep-going`: continue after a scenario failure

## Running the Stub Smoke Path

This is the fastest end-to-end validation path. It verifies:

- server boot
- fake player spawn
- Mina submission
- Java dev turn logging
- Python debug bundle generation

It does not validate real-model quality.

Run the stub workflow directly:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-headless \
  --agent-mode stub \
  --scenario-id companion_smoke
```

Run the gated smoke test:

```bash
MINA_RUN_HEADLESS_SMOKE=1 ./.venv/bin/python -m pytest agent_service/tests/test_headless_smoke.py -q
```

## Inspecting Recent Turns

Show recent turns from the default debug directory:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns --limit 10
```

Filter by player:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns \
  --player Steve \
  --limit 20
```

Inspect a specific headless run by pointing to that scenario's `agent_data/debug`:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli recent-turns \
  --debug-dir /Users/zhaozhiyu/Projects/mina/tmp/headless/20260323_120000/01_default/companion_smoke/agent_data/debug \
  --limit 20
```

## Promoting a Trace into a Regression Scenario

The normal workflow is:

1. reproduce a bad turn
2. get the `turn_id`
3. locate the matching bundle
4. promote that bundle into a checked-in scenario

Example:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli promote-trace \
  --debug-dir /Users/zhaozhiyu/Projects/mina/agent_service/data/debug \
  --turn-id 170fbc82-b0fc-484c-9f05-e00a1f099916 \
  --scenario-id my_new_case \
  --world-template default
```

This writes a scenario file under [testing/headless/scenarios](/Users/zhaozhiyu/Projects/mina/testing/headless/scenarios).

If the target file already exists, add:

```bash
--force
```

## Scenario File Format

Each scenario is a JSON file with this shape:

```json
{
  "scenario_id": "my_case",
  "world_template": "default",
  "player_name": "Steve",
  "setup_commands": ["time set day"],
  "message": "Mina，跟我说一句话。",
  "follow_up_messages": [],
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

Field meanings:

- `scenario_id`: stable scenario name
- `world_template`: world template id
- `player_name`: fake player name used by Carpet
- `setup_commands`: commands to run before Mina is called
- `message`: first Mina message
- `follow_up_messages`: optional extra turns in the same scenario
- `assertions`: pass/fail policy

## Assertion Semantics

- `expected_final_status`: required final status, usually `completed`
- `forbidden_statuses`: final statuses that should fail the scenario
- `required_capability_ids`: capability ids that must be used
- `forbidden_capability_ids`: capability ids that must not be used
- `confirmation_expected`: whether Mina should end in confirmation mode
- `required_reply_substrings`: weak positive string checks on the final reply
- `forbidden_reply_substrings`: weak negative string checks on the final reply
- `max_duration_ms`: upper bound on total scenario duration

The workflow intentionally does not do exact full-text reply snapshots for real-model runs.

## World Templates

Each world template must at least include a `template.json`.

Current example:

- [testing/headless/world_templates/default/template.json](/Users/zhaozhiyu/Projects/mina/testing/headless/world_templates/default/template.json)

The runner materializes an isolated server run directory from the template, then executes the scenario against that isolated state.

## What the Runner Actually Does

For each `world_template` group:

1. prepare an isolated server run directory under the current output root
2. temporarily swap that isolated run directory into the active `run/`
3. boot the Fabric server with `./gradlew runServer --no-daemon`
4. start the Python agent or local stub agent on a free local port
5. spawn the fake player
6. run setup commands
7. submit Mina turns through `execute as <player> run mina <message>`
8. wait for:
   - Java-side `accepted` and `completed` or `failed` events
   - Python-side turn bundle creation
9. copy the resulting server-side artifacts back into the isolated output directory
10. restore the original `run/`

## How to Debug Failures

Look here first:

1. runner terminal output
2. `<headless-output>/<group>/<scenario>/server/mina-dev/turns.jsonl`
3. `<headless-output>/<group>/<scenario>/agent_data/debug/index.jsonl`
4. turn bundle `response.final.json`
5. turn bundle `scenario.capture.json`
6. `<headless-output>/<group>/<scenario>/server/logs/latest.log`

If a scenario fails, the runner prints the exact `turn_id` and bundle path.

## Common Failure Categories

- `startup_failure`: server or agent did not start correctly
- `missing_accepted_turn`: Mina submission happened but Java-side accepted log was not observed
- `timeout`: the turn or scenario exceeded the configured timeout
- `missing_trace_bundle`: Java-side turn completed but Python bundle was not found
- `runtime_exception`: Mina finished in a failed state
- `unknown_capability_rejection`: model selected an invalid capability id
- `missing_required_capability`: required capability assertion failed
- `reply_assertion_failure`: weak reply text assertion failed

## Important Operational Notes

- Do not run another manual `./gradlew runServer` at the same time as headless testing.
- The runner temporarily takes over the active `run/` directory, then restores it.
- Local agent health checks explicitly bypass HTTP proxies, because proxy interception breaks localhost readiness checks.
- `--agent-port` normally should not be set manually.
- `recent-turns` defaults to the repo's default debug directory; for headless outputs under `tmp/headless/...`, pass `--debug-dir` explicitly.
- Stub mode is only for workflow validation, not answer-quality validation.
- In offline mode, Carpet fake-player spawn may still log Mojang profile lookup warnings or timeouts before continuing.

## Recommended Day-to-Day Loops

Fast loop while building infrastructure:

```bash
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-headless \
  --agent-mode stub \
  --scenario-id companion_smoke
```

Real-model loop while iterating on agent behavior:

```bash
export MINA_API_KEY='...'
export MINA_BASE_URL='https://api.deepseek.com/v1'
export MINA_MODEL='deepseek-chat'
PYTHONPATH=agent_service/src ./.venv/bin/python -m mina_agent.dev.cli run-headless
```

Bug-to-regression loop:

1. reproduce failure
2. note the `turn_id`
3. inspect `scenario.capture.json`
4. run `promote-trace`
5. tighten assertions if needed
6. rerun `run-headless`
