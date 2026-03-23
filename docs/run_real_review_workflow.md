# Reviewing `run-real` Output

## Purpose

This document is the standard checklist for reviewing a Mina `run-real` output directory.

It is designed for two goals:

1. quickly judge whether the run is healthy
2. separate true Mina behavior problems from fixture problems, assertion drift, or artifact bloat

Use this whenever a future conversation starts with a path like:

```text
tmp/headless/real/<timestamp>
```

## What To Check First

Given a run root:

```bash
RUN_ROOT=tmp/headless/real/<timestamp>
```

Start with:

```bash
sed -n '1,220p' "$RUN_ROOT/summary.json"
sed -n '1,220p' "$RUN_ROOT/scorecard.md"
sed -n '1,220p' "$RUN_ROOT/target_state_gaps.json"
```

Review in this order:

1. `infra_failures`
2. `behavior_gaps`
3. prompt health
4. `passed`
5. `skipped_planned`

Interpretation:

- `infra_failure` means the run is operationally unhealthy and the result set is incomplete
- `behavior_gap` means the run completed, but Mina did not meet the scenario target
- `passed` only means the current assertions passed, not that the scenario is semantically good

## Fast Triage Commands

List scenario bundles:

```bash
find "$RUN_ROOT" -path '*/response.final.json' | sort
find "$RUN_ROOT" -path '*/scenario.capture.json' | sort
```

Find the heaviest outputs:

```bash
du -ah "$RUN_ROOT" | sort -h | tail -n 80
```

Check special artifact classes:

```bash
find "$RUN_ROOT" -name '*.db' | sort
find "$RUN_ROOT" -path '*/prompts/*' -type f | wc -l
du -ah "$RUN_ROOT" | rg 'mina_agent\.db|prompts/step_.*provider_input\.json|debug\.log|latest\.log|turns\.jsonl|scenario\.capture\.json' | sort -h
```

## How To Judge Mina Output

For each failed or suspicious scenario, inspect the last bundle in `bundle_dirs` from `summary.json`.

Priority files:

1. `response.final.json`
2. `scenario.capture.json`
3. `events.jsonl`
4. `summary.json`
5. `prompts/step_*.provider_input.json`

Questions to answer:

1. Is Mina's final reply useful, grounded, and in character?
2. Did it use the expected capability ids?
3. If it failed with `unknown_capability_rejection`, did the model actually hallucinate a bad id, or did runtime lose a valid id?
4. If a passed reply sounds plausible, is it actually appropriate for the intended template?

## Prompt Review

Prompt review is part of the default `run-real` review flow.
Do not treat it as an optional deep-dive. Review the prompt before blaming Mina, the fixture, or the assertion.

For every failed scenario, and for at least a few representative passed scenarios, inspect the latest `prompts/step_*.provider_input.json`.

### What To Check

1. Prompt arrangement

- Confirm the assembled context is ordered from stable to volatile.
- The expected section pattern is:
  - `stable_core`
  - `runtime_policy`
  - `scene_slice`
  - `observation_brief`
  - `task_focus`
  - `confirmation_loop` when relevant
  - `dialogue_continuity`
  - `dialogue_history`
  - `recoverable_history`
  - `capability_brief`
- If highly volatile data is injected before stable sections, flag it as a prompt-layout problem.
- If `capability_brief` is missing, truncated, or buried behind irrelevant noise, treat capability-selection failures as prompt problems first.

2. Prefix-cache friendliness

- Compare `step_001.provider_input.json` and later step prompt files for the same turn.
- The early system prompt prefix should stay highly stable across steps within the same run group.
- Stable instructions and stable policy text should remain at the front; dynamic facts should be appended later.
- If early prompt bytes change every step because dynamic sections are inserted too early, flag poor prefix-cache utilization.
- If two similar scenarios in the same group produce wildly different early system prefixes, inspect why.

3. Duplicate content

- Check whether the same fact is repeated across `scene_slice`, `observation_brief`, `task_focus`, and dialogue blocks.
- Check for repeated runtime notes, repeated delegate summaries, or repeated capability descriptions.
- Repetition is especially suspicious when it inflates prompt size without adding new constraints.
- If the same actionable fact appears in 2-3 sections verbatim, flag it as prompt bloat.

4. Content sufficiency

- Verify that the prompt contains the actual facts needed for the user request.
- For target questions, confirm prompt contains target status or a visible target-read capability.
- For social questions, confirm prompt contains nearby-player facts or at least `server_env.current_players`.
- For village / POI questions, confirm the prompt makes the POI capability visible and the village setup facts reachable.
- For low-health / night-danger questions, confirm prompt contains real health, threat, and position state after setup.
- For local knowledge questions, confirm prompt clearly exposes `retrieval.local_knowledge.search` and the user’s explicit preference not to rely on live observation.

5. Truncation and budget pressure

- Inspect `summary.json` message stats and truncation fields alongside the prompt files.
- If prompt size is near budget, ask whether important facts were slimmed or displaced.
- If a wrong answer coincides with heavy truncation, treat prompt compaction and duplication as first-order suspects.

6. Prompt-to-output alignment

