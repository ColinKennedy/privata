"""Shared AST name helpers for method analysis."""

from __future__ import annotations

import ast

from privata._models import NAMESPACE_SEPARATOR


def dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}{NAMESPACE_SEPARATOR}{node.attr}"
    return None


def referenced_names(tree: ast.Module) -> set[str]:
    """Return every attribute name and string literal a module mentions.

    String literals count because a name reached through ``getattr(obj, "run")``
    is as real a use as ``obj.run``.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names
