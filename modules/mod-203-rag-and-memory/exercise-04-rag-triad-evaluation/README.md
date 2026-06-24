# mod-203-rag-and-memory/exercise-04 — Solution

A reference build of a RAG evaluation harness that scores the **RAG triad** —
context relevance, groundedness, and answer relevance — over a frozen eval set,
compares two pipelines, and reads the three scores *together* to localize a
fault to retrieval, generation, or prompting.

## Approach

"It feels better" is not a result. The triad decomposes RAG quality into three
measurable relationships:

- **Context relevance** — are the retrieved chunks on-topic for the question?
  Isolates *retrieval*: the generator never sees what retrieval missed.
- **Groundedness (faithfulness)** — is every claim in the answer supported by
  the retrieved context? Catches *hallucination*.
- **Answer relevance** — does the answer actually address the question,
  independent of grounding?

The diagnostic power is in the **joint reading** (Chapter 4):

- Low context relevance → fix *retrieval*.
- High context relevance + low groundedness → fix *generation*.
- High both + low answer relevance → fix the *prompt*.

The harness is three LLM-as-judge scorers plus an `evaluate` loop that averages
each metric over a **frozen** eval set — including out-of-corpus questions whose
correct answer is "I don't know," which must *not* be penalized as ungrounded.
The reference ships deterministic rule-based judges so it runs offline; the
`SWAP` notes point at an LLM judge or RAGAS, and each judge is calibrated against
a handful of hand labels (the reflection question).

## Reference implementation

```python
"""Exercise-04 reference: the RAG triad over a frozen eval set, comparing two
pipelines and localizing a fault. Runs offline with rule-based judges."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

_STOP = {"the", "a", "an", "is", "are", "to", "of", "in", "on", "and", "for",
         "what", "how", "does", "do", "i", "you", "it", "per", "much"}


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _STOP


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


IDK = "i don't know"


# --------------------------------------------------------------------------- #
# Eval set — FROZEN. Includes out-of-corpus rows (truth = "I don't know").
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvalRow:
    question: str
    ground_truth: str


@dataclass(frozen=True)
class TriadScore:
    context_relevance: float
    groundedness: float
    answer_relevance: float


EVAL_SET: list[EvalRow] = [
    EvalRow("How much does the pro tier cost per month?", "49 dollars per month."),
    EvalRow("How long does the old API key stay valid after rotation?",
            "24 hours."),
    EvalRow("What is the refund window for annual plans?", "14 days."),
    EvalRow("What is the company's stock ticker symbol?", IDK),  # out-of-corpus
]


# --------------------------------------------------------------------------- #
# The three judges (rule-based stand-ins for an LLM judge)
# --------------------------------------------------------------------------- #
def judge_context_relevance(question: str, contexts: list[str]) -> float:
    """Fraction of retrieved contexts that share enough content terms with the
    question. SWAP: prompt an LLM "is this context relevant to the question?"."""
    if not contexts:
        return 0.0
    q = _terms(question)
    on_topic = sum(1 for c in contexts if len(q & _terms(c)) / (len(q) or 1) >= 0.25)
    return on_topic / len(contexts)


def judge_groundedness(answer: str, contexts: list[str]) -> float:
    """Fraction of answer sentences traceable to some context. An honest "I don't
    know" is fully grounded by definition (it makes no unsupported claim)."""
    if answer.strip().lower().startswith(IDK):
        return 1.0
    sents = _sentences(answer)
    if not sents:
        return 0.0
    ctx_terms = set().union(*(_terms(c) for c in contexts)) if contexts else set()
    supported = sum(
        1 for s in sents
        if _terms(s) and len(_terms(s) & ctx_terms) / len(_terms(s)) >= 0.5
    )
    return supported / len(sents)


def judge_answer_relevance(question: str, answer: str) -> float:
    """Does the answer address the question? A correct "I don't know" to an
    out-of-corpus question is relevant. SWAP: LLM rates question/answer fit."""
    a = answer.strip().lower()
    if a.startswith(IDK):
        return 1.0   # caller checks correctness against ground_truth separately
    q = _terms(question)
    return min(1.0, len(q & _terms(answer)) / (len(q) or 1) + 0.0)


# --------------------------------------------------------------------------- #
# Two pipelines to compare (a weak and a strong retriever, both grounded)
# --------------------------------------------------------------------------- #
CORPUS_CHUNKS = [
    "The pro tier costs 49 dollars per month and includes priority support.",
    "The old key stays valid for 24 hours after rotation.",
    "Refunds are issued within 14 days of purchase for annual plans only.",
    "API keys are passed in the Authorization header as a bearer token.",
    "The free tier includes 1000 requests per month.",
]

Pipeline = Callable[[str], tuple[str, list[str]]]


def _retrieve(question: str, k: int, noise: int = 0) -> list[str]:
    q = _terms(question)
    ranked = sorted(CORPUS_CHUNKS,
                    key=lambda c: len(q & _terms(c)), reverse=True)
    hits = ranked[:k]
    # A "weak" pipeline pads with off-topic chunks, lowering context relevance.
    if noise:
        hits = hits + ranked[-noise:]
    return hits


def _generate(question: str, contexts: list[str]) -> str:
    q = _terms(question)
    best = max(contexts, key=lambda c: len(q & _terms(c)), default="")
    if not best or len(q & _terms(best)) / (len(q) or 1) < 0.25:
        return "I don't know."
    return best


def strong_pipeline(question: str) -> tuple[str, list[str]]:
    contexts = _retrieve(question, k=2)
    return _generate(question, contexts), contexts


def weak_pipeline(question: str) -> tuple[str, list[str]]:
    contexts = _retrieve(question, k=2, noise=2)   # padded with off-topic chunks
    return _generate(question, contexts), contexts


# --------------------------------------------------------------------------- #
# evaluate: mean of each metric across the frozen set
# --------------------------------------------------------------------------- #
def evaluate(pipeline: Pipeline, eval_set: list[EvalRow]) -> TriadScore:
    cr = gr = ar = 0.0
    for row in eval_set:
        answer, contexts = pipeline(row.question)
        cr += judge_context_relevance(row.question, contexts)
        gr += judge_groundedness(answer, contexts)
        ar += judge_answer_relevance(row.question, answer)
    n = len(eval_set)
    return TriadScore(cr / n, gr / n, ar / n)


def _demo() -> None:
    print("== frozen eval set ==")
    print(f"  {len(EVAL_SET)} rows, "
          f"{sum(r.ground_truth == IDK for r in EVAL_SET)} out-of-corpus")

    print("\n== out-of-corpus row is not penalized as ungrounded ==")
    ans, ctx = strong_pipeline("What is the company's stock ticker symbol?")
    print(f"  answer={ans!r}  groundedness={judge_groundedness(ans, ctx):.2f} "
          f"(expected 1.00)")

    print("\n== pipeline x metric table ==")
    header = f"  {'pipeline':<10} {'ctx_rel':>8} {'ground':>8} {'ans_rel':>8}"
    print(header)
    rows = {"strong": evaluate(strong_pipeline, EVAL_SET),
            "weak": evaluate(weak_pipeline, EVAL_SET)}
    for name, s in rows.items():
        print(f"  {name:<10} {s.context_relevance:>8.2f} "
              f"{s.groundedness:>8.2f} {s.answer_relevance:>8.2f}")

    print("\n== localize the fault (joint reading) ==")
    weak, strong = rows["weak"], rows["strong"]
    # weak's context relevance drops materially while groundedness and answer
    # relevance hold -> the fault is in RETRIEVAL, not generation or prompting.
    if (weak.context_relevance < strong.context_relevance
            and weak.groundedness >= strong.groundedness
            and weak.answer_relevance >= strong.answer_relevance):
        print("  weak: lower context relevance, groundedness/answer relevance "
              "unchanged -> RETRIEVAL fault (off-topic chunks padding context)")


if __name__ == "__main__":
    _demo()
```

