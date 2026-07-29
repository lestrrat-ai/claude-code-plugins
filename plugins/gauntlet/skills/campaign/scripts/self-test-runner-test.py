#!/usr/bin/env python3
"""Fixtures for the shared fixture runner in `_gauntlet/testing.py`.

MOST `self-test` SUBCOMMANDS IN THIS DIRECTORY NOW GET THEIR EXIT CODE FROM THAT ONE RUNNER. That
concentration is the point of it, and it is also the hazard: a runner that swallowed a failing fixture,
miscounted, or returned 0 over an empty case table would turn every one of those suites green at once,
and every one of them would still LOOK green — so their output cannot be the evidence. THIS FILE IS THE
EVIDENCE. It drives the runner into every way of not passing that the runner guards, and fails if any of
those came back zero or unreported.

Run it directly (`python3 self-test-runner-test.py`); CI does. It has no accessor and no `self-test`
subcommand of its own, because the thing it tests IS the self-test machinery.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _gauntlet.testing import capture_cli, run_cases, run_sibling_suite

SUBJECT = "the runner's own contract"


class Failure(AssertionError):
    """Stands in for a suite's own failure type — the `failure=` argument the runner is handed."""


class OtherFailure(RuntimeError):
    """Anything a fixture raises that is NOT the suite's failure type."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def drive(**kwargs) -> "tuple[int, str]":
    """Run `run_cases` with stdout captured, and return its exit code and what it printed."""
    code, out, _err = capture_cli(lambda _argv: run_cases(subject=SUBJECT, **kwargs), [])
    return code, out


def drive_sibling(sibling: Path, module_name: str = "runner_probe_suite") -> "tuple[int, str]":
    """Same, for the sibling-loading door."""
    code, out, _err = capture_cli(
        lambda _argv: run_sibling_suite(sibling, module_name, failure=Failure, subject=SUBJECT), [])
    return code, out


def passing() -> None:
    """An ordinary fixture: it does its work and raises nothing."""


# --- the happy path, so the negatives below mean something --------------------

def t_all_passing_returns_zero() -> None:
    code, out = drive(cases=[("a", "a rule", passing), ("b", "another", passing)], failure=Failure)
    check(code == 0, f"a suite whose fixtures all pass must exit 0, got {code}")
    check(out.count("ok       ") == 2, f"every passing fixture gets an `ok` line: {out!r}")
    check(f"all 2 fixtures hold — {SUBJECT} is intact." in out, f"the verdict names the subject: {out!r}")


def t_a_returned_value_is_not_a_verdict() -> None:
    """The contract is RAISE-to-fail, so what a fixture returns is nobody's business."""
    code, _out = drive(cases=[("falsy", "returning False is not failing", lambda: False),
                              ("none", "returning None is not failing", lambda: None)],
                       failure=Failure)
    check(code == 0, f"a fixture that returns a falsy value has PASSED, got exit {code}")


# --- a fixture that fails, in each of the ways it can --------------------------

def t_the_suites_own_failure_is_reported_with_its_message() -> None:
    def fails() -> None:
        raise Failure("the rule this fixture pins does not hold")

    code, out = drive(cases=[("ok-one", "holds", passing), ("bad", "the broken rule", fails)],
                      failure=Failure)
    check(code == 1, f"one failing fixture must make the whole suite exit 1, got {code}")
    check("the rule this fixture pins does not hold" in out,
          f"the fixture's OWN message is what identifies the broken rule: {out!r}")
    check(f"1 check(s) FAILED — {SUBJECT} is broken." in out, f"the tally names the subject: {out!r}")
    check("ok       ok-one" in out, f"the fixtures that passed still report: {out!r}")


def t_any_other_exception_is_a_failure_named_by_its_type() -> None:
    def crashes() -> None:
        raise OtherFailure("boom")

    code, out = drive(cases=[("crash", "a rule", crashes)], failure=Failure)
    check(code == 1, f"a fixture that CRASHES has not passed, got exit {code}")
    check("raised OtherFailure: boom" in out,
          f"a crash is reported with its type — it is not a verdict the fixture rendered: {out!r}")


def t_a_fixture_that_exits_is_reported_and_the_rest_still_run() -> None:
    """`SystemExit(0)` is the dangerous one: unhandled it would end the suite silently and GREEN."""
    def exits() -> None:
        raise SystemExit(0)

    code, out = drive(cases=[("exits", "a rule", exits), ("after", "runs anyway", passing)],
                      failure=Failure)
    check(code == 1, f"a fixture that EXITS the interpreter has not passed, got exit {code}")
    check("raised SystemExit(0)" in out, f"the report names what it exited with: {out!r}")
    check("ok       after" in out,
          f"the fixtures AFTER an exiting one must still run — the exit was contained: {out!r}")


