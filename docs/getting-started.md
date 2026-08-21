---
icon: lucide/rocket
---

# Getting Started

## Prerequisites

You need:

- Python 3.10+
- a Python project with importable source files

## Installation

=== "uv tool"

    ```bash
    uv tool install privata
    ```

=== "pipx"

    ```bash
    pipx install privata
    ```

=== "pip"

    ```bash
    pip install privata
    ```

=== "From source"

    ```bash
    git clone https://github.com/basnijholt/privata.git
    cd privata
    uv sync --extra dev
    ```

## Run

From a project root:

```bash
privata .
```

Privata exits with status `0` when no privacy issues are found.
It exits with status `1` when it finds public symbols that can be made private, or private module imports that cross package boundaries.
The method check is opt-in; add `--methods` to include it.

## Pre-Commit

Add Privata to another repository's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.8.0
    hooks:
      - id: privata
```

Use `id: privata-manual` instead if you only want to run Privata on demand:

```bash
pre-commit run --hook-stage manual privata-manual --all-files
```

## Source Roots

Privata uses `tach.toml` `source_roots` when present:

```toml
source_roots = ["lib"]
```

Without Tach source roots, Privata prefers a `src/` directory:

```text
project/
├── pyproject.toml
└── src/
    └── package/
        ├── __init__.py
        └── module.py
```

## Adopting Privata Incrementally

`source_roots` controls both what gets *searched* and what gets *reported*.
To onboard a large codebase gradually, add `privata_search_paths` alongside
`source_roots` in `tach.toml`: it widens the search without widening the
report.

```toml
source_roots = ["python/some/subfolder"]
privata_search_paths = ["python"]
```

This searches all of `python/` for cross-references — so a symbol defined
in `python/some/subfolder` that's used elsewhere in `python/` is correctly
recognized as used — but only reports findings inside
`python/some/subfolder`. Expand `source_roots` over time as more of the
codebase is ready to be checked.

If `src/` is absent, Privata scans the project root and ignores tests, virtualenvs, build output, docs output, and hidden tooling directories.
Tests can live anywhere.
Imports from tests do not count when deciding whether a symbol should stay public.
