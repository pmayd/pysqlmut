"""Command line interface."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from pysqlmut.mutants import Mutant, generate
from pysqlmut.operators import ALL_OPERATORS
from pysqlmut.runner import SURVIVED, BaselineFailedError, Result, run, write_report
from pysqlmut.source import SqlSource


def _operators(args: argparse.Namespace) -> list[str] | None:
    return args.operators.split(",") if args.operators else None


def _list(args: argparse.Namespace) -> int:
    operators = _operators(args)
    produced: Counter[str] = Counter()
    rejected: Counter[tuple[str, str]] = Counter()
    for path in args.files:
        generation = generate(SqlSource.read(path, args.dialect), operators)
        produced.update(m.operator for m in generation.mutants)
        rejected.update(generation.rejected)
        print(f"{path}: {len(generation.source.statements)} statements, {len(generation.mutants)} mutants")
        if args.show:
            for mutant in generation.mutants:
                before, after = mutant.diff(generation.source.text)
                print(f"  {mutant.line:5} {mutant.operator:15} {mutant.description}")
                print(f"        - {before.strip()}")
                print(f"        + {after.strip()}")
    print("\nper operator: mutants / rejected")
    for name in operators or ALL_OPERATORS:
        reasons = {reason: count for (operator, reason), count in rejected.items() if operator == name}
        print(f"  {name:15} {produced[name]:5} / {sum(reasons.values()):<5} {reasons or ''}")
    return 0


def _run(args: argparse.Namespace) -> int:
    mutants: list[Mutant] = []
    for path in args.files:
        mutants.extend(generate(SqlSource.read(path, args.dialect), _operators(args)).mutants)
    print(f"{len(mutants)} mutants, {args.workers} workers: {args.command}", flush=True)

    done = 0

    def progress(result: Result) -> None:
        nonlocal done
        done += 1
        if done % 25 == 0 or done == len(mutants):
            print(f"  {done}/{len(mutants)}", flush=True)

    try:
        results = run(
            mutants, args.project, args.command, workers=args.workers, timeout=args.timeout, progress=progress
        )
    except BaselineFailedError as error:
        print(error, file=sys.stderr)
        return 2
    if args.report:
        write_report(results, args.report)

    by_operator: Counter[tuple[str, str]] = Counter((r.operator, r.status) for r in results)
    print("\nper operator: caught / survived / timeout")
    for name in sorted({r.operator for r in results}):
        caught, survived, timeout = (by_operator[name, s] for s in ("caught", "survived", "timeout"))
        print(f"  {name:15} {caught:5} / {survived:5} / {timeout:5}")
    survivors = [r for r in results if r.status == SURVIVED]
    print(f"\n{len(survivors)} of {len(results)} mutants survived")
    for result in survivors:
        print(f"  {result.path}:{result.line} {result.operator} ({result.description})")
        print(f"      - {result.before}")
        print(f"      + {result.after}")
    return 1 if survivors else 0


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--dialect", required=True, help="sqlglot dialect, e.g. snowflake, duckdb, bigquery")
    parser.add_argument("--operators", help=f"comma-separated subset of: {', '.join(ALL_OPERATORS)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pysqlmut", description="Mutation testing for SQL.")
    commands = parser.add_subparsers(dest="command_name", required=True)

    list_parser = commands.add_parser("list", help="generate and verify the mutants of SQL files")
    _add_selection(list_parser)
    list_parser.add_argument("--show", action="store_true", help="print every mutant with its line before and after")
    list_parser.set_defaults(handler=_list)

    run_parser = commands.add_parser("run", help="run a test command against every mutant")
    _add_selection(run_parser)
    run_parser.add_argument("--command", required=True, help="test command, run in a copy of the project")
    run_parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root to copy (default: .)")
    run_parser.add_argument("--workers", type=int, default=1, help="parallel copies of the project")
    run_parser.add_argument("--timeout", type=float, default=300.0, help="seconds before a run counts as timeout")
    run_parser.add_argument("--report", type=Path, help="write every result as JSON")
    run_parser.set_defaults(handler=_run)

    args = parser.parse_args(argv)
    return args.handler(args)