def t_every_failure_is_counted_not_just_the_first() -> None:
    def fails() -> None:
        raise Failure("no")

    code, out = drive(cases=[("a", "r", fails), ("b", "r", fails), ("c", "r", passing)], failure=Failure)
    check(code == 1, f"expected exit 1, got {code}")
    check("2 check(s) FAILED" in out, f"the tally counts EVERY failure, not just the first: {out!r}")


# --- a suite that proved nothing ----------------------------------------------

def t_an_empty_case_table_is_a_failure() -> None:
    """A self-test with no subject passes every time. That is the loudest false green there is."""
    code, out = drive(cases=[], failure=Failure)
    check(code == 1, f"an EMPTY case table must FAIL, never report an all-clear; got exit {code}")
    check("empty-case-table" in out, f"the refusal names itself: {out!r}")


def t_a_missing_sibling_is_a_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        code, out = drive_sibling(Path(d) / "not-there-test.py")
    check(code == 1, f"a MISSING fixture file must FAIL — it has nothing to check; got exit {code}")
    check("sibling-fixtures" in out and "IS MISSING" in out, f"the refusal says what is wrong: {out!r}")


def t_an_unloadable_sibling_is_a_failure() -> None:
    """A path Python has no loader for: the module comes back as `None`, never as an empty suite."""
    with tempfile.TemporaryDirectory() as d:
        sibling = Path(d) / "fixtures-test.txt"  # a real file, but not one importlib can load
        sibling.write_text("CASES = []\n", encoding="utf-8")
        code, out = drive_sibling(sibling)
    check(code == 1, f"a fixture file that cannot be LOADED must FAIL, got exit {code}")
    check("cannot be loaded as a module" in out, f"the refusal says what is wrong: {out!r}")


def t_a_sibling_exporting_no_cases_is_a_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        sibling = Path(d) / "no-cases-test.py"
        sibling.write_text("X = 1\n", encoding="utf-8")
        code, out = drive_sibling(sibling, "runner_probe_no_cases")
    check(code == 1, f"a sibling with NO `CASES` must FAIL, got exit {code}")
    check("exports no CASES" in out, f"the refusal says what is wrong: {out!r}")


def t_a_sibling_exporting_empty_cases_is_a_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        sibling = Path(d) / "empty-cases-test.py"
        sibling.write_text("CASES = []\n", encoding="utf-8")
        code, _out = drive_sibling(sibling, "runner_probe_empty_cases")
    check(code == 1, f"a sibling whose `CASES` is EMPTY must FAIL, got exit {code}")


# --- a fixture whose body never ran -------------------------------------------
#
# These three classify the FUNCTION, before it is called. The regression fixture below is why: the
# earlier attempt at this classified the RETURN VALUE, and failed ordinary fixtures for what they
# returned. See `_never_ran` in `_gauntlet/testing.py`.

def t_a_generator_fixture_never_ran() -> None:
    def generator_fixture():
        yield 1  # calling this BUILDS an iterator; the body never executes

    code, out = drive(cases=[("gen", "a rule", generator_fixture)], failure=Failure)
    check(code == 1, f"a fixture whose body never ran has proved NOTHING, got exit {code}")
    check("generator function" in out, f"the report says why it never ran: {out!r}")


def t_a_coroutine_fixture_never_ran() -> None:
    async def coroutine_fixture() -> None:
        raise Failure("this never runs — nothing awaits it")

    code, out = drive(cases=[("coro", "a rule", coroutine_fixture)], failure=Failure)
    check(code == 1, f"an `async def` fixture is never awaited and proves NOTHING, got exit {code}")
    check("coroutine function" in out, f"the report says why it never ran: {out!r}")


def t_an_async_generator_fixture_never_ran() -> None:
    async def async_generator_fixture():
        yield 1

    code, out = drive(cases=[("agen", "a rule", async_generator_fixture)], failure=Failure)
    check(code == 1, f"an async generator fixture proves NOTHING, got exit {code}")
    check("async generator function" in out, f"the report says why it never ran: {out!r}")


def t_a_fixture_that_ran_and_returned_a_generator_passes() -> None:
    """REGRESSION. This is the exact fixture the earlier return-value classification failed.

    It runs to completion — the marker proves it — and then returns a generator, which is an ordinary
    thing for a fixture to do. Judging the RETURN VALUE reported "its body never executed" about a body
    that plainly had. Judging the FUNCTION cannot make that mistake.
    """
    marker: "list[str]" = []

    def ran_then_returns_a_generator():
        marker.append("the body ran")
        return (n for n in (1, 2, 3))

    code, out = drive(cases=[("genexp", "a rule", ran_then_returns_a_generator)], failure=Failure)
    check(marker == ["the body ran"], "the fixture's body must actually have run for this to test anything")
    check(code == 0, f"a fixture that RAN and returned a generator has PASSED; got exit {code}\n{out}")
    check("never" not in out, f"nothing may claim this body did not run: {out!r}")


# --- the working directory ----------------------------------------------------

