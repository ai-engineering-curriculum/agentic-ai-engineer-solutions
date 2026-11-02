# mod-209-building-custom-harnesses/exercise-04-context-and-session-state — Solution

## Approach

This solution extends the
[exercise-01 reference harness](../exercise-01-build-a-minimal-harness/README.md).
It does not edit any exercise-01 file. Four modules are added beside it:

- `context.py` — token counting, tool-result clearing, and compaction, as
  pure functions over a tuple of messages.
- `managed.py` — `ManagedHarness`, a subclass of the exercise-01 `Harness`
  that overrides the one extension point, `_before_model_call`.
- `session.py` — `features.json` and `progress.md`, the write guard, the
  initializer, git access, and `run_session`.
- `session_cli.py` — a fresh-process entry point.

There are three ways to make room in a context window. They lose different
things, so the harness tries the cheapest first.

**Token accounting.** Every count comes from one function, `estimate_tokens`
(characters ÷ 4). `ContextPolicy.count` holds it, so you can pass a real
counter (for example a wrapper around `client.messages.count_tokens`) without
touching anything else. The provider's reported usage (`input_tokens`,
`output_tokens`) is still what the run's budget counts, because that is what
you pay for. But the clearing decision is made *before* a call, about messages
the provider hasn't seen yet, so it needs a counter. `ManagedHarness.context_tokens()`
and `ContextPolicy.context_limit` are the exposed knobs.

**Clearing (at 60%).** Old `tool_result` contents are replaced, oldest first,
with `[cleared: read_file big1.txt, 1,000 tokens]` until context drops below
the first threshold. The most recent `keep_recent_results` results are never
touched. Only the `content` string changes. The block, its `tool_use_id`, and
the `tool_use` that produced it all stay, so every call is still paired with a
result. Clearing loses only raw output the model has already acted on, and it
needs no model call.

**Compaction (at 85%).** If context is still over the second threshold after
clearing, the harness makes a separate call to a `summarizer` model. The
summarizer gets the history flattened to plain text, plus `COMPACTION_PROMPT`
as its system prompt. Plain text means the request carries no `tool_use`
blocks, so it needs no tool schemas, and the summarizer has nothing to call.
The new history is the summary (as the first user message), followed by the
last `keep_recent_turns` turns. The cut is made at an assistant message, so
each `tool_use` keeps its result. If the result is still over the threshold,
the harness keeps fewer turns. The system prompt is passed separately on every
call, as in exercise-01, so it always survives.

"Its own small budget" means two things here. First, the summarizer is a
separate `ModelClient`; in the CLI it's `AnthropicModel(max_tokens=1024)`.
Second, a summary that comes back with `stop_reason == "max_tokens"` is
treated as truncated and rejected with `CompactionError`. A half-finished
summary is worse than none, because the history it replaces is gone. The
summary call's tokens count toward the run's budget.

The transcript in `RunResult` is never trimmed. Only `messages`, which the
model sees, shrinks. When you debug a bad compaction, you can compare the full
transcript with what the model saw.

**Session state.** `initialize()` runs once. It writes `features.json` (a
list of `{id, description, passes: false}`) and an empty `progress.md`, and
makes a first commit if git is available. It never overwrites existing state.
Each session then builds its prompt from disk: `progress.md`, `features.json`,
`git log --oneline -10`, and the first feature that doesn't pass. The prompt
tells the agent to work on that one feature, flip its `passes`, append to
`progress.md`, and commit.

**The write guard is enforced by the harness, in two places.**

1. `features_guard` is a `pre_tool` hook on `write_file`. It resolves the
   path (so `src/../features.json` is caught) and compares the proposed
   content with the file on disk. Any change other than `passes` values is
   blocked, and the reason goes back to the model as an error result.
2. The hook can't see `run_command`. A model that runs a Python script can
   rewrite the file another way. So `run_session` compares the file before
   and after the session, in a `finally` block. If the change is illegal, it
   restores the old content and reports the violation.

The model can't create `features.json` either. Only the initializer writes it.

**Git is optional.** `read_git_log` returns a note instead of failing when git
is missing or the workspace isn't a repo. `initialize` does the same. The
tests that need a real commit check `shutil.which("git")` first.
`session_harness` auto-allows `git`, but `git_subcommand_guard` limits it to
`status`, `log`, `diff`, `add`, `commit`, and `show`. An agent that can commit
doesn't need to push or reset.

**Resume.** A session holds nothing in memory that the next one needs.
`run_session` takes a harness and a workspace path, reads the files, and
starts a new run with a single user message. The tests kill session 1 partway
through feature `f2`, then show that a brand-new harness instance, and a
separate Python process, both pick up at `f2` with session 1's notes in the
prompt.

## Reference implementation

Layout. Files marked *unchanged* are byte-for-byte the exercise-01 files; copy
them from the [exercise-01 solution](../exercise-01-build-a-minimal-harness/README.md).

```text
exercise-04-context-and-session-state/
├── harness/
│   ├── __init__.py      # unchanged
│   ├── types.py         # unchanged
│   ├── workspace.py     # unchanged
│   ├── schema.py        # unchanged
│   ├── models.py        # unchanged
│   ├── tools.py         # unchanged
│   ├── permissions.py   # unchanged
│   ├── hooks.py         # unchanged
│   ├── loop.py          # unchanged (provides the _before_model_call seam)
│   ├── cli.py           # unchanged (session_cli reuses its terminal approver)
│   ├── context.py       # NEW: token counting, clearing, compaction
│   ├── managed.py       # NEW: ManagedHarness(Harness)
│   ├── session.py       # NEW: state files, write guard, initializer, resume
│   └── session_cli.py   # NEW: fresh-process entry point
├── tests/
│   ├── conftest.py      # unchanged
│   ├── test_tools.py    # unchanged
│   ├── test_loop.py     # unchanged
│   ├── test_models.py   # unchanged
│   ├── test_context.py  # NEW
│   └── test_session.py  # NEW
└── pyproject.toml       # unchanged
```

