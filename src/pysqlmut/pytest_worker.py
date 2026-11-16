"""Run pytest sessions on request inside one long-lived process.

pysqlmut starts a copy of this file with the tested project's own Python, so it may import only the standard
library and pytest, and it must run on older Python versions than pysqlmut itself (3.9 and newer). Each request
is one JSON line on stdin and gets one JSON line back on the original stdout; pytest's own output goes to
/dev/null.

Requests:
- {"args": [...], "record": true, "watch": [paths]} runs pytest in this process and answers which watched files
  each test read: {"exit": code, "reads": {test id: [paths]}}. A read while a fixture is set up counts for every
  test that uses the fixture; a read outside any test, for example while modules are imported, is listed under
  the empty test id. Test ids are relative to the working directory, so they can be passed back as arguments.
- {"args": [...]} runs pytest in a fork and answers {"exit": code, "failed": [test ids]}, listing every test
  that failed in its setup, its call or its teardown.

Every run after the recording happens in a fork that no longer holds the project's modules. Files read at
import, cached by a function or kept by a fixture are therefore read again, while third-party modules stay
loaded, which saves starting Python and importing them for every run.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


def _test_id(item: pytest.Item) -> str:
    """The item's test id with its file relative to the working directory instead of pytest's rootdir."""
    _, _, rest = item.nodeid.partition("::")
    local = os.path.relpath(str(item.path))
    return f"{local}::{rest}" if rest else local


class _ReadRecorder:
    """Pytest plugin that notes which watched files each test reads, directly or through its fixtures."""

    def __init__(self, watched: set[str]) -> None:
        self.watched = watched
        self.current = ""
        self.fixtures: list[str] = []
        self.reads: dict[str, set[str]] = {}
        self.fixture_reads: dict[str, set[str]] = {}

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, Any, Any]:
        self.current = _test_id(item)
        try:
            return (yield)
        finally:
            # A fixture set up for an earlier test is reused without reading its files again.
            for name in getattr(item, "fixturenames", ()):
                self.reads.setdefault(self.current, set()).update(self.fixture_reads.get(name, ()))
            self.current = ""

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef: Any) -> Generator[None, Any, Any]:
        self.fixtures.append(fixturedef.argname)
        try:
            return (yield)
        finally:
            self.fixtures.pop()

    def saw(self, file: Any) -> None:
        try:
            path = os.path.realpath(os.fspath(file))
        except TypeError:
            return
        if path not in self.watched:
            return
        self.reads.setdefault(self.current, set()).add(path)
        for name in self.fixtures:
            self.fixture_reads.setdefault(name, set()).add(path)


class _FailureRecorder:
    """Pytest plugin that notes every test that fails, whether in its setup, its call or its teardown."""

    def __init__(self) -> None:
        self.current = ""
        self.failed: set[str] = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, Any, Any]:
        self.current = _test_id(item)
        try:
            return (yield)
        finally:
            self.current = ""

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed and self.current:
            self.failed.add(self.current)


def _loaded_from(module: Any, root: str) -> bool:
    paths = [getattr(module, "__file__", None), *(getattr(module, "__path__", None) or [])]
    return any(isinstance(path, str) and os.path.realpath(path).startswith(root + os.sep) for path in paths)


def _run_forked(args: list[str]) -> tuple[int, list[str]]:
    """Run pytest in a fork without the modules loaded from the project; the exit code and the failed tests."""
    # The virtual environment inside the copy is a link, so its real path lies outside the copy.
    root = os.path.realpath(os.curdir)
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        # Exit code 4 counts as an error, not as a caught mutant.
        code = 4
        try:
            for name, module in list(sys.modules.items()):
                if _loaded_from(module, root):
                    del sys.modules[name]
            recorder = _FailureRecorder()
            code = int(pytest.main(args, plugins=[recorder]))
            with os.fdopen(write_end, "w") as failed:
                json.dump(sorted(recorder.failed), failed)
        finally:
            os._exit(code)
    os.close(write_end)
    # Read before waiting, so that a long list cannot block the fork on a full pipe.
    with os.fdopen(read_end) as failed:
        sent = failed.read()
    code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    # A negative code means the fork was killed by a signal.
    return (code, json.loads(sent) if sent else []) if code >= 0 else (4, [])


def main() -> None:
    responses = os.fdopen(os.dup(1), "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    real_open = io.open
    active: list[_ReadRecorder] = []

    def open_and_record(file: Any, *args: Any, **kwargs: Any) -> Any:
        if active:
            active[0].saw(file)
        return real_open(file, *args, **kwargs)

    # pathlib.Path.read_text and builtin open both go through io.open.
    builtins.open = open_and_record  # ty: ignore[invalid-assignment]
    io.open = open_and_record  # ty: ignore[invalid-assignment]

    for line in sys.stdin:
        request = json.loads(line)
        if request.get("record"):
            recorder = _ReadRecorder({os.path.realpath(path) for path in request["watch"]})
            active.append(recorder)
            try:
                code = pytest.main(request["args"], plugins=[recorder])
            finally:
                active.clear()
            reads = {test: sorted(paths) for test, paths in recorder.reads.items() if paths}
            response: dict[str, Any] = {"exit": int(code), "reads": reads}
        else:
            code, failed = _run_forked(request["args"])
            response = {"exit": code, "failed": failed}
        responses.write(json.dumps(response) + "\n")


if __name__ == "__main__":
    main()
