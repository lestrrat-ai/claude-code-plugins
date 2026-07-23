# ci: pyright
"""Shared mechanics for Gauntlet's source-mutation test harnesses."""

from __future__ import annotations

import ast
import re
import types
from collections.abc import Callable, Collection
from pathlib import Path


MarkedStatements = dict[str, tuple[str, ast.stmt]]
ErrorFactory = Callable[[str], Exception]

_MARKER_RE = re.compile(
    r"^(?P<indent>[ ]*)# MUTATE:(?P<rule>[a-z0-9-]+):(?P<weakening>.+?)\s*$"
)


def marked_statements(
    source: str,
    *,
    error_factory: ErrorFactory,
    no_markers_message: str,
) -> MarkedStatements:
    """Bind each mutation marker to the source statement directly below it.

    ``error_factory`` and ``no_markers_message`` keep each harness's existing
    exception type and diagnostic while sharing the parsing and AST binding.
    """
    markers = []
    for n, line in enumerate(source.splitlines(), 1):
        match = _MARKER_RE.match(line)
        if match:
            markers.append((match.group("rule"), match.group("weakening"), n))

    tree = ast.parse(source)
    statements = {node.lineno: node for node in ast.walk(tree) if isinstance(node, ast.stmt)}
    marked: MarkedStatements = {}
    for rule, weakening, line in markers:
        statement = statements.get(line + 1)
        if statement is None:
            raise error_factory(f"# MUTATE:{rule} on line {line} sits above no statement")
        if rule in marked:
            raise error_factory(f"duplicate rule id {rule!r} — every rule is marked exactly once")
        marked[rule] = (weakening, statement)
    if not marked:
        raise error_factory(no_markers_message)
    return marked


def _bare_name(node: ast.expr | None) -> str | None:
    """Return an expression's identifier only when the expression is a bare name."""
    return node.id if isinstance(node, ast.Name) else None


def unmarked_enforcements(
    source: str,
    marked: MarkedStatements,
    *,
    rule_functions: Collection[str],
    enforcing_exceptions: Collection[str],
    enforcing_verdicts: Collection[str],
    source_name: str,
) -> list[str]:
    """Find enforcing raises and tuple returns without a mutation marker.

    The caller owns which functions enforce its contract and which exception
    and verdict names reject input. This function owns only the common AST
    walk and the shared diagnostic shape.
    """
    marked_lines = {statement.lineno for _, statement in marked.values()}
    problems = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef) or function.name not in rule_functions:
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Raise):
                enforcing = (
                    isinstance(node.exc, ast.Call)
                    and _bare_name(node.exc.func) in enforcing_exceptions
                )
                what = "raise"
            elif isinstance(node, ast.Return):
                value = node.value
                enforcing = (
                    isinstance(value, ast.Tuple)
                    and bool(value.elts)
                    and _bare_name(value.elts[0]) in enforcing_verdicts
                )
                what = "return"
            else:
                continue
            if enforcing and node.lineno not in marked_lines:
                problems.append(
                    f"{source_name}:{node.lineno}: {function.name}() enforces a rule ({what}) with NO "
                    f"# MUTATE marker — an unmarked rule is never mutated, so nothing can report it unpinned"
                )
    return problems


def mutate_source(source: str, rule: str, weakening: str, statement: ast.stmt) -> str:
    """Replace one marked statement with its weakening at the same indentation."""
    lines = source.splitlines()
    body = [f"{' ' * statement.col_offset}{weakening}  # MUTANT:{rule}"]
    return "\n".join(
        lines[: statement.lineno - 1] + body + lines[statement.end_lineno :]
    ) + "\n"


def load_source_module(source: str, name: str, origin: Path) -> types.ModuleType:
    """Execute source as a non-main module with its original file path."""
    module = types.ModuleType(name)
    module.__file__ = str(origin)
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)  # noqa: S102 - this is the harness's job
    return module
