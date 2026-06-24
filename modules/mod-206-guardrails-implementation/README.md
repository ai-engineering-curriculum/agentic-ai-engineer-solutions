# mod-206-guardrails-implementation: Guardrails & Safety Implementation — Solutions

Reference solutions for the four guardrails exercises. Each builds one control
in runnable Python, then wires it into a single enforcement surface the agent
cannot route around. Guardrails are code that runs whether or not the model
cooperates — every solution here is designed to hold against a fully
adversarial model.

## Exercises

- [exercise-01-io-moderation-guardrails](exercise-01-io-moderation-guardrails/README.md)
  — one `GuardrailPolicy` object guarding input, output, and tool calls;
  hand-rolled rails plus framework parity; fail-closed on the security path.
- [exercise-02-prompt-injection-defenses](exercise-02-prompt-injection-defenses/README.md)
  — provenance tagging, content isolation (spotlighting), an injection detector,
  and least-privilege containment that holds when detection misses.
- [exercise-03-tool-permission-enforcement](exercise-03-tool-permission-enforcement/README.md)
  — role→policy mapping, argument-level allowlists, default-deny, and a real
  `subprocess` sandbox for the dangerous tool.
- [exercise-04-human-approval-checkpoints](exercise-04-human-approval-checkpoints/README.md)
  — argument-aware risk classification, full-concrete-action prompts,
  fail-closed timeouts, replan-on-denial, and an append-only audit trail.

## Running

Each exercise directory's `README.md` contains the full annotated reference
implementation and a `Verification` section with the exact command. The demos
run offline (stubbed model and moderation backends), so no API key or network is
required to reproduce the acceptance-criteria output.
