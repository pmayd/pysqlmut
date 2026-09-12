"""The runner classifies mutants by running a real test suite, and never changes the project."""

import sys
import textwrap
from pathlib import Path

import pytest

from pysqlmut.mutants import generate
from pysqlmut.runner import CAUGHT, SURVIVED, BaselineFailedError, run
from pysqlmut.source import SqlSource

QUERY = """-- Paid orders per customer.
SELECT customer, SUM(amount) AS total
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY customer;
"""

# The test checks the total of paid orders, but has no order with amount 0, so it cannot tell
# amount > 0 from amount >= 0.
TEST = """
from pathlib import Path

import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('a', 7, 'open'), ('b', 2, 'paid')")
    sql = (Path(__file__).parent / "query.sql").read_text()
    assert con.execute(sql).fetchall() == [("a", 8), ("b", 2)]
"""

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "query.sql").write_text(QUERY)
    (tmp_path / "test_query.py").write_text(textwrap.dedent(TEST))
    return tmp_path


def test_a_mutant_that_changes_the_result_is_caught_and_an_invisible_one_survives(project):
    # Mutants carry the absolute path they were read from; the runner must still only change its copies.
    mutants = generate(SqlSource.read(project / "query.sql", "duckdb"), ["comparison", "aggregate"]).mutants
    results = run(mutants, project, PYTEST, workers=2, timeout=60)
    statuses = {f"{r.before} -> {r.after}": r.status for r in results}
    assert statuses["SELECT customer, SUM(amount) AS total -> SELECT customer, MAX(amount) AS total"] == CAUGHT
    assert statuses["WHERE status = 'paid' AND amount > 0 -> WHERE status <> 'paid' AND amount > 0"] == CAUGHT
    assert statuses["WHERE status = 'paid' AND amount > 0 -> WHERE status = 'paid' AND amount >= 0"] == SURVIVED
    assert (project / "query.sql").read_text() == QUERY


def test_a_failing_baseline_stops_the_run(project):
    (project / "test_query.py").write_text("def test_broken():\n    assert False\n")
    mutants = generate(SqlSource.read(project / "query.sql", "duckdb"), ["comparison"]).mutants
    with pytest.raises(BaselineFailedError):
        run(mutants, project, PYTEST, timeout=60)
