# ci: pyright
"""The field-shape check every Gauntlet campaign decider applies to a `gh pr view` payload."""

from __future__ import annotations

from typing import Iterable


def field_problem(view: object, *, strings: "Iterable[str]" = (),
                  bools: "Iterable[str]" = ()) -> "str | None":
    """`None` if `view` is a JSON object carrying every named field at the named JSON type; otherwise a
    short description of the FIRST thing wrong. PURE — no I/O, no raising.

    Three deciders read a `gh pr view` payload and refuse a malformed one: `base-preflight.py` and
    `merge-check.py` turn the result into a fail-closed verdict, `merge.py` into a `Refusal`. Each one
    checked its own field list against the same three failures, so this owns the CHECK and its WORDING
    while each caller keeps its own field list and its own way of refusing.

    `strings` and `bools` are ORDERED and checked in that order, strings first. The order is part of the
    contract, not an accident of iteration: the caller decides which malformation a user hears about when
    a payload has several, and a caller that reorders its own list changes only which of its own messages
    comes first.

    Missing is reported SEPARATELY from wrong-typed. They are different repairs — one payload lacks a
    field the tool asked `gh` for, the other carries it at a type no `gh pr view` produces — and a single
    message for both leaves the reader unable to tell which happened.
    """
    if not isinstance(view, dict):
        return f"view is not a JSON object (got {type(view).__name__})"
    for name in strings:
        if name not in view:
            return f"missing field {name!r}"
        # bool is a subclass of int, not str, so a JSON string is the only thing that passes here.
        if not isinstance(view[name], str):
            return f"field {name!r} must be a string, got {type(view[name]).__name__}"
    for name in bools:
        if name not in view:
            return f"missing field {name!r}"
        # The reverse of the note above: `isinstance(True, int)` holds, so a bool check must come from
        # `bool` itself. A JSON string that reads like a bool ("false") is the case that reaches here.
        if not isinstance(view[name], bool):
            return f"field {name!r} must be a bool, got {type(view[name]).__name__}"
    return None
