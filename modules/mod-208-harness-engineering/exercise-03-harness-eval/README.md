# mod-208-harness-engineering/exercise-03-harness-eval — Solution

## Approach

The question is "did this harness change help?" One session where the agent
did well tells you very little. Agents are noisy: the same prompt in the same
repo can pass on one try and fail on the next. So the eval runs each task
several times under each harness, scores every trial the same way, and
reports the difference with an interval that shows how much of it could be
noise.

The pieces, in the order a trial runs:

1. **Tasks** (`tasks.py`). A JSON file (YAML if PyYAML is installed) lists
   tasks with `id`, `prompt`, `setup`, and `check`. The file is validated in
   full at load time. Every bad task is reported at once, by number and id,
   with the missing or empty field named. A typo fails in the first second,
   not twenty minutes into a paid run.
2. **Isolation** (`evaluate.run_trial`). Every trial gets its own temporary
   directory. The repo is copied in (`.git` included, so `git checkout`
   works), `setup` runs, then the harness files are copied over the result.
   The order matters: setup can reset the tree, so the harness goes on after
   it. When the trial ends, the directory is deleted. No trial can see
   another trial's edits, and the source repo is never touched.
3. **Runner** (`runners.py`, `claude_runner.py`). A `Runner` protocol with one
   method, `run(prompt, workdir, harness) -> TrialResult`. `FakeRunner` is
   seeded and offline. It "solves" a task by writing a `DONE` file, with a
   pass probability set per harness, so tests go through the real `check`
   path. `ClaudeCodeRunner` shells out to `claude -p ... --output-format json`
   in the workdir, enforces a wall-clock timeout, and parses turns, cost, and
   duration from the JSON.
4. **Scoring.** `check` runs in the workdir after the agent finishes. Exit
   code `0` is a pass; anything else is a fail. If the runner times out or
   raises, the trial is recorded with `passed: false` and an `error`, and the
   run moves on.
5. **Results** (`results.jsonl`). One JSON line per trial, flushed right away.
   Rerunning skips trials already in the file, so a crash costs only the
   trial in flight. If a crash left a half-written last line, the loader
   skips that line with a warning, and the writer starts a fresh line before
   appending.
6. **Report** (`report.py`). Per-harness pass rate (overall and per task),
   mean cost, mean turns, and error count. Then a percentile bootstrap 95%
   interval on `pass_rate(candidate) - pass_rate(baseline)`. If the interval
   contains zero, the report says "no clear difference".

Some choices worth calling out:

- **Errors count as failures in the pass rate.** A harness that makes the
  agent time out more often is worse, and the pass rate should show that.
  Errors are also counted separately, so you can tell "fails the check" from
  "never finished".
- **Mean cost and turns exclude errored trials.** A timed-out trial's cost is
  unknown, not zero. Averaging in zeros would make a flaky harness look cheap.
- **The harness loop is innermost.** Trial 0 of task A runs under baseline,
  then under v2, then trial 0 of task B, and so on. If the API slows down or
  the model changes mid-run, both harnesses are affected about equally.
- **Zero dependencies.** The bootstrap uses `random.Random(seed)` from the
  standard library, so the report is reproducible and needs no numpy. PyYAML
  is optional.
- **Immutable records.** `Task`, `TrialResult`, `TrialRecord`, `EvalConfig`,
  and the summary types are frozen dataclasses. A record is written once and
  never changed.

## Reference implementation

Layout:

```text
exercise-03-harness-eval/
├── harness_eval/
│   ├── __init__.py
│   ├── __main__.py        # python -m harness_eval
│   ├── cli.py             # run / report subcommands
│   ├── tasks.py           # Task + validated loading
│   ├── runners.py         # TrialResult, Runner protocol, FakeRunner
│   ├── claude_runner.py   # ClaudeCodeRunner + JSON parsing
│   ├── evaluate.py        # isolation, scoring, JSONL, resume
│   └── report.py          # summaries, bootstrap CI, tables
├── harnesses/
│   ├── baseline/          # empty on purpose (commit a .gitkeep)
│   └── v2/
│       └── AGENTS.md      # add .claude/settings.json + hooks from exercise-02
├── examples/
│   ├── tasks.json         # live template: three tasks on the exercise-01 repo
│   ├── fake-tasks.json    # offline demo tasks scored by `test -f DONE`
│   └── demo-repo/
│       └── README.md
├── tests/
│   ├── conftest.py
│   ├── test_tasks.py
│   ├── test_evaluate.py
│   └── test_report.py
├── pyproject.toml
└── requirements.txt
```

### `harness_eval/__init__.py`

```python
"""Harness eval: did this harness change help? Answer with numbers."""
```

### `harness_eval/__main__.py`

```python
import sys

from .cli import main

sys.exit(main())
```

### `harness_eval/tasks.py`

```python
"""Task file loading and validation.

A task file is JSON (or YAML, if PyYAML is installed): either a list of tasks
or an object with a "tasks" list. Every task needs id, prompt, setup, check,
all non-empty strings. Validation happens once, at load time, so a bad file
fails before any trial runs and the error names the task and the field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = ("id", "prompt", "setup", "check")


class TaskFileError(ValueError):
    """The task file is missing, unreadable, or has invalid tasks."""


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    setup: str
    check: str


def load_tasks(path: Path) -> tuple[Task, ...]:
    data = _read_file(path)
    entries = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise TaskFileError(
            f'{path}: expected a non-empty list of tasks, or {{"tasks": [...]}}'
        )
    tasks: list[Task] = []
    errors: list[str] = []
    for index, entry in enumerate(entries, start=1):
        problem = _problem_with(entry, {t.id for t in tasks})
        if problem:
            errors.append(f"{_label(index, entry)}: {problem}")
        else:
            tasks.append(Task(**{name: entry[name] for name in REQUIRED_FIELDS}))
    if errors:
        details = "\n  ".join(errors)
        raise TaskFileError(f"{path} has {len(errors)} invalid task(s):\n  {details}")
    return tuple(tasks)


def _problem_with(entry: Any, seen_ids: set[str]) -> str | None:
    if not isinstance(entry, dict):
        return "must be an object with fields " + ", ".join(REQUIRED_FIELDS)
    missing = [name for name in REQUIRED_FIELDS if name not in entry]
    if missing:
        return "missing field(s): " + ", ".join(missing)
    bad = [
        name
        for name in REQUIRED_FIELDS
        if not isinstance(entry[name], str) or not entry[name].strip()
    ]
    if bad:
        return "field(s) must be non-empty strings: " + ", ".join(bad)
    if entry["id"] in seen_ids:
        return f"duplicate id {entry['id']!r}"
    return None


def _label(index: int, entry: Any) -> str:
    task_id = entry.get("id") if isinstance(entry, dict) else None
    return f"task #{index}" + (f" (id={task_id!r})" if isinstance(task_id, str) else "")


def _read_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskFileError(f"cannot read task file {path}: {exc.strerror}") from exc
    if path.suffix in (".yaml", ".yml"):
        return _parse_yaml(text, path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskFileError(
            f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}"
        ) from exc


def _parse_yaml(text: str, path: Path) -> Any:
    try:
        import yaml  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise TaskFileError(
            f"{path}: reading YAML needs PyYAML (pip install pyyaml); "
            "or convert the file to JSON"
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaskFileError(f"{path}: invalid YAML: {exc}") from exc
```

