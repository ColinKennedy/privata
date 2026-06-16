# Privata

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/basnijholt/privata/actions/workflows/ci.yml/badge.svg)](https://github.com/basnijholt/privata/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/privata.svg)](https://pypi.org/project/privata/)
[![Python Versions](https://img.shields.io/pypi/pyversions/privata.svg)](https://pypi.org/project/privata/)
[![Docs](https://img.shields.io/badge/docs-privata.nijho.lt-blue)](http://privata.nijho.lt/)

<img src="docs/assets/logo.svg" alt="Privata logo" align="right" width="120" />

Find Python code that looks public but is only used privately.

Privata is a static checker for keeping module boundaries intentional.
It scans your production Python modules and reports four kinds of interface drift:

- public top-level functions, classes, variables, and type aliases that are only used inside their own module
- imports of private modules such as `pkg._internal` from outside their owning package subtree
- imports of private top-level symbols such as `pkg.service._Helper` from another production module
- literal `__all__` declarations that are stale, incomplete, or exporting names that do not exist

It is designed for packages and applications where `helper()` should become `_helper()` once it is no longer part of the production interface.
Test imports do not count, so tests can still reach internals without forcing those internals to stay public.

## Example

Given:

```python
# src/example/service.py
def helper() -> int:
    return 1


def run() -> int:
    return helper()
```

Privata reports:

```text
Found 1 public symbols that could be made private:

  src/example/service.py:1: function `helper`
```

## Install

```bash
uv tool install privata
```

For local development:

```bash
uv sync --extra dev --group docs
uv run pre-commit install
```

## Usage

Run Privata from a project root:

```bash
privata .
```

Privata uses `tach.toml` `source_roots` when present.
Otherwise it prefers `src/` when that directory exists, and falls back to scanning the project root while ignoring tests, virtualenvs, build output, docs output, and hidden tooling directories.

Use Privata as a pre-commit hook in another repository:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.1.2
    hooks:
      - id: privata
```

For a less strict setup that only runs when requested:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.1.2
    hooks:
      - id: privata-manual
```

```bash
pre-commit run --hook-stage manual privata-manual --all-files
```

Full output can include multiple issue types:

```text
Found 2 public symbols that could be made private:

  src/example/service.py:12: function `helper`
  src/example/service.py:21: class `InternalState`

Found 1 private module imports outside their package subtree:

  src/example/api.py:3: imports private module `example.worker._runtime`

Found 1 private symbol imports from production modules:

  src/example/api.py:4: imports private symbol `example.worker.runtime._Helper`

Found 1 __all__ export issues:

  src/example/__init__.py:5: public name `Service` missing from __all__
```

If the project is clean:

```text
No module privacy issues found.
```

## What Privata Checks

- Public top-level functions, classes, variables, and type aliases in production source roots.
- Whether those symbols are imported by another production module under those roots.
- Whether private modules such as `pkg._internal` are imported outside their containing package subtree.
- Whether private top-level symbols are imported from another production module.
- Whether literal `__all__` declarations exactly match public top-level bindings.
- Console entry points in `pyproject.toml`.
- Uvicorn entry points in shell scripts and Dockerfiles.
- Symbols exported through package `__init__.py` and `__all__`.
- Tach `[[interfaces]]` entries, when `tach.toml` is present.

Privata intentionally ignores imports from `tests/`.
If only tests import a symbol, Privata treats that symbol as private.

**Exception — test helper modules in a test source root:** when `tach.toml` lists a directory such as `tests/` under `source_roots`, non-test files inside that root (e.g. `tests/something.py`) are scanned as ordinary modules. Imports from co-located test files *do* count as cross-module usage in this case, because those helper modules exist solely to serve the test suite. A symbol that at least one test file imports is treated as public; a symbol that no test file imports is still flagged as a private candidate.

## Development

```bash
uv run pytest  # enforces 100% coverage
uv run pre-commit run --all-files
uv build
```
