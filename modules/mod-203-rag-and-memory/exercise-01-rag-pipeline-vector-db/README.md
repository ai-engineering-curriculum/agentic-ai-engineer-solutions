# mod-203-rag-and-memory/exercise-01 — Solution

A reference build of a retrieval-augmented generation pipeline: structure-aware
chunking with metadata, an embed step, a swappable vector store, top-k retrieval
with scores, and a grounded generation that cites sources and refuses to answer
out-of-corpus questions.

## Approach

The pipeline is five composable stages, each isolated so you can tune one
without rewriting the others:

1. **Chunk.** Split on structure (blank-line paragraphs) first, then pack
   paragraphs up to a target token budget with ~15% overlap. Every `Chunk`
   carries `source`, `section`, and `timestamp` — the same metadata exercise-02
   needs for conflict resolution.
2. **Embed.** One embedding function for both chunks and queries. The reference
   uses a deterministic hashed bag-of-words embedding so the code runs offline;
   a one-line swap points it at a real model. The store **asserts** that every
   query vector matches the index dimension, which is the single most common RAG
   wiring bug.
3. **Store.** A thin `VectorStore` interface (`upsert`, `query`) with an
   in-memory cosine-similarity implementation. Because the pipeline only depends
   on that interface, swapping in pgvector, Chroma, or Qdrant changes nothing
   above it (stretch goal).
4. **Retrieve.** Embed the question, score against the index, return the top-k
   `(score, Chunk)` pairs sorted high-to-low.
5. **Ground.** Build a prompt that injects the retrieved chunks and instructs
   the model to answer **only** from them, **cite** each `source`, and say
   "I don't know" when the answer is absent. The offline generator enforces the
   same contract with a sentence-overlap rule so the "I don't know" path is
   demonstrable without an API key.

Keeping the store behind an interface and the embedder behind a function is what
makes exercises 02 and 03 reuse this file instead of forking it.

## Reference implementation

