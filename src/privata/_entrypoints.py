"""External public-interface discovery."""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

if TYPE_CHECKING:
    from pathlib import Path

_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\\.]*:[A-Za-z_][A-Za-z0-9_]*$")
_UVICORN_RE = re.compile(r"\buvicorn\s+([A-Za-z_][A-Za-z0-9_\.]*):([A-Za-z_][A-Za-z0-9_]*)\b")


def collect_external_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    """Return symbols made public by external entrypoint declarations."""
    pairs = _load_pyproject_entrypoints(project_root)
    pairs.update(_load_shell_uvicorn_entrypoints(project_root))
    return pairs


def _load_pyproject_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    """Return pyproject console and GUI script entrypoint targets."""
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return set()

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project_table = data.get("project", {})

    pairs: set[tuple[str, str]] = set()
    for table_key in ("scripts", "gui-scripts"):
        table = project_table.get(table_key, {})
        if not isinstance(table, dict):
            continue
        for raw in table.values():
            if not isinstance(raw, str):
                continue
            if not _ENTRYPOINT_RE.fullmatch(raw):
                continue
            module_name, symbol_name = raw.split(":", 1)
            pairs.add((module_name, symbol_name))
    return pairs


def _entrypoint_shell_files(project_root: Path) -> list[Path]:
    """Return shell-like files that may launch Python entrypoints."""
    files: list[Path] = []
    files.extend(project_root.glob("*.sh"))
    files.extend(project_root.glob("Dockerfile*"))
    scripts_dir = project_root / "scripts"
    if scripts_dir.exists():
        files.extend(scripts_dir.rglob("*.sh"))
    return sorted(set(files))


def _load_shell_uvicorn_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    """Return Uvicorn app targets referenced by shell files."""
    pairs: set[tuple[str, str]] = set()
    for path in _entrypoint_shell_files(project_root):
        text = path.read_text(encoding="utf-8")
        for module_name, symbol_name in _UVICORN_RE.findall(text):
            pairs.add((module_name, symbol_name))
    return pairs


def load_tach_interface_exports(project_root: Path) -> set[tuple[str, str]]:
    """Return symbols exposed by Tach interfaces."""
    tach_path = project_root / "tach.toml"
    if not tach_path.exists():
        return set()

    data = tomllib.loads(tach_path.read_text(encoding="utf-8"))
    pairs: set[tuple[str, str]] = set()
    for interface in data.get("interfaces", []):
        source_modules = interface.get("from", [])
        exposed_names = interface.get("expose", [])
        if not isinstance(source_modules, list) or not isinstance(exposed_names, list):
            continue
        for module_name in source_modules:
            if not isinstance(module_name, str):
                continue
            for symbol_name in exposed_names:
                if isinstance(symbol_name, str):
                    pairs.add((module_name, symbol_name))
    return pairs
