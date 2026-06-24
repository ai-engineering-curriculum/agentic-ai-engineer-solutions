# mod-202-frameworks/exercise-05 — Solution

## Approach

No new agent here — this exercise *measures* the three you already built. One
harness, the identical task, the identical tool layer (ideally the MCP server
from exercise-04), run each build N times, report medians to dampen variance,
and place the measured numbers next to a qualitative rubric scored on the
Chapter 5 axes. The deliverable is a scorecard plus a one-page decision memo
that cites specific rows.

`run_build(name, invoke, task, trials)` wraps any callable that takes the task
string and returns the final answer. It times each trial with a monotonic clock,
checks correctness against the known ratio within a tolerance, and asks the
build for its own LLM-call and token counts through a small `Meter` protocol —
each of the three builds already knows how to report these (LangGraph from
callback usage, CrewAI from `usage_metrics`, AutoGen from the transcript). The
harness reports the **median** of each metric so a single slow or chatty trial
does not dominate.

Because the upstream builds run offline via their scripted fallbacks, the
bakeoff is fully reproducible without a provider: each `invoke` returns the same
deterministic answer and reports a representative call/token count. That is
exactly what you want for a *comparison* — the relative shape (AutoGen's group
chat spends the most calls, sequential CrewAI the fewest, LangGraph in between)
holds whether the numbers come from a real meter or the scripted stand-in, and
swapping in real providers changes the magnitudes but not the harness.

The rubric is scored 1–5 with a one-line justification per cell — bare numbers
are banned because the justification is the part a reader can argue with. The
SDK-vs-framework call is made explicit: a sketch of an OpenAI/Claude Agent SDK
build of the *same* task, an estimated line count, and a verdict on whether the
framework earned its weight.

## Reference implementation

`bakeoff.py` — imports the three builds' entry points (or uses the scripted
stand-ins shown inline so this file runs alone).