### `harness_eval/runners.py`

```python
"""The Runner interface, plus a deterministic fake for tests and demos.

A runner takes a prompt, a prepared working directory, and the harness
directory, lets an agent work in that directory, and reports turns, cost, and
duration. It does not judge the result; the task's check command does that.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

FAKE_COST_PER_TURN_USD = 0.012
FAKE_SECONDS_PER_TURN = 1.5


@dataclass(frozen=True)
class TrialResult:
    turns: int
    cost_usd: float
    duration_s: float
    error: str | None = None


class Runner(Protocol):
    def run(self, prompt: str, workdir: Path, harness: Path) -> TrialResult: ...


class FakeRunner:
    """Simulated agent. No network, repeatable for a given seed.

    For each call it "solves" the task (writes `solve_file` into the workdir)
    with the probability configured for that harness name. Tasks whose check
    is `test -f DONE` then pass or fail through the real scoring path.

    `crash_prompts` raise an exception; `timeout_prompts` return a timeout
    error. Both let tests prove a bad trial is recorded, not dropped.
    """

    def __init__(
        self,
        pass_prob: Mapping[str, float],
        seed: int = 0,
        solve_file: str = "DONE",
        crash_prompts: frozenset[str] = frozenset(),
        timeout_prompts: frozenset[str] = frozenset(),
    ) -> None:
        for name, prob in pass_prob.items():
            if not 0.0 <= prob <= 1.0:
                raise ValueError(f"pass probability for {name!r} must be in [0, 1]")
        self._pass_prob = dict(pass_prob)
        self._seed = seed
        self._solve_file = solve_file
        self._crash = crash_prompts
        self._timeout = timeout_prompts
        self._counts: dict[tuple[str, str], int] = {}

    @property
    def calls(self) -> int:
        return sum(self._counts.values())

    def run(self, prompt: str, workdir: Path, harness: Path) -> TrialResult:
        if harness.name not in self._pass_prob:
            raise ValueError(f"FakeRunner has no pass probability for {harness.name!r}")
        key = (harness.name, prompt)
        attempt = self._counts.get(key, 0)
        self._counts[key] = attempt + 1
        if prompt in self._crash:
            raise RuntimeError("fake agent crashed")
        if prompt in self._timeout:
            return TrialResult(0, 0.0, 0.0, error="timeout (simulated)")
        # A string seed is hashed with SHA-512, so this is stable across runs
        # and independent of call order between different (harness, prompt).
        rng = random.Random(f"{self._seed}:{harness.name}:{prompt}:{attempt}")
        turns = rng.randint(3, 15)
        if rng.random() < self._pass_prob[harness.name]:
            (workdir / self._solve_file).write_text("solved\n", encoding="utf-8")
        return TrialResult(
            turns=turns,
            cost_usd=round(turns * FAKE_COST_PER_TURN_USD, 4),
            duration_s=turns * FAKE_SECONDS_PER_TURN,
        )
```

`FakeRunner` derives a fresh RNG for every call from
`seed:harness:prompt:attempt`. Python hashes string seeds with SHA-512, so
results are the same on every machine and every run. They also don't depend
on the order in which different tasks happen to run.

### `harness_eval/claude_runner.py`

```python
"""Runner that drives Claude Code headlessly.

Invocation: claude -p PROMPT --output-format json [--max-turns N]
                [--max-budget-usd X]
The JSON result is expected to carry num_turns, total_cost_usd, duration_ms,
and is_error. Flags and field names change between versions: check them
against `claude --help` and one real run before trusting the numbers.
Set max_turns or max_budget_usd to None to leave that flag off.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runners import TrialResult


@dataclass(frozen=True)
class ClaudeCodeRunner:
    max_turns: int | None = 30
    max_budget_usd: float | None = None
    timeout_s: float = 900.0
    executable: str = "claude"
    extra_args: tuple[str, ...] = ()

    def command(self, prompt: str) -> list[str]:
        cmd = [self.executable, "-p", prompt, "--output-format", "json"]
        if self.max_turns is not None:
            cmd += ["--max-turns", str(self.max_turns)]
        if self.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return [*cmd, *self.extra_args]

    def run(self, prompt: str, workdir: Path, harness: Path) -> TrialResult:
        # The harness files are already in workdir; Claude Code picks up
        # AGENTS.md / CLAUDE.md and .claude/settings.json from its cwd.
        start = time.monotonic()
        try:
            proc = subprocess.run(
                self.command(prompt),
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return TrialResult(
                0, 0.0, elapsed, error=f"timeout after {self.timeout_s}s"
            )
        except FileNotFoundError:
            return TrialResult(
                0, 0.0, 0.0, error=f"{self.executable!r} not found on PATH"
            )
        elapsed = time.monotonic() - start
        return parse_claude_output(proc.stdout, proc.returncode, proc.stderr, elapsed)


def parse_claude_output(
    stdout: str, returncode: int, stderr: str, elapsed_s: float
) -> TrialResult:
    try:
        data: Any = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip()[-300:]
        return TrialResult(
            0, 0.0, elapsed_s, error=f"unparseable output (exit {returncode}): {detail}"
        )
    if not isinstance(data, dict):
        return TrialResult(0, 0.0, elapsed_s, error="output JSON is not an object")
    turns = int(data.get("num_turns") or 0)
    cost = float(data.get("total_cost_usd") or 0.0)
    duration_ms = data.get("duration_ms")
    duration = duration_ms / 1000 if isinstance(duration_ms, int | float) else elapsed_s
    error = None
    if data.get("is_error") or returncode != 0:
        error = f"agent error: {data.get('subtype', 'unknown')} (exit {returncode})"
    return TrialResult(turns, cost, duration, error)
```

