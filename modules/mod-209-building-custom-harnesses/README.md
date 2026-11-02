# mod-209-building-custom-harnesses — Solutions

Reference solutions for *Building a Custom Agent Harness*. Exercise-01 builds
the core harness package; exercises 02–07 each extend that same package on
their own, listing exactly which exercise-01 files they add or change. Every
solution runs and is fully tested offline with scripted fake models (no API key);
live runs against the Anthropic API, Docker, or an MCP server are opt-in where
the exercise calls for them.

## Exercises

- [exercise-01-build-a-minimal-harness](exercise-01-build-a-minimal-harness/README.md)
  — The agent loop, tool registry with workspace containment, schema
  validation, a permission gate, pre/post-tool hooks, and four stop conditions.
- [exercise-02-coding-tool-suite](exercise-02-coding-tool-suite/README.md)
  — Line-numbered paged reads, exact-match edits with read-before-edit,
  `glob`/`grep` with a ripgrep fast path, local and Docker command runners, and
  git checkpoints on private refs.
- [exercise-03-prompt-assembly-and-model-layer](exercise-03-prompt-assembly-and-model-layer/README.md)
  — Instruction-file discovery with imports, a skills index and `load_skill`,
  cache-aware prompt blocks, jittered retries, streaming events, and parallel
  read-only tool calls.
- [exercise-04-context-and-session-state](exercise-04-context-and-session-state/README.md)
  — Token accounting, tool-result clearing, compaction, a guarded
  `features.json`, and resume from disk.
- [exercise-05-subagents-mcp-and-config](exercise-05-subagents-mcp-and-config/README.md)
  — A `task` tool with budget sharing and a depth limit, an MCP client adapter,
  and validated layered settings for permissions and command hooks.
- [exercise-06-observability-replay-and-shipping](exercise-06-observability-replay-and-shipping/README.md)
  — Redacted JSONL run logs, OpenTelemetry spans, record-and-replay tests, and a
  headless CLI with JSON output and exit codes.
- [exercise-07-trust-layer-veriswarm](exercise-07-trust-layer-veriswarm/README.md)
  — VeriSwarm as a trust layer: Gate decisions as a permission policy with
  caching and per-tool fail modes, result and input filter stages, PII
  tokenization with rehydration at write boundaries, injection scanning, and
  batched content-free behavioral events. Tested offline against a fake client
  and checked against the real `veriswarm` 0.5 SDK's signatures.
