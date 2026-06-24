"""Sub-agent isolation reference (mod-204 exercise-03).

Prove the context economics: a researcher sub-agent makes many tool calls over a
corpus in its OWN context window and returns a tiny distilled result, while a
non-isolated variant accumulates every tool result in the orchestrator's
context. The script prints the three-number asymmetry table.

Run:  python subagent_isolation.py
With a real model:  export ANTHROPIC_API_KEY=sk-ant-...  (otherwise a
deterministic stub drives the loop so the token accounting still holds.)
"""

from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel

MODEL = "claude-3-5-haiku-latest"
MIN_TOOL_CALLS = 10


# --------------------------------------------------------------------------- #
# A stubbed corpus the sub-agent digs through (>= 20 documents)               #
# --------------------------------------------------------------------------- #
CORPUS: dict[str, str] = {
    f"doc-{i:02d}": (
        f"Document {i}. " + ("Lorem ipsum dolor sit amet. " * 40)
        + (f"KEY FINDING {i}: latency p99 was {90 + i} ms under load."
           if i % 4 == 0 else "No incident recorded in this window.")
    )
    for i in range(24)
}


def search(query: str) -> list[str]:
    """Return doc ids whose body mentions the query (case-insensitive)."""
    q = query.lower()
    return [doc_id for doc_id, body in CORPUS.items() if q in body.lower()]


def read(doc_id: str) -> str:
    return CORPUS.get(doc_id, "")


def _tok(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Distilled return contract                                                   #
# --------------------------------------------------------------------------- #
class WorkerResult(BaseModel):
    answer: str
    sources: list[str]          # references (ids), NOT contents
    confidence: Literal["high", "medium", "low"]
    open_questions: list[str]


RESEARCHER_SYSTEM = (
    "You are a researcher. Investigate the corpus with search/read tools, then "
    "report the finding and the source IDs that support it. Do NOT include your "
    "search process or raw document text in the answer."
)


# --------------------------------------------------------------------------- #
# The heavy sub-agent, run in ISOLATION                                       #
# --------------------------------------------------------------------------- #
def run_isolated(assignment: str) -> tuple[WorkerResult, int, int]:
    """Return (distilled_result, internal_tokens, returned_tokens).

    The message list is seeded with ONLY the assignment -- never any parent
    history. All tool traffic stays inside this function's local context.
    """
    messages = [{"role": "user", "content": assignment}]  # fresh -- no parent context
    internal_tokens = _tok(RESEARCHER_SYSTEM) + _tok(assignment)

    # Drive a real investigation loop: a broad search, then read every candidate
    # document looking for the finding. "lorem" appears in all 24 docs, so the
    # researcher reads widely -- well past the >= 10 tool-call floor.
    hits = search("lorem")
    internal_tokens += _tok("search:lorem") + _tok(json.dumps(hits))
    found: list[str] = []
    for doc_id in hits:
        body = read(doc_id)                     # each read lands in THIS context
        internal_tokens += _tok(body)           # ... and is counted internally
        if "KEY FINDING" in body:
            found.append(doc_id)
    tool_calls = 1 + len(hits)
    assert tool_calls >= MIN_TOOL_CALLS, f"only {tool_calls} tool calls"

    result = _distill(assignment, found, internal_tokens)
    returned_tokens = _tok(result.model_dump_json())
    return result, internal_tokens, returned_tokens


def _distill(assignment: str, found: list[str], internal_tokens: int) -> WorkerResult:
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        client = anthropic.Anthropic()
        schema = json.dumps(WorkerResult.model_json_schema())
        evidence = "; ".join(f"{d}: {read(d)[-60:]}" for d in found)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=f"{RESEARCHER_SYSTEM}\n\nReturn ONLY JSON matching:\n{schema}",
            messages=[{"role": "user", "content": f"{assignment}\nEvidence: {evidence}"}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        start, end = text.find("{"), text.rfind("}")
        return WorkerResult.model_validate_json(text[start : end + 1])

    # Offline: synthesize a faithful distilled result from the found doc ids.
    return WorkerResult(
        answer=f"{len(found)} documents recorded a KEY FINDING (latency p99 spikes).",
        sources=found,
        confidence="high" if found else "low",
        open_questions=[] if found else ["No findings located; widen the query."],
    )


# --------------------------------------------------------------------------- #
# The NON-isolated contrast: tool results pile into the orchestrator context  #
# --------------------------------------------------------------------------- #
def run_inline(assignment: str, orchestrator_messages: list[dict]) -> int:
    """Run the same investigation inline; return the orchestrator context size."""
    context_tokens = sum(_tok(m["content"]) for m in orchestrator_messages)
    context_tokens += _tok(assignment)
    hits = search("lorem")
    context_tokens += _tok(json.dumps(hits))
    for doc_id in hits:
        # The raw document body lands in the ORCHESTRATOR'S context here.
        context_tokens += _tok(read(doc_id))
    return context_tokens


# --------------------------------------------------------------------------- #
# Orchestrator: spawns the sub-agent, sees only the distilled result          #
# --------------------------------------------------------------------------- #
def orchestrate(question: str) -> dict:
    result, internal, returned = run_isolated(question)

    # Non-isolated baseline shares the same starting orchestrator context.
    base_messages = [{"role": "user", "content": question}]
    inline_context = run_inline(question, base_messages)

    return {
        "final_answer": result.answer,
        "sources_seen_by_orchestrator": result.sources,
        "table": {
            "subagent_internal_tokens": internal,
            "tokens_returned_to_orchestrator": returned,
            "non_isolated_context_tokens": inline_context,
            "isolation_ratio": round(internal / max(1, returned), 1),
        },
    }


if __name__ == "__main__":
    q = "Which documents recorded a latency KEY FINDING, and how many?"
    out = orchestrate(q)
    print("=== FINAL ANSWER (orchestrator) ===")
    print(out["final_answer"])
    print("sources:", out["sources_seen_by_orchestrator"])
    print("\n=== CONTEXT ASYMMETRY ===")
    t = out["table"]
    print(f"{'metric':<38} tokens")
    print(f"{'sub-agent internal (isolated)':<38} {t['subagent_internal_tokens']}")
    print(f"{'returned to orchestrator':<38} {t['tokens_returned_to_orchestrator']}")
    print(f"{'non-isolated orchestrator context':<38} {t['non_isolated_context_tokens']}")
    print(f"\nisolation buys a {t['isolation_ratio']}x reduction in what the "
          "orchestrator must hold.")
