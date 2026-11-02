# mod-208-harness-engineering/exercise-01-harness-a-repository — Solution

## Approach

This exercise has no single right answer. Every repository needs a different
harness. The reference below shows a complete harness for a representative
target, a small Python CLI called `csvtool` (parse, filter, and export CSV
files, about 3k lines, `pytest` tests), and explains the reasoning behind each
file so you can apply the same reasoning to your own repo.

The order of work matters more than the files:

1. **Baseline first, with no harness.** You can't write a good instruction file
   until you've watched the agent fail without one. Every convention line in the
   final `AGENTS.md` below comes from something the agent got wrong in the
   baseline. None of it is advice written in advance.
2. **One check command before anything else.** It's the definition of done the
   other files point to, and the sensor the agent iterates against.
3. **Instruction file as a map.** Exact commands, a directory map, the
   conventions the agent broke, and pointers. Nothing it already gets right.
4. **Skill for the repeated procedure.** In `csvtool`, adding a subcommand
   touches four files in a fixed order, and the baseline agent missed the help
   text and the registration step.
5. **Permissions last.** Once you know which commands the agent runs constantly,
   allow those so prompts stop, and deny the few things that must never happen.

## Reference implementation

Layout of the harness files added to the target repo:

```text
csvtool/
├── AGENTS.md                         # the map (all tools read this)
├── CLAUDE.md                         # one line: imports AGENTS.md
├── Makefile                          # `make check` = definition of done
├── .claude/
│   ├── settings.json                 # Claude Code permissions
│   └── skills/
│       └── add-subcommand/
│           └── SKILL.md              # the repeated procedure
├── .codex/
│   └── config.toml                   # Codex sandbox + approvals (project scope)
├── TASKS.md                          # the three benchmark tasks
└── RESULTS.md                        # before/after measurements
```

### `Makefile`

```makefile
.PHONY: check fmt-check lint types test

# The one command the agent runs before saying it's done.
check: fmt-check lint types test

fmt-check:
	uv run ruff format --check src tests

lint:
	uv run ruff check src tests

types:
	uv run mypy src

test:
	uv run pytest -q
```

`make` stops at the first failing target and exits non-zero, which is exactly
what both the agent and the exercise-02 stop gate need.

### `AGENTS.md`

