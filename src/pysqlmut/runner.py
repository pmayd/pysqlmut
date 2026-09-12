"""Run a project's test command once per mutant, each in its own copy of the project."""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from pysqlmut.mutants import Mutant

# Copying these would be slow and is not needed: the copies share the project's virtual environment.
_NOT_COPIED = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules")

CAUGHT, SURVIVED, TIMEOUT = "caught", "survived", "timeout"


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


class BaselineFailedError(RuntimeError):
    """The test command fails on the unchanged project, so no mutant result would mean anything."""


def _run(command: str, cwd: Path, timeout: float) -> int | None:
    # uv must not re-sync the shared virtual environment into the copy.
    env = os.environ | {"UV_NO_SYNC": "1"}
    try:
        completed = subprocess.run(
            command, shell=True, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    return completed.returncode


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
            copies.append(copy)
        yield copies


def run(
    mutants: Sequence[Mutant],
    project: Path,
    command: str,
    *,
    workers: int = 1,
    timeout: float = 300.0,
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Run the command against every mutant and classify each as caught, survived or timed out."""
    project = project.resolve()
    # A mutant's path may be absolute or relative to the working directory; inside a copy it must be
    # relative to the project, or the mutant would be written into the real project.
    paths = {m.path: m.path.resolve().relative_to(project) for m in mutants}
    originals = {m.path: (project / paths[m.path]).read_text(encoding="utf-8") for m in mutants}
    with _copies(project, max(1, workers)) as copies:
        if _run(command, copies[0], timeout) != 0:
            raise BaselineFailedError(f"the test command fails on the unchanged project: {command}")
        free: queue.Queue[Path] = queue.Queue()
        for copy in copies:
            free.put(copy)

        def one(mutant: Mutant) -> Result:
            copy = free.get()
            target = copy / paths[mutant.path]
            original = originals[mutant.path]
            before, after = mutant.diff(original)
            started = time.monotonic()
            try:
                target.write_text(mutant.apply(original), encoding="utf-8")
                code = _run(command, copy, timeout)
            finally:
                target.write_text(original, encoding="utf-8")
                free.put(copy)
            status = TIMEOUT if code is None else CAUGHT if code != 0 else SURVIVED
            result = Result(
                str(paths[mutant.path]),
                mutant.line,
                mutant.operator,
                mutant.description,
                status,
                round(time.monotonic() - started, 2),
                before.strip(),
                after.strip(),
            )
            if progress:
                progress(result)
            return result

        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            return list(pool.map(one, mutants))


def write_report(results: Sequence[Result], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
