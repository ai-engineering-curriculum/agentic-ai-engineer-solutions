# mod-209-building-custom-harnesses/exercise-02-coding-tool-suite — Solution

## Approach

This solution extends the
[exercise-01 reference harness](../exercise-01-build-a-minimal-harness/README.md).
The registry, permission gate, hooks, and loop stay as they are. Seven modules
are added:

- `ledger.py` — the per-session read ledger.
- `files.py` — `read_file`, `edit_file`, `write_file`.
- `search.py` — `glob` and `grep`, with a ripgrep backend and a pure-Python one.
- `runners.py` — the `CommandRunner` protocol, `LocalRunner`, `DockerRunner`,
  and the config that picks one.
- `checkpoints.py` — git checkpoints in private refs, and `rewind`.
- `coding.py` — the `bash` tool, the registry that assembles the suite, and
  `CodingHarness`, which takes a checkpoint before every model call.
- `coding_cli.py` — a live entry point with `--sandbox`, `--list-checkpoints`,
  and `--rewind`.

Two exercise-01 files change, by a few lines each. `RuleBasedPolicy` treats
`glob` and `grep` as reads, `edit_file` as a write, and `bash` as a command.
`block_protected_writes` covers `edit_file` as well as `write_file`. Without
these changes, every new tool would fall through to "no rule for tool" and ask,
and an `edit_file` to `.env` would skip the guard.

Every tool is still a plain `fn(workspace, input) -> str`, as in exercise-01.
Tools that need session state (the ledger, the runner) get it through a
closure: `make_read_file(ledger)` returns a `Tool`. The loop doesn't change.

**`read_file` numbers lines and pages.** Each line comes back as
`<number, right-aligned to 6>\t<text>`, the format of `cat -n`. With numbers
the model can say "line 42" and page precisely. `offset` is a 1-based line
number and `limit` defaults to 2,000 lines. When a file is cut, the output ends
with `[showing lines 1-2000 of 2500. To read more, call read_file with
offset=2001.]`. That gives the model the exact next call, not just a warning.
Lines over 2,000 characters are clipped with a marker, because one minified
line can fill the context window. A file is binary if it has a NUL byte in its
first 8 KB (git uses the same test) or starts with a known magic number. For a
binary file the tool returns one line, such as
`logo.png is a binary file (PNG image, 108 bytes)`, never the bytes.

Lines are split on `\n` only. `str.splitlines()` also splits on form feeds and
other separators, so line numbers from `read_file` would disagree with the
line counts `edit_file` uses for its confirmation.

**`edit_file` replaces exact text.** Rewriting a whole file to change one line
costs output tokens for every unchanged line, and the model can silently drop
or alter code it was only meant to copy. Line-number edits go stale: once an
edit shifts the file, every later line number from the last read is wrong, and
nothing detects it. An exact-match edit names the text it expects to find. If
that text isn't there, or appears twice, the edit fails, and the error says
what to do: "old_string matches 2 places. Include more surrounding lines so it
matches one, or set replace_all." If `old_string` starts with a line-number
prefix (spaces, digits, then a tab), the error adds a hint: the model has copied `read_file`'s line-number
prefix. That is the most common way a model's edit misses.

The confirmation is the edited lines plus three lines of context on each side,
numbered. That's enough for the model to check its edit without re-reading the
whole file.

**Read-before-edit.** The `ReadLedger` records each file `read_file` returns,
with its `mtime_ns` and size. `edit_file` refuses a file that isn't in the
ledger ("you have not read src/app.py yet"), and a file whose mtime or size
changed since the read ("changed since you read it; read it again"). This stops
two failures. First, the model edits from memory, or from a guess, without
having seen the file. Second, a person or another process changes the file
after the model read it, and the model's edit, based on the old text, overwrites
their work. Size is checked as well as mtime because some filesystems store
mtime at one-second (or two-second) resolution, so two writes in the same tick
look identical by mtime alone.

After `edit_file` or `write_file` writes a file, it records the file again.
The harness's own write is not a "change since you read it", so the model can
make several edits in a row without re-reading in between.

The ledger is the one mutable object in the suite. It is session state that
the tools update as a side effect, like the filesystem. The `ReadRecord`s it
holds are frozen.

**`write_file` creates files.** It creates a new file freely. Overwriting an
existing file goes through the same ledger check as `edit_file`. The spec says
both "new files only" and "overwriting requires a prior read"; this solution
reads that as "creating is free, overwriting needs a fresh read".

**`glob` and `grep` share one walker and one set of rules.** `walk_files`
prunes `.git/`, `node_modules/`, `.venv/`, and `__pycache__/` at any depth, and
never follows symlinks, neither to files nor to directories. A symlink can point
outside the workspace, so following it would break containment. Every `path`
argument still goes through `Workspace.resolve`, so `path=".."` raises
`PathEscapeError`, and the loop turns that into an error result.

`glob` matches its pattern against paths relative to `path`, so `*.py` finds
top-level files only and `**/*.py` finds them at any depth. Results are sorted
newest first (ties broken by path, for stable output) and capped at 100, with
a note saying how many were left out. The model usually wants the file it just
changed or the one under active work.

`grep` has two backends, and the hard part is making them agree. Both return
the same data, a tuple of `FileHits(path, lines)`, where each `Line` has a
number, its text, and whether it matched or is context. One renderer turns that
into `files`, `count`, or `content` output. So parity is tested on data, and a
formatting difference can't hide a search difference. The rg backend runs
`rg --json` and parses it. JSON avoids ambiguity when a file name contains `:`,
and it reports context lines separately.

The rg flags pin down behavior that would otherwise depend on the machine:

- `--no-config` ignores `RIPGREP_CONFIG_PATH`, so a developer's smart-case
  setting can't change the results.
- `--hidden --no-ignore` turns off rg's own hidden-file and `.gitignore`
  filtering. The ignore list is ours: one explicit list, applied the same way
  by both backends.
- `--glob !node_modules/` (and the others) come last, because later globs win
  in rg.

These are the rg behaviors the Python fallback copies. Each was checked against
rg 15:

- Files with a NUL byte are skipped while walking a directory. The tool also
  refuses an explicitly named binary file, before either backend runs.
- A `glob` filter without `/` matches the file name at any depth. With `/`, it
  matches the path relative to the directory rg runs in, which is the
  workspace root. The filter applies only to files found by walking, never to a
  file named in `path`.
- An explicit `path` inside an ignored directory is still searched. The model
  asked for it.
- Lines split on `\n`. A trailing `\r` stays part of the line, so `hi$` does not
  match `hi\r\n` in either backend.

Regex dialects still differ at the edges. Python's `re` supports look-around and
Rust's `regex` doesn't. The tool compiles every pattern with `re` first, so an
invalid pattern gets the same error from both backends. A look-around pattern
works in the fallback and fails in rg, with rg's error message.

**`bash` decides whether; a `CommandRunner` decides where.** The `bash` tool
splits the command with `shlex`, checks the program against the exercise-01
allowlist, and calls `runner.run(argv, cwd, timeout, env)`. It passes the
harness's own environment, and the runner scrubs it. Scrubbing in the runner,
not the caller, means no code path can hand a runner the raw environment by
mistake. The tool is called `bash` because that is what models expect, but it
has no shell, and its description says so: pipes, redirects, `&&`, and `$VARS`
are literal arguments.

A non-zero exit is a normal result (`exit code: 1` and the output), because a
failing test run is information the model needs. A timeout is an error, with
any partial output attached, because the command didn't finish and its exit
code means nothing.

`execute`, shared by both runners, does four things `subprocess.run` doesn't
do by default:

- `stdin=DEVNULL`, so a command that waits for input gets EOF at once. It does
  not hang until the timeout.
- `start_new_session=True`, so the child gets its own process group.
- On timeout, kill the whole process group. `subprocess.run` kills only the
  direct child. A test runner's worker processes, or a server the tests
  started, would keep running.
- Exit code 127 for a missing program and 126 for a permission error, returned
  as results. These are the shell's conventions, and Docker reports the same
  codes, so both runners look alike.

stdout and stderr are kept separate and truncated head-and-tail with
exercise-01's `truncate_middle`.

