#!/usr/bin/env python3
"""Fixtures for `merge-check.py` — the merge-readiness decider.

They live in a SIBLING file, and `merge-check.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. It asserts the EXACT verdict AND, where the wording is load-bearing, the EXACT
reason — a suite that only checked `verdict == "not-yet"` would pass against a decider that returned the
wrong reason, and the reason is what the driver acts on. The ordering fixtures (held/stale/ci over the
enums) pin FIRST-FAILING-CHECK-WINS: a fully clean+green view that still returns not-yet proves the earlier
check outranks the enums.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "merge-check.py"


def _load_owner():
    mod = load_module_from_path("merge_check_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the merge-readiness decider at {OWNER}")
    return mod


M = _load_owner()
L = M.L

SHA_A = "a" * 40   # the reviewed head
SHA_B = "b" * 40   # a head the tip has moved to


def row(*, status="in_review", head_sha=SHA_A, ci="green", tier="HIGH", reviews_ok=2,
        base_branch="-") -> dict:
    r = dict(L.ROW_DEFAULTS)
    r.update(pr="9", status=status, head_sha=head_sha, ci=ci, tier=tier, reviews_ok=str(reviews_ok),
             base_branch=base_branch)
    r["id"] = "pr9"
    return r


def view(*, mergeable="MERGEABLE", mergeStateStatus="CLEAN", isDraft=False, state="OPEN",
         headRefOid=SHA_A, baseRefName="main") -> dict:
    return {"mergeable": mergeable, "mergeStateStatus": mergeStateStatus, "isDraft": isDraft,
            "state": state, "headRefOid": headRefOid, "baseRefName": baseRefName}


# The header carries base_branch "main", so a `-` row inherits "main" and matches view()'s default
# baseRefName — the effective base every fixture below decides under, unless it sets one explicitly.
_HEADER = {"base_branch": "main"}


def decide(r: dict, v: dict) -> dict:
    return M.decide(r, v, required=M.REQUIRED, effective_base=L.effective_base(_HEADER, r))


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


def expect(r: dict, v: dict, verdict: str, reason: "str | None" = None) -> None:
    got = decide(r, v)
    check(got["verdict"] == verdict, f"expected verdict {verdict!r}, got {got!r}")
    if reason is not None:
        check(got["reason"] == reason, f"expected reason {reason!r}, got {got['reason']!r}")


# --- merge verdicts -----------------------------------------------------------

def t_clean_and_all_met():
    expect(row(), view(), "merge", "")


def t_has_hooks_merges():
    expect(row(), view(mergeStateStatus="HAS_HOOKS"), "merge", "")


# --- held FREEZES, whatever the counters or enums say -------------------------

def t_held_never_merges():
    # A fully clean+green+2/2 view — the ONLY thing stopping the merge is the held status.
    for status in (L.REPAIR_STATUS, "awaiting-user", "awaiting-api"):
        expect(row(status=status), view(), "not-yet", f"held ({status})")


# --- the status ALLOW-LIST: only `in_review` is a merge candidate -------------

def t_terminal_status_never_merges():
    # THE REPRO: a TERMINAL row (aborted/merged) with a fully clean+green+2/2 view and a matching SHA once
    # printed `{"verdict":"merge"}` — it would have merged an aborted PR. The allow-list parks anything that
    # is not `in_review`, naming the offending status so the reason is actionable. `merged` is the same trap.
    for status in ("aborted", "merged"):
        expect(row(status=status), view(), "not-yet", f"row status is {status}, not in_review")


def t_unexpected_status_never_merges():
    # An ALLOW-LIST, not a reject-list: a status nobody enumerated (a future one, a typo) STILL parks — it is
    # not `in_review`, so it never reaches a merge verdict, whatever the counters and enums say.
    expect(row(status="quarantined"), view(), "not-yet", "row status is quarantined, not in_review")


def t_in_review_is_the_one_that_merges():
    # The allow-list did NOT break the happy path: an `in_review` row with every precondition met + CLEAN
    # still merges. (row()'s default status is already in_review; state it explicitly for this fixture.)
    expect(row(status="in_review"), view(), "merge", "")


# --- ledger preconditions, in order -------------------------------------------

def t_not_open():
    expect(row(), view(state="MERGED"), "not-yet", "pr is MERGED, not open")
    expect(row(), view(state="CLOSED"), "not-yet", "pr is CLOSED, not open")


def t_draft():
    expect(row(), view(isDraft=True), "not-yet", "draft — park awaiting-user")


def t_stale_sha():
    # ci green + CLEAN, but the tip moved — stale SHA outranks ci and the enums.
    got = decide(row(head_sha=SHA_A), view(headRefOid=SHA_B))
    check(got["verdict"] == "not-yet", f"a moved head must not merge, got {got!r}")
    check("moved off the reviewed SHA" in got["reason"] and SHA_B[:7] in got["reason"]
          and SHA_A[:7] in got["reason"], f"the stale-SHA reason must name both short SHAs, got {got!r}")


def t_ci_not_green():
    # CLEAN view, but ci is not green — ci is checked before the enums, and mergeStateStatus NEVER feeds ci.
    expect(row(ci="pending"), view(), "not-yet", "ci is pending, not green")
    expect(row(ci="red"), view(), "not-yet", "ci is red, not green")


def t_short_reviews():
    # 1 of 2 for a HIGH PR — short of required(tier).
    expect(row(tier="HIGH", reviews_ok=1), view(), "not-yet", "1 of 2 approvals")
    # TRIVIAL needs only 1 — pins required(tier): 1/1 + CLEAN merges.
    expect(row(tier="TRIVIAL", reviews_ok=1), view(), "merge", "")
    # ...and a TRIVIAL PR with 0 verdicts is still short.
    expect(row(tier="TRIVIAL", reviews_ok=0), view(), "not-yet", "0 of 1 approvals")


# --- the two GitHub enums, TOTALLY --------------------------------------------

def t_mergestate_behind():
    expect(row(), view(mergeStateStatus="BEHIND"), "not-yet", "base moved ahead — rebase")


def t_mergestate_dirty():
    expect(row(), view(mergeStateStatus="DIRTY"), "not-yet", "conflicts — rebase")


def t_mergestate_unstable():
    expect(row(), view(mergeStateStatus="UNSTABLE"), "not-yet",
           "a check is non-passing (may still be running) — not campaign's ci signal")


def t_mergestate_blocked():
    # BLOCKED is NO LONGER a final park at decide-level: `decide` returns the PROBE marker (it cannot tell a
    # human/ruleset block from a merely-behind base without I/O), and `check` resolves it via the base-ancestry
    # probe. The carried reason is the eventual park reason, used only when the probe proves the base current.
    expect(row(), view(mergeStateStatus="BLOCKED"), M.PROBE, M.BLOCKED_PARK_REASON)


def t_mergestate_unknown():
    expect(row(), view(mergeStateStatus="UNKNOWN"), "not-yet", "merge state not computed yet — re-poll")


def t_conflicting_mergeable():
    # CONFLICTING is decided on .mergeable alone; .mergeStateStatus is not even consulted.
    expect(row(), view(mergeable="CONFLICTING", mergeStateStatus="CLEAN"), "not-yet",
           "conflicts with base — rebase")


def t_unknown_mergeable():
    expect(row(), view(mergeable="UNKNOWN", mergeStateStatus="CLEAN"), "not-yet",
           "mergeability not computed yet — re-poll")


def t_unknown_mergestate_value_parks():
    # A value GitHub's schema does not declare — the catch-all parks it, never guesses. Pins TOTALITY.
    expect(row(), view(mergeStateStatus="FROZEN"), "not-yet", "unknown merge state FROZEN — park, never guess")


def t_unknown_mergeable_value_parks():
    expect(row(), view(mergeable="WOBBLY"), "not-yet", "unknown mergeable value WOBBLY — park")


# --- CLI: a recorded view makes `check` testable without gh --------------------

def t_cli_injected_view():
    real_check = M.B.check_base_ancestry
    M.B.check_base_ancestry = lambda *_args: ("current", "")
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1", base_branch="main"), [row()])
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view()), encoding="utf-8")
            code, out, err = capture_cli(
                M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
    finally:
        M.B.check_base_ancestry = real_check
    check(code == 0, f"the CLI must exit 0 on a computed verdict (stderr: {err})")
    check(json.loads(out) == {"verdict": "merge", "reason": ""},
          f"the CLI should print the merge verdict, got {out!r}")


def t_cli_stale_base_blocks_merge():
    """The final gate repeats base ancestry before it turns a CLEAN candidate into a merge."""
    real_check = M.B.check_base_ancestry
    M.B.check_base_ancestry = lambda *_args: ("stale", "")
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1", base_branch="main"), [row()])
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view()), encoding="utf-8")
            code, out, err = capture_cli(
                M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
    finally:
        M.B.check_base_ancestry = real_check
    check(code == 0, f"a stale base is a computed not-yet result (stderr: {err})")
    check(json.loads(out) == {"verdict": "not-yet", "reason": "base moved ahead — rebase"},
          f"a CLEAN candidate behind the base must not merge, got {out!r}")


def t_cli_unverified_base_blocks_merge():
    """The final gate fails closed when it cannot read the candidate's base ancestry."""
    real_check = M.B.check_base_ancestry
    M.B.check_base_ancestry = lambda *_args: ("unverified", "could not fetch origin/main: unavailable")
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1", base_branch="main"), [row()])
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view()), encoding="utf-8")
            code, out, err = capture_cli(
                M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
    finally:
        M.B.check_base_ancestry = real_check
    check(code != 0, f"unverified base ancestry must fail closed (stderr: {err})")
    check(json.loads(out) == {
        "verdict": "not-yet",
        "reason": "could not verify base ancestry: could not fetch origin/main: unavailable",
    }, f"an unverified base must block merge, got {out!r}")


def _cli_blocked(ancestry: tuple) -> "tuple[int, str, str]":
    """Drive the real CLI over a BLOCKED view with a patched base-ancestry probe. BLOCKED routes to the PROBE
    sentinel, so `check` resolves it HERE on `ancestry` — this exercises that resolution end to end."""
    real_check = M.B.check_base_ancestry
    M.B.check_base_ancestry = lambda *_args: ancestry
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1", base_branch="main"), [row()])
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view(mergeStateStatus="BLOCKED")), encoding="utf-8")
            return capture_cli(
                M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
    finally:
        M.B.check_base_ancestry = real_check


def t_cli_blocked_behind_rebases():
    """THE #134 REGRESSION TEST: a BLOCKED PR that is BEHIND its base must route to REBASE, not park. Run
    against the OLD code (BLOCKED -> final park) this parked awaiting-user; now it must emit the rebase reason
    Stage 3 routes on to clean-rebase.py, so the verdicts carry forward and the PR merges."""
    code, out, err = _cli_blocked(("stale", ""))
    check(code == 0, f"a behind BLOCKED PR is a computed not-yet result (stderr: {err})")
    check(json.loads(out) == {"verdict": "not-yet", "reason": "base moved ahead — rebase"},
          f"a BLOCKED PR behind its base must route to rebase, not park, got {out!r}")


def t_cli_blocked_current_parks():
    """A BLOCKED PR proven up-to-date is a genuine human/ruleset block — it still parks awaiting-user."""
    code, out, err = _cli_blocked(("current", ""))
    check(code == 0, f"an up-to-date BLOCKED PR is a computed not-yet result (stderr: {err})")
    check(json.loads(out) == {"verdict": "not-yet", "reason": "GitHub says BLOCKED — park awaiting-user"},
          f"a BLOCKED PR that is up to date must still park, got {out!r}")


def t_cli_blocked_unverified_leaves():
    """A BLOCKED PR whose base ancestry cannot be read fails CLOSED: leave/re-poll (exit 1), never invent a
    merge and never park on a transient. Mirrors the CLEAN-path unverified fixture."""
    code, out, err = _cli_blocked(("unverified", "could not fetch origin/main: unavailable"))
    check(code != 0, f"unverifiable ancestry on a BLOCKED PR must fail closed (stderr: {err})")
    check(json.loads(out) == {
        "verdict": "not-yet",
        "reason": "could not verify base ancestry: could not fetch origin/main: unavailable",
    }, f"an unverifiable BLOCKED PR must leave/re-poll, never merge or park, got {out!r}")


def t_cli_no_ledger_row():
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1"), [row()])  # holds pr 9, not pr 42
        code, out, _err = capture_cli(
            M.main, ["check", "--pr", "42", "--file", str(led), "--view-json", str(led)])
        check(code == 0, "a PR with no ledger row is a computed not-yet, not an error")
        check(json.loads(out) == {"verdict": "not-yet", "reason": "no ledger row"},
              f"a missing row must decide `no ledger row` without ever reading the view, got {out!r}")


def _cli_malformed_view(bad_view: dict, missing_or_wrong: str) -> None:
    """Drive the real CLI with a syntactically valid but malformed `--view-json`. It must fail CLOSED: a
    structured not-yet naming the malformed view, a NON-ZERO exit, and NO traceback — never a KeyError and
    never `merge`."""
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1"), [row()])
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps(bad_view), encoding="utf-8")
        code, out, err = capture_cli(
            M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
        check(code != 0, f"a malformed view must exit non-zero (fail closed), got {code} (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "not-yet",
              f"a malformed view must decide not-yet, never merge, got {result!r}")
        check(result["reason"].startswith("malformed PR view:"),
              f"the reason must name the malformed view, got {result['reason']!r}")
        check(missing_or_wrong in result["reason"],
              f"the reason must say what is wrong ({missing_or_wrong!r}), got {result['reason']!r}")


def t_cli_view_missing_field():
    # A valid JSON object MISSING mergeStateStatus — decide() would KeyError on it; the boundary parks it.
    v = view()
    del v["mergeStateStatus"]
    _cli_malformed_view(v, "mergeStateStatus")


def t_cli_view_wrong_type_field():
    # isDraft handed in as a STRING, not a bool — a wrong JSON type must fail closed like a missing field.
    v = view()
    v["isDraft"] = "false"
    _cli_malformed_view(v, "isDraft")


def t_cli_gh_spawn_failure():
    # gh ABSENT (or not executable): subprocess.run raises OSError BEFORE any returncode. It must fail
    # CLOSED through the one `except ViewError` — a structured not-yet, a NON-ZERO exit, and NO traceback —
    # never an uncaught FileNotFoundError with no verdict on stdout. Simulate the spawn failure by patching
    # the module's subprocess.run; drive the real CLI with NO --view-json so it takes load_view's gh path.
    def boom(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "gh")
    real_run = M.subprocess.run
    M.subprocess.run = boom
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1"), [row()])
            # capture_cli only catches SystemExit, so an uncaught spawn traceback would ESCAPE here and fail
            # this fixture — that is the teeth.
            code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--file", str(led), "--repo", "o/n"])
    finally:
        M.subprocess.run = real_run
    check(code != 0, f"a gh-spawn failure must exit non-zero (fail closed), got {code} (stderr: {err})")
    result = json.loads(out)
    check(result["verdict"] == "not-yet",
          f"a gh-spawn failure must decide not-yet, never merge, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the failed view fetch, got {result['reason']!r}")


def t_cli_unresolved_base_never_merges():
    """THE FINDING #2 REGRESSION TEST: a both-`-` ledger (header AND row base_branch='-', so effective_base
    resolves to '-') with a CLEAN/MERGEABLE/OPEN view whose baseRefName='-' must NEVER merge — even when the
    base-ancestry probe would report `current` (a git branch literally named '-' that HEAD descends from).
    The '-' == '-' coincidence slips `decide`'s retarget check, so the guard MUST be the shared
    `require_effective_base` fail-closed at the top of `check`, before `decide` and before the probe. Run
    against the pre-fix code (which resolved the base through the raw `effective_base` accessor with no
    refusal) this printed `{"verdict":"merge"}` at exit 0 — a false permissive at the merge gate."""
    real_check = M.B.check_base_ancestry
    # `current` is the WORST case: even if the probe would clear, the unresolved base must still block merge.
    M.B.check_base_ancestry = lambda *_args: ("current", "")
    try:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "state.jsonl"
            # header base '-' too, so a `-` row's effective base stays the unresolved '-' sentinel.
            L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1", base_branch="-"), [row(base_branch="-")])
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view(baseRefName="-")), encoding="utf-8")
            code, out, err = capture_cli(
                M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
    finally:
        M.B.check_base_ancestry = real_check
    check(code != 0, f"an unresolved base must fail closed (exit non-zero), got {code} (stderr: {err})")
    result = json.loads(out)
    check(result["verdict"] == "not-yet",
          f"an unresolved base must decide not-yet, NEVER merge, got {result!r}")
    check(result["reason"] == "pr 9 has no usable effective base in the ledger",
          f"the reason must be require_effective_base's ready-to-emit refusal, got {result['reason']!r}")


def t_base_retarget_parks():
    # A fully clean+green+in_review PR whose live base no longer matches its recorded (effective) base. The
    # recorded base is IMMUTABLE; a retarget is unsupported and parks with the SAME machine-blocker wording
    # every base door records. This fixture pins that the merge door catches it and uses the shared reason.
    expected = M.PA.BASE_CHANGE_PARK_REASON.format(recorded="v3", live="main")
    expect(row(base_branch="v3"), view(baseRefName="main"), "not-yet", expected)


def t_base_advance_is_not_a_retarget():
    # SAME base name on both sides — this is NOT a retarget, so the base step falls through to the enums and
    # (with a CLEAN view) reaches merge. A base that ADVANCED with new commits is the ancestry probe's job,
    # not this equality check. Guards against over-firing the retarget park on a normal base advance.
    expect(row(base_branch="v3"), view(baseRefName="v3"), "merge", "")


CASES = [
    ("clean-all-met", "CLEAN + every precondition met -> merge", t_clean_and_all_met),
    ("has-hooks", "HAS_HOOKS -> merge", t_has_hooks_merges),
    ("base-retarget-parks", "a live base retarget parks with the shared reason", t_base_retarget_parks),
    ("base-advance-not-retarget", "same base name is not a retarget -> reaches merge",
     t_base_advance_is_not_a_retarget),
    ("held-frozen", "a held PR never merges, whatever the counters/enums say", t_held_never_merges),
    ("terminal-frozen", "a terminal row (aborted/merged) never merges — the allow-list repro",
     t_terminal_status_never_merges),
    ("unexpected-frozen", "an unexpected status parks — allow-list, not reject-list",
     t_unexpected_status_never_merges),
    ("in-review-merges", "only in_review clears the status allow-list", t_in_review_is_the_one_that_merges),
    ("not-open", "a merged/closed PR is not a candidate", t_not_open),
    ("draft", "a draft PR is parked, not merged", t_draft),
    ("stale-sha", "a moved head outranks ci and the enums", t_stale_sha),
    ("ci-not-green", "ci is checked before the enums; mergeStateStatus never feeds ci", t_ci_not_green),
    ("short-reviews", "reviews_ok < required(tier) blocks; required(tier) is 1 for TRIVIAL", t_short_reviews),
    ("mss-behind", "BEHIND -> rebase", t_mergestate_behind),
    ("mss-dirty", "DIRTY -> rebase", t_mergestate_dirty),
    ("mss-unstable", "UNSTABLE -> non-passing, not campaign's ci", t_mergestate_unstable),
    ("mss-blocked", "BLOCKED -> probe base ancestry (rebase if behind, else park)", t_mergestate_blocked),
    ("mss-unknown", "UNKNOWN merge state -> re-poll", t_mergestate_unknown),
    ("mergeable-conflicting", "CONFLICTING decided on .mergeable alone", t_conflicting_mergeable),
    ("mergeable-unknown", "UNKNOWN mergeability -> re-poll", t_unknown_mergeable),
    ("mss-unknown-value-parks", "an unrecognised merge state parks (totality)", t_unknown_mergestate_value_parks),
    ("mergeable-unknown-value-parks", "an unrecognised mergeable value parks", t_unknown_mergeable_value_parks),
    ("cli-injected-view", "check --view-json decides without gh and exits 0", t_cli_injected_view),
    ("cli-unresolved-base", "a both-`-` unresolved base never merges — the finding #2 false-permissive fix",
     t_cli_unresolved_base_never_merges),
    ("cli-stale-base", "a CLEAN candidate behind refreshed base cannot merge", t_cli_stale_base_blocks_merge),
    ("cli-unverified-base", "an unreadable base ancestry fails closed", t_cli_unverified_base_blocks_merge),
    ("cli-blocked-behind", "a BLOCKED PR behind its base routes to rebase (the #134 fix)",
     t_cli_blocked_behind_rebases),
    ("cli-blocked-current", "a BLOCKED PR up to date still parks awaiting-user", t_cli_blocked_current_parks),
    ("cli-blocked-unverified", "a BLOCKED PR with unreadable ancestry fails closed", t_cli_blocked_unverified_leaves),
    ("cli-no-row", "a PR absent from the ledger decides `no ledger row`", t_cli_no_ledger_row),
    ("cli-view-missing-field", "a view missing a field fails closed, never KeyError", t_cli_view_missing_field),
    ("cli-view-wrong-type", "a view field of the wrong JSON type fails closed", t_cli_view_wrong_type_field),
    ("cli-gh-spawn-failure", "a gh-spawn failure fails closed, no traceback", t_cli_gh_spawn_failure),
]
