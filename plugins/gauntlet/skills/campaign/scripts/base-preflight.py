#!/usr/bin/env python3
"""DECIDE whether a PR's branch is current with its base BEFORE a review or a fix is authored on it.

This enforces the rebase-before-review/fix precondition `stage-2-review-gate.md` states in prose: if a PR
conflicts with `<base>` or lacks its refreshed tip, rebase it before reviewing or fixing. GitHub may still
report MERGEABLE/CLEAN after another campaign PR advances an unprotected base, so the check combines its
merge states with fetched Git ancestry. A fix authored on a stale/conflicting base is wasted — it is
re-reviewed against the rebased tip anyway. This turns the prose into an enforced check.

It DECIDES one of `proceed` / `rebase-first` / `recheck` / `park` from the live PR view plus fetched base ancestry
and PERFORMS NO REBASE: the driver rebases when told `rebase-first`, then re-runs this. Deciding and doing
are two jobs and this is only the first — nothing here runs `git rebase`/`git merge` or edits a branch.

    base-preflight.py check --pr 31 [--repo owner/name] [--view-json <path>] [--project-root <dir>]
        [--worktree <path> --base <branch> [--remote origin]] [--file <ledger>]
    base-preflight.py self-test   run every fixture (base-preflight-test.py)

When `--file <ledger>` is given, the ledger ROW owns the base: this resolves the selected PR row's
`effective_base` (its explicit `base_branch`, else the legacy header fallback, through `ledger.py`), and any
`--base` argument becomes an ASSERTION that must equal it — not an independent base source. It also compares
the live PR view's `baseRefName` with that effective base and REFUSES `proceed` when they differ: the PR was
retargeted to a different branch NAME, an unsupported mid-run change, so it fails closed to `recheck` with the
same `BASE_CHANGE_PARK_REASON` wording a re-adoption/reconcile park records and the driver parks the row. (A
base that merely ADVANCED — same branch, new commits — is NOT a retarget; that stays the ancestry
`rebase-first` below.)

With `--file <ledger>`, a final `proceed` records that base check through `ledger.py base-ok`, while `park`
records an unrecognized enum through `ledger.py park`. The latter is the existing machine-blocker transition:
it atomically sets `status = awaiting-user`, names the unrecognized value in `ci_reason`, and clears
`blocker_ruling`. Without `--file` the tool is the pure decider it always was (it writes nothing, and `--base`
is the base it fetches); `decide()` itself stays pure regardless.

The verdict is printed as JSON on stdout, and the EXIT CODE gates a caller's `$?`: 0 for `proceed`, non-zero
for `rebase-first`, `recheck`, and `park` (and for a view that could not be fetched or was malformed — those fail
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
LEDGER = _HERE / "ledger.py"                   # the sibling that owns base_ok_sha; `base-ok` is its only writer
PR_ADOPT = _HERE / "pr-adopt.py"               # owns BASE_CHANGE_PARK_REASON — reused, never re-spelt here


def _load_ledger():
    mod = load_module_from_path("base_preflight_ledger", LEDGER)
    if mod is None:
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER}")
    return mod


L = _load_ledger()

# Loaded LAZILY (only when a live retarget is found) so the pure decider and self-test never pull the
# adoption module chain: pr-adopt.py OWNS `BASE_CHANGE_PARK_REASON`, the EXACT machine-blocker wording a
# re-adoption/reconcile park records. A preflight retarget-refusal reuses that one owner so the reason reads
# identically everywhere — never a second copy of the string here.
_PA = None


def _base_change_reason(recorded: str, live: str) -> str:
    global _PA
    if _PA is None:
        _PA = load_module_from_path("base_preflight_pr_adopt", PR_ADOPT)
        if _PA is None:
            raise RuntimeError(f"cannot load pr-adopt.py for the base-change reason at {PR_ADOPT}")
    return _PA.BASE_CHANGE_PARK_REASON.format(recorded=recorded, live=live)


# --- the verdicts -------------------------------------------------------------
#
# `proceed` is the ONLY verdict that clears a review/fix onto this branch; `rebase-first` says the base is
# stale/conflicting and a rebase must land first; `recheck` is NEVER a verdict — it says the mergeability is
# not yet computed, so re-poll and decide again; `park` says an enum value is unrecognized and records the
# existing machine-blocker transition when a ledger is supplied. Every non-`proceed` outcome fails CLOSED.
PROCEED = "proceed"
REBASE_FIRST = "rebase-first"
RECHECK = "recheck"
PARK = "park"


# --- the two GitHub enums, as data so `decide` reads ONE source, mapped TOTALLY -------------------
#
# The AUTHORITATIVE value sets are GitHub's schema, as `stage-3-merge.md` records them. The two enums are
# crossed only as a pre-screen before the Git ancestry check, never for merge-readiness (that is
# `merge-check.py`'s job, downstream, over the same enums). The mapping below is TOTAL over both sets: every
# value has a home, and a value in NEITHER set is one GitHub added since — the catch-all in `decide` parks it
# through the machine-blocker path rather than guessing or repeatedly polling a stable unknown value.
MERGEABLE_VALUES = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})
MERGE_STATE_STATUS_VALUES = frozenset(
    {"DIRTY", "UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS", "CLEAN"})

# The `mergeStateStatus` values that pass the ENUM screen. UNSTABLE and BLOCKED are about
# CHECKS/PERMISSIONS, not a stale base. This is not proof the branch contains the refreshed base; the graph
# check in `check` supplies that proof. DIRTY/BEHIND/UNKNOWN are deliberately ABSENT: each is handled by an
# earlier rule in `decide`. NOTE: the downstream merge gate (`merge-check.py`) now runs this SAME
# `check_base_ancestry` probe on BLOCKED before it parks — a BLOCKED PR that is only behind its base rebases
# rather than escalating — so both gates treat BLOCKED identically (enum screen, then graph proof).
BASE_CURRENT_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE", "BLOCKED"})


def _verdict(verdict: str, reason: str) -> dict:
    return {"verdict": verdict, "reason": reason}


def check_base_ancestry(worktree: "str | None", base: "str | None", remote: str) -> tuple[str, str]:
    """Return ``current``, ``stale``, or ``unverified`` for the fetched base against a PR worktree.

    GitHub may keep reporting ``MERGEABLE/CLEAN`` after another campaign PR advances an unprotected base.
    The merge-state enums alone therefore cannot prove that a candidate contains the current base. Fetch the
    named base through a fully qualified source:tracking-ref refspec and ask Git's ancestry graph directly;
    this updates only the remote-tracking ref, never the candidate branch. The qualified source prevents a
    legal dash-leading base name from being parsed as a Git option. Callers treat ``unverified`` as
    fail-closed.
    """
    if not worktree or not base:
        return "unverified", "base ancestry requires --worktree and --base"
    tracking_refspec = f"refs/heads/{base}:refs/remotes/{remote}/{base}"
    fetch = subprocess.run(  # noqa: S603
        ["git", "-C", worktree, "fetch", remote, tracking_refspec],
        capture_output=True, text=True, check=False)
    if fetch.returncode != 0:
        return "unverified", f"could not fetch {remote}/{base}: {fetch.stderr.strip()}"
    probe = subprocess.run(  # noqa: S603
        ["git", "-C", worktree, "merge-base", "--is-ancestor", f"{remote}/{base}", "HEAD"],
        capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        return "current", ""
    if probe.returncode == 1:
        return "stale", ""
    return "unverified", f"could not compare HEAD with {remote}/{base}: {probe.stderr.strip()}"


def decide(view: dict) -> dict:
    """PURE. Return `{"verdict": ..., "reason": ...}` for one PR view. No I/O. Assumes a validated view (see
    `validate_view`), so `view["mergeable"]` and `view["mergeStateStatus"]` are present strings.

    The order is deliberate and FIRST-MATCHING-RULE-WINS, and it fails safe BEFORE it acts: an unrecognised
    value of EITHER enum parks first, then an uncomputed mergeability re-polls, and only then is a conflict
    or a moved base allowed to say `rebase-first`. This ordering is load-bearing — a view like
    `{mergeable: CONFLICTING, mergeStateStatus: UNKNOWN}` or `{..., mergeStateStatus: FROZEN}` (a value GitHub
    added since) must NOT be steered to `rebase-first` on the half of the view we DO recognise while the other
    half is uncomputed or unclassified. UNKNOWN re-polls; unrecognised parks. A fully computed, recognised,
    non-conflicting view reaches preliminary `proceed`; `check` then verifies the fetched base graph before
    emitting final `proceed`.
    """
    mergeable = view["mergeable"]
    mss = view["mergeStateStatus"]

    # 1. UNRECOGNISED VALUE of EITHER enum — a value GitHub's schema does not declare (one it added since).
    #    Fail safe BEFORE any rebase/proceed decision: park through the machine-blocker path.
    if mergeable not in MERGEABLE_VALUES:
        return _verdict(PARK, f"unknown mergeable value {mergeable} — park")
    if mss not in MERGE_STATE_STATUS_VALUES:
        return _verdict(PARK, f"unknown merge state {mss} — park")

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

    # 5. ENUM SCREEN PASSES — the graph check in `check` decides whether this becomes final `proceed`.
    if mergeable == "MERGEABLE" and mss in BASE_CURRENT_STATES:
        return _verdict(PROCEED, "GitHub merge state permits base check")

    # 6. DEFENSIVE CATCH-ALL — unreachable after step 1 pinned both enums to their schema sets and steps 2-5
    #    covered every recognised combination. Fail closed rather than fall off the end of the function.
    return _verdict(RECHECK, "mergeability not computed yet — re-poll")


# --- obtain the live PR view ---------------------------------------------------

VIEW_FIELDS = "mergeable,mergeStateStatus,baseRefName"

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
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, cwd=project_root)
    except OSError as exc:
        # subprocess.run RAISES before it ever produces a returncode when the executable is missing
        # (FileNotFoundError) or `--project-root` is not a usable directory (NotADirectoryError) — both are
        # OSError. Uncaught, that prints a traceback and never fails closed; turn it into a ViewError so the
        # caller emits a structured `recheck` and exits non-zero, exactly like a non-zero `gh` exit.
        raise ViewError(f"could not run `gh pr view {pr}`: {exc}") from exc
    if proc.returncode != 0:
        raise ViewError(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ViewError(f"gh response is not JSON ({exc})") from exc


def record_base_ok(ledger_file: str, pr: str, worktree: "str | None") -> "str | None":
    """On a `proceed`, stamp the ledger's `base_ok_sha` for the worktree's CURRENT head. Returns an error
    string on failure, else `None`. Called ONLY from `check`'s proceed branch, so it never touches `decide`.

    A `proceed` clears a review or fix onto THIS head, and the stamp is what lets the later `ledger.py verdict`
    land: that door refuses unless `base_ok_sha == head_sha`. Resolving the head HERE (never trusting a caller
    value) and writing through `ledger.py base-ok` (the only sanctioned writer) keeps the stamp a byproduct of
    the check actually reaching `proceed`. A `proceed` guarantees the ancestry check ran, so `worktree` is set;
    the guard is kept anyway so a caller change cannot silently reach a `rev-parse` with no worktree.
    """
    if not worktree:
        return "proceed reached with no --worktree, so the head to stamp cannot be resolved"
    head = subprocess.run(  # noqa: S603
        ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        return f"could not resolve HEAD in {worktree}: {head.stderr.strip()}"
    sha = head.stdout.strip()
    stamp = subprocess.run(  # noqa: S603
        [sys.executable, str(LEDGER), "--file", ledger_file, "base-ok", "--pr", str(pr), "--head-sha", sha],
        capture_output=True, text=True, check=False)
    if stamp.returncode != 0:
        return f"`ledger.py base-ok` exited {stamp.returncode}: {stamp.stderr.strip()}"
    return None


def record_park(ledger_file: str, pr: str, reason: str) -> "str | None":
    """Record an unrecognized enum through `ledger.py park`, the machine-blocker transition owner."""
    parked = subprocess.run(  # noqa: S603
        [sys.executable, str(LEDGER), "--file", ledger_file, "park", "--pr", str(pr), "--reason", reason],
        capture_output=True, text=True, check=False)
    if parked.returncode != 0:
        return f"`ledger.py park` exited {parked.returncode}: {parked.stderr.strip()}"
    return None


def resolve_ledger_base(ledger_file: str, pr: str, base_arg: "str | None",
                        view: dict) -> "tuple[str | None, dict | None]":
    """When a ledger is named the ROW owns the base — `--base` is only an assertion, never a base source.

    Load the row for `pr`, resolve its `effective_base` (its explicit `base_branch`, else the legacy header
    fallback, through `ledger.py`'s accessor — never a second copy of that rule), then fail CLOSED unless:
    the row exists and has a usable base; any `--base` given agrees with it (`ledger.py`'s `base_agrees` —
    the one owner of that comparison); and the PR's LIVE `baseRefName` still equals it. A live mismatch is an unsupported retarget: it
    routes the SAME `BASE_CHANGE_PARK_REASON` wording a re-adoption/reconcile park records, so the driver
    parks the row through the existing machine-blocker path. (A base that merely ADVANCED — same branch NAME,
    new commits — is NOT this: it is the ancestry `rebase-first` in `check`.) Returns `(effective_base, None)`
    to continue with that base, or `(None, verdict)` — always a `recheck`, never `proceed` — to refuse."""
    try:
        header, rows = L.load(Path(ledger_file))
    except SystemExit as exc:
        return None, _verdict(RECHECK, f"could not read ledger {ledger_file}: {exc}")
    row = L.find_row(rows, str(pr))
    if row is None:
        return None, _verdict(RECHECK, f"no ledger row for pr {pr} — its base cannot be resolved")
    effective_base, base_problem = L.require_effective_base(header, row, pr)
    if base_problem is not None:
        return None, _verdict(RECHECK, base_problem)
    if base_arg is not None and not L.base_agrees(base_arg, effective_base):
        return None, _verdict(
            RECHECK, f"--base {base_arg!r} disagrees with pr {pr}'s ledger effective base "
                     f"{effective_base!r} — --base is an assertion, not a base source")
    live = view.get("baseRefName")
    if not isinstance(live, str) or not live:
        return None, _verdict(RECHECK, "malformed PR view: missing baseRefName, so the live base is unknown")
    if live != effective_base:
        return None, _verdict(RECHECK, _base_change_reason(effective_base, live))
    return effective_base, None


def check(pr: str, repo: "str | None", view_json: "str | None", project_root: "str | None",
          worktree: "str | None", base: "str | None", remote: str, ledger_file: "str | None") -> int:
    """Fetch the live view, decide, print the verdict as JSON. EXIT 0 only on `proceed`; every other outcome
    (rebase-first, recheck, park, an unfetchable or malformed view) exits non-zero so a caller can gate on
    `$?`.

    When `--file` is given, a final `proceed` records `base_ok_sha` for the live head, while `park` records
    the machine-blocker transition. A recording failure fails CLOSED to `recheck`. Without `--file`, nothing
    is written."""
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
    # When a ledger is named, the ROW owns the base: resolve `effective_base`, assert any `--base` matches it,
    # and refuse (fail CLOSED to `recheck`) if the PR's live target no longer equals it. On success `base` is
    # the row's effective base, so the ancestry fetch below measures against the base the row actually tracks
    # — never a `--base` a caller could have handed in disagreeing with the ledger.
    if ledger_file is not None:
        base, refusal = resolve_ledger_base(ledger_file, pr, base, view)
        if refusal is not None:
            print(json.dumps(refusal))
            return 1
    result = decide(view)
    # An unrecognized enum is stable input, not transient UNKNOWN. Stage 3 must park it BEFORE leaving the
    # candidate, using the same ledger-owned machine-blocker transition as merge-check's park action.
    if result["verdict"] == PARK and ledger_file is not None:
        err = record_park(ledger_file, pr, result["reason"])
        if err is not None:
            result = _verdict(RECHECK, f"could not record machine-blocker park: {err}")
    if result["verdict"] == PROCEED:
        ancestry, detail = check_base_ancestry(worktree, base, remote)
        if ancestry == "stale":
            result = _verdict(REBASE_FIRST, "base has moved ahead — rebase first")
        elif ancestry == "unverified":
            result = _verdict(RECHECK, f"could not verify base ancestry: {detail}")
    # ONLY on a final `proceed`, and ONLY when a ledger was named, record the proceed as `base_ok_sha` for the
    # live head. Fail CLOSED to `recheck` on a recording failure so a `proceed` always implies the stamp landed.
    if result["verdict"] == PROCEED and ledger_file is not None:
        err = record_base_ok(ledger_file, pr, worktree)
        if err is not None:
            result = _verdict(RECHECK, f"could not record base_ok_sha: {err}")
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
    c.add_argument("--worktree", help="the PR-head worktree used for the Git ancestry check")
    c.add_argument("--base", help="the PR base branch to fetch and compare; with --file it is an ASSERTION "
                                  "that must equal the row's effective base, not an independent base source")
    c.add_argument("--remote", default="origin", help="the worktree remote holding the base (default: origin)")
    c.add_argument("--file", help="the ledger (state.jsonl); resolves the row's effective base (asserting "
                                  "--base and refusing a live retarget), and records the final decision: "
                                  "`proceed` records base_ok_sha for the live head so `ledger.py verdict` "
                                  "can later count; `park` records the ledger-owned machine blocker. "
                                  "Absent: the pure decider, no write, --base is the base it fetches")

    sub.add_parser("self-test", help="run every fixture (base-preflight-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    return check(args.pr, args.repo, args.view_json, args.project_root,
                 args.worktree, args.base, args.remote, args.file)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
