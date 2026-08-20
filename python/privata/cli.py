"""Command-line interface for Privata."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from privata._privata import check_project

if TYPE_CHECKING:
    from collections.abc import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privata",
        description="Check a Python project for module privacy issues.",
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=Path.cwd(),
        metavar="project-root",
        type=Path,
        help="Project root to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--methods",
        action="store_true",
        help=(
            "Also report public methods that no other production module refers to. "
            "Off by default: attribute access is dynamic, so this check cannot see "
            "every caller."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Privata module privacy checker."""
    args = _build_parser().parse_args(argv)
    # The scan runs in Rust; printing happens here so pytest's capsys (which
    # patches sys.stdout, not the OS file descriptor) can see CLI output.
    text, exit_code = check_project(str(args.project_root), args.methods)
    print(text, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
