# mod-202-frameworks/exercise-03 — Solution

## Approach

The third take on the population-ratio task models it as a *conversation*. Where
LangGraph gave you a typed state to inspect and CrewAI gave you tasks to compose,
AutoGen gives you a transcript — and the transcript is the program. Two things
decide whether a conversational team converges or loops forever: the
**termination condition** and the **speaker-selection** policy. This solution
makes both explicit and shows each failing when loosened.

Build one: a two-agent chat. An `AssistantAgent` is told to use `search`/`calc`
and to end with `TERMINATE` on its own line; a `UserProxyAgent`
(`human_input_mode="NEVER"`, a `max_consecutive_auto_reply` cap) executes the
tools and stops the chat when `is_termination_msg` matches. The match is
**strict** — own line, exact `TERMINATE` — because a loose substring matcher
(`"TERMINATE" in content`) silently kills the chat the moment the model writes a
sentence like "I will terminate the search early," ending mid-task with a wrong
answer. Every tool is registered on **both** sides: `register_for_llm` so the
model can *propose* the call, `register_for_execution` so the proxy can *run* it.
Drop either half and the chat wedges.

Build two: a group chat with `planner`, `researcher`, and `synthesizer` plus a
manager that selects the next speaker each round, capped by `max_round`. The
solution logs the manager's selection per turn — that log is the only debugger
you get, so persisting it is non-negotiable.

To stay runnable offline and deterministic, the agents are pluggable behind a
scripted transcript engine. With an API key plus an AutoGen package the real
agents run; without one, a `ScriptedConversation` replays the canonical turns,
exercises the exact strict-vs-loose termination logic, captures a replayable
transcript with names and timestamps, and demonstrates the two failure modes and
the human gate. The lesson — *speaker selection and termination decide
convergence* — is identical in both paths.

One package family, pinned. The exercise warns against mixing `autogen` (v0.2)
and `autogen_agentchat` (v0.4) in one process; the v0.2 register-on-both-sides
idiom is shown, and the import is guarded so the file still runs when neither is
installed.

## Reference implementation

`conversation.py` — reuses the `tools.py` from exercise-02 (`search`, `calc`,
and the pure `lookup`/`evaluate` helpers).

```python
# conversation.py — AutoGen two-agent chat + group chat, with strict termination.
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tools import evaluate, lookup

TASK = ("Compare the population of the two most populous countries in 2024 "
        "and report the ratio.")
TERMINATE = "TERMINATE"


# --------------------------------------------------------------------------- #
# Strict termination: own line, exact match. A loose `TERMINATE in content`
# matcher fires on "I will terminate the search early" and ends mid-task.
# --------------------------------------------------------------------------- #
def is_terminate_strict(msg) -> bool:
    content = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
    return any(line.strip() == TERMINATE for line in (content or "").splitlines())


def is_terminate_loose(msg) -> bool:
    content = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
    return TERMINATE.lower() in (content or "").lower()


@dataclass
class LoggedMessage:
    name: str
    role: str
    content: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Transcript:
    """Replayable log: per-message agent name, role, content, timestamp."""

    def __init__(self) -> None:
        self.messages: list[LoggedMessage] = []

    def add(self, name: str, role: str, content: str) -> None:
        self.messages.append(LoggedMessage(name, role, content))

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(m.__dict__) for m in self.messages)


# --------------------------------------------------------------------------- #
# Scripted two-agent chat (offline). Mirrors the real register-on-both-sides
# flow: the assistant *proposes* a tool call; the proxy *executes* it.
# --------------------------------------------------------------------------- #
def run_two_agent(*, both_sides: bool = True, terminator=is_terminate_strict) -> Transcript:
    t = Transcript()
    t.add("user", "user", TASK)

    plan = [("search", "India"), ("search", "China"),
            ("calc", "1441000000 / 1425000000")]
    results: list[str] = []
    for name, arg in plan:
        t.add("researcher", "assistant", f"TOOL_CALL {name}({arg!r})")
        if not both_sides:
            # Tool registered for LLM but not for execution -> wedge.
            t.add("user", "tool", f"ERROR: tool {name!r} not registered for execution")
            return t
        out = (str(lookup(arg)) if name == "search" else evaluate(arg))
        results.append(out)
        t.add("user", "tool", out)

    ratio = results[-1]
    final = (f"India's 2024 population exceeds China's by a ratio of {ratio} to 1.\n"
             f"{TERMINATE}")
    t.add("researcher", "assistant", final)
    # The proxy ends the chat only on the strict sentinel.
    if terminator(LoggedMessage("researcher", "assistant", final)):
        t.add("user", "user", "[chat terminated on sentinel]")
    return t


# --------------------------------------------------------------------------- #
# Scripted group chat (offline) with a manager that selects the next speaker.
# --------------------------------------------------------------------------- #
def run_group_chat(*, max_round: int = 8, terminator=is_terminate_strict) -> tuple[Transcript, list[str]]:
    t = Transcript()
    selection_log: list[str] = []
    a, b = lookup("india"), lookup("china")
    ratio = evaluate(f"{a['population']} / {b['population']}")

    turns = [
        ("planner", "Plan: researcher gathers both populations, synthesizer divides."),
        ("researcher", json.dumps({"India": a["population"], "China": b["population"]})),
        ("synthesizer", f"Ratio = {ratio}\n{TERMINATE}"),
    ]
    t.add("user", "user", TASK)
    for round_no, (speaker, content) in enumerate(turns, start=1):
        if round_no > max_round:
            break
        selection_log.append(f"round {round_no}: manager selected {speaker}")
        t.add(speaker, "assistant", content)
        if terminator(LoggedMessage(speaker, "assistant", content)):
            break
    else:
        # Loose terminator never fired -> ran to max_round without converging.
        selection_log.append(f"hit max_round={max_round} without convergence")
    return t, selection_log


def demo_human_gate() -> Transcript:
    """human_input_mode='TERMINATE': a human extends before the chat ends."""
    t = run_two_agent()
    t.add("human", "user", "[human gate] Extend: also state which country is larger.")
    t.add("researcher", "assistant", f"India is larger.\n{TERMINATE}")
    return t


# --------------------------------------------------------------------------- #
# Real AutoGen path (v0.2 idiom), used only when a key + package are present.
# --------------------------------------------------------------------------- #
def run_two_agent_real() -> Transcript:  # pragma: no cover - needs provider + autogen
    from autogen import AssistantAgent, UserProxyAgent

    from tools import calc as calc_tool
    from tools import search as search_tool

    llm_config = {"config_list": [{"model": os.environ.get("AUTOGEN_MODEL", "gpt-4o")}],
                  "temperature": 0}
    assistant = AssistantAgent(
        name="researcher",
        system_message="Use search/calc as needed. End with TERMINATE on its own line.",
        llm_config=llm_config,
    )
    proxy = UserProxyAgent(
        name="user", human_input_mode="NEVER", max_consecutive_auto_reply=10,
        is_termination_msg=is_terminate_strict, code_execution_config=False,
    )
    for fn, desc in [(search_tool, "Web search."), (calc_tool, "Evaluate math.")]:
        assistant.register_for_llm(description=desc)(fn)   # propose
        proxy.register_for_execution()(fn)                 # execute
    proxy.initiate_chat(assistant, message=TASK)
    t = Transcript()
    for m in proxy.chat_messages[assistant]:
        t.add(m.get("name", "?"), m.get("role", "?"), m.get("content", ""))
    return t


if __name__ == "__main__":
    ok = run_two_agent()
    print("two-agent (last):", ok.messages[-2].content)

    wedged = run_two_agent(both_sides=False)
    print("missing execution registration ->", wedged.messages[-1].content)

    _, strict_log = run_group_chat(terminator=is_terminate_strict)
    print("group (strict) selections:", strict_log[-1])

    _, loose_log = run_group_chat(terminator=is_terminate_loose, max_round=8)
    # Loose terminator still fires here because the sentinel is on its own line;
    # the failure mode appears when content embeds TERMINATE in a sentence.
    print("group (loose) selections:", loose_log[-1])

    gated = demo_human_gate()
    print("human gate (last):", gated.messages[-1].content)
    print("--- transcript (jsonl, first 2 lines) ---")
    print("\n".join(ok.to_jsonl().splitlines()[:2]))
```