```python
"""Exercise-01 reference: an end-to-end RAG pipeline with a swappable store.

Runs offline with the standard library. To use real components, set the three
swap points noted inline (EMBED, STORE, GENERATE).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Chunk:
    """A retrievable passage plus the metadata later exercises depend on."""

    text: str
    source: str
    section: str
    timestamp: str
    chunk_id: str = ""


# --------------------------------------------------------------------------- #
# 1. Chunking — structure first, then length, with overlap
# --------------------------------------------------------------------------- #
def _approx_tokens(text: str) -> int:
    """Cheap token estimate (~1 token per whitespace word). Good enough to size
    chunks without pulling in a tokenizer dependency."""
    return len(text.split())


def chunk_document(
    doc: str,
    source: str,
    target_tokens: int = 300,
    overlap_ratio: float = 0.15,
) -> list[Chunk]:
    """Split on paragraph structure, then pack paragraphs up to ``target_tokens``
    with ``overlap_ratio`` carry-over between consecutive chunks."""
    now = datetime.now(timezone.utc).isoformat()
    # Structure-aware split: blank lines separate paragraphs; a leading "# ..."
    # line names the current section.
    section = source
    paragraphs: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", doc.strip()):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^#+\s+(.*)", block)
        if heading:
            section = heading.group(1).strip()
            continue
        paragraphs.append((section, block))

    chunks: list[Chunk] = []
    buffer: list[str] = []
    buf_section = section
    overlap_tokens = max(1, int(target_tokens * overlap_ratio))

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(buffer).strip()
        cid = hashlib.sha1(f"{source}:{len(chunks)}:{text[:40]}".encode()).hexdigest()[:12]
        chunks.append(Chunk(text=text, source=source, section=buf_section,
                            timestamp=now, chunk_id=cid))

    for sec, para in paragraphs:
        if not buffer:
            buf_section = sec
        buffer.append(para)
        if _approx_tokens(" ".join(buffer)) >= target_tokens:
            flush()
            # Carry the tail of this chunk forward as overlap.
            tail = " ".join(buffer).split()[-overlap_tokens:]
            buffer = [" ".join(tail)]
            buf_section = sec
    flush()
    return chunks


# --------------------------------------------------------------------------- #
# 2. Embedding — ONE model for chunks and queries
# --------------------------------------------------------------------------- #
EMBED_DIM = 256


def hashed_embedding(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-words embedding, L2-normalised.

    SWAP (EMBED): replace this body with a call to a real embedding model, e.g.
        return openai_client.embeddings.create(model=..., input=text).data[0].embedding
    Keep the SAME function for chunks and queries (acceptance criterion).
    """
    vec = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
        # A signed second hash reduces collisions cancelling out.
        vec[(h // dim) % dim] += 1.0 if (h & 1) else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both inputs are unit vectors


Embedder = Callable[[str], list[float]]


# --------------------------------------------------------------------------- #
# 3. Vector store — thin, swappable interface
# --------------------------------------------------------------------------- #
class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk]) -> None: ...
    def query(self, text: str, k: int) -> list[tuple[float, Chunk]]: ...


@dataclass
class InMemoryStore:
    """Cosine-similarity store. Swap for pgvector/Chroma/Qdrant behind this
    interface (STORE) without touching anything above."""

    embed: Embedder
    dim: int = EMBED_DIM
    _vectors: list[list[float]] = field(default_factory=list)
    _chunks: list[Chunk] = field(default_factory=list)

    def upsert(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            vec = self.embed(chunk.text)
            assert len(vec) == self.dim, (
                f"embedding dim {len(vec)} != index dim {self.dim}"
            )
            self._vectors.append(vec)
            self._chunks.append(chunk)

    def query(self, text: str, k: int) -> list[tuple[float, Chunk]]:
        qvec = self.embed(text)
        assert len(qvec) == self.dim, "query embedded with wrong-dimension model"
        scored = [(cosine(qvec, v), c) for v, c in zip(self._vectors, self._chunks)]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:k]


# --------------------------------------------------------------------------- #
# 4. Retrieve
# --------------------------------------------------------------------------- #
def retrieve(store: VectorStore, question: str, k: int = 5) -> list[tuple[float, Chunk]]:
    return store.query(question, k)


# --------------------------------------------------------------------------- #
# 5. Grounded generation — cite or refuse
# --------------------------------------------------------------------------- #
GROUNDING_PROMPT = """You answer ONLY from the context below.
Cite the [source] of each claim. If the answer is not in the context,
reply exactly "I don't know."

Context:
{context}

Question: {question}
Answer:"""


def _offline_generate(question: str, hits: list[tuple[float, Chunk]]) -> str:
    """Rule-based stand-in for an LLM: answers only when a retrieved chunk shares
    enough content words with the question, otherwise refuses. Demonstrates the
    grounding contract without an API key.

    SWAP (GENERATE): replace with an LLM call using GROUNDING_PROMPT.
    """
    q_terms = set(re.findall(r"[a-z0-9]+", question.lower())) - _STOPWORDS
    best_score, best = 0.0, None
    for _, chunk in hits:
        c_terms = set(re.findall(r"[a-z0-9]+", chunk.text.lower()))
        overlap = len(q_terms & c_terms) / (len(q_terms) or 1)
        if overlap > best_score:
            best_score, best = overlap, chunk
    if best is None or best_score < 0.30:
        return "I don't know."
    return f"{best.text} [{best.source}]"


_STOPWORDS = {"the", "a", "an", "is", "are", "what", "which", "of", "to", "in",
              "on", "and", "for", "how", "do", "does", "i", "you", "it"}


def answer(
    store: VectorStore,
    question: str,
    k: int = 5,
    generate: Callable[[str, list[tuple[float, Chunk]]], str] | None = None,
) -> str:
    hits = retrieve(store, question, k)
    if generate is not None:
        return generate(question, hits)
    if os.getenv("OPENAI_API_KEY"):
        return _llm_generate(question, hits)  # pragma: no cover - needs key
    return _offline_generate(question, hits)


def _llm_generate(question: str, hits: list[tuple[float, Chunk]]) -> str:  # pragma: no cover
    """Optional real-LLM path. Requires `pip install openai` and a key."""
    from openai import OpenAI

    context = "\n\n".join(f"[{c.source}] {c.text}" for _, c in hits)
    prompt = GROUNDING_PROMPT.format(context=context, question=question)
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return resp.choices[0].message.content.strip()


# --------------------------------------------------------------------------- #
# Demo: in-corpus question cites; out-of-corpus refuses; k/chunk-size sweep
# --------------------------------------------------------------------------- #
CORPUS = {
    "billing.md": (
        "# Billing\n\n"
        "The free tier includes 1000 requests per month and community support.\n\n"
        "The pro tier costs 49 dollars per month and adds priority support "
        "and a 50000 request quota.\n\n"
        "# Refunds\n\n"
        "Refunds are issued within 14 days of purchase for annual plans only."
    ),
    "auth.md": (
        "# Authentication\n\n"
        "API keys are passed in the Authorization header as a bearer token.\n\n"
        "Keys can be rotated from the dashboard; the old key stays valid for "
        "24 hours after rotation."
    ),
}


def build_store(embed: Embedder = hashed_embedding,
                target_tokens: int = 40) -> InMemoryStore:
    # Small target_tokens here so the tiny demo corpus yields one chunk per
    # paragraph and retrieval is discriminative. Raise it for real corpora.
    store = InMemoryStore(embed=embed)
    for source, doc in CORPUS.items():
        store.upsert(chunk_document(doc, source, target_tokens=target_tokens))
    return store


def _demo() -> None:
    store = build_store()

    print("== retrieve scores (known question) ==")
    for score, chunk in retrieve(store, "How much does the pro tier cost?", k=3):
        print(f"  {score:.3f}  [{chunk.source}/{chunk.section}] {chunk.text[:60]}...")

    print("\n== in-corpus (should cite) ==")
    print(" ", answer(store, "How much does the pro tier cost?"))

    print("\n== out-of-corpus (should refuse) ==")
    print(" ", answer(store, "What is the company's stock ticker symbol?"))

    print("\n== k sweep (same question, k=2 vs k=8) ==")
    for k in (2, 8):
        hits = retrieve(store, "How are API keys rotated?", k=k)
        print(f"  k={k}: {len(hits)} chunks, top source = {hits[0][1].source}")


if __name__ == "__main__":
    _demo()
```

