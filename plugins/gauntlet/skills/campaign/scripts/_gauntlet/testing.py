"""Narrow helpers shared by Gauntlet's in-process CLI tests."""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Callable, ContextManager, Generator, Sequence

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


# How deep the nesting below goes. `json.loads` recurses once per level, but the depth at which it gives
# up is NOT `sys.getrecursionlimit()`: the C scanner is bounded by the interpreter's stack headroom, so the
# real threshold moves with the Python build and the platform. A fixture that sat near it would pass on one
# machine and not the next, so this is deliberately far past any of them rather than tuned to one.
_NESTING_DEPTH = 100_000


def deeply_nested_json() -> bytes:
    """A well-formed JSON array nested far past any parse's recursion limit, as the RAW BYTES of a response.

    This is the input that makes `json.loads` raise `RecursionError`, which is a `RuntimeError` and NOT a
    `ValueError` — so it lands in a DIFFERENT branch of the exception tree from a syntax error or a decode
    error, and any reader that catches by naming types will miss it unless it named this one too.

    It is the shared input for the suites that drive `_gauntlet/gh.py`'s two parses, which want a WHOLE
    response. It is deliberately NOT the tree's only deep-JSON fixture: the JSONL readers pin the same
    escape with a deep LINE, in inputs their own suites own, and merging the two would be merging inputs
    of different shapes for different parsers rather than removing a duplicate.
    """
    return b"[" * _NESTING_DEPTH + b"]" * _NESTING_DEPTH


# Python 3.11 and later refuse to convert an integer STRING longer than this many digits, so a JSON document
# holding a longer integer literal is well-formed and still unparseable. The limit is CPython's documented
# `sys.get_int_max_str_digits()` default; the payload below clears it by a wide margin rather than sitting on
# it, so this fixture does not become a test of one interpreter's threshold.
_OVERSIZED_INT_DIGITS = 5000


def oversized_int_json() -> bytes:
    """A well-formed JSON object whose integer literal is too long for CPython to convert, as RAW BYTES.

    This is the input that makes `json.loads` raise a PLAIN `ValueError` — not a `json.JSONDecodeError`, not
    a `UnicodeDecodeError`, but their shared BASE. A reader that catches the two subclasses by name lets this
    one straight through, which is exactly how it was found.
    """
    return b'{"n": ' + b"9" * _OVERSIZED_INT_DIGITS + b"}"


def hostile_json_responses() -> "list[tuple[str, bytes]]":
    """Every `(name, raw bytes)` row a `gh pr view` reader must survive, as a TABLE rather than a member list.

    THIS IS THE CHECK THAT CAN FAIL FOR AN INPUT NOBODY NAMED. The per-exception fixtures beside it pin which
    input produces which message, and that is worth having — but each one is written around an exception type
    that was already known, so a family member discovered tomorrow cannot make any of them go red. A caller
    drives this whole table through its real CLI and asserts the same property for every row: a structured
    verdict on stdout, no traceback on stderr. ADD A ROW HERE when a new hostile input turns up; that is the
    one edit that makes every suite using the table cover it at once.

    The rows are deliberately spread across the exception tree — a syntax error, a decode failure before the
    parse is even reached, a conversion limit inside a well-formed document, and a recursion limit — so that
    a reader narrowed back to catching a single branch fails at least one row.
    """
    return [
        ("bad-syntax", b"{not json at all"),
        ("truncated", b'{"mergeable": "MERGEA'),
        ("undecodable-bytes", b'{"mergeable": "MERGEABLE", "x": "\xff"}'),
        ("oversized-int", oversized_int_json()),
        ("deep-nesting", deeply_nested_json()),
    ]


