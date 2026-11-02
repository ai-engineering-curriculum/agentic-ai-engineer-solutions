# mod-209-building-custom-harnesses/exercise-06-observability-replay-and-shipping — Solution

## Approach

This solution extends the
[exercise-01 reference harness](../exercise-01-build-a-minimal-harness/README.md).
It changes two exercise-01 files and adds six modules beside them.

**One seam in the loop.** Everything in this exercise watches the same thing:
what the loop did, step by step. So the loop gets one new field,
`on_event: Callable[[str, dict], None] | None`, and calls it at six points:
`run_started`, `model_request`, `model_response`, `tool_call`, `tool_result`,
and `run_finished`. This hook plays the same role as exercise-03's event
callback. If you built that one, use it here and skip the `loop.py` change.
The solution doesn't depend on exercise-03, so it adds its own.

`loop.py` changes in three places:

1. `run` emits `run_started` and `run_finished`. If the loop raises, it emits
   `run_finished` with `stop_reason: "error"` and re-raises. The log then
   ends cleanly even when the run doesn't.
2. The model call moves into `_call_model`, which emits the request and
   response events around it.
3. `execute` splits into `_gate` (lookup, schema, `pre_tool` hooks,
   permission) and the run itself. The split is needed because a `tool_call`
   event must carry the permission decision and the hook outcome, and must be
   sent *before* the tool runs. Otherwise a trace or a stream-json reader
   would see the result before the call. `_permission_denial` becomes
   `_permission`, which returns a label (`allow`, `deny`, `ask_approved`,
   `ask_denied`) along with the reason. The error strings the model sees
   are unchanged, so every exercise-01 test still passes.

The loop sends plain dicts and knows nothing about files, spans, or run ids.

**The Recorder is the callback.** `events.Recorder(run_id, sinks)` is what
you pass as `on_event`. For each event it:

