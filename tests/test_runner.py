"""The runner classifies mutants by running a real test suite, and never changes the project."""

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from pysqlmut.mutants import generate
from pysqlmut.runner import CAUGHT, NOT_COVERED, SURVIVED, BaselineFailedError, run
from pysqlmut.source import SqlSource

PAID = """-- Paid orders per customer.
SELECT customer, SUM(amount) AS total
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY customer;
"""

# The test checks the totals of paid orders but has no order with amount 0, so it cannot tell
# amount > 0 from amount >= 0.
TEST_PAID = """
from pathlib import Path

import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('a', 7, 'open'), ('b', 2, 'paid')")
    assert con.execute((Path(__file__).parent / "paid.sql").read_text()).fetchall() == [("a", 8), ("b", 2)]
"""

OPEN = "SELECT COUNT(*) FROM orders WHERE status = 'open';\n"

# Writes a line to a log outside the project copies whenever it runs, so a test can count its runs.
TEST_OPEN = """
from pathlib import Path

import duckdb


def test_open_count():
    with open({log!r}, "a") as log:
        log.write("ran\\n")
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 7, 'open')")
    assert con.execute((Path(__file__).parent / "open.sql").read_text()).fetchall() == [(1,)]
"""

UNREAD = "SELECT 1 WHERE 1 = 1;\n"

PYTEST_COMMAND = f"{sys.executable} -m pytest -q -p no:cacheprovider"
MODES: dict[str, dict[str, Any]] = {
    "command": {"command": PYTEST_COMMAND},
    "pytest workers": {"pytest_python": sys.executable, "pytest_args": ["-q", "-p", "no:cacheprovider"]},
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "paid.sql").write_text(PAID)
    (root / "test_paid.py").write_text(textwrap.dedent(TEST_PAID))
    (root / "open.sql").write_text(OPEN)
    (root / "test_open.py").write_text(textwrap.dedent(TEST_OPEN).replace("{log!r}", repr(str(tmp_path / "runs.log"))))
    (root / "unread.sql").write_text(UNREAD)
    return root


def mutants_of(path: Path, *operators: str):
    # Mutants carry the absolute path they were read from; the runner must still only change its copies.
    return generate(SqlSource.read(path, "duckdb"), list(operators)).mutants


@pytest.mark.parametrize("mode", MODES)
def test_a_mutant_that_changes_the_result_is_caught_and_an_invisible_one_survives(project, mode):
    results = run(
        mutants_of(project / "paid.sql", "comparison", "aggregate"), project, workers=2, timeout=60, **MODES[mode]
    )
    statuses = {f"{r.before} -> {r.after}": r.status for r in results}
    assert statuses["SELECT customer, SUM(amount) AS total -> SELECT customer, MAX(amount) AS total"] == CAUGHT
    assert statuses["WHERE status = 'paid' AND amount > 0 -> WHERE status <> 'paid' AND amount > 0"] == CAUGHT
    assert statuses["WHERE status = 'paid' AND amount > 0 -> WHERE status = 'paid' AND amount >= 0"] == SURVIVED
    assert (project / "paid.sql").read_text() == PAID


def test_pytest_workers_run_only_the_tests_that_read_the_mutated_file(project):
    results = run(mutants_of(project / "paid.sql", "comparison"), project, timeout=60, **MODES["pytest workers"])
    assert len(results) > 1
    assert {r.tests for r in results} == {1}
    # Only the recording baseline ran test_open; none of the mutants of paid.sql ran it again.
    assert (project.parent / "runs.log").read_text().splitlines() == ["ran"]


def test_a_file_no_test_reads_is_not_covered_and_not_run(project):
    results = run(mutants_of(project / "unread.sql", "comparison"), project, timeout=60, **MODES["pytest workers"])
    assert results
    assert {r.status for r in results} == {NOT_COVERED}


def test_a_failing_baseline_stops_the_run(project):
    (project / "test_paid.py").write_text("def test_broken():\n    assert False\n")
    with pytest.raises(BaselineFailedError):
        run(mutants_of(project / "paid.sql", "comparison"), project, timeout=60, **MODES["pytest workers"])
