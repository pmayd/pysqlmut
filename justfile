set dotenv-load := false

# List all recipes
[private]
default:
    just --list

# Install all dependency groups and the pre-commit hooks
setup:
    uv sync --all-groups
    uv run pre-commit install

# Format and fix what ruff can fix
format:
    uv run ruff format
    uv run ruff check --fix

# Lint without changing files
lint:
    uv run ruff format --check
    uv run ruff check

# Type check
typecheck:
    uv run ty check src tests

# Run the tests
test *ARGS='':
    uv run pytest {{ARGS}}

# Everything CI would run
check: lint typecheck test
