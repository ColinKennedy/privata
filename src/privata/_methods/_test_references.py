"""Reference attribution for co-located test helpers."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._imports import resolve_import_source
from privata._methods._ast import referenced_names
from privata._models import NAMESPACE_SEPARATOR

if TYPE_CHECKING:
    from collections.abc import Mapping

    from privata._models import Module


def referenced_names_by_module(
    module: Module,
    known_modules: Mapping[str, Module],
) -> dict[str, set[str]]:
    """Return the names a test file mentions, for each helper module it imports.

    Attribution is per file, not per receiver: every name the file mentions is
    credited to every helper module it imports. A test that imports a helper is
    taken to exercise it, so crediting the whole file can only keep a method
    public that a finer reading would have flagged. That is the safe direction
    for a checker whose findings are acted on by renaming a method.
    """
    if module.tree is None:
        return {}

    imported = _imported_known_modules(module.tree, module.package_parts, known_modules)
    if not imported:
        return {}

    names = referenced_names(module.tree)
    return {source: set(names) for source in imported}


def _imported_known_modules(
    tree: ast.Module,
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
) -> set[str]:
    """Return the known modules a file imports, at any nesting depth."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names if alias.name in known_modules)
        elif isinstance(node, ast.ImportFrom):
            source = resolve_import_source(package_parts, node.level, node.module)
            if source is None:
                continue
            if source in known_modules:
                imported.add(source)
            imported.update(
                f"{source}{NAMESPACE_SEPARATOR}{alias.name}"
                for alias in node.names
                if f"{source}{NAMESPACE_SEPARATOR}{alias.name}" in known_modules
            )
    return imported
