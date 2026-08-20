"""Shared data models for privacy checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ast
    from pathlib import Path

NAMESPACE_SEPARATOR = "."


@dataclass
class Symbol:
    """A public top-level symbol found in a module."""

    name: str
    kind: str
    lineno: int
    module: str
    path: Path


@dataclass
class Method:
    """A public method that no other production module references.

    ``class_lineno`` and ``class_public_methods`` describe the owning class, so
    a caller can group findings and see how much of the class they cover. Ten
    findings out of ten public methods is one question about the class; ten out
    of a hundred is ten separate helpers that leaked.
    """

    name: str
    class_name: str
    lineno: int
    module: str
    path: Path
    class_lineno: int
    class_public_methods: int


@dataclass
class Module:
    """A parsed Python module with its top-level symbols."""

    name: str
    path: Path
    package_parts: tuple[str, ...]
    symbols: list[Symbol] = field(default_factory=list)
    private_symbols: list[Symbol] = field(default_factory=list)
    tree: ast.Module | None = None
    ignored_lines: frozenset[int] = frozenset()
    exports: frozenset[str] = frozenset()


@dataclass
class ModuleCollision:
    """A module name that resolves to more than one file across source roots."""

    module: str
    paths: list[Path]


@dataclass
class UnparsableModule:
    """A source file that could not be parsed, so its references were not seen."""

    module: str
    path: Path
    lineno: int
    message: str


@dataclass
class PrivateModuleImport:
    """A private module imported from outside its containing package subtree."""

    module: str
    path: Path
    imported_by: str
    imported_by_path: Path
    lineno: int


@dataclass
class PrivateSymbolImport:
    """A private top-level symbol imported from another production module."""

    module: str
    name: str
    path: Path
    imported_by: str
    imported_by_path: Path
    lineno: int


@dataclass
class ExportIssue:
    """A mismatch between literal __all__ and public module bindings."""

    module: str
    path: Path
    name: str
    kind: str
    lineno: int


@dataclass(frozen=True)
class SymbolCandidate:
    """A candidate top-level symbol before filtering."""

    name: str
    kind: str
    lineno: int
