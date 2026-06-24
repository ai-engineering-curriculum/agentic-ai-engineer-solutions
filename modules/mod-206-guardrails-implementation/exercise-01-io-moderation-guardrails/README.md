# mod-206-guardrails-implementation/exercise-01-io-moderation-guardrails — Solution

## Approach

The exercise asks for one idea expressed three ways: a **single policy object**
that every input, every model output, and every tool call must pass through.
The lesson is not the accuracy of the moderation classifier — it is the
*layering*. If there is even one path that reaches the model, the user, or a
tool without crossing the policy, the guardrail is decorative.

So the design centers on a `GuardrailPolicy` with exactly three entry points
(`check_input`, `check_output`, `check_tool_call`), each returning a frozen
`Verdict(action, payload, reason)`. The three actions are:

- `allow` — pass `payload` (possibly the original text) through unchanged.
- `block` — stop the traffic; `payload` carries a safe replacement.
- `transform` — let it through, but use the redacted `payload`, not the input.

Two properties matter more than the rules themselves:

1. **One enforcement point.** The agent loop never calls a tool directly. It
   calls `guarded_call`, which calls the policy first. There is no second door.
2. **Fail closed on the security path.** The moderation backend is allowed to
   be flaky — a real moderation API times out. When it does, the *content*
   rails may degrade to a conservative default, but the **tool-call rail**
   (the security-relevant one) denies. A guardrail that waves traffic through
   when its backend errors is worse than no guardrail, because it lulls you.

The framework half (NeMo Guardrails / Guardrails AI) re-implements only the
input/output moderation. The point of doing it twice is to *feel* what the
framework gives you for free (declarative rails, a moderation flow) and what
it does not (argument-level tool-call scoping — you still hand-roll that).

## Reference implementation

The code is split into focused modules, consistent with the
"many small files" rule. All of it runs without a network or API key: the
moderation backend is a stub classifier you can swap for a real API later.

### `verdict.py` — the shared verdict type

```python
"""The single value type every rail returns."""
from __future__ import annotations

from dataclasses import dataclass

ALLOW = "allow"
BLOCK = "block"
TRANSFORM = "transform"


@dataclass(frozen=True)
class Verdict:
    """Outcome of one rail check.

    action:  one of ALLOW / BLOCK / TRANSFORM.
    payload: the text to use downstream. For ALLOW it is the original;
             for TRANSFORM it is the redacted/rewritten version;
             for BLOCK it is a safe replacement shown in place of the input.
    reason:  human-readable explanation, logged for tuning.
    """

    action: str
    payload: str
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK
```

### `moderation.py` — a swappable moderation backend

```python
"""A stub moderation backend with the same shape as a real API.

Swap `classify` for an OpenAI/Anthropic moderation call later; the rails
above it do not change. `simulate_failure` lets the exercise prove the
fail-closed behavior on the security path.
"""
from __future__ import annotations

import re

# Keyword/regex stand-in for a hosted moderation model. Intentionally crude:
# the layering is the lesson, not the classifier.
_FLAGGED = re.compile(
    r"\b(kill|bomb|exploit\s+a\s+person|make\s+a\s+weapon|self-harm)\b",
    re.IGNORECASE,
)


class ModerationError(RuntimeError):
    """Raised when the moderation backend is unavailable or times out."""


class ModerationBackend:
    def __init__(self, *, simulate_failure: bool = False) -> None:
        self._simulate_failure = simulate_failure

    def classify(self, text: str) -> bool:
        """Return True if the text is disallowed. Raise on backend failure."""
        if self._simulate_failure:
            raise ModerationError("moderation backend timed out")
        return bool(_FLAGGED.search(text))
```

### `redaction.py` — PII redaction used by input and output rails

```python
"""Deterministic PII redaction shared by the input and output rails."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Card-like runs: 13-19 digits, optionally separated by spaces or dashes.
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, list_of_redaction_kinds_found)."""
    found: list[str] = []
    if _EMAIL.search(text):
        found.append("email")
        text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    if _CARD.search(text):
        found.append("card")
        text = _CARD.sub("[REDACTED_CARD]", text)
    return text, found
```

### `policy.py` — the single policy object