1. redacts the data (a new structure; the loop's data is never changed),
2. stamps it with `ts`, `run_id`, and a monotonic `seq`,
3. passes the frozen `Event` to every sink in order.

Redaction happens once, before any sink. The JSONL log, the spans, and the
CLI's stream-json output all get the same clean data, so a new sink can't
leak a secret by forgetting to redact. A sink is any `Callable[[Event], None]`.
There are five: `JsonlSink`, `EventCollector`, `SpanSink`, the stream-json
printer, and the interactive console view.

**The log.** One file per run, at `~/.local/state/harness/runs/<run_id>.jsonl`
by default (it honors `XDG_STATE_HOME`; override it with `log_dir` in config,
`HARNESS_LOG_DIR`, or `--log-dir`). `JsonlSink` opens the file, appends one
line, flushes, and closes it for every event. That costs a syscall or two per
event, which is nothing next to a model call. In return, a crash leaves every
line up to the crash on disk. Lines are flat: the envelope fields first, then
the event's own fields, so `jq 'select(.type=="tool_call") | .name'` works.

What each event carries:

| Event | Fields |
|---|---|
| `run_started` | model, workspace, tool names, limits, hook names, harness version |
| `model_request` | turn, message count, estimated tokens (characters ÷ 4) |
| `model_response` | turn, stop reason, text, tool calls (id and name), usage |
| `tool_call` | id, name, input, permission decision, hook outcome |
| `tool_result` | id, name, is_error, size, duration in ms, first 1,000 chars |
| `run_finished` | stop reason, turns, total tokens, or the error text |

`tool_result` keeps a preview, not the whole output, so logs stay small. The
preview is also what makes the output-side redaction test meaningful.

**Redaction** follows the starter: blank the value of any key named like a
secret, at any depth, and mask strings that look like common key formats
(`sk-…`, GitHub tokens, AWS key ids, Slack tokens, `Bearer …`). Key names are
compared after lowercasing and turning `-` into `_`, so `X-Api-Key` matches.
It only changes what gets *recorded*. The model and the tools still see the
real values, which the test checks: the planted key is redacted in the log
and present in the file the agent wrote.

**Spans.** `tracing.SpanSink` is just another sink. It opens a `harness.run`
span on `run_started`, a `chat <model>` child on `model_request` (closed on
`model_response`), and an `execute_tool <name>` child on `tool_call` (closed
on `tool_result`). Children get the run span as an explicit parent, so the
tree is right even if other code changes the current context. Model spans
carry `gen_ai.operation.name = "chat"`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and
`gen_ai.response.finish_reasons`. Tool spans carry `gen_ai.tool.name`, the
permission decision, the hook outcome, and an ERROR status when the result
is an error.

The OpenTelemetry GenAI semantic conventions are still evolving. Attribute
names have been renamed before (the provider attribute, for one) and the
content attributes have changed shape. Check the current spec before you
build dashboards or alerts on these names. Anything this harness invents is
under `harness.*`, so it can't collide with a future convention.

No prompt text, model output, or tool input/output goes on spans unless
`include_content` is set (`[tracing] include_content = true` or
`--trace-content`). Even the error status uses a fixed description, because
a tool's error message can quote file contents. Traces often go to shared
backends that more people can read than can read your run logs.

Tracing is off unless configured. `tracing.py` imports `opentelemetry` at the
top, and nothing else imports `tracing.py` except `main.make_tracing`, which
only does so when tracing is on. Without the package, the harness runs
normally. Turning tracing on without it is a config error (exit 2) that says
what to install. A test proves both by blocking every `opentelemetry` import
and re-importing the harness from scratch.

**Record and replay.** A cassette stores, for each model call, a fingerprint
of the request and the full response. The fingerprint is exactly what
`ReplayModel` checks: the tool schema names, the message count, the last
message's role, and its tool-result ids. Storing only that keeps hand-written
cassettes short and keeps message contents (source code, maybe secrets) out of
files you commit. `save_cassette` also runs the cassette through `redact`.

On each call `ReplayModel` builds the fingerprint of the real request, diffs
it against the recorded one, and either returns the recorded response or
raises `ReplayMismatch`. The message lists every difference, numbered by turn:

```text
turn 1: tool schema names differ: +run_cmd -run_command
turn 1: message count differs: recorded 1, got 2
turn 3: the harness made a model call the cassette does not have (it has 2)
```

`+name` is a tool this run sends that the recording didn't have; `-name` is
one that disappeared. Tool names are compared as sets, so reordering the
registry doesn't fail a test. Reordering does change the prompt prefix,
though. If you depend on prompt caching, compare the lists instead.

`RecordingModel` wraps any `ModelClient` and rewrites the cassette after
every call. It writes to a temp file and then calls `os.replace`, so a crash
never leaves half a cassette. `harness --record run.json …` turns any live
run into a regression test.

The two regression cassettes are hand-written, so no API key is needed.
`success.json` lists `src/`, reads `src/app.py`, and answers.
`denied_then_recovers.json` tries to write `NOTES.md` at the workspace root.
The rule-based policy says `ask` for that path, and with no approver the call
is denied. The model then writes `src/NOTES.md` instead, which the policy
allows. The responses use fixed tool ids (`toolu_s1`, `toolu_d1`), so the
tool-result ids the harness sends back are predictable.

**The CLI.** `main.py` is the `harness` console script. `python -m
harness.cli` from exercise-01 still works and is unchanged; `main.py` reuses
its `terminal_approver`.

- `-p` is a flag, and the prompt is positional, as in `claude -p`. Both
  `harness -p "fix it"` and `harness "fix it" -p` work.
- `text` prints the final answer only. `json` prints one object with
  `result`, `stop_reason`, `turns`, `usage`, `cost_usd`, `run_id`, and
  `is_error`. `stream-json` prints every event as a JSON line as it happens,
  then a last line with `"type": "result"` and the same fields as `json`, so a
  stream reader doesn't need a second call to get totals.
- Headless `ask` is denied by passing no approver to the loop. The model gets
  `permission denied: needs approval (…) and no approver is configured`.
  `--allow-ask` passes an approver that says yes. Interactive mode asks in
  the terminal.
- Exit codes come from the final stop reason: `done` → 0, `max_turns` /
  `budget` / `loop_detected` → 3, a crash (model error, replay mismatch) → 1.
  Problems found before the run starts → 2: a bad flag, unreadable or
  invalid config, a missing workspace, a missing cassette, or a live model
  with no `ANTHROPIC_API_KEY` or no SDK. Checking the key up front matters.
  Exercise-01's `AnthropicModel` only fails on the first call, which would
  look like a runtime error (1) instead of a config error (2).
- `is_error` is true for any stop other than `done`. A limit stop isn't a
  crash, but the task isn't known to be finished, and callers usually treat
  both cases the same. The exit code still tells them apart.
- `summarize` builds the result from the event records, not from
  `RunResult`. It's the same code that would read a log file back from disk,
  which is where a cost report across runs (a stretch goal) would start.

**Cost.** `cost_usd` comes from a `[prices."<model>"]` table in the config
file, in dollars per million input and output tokens. The harness ships no
prices. A built-in number goes stale without anyone noticing and quietly
under-reports spend. A model with no entry reports `cost_usd: null`, not
`0`. `harness.example.toml` has **placeholder** prices that are made up so
the arithmetic is easy to check. Replace them with current prices from your
provider's pricing page. If you use prompt caching, add cache read and
cache write prices too. Exercise-01's `ModelTurn` doesn't carry cache token
counts yet.

**Config precedence**, lowest to highest: built-in defaults, the config file
(`--config`, or `~/.config/harness/config.toml` if it exists), the
`HARNESS_MODEL` / `HARNESS_LOG_DIR` environment variables, then flags.
Unknown keys are errors, so a typo like `modle = …` fails loudly instead of
being ignored.

**Offline model.** `HARNESS_MODEL=replay:<cassette.json>` (or `--model
replay:…`) runs the CLI against a `ReplayModel`. The CLI tests use it, so
they need no key and no network, and they still check the requests the
harness sends. Exercise-01's `AnthropicModel` already reads `HARNESS_MODEL`
for the model name. The `replay:` prefix reuses that variable without
clashing with a real model name.

**Plugging into an eval runner.** Each trial becomes
`harness -p "<task>" --output-format json --workspace <fresh copy>`. Parse
the single JSON line, branch on the exit code, and score the workspace
afterward. `run_id` points at the full log when a trial fails.

## Reference implementation

Layout. Files marked *unchanged* are byte-for-byte the exercise-01 files; copy
them from the [exercise-01 solution](../exercise-01-build-a-minimal-harness/README.md).

```text
exercise-06-observability-replay-and-shipping/
├── harness/
│   ├── __init__.py      # unchanged
│   ├── types.py         # unchanged
│   ├── workspace.py     # unchanged
│   ├── schema.py        # unchanged
│   ├── models.py        # unchanged
│   ├── tools.py         # unchanged
│   ├── permissions.py   # unchanged
│   ├── hooks.py         # unchanged
│   ├── cli.py           # unchanged (main.py reuses its terminal approver)
│   ├── loop.py          # CHANGED: on_event hook, _call_model, _gate, _permission
│   ├── events.py        # NEW: redaction, Recorder, JSONL sink
│   ├── tracing.py       # NEW: OpenTelemetry SpanSink (imported only when tracing is on)
│   ├── replay.py        # NEW: cassettes, RecordingModel, ReplayModel
│   ├── config.py        # NEW: TOML config, precedence, price table
│   ├── report.py        # NEW: run summary, result object, exit codes
│   └── main.py          # NEW: the `harness` console script
├── tests/
│   ├── conftest.py      # unchanged
│   ├── test_tools.py    # unchanged
│   ├── test_loop.py     # unchanged
│   ├── test_models.py   # unchanged
│   ├── cassettes/
│   │   ├── success.json               # NEW (hand-written)
│   │   └── denied_then_recovers.json  # NEW (hand-written)
│   ├── test_events.py   # NEW
│   ├── test_tracing.py  # NEW
│   ├── test_replay.py   # NEW
│   └── test_main.py     # NEW
├── harness.example.toml # NEW: example config with placeholder prices
└── pyproject.toml       # CHANGED: build system, console script, tracing extra
```

The exercise-01 tests run here unchanged and still pass. The new code is
about 980 lines of package code (plus about 100 changed in `loop.py`) and 620
lines of tests.

### Changed files

`pyproject.toml` gains a `[build-system]` table and `[tool.setuptools]
packages`, so `pip install -e .` works and doesn't try to package `tests/`;
a `[project.scripts]` entry point; and a `tracing` extra. The version moves to
0.6.0.

### `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "minimal-harness"
version = "0.6.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
live = ["anthropic>=0.40"]
tracing = ["opentelemetry-sdk>=1.25"]
test = ["pytest>=8"]

[project.scripts]
harness = "harness.main:main"

[tool.setuptools]
packages = ["harness"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

`loop.py`, in full. The changes are described in the Approach section.

### `harness/loop.py`

```python
"""The agent loop and the per-call pipeline around it.

Per call: lookup -> schema check -> pre_tool hooks -> permission -> run ->
post_tool hooks. Every failure becomes an is_error tool result, so the model
reads what went wrong and the loop keeps going.

exercise-06 adds one seam: `on_event(type, data)`, called at each step of a
run. Logging, tracing, and CLI output all hang off it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
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
PREVIEW_CHARS = 1_000
EventHook = Callable[[str, dict[str, Any]], None]
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
class Gate:
    """What the pipeline decided before a tool runs. `error` set means it won't run."""

    error: str | None = None
    hook: str = "not_run"  # not_run | passed | blocked
    permission: str = "not_run"  # not_run | allow | deny | ask_approved | ask_denied


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
    on_event: EventHook | None = None

    def run(self, task: str) -> RunResult:
        first = {"role": "user", "content": task}
        self._emit("run_started", self._config_summary())
        try:
            result = self._loop(LoopState(messages=(first,), transcript=(first,)))
        except Exception as exc:  # record the crash, then let it propagate
            self._emit(
                "run_finished", {"stop_reason": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
            raise
        self._emit(
            "run_finished",
            {
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "total_tokens": result.total_tokens,
            },
        )
        return result

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event_type, data)

    def _config_summary(self) -> dict[str, Any]:
        return {
            "model": getattr(self.model, "model", type(self.model).__name__),
            "workspace": str(self.workspace.root),
            "tools": [t.name for t in self.registry.tools],
            "max_turns": self.max_turns,
            "token_budget": self.token_budget,
            "pre_tool": [getattr(h, "__name__", repr(h)) for h in self.pre_tool],
            "post_tool": [getattr(h, "__name__", repr(h)) for h in self.post_tool],
        }

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

    def _call_model(self, state: LoopState, tools: list[dict]) -> ModelTurn:
        n = state.turns + 1
        self._emit(
            "model_request",
            {
                "turn": n,
                "message_count": len(state.messages),
                "estimated_tokens": len(json.dumps(state.messages, default=str)) // 4,
            },
        )
        turn = self.model.complete(self.system, list(state.messages), tools)
        self._emit(
            "model_response",
            {
                "turn": n,
                "stop_reason": turn.stop_reason,
                "text": turn.text,
                "tool_calls": [{"id": c.id, "name": c.name} for c in turn.tool_calls],
                "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens},
            },
        )
        return turn

    def execute(self, call: ToolCall) -> ToolResult:
        tool, gate = self._gate(call)
        self._emit(
            "tool_call",
            {
                "id": call.id,
                "name": call.name,
                "input": call.input,
                "permission": gate.permission,
                "hook": gate.hook,
            },
        )
        started = time.monotonic()
        if tool is None or gate.error is not None:
            result = _error(call, gate.error or f"unknown tool {call.name!r}")
        else:
            result = self._post_tool_notes(call, self._invoke(tool, call))
        self._emit(
            "tool_result",
            {
                "id": call.id,
                "name": call.name,
                "is_error": result.is_error,
                "size": len(result.content),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "preview": result.content[:PREVIEW_CHARS],
            },
        )
        return result

    def _gate(self, call: ToolCall) -> tuple[Tool | None, Gate]:
        tool = self.registry.get(call.name)
        if tool is None:
            return None, Gate(error=f"unknown tool {call.name!r}")
        problems = validate(call.input, tool.input_schema)
        if problems:
            return None, Gate(error=f"invalid input for {call.name}: " + "; ".join(problems))
        blocked = self._pre_tool_block(call)
        if blocked is not None:
            return None, Gate(error=f"blocked by hook: {blocked}", hook="blocked")
        permission, denied = self._permission(call)
        if denied is not None:
            return None, Gate(f"permission denied: {denied}", "passed", permission)
        return tool, Gate(hook="passed", permission=permission)

    def _pre_tool_block(self, call: ToolCall) -> str | None:
        for hook in self.pre_tool:
            try:
                reason = hook(call)
            except Exception as exc:  # a broken guard fails closed
                return f"pre_tool hook {getattr(hook, '__name__', hook)!r} crashed: {exc}"
            if reason is not None:
                return reason
        return None

    def _permission(self, call: ToolCall) -> tuple[str, str | None]:
        """Return (decision label, denial reason or None)."""
        verdict = self.policy.decide(call)
        if verdict.decision == "allow":
            return "allow", None
        if verdict.decision == "deny":
            return "deny", verdict.reason
        if self.approver is None:
            return "ask_denied", f"needs approval ({verdict.reason}) and no approver is configured"
        if self.approver(call, verdict.reason):
            return "ask_approved", None
        return "ask_denied", f"the user declined ({verdict.reason})"

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

### New files

### `harness/events.py`

```python
"""Structured run events: redaction, stamping, and the JSONL log.

The loop calls `on_event(type, data)`. A Recorder is that callback: it redacts
the data, stamps it with ts / run_id / seq, and hands the Event to each sink.
Sinks are plain callables, so the log file, the tracer, and the CLI's output
all receive the same redacted events.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import reduce
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
# Compared after lowercasing and turning '-' into '_', so "X-Api-Key" matches.
REDACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "client_secret",
    }
)
KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # Anthropic / OpenAI style
    re.compile(r"(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)
ENVELOPE = ("ts", "run_id", "seq", "type")


def _secret_key(key: str) -> bool:
    return key.lower().replace("-", "_") in REDACT_KEYS


def redact(value: Any) -> Any:
    """Return a copy with secret-named keys blanked and key-shaped strings masked."""
    if isinstance(value, Mapping):
        return {k: REDACTED if _secret_key(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return reduce(lambda s, p: p.sub(REDACTED, s), KEY_PATTERNS, value)
    return value


@dataclass(frozen=True)
class Event:
    ts: str
    run_id: str
    seq: int
    type: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON shape: envelope fields first, then the event's own fields."""
        body = {k: v for k, v in self.data.items() if k not in ENVELOPE}
        return {"ts": self.ts, "run_id": self.run_id, "seq": self.seq, "type": self.type, **body}


EventSink = Callable[[Event], None]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    """Sortable by start time, unique enough for a laptop: 20261102T101500Z-1a2b3c4d."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


class Recorder:
    """The loop's on_event callback. Owns the run id and the seq counter.

    It holds a counter, so it is not frozen. It never changes the data it is
    given; `redact` returns a new structure.
    """

    def __init__(
        self,
        run_id: str,
        sinks: Iterable[EventSink],
        version: str = "",
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.run_id = run_id
        self.version = version
        self._sinks = tuple(sinks)
        self._seq = itertools.count(1)
        self._clock = clock

    def __call__(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "run_started" and self.version:
            data = {**data, "harness_version": self.version}
        event = Event(self._clock(), self.run_id, next(self._seq), event_type, redact(data))
        for sink in self._sinks:
            sink(event)


@dataclass(frozen=True)
class JsonlSink:
    """Append one JSON line per event. Open, write, flush, close: a crash keeps every line."""

    path: Path

    def __call__(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()


def open_run_log(log_dir: Path, run_id: str) -> JsonlSink:
    log_dir.mkdir(parents=True, exist_ok=True)
    return JsonlSink(log_dir / f"{run_id}.jsonl")


class EventCollector:
    """Keeps every event in memory, for the end-of-run summary and for tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def records(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]


def default_log_dir(app: str = "harness") -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / app / "runs"


def read_events(path: Path) -> list[dict[str, Any]]:
    """Parse a run log. Only a torn *final* line (a crash mid-write) is skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return records
```

### `harness/tracing.py`

```python
"""OpenTelemetry spans built from run events.

Import this module only when tracing is on: it imports `opentelemetry` at the
top, and the rest of the harness must work without it installed.

Span shape: one run span; under it, one span per model call and one per tool
call. Model spans use OpenTelemetry GenAI semantic-convention names. Those
conventions are still evolving, so check the current spec before you build
dashboards on them. Prompt text, model output, and tool input/output are left
off spans unless `include_content` is set.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from harness.events import Event

TRACER_NAME = "harness"


class SpanSink:
    """An event sink that opens and closes spans as run events arrive.

    It tracks open spans, so it holds state. One SpanSink per run.
    """

    def __init__(self, tracer: trace.Tracer, include_content: bool = False) -> None:
        self._tracer = tracer
        self._include_content = include_content
        self._model_name = "unknown"
        self._run: Span | None = None
        self._model_span: Span | None = None
        self._tool_spans: dict[str, Span] = {}

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    def _child(self, name: str, attributes: dict[str, Any]) -> Span:
        parent = trace.set_span_in_context(self._run) if self._run is not None else None
        return self._tracer.start_span(name, context=parent, attributes=attributes)

    def _on_run_started(self, event: Event) -> None:
        self._model_name = str(event.data.get("model", "unknown"))
        self._run = self._tracer.start_span(
            "harness.run",
            attributes={
                "harness.run_id": event.run_id,
                "harness.model": self._model_name,
                "harness.version": str(event.data.get("harness_version", "")),
            },
        )

    def _on_model_request(self, event: Event) -> None:
        self._model_span = self._child(
            f"chat {self._model_name}",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": self._model_name,
                "harness.turn": event.data["turn"],
                "harness.message_count": event.data["message_count"],
            },
        )

    def _on_model_response(self, event: Event) -> None:
        span, self._model_span = self._model_span, None
        if span is None:
            return
        usage = event.data["usage"]
        span.set_attribute("gen_ai.usage.input_tokens", usage["input_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", usage["output_tokens"])
        span.set_attribute("gen_ai.response.finish_reasons", [event.data["stop_reason"]])
        span.set_attribute("harness.tool_call_count", len(event.data["tool_calls"]))
        if self._include_content:
            span.set_attribute("harness.content.response_text", event.data["text"])
        span.end()

    def _on_tool_call(self, event: Event) -> None:
        name = event.data["name"]
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": event.data["id"],
            "harness.permission.decision": event.data["permission"],
            "harness.hook.outcome": event.data["hook"],
        }
        if self._include_content:
            attributes["harness.content.tool_input"] = json.dumps(event.data["input"])
        self._tool_spans[event.data["id"]] = self._child(f"execute_tool {name}", attributes)

    def _on_tool_result(self, event: Event) -> None:
        span = self._tool_spans.pop(event.data["id"], None)
        if span is None:
            return
        span.set_attribute("harness.tool.is_error", event.data["is_error"])
        span.set_attribute("harness.tool.result_size", event.data["size"])
        if self._include_content:
            span.set_attribute("harness.content.tool_output", event.data["preview"])
        if event.data["is_error"]:
            span.set_attribute("error.type", "tool_error")
            span.set_status(Status(StatusCode.ERROR, "tool returned an error"))
        span.end()

    def _on_run_finished(self, event: Event) -> None:
        crashed = event.data["stop_reason"] == "error"
        open_spans = [s for s in (self._model_span, *self._tool_spans.values()) if s is not None]
        for span in open_spans:  # only non-empty after a crash mid-call
            span.set_status(Status(StatusCode.ERROR, "run ended before this call finished"))
            span.end()
        self._model_span, self._tool_spans = None, {}
        if self._run is None:
            return
        self._run.set_attribute("harness.stop_reason", event.data["stop_reason"])
        self._run.set_attribute("harness.turns", event.data.get("turns", 0))
        if crashed:
            self._run.set_attribute("error.type", event.data.get("error", "error").split(":")[0])
            self._run.set_status(Status(StatusCode.ERROR, "run crashed"))
        self._run.end()
        self._run = None


def make_span_sink(
    tracer_provider: trace.TracerProvider | None = None, include_content: bool = False
) -> SpanSink:
    """Use the given provider, or the process-wide one if None."""
    tracer = trace.get_tracer(TRACER_NAME, tracer_provider=tracer_provider)
    return SpanSink(tracer, include_content=include_content)


def console_provider() -> TracerProvider:
    """Print finished spans to stderr. Swap the exporter for OTLP to use Jaeger or Phoenix."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    return provider
```

### `harness/replay.py`

```python
"""Record a run to a cassette, then replay it as a deterministic test.

A cassette stores, per model call, a fingerprint of the request and the full
response. Replay serves the responses in order and checks each request's
fingerprint: tool schema names, message count, and the last message's role and
tool-result ids. Those catch most harness changes that would alter what the
model sees, without making the test break on every prompt-wording tweak.

Cassette format (JSON):

    {"version": 1, "model": "...", "interactions": [
        {"request": {"tool_names": [...], "message_count": 1,
                     "last_role": "user", "tool_result_ids": []},
         "response": {"text": "", "tool_calls": [{"id", "name", "input"}],
                      "stop_reason": "tool_use",
                      "usage": {"input_tokens": 0, "output_tokens": 0}}}]}
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.events import redact
from harness.models import ModelClient
from harness.types import ModelTurn, ToolCall

CASSETTE_VERSION = 1


class ReplayMismatch(Exception):
    """The harness sent a request that differs from the recorded one."""


class CassetteError(ValueError):
    """The cassette file is missing or malformed."""


@dataclass(frozen=True)
class RequestPrint:
    tool_names: tuple[str, ...]
    message_count: int
    last_role: str
    tool_result_ids: tuple[str, ...]

    @classmethod
    def of(cls, messages: Sequence[dict], tools: Sequence[dict]) -> RequestPrint:
        last = messages[-1] if messages else {}
        content = last.get("content")
        blocks = content if isinstance(content, list) else []
        ids = tuple(b["tool_use_id"] for b in blocks if b.get("type") == "tool_result")
        return cls(tuple(t["name"] for t in tools), len(messages), last.get("role", ""), ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_names": list(self.tool_names),
            "message_count": self.message_count,
            "last_role": self.last_role,
            "tool_result_ids": list(self.tool_result_ids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RequestPrint:
        return cls(
            tuple(d["tool_names"]),
            int(d["message_count"]),
            str(d["last_role"]),
            tuple(d["tool_result_ids"]),
        )


def diff_prints(recorded: RequestPrint, actual: RequestPrint) -> list[str]:
    """Human-readable differences. '+name' is new in this run, '-name' is gone."""
    problems = []
    added = sorted(set(actual.tool_names) - set(recorded.tool_names))
    removed = sorted(set(recorded.tool_names) - set(actual.tool_names))
    if added or removed:
        names = [f"+{n}" for n in added] + [f"-{n}" for n in removed]
        problems.append("tool schema names differ: " + " ".join(names))
    if actual.message_count != recorded.message_count:
        problems.append(
            f"message count differs: recorded {recorded.message_count}, got {actual.message_count}"
        )
    if actual.last_role != recorded.last_role:
        problems.append(
            f"last message role differs: recorded {recorded.last_role!r}, got {actual.last_role!r}"
        )
    if actual.tool_result_ids != recorded.tool_result_ids:
        problems.append(
            f"tool-result ids differ: recorded {list(recorded.tool_result_ids)}, "
            f"got {list(actual.tool_result_ids)}"
        )
    return problems


def turn_to_dict(turn: ModelTurn) -> dict[str, Any]:
    return {
        "text": turn.text,
        "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in turn.tool_calls],
        "stop_reason": turn.stop_reason,
        "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens},
    }


def turn_from_dict(d: dict[str, Any]) -> ModelTurn:
    calls = tuple(ToolCall(c["id"], c["name"], dict(c["input"])) for c in d.get("tool_calls", []))
    usage = d.get("usage", {})
    return ModelTurn(
        text=str(d.get("text", "")),
        tool_calls=calls,
        stop_reason=str(d.get("stop_reason", "tool_use" if calls else "end_turn")),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
    )


@dataclass(frozen=True)
class Interaction:
    request: RequestPrint
    response: ModelTurn


@dataclass(frozen=True)
class Cassette:
    model: str
    interactions: tuple[Interaction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CASSETTE_VERSION,
            "model": self.model,
            "interactions": [
                {"request": i.request.to_dict(), "response": turn_to_dict(i.response)}
                for i in self.interactions
            ],
        }


def load_cassette(path: Path) -> Cassette:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CassetteError(f"cannot read cassette {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != CASSETTE_VERSION:
        raise CassetteError(f"{path}: expected an object with version {CASSETTE_VERSION}")
    try:
        interactions = tuple(
            Interaction(RequestPrint.from_dict(i["request"]), turn_from_dict(i["response"]))
            for i in raw["interactions"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CassetteError(f"{path}: malformed interaction: {exc!r}") from exc
    return Cassette(str(raw.get("model", "unknown")), interactions)


def save_cassette(path: Path, cassette: Cassette) -> None:
    """Redact, then write atomically: a crash never leaves half a cassette."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(redact(cassette.to_dict()), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class ReplayModel:
    """Serves recorded responses in order and checks each request against the cassette.

    A test double with a cursor, like ScriptedModel, so it is mutable.
    """

    def __init__(self, cassette: Cassette) -> None:
        self.model = cassette.model
        self._interactions = cassette.interactions
        self._cursor = 0

    @classmethod
    def from_file(cls, path: Path) -> ReplayModel:
        return cls(load_cassette(path))

    @property
    def remaining(self) -> int:
        return len(self._interactions) - self._cursor

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        n = self._cursor + 1
        if self._cursor >= len(self._interactions):
            raise ReplayMismatch(
                f"turn {n}: the harness made a model call the cassette does not have "
                f"(it has {len(self._interactions)})"
            )
        recorded = self._interactions[self._cursor]
        problems = diff_prints(recorded.request, RequestPrint.of(messages, tools))
        if problems:
            raise ReplayMismatch(f"turn {n}: " + "; ".join(problems))
        self._cursor += 1
        return recorded.response


class RecordingModel:
    """Wraps a real ModelClient and saves the cassette after every call."""

    def __init__(self, inner: ModelClient, path: Path) -> None:
        self.inner = inner
        self.path = path
        self.model = str(getattr(inner, "model", type(inner).__name__))
        self._interactions: tuple[Interaction, ...] = ()

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        turn = self.inner.complete(system, messages, tools)
        recorded = Interaction(RequestPrint.of(messages, tools), turn)
        self._interactions = (*self._interactions, recorded)
        save_cassette(self.path, Cassette(self.model, self._interactions))
        return turn
```

### `harness/config.py`

```python
"""CLI configuration: a TOML file, then environment, then flags.

Precedence, lowest to highest: built-in defaults, the config file, the
HARNESS_MODEL / HARNESS_LOG_DIR environment variables, command-line flags.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from harness.events import default_log_dir
from harness.models import DEFAULT_MODEL

TOP_LEVEL_KEYS = frozenset({"model", "max_turns", "token_budget", "log_dir", "tracing", "prices"})
TRACING_KEYS = frozenset({"enabled", "include_content"})
PRICE_KEYS = frozenset({"input_per_mtok", "output_per_mtok"})


class ConfigError(Exception):
    """Bad usage or bad configuration. The CLI exits 2."""


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class HarnessConfig:
    model: str = DEFAULT_MODEL
    max_turns: int = 20
    token_budget: int = 200_000
    log_dir: Path = field(default_factory=default_log_dir)
    tracing: bool = False
    trace_content: bool = False
    # No built-in prices: a stale number is worse than none. Unpriced -> cost_usd null.
    prices: Mapping[str, Price] = field(default_factory=lambda: MappingProxyType({}))


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "harness" / "config.toml"


def _check_keys(section: str, raw: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {section}: {', '.join(unknown)}")


def _positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {value!r}")
    return value


def _parse_prices(raw: Mapping[str, Any]) -> Mapping[str, Price]:
    prices = {}
    for model, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"prices.{model} must be a table")
        _check_keys(f"prices.{model}", entry, PRICE_KEYS)
        try:
            prices[model] = Price(float(entry["input_per_mtok"]), float(entry["output_per_mtok"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"prices.{model} needs numeric {sorted(PRICE_KEYS)}") from exc
    return MappingProxyType(prices)


def parse_config(raw: Mapping[str, Any]) -> HarnessConfig:
    _check_keys("config", raw, TOP_LEVEL_KEYS)
    tracing = raw.get("tracing", {})
    if not isinstance(tracing, Mapping) or not isinstance(raw.get("prices", {}), Mapping):
        raise ConfigError("[tracing] and [prices] must be tables")
    _check_keys("[tracing]", tracing, TRACING_KEYS)
    defaults = HarnessConfig()
    return HarnessConfig(
        model=str(raw.get("model", defaults.model)),
        max_turns=_positive_int(raw, "max_turns", defaults.max_turns),
        token_budget=_positive_int(raw, "token_budget", defaults.token_budget),
        log_dir=Path(raw["log_dir"]).expanduser() if "log_dir" in raw else defaults.log_dir,
        tracing=bool(tracing.get("enabled", False)),
        trace_content=bool(tracing.get("include_content", False)),
        prices=_parse_prices(raw.get("prices", {})),
    )


def load_config(path: Path | None) -> HarnessConfig:
    """An explicit path must exist. With None, use the default path if it exists."""
    if path is None:
        path = default_config_path()
        if not path.exists():
            return HarnessConfig()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(raw)


def with_env(config: HarnessConfig, env: Mapping[str, str]) -> HarnessConfig:
    changes: dict[str, Any] = {}
    if env.get("HARNESS_MODEL"):
        changes["model"] = env["HARNESS_MODEL"]
    if env.get("HARNESS_LOG_DIR"):
        changes["log_dir"] = Path(env["HARNESS_LOG_DIR"]).expanduser()
    return replace(config, **changes)


def cost_usd(
    prices: Mapping[str, Price], model: str, input_tokens: int, output_tokens: int
) -> float | None:
    price = prices.get(model)
    if price is None:
        return None
    total = input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
    return round(total / 1_000_000, 6)
```

### `harness/report.py`

```python
"""Turn a run's events into the headless result object and an exit code.

Everything here reads event records (dicts), so it works the same on the
in-memory events of a live run and on a JSONL log read back from disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from harness.config import Price, cost_usd

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LIMIT = 3
LIMIT_STOPS = frozenset({"max_turns", "budget", "loop_detected"})


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    model: str
    stop_reason: str
    final_text: str
    turns: int
    input_tokens: int
    output_tokens: int
    error: str | None = None


def summarize(records: Iterable[Mapping[str, Any]]) -> RunSummary:
    """Fold events into totals. A log with no run_finished counts as an error."""
    run_id, model, stop, text, error = "", "unknown", "error", "", "run did not finish"
    turns = input_tokens = output_tokens = 0
    for r in records:
        run_id = str(r.get("run_id", run_id))
        if r["type"] == "run_started":
            model = str(r.get("model", model))
        elif r["type"] == "model_response":
            turns += 1
            text = str(r.get("text", ""))
            input_tokens += int(r["usage"]["input_tokens"])
            output_tokens += int(r["usage"]["output_tokens"])
        elif r["type"] == "run_finished":
            stop, error = str(r["stop_reason"]), r.get("error")
    return RunSummary(run_id, model, stop, text, turns, input_tokens, output_tokens, error)


def exit_code(stop_reason: str) -> int:
    if stop_reason == "done":
        return EXIT_OK
    if stop_reason in LIMIT_STOPS:
        return EXIT_LIMIT
    return EXIT_ERROR


def result_object(summary: RunSummary, prices: Mapping[str, Price]) -> dict[str, Any]:
    """The `--output-format json` payload. Anything but `done` is is_error."""
    return {
        "result": summary.error if summary.error else summary.final_text,
        "stop_reason": summary.stop_reason,
        "turns": summary.turns,
        "usage": {"input_tokens": summary.input_tokens, "output_tokens": summary.output_tokens},
        "cost_usd": cost_usd(prices, summary.model, summary.input_tokens, summary.output_tokens),
        "run_id": summary.run_id,
        "is_error": summary.stop_reason != "done",
    }
```

### `harness/main.py`

```python
"""The `harness` command: interactive by default, headless with -p.

    harness "fix the failing test"                       # interactive
    harness -p "fix the failing test" --output-format json

Exit codes: 0 done; 1 error; 2 bad usage or config; 3 stopped on a limit.
Set HARNESS_MODEL=replay:<cassette.json> (or --model) to run offline from a
cassette; no API key is needed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from harness.cli import terminal_approver
from harness.config import ConfigError, HarnessConfig, load_config, with_env
from harness.events import (
    Event,
    EventCollector,
    EventSink,
    Recorder,
    new_run_id,
    open_run_log,
)
from harness.hooks import block_protected_writes, python_syntax_note
from harness.loop import Harness
from harness.models import AnthropicModel, ModelClient
from harness.permissions import Approver, RuleBasedPolicy
from harness.replay import CassetteError, RecordingModel, ReplayModel
from harness.report import (
    EXIT_ERROR,
    EXIT_USAGE,
    RunSummary,
    exit_code,
    result_object,
    summarize,
)
from harness.workspace import Workspace

REPLAY_PREFIX = "replay:"
TOOL_INPUT_PREVIEW = 200


def harness_version() -> str:
    try:
        return version("minimal-harness")
    except PackageNotFoundError:
        return "0.0.0+source"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Run the coding-agent harness.")
    p.add_argument("prompt", help="the task for the agent")
    p.add_argument("-p", "--print", action="store_true", help="headless: no prompts, no UI")
    p.add_argument("--output-format", choices=("text", "json", "stream-json"))
    p.add_argument("--allow-ask", action="store_true", help="headless: approve `ask` calls")
    p.add_argument("--workspace", default=".", help="directory the agent may touch")
    p.add_argument("--config", type=Path, help="TOML config file")
    p.add_argument("--model", help="model name, or replay:<cassette.json>")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--budget", type=int, help="total token budget")
    p.add_argument("--log-dir", type=Path, help="where run logs go")
    p.add_argument("--record", type=Path, help="save this run as a replay cassette")
    p.add_argument("--trace", action="store_true", help="print OpenTelemetry spans to stderr")
    p.add_argument("--trace-content", action="store_true", help="put content on spans")
    p.add_argument("--version", action="version", version=f"%(prog)s {harness_version()}")
    return p


def resolve_config(args: argparse.Namespace, env: dict[str, str]) -> HarnessConfig:
    config = with_env(load_config(args.config), env)
    flags: dict[str, Any] = {
        "model": args.model,
        "max_turns": args.max_turns,
        "token_budget": args.budget,
        "log_dir": args.log_dir,
    }
    for name in ("max_turns", "token_budget"):
        if flags[name] is not None and flags[name] <= 0:
            raise ConfigError(f"--{name.replace('_', '-')} must be positive")
    config = replace(config, **{k: v for k, v in flags.items() if v is not None})
    if args.trace or args.trace_content:
        config = replace(
            config, tracing=True, trace_content=config.trace_content or args.trace_content
        )
    return config


def make_model(spec: str, env: dict[str, str]) -> ModelClient:
    if spec.startswith(REPLAY_PREFIX):
        return ReplayModel.from_file(Path(spec.removeprefix(REPLAY_PREFIX)))
    if not env.get("ANTHROPIC_API_KEY"):
        raise ConfigError("ANTHROPIC_API_KEY is not set (or use --model replay:<cassette>)")
    if importlib.util.find_spec("anthropic") is None:
        raise ConfigError("live runs need the SDK: pip install anthropic")
    return AnthropicModel(model=spec)


@dataclass(frozen=True)
class Tracing:
    sink: EventSink
    shutdown: Callable[[], None]


def make_tracing(config: HarnessConfig) -> Tracing | None:
    if not config.tracing:
        return None
    try:
        from harness import tracing
    except ImportError as exc:
        raise ConfigError(
            "tracing is on but opentelemetry-sdk is not installed: "
            "pip install 'minimal-harness[tracing]'"
        ) from exc
    provider = tracing.console_provider()
    sink = tracing.make_span_sink(provider, include_content=config.trace_content)
    return Tracing(sink, provider.shutdown)


def stream_json_sink(event: Event) -> None:
    print(json.dumps(event.to_dict(), default=str), flush=True)


def console_sink(event: Event) -> None:
    """Interactive view: model text as it arrives, one line per tool call and result."""
    d = event.data
    if event.type == "model_response" and d["text"]:
        print(d["text"], flush=True)
    elif event.type == "tool_call":
        shown = json.dumps(d["input"])[:TOOL_INPUT_PREVIEW]
        print(f"→ {d['name']} {shown}  [{d['permission']}]", flush=True)
    elif event.type == "tool_result":
        status = "error" if d["is_error"] else "ok"
        print(f"  ← {status}, {d['size']} chars, {d['duration_ms']:.0f} ms", flush=True)


def choose_approver(args: argparse.Namespace) -> Approver | None:
    """Headless: no approver, so `ask` is denied, unless --allow-ask."""
    if not args.print:
        return terminal_approver
    if args.allow_ask:
        return lambda call, reason: True
    return None


@dataclass(frozen=True)
class Setup:
    config: HarnessConfig
    model: ModelClient
    workspace: Workspace
    tracing: Tracing | None


def prepare(args: argparse.Namespace, env: dict[str, str]) -> Setup:
    """Everything that can fail as bad usage or config, before the run starts."""
    config = resolve_config(args, env)
    model = make_model(config.model, env)
    if args.record is not None:
        model = RecordingModel(model, args.record)
    try:
        workspace = Workspace.at(args.workspace)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"bad workspace {args.workspace!r}: {exc}") from exc
    return Setup(config, model, workspace, make_tracing(config))


def build_harness(setup: Setup, args: argparse.Namespace, recorder: Recorder) -> Harness:
    return Harness(
        model=setup.model,
        workspace=setup.workspace,
        policy=RuleBasedPolicy(setup.workspace),
        approver=choose_approver(args),
        pre_tool=(block_protected_writes,),
        post_tool=(python_syntax_note,),
        max_turns=setup.config.max_turns,
        token_budget=setup.config.token_budget,
        on_event=recorder,
    )


def output_sinks(args: argparse.Namespace) -> list[EventSink]:
    if not args.print:
        return [console_sink]
    return [stream_json_sink] if args.output_format == "stream-json" else []


def run_once(setup: Setup, args: argparse.Namespace) -> int:
    run_id = new_run_id()
    collector = EventCollector()
    log = open_run_log(setup.config.log_dir, run_id)
    sinks = [collector, log, *output_sinks(args)]
    if setup.tracing is not None:
        sinks.append(setup.tracing.sink)
    recorder = Recorder(run_id, sinks, version=harness_version())
    try:
        build_harness(setup, args, recorder).run(args.prompt)
    except Exception as exc:  # already recorded as run_finished(error); report, don't trace back
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if setup.tracing is not None:
            setup.tracing.shutdown()
    summary = summarize(collector.records())
    report(summary, setup.config, args, log.path)
    return exit_code(summary.stop_reason)


def report(
    summary: RunSummary, config: HarnessConfig, args: argparse.Namespace, log_path: Path
) -> None:
    """Interactive mode already streamed the text, so it only gets a status line."""
    fmt = args.output_format or "text"
    if not args.print:
        print(f"[{summary.stop_reason}, {summary.turns} turns, log: {log_path}]", file=sys.stderr)
    elif fmt == "json":
        print(json.dumps(result_object(summary, config.prices)))
    elif fmt == "stream-json":
        print(json.dumps({"type": "result", **result_object(summary, config.prices)}))
    elif summary.stop_reason != "error":
        print(summary.final_text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output_format and not args.print:
        parser.error("--output-format needs -p")  # exits 2
    if args.allow_ask and not args.print:
        parser.error("--allow-ask only applies with -p")
    env = dict(os.environ)
    try:
        setup = prepare(args, env)
    except (ConfigError, CassetteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        return run_once(setup, args)
    except OSError as exc:  # e.g. the log directory is not writable
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
```

### `harness.example.toml`

```toml
# Copy to ~/.config/harness/config.toml (or pass --config) and edit.

model = "claude-sonnet-4-5"   # check your provider's current model list
max_turns = 20
token_budget = 200000
log_dir = "~/.local/state/harness/runs"

[tracing]
enabled = false          # needs: pip install 'minimal-harness[tracing]'
include_content = false  # prompts and tool I/O on spans; leave off for shared backends

# PLACEHOLDER PRICES. These numbers are made up so the arithmetic is easy to
# check. Replace them with current per-million-token prices from your
# provider's pricing page before you trust any cost_usd figure.
[prices."claude-sonnet-4-5"]
input_per_mtok = 1.00
output_per_mtok = 10.00
```

The two cassettes. Every request fingerprint lists the four default tools.
Message counts go 1, 3, 5: the task, then one assistant message and one
tool-result message per turn.

### `tests/cassettes/success.json`

```json
{
  "version": 1,
  "model": "claude-sonnet-4-5",
  "interactions": [
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 1,
        "last_role": "user",
        "tool_result_ids": []
      },
      "response": {
        "text": "",
        "tool_calls": [{"id": "toolu_s1", "name": "list_dir", "input": {"path": "src"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1000, "output_tokens": 40}
      }
    },
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 3,
        "last_role": "user",
        "tool_result_ids": ["toolu_s1"]
      },
      "response": {
        "text": "",
        "tool_calls": [{"id": "toolu_s2", "name": "read_file", "input": {"path": "src/app.py"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1100, "output_tokens": 40}
      }
    },
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 5,
        "last_role": "user",
        "tool_result_ids": ["toolu_s2"]
      },
      "response": {
        "text": "app.py prints hi.",
        "tool_calls": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1200, "output_tokens": 20}
      }
    }
  ]
}
```

### `tests/cassettes/denied_then_recovers.json`

```json
{
  "version": 1,
  "model": "claude-sonnet-4-5",
  "interactions": [
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 1,
        "last_role": "user",
        "tool_result_ids": []
      },
      "response": {
        "text": "",
        "tool_calls": [
          {
            "id": "toolu_d1",
            "name": "write_file",
            "input": {"path": "NOTES.md", "content": "# Notes\nThe app prints hi.\n"}
          }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1000, "output_tokens": 60}
      }
    },
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 3,
        "last_role": "user",
        "tool_result_ids": ["toolu_d1"]
      },
      "response": {
        "text": "Writing at the workspace root needs approval. I'll put the notes under src/ instead.",
        "tool_calls": [
          {
            "id": "toolu_d2",
            "name": "write_file",
            "input": {"path": "src/NOTES.md", "content": "# Notes\nThe app prints hi.\n"}
          }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1150, "output_tokens": 70}
      }
    },
    {
      "request": {
        "tool_names": ["read_file", "write_file", "list_dir", "run_command"],
        "message_count": 5,
        "last_role": "user",
        "tool_result_ids": ["toolu_d2"]
      },
      "response": {
        "text": "Wrote src/NOTES.md. The root NOTES.md was denied, so the notes live under src/.",
        "tool_calls": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1250, "output_tokens": 30}
      }
    }
  ]
}
```

### `tests/test_events.py`

```python
"""Event log: order, seq, flush-per-event, crash safety, and redaction."""

from __future__ import annotations

import json

import pytest
from conftest import call, turn

from harness.events import (
    REDACTED,
    EventCollector,
    Recorder,
    open_run_log,
    read_events,
    redact,
)
from harness.models import ScriptExhausted

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE0123456789"
FAKE_GH = "ghp_FAKEfake0123456789FAKEfake"


def recorded_harness(make_harness, tmp_path, *turns, run_id="run-1", extra_sinks=()):
    log = open_run_log(tmp_path / "logs", run_id)
    collector = EventCollector()
    recorder = Recorder(run_id, [log, collector, *extra_sinks], version="test")
    harness, model = make_harness(*turns, on_event=recorder)
    return harness, log.path, collector


def test_scripted_run_logs_expected_events_in_order(make_harness, tmp_path):
    harness, path, _ = recorded_harness(
        make_harness,
        tmp_path,
        turn(call("list_dir", path="src")),
        turn(call("read_file", path="src/app.py")),
        turn(text="app.py prints hi."),
    )
    harness.run("What does the app print?")
    records = read_events(path)
    step = ["model_request", "model_response", "tool_call", "tool_result"]
    expected = ["run_started", *step, *step, "model_request", "model_response", "run_finished"]
    assert [r["type"] for r in records] == expected
    assert [r["seq"] for r in records] == list(range(1, len(expected) + 1))
    assert {r["run_id"] for r in records} == {"run-1"}
    assert all({"ts", "run_id", "seq", "type"} <= r.keys() for r in records)
    started, finished = records[0], records[-1]
    assert started["harness_version"] == "test" and "run_command" in started["tools"]
    assert finished["stop_reason"] == "done" and finished["turns"] == 3
    tool_call, tool_result = records[3], records[4]
    assert tool_call["permission"] == "allow" and tool_call["hook"] == "passed"
    assert tool_result["is_error"] is False and tool_result["duration_ms"] >= 0


def test_each_event_is_on_disk_before_the_next_step(make_harness, tmp_path):
    log = open_run_log(tmp_path / "logs", "run-2")
    seen: list[tuple[int, int]] = []

    def lines_on_disk(event):  # runs right after the log sink, for every event
        seen.append((event.seq, len(log.path.read_text().splitlines())))

    harness, _ = make_harness(
        turn(call("list_dir", path="src")),
        turn(text="ok"),
        on_event=Recorder("run-2", [log, lines_on_disk]),
    )
    harness.run("go")
    assert seen and all(seq == lines for seq, lines in seen)


def test_crash_leaves_a_usable_log_ending_in_run_finished(make_harness, tmp_path):
    harness, path, _ = recorded_harness(make_harness, tmp_path, turn(call("list_dir", path=".")))
    with pytest.raises(ScriptExhausted):
        harness.run("go")
    records = read_events(path)
    assert records[-1]["type"] == "run_finished"
    assert records[-1]["stop_reason"] == "error"
    assert "ScriptExhausted" in records[-1]["error"]


def test_planted_secrets_never_reach_the_log(make_harness, ws, tmp_path):
    (ws.root / "src" / "settings.py").write_text(f"API_KEY = '{FAKE_KEY}'\nGH = '{FAKE_GH}'\n")
    harness, path, collector = recorded_harness(
        make_harness,
        tmp_path,
        turn(call("write_file", path="src/env_example.py", content=f"KEY = '{FAKE_KEY}'\n")),
        turn(call("read_file", path="src/settings.py")),
        turn(text="done"),
    )
    harness.run("go")
    text = path.read_text()
    assert FAKE_KEY not in text and FAKE_GH not in text
    assert REDACTED in text
    calls = [e for e in collector.events if e.type == "tool_call"]
    results = [e for e in collector.events if e.type == "tool_result"]
    assert calls[0].data["input"]["content"] == f"KEY = '{REDACTED}'\n"  # tool input
    assert REDACTED in results[1].data["preview"]  # tool output
    assert (ws.root / "src" / "env_example.py").read_text() == f"KEY = '{FAKE_KEY}'\n"


def test_redact_blanks_secret_keys_at_any_depth_and_copies():
    original = {
        "headers": {"Authorization": "Bearer abc", "X-Api-Key": "k", "accept": "json"},
        "items": [{"token": "t", "name": "n"}, f"log line with {FAKE_KEY} inside"],
        "count": 3,
    }
    snapshot = json.dumps(original)
    cleaned = redact(original)
    assert cleaned["headers"] == {
        "Authorization": REDACTED,
        "X-Api-Key": REDACTED,
        "accept": "json",
    }
    assert cleaned["items"][0] == {"token": REDACTED, "name": "n"}
    assert cleaned["items"][1] == f"log line with {REDACTED} inside"
    assert cleaned["count"] == 3
    assert json.dumps(original) == snapshot  # input unchanged


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "github_pat_FAKE0123456789abcdefghij",
        "Bearer eyJhbGciOiJIUzI1NiJ9.fake.payload",
    ],
)
def test_redact_masks_common_key_formats(secret):
    assert secret not in redact(f"value={secret}")