- Ask whether Mina’s bad choice was actually encouraged by the prompt.
- If the prompt strongly nudges delegate behavior when a direct capability should be preferred, that is a prompt-policy problem.
- If the prompt lacks explicit instructions for a user-stated preference such as “别直接接管” or “直接用本地知识库”, treat that as prompt insufficiency.

### Practical Commands

List prompt files:

```bash
find "$RUN_ROOT" -path '*/prompts/step_*.provider_input.json' | sort
```

Compare early prompt prefixes across steps:

```bash
python3 - <<'PY'
from pathlib import Path
import json

prompt_files = sorted(Path(".").glob("$RUN_ROOT/**/prompts/step_*.provider_input.json"))
for path in prompt_files[:10]:
    payload = json.loads(path.read_text())
    first = payload[0]["content"] if payload else ""
    print(path)
    print(first[:1200].replace("\n", "\\n"))
    print()
PY
```

Quick duplication sniff:

```bash
python3 - <<'PY'
from pathlib import Path
import json
from collections import Counter

for path in sorted(Path(".").glob("$RUN_ROOT/**/prompts/step_*.provider_input.json"))[:10]:
    payload = json.loads(path.read_text())
    text = "\n".join(str(item.get("content", "")) for item in payload if isinstance(item, dict))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dupes = [line for line, count in Counter(lines).items() if count > 1]
    if dupes:
        print(path)
        print("duplicates:", dupes[:10])
PY
```

## Important Distinction: Behavior Gap vs Fixture Problem

A scenario can fail for very different reasons:

### Real behavior problem

Examples:

- Mina talks past the player
- Mina overuses tools
- Mina fails to ask for clarification
- Mina refuses to use an actually available capability

### Assertion drift

Examples:

- scenario expects `game.player_snapshot.read`
- current runtime exposes `observe.player`
- reply is grounded, but the test still fails because the asserted id is stale

### Fixture problem

Examples:

- `cave_underground` scenario actually spawns on surface
- `home_base_storage` scenario has no real storage nearby
- `technical_carpet_lab` is only a renamed default world

In those cases, a pass/fail label is less informative than the fixture mismatch itself.

## How To Detect Unknown Capability Parsing Bugs

When a scenario reports `unknown_capability_rejection`, inspect `events.jsonl`.

Look for this pattern:

1. `model_response.raw_response_preview` contains a valid capability id
2. `model_decision` has `capability_id: null`
3. `capability_rejected` reports `capability_id: "<empty>"`

If this pattern appears, the issue is probably not “model guessed a nonexistent capability”.
It is more likely a decision parsing / normalization / bridging bug between provider output and runtime decision handling.

## How To Audit Output Redundancy

Treat artifacts in three buckets.

### Keep by default

- `summary.json`
- `scorecard.md`
- `failing_cases.json`
- `target_state_gaps.json`
- `server/mina-dev/turns.jsonl`
- `server/logs/latest.log`
- per-turn `response.final.json`
- per-turn `scenario.capture.json`
- per-turn `events.jsonl`
- per-turn `prompts/step_*.provider_input.json`

These are small and high-value for debugging and discussion.

### Keep conditionally

- `agent_data/mina_agent.db`
- `server/logs/debug.log`

These are useful for deep debugging, but not always necessary for every successful run.

### Likely largest redundant outputs

- `server/world/**`
- especially `server/world/region/*.mca`

If the goal is only post-run review, these files are usually the dominant storage cost and often not needed after the run completes.
They are mainly useful when you want exact world-state reproduction or deep server-side inspection.

## Recommended Interpretation Rules

When a scenario passes:

- check whether the reply is semantically appropriate for the template
- if the template is weak, do not over-trust the pass

When a scenario fails with `missing_required_capability`:

- verify the current capability namespace before blaming Mina
- if the runtime currently exposes renamed ids, mark it as assertion drift first

When a scenario fails with `unknown_capability_rejection`:

- inspect whether the requested id was actually valid in `model_response`
- if runtime later sees `<empty>`, treat it as a runtime bug, not a model quality failure

When a scenario fails with `startup_failure`:

- treat the run as operationally incomplete
- inspect whether the agent failed to bind, failed health check, or crashed between scenarios

When a scenario fails with `runtime_exception` and the underlying trace shows provider transport errors such as `unexpected_provider_error` or `IncompleteRead(...)`:

- do not immediately classify it as a Mina behavior failure
- first separate it into provider / transport instability inside an otherwise completed run
- keep it distinct from groundedness, companionship, and capability-selection judgments

When a scenario fails with `missing_required_capability` but the final reply is clearly grounded in facts already available in the scoped turn context:

- do not immediately conclude that Mina “should have used a tool”
- first verify whether the answer could already be supported by built-in context such as `server_env.current_players`, scoped nearby-state facts, or dialogue continuity
- if yes, treat it as assertion pressure or assertion drift before treating it as a product miss

## Example Findings From `tmp/headless/real/20260323_150453`

### 1. The pass/fail summary

- Passed: 10
- Infra failures: 1
- Behavior gaps: 11
- Skipped planned: 12

This run is not healthy enough to treat the pass count as a product score, because it contains an infra failure and multiple runtime-class behavior gaps.

