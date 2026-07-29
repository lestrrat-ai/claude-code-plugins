# ci: pyright
"""Narrow helpers shared by Gauntlet's in-process CLI tests."""

from __future__ import annotations

import inspect
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Callable, Generator, Sequence

from _gauntlet.modules import load_module_from_path

# One fixture row: a short name, the rule it pins, and the callable that proves it.
Case = tuple[str, str, Callable[..., object]]


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


# --- the shared fixture runner ------------------------------------------------
#
# A `self-test` subcommand is an EXIT CODE that CI and the review gate both trust, so the one thing this
# runner must never do is return 0 over a suite that proved nothing. It returns 1 for a missing fixture
# file, a fixture file that will not load, an absent or empty `CASES`, a fixture that raises the suite's
# own failure type, a fixture that raises anything else, a fixture that EXITS the interpreter instead of
# raising, and a fixture whose body cannot run because of how it was declared. Every one of those already
# failed in at least one of the hand-copied loops this replaces — the runner adds no guarantee that was
# not already being made somewhere in this directory.
#
# `self-test-runner-test.py` is that claim EXECUTED. A runner is exactly the kind of code whose bugs are
# invisible in a green run: one that swallowed a failing fixture, miscounted, or returned 0 over an empty
# case table would turn every suite in this directory green at once, and every one of them would still
# LOOK green, so their output cannot be the evidence.
#
# `KeyboardInterrupt` is deliberately not caught. It is not a fixture's verdict, it is the operator's:
# Ctrl-C must ABORT the run, not be filed as one failed check while the remaining fixtures carry on. That
# is why the catches below name `SystemExit` and `Exception`, never `BaseException`.
#
# What stays the CALLER's, because each tool names its own contract: the SUBJECT ("the lease's contract"),
# the failure type its fixtures raise, the name-column width, and whether each fixture is handed a fresh
# working directory. What is now shared: the load, the guards, the loop, the per-case reporting, and the
# verdict.

_MISSING = ("the fixture file {path} IS MISSING — this suite has no fixtures to run and CANNOT report "
            "health. Every rule this file enforces is now unpinned.")
_UNLOADABLE = "{path} exists but cannot be loaded as a module"
_NO_CASES = ("{path} exports no CASES — every rule in this file is unpinned while the suite still "
             "exits 0")
_EMPTY = ("the suite holds NO fixtures — a self-test with no subject passes every time, so an empty case "
          "table is a FAILURE, never an all-clear")
_NEVER_RAN = ("the fixture is {kind}, so CALLING it only builds an object and runs no body at all — this "
              "check proved NOTHING while reporting a pass. A fixture must do its work when it is "
              "called: never `yield` from it, never declare it `async`.")
_EXITED = ("raised SystemExit({code!r}) — a fixture must RAISE to fail, never exit the interpreter: "
           "unhandled, this would have ended the suite here, skipping every fixture after it")

_SIBLING_NAME = "sibling-fixtures"


@contextmanager
def _work_root(per_case_dir: bool) -> "Generator[Path | None, None, None]":
    """A temporary root for per-fixture working directories, or ``None`` when fixtures take no argument."""
    if not per_case_dir:
        yield None
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _sole_failure(name: str, rule: str, detail: str, *, subject: str, width: int) -> int:
    """Report ONE failed check that stands for the whole suite, and return 1."""
    print(f"FAIL     {name:{width}} -> {rule}\n         {detail}")
    print(f"\n1 check(s) FAILED — {subject} is broken.")
    return 1


def _never_ran(fn: "Callable[..., object]") -> "str | None":
    """Why calling ``fn`` would run no body at all, or ``None`` when it is an ordinary function.

    Three DECLARATIONS make a call build an object instead of executing the body: a generator function
    (its body contains ``yield``), a coroutine function (it is ``async def``) and an async generator
    (both at once). A fixture written any of those ways passes every time while proving nothing — an
    un-awaited coroutine surfaces only as a ``RuntimeWarning`` that no exit code reflects.

    This classifies the FUNCTION, never what a call returned, and that distinction is the whole point.
    A fixture that runs to completion and then returns a generator — ``return (row for row in rows)`` —
    is an ordinary fixture that PASSED; judging it by its return value fails it with a message asserting
    its body never executed, which is a false failure stating a false reason. ``inspect`` reads the code
    object's own flags, so it cannot make that mistake. Do not "simplify" this to a test on the result.
    """
    if inspect.isasyncgenfunction(fn):
        return _NEVER_RAN.format(kind="an async generator function (`async def` with `yield`)")
    if inspect.iscoroutinefunction(fn):
        return _NEVER_RAN.format(kind="a coroutine function (`async def`)")
    if inspect.isgeneratorfunction(fn):
        return _NEVER_RAN.format(kind="a generator function (its body contains `yield`)")
    return None


