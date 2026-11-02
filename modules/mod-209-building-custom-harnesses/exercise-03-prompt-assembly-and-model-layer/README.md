# mod-209-building-custom-harnesses/exercise-03-prompt-assembly-and-model-layer — Solution

## Approach

This solution extends the
[exercise-01 reference harness](../exercise-01-build-a-minimal-harness/README.md).
Most of the work is in nine new modules. Three exercise-01 files get small,
backward-compatible changes, listed below. The exercise-01 tests still pass
unchanged.

The exercise has two halves. The first builds the system prompt from the
environment. The second makes model calls survive real-world failures and
report what they're doing.

**Instruction files (`instructions.py`).** The harness walks up from the
workspace root and collects `AGENTS.md` and `CLAUDE.md` from each directory.
It stops at the git root, which is the first directory holding `.git`. A
`.git` *file* counts too, because worktrees and submodules use one. With no
repo, the walk goes to the filesystem root. The user-level file comes from
`~/.config/minimal-harness/AGENTS.md`, or from `$HARNESS_CONFIG_DIR` when set,
which is how the tests keep your real home directory out of the results.

The files are ordered from most general (user-level) to most specific
(workspace root). When two rules conflict, the model needs a tie-breaker. A
specific rule should beat a general one, so specific files go last, and the
block header says "later files are more specific and win".
Each file is wrapped in `<instructions path="...">` so the model can tell
which rule came from where.

An `@relative/path.md` line is replaced with that file's text, resolved
relative to the importing file. Three things stop an import: the file is
already in the current import chain (a cycle), the chain is already five
deep, or the file doesn't exist. Each leaves a visible marker such as
`[import skipped: a.md would create a cycle]` instead of failing silently.
Only `.md` targets are imported. A line like `@secrets.env` stays as plain
text. Files are deduplicated by resolved path, because `CLAUDE.md` is often a
symlink to `AGENTS.md`.

The size cap keeps the most specific files. It walks from the workspace end
and stops at the first file that doesn't fit. That file and everything more
general are dropped, even a small user-level file above it. Skipping only the
large middle layer would apply grandparent rules without the parent's
overrides. A note lists the dropped paths so the model can read them with a
tool if it needs them.

**Skills (`skills.py`).** The harness scans `<config>/skills/*/SKILL.md` and
`<workspace>/.skills/*/SKILL.md`, in that order, so a workspace skill wins a
name clash. A minimal parser reads `key: value` lines between `---` fences.
Only one line per skill (`- name: description`) goes in the prompt, sorted by
name so the text is the same on every run. `load_skill(name)` looks the name
up in the index and reads the body from disk at call time. The name never
becomes a path, so `../../etc/passwd` is just an unknown skill.

**The system prompt (`prompt.py`, `environment.py`).** The prompt is a tuple
of frozen `PromptBlock`s in a fixed order:

```text
base -> environment -> skills -> instructions [cache breakpoint] -> session
```

The spec's environment block is split in two. Working directory, platform,
and "is this a git repo" rarely change, so they stay before the breakpoint.
Date and branch change between sessions, so they go in a `session` block
after it. The branch is read from `.git/HEAD` directly, so it works without
git installed. `test_cached_prefix_is_identical_across_days_and_branches`
checks the split: change the date and the branch, and the first four blocks
are byte-identical.

Why it matters: the API caches a *prefix* of the request, and the order of
that prefix is **tools → system → messages**. A `cache_control` breakpoint
caches everything up to and including the block it's on. Any change before
the breakpoint, even one character, is a cache miss for everything after it.
If the date were in the first block, every new day would miss the cache.
Tools come first in the prefix, so the adapter sorts them by name. Then
registering `load_skill` last, or building the registry in a different
order, doesn't change the bytes. One more catch: a prefix shorter than the
model's minimum cacheable length is not cached at all, and the API doesn't
return an error. The minimum differs by model. Check the prompt caching docs
for your model before you rely on a hit.

`to_anthropic_system` maps the blocks to the Messages API `system` parameter:
a list of `{"type": "text", "text": ...}` blocks, with
`"cache_control": {"type": "ephemeral"}` on the breakpoint block only.

**Retries (`retry.py`).** `RetryingModel` wraps any model client. It retries
one exception type, `ModelCallError`, which carries an HTTP status, a parsed
`retry-after`, and a connection flag. It knows nothing about any SDK. The
Anthropic adapter translates SDK exceptions into it:

| SDK exception | `ModelCallError` | Retried? |
| --- | --- | --- |
| `RateLimitError` (429) | `status=429`, `retry_after` from the header | yes |
| `APIStatusError` with status 529, or `OverloadedError` where the SDK has it | `status=529` | yes |
| `InternalServerError` and any other 5xx | `status=5xx` | yes |
| `APIConnectionError`, `APITimeoutError` (a subclass) | `connection=True` | yes |
| `BadRequestError` (400), `AuthenticationError` (401), `PermissionDeniedError` (403), other 4xx | `status=4xx` | no |
| anything else | not translated, raised as is | no |

The translation keys on `status_code`, not class names, so it works across
SDK versions that add or rename subclasses. A streamed request can also fail
*after* the HTTP 200 arrives. The SDK then raises `APIStatusError` with
status 200 and an error `type` such as `overloaded_error` from the event body.
The adapter maps those types back to 429, 529, or 500.

Delays use exponential backoff with full jitter: a uniform random wait
between 0 and `min(max_delay, base_delay * 2**n)`. Jitter spreads retries out
so many clients that failed together don't all retry together. A `retry-after`
header (seconds form) replaces the computed delay. Three limits apply: a
maximum number of attempts, a per-wait cap, and a maximum total wait. If the
next wait would exceed the total, the wrapper gives up now instead of
sleeping first. Each retry emits a `retry` event with the attempt number, the
delay, and the reason. Sleep and RNG are fields, so tests pass `sleeps.append`
and a seeded `random.Random`, and nothing actually waits.

The SDK retries on its own by default (twice). `CachedAnthropicModel` turns
that off with `with_options(max_retries=0)`. With two retry layers you'd get
up to 3 × 5 attempts, and the inner ones would be invisible.

**Events and streaming (`events.py`, `anthropic_model.py`).** The harness
takes an `on_event` callback and emits `text_delta`, `tool_call_started`,
`tool_call_finished`, `turn_finished`, and `retry`. `call_model` streams when
the model has a `stream` method. Otherwise it calls `complete` and emits the
whole text as one delta, so a scripted run produces the same event types as
a live one. `CachedAnthropicModel.stream` uses `messages.stream`, emits each
chunk from `text_stream`, and builds the turn from `get_final_message()`.
`turn_finished` fires when the model's turn ends, before its tool calls run.
It carries the stop reason and token usage, including
`cache_read_tokens` and `cache_creation_tokens`.

**Parallel tools (`prompted.py`).** `Tool` gains a `read_only` flag.
`read_file`, `list_dir`, and `load_skill` set it. When one model turn asks for
several calls and every one is read-only, they run in a `ThreadPoolExecutor`.
`pool.map` returns results in input order, whatever order the calls finish
in, so results go back to the model in call order. A batch containing a write,
a command, or an unknown tool runs one call at a time, in order. Started
events are emitted from the main thread in call order. Finished events come
from worker threads in completion order.

**Where it plugs in.** `PromptedHarness` subclasses the exercise-01
`Harness` and overrides two new seams: `_call_model` and `_execute_all`.
`build_prompted_harness` scans skills and instructions, detects the
environment, registers `load_skill`, and returns the harness.
`prompted_cli.py` is the live entry point.

## Reference implementation

Layout. Files marked *unchanged* are byte-for-byte the exercise-01 files; copy
them from the [exercise-01 solution](../exercise-01-build-a-minimal-harness/README.md).

```text
exercise-03-prompt-assembly-and-model-layer/
├── harness/
│   ├── __init__.py          # unchanged
│   ├── types.py             # CHANGED: cache-token fields on ModelTurn
│   ├── workspace.py         # unchanged
│   ├── schema.py            # unchanged
│   ├── models.py            # unchanged
│   ├── tools.py             # CHANGED: Tool.read_only; read_file and list_dir set it
│   ├── permissions.py       # unchanged
│   ├── hooks.py             # unchanged
│   ├── loop.py              # CHANGED: _call_model and _execute_all seams
│   ├── cli.py               # unchanged (prompted_cli reuses its terminal approver)
│   ├── environment.py       # NEW: git root, branch, environment text
│   ├── instructions.py      # NEW: AGENTS.md/CLAUDE.md discovery, imports, cap
│   ├── skills.py            # NEW: skills index and the load_skill tool
│   ├── prompt.py            # NEW: PromptBlock, assembly, Anthropic system param
│   ├── events.py            # NEW: Event, sinks, call_model
│   ├── retry.py             # NEW: ModelCallError, RetryPolicy, RetryingModel
│   ├── anthropic_model.py   # NEW: CachedAnthropicModel, SDK error translation
│   ├── prompted.py          # NEW: PromptedHarness(Harness), builder
│   └── prompted_cli.py      # NEW: live entry point with event printing
├── tests/
│   ├── conftest.py              # unchanged
│   ├── test_tools.py            # unchanged
│   ├── test_loop.py             # unchanged
│   ├── test_models.py           # unchanged
│   ├── test_instructions.py     # NEW
│   ├── test_prompt.py           # NEW
│   ├── test_retry.py            # NEW
│   ├── test_anthropic_model.py  # NEW
│   └── test_prompted.py         # NEW
└── pyproject.toml           # unchanged
```

