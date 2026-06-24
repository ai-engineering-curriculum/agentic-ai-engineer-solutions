# mod-205-evaluation-observability — Solutions

Reference solutions for the Evaluation & Observability Instrumentation module. Each
exercise folder contains an annotated, runnable walkthrough: the approach, a
reference implementation with offline fallbacks (no API key or platform account
required to run), how it meets the acceptance criteria, common pitfalls, and a
verification recipe.

## Exercises

- [exercise-01 — Trajectory Eval Implementation](exercise-01-trajectory-eval-implementation/README.md)
  — capture an agent trajectory inside the loop and score outcome vs. process
  (tool-call correctness, in-order-subset order, efficiency, reference-free
  invariants).
- [exercise-02 — OpenTelemetry Tracing Wire-Up](exercise-02-otel-tracing-wireup/README.md)
  — instrument model and tool calls as nested GenAI spans and export to any OTLP
  backend (Langfuse / Phoenix / LangSmith), with error status and sampling.
- [exercise-03 — LLM-as-Judge Scoring](exercise-03-llm-judge-scoring/README.md)
  — build a rubric judge, calibrate it against human labels with exact agreement and
  Cohen's kappa, and probe for verbosity bias.
- [exercise-04 — Agent Regression Suite](exercise-04-agent-regression-suite/README.md)
  — compose the scorers over a versioned dataset with k-run aggregation and a CI gate
  that blocks a regression on a non-zero exit.

## How these fit together

The four exercises build one pipeline: exercise-01 produces the trajectory and the
scorers, exercise-02 captures the same run as a portable trace, exercise-03 adds a
trustworthy judge for fuzzy quality, and exercise-04 wires all of it into a
dataset-backed gate that runs in CI. Read them in order — each later solution reuses
the earlier ones.
