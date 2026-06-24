# mod-201-agent-fundamentals/exercise-01-react-loop-from-scratch — Solution

## Approach

The exercise asks for a hand-rolled ReAct loop with no agent framework. The
design splits cleanly into four concerns, and keeping them separate is what
makes the loop debuggable:

- **Prompt construction.** A single rendered string holds the system prompt,
  the tool catalog, the format reminder, and the running transcript. The loop
  appends to this string and re-sends it each step. Seeing the full prompt is
  the most valuable debugging artifact, so the agent logs it on demand.
- **Generation with a stop sequence.** Every model call passes
  `stop=["Observation:"]`. The model writes `Thought` / `Action` /
  `Action Input` and then halts exactly where the runtime must take over. This
  is the single highest-leverage line in the whole agent — without it the model
  hallucinates its own observations.
- **Parsing.** Three regexes pull the `Action`, `Action Input`, and
  `Final Answer` out of the chunk the model produced. Parsing is treated as a
  fallible boundary: malformed output becomes an error *observation* fed back to
  the model, never a Python exception that kills the loop.
- **Dispatch and termination.** Tools are a `dict[str, Callable]`; dispatch is a
  lookup. The loop terminates on `Final Answer:` or a `max_steps` budget, and a
  token counter refuses any turn that would exceed 80% of the context window.

The reference code below is provider-agnostic: it defines a tiny `LLM` protocol
and ships a deterministic `ScriptedLLM` so the whole thing runs and tests
offline. Swapping in a real provider is a 10-line adapter that implements
`complete(prompt, stop) -> str`.

## Reference implementation

`react_agent.py` — the loop, the parsers, the budget checks:

```python
"""A from-scratch ReAct agent: no framework, string-parsed Thought/Action/Observation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

logger = logging.getLogger("react")

# Parsers. Kept deliberately narrow; weird input becomes an error observation.
ACTION_RE = re.compile(r"Action:\s*(\w+)")
INPUT_RE = re.compile(r"Action Input:\s*(\{.*?\})", re.DOTALL)
FINAL_RE = re.compile(r"Final Answer:\s*(.+)", re.DOTALL)
THOUGHT_RE = re.compile(r"Thought:\s*(.+)")

# A frontier model's context window; refuse turns past this fraction of it.
CONTEXT_WINDOW_TOKENS = 128_000
CONTEXT_BUDGET_FRACTION = 0.80
DEFAULT_MAX_STEPS = 8


class StepBudgetExceeded(RuntimeError):
    """Raised when the agent runs out of its step budget without a final answer."""


class LLM(Protocol):
    """Minimal provider contract. Real adapters wrap an HTTP/SDK client."""

    def complete(self, prompt: str, stop: list[str]) -> str:
        """Return the next completion, halting before any `stop` substring."""


def count_tokens(text: str) -> int:
    """Approximate token count.

    A real implementation calls `tiktoken` or the provider tokenizer (Chapter 2).
    The 4-chars-per-token heuristic is good enough to enforce a budget and keeps
    the reference dependency-free.
    """
    return max(1, len(text) // 4)


@dataclass
class StepRecord:
    """One iteration of the loop, for the structured log and for replay."""

    step_index: int
    thought: str
    action: str
    action_input: str
    observation: str


@dataclass
class ReActAgent:
    """A string-parsed ReAct agent over a dict of Python callables."""

    llm: LLM
    tools: dict[str, Callable[..., str]]
    system_prompt: str
    max_steps: int = DEFAULT_MAX_STEPS
    trace: list[StepRecord] = field(default_factory=list)

    def _render_tool_catalog(self) -> str:
        lines = []
        for name, fn in self.tools.items():
            doc = (fn.__doc__ or "").strip().splitlines()
            summary = doc[0] if doc else "(no description)"
            lines.append(f"- {name}: {summary}")
        return "\n".join(lines)

    def _build_prompt(self, task: str) -> str:
        return (
            f"{self.system_prompt}\n\n"
            f"You have access to the following tools:\n"
            f"{self._render_tool_catalog()}\n\n"
            "Use this exact format. Each step is one of:\n\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            "Action Input: <JSON arguments>\n\n"
            "...or, when you have the final answer:\n\n"
            "Thought: <your reasoning>\n"
            "Final Answer: <your answer>\n\n"
            "The runtime appends an Observation: line after each Action.\n\n"
            f"Question: {task}\n"
        )

    def _dispatch(self, action: str, raw_args: str) -> str:
        """Run a tool, returning its result or an error string. Never raises."""
        tool = self.tools.get(action)
        if tool is None:
            return f"Error: unknown tool '{action}'. Available: {', '.join(self.tools)}."
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            return f"Error: Action Input was not valid JSON ({exc.msg})."
        if not isinstance(args, dict):
            return "Error: Action Input must be a JSON object of keyword arguments."
        try:
            return str(tool(**args))
        except TypeError as exc:
            return f"Error: bad arguments for '{action}': {exc}."
        except Exception as exc:  # noqa: BLE001 - tool errors are observations.
            return f"Error: tool '{action}' raised {exc!r}."

    def run(self, task: str) -> str:
        """Drive the loop until Final Answer or the step budget is exhausted."""
        prompt = self._build_prompt(task)
        for step_index in range(self.max_steps):
            self._guard_context(prompt)
            chunk = self.llm.complete(prompt, stop=["Observation:"])
            prompt += chunk

            if final := FINAL_RE.search(chunk):
                answer = final.group(1).strip()
                logger.info("step %d | final_answer", step_index)
                return answer

            thought = THOUGHT_RE.search(chunk)
            action = ACTION_RE.search(chunk)
            args = INPUT_RE.search(chunk)
            if not action or not args:
                observation = (
                    "Error: could not parse an Action and Action Input. "
                    "Emit `Action: <name>` then `Action Input: <json>`."
                )
            else:
                observation = self._dispatch(action.group(1), args.group(1))

            record = StepRecord(
                step_index=step_index,
                thought=thought.group(1).strip() if thought else "",
                action=action.group(1) if action else "",
                action_input=args.group(1) if args else "",
                observation=observation,
            )
            self.trace.append(record)
            logger.info(
                "step %d | action=%s | input=%s | obs=%r",
                record.step_index,
                record.action or "(none)",
                record.action_input or "{}",
                observation[:200],
            )
            prompt += f"\nObservation: {observation}\n"

        raise StepBudgetExceeded(f"no final answer within {self.max_steps} steps")

    def _guard_context(self, prompt: str) -> None:
        budget = int(CONTEXT_WINDOW_TOKENS * CONTEXT_BUDGET_FRACTION)
        used = count_tokens(prompt)
        if used > budget:
            raise StepBudgetExceeded(
                f"prompt is {used} tokens, over the {budget}-token budget "
                f"({int(CONTEXT_BUDGET_FRACTION * 100)}% of {CONTEXT_WINDOW_TOKENS})"
            )
```

