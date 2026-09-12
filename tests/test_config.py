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
    totals = config.operators_for(Path("sql/totals.sql"))
    labels = config.operators_for(Path("sql/labels.sql"))
    assert "union" not in totals
    assert "string-literal" in totals
    assert set(ALL_OPERATORS) - set(labels) == {"union", "string-literal", "case"}


def test_an_unknown_operator_name_is_an_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[tool.pysqlmut]\nexclude-operators = ["unions"]\n')
    with pytest.raises(ValueError, match="unions"):
        load_config(tmp_path)


def test_without_settings_every_operator_runs(tmp_path: Path):
    config = load_config(tmp_path)
    assert config.operators_for(Path("any.sql")) == list(ALL_OPERATORS)
