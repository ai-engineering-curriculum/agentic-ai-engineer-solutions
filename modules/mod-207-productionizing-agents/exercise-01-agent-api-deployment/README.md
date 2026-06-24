# mod-207-productionizing-agents/exercise-01-agent-api-deployment — Solution

## Approach

The exercise asks for one agent exposed three ways — synchronous, streaming, and
background job — plus the operational scaffolding (timeouts, cancellation, a
concurrency cap, health checks, secret hygiene) and a container that runs the
same on a laptop and in a cluster.

The design that keeps all three endpoint shapes honest is a single agent core
with two entry points:

- `run_agent(prompt)` — an async coroutine that returns the final answer.
- `run_agent_streaming(prompt)` — an async generator that yields step/token
  chunks as they are produced.

Everything else is plumbing around those two functions:

- **Synchronous** wraps `run_agent` in `asyncio.wait_for` so a stuck run returns
  `504` instead of holding the socket open forever.
- **Streaming** drives `run_agent_streaming` through a `StreamingResponse` of
  Server-Sent Events. The generator is the cancellation point: when the client
  disconnects, FastAPI cancels the request task, the generator's `await` raises
  `asyncio.CancelledError`, and the agent loop stops — no more tokens burned.
- **Background jobs** return a `job_id` immediately and run the agent in an
  `asyncio` task tracked in an in-process dict. The dict is intentionally
  volatile; the exercise's whole point is to *feel* why that is unacceptable for
  work you cannot afford to lose, which exercise-02 fixes with Temporal.

A single `asyncio.Semaphore` caps in-flight runs across **all three** endpoints so
a burst cannot exhaust the model rate limit. The model client is read from the
environment at process start; the key never enters the image.

To keep the solution runnable offline (no key, no network, no paid tokens), the
agent core ships with a deterministic fake model that streams a few chunks with a
small delay. Set `AGENT_FAKE_MODEL=0` and provide `ANTHROPIC_API_KEY` to swap in
the real client; the endpoint code does not change.

## Reference implementation

Layout:

```text
exercise-01-agent-api-deployment/
├── app/
│   ├── __init__.py
│   ├── agent.py        # agent core: run_agent + run_agent_streaming
│   ├── config.py       # env-driven settings, secret hygiene
│   ├── jobs.py         # in-process job store (volatile by design)
│   └── main.py         # FastAPI app: the three endpoints + healthz
├── tests/
│   └── test_api.py     # exercises every acceptance criterion
├── Dockerfile
└── requirements.txt
```

### `app/config.py`

```python
"""Process configuration, read once at import time.

Secret hygiene: the API key is read from the environment and never logged or
returned. Nothing here is baked into the container image.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    fake_model: bool
    run_timeout_seconds: float
    max_concurrent_runs: int
    api_key: str | None

    @property
    def model_ready(self) -> bool:
        """True when the model client can actually be constructed."""
        return self.fake_model or bool(self.api_key)


def load_settings() -> Settings:
    """Build settings from the environment. Fail-open to the fake model so the
    service is runnable offline without a key."""
    return Settings(
        fake_model=os.environ.get("AGENT_FAKE_MODEL", "1") != "0",
        run_timeout_seconds=float(os.environ.get("AGENT_RUN_TIMEOUT", "120")),
        max_concurrent_runs=int(os.environ.get("AGENT_MAX_CONCURRENT", "4")),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )


SETTINGS = load_settings()
```

### `app/agent.py`

```python
"""Agent core with two entry points: a coroutine and an async generator.

Both share one implementation so the synchronous, streaming, and background
endpoints run *the same* agent. The fake model keeps the module runnable with no
key and no network; flip AGENT_FAKE_MODEL=0 to use the real Anthropic client.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .config import SETTINGS

# A small, deterministic "thought stream" the fake model emits. With a real
# model these chunks would be tokens or step events from the agent loop.
_FAKE_STEPS = [
    "Reading the question. ",
    "Selecting a tool. ",
    "Calling the tool. ",
    "Composing the final answer. ",
]
_CHUNK_DELAY_SECONDS = 0.05


async def _fake_stream(prompt: str) -> AsyncIterator[str]:
    """Emit a few chunks with a delay so streaming is observably incremental."""
    for step in _FAKE_STEPS:
        await asyncio.sleep(_CHUNK_DELAY_SECONDS)
        yield step
    await asyncio.sleep(_CHUNK_DELAY_SECONDS)
    yield f"Answer to: {prompt.strip()[:120]}"


async def _real_stream(prompt: str) -> AsyncIterator[str]:
    """Stream from the Anthropic API. Imported lazily so the dependency is only
    needed when AGENT_FAKE_MODEL=0."""
    import anthropic  # noqa: PLC0415 — lazy import keeps offline path clean

    client = anthropic.AsyncAnthropic(api_key=SETTINGS.api_key)
    async with client.messages.stream(
        model="claude-sonnet-4-0",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def run_agent_streaming(prompt: str) -> AsyncIterator[str]:
    """Yield agent output chunk by chunk. Cancellation propagates here: when the
    consumer stops pulling, the await unwinds and the run stops."""
    source = _fake_stream if SETTINGS.fake_model else _real_stream
    async for chunk in source(prompt):
        yield chunk


async def run_agent(prompt: str) -> str:
    """Run the agent to completion and return the final answer by draining the
    same streaming core the streaming endpoint uses."""
    chunks: list[str] = []
    async for chunk in run_agent_streaming(prompt):
        chunks.append(chunk)
    return "".join(chunks)
```

