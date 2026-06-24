# mod-207-productionizing-agents/exercise-02-durable-execution-temporal — Solution

## Approach

The exercise converts a multi-step agent run into a Temporal workflow so that
killing the worker mid-run resumes instead of restarting, completed model calls
are never re-paid, and a side-effecting tool fires exactly once.

The discipline that makes this work is a hard split:

- **Workflow** (`DurableAgent`) owns *orchestration only* — the order of steps and
  the branching. It must be deterministic: on recovery Temporal re-runs it from
  the top, feeding back the recorded results of completed activities, so any
  non-determinism (`random`, `datetime.now()`, direct I/O) would diverge from
  history and corrupt the replay.
- **Activities** own every side effect — each model call, the recorded result.
  Temporal runs each activity once, persists its result, and on replay returns
  the recorded value instead of re-executing.

Two correctness properties get explicit treatment:

1. **Replay survives a crash.** Each activity logs on entry. Because the model
   call lives *inside* an activity, a completed plan/research step is never
   re-executed on recovery — its log line does not reappear, and no tokens are
   re-spent.
2. **The side effect fires exactly once.** `record_result` is guarded by an
   idempotency key derived from `workflow_id + step`. Replay protects *completed*
   activities, but an activity interrupted mid-flight is *retried* — so the guard
   must dedupe at the activity level. A `flaky_then_ok` activity raises once then
   succeeds under a retry policy to prove the run recovers without restarting.

To stay runnable offline, the model call is a deterministic stub (a real
`run_model` would call the provider). The recorder writes to a local JSON file so
"exactly once" is observable on disk. The whole thing runs against Temporal's
**time-skipping test environment** in the test suite — no external server needed —
and against a local dev server for the manual crash demo.

## Reference implementation

Layout:

```text
exercise-02-durable-execution-temporal/
├── durable_agent/
│   ├── __init__.py
│   ├── activities.py   # idempotent, side-effecting steps
│   ├── workflow.py     # deterministic orchestration
│   └── worker.py       # hosts workflow + activities, starts a run
├── tests/
│   └── test_durable.py # replay + idempotency + retry, in-memory env
└── requirements.txt
```

### `durable_agent/activities.py`

```python
"""Activities: every side effect lives here. Each runs once and its result is
persisted; on replay Temporal returns the recorded result.

The recorder is guarded by an idempotency key so a *retried* (not replayed)
activity still writes exactly once.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from temporalio import activity

log = logging.getLogger("durable_agent.activities")

RECORD_PATH = Path("agent_records.json")

# Module-level latch so the test can force exactly one failure on the flaky
# activity. A real flaky tool would fail on a transient network error instead.
_FLAKE_FIRED = {"value": False}


async def run_model(prompt: str) -> str:
    """Deterministic stand-in for a provider call. Safe to replay: pure output,
    no side effect. Swap for a real async client when a key is available."""
    await asyncio.sleep(0)  # yield control like a real I/O call
    return f"model({prompt[:60]})"


@activity.defn
async def plan_step(question: str) -> str:
    """Pure model call — naturally safe to replay."""
    log.info("ACTIVITY plan_step start question=%s", question)
    return await run_model(f"Plan steps for: {question}")


@activity.defn
async def research_step(plan: str) -> str:
    """Pure model call — naturally safe to replay."""
    log.info("ACTIVITY research_step start")
    return await run_model(f"Execute plan: {plan}")


def _read_records() -> dict[str, str]:
    if RECORD_PATH.exists():
        return json.loads(RECORD_PATH.read_text())
    return {}


@activity.defn
async def record_result(key: str, payload: str) -> str:
    """External side effect, made idempotent by `key`.

    `key` is workflow_id + step, so a retry of an interrupted attempt sees the
    record already present and does not write twice.
    """
    log.info("ACTIVITY record_result start key=%s", key)
    records = _read_records()
    if key in records:
        log.info("record_result idempotent skip key=%s", key)
        return "already-recorded"
    records[key] = payload
    RECORD_PATH.write_text(json.dumps(records, indent=2))
    return "recorded"


@activity.defn
async def flaky_then_ok(token: str) -> str:
    """Fails on its first attempt, succeeds on retry — proves the retry policy
    recovers a transient error without restarting the workflow."""
    log.info("ACTIVITY flaky_then_ok attempt token=%s", token)
    if not _FLAKE_FIRED["value"]:
        _FLAKE_FIRED["value"] = True
        raise RuntimeError("transient failure (will succeed on retry)")
    return f"ok:{token}"
```

