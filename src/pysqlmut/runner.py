"""Run tests against every mutant, each mutant in one of several copies of the project.

Two ways to run tests:
- A shell command: every mutant starts a new process and runs whatever the command runs.
- Pytest workers: one long-lived pytest process per copy. A recording baseline run notes which test reads which
  mutated file, and each mutant then runs only the tests that read its file, in a fork of the worker.
"""

import json
import os
import queue
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pysqlmut import accepted as accepted_survivors
from pysqlmut.config import IGNORED_DIRECTORIES
from pysqlmut.mutants import Mutant
from pysqlmut.source import read_text

# Not copied: the copies share the project's virtual environment, and the rest is cache.
_NOT_COPIED = shutil.ignore_patterns(*IGNORED_DIRECTORIES)
_WORKER_SCRIPT = Path(__file__).with_name("pytest_worker.py")
# Starting the tests for the first time includes starting Python and collecting every test.
_BASELINE_TIMEOUT = 600.0

CAUGHT, SURVIVED, TIMEOUT, ERROR, NOT_COVERED = "caught", "survived", "timeout", "error", "not covered"
# A survivor the team accepted earlier; it is not run again.
ACCEPTED = "accepted"
# Pytest exit codes: 1 tests failed, 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected. An
# interruption or internal error is how a mutant that breaks a test module or fixture shows up, so it counts as
# caught; a usage error or no tests means the run did not test the mutant at all.
_CAUGHT_EXIT_CODES = {1, 2, 3}


@dataclass(frozen=True)
class Result:
    path: str
    line: int
    operator: str
    description: str
    status: str
    seconds: float
    before: str
    after: str
    # How many tests the mutant ran; None means every test the command or the test paths collect.
    tests: int | None = None


class SetupError(RuntimeError):
    """The tests cannot tell mutants apart in this setup, so no result would mean anything."""


class BaselineFailedError(SetupError):
    """The tests fail on the unchanged project."""


class ProjectReadError(SetupError):
    """The tests read the project itself instead of the copy that holds the mutant."""


class WorkerStoppedError(RuntimeError):
    """The run was interrupted and the worker takes no more mutants."""


@dataclass(frozen=True)
class Selection:
    """The tests a mutant of a file runs; None means all of them."""

    tests: tuple[str, ...] | None


def _env(project: Path, copy: Path) -> dict[str, str]:
    """The environment of the tests in a copy.

    uv must not re-sync the shared virtual environment into a copy, and pysqlmut's own virtual environment
    must not leak into the tested project's commands. Import paths that point into the project, from
    PYTHONPATH or from an editable install, are pointed at the copy first; otherwise the tests would import
    the unchanged files of the project.
    """
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    current = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry]
    entries = list(current)
    for pth in (project / ".venv").glob("lib/python*/site-packages/*.pth"):
        entries += [line for line in pth.read_text(encoding="utf-8").splitlines() if not line.startswith("import")]
    moved = []
    for entry in entries:
        path = Path(entry.strip()).resolve()
        if entry.strip() and path.is_relative_to(project):
            moved.append(str(copy / path.relative_to(project)))
    if moved:
        env["PYTHONPATH"] = os.pathsep.join([*moved, *current])
    return env | {"UV_NO_SYNC": "1"}


