"""The runner classifies mutants by running a real test suite, and never changes the project."""

import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from samples import PAID, TEST_PAID

from pysqlmut.mutants import generate
from pysqlmut.runner import CAUGHT, NOT_COVERED, SURVIVED, TIMEOUT, BaselineFailedError, ProjectReadError, _env, run
from pysqlmut.source import SqlSource

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


def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test: str, queries: str = QUERIES_MODULE) -> Path:
    """A project whose SQL ships in a package, importable from the project's source like an editable install."""
    root = tmp_path / "packaged"
    (root / "src" / "shop").mkdir(parents=True)
    (root / "src" / "shop" / "__init__.py").write_text("")
    (root / "src" / "shop" / "paid.sql").write_text(PAID)
    (root / "src" / "shop" / "queries.py").write_text(textwrap.dedent(queries))
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


def _assert_ended(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} is still running")


CONFTEST_SESSION = """
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def paid_sql():
    return (Path(__file__).parent / "paid.sql").read_text()
"""

TEST_SESSION = """
import duckdb


def test_query_is_not_empty(paid_sql):
    assert paid_sql.strip()


def test_paid_totals(paid_sql):
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    assert con.execute(paid_sql).fetchall() == [("a", 8), ("b", 2)]
"""


def test_a_file_read_by_a_session_fixture_runs_every_test_that_uses_the_fixture(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    (root / "paid.sql").write_text(PAID)
    (root / "conftest.py").write_text(textwrap.dedent(CONFTEST_SESSION))
    (root / "test_paid.py").write_text(textwrap.dedent(TEST_SESSION))
    results = run(mutants_of(root / "paid.sql", "aggregate"), root, timeout=60, **MODES["pytest workers"])
    assert [(r.status, r.tests) for r in results] == [(CAUGHT, 2)]


QUERIES_CACHED = """
from functools import cache
from importlib.resources import files


@cache
def paid_sql():
    return files("shop").joinpath("paid.sql").read_text()
"""

TEST_READS_CACHED = """
from shop.queries import paid_sql


def test_cached_totals():
    assert paid_totals(paid_sql()) == [("a", 8), ("b", 2)]
"""


def test_a_file_a_function_caches_is_read_again_for_every_mutant(tmp_path, monkeypatch):
    root = packaged(tmp_path, monkeypatch, TEST_READS_CACHED, queries=QUERIES_CACHED)
    paid = root / "src" / "shop" / "paid.sql"
    results = run(mutants_of(paid, "aggregate"), root, timeout=60, **MODES["pytest workers"])
    assert [r.status for r in results] == [CAUGHT]


TEST_BELOW_PROJECT = """
from pathlib import Path

import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    sql = (Path(__file__).parent.parent / "paid.sql").read_text()
    assert con.execute(sql).fetchall() == [("a", 8), ("b", 2)]
"""


def test_tests_below_their_own_pytest_ini_are_selected_from_the_project_root(tmp_path):
    root = tmp_path / "nested"
    (root / "tests").mkdir(parents=True)
    (root / "paid.sql").write_text(PAID)
    # pytest takes tests/ as its rootdir and names the tests relative to it.
    (root / "tests" / "pytest.ini").write_text("[pytest]\n")
    (root / "tests" / "test_paid.py").write_text(textwrap.dedent(TEST_BELOW_PROJECT))
    options: dict[str, Any] = {**MODES["pytest workers"], "tests": ["tests"]}
    results = run(mutants_of(root / "paid.sql", "aggregate"), root, timeout=60, **options)
    assert [(r.status, r.tests) for r in results] == [(CAUGHT, 1)]


TEST_IMPORTS_CONFIG = """
from pathlib import Path

import duckdb
from config import EXPECTED


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    assert con.execute((Path(__file__).parent / "paid.sql").read_text()).fetchall() == EXPECTED
"""


def test_a_project_module_named_like_a_pysqlmut_module_is_imported_from_the_project(tmp_path, monkeypatch):
    root = tmp_path / "shadow"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "config.py").write_text('EXPECTED = [("a", 8), ("b", 2)]\n')
    (root / "paid.sql").write_text(PAID)
    (root / "test_paid.py").write_text(textwrap.dedent(TEST_IMPORTS_CONFIG))
    monkeypatch.setenv("PYTHONPATH", str(root / "lib"))
    results = run(mutants_of(root / "paid.sql", "aggregate"), root, timeout=60, **MODES["pytest workers"])
    assert [r.status for r in results] == [CAUGHT]


