# mod-205-evaluation-observability/exercise-01 — Solution

## Approach

The exercise asks us to grade an agent on two independent axes — the **outcome**
(was the final answer right?) and the **trajectory** (did it take a sane path to
get there?). The key design decision is to keep those two scores fully separate so
that a right answer reached through a wrong tool path still shows up as a failure.

The reference implementation is built in four layers:

1. **Capture inside the loop.** A tiny instrumented agent loop appends a structured
   `Step` on every iteration — reasoning turns, tool calls (name plus args), tool
   results, and the final answer — and emits one `Trajectory` per task. Nothing is
   reconstructed from logs after the fact; the loop is the source of truth.
2. **Outcome eval.** A deterministic final-answer check (normalized exact match or
   a substring assertion) compared against a reference for each task.
3. **Trajectory eval.** Three process checks: tool-call **correctness** (right tool,
   valid args), tool-call **order** via **in-order subset** matching (every expected
   call appears in the right relative order, benign extra steps allowed), and an
   **efficiency** metric (step count plus a flag for any identical call repeated
   three or more times).
4. **Reference-free invariants.** Cheap, scalable checks that need no reference
   trajectory: no malformed args, stopped within a step budget, and every cited
   source actually appears in a tool result (catches hallucinated citations).

To keep the solution runnable offline with no API key, the agent uses a small
**scripted planner**: per task, a deterministic policy decides which tools to call.
This is the same `Trajectory` data structure a real model-driven loop would emit, so
swapping in a live LLM is a drop-in change at the `_plan` boundary — the evaluator
code is identical either way. The chapter's point is that the *evaluator* is the
deliverable, not the agent, so a deterministic agent keeps the focus there and makes
the suite reproducible.

## Reference implementation

Save as `trajectory_eval.py` and run `python trajectory_eval.py`. Pure standard
library; no API key required.

