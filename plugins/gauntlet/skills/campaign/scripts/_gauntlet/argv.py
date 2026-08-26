"""Argument-vector helpers shared by Gauntlet campaign scripts."""

from __future__ import annotations

import sys


def bind_separate_option_value(argv: "list[str] | None", option: str) -> "list[str]":
    """Bind a non-option argv member after ``option`` as its value.

    Campaign instructions construct selected data-bearing options as two argv members. ``argparse`` treats a
    dash-leading second member as another option, so preserve it as a separate token and let ``argparse``
    report the missing value. A legitimate dash-leading value must use the equivalent ``--option=value`` form.
    """
    source = list(sys.argv[1:] if argv is None else argv)
    bound: list[str] = []
    index = 0
    while index < len(source):
        token = source[index]
        if token == option and index + 1 < len(source) and not source[index + 1].startswith("-"):
            bound.append(f"{option}={source[index + 1]}")
            index += 2
            continue
        bound.append(token)
        index += 1
    return bound
