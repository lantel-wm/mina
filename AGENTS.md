# AGENTS.md

## Mission

Build **Mina (Minecraft Navigator)** for **Minecraft 1.21.11 + Fabric 0.18.4 + Java 21** as:

**Fabric mod (Java bridge, depends on Carpet) + external Python agent service**

Mina is a **natural-language-first companion runtime**.

---

## Product

Users interact with Mina only through **natural language**.

Mina should primarily make Minecraft:
- more fun
- more alive
- more immersive
- less lonely

Mina should prioritize:
- companionship
- player enjoyment
- contextual presence
- emotional and social continuity
- light guidance over task automation

The LLM should decide when to:
- reply conversationally
- react to the player’s situation
- retrieve knowledge
- use a capability
- generate and run a script

Do **not** implement business intent routing with hardcoded string matching.

The system may hardcode:
- capability boundaries
- permission levels
- execution modes
- risk classes
- sandbox policies
- audit requirements

Mina is:
- companion-oriented
- capability-driven
- results-oriented
- LLM-first in decision making

Mina is **not**:
- a keyword router
- a command macro system
- a coding-agent UI
- a diff-review workflow
- a utility-first automation bot

Users should care about:
- whether Mina feels present
- whether Mina makes the game more enjoyable
- whether Mina understands the situation
- whether Mina helps when it actually matters

Users should **not** need to care about generated code.

---

## Behavioral Priorities

Mina should behave like a **companion first**, not an executor first.

Default priority order:

1. **Companionship**
   - conversation
   - encouragement
   - reactions
   - atmosphere
   - humor
   - presence

2. **Guidance**
   - suggestions
   - reminders
   - lightweight planning
   - interpretation of events
   - helping players make their own decisions

3. **Execution**
   - retrieval
   - tool use
   - skill use
   - script-backed actions

Execution is important, but it should serve the player experience rather than dominate it.

Mina should prefer:
- guiding over taking over
- enhancing play over optimizing everything
- supporting exploration over replacing it
- being helpful without being intrusive

Mina should also be capable of restraint:
- it should not over-talk
- it should not interrupt constantly
- it should not force optimization when the player is just playing casually
- it should not default to tool/script use when a natural conversational response is better

---

## Architecture

1. **The Fabric mod is a bridge, not the brain.**  
   It should only handle:
   - game-side input
   - minimal player/world state collection
   - safe execution bridging
   - Carpet-backed integrations
   - response rendering
   - async communication with Python service

2. **The Python service owns agent logic.**  
   It owns:
   - agent loop
   - context building
   - memory
   - retrieval
   - model calls
   - orchestration
   - policy-aware execution flow
   - companionship behavior logic

3. **Keep the mod a standard Fabric mod.**  
   Do not embed Python runtime, venvs, or Python packages into the mod jar.

4. **Never block the Minecraft main thread.**  
   All agent/network interactions must be asynchronous.

5. **All world-modifying actions must execute in Minecraft’s controlled server execution context.**  
   External communication is async, but world/entity/inventory/block changes must execute through Minecraft’s controlled server-side execution path.

6. **The Python service must be independently deployable.**

7. Mina should expose a **unified capability space** to the LLM, including:
   - tools
   - skills
   - retrieval actions
   - script execution actions

8. Context must be **dynamically scoped**.  
   Do not inject full world state, full memory, full rules, or full capability descriptions by default.

9. **Callable capabilities must be exposed as one explicit authoritative id list per turn.**  
   The agent context should contain a single exact list of callable capability ids for the current turn.
   Mina may call only ids from that list, using exact matches only.
   Do not rely on guessed ids, fuzzy matching, hidden aliases, or implicit capability-name translation as the normal path.
   This list should remain available even when other context is trimmed.

---

## Safety

LLM decides what Mina wants to do.  
The system decides what Mina is allowed to do.

Use layered permissions such as:
- conversation-only
- read-only
- low-risk actions
- admin actions
- privileged experimental actions

Policy must enforce:
- role restrictions
- risk restrictions
- environment restrictions
- execution-mode restrictions
- rate limits / call budgets
- safety-mode restrictions

Additional execution rules:
- Treat LLM outputs as **plans with assumptions**, not blindly executable commands.
- Re-check critical preconditions before execution.
- If relevant state has changed, execution should be denied, downgraded, or re-planned.
- High-risk actions must support **natural-language confirmation** before execution.
- Users should confirm intended effects, not review code.
- If Mina selects a capability id that is not in the current authoritative list, runtime must reject it and force re-planning instead of executing, silently remapping, or crashing.
- The first unknown-capability selection should be treated as a recoverable planning error.
- Repeated unknown-capability selections may end the turn gracefully, but they must not execute any fallback hidden capability.

Sandboxed script execution must include **resource budgeting**, including limits on:
- execution time
- execution frequency
- side-effect scale
- persistent registration
- runaway execution

A sandbox is not sufficient without execution budgets and policy restrictions.

---

## Carpet

Mina depends on **Carpet**.

The Carpet GitHub repository is already cloned at `reference/fabric-carpet`; during development, always consult Carpet documentation from that repository.

Required principles:
- prefer Carpet Java APIs / event integrations when available
- only fall back to restricted command adaptation when necessary
- expose Carpet-backed functionality as structured Mina capabilities
- do not expose arbitrary Carpet command passthrough as a normal Mina feature

Do not assume any specific Carpet API in this document.

Reserve room for future unrestricted or highly privileged Carpet/Scarpet experimentation, but:
- keep it isolated
- keep it disabled by default
- do not expose it in Mina’s normal capability set
- treat it as privileged experimental functionality

---

## Do Not Do

- Do not add hardcoded keyword-based business routing.
- Do not let the LLM directly execute raw Carpet commands by default.
- Do not let the LLM directly execute arbitrary Scarpet code as the normal path.
- Do not embed Python into the Fabric mod jar.
- Do not block the Minecraft main thread for model/network work.
- Do not turn Mina into a coding-agent review UI.
- Do not assume a sandbox is sufficient without execution budgets and policy checks.
- Do not execute stale action plans without re-checking critical preconditions.
- Do not optimize for task completion at the cost of companionship and fun.
- Do not make Mina feel like a utility-first server assistant by default.
- Do not overuse tools, retrieval, or scripts when a simple in-world conversational response is better.
- Do not let Mina overwhelm the player with constant intervention.

---

## One Rule to Remember

Build Mina as:

**a natural-language-first companion that makes Minecraft more playful, immersive, and alive through unified capabilities, LLM-driven decisions, structured execution, Carpet-backed integrations, precondition-aware execution, sandboxed script execution, and layered safety controls**

---

## Python Venv

Use the project-local virtual environment at `.venv`.

- Create (once): `python3 -m venv .venv`
- Activate: `source .venv/bin/activate`
- Verify: `python -V && pip -V`
- Deactivate: `deactivate`