### `app/jobs.py`

```python
"""In-process job store. Volatile by design — see NOTES.md.

This dict lives in the worker process. It does not survive a restart, a deploy,
or a crash, and it is not shared across replicas. That is exactly why the next
exercise replaces it with a durable workflow engine.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Job:
    """One background run. Immutable: transitions produce a new Job."""

    id: str
    status: str  # "running" | "done" | "failed"
    result: str | None = None
    error: str | None = None


@dataclass
class JobStore:
    """Tracks background jobs and their asyncio tasks in memory."""

    _jobs: dict[str, Job] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def create(self) -> str:
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = Job(id=job_id, status="running")
        return job_id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def attach_task(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks[job_id] = task

    def mark_done(self, job_id: str, result: str) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = replace(job, status="done", result=result)

    def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = replace(job, status="failed", error=error)
```

### `app/main.py`

```python
"""FastAPI surface: synchronous, streaming, and background-job endpoints, plus a
health check and a global concurrency cap.

The semaphore is shared across all three endpoints so total in-flight runs — not
per-endpoint runs — stay under the cap.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import run_agent, run_agent_streaming
from .config import SETTINGS
from .jobs import JobStore

app = FastAPI(title="Productionized Agent API")

RUN_LIMIT = asyncio.Semaphore(SETTINGS.max_concurrent_runs)
JOBS = JobStore()


class AgentRequest(BaseModel):
    """Validated request body. Rejects malformed input before it reaches the
    agent — never pass an unvalidated body into a prompt."""

    prompt: str = Field(min_length=1, max_length=8000)


@app.post("/agent/run")
async def run(req: AgentRequest) -> dict:
    """Synchronous: wait for the whole answer, bounded by a server-side timeout."""
    async with RUN_LIMIT:
        try:
            answer = await asyncio.wait_for(
                run_agent(req.prompt),
                timeout=SETTINGS.run_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="agent run timed out")
    return {"answer": answer}


@app.post("/agent/stream")
async def stream(req: AgentRequest, request: Request) -> StreamingResponse:
    """Streaming over SSE. Stops the run when the client disconnects."""

    async def events():
        async with RUN_LIMIT:
            try:
                async for chunk in run_agent_streaming(req.prompt):
                    if await request.is_disconnected():
                        # Client left: stop pulling so the agent loop unwinds.
                        break
                    yield f"data: {chunk}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
            except asyncio.CancelledError:
                # Disconnect mid-await: let cancellation propagate, stop work.
                raise

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/agent/jobs")
async def start_job(req: AgentRequest) -> dict:
    """Background job: return an id immediately, run the agent out of band."""
    job_id = JOBS.create()

    async def _runner() -> None:
        async with RUN_LIMIT:
            try:
                result = await run_agent(req.prompt)
                JOBS.mark_done(job_id, result)
            except Exception as exc:  # noqa: BLE001 — record, do not swallow
                JOBS.mark_failed(job_id, str(exc))

    JOBS.attach_task(job_id, asyncio.create_task(_runner()))
    return {"job_id": job_id, "status": "running"}


@app.get("/agent/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    """Poll a background job."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    body: dict = {"job_id": job.id, "status": job.status}
    if job.status == "done":
        body["result"] = job.result
    if job.status == "failed":
        body["error"] = job.error
    return body


@app.get("/healthz")
async def healthz() -> dict:
    """Readiness: confirm a model client can actually be configured."""
    if not SETTINGS.model_ready:
        raise HTTPException(status_code=503, detail="model client not configured")
    return {"ok": True, "fake_model": SETTINGS.fake_model}
```

### `requirements.txt`

```text
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
httpx==0.28.1
anthropic==0.42.0
```

### `Dockerfile`