### `durable_agent/workflow.py`

```python
"""The workflow: orchestration only. Deterministic — no I/O, no random, no
wall-clock reads. Temporal replays this from the top on recovery, so every line
here must produce the same decisions given the same history.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import (
        flaky_then_ok,
        plan_step,
        record_result,
        research_step,
    )


@workflow.defn
class DurableAgent:
    @workflow.run
    async def run(self, question: str) -> str:
        # workflow.logger is replay-aware; a plain log line would re-print on
        # every replay. Use it for orchestration-level logging.
        workflow.logger.info("WORKFLOW DurableAgent start")

        plan = await workflow.execute_activity(
            plan_step,
            question,
            start_to_close_timeout=timedelta(minutes=2),
        )
        findings = await workflow.execute_activity(
            research_step,
            plan,
            start_to_close_timeout=timedelta(minutes=5),
        )

        # Flaky activity with a retry policy: backoff, capped attempts.
        await workflow.execute_activity(
            flaky_then_ok,
            "tool-call",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=50),
                backoff_coefficient=2.0,
                maximum_attempts=5,
            ),
        )

        # Idempotency key from workflow id + step. Deterministic: the same id is
        # produced on replay, so the recorder dedupes correctly.
        key = f"{workflow.info().workflow_id}:record"
        await workflow.execute_activity(
            record_result,
            args=[key, findings],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return findings
```

### `durable_agent/worker.py`

```python
"""Run a worker that hosts the workflow + activities, then trigger one run.

Requires a local Temporal dev server:  `temporal server start-dev`
"""
from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import (
    flaky_then_ok,
    plan_step,
    record_result,
    research_step,
)
from .workflow import DurableAgent

TASK_QUEUE = "durable-agent-tq"

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    client = await Client.connect("localhost:7233")
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DurableAgent],
        activities=[plan_step, research_step, flaky_then_ok, record_result],
    ):
        result = await client.execute_workflow(
            DurableAgent.run,
            "How do durable workflows resume?",
            id="durable-agent-run-1",
            task_queue=TASK_QUEUE,
        )
        print("workflow result:", result)


if __name__ == "__main__":
    asyncio.run(main())
```

### `requirements.txt`

```text
temporalio==1.9.0
```

### `tests/test_durable.py`

```python
"""Durability properties without an external server, using Temporal's
time-skipping test environment.

Covered: end-to-end completion, idempotent side effect under a double-run, and a
flaky activity recovering via its retry policy. The crash/replay demonstration is
manual (see Verification) because it requires killing a live worker.
"""
from __future__ import annotations

import json
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_agent import activities
from durable_agent.activities import (
    RECORD_PATH,
    flaky_then_ok,
    plan_step,
    record_result,
    research_step,
)
from durable_agent.workflow import DurableAgent

TASK_QUEUE = "durable-agent-test-tq"


@pytest.fixture(autouse=True)
def clean_records():
    if RECORD_PATH.exists():
        RECORD_PATH.unlink()
    activities._FLAKE_FIRED["value"] = False
    yield
    if RECORD_PATH.exists():
        RECORD_PATH.unlink()


async def _run_once(env: WorkflowEnvironment, wf_id: str) -> str:
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DurableAgent],
        activities=[plan_step, research_step, flaky_then_ok, record_result],
    ):
        return await env.client.execute_workflow(
            DurableAgent.run,
            "test question",
            id=wf_id,
            task_queue=TASK_QUEUE,
        )


@pytest.mark.asyncio
async def test_workflow_completes():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_once(env, f"wf-{uuid.uuid4().hex}")
    assert result.startswith("model(")


@pytest.mark.asyncio
async def test_flaky_activity_recovers_via_retry():
    # flaky_then_ok raises once; the retry policy must carry the run to success.
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_once(env, f"wf-{uuid.uuid4().hex}")
    assert result.startswith("model(")
    assert activities._FLAKE_FIRED["value"] is True  # it did fail once


@pytest.mark.asyncio
async def test_side_effect_is_idempotent_across_two_runs():
    wf_id = f"wf-{uuid.uuid4().hex}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run_once(env, wf_id)
        # Reset only the flake latch; the record file persists. A retry of the
        # gated activity reuses the same workflow-derived idempotency key.
        activities._FLAKE_FIRED["value"] = False
        await _run_once(env, wf_id)

    records = json.loads(RECORD_PATH.read_text())
    keys_for_run = [k for k in records if k.startswith(wf_id)]
    assert len(keys_for_run) == 1  # the gated effect recorded exactly once
```

