"""Command line interface."""

import shlex
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from sqlglot.errors import SqlglotError

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

app = typer.Typer(name="pysqlmut", help="Mutation testing for SQL.", no_args_is_help=True)

Files = Annotated[
    list[Path] | None,
    # The backslash stops rich markup from reading [tool.pysqlmut] as a style tag.
    typer.Argument(help="SQL files. Default: the files setting in \\[tool.pysqlmut].", show_default=False),
]
Dialect = Annotated[str | None, typer.Option(help="sqlglot dialect, e.g. snowflake, duckdb, bigquery.")]
Operators = Annotated[str | None, typer.Option(help=f"Comma-separated subset of: {', '.join(ALL_OPERATORS)}.")]
Project = Annotated[Path, typer.Option(help="Project root.", exists=True, file_okay=False)]
AcceptedFile = Annotated[
    str | None, typer.Option("--accepted", help=f"Accepted survivors file. Default: {_DEFAULT_ACCEPTED}.")
]
Top = Annotated[int, typer.Option(help="How many groups to print.")]


def _fail(message: str) -> typer.Exit:
    typer.echo(f"pysqlmut: {message}", err=True)
    return typer.Exit(2)


def _config(project: Path, dialect: str | None, operators: str | None) -> Config:
    try:
        config = load_config(project)
        if dialect:
            config = replace(config, dialect=dialect)
        if operators:
            config = replace(config, operators=tuple(operators.split(",")))
    except ValueError as error:
        raise _fail(str(error)) from error
    return config


def _generations(files: list[Path] | None, project: Path, config: Config) -> Iterator[Generation]:
    if config.dialect is None:
        raise _fail("no dialect: pass --dialect or set dialect in [tool.pysqlmut]")
    project = project.resolve()
    paths = [path.resolve() for path in files or config.files_in(project)]
    if not paths:
        raise _fail("no SQL files: pass them or set files in [tool.pysqlmut]")
    for path in paths:
        try:
            source = SqlSource.read(path, config.dialect)
        except SqlglotError as error:
            typer.echo(f"{path}: skipped, cannot tokenize: {error}", err=True)
            continue
        yield generate(source, config.operators_for(path.relative_to(project)))


def generate_command(
    files: Files = None,
    dialect: Dialect = None,
    operators: Operators = None,
    project: Project = Path(),
    show: Annotated[bool, typer.Option(help="Print every mutant with its line before and after.")] = False,
) -> None:
    """Generate and verify the mutants of SQL files without running tests.

    Mutants are not stored anywhere, so listing them means generating them.
    """
    config = _config(project, dialect, operators)
    produced: Counter[str] = Counter()
    rejected: Counter[tuple[str, str]] = Counter()
    for generation in _generations(files, project, config):
        produced.update(m.operator for m in generation.mutants)
        rejected.update(generation.rejected)
        source = generation.source
        typer.echo(f"{source.path}: {len(source.statements)} statements, {len(generation.mutants)} mutants")
        if show:
            for mutant in generation.mutants:
                before, after = mutant.diff(source.text)
                typer.echo(f"  {mutant.line:5} {mutant.operator:15} {mutant.description}")
                typer.echo(f"        - {before.strip()}")
                typer.echo(f"        + {after.strip()}")
    typer.echo("\nper operator: mutants / rejected")
    for name in ALL_OPERATORS:
        reasons = {reason: count for (operator, reason), count in rejected.items() if operator == name}
        if produced[name] or reasons:
            typer.echo(f"  {name:15} {produced[name]:5} / {sum(reasons.values()):<5} {reasons or ''}")


app.command("generate")(generate_command)
app.command("list", help="Alias for generate.")(generate_command)


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


