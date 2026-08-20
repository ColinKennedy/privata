"""Candidate eligibility and production-reference analysis."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._methods._ast import dotted_name as _dotted_name
from privata._methods._ast import referenced_names as _referenced_names_in_tree
from privata._models import NAMESPACE_SEPARATOR, Method

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from privata._models import Module

# Decorators that leave a method free to be renamed. Canonical names avoid
# treating unrelated decorators with a trusted basename as rename-safe.
_SAFE_METHOD_DECORATORS = frozenset(
    {
        "builtins.classmethod",
        "builtins.property",
        "builtins.staticmethod",
        "functools.cache",
        "functools.cached_property",
        "functools.lru_cache",
        "typing.final",
        "typing_extensions.final",
    },
)
_SAFE_CLASS_DECORATORS = frozenset(
    {
        "dataclasses.dataclass",
        "typing.final",
        "typing_extensions.final",
    },
)
_SAFE_DECORATOR_ROOTS = frozenset(
    name.split(NAMESPACE_SEPARATOR, 1)[0]
    for name in _SAFE_METHOD_DECORATORS | _SAFE_CLASS_DECORATORS
)
_BUILTIN_ALIASES = {
    "classmethod": "builtins.classmethod",
    "object": "builtins.object",
    "property": "builtins.property",
    "staticmethod": "builtins.staticmethod",
}
_LOCAL_DECORATOR_PREFIX = "<local>"
# Builtins that reach an attribute by name, so a computed name hides the target.
_DYNAMIC_LOOKUPS = frozenset({"delattr", "getattr", "hasattr", "setattr"})
_DYNAMIC_LOOKUP_NAME_POSITION = 2


def collect_method_candidates(
    modules: Mapping[str, Module],
    public_interface: set[tuple[str, str]] | None = None,
    test_references: Mapping[str, set[str]] | None = None,
) -> list[Method]:
    """Return public methods that only their own module refers to.

    A method counts as used when another production module mentions its name,
    either as an attribute access or as a string literal (for ``getattr`` style
    lookups). ``test_references`` supplies extra names for helper modules that
    live in a test source root, mirroring the test-helper rule for symbols.
    """
    interface = public_interface if public_interface is not None else set()
    extra_references = test_references if test_references is not None else {}
    references = _references_by_module(modules)
    base_names = _base_class_names(modules)

    candidates: list[Method] = []
    for module in modules.values():
        if module.tree is None:
            continue
        for node in module.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            decorator_aliases = _decorator_aliases(module.tree, node.lineno)
            if not _is_checkable_class(node, module, interface, decorator_aliases, base_names):
                continue
            candidates.extend(
                _class_method_candidates(
                    module,
                    node,
                    references,
                    extra_references.get(module.name, set()),
                    decorator_aliases,
                ),
            )

    candidates.sort(key=lambda method: (str(method.path), method.lineno))
    return candidates


def _referenced_names(module: Module) -> set[str]:
    """Return every attribute name and string literal a module mentions."""
    if module.tree is None:
        return set()
    return _referenced_names_in_tree(module.tree)


def _references_by_module(modules: Mapping[str, Module]) -> dict[str, set[str]]:
    """Map each referenced name to the modules that mention it."""
    references: dict[str, set[str]] = {}
    for module_name, module in modules.items():
        for name in _referenced_names(module):
            references.setdefault(name, set()).add(module_name)
    return references


def _base_class_names(modules: Mapping[str, Module]) -> frozenset[str]:
    """Return every name that any class in the project uses as a base.

    A subclass that only overrides a method never mentions the name as an
    attribute, so the reference scan cannot see it. Renaming the base method
    would leave the override stranded under its old name, so a class that
    anything subclasses keeps its methods public.
    """
    names: set[str] = set()
    for module in modules.values():
        if module.tree is None:
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                names.update(_referenced_base_names(node.bases))
    return frozenset(names)


def _referenced_base_names(bases: list[ast.expr]) -> set[str]:
    """Return the trailing names of every base expression.

    Bases are matched by trailing name, so ``Base``, ``mod.Base`` and the
    subscripted ``Base[int]`` all protect a class named ``Base``. Matching is
    deliberately loose: over-matching only suppresses reports, which is the
    safe direction.
    """
    names: set[str] = set()
    for base in bases:
        for node in ast.walk(base):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _class_method_candidates(
    module: Module,
    class_node: ast.ClassDef,
    references: Mapping[str, set[str]],
    test_references: set[str],
    decorator_aliases: Mapping[str, str],
) -> Iterator[Method]:
    aliases = dict(decorator_aliases)
    protected_methods = _protected_method_nodes(class_node)
    public_methods = sum(
        1
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    )
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _update_aliases(aliases, node)
            continue
        checkable = _is_checkable_method(node, aliases)
        aliases[node.name] = f"{_LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{node.name}"
        if not checkable:
            continue
        if id(node) in protected_methods:
            continue
        if node.lineno in module.ignored_lines:
            continue
        if node.name in test_references:
            continue
        if references.get(node.name, set()) - {module.name}:
            continue
        yield Method(
            name=node.name,
            class_name=class_node.name,
            lineno=node.lineno,
            module=module.name,
            path=module.path,
            class_lineno=class_node.lineno,
            class_public_methods=public_methods,
        )


def _protected_method_nodes(class_node: ast.ClassDef) -> set[int]:  # noqa: C901
    """Return methods whose class binding is consumed or replaced later."""
    current_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    protected: set[int] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header = [
                *node.decorator_list,
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
                *(
                    argument.annotation
                    for argument in [
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                        node.args.vararg,
                        node.args.kwarg,
                    ]
                    if argument is not None and argument.annotation is not None
                ),
                node.returns,
            ]
            for expression in header:
                if expression is not None:
                    for name in _loaded_names(expression):
                        if name in current_methods:
                            protected.add(id(current_methods[name]))
            previous = current_methods.get(node.name)
            if previous is not None:
                protected.add(id(previous))
            current_methods[node.name] = node
            continue
        for name in _loaded_names(node):
            if name in current_methods:
                protected.add(id(current_methods[name]))
        for name in _bound_names(node):
            previous = current_methods.pop(name.split(NAMESPACE_SEPARATOR, 1)[0], None)
            if previous is not None:
                protected.add(id(previous))
    return protected


def _is_checkable_class(
    node: ast.ClassDef,
    module: Module,
    public_interface: set[tuple[str, str]],
    decorator_aliases: Mapping[str, str],
    base_class_names: frozenset[str],
) -> bool:
    """Return whether a class owns its method names outright.

    Only plain, public, non-exported, non-subclassed classes qualify. A base
    class Privata cannot see may require a method to keep its public name, an
    exported class exposes its methods as part of the package interface, a
    class with subclasses owes its method names to those overrides, and a class
    that looks attributes up by a computed name may reach any of its own
    methods without ever spelling them out.
    """
    if (
        node.name.startswith("_")
        or node.name in module.exports
        or (module.name, node.name) in public_interface
        or node.name in base_class_names
    ):
        return False
    if node.keywords or any(
        _resolved_name(base, decorator_aliases) != "builtins.object" for base in node.bases
    ):
        return False
    if _looks_up_attributes_dynamically(node):
        return False
    return _has_only_safe_decorators(
        node.decorator_list,
        _SAFE_CLASS_DECORATORS,
        decorator_aliases,
    )


def _looks_up_attributes_dynamically(node: ast.ClassDef) -> bool:
    """Return whether a class reaches attributes by a name it computes.

    ``getattr(self, "visit_" + kind)`` names a method that appears nowhere as a
    literal, so the reference scan cannot see the call. A class that dispatches
    this way may reach any of its own methods, and renaming one would break a
    call the scan never knew about, so none of its methods are checked.

    Only a non-literal second argument counts. ``getattr(self, "run")`` spells
    the name out and is already picked up as a string reference.
    """
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in _DYNAMIC_LOOKUPS
        and len(child.args) >= _DYNAMIC_LOOKUP_NAME_POSITION
        and not _is_string_literal(child.args[1])
        for child in ast.walk(node)
    )


def _is_string_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_checkable_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    decorator_aliases: Mapping[str, str],
) -> bool:
    if node.name.startswith("_"):
        return False
    if _forwards_to_same_named_super_method(node):
        return False
    return _has_only_safe_decorators(
        node.decorator_list,
        _SAFE_METHOD_DECORATORS,
        decorator_aliases,
    )


def _forwards_to_same_named_super_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Return whether a method participates in a cooperative super call."""
    return any(
        isinstance(child, ast.Attribute)
        and child.attr == node.name
        and isinstance(child.value, ast.Call)
        and isinstance(child.value.func, ast.Name)
        and child.value.func.id == "super"
        for child in ast.walk(node)
    )


