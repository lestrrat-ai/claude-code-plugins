#!/usr/bin/env python3
"""Fixtures for `repair-pass.py` — the reassessment decision, the ownership guardrail, and the repair cap.

They live in a SIBLING file, and `repair-pass.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. Three of these guard
things a well-meaning driver would otherwise do without noticing: reassess a PR that never hit a cap,
rewrite a PR belonging to someone else, and repair forever.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "repair-pass.py"


def _load_owner():
    mod = load_module_from_path("repair_pass_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the repair pass at {OWNER}")
    return mod


R = _load_owner()
L = R.L  # the ledger — the ONE owner of the schema, the caps and the statuses

SHA = "c" * 40


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise R.SelfTestFailure(msg)


def ledger_cli(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the LEDGER's real CLI in-process — the guard and the row reads live there, not here."""
    return capture_cli(L.main, argv)


def setup(tmp: Path, name: str = "state.jsonl", **row) -> "tuple[Path, Path]":
    """A ledger holding one PR at a cap, plus a written decision record.

    Written RAW (never through `dump()`), because `repair_count` and `review_rounds` have no CLI door —
    that is the point of them — so a fixture that needs a PR mid-budget must place it there directly.
    """
    path = tmp / name
    fields = {**L.ROW_DEFAULTS, "pr": "1", "head_sha": SHA, "status": L.REPAIR_STATUS,
              "review_rounds": str(L.ROUND_CAP), "ns_streak": "1", **row}
    path.write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS}) + "\n"
        + json.dumps({"type": "row", **fields}) + "\n"
    )
    record = tmp / f"repair-{name}.md"
    record.write_text("# reassessment\n\n21 rounds, the diff tripled, the findings left the purpose.\n")
    return path, record


def decide(path: Path, record: Path, decision: str, pr: str = "1") -> "tuple[int, str, str]":
    return R.run(["--file", str(path), "decide", "--pr", pr, "--decision", decision, "--record", str(record)])


def field(path: Path, name: str, pr: str = "1") -> str:
    code, out, err = ledger_cli(["--file", str(path), "get", "--pr", pr, "--field", name])
    check(code == 0, f"get --field {name} exited {code}: {err!r}")
    return out.strip()


# --- the guardrail: campaign NEVER rewrites a PR it does not own ---------------

def t_external_pr_is_never_rewritten(tmp: Path) -> None:
    """An `external` PR REFUSES every decision that rewrites branch content — RESCOPE and ROOT-CAUSE.

    Campaign ADOPTS PRs. They may be the user's or a third party's, and reshaping someone else's work
    uninvited is not a repair, it is a hijack. This is the fixture that stands between an autonomous
    mechanism and a stranger's branch, so it checks BOTH directions: the rewrites are refused, and the
    three that are safe still work — a guardrail that refused everything would just be a broken feature.
    """
    check(set(R.REWRITES_CONTENT) == {"rescope", "root-cause"},
            f"the rewriting decisions are {R.REWRITES_CONTENT} — if a new decision rewrites branch content "
            f"it MUST be refused on an external PR, and this fixture must know about it")

    for decision in R.REWRITES_CONTENT:
        path, record = setup(tmp, f"ext-{decision}.jsonl", pr_origin="external")
        code, _, err = decide(path, record, decision)
        check(code == 1, f"[{decision}] an EXTERNAL PR was rewritten by an autonomous repair (exit {code})")
        check("did not open this PR" in err, f"[{decision}] refused for the wrong reason: {err!r}")
        check(field(path, "repair_count") == "0", f"[{decision}] a REFUSED decision spent the budget")
        check(field(path, "repair_decision") == "-", f"[{decision}] a REFUSED decision was recorded")

    for decision in R.EXTERNAL_PERMITTED:
        path, record = setup(tmp, f"ok-{decision}.jsonl", pr_origin="external")
        code, _, err = decide(path, record, decision)
        check(code == 0, f"[{decision}] a PERMITTED repair on an external PR was refused: {err!r}")
        check(field(path, "repair_decision").startswith(decision), "the decision was not recorded")


