# AGENTS.md

## Project

pysqlmut is mutation testing for SQL. It finds places in SQL files where a small, syntactically valid change (a
flipped comparison, a swapped aggregate, a dropped condition) should change the result, applies each change to a
copy of the project, runs the project's tests there, either through a shell command or through pytest workers,
and reports the changes no test noticed.

- Parsing and node lookup: sqlglot, for every dialect it supports.
- A mutant is a text patch on the original file: only the characters of the mutated node change. The SQL is never
  regenerated from the parse tree, because regeneration reformats the file, drops comments and can change
  constructs sqlglot does not round-trip.
- Mutants run in a copy of the project, never in the user's working tree.
- Words used everywhere, in code, help texts and docs: a *mutant* is *caught*, *survived*, *not covered*,
  *accepted*, *timeout* or *error*; *survivors* are the mutants that survived or were not covered.

## Structure

- `src/pysqlmut/` - package code; `pytest_worker.py` runs inside the tested project's Python and may use only the
  standard library and pytest, on Python 3.9 and newer
- `tests/` - pytest tests; `tests/samples.py` holds the sample project several tests run pysqlmut on
- `examples/getting-started/` - the project `GETTING_STARTED.md` walks through
- `justfile` - `just setup`, `just format`, `just lint`, `just typecheck`, `just test`, `just check`
- `README.md` for users, `GETTING_STARTED.md`, `ROADMAP.md`, `CHANGELOG.md`, `RELEASING.md`, `SECURITY.md`

## Testing

Tests must earn their place. There is no test-first rule.

- Write a test only if it would fail on a real defect: a wrong patch, a mutant that no longer parses, a runner
  that misclassifies a caught mutant, a project left changed. A test that fails on a harmless refactor is a cost.
- Assert behaviour: the patched SQL text, the list of mutants, the run result, the exit code. Never assert on
  implementation details.
- Prefer a few scenario tests over many narrow tests of the same path.

## Comments

- A comment says what the code is for and why, in plain, complete sentences.
- Never write measurements, investigation history or ticket numbers into comments.
- No comment that repeats what the code and its names already say.

## Changes

- Record user-facing changes under *Unreleased* in `CHANGELOG.md`.
- Keep `README.md`, `GETTING_STARTED.md` and the command help in step with the behaviour.
