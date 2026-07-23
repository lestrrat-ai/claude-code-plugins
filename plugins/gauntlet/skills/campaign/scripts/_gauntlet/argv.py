# ci: pyright
"""Argument-vector helpers shared by Gauntlet campaign scripts."""

from __future__ import annotations

import sys


def bind_separate_option_value(argv: "list[str] | None", option: str) -> "list[str]":
    """Bind the argv member after ``option`` as its value, even when it begins with ``-``.

    Campaign instructions construct selected data-bearing options as two argv members. ``argparse`` treats a
    dash-leading second member as another option, so join only the named option and its immediate value into
    the equivalent ``--option=value`` form before parsing. Leave a trailing option unchanged so ``argparse``
    still reports its missing value.
    """
    source = list(sys.argv[1:] if argv is None else argv)
    bound: list[str] = []
    index = 0
    while index < len(source):
        token = source[index]
        if token == option and index + 1 < len(source):
            bound.append(f"{option}={source[index + 1]}")
            index += 2
            continue
        bound.append(token)
        index += 1
    return bound
