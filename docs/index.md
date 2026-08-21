---
icon: lucide/shield-check
---

# Privata

**Keep Python module interfaces intentional**

<div style="text-align: center; margin: 1.5rem 0;">
  <img src="assets/logo.svg" alt="Privata logo" width="140" />
</div>

Privata scans Python source roots and reports public symbols and methods that are only used inside their own module.
It also reports private module imports that cross package boundaries.

[PyPI package](https://pypi.org/project/privata/) · [GitHub repository](https://github.com/basnijholt/privata)

## Quick Start

```bash
uv tool install privata
privata .
```

Continue with [Getting Started](getting-started.md), or see the [usage guide](usage.md) for the full checker behavior.

## Features

- Finds public module-level functions, classes, variables, and type aliases that can be made private.
- Finds public methods of plain classes that no other production module refers to, when you pass `--methods`.
- Ignores test imports when deciding whether a symbol is public.
- Detects private modules imported from outside their owning package subtree.
- Detects private top-level symbols imported by another production module.
- Validates literal `__all__` declarations for stale and missing exports.
- Honors package `__init__.py` re-exports and literal `__all__` declarations.
- Honors `pyproject.toml` console entry points, Uvicorn shell entry points, and Tach interfaces.
- Uses Tach `source_roots`, `src/`, or the project root depending on the repository layout.
- Supports `privata_search_paths` to search a wider tree for cross-references without reporting on it, for incremental adoption.
- Uses only the Python standard library at runtime.

## Example

```text
Found 2 public symbols that could be made private:

  src/example/service.py:12: function `helper`
  src/example/service.py:21: class `InternalState`

Found 2 public methods in 1 class that could be made private:

  src/example/service.py:21: class `InternalState` (2 of 5 public methods)
      reset:25, drain:31
```
