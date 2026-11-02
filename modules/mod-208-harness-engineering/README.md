# mod-208-harness-engineering — Solutions

Reference solutions for *Harness Engineering*. Each exercise folder holds an
annotated walkthrough: approach, reference implementation, how it meets the
acceptance criteria, common pitfalls, and verification steps. Exercises 02 and
03 are runnable and fully tested offline (no API key needed); live runs against
Claude Code or Codex CLI are opt-in. Building a harness from scratch continues
in [mod-209](../mod-209-building-custom-harnesses/README.md).

## Exercises

- [exercise-01-harness-a-repository](exercise-01-harness-a-repository/README.md)
  — A worked harness for a small Python CLI: a map-style `AGENTS.md`, a skill, a
  permission policy for Claude Code and Codex, a single `make check`, and a
  before/after results template.
- [exercise-02-guardrail-hooks](exercise-02-guardrail-hooks/README.md)
  — Three Claude Code hooks (pre-tool guard, post-edit formatter, stop gate) with
  subprocess tests that pipe real hook events to each script.
- [exercise-03-harness-eval](exercise-03-harness-eval/README.md)
  — An eval runner that compares two harness configurations across isolated,
  repeated trials, with JSONL results and a bootstrap interval on the
  pass-rate difference.
