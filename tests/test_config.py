"""Settings choose the files and, per file, the operators."""

from pathlib import Path

import pytest

from pysqlmut.config import load_config
from pysqlmut.operators import ALL_OPERATORS

PYPROJECT = """
[tool.pysqlmut]
dialect = "snowflake"
files = ["sql/*.sql"]
exclude-operators = ["union"]

[[tool.pysqlmut.file]]
pattern = "sql/labels.sql"
exclude-operators = ["string-literal", "case"]
"""


def test_files_come_from_patterns_and_each_file_gets_its_operators(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "sql").mkdir()
    for name in ("totals.sql", "labels.sql", "notes.txt"):
        (tmp_path / "sql" / name).write_text("SELECT 1;\n")
    config = load_config(tmp_path)

    assert [path.name for path in config.files_in(tmp_path)] == ["labels.sql", "totals.sql"]
    totals = config.operators_for(Path("sql/totals.sql"), tmp_path)
    labels = config.operators_for(Path("sql/labels.sql"), tmp_path)
    assert "union" not in totals
    assert "string-literal" in totals
    assert set(ALL_OPERATORS) - set(labels) == {"union", "string-literal", "case"}


def test_an_unknown_operator_name_is_an_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[tool.pysqlmut]\nexclude-operators = ["unions"]\n')
    with pytest.raises(ValueError, match="unions"):
        load_config(tmp_path)


def test_without_settings_every_operator_runs(tmp_path: Path):
    config = load_config(tmp_path)
    assert config.operators_for(Path("any.sql"), tmp_path) == list(ALL_OPERATORS)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ('files = "sql/*.sql"', "files must be a list of strings"),
        ("workers = 0", "workers must be a positive number"),
        ("timeout = -1", "timeout must be a positive number"),
        ("operators = []", "names no operator"),
        ('[tool.pysqlmut.pytest]\npyton = "python"', r"unknown \[tool\.pysqlmut\.pytest\] settings: pyton"),
        ('[[tool.pysqlmut.file]]\nexclude-operators = ["case"]', "needs a pattern"),
    ],
)
def test_settings_of_the_wrong_kind_are_reported(tmp_path: Path, settings, message):
    (tmp_path / "pyproject.toml").write_text(f"[tool.pysqlmut]\n{settings}\n")
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path)


def test_patterns_are_globs_and_skip_the_directories_copies_leave_out(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pysqlmut]\nfiles = ["**/*.sql"]\n\n[[tool.pysqlmut.file]]\npattern = "sql/*.sql"\n'
        'exclude-operators = ["case"]\n'
    )
    for name in ("sql/top.sql", "sql/nested/deep.sql", ".venv/lib/cached.sql"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("SELECT 1;\n")
    config = load_config(tmp_path)
    found = sorted(path.relative_to(tmp_path).as_posix() for path in config.files_in(tmp_path))
    assert found == ["sql/nested/deep.sql", "sql/top.sql"]
    # A * in a glob does not cross a directory, so the rule for sql/*.sql leaves sql/nested alone.
    assert "case" not in config.operators_for(Path("sql/top.sql"), tmp_path)
    assert "case" in config.operators_for(Path("sql/nested/deep.sql"), tmp_path)
