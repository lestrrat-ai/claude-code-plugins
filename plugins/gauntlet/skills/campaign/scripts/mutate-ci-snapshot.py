#!/usr/bin/env python3
"""Mutation test for `ci-snapshot.py`: WHICH RULES ARE PINNED BY NO FIXTURE?

`ci-snapshot.py self-test` answers "do the fixtures still produce their expected verdicts?" — and a suite
of fixtures that all pass is exactly as reassuring as a suite of fixtures that all pass FOR THE WRONG
REASON. It cannot see the failure mode that matters most: a RULE THAT NOTHING TESTS. Delete such a rule and
`self-test` stays green, so the suite reports total health while the tool has quietly stopped checking
something. That is worse than a missing fixture; it is a missing fixture that LIES.

It has already happened, twice, in this very file. A hand-written "17/17 mutation matrix" in a fix report
claimed every rule was pinned. It was FALSE: the generic unexpected-field rejection and the RUNNING/PENDING
mapping were pinned by NOTHING — delete either and `self-test` still passed — and a REVIEWER, not the
suite, is what found that out. A matrix in a report is a claim; a claim nobody runs is a claim nobody
checks, and it rots the moment a rule is added.

So the matrix is DERIVED, here, every CI run:

  1. every rule in `ci-snapshot.py` marks itself with `# MUTATE:<rule-id>:<weakening>` directly above the
     statement that ENFORCES it (the `raise`, or the `return <verdict>`);
  2. this harness splices that weakening in — the rule is now gone, in the SPECIFIC way it would plausibly
     be broken: a row SKIPPED instead of rejected, a bad name ACCEPTED, a verdict NOT returned;
  3. it re-runs every fixture and every filename case against the mutant;
  4. a rule is PINNED if at least one fixture NOTICES — and the harness names the fixture, the verdict it
     now returns, and how loud the failure is;
  5. a rule NOTHING notices is reported UNPINNED and this exits non-zero.

Step 5 is the whole point: **"which rules are unpinned?" is a question the SUITE answers, not one a
reviewer has to discover.**

And the inventory cannot silently rot either. `--check-coverage` (run first, always) parses the rule
functions and asserts that EVERY enforcement point in them — every `raise SnapshotError` / `raise
Unverifiable`, every `return RED/PENDING/UNCLASSIFIED` — sits under a marker. Add a rule without a marker
and this fails: an unmarked rule is an untested rule, and it does not get to hide by being invisible.

KILL STRENGTH, reported per rule, because not all kills are equal:

  GREEN    the fixture goes GREEN — the loudest possible failure, and the one that matters: the weakened
           tool says "ship it" about evidence that is defective. Every rule that CAN be pinned this way is.
  VERDICT  the fixture returns a different NON-green verdict (e.g. red -> unclassified). Still a real kill.
           Some rules cannot do better: delete the "a FAILED run is RED" rule and the unclassified
           catch-all — a SAFETY NET, deliberately behind it — stops the file short of green. A rule whose
           removal is caught by a net BEHIND it is pinned, but it is not pinned by a false green.
  MESSAGE  the verdict is unchanged and only the REASON changes. Exactly one rule is like this, and it is
           honest: the witness-`sha` rejection is a MESSAGE SPECIALISATION of the generic unexpected-field
           rule. Delete it and the generic rule still says UNUSABLE — no verdict can ever pin it, and the
           specific message ("that sha is one WE INVENTED") is the entire thing it contributes. So the
           fixture pins the message, and this harness says so out loud instead of pretending otherwise.
  CRASH    the mutant raises instead of returning a verdict. A kill (a crash is not a verdict), and
           reported as such.

Usage:  python3 mutate-ci-snapshot.py            # the full matrix; exits 1 if ANY rule is unpinned
        python3 mutate-ci-snapshot.py --check-coverage   # marker coverage only, no mutants run
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

SCRIPT = Path(__file__).parent / "ci-snapshot.py"
FIXTURES = Path(__file__).parent / "fixtures" / "ci-snapshot"

# `    # MUTATE:blank-line:continue` — the rule id, and the statement that REPLACES the enforcement.
MARKER_RE = re.compile(r"^(?P<indent>[ ]*)# MUTATE:(?P<rule>[a-z0-9-]+):(?P<weakening>.+?)\s*$")

# The functions that ENFORCE the contract. Every enforcement point inside them must carry a marker.
# `evaluate` is not one: it MAPS an exception to a verdict, it does not decide anything.
RULE_FUNCTIONS = ("parse", "verify_filename", "verify_sha", "verify_sources", "check_containment", "decide")

# What "enforcement" looks like in those functions.
ENFORCING_EXCEPTIONS = ("SnapshotError", "Unverifiable")
ENFORCING_VERDICTS = ("RED", "PENDING", "UNCLASSIFIED")  # `return GREEN` is the ABSENCE of a rule

GREEN_KILL, VERDICT_KILL, MESSAGE_KILL, CRASH_KILL = "GREEN", "VERDICT", "MESSAGE", "CRASH"


class HarnessError(Exception):
    """The harness itself cannot run — never confuse this with a rule being unpinned."""


def load(source: str, name: str) -> types.ModuleType:
    """Exec a (possibly mutated) copy of ci-snapshot.py as a module. `__name__` is not `__main__`,
    so the CLI at the bottom does not fire."""
    mod = types.ModuleType(name)
    mod.__file__ = str(SCRIPT)
    exec(compile(source, f"<{name}>", "exec"), mod.__dict__)  # noqa: S102 - the whole job
    return mod


def markers(source: str) -> list[tuple[str, str, int]]:
    """(rule, weakening, line number of the marker). Order = source order."""
    out = []
    for n, line in enumerate(source.splitlines(), 1):
        m = MARKER_RE.match(line)
        if m:
            out.append((m.group("rule"), m.group("weakening"), n))
    return out


def marked_statements(source: str) -> dict[str, tuple[str, ast.stmt]]:
    """Map each rule id to (weakening, the statement the marker sits directly above).

    The marked statement is the one the rule is MADE of, so its line span is what gets replaced.
    """
    tree = ast.parse(source)
    stmts = {node.lineno: node for node in ast.walk(tree) if isinstance(node, ast.stmt)}
    out: dict[str, tuple[str, ast.stmt]] = {}
    for rule, weakening, line in markers(source):
        stmt = stmts.get(line + 1)
        if stmt is None:
            raise HarnessError(f"# MUTATE:{rule} on line {line} sits above no statement")
        if rule in out:
            raise HarnessError(f"duplicate rule id {rule!r} — every rule is marked exactly once")
        out[rule] = (weakening, stmt)
    if not out:
        raise HarnessError("no # MUTATE markers found — the rules cannot mark themselves absent")
    return out


def check_coverage(source: str, marked: dict[str, tuple[str, ast.stmt]]) -> list[str]:
    """EVERY enforcement point in a rule function must sit under a marker.

    This is the half of the coverage question that fixtures can never answer: a rule ADDED without a
    marker would never be mutated, so it would be reported "pinned" by nobody ever asking. An unmarked
    rule is an untested rule.
    """
    marked_lines = {stmt.lineno for _, stmt in marked.values()}
    tree = ast.parse(source)
    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in RULE_FUNCTIONS:
            continue
        for node in ast.walk(fn):
            enforcing = False
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                enforcing = getattr(node.exc.func, "id", None) in ENFORCING_EXCEPTIONS
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and node.value.elts:
                enforcing = getattr(node.value.elts[0], "id", None) in ENFORCING_VERDICTS
            if enforcing and node.lineno not in marked_lines:
                what = "raise" if isinstance(node, ast.Raise) else "return"
                problems.append(
                    f"{SCRIPT.name}:{node.lineno}: {fn.name}() enforces a rule ({what}) with NO "
                    f"# MUTATE marker — an unmarked rule is never mutated, so nothing can report it unpinned"
                )
    return problems


def mutate(source: str, rule: str, weakening: str, stmt: ast.stmt) -> str:
    """Replace the enforcing statement with its weakening, at the same indent."""
    lines = source.splitlines()
    indent = " " * (stmt.col_offset)
    body = [f"{indent}{weakening}  # MUTANT:{rule}"]
    return "\n".join(lines[: stmt.lineno - 1] + body + lines[stmt.end_lineno :]) + "\n"


def required_case_id(name: str, spec: str) -> str:
    """Stable, readable key for a REQUIRED_CASES case. The spec is part of the identity: the SAME bytes are
    green or pending depending on it, which is the entire point of that table."""
    return f"[req] {name} + {spec[:40]}"


def run_cases(mod: types.ModuleType) -> dict[str, tuple[str, str]]:
    """Every fixture + every filename case + every required-set case, against this (possibly mutated)
    module.

    A mutant that CRASHES has not returned a verdict, and "no verdict" is itself a deviation — so it is
    recorded, never swallowed.
    """
    out: dict[str, tuple[str, str]] = {}
    for name in mod.EXPECTED:
        try:
            out[name] = mod.evaluate(
                FIXTURES / name, mod.FIXTURE_SHA, required=mod.NO_REQUIRED, expect_filename_sha=False
            )
        except Exception as exc:  # noqa: BLE001 - a crash IS the result here
            out[name] = (f"crash:{type(exc).__name__}", str(exc))
    with tempfile.TemporaryDirectory() as tmp:
        for name, _want, _needle, _why in mod.FILENAME_CASES:
            path = Path(tmp) / name
            shutil.copyfile(FIXTURES / "green.jsonl", path)
            try:
                out[f"[name] {name}"] = mod.evaluate(
                    path, mod.FIXTURE_SHA, required=mod.NO_REQUIRED, expect_filename_sha=True
                )
            except Exception as exc:  # noqa: BLE001
                out[f"[name] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    # The required-set rule is the ONE rule whose input is not in the file. Its cases must run against the
    # mutant too, or removing it would be pinned by nobody — the failure mode this harness exists for.
    for name, spec, _want, _needle, _why in mod.REQUIRED_CASES:
        case = required_case_id(name, spec)
        try:
            out[case] = mod.evaluate(
                FIXTURES / name, mod.FIXTURE_SHA,
                required=mod.parse_required_set(spec), expect_filename_sha=False,
            )
        except Exception as exc:  # noqa: BLE001
            out[case] = (f"crash:{type(exc).__name__}", str(exc))
    return out


def expectations(mod: types.ModuleType) -> dict[str, tuple[str, str]]:
    """case -> (expected verdict, needle the reason must contain)."""
    out = {name: (want, needle) for name, (want, needle, _why) in mod.EXPECTED.items()}
    out.update({f"[name] {n}": (want, needle) for n, want, needle, _why in mod.FILENAME_CASES})
    out.update({
        required_case_id(n, spec): (want, needle)
        for n, spec, want, needle, _why in mod.REQUIRED_CASES
    })
    return out


def kills(expect: dict[str, tuple[str, str]], got: dict[str, tuple[str, str]], green: str) -> list[tuple]:
    """Which cases NOTICED the mutation, and how loudly. Returns (strength, case, verdict) sorted loudest
    first.

    A case whose EXPECTED verdict is green is not a killer — it is a CANARY. Mutations only ever REMOVE a
    rule, so they can never turn a green file non-green; if one does, the mutation is bogus, and that is a
    harness bug, not a pinned rule. `bogus()` below is what watches for it.
    """
    found = []
    for case, (want, needle) in expect.items():
        if want == green:
            continue
        verdict, reason = got[case]
        if verdict == green:
            strength = GREEN_KILL
        elif verdict.startswith("crash:"):
            strength = CRASH_KILL
        elif verdict != want:
            strength = VERDICT_KILL
        elif needle not in reason:
            strength = MESSAGE_KILL
        else:
            continue
        found.append((strength, case, verdict))
    order = {GREEN_KILL: 0, VERDICT_KILL: 1, CRASH_KILL: 2, MESSAGE_KILL: 3}
    return sorted(found, key=lambda k: (order[k[0]], k[1]))


def bogus(expect: dict[str, tuple[str, str]], got: dict[str, tuple[str, str]], green: str) -> list[str]:
    """Removing a rule can never make a GREEN fixture stop being green. If it did, the mutation is wrong."""
    return [
        f"{case} expected {green} but the mutant returned {got[case][0]}"
        for case, (want, _needle) in expect.items()
        if want == green and got[case][0] != green
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--check-coverage",
        action="store_true",
        help="only assert every enforcement point carries a # MUTATE marker; run no mutants",
    )
    args = p.parse_args()

    source = SCRIPT.read_text(encoding="utf-8")
    try:
        marked = marked_statements(source)
    except HarnessError as exc:
        print(f"HARNESS BROKEN: {exc}", file=sys.stderr)
        return 2

    gaps = check_coverage(source, marked)
    for gap in gaps:
        print(f"UNMARKED {gap}")
    if args.check_coverage:
        if gaps:
            print(f"\n{len(gaps)} enforcement point(s) carry NO marker.")
            return 1
        print(f"every enforcement point in {SCRIPT.name} carries a # MUTATE marker ({len(marked)} rules).")
        return 0

    baseline = load(source, "ci_snapshot_baseline")
    expect = expectations(baseline)
    got = run_cases(baseline)
    stale = [f"{c}: expected {w}/{n!r}, got {got[c]}" for c, (w, n) in expect.items()
             if got[c][0] != w or n not in got[c][1]]
    if stale:
        print("BASELINE IS NOT GREEN — fix `ci-snapshot.py self-test` before asking what it pins:")
        for s in stale:
            print(f"  {s}")
        return 2
    print(f"baseline: {len(expect)} cases hold. Now removing each rule in turn.\n")

    print(f"{'rule':22} {'weakened to':46} {'killed by':30} {'verdict':13} kill")
    print(f"{'-' * 22} {'-' * 46} {'-' * 30} {'-' * 13} ----")

    unpinned, broken, tally = [], [], {GREEN_KILL: 0, VERDICT_KILL: 0, MESSAGE_KILL: 0, CRASH_KILL: 0}
    for rule, (weakening, stmt) in marked.items():
        try:
            mod = load(mutate(source, rule, weakening, stmt), f"ci_snapshot_mutant_{rule.replace('-', '_')}")
        except SyntaxError as exc:
            broken.append(f"{rule}: the weakening {weakening!r} does not compile ({exc})")
            continue
        mutant = run_cases(mod)
        wrong = bogus(expect, mutant, baseline.GREEN)
        if wrong:
            broken.append(f"{rule}: BOGUS MUTATION — {'; '.join(wrong)}")
            continue
        killers = kills(expect, mutant, baseline.GREEN)
        if not killers:
            print(f"{rule:22} {weakening[:46]:46} {'NOTHING':30} {'—':13} UNPINNED")
            unpinned.append(rule)
            continue
        strength, case, verdict = killers[0]
        extra = f" (+{len(killers) - 1} more)" if len(killers) > 1 else ""
        tally[strength] += 1
        print(f"{rule:22} {weakening[:46]:46} {case[:30]:30} {verdict:13} {strength}{extra}")

    print()
    for b in broken:
        print(f"HARNESS BROKEN: {b}")
    if unpinned:
        print(
            f"{len(unpinned)} RULE(S) PINNED BY NO FIXTURE: {', '.join(unpinned)}\n"
            f"Delete any one of them and `self-test` still passes — the suite would report total health "
            f"while the tool had stopped checking. Write a fixture that FAILS when the rule is gone."
        )
    if unpinned or broken:
        return 1
    print(
        f"all {len(marked)} rules are pinned: {tally[GREEN_KILL]} by a FALSE GREEN, "
        f"{tally[VERDICT_KILL]} by a verdict change, {tally[CRASH_KILL]} by a crash, "
        f"{tally[MESSAGE_KILL]} by its message. Remove any rule and a fixture fails."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
