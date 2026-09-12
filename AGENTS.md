# AGENTS.md

## Project

pysqlmut is mutation testing for SQL. It finds places in SQL files where a small, syntactically valid
change (a flipped comparison, a swapped aggregate, a dropped condition) should change the result,
applies each change to a copy of the project, runs the project's own test command, and reports the
changes no test noticed.

- Parsing and node lookup: sqlglot, for every dialect it supports.
- A mutant is a text patch on the original file: only the characters of the mutated node change.
  The SQL is never regenerated from the parse tree, because regeneration reformats the file, drops
  comments and can change constructs sqlglot does not round-trip.
- Mutants run in a copy of the project, never in the user's working tree.

## Structure

- `src/pysqlmut/` - package code
- `tests/` - pytest tests
- `justfile` - `just setup`, `just format`, `just lint`, `just typecheck`, `just test`, `just check`

## Testing

Tests must earn their place. There is no test-first rule.

- Write a test only if it would fail on a real defect: a wrong patch, a mutant that no longer parses,
  a runner that misclassifies a killed mutant. A test that fails on a harmless refactor is a cost.
- Assert behaviour: the patched SQL text, the list of mutants, the run result. Never assert on
  implementation details.
- Prefer a few scenario tests over many narrow tests of the same path.

## Comments

- A comment says what the code is for and why, in plain, complete sentences.
- Never write measurements, investigation history or ticket numbers into comments.
- No comment that repeats what the code and its names already say.
