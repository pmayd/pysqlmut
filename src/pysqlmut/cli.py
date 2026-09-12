"""Command line interface."""

import argparse
import shlex
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from pysqlmut import accepted, report
from pysqlmut.config import Config, load_config
from pysqlmut.mutants import Generation, Mutant, generate
from pysqlmut.operators import ALL_OPERATORS
from pysqlmut.runner import (
    ACCEPTED,
    CAUGHT,
    ERROR,
    NOT_COVERED,
    SURVIVED,
    TIMEOUT,
    BaselineFailedError,
    Result,
    run,
    write_report,
)
from pysqlmut.source import SqlSource

_STATUSES = (CAUGHT, SURVIVED, NOT_COVERED, ACCEPTED, TIMEOUT, ERROR)
_DEFAULT_ACCEPTED = "pysqlmut-accepted.json"


class UsageError(Exception):
    """A missing or conflicting setting; printed without a traceback."""


def _config(args: argparse.Namespace) -> Config:
    config = load_config(args.project)
    overrides: dict[str, object] = {}
    if args.dialect:
        overrides["dialect"] = args.dialect
    if args.operators:
        overrides["operators"] = tuple(args.operators.split(","))
    return replace(config, **overrides)


def _generations(args: argparse.Namespace, config: Config) -> Iterator[Generation]:
    if config.dialect is None:
        raise UsageError("no dialect: pass --dialect or set dialect in [tool.pysqlmut]")
    project = args.project.resolve()
    files = [path.resolve() for path in args.files] or [path.resolve() for path in config.files_in(project)]
    if not files:
        raise UsageError("no SQL files: pass them or set files in [tool.pysqlmut]")
    for path in files:
        yield generate(SqlSource.read(path, config.dialect), config.operators_for(path.relative_to(project)))


def _list(args: argparse.Namespace) -> int:
    config = _config(args)
    produced: Counter[str] = Counter()
    rejected: Counter[tuple[str, str]] = Counter()
    for generation in _generations(args, config):
        produced.update(m.operator for m in generation.mutants)
        rejected.update(generation.rejected)
        source = generation.source
        print(f"{source.path}: {len(source.statements)} statements, {len(generation.mutants)} mutants")
        if args.show:
            for mutant in generation.mutants:
                before, after = mutant.diff(source.text)
                print(f"  {mutant.line:5} {mutant.operator:15} {mutant.description}")
                print(f"        - {before.strip()}")
                print(f"        + {after.strip()}")
    print("\nper operator: mutants / rejected")
    for name in ALL_OPERATORS:
        reasons = {reason: count for (operator, reason), count in rejected.items() if operator == name}
        if produced[name] or reasons:
            print(f"  {name:15} {produced[name]:5} / {sum(reasons.values()):<5} {reasons or ''}")
    return 0


def _accepted_results(mutants: list[Mutant], known: set[accepted.Fingerprint], project: Path) -> dict[int, Result]:
    """Results for the mutants that match an accepted survivor, keyed by their position."""
    texts: dict[Path, str] = {}
    results = {}
    for position, mutant in enumerate(mutants):
        path = mutant.path.resolve()
        text = texts.setdefault(path, path.read_text(encoding="utf-8"))
        before, after = mutant.diff(text)
        relative = path.relative_to(project).as_posix()
        if accepted.fingerprint(relative, mutant.operator, mutant.description, before, after) in known:
            results[position] = Result(
                relative,
                mutant.line,
                mutant.operator,
                mutant.description,
                ACCEPTED,
                0.0,
                before.strip(),
                after.strip(),
                0,
            )
    return results


def _run(args: argparse.Namespace) -> int:
    config = _config(args)
    command = args.command or (None if args.pytest else config.command)
    pytest_python = args.pytest or (None if args.command else config.pytest_python)
    if (command is None) == (pytest_python is None):
        raise UsageError("give --command or --pytest, or set command or [tool.pysqlmut.pytest] python")
    project = args.project.resolve()
    mutants = [mutant for generation in _generations(args, config) for mutant in generation.mutants]
    accepted_file = project / (args.accepted or config.accepted or _DEFAULT_ACCEPTED)
    skipped = _accepted_results(mutants, accepted.load(accepted_file), project)
    to_run = [mutant for position, mutant in enumerate(mutants) if position not in skipped]
    workers = args.workers or config.workers
    how = command or f"pytest workers ({pytest_python})"
    print(f"{len(to_run)} mutants to run ({len(skipped)} accepted), {workers} workers: {how}", flush=True)

    done = 0

    def progress(result: Result) -> None:
        nonlocal done
        done += 1
        if done % 50 == 0 or done == len(to_run):
            print(f"  {done}/{len(to_run)}", flush=True)

    try:
        ran = run(
            to_run,
            project,
            command=command,
            pytest_python=pytest_python,
            tests=args.tests if args.tests is not None else config.tests,
            pytest_args=shlex.split(args.pytest_args) if args.pytest_args is not None else config.pytest_args,
            workers=workers,
            timeout=args.timeout or config.timeout,
            progress=progress,
        )
    except BaselineFailedError as error:
        print(error, file=sys.stderr)
        return 2
    ran_iter = iter(ran)
    results = [skipped[position] if position in skipped else next(ran_iter) for position in range(len(mutants))]
    if args.report:
        write_report(results, args.report)

    counts: Counter[tuple[str, str]] = Counter((r.operator, r.status) for r in results)
    print("\nper operator: " + " / ".join(_STATUSES))
    for name in sorted({r.operator for r in results}):
        print(f"  {name:15} " + " / ".join(f"{counts[name, status]:5}" for status in _STATUSES))
    survivors = [r for r in results if r.status in {SURVIVED, NOT_COVERED}]
    groups = report.group(survivors)
    print(f"\n{len(survivors)} of {len(results)} mutants survived or were not covered, in {len(groups)} groups")
    print(report.render(groups, args.top))
    return 1 if survivors else 0


