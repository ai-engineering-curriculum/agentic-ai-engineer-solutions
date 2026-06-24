"""Orchestrator-worker reference (mod-204 exercise-01).

Decompose a research question into bounded, structured assignments, fan out to
isolated worker agents concurrently, and synthesize their distilled results --
tolerating partial failure and logging per-run instrumentation.

Run:  python orchestrator_worker.py
With a real model:  export ANTHROPIC_API_KEY=sk-ant-...  (otherwise an offline
deterministic stub is used so the patterns run without spend.)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

MODEL_ORCHESTRATOR = "claude-sonnet-4-0"
MODEL_WORKER = "claude-3-5-haiku-latest"
MAX_ASSIGNMENTS = 5

# A worker role whose instruction begins with this token is forced to fail, so
# the partial-failure path is exercised deterministically in the demo.
FAIL_SENTINEL = "[FAIL]"


# --------------------------------------------------------------------------- #
# Structured contracts                                                        #
# --------------------------------------------------------------------------- #
class Assignment(BaseModel):
    worker_role: str
    instruction: str


class Decomposition(BaseModel):
    assignments: list[Assignment] = Field(max_length=MAX_ASSIGNMENTS)


class WorkerResult(BaseModel):
    answer: str
    sources: list[str]
    confidence: Literal["high", "medium", "low"]


@dataclass
class RunStats:
    """Per-run instrumentation, reused by mod-205 observability work."""

    assignment_count: int = 0
    worker_tokens: dict[str, int] = field(default_factory=dict)
    failed_roles: list[str] = field(default_factory=list)
    wall_clock_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "assignments": self.assignment_count,
            "worker_tokens": self.worker_tokens,
            "failed": self.failed_roles,
            "wall_clock_s": round(self.wall_clock_s, 3),
        }


# --------------------------------------------------------------------------- #
# Model access: real Anthropic call, with a deterministic offline fallback    #
# --------------------------------------------------------------------------- #
def _structured_offline(model: str, system: str, user: str, schema: type[BaseModel]):
    """Deterministic stand-in so the orchestration logic runs without an API key."""
    if schema is Decomposition:
        # Three obviously-distinct, self-contained sub-tasks; respects the cap.
        return Decomposition(
            assignments=[
                Assignment(worker_role="aws", instruction="Summarize AWS EKS on cost, autoscaling, GPU support."),
                Assignment(worker_role="gcp", instruction="Summarize GCP GKE on cost, autoscaling, GPU support."),
                Assignment(worker_role="azure", instruction=f"{FAIL_SENTINEL} Summarize Azure AKS on cost, autoscaling, GPU support."),
            ]
        )
    if schema is WorkerResult:
        provider = user.split()[1] if len(user.split()) > 1 else "provider"
        return WorkerResult(
            answer=f"Offline finding for: {user[:60]}",
            sources=[f"https://docs.example/{provider.lower().strip('.')}"],
            confidence="medium",
        )
    raise ValueError(f"no offline stub for {schema!r}")


def call_structured(
    model: str, system: str, user: str, schema: type[BaseModel]
) -> tuple[BaseModel, int]:
    """Return (validated_model, input+output_tokens).

    Asks the model for JSON and validates it against ``schema``. Falls back to a
    deterministic stub when ``ANTHROPIC_API_KEY`` is absent.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        obj = _structured_offline(model, system, user, schema)
        # Approximate token usage so instrumentation is non-trivial offline.
        return obj, len(system) // 4 + len(user) // 4 + 64

    import anthropic  # imported lazily so the offline path needs no install

    client = anthropic.Anthropic()
    schema_json = json.dumps(schema.model_json_schema())
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        system=f"{system}\n\nReturn ONLY JSON matching this schema:\n{schema_json}",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    obj = schema.model_validate_json(_extract_json(text))
    tokens = msg.usage.input_tokens + msg.usage.output_tokens
    return obj, tokens


