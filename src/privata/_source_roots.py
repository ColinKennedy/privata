"""Source-root discovery and source-file filtering."""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):  # pragma: no cover
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


_TEST_FILENAME_RE = re.compile(r"(?:test_\w+|\w+_test|test[A-Z]\w*|\w+Test)\.py")


def is_test_module_filename(name: str) -> bool:
    """Return whether a filename matches a recognised test-file naming convention."""
    return bool(_TEST_FILENAME_RE.fullmatch(name))


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
    return is_test_module_filename(py_file.name) or is_in_ignored_directory(py_file, source_root)


def is_in_ignored_directory(py_file: Path, source_root: Path) -> bool:
    """Return whether a file sits inside an ignored or hidden directory."""
    rel_parts = py_file.relative_to(source_root).parts
    return any(
        part in _IGNORED_SOURCE_DIR_NAMES or (part.startswith(".") and part != ".")
        for part in rel_parts[:-1]
    )


def is_test_source_root(source_root: Path) -> bool:
    """Return whether a source root is itself a test directory by name."""
    return source_root.name in _IGNORED_SOURCE_DIR_NAMES


def is_nested_test_file(py_file: Path, source_root: Path) -> bool:
    """Return whether a file is test-shaped source nested inside a production root.

    ``should_skip_source_file`` already discards these from the production module
    scan, either by filename pattern or because they sit under a nested ``tests/``
    directory. This identifies that same set of files so they can be swept up as
    test consumers instead of silently disappearing. Files skipped for unrelated
    reasons (build output, caches, venvs, hidden directories) are excluded: they
    are not test code, and some of them (a ``.venv``, say) can contain unrelated
    third-party files that happen to match the test naming convention.
    """
    rel_parts = py_file.relative_to(source_root).parts
    dir_parts = rel_parts[:-1]
    other_ignored_dir_names = _IGNORED_SOURCE_DIR_NAMES - {"tests"}
    if any(
        part in other_ignored_dir_names or (part.startswith(".") and part != ".")
        for part in dir_parts
    ):
        return False
    return is_test_module_filename(py_file.name) or "tests" in dir_parts