def _report(args: argparse.Namespace) -> int:
    statuses = set(args.status.split(","))
    results = [r for r in report.load(args.report_file) if r.status in statuses]
    groups = report.group(results)
    print(f"{len(results)} results with status {', '.join(sorted(statuses))}, in {len(groups)} groups")
    print(report.render(groups, args.top))
    return 0


def _accept(args: argparse.Namespace) -> int:
    operators = set(args.operators.split(",")) if args.operators else None
    survivors = [
        r
        for r in report.load(args.report_file)
        if r.status in {SURVIVED, NOT_COVERED} and (operators is None or r.operator in operators)
    ]
    config = load_config(args.project)
    path = args.project / (args.accepted or config.accepted or _DEFAULT_ACCEPTED)
    known = accepted.load(path)
    new = {accepted.of_result(r) for r in survivors} - known
    accepted.save(path, known | new)
    print(f"accepted {len(new)} more survivors; {path} now holds {len(known | new)}")
    return 0


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", nargs="*", type=Path, help="SQL files (default: files in [tool.pysqlmut])")
    parser.add_argument("--dialect", help="sqlglot dialect, e.g. snowflake, duckdb, bigquery")
    parser.add_argument("--operators", help=f"comma-separated subset of: {', '.join(ALL_OPERATORS)}")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root (default: .)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pysqlmut", description="Mutation testing for SQL.")
    commands = parser.add_subparsers(dest="command_name", required=True)

    list_parser = commands.add_parser("list", help="generate and verify the mutants of SQL files")
    _add_selection(list_parser)
    list_parser.add_argument("--show", action="store_true", help="print every mutant with its line before and after")
    list_parser.set_defaults(handler=_list)

    run_parser = commands.add_parser("run", help="run the tests against every mutant")
    _add_selection(run_parser)
    how = run_parser.add_mutually_exclusive_group()
    how.add_argument("--command", help="shell command that runs all tests; a new process per mutant")
    how.add_argument(
        "--pytest",
        metavar="PYTHON",
        help="the project's Python, e.g. 'uv run python': long-lived pytest workers that run only the tests "
        "reading the mutated file",
    )
    run_parser.add_argument("--tests", nargs="*", help="pytest paths to collect (with --pytest)")
    run_parser.add_argument("--pytest-args", help="extra pytest options (with --pytest)")
    run_parser.add_argument("--workers", type=int, help="parallel copies of the project")
    run_parser.add_argument("--timeout", type=float, help="seconds before a run counts as timeout")
    run_parser.add_argument("--accepted", help=f"accepted survivors file (default: {_DEFAULT_ACCEPTED})")
    run_parser.add_argument("--report", type=Path, help="write every result as JSON")
    run_parser.add_argument("--top", type=int, default=30, help="how many survivor groups to print")
    run_parser.set_defaults(handler=_run)

    report_parser = commands.add_parser("report", help="group the results of a JSON report")
    report_parser.add_argument("report_file", type=Path)
    report_parser.add_argument("--status", default=f"{SURVIVED},{NOT_COVERED}", help="comma-separated statuses")
    report_parser.add_argument("--top", type=int, default=30, help="how many groups to print")
    report_parser.set_defaults(handler=_report)

    accept_parser = commands.add_parser("accept", help="accept the survivors of a JSON report after review")
    accept_parser.add_argument("report_file", type=Path)
    accept_parser.add_argument("--operators", help="accept only survivors of these comma-separated operators")
    accept_parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root (default: .)")
    accept_parser.add_argument("--accepted", help=f"accepted survivors file (default: {_DEFAULT_ACCEPTED})")
    accept_parser.set_defaults(handler=_accept)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (UsageError, ValueError) as error:
        print(f"pysqlmut: {error}", file=sys.stderr)
        return 2