The new code is about 890 lines of package code and 770 lines of tests.

The three changed files, and why each change is needed:

- `loop.py`: two lines in `_loop` now call `self._call_model(state, tools)`
  and `self._execute_all(turn.tool_calls)`. The base versions do exactly what
  the inline code did. They exist so `PromptedHarness` can stream and run
  calls in parallel without copying the loop.
- `tools.py`: `Tool` gets `read_only: bool = False`. `read_file` and
  `list_dir` set it to `True`. The spec asks for the flag to live in the
  registry, next to the tool.
- `types.py`: `ModelTurn` gets `cache_read_tokens` and `cache_creation_tokens`,
  both defaulting to 0. Existing five-argument constructions still work. The
  live check needs these numbers, and they belong with the other usage
  counts.

The full files follow. The changed lines are marked with `exercise-03`
comments or are visible in the seams.

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
            turn = self._call_model(state, tools)
            state = replace(
                state.append(_assistant_message(turn)),
                turns=state.turns + 1,
                tokens=state.tokens + turn.input_tokens + turn.output_tokens,
                final_text=turn.text,
            )
            if not turn.tool_calls:
                return _result(state, "done")
            results = self._execute_all(turn.tool_calls)
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

    def _call_model(self, state: LoopState, tools: list[dict]) -> ModelTurn:
        """Extension point. exercise-03 overrides this to stream and use prompt blocks."""
        return self.model.complete(self.system, list(state.messages), tools)

    def _execute_all(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        """Extension point. exercise-03 overrides this to run read-only calls in parallel."""
        return tuple(self.execute(call) for call in calls)

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
    read_only: bool = False  # exercise-03: read-only calls in one turn may run in parallel

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
            Tool(
                "read_file",
                "Read a UTF-8 text file in the workspace.",
                path_only,
                read_file,
                read_only=True,
            )
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
                read_only=True,
            )
        )
        .register(run_command or make_run_command())
    )
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
    # exercise-03: prompt-cache usage. Defaults keep exercise-01 callers working.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


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

### `harness/environment.py`

```python
"""What the model should know about where it runs.

Split in two on purpose. `stable_text` (directory, platform, repo or not)
rarely changes and sits before the cache breakpoint. `session_text` (date,
branch) changes between sessions and sits after it.

Git facts come from reading `.git` directly, not from running git, so this
works without git installed and is deterministic in tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HEAD_REF_PREFIX = "ref: refs/heads/"
SHORT_SHA = 12


def find_git_root(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding `.git` (a dir or a file).

    `.git` is a file, not a directory, in worktrees and submodules.
    """
    return next((d for d in (start, *start.parents) if (d / ".git").exists()), None)


def _git_dir(git_root: Path) -> Path:
    dot_git = git_root / ".git"
    if dot_git.is_dir():
        return dot_git
    text = dot_git.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        raise OSError(f"unrecognized .git file: {dot_git}")
    target = Path(text.removeprefix("gitdir:").strip())
    return target if target.is_absolute() else (git_root / target).resolve()


def current_branch(git_root: Path) -> str | None:
    """Branch name, 'detached at <sha>', or None if HEAD can't be read."""
    try:
        head = (_git_dir(git_root) / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith(HEAD_REF_PREFIX):
        return head.removeprefix(HEAD_REF_PREFIX)
    return f"detached at {head[:SHORT_SHA]}"


@dataclass(frozen=True)
class Environment:
    cwd: str
    platform: str
    git_root: str | None
    today: str
    branch: str | None

    def stable_text(self) -> str:
        repo = f"yes (root: {self.git_root})" if self.git_root else "no"
        return (
            "# Environment\n"
            f"Working directory: {self.cwd}\n"
            f"Platform: {self.platform}\n"
            f"Git repository: {repo}"
        )

    def session_text(self) -> str:
        branch = self.branch or ("unknown" if self.git_root else "n/a (not a git repository)")
        return f"# Session\nToday's date: {self.today}\nCurrent git branch: {branch}"


def detect_environment(root: Path, today: date | None = None) -> Environment:
    git_root = find_git_root(root)
    return Environment(
        cwd=str(root),
        platform=sys.platform,
        git_root=str(git_root) if git_root else None,
        today=(today or date.today()).isoformat(),
        branch=current_branch(git_root) if git_root else None,
    )
```

### `harness/instructions.py`

```python
"""Instruction-file discovery: AGENTS.md and CLAUDE.md, general to specific.

Order: the user-level file, then each directory from the stop point (git root,
or filesystem root) down to the workspace root. Later files are more specific
and win when rules conflict, so they go last, nearest the task.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from harness.environment import find_git_root

INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md")
CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("~/.config/minimal-harness")
MAX_IMPORT_DEPTH = 5
DEFAULT_CAP_CHARS = 40_000
IMPORT_LINE = re.compile(r"^@(\S+\.md)$")


def config_dir() -> Path:
    """User-level config directory. Tests point HARNESS_CONFIG_DIR at a temp dir."""
    return Path(os.environ.get(CONFIG_DIR_ENV) or DEFAULT_CONFIG_DIR).expanduser()


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    text: str

    def render(self) -> str:
        return f'<instructions path="{self.path}">\n{self.text.strip()}\n</instructions>'


@dataclass(frozen=True)
class Instructions:
    files: tuple[InstructionFile, ...]
    dropped: tuple[Path, ...] = ()
    cap_chars: int = DEFAULT_CAP_CHARS

    def render(self) -> str:
        if not self.files and not self.dropped:
            return "# Instructions\nNo instruction files were found."
        parts = ["# Instructions\nMost general first. Later files are more specific and win."]
        if self.dropped:
            names = ", ".join(str(p) for p in self.dropped)
            parts.append(
                f"[note] Dropped to fit the {self.cap_chars:,}-char instruction cap "
                f"(most general first): {names}. Read them with a tool if you need them."
            )
        parts += [f.render() for f in self.files]
        return "\n\n".join(parts)


def search_dirs(workspace_root: Path) -> list[Path]:
    """Workspace root and its parents up to the git root (or filesystem root)."""
    stop = find_git_root(workspace_root)
    dirs: list[Path] = []
    for d in (workspace_root, *workspace_root.parents):
        dirs.append(d)
        if d == stop:
            break
    return dirs


def instruction_paths(workspace_root: Path, user_dir: Path | None) -> tuple[Path, ...]:
    """Existing files, general to specific, each real file once."""
    candidates = [user_dir / n for n in INSTRUCTION_NAMES] if user_dir else []
    for d in reversed(search_dirs(workspace_root)):
        candidates += [d / n for n in INSTRUCTION_NAMES]
    seen: set[Path] = set()
    found: list[Path] = []
    for path in candidates:
        real = path.resolve()  # CLAUDE.md is often a symlink to AGENTS.md
        if path.is_file() and real not in seen:
            seen.add(real)
            found.append(path)
    return tuple(found)


def read_with_imports(path: Path, depth: int = 0, chain: tuple[Path, ...] = ()) -> str:
    """File text with each `@relative/path.md` line replaced by that file's text."""
    chain = (*chain, path.resolve())
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(_expand(line, path, depth, chain) for line in lines)


def _expand(line: str, importer: Path, depth: int, chain: tuple[Path, ...]) -> str:
    match = IMPORT_LINE.match(line.strip())
    if match is None:
        return line
    ref = match.group(1)
    target = (importer.parent / ref).resolve()
    if target in chain:
        return f"[import skipped: {ref} would create a cycle]"
    if depth >= MAX_IMPORT_DEPTH:
        return f"[import skipped: {ref} is past the depth limit of {MAX_IMPORT_DEPTH}]"
    if not target.is_file():
        return f"[import not found: {ref}]"
    return read_with_imports(target, depth + 1, chain)


def apply_cap(
    files: tuple[InstructionFile, ...], cap_chars: int
) -> tuple[tuple[InstructionFile, ...], tuple[Path, ...]]:
    """Keep the most specific files that fit. Drop the first misfit and everything above it.

    Dropping a middle layer would apply grandparent rules without the parent's
    overrides, so the kept set is always a contiguous, most-specific run.
    """
    total = 0
    for kept_count, f in enumerate(reversed(files)):
        total += len(f.text)
        if total > cap_chars:
            cut = len(files) - kept_count
            return files[cut:], tuple(f.path for f in files[:cut])
    return files, ()


def load_instructions(
    workspace_root: Path, user_dir: Path | None, cap_chars: int = DEFAULT_CAP_CHARS
) -> Instructions:
    paths = instruction_paths(workspace_root, user_dir)
    files = tuple(InstructionFile(p, read_with_imports(p)) for p in paths)
    kept, dropped = apply_cap(files, cap_chars)
    return Instructions(kept, dropped, cap_chars)
```

### `harness/skills.py`

