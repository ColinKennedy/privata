"""Tests for ``privata``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from privata import (
    find_export_issues,
    find_method_candidates,
    find_module_collisions,
    find_private_candidates,
    find_private_module_imports,
    find_private_symbol_imports,
    find_unparsable_modules,
)
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
from privata._models import Module
from privata._modules import collect_modules
from privata._source_roots import source_roots
from privata.cli import main as cli_main


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _symbols(project_root: Path) -> set[tuple[str, str]]:
    return {(symbol.module, symbol.name) for symbol in find_private_candidates(project_root)}


def _methods(project_root: Path) -> set[tuple[str, str, str]]:
    return {
        (method.module, method.class_name, method.name)
        for method in find_method_candidates(project_root)
    }


def _private_module_imports(project_root: Path) -> set[tuple[str, str]]:
    return {
        (issue.module, issue.imported_by) for issue in find_private_module_imports(project_root)
    }


def _private_symbol_imports(project_root: Path) -> set[tuple[str, str, str]]:
    return {
        (issue.module, issue.name, issue.imported_by)
        for issue in find_private_symbol_imports(project_root)
    }


def _export_issues(project_root: Path) -> set[tuple[str, str, str]]:
    return {(issue.module, issue.name, issue.kind) for issue in find_export_issues(project_root)}


def _module_collisions(project_root: Path) -> dict[str, list[str]]:
    return {
        collision.module: [path.relative_to(project_root).as_posix() for path in collision.paths]
        for collision in find_module_collisions(project_root)
    }


def test_fastapi_route_functions_and_models_are_skipped(tmp_path: Path) -> None:
    """FastAPI route handlers and request/response models should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class RequestModel(BaseModel):
    value: int

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "health") not in symbols
    assert ("pkg.api", "RequestModel") not in symbols
    assert ("pkg.api", "router") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_fastapi_related_type_aliases_and_derived_models_are_skipped(tmp_path: Path) -> None:
    """Names only referenced from route signatures/decorators should be skipped."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Annotated

router = APIRouter()

class BasePayload(BaseModel):
    name: str

class ExtendedPayload(BasePayload):
    age: int

RoomFilter = Annotated[str | None, Query(default=None)]

@router.get("/items", response_model=ExtendedPayload)
async def list_items(room_id: RoomFilter = None) -> ExtendedPayload:
    return ExtendedPayload(name="a", age=1)
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "ExtendedPayload") not in symbols
    assert ("pkg.api", "RoomFilter") not in symbols
    assert ("pkg.api", "list_items") not in symbols


def test_typer_callbacks_are_skipped(tmp_path: Path) -> None:
    """Typer command callbacks should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
import typer

app = typer.Typer()

@app.command()
def run() -> None:
    pass

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "app") not in symbols
    assert ("pkg.cli", "run") not in symbols
    assert ("pkg.cli", "local_helper") in symbols


def test_logger_variable_is_ignored(tmp_path: Path) -> None:
    """Module-level logger should be ignored by privacy checks."""
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        """
from logging import getLogger

logger = getLogger(__name__)

def local_helper() -> int:
    logger.info("x")
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.mod", "logger") not in symbols
    assert ("pkg.mod", "local_helper") in symbols


def test_pyproject_console_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Console-script entrypoints in pyproject.toml should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
def main() -> int:
    return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "example"
version = "0.1.0"
scripts.example = "pkg.cli:main"
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "main") not in symbols


def test_fastapi_include_router_dependency_symbols_are_skipped(tmp_path: Path) -> None:
    """FastAPI dependency callbacks used via include_router should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Depends, FastAPI

app = FastAPI()
router = APIRouter()

async def verify_user() -> dict[str, str]:
    return {"id": "1"}

app.include_router(router, dependencies=[Depends(verify_user)])

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "verify_user") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_shell_uvicorn_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Uvicorn entrypoints in shell scripts should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "server.py",
        """
def build_app() -> object:
    return object()

asgi = build_app()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "run-server.sh",
        """
#!/usr/bin/env bash
exec uvicorn pkg.server:asgi --host 0.0.0.0 --port 8000
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.server", "asgi") not in symbols