**`LocalRunner` scrubs the environment with an allowlist:** `PATH`, `HOME`,
`LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `TZ`. A denylist (drop
`ANTHROPIC_API_KEY`) fails open: the next secret someone exports
(`AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`) leaks. An allowlist
fails closed.

Scrubbing is the only isolation `LocalRunner` provides. A malicious or
confused command can still:

- read anything your user can read, such as `~/.ssh`, `~/.aws/credentials`,
  browser profiles, and other repos;
- write or delete anything your user can write, anywhere (`python -c` makes the
  allowlist irrelevant here);
- reach the network: exfiltrate what it read, download and run code;
- use all CPU, memory, disk, or process slots;
- signal or kill your other processes;
- leave a background process running after the harness exits (`setsid` escapes
  the process-group kill).

**`DockerRunner` closes most of that.** Each command runs in a fresh container.
Each flag answers one item on the list above:

| Flag | What it stops |
|------|---------------|
| `--network none` | all network access: no exfiltration, no downloads. Only loopback exists. |
| `--mount type=bind,src=<workspace>,dst=/workspace` | the only host directory the container can see; `~/.ssh` doesn't exist inside |
| `--read-only` plus `--tmpfs /tmp` | writes anywhere except `/workspace` and a small, throwaway `/tmp` |
| `--cpus`, `--memory`, `--pids-limit` | resource exhaustion and fork bombs |
| `--user <uid>:<gid>` | root inside the container; files it creates in the workspace belong to you, not root |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | regaining privileges through setuid binaries or capabilities |
| `--rm`, plus a named container killed on timeout | leftover containers and processes |

The container gets `HOME=/tmp` and only `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`,
and `TZ` from the host. The host's `PATH` would be wrong inside the image. The
docker *client* runs on the host with its own small allowlist (`PATH`, `HOME`,
`DOCKER_HOST`, and similar) so it can find the daemon.

Killing the `docker run` client on timeout does not stop the container. It
keeps running with no one attached. So each container gets a random
`--name harness-<hex>`, and the timeout handler runs `docker kill <name>`
before it kills the client.

What Docker does not stop: damage inside `/workspace` (the checkpoints are for
that), kernel exploits that escape the container, and whatever is in the
image you choose. The harness picks a runner from `SandboxConfig(kind="local"
| "docker", image=...)`, and `coding_cli --sandbox docker` exposes it. The
default image is `python:3.12-slim`; use one that has your project's
toolchain, or `pytest` won't be found.

**Git checkpoints use a temporary index.** `create()` does this:

1. Copy the user's index file to a temp directory and point `GIT_INDEX_FILE` at
   the copy. Starting from a copy, not an empty index, lets git reuse its
   cached stat data, so only changed files are rehashed.
2. `git add --all -- .` into the copy, then `git write-tree`.
3. If the tree equals the latest checkpoint's tree, return that checkpoint.
   Nothing changed, so there's no new ref.
4. `git commit-tree --no-gpg-sign <tree> -p <previous checkpoint>`, with a fixed
   `harness` identity in the environment.
5. `git update-ref refs/harness/checkpoints/<n> <commit> ""`. The empty old
   value means "this ref must not exist yet", so two writers can't silently
   overwrite each other.

The user's index, branch, and HEAD are never written. `commit-tree` honors
`commit.gpgSign`, so without `--no-gpg-sign` a developer with signing turned
on would get a pinentry prompt, or a hang, at every turn. The harness identity
means checkpoints work in a repo with no `user.name` configured.

`rewind(n)` first takes a safety checkpoint of the current state, so a rewind
can itself be undone. It then builds a second temp index holding every current
file and runs `git restore --source=<checkpoint> --worktree -- .`. `restore` runs
in no-overlay mode by default: paths that are in the index but not in the
source are deleted. Because the temp index lists every current file, files
created after the checkpoint are removed, and empty directories go with them.
Modified and deleted files come back from the checkpoint. Only the worktree is
written; the real index is untouched.

**What a checkpoint covers.** The scope is what `git add -A` sees: tracked
files, plus untracked files that aren't ignored. Ignored files are outside the
scope in both directions. A checkpoint doesn't save them, and a rewind neither
restores nor deletes them. The alternative, deleting ignored files created
after the checkpoint, would delete a `.venv/` that `pip install` filled, build
caches, and a gitignored `.env`. Losing those costs more than leaving an
agent's stray `debug.log` behind. The cost of this choice: if the agent edits
an ignored file, rewind can't undo it. The protected-path hook already blocks
`.env*` for that reason.

If the workspace isn't in a git work tree, or git isn't installed,
`GitCheckpoints.for_workspace` issues a `RuntimeWarning` and returns a
disabled instance. `create()` returns `None` and the harness runs normally. If
the workspace is a subdirectory of a repo, every git command runs from the
workspace with the pathspec `.`, so a rewind only touches files under the
workspace.

**When checkpoints are taken.** The spec asks for one "before each model turn
that might change files". The harness can't know in advance whether the next
model turn will edit anything. So `CodingHarness` overrides exercise-01's
`_before_model_call` seam and checkpoints before every call, and the tree-hash
dedupe makes that cheap: a turn that only read files adds no ref. If a
checkpoint fails, the error propagates and the run stops. Carrying on without
the undo the user expects is worse than stopping.

## Reference implementation

Layout. Files marked *unchanged* are byte-for-byte the exercise-01 files; copy
them from the [exercise-01 solution](../exercise-01-build-a-minimal-harness/README.md).

```text
exercise-02-coding-tool-suite/
├── harness/
│   ├── __init__.py        # unchanged
│   ├── types.py           # unchanged
│   ├── workspace.py       # unchanged (every tool path still goes through resolve)
│   ├── schema.py          # unchanged
│   ├── models.py          # unchanged
│   ├── tools.py           # unchanged (Tool, ToolRegistry, truncate_middle, the allowlist)
│   ├── permissions.py     # CHANGED: knows glob/grep, edit_file, bash
│   ├── hooks.py           # CHANGED: protected paths also block edit_file
│   ├── loop.py            # unchanged (provides the _before_model_call seam)
│   ├── cli.py             # unchanged (coding_cli reuses its terminal approver)
│   ├── ledger.py          # NEW: ReadLedger
│   ├── files.py           # NEW: read_file, edit_file, write_file
│   ├── search.py          # NEW: glob, grep (rg + Python fallback)
│   ├── runners.py         # NEW: CommandRunner, LocalRunner, DockerRunner, config
│   ├── checkpoints.py     # NEW: git checkpoints and rewind
│   ├── coding.py          # NEW: bash tool, coding_registry, CodingHarness
│   └── coding_cli.py      # NEW: live entry point
├── tests/
│   ├── conftest.py        # unchanged
│   ├── test_tools.py      # unchanged
│   ├── test_loop.py       # unchanged
│   ├── test_models.py     # unchanged
│   ├── test_files.py      # NEW
│   ├── test_search.py     # NEW
│   ├── test_runners.py    # NEW
│   └── test_checkpoints.py  # NEW
└── pyproject.toml         # unchanged (no new dependencies)
```

The exercise-01 `read_file`, `write_file`, `list_dir`, and `run_command` are
still in `tools.py`, and the exercise-01 tests still run against them and pass.
The coding harness doesn't register them; `coding_registry` builds the new
suite instead. The new code is about 1,200 lines of package code and 840 lines
of tests.

### `harness/permissions.py`

Changed: three tool-name sets replace the two hard-coded names. The rest is as
in exercise-01.

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
    read_tools: frozenset[str] = frozenset({"read_file", "list_dir", "glob", "grep"})
    write_tools: frozenset[str] = frozenset({"write_file", "edit_file"})
    command_tools: frozenset[str] = frozenset({"run_command", "bash"})
    write_prefixes: tuple[str, ...] = ("src/", "tests/")
    auto_programs: frozenset[str] = frozenset({"ls", "cat", "grep", "echo", "pytest"})

    def decide(self, call: ToolCall) -> PermissionDecision:
        if call.name in self.read_tools:
            return PermissionDecision("allow", "reads are always allowed")
        if call.name in self.write_tools:
            return self._decide_write(str(call.input.get("path", "")))
        if call.name in self.command_tools:
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

Changed: `WRITE_TOOLS`, so `edit_file` is checked too.

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
WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def protected_path_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if any(d in parts for d in PROTECTED_DIRS):
        return f"writes under {PROTECTED_DIRS} are blocked: {path}"
    name = parts[-1] if parts else ""
    if any(fnmatch.fnmatch(name, g) for g in PROTECTED_GLOBS):
        return f"{name} is a protected file; edit it by hand"
    return None


def block_protected_writes(call: ToolCall) -> str | None:
    """pre_tool: refuse write_file/edit_file to .env*, .git/, secrets/, and lockfiles."""
    if call.name not in WRITE_TOOLS:
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

### `harness/ledger.py`

```python
"""The read ledger: which files the model has seen this session, and when.

edit_file and write_file consult it before touching an existing file. The
ledger is the one mutable object in the tool suite. It is session state that
tools update as a side effect, the same way they update the filesystem. The
records it holds are frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReadRecord:
    path: Path
    mtime_ns: int
    size: int


def _snapshot(path: Path) -> ReadRecord:
    stat = path.stat()
    return ReadRecord(path, stat.st_mtime_ns, stat.st_size)


class ReadLedger:
    """Maps resolved paths to the file's mtime and size when it was last read."""

    def __init__(self) -> None:
        self._records: dict[Path, ReadRecord] = {}

    def record(self, path: Path) -> None:
        """Call after a read, and after the harness itself writes the file."""
        self._records[path] = _snapshot(path)

    def has_read(self, path: Path) -> bool:
        return path in self._records

    def check_fresh(self, path: Path, display: str) -> str | None:
        """None if safe to edit, else an error message for the model."""
        seen = self._records.get(path)
        if seen is None:
            return f"you have not read {display} yet. Call read_file on it first, then edit."
        now = _snapshot(path)
        # Size catches changes that land within the filesystem's mtime resolution.
        if (now.mtime_ns, now.size) != (seen.mtime_ns, seen.size):
            return f"{display} changed since you read it; read it again before editing."
        return None
```

### `harness/files.py`

```python
"""File tools for a coding agent: numbered reads, exact-match edits, guarded writes.

Each public `make_*` function closes over a ReadLedger and returns a Tool. The
pure helpers (number_lines, apply_edit, is_binary) are tested on their own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.ledger import ReadLedger
from harness.tools import Tool, ToolError
from harness.workspace import Workspace

DEFAULT_READ_LIMIT = 2_000
MAX_LINE_CHARS = 2_000
BINARY_SNIFF_BYTES = 8_192
EDIT_CONTEXT_LINES = 3
_MAGIC = {
    b"\x89PNG": "PNG image",
    b"\xff\xd8\xff": "JPEG image",
    b"GIF8": "GIF image",
    b"%PDF": "PDF document",
    b"PK\x03\x04": "zip archive",
    b"\x7fELF": "ELF executable",
}
_LINE_PREFIX = re.compile(r"^\s*\d+\t", re.MULTILINE)


class EditError(ToolError):
    """old_string didn't identify exactly one place to change."""


# --- pure helpers -----------------------------------------------------------


def is_binary(head: bytes) -> bool:
    """Same test git uses: a NUL byte near the start means binary."""
    return b"\0" in head[:BINARY_SNIFF_BYTES] or head.startswith(tuple(_MAGIC))


def describe_binary(rel: str, data: bytes) -> str:
    kind = next((name for magic, name in _MAGIC.items() if data.startswith(magic)), "binary data")
    return f"{rel} is a binary file ({kind}, {len(data):,} bytes). Its contents are not shown."


def clip_line(line: str, limit: int = MAX_LINE_CHARS) -> str:
    if len(line) <= limit:
        return line
    return f"{line[:limit]} [... line truncated, {len(line) - limit:,} more chars]"


def text_lines(text: str) -> list[str]:
    """Split on newline only, so numbering agrees with edit_snippet. Drop a CR."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines = lines[:-1]
    return [line.removesuffix("\r") for line in lines]


def number_lines(lines: list[str], first: int) -> str:
    """cat -n style: right-aligned line number, a tab, then the line."""
    return "\n".join(f"{first + i:>6}\t{clip_line(line)}" for i, line in enumerate(lines))


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> str:
    count = text.count(old)
    if count == 0:
        hint = ""
        if _LINE_PREFIX.search(old):
            hint = " It looks like you included read_file's line-number prefix; drop it."
        raise EditError(
            "old_string not found. Read the file again and copy the exact text, "
            f"including whitespace.{hint}"
        )
    if count > 1 and not replace_all:
        raise EditError(
            f"old_string matches {count} places. Include more surrounding lines so it "
            "matches one, or set replace_all."
        )
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def edit_snippet(updated: str, start: int, new: str) -> str:
    """A few numbered lines around the first replacement, not the whole file."""
    lines = updated.split("\n")
    first_changed = updated.count("\n", 0, start)
    last_changed = first_changed + new.rstrip("\n").count("\n")  # a trailing newline isn't a line
    lo = max(0, first_changed - EDIT_CONTEXT_LINES)
    hi = min(len(lines), last_changed + EDIT_CONTEXT_LINES + 1)
    return number_lines(lines[lo:hi], lo + 1)


# --- read_file --------------------------------------------------------------


def _page(lines: list[str], offset: int, limit: int, rel: str) -> str:
    total = len(lines)
    if total == 0:
        return f"{rel} is empty."
    if offset > total:
        raise ToolError(f"offset {offset} is past the end of {rel}, which has {total} lines.")
    shown = lines[offset - 1 : offset - 1 + limit]
    body = number_lines(shown, offset)
    last = offset + len(shown) - 1
    if last < total:
        body += (
            f"\n\n[showing lines {offset}-{last} of {total}. "
            f"To read more, call read_file with offset={last + 1}.]"
        )
    return body


def make_read_file(ledger: ReadLedger) -> Tool:
    def read_file(ws: Workspace, args: dict[str, Any]) -> str:
        path, rel = ws.resolve(args["path"]), ws.relative(args["path"])
        if not path.is_file():
            raise ToolError(f"not a file: {rel}")
        offset, limit = args.get("offset", 1), args.get("limit", DEFAULT_READ_LIMIT)
        if offset < 1 or limit < 1:
            raise ToolError("offset and limit must be 1 or more (offset is a 1-based line number)")
        data = path.read_bytes()
        if is_binary(data):
            return describe_binary(rel, data)
        ledger.record(path)
        return _page(text_lines(data.decode("utf-8", errors="replace")), offset, limit, rel)

    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based line to start at"},
            "limit": {"type": "integer", "description": f"max lines, default {DEFAULT_READ_LIMIT}"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    desc = (
        "Read a text file. Lines come back as '<line number>\\t<text>'; the number and tab "
        "are not part of the file. Use offset and limit to page through long files. "
        "You must read a file before you edit or overwrite it."
    )
    return Tool("read_file", desc, schema, read_file)


# --- edit_file and write_file -----------------------------------------------


def _read_text_strict(path: Path, rel: str) -> str:
    data = path.read_bytes()
    if is_binary(data):
        raise ToolError(f"{rel} is a binary file; edit_file only edits text.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{rel} is not valid UTF-8; edit_file only edits UTF-8 text.") from exc


def _check_edit_args(old: str, new: str) -> None:
    if old == "":
        raise EditError("old_string is empty. To create a file use write_file.")
    if old == new:
        raise EditError("old_string and new_string are identical; there is nothing to change.")


def make_edit_file(ledger: ReadLedger) -> Tool:
    def edit_file(ws: Workspace, args: dict[str, Any]) -> str:
        path, rel = ws.resolve(args["path"]), ws.relative(args["path"])
        if not path.is_file():
            raise ToolError(f"{rel} does not exist. Use write_file to create a new file.")
        stale = ledger.check_fresh(path, rel)
        if stale is not None:
            raise ToolError(stale)
        old, new = args["old_string"], args["new_string"]
        _check_edit_args(old, new)
        text = _read_text_strict(path, rel)
        replace_all = args.get("replace_all", False)
        try:
            updated = apply_edit(text, old, new, replace_all)
        except EditError as exc:
            raise EditError(f"{rel}: {exc}") from exc
        path.write_bytes(updated.encode("utf-8"))
        ledger.record(path)  # our own write is not a "change since you read it"
        count = text.count(old) if replace_all else 1
        snippet = edit_snippet(updated, text.find(old), new)
        return f"edited {rel}: replaced {count} occurrence(s).\n{snippet}"

    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "exact text to replace"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "replace every match"},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    }
    desc = (
        "Replace exact text in a file you have read. old_string must match the file "
        "byte for byte (whitespace included, no line-number prefix) and must match "
        "exactly one place unless replace_all is true. Include surrounding lines to make "
        "it unique."
    )
    return Tool("edit_file", desc, schema, edit_file)


def make_write_file(ledger: ReadLedger) -> Tool:
    def write_file(ws: Workspace, args: dict[str, Any]) -> str:
        path, rel = ws.resolve(args["path"]), ws.relative(args["path"])
        if path.is_dir():
            raise ToolError(f"{rel} is a directory.")
        existed = path.exists()
        if existed:
            stale = ledger.check_fresh(path, rel)
            if stale is not None:
                raise ToolError(f"{stale} (write_file only overwrites files you have read.)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(args["content"].encode("utf-8"))
        ledger.record(path)
        verb = "overwrote" if existed else "created"
        return f"{verb} {rel} ({len(args['content'])} chars)"

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    desc = (
        "Create a new file. To change an existing file use edit_file. Overwriting an "
        "existing file is allowed only after you have read it."
    )
    return Tool("write_file", desc, schema, write_file)
```

### `harness/search.py`

```python
"""glob and grep. grep uses ripgrep when installed and pure Python otherwise.

