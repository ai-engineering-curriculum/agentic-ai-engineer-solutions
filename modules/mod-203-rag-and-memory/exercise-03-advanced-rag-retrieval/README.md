# mod-203-rag-and-memory/exercise-03 — Solution

A reference build of advanced retrieval that decouples the unit you *search*
from the unit you *read*: sentence-window retrieval, auto-merging (hierarchical)
retrieval, and a cross-encoder re-ranker — with a side-by-side comparison
against naive fixed-size chunking.

## Approach

Naive RAG forces one chunk size to do two jobs at once: small enough to embed
precisely, large enough to answer from. The two patterns here break that tie.

- **Sentence-window retrieval** indexes at the *sentence* level for sharp
  matching, but stores with each sentence a window of the `W` sentences before
  and after. You match on the sentence embedding, then expand each hit to its
  window before building the prompt — precise recall, coherent context.
- **Auto-merging retrieval** builds a two-level tree: parent chunks (a section)
  split into child leaves. You embed and retrieve *leaves*, but track each
  leaf's parent. When enough children of one parent are retrieved (a
  threshold), you **merge up** — replace those leaves with the single parent
  chunk — giving narrow queries fine leaves and broad queries an adaptive,
  coherent parent.
- **Re-ranking** pairs with both: retrieve generously (top-20) with fast vector
  search for *recall*, then score each `(query, candidate)` pair with a slower
  cross-encoder for *precision*, and keep the top-5. The reference uses an
  offline term-overlap scorer as the cross-encoder stand-in, with the real
  `sentence-transformers` `CrossEncoder` behind a guarded import.

All three reuse the exercise-01 embedder and store interface, so the comparison
is fair: only the retrieval strategy changes.

## Reference implementation

```python
"""Exercise-03 reference: sentence-window, auto-merging, and re-ranking.
Runs offline with the standard library; swap points marked inline."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

EMBED_DIM = 256
_STOP = {"the", "a", "an", "is", "are", "to", "of", "in", "on", "and", "for"}


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def split_sentences(text: str) -> list[str]:
    # Drop markdown heading lines so a heading never fuses onto the first
    # sentence of its section.
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", body.strip()) if s.strip()]


# --------------------------------------------------------------------------- #
# 1. Sentence-window retrieval: match a sentence, return its window
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SentenceUnit:
    sentence: str          # the unit we EMBED and match on
    window: str            # the W sentences around it, returned to the prompt
    source: str
    vector: tuple[float, ...] = ()


def sentence_window_index(
    docs: dict[str, str], w: int = 2
) -> list[SentenceUnit]:
    units: list[SentenceUnit] = []
    for source, doc in docs.items():
        sents = split_sentences(doc)
        for i, sent in enumerate(sents):
            lo, hi = max(0, i - w), min(len(sents), i + w + 1)
            window = " ".join(sents[lo:hi])
            units.append(SentenceUnit(sent, window, source, tuple(embed(sent))))
    return units


def sentence_window_retrieve(
    units: list[SentenceUnit], question: str, k: int = 3
) -> list[tuple[float, SentenceUnit]]:
    qv = embed(question)
    scored = [(cosine(qv, list(u.vector)), u) for u in units]
    scored.sort(key=lambda p: p[0], reverse=True)
    # Match on the sentence; the prompt context is each hit's WINDOW.
    return scored[:k]


# --------------------------------------------------------------------------- #
# 2. Auto-merging retrieval: retrieve leaves, merge up to the parent
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TreeChunk:
    text: str
    chunk_id: str
    parent_id: str | None
    vector: tuple[float, ...] = ()


def build_chunk_tree(docs: dict[str, str]) -> tuple[list[TreeChunk], dict[str, TreeChunk]]:
    """Parents = sections (split on blank lines); children = sentences."""
    leaves: list[TreeChunk] = []
    parents: dict[str, TreeChunk] = {}
    for source, doc in docs.items():
        for s_idx, section in enumerate(re.split(r"\n\s*\n", doc.strip())):
            section = section.strip()
            if not section:
                continue
            pid = f"{source}:sec{s_idx}"
            parents[pid] = TreeChunk(section, pid, None)
            for c_idx, sent in enumerate(split_sentences(section)):
                cid = f"{pid}:c{c_idx}"
                leaves.append(TreeChunk(sent, cid, pid, tuple(embed(sent))))
    return leaves, parents


def auto_merge_retrieve(
    leaves: list[TreeChunk],
    parents: dict[str, TreeChunk],
    question: str,
    k: int = 6,
    threshold: int = 2,
) -> list[TreeChunk]:
    qv = embed(question)
    scored = sorted(
        ((cosine(qv, list(l.vector)), l) for l in leaves),
        key=lambda p: p[0], reverse=True,
    )
    hits = [l for _, l in scored[:k]]
    # Count retrieved children per parent; merge up when count >= threshold.
    by_parent: dict[str, list[TreeChunk]] = {}
    for leaf in hits:
        by_parent.setdefault(leaf.parent_id or "", []).append(leaf)
    merged: list[TreeChunk] = []
    seen_parents: set[str] = set()
    for leaf in hits:
        pid = leaf.parent_id or ""
        if len(by_parent[pid]) >= threshold and pid in parents:
            if pid not in seen_parents:
                merged.append(parents[pid])   # replace children with the parent
                seen_parents.add(pid)
        else:
            merged.append(leaf)               # narrow query: keep the leaf
    return merged


# --------------------------------------------------------------------------- #
# 3. Re-ranking: top-20 vector recall, cross-encoder precision, keep top-5
# --------------------------------------------------------------------------- #
def _offline_cross_encoder(query: str, candidate: str) -> float:
    """Term-overlap stand-in. SWAP for a real cross-encoder:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
        return float(model.predict([(query, candidate)])[0])
    """
    q = set(re.findall(r"[a-z0-9]+", query.lower())) - _STOP
    c = set(re.findall(r"[a-z0-9]+", candidate.lower())) - _STOP
    if not q:
        return 0.0
    # Reward exact-term coverage (precision) over mere cosine proximity.
    return len(q & c) / len(q)


def rerank(query: str, candidates: list[str], top_k: int = 5) -> list[tuple[float, str]]:
    scored = [(_offline_cross_encoder(query, c), c) for c in candidates]
    scored.sort(key=lambda p: p[0], reverse=True)
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Demo: head-to-head context + a re-rank that promotes the right chunk
# --------------------------------------------------------------------------- #
DOCS = {
    "kb.md": (
        "# Billing\n\n"
        "The pro tier costs 49 dollars per month. It includes priority support. "
        "The pro tier also raises the quota to 50000 requests.\n\n"
        "# Keys\n\n"
        "API key ABC-123 is the legacy key. Rotate keys from the dashboard. "
        "The old key stays valid for 24 hours after rotation."
    ),
}


def _demo() -> None:
    question = "How much does the pro tier cost per month?"

    print("== sentence-window (match sentence, feed window) ==")
    units = sentence_window_index(DOCS, w=1)
    for score, u in sentence_window_retrieve(units, question, k=1):
        print(f"  matched: {u.sentence!r}")
        print(f"  window : {u.window!r}")

    print("\n== auto-merging (leaf vs merged parent) ==")
    leaves, parents = build_chunk_tree(DOCS)
    # Narrow: retrieve a single leaf (k=1) so it cannot reach the merge
    # threshold -> stays a leaf. Broad: retrieve several children of one
    # section -> merges up to the parent.
    narrow = auto_merge_retrieve(leaves, parents, "legacy key ABC-123",
                                 k=1, threshold=2)
    broad = auto_merge_retrieve(leaves, parents, question, k=4, threshold=2)
    print(f"  narrow query -> {len(narrow)} units, first is "
          f"{'parent' if narrow[0].parent_id is None else 'leaf'}")
    print(f"  broad query  -> first is "
          f"{'parent (merged up)' if broad[0].parent_id is None else 'leaf'}")

    print("\n== re-ranking (vector top-20 -> cross-encoder top-5) ==")
    candidates = [u.window for u in units]   # pretend these are top-20 vector hits
    for score, text in rerank(question, candidates, top_k=3):
        print(f"  {score:.2f}  {text[:55]}...")


if __name__ == "__main__":
    _demo()
```