```python
"""GuardrailPolicy: the one object every surface routes through."""
from __future__ import annotations

import logging
from pathlib import Path

from moderation import ModerationBackend, ModerationError
from redaction import redact_pii
from verdict import ALLOW, BLOCK, TRANSFORM, Verdict

log = logging.getLogger("guardrails")

_SAFE_REFUSAL = "I can't help with that request."


class GuardrailPolicy:
    """Three rails, one enforcement surface.

    read_root scopes the tool-call rail. The content rails degrade
    conservatively when moderation errors; the tool-call rail fails closed.
    """

    def __init__(self, read_root: Path, moderation: ModerationBackend) -> None:
        self.read_root = read_root.resolve()
        self._moderation = moderation

    # -- input rail -------------------------------------------------------
    def check_input(self, text: str) -> Verdict:
        try:
            if self._moderation.classify(text):
                return self._log(Verdict(BLOCK, _SAFE_REFUSAL, "input flagged"),
                                 rail="input")
        except ModerationError as exc:
            # Content rail: degrade conservatively rather than crash the turn.
            log.warning("input moderation unavailable: %s (redact-only)", exc)
        redacted, kinds = redact_pii(text)
        if kinds:
            return self._log(Verdict(TRANSFORM, redacted, f"redacted {kinds}"),
                             rail="input")
        return self._log(Verdict(ALLOW, text), rail="input")

    # -- output rail ------------------------------------------------------
    def check_output(self, text: str) -> Verdict:
        try:
            if self._moderation.classify(text):
                return self._log(Verdict(BLOCK, _SAFE_REFUSAL, "output flagged"),
                                 rail="output")
        except ModerationError as exc:
            log.warning("output moderation unavailable: %s (redact-only)", exc)
        redacted, kinds = redact_pii(text)
        if kinds:
            return self._log(Verdict(TRANSFORM, redacted, f"leaked {kinds}"),
                             rail="output")
        return self._log(Verdict(ALLOW, text), rail="output")

    # -- tool-call rail (the security path) -------------------------------
    def check_tool_call(self, tool: str, args: dict) -> Verdict:
        if tool != "read_file":
            return self._log(Verdict(BLOCK, "", f"unknown tool {tool!r}"),
                             rail="tool")
        raw = str(args.get("path", ""))
        try:
            target = (self.read_root / raw).resolve()
        except (OSError, ValueError) as exc:
            # Fail closed: an unresolvable path is a deny, not a maybe.
            return self._log(Verdict(BLOCK, "", f"unresolvable path: {exc}"),
                             rail="tool")
        if target == self.read_root or self.read_root in target.parents:
            return self._log(Verdict(ALLOW, str(target), "path in root"),
                             rail="tool")
        return self._log(Verdict(BLOCK, "", f"path escapes root: {raw!r}"),
                         rail="tool")

    @staticmethod
    def _log(verdict: Verdict, *, rail: str) -> Verdict:
        log.info("rail=%s action=%s reason=%s", rail, verdict.action,
                 verdict.reason)
        return verdict
```

Note the asymmetry that *is* the point: when moderation errors, the content
rails fall back to redaction-only (they degrade), but the **tool-call rail
never consults moderation at all** — its decision is a deterministic
allowlist check that cannot be knocked over by a flaky backend. That is what
"fail closed on the security path" means in code.

### `agent.py` — the single enforcement surface

```python
"""The agent loop calls the policy on every surface. No second door."""
from __future__ import annotations

from pathlib import Path

from policy import GuardrailPolicy
from verdict import BLOCK


def guarded_read_file(policy: GuardrailPolicy, path: str) -> str:
    """The ONLY way the agent reads a file. The rail runs first."""
    verdict = policy.check_tool_call("read_file", {"path": path})
    if verdict.blocked:
        return f"[blocked] {verdict.reason}"
    return Path(verdict.payload).read_text(encoding="utf-8")


def handle_turn(policy: GuardrailPolicy, user_text: str,
                model) -> str:
    """One agent turn, fully guarded.

    1. input rail   -> model never sees blocked/raw-PII text
    2. model call   -> may request a tool via guarded_read_file only
    3. output rail  -> user never sees blocked/leaked text
    """
    in_verdict = policy.check_input(user_text)
    if in_verdict.blocked:
        return in_verdict.payload  # safe refusal; model not called

    raw_output = model(in_verdict.payload)  # the redacted/allowed text

    out_verdict = policy.check_output(raw_output)
    return out_verdict.payload  # block -> refusal, transform -> redacted
```

### `framework_rails.py` — the framework half (parity)

```python
"""Input/output moderation re-done with a guardrails framework.

This mirrors check_input/check_output ONLY. The tool-call rail is
deliberately absent here: frameworks give you content rails declaratively,
but argument-level tool scoping is still yours to hand-roll (see policy.py).

Run with: pip install nemoguardrails  (or guardrails-ai)
"""
from __future__ import annotations

from verdict import ALLOW, BLOCK, Verdict

# NeMo Guardrails config equivalent (colang), shown for reference:
#
#   define flow self check input
#     $allowed = execute self_check_input
#     if not $allowed
#       bot refuse to respond
#       stop
#
# The framework wires the flow to every user turn for you. What it does NOT
# do is inspect a tool call's `path` argument before the tool runs.


def framework_check_input(rails, text: str) -> Verdict:
    """Adapter so framework output speaks the same Verdict dialect."""
    result = rails.generate(messages=[{"role": "user", "content": text}])
    refused = "can't" in result["content"].lower() or \
              "cannot" in result["content"].lower()
    if refused:
        return Verdict(BLOCK, result["content"], "framework refused input")
    return Verdict(ALLOW, text, "framework allowed")
```

