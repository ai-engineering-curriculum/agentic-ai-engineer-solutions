# mod-206-guardrails-implementation/exercise-04-human-approval-checkpoints — Solution

## Approach

This is the backstop: the control that holds *after* moderation, isolation, and
least privilege have all been bypassed. When an agent is about to take a
high-risk, irreversible action, a person sees the **concrete call** and decides.

Four design choices carry the exercise:

1. **Classify on tool *and* arguments.** `delete_file` always gates;
   `spend` gates above a threshold; `send_email` gates only when the recipient
   is outside our domain. Gating on the tool name alone either fatigues the
   approver (everything gates) or leaves a hole (a parameter slips an action
   past). The argument is where the danger usually lives.
2. **Show the full concrete action.** "Approve? [y/n]" is useless — the approver
   must see the exact recipient, the exact amount, the exact path, and the
   agent's stated reason. The reflection's key case is precisely this: approving
   a *plan* ("send a summary email") can be safe while the *concrete call*
   ("send to attacker@evil.com") is the breach. You catch hijacked arguments
   only by surfacing them.
3. **Fail closed on timeout.** No decision within the window is a **deny**, never
   an approve. An unattended checkpoint must not become an auto-yes.
4. **Close the loop.** On denial, return a tool result the agent can reason
   about (`"action denied by human: <reason>"`) so it replans instead of
   re-requesting the identical call in a loop.

Every decision — approved or denied — is persisted to an append-only audit log,
which pairs with the observability work in mod-205.

## Reference implementation

### `risk.py` — argument-aware risk classifier

```python
"""needs_approval keys on the tool AND its arguments."""
from __future__ import annotations

# Tools whose every invocation is irreversible enough to always gate.
ALWAYS_GATE = frozenset({"delete_file", "deploy", "publish"})

INTERNAL_DOMAIN = "ourcompany.com"
SPEND_THRESHOLD = 100.0


def needs_approval(tool: str, args: dict) -> bool:
    """True if this concrete call must pause for a human."""
    if tool in ALWAYS_GATE:
        return True
    if tool == "spend":
        return float(args.get("amount", 0)) > SPEND_THRESHOLD
    if tool == "send_email":
        recipient = str(args.get("to", ""))
        domain = recipient.rsplit("@", 1)[-1].lower()
        return domain != INTERNAL_DOMAIN  # external recipient gates
    return False  # search, read_file, ... auto-run
```

### `checkpoint.py` — the approval mechanics

```python
"""Present the concrete action, capture a decision, fail closed on timeout."""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("approval")


@dataclass(frozen=True)
class Decision:
    approved: bool
    decided_by: str
    decided_at: str
    reason: str = ""
    edited_args: dict | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render(tool: str, args: dict, why: str) -> str:
    """The full concrete action — recipient, amount, path all visible."""
    lines = [f"  APPROVAL REQUIRED for tool: {tool}", f"  reason: {why}"]
    for key, value in args.items():
        lines.append(f"    {key} = {value!r}")
    return "\n".join(lines)


def _read_with_timeout(prompt_fn, timeout_s: int) -> str | None:
    """Read one line, or return None if the window elapses (fail closed)."""
    result: queue.Queue[str] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put(prompt_fn())
        except (EOFError, OSError):
            pass

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        return result.get(timeout=timeout_s)
    except queue.Empty:
        return None  # timed out -> caller denies


def request_approval(tool: str, args: dict, why: str,
                     timeout_s: int = 60,
                     prompt_fn=None, approver: str = "cli-user") -> Decision:
    """Show the action, capture y/n, deny on timeout."""
    print(_render(tool, args, why))
    prompt_fn = prompt_fn or (lambda: input("  approve? [y/N]: ").strip())

    answer = _read_with_timeout(prompt_fn, timeout_s)
    if answer is None:
        log.warning("approval timed out -> DENY (fail closed) tool=%s", tool)
        return Decision(False, approver, now(), "timeout: failed closed")

    approved = answer.lower() in ("y", "yes")
    reason = "approved by human" if approved else "denied by human"
    return Decision(approved, approver, now(), reason)
```

### `audit.py` — append-only decision log

```python
"""Persist every checkpoint decision (approved and denied)."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from checkpoint import Decision


def record(log_path: Path, tool: str, args: dict, decision: Decision) -> None:
    """Append one JSON line per decision. Pairs with mod-205 observability."""
    entry = {"tool": tool, "args": args, **asdict(decision)}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
```

### `agent.py` — gated execution and replanning

```python
"""The agent executes high-risk tools only through gated_execute."""
from __future__ import annotations

from pathlib import Path

from audit import record
from checkpoint import Decision, request_approval
from risk import needs_approval


def gated_execute(tool: str, args: dict, why: str, run_tool,
                  audit_path: Path, timeout_s: int = 60,
                  prompt_fn=None) -> str:
    """Low-risk tools auto-run; high-risk tools pause for approval."""
    if not needs_approval(tool, args):
        return run_tool(tool, args)

    decision: Decision = request_approval(
        tool, args, why, timeout_s=timeout_s, prompt_fn=prompt_fn)
    record(audit_path, tool, args, decision)

    if not decision.approved:
        # Close the loop: a result the agent can replan against.
        return f"action denied by human: {decision.reason}"

    effective = decision.edited_args or args  # supports edit-before-approve
    return run_tool(tool, effective)


def replan_on_denial(result: str) -> str:
    """A sensible next step instead of re-requesting the identical call."""
    if result.startswith("action denied by human"):
        return ("The action was denied. I will not retry it. "
                "I'll ask the user how they'd like to proceed.")
    return result
```

