# AI Engineering · Agentic AI Engineer — Solutions Repository

<!-- aicg:site-banner -->
> 🎓 Part of the free, open-source **AI Career Curriculum** ecosystem — [Infrastructure](https://github.com/ai-infra-curriculum) · [ML Engineering](https://github.com/ml-engineering-curriculum) · [AI Engineering](https://github.com/ai-engineering-curriculum) · [Governance](https://github.com/ai-governance-curriculum). Live cohorts &amp; team programs: **[ai-infra-curriculum.github.io](https://ai-infra-curriculum.github.io/)**.
<!-- /aicg:site-banner -->

<!-- aicg:sponsor -->
> 💜 **[Sponsor this curriculum](https://github.com/sponsors/ai-engineering-curriculum)** — sponsorships keep the whole open-source AI Career Curriculum free and moving.
<!-- /aicg:sponsor -->

> **Complete reference solutions for every exercise and capstone in the Agentic AI Engineer track.**

## 🎯 Overview

This repository holds the reference implementations for the paired
[`ai-infra-agentic-ai-engineer-learning`](https://github.com/ai-engineering-curriculum/agentic-ai-engineer-learning)
track — the agentic rung of the AI Infrastructure Curriculum, covering how to
build, evaluate, guard, and ship autonomous LLM agents.

Every exercise solution is an annotated, **runnable** walkthrough rather than a
bare answer key. Each one includes:

- ✅ **Working reference code** — runs offline against a mock or scripted model, with a one-adapter swap to any live provider
- 📚 **Approach + walkthrough** — the reasoning behind the implementation, not just the result
- 🎯 **Acceptance-criteria mapping** — how the solution satisfies the learning-side requirements
- ⚠️ **Common pitfalls** — the grader-observed mistakes the design avoids
- 🔍 **Verification steps** — how to confirm the solution behaves as intended

> **Status:** ✅ Reference solutions complete for every module exercise and both
> capstone projects. AI-assisted content is under ongoing human review.

## 📁 Repository Structure

```text
ai-infra-agentic-ai-engineer-solutions/
├── modules/
│   ├── mod-201-agent-fundamentals/            # ReAct, tools, coding & planning agents
│   ├── mod-202-frameworks/                    # LangGraph, CrewAI, AutoGen, MCP
│   ├── mod-203-rag-and-memory/                # RAG pipelines, memory, evaluation
│   ├── mod-204-multi-agent-implementation/    # orchestration, handoffs, isolation
│   ├── mod-205-evaluation-observability/      # trajectory eval, tracing, judges
│   ├── mod-206-guardrails-implementation/     # moderation, injection, permissions
│   └── mod-207-productionizing-agents/        # APIs, durability, caching, HITL
├── projects/
│   ├── project-201-production-multi-agent-system/   # capstone walkthrough
│   └── project-202-benchmark-agent/                 # GAIA-style benchmark agent
├── SOLUTIONS_INDEX.md                         # inventory + completion map
└── README.md                                  # this file
```

## 📚 Modules

Each module directory holds a module-level overview plus one folder per exercise.
Solution counts below reflect the exercises with completed reference walkthroughs.

| Module | Solutions |
|--------|-----------|
| [mod-201 — Agent Fundamentals](./modules/mod-201-agent-fundamentals/README.md) | 4 |
| [mod-202 — Agent Frameworks in Practice](./modules/mod-202-frameworks/README.md) | 5 |
| [mod-203 — RAG & Memory](./modules/mod-203-rag-and-memory/README.md) | 4 |
| [mod-204 — Multi-Agent Implementation](./modules/mod-204-multi-agent-implementation/README.md) | 4 |
| [mod-205 — Evaluation & Observability](./modules/mod-205-evaluation-observability/README.md) | 4 |
| [mod-206 — Guardrails & Safety Implementation](./modules/mod-206-guardrails-implementation/README.md) | 4 |
| [mod-207 — Productionizing Agents](./modules/mod-207-productionizing-agents/README.md) | 4 |
| **Total** | **29** |

Two capstone projects round out the track:

- [project-201 — Production Multi-Agent System](./projects/project-201-production-multi-agent-system/README.md): build and deploy an end-to-end multi-agent system.
- [project-202 — Benchmark Agent](./projects/project-202-benchmark-agent/README.md): a GAIA-style benchmark agent.

See [`SOLUTIONS_INDEX.md`](./SOLUTIONS_INDEX.md) for the full inventory and
completion map.

## 📖 How to Use This Repository

### For Self-Study

1. **Start in the [learning repository](https://github.com/ai-engineering-curriculum/agentic-ai-engineer-learning)** to understand the concepts and attempt each exercise from the stubs.
2. **Implement the exercise yourself first** — the reference is most valuable as a comparison, not a starting point.
3. **Compare your approach** against the matching solution folder here.
4. **Read the walkthrough** to understand the trade-offs, pitfalls, and acceptance-criteria mapping.
5. **Run the reference** offline (no provider key required) and then **swap in a live model** using the documented one-adapter change.

### For Instructors

- Use the **learning repository** for assignments and the **solutions repository** as the answer key and lecture reference.
- The per-exercise pitfalls and verification steps double as grading rubrics.
- The deterministic offline fallbacks make the code safe to demo and reproduce in class without API keys or cost.

### For Hiring Managers

- Use the exercises and capstones as **technical-assessment baselines**.
- Evaluate candidate implementations against these references for correctness, structure, and agentic-design judgment.
- Reference the architecture and trade-off discussions as **interview talking points**.

## 🔗 Paired Learning Repository

This solutions repository is the counterpart to the hands-on track:

- **[ai-infra-agentic-ai-engineer-learning](https://github.com/ai-engineering-curriculum/agentic-ai-engineer-learning)** — learning materials, exercise stubs, and project briefs.

---

<!-- aicg:maintained-by -->
Maintained by [VeriSwarm.ai](https://veriswarm.ai)