The exercise-01 tests still run here and still pass, which shows the
extension didn't change base behavior. The new code is about 650 lines of
package code and 465 lines of tests.

### `harness/context.py`

```python
"""Token accounting, tool-result clearing, and compaction.

Everything here is a pure function over a tuple of messages: it returns a new
tuple and never edits a message in place.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.models import ModelClient

TokenCounter = Callable[[str], int]

CLEAR_AT = 0.60
COMPACT_AT = 0.85
KEEP_RECENT_RESULTS = 3
KEEP_RECENT_TURNS = 2
CLEARED_PREFIX = "[cleared:"
SUMMARY_HEADER = "[This session was compacted. Summary of the work so far:]"

COMPACTION_PROMPT = """Summarize this session for a fresh engineer taking over.
Use these headings: Goal, Decisions, Files changed, Current state, Next step.
Be specific: file paths, function names, failing test names. No narrative."""


class CompactionError(RuntimeError):
    """The summary call failed or returned something we can't use."""


def estimate_tokens(text: str) -> int:
    """The one place token counts come from. Swap for a real counter."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class ContextPolicy:
    context_limit: int = 200_000
    clear_at: float = CLEAR_AT
    compact_at: float = COMPACT_AT
    keep_recent_results: int = KEEP_RECENT_RESULTS
    keep_recent_turns: int = KEEP_RECENT_TURNS
    count: TokenCounter = field(default=estimate_tokens)

    def __post_init__(self) -> None:
        if not 0 < self.clear_at < self.compact_at <= 1:
            raise ValueError("need 0 < clear_at < compact_at <= 1")
        if self.context_limit <= 0 or self.keep_recent_results < 0 or self.keep_recent_turns < 0:
            raise ValueError("context_limit must be positive and keep_* non-negative")

    @property
    def clear_tokens(self) -> int:
        return int(self.context_limit * self.clear_at)

    @property
    def compact_tokens(self) -> int:
        return int(self.context_limit * self.compact_at)


def block_text(block: dict[str, Any]) -> str:
    kind = block.get("type")
    if kind == "text":
        return block["text"]
    if kind == "tool_use":
        return f"{block['name']} {json.dumps(block['input'], sort_keys=True)}"
    if kind == "tool_result":
        content = block.get("content", "")
        return content if isinstance(content, str) else json.dumps(content)
    return json.dumps(block)


def message_tokens(message: dict[str, Any], count: TokenCounter = estimate_tokens) -> int:
    content = message["content"]
    if isinstance(content, str):
        return count(content)
    return sum(count(block_text(b)) for b in content)


def context_tokens(
    system: str, messages: tuple[dict, ...], count: TokenCounter = estimate_tokens
) -> int:
    return count(system) + sum(message_tokens(m, count) for m in messages)


# --- clearing -------------------------------------------------------------


def _tool_calls_by_id(messages: tuple[dict, ...]) -> dict[str, dict[str, Any]]:
    return {
        b["id"]: b
        for m in messages
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("type") == "tool_use"
    }


def _result_positions(messages: tuple[dict, ...]) -> list[tuple[int, int]]:
    return [
        (mi, bi)
        for mi, m in enumerate(messages)
        if m["role"] == "user" and isinstance(m["content"], list)
        for bi, b in enumerate(m["content"])
        if b.get("type") == "tool_result"
    ]


def _placeholder(call: dict[str, Any] | None, tokens: int) -> str:
    if call is None:
        return f"{CLEARED_PREFIX} tool result, {tokens:,} tokens]"
    arg = next((v for v in call["input"].values() if isinstance(v, str)), "")
    label = f"{call['name']} {arg[:60]}".strip()
    return f"{CLEARED_PREFIX} {label}, {tokens:,} tokens]"


def _replace_block(
    messages: tuple[dict, ...], mi: int, bi: int, new_block: dict[str, Any]
) -> tuple[dict, ...]:
    old = messages[mi]
    content = [*old["content"][:bi], new_block, *old["content"][bi + 1 :]]
    return (*messages[:mi], {**old, "content": content}, *messages[mi + 1 :])


def clear_tool_results(
    system: str, messages: tuple[dict, ...], target: int, policy: ContextPolicy
) -> tuple[dict, ...]:
    """Replace old tool-result contents, oldest first, until under `target`.

    The block itself stays, with its tool_use_id, so every call keeps its
    result. The most recent `keep_recent_results` results are never touched.
    """
    positions = _result_positions(messages)
    keep = policy.keep_recent_results
    candidates = positions[:-keep] if keep else positions
    calls = _tool_calls_by_id(messages)
    used = context_tokens(system, messages, policy.count)
    out = messages
    for mi, bi in candidates:
        if used <= target:
            break
        block = out[mi]["content"][bi]
        text = block_text(block)
        if text.startswith(CLEARED_PREFIX):
            continue
        before = policy.count(text)
        stub = _placeholder(calls.get(block["tool_use_id"]), before)
        out = _replace_block(out, mi, bi, {**block, "content": stub})
        used -= before - policy.count(stub)
    return out


# --- compaction -----------------------------------------------------------


def render_transcript(messages: tuple[dict, ...]) -> str:
    """Flatten history to plain text for the summarizer.

    Sending raw tool_use blocks would require sending tool schemas too, and
    invites the summarizer to call tools. Plain text avoids both.
    """
    lines = []
    for m in messages:
        if isinstance(m["content"], str):
            lines.append(f"{m['role'].upper()}: {m['content']}")
            continue
        for b in m["content"]:
            kind = b.get("type")
            if kind == "tool_use":
                lines.append(f"TOOL CALL: {block_text(b)}")
            elif kind == "tool_result":
                flag = " (error)" if b.get("is_error") else ""
                lines.append(f"TOOL RESULT{flag}: {block_text(b)}")
            else:
                lines.append(f"{m['role'].upper()}: {block_text(b)}")
    return "\n".join(lines)


def last_turns(messages: tuple[dict, ...], k: int) -> tuple[dict, ...]:
    """The tail starting at the k-th last assistant message.

    Cutting at an assistant message keeps each tool_use with its result,
    which always follows it.
    """
    starts = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    if k == 0 or not starts:
        return ()
    return messages[starts[-min(k, len(starts))] :]


def summarize(summarizer: ModelClient, messages: tuple[dict, ...]) -> tuple[str, int]:
    """One separate model call. Returns (summary text, tokens spent)."""
    request = [{"role": "user", "content": render_transcript(messages) + "\n\nSummarize now."}]
    turn = summarizer.complete(COMPACTION_PROMPT, request, [])
    spent = turn.input_tokens + turn.output_tokens
    if turn.tool_calls:
        raise CompactionError("summarizer tried to call tools")
    if turn.stop_reason == "max_tokens":
        raise CompactionError("summary hit the summarizer's max_tokens and is truncated")
    if not turn.text.strip():
        raise CompactionError("summarizer returned an empty summary")
    return turn.text.strip(), spent


def compact(
    system: str, messages: tuple[dict, ...], summarizer: ModelClient, policy: ContextPolicy
) -> tuple[tuple[dict, ...], int]:
    """Replace history with [summary, last few turns]. Returns (history, tokens spent)."""
    summary, spent = summarize(summarizer, messages)
    head = {"role": "user", "content": f"{SUMMARY_HEADER}\n\n{summary}"}
    for k in range(policy.keep_recent_turns, -1, -1):
        candidate = (head, *last_turns(messages, k))
        if context_tokens(system, candidate, policy.count) < policy.compact_tokens:
            return candidate, spent
    raise CompactionError("the summary alone is over the compaction threshold")
```

