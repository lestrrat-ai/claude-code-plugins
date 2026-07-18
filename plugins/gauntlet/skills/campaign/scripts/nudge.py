#!/usr/bin/env python3
"""The nudge printer — sticky-note reminders the campaign orchestrator prints at the top of every wake.

Every wake is a fresh agent instance, and the campaign's obligations live in prose it re-derives by hand.
So it forgets: it forgets to arm the fallback heartbeat, it drives off a stale ledger instead of fanning
out, it lets a review agent die unnoticed, it forgets to swap a PR's labels as the gate moves. This tool
reads the durable state and PRINTS what the orchestrator should CHECK — the same audit the wake skeleton
describes in prose, computed from disk so an amnesiac wake is handed it rather than told to remember it.

**It is a REMINDER, not a supervisor.** Every rule is a CHEAP read (ledger fields, an open-follow-up
count, whether a `<rundir>` file exists) → a short "check X" string. It NEVER derives CI, counts verdicts,
decides merge-readiness, evaluates a liveness cap, or judges whether a review agent is actually alive —
those tools already exist and it only reminds the orchestrator to run them. It decides nothing about
whether a PR may merge; it is branch-owned, dogfoodable, and always exits 0. A reminder you can ignore is
the point: its value is being DELIVERED at the wake, computed and concrete, not forcing anything.

The design and the full obligation inventory it was trimmed from live in `.gauntlet/DESIGN-nudge-printer.md`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Print per-wake reminders for the campaign orchestrator (sticky notes, not a supervisor)."

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


def reminders(header: dict, rows: list, n_followups: int, rundir: "Path | None") -> list:
    """Compute the reminder lines. Pure: same inputs → same output. Returns a list of strings."""
    out: list = []
    active = [r for r in rows if r["status"] not in TERMINAL]

    # --- always-fire floor -----------------------------------------------------
    out.append("check the heartbeat/wake is armed.")
    out.append("re-read the ledger header (base_branch, reviewer, required_set, skill_version).")
    if active:
        out.append("check each active PR's labels match its gate state.")

    # --- run-level -------------------------------------------------------------
    if header.get("required_set") == "unknown":
        out.append("required_set is unknown — derive it.")
    if active:
        out.append(f"{len(active)} PR(s) open — reconcile and fan out work up to caps.")
    if n_followups:
        out.append(f"{n_followups} open follow-up(s) — start any you can.")

    # --- per-PR reminders ------------------------------------------------------
    # A HELD PR (parked or repairing) fires ONLY its held reminder. That exclusion is enforced by the
    # `status == "in_review"` guard on every in-flight rule below — a held PR is never in_review, so it
    # reaches none of them. No explicit short-circuit is needed (and one would be an untestable no-op).
    for r in active:
        pr = r["pr"]
        status = r["status"]
        if status == REPAIRING:
            if r.get("repair_decision", "-") == "-":
                out.append(f"PR {pr}: repairing, no decision — run the reassessment pass.")
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


def render(header: dict, rows: list, n_followups: int, rundir: "Path | None") -> str:
    lines = reminders(header, rows, n_followups, rundir)
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