def _extract_json(text: str) -> str:
    """Tolerate models that wrap JSON in prose or fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return text[start : end + 1]


# --------------------------------------------------------------------------- #
# The three phases                                                            #
# --------------------------------------------------------------------------- #
ORCHESTRATOR_DECOMPOSE = (
    "You are a research orchestrator. Split the question into at most "
    f"{MAX_ASSIGNMENTS} INDEPENDENT, NON-OVERLAPPING sub-tasks. Each instruction "
    "must be self-contained: a worker sees only its own instruction, never the "
    "original question or sibling work."
)

WORKER_SYSTEM = (
    "You are a focused research worker. Answer ONLY your instruction. Return the "
    "finding plus the sources that back it -- never your search process or raw "
    "tool output."
)

ORCHESTRATOR_SYNTHESIZE = (
    "You are a research orchestrator synthesizing worker results. Combine the "
    "findings, surface any disagreements between workers, and note gaps from "
    "failed workers. Do NOT introduce facts absent from the results."
)


async def decompose(question: str, max_n: int = MAX_ASSIGNMENTS) -> tuple[list[Assignment], int]:
    obj, tokens = await asyncio.to_thread(
        call_structured, MODEL_ORCHESTRATOR, ORCHESTRATOR_DECOMPOSE, question, Decomposition
    )
    # Enforce the cap in code even if the model over-produces.
    return obj.assignments[:max_n], tokens


async def run_worker(a: Assignment) -> WorkerResult:
    """Isolated agent loop seeded with ONLY this assignment's instruction."""
    if a.instruction.startswith(FAIL_SENTINEL):
        raise RuntimeError(f"worker '{a.worker_role}' failed (injected)")
    obj, tokens = await asyncio.to_thread(
        call_structured, MODEL_WORKER, WORKER_SYSTEM, a.instruction, WorkerResult
    )
    # Smuggle token usage out via a private attribute for the caller to log.
    obj.__dict__["_tokens"] = tokens
    obj.__dict__["_role"] = a.worker_role
    return obj


async def synthesize(question: str, results: list[WorkerResult], failed: list[Assignment]) -> str:
    failed_note = (
        "\nMissing (workers that failed): " + ", ".join(a.worker_role for a in failed)
        if failed
        else ""
    )
    findings = "\n".join(f"- [{r.confidence}] {r.answer} (sources: {r.sources})" for r in results)
    user = f"Question: {question}\n\nWorker findings:\n{findings}{failed_note}"
    obj, _ = await asyncio.to_thread(
        call_structured, MODEL_ORCHESTRATOR, ORCHESTRATOR_SYNTHESIZE, user, WorkerResult
    )
    summary = obj.answer
    if failed:
        summary += "\n\n[gap] Unable to cover: " + ", ".join(a.worker_role for a in failed)
    return summary


async def main(question: str) -> tuple[str, RunStats]:
    stats = RunStats()
    t0 = time.perf_counter()

    assignments, _ = await decompose(question)
    stats.assignment_count = len(assignments)

    # Fan out concurrently; return_exceptions keeps one failure from sinking all.
    settled = await asyncio.gather(
        *(run_worker(a) for a in assignments), return_exceptions=True
    )

    results: list[WorkerResult] = []
    failed: list[Assignment] = []
    for a, r in zip(assignments, settled):
        if isinstance(r, WorkerResult):
            results.append(r)
            stats.worker_tokens[a.worker_role] = r.__dict__.get("_tokens", 0)
        else:
            failed.append(a)
            stats.failed_roles.append(a.worker_role)

    final = await synthesize(question, results, failed)
    stats.wall_clock_s = time.perf_counter() - t0
    return final, stats


if __name__ == "__main__":
    q = (
        "Compare the managed Kubernetes offerings of AWS, GCP, and Azure on "
        "cost, autoscaling, and GPU support."
    )
    answer, run_stats = asyncio.run(main(q))
    print("=== FINAL ANSWER ===")
    print(answer)
    print("\n=== RUN STATS ===")
    print(json.dumps(run_stats.as_dict(), indent=2))