def test_package_init_reexports_count_as_cross_module_imports(tmp_path: Path) -> None:
    """Symbols re-exported from package ``__init__`` are production imports."""
    _write(
        tmp_path / "src" / "pkg" / "features" / "types.py",
        """
PUBLIC_VALUE = "value"

def public_helper() -> str:
    return PUBLIC_VALUE

def local_helper() -> str:
    return "local"
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "features" / "__init__.py",
        """
from .types import PUBLIC_VALUE, public_helper

__all__ = ["PUBLIC_VALUE", "public_helper"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.features.types", "PUBLIC_VALUE") not in symbols
    assert ("pkg.features.types", "public_helper") not in symbols
    assert ("pkg.features.types", "local_helper") in symbols


def test_package_init_self_reexport_is_skipped_via_package_reexports(
    tmp_path: Path,
) -> None:
    """A symbol an ``__init__`` re-exports from its own package is not a candidate.

    ``cross_imports`` certifies most ``__init__`` re-exports on its own, because the
    ``__init__`` module doing the importing is itself scanned as a "consumer" of the
    module it imports from -- see ``test_package_init_reexports_count_as_cross_module_imports``.
    That certification has one blind spot: it explicitly ignores a module importing
    from *itself*. A package ``__init__.py`` that both defines a symbol and re-imports
    it through its own package name (a redundant but legal re-export idiom) hits that
    blind spot, so only ``package_reexports`` -- not ``cross_imports`` -- certifies it.
    """
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
class Thing:
    pass

from pkg import Thing
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg", "Thing") not in symbols


def test_tach_interface_exposed_symbols_are_skipped(tmp_path: Path) -> None:
    """Tach interface exposure marks a symbol as public even without src imports."""
    _write(
        tmp_path / "src" / "pkg" / "runtime.py",
        """
class RuntimeFacade:
    pass

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["src"]

[[interfaces]]
from = ["pkg.runtime"]
expose = ["RuntimeFacade"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.runtime", "RuntimeFacade") not in symbols
    assert ("pkg.runtime", "local_helper") in symbols


def test_external_test_source_root_keeps_public_symbol(tmp_path: Path) -> None:
    """A sibling project's tests can keep a scanned project's symbol public."""
    core = tmp_path / "core"
    engine = tmp_path / "engine"
    _write(
        core / "src" / "corepkg" / "api.py",
        """
def public_helper() -> int:
    return 1

def local_helper() -> int:
    return 2
""".strip()
        + "\n",
    )
    _write(
        engine / "tests" / "test_api.py",
        """
from corepkg.api import public_helper


def test_public_helper() -> None:
    assert public_helper() == 1
""".strip()
        + "\n",
    )
    _write(
        core / "tach.toml",
        'source_roots = ["src", "../engine/tests"]\n',
    )

    symbols = _symbols(core)
    assert ("corepkg.api", "public_helper") not in symbols
    assert ("corepkg.api", "local_helper") in symbols


def test_private_module_imported_within_same_package_is_ignored(tmp_path: Path) -> None:
    """Private modules can be imported from within their own package subtree."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "one" / "public.py",
        """
from ._internal import helper

VALUE = helper()
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_private_module_imported_from_other_package_is_reported(tmp_path: Path) -> None:
    """Private modules imported outside their package subtree should be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal

VALUE = _internal.VALUE
""".strip()
        + "\n",
    )

    private_module_imports = _private_module_imports(tmp_path)
    assert ("pkg.one._internal", "pkg.two.public") in private_module_imports


def test_cli_reports_private_symbol_imports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Private top-level symbols imported by production modules should be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "write_coordinator.py",
        """
class _EventCacheWriteCoordinator:
    pass

def local_helper() -> None:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "runtime_support.py",
        """
from pkg.write_coordinator import _EventCacheWriteCoordinator
""".strip()
        + "\n",
    )

    assert cli_main([str(tmp_path)]) == 1

    output = capsys.readouterr()
    assert "Found 1 private symbol import from production modules:" in output.out
    assert (
        "src/pkg/runtime_support.py:1: imports private symbol "
        "`pkg.write_coordinator._EventCacheWriteCoordinator`"
    ) in output.out
    assert "function `local_helper`\n\nFound 1 private symbol import" in output.out


def test_private_symbol_imports_are_reported(tmp_path: Path) -> None:
    """Private symbol import findings include the source symbol and consumer."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
class _PrivateService:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import _PrivateService
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_PrivateService", "pkg.consumer"),
    }


def test_private_symbol_self_import_is_ignored(tmp_path: Path) -> None:
    """Self imports do not count as private symbol boundary crossings."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
from .producer import _PrivateService

class _PrivateService:
    pass
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == set()


def test_private_module_imported_only_by_tests_is_ignored(tmp_path: Path) -> None:
    """Only imports from production source roots count for private module detection."""
    _write(
        tmp_path / "src" / "pkg" / "_internal.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_private.py",
        """
from pkg._internal import helper

assert helper() == 1
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_cli_defaults_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running privata without arguments scans the current directory."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    monkeypatch.chdir(tmp_path)

    assert cli_main([]) == 1
    output = capsys.readouterr()
    assert "src/pkg/module.py:1: function `helper`" in output.out
    assert output.err == ""


def test_collect_modules_skips_invalid_and_cache_files(tmp_path: Path) -> None:
    """Invalid Python and cache files should not affect scanning."""
    _write(tmp_path / "src" / "__init__.py", "")
    _write(
        tmp_path / "src" / "single.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(tmp_path / "src" / "broken.py", "def broken(:\n")
    _write(tmp_path / "src" / "__pycache__" / "cached.py", "CACHED = 1\n")

    symbols = _symbols(tmp_path)
    assert ("single", "VALUE") in symbols
    assert ("broken", "broken") not in symbols
    assert ("cached", "CACHED") not in symbols


def test_collect_modules_returns_only_the_parsed_modules(tmp_path: Path) -> None:
    """The public collector drops unparsable files and reports nothing about them."""
    _write(tmp_path / "src" / "good.py", "VALUE = 1\n")
    _write(tmp_path / "src" / "broken.py", "def broken(:\n")

    modules = collect_modules(source_roots(tmp_path))

    assert set(modules) == {"good"}


def test_annotated_assignments_type_aliases_and_unpacking_are_collected(
    tmp_path: Path,
) -> None:
    """Assignment forms should be discovered as public symbols."""
    _write(
        tmp_path / "src" / "pkg" / "types.py",
        """
Name: type = str
Alias = int
PairAlias: type = tuple[str, str]
FIRST, (SECOND, _ignored) = (1, (2, 3))
app = make_app()()
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.types", "Name") in symbols
    assert ("pkg.types", "Alias") in symbols
    assert ("pkg.types", "PairAlias") in symbols
    assert ("pkg.types", "FIRST") in symbols
    assert ("pkg.types", "SECOND") in symbols
    assert ("pkg.types", "app") in symbols


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_type_statement_is_collected_on_supported_python(tmp_path: Path) -> None:
    """PEP 695 type statements are collected when the parser supports them."""
    _write(
        tmp_path / "src" / "pkg" / "aliases.py",
        """
type UserId = int
""".strip()
        + "\n",
    )

    assert ("pkg.aliases", "UserId") in _symbols(tmp_path)


def test_framework_callback_signature_names_are_skipped(tmp_path: Path) -> None:
    """Framework callback signature annotations and defaults mark names public."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Depends

router = APIRouter()

class Item:
    pass

class Params:
    pass

def fallback() -> Item:
    return Item()

@router.post("/items")
def create_item(*items: Item, item: Item = fallback(), **params: Params) -> Item:
    return item
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "Item") not in symbols
    assert ("pkg.api", "Params") not in symbols
    assert ("pkg.api", "fallback") not in symbols
    assert ("pkg.api", "create_item") not in symbols


def test_pytest_fixture_functions_are_skipped(tmp_path: Path) -> None:
    """Fixtures are consumed via parameter-name injection, never import, in a production module."""
    _write(
        tmp_path / "src" / "pkg" / "testing.py",
        """
import pytest


@pytest.fixture
def sandbox_dir(tmp_path):
    yield tmp_path


@pytest.fixture()
def bare_call_fixture():
    yield None


@pytest.fixture(scope="module")
def scoped_fixture():
    yield None


def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.testing", "sandbox_dir") not in symbols
    assert ("pkg.testing", "bare_call_fixture") not in symbols
    assert ("pkg.testing", "scoped_fixture") not in symbols
    assert ("pkg.testing", "local_helper") in symbols


def test_bare_fixture_decorator_is_skipped(tmp_path: Path) -> None:
    """A fixture imported directly (``from pytest import fixture``) is still exempt."""
    _write(
        tmp_path / "src" / "pkg" / "testing.py",
        """
from pytest import fixture


@fixture
def sandbox_dir(tmp_path):
    yield tmp_path


@fixture()
def other_fixture():
    yield None
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.testing", "sandbox_dir") not in symbols
    assert ("pkg.testing", "other_fixture") not in symbols


def test_async_pytest_fixture_is_skipped(tmp_path: Path) -> None:
    """Async fixtures are exempt the same way as sync ones."""
    _write(
        tmp_path / "src" / "pkg" / "testing.py",
        """
import pytest


@pytest.fixture
async def async_sandbox_dir(tmp_path):
    yield tmp_path
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.testing", "async_sandbox_dir") not in symbols


def test_pytest_plugins_variable_is_skipped(tmp_path: Path) -> None:
    """``pytest_plugins`` is read by pytest itself, never imported."""
    _write(
        tmp_path / "src" / "pkg" / "conftest.py",
        """
pytest_plugins = ["pkg.fixtures"]

local_helper_value = 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.conftest", "pytest_plugins") not in symbols
    assert ("pkg.conftest", "local_helper_value") in symbols


def test_pytest_hook_functions_are_skipped(tmp_path: Path) -> None:
    """``pytest_*`` hook functions are called by pytest itself, never imported."""
    _write(
        tmp_path / "src" / "pkg" / "conftest.py",
        """
def pytest_configure(config) -> None:
    pass


def pytest_collection_modifyitems(config, items) -> None:
    pass


def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.conftest", "pytest_configure") not in symbols
    assert ("pkg.conftest", "pytest_collection_modifyitems") not in symbols
    assert ("pkg.conftest", "local_helper") in symbols


def test_non_framework_decorators_and_dynamic_bases_are_handled(tmp_path: Path) -> None:
    """Plain decorators and dynamic bases should not crash name analysis."""
    _write(
        tmp_path / "src" / "pkg" / "models.py",
        """
def decorator(func):
    return func

def make_base():
    return object()

@decorator
def helper() -> int:
    return 1

class Dynamic(make_base().Base):
    pass
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.models", "decorator") in symbols
    assert ("pkg.models", "make_base") in symbols
    assert ("pkg.models", "helper") in symbols
    assert ("pkg.models", "Dynamic") in symbols


def test_non_literal_all_does_not_hide_symbols(tmp_path: Path) -> None:
    """A dynamic ``__all__`` is ignored because it cannot be trusted statically."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
NAMES = ["EXPORTED"]
__all__ = ["EXPORTED", *NAMES]
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") in _symbols(tmp_path)


def test_literal_all_hides_exported_symbols(tmp_path: Path) -> None:
    """A literal ``__all__`` marks listed symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
__all__ = ["EXPORTED"]
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") not in _symbols(tmp_path)


def test_literal_all_reports_unknown_and_missing_public_exports(tmp_path: Path) -> None:
    """Literal ``__all__`` should be exact for public local top-level bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from __future__ import annotations
import json as json_module
from dataclasses import dataclass

__all__ = ["Exported", "MISSING"]

@dataclass
class Exported:
    value: str

async def async_helper() -> None:
    pass

LOCAL = json_module.dumps({"x": 1})
_PRIVATE = 1
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {
        ("pkg.exports", "MISSING", "unknown"),
        ("pkg.exports", "LOCAL", "missing"),
        ("pkg.exports", "async_helper", "missing"),
    }


def test_literal_all_accepts_reexports_and_try_fallbacks(tmp_path: Path) -> None:
    """Common package export patterns should validate when ``__all__`` is complete."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class PublicThing:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from .module import PublicThing

try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0"

__all__ = ["PublicThing", "__version__"]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_package_init_imports(tmp_path: Path) -> None:
    """Package imports are valid explicit exports but are not mandatory exports."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class PublicThing:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from .module import PublicThing

__all__ = []
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_logger_binding(tmp_path: Path) -> None:
    """Module-level loggers are implementation details by convention."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["public"]

logger = object()

def public() -> None:
    pass
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_regular_module_imports(tmp_path: Path) -> None:
    """Imported dependencies are not local public API that must be listed in ``__all__``."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json

__all__ = ["Exported"]

@dataclass
class Exported:
    path: Path
    value: Any
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_accepts_imported_regular_module_exports(tmp_path: Path) -> None:
    """A regular module may still explicitly export an imported binding."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from .types import PublicType

__all__ = ["PublicType"]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_reports_private_explicit_exports(tmp_path: Path) -> None:
    """Literal ``__all__`` should not export private bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["_private_helper"]

def _private_helper() -> None:
    pass
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {("pkg.exports", "_private_helper", "private")}


def test_dynamic_all_is_not_validated(tmp_path: Path) -> None:
    """Non-literal ``__all__`` forms are ignored by export validation."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
PUBLIC = 1
NAMES = ["PUBLIC"]
__all__ = ["PUBLIC", *NAMES]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_literal_all_checks_annotated_assignments_and_type_aliases(tmp_path: Path) -> None:
    """Annotated assignments and PEP 695 type aliases are public bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["Name"]

Name: type = str
type UserId = int
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {("pkg.exports", "UserId", "missing")}


def test_export_validation_ignores_unparsed_modules(tmp_path: Path) -> None:
    """Modules without parsed ASTs should not affect export validation."""
    module = Module("pkg.empty", tmp_path / "empty.py", ("pkg",), tree=None)

    assert collect_export_issues({"pkg.empty": module}) == []


def test_string_all_does_not_hide_symbols(tmp_path: Path) -> None:
    """A string ``__all__`` is malformed and should not hide symbols."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
__all__ = "EXPORTED"
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") in _symbols(tmp_path)


def test_unsupported_assignment_targets_are_ignored(tmp_path: Path) -> None:
    """Attribute assignment targets are not top-level public symbol definitions."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class Holder:
    pass

holder = Holder()
holder.value = 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.module", "Holder") in symbols
    assert ("pkg.module", "holder") in symbols
    assert ("pkg.module", "value") not in symbols


def test_star_imports_submodule_imports_and_bad_relatives_count_correctly(
    tmp_path: Path,
) -> None:
    """Cross-import detection should handle star, submodule, and invalid relative imports."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
OTHER = 2
LOCAL = 3
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "submod.py",
        """
THING = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .source import *
from . import submod
from ...missing import nope

USED = VALUE + submod.THING
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.source", "VALUE") not in symbols
    assert ("pkg.source", "OTHER") not in symbols
    assert ("pkg.source", "LOCAL") not in symbols
    assert ("pkg.submod", "THING") not in symbols


def test_attribute_access_with_non_name_base_is_ignored(tmp_path: Path) -> None:
    """Only imported module aliases should mark module attributes as externally used."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
def factory():
    import pkg.source
    return pkg.source

USED = factory().VALUE
""".strip()
        + "\n",
    )

    assert ("pkg.source", "VALUE") in _symbols(tmp_path)


def test_private_module_import_edge_cases(tmp_path: Path) -> None:
    """Private import detection handles self imports and invalid relatives."""
    _write(
        tmp_path / "src" / "pkg" / "_internal.py",
        """
import pkg._internal
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "public.py",
        """
from ...missing import _internal
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_private_module_import_helpers_ignore_unparsed_modules(tmp_path: Path) -> None:
    """Internal helpers should tolerate modules without parsed ASTs."""
    module = Module("pkg.empty", tmp_path / "empty.py", ("pkg",), tree=None)

    assert find_cross_imports({"pkg.empty": module}) == set()
    assert collect_private_module_imports({"pkg.empty": module}) == []
    assert collect_private_symbol_imports({"pkg.empty": module}) == []


def test_annotated_framework_constructor_is_skipped(tmp_path: Path) -> None:
    """Annotated framework constructor assignments should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter

router: APIRouter = APIRouter()
""".strip()
        + "\n",
    )

    assert ("pkg.api", "router") not in _symbols(tmp_path)


def test_pyproject_ignores_malformed_entrypoints(tmp_path: Path) -> None:
    """Only string entrypoints with module:symbol shape are public."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
def main() -> int:
    return 0

def gui() -> int:
    return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "example"
scripts = "not-a-table"

[project.gui-scripts]
good = "pkg.cli:gui"
bad_shape = "pkg.cli"
bad_type = 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "gui") not in symbols
    assert ("pkg.cli", "main") in symbols


def test_scripts_directory_uvicorn_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Uvicorn entrypoints under scripts/ should be honored."""
    _write(
        tmp_path / "src" / "pkg" / "server.py",
        """
app = object()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "scripts" / "serve.sh",
        """
uvicorn pkg.server:app
""".strip()
        + "\n",
    )

    assert ("pkg.server", "app") not in _symbols(tmp_path)


def test_tach_ignores_malformed_interfaces(tmp_path: Path) -> None:
    """Malformed Tach interface entries should not mark symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "runtime.py",
        """
class RuntimeFacade:
    pass

class OtherFacade:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
[[interfaces]]
from = "pkg.runtime"
expose = ["RuntimeFacade"]

[[interfaces]]
from = [1, "pkg.runtime"]
expose = [2, "OtherFacade"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.runtime", "RuntimeFacade") in symbols
    assert ("pkg.runtime", "OtherFacade") not in symbols


def test_cli_reports_no_src_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project without Python source files is clean instead of requiring src/."""
    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


def test_project_root_is_scanned_when_src_directory_is_absent(tmp_path: Path) -> None:
    """Projects without src/ should still be scanned from the project root."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_root_level_test_files_certify_imports_in_project_root_fallback(tmp_path: Path) -> None:
    """Root-level pytest modules should count as production imports, like any other test file."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1

def unused() -> int:
    return 2
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "test_module.py",
        """
from pkg.module import helper
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "module_test.py",
        """
from pkg.module import helper as imported_helper
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.module", "helper") not in symbols
    assert ("pkg.module", "unused") in symbols


def test_nested_tests_directory_certifies_imports_when_scanning_project_root(
    tmp_path: Path,
) -> None:
    """A tests/ directory nested inside a production root should certify its imports."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pkg" / "tests" / "test_module.py",
        """
from pkg.module import helper
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") not in _symbols(tmp_path)


def test_tach_source_roots_define_scanned_roots(tmp_path: Path) -> None:
    """Tach source_roots should control which roots are scanned."""
    _write(
        tmp_path / "lib" / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pkg" / "ignored.py",
        """
def ignored() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["lib"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.module", "helper") in symbols
    assert ("pkg.ignored", "ignored") not in symbols


def test_malformed_tach_source_roots_fall_back_to_project_layout(tmp_path: Path) -> None:
    """Malformed Tach source_roots should not break source discovery."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = "pkg"
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)

    _write(
        tmp_path / "tach.toml",
        """
source_roots = [1, "missing"]
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_cli_reports_clean_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project with no public candidates reports cleanly."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


def test_cli_reports_private_imports_without_symbol_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Private import findings are printed even when no public symbols are found."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
_VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 private module import outside the owning package subtree:" in output.out
    assert "src/pkg/two/public.py:1: imports private module `pkg.one._internal`" in output.out


def test_cli_reports_export_issues_without_symbol_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Literal ``__all__`` mismatches are printed as their own finding section."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["MISSING"]
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 __all__ export issue:" in output.out
    assert "src/pkg/exports.py:1: __all__ exports unknown name `MISSING`" in output.out


def test_cli_reports_private_all_exports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Private names in ``__all__`` should be printed as private export issues."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["_private_helper"]

def _private_helper() -> None:
    pass
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "src/pkg/exports.py:1: __all__ exports private name `_private_helper`" in output.out


def test_cli_separates_symbol_and_private_import_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blank line separates symbol and private import sections."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
_VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "function `local_helper`\n\nFound 1 private module import" in output.out


def test_cli_separates_export_findings_from_previous_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blank line separates export findings from earlier finding sections."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["MISSING"]

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "function `local_helper`\n\nFound 2 __all__ export issues" in output.out


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_cli_wrapper_and_module_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The installed CLI wrapper and Python module entrypoints call the checker."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert cli_main() == 0
    assert "No module privacy issues found." in capsys.readouterr().out

    with pytest.raises(SystemExit) as package_exit:
        runpy.run_module("privata", run_name="__main__")
    assert package_exit.value.code == 0

    with pytest.raises(SystemExit) as cli_exit:
        runpy.run_module("privata.cli", run_name="__main__")
    assert cli_exit.value.code == 0


def test_cli_uses_argparse_for_help(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console wrapper should expose argparse help without running checks.

    Python 3.14 colours argparse help, so the assertions below would trip over
    escape sequences. ``NO_COLOR`` pins the output to the plain form without
    changing what a user sees.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit) as cli_exit:
        cli_main(["--help"])

    assert cli_exit.value.code == 0
    output = capsys.readouterr()
    assert output.out.startswith("usage: privata")
    assert "project-root" in output.out
    assert "No module privacy issues found." not in output.out
    assert output.err == ""


def test_cli_accepts_explicit_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The argparse wrapper accepts an argv sequence for tests and embedding."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_checker_module_is_not_runtime_entrypoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The internal checker module should not also act as a CLI entrypoint."""
    runpy.run_module("privata._checker", run_name="__main__")

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_privata_ignore_suppresses_bare_private_module_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a bare import statement suppresses the finding."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        "VALUE = 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        "import pkg.one._internal  # privata: ignore\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_privata_ignore_suppresses_from_private_module_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a from-import suppresses a private module finding."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        "VALUE = 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        "from pkg.one import _internal  # privata: ignore\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_privata_ignore_suppresses_private_symbol_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a from-import suppresses a private symbol finding."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        "class _PrivateService:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        "from .producer import _PrivateService  # privata: ignore\n",
    )

    assert _private_symbol_imports(tmp_path) == set()


def test_privata_ignore_suppresses_one_multiline_private_symbol_import(
    tmp_path: Path,
) -> None:
    """In multi-line imports, # privata: ignore applies to the alias line."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
class _IgnoredService:
    pass

class _ReportedService:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import (
    _IgnoredService,  # privata: ignore
    _ReportedService,
)
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_ReportedService", "pkg.consumer"),
    }


def test_privata_ignore_suppresses_one_multiline_private_submodule_import(
    tmp_path: Path,
) -> None:
    """In multi-line from-imports, # privata: ignore applies per private submodule alias."""
    _write(tmp_path / "src" / "pkg" / "one" / "_ignored.py", "VALUE = 1\n")
    _write(tmp_path / "src" / "pkg" / "one" / "_reported.py", "VALUE = 2\n")
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import (
    _ignored,  # privata: ignore
    _reported,
)
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == {
        ("pkg.one._reported", "pkg.two.public"),
    }


def test_privata_ignore_on_multiline_header_keeps_private_symbol_findings(
    tmp_path: Path,
) -> None:
    """In multi-line imports, header comments do not suppress alias findings."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        "class _PrivateService:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import (  # privata: ignore
    _PrivateService,
)
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_PrivateService", "pkg.consumer"),
    }


def test_privata_ignore_on_multiline_header_keeps_private_submodule_findings(
    tmp_path: Path,
) -> None:
    """In multi-line from-imports, header comments do not suppress private submodules."""
    _write(tmp_path / "src" / "pkg" / "one" / "_internal.py", "VALUE = 1\n")
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import (  # privata: ignore
    _internal,
)
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == {
        ("pkg.one._internal", "pkg.two.public"),
    }


def test_symbol_ignore_comment_suppresses_finding(tmp_path: Path) -> None:
    """A # privata: ignore comment on the definition line suppresses the symbol finding."""
    _write(
        tmp_path / "src" / "pkg" / "plugins.py",
        """
class AlphaPlugin:  # privata: ignore
    pass

class BravoPlugin:
    pass
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.plugins", "AlphaPlugin") not in symbols
    assert ("pkg.plugins", "BravoPlugin") in symbols


def test_plain_import_chained_attribute_access_is_detected(tmp_path: Path) -> None:
    """import pkg.mod followed by pkg.mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module

result = provider.module.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_plain_import_deeply_chained_attribute_access_is_detected(tmp_path: Path) -> None:
    """import a.b.c.mod followed by a.b.c.mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import pkg.sub.leaf

result = pkg.sub.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_aliased_plain_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """import pkg.mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module as m

result = m.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_aliased_deep_plain_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """import a.b.c.mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import pkg.sub.leaf as leaf

result = leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_from_submodule_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod followed by mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module

result = module.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_from_submodule_aliased_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module as m

result = m.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_plain_import_chained_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """import pkg.mod followed by pkg.mod.SomeClass and pkg.mod.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module

x = provider.module.SomeClass()
y = provider.module.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_aliased_import_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """import pkg.mod as m followed by m.SomeClass and m.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module as m

x = m.SomeClass()
y = m.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_from_import_class_and_variable_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod followed by mod.SomeClass and mod.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module

x = module.SomeClass()
y = module.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_from_aliased_import_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """from pkg import mod as m followed by m.SomeClass and m.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module as m

x = m.SomeClass()
y = m.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_aliased_subpackage_import_deep_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import sub followed by sub.mod.Symbol should count as cross-module usage."""
    _write(tmp_path / "src" / "pkg" / "sub" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from pkg import sub

result = sub.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_aliased_subpackage_aliased_import_deep_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """from pkg import sub as s followed by s.mod.Symbol should count as cross-module usage."""
    _write(tmp_path / "src" / "pkg" / "sub" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from pkg import sub as s

result = s.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_chained_attribute_on_call_result_base_is_ignored(tmp_path: Path) -> None:
    """Attribute chains rooted in a call result should not be treated as module access."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
def factory():
    pass

USED = factory().sub.VALUE
""".strip()
        + "\n",
    )

    assert ("pkg.source", "VALUE") in _symbols(tmp_path)


def test_unimported_dotted_attribute_access_is_ignored(tmp_path: Path) -> None:
    """Dotted names should only count as module usage when backed by an import."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
result = provider.module.get_value()
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "get_value") in symbols


def test_privata_is_clean_under_its_own_rules() -> None:
    """Privata should pass its own public-symbol check."""
    project_root = Path(__file__).resolve().parents[1]

    assert _symbols(project_root) == set()


def test_test_source_root_consumers_count_for_publicness(tmp_path: Path) -> None:
    """Symbols in a test source root used by co-located test files should not be flagged."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n",
    )
    # snake_case test file
    _write(
        tmp_path / "tests" / "inner_folder" / "test_blah.py",
        "import something\n\ndef test_value() -> None:\n    assert something.get_value() == 42\n",
    )
    # camelCase testFoo style
    _write(
        tmp_path / "tests" / "inner_folder" / "testHelper.py",
        "import something\n\ndef testValue() -> None:\n    something.get_value()\n",
    )
    # camelCase fooTest style
    _write(
        tmp_path / "tests" / "inner_folder" / "helperTest.py",
        "import something\n",
    )
    # test file inside a hidden subdir → skipped as consumer
    _write(
        tmp_path / "tests" / ".hidden" / "test_noise.py",
        "import something\n",
    )
    # test file with broken syntax → skipped as consumer
    _write(
        tmp_path / "tests" / "test_broken.py",
        "def bad(:\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    assert ("something", "get_value") not in _symbols(tmp_path)


def test_test_source_root_consumers_do_not_count_for_production_modules(
    tmp_path: Path,
) -> None:
    """Test source roots must not make production symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_module.py",
        "from pkg.module import helper\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_camelcase_test_files_count_as_consumers(tmp_path: Path) -> None:
    """camelCase test filenames nested in a production root should certify their imports."""
    _write(
        tmp_path / "pkg" / "module.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "testModule.py",
        "from pkg.module import helper\n",
    )
    _write(
        tmp_path / "moduleTest.py",
        "from pkg.module import helper\n",
    )
    assert ("pkg.module", "helper") not in _symbols(tmp_path)


def test_test_source_root_symbols_without_consumers_remain_flagged(tmp_path: Path) -> None:
    """Symbols in a test source root with no test consumers are still private candidates."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    assert ("something", "get_value") in _symbols(tmp_path)


def test_test_source_root_partially_consumed_symbols(tmp_path: Path) -> None:
    """In a test source root, used symbols are cleared while unused ones remain flagged."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n\ndef get_bar() -> int:\n    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        "import something\n\ndef test_value() -> None:\n    assert something.get_value() == 42\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    symbols = _symbols(tmp_path)
    assert ("something", "get_value") not in symbols
    assert ("something", "get_bar") in symbols


def test_test_file_nested_in_production_source_root_certifies_import(tmp_path: Path) -> None:
    """A tests/ dir nested inside a declared production root should certify its imports."""
    _write(
        tmp_path / "src" / "pkg" / "helpers.py",
        "def thing() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "tests" / "test_x.py",
        "from pkg.helpers import thing\n\ndef test_thing() -> None:\n    assert thing() == 1\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src"]\n',
    )
    assert ("pkg.helpers", "thing") not in _symbols(tmp_path)


def test_test_file_nested_in_production_source_root_is_not_itself_scanned(
    tmp_path: Path,
) -> None:
    """A nested test file's own definitions are not treated as production symbols."""
    _write(
        tmp_path / "src" / "pkg" / "widgets" / "registry.py",
        "class WidgetRegistry:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "widgets" / "tests" / "test_widgets.py",
        "from pkg.widgets.registry import WidgetRegistry\n\n"
        "def test_registry() -> None:\n    assert WidgetRegistry() is not None\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src"]\n',
    )
    assert ("pkg.widgets.registry", "WidgetRegistry") not in _symbols(tmp_path)
    assert "pkg.widgets.tests.test_widgets" not in {
        module.module for module in find_private_candidates(tmp_path)
    }


def test_nested_test_consumers_only_certify_what_they_import(tmp_path: Path) -> None:
    """A nested test file only certifies the symbols it actually imports."""
    _write(
        tmp_path / "src" / "pkg" / "helpers.py",
        "def used() -> int:\n    return 1\n\ndef unused() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "tests" / "test_x.py",
        "from pkg.helpers import used\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src"]\n',
    )
    symbols = _symbols(tmp_path)
    assert ("pkg.helpers", "used") not in symbols
    assert ("pkg.helpers", "unused") in symbols


def test_test_shaped_files_under_other_ignored_directories_are_not_swept(
    tmp_path: Path,
) -> None:
    """Only test-shaped files count as nested consumers, not unrelated ignored directories."""
    _write(
        tmp_path / "src" / "pkg" / "helpers.py",
        "def thing() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "src" / ".venv" / "site-packages" / "test_thing.py",
        "from pkg.helpers import thing\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src"]\n',
    )
    assert ("pkg.helpers", "thing") in _symbols(tmp_path)


def test_nested_test_consumers_certify_helper_methods(tmp_path: Path) -> None:
    """A test file nested inside a production root should certify method usage too."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def used(self) -> int:
        return 1

    def unused(self) -> int:
        return 2
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "tests" / "test_service.py",
        """
from pkg.service import Service


def test_used() -> None:
    assert Service().used() == 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src"]\n',
    )
    methods = _methods(tmp_path)
    assert ("pkg.service", "Service", "used") not in methods
    assert ("pkg.service", "Service", "unused") in methods


def test_module_name_collision_across_source_roots(tmp_path: Path) -> None:
    """Two source roots defining the same module name should be reported."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "utils.py",
        "def test_helper() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert _module_collisions(tmp_path) == {"utils": ["src/utils.py", "tests/utils.py"]}


def test_module_and_package_collision_in_same_root(tmp_path: Path) -> None:
    """A module next to a same-named package is ambiguous and should be reported."""
    _write(
        tmp_path / "src" / "pkg.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    assert _module_collisions(tmp_path) == {"pkg": ["src/pkg/__init__.py", "src/pkg.py"]}


def test_distinct_module_names_have_no_collisions(tmp_path: Path) -> None:
    """Distinct module names across roots should not be reported, and test files never count."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "helper.py",
        "def test_helper() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tests" / "utils_test.py",
        "def utils() -> int:\n    return 3\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert _module_collisions(tmp_path) == {}


def test_duplicate_source_roots_are_not_collisions(tmp_path: Path) -> None:
    """Listing the same source root twice must not report a file as colliding with itself."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "src"]\n',
    )
    assert _module_collisions(tmp_path) == {}


def test_module_collision_does_not_evict_first_source_roots_module(tmp_path: Path) -> None:
    """The first source root's module survives a name collision instead of being evicted."""
    _write(
        tmp_path / "src" / "utils.py",
        "def first_root_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "other" / "utils.py",
        "def second_root_helper() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "other"]\n',
    )

    symbols = _symbols(tmp_path)
    assert ("utils", "first_root_helper") in symbols
    assert _module_collisions(tmp_path) == {"utils": ["other/utils.py", "src/utils.py"]}


def test_print_module_collisions_handles_paths_outside_project_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A colliding path outside project_root is shown by absolute path, not a crash."""
    core = tmp_path / "core"
    sibling = tmp_path / "sibling"
    _write(
        core / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        sibling / "utils.py",
        "def sibling_helper() -> int:\n    return 2\n",
    )
    _write(
        core / "tach.toml",
        'source_roots = ["src", "../sibling"]\n',
    )

    assert cli_main([str(core)]) == 1

    output = capsys.readouterr().out
    assert "Found 1 module name defined by multiple files" in output
    assert "src/utils.py" in output
    assert sibling.resolve().as_posix() in output


def test_unparsable_file_is_reported(tmp_path: Path) -> None:
    """A file that cannot be parsed is reported with its position and reason."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "broken.py", "def oops(:\n    pass\n")

    unparsable = find_unparsable_modules(tmp_path)

    assert [(module.module, module.lineno) for module in unparsable] == [("pkg.broken", 1)]
    assert unparsable[0].path == tmp_path / "src" / "pkg" / "broken.py"
    assert unparsable[0].message


def test_parsable_project_reports_no_unparsable_files(tmp_path: Path) -> None:
    """A project that parses cleanly reports nothing."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "service.py", "def run() -> int:\n    return 1\n")

    assert find_unparsable_modules(tmp_path) == []


def test_unparsable_file_alone_fails_the_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparsable file is a finding on its own, even with nothing else to report."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "broken.py", "def oops(:\n    pass\n")

    assert cli_main([str(tmp_path)]) == 1
    assert "could not be parsed" in capsys.readouterr().out


def test_cli_reports_unparsable_files_before_other_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unparsable files are printed first, because they invalidate everything after."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def work(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef go() -> int:\n    return Service().work()\n"
        "\n\ndef oops(:\n    pass\n",
    )

    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Found 1 source file that could not be parsed" in output
    assert "src/pkg/app.py:8:" in output
    assert output.index("could not be parsed") < output.index("could be made private")


def test_cli_reports_module_collisions_before_other_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Collisions are printed as their own section ahead of symbol candidates."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "utils.py",
        "def unused() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 module name defined by multiple files" in output.out
    assert "module `utils` is defined by: src/utils.py, tests/utils.py" in output.out
    assert "public symbol that could be made private" in output.out
    assert output.out.index("defined by multiple files") < output.out.index("could be made private")


def test_module_local_public_method_is_flagged(tmp_path: Path) -> None:
    """A public method that only its own module uses should be reported."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    label = "service"

    def __init__(self) -> None:
        self.count = 0

    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1

    async def async_helper(self) -> int:
        return 2
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "helper"),
        ("pkg.service", "Service", "async_helper"),
    }


