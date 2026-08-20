"""Validation for literal __all__ declarations."""

from __future__ import annotations

import ast

from privata._models import NAMESPACE_SEPARATOR, ExportIssue, Module
from privata._modules import names_from_target

_IGNORED_PUBLIC_BINDINGS = {"logger"}


def collect_export_issues(modules: dict[str, Module]) -> list[ExportIssue]:
    """Return mismatches between literal __all__ and public bindings."""
    issues: list[ExportIssue] = []

    for module in modules.values():
        if module.tree is None:
            continue

        all_names, lineno = _literal_all(module.tree)
        if all_names is None:
            continue

        all_bindings = _all_bindings(module.tree)
        public_bindings = _public_bindings(module.tree)

        issues.extend(
            ExportIssue(
                module=module.name,
                path=module.path,
                name=name,
                kind="unknown",
                lineno=lineno,
            )
            for name in sorted(all_names - all_bindings)
        )

        issues.extend(
            ExportIssue(
                module=module.name,
                path=module.path,
                name=name,
                kind="private",
                lineno=lineno,
            )
            for name in sorted(name for name in all_names & all_bindings if _is_private(name))
        )

        issues.extend(
            ExportIssue(
                module=module.name,
                path=module.path,
                name=name,
                kind="missing",
                lineno=lineno,
            )
            for name in sorted(public_bindings - all_names)
        )

    return sorted(issues, key=lambda item: (str(item.path), item.lineno, item.kind, item.name))


def _literal_all(tree: ast.Module) -> tuple[set[str] | None, int]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return _strings_from_node(node.value), node.lineno
    return None, 0


def _strings_from_node(node: ast.expr) -> set[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        names: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.add(elt.value)
            else:
                return None
        return names
    return None


def _all_bindings(tree: ast.Module) -> set[str]:
    bindings: set[str] = set()
    _collect_bound_names(tree.body, bindings, public_only=False, include_imports=True)
    return bindings


def _public_bindings(tree: ast.Module) -> set[str]:
    bindings: set[str] = set()
    _collect_bound_names(
        tree.body,
        bindings,
        public_only=True,
        include_imports=False,
    )
    return bindings


def _collect_bound_names(
    statements: list[ast.stmt],
    bindings: set[str],
    *,
    public_only: bool,
    include_imports: bool,
) -> None:
    for node in statements:
        if include_imports or not isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in _bound_names(node):
                _add_binding(bindings, name, public_only=public_only)
        for nested_statements in _nested_public_binding_statements(node):
            _collect_bound_names(
                nested_statements,
                bindings,
                public_only=public_only,
                include_imports=include_imports,
            )


def _bound_names(node: ast.stmt) -> list[str]:
    names: list[str] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names = [node.name]
    elif isinstance(node, ast.Assign):
        names = [
            name
            for target in node.targets
            for name in names_from_target(target)
            if name != "__all__"
        ]
    elif isinstance(node, ast.AnnAssign):
        names = names_from_target(node.target)
    elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
        names = names_from_target(node.name)
    elif isinstance(node, ast.Import):
        names = [alias.asname or alias.name.split(NAMESPACE_SEPARATOR)[0] for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = _import_from_bound_names(node)
    return names


def _import_from_bound_names(node: ast.ImportFrom) -> list[str]:
    if node.module == "__future__":
        return []
    return [alias.asname or alias.name for alias in node.names if alias.name != "*"]


def _nested_public_binding_statements(node: ast.stmt) -> list[list[ast.stmt]]:
    if not isinstance(node, ast.Try):
        return []
    return [
        node.body,
        node.orelse,
        node.finalbody,
        *(handler.body for handler in node.handlers),
    ]


def _add_binding(bindings: set[str], name: str, *, public_only: bool) -> None:
    if public_only and name in _IGNORED_PUBLIC_BINDINGS:
        return
    if public_only and _is_private(name):
        return
    bindings.add(name)


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
