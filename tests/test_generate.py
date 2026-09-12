"""Each operator changes exactly one expression and leaves the rest of the file, comments included, untouched."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from sqlglot import exp

from pysqlmut.mutants import generate
from pysqlmut.operators import ALL_OPERATORS, Candidate, Patch
from pysqlmut.source import SqlSource

QUERY = """-- Totals per customer; this comment must survive every mutant.
SELECT
    a.x,
    SUM(b.y) AS total,
    COALESCE(a.p, b.q) AS c,
    COUNT(DISTINCT b.k) AS keys,
    -1 * a.n AS negated
FROM t AS a
LEFT JOIN u AS b ON a.id = b.id AND a.v = b.v
WHERE a.z >= 10 AND b.w IS NULL
ORDER BY total DESC;
"""

UNIONS = """SELECT DISTINCT a FROM t
UNION ALL
SELECT a + 1 FROM u WHERE b IS NOT NULL;
"""


def mutated(sql: str, operator: str) -> set[str]:
    generation = generate(SqlSource(Path("q.sql"), sql, "snowflake"), [operator])
    return {mutant.apply(sql) for mutant in generation.mutants}


def replaced(sql: str, old: str, new: str) -> str:
    assert sql.count(old) == 1, old
    return sql.replace(old, new)


@pytest.mark.parametrize(
    ("operator", "old", "new"),
    [
        ("comparison", "a.z >= 10", "a.z > 10"),
        ("comparison", "a.id = b.id", "a.id <> b.id"),
        ("logical", "a.z >= 10 AND b.w", "a.z >= 10 OR b.w"),
        ("drop-condition", "WHERE a.z >= 10 AND b.w IS NULL", "WHERE a.z >= 10"),
        ("drop-condition", "WHERE a.z >= 10 AND b.w IS NULL", "WHERE b.w IS NULL"),
        ("is-null", "b.w IS NULL", "b.w IS NOT NULL"),
        ("aggregate", "SUM(b.y)", "MAX(b.y)"),
        ("coalesce", "COALESCE(a.p, b.q) AS c", "a.p AS c"),
        ("coalesce", "COALESCE(a.p, b.q) AS c", "COALESCE(b.q, a.p) AS c"),
        ("order", "total DESC", "total ASC"),
        ("join-type", "LEFT JOIN u", "INNER JOIN u"),
        ("arithmetic", "-1 * a.n", "1 * a.n"),
        ("arithmetic", "-1 * a.n", "-1 / a.n"),
        ("literal", ">= 10", ">= 11"),
        ("distinct", "COUNT(DISTINCT b.k)", "COUNT(b.k)"),
    ],
)
def test_an_operator_changes_only_its_expression(operator, old, new):
    assert replaced(QUERY, old, new) in mutated(QUERY, operator)


@pytest.mark.parametrize(
    ("operator", "old", "new"),
    [
        ("union", "UNION ALL", "UNION"),
        ("distinct", "SELECT DISTINCT a", "SELECT a"),
        ("is-null", "b IS NOT NULL", "b IS NULL"),
        ("arithmetic", "a + 1", "a - 1"),
    ],
)
def test_operators_work_across_statements_and_set_operations(operator, old, new):
    assert replaced(UNIONS, old, new) in mutated(UNIONS, operator)


LABELS = """SELECT
    CASE WHEN a = 1 THEN 'one' WHEN a = 2 THEN 'two' ELSE 'many' END AS label
FROM t;
"""


@pytest.mark.parametrize(
    ("operator", "old", "new"),
    [
        ("case", "ELSE 'many'", "ELSE NULL"),
        ("case", "WHEN a = 1 THEN 'one' ", ""),
        ("case", "WHEN a = 2 THEN 'two' ", ""),
        ("string-literal", "'one'", "'one_mutated'"),
    ],
)
def test_case_branches_and_string_literals_change_on_their_own(operator, old, new):
    assert replaced(LABELS, old, new) in mutated(LABELS, operator)


def test_comments_exclude_a_line_or_a_block_from_mutation():
    sql = """SELECT a FROM t WHERE b = 1; -- pysqlmut: skip
SELECT a FROM t WHERE b = 2;
-- pysqlmut: off
SELECT a FROM t WHERE b = 3;
-- pysqlmut: on
SELECT a FROM t WHERE b = 4;
"""
    mutants = generate(SqlSource(Path("q.sql"), sql, "snowflake"), ["comparison"]).mutants
    assert sorted({m.line for m in mutants}) == [2, 6]


LABEL_BLOCKS = """SELECT 'product' AS kind, 'product_name' AS label, product AS code, COUNT(*) AS n FROM s GROUP BY ALL
UNION ALL
(SELECT 'product' AS kind, 'product_short_name' AS label, product AS code, COUNT(*) AS n FROM s GROUP BY ALL)
UNION ALL
SELECT DISTINCT 'store', 'store_name', store, 1 FROM s;
"""


def test_union_all_stays_where_every_row_is_already_unique():
    generation = generate(SqlSource(Path("q.sql"), LABEL_BLOCKS, "snowflake"), ["union"])
    assert generation.mutants == []
    assert generation.rejected["union", "equivalent: rows are already unique"] == 2


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # The first two branches can return the same row.
        ("'product_short_name' AS label", "'product_name' AS label"),
        # The last branch can return the same row twice.
        ("SELECT DISTINCT 'store'", "SELECT 'store'"),
        # A string and a number may compare equal after conversion.
        ("'product_short_name' AS label, product AS code, COUNT(*) AS n", "1 AS label, product AS code, 7 AS n"),
    ],
)
def test_union_all_changes_where_rows_may_repeat(old, new):
    assert mutated(replaced(LABEL_BLOCKS, old, new), "union")


COLUMNS = """WITH months AS (
    SELECT customer_id, order_month, next_month FROM calendar
)
SELECT
    o.customer_id,
    o.discount AS discount,
    o.discount_rate AS rate,
    o.shipping_status,
    m.next_month