def test_method_used_from_another_module_is_not_flagged(tmp_path: Path) -> None:
    """Attribute access from another production module keeps a method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert _methods(tmp_path) == set()


def test_method_named_in_a_string_literal_elsewhere_is_not_flagged(tmp_path: Path) -> None:
    """A getattr-style string reference in another module keeps a method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        """
from pkg.service import Service


def start() -> int:
    return getattr(Service(), "run")()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_private_and_dunder_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods that are already private, and dunder methods, are never candidates."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def __init__(self) -> None:
        self.value = 1

    def __repr__(self) -> str:
        return "Service()"

    def _helper(self) -> int:
        return self.value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_methods_of_private_classes_are_not_flagged(tmp_path: Path) -> None:
    """A private class is already internal, so its method names are not reported."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class _Service:\n    def run(self) -> int:\n        return 1\n",
    )

    assert _methods(tmp_path) == set()


def test_methods_of_subclasses_are_not_flagged(tmp_path: Path) -> None:
    """A base class Privata cannot inspect may require the method name, so skip the class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from abc import ABC


class Service(ABC):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_same_named_super_method_is_not_flagged(tmp_path: Path) -> None:
    """A cooperative mixin method must keep the name used by the next MRO class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class ChatToolArgumentsCompat:
    def parse_tool_calls(self, value: object) -> object:
        parsed = super().parse_tool_calls(value)
        return parsed

    def local_helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "ChatToolArgumentsCompat", "local_helper"),
    }


