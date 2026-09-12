"""Command line interface.

Exit codes: 0 when every mutant was caught or accepted, 1 when mutants survived, were not covered, timed out or
ended in an error, 2 on a usage or settings error, 3 when the tests cannot tell mutants apart (they fail on the
unchanged project, or read the project instead of its copy), 4 when pysqlmut itself fails, and 130 when a run
is interrupted.
"""

import shlex
import signal
import sys
import traceback
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError

from pysqlmut import accepted, report
from pysqlmut.config import Config, load_config
from pysqlmut.mutants import Generation, generate
from pysqlmut.operators import ALL_OPERATORS
from pysqlmut.runner import (
    ACCEPTED,
    CAUGHT,
    ERROR,
    NOT_COVERED,
    SURVIVED,
    TIMEOUT,
    Result,
    SetupError,
    run,
)
from pysqlmut.source import SqlSource

_STATUSES = (CAUGHT, SURVIVED, NOT_COVERED, ACCEPTED, TIMEOUT, ERROR)
_DEFAULT_ACCEPTED = "pysqlmut-accepted.json"
EXIT_FINDINGS, EXIT_USAGE, EXIT_SETUP, EXIT_CRASH, EXIT_INTERRUPTED = 1, 2, 3, 4, 130

app = typer.Typer(
    name="pysqlmut", help="Mutation testing for SQL.", no_args_is_help=True, pretty_exceptions_enable=False
)

Files = Annotated[
    list[Path] | None,
    # The backslash stops rich markup from reading [tool.pysqlmut] as a style tag.
    typer.Argument(help="SQL files. Default: the files setting in \\[tool.pysqlmut].", show_default=False),
]
Dialect_ = Annotated[str | None, typer.Option("--dialect", help="sqlglot dialect, e.g. snowflake, duckdb, bigquery.")]
Operators = Annotated[str | None, typer.Option(help=f"Comma-separated subset of: {', '.join(ALL_OPERATORS)}.")]
Project = Annotated[Path, typer.Option(help="Project root.", exists=True, file_okay=False)]
AcceptedFile = Annotated[
    Path | None,
    typer.Option(
        "--accepted",
        help=f"Accepted survivors file. Default: the accepted setting, or {_DEFAULT_ACCEPTED} in the project.",
    ),
]
Workers = Annotated[int | None, typer.Option(min=1, help="Processes that verify mutants, then parallel test runs.")]
Top = Annotated[int, typer.Option(min=0, help="How many groups to print.")]


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    typer.echo(f"pysqlmut: {message}", err=True)
    raise typer.Exit(code)


def _names(value: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in value.split(",") if name.strip())


def _config(project: Path, dialect: str | None, operators: str | None) -> Config:
    try:
        config = load_config(project)
        if dialect:
            config = replace(config, dialect=dialect)
        if operators is not None:
            config = replace(config, operators=_names(operators))
    except ValueError as error:
        _fail(str(error))
    return config


def _shown(path: Path) -> str:
    """A path relative to the working directory when it lies below it."""
    resolved, here = path.resolve(), Path.cwd().resolve()
    return str(resolved.relative_to(here)) if resolved.is_relative_to(here) else str(path)


def _accepted_path(project: Path, option: Path | None, config: Config) -> Path:
    """An --accepted path is relative to the working directory, a setting relative to the project."""
    return option if option is not None else project / (config.accepted or _DEFAULT_ACCEPTED)


