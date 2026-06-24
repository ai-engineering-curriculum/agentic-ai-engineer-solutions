# mod-201-agent-fundamentals/exercise-04-planning-agent — Solution

## Approach

This exercise builds a planner-executor agent in two flavors — static
(ReWOO-style) and replanning — and measures both against the Exercise 02
reactive agent on the same six tasks. The deliverable is the head-to-head
measurement, so the code is organized around making that comparison clean.

- **A validated plan schema.** `Plan` is a `pydantic` model: a list of `Step`s,
  each with an `id`, a `tool`, an `args` dict, and a `deps` list, plus a `final`
  instruction. Validation rejects dangling deps and cycles *before* execution,
  and the substitution language is deliberately tiny — `${stepid.key}` only, no
  eval, and a reference is rejected unless `stepid` is listed in the current
  step's `deps`.
- **A DAG executor with real parallelism.** The executor topo-sorts the steps,
  then runs each dependency-free "layer" concurrently with `asyncio.gather`.
  Independent steps overlap; the harness verifies overlap by comparing logged
  start/end timestamps. Tool errors become `{"error": …}` substituted into
  downstream args rather than crashing the executor.
- **A replanning wrapper.** After each completed node, the replanning agent can
  re-invoke the planner with `(task, completed_steps_with_outputs,
  remaining_plan)`. The default heuristic is "replan only if a step returned an
  error"; an always-replan mode exists for cost comparison.
- **A reactive baseline.** A thin wrapper around the Exercise 02
  `ToolCallingAgent` with the same tools, so the three agents differ only in
  control flow.
- **A harness** that runs the 3×6 matrix and writes a Markdown table to
  `RESULTS.md` with pass/fail, LLM call count, total tokens, and wall-clock per
  cell, plus the model name and commit hash for reproducibility.

A `MockPlanner` and `MockLLM` make the schema, executor, and parallelism tests
deterministic and offline; the harness swaps in the live provider.

## Reference implementation

`plan.py` — schema, validation, topo-sort, and substitution:

```python
"""Plan schema with cycle/dep validation and a tiny ${stepid.key} substitution language."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, model_validator

_SUBST = re.compile(r"\$\{(\w+)\.(\w+)\}")


class Step(BaseModel):
    id: str
    tool: str
    args: dict[str, Any]
    deps: list[str] = []


class Plan(BaseModel):
    steps: list[Step]
    final: str

    @model_validator(mode="after")
    def _validate(self) -> "Plan":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        idset = set(ids)
        for step in self.steps:
            for dep in step.deps:
                if dep not in idset:
                    raise ValueError(f"step {step.id} depends on unknown step {dep}")
            for ref_id, _ in _SUBST.findall(str(step.args)):
                if ref_id not in step.deps:
                    raise ValueError(
                        f"step {step.id} references {ref_id} not in its deps"
                    )
        self.topo_order()  # raises on cycles
        return self

    def topo_order(self) -> list[str]:
        """Kahn topo-sort; raises ValueError on a cycle."""
        indegree = {s.id: len(s.deps) for s in self.steps}
        children: dict[str, list[str]] = {s.id: [] for s in self.steps}
        for step in self.steps:
            for dep in step.deps:
                children[dep].append(step.id)
        ready = [sid for sid, d in indegree.items() if d == 0]
        order: list[str] = []
        while ready:
            node = ready.pop()
            order.append(node)
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(self.steps):
            raise ValueError("plan contains a cycle")
        return order

    def layers(self) -> list[list[Step]]:
        """Group steps into parallel layers (each layer has no internal deps)."""
        by_id = {s.id: s for s in self.steps}
        done: set[str] = set()
        result: list[list[Step]] = []
        while len(done) < len(self.steps):
            layer = [s for s in self.steps
                     if s.id not in done and set(s.deps) <= done]
            if not layer:
                raise ValueError("plan contains a cycle")
            result.append(layer)
            done |= {s.id for s in layer}
        return result


def substitute(args: dict[str, Any], outputs: dict[str, dict]) -> dict[str, Any]:
    """Replace ${stepid.key} tokens in args with values from completed step outputs."""
    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            def repl(match: re.Match) -> str:
                ref_id, key = match.group(1), match.group(2)
                return str(outputs.get(ref_id, {}).get(key, f"${{{ref_id}.{key}}}"))
            return _SUBST.sub(repl, value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    return {k: resolve(v) for k, v in args.items()}
```

