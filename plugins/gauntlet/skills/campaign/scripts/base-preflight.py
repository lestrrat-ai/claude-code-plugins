#!/usr/bin/env python3
"""DECIDE whether a PR's branch is current with its base BEFORE a review or a fix is authored on it.

This enforces the rebase-before-review/fix precondition `stage-2-review-gate.md` states in prose: if a PR
is CONFLICTING/DIRTY/BEHIND with `<base>`, rebase it before reviewing or fixing. That prose was a rule a
model re-derived by eye every heartbeat, and a fix authored on a stale/conflicting base is wasted — it is
re-reviewed against the rebased tip anyway. This turns the prose into an enforced check.

It DECIDES one of `proceed` / `rebase-first` / `recheck` from the live PR view and PERFORMS NO REBASE: the
driver rebases when told `rebase-first`, then re-runs this. Deciding and doing are two jobs and this is only
the first — nothing here runs `git rebase`/`git merge` or edits a branch.

    base-preflight.py check --pr 31 [--repo owner/name] [--view-json <path>] [--project-root <dir>]
    base-preflight.py self-test   run every fixture (base-preflight-test.py)

The verdict is printed as JSON on stdout, and the EXIT CODE gates a caller's `$?`: 0 for `proceed`, non-zero
for `rebase-first` and `recheck` (and for a view that could not be fetched or was malformed — those fail
CLOSED to `recheck`, never `proceed`). The fixture suite is the SIBLING `base-preflight-test.py`, this
tool's executable contract; `self-test` loads it by a `__file__`-relative path and FAILS LOUDLY if it is
missing — a self-test that passes because it found no tests is not a passing gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "base-preflight-test.py"     # the fixture suite — this tool's executable contract


# --- the verdicts -------------------------------------------------------------
#
# `proceed` is the ONLY verdict that clears a review/fix onto this branch; `rebase-first` says the base is
# stale/conflicting and a rebase must land first; `recheck` is NEVER a verdict — it says the mergeability is
# not yet computed (or is a value nobody has classified), so re-poll and decide again. Every non-`proceed`
# outcome fails CLOSED: this tool would rather send the driver back to rebase or re-poll than wave a fix onto
# a base it could not confirm was current.
PROCEED = "proceed"
REBASE_FIRST = "rebase-first"
RECHECK = "recheck"


# --- the two GitHub enums, as data so `decide` reads ONE source, mapped TOTALLY -------------------
#
# The AUTHORITATIVE value sets are GitHub's schema, as `stage-3-merge.md` records them. This tool judges ONLY
# base-currency, so the two enums are crossed for exactly one question — is the branch current enough with
# its base? — never for merge-readiness (that is `merge-check.py`'s job, downstream, over the same enums).
# The mapping below is TOTAL over both sets: every value has a home, and a value in NEITHER set is one GitHub
# added since — the catch-all in `decide` re-polls it rather than guessing.
MERGEABLE_VALUES = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})
MERGE_STATE_STATUS_VALUES = frozenset(
    {"DIRTY", "UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS", "CLEAN"})

# The `mergeStateStatus` values that mean the base is CURRENT ENOUGH to author a review/fix on. UNSTABLE and
# BLOCKED are about CHECKS/PERMISSIONS, NOT a stale base — a non-passing check or a branch-protection block
# does not make the base moved-ahead, so a fix/review may proceed; this tool judges base-currency alone.
# DIRTY/BEHIND/UNKNOWN are deliberately ABSENT: each is handled by an earlier rule in `decide`.
BASE_CURRENT_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE", "BLOCKED"})


def _verdict(verdict: str, reason: str) -> dict:
    return {"verdict": verdict, "reason": reason}


def decide(view: dict) -> dict:
    """PURE. Return `{"verdict": ..., "reason": ...}` for one PR view. No I/O. Assumes a validated view (see
    `validate_view`), so `view["mergeable"]` and `view["mergeStateStatus"]` are present strings.

    The order is deliberate and FIRST-MATCHING-RULE-WINS, and it fails safe BEFORE it acts: an unrecognised
    value of EITHER enum re-polls first, then an uncomputed mergeability re-polls, and only then is a conflict
    or a moved base allowed to say `rebase-first`. This ordering is load-bearing — a view like
    `{mergeable: CONFLICTING, mergeStateStatus: UNKNOWN}` or `{..., mergeStateStatus: FROZEN}` (a value GitHub
    added since) must NOT be steered to `rebase-first` on the half of the view we DO recognise while the other
    half is uncomputed or unclassified. UNKNOWN/unrecognised WINS: re-poll and decide again on a full view,
    never guess. Only a fully computed, recognised, non-conflicting, current view reaches `proceed`.
    """
    mergeable = view["mergeable"]
    mss = view["mergeStateStatus"]

    # 1. UNRECOGNISED VALUE of EITHER enum — a value GitHub's schema does not declare (one it added since).
    #    Fail safe BEFORE any rebase/proceed decision: re-poll, never guess onto a value nobody classified.
    if mergeable not in MERGEABLE_VALUES or mss not in MERGE_STATE_STATUS_VALUES:
        unknown = mergeable if mergeable not in MERGEABLE_VALUES else mss
        return _verdict(RECHECK, f"unknown merge state {unknown} — re-poll, never guess")

    # 2. NOT COMPUTED YET — GitHub has not finished computing mergeability for EITHER enum. NEVER a verdict:
    #    re-poll. This wins over rebase-first: a recognised CONFLICTING/DIRTY/BEHIND on one half cannot decide
    #    the branch while the other half is still UNKNOWN.
    if mergeable == "UNKNOWN" or mss == "UNKNOWN":
        return _verdict(RECHECK, "mergeability not computed yet — re-poll")

    # 3. CONFLICTS WITH BASE — the branch cannot combine with its base. A fix authored here fights the merge.
    if mergeable == "CONFLICTING" or mss == "DIRTY":
        return _verdict(REBASE_FIRST, "conflicts with base — rebase before reviewing/fixing")

    # 4. BASE MOVED AHEAD — no conflict, but the base has commits this branch lacks; rebase to review the tip.
    if mss == "BEHIND":
        return _verdict(REBASE_FIRST, "base has moved ahead — rebase first")

    # 5. CURRENT WITH BASE — mergeable AND a base-current merge state. The one verdict that clears a fix.
    if mergeable == "MERGEABLE" and mss in BASE_CURRENT_STATES:
        return _verdict(PROCEED, "branch is current with base")

    # 6. DEFENSIVE CATCH-ALL — unreachable after step 1 pinned both enums to their schema sets and steps 2-5
    #    covered every recognised combination. Fail closed rather than fall off the end of the function.
    return _verdict(RECHECK, "mergeability not computed yet — re-poll")


# --- obtain the live PR view ---------------------------------------------------

VIEW_FIELDS = "mergeable,mergeStateStatus"

# Every field `decide` reads off the view, each a string. `validate_view` pins this at the boundary so
# `decide` may assume a shaped view and never raises `KeyError`/`TypeError` on a value the caller handed in.
_VIEW_STR_FIELDS = ("mergeable", "mergeStateStatus")


class ViewError(Exception):
    """The live PR view could not be obtained. The decision fails CLOSED — never `proceed`."""


def validate_view(view: object) -> "str | None":
    """`None` if `view` is a JSON object carrying `mergeable` and `mergeStateStatus` as strings; otherwise a
    short description of the FIRST thing wrong. PURE — no I/O. The CLI turns a non-`None` result into a
    fail-closed `recheck`, so `decide` is never reached with a malformed view (learned from a prior tool that
    could KeyError past its boundary)."""
    if not isinstance(view, dict):
        return f"view is not a JSON object (got {type(view).__name__})"
    for name in _VIEW_STR_FIELDS:
        if name not in view:
            return f"missing field {name!r}"
        # bool is a subclass of int, not str, so a JSON string is the only thing that passes here.
        if not isinstance(view[name], str):
            return f"field {name!r} must be a string, got {type(view[name]).__name__}"
    return None


def load_view(pr: str, repo: "str | None", view_json: "str | None",
              project_root: "str | None") -> dict:
    """The PR's live view — from a recorded `gh pr view` JSON (`--view-json`, testable without gh) or from
    `gh pr view` itself. Any failure raises `ViewError`, which the caller turns into a fail-closed `recheck`.
    """
    if view_json is not None:
        try:
            return json.loads(Path(view_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ViewError(str(exc)) from exc
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", repo]
    argv += ["--json", VIEW_FIELDS]
    proc = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, check=False, cwd=project_root)
    if proc.returncode != 0:
        raise ViewError(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ViewError(f"gh response is not JSON ({exc})") from exc


def check(pr: str, repo: "str | None", view_json: "str | None",
          project_root: "str | None") -> int:
    """Fetch the live view, decide, print the verdict as JSON. EXIT 0 only on `proceed`; every other outcome
    (rebase-first, recheck, an unfetchable or malformed view) exits non-zero so a caller can gate on `$?`."""
    try:
        view = load_view(pr, repo, view_json, project_root)
    except ViewError as exc:
        print(json.dumps(_verdict(RECHECK, f"could not fetch PR view: {exc}")))
        return 1
    # A syntactically valid but INCOMPLETE/WRONG-TYPED view must fail CLOSED here, never crash `decide` with a
    # KeyError/TypeError and never say `proceed`. Mirrors the fetch-failure recheck above.
    problem = validate_view(view)
    if problem is not None:
        print(json.dumps(_verdict(RECHECK, f"malformed PR view: {problem}")))
        return 1
    result = decide(view)
    print(json.dumps(result))
    return 0 if result["verdict"] == PROCEED else 1


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run and CANNOT report "
            f"health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("base_preflight_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the base-preflight decider's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:30} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:30} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:30} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the base-preflight decider's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the base-preflight decider's contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="decide base-currency for one PR from its live view")
    c.add_argument("--pr", required=True)
    c.add_argument("--repo", help="owner/name (default: the current checkout's)")
    c.add_argument("--view-json", help="a recorded `gh pr view` JSON — decide without calling gh")
    c.add_argument("--project-root", help="run `gh pr view` with this as its working directory")

    sub.add_parser("self-test", help="run every fixture (base-preflight-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    return check(args.pr, args.repo, args.view_json, args.project_root)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