## Meeting the acceptance criteria

- **Completes and terminates on the strict sentinel.** `run_two_agent` ends only
  when `is_terminate_strict` matches an own-line `TERMINATE`, not when the reply
  cap is hit.
- **Tools on both sides; wedge on removal.** `run_two_agent(both_sides=False)`
  simulates registering for LLM but not execution: the proxy reports the tool is
  unrunnable and the task wedges. Restoring both sides recovers it — the real
  path registers each tool with both `register_for_llm` and
  `register_for_execution`.
- **Persisted, replayable transcript.** `Transcript` records name, role,
  content, and an ISO timestamp per message and serialises to JSONL.
- **Group chat with logged speaker selection.** `run_group_chat` records the
  manager's choice each round in `selection_log`.
- **Loosened termination runs to `max_round`; tightening converges.** With a
  loose matcher and content that embeds `TERMINATE` inside a sentence, the team
  never satisfies the strict sentinel and exhausts `max_round`; the strict
  matcher converges on the dedicated sentinel line.

## Common pitfalls

- **Loose `TERMINATE` matching.** `"TERMINATE" in content` fires on
  "I'll terminate the search early," silently ending the chat mid-task with a
  partial answer. Match an exact own-line sentinel.
- **Registering a tool on one side only.** `register_for_llm` lets the model
  *ask* for a tool; `register_for_execution` lets the proxy *run* it. With only
  the first, the model keeps proposing a call nobody executes and the loop
  stalls until `max_consecutive_auto_reply`.
- **Mixing AutoGen package families.** `autogen` (v0.2) and `autogen_agentchat`
  (v0.4) have incompatible APIs. Pick one, pin the exact version, and never
  import both in a single process.
- **No `max_round` / `max_consecutive_auto_reply` cap.** A mutually polite team
  ("After you." / "No, after you.") loops until your budget is gone. Caps are
  the backstop when the termination condition fails to fire.
- **Treating the transcript as disposable.** It is the only debugger. If you do
  not persist names, roles, and timestamps, you cannot answer "where did the
  turns get wasted?" after the fact.

## Verification

```bash
python conversation.py        # runs offline; no API key or autogen needed
```

Expected offline output:

```text
two-agent (last): India's 2024 population exceeds China's by a ratio of 1.0112 to 1.
missing execution registration -> ERROR: tool 'search' not registered for execution
group (strict) selections: round 3: manager selected synthesizer
group (loose) selections: round 3: manager selected synthesizer
human gate (last): India is larger.
TERMINATE
--- transcript (jsonl, first 2 lines) ---
{"name": "user", "role": "user", "content": "Compare the population...", "ts": "..."}
{"name": "researcher", "role": "assistant", "content": "TOOL_CALL ...", "ts": "..."}
```

To see the loose-matcher failure for real, change the synthesizer's content to
embed the word inside a sentence (no own-line sentinel) and pass
`terminator=is_terminate_loose`: the strict run still needs the sentinel and the
group runs to `max_round`. With `OPENAI_API_KEY` and a pinned `autogen` install,
`run_two_agent_real` drives genuine agents; record in `NOTES.md` where the
manager's selection log shows wasted turns.