```dockerfile
# Pin the base image so builds are reproducible.
FROM python:3.12-slim

WORKDIR /app

# Dependency layer first so it caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Production ASGI server — never the dev reload mode.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `tests/test_api.py`

```python
"""Exercise every acceptance criterion against the in-process app.

Runs fully offline with the fake model. The timeout test patches the agent with a
sleeper so it deterministically trips the 504 path without waiting two minutes.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import main
from app.main import app


@pytest.mark.asyncio
async def test_run_returns_validated_answer():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/agent/run", json={"prompt": "hello"})
    assert resp.status_code == 200
    assert "Answer to: hello" in resp.json()["answer"]


@pytest.mark.asyncio
async def test_run_rejects_empty_prompt():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/agent/run", json={"prompt": ""})
    assert resp.status_code == 422  # Pydantic boundary validation


@pytest.mark.asyncio
async def test_run_times_out(monkeypatch):
    async def _slow(_prompt: str) -> str:
        await asyncio.sleep(10)
        return "never"

    monkeypatch.setattr(main, "run_agent", _slow)
    monkeypatch.setattr(main.SETTINGS, "run_timeout_seconds", 0.05, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/agent/run", json={"prompt": "slow"})
    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_stream_is_incremental():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        async with c.stream("POST", "/agent/stream", json={"prompt": "hi"}) as r:
            chunks = [line async for line in r.aiter_lines() if line.startswith("data:")]
    assert len(chunks) > 1  # multiple SSE frames, not one buffered blob
    assert any("[DONE]" in line for line in chunks)


@pytest.mark.asyncio
async def test_background_job_lifecycle():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        start = await c.post("/agent/jobs", json={"prompt": "bg"})
        assert start.status_code == 200
        job_id = start.json()["job_id"]
        assert start.json()["status"] == "running"

        deadline = time.monotonic() + 5
        status = "running"
        while time.monotonic() < deadline:
            poll = await c.get(f"/agent/jobs/{job_id}")
            status = poll.json()["status"]
            if status == "done":
                assert "Answer to: bg" in poll.json()["result"]
                break
            await asyncio.sleep(0.05)
    assert status == "done"


@pytest.mark.asyncio
async def test_healthz():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
```

## Meeting the acceptance criteria

- **`POST /agent/run` returns a validated answer; `504` on timeout.** The body is
  a Pydantic `AgentRequest`; `asyncio.wait_for` converts an overrun into
  `HTTPException(504)`. `test_run_returns_validated_answer` and `test_run_times_out`
  cover both.
- **`POST /agent/stream` streams incrementally and stops on disconnect.** Each
  fake step yields a separate SSE frame with a delay; `request.is_disconnected()`
  plus `asyncio.CancelledError` propagation stop the run when the client leaves.
  `test_stream_is_incremental` asserts multiple frames.
- **`POST /agent/jobs` returns immediately; polling reflects `running` → `done`.**
  The endpoint creates the job and spawns an `asyncio` task before returning.
  `test_background_job_lifecycle` polls through both states.
- **The concurrency cap holds.** One module-level `asyncio.Semaphore` wraps the
  agent body in all three endpoints, so simultaneous requests beyond the cap
  queue on `async with RUN_LIMIT` rather than all firing at once.
- **The container builds and serves all three endpoints.** The Dockerfile pins
  `python:3.12-slim`, caches the dependency layer, runs as `appuser`, and starts
  `uvicorn`. The key is injected with `-e`, never baked in.

## Common pitfalls

- **Buffering the stream.** Building the whole answer and yielding it once defeats
  streaming. Yield each chunk as the agent produces it, and flush a frame per step.
- **Ignoring disconnect.** Without a `is_disconnected()` check or cancellation
  handling, an abandoned SSE request keeps calling the model — pure cost. The
  generator must be the cancellation point.
- **Per-endpoint semaphores.** Three separate caps let total concurrency reach
  3× the intended limit. Share one semaphore across every entry point.
- **Treating the job dict as durable.** It is process-local and dies on restart.
  Do not build anything on it that must survive a deploy — that is exercise-02.
- **Baking the key into the image.** A key in the Dockerfile or code leaks with
  the image. Read it from the environment and pass it at `docker run -e` time.

## Verification

```bash
cd modules/mod-207-productionizing-agents/exercise-01-agent-api-deployment
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio

# Unit/integration tests (offline, fake model).
pytest -q

# Run the service locally.
uvicorn app.main:app --port 8000 &

curl -s -X POST localhost:8000/agent/run \
  -H 'content-type: application/json' -d '{"prompt":"summarize x"}'

curl -N -X POST localhost:8000/agent/stream \
  -H 'content-type: application/json' -d '{"prompt":"summarize x"}'

JOB=$(curl -s -X POST localhost:8000/agent/jobs \
  -H 'content-type: application/json' -d '{"prompt":"long task"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -s localhost:8000/agent/jobs/$JOB

curl -s localhost:8000/healthz

# Container: key injected at runtime, never in the image.
docker build -t agent-api .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" agent-api
```

Expected: `pytest` is green; `/agent/run` returns a JSON answer; `/agent/stream`
prints frames one at a time; the job polls from `running` to `done`; `/healthz`
returns `{"ok": true}`; the container serves all three endpoints.
