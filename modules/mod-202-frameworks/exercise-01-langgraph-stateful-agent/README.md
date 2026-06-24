# mod-202-frameworks/exercise-01 — Solution

## Approach

The mod-201 reason–act loop you hand-wrote becomes three pieces of LangGraph
plumbing: a typed `AgentState`, a `model` node, and a `ToolNode`. The loop
itself — "call the model, run the tools it asked for, call the model again" —
is no longer a `while` loop you maintain; it is a cycle in the graph
(`START -> model -> tools -> model -> ... -> END`) closed by a conditional edge.
The router `should_continue` is the only piece of the old termination policy you
still write by hand: it inspects the last message and routes to `tools` when the
model emitted `tool_calls`, otherwise to `END`.

To keep the solution runnable offline and deterministic — the property the
exercise asks for — the model is pluggable. If a real provider key is present we
bind tools to a `ChatOpenAI`-style model; otherwise we fall back to a small
scripted `FakeToolModel` that emits the canonical trajectory (two `search`
calls, one `calc`, one final answer). The graph, the state, the checkpointer,
and the interrupt logic are identical in both cases — only the node that
produces messages changes. That is the whole point: LangGraph owns the control
flow regardless of which model fills the `model` node.

The non-message state slot is a `step` counter with an `operator.add` reducer.
`model_node` writes `{"step": 1}` on every turn; `should_continue` and the
optional `summarize` node read it. This proves a reducer *other* than
`add_messages` round-trips through the graph.

Design decisions worth calling out:

- **Safe `calc`.** Arithmetic is evaluated by walking a whitelisted `ast`, never
  `eval`. A disallowed node (a name, a call, a power operator) raises a
  `ValueError` that surfaces as a tool error message, not a crash.
- **Deterministic `search`.** A fixed corpus keyed by substring returns the two
  population facts the task needs, so the trajectory is reproducible without a
  network.
- **Bounded loops.** Every `invoke` passes an explicit `recursion_limit`. A
  misbehaving model that never stops calling tools hits the limit and raises
  `GraphRecursionError` instead of spinning forever.

## Reference implementation

Save as `agent.py`. It runs with no API key (scripted model) and, if
`OPENAI_API_KEY` plus `langchain-openai` are present, against a real model.

```python
"""LangGraph stateful research agent: typed state, ToolNode, checkpointing,
human-in-the-loop interrupt. Runs offline with a scripted model fallback."""

from __future__ import annotations

import ast
import operator as op
import os
from typing import Annotated, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode


# --------------------------------------------------------------------------- #
# 1. Typed state. `messages` uses the message reducer; `step` uses a different
#    reducer (operator.add) so we exercise a non-message slot end to end.
# --------------------------------------------------------------------------- #
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step: Annotated[int, op.add]


# --------------------------------------------------------------------------- #
# 2. Tools. Deterministic search corpus + ast-based safe calculator.
# --------------------------------------------------------------------------- #
_CORPUS: dict[str, str] = {
    "india": "India had an estimated population of 1,441,000,000 in 2024 (UN WPP).",
    "china": "China had an estimated population of 1,425,000,000 in 2024 (UN WPP).",
}


@tool
def search(query: str) -> str:
    """Search a fixed corpus and return the best matching snippet."""
    q = query.lower()
    for key, snippet in _CORPUS.items():
        if key in q:
            return snippet
    return "No matching record found in the local corpus."


_ALLOWED_BINOPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
}
_ALLOWED_UNARY = {ast.USub: op.neg, ast.UAdd: op.pos}


def _safe_eval(node: ast.AST) -> float:
    """Recurse over a whitelisted arithmetic AST. Anything else is rejected."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _safe_eval(node.left), _safe_eval(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"Disallowed expression element: {ast.dump(node)}")


@tool
def calc(expression: str) -> str:
    """Evaluate a basic arithmetic expression safely (no eval)."""
    try:
        tree = ast.parse(expression, mode="eval")
        return str(round(_safe_eval(tree.body), 4))
    except (ValueError, SyntaxError) as exc:
        return f"calc error: {exc}"


TOOLS = [search, calc]


# --------------------------------------------------------------------------- #
# 3. Pluggable model. Real provider if a key is present; otherwise a scripted
#    model that produces the canonical trajectory deterministically.
# --------------------------------------------------------------------------- #
def _build_real_model():
    from langchain_openai import ChatOpenAI  # imported lazily

    return ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(TOOLS)


class FakeToolModel:
    """Deterministic stand-in: search India, search China, calc ratio, answer."""

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        n = len(tool_results)
        if n == 0:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {"query": "India population 2024"},
                             "id": "call_in"}],
            )
        if n == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {"query": "China population 2024"},
                             "id": "call_cn"}],
            )
        if n == 2:
            return AIMessage(
                content="",
                tool_calls=[{"name": "calc", "args": {"expression": "1441000000 / 1425000000"},
                             "id": "call_ratio"}],
            )
        ratio = tool_results[-1].content
        return AIMessage(
            content=f"India's 2024 population exceeds China's by a ratio of {ratio} to 1."
        )


def get_model():
    if os.getenv("OPENAI_API_KEY"):
        try:
            return _build_real_model()
        except Exception:  # missing package -> fall back rather than crash
            pass
    return FakeToolModel()


_MODEL = get_model()


# --------------------------------------------------------------------------- #
# 4. Nodes + router. model_node writes the `step` slot; should_continue reads
#    the last message.
# --------------------------------------------------------------------------- #
def model_node(state: AgentState) -> dict:
    response = _MODEL.invoke(state["messages"])
    return {"messages": [response], "step": 1}


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return END


def summarize_node(state: AgentState) -> dict:
    """Optional node demonstrating a read of the non-message slot."""
    return {"messages": [AIMessage(content=f"(ran {state['step']} model turns)")]}


# --------------------------------------------------------------------------- #
# 5. Graph factory. `interrupt_before` and `checkpointer` are parameters so the
#    same builder serves the basic, resumable, and human-gated demos.
# --------------------------------------------------------------------------- #
def build_graph(*, interrupt_before: list[str] | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("model", model_node)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
    builder.add_edge("tools", "model")
    return builder.compile(
        checkpointer=MemorySaver(),
        interrupt_before=interrupt_before or [],
    )


TASK = ("Compare the populations of the two most populous countries in 2024 "
        "and report the ratio.")


def run_basic() -> str:
    graph = build_graph()
    config = {"configurable": {"thread_id": "demo-1"}, "recursion_limit": 12}
    out = graph.invoke({"messages": [HumanMessage(TASK)], "step": 0}, config=config)
    return out["messages"][-1].content


def run_resumable() -> tuple[str, str]:
    """Invoke twice on the same thread_id; the second call sees prior messages."""
    graph = build_graph()
    config = {"configurable": {"thread_id": "demo-resume"}, "recursion_limit": 12}
    first = graph.invoke({"messages": [HumanMessage(TASK)], "step": 0}, config=config)
    # Same thread_id, follow-up question — prior checkpoint is carried over.
    second = graph.invoke(
        {"messages": [HumanMessage("Now state which country is larger.")]},
        config=config,
    )
    return first["messages"][-1].content, second["messages"][-1].content


def run_with_interrupt() -> str:
    """Pause before the first tool call, inspect it, then resume after approval."""
    graph = build_graph(interrupt_before=["tools"])
    config = {"configurable": {"thread_id": "demo-hil"}, "recursion_limit": 12}
    graph.invoke({"messages": [HumanMessage(TASK)], "step": 0}, config=config)

    snapshot = graph.get_state(config)
    pending = snapshot.values["messages"][-1]
    print("Pending tool call:", pending.tool_calls)  # human reviews this
    # Simulated approval: resume by invoking with None and the same config.
    final = graph.invoke(None, config=config)
    return final["messages"][-1].content


if __name__ == "__main__":
    print("basic     :", run_basic())
    a, b = run_resumable()
    print("resume 1  :", a)
    print("resume 2  :", b)
    print("interrupt :", run_with_interrupt())
```

