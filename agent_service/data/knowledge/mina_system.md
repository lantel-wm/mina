# Mina System Notes

Mina is a natural-language-first Minecraft server agent runtime.

- Users should talk to Mina in natural language.
- Mina should not rely on keyword routing for business intent.
- The Python service owns agent orchestration, memory, retrieval, and model calls.
- The Fabric mod is a bridge and final execution guard, not the agent brain.
- World-changing actions must return to Minecraft's controlled server execution context.
- High-risk actions require natural-language confirmation.
- Experimental privileged capabilities stay hidden by default.
