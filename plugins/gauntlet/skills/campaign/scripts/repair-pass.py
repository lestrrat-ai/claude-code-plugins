#!/usr/bin/env python3
"""The REASSESSMENT PASS's door — the only sanctioned way to record what to do about a PR that has stopped
converging.

The campaign review gate had no memory. Every wake was a fresh agent instance, `reviews_ok` was zeroed on
every NOT SATISFIED, and nothing counted rounds — so the ledger after 21 review rounds was indistinguishable
from the ledger after 1, and every stopping rule in the skill was a rule with NO SENSOR. Two PRs ran 21 and
14 adversarial rounds, produced a true finding almost every time, and never converged. A human stopped it at
8.5 hours, and could only do so by holding all 21 rounds in mind at once.

`ledger.py` now carries the memory (`review_rounds`, `ns_streak`) and the caps. When one is reached the row
goes `repairing` and ordinary gate work is REFUSED for it. This file owns what happens next: a
context-isolated agent is handed THE WHOLE HISTORY AT ONCE — every round's verdict and finding, the
diff-growth curve, the PR's intent artifact, the current diff — and returns exactly ONE decision from a
CLOSED enum. `references/repair-pass.md` is the definition; this is its enforcement.

**A CAP IS A MODE SWITCH, NOT A DOORBELL.** It does not stop and ask the user. The driver stops dispatching
targeted fixes and REPAIRS THE PR ITSELF — rescopes it back to its stated purpose, re-authors the intent the
reviewer had nothing to measure against, demotes findings that anchor to no purpose and no writer, fixes at
the chokepoint instead of playing whack-a-mole, or gives up and leaves the PR open for a human. Only the
last of those involves the user at all, and it is the last resort, not the first.

Three refusals this tool exists to make, all of them things a well-meaning driver would otherwise do:

1. **A decision for a PR that is not at a cap.** The reassessment is not a tool for skipping a review you
   dislike. Only a `repairing` row may take one.
2. **A repair that REWRITES A PR CAMPAIGN DOES NOT OWN.** Campaign ADOPTS PRs — they may be the user's or a
   third party's. RESCOPE and ROOT-CAUSE reshape branch content wholesale, and doing that to someone else's
   work uninvited is not a repair, it is a hijack. On an `external` PR the permitted decisions are ONLY
   DEMOTE / REPAIR-INTENT / ABORT, and this tool refuses the other two outright. (Targeted per-finding
   fixes are NOT affected — campaign has always pushed those to adopted PRs, and that is the workflow the
   user asked for. What is forbidden is the wholesale reshaping, not the ordinary fix.)
3. **A THIRD repair.** The mechanism that fixes non-convergence must not itself fail to converge — the
   irony would be fatal. At `REPAIR_CAP` the only decision left is ABORT.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

DESCRIPTION = "Record the reassessment pass's decision for a PR that has stopped converging."

OWNER = Path(__file__).resolve().parent / "ledger.py"


def load_ledger():
    """Load `ledger.py` BY PATH — it owns the schema, the caps and the statuses, and it owns them ONCE.

    Not by import: the cwd is the driver's worktree while the skill's scripts live wherever the plugin is
    installed. Re-declaring the field names or the caps here would be a second copy of the schema, which is
    the exact defect `ledger.py` exists to prevent.
    """
    spec = importlib.util.spec_from_file_location("ledger", OWNER)
    if spec is None or spec.loader is None:  # a broken install — never an input error
        print(f"repair-pass: cannot load its schema owner at {OWNER}", file=sys.stderr)
        raise SystemExit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load_ledger()

# --- the closed enum ----------------------------------------------------------
#
# FIVE decisions, and the reassessment agent returns EXACTLY ONE. A closed enum is the point: "think about
# it and do something sensible" is what a fresh-context driver holding one finding already does, twenty-one
# times in a row. Each decision names a DIFFERENT diagnosis of why the loop stopped converging, and the
# driver executes it without asking the user.
DECISIONS = {
    "rescope": (
        "THE DIFF HAS OUTGROWN ITS STATED PURPOSE. The findings may all be true, and the PR is still no "
        "longer the change it set out to be — most of its lines now defend the guards the loop itself "
        "added. Dispatch a shrink back to intent, then re-gate. (This is what a human did to PR #42, at "
        "round 21 rather than round 13: followups.py went 4,319 lines -> 939 and lost nothing real.)"
    ),
    "repair-intent": (
        "THE INTENT ARTIFACT IS MISSING, VAGUE OR WRONG, so the reviewer has nothing to measure against "
        "and NOTHING CAN BE OUT OF SCOPE. Re-author it — Purpose, Non-goals, Threat model — and re-gate. "
        "This was the actual root cause of the 2026-07-14 spiral: an open-ended adversarial mandate over a "
        "growing surface has no fixed point, because there is always one more true statement to make."
    ),
    "demote": (
        "THE FINDINGS ANCHOR TO NO PURPOSE LINE AND NO THREAT-MODEL ACTOR. They are true and they are not "
        "reasons to block this PR: a defect in a guard this same loop added, against an input nobody but a "
        "developer with a text editor can write. RECORD THEM AS FOLLOW-UPS, DO NOT FIX THEM, and re-gate. "
        "Fixing them adds review surface at the rate it removes it."
    ),
    "root-cause": (
        "THE FINDINGS SHARE ONE CAUSE. Stop patching sites and fix at the chokepoint: run the root-cause "
        "pass (`root-cause-pass.md` — it already exists; do NOT reinvent it), which maps the whole space "
        "with a read-only mapper and fixes every cell at once, including the ones no reviewer has hit yet."
    ),
    "abort": (
        "UNSALVAGEABLE. Leave the PR OPEN, drop this run's labels, write the abort note "
        "(`bailout-and-final-report.md` owns the procedure — reuse it, do not invent a second one). "
        "Campaign never closes an adopted PR: it is the user's, and it is left for them."
    ),
}

# What an `external` PR may take. The two that are missing are the two that REWRITE BRANCH CONTENT.
EXTERNAL_PERMITTED = ("demote", "repair-intent", "abort")

# The repairs that reshape someone's branch wholesale — the ones the ownership guardrail exists for.
REWRITES_CONTENT = tuple(d for d in DECISIONS if d not in EXTERNAL_PERMITTED)


def fail(msg: str) -> NoReturn:
    print(f"repair-pass: {msg}", file=sys.stderr)
    raise SystemExit(1)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def permitted_for(row: dict) -> "tuple[str, ...]":
    """The decisions this row may actually take — DERIVED, never retyped.

    Two independent narrowings, and the budget one wins:
      * an `external` PR (the default!) may not have its content rewritten -> DEMOTE / REPAIR-INTENT / ABORT
      * a PR whose repair budget is SPENT may only ABORT — whatever its origin
    The reassessment agent is TOLD this set by `permitted`, so its prompt can never drift from the rule
    the tool enforces. A closed enum restated in prose is a closed enum that goes stale.
    """
    if L.counter(row, "repair_count") >= L.REPAIR_CAP:
        return ("abort",)
    if row["pr_origin"] == "gauntlet":
        return tuple(DECISIONS)
    return EXTERNAL_PERMITTED


def get_row(path: Path, pr: str) -> dict:
    _, rows = L.load(path)
    row = L.find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}")
    return row


def cmd_permitted(path: Path, args) -> int:
    """Print the decisions this PR may take, and why — the reassessment prompt is BUILT from this."""
    row = get_row(path, str(args.pr))
    allowed = permitted_for(row)
    spent = L.counter(row, "repair_count") >= L.REPAIR_CAP
    print(json.dumps({
        "pr": row["pr"],
        "status": row["status"],
        "pr_origin": row["pr_origin"],
        "review_rounds": row["review_rounds"],
        "ns_streak": row["ns_streak"],
        "repair_count": row["repair_count"],
        "repair_cap": str(L.REPAIR_CAP),
        "permitted": list(allowed),
        "why": (
            f"the repair budget is SPENT ({row['repair_count']} of {L.REPAIR_CAP}) — a second failed repair "
            f"aborts rather than looping, so ABORT is all that is left"
            if spent else
            "campaign opened this PR, so every repair is permitted" if row["pr_origin"] == "gauntlet" else
            f"pr_origin={row['pr_origin']} — campaign did NOT open this PR, so it may never rewrite its "
            f"content: {', '.join(REWRITES_CONTENT)} are refused. Record findings, re-author the intent, or "
            f"leave it for its owner"
        ),
    }))
    return 0


def cmd_decide(path: Path, args) -> int:
    """Record the reassessment's decision. The ONLY sanctioned way — and it REFUSES more than it accepts."""
    pr = str(args.pr)
    header, rows = L.load(path)
    row = L.find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}")

    if row["status"] != L.REPAIR_STATUS:
        fail(f"pr {pr} is {row['status']}, not {L.REPAIR_STATUS} — it has NOT reached a review-loop cap, so "
             f"there is nothing to reassess. The reassessment is not a way around a review you disagree "
             f"with; it is what happens when the loop stops converging.")

    # THE DECISION RECORD MUST EXIST BEFORE IT IS RECORDED. A decision whose reasoning is only in a dead
    # agent's context is a decision nobody can audit — and every wake is a fresh agent instance.
    record = Path(args.record)
    if not record.exists() or not record.read_text().strip():
        fail(f"--record {record} does not exist or is empty. Write the reassessment's reasoning — the "
             f"round-by-round history it saw, the decision, and WHY — before recording the decision. A "
             f"decision with no record on disk cannot be audited by the next wake, which remembers nothing.")

    allowed = permitted_for(row)
    if args.decision not in allowed:
        spent = L.counter(row, "repair_count") >= L.REPAIR_CAP
        if spent:
            fail(f"pr {pr} has spent its repair budget ({row['repair_count']} of {L.REPAIR_CAP}) — the only "
                 f"permitted decision is `abort`, not `{args.decision}`. A mechanism that fixes "
                 f"non-convergence must not itself fail to converge.")
        fail(f"`{args.decision}` REWRITES BRANCH CONTENT, and pr {pr} has pr_origin={row['pr_origin']} — "
             f"campaign did not open this PR. It may be the user's or a third party's, and reshaping "
             f"someone else's work uninvited is not a repair. Permitted here: {', '.join(allowed)}. "
             f"(Targeted per-finding fixes are unaffected — this refusal is about the WHOLESALE rewrite.)")

    row["repair_count"] = str(L.counter(row, "repair_count") + 1)
    row["repair_decision"] = f"{args.decision}@{now()}"
    if args.decision == "abort":
        # Terminal. The driver still runs the abort PROCEDURE (leave the PR OPEN, drop this run's labels,
        # write abort-<id>.md) — `bailout-and-final-report.md` owns it, and this does not replace it.
        row["status"] = "aborted"
    L.dump(path, header, rows)
    print(json.dumps({f: row[f] for f in L.ROW_FIELDS}))

    if args.decision == "abort":
        print(f"repair-pass: pr {pr} -> ABORTED. Now run the abort procedure "
              f"(`bailout-and-final-report.md`): LEAVE THE PR OPEN, remove this run's labels, write "
              f"abort-<id>.md, and keep driving the other PRs.", file=sys.stderr)
        return 0
    print(f"repair-pass: pr {pr} -> {args.decision.upper()} (repair {row['repair_count']} of "
          f"{L.REPAIR_CAP}). The row stays `{L.REPAIR_STATUS}`: dispatch THIS repair and no other work "
          f"(`ledger.py dispatch-check --pr {pr} --action repair`). When the repair has landed, return the "
          f"row to `in_review` and let the gate run again. If this PR reaches a cap AGAIN, its budget is "
          f"{'SPENT — the next decision must be abort' if L.counter(row, 'repair_count') >= L.REPAIR_CAP else 'nearly spent'}.",
          file=sys.stderr)
    return 0


