"""Tests for the ``privata`` Python binding surface.

The privacy-checking logic itself lives in the ``privata-core`` Rust crate
and is covered there by its own unit tests (``cargo test``). These tests
exercise only what crosses the Python/Rust boundary: the seven public
``find_*`` functions, the finding dataclasses they return, the CLI, and
``__version__``. They are deliberately black-box — a regression in
`collect_modules`-style internals would be caught by the Rust suite, not
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import privata
from privata import (
    ExportIssue,
    Method,
    ModuleCollision,
    PrivateModuleImport,
    PrivateSymbolImport,
    Symbol,
    UnparsableModule,
    find_export_issues,
    find_method_candidates,
    find_module_collisions,
    find_private_candidates,
    find_private_module_imports,
    find_private_symbol_imports,
    find_unparsable_modules,
)
from privata.cli import main as cli_main

if TYPE_CHECKING:
    import pytest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# __version__ and the public surface
# ---------------------------------------------------------------------------


def test_version_is_a_nonempty_string() -> None:
    assert isinstance(privata.__version__, str)
    assert privata.__version__


def test_public_api_exposes_exactly_the_documented_names() -> None:
    assert set(privata.__all__) == {
        "ExportIssue",
        "Method",
        "ModuleCollision",
        "PrivateModuleImport",
        "PrivateSymbolImport",
        "Symbol",
        "UnparsableModule",
        "__version__",
        "find_export_issues",
        "find_method_candidates",
        "find_module_collisions",
        "find_private_candidates",
        "find_private_module_imports",
        "find_private_symbol_imports",
        "find_unparsable_modules",
    }


# ---------------------------------------------------------------------------
# find_private_candidates
# ---------------------------------------------------------------------------


def test_find_private_candidates_flags_unused_public_symbol(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "mod.py", "def helper() -> int:\n    return 1\n")

    candidates = find_private_candidates(tmp_path)

    assert len(candidates) == 1
    symbol = candidates[0]
    assert isinstance(symbol, Symbol)
    assert symbol.name == "helper"
    assert symbol.kind == "function"
    assert symbol.module == "pkg.mod"
    assert symbol.lineno == 1
    assert Path(symbol.path) == (tmp_path / "src" / "pkg" / "mod.py").resolve()


def test_find_private_candidates_excludes_symbols_used_by_another_module(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "producer.py", "def helper() -> int:\n    return 1\n")
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        "from pkg.producer import helper\n\ndef run() -> int:\n    return helper()\n",
    )

    names = {s.name for s in find_private_candidates(tmp_path)}
    assert "helper" not in names


def test_find_private_candidates_respects_dunder_all(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        '__all__ = ["helper"]\n\n\ndef helper() -> int:\n    return 1\n',
    )
    assert find_private_candidates(tmp_path) == []


# ---------------------------------------------------------------------------
# find_method_candidates
# ---------------------------------------------------------------------------


def test_find_method_candidates_flags_unused_public_method(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        "class Service:\n    def helper(self) -> int:\n        return 1\n",
    )

    methods = find_method_candidates(tmp_path)

    assert len(methods) == 1
    method = methods[0]
    assert isinstance(method, Method)
    assert method.name == "helper"
    assert method.class_name == "Service"
    assert method.class_public_methods == 1
    assert method.module == "pkg.mod"


def test_find_method_candidates_excludes_methods_used_elsewhere(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def helper(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        "from pkg.service import Service\n\n"
        "def run(svc: Service) -> int:\n    return svc.helper()\n",
    )

    names = {m.name for m in find_method_candidates(tmp_path)}
    assert "helper" not in names


# ---------------------------------------------------------------------------
# private module and private symbol imports
# ---------------------------------------------------------------------------


def test_find_private_module_imports_flags_cross_package_import(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "one" / "_internal.py", "VALUE = 1\n")
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        "from pkg.one import _internal\n\nVALUE = _internal.VALUE\n",
    )

    imports = find_private_module_imports(tmp_path)

    assert len(imports) == 1
    finding = imports[0]
    assert isinstance(finding, PrivateModuleImport)
    assert finding.module == "pkg.one._internal"
    assert finding.imported_by == "pkg.two.public"


def test_find_private_module_imports_allows_import_within_same_package(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "_internal.py", "def helper() -> int:\n    return 1\n")
    _write(tmp_path / "src" / "pkg" / "public.py", "from pkg._internal import helper\n")
    assert find_private_module_imports(tmp_path) == []


def test_find_private_symbol_imports_flags_private_symbol_crossing_modules(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src" / "pkg" / "producer.py", "class _PrivateService:\n    pass\n")
    _write(tmp_path / "src" / "pkg" / "consumer.py", "from .producer import _PrivateService\n")

    imports = find_private_symbol_imports(tmp_path)

    assert len(imports) == 1
    finding = imports[0]
    assert isinstance(finding, PrivateSymbolImport)
    assert finding.module == "pkg.producer"
    assert finding.name == "_PrivateService"
    assert finding.imported_by == "pkg.consumer"


# ---------------------------------------------------------------------------
# find_export_issues
# ---------------------------------------------------------------------------


def test_find_export_issues_flags_missing_and_unknown_names(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        '__all__ = ["missing_name"]\n\n\ndef helper() -> int:\n    return 1\n',
    )

    issues = find_export_issues(tmp_path)
    kinds_by_name = {issue.name: issue.kind for issue in issues}

    assert isinstance(issues[0], ExportIssue)
    assert kinds_by_name["missing_name"] == "unknown"
    assert kinds_by_name["helper"] == "missing"


# ---------------------------------------------------------------------------
# find_unparsable_modules / find_module_collisions
# ---------------------------------------------------------------------------


def test_find_unparsable_modules_reports_syntax_errors(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "broken.py", "def oops(:\n    pass\n")

    unparsable = find_unparsable_modules(tmp_path)

    assert len(unparsable) == 1
    finding = unparsable[0]
    assert isinstance(finding, UnparsableModule)
    assert finding.module == "pkg.broken"
    assert finding.lineno == 1
    assert finding.message


def test_find_module_collisions_reports_duplicate_module_names(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "utils.py", "")
    _write(tmp_path / "lib" / "utils.py", "")
    _write(tmp_path / "tach.toml", 'source_roots = ["src", "lib"]\n')

    collisions = find_module_collisions(tmp_path)

    assert len(collisions) == 1
    collision = collisions[0]
    assert isinstance(collision, ModuleCollision)
    assert collision.module == "utils"
    assert len(collision.paths) == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_reports_no_issues_on_a_clean_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "src" / "pkg" / "producer.py", "def helper() -> int:\n    return 1\n")
    _write(
        tmp_path / "src" / "pkg" / "__main__.py",
        "from pkg.producer import helper\n\nhelper()\n",
    )

    assert cli_main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == "No module privacy issues found.\n"


def test_cli_reports_private_symbol_imports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "src" / "pkg" / "write_coordinator.py",
        "class _EventCacheWriteCoordinator:\n    pass\n\n\ndef local_helper() -> None:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "runtime_support.py",
        "from pkg.write_coordinator import _EventCacheWriteCoordinator\n",
    )

    assert cli_main([str(tmp_path)]) == 1

    output = capsys.readouterr().out
    assert "Found 1 private symbol import from production modules:" in output
    assert (
        "src/pkg/runtime_support.py:1: imports private symbol "
        "`pkg.write_coordinator._EventCacheWriteCoordinator`"
    ) in output
    assert "function `local_helper`" in output


def test_cli_methods_flag_is_off_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def helper(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__main__.py",
        "from pkg.service import Service\n\nService()\n",
    )

    assert cli_main([str(tmp_path)]) == 0
    assert "helper" not in capsys.readouterr().out

    assert cli_main([str(tmp_path), "--methods"]) == 1
    assert "helper" in capsys.readouterr().out


def test_cli_defaults_project_root_to_current_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "src" / "pkg" / "mod.py", "def helper() -> int:\n    return 1\n")
    monkeypatch.chdir(tmp_path)

    assert cli_main([]) == 1
    assert "function `helper`" in capsys.readouterr().out