Both grep backends return the same data (a tuple of FileHits), and one
renderer turns it into text. Parity is tested on the data, not on two
hand-written output formats.

Shared rules, so the backends agree:
- skip IGNORED_DIRS anywhere in the tree, plus symlinks (never followed);
- include hidden files and ignore .gitignore (the ignore list is ours, explicit);
- skip binary files (a NUL byte anywhere);
- a `glob` filter without '/' matches the file name; with '/', the
  workspace-relative path. That is how rg treats --glob when run from the root.
  It filters files found by walking a directory, never a file named in `path`;
- an explicit `path` inside an ignored directory is searched (the model asked).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from harness.files import clip_line
from harness.tools import Tool, ToolError
from harness.workspace import Workspace

IGNORED_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
GLOB_LIMIT = 100
GREP_LINE_LIMIT = 200
GREP_LINE_CHARS = 500
RG_TIMEOUT_S = 30
OUTPUT_MODES = ("files", "content", "count")


# --- walking and glob patterns ----------------------------------------------


def walk_files(base: Path) -> Iterator[Path]:
    """Regular files under base, sorted, skipping ignored dirs and all symlinks."""
    for dirpath, dirnames, filenames in os.walk(base):
        # os.walk prunes by in-place edits to dirnames; that is its API.
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORED_DIRS and not Path(dirpath, d).is_symlink()
        )
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if not path.is_symlink() and path.is_file():
                yield path


def _glob_token(p: str, i: int) -> tuple[str, int]:
    if p.startswith("**/", i):
        return "(?:.*/)?", i + 3
    if p.startswith("**", i):
        return ".*", i + 2
    c, close = p[i], {"[": "]", "{": "}"}.get(p[i])
    end = p.find(close, i + 1) if close else -1
    if c == "*":
        return "[^/]*", i + 1
    if c == "?":
        return "[^/]", i + 1
    if c == "[" and end != -1:
        body = p[i + 1 : end]
        body = "^" + body[1:] if body.startswith("!") else body
        return "[" + body.replace("\\", "\\\\") + "]", end + 1
    if c == "{" and end != -1:
        alts = p[i + 1 : end].split(",")
        return "(?:" + "|".join(_translate(a) for a in alts) + ")", end + 1
    return re.escape(c), i + 1


def _translate(pattern: str) -> str:
    parts, i = [], 0
    while i < len(pattern):
        piece, i = _glob_token(pattern, i)
        parts.append(piece)
    return "".join(parts)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay within one path segment, `**/` spans zero or more, `{a,b}` alternates."""
    return re.compile(_translate(pattern))


def _cap(lines: list[str], limit: int, what: str) -> str:
    if len(lines) <= limit:
        return "\n".join(lines)
    rest = len(lines) - limit
    return "\n".join([*lines[:limit], f"[{rest} more {what} not shown; narrow the search]"])


# --- glob tool ----------------------------------------------------------------


def make_glob(limit: int = GLOB_LIMIT) -> Tool:
    def glob(ws: Workspace, args: dict[str, Any]) -> str:
        base, rel = ws.resolve(args.get("path", ".")), ws.relative(args.get("path", "."))
        if not base.is_dir():
            raise ToolError(f"not a directory: {rel}")
        rx = glob_to_regex(args["pattern"])
        found = [p for p in walk_files(base) if rx.fullmatch(p.relative_to(base).as_posix())]
        if not found:
            return f"no files match {args['pattern']!r} under {rel}"
        newest = sorted(found, key=lambda p: (-p.stat().st_mtime_ns, p.as_posix()))
        return _cap([p.relative_to(ws.root).as_posix() for p in newest], limit, "results")

    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "e.g. '**/*.py' or 'src/*.ts'"},
            "path": {"type": "string", "description": "directory to search, default '.'"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    desc = (
        "Find files by glob pattern, matched against paths relative to `path`. "
        "Use '**/' to match any depth. Returns workspace-relative paths, newest first. "
        f"Skips {', '.join(sorted(IGNORED_DIRS))}."
    )
    return Tool("glob", desc, schema, glob)


# --- grep: shared data --------------------------------------------------------


@dataclass(frozen=True)
class Line:
    number: int
    text: str
    is_match: bool


@dataclass(frozen=True)
class FileHits:
    path: str  # workspace-relative, POSIX
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class GrepQuery:
    pattern: str
    path: str  # workspace-relative
    glob: str | None = None
    context: int = 0


def _glob_filter(glob: str | None, rel: str) -> bool:
    if glob is None:
        return True
    target = rel if "/" in glob else PurePosixPath(rel).name
    return glob_to_regex(glob).fullmatch(target) is not None


def _split(text: str) -> list[str]:
    """Split on '\\n' only, as rg does. A trailing newline ends the last line."""
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _hits_in(lines: list[str], rx: re.Pattern[str], context: int) -> tuple[Line, ...]:
    matches = {i for i, line in enumerate(lines) if rx.search(line)}
    keep = sorted(
        {j for i in matches for j in range(max(0, i - context), min(len(lines), i + context + 1))}
    )
    return tuple(Line(j + 1, lines[j], j in matches) for j in keep)


# --- grep: backends -----------------------------------------------------------


def _candidates(ws: Workspace, q: GrepQuery) -> list[Path]:
    base = ws.resolve(q.path)
    if base.is_file():
        return [base]  # like rg: a file named explicitly ignores the glob filter
    return [p for p in walk_files(base) if _glob_filter(q.glob, p.relative_to(ws.root).as_posix())]


def search_python(ws: Workspace, q: GrepQuery, rx: re.Pattern[str]) -> tuple[FileHits, ...]:
    results = []
    for path in _candidates(ws, q):
        data = path.read_bytes()
        if b"\0" in data:
            continue
        lines = _hits_in(_split(data.decode("utf-8", errors="replace")), rx, q.context)
        if lines:
            results.append(FileHits(path.relative_to(ws.root).as_posix(), lines))
    return tuple(sorted(results, key=lambda h: h.path))


def _rg_argv(q: GrepQuery) -> list[str]:
    argv = ["rg", "--json", "--no-config", "--hidden", "--no-ignore", "-C", str(q.context)]
    if q.glob is not None:
        argv += ["--glob", q.glob]
    for d in sorted(IGNORED_DIRS):  # later globs win in rg, so excludes go last
        argv += ["--glob", f"!{d}/"]
    return [*argv, "--regexp", q.pattern, "--", q.path]


def _rg_text(field: dict[str, str]) -> str:
    if "text" in field:
        return field["text"]
    return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")


def _parse_rg(stdout: str) -> tuple[FileHits, ...]:
    by_path: dict[str, list[Line]] = {}
    for raw in stdout.splitlines():
        event = json.loads(raw)
        if event["type"] not in ("match", "context"):
            continue
        data = event["data"]
        path = PurePosixPath(_rg_text(data["path"])).as_posix()
        text = _rg_text(data["lines"]).removesuffix("\n")
        line = Line(data["line_number"], text, event["type"] == "match")
        by_path[path] = [*by_path.get(path, []), line]
    return tuple(FileHits(p, tuple(ls)) for p, ls in sorted(by_path.items()))


def search_rg(ws: Workspace, q: GrepQuery) -> tuple[FileHits, ...]:
    try:
        proc = subprocess.run(
            _rg_argv(q),
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"grep timed out after {RG_TIMEOUT_S}s; narrow the path or glob") from exc
    if proc.returncode not in (0, 1) and not proc.stdout:
        raise ToolError(f"grep failed: {proc.stderr.strip()}")
    return _parse_rg(proc.stdout)


# --- grep: rendering and the tool ---------------------------------------------


def _content_lines(hits: tuple[FileHits, ...], context: int) -> list[str]:
    out: list[str] = []
    for hit in hits:
        prev = None
        for line in hit.lines:
            if context and out and line.number - 1 != prev:
                out.append("--")  # rg's separator between non-adjacent groups
            sep = ":" if line.is_match else "-"
            text = clip_line(line.text.removesuffix("\r"), GREP_LINE_CHARS)
            out.append(f"{hit.path}{sep}{line.number}{sep}{text}")
            prev = line.number
    return out


def render_hits(hits: tuple[FileHits, ...], output: str, context: int) -> str:
    if output == "files":
        lines = [h.path for h in hits]
    elif output == "count":
        lines = [f"{h.path}:{sum(ln.is_match for ln in h.lines)}" for h in hits]
    else:
        lines = _content_lines(hits, context)
    return _cap(lines, GREP_LINE_LIMIT, "lines")


def _query(ws: Workspace, args: dict[str, Any]) -> tuple[GrepQuery, re.Pattern[str]]:
    if args.get("output", "files") not in OUTPUT_MODES:
        raise ToolError(f"output must be one of {OUTPUT_MODES}, got {args['output']!r}")
    if args.get("context", 0) < 0:
        raise ToolError("context must be 0 or more")
    try:
        rx = re.compile(args["pattern"])
    except re.error as exc:
        raise ToolError(f"invalid regex {args['pattern']!r}: {exc}") from exc
    rel = ws.relative(args.get("path", "."))
    return GrepQuery(args["pattern"], rel, args.get("glob"), args.get("context", 0)), rx


def make_grep(use_rg: bool | None = None) -> Tool:
    """use_rg=None picks rg when it is on PATH. True/False force a backend (tests)."""
    rg = shutil.which("rg") is not None if use_rg is None else use_rg
    if rg and shutil.which("rg") is None:
        raise ValueError("use_rg=True but rg is not installed")

    def grep(ws: Workspace, args: dict[str, Any]) -> str:
        q, rx = _query(ws, args)
        target = ws.resolve(q.path)
        if not target.exists():
            raise ToolError(f"no such file or directory: {q.path}")
        if target.is_file() and b"\0" in target.read_bytes():
            return f"{q.path} is a binary file; not searched."
        hits = search_rg(ws, q) if rg else search_python(ws, q, rx)
        if not hits:
            return f"no matches for {q.pattern!r} in {q.path}"
        return render_hits(hits, args.get("output", "files"), q.context)

    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regular expression"},
            "path": {"type": "string", "description": "file or directory, default '.'"},
            "glob": {"type": "string", "description": "file filter, e.g. '*.py'"},
            "output": {"type": "string", "enum": list(OUTPUT_MODES)},
            "context": {"type": "integer", "description": "lines around each match"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    desc = (
        "Search file contents with a regular expression. output='files' (default) lists "
        "matching files; 'content' shows path:line:text; 'count' shows matches per file. "
        "glob filters files: without '/' it matches the file name, with '/' the "
        f"workspace-relative path. Skips binary files and {', '.join(sorted(IGNORED_DIRS))}."
    )
    return Tool("grep", desc, schema, grep)
```

### `harness/runners.py`

```python
"""Command runners: where the bash tool's commands actually execute.

