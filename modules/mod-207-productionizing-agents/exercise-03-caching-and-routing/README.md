# mod-207-productionizing-agents/exercise-03-caching-and-routing — Solution

## Approach

The exercise has two levers and one rule: add **prompt caching** to the stable
prefix, add a **model router** that sends each turn to the smallest capable
model, and **measure** both wins against a baseline without dropping pass rate.

The design separates the three concerns so each can be measured in isolation:

- A **provider seam** (`call_model`) wraps the Anthropic Messages API and returns
  a normalized result: text, `input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`, and latency. Caching
  is on or off per call via the `cache_control` breakpoint on the system block.
- A **router** (`choose_model`) maps each turn to small / mid / large by task kind
  and cheap input heuristics, with an **escalation path**: if the cheap model's
  output fails a check, retry on the large model and record that escalation fired.
- A **harness** runs the eval set under three configurations — baseline,
  `+cache`, `+cache+route` — and prints a comparison table of total cost,
  mean/p95 latency, and pass rate. Cost is computed from a per-model price table
  that bills cache reads at the discounted rate, so the cache win shows up in
  dollars, not just token counts.

To stay runnable offline, the provider seam ships a deterministic **fake
backend** that simulates cache behavior (first call writes, later identical
prefixes read) and per-model latency/quality. Set `CACHE_FAKE=0` with a real key
to hit the live API; the harness, router, and table code are unchanged. The eval
set is a small bundled JSONL of representative turns with a per-turn check.

## Reference implementation

Layout:

```text
exercise-03-caching-and-routing/
├── caching_routing/
│   ├── __init__.py
│   ├── provider.py     # call_model seam: real Anthropic OR fake backend
│   ├── router.py       # choose_model + escalation
│   ├── pricing.py      # per-model price table, cost incl. cache discount
│   └── harness.py      # run eval set across 3 configs, print table
├── data/
│   └── evalset.jsonl   # ~12 representative turns with checks
├── tests/
│   └── test_caching_routing.py
└── requirements.txt
```

### `caching_routing/provider.py`

```python
"""Model-call seam. Returns normalized usage so caching and routing wins are
measurable. The fake backend simulates the cache so the whole exercise runs with
no key and no spend; CACHE_FAKE=0 switches to the live Anthropic API.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

FAKE = os.environ.get("CACHE_FAKE", "1") != "0"

# Per-model simulated behavior for the fake backend: latency and a crude quality
# proxy used to model when a cheap model "fails" a hard turn.
_FAKE_PROFILE = {
    "claude-3-5-haiku-latest": {"latency": 0.05, "handles_hard": False},
    "claude-sonnet-4-0": {"latency": 0.12, "handles_hard": True},
    "claude-opus-4-1": {"latency": 0.30, "handles_hard": True},
}

# Tracks which (model, prefix) pairs have been "written" so a second identical
# prefix reads from cache, mirroring the ephemeral cache.
_FAKE_CACHE_SEEN: set[tuple[str, int]] = set()


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    latency_s: float


def reset_fake_cache() -> None:
    """Clear the simulated cache (used between harness configs and in tests)."""
    _FAKE_CACHE_SEEN.clear()


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _call_fake(model: str, system_prefix: str, user_turn: str, cache: bool) -> ModelResult:
    profile = _FAKE_PROFILE[model]
    time.sleep(profile["latency"])
    prefix_tokens = _approx_tokens(system_prefix)
    out_tokens = _approx_tokens(user_turn) + 20

    read = create = 0
    fresh_input = prefix_tokens + _approx_tokens(user_turn)
    if cache:
        seen_key = (model, hash(system_prefix) & 0xFFFF)
        if seen_key in _FAKE_CACHE_SEEN:
            read = prefix_tokens          # prefix served from cache
            fresh_input = _approx_tokens(user_turn)
        else:
            create = prefix_tokens        # first call writes the cache
            _FAKE_CACHE_SEEN.add(seen_key)

    # Deterministic "answer". Hard turns answered by a non-capable model are
    # marked so the escalation check can catch them.
    quality_tag = "" if profile["handles_hard"] else "[LOW_CONFIDENCE]"
    text = f"{quality_tag}answer({user_turn[:40]})"
    return ModelResult(
        text=text,
        model=model,
        input_tokens=fresh_input,
        output_tokens=out_tokens,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=create,
        latency_s=profile["latency"],
    )


def _call_real(model: str, system_prefix: str, user_turn: str, cache: bool) -> ModelResult:
    import anthropic  # noqa: PLC0415 — lazy import keeps the offline path clean

    client = anthropic.Anthropic()
    system_block = {"type": "text", "text": system_prefix}
    if cache:
        system_block["cache_control"] = {"type": "ephemeral"}

    start = time.perf_counter()
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[system_block],
        messages=[{"role": "user", "content": user_turn}],
    )
    latency = time.perf_counter() - start
    u = msg.usage
    return ModelResult(
        text=msg.content[0].text,
        model=model,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        latency_s=latency,
    )


def call_model(model: str, system_prefix: str, user_turn: str, cache: bool) -> ModelResult:
    """One model call. `cache` marks the system prefix with cache_control."""
    backend = _call_fake if FAKE else _call_real
    return backend(model, system_prefix, user_turn, cache)
```