```python
"""Skills: an index in the system prompt, bodies loaded on demand.

A skill is `<dir>/<skill>/SKILL.md` with `name:` and `description:` in a
frontmatter block. Only the one-line index goes in the prompt. The model
calls `load_skill(name)` to read a body, so unused skills cost one line each.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.tools import Tool, ToolError
from harness.workspace import Workspace

SKILL_FILE = "SKILL.md"
FENCE = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal `key: value` frontmatter parser. Returns (fields, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, text
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == FENCE), None)
    if end is None:
        return {}, text
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields, "\n".join(lines[end + 1 :]).strip() + "\n"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        """Read at call time, so the index never holds bodies."""
        return parse_frontmatter(self.path.read_text(encoding="utf-8"))[1]


@dataclass(frozen=True)
class SkillIndex:
    skills: tuple[Skill, ...] = ()

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.skills if s.name == name), None)

    def render(self) -> str:
        if not self.skills:
            return "# Skills\nNo skills are installed."
        lines = [f"- {s.name}: {s.description or '(no description)'}" for s in self.skills]
        return "# Skills\nCall load_skill(name) to read one before using it.\n" + "\n".join(lines)


def skill_dirs(workspace_root: Path, user_dir: Path | None) -> tuple[Path, ...]:
    """User-level first, workspace last, so a workspace skill wins a name clash."""
    user = (user_dir / "skills",) if user_dir else ()
    return (*user, workspace_root / ".skills")


def scan_skills(dirs: Iterable[Path]) -> SkillIndex:
    by_name: dict[str, Skill] = {}
    for root in dirs:
        for path in sorted(root.glob(f"*/{SKILL_FILE}")):
            fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            name = fields.get("name") or path.parent.name
            by_name[name] = Skill(name, fields.get("description", ""), path)
    # Sorted by name: the index text is identical run to run, so it caches.
    return SkillIndex(tuple(by_name[n] for n in sorted(by_name)))


def make_load_skill_tool(index: SkillIndex) -> Tool:
    def load_skill(ws: Workspace, args: dict[str, Any]) -> str:
        # Look up by indexed name only. The name never becomes a path, so
        # '../../etc/passwd' is just an unknown skill.
        skill = index.get(args["name"])
        if skill is None:
            known = ", ".join(s.name for s in index.skills) or "(none)"
            raise ToolError(f"unknown skill {args['name']!r}; available: {known}")
        return skill.body()

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    desc = "Return the full instructions for a skill listed in the system prompt."
    return Tool("load_skill", desc, schema, load_skill, read_only=True)
```

### `harness/prompt.py`

```python
"""System prompt as an ordered list of blocks, with one cache breakpoint.

The provider caches a prefix of the request: tools, then system, then
messages. A breakpoint caches everything up to and including its block. So
blocks that rarely change go first, the breakpoint goes on the last of them,
and per-session values (date, branch) go after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from harness.environment import Environment

BASE_PROMPT = """You are a coding agent working in a sandboxed workspace.

Tool use:
- Use the tools to inspect and change files. Paths are relative to the workspace root.
- Read a file before you change it. Prefer small, targeted edits.
- Independent read-only calls (read_file, list_dir, load_skill) may go in one turn.
- If a tool result is an error, read it and adjust. Don't repeat the same call.
- Skills are listed below by name. Call load_skill before following one.

When you are done:
- Reply with a short summary of what changed and how you checked it, and make no tool calls.
- If you could not finish, say what is left and why."""


@dataclass(frozen=True)
class PromptBlock:
    label: str
    text: str
    cache_breakpoint: bool = False


SystemPrompt = str | Sequence[PromptBlock]


def assemble_system_prompt(
    env: Environment, skills_text: str, instructions_text: str, base: str = BASE_PROMPT
) -> tuple[PromptBlock, ...]:
    """Fixed order: base, environment, skills, instructions | session.

    The breakpoint sits on the instructions block, the last one that rarely
    changes. Everything that changes per session comes after it.
    """
    return (
        PromptBlock("base", base),
        PromptBlock("environment", env.stable_text()),
        PromptBlock("skills", skills_text),
        PromptBlock("instructions", instructions_text, cache_breakpoint=True),
        PromptBlock("session", env.session_text()),
    )


def render_text(system: SystemPrompt) -> str:
    """One string, for models that take a plain system prompt."""
    if isinstance(system, str):
        return system
    return "\n\n".join(block.text for block in system)


def to_anthropic_system(system: SystemPrompt) -> str | list[dict[str, Any]]:
    """Map blocks to the Messages API `system` param: a list of text blocks."""
    if isinstance(system, str):
        return system
    if sum(block.cache_breakpoint for block in system) > 1:
        raise ValueError("use one cache breakpoint in the system prompt")
    out: list[dict[str, Any]] = []
    for block in system:
        item: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache_breakpoint:
            item["cache_control"] = {"type": "ephemeral"}
        out.append(item)
    return out
```

### `harness/events.py`

```python
"""Harness events and the one function that turns a model call into events.

A CLI prints events as they arrive. Tests collect them in a list. Events may
be emitted from worker threads (parallel tool calls), so a sink must be safe
to call from more than one thread. `list.append` is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from harness.prompt import SystemPrompt
from harness.types import ModelTurn

EventType = Literal[
    "text_delta", "tool_call_started", "tool_call_finished", "turn_finished", "retry"
]


@dataclass(frozen=True)
class Event:
    type: EventType
    data: Mapping[str, Any] = field(default_factory=dict)


EventSink = Callable[[Event], None]


def null_sink(event: Event) -> None:
    """The default sink: drop every event."""


class StreamingModel(Protocol):
    def stream(
        self, system: SystemPrompt, messages: list[dict], tools: list[dict], on_event: EventSink
    ) -> ModelTurn: ...


def call_model(
    model: Any, system: SystemPrompt, messages: list[dict], tools: list[dict], on_event: EventSink
) -> ModelTurn:
    """Stream when the model can; otherwise complete and emit the text as one delta.

    The fallback keeps ScriptedModel (exercise-01) usable, and it means a
    scripted run produces the same event types as a live one.
    """
    stream = getattr(model, "stream", None)
    if callable(stream):
        return stream(system, messages, tools, on_event)
    turn = model.complete(system, messages, tools)
    if turn.text:
        on_event(Event("text_delta", {"text": turn.text}))
    return turn
```

### `harness/retry.py`

```python
"""Retries with exponential backoff and full jitter.

The wrapper knows nothing about any SDK. It retries `ModelCallError`, a
small error type that carries an HTTP status, a parsed `retry-after`, and a
connection flag. Each provider adapter translates its own exceptions into it
(see anthropic_model.translate_sdk_error). That keeps these tests SDK-free.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.events import Event, EventSink, call_model, null_sink
from harness.prompt import SystemPrompt
from harness.types import ModelTurn

RATE_LIMITED = 429
OVERLOADED = 529
SERVER_ERROR_MIN = 500


class ModelCallError(Exception):
    """A failed model call, described in provider-neutral terms."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        connection: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.connection = connection

    @property
    def retryable(self) -> bool:
        """429, 529, any other 5xx, and connection failures or timeouts."""
        if self.connection:
            return True
        return self.status is not None and (
            self.status == RATE_LIMITED or self.status >= SERVER_ERROR_MIN
        )

    @property
    def reason(self) -> str:
        if self.connection:
            return f"connection error: {self}"
        if self.status == RATE_LIMITED:
            return "rate limited (429)"
        if self.status == OVERLOADED:
            return "overloaded (529)"
        return f"HTTP {self.status}: {self}"


def parse_retry_after(value: str | None) -> float | None:
    """Seconds form only ('7', '1.5'). The HTTP-date form falls back to backoff."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 32.0
    max_total_wait: float = 120.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay <= 0 or self.max_delay < self.base_delay:
            raise ValueError("need max_attempts >= 1 and 0 < base_delay <= max_delay")

    def backoff(self, retry_index: int, rng: random.Random) -> float:
        """Full jitter: uniform between 0 and min(max_delay, base * 2**n)."""
        return rng.uniform(0, min(self.max_delay, self.base_delay * 2**retry_index))


@dataclass(frozen=True)
class RetryingModel:
    """Wraps any model client. Sleep and RNG are injectable for tests."""

    inner: Any
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    on_event: EventSink = null_sink

    def complete(self, system: SystemPrompt, messages: list[dict], tools: list[dict]) -> ModelTurn:
        return self._with_retries(
            lambda: self.inner.complete(system, messages, tools), self.on_event
        )

    def stream(
        self, system: SystemPrompt, messages: list[dict], tools: list[dict], on_event: EventSink
    ) -> ModelTurn:
        """Retry events go to the caller's sink, so the harness sees them."""
        return self._with_retries(
            lambda: call_model(self.inner, system, messages, tools, on_event), on_event
        )

    def _with_retries(self, attempt: Callable[[], ModelTurn], on_event: EventSink) -> ModelTurn:
        waited = 0.0
        number = 1
        while True:
            try:
                return attempt()
            except ModelCallError as exc:
                delay = self._next_delay(exc, number, waited)
                if delay is None:
                    exc.add_note(f"gave up after {number} attempt(s), {waited:.1f}s waited")
                    raise
                on_event(Event("retry", {"attempt": number, "delay": delay, "reason": exc.reason}))
            self.sleep(delay)
            waited += delay
            number += 1

    def _next_delay(self, exc: ModelCallError, number: int, waited: float) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        if not exc.retryable or number >= self.policy.max_attempts:
            return None
        if exc.retry_after is not None:
            delay = exc.retry_after
        else:
            delay = self.policy.backoff(number - 1, self.rng)
        if waited + delay > self.policy.max_total_wait:
            return None
        return delay
```

### `harness/anthropic_model.py`

