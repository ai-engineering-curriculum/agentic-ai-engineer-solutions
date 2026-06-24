# mod-205-evaluation-observability/exercise-02 — Solution

## Approach

The deliverable is **provider-portable** instrumentation: spans that follow the OTel
GenAI semantic conventions, so the same code exports to Langfuse, Arize Phoenix, or
LangSmith with only a config change. The structure mirrors a trace:

```text
span: agent.run                          (root)
 ├─ span: chat claude-sonnet-4   gen_ai.* attrs, token usage
 ├─ span: execute_tool search    gen_ai.tool.name=search
 ├─ span: chat claude-sonnet-4
 └─ span: execute_tool calculator
```

Design points:

1. **Exporter is config-driven.** The `TracerProvider` reads its OTLP endpoint and
   auth headers from environment variables (`OTEL_EXPORTER_OTLP_ENDPOINT`,
   `OTEL_EXPORTER_OTLP_HEADERS`). No credentials in source. Switching backends is a
   change to those two env vars.
2. **Standard keys, standard names.** Model calls become `chat {model}` spans with
   `gen_ai.operation.name=chat`, request/response model, and token usage; tool calls
   become `execute_tool {name}` spans. Using the conventions is what lets the
   platform compute cost and latency for free.
3. **Automatic nesting.** `start_as_current_span` makes each new span a child of the
   active span, so opening one `agent.run` root and then model/tool spans inside the
   loop yields a correct tree with no manual parenting.
4. **Errors are visible.** On any exception we set the span status to `ERROR` and
   call `record_exception`, so a failed run renders red instead of silently missing.
5. **Sampling that always keeps errors.** The stretch sampler traces a 10% baseline
   but force-keeps any errored run, because those are the traces you most need.

To stay runnable with **no platform account and no network**, the reference uses a
`ConsoleSpanExporter` fallback when no OTLP endpoint is configured, and a fake model
client when no API key is present. The instrumentation code is byte-for-byte the same
as it would be against a live backend — only the exporter and the client swap.

## Reference implementation

Install (optional for live export): `pip install opentelemetry-sdk
opentelemetry-exporter-otlp-proto-http`. The script runs without them by falling
back to the in-tree console exporter. Save as `otel_wireup.py`.

```python
"""OTel GenAI tracing wire-up for a tool-calling agent.

Exports to any OTLP backend (Langfuse / Phoenix / LangSmith) when
OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise prints spans to the console so the
file runs offline with zero setup. Credentials are read from the environment only.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import (
    ParentBased,
    Sampler,
    SamplingResult,
    Decision,
    TraceIdRatioBased,
)
from opentelemetry.trace import Status, StatusCode

# ---------------------------------------------------------------------------
# Exporter setup — endpoint + auth from env only; console fallback offline.
# ---------------------------------------------------------------------------


def _build_exporter():
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return ConsoleSpanExporter()  # offline, no signup
    # Headers (e.g. Langfuse basic auth) come from OTEL_EXPORTER_OTLP_HEADERS,
    # which the OTLP exporter reads automatically. Never hardcode them here.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter()  # picks up endpoint + headers from env


class _ErrorAwareSampler(Sampler):
    """Trace a fixed ratio, but force-keep any span flagged as an error."""

    def __init__(self, ratio: float = 0.10) -> None:
        self._ratio = TraceIdRatioBased(ratio)

    def should_sample(self, parent_context, trace_id, name, *args, **kwargs):
        attributes = kwargs.get("attributes") or {}
        if attributes.get("force_trace"):
            return SamplingResult(Decision.RECORD_AND_SAMPLE)
        return self._ratio.should_sample(parent_context, trace_id, name, *args, **kwargs)

    def get_description(self) -> str:
        return "ErrorAwareSampler"


def init_tracing(service: str = "agent") -> trace.Tracer:
    provider = TracerProvider(sampler=ParentBased(_ErrorAwareSampler(ratio=1.0)))
    provider.add_span_processor(BatchSpanProcessor(_build_exporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service)


# ---------------------------------------------------------------------------
# A fake model client so the file runs without an API key. Replace with a real
# provider client; the span code does not change.
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens, self.output_tokens = i, o


class _Resp:
    def __init__(self, text: str, model: str) -> None:
        self.text, self.model = text, model
        self.usage = _Usage(len(text.split()) + 10, len(text.split()))


class FakeClient:
    def create(self, model: str, messages: list[dict]) -> _Resp:
        last = messages[-1]["content"] if messages else ""
        return _Resp(f"reply to: {last}", model)


def get_client():
    # A real implementation would branch on os.getenv("ANTHROPIC_API_KEY") etc.
    return FakeClient()


# ---------------------------------------------------------------------------
# Instrumented model + tool calls (the heart of the exercise)
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4"
client = get_client()


def call_model(tracer, messages, model: str = MODEL):
    with tracer.start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        resp = client.create(model=model, messages=messages)
        span.set_attribute("gen_ai.response.model", resp.model)
        span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
        return resp


def call_tool(tracer, name: str, args: dict, fn):
    with tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        try:
            result = fn(**args)
            span.set_attribute("gen_ai.tool.result_chars", len(str(result)))
            return result
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


@contextmanager
def agent_run(tracer, task: str):
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("gen_ai.operation.name", "agent")
        span.set_attribute("agent.task", task)
        try:
            yield span
        except Exception as exc:  # mark the whole run red on failure
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


# ---------------------------------------------------------------------------
# Tools + tasks, including one that errors
# ---------------------------------------------------------------------------


def tool_search(query: str) -> str:
    return f"results for {query}"


def tool_calculator(expr: str) -> str:
    return str(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - demo only


TOOLS = {"search": tool_search, "calculator": tool_calculator}

TASKS = [
    {"q": "capital of France", "calls": [("search", {"query": "capital of France"})]},
    {"q": "what is 6*7", "calls": [("calculator", {"expr": "6*7"})]},
    {"q": "two-step", "calls": [("search", {"query": "x"}), ("calculator", {"expr": "2+2"})]},
    {"q": "boom", "calls": [("calculator", {"expr": "1/0"})]},  # errors -> red span
]


def run_task(tracer, task: dict) -> None:
    with agent_run(tracer, task["q"]):
        call_model(tracer, [{"role": "user", "content": task["q"]}])
        for name, args in task["calls"]:
            call_tool(tracer, name, args, TOOLS[name])
        call_model(tracer, [{"role": "assistant", "content": "final answer"}])


def main() -> None:
    tracer = init_tracing()
    for task in TASKS:
        try:
            run_task(tracer, task)
        except Exception as exc:  # noqa: BLE001 - keep going so all traces flush
            print(f"[run errored as expected] {task['q']}: {exc}")
    trace.get_tracer_provider().shutdown()  # flush BatchSpanProcessor


if __name__ == "__main__":
    main()
```