### `harness/managed.py`

```python
"""ManagedHarness: the exercise-01 Harness plus context management.

It only overrides the extension point that runs before each model call. The
loop, tools, gate, hooks, and stop conditions are reused unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from harness.context import ContextPolicy, clear_tool_results, compact, context_tokens
from harness.loop import Harness, LoopState
from harness.models import ModelClient


@dataclass(frozen=True)
class ManagedHarness(Harness):
    summarizer: ModelClient | None = None
    context: ContextPolicy = field(default_factory=ContextPolicy)

    def context_tokens(self, messages: tuple[dict, ...]) -> int:
        return context_tokens(self.system, messages, self.context.count)

    def _before_model_call(self, state: LoopState) -> LoopState:
        # Cheapest first: clearing loses only old raw output. Compaction
        # loses detail and costs a model call, so it runs only if still needed.
        if self.context_tokens(state.messages) >= self.context.clear_tokens:
            cleared = clear_tool_results(
                self.system, state.messages, self.context.clear_tokens, self.context
            )
            state = replace(state, messages=cleared)
        if self.context_tokens(state.messages) < self.context.compact_tokens:
            return state
        if self.summarizer is None:
            raise RuntimeError("context is over the compaction threshold and no summarizer is set")
        history, spent = compact(self.system, state.messages, self.summarizer, self.context)
        # The transcript keeps everything; only what the model sees shrinks.
        return replace(state, messages=history, tokens=state.tokens + spent)
```

### `harness/session.py`

