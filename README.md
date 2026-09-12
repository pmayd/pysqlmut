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

Early prototype. `pysqlmut list` generates and verifies mutants; running tests against them is next.

## Usage

```bash
uv run pysqlmut list scripts/sql/refresh.sql --dialect snowflake --show
```

## Development

```bash
just setup   # install dependencies and pre-commit hooks
just check   # lint, type check, test
```
