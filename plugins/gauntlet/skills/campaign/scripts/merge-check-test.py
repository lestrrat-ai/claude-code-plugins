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

import io
import json
import tempfile
from contextlib import redirect_stdout
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


def row(*, status="in_review", head_sha=SHA_A, ci="green", tier="HIGH", reviews_ok=2) -> dict:
    r = dict(L.ROW_DEFAULTS)
    r.update(pr="9", status=status, head_sha=head_sha, ci=ci, tier=tier, reviews_ok=str(reviews_ok))
    r["id"] = "pr9"
    return r


def view(*, mergeable="MERGEABLE", mergeStateStatus="CLEAN", isDraft=False, state="OPEN",
         headRefOid=SHA_A) -> dict:
    return {"mergeable": mergeable, "mergeStateStatus": mergeStateStatus, "isDraft": isDraft,
            "state": state, "headRefOid": headRefOid}


def decide(r: dict, v: dict) -> dict:
    return M.decide(r, v, required=M.REQUIRED)


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
    expect(row(), view(mergeStateStatus="BLOCKED"), "not-yet", "GitHub says BLOCKED — park awaiting-user")


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
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        L.dump(led, dict(L.HEADER_DEFAULTS, run_id="g1"), [row()])
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(
            M.main, ["check", "--pr", "9", "--file", str(led), "--view-json", str(vjson)])
        check(code == 0, f"the CLI must exit 0 on a computed verdict (stderr: {err})")
        check(json.loads(out) == {"verdict": "merge", "reason": ""},
              f"the CLI should print the merge verdict, got {out!r}")


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


# --- the drift guard has teeth ------------------------------------------------

def t_doc_check_agrees_with_the_shipped_doc():
    check(M.doc_check(M.DOC) == 0, "the shipped stage-3-merge.md must agree with the code that runs")


def _doc_check_quiet(doc: Path) -> int:
    """Run doc_check but swallow its (intentionally alarming) FAIL output — these fixtures WANT it to fail,
    so its report must not pollute the self-test transcript with lines that look like a real break."""
    with redirect_stdout(io.StringIO()):
        return M.doc_check(doc)


START = M.PRECONDITION_TABLE_START
END = M.PRECONDITION_TABLE_END


def _table_rows(mss_values, mergeable_values, *, meaning_tokens=()) -> str:
    """A minimal merge-precondition TABLE wrapped in the start/end markers doc_enum requires. Each enum token
    sits in the FIRST cell of its row — the only cell doc_enum reads. `meaning_tokens` inject extra
    `.mergeStateStatus = X` strings into a row's MEANING (second) cell; those must NOT count. Used by the
    drift fixtures so they exercise the real marked-table, first-cell path."""
    rows = ([f"| `.mergeStateStatus = {v}` | meaning | do |" for v in mss_values]
            + [f"| `.mergeable = {v}` | meaning | do |" for v in mergeable_values]
            + [f"| `.mergeStateStatus = CLEAN` | also names `.mergeStateStatus = {t}` here | do |"
               for t in meaning_tokens])
    return (f"{START}\n| Field / value | Meaning | Do |\n|---|---|---|\n"
            + "\n".join(rows) + f"\n{END}\n")


def t_doc_check_detects_a_dropped_value():
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        states = [v for v in M.MERGE_STATE_STATUS if v != "HAS_HOOKS"]  # drop one FROM THE TABLE
        doc.write_text(_table_rows(states, M.MERGEABLE), encoding="utf-8")
        check(_doc_check_quiet(doc) == 1, "a doc missing a mergeStateStatus value the code handles must FAIL")


def t_doc_check_prose_cannot_mask_a_dropped_table_row():
    # The exact bypass this pins: a value dropped from the TABLE but still named in PROSE outside it must
    # NOT sneak back into the extracted set. The old whole-doc regex would pick the prose token up, the sets
    # would match, and doc-check would PASS while the table silently lost a row. Scoped to table rows, the
    # prose contributes nothing, the set is short a value, and doc-check FAILS as it must.
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        states = [v for v in M.MERGE_STATE_STATUS if v != "HAS_HOOKS"]  # HAS_HOOKS is gone FROM THE TABLE
        table = _table_rows(states, M.MERGEABLE)
        prose = ("\nNote: a `.mergeStateStatus = HAS_HOOKS` PR still merges — but this line is PROSE, not a\n"
                 "table row, so it must NOT count toward the enumerated set.\n")
        doc.write_text(table + prose, encoding="utf-8")
        check(_doc_check_quiet(doc) == 1,
              "a value dropped from the TABLE but named only in PROSE must STILL fail doc-check")


