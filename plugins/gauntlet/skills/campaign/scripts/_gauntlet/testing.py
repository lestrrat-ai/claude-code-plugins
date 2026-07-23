# ci: pyright
"""Narrow helpers shared by Gauntlet's in-process CLI tests."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Callable


def capture_cli(main: "Callable[[list[str]], int]", argv: "list[str]") -> "tuple[int, str, str]":
    """Run ``main`` in-process and return its exit code, stdout, and stderr.

    Integer ``SystemExit`` codes are preserved. Non-integer codes match command-line failure behavior by
    becoming exit code 1.
    """
    out, err = StringIO(), StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()