def _generations(files: list[Path] | None, project: Path, config: Config, workers: int) -> Iterator[Generation]:
    if config.dialect is None:
        _fail("no dialect: pass --dialect or set dialect in [tool.pysqlmut]")
    try:
        Dialect.get_or_raise(config.dialect)
    except ValueError as error:
        _fail(str(error))
    project = project.resolve()
    paths = []
    for path in files or config.files_in(project):
        resolved = path.resolve()
        if not resolved.is_file():
            _fail(f"{path}: no such file")
        if not resolved.is_relative_to(project):
            _fail(f"{path} is outside the project {project}; pass --project")
        if resolved not in paths:
            paths.append(resolved)
    if not paths:
        _fail("no SQL files: pass them or set files in [tool.pysqlmut]")
    with ProcessPoolExecutor(workers) if workers > 1 else nullcontext() as executor:
        for path in paths:
            try:
                source = SqlSource.read(path, config.dialect)
            except (SqlglotError, UnicodeDecodeError) as error:
                typer.echo(f"{path}: skipped, cannot read it: {error}", err=True)
                continue
            yield generate(source, config.operators_for(path.relative_to(project), project), executor)


def generate_command(
    files: Files = None,
    dialect: Dialect_ = None,
    operators: Operators = None,
    project: Project = Path(),
    show: Annotated[bool, typer.Option(help="Print every mutant with its line before and after.")] = False,
    workers: Workers = None,
) -> None:
    """Generate and verify the mutants of SQL files without running tests.

    Mutants are not stored anywhere, so listing them means generating them.
    """
    config = _config(project, dialect, operators)
    produced: Counter[str] = Counter()
    rejected: Counter[tuple[str, str]] = Counter()
    for generation in _generations(files, project, config, workers if workers is not None else config.workers):
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


def _interrupt(signum: int, frame: object) -> NoReturn:
    raise KeyboardInterrupt


@app.command("run")
def run_command(
    files: Files = None,
    dialect: Dialect_ = None,
    operators: Operators = None,
    project: Project = Path(),
    command: Annotated[
        str | None,
        typer.Option(
            help="Shell command that runs your tests, e.g. 'uv run pytest' or 'python -m unittest'; started anew for "
            "every mutant.",
        ),
    ] = None,
    pytest: Annotated[
        str | None,
        typer.Option(
            metavar="PYTHON",
            help="Command that starts your project's Python, where pytest and your dependencies are installed, e.g. "
            "'uv run python' or '.venv/bin/python'. Keeps pytest running and runs only the tests that read the "
            "mutated file.",
        ),
    ] = None,
    tests: Annotated[list[str] | None, typer.Option(help="Pytest path to collect; repeatable.")] = None,
    pytest_args: Annotated[str | None, typer.Option(help="Extra pytest options.")] = None,
    workers: Workers = None,
    timeout: Annotated[float | None, typer.Option(min=1.0, help="Seconds before a run counts as timeout.")] = None,
    accepted_file: AcceptedFile = None,
    report_file: Annotated[Path | None, typer.Option("--report", help="Write every result as JSON.")] = None,
    top: Top = 30,
) -> None:
    """Run the tests against every mutant.

    Exit codes: 0 when every mutant was caught or accepted; 1 when mutants survived, were not covered, timed out or
    ended in an error; 2 on a usage or settings error; 3 when the tests fail on the unchanged project or read the
    project instead of its copy; 130 when interrupted.
    """
    if command is not None and pytest is not None:
        _fail("give either --command or --pytest, not both")
    config = _config(project, dialect, operators)
    command = command if command is not None else None if pytest is not None else config.command
    pytest_python = pytest if pytest is not None else None if command is not None else config.pytest_python
    if (command is None) == (pytest_python is None):
        _fail("give --command or --pytest, or set command or [tool.pysqlmut.pytest] python")
    project = project.resolve()
    workers = workers if workers is not None else config.workers
    # The generation processes end before the test workers start.
    mutants = [mutant for generation in _generations(files, project, config, workers) for mutant in generation.mutants]
    try:
        known = accepted.load(_accepted_path(project, accepted_file, config))
    except ValueError as error:
        _fail(str(error))
    how = command or f"pytest workers ({pytest_python})"
    typer.echo(f"{len(mutants)} mutants, {workers} worker{'' if workers == 1 else 's'}: {how}")

    done = 0

    def progress(result: Result) -> None:
        nonlocal done
        done += 1
        if done % 50 == 0 or done == len(mutants):
            typer.echo(f"  {done}/{len(mutants)}")

    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        results = run(
            mutants,
            project,
            command=command,
            pytest_python=pytest_python,
            tests=tests if tests is not None else config.tests,
            pytest_args=shlex.split(pytest_args) if pytest_args is not None else config.pytest_args,
            workers=workers,
            timeout=timeout if timeout is not None else config.timeout,
            accepted=known,
            progress=progress,
        )
    except SetupError as error:
        _fail(str(error), EXIT_SETUP)
    except ValueError as error:
        _fail(str(error))
    except KeyboardInterrupt:
        _fail("interrupted; the project is unchanged", EXIT_INTERRUPTED)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if report_file:
        report.save(results, report_file)
    if _summarize(results, top):
        raise typer.Exit(EXIT_FINDINGS)


