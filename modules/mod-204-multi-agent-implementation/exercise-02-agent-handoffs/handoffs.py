"""Agent handoffs reference (mod-204 exercise-02).

A support-desk flow: a cheap front-door router picks a starting specialist, then
specialists can hand off to each other via a typed payload. Loop protections --
a hop limit and a visited set -- guarantee termination at ``escalation``.

Run:  python handoffs.py
With a real model:  export ANTHROPIC_API_KEY=sk-ant-...  (otherwise an offline
deterministic stub drives the routing/handoff logic.)
"""

from __future__ import annotations

import json
import os
from typing import Literal, Optional

from pydantic import BaseModel

MODEL_ROUTER = "claude-3-5-haiku-latest"   # cheap classifier, no tools
MODEL_SPECIALIST = "claude-sonnet-4-0"
MAX_HOPS = 5

Specialist = Literal["billing", "technical", "escalation"]


# --------------------------------------------------------------------------- #
# Typed contracts                                                             #
# --------------------------------------------------------------------------- #
class Route(BaseModel):
    to: Specialist


class HandoffPayload(BaseModel):
    """The entire context the next agent receives -- never the transcript."""

    to: Specialist
    summary: str            # goal + what's established + what's still open


class AgentStep(BaseModel):
    """A specialist either answers (final) or hands off (handoff)."""

    final: Optional[str] = None
    handoff: Optional[HandoffPayload] = None


# --------------------------------------------------------------------------- #
# Model access with deterministic offline fallback                           #
# --------------------------------------------------------------------------- #
def _route_offline(user: str) -> Route:
    low = user.lower()
    if "refund" in low or "charge" in low or "invoice" in low:
        return Route(to="billing")
    if "error" in low or "crash" in low or "loop-bait" in low:
        return Route(to="technical")
    return Route(to="escalation")


def _step_offline(active: Specialist, context: str, forbid: set[str]) -> AgentStep:
    low = context.lower()
    # The mis-route demo: a billing-tagged request that actually needs technical.
    if active == "billing" and "actually a sync error" in low and "technical" not in forbid:
        return AgentStep(
            handoff=HandoffPayload(
                to="technical",
                summary="Customer reported a refund issue, but root cause is a sync error on export. Verify the export pipeline.",
            )
        )
    # The loop-bait demo: each specialist tries to bounce to the other forever.
    # The runtime's visited-set rule rewrites the second bounce to escalation.
    if "loop-bait" in low and active in ("technical", "billing"):
        target = "billing" if active == "technical" else "technical"
        return AgentStep(
            handoff=HandoffPayload(
                to=target,
                summary="loop-bait: cannot resolve; bouncing per the (deliberately) cyclic request.",
            )
        )
    if active == "escalation":
        return AgentStep(final="Escalated to a human owner with full context.")
    return AgentStep(final=f"[{active}] Resolved: {context[:80]}")


def call_router(user: str) -> Route:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _route_offline(user)
    import anthropic

    client = anthropic.Anthropic()
    schema = json.dumps(Route.model_json_schema())
    msg = client.messages.create(
        model=MODEL_ROUTER,
        max_tokens=128,
        system=("Classify the request and pick ONE starting specialist. Do no work. "
                f"Return ONLY JSON matching:\n{schema}"),
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return Route.model_validate_json(_extract_json(text))


def call_specialist(active: Specialist, context: str, forbid: set[str]) -> AgentStep:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _step_offline(active, context, forbid)
    import anthropic

    client = anthropic.Anthropic()
    schema = json.dumps(AgentStep.model_json_schema())
    system = (
        SPECIALIST_SYSTEM[active]
        + f"\n\nAgents that already handled this request (do NOT hand off to them): "
        + (", ".join(sorted(forbid)) or "none")
        + f"\n\nReturn ONLY JSON matching:\n{schema}. Set 'final' to answer, or "
        "'handoff' to transfer. Never both."
    )
    msg = client.messages.create(
        model=MODEL_SPECIALIST,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": context}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return AgentStep.model_validate_json(_extract_json(text))


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return text[start : end + 1]


# --------------------------------------------------------------------------- #
# Specialist system prompts (deliberately narrow scopes)                      #
# --------------------------------------------------------------------------- #
_LOOP_RULE = (
    " Do not hand off to an agent that has already handled this request -- "
    "answer it yourself or hand off to escalation."
)
SPECIALIST_SYSTEM: dict[Specialist, str] = {
    "billing": "You handle invoices, charges, and refunds ONLY." + _LOOP_RULE,
    "technical": "You handle product errors, crashes, and integrations ONLY." + _LOOP_RULE,
    "escalation": "You are the final stop. You always resolve or hand to a human; you never hand off." + _LOOP_RULE,
}


# --------------------------------------------------------------------------- #
# The handoff loop                                                            #
# --------------------------------------------------------------------------- #
def run(user_msg: str, max_hops: int = MAX_HOPS) -> tuple[str, list[Specialist]]:
    """Return (final_answer, handoff_path) for one request."""
    active = call_router(user_msg).to
    context, visited = user_msg, {active}
    path: list[Specialist] = [active]

    for _ in range(max_hops):
        step = call_specialist(active, context, forbid=visited)
        if step.final is not None:
            return step.final, path

        target = step.handoff.to
        # Enforce the visited rule in code, not just in the prompt.
        if target in visited:
            target = "escalation"
        active = target
        context = step.handoff.summary
        visited.add(active)
        path.append(active)

    # Hop limit hit: terminate cleanly at escalation rather than raising.
    if active != "escalation":
        path.append("escalation")
    final, _ = "escalated: handoff limit reached", None
    return final, path


def _demo(label: str, user_msg: str) -> None:
    answer, path = run(user_msg)
    print(f"--- {label} ---")
    print("request:", user_msg)
    print("path:   ", " -> ".join(path))
    print("answer: ", answer, "\n")


if __name__ == "__main__":
    _demo("correctly routed", "I was double-charged on my latest invoice.")
    _demo("mis-routed then handed off",
          "Refund please -- the export is broken, it's actually a sync error.")
    _demo("loop-bait (protections engage)",
          "loop-bait: keep bouncing me between billing and technical forever.")