def _kill(process: subprocess.Popen[Any]) -> None:
    """End a process started in a session of its own, together with everything it started."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _tail(output: bytes, lines: int = 20) -> str:
    return "\n".join(output.decode(errors="replace").splitlines()[-lines:])


class Worker(Protocol):
    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None: ...
    def run(self, selection: Selection) -> str: ...
    def stop(self) -> None: ...


class CommandWorker:
    """Runs a shell command in a new process for every mutant."""

    def __init__(self, copy: Path, env: dict[str, str], command: str, timeout: float) -> None:
        self.copy, self.env, self.command, self.timeout = copy, env, command, timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.stopped = False

    def _run(self, timeout: float) -> tuple[int | None, bytes]:
        if self.stopped:
            raise WorkerStoppedError
        # A session of its own, so that a timeout also ends the processes the command started.
        process = subprocess.Popen(  # noqa: S602 - the user's own test command, run as a shell command on purpose
            self.command,
            shell=True,
            cwd=self.copy,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.process = process
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            return None, b""
        finally:
            self.process = None
        return process.returncode, output

    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None:
        timeout = max(self.timeout, _BASELINE_TIMEOUT)
        code, output = self._run(timeout)
        if code is None:
            raise BaselineFailedError(f"the test command does not finish within {timeout:g} seconds: {self.command}")
        if code != 0:
            raise BaselineFailedError(
                f"the test command fails on the unchanged project (exit code {code}): {self.command}\n{_tail(output)}"
            )
        return None

    def run(self, selection: Selection) -> str:
        code, _ = self._run(self.timeout)
        return TIMEOUT if code is None else CAUGHT if code != 0 else SURVIVED

    def stop(self) -> None:
        self.stopped = True
        process = self.process
        if process is not None:
            _kill(process)


class PytestWorker:
    """Keeps one pytest process alive and runs the selected tests for each mutant in a fork of it."""

    def __init__(
        self,
        copy: Path,
        env: dict[str, str],
        *,
        script: Path,
        python: str,
        tests: Sequence[str],
        args: Sequence[str],
        timeout: float,
    ) -> None:
        self.copy, self.env, self.tests, self.args, self.timeout = copy, env, list(tests), list(args), timeout
        self.python = python
        self.command = [*shlex.split(python), str(script)]
        self.process: subprocess.Popen[str] | None = None
        self.stopped = False

    def _start(self) -> subprocess.Popen[str]:
        if self.stopped:
            raise WorkerStoppedError
        if self.process is None:
            # A session of its own, so that stopping the worker also ends the forks it started.
            self.process = subprocess.Popen(  # noqa: S603 - the project's own Python, as configured by the user
                self.command,
                cwd=self.copy,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        return self.process

    def _request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        process = self._start()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("the pytest worker was started without pipes")
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except BrokenPipeError:
            self._close()
            return None
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        line = process.stdout.readline() if ready else ""
        if not line:
            self._close()
            return None
        return json.loads(line)

    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None:
        args = [*self.tests, *self.args]
        request = {"args": args, "record": True, "watch": [str(p) for p in watched]}
        response = self._request(request, max(self.timeout, _BASELINE_TIMEOUT))
        rerun = f"{self.python} -m pytest {shlex.join(args)}"
        if response is None:
            raise BaselineFailedError(f"pytest does not finish on the unchanged project; try: {rerun}")
        if response["exit"] != 0:
            raise BaselineFailedError(
                f"pytest fails on the unchanged project (exit code {response['exit']}); run it to see why: {rerun}"
            )
        return response["reads"]

    def run(self, selection: Selection) -> str:
        tests = self.tests if selection.tests is None else list(selection.tests)
        response = self._request({"args": [*tests, *self.args]}, self.timeout)
        if response is None:
            if self.stopped:
                raise WorkerStoppedError
            return TIMEOUT
        if response["exit"] == 0:
            return SURVIVED
        return CAUGHT if response["exit"] in _CAUGHT_EXIT_CODES else ERROR

    def _close(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            _kill(process)

    def stop(self) -> None:
        self.stopped = True
        self._close()


@contextmanager
def _copies(project: Path, count: int) -> Iterator[list[Path]]:
    """Copies of the project that share its virtual environment."""
    with tempfile.TemporaryDirectory(prefix="pysqlmut-") as root:
        copies = []
        for index in range(count):
            copy = Path(root) / f"worker-{index}"
            shutil.copytree(project, copy, ignore=_NOT_COPIED, symlinks=True)
            if (project / ".venv").exists():
                (copy / ".venv").symlink_to(project / ".venv")
            copies.append(copy.resolve())
        yield copies


def _relative(path: Path, project: Path) -> Path:
    """A mutated file's path relative to the project, refusing files a copy would not hold."""
    resolved = path.resolve()
    if not resolved.is_relative_to(project):
        raise ValueError(f"{path} is outside the project {project}")
    relative = resolved.relative_to(project)
    if set(relative.parts) & set(IGNORED_DIRECTORIES):
        raise ValueError(f"{path} is in a directory that project copies leave out ({', '.join(IGNORED_DIRECTORIES)})")
    return relative