@contextmanager
def gh_writing(stdout: bytes, *, stderr: bytes = b"", exit_code: int = 0) -> "Generator[None, None, None]":
    """Put a fake ``gh`` FIRST on ``PATH`` that writes exactly ``stdout`` (and ``stderr``) as RAW BYTES,
    then exits.

    A str-typed stub over ``subprocess.run`` cannot reproduce what an operator's ``gh`` can actually do,
    because bytes that are not valid UTF-8 never survive being written as a Python ``str``. Only a real
    child process writing to its raw stdout buffer gets them onto the pipe — and that is the whole point
    for a decider that spawns with ``text=True``, where the decode happens inside ``communicate()`` and
    any failure therefore surfaces from the ``subprocess.run`` CALL rather than from the parse after it.

    ``PATH`` is restored on the way out, so the real ``gh`` answers again for every later fixture. RESTORED
    means RESTORED, INCLUDING ITS ABSENCE: an unset ``PATH`` and ``PATH=""`` are NOT the same thing to
    ``exec``. With the variable gone, ``execvp`` falls back to ``confstr(_CS_PATH)`` and a bare ``git``
    still resolves; with it set to the empty string there is nothing to search and the spawn raises
    ``FileNotFoundError``. Writing back a defaulted ``""`` would therefore leave the process in a state it
    was never in, and every later fixture that spawns a bare command name would fail — so an originally
    ABSENT ``PATH`` is DELETED here, never re-created empty.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        fake = Path(tmpdir) / "gh"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.stdout.buffer.write({stdout!r})\n"
            "sys.stdout.buffer.flush()\n"
            f"sys.stderr.buffer.write({stderr!r})\n"
            "sys.stderr.buffer.flush()\n"
            f"raise SystemExit({exit_code})\n",
            encoding="utf-8")
        fake.chmod(0o755)
        before = os.environ.get("PATH")
        os.environ["PATH"] = tmpdir if not before else f"{tmpdir}{os.pathsep}{before}"
        try:
            yield
        finally:
            if before is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = before


@contextmanager
def gh_spawn_failing() -> "Generator[None, None, None]":
    """Make the fetch's SPAWN raise the exact ``OSError`` an absent ``gh`` raises, then restore it.

    ``subprocess.run`` is patched in ``_gauntlet/gh.py`` — the module that OWNS the spawn — because patching
    anywhere else leaves the real ``gh`` to answer and the fixture passes on a fetch it never broke. The
    exception is built exactly as ``execvp`` builds it, so the message a caller prints is the one an operator
    without ``gh`` sees; emptying ``PATH`` instead would make the fixture depend on the machine's ``gh``.
    """
    def boom(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "gh")
    from _gauntlet import gh as _gh
    real_run = _gh.subprocess.run
    setattr(_gh.subprocess, "run", boom)
    try:
        yield
    finally:
        setattr(_gh.subprocess, "run", real_run)


# One legacy view-fetch failure: a short name, the extra CLI args that provoke it, a factory for the context
# it needs, and the EXACT message tail the caller must print after its own `could not fetch PR view: ` prefix.
LegacyViewCase = tuple[str, "list[str]", "Callable[[], ContextManager[None]]", str]


def legacy_view_error_cases(work: Path, *, pr: str = "9") -> "list[LegacyViewCase]":
    """Every view-fetch failure that ALREADY HAD A MESSAGE before `_gauntlet/gh.py` owned the fetch, with the
    exact wording each one must still produce. THE SET IS CLOSED: it is fixed by history, not by what the
    fetch can raise, so nothing is ever added here. A failure absent from it had no prior message at all — it
    ended in a traceback and no verdict — and its wording is owned by `gh.py` alone.

    THE TAILS BELOW ARE LITERALS ON PURPOSE, and asserting the WHOLE reason is the entire point of this
    table. Both callers already had fixtures asserting the shared `could not fetch PR view: ` PREFIX, and a
    rewrite of every tail behind that prefix passed all of them, through six rounds of review, unnoticed. A
    prefix assertion cannot fail for a reworded tail; only a full-string one can.

    Both callers are checked against the SAME rows because both are meant to print the same thing: the two
    `load_view` bodies this fetch replaced produced byte-identical messages, so a per-caller expectation here
    would be a claim the code never made. Each suite supplies its own base argv and its own verdict name; the
    reason tail is all that is shared, and it is shared completely.

    `work` is a fixture's own empty directory; the recorded-view rows are materialized inside it.
    """
    empty = work / "empty-view.json"
    empty.write_bytes(b"")
    missing = work / "no-such-view.json"
    a_dir = work / "view-is-a-directory"
    a_dir.mkdir(exist_ok=True)
    return [
        ("recorded-view-not-json", ["--view-json", str(empty)], nullcontext,
         "Expecting value: line 1 column 1 (char 0)"),
        ("recorded-view-missing", ["--view-json", str(missing)], nullcontext,
         f"[Errno 2] No such file or directory: '{missing}'"),
        ("recorded-view-is-a-directory", ["--view-json", str(a_dir)], nullcontext,
         f"[Errno 21] Is a directory: '{a_dir}'"),
        ("gh-spawn-failed", ["--repo", "o/n"], gh_spawn_failing,
         f"could not run `gh pr view {pr}`: [Errno 2] No such file or directory: 'gh'"),
        ("gh-response-not-json", ["--repo", "o/n"], lambda: gh_writing(b"not json at all"),
         "gh response is not JSON (Expecting value: line 1 column 1 (char 0))"),
        ("gh-exited-non-zero", ["--repo", "o/n"],
         lambda: gh_writing(b"", stderr=b"boom from gh", exit_code=3),
         f"`gh pr view {pr}` exited 3: boom from gh"),
    ]


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
