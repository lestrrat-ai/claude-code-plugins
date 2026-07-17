#!/usr/bin/env python3
"""Fixtures for `nudge.py` — the sticky-note reminder rules.

They live in a SIBLING file, and `nudge.py --self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE with TEETH: it asserts the reminder fires when its condition holds AND is
ABSENT when it does not. A rule that only ever checked "the line is present" would pass against a printer
that emits every line unconditionally — which is no printer at all.
"""

from __future__ import annotations

import tempfile
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


def fire(rows, *, hdr=None, n_followups=0, rundir=None) -> list:
    return N.reminders(hdr or header(run_id="g1"), rows, n_followups, rundir)


def has(lines, substr) -> bool:
    return any(substr in line for line in lines)


def check(cond, msg):
    if not cond:
        raise N.SelfTestFailure(msg)


# --- always-fire floor --------------------------------------------------------

def t_heartbeat_always_fires():
    for rows in ([], [row(1, "in_review")], [row(1, "merged")]):
        check(has(fire(rows), "heartbeat/wake is armed"),
              "the heartbeat reminder must fire EVERY wake, whatever the ledger holds")


def t_header_reread_always_fires():
    check(has(fire([]), "re-read the ledger header FRESH"),
          "the header-reread reminder must fire every wake")


# --- PR labels ----------------------------------------------------------------

def t_labels_fire_only_with_an_active_pr():
    check(has(fire([row(1, "in_review")]), "labels still mirror its gate state"),
          "the labels reminder must fire when a PR is active")
    check(not has(fire([]), "labels still mirror"),
          "the labels reminder must NOT fire with no PRs — nothing to relabel")
    check(not has(fire([row(1, "merged"), row(2, "aborted")]), "labels still mirror"),
          "the labels reminder must NOT fire when every PR is terminal")


# --- run-level ----------------------------------------------------------------

def t_required_set_unknown():
    check(has(fire([], hdr=header(required_set="unknown")), "required_set is UNKNOWN"),
          "an unknown required_set must nudge to derive it")
    check(not has(fire([], hdr=header(required_set="none")), "required_set is UNKNOWN"),
          "a known required_set must NOT fire the derive nudge")


def t_fanout_fires_only_with_open_work():
    check(has(fire([row(1, "in_review")]), "fan out more work"),
          "an open PR must nudge to fan out")
    check(not has(fire([row(1, "merged")]), "fan out more work"),
          "no open PR must NOT nudge to fan out")


def t_followups_fire_only_when_open():
    check(has(fire([], n_followups=3), "3 open follow-up"),
          "open follow-ups must nudge, with the count")
    check(not has(fire([], n_followups=0), "open follow-up"),
          "zero open follow-ups must NOT nudge")


# --- held PRs short-circuit ---------------------------------------------------

def t_parked_pr_fires_only_its_own_reminder():
    lines = fire([row(7, "awaiting-user", ci_reason="settled but not green")])
    check(has(lines, "PR 7: PARKED"), "a parked PR must nudge that it is parked")
    check(has(lines, "settled but not green"), "the park reminder must carry ci_reason")
    check(not has(lines, "PR 7: CI is pending") and not has(lines, "PR 7: reads mergeable")
          and not has(lines, "PR 7: a review looks dispatched"),
          "a HELD PR must fire ONLY its held reminder — never review/CI/merge nudges")


def t_repairing_splits_on_decision():
    no_dec = fire([row(7, "repairing", repair_decision="-")])
    check(has(no_dec, "repairing with NO decision"), "repairing + no decision → reassess nudge")
    check(not has(no_dec, "dispatch that repair"), "no-decision repairing must not say dispatch")
    with_dec = fire([row(7, "repairing", repair_decision="demote@2026-01-01T00:00:00Z")])
    check(has(with_dec, "dispatch that repair"), "repairing + decision → dispatch nudge")
    check(not has(with_dec, "NO decision"), "decided repairing must not say NO decision")


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
    check(has(fire([row(9, "in_review", ci="pending")]), "PR 9: CI is pending"),
          "a pending-CI PR must nudge to re-derive")
    check(not has(fire([row(9, "in_review", ci="green")]), "PR 9: CI is pending"),
          "a green PR must NOT fire the CI-pending nudge")


def t_review_alive_fires_only_with_a_progress_file():
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        (rd / "intent-9.md").write_text("x", encoding="utf-8")  # keep the intent nudge quiet
        r = [row(9, "in_review", reviews_ok=0, tier="HIGH", ci="green", review_rounds=2)]
        check(not has(fire(r, rundir=rd), "review agent is still alive"),
              "no progress file → no dispatched review → no liveness nudge")
        (rd / "review-9-2.progress.jsonl").write_text("{}", encoding="utf-8")
        check(has(fire(r, rundir=rd), "review agent is still alive"),
              "a progress file for the current round → nudge to check the reviewer is alive")


def t_mergeable_fires_when_counters_are_met():
    check(has(fire([row(9, "in_review", reviews_ok=2, tier="HIGH", ci="green")]), "reads mergeable"),
          "reviews_ok >= required and green → nudge to check merge-readiness")
    check(not has(fire([row(9, "in_review", reviews_ok=1, tier="HIGH", ci="green")]), "reads mergeable"),
          "short of required verdicts → NOT mergeable, no merge nudge")
    # TRIVIAL needs only 1
    check(has(fire([row(9, "in_review", reviews_ok=1, tier="TRIVIAL", ci="green")]), "reads mergeable"),
          "a TRIVIAL PR needs only 1 verdict to read mergeable")


def t_terminal_pr_fires_no_per_pr_line():
    lines = fire([row(9, "merged"), row(10, "aborted")])
    check(not has(lines, "PR 9:") and not has(lines, "PR 10:"),
          "a terminal PR must produce NO per-PR reminder")


def t_a_nudge_never_blocks():
    # main() over a real ledger file exits 0 no matter what it prints.
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        led.write_text('{"type": "header", "run_id": "g1", "required_set": "unknown"}\n', encoding="utf-8")
        code = N.main(["--file", str(led)])
        check(code == 0, "the nudge printer must ALWAYS exit 0 — it reminds, it never blocks")


CASES = [
    ("heartbeat-always", "the heartbeat reminder fires every wake", t_heartbeat_always_fires),
    ("header-always", "the header-reread reminder fires every wake", t_header_reread_always_fires),
    ("labels-active-only", "the labels reminder fires only with an active PR", t_labels_fire_only_with_an_active_pr),
    ("required-set-unknown", "unknown required_set nudges to derive it", t_required_set_unknown),
    ("fanout-open-only", "fan-out nudges only with open work", t_fanout_fires_only_with_open_work),
    ("followups-open-only", "follow-ups nudge only when open, with the count", t_followups_fire_only_when_open),
    ("parked-short-circuits", "a parked PR fires only its held reminder", t_parked_pr_fires_only_its_own_reminder),
    ("repairing-splits", "repairing splits on whether a decision is recorded", t_repairing_splits_on_decision),
    ("intent-missing", "intent nudge fires only without the file", t_intent_missing_fires_only_without_the_file),
    ("ci-pending", "pending CI nudges to re-derive", t_ci_pending_fires),
    ("review-alive", "the liveness nudge needs a dispatched progress file", t_review_alive_fires_only_with_a_progress_file),
    ("mergeable", "mergeable nudge respects required(tier)", t_mergeable_fires_when_counters_are_met),
    ("terminal-quiet", "a terminal PR fires no per-PR line", t_terminal_pr_fires_no_per_pr_line),
    ("never-blocks", "a nudge always exits 0", t_a_nudge_never_blocks),
]