Check the flags and fields against your installed version before a live run.
On Claude Code 2.1.x, `claude --help` lists `--output-format` and
`--max-budget-usd` but not `--max-turns`. Older docs describe `--max-turns`
for print mode. If your version rejects it, pass `--max-turns 0` to leave it
off and rely on `--max-budget-usd` plus the wall-clock timeout. Run
`claude -p "say hi" --output-format json` once and confirm that `num_turns`,
`total_cost_usd`, `duration_ms`, and `is_error` are present. If a field is
renamed, `parse_claude_output` silently reads `0` for it, so you would see
zero cost and zero turns in the report.

### `harness_eval/evaluate.py`

```python
"""Run harness x task x trial, score with the task's check, append to JSONL.

Each trial gets a fresh copy of the repo in its own temp directory:
copy repo -> run setup -> copy harness files over it -> run agent -> run check.
Trials never share a directory, so one trial's edits can't leak into another.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, TextIO

from .runners import Runner, TrialResult
from .tasks import Task

log = logging.getLogger(__name__)
OUTPUT_TAIL_CHARS = 500


@dataclass(frozen=True)
class EvalConfig:
    repo: Path
    results_path: Path
    trials: int = 5
    command_timeout_s: float = 300.0


@dataclass(frozen=True)
class TrialRecord:
    harness: str
    task_id: str
    trial: int
    passed: bool
    turns: int
    cost_usd: float
    duration_s: float
    error: str | None

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.harness, self.task_id, self.trial)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrialRecord:
        names = {f.name for f in fields(cls)}
        if set(data) != names:
            raise ValueError(f"expected keys {sorted(names)}, got {sorted(data)}")
        return cls(**data)


@dataclass(frozen=True)
class ShellOutcome:
    ok: bool
    detail: str
    timed_out: bool = False


def run_shell(command: str, cwd: Path, timeout_s: float) -> ShellOutcome:
    """Run a task-file command (trusted operator config) through the shell."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ShellOutcome(False, f"timed out after {timeout_s}s", timed_out=True)
    tail = (proc.stdout + proc.stderr).strip()[-OUTPUT_TAIL_CHARS:]
    return ShellOutcome(proc.returncode == 0, f"exit {proc.returncode}: {tail}")


def _record(
    harness: Path, task: Task, trial: int, result: TrialResult, passed: bool
) -> TrialRecord:
    return TrialRecord(
        harness=harness.name,
        task_id=task.id,
        trial=trial,
        passed=passed,
        turns=result.turns,
        cost_usd=result.cost_usd,
        duration_s=result.duration_s,
        error=result.error,
    )


def _safe_run(runner: Runner, prompt: str, workdir: Path, harness: Path) -> TrialResult:
    try:
        return runner.run(prompt, workdir, harness)
    except Exception as exc:  # noqa: BLE001 - a crash is data, record it
        return TrialResult(
            0, 0.0, 0.0, error=f"runner crashed: {type(exc).__name__}: {exc}"
        )


def run_trial(
    task: Task, harness: Path, runner: Runner, config: EvalConfig, trial: int
) -> TrialRecord:
    with tempfile.TemporaryDirectory(prefix="harness-eval-") as tmp:
        workdir = Path(tmp) / "repo"
        shutil.copytree(config.repo, workdir, symlinks=True)
        setup = run_shell(task.setup, workdir, config.command_timeout_s)
        if not setup.ok:
            failed = TrialResult(0, 0.0, 0.0, error=f"setup failed: {setup.detail}")
            return _record(harness, task, trial, failed, passed=False)
        shutil.copytree(harness, workdir, dirs_exist_ok=True)
        result = _safe_run(runner, task.prompt, workdir, harness)
        if result.error is not None:
            return _record(harness, task, trial, result, passed=False)
        check = run_shell(task.check, workdir, config.command_timeout_s)
        if check.timed_out:
            result = TrialResult(
                result.turns,
                result.cost_usd,
                result.duration_s,
                f"check {check.detail}",
            )
        return _record(harness, task, trial, result, passed=check.ok)


def load_records(path: Path) -> tuple[TrialRecord, ...]:
    """Read results.jsonl, skipping (and logging) lines a crash left broken."""
    if not path.exists():
        return ()
    records: list[TrialRecord] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(TrialRecord.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("%s:%d: skipping unreadable line (%s)", path, number, exc)
    return tuple(records)


def _validate_inputs(harnesses: Sequence[Path], config: EvalConfig) -> None:
    if not config.repo.is_dir():
        raise ValueError(f"repo {config.repo} is not a directory")
    if config.trials < 1:
        raise ValueError("trials must be at least 1")
    for harness in harnesses:
        if not harness.is_dir():
            raise ValueError(f"harness {harness} is not a directory")
    names = [h.name for h in harnesses]
    if len(set(names)) != len(names):
        raise ValueError(f"harness directory names must be unique, got {names}")


def _open_for_append(path: Path) -> TextIO:
    """Open results for append, ending any half-written last line first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = path.exists() and path.stat().st_size > 0
    if needs_newline:
        with path.open("rb") as existing:
            existing.seek(-1, 2)
            needs_newline = existing.read(1) != b"\n"
    handle = path.open("a", encoding="utf-8")
    if needs_newline:
        handle.write("\n")
    return handle


def run_eval(
    tasks: Sequence[Task],
    harnesses: Sequence[Path],
    runner: Runner,
    config: EvalConfig,
) -> tuple[TrialRecord, ...]:
    """Run every missing trial and return the new records.

    Trials already in results_path are skipped, so rerunning resumes a run.
    The harness loop is innermost so both harnesses see the same conditions
    (API load, model version) at roughly the same time.
    """
    _validate_inputs(harnesses, config)
    done = {record.key for record in load_records(config.results_path)}
    new: list[TrialRecord] = []
    with _open_for_append(config.results_path) as out:
        for trial in range(config.trials):
            for task in tasks:
                for harness in harnesses:
                    if (harness.name, task.id, trial) in done:
                        continue
                    record = run_trial(task, harness, runner, config, trial)
                    out.write(record.to_json() + "\n")
                    out.flush()
                    new.append(record)
    return tuple(new)
```

`setup` and `check` run through the shell because they are shell commands by
design (`git checkout <sha>`, `make check && grep ...`). The task file is
code, so treat it like code: review it, and don't load task files from
people you don't trust.

### `harness_eval/report.py`