### 2. Companion-style outputs are broadly reasonable

Examples:

- `real_companion_greeting_day` produced a natural, light greeting
- `real_companion_reassurance_without_takeover` gave calm reassurance without grabbing control
- `real_overtalk_restraint_after_simple_answer` stayed short

These are positive signs for Mina's tone.

### 3. Several “passes” were semantically misleading because templates were not real fixtures

Historically, the template directories only carried `template.json` server properties and no checked-in `world/` fixture.

That is no longer the intended setup:

- there is now a shared checked-in base world
- multiple templates may inherit it through `world_source_template`
- scenario-level `setup_commands` are responsible for building stable cave / base / village / threat scenes on top of that shared world

Review implication:

- old runs may still be invalid because the template layer was too weak
- for new runs, inspect both the shared world fixture and the scenario `setup_commands` before concluding that a template is broken

### 4. Several `unknown_capability_rejection` failures are likely runtime parsing bugs

In multiple failing traces:

- `model_response.raw_response_preview` contains valid ids such as `game.target_block.read` or `server.rules.read`
- later `capability_rejected` reports `capability_id: "<empty>"`

That suggests the rejection is not pure model hallucination.
It looks like the capability id is being lost between model output and runtime decision consumption.

### 5. Several `missing_required_capability` failures look like assertion drift

The run artifacts show current capability ids such as:

- `observe.player`
- `observe.inventory`
- `observe.scene`
- `observe.social`

But several scenarios still assert older ids such as:

- `game.player_snapshot.read`
- `carpet.distance.measure`
- `retrieval.local_knowledge.search`

Some failures therefore look like test-definition drift rather than strictly bad Mina behavior.

### 6. The run output contains large redundant world snapshots

This run is about 69 MB.

Largest contributor:

- `server/world/region/*.mca` repeated across four group directories, about 63 MB total

Moderate contributors:

- `agent_data/mina_agent.db` files, about 3.9 MB total
- `server/logs/debug.log`, about 1.8 MB total

Small but valuable:

- `scenario.capture.json`
- `response.final.json`
- `turns.jsonl`
- `scorecard.md`

## Suggested Follow-Up Actions After A Review

1. Fix template realism before trusting semantic pass rates.
2. Fix capability parsing before treating `unknown_capability_rejection` as model blame.
3. Reconcile scenario assertions with the current capability namespace.
4. Make world snapshots optional for successful runs, or only keep them behind a debug flag.
5. Consider keeping prompt artifacts and SQLite DBs only on failure, strict mode, or explicit debug runs.

## Additional Findings From `tmp/headless/real/20260323_152405`

### 1. Zero infra failures does not guarantee an operationally clean run

This run reports:

- Passed: 14
- Infra failures: 0
- Behavior gaps: 16

But `real_observability_brief` still failed with:

- `runtime_exception: final turn failed: unexpected_provider_error`
- provider payload showing `IncompleteRead(843 bytes read)`

So a run can be infra-clean at the suite level while still containing transport-level provider failures inside individual scenarios.

### 2. The unknown-capability parse-loss pattern is now confirmed twice

In the latest target-block trace:

- `model_response.raw_response_preview` requested `game.target_block.read`
- `model_decision` still had `capability_id: null`
- runtime rejected `<empty>`

This strengthens the earlier conclusion that a meaningful part of `unknown_capability_rejection` is a runtime parsing / normalization bug, not just model hallucination.

### 3. Some missing-capability gaps are really “grounded direct reply” cases

Examples:

- `real_social_presence_basic`
- `real_two_player_social_read`
- `real_nearby_threat_basic`

These replies were grounded and useful, but they answered directly from already-scoped context instead of calling the asserted capability id.

Review implication:

- distinguish “bad answer” from “the scenario insisted on a tool call the model did not actually need”

### 4. Repeated world-template prefixes in one run root may be expected

This run contains pairs such as:

- `technical_carpet_lab__...__0e59adb3`
- `technical_carpet_lab__...__bd2377ad`
- `village_social__...__0e59adb3`
- `village_social__...__bd2377ad`

That is expected because the runner groups by:

- world template
- feature flags
- actor role profile

So repeated template names are not automatically duplicate work or a bug.
They often mean the same template had to be re-run under different actor setups.

### 5. Actor-profile grouping can dominate storage growth

This newer run is about 135 MB, roughly double the earlier 69 MB example.
The main reason is not richer per-turn bundles, but more grouped server directories, each carrying its own copied `server/world/region/*.mca`.

Review implication:

- when run size jumps, check group count first
- world snapshots scale with the number of server groups, not just scenario count

## Reusable Prompt For Future Conversations

You can start a future review session with:

```text
检查一下这个 run-real 输出目录：<RUN_ROOT>。
按 docs/run_real_review_workflow.md 的流程做：
1. 先给出 infra failure / behavior gap / passed 总结
2. 抽样检查 Mina 回复是否合理
3. 判断哪些问题是 Mina 行为问题，哪些是 fixture 或 assertion drift
4. 检查输出里是否有冗余文件
5. 最后给出可执行的后续改进建议
```