def t_gauntlet_pr_takes_every_repair(tmp: Path) -> None:
    """A PR campaign itself opened may take ALL FIVE decisions — the guardrail is about OWNERSHIP, not fear."""
    for decision in R.DECISIONS:
        path, record = setup(tmp, f"own-{decision}.jsonl", pr_origin="gauntlet")
        code, _, err = decide(path, record, decision)
        check(code == 0, f"[{decision}] refused on a campaign-authored PR: {err!r}")
        check(field(path, "repair_decision").startswith(decision), f"[{decision}] not recorded")


def t_unknown_origin_is_treated_as_external(tmp: Path) -> None:
    """A row whose origin was never established is EXTERNAL — the fail-safe direction, and the default.

    Guessing wrong in the other direction lets an autonomous mechanism rewrite a stranger's branch because
    a field was never set. "I do not know who wrote this" must never resolve to "I did".
    """
    path = tmp / "unset.jsonl"
    path.write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS}) + "\n"
        # A row written BEFORE `pr_origin` existed — no such key at all. It must read back `external`.
        + json.dumps({"type": "row", "pr": "1", "head_sha": SHA, "status": L.REPAIR_STATUS}) + "\n"
    )
    record = tmp / "r.md"
    record.write_text("reassessment\n")
    check(field(path, "pr_origin") == "external", "an unset pr_origin must read back as external")
    code, _, err = decide(path, record, "rescope")
    check(code == 1, f"a PR of UNKNOWN origin was rewritten (exit {code})")
    check("did not open this PR" in err, f"refused for the wrong reason: {err!r}")


# --- the repair's own bound ---------------------------------------------------

def t_repair_budget_is_spent(tmp: Path) -> None:
    """At REPAIR_CAP the ONLY decision left is ABORT — even for a PR campaign owns.

    The mechanism that fixes non-convergence must not itself fail to converge; the irony would be fatal.
    A second failed repair leaves the PR OPEN for a human rather than looping.
    """
    for decision in (d for d in R.DECISIONS if d != "abort"):
        path, record = setup(tmp, f"spent-{decision}.jsonl", pr_origin="gauntlet",
                             repair_count=str(L.REPAIR_CAP))
        code, _, err = decide(path, record, decision)
        check(code == 1, f"[{decision}] a THIRD repair was permitted (exit {code}) — the repair loops")
        check("spent its repair budget" in err, f"[{decision}] refused for the wrong reason: {err!r}")

    path, record = setup(tmp, "spent-abort.jsonl", pr_origin="gauntlet", repair_count=str(L.REPAIR_CAP))
    code, _, err = decide(path, record, "abort")
    check(code == 0, f"ABORT must always remain available: {err!r}")
    check(field(path, "status") == "aborted", f"abort left the row {field(path, 'status')!r}")

    # …and `permitted` says so BEFORE the agent is even asked — the prompt is built from this.
    code, out, _ = R.run(["--file", str(path), "permitted", "--pr", "1"])
    check(code == 0, "permitted exited non-zero")
    check(json.loads(out)["permitted"] == ["abort"], f"permitted did not narrow to abort: {out!r}")


def t_abort_is_terminal_and_leaves_the_pr_open(tmp: Path) -> None:
    """ABORT goes terminal and TELLS THE DRIVER TO LEAVE THE PR OPEN.

    Campaign never closes an adopted PR — it is the user's. The abort PROCEDURE is owned by
    `bailout-and-final-report.md`; this decision routes into it and does not invent a second one.
    """
    path, record = setup(tmp, "abort.jsonl", pr_origin="gauntlet")
    code, _, err = decide(path, record, "abort")
    check(code == 0, f"abort exited {code}: {err!r}")
    check(field(path, "status") == "aborted", "abort is terminal")
    check("LEAVE THE PR OPEN" in err, f"the abort message must say the PR stays open: {err!r}")
    check("bailout-and-final-report" in err, f"the abort must route into the EXISTING procedure: {err!r}")


