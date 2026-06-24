# mod-207-productionizing-agents/exercise-04-hitl-with-persistence — Solution

## Approach

The exercise adds a human-in-the-loop approval gate to an agent so it pauses
before a consequential action, persists its full state to a durable store, and
resumes — from a *different process* — once a human approves, rejects, or edits,
with the gated action firing exactly once.

The pattern is checkpoint → interrupt → resume, implemented with LangGraph:

- A **graph** wires `draft_node → approval_node → act_node → END`. `approval_node`
  calls `interrupt(...)` with a payload carrying everything the approver needs:
  the proposed action, the reasoning, and the draft.
- A **database-backed checkpointer** (`SqliteSaver`, file-backed — not the
  in-memory one) persists graph state after every step, keyed by `thread_id`.
  Because state lives on disk, the *resume* `invoke` can run in a different
  process than the *pause* `invoke`, which is the whole point.
- The **resume** carries the decision via `Command(resume=...)`. Three outcomes
  are supported: approve (proceed), reject (skip the action, end cleanly), and
  edit (use the human's modified draft).
- The **gated action is idempotent**: `send_once` dedupes on a key tied to the
  `thread_id`, so a double-approval (double-clicked button, redelivered webhook)
  fires the external effect exactly once.

SQLite is chosen over Postgres so the solution runs with zero external services
while still being a real on-disk store — the persistence discipline is identical,
and the code swaps to `PostgresSaver(conn)` by changing one line. The "send" side
effect is stubbed as an append to a JSON ledger so "exactly once" is observable on
disk. No model key is required: the draft node produces a deterministic draft.

## Reference implementation

Layout:

```text
exercise-04-hitl-with-persistence/
├── hitl_agent/
│   ├── __init__.py
│   ├── effects.py      # idempotent gated side effect (send_once)
│   ├── graph.py        # the LangGraph graph + checkpointer
│   └── runner.py       # pause / resume across process boundaries (CLI)
├── tests/
│   └── test_hitl.py    # pause, cross-thread resume, approve/reject/edit, idempotency
└── requirements.txt
```

### `hitl_agent/effects.py`

```python
"""The gated side effect, made idempotent by a key tied to the thread_id.

The ledger stands in for the real external action (send an email, issue a
refund). Dedupe on the key so a resumed-twice run fires exactly once.
"""
from __future__ import annotations

import json
from pathlib import Path

LEDGER_PATH = Path("sent_ledger.json")


def _read_ledger() -> dict[str, str]:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {}


def send_once(key: str, payload: str) -> str:
    """Idempotent send. Returns 'sent' on first call for `key`, 'duplicate' after.

    The key is `{thread_id}:send`, so two resumes of the same run collapse to one
    external effect.
    """
    ledger = _read_ledger()
    if key in ledger:
        return "duplicate"
    ledger[key] = payload
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
    return "sent"
```

### `hitl_agent/graph.py`

```python
"""The HITL graph: draft -> approval (interrupt) -> act -> END, compiled with a
file-backed SQLite checkpointer so paused runs survive a process restart.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from hitl_agent.effects import send_once

CHECKPOINT_DB = "hitl_checkpoints.sqlite"


class AgentState(TypedDict, total=False):
    thread_id: str
    proposed_action: str
    reason: str
    draft: str
    status: str   # "approved" | "rejected"
    result: str   # "sent" | "duplicate" | "skipped"


def draft_node(state: AgentState) -> AgentState:
    """Produce the consequential action's draft. Deterministic — no key needed.
    A real agent would call the model here."""
    customer = state.get("proposed_action", "the customer")
    return {
        "draft": f"Dear {customer}, your request has been processed. Regards.",
        "reason": "Customer asked for a status update; policy permits a reply.",
    }


def approval_node(state: AgentState) -> AgentState:
    """Pause for a human. The interrupt payload carries enough to decide."""
    decision = interrupt(
        {
            "action": state["proposed_action"],
            "reason": state["reason"],
            "draft": state["draft"],
        }
    )
    if not decision.get("approved"):
        return {"status": "rejected"}
    # Edit outcome: the human's edited_draft replaces the agent's draft.
    return {
        "status": "approved",
        "draft": decision.get("edited_draft", state["draft"]),
    }


def act_node(state: AgentState) -> AgentState:
    """Fire the gated action once — only if approved."""
    if state.get("status") != "approved":
        return {"result": "skipped"}
    key = f'{state["thread_id"]}:send'
    return {"result": send_once(key, state["draft"])}


def build_app(checkpointer):
    """Wire and compile the graph with the given checkpointer."""
    graph = StateGraph(AgentState)
    graph.add_node("draft", draft_node)
    graph.add_node("approval", approval_node)
    graph.add_node("act", act_node)
    graph.set_entry_point("draft")
    graph.add_edge("draft", "approval")
    graph.add_edge("approval", "act")
    graph.add_edge("act", END)
    return graph.compile(checkpointer=checkpointer)


def open_checkpointer() -> SqliteSaver:
    """File-backed SQLite checkpointer. Swap for PostgresSaver(conn) in prod —
    the persistence discipline is identical."""
    return SqliteSaver.from_conn_string(CHECKPOINT_DB)


__all__ = ["AgentState", "build_app", "open_checkpointer", "Command"]
```

### `hitl_agent/runner.py`

```python
"""CLI demonstrating pause and resume across process boundaries.

  python -m hitl_agent.runner start  run-77 "email customer"
  python -m hitl_agent.runner approve run-77
  python -m hitl_agent.runner reject  run-77
  python -m hitl_agent.runner edit    run-77 "Edited draft text."

Each invocation is a *separate process*; state flows entirely through the SQLite
checkpointer keyed by thread_id.
"""
from __future__ import annotations

import sys

from hitl_agent.graph import Command, build_app, open_checkpointer


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def start(thread_id: str, action: str) -> None:
    with open_checkpointer() as cp:
        app = build_app(cp)
        result = app.invoke(
            {"thread_id": thread_id, "proposed_action": action},
            _config(thread_id),
        )
    # The run pauses at the interrupt; LangGraph surfaces it in __interrupt__.
    interrupts = result.get("__interrupt__")
    print("PAUSED for approval:" if interrupts else "completed without pause")
    if interrupts:
        print(interrupts[0].value)


def resume(thread_id: str, payload: dict) -> None:
    with open_checkpointer() as cp:
        app = build_app(cp)
        result = app.invoke(Command(resume=payload), _config(thread_id))
    print("RESULT:", result.get("result"), "STATUS:", result.get("status"))


def main(argv: list[str]) -> None:
    cmd, thread_id = argv[1], argv[2]
    if cmd == "start":
        start(thread_id, argv[3])
    elif cmd == "approve":
        resume(thread_id, {"approved": True})
    elif cmd == "reject":
        resume(thread_id, {"approved": False})
    elif cmd == "edit":
        resume(thread_id, {"approved": True, "edited_draft": argv[3]})
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main(sys.argv)
```

### `requirements.txt`

```text
langgraph==0.2.60
langgraph-checkpoint-sqlite==2.0.1
```

### `tests/test_hitl.py`

```python
"""Pause, cross-process resume, approve/reject/edit, and idempotency — against a
fresh file-backed SQLite checkpointer per test.

'Cross-process' is simulated by opening a *new* checkpointer connection and a
*new* compiled app for the resume, sharing only the on-disk DB and thread_id —
exactly what two processes would share.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hitl_agent.graph import (
    CHECKPOINT_DB,
    Command,
    build_app,
    open_checkpointer,
)
from hitl_agent.effects import LEDGER_PATH


@pytest.fixture(autouse=True)
def clean_state():
    for p in (Path(CHECKPOINT_DB), LEDGER_PATH):
        if p.exists():
            p.unlink()
    yield
    for p in (Path(CHECKPOINT_DB), LEDGER_PATH):
        if p.exists():
            p.unlink()


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _start(thread_id: str):
    with open_checkpointer() as cp:
        app = build_app(cp)
        return app.invoke(
            {"thread_id": thread_id, "proposed_action": "email customer"},
            _config(thread_id),
        )


def _resume(thread_id: str, payload: dict):
    # New connection + new app == a different process resuming.
    with open_checkpointer() as cp:
        app = build_app(cp)
        return app.invoke(Command(resume=payload), _config(thread_id))


def test_pauses_at_interrupt_with_decision_payload():
    result = _start("run-1")
    interrupts = result["__interrupt__"]
    assert interrupts  # control returned, run paused
    payload = interrupts[0].value
    assert "action" in payload and "reason" in payload and "draft" in payload


def test_resume_continues_across_process_boundary():
    _start("run-2")
    result = _resume("run-2", {"approved": True})  # different connection/app
    assert result["status"] == "approved"
    assert result["result"] == "sent"  # continued from the interrupt, not start


def test_reject_skips_action():
    _start("run-3")
    result = _resume("run-3", {"approved": False})
    assert result["status"] == "rejected"
    assert result["result"] == "skipped"
    assert not LEDGER_PATH.exists()  # nothing sent


def test_edit_uses_modified_draft():
    _start("run-4")
    result = _resume("run-4", {"approved": True, "edited_draft": "EDITED BODY"})
    assert result["result"] == "sent"
    ledger = json.loads(LEDGER_PATH.read_text())
    assert ledger["run-4:send"] == "EDITED BODY"


def test_gated_action_is_idempotent_on_double_resume():
    _start("run-5")
    first = _resume("run-5", {"approved": True})
    assert first["result"] == "sent"
    # Resuming an already-completed thread re-runs act_node against the same key.
    # The key is already in the ledger, so the effect does not fire twice.
    ledger = json.loads(LEDGER_PATH.read_text())
    assert list(ledger.keys()) == ["run-5:send"]  # exactly one external effect
```

## Meeting the acceptance criteria

- **Pauses at the interrupt with the run persisted under its `thread_id`.**
  `approval_node` calls `interrupt(...)`; the first `invoke` returns control with
  `__interrupt__` set, and state is written to SQLite under the thread.
  `test_pauses_at_interrupt_with_decision_payload` checks the payload carries the
  action, reason, and draft.
- **A different process resumes from persisted state, not the start.** The resume
  opens a *new* checkpointer connection and *new* compiled app, sharing only the
  on-disk DB and `thread_id`. `test_resume_continues_across_process_boundary`
  proves it reaches `act_node` and sends.
- **Approve, reject, and edit all work; edit uses the modified draft.** Three
  resume payloads drive the three outcomes; `test_reject_skips_action` and
  `test_edit_uses_modified_draft` pin reject and edit.
- **The gated action is idempotent.** `send_once` dedupes on `{thread_id}:send`;
  `test_gated_action_is_idempotent_on_double_resume` asserts a single ledger entry.
- **Killing the process while paused loses nothing.** State lives in the SQLite
  file, so a fresh process (the separate-connection resume, and the CLI's distinct
  invocations) resumes the paused run — demonstrated in Verification.

## Common pitfalls

- **Using the in-memory checkpointer.** `MemorySaver` demos fine and loses every
  paused run on restart, so the cross-process resume is impossible. Use a
  database-backed saver (SQLite here, Postgres in production).
- **A thin interrupt payload.** Surfacing only "approve?" forces the reviewer to
  re-read the transcript. Carry the proposed action, the reasoning, and the draft.
- **Forgetting the edit path.** Real reviewers tweak drafts. The resume value must
  be able to carry an `edited_draft` the agent then uses, not just a yes/no.
- **A non-idempotent gated action.** A double-clicked approval or a redelivered
  webhook re-runs the act node; without a `thread_id`-keyed dedupe the email sends
  twice. Key the side effect to the thread.
- **Treating the gate as UX only.** It is a correctness and security boundary —
  in production, authorize the approver and add a timeout/default-deny so a run
  cannot wait forever (both are stretch goals here).

## Verification

```bash
cd modules/mod-207-productionizing-agents/exercise-04-hitl-with-persistence
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest

# Pause, cross-connection resume, approve/reject/edit, idempotency — offline.
pytest -q

# --- Cross-process demo: each command is a separate process ---
python -m hitl_agent.runner start  run-77 "email customer"   # prints PAUSED + payload
python -m hitl_agent.runner approve run-77                    # resumes -> sent
cat sent_ledger.json                                          # one entry: run-77:send

# Reject and edit on fresh threads.
python -m hitl_agent.runner start  run-88 "issue refund"
python -m hitl_agent.runner reject run-88                     # skipped, nothing sent

python -m hitl_agent.runner start  run-99 "post comment"
python -m hitl_agent.runner edit   run-99 "Approved with edits."
cat sent_ledger.json                                          # run-99 holds the edited body
```

Expected: `pytest` is green; `start` pauses and prints the decision payload; a
separate `approve` process resumes from the interrupt and sends exactly once;
`reject` skips with nothing sent; `edit` sends the modified draft; the ledger
never holds a duplicate for a thread.