def t_doc_check_another_table_outside_markers_cannot_mask_a_dropped_row():
    # Bypass #1: a value dropped from the MARKED precondition table, but still carried by ANOTHER `|`-table
    # elsewhere in the doc (outside the markers). Scoped to the marked region, the second table contributes
    # NOTHING, the set is short a value, and doc-check FAILS as it must.
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        states = [v for v in M.MERGE_STATE_STATUS if v != "HAS_HOOKS"]  # HAS_HOOKS gone from the MARKED table
        marked = _table_rows(states, M.MERGEABLE)
        other = ("\n### An unrelated table, OUTSIDE the markers\n\n"
                 "| Field / value | Meaning | Do |\n|---|---|---|\n"
                 "| `.mergeStateStatus = HAS_HOOKS` | some other context | do |\n")
        doc.write_text(marked + other, encoding="utf-8")
        check(_doc_check_quiet(doc) == 1,
              "a value dropped from the MARKED table but named in another table OUTSIDE the markers must "
              "STILL fail doc-check")


def t_doc_check_meaning_cell_inside_markers_cannot_mask_a_dropped_row():
    # Bypass #2: a value dropped from its own value-column row, but still mentioned in a later (MEANING) cell
    # of a row INSIDE the markers. Only the FIRST cell (the value column) is read, so the meaning-cell token
    # contributes NOTHING, the set is short a value, and doc-check FAILS as it must.
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        states = [v for v in M.MERGE_STATE_STATUS if v != "HAS_HOOKS"]  # no HAS_HOOKS value-column row
        doc.write_text(_table_rows(states, M.MERGEABLE, meaning_tokens=("HAS_HOOKS",)), encoding="utf-8")
        check(_doc_check_quiet(doc) == 1,
              "a value named only in a MEANING cell inside the markers must STILL fail doc-check")


def t_doc_check_fails_when_markers_are_absent():
    # No markers at all — the drift-guard cannot LOCATE its table, so it must fail loudly, never pass.
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        body = _table_rows(list(M.MERGE_STATE_STATUS), M.MERGEABLE)
        body = body.replace(START + "\n", "").replace(END + "\n", "")  # strip both markers
        doc.write_text(body, encoding="utf-8")
        check(_doc_check_quiet(doc) == 1, "a doc with no precondition-table markers must FAIL — an "
                                          "unlocatable table is never a pass")


def t_doc_check_fails_when_it_finds_nothing():
    # Markers PRESENT (so the table is located) but the marked region names ZERO enum values — the
    # empty-extraction branch, distinct from the markers-absent one above.
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "stage-3-merge.md"
        doc.write_text(f"{START}\n| Field / value | Meaning | Do |\n|---|---|---|\n"
                       f"| this table names no enum values at all | meaning | do |\n{END}\n",
                       encoding="utf-8")
        check(_doc_check_quiet(doc) == 1, "a doc that enumerates zero values must FAIL — a check with no "
                                          "subject never passes")


CASES = [
    ("clean-all-met", "CLEAN + every precondition met -> merge", t_clean_and_all_met),
    ("has-hooks", "HAS_HOOKS -> merge", t_has_hooks_merges),
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
    ("mss-blocked", "BLOCKED -> park awaiting-user", t_mergestate_blocked),
    ("mss-unknown", "UNKNOWN merge state -> re-poll", t_mergestate_unknown),
    ("mergeable-conflicting", "CONFLICTING decided on .mergeable alone", t_conflicting_mergeable),
    ("mergeable-unknown", "UNKNOWN mergeability -> re-poll", t_unknown_mergeable),
    ("mss-unknown-value-parks", "an unrecognised merge state parks (totality)", t_unknown_mergestate_value_parks),
    ("mergeable-unknown-value-parks", "an unrecognised mergeable value parks", t_unknown_mergeable_value_parks),
    ("cli-injected-view", "check --view-json decides without gh and exits 0", t_cli_injected_view),
    ("cli-no-row", "a PR absent from the ledger decides `no ledger row`", t_cli_no_ledger_row),
    ("cli-view-missing-field", "a view missing a field fails closed, never KeyError", t_cli_view_missing_field),
    ("cli-view-wrong-type", "a view field of the wrong JSON type fails closed", t_cli_view_wrong_type_field),
    ("doc-agrees", "the shipped doc agrees with the code", t_doc_check_agrees_with_the_shipped_doc),
    ("doc-drift-caught", "doc-check FAILS when the doc drops a value", t_doc_check_detects_a_dropped_value),
    ("doc-prose-cant-mask", "prose outside the table can't mask a dropped table row",
     t_doc_check_prose_cannot_mask_a_dropped_table_row),
    ("doc-other-table-cant-mask", "another table OUTSIDE the markers can't mask a dropped row",
     t_doc_check_another_table_outside_markers_cannot_mask_a_dropped_row),
    ("doc-meaning-cell-cant-mask", "a MEANING cell inside the markers can't mask a dropped row",
     t_doc_check_meaning_cell_inside_markers_cannot_mask_a_dropped_row),
    ("doc-markers-absent-fails", "doc-check FAILS when the precondition-table markers are absent",
     t_doc_check_fails_when_markers_are_absent),
    ("doc-empty-fails", "doc-check FAILS when it extracts nothing", t_doc_check_fails_when_it_finds_nothing),
]
