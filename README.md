# pysqlmut

Mutation testing for SQL.

pysqlmut finds places in your SQL files where a small, valid change should change the result: a
flipped comparison, a swapped aggregate, a dropped join condition, a reversed sort order. It
applies each change to a copy of your project, runs your own test command, and reports every
change that no test noticed.

- Works on any dialect [sqlglot](https://github.com/tobymao/sqlglot) parses.
- A mutant changes only the characters of one expression; the rest of the file, comments and
  formatting included, stays as it is.
- Statements sqlglot cannot parse (for example scripting blocks) are skipped, not guessed.

## Status

Prototype.

## Usage

```bash
pysqlmut generate scripts/sql/refresh.sql --dialect snowflake --show   # generate and show the mutants
pysqlmut run --report pysqlmut-report.json                             # run tests against every mutant
pysqlmut report pysqlmut-report.json                                   # survivors grouped by the code they changed
pysqlmut accept pysqlmut-report.json                                   # accept reviewed survivors
```

`list` is an alias for `generate`. Mutants are not stored anywhere, so listing them means generating
them from the SQL each time; no tests run.

`run` exits with 1 while survivors remain that nobody accepted.

## Configuration

In the tested project's `pyproject.toml`; command line options override it.

```toml
[tool.pysqlmut]
dialect = "snowflake"
files = ["sql/*.sql"]
exclude-operators = ["union"]
accepted = "pysqlmut-accepted.json"
workers = 8   # processes that generate mutants, then parallel test runs
timeout = 180

# Long-lived pytest workers that run only the tests reading the mutated file.
[tool.pysqlmut.pytest]
python = "uv run python"
tests = ["tests/sql"]
args = ["-q", "-x"]

[[tool.pysqlmut.file]]
pattern = "scripts/sql/labels.sql"
exclude-operators = ["string-literal"]
```

Instead of `[tool.pysqlmut.pytest]`, `command = "..."` runs any test command, in a new process per mutant.

In SQL, `-- pysqlmut: skip` excludes its line, and `-- pysqlmut: off` ... `-- pysqlmut: on` a block.

## Development

```bash
just setup   # install dependencies and pre-commit hooks
just check   # lint, type check, test
```
