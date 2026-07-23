#!/usr/bin/env python3
# ci: pyright
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

And the inventory cannot silently rot either. Coverage is checked FIRST, in EVERY mode: it parses the rule
functions and asserts that EVERY enforcement point in them — every `raise SnapshotError` / `raise
Unverifiable`, every `return RED/PENDING/UNCLASSIFIED` — sits under a marker. Add a rule without a marker
and this fails — the bare full-matrix run and `--check-coverage` alike; the flag only skips the mutants,
never the coverage gate. An unmarked rule is an untested rule, and it does not get to hide by being
invisible: the matrix iterates marked rules only, so a gap is invisible to it and must be fatal on its own.

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

Usage:  python3 mutate-ci-snapshot.py            # coverage + full matrix; exits 1 if any rule is UNMARKED or unpinned
        python3 mutate-ci-snapshot.py --check-coverage   # marker coverage only, no mutants run
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import types
from pathlib import Path

from _gauntlet.mutation import (
    load_source_module,
    marked_statements,
    mutate_source,
    unmarked_enforcements,
)

SCRIPT = Path(__file__).parent / "ci-snapshot.py"
FIXTURES = Path(__file__).parent / "fixtures" / "ci-snapshot"

# The functions that ENFORCE the contract. Every enforcement point inside them must carry a marker.
# `evaluate` is not one: it MAPS an exception to a verdict, it does not decide anything.
RULE_FUNCTIONS = ("parse", "verify_filename", "verify_sha", "verify_sources", "check_containment", "decide")

# What "enforcement" looks like in those functions.
ENFORCING_EXCEPTIONS = ("SnapshotError", "Unverifiable")
ENFORCING_VERDICTS = ("RED", "PENDING", "UNCLASSIFIED")  # `return GREEN` is the ABSENCE of a rule

GREEN_KILL, VERDICT_KILL, MESSAGE_KILL, CRASH_KILL = "GREEN", "VERDICT", "MESSAGE", "CRASH"


class HarnessError(Exception):
    """The harness itself cannot run — never confuse this with a rule being unpinned."""


def check_shared_mechanics() -> None:
    """Pin the shared helper edge that an all-marked real subject cannot exercise."""
    source = (
        "def decide():\n"
        "    # MUTATE:keep:pass\n"
        "    keep()\n"
        "    raise SnapshotError('refused')\n"
        "    return RED, 'failed'\n"
        "\n"
        "def ignored():\n"
        "    raise SnapshotError('outside configured functions')\n"
    )
    marked = marked_statements(
        source,
        error_factory=HarnessError,
        no_markers_message="synthetic subject lost its mutation marker",
    )
    weakening, statement = marked.get("keep", ("", None))
    if weakening != "pass" or statement is None or statement.lineno != 3:
        raise HarnessError("the shared marker parser did not bind a marker to the following statement")

    gaps = unmarked_enforcements(
        source,
        marked,
        rule_functions=("decide",),
        enforcing_exceptions=("SnapshotError",),
        enforcing_verdicts=("RED",),
        source_name="synthetic.py",
    )
    suffix = "with NO # MUTATE marker — an unmarked rule is never mutated, so nothing can report it unpinned"
    expected = [
        f"synthetic.py:4: decide() enforces a rule (raise) {suffix}",
        f"synthetic.py:5: decide() enforces a rule (return) {suffix}",
    ]
    if gaps != expected:
        raise HarnessError(f"the shared enforcement scan returned {gaps!r}, expected {expected!r}")

    mutant = mutate_source(source, "keep", weakening, statement)
    if "    pass  # MUTANT:keep\n" not in mutant or "    keep()\n" in mutant:
        raise HarnessError("the shared source mutator did not replace the marked statement")
    origin = Path("synthetic.py")
    module = load_source_module(mutant, "synthetic_mutant", origin)
    if module.__file__ != str(origin):
        raise HarnessError("the shared source loader did not preserve the subject's origin")


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
        # Only the NAME is an input here — the verdict a case expects is what `expectations()` reads, and it
        # unpacks the row in full, so the row's shape stays pinned there.
        for name, *_ in mod.FILENAME_CASES:
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
    for name, spec, *_ in mod.REQUIRED_CASES:
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
    out = {name: (want, needle) for name, (want, needle, _) in mod.EXPECTED.items()}
    out.update({f"[name] {n}": (want, needle) for n, want, needle, _ in mod.FILENAME_CASES})
    out.update({
        required_case_id(n, spec): (want, needle)
        for n, spec, want, needle, _ in mod.REQUIRED_CASES
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
        for case, (want, _) in expect.items()
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
        check_shared_mechanics()
        marked = marked_statements(
            source,
            error_factory=HarnessError,
            no_markers_message="no # MUTATE markers found — the rules cannot mark themselves absent",
        )
    except HarnessError as exc:
        print(f"HARNESS BROKEN: {exc}", file=sys.stderr)
        return 2

    gaps = unmarked_enforcements(
        source,
        marked,
        rule_functions=RULE_FUNCTIONS,
        enforcing_exceptions=ENFORCING_EXCEPTIONS,
        enforcing_verdicts=ENFORCING_VERDICTS,
        source_name=SCRIPT.name,
    )
    for gap in gaps:
        print(f"UNMARKED {gap}")
    # Coverage is checked FIRST, in EVERY mode — not only under --check-coverage. CI runs the bare
    # full-matrix invocation, so gating the failure on the flag let a new unmarked rule pass the build
    # while merely PRINTING `UNMARKED`. An unmarked rule is never mutated (the matrix below iterates
    # `marked` only), so the matrix can never report it unpinned: the gap itself IS the failure, and it
    # must be fatal wherever coverage runs. (`review-pass-test.py` fails on its gaps the same way.)
    if gaps:
        print(f"\n{len(gaps)} enforcement point(s) carry NO marker.")
        return 1
    if args.check_coverage:
        print(f"every enforcement point in {SCRIPT.name} carries a # MUTATE marker ({len(marked)} rules).")
        return 0

    baseline = load_source_module(source, "ci_snapshot_baseline", SCRIPT)
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
            mod = load_source_module(
                mutate_source(source, rule, weakening, stmt),
                f"ci_snapshot_mutant_{rule.replace('-', '_')}",
                SCRIPT,
            )
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
