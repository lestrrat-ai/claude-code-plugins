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

IT DRIVES `ci-status.py` TOO (`--script`), because the question it answers is not about one file. That
script is the PRODUCER (it fetches a PR's checks and promotes the snapshot this one verifies), and its
rules are pinned by the same method: a `# MUTATE:` marker on each, fixtures that must notice. Two things
differ, and the target script DECLARES them rather than this harness guessing:

  * **How the rules are ENUMERATED.** Every rule in `ci-snapshot.py` is a `raise` or a `return <verdict>`,
    so `--check-coverage` can DISCOVER them from the AST and prove none is unmarked. A producer's rule is a
    CHOICE OF VALUE ("take the sha from the RESPONSE, never from the literal we asked for") — no AST scan
    can tell that assignment from any other. Such a script instead exports `MUTATION_RULES`, a DECLARED
    inventory, and coverage reconciles it against the markers BOTH WAYS: a declared rule with no marker is
    never mutated; a marker nobody declared is a rule nobody wrote down.
  * **Whether a GREEN fixture may move.** See `bogus()`.
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

HERE = Path(__file__).parent
DEFAULT_SCRIPT = HERE / "ci-snapshot.py"
FIXTURES = HERE / "fixtures" / "ci-snapshot"

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


def load(source: str, name: str, script: Path) -> types.ModuleType:
    """Exec a (possibly mutated) copy of the target script as a module. `__name__` is not `__main__`,
    so the CLI at the bottom of it does not fire."""
    mod = types.ModuleType(name)
    mod.__file__ = str(script)
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


def bare_name(node: ast.expr | None) -> str | None:
    """The identifier this expression IS, or None if it is not a bare name.

    `ast.expr` declares no `id` — only `ast.Name` does. Asking for `.id` through the base class (or through
    a `getattr`) reads as "any expression might be a name", which is false, and it silently answers None for
    an expression whose shape was never considered. This asks the question the node type can actually
    answer, and every caller below has to handle the None.
    """
    return node.id if isinstance(node, ast.Name) else None


def is_enforcing_raise(node: ast.Raise) -> bool:
    """`raise SnapshotError(...)` / `raise Unverifiable(...)` — a rule REJECTING the evidence."""
    return isinstance(node.exc, ast.Call) and bare_name(node.exc.func) in ENFORCING_EXCEPTIONS


def is_enforcing_return(node: ast.Return) -> bool:
    """`return RED/PENDING/UNCLASSIFIED, ...` — a rule DECIDING a non-green verdict."""
    return (
        isinstance(node.value, ast.Tuple)
        and bool(node.value.elts)
        and bare_name(node.value.elts[0]) in ENFORCING_VERDICTS
    )


def check_coverage(source: str, marked: dict[str, tuple[str, ast.stmt]], script: Path,
                   declared: "dict[str, str] | None") -> list[str]:
    """Is every rule MARKED? A rule ADDED without a marker is never mutated, so nothing can ever report it
    unpinned — it would be "covered" by nobody ever asking. This is the half of the coverage question that
    fixtures can never answer.

    Two ways to ask it, and the target script picks by exporting `MUTATION_RULES` (or not):

      * **DERIVED (`ci-snapshot.py`)** — every rule there IS a `raise` or a `return <verdict>` inside a rule
        function, so the AST can DISCOVER them and this proves, with no inventory to keep, that none is
        unmarked.
      * **DECLARED (`ci-status.py`)** — a PRODUCER's rules are choices of value, not raises; the AST cannot
        see them. It declares them instead, and this reconciles the inventory against the markers BOTH WAYS.
        The honest limit, stated where it lives (`ci-status.py`, `RULES`): a rule added to that file and left
        out of BOTH the inventory and the markers is invisible to this check. Reconciliation cannot invent
        the entry you never wrote; only review catches that.
    """
    marked_lines = {stmt.lineno for _, stmt in marked.values()}
    problems = []

    if declared is not None:
        for rule in sorted(set(declared) - set(marked)):
            problems.append(
                f"{script.name}: rule {rule!r} is DECLARED in MUTATION_RULES but carries no # MUTATE marker "
                f"— it is never mutated, so nothing can report it unpinned"
            )
        for rule in sorted(set(marked) - set(declared)):
            problems.append(
                f"{script.name}: marker {rule!r} is not DECLARED in MUTATION_RULES — a rule nobody wrote "
                f"down is a rule nobody reviews"
            )
        return problems

    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in RULE_FUNCTIONS:
            continue
        for node in ast.walk(fn):
            # `ast.walk` yields `ast.AST`, and `AST` carries NO position — `lineno` lives on the concrete
            # node types. So the enforcement point is narrowed to the two node types it can actually BE,
            # and the line number is read off THOSE. That narrowing is the whole point: it is what makes
            # `what` follow from the node's type instead of being re-derived from it afterwards.
            if isinstance(node, ast.Raise):
                what, enforcing = "raise", is_enforcing_raise(node)
            elif isinstance(node, ast.Return):
                what, enforcing = "return", is_enforcing_return(node)
            else:
                continue
            if enforcing and node.lineno not in marked_lines:
                problems.append(
                    f"{script.name}:{node.lineno}: {fn.name}() enforces a rule ({what}) with NO "
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
    """Every case, against this (possibly mutated) module: case -> (verdict, reason).

    For `ci-snapshot.py` that is every fixture + every filename case + every REQUIRED-SET case (the
    required-set rule is the one rule whose input is NOT in the file, so its cases carry their own spec).

    A script that owns cases this harness cannot construct — `ci-status.py`'s are RECORDED API RESPONSES
    driven through its producer, plus the SEAMS no recorded response can reach (its `gh` runner, which every
    fixture replaces, and its CLI guards) — exports `mutation_run()` and answers for itself.

    A mutant that CRASHES has not returned a verdict, and "no verdict" is itself a deviation — so it is
    recorded, never swallowed.
    """
    if hasattr(mod, "mutation_run"):
        return mod.mutation_run()

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
    if hasattr(mod, "mutation_expectations"):
        return mod.mutation_expectations()
    out = {name: (want, needle) for name, (want, needle, _) in mod.EXPECTED.items()}
    out.update({f"[name] {n}": (want, needle) for n, want, needle, _ in mod.FILENAME_CASES})
    out.update({
        required_case_id(n, spec): (want, needle)
        for n, spec, want, needle, _ in mod.REQUIRED_CASES
    })
    return out


def kills(expect: dict[str, tuple[str, str]], got: dict[str, tuple[str, str]], green: str,
          canary: bool) -> list[tuple]:
    """Which cases NOTICED the mutation, and how loudly. Returns (strength, case, verdict) sorted loudest
    first.

    Under the CANARY (a VERIFIER — see `bogus()`), a green-expecting case is not a killer: a mutation can
    only make a verifier more permissive, so a green fixture MUST stay green, and one that moves means the
    mutation is bogus. Without the canary (a PRODUCER), removing a rule corrupts the artifact and the
    verifier downstream refuses it — so a green fixture going `unusable` IS the fixture noticing, and it
    counts like any other kill.
    """
    found = []
    for case, (want, needle) in expect.items():
        if want == green and canary:
            continue
        verdict, reason = got[case]
        if verdict == want and needle in reason:
            continue  # the case is UNMOVED — it did not notice this mutation
        if verdict == green and want != green:
            strength = GREEN_KILL  # the weakened tool says "ship it" about evidence that is defective
        elif verdict.startswith("crash:"):
            strength = CRASH_KILL
        elif verdict != want:
            strength = VERDICT_KILL
        else:
            strength = MESSAGE_KILL  # right verdict, and ONLY the reason moved
        found.append((strength, case, verdict))
    order = {GREEN_KILL: 0, VERDICT_KILL: 1, CRASH_KILL: 2, MESSAGE_KILL: 3}
    return sorted(found, key=lambda k: (order[k[0]], k[1]))


def bogus(expect: dict[str, tuple[str, str]], got: dict[str, tuple[str, str]], green: str,
          canary: bool) -> list[str]:
    """THE CANARY, and it holds for a VERIFIER ONLY.

    Removing a rule from `ci-snapshot.py` can only make it MORE PERMISSIVE, so a green fixture must STAY
    green; one that moves means the mutation spliced in something that broke the tool rather than weakening
    one rule — a harness bug, and it must never be miscounted as a pinned rule.

    **THE INVERSE HOLDS FOR A PRODUCER, WHICH IS WHY `ci-status.py` TURNS THIS OFF** (`MUTATION_GREEN_CANARY
    = False`). Removing one of ITS rules CORRUPTS THE ARTIFACT — a marker missing the sha it must carry, a
    check family never fetched — and `ci-snapshot.py`, downstream, then REFUSES it. `green.json` coming back
    `unusable` is the fixture doing its job, not the harness failing at its own.
    """
    if not canary:
        return []
    return [
        f"{case} expected {green} but the mutant returned {got[case][0]}"
        for case, (want, _) in expect.items()
        if want == green and got[case][0] != green
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--script", type=Path, default=DEFAULT_SCRIPT,
        help="the script whose rules to mutate (default: ci-snapshot.py; also: ci-status.py)",
    )
    p.add_argument(
        "--check-coverage",
        action="store_true",
        help="only assert every rule carries a # MUTATE marker; run no mutants",
    )
    args = p.parse_args()

    script: Path = args.script
    if not script.exists():
        print(f"HARNESS BROKEN: no such script: {script}", file=sys.stderr)
        return 2

    source = script.read_text(encoding="utf-8")
    try:
        marked = marked_statements(source)
        baseline = load(source, f"mutation_baseline_{script.stem.replace('-', '_')}", script)
    except HarnessError as exc:
        print(f"HARNESS BROKEN: {exc}", file=sys.stderr)
        return 2

    declared = getattr(baseline, "MUTATION_RULES", None)
    canary = getattr(baseline, "MUTATION_GREEN_CANARY", True)

    gaps = check_coverage(source, marked, script, declared)
    for gap in gaps:
        print(f"UNMARKED {gap}")
    if args.check_coverage:
        if gaps:
            print(f"\n{len(gaps)} rule(s) carry NO marker.")
            return 1
        print(f"every rule in {script.name} carries a # MUTATE marker ({len(marked)} rules).")
        return 0
    if gaps:
        print(f"\n{len(gaps)} rule(s) are not marked — an unmarked rule is never mutated.")
        return 1

    print(f"=== {script.name} ===")
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
            mod = load(mutate(source, rule, weakening, stmt), f"mutant_{rule.replace('-', '_')}", script)
        except SyntaxError as exc:
            broken.append(f"{rule}: the weakening {weakening!r} does not compile ({exc})")
            continue
        mutant = run_cases(mod)
        wrong = bogus(expect, mutant, baseline.GREEN, canary)
        if wrong:
            broken.append(f"{rule}: BOGUS MUTATION — {'; '.join(wrong)}")
            continue
        killers = kills(expect, mutant, baseline.GREEN, canary)
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
