#!/usr/bin/env python3
"""Fixtures for `nudge.py` — the sticky-note reminder rules.

They live in a SIBLING file, and `nudge.py --self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE with TEETH: it asserts the reminder fires when its condition holds AND is
ABSENT when it does not. A rule that only ever checked "the line is present" would pass against a printer
that emits every line unconditionally — which is no printer at all.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "nudge.py"


def _load_owner():
    mod = load_module_from_path("nudge_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the nudge printer at {OWNER}")
    return mod


N = _load_owner()
L = N.L


def header(**kw) -> dict:
    return dict(L.HEADER_DEFAULTS, **{k: str(v) for k, v in kw.items()})


def row(pr, status, **kw) -> dict:
    r = dict(L.ROW_DEFAULTS, pr=str(pr), status=status)
    r.update({k: str(v) for k, v in kw.items()})
    r["id"] = f"pr{pr}"
    return r


def fire(rows, *, hdr=None, n_followups=0, rundir=None, now=None) -> list:
    return N.reminders(hdr or header(run_id="g1"), rows, n_followups, rundir, now)


# A fixed "now" and two stamps around it, so the quiet-run rule is DETERMINISTIC: one older than
# QUIET_AFTER (fires) and one comfortably inside it (silent).
NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def stamp(minutes_ago: int) -> str:
    """A last_activity value `minutes_ago` before NOW, in the ledger's UTC ISO-8601 second-precision form."""
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def has(lines, substr) -> bool:
    return any(substr in line for line in lines)


def check(cond, msg):
    if not cond:
        raise N.SelfTestFailure(msg)


# --- always-fire floor --------------------------------------------------------

def t_heartbeat_always_fires():
    for rows in ([], [row(1, "in_review")], [row(1, "merged")]):
        check(has(fire(rows), "heartbeat is armed"),
              "the heartbeat reminder must fire EVERY heartbeat, whatever the ledger holds")


def t_header_reread_always_fires():
    check(has(fire([]), "re-read the ledger header"),
          "the header-reread reminder must fire every heartbeat")


# --- PR labels ----------------------------------------------------------------

def t_labels_fire_only_with_an_active_pr():
    check(has(fire([row(1, "in_review")]), "labels match its gate state"),
          "the labels reminder must fire when a PR is active")
    check(not has(fire([]), "labels match"),
          "the labels reminder must NOT fire with no PRs — nothing to relabel")
    check(not has(fire([row(1, "merged"), row(2, "aborted")]), "labels match"),
          "the labels reminder must NOT fire when every PR is terminal")


# --- run-level ----------------------------------------------------------------

def t_required_set_unknown():
    check(has(fire([], hdr=header(required_set="unknown")), "required_set is unknown"),
          "an unknown required_set must nudge to derive it")
    check(not has(fire([], hdr=header(required_set="none")), "required_set is unknown"),
          "a known required_set must NOT fire the derive nudge")


def t_fanout_fires_only_with_open_work():
    check(has(fire([row(1, "in_review")]), "fan out work"),
          "an open PR must nudge to fan out")
    check(not has(fire([row(1, "merged")]), "fan out work"),
          "no open PR must NOT nudge to fan out")


def t_followups_fire_only_when_open():
    check(has(fire([], n_followups=3), "3 open follow-up"),
          "open follow-ups must nudge, with the count")
    check(not has(fire([], n_followups=0), "open follow-up"),
          "zero open follow-ups must NOT nudge")


# --- held PRs short-circuit ---------------------------------------------------

def t_parked_pr_fires_only_its_own_reminder():
    lines = fire([row(7, "awaiting-user", ci_reason="settled but not green")])
    check(has(lines, "PR 7: parked"), "a parked PR must nudge that it is parked")
    check(has(lines, "settled but not green"), "the park reminder must carry ci_reason")
    check(not has(lines, "PR 7: CI pending") and not has(lines, "PR 7: mergeable")
          and not has(lines, "PR 7: work due"),
          "a HELD PR must fire ONLY its held reminder — never review/CI/merge nudges")