```python
"""Session state on disk: features.json, progress.md, and git.

A session reads these files, works on one failing feature, flips its `passes`
flag, appends to progress.md, and commits. The harness, not the model, decides
what counts as a legal change to features.json.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from harness.context import ContextPolicy
from harness.hooks import PreToolHook, block_protected_writes, python_syntax_note
from harness.managed import ManagedHarness
from harness.models import ModelClient
from harness.permissions import Approver, RuleBasedPolicy
from harness.types import RunResult, ToolCall
from harness.workspace import PathEscapeError, Workspace

FEATURES_FILE = "features.json"
PROGRESS_FILE = "progress.md"
FEATURE_KEYS = frozenset({"id", "description", "passes"})


class FeatureError(ValueError):
    """features.json content is malformed."""


@dataclass(frozen=True)
class Feature:
    id: str
    description: str
    passes: bool = False


def _parse_feature(item: Any, index: int) -> Feature:
    if not isinstance(item, dict) or set(item) != FEATURE_KEYS:
        raise FeatureError(f"item {index} must have exactly the keys {sorted(FEATURE_KEYS)}")
    if not isinstance(item["id"], str) or not isinstance(item["description"], str):
        raise FeatureError(f"item {index}: id and description must be strings")
    if not isinstance(item["passes"], bool):
        raise FeatureError(f"item {index}: passes must be true or false")
    return Feature(item["id"], item["description"], item["passes"])


def parse_features(text: str) -> tuple[Feature, ...]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeatureError(f"not valid JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise FeatureError("top level must be a list")
    features = tuple(_parse_feature(item, i) for i, item in enumerate(data))
    ids = [f.id for f in features]
    if len(ids) != len(set(ids)):
        raise FeatureError("feature ids must be unique")
    return features


def dump_features(features: Iterable[Feature]) -> str:
    rows = [{"id": f.id, "description": f.description, "passes": f.passes} for f in features]
    return json.dumps(rows, indent=2) + "\n"


def transition_error(old_text: str, new_text: str) -> str | None:
    """None if new differs from old only in `passes` values; else why not."""
    try:
        new = parse_features(new_text)
    except FeatureError as exc:
        return f"features.json would become invalid: {exc}"
    old = parse_features(old_text)
    old_ids, new_ids = [f.id for f in old], [f.id for f in new]
    added, removed = set(new_ids) - set(old_ids), set(old_ids) - set(new_ids)
    if added:
        return f"adding features is not allowed: {sorted(added)}"
    if removed:
        return f"removing features is not allowed: {sorted(removed)}"
    if new_ids != old_ids:
        return "reordering features is not allowed"
    reworded = [o.id for o, n in zip(old, new, strict=True) if o.description != n.description]
    if reworded:
        return f"rewording features is not allowed: {reworded}"
    return None


def features_guard(workspace: Workspace) -> PreToolHook:
    """pre_tool hook: write_file to features.json may only flip `passes`."""
    target = workspace.root / FEATURES_FILE

    def guard_features(call: ToolCall) -> str | None:
        if call.name != "write_file":
            return None
        try:
            path = workspace.resolve(str(call.input.get("path", "")))
        except PathEscapeError:
            return None  # the tool rejects escapes itself
        if path != target:
            return None
        if not target.exists():
            return "features.json is created by the initializer, not by the agent"
        reason = transition_error(target.read_text(), str(call.input.get("content", "")))
        if reason is None:
            return None
        return f"{reason}. The only allowed change is flipping 'passes' on an existing feature."

    return guard_features


ALLOWED_GIT = frozenset({"status", "log", "diff", "add", "commit", "show"})


def git_subcommand_guard(call: ToolCall) -> str | None:
    """pre_tool hook: the agent may inspect and commit, not push or reset."""
    if call.name != "run_command":
        return None
    try:
        argv = shlex.split(str(call.input.get("command", "")))
    except ValueError:
        return None  # run_command reports the parse error
    if argv[:1] != ["git"]:
        return None
    sub = argv[1] if len(argv) > 1 else ""
    if sub in ALLOWED_GIT:
        return None
    return f"git {sub!r} is not allowed in a session; allowed: {sorted(ALLOWED_GIT)}"


# --- git (tolerant) ---------------------------------------------------------


def _git(workspace: Workspace, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in the workspace. None if git isn't installed."""
    try:
        return subprocess.run(
            ["git", *args], cwd=workspace.root, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return None


def read_git_log(workspace: Workspace) -> str:
    proc = _git(workspace, "log", "--oneline", "-10")
    if proc is None:
        return "(git not available; skipping history)"
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["unknown error"])[0]
        return f"(no git history: {first})"
    return proc.stdout.strip() or "(no commits yet)"


def _git_init(workspace: Workspace) -> str:
    if _git(workspace, "--version") is None:
        return "git not available; state files written without a commit"
    if (workspace.root / ".git").exists():
        return "git repository already present"
    steps = [
        ("init", "-q"),
        ("add", "-A"),
        ("commit", "-q", "-m", "chore: initialize session state"),
    ]
    for step in steps:
        proc = _git(workspace, *step)
        if proc is None or proc.returncode != 0:
            detail = proc.stderr.strip() if proc else "git disappeared"
            return f"git {step[0]} failed: {detail}"
    return "initialized git repository"


# --- initializer, loading, prompt -------------------------------------------


def initialize(workspace: Workspace, features: Iterable[tuple[str, str]]) -> str:
    """First run only: write features.json and an empty progress.md.

    Never overwrites existing state. Returns a one-line note for the operator.
    """
    path = workspace.root / FEATURES_FILE
    if path.exists():
        return "already initialized; leaving state files alone"
    rows = tuple(Feature(fid, desc) for fid, desc in features)
    if not rows:
        raise FeatureError("initializer needs at least one feature")
    text = dump_features(rows)
    parse_features(text)  # validates unique ids before anything touches disk
    path.write_text(text)
    (workspace.root / PROGRESS_FILE).write_text("")
    return _git_init(workspace)


@dataclass(frozen=True)
class SessionState:
    progress: str
    features: tuple[Feature, ...]
    git_log: str

    @property
    def next_feature(self) -> Feature | None:
        return next((f for f in self.features if not f.passes), None)


def load_session(workspace: Workspace) -> SessionState:
    features_path = workspace.root / FEATURES_FILE
    if not features_path.exists():
        raise FileNotFoundError(f"{FEATURES_FILE} not found; run the initializer first")
    progress_path = workspace.root / PROGRESS_FILE
    progress = progress_path.read_text() if progress_path.exists() else ""
    return SessionState(
        progress, parse_features(features_path.read_text()), read_git_log(workspace)
    )


def build_session_prompt(state: SessionState, feature: Feature) -> str:
    progress = state.progress.strip() or "(empty: this is the first working session)"
    return f"""You are continuing a multi-session task. Work on exactly one feature.

## Next feature
{feature.id}: {feature.description}

## {PROGRESS_FILE}
{progress}

## {FEATURES_FILE}
{dump_features(state.features).strip()}

## git log --oneline -10
{state.git_log}

## Rules
- Implement and test only feature {feature.id}.
- When it works, set its "passes" to true in {FEATURES_FILE}.
  No other edit to that file is accepted.
- Before you finish, append a short entry to {PROGRESS_FILE} (what you did, what is next),
  then commit with git add and git commit."""


# --- running a session --------------------------------------------------------


class SessionRunner(Protocol):
    """Anything with run(task) -> RunResult, e.g. Harness or ManagedHarness."""

    def run(self, task: str) -> RunResult: ...


@dataclass(frozen=True)
class SessionResult:
    feature_id: str | None
    run: RunResult | None
    violation: str | None = None


def _enforce_features(workspace: Workspace, before: str) -> str | None:
    """Backstop for writes the hook can't see (e.g. a script run via run_command)."""
    path = workspace.root / FEATURES_FILE
    after = path.read_text() if path.exists() else ""
    if after == before:
        return None
    reason = transition_error(before, after)
    if reason is not None:
        path.write_text(before)
        return f"{reason}; features.json was restored"
    return None


def run_session(harness: SessionRunner, workspace: Workspace) -> SessionResult:
    """Read state from disk, run one session on the next failing feature."""
    state = load_session(workspace)
    feature = state.next_feature
    if feature is None:
        return SessionResult(None, None)
    before = (workspace.root / FEATURES_FILE).read_text()
    try:
        result = harness.run(build_session_prompt(state, feature))
    finally:
        # Runs even if the session crashes, so a bad edit never survives.
        violation = _enforce_features(workspace, before)
    return SessionResult(feature.id, result, violation)


def session_harness(
    model: ModelClient,
    summarizer: ModelClient,
    workspace: Workspace,
    context: ContextPolicy | None = None,
    approver: Approver | None = None,
    max_turns: int = 40,
    token_budget: int = 400_000,
) -> ManagedHarness:
    """A ManagedHarness wired for sessions: state-file writes, git, guards."""
    policy = RuleBasedPolicy(
        workspace,
        write_prefixes=("src/", "tests/", FEATURES_FILE, PROGRESS_FILE),
        auto_programs=frozenset({"ls", "cat", "grep", "echo", "pytest", "git"}),
    )
    return ManagedHarness(
        model=model,
        workspace=workspace,
        policy=policy,
        approver=approver,
        pre_tool=(block_protected_writes, features_guard(workspace), git_subcommand_guard),
        post_tool=(python_syntax_note,),
        max_turns=max_turns,
        token_budget=token_budget,
        summarizer=summarizer,
        context=context or ContextPolicy(),
    )
```

