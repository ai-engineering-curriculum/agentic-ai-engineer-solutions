"""Evaluator-optimizer loop reference (mod-204 exercise-04).

A generator writes a Python function; an adversarial critic runs the real test
suite and returns a structured Verdict. The loop iterates until the work passes,
the round cap is hit, or the score stalls for two rounds -- so it both succeeds
and correctly gives up.

Run:  python evaluator_optimizer.py
The critic's verdict is grounded in pytest-style execution, so the loop runs
fully offline; ANTHROPIC_API_KEY is optional and only changes the generator.
"""

from __future__ import annotations

import os
from typing import Callable

from pydantic import BaseModel

MODEL_GENERATOR = "claude-sonnet-4-0"
MAX_ROUNDS = 4
NO_PROGRESS_LIMIT = 2
PASS_SCORE = 8


# --------------------------------------------------------------------------- #
# Structured verdict                                                          #
# --------------------------------------------------------------------------- #
class Verdict(BaseModel):
    passes: bool
    score: int                  # 1-10 against the rubric below
    issues: list[str]
    must_fix: list[str]


class Task(BaseModel):
    name: str
    prompt: str
    tests: list[tuple]          # (args_tuple, expected) cases the critic runs
    func_name: str


# --------------------------------------------------------------------------- #
# Generator: produces / revises the candidate function                        #
# --------------------------------------------------------------------------- #
GENERATOR_SYSTEM = (
    "You are a Python implementer. Write ONLY the function body for the named "
    "function. Address every issue raised by the critic in the previous round."
)


def generate(task: Task, prior: str | None, issues: list[str]) -> str:
    """Return source for the target function.

    Offline, we model a generator that gets the impossible task wrong and the
    passable task right after one correction -- exactly the dynamics the loop
    must handle. With an API key, the real model produces the source.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        client = anthropic.Anthropic()
        fix = ("\n\nFix these issues:\n" + "\n".join(issues)) if issues else ""
        prior_block = f"\n\nPrevious attempt:\n{prior}" if prior else ""
        msg = client.messages.create(
            model=MODEL_GENERATOR,
            max_tokens=512,
            system=GENERATOR_SYSTEM,
            messages=[{"role": "user", "content": task.prompt + prior_block + fix}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return _strip_fences(text)

    return _offline_generate(task, prior, issues)


def _offline_generate(task: Task, prior: str | None, issues: list[str]) -> str:
    if task.name == "is_even":
        if not issues:        # first attempt: off-by-one bug (uses == 1)
            return "def is_even(n):\n    return n % 2 == 1\n"
        return "def is_even(n):\n    return n % 2 == 0\n"   # corrected
    if task.name == "impossible":
        # Contradictory rubric: tests demand f(x) == x AND f(x) == x + 1.
        return "def identity(n):\n    return n\n"
    raise ValueError(task.name)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("python"):
            text = text[len("python"):]
    return text.strip() + "\n"


# --------------------------------------------------------------------------- #
# Critic: an adversarial evaluator grounded in a REAL test run                #
# --------------------------------------------------------------------------- #
CRITIC_SYSTEM = (
    "You are an adversarial reviewer. Your job is to find what is wrong, not to "
    "be agreeable. Run the tests and score against the rubric: 10 = all tests "
    "pass cleanly; subtract for every failing case."
)


def _run_tests(source: str, task: Task) -> list[str]:
    """Execute the candidate against the task's cases; return failure messages."""
    namespace: dict = {}
    try:
        exec(source, namespace)            # noqa: S102 - sandboxed reference demo
    except Exception as exc:               # generator produced non-runnable code
        return [f"source failed to import: {exc!r}"]
    fn: Callable = namespace.get(task.func_name)
    if fn is None:
        return [f"function {task.func_name!r} not defined"]

    failures: list[str] = []
    for args, expected in task.tests:
        try:
            got = fn(*args)
        except Exception as exc:
            failures.append(f"{task.func_name}{args} raised {exc!r}")
            continue
        if got != expected:
            failures.append(f"{task.func_name}{args} -> {got!r}, expected {expected!r}")
    return failures


def critique(source: str, task: Task) -> Verdict:
    """Deterministic, evidence-grounded verdict from the real test outcome."""
    failures = _run_tests(source, task)
    total = len(task.tests)
    passed = total - len(failures)
    score = max(1, round(10 * passed / total)) if total else 1
    return Verdict(
        passes=(not failures and score >= PASS_SCORE),
        score=score,
        issues=failures,
        must_fix=failures,        # every failing case must be fixed to pass
    )


# --------------------------------------------------------------------------- #
# The loop: generate -> evaluate -> revise, with hard stops                   #
# --------------------------------------------------------------------------- #
class LoopResult(BaseModel):
    source: str
    passed: bool
    final_score: int
    rounds_used: int
    exit_reason: str


def evaluate_optimize(task: Task, max_rounds: int = MAX_ROUNDS) -> LoopResult:
    draft = generate(task, prior=None, issues=[])
    best, best_score, no_progress = draft, -1, 0

    for round_i in range(1, max_rounds + 1):
        verdict = critique(draft, task)
        print(f"  [{task.name}] round {round_i}: score={verdict.score} "
              f"passes={verdict.passes} issues={len(verdict.issues)}")

        if verdict.passes:
            return LoopResult(source=draft, passed=True, final_score=verdict.score,
                              rounds_used=round_i, exit_reason="pass_condition")

        if verdict.score > best_score:
            best, best_score, no_progress = draft, verdict.score, 0
        else:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                return LoopResult(source=best, passed=False, final_score=best_score,
                                  rounds_used=round_i, exit_reason="no_progress")

        draft = generate(task, prior=draft, issues=verdict.issues)

    return LoopResult(source=best, passed=False, final_score=best_score,
                      rounds_used=max_rounds, exit_reason="round_cap")


# --------------------------------------------------------------------------- #
# Demos: one passable task, one impossible task                               #
# --------------------------------------------------------------------------- #
PASSABLE = Task(
    name="is_even",
    func_name="is_even",
    prompt="Write `def is_even(n)` returning True iff n is even.",
    tests=[((2,), True), ((3,), False), ((0,), True), ((-4,), True), ((7,), False)],
)

IMPOSSIBLE = Task(
    name="impossible",
    func_name="identity",
    prompt="Write `def identity(n)` satisfying a contradictory rubric.",
    # Contradiction: the same input must map to two different outputs.
    tests=[((5,), 5), ((5,), 6)],
)


def _report(label: str, res: LoopResult) -> None:
    print(f"--- {label} ---")
    print(f"  passed={res.passed} score={res.final_score} "
          f"rounds={res.rounds_used} exit={res.exit_reason}")
    print(f"  final source:\n    " + res.source.replace("\n", "\n    "))


if __name__ == "__main__":
    print("Passable task (should exit on pass condition):")
    _report("is_even", evaluate_optimize(PASSABLE))
    print("\nImpossible task (should exit via no_progress / round_cap, never loop forever):")
    _report("impossible", evaluate_optimize(IMPOSSIBLE))

    # Cost contrast: a single generator pass with no critic loop.
    single = generate(PASSABLE, prior=None, issues=[])
    single_pass = critique(single, PASSABLE).passes
    print(f"\nSingle-pass (no critic) on is_even passed: {single_pass} "
          "-- the off-by-one bug ships without the loop.")
