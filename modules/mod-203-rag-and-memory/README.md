# mod-203-rag-and-memory — Solutions

Reference solutions for the RAG & Memory module. Each exercise directory holds a
single `README.md` with the approach, an annotated runnable reference
implementation, a mapping to the acceptance criteria, common pitfalls, and
verification steps.

The reference implementations favour **offline fallbacks** (deterministic
hash-based embeddings, a rule-based generator, and a rule-based judge) so every
snippet runs with `python` and the standard library alone. Each solution notes
where to swap in a real embedding model, vector store, LLM, and cross-encoder
for production use.

## Index

- [exercise-01: Build a RAG pipeline on a vector DB](exercise-01-rag-pipeline-vector-db/README.md)
  — chunk, embed, store, retrieve, and ground a cited generation behind a
  swappable vector-store interface.
- [exercise-02: Agent long-term memory](exercise-02-agent-long-term-memory/README.md)
  — working, episodic, and long-term tiers with just-in-time recall and
  conflict resolution by source authority, recency, and confidence.
- [exercise-03: Advanced RAG retrieval](exercise-03-advanced-rag-retrieval/README.md)
  — sentence-window and auto-merging retrieval plus cross-encoder re-ranking.
- [exercise-04: RAG triad evaluation](exercise-04-rag-triad-evaluation/README.md)
  — context relevance, groundedness, and answer relevance over a frozen eval
  set to compare and localize faults across pipelines.

## Conventions

- Code targets Python 3.11+ and uses only the standard library in its default
  (offline) path; optional integrations are guarded behind `try/except` imports.
- Metadata fields (`source`, `section`, `timestamp`, `confidence`) are carried
  end to end so exercise-02's conflict resolution and exercise-04's evaluation
  can reuse them.
- Data is treated immutably: retrieval and resolution return new lists rather
  than mutating the store in place.