def t_repairing_splits_on_decision():
    no_dec = fire([row(7, "repairing", repair_decision="-")])
    check(has(no_dec, "repairing, no decision"), "repairing + no decision → reassess nudge")
    check(has(no_dec, "repair-pass.py bundle"), "reassessment nudge must name the executable bundle door")
    check(not has(no_dec, "dispatch decision"), "no-decision repairing must not say dispatch")
    with_dec = fire([row(7, "repairing", repair_decision="demote@2026-01-01T00:00:00Z")])
    check(has(with_dec, "dispatch decision"), "repairing + decision → dispatch nudge")
    check(not has(with_dec, "no decision"), "decided repairing must not say NO decision")


# --- per-PR in-flight ---------------------------------------------------------

def t_intent_missing_fires_only_without_the_file():
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        r = [row(9, "in_review", reviews_ok=0, tier="HIGH")]
        check(has(fire(r, rundir=rd), "no intent-9.md"),
              "a review-due PR with no intent file must nudge to write it")
        (rd / "intent-9.md").write_text("x", encoding="utf-8")
        check(not has(fire(r, rundir=rd), "no intent-9.md"),
              "once the intent file exists the nudge must STOP")


def t_ci_pending_fires():
    check(has(fire([row(9, "in_review", ci="pending")]), "PR 9: CI pending"),
          "a pending-CI PR must nudge to re-derive")
    check(not has(fire([row(9, "in_review", ci="green")]), "PR 9: CI pending"),
          "a green PR must NOT fire the CI-pending nudge")


def t_work_due_fires_whenever_review_is_due():
    """The work-due reminder fires whenever a PR needs review and isn't blocked — NOT keyed to any
    progress file. This pins fu25: a first review (review_rounds=0) must fire, and it must not claim to
    know the work is a 'review' specifically (an audit or fix may be what's live)."""
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        (rd / "intent-9.md").write_text("x", encoding="utf-8")  # keep the intent nudge quiet
        # review_rounds=0, no progress file at all → the OLD rule missed this; the new one must fire.
        r = [row(9, "in_review", reviews_ok=0, tier="HIGH", ci="green", review_rounds=0)]
        check(has(fire(r, rundir=rd), "work due — make sure a dispatched review/audit/fix is live"),
              "a first review (review_rounds=0) must fire the work-due nudge — the fu25 miss")
        # not review-due → silent: enough verdicts (mergeable), or CI red.
        check(not has(fire([row(9, "in_review", reviews_ok=2, tier="HIGH", ci="green")]), "work due"),
              "a PR with its verdicts is NOT work-due — no work-due nudge")
        check(not has(fire([row(9, "in_review", reviews_ok=0, tier="HIGH", ci="red")]), "work due"),
              "a red-CI PR is not review-due — fix CI first, no work-due nudge")


def t_mergeable_fires_when_counters_are_met():
    check(has(fire([row(9, "in_review", reviews_ok=2, tier="HIGH", ci="green")]), "mergeable"),
          "reviews_ok >= required and green → nudge to check merge-readiness")
    check(not has(fire([row(9, "in_review", reviews_ok=1, tier="HIGH", ci="green")]), "mergeable"),
          "short of required verdicts → NOT mergeable, no merge nudge")
    # TRIVIAL needs only 1
    check(has(fire([row(9, "in_review", reviews_ok=1, tier="TRIVIAL", ci="green")]), "mergeable"),
          "a TRIVIAL PR needs only 1 verdict to read mergeable")


def t_terminal_pr_fires_no_per_pr_line():
    lines = fire([row(9, "merged"), row(10, "aborted")])
    check(not has(lines, "PR 9:") and not has(lines, "PR 10:"),
          "a terminal PR must produce NO per-PR reminder")