def test_methods_of_classes_with_class_keywords_are_not_flagged(tmp_path: Path) -> None:
    """Class keyword arguments such as metaclass= signal an external contract."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service(metaclass=type):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_explicit_object_base_is_still_checked(tmp_path: Path) -> None:
    """An explicit object base adds no external contract."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service(object):\n    def run(self) -> int:\n        return 1\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_shadowed_object_base_is_not_checked(tmp_path: Path) -> None:
    """A name spelled object may still refer to an external base class."""
    _write(
        tmp_path / "src" / "framework.py",
        "class Base:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from framework import Base as object


class Service(object):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_rename_safe_decorators_are_still_checked(tmp_path: Path) -> None:
    """Decorators that only change binding behavior do not protect a method name."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import functools


class Service:
    @property
    def value(self) -> int:
        return 1

    @staticmethod
    def build() -> int:
        return 2

    @classmethod
    def create(cls) -> int:
        return 3

    @functools.cached_property
    def total(self) -> int:
        return 4
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "value"),
        ("pkg.service", "Service", "build"),
        ("pkg.service", "Service", "create"),
        ("pkg.service", "Service", "total"),
    }


def test_custom_decorators_with_safe_basenames_are_skipped(tmp_path: Path) -> None:
    """A trusted decorator basename does not make an unrelated decorator rename-safe."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import registry
import functools
import typing
from registry import final
from .registry import property

functools = registry
typing.final = registry.final
for functools in [registry]:
    pass


@registry.dataclass
class Registered:
    def run(self) -> int:
        return 1


class Service:
    @final
    def handle(self) -> int:
        return 2

    @property
    def value(self) -> int:
        return 3

    @functools.cache
    def cached(self) -> int:
        return 4

    @typing.final
    def finalized(self) -> int:
        return 5
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_conditionally_shadowed_safe_decorators_are_skipped(tmp_path: Path) -> None:
    """A binding inside control flow may replace a trusted decorator."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import functools

if ENABLE_CUSTOM:
    from registry import property
    functools.cache = registry.cache


class Service:
    @property
    def value(self) -> int:
        return 1

    @functools.cache
    def cached(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_star_import_shadowed_safe_decorators_are_skipped(tmp_path: Path) -> None:
    """A star import may replace both direct and module decorator bindings."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from builtins import property
from .registry import *


class Service:
    @property
    def value(self) -> int:
        return 1

    @functools.cache
    def cached(self) -> int:
        return 2
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "registry.py",
        """
__all__ = ["functools", "property"]

functools = object()
property = lambda function: function
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_class_decorator_rooted_in_a_subscript_is_unsafe(tmp_path: Path) -> None:
    """A decorator Privata cannot resolve to a dotted name keeps the class off the report."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
REGISTRIES = {"main": None}


@REGISTRIES["main"].register
class Service:
    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_import_inside_a_compound_statement_shadows_a_decorator_name(tmp_path: Path) -> None:
    """A guarded import rebinds the name, so the decorator is no longer trusted."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
try:
    import dataclasses
except ImportError:
    dataclasses = None


@dataclasses.dataclass
class Service:
    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_compound_decorator_bindings_are_conservative(tmp_path: Path) -> None:
    """Every binding form can shadow a trusted decorator name."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
async def property():
    return None


class classmethod:
    pass


staticmethod = lambda function: function

try:
    pass
except Exception as cached:
    pass

match VALUE:
    case {"decorator": property, **rest}:
        pass
    case [*items]:
        pass


class Service:
    @property
    def value(self) -> int:
        return 1

    @classmethod
    def create(cls) -> int:
        return 2

    @staticmethod
    def build() -> int:
        return 3

    def plain(self) -> int:
        return 4


class Rebound:
    def overwritten(self) -> int:
        return 5

    overwritten = 5


class Aliased:
    def run(self) -> int:
        return 6

    if ENABLE:
        def nested():
            return run

        async def async_nested():
            return run

        class Nested:
            pass

        callback = lambda: run

    alias = run
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "plain")}