def test_read_events_skips_only_a_torn_final_line(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text('{"seq": 1}\n{"seq": 2}\n{"seq": 3, "ty')
    assert [r["seq"] for r in read_events(path)] == [1, 2]
    path.write_text('{"seq": 1}\n{"se\n{"seq": 3}\n')
    with pytest.raises(json.JSONDecodeError):
        read_events(path)
```

### `tests/test_tracing.py`

```python
"""OpenTelemetry spans from run events, checked with the in-memory exporter."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from harness.events import Recorder  # noqa: E402
from harness.replay import ReplayModel  # noqa: E402
from harness.tracing import make_span_sink  # noqa: E402

CASSETTES = Path(__file__).parent / "cassettes"
TASK = "What does the app print?"


def traced_run(make_harness, cassette: str, include_content: bool = False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = make_span_sink(provider, include_content=include_content)
    harness, _ = make_harness(
        model=ReplayModel.from_file(CASSETTES / cassette), on_event=Recorder("run-t", [sink])
    )
    harness.run(TASK)
    return exporter.get_finished_spans()


def by_kind(spans):
    runs = [s for s in spans if s.name == "harness.run"]
    models = [s for s in spans if s.name.startswith("chat ")]
    tools = [s for s in spans if s.name.startswith("execute_tool ")]
    return runs, models, tools


def test_one_run_span_with_a_child_per_model_and_tool_call(make_harness):
    spans = traced_run(make_harness, "success.json")
    runs, models, tools = by_kind(spans)
    assert (len(runs), len(models), len(tools)) == (1, 3, 2)
    assert len(spans) == 6
    run = runs[0]
    assert run.parent is None
    for child in (*models, *tools):
        assert child.parent is not None and child.parent.span_id == run.context.span_id
        assert child.context.trace_id == run.context.trace_id
    assert run.attributes["harness.stop_reason"] == "done"
    assert run.attributes["harness.run_id"] == "run-t"


def test_model_spans_carry_genai_attributes(make_harness):
    _, models, _ = by_kind(traced_run(make_harness, "success.json"))
    first = models[0]
    assert first.name == "chat claude-sonnet-4-5"
    assert first.attributes["gen_ai.operation.name"] == "chat"
    assert first.attributes["gen_ai.request.model"] == "claude-sonnet-4-5"
    assert first.attributes["gen_ai.usage.input_tokens"] == 1000
    assert first.attributes["gen_ai.usage.output_tokens"] == 40
    assert tuple(models[-1].attributes["gen_ai.response.finish_reasons"]) == ("end_turn",)


def test_tool_spans_carry_name_decision_and_error_status(make_harness):
    _, _, tools = by_kind(traced_run(make_harness, "denied_then_recovers.json"))
    denied, allowed = tools
    assert denied.attributes["gen_ai.tool.name"] == "write_file"
    assert denied.attributes["harness.permission.decision"] == "ask_denied"
    assert denied.attributes["harness.tool.is_error"] is True
    assert denied.status.status_code == StatusCode.ERROR
    assert allowed.attributes["harness.permission.decision"] == "allow"
    assert allowed.attributes["harness.tool.is_error"] is False
    assert allowed.status.status_code != StatusCode.ERROR


def all_attribute_text(spans) -> str:
    return " ".join(f"{k}={v}" for s in spans for k, v in s.attributes.items())


def test_no_content_on_spans_by_default(make_harness):
    text = all_attribute_text(traced_run(make_harness, "success.json"))
    assert "harness.content." not in text
    for content in ("print('hi')", "app.py prints hi.", TASK, "src/app.py"):
        assert content not in text


def test_content_flag_adds_content(make_harness):
    text = all_attribute_text(traced_run(make_harness, "success.json", include_content=True))
    assert "harness.content.tool_input" in text and "src/app.py" in text
    assert "print('hi')" in text  # tool output preview
    assert "app.py prints hi." in text  # model text
```

### `tests/test_replay.py`

```python
"""Record-and-replay: two cassette regression tests and readable mismatch failures."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import call, results_in, turn

from harness.loop import Harness, LoopState
from harness.models import ScriptedModel
from harness.permissions import RuleBasedPolicy
from harness.replay import (
    Cassette,
    RecordingModel,
    ReplayMismatch,
    ReplayModel,
    RequestPrint,
    diff_prints,
    load_cassette,
)
from harness.tools import default_registry, make_run_command

CASSETTES = Path(__file__).parent / "cassettes"


def replay(make_harness, name: str, **overrides):
    model = ReplayModel.from_file(CASSETTES / name)
    harness, _ = make_harness(model=model, **overrides)
    return harness, model


# --- the two regression tests -------------------------------------------------


def test_regression_successful_task(make_harness):
    harness, model = replay(make_harness, "success.json")
    result = harness.run("What does the app print?")
    assert result.stop_reason == "done"
    assert result.final_text == "app.py prints hi."
    assert result.turns == 3 and model.remaining == 0


def test_regression_permission_denial_then_recovery(make_harness, ws):
    harness, model = replay(make_harness, "denied_then_recovers.json")  # no approver
    result = harness.run("Write a short NOTES.md describing the app.")
    assert result.stop_reason == "done" and model.remaining == 0
    denial = [b for m in result.transcript[2:3] for b in m["content"]][0]
    assert denial["is_error"] and "needs approval" in denial["content"]
    assert not (ws.root / "NOTES.md").exists()
    assert (ws.root / "src" / "NOTES.md").read_text() == "# Notes\nThe app prints hi.\n"


# --- deliberate harness changes make replay fail, with a readable message ---


def test_renaming_a_tool_fails_replay_and_names_the_difference(make_harness):
    renamed = replace(make_run_command(), name="run_cmd")
    harness, _ = replay(make_harness, "success.json", registry=default_registry(renamed))
    with pytest.raises(ReplayMismatch) as err:
        harness.run("What does the app print?")
    assert str(err.value) == "turn 1: tool schema names differ: +run_cmd -run_command"


class ReminderHarness(Harness):
    """A 'harmless' change: inject a reminder message before every model call."""

    def _before_model_call(self, state: LoopState) -> LoopState:
        return state.append({"role": "user", "content": "Reminder: be brief."})


def test_injecting_a_message_fails_replay_with_counts(ws):
    model = ReplayModel.from_file(CASSETTES / "success.json")
    harness = ReminderHarness(model=model, workspace=ws, policy=RuleBasedPolicy(ws))
    with pytest.raises(ReplayMismatch, match=r"^turn 1: message count differs: recorded 1, got 2$"):
        harness.run("What does the app print?")


def test_extra_model_call_beyond_the_cassette_fails(make_harness):
    full = load_cassette(CASSETTES / "success.json")
    short = Cassette(full.model, full.interactions[:2])
    harness, _ = make_harness(model=ReplayModel(short))
    with pytest.raises(ReplayMismatch, match=r"^turn 3: .*cassette does not have \(it has 2\)$"):
        harness.run("What does the app print?")


def test_diff_lists_every_difference():
    recorded = RequestPrint(("a", "b"), 3, "user", ("t1",))
    actual = RequestPrint(("a", "c"), 4, "assistant", ("t1", "t2"))
    assert diff_prints(recorded, actual) == [
        "tool schema names differ: +c -b",
        "message count differs: recorded 3, got 4",
        "last message role differs: recorded 'user', got 'assistant'",
        "tool-result ids differ: recorded ['t1'], got ['t1', 't2']",
    ]
    assert diff_prints(recorded, recorded) == []


# --- recording ---------------------------------------------------------------


def test_recording_then_replaying_round_trips(make_harness, tmp_path):
    cassette = tmp_path / "rec.json"
    scripted = ScriptedModel(
        [
            turn(call("list_dir", path="src"), tokens=(100, 10)),
            turn(call("read_file", path="src/app.py")),
            turn(text="prints hi"),
        ]
    )
    harness, _ = make_harness(model=RecordingModel(scripted, cassette))
    first = harness.run("What does the app print?")
    saved = json.loads(cassette.read_text())
    assert saved["version"] == 1 and len(saved["interactions"]) == 3
    assert saved["interactions"][1]["request"]["message_count"] == 3
    assert saved["interactions"][0]["response"]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 10,
    }

    replayed = ReplayModel(load_cassette(cassette))
    harness2, _ = make_harness(model=replayed)
    second = harness2.run("What does the app print?")
    assert (second.final_text, second.turns) == (first.final_text, first.turns)
    assert replayed.remaining == 0