```python
"""Anthropic adapter with prompt blocks, cache breakpoints, streaming, and
error translation.

It extends the exercise-01 AnthropicModel instead of replacing it. Three
differences: `system` may be a tuple of PromptBlocks, tools are sorted by
name so the cached prefix is byte-stable, and SDK errors become
ModelCallError so RetryingModel can decide what to do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from harness.events import Event, EventSink
from harness.models import AnthropicModel, _to_turn
from harness.prompt import SystemPrompt, to_anthropic_system
from harness.retry import ModelCallError, parse_retry_after
from harness.types import ModelTurn

# A streamed request can fail after the HTTP 200 arrives. The SDK then raises
# APIStatusError carrying the 200 and an error `type` from the event body.
STREAM_ERROR_STATUS = {"rate_limit_error": 429, "overloaded_error": 529, "api_error": 500}


def translate_sdk_error(exc: Exception) -> ModelCallError | None:
    """Map an anthropic SDK exception to ModelCallError. None means 'not ours'."""
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status < 400:
            status = STREAM_ERROR_STATUS.get(str(getattr(exc, "type", "")), status)
        headers = getattr(exc.response, "headers", None) or {}
        retry_after = parse_retry_after(headers.get("retry-after"))
        return ModelCallError(str(exc), status=status, retry_after=retry_after)
    if isinstance(exc, anthropic.APIConnectionError):  # APITimeoutError is a subclass
        return ModelCallError(str(exc), connection=True)
    return None


@contextmanager
def translated_sdk_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        mapped = translate_sdk_error(exc)
        if mapped is None:
            raise
        raise mapped from exc


def to_cached_turn(response: Any) -> ModelTurn:
    usage = response.usage
    return replace(
        _to_turn(response),
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


@dataclass(frozen=True)
class CachedAnthropicModel(AnthropicModel):
    @cached_property
    def _sdk(self) -> Any:
        if self.client is not None:
            return self.client
        # The SDK retries on its own by default. Turn that off so RetryingModel
        # is the only retry layer and every retry shows up as an event.
        return AnthropicModel._sdk.func(self).with_options(max_retries=0)

    def request(
        self, system: SystemPrompt, messages: list[dict], tools: list[dict]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": to_anthropic_system(system),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = sorted(tools, key=lambda t: t["name"])
        return kwargs

    def complete(self, system: SystemPrompt, messages: list[dict], tools: list[dict]) -> ModelTurn:
        with translated_sdk_errors():
            response = self._sdk.messages.create(**self.request(system, messages, tools))
        return to_cached_turn(response)

    def stream(
        self, system: SystemPrompt, messages: list[dict], tools: list[dict], on_event: EventSink
    ) -> ModelTurn:
        with translated_sdk_errors():
            with self._sdk.messages.stream(**self.request(system, messages, tools)) as stream:
                for text in stream.text_stream:
                    on_event(Event("text_delta", {"text": text}))
                response = stream.get_final_message()
        return to_cached_turn(response)
```

### `harness/prompted.py`

```python
"""PromptedHarness: the exercise-01 loop with prompt blocks, events, and
parallel read-only tool calls.

It overrides two seams in Harness: `_call_model` (stream, emit events, pass
blocks) and `_execute_all` (thread pool when every call is read-only).
`build_prompted_harness` assembles the system prompt from the environment.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from harness.environment import detect_environment
from harness.events import Event, EventSink, call_model, null_sink
from harness.instructions import DEFAULT_CAP_CHARS, config_dir, load_instructions
from harness.loop import Harness, LoopState
from harness.permissions import PermissionPolicy
from harness.prompt import PromptBlock, assemble_system_prompt
from harness.skills import make_load_skill_tool, scan_skills, skill_dirs
from harness.tools import ToolRegistry, default_registry
from harness.types import ModelTurn, ToolCall, ToolResult
from harness.workspace import Workspace

MAX_WORKERS = 8


@dataclass(frozen=True)
class PromptedHarness(Harness):
    system_blocks: tuple[PromptBlock, ...] = ()
    on_event: EventSink = null_sink
    max_workers: int = MAX_WORKERS

    def _call_model(self, state: LoopState, tools: list[dict]) -> ModelTurn:
        system = self.system_blocks or self.system
        turn = call_model(self.model, system, list(state.messages), tools, self.on_event)
        self.on_event(Event("turn_finished", _turn_data(state.turns + 1, turn)))
        return turn

    def _execute_all(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        if len(calls) > 1 and all(self._is_read_only(c) for c in calls):
            return self._execute_parallel(calls)
        # Any write or command in the batch: one at a time, in order.
        return tuple(self._started_then_run(c) for c in calls)

    def _is_read_only(self, call: ToolCall) -> bool:
        tool = self.registry.get(call.name)
        return tool is not None and tool.read_only

    def _execute_parallel(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        for call in calls:  # started events in call order, from this thread
            self._emit_started(call)
        workers = min(self.max_workers, len(calls))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool") as pool:
            # map() yields in input order, whatever order the calls finish in.
            return tuple(pool.map(self._run_and_report, calls))

    def _started_then_run(self, call: ToolCall) -> ToolResult:
        self._emit_started(call)
        return self._run_and_report(call)

    def _emit_started(self, call: ToolCall) -> None:
        self.on_event(Event("tool_call_started", {"id": call.id, "name": call.name}))

    def _run_and_report(self, call: ToolCall) -> ToolResult:
        result = self.execute(call)
        data = {"id": call.id, "name": call.name, "is_error": result.is_error}
        self.on_event(Event("tool_call_finished", data))
        return result


def _turn_data(number: int, turn: ModelTurn) -> dict[str, Any]:
    return {
        "turn": number,
        "stop_reason": turn.stop_reason,
        "tool_calls": len(turn.tool_calls),
        "input_tokens": turn.input_tokens,
        "output_tokens": turn.output_tokens,
        "cache_read_tokens": turn.cache_read_tokens,
        "cache_creation_tokens": turn.cache_creation_tokens,
    }


def build_system_prompt(
    workspace: Workspace,
    registry: ToolRegistry,
    user_dir: Path | None,
    today: date | None = None,
    cap_chars: int = DEFAULT_CAP_CHARS,
) -> tuple[tuple[PromptBlock, ...], ToolRegistry]:
    """Scan skills and instructions, detect the environment, register load_skill."""
    skills = scan_skills(skill_dirs(workspace.root, user_dir))
    instructions = load_instructions(workspace.root, user_dir, cap_chars)
    env = detect_environment(workspace.root, today)
    blocks = assemble_system_prompt(env, skills.render(), instructions.render())
    return blocks, registry.register(make_load_skill_tool(skills))


def build_prompted_harness(
    model: Any,
    workspace: Workspace,
    policy: PermissionPolicy,
    *,
    user_dir: Path | None = None,
    today: date | None = None,
    registry: ToolRegistry | None = None,
    **overrides: Any,
) -> PromptedHarness:
    blocks, full_registry = build_system_prompt(
        workspace, registry or default_registry(), user_dir or config_dir(), today
    )
    return PromptedHarness(
        model=model,
        workspace=workspace,
        policy=policy,
        registry=full_registry,
        system_blocks=blocks,
        **overrides,
    )
```

### `harness/prompted_cli.py`

```python
"""Live entry point: python -m harness.prompted_cli --workspace DIR "task".

Streams text to stdout and prints tool, retry, and turn events (with cache
usage) to stderr. --show-prompt prints the assembled system prompt and exits
without calling a model.
"""

from __future__ import annotations

import argparse
import sys

from harness.anthropic_model import CachedAnthropicModel
from harness.cli import terminal_approver
from harness.events import Event
from harness.hooks import block_protected_writes, python_syntax_note
from harness.permissions import RuleBasedPolicy
from harness.prompted import PromptedHarness, build_prompted_harness
from harness.retry import ModelCallError, RetryingModel
from harness.workspace import Workspace

READ_TOOLS = frozenset({"read_file", "list_dir", "load_skill"})


def print_event(event: Event) -> None:
    data = event.data
    if event.type == "text_delta":
        sys.stdout.write(data["text"])
        sys.stdout.flush()
    elif event.type == "tool_call_started":
        print(f"\n[tool] {data['name']} ({data['id']})", file=sys.stderr)
    elif event.type == "retry":
        print(
            f"[retry] attempt {data['attempt']} failed: {data['reason']}; "
            f"waiting {data['delay']:.1f}s",
            file=sys.stderr,
        )
    elif event.type == "turn_finished":
        print(
            f"\n[turn {data['turn']}] stop={data['stop_reason']} in={data['input_tokens']} "
            f"out={data['output_tokens']} cache_read={data['cache_read_tokens']} "
            f"cache_write={data['cache_creation_tokens']}",
            file=sys.stderr,
        )


def show_prompt(harness: PromptedHarness) -> None:
    for block in harness.system_blocks:
        marker = "  <-- cache breakpoint" if block.cache_breakpoint else ""
        print(f"===== {block.label}{marker}\n{block.text}\n")
    print("tools:", ", ".join(sorted(t.name for t in harness.registry.tools)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the prompted harness.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--budget", type=int, default=100_000, help="total token budget")
    parser.add_argument("--show-prompt", action="store_true", help="print the prompt and exit")
    parser.add_argument("task", nargs="?", default="")
    args = parser.parse_args(argv)

    workspace = Workspace.at(args.workspace)
    harness = build_prompted_harness(
        RetryingModel(CachedAnthropicModel()),
        workspace,
        RuleBasedPolicy(workspace, read_tools=READ_TOOLS),
        approver=terminal_approver,
        pre_tool=(block_protected_writes,),
        post_tool=(python_syntax_note,),
        max_turns=args.max_turns,
        token_budget=args.budget,
        on_event=print_event,
    )
    if args.show_prompt or not args.task:
        show_prompt(harness)
        return 0
    try:
        result = harness.run(args.task)
    except (RuntimeError, ModelCallError) as exc:  # missing key, or retries ran out
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"\nstop={result.stop_reason} turns={result.turns} tokens={result.total_tokens}")
    return 0 if result.stop_reason == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
```

