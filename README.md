# pysqlmut

[![PyPI version](https://img.shields.io/pypi/v/pysqlmut)](https://pypi.org/project/pysqlmut/)
[![Python versions](https://img.shields.io/pypi/pyversions/pysqlmut)](https://pypi.org/project/pysqlmut/)
[![GitHub release](https://img.shields.io/github/v/release/pmayd/pysqlmut)](https://github.com/pmayd/pysqlmut/releases)
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

- pysqlmut needs Python 3.12 or newer and runs on Linux, macOS and Windows through WSL. Installed as a tool, it
  lives in an environment of its own, apart from your project.
- Your tests keep running in your project's own environment. For the pytest runner, that environment needs
  pytest 8.1 or newer on Python 3.9 or newer; see [Choosing a runner](#choosing-a-runner).

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
environment; your working tree is never changed. The tests run with one of two runners, described next.

Ctrl-C or a timeout ends the test processes a run started. A run stops before testing any mutant when the
tests fail on the unchanged project, or when they read the project itself instead of the copy that holds the
mutant, since no result would mean anything then.

## Choosing a runner

pysqlmut has to run your tests once for every mutant, so how it runs them decides how long a run takes.

**`--command "..."` runs any test command.** Give it the command you would type, for example
`--command "uv run pytest tests/sql"`, `--command "python -m unittest"` or `--command "make test"`. For every
mutant, pysqlmut starts that command in a new process and reads its exit code. This works with every test
tool, but each mutant pays for starting Python, importing your dependencies and running the whole suite.

Use it to try pysqlmut, for test tools other than pytest, and for tests that read the SQL in another process,
such as dbt, a database command line tool or pytest-xdist workers.

**`--pytest COMMAND` runs pytest inside your project's Python.** Give it the command that starts the Python of
your project's environment, the one with pytest and your project's dependencies installed: `uv run python`,
`poetry run python`, or a path such as `.venv/bin/python`. pysqlmut needs it because it is installed apart from
your project, so its own Python cannot import your code or your test dependencies.

pysqlmut starts pytest in that Python once per worker and keeps it running. A first run records which test
reads which SQL file, directly or through a fixture. For each mutant, pysqlmut then runs only the tests that
read the changed file, in a fork of the running process that imports your project's modules again, so
libraries such as DuckDB or pandas are not imported again for every mutant. When a file is read while modules
are imported, every test runs for its mutants.

Use it when your tests are pytest tests; pytest also runs `unittest.TestCase` tests. On a large suite where
each SQL file is read by only a few tests, it is much faster than `--command`.

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

## Command reference

Every option can also come from `[tool.pysqlmut]` (see [Configuration](#configuration)); an option on the command
line wins over the setting. `pysqlmut COMMAND --help` prints the same options.

### `pysqlmut generate [FILES]` (alias `pysqlmut list`)

Generates the mutants and shows why candidates were rejected, without running any test.

| Option | Meaning | Default or setting |
|---|---|---|
| `FILES` | SQL files to mutate | the `files` setting |
| `--dialect NAME` | sqlglot dialect the SQL is written in, such as `duckdb`, `snowflake`, `bigquery`, `postgres` | `dialect` |
| `--operators LIST` | comma-separated operators to use, from the [Operators](#operators) table | `operators`, else all |
| `--project DIR` | project root: where `pyproject.toml` is read and relative paths start | the working directory |
| `--show` | print every mutant with its line before and after | off |
| `--workers N` | processes that generate mutants in parallel | `workers`, else 1 |

### `pysqlmut run [FILES]`

Runs the tests against every mutant and prints the results grouped by the code they changed. It takes the
options of `generate` except `--show`, and these:

| Option | Meaning | Default or setting |
|---|---|---|
| `--command COMMAND` | shell command that runs your tests, started anew for every mutant | `command` |
| `--pytest COMMAND` | command that starts your project's Python, for the pytest runner; see [Choosing a runner](#choosing-a-runner) | `[tool.pysqlmut.pytest] python` |
| `--tests PATH` | pytest path to collect; repeat it for several | `[tool.pysqlmut.pytest] tests`, else pytest's own settings |
| `--pytest-args "ARGS"` | extra pytest options in one string, such as `"-q -x"` | `[tool.pysqlmut.pytest] args` |
| `--workers N` | copies of the project that test mutants in parallel | `workers`, else 1 |
| `--timeout SECONDS` | time one mutant's tests may take before the mutant counts as timeout; the first run of the unchanged tests gets at least 600 | `timeout`, else 300 |
| `--accepted PATH` | file of accepted survivors, relative to the working directory | `accepted`, else `pysqlmut-accepted.json` in the project |
| `--report PATH` | write every result as JSON, for `report` and `accept` | no report |
| `--top N` | how many survivor groups to print | 30 |

Give exactly one of `--command` and `--pytest`, or set one of them in the settings.

### `pysqlmut report REPORT_FILE`

Prints the results of a JSON report again, grouped by the code they changed.

| Option | Meaning | Default |
|---|---|---|
| `REPORT_FILE` | a report written by `run --report` | required |
| `--status LIST` | comma-separated statuses to show: `caught`, `survived`, `not covered`, `accepted`, `timeout`, `error` | `survived,not covered` |
| `--top N` | how many groups to print | 30 |

### `pysqlmut accept REPORT_FILE`

Records the survivors of a report as reviewed, so later runs neither run nor report them.

| Option | Meaning | Default or setting |
|---|---|---|
| `REPORT_FILE` | a report written by `run --report` | required |
| `--operators LIST` | accept only the survivors of these comma-separated operators | all survivors |
| `--project DIR` | project root, for the `accepted` setting | the working directory |
| `--accepted PATH` | file of accepted survivors, relative to the working directory | `accepted`, else `pysqlmut-accepted.json` in the project |

### Everywhere

`--help` shows a command's options. `pysqlmut --install-completion` installs shell completion for the current
shell, and `--show-completion` prints it to copy.

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

- **Exclude SQL from mutation with comments in the SQL file itself.** `-- pysqlmut: skip` at the end of a line
  excludes that line. `-- pysqlmut: off` and `-- pysqlmut: on`, each on a line of its own, exclude the lines
  between them, and any change that would reach into them:

  ```sql
  SELECT customer, SUM(amount) AS revenue
  FROM orders
  WHERE status = 'paid'  -- pysqlmut: skip
  -- pysqlmut: off
    AND created_at >= DATE '2020-01-01'
    AND region <> 'test'
  -- pysqlmut: on
  GROUP BY customer;
  ```

  Here pysqlmut changes nothing on the `WHERE` line and the two lines between `off` and `on`.
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