The bash tool decides *whether* a command may run (parse, allowlist). A
CommandRunner decides *where and how* (host or container, which environment,
which limits). Swapping LocalRunner for DockerRunner changes isolation and
nothing else.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from harness.tools import MAX_OUTPUT_CHARS, truncate_middle

# Only these reach a local child. Everything else (API keys, cloud tokens,
# SSH agent sockets) is dropped. An allowlist fails safe; a denylist doesn't.
LOCAL_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ"}
)
# Inside a container the host's PATH and HOME are wrong, so they are not passed.
CONTAINER_ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"})
# What the docker *client* on the host needs to find the daemon.
DOCKER_CLIENT_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "XDG_RUNTIME_DIR",
    }
)
DEFAULT_IMAGE = "python:3.12-slim"


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> CommandResult: ...


def scrub_env(env: Mapping[str, str], allowlist: frozenset[str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in allowlist}


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the child and everything it started (pytest workers, servers)."""
    with contextlib.suppress(ProcessLookupError):  # already gone
        os.killpg(proc.pid, signal.SIGKILL)


def execute(
    argv: list[str],
    cwd: Path,
    timeout: float,
    env: dict[str, str],
    max_chars: int = MAX_OUTPUT_CHARS,
    on_timeout: Callable[[], None] = lambda: None,
) -> CommandResult:
    """Run argv with no shell, no stdin, its own process group, and a deadline."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,  # a command waiting for input gets EOF, not a hang
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # own process group, so a timeout kills the whole tree
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"program not found: {argv[0]}", False, 0.0)
    except PermissionError:
        return CommandResult(126, "", f"permission denied running {argv[0]}", False, 0.0)
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        on_timeout()
        _kill_group(proc)
        out, err = proc.communicate()
        timed_out = True
    return CommandResult(
        exit_code=proc.returncode,
        stdout=truncate_middle(out, max_chars),
        stderr=truncate_middle(err, max_chars),
        timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 3),
    )


