"""Each operator changes exactly one expression and leaves the rest of the file, comments included, untouched."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from pysqlmut.mutants import generate
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
