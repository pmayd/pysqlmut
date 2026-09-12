# Releasing

Releases are built and published by `.github/workflows/release.yml` when a version tag is pushed. PyPI
trusts the workflow directly (trusted publishing), so no API token is stored anywhere.

## One-time setup

1. On PyPI, open *Your account* -> *Publishing* and add a pending trusted publisher:
   - PyPI project name: `pysqlmut`
   - Owner: `pmayd`
   - Repository name: `pysqlmut`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. On GitHub, open the repository settings -> *Environments* and create an environment named `pypi`.
   Adding yourself as a required reviewer makes every release wait for a click before it uploads.

The pending publisher turns into a normal one with the first upload, which also creates the project.

## Each release

1. Update the version (not for 0.1.0, which the project already has): `uv version --bump minor` (or `patch`, `major`, or `uv version 0.2.0`).
2. In `CHANGELOG.md`, rename *Unreleased* to the new version with today's date and start a new empty
   *Unreleased* section.
3. Run `just check`, commit, and push to `main`; wait for CI to pass.
4. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The workflow checks that the tag matches the version in `pyproject.toml`, builds the sdist and wheel,
uploads them to PyPI, and creates a GitHub release with the files and generated notes.

## Trying a release without publishing

```bash
uv build
uvx twine check --strict dist/*
uv tool run --from ./dist/pysqlmut-*.whl pysqlmut --help
```
