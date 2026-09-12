"""Settings from [tool.pysqlmut] in the project's pyproject.toml; command line options override them.

[tool.pysqlmut]
dialect = "duckdb"
files = ["sql/*.sql"]
exclude-operators = ["union"]
accepted = "pysqlmut-accepted.json"
workers = 8
timeout = 180

[tool.pysqlmut.pytest]
python = "uv run python"
tests = ["tests/sql"]
args = ["-q", "-x"]

[[tool.pysqlmut.file]]
pattern = "sql/labels.sql"
exclude-operators = ["string-literal"]
"""

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pysqlmut.operators import ALL_OPERATORS

_KEYS = {
    "dialect",
    "files",
    "operators",
    "exclude-operators",
    "file",
    "accepted",
    "command",
    "pytest",
    "workers",
    "timeout",
}


@dataclass(frozen=True)
class FileRule:
    """Operators left out for files matching a glob pattern relative to the project."""

    pattern: str
    exclude_operators: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    dialect: str | None = None
    files: tuple[str, ...] = ()
    # None means every operator.
    operators: tuple[str, ...] | None = None
    exclude_operators: tuple[str, ...] = ()
    file_rules: tuple[FileRule, ...] = ()
    accepted: str | None = None
    command: str | None = None
    pytest_python: str | None = None
    tests: tuple[str, ...] = ()
    pytest_args: tuple[str, ...] = ()
    workers: int = 1
    timeout: float = 300.0

    def __post_init__(self) -> None:
        named = [
            *(self.operators or ()),
            *self.exclude_operators,
            *(name for rule in self.file_rules for name in rule.exclude_operators),
        ]
        unknown = sorted(set(named) - set(ALL_OPERATORS))
        if unknown:
            raise ValueError(f"unknown operators: {', '.join(unknown)}; known: {', '.join(ALL_OPERATORS)}")

    def files_in(self, project: Path) -> list[Path]:
        """The files matched by the file patterns, relative to the project."""
        return sorted({path for pattern in self.files for path in project.glob(pattern) if path.is_file()})

    def operators_for(self, relative: Path) -> list[str]:
        """The operators to run on a file, given its path relative to the project."""
        excluded = set(self.exclude_operators)
        for rule in self.file_rules:
            if fnmatch.fnmatch(relative.as_posix(), rule.pattern):
                excluded.update(rule.exclude_operators)
        return [name for name in (self.operators or ALL_OPERATORS) if name not in excluded]


def _strings(settings: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(value) for value in settings.get(key, ()))


def load_config(project: Path) -> Config:
    pyproject = project / "pyproject.toml"
    if not pyproject.exists():
        return Config()
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("pysqlmut", {})
    unknown = sorted(set(settings) - _KEYS)
    if unknown:
        raise ValueError(f"unknown [tool.pysqlmut] settings: {', '.join(unknown)}")
    pytest_settings = settings.get("pytest", {})
    return Config(
        dialect=settings.get("dialect"),
        files=_strings(settings, "files"),
        operators=_strings(settings, "operators") if "operators" in settings else None,
        exclude_operators=_strings(settings, "exclude-operators"),
        file_rules=tuple(
            FileRule(str(rule["pattern"]), _strings(rule, "exclude-operators")) for rule in settings.get("file", ())
        ),
        accepted=settings.get("accepted"),
        command=settings.get("command"),
        pytest_python=pytest_settings.get("python"),
        tests=_strings(pytest_settings, "tests"),
        pytest_args=_strings(pytest_settings, "args"),
        workers=int(settings.get("workers", 1)),
        timeout=float(settings.get("timeout", 300.0)),
    )