### `harness/session_cli.py`

```python
"""Session entry point.

    python -m harness.session_cli --workspace DIR --init features.json   # first run
    python -m harness.session_cli --workspace DIR --show-prompt          # offline
    python -m harness.session_cli --workspace DIR                        # one live session

Every invocation is a fresh process: all it knows comes from the workspace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.cli import terminal_approver
from harness.models import AnthropicModel
from harness.session import (
    build_session_prompt,
    initialize,
    load_session,
    run_session,
    session_harness,
)
from harness.workspace import Workspace

SUMMARY_MAX_TOKENS = 1_024


def _init(workspace: Workspace, spec_path: str) -> int:
    rows = json.loads(Path(spec_path).read_text())
    note = initialize(workspace, [(r["id"], r["description"]) for r in rows])
    print(note)
    return 0


def _show_prompt(workspace: Workspace) -> int:
    state = load_session(workspace)
    feature = state.next_feature
    if feature is None:
        print("all features pass; nothing to do")
        return 0
    print(build_session_prompt(state, feature))
    return 0


def _run_live(workspace: Workspace) -> int:
    harness = session_harness(
        model=AnthropicModel(),
        summarizer=AnthropicModel(max_tokens=SUMMARY_MAX_TOKENS),
        workspace=workspace,
        approver=terminal_approver,
    )
    try:
        outcome = run_session(harness, workspace)
    except RuntimeError as exc:  # missing key or SDK: say so, no traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if outcome.run is None:
        print("all features pass; nothing to do")
        return 0
    print(f"\n{outcome.run.final_text}\n")
    run = outcome.run
    print(f"feature={outcome.feature_id} stop={run.stop_reason} tokens={run.total_tokens}")
    if outcome.violation:
        print(f"features.json violation: {outcome.violation}")
    return 0 if outcome.run.stop_reason == "done" and not outcome.violation else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one harness session from on-disk state.")
    parser.add_argument("--workspace", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", metavar="SPEC", help="JSON list of {id, description}")
    group.add_argument("--show-prompt", action="store_true", help="print the next prompt and exit")
    args = parser.parse_args(argv)
    workspace = Workspace.at(args.workspace)
    if args.init:
        return _init(workspace, args.init)
    if args.show_prompt:
        return _show_prompt(workspace)
    return _run_live(workspace)


if __name__ == "__main__":
    sys.exit(main())
```

### `tests/test_context.py`