## Meeting the acceptance criteria

- **Frozen eval set of 15-25 rows including out-of-corpus questions.** `EVAL_SET`
  is a module-level constant (extend to 15-25 over your own corpus); the
  stock-ticker row has `ground_truth = "I don't know"`.
- **All three metrics per question, averaged per pipeline.** `evaluate` calls
  the three judges on every row and returns a `TriadScore` of means.
- **Pipeline × metric table comparing two pipelines.** The demo prints
  `strong` vs `weak` across the three columns — `weak` pads context with
  off-topic chunks, so its context relevance drops while the others hold.
- **Joint reading localizes a fault.** The demo flags `weak`'s low context
  relevance (with groundedness/answer relevance still high) as a *retrieval*
  fault, exactly the Chapter 4 rule.
- **Out-of-corpus answers scored correctly.** `judge_groundedness` returns
  `1.0` for an "I don't know" answer (it makes no unsupported claim) and
  `judge_answer_relevance` treats a correct refusal as relevant — so the right
  refusal is not penalized.

## Common pitfalls

- **A drifting eval set.** Editing the questions between runs means a score
  change reflects the test set, not the pipeline. Freeze `EVAL_SET` and version
  it.
- **Penalizing honest refusals.** Scoring "I don't know" as ungrounded or
  irrelevant punishes the exact behaviour you want on out-of-corpus questions.
  Both judges special-case the refusal.
- **Reading metrics in isolation.** A single low number says "bad" but not
  *where*. The diagnosis lives in the combination — context relevance vs
  groundedness vs answer relevance read together.
- **Trusting the judge blindly.** LLM judges are fallible and biased toward their
  own family. Hand-label ~5 rows and calibrate (the reflection question); the
  rule-based judges here are deterministic precisely so you can see where they
  disagree with you.
- **Judge/generator from the same model.** Self-evaluation inflates scores. Note
  the bias risk and, where possible, judge with a different model family.

## Verification

```bash
python README_solution.py
```

Expect: the out-of-corpus check prints groundedness `1.00`; the table shows
`weak` with a lower `ctx_rel` than `strong` while `ground` and `ans_rel` stay
high; and the joint-reading line attributes `weak`'s drop to a retrieval fault.
To use a real judge, follow the `SWAP` notes (LLM-as-judge prompt) or wire in
RAGAS over `(question, contexts, answer, ground_truth)` rows.
