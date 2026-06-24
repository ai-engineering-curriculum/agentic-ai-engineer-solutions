# mod-206-guardrails-implementation/exercise-03-tool-permission-enforcement — Solution

## Approach

Two ideas carry this exercise, and both are about *where* the decision is made.

First, **the policy layer decides, not the model.** When the exercise says the
`reader` "cannot reach" `fetch_url`, it does not mean the model politely
declines — it means the enforcement code refuses regardless of what the model
asks for. The model is adversarial in this threat model; the boundary has to
hold against a model that *wants* to escalate.

Second, **scope the arguments, not just the tool names.** Allowing `read_file`
is meaningless if `read_file("/etc/shadow")` then succeeds. The interesting
checks are at the argument level: resolve the path and test containment against
an allowed root; parse the URL and compare the *host* against an allowlist
(never substring-match). Everything not explicitly classified is a deny —
default-deny is the invariant that survives adding a fourth tool tomorrow.

The design is a `Policy` dataclass (allowed tools, read root, URL allowlist),
a role→policy map where `reader` is a strict subset of `publisher`, and a
single `enforce` entry point that every call flows through. The genuinely
dangerous tool, `run_code`, gets a real sandbox: `subprocess` with an argv list
(no `shell=True`), a confined `cwd`, a scrubbed `env`, a timeout, and POSIX
`setrlimit`. In-process `eval`/`exec` is explicitly *not* a sandbox — it shares
your interpreter, your memory, and your secrets; only a separate process with
dropped resources gives you a blast-radius boundary.

## Reference implementation

### `policy.py` — policies and the role map

```python
"""Policies, roles, and the strict-subset relationship between them."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    allowed_tools: frozenset[str]
    read_root: Path
    url_allowlist: frozenset[str]


def build_roles(read_root: Path) -> dict[str, Policy]:
    """reader is a strict subset of publisher; neither can run code."""
    reader = Policy(
        allowed_tools=frozenset({"read_file"}),
        read_root=read_root.resolve(),
        url_allowlist=frozenset(),
    )
    publisher = Policy(
        allowed_tools=frozenset({"read_file", "fetch_url"}),
        read_root=read_root.resolve(),
        url_allowlist=frozenset({"example.com", "docs.example.com"}),
    )
    return {"reader": reader, "publisher": publisher}


def is_subset(narrow: Policy, wide: Policy) -> bool:
    """True if `narrow` grants nothing `wide` does not. Checked on startup."""
    return (
        narrow.allowed_tools <= wide.allowed_tools
        and narrow.url_allowlist <= wide.url_allowlist
    )
```

### `enforcement.py` — argument-level checks, default-deny

```python
"""One enforce() entry point. Argument-level. Default-deny."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from policy import Policy

log = logging.getLogger("permissions")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _allow_read(policy: Policy, path: str) -> Decision:
    if "read_file" not in policy.allowed_tools:
        return Decision(False, "read_file not in role's allowed tools")
    try:
        target = (policy.read_root / path).resolve()
    except (OSError, ValueError) as exc:
        return Decision(False, f"unresolvable path: {exc}")
    if target == policy.read_root or policy.read_root in target.parents:
        return Decision(True, "path within read_root")
    return Decision(False, f"path escapes read_root: {path!r}")


def _allow_fetch(policy: Policy, url: str) -> Decision:
    if "fetch_url" not in policy.allowed_tools:
        return Decision(False, "fetch_url not in role's allowed tools")
    host = urlparse(url).hostname  # parsed host, never a substring match
    if host in policy.url_allowlist:
        return Decision(True, f"host {host!r} on allowlist")
    return Decision(False, f"host {host!r} not on allowlist")


def enforce(role: str, policy: Policy, tool: str, args: dict) -> Decision:
    """Every tool call flows through here. Unknown tool/arg -> deny."""
    if tool == "read_file":
        decision = _allow_read(policy, str(args.get("path", "")))
    elif tool == "fetch_url":
        decision = _allow_fetch(policy, str(args.get("url", "")))
    elif tool == "run_code":
        # Only a future `executor` role ever gets this; default-deny otherwise.
        decision = Decision(
            "run_code" in policy.allowed_tools,
            "run_code not in role's allowed tools"
            if "run_code" not in policy.allowed_tools else "permitted",
        )
    else:
        decision = Decision(False, f"unknown tool {tool!r} (default-deny)")

    if not decision.allowed:
        log.warning("DENY role=%s tool=%s args=%s reason=%s",
                    role, tool, args, decision.reason)
    return decision
```

