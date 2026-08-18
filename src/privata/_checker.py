"""Detect module privacy issues within Python source roots."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, NamedTuple

from privata._entrypoints import collect_external_entrypoints, load_tach_interface_exports
from privata._exports import collect_export_issues
from privata._imports import (
    collect_private_module_imports,
    collect_private_symbol_imports,
    find_cross_imports,
)
from privata._methods import (
    collect_method_candidates,
    collect_reexports,
    referenced_names_by_module,
)
from privata._modules import (
    collect_module_collisions,
    collect_modules_with_errors,
    collect_test_consumers,
)
from privata._source_roots import is_test_source_root, source_roots

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from privata._models import (
        ExportIssue,
        Method,
        Module,
        ModuleCollision,
        PrivateModuleImport,
        PrivateSymbolImport,
        Symbol,
        UnparsableModule,
    )


_METHOD_LIST_INDENT = " " * 6
# Keeps the indented method list inside the project's 100-column limit.
_METHOD_LIST_WIDTH = 100 - len(_METHOD_LIST_INDENT)


class _PrivacyFindings(NamedTuple):
    """All findings produced by one scan of a project."""

    unparsable_modules: list[UnparsableModule]
    candidates: list[Symbol]
    method_candidates: list[Method]
    private_module_imports: list[PrivateModuleImport]
    private_symbol_imports: list[PrivateSymbolImport]
    export_issues: list[ExportIssue]
    module_collisions: list[ModuleCollision]


def _test_helper_cross_imports(
    test_roots: list[Path],
    modules: dict[str, Module],
    test_consumers: dict[str, Module],
) -> set[tuple[str, str]]:
    """Return helper-module symbols in test source roots that co-located test files use.

    Each pass is scoped to a single test root so that test files can only certify
    helper modules in their own root, never production symbols.
    """
    used: set[tuple[str, str]] = set()
    for root in test_roots:
        helpers = {
            name: module for name, module in modules.items() if module.path.is_relative_to(root)
        }
        consumers = {
            name: module
            for name, module in test_consumers.items()
            if module.path.is_relative_to(root)
        }
        used |= find_cross_imports(helpers, consumers)
    return used


def _test_helper_method_references(
    test_roots: list[Path],
    modules: dict[str, Module],
    test_consumers: dict[str, Module],
) -> dict[str, set[str]]:
    """Return names that co-located test files mention, per helper module.

    Helper modules in a test source root exist to serve their own test files, so
    a method those tests call is treated as used.
    """
    references: dict[str, set[str]] = {}
    for root in test_roots:
        helpers = {
            name: module for name, module in modules.items() if module.path.is_relative_to(root)
        }
        consumers = {
            name: module
            for name, module in test_consumers.items()
            if module.path.is_relative_to(root)
        }
        for consumer in consumers.values():
            for module_name, names in referenced_names_by_module(consumer, helpers).items():
                references.setdefault(module_name, set()).update(names)
    return references


def _split_test_source_roots(
    project_root: Path,
    roots: list[Path],
) -> tuple[list[Path], list[Path]]:
    """Split test roots into local helper roots and external consumer roots."""
    local: list[Path] = []
    external: list[Path] = []
    for root in roots:
        if not is_test_source_root(root):
            continue
        if root.is_relative_to(project_root):
            local.append(root)
        else:
            external.append(root)
    return local, external


def _merge_method_references(*reference_maps: dict[str, set[str]]) -> dict[str, set[str]]:
    """Union method references keyed by module name."""
    merged: dict[str, set[str]] = {}
    for reference_map in reference_maps:
        for module_name, names in reference_map.items():
            merged.setdefault(module_name, set()).update(names)
    return merged


def _external_test_method_references(
    modules: dict[str, Module],
    test_consumers: dict[str, Module],
) -> dict[str, set[str]]:
    """Return names external test files mention for imported production modules."""
    references: dict[str, set[str]] = {}
    for consumer in test_consumers.values():
        for module_name, names in referenced_names_by_module(consumer, modules).items():
            references.setdefault(module_name, set()).update(names)
    return references


def _collect_privacy_findings(
    project_root: Path,
    *,
    include_methods: bool,
) -> _PrivacyFindings:
    """Collect public-symbol and private-module boundary findings.

    ``include_methods`` is required rather than defaulted: the method scan is
    the most expensive part of a run, and every caller knows whether it wants
    the answer.
    """
    project_root = project_root.resolve()
    roots = source_roots(project_root)
    modules, unparsable_modules = collect_modules_with_errors(roots)
    local_test_roots, external_test_roots = _split_test_source_roots(project_root, roots)
    local_test_consumers = collect_test_consumers(local_test_roots)
    external_test_consumers = collect_test_consumers(external_test_roots)
    cross_imports = (
        find_cross_imports(modules)
        | find_cross_imports(
            modules,
            external_test_consumers,
        )
        | _test_helper_cross_imports(
            local_test_roots,
            modules,
            local_test_consumers,
        )
    )
    external_entrypoints = collect_external_entrypoints(project_root)
    public_interface_exports = load_tach_interface_exports(project_root)
    package_reexports = collect_reexports(modules)

    candidates = [
        sym
        for mod in modules.values()
        for sym in mod.symbols
        if (sym.module, sym.name) not in cross_imports
        and (sym.module, sym.name) not in external_entrypoints
        and (sym.module, sym.name) not in public_interface_exports
    ]
    candidates.sort(key=lambda s: (str(s.path), s.lineno))
    return _PrivacyFindings(
        unparsable_modules=unparsable_modules,
        candidates=candidates,
        method_candidates=(
            collect_method_candidates(
                modules,
                public_interface=(
                    external_entrypoints | public_interface_exports | package_reexports
                ),
                test_references=_merge_method_references(
                    _test_helper_method_references(
                        local_test_roots,
                        modules,
                        local_test_consumers,
                    ),
                    _external_test_method_references(
                        modules,
                        external_test_consumers,
                    ),
                ),
            )
            if include_methods
            else []
        ),
        private_module_imports=collect_private_module_imports(modules),
        private_symbol_imports=collect_private_symbol_imports(modules),
        export_issues=collect_export_issues(modules),
        module_collisions=collect_module_collisions(roots),
    )


def find_unparsable_modules(project_root: Path) -> list[UnparsableModule]:
    """Find production source files that could not be parsed."""
    return _collect_privacy_findings(project_root, include_methods=False).unparsable_modules


def find_private_candidates(project_root: Path) -> list[Symbol]:
    """Find symbols that appear module-local and should be private."""
    return _collect_privacy_findings(project_root, include_methods=False).candidates


def find_method_candidates(project_root: Path) -> list[Method]:
    """Find public methods that only their own module refers to."""
    return _collect_privacy_findings(project_root, include_methods=True).method_candidates


def find_private_module_imports(project_root: Path) -> list[PrivateModuleImport]:
    """Find private modules imported from outside their package subtree."""
    return _collect_privacy_findings(project_root, include_methods=False).private_module_imports


def find_private_symbol_imports(project_root: Path) -> list[PrivateSymbolImport]:
    """Find private top-level symbols imported from another production module."""
    return _collect_privacy_findings(project_root, include_methods=False).private_symbol_imports


def find_export_issues(project_root: Path) -> list[ExportIssue]:
    """Find literal __all__ declarations that are stale or incomplete."""
    return _collect_privacy_findings(project_root, include_methods=False).export_issues


def find_module_collisions(project_root: Path) -> list[ModuleCollision]:
    """Find module names that resolve to more than one file across source roots."""
    return _collect_privacy_findings(project_root, include_methods=False).module_collisions


def check_project(project_root: Path, *, include_methods: bool = False) -> int:
    """Scan project and report module-local public symbols.

    The method check is off by default. Attribute access is dynamic in Python,
    so it cannot see every caller, and on a large codebase it reports far more
    than the other checks. Opt in with ``include_methods`` once the noise is
    worth it for a given project.
    """
    project_root = project_root.resolve()
    findings = _collect_privacy_findings(project_root, include_methods=include_methods)

    sections: list[tuple[bool, Callable[[], None]]] = [
        (
            bool(findings.unparsable_modules),
            lambda: _print_unparsable_modules(findings.unparsable_modules, project_root),
        ),
        (
            bool(findings.module_collisions),
            lambda: _print_module_collisions(findings.module_collisions, project_root),
        ),
        (
            bool(findings.candidates),
            lambda: _print_private_candidates(findings.candidates, project_root),
        ),
        (
            bool(findings.method_candidates),
            lambda: _print_method_candidates(findings.method_candidates, project_root),
        ),
        (
            bool(findings.private_module_imports),
            lambda: _print_private_module_imports(findings.private_module_imports, project_root),
        ),
        (
            bool(findings.private_symbol_imports),
            lambda: _print_private_symbol_imports(findings.private_symbol_imports, project_root),
        ),
        (
            bool(findings.export_issues),
            lambda: _print_export_issues(findings.export_issues, project_root),
        ),
    ]
    printers = [printer for has_findings, printer in sections if has_findings]
    if not printers:
        print("No module privacy issues found.")
        return 0

    for index, printer in enumerate(printers):
        if index:
            print()
        printer()

    return 1


def _print_unparsable_modules(
    unparsable: list[UnparsableModule],
    project_root: Path,
) -> None:
    print(
        f"Found {_count(len(unparsable), 'source file')} that could not be parsed; "
        "skipped files stop contributing references, so every finding below "
        "may be wrong:\n",
    )
    for module in unparsable:
        rel = module.path.relative_to(project_root).as_posix()
        print(f"  {rel}:{module.lineno}: {module.message}")


def _print_module_collisions(collisions: list[ModuleCollision], project_root: Path) -> None:
    print(
        f"Found {_count(len(collisions), 'module name')} defined by multiple files; "
        "only one file per name is scanned, so findings for these modules "
        "may be incomplete:\n",
    )
    for collision in collisions:
        rels = ", ".join(path.relative_to(project_root).as_posix() for path in collision.paths)
        print(f"  module `{collision.module}` is defined by: {rels}")


def _print_private_candidates(candidates: list[Symbol], project_root: Path) -> None:
    print(f"Found {_count(len(candidates), 'public symbol')} that could be made private:\n")
    for symbol in candidates:
        rel = symbol.path.relative_to(project_root).as_posix()
        print(f"  {rel}:{symbol.lineno}: {symbol.kind} `{symbol.name}`")


def _print_method_candidates(methods: list[Method], project_root: Path) -> None:
    """Print method findings grouped by the class that owns them.

    A flat list reads as one decision per method, which is misleading. Ten
    findings in a ten-method class is a single question about the class; the
    ``n of m`` count is what tells those apart, so it leads each group.
    """
    groups: dict[tuple[Path, int, str], list[Method]] = {}
    for method in methods:
        key = (method.path, method.class_lineno, method.class_name)
        groups.setdefault(key, []).append(method)

    print(
        f"Found {_count(len(methods), 'public method')} "
        f"in {_count(len(groups), 'class', 'classes')} that could be made private:\n",
    )
    for (path, class_lineno, class_name), found in groups.items():
        rel = path.relative_to(project_root).as_posix()
        total = found[0].class_public_methods
        print(
            f"  {rel}:{class_lineno}: class `{class_name}` "
            f"({len(found)} of {total} public methods)",
        )
        names = ", ".join(f"{method.name}:{method.lineno}" for method in found)
        for line in textwrap.wrap(names, width=_METHOD_LIST_WIDTH, break_long_words=False):
            print(f"{_METHOD_LIST_INDENT}{line}")


def _count(number: int, singular: str, plural: str | None = None) -> str:
    """Return ``number`` with a correctly pluralised noun."""
    if number == 1:
        return f"1 {singular}"
    return f"{number} {plural if plural is not None else singular + 's'}"


def _print_private_module_imports(
    private_module_imports: list[PrivateModuleImport],
    project_root: Path,
) -> None:
    print(
        "Found "
        f"{_count(len(private_module_imports), 'private module import')} "
        "outside the owning package subtree:\n",
    )
    for private_import in private_module_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(f"  {rel}:{private_import.lineno}: imports private module `{private_import.module}`")


def _print_private_symbol_imports(
    private_symbol_imports: list[PrivateSymbolImport],
    project_root: Path,
) -> None:
    print(
        f"Found {_count(len(private_symbol_imports), 'private symbol import')} "
        "from production modules:\n",
    )
    for private_import in private_symbol_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(
            f"  {rel}:{private_import.lineno}: imports private symbol "
            f"`{private_import.module}.{private_import.name}`",
        )


def _print_export_issues(export_issues: list[ExportIssue], project_root: Path) -> None:
    print(f"Found {_count(len(export_issues), '__all__ export issue')}:\n")
    for export_issue in export_issues:
        rel = export_issue.path.relative_to(project_root).as_posix()
        if export_issue.kind == "unknown":
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"__all__ exports unknown name `{export_issue.name}`",
            )
        elif export_issue.kind == "private":
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"__all__ exports private name `{export_issue.name}`",
            )
        else:
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"public name `{export_issue.name}` missing from __all__",
            )