## Meeting the acceptance criteria

- **Deterministic workflow, idempotent activities, no I/O in the workflow.**
  `DurableAgent.run` only sequences `execute_activity` calls and reads
  `workflow.info()`; every model call and file write lives in an `@activity.defn`.
- **Restarting the worker mid-run resumes; completed model calls do not re-run.**
  Each activity logs on entry. After a crash-and-restart, the completed
  `plan_step` / `research_step` log lines do not reappear because Temporal returns
  their recorded results instead of executing them. Shown by the Verification
  crash demo.
- **The side-effecting activity, forced to retry, fires exactly once.**
  `record_result` dedupes on the `workflow_id + step` key.
  `test_side_effect_is_idempotent_across_two_runs` asserts a single record.
- **A flaky activity recovers via its retry policy without restarting the run.**
  `flaky_then_ok` raises once, then the `RetryPolicy` carries it to success;
  `test_flaky_activity_recovers_via_retry` confirms the failure happened and the
  run still completed.

## Common pitfalls

- **Non-determinism in the workflow.** An `import random`, a `datetime.now()`, or
  a direct HTTP call inside `DurableAgent.run` diverges from recorded history on
  replay and raises a non-determinism error. Push all of it into activities.
- **Confusing replay with retry.** Replay returns recorded results for *completed*
  activities; an activity interrupted mid-flight is *retried* and can run twice.
  Idempotency keys defend the retry case — replay alone does not.
- **Idempotency key derived from a non-deterministic value.** Keying on
  `uuid4()` or a timestamp generated inside the workflow breaks dedupe on replay.
  Derive it from stable, replay-safe inputs like `workflow_id`.
- **Importing activity modules unguarded in the workflow file.** Workflow imports
  must go through `workflow.unsafe.imports_passed_through()` so the sandbox does
  not flag the activity module's I/O at import time.
- **Logging with the stdlib logger inside the workflow.** A plain `logging` call
  re-prints on every replay. Use `workflow.logger`, which is replay-aware.

## Verification

```bash
cd modules/mod-207-productionizing-agents/exercise-02-durable-execution-temporal
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio

# Durability properties, no external server (time-skipping test env).
pytest -q

# --- Manual crash/replay demo ---
# Terminal A: a local Temporal dev server.
temporal server start-dev

# Terminal B: start the worker and trigger a run.
python -m durable_agent.worker
# Watch the logs: "ACTIVITY plan_step start" then "ACTIVITY research_step start".
# Ctrl-C the worker AFTER plan_step completes but before the run finishes,
# then restart it:
python -m durable_agent.worker
# On restart, plan_step's log line does NOT reappear (its result was replayed);
# the run finishes from where it stopped and prints the result.

cat agent_records.json   # the gated effect recorded exactly once
```

Expected: `pytest` is green; on the manual demo the completed activity is not
re-executed after restart, the run resumes to completion, the flaky activity
recovers via retry, and `agent_records.json` holds a single record for the run.
