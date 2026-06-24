# mod-203-rag-and-memory/exercise-02 — Solution

A reference build of an agent's three memory tiers — working, episodic, and
long-term — wired as a write-then-recall system with just-in-time retrieval and
conflict resolution. The agent remembers preferences across sessions, recalls
them only when a turn needs it, and resolves the "free tier → pro" contradiction
to the correct winner.

## Approach

The three tiers answer three different questions and live in three different
stores:

- **Working memory** is the current turn's scratch space — passed in, not
  persisted. It is the model's context window for one reasoning step.
- **Episodic memory** keeps the last `N` turns verbatim and rolls older turns
  into a running summary, exposed through `context()`. This bounds the prompt so
  a long conversation never overflows.
- **Long-term memory** is the exercise-01 vector store pointed at a different
  question: *"what do I already know about this user?"* Writes go through
  `remember`, which **consolidates** (decides what's durable) and stamps each
  fact with `timestamp`, `source`, and `confidence`.

Two mechanisms make it production-shaped:

1. **JIT recall as a tool.** `recall_memory` is exposed as a tool the agent
   *chooses* to call, not an eager pre-load. The system prompt tells it to look
   up stored facts only when relevant, so turns that are self-contained skip the
   recall round-trip entirely.
2. **Conflict resolution on recall and on write.** When two memories concern the
   same attribute and disagree, `resolve` picks the winner by **source authority,
   then recency, then confidence** (matching Chapter 3). On write, the new fact
   **supersedes** the stale one — the old memory is invalidated so recall never
   surfaces the contradiction again.

The vector store and embedder are imported conceptually from exercise-01; the
reference inlines a tiny compatible version so the file runs standalone.

## Reference implementation

```python
"""Exercise-02 reference: three-tier agent memory with JIT recall and conflict
resolution. Runs offline; the agent loop is simulated with a tool dispatcher so
the recall-as-a-tool behaviour is observable without an LLM."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


# --------------------------------------------------------------------------- #
# Minimal embedding + store (compatible with exercise-01's interface)
# --------------------------------------------------------------------------- #
EMBED_DIM = 256


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------- #
# Long-term memory record
# --------------------------------------------------------------------------- #
SOURCE_RANK = {"user_stated": 2, "agent_inferred": 1}


@dataclass(frozen=True)
class Memory:
    fact: str
    timestamp: str
    source: str        # "user_stated" | "agent_inferred"
    confidence: float
    attribute: str = ""   # e.g. "tier"; lets us detect same-attribute conflicts
    active: bool = True


# --------------------------------------------------------------------------- #
# Long-term store: write (consolidate + supersede) and recall (JIT)
# --------------------------------------------------------------------------- #
@dataclass
class LongTermMemory:
    _vecs: list[list[float]] = field(default_factory=list)
    _mems: list[Memory] = field(default_factory=list)

    def remember(
        self,
        fact: str,
        source: str,
        confidence: float,
        attribute: str = "",
    ) -> bool:
        """Consolidate then write. Returns False if the fact was declined.

        Consolidation rule: skip transient chatter (greetings, acks) and very
        low-confidence inferences. On write, supersede any active memory about
        the SAME attribute so the contradiction never resurfaces (Chapter 3)."""
        if not _is_durable(fact, confidence):
            return False
        new = Memory(fact=fact, timestamp=_now(), source=source,
                     confidence=confidence, attribute=attribute)
        if attribute:
            # Supersede stale memories about the same attribute (immutably:
            # rebuild the list with the old ones marked inactive).
            self._mems = [
                m if m.attribute != attribute or not m.active
                else Memory(**{**m.__dict__, "active": False})
                for m in self._mems
            ]
        self._vecs.append(embed(fact))
        self._mems.append(new)
        return True

    def recall_memory(self, cue: str, k: int = 5) -> list[Memory]:
        """JIT recall: the agent calls this as a tool. Returns the top-k ACTIVE
        memories, with same-attribute conflicts resolved to a single winner."""
        qv = embed(cue)
        scored = [
            (cosine(qv, v), m)
            for v, m in zip(self._vecs, self._mems)
            if m.active
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = [m for _, m in scored[:k]]
        return _dedupe_by_attribute(top)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TRANSIENT = re.compile(r"^\s*(hi|hello|thanks|thank you|ok|okay|sure|got it)\b",
                        re.IGNORECASE)


def _is_durable(fact: str, confidence: float) -> bool:
    if _TRANSIENT.match(fact):
        return False
    if confidence < 0.3:
        return False
    return True


def resolve(conflicting: list[Memory]) -> Memory:
    """Winner = highest source authority, then most recent, then most confident."""
    return max(
        conflicting,
        key=lambda m: (SOURCE_RANK[m.source], m.timestamp, m.confidence),
    )


def _dedupe_by_attribute(mems: list[Memory]) -> list[Memory]:
    by_attr: dict[str, list[Memory]] = {}
    passthrough: list[Memory] = []
    for m in mems:
        if m.attribute:
            by_attr.setdefault(m.attribute, []).append(m)
        else:
            passthrough.append(m)
    winners = [resolve(group) for group in by_attr.values()]
    return passthrough + winners


# --------------------------------------------------------------------------- #
# Episodic memory: verbatim window + rolling summary
# --------------------------------------------------------------------------- #
@dataclass
class EpisodicMemory:
    keep_last: int = 4
    _turns: list[str] = field(default_factory=list)
    _summary: str = ""

    def add_turn(self, role: str, text: str) -> None:
        self._turns.append(f"{role}: {text}")
        if len(self._turns) > self.keep_last:
            rolled = self._turns[: -self.keep_last]
            self._turns = self._turns[-self.keep_last :]
            self._summary = _summarize(self._summary, rolled)

    def context(self) -> str:
        parts = []
        if self._summary:
            parts.append(f"[summary] {self._summary}")
        parts.extend(self._turns)
        return "\n".join(parts)


def _summarize(prev: str, turns: list[str]) -> str:
    """Offline summary: keep the prior summary plus a compressed tail. SWAP for
    an LLM summarization call in production."""
    gist = "; ".join(t.split(": ", 1)[-1][:40] for t in turns)
    return (prev + " | " if prev else "") + gist


# --------------------------------------------------------------------------- #
# Simulated agent loop: recall is a TOOL the agent chooses to call
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "Look up what you know about the user with recall_memory only when it is "
    "relevant to the current turn. Do not recall for things already in this "
    "conversation."
)


@dataclass
class Agent:
    ltm: LongTermMemory
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)
    recall_calls: int = 0

    def turn(self, user_msg: str, needs_recall: Callable[[str], bool]) -> str:
        """One turn. `needs_recall` stands in for the model's decision to call the
        tool; in a real loop the LLM emits a tool call instead."""
        self.episodic.add_turn("user", user_msg)
        recalled: list[Memory] = []
        if needs_recall(user_msg):
            self.recall_calls += 1
            recalled = self.ltm.recall_memory(user_msg)
        reply = self._respond(user_msg, recalled)
        self.episodic.add_turn("assistant", reply)
        return reply

    def _respond(self, user_msg: str, recalled: list[Memory]) -> str:
        if recalled:
            facts = "; ".join(m.fact for m in recalled)
            return f"Based on what I know about you ({facts}), here is your answer."
        return "Answering from the current conversation."


# --------------------------------------------------------------------------- #
# Cross-session demonstration + the free->pro conflict
# --------------------------------------------------------------------------- #
def _demo() -> None:
    ltm = LongTermMemory()  # persists across sessions (the only retained state)

    # --- Session A: user states preferences; agent consolidates ---
    print("== session A ==")
    agent_a = Agent(ltm=ltm)
    agent_a.turn("Hi there!", needs_recall=lambda _: False)  # transient, declined
    ltm.remember("User prefers dark mode.", "user_stated", 0.95, attribute="theme")
    ltm.remember("User is on the free tier.", "user_stated", 0.9, attribute="tier")
    consolidated = ltm.remember("Hi there!", "user_stated", 0.9)
    print(f"  greeting consolidated? {consolidated} (expected False)")

    # End session A: working + episodic memory are dropped; LTM survives.
    del agent_a

    # --- Session B: fresh process state; agent recalls from LTM ---
    print("\n== session B (fresh) ==")
    agent_b = Agent(ltm=ltm)
    # A self-contained turn: NO recall needed (JIT saves a round-trip).
    print(" ", agent_b.turn("What is 2 + 2?", needs_recall=lambda _: False))
    # A turn that needs stored preferences: recall fires.
    print(" ", agent_b.turn("Set up my dashboard the way I like it.",
                            needs_recall=lambda _: True))
    print(f"  recall calls so far: {agent_b.recall_calls} (expected 1)")

    # --- Conflict: user upgrades; new fact supersedes the old ---
    print("\n== conflict: free -> pro ==")
    ltm.remember("User upgraded to the pro tier.", "user_stated", 0.95,
                 attribute="tier")
    resolved = _dedupe_by_attribute(ltm.recall_memory("what tier is the user on"))
    tier = next(m for m in resolved if m.attribute == "tier")
    print(f"  resolved tier: {tier.fact!r}")
    assert "pro" in tier.fact, "conflict must resolve to pro"
    # Stale free-tier memory is inactive and never recalled again.
    assert all("free" not in m.fact for m in ltm.recall_memory("tier"))
    print("  stale 'free tier' memory superseded")


if __name__ == "__main__":
    _demo()
```

## Meeting the acceptance criteria

- **Episodic memory keeps recent turns verbatim and summarizes older ones.**
  `EpisodicMemory` retains `keep_last` turns and rolls the rest into `_summary`;
  `context()` returns summary + recent turns, so a long conversation cannot
  overflow.
- **Long-term writes are clean facts with `timestamp`, `source`, `confidence`.**
  `remember` constructs a `Memory` with all three fields and runs `_is_durable`
  consolidation — it stores statements, never transcript dumps.
- **`recall_memory` is invoked as a tool, on demand.** `Agent.turn` only calls
  `recall_memory` when `needs_recall` is true; the demo shows the `2 + 2` turn
  answering *without* a recall call (`recall_calls` stays at 1).
- **Free→pro resolves to pro and the stale fact never resurfaces.** Writing the
  pro memory supersedes the free memory (marked `active=False`); `resolve`
  ranks `user_stated` + newer timestamp first, and the assertions confirm
  `"free"` never appears in subsequent recalls.
- **A fresh session recalls first-session preferences.** `LongTermMemory` is the
  only state carried from session A to session B; `agent_b` recalls dark-mode
  and tier without being re-told.

## Common pitfalls

- **Eager pre-loading instead of JIT.** Injecting all memories on every turn
  burns context and surfaces irrelevant facts. Recall must be a tool the agent
  *chooses* to call — the `needs_recall` gate models that decision.
- **Append-only memory.** Without supersession, the stale "free tier" fact keeps
  resurfacing next to "pro." Resolve on write (invalidate the old) *and* on
  recall (pick the winner).
- **Wrong resolution order.** Sorting by recency alone lets a freshly written
  low-authority inference beat an older user statement. Order is source
  authority → recency → confidence; build a case where recency would mislead
  (the reflection question).
- **Consolidating everything.** Remembering greetings and acks pollutes recall
  and raises cost. `_is_durable` declines transient chatter and low-confidence
  inferences.
- **Summarizing too aggressively.** Rolling turns into a summary too early loses
  detail the next turn needs. Tune `keep_last` so the verbatim window covers the
  active sub-task.

## Verification

```bash
python README_solution.py
```

Expect: the greeting is declined by consolidation (`False`); session B answers
`2 + 2` with no recall call and the dashboard turn with one; the conflict block
prints a resolved tier containing `pro` and confirms the stale free-tier memory
is superseded. All three assertions must pass.