### `sandbox.py` — the real sandbox for `run_code`

```python
"""run_code in a subprocess with a confined cwd, scrubbed env, and limits."""
from __future__ import annotations

import subprocess
import sys

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX fallback
    resource = None

# Megabytes of address space the child may map before the kernel kills it.
_MEM_LIMIT_BYTES = 256 * 1024 * 1024


def _drop_limits() -> None:
    """Run in the child before exec: cap CPU seconds and address space."""
    if resource is None:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))


def run_sandboxed(source: str, cwd: str,
                  timeout_s: int = 5) -> subprocess.CompletedProcess:
    """Execute `source` as a separate process. No shell, no inherited env."""
    return subprocess.run(
        [sys.executable, "-I", "-c", source],  # argv list, isolated mode
        cwd=cwd,                                # confined working directory
        env={"PATH": "/usr/bin"},               # scrubbed: no parent secrets
        capture_output=True,
        text=True,
        timeout=timeout_s,                      # runaway -> TimeoutExpired
        check=False,
        preexec_fn=_drop_limits if resource else None,
    )
```

### `agent.py` — the single guarded call surface

```python
"""The agent reaches tools only through guarded_call."""
from __future__ import annotations

from enforcement import Decision, enforce
from policy import Policy
from sandbox import run_sandboxed


def guarded_call(role: str, policy: Policy, tool: str, args: dict) -> str:
    """No tool runs unless enforce() allows it first."""
    decision: Decision = enforce(role, policy, tool, args)
    if not decision.allowed:
        return f"[denied] {decision.reason}"

    if tool == "read_file":
        from pathlib import Path
        target = (policy.read_root / str(args["path"])).resolve()
        return target.read_text(encoding="utf-8")
    if tool == "fetch_url":
        return f"[fetched] {args['url']}"  # stubbed network
    if tool == "run_code":
        result = run_sandboxed(str(args["source"]), cwd=str(policy.read_root))
        return result.stdout or result.stderr
    return "[denied] no handler"
```

### `demo.py` — proves the boundary and the sandbox

```python
"""Runnable demonstration of all five acceptance criteria."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import guarded_call
from policy import build_roles, is_subset
from sandbox import run_sandboxed


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ok.txt").write_text("safe content", encoding="utf-8")
        roles = build_roles(root)
        reader, publisher = roles["reader"], roles["publisher"]

        print("== reader is a strict subset of publisher ==")
        print("subset?", is_subset(reader, publisher))

        print("\n== argument-level enforcement ==")
        print("read ok    :", guarded_call("reader", reader, "read_file",
                                            {"path": "ok.txt"}))
        print("traversal  :", guarded_call("reader", reader, "read_file",
                                            {"path": "../../etc/passwd"}))

        print("\n== reader cannot reach fetch_url or run_code ==")
        print("fetch_url  :", guarded_call("reader", reader, "fetch_url",
                                            {"url": "http://example.com"}))
        print("run_code   :", guarded_call("reader", reader, "run_code",
                                            {"source": "print(1)"}))

        print("\n== publisher off-allowlist host is denied ==")
        print("evil host  :", guarded_call("publisher", publisher, "fetch_url",
                                            {"url": "http://evil.example"}))
        print("good host  :", guarded_call("publisher", publisher, "fetch_url",
                                            {"url": "http://example.com/x"}))

        print("\n== sandbox: runaway killed; parent secret not visible ==")
        os.environ["PARENT_SECRET"] = "topsecret"
        try:
            run_sandboxed("while True: pass", cwd=str(root), timeout_s=2)
        except subprocess.TimeoutExpired:
            print("runaway    : killed by timeout")
        leaked = run_sandboxed(
            "import os; print(os.environ.get('PARENT_SECRET', 'NOT_VISIBLE'))",
            cwd=str(root),
        )
        print("env scrub  :", leaked.stdout.strip())


if __name__ == "__main__":
    main()
```