# --- quiet-run sweep ----------------------------------------------------------
# Boundary stamps are derived FROM N.QUIET_AFTER, not hard-coded minutes, so a re-tuned constant still
# leaves one stamp comfortably over the threshold and one comfortably under it.
_QUIET_MIN = int(N.QUIET_AFTER.total_seconds() // 60)


def t_quiet_run_fires_when_stale():
    """A ledger quiet longer than QUIET_AFTER with an open PR fires the whole sweep — and a FRESH ledger
    fires none of it (the teeth: an always-on sweep would be no sweep)."""
    stale = fire([row(1, "in_review", ci="green")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(stale, "QUIET"), "a run quiet past QUIET_AFTER must fire the sweep")
    check(has(stale, "review-pass.py status --run <rundir> --verify"),
          "the sweep must remind to run the review-pass status check with --verify")
    check(has(stale, "re-derive CI"), "the sweep must remind to re-derive CI for rows that can move")
    check(has(stale, "confirm the next heartbeat is armed before you sleep"),
          "the sweep must remind to arm the next heartbeat before sleeping")
    fresh = fire([row(1, "in_review", ci="green")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN - 5)), now=NOW)
    check(not has(fresh, "QUIET"), "a run with recent activity must NOT fire the quiet sweep")


def t_quiet_run_silent_when_absent():
    """An absent/`-` last_activity (an old ledger that predates the sensor) is NOT a stall — stay silent."""
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1"), now=NOW), "QUIET"),
          "a default `-` last_activity must NOT fire the quiet sweep")
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1", last_activity=""), now=NOW), "QUIET"),
          "an empty last_activity must NOT fire the quiet sweep")
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1", last_activity="not-a-date"), now=NOW), "QUIET"),
          "an unparseable last_activity must NOT fire the quiet sweep — advisory, never a crash")


