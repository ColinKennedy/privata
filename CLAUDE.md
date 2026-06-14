# Privata

A Python module privacy checker. Uses AST analysis to flag publicly-accessible symbols that are never imported by another production module, private modules imported outside their package subtree, private symbols imported across module boundaries, and `__all__` export mismatches.

## Environment setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --group docs
```

- `--extra dev` installs pytest, ruff, mypy, ty, pre-commit.
- `--group docs` installs zensical for the documentation site.

Install pre-commit hooks after the first sync:

```bash
uv run pre-commit install
```

## Running tests

```bash
uv run pytest
```

Tests require 100% branch coverage. Coverage is enforced automatically via `--cov-fail-under=100` in `pyproject.toml`.

## Linting and type checking

These are also run as pre-commit hooks on every commit.

```bash
uv run ruff check .          # lint
uv run ruff format --check . # format check (drop --check to apply)
uv run mypy src tests        # type check (strict mode)
uv run ty check              # experimental Astral type checker
uv run privata .             # run privata on itself
```

Run all hooks at once:

```bash
uv run pre-commit run --all-files
```

## Building

```bash
uv build
```

Produces `dist/test_privata-<version>-py3-none-any.whl` and a source distribution. The version is derived from git tags via `hatch-vcs` — there is no manually maintained version string. `src/privata/_version.py` is auto-generated and not tracked in git.

## Publishing to PyPI

Tag the release first (version is inferred from the tag):

```bash
git tag v0.2.0
git push origin v0.2.0
```

Then build and publish:

```bash
uv build
uv publish
```

`uv publish` reads credentials from `UV_PUBLISH_TOKEN` or `~/.pypi` credentials. There is no automated publish CI workflow — publishing is done manually.

## Documentation

```bash
uv run zensical build   # build to ./site/
uv run zensical serve   # live preview at localhost
```

Source lives in `docs/`. The site is deployed to GitHub Pages automatically from `main` via `.github/workflows/docs.yml`.

## Project structure

```
src/privata/
  _checker.py       # top-level orchestration: find_private_candidates, find_private_module_imports, etc.
  _imports.py       # cross-import detection and private import collection
  _exports.py       # __all__ export validation
  _modules.py       # AST parsing and symbol extraction
  _entrypoints.py   # external entrypoint discovery (pyproject.toml, shell scripts, tach.toml)
  _source_roots.py  # source root detection (src/, tach.toml, fallback)
  _models.py        # dataclasses: Module, Symbol, PrivateModuleImport, etc.
  cli.py            # argparse CLI entrypoint (console script: privata)
tests/
  test_checker.py   # all behavioral tests; one file, tmp_path fixture pattern
```

## Pre-commit hook (for consumers)

This repo ships a reusable hook. In a downstream project's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/ColinKennedy/privata
    rev: v0.1.1
    hooks:
      - id: privata          # runs on every commit
      - id: privata-manual   # runs only when explicitly invoked
```

## Key invariants

- No runtime dependencies — stdlib only.
- Test files (under `tests/`) are excluded from cross-import analysis; only production source roots count.
- Symbols used only in tests remain flagged as private candidates.
- `__all__` validation only triggers when the list is a literal (dynamic `__all__` is ignored).
