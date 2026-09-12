"""Run results as a JSON file, and survivors grouped by the shape of the code they changed.

The same rule often repeats across a file: the same filter on many tables, the same fallback on many columns.
Grouping mutants whose changed lines only differ in names and literals shows each repeated gap once, with a
count, instead of once per place.
"""

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from pysqlmut.runner import Result

_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_VERSION = 1


def shape(line: str) -> str:
    """The line with names, strings and numbers masked; upper-case SQL keywords stay readable."""
    masked = _STRING.sub("'S'", line)
    masked = _NUMBER.sub("N", masked)
    # Upper-case words are keywords, and S and N are the string and number placeholders set above.
    masked = _WORD.sub(lambda m: m.group(0) if m.group(0).isupper() else "x", masked)
    return " ".join(masked.split())


@dataclass(frozen=True)
class Group:
    operator: str
    description: str
    shape: str
    results: tuple[Result, ...]


def group(results: Iterable[Result]) -> list[Group]:
    """Group results by operator, masked description and the masked line before and after, largest group first.

    The line after the change is part of the key: one line can hold two different changes with the same
    description, such as dropping the inner or the outer condition of a chained AND.
    """
    groups: dict[tuple[str, str, str, str], list[Result]] = {}
    for result in results:
        description = _NUMBER.sub("N", _STRING.sub("'S'", result.description))
        key = (result.operator, description, shape(result.before), shape(result.after))
        groups.setdefault(key, []).append(result)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return [Group(operator, description, line, tuple(members)) for (operator, description, line, _), members in ordered]


def save(results: Sequence[Result], path: Path) -> None:
    data = {"version": _VERSION, "results": [asdict(r) for r in results]}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> list[Result]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        raise ValueError(f"{path} is not a pysqlmut report of version {_VERSION}")
    names = {field.name for field in fields(Result)}
    return [Result(**{key: value for key, value in item.items() if key in names}) for item in data["results"]]


def render(groups: Sequence[Group], limit: int) -> str:
    lines = []
    for item in groups[:limit]:
        example = item.results[0]
        lines.append(f"{len(item.results):5}x {item.operator} ({item.description}): {item.shape}")
        lines.append(f"       e.g. {example.path}:{example.line}")
        lines.append(f"         - {example.before}")
        lines.append(f"         + {example.after}")
    hidden = len(groups) - len(groups[:limit])
    if hidden > 0:
        lines.append(f"  ... and {hidden} smaller groups")
    return "\n".join(lines)