# --- the decision is real, recorded, and only for a PR that needs one ----------

def t_only_a_capped_pr_may_be_reassessed(tmp: Path) -> None:
    """A PR that never hit a cap CANNOT be reassessed. The repair is not a way around a review you dislike."""
    for status in ("in_review", "pending", "awaiting-user"):
        path, record = setup(tmp, f"live-{status}.jsonl", status=status, pr_origin="gauntlet")
        code, _, err = decide(path, record, "demote")
        check(code == 1, f"[{status}] a PR that is not repairing took a decision (exit {code})")
        check("has NOT reached a review-loop cap" in err, f"[{status}] wrong reason: {err!r}")


def t_a_decision_needs_a_record(tmp: Path) -> None:
    """No decision without its REASONING ON DISK. Every heartbeat is a fresh agent that remembers nothing.

    The whole failure was a loop that could not see itself. A decision whose justification lives only in
    the context of an agent that has already exited is a decision the next heartbeat — and the user — cannot
    audit, and it would be the one artifact of the mechanism that has no evidence behind it.
    """
    path, _ = setup(tmp, "norec.jsonl", pr_origin="gauntlet")
    missing = path.parent / "nope.md"
    code, _, err = R.run(["--file", str(path), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(missing)])
    check(code == 1, f"a decision with NO record was accepted (exit {code})")
    check("does not exist or is empty" in err, f"wrong reason: {err!r}")

    empty = path.parent / "empty.md"
    empty.write_text("   \n\n")
    code, _, err = R.run(["--file", str(path), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(empty)])
    check(code == 1, f"a decision with an EMPTY record was accepted (exit {code})")
    check(field(path, "repair_count") == "0", "a refused decision spent the budget anyway")


def t_decision_enum_is_closed(tmp: Path) -> None:
    """The enum is CLOSED — argparse refuses anything else, and the five have a definition each.

    "Think about it and do something sensible" is precisely what a fresh-context driver holding one finding
    already did, twenty-one times. A decision outside the enum is not a decision, it is improvisation.
    """
    path, record = setup(tmp, "enum.jsonl", pr_origin="gauntlet")
    for bogus in ("fix", "retry", "RESCOPE", "rescope-and-merge", ""):
        code, _, _ = decide(path, record, bogus)
        check(code == 2, f"the decision {bogus!r} was not rejected by the parser (exit {code})")
    check(set(R.DECISIONS) == {"rescope", "repair-intent", "demote", "root-cause", "abort"},
            f"the enum changed: {sorted(R.DECISIONS)} — a new decision needs an ownership ruling "
            f"(does it rewrite branch content?) before it can ship")
    for name, why in R.DECISIONS.items():
        check(len(why) > 80, f"the decision {name!r} has no real definition — the agent is told nothing")