def _summarize(results: list[Result], top: int) -> bool:
    """Print the counts and the survivor groups; true when anything needs attention."""
    counts: Counter[tuple[str, str]] = Counter((r.operator, r.status) for r in results)
    typer.echo("\nper operator: " + " / ".join(_STATUSES))
    for name in sorted({r.operator for r in results}):
        typer.echo(f"  {name:15} " + " / ".join(f"{counts[name, status]:5}" for status in _STATUSES))
    survivors = [r for r in results if r.status in {SURVIVED, NOT_COVERED}]
    groups = report.group(survivors)
    typer.echo(f"\n{len(survivors)} of {len(results)} mutants survived or were not covered, in {len(groups)} groups")
    if groups:
        typer.echo(report.render(groups, top))
    if any(r.status == NOT_COVERED for r in results):
        typer.echo(
            "\nNot covered means no test was seen reading the file. With --pytest, reads by subprocesses, "
            "pytest-xdist workers or native code are not seen; --command runs every test instead."
        )
    problems = sum(r.status in {TIMEOUT, ERROR} for r in results)
    if problems:
        typer.echo(
            f"\n{problems} mutants timed out or ended in an error: the tests did not finish, or pytest could not run "
            "them (for example no tests collected). Run the tests on one of these mutants to see why."
        )
    return bool(survivors or problems)


def _statuses(value: str) -> set[str]:
    statuses = set(_names(value))
    unknown = sorted(statuses - set(_STATUSES))
    if unknown:
        _fail(f"unknown statuses: {', '.join(unknown)}; known: {', '.join(_STATUSES)}")
    return statuses


def _load_report(path: Path) -> list[Result]:
    try:
        return report.load(path)
    except ValueError as error:
        _fail(str(error))


@app.command("report")
def report_command(
    report_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    status: Annotated[str, typer.Option(help="Comma-separated statuses.")] = f"{SURVIVED},{NOT_COVERED}",
    top: Top = 30,
) -> None:
    """Group the results of a JSON report by the code they changed."""
    statuses = _statuses(status)
    results = [r for r in _load_report(report_file) if r.status in statuses]
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
    chosen = set(_names(operators)) if operators is not None else None
    survivors = [
        r
        for r in _load_report(report_file)
        if r.status in {SURVIVED, NOT_COVERED} and (chosen is None or r.operator in chosen)
    ]
    path = _accepted_path(project.resolve(), accepted_file, _config(project, None, None))
    try:
        known = accepted.load(path)
    except ValueError as error:
        _fail(str(error))
    new = {accepted.of_result(r) for r in survivors} - known
    accepted.save(path, known | new)
    typer.echo(f"accepted {len(new)} more survivors; {_shown(path)} now holds {len(known | new)}")


def main() -> None:
    try:
        app()
    except Exception:  # noqa: BLE001 - any failure that reaches here is a bug in pysqlmut, reported with its trace
        traceback.print_exc()
        typer.echo("pysqlmut: internal error; please report it with the trace above", err=True)
        sys.exit(EXIT_CRASH)
