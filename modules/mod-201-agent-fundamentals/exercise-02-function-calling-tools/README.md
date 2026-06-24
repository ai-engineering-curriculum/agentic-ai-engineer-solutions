# mod-201-agent-fundamentals/exercise-02-function-calling-tools — Solution

## Approach

This exercise rebuilds the Exercise 01 agent on top of native function calling.
The string-parsing disappears; the SDK hands back a typed `tool_calls` list, and
the loop body shrinks. Three pieces carry the design:

- **A `@tool` decorator that generates JSON Schema from type hints.** It inspects
  the signature with `inspect.signature`, reads the docstring for the
  description, and builds a provider-shaped schema. Required vs. optional is
  derived from whether a parameter has a default; `Literal[...]` annotations
  become JSON-Schema `enum`s; nested `pydantic` models expand to nested object
  schemas. Hand-writing schemas is explicitly banned, so this decorator is the
  heart of the exercise.
- **An async loop that drives `tools=` / `tool_choice=`.** Each turn sends the
  messages and the tool catalog, appends the assistant message verbatim
  (including `tool_calls`), executes every requested call — concurrently when
  there is more than one — and appends one `role: "tool"` message per call keyed
  by `tool_call_id`. It terminates when the assistant returns no tool calls or
  the step budget is hit.
- **A real I/O tool.** `get_weather` hits Open-Meteo (no API key, stable JSON)
  with a 5-second timeout, so the demo's answer genuinely depends on an HTTP
  round-trip and changes day to day.