```python
"""Token accounting, clearing at 60%, compaction at 85%."""

from __future__ import annotations

import pytest
from conftest import call, turn

from harness.context import (
    COMPACTION_PROMPT,
    SUMMARY_HEADER,
    CompactionError,
    ContextPolicy,
    clear_tool_results,
    compact,
    context_tokens,
    estimate_tokens,
)
from harness.managed import ManagedHarness
from harness.models import ScriptedModel
from harness.permissions import RuleBasedPolicy
from harness.types import ModelTurn

SUMMARY = (
    "Goal: add a parser.\nDecisions: stdlib only.\nFiles changed: src/parse.py\n"
    "Current state: tests/test_parse.py::test_empty fails.\nNext step: handle empty input."
)


def assert_paired(messages):
    """Every tool_use is answered in the very next message, and vice versa."""
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        ids = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
        if not ids:
            continue
        nxt = messages[i + 1]["content"]
        assert [b["tool_use_id"] for b in nxt if b["type"] == "tool_result"] == ids
    used = {
        b["id"]
        for m in messages
        if m["role"] == "assistant"
        for b in m["content"]
        if b["type"] == "tool_use"
    }
    answered = {
        b["tool_use_id"]
        for m in messages
        if isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    }
    assert used == answered


def managed(ws, turns, summary_turns=(), **policy):
    model, summarizer = ScriptedModel(turns), ScriptedModel(summary_turns)
    harness = ManagedHarness(
        model=model,
        workspace=ws,
        policy=RuleBasedPolicy(ws),
        summarizer=summarizer,
        context=ContextPolicy(**policy),
        system="sys",
    )
    return harness, model, summarizer


def test_estimate_and_context_tokens():
    assert estimate_tokens("") == 1 and estimate_tokens("a" * 400) == 100
    msgs = ({"role": "user", "content": "a" * 400},)
    assert context_tokens("b" * 40, msgs) == 110


def test_policy_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        ContextPolicy(clear_at=0.9, compact_at=0.8)


def test_clearing_triggers_at_first_threshold_and_keeps_recent(ws):
    for i in range(1, 6):
        (ws.root / f"big{i}.txt").write_text(f"{i}" * 4_000)  # 1,000 tokens each
    turns = [turn(call("read_file", path=f"big{i}.txt")) for i in range(1, 6)] + [turn(text="ok")]
    # limit 5,000: clear at 3,000, compact at 4,250.
    harness, model, summarizer = managed(ws, turns, context_limit=5_000, keep_recent_results=2)
    result = harness.run("read them all")

    assert result.stop_reason == "done"
    assert summarizer.requests == []  # clearing alone was enough
    below = model.requests[2]["messages"]  # ~2,000 tokens: untouched
    assert all("[cleared:" not in str(m["content"]) for m in below)

    sent = model.requests[3]["messages"]  # first request over 3,000
    results = [
        b
        for m in sent
        if isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_result"
    ]
    assert results[0]["content"] == "[cleared: read_file big1.txt, 1,000 tokens]"
    assert results[1]["content"] == "2" * 4_000 and results[2]["content"] == "3" * 4_000
    assert harness.context_tokens(sent) <= 3_000
    for request in model.requests:
        assert_paired(request["messages"])
    # The transcript is the full record; nothing in it was cleared.
    assert "[cleared:" not in str(result.transcript)


def test_clearing_never_touches_the_most_recent_results():
    big = "x" * 4_000
    msgs = (
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": big, "is_error": False}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "b", "name": "read_file", "input": {"path": "b"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "b", "content": big, "is_error": False}
            ],
        },
    )
    out = clear_tool_results("", msgs, target=10, policy=ContextPolicy(keep_recent_results=1))
    assert out[2]["content"][0]["content"] == "[cleared: read_file a, 1,000 tokens]"
    assert out[4]["content"][0]["content"] == big  # still over target, but kept
    assert msgs[2]["content"][0]["content"] == big  # input not mutated
    assert_paired(out)


def test_compaction_triggers_at_second_threshold(ws):
    chatter = "thinking out loud " * 120  # ~540 tokens of assistant text per turn
    # Alternate paths so loop detection stays out of the way.
    paths = [".", "src", ".", "src", "."]
    turns = [turn(call("list_dir", path=p), text=chatter) for p in paths] + [turn(text="done")]
    summary_turn = ModelTurn(SUMMARY, (), "end_turn", 900, 60)
    # limit 2,500: clear at 1,500, compact at 2,125.
    harness, model, summarizer = managed(
        ws, turns, [summary_turn], context_limit=2_500, keep_recent_turns=1
    )
    result = harness.run("build the parser")

    assert result.stop_reason == "done"
    assert len(summarizer.requests) == 1
    req = summarizer.requests[0]
    assert req["system"] == COMPACTION_PROMPT and req["tools"] == ()
    assert "TOOL CALL: list_dir" in req["messages"][0]["content"]

    after = next(
        r["messages"]
        for r in model.requests
        if r["messages"][0]["content"].startswith(SUMMARY_HEADER)
    )
    assert SUMMARY in after[0]["content"]
    assert [m["role"] for m in after] == ["user", "assistant", "user"]  # summary + last turn
    assert harness.context_tokens(after) < harness.context.compact_tokens
    assert_paired(after)
    assert result.total_tokens == 6 * 15 + 960  # main turns + the summary call
    assert len(result.transcript) == 12  # full record survives compaction


def _history(n: int):
    msgs = [{"role": "user", "content": "task"}]
    for i in range(n):
        msgs.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "list_dir", "input": {}}],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}],
            }
        )
    return tuple(msgs)


@pytest.mark.parametrize(
    "bad_turn, message",
    [
        (ModelTurn("half a summ", (), "max_tokens", 1, 1), "truncated"),
        (ModelTurn("", (), "end_turn", 1, 1), "empty"),
        (ModelTurn("x", (call("list_dir", path="."),), "tool_use", 1, 1), "call tools"),
    ],
)
def test_compaction_rejects_unusable_summaries(bad_turn, message):
    with pytest.raises(CompactionError, match=message):
        compact("sys", _history(3), ScriptedModel([bad_turn]), ContextPolicy())


def test_compaction_drops_recent_turns_until_under_threshold():
    policy = ContextPolicy(context_limit=100, keep_recent_turns=3)
    history, spent = compact(
        "sys", _history(30), ScriptedModel([ModelTurn("short", (), "end_turn", 5, 5)]), policy
    )
    assert spent == 10
    assert history[0]["content"].endswith("short")
    assert context_tokens("sys", history) < policy.compact_tokens
    assert_paired(history)


def test_missing_summarizer_is_a_clear_error(ws):
    harness = ManagedHarness(
        model=ScriptedModel([turn(text="x")]),
        workspace=ws,
        policy=RuleBasedPolicy(ws),
        context=ContextPolicy(context_limit=10),
    )
    with pytest.raises(RuntimeError, match="no summarizer"):
        harness.run("a task long enough to exceed a tiny limit")
```

### `tests/test_session.py`