### `tests/test_instructions.py`

```python
"""Instruction discovery: parents, order, imports, cycles, depth, and the cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.environment import find_git_root
from harness.instructions import (
    MAX_IMPORT_DEPTH,
    config_dir,
    instruction_paths,
    load_instructions,
    read_with_imports,
    search_dirs,
)


@pytest.fixture
def repo(tmp_path) -> Path:
    """tmp/repo (.git) / pkg / app  <- workspace root; tmp/AGENTS.md is above the repo."""
    (tmp_path / "AGENTS.md").write_text("ABOVE THE REPO\n")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "pkg" / "app").mkdir(parents=True)
    (root / "AGENTS.md").write_text("repo rules\n")
    (root / "pkg" / "CLAUDE.md").write_text("pkg rules\n")
    (root / "pkg" / "app" / "AGENTS.md").write_text("app rules\n")
    (root / "pkg" / "app" / "CLAUDE.md").write_text("app claude rules\n")
    return root


@pytest.fixture
def user_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "user-config"
    path.mkdir()
    (path / "AGENTS.md").write_text("user rules\n")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(path))
    return path


def test_finds_parents_and_orders_general_to_specific(repo, user_dir):
    app = repo / "pkg" / "app"
    paths = instruction_paths(app, config_dir())
    assert paths == (
        user_dir / "AGENTS.md",
        repo / "AGENTS.md",
        repo / "pkg" / "CLAUDE.md",
        app / "AGENTS.md",
        app / "CLAUDE.md",
    )
    text = load_instructions(app, config_dir()).render()
    order = [text.index(s) for s in ("user rules", "repo rules", "pkg rules", "app rules")]
    assert order == sorted(order)
    assert "ABOVE THE REPO" not in text  # stopped at the git root
    assert f'<instructions path="{repo / "pkg" / "CLAUDE.md"}">\npkg rules\n</instructions>' in text


def test_git_file_marks_the_root_too(tmp_path):
    (tmp_path / "AGENTS.md").write_text("above\n")
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".git").write_text("gitdir: /somewhere/else\n")
    assert find_git_root(root) == root
    assert instruction_paths(root, None) == ()


def test_without_a_repo_the_walk_reaches_the_filesystem_root(tmp_path):
    if find_git_root(tmp_path) is not None:
        pytest.skip("temp dir is inside a git repo on this machine")
    ws = tmp_path / "a" / "b"
    ws.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("found above\n")
    dirs = search_dirs(ws)
    assert dirs[0] == ws and dirs[-1] == Path(ws.anchor)
    assert tmp_path / "AGENTS.md" in instruction_paths(ws, None)


def test_symlinked_claude_md_is_included_once(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("rules\n")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")
    assert instruction_paths(tmp_path, None) == (tmp_path / "AGENTS.md",)


def test_imports_resolve_relative_to_the_importing_file(tmp_path):
    (tmp_path / "docs" / "deep").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("top\n@docs/style.md\nend\n")
    (tmp_path / "docs" / "style.md").write_text("style\n  @deep/naming.md\n")
    (tmp_path / "docs" / "deep" / "naming.md").write_text("naming\n")
    assert read_with_imports(tmp_path / "AGENTS.md") == "top\nstyle\nnaming\nend"


def test_import_cycles_stop(tmp_path):
    (tmp_path / "a.md").write_text("A\n@b.md\n")
    (tmp_path / "b.md").write_text("B\n@a.md\n")
    text = read_with_imports(tmp_path / "a.md")
    assert text == "A\nB\n[import skipped: a.md would create a cycle]"


def test_import_depth_is_limited(tmp_path):
    count = MAX_IMPORT_DEPTH + 2
    for i in range(count):
        nxt = f"\n@f{i + 1}.md" if i + 1 < count else ""
        (tmp_path / f"f{i}.md").write_text(f"level {i}{nxt}\n")
    text = read_with_imports(tmp_path / "f0.md")
    assert f"level {MAX_IMPORT_DEPTH}" in text
    assert f"level {MAX_IMPORT_DEPTH + 1}" not in text
    assert f"[import skipped: f{MAX_IMPORT_DEPTH + 1}.md is past the depth limit" in text


def test_missing_and_non_markdown_imports(tmp_path):
    (tmp_path / "AGENTS.md").write_text("@nope.md\n@secrets.env\nemail me @ home\n")
    text = read_with_imports(tmp_path / "AGENTS.md")
    assert text == "[import not found: nope.md]\n@secrets.env\nemail me @ home"


def test_cap_keeps_most_specific_and_says_what_was_dropped(repo, user_dir):
    app = repo / "pkg" / "app"
    (repo / "AGENTS.md").write_text("R" * 500)
    result = load_instructions(app, user_dir, cap_chars=100)
    kept = [f.path for f in result.files]
    assert kept == [repo / "pkg" / "CLAUDE.md", app / "AGENTS.md", app / "CLAUDE.md"]
    # The small user file is dropped too: the kept set stays contiguous.
    assert result.dropped == (user_dir / "AGENTS.md", repo / "AGENTS.md")
    text = result.render()
    assert "[note] Dropped to fit the 100-char instruction cap" in text
    assert str(repo / "AGENTS.md") in text and "RRRR" not in text and "user rules" not in text


def test_no_files_says_so(tmp_path):
    (tmp_path / ".git").mkdir()
    assert "No instruction files were found." in load_instructions(tmp_path, None).render()
```

### `tests/test_prompt.py`

```python
"""Skills index, load_skill, environment, and block order with the cache breakpoint."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import results_in, turn

from harness.environment import current_branch, detect_environment
from harness.models import ScriptedModel
from harness.permissions import RuleBasedPolicy
from harness.prompt import render_text, to_anthropic_system
from harness.prompted import build_prompted_harness, build_system_prompt
from harness.skills import parse_frontmatter, scan_skills, skill_dirs
from harness.tools import default_registry
from harness.types import ToolCall

DAY1, DAY2 = date(2026, 11, 2), date(2026, 11, 3)
BODY = "Step 1: run the tests.\nStep 2: SECRET-BODY-MARKER.\n"


def load_skill_call(name: str) -> ToolCall:
    """conftest.call() can't take a `name` argument, since that's its own first parameter."""
    return ToolCall(id=f"toolu_skill_{name}", name="load_skill", input={"name": name})


def write_skill(root, folder, name, description, body=BODY):
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}")


@pytest.fixture
def user_dir(tmp_path):
    path = tmp_path / "user-config"
    write_skill(path / "skills", "deploy", "deploy", "user-level deploy steps")
    write_skill(path / "skills", "commit", "commit", '"Write a conventional commit"')
    return path


@pytest.fixture
def repo_ws(ws, user_dir):
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n")
    (ws.root / "AGENTS.md").write_text("Use pytest.\n")
    write_skill(ws.root / ".skills", "deploy", "deploy", "workspace deploy steps")
    write_skill(ws.root / ".skills", "testing", "testing", "How to run the tests")
    return ws


def test_frontmatter_parser():
    fields, body = parse_frontmatter("---\nname: a\ndescription: 'b: c'\n---\nbody\n")
    assert fields == {"name": "a", "description": "b: c"} and body == "body\n"
    assert parse_frontmatter("no frontmatter\n") == ({}, "no frontmatter\n")


def test_index_is_sorted_and_workspace_wins_a_name_clash(repo_ws, user_dir):
    index = scan_skills(skill_dirs(repo_ws.root, user_dir))
    assert [s.name for s in index.skills] == ["commit", "deploy", "testing"]
    assert index.get("deploy").description == "workspace deploy steps"
    assert "- commit: Write a conventional commit" in index.render()


def test_prompt_has_the_index_but_no_bodies(repo_ws, user_dir):
    blocks, _ = build_system_prompt(repo_ws, default_registry(), user_dir, DAY1)
    text = render_text(blocks)
    assert "- testing: How to run the tests" in text
    assert "SECRET-BODY-MARKER" not in text


def test_load_skill_returns_bodies_only_for_indexed_names(repo_ws, user_dir):
    harness = build_prompted_harness(
        None,
        repo_ws,
        RuleBasedPolicy(repo_ws, read_tools=frozenset({"load_skill"})),
        user_dir=user_dir,
        today=DAY1,
    )
    ok = harness.execute(load_skill_call("testing"))
    assert not ok.is_error and ok.content == BODY
    for bad in ("nope", "../../etc/passwd", "testing/SKILL.md"):
        result = harness.execute(load_skill_call(bad))
        assert result.is_error and result.content.startswith(f"unknown skill {bad!r}")


def test_block_order_and_breakpoint_on_the_last_stable_block(repo_ws, user_dir):
    blocks, _ = build_system_prompt(repo_ws, default_registry(), user_dir, DAY1)
    labels = [b.label for b in blocks]
    assert labels == ["base", "environment", "skills", "instructions", "session"]
    assert [b.label for b in blocks if b.cache_breakpoint] == ["instructions"]
    cut = labels.index("instructions") + 1
    before, after = render_text(blocks[:cut]), render_text(blocks[cut:])
    assert "2026-11-02" in after and "feat/x" in after
    assert "2026-11-02" not in before and "feat/x" not in before
    assert str(repo_ws.root) in before and "Use pytest." in before


def test_cached_prefix_is_identical_across_days_and_branches(repo_ws, user_dir):
    first, _ = build_system_prompt(repo_ws, default_registry(), user_dir, DAY1)
    (repo_ws.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    second, _ = build_system_prompt(repo_ws, default_registry(), user_dir, DAY2)
    assert first[:4] == second[:4]
    assert first[4] != second[4]


def test_anthropic_system_marks_only_the_breakpoint(repo_ws, user_dir):
    blocks, _ = build_system_prompt(repo_ws, default_registry(), user_dir, DAY1)
    system = to_anthropic_system(blocks)
    assert [b["type"] for b in system] == ["text"] * 5
    assert [("cache_control" in b) for b in system] == [False, False, False, True, False]
    assert system[3]["cache_control"] == {"type": "ephemeral"}
    assert to_anthropic_system("plain") == "plain"


def test_environment_reads_branch_detached_head_and_worktrees(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("0123456789abcdef0123\n")
    assert current_branch(main) == "detached at 0123456789ab"
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    (main / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/topic\n")
    env = detect_environment(wt, DAY1)
    assert (env.branch, env.today, env.git_root) == ("topic", "2026-11-02", str(wt))


def test_scripted_run_sends_blocks(repo_ws, user_dir):
    model = ScriptedModel([turn(load_skill_call("testing")), turn(text="ok")])
    harness = build_prompted_harness(
        model,
        repo_ws,
        RuleBasedPolicy(repo_ws, read_tools=frozenset({"load_skill"})),
        user_dir=user_dir,
        today=DAY1,
    )
    assert harness.run("go").stop_reason == "done"
    assert model.requests[0]["system"] == harness.system_blocks
    assert "load_skill" in [t["name"] for t in model.requests[0]["tools"]]
    assert results_in(model.requests[1])[0]["content"] == BODY
```