```markdown
# csvtool

A command-line tool for filtering, transforming, and exporting CSV files.
Used in data pipelines, so output formats are a public contract.

## Commands
- Setup: `uv sync`
- Check (run before you say you're done; must pass): `make check`
- One test: `uv run pytest tests/test_filter.py -k test_name -q`
- Try the CLI: `uv run csvtool --help`

## Map
- `src/csvtool/cli.py` — argument parsing; every subcommand registers here
- `src/csvtool/commands/` — one module per subcommand (`export.py` is the reference)
- `src/csvtool/core/` — pure functions, no file or network I/O
- `src/csvtool/io.py` — the only module that opens files
- `tests/` — mirrors `src/`; fixtures in `tests/data/`

## Conventions
- `core/` must not import from `commands/` or `io.py`. Pass data in, return data out.
- Never change an existing output column name or order; it breaks users' pipelines.
  Add new columns at the end, behind a flag.
- Errors shown to users go through `csvtool.errors.fail(msg)`, never `print` + `sys.exit`.
- Don't edit `tests/data/*.csv` to make a test pass. Add a new fixture.

## More
- Design and layer rules: `docs/architecture.md`
- Output format contract: `docs/output-formats.md`
- Adding a subcommand: use the `add-subcommand` skill
```

Why each section is there:

| Section | Baseline failure it fixes |
|---|---|
| Commands | The agent ran `python -m pytest` outside the venv, got import errors, and spent four turns on the environment. |
| Map | It searched the whole tree to find where subcommands register. |
| `core/` import rule | In the refactor task it imported `io.py` into `core/filter.py`. |
| Output contract | In the feature task it renamed a column "for clarity". |
| `fail()` rule | It added `print(...); sys.exit(1)` in two places. |
| Fixture rule | In the bug-fix task it edited a fixture CSV so the test passed. |

Lines considered and cut: "write clean code" (no checkable content), the full
`ruff` rule list (the linter enforces it), and a paragraph of project history
(true but never changed what the agent did).

### `CLAUDE.md`

```markdown
@AGENTS.md
```

One source of truth. Codex, Cursor, and Copilot read `AGENTS.md` directly, and
Claude Code pulls it in through the import.

### `.claude/skills/add-subcommand/SKILL.md`

```markdown
---
name: add-subcommand
description: Use when adding a new csvtool subcommand. Covers the module, registration, help text, and tests.
---

1. Create `src/csvtool/commands/<name>.py`. Copy the structure of `commands/export.py`:
   a `register(subparsers)` function and a `run(args) -> int` function.
2. Put any data logic in `src/csvtool/core/<name>.py` as pure functions. `run()` reads input
   via `csvtool.io`, calls core, writes output via `csvtool.io`.
3. In `src/csvtool/cli.py`, import the module and call `register(subparsers)`, keeping the
   list alphabetical.
4. Add `tests/commands/test_<name>.py` (CLI behavior) and `tests/core/test_<name>.py`
   (pure logic). Cover: happy path, a missing required argument, an empty CSV.
5. Run `make check`. Then confirm `uv run csvtool --help` lists the new subcommand with a
   one-line description.
```

### `.claude/settings.json`

```json
{
  "permissions": {
    "allow": [
      "Bash(make check)",
      "Bash(uv run pytest:*)",
      "Bash(uv run ruff:*)",
      "Bash(uv run mypy:*)",
      "Bash(uv run csvtool:*)",
      "Bash(git status)",
      "Bash(git diff:*)",
      "Bash(git log:*)"
    ],
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "Edit(./uv.lock)",
      "Edit(./tests/data/**)",
      "Bash(git push:*)",
      "Bash(uv publish:*)"
    ]
  }
}
```

The allow list is the commands the agent ran repeatedly in the baseline. Each
one used to be a prompt. The deny list makes two of the `AGENTS.md` conventions
mechanical: the lockfile and the fixture data. Instructions ask; deny rules
decide. Pushing and publishing are denied because they reach outside the
machine, and a human should do them. Check the current rule syntax in the Claude
Code settings docs; the pattern format has changed between versions.

### `.codex/config.toml`

```toml
# Codex: work inside the repo, ask before anything outside it.
sandbox_mode = "workspace-write"
approval_policy = "on-request"

[sandbox_workspace_write]
network_access = false
```

Codex expresses the policy differently. The sandbox keeps writes inside the
workspace and blocks network access at the OS level, and the approval policy
decides when it asks. There's no per-path deny list the way Claude Code has, so
the fixture and lockfile rules rely on the instruction file plus the
exercise-02-style checks. Confirm key names against the Codex config reference
for your installed version, and note that project-scoped config may need the
project to be trusted first.

### `TASKS.md`

```markdown
# Benchmark tasks

Run each in a fresh session. Reset with `git checkout bench-start && git clean -fd` first.

1. **Bug fix.** `csvtool filter --where "age>30"` drops rows where `age` has surrounding
   whitespace. Fix it.
   Done when: `uv run pytest tests/core/test_filter.py -q` passes, including a new test for
   the whitespace case, and `make check` passes.

2. **Feature.** Add a `dedupe` subcommand that removes duplicate rows, keyed on
   `--columns a,b` (default: all columns), keeping the first occurrence.
   Done when: `uv run csvtool dedupe --help` works, tests cover the default and keyed cases,
   and `make check` passes.

3. **Refactor.** `commands/export.py` has the JSON and Parquet writers inline. Move them to
   `core/writers.py` as pure functions with no behavior change.
   Done when: `git diff --stat tests/` is empty (no test changes) and `make check` passes.
```

### `RESULTS.md` (template with an illustrative fill)

```markdown
| Task | Harness | Done? | Check passed? | Turns | Cost / tokens | Interventions |
|------|---------|-------|---------------|-------|---------------|---------------|
| 1 bug fix | none | yes | no (edited fixture) | 31 | … | stopped it editing tests/data |
| 1 bug fix | v1   | yes | yes | 14 | … | none |
| 2 feature | none | yes | no (help text missing) | 42 | … | pointed it to cli.py |
| 2 feature | v1   | yes | yes | 22 | … | none |
| 3 refactor | none | yes | yes | 27 | … | approved 9 commands |
| 3 refactor | v1   | yes | yes | 18 | … | none |
```

These numbers are an example of the *shape* of a result, not a measurement to
compare against. Your repo, tool, and model will differ. With one run per cell,
treat any difference as a lead to investigate, not proof. Exercise-03 shows
how to measure it properly.

## How it meets the acceptance criteria

| Criterion | Where |
|---|---|
| `AGENTS.md` under 80 lines with exact commands and pointers, not copies | `AGENTS.md` is 29 lines; Commands section; More section links `docs/` and the skill |
| Skill loads for its task | Task 2 transcript shows `add-subcommand` invoked; the description is written as a "Use when..." trigger |
| Check runs without prompting; a denied action is blocked | `Bash(make check)` in `allow`; editing `tests/data/` or `uv.lock` hits `deny` |
| `make check` fails on breakage, passes clean | `make` exits non-zero on the first failing target; verify with the steps below |
| `RESULTS.md` has before/after rows for all three tasks | Template above, same fields per row |

## Common pitfalls

- **Writing `AGENTS.md` before the baseline.** You end up with generic advice
  and miss the specific mistakes this agent makes in this repo.
- **Copying docs into the instruction file.** It loads every session. Link to
  `docs/architecture.md`; don't paste it.
- **Vague commands.** "Run the tests" leads to the agent guessing at an
  invocation. Paste the exact command, including `uv run` or the venv.
- **A skill description written as a title** ("Subcommands"). The description
  decides when the skill loads; write it as "Use when...".
- **Allowing too little.** If every `pytest` run prompts, the human starts
  approving without reading, and the prompts protect nothing.
- **Relying on deny patterns as security.** They're guardrails. The sandbox is
  the boundary (Chapter 3).
- **Changing several things between runs and crediting one.** If you edit
  `AGENTS.md`, add a skill, and change permissions at once, you can't say which
  one helped.

## Verification

```bash
# 1. Instruction file is short
wc -l AGENTS.md                 # expect < 80

# 2. The check command fails on breakage and passes clean
echo "x = (" >> src/csvtool/core/filter.py
make check; echo "exit=$?"      # expect non-zero
git checkout src/csvtool/core/filter.py
make check; echo "exit=$?"      # expect 0

# 3. Permissions: in a Claude Code session, ask it to "update the fixture in
#    tests/data/people.csv". Expect a permission denial. Ask it to run the checks;
#    expect no prompt.

# 4. Skill: run task 2 and look for the add-subcommand skill in the transcript
#    (Claude Code shows skill use inline; or run `/skills` to confirm it's listed).

# 5. Codex: `codex` in the repo, ask it to `curl https://example.com`. Expect the
#    sandbox to block network access.
```
