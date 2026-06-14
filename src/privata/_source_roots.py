"""Source-root discovery and source-file filtering."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

if TYPE_CHECKING:
    from pathlib import Path

_IGNORED_SOURCE_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "htmlcov",
    "site",
    "tests",
}


def _is_test_module_filename(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py")


def _src_dir(project_root: Path) -> Path | None:
    """Return the conventional src/ root when it exists."""
    src = project_root / "src"
    return src if src.is_dir() else None


def _load_tach_source_roots(project_root: Path) -> list[Path]:
    """Load Tach source roots from tach.toml."""
    tach_path = project_root / "tach.toml"
    if not tach_path.exists():
        return []

    data = tomllib.loads(tach_path.read_text(encoding="utf-8"))
    source_roots = data.get("source_roots", [])
    if not isinstance(source_roots, list):
        return []

    roots: list[Path] = []
    for source_root in source_roots:
        if not isinstance(source_root, str):
            continue
        root = (project_root / source_root).resolve()
        if root.is_dir():
            roots.append(root)
    return roots


def source_roots(project_root: Path) -> list[Path]:
    """Resolve source roots for a project."""
    tach_roots = _load_tach_source_roots(project_root)
    if tach_roots:
        return tach_roots

    src = _src_dir(project_root)
    if src is not None:
        return [src]

    return [project_root]


def should_skip_source_file(py_file: Path, source_root: Path) -> bool:
    """Return whether a Python file should be ignored as non-production source."""
    if _is_test_module_filename(py_file.name):
        return True

    rel_parts = py_file.relative_to(source_root).parts
    return any(
        part in _IGNORED_SOURCE_DIR_NAMES or (part.startswith(".") and part != ".")
        for part in rel_parts[:-1]
    )