def run_cases(
    cases: "Sequence[Case]",
    *,
    failure: "type[BaseException]",
    subject: str,
    width: int = 30,
    per_case_dir: bool = False,
) -> int:
    """Run every ``(name, rule, fn)`` row; return 0 iff all of them passed AND there was at least one.

    ``failure`` is the suite's own assertion type: it is reported as the fixture's own message, while
    anything else is reported with its type name, because a fixture that CRASHES has not passed either
    way. ``SystemExit`` is caught with the rest and named specifically: a fixture that exits the
    interpreter would otherwise end the suite mid-list, and ``SystemExit(0)`` would end it GREEN.

    ``subject`` names the contract in the verdict line. ``per_case_dir`` hands each fixture a fresh empty
    directory under one temporary root, numbered and named for the fixture so two rows sharing a name
    still get their own.
    """
    rows = list(cases)
    if not rows:
        return _sole_failure("empty-case-table", "a suite must hold at least one fixture", _EMPTY,
                             subject=subject, width=width)
    failures = 0
    with _work_root(per_case_dir) as root:
        for index, (name, rule, fn) in enumerate(rows):
            never_ran = _never_ran(fn)
            if never_ran is not None:
                print(f"FAIL     {name:{width}} -> {rule}\n         {never_ran}")
                failures += 1
                continue
            try:
                if root is None:
                    fn()
                else:
                    work = root / f"{index:02d}-{name}"
                    work.mkdir(parents=True)
                    fn(work)
            except failure as exc:
                print(f"FAIL     {name:{width}} -> {rule}\n         {exc}")
                failures += 1
            except SystemExit as exc:  # a BaseException: it would otherwise take the whole suite with it
                print(f"FAIL     {name:{width}} -> {rule}\n         {_EXITED.format(code=exc.code)}")
                failures += 1
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                print(f"FAIL     {name:{width}} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"ok       {name:{width}} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — {subject} is broken.")
        return 1
    print(f"all {len(rows)} fixtures hold — {subject} is intact.")
    return 0


def run_sibling_suite(
    sibling: Path,
    module_name: str,
    *,
    failure: "type[BaseException]",
    subject: str,
    width: int = 30,
    per_case_dir: bool = False,
) -> int:
    """Load ``sibling``'s ``CASES`` and run them. Return 0 iff every rule the caller claims actually holds.

    A missing file, one that will not load, and an absent or empty ``CASES`` are each reported as ONE
    failed check and return 1 — a self-test that reports success because it found nothing to check
    certifies a contract that nothing checked.

    An exception raised while EXECUTING the fixture file propagates untouched, exactly as every
    hand-rolled loader here did: a fixture module that cannot even import is a broken install, and its
    own traceback says more than any message this could print.
    """
    rule = f"the fixtures in {sibling.name} must be RUNNABLE"
    if not sibling.exists():
        return _sole_failure(_SIBLING_NAME, rule, _MISSING.format(path=sibling),
                             subject=subject, width=width)
    module = load_module_from_path(module_name, sibling, register=True)
    if module is None:
        return _sole_failure(_SIBLING_NAME, rule, _UNLOADABLE.format(path=sibling),
                             subject=subject, width=width)
    cases = getattr(module, "CASES", None)
    if not cases:
        return _sole_failure(_SIBLING_NAME, rule, _NO_CASES.format(path=sibling),
                             subject=subject, width=width)
    return run_cases(cases, failure=failure, subject=subject, width=width, per_case_dir=per_case_dir)


def checker(failure: "type[BaseException]") -> "Callable[[bool, str], None]":
    """A ``check(cond, msg)`` that raises ``failure(msg)`` when ``cond`` is false.

    Every sibling suite in this directory needs exactly this, bound to the failure type its own accessor
    exports, and each one used to declare its own copy. The runner reports ``failure`` as the fixture's
    own message, so binding the two together here is what keeps a fixture's stated reason intact.
    """
    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise failure(msg)
    return check
