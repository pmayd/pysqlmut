"""The runner classifies mutants by running a real test suite, and never changes the project."""

import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from pysqlmut.mutants import generate
from pysqlmut.runner import CAUGHT, NOT_COVERED, SURVIVED, TIMEOUT, BaselineFailedError, run
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


PACKAGED_SETUP = """
import duckdb


def paid_totals(sql):
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    return con.execute(sql).fetchall()
"""

TEST_READS_WHEN_CALLED = """
from importlib.resources import files


def test_packaged_totals():
    assert paid_totals(files("shop").joinpath("paid.sql").read_text()) == [("a", 8), ("b", 2)]
"""

# Like SQL constants in a module: the file is read once, while the module is imported.
QUERIES_MODULE = 'from importlib.resources import files\n\nPAID_SQL = files("shop").joinpath("paid.sql").read_text()\n'

TEST_READS_AT_IMPORT = """
from shop.queries import PAID_SQL


def test_imported_totals():
    assert paid_totals(PAID_SQL) == [("a", 8), ("b", 2)]
"""

TEST_HANGS = """
import os
import time

from shop.queries import PAID_SQL


def test_hangs_on_wrong_totals():
    if paid_totals(PAID_SQL) != [("a", 8), ("b", 2)]:
        with open({log!r}, "a") as log:
            log.write(f"{os.getpid()}\\n")
        time.sleep(120)
"""


def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test: str) -> Path:
    """A project whose SQL ships in a package, importable from the project's source like an editable install."""
    root = tmp_path / "packaged"
    (root / "src" / "shop").mkdir(parents=True)
    (root / "src" / "shop" / "__init__.py").write_text("")
    (root / "src" / "shop" / "paid.sql").write_text(PAID)
    (root / "src" / "shop" / "queries.py").write_text(QUERIES_MODULE)
    (root / "test_packaged.py").write_text(textwrap.dedent(PACKAGED_SETUP) + textwrap.dedent(test))
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    return root


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("test", [TEST_READS_WHEN_CALLED, TEST_READS_AT_IMPORT], ids=["when called", "at import"])
def test_a_package_imported_from_the_project_is_imported_from_the_copy(tmp_path, monkeypatch, mode, test):
    root = packaged(tmp_path, monkeypatch, test)
    paid = root / "src" / "shop" / "paid.sql"
    results = run(mutants_of(paid, "aggregate", "comparison"), root, timeout=60, **MODES[mode])
    statuses = {f"{r.before} -> {r.after}": r.status for r in results}
    assert statuses["SELECT customer, SUM(amount) AS total -> SELECT customer, MAX(amount) AS total"] == CAUGHT
    assert statuses["WHERE status = 'paid' AND amount > 0 -> WHERE status = 'paid' AND amount >= 0"] == SURVIVED


def test_a_hanging_test_times_out_and_its_process_ends(tmp_path, monkeypatch):
    log = tmp_path / "hanging.log"
    root = packaged(tmp_path, monkeypatch, TEST_HANGS.replace("{log!r}", repr(str(log))))
    paid = root / "src" / "shop" / "paid.sql"
    results = run(mutants_of(paid, "aggregate"), root, timeout=5, **MODES["pytest workers"])
    assert [r.status for r in results] == [TIMEOUT]
    pid = int(log.read_text().split()[0])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"the hanging test process {pid} is still running")


def test_a_failing_baseline_stops_the_run(project):
    (project / "test_paid.py").write_text("def test_broken():\n    assert False\n")
    with pytest.raises(BaselineFailedError):
        run(mutants_of(project / "paid.sql", "comparison"), project, timeout=60, **MODES["pytest workers"])