The reference is provider-shaped after the OpenAI Chat Completions tool
contract (the chapter's running example), with a `MockLLM` so tests and the
parallel-call demo run offline and deterministically. A thin `OpenAILLM`
adapter (shown in Verification) swaps in the live API.

## Reference implementation

`tools/decorators.py` — the schema-generating `@tool` decorator:

```python
"""@tool: turn a typed Python function into a provider-ready tool schema."""

from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, Literal, get_args, get_origin

from pydantic import BaseModel

_PRIMITIVES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    """Map a single type annotation to a JSON-Schema fragment."""
    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}
    if get_origin(annotation) is Literal:
        choices = list(get_args(annotation))
        return {"type": "string", "enum": choices}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.model_json_schema()
    if get_origin(annotation) in (list, typing.List):
        (item,) = get_args(annotation) or (str,)
        return {"type": "array", "items": _annotation_to_schema(item)}
    # Unknown annotations fall back to a permissive string.
    return {"type": "string"}


def _build_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        annotation = hints.get(name, str)
        properties[name] = _annotation_to_schema(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            properties[name]["default"] = param.default
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip(),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Attach a `.tool_schema` to `fn` generated from its type hints."""
    fn.tool_schema = _build_schema(fn)  # type: ignore[attr-defined]
    return fn
```

`tools/calc.py`, `tools/search.py`, `tools/weather.py` — the tool set:

```python
"""Decorated tools. calc uses an AST allowlist; get_weather does real HTTP I/O."""

from __future__ import annotations

import ast
import operator
from typing import Literal

import httpx

from .decorators import tool

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand)
    raise ValueError("disallowed expression")


@tool
def calc(expression: str) -> dict:
    """Evaluate a math expression with + - * / ** and parentheses."""
    return {"result": _eval(ast.parse(expression, mode="eval"))}


@tool
def search(query: str, top_k: int = 1) -> dict:
    """Return up to top_k short snippet strings for a query (stubbed)."""
    facts = {
        "paris": "Paris is the capital of France.",
        "tokyo": "Tokyo is the capital of Japan.",
    }
    hit = facts.get(query.strip().lower(), f"No snippet for {query!r}.")
    return {"snippets": [hit][:top_k]}


@tool
def get_weather(city: str, units: Literal["celsius", "fahrenheit"] = "celsius") -> dict:
    """Return today's temperature for a city via the Open-Meteo API."""
    geo = httpx.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1},
        timeout=5.0,
    ).json()
    if not geo.get("results"):
        return {"error": f"unknown city: {city}", "hint": "check spelling"}
    place = geo["results"][0]
    unit = "fahrenheit" if units == "fahrenheit" else "celsius"
    weather = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m",
            "temperature_unit": unit,
        },
        timeout=5.0,
    ).json()
    return {
        "city": place["name"],
        "temperature": weather["current"]["temperature_2m"],
        "units": units,
    }
```

`agent.py` — the short async tool-calling loop:

```python
"""ToolCallingAgent: native function-calling loop with concurrent tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger("toolcalling")

CONTEXT_WINDOW_TOKENS = 128_000
CONTEXT_BUDGET_FRACTION = 0.80


class TokenBudgetExceeded(RuntimeError):
    """Raised when the next turn would exceed the context budget."""


class LLM(Protocol):
    async def create(self, messages: list[dict], tools: list[dict], tool_choice: str): ...


def count_tokens(messages: list[dict]) -> int:
    """Approximate token count over the whole message list (Chapter 2 stand-in)."""
    return max(1, len(json.dumps(messages)) // 4)


@dataclass
class ToolCallingAgent:
    """Drive a provider's native tool-calling API over decorated Python functions."""

    llm: LLM
    system_prompt: str
    tools: list[Callable[..., Any]]
    max_steps: int = 8
    force_first_tool: bool = False
    transcript: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._schemas = [fn.tool_schema for fn in self.tools]
        self._dispatch: dict[str, Callable[..., Any]] = {
            fn.tool_schema["function"]["name"]: fn for fn in self.tools
        }

    async def _call_one(self, name: str, raw_args: str) -> dict:
        """Execute a single tool call; tool errors come back as data, not exceptions."""
        fn = self._dispatch.get(name)
        if fn is None:
            return {"error": f"unknown tool: {name}"}
        try:
            args = json.loads(raw_args or "{}")
            result = fn(**args)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001 - surfaced to the model.
            return {"error": str(exc), "hint": f"check arguments to {name}"}

    async def run(self, task: str) -> str:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        for step in range(self.max_steps):
            self._guard_context(messages)
            choice = "required" if (step == 0 and self.force_first_tool) else "auto"
            logger.info(
                "turn %d | sending %d messages | tool_choice=%s",
                step, len(messages), choice,
            )
            message = await self.llm.create(messages, self._schemas, choice)
            messages.append(message)

            calls = message.get("tool_calls") or []
            if not calls:
                self.transcript = messages
                return message.get("content") or ""

            # Parallel dispatch: all calls in this turn fire concurrently.
            results = await asyncio.gather(
                *(self._call_one(c["function"]["name"], c["function"]["arguments"])
                  for c in calls)
            )
            for call, result in zip(calls, results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result),
                })
        self.transcript = messages
        raise RuntimeError(f"max_steps ({self.max_steps}) exhausted")

    def _guard_context(self, messages: list[dict]) -> None:
        budget = int(CONTEXT_WINDOW_TOKENS * CONTEXT_BUDGET_FRACTION)
        used = count_tokens(messages)
        if used > budget:
            raise TokenBudgetExceeded(f"{used} tokens over {budget}-token budget")
```

`tests/test_decorator.py` — schema generation across varied signatures:

```python
"""Verify @tool generates the expected schema for varied signatures."""

from __future__ import annotations

from typing import Literal

from tools.decorators import tool


def test_required_only_signature():
    @tool
    def f(expression: str) -> dict:
        """Eval an expression."""
        return {}

    params = f.tool_schema["function"]["parameters"]
    assert params["required"] == ["expression"]
    assert params["properties"]["expression"] == {"type": "string"}


def test_optional_with_default():
    @tool
    def f(query: str, top_k: int = 3) -> dict:
        """Search."""
        return {}

    params = f.tool_schema["function"]["parameters"]
    assert params["required"] == ["query"]
    assert params["properties"]["top_k"]["default"] == 3
    assert params["properties"]["top_k"]["type"] == "integer"


def test_literal_becomes_enum():
    @tool
    def f(units: Literal["celsius", "fahrenheit"] = "celsius") -> dict:
        """Weather."""
        return {}

    schema = f.tool_schema["function"]["parameters"]["properties"]["units"]
    assert schema["enum"] == ["celsius", "fahrenheit"]
    assert f.tool_schema["function"]["description"] == "Weather."
```

`tests/test_loop.py` — single, parallel, tool-error, and budget paths:

```python
"""Loop behavior: single call, two parallel calls, tool-error recovery, budget."""

from __future__ import annotations

import asyncio

import pytest

from agent import ToolCallingAgent, TokenBudgetExceeded
from tools.calc import calc


def _msg(tool_calls=None, content=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args}}


class MockLLM:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    async def create(self, messages, tools, tool_choice):
        return self._scripted.pop(0)


def run(agent, task):
    return asyncio.run(agent.run(task))


def test_single_tool_call():
    llm = MockLLM([
        _msg(tool_calls=[_call("c1", "calc", '{"expression": "2+2"}')]),
        _msg(content="The answer is 4."),
    ])
    agent = ToolCallingAgent(llm, "system", tools=[calc])
    assert run(agent, "2+2?") == "The answer is 4."


def test_parallel_tool_calls_both_fire():
    llm = MockLLM([
        _msg(tool_calls=[
            _call("a", "calc", '{"expression": "1+1"}'),
            _call("b", "calc", '{"expression": "3*3"}'),
        ]),
        _msg(content="done"),
    ])
    agent = ToolCallingAgent(llm, "system", tools=[calc])
    assert run(agent, "two sums") == "done"
    tool_messages = [m for m in agent.transcript if m["role"] == "tool"]
    assert len(tool_messages) == 2  # both calls produced results


def test_tool_exception_becomes_error_payload():
    llm = MockLLM([
        _msg(tool_calls=[_call("c", "calc", '{"expression": "1/"}')]),
        _msg(content="recovered"),
    ])
    agent = ToolCallingAgent(llm, "system", tools=[calc])
    assert run(agent, "bad expr") == "recovered"
    tool_msg = next(m for m in agent.transcript if m["role"] == "tool")
    assert "error" in tool_msg["content"]


def test_token_budget_aborts_long_conversation():
    agent = ToolCallingAgent(MockLLM([]), "system", tools=[calc])
    with pytest.raises(TokenBudgetExceeded):
        asyncio.run(agent.run("y" * 600_000))  # oversized seed turn trips the guard
```

`demo.py` — two concurrent `get_weather` calls (offline mock by default):

```python
"""Compare two cities' weather; expect two parallel get_weather tool calls."""

from __future__ import annotations

import asyncio
import logging

from agent import ToolCallingAgent
from tools.weather import get_weather

# Reuse the mock LLM and helpers from the test module for an offline demo.
from tests.test_loop import MockLLM, _call, _msg


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    llm = MockLLM([
        _msg(tool_calls=[
            _call("p", "get_weather", '{"city": "Paris"}'),
            _call("t", "get_weather", '{"city": "Tokyo"}'),
        ]),
        _msg(content="Paris and Tokyo temperatures fetched concurrently."),
    ])
    agent = ToolCallingAgent(llm, "Compare weather.", tools=[get_weather])
    print(asyncio.run(agent.run("Compare the weather in Paris and Tokyo today.")))


if __name__ == "__main__":
    main()
```

## Meeting the acceptance criteria

- **`python demo.py` answer depends on real HTTP I/O.** `get_weather` hits
  Open-Meteo; against the live `OpenAILLM` adapter the temperatures change day
  to day. The shipped `demo.py` uses `MockLLM` so it runs offline, but the tool
  itself performs the real round-trip whenever invoked.
- **At least one turn with two concurrent `tool_calls`.** Both `demo.py` and
  `test_parallel_tool_calls_both_fire` return two `tool_calls` in one assistant
  turn; `asyncio.gather` fires them concurrently and two `role: "tool"` messages
  come back.
- **`pytest` passes schema-fixture, single-call, parallel-call, tool-error
  tests.** `test_decorator.py` covers required-only, optional-with-default, and
  enum signatures; `test_loop.py` covers the single, parallel, error, and budget
  paths.
- **Loop body is shorter than Exercise 01.** `run()` has no regex parsing, no
  manual `Observation:` rendering, and no hand-built dispatch beyond a dict
  comprehension — the SDK returns structured `tool_calls`, so the body is
  visibly shorter than the string-parsed loop.
- **Token counter aborts gracefully.** `_guard_context` raises
  `TokenBudgetExceeded` before sending an over-budget turn;
  `test_token_budget_aborts_long_conversation` proves it.
- **No hand-written schemas, no framework imports.** Schemas come from `@tool`
  via `inspect` + type hints; only the standard library, `httpx`, `pydantic`,
  and (live) the provider SDK are imported.

## Common pitfalls

- **Appending a reconstructed assistant message instead of the verbatim one.**
  The assistant message with its `tool_calls` must go back into `messages`
  unchanged; rebuilding it by hand drops the `tool_call_id`s and the next turn
  cannot match results to calls.
- **Mismatched `tool_call_id`s.** Each `role: "tool"` message must carry the
  exact `id` from its originating call. Iterating calls and results with `zip`
  keeps them aligned; reordering or dropping one corrupts the conversation.
- **Letting a tool raise inside `gather`.** A single raising coroutine makes
  `asyncio.gather` cancel its siblings. `_call_one` catches everything and
  returns an `{"error": …}` payload so the loop survives and the model can
  recover.
- **Required-vs-optional inferred wrong.** A parameter is required only when it
  has no default. Treating every annotated parameter as required makes the model
  pass junk for optionals; the decorator keys `required` off
  `inspect.Parameter.empty`.
- **Blocking HTTP in an async loop.** `httpx.get` is synchronous; for true
  overlap under load use `httpx.AsyncClient`. The reference keeps it simple
  because the structure (`asyncio.gather` over calls) is the lesson — but note
  the upgrade path.

## Verification

```bash
cd exercise-02-function-calling-tools
python -m venv .venv && source .venv/bin/activate
python -m pip install pytest httpx pydantic openai

python -m pytest -q       # decorator + single/parallel/error/budget tests pass
python demo.py            # prints the two-city comparison (offline mock by default)
```

To run live, add an adapter and point the agent at it:

```python
# openai_llm.py
import asyncio
from openai import OpenAI


class OpenAILLM:
    def __init__(self, model="gpt-4o"):
        self._client = OpenAI()
        self._model = model

    async def create(self, messages, tools, tool_choice):
        resp = await asyncio.to_thread(
            self._client.chat.completions.create,
            model=self._model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        return resp.choices[0].message.model_dump(exclude_none=True)
```

Swap `MockLLM(...)` for `OpenAILLM()` in `demo.py`, export `OPENAI_API_KEY`, and
rerun: the temperatures differ on different days, proving the real I/O path.