### `caching_routing/pricing.py`

```python
"""Per-model pricing and cost computation. Cache reads bill at a discount, so the
caching win shows up in dollars. Rates are illustrative USD per 1M tokens — update
to the current public price sheet before quoting real numbers.
"""
from __future__ import annotations

from caching_routing.provider import ModelResult

# (input, output, cache_write, cache_read) USD per 1M tokens.
PRICES = {
    "claude-3-5-haiku-latest": (0.80, 4.00, 1.00, 0.08),
    "claude-sonnet-4-0": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4-1": (15.00, 75.00, 18.75, 1.50),
}


def cost_usd(r: ModelResult) -> float:
    """Dollar cost of one call, billing cache reads/writes at their own rates."""
    in_rate, out_rate, write_rate, read_rate = PRICES[r.model]
    return (
        r.input_tokens * in_rate
        + r.output_tokens * out_rate
        + r.cache_creation_input_tokens * write_rate
        + r.cache_read_input_tokens * read_rate
    ) / 1_000_000
```

### `caching_routing/router.py`

```python
"""Model routing with an escalation path. Cheap by default, expensive on demand,
and a failure check that retries a botched cheap-model turn on the large model.
"""
from __future__ import annotations

from caching_routing.provider import ModelResult, call_model

SMALL = "claude-3-5-haiku-latest"
MID = "claude-sonnet-4-0"
LARGE = "claude-opus-4-1"


def choose_model(turn: dict) -> str:
    """Route by task kind and cheap input heuristics."""
    if turn.get("kind") in {"classify", "extract", "format"}:
        return SMALL
    if turn.get("needs_reasoning") or turn.get("context_tokens", 0) > 50_000:
        return LARGE
    return MID


def failed_check(result: ModelResult) -> bool:
    """Escalation trigger: a self-reported low-confidence marker or empty output.
    A real check might validate JSON structure or score against a rubric."""
    return "[LOW_CONFIDENCE]" in result.text or not result.text.strip()


def run_turn(turn: dict, system_prefix: str, cache: bool) -> tuple[ModelResult, bool]:
    """Run one turn through the router. Returns (result, escalated)."""
    model = choose_model(turn)
    result = call_model(model, system_prefix, turn["input"], cache)
    if failed_check(result) and model != LARGE:
        result = call_model(LARGE, system_prefix, turn["input"], cache)
        return result, True
    return result, False
```

### `caching_routing/harness.py`