# --- self-test: the fixtures live in the SIBLING, and a missing sibling is a HARD FAILURE ---------------

SIBLING = Path(__file__).resolve().parent / "repair-pass-test.py"


class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def run(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:  # fail() -> 1; argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def sibling_cases() -> list:
    """Load the sibling's fixtures — and FAIL LOUDLY if they are not there.

    A self-test that passes because it found nothing to check is worse than no self-test: it reports health
    while checking nothing. A reviewer proved exactly that on this repo's own follow-up ledger, where
    `self_test()` went green with an empty case list. Missing, unloadable, or exporting no cases: all hard
    errors, never an empty list quietly appended to nothing.
    """
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run and CANNOT report "
            f"health. Every rule this file enforces is now unpinned."
        )
    spec = importlib.util.spec_from_file_location("repair_pass_test", SIBLING)
    if spec is None or spec.loader is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["repair_pass_test"] = mod
    spec.loader.exec_module(mod)
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
        print(f"FAIL     {'sibling-fixtures':22} -> the fixtures in {SIBLING.name} must be RUNNABLE\n         {exc}")
        print("\n1 check(s) FAILED — the repair pass's contract is broken.")
        return 1
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, rule, fn in cases:
            work = Path(tmpdir) / name
            work.mkdir()
            try:
                fn(work)
            except SelfTestFailure as exc:
                print(f"FAIL     {name:22} -> {rule}\n         {exc}")
                failures += 1
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                print(f"FAIL     {name:22} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"ok       {name:22} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the repair pass's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the repair pass's contract is intact.")
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", help="path to the ledger (state.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("permitted", help="print the decisions this PR may take, and why (build the "
                                         "reassessment prompt from this — never from a retyped list)")
    p.add_argument("--pr", required=True, help="PR number (row key)")

    d = sub.add_parser("decide", help="record the reassessment pass's ONE decision")
    d.add_argument("--pr", required=True, help="PR number (row key)")
    d.add_argument("--decision", required=True, choices=tuple(DECISIONS),
                   help="; ".join(f"{k}: {v.split('.')[0]}" for k, v in DECISIONS.items()))
    d.add_argument("--record", required=True,
                   help="path to the decision record — the history the pass saw, the decision, and why. "
                        "Refused if it does not exist or is empty")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def dispatch(args) -> int:
    if args.cmd == "self-test":
        return self_test()
    if args.file is None:
        build_parser().error("the following arguments are required: --file")
    path = Path(args.file)
    return {"permitted": cmd_permitted, "decide": cmd_decide}[args.cmd](path, args)


def main(argv: "list[str] | None" = None) -> int:
    return dispatch(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
