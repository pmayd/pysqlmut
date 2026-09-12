"""Command line interface."""

import argparse
from collections import Counter
from pathlib import Path

from pysqlmut.mutants import generate
from pysqlmut.operators import ALL_OPERATORS
from pysqlmut.source import SqlSource


def _list(args: argparse.Namespace) -> int:
    operators = args.operators.split(",") if args.operators else None
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pysqlmut", description="Mutation testing for SQL.")
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="generate and verify the mutants of SQL files")
    list_parser.add_argument("files", nargs="+", type=Path)
    list_parser.add_argument("--dialect", required=True, help="sqlglot dialect, e.g. snowflake, duckdb, bigquery")
    list_parser.add_argument("--operators", help=f"comma-separated subset of: {', '.join(ALL_OPERATORS)}")
    list_parser.add_argument("--show", action="store_true", help="print every mutant with its line before and after")
    list_parser.set_defaults(handler=_list)
    args = parser.parse_args(argv)
    return args.handler(args)
