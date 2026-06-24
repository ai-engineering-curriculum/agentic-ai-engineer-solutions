# mod-205-evaluation-observability/exercise-03 — Solution

## Approach

The job is not "write a judge" — it's "write a judge whose scores you can trust, and
prove how much." That order matters, and the solution enforces it:

1. **Hand-label first.** Ground truth is built *before* the judge exists, so the
   judge cannot anchor on its own output. We score one fuzzy dimension —
   **faithfulness** (is every claim in the answer supported by the sources?) — on a
   **binary** scale, with written criteria reused verbatim in the judge prompt.
2. **Build the judge to the rubric rules from Chapter 3.** One dimension, binary
   scale, concrete anchors, **rationale before verdict**, structured output, and an
   auditable evidence field (`unsupported_claims`). Adversarial framing ("find what's
   wrong") defends against leniency; an explicit "length is not quality" line defends
   against verbosity bias.
3. **Calibrate against the human labels** with exact agreement and Cohen's kappa
   (chance-corrected). The number is the deliverable — a judge at ~85%+ agreement is
   trustworthy for that task; at 60% the rubric is broken.
4. **Show the rubric moved the number.** We run a deliberately **weak** rubric
   ("rate overall quality, 1–10, higher is better") and a **sharp** rubric, and
   report agreement before/after to demonstrate that sharpening the prompt — not
   editing the labels — is what fixes a bad judge.
5. **Probe a bias.** A verbosity probe pads a known-good short answer with filler and
   checks the verdict does not move.

To run **offline with no API key**, the reference ships a deterministic
`rule_based_judge` that simulates the two rubrics: the sharp version checks claim
grounding against the sources (high human agreement), the weak version scores on
answer length (low agreement, and visibly verbosity-biased). A `llm_judge` adapter
shows exactly where a real structured-output model call slots in — the calibration
and bias-probe code is identical for either backend.

## Reference implementation

Save as `judge_calibration.py` and run `python judge_calibration.py`. Pure standard
library; the live-model path is shown but not required.

```python
"""LLM-as-judge with calibration against human labels (offline-runnable).

Dimension: faithfulness (binary). Demonstrates rubric -> agreement coupling and a
verbosity bias probe. A deterministic judge stands in for a model so the file runs
with no API key; `llm_judge` shows where a real structured-output call goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Structured judgment (matches the chapter's starter)
# ---------------------------------------------------------------------------


@dataclass
class Judgment:
    reasoning: str                              # written BEFORE the verdict
    faithful: bool                              # one dimension, binary
    unsupported_claims: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dataset: agent answers + their sources + a hand label assigned FIRST.
# Each answer is a list of atomic claims so grounding is checkable.
# ---------------------------------------------------------------------------

DATASET = [
    {
        "sources": ["Paris is the capital of France.", "France is in Europe."],
        "claims": ["Paris is the capital of France."],
        "human_faithful": True,
    },
    {
        "sources": ["The Eiffel Tower is in Paris."],
        "claims": ["The Eiffel Tower is in Paris.", "It was built in 1889."],
        "human_faithful": False,  # build date not in sources -> unsupported
    },
    {
        "sources": ["Water boils at 100C at sea level."],
        "claims": ["Water boils at 100C at sea level."],
        "human_faithful": True,
    },
    {
        "sources": ["The book has 300 pages."],
        "claims": ["The book has 300 pages.", "The author is famous."],
        "human_faithful": False,
    },
    {
        "sources": ["Mercury is the closest planet to the Sun."],
        "claims": ["Mercury is the closest planet to the Sun."],
        "human_faithful": True,
    },
    {
        "sources": ["The store opens at 9am."],
        "claims": ["The store opens at 9am.", "It closes at 5pm."],
        "human_faithful": False,
    },
]

JUDGE_PROMPT = """You are a strict evaluator. Judge ONLY whether every claim in the
ANSWER is supported by the SOURCES. Reason first, then decide. Length is not quality;
a longer answer is not a better answer. Find what is unsupported.

SOURCES:
{sources}

ANSWER:
{answer}

Return reasoning, then faithful (true/false), then any unsupported claims."""


# ---------------------------------------------------------------------------
# Judges. `rule_based_judge` simulates two rubrics deterministically so the
# calibration story is reproducible offline.
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def rule_based_judge(claims: list[str], sources: list[str], rubric: str) -> Judgment:
    if rubric == "weak":
        # Bad rubric: scores on length, not grounding. High verbosity bias.
        total_len = sum(len(c) for c in claims)
        verdict = total_len < 60
        return Judgment(
            reasoning="overall quality felt high because the answer was concise",
            faithful=verdict,
        )
    # Sharp rubric: each claim must be supported by some source.
    src = [_normalize(s) for s in sources]
    unsupported = [c for c in claims if not any(_normalize(c) in s or s in _normalize(c) for s in src)]
    return Judgment(
        reasoning=f"checked {len(claims)} claim(s) against {len(sources)} source(s)",
        faithful=not unsupported,
        unsupported_claims=unsupported,
    )


def llm_judge(answer: str, sources: str) -> Judgment:
    """Where a real structured-output model call goes. Pseudocode:

        client = anthropic.Anthropic()  # key from env
        msg = client.messages.create(
            model="claude-sonnet-4",
            messages=[{"role": "user",
                       "content": JUDGE_PROMPT.format(sources=sources, answer=answer)}],
            tools=[{"name": "judgment", "input_schema": Judgment_schema}],
            tool_choice={"type": "tool", "name": "judgment"},
        )
        return Judgment(**msg.content[0].input)

    Use a DIFFERENT model family than the one that produced the answers to avoid
    self-preference bias.
    """
    raise NotImplementedError("wire a structured-output model call here")


# ---------------------------------------------------------------------------
# Calibration metrics
# ---------------------------------------------------------------------------


def agreement(judge_labels: list[bool], human_labels: list[bool]) -> float:
    matches = sum(j == h for j, h in zip(judge_labels, human_labels))
    return matches / len(human_labels)


def cohens_kappa(judge_labels: list[bool], human_labels: list[bool]) -> float:
    n = len(human_labels)
    po = agreement(judge_labels, human_labels)
    pj = sum(judge_labels) / n
    ph = sum(human_labels) / n
    pe = pj * ph + (1 - pj) * (1 - ph)        # chance agreement
    return 0.0 if pe == 1 else (po - pe) / (1 - pe)


# ---------------------------------------------------------------------------
# Calibration run + verbosity bias probe
# ---------------------------------------------------------------------------


def calibrate(rubric: str) -> dict:
    judge_labels, human_labels = [], []
    for row in DATASET:
        j = rule_based_judge(row["claims"], row["sources"], rubric)
        judge_labels.append(j.faithful)
        human_labels.append(row["human_faithful"])
    return {
        "rubric": rubric,
        "agreement": agreement(judge_labels, human_labels),
        "kappa": cohens_kappa(judge_labels, human_labels),
    }


def verbosity_probe(rubric: str) -> dict:
    # The padding is a verbose RESTATEMENT of a supported fact, so it adds length
    # without adding any unsupported claim. A faithful judge should ignore it; a
    # verbosity-biased (length-based) judge flips its verdict purely on length.
    sources = ["Paris is the capital of France."]
    base_claims = ["Paris is the capital of France."]
    padded_claims = base_claims + ["Paris is the capital of France."]  # grounded filler
    before = rule_based_judge(base_claims, sources, rubric).faithful
    after = rule_based_judge(padded_claims, sources, rubric).faithful
    return {"rubric": rubric, "verdict_before": before, "verdict_after": after,
            "moved": before != after}


def main() -> None:
    print("=== calibration: weak vs sharp rubric ===")
    weak = calibrate("weak")
    sharp = calibrate("sharp")
    for r in (weak, sharp):
        print(f"- rubric={r['rubric']:5}  agreement={r['agreement']:.3f}  kappa={r['kappa']:.3f}")
    print(f"  -> sharpening moved agreement {weak['agreement']:.3f} -> {sharp['agreement']:.3f}")

    print("\n=== verbosity bias probe ===")
    for r in (verbosity_probe("weak"), verbosity_probe("sharp")):
        flag = "BIASED (verdict moved on padding)" if r["moved"] else "stable"
        print(f"- rubric={r['rubric']:5}  {flag}")


if __name__ == "__main__":
    main()
```

Expected output: the **weak** rubric lands near chance with low kappa and is flagged
verbosity-biased (padding flips its verdict); the **sharp** rubric reaches full
agreement with kappa ≈ 1.0 and is stable under padding.

## Meeting the acceptance criteria

- **Hand-labeled before building the judge** — `human_faithful` is fixed in the
  dataset; the judge functions never read it except for scoring.
- **One dimension, binary, reasons before verdict, structured + auditable** — the
  `Judgment` dataclass carries `reasoning`, the binary `faithful`, and
  `unsupported_claims` as evidence; the sharp judge populates all three.
- **Concrete agreement number reported** — `calibrate` prints exact agreement and
  Cohen's kappa for the rubric.
- **Sharpening measurably changed agreement** — the weak-vs-sharp comparison shows a
  before/after jump driven by the rubric, not the labels.
- **At least one bias probe** — the verbosity probe pads a known-good answer and
  reports whether the verdict moved (it does for the weak rubric, not the sharp one).

## Common pitfalls

- **Labeling after seeing judge output.** If you write ground truth by reading the
  judge's verdicts, agreement is circular and meaningless. Label first, in a separate
  pass.
- **A 1–10 scale.** Models can't reliably separate a 6 from a 7; the spread becomes
  noise that tanks agreement. Use binary or 1–3 with anchors.
- **One "overall quality" score.** Smearing faithfulness, helpfulness, and tone into
  one number makes the result uninterpretable and uncalibratable. Score one dimension
  at a time.
- **Verdict before reasoning.** Putting the boolean before the rationale forfeits the
  chain-of-thought accuracy gain and leaves nothing to audit. Force reasoning first.
- **Judging with the same model that wrote the answers.** Self-preference inflates
  scores; use a different model family as the judge, and re-calibrate whenever the
  judge model, rubric, or task changes.

## Verification

```bash
python judge_calibration.py
```

Confirm the sharp rubric reports high agreement (1.000) and kappa near 1.0 while the
weak rubric sits near chance, and that the verbosity probe flags the weak rubric as
`BIASED` and the sharp one as `stable`. This reproduces the chapter's core claim:
agreement is a property of the **rubric**, and you fix a bad judge by sharpening the
prompt, not by editing the labels. To extend to a live judge, implement `llm_judge`
with a different model family and feed its labels into the same `agreement` /
`cohens_kappa` functions unchanged.