def test_class_local_decorator_bindings_resolve_in_source_order(tmp_path: Path) -> None:
    """A class-local assignment only shadows later decorator expressions."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import registry


class Service:
    @property
    def builtin_value(self) -> int:
        return 1

    property = registry.property

    @property
    def registered_value(self) -> int:
        return 2

    @external
    def externally_registered(self) -> int:
        return 3

    def plain(self) -> int:
        return 4
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "builtin_value"),
        ("pkg.service", "Service", "plain"),
    }


def test_unknown_method_decorators_are_skipped(tmp_path: Path) -> None:
    """A decorator may register the method under its current name, so leave it alone."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import celery

registry = {}


class Service:
    @celery.task
    def run(self) -> int:
        return 1

    @registry["handler"]
    def handle(self) -> int:
        return 2

    @property
    def value(self) -> int:
        return 3

    @value.setter
    def value(self, new_value: int) -> None:
        self._value = new_value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_unknown_class_decorators_are_skipped(tmp_path: Path) -> None:
    """A decorated class may be registered elsewhere, while dataclasses stay checkable."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from dataclasses import dataclass

import registry


@registry.register
class Registered:
    def run(self) -> int:
        return 1


@dataclass
class Plain:
    value: int = 0

    def compute(self) -> int:
        return self.value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Plain", "compute")}


def test_exported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods of a class listed in __all__ are part of the module interface."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
__all__ = ["Service"]


class Service:
    def run(self) -> int:
        return 1


class Internal:
    def run_internal(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Internal", "run_internal")}


def test_package_reexported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """A class re-exported from a package module is part of the package interface."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        'from .service import Service\n\n__all__ = ["Service"]\n',
    )

    assert _methods(tmp_path) == set()


