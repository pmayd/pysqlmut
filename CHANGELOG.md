# Changelog

All notable changes to pysqlmut are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the command line or
the configuration.

## [Unreleased]

### Added

- Mutant generation for every dialect sqlglot parses. A mutant is a text patch on one expression, kept
  only when the patched statement parses to exactly the intended change; comments and formatting stay.
- Operators: `comparison`, `logical`, `drop-condition`, `is-null`, `aggregate`, `coalesce`, `order`,
  `union`, `join-type`, `arithmetic`, `literal`, `distinct`, `case`, `string-literal`, `column`.
- UNION ALL and UNION changes are left out when every branch returns unique rows that no other branch
  can return.
- `pysqlmut generate` (alias `list`), `run`, `report` and `accept` commands.
- Two ways to run tests: any shell command, or long-lived pytest workers that run only the tests reading
  the mutated file, forking when the file is read at import.
- Project copies per worker that share the project's virtual environment and import the project's own
  packages from the copy.
- `[tool.pysqlmut]` settings in `pyproject.toml`, `-- pysqlmut: skip` and `off`/`on` comments, and a file
  of accepted survivors.
- Survivors grouped by the shape of the line they changed.
