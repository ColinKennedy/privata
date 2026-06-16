"""Python module collection and public-symbol extraction."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._models import Module, Symbol, SymbolCandidate
from privata._source_roots import is_test_module_filename, is_test_source_root, should_skip_source_file, should_skip_test_consumer

if TYPE_CHECKING:
    from pathlib import Path

_ROUTE_DECORATORS = {
    "api_route",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
    "websocket",
    "websocket_route",
}
_CLI_DECORATORS = {"callback", "command"}
_FRAMEWORK_CONSTRUCTORS = {"APIRouter", "FastAPI", "Typer"}
_FRAMEWORK_REGISTRATION_CALLS = {"add_api_route", "add_api_websocket_route", "include_router"}
_ALLOWED_PUBLIC_NAMES = {"logger"}


def _module_name_from_path(py_file: Path, source_root: Path) -> str | None:
    """Derive dotted module name from file path relative to a source root."""
    rel = py_file.relative_to(source_root)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _package_parts(module_name: str, *, is_package_init: bool = False) -> tuple[str, ...]:
    """Return the package path used to resolve relative imports."""
    if is_package_init:
        return tuple(module_name.split("."))
    parts = module_name.rsplit(".", 1)
    if len(parts) == 1:
        return ()
    return tuple(parts[0].split("."))


def collect_modules(source_roots: list[Path]) -> dict[str, Module]:  # noqa: C901, PLR0912
    """Parse every production .py under source roots and collect top-level public definitions."""
    modules: dict[str, Module] = {}

    for source_root in source_roots:
        for py_file in sorted(source_root.rglob("*.py")):
            if should_skip_source_file(py_file, source_root):
                continue
            mod_name = _module_name_from_path(py_file, source_root)
            if mod_name is None:
                continue

            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            explicit_exports = _extract_all(tree)
            framework_related_names = _collect_framework_related_names(tree)
            pydantic_model_names: set[str] = set()

            mod = Module(
                name=mod_name,
                path=py_file,
                package_parts=_package_parts(
                    mod_name,
                    is_package_init=py_file.name == "__init__.py",
                ),
                tree=tree,
            )

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_framework_callback(node):
                        continue
                    _maybe_add(
                        mod,
                        SymbolCandidate(node.name, "function", node.lineno),
                        explicit_exports,
                        ignored_names=framework_related_names,
                    )
                elif isinstance(node, ast.ClassDef):
                    if _is_pydantic_model(node, pydantic_model_names):
                        pydantic_model_names.add(node.name)
                        continue
                    _maybe_add(
                        mod,
                        SymbolCandidate(node.name, "class", node.lineno),
                        explicit_exports,
                        ignored_names=framework_related_names,
                    )
                elif isinstance(node, ast.Assign):
                    if _is_framework_constructor_call(node.value):
                        continue
                    for target in node.targets:
                        for name in names_from_target(target):
                            _maybe_add(
                                mod,
                                SymbolCandidate(name, "variable", node.lineno),
                                explicit_exports,
                                ignored_names=framework_related_names,
                            )
                elif isinstance(node, ast.AnnAssign) and node.target:
                    if node.value is not None and _is_framework_constructor_call(node.value):
                        continue
                    for name in names_from_target(node.target):
                        _maybe_add(
                            mod,
                            SymbolCandidate(name, "variable", node.lineno),
                            explicit_exports,
                            ignored_names=framework_related_names,
                        )
                elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                    for name in names_from_target(node.name):
                        _maybe_add(
                            mod,
                            SymbolCandidate(name, "variable", node.lineno),
                            explicit_exports,
                            ignored_names=framework_related_names,
                        )

            modules[mod_name] = mod

    return modules


def collect_test_consumers(source_roots: list[Path]) -> dict[str, Module]:
    """Parse test files within test source roots for use as import consumers."""
    consumers: dict[str, Module] = {}
    for source_root in source_roots:
        if not is_test_source_root(source_root):
            continue
        for py_file in sorted(source_root.rglob("*.py")):
            name = py_file.name
            if not is_test_module_filename(name):
                continue
            if should_skip_test_consumer(py_file, source_root):
                continue
            mod_name = _module_name_from_path(py_file, source_root)
            assert mod_name is not None
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            consumers[mod_name] = Module(
                name=mod_name,
                path=py_file,
                package_parts=_package_parts(mod_name),
                tree=tree,
            )
    return consumers


def _extract_all(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return _strings_from_node(node.value)
    return None


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _decorator_attr_name(decorator: ast.expr) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_framework_callback(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        attr_name = _decorator_attr_name(decorator)
        if attr_name in _ROUTE_DECORATORS or attr_name in _CLI_DECORATORS:
            return True
    return False


def _framework_callback_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    expressions: list[ast.expr] = [*node.decorator_list]

    if node.returns is not None:
        expressions.append(node.returns)

    arg_annotations = [
        arg.annotation
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if arg.annotation is not None
    ]
    expressions.extend(arg_annotations)
    if node.args.vararg and node.args.vararg.annotation is not None:
        expressions.append(node.args.vararg.annotation)
    if node.args.kwarg and node.args.kwarg.annotation is not None:
        expressions.append(node.args.kwarg.annotation)

    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    expressions.extend(defaults)

    for expr in expressions:
        names.update(_names_in_expr(expr))

    return names


def _names_in_expr(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _is_framework_registration_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in _FRAMEWORK_REGISTRATION_CALLS


def _collect_framework_related_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_framework_callback(node):
                names.update(_framework_callback_names(node))
            continue

        expr: ast.expr | None = None
        if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            expr = node.value

        if expr is not None and _is_framework_registration_call(expr):
            names.update(_names_in_expr(expr))

    return names


def _is_pydantic_model(node: ast.ClassDef, known_models: set[str]) -> bool:
    for base in node.bases:
        base_name = _dotted_name(base)
        if base_name is None:
            continue
        if base_name == "BaseModel" or base_name.endswith(".BaseModel"):
            return True
        short = base_name.rsplit(".", 1)[-1]
        if short in known_models:
            return True
    return False


def _is_framework_constructor_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    callee = _dotted_name(node.func)
    if callee is None:
        return False
    short = callee.rsplit(".", 1)[-1]
    return short in _FRAMEWORK_CONSTRUCTORS


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


def names_from_target(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        result: list[str] = []
        for elt in node.elts:
            result.extend(names_from_target(elt))
        return result
    return []


def _maybe_add(
    mod: Module,
    candidate: SymbolCandidate,
    explicit_exports: set[str] | None,
    *,
    ignored_names: set[str] | None = None,
) -> None:
    name = candidate.name
    if name.startswith("_"):
        if _is_private_symbol_name(name):
            mod.private_symbols.append(
                Symbol(
                    name=name,
                    kind=candidate.kind,
                    lineno=candidate.lineno,
                    module=mod.name,
                    path=mod.path,
                ),
            )
        return
    if name in _ALLOWED_PUBLIC_NAMES:
        return
    if explicit_exports is not None and name in explicit_exports:
        return
    if ignored_names is not None and name in ignored_names:
        return
    mod.symbols.append(
        Symbol(
            name=name,
            kind=candidate.kind,
            lineno=candidate.lineno,
            module=mod.name,
            path=mod.path,
        ),
    )


def _is_private_symbol_name(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