```python
"""Trajectory + outcome evaluation for a tool-calling agent (offline, deterministic).

Layers:
  - capture a structured Trajectory inside the agent loop
  - outcome eval (final-answer match)
  - trajectory eval (tool correctness, in-order-subset order, efficiency)
  - reference-free invariants (arg validity, step budget, citation grounding)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Trajectory data model (matches the chapter's starter)
# ---------------------------------------------------------------------------


@dataclass
class Step:
    kind: str                       # "reason" | "tool_call" | "tool_result" | "answer"
    tool: str | None = None
    args: dict = field(default_factory=dict)
    output: str | None = None


@dataclass
class Trajectory:
    task: str
    steps: list[Step] = field(default_factory=list)
    final: str | None = None


def tool_calls(t: Trajectory) -> list[str]:
    return [s.tool for s in t.steps if s.kind == "tool_call" and s.tool is not None]


# ---------------------------------------------------------------------------
# Tools (deterministic stubs). Each returns a string; SOURCE: tokens let the
# citation invariant verify grounding.
# ---------------------------------------------------------------------------

_FACTS = {
    "capital of france": "SOURCE:wiki Paris is the capital of France.",
    "tallest mountain": "SOURCE:wiki Mount Everest is the tallest mountain.",
    "speed of light": "SOURCE:phys The speed of light is 299792458 m/s.",
}


def tool_search(query: str) -> str:
    key = query.strip().lower()
    for k, v in _FACTS.items():
        if k in key:
            return v
    return "SOURCE:wiki no result"


def tool_calculator(expr: str) -> str:
    # Tightly restricted arithmetic eval — digits and operators only.
    if not re.fullmatch(r"[0-9+\-*/(). ]+", expr or ""):
        raise ValueError(f"unsafe calculator expression: {expr!r}")
    return str(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - input is regex-gated


def tool_lookup(entity: str) -> str:
    table = {"euro": "SOURCE:fx EUR is the currency of France."}
    return table.get((entity or "").strip().lower(), "SOURCE:fx unknown entity")


TOOLS: dict[str, Callable[..., str]] = {
    "search": tool_search,
    "calculator": tool_calculator,
    "lookup": tool_lookup,
}

# Argument schema used by the malformed-argument invariant.
TOOL_ARG_KEYS = {"search": "query", "calculator": "expr", "lookup": "entity"}


# ---------------------------------------------------------------------------
# Instrumented agent loop. The planner is deterministic so the suite is
# reproducible; replace `_plan` with a model call for a live agent.
# ---------------------------------------------------------------------------


def _plan(task: dict) -> list[dict]:
    """Return the ordered tool calls a real model would emit for this task.

    A task may carry a `force` directive so we can deliberately break a run
    (wrong tool / forced repeat) and prove the evaluator catches it.
    """
    if "force" in task:
        return task["force"]
    return task["plan"]


def run_agent(task: dict, step_budget: int = 8) -> Trajectory:
    """Execute the scripted plan, capturing a Step on every iteration."""
    traj = Trajectory(task=task["question"])
    traj.steps.append(Step(kind="reason", output=f"considering: {task['question']}"))

    results: list[str] = []
    for call in _plan(task):
        if len(tool_calls(traj)) >= step_budget:
            break  # never exceed the budget; the invariant will still flag it
        name, args = call["tool"], call.get("args", {})
        traj.steps.append(Step(kind="tool_call", tool=name, args=args))
        try:
            output = TOOLS[name](**args)
        except Exception as exc:  # capture tool failure as a result, keep going
            output = f"ERROR: {exc}"
        traj.steps.append(Step(kind="tool_result", tool=name, output=output))
        results.append(output)

    traj.final = task.get("answer_template", "{r}").format(r=results[-1] if results else "")
    traj.steps.append(Step(kind="answer", output=traj.final))
    return traj


# ---------------------------------------------------------------------------
# Match modes
# ---------------------------------------------------------------------------


def in_order_subset(actual: list[str], expected: list[str]) -> bool:
    """Every expected tool appears in the right relative order; extras allowed."""
    it = iter(actual)
    return all(tool in it for tool in expected)


def set_match(actual: list[str], expected: list[str]) -> bool:
    return set(actual) == set(expected)


# ---------------------------------------------------------------------------
# Reference-free invariants
# ---------------------------------------------------------------------------


def _cited_sources(final: str | None) -> set[str]:
    return set(re.findall(r"SOURCE:(\w+)", final or ""))


def invariants(t: Trajectory, step_budget: int) -> dict[str, bool]:
    calls = [s for s in t.steps if s.kind == "tool_call"]

    args_ok = all(
        TOOL_ARG_KEYS.get(s.tool) in s.args and s.args[TOOL_ARG_KEYS[s.tool]] not in (None, "")
        for s in calls
    )

    within_budget = len(calls) <= step_budget

    available: set[str] = set()
    for s in t.steps:
        if s.kind == "tool_result":
            available |= _cited_sources(s.output)
    citations_grounded = _cited_sources(t.final).issubset(available)

    return {
        "args_ok": args_ok,
        "within_budget": within_budget,
        "citations_grounded": citations_grounded,
    }


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def efficiency(t: Trajectory) -> dict:
    calls = tool_calls(t)
    seen: dict[tuple, int] = {}
    repeated_3x = False
    for s in t.steps:
        if s.kind == "tool_call":
            sig = (s.tool, tuple(sorted(s.args.items())))
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] >= 3:
                repeated_3x = True
    return {"n_tool_calls": len(calls), "repeated_3x": repeated_3x}


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def outcome_pass(t: Trajectory, reference: str) -> bool:
    return _normalize(reference) in _normalize(t.final)


# ---------------------------------------------------------------------------
# Top-level evaluator: outcome and trajectory scored SEPARATELY
# ---------------------------------------------------------------------------


def eval_trajectory(t: Trajectory, expected_tools: list[str], step_budget: int) -> dict:
    calls = tool_calls(t)
    correctness = set(expected_tools).issubset(set(calls))  # right tools present
    order_ok = in_order_subset(calls, expected_tools)
    eff = efficiency(t)
    inv = invariants(t, step_budget)
    trajectory_ok = correctness and order_ok and not eff["repeated_3x"] and all(inv.values())
    return {
        "tool_correctness": correctness,
        "order_ok": order_ok,
        "efficiency": eff,
        "invariants": inv,
        "trajectory_ok": trajectory_ok,
    }


# ---------------------------------------------------------------------------
# Dataset: happy paths plus two deliberately broken cases
# ---------------------------------------------------------------------------

DATASET = [
    {
        "question": "What is the capital of France?",
        "plan": [{"tool": "search", "args": {"query": "capital of france"}}],
        "answer_template": "Answer: {r}",
        "reference": "Paris is the capital of France",
        "expected_tools": ["search"],
    },
    {
        "question": "What is 21 * 2?",
        "plan": [{"tool": "calculator", "args": {"expr": "21 * 2"}}],
        "answer_template": "Answer: {r}",
        "reference": "42",
        "expected_tools": ["calculator"],
    },
    {
        "question": "Currency of France, then confirm by search?",
        "plan": [
            {"tool": "lookup", "args": {"entity": "euro"}},
            {"tool": "search", "args": {"query": "capital of france"}},
        ],
        "answer_template": "Answer: {r}",
        "reference": "Paris is the capital of France",
        "expected_tools": ["lookup", "search"],
    },
    {
        "question": "Tallest mountain (extra benign step allowed)?",
        "plan": [
            {"tool": "search", "args": {"query": "weather"}},      # benign extra
            {"tool": "search", "args": {"query": "tallest mountain"}},
        ],
        "answer_template": "Answer: {r}",
        "reference": "Mount Everest is the tallest mountain",
        "expected_tools": ["search"],
    },
    {
        "question": "Speed of light?",
        "plan": [{"tool": "search", "args": {"query": "speed of light"}}],
        "answer_template": "Answer: {r}",
        "reference": "299792458",
        "expected_tools": ["search"],
    },
    {
        # BROKEN: right answer, WRONG tool path (calculator for a lookup task).
        "question": "Capital of France (wrong-tool path)?",
        "force": [{"tool": "calculator", "args": {"expr": "1+1"}}],
        "answer_template": "Answer: SOURCE:wiki Paris is the capital of France.",
        "reference": "Paris is the capital of France",
        "expected_tools": ["search"],
    },
    {
        # BROKEN: forced 3x identical call -> efficiency flag trips.
        "question": "Capital of France (looping)?",
        "force": [
            {"tool": "search", "args": {"query": "capital of france"}},
            {"tool": "search", "args": {"query": "capital of france"}},
            {"tool": "search", "args": {"query": "capital of france"}},
        ],
        "answer_template": "Answer: {r}",
        "reference": "Paris is the capital of France",
        "expected_tools": ["search"],
    },
]


def run_report(dataset: list[dict], step_budget: int = 8) -> dict:
    rows = []
    for task in dataset:
        traj = run_agent(task, step_budget=step_budget)
        out = outcome_pass(traj, task["reference"])
        tev = eval_trajectory(traj, task["expected_tools"], step_budget)
        rows.append({"task": task["question"], "outcome": out, **tev})

    n = len(rows)
    agg = {
        "outcome_pass_rate": sum(r["outcome"] for r in rows) / n,
        "trajectory_match_rate": sum(r["trajectory_ok"] for r in rows) / n,
        "mean_steps": sum(r["efficiency"]["n_tool_calls"] for r in rows) / n,
    }

    print("=== per-task ===")
    for r in rows:
        print(
            f"- {r['task'][:42]:42}  outcome={'PASS' if r['outcome'] else 'FAIL'}"
            f"  traj={'PASS' if r['trajectory_ok'] else 'FAIL'}"
            f"  steps={r['efficiency']['n_tool_calls']}"
            f"  repeat3x={r['efficiency']['repeated_3x']}"
        )
    print("\n=== aggregate ===")
    for k, v in agg.items():
        print(f"- {k}: {v:.3f}")
    return {"rows": rows, "aggregate": agg}


if __name__ == "__main__":
    run_report(DATASET)
```