def t_quiet_run_needs_an_open_pr():
    """A quiet ledger with NOTHING open has nothing to sweep — the rule stays silent."""
    check(not has(fire([row(1, "merged")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW), "QUIET"),
          "a quiet run whose only rows are terminal must NOT fire the sweep")


def t_quiet_run_names_the_park():
    """When PARKED rows are the ONLY open rows, the sweep says the run is waiting on the USER and surfaces
    the unanswered question — the park is the thing to act on, not a stall to chase."""
    lines = fire([row(7, "awaiting-user", ci_reason="needs your approval")],
                 hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(lines, "waiting on YOU"), "a parked-only quiet run must say it is waiting on the user")
    check(has(lines, "PR 7: parked — LEAD") and has(lines, "needs your approval"),
          "the sweep must surface the parked PR's question, led with how long it has waited")
    # a MIXED run (an in-flight row alongside the parked one) is NOT parked-only — it must not claim so.
    mixed = fire([row(7, "awaiting-user", ci_reason="needs your approval"), row(8, "in_review")],
                 hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(mixed, "QUIET") and not has(mixed, "waiting on YOU"),
          "a run with an in-flight PR is not idle-on-user — it must not claim every open PR is parked")


# --- watchdog-due reminder ----------------------------------------------------
# Stamps built around NOW so the rule is DETERMINISTIC: a future deadline (ok → silent) and a past one
# (due → fires), plus the `-`/malformed/naive spellings that read unset/invalid.

def _future(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _past(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def t_watchdog_due_fires_on_unset_overdue_invalid_with_open_work():
    """With an OPEN PR, the reminder fires when watchdog_due is unset (`-`), overdue (a past deadline), or
    invalid (malformed or naive) — and stays SILENT when it is `ok` (a future deadline). The teeth: an
    always-on reminder would be no sensor, so the `ok` case must be silent."""
    open_row = [row(1, "in_review")]
    for label, wd in (("unset (`-`)", "-"),
                      ("overdue", _past(10)),
                      ("malformed", "not-a-date"),
                      ("naive", "2026-07-19T11:00:00")):  # no tzinfo
        lines = fire(open_row, hdr=header(run_id="g1", watchdog_due=wd), now=NOW)
        check(has(lines, "watchdog due — run the health pass"),
              f"a {label} watchdog_due with open work must fire the watchdog-due reminder")
    ok = fire(open_row, hdr=header(run_id="g1", watchdog_due=_future(30)), now=NOW)
    check(not has(ok, "watchdog due"),
          "a future (ok) watchdog deadline must NOT fire the reminder — the run does not owe a health pass yet")


def t_watchdog_due_silent_without_open_work():
    """No non-terminal row → nothing to health-check, so the reminder stays silent even when unset/overdue —
    both an empty ledger and a terminal-only one."""
    for rows in ([], [row(1, "merged"), row(2, "aborted")]):
        for wd in ("-", _past(10), "not-a-date"):
            lines = fire(rows, hdr=header(run_id="g1", watchdog_due=wd), now=NOW)
            check(not has(lines, "watchdog due"),
                  f"a run with no open work must NOT fire the watchdog-due reminder (rows={rows}, wd={wd!r})")


def t_a_nudge_never_blocks():
    # main() over a real ledger file exits 0 no matter what it prints.
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        led.write_text('{"type": "header", "run_id": "g1", "required_set": "unknown"}\n', encoding="utf-8")
        code = N.main(["--file", str(led)])
        check(code == 0, "the nudge printer must ALWAYS exit 0 — it reminds, it never blocks")


CASES = [
    ("heartbeat-always", "the heartbeat reminder fires every heartbeat", t_heartbeat_always_fires),
    ("header-always", "the header-reread reminder fires every heartbeat", t_header_reread_always_fires),
    ("labels-active-only", "the labels reminder fires only with an active PR", t_labels_fire_only_with_an_active_pr),
    ("required-set-unknown", "unknown required_set nudges to derive it", t_required_set_unknown),
    ("fanout-open-only", "fan-out nudges only with open work", t_fanout_fires_only_with_open_work),
    ("followups-open-only", "follow-ups nudge only when open, with the count", t_followups_fire_only_when_open),
    ("parked-short-circuits", "a parked PR fires only its held reminder", t_parked_pr_fires_only_its_own_reminder),
    ("repairing-splits", "repairing splits on whether a decision is recorded", t_repairing_splits_on_decision),
    ("intent-missing", "intent nudge fires only without the file", t_intent_missing_fires_only_without_the_file),
    ("ci-pending", "pending CI nudges to re-derive", t_ci_pending_fires),
    ("work-due", "the work-due nudge fires whenever review is due (fu25)", t_work_due_fires_whenever_review_is_due),
    ("mergeable", "mergeable nudge respects required(tier)", t_mergeable_fires_when_counters_are_met),
    ("terminal-quiet", "a terminal PR fires no per-PR line", t_terminal_pr_fires_no_per_pr_line),
    ("quiet-fires-when-stale", "a run quiet past QUIET_AFTER with an open PR fires the sweep; fresh does not", t_quiet_run_fires_when_stale),
    ("quiet-silent-when-absent", "an absent/`-`/unparseable last_activity never fires the sweep", t_quiet_run_silent_when_absent),
    ("quiet-needs-open-pr", "a quiet run with nothing open has nothing to sweep", t_quiet_run_needs_an_open_pr),
    ("quiet-names-the-park", "a parked-only quiet run says it waits on the user and surfaces the question", t_quiet_run_names_the_park),
    ("watchdog-due-fires", "watchdog-due reminder fires on unset/overdue/invalid with open work, silent when ok", t_watchdog_due_fires_on_unset_overdue_invalid_with_open_work),
    ("watchdog-due-needs-open-work", "watchdog-due reminder is silent with no open/terminal-only rows", t_watchdog_due_silent_without_open_work),
    ("never-blocks", "a nudge always exits 0", t_a_nudge_never_blocks),
]