def t_the_repair_dispatch_gate(tmp: Path) -> None:
    """A repair may be dispatched ONLY after its decision is recorded — and ordinary work never may.

    This is the hole the guard would otherwise have: a driver could call its next targeted fix "the repair",
    dispatch it, and go on whacking moles under a new name. The decision must exist first.
    """
    path, record = setup(tmp, "gate.jsonl", pr_origin="gauntlet")

    code, _, err = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == L.EXIT_STOP, f"a repair was dispatchable with NO decision recorded (exit {code})")
    check("NO REASSESSMENT DECISION" in err, f"wrong reason: {err!r}")

    code, _, err = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1"])
    check(code == L.EXIT_STOP, f"ordinary work was dispatchable on a repairing PR (exit {code})")

    decide(path, record, "rescope")

    code, out, _ = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == 0, f"the DECIDED repair was refused (exit {code})")
    check("rescope" in out, f"dispatch-check must name the decided repair, so the right work runs: {out!r}")

    code, _, _ = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1"])
    check(code == L.EXIT_STOP, "ordinary work is STILL frozen while the repair is outstanding")

    # …and once the repair has landed and the driver returns the row to the gate, everything is normal again.
    ledger_cli(["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
    code, _, _ = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1"])
    check(code == 0, "the PR never returned to the gate after its repair")
    code, _, _ = ledger_cli(["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == L.EXIT_STOP, "a repair was still dispatchable after the PR returned to the gate")


def t_shared_module_loader_preserves_importlib_semantics(tmp: Path) -> None:
    """The shared loader preserves registration choices and lets execution exceptions pass through."""
    plain_name = "gauntlet_loader_plain"
    plain_path = tmp / "plain.py"
    plain_path.write_text("VALUE = 42\n")
    sys.modules.pop(plain_name, None)
    plain = load_module_from_path(plain_name, plain_path)
    check(plain is not None, "a Python source file returned no module")
    assert plain is not None
    check(plain.VALUE == 42, "the helper did not execute an unregistered module")
    check(plain_name not in sys.modules, "register=False added the module to sys.modules")

    registered_name = "gauntlet_loader_registered"
    registered_path = tmp / "registered.py"
    registered_path.write_text("import sys\nSEES_SELF = sys.modules[__name__] is sys.modules.get(__name__)\n")
    sys.modules.pop(registered_name, None)
    try:
        registered = load_module_from_path(registered_name, registered_path, register=True)
        check(registered is not None, "a Python source file returned no module")
        assert registered is not None
        check(registered.SEES_SELF, "registration happened after module execution")
        check(sys.modules.get(registered_name) is registered, "register=True stored a different module")
    finally:
        sys.modules.pop(registered_name, None)

    broken_name = "gauntlet_loader_broken"
    broken_path = tmp / "broken.py"
    broken_path.write_text("raise RuntimeError('module execution failed')\n")
    sys.modules.pop(broken_name, None)
    try:
        try:
            load_module_from_path(broken_name, broken_path, register=True)
        except RuntimeError as exc:
            check(str(exc) == "module execution failed", f"the execution exception changed: {exc!r}")
        else:
            check(False, "an exception from module execution was swallowed")
        check(broken_name in sys.modules, "a failed registered load removed its sys.modules entry")
    finally:
        sys.modules.pop(broken_name, None)

    check(load_module_from_path("gauntlet_loader_no_spec", tmp / "no-extension") is None,
          "a path with no executable module spec was accepted")


CASES = [
    ("external-not-rewritten", "an external PR refuses RESCOPE and ROOT-CAUSE, and takes the other three", t_external_pr_is_never_rewritten),
    ("gauntlet-takes-all", "a campaign-authored PR may take every decision", t_gauntlet_pr_takes_every_repair),
    ("unknown-is-external", "an unset origin is EXTERNAL — the fail-safe direction", t_unknown_origin_is_treated_as_external),
    ("budget-spent", "at REPAIR_CAP the only decision left is abort", t_repair_budget_is_spent),
    ("abort-leaves-it-open", "abort is terminal, leaves the PR OPEN, and reuses the existing procedure", t_abort_is_terminal_and_leaves_the_pr_open),
    ("only-a-capped-pr", "a PR that never hit a cap cannot be reassessed", t_only_a_capped_pr_may_be_reassessed),
    ("decision-needs-record", "no decision without its reasoning on disk", t_a_decision_needs_a_record),
    ("enum-is-closed", "the decision enum is closed, and each member is defined", t_decision_enum_is_closed),
    ("repair-dispatch-gate", "a repair needs a recorded decision; ordinary work stays frozen", t_the_repair_dispatch_gate),
    ("shared-module-loader", "path loading preserves registration and exception behavior", t_shared_module_loader_preserves_importlib_semantics),
]
