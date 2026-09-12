"""Run pytest sessions on request inside one long-lived process.

pysqlmut starts this file with the tested project's own Python, so it may import only the standard
library and pytest. Each request is one JSON line on stdin and gets one JSON line back on the
original stdout; pytest's own output goes to /dev/null.

Requests:
- {"args": [...]} runs pytest with these arguments and answers {"exit": code}.
- {"args": [...], "record": true, "watch": [paths]} does the same and also answers which watched
  files each test read: {"exit": code, "reads": {nodeid: [paths]}}. Reads outside any test, for
  example while test modules are imported, are listed under the empty node id.
"""

import builtins
import io
import json
import os
import sys
from collections.abc import Generator
from typing import Any

import pytest


class _ReadRecorder:
    """Pytest plugin that notes which watched files each test opens."""

    def __init__(self, watched: set[str]) -> None:
        self.watched = watched
        self.current = ""
        self.reads: dict[str, set[str]] = {}

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, Any, Any]:
        self.current = item.nodeid
        try:
            return (yield)
        finally:
            self.current = ""

    def saw(self, file: Any) -> None:
        try:
            path = os.path.realpath(os.fspath(file))
        except TypeError:
            return
        if path in self.watched:
            self.reads.setdefault(self.current, set()).add(path)


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
            reads = {nodeid: sorted(paths) for nodeid, paths in recorder.reads.items()}
            response: dict[str, Any] = {"exit": int(code), "reads": reads}
        else:
            response = {"exit": int(pytest.main(request["args"]))}
        responses.write(json.dumps(response) + "\n")


if __name__ == "__main__":
    main()
