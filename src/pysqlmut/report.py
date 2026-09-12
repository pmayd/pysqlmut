"""Summaries of run results: survivors grouped by the shape of the code they changed.

The same rule often repeats across many columns (a lookup join per text column, a fallback per
label). Grouping mutants whose changed lines only differ in names and literals shows each repeated
gap once, with a count, instead of once per column.
"""

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pysqlmut.runner import Result

_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


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
    """Group results by operator, masked description and masked original line, largest group first."""
    groups: dict[tuple[str, str, str], list[Result]] = {}
    for result in results:
        key = (result.operator, _NUMBER.sub("N", _STRING.sub("'S'", result.description)), shape(result.before))
        groups.setdefault(key, []).append(result)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return [Group(operator, description, line, tuple(members)) for (operator, description, line), members in ordered]


def load(path: Path) -> list[Result]:
    return [Result(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def render(groups: Sequence[Group], limit: int | None = None) -> str:
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