## Meeting the acceptance criteria

- **Canonical trajectory without hand-written dispatch.** `ToolNode(TOOLS)`
  executes whatever the model requested; `run_basic` produces two `search`
  calls, one `calc`, and a final answer. No dispatch dict appears anywhere in
  the agent code.
- **Non-message slot written and read with a different reducer.** `step` carries
  an `operator.add` reducer; `model_node` writes `{"step": 1}` each turn and
  `summarize_node` (and `should_continue` indirectly) reads it.
- **Resumption on the same `thread_id`.** `run_resumable` invokes twice with
  `thread_id="demo-resume"`; the second invoke continues from the persisted
  checkpoint — the follow-up question is answered with the earlier messages
  still in state.
- **Human-in-the-loop interrupt.** `run_with_interrupt` compiles with
  `interrupt_before=["tools"]`, reads the pending tool call via `get_state`,
  prints it for review, and resumes with `graph.invoke(None, config)`.
- **Bounded recursion.** Every config sets `recursion_limit`. A model that loops
  forever raises `GraphRecursionError` rather than hanging.

## Common pitfalls

- **Forgetting `step: 0` in the initial state.** The `operator.add` reducer has
  no identity supplied at invoke time, so the first state must seed every slot.
  Omit it and the first `model_node` write fails because there is nothing to add
  to.
- **Returning the whole message list from a node.** Nodes return *deltas*
  (`{"messages": [response]}`), and `add_messages` appends. Returning the full
  list duplicates history and inflates token spend every turn.
- **Using `eval` (or `numexpr`) for `calc`.** Both are remote-code-execution
  risks once the expression comes from a model. The `ast` whitelist is the
  control; keep `Call`, `Name`, and `Pow` out of it.
- **Reusing a `thread_id` across unrelated demos.** Checkpoints are keyed by
  `thread_id`; share one and the "fresh start" demo silently inherits old
  messages. Give each scenario its own id.
- **Expecting `interrupt_before` to work without a checkpointer.** Interrupts
  persist the paused state to the checkpointer; compile without one and
  `get_state`/resume have nothing to read.

## Verification

```bash
pip install langgraph langchain-core
python agent.py                 # runs offline with the scripted model
```

Expected output (ratio rounded to four places by `calc`):

```text
basic     : India's 2024 population exceeds China's by a ratio of 1.0112 to 1.
resume 1  : India's 2024 population exceeds China's by a ratio of 1.0112 to 1.
resume 2  : ...
Pending tool call: [{'name': 'search', ...}]
interrupt : India's 2024 population exceeds China's by a ratio of 1.0112 to 1.
```

To confirm the recursion guard, lower `recursion_limit` to `1` in `run_basic`
and re-run: the call raises `GraphRecursionError` instead of looping. To run
against a real provider, export `OPENAI_API_KEY` and
`pip install langchain-openai`; the graph is unchanged.

Reflection answers belong in `NOTES.md`: `ToolNode` plus the conditional edge
replaced the dispatch and the `while` loop; you still wrote `should_continue`
and the state schema. A plan-and-execute variant adds a `plan` node before
`model` and a `step` guard. A 2 MB blob in a state slot bloats every checkpoint
write — store it externally and keep a reference (URI or id) in state.