Expected output (abridged): the lookup/calculator happy-path tasks and the
benign-extra-step task pass both axes; the **wrong-tool** task shows
`outcome=PASS traj=FAIL` (the latent bug surfaces), and the **looping** task shows
`repeat3x=True` with `traj=FAIL`.

## Meeting the acceptance criteria

- **Structured `Trajectory` captured inside the loop** — `run_agent` appends a
  `Step` on every iteration and returns one `Trajectory`; nothing is reconstructed
  from logs.
- **Outcome and trajectory scored separately** — `outcome_pass` and
  `eval_trajectory` are independent; the per-task report prints both columns.
- **In-order-subset passes a valid longer path, fails a skip/reorder** — the
  "tallest mountain" task has a benign extra `search` and still passes order;
  swap the expected order and `in_order_subset` returns `False`.
- **A deliberately broken task is caught even when the answer is right** — the
  wrong-tool task passes outcome but fails `tool_correctness`; the looping task
  trips `repeated_3x`.
- **Per-task and aggregate metrics** — `run_report` prints both, including
  outcome pass rate, trajectory match rate, and mean steps.

## Common pitfalls

- **Collapsing the two scores into one.** If you `and` outcome with trajectory into
  a single "pass," you lose the exact signal the chapter is about — a right answer
  via a wrong path. Keep them in separate columns.
- **Over-strict order matching.** Using exact-sequence equality instead of in-order
  subset produces constant false failures the moment the agent takes a valid longer
  path. In-order subset is the pragmatic default.
- **Citation invariant that only checks the final string.** Grounding means a cited
  source actually appeared in a **tool result**, not merely that the string looks
  like a citation. Build the `available` set from `tool_result` steps.
- **Counting reasoning steps as tool calls** in the efficiency metric. The step
  budget and repeat detector should count `tool_call` steps only, or every metric
  is inflated and the budget never triggers.
- **An unsandboxed `calculator`.** A raw `eval` on model-supplied text is a code
  execution hole. Gate the expression with a strict regex before evaluating.

## Verification

```bash
python trajectory_eval.py
```

Confirm in the output that (a) the wrong-tool task prints `outcome=PASS traj=FAIL`,
(b) the looping task prints `repeat3x=True`, and (c) the aggregate block shows a
trajectory match rate below the outcome pass rate — proof the trajectory layer is
catching bugs the outcome layer misses. To prove order sensitivity, temporarily set
the multi-tool task's `expected_tools` to `["search", "lookup"]` (reversed) and
re-run: `order_ok` flips to `False` while `tool_correctness` stays `True`.