```python
"""Summaries and a bootstrap interval on the pass-rate difference.

Pass rate counts every trial, errors included (an error is a failure).
Mean cost and turns use only trials without errors, because an errored
trial's cost is unknown, not zero.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from .evaluate import TrialRecord

DEFAULT_BOOTSTRAP = 10_000
CI_LEVEL = 0.95


@dataclass(frozen=True)
class HarnessSummary:
    name: str
    trials: int
    pass_rate: float
    per_task: tuple[tuple[str, float], ...]
    mean_cost_usd: float | None
    mean_turns: float | None
    errors: int


@dataclass(frozen=True)
class Comparison:
    baseline: str
    candidate: str
    diff: float
    low: float
    high: float

    @property
    def verdict(self) -> str:
        if self.low > 0:
            return f"{self.candidate} is better"
        if self.high < 0:
            return f"{self.baseline} is better"
        return "no clear difference"


def _rate(outcomes: Sequence[bool]) -> float:
    return sum(outcomes) / len(outcomes)


def _mean_or_none(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


def summarize_harness(name: str, records: Sequence[TrialRecord]) -> HarnessSummary:
    mine = [r for r in records if r.harness == name]
    if not mine:
        raise ValueError(f"no trials recorded for harness {name!r}")
    task_ids = list(dict.fromkeys(r.task_id for r in mine))
    per_task = tuple(
        (tid, _rate([r.passed for r in mine if r.task_id == tid])) for tid in task_ids
    )
    clean = [r for r in mine if r.error is None]
    return HarnessSummary(
        name=name,
        trials=len(mine),
        pass_rate=_rate([r.passed for r in mine]),
        per_task=per_task,
        mean_cost_usd=_mean_or_none([r.cost_usd for r in clean]),
        mean_turns=_mean_or_none([r.turns for r in clean]),
        errors=len(mine) - len(clean),
    )


def bootstrap_diff_ci(
    baseline: Sequence[bool],
    candidate: Sequence[bool],
    n_boot: int = DEFAULT_BOOTSTRAP,
    level: float = CI_LEVEL,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Return (observed diff, low, high) for pass_rate(candidate) - pass_rate(baseline).

    Percentile bootstrap: resample each group's trials with replacement,
    recompute the difference, and take the middle `level` of the results.
    """
    if not baseline or not candidate:
        raise ValueError("both groups need at least one trial")
    rng = random.Random(seed)
    a, b = list(baseline), list(candidate)
    diffs = sorted(
        _rate(rng.choices(b, k=len(b))) - _rate(rng.choices(a, k=len(a)))
        for _ in range(n_boot)
    )
    tail = (1 - level) / 2
    low = diffs[int(tail * n_boot)]
    high = diffs[min(n_boot - 1, int((1 - tail) * n_boot))]
    return _rate(b) - _rate(a), low, high


def compare(
    records: Sequence[TrialRecord],
    baseline: str,
    candidate: str,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = 0,
) -> Comparison:
    a = [r.passed for r in records if r.harness == baseline]
    b = [r.passed for r in records if r.harness == candidate]
    diff, low, high = bootstrap_diff_ci(a, b, n_boot=n_boot, seed=seed)
    return Comparison(baseline, candidate, diff, low, high)


def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def _summary_table(summaries: Sequence[HarnessSummary]) -> list[str]:
    header = f"{'harness':<16}{'trials':>7}{'pass':>8}{'mean $':>10}{'turns':>8}"
    lines = [header + f"{'errors':>8}"]
    for s in summaries:
        cost = _fmt(s.mean_cost_usd, ".4f")
        turns = _fmt(s.mean_turns, ".1f")
        lines.append(
            f"{s.name:<16}{s.trials:>7}{s.pass_rate:>8.1%}"
            f"{cost:>10}{turns:>8}{s.errors:>8}"
        )
    return lines


def _per_task_table(summaries: Sequence[HarnessSummary]) -> list[str]:
    task_ids = list(dict.fromkeys(tid for s in summaries for tid, _ in s.per_task))
    lines = [f"{'task':<24}" + "".join(f"{s.name:>16}" for s in summaries)]
    for tid in task_ids:
        cells = []
        for s in summaries:
            rate = dict(s.per_task).get(tid)
            cells.append(f"{_fmt(rate, '.1%'):>16}")
        lines.append(f"{tid:<24}" + "".join(cells))
    return lines


def format_report(
    records: Sequence[TrialRecord], n_boot: int = DEFAULT_BOOTSTRAP, seed: int = 0
) -> str:
    """Tables for every harness, then each later harness vs the first one."""
    if not records:
        return "no trials recorded"
    names = list(dict.fromkeys(r.harness for r in records))
    summaries = [summarize_harness(name, records) for name in names]
    lines = [
        *_summary_table(summaries),
        "",
        "pass rate by task",
        *_per_task_table(summaries),
    ]
    for candidate in names[1:]:
        c = compare(records, names[0], candidate, n_boot=n_boot, seed=seed)
        lines += [
            "",
            f"{c.candidate} vs {c.baseline}: pass-rate difference {c.diff:+.1%}, "
            f"{CI_LEVEL:.0%} CI [{c.low:+.1%}, {c.high:+.1%}] -> {c.verdict}",
        ]
    return "\n".join(lines)
```

### `harness_eval/cli.py`

