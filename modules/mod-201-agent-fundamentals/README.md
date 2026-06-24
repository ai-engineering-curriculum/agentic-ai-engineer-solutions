# mod-201-agent-fundamentals — Solutions

Reference solutions for the Agent Fundamentals module. Each exercise folder holds
an annotated, runnable walkthrough: the approach, a reference implementation,
how it meets the acceptance criteria, common pitfalls, and verification steps.

## Index

| # | Exercise | Solution |
|---|----------|----------|
| 01 | ReAct Loop From Scratch | [exercise-01-react-loop-from-scratch](exercise-01-react-loop-from-scratch/README.md) |
| 02 | Function-Calling Tools | [exercise-02-function-calling-tools](exercise-02-function-calling-tools/README.md) |
| 03 | Coding Agent: Read / Write / Execute | [exercise-03-coding-agent-read-write-execute](exercise-03-coding-agent-read-write-execute/README.md) |
| 04 | Planning Agent | [exercise-04-planning-agent](exercise-04-planning-agent/README.md) |

## How the solutions build on each other

- **01** hand-rolls the reason-act loop with string parsing, a stop sequence, a
  step budget, and a token guard — the smallest non-trivial agent.
- **02** replaces string parsing with native function calling, generating JSON
  Schema from type hints and adding parallel tool calls plus a real I/O tool.
- **03** points the Exercise 02 loop at a sandboxed workspace with `read_file`,
  `write_file`, and `run_shell`, layering path scoping, read-before-write, and
  four termination budgets.
- **04** contrasts reactive ReAct with a planner-executor DAG (static and
  replanning) and measures all three head-to-head.

Each reference uses a mock or scripted LLM so the code runs and tests pass
offline; every solution documents the one-adapter swap to a live provider.
