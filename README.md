# pysqlmut

[![CI](https://github.com/pmayd/pysqlmut/actions/workflows/ci.yml/badge.svg)](https://github.com/pmayd/pysqlmut/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Mutation testing for SQL.

pysqlmut changes your SQL files in small, valid ways (a flipped comparison, a swapped aggregate, a dropped
join condition, a column mixed up with a similar one), runs your tests against every change, and reports
the changes no test noticed. Each surviving change points at a rule in your SQL that your tests do not pin
down.

It is made for SQL that is tested by running it, for example on DuckDB inside pytest. Tests that compare
SQL text cannot catch these changes.

**New here?** [GETTING_STARTED.md](GETTING_STARTED.md) walks through a small example from the first run to
closing the gaps it finds. [ROADMAP.md](ROADMAP.md) lists what is missing and what may come next.

## Status

Early development. The command line, the settings and the report format may still change before 1.0.

## Installation

```bash
uv tool install pysqlmut      # or: pipx install pysqlmut
```

- pysqlmut needs Python 3.12 or newer and runs on Linux, macOS and Windows through WSL.
- With `--pytest`, the tested project's own environment needs pytest 8.1 or newer, on Python 3.9 or newer.

## Quick start

```bash
cd your-project

# See which changes pysqlmut would make, without running any test.
pysqlmut generate sql/orders.sql --dialect duckdb --show

# Run the tests against every change: 8 copies of the project, each with a pytest process that runs
# only the tests that read the changed file.
pysqlmut run sql/orders.sql --dialect duckdb --pytest "uv run python" --tests tests/sql \
    --workers 8 --report pysqlmut-report.json

# Review the survivors later, grouped by the code they changed.
pysqlmut report pysqlmut-report.json
```

A group of survivors looks like this:

```text
    3x comparison (= -> <>): x.x = 'S'
       e.g. sql/orders.sql:14
         - WHERE o.status = 'paid'
         + WHERE o.status <> 'paid'
```

The same rule written in several places, with different names and values, is one group, so a repeated gap
shows up once.

## How it works

**Generating mutants.** sqlglot parses each statement. Each operator looks at one expression and proposes a
text patch together with the change it means for the parse tree. A mutant is kept only when the patched
statement parses to exactly that changed tree, so a mutant never changes more than it claims, and the rest of
the file stays as it is, comments, formatting and line endings included. Statements sqlglot cannot parse are
skipped. For the same SQL and settings, the mutants and their order are always the same.

**Running tests.** Every worker gets its own copy of the project, which shares the project's virtual
environment; your working tree is never changed. There are two runners:

- `--command "..."` runs any shell command in a new process for every mutant.
- `--pytest PYTHON` keeps one pytest process per copy. A first run records which test reads which SQL file,
  directly or through a fixture, and each mutant then runs only those tests, in a fork of that process that
  imports the project's modules again. Files read while modules are imported make every test run.

Ctrl-C or a timeout ends the test processes a run started. A run stops before testing any mutant when the
tests fail on the unchanged project, or when they read the project itself instead of the copy that holds the
mutant, since no result would mean anything then.

## Results

| Status | Meaning |
|---|---|
| caught | a test failed |
| survived | every test passed although the SQL changed |
| not covered | no test was seen reading the file, so nothing ran |
| accepted | a reviewed survivor, not run again |
| timeout | the tests did not finish in time |
| error | the tests could not run, for example pytest collected no tests |

*Survivors* are the mutants that survived or were not covered.

| Exit code | When |
|---|---|
| 0 | every mutant was caught or accepted |
| 1 | mutants survived, were not covered, timed out or ended in an error |
| 2 | a usage or settings error |
| 3 | the tests fail on the unchanged project, or read the project instead of its copy |
| 4 | pysqlmut itself failed; please report it |
| 130 | the run was interrupted |

## Operators

| Operator | Change |
|---|---|
| `comparison` | `=` and `<>` swap; `<` and `<=`, `>` and `>=` move the boundary |
| `logical` | `AND` and `OR` swap |
| `drop-condition` | one side of an `AND` is dropped |
| `is-null` | `IS NULL` and `IS NOT NULL` swap |
| `aggregate` | `SUM` becomes `MAX`, `MAX` becomes `MIN`, `MIN` becomes `MAX`, `AVG` becomes `SUM` |
| `coalesce` | only the first argument is kept, or the first two swap |
| `order` | `ASC` and `DESC` swap |
| `union` | `UNION` and `UNION ALL` swap |
| `join-type` | `LEFT JOIN` and `INNER JOIN` swap |
| `arithmetic` | `+` and `-`, `*` and `/` swap; a unary minus is dropped |
| `literal` | an integer grows by one |
| `distinct` | `DISTINCT` is dropped from `SELECT` and `COUNT` |
| `case` | the `ELSE` value becomes `NULL`, or one `WHEN` branch is dropped |
| `string-literal` | a string gets a suffix |
| `column` | a column becomes the most similarly named other column of the same table |

A change that provably cannot alter any result is left out: `UNION ALL` and `UNION` return the same rows when
every branch returns unique rows and each pair of branches differs in a literal column.

## Configuration

Settings live in the tested project's `pyproject.toml`; command line options override them. Paths in the
settings are relative to the project, and patterns are globs, where `*` does not cross a directory.

```toml
[tool.pysqlmut]
dialect = "duckdb"
files = ["sql/**/*.sql"]
exclude-operators = ["union"]
accepted = "pysqlmut-accepted.json"
workers = 8     # processes that generate mutants, then parallel test runs
timeout = 180   # seconds per mutant

# Long-lived pytest workers that run only the tests reading the mutated file.
[tool.pysqlmut.pytest]
python = "uv run python"
tests = ["tests/sql"]
args = ["-q", "-x"]

[[tool.pysqlmut.file]]
pattern = "sql/labels.sql"
exclude-operators = ["string-literal"]
```

Instead of `[tool.pysqlmut.pytest]`, `command = "..."` runs any test command, in a new process per mutant.
With the settings in place, `pysqlmut run` needs no arguments. Unknown settings and values of the wrong type
are reported before anything runs.

## Reviewing survivors

- `-- pysqlmut: skip` at the end of a line excludes that line; `-- pysqlmut: off` and `-- pysqlmut: on`
  exclude a block, including changes that would reach into it.
- `pysqlmut accept pysqlmut-report.json` records the survivors of a report as reviewed, and later runs neither
  run nor report them. A survivor is identified by its file, operator, description and the line before and
  after the change, so edits elsewhere in the file keep it accepted. `--operators` accepts only some
  operators. Commit the accepted file with the project.
- `pysqlmut generate` (also available as `pysqlmut list`) shows every mutant and why candidates were
  rejected.

## Limitations

- With `--pytest`, a file read by a subprocess, a pytest-xdist worker or native code counts as not covered.
  Use `--command` for such projects.
- Some survivors cannot change a result for any data your SQL can see, for example `MAX` to `MIN` on a value
  that is the same in every row of its group. pysqlmut only removes such changes when it can prove it from the
  SQL alone; accept the rest after review.
- `string-literal` and `case` produce many survivors on label and mapping SQL. Exclude them per file when their
  survivors are not useful.
- Jinja templates, scripting blocks, procedures and other statements sqlglot cannot parse are not mutated.
- Windows is supported only through WSL.

## Development

```bash
just setup   # install dependencies and pre-commit hooks
just check   # lint, type check, test
```

`AGENTS.md` describes the design rules and what makes a test worth keeping. `RELEASING.md` describes how
versions are published, `SECURITY.md` how to report a vulnerability, and `CHANGELOG.md` lists the changes.

## License

MIT