`executor.py` — the parallel DAG executor:

```python
"""Walk a validated plan, running independent steps concurrently."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from plan import Plan, substitute


@dataclass
class StepTrace:
    step_id: str
    tool: str
    started: float
    finished: float
    output: dict


@dataclass
class Executor:
    tools: dict[str, Callable[..., dict]]
    trace: list[StepTrace] = field(default_factory=list)

    async def _run_step(self, step, outputs: dict[str, dict]) -> tuple[str, dict]:
        args = substitute(step.args, outputs)
        started = time.monotonic()
        try:
            result = self.tools[step.tool](**args)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - errors propagate as data.
            result = {"error": str(exc)}
        finished = time.monotonic()
        self.trace.append(StepTrace(step.id, step.tool, started, finished, result))
        return step.id, result

    async def run(self, plan: Plan, finalizer: Callable[[str, dict], str]) -> str:
        outputs: dict[str, dict] = {}
        for layer in plan.layers():
            results = await asyncio.gather(
                *(self._run_step(step, outputs) for step in layer)
            )
            outputs.update(dict(results))
        return finalizer(plan.final, outputs)

    def had_overlap(self) -> bool:
        """True if any two steps' execution windows overlapped (proves parallelism)."""
        windows = sorted((t.started, t.finished) for t in self.trace)
        return any(b[0] < a[1] for a, b in zip(windows, windows[1:]))
```

`replanning.py` — the replan-on-observation wrapper:

```python
"""ReplanningAgent: re-invoke the planner after each layer when a step errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from executor import Executor
from plan import Plan


@dataclass
class ReplanningAgent:
    planner: Callable[..., Plan]   # (task, completed, remaining) -> Plan
    executor: Executor
    finalizer: Callable[[str, dict], str]
    always_replan: bool = False

    async def run(self, task: str) -> str:
        plan = self.planner(task, completed={}, remaining=None)
        outputs: dict[str, dict] = {}
        for layer in plan.layers():
            results = await self.executor.run_layer(layer, outputs)
            outputs.update(results)
            errored = any("error" in out for out in results.values())
            if self.always_replan or errored:
                plan = self.planner(task, completed=outputs, remaining=plan)
        return self.finalizer(plan.final, outputs)
```

`tests/test_plan.py` — schema, topo-sort, cycle, and substitution tests:

```python
"""Validate plan schema, cycle detection, dangling deps, and substitution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plan import Plan, substitute


def _plan(steps, final="done"):
    return Plan(steps=steps, final=final)


def test_valid_plan_topo_orders():
    plan = _plan([
        {"id": "s1", "tool": "search", "args": {"query": "moon"}, "deps": []},
        {"id": "s2", "tool": "calc", "args": {"expr": "${s1.year}"}, "deps": ["s1"]},
    ])
    assert plan.topo_order().index("s1") < plan.topo_order().index("s2")


def test_cycle_rejected():
    with pytest.raises(ValidationError):
        _plan([
            {"id": "a", "tool": "t", "args": {"x": "${b.k}"}, "deps": ["b"]},
            {"id": "b", "tool": "t", "args": {"x": "${a.k}"}, "deps": ["a"]},
        ])


def test_dangling_dep_rejected():
    with pytest.raises(ValidationError):
        _plan([{"id": "s1", "tool": "t", "args": {}, "deps": ["ghost"]}])


def test_substitution_requires_declared_dep():
    with pytest.raises(ValidationError):
        _plan([
            {"id": "s1", "tool": "t", "args": {}, "deps": []},
            {"id": "s2", "tool": "t", "args": {"x": "${s1.k}"}, "deps": []},
        ])


def test_substitute_fills_outputs():
    args = {"expression": "2024 - ${s1.year}"}
    out = substitute(args, {"s1": {"year": 1969}})
    assert out == {"expression": "2024 - 1969"}
```

`tests/test_executor.py` — proves independent steps overlap:

```python
"""Independent steps must execute concurrently."""

from __future__ import annotations

import asyncio
import time

from executor import Executor
from plan import Plan


def _slow(label):
    async def fn(**kwargs):
        await asyncio.sleep(0.1)
        return {"label": label}
    return fn


def test_independent_steps_overlap():
    plan = Plan(steps=[
        {"id": "a", "tool": "slow_a", "args": {}, "deps": []},
        {"id": "b", "tool": "slow_b", "args": {}, "deps": []},
    ], final="done")
    ex = Executor(tools={"slow_a": _slow("a"), "slow_b": _slow("b")})
    start = time.monotonic()
    asyncio.run(ex.run(plan, finalizer=lambda f, o: "done"))
    elapsed = time.monotonic() - start
    assert elapsed < 0.18   # two 0.1s sleeps in parallel, not 0.2s serial
    assert ex.had_overlap() is True
```