## Meeting the acceptance criteria

- **Two roles map to two policies; reader ⊂ publisher.** `build_roles` returns
  both, and `is_subset(reader, publisher)` returns `True` because the reader's
  tool set and URL allowlist are subsets of the publisher's. Validate this on
  startup so a misconfigured role can never silently grant *more* than its
  parent.
- **Path-traversal read and off-allowlist fetch are denied by the policy
  layer.** `_allow_read` resolves the path and requires `read_root in
  target.parents`; `../../etc/passwd` fails and returns `[denied]`.
  `_allow_fetch` compares the *parsed* host, so `evil.example` is denied even
  though the string contains `example`.
- **The reader cannot reach `fetch_url` or `run_code` regardless of the
  model.** Both tools are absent from the reader's `allowed_tools`, so `enforce`
  denies at the tool-membership check before any argument is even inspected. The
  model requesting them changes nothing.
- **`run_code` runs sandboxed; runaway killed, parent secrets hidden.**
  `run_sandboxed` uses an argv list (no `shell=True`), `-I` isolated mode, a
  scrubbed `env={"PATH": "/usr/bin"}`, a timeout, and POSIX `setrlimit`. The
  demo shows `while True: pass` raising `TimeoutExpired` and the child printing
  `NOT_VISIBLE` for `PARENT_SECRET`.
- **Every denial is logged with role, tool, args, reason.** `enforce` emits
  `DENY role=... tool=... args=... reason=...` on every denied call — the
  evidence the control fired.

## Common pitfalls

- **Trusting the model to decline.** "The model won't call `run_code`" is not a
  control. The boundary must be code that denies even when the model insists;
  test it by issuing the forbidden call directly, not by prompting.
- **Substring host matching.** `if "example.com" in url` passes
  `evil-example.com.attacker.net` and `http://example.com.evil.net`. Parse the
  URL and compare `urlparse(url).hostname` exactly against the allowlist.
- **Blocklisting paths instead of allowlisting a root.** Enumerating bad paths
  (`/etc`, `..`) always misses one (`....//`, symlinks, encoded separators).
  Resolve the path and test containment against the resolved root.
- **`eval()`/`exec()` as a "sandbox".** In-process execution shares your
  interpreter, memory, file descriptors, and environment — a malicious snippet
  reads your secrets and never terminates. Only a separate process with a
  timeout and dropped `rlimit` gives a real boundary.
- **Default-allow on unknown tools.** If `enforce` falls through to "allow" for
  a tool it doesn't recognize, the next tool you add is ungated by accident.
  The fallthrough must be a deny.

## Verification

```bash
cd modules/mod-206-guardrails-implementation/exercise-03-tool-permission-enforcement
python demo.py
```

Expected (POSIX): `subset? True`; the reader's `read_file("ok.txt")` returns
`safe content` while the traversal, `fetch_url`, and `run_code` all return
`[denied] ...`; the publisher's `evil.example` fetch is denied and the
`example.com` fetch is allowed; the runaway prints `killed by timeout`; and the
env-scrub line prints `NOT_VISIBLE`, proving `PARENT_SECRET` did not cross into
the child. The `DENY ...` log lines show each control firing.

On non-POSIX hosts `setrlimit` is skipped (the `resource` import guard handles
it), but the timeout and env scrub still hold. Run `run_code` in a disposable
container (read-only root, dropped capabilities, no network) for the stretch
goal.