def test_recording_redacts_secrets_in_the_cassette(make_harness, tmp_path):
    cassette = tmp_path / "rec.json"
    key = "sk-ant-api03-FAKEFAKEFAKEFAKE0123456789"
    scripted = ScriptedModel([turn(text=f"your key is {key}")])
    harness, _ = make_harness(model=RecordingModel(scripted, cassette))
    harness.run("go")
    assert key not in cassette.read_text()


def test_replay_requests_match_what_the_cassette_recorded(make_harness):
    """The fingerprint really is computed from the harness's request."""
    cassette = load_cassette(CASSETTES / "success.json")
    scripted = ScriptedModel(i.response for i in cassette.interactions)
    harness, _ = make_harness(model=scripted)
    harness.run("What does the app print?")
    prints = [RequestPrint.of(r["messages"], r["tools"]) for r in scripted.requests]
    recorded = [i.request for i in cassette.interactions]
    assert prints == recorded
    assert results_in(scripted.requests[1])[0]["tool_use_id"] == "toolu_s1"
```

### `tests/test_main.py`

```python
"""The `harness` CLI, driven as a subprocess from replay cassettes. No API key needed."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.config import ConfigError, Price, cost_usd, load_config
from harness.events import read_events
from harness.report import exit_code

ROOT = Path(__file__).resolve().parents[1]
CASSETTES = Path(__file__).parent / "cassettes"
SUCCESS = CASSETTES / "success.json"
DENIED = CASSETTES / "denied_then_recovers.json"
PRICED = """
[prices."claude-sonnet-4-5"]
input_per_mtok = 1.0
output_per_mtok = 10.0
"""


@pytest.fixture
def cli(ws, tmp_path):
    """Run `python -m harness.main` with a cassette model and private config/log dirs."""
    config = tmp_path / "harness.toml"
    config.write_text(PRICED)

    def run(*args: str, cassette: Path = SUCCESS, stdin: str = "") -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update(
            HARNESS_MODEL=f"replay:{cassette}",
            HARNESS_LOG_DIR=str(tmp_path / "logs"),
            XDG_CONFIG_HOME=str(tmp_path / "xdg"),  # never read the developer's real config
            PYTHONPATH=str(ROOT),
        )
        argv = [sys.executable, "-m", "harness.main", "--workspace", str(ws.root)]
        if "--config" not in args:
            argv += ["--config", str(config)]
        return subprocess.run(
            [*argv, *args], input=stdin, capture_output=True, text=True, env=env, timeout=60
        )

    return run


def log_for(tmp_path: Path, run_id: str) -> list[dict]:
    return read_events(tmp_path / "logs" / f"{run_id}.jsonl")


# --- headless json -----------------------------------------------------------


def test_json_output_is_one_object_with_every_field(cli, tmp_path):
    proc = cli("-p", "What does the app print?", "--output-format", "json")
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.strip().splitlines()) == 1
    out = json.loads(proc.stdout)
    assert out == {
        "result": "app.py prints hi.",
        "stop_reason": "done",
        "turns": 3,
        "usage": {"input_tokens": 3300, "output_tokens": 100},
        "cost_usd": 0.0043,  # 3300 * 1.0 / 1e6 + 100 * 10.0 / 1e6
        "run_id": out["run_id"],
        "is_error": False,
    }
    assert log_for(tmp_path, out["run_id"])[-1]["type"] == "run_finished"


def test_limit_stop_exits_3_and_still_prints_json(cli):
    proc = cli("-p", "go", "--output-format", "json", "--max-turns", "2")
    assert proc.returncode == 3
    out = json.loads(proc.stdout)
    assert out["stop_reason"] == "max_turns" and out["is_error"] is True and out["turns"] == 2


def test_replay_mismatch_is_an_error_exit_1(cli, tmp_path):
    broken = json.loads(SUCCESS.read_text())
    broken["interactions"][0]["request"]["tool_names"][3] = "run_cmd"
    cassette = tmp_path / "broken.json"
    cassette.write_text(json.dumps(broken))
    proc = cli("-p", "go", "--output-format", "json", cassette=cassette)
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["stop_reason"] == "error" and out["is_error"] is True
    assert "tool schema names differ: +run_command -run_cmd" in out["result"]


def test_unpriced_model_reports_null_cost(cli, tmp_path):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    proc = cli("-p", "go", "--output-format", "json", "--config", str(empty))
    assert proc.returncode == 0 and json.loads(proc.stdout)["cost_usd"] is None


# --- headless text and stream-json ------------------------------------------


def test_text_output_is_only_the_final_answer(cli):
    proc = cli("-p", "What does the app print?")
    assert proc.returncode == 0
    assert proc.stdout == "app.py prints hi.\n"


def test_stream_json_emits_one_event_per_line_then_a_result(cli):
    proc = cli("-p", "go", "--output-format", "stream-json")
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    events, result = lines[:-1], lines[-1]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert result["type"] == "result" and result["stop_reason"] == "done"


# --- ask in headless mode ----------------------------------------------------


def permissions(tmp_path: Path, run_id: str) -> list[str]:
    return [e["permission"] for e in log_for(tmp_path, run_id) if e["type"] == "tool_call"]


def test_headless_denies_ask_by_default(cli, ws, tmp_path):
    proc = cli("-p", "Write NOTES.md", "--output-format", "json", cassette=DENIED)
    assert proc.returncode == 0, proc.stderr
    run_id = json.loads(proc.stdout)["run_id"]
    assert permissions(tmp_path, run_id) == ["ask_denied", "allow"]
    assert not (ws.root / "NOTES.md").exists()
    assert (ws.root / "src" / "NOTES.md").exists()


def test_allow_ask_approves_in_headless(cli, ws, tmp_path):
    proc = cli("-p", "Write NOTES.md", "--output-format", "json", "--allow-ask", cassette=DENIED)
    assert proc.returncode == 0, proc.stderr
    run_id = json.loads(proc.stdout)["run_id"]
    assert permissions(tmp_path, run_id) == ["ask_approved", "allow"]
    assert (ws.root / "NOTES.md").exists()


def test_interactive_mode_shows_calls_and_prompts_for_ask(cli, ws):
    proc = cli("Write NOTES.md", cassette=DENIED, stdin="n\n")
    assert proc.returncode == 0, proc.stderr
    assert "[approve?] write_file" in proc.stdout
    assert "→ write_file" in proc.stdout and "[ask_denied]" in proc.stdout
    assert proc.stdout.count("so the notes live under src/.") == 1  # streamed once, not repeated
    assert "[done, 3 turns, log: " in proc.stderr
    assert not (ws.root / "NOTES.md").exists()


# --- bad usage and config: exit 2 --------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("go", "--output-format", "json"),  # needs -p
        ("go", "--allow-ask"),  # needs -p
        ("-p", "go", "--output-format", "xml"),
        ("-p", "go", "--max-turns", "0"),
        ("-p", "go", "--config", "/nonexistent/harness.toml"),
        ("-p", "go", "--workspace", "/nonexistent/dir"),
        ("-p", "go", "--model", "claude-sonnet-4-5"),  # live model, no API key
        ("-p", "go", "--model", "replay:/nonexistent/cassette.json"),
        ("-p",),  # no prompt
    ],
)
def test_bad_usage_or_config_exits_2(cli, args):
    proc = cli(*args)
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""


def test_unknown_config_key_exits_2_and_names_it(cli, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('modle = "typo"\n')
    proc = cli("-p", "go", "--config", str(bad))
    assert proc.returncode == 2 and "unknown key(s) in config: modle" in proc.stderr


# --- in-process checks -------------------------------------------------------


@pytest.mark.parametrize(
    ("stop", "code"),
    [("done", 0), ("error", 1), ("max_turns", 3), ("budget", 3), ("loop_detected", 3)],
)
def test_exit_codes(stop, code):
    assert exit_code(stop) == code


def test_cost_from_price_table():
    prices = {"m": Price(input_per_mtok=2.0, output_per_mtok=8.0)}
    assert cost_usd(prices, "m", 500_000, 100_000) == 1.8
    assert cost_usd(prices, "other", 1, 1) is None


def test_example_config_loads():
    config = load_config(ROOT / "harness.example.toml")
    assert config.model == "claude-sonnet-4-5" and config.tracing is False
    assert "claude-sonnet-4-5" in config.prices


def test_bad_types_in_config_raise(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("max_turns = -1\n")
    with pytest.raises(ConfigError, match="max_turns must be a positive integer"):
        load_config(path)


def block_opentelemetry(monkeypatch):
    """Make every `import opentelemetry...` fail, and re-import harness from scratch."""
    for name in [m for m in sys.modules if m == "opentelemetry" or m.startswith("opentelemetry.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    for name in [m for m in sys.modules if m == "harness" or m.startswith("harness.")]:
        monkeypatch.delitem(sys.modules, name)


def test_harness_runs_without_opentelemetry(monkeypatch, ws, tmp_path, capsys):
    block_opentelemetry(monkeypatch)
    with pytest.raises(ImportError):
        importlib.import_module("opentelemetry.trace")
    main = importlib.import_module("harness.main")
    monkeypatch.setenv("HARNESS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    base = ["--workspace", str(ws.root), "--model", f"replay:{SUCCESS}", "-p", "go"]
    assert main.main(base) == 0
    assert capsys.readouterr().out == "app.py prints hi.\n"
    assert main.main([*base, "--trace"]) == 2
    assert "pip install 'minimal-harness[tracing]'" in capsys.readouterr().err
```

## Meeting the acceptance criteria

- **A scripted run produces a JSONL log with the expected event types in
  order, increasing `seq`, and no secrets.**
  `test_scripted_run_logs_expected_events_in_order` reads the log back from
  disk and checks the exact type sequence, `seq` = 1..12, one `run_id`, and
  the envelope on every line. `test_planted_secrets_never_reach_the_log`
  plants a fake `sk-ant-…` key in a `write_file` input and a file holding it
  plus a fake GitHub token that `read_file` returns. Neither appears anywhere
  in the log file. `[REDACTED]` appears in both the logged input and the
  logged output preview, and the file on disk still has the real value.
  `test_each_event_is_on_disk_before_the_next_step` shows that line *n* is
  on disk when event *n* is handed to the next sink.
  `test_crash_leaves_a_usable_log_ending_in_run_finished` shows a crash still
  leaves a parseable log that ends in `run_finished` with the error.
- **With the in-memory exporter: one run span, one span per model call, one
  per tool call, GenAI attributes, no content by default.**
  `test_one_run_span_with_a_child_per_model_and_tool_call` counts 1 + 3 + 2
  spans and checks every child's parent is the run span.
  `test_model_spans_carry_genai_attributes` checks the four named attributes.
  `test_tool_spans_carry_name_decision_and_error_status` checks the tool
  name, `ask_denied` / `allow`, and the ERROR status on the denied call.
  `test_no_content_on_spans_by_default` searches every attribute for the
  task, the file contents, the tool input, and the model's answer, and finds
  none. `test_content_flag_adds_content` shows the flag turns them on.
  `test_harness_runs_without_opentelemetry` covers "runs without it
  installed".
- **Both cassette regression tests pass, and renaming a tool makes one fail
  with a message that names the difference.**
  `test_regression_successful_task` and
  `test_regression_permission_denial_then_recovery` replay the two cassettes
  and use up every interaction. The second also checks that the denial
  reached the model and that only `src/NOTES.md` was written.
  `test_renaming_a_tool_fails_replay_and_names_the_difference` renames
  `run_command` to `run_cmd` and asserts the whole message:
  `turn 1: tool schema names differ: +run_cmd -run_command`. Two more tests
  show the other kinds of drift: an injected reminder message (`message count
  differs: recorded 1, got 2`) and an extra model call.
- **`harness -p ... --output-format json` prints a single valid JSON object
  with all the listed fields, and the exit code matches the stop reason.**
  `test_json_output_is_one_object_with_every_field` runs the CLI as a
  subprocess and compares the whole object, including `cost_usd` from the
  test's price table (3,300 × $1 + 100 × $10 per million = $0.0043).
  `test_limit_stop_exits_3_and_still_prints_json`,
  `test_replay_mismatch_is_an_error_exit_1`, and the nine cases of
  `test_bad_usage_or_config_exits_2` cover exit codes 3, 1, and 2.
- **In headless mode `ask` is denied unless `--allow-ask` is passed.**
  `test_headless_denies_ask_by_default` and `test_allow_ask_approves_in_headless`
  run the denial cassette through the CLI. They read the run log to check the
  recorded decision (`ask_denied` vs `ask_approved`) and check whether
  `NOTES.md` exists.

## Common pitfalls

- **Redacting in one sink only.** If the JSONL sink redacts but the tracer or
  stream-json printer doesn't, the secret leaks through the one you forgot.
  Redact once, before fan-out.
- **Redacting what the model sees.** Redaction is for records. If you redact
  the messages, the agent can't work with a config file that contains a
  token. Redact the log, not the conversation.
- **Trusting pattern masking alone.** Patterns catch known formats only. A
  database password in a connection string won't match any of them. Treat
  logs as sensitive anyway, and set a retention period.
- **Buffered log writes.** Writing through a long-lived file handle without
  flushing loses the last events on a crash, which are the ones you need.
- **Emitting `tool_call` after the tool runs.** It's simpler, but the span
  then starts after the work it measures, and stream-json readers see the
  result before the call.
- **Content on spans "just for debugging."** It stays on, and the traces go
  to a backend with wider access than your logs. Keep it behind a flag that
  defaults to off.
- **Importing `opentelemetry` at module top in the core.** One stray import in
  `loop.py` or `main.py` makes the SDK a hard dependency. Keep it in the one
  module that's imported only when tracing is on, and test with it blocked.
- **Hard-coding GenAI attribute names as settled.** They've been renamed
  before. Keep them in one module, so a spec update is a one-file change.
- **Replay that compares everything.** If the fingerprint includes the full
  prompt text, every wording tweak breaks every cassette and people learn to
  re-record without looking. Compare the shape that matters, and re-record
  on purpose when you change it.
- **Replay that compares too little.** This fingerprint doesn't include
  `is_error` or the result text. The denial cassette also passes with
  `--allow-ask`, where the first write succeeds. That's why the regression
  test also asserts on the transcript and the files. Replay says the harness
  sent the same *shape*; it doesn't say the run went the same way.
- **Random tool ids in hand-written cassettes.** The harness echoes the
  model's tool ids back in tool results. Fixed ids in the cassette make the
  recorded `tool_result_ids` predictable.
- **Treating a limit stop as success.** A script that checks only for exit 0
  vs non-zero can't tell "out of budget" from "crashed". Give limits their own
  code (3), and say in the docs what each code means.
- **Silently allowing `ask` in headless mode.** An unattended agent must not
  get more power than an attended one. Deny by default; make allowing an
  explicit flag.
- **Shipping real prices in code.** They go stale. Keep them in config, show
  `null` when a model isn't priced, and label any example numbers as
  placeholders.
- **Reading the developer's real config in tests.** The CLI tests point
  `XDG_CONFIG_HOME` and `HARNESS_LOG_DIR` at temp directories and remove
  `ANTHROPIC_API_KEY`, so the results don't depend on the machine running
  them.

## Verification

Start from the exercise-01 files, apply the two changes, and add the new
files above. Then:

```bash
cd exercise-06-observability-replay-and-shipping
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[test,tracing]'

# Offline: scripted and replay models only; no key, no network.
pytest -q

# The installed command, against a cassette.
mkdir -p /tmp/h6-ws/src && echo "print('hi')" > /tmp/h6-ws/src/app.py
export HARNESS_MODEL=replay:$PWD/tests/cassettes/success.json
harness --version
harness -p "What does the app print?" --workspace /tmp/h6-ws --output-format json; echo "exit=$?"
harness -p "go" --workspace /tmp/h6-ws --output-format json --max-turns 2; echo "exit=$?"
harness -p "go" --workspace /tmp/h6-ws --output-format stream-json | jq -c '{seq, type}'
harness -p "go" --workspace /tmp/h6-ws --trace 2>&1 >/dev/null | grep '"name"'
ls ~/.local/state/harness/runs/

# Live run, recorded as a new cassette. Set a spend cap on the key first.
pip install -e '.[live]'
export ANTHROPIC_API_KEY=...            # never commit this
export HARNESS_MODEL=claude-sonnet-4-5  # check the current models list
mkdir -p ~/.config/harness
cp harness.example.toml ~/.config/harness/config.toml  # then fill in real prices
harness -p "What does src/app.py print?" --workspace /tmp/h6-ws \
  --output-format json --record /tmp/h6-live.json
```

Expected: `pytest -q` reports `84 passed` (32 from exercise-01, 52 new). The
first `harness -p` prints one JSON line with `"stop_reason": "done"` and
`exit=0`; `cost_usd` is `null` until you add a price for the model. The
`--max-turns 2` run prints `"stop_reason": "max_turns"` and `exit=3`.
stream-json prints `seq` 1 to 12, then a `result` line. `--trace` prints six
spans to stderr: three `chat claude-sonnet-4-5`, `execute_tool list_dir`,
`execute_tool read_file`, and `harness.run`. The live run leaves a cassette at
`/tmp/h6-live.json`. Point `HARNESS_MODEL=replay:/tmp/h6-live.json` at it and
run the same prompt again to replay it with no network.