def test_a_mutant_in_a_directory_the_copies_leave_out_is_refused(project):
    # The copies link to the project's .venv, so a mutant written there would change the real file.
    (project / ".venv").mkdir()
    (project / ".venv" / "cached.sql").write_text(UNREAD)
    with pytest.raises(ValueError, match="leave out"):
        run(mutants_of(project / ".venv" / "cached.sql", "comparison"), project, timeout=60, **MODES["command"])


def test_a_command_that_times_out_ends_together_with_the_processes_it_started(project, tmp_path):
    pid_file = tmp_path / "sleep.pid"
    tests = f"{sys.executable} -m pytest -q -p no:cacheprovider"
    command = f"if grep -q '<>' paid.sql; then sleep 120 & echo $! > {pid_file}; wait; else {tests}; fi"
    results = run(mutants_of(project / "paid.sql", "comparison"), project, command=command, timeout=10)
    statuses = {r.after: r.status for r in results}
    assert statuses["WHERE status <> 'paid' AND amount > 0"] == TIMEOUT
    _assert_ended(int(pid_file.read_text()))


TEST_HANGS_ON_BOUNDARY = """
import os
import time
from pathlib import Path

import duckdb


def test_paid_totals():
    sql = (Path(__file__).parent / "paid.sql").read_text()
    if ">= 0" in sql:
        Path(PID_FILE).write_text(str(os.getpid()))
        time.sleep(120)
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    assert con.execute(sql).fetchall() == [("a", 8), ("b", 2)]
"""


def test_an_interrupted_run_stops_its_tests_at_once(tmp_path):
    root = tmp_path / "interrupted"
    root.mkdir()
    pid_file = tmp_path / "hanging.pid"
    (root / "paid.sql").write_text(PAID)
    (root / "test_paid.py").write_text(f"PID_FILE = {str(pid_file)!r}\n" + textwrap.dedent(TEST_HANGS_ON_BOUNDARY))

    def interrupt(result):
        raise KeyboardInterrupt

    mutants = mutants_of(root / "paid.sql", "comparison")
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run(mutants, root, workers=2, timeout=300, progress=interrupt, **MODES["pytest workers"])
    assert time.monotonic() - started < 60
    if pid_file.exists():
        _assert_ended(int(pid_file.read_text()))


TEST_READS_THE_PROJECT = """
import duckdb


def test_paid_totals():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (customer VARCHAR, amount INT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('a', 5, 'paid'), ('a', 3, 'paid'), ('b', 2, 'paid')")
    with open(PROJECT_FILE) as file:
        assert con.execute(file.read()).fetchall() == [("a", 8), ("b", 2)]
"""


def test_tests_that_read_the_project_instead_of_the_copy_stop_the_run(tmp_path):
    root = tmp_path / "absolute"
    root.mkdir()
    (root / "paid.sql").write_text(PAID)
    header = f"PROJECT_FILE = {str(root / 'paid.sql')!r}\n"
    (root / "test_paid.py").write_text(header + textwrap.dedent(TEST_READS_THE_PROJECT))
    with pytest.raises(ProjectReadError, match=r"paid\.sql"):
        run(mutants_of(root / "paid.sql", "aggregate"), root, timeout=60, **MODES["pytest workers"])


def test_a_file_with_windows_line_endings_keeps_them_in_every_mutant(tmp_path):
    root = tmp_path / "crlf"
    root.mkdir()
    (root / "paid.sql").write_bytes(PAID.replace("\n", "\r\n").encode())
    (root / "check.py").write_text('import sys\nsys.exit(0 if b"\\r\\n" in open("paid.sql", "rb").read() else 1)\n')
    results = run(mutants_of(root / "paid.sql", "comparison"), root, command=f"{sys.executable} check.py", timeout=60)
    assert results
    assert {r.status for r in results} == {SURVIVED}


def test_an_editable_install_of_the_project_is_imported_from_the_copy(tmp_path):
    project = (tmp_path / "project").resolve()
    site_packages = project / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "_shop.pth").write_text(f"{project / 'src'}\n")
    copy = tmp_path / "copy"
    assert _env(project, copy)["PYTHONPATH"].split(os.pathsep)[0] == str(copy / "src")
