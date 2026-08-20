import os
from typing import TypeAlias

_PathLike: TypeAlias = str | os.PathLike[str]

__version__: str

class Symbol:
    name: str
    kind: str
    lineno: int
    module: str
    path: str

class Method:
    name: str
    class_name: str
    lineno: int
    module: str
    path: str
    class_lineno: int
    class_public_methods: int

class ModuleCollision:
    module: str
    paths: list[str]

class UnparsableModule:
    module: str
    path: str
    lineno: int
    message: str

class PrivateModuleImport:
    module: str
    path: str
    imported_by: str
    imported_by_path: str
    lineno: int

class PrivateSymbolImport:
    module: str
    name: str
    path: str
    imported_by: str
    imported_by_path: str
    lineno: int

class ExportIssue:
    module: str
    path: str
    name: str
    kind: str
    lineno: int

def find_unparsable_modules(project_root: _PathLike) -> list[UnparsableModule]: ...
def find_private_candidates(project_root: _PathLike) -> list[Symbol]: ...
def find_method_candidates(project_root: _PathLike) -> list[Method]: ...
def find_private_module_imports(project_root: _PathLike) -> list[PrivateModuleImport]: ...
def find_private_symbol_imports(project_root: _PathLike) -> list[PrivateSymbolImport]: ...
def find_export_issues(project_root: _PathLike) -> list[ExportIssue]: ...
def find_module_collisions(project_root: _PathLike) -> list[ModuleCollision]: ...
def check_project(project_root: _PathLike, methods: bool = False) -> tuple[str, int]: ...
