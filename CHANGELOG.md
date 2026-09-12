# Changelog

All notable changes to pysqlmut are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the command line, the
settings or the report format.

## [Unreleased]

## [0.1.0] - 2026-09-13

First release.

### Added

- Mutant generation for every dialect sqlglot parses. A mutant is a text patch on one expression, kept only
  when the patched statement parses to exactly the intended change; comments, formatting and line endings
  stay. The same SQL and settings always give the same mutants.
- Operators: `comparison`, `logical`, `drop-condition`, `is-null`, `aggregate`, `coalesce`, `order`, `union`,
  `join-type`, `arithmetic`, `literal`, `distinct`, `case`, `string-literal`, `column`.
- UNION ALL and UNION changes are left out when every branch returns unique rows that no other branch can
  return.
- Commands `generate` (with the permanent alias `list`), `run`, `report` and `accept`, and exit codes that
  separate findings, usage errors, setup problems, internal errors and interruptions.
- Two ways to run tests: any shell command, or long-lived pytest workers that run only the tests reading the
  mutated file, directly or through a fixture, each mutant in a fork that imports the project's modules again.
- Project copies per worker that share the project's virtual environment and import the project's own
  packages from the copy. A run stops early when the tests fail on the unchanged project or read the project
  instead of its copy, and an interrupted or timed-out run ends the processes it started.
- `[tool.pysqlmut]` settings in `pyproject.toml` with checked types, `-- pysqlmut: skip` and `off`/`on`
  comments, and a file of accepted survivors.
- Survivors grouped by the shape of the lines they changed, and versioned JSON reports.
- A getting-started guide with an example project, a roadmap, and a security policy.

[Unreleased]: https://github.com/pmayd/pysqlmut/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pmayd/pysqlmut/releases/tag/v0.1.0
