"""Re-export discovery for public classes, in packages and facade modules."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._imports import resolve_import_source
from privata._methods._ast import dotted_name as _dotted_name
from privata._models import NAMESPACE_SEPARATOR

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from privata._models import Module


def collect_reexports(modules: Mapping[str, Module]) -> set[tuple[str, str]]:
    """Return classes exposed by runtime imports in package and facade modules.

    A package ``__init__.py`` re-exports whatever it imports. Any other module
    re-exports only what its ``__all__`` names, which is the explicit form of
    the same intent: a facade such as ``pkg/api.py`` that imports ``Service``
    and lists it in ``__all__`` publishes that class just as an ``__init__.py``
    would.
    """
    reexports: set[tuple[str, str]] = set()
    for module in modules.values():
        if module.tree is None:
            continue
        is_package_init = module.path.name == "__init__.py"
        if not is_package_init and not module.exports:
            continue
        for node in _runtime_module_imports(module.tree.body):
            source = resolve_import_source(module.package_parts, node.level, node.module)
            if source is None or source not in modules:
                continue
            if any(alias.name == "*" for alias in node.names):
                reexports.update(
                    (source, symbol.name)
                    for symbol in modules[source].symbols
                    if _is_exposed(symbol.name, module, is_package_init=is_package_init)
                )
            defined = {symbol.name for symbol in modules[source].symbols}
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "*" or not _is_exposed(
                    local,
                    module,
                    is_package_init=is_package_init,
                ):
                    continue
                submodule = f"{source}{NAMESPACE_SEPARATOR}{alias.name}"
                if submodule not in modules and alias.name in defined:
                    reexports.add((source, alias.name))
    return reexports


def _is_exposed(local: str, module: Module, *, is_package_init: bool) -> bool:
    """Return whether a module publishes an imported name to its consumers."""
    if not is_package_init:
        return local in module.exports
    return not local.startswith("_") or local in module.exports


def _runtime_module_imports(  # noqa: C901
    statements: list[ast.stmt],
) -> Iterator[ast.ImportFrom]:
    """Yield imports that can create module-level runtime bindings."""
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            yield node
        elif isinstance(node, ast.If):
            guard = _type_checking_guard(node.test)
            if guard is True:
                yield from _runtime_module_imports(node.orelse)
            elif guard is False:
                yield from _runtime_module_imports(node.body)
            else:
                yield from _runtime_module_imports(node.body)
                yield from _runtime_module_imports(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _runtime_module_imports(node.body)
            yield from _runtime_module_imports(node.orelse)
            yield from _runtime_module_imports(node.finalbody)
            for handler in node.handlers:
                yield from _runtime_module_imports(handler.body)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _runtime_module_imports(node.body)
            yield from _runtime_module_imports(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _runtime_module_imports(node.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _runtime_module_imports(case.body)


def _type_checking_guard(node: ast.expr) -> bool | None:
    """Return whether a conventional guard disables its body at runtime."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        guarded = _type_checking_guard(node.operand)
        return None if guarded is None else not guarded
    if _dotted_name(node) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
        return True
    return None