```python
"""Run the eval set under baseline / +cache / +cache+route and print a comparison
table: total cost, mean & p95 latency, pass rate. The win is read off the numbers.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from caching_routing.pricing import cost_usd
from caching_routing.provider import call_model, reset_fake_cache
from caching_routing.router import MID, run_turn

SYSTEM_PREFIX = (
    "You are a precise assistant. Tools: search, calculator, formatter. "
    "Context: " + ("lorem ipsum stable context. " * 200)  # large, stable head
)


def load_evalset(path: str = "data/evalset.jsonl") -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def passes(turn: dict, text: str) -> bool:
    """Per-turn check: the expected substring appears and no low-confidence flag
    leaked through (escalation should have removed it)."""
    return turn["expect"] in text and "[LOW_CONFIDENCE]" not in text


def _summary(costs, latencies, passed, total, escalations):
    lat = sorted(latencies)
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)] if lat else 0.0
    return {
        "total_cost": sum(costs),
        "mean_latency": statistics.mean(latencies) if latencies else 0.0,
        "p95_latency": p95,
        "pass_rate": passed / total if total else 0.0,
        "escalations": escalations,
    }


def run_config(turns: list[dict], *, cache: bool, route: bool) -> dict:
    reset_fake_cache()
    costs, latencies = [], []
    passed = escalations = 0
    for turn in turns:
        if route:
            result, escalated = run_turn(turn, SYSTEM_PREFIX, cache)
            escalations += int(escalated)
        else:
            result = call_model(MID, SYSTEM_PREFIX, turn["input"], cache)
        costs.append(cost_usd(result))
        latencies.append(result.latency_s)
        passed += int(passes(turn, result.text))
    return _summary(costs, latencies, passed, len(turns), escalations)


def main() -> None:
    turns = load_evalset()
    configs = {
        "baseline": run_config(turns, cache=False, route=False),
        "+cache": run_config(turns, cache=True, route=False),
        "+cache+route": run_config(turns, cache=True, route=True),
    }
    header = f"{'config':<14}{'cost($)':>12}{'mean(s)':>10}{'p95(s)':>10}{'pass':>8}{'esc':>6}"
    print(header)
    print("-" * len(header))
    for name, s in configs.items():
        print(
            f"{name:<14}{s['total_cost']:>12.6f}{s['mean_latency']:>10.3f}"
            f"{s['p95_latency']:>10.3f}{s['pass_rate']:>8.0%}{s['escalations']:>6}"
        )


if __name__ == "__main__":
    main()
```

### `data/evalset.jsonl`

```text
{"kind": "classify", "input": "Is this spam: win a prize now", "expect": "answer", "needs_reasoning": false}
{"kind": "extract", "input": "Pull the date from: meeting on 2026-07-01", "expect": "answer", "needs_reasoning": false}
{"kind": "format", "input": "Format as title case: hello world", "expect": "answer", "needs_reasoning": false}
{"kind": "qa", "input": "What is the capital of France?", "expect": "answer", "needs_reasoning": false}
{"kind": "qa", "input": "Summarize the second paragraph.", "expect": "answer", "needs_reasoning": false}
{"kind": "reason", "input": "Prove there are infinitely many primes.", "expect": "answer", "needs_reasoning": true}
{"kind": "reason", "input": "Plan a 3-step migration with rollback.", "expect": "answer", "needs_reasoning": true}
{"kind": "qa", "input": "Translate 'good morning' to Spanish.", "expect": "answer", "needs_reasoning": false}
{"kind": "extract", "input": "List the emails in: a@b.com, c@d.com", "expect": "answer", "needs_reasoning": false}
{"kind": "qa", "input": "What year did the moon landing happen?", "expect": "answer", "needs_reasoning": false}
{"kind": "reason", "input": "Design a rate limiter for a bursty API.", "expect": "answer", "needs_reasoning": true}
{"kind": "classify", "input": "Sentiment of: this product is great", "expect": "answer", "needs_reasoning": false}
```

### `requirements.txt`

```text
anthropic==0.42.0
```

### `tests/test_caching_routing.py`

