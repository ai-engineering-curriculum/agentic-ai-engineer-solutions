# mod-202-frameworks/exercise-04 — Solution

## Approach

The `search` and `calc` logic that lived inside three different agents now lives
in exactly one place — a standalone MCP server — and the agents reach it over a
JSON-RPC boundary instead of an in-process import. The payoff is the central MCP
claim: write the tool once, run it everywhere, swap the agent without touching
the tool. This solution proves it three ways — a raw client, a framework host,
and a *second* framework host against the same unchanged binary.

The server (`search_calc_server.py`) uses `FastMCP` and exposes `search` and
`calc` as `@mcp.tool()` functions. Three rules are load-bearing on stdio
transport:

1. **stdout is the protocol.** A stray `print` to stdout corrupts the JSON-RPC
   stream and wedges the client. Every diagnostic goes to `stderr`.
2. **The docstring is the tool description.** The model sees the docstring as
   the tool spec, so it is written for a model, not a human reader.
3. **Every argument is hostile.** `calc` validates the expression against an
   `ast` whitelist and returns a *structured error object* — not a raised
   exception — for anything outside it. A disallowed input is rejected cleanly
   and the server keeps serving.

The raw client (`client.py`) spawns the server over stdio, runs the
`initialize` handshake, calls `list_tools` (discovery — it learns names and
schemas without hard-coding them), and invokes both tools. The framework host
(`host_langgraph.py`) loads the *same* server through an MCP adapter and runs the
population-ratio task with the tools coming from the server process. Portability
is demonstrated by pointing a second host (the raw client, or a second adapter)
at the identical binary with zero edits to the server file.

## Reference implementation

### The server

```python
# search_calc_server.py — one MCP server; every agent in the module can use it.
from __future__ import annotations

import ast
import operator as op
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("search-calc")

# Deterministic corpus so offline runs are reproducible.
_CORPUS = {
    "india": {"country": "India", "population": 1_441_000_000, "source": "UN WPP 2024"},
    "china": {"country": "China", "population": 1_425_000_000, "source": "UN WPP 2024"},
}

_ALLOWED_BINOPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
_ALLOWED_UNARY = {ast.USub: op.neg, ast.UAdd: op.pos}


@mcp.tool()
def search(query: str) -> list[dict]:
    """Search the local population index. Returns up to 5 matching country records.

    Each record has: country (str), population (int), source (str).
    """
    q = query.lower()
    return [rec for key, rec in _CORPUS.items() if key in q][:5]


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"disallowed expression element: {type(node).__name__}")


@mcp.tool()
def calc(expression: str) -> dict:
    """Evaluate a basic arithmetic expression. Supports + - * / and parentheses.

    Returns {"ok": True, "result": float} on success, or
    {"ok": False, "error": str} for any disallowed or malformed input.
    The server never raises to the caller — adversarial input is rejected cleanly.
    """
    try:
        result = _safe_eval(ast.parse(expression, mode="eval").body)
        return {"ok": True, "result": round(result, 6)}
    except (ValueError, SyntaxError, TypeError, ZeroDivisionError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    # stdout is reserved for the protocol; all logs go to stderr.
    print("search-calc server starting", file=sys.stderr)
    mcp.run(transport="stdio")
```

### The raw client

```python
# client.py — spawn the server, handshake, discover tools, invoke both.
from __future__ import annotations

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command="python", args=["search_calc_server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("discovered tools:", [t.name for t in tools.tools])

            found = await session.call_tool("search", {"query": "India population"})
            print("search ->", found.content)

            good = await session.call_tool("calc", {"expression": "1441000000 / 1425000000"})
            print("calc ok ->", good.content)

            # Adversarial input: a disallowed function call. Server returns a
            # structured error and stays up — no crash, no protocol corruption.
            bad = await session.call_tool("calc", {"expression": "__import__('os').system('echo hi')"})
            print("calc adversarial ->", bad.content)


if __name__ == "__main__":
    asyncio.run(main())
```

### A framework host (LangGraph via langchain-mcp-adapters)

```python
# host_langgraph.py — same MCP server, consumed by a LangGraph agent.
from __future__ import annotations

import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent


async def main() -> None:
    # The server is launched as a subprocess; tools arrive over stdio, not import.
    client = MultiServerMCPClient({
        "search-calc": {
            "command": "python",
            "args": ["search_calc_server.py"],
            "transport": "stdio",
        }
    })
    tools = await client.get_tools()  # discovered, not hard-coded
    agent = create_react_agent("openai:gpt-4o-mini", tools)
    task = ("Compare the population of the two most populous countries in 2024 "
            "and report the ratio.")
    result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

The "second host" requirement is met by running `client.py` and
`host_langgraph.py` against the *same* `search_calc_server.py` — or by wiring the
server into a CrewAI/OpenAI-Agents host through its own MCP adapter. The server
file is byte-for-byte identical in every case; only the host wiring differs.

## Meeting the acceptance criteria

- **Raw client discovers and invokes over stdio.** `client.py` runs
  `initialize`, prints the tool names from `list_tools` (no hard-coding), and
  calls both `search` and `calc`.
- **Framework host runs the task from the served tools.** `host_langgraph.py`
  loads the tools through `MultiServerMCPClient` — the agent never imports the
  tool functions; they come from the subprocess.
- **Second host, unchanged binary.** The raw client and the framework host both
  point at the same `search_calc_server.py`; swapping hosts touches no server
  code.
- **Disallowed `calc` returns a structured error, server survives.** The
  `__import__(...)` probe returns `{"ok": False, "error": ...}` and the session
  continues to serve subsequent calls.
- **No stdout pollution.** The only stdout writer is the MCP transport; the
  startup banner and any diagnostics go to `stderr`.

## Common pitfalls

- **`print()` to stdout in the server.** It injects non-JSON bytes into the
  protocol stream and the client's parser chokes. Route every log to `stderr`.
  This is the single most common reason an MCP stdio server "mysteriously hangs."
- **Raising exceptions from a tool on bad input.** An unhandled exception can
  tear down the request and leak a stack trace. Validate, then return a
  structured error object so the model can react and the server stays up.
- **`eval`/`exec` in `calc`.** Once the expression originates from a model it is
  untrusted input. The `ast` whitelist (no `Call`, no `Name`, no `Attribute`) is
  the actual control.
- **Vague docstrings.** The docstring *is* the tool schema the model reads. "Do
  math" produces worse tool selection than the explicit input/output contract
  shown above.
- **Assuming MCP is free.** The JSON-RPC round-trip adds latency over an
  in-process call. For a microsecond-scale pure function called in a tight loop,
  that overhead can dominate — keep MCP for tools whose value (reuse, isolation,
  language independence) justifies the boundary.

## Verification

```bash
pip install mcp
python client.py
```

Expected output:

```text
discovered tools: ['search', 'calc']
search -> [{'country': 'India', 'population': 1441000000, 'source': 'UN WPP 2024'}]
calc ok -> {'ok': True, 'result': 1.011228}
calc adversarial -> {'ok': False, 'error': "disallowed expression element: Call"}
```

The adversarial line is the security proof: a disallowed `Call` node is rejected
with a structured error and the session keeps running. For the framework host,
`pip install langchain-mcp-adapters langgraph langchain-openai`, export
`OPENAI_API_KEY`, and run `python host_langgraph.py`; the agent solves the task
with tools served from the subprocess. Record in `NOTES.md` what each host had to
change to consume MCP versus an in-process import (host wiring) versus what
stayed identical (the server file).
