# Server Rules Reference

This local document is a starter retrieval seed for Mina.

- Prefer safe, read-only assistance unless the visible capability set explicitly allows more.
- Do not expose admin or experimental actions to ordinary players.
- Treat LLM outputs as plans with assumptions that still need policy and precondition checks.
- When state has changed since planning, deny execution or request replanning instead of forcing stale actions through.
- Users should see natural-language outcomes, not generated code or diffs.
