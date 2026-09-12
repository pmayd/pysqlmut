"""Run tests against every mutant, each mutant in one of several copies of the project.

Two ways to run tests:
- A shell command: every mutant starts a new process and runs whatever the command runs.
- Pytest workers: one long-lived pytest process per copy. A recording baseline run notes which
  test reads which mutated file, and each mutant then runs only the tests that read its file.
"""

import json
import os
import queue
import select
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from pysqlmut.mutants import Mutant

# Not copied: the copies share the project's virtual environment, and the rest is cache.
_NOT_COPIED = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules")
_WORKER_SCRIPT = Path(__file__).with_name("pytest_worker.py")

CAUGHT, SURVIVED, TIMEOUT, ERROR, NOT_COVERED = "caught", "survived", "timeout", "error", "not covered"
# Pytest exit codes: 1 tests failed, 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected.
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
    # How many tests the mutant ran: None means all of them (and reports written before selection existed).
    tests: int | None = None


class BaselineFailedError(RuntimeError):
    """The tests fail on the unchanged project, so no mutant result would mean anything."""


@dataclass(frozen=True)
class Selection:
    """Which tests a mutant of a file runs, and whether it needs a fresh process."""

    tests: tuple[str, ...] | None
    fresh: bool = False


def _env() -> dict[str, str]:
    # uv must not re-sync the shared virtual environment into a copy, and pysqlmut's own virtual
    # environment must not leak into the tested project's commands.
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    return env | {"UV_NO_SYNC": "1"}


class Worker(Protocol):
    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None: ...
    def run(self, selection: Selection) -> str: ...
    def close(self) -> None: ...


class CommandWorker:
    """Runs a shell command in a new process for every mutant."""

    def __init__(self, copy: Path, command: str, timeout: float) -> None:
        self.copy, self.command, self.timeout = copy, command, timeout

    def _run(self) -> int | None:
        try:
            completed = subprocess.run(
                self.command,
                shell=True,
                cwd=self.copy,
                env=_env(),
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        return completed.returncode

    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None:
        if self._run() != 0:
            raise BaselineFailedError(f"the test command fails on the unchanged project: {self.command}")
        return None

    def run(self, selection: Selection) -> str:
        code = self._run()
        return TIMEOUT if code is None else CAUGHT if code != 0 else SURVIVED

    def close(self) -> None:
        pass


class PytestWorker:
    """Keeps one pytest process alive and runs the selected tests for each mutant."""

    def __init__(self, copy: Path, python: str, tests: Sequence[str], args: Sequence[str], timeout: float) -> None:
        self.copy, self.tests, self.args, self.timeout = copy, list(tests), list(args), timeout
        self.command = [*shlex.split(python), str(_WORKER_SCRIPT)]
        self.process: subprocess.Popen[str] | None = None

    def _start(self) -> subprocess.Popen[str]:
        if self.process is None:
            self.process = subprocess.Popen(
                self.command, cwd=self.copy, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
            )
        return self.process

    def _request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        process = self._start()
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], self.timeout)
        line = process.stdout.readline() if ready else ""
        if not line:
            self.close()
            return None
        return json.loads(line)

    def baseline(self, watched: Sequence[Path]) -> dict[str, list[str]] | None:
        request = {"args": [*self.tests, *self.args], "record": True, "watch": [str(p) for p in watched]}
        response = self._request(request)
        if response is None or response["exit"] != 0:
            raise BaselineFailedError(f"pytest fails on the unchanged project: {' '.join(request['args'])}")
        return response["reads"]

    def run(self, selection: Selection) -> str:
        if selection.fresh:
            self.close()
        tests = self.tests if selection.tests is None else list(selection.tests)
        response = self._request({"args": [*tests, *self.args]})
        if response is None:
            return TIMEOUT
        if response["exit"] == 0:
            return SURVIVED
        return CAUGHT if response["exit"] in _CAUGHT_EXIT_CODES else ERROR

    def close(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process.wait()
            self.process = None


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


def _selections(reads: dict[str, list[str]] | None, copy: Path, files: Sequence[Path]) -> dict[Path, Selection | None]:
    """Per file: the tests that read it, all tests in a fresh process, or None when no test reads it."""
    if reads is None:
        return dict.fromkeys(files, Selection(None))
    readers: dict[Path, set[str]] = {}
    for nodeid, paths in reads.items():
        for path in paths:
            readers.setdefault(Path(path).relative_to(copy), set()).add(nodeid)
    selections: dict[Path, Selection | None] = {}
    for file in files:
        nodeids = readers.get(file)
        if not nodeids:
            selections[file] = None
        elif "" in nodeids:
            # Read while modules were imported: a warm process would keep the unchanged text.
            selections[file] = Selection(None, fresh=True)
        else:
            selections[file] = Selection(tuple(sorted(nodeids)))
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
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Run the tests against every mutant and classify each one."""
    if (command is None) == (pytest_python is None):
        raise ValueError("give either a shell command or a pytest Python, not both")
    project = project.resolve()
    # A mutant's path may be absolute or relative to the working directory; inside a copy it must be
    # relative to the project, or the mutant would be written into the real project.
    paths = {m.path: m.path.resolve().relative_to(project) for m in mutants}
    originals = {m.path: (project / paths[m.path]).read_text(encoding="utf-8") for m in mutants}
    files = sorted(set(paths.values()))

    with _copies(project, max(1, workers)) as copies, ExitStack() as stack:
        pool_workers: list[Worker] = []
        for copy in copies:
            worker: Worker = (
                CommandWorker(copy, command, timeout)
                if command is not None
                else PytestWorker(copy, pytest_python or "", tests, pytest_args, timeout)
            )
            stack.callback(worker.close)
            pool_workers.append(worker)
        reads = pool_workers[0].baseline([copies[0] / file for file in files])
        selections = _selections(reads, copies[0], files)

        free: queue.Queue[tuple[Worker, Path]] = queue.Queue()
        for worker, copy in zip(pool_workers, copies, strict=True):
            free.put((worker, copy))

        def one(mutant: Mutant) -> Result:
            relative, original = paths[mutant.path], originals[mutant.path]
            before, after = mutant.diff(original)
            selection = selections[relative]
            started = time.monotonic()
            if selection is None:
                status = NOT_COVERED
            else:
                worker, copy = free.get()
                target = copy / relative
                try:
                    target.write_text(mutant.apply(original), encoding="utf-8")
                    status = worker.run(selection)
                finally:
                    target.write_text(original, encoding="utf-8")
                    free.put((worker, copy))
            result = Result(
                str(relative),
                mutant.line,
                mutant.operator,
                mutant.description,
                status,
                round(time.monotonic() - started, 2),
                before.strip(),
                after.strip(),
                None if selection is None or selection.tests is None else len(selection.tests),
            )
            if progress:
                progress(result)
            return result

        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            return list(pool.map(one, mutants))


def write_report(results: Sequence[Result], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