@dataclass(frozen=True)
class LocalRunner:
    """Runs on the host. Scrubs the environment; does not isolate anything else."""

    env_allowlist: frozenset[str] = LOCAL_ENV_ALLOWLIST
    max_chars: int = MAX_OUTPUT_CHARS

    def run(
        self, argv: Sequence[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> CommandResult:
        child_env = scrub_env(env, self.env_allowlist)
        return execute(list(argv), cwd, timeout, child_env, self.max_chars)


@dataclass(frozen=True)
class DockerRunner:
    """Runs each command in a fresh, network-less container with the workspace mounted."""

    workspace: Path
    image: str = DEFAULT_IMAGE
    cpus: str = "1"
    memory: str = "512m"
    pids_limit: int = 256
    tmp_size: str = "64m"
    env_allowlist: frozenset[str] = CONTAINER_ENV_ALLOWLIST
    max_chars: int = MAX_OUTPUT_CHARS
    docker: str = "docker"

    def docker_argv(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], name: str
    ) -> list[str]:
        rel = cwd.resolve().relative_to(self.workspace.resolve())  # ValueError if outside
        flags = [
            "run", "--rm", "--name", name,
            "--network", "none",
            "--read-only", "--tmpfs", f"/tmp:rw,size={self.tmp_size}",
            "--cpus", self.cpus, "--memory", self.memory, "--pids-limit", str(self.pids_limit),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,src={self.workspace.resolve()},dst=/workspace",
            "--workdir", str(PurePosixPath("/workspace", rel.as_posix())),
            "--env", "HOME=/tmp",
        ]  # fmt: skip
        for key, value in sorted(scrub_env(env, self.env_allowlist).items()):
            flags += ["--env", f"{key}={value}"]
        return [self.docker, *flags, self.image, *argv]

    def run(
        self, argv: Sequence[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> CommandResult:
        name = f"harness-{uuid.uuid4().hex[:12]}"
        client_env = scrub_env(os.environ, DOCKER_CLIENT_ENV)
        return execute(
            self.docker_argv(argv, cwd, env, name),
            self.workspace,
            timeout,
            client_env,
            self.max_chars,
            # Killing the docker client does not stop the container. Kill it by name.
            on_timeout=lambda: self._kill(name, client_env),
        )

    def _kill(self, name: str, env: dict[str, str]) -> None:
        # Best effort: execute() kills the client next, and --rm cleans up on exit.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            kill = [self.docker, "kill", name]
            subprocess.run(kill, env=env, capture_output=True, timeout=30, check=False)


# --- config -------------------------------------------------------------------

SandboxKind = Literal["local", "docker"]


@dataclass(frozen=True)
class SandboxConfig:
    kind: SandboxKind = "local"
    image: str = DEFAULT_IMAGE
    cpus: str = "1"
    memory: str = "512m"


def make_runner(config: SandboxConfig, workspace: Path) -> CommandRunner:
    if config.kind == "local":
        return LocalRunner()
    if config.kind == "docker":
        return DockerRunner(workspace, config.image, config.cpus, config.memory)
    raise ValueError(f"unknown sandbox kind: {config.kind!r}")


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0
```

### `harness/checkpoints.py`

```python
"""Git checkpoints in private refs, so any agent turn can be rolled back.

A checkpoint is a commit of the working tree stored at
refs/harness/checkpoints/<n>. It is built in a temporary index
(GIT_INDEX_FILE), so the user's index, branch, and HEAD never move.

Scope: every file git would see with `git add -A` from the workspace root,
which means tracked files plus untracked files that are not ignored. Ignored
files (.venv/, build output, a gitignored .env) are outside the scope: a
checkpoint doesn't save them and a rewind doesn't touch them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REF_PREFIX = "refs/harness/checkpoints/"
GIT_TIMEOUT_S = 60
# Checkpoints are the harness's commits, not the user's, and must not depend
# on the user having an identity configured.
HARNESS_IDENTITY = {
    "GIT_AUTHOR_NAME": "harness",
    "GIT_AUTHOR_EMAIL": "harness@localhost",
    "GIT_COMMITTER_NAME": "harness",
    "GIT_COMMITTER_EMAIL": "harness@localhost",
}


class CheckpointError(RuntimeError):
    """A git command failed, or the requested checkpoint doesn't exist."""


@dataclass(frozen=True)
class Checkpoint:
    n: int
    commit: str
    tree: str


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise CheckpointError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _disabled_reason(root: Path) -> str | None:
    if shutil.which("git") is None:
        return "git is not installed"
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return f"{root} is not inside a git work tree"
    return None


@contextmanager
def _temp_index(root: Path) -> Iterator[dict[str, str]]:
    """An env whose GIT_INDEX_FILE is a throwaway copy of the user's index.

    Starting from a copy (not an empty index) lets git reuse cached stat data,
    so `git add -A` rehashes only files that changed.
    """
    real = root / _git(root, "rev-parse", "--git-path", "index")
    with tempfile.TemporaryDirectory(prefix="harness-index-") as tmp:
        index = Path(tmp, "index")
        if real.exists():
            shutil.copyfile(real, index)
        yield {**os.environ, **HARNESS_IDENTITY, "GIT_INDEX_FILE": str(index)}


@dataclass(frozen=True)
class GitCheckpoints:
    root: Path
    enabled: bool
    warning: str = ""

    @classmethod
    def for_workspace(cls, root: Path) -> GitCheckpoints:
        """Enabled in a git work tree. Elsewhere: disabled, with a warning, no crash."""
        reason = _disabled_reason(root)
        if reason is None:
            return cls(root, True)
        warnings.warn(f"checkpoints disabled: {reason}", RuntimeWarning, stacklevel=2)
        return cls(root, False, reason)

    def list(self) -> tuple[Checkpoint, ...]:
        if not self.enabled:
            return ()
        fmt = "--format=%(refname) %(objectname) %(tree)"
        rows = [
            line.split() for line in _git(self.root, "for-each-ref", fmt, REF_PREFIX).splitlines()
        ]
        found = (Checkpoint(int(ref.removeprefix(REF_PREFIX)), c, t) for ref, c, t in rows)
        return tuple(sorted(found, key=lambda cp: cp.n))

    def _snapshot_tree(self, env: dict[str, str]) -> str:
        _git(self.root, "add", "--all", "--", ".", env=env)
        return _git(self.root, "write-tree", env=env)

    def create(self, label: str = "") -> Checkpoint | None:
        """Record the working tree. Returns the latest checkpoint if nothing changed."""
        if not self.enabled:
            return None
        with _temp_index(self.root) as env:
            tree = self._snapshot_tree(env)
        existing = self.list()
        latest = existing[-1] if existing else None
        if latest is not None and latest.tree == tree:
            return latest
        n = latest.n + 1 if latest else 1
        parent = ["-p", latest.commit] if latest else []
        message = f"harness checkpoint {n}" + (f": {label}" if label else "")
        env = {**os.environ, **HARNESS_IDENTITY}
        # --no-gpg-sign: commit-tree honors commit.gpgSign, and a pinentry prompt would hang.
        commit = _git(
            self.root, "commit-tree", "--no-gpg-sign", tree, *parent, "-m", message, env=env
        )
        _git(self.root, "update-ref", f"{REF_PREFIX}{n}", commit, "")  # "" = must not exist yet
        return Checkpoint(n, commit, tree)

    def rewind(self, n: int) -> Checkpoint | None:
        """Make the working tree match checkpoint n. Returns a checkpoint of the state
        just before the rewind, so the rewind itself can be undone."""
        if not self.enabled:
            raise CheckpointError(f"checkpoints are disabled: {self.warning}")
        target = next((cp for cp in self.list() if cp.n == n), None)
        if target is None:
            have = [cp.n for cp in self.list()]
            raise CheckpointError(f"no checkpoint {n}; available: {have}")
        safety = self.create(f"before rewind to {n}")
        with _temp_index(self.root) as env:
            # The temp index now lists every current file. restore runs in no-overlay
            # mode, so files listed there but absent from the target are deleted.
            self._snapshot_tree(env)
            _git(
                self.root, "restore", f"--source={target.commit}", "--worktree", "--", ".", env=env
            )
        return safety
```

### `harness/coding.py`

```python
"""The coding tool suite, wired into the exercise-01 harness.

- make_bash: parse with shlex, apply the program allowlist, hand off to a runner.
- coding_registry: read_file, edit_file, write_file, glob, grep, bash.
- CodingHarness: the exercise-01 Harness plus a checkpoint before each model call.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from harness.checkpoints import GitCheckpoints
from harness.files import make_edit_file, make_read_file, make_write_file
from harness.hooks import block_protected_writes
from harness.ledger import ReadLedger
from harness.loop import Harness, LoopState
from harness.models import ModelClient
from harness.permissions import Approver, RuleBasedPolicy
from harness.runners import CommandResult, CommandRunner, SandboxConfig, make_runner
from harness.search import make_glob, make_grep
from harness.tools import COMMAND_TIMEOUT_S, DEFAULT_PROGRAMS, Tool, ToolError, ToolRegistry
from harness.workspace import Workspace

CODING_SYSTEM = (
    "You are a coding agent working in a workspace. Paths are relative to the workspace "
    "root. Find files with glob and grep, read them with read_file, and change them with "
    "edit_file (exact text replacement). Read a file before you edit it. Use write_file "
    "only for new files. bash runs one program with arguments and no shell, so pipes, "
    "redirects, && and $VARS don't work. A non-zero exit code is a normal result: read "
    "the output and fix the problem. When the task is complete, reply with a short "
    "summary and no tool calls."
)


def parse_command(command: str, programs: frozenset[str]) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolError(f"could not parse command: {exc}") from exc
    if not argv:
        raise ToolError("empty command")
    if argv[0] not in programs:
        raise ToolError(f"program not allowed: {argv[0]!r}; allowed: {sorted(programs)}")
    return argv


def _output(result: CommandResult) -> str:
    parts = [result.stdout.rstrip("\n")] if result.stdout else []
    if result.stderr:
        parts.append("[stderr]\n" + result.stderr.rstrip("\n"))
    return "\n".join(parts)


def format_result(result: CommandResult) -> str:
    output = _output(result)
    return f"exit code: {result.exit_code}" + (f"\n{output}" if output else "")


def make_bash(
    runner: CommandRunner,
    programs: frozenset[str] = DEFAULT_PROGRAMS,
    timeout_s: int = COMMAND_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
) -> Tool:
    """env defaults to the harness's own environment. The runner scrubs it."""

    def bash(ws: Workspace, args: dict[str, Any]) -> str:
        argv = parse_command(args["command"], programs)
        result = runner.run(argv, ws.root, timeout_s, os.environ if env is None else env)
        if result.timed_out:
            raise ToolError(
                f"command timed out after {timeout_s}s and was killed. "
                f"Partial output:\n{_output(result) or '(none)'}"
            )
        return format_result(result)

    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }
    desc = (
        "Run one program with arguments in the workspace root, e.g. 'pytest -q tests'. "
        "There is no shell: pipes, redirects, &&, globs and $VARS are not interpreted. "
        f"Allowed programs: {', '.join(sorted(programs))}. Times out after {timeout_s}s."
    )
    return Tool("bash", desc, schema, bash)


def coding_registry(
    runner: CommandRunner,
    ledger: ReadLedger | None = None,
    programs: frozenset[str] = DEFAULT_PROGRAMS,
    use_rg: bool | None = None,
) -> ToolRegistry:
    """One ledger is shared by read_file, edit_file and write_file."""
    seen = ledger if ledger is not None else ReadLedger()
    tools = (
        make_read_file(seen),
        make_edit_file(seen),
        make_write_file(seen),
        make_glob(),
        make_grep(use_rg),
        make_bash(runner, programs),
    )
    registry = ToolRegistry()
    for tool in tools:
        registry = registry.register(tool)
    return registry


@dataclass(frozen=True)
class CodingHarness(Harness):
    """Harness that checkpoints the workspace before every model call.

    Checkpoints dedupe on the tree hash, so a turn that changed nothing adds no
    new ref. A failed checkpoint raises: running on without undo is worse.
    """

    checkpoints: GitCheckpoints | None = None

    def _before_model_call(self, state: LoopState) -> LoopState:
        if self.checkpoints is not None:
            self.checkpoints.create(f"before turn {state.turns + 1}")
        return state


def coding_harness(
    model: ModelClient,
    workspace: Workspace,
    sandbox: SandboxConfig | None = None,
    approver: Approver | None = None,
    checkpoints: bool = True,
    **overrides: Any,
) -> CodingHarness:
    """Production wiring: rule policy, protected-path hook, runner picked from config."""
    runner = make_runner(sandbox or SandboxConfig(), workspace.root)
    kwargs: dict[str, Any] = {
        "model": model,
        "workspace": workspace,
        "policy": RuleBasedPolicy(workspace),
        "registry": coding_registry(runner),
        "approver": approver,
        "pre_tool": (block_protected_writes,),
        "system": CODING_SYSTEM,
        "checkpoints": GitCheckpoints.for_workspace(workspace.root) if checkpoints else None,
    }
    return CodingHarness(**{**kwargs, **overrides})
```

### `harness/coding_cli.py`

```python
"""Live entry point for the coding harness.

python -m harness.coding_cli --workspace DIR [--sandbox docker] "task"
python -m harness.coding_cli --workspace DIR --list-checkpoints
python -m harness.coding_cli --workspace DIR --rewind N
"""

from __future__ import annotations

import argparse
import sys

from harness.checkpoints import CheckpointError, GitCheckpoints
from harness.cli import terminal_approver
from harness.coding import coding_harness
from harness.models import AnthropicModel
from harness.runners import DEFAULT_IMAGE, SandboxConfig
from harness.workspace import Workspace


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the coding harness against a real model.")
    p.add_argument("--workspace", required=True, help="directory the agent may touch")
    p.add_argument("--sandbox", choices=("local", "docker"), default="local")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="container image for --sandbox docker")
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--budget", type=int, default=300_000, help="total token budget")
    p.add_argument("--no-checkpoints", action="store_true")
    p.add_argument("--list-checkpoints", action="store_true")
    p.add_argument("--rewind", type=int, metavar="N", help="restore checkpoint N and exit")
    p.add_argument("task", nargs="?", help="what the agent should do")
    return p


def _checkpoint_command(workspace: Workspace, args: argparse.Namespace) -> int:
    cps = GitCheckpoints.for_workspace(workspace.root)
    if args.list_checkpoints:
        for cp in cps.list():
            print(f"{cp.n}\t{cp.commit[:12]}")
        return 0
    try:
        safety = cps.rewind(args.rewind)
    except CheckpointError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    undo = f"; undo with --rewind {safety.n}" if safety else ""
    print(f"restored checkpoint {args.rewind}{undo}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = Workspace.at(args.workspace)
    if args.list_checkpoints or args.rewind is not None:
        return _checkpoint_command(workspace, args)
    if not args.task:
        _parser().error("a task is required unless you pass --list-checkpoints or --rewind")
    harness = coding_harness(
        AnthropicModel(),
        workspace,
        sandbox=SandboxConfig(kind=args.sandbox, image=args.image),
        approver=terminal_approver,
        checkpoints=not args.no_checkpoints,
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

### `tests/test_files.py`

```python
"""read_file, edit_file, write_file, and the read ledger."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from conftest import call, results_in, turn

from harness.coding import coding_registry
from harness.files import (
    EditError,
    apply_edit,
    make_edit_file,
    make_read_file,
    make_write_file,
)
from harness.hooks import block_protected_writes
from harness.ledger import ReadLedger
from harness.permissions import RuleBasedPolicy
from harness.runners import LocalRunner
from harness.tools import ToolError
from harness.types import ToolCall


@pytest.fixture
def fs(ws):
    ledger = ReadLedger()
    tools = SimpleNamespace(
        ledger=ledger,
        read=make_read_file(ledger).fn,
        edit=make_edit_file(ledger).fn,
        write=make_write_file(ledger).fn,
    )
    return ws, tools


def put(ws, rel: str, text: str) -> None:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def bump_mtime(ws, rel: str) -> None:
    """Move mtime forward explicitly, so the test doesn't depend on clock resolution."""
    st = (ws.root / rel).stat()
    os.utime(ws.root / rel, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


# --- read_file ----------------------------------------------------------------


def test_read_numbers_lines(fs):
    ws, t = fs
    put(ws, "src/m.py", "alpha\nbeta\ngamma\n")
    assert t.read(ws, {"path": "src/m.py"}) == "     1\talpha\n     2\tbeta\n     3\tgamma"


def test_read_pages_with_offset_and_limit(fs):
    ws, t = fs
    put(ws, "ten.txt", "".join(f"line {i}\n" for i in range(1, 11)))
    page = t.read(ws, {"path": "ten.txt", "offset": 4, "limit": 3})
    assert page.splitlines()[:3] == ["     4\tline 4", "     5\tline 5", "     6\tline 6"]
    assert "[showing lines 4-6 of 10. To read more, call read_file with offset=7.]" in page
    last = t.read(ws, {"path": "ten.txt", "offset": 9})
    assert last == "     9\tline 9\n    10\tline 10"  # nothing left, so no note


def test_read_default_limit_flags_truncation(fs):
    ws, t = fs
    put(ws, "big.txt", "x\n" * 2_500)
    out = t.read(ws, {"path": "big.txt"})
    assert "  2000\tx" in out and "  2001\tx" not in out
    assert out.endswith(
        "[showing lines 1-2000 of 2500. To read more, call read_file with offset=2001.]"
    )


def test_read_truncates_very_long_lines(fs):
    ws, t = fs
    put(ws, "min.js", "a" * 2_500 + "\nshort\n")
    first, second = t.read(ws, {"path": "min.js"}).splitlines()
    assert first.endswith("[... line truncated, 500 more chars]") and len(first) < 2_100
    assert second == "     2\tshort"


def test_read_describes_binary_files(fs):
    ws, t = fs
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
    (ws.root / "logo.png").write_bytes(png)
    (ws.root / "blob.dat").write_bytes(b"abc\0def")
    out = t.read(ws, {"path": "logo.png"})
    assert out == "logo.png is a binary file (PNG image, 108 bytes). Its contents are not shown."
    assert "binary data, 7 bytes" in t.read(ws, {"path": "blob.dat"})
    assert not t.ledger.has_read(ws.root / "logo.png")


def test_read_edge_cases(fs):
    ws, t = fs
    put(ws, "empty.txt", "")
    assert t.read(ws, {"path": "empty.txt"}) == "empty.txt is empty."
    with pytest.raises(ToolError, match="offset 9 is past the end of README.md, which has 1 lines"):
        t.read(ws, {"path": "README.md", "offset": 9})
    with pytest.raises(ToolError, match="must be 1 or more"):
        t.read(ws, {"path": "README.md", "offset": 0})
    with pytest.raises(ToolError, match="not a file: src"):
        t.read(ws, {"path": "src"})


def test_read_records_path_and_mtime_in_ledger(fs):
    ws, t = fs
    t.read(ws, {"path": "src/app.py", "limit": 1})
    assert t.ledger.has_read(ws.root / "src" / "app.py")
    assert t.ledger.check_fresh(ws.root / "src" / "app.py", "src/app.py") is None


# --- apply_edit and edit_file -------------------------------------------------


def test_apply_edit_rules():
    assert apply_edit("a b c", "b", "B", False) == "a B c"
    assert apply_edit("x x x", "x", "y", True) == "y y y"
    with pytest.raises(EditError, match="not found.*including whitespace"):
        apply_edit("abc", "abd", "z", False)
    with pytest.raises(EditError, match="matches 3 places. Include more surrounding lines"):
        apply_edit("x x x", "x", "y", False)


def test_edit_rejects_zero_and_ambiguous_matches(fs):
    ws, t = fs
    put(ws, "src/c.py", "count = 0\ncount = 0\n")
    t.read(ws, {"path": "src/c.py"})
    with pytest.raises(EditError, match=r"^src/c.py: old_string not found"):
        t.edit(ws, {"path": "src/c.py", "old_string": "count=0", "new_string": "count = 1"})
    with pytest.raises(EditError, match="matches 2 places.*or set replace_all"):
        t.edit(ws, {"path": "src/c.py", "old_string": "count = 0", "new_string": "count = 1"})
    assert (ws.root / "src/c.py").read_text() == "count = 0\ncount = 0\n"


def test_edit_hints_when_line_number_prefix_is_copied(fs):
    ws, t = fs
    t.read(ws, {"path": "src/app.py"})
    with pytest.raises(EditError, match="line-number prefix"):
        t.edit(ws, {"path": "src/app.py", "old_string": "     1\tprint('hi')", "new_string": "x"})


def test_edit_applies_unique_and_replace_all(fs):
    ws, t = fs
    put(ws, "src/c.py", "a = 1\nb = 1\nprint(a, b)\n")
    t.read(ws, {"path": "src/c.py"})
    out = t.edit(ws, {"path": "src/c.py", "old_string": "b = 1", "new_string": "b = 2"})
    assert out.startswith("edited src/c.py: replaced 1 occurrence(s).")
    out = t.edit(
        ws, {"path": "src/c.py", "old_string": "= ", "new_string": ":= ", "replace_all": True}
    )
    assert "replaced 2 occurrence(s)" in out  # no fresh read needed after our own edit
    assert (ws.root / "src/c.py").read_text() == "a := 1\nb := 2\nprint(a, b)\n"


def test_edit_confirmation_shows_context_not_the_whole_file(fs):
    ws, t = fs
    put(ws, "src/long.py", "".join(f"v{i} = {i}\n" for i in range(1, 51)))
    t.read(ws, {"path": "src/long.py"})
    out = t.edit(ws, {"path": "src/long.py", "old_string": "v25 = 25\n", "new_string": "v25 = 0\n"})
    numbered = out.splitlines()[1:]
    assert numbered[0] == "    22\tv22 = 22" and numbered[-1] == "    28\tv28 = 28"
    assert "    25\tv25 = 0" in numbered and len(numbered) == 7


def test_edit_refuses_unread_and_changed_files(fs):
    ws, t = fs
    edit = {"path": "src/app.py", "old_string": "hi", "new_string": "bye"}
    with pytest.raises(ToolError, match="you have not read src/app.py yet"):
        t.edit(ws, edit)
    t.read(ws, {"path": "src/app.py"})
    put(ws, "src/app.py", "print('hi')  # someone else\n")
    bump_mtime(ws, "src/app.py")
    with pytest.raises(ToolError, match="changed since you read it; read it again"):
        t.edit(ws, edit)
    t.read(ws, {"path": "src/app.py"})
    t.edit(ws, edit)
    assert (ws.root / "src/app.py").read_text() == "print('bye')  # someone else\n"


def test_edit_other_errors(fs):
    ws, t = fs
    with pytest.raises(ToolError, match="does not exist. Use write_file"):
        t.edit(ws, {"path": "src/nope.py", "old_string": "a", "new_string": "b"})
    t.read(ws, {"path": "src/app.py"})
    with pytest.raises(EditError, match="identical"):
        t.edit(ws, {"path": "src/app.py", "old_string": "hi", "new_string": "hi"})
    with pytest.raises(EditError, match="empty"):
        t.edit(ws, {"path": "src/app.py", "old_string": "", "new_string": "x"})


# --- write_file ---------------------------------------------------------------


def test_write_creates_new_files_but_overwrite_needs_a_read(fs):
    ws, t = fs
    assert t.write(ws, {"path": "src/pkg/new.py", "content": "x = 1\n"}).startswith("created")
    with pytest.raises(ToolError, match="you have not read README.md yet.*only overwrites"):
        t.write(ws, {"path": "README.md", "content": "# replaced\n"})
    t.read(ws, {"path": "README.md"})
    assert t.write(ws, {"path": "README.md", "content": "# replaced\n"}).startswith("overwrote")
    assert (ws.root / "README.md").read_text() == "# replaced\n"


# --- wiring: policy, hooks, and the loop --------------------------------------


def test_policy_and_hook_know_the_new_tool_names(ws):
    policy = RuleBasedPolicy(ws)
    assert policy.decide(ToolCall("1", "glob", {"pattern": "*"})).decision == "allow"
    assert policy.decide(ToolCall("2", "edit_file", {"path": "src/a.py"})).decision == "allow"
    assert policy.decide(ToolCall("3", "edit_file", {"path": "top.py"})).decision == "ask"
    assert policy.decide(ToolCall("4", "bash", {"command": "pytest -q"})).decision == "allow"
    assert block_protected_writes(ToolCall("5", "edit_file", {"path": "src/.env"})) is not None


def test_read_before_edit_through_the_loop(make_harness, ws):
    harness, model = make_harness(
        turn(call("edit_file", path="src/app.py", old_string="hi", new_string="bye")),
        turn(call("read_file", path="src/app.py")),
        turn(call("edit_file", path="src/app.py", old_string="hi", new_string="bye")),
        turn(text="done"),
        registry=coding_registry(LocalRunner()),
    )
    assert harness.run("say bye").stop_reason == "done"
    refused = results_in(model.requests[1])[0]
    assert refused["is_error"] and "have not read src/app.py" in refused["content"]
    assert not results_in(model.requests[3])[0]["is_error"]
    assert (ws.root / "src/app.py").read_text() == "print('bye')\n"
```

### `tests/test_search.py`

```python
"""glob and grep: containment, ignore rules, and rg/Python parity."""

from __future__ import annotations

import os
import re
import shutil

import pytest

from harness.search import (
    GrepQuery,
    glob_to_regex,
    make_glob,
    make_grep,
    search_python,
    search_rg,
)
from harness.tools import ToolError
from harness.workspace import PathEscapeError

HAS_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAS_RG, reason="ripgrep (rg) is not installed")
BACKENDS = ["python", pytest.param("rg", marks=needs_rg)]

FILES: dict[str, str | bytes] = {
    "src/pkg/util.py": "def helper():\n    return 'hi'\n\n\ndef other():\n    pass\n",
    "src/pkg/notes.txt": "hi there\n",
    "src/nonl.py": "x = 'hi'",  # no trailing newline
    "src/uni.py": "naïve = 'hi'\n",
    "src/win.txt": "hi\r\nthere\r\n",
    "tests/test_app.py": "from src.app import main\n# hi\n",
    ".hidden/conf.py": "hi = 1\n",
    "node_modules/lib/index.js": "hi\n",
    ".venv/lib/site.py": "hi\n",
    "src/__pycache__/app.cpython-311.pyc": "hi\n",
    ".git/config": "hi\n",
    "data.bin": b"hi\0\x01\x02",
}
INCLUDED_HI = [
    ".hidden/conf.py",
    "src/app.py",
    "src/nonl.py",
    "src/pkg/notes.txt",
    "src/pkg/util.py",
    "src/uni.py",
    "src/win.txt",
    "tests/test_app.py",
]


@pytest.fixture
def tree(ws, tmp_path_factory):
    for rel, body in FILES.items():
        path = ws.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("hi secret\n")
    (ws.root / "src" / "leak.py").symlink_to(outside / "secret.py")
    (ws.root / "linkdir").symlink_to(outside)
    return ws


def grep(ws, backend: str, **args) -> str:
    return make_grep(use_rg=backend == "rg").fn(ws, args)


# --- glob -----------------------------------------------------------------------


def test_glob_to_regex():
    rx = glob_to_regex("src/**/*.{py,txt}")
    assert rx.fullmatch("src/a.py") and rx.fullmatch("src/x/y/b.txt")
    assert not rx.fullmatch("src/a.js") and not rx.fullmatch("lib/a.py")
    assert glob_to_regex("*.py").fullmatch("a.py") and not glob_to_regex("*.py").fullmatch("d/a.py")
    assert glob_to_regex("file[!0-9].txt").fullmatch("fileA.txt")


def test_glob_newest_first_skips_ignored_and_symlinks(tree):
    ws = tree
    for age, rel in enumerate(["src/uni.py", "src/pkg/util.py", "src/app.py"]):
        os.utime(ws.root / rel, (1_000_000 - age * 1000, 1_000_000 - age * 1000))
    out = make_glob().fn(ws, {"pattern": "src/**/*.py"}).splitlines()
    assert out[-3:] == ["src/uni.py", "src/pkg/util.py", "src/app.py"]  # oldest last
    assert set(out) == {"src/app.py", "src/nonl.py", "src/uni.py", "src/pkg/util.py"}
    everything = make_glob(limit=1_000).fn(ws, {"pattern": "**/*"})
    for hidden in ("node_modules", ".venv", "__pycache__", ".git/", "leak.py", "secret"):
        assert hidden not in everything


def test_glob_is_relative_to_path_and_stays_inside(tree):
    ws = tree
    top_level = make_glob().fn(ws, {"pattern": "*.py", "path": "src"}).splitlines()
    assert set(top_level) == {"src/app.py", "src/nonl.py", "src/uni.py"}
    assert make_glob().fn(ws, {"pattern": "../*"}).startswith("no files match")
    with pytest.raises(PathEscapeError):
        make_glob().fn(ws, {"pattern": "*", "path": ".."})
    with pytest.raises(ToolError, match="not a directory"):
        make_glob().fn(ws, {"pattern": "*", "path": "README.md"})


def test_glob_caps_results_with_a_note(tree):
    out = make_glob(limit=3).fn(tree, {"pattern": "**/*"}).splitlines()
    assert len(out) == 4 and re.fullmatch(
        r"\[\d+ more results not shown; narrow the search\]", out[-1]
    )


# --- grep: expected results on both backends ----------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_grep_files_skips_ignored_binary_and_symlinks(tree, backend):
    assert grep(tree, backend, pattern="hi").splitlines() == INCLUDED_HI


@pytest.mark.parametrize("backend", BACKENDS)
def test_grep_glob_filter(tree, backend):
    assert grep(tree, backend, pattern="hi", glob="*.txt").splitlines() == [
        "src/pkg/notes.txt",
        "src/win.txt",
    ]
    assert grep(tree, backend, pattern="hi", glob="src/**/*.py").splitlines() == [
        "src/app.py",
        "src/nonl.py",
        "src/pkg/util.py",
        "src/uni.py",
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_grep_content_with_context(tree, backend):
    out = grep(tree, backend, pattern=r"^def ", path="src/pkg", output="content", context=1)
    assert out.splitlines() == [
        "src/pkg/util.py:1:def helper():",
        "src/pkg/util.py-2-    return 'hi'",
        "--",
        "src/pkg/util.py-4-",
        "src/pkg/util.py:5:def other():",
        "src/pkg/util.py-6-    pass",
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_grep_count_and_no_match(tree, backend):
    assert grep(tree, backend, pattern="def", output="count") == "src/pkg/util.py:2"
    assert grep(tree, backend, pattern="zzz_nope") == "no matches for 'zzz_nope' in ."


@pytest.mark.parametrize("backend", BACKENDS)
def test_grep_stays_inside_and_validates_input(tree, backend):
    with pytest.raises(PathEscapeError):
        grep(tree, backend, pattern="hi", path="../")
    with pytest.raises(ToolError, match="invalid regex"):
        grep(tree, backend, pattern="(unclosed")
    with pytest.raises(ToolError, match="output must be one of"):
        grep(tree, backend, pattern="hi", output="json")
    assert (
        grep(tree, backend, pattern="hi", path="data.bin")
        == "data.bin is a binary file; not searched."
    )


def test_grep_caps_output(ws):
    (ws.root / "many.txt").write_text("hit\n" * 250)
    out = make_grep(use_rg=False).fn(ws, {"pattern": "hit", "output": "content"}).splitlines()
    assert len(out) == 201 and out[-1] == "[50 more lines not shown; narrow the search]"


# --- grep: rg and the Python fallback agree -----------------------------------

QUERIES = [
    GrepQuery("hi", "."),
    GrepQuery("hi", ".", glob="*.py"),
    GrepQuery("hi", ".", glob="src/**/*.{py,txt}", context=2),
    GrepQuery(r"def \w+", "src", context=1),
    GrepQuery(r"^hi$", "."),
    GrepQuery(r"hi\r$", "."),
    GrepQuery("na.ve", "."),
    GrepQuery("return|pass", "src/pkg/util.py", context=3),
    GrepQuery("zzz_nope", "."),
    GrepQuery("hi", "src/app.py", glob="*.txt"),  # a named file ignores the glob
    GrepQuery("hi", "node_modules"),  # a named ignored dir is searched
]


@needs_rg
@pytest.mark.parametrize(
    "query", QUERIES, ids=lambda q: f"{q.pattern}|{q.path}|{q.glob}|{q.context}"
)
def test_rg_and_python_return_identical_hits(tree, query):
    assert search_rg(tree, query) == search_python(tree, query, re.compile(query.pattern))


@needs_rg
@pytest.mark.parametrize("output", ["files", "content", "count"])
def test_rg_and_python_render_identically(tree, output):
    for q in QUERIES:
        args = {"pattern": q.pattern, "path": q.path, "output": output, "context": q.context}
        if q.glob:
            args["glob"] = q.glob
        assert grep(tree, "rg", **args) == grep(tree, "python", **args)
```

### `tests/test_runners.py`

```python
"""CommandRunner implementations and the bash tool."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from conftest import call, results_in, turn

from harness.coding import coding_registry, make_bash
from harness.runners import (
    DockerRunner,
    LocalRunner,
    SandboxConfig,
    docker_available,
    make_runner,
)
from harness.tools import ToolError

PY = sys.executable
PLANTED = "sk-ant-planted-do-not-leak"
DOCKER_TEST_IMAGE = "alpine:3.20"


def py(code: str) -> str:
    """A bash-tool command string that runs Python code."""
    return f'{PY} -c "{code}"'


# --- LocalRunner ------------------------------------------------------------------


def test_local_runner_passes_only_allowlisted_env(ws):
    env = {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": PLANTED,
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    result = LocalRunner().run([PY, "-c", code], ws.root, 10, env)
    child = json.loads(result.stdout)
    assert child["LANG"] == "C.UTF-8" and child["PATH"] == os.environ["PATH"]
    for secret in ("ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"):
        assert secret not in child
    assert PLANTED not in result.stdout


def test_bash_tool_never_hands_the_api_key_to_the_child(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLANTED)  # planted in the harness's own env
    bash = make_bash(LocalRunner(), frozenset({PY}))
    out = bash.fn(
        ws, {"command": py("import os; print(os.environ.get('ANTHROPIC_API_KEY', 'absent'))")}
    )
    assert out == "exit code: 0\nabsent"
    assert os.environ["ANTHROPIC_API_KEY"] == PLANTED


def test_nonzero_exit_is_a_normal_result_in_the_loop(make_harness):
    command = py("import sys; print('FAILED test_x'); print('boom', file=sys.stderr); sys.exit(1)")
    harness, model = make_harness(
        turn(call("bash", command=command)),
        turn(text="saw the failure"),
        registry=coding_registry(LocalRunner(), programs=frozenset({PY})),
        approver=lambda c, r: True,
    )
    harness.run("run the tests")
    block = results_in(model.requests[1])[0]
    assert not block["is_error"]
    assert block["content"] == "exit code: 1\nFAILED test_x\n[stderr]\nboom"


def test_timeout_is_an_error_with_partial_output(ws):
    bash = make_bash(LocalRunner(), frozenset({PY}), timeout_s=1)
    command = py("import time; print('started', flush=True); time.sleep(10)")
    with pytest.raises(
        ToolError, match=r"timed out after 1s and was killed. Partial output:\nstarted$"
    ):
        bash.fn(ws, {"command": command})


def test_timeout_kills_grandchildren(ws):
    marker = ws.root / "grandchild-survived"
    grandchild = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).touch()"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(10)"
    )
    result = LocalRunner().run([PY, "-c", parent], ws.root, 0.5, dict(os.environ))
    assert result.timed_out
    time.sleep(2.5)
    assert not marker.exists()


def test_runner_edge_cases(ws):
    runner = LocalRunner(max_chars=100)
    missing = runner.run(["no-such-program-xyz"], ws.root, 5, dict(os.environ))
    assert missing.exit_code == 127 and "program not found" in missing.stderr
    no_stdin = runner.run([PY, "-c", "input()"], ws.root, 5, dict(os.environ))
    assert no_stdin.exit_code == 1 and not no_stdin.timed_out  # EOF, not a hang
    long = runner.run(
        [PY, "-c", "print('HEAD' + 'x' * 500 + 'TAIL')"], ws.root, 5, dict(os.environ)
    )
    assert "HEAD" in long.stdout and "TAIL" in long.stdout and "[... truncated" in long.stdout
    assert long.duration_s >= 0


def test_bash_tool_allowlist_and_no_shell(ws):
    bash = make_bash(LocalRunner(), frozenset({"echo"}))
    assert (
        bash.fn(ws, {"command": "echo hello; rm -rf / && $HOME"})
        == "exit code: 0\nhello; rm -rf / && $HOME"
    )
    with pytest.raises(ToolError, match="program not allowed: 'bash'"):
        bash.fn(ws, {"command": "bash -c 'echo hi'"})
    with pytest.raises(ToolError, match="could not parse"):
        bash.fn(ws, {"command": "echo 'unterminated"})


# --- config and DockerRunner flags (no Docker needed) ---------------------------


def test_make_runner_picks_from_config(ws):
    assert isinstance(make_runner(SandboxConfig(), ws.root), LocalRunner)
    docker = make_runner(SandboxConfig(kind="docker", image="img:1", memory="1g"), ws.root)
    assert isinstance(docker, DockerRunner) and (docker.image, docker.memory) == ("img:1", "1g")
    with pytest.raises(ValueError, match="unknown sandbox kind"):
        make_runner(SandboxConfig(kind="vm"), ws.root)  # type: ignore[arg-type]


def test_docker_argv_isolation_flags(ws):
    runner = DockerRunner(ws.root, image="img:1")
    env = {"ANTHROPIC_API_KEY": PLANTED, "PATH": "/host/bin", "LANG": "C.UTF-8"}
    argv = runner.docker_argv(["pytest", "-q"], ws.root / "src", env, "harness-test")
    joined = " ".join(argv)
    for flag in (
        "run --rm",
        "--network none",
        "--read-only --tmpfs /tmp:rw,size=64m",
        "--cpus 1 --memory 512m",
        f"--user {os.getuid()}:{os.getgid()}",
        f"type=bind,src={ws.root},dst=/workspace",
        "--workdir /workspace/src",
        "--env HOME=/tmp",
        "--env LANG=C.UTF-8",
    ):
        assert flag in joined
    assert PLANTED not in joined and "/host/bin" not in joined
    assert argv[-3:] == ["img:1", "pytest", "-q"]
    with pytest.raises(ValueError):
        runner.docker_argv(["ls"], ws.root.parent, {}, "harness-test")


# --- DockerRunner for real (skipped without Docker) -----------------------------


@pytest.fixture(scope="module")
def docker_image() -> str:
    if not docker_available():
        pytest.skip("Docker is not available")
    have = subprocess.run(
        ["docker", "image", "inspect", DOCKER_TEST_IMAGE], capture_output=True, check=False
    )
    if have.returncode != 0:
        pulled = subprocess.run(
            ["docker", "pull", DOCKER_TEST_IMAGE], capture_output=True, timeout=300, check=False
        )
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {DOCKER_TEST_IMAGE}")
    return DOCKER_TEST_IMAGE


def in_docker(ws, image: str, script: str, timeout: float = 60, env=None):
    runner = DockerRunner(ws.root, image=image)
    return runner.run(["sh", "-c", script], ws.root, timeout, env or {})


def test_docker_has_no_network(ws, docker_image):
    script = "ip route; ip -o link show up; wget -T 3 -q -O- http://1.1.1.1 && echo REACHED"
    result = in_docker(ws, docker_image, script)
    lines = result.stdout.splitlines()  # `ip route` printed nothing: there are no routes
    assert len(lines) == 1 and lines[0].startswith("1: lo:")  # only loopback is up
    assert "REACHED" not in result.stdout and result.exit_code != 0
    assert "unreachable" in result.stderr.lower()


def test_docker_writes_only_to_workspace_and_tmp(ws, docker_image):
    script = (
        "echo from-container > /workspace/inside.txt && echo ok > /tmp/t && cat /tmp/t; "
        "for p in /outside.txt /etc/outside.txt /usr/outside.txt; do "
        "touch $p 2>/dev/null && echo WROTE $p || echo BLOCKED $p; done"
    )
    result = in_docker(ws, docker_image, script)
    assert (ws.root / "inside.txt").read_text() == "from-container\n"
    assert result.stdout.splitlines() == [
        "ok",
        "BLOCKED /outside.txt",
        "BLOCKED /etc/outside.txt",
        "BLOCKED /usr/outside.txt",
    ]


def test_docker_runs_as_host_user_with_scrubbed_env(ws, docker_image):
    env = {"ANTHROPIC_API_KEY": PLANTED, "LANG": "C.UTF-8"}
    result = in_docker(ws, docker_image, "id -u; env", env=env)
    lines = result.stdout.splitlines()
    assert lines[0] == str(os.getuid())
    assert "LANG=C.UTF-8" in lines and "HOME=/tmp" in lines
    assert PLANTED not in result.stdout


def test_docker_timeout_kills_the_container(ws, docker_image):
    runner = DockerRunner(ws.root, image=docker_image)
    result = runner.run(["sleep", "60"], ws.root, 3, {})
    assert result.timed_out
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=harness-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if not ps.stdout.strip():
            break
        time.sleep(0.5)
    assert not ps.stdout.strip(), "container still running after timeout"
```

### `tests/test_checkpoints.py`

```python
"""Git checkpoints: private refs, rewind, and the user's git state left alone."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from conftest import call, turn

from harness.checkpoints import CheckpointError, GitCheckpoints
from harness.coding import coding_harness
from harness.models import ScriptedModel

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    """Ignore the developer's git config (signing, hooks) and set an identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "test-user")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")


def git(ws, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ws.root, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def write(ws, rel: str, text: str) -> None:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def user_git_state(ws) -> dict[str, str]:
    """Everything a checkpoint or rewind must not move."""
    return {
        "branch": git(ws, "symbolic-ref", "HEAD"),
        "head": git(ws, "rev-parse", "HEAD"),
        "index": git(ws, "ls-files", "--stage"),
        "branches": git(ws, "for-each-ref", "refs/heads"),
        "log": git(ws, "log", "--format=%H %s", "main"),
    }


@pytest.fixture
def repo(ws):
    """A repo with a commit, a staged change, an untracked file, and an ignored file."""
    write(ws, ".gitignore", "*.log\n")
    write(ws, "gone.txt", "will be deleted\n")
    git(ws, "init", "-q", "-b", "main")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "initial")
    write(ws, "README.md", "# demo\nstaged line\n")
    git(ws, "add", "README.md")  # the user has a staged change
    write(ws, "notes.txt", "untracked but not ignored\n")
    write(ws, "debug.log", "ignored\n")
    return ws


def test_rewind_restores_modified_deleted_and_new_files(repo):
    ws = repo
    before = user_git_state(ws)
    cps = GitCheckpoints.for_workspace(ws.root)
    cp = cps.create("before agent")
    assert user_git_state(ws) == before  # creating a checkpoint moved nothing

    write(ws, "src/app.py", "print('changed')\n")  # modified
    (ws.root / "gone.txt").unlink()  # deleted, tracked
    (ws.root / "notes.txt").unlink()  # deleted, untracked
    write(ws, "src/pkg/new.py", "x = 1\n")  # created, in a new directory
    write(ws, "agent.log", "ignored, created\n")  # ignored: outside checkpoint scope

    cps.rewind(cp.n)

    assert (ws.root / "src/app.py").read_text() == "print('hi')\n"
    assert (ws.root / "gone.txt").read_text() == "will be deleted\n"
    assert (ws.root / "notes.txt").read_text() == "untracked but not ignored\n"
    assert not (ws.root / "src/pkg").exists()
    assert (ws.root / "README.md").read_text() == "# demo\nstaged line\n"
    assert (ws.root / "agent.log").exists() and (ws.root / "debug.log").exists()
    assert user_git_state(ws) == before  # same branch, HEAD, index, history


def test_checkpoints_live_in_private_refs_and_dedupe(repo):
    ws = repo
    cps = GitCheckpoints.for_workspace(ws.root)
    first = cps.create()
    assert cps.create() == first  # nothing changed: no new ref
    write(ws, "src/app.py", "print('v2')\n")
    second = cps.create("turn 2")
    assert (first.n, second.n) == (1, 2)
    assert git(ws, "rev-parse", "refs/harness/checkpoints/2") == second.commit
    assert git(ws, "rev-parse", f"{second.commit}^") == first.commit
    assert (
        git(ws, "log", "-1", "--format=%an %s", second.commit)
        == "harness harness checkpoint 2: turn 2"
    )
    assert "harness" not in git(ws, "branch", "--all")


def test_rewind_can_be_undone(repo):
    ws = repo
    cps = GitCheckpoints.for_workspace(ws.root)
    cp = cps.create()
    write(ws, "src/app.py", "print('agent work')\n")
    safety = cps.rewind(cp.n)
    assert (ws.root / "src/app.py").read_text() == "print('hi')\n"
    cps.rewind(safety.n)
    assert (ws.root / "src/app.py").read_text() == "print('agent work')\n"


def test_rewind_unknown_checkpoint_is_an_error(repo):
    cps = GitCheckpoints.for_workspace(repo.root)
    cps.create()
    with pytest.raises(CheckpointError, match=r"no checkpoint 7; available: \[1\]"):
        cps.rewind(7)


def test_signing_config_does_not_break_checkpoints(repo):
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "false")  # any signing attempt would fail
    assert GitCheckpoints.for_workspace(repo.root).create() is not None


def test_not_a_repo_disables_with_a_warning(ws):
    with pytest.warns(
        RuntimeWarning, match="checkpoints disabled: .* is not inside a git work tree"
    ):
        cps = GitCheckpoints.for_workspace(ws.root)
    assert not cps.enabled and cps.create() is None and cps.list() == ()
    with pytest.raises(CheckpointError, match="disabled"):
        cps.rewind(1)


def test_harness_checkpoints_each_changing_turn_and_rewinds_agent_work(repo):
    ws = repo
    before = user_git_state(ws)
    model = ScriptedModel(
        [
            turn(call("read_file", path="src/app.py")),
            turn(call("edit_file", path="src/app.py", old_string="hi", new_string="bye")),
            turn(call("write_file", path="src/extra.py", content="y = 2\n")),
            turn(text="done"),
        ]
    )
    harness = coding_harness(model, ws)
    assert harness.run("change the greeting").stop_reason == "done"
    cps = harness.checkpoints
    # before turn 1; turn 2 unchanged (deduped); before turn 3 (edit); before turn 4 (new file)
    assert [cp.n for cp in cps.list()] == [1, 2, 3]
    assert (ws.root / "src/app.py").read_text() == "print('bye')\n"
    cps.rewind(1)
    assert (ws.root / "src/app.py").read_text() == "print('hi')\n"
    assert not (ws.root / "src/extra.py").exists()
    assert user_git_state(ws) == before


def test_harness_runs_without_git_and_warns(ws):
    model = ScriptedModel([turn(call("glob", pattern="**/*.py")), turn(text="ok")])
    with pytest.warns(RuntimeWarning, match="checkpoints disabled"):
        harness = coding_harness(model, ws)
    assert harness.run("list python files").stop_reason == "done"
```

## Meeting the acceptance criteria

- **`read_file` numbers lines, pages with `offset`/`limit`, flags truncation,
  and describes binary files.** `test_read_numbers_lines` checks the exact
  format. `test_read_pages_with_offset_and_limit` reads lines 4–6 of 10, checks
  the note names `offset=7`, and checks the last page has no note.
  `test_read_default_limit_flags_truncation` covers the 2,000-line default, and
  `test_read_truncates_very_long_lines` covers the per-line marker.
  `test_read_describes_binary_files` checks a PNG and a NUL-containing blob, and
  that neither is put in the ledger. `test_read_records_path_and_mtime_in_ledger`
  shows a read makes the file editable.
- **`edit_file` rejects zero and ambiguous matches with actionable messages,
  and applies unique and `replace_all` edits.**
  `test_edit_rejects_zero_and_ambiguous_matches` checks both messages and that
  the file is unchanged. `test_edit_hints_when_line_number_prefix_is_copied`
  covers the common miss. `test_edit_applies_unique_and_replace_all` applies one
  of each, the second without a re-read. `test_edit_confirmation_shows_context_not_the_whole_file`
  checks the snippet is lines 22–28 of a 50-line file.
- **`edit_file` refuses unread files and files modified after the read.**
  `test_edit_refuses_unread_and_changed_files` edits before any read, then
  after an outside change (mtime moved explicitly, so the test doesn't depend
  on clock resolution), then after a fresh read, which succeeds.
  `test_write_creates_new_files_but_overwrite_needs_a_read` covers the same
  rule for `write_file`. `test_read_before_edit_through_the_loop` runs it
  through the real harness and permission gate.
- **`glob` and `grep` stay inside the workspace, skip ignored directories, and
  give identical results with rg and the Python fallback.** The `tree` fixture
  plants every hazard at once: `.git/`, `node_modules/`, `.venv/`,
  `__pycache__/`, a binary file, a symlinked file and a symlinked directory
  pointing outside, a hidden directory, a CRLF file, a file with no trailing
  newline, and non-ASCII text. The expected-result tests run on both backends
  (`BACKENDS`); the rg case skips when rg is missing.
  `test_grep_stays_inside_and_validates_input` and
  `test_glob_is_relative_to_path_and_stays_inside` check that `..` raises
  `PathEscapeError`. `test_rg_and_python_return_identical_hits` compares the
  two backends' data on 11 queries, including context, brace and `**` globs,
  anchors against CRLF, a named file with a glob, and a named ignored directory.
  `test_rg_and_python_render_identically` compares the final text for all three
  output modes.
- **`LocalRunner` doesn't pass the API key or other non-allowlisted variables
  to the child.** `test_local_runner_passes_only_allowlisted_env` hands the
  runner an environment containing `ANTHROPIC_API_KEY`, `AWS_SECRET_ACCESS_KEY`,
  and `SSH_AUTH_SOCK`, and shows the child sees `PATH` and `LANG` but none of
  those. `test_bash_tool_never_hands_the_api_key_to_the_child` plants the key in
  the harness's own `os.environ` and runs the full `bash` tool path; the child
  prints `absent`.
- **Checkpoints restore a modified, a deleted, and a newly created file, and
  leave the user's branch, index, and HEAD unchanged.**
  `test_rewind_restores_modified_deleted_and_new_files` starts from a repo with
  a staged change, an untracked file, and an ignored file. It modifies a file,
  deletes a tracked and an untracked file, creates a file in a new directory,
  and creates an ignored file. After `rewind`, the first four are back the way
  they were (the new directory is gone too), the ignored files are untouched,
  and `user_git_state` (symbolic HEAD, HEAD commit, `ls-files --stage`,
  branches, log) equals its value before the checkpoint.
  `test_harness_checkpoints_each_changing_turn_and_rewinds_agent_work` does the
  same through `CodingHarness` and a scripted model, and checks the dedupe:
  four model calls, three checkpoints. `test_signing_config_does_not_break_checkpoints`
  turns on `commit.gpgsign` with `gpg.program=false`.
  `test_not_a_repo_disables_with_a_warning` and
  `test_harness_runs_without_git_and_warns` cover the no-git case.
- **With Docker present, a `DockerRunner` command can't reach the network and
  can't write outside `/workspace` and `/tmp`.** `test_docker_has_no_network`
  shows there are no routes, only loopback is up, and `wget` to `1.1.1.1` fails
  with "Network unreachable". `test_docker_writes_only_to_workspace_and_tmp`
  writes `/workspace/inside.txt` (and finds it on the host) and `/tmp/t`, and
  is refused at `/`, `/etc`, and `/usr`. `test_docker_runs_as_host_user_with_scrubbed_env`
  and `test_docker_timeout_kills_the_container` cover the user, the
  environment, and cleanup. All four skip when `docker info` fails.
  `test_docker_argv_isolation_flags` checks the full flag list without Docker.

## Common pitfalls

- **Showing line numbers, then matching them.** If the model copies the line-number prefix
  (spaces, digits, a tab) into `old_string`, the edit can't match. Say in the tool description that the
  prefix isn't part of the file, and detect it in the error.
- **Forgetting to re-record after your own write.** If `edit_file` doesn't
  update the ledger, the model's second edit fails with "changed since you read
  it", and it re-reads after every edit.
- **Trusting mtime alone.** On a filesystem with one-second mtime resolution, a
  change in the same second as the read is invisible. Compare size too, and in
  tests move mtime explicitly with `os.utime` instead of sleeping.
- **Following symlinks in search.** `os.walk` lists symlinked files like any
  other file, and `Path.rglob` yields them too, so a naive search reads
  `src/leak.py -> ~/.aws/credentials`. With `followlinks=True`, `os.walk` also
  descends into linked directories. Skip symlinks, as rg does.
- **Letting rg's defaults decide.** By default rg skips hidden files and
  respects `.gitignore` and the user's config file, so its results depend on
  the machine. Your fallback will disagree with it on someone's laptop. Set
  `--no-config --hidden --no-ignore` and apply one explicit ignore list.
- **Parsing rg's text output.** `path:12:text` is ambiguous when a path
  contains `:`, and the output order is unstable across threads. Use `--json`,
  and sort in Python.
- **Denylisting secrets.** Removing `ANTHROPIC_API_KEY` from the environment
  protects that one key. Pass an allowlist.
- **`subprocess.run(timeout=...)` alone.** It kills the direct child only.
  Start the child in a new session and kill the process group. Close stdin,
  too, or a command that prompts for input hangs until the timeout.
- **Killing the docker client and calling it done.** The container keeps
  running. Name it and `docker kill` it.
- **Passing the host `PATH` into a container.** `/opt/homebrew/bin` doesn't
  exist in the image. Let the image's `PATH` stand, and set `HOME` to the
  writable `/tmp`.
- **Checkpointing with `git stash` or `git commit`.** Both move user-visible
  state: the stash list, the branch, the index. Use a temporary
  `GIT_INDEX_FILE`, `write-tree`, `commit-tree`, and a ref under
  `refs/harness/`.
- **Forgetting `commit-tree` signs.** It honors `commit.gpgSign`. Pass
  `--no-gpg-sign`, and in tests set `GIT_CONFIG_GLOBAL=/dev/null` so the
  developer's config can't interfere.
- **Restoring without deleting.** `git checkout <commit> -- .` brings back
  modified and deleted files but leaves files created since the checkpoint.
  `git restore --worktree` against a temp index that lists the current files
  removes them.
- **Deleting ignored files on rewind.** A `git clean -fdx` style rewind wipes
  `.venv/`, caches, and `.env`. Decide the scope on purpose and write it down.
- **Pushing the refs.** `git push --mirror` sends every ref, including
  `refs/harness/checkpoints/*`, and checkpoints may contain files you never
  meant to commit. Delete them when a session ends
  (`git for-each-ref --format='%(refname)' refs/harness | xargs -n1 git update-ref -d`),
  or don't mirror-push.

## Verification

Start from the exercise-01 files, apply the two changes and add the eleven new
files above, then:

```bash
cd exercise-02-coding-tool-suite
python3.11 -m venv .venv && . .venv/bin/activate
pip install pytest

# Offline. rg and Docker are optional: their tests skip when absent.
pytest -q
pytest -q -rs          # list what skipped and why

# Live, in a scratch git repo. Set a spend cap on the key first.
pip install anthropic
export ANTHROPIC_API_KEY=...            # never commit this
export HARNESS_MODEL=claude-sonnet-4-5  # check the current models list
mkdir -p /tmp/coding-ws/src && cd /tmp/coding-ws && git init -q && cd -
printf 'def add(a, b):\n    return a - b\n' > /tmp/coding-ws/src/calc.py
python -m harness.coding_cli --workspace /tmp/coding-ws \
  "src/calc.py has a bug in add(). Fix it and add tests/test_calc.py; run pytest."
python -m harness.coding_cli --workspace /tmp/coding-ws --list-checkpoints
python -m harness.coding_cli --workspace /tmp/coding-ws --rewind 1
git -C /tmp/coding-ws status --short   # the agent's files are gone; nothing staged

# The same task in a container (needs an image with pytest installed).
python -m harness.coding_cli --workspace /tmp/coding-ws --sandbox docker \
  --image my-python-with-pytest:latest "Fix add() in src/calc.py and run pytest."
```

Expected: with rg and Docker available, `pytest -q` reports `99 passed` (32
from exercise-01, 67 new). Without either, it reports `76 passed, 23 skipped`:
4 Docker tests and 19 rg cases. In the live run, the agent reads `calc.py`
before editing it. A `pytest` failure comes back as `exit code: 1` with the
output, not as a tool error. `--list-checkpoints` shows one checkpoint per turn
that changed files. After `--rewind 1`, `git status` shows the workspace as it
was before the agent started, and it prints the checkpoint number that undoes
the rewind.
