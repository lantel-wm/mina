# AGENTS.md

## Mission

Build **Mina (Minecraft Navigator)** for **Minecraft 1.21.11 + Fabric 0.18.4 + Java 21** as:

**Fabric mod (Java bridge, depends on Carpet) + external Python agent service**

Mina is a **natural-language-first agent runtime**.

---

## Product

Users interact with Mina only through **natural language**.

The LLM should decide when to:
- reply
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
- capability-driven
- results-oriented
- LLM-first in decision making

Mina is **not**:
- a keyword router
- a command macro system
- a coding-agent UI
- a diff-review workflow

Users should care about results, not generated code.

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

---

## One Rule to Remember

Build Mina as:

**a natural-language-first runtime with unified capabilities, LLM-driven decisions, structured execution, Carpet-backed integrations, precondition-aware execution, sandboxed script execution, and layered safety controls**

---

## Python Venv

Use the project-local virtual environment at `.venv`.

- Create (once): `python3 -m venv .venv`
- Activate: `source .venv/bin/activate`
- Verify: `python -V && pip -V`
- Deactivate: `deactivate`