```python
"""features.json guard, initializer, and resume from disk."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import call, turn

from harness.models import ScriptedModel, ScriptExhausted
from harness.session import (
    FEATURES_FILE,
    PROGRESS_FILE,
    initialize,
    load_session,
    read_git_log,
    run_session,
    session_harness,
)
from harness.tools import DEFAULT_PROGRAMS, default_registry, make_run_command

HAS_GIT = shutil.which("git") is not None
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEATURES = [
    ("f1", "parse an empty string"),
    ("f2", "parse a single number"),
    ("f3", "report errors"),
]


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    """Ignore the developer's git config (signing, hooks) and set an identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "harness-test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "harness@example.invalid")


def features_with(ws, **changes) -> list[dict]:
    rows = json.loads((ws.root / FEATURES_FILE).read_text())
    return [{**r, **changes.get(r["id"], {})} for r in rows]


def flip(ws, fid: str) -> str:
    return json.dumps(features_with(ws, **{fid: {"passes": True}}), indent=2)


def session(ws, *turns, **kw):
    model = ScriptedModel(turns)
    return session_harness(model, ScriptedModel([]), ws, **kw), model


def test_initializer_writes_state_once(ws):
    initialize(ws, FEATURES)
    rows = json.loads((ws.root / FEATURES_FILE).read_text())
    assert [r["id"] for r in rows] == ["f1", "f2", "f3"]
    assert all(r["passes"] is False for r in rows)
    assert (ws.root / PROGRESS_FILE).read_text() == ""
    assert initialize(ws, [("other", "x")]).startswith("already initialized")
    if HAS_GIT:
        assert "chore: initialize session state" in read_git_log(ws)


def test_initializer_without_git(ws, monkeypatch, tmp_path_factory):
    monkeypatch.setenv("PATH", str(tmp_path_factory.mktemp("empty-path")))
    assert initialize(ws, FEATURES).startswith("git not available")
    assert read_git_log(ws).startswith("(git not available")


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_git_log_outside_a_repo_is_tolerated(ws):
    assert read_git_log(ws).startswith("(no git history:")


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda rows: [*rows, {"id": "f4", "description": "new", "passes": False}], "adding"),
        (lambda rows: rows[:-1], "removing"),
        (lambda rows: [{**rows[0], "description": "parse anything"}, *rows[1:]], "rewording"),
        (lambda rows: [rows[1], rows[0], rows[2]], "reordering"),
        (lambda rows: [{**rows[0], "notes": "hi"}, *rows[1:]], "exactly the keys"),
        (lambda rows: [{**rows[0], "passes": "yes"}, *rows[1:]], "true or false"),
    ],
)
def test_illegal_features_edits_are_rejected(ws, mutate, reason):
    initialize(ws, FEATURES)
    before = (ws.root / FEATURES_FILE).read_text()
    harness, _ = session(ws)
    content = json.dumps(mutate(features_with(ws)))
    result = harness.execute(call("write_file", path="features.json", content=content))
    assert result.is_error and result.content.startswith("blocked by hook:")
    assert reason in result.content
    assert (ws.root / FEATURES_FILE).read_text() == before


def test_invalid_json_and_sneaky_paths_are_rejected(ws):
    initialize(ws, FEATURES)
    harness, _ = session(ws)
    bad = harness.execute(call("write_file", path="features.json", content="[{"))
    sneaky = harness.execute(call("write_file", path="src/../features.json", content="[]"))
    assert "not valid JSON" in bad.content
    assert sneaky.is_error and "removing" in sneaky.content


def test_flipping_passes_is_accepted(ws):
    initialize(ws, FEATURES)
    harness, _ = session(ws)
    result = harness.execute(call("write_file", path="features.json", content=flip(ws, "f2")))
    assert not result.is_error
    assert [f.passes for f in load_session(ws).features] == [False, True, False]


def test_agent_cannot_create_features_file(ws):
    harness, _ = session(ws)
    result = harness.execute(call("write_file", path="features.json", content="[]"))
    assert "created by the initializer" in result.content


def test_git_guard_blocks_push_allows_status(ws):
    harness, _ = session(ws)
    push = harness.execute(call("run_command", command="git push origin main"))
    assert push.is_error and "git 'push' is not allowed" in push.content
    status = harness.execute(call("run_command", command="git status"))
    assert "is not allowed" not in status.content


def test_backstop_restores_features_changed_outside_write_file(ws):
    initialize(ws, FEATURES)
    before = (ws.root / FEATURES_FILE).read_text()
    script = (
        "import json, pathlib\n"
        "p = pathlib.Path('features.json')\n"
        "rows = json.loads(p.read_text())\n"
        "rows.append({'id': 'f9', 'description': 'sneaky', 'passes': True})\n"
        "p.write_text(json.dumps(rows))\n"
    )
    harness, _ = session(
        ws,
        turn(call("write_file", path="src/sneak.py", content=script)),
        turn(call("run_command", command=f"{sys.executable} src/sneak.py")),
        turn(text="done"),
        approver=lambda c, r: True,
    )
    programs = DEFAULT_PROGRAMS | {sys.executable}
    harness = replace(harness, registry=default_registry(make_run_command(programs)))
    outcome = run_session(harness, ws)
    assert outcome.violation is not None and "adding features" in outcome.violation
    assert (ws.root / FEATURES_FILE).read_text() == before


@pytest.fixture
def killed_after_f1(ws):
    """Session 1 finishes f1, starts f2, then the process dies mid-run."""
    initialize(ws, FEATURES)
    commit = (
        [
            turn(call("run_command", command="git add -A")),
            turn(call("run_command", command='git commit -q -m "feat: f1 parse empty string"')),
        ]
        if HAS_GIT
        else []
    )
    harness, _ = session(
        ws,
        turn(call("write_file", path="src/parse.py", content="def parse(s):\n    return []\n")),
        turn(call("write_file", path="features.json", content=flip(ws, "f1"))),
        turn(
            call(
                "write_file",
                path="progress.md",
                content="## Session 1\n- f1 done in src/parse.py\n",
            )
        ),
        *commit,
        turn(call("write_file", path="src/parse.py", content="def parse(s):\n    # f2 half")),
    )
    with pytest.raises(ScriptExhausted):  # stands in for kill -9
        run_session(harness, ws)
    return ws


def test_fresh_harness_resumes_with_next_failing_feature(killed_after_f1):
    ws = killed_after_f1
    # Brand-new harness and model: no shared objects, no history.
    harness, model = session(
        ws,
        turn(call("read_file", path="src/parse.py")),
        turn(call("write_file", path="features.json", content=flip(ws, "f2"))),
        turn(text="f2 done"),
    )
    outcome = run_session(harness, ws)
    first = model.requests[0]["messages"]
    assert len(first) == 1  # only the session prompt, built from disk
    prompt = first[0]["content"]
    assert "## Next feature\nf2: parse a single number" in prompt
    assert "## Session 1" in prompt
    if HAS_GIT:
        assert "feat: f1 parse empty string" in prompt
    assert outcome.feature_id == "f2" and outcome.run.stop_reason == "done"
    assert outcome.violation is None
    assert load_session(ws).next_feature.id == "f3"


def test_fresh_process_reads_state_from_disk(killed_after_f1):
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.session_cli",
            "--workspace",
            str(killed_after_f1.root),
            "--show-prompt",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    assert "f2: parse a single number" in proc.stdout
    assert "f1 done in src/parse.py" in proc.stdout


def test_nothing_to_do_when_all_features_pass(ws):
    initialize(ws, [("f1", "only one")])
    (ws.root / FEATURES_FILE).write_text(flip(ws, "f1"))
    harness, model = session(ws)
    outcome = run_session(harness, ws)
    assert outcome.feature_id is None and outcome.run is None and model.requests == []
```

## Meeting the acceptance criteria