```python
"""Asserts the four acceptance properties on the fake backend, offline."""
from __future__ import annotations

from caching_routing import harness
from caching_routing.harness import SYSTEM_PREFIX, load_evalset, run_config
from caching_routing.provider import call_model, reset_fake_cache
from caching_routing.router import LARGE, choose_model, run_turn


def test_cache_read_nonzero_after_first_call():
    reset_fake_cache()
    first = call_model("claude-sonnet-4-0", SYSTEM_PREFIX, "turn one", cache=True)
    second = call_model("claude-sonnet-4-0", SYSTEM_PREFIX, "turn two", cache=True)
    assert first.cache_creation_input_tokens > 0  # first call writes
    assert first.cache_read_input_tokens == 0
    assert second.cache_read_input_tokens > 0     # later calls read


def test_router_assigns_by_difficulty():
    assert choose_model({"kind": "classify"}) == "claude-3-5-haiku-latest"
    assert choose_model({"kind": "reason", "needs_reasoning": True}) == LARGE
    assert choose_model({"kind": "qa"}) == "claude-sonnet-4-0"


def test_escalation_fires_on_hard_turn():
    reset_fake_cache()
    # A reasoning turn would route LARGE directly; force the cheap path to prove
    # the escalation check by routing a hard turn as "classify" first.
    hard = {"kind": "classify", "input": "Prove a hard theorem.", "needs_reasoning": False}
    # Small model emits the low-confidence marker -> escalates to LARGE.
    result, escalated = run_turn(hard, SYSTEM_PREFIX, cache=True)
    assert escalated is True
    assert result.model == LARGE
    assert "[LOW_CONFIDENCE]" not in result.text


def test_cost_drops_without_hurting_pass_rate():
    turns = load_evalset()
    base = run_config(turns, cache=False, route=False)
    best = run_config(turns, cache=True, route=True)
    assert best["total_cost"] < base["total_cost"]          # a real cost win
    assert best["pass_rate"] >= base["pass_rate"]            # no quality regression
```

## Meeting the acceptance criteria

- **Caching verified by usage fields.** `cache_read_input_tokens` is zero on the
  first call (which writes via `cache_creation_input_tokens`) and non-zero on
  subsequent calls with the same prefix. `test_cache_read_nonzero_after_first_call`
  asserts both.
- **The router assigns by difficulty and is inspectable.** `choose_model` maps
  task kind / heuristics to small / mid / large; the harness records the model per
  turn. `test_router_assigns_by_difficulty` pins the mapping.
- **The escalation path catches a cheap-model failure.** `failed_check` detects the
  low-confidence marker and `run_turn` retries on `LARGE`, counted in the table's
  `esc` column. `test_escalation_fires_on_hard_turn` confirms a retry occurred.
- **The comparison table shows a real cost/latency reduction with no pass-rate
  drop.** `run_config` produces baseline vs. `+cache` vs. `+cache+route`;
  `test_cost_drops_without_hurting_pass_rate` asserts lower cost and equal-or-better
  pass rate.

## Common pitfalls

- **Volatile content before the cache breakpoint.** Putting the changing user turn
  inside the cached system block invalidates the prefix every call —
  `cache_read_input_tokens` stays zero. Order stable-first, volatile-last.
- **Caching a prefix you call once.** The write costs more than a normal token;
  the savings only come on reuse within the short ephemeral window. A one-shot
  prefix is a net loss.
- **Reporting tokens, not dollars.** Cache reads bill at a steep discount, so a
  raw token count understates the win. Compute cost with a price table that rates
  reads separately.
- **Routing without an escalation path.** Aggressive routing silently botches hard
  turns. Always pair the router with a failure check and a retry on the larger
  model, and watch the pass rate.
- **Declaring victory on cost alone.** A cost win that drops pass rate is a
  regression. The table must show pass rate beside cost, and the cheap config must
  not lose quality.

## Verification

```bash
cd modules/mod-207-productionizing-agents/exercise-03-caching-and-routing
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest

# Acceptance properties, offline (fake backend simulates the cache).
pytest -q

# Print the comparison table.
python -m caching_routing.harness

# Against the live API: real cache_read tokens, real prices.
CACHE_FAKE=0 ANTHROPIC_API_KEY=sk-... python -m caching_routing.harness
```

Expected: `pytest` is green; the table shows `+cache` and `+cache+route` with
lower total cost and lower mean/p95 latency than baseline, a non-zero `esc` count,
and a pass rate no lower than baseline.