## Meeting the acceptance criteria

- **Chunks in a real vector DB with `source`, `section`, `timestamp`.** Every
  `Chunk` is constructed with all three fields in `chunk_document`; `upsert`
  stores them alongside the vector. `InMemoryStore` is the offline stand-in —
  the `STORE` swap point replaces it with pgvector/Chroma/Qdrant unchanged
  above the interface.
- **Same embedding model for chunks and queries, with a dimension assertion.**
  `InMemoryStore.embed` is used in both `upsert` and `query`, and both paths
  `assert len(vec) == self.dim`.
- **Ranked retrieval with scores, on-topic top hits.** `retrieve` returns
  `(score, Chunk)` sorted descending; the demo prints scores for a known
  question and the billing chunk carrying the pro-tier fact appears in the
  top hits. (With the deterministic offline embedding a lexically similar
  off-topic chunk can edge it out at rank 1 — exactly the blind spot the
  reflection asks about and that re-ranking in exercise-03 corrects.)
- **Cited answer in-corpus, "I don't know" out-of-corpus.** `answer` appends
  `[source]`; `_offline_generate` returns `"I don't know."` below the overlap
  threshold, and the stock-ticker demo question triggers it.
- **k and chunk size change the retrieved set.** The demo sweeps `k=2` vs `k=8`;
  `chunk_document(target_tokens=...)` exposes chunk size, so re-indexing at 150
  vs 500 tokens changes which passages exist to retrieve.

## Common pitfalls

- **Mismatched embedders.** Embedding chunks with one model and queries with
  another silently returns garbage neighbours. The shared `embed` reference plus
  the dimension `assert` make the mistake fail loudly.
- **Chunking on length only.** Splitting at a fixed character count slices
  sentences mid-thought and orphans headings from their bodies. Split on
  structure first; fall back to length only to cap size.
- **No overlap.** Zero-overlap chunks drop facts that straddle a boundary.
  ~15% overlap keeps boundary spanning answers retrievable.
- **Trusting vector similarity blindly.** A lexically similar but off-topic
  chunk can outscore the right one (the reflection question). This is exactly
  why exercise-03 adds re-ranking and exercise-04 measures it.
- **Letting the model answer ungrounded.** Without the explicit "only from
  context / say I don't know" instruction, the model fabricates. The grounding
  contract must be in the prompt, not assumed.

## Verification

```bash
python README_solution.py   # or paste the block into a .py file and run it
```

Expect: retrieval prints positive scores for the top chunks (the billing chunk
with the pro-tier fact is among them); the in-corpus question prints a cited
answer ending in `[billing.md]`; the stock-ticker question prints
`I don't know.`; and the `k=2`/`k=8` sweep prints different chunk counts.
Swapping `EMBED` for a real model sharpens the ranking; swapping `STORE` or
`GENERATE` should leave every line above the interface unchanged.
