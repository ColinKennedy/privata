"""Detect module privacy issues within Python source roots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from privata._entrypoints import collect_external_entrypoints, load_tach_interface_exports
from privata._exports import collect_export_issues
from privata._imports import (
    collect_private_module_imports,
    collect_private_symbol_imports,
    find_cross_imports,
)
from privata._modules import collect_modules, collect_test_consumers
from privata._source_roots import source_roots

if TYPE_CHECKING:
    from pathlib import Path

    from privata._models import ExportIssue, PrivateModuleImport, PrivateSymbolImport, Symbol


def _collect_privacy_findings(
    project_root: Path,
) -> tuple[list[Symbol], list[PrivateModuleImport], list[PrivateSymbolImport], list[ExportIssue]]:
    """Collect public-symbol and private-module boundary findings."""
    roots = source_roots(project_root)
    modules = collect_modules(roots)
    test_consumers = collect_test_consumers(roots)
    cross_imports = find_cross_imports(modules, test_consumers)
    external_entrypoints = collect_external_entrypoints(project_root)
    public_interface_exports = load_tach_interface_exports(project_root)

    candidates = [
        sym
        for mod in modules.values()
        for sym in mod.symbols
        if (sym.module, sym.name) not in cross_imports
        and (sym.module, sym.name) not in external_entrypoints
        and (sym.module, sym.name) not in public_interface_exports
    ]
    candidates.sort(key=lambda s: (str(s.path), s.lineno))
    private_module_imports = collect_private_module_imports(modules)
    private_symbol_imports = collect_private_symbol_imports(modules)
    export_issues = collect_export_issues(modules)
    return candidates, private_module_imports, private_symbol_imports, export_issues


def find_private_candidates(project_root: Path) -> list[Symbol]:
    """Find symbols that appear module-local and should be private."""
    candidates, _, _, _ = _collect_privacy_findings(project_root)
    return candidates


def find_private_module_imports(project_root: Path) -> list[PrivateModuleImport]:
    """Find private modules imported from outside their package subtree."""
    _, private_module_imports, _, _ = _collect_privacy_findings(project_root)
    return private_module_imports


def find_private_symbol_imports(project_root: Path) -> list[PrivateSymbolImport]:
    """Find private top-level symbols imported from another production module."""
    _, _, private_symbol_imports, _ = _collect_privacy_findings(project_root)
    return private_symbol_imports


def find_export_issues(project_root: Path) -> list[ExportIssue]:
    """Find literal __all__ declarations that are stale or incomplete."""
    _, _, _, export_issues = _collect_privacy_findings(project_root)
    return export_issues


def check_project(project_root: Path) -> int:
    """Scan project and report module-local public symbols."""
    project_root = project_root.resolve()
    candidates, private_module_imports, private_symbol_imports, export_issues = (
        _collect_privacy_findings(project_root)
    )

    if (
        not candidates
        and not private_module_imports
        and not private_symbol_imports
        and not export_issues
    ):
        print("No module privacy issues found.")
        return 0

    if candidates:
        _print_private_candidates(candidates, project_root)

    if private_module_imports:
        if candidates:
            print()
        _print_private_module_imports(private_module_imports, project_root)

    if private_symbol_imports:
        if candidates or private_module_imports:
            print()
        _print_private_symbol_imports(private_symbol_imports, project_root)

    if export_issues:
        if candidates or private_module_imports or private_symbol_imports:
            print()
        _print_export_issues(export_issues, project_root)

    return 1


def _print_private_candidates(candidates: list[Symbol], project_root: Path) -> None:
    print(f"Found {len(candidates)} public symbols that could be made private:\n")
    for symbol in candidates:
        rel = symbol.path.relative_to(project_root).as_posix()
        print(f"  {rel}:{symbol.lineno}: {symbol.kind} `{symbol.name}`")


def _print_private_module_imports(
    private_module_imports: list[PrivateModuleImport],
    project_root: Path,
) -> None:
    print(
        "Found "
        f"{len(private_module_imports)} "
        "private module imports outside their package subtree:\n",
    )
    for private_import in private_module_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(f"  {rel}:{private_import.lineno}: imports private module `{private_import.module}`")


def _print_private_symbol_imports(
    private_symbol_imports: list[PrivateSymbolImport],
    project_root: Path,
) -> None:
    print(
        f"Found {len(private_symbol_imports)} private symbol imports from production modules:\n",
    )
    for private_import in private_symbol_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(
            f"  {rel}:{private_import.lineno}: imports private symbol "
            f"`{private_import.module}.{private_import.name}`",
        )


def _print_export_issues(export_issues: list[ExportIssue], project_root: Path) -> None:
    print(f"Found {len(export_issues)} __all__ export issues:\n")
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