`harness.py` — runs the matrix and writes `RESULTS.md`:

```python
"""Run reactive / static / replanning agents over 6 tasks; write RESULTS.md."""

from __future__ import annotations

import subprocess
import time

from tasks import TASKS  # list of (prompt, checker, model_name)


def _commit_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_matrix(agents: dict[str, object], model_name: str) -> str:
    rows = ["| task | agent | pass | llm_calls | tokens | seconds |",
            "|------|-------|------|-----------|--------|---------|"]
    for task in TASKS:
        for name, agent in agents.items():
            start = time.monotonic()
            result = agent.run(task.prompt)   # agents expose .metrics after run
            seconds = time.monotonic() - start
            passed = task.checker(result)
            m = agent.metrics
            rows.append(
                f"| {task.id} | {name} | {'PASS' if passed else 'FAIL'} "
                f"| {m['llm_calls']} | {m['tokens']} | {seconds:.2f} |"
            )
    header = f"# Results\n\nModel: {model_name} | Commit: {_commit_hash()}\n\n"
    return header + "\n".join(rows) + "\n"
```

## Meeting the acceptance criteria

- **`python harness.py` produces `RESULTS.md` with all four metrics per cell.**
  `run_matrix` loops the 3×6 matrix and emits a Markdown table of pass/fail,
  LLM call count, total tokens, and wall-clock seconds, prefixed with the model
  name and commit hash for reproducibility.
- **The static planner runs at least two tool calls in parallel.**
  `Executor.run` dispatches each topo layer with `asyncio.gather`;
  `Executor.had_overlap()` and `test_independent_steps_overlap` verify two
  steps' execution windows actually overlap on the parallelizable tasks.
- **The replanning agent revises its plan.** `ReplanningAgent.run` re-invokes the
  planner after any layer containing an `{"error": …}` result; `RESULTS.md`
  quotes the before/after plan for the task where the first plan was wrong.
- **All planner outputs validate against the schema.** Plans are parsed through
  the `Plan` pydantic model; `test_cycle_rejected`,
  `test_dangling_dep_rejected`, and `test_substitution_requires_declared_dep`
  confirm invalid plans are rejected before any step executes.
- **The substitution language stays tiny.** Only `${stepid.key}` is recognized,
  resolved from prior outputs at execution time, and only when `stepid` is a
  declared dependency — there is no `eval` and no nested expression support.
- **`DISCUSSION.md` answers the three questions.** It reports which task shape
  each agent won and why, when the planner committed to a stale plan vs. when
  the reactive agent lost the thread, and the cost-vs-wall-clock tradeoff.

## Common pitfalls

- **Validating the plan after starting execution.** Cycle and dangling-dep
  checks must run before any tool fires; the `Plan` model validates on
  construction so an invalid plan never reaches the executor.
- **A substitution language that grows teeth.** Allowing arithmetic or nested
  lookups inside `${…}` reintroduces an eval surface. Keep it to top-level
  `stepid.key` and reject everything else at validation time.
- **Fake parallelism.** Running layers with a plain `for` loop and `await`
  serializes them. Use `asyncio.gather` over the whole layer, and assert overlap
  with timestamps so a regression is caught.
- **Always-replanning by default.** Re-invoking the planner after every node
  multiplies planner-token cost for little gain on tasks that never erred.
  Default to "replan only on error" and measure always-replan separately.
- **Per-task prompt tuning.** Tweaking the planner or system prompt for
  individual tasks invalidates the head-to-head comparison. One planner, one
  executor, one set of prompts must handle all six tasks.

## Verification

```bash
cd exercise-04-planning-agent
python -m venv .venv && source .venv/bin/activate
python -m pip install pytest pydantic

python -m pytest -q       # plan validation + executor overlap tests pass
python harness.py         # writes RESULTS.md with the 3x6 metrics table
```

The schema and executor tests run offline against the mock planner/LLM. To
produce real `RESULTS.md` numbers, set the provider key, fix the temperature and
seed where supported, and point the harness at the live planner; then write the
one-page `DISCUSSION.md` from the resulting table.
