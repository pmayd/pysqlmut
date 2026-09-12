"""End to end: run from settings, accept the survivors, and the next run passes without rerunning them."""

import sys
import textwrap
from pathlib import Path

from pysqlmut.cli import main
from pysqlmut.report import load
from pysqlmut.runner import ACCEPTED, CAUGHT, SURVIVED

PAID = """SELECT customer, SUM(amount) AS total
FROM orders
WHERE status = 'paid' AND amount > 0
GROUP BY customer
ORDER BY customer;
"""

# No order has amount 0, so amount > 0 and amount >= 0 give the same result.
TEST = """
from pathlib import Path

import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('a', 7, 'open'), ('b', 2, 'paid')")
    assert con.execute((Path(__file__).parent / "paid.sql").read_text()).fetchall() == [("a", 8), ("b", 2)]
"""

PYPROJECT = f"""
[tool.pysqlmut]
dialect = "duckdb"
files = ["*.sql"]
operators = ["comparison"]

[tool.pysqlmut.pytest]
python = {sys.executable!r}
tests = ["."]
args = ["-q", "-p", "no:cacheprovider"]
"""


def test_accepted_survivors_are_neither_rerun_nor_reported(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(PYPROJECT)
    (project / "paid.sql").write_text(PAID)
    (project / "test_paid.py").write_text(textwrap.dedent(TEST))
    report = tmp_path / "report.json"

    assert main(["run", "--project", str(project), "--report", str(report)]) == 1
    first = load(report)
    assert {r.status for r in first} == {CAUGHT, SURVIVED}

    assert main(["accept", str(report), "--project", str(project)]) == 0
    assert (project / "pysqlmut-accepted.json").exists()

    assert main(["run", "--project", str(project), "--report", str(report)]) == 0
    second = load(report)
    assert [r.status for r in second] == [ACCEPTED if r.status == SURVIVED else r.status for r in first]
    assert all(r.seconds == 0.0 for r in second if r.status == ACCEPTED)