def _selections(
    reads: dict[str, list[str]] | None, copy: Path, project: Path, files: Sequence[Path]
) -> dict[Path, Selection | None]:
    """Per file: the tests that read it, all tests, or None when no test reads it."""
    if reads is None:
        return dict.fromkeys(files, Selection(None))
    readers: dict[Path, set[str]] = {}
    outside: dict[Path, set[str]] = {}
    for test, paths in reads.items():
        for path in map(Path, paths):
            if path.is_relative_to(copy):
                readers.setdefault(path.relative_to(copy), set()).add(test)
            elif path.is_relative_to(project):
                outside.setdefault(path.relative_to(project), set()).add(test)
    if outside:
        found = "; ".join(
            f"{file} by {', '.join(sorted(tests)) or 'module imports'}" for file, tests in outside.items()
        )
        raise ProjectReadError(
            f"tests read the project itself instead of the copy that holds the mutant: {found}. Check for an "
            "absolute path to the project, or a package installed from the project outside its .venv."
        )
    selections: dict[Path, Selection | None] = {}
    for file in files:
        tests = readers.get(file)
        if not tests:
            selections[file] = None
        elif "" in tests:
            # Read while modules were imported, so any test may depend on the text.
            selections[file] = Selection(None)
        else:
            selections[file] = Selection(tuple(sorted(tests)))
    return selections


def run(
    mutants: Sequence[Mutant],
    project: Path,
    *,
    command: str | None = None,
    pytest_python: str | None = None,
    tests: Sequence[str] = (),
    pytest_args: Sequence[str] = (),
    workers: int = 1,
    timeout: float = 300.0,
    accepted: Collection[accepted_survivors.Fingerprint] = frozenset(),
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Run the tests against every mutant and classify each one, in the order of the mutants."""
    if (command is None) == (pytest_python is None):
        raise ValueError("give exactly one of a shell command and a pytest Python")
    project = project.resolve()
    # A mutant's path may be absolute or relative to the working directory; inside a copy it must be
    # relative to the project, or the mutant would be written into the real project.
    paths = {m.path: _relative(m.path, project) for m in mutants}
    originals = {m.path: read_text(project / paths[m.path]) for m in mutants}
    files = sorted(set(paths.values()))

    with _copies(project, max(1, workers)) as copies, ExitStack() as stack:
        if command is not None:
            pool_workers: list[Worker] = [CommandWorker(copy, _env(project, copy), command, timeout) for copy in copies]
        else:
            # Next to the copies rather than in pysqlmut's package, whose modules would shadow the project's.
            script = copies[0].parent / "_pysqlmut_pytest_worker.py"
            shutil.copyfile(_WORKER_SCRIPT, script)
            pool_workers = [
                PytestWorker(
                    copy,
                    _env(project, copy),
                    script=script,
                    python=pytest_python or "",
                    tests=tests,
                    args=pytest_args,
                    timeout=timeout,
                )
                for copy in copies
            ]
        for worker in pool_workers:
            stack.callback(worker.stop)
        watched = [root / file for root in (copies[0], project) for file in files]
        selections = _selections(pool_workers[0].baseline(watched), copies[0], project, files)

        free: queue.Queue[tuple[Worker, Path]] = queue.Queue()
        for worker, copy in zip(pool_workers, copies, strict=True):
            free.put((worker, copy))

        def one(mutant: Mutant) -> Result:
            relative, original = paths[mutant.path], originals[mutant.path]
            before, after = mutant.diff(original)
            selection = selections[relative]
            started = time.monotonic()
            count: int | None = 0
            if accepted_survivors.fingerprint(
                relative.as_posix(), mutant.operator, mutant.description, before, after
            ) in (accepted):
                status = ACCEPTED
            elif selection is None:
                status = NOT_COVERED
            else:
                count = None if selection.tests is None else len(selection.tests)
                worker, copy = free.get()
                target = copy / relative
                try:
                    target.write_text(mutant.apply(original), encoding="utf-8", newline="")
                    status = worker.run(selection)
                finally:
                    target.write_text(original, encoding="utf-8", newline="")
                    free.put((worker, copy))
            result = Result(
                relative.as_posix(),
                mutant.line,
                mutant.operator,
                mutant.description,
                status,
                round(time.monotonic() - started, 2),
                before.strip(),
                after.strip(),
                count,
            )
            if progress:
                progress(result)
            return result

        return _map_or_stop(one, mutants, pool_workers)


def _map_or_stop(one: Callable[[Mutant], Result], mutants: Sequence[Mutant], workers: Sequence[Worker]) -> list[Result]:
    """Run every mutant, one thread per worker; on an error or an interruption, stop the test processes first."""
    pool = ThreadPoolExecutor(max_workers=len(workers))
    try:
        return list(pool.map(one, mutants))
    except BaseException:
        # Stopped first, so that no thread keeps waiting for a test that will not finish.
        for worker in workers:
            worker.stop()
        raise
    finally:
        pool.shutdown(cancel_futures=True)
