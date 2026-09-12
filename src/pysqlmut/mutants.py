"""Generate the mutants of a SQL file and keep only those that change exactly what they claim."""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot.errors import SqlglotError

from pysqlmut.operators import ALL_OPERATORS, Candidate, Operator, Patch
from pysqlmut.source import SqlSource, Statement


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
    for number, line in enumerate(text.splitlines(), 1):
        match = _DIRECTIVE.search(line)
        directive = match.group(1).lower() if match else None
        if directive == "off":
            off = True
        if off or directive == "skip":
            skipped.add(number)
        if directive == "on":
            off = False
    return skipped


def generate(source: SqlSource, operators: Iterable[str] | None = None) -> Generation:
    """Run the operators over every statement and verify each candidate by parsing the patched statement.

    operators=None runs every operator; an empty list runs none.
    """
    chosen: list[Operator] = [ALL_OPERATORS[name] for name in (ALL_OPERATORS if operators is None else operators)]
    generation = Generation(source)
    skipped = skipped_lines(source.text)
    for statement in source.statements:
        nodes = list(statement.tree.walk())
        for index, node in enumerate(nodes):
            for operator in chosen:
                for candidate in operator(source, node, statement.span.start):
                    patch = candidate.patch
                    if skipped and {source.line_of(patch.start), source.line_of(patch.end)} & skipped:
                        generation.rejected[candidate.operator, "skipped by comment"] += 1
                        continue
                    _verify(generation, statement, index, candidate)
    return generation


def _verify(generation: Generation, statement: Statement, index: int, candidate: Candidate) -> None:
    source = generation.source
    patch = candidate.patch
    if not statement.span.start <= patch.start <= patch.end <= statement.span.end:
        generation.rejected[candidate.operator, "outside statement"] += 1
        return
    expected_tree = statement.tree.copy()
    target = list(expected_tree.walk())[index]
    replacement = candidate.mutate(target)
    if replacement is not target:
        if target is expected_tree:
            expected_tree = replacement
        else:
            target.replace(replacement)
    expected = expected_tree.sql(dialect=source.dialect)

    relative_start, relative_end = patch.start - statement.span.start, patch.end - statement.span.start
    original = source.text[statement.span.start : statement.span.end]
    mutated = original[:relative_start] + patch.replacement + original[relative_end:]
    try:
        trees = [tree for tree in sqlglot.parse(mutated, read=source.dialect) if tree]
    except SqlglotError:
        generation.rejected[candidate.operator, "does not parse"] += 1
        return
    if len(trees) != 1 or trees[0].sql(dialect=source.dialect) != expected:
        generation.rejected[candidate.operator, "differs from the intended change"] += 1
        return
    if expected == statement.tree.sql(dialect=source.dialect):
        generation.rejected[candidate.operator, "changes nothing"] += 1
        return
    generation.mutants.append(
        Mutant(source.path, candidate.operator, candidate.description, source.line_of(patch.start), patch)
    )
