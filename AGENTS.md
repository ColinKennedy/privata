# Repository Guidelines

Privata is a small Python module privacy checker. The privacy-checking logic
lives in Rust (`crates/privata-core`), exposed to Python through a PyO3
extension (`crates/privata-python`, built with maturin as `privata._privata`).
A thin Python package (`python/privata`) wraps the extension with the public
API and the CLI's `argparse` layer.

## Development

- Use `uv sync --extra dev --group docs` for a full development environment.
  `uv sync` (and `uv run`) invoke maturin as the build backend, so a Rust
  toolchain must be installed.
- Run `cargo test --workspace` for the core checker logic — this is the
  primary test suite and is where nearly all behavior coverage lives.
- Run `uv run pytest` for the Python binding surface: the seven public
  `find_*` functions, the finding dataclasses, `__version__`, and the CLI.
  These tests are deliberately black-box; a logic regression should show up
  in `cargo test`, not here.
- Run `cargo clippy --workspace --all-targets -- -D warnings` and
  `cargo fmt --all -- --check` before submitting Rust changes.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  and `uv run ty check` before submitting Python changes.
- Use `uv build` (or `uv run maturin build --release`) to verify packaging.
- After changing Rust code, run `uv run maturin develop` to rebuild the
  extension in place before running `uv run pytest`.

## Structure

- `crates/privata-core/src/` contains the checker implementation, organized
  the same way the original Python modules were (`modules.rs`, `imports.rs`,
  `exports.rs`, `entrypoints.rs`, `methods/`, `checker.rs`), plus each
  module's own `#[cfg(test)]` unit tests.
- `crates/privata-python/src/lib.rs` contains the PyO3 bindings. Only the
  project-root-driven `find_*` functions are exposed — the lower-level
  `collect_*` helpers that pass `Module` objects (each carrying a parsed
  AST) between calls stay internal to Rust, since there's no practical way
  to hand a parsed AST across the Python/Rust boundary.
- `python/privata/__init__.py` re-exports the bound functions and types.
  `python/privata/cli.py` is the `argparse` entry point; it calls into Rust
  for the scan and prints the returned report text itself (not from Rust),
  so pytest's `capsys` — which patches `sys.stdout`, not the OS file
  descriptor a native extension would write to — can still capture CLI
  output in tests.
- `tests/test_checker.py` exercises only the Python binding surface.
- `docs/` contains the Zensical documentation site.

## Style

- Preserve the rule that test imports do not count when deciding whether a
  symbol should remain public.
- When porting or changing checker behavior, update both the Rust
  implementation and its co-located unit tests in the same module file.
- Keep the Python layer thin: argument parsing, printing, and re-exports
  only. New checking logic belongs in `privata-core`.