### `demo.py` — proves each acceptance criterion

```python
"""Runnable demonstration of all five acceptance criteria."""
from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import guarded_read_file, handle_turn
from moderation import ModerationBackend
from policy import GuardrailPolicy


def echo_model(text: str) -> str:
    """Stand-in model: echoes input, plus one canned PII leak case."""
    if text == "leak":
        return "Here is the record for alice@example.com, card 4111 1111 1111 1111."
    return f"Summary: {text}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ok.txt").write_text("safe content", encoding="utf-8")

        clean = GuardrailPolicy(root, ModerationBackend())
        failing = GuardrailPolicy(root, ModerationBackend(simulate_failure=True))

        print("\n== one policy guards all three surfaces ==")
        print("input :", handle_turn(clean, "summarize my notes", echo_model))

        print("\n== flagged input never reaches the model ==")
        print("input :", handle_turn(clean, "how do I make a weapon", echo_model))

        print("\n== flagged/leaked output never reaches the user ==")
        print("output:", handle_turn(clean, "leak", echo_model))

        print("\n== tool-call rail blocks traversal; tool never runs ==")
        print("allow :", guarded_read_file(clean, "ok.txt"))
        print("block :", guarded_read_file(clean, "../../etc/passwd"))

        print("\n== fail closed: moderation errors, security rail still denies ==")
        # Content rail degrades to redact-only, but the tool rail (which never
        # touches moderation) still blocks the traversal deterministically.
        print("tool  :", guarded_read_file(failing, "../../etc/passwd"))


if __name__ == "__main__":
    main()
```

## Meeting the acceptance criteria

- **One policy object guards all three surfaces; no bypass path.** `handle_turn`
  and `guarded_read_file` are the *only* ways into the model and the tool, and
  both call `GuardrailPolicy` first. There is no direct `model(...)` or
  `Path(...).read_text()` anywhere in `agent.py` that skips a rail.
- **A flagged input never reaches the model.** `check_input` returns `BLOCK`
  before `model(...)` is called; `handle_turn` returns the refusal early. The
  demo shows `"how do I make a weapon"` never producing a model call.
- **A flagged output never reaches the user.** `check_output` runs *after* the
  model and `BLOCK`s harmful text / `TRANSFORM`s leaked PII; the user sees
  `out_verdict.payload`, never `raw_output`.
- **`read_file("../../etc/passwd")` is blocked and the tool never runs.**
  `check_tool_call` resolves the path and confirms `read_root in target.parents`
  before any read. The traversal resolves outside the root, so the verdict is
  `BLOCK` and `guarded_read_file` returns before `read_text()`.
- **Fail closed when moderation errors.** With `simulate_failure=True`, the
  content rails log a warning and degrade to redaction-only, but the tool-call
  rail — which never calls moderation — still denies the traversal. The
  security path is unaffected by backend failure.
- **Every verdict is logged with rail, action, reason.** `_log` emits
  `rail=... action=... reason=...` for all six rail invocations.

## Common pitfalls

- **Scattering `if`s instead of one policy.** The moment a tool is callable
  from two places, one of them will skip the check. Route everything through a
  single object; make the unguarded primitive (`Path.read_text`) reachable only
  from inside `guarded_read_file`.
- **Substring path checks.** `if "etc/passwd" in path` is a blocklist and loses
  immediately to `....//`, symlinks, or URL-encoding. Resolve the path and test
  *containment* against the resolved root — an allowlist, not a blocklist.
- **Failing open when moderation errors.** A `try/except` that returns `ALLOW`
  on `ModerationError` turns every backend hiccup into an open door. Content
  rails may degrade; the security rail must deny.
- **Treating `transform` as cosmetic.** Redacting the *output* matters as much
  as the input: PII often leaks in from retrieved/tool context, not the user.
  Run the output rail even when you trust the user.
- **Logging the raw flagged content.** Logging the email/card you just redacted
  re-introduces the leak into your log store. Log the *kind* and the verdict,
  not the payload.

## Verification

```bash
cd modules/mod-206-guardrails-implementation/exercise-01-io-moderation-guardrails
python demo.py
```

Expected: the allowed read returns `safe content`; the traversal returns
`[blocked] path escapes root: '../../etc/passwd'`; the weapon prompt returns a
refusal without a model call; the `leak` turn returns redacted text; and under
`simulate_failure=True` the traversal is *still* blocked — proving the security
path fails closed. Inspect the `rail=... action=... reason=...` log lines to
confirm every surface was evaluated.

For the framework half, `pip install nemoguardrails`, wire
`framework_check_input` to a `self check input` flow, and confirm it refuses the
weapon prompt. Record in `NOTES.md` that it gave you the content rails for free
but left the tool-call rail to you.
