"""Import analysis for public symbols and private modules."""

from __future__ import annotations

import ast

from privata._models import Module, PrivateModuleImport, PrivateSymbolImport

_SPLIT_MODULE_PART_COUNT = 2


def _resolve_alias_prefix(base: str, import_aliases: dict[str, str]) -> str | None:
    """Resolve a dotted base to a module by matching its longest alias prefix."""
    parts = base.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        aliased = import_aliases.get(prefix)
        if aliased is not None:
            suffix = ".".join(parts[i:])
            return f"{aliased}.{suffix}" if suffix else aliased
    return None


def _dotted_name(node: ast.expr) -> str | None:
    """Resolve a chained attribute expression to a dotted string, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _is_private_module_name(module_name: str) -> bool:
    """Return whether any segment of a dotted module path is private."""
    return any(part.startswith("_") for part in module_name.split("."))


def _private_module_owner_package(module_name: str) -> str:
    """Return the package that owns a private module."""
    parts = module_name.rsplit(".", 1)
    return parts[0] if len(parts) == _SPLIT_MODULE_PART_COUNT else module_name


def _module_is_within_package(module_name: str, package_name: str) -> bool:
    """Return whether a module is inside a package subtree."""
    return module_name == package_name or module_name.startswith(f"{package_name}.")


def _resolve_relative_import(
    importer_package: tuple[str, ...],
    level: int,
    module_attr: str | None,
) -> str | None:
    """Resolve a relative import to an absolute dotted module name."""
    if level == 0:
        return module_attr

    up = level - 1
    if up > len(importer_package):
        return None
    base = list(importer_package[: len(importer_package) - up])
    if module_attr:
        base.extend(module_attr.split("."))
    return ".".join(base) if base else None


def find_cross_imports(modules: dict[str, Module]) -> set[tuple[str, str]]:  # noqa: C901, PLR0912
    """Return pairs that are imported by another production source module."""
    known = set(modules)
    used: set[tuple[str, str]] = set()
    defined = {
        mod_name: {symbol.name for symbol in mod.symbols} for mod_name, mod in modules.items()
    }

    for consumer_name, consumer in modules.items():
        if consumer.tree is None:
            continue

        import_aliases: dict[str, str] = {}

        for node in ast.walk(consumer.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    import_aliases[local] = alias.name

            elif isinstance(node, ast.ImportFrom):
                source = _resolve_relative_import(
                    consumer.package_parts,
                    node.level or 0,
                    node.module,
                )
                if source is None:
                    continue

                for alias in node.names:
                    sym = alias.name
                    if sym == "*":
                        if source in defined and source != consumer_name:
                            for public_symbol in defined[source]:
                                used.add((source, public_symbol))
                        continue

                    submodule = f"{source}.{sym}"
                    if submodule in known:
                        local = alias.asname or sym
                        import_aliases[local] = submodule
                        continue

                    if source != consumer_name and source in defined and sym in defined[source]:
                        used.add((source, sym))

        for node in ast.walk(consumer.tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            base = _dotted_name(node.value)
            if base is None:
                continue
            if base in defined and base != consumer_name and attr in defined[base]:
                used.add((base, attr))
            else:
                resolved = _resolve_alias_prefix(base, import_aliases)
                if (
                    resolved
                    and resolved != consumer_name
                    and resolved in defined
                    and attr in defined[resolved]
                ):
                    used.add((resolved, attr))

    return used


def collect_private_module_imports(modules: dict[str, Module]) -> list[PrivateModuleImport]:
    """Return private modules imported from outside their package subtree."""
    private_modules = {
        module_name for module_name in modules if _is_private_module_name(module_name)
    }
    findings: dict[tuple[str, str, int], PrivateModuleImport] = {}

    def record(private_module_name: str, consumer: Module, lineno: int) -> None:
        if private_module_name == consumer.name:
            return
        owner_package = _private_module_owner_package(private_module_name)
        if _module_is_within_package(consumer.name, owner_package):
            return
        findings.setdefault(
            (private_module_name, consumer.name, lineno),
            PrivateModuleImport(
                module=private_module_name,
                path=modules[private_module_name].path,
                imported_by=consumer.name,
                imported_by_path=consumer.path,
                lineno=lineno,
            ),
        )

    for consumer in modules.values():
        if consumer.tree is None:
            continue

        for private_module_name, lineno in _find_private_imports_in_module(
            consumer,
            consumer.tree,
            private_modules,
        ):
            record(private_module_name, consumer, lineno)

    return sorted(
        findings.values(),
        key=lambda item: (str(item.imported_by_path), item.lineno, item.module),
    )


def collect_private_symbol_imports(modules: dict[str, Module]) -> list[PrivateSymbolImport]:
    """Return private top-level symbols imported from another production module."""
    private_symbols = {
        module_name: {symbol.name: symbol for symbol in module.private_symbols}
        for module_name, module in modules.items()
        if module.private_symbols
    }
    findings: dict[tuple[str, str, str, int], PrivateSymbolImport] = {}

    def record(source: str, name: str, consumer: Module, lineno: int) -> None:
        if source == consumer.name:
            return
        symbol = private_symbols[source][name]
        findings.setdefault(
            (source, name, consumer.name, lineno),
            PrivateSymbolImport(
                module=source,
                name=name,
                path=symbol.path,
                imported_by=consumer.name,
                imported_by_path=consumer.path,
                lineno=lineno,
            ),
        )

    for consumer in modules.values():
        if consumer.tree is None:
            continue

        for node in ast.walk(consumer.tree):
            if not isinstance(node, ast.ImportFrom):
                continue

            source = _resolve_relative_import(
                consumer.package_parts,
                node.level or 0,
                node.module,
            )
            if source is None or source not in private_symbols:
                continue

            for alias in node.names:
                name = alias.name
                if name != "*" and name in private_symbols[source]:
                    record(source, name, consumer, node.lineno)

    return sorted(
        findings.values(),
        key=lambda item: (str(item.imported_by_path), item.lineno, item.module, item.name),
    )


def _find_private_imports_in_module(
    consumer: Module,
    tree: ast.Module,
    private_modules: set[str],
) -> set[tuple[str, int]]:
    findings: set[tuple[str, int]] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            findings.update(_private_imports_from_import(node, private_modules))
            continue
        if isinstance(node, ast.ImportFrom):
            findings.update(_private_imports_from_import_from(consumer, node, private_modules))

    return findings


def _private_imports_from_import(
    node: ast.Import,
    private_modules: set[str],
) -> set[tuple[str, int]]:
    return {(alias.name, node.lineno) for alias in node.names if alias.name in private_modules}


def _private_imports_from_import_from(
    consumer: Module,
    node: ast.ImportFrom,
    private_modules: set[str],
) -> set[tuple[str, int]]:
    source = _resolve_relative_import(
        consumer.package_parts,
        node.level or 0,
        node.module,
    )
    if source is None:
        return set()

    findings: set[tuple[str, int]] = set()
    if source in private_modules:
        findings.add((source, node.lineno))

    findings.update(
        (f"{source}.{alias.name}", node.lineno)
        for alias in node.names
        if alias.name != "*" and f"{source}.{alias.name}" in private_modules
    )
    return findings
