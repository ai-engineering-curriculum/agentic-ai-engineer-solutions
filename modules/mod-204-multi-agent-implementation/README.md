# mod-204-multi-agent-implementation — Solutions index

Reference solutions for the multi-agent implementation module. Each exercise
folder holds a single `README.md` with the approach, an annotated runnable
Python reference, how it meets the acceptance criteria, common pitfalls, and a
verification recipe.

The references share one design: every model call goes through a small
`call_structured(...)` helper that asks Claude for JSON and validates it with
Pydantic. When `ANTHROPIC_API_KEY` is unset, the helper falls back to a
deterministic offline stub so the code runs (and the patterns stay gradeable)
without network access or spend.

| Exercise | Pattern | Reference file |
| --- | --- | --- |
| [exercise-01](exercise-01-orchestrator-worker-build/README.md) | Orchestrator-worker: decompose, concurrent fan-out, synthesize, partial failure | `orchestrator_worker.py` |
| [exercise-02](exercise-02-agent-handoffs/README.md) | Handoffs and routing: front-door router, typed payload, hop limit, visited set | `handoffs.py` |
| [exercise-03](exercise-03-subagent-isolation/README.md) | Sub-agent isolation: fresh context, distilled return, isolated-vs-inline token table | `subagent_isolation.py` |
| [exercise-04](exercise-04-evaluator-optimizer-loop/README.md) | Evaluator-optimizer loop: structured verdict, reachable pass, round cap, no-progress exit | `evaluator_optimizer.py` |

## Running any reference

```bash
pip install anthropic pydantic
export ANTHROPIC_API_KEY="sk-ant-..."   # optional; omit to run the offline stub
python orchestrator_worker.py
```

Each file is a self-contained script with a `main()` demo and an
`if __name__ == "__main__"` guard. The code block in every solution README is
the full file content — copy it into the named file and run it.