```python
"""Command line: `python -m harness_eval run ...` and `... report ...`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .claude_runner import ClaudeCodeRunner
from .evaluate import EvalConfig, load_records, run_eval
from .report import format_report
from .runners import FakeRunner, Runner
from .tasks import TaskFileError, load_tasks


def _parse_fake_pass(items: Sequence[str]) -> dict[str, float]:
    probs: dict[str, float] = {}
    for item in items:
        name, _, value = item.partition("=")
        try:
            prob = float(value)
        except ValueError:
            prob = -1.0
        if not name or not 0.0 <= prob <= 1.0:
            raise ValueError(
                f"--fake-pass expects NAME=PROB with PROB in [0, 1], got {item!r}"
            )
        probs[name] = prob
    return probs


def _build_runner(args: argparse.Namespace, harnesses: Sequence[Path]) -> Runner:
    if args.runner == "claude":
        return ClaudeCodeRunner(
            max_turns=args.max_turns or None,
            max_budget_usd=args.max_budget_usd,
            timeout_s=args.timeout,
            extra_args=tuple(args.claude_arg),
        )
    probs = _parse_fake_pass(args.fake_pass)
    missing = [h.name for h in harnesses if h.name not in probs]
    if missing:
        raise ValueError(f"--fake-pass is missing harness(es): {', '.join(missing)}")
    return FakeRunner(probs, seed=args.seed)


def _cmd_run(args: argparse.Namespace) -> int:
    harnesses = [Path(h) for h in args.harness]
    tasks = load_tasks(Path(args.tasks))
    runner = _build_runner(args, harnesses)
    config = EvalConfig(
        repo=Path(args.repo),
        results_path=Path(args.results),
        trials=args.trials,
        command_timeout_s=args.command_timeout,
    )
    new = run_eval(tasks, harnesses, runner, config)
    print(f"ran {len(new)} new trial(s); results in {config.results_path}\n")
    print(format_report(load_records(config.results_path), seed=args.seed))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    print(format_report(load_records(Path(args.results)), seed=args.seed))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run trials and print the report")
    run.add_argument("--tasks", required=True, help="tasks.json (or .yaml with PyYAML)")
    run.add_argument("--repo", required=True, help="repo copied fresh for every trial")
    run.add_argument(
        "--harness",
        action="append",
        required=True,
        help="harness directory; repeat. The first is the baseline.",
    )
    run.add_argument("--trials", type=int, default=5)
    run.add_argument("--results", default="results.jsonl")
    run.add_argument("--runner", choices=("fake", "claude"), default="fake")
    run.add_argument("--fake-pass", action="append", default=[], metavar="NAME=PROB")
    run.add_argument("--max-turns", type=int, default=30, help="0 leaves the flag off")
    run.add_argument(
        "--max-budget-usd", type=float, default=None, help="per-trial spend cap"
    )
    run.add_argument("--timeout", type=float, default=900.0, help="agent wall clock, s")
    run.add_argument(
        "--command-timeout", type=float, default=300.0, help="setup/check timeout, s"
    )
    run.add_argument(
        "--claude-arg",
        action="append",
        default=[],
        help="extra arg passed to claude; repeat",
    )
    run.add_argument("--seed", type=int, default=0)
    run.set_defaults(func=_cmd_run)
    report = sub.add_parser("report", help="print the report for a results file")
    report.add_argument("--results", default="results.jsonl")
    report.add_argument("--seed", type=int, default=0)
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (TaskFileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

### `harnesses/v2/AGENTS.md`

```markdown
# Agent instructions

- Run `make check` before you say you are done. If it fails, fix it.
- Change source files, not tests, unless the task asks for a new test.
- Keep diffs small. Don't reformat files you didn't otherwise change.
```

### `examples/tasks.json`

```json
{
  "tasks": [
    {
      "id": "fix-off-by-one",
      "prompt": "The pagination test in tests/test_pages.py fails. Find the bug in src/pages.py and fix it. Do not edit the tests.",
      "setup": "git checkout --quiet task/fix-off-by-one",
      "check": "make check"
    },
    {
      "id": "add-health-endpoint",
      "prompt": "Add a GET /healthz endpoint that returns {\"ok\": true}. Add a test for it.",
      "setup": "git checkout --quiet task/add-health-endpoint",
      "check": "make check && grep -rq healthz tests/"
    },
    {
      "id": "rename-config-key",
      "prompt": "Rename the config key `timeout` to `timeout_s` everywhere, including docs. Keep backward compatibility by reading the old key with a deprecation warning.",
      "setup": "git checkout --quiet task/rename-config-key",
      "check": "make check && ! grep -rqw 'timeout' docs/"
    }
  ]
}
```

This template assumes the exercise-01 repo has a `task/<id>` tag (or branch)
at each task's starting state. Replace the prompts and checks with your own.
A good `check` fails before the task is done and passes only when the task is
done.

### `examples/fake-tasks.json`

```json
[
  {"id": "task-a", "prompt": "Solve task A.", "setup": "rm -f DONE", "check": "test -f DONE"},
  {"id": "task-b", "prompt": "Solve task B.", "setup": "rm -f DONE", "check": "test -f DONE"},
  {"id": "task-c", "prompt": "Solve task C.", "setup": "rm -f DONE", "check": "test -f DONE"}
]
```

### `examples/demo-repo/README.md`

```markdown
# demo-repo

A stand-in repository for offline runs with the fake runner. Each trial gets
a fresh copy of this directory.
```

### `pyproject.toml`

```toml
[project]
name = "harness-eval"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

### `requirements.txt`

```text
pytest>=8.0
# Optional: only needed for tasks.yaml. JSON task files need nothing extra.
# pyyaml>=6.0
```

### `tests/conftest.py`

```python
"""Fixtures: a tiny repo, two harnesses, three tasks scored by `test -f DONE`."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_eval.evaluate import EvalConfig
from harness_eval.tasks import Task


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n")
    return root


@pytest.fixture
def harnesses(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "harnesses" / "baseline"
    v2 = tmp_path / "harnesses" / "v2"
    baseline.mkdir(parents=True)
    v2.mkdir(parents=True)
    (v2 / "AGENTS.md").write_text("Run the checks before finishing.\n")
    return baseline, v2


@pytest.fixture
def tasks() -> tuple[Task, ...]:
    return tuple(
        Task(
            id=f"task-{n}",
            prompt=f"Solve {n}.",
            setup="rm -f DONE",
            check="test -f DONE",
        )
        for n in ("a", "b", "c")
    )


@pytest.fixture
def config(repo: Path, tmp_path: Path) -> EvalConfig:
    return EvalConfig(
        repo=repo, results_path=tmp_path / "out" / "results.jsonl", trials=5
    )
```

### `tests/test_tasks.py`

```python
"""Task file validation: bad files fail at load time with a clear message."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_eval.tasks import Task, TaskFileError, load_tasks

VALID = {"id": "t1", "prompt": "do it", "setup": "true", "check": "true"}


def write(tmp_path: Path, data: object, name: str = "tasks.json") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_loads_list(tmp_path):
    assert load_tasks(write(tmp_path, [VALID])) == (Task(**VALID),)


def test_loads_tasks_wrapper_and_ignores_extra_fields(tmp_path):
    path = write(tmp_path, {"tasks": [{**VALID, "notes": "extra"}]})
    assert load_tasks(path) == (Task(**VALID),)


def test_missing_field_names_task_and_field(tmp_path):
    bad = {k: v for k, v in VALID.items() if k != "check"}
    with pytest.raises(
        TaskFileError, match=r"task #1 \(id='t1'\): missing field\(s\): check"
    ):
        load_tasks(write(tmp_path, [bad]))


def test_reports_every_bad_task_at_once(tmp_path):
    entries = [
        VALID,
        {"id": "t2"},
        "not an object",
        {**VALID, "id": "t4", "prompt": "  "},
    ]
    with pytest.raises(TaskFileError) as info:
        load_tasks(write(tmp_path, entries))
    message = str(info.value)
    assert "3 invalid task(s)" in message
    assert "task #2 (id='t2'): missing field(s): prompt, setup, check" in message
    assert "task #3: must be an object" in message
    assert "task #4 (id='t4'): field(s) must be non-empty strings: prompt" in message


def test_non_string_field_rejected(tmp_path):
    with pytest.raises(TaskFileError, match="non-empty strings: check"):
        load_tasks(write(tmp_path, [{**VALID, "check": 0}]))


def test_duplicate_id_rejected(tmp_path):
    with pytest.raises(TaskFileError, match="duplicate id 't1'"):
        load_tasks(write(tmp_path, [VALID, VALID]))


@pytest.mark.parametrize("data", [[], {}, {"tasks": "nope"}, 42])
def test_wrong_shape_rejected(tmp_path, data):
    with pytest.raises(TaskFileError, match="non-empty list of tasks"):
        load_tasks(write(tmp_path, data))


def test_invalid_json_is_a_task_file_error(tmp_path):
    with pytest.raises(TaskFileError, match="invalid JSON at line 1"):
        load_tasks(write(tmp_path, "{not json"))


def test_missing_file_is_a_task_file_error(tmp_path):
    with pytest.raises(TaskFileError, match="cannot read task file"):
        load_tasks(tmp_path / "nope.json")


def test_yaml_without_pyyaml_explains_fix(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` fail
    with pytest.raises(TaskFileError, match="needs PyYAML"):
        load_tasks(write(tmp_path, "tasks: []", name="tasks.yaml"))


