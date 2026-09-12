"""End to end through the command line."""

import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pysqlmut.cli import app, main
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


def invoke(*args: str) -> int:
    return CliRunner().invoke(app, list(args)).exit_code


def test_accepted_survivors_are_neither_rerun_nor_reported(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(PYPROJECT)
    (project / "paid.sql").write_text(PAID)
    (project / "test_paid.py").write_text(textwrap.dedent(TEST))
    report = tmp_path / "report.json"

    assert invoke("run", "--project", str(project), "--report", str(report)) == 1
    first = load(report)
    assert {r.status for r in first} == {CAUGHT, SURVIVED}

    assert invoke("accept", str(report), "--project", str(project)) == 0
    assert (project / "pysqlmut-accepted.json").exists()

    assert invoke("run", "--project", str(project), "--report", str(report)) == 0
    second = load(report)
    assert [r.status for r in second] == [ACCEPTED if r.status == SURVIVED else r.status for r in first]
    assert all(r.seconds == 0.0 for r in second if r.status == ACCEPTED)


def test_a_file_sqlglot_cannot_tokenize_is_skipped_and_the_others_still_run(tmp_path: Path):
    (tmp_path / "broken.sql").write_text("SELECT 'never closed FROM t;\n")
    (tmp_path / "paid.sql").write_text(PAID)
    files = [str(tmp_path / "broken.sql"), str(tmp_path / "paid.sql")]
    result = CliRunner().invoke(app, ["list", *files, "--dialect", "duckdb", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "broken.sql: skipped, cannot read it" in result.output
    assert "paid.sql: 1 statements" in result.output


def paid_project(tmp_path: Path, pyproject: str = PYPROJECT, test: str = TEST) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(pyproject)
    (project / "paid.sql").write_text(PAID)
    (project / "test_paid.py").write_text(textwrap.dedent(test))
    return project


def test_usage_mistakes_end_with_one_line_and_exit_code_2(tmp_path: Path):
    (tmp_path / "paid.sql").write_text(PAID)
    sql, project = str(tmp_path / "paid.sql"), str(tmp_path)
    for args in (
        ["generate", sql, "--dialect", "no_such_dialect", "--project", project],
        ["generate", str(tmp_path / "missing.sql"), "--dialect", "duckdb", "--project", project],
        ["generate", sql, "--dialect", "duckdb", "--workers", "0", "--project", project],
        ["report", sql],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert "Traceback" not in result.output


def test_a_run_whose_tests_fail_without_a_mutant_exits_with_3(tmp_path: Path):
    project = paid_project(tmp_path, test="def test_broken():\n    assert False\n")
    assert invoke("run", "--project", str(project)) == 3


def test_report_and_accept_take_only_the_chosen_statuses_and_operators(tmp_path: Path):
    project = paid_project(tmp_path, PYPROJECT.replace('["comparison"]', '["comparison", "literal"]'))
    report = tmp_path / "report.json"
    # No order has an amount of 1 either, so amount > 1 survives next to amount >= 0.
    assert invoke("run", "--project", str(project), "--report", str(report)) == 1
    caught = CliRunner().invoke(app, ["report", str(report), "--status", "caught"])
    assert "1 results with status caught" in caught.output

    assert invoke("accept", str(report), "--project", str(project), "--operators", "literal") == 0
    assert invoke("run", "--project", str(project), "--report", str(report)) == 1
    remaining = {(r.operator, r.status) for r in load(report) if r.status != CAUGHT}
    assert remaining == {("literal", ACCEPTED), ("comparison", SURVIVED)}


def test_a_failure_inside_pysqlmut_exits_with_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(project: Path):
        raise RuntimeError("a bug")

    monkeypatch.setattr("pysqlmut.cli.load_config", broken)
    monkeypatch.setattr(sys, "argv", ["pysqlmut", "generate", "--dialect", "duckdb", "--project", str(tmp_path)])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 4
