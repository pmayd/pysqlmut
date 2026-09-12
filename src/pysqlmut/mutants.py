"""Generate the mutants of a SQL file and keep only those that change exactly what they claim."""

import math
import re
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot.errors import SqlglotError

from pysqlmut.operators import ALL_OPERATORS, Candidate, Operator, Patch
from pysqlmut.source import SqlSource, Statement

# With an executor, a file is cut into about this many parts of similar cost.
PARTS_PER_FILE = 64


@dataclass(frozen=True)
class Mutant:
    path: Path
    operator: str
    description: str
    line: int
    patch: Patch

    def apply(self, text: str) -> str:
        return text[: self.patch.start] + self.patch.replacement + text[self.patch.end :]

    def diff(self, text: str) -> tuple[str, str]:
        """The original and the mutated line."""
        start = text.rfind("\n", 0, self.patch.start) + 1
        end = text.find("\n", self.patch.end)
        end = len(text) if end == -1 else end
        mutated_end = end + len(self.patch.replacement) - (self.patch.end - self.patch.start)
        return text[start:end], self.apply(text)[start:mutated_end]


@dataclass
class Generation:
    source: SqlSource
    mutants: list[Mutant] = field(default_factory=list)
    # (operator, reason) for candidates that were dropped
    rejected: Counter[tuple[str, str]] = field(default_factory=Counter)


_DIRECTIVE = re.compile(r"--\s*pysqlmut:\s*(skip|off|on)\b", re.IGNORECASE)


def skipped_lines(text: str) -> set[int]:
    """Lines excluded by comments: `-- pysqlmut: skip` on the line, or between `-- pysqlmut: off` and `on`."""
    skipped: set[int] = set()
    off = False
    # Split on newlines only, the way line numbers are counted; splitlines also splits on form feeds.
    for number, line in enumerate(text.split("\n"), 1):
        match = _DIRECTIVE.search(line)
        directive = match.group(1).lower() if match else None
        if directive == "off":
            off = True
        if off or directive == "skip":
            skipped.add(number)
        if directive == "on":
            off = False
    return skipped


@dataclass(frozen=True)
class _Part:
    """The nodes start to stop, in walk order, of one statement."""

    statement: int
    start: int
    stop: int


def generate(source: SqlSource, operators: Iterable[str] | None = None, executor: Executor | None = None) -> Generation:
    """Run the operators over every statement and verify each candidate by parsing the patched statement.

    operators=None runs every operator; an empty list runs none. With a process pool as executor, parts of
    the file are verified in parallel; the result is the same as without.
    """
    names = tuple(ALL_OPERATORS if operators is None else operators)
    parts = _parts(source, 1 if executor is None else PARTS_PER_FILE)
    if executor is None:
        results = [_generate_part(source, names, part) for part in parts]
    else:
        count = len(parts)
        results = executor.map(
            _generate_part_in_worker,
            [source.path] * count,
            [source.text] * count,
            [source.dialect] * count,
            [names] * count,
            parts,
        )
    generation = Generation(source)
    for mutants, rejected in results:
        generation.mutants.extend(mutants)
        generation.rejected.update(rejected)
    return generation


def _parts(source: SqlSource, target: int) -> list[_Part]:
    """Cut the statements into about target parts; a candidate costs about as much as its statement is large."""
    sizes = [sum(1 for _ in statement.tree.walk()) for statement in source.statements]
    total = sum(size * size for size in sizes) or 1
    parts = []
    for index, size in enumerate(sizes):
        pieces = min(size, math.ceil(target * size * size / total))
        step = math.ceil(size / pieces)
        parts.extend(_Part(index, start, min(start + step, size)) for start in range(0, size, step))
    return parts


# Each worker process parses a file once and keeps it for the following parts.
_worker_sources: dict[tuple[Path, str], SqlSource] = {}


def _generate_part_in_worker(
    path: Path, text: str, dialect: str, names: tuple[str, ...], part: _Part
) -> tuple[list[Mutant], Counter[tuple[str, str]]]:
    source = _worker_sources.get((path, dialect))
    if source is None or source.text != text:
        # One file at a time: the parts of a file arrive together, so earlier files are not needed again.
        _worker_sources.clear()
        source = _worker_sources[path, dialect] = SqlSource(path, text, dialect)
    return _generate_part(source, names, part)


def _generate_part(
    source: SqlSource, names: tuple[str, ...], part: _Part
) -> tuple[list[Mutant], Counter[tuple[str, str]]]:
    chosen: list[Operator] = [ALL_OPERATORS[name] for name in names]
    skipped = skipped_lines(source.text)
    statement = source.statements[part.statement]
    original = statement.tree.sql(dialect=source.dialect)
    nodes = list(statement.tree.walk())
    mutants: list[Mutant] = []
    rejected: Counter[tuple[str, str]] = Counter()
    for index in range(part.start, part.stop):
        for operator in chosen:
            for candidate in operator(source, nodes[index], statement.span.start):
                outcome = _check(source, statement, original, index=index, candidate=candidate, skipped=skipped)
                if isinstance(outcome, Mutant):
                    mutants.append(outcome)
                else:
                    rejected[candidate.operator, outcome] += 1
    return mutants, rejected


def _check(
    source: SqlSource, statement: Statement, original: str, *, index: int, candidate: Candidate, skipped: set[int]
) -> Mutant | str:
    """The mutant a candidate makes, or the reason it is rejected."""
    patch = candidate.patch
    if not skipped.isdisjoint(range(source.line_of(patch.start), source.line_of(patch.end) + 1)):
        return "skipped by comment"
    if candidate.equivalent:
        return f"equivalent: {candidate.equivalent}"
    if not statement.span.start <= patch.start <= patch.end <= statement.span.end:
        return "outside statement"
    expected_tree = statement.tree.copy()
    target = list(expected_tree.walk())[index]
    replacement = candidate.mutate(target)
    if replacement is not target:
        if target is expected_tree:
            expected_tree = replacement
        else:
            target.replace(replacement)
    # Both trees belong to this check alone, so the generator may change them instead of copying them first.
    expected = expected_tree.sql(dialect=source.dialect, copy=False)

    relative_start, relative_end = patch.start - statement.span.start, patch.end - statement.span.start
    text = source.text[statement.span.start : statement.span.end]
    mutated = text[:relative_start] + patch.replacement + text[relative_end:]
    try:
        trees = [tree for tree in sqlglot.parse(mutated, read=source.dialect) if tree]
    except SqlglotError:
        return "does not parse"
    if len(trees) != 1 or trees[0].sql(dialect=source.dialect, copy=False) != expected:
        return "differs from the intended change"
    if expected == original:
        return "changes nothing"
    return Mutant(source.path, candidate.operator, candidate.description, source.line_of(patch.start), patch)