- **Clearing triggers at the first threshold, keeps the most recent N
  results, and leaves every call paired.**
  `test_clearing_triggers_at_first_threshold_and_keeps_recent` reads five
  1,000-token files against a 5,000-token limit. The request at about 2,000
  tokens is untouched. The first request over 3,000 has `big1.txt` cleared,
  the two most recent results intact, and context back under 3,000.
  `assert_paired` checks every request: each `tool_use` id is answered in the
  next message, and no result is orphaned. The summarizer is never called,
  and the transcript still holds the full output.
  `test_clearing_never_touches_the_most_recent_results` shows the protected
  results stay even when the target can't be reached, and that the input
  tuple isn't mutated.
- **Compaction triggers at the second threshold; the new history holds the
  summary and recent turns, under the threshold.**
  `test_compaction_triggers_at_second_threshold` builds up assistant text
  that clearing can't remove. It checks that the summarizer was called once
  with `COMPACTION_PROMPT` and no tools, that the next request is
  `[summary, last assistant turn, its results]`, that the result is under
  `compact_tokens` and paired, and that the summary call's tokens are in
  `total_tokens`. The system prompt is sent separately on every call.
  `test_compaction_rejects_unusable_summaries` covers truncated, empty, and
  tool-calling summaries.
  `test_compaction_drops_recent_turns_until_under_threshold` covers a tail
  too big to keep.
- **Adding, removing, or rewording a feature is rejected; flipping `passes`
  is accepted.** `test_illegal_features_edits_are_rejected` covers add,
  remove, reword, reorder, an extra key, and a non-boolean `passes`, and
  checks the file is unchanged. `test_flipping_passes_is_accepted` shows the
  legal edit going through.
  `test_invalid_json_and_sneaky_paths_are_rejected` covers malformed JSON and
  a `src/../features.json` path.
  `test_backstop_restores_features_changed_outside_write_file` writes the file
  through `run_command` and shows `run_session` restoring it.
- **A fresh process given only the workspace resumes with the next failing
  feature.** The `killed_after_f1` fixture runs session 1: it implements
  `f1`, flips it, appends to `progress.md`, commits (when git is present),
  starts `f2`, and then dies when the scripted model runs out of turns.
  `test_fresh_harness_resumes_with_next_failing_feature` builds a new harness
  and a new model, and shows that the first request has exactly one message,
  built from disk, naming `f2` and quoting session 1's progress and commit.
  It then finishes `f2` and leaves `f3` next.
  `test_fresh_process_reads_state_from_disk` runs
  `python -m harness.session_cli --show-prompt` as a real subprocess and gets
  the same answer.

## Common pitfalls

- **Deleting old tool results instead of clearing them.** Removing a
  `tool_result` block, or the `tool_use` that produced it, leaves the other
  half unpaired, and the next API call is rejected. Replace the content and
  keep both blocks.
- **Cutting the compaction tail in the middle of a turn.** If the kept turns
  start at a `tool_result` message, its `tool_use` is gone. Cut at an
  assistant message.
- **Compacting first.** Compaction costs a model call and loses detail.
  Clearing is free and loses only output the model already used. Clear
  first, and compact only if that wasn't enough.
- **Sending raw tool blocks to the summarizer.** The API requires tool
  definitions when the history contains tool blocks, and with tools defined
  the summarizer may try to call one. Flatten the history to text.
- **Accepting a truncated summary.** A summary cut off by `max_tokens`
  replaces a full history with half a note. Check `stop_reason` and fail
  loudly.
- **Guarding only `write_file`.** If the agent can run Python, it can write
  any file. The pre-tool hook gives fast feedback. The before/after check in
  `run_session` is what actually enforces the rule.
- **Letting the model create the feature list mid-run.** If the agent can
  write `features.json` from scratch, it can write a shorter one. Only the
  initializer creates it.
- **Resuming from in-memory state.** If the second session reuses the first
  harness object or its message history, you haven't tested resume. Build a
  new harness, or better, start a new process.
- **Tests that depend on your git config.** Commit signing or a global hook
  will break `git commit` in tests. The `isolated_git` fixture points
  `GIT_CONFIG_GLOBAL` at `/dev/null` and sets an identity.

## Verification

Start from the exercise-01 files, add the six new files above, then:

```bash
cd exercise-04-context-and-session-state
python3.11 -m venv .venv && . .venv/bin/activate
pip install pytest

# Offline: ScriptedModel for both the agent and the summarizer.
pytest -q

# Session lifecycle, one fresh process per step.
mkdir -p /tmp/session-ws/src /tmp/session-ws/tests
cat > /tmp/features-spec.json <<'JSON'
[
  {"id": "f1", "description": "src/slug.py: slugify(text) lowercases and joins words with '-'"},
  {"id": "f2", "description": "slugify drops characters that are not letters, digits, or spaces"},
  {"id": "f3", "description": "tests/test_slug.py covers both behaviors and passes under pytest"}
]
JSON
python -m harness.session_cli --workspace /tmp/session-ws --init /tmp/features-spec.json
python -m harness.session_cli --workspace /tmp/session-ws --show-prompt   # no model call

# Live sessions. Set a spend cap on the key first.
pip install anthropic
export ANTHROPIC_API_KEY=...            # never commit this
export HARNESS_MODEL=claude-sonnet-4-5  # check the current models list
python -m harness.session_cli --workspace /tmp/session-ws   # works on f1
# Kill the next one partway (Ctrl-C), then start it again:
python -m harness.session_cli --workspace /tmp/session-ws
python -m harness.session_cli --workspace /tmp/session-ws --show-prompt
git -C /tmp/session-ws log --oneline
```

Expected: `pytest -q` reports `59 passed` (32 from exercise-01, 27 new).
`--init` prints `initialized git repository` (or a note that git isn't
available). `--show-prompt` names `f1` before any session runs and names the
next failing feature after each one. After a killed session, the next run
starts from the files on disk: features that already pass stay done, and
`progress.md` and the git log show what the earlier sessions did.