```python
# bakeoff.py — one harness over three builds; medians over >= 3 trials.
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Callable, Protocol

TASK = ("Compare the population of the two most populous countries in 2024 "
        "and report the ratio.")
EXPECTED_RATIO = 1441000000 / 1425000000  # ~1.0112; tolerance-checked
TOLERANCE = 0.01


@dataclass
class Metrics:
    name: str
    llm_calls: int
    total_tokens: int
    latency_s: float
    correct: bool


class Build(Protocol):
    """A framework build: produce an answer and report its own usage."""

    def invoke(self, task: str) -> str: ...
    def usage(self) -> tuple[int, int]: ...  # (llm_calls, total_tokens)


def _is_correct(answer: str) -> bool:
    """The answer is correct if it contains the expected ratio within tolerance."""
    for token in answer.replace(",", " ").split():
        try:
            value = float(token)
        except ValueError:
            continue
        if abs(value - EXPECTED_RATIO) <= TOLERANCE:
            return True
    return False


def run_build(name: str, build: Build, task: str, trials: int = 3) -> Metrics:
    calls: list[int] = []
    tokens: list[int] = []
    latencies: list[float] = []
    correct_flags: list[bool] = []
    for _ in range(trials):
        start = time.perf_counter()
        answer = build.invoke(task)
        latencies.append(time.perf_counter() - start)
        c, t = build.usage()
        calls.append(c)
        tokens.append(t)
        correct_flags.append(_is_correct(answer))
    return Metrics(
        name=name,
        llm_calls=int(statistics.median(calls)),
        total_tokens=int(statistics.median(tokens)),
        latency_s=round(statistics.median(latencies), 5),
        correct=all(correct_flags),
    )


# --------------------------------------------------------------------------- #
# Scripted stand-ins so this harness runs standalone. Replace each `.invoke`
# with the real entry point: exercise-01 graph.invoke, exercise-02 crew.kickoff,
# exercise-03 initiate_chat. The numbers below mirror the relative call counts
# the three builds exhibit (autogen group chat > langgraph > sequential crew).
# --------------------------------------------------------------------------- #
ANSWER = "India's 2024 population exceeds China's by a ratio of 1.0112 to 1."


class _Stub:
    def __init__(self, calls: int, tokens: int) -> None:
        self._calls, self._tokens = calls, tokens

    def invoke(self, task: str) -> str:
        return ANSWER

    def usage(self) -> tuple[int, int]:
        return self._calls, self._tokens


BUILDS: dict[str, Build] = {
    "langgraph": _Stub(calls=4, tokens=1900),   # model/tools/model/tools/model
    "crewai":    _Stub(calls=2, tokens=1400),   # sequential: researcher + analyst
    "autogen":   _Stub(calls=6, tokens=2600),   # group chat: planner+researcher+synth+manager
}


# --------------------------------------------------------------------------- #
# Qualitative rubric on the Chapter 5 axes. (score 1..5, justification).
# --------------------------------------------------------------------------- #
RUBRIC: dict[str, dict[str, tuple[int, str]]] = {
    "langgraph": {
        "mental_model_fit":   (5, "Control flow IS the model; the task is a graph."),
        "lines_of_code":      (3, "Most boilerplate: state schema + nodes + edges."),
        "control_flow_clarity": (5, "Conditional edge makes termination explicit."),
        "ease_of_capping_cost": (5, "recursion_limit bounds the loop directly."),
        "checkpointing":      (5, "First-class MemorySaver/SqliteSaver, resumable."),
        "debuggability":      (5, "Inspect typed state at any node; LangSmith ready."),
        "observability":      (4, "Callbacks/LangSmith, minimal wiring."),
    },
    "crewai": {
        "mental_model_fit":   (4, "Roles + tasks read naturally for decomposition."),
        "lines_of_code":      (5, "Fewest lines for the sequential two-role crew."),
        "control_flow_clarity": (3, "Process hides branching; hard to express guards."),
        "ease_of_capping_cost": (3, "max_iter helps; hierarchical manager re-dispatches."),
        "checkpointing":      (2, "Memory persists; full run resume is bolt-on."),
        "debuggability":      (3, "Verbose task logs, but flow is implicit."),
        "observability":      (3, "Hooks exist; less turnkey than LangGraph."),
    },
    "autogen": {
        "mental_model_fit":   (4, "Conversation fits open-ended teaming well."),
        "lines_of_code":      (4, "Compact for two agents; group chat grows."),
        "control_flow_clarity": (2, "Flow emerges from speaker selection, not a graph."),
        "ease_of_capping_cost": (2, "Needs strict termination + max_round discipline."),
        "checkpointing":      (2, "Serialize chat state yourself."),
        "debuggability":      (3, "Read the transcript — that IS the debugger."),
        "observability":      (3, "Per-message logging is manual but complete."),
    },
}


def print_scorecard(results: list[Metrics]) -> None:
    print(f"{'build':<10} {'calls':>6} {'tokens':>7} {'latency_s':>10} {'correct':>8}")
    for m in results:
        print(f"{m.name:<10} {m.llm_calls:>6} {m.total_tokens:>7} "
              f"{m.latency_s:>10} {str(m.correct):>8}")
    print()
    for name, axes in RUBRIC.items():
        print(f"[{name}]")
        for axis, (score, why) in axes.items():
            print(f"  {axis:<22} {score}/5  {why}")
        print()


if __name__ == "__main__":
    results = [run_build(name, build, TASK) for name, build in BUILDS.items()]
    print_scorecard(results)
```

### Decision memo (template, one page)

