# mod-201-agent-fundamentals/exercise-03-coding-agent-read-write-execute — Solution

## Approach

This exercise turns the Exercise 02 tool-calling loop into a minimal coding
agent: three tools (`read_file`, `write_file`, `run_shell`), a scoped workspace,
and budgets that guarantee termination. The hard parts are not the loop — it is
the same one — but the guardrails around the tools and the sandbox boundary.

- **Path scoping.** Every tool resolves its path with `Path.resolve()` and
  rejects anything that escapes the workspace root, whether via `..`, an
  absolute path, or a symlink that points outside. Resolving *first* and then
  checking containment is what defeats the symlink-escape trick.
- **Read-before-write.** The agent tracks which files it has read this run.
  `write_file` refuses to overwrite an existing file the model has not read,
  returning the refusal as a tool result. This closes the "model hallucinated
  the file and clobbered it" failure class.
- **Sandboxed execution.** `run_shell` executes inside a Docker container with
  the workspace bind-mounted and `--network none` by default. A host subprocess
  is explicitly *not acceptable*; the `LocalSandbox` shown here is a clearly
  labeled fallback for environments without Docker, used only for the tool unit
  tests, never for a live agent run.
- **Output discipline.** `run_shell` enforces a timeout and tail-truncates
  stdout/stderr (errors live at the bottom of stack traces), surfacing the exit
  code so the model can branch on success or failure.
- **Termination.** The loop stops on any of: a final answer, step budget, token
  budget, wall-clock budget, or three identical consecutive tool calls. Each run
  writes `runs/<run_id>/{trace.jsonl,messages.json,final.md}` for replay and
  debugging.

The reference reuses the `ToolCallingAgent` shape from Exercise 02 and layers the
budgets, the trace writer, and the repeat-call detector on top.

## Reference implementation

`agent/sandbox.py` — the Docker sandbox plus a labeled local fallback:

```python
"""Sandbox abstraction. DockerSandbox is the real one; LocalSandbox is a test-only fallback."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

STDOUT_CAP = 4000
STDERR_CAP = 2000


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class DockerSandbox:
    """Run commands inside a no-network container with the workspace bind-mounted."""

    def __init__(self, image: str, host_workspace: str, container_workspace="/workspace"):
        self.image = image
        self.host_workspace = host_workspace
        self.container_workspace = container_workspace

    def run(self, command: str, timeout_s: int = 30) -> ShellResult:
        argv = [
            "docker", "run", "--rm", "--network", "none",
            "--memory", "512m", "--cpus", "1",
            "-v", f"{self.host_workspace}:{self.container_workspace}",
            "-w", self.container_workspace,
            self.image, "bash", "-lc", command,
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_s + 5
            )
        except subprocess.TimeoutExpired as exc:
            return ShellResult(124, exc.stdout or "", "timeout", timed_out=True)
        return ShellResult(
            proc.returncode,
            proc.stdout[-STDOUT_CAP:],
            proc.stderr[-STDERR_CAP:],
        )


class LocalSandbox:
    """TEST-ONLY fallback. Runs on the host inside the workspace. Never use for a live agent run."""

    def __init__(self, host_workspace: str):
        self.host_workspace = host_workspace

    def run(self, command: str, timeout_s: int = 30) -> ShellResult:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.host_workspace,
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            return ShellResult(124, exc.stdout or "", "timeout", timed_out=True)
        return ShellResult(
            proc.returncode,
            proc.stdout[-STDOUT_CAP:],
            proc.stderr[-STDERR_CAP:],
        )
```

`agent/tools.py` — the three guarded tools:

```python
"""read_file / write_file / run_shell with path scoping and read-before-write."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 5_000_000


def _scoped(workspace: Path, path: str) -> Path | None:
    """Resolve `path` under `workspace`; return None if it escapes (incl. symlinks)."""
    candidate = (workspace / path).resolve()
    if candidate == workspace or workspace in candidate.parents:
        return candidate
    return None


@dataclass
class Workspace:
    """Holds the root and the read-before-write set for one agent run."""

    root: Path
    sandbox: object
    files_read: set[Path] = field(default_factory=set)

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> dict:
        target = _scoped(self.root, path)
        if target is None:
            return {"error": "path escapes workspace"}
        if not target.is_file():
            return {"error": f"no such file: {path}"}
        if target.stat().st_size > MAX_BYTES:
            return {"error": "file too large; use run_shell head/tail/grep"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[offset:offset + limit]
        self.files_read.add(target)
        return {
            "lines": chunk,
            "total_lines": len(lines),
            "next_offset": offset + len(chunk) if offset + limit < len(lines) else None,
        }

    def write_file(self, path: str, content: str) -> dict:
        target = _scoped(self.root, path)
        if target is None:
            return {"error": "path escapes workspace"}
        if target.exists() and target not in self.files_read:
            return {"error": "must read before overwriting; call read_file first"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.files_read.add(target)
        return {"ok": True, "path": str(target.relative_to(self.root))}

    def run_shell(self, command: str, timeout_s: int = 30) -> dict:
        result = self.sandbox.run(command, timeout_s=timeout_s)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }
```