### `tests/test_retry.py`

```python
"""RetryingModel against fakes: no SDK, no real sleeping, seeded jitter."""

from __future__ import annotations

import random

import pytest
from conftest import turn

from harness.retry import ModelCallError, RetryingModel, RetryPolicy, parse_retry_after

OK = turn(text="ok")


class FlakyModel:
    """Raises the scripted errors in order, then returns OK. Counts calls."""

    def __init__(self, *errors: ModelCallError) -> None:
        self.errors = list(errors)
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return OK


class MaxRng(random.Random):
    """uniform() returns the top of the range, so delays show the backoff caps."""

    def uniform(self, a, b):
        return b


def status(code: int, retry_after: float | None = None) -> ModelCallError:
    return ModelCallError(f"status {code}", status=code, retry_after=retry_after)


def wrap(inner, policy=None, rng=None):
    sleeps: list[float] = []
    events: list = []
    model = RetryingModel(
        inner,
        policy or RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=30.0),
        sleep=sleeps.append,
        rng=rng or random.Random(7),
        on_event=events.append,
    )
    return model, sleeps, events


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504, 529])
def test_retries_retryable_statuses(code):
    inner = FlakyModel(status(code), status(code))
    model, sleeps, events = wrap(inner)
    assert model.complete("s", [], []) is OK
    assert inner.calls == 3 and len(sleeps) == 2


def test_retries_connection_errors_and_timeouts():
    inner = FlakyModel(ModelCallError("timed out", connection=True))
    model, sleeps, events = wrap(inner)
    assert model.complete("s", [], []) is OK
    assert events[0].data["reason"] == "connection error: timed out"


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_fail_immediately(code):
    inner = FlakyModel(status(code))
    model, sleeps, events = wrap(inner)
    with pytest.raises(ModelCallError) as info:
        model.complete("s", [], [])
    assert info.value.status == code
    assert inner.calls == 1 and sleeps == [] and events == []


def test_delays_grow_with_full_jitter():
    inner = FlakyModel(*[status(529)] * 4)
    model, sleeps, _ = wrap(inner, rng=MaxRng())
    model.complete("s", [], [])
    assert sleeps == [1.0, 2.0, 4.0, 8.0]  # the caps: base * 2**n

    seeded = FlakyModel(*[status(529)] * 4)
    model, sleeps, _ = wrap(seeded, rng=random.Random(42))
    model.complete("s", [], [])
    caps = [1.0, 2.0, 4.0, 8.0]
    assert all(0 <= s <= cap for s, cap in zip(sleeps, caps, strict=True))
    assert sleeps != caps  # jittered, not fixed


def test_max_delay_caps_each_wait():
    inner = FlakyModel(*[status(503)] * 4)
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=3.0)
    model, sleeps, _ = wrap(inner, policy=policy, rng=MaxRng())
    model.complete("s", [], [])
    assert sleeps == [1.0, 2.0, 3.0, 3.0]


def test_retry_after_is_honored():
    inner = FlakyModel(status(429, retry_after=7.0))
    model, sleeps, events = wrap(inner, rng=MaxRng())
    model.complete("s", [], [])
    assert sleeps == [7.0]
    assert events[0].data == {"attempt": 1, "delay": 7.0, "reason": "rate limited (429)"}


def test_attempt_cap_holds():
    inner = FlakyModel(*[status(529)] * 10)
    model, sleeps, events = wrap(inner, policy=RetryPolicy(max_attempts=3))
    with pytest.raises(ModelCallError) as info:
        model.complete("s", [], [])
    assert inner.calls == 3 and len(sleeps) == 2 and len(events) == 2
    assert "gave up after 3 attempt(s)" in info.value.__notes__[0]


def test_total_wait_cap_gives_up_early():
    inner = FlakyModel(status(429, retry_after=50.0), status(429, retry_after=50.0))
    policy = RetryPolicy(max_attempts=10, max_total_wait=60.0)
    model, sleeps, _ = wrap(inner, policy=policy)
    with pytest.raises(ModelCallError):
        model.complete("s", [], [])
    assert sleeps == [50.0] and inner.calls == 2


def test_retry_events_carry_attempt_delay_reason():
    inner = FlakyModel(status(529), status(503))
    model, sleeps, events = wrap(inner, rng=MaxRng())
    model.complete("s", [], [])
    assert [e.type for e in events] == ["retry", "retry"]
    assert [(e.data["attempt"], e.data["delay"]) for e in events] == [(1, 1.0), (2, 2.0)]
    assert [e.data["reason"] for e in events] == ["overloaded (529)", "HTTP 503: status 503"]


def test_parse_retry_after():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert parse_retry_after("-1") is None
```

### `tests/test_anthropic_model.py`