def test_yaml_loads_when_available(tmp_path):
    pytest.importorskip("yaml")
    text = "tasks:\n  - {id: t1, prompt: do it, setup: 'true', check: 'true'}\n"
    assert load_tasks(write(tmp_path, text, name="tasks.yaml")) == (Task(**VALID),)
```

### `tests/test_evaluate.py`

```python
"""The trial loop: isolation, scoring by exit code, JSONL, resume, errors."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness_eval.evaluate import load_records, run_eval
from harness_eval.runners import FakeRunner, TrialResult
from harness_eval.tasks import Task


class SpyRunner(FakeRunner):
    """FakeRunner that records what each trial's workdir looked like on entry."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[dict[str, bool]] = []

    def run(self, prompt: str, workdir: Path, harness: Path) -> TrialResult:
        self.seen.append(
            {
                "leak": (workdir / "LEAK").exists(),
                "agents_md": (workdir / "AGENTS.md").exists(),
                "v2": harness.name == "v2",
            }
        )
        (workdir / "LEAK").write_text("left behind by an earlier trial")
        (workdir / "app.py").write_text("clobbered\n")
        return super().run(prompt, workdir, harness)


def test_trials_are_isolated_and_harness_applied(tasks, harnesses, config, repo):
    runner = SpyRunner({"baseline": 0.5, "v2": 0.5})
    run_eval(tasks, harnesses, runner, config)
    assert len(runner.seen) == 2 * 3 * 5
    assert not any(s["leak"] for s in runner.seen)  # no trial sees another's files
    assert all(
        s["agents_md"] == s["v2"] for s in runner.seen
    )  # harness files only in v2
    assert (repo / "app.py").read_text() == "print('hi')\n"  # source repo untouched
    assert not (repo / "LEAK").exists()


def test_harness_is_applied_after_setup(harnesses, config):
    task = Task(id="t", prompt="p", setup="rm -f AGENTS.md", check="true")
    runner = SpyRunner({"baseline": 1.0, "v2": 1.0})
    run_eval([task], harnesses, runner, replace(config, trials=1))
    assert [s["agents_md"] for s in runner.seen] == [False, True]


@pytest.mark.parametrize("prob, expected", [(1.0, True), (0.0, False)])
def test_scored_by_check_exit_code(tasks, harnesses, config, prob, expected):
    runner = FakeRunner({"baseline": prob, "v2": prob})
    records = run_eval(tasks, harnesses, runner, config)
    assert {r.passed for r in records} == {expected}


def test_failing_check_fails_even_if_agent_succeeds(harnesses, config):
    task = Task(id="t", prompt="p", setup="true", check="exit 3")
    records = run_eval([task], harnesses, FakeRunner({"baseline": 1, "v2": 1}), config)
    assert not any(r.passed for r in records)
    assert all(r.error is None for r in records)


def test_jsonl_has_one_line_per_trial(tasks, harnesses, config):
    run_eval(tasks, harnesses, FakeRunner({"baseline": 0.3, "v2": 0.8}), config)
    lines = config.results_path.read_text().splitlines()
    assert len(lines) == 2 * 3 * 5
    first = json.loads(lines[0])
    assert set(first) == {
        "harness", "task_id", "trial", "passed",
        "turns", "cost_usd", "duration_s", "error",
    }  # fmt: skip
    keys = {(r.harness, r.task_id, r.trial) for r in load_records(config.results_path)}
    assert len(keys) == 30


def test_rerun_resumes_instead_of_repeating(tasks, harnesses, config):
    run_eval(
        tasks,
        harnesses,
        FakeRunner({"baseline": 0.5, "v2": 0.5}),
        replace(config, trials=2),
    )
    runner = FakeRunner({"baseline": 0.5, "v2": 0.5})
    new = run_eval(tasks, harnesses, runner, config)
    assert runner.calls == 2 * 3 * 3  # only trials 2, 3, 4
    assert {r.trial for r in new} == {2, 3, 4}
    assert len(load_records(config.results_path)) == 30


def test_resume_survives_a_half_written_line(tasks, harnesses, config):
    run_eval(
        tasks,
        harnesses,
        FakeRunner({"baseline": 0.5, "v2": 0.5}),
        replace(config, trials=1),
    )
    with config.results_path.open("a") as f:
        f.write('{"harness": "baseline", "task_')  # simulated crash mid-write
    run_eval(
        tasks,
        harnesses,
        FakeRunner({"baseline": 0.5, "v2": 0.5}),
        replace(config, trials=2),
    )
    assert len(load_records(config.results_path)) == 12


def test_crash_and_timeout_are_recorded_and_run_continues(tasks, harnesses, config):
    runner = FakeRunner(
        {"baseline": 1.0, "v2": 1.0},
        crash_prompts=frozenset({"Solve a."}),
        timeout_prompts=frozenset({"Solve b."}),
    )
    records = run_eval(tasks, harnesses, runner, config)
    assert len(records) == 30
    by_task = {
        tid: [r for r in records if r.task_id == tid]
        for tid in ("task-a", "task-b", "task-c")
    }
    assert all("runner crashed: RuntimeError" in r.error for r in by_task["task-a"])
    assert all(r.error == "timeout (simulated)" for r in by_task["task-b"])
    assert all(r.passed and r.error is None for r in by_task["task-c"])
    assert not any(r.passed for r in by_task["task-a"] + by_task["task-b"])