def test_chained_package_reexported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Each package hop preserves the defining class as part of the public interface."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "impl.py",
        "class Thing:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "sub" / "__init__.py",
        'from .impl import Thing\n\n__all__ = ["Thing"]\n',
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        'from .sub import Thing\n\n__all__ = ["Thing"]\n',
    )

    assert _methods(tmp_path) == set()


def test_facade_module_all_reexports_class_methods(tmp_path: Path) -> None:
    """A plain module that lists an imported class in __all__ publishes it."""
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "src" / "pkg" / "impl.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        'from pkg.impl import Service\n\n__all__ = ["Service"]\n',
    )

    assert _methods(tmp_path) == set()


def test_facade_module_without_all_does_not_reexport(tmp_path: Path) -> None:
    """Importing a class without naming it in __all__ is use, not publication."""
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "src" / "pkg" / "impl.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        "from pkg.impl import Service\n\nservice = Service()\n",
    )

    assert _methods(tmp_path) == {("pkg.impl", "Service", "run")}


def test_facade_module_all_omitting_the_class_does_not_reexport(tmp_path: Path) -> None:
    """An __all__ that leaves the class out does not publish it."""
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "src" / "pkg" / "impl.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        'from pkg.impl import Service\n\nbuilt = Service\n\n__all__ = ["built"]\n',
    )

    assert _methods(tmp_path) == {("pkg.impl", "Service", "run")}