def t_per_case_dir_hands_each_fixture_a_fresh_empty_directory() -> None:
    seen: "list[Path]" = []

    def uses_work(work: Path) -> None:
        check(work.is_dir(), f"the fixture's working directory must EXIST, got {work}")
        check(not list(work.iterdir()), f"it must be EMPTY, found {list(work.iterdir())}")
        (work / "left-behind").write_text("x", encoding="utf-8")
        seen.append(work)

    code, _out = drive(cases=[("first", "r", uses_work), ("second", "r", uses_work)],
                       failure=Failure, per_case_dir=True)
    check(code == 0, f"expected exit 0, got {code}")
    check(len(set(seen)) == 2, f"each fixture gets its OWN directory, got {seen}")


def t_two_rows_sharing_a_name_still_get_separate_directories() -> None:
    seen: "list[Path]" = []
    code, _out = drive(cases=[("same", "r", seen.append), ("same", "r", seen.append)],
                       failure=Failure, per_case_dir=True)
    check(code == 0, f"a duplicate row NAME must not collide into one directory; got exit {code}")
    check(len(set(seen)) == 2, f"the rows got the same directory: {seen}")


# --- what the runner deliberately does NOT catch ------------------------------

def t_keyboard_interrupt_aborts_rather_than_counting_as_one_failure() -> None:
    """Ctrl-C is the OPERATOR's verdict, not a fixture's. It must end the run, not be filed as a check."""
    def interrupted() -> None:
        raise KeyboardInterrupt

    aborted = False
    try:
        drive(cases=[("interrupt", "a rule", interrupted), ("after", "r", passing)], failure=Failure)
    except KeyboardInterrupt:
        aborted = True
    check(aborted, "KeyboardInterrupt must PROPAGATE — never be caught and counted as one failed check")


CASES = [
    ("all-pass", "a suite whose fixtures all pass exits 0", t_all_passing_returns_zero),
    ("return-is-not-verdict", "the contract is raise-to-fail; a return value is ignored",
     t_a_returned_value_is_not_a_verdict),
    ("own-failure", "the suite's failure type is reported with the fixture's own message",
     t_the_suites_own_failure_is_reported_with_its_message),
    ("other-exception", "any other exception fails, named by its type",
     t_any_other_exception_is_a_failure_named_by_its_type),
    ("fixture-exits", "a fixture that exits the interpreter fails, and the rest still run",
     t_a_fixture_that_exits_is_reported_and_the_rest_still_run),
    ("counts-all", "every failure is counted, not just the first",
     t_every_failure_is_counted_not_just_the_first),
    ("empty-table", "an empty case table is a FAILURE, never an all-clear",
     t_an_empty_case_table_is_a_failure),
    ("missing-sibling", "a missing fixture file is a FAILURE", t_a_missing_sibling_is_a_failure),
    ("unloadable-sibling", "a fixture file that cannot be loaded is a FAILURE",
     t_an_unloadable_sibling_is_a_failure),
    ("no-cases", "a sibling exporting no CASES is a FAILURE", t_a_sibling_exporting_no_cases_is_a_failure),
    ("empty-cases", "a sibling exporting an empty CASES is a FAILURE",
     t_a_sibling_exporting_empty_cases_is_a_failure),
    ("generator-fixture", "a `yield`ing fixture never ran and fails", t_a_generator_fixture_never_ran),
    ("coroutine-fixture", "an `async def` fixture never ran and fails", t_a_coroutine_fixture_never_ran),
    ("async-generator-fixture", "an async generator fixture never ran and fails",
     t_an_async_generator_fixture_never_ran),
    ("ran-then-returned-a-generator", "REGRESSION: a fixture that RAN and returned a generator PASSES",
     t_a_fixture_that_ran_and_returned_a_generator_passes),
    ("per-case-dir", "each fixture gets a fresh empty working directory",
     t_per_case_dir_hands_each_fixture_a_fresh_empty_directory),
    ("per-case-dir-name-clash", "rows sharing a name still get separate directories",
     t_two_rows_sharing_a_name_still_get_separate_directories),
    ("keyboard-interrupt", "Ctrl-C aborts the run rather than counting as one failed check",
     t_keyboard_interrupt_aborts_rather_than_counting_as_one_failure),
]


def main() -> int:
    """The one hand-rolled loop left in this directory, and DELIBERATELY so.

    Every other suite runs on `run_cases`. This suite cannot: it is the code under test, and a runner
    that swallowed failures would swallow the failures of the very fixtures meant to catch it. So the
    loop here is short enough to read in full and check by eye.
    """
    failures = 0
    for name, rule, fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a fixture that crashes has not passed
            print(f"FAIL     {name:32} -> {rule}\n         {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the shared fixture runner is broken, and every suite that "
              f"exits through it is reporting a health it has not proved.")
        return 1
    print(f"all {len(CASES)} fixtures hold — the shared fixture runner refuses every way of not passing "
          f"that it guards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