`agent/budgets.py` — the four budgets plus the repeat-call detector:

```python
"""Termination budgets: steps, tokens, wall-clock, and consecutive identical calls."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CONTEXT_WINDOW_TOKENS = 128_000


@dataclass
class Budgets:
    max_steps: int = 25
    max_seconds: float = 300.0
    token_fraction: float = 0.80
    started_at: float = field(default_factory=time.monotonic)
    _recent: list[str] = field(default_factory=list)

    def step_exhausted(self, step: int) -> bool:
        return step >= self.max_steps

    def time_exhausted(self) -> bool:
        return time.monotonic() - self.started_at > self.max_seconds

    def token_exhausted(self, token_count: int) -> bool:
        return token_count > int(CONTEXT_WINDOW_TOKENS * self.token_fraction)

    def repeated(self, signature: str) -> bool:
        """True when the same tool call fired three times in a row."""
        self._recent.append(signature)
        self._recent = self._recent[-3:]
        return len(self._recent) == 3 and len(set(self._recent)) == 1
```

`agent/loop.py` — the Exercise 02 loop extended with budgets and tracing:

```python
"""Coding-agent loop: tool-calling plus budgets, trace writing, repeat detection."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .budgets import Budgets
from .tools import Workspace


class CodingAgent:
    def __init__(self, llm, workspace: Workspace, system_prompt: str, runs_dir="runs"):
        self.llm = llm
        self.ws = workspace
        self.system_prompt = system_prompt
        self.runs_dir = Path(runs_dir)
        self._schemas = _TOOL_SCHEMAS
        self._dispatch = {
            "read_file": self.ws.read_file,
            "write_file": self.ws.write_file,
            "run_shell": self.ws.run_shell,
        }

    def run(self, task: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        run_path = self.runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)
        trace = (run_path / "trace.jsonl").open("w", encoding="utf-8")
        budgets = Budgets()
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        answer = "(no answer)"
        try:
            for step in range(budgets.max_steps + 1):
                if budgets.step_exhausted(step) or budgets.time_exhausted():
                    answer = "budget exhausted"
                    break
                if budgets.token_exhausted(len(json.dumps(messages)) // 4):
                    answer = "token budget exhausted"
                    break
                message = self.llm.create(messages, self._schemas, "auto")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    answer = message.get("content") or ""
                    break
                stop = False
                for call in calls:
                    name = call["function"]["name"]
                    args = json.loads(call["function"]["arguments"] or "{}")
                    signature = f"{name}:{json.dumps(args, sort_keys=True)}"
                    result = self._dispatch[name](**args)
                    trace.write(json.dumps(
                        {"step": step, "tool": name, "args": args, "result": result}
                    ) + "\n")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    })
                    if budgets.repeated(signature):
                        answer = "stuck: repeated identical tool call"
                        stop = True
                if stop:
                    break
        finally:
            trace.close()
            (run_path / "messages.json").write_text(json.dumps(messages, indent=2))
            (run_path / "final.md").write_text(answer)
        return answer


_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 file under the workspace, with paging.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 2000},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a file under the workspace (read-before-write enforced).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "Run a shell command in the sandbox; returns exit_code/stdout/stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer", "default": 30},
        }, "required": ["command"]}}},
]
```

`tests/test_tools.py` — the security and termination tests:

```python
"""Path scoping, read-before-write, timeout/truncation, repeat-call termination."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.budgets import Budgets
from agent.sandbox import LocalSandbox
from agent.tools import Workspace


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    (tmp_path / "app.py").write_text("print('hi')\n")
    return Workspace(root=tmp_path.resolve(), sandbox=LocalSandbox(str(tmp_path)))


def test_relative_dotdot_rejected(ws):
    assert ws.read_file("../etc/passwd")["error"] == "path escapes workspace"


def test_absolute_outside_root_rejected(ws):
    assert ws.read_file("/etc/passwd")["error"] == "path escapes workspace"


def test_symlink_escape_rejected(ws, tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("top secret")
    (ws.root / "link").symlink_to(secret)
    assert ws.read_file("link")["error"] == "path escapes workspace"


def test_write_refuses_unread_existing_file(ws):
    result = ws.write_file("app.py", "print('clobber')")
    assert "must read before overwriting" in result["error"]
    ws.read_file("app.py")
    assert ws.write_file("app.py", "print('ok')")["ok"] is True


def test_run_shell_truncates_and_times_out(ws):
    big = ws.run_shell("python3 -c \"print('x'*10000)\"")
    assert len(big["stdout"]) <= 4000
    slow = ws.run_shell("sleep 5", timeout_s=1)
    assert slow["timed_out"] is True


def test_repeat_call_detector_terminates():
    budgets = Budgets()
    sig = 'run_shell:{"command": "ls"}'
    assert budgets.repeated(sig) is False
    assert budgets.repeated(sig) is False
    assert budgets.repeated(sig) is True  # third identical call -> stop
```

`Makefile` — reset-and-run targets for the two demo tasks:

```text
demo-a:
	git -C fixture-repo stash --include-untracked || true
	git -C fixture-repo restore .
	python -m agent.cli --task tasks/task_a.md

demo-b:
	git -C fixture-repo stash --include-untracked || true
	git -C fixture-repo restore .
	python -m agent.cli --task tasks/task_b.md
```

## Meeting the acceptance criteria

- **`make demo-a` resets the repo, runs the agent, ends green.** The target
  restores the fixture with `git restore .`, then runs the agent on Task A (a
  function returning the wrong value); the trace shows reads, one targeted edit,
  and a final `pytest` run.
- **`make demo-b` resets and adds the `/healthz` route.** Same reset, then Task B
  (a missing endpoint). When the model cannot solve it reliably, the run is
  still shipped and the failure documented in `RUN-LOG.md` — that catalog is the
  deliverable.
- **`pytest tests/test_tools.py` covers the required guards.** Path-scoping
  rejection for `..`, absolute-outside-root, and symlink escape; read-before-
  write refusal then success; `run_shell` truncation and timeout.
- **`run_shell` cannot reach the host filesystem.** `DockerSandbox` runs with
  `--network none` and only the workspace bind-mounted, so `ls /etc` inside the
  container does not expose the host. The `LocalSandbox` is used only by the unit
  tests, never for a live run.
- **Loop terminates on a synthetic infinite loop.** `Budgets.repeated` returns
  `True` on the third identical signature; `test_repeat_call_detector_terminates`
  proves it, and `loop.py` breaks when it fires.
- **Every run produces a greppable `trace.jsonl`.** One JSON line per tool call
  is written under `runs/<run_id>/`, alongside `messages.json` and `final.md`.

## Common pitfalls

- **Checking containment before resolving.** Validating the raw string for `..`
  misses symlinks and normalized paths. Always `resolve()` first, then test
  containment against the resolved workspace root.
- **Running `run_shell` on the host "just for the demo".** A failed loop can then
  `rm -rf` real files or `cat .env`. Wire the Docker sandbox before the first
  live run; the `LocalSandbox` label exists precisely to keep it out of agent
  runs.
- **Forgetting one termination path.** Step budget alone is not enough — a model
  that emits the same `run_shell("pytest")` forever burns wall-clock and tokens.
  Wire all four budgets plus the repeat detector and break on whichever trips
  first.
- **Head-truncating shell output.** Stack traces put the actual error at the
  bottom; head-truncation throws away the useful part. Tail-truncate stdout and
  stderr.
- **Not resetting the fixture between runs.** A second `make demo-a` against an
  already-patched repo "passes" for the wrong reason. Reset from a known snapshot
  (`git restore .` or `docker run --rm`) before every run.

## Verification

```bash
cd exercise-03-coding-agent-read-write-execute
python -m venv .venv && source .venv/bin/activate
python -m pip install pytest

python -m pytest tests/test_tools.py -q   # scoping, RBW, timeout, repeat-call pass
make demo-a                               # resets fixture, agent fixes the bug, tests green
make demo-b                               # resets fixture, agent adds /healthz (or logs why not)
```

The tool tests run with the `LocalSandbox` and need no Docker. The `make demo-*`
targets require Docker (for `DockerSandbox`) and a configured LLM adapter;
inspect `runs/<run_id>/trace.jsonl` after each run and summarize five runs of
each task in `RUN-LOG.md`.