def _has_only_safe_decorators(
    decorators: list[ast.expr],
    safe_names: frozenset[str],
    aliases: Mapping[str, str],
) -> bool:
    return all(_decorator_name(decorator, aliases) in safe_names for decorator in decorators)


def _decorator_name(decorator: ast.expr, aliases: Mapping[str, str]) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return _resolved_name(target, aliases)


def _resolved_name(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    parts = dotted.split(NAMESPACE_SEPARATOR)
    for index in range(len(parts), 0, -1):
        prefix = NAMESPACE_SEPARATOR.join(parts[:index])
        resolved = aliases.get(prefix)
        if resolved is not None:
            suffix = NAMESPACE_SEPARATOR.join(parts[index:])
            return f"{resolved}{NAMESPACE_SEPARATOR}{suffix}" if suffix else resolved
    return dotted


def _decorator_aliases(
    tree: ast.Module,
    before_lineno: int,
) -> dict[str, str]:
    """Return canonical module-level names used by decorator expressions."""
    aliases = dict(_BUILTIN_ALIASES)

    for node in tree.body:
        if node.lineno >= before_lineno:
            break
        _update_aliases(aliases, node)
    return aliases


def _update_aliases(aliases: dict[str, str], node: ast.stmt) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(NAMESPACE_SEPARATOR, 1)[0]
            imported = alias.name if alias.asname else local
            aliases[local] = imported
    elif isinstance(node, ast.ImportFrom):
        source = f"{'.' * node.level}{node.module or ''}"
        for alias in node.names:
            if alias.name == "*":
                for name in {*aliases, *_SAFE_DECORATOR_ROOTS}:
                    aliases[name] = f"{_LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{name}"
            else:
                aliases[alias.asname or alias.name] = f"{source}{NAMESPACE_SEPARATOR}{alias.name}"
    else:
        for name in _bound_names(node):
            aliases[name] = f"{_LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{name}"


class _BoundNameCollector(ast.NodeVisitor):
    """Collect bindings without descending into nested scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            dotted = _dotted_name(node)
            if dotted is not None:
                self.names.add(dotted)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(NAMESPACE_SEPARATOR, 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)


def _bound_names(node: ast.stmt) -> set[str]:
    collector = _BoundNameCollector()
    collector.visit(node)
    return collector.names


class _LoadedNameCollector(ast.NodeVisitor):
    """Collect class-scope reads without entering nested scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        del node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        del node

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        del node

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node


def _loaded_names(node: ast.AST) -> set[str]:
    collector = _LoadedNameCollector()
    collector.visit(node)
    return collector.names
