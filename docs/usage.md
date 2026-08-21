---
icon: lucide/terminal
---

# Usage

## Command

```bash
privata <project-root>
```

The command scans production Python source roots under `<project-root>`.
It uses `tach.toml` `source_roots` when present, otherwise prefers `src/`, otherwise scans the project root while ignoring tests, virtualenvs, build output, docs output, and hidden tooling directories.

`source_roots` is both what gets searched and what gets reported. To search
a wider tree (so cross-references outside `source_roots` still count as
usage) without reporting on it, add `tach.toml` `privata_search_paths`:

```toml
source_roots = ["python/some/subfolder"]
privata_search_paths = ["python"]
```

See [Adopting Privata Incrementally](getting-started.md#adopting-privata-incrementally).

## Pre-Commit

Add Privata to another repository's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.8.0
    hooks:
      - id: privata
```

The hook runs `privata .` once per commit, so it checks the repository as a whole instead of only the changed files.
Run `pre-commit autoupdate` to move `rev` to the newest release.

For a less strict setup, use the manual hook:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.8.0
    hooks:
      - id: privata-manual
```

Then run it on demand:

```bash
pre-commit run --hook-stage manual privata-manual --all-files
```

## Public Symbols

Privata reports top-level public symbols that are not imported from another production module:

```python
def helper() -> int:
    return 1
```

If `helper` is only used inside its own module, Privata reports it as a candidate for `_helper`.

## Public Methods

Privata can also report public methods that no other production module refers to.
This check is **off by default**; pass `--methods` to turn it on:

```bash
privata . --methods
```

It is opt-in because attribute access in Python is dynamic, so the check cannot see every caller, and on a large codebase it reports far more than the other checks.
Turn it on once you have decided the extra noise is worth it for a given project.
To enable it in the pre-commit hook above, add `args: [--methods]` to the hook entry.

The library entry point `find_method_candidates` is unaffected by the flag and always reports.

```python
class Service:
    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1
```

If another module calls `Service().run()` but nothing outside `service.py` mentions `helper`, Privata reports `helper` as a candidate for `_helper`.

Findings are grouped by the class that owns them, and each group leads with how much of the class it covers:

```text
Found 13 public methods in 2 classes that could be made private:

  src/pkg/tracker.py:64: class `DefinedVariableTracker` (12 of 14 public methods)
      is_defined:71, record_definition:78, next_branch:85, skip_branch:92
  src/pkg/messages.py:40: class `MessageBuilder` (1 of 143 public methods)
      are_type_names_disabled:207
```

The `n of m` count separates two different findings. Twelve of fourteen is one question about the class: is it internal to its module? One of a hundred and forty-three is a single helper that leaked.
A method counts as used when another production module accesses the name as an attribute, such as `service.helper()`, or names it in a string literal, such as `getattr(service, "helper")`.
As with top-level symbols, use inside the defining module does not keep a method public, and use from tests does not either.
Matching is intentionally name-based rather than receiver-aware, so an unrelated attribute with the same name in another module conservatively suppresses the report.

The name of a method can belong to something Privata cannot see, such as a framework base class or a registry, so the check only inspects plain classes and undecorated methods.
Privata skips:

- classes with a base class other than `object`, since the base may define the method contract
- classes that another class in the project subclasses, since a subclass may override the method and renaming the base method would strand that override under its old name
- classes with class keywords such as `metaclass=`
- classes with decorators other than `@dataclass` and `@final`
- private classes, classes listed in `__all__`, classes re-exported by a package `__init__.py` or named in another module's `__all__`, and classes exposed through entry points or a Tach interface
- classes nested inside functions or other classes
- dunder methods and methods that are already private
- methods carrying any decorator other than `@property`, `@staticmethod`, `@classmethod`, `@cached_property`, `@cache`, `@lru_cache`, and `@final`
- methods that call the same method through `super()`, since cooperative mixins must preserve that name

The decorator rule is what keeps route handlers, Pydantic validators, pytest fixtures, and Celery tasks out of the report: those methods are registered under their current name by a decorator.

### Dispatch Privata cannot see

The computed-name rule covers `getattr`, `setattr`, `hasattr`, and `delattr` called with a name that is not a string literal, which is the common visitor and handler shape:

```python
class Converter:
    def visit(self, node):
        return getattr(self, "visit_" + type(node).__name__)(node)
```

Every method of `Converter` is left alone, because the dispatch could reach any of them.

Dispatch that does not go through those builtins is **not supported**, and Privata will report false positives for it.
That includes a table of bound methods assembled in another module, a method name forwarded through `**kwargs`, `operator.attrgetter`, and anything reached through `eval` or `globals()`.
There is no reliable way to see those statically, so Privata does not try.
Suppress such a method with `__all__`, a Tach interface entry, or `# privata: ignore`.

## Private Module Imports

Private modules are modules whose dotted path contains a private segment:

```text
package._internal
package.feature._runtime
```

Those modules can be imported from inside their owning package subtree.
Imports from outside that subtree are reported.

## Private Symbol Imports

Private top-level symbols are functions, classes, variables, or type aliases whose names begin with a single underscore:

```python
class _RuntimeService:
    pass
```

Imports of those names from another production module are reported.
Tests are ignored, so tests can still import internals without making them public.

## What Counts As Public Use

The following keep a symbol public:

- another module under a production source root imports the symbol, or refers to the method name
- a package `__init__.py` re-exports the symbol, or a facade module imports it and names it in `__all__`
- a literal `__all__` includes the symbol
- `pyproject.toml` lists the symbol as a console or GUI script entry point
- a shell script or Dockerfile launches the symbol as a Uvicorn app
- `tach.toml` exposes the symbol through a `[[interfaces]]` entry

Imports from tests do not count.

## Export Validation

When a module declares a literal `__all__`, Privata validates that it is exact.
Names listed in `__all__` must be bound by the module.
Public top-level bindings must be listed in `__all__`.
Use underscore-prefixed imports or helpers for implementation details that should not be exported.

## Suppressing Findings

Add a `# privata: ignore` comment to any import line to suppress that specific finding:

```python
from pkg._internal import helper  # privata: ignore
import pkg._internal  # privata: ignore
from pkg.impl import _Service  # privata: ignore
```

The same comment works on a `def` line to keep a public method out of the report:

```python
class Service:
    def run(self) -> int:  # privata: ignore
        return 1
```

The comment suppresses only the finding on that line. Other issues in the same file are still reported.

In multi-line imports, each imported name is reported on its own line, so put the comment on the line of the name you want to suppress.
A comment on the `from ...` header line only suppresses findings about the imported module itself, not the names inside the parentheses:

```python
from pkg.impl import (
    _Service,  # privata: ignore
    _Helper,  # still reported
)
```

## Framework Exceptions

Privata skips common framework-owned names:

- FastAPI route handlers and related request/response models
- Typer command callbacks
- framework app/router objects created with `FastAPI`, `APIRouter`, or `Typer`
- module-level `logger`

These names are often public by framework convention even when they are not imported from another production module.