def test_facade_module_star_import_reexports_named_class(tmp_path: Path) -> None:
    """A star import combined with __all__ publishes the named class."""
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "src" / "pkg" / "impl.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        'from pkg.impl import *\n\n__all__ = ["Service"]\n',
    )

    assert _methods(tmp_path) == set()


def test_package_local_imports_do_not_count_as_reexports(tmp_path: Path) -> None:
    """Function-local and TYPE_CHECKING imports do not expose a class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service


def build():
    from .service import Service

    return Service()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_package_control_flow_reexport_keeps_class_methods_public(tmp_path: Path) -> None:
    """A runtime package import inside fallback control flow remains a reexport."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
try:
    from .service import Service
except ImportError:
    from .service import Service
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_package_runtime_control_flow_reexports_are_detected(tmp_path: Path) -> None:
    """Runtime module-scope imports remain reexports across control-flow forms."""
    modules = {
        "starred": "Starred",
        "private": "Private",
        "runtime": "Runtime",
        "conditional": "Conditional",
        "fallback": "Fallback",
        "negated": "Negated",
        "iterated": "Iterated",
        "iter_else": "IterElse",
        "looped": "Looped",
        "loop_else": "LoopElse",
        "contextual": "Contextual",
        "matched": "Matched",
    }
    for module, class_name in modules.items():
        _write(
            tmp_path / "src" / "pkg" / f"{module}.py",
            f"class {class_name}:\n    def run(self) -> int:\n        return 1\n",
        )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from typing import TYPE_CHECKING

from .starred import *
from .private import Private as _Private

if not TYPE_CHECKING:
    from .runtime import Runtime

if ENABLE:
    from .conditional import Conditional
else:
    from .fallback import Fallback

if not ENABLE:
    from .negated import Negated

for item in ITEMS:
    from .iterated import Iterated
else:
    from .iter_else import IterElse

while ENABLE:
    from .looped import Looped
else:
    from .loop_else import LoopElse

with manager():
    from .contextual import Contextual

match MODE:
    case _:
        from .matched import Matched
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.private", "Private", "run")}


def test_tach_interface_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods of a class exposed through a Tach interface stay public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["src"]

[[interfaces]]
expose = ["Service"]
from = ["pkg.service"]
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_entrypoint_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """A class used as a console script target keeps its methods public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[project.scripts]\npkg = "pkg.service:Service"\n',
    )

    assert _methods(tmp_path) == set()


def test_method_ignore_comment_suppresses_finding(tmp_path: Path) -> None:
    """A # privata: ignore comment on the def line suppresses the method finding."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def kept(self) -> int:  # privata: ignore
        return 1

    def flagged(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "flagged")}


def test_methods_of_nested_classes_are_not_flagged(tmp_path: Path) -> None:
    """Only top-level classes are inspected for method privacy."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
def build() -> type:
    class Nested:
        def run(self) -> int:
            return 1

    return Nested
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_computed_getattr_keeps_all_methods_of_the_class_public(tmp_path: Path) -> None:
    """A class that dispatches by a computed name may reach any of its methods."""
    _write(
        tmp_path / "src" / "pkg" / "visitor.py",
        """
class Converter:
    def visit(self, node: object) -> object:
        return getattr(self, "visit_" + type(node).__name__)(node)

    def visit_If(self, node: object) -> int:
        return 1

    def visit_For(self, node: object) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_literal_getattr_leaves_the_class_checkable(tmp_path: Path) -> None:
    """A spelled-out getattr name is an ordinary reference, not dynamic dispatch.

    All three methods are still reported: a string literal only keeps a method
    public when another module holds it, exactly as for an attribute access.
    """
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def run(self) -> object:
        return getattr(self, "handler")

    def handler(self) -> int:
        return 1

    def helper(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "run"),
        ("pkg.service", "Service", "handler"),
        ("pkg.service", "Service", "helper"),
    }


@pytest.mark.parametrize("builtin", ["delattr", "hasattr", "setattr"])
def test_other_dynamic_attribute_builtins_are_recognised(tmp_path: Path, builtin: str) -> None:
    """Every builtin that reaches an attribute by name counts as dynamic."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        f"""
class Service:
    def run(self, name: str) -> object:
        return {builtin}(self, name, 1) if "{builtin}" == "setattr" else {builtin}(self, name)

    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_bare_getattr_call_is_not_treated_as_dynamic(tmp_path: Path) -> None:
    """A one-argument getattr cannot name an attribute, so it is not dispatch."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def run(self) -> object:
        return getattr(self)

    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "run"),
        ("pkg.service", "Service", "helper"),
    }


def test_subclassed_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """A subclass that only overrides a method keeps the base method public."""
    _write(
        tmp_path / "src" / "pkg" / "base.py",
        """
class Base:
    def run(self) -> int:
        return self.step()

    def step(self) -> int:
        raise NotImplementedError
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "child.py",
        """
from pkg.base import Base


class Child(Base):
    def step(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_subclassed_class_in_the_same_module_keeps_methods_public(tmp_path: Path) -> None:
    """An override in the defining module protects the base method too."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Base:
    def run(self) -> int:
        return self.step()

    def step(self) -> int:
        return 1