`tools.py` — the demo tools, with a safe `calc` (no `eval`):

```python
"""Demo tools for the ReAct agent. `calc` uses an AST allowlist, never eval()."""

from __future__ import annotations

import ast
import operator

# Hard-coded "search" so the loop is the focus, not retrieval quality.
_FACTS = {
    "most populous countries 2025": (
        "India ~1.45e9, China ~1.41e9 (UN World Population Prospects 2024)."
    ),
    "tallest mountain": "Mount Everest, 8849 m above sea level.",
}

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def search(query: str) -> str:
    """Return a short summary string for `query` (stubbed lookup)."""
    return _FACTS.get(query.strip().lower(), f"No results for {query!r}.")


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"disallowed expression element: {ast.dump(node)}")


def calc(expression: str) -> str:
    """Evaluate a math expression (numbers, + - * / ** % and parentheses)."""
    tree = ast.parse(expression, mode="eval")
    return str(_eval_node(tree))
```

`demo.py` — the end-to-end run with a scripted LLM so it runs offline:

```python
"""Run the agent on the population-ratio task and print the numbered trace."""

from __future__ import annotations

import logging

from react_agent import ReActAgent
from tools import calc, search


class ScriptedLLM:
    """Deterministic stand-in for a provider. A real adapter calls the API."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)

    def complete(self, prompt: str, stop: list[str]) -> str:
        return self._chunks.pop(0)


SCRIPT = [
    (
        "Thought: I need the top-two populations.\n"
        'Action: search\nAction Input: {"query": "most populous countries 2025"}\n'
    ),
    (
        "Thought: Divide India's population by China's.\n"
        'Action: calc\nAction Input: {"expression": "1.45e9 / 1.41e9"}\n'
    ),
    (
        "Thought: I have enough to answer.\n"
        "Final Answer: About 1.03 - India's population is roughly 1.03x China's.\n"
    ),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    agent = ReActAgent(
        llm=ScriptedLLM(SCRIPT),
        tools={"search": search, "calc": calc},
        system_prompt="You are a research assistant. Reason step by step.",
    )
    answer = agent.run(
        "What is the population of the most populous country, divided by the "
        "population of the second most populous country?"
    )
    print("\n=== TRACE ===")
    for record in agent.trace:
        print(
            f"step {record.step_index} | action={record.action} "
            f"| input={record.action_input} | obs={record.observation!r}"
        )
    print("\n=== FINAL ANSWER ===")
    print(answer)


if __name__ == "__main__":
    main()
```

`tests/test_loop.py` — the three required paths plus the adversarial case:

```python
"""pytest coverage: happy path, unknown tool, budget exhaustion, malformed input."""

from __future__ import annotations

import pytest

from react_agent import ReActAgent, StepBudgetExceeded
from tools import calc, search


class ScriptedLLM:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def complete(self, prompt, stop):
        # If the script runs dry, keep looping to exercise the budget guard.
        return self._chunks.pop(0) if self._chunks else (
            'Thought: retry\nAction: calc\nAction Input: {"expression": "1+1"}\n'
        )


def make_agent(script, **kwargs):
    return ReActAgent(
        llm=ScriptedLLM(script),
        tools={"search": search, "calc": calc},
        system_prompt="Reason step by step.",
        **kwargs,
    )


def test_happy_path_returns_numeric_final_answer():
    agent = make_agent(
        [
            'Thought: add.\nAction: calc\nAction Input: {"expression": "2 + 3"}\n',
            "Thought: done.\nFinal Answer: 5\n",
        ]
    )
    assert agent.run("2 + 3?") == "5"
    assert agent.trace[0].observation == "5.0"


def test_unknown_tool_becomes_error_observation():
    agent = make_agent(
        [
            'Thought: try it.\nAction: nope\nAction Input: {"x": 1}\n',
            "Thought: recover.\nFinal Answer: handled\n",
        ]
    )
    assert agent.run("anything") == "handled"
    assert "unknown tool" in agent.trace[0].observation


def test_step_budget_exhausted_raises():
    agent = make_agent([], max_steps=3)  # script is empty -> loops forever
    with pytest.raises(StepBudgetExceeded):
        agent.run("loop forever")
    assert len(agent.trace) == 3


def test_malformed_json_is_surfaced_not_raised():
    agent = make_agent(
        [
            'Thought: oops.\nAction: search\nAction Input: {"query": not json}\n',
            "Thought: recover.\nFinal Answer: ok\n",
        ]
    )
    assert agent.run("adversarial") == "ok"
    assert "not valid JSON" in agent.trace[0].observation
```

## Meeting the acceptance criteria

- **`python demo.py` prints a final answer and a numbered trace.** `demo.py`
  iterates `agent.trace` after `run()` returns, printing one line per step plus
  the verbatim final answer.
- **`pytest` passes the three required paths.** `test_happy_path_*` (calc →
  numeric answer), `test_unknown_tool_*` (recovers from a bad tool name), and
  `test_step_budget_exhausted_raises` (empty script loops until the budget
  raises `StepBudgetExceeded`).
- **Adversarial malformed input is caught, not crashed.**
  `test_malformed_json_is_surfaced_not_raised` feeds `Action Input: {"query":
  not json}`; `_dispatch` catches `JSONDecodeError` and returns an error
  observation, so the loop continues to a final answer.
- **The structured log is one line per step.** The `logger.info` call inside the
  loop emits `step N | action=… | input=… | obs="…"`, exactly the requested
  shape.
- **The provider call uses `stop=["Observation:"]`.** Every `llm.complete` call
  in `run()` passes that stop list; the model halts before fabricating
  observations.
- **`Final Answer:` is returned verbatim.** `FINAL_RE` captures everything after
  the sentinel and `run()` returns it `.strip()`-ed.
- **Token budget enforced.** `_guard_context` raises before sending any turn
  above 80% of the context window.
- **No agent-framework imports.** The only imports are the standard library plus
  the local modules.

## Common pitfalls

- **Forgetting the stop sequence.** Without `stop=["Observation:"]` the model
  writes its own fake `Observation:` lines and the loop never dispatches a real
  tool. This is the most common first bug.
- **Letting tool errors raise.** A `KeyError` or `JSONDecodeError` that escapes
  `_dispatch` kills the run. Errors must become observations so the model can
  recover — that recovery loop is half the value of ReAct.
- **Greedy or anchored regexes.** `Action Input:\s*(\{.*\})` without `re.DOTALL`
  misses multi-line JSON; anchoring with `^`/`$` breaks on leading whitespace or
  code fences. Test the parsers against ugly input before wiring the loop.
- **Using `eval()` for `calc`.** `eval("__import__('os').system('rm -rf /')")`
  is one model hallucination away. The AST allowlist in `tools.py` only permits
  numeric literals and a fixed operator set.
- **No step budget, or a budget so large it hides bugs.** A confused agent loops
  forever. Keep `max_steps` small (4–8) during development so failures surface
  fast instead of burning tokens.

## Verification

```bash
cd exercise-01-react-loop-from-scratch
python -m venv .venv && source .venv/bin/activate
python -m pip install pytest

python demo.py            # prints the numbered trace and the final answer
python -m pytest -q       # 4 passing tests: happy / unknown-tool / budget / malformed
```

Expected: `demo.py` ends with `About 1.03 ...`; `pytest` reports `4 passed`.
With a real provider, replace `ScriptedLLM` with an adapter whose `complete`
calls the API using `stop=["Observation:"]`, and run `demo.py` against the live
model.
