"""A SQL file with its tokens and parse trees, and the text ranges of parsed nodes."""

import bisect
import logging
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

# Statements sqlglot cannot parse are kept as commands and skipped; its warnings about them are noise here.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# How many tokens a node's text may reach beyond its first and last positioned leaf (keywords such as
# IS NULL or the parentheses of a call carry no position of their own).
MAX_EXTENSION = 4


def read_text(path: Path) -> str:
    """A file's text with its line endings as they are, so that a patched file keeps them."""
    with path.open(encoding="utf-8", newline="") as file:
        return file.read()


@dataclass(frozen=True)
class Span:
    """A range of the source text; end is exclusive."""

    start: int
    end: int


@dataclass(frozen=True)
class Statement:
    """One parsed statement and the part of the file it was parsed from."""

    tree: exp.Expr
    span: Span


class SqlSource:
    """The text, tokens and statements of one SQL file."""

    def __init__(self, path: Path, text: str, dialect: str) -> None:
        self.path = path
        self.text = text
        self.dialect = dialect
        self.tokens: list[Token] = Dialect.get_or_raise(dialect).tokenize(text)
        self._starts = [token.start for token in self.tokens]
        self.statements = self._statements()
        self._spans: dict[int, Span | None] = {}

    @classmethod
    def read(cls, path: Path, dialect: str) -> "SqlSource":
        return cls(path, read_text(path), dialect)

    def _statements(self) -> list[Statement]:
        """Parse the file one statement at a time, keeping the text range of each."""
        chunks: list[list[Token]] = [[]]
        for token in self.tokens:
            if token.token_type == TokenType.SEMICOLON:
                chunks.append([])
            else:
                chunks[-1].append(token)
        statements = []
        for chunk in chunks:
            if not chunk:
                continue
            span = Span(chunk[0].start, chunk[-1].end + 1)
            try:
                trees = [tree for tree in sqlglot.parse(self.text[span.start : span.end], read=self.dialect) if tree]
            except SqlglotError:
                continue
            if len(trees) == 1 and not isinstance(trees[0], exp.Command):
                statements.append(Statement(trees[0], span))
        return statements

    def line_of(self, position: int) -> int:
        return self.text.count("\n", 0, position) + 1

    def token_at(self, position: int) -> int | None:
        """Index of the token covering a character position."""
        index = bisect.bisect_right(self._starts, position) - 1
        if index >= 0 and self.tokens[index].start <= position <= self.tokens[index].end:
            return index
        return None

    def first_token_from(self, position: int) -> int | None:
        """Index of the first token starting at or after a character position."""
        index = bisect.bisect_left(self._starts, position)
        return index if index < len(self.tokens) else None

    def leaf_tokens(self, node: exp.Expr, offset: int) -> tuple[int, int] | None:
        """First and last token covered by the node's positioned leaves.

        Positions in a node's meta are relative to the statement text, so offset is the statement start.
        """
        positions = [(n.meta["start"], n.meta["end"]) for n in node.walk() if "start" in n.meta and "end" in n.meta]
        if not positions:
            return None
        first = self.token_at(offset + min(start for start, _ in positions))
        last = self.token_at(offset + max(end for _, end in positions))
        if first is None or last is None:
            return None
        return first, last

    def node_span(self, node: exp.Expr, offset: int) -> Span | None:
        """The exact text of an expression, found by growing its leaf range until it parses back to the node."""
        key = id(node)
        if key not in self._spans:
            self._spans[key] = self._find_span(node, offset)
        return self._spans[key]

    def _find_span(self, node: exp.Expr, offset: int) -> Span | None:
        bounds = self.leaf_tokens(node, offset)
        if bounds is None:
            return None
        first, last = bounds
        target = node.sql(dialect=self.dialect)
        for total in range(2 * MAX_EXTENSION + 1):
            for back in range(min(total, MAX_EXTENSION) + 1):
                forward = total - back
                if forward > MAX_EXTENSION or first - back < 0 or last + forward >= len(self.tokens):
                    continue
                span = Span(self.tokens[first - back].start, self.tokens[last + forward].end + 1)
                if self._parses_to(self.text[span.start : span.end], target):
                    return span
        return None

    def _parses_to(self, snippet: str, target: str) -> bool:
        try:
            select = sqlglot.parse_one(f"SELECT {snippet}", read=self.dialect)
        except SqlglotError:
            return False
        projections = select.expressions if isinstance(select, exp.Select) else []
        return len(projections) == 1 and projections[0].sql(dialect=self.dialect) == target