@app.command("run")
def run_command(
    files: Files = None,
    dialect: Dialect = None,
    operators: Operators = None,
    project: Project = Path(),
    command: Annotated[
        str | None, typer.Option(help="Shell command that runs all tests; a new process per mutant.")
    ] = None,
    pytest: Annotated[
        str | None,
        typer.Option(
            metavar="PYTHON",
            help="The project's Python, e.g. 'uv run python': long-lived pytest workers that run only the tests "
            "reading the mutated file.",
        ),
    ] = None,
    tests: Annotated[list[str] | None, typer.Option(help="Pytest path to collect; repeatable.")] = None,
    pytest_args: Annotated[str | None, typer.Option(help="Extra pytest options.")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel copies of the project.")] = None,
    timeout: Annotated[float | None, typer.Option(help="Seconds before a run counts as timeout.")] = None,
    accepted_file: AcceptedFile = None,
    report_file: Annotated[Path | None, typer.Option("--report", help="Write every result as JSON.")] = None,
    top: Top = 30,
) -> None:
    """Run the tests against every mutant.

    Exits with 1 while survivors remain that nobody accepted, and with 2 when the tests fail without a mutant.
    """
    if command and pytest:
        raise _fail("give either --command or --pytest, not both")
    config = _config(project, dialect, operators)
    command = command or (None if pytest else config.command)
    pytest_python = pytest or (None if command else config.pytest_python)
    if (command is None) == (pytest_python is None):
        raise _fail("give --command or --pytest, or set command or [tool.pysqlmut.pytest] python")
    project = project.resolve()
    mutants = [mutant for generation in _generations(files, project, config) for mutant in generation.mutants]
    known = accepted.load(project / (accepted_file or config.accepted or _DEFAULT_ACCEPTED))
    skipped = _accepted_results(mutants, known, project)
    to_run = [mutant for position, mutant in enumerate(mutants) if position not in skipped]
    workers = workers or config.workers
    how = command or f"pytest workers ({pytest_python})"
    typer.echo(f"{len(to_run)} mutants to run ({len(skipped)} accepted), {workers} workers: {how}")

    done = 0

    def progress(result: Result) -> None:
        nonlocal done
        done += 1
        if done % 50 == 0 or done == len(to_run):
            typer.echo(f"  {done}/{len(to_run)}")

    try:
        ran = run(
            to_run,
            project,
            command=command,
            pytest_python=pytest_python,
            tests=tests or config.tests,
            pytest_args=shlex.split(pytest_args) if pytest_args is not None else config.pytest_args,
            workers=workers,
            timeout=timeout or config.timeout,
            progress=progress,
        )
    except BaselineFailedError as error:
        raise _fail(str(error)) from error
    ran_iter = iter(ran)
    results = [skipped[position] if position in skipped else next(ran_iter) for position in range(len(mutants))]
    if report_file:
        write_report(results, report_file)

    counts: Counter[tuple[str, str]] = Counter((r.operator, r.status) for r in results)
    typer.echo("\nper operator: " + " / ".join(_STATUSES))
    for name in sorted({r.operator for r in results}):
        typer.echo(f"  {name:15} " + " / ".join(f"{counts[name, status]:5}" for status in _STATUSES))
    survivors = [r for r in results if r.status in {SURVIVED, NOT_COVERED}]
    groups = report.group(survivors)
    typer.echo(f"\n{len(survivors)} of {len(results)} mutants survived or were not covered, in {len(groups)} groups")
    typer.echo(report.render(groups, top))
    if survivors:
        raise typer.Exit(1)


@app.command("report")
def report_command(
    report_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    status: Annotated[str, typer.Option(help="Comma-separated statuses.")] = f"{SURVIVED},{NOT_COVERED}",
    top: Top = 30,
) -> None:
    """Group the results of a JSON report by the code they changed."""
    statuses = set(status.split(","))
    results = [r for r in report.load(report_file) if r.status in statuses]
    groups = report.group(results)
    typer.echo(f"{len(results)} results with status {', '.join(sorted(statuses))}, in {len(groups)} groups")
    typer.echo(report.render(groups, top))


@app.command("accept")
def accept_command(
    report_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    operators: Annotated[
        str | None, typer.Option(help="Accept only survivors of these comma-separated operators.")
    ] = None,
    project: Project = Path(),
    accepted_file: AcceptedFile = None,
) -> None:
    """Accept the survivors of a JSON report after review; later runs neither rerun nor report them."""
    chosen = set(operators.split(",")) if operators else None
    survivors = [
        r
        for r in report.load(report_file)
        if r.status in {SURVIVED, NOT_COVERED} and (chosen is None or r.operator in chosen)
    ]
    path = project / (accepted_file or _config(project, None, None).accepted or _DEFAULT_ACCEPTED)
    known = accepted.load(path)
    new = {accepted.of_result(r) for r in survivors} - known
    accepted.save(path, known | new)
    typer.echo(f"accepted {len(new)} more survivors; {path} now holds {len(known | new)}")


def main() -> None:
    app()
