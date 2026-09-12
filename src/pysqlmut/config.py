"""Settings from [tool.pysqlmut] in the project's pyproject.toml; the README lists them with an example.

Command line options override these settings. Paths in the settings are relative to the project.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pysqlmut.operators import ALL_OPERATORS

# Directories no mutant is taken from: the project copies share the virtual environment, and the rest is cache.
IGNORED_DIRECTORIES = (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules")

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
_PYTEST_KEYS = {"python", "tests", "args"}
_FILE_KEYS = {"pattern", "exclude-operators"}


@dataclass(frozen=True)
class FileRule:
    """Operators left out for the files matching a glob pattern relative to the project."""

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
        if self.operators is not None and not self.operators:
            raise ValueError("operators names no operator; leave it out to run every operator")
        named = [
            *(self.operators or ()),
            *self.exclude_operators,
            *(name for rule in self.file_rules for name in rule.exclude_operators),
        ]
        unknown = sorted(set(named) - set(ALL_OPERATORS))
        if unknown:
            raise ValueError(f"unknown operators: {', '.join(unknown)}; known: {', '.join(ALL_OPERATORS)}")

    def files_in(self, project: Path) -> list[Path]:
        """The files the file patterns match, as absolute paths."""
        return sorted({project / path for pattern in self.files for path in _glob(project.resolve(), pattern)})

    def operators_for(self, relative: Path, project: Path) -> list[str]:
        """The operators to run on a file, given its path relative to the project."""
        excluded = set(self.exclude_operators)
        for rule in self.file_rules:
            if relative in _glob(project.resolve(), rule.pattern):
                excluded.update(rule.exclude_operators)
        return [name for name in (self.operators or ALL_OPERATORS) if name not in excluded]


def _glob(project: Path, pattern: str) -> frozenset[Path]:
    """The files a glob pattern matches, relative to the project, outside the ignored directories."""
    matches = (path.relative_to(project) for path in project.glob(pattern) if path.is_file())
    return frozenset(path for path in matches if not set(path.parts) & set(IGNORED_DIRECTORIES))


def _check_keys(settings: dict[str, Any], known: set[str], table: str) -> None:
    unknown = sorted(set(settings) - known)
    if unknown:
        raise ValueError(f"unknown {table} settings: {', '.join(unknown)}")


def _strings(settings: dict[str, Any], key: str, table: str) -> tuple[str, ...]:
    value = settings.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{table} {key} must be a list of strings")
    return tuple(value)


def _string(settings: dict[str, Any], key: str, table: str) -> str | None:
    value = settings.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{table} {key} must be a string")
    return value


def _number(settings: dict[str, Any], key: str, default: float, table: str) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{table} {key} must be a positive number")
    return value


def load_config(project: Path) -> Config:
    pyproject = project / "pyproject.toml"
    if not pyproject.exists():
        return Config()
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("pysqlmut", {})
    _check_keys(settings, _KEYS, "[tool.pysqlmut]")
    pytest_settings = settings.get("pytest", {})
    _check_keys(pytest_settings, _PYTEST_KEYS, "[tool.pysqlmut.pytest]")
    rules = []
    for rule in settings.get("file", []):
        _check_keys(rule, _FILE_KEYS, "[[tool.pysqlmut.file]]")
        pattern = _string(rule, "pattern", "[[tool.pysqlmut.file]]")
        if pattern is None:
            raise ValueError("every [[tool.pysqlmut.file]] needs a pattern")
        rules.append(FileRule(pattern, _strings(rule, "exclude-operators", "[[tool.pysqlmut.file]]")))
    workers = _number(settings, "workers", 1, "[tool.pysqlmut]")
    if not isinstance(workers, int):
        raise ValueError("[tool.pysqlmut] workers must be a whole number")
    table = "[tool.pysqlmut]"
    return Config(
        dialect=_string(settings, "dialect", table),
        files=_strings(settings, "files", table),
        operators=_strings(settings, "operators", table) if "operators" in settings else None,
        exclude_operators=_strings(settings, "exclude-operators", table),
        file_rules=tuple(rules),
        accepted=_string(settings, "accepted", table),
        command=_string(settings, "command", table),
        pytest_python=_string(pytest_settings, "python", "[tool.pysqlmut.pytest]"),
        tests=_strings(pytest_settings, "tests", "[tool.pysqlmut.pytest]"),
        pytest_args=_strings(pytest_settings, "args", "[tool.pysqlmut.pytest]"),
        workers=workers,
        timeout=float(_number(settings, "timeout", 300.0, table)),
    )
