"""Mutation operators.

Each operator looks at one parsed node and yields candidates: a text patch on the file and the same
change applied to the parse tree. The generator keeps a candidate only when the patched text parses
to exactly that changed tree, so an operator may propose a patch it is unsure about.
"""

import difflib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from pysqlmut.source import Span, SqlSource


@dataclass(frozen=True)
class Patch:
    """Replace the text from start to end (exclusive) with replacement."""

    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class Candidate:
    operator: str
    description: str
    patch: Patch
    # Applied to a copy of the node; returns the node that takes its place.
    mutate: Callable[[exp.Expr], exp.Expr]
    # Why the change cannot alter any result, when the operator can prove it; such a candidate is dropped.
    equivalent: str | None = None


Operator = Callable[[SqlSource, exp.Expr, int], Iterator[Candidate]]


def _set(**values: object) -> Callable[[exp.Expr], exp.Expr]:
    """A tree change that sets arguments of the node; expressions are copied, so every tree gets its own."""

    def mutate(node: exp.Expr) -> exp.Expr:
        for key, value in values.items():
            node.set(key, value.copy() if isinstance(value, exp.Expr) else value)
        return node

    return mutate


def _token_between(
    source: SqlSource, left: exp.Expr, right: exp.Expr, offset: int, types: set[TokenType]
) -> int | None:
    """The single token of the given types between two sibling nodes."""
    left_tokens, right_tokens = source.leaf_tokens(left, offset), source.leaf_tokens(right, offset)
    if left_tokens is None or right_tokens is None:
        return None
    matches = [i for i in range(left_tokens[1] + 1, right_tokens[0]) if source.tokens[i].token_type in types]
    return matches[0] if len(matches) == 1 else None


def _replace_token(source: SqlSource, index: int, replacement: str) -> Patch:
    token = source.tokens[index]
    return Patch(token.start, token.end + 1, replacement)


_COMPARISONS: dict[type[exp.Expr], tuple[type[exp.Binary], str, TokenType]] = {
    exp.EQ: (exp.NEQ, "<>", TokenType.EQ),
    exp.NEQ: (exp.EQ, "=", TokenType.NEQ),
    exp.LT: (exp.LTE, "<=", TokenType.LT),
    exp.LTE: (exp.LT, "<", TokenType.LTE),
    exp.GT: (exp.GTE, ">=", TokenType.GT),
    exp.GTE: (exp.GT, ">", TokenType.GTE),
}


_LOGICAL: dict[type[exp.Expr], tuple[type[exp.Binary], str, TokenType]] = {
    exp.And: (exp.Or, "OR", TokenType.AND),
    exp.Or: (exp.And, "AND", TokenType.OR),
}


def _swap_binary(
    name: str,
    swaps: dict[type[exp.Expr], tuple[type[exp.Binary], str, TokenType]],
    source: SqlSource,
    node: exp.Expr,
    offset: int,
) -> Iterator[Candidate]:
    """Replace the operator of a binary expression by the one the table names for its type."""
    swap = swaps.get(type(node))
    if swap is None:
        return
    target, text, token_type = swap
    index = _token_between(source, node.this, node.expression, offset, {token_type})
    if index is None:
        return
    yield Candidate(
        name,
        f"{source.tokens[index].text} -> {text}",
        _replace_token(source, index, text),
        lambda n: target(this=n.this, expression=n.expression),
    )