class Child(Base):
    def step(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_subscripted_base_protects_the_base_class(tmp_path: Path) -> None:
    """A generic base such as ``Base[int]`` still protects ``Base``."""
    _write(
        tmp_path / "src" / "pkg" / "base.py",
        """
class Base:
    def __class_getitem__(cls, item: object) -> type:
        return cls

    def step(self) -> int:
        return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "child.py",
        """
from pkg.base import Base


class Child(Base[int]):
    def step(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_dotted_base_protects_the_base_class(tmp_path: Path) -> None:
    """A base referenced as ``module.Base`` protects ``Base`` as well."""
    _write(
        tmp_path / "src" / "pkg" / "base.py",
        "class Base:\n    def step(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "child.py",
        """
from pkg import base


class Child(base.Base):
    def step(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_unsubclassed_class_methods_are_still_flagged(tmp_path: Path) -> None:
    """Sharing a module with an unrelated subclass does not protect a class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Other:
    def step(self) -> int:
        return 1


class Service:
    def helper(self) -> int:
        return 2


class Child(Other):
    def step(self) -> int:
        return 3
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "helper")}


def test_from_import_certifies_the_helper_module(tmp_path: Path) -> None:
    """A ``from helper import X`` form attributes the file's names to ``helper``."""
    _write(
        tmp_path / "tests" / "something.py",
        "class Helper:\n    def get_value(self) -> int:\n        return 42\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
from something import Helper


def test_value() -> None:
    assert Helper().get_value() == 42
""".strip()
        + "\n",
    )
    _write(tmp_path / "tach.toml", 'source_roots = ["tests"]\n')

    assert _methods(tmp_path) == set()


def test_helper_references_are_credited_to_every_imported_helper(tmp_path: Path) -> None:
    """Attribution is per file, not per receiver: both imported helpers are credited.

    The coarse rule can only keep a method public that a receiver-aware reading
    would have flagged, which is the safe direction for a rename suggestion.
    """
    _write(
        tmp_path / "tests" / "first.py",
        "class First:\n    def shared(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "second.py",
        "class Second:\n    def shared(self) -> int:\n        return 2\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
import first
import second


def test_value() -> None:
    assert first.First().shared() == 1
    assert second.Second() is not None
""".strip()
        + "\n",
    )
    _write(tmp_path / "tach.toml", 'source_roots = ["tests"]\n')

    assert _methods(tmp_path) == set()


def test_unresolvable_relative_import_certifies_nothing(tmp_path: Path) -> None:
    """A relative import that escapes the source root is ignored, not guessed at."""
    _write(
        tmp_path / "tests" / "orphan.py",
        "class Orphan:\n    def unused(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
from .. import orphan


def test_value() -> None:
    assert orphan.Orphan().unused() == 1
""".strip()
        + "\n",
    )
    _write(tmp_path / "tach.toml", 'source_roots = ["tests"]\n')

    assert _methods(tmp_path) == {("orphan", "Orphan", "unused")}


def test_helper_methods_are_flagged_when_no_test_file_imports_the_helper(
    tmp_path: Path,
) -> None:
    """A helper module that no test file imports certifies nothing."""
    _write(
        tmp_path / "tests" / "orphan.py",
        "class Orphan:\n    def unused(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        "def test_value() -> None:\n    assert True\n",
    )
    _write(tmp_path / "tach.toml", 'source_roots = ["tests"]\n')

    assert _methods(tmp_path) == {("orphan", "Orphan", "unused")}


def test_test_source_root_test_files_certify_helper_methods(tmp_path: Path) -> None:
    """Test files certify methods of helper modules in their own test source root."""
    _write(
        tmp_path / "tests" / "something.py",
        """
class Helper:
    def get_value(self) -> int:
        return 42

    def get_unused(self) -> int:
        return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
import something


def test_value() -> None:
    assert getattr(something.Helper(), "get_value")() == 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("something", "Helper", "get_unused")}


def test_star_imported_test_helper_methods_are_certified(tmp_path: Path) -> None:
    """Star imports retain methods that co-located tests call."""
    _write(
        tmp_path / "tests" / "something.py",
        """
class Helper:
    def get_value(self) -> int:
        return 42

    def get_unused(self) -> int:
        return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
from something import *


def test_value() -> None:
    assert Helper().get_value() == 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("something", "Helper", "get_unused")}


def test_lambda_default_uses_outer_helper_binding(tmp_path: Path) -> None:
    """Lambda defaults are evaluated before parameter bindings exist."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        "from helper import Helper\n\ncallback = lambda value=Helper.run: value\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_conditional_test_helper_import_certifies_method(tmp_path: Path) -> None:
    """A helper imported and used inside one branch is still referenced."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
def test_run(enabled: bool) -> None:
    if enabled:
        from helper import Helper

        assert Helper().run() == 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_conditional_helper_bindings_certify_all_runtime_possibilities(
    tmp_path: Path,
) -> None:
    """A post-branch call may reference either helper bound by the branches."""
    for helper in ["first_helper", "second_helper"]:
        _write(
            tmp_path / "tests" / f"{helper}.py",
            "class Helper:\n    def run(self) -> int:\n        return 1\n",
        )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
if ENABLED:
    from first_helper import Helper
else:
    from second_helper import Helper

Helper().run()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_methods_used_only_by_tests_are_flagged(tmp_path: Path) -> None:
    """Test usage does not keep a production method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_service.py",
        """
from pkg.service import Service


def test_run() -> None:
    assert Service().run()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_external_test_source_root_keeps_public_method(tmp_path: Path) -> None:
    """A sibling project's tests act as external consumers for method privacy."""
    core = tmp_path / "core"
    engine = tmp_path / "engine"
    _write(
        core / "src" / "corepkg" / "service.py",
        """
class Service:
    def run(self) -> int:
        return 1

    def helper(self) -> int:
        return 2
""".strip()
        + "\n",
    )
    _write(
        engine / "tests" / "test_service.py",
        """
from corepkg.service import Service


def test_run() -> None:
    assert Service().run() == 1
""".strip()
        + "\n",
    )
    _write(
        core / "tach.toml",
        'source_roots = ["src", "../engine/tests"]\n',
    )

    assert _methods(core) == {("corepkg.service", "Service", "helper")}


def test_relative_project_root_keeps_local_tests_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative library API calls should still classify in-project tests as local."""
    core = tmp_path / "core"
    engine = tmp_path / "engine"
    _write(
        core / "src" / "corepkg" / "api.py",
        """
def own_test_only() -> int:
    return 1

def external_test_only() -> int:
    return 2
""".strip()
        + "\n",
    )
    _write(
        core / "tests" / "test_api.py",
        """
from corepkg.api import own_test_only


def test_own() -> None:
    assert own_test_only() == 1
""".strip()
        + "\n",
    )
    _write(
        engine / "tests" / "test_api.py",
        """
from corepkg.api import external_test_only


def test_external() -> None:
    assert external_test_only() == 2
""".strip()
        + "\n",
    )
    _write(
        core / "tach.toml",
        'source_roots = ["src", "tests", "../engine/tests"]\n',
    )
    monkeypatch.chdir(tmp_path)

    symbols = _symbols(Path("core"))
    assert ("corepkg.api", "own_test_only") in symbols
    assert ("corepkg.api", "external_test_only") not in symbols


def test_method_collection_skips_modules_without_a_tree() -> None:
    """Modules that failed to parse contribute no methods and no references."""
    module = Module(name="pkg.mod", path=Path("pkg/mod.py"), package_parts=("pkg",))

    assert referenced_names_by_module(module, {}) == {}
    assert collect_method_candidates({"pkg.mod": module}) == []
    assert collect_reexports({"pkg.mod": module}) == set()


def test_cli_reports_method_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --methods, candidates are printed in their own section after symbols."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert cli_main([str(tmp_path), "--methods"]) == 1
    output = capsys.readouterr().out
    assert "Found 1 public method in 1 class that could be made private:" in output
    assert "src/pkg/service.py:1: class `Service` (1 of 2 public methods)" in output
    assert "helper:5" in output
    assert output.index("public symbol that could be made private") < output.index(
        "public method in 1 class that could be made private",
    )


def _method_only_project(tmp_path: Path) -> None:
    """Write a project whose only finding is a method that could be private."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[project.scripts]\npkg = "pkg.app:start"\n',
    )


def test_cli_omits_method_candidates_without_the_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The method check is opt-in, so a plain run passes and says nothing."""
    _method_only_project(tmp_path)

    assert cli_main([str(tmp_path)]) == 0
    assert "No module privacy issues found." in capsys.readouterr().out


def test_cli_methods_flag_turns_the_check_on(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same project fails once the method check is requested."""
    _method_only_project(tmp_path)

    assert cli_main([str(tmp_path), "--methods"]) == 1
    assert "class `Service` (1 of 2 public methods)" in capsys.readouterr().out


def test_method_findings_are_grouped_by_owning_class(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each class gets one header carrying its share of the class, then its methods."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "wide.py",
        """
class Wide:
    def kept(self) -> int:
        return 1

    def alpha(self) -> int:
        return 2

    def beta(self) -> int:
        return 3
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "narrow.py",
        """
class Narrow:
    def solo(self) -> int:
        return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        """
from pkg.narrow import Narrow
from pkg.wide import Wide


def start() -> int:
    assert Narrow() is not None
    return Wide().kept()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[project.scripts]\npkg = "pkg.app:start"\n',
    )

    assert cli_main([str(tmp_path), "--methods"]) == 1
    lines = capsys.readouterr().out.splitlines()

    assert lines[0] == "Found 3 public methods in 2 classes that could be made private:"
    assert "  src/pkg/narrow.py:1: class `Narrow` (1 of 1 public methods)" in lines
    assert "      solo:2" in lines
    assert "  src/pkg/wide.py:1: class `Wide` (2 of 3 public methods)" in lines
    assert "      alpha:5, beta:8" in lines


def test_long_method_group_wraps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A class with many findings wraps onto continuation lines rather than one long line."""
    body = "\n\n".join(
        f"    def method_number_{index}(self) -> int:\n        return {index}"
        for index in range(12)
    )
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "wide.py", f"class Wide:\n{body}\n")

    assert cli_main([str(tmp_path), "--methods"]) == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("      ")]

    assert len(lines) > 1
    assert all(len(line) <= 100 for line in lines)
    assert "method_number_0:2" in lines[0]


def test_method_carries_its_class_context(tmp_path: Path) -> None:
    """Each finding knows where its class starts and how many public methods it has."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def helper(self) -> int:
        return 1

    def other(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    found = find_method_candidates(tmp_path)

    context = [(method.name, method.class_lineno, method.class_public_methods) for method in found]

    assert context == [("helper", 1, 2), ("other", 1, 2)]


def test_find_method_candidates_ignores_the_cli_default(tmp_path: Path) -> None:
    """The library entry point still reports methods without any opt-in."""
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def helper(self) -> int:\n        return 1\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "helper")}