```python
"""CachedAnthropicModel with a fake SDK client, and SDK error translation.

The translation tests build real anthropic exception objects, so they skip
when the SDK isn't installed. Everything else runs without it.
"""

from __future__ import annotations

import random
from types import SimpleNamespace as NS

import pytest

from harness.anthropic_model import CachedAnthropicModel, translate_sdk_error
from harness.prompt import PromptBlock
from harness.retry import ModelCallError, RetryingModel

BLOCKS = (
    PromptBlock("base", "be helpful"),
    PromptBlock("instructions", "rules", cache_breakpoint=True),
    PromptBlock("session", "date"),
)
TOOLS = [{"name": "write_file"}, {"name": "load_skill"}, {"name": "read_file"}]


def response(text="ok", cache_read=0, cache_write=0):
    usage = NS(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=usage)


class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.kwargs: list[dict] = []

    def _next(self, kwargs):
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create(self, **kwargs):
        return self._next(kwargs)

    def stream(self, **kwargs):
        return self._next(kwargs)


def model_with(*outcomes):
    messages = FakeMessages(*outcomes)
    return CachedAnthropicModel(model="m-test", client=NS(messages=messages)), messages


def test_blocks_become_system_list_and_tools_are_sorted():
    model, messages = model_with(response(cache_read=900, cache_write=0))
    turn = model.complete(BLOCKS, [{"role": "user", "content": "hi"}], TOOLS)
    sent = messages.kwargs[0]
    assert sent["system"] == [
        {"type": "text", "text": "be helpful"},
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "date"},
    ]
    assert [t["name"] for t in sent["tools"]] == ["load_skill", "read_file", "write_file"]
    assert TOOLS[0]["name"] == "write_file"  # caller's list not reordered in place
    assert (turn.cache_read_tokens, turn.cache_creation_tokens) == (900, 0)


def test_stream_emits_text_deltas_and_returns_the_final_message():
    model, _ = model_with(FakeStream(["Hel", "lo"], response("Hello", cache_write=1200)))
    events: list = []
    turn = model.stream(BLOCKS, [], TOOLS, events.append)
    assert [(e.type, e.data["text"]) for e in events] == [
        ("text_delta", "Hel"),
        ("text_delta", "lo"),
    ]
    assert turn.text == "Hello" and turn.cache_creation_tokens == 1200


def test_missing_cache_fields_default_to_zero():
    plain = NS(
        content=[NS(type="text", text="ok")],
        stop_reason="end_turn",
        usage=NS(input_tokens=1, output_tokens=1),
    )
    model, _ = model_with(plain)
    assert model.complete("sys", [], []).cache_read_tokens == 0


# --- SDK error translation (needs the anthropic package) ---------------------


def fake_response(code, headers=None):
    return NS(status_code=code, headers=headers or {}, request=None)


def sdk_errors():
    anthropic = pytest.importorskip("anthropic")

    def status(cls, code, headers=None, body=None):
        return cls(f"error {code}", response=fake_response(code, headers), body=body)

    return anthropic, status


@pytest.mark.parametrize(
    ("name", "code"),
    [("RateLimitError", 429), ("InternalServerError", 500), ("APIStatusError", 529)],
)
def test_retryable_sdk_errors(name, code):
    anthropic, status = sdk_errors()
    mapped = translate_sdk_error(status(getattr(anthropic, name), code))
    assert isinstance(mapped, ModelCallError) and mapped.status == code and mapped.retryable


@pytest.mark.parametrize(
    ("name", "code"),
    [("BadRequestError", 400), ("AuthenticationError", 401), ("PermissionDeniedError", 403)],
)
def test_non_retryable_sdk_errors(name, code):
    anthropic, status = sdk_errors()
    mapped = translate_sdk_error(status(getattr(anthropic, name), code))
    assert mapped.status == code and not mapped.retryable


def test_connection_and_timeout_errors_are_retryable():
    anthropic, _ = sdk_errors()
    for exc in (
        anthropic.APIConnectionError(request=None),
        anthropic.APITimeoutError(request=None),
    ):
        mapped = translate_sdk_error(exc)
        assert mapped.connection and mapped.retryable


def test_retry_after_header_is_parsed():
    anthropic, status = sdk_errors()
    mapped = translate_sdk_error(status(anthropic.RateLimitError, 429, {"retry-after": "3"}))
    assert mapped.retry_after == 3.0


def test_mid_stream_overload_maps_by_error_type():
    anthropic, status = sdk_errors()
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    mapped = translate_sdk_error(status(anthropic.APIStatusError, 200, body=body))
    assert mapped.status == 529 and mapped.retryable


def test_unrelated_exceptions_pass_through():
    sdk_errors()
    assert translate_sdk_error(ValueError("bug")) is None
    model, _ = model_with(ValueError("bug"))
    with pytest.raises(ValueError):
        model.complete("s", [], [])


def test_adapter_and_retry_wrapper_together():
    anthropic, status = sdk_errors()
    model, messages = model_with(
        status(anthropic.APIStatusError, 529),
        status(anthropic.RateLimitError, 429, {"retry-after": "2"}),
        response("finally"),
    )
    sleeps: list[float] = []
    retrying = RetryingModel(model, sleep=sleeps.append, rng=random.Random(1))
    assert retrying.complete(BLOCKS, [], TOOLS).text == "finally"
    assert len(messages.kwargs) == 3 and sleeps[1] == 2.0 and 0 <= sleeps[0] <= 1.0

    model, messages = model_with(status(anthropic.BadRequestError, 400))
    with pytest.raises(ModelCallError) as info:
        RetryingModel(model, sleep=sleeps.append).complete(BLOCKS, [], TOOLS)
    assert info.value.status == 400 and len(messages.kwargs) == 1
    assert isinstance(info.value.__cause__, anthropic.BadRequestError)
```

### `tests/test_prompted.py`

```python
"""Parallel read-only calls, sequential mixed batches, and the event stream."""

from __future__ import annotations

import threading
import time

from conftest import call, results_in, turn

from harness.models import ScriptedModel
from harness.permissions import RuleBasedPolicy
from harness.prompted import PromptedHarness
from harness.retry import ModelCallError, RetryingModel
from harness.tools import Tool, default_registry

READS = frozenset({"read_file", "list_dir", "slow_read"})


class SlowTools:
    """A slow read-only tool and a slow write tool that log (name, start, end)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spans: list[tuple[str, float, float]] = []

    def _run(self, label: str, seconds: float) -> str:
        start = time.monotonic()
        time.sleep(seconds)
        with self.lock:
            self.spans.append((label, start, time.monotonic()))
        return f"{label} done"

    def registry(self):
        schema = {
            "type": "object",
            "properties": {"label": {"type": "string"}, "seconds": {"type": "number"}},
            "required": ["label", "seconds"],
        }

        def run(ws, args):
            return self._run(args["label"], args["seconds"])

        return (
            default_registry()
            .register(Tool("slow_read", "slow read", schema, run, read_only=True))
            .register(Tool("slow_write", "slow write", schema, run))
        )


def build(ws, *turns, **overrides):
    model = ScriptedModel(turns)
    harness = PromptedHarness(
        model=model,
        workspace=ws,
        policy=RuleBasedPolicy(ws, read_tools=READS),
        approver=lambda c, r: True,
        **overrides,
    )
    return harness, model


def test_three_read_only_calls_run_concurrently_in_call_order(ws):
    slow = SlowTools()
    # Different durations, so they finish in a different order than called.
    durations = {"a": 0.6, "b": 0.2, "c": 0.4}  # sequential: 1.2s; parallel: about 0.6s
    calls = [call("slow_read", label=n, seconds=s) for n, s in durations.items()]
    harness, model = build(ws, turn(*calls), turn(text="ok"), registry=slow.registry())
    start = time.monotonic()
    harness.run("read three")
    elapsed = time.monotonic() - start
    assert [s[0] for s in slow.spans] == ["b", "c", "a"]  # finish order
    blocks = results_in(model.requests[1])
    assert [b["tool_use_id"] for b in blocks] == [c.id for c in calls]
    assert [b["content"] for b in blocks] == ["a done", "b done", "c done"]
    latest_start = max(s[1] for s in slow.spans)
    earliest_end = min(s[2] for s in slow.spans)
    assert latest_start < earliest_end  # all three were running at once
    assert elapsed < sum(durations.values()) - 0.2  # generous margin against slow CI


def test_a_batch_with_a_write_runs_sequentially_in_order(ws):
    slow = SlowTools()
    calls = [
        call("slow_read", label="r1", seconds=0.1),
        call("slow_write", label="w", seconds=0.1),
        call("slow_read", label="r2", seconds=0.1),
    ]
    harness, model = build(ws, turn(*calls), turn(text="ok"), registry=slow.registry())
    harness.run("mixed")
    assert [s[0] for s in slow.spans] == ["r1", "w", "r2"]
    for (_, _, prev_end), (_, next_start, _) in zip(slow.spans, slow.spans[1:], strict=False):
        assert next_start >= prev_end  # no overlap
    assert [b["content"] for b in results_in(model.requests[1])] == ["r1 done", "w done", "r2 done"]


def test_unknown_tool_in_a_batch_forces_sequential(ws):
    harness, model = build(
        ws, turn(call("read_file", path="README.md"), call("nope")), turn(text="ok")
    )
    assert not harness._is_read_only(call("nope"))
    harness.run("go")
    blocks = results_in(model.requests[1])
    assert blocks[0]["content"] == "# demo\n" and blocks[1]["is_error"]


def test_event_stream_for_a_scripted_run(ws):
    events: list = []
    harness, _ = build(
        ws,
        turn(call("list_dir", path="src"), text="Looking."),
        turn(call("write_file", path="src/new.py", content="x = 1\n")),
        turn(text="Done."),
        on_event=events.append,
    )
    assert harness.run("go").stop_reason == "done"
    assert [
        (e.type, e.data.get("name") or e.data.get("text") or e.data.get("turn")) for e in events
    ] == [
        ("text_delta", "Looking."),
        ("turn_finished", 1),
        ("tool_call_started", "list_dir"),
        ("tool_call_finished", "list_dir"),
        ("turn_finished", 2),
        ("tool_call_started", "write_file"),
        ("tool_call_finished", "write_file"),
        ("text_delta", "Done."),
        ("turn_finished", 3),
    ]
    assert events[1].data["stop_reason"] == "tool_use" and events[1].data["tool_calls"] == 1
    assert events[-1].data["stop_reason"] == "end_turn"


def test_parallel_events_start_in_call_order(ws):
    events: list = []
    calls = [call("read_file", path="README.md"), call("list_dir", path=".")]
    harness, _ = build(ws, turn(*calls), turn(text="ok"), on_event=events.append)
    harness.run("go")
    started = [e.data["id"] for e in events if e.type == "tool_call_started"]
    finished = {e.data["id"] for e in events if e.type == "tool_call_finished"}
    assert started == [c.id for c in calls] and finished == set(started)


def test_retry_events_reach_the_harness_sink(ws):
    class FlakyOnce:
        def __init__(self):
            self.inner = ScriptedModel([turn(text="ok")])
            self.failed = False

        def complete(self, system, messages, tools):
            if not self.failed:
                self.failed = True
                raise ModelCallError("busy", status=529)
            return self.inner.complete(system, messages, tools)

    events: list = []
    model = RetryingModel(FlakyOnce(), sleep=lambda s: None)
    harness = PromptedHarness(
        model=model, workspace=ws, policy=RuleBasedPolicy(ws), on_event=events.append
    )
    assert harness.run("go").stop_reason == "done"
    assert [e.type for e in events] == ["retry", "text_delta", "turn_finished"]
    assert events[0].data["reason"] == "overloaded (529)"
```