FROM orders AS o
INNER JOIN months AS m ON o.customer_id = m.customer_id;
"""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # The columns share the word "discount"; shipping_status shares none.
        ("o.discount AS discount", "o.discount_rate AS discount"),
        # order_month is not read by this SELECT but is an output of the CTE m names.
        ("m.next_month\nFROM", "m.order_month\nFROM"),
    ],
)
def test_a_column_is_replaced_by_the_likeliest_mix_up_of_the_same_table(old, new):
    assert replaced(COLUMNS, old, new) in mutated(COLUMNS, "column")


def test_columns_in_group_by_and_exclude_lists_stay():
    sql = "SELECT sales.* EXCLUDE (a, b), sales.c, sales.d FROM sales GROUP BY sales.c, sales.d;\n"
    mutants = mutated(sql, "column")
    assert mutants
    assert all("EXCLUDE (a, b)" in mutant and "GROUP BY sales.c, sales.d;" in mutant for mutant in mutants)


def test_a_process_pool_generates_the_same_mutants_in_the_same_order():
    source = SqlSource(Path("q.sql"), QUERY + UNIONS + LABELS, "snowflake")
    serial = generate(source)
    with ProcessPoolExecutor(2) as executor:
        parallel = generate(source, executor=executor)
    assert len(serial.mutants) > 20
    assert parallel.mutants == serial.mutants
    assert parallel.rejected == serial.rejected


def test_an_empty_operator_list_runs_no_operator():
    assert generate(SqlSource(Path("q.sql"), QUERY, "snowflake"), []).mutants == []


def test_statements_sqlglot_cannot_parse_are_skipped():
    sql = """GRANT SELECT ON TABLE s.t TO ROLE reader;
EXECUTE IMMEDIATE $$
BEGIN
    UPDATE s.t SET a = 1 WHERE b = 2;
END;
$$;
SELECT a FROM s.t WHERE b = 2;
"""
    source = SqlSource(Path("q.sql"), sql, "snowflake")
    mutants = generate(source, ["comparison"]).mutants
    assert [m.line for m in mutants] == [7]


BRANCHES = """SELECT a FROM t JOIN u ON t.id = u.id ORDER BY a ASC, b;
SELECT a FROM t UNION SELECT a FROM u;
"""


@pytest.mark.parametrize(
    ("operator", "old", "new"),
    [
        ("join-type", "JOIN u", "LEFT JOIN u"),
        ("order", "a ASC", "a DESC"),
        ("order", ", b;", ", b DESC;"),
        ("union", "UNION SELECT", "UNION ALL SELECT"),
    ],
)
def test_join_order_and_union_change_in_the_other_direction_too(operator, old, new):
    assert replaced(BRANCHES, old, new) in mutated(BRANCHES, operator)


def test_a_candidate_is_kept_only_when_its_patch_makes_exactly_the_change_it_claims(monkeypatch):
    sql = "SELECT a FROM t WHERE b = 1;\n"
    equals = sql.index("=")

    def to_not_equal(node: exp.Expr) -> exp.Expr:
        return exp.NEQ(this=node.this.copy(), expression=node.expression.copy())

    def unchanged(node: exp.Expr) -> exp.Expr:
        return node

    candidates = [
        Candidate("claims", "honest", Patch(equals, equals + 1, "<>"), to_not_equal),
        Candidate("claims", "patch without the claimed tree change", Patch(equals, equals + 1, "<>"), unchanged),
        Candidate("claims", "patch past the statement", Patch(len(sql), len(sql), " "), to_not_equal),
        Candidate("claims", "patch that breaks the syntax", Patch(equals, equals + 1, "= ="), unchanged),
        Candidate("claims", "patch that changes nothing", Patch(equals, equals + 1, "="), unchanged),
    ]

    def claims(source, node, offset):
        if isinstance(node, exp.EQ):
            yield from candidates

    monkeypatch.setitem(ALL_OPERATORS, "claims", claims)
    generation = generate(SqlSource(Path("q.sql"), sql, "duckdb"), ["claims"])
    assert [m.description for m in generation.mutants] == ["honest"]
    assert dict(generation.rejected) == {
        ("claims", "differs from the intended change"): 1,
        ("claims", "outside statement"): 1,
        ("claims", "does not parse"): 1,
        ("claims", "changes nothing"): 1,
    }


def test_skip_comments_count_lines_the_way_line_numbers_do():
    sql = (
        "SELECT a FROM t WHERE b = 1; -- page\x0cbreak\n"
        "SELECT a FROM t WHERE b = 2; -- pysqlmut: skip\n"
        "SELECT a FROM t WHERE b = 3;\n"
    )
    mutants = generate(SqlSource(Path("q.sql"), sql, "snowflake"), ["comparison"]).mutants
    assert sorted({m.line for m in mutants}) == [1, 3]


SPANNING = """SELECT a FROM t
WHERE b = 1
{off}
-- nothing in here may change
{on}
AND c = 2;
"""


def test_a_change_across_an_excluded_block_is_skipped():
    def drop_conditions(off: str, on: str) -> list:
        sql = SPANNING.format(off=off, on=on)
        return generate(SqlSource(Path("q.sql"), sql, "snowflake"), ["drop-condition"]).mutants

    assert len(drop_conditions("--", "--")) == 2
    assert drop_conditions("-- pysqlmut: off", "-- pysqlmut: on") == []
