# Roadmap

Where pysqlmut stands and what could come next. These are directions, not promises; issues and pull
requests decide the order.

## Supported today

- **SQL:** every dialect sqlglot parses. Statements sqlglot cannot parse, such as scripting blocks and
  procedures, are skipped.
- **Tests:** any test suite through `--command`, which runs every test for every mutant. With `--pytest`,
  only the tests that read the mutated file run. pytest also collects `unittest.TestCase` tests, so unittest
  suites work with `--pytest` as long as pytest is installed.
- **Platforms:** Linux and macOS, and Windows through WSL.

## Known gaps

- **Reads pysqlmut cannot see.** With `--pytest`, a file read by a subprocess (dbt, a database CLI), by a
  pytest-xdist worker or by native code counts as not covered. Use `--command` for such projects for now.
- **Templated SQL.** Jinja templates such as dbt models do not parse as SQL, so they are skipped.
- **Survivors that cannot change a result.** Some remain, for example `MAX` to `MIN` on a value that is the
  same in every row of its group. They need a review and `pysqlmut accept`.
- **Windows without WSL.** The pytest runner relies on `fork`, process groups and `select` on pipes.

## Next steps

1. **Faster feedback in CI**
   - Mutate only files changed since a git ref (`--changed-since main`).
   - Remember the results of unchanged files and tests between runs.
   - Output for CI systems: a Markdown summary, GitHub annotations, JUnit or SARIF.
2. **Better mutants**
   - A column operator that knows the schema, from DDL files or a database, so that it can also swap in
     columns the query does not read.
   - More operators: `BETWEEN` bounds, `IN` lists, `CAST` types, date arithmetic, `LIMIT`, window
     `PARTITION BY` and frames, `IS DISTINCT FROM`.
   - More proofs that a change cannot alter the result, to leave out equivalent mutants.
3. **More projects**
   - A way to run all tests for files whose reads are not seen, instead of reporting them as not covered.
   - dbt: mutate a model's compiled SQL and map the result back to the model.
   - A pre-commit hook and a GitHub Action.
   - When pysqlmut is installed in the project's own environment, for example as a dev dependency run with
     `uv run pysqlmut run`, use that Python for the pytest runner by default, so `--pytest` needs no command.
   - A thin pytest plugin, such as `pytest --sqlmut`, that only starts a pysqlmut run for the current project.
     The work stays in the standalone tool, which keeps its own dependencies and Python version apart from the
     project and also serves projects that do not use pytest.
4. **Windows** without WSL: first `generate` and `--command`, then the pytest runner if there is demand.

## Before 1.0

- The command line, the `[tool.pysqlmut]` settings and the report format stay stable for a while.
- pysqlmut has been used on several real projects beyond the ones it was built with.