## Meeting the acceptance criteria

- **Instruction discovery finds files in parents, orders general to specific,
  resolves `@imports`, stops cycles, and enforces the size cap with a note.**
  `test_finds_parents_and_orders_general_to_specific` builds
  `repo/.git`, `repo/pkg`, and `repo/pkg/app` with a file above the repo too.
  It checks the exact path order (user, repo, pkg, app `AGENTS.md`, app
  `CLAUDE.md`), the labeled blocks, and that the file above the git root is
  not read. `test_git_file_marks_the_root_too` covers a worktree-style `.git`
  file. `test_without_a_repo_the_walk_reaches_the_filesystem_root` covers the
  no-repo case. `test_imports_resolve_relative_to_the_importing_file`,
  `test_import_cycles_stop`, `test_import_depth_is_limited`, and
  `test_missing_and_non_markdown_imports` cover imports.
  `test_cap_keeps_most_specific_and_says_what_was_dropped` checks the kept
  files, the dropped paths, and the note.
- **The system prompt contains the skills index but no skill bodies, and
  `load_skill` returns a body only for indexed names.**
  `test_prompt_has_the_index_but_no_bodies` searches the rendered prompt for
  a marker that only appears in skill bodies.
  `test_load_skill_returns_bodies_only_for_indexed_names` loads a real skill,
  then rejects an unknown name, a traversal path, and a file path.
  `test_scripted_run_sends_blocks` runs `load_skill` through the full loop.
- **The cache breakpoint sits on the last stable block, and per-session
  values come after it.**
  `test_block_order_and_breakpoint_on_the_last_stable_block` checks the
  order, that `instructions` holds the only breakpoint, and that the date and
  branch appear only after it.
  `test_cached_prefix_is_identical_across_days_and_branches` shows the
  prefix doesn't change when they do.
  `test_anthropic_system_marks_only_the_breakpoint` and
  `test_blocks_become_system_list_and_tools_are_sorted` check what the SDK
  receives.
- **Retries happen on 429/529/5xx with growing jittered delays and respect
  `retry-after`; 400/401 fail immediately; the attempt cap holds.**
  `test_retries_retryable_statuses` (429, 500, 502, 503, 504, 529),
  `test_retries_connection_errors_and_timeouts`,
  `test_client_errors_fail_immediately` (400, 401, 403, 404, no sleep, no
  event), `test_delays_grow_with_full_jitter` (caps 1, 2, 4, 8 with a max-RNG,
  and seeded jitter within those caps), `test_retry_after_is_honored`,
  `test_attempt_cap_holds`, and `test_total_wait_cap_gives_up_early`.
  `test_retryable_sdk_errors`, `test_non_retryable_sdk_errors`,
  `test_mid_stream_overload_maps_by_error_type`, and
  `test_adapter_and_retry_wrapper_together` build real `anthropic`
  exceptions and run them through the adapter. They skip if the SDK isn't
  installed.
- **Three read-only calls run concurrently and return in call order; a mix
  of read and write runs sequentially.**
  `test_three_read_only_calls_run_concurrently_in_call_order` uses a slow
  read-only tool with durations 0.6, 0.2, and 0.4 seconds. They finish in the
  order b, c, a. Results still come back a, b, c. The test checks that all
  three time spans overlap, and that the run took well under the 1.2 seconds
  a sequential run needs. The overlap check is the main signal. The timing
  check has a wide margin so a slow CI machine doesn't fail it.
  `test_a_batch_with_a_write_runs_sequentially_in_order` checks that no two
  spans overlap and the order is kept.
- **The event stream for a scripted run contains the expected events in
  order.** `test_event_stream_for_a_scripted_run` asserts the full sequence
  for a three-turn run. `test_retry_events_reach_the_harness_sink` shows a
  retry event arriving at the harness callback before the turn's text.
- **The live run shows a cache hit on the second call.** This needs an API
  key, so it isn't in `pytest`. The commands are under Verification. The
  reference solution's live check has not been run: it was written without an
  API key. Run it yourself and keep the output.

## Common pitfalls

- **Per-session values before the breakpoint.** A date, a branch name, a
  timestamp, or a random ID in an early block changes the prefix every
  session, so nothing is ever read from the cache. Put them last.
- **Unordered tools.** Tools come first in the cached prefix. If the order
  depends on dict iteration over a set, plugin load order, or when a tool was
  registered, the prefix changes and the cache misses. Sort them.
- **A prefix that's too short to cache.** Below the model's minimum
  cacheable length the API just doesn't cache, and returns no error. If
  `cache_read_input_tokens` stays at 0, check the length first.
- **Two retry layers.** The SDK's built-in retries plus your own multiply
  the attempts and hide the inner ones from your events. Set
  `max_retries=0` on the client.
- **Retrying 400s.** A bad request fails the same way every time. Retrying
  it wastes time and quota. Retry only on 429, 5xx (including 529), and
  connection errors.
- **Backoff without jitter.** If many clients fail at once and all wait 1,
  2, 4 seconds, they all retry at once too. Full jitter spreads them out.
- **Ignoring `retry-after`, or capping it.** The server knows when it will
  have capacity. Wait as long as it says, and give up if that's past your
  total budget.
- **Retrying a stream that already emitted text.** If a stream fails
  halfway, the retry starts the answer again and the UI shows the first part
  twice. The `retry` event is the UI's cue to discard the partial text.
- **Mid-stream errors look like success.** The HTTP status of a stream is
  200 even if an `overloaded_error` arrives later. Map on the error type too,
  or that overload is treated as a permanent failure.
- **Parallel writes.** Two concurrent `edit_file` calls on the same file
  both read the old content, and the second write discards the first. Two
  commands can also race on the same files or ports. Only parallelize calls
  that are marked read-only, and treat unknown tools as not read-only.
- **Results in completion order.** Collecting results with `as_completed`
  is easy, but the order then changes from run to run. Results are matched
  by `tool_use_id`, but call order makes runs reproducible and transcripts
  readable. `pool.map` keeps input order.
- **Approval prompts in parallel.** A read-only tool that needs approval
  would prompt from several threads at once. The CLI's policy allows the
  read-only tools, so this doesn't happen. Keep it that way, or serialize the
  approver.
- **Treating instruction imports as harmless.** An `@` line in a cloned
  repo's `AGENTS.md` could pull another file into the prompt, and the prompt
  goes to the model provider. This solution imports only `.md` files. A
  stricter harness would also keep imports inside the git root or the user
  config directory.
- **Cached tokens and the budget.** `input_tokens` does not include cache
  reads or writes. The exercise-01 budget counts `input_tokens +
  output_tokens`, so it undercounts cached runs. Add the cache fields if the
  budget should cover them.

## Verification

Start from the exercise-01 files, apply the three changes, add the new files
above, then:

```bash
cd exercise-03-prompt-assembly-and-model-layer
python3.11 -m venv .venv && . .venv/bin/activate
pip install pytest

# Offline: ScriptedModel, fake SDK clients, injected sleep and RNG.
pytest -q

# Optional: with the SDK installed, the error-translation tests run too.
pip install anthropic
pytest -q

# Print the assembled prompt for any workspace. No model call, no key needed.
python -m harness.prompted_cli --workspace . --show-prompt
```

Expected: without the SDK, `pytest -q` reports `78 passed, 11 skipped`. With
it, `89 passed` (32 from exercise-01, 57 new). `--show-prompt` prints the five
blocks, marks `instructions` as the cache breakpoint, and lists the tools,
including `load_skill`.

**Live check (task 6).** Run the same short task twice within the cache
lifetime. The default lifetime is a few minutes (see the prompt caching
docs), so run them back to back. The workspace's `AGENTS.md` is padded so the
cached prefix (tools plus system) is clearly longer than the model's minimum
cacheable length. Check that minimum for your model in the docs.

```bash
export ANTHROPIC_API_KEY=...            # never commit this; set a spend cap
export HARNESS_MODEL=claude-sonnet-4-5  # check the current models list

mkdir -p /tmp/cache-ws && git -C /tmp/cache-ws init -q
echo "# demo project" > /tmp/cache-ws/README.md
python3 - <<'PY'
from pathlib import Path
rule = "keep functions short, name things clearly, and test behavior, not implementation."
lines = [f"- Rule {i}: {rule}" for i in range(400)]  # several thousand tokens
Path("/tmp/cache-ws/AGENTS.md").write_text("# Project rules\n" + "\n".join(lines) + "\n")
PY

python -m harness.prompted_cli --workspace /tmp/cache-ws --show-prompt | head -5
for run in 1 2; do
  python -m harness.prompted_cli --workspace /tmp/cache-ws --max-turns 3 \
    "Read README.md and tell me what it says." 2>&1 | grep '^\[turn 1\]'
done
```

Expected: the first run's `[turn 1]` line shows `cache_read=0` and a
non-zero `cache_write`. The second run's `[turn 1]` line shows a non-zero
`cache_read`, about the size of the first run's `cache_write`. If
`cache_read` stays at 0 on the second run, check three things: the runs were
within the cache lifetime, the prefix is above the minimum length, and nothing
before the breakpoint changed between the runs.

This live check was not run for the reference solution, because it was
written without an API key. The offline tests cover the request shape
(`cache_control` on the breakpoint block, sorted tools) and the usage mapping
(`cache_read_input_tokens` to `cache_read_tokens`), but only a live run proves
the cache hit.