def test_check_timeout_is_an_error(harnesses, config):
    task = Task(id="slow", prompt="p", setup="true", check="sleep 5")
    cfg = replace(config, trials=1, command_timeout_s=0.3)
    records = run_eval([task], harnesses, FakeRunner({"baseline": 1, "v2": 1}), cfg)
    assert all(r.error and r.error.startswith("check timed out") for r in records)


def test_setup_failure_is_an_error(harnesses, config):
    task = Task(id="t", prompt="p", setup="exit 7", check="true")
    records = run_eval(
        [task],
        harnesses,
        FakeRunner({"baseline": 1, "v2": 1}),
        replace(config, trials=1),
    )
    assert all(r.error.startswith("setup failed: exit 7") for r in records)
    assert not any(r.passed for r in records)


def test_rejects_bad_inputs(tasks, harnesses, config, tmp_path):
    runner = FakeRunner({"baseline": 1, "v2": 1})
    with pytest.raises(ValueError, match="not a directory"):
        run_eval(tasks, [tmp_path / "missing"], runner, config)
    with pytest.raises(ValueError, match="unique"):
        run_eval(tasks, [harnesses[0], harnesses[0]], runner, config)
    with pytest.raises(ValueError, match="repo"):
        run_eval(tasks, harnesses, runner, replace(config, repo=tmp_path / "nope"))
```

`SpyRunner` is a `FakeRunner` subclass that records what each workdir looked
like when the agent started. It also leaves a file behind and overwrites
`app.py`, as a sloppy agent would. If any later trial sees that file, or the
source repo changes, isolation is broken.

### `tests/test_report.py`

```python
"""Report math, the two headline scenarios, and Claude output parsing."""

from __future__ import annotations

import json

import pytest

from harness_eval.claude_runner import ClaudeCodeRunner, parse_claude_output
from harness_eval.evaluate import TrialRecord, load_records, run_eval
from harness_eval.report import (
    bootstrap_diff_ci,
    compare,
    format_report,
    summarize_harness,
)
from harness_eval.runners import FakeRunner


def rec(harness, task_id, trial, passed, turns=10, cost=0.1, error=None) -> TrialRecord:
    return TrialRecord(harness, task_id, trial, passed, turns, cost, 1.0, error)


def test_summary_math():
    records = [
        rec("a", "t1", 0, True, turns=4, cost=0.2),
        rec("a", "t1", 1, False, turns=8, cost=0.4),
        rec("a", "t2", 0, True, turns=6, cost=0.3),
        rec("a", "t2", 1, False, turns=0, cost=0.0, error="timeout"),
        rec("b", "t1", 0, True),
    ]
    s = summarize_harness("a", records)
    assert s.trials == 4
    assert s.pass_rate == 0.5  # errors count as failures
    assert dict(s.per_task) == {"t1": 0.5, "t2": 0.5}
    assert s.mean_turns == pytest.approx(6.0)  # errored trial excluded
    assert s.mean_cost_usd == pytest.approx(0.3)
    assert s.errors == 1


def test_bootstrap_identical_groups_include_zero():
    outcomes = [True, False] * 10
    diff, low, high = bootstrap_diff_ci(outcomes, outcomes, n_boot=2000)
    assert diff == 0
    assert low < 0 < high


def test_bootstrap_degenerate_groups():
    assert bootstrap_diff_ci([False] * 5, [True] * 5, n_boot=500) == (1.0, 1.0, 1.0)


def test_bootstrap_is_seeded():
    a, b = [True, False, False] * 5, [True, True, False] * 5
    assert bootstrap_diff_ci(a, b, seed=7) == bootstrap_diff_ci(a, b, seed=7)


def test_bootstrap_rejects_empty_group():
    with pytest.raises(ValueError):
        bootstrap_diff_ci([], [True])


def test_b_clearly_better_interval_excludes_zero(tasks, harnesses, config):
    run_eval(tasks, harnesses, FakeRunner({"baseline": 0.2, "v2": 0.9}, seed=1), config)
    records = load_records(config.results_path)
    c = compare(records, "baseline", "v2")
    assert c.diff > 0.4
    assert c.low > 0
    assert c.verdict == "v2 is better"
    assert "v2 is better" in format_report(records)


def test_equal_harnesses_no_clear_difference(tasks, harnesses, config):
    run_eval(tasks, harnesses, FakeRunner({"baseline": 0.5, "v2": 0.5}, seed=1), config)
    records = load_records(config.results_path)
    c = compare(records, "baseline", "v2")
    assert c.low <= 0 <= c.high
    assert "no clear difference" in format_report(records)


def test_report_lists_every_harness_and_task(tasks, harnesses, config):
    run_eval(tasks, harnesses, FakeRunner({"baseline": 0.3, "v2": 0.7}), config)
    text = format_report(load_records(config.results_path))
    for word in ("baseline", "v2", "task-a", "task-b", "task-c", "95% CI", "errors"):
        assert word in text


def test_report_on_empty_results():
    assert format_report([]) == "no trials recorded"


def test_claude_command_optional_caps():
    runner = ClaudeCodeRunner(max_turns=None, max_budget_usd=0.5)
    assert runner.command("go") == [
        "claude", "-p", "go", "--output-format", "json", "--max-budget-usd", "0.5",
    ]  # fmt: skip


def test_claude_command_shape():
    cmd = ClaudeCodeRunner(
        max_turns=12, extra_args=("--permission-mode", "acceptEdits")
    ).command("fix it")
    assert cmd == [
        "claude", "-p", "fix it", "--output-format", "json", "--max-turns", "12",
        "--permission-mode", "acceptEdits",
    ]  # fmt: skip


def test_parse_claude_success():
    out = json.dumps(
        {
            "num_turns": 7,
            "total_cost_usd": 0.42,
            "duration_ms": 12500,
            "is_error": False,
        }
    )
    result = parse_claude_output(out, 0, "", 99.0)
    assert (result.turns, result.cost_usd, result.duration_s, result.error) == (
        7,
        0.42,
        12.5,
        None,
    )


def test_parse_claude_reported_error():
    out = json.dumps(
        {
            "num_turns": 30,
            "total_cost_usd": 1.1,
            "is_error": True,
            "subtype": "error_max_turns",
        }
    )
    result = parse_claude_output(out, 1, "", 50.0)
    assert result.turns == 30 and result.duration_s == 50.0
    assert "error_max_turns" in result.error


@pytest.mark.parametrize("out", ["not json", "[]"])
def test_parse_claude_garbage_is_an_error(out):
    result = parse_claude_output(out, 0, "boom", 1.0)
    assert result.error is not None
