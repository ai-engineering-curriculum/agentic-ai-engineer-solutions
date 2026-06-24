# mod-207-productionizing-agents — Solutions

Reference solutions for *Productionizing Agents*. Each exercise folder holds an
annotated, runnable walkthrough: approach, reference implementation, how it meets
the acceptance criteria, common pitfalls, and verification steps. Every solution
runs offline by default (deterministic fakes) and swaps to live providers via an
environment flag.

## Exercises

- [exercise-01-agent-api-deployment](exercise-01-agent-api-deployment/README.md)
  — FastAPI agent service: synchronous, streaming (SSE), and background-job
  endpoints, with timeouts, cancellation, a shared concurrency cap, a health
  check, and a non-root container.
- [exercise-02-durable-execution-temporal](exercise-02-durable-execution-temporal/README.md)
  — A multi-step run as a Temporal workflow: deterministic orchestration,
  idempotent activities, crash/replay survival, and a retry policy.
- [exercise-03-caching-and-routing](exercise-03-caching-and-routing/README.md)
  — Prompt caching on the stable prefix plus a model router with an escalation
  path, measured against a baseline (cost, latency, pass rate).
- [exercise-04-hitl-with-persistence](exercise-04-hitl-with-persistence/README.md)
  — A LangGraph human-in-the-loop gate with a database-backed checkpointer:
  checkpoint → interrupt → resume across processes, with an idempotent gated
  action.