def comparison(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap = and <>, and move the boundary of <, <=, > and >=."""
    return _swap_binary("comparison", _COMPARISONS, source, node, offset)


def logical(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap AND and OR."""
    return _swap_binary("logical", _LOGICAL, source, node, offset)


def drop_condition(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Drop one side of an AND."""
    if not isinstance(node, exp.And):
        return
    left, right = source.node_span(node.this, offset), source.node_span(node.expression, offset)
    if left is None or right is None:
        return
    yield Candidate("drop-condition", "drop the right condition", Patch(left.end, right.end, ""), lambda n: n.this)
    yield Candidate(
        "drop-condition", "drop the left condition", Patch(left.start, right.start, ""), lambda n: n.expression
    )


def is_null(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap IS NULL and IS NOT NULL."""
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and not isinstance(node.parent, exp.Not):
        tokens = source.leaf_tokens(node.this, offset)
        after = tokens[1] + 1 if tokens else None
        if after is not None and after < len(source.tokens) and source.tokens[after].token_type == TokenType.IS:
            position = source.tokens[after].end + 1
            yield Candidate(
                "is-null", "IS NULL -> IS NOT NULL", Patch(position, position, " NOT"), lambda n: exp.Not(this=n.copy())
            )
    elif isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
        tokens = source.leaf_tokens(node.this.this, offset)
        if tokens is None or tokens[1] + 2 >= len(source.tokens):
            return
        is_token, not_token = source.tokens[tokens[1] + 1], source.tokens[tokens[1] + 2]
        if is_token.token_type == TokenType.IS and not_token.token_type == TokenType.NOT:
            yield Candidate(
                "is-null", "IS NOT NULL -> IS NULL", Patch(is_token.end + 1, not_token.end + 1, ""), lambda n: n.this
            )


_AGGREGATES: dict[type[exp.Expr], tuple[type[exp.Expr], str]] = {
    exp.Sum: (exp.Max, "MAX"),
    exp.Max: (exp.Min, "MIN"),
    exp.Min: (exp.Max, "MAX"),
    exp.Avg: (exp.Sum, "SUM"),
}


def aggregate(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap SUM, MAX, MIN and AVG."""
    swap = _AGGREGATES.get(type(node))
    if swap is None or node.args.get("expressions") or "start" not in node.meta:
        return
    target, name = swap
    index = source.token_at(offset + node.meta["start"])
    if index is None:
        return
    yield Candidate(
        "aggregate",
        f"{source.tokens[index].text} -> {name}",
        _replace_token(source, index, name),
        lambda n: target(this=n.this),
    )


def _call_arguments(source: SqlSource, name_index: int) -> tuple[list[Span], int] | None:
    """The text of each argument of a call and the index of its closing parenthesis."""
    open_index = name_index + 1
    if open_index >= len(source.tokens) or source.tokens[open_index].token_type != TokenType.L_PAREN:
        return None
    arguments: list[Span] = []
    depth, first = 0, open_index + 1
    for index in range(open_index, len(source.tokens)):
        token_type = source.tokens[index].token_type
        if token_type == TokenType.L_PAREN:
            depth += 1
        elif token_type == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                arguments.append(Span(source.tokens[first].start, source.tokens[index - 1].end + 1))
                return arguments, index
        elif token_type == TokenType.COMMA and depth == 1:
            arguments.append(Span(source.tokens[first].start, source.tokens[index - 1].end + 1))
            first = index + 1
    return None


def coalesce(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Drop the fallbacks of COALESCE, or swap its first two arguments."""
    if not isinstance(node, exp.Coalesce) or "start" not in node.meta:
        return
    name_index = source.token_at(offset + node.meta["start"])
    if name_index is None:
        return
    call = _call_arguments(source, name_index)
    if call is None:
        return
    arguments, close_index = call
    if len(arguments) != len(node.expressions) + 1 or len(arguments) < 2:
        return
    first, second = arguments[0], arguments[1]
    whole = Patch(
        source.tokens[name_index].start, source.tokens[close_index].end + 1, source.text[first.start : first.end]
    )
    yield Candidate("coalesce", "keep only the first argument", whole, lambda n: n.this.copy())
    swapped = (
        source.text[second.start : second.end]
        + source.text[first.end : second.start]
        + source.text[first.start : first.end]
    )

    def swap(n: exp.Expr) -> exp.Expr:
        rest = [e.copy() for e in n.expressions[1:]]
        return exp.Coalesce(this=n.expressions[0].copy(), expressions=[n.this.copy(), *rest])

    yield Candidate("coalesce", "swap the first two arguments", Patch(first.start, second.end, swapped), swap)


def _default_nulls_first(dialect: str, desc: bool) -> bool:
    """Where the dialect puts NULLs for a direction when the query does not say."""
    direction = "DESC" if desc else "ASC"
    # A fixed query, parsed only to learn the dialect's default; it never runs.
    query = f"SELECT a FROM t ORDER BY a {direction}"  # noqa: S608
    ordered = sqlglot.parse_one(query, read=dialect).find(exp.Ordered)
    return bool(ordered and ordered.args.get("nulls_first"))


def order(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Reverse an ORDER BY direction, in queries and window functions."""
    if not isinstance(node, exp.Ordered):
        return
    span = source.node_span(node.this, offset)
    after = source.first_token_from(span.end) if span else None
    if span is None:
        return
    next_type = source.tokens[after].token_type if after is not None else None
    # NULLS FIRST/LAST may follow the direction or the expression itself; sqlglot tokenizes NULLS as a word.
    nulls_index = after + 1 if after is not None and next_type in {TokenType.ASC, TokenType.DESC} else after
    explicit_nulls = (
        nulls_index is not None
        and nulls_index < len(source.tokens)
        and source.tokens[nulls_index].text.upper() == "NULLS"
    )

    def reverse(desc: bool) -> Callable[[exp.Expr], exp.Expr]:
        def mutate(n: exp.Expr) -> exp.Expr:
            n.set("desc", desc)
            if not explicit_nulls:
                # Without NULLS FIRST/LAST in the text, NULLs follow the dialect's default for the new direction.
                n.set("nulls_first", _default_nulls_first(source.dialect, desc))
            return n

        return mutate

    if next_type == TokenType.DESC and after is not None:
        yield Candidate("order", "DESC -> ASC", _replace_token(source, after, "ASC"), reverse(False))
    elif next_type == TokenType.ASC and after is not None:
        yield Candidate("order", "ASC -> DESC", _replace_token(source, after, "DESC"), reverse(True))
    else:
        yield Candidate("order", "ascending -> DESC", Patch(span.end, span.end, " DESC"), reverse(True))


def _union_branches(node: exp.Expr) -> list[exp.Expr]:
    """The queries a chain of UNIONs combines, looking through parentheses and nested UNIONs."""
    while isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Union):
        return [*_union_branches(node.this), *_union_branches(node.expression)]
    return [node]


def _returns_unique_rows(select: exp.Select) -> bool:
    """Whether a query cannot return the same row twice: SELECT DISTINCT, or a GROUP BY on columns it returns."""
    distinct = select.args.get("distinct")
    if isinstance(distinct, exp.Distinct):
        return not distinct.args.get("on")
    group = select.args.get("group")
    if group is None or any(group.args.get(key) for key in ("rollup", "cube", "grouping_sets")):
        return False
    if group.args.get("all"):
        return True
    # Names are not matched against aliases: GROUP BY f may group by a source column f that the query changes.
    returned = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in select.expressions}
    for key in group.expressions:
        if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit():
            if not 1 <= int(key.this) <= len(select.expressions):
                return False
        elif key.sql() not in returned:
            return False
    return True


def _literal_columns(select: exp.Select) -> dict[int, tuple[str, object]]:
    """The value of each output column that is a string or number literal, by position."""
    values: dict[int, tuple[str, object]] = {}
    for position, projection in enumerate(select.expressions):
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(value, exp.Literal):
            continue
        if value.is_string:
            # A collation may compare strings without case or trailing spaces.
            values[position] = ("string", value.this.casefold().rstrip())
        else:
            try:
                values[position] = ("number", Decimal(value.this))
            except InvalidOperation:
                continue
    return values


def _rows_already_unique(node: exp.Union) -> bool:
    """Whether UNION and UNION ALL return the same rows here.

    They do when every branch returns unique rows and no two branches can return the same row, because a
    column holds a different literal in each. Columns are matched by position, so a branch with * cannot be judged.
    """
    branches = _union_branches(node)
    selects = [b for b in branches if isinstance(b, exp.Select) and not b.is_star and _returns_unique_rows(b)]
    if len(selects) != len(branches) or len({len(s.expressions) for s in selects}) != 1:
        return False
    literals = [_literal_columns(s) for s in selects]
    return all(
        any(
            # A string and a number may compare equal after conversion, so only literals of one kind count.
            position in second and first[position][0] == second[position][0] and first[position] != second[position]
            for position in first
        )
        for first, second in combinations(literals, 2)
    )


def union(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap UNION and UNION ALL, unless the rows are already unique so both return the same."""
    if not isinstance(node, exp.Union):
        return
    index = _token_between(source, node.this, node.expression, offset, {TokenType.UNION})
    if index is None or index + 1 >= len(source.tokens):
        return
    equivalent = "rows are already unique" if _rows_already_unique(node) else None
    union_token, following = source.tokens[index], source.tokens[index + 1]
    if following.token_type == TokenType.ALL:
        patch = Patch(union_token.end + 1, following.end + 1, "")
        yield Candidate("union", "UNION ALL -> UNION", patch, _set(distinct=True), equivalent)
    elif following.token_type == TokenType.DISTINCT:
        patch = _replace_token(source, index + 1, "ALL")
        yield Candidate("union", "UNION DISTINCT -> UNION ALL", patch, _set(distinct=False), equivalent)
    else:
        patch = Patch(union_token.end + 1, union_token.end + 1, " ALL")
        yield Candidate("union", "UNION -> UNION ALL", patch, _set(distinct=False), equivalent)


_JOIN_MODIFIERS = {TokenType.LEFT, TokenType.INNER, TokenType.OUTER}


def join_type(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap LEFT JOIN and INNER JOIN."""
    if not isinstance(node, exp.Join) or not isinstance(node.this, exp.Table | exp.Subquery):
        return
    tokens = source.leaf_tokens(node.this, offset)
    if tokens is None:
        return
    join_index = next((i for i in range(tokens[0] - 1, -1, -1) if source.tokens[i].token_type == TokenType.JOIN), None)
    if join_index is None:
        return
    start = join_index
    while start > 0 and source.tokens[start - 1].token_type in _JOIN_MODIFIERS:
        start -= 1
    side, kind = (node.side or "").upper(), (node.kind or "").upper()
    patch_range = (source.tokens[start].start, source.tokens[join_index].end + 1)

    if side == "LEFT":
        to_inner = _set(side=None, kind="INNER")
        yield Candidate("join-type", "LEFT JOIN -> INNER JOIN", Patch(*patch_range, "INNER JOIN"), to_inner)
    elif not side and kind in {"", "INNER"}:
        to_left = _set(side="LEFT", kind=None)
        yield Candidate("join-type", "INNER JOIN -> LEFT JOIN", Patch(*patch_range, "LEFT JOIN"), to_left)


_ARITHMETIC: dict[type[exp.Expr], tuple[type[exp.Binary], str, TokenType]] = {
    exp.Add: (exp.Sub, "-", TokenType.PLUS),
    exp.Sub: (exp.Add, "+", TokenType.DASH),
    exp.Mul: (exp.Div, "/", TokenType.STAR),
    exp.Div: (exp.Mul, "*", TokenType.SLASH),
}


def arithmetic(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Swap + and -, * and /, and drop a unary minus."""
    yield from _swap_binary("arithmetic", _ARITHMETIC, source, node, offset)
    if isinstance(node, exp.Neg):
        tokens = source.leaf_tokens(node.this, offset)
        if tokens and tokens[0] > 0 and source.tokens[tokens[0] - 1].token_type == TokenType.DASH:
            dash = source.tokens[tokens[0] - 1]
            patch = Patch(dash.start, source.tokens[tokens[0]].start, "")
            yield Candidate("arithmetic", "drop unary minus", patch, lambda n: n.this.copy())


def literal(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Add one to an integer literal."""
    if not isinstance(node, exp.Literal) or node.is_string or not node.this.isdigit() or "start" not in node.meta:
        return
    value = int(node.this) + 1
    patch = Patch(offset + node.meta["start"], offset + node.meta["end"] + 1, str(value))
    yield Candidate("literal", f"{node.this} -> {value}", patch, lambda _: exp.Literal.number(value))


def distinct(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Drop DISTINCT from SELECT DISTINCT and COUNT(DISTINCT ...)."""
    if isinstance(node, exp.Select) and isinstance(node.args.get("distinct"), exp.Distinct) and node.expressions:
        if node.args["distinct"].args.get("on"):
            return
        tokens = source.leaf_tokens(node.expressions[0], offset)
        if tokens and tokens[0] > 0:
            index = next(
                (i for i in range(tokens[0] - 1, -1, -1) if source.tokens[i].token_type == TokenType.DISTINCT), None
            )
            if index is not None:
                patch = Patch(source.tokens[index].start, source.tokens[index + 1].start, "")
                yield Candidate("distinct", "SELECT DISTINCT -> SELECT", patch, _set(distinct=None))
    elif isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct) and len(node.this.expressions) == 1:
        tokens = source.leaf_tokens(node.this.expressions[0], offset)
        if tokens and tokens[0] > 0 and source.tokens[tokens[0] - 1].token_type == TokenType.DISTINCT:
            token = source.tokens[tokens[0] - 1]
            patch = Patch(token.start, source.tokens[tokens[0]].start, "")
            yield Candidate(
                "distinct",
                "COUNT(DISTINCT x) -> COUNT(x)",
                patch,
                lambda n: exp.Count(this=n.this.expressions[0].copy()),
            )


def case(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Replace the ELSE value of a CASE with NULL, and drop one WHEN branch of a CASE that has several."""
    if not isinstance(node, exp.Case):
        return
    default = node.args.get("default")
    default_span = source.node_span(default, offset) if default is not None else None
    if default_span is not None and not isinstance(default, exp.Null):
        patch = Patch(default_span.start, default_span.end, "NULL")
        yield Candidate("case", "ELSE value -> NULL", patch, _set(default=exp.Null()))

    branches = node.args.get("ifs") or []
    if len(branches) < 2:
        return
    for position, branch in enumerate(branches):
        condition = source.node_span(branch.this, offset)
        value = source.node_span(branch.args["true"], offset)
        first = source.token_at(condition.start) if condition else None
        following = source.first_token_from(value.end) if value else None
        if first is None or following is None or first == 0 or source.tokens[first - 1].token_type != TokenType.WHEN:
            continue

        def drop(n: exp.Expr, position: int = position) -> exp.Expr:
            n.set("ifs", [b.copy() for i, b in enumerate(n.args["ifs"]) if i != position])
            return n

        patch = Patch(source.tokens[first - 1].start, source.tokens[following].start, "")
        yield Candidate("case", f"drop WHEN branch {position + 1}", patch, drop)


def string_literal(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Change a string literal, so that a test has to depend on its exact value."""
    if not isinstance(node, exp.Literal) or not node.is_string or "start" not in node.meta:
        return
    index = source.token_at(offset + node.meta["start"])
    if index is None:
        return
    value = f"{node.this}_mutated"
    replacement = "'" + value.replace("'", "''") + "'"
    yield Candidate(
        "string-literal",
        f"'{node.this}' -> {replacement}",
        _replace_token(source, index, replacement),
        lambda _: exp.Literal.string(value),
    )


def _source_outputs(select: exp.Select, qualifier: str) -> list[exp.Identifier]:
    """The output columns of the CTE or subquery a qualifier names, when they are known without a schema."""
    for source in [select.args.get("from_"), *select.args.get("joins", [])]:
        relation = source.this if isinstance(source, exp.From | exp.Join) else None
        if relation is None or relation.alias_or_name != qualifier:
            continue
        query = relation.this if isinstance(relation, exp.Subquery) else None
        if isinstance(relation, exp.Table) and not relation.db:
            ancestor: exp.Expr | None = select
            while ancestor is not None and query is None:
                with_ = ancestor.args.get("with_")
                ctes = with_.expressions if isinstance(with_, exp.With) else []
                query = next((cte.this for cte in ctes if cte.alias == relation.name), None)
                ancestor = ancestor.parent
        if not isinstance(query, exp.Query) or query.is_star:
            return []
        outputs = [e.args.get("alias") if isinstance(e, exp.Alias) else e.this for e in query.selects]
        return [o for o in outputs if isinstance(o, exp.Identifier)]
    return []


def _likeliest_mix_up(name: str, others: dict[str, exp.Identifier]) -> exp.Identifier | None:
    """The other column most easily confused with a name: most shared words, then most similar spelling."""
    words = set(name.lower().split("_"))

    def closeness(other: str) -> tuple[int, float]:
        shared = len(words & set(other.lower().split("_")))
        return shared, difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio()

    best = max(sorted(others), key=closeness, default=None)
    return others[best] if best is not None else None


def column(source: SqlSource, node: exp.Expr, offset: int) -> Iterator[Candidate]:
    """Replace a qualified column with the likeliest mix-up among the other columns of the same table.

    Those are the columns the same SELECT reads from that table, and the output columns of a CTE or subquery.
    Columns in GROUP BY and in EXCLUDE lists are left alone, since changing only them makes the query fail.
    """
    if not isinstance(node, exp.Column) or not isinstance(node.this, exp.Identifier) or not node.table:
        return
    select = node.parent_select
    if select is None or isinstance(node.find_ancestor(exp.Group, exp.Star, exp.Select), exp.Group | exp.Star):
        return
    identifier = node.this
    if "start" not in identifier.meta:
        return
    candidates = {
        other.this.name: other.this
        for other in select.find_all(exp.Column)
        if other.table == node.table and isinstance(other.this, exp.Identifier) and other.parent_select is select
    }
    candidates.update({output.name: output for output in _source_outputs(select, node.table)})
    others = {name: ident for name, ident in candidates.items() if name.lower() != identifier.name.lower()}
    replacement = _likeliest_mix_up(identifier.name, others)
    index = source.token_at(offset + identifier.meta["start"])
    if replacement is None or index is None:
        return
    text = replacement.sql(dialect=source.dialect)
    yield Candidate(
        "column",
        f"{node.table}.{identifier.name} -> {node.table}.{replacement.name}",
        _replace_token(source, index, text),
        _set(this=replacement),
    )


ALL_OPERATORS: dict[str, Operator] = {
    "comparison": comparison,
    "logical": logical,
    "drop-condition": drop_condition,
    "is-null": is_null,
    "aggregate": aggregate,
    "coalesce": coalesce,
    "order": order,
    "union": union,
    "join-type": join_type,
    "arithmetic": arithmetic,
    "literal": literal,
    "distinct": distinct,
    "case": case,
    "string-literal": string_literal,
    "column": column,
}
