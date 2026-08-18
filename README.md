# Privata

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/basnijholt/privata/actions/workflows/ci.yml/badge.svg)](https://github.com/basnijholt/privata/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/privata.svg)](https://pypi.org/project/privata/)
[![Python Versions](https://img.shields.io/pypi/pyversions/privata.svg)](https://pypi.org/project/privata/)
[![Docs](https://img.shields.io/badge/docs-privata.nijho.lt-blue)](http://privata.nijho.lt/)

<img src="docs/assets/logo.svg" alt="Privata logo" align="right" width="120" />

Find Python code that looks public but is only used privately.

Privata is a static checker for keeping module boundaries intentional.
It scans your production Python modules and reports five kinds of interface drift:

- public top-level functions, classes, variables, and type aliases that are only used inside their own module
- public methods such as `Service.helper` that no other production module ever refers to (opt in with `--methods`)
- imports of private modules such as `pkg._internal` from outside their owning package subtree
- imports of private top-level symbols such as `pkg.service._Helper` from another production module
- literal `__all__` declarations that are stale, incomplete, or exporting names that do not exist

It is designed for packages and applications where `helper()` should become `_helper()` once it is no longer part of the production interface.
Imports from a project's own tests do not count, so those tests can still reach internals without forcing them to stay public. Imports from another project's tests do count.

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
Found 1 public symbol that could be made private:

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
    rev: v0.8.0
    hooks:
      - id: privata
```

Run `pre-commit autoupdate` to move `rev` to the newest release.

For a less strict setup that only runs when requested:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.8.0
    hooks:
      - id: privata-manual
```

```bash
pre-commit run --hook-stage manual privata-manual --all-files
```

Full output can include multiple issue types (`--methods` adds the method section):

```text
Found 2 public symbols that could be made private:

  src/example/service.py:12: function `helper`
  src/example/service.py:21: class `InternalState`

Found 2 public methods in 1 class that could be made private:

  src/example/service.py:21: class `InternalState` (2 of 5 public methods)
      reset:25, drain:31

Found 1 private module import outside the owning package subtree:

  src/example/api.py:3: imports private module `example.worker._runtime`

Found 1 private symbol import from production modules:

  src/example/api.py:4: imports private symbol `example.worker.runtime._Helper`

Found 1 __all__ export issue:

  src/example/__init__.py:5: public name `Service` missing from __all__
```

If the project is clean:

```text
No module privacy issues found.
```

## What Privata Checks

- Public top-level functions, classes, variables, and type aliases in production source roots.
- Whether those symbols are imported by another production module under those roots.
- Public methods of plain classes, and whether any other production module refers to them by name.
- Whether private modules such as `pkg._internal` are imported outside their containing package subtree.
- Whether private top-level symbols are imported from another production module.
- Whether literal `__all__` declarations exactly match public top-level bindings.
- Console entry points in `pyproject.toml`.
- Uvicorn entry points in shell scripts and Dockerfiles.
- Symbols exported through package `__init__.py` and `__all__`.
- Tach `[[interfaces]]` entries, when `tach.toml` is present.
- Module names defined by more than one file across source roots (e.g. `src/utils.py` next to `tests/utils.py`, or `pkg.py` next to `pkg/__init__.py`). Such names are ambiguous at import time and only one file per name can be scanned, so Privata reports the collision instead of silently picking one.
- Source files that cannot be parsed. A skipped file stops contributing references, so unrelated modules gain findings that are not real while genuine findings disappear. Privata reports the file first and fails the check, rather than reporting results it cannot stand behind. The usual cause is running Privata on an interpreter older than the syntax in the project, such as scanning a project that uses `type X = ...` on Python 3.11.

### Methods

A public method is reported when no other production module mentions its name, either as an attribute access such as `service.helper()` or as a string literal such as `getattr(service, "helper")`.
Use inside the defining module does not keep a method public, exactly as for top-level symbols.
Matching is intentionally name-based rather than receiver-aware, so an unrelated attribute with the same name in another module conservatively suppresses the report.

Because a method name can be owned by something Privata cannot see, the method check only looks at plain classes.
It skips:

- classes with a base class other than `object`, since the base may define the contract
- classes that another class in the project subclasses, since renaming a base method would strand the override under its old name
- classes that reach attributes by a computed name, such as `getattr(self, "visit_" + kind)`, since such a class may call any of its own methods without ever spelling the name out
- classes with class keywords such as `metaclass=`, and classes with decorators other than `@dataclass` and `@final`
- private classes, classes listed in `__all__`, classes re-exported by a package `__init__.py` or named in another module's `__all__`, and classes exposed through entry points or Tach interfaces
- methods with decorators other than `@property`, `@staticmethod`, `@classmethod`, `@cached_property`, `@cache`, `@lru_cache`, and `@final`, since another decorator may register the method under its current name
- methods that call the same method through `super()`, since cooperative mixins must preserve that name
- dunder methods, methods that are already private, and classes nested inside functions or other classes

Detection of computed attribute names is deliberately shallow: it looks for `getattr`, `setattr`, `hasattr`, and `delattr` called with a name Privata cannot read as a literal.
Dispatch that Privata cannot see at all is **not supported** and will produce false positives. That includes a lookup table of bound methods assembled in another module, a name forwarded through `**kwargs`, `operator.attrgetter`, and anything reached through `eval` or `globals()`.
If your code dispatches that way, use `__all__`, a Tach interface entry, or `# privata: ignore`.

Privata intentionally ignores imports from its own `tests/`.
If a symbol is only imported by its own test suite, Privata treats that symbol as private. Imports from another project's test suite keep the symbol public.

**Exception — test helper modules in a test source root:** when `tach.toml` lists a directory such as `tests/` under `source_roots`, non-test files inside that root (e.g. `tests/something.py`) are scanned as ordinary modules. Imports from co-located test files *do* count as cross-module usage in this case, because those helper modules exist solely to serve the test suite. A symbol that at least one test file imports is treated as public; a symbol that no test file imports is still flagged as a private candidate.
For methods, a test file that imports a helper module certifies every method name that file mentions. Attribution is per file rather than per receiver, so a helper a test never imports is still checked, while a helper it does import is credited generously.

## Development

```bash
uv run pytest  # enforces 100% coverage
uv run pre-commit run --all-files
uv build
```