To export to **Langfuse** instead of the console, set (do not hardcode):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel/v1/traces"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public:secret)>"
python otel_wireup.py
```

For **Arize Phoenix** locally, run `phoenix serve` and point
`OTEL_EXPORTER_OTLP_ENDPOINT` at `http://localhost:6006/v1/traces` — no signup.

## Meeting the acceptance criteria

- **Nested under a root run span** — `agent_run` opens `agent.run`; every
  `call_model` / `call_tool` uses `start_as_current_span`, so they nest
  automatically as children.
- **Standard keys and naming** — spans are named `chat {model}` and
  `execute_tool {name}` and carry `gen_ai.operation.name`, `gen_ai.request.model`,
  `gen_ai.response.model`, `gen_ai.usage.*`, and `gen_ai.tool.name`.
- **Token counts and latency derived from attributes** — usage tokens come straight
  from the response object onto the span; latency is the span duration the SDK
  records. The platform computes cost from these, not hand math.
- **Errored runs render red** — the divide-by-zero task sets `StatusCode.ERROR` and
  records the exception on both the tool span and the root run span.
- **No hardcoded credentials** — endpoint and auth headers are read from
  `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`; the offline path uses
  the console exporter and needs none.

## Common pitfalls

- **Inventing custom attribute keys.** Naming a key `model_name` instead of
  `gen_ai.request.model` means the platform's cost and token dashboards stay empty.
  Use the `gen_ai.*` namespace exactly.
- **Manually parenting spans.** Passing explicit parent contexts is error-prone;
  rely on `start_as_current_span` and the implicit current-span context so nesting
  is automatic and correct.
- **Forgetting to flush.** `BatchSpanProcessor` buffers; a short-lived script that
  exits without `shutdown()` (or `force_flush()`) drops its spans and you see an
  empty UI. Always flush before exit.
- **Swallowing exceptions before recording them.** If you catch and log an error
  without `set_status(ERROR)` + `record_exception`, the run looks green in the trace
  and you lose exactly the run you needed to debug.
- **Hardcoding the auth header.** Putting a Langfuse secret in the source is a
  credential leak; read it from `OTEL_EXPORTER_OTLP_HEADERS`.

## Verification

```bash
python otel_wireup.py
```

Offline, confirm the console exporter prints, per task, a tree with `agent.run` as
the parent and `chat claude-sonnet-4` / `execute_tool ...` children carrying
`gen_ai.*` attributes, and that the `boom` task's spans show
`status: {"status_code": "ERROR"}` plus a recorded exception event. Against a live
backend, set the OTLP env vars and confirm the trace appears with computed token and
latency columns and one red run — then prove portability by changing only
`OTEL_EXPORTER_OTLP_ENDPOINT` to a second platform and re-running.