```

## Meeting the acceptance criteria

- **`pytest` passes using only `FakeRunner`: task validation, trial
  isolation, scoring by exit code, JSONL output, and report math.** 41
  tests. One of them (`test_yaml_loads_when_available`) is skipped when
  PyYAML isn't installed. Task validation: `test_tasks.py`. Isolation:
  `test_trials_are_isolated_and_harness_applied` and
  `test_harness_is_applied_after_setup`. Scoring by exit code:
  `test_scored_by_check_exit_code` and
  `test_failing_check_fails_even_if_agent_succeeds`. JSONL:
  `test_jsonl_has_one_line_per_trial`, plus the resume tests. Report math:
  `test_summary_math` and the `test_bootstrap_*` tests. The `claude` tests
  only build the command and parse sample JSON. Nothing calls the CLI.
- **With `FakeRunner` configured so harness B is better, the report shows B
  ahead with an interval that excludes zero; with both equal, it reports no
  clear difference.** `test_b_clearly_better_interval_excludes_zero` uses
  pass probabilities of 0.2 vs 0.9 over 3 tasks × 5 trials.
  `test_equal_harnesses_no_clear_difference` uses 0.5 vs 0.5. During
  development, both scenarios were checked across 30 seeds. All 30 gave the
  expected verdict, so the tests don't depend on a lucky seed.
- **One live run of at least 3 tasks × 3 trials × 2 harnesses completes, and
  `results.jsonl` has one line per trial.** Use the live command under
  Verification. `wc -l` should print `18`. The resume logic means an
  interrupted live run can be finished by running the same command again.
- **A trial that times out or crashes is recorded as an error, not dropped,
  and doesn't stop the run.** `ClaudeCodeRunner` turns `TimeoutExpired` into
  an error result. `_safe_run` turns any exception from a runner into one.
  `test_crash_and_timeout_are_recorded_and_run_continues` makes one task
  crash and another time out, and asserts that all 30 trials are recorded
  and the third task still passes. `test_check_timeout_is_an_error` and
  `test_setup_failure_is_an_error` cover the other two places a trial can
  hang or break.

## Common pitfalls

- **Reusing one working copy across trials.** The second trial starts from
  the first trial's edits, and the results mean nothing. Copy fresh every
  time. For large repos, `git worktree add` is faster than `copytree` (see
  the stretch goal).
- **Applying the harness before `setup`.** `git checkout <sha>` or
  `git clean` can delete the harness files you just copied in. The baseline
  and v2 then run the same harness, and you measure nothing.
- **Dropping failed trials.** If a timeout raises and the loop dies, or the
  trial is skipped, the harness that times out more often looks *better*.
  Record every trial and count errors as failures.
- **Reading a small difference as a win.** With 15 trials per harness, a
  10-point gap is well inside the noise. The interval is there to stop you
  shipping a change that only looked better by chance. If the interval
  includes zero, run more trials or accept "no clear difference".
- **Treating the bootstrap as exact.** This bootstrap resamples trials
  without regard to task, and it assumes trials are independent. Both are
  simplifications. It is a sanity check, not a publication-grade test.
  Stratifying the resampling by task is a reasonable next step when tasks
  differ a lot in difficulty.
- **Running harness A for an hour, then harness B.** Model and API
  conditions drift. Interleave harnesses within each trial round, as
  `run_eval` does.
- **Permissions differ between harnesses by accident.** In `-p` mode, Claude
  Code denies tools that aren't allowed. If v2's `.claude/settings.json`
  allows `Bash(make check)` and baseline doesn't, part of the measured
  difference is permissions. That may be the point, but know it. Pass shared
  permission flags (for example `--permission-mode acceptEdits`) with
  `--claude-arg` so both harnesses get them.
- **Trusting `subprocess.run(timeout=...)` to kill everything.** It kills
  the direct child. If the agent started long-lived grandchildren (a dev
  server, a watcher), they can outlive the trial. For live runs, consider
  `start_new_session=True` and killing the process group on timeout, or run
  each trial in a container.
- **A `check` that is easy to game.** If `check` is only `pytest`, an agent
  can delete the failing test. Pin the test files in `check` (for example
  `git diff --exit-code -- tests/`), or check behavior in a way the agent's
  edits can't reach.

## Verification

From a directory that contains the files above:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Offline tests: FakeRunner only, no network, no Claude Code.
pytest -q

# Offline demo: v2 configured to be better.
python -m harness_eval run \
  --tasks examples/fake-tasks.json --repo examples/demo-repo \
  --harness harnesses/baseline --harness harnesses/v2 \
  --runner fake --fake-pass baseline=0.3 --fake-pass v2=0.8 \
  --results /tmp/fake-results.jsonl

# Same harnesses configured equal: expect "no clear difference".
python -m harness_eval run \
  --tasks examples/fake-tasks.json --repo examples/demo-repo \
  --harness harnesses/baseline --harness harnesses/v2 \
  --runner fake --fake-pass baseline=0.5 --fake-pass v2=0.5 \
  --results /tmp/fake-equal.jsonl

# A bad task file fails fast with the task and field named (exit 2).
echo '[{"id": "x"}]' > /tmp/bad.json
python -m harness_eval run --tasks /tmp/bad.json --repo . --harness harnesses/v2
```

Expected: `pytest` reports `40 passed, 1 skipped` (or `41 passed` with
PyYAML installed). The first demo prints a table and a line like:

```text
v2 vs baseline: pass-rate difference +60.0%, 95% CI [+33.3%, +86.7%] -> v2 is better
```

The equal demo prints:

```text
v2 vs baseline: pass-rate difference -13.3%, 95% CI [-46.7%, +20.0%] -> no clear difference
```

Both harnesses had the same true pass rate, yet the observed gap is 13
points. That gap is noise, and it is the reason the interval exists.

The bad task file prints
`task #1 (id='x'): missing field(s): prompt, setup, check`.

Live run (costs money; check the flags first as described under
`claude_runner.py`):

```bash
claude -p "say hi" --output-format json   # confirm the JSON fields once

python -m harness_eval run --runner claude \
  --tasks examples/tasks.json --repo /path/to/exercise-01-repo \
  --harness harnesses/baseline --harness harnesses/v2 \
  --trials 3 --max-budget-usd 1.00 --timeout 900 \
  --claude-arg=--permission-mode --claude-arg=acceptEdits \
  --results results-live.jsonl

wc -l results-live.jsonl                       # 18 = 3 tasks x 3 trials x 2 harnesses
python -m harness_eval report --results results-live.jsonl
```

If the run is interrupted, run the same command again. It skips the trials
already in `results-live.jsonl` and finishes the rest.