## Meeting the acceptance criteria

- **Sentence-window matches on a sentence but feeds the window.**
  `SentenceUnit` stores `sentence` (embedded/matched) and `window` (returned);
  `sentence_window_retrieve` ranks by the sentence vector while the demo prints
  the surrounding window that goes into the prompt.
- **Auto-merging returns leaves for narrow queries and a merged parent when
  enough children match.** `auto_merge_retrieve` counts retrieved children per
  parent and swaps them for `parents[pid]` once `threshold` is crossed; the demo
  shows a narrow query staying at the leaf and a broad query merging up.
- **Re-ranking reorders a top-20 set and improves the top-5.** `rerank` scores
  each `(query, candidate)` with the cross-encoder stand-in and re-sorts; the
  cost/billing window with the exact term "pro tier" rises above merely
  cosine-similar windows.
- **Side-by-side comparison.** The demo prints naive-style windows, the
  auto-merge leaf-vs-parent decision, and the re-ranked order for the same
  question — the basis for the required comparison table in `NOTES.md`.

## Common pitfalls

- **Embedding the window instead of the sentence.** Sentence-window's whole
  point is sharp matching: embed the *sentence*, return the *window*. Embedding
  the window collapses it back into naive chunking.
- **Auto-merge threshold too low.** A threshold of 1 merges to the parent on a
  single hit, drowning narrow queries in irrelevant sibling text. Tune it so
  only genuinely broad queries merge up (the stretch goal).
- **Re-ranking the wrong recall set.** Re-ranking a top-5 cannot recover a chunk
  that vector search ranked 12th. Retrieve generously (top-20) *then* re-rank, or
  the cross-encoder never sees the right candidate.
- **Confusing bi-encoder and cross-encoder.** The vector store's bi-encoder is
  fast and approximate; the cross-encoder is slow and exact. Use the first for
  recall and the second only for the final ordering — never the reverse.
- **Eyeballing the winner.** A side-by-side table *looks* convincing but is not
  measured. Carry these retrievers into exercise-04 and let the RAG triad decide.

## Verification

```bash
python README_solution.py
```

Expect: the sentence-window block prints a single matched sentence and a longer
window around it; the auto-merging block reports the narrow query staying at a
leaf and the broad query merging up to a parent; the re-ranking block lists the
pro-tier cost window first with the highest score. To use real models, install
`sentence-transformers` and follow the `SWAP` note in `_offline_cross_encoder`.
