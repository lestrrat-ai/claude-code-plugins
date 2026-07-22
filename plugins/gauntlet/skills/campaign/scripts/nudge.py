#!/usr/bin/env python3
"""The nudge printer — sticky-note reminders the campaign orchestrator prints at the top of every heartbeat.

Every heartbeat is a fresh agent instance, and the campaign's obligations live in prose it re-derives by hand.
So it forgets: it forgets to arm the fallback heartbeat, it drives off a stale ledger instead of fanning
out, it lets a review agent die unnoticed, it forgets to swap a PR's labels as the gate moves. This tool
reads the durable state and PRINTS what the orchestrator should CHECK — the same audit the heartbeat skeleton
describes in prose, computed from disk so an amnesiac heartbeat is handed it rather than told to remember it.

**It is a REMINDER, not a supervisor.** Every rule is a CHEAP read (ledger fields, an open-follow-up
count, whether a `<rundir>` file exists) → a short "check X" string. It NEVER derives CI, counts verdicts,
decides merge-readiness, evaluates a liveness cap, or judges whether a review agent is actually alive —
those tools already exist and it only reminds the orchestrator to run them. It decides nothing about
whether a PR may merge; it is branch-owned, dogfoodable, and always exits 0. A reminder you can ignore is
the point: its value is being DELIVERED at the heartbeat, computed and concrete, not forcing anything.

The design and the full obligation inventory it was trimmed from live in `.gauntlet/DESIGN-nudge-printer.md`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Print per-heartbeat reminders for the campaign orchestrator (sticky notes, not a supervisor)."

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "nudge-test.py"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


L = _load("nudge_ledger", "ledger.py")
F = _load("nudge_followups", "followups.py")

# A held PR is FROZEN — it fires ONLY its held reminder, never review/CI/merge nudges. The enumeration
# lives in ledger.py; never retype it (a new held status inherits the freeze with no edit here).
HELD = L.HELD_STATUSES
REPAIRING = L.REPAIR_STATUS
TERMINAL = ("merged", "aborted")
OPEN_FOLLOWUP_HIDDEN = F.TABLE_HIDDEN_STATES  # a follow-up in one of these is closed, not open work

# The PARKED-on-a-human statuses — HELD minus `repairing` (which waits on the reassessment pass, not the
# user). DERIVED from HELD so a new held status cannot silently join or miss this set. The quiet-run rule
# treats these specially: a run idle only because every open PR is parked is idle BECAUSE it waits on YOU.
PARKED = tuple(s for s in HELD if s != REPAIRING)

# How long a run may show NO ledger activity before the quiet-run sweep fires. Derived, not guessed: two
# normal ~15-min heartbeat intervals (so a single slow heartbeat never trips it) plus ~5 min of slack.
QUIET_AFTER = timedelta(minutes=35)


def _activity_age(last_activity: str, now: datetime) -> "timedelta | None":
    """How long since the run last did something — or None if that is UNKNOWABLE, in which case the
    quiet-run rule stays silent. This is advisory and NEVER raises: a `-` (an old ledger that predates the
    sensor), an empty value, an unparseable stamp, or a naive one all read as "cannot tell" and skip the
    rule rather than fabricate an age. A stamp is UTC ISO-8601 (ledger.py writes `now_activity()`); a naive
    value cannot be subtracted from an aware `now`, so it is refused here rather than crashing the printer.
    """
    if not last_activity or last_activity == "-":
        return None
    try:
        ts = datetime.fromisoformat(last_activity)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return now - ts


def required(tier: str) -> int:
    """1 if TRIVIAL, else 2. Untriaged ('-'/'') counts as needs-review (target 2) — never under-remind."""
    return 1 if tier == "TRIVIAL" else 2


def open_followups(followups_path: "Path | None") -> int:
    if followups_path is None or not followups_path.exists():
        return 0
    entries = F.load(followups_path)
    return sum(1 for e in entries if e.get("state") not in OPEN_FOLLOWUP_HIDDEN)


def rundir_has(rundir: "Path | None", name: str) -> bool:
    return rundir is not None and (rundir / name).exists()


def reminders(header: dict, rows: list, n_followups: int, rundir: "Path | None",
              now: "datetime | None" = None) -> list:
    """Compute the reminder lines. Pure: same inputs → same output. Returns a list of strings.

    `now` is the current UTC time, injectable so the quiet-run rule is testable; it defaults to
    `datetime.now(timezone.utc)` when a caller (main) does not pass one.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    out: list = []
    active = [r for r in rows if r["status"] not in TERMINAL]

    # --- always-fire floor -----------------------------------------------------
    out.append('follow loop-control.md, "Primary continuity", before yielding.')
    out.append("re-read the ledger header (base_branch, reviewer, required_set, skill_version).")
    if active:
        # The base a PR merges into and its required-check set are per-ROW state now (`effective_base` /
        # `effective_required_set`); the header `base_branch`/`required_set` are only the LEGACY FALLBACK a
        # row with none inherits. So the heartbeat re-resolves them PER active PR, not once from the header.
        out.append("check each active PR's labels match its gate state.")
        out.append("re-resolve each active PR's effective base and required set (row-owned; the header "
                   "base_branch/required_set are only the legacy fallback for a row that carries none).")

    # --- run-level -------------------------------------------------------------
    # Required checks are per-BASE row state. Group active rows by effective base; a base whose effective
    # required set is still `unknown` (a read failed or never ran) FAILS CLOSED — it cannot go green — and
    # blocks only ITS group, so the reminder names the base and its PRs. `-` is NOT `unknown`: it means
    # "inherit the header", which `effective_required_set` resolves; a new run's rows read the header's
    # `unknown` until the grouped read (ci-status.py required-set) writes each base's canonical set.
    unknown_bases: "dict[str, list]" = {}
    for r in active:
        if L.effective_required_set(header, r) == "unknown":
            unknown_bases.setdefault(L.effective_base(header, r), []).append(r["pr"])
    for base in sorted(unknown_bases):
        prs = ", ".join(unknown_bases[base])
        out.append(f"required set unknown for base {base} (PR(s) {prs}) — run the grouped required-set "
                   f"read (ci-status.py required-set).")
    if active:
        out.append(f"{len(active)} PR(s) open — reconcile and fan out work up to caps.")
    if n_followups:
        out.append(f"{n_followups} open follow-up(s) — start any you can.")

    # --- watchdog-due reminder -------------------------------------------------
    # The durable long-cadence health-pass deadline (ledger.py's `watchdog_due`). When it is due, never armed,
    # or unreadable AND there is open work, the run owes a deep health pass — the long sensor that catches a
    # run heartbeats keep firing on but never look deeply at. The parse is ledger.py's own `watchdog_state`
    # (never a second copy of it), and like every nudge rule it is a CHEAP read that never raises: a malformed
    # or naive deadline reads `invalid`, which fires the same "re-arm it" reminder rather than crashing. Nudge
    # knows NOTHING about scheduler entries — that is the health pass's adapter business, not a reminder's.
    if active:
        wd_state, _ = L.watchdog_state(header.get("watchdog_due", "-"), now)
        if wd_state in ("due", "unset", "invalid"):
            out.append("watchdog due — run the health pass, then `ledger.py watchdog arm`.")

    # --- quiet-run sweep -------------------------------------------------------
    # A run whose ledger has not moved for QUIET_AFTER is not necessarily healthy — a review may have died,
    # a poll may be stuck, or the user may be sitting on a parked question. Every heartbeat is a fresh
    # context, so "how long has nothing moved?" is read from the durable `last_activity` stamp, not memory.
    # The rule reminds the orchestrator to SWEEP before rescheduling into another silent interval; it decides
    # nothing. It stays silent unless there is BOTH a readable stamp that old AND at least one open PR — an
    # idle run with nothing open has nothing to sweep, and a `-`/absent stamp is an old ledger, not a stall.
    age = _activity_age(header.get("last_activity", "-"), now)
    if age is not None and age >= QUIET_AFTER and active:
        quiet_min = int(age.total_seconds() // 60)
        parked = [r for r in active if r["status"] in PARKED]
        out.append(f"run has been QUIET for ~{quiet_min}m (no ledger activity) — SWEEP before rescheduling.")
        if len(parked) == len(active):
            # The run is not stalled — it is WAITING ON THE USER. The unanswered question is the thing to
            # surface, not a stall to chase; say so explicitly so the sweep is not misread as a fault.
            out.append("every open PR is parked — the run is idle BECAUSE it is waiting on YOU; the "
                       "unanswered question below is the thing to surface, not a stall to fix.")
        out.append("run `review-pass.py status --run <rundir> --verify` — apply the launch-deadline and "
                   "meaningful-progress rules to any in-flight pass.")
        out.append("re-derive CI for every row that can still move.")
        for r in parked:
            why = r.get("ci_reason", "-")
            tail = f": {why}" if why and why != "-" else ""
            out.append(f"PR {r['pr']}: parked — LEAD your next status to the user with this question and how "
                       f"long it has waited (≥ ~{quiet_min}m quiet){tail}.")

    # --- per-PR reminders ------------------------------------------------------
    # A HELD PR (parked or repairing) fires ONLY its held reminder. That exclusion is enforced by the
    # `status == "in_review"` guard on every in-flight rule below — a held PR is never in_review, so it
    # reaches none of them. No explicit short-circuit is needed (and one would be an untestable no-op).
    for r in active:
        pr = r["pr"]
        status = r["status"]
        if status == REPAIRING:
            if r.get("repair_decision", "-") == "-":
                out.append(f"PR {pr}: repairing, no decision — run `repair-pass.py bundle`.")
            else:
                out.append(f"PR {pr}: repairing — dispatch decision ({r['repair_decision']}), nothing else.")
        elif status in HELD:  # awaiting-user / awaiting-api
            why = r.get("ci_reason", "-")
            tail = f" ({why})" if why and why != "-" else ""
            out.append(f"PR {pr}: parked{tail} — surface the question, don't mutate it.")

        # in-flight rules — each gated on in_review, so held PRs above fire none of them
        need = required(r["tier"])
        ok = int(r["reviews_ok"]) if r["reviews_ok"].isdigit() else 0
        ci = r["ci"]
        if status == "in_review" and ok < need and not rundir_has(rundir, f"intent-{pr}.md"):
            out.append(f"PR {pr}: no intent-{pr}.md — write it before reviewing.")
        if status == "in_review" and ci == "pending":
            out.append(f"PR {pr}: CI pending — re-derive it.")
        if status == "in_review" and ok < need and ci != "red":
            # Work is DUE. The nudge cannot tell from disk whether a review/audit/fix is actually running
            # (that liveness lives in the session, not the ledger) or which KIND is running — so it does not
            # try. It reminds to make sure SOMETHING is live, or to launch one. An earlier version probed
            # review-<pr>-<review_rounds>.progress.jsonl and missed a first review (review_rounds=0) — the
            # exact miss dogfooding and the review both caught (fu25).
            out.append(f"PR {pr}: work due — make sure a dispatched review/audit/fix is live, or launch one.")
        if status == "in_review" and ok >= need and ci == "green":
            out.append(f"PR {pr}: mergeable by counters — check merge-readiness.")

    return out


def render(header: dict, rows: list, n_followups: int, rundir: "Path | None",
           now: "datetime | None" = None) -> str:
    lines = reminders(header, rows, n_followups, rundir, now)
    run_id = header.get("run_id", "-")
    head = f"NUDGE (run {run_id}) — {len(lines)} reminder(s):"
    body = "\n".join(f"  - {line}" for line in lines)
    return head + ("\n" + body if body else "")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", help="the run ledger (<rundir>/state.jsonl)")
    parser.add_argument("--followups", help="the follow-up store (.gauntlet/followups.jsonl)")
    parser.add_argument("--rundir", help="the run directory, for intent/CI/progress file checks")
    parser.add_argument("--self-test", action="store_true", help="run every fixture and assert the rules "
                                                                 "this file enforces still hold")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.file is None:
        parser.error("the following arguments are required: --file")
    header, rows = L.load(Path(args.file))
    n_followups = open_followups(Path(args.followups) if args.followups else None)
    rundir = Path(args.rundir) if args.rundir else None
    print(render(header, rows, n_followups, rundir))
    return 0  # a nudge NEVER blocks — it only reminds


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run "
                              f"and CANNOT report health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("nudge_test", SIBLING, register=True)
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
        print("\n1 check(s) FAILED — the nudge printer's contract is broken.")
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
        print(f"{failures} check(s) FAILED — the nudge printer's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the nudge printer's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