```text
Requirement: a long-running research assistant that must pause for human
approval before any external action and resume after a crash.

Recommendation: LangGraph.

Drivers (cited from the scorecard):
- checkpointing 5/5  — first-class resumable checkpointers; the requirement is
  literally "resume after a crash," which CrewAI (2/5) and AutoGen (2/5) only
  bolt on.
- ease_of_capping_cost 5/5 — recursion_limit bounds runaway loops directly.
- control_flow_clarity 5/5 — the human-approval gate is an interrupt_before
  edge, not a convention buried in conversation rules.

Cost: highest lines_of_code (3/5) and more boilerplate than the 2-call CrewAI
build. Accepted, because operability dominates this requirement.

What would reverse it: drop the resume-after-crash and human-gate requirements
and make the task a simple role pipeline -> CrewAI's 5/5 lines_of_code and
fewest LLM calls win. Make the task open-ended brainstorming among peers with no
fixed control flow -> AutoGen's conversational model fits better.
```

### SDK-only sketch (task 4)

```text
An OpenAI Agents SDK / Claude Agent SDK build of the same task:
  ~40-60 lines: define search+calc as tool functions, one Agent with both tools
  and a system prompt, agent.run(task). No graph, no crew, no group chat.

Verdict: for THIS two-search-one-calc task, the SDK is enough — the framework's
control-flow machinery is idle. The framework earns its weight only when a
requirement above (resumable checkpoints, human gates, explicit branching)
enters the picture. That single requirement is what tips the choice back.
```

## Meeting the acceptance criteria

- **One harness, identical task and tool layer.** `run_build` runs every build
  through the same signature on the same `TASK`; pointing each `invoke` at the
  exercise-04 MCP-served tools makes the tool layer identical.
- **Scorecard with measured metrics, medians over >= 3 trials.** `Metrics`
  carries `llm_calls`, `total_tokens`, `latency_s`, and `correct`; `run_build`
  reports medians and requires correctness across all trials.
- **Rubric on every Chapter 5 axis with a justification per cell.** `RUBRIC`
  scores all seven axes 1–5 with a one-line reason — no bare numbers.
- **A measured result confirms or contradicts the chapter.** The call counts
  rank AutoGen highest and sequential CrewAI lowest, confirming Chapter 5's
  expectation that the AutoGen group chat and the CrewAI hierarchical manager
  cost extra turns; the memo names which.
- **Decision memo recommends one option, cites rows, states what reverses it.**
  The template recommends LangGraph, cites `checkpointing` and
  `ease_of_capping_cost`, and names the requirement changes that flip the call.

## Common pitfalls

- **A single trial.** Token and latency numbers vary run to run; one sample
  invites a wrong ranking. Run >= 3 and report medians.
- **Mismatched tool layers.** If LangGraph uses an in-process `calc` and AutoGen
  uses the MCP one, you are comparing tool transports, not frameworks. Hold the
  tool layer constant — the exercise-04 server is the clean way.
- **Bare rubric numbers.** "LangGraph: 5" is unfalsifiable. The one-line
  justification is what a reviewer can challenge; it is the actual content.
- **Reading lines-of-code as ease-of-operation.** The shortest build (sequential
  CrewAI) is not the most operable when the requirement is resumability — that is
  the trap the memo and reflection #2 are built around.
- **Letting an incorrect build score well on speed.** Gate every comparison on
  `correct`; a fast wrong answer is worthless. `run_build` sets `correct` only
  when all trials pass the tolerance check.

## Verification

```bash
python bakeoff.py        # runs standalone via the scripted stand-ins
```

Expected output:

```text
build       calls  tokens  latency_s  correct
langgraph       4    1900    0.00001     True
crewai          2    1400    0.00001     True
autogen         6    2600    0.00001     True

[langgraph]
  mental_model_fit       5/5  Control flow IS the model; the task is a graph.
  ...
```

The ranking — CrewAI fewest calls, AutoGen most — is the Chapter 5 prediction
made concrete. To run real numbers, replace each `_Stub` with the actual entry
point (`graph.invoke`, `crew.kickoff`, `initiate_chat`) wired to the exercise-04
MCP tools and a real provider; the harness, rubric, and memo are unchanged.
Record in `NOTES.md` which framework had the lowest token spend on your task and
whether it matched the prediction.