### `demo.py` — proves each acceptance criterion

```python
"""Runnable demonstration with scripted approvers (no manual input)."""
from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import gated_execute, replan_on_denial
from risk import needs_approval


def fake_tool(tool: str, args: dict) -> str:
    return f"[executed] {tool}({args})"


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    with TemporaryDirectory() as tmp:
        audit = Path(tmp) / "approvals.jsonl"

        print("== low-risk auto-runs, no prompt ==")
        print(gated_execute("search", {"q": "weather"}, "user asked",
                            fake_tool, audit))

        print("\n== high-risk pauses; APPROVE executes ==")
        print(gated_execute(
            "send_email", {"to": "client@external.com", "body": "hi"},
            "send the requested summary", fake_tool, audit,
            prompt_fn=lambda: "y"))

        print("\n== high-risk DENY does not execute; agent replans ==")
        denied = gated_execute(
            "delete_file", {"path": "/data/prod.db"},
            "cleanup", fake_tool, audit, prompt_fn=lambda: "n")
        print(denied)
        print("replan:", replan_on_denial(denied))

        print("\n== timeout fails CLOSED (denied) ==")
        def hang() -> str:
            import time
            time.sleep(5)
            return "y"
        timed = gated_execute(
            "spend", {"amount": 500, "to": "vendor"}, "purchase",
            fake_tool, audit, timeout_s=1, prompt_fn=hang)
        print(timed)

        print("\n== argument-aware classification ==")
        print("internal email gates? ",
              needs_approval("send_email", {"to": "bob@ourcompany.com"}))
        print("external email gates? ",
              needs_approval("send_email", {"to": "bob@evil.com"}))
        print("small spend gates?    ",
              needs_approval("spend", {"amount": 10}))

        print("\n== audit trail ==")
        print(audit.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    main()
```

## Meeting the acceptance criteria

- **Low-risk auto-runs; high-risk and over-threshold spends pause.**
  `needs_approval` returns `False` for `search`/`read_file` (they run with no
  prompt), `True` for `delete_file`, external `send_email`, and
  `spend > 100`. The demo shows `search` executing immediately and `spend(500)`
  gating.
- **The checkpoint shows the full concrete action.** `_render` prints the tool,
  the agent's reason, and every argument (`to = 'client@external.com'`,
  `path = '/data/prod.db'`, `amount = 500`) — not a vague summary. This is what
  lets a human catch a hijacked recipient.
- **Approved executes; denied does not; timeout fails closed.** With
  `prompt_fn=lambda: "y"` the email is executed; with `"n"` the delete returns
  `action denied by human: ...` and `run_tool` is never called; with
  `timeout_s=1` against a hanging approver, `_read_with_timeout` returns `None`
  and the decision is a **deny** — the spend never runs.
- **On denial the agent replans.** `gated_execute` returns a structured
  `"action denied by human: <reason>"`; `replan_on_denial` turns that into a
  sensible next step (ask the user) rather than re-issuing the identical call.
- **Every decision is persisted with who, when, outcome.** `audit.record`
  appends a JSON line — `tool`, `args`, `approved`, `decided_by`, `decided_at`,
  `reason` — for both approvals and denials. The demo prints the resulting log.

## Common pitfalls

- **Approving the plan, not the call.** A human who approves "send a summary
  email" never sees that the recipient was rewritten to `attacker@evil.com` by
  an upstream injection. Always surface the *resolved* arguments at the moment
  of execution, not the abstract intent.
- **Defaulting to approve on timeout.** An unattended checkpoint that auto-yeses
  is strictly worse than no checkpoint — it manufactures consent. Timeout must
  be deny; the demo proves the hanging approver results in no execution.
- **Gating everything → approval fatigue.** If every tool prompts, approvers
  rubber-stamp and the control rots. Tune the classifier on arguments (internal
  recipients, small spends auto-run) so the human's attention is spent only on
  genuinely risky calls — without leaving an irreversible action ungated.
- **A bare yes/no prompt.** "Approve? [y/n]" with no context forces a blind
  decision. Render recipient, amount, and path so the approver can actually
  judge the action.
- **Not closing the loop.** If a denied call returns nothing useful, the agent
  re-requests the identical action and loops. Return a denial reason the agent
  can replan against, and have it pick a safe alternative or ask the user.

## Verification

```bash
cd modules/mod-206-guardrails-implementation/exercise-04-human-approval-checkpoints
python demo.py
```

Expected: `search` prints `[executed] search(...)` with no prompt; the approved
email prints `[executed] send_email(...)`; the denied `delete_file` prints
`action denied by human: denied by human` followed by a replan message; the
`spend(500)` under a 1-second timeout prints `action denied by human: timeout:
failed closed`; the classification lines show internal email `False`, external
email `True`, small spend `False`; and the audit trail prints one JSON line per
gated decision (approved and denied alike).

`demo.py` uses scripted `prompt_fn` callables so it runs unattended; in real use,
omit `prompt_fn` and the checkpoint reads from `input()`. For the stretch goals,
populate `Decision.edited_args` from the approver to run a corrected recipient,
and add a per-action expiry so a stale approval cannot be replayed.
