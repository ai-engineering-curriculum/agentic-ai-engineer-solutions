# mod-202-frameworks: Agent Frameworks in Practice — Solutions

Reference solutions for the framework module. All five exercises build the
**same** research-assistant task — compare the two most populous countries in
2024 and report the population ratio — across LangGraph, CrewAI, and AutoGen,
then factor the tools into an MCP server and run a side-by-side bakeoff. Every
build ships a deterministic offline fallback, so the code runs with no provider
key while staying drop-in compatible with a real model.

## Exercises

- [exercise-01 — LangGraph Stateful Agent](exercise-01-langgraph-stateful-agent/README.md):
  typed `AgentState`, `ToolNode`, conditional-edge termination, `MemorySaver`
  checkpointing, and an `interrupt_before` human gate.
- [exercise-02 — CrewAI Role-Based Crew](exercise-02-crewai-role-based-crew/README.md):
  researcher + analyst composed by `Task.context`, run sequentially and
  hierarchically, with the manager's cost delta measured.
- [exercise-03 — AutoGen Multi-Agent](exercise-03-autogen-multi-agent/README.md):
  two-agent chat and a manager-led group chat, strict `TERMINATE` matching,
  tool registration on both sides, and a replayable transcript.
- [exercise-04 — MCP Tool Server](exercise-04-mcp-tool-server/README.md):
  `search`/`calc` behind a `FastMCP` stdio server, called from a raw client and
  a framework host, with an adversarial-input guard.
- [exercise-05 — Framework Tradeoff Bakeoff](exercise-05-framework-tradeoff-bakeoff/README.md):
  one harness over all three builds, a 1–5 rubric on the Chapter 5 axes, and a
  one-page decision memo.

## Shared building blocks

- **Tools.** `search` (fixed corpus) and `calc` (`ast` whitelist, never `eval`)
  are identical across builds so the comparison is apples to apples.
- **Determinism.** Each exercise falls back to a scripted model/crew/conversation
  when no API key is present, so trajectories and metrics are reproducible.
- **Provider-agnostic.** Any LangChain-supported provider works; export the
  relevant key and install the framework package to run against a real model.
