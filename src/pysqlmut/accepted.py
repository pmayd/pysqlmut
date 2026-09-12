"""Survivors a team has reviewed and accepted, so later runs neither rerun nor report them.

A survivor is identified by its file, operator, description and the exact line before and after
the change, not by its line number, so accepting it survives edits elsewhere in the file. Identical
lines in one file share an identity and are accepted together.
"""

import json
from collections.abc import Iterable
from pathlib import Path

from pysqlmut.runner import Result

Fingerprint = tuple[str, str, str, str, str]
_FIELDS = ("path", "operator", "description", "before", "after")


def fingerprint(path: str, operator: str, description: str, before: str, after: str) -> Fingerprint:
    return (path, operator, description, before.strip(), after.strip())


def of_result(result: Result) -> Fingerprint:
    return fingerprint(result.path, result.operator, result.description, result.before, result.after)


def load(path: Path) -> set[Fingerprint]:
    if not path.exists():
        return set()
    items = json.loads(path.read_text(encoding="utf-8"))
    return {fingerprint(*(item[field] for field in _FIELDS)) for item in items}


def save(path: Path, fingerprints: Iterable[Fingerprint]) -> None:
    items = [dict(zip(_FIELDS, item, strict=True)) for item in sorted(set(fingerprints))]
    path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
