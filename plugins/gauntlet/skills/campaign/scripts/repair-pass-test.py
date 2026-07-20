#!/usr/bin/env python3
"""Fixtures for `repair-pass.py` — the reassessment bundle, decision, ownership guardrail, and repair cap.

They live in a SIBLING file, and `repair-pass.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. Three of these guard
things a well-meaning driver would otherwise do without noticing: reassess a PR that never hit a cap,
rewrite a PR belonging to someone else, and repair forever.
"""

from __future__ import annotations

import json
import hashlib
import subprocess
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
# The audit's schema owner — fixtures now produce real `.jsonl` audits through it, the way the runtime does,
# instead of hand-writing the old `.md` file the bundle no longer reads.
def _load_finding_audit():
    """Load finding-audit.py by path, guarding+raising so the value is non-Optional — the same shape as
    `_load_owner`, so `FA.main` is well-typed inside every fixture. A module-level `assert FA is not None`
    would NOT type-narrow the global inside function bodies; the guarded helper does."""
    mod = load_module_from_path("repair_pass_test_finding_audit", OWNER.parent / "finding-audit.py")
    if mod is None:
        raise RuntimeError(f"cannot load the finding-audit accessor at {OWNER.parent / 'finding-audit.py'}")
    return mod


FA = _load_finding_audit()

SHA = "c" * 40


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise R.SelfTestFailure(msg)


def ledger_cli(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the LEDGER's real CLI in-process — the guard and the row reads live there, not here."""
    return capture_cli(L.main, argv)


def decision_repo(tmp: Path) -> "tuple[Path, str]":
    """One clean Git worktree shared by a fixture's decision-door ledgers."""
    repo = tmp / "decision-worktree"
    if not repo.exists():
        repo.mkdir()
        commands = [
            ["git", "-C", str(repo), "init", "-q", "-b", "main"],
            ["git", "-C", str(repo), "config", "user.name", "Gauntlet Test"],
            ["git", "-C", str(repo), "config", "user.email", "gauntlet@example.invalid"],
        ]
        for argv in commands:
            result = subprocess.run(argv, capture_output=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode(errors="replace"))
        (repo / "base.txt").write_text("base\n")
        for argv in (["git", "-C", str(repo), "add", "base.txt"],
                     ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
                     # decide re-resolves `origin/<base>` to verify the bundle's pinned base SHA (F2), so the
                     # decision-door worktree must carry the remote-tracking ref, at the one commit it holds.
                     ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"]):
            result = subprocess.run(argv, capture_output=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode(errors="replace"))
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return repo, result.stdout.decode().strip()


def setup(tmp: Path, name: str = "state.jsonl", *, decision: str = "demote", **row) -> "tuple[Path, Path]":
    """A ledger holding one PR at a cap, plus a written decision record declaring `decision`.

    Written RAW (never through `dump()`), because `repair_count` and `review_rounds` have no CLI door —
    that is the point of them — so a fixture that needs a PR mid-budget must place it there directly.

    `decision` is the enum the record's `DECISION:` line declares — decide requires it to equal `--decision`,
    so a fixture whose decide call succeeds must set it to the decision it will pass. A fixture that expects
    refusal BEFORE the decision-field check (wrong status, spent budget, external rewrite, bad bundle) may
    leave the default: those never reach the `DECISION:` comparison.
    """
    path = tmp / name
    repo, head_sha = decision_repo(tmp)
    fields = {**L.ROW_DEFAULTS, "pr": "1", "head_sha": head_sha, "worktree": str(repo.resolve()),
              "status": L.REPAIR_STATUS,
              "review_rounds": str(L.ROUND_CAP), "ns_streak": "1", **row}
    path.write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS, "base_branch": "main"}) + "\n"
        + json.dumps({"type": "row", **fields}) + "\n"
    )
    record = tmp / f"repair-{name}.md"
    bind_record(path, record, fields["pr"], fields["head_sha"], repo, decision=decision)
    return path, record


def bundle_for(record: Path) -> Path:
    prompt = record.with_name(record.name + ".prompt.txt")
    return R.bundle_manifest_path(prompt)


def bind_record(ledger: Path, record: Path, pr: str, head_sha: str, worktree: Path,
                decision: str = "demote") -> Path:
    """Create the smallest valid prepared-bundle witness for decision-door fixtures.

    The record declares its chosen decision in the machine-readable `DECISION: <decision>` line decide reads.
    """
    _, rows = L.load(ledger)
    row = L.find_row(rows, pr)
    check(row is not None, f"fixture ledger has no row for pr {pr}")
    assert row is not None
    permitted = R.permitted_record(row)
    # `decision_repo` points `origin/main` at its single commit, which is also this fixture's `head_sha`, so
    # the bundle's pinned base SHA (F2) is `head_sha` here — decide re-resolves `origin/main` and matches it.
    payload = R.canonical_json({
        "schema": R.BUNDLE_SCHEMA,
        "pr": pr,
        "head_sha": head_sha,
        "base_ref": "origin/main",
        "base_sha": head_sha,
        "ledger": {"path": str(ledger.resolve()), "row": R.decision_projection(row)},
        "intent": {},
        "rounds": [],
        "diff_growth": [],
        "current_diff": "",
        "permitted": permitted,
        "decision_definitions": {name: R.DECISIONS[name] for name in permitted["permitted"]},
    }).encode()
    bundle_hash = hashlib.sha256(payload).hexdigest()
    marker = f"{R.BUNDLE_MARKER}: {bundle_hash}"
    prompt = record.with_name(record.name + ".prompt.txt")
    prompt_bytes = f"REASSESSMENT PASS\n{marker}\n\n".encode() + payload
    prompt.write_bytes(prompt_bytes)
    manifest = bundle_for(record)
    manifest.write_text(R.canonical_json({
        "schema": R.MANIFEST_SCHEMA,
        "bundle_sha256": bundle_hash,
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "prompt_path": str(prompt),
        "ledger_path": str(ledger.resolve()),
        "run_dir": str(record.parent.resolve()),
        "worktree": str(worktree.resolve()),
        "pr": pr,
        "head_sha": head_sha,
        "base_ref": "origin/main",
        "base_sha": head_sha,
        "rounds": [],
    }))
    record.write_text(
        f"{marker}\n\nDECISION: {decision}\n\n# reassessment\n\n"
        f"21 rounds, the diff tripled, the findings left the purpose.\n")
    return manifest


def decide(path: Path, record: Path, decision: str, pr: str = "1") -> "tuple[int, str, str]":
    return R.run(["--file", str(path), "decide", "--pr", pr, "--decision", decision,
                  "--record", str(record), "--bundle-manifest", str(bundle_for(record))])


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
        path, record = setup(tmp, f"ext-{decision}.jsonl", pr_origin="external", decision=decision)
        code, _, err = decide(path, record, decision)
        check(code == 1, f"[{decision}] an EXTERNAL PR was rewritten by an autonomous repair (exit {code})")
        check("did not open this PR" in err, f"[{decision}] refused for the wrong reason: {err!r}")
        check(field(path, "repair_count") == "0", f"[{decision}] a REFUSED decision spent the budget")
        check(field(path, "repair_decision") == "-", f"[{decision}] a REFUSED decision was recorded")

    for decision in R.EXTERNAL_PERMITTED:
        path, record = setup(tmp, f"ok-{decision}.jsonl", pr_origin="external", decision=decision)
        code, _, err = decide(path, record, decision)
        check(code == 0, f"[{decision}] a PERMITTED repair on an external PR was refused: {err!r}")
        check(field(path, "repair_decision").startswith(decision), "the decision was not recorded")


def t_gauntlet_pr_takes_every_repair(tmp: Path) -> None:
    """A PR campaign itself opened may take ALL FIVE decisions — the guardrail is about OWNERSHIP, not fear.

    Each fixture record DECLARES the decision it will pass, because decide now requires the record's
    `DECISION:` line to equal `--decision`; a generic record shared across every enum would no longer bind.
    """
    for decision in R.DECISIONS:
        path, record = setup(tmp, f"own-{decision}.jsonl", pr_origin="gauntlet", decision=decision)
        code, _, err = decide(path, record, decision)
        check(code == 0, f"[{decision}] refused on a campaign-authored PR: {err!r}")
        check(field(path, "repair_decision").startswith(decision), f"[{decision}] not recorded")


def t_unknown_origin_is_treated_as_external(tmp: Path) -> None:
    """A row whose origin was never established is EXTERNAL — the fail-safe direction, and the default.

    Guessing wrong in the other direction lets an autonomous mechanism rewrite a stranger's branch because
    a field was never set. "I do not know who wrote this" must never resolve to "I did".
    """
    path = tmp / "unset.jsonl"
    repo, head_sha = decision_repo(tmp)
    path.write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS, "base_branch": "main"}) + "\n"
        # A row written BEFORE `pr_origin` existed — no such key at all. It must read back `external`.
        + json.dumps({"type": "row", "pr": "1", "head_sha": head_sha, "worktree": str(repo.resolve()),
                      "status": L.REPAIR_STATUS}) + "\n"
    )
    record = tmp / "r.md"
    bind_record(path, record, "1", head_sha, repo)
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

    path, record = setup(tmp, "spent-abort.jsonl", pr_origin="gauntlet", repair_count=str(L.REPAIR_CAP),
                         decision="abort")
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
    path, record = setup(tmp, "abort.jsonl", pr_origin="gauntlet", decision="abort")
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
    path, record = setup(tmp, "norec.jsonl", pr_origin="gauntlet")
    missing = path.parent / "nope.md"
    code, _, err = R.run(["--file", str(path), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(missing), "--bundle-manifest", str(bundle_for(record))])
    check(code == 1, f"a decision with NO record was accepted (exit {code})")
    check("does not exist or is empty" in err, f"wrong reason: {err!r}")

    empty = path.parent / "empty.md"
    empty.write_text("   \n\n")
    code, _, err = R.run(["--file", str(path), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(empty), "--bundle-manifest", str(bundle_for(record))])
    check(code == 1, f"a decision with an EMPTY record was accepted (exit {code})")
    check(field(path, "repair_count") == "0", "a refused decision spent the budget anyway")


def t_record_decision_must_match_the_argument(tmp: Path) -> None:
    """The record's DECLARED decision must equal `--decision`, and the `DECISION:` field must be well-formed.

    The record survives the fresh-heartbeat context boundary; `--decision` does not. If decide trusted
    `--decision` alone, a record concluding DEMOTE could be recorded as a terminal, irreversible ABORT and
    the ledger would silently disagree with the audit artifact. ABORT is the sharp case — it flips the row to
    `aborted` and the driver drops the labels — so a mismatch must mutate NOTHING. This also pins the
    fail-closed shape of the field itself: absent, duplicated, and not-permitted are each refused.
    """
    # gauntlet + budget unspent, so BOTH demote and abort are permitted: the refusal is the DECISION
    # mismatch itself, not the ownership guardrail or the spent-budget rule.
    path, record = setup(tmp, "mismatch.jsonl", pr_origin="gauntlet", decision="demote")
    code, _, err = decide(path, record, "abort")
    check(code == 1, f"a record declaring DEMOTE was recorded as ABORT (exit {code})")
    check("declares" in err and "abort" in err, f"refused for the wrong reason: {err!r}")
    check(field(path, "repair_count") == "0", "a refused mismatch spent the repair budget")
    check(field(path, "repair_decision") == "-", "a refused mismatch recorded a decision")
    check(field(path, "status") == L.REPAIR_STATUS,
          f"a refused mismatch flipped the row to {field(path, 'status')!r} — the abort branch ran anyway")

    marker_line = record.read_text().splitlines()[0]

    def decide_record(text: str) -> "tuple[int, str, str]":
        other = tmp / f"variant-{abs(hash(text))}.md"
        other.write_text(text)
        return R.run(["--file", str(path), "decide", "--pr", "1", "--decision", "demote",
                      "--record", str(other), "--bundle-manifest", str(bundle_for(record))])

    code, _, err = decide_record(f"{marker_line}\n\nno machine-readable decision line here at all.\n")
    check(code == 1 and "exactly one" in err, f"a record with NO DECISION line was accepted: {err!r}")
    code, _, err = decide_record(f"{marker_line}\n\nDECISION: demote\nDECISION: demote\n")
    check(code == 1 and "exactly one" in err, f"a record with TWO DECISION lines was accepted: {err!r}")
    code, _, err = decide_record(f"{marker_line}\n\nDECISION: teleport\n")
    check(code == 1 and "not a permitted decision" in err, f"an unknown DECISION was accepted: {err!r}")
    check(field(path, "repair_count") == "0", "a malformed DECISION field spent the repair budget")


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
    path, record = setup(tmp, "gate.jsonl", pr_origin="gauntlet", decision="rescope")

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


# --- the prepared reassessment bundle ----------------------------------------

INTENT = """## Purpose
- assemble complete repair history deterministically

## Non-goals
- deciding which repair is correct

## Threat model
- Who can write the inputs this code reads: the campaign driver and review workers
- Who cannot: unrelated repository users
"""


def git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, check=False)
    check(result.returncode == 0,
          f"git {' '.join(argv)} failed: {result.stderr.decode(errors='replace')!r}")
    return result


def init_repo(parent: Path, name: str = "worktree") -> "tuple[Path, str]":
    repo = parent / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Gauntlet Test")
    git(repo, "config", "user.email", "gauntlet@example.invalid")
    (repo / "feature.txt").write_text("base\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "feature.txt").write_text("base\nfeature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature change")
    return repo, git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def write_review_attempt(rundir: Path, round_no: int, attempt: int, head_sha: str,
                         report: str) -> Path:
    RP = R.RP
    stem = f"review-1-{round_no}" + (f".a{attempt}" if attempt >= 2 else "")
    plan = rundir / f"review-1-{round_no}.plan.jsonl"
    if not plan.exists():
        plan.write_text(json.dumps({
            "type": RP.UNIT,
            "id": "u01",
            "kind": "file",
            "target": "feature.txt",
            "checks": ["the complete PR diff and stated purpose"],
        }) + "\n")
    progress = rundir / f"{stem}.progress.jsonl"
    records = [
        {"type": RP.IDENTITY, "pr": "1", "pass": str(round_no), "head_sha": head_sha,
         "launch_attempt": str(attempt), "dispatched_at": "2026-07-20T01:02:03Z"},
        {"type": RP.PROGRESS, "unit": "u01", "status": RP.STARTED},
        {"type": RP.PROGRESS, "unit": "u01", "status": RP.DONE,
         "evidence": "feature.txt:1-2 reviewed against the purpose"},
    ]
    progress.write_text("".join(json.dumps(record) + "\n" for record in records))
    (rundir / f"{stem}.txt").write_text(report)
    return progress


def make_audit(rundir: Path, round_no: int, attempt: int = 1) -> Path:
    """Produce a round's finding audit the REAL way — through `finding-audit.py` against the round's ACTIVE
    attempt — recording one CONFIRMED verdict for every gating finding it exposes.

    This replaces the pre-jsonl fixtures' hand-written `audit-1-<n>.md`: those bytes were never a real audit,
    so a `.md`-only fixture would pass a bundle reader that only checked "the file exists". The bundle now
    reads `audit-1-<n>.jsonl`, so the fixtures must produce it the way the runtime does. The round's active
    progress and findings (and the intent) must already be on disk.
    """
    stem = f"review-1-{round_no}" + (f".a{attempt}" if attempt >= 2 else "")
    progress = rundir / f"{stem}.progress.jsonl"
    audit = rundir / f"audit-1-{round_no}.jsonl"
    code, out, err = capture_cli(FA.main, ["init", "--file", str(audit), "--progress", str(progress)])
    check(code == 0, f"audit init for round {round_no} failed: {err!r}")
    for item in json.loads(out)["gating"]:
        code, _, err = capture_cli(FA.main, [
            "record", "--file", str(audit), "--finding-id", item["finding_id"],
            "--verdict", "CONFIRMED", "--evidence", f"round {round_no} audit verified the finding"])
        check(code == 0, f"audit record for round {round_no} failed: {err!r}")
    return audit


# One gating finding, reused wherever a cap round must carry the finding its NOT-SATISFIED verdict implies.
GATING_FINDING = {
    "file": "feature.txt",
    "line": "2",
    "writer": "end-user",
    "purpose": "assemble complete repair history deterministically",
    "repro": "supply the feature input through the documented user path",
    "fix": "handle the feature input at the shared boundary",
}


def write_cap_finding(rundir: Path, round_no: int, attempt: int = 1) -> Path:
    """Give a cap round the one gating finding it must carry: a cap trips only on NOT SATISFIED, and
    review-pass.py's coherence rule (an IF AND ONLY IF) makes that mean at least one gating finding."""
    stem = f"review-1-{round_no}" + (f".a{attempt}" if attempt >= 2 else "")
    path = rundir / f"{stem}.findings.jsonl"
    path.write_text(json.dumps({"type": R.RP.FINDING, **GATING_FINDING}) + "\n")
    return path


def bundle_setup(tmp: Path, *, rounds: int = 1, relaunch_round: "int | None" = None,
                 origin: str = "external", hostile_names: bool = False, cap_finding: bool = True) -> dict:
    tmp.mkdir(parents=True, exist_ok=True)
    repo_name = "worktree `quoted` --" if hostile_names else "worktree"
    run_name = "run $value 'quoted' --" if hostile_names else "run"
    repo, head_sha = init_repo(tmp, repo_name)
    rundir = tmp / run_name
    rundir.mkdir()
    (rundir / "intent-1.md").write_text(INTENT)
    for round_no in range(1, rounds + 1):
        is_cap = round_no == rounds
        active_attempt = 2 if relaunch_round == round_no else 1
        report = f"round {round_no}\n\nVERDICT: NOT SATISFIED\n"
        # Every round here returns NOT SATISFIED, and #126's `parse_report` verdict now coheres with the
        # findings per round (review-pass.py's IF AND ONLY IF). So each round carries a gating finding, and
        # every NON-cap round also records its finding audit — a cap round legitimately skips its audit (the
        # NOT-SATISFIED action sequence goes straight to `repairing`).
        write_review_attempt(rundir, round_no, 1, head_sha, report)
        if relaunch_round == round_no:
            write_review_attempt(rundir, round_no, 1, head_sha,
                                 "DEAD ATTEMPT 1 MUST NOT ENTER THE BUNDLE\n")
            write_review_attempt(rundir, round_no, 2, head_sha,
                                 "ACTIVE ATTEMPT 2\n\nVERDICT: NOT SATISFIED\n")
        if not is_cap:
            # The non-cap round's gating finding, then its REAL `.jsonl` audit produced through
            # finding-audit.py against the active attempt — the artifact the bundle now reads.
            write_cap_finding(rundir, round_no, active_attempt)
            make_audit(rundir, round_no, active_attempt)

    # The last round is the cap round (the NOT SATISFIED that tripped the cap and set `repairing`). By the
    # coherence rule it must carry a gating finding; give it one so the history is usable. `cap_finding=False`
    # builds the incoherent history the cap-needs-finding refusal fixture needs.
    if cap_finding:
        write_cap_finding(rundir, rounds, 2 if relaunch_round == rounds else 1)

    ledger = rundir / "state.jsonl"
    row = {**L.ROW_DEFAULTS, "pr": "1", "head_sha": head_sha, "status": L.REPAIR_STATUS,
           "review_rounds": str(rounds), "ns_streak": "1", "pr_origin": origin,
           "worktree": str(repo.resolve())}
    ledger.write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS, "base_branch": "main"}) + "\n"
        + json.dumps({"type": "row", **row}) + "\n")
    return {"repo": repo, "rundir": rundir, "ledger": ledger, "row": row, "head_sha": head_sha}


def run_bundle(case: dict, output: Path) -> "tuple[int, str, str]":
    return R.run(["--file", str(case["ledger"]), "bundle", "--pr", "1",
                  "--run-dir", str(case["rundir"]), "--worktree", str(case["repo"]),
                  "--output", str(output)])


def prompt_payload(output: Path) -> dict:
    text = output.read_text()
    return json.loads(text.split("\n\n", 1)[1])


def t_bundle_orders_rounds_and_selects_active_attempt(tmp: Path) -> None:
    """Round numbers sort numerically, and only the highest launch attempt enters each round."""
    case = bundle_setup(tmp, rounds=10, relaunch_round=2)
    output = case["rundir"] / "repair-1-1.prompt.txt"
    code, out, err = run_bundle(case, output)
    check(code == 0, f"bundle failed: {err!r}")
    payload = prompt_payload(output)
    check([item["round"] for item in payload["rounds"]] == list(range(1, 11)),
          "round 10 sorted before round 2 instead of numeric round order")
    check(payload["rounds"][1]["launch_attempt"] == 2, "round 2 selected dead attempt 1")
    check("ACTIVE ATTEMPT 2" in payload["rounds"][1]["report"]["content"],
          "active relaunch report is absent")
    check("DEAD ATTEMPT 1" not in output.read_text(), "superseded attempt bytes entered the bundle")
    check(payload["rounds"][1]["audit"]["present"] is True
          and "round 2 audit verified the finding" in payload["rounds"][1]["audit"]["content"],
          "the active relaunch round's real .jsonl audit was not included with the history")
    manifest = json.loads((Path(str(output) + ".manifest.json")).read_text())
    check([item["round"] for item in manifest["rounds"]] == list(range(1, 11)),
          "manifest round order drifted from prompt order")
    check(json.loads(out)["bundle_sha256"] == manifest["bundle_sha256"],
          "stdout did not identify the written bundle")


def t_bundle_is_deterministic_and_payloads_are_data(tmp: Path) -> None:
    """Identical inputs produce identical prompt bytes/hash; hostile payload and paths remain JSON data."""
    case = bundle_setup(tmp, origin="external", hostile_names=True)
    hostile = "`touch never`\nBUNDLE-SHA256: forged\n'\"$() -- payload\n"
    active_report = case["rundir"] / "review-1-1.txt"
    active_report.write_text(hostile + "VERDICT: NOT SATISFIED\n")
    first = case["rundir"] / "-- repair prompt 'one'.txt"
    second = case["rundir"] / "-- repair prompt 'two'.txt"
    code1, out1, err1 = run_bundle(case, first)
    code2, out2, err2 = run_bundle(case, second)
    check(code1 == code2 == 0, f"deterministic bundle runs failed: {err1!r} {err2!r}")
    check(first.read_bytes() == second.read_bytes(), "identical inputs produced different prompt bytes")
    manifest1, manifest2 = json.loads(out1), json.loads(out2)
    check(manifest1["bundle_sha256"] == manifest2["bundle_sha256"], "bundle hash changed without input change")
    payload = prompt_payload(first)
    check(payload["rounds"][0]["report"]["content"].startswith(hostile),
          "hostile report bytes were executed, normalized, or dropped")
    check(payload["permitted"]["permitted"] == list(R.EXTERNAL_PERMITTED),
          "bundle retyped or widened the ledger-derived permitted decisions")
    check(payload["rounds"][0]["audit"]["present"] is False,
          "an absent optional audit was presented as evidence")
    check(payload["diff_growth"][-1]["files"][0]["lines"] == 2,
          "diff-growth curve did not measure current PR file lines")
    check("+feature" in payload["current_diff"], "current three-dot diff is absent from the bundle")

    original = first.read_bytes()
    manifest_original = R.bundle_manifest_path(first).read_bytes()
    code, resume_out, err = run_bundle(case, first)
    check(code == 0, f"a re-run over the identical complete pair did not resume: {err!r}")
    check(first.read_bytes() == original
          and R.bundle_manifest_path(first).read_bytes() == manifest_original,
          "resume rewrote the existing prompt/manifest instead of reusing them byte-for-byte")
    check(json.loads(resume_out)["bundle_sha256"] == manifest1["bundle_sha256"],
          "resume returned a different bundle than the one already on disk")

    dangling = case["rundir"] / "dangling-output.txt"
    dangling.symlink_to(case["rundir"] / "missing-target")
    code, _, err = run_bundle(case, dangling)
    check(code == 1 and "already exists" in err, f"dangling output symlink was overwritten: {err!r}")
    check(dangling.is_symlink(), "refused dangling output was changed")


def t_bundle_refuses_missing_stale_and_duplicate_inputs(tmp: Path) -> None:
    """Missing reports, stale heads, and duplicate identities fail before either output exists."""
    missing_case = bundle_setup(tmp / "missing")
    (missing_case["rundir"] / "review-1-1.txt").unlink()
    missing_output = missing_case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(missing_case, missing_output)
    # The report is now read through `parse_report` (the one sanctioned reader), whose missing-file Defect
    # names the "active review report" — routing it there is exactly the F1 fix.
    check(code == 1 and "active review report" in err, f"missing report was not refused: {err!r}")
    check(not missing_output.exists() and not Path(str(missing_output) + ".manifest.json").exists(),
          "missing input left partial bundle output")

    stale_case = bundle_setup(tmp / "stale")
    stale_case["ledger"].write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS, "base_branch": "main"}) + "\n"
        + json.dumps({"type": "row", **stale_case["row"], "head_sha": "d" * 40}) + "\n")
    stale_output = stale_case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(stale_case, stale_output)
    check(code == 1 and "stale ledger head" in err, f"stale head was not refused: {err!r}")
    check(not stale_output.exists(), "stale head left bundle output")

    duplicate_case = bundle_setup(tmp / "duplicate")
    progress = duplicate_case["rundir"] / "review-1-1.progress.jsonl"
    lines = progress.read_text().splitlines()
    progress.write_text(lines[0] + "\n" + lines[0] + "\n" + "\n".join(lines[1:]) + "\n")
    duplicate_output = duplicate_case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(duplicate_case, duplicate_output)
    check(code == 1 and "pass_identity" in err, f"duplicate active identity was not refused: {err!r}")
    check(not duplicate_output.exists(), "duplicate active attempt left bundle output")

    # Round 1 of a 2-round history is a non-cap round that carries a gating finding, so it MUST record its
    # finding audit. Drop that audit and the bundle refuses the round before any output.
    audit_case = bundle_setup(tmp / "missing-audit", rounds=2)
    (audit_case["rundir"] / "audit-1-1.jsonl").unlink()
    audit_output = audit_case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(audit_case, audit_output)
    check(code == 1 and "missing required finding audit" in err,
          f"missing audit from an earlier gating round was accepted: {err!r}")
    check(not audit_output.exists(), "missing earlier audit left bundle output")


def t_bundle_preserves_findings_from_an_older_intent(tmp: Path) -> None:
    """Old intent anchors remain readable, and a cap round may correctly have no finding audit yet."""
    case = bundle_setup(tmp)
    findings = case["rundir"] / "review-1-1.findings.jsonl"
    findings.write_text(json.dumps({
        "type": R.RP.FINDING,
        "file": "feature.txt",
        "line": "2",
        "writer": "end-user",
        "purpose": "purpose text from the intent that governed round 1",
        "repro": "supply the feature input through the documented user path",
        "fix": "handle the feature input at the shared boundary",
    }) + "\n")
    output = case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(case, output)
    check(code == 0, f"historical purpose anchor was re-judged against the current intent: {err!r}")
    payload = prompt_payload(output)
    check("round 1" in payload["rounds"][0]["findings"]["content"],
          "historical finding bytes were not preserved")
    check(payload["rounds"][0]["gating_findings"] == 1
          and payload["rounds"][0]["audit"]["present"] is False,
          "the cap round incorrectly required an audit the NOT-SATISFIED sequence skips")


def t_bundle_exempts_every_prior_cap_round(tmp: Path) -> None:
    """A SECOND repair cap builds its bundle — an EARLIER cap round's legitimately absent audit is exempt too.

    A cap round skips its audit (the NOT-SATISFIED sequence goes straight to `repairing`). `REPAIR_CAP`
    allows a second cap, and once it is reached the FIRST cap round is no longer the latest. If only the
    latest round were exempt, that earlier cap's absent audit would fail the bundle — so the repairing PR
    could neither take its second repair nor even ABORT through the sanctioned door (both need a valid
    bundle). The earlier cap is recovered from the manifest it wrote at the first cap.
    """
    case = bundle_setup(tmp, rounds=1)  # first cap at round 1 (gating finding, no audit)
    first_output = case["rundir"] / "repair-1-1.prompt.txt"
    code, _, err = run_bundle(case, first_output)
    check(code == 0, f"the first cap bundle failed: {err!r}")
    check(R.bundle_manifest_path(first_output).exists(),
          "the first cap did not leave the manifest a later cap recovers its round from")

    # The repair landed, the row returned to review, two more rounds ran, and round 3 is the SECOND cap.
    head = case["head_sha"]
    # A clean intermediate round: SATISFIED coheres with 0 gating findings, and #126's `parse_report`
    # requires its one RESIDUAL-RISK line immediately above the verdict. No audit is owed (0 gating).
    write_review_attempt(case["rundir"], 2, 1, head,
                         "round 2\n\nRESIDUAL-RISK: feature.txt — the clean re-review after the repair "
                         "landed\nVERDICT: SATISFIED\n")
    write_review_attempt(case["rundir"], 3, 1, head, "round 3\n\nVERDICT: NOT SATISFIED\n")
    write_cap_finding(case["rundir"], 3)  # the second cap round carries its gating finding, no audit
    # `review_rounds`/`repair_count` have no CLI door, so place the second-cap state directly.
    row = {**case["row"], "review_rounds": "3", "repair_count": "1"}
    case["ledger"].write_text(
        json.dumps({"type": "header", **L.HEADER_DEFAULTS, "base_branch": "main"}) + "\n"
        + json.dumps({"type": "row", **row}) + "\n")

    second_output = case["rundir"] / "repair-1-2.prompt.txt"
    code, out, err = run_bundle(case, second_output)
    check(code == 0, f"the second cap bundle rejected the earlier cap round's absent audit: {err!r}")
    payload = prompt_payload(second_output)
    check([item["round"] for item in payload["rounds"]] == [1, 2, 3],
          "the second cap history is not rounds 1..3")
    check(payload["rounds"][0]["gating_findings"] == 1 and payload["rounds"][0]["audit"]["present"] is False,
          "the FIRST cap round should keep its gating finding and its legitimately-absent audit")
    check(payload["rounds"][2]["gating_findings"] == 1 and payload["rounds"][2]["audit"]["present"] is False,
          "the SECOND cap round should keep its gating finding and its legitimately-absent audit")
    check(json.loads(out)["bundle_sha256"] == R.sha256_bytes(
              R.canonical_json(payload).encode("utf-8")),
          "the emitted bundle hash does not cover the second cap payload")


def t_bundle_audit_read_does_not_re_anchor(tmp: Path) -> None:
    """A landed round's finding audit is embedded as HISTORICAL EVIDENCE and is NEVER re-anchored to the
    current intent — the audit-side mirror of `load_historical_findings` and of every-prior-cap exemption.

    After a REPAIR-INTENT re-authors `intent-<pr>.md` and drops a purpose an earlier round anchored to, that
    round's audit — bound to the dropped purpose through its source findings — must still read back into the
    next cap's bundle. finding-audit.py's OWN read door (`verify`) re-reads those source findings and
    re-anchors their purposes to the new intent, so it would reject the audit and WEDGE the bundle; the bundle
    reads it through review-pass.py's non-re-anchoring line parser instead, exactly as it reads the findings
    beside it. Without this, a repairing PR could neither take its second repair nor ABORT through the
    sanctioned door — both need a buildable bundle.
    """
    case = bundle_setup(tmp, rounds=2, origin="external")  # round 1 non-cap with a REAL audit; round 2 cap
    audit_path = case["rundir"] / "audit-1-1.jsonl"
    check(audit_path.exists(), "round 1 should have produced a real .jsonl audit through finding-audit.py")

    # REPAIR-INTENT re-authors the intent, dropping the purpose round 1's finding (and its audit) anchored to.
    (case["rundir"] / "intent-1.md").write_text(
        "## Purpose\n- a wholly re-authored purpose written by the intent repair\n\n"
        "## Non-goals\n- deciding which repair is correct\n\n"
        "## Threat model\n"
        "- Who can write the inputs this code reads: the campaign driver and review workers\n"
        "- Who cannot: unrelated repository users\n")

    # finding-audit.py's OWN read door now REJECTS this audit: it re-anchors the source findings to the new
    # intent, where the round's purpose no longer exists. This is exactly the door the bundle must NOT use.
    code_v, _, err_v = capture_cli(FA.main, ["verify", "--file", str(audit_path)])
    check(code_v == 1 and "Purpose" in err_v,
          f"the re-anchoring door unexpectedly accepted the post-repair audit — the guard is moot: {err_v!r}")

    output = case["rundir"] / "repair-1-1.prompt.txt"
    code, _, err = run_bundle(case, output)
    check(code == 0, f"the bundle re-anchored a historical audit to the re-authored intent and wedged: {err!r}")
    payload = prompt_payload(output)
    check(payload["rounds"][0]["audit"]["present"] is True
          and "round 1 audit verified the finding" in payload["rounds"][0]["audit"]["content"],
          "the historical audit was not embedded as evidence after the intent was re-authored")


def t_bundle_refuses_a_header_only_audit(tmp: Path) -> None:
    """A landed round's HEADER-ONLY finding audit is incomplete history and is refused before any output.

    `finding-audit.py init` writes the header and zero `audit_result` rows. Those bytes are well-formed
    JSONL with a valid header, so `parse_lines` alone would embed them as `present: True` indistinguishably
    from a complete audit — the one landed artifact re-validated only for well-formedness. The bundle now
    also runs finding-audit.py's header-internal completeness check, so a round whose header names gating
    findings but records no result for them fails closed. (Red before the fix: the header-only audit passed
    as complete and the bundle succeeded.)
    """
    case = bundle_setup(tmp, rounds=2)  # round 1 non-cap with a REAL complete audit, round 2 cap
    audit_path = case["rundir"] / "audit-1-1.jsonl"
    lines = audit_path.read_text().splitlines()
    check(len(lines) >= 2, "round 1's real audit should carry a header plus at least one audit_result row")
    # Reduce it to what `init` alone writes: the header, no results. Well-formed, valid header, incomplete.
    audit_path.write_text(lines[0] + "\n")

    output = case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(case, output)
    check(code == 1 and "incomplete" in err,
          f"a header-only audit was embedded as complete history rather than refused: {err!r}")
    check(not output.exists() and not R.bundle_manifest_path(output).exists(),
          "a refused bundle must leave no output")


def t_bundle_audit_completeness_is_header_internal(tmp: Path) -> None:
    """The bundle's audit COMPLETENESS check reads only the audit's own header — it NEVER re-anchors.

    A COMPLETE audit whose source purpose was later dropped from `intent-<pr>.md` must still validate as
    complete, exactly as `t_bundle_audit_read_does_not_re_anchor` requires of the whole bundle. If the new
    completeness check went through finding-audit.py's re-anchoring door (`verify`/`load_audit`), it would
    re-read the round's source findings, fail to anchor the dropped purpose, reject the audit, and WEDGE the
    bundle. This pins that the check stays header-internal: `verify` rejects the same audit, the
    header-internal check accepts it.
    """
    case = bundle_setup(tmp, rounds=2, origin="external")  # round 1 non-cap with a REAL complete audit
    audit_path = case["rundir"] / "audit-1-1.jsonl"
    check(audit_path.exists(), "round 1 should have produced a real, complete .jsonl audit")

    # REPAIR-INTENT re-authors the intent, dropping the purpose round 1's finding (and its audit) anchored to.
    (case["rundir"] / "intent-1.md").write_text(
        "## Purpose\n- a wholly re-authored purpose written by the intent repair\n\n"
        "## Non-goals\n- deciding which repair is correct\n\n"
        "## Threat model\n"
        "- Who can write the inputs this code reads: the campaign driver and review workers\n"
        "- Who cannot: unrelated repository users\n")

    text = audit_path.read_text()
    # finding-audit.py's RE-ANCHORING door rejects this complete audit: its source purpose is gone from intent.
    code_v, _, err_v = capture_cli(FA.main, ["verify", "--file", str(audit_path)])
    check(code_v == 1 and "Purpose" in err_v,
          f"the re-anchoring door unexpectedly accepted the post-repair audit — the contrast is moot: {err_v!r}")
    # The header-internal completeness check reads only the audit's own bytes, so the complete audit passes.
    try:
        FA.check_landed_audit_complete(text, audit_path)
    except FA.AuditError as exc:
        check(False, f"the completeness check re-anchored or mis-read a complete historical audit: {exc}")


def t_bundle_refuses_a_cap_round_with_no_gating_finding(tmp: Path) -> None:
    """A cap round that records NO gating finding is unusable history and is refused before any output.

    A cap trips ONLY on a NOT SATISFIED, and review-pass.py's coherence rule is an IF AND ONLY IF: NOT
    SATISFIED exactly when at least one gating finding stands. So a cap round with zero gating findings is a
    NOT SATISFIED with nothing behind it — history review-pass.py itself rejects — and must not reach the
    reassessment worker dressed as sound evidence.
    """
    case = bundle_setup(tmp, rounds=1, cap_finding=False)  # cap round 1 with NO findings at all
    output = case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(case, output)
    check(code == 1 and "NO gating finding" in err,
          f"a cap round with zero gating findings was bundled as sound history: {err!r}")
    check(not output.exists() and not R.bundle_manifest_path(output).exists(),
          "the refused incoherent history left bundle output")


def t_bundle_refuses_a_report_with_no_verdict(tmp: Path) -> None:
    """A truncated / prose-only active report with no terminal `VERDICT:` line is refused (F1).

    Every sibling active artifact — progress, identity, findings — is re-validated through the sanctioned
    `review-pass.py` readers and fails CLOSED on malformed input. The report was the one exception: read with
    a bare exists+nonempty check, a killed-worker report (the file's own founding scenario) bundled at exit 0
    while `review-pass.py`'s own reader rejects it. Routing it through #126's `parse_report` — the ONE
    sanctioned report reader — closes that fail-open gap without a second parser.
    """
    case = bundle_setup(tmp, rounds=1, origin="external")  # cap_finding=True: the gating finding is present
    report = case["rundir"] / "review-1-1.txt"
    report.write_text("The reviewer began analyzing feature.txt and then the process was killed mid-sentence\n")
    output = case["rundir"] / "bundle.txt"
    code, _, err = run_bundle(case, output)
    check(code == 1 and "VERDICT:" in err,
          f"a report with no terminal VERDICT line was bundled instead of refused: {err!r}")
    check(not output.exists() and not R.bundle_manifest_path(output).exists(),
          "the refused unparseable report left bundle output")


def t_bundle_pins_base_sha_against_a_racing_fetch(tmp: Path) -> None:
    """`origin/<base>` is resolved to ONE immutable SHA before any read, so a concurrent fetch that advances
    the remote-tracking ref mid-build cannot make the bundle mix two base SHAs (F2).

    Before the fix, `current_diff` resolved `origin/<base>` and `diff_growth` resolved it again; a fetch
    landing between them produced one bundle describing the PR against two different bases, at exit 0, with
    only the symbolic ref in the manifest so decide could not detect the mix. Pinning the base to a SHA up
    front is the symmetric completion of the HEAD-moved guard: every read uses the same pinned base.
    """
    case = bundle_setup(tmp, rounds=1, origin="external")
    repo, head = case["repo"], case["head_sha"]
    base_before = git(repo, "rev-parse", "origin/main").stdout.decode().strip()
    real_git = R._run_git
    advanced = False

    def racing_git(worktree: Path, *argv: str) -> subprocess.CompletedProcess:
        """Advance origin/main right after the current-diff read, as a real background fetch would."""
        nonlocal advanced
        result = real_git(worktree, *argv)
        if not advanced and argv[:3] == ("diff", "--binary", "--no-ext-diff"):
            git(worktree, "update-ref", "refs/remotes/origin/main", head)
            advanced = True
        return result

    setattr(R, "_run_git", racing_git)
    try:
        output = case["rundir"] / "repair-1-1.prompt.txt"
        code, _, err = run_bundle(case, output)
    finally:
        setattr(R, "_run_git", real_git)

    check(advanced, "the fixture never advanced origin/main mid-build — the race was not exercised")
    base_after = git(repo, "rev-parse", "origin/main").stdout.decode().strip()
    check(base_after == head and base_after != base_before,
          "the fixture did not actually move origin/main between reads")
    check(code == 0, f"bundle refused a build whose base was pinned before any read: {err!r}")
    payload = prompt_payload(output)
    manifest = json.loads(R.bundle_manifest_path(output).read_text())
    check(payload["base_sha"] == base_before,
          f"bundle did not pin the pre-race base SHA: {payload['base_sha']!r} != {base_before!r}")
    check(manifest["base_sha"] == base_before, "manifest bound a different base SHA than the payload")
    # The single pinned base governs BOTH reads: the feature change is present in the current diff and the
    # growth curve measures exactly the one feature commit against that base — never one-vs-old, one-vs-new.
    check("+feature" in payload["current_diff"],
          "the current diff lost the feature change under the pinned base")
    check(len(payload["diff_growth"]) == 1,
          f"diff_growth measured against the moved ref, not the pinned base: "
          f"{len(payload['diff_growth'])} commits")


def t_bundle_resumes_after_context_loss(tmp: Path) -> None:
    """A heartbeat that built the bundle and died before `decide` re-runs `bundle` and RESUMES, not wedges.

    The documented resume branch re-runs `bundle` whenever `repair_decision == "-"`, and every heartbeat is
    a fresh agent. A complete, matching prompt/manifest pair on disk is reused byte-for-byte; a partial pair
    left by a crash mid-write is regenerated; a foreign pair is refused rather than overwritten.
    """
    case = bundle_setup(tmp, origin="external")
    output = case["rundir"] / "repair-1-1.prompt.txt"
    manifest_path = R.bundle_manifest_path(output)
    code, out1, err = run_bundle(case, output)
    check(code == 0, f"the first bundle failed: {err!r}")
    first_prompt = output.read_bytes()
    first_manifest = manifest_path.read_bytes()

    # A fresh heartbeat re-enters the same `repair_decision == "-"` branch and re-runs bundle.
    code, out2, err = run_bundle(case, output)
    check(code == 0, f"the resume run wedged instead of reusing the prepared bundle: {err!r}")
    check(output.read_bytes() == first_prompt and manifest_path.read_bytes() == first_manifest,
          "the resume run rewrote the prepared bundle instead of reusing it byte-for-byte")
    check(json.loads(out2)["bundle_sha256"] == json.loads(out1)["bundle_sha256"],
          "the resume run returned a different bundle than the one already on disk")

    # A partial pair — the manifest never landed (killed between the two atomic promotions) — regenerates.
    manifest_path.unlink()
    code, out3, err = run_bundle(case, output)
    check(code == 0, f"a partial (manifest-less) bundle was not regenerated: {err!r}")
    check(manifest_path.exists() and output.read_bytes() == first_prompt,
          "regenerating a partial bundle did not restore the complete deterministic pair")
    check(json.loads(out3)["bundle_sha256"] == json.loads(out1)["bundle_sha256"],
          "the regenerated bundle differs from the deterministic original")

    # A foreign prompt at the path (bytes that are NOT this bundle) is refused, never reused or overwritten.
    output.write_bytes(b"not the prepared bundle\n")
    code, _, err = run_bundle(case, output)
    check(code == 1 and "does NOT match" in err,
          f"a foreign bundle at the output path was reused or overwritten: {err!r}")
    check(output.read_bytes() == b"not the prepared bundle\n", "the refused foreign output was overwritten")


def t_bundle_identity_ignores_liveness_but_not_decision_fields(tmp: Path) -> None:
    """A CI-observation write to a liveness field must NOT wedge resume; a decision-field change still does.

    While a row is `repairing` the CI watch still OBSERVES the PR (observing is not mutating), so a liveness
    counter such as `settled_strikes` legitimately moves between two `bundle` runs. That write must not shift
    the bundle's deterministic identity — only the `DECISION_FIELDS` projection may — so the next `bundle`
    RESUMES the pair byte-for-byte and `decide` still accepts the bundle a record was already bound to. The
    mirror image also holds: a change to a decision field IS a real drift, so `decide` refuses it as stale.
    """
    case = bundle_setup(tmp / "liveness", origin="external")
    output = case["rundir"] / "repair-1-1.prompt.txt"
    manifest_path = R.bundle_manifest_path(output)
    code, out1, err = run_bundle(case, output)
    check(code == 0, f"the first bundle failed: {err!r}")
    first_prompt, first_manifest = output.read_bytes(), manifest_path.read_bytes()
    bundle_sha = json.loads(out1)["bundle_sha256"]

    # A routine CI observation bumps a LIVENESS field on the repairing row; the decision fields are untouched.
    header_line, row_line = case["ledger"].read_text().splitlines()
    row = json.loads(row_line)
    check(str(row["settled_strikes"]) != "2", "fixture assumed a settled_strikes value the row already held")
    row["settled_strikes"] = "2"
    case["ledger"].write_text(header_line + "\n" + json.dumps(row) + "\n")

    # The next heartbeat re-runs bundle: the liveness write is OUT of the identity, so it resumes the pair.
    code, out2, err = run_bundle(case, output)
    check(code == 0, f"a liveness write wedged bundle resume: {err!r}")
    check(output.read_bytes() == first_prompt and manifest_path.read_bytes() == first_manifest,
          "a liveness write changed the deterministic bundle bytes")
    check(json.loads(out2)["bundle_sha256"] == bundle_sha,
          "a liveness write moved the bundle identity a decision record is bound to")

    # decide still ACCEPTS the bundle after the liveness drift — its staleness check reads only the projection.
    record = case["rundir"] / "repair-1-1.md"
    record.write_text(f"{R.BUNDLE_MARKER}: {bundle_sha}\n\nDECISION: demote\n\n"
                      f"The finding is outside the PR's purpose.\n")
    code, _, err = R.run(["--file", str(case["ledger"]), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(record), "--bundle-manifest", str(manifest_path)])
    check(code == 0, f"decide refused a valid bundle after a mere liveness write: {err!r}")
    check(field(case["ledger"], "repair_decision").startswith("demote@"),
          "the liveness-drift decision was not recorded")

    # Mirror image: a DECISION-field change is a real drift, so the bundle-bound decide refuses it as stale.
    drift = bundle_setup(tmp / "decision-drift", origin="external")
    drift_output = drift["rundir"] / "repair-1-1.prompt.txt"
    code, drift_out, err = run_bundle(drift, drift_output)
    check(code == 0, f"the decision-drift bundle failed: {err!r}")
    drift_sha = json.loads(drift_out)["bundle_sha256"]
    header_line, row_line = drift["ledger"].read_text().splitlines()
    row = json.loads(row_line)
    row["ns_streak"] = str(int(row["ns_streak"]) + 1)  # a decision field moved after the bundle was built
    drift["ledger"].write_text(header_line + "\n" + json.dumps(row) + "\n")
    drift_record = drift["rundir"] / "repair-1-1.md"
    drift_record.write_text(f"{R.BUNDLE_MARKER}: {drift_sha}\n\nDECISION: demote\n\nStale-bundle demote.\n")
    code, _, err = R.run(["--file", str(drift["ledger"]), "decide", "--pr", "1", "--decision", "demote",
                          "--record", str(drift_record),
                          "--bundle-manifest", str(R.bundle_manifest_path(drift_output))])
    check(code == 1 and "ns_streak changed" in err, f"a decision-field drift was not caught as stale: {err!r}")
    check(field(drift["ledger"], "repair_count") == "0", "a stale-bundle decision spent the repair budget")


def t_bundle_git_and_atomic_failures_leave_no_output(tmp: Path) -> None:
    """A failed Git read or second promotion leaves neither prompt nor manifest behind."""
    git_case = bundle_setup(tmp / "git-failure")
    git_output = git_case["rundir"] / "bundle.txt"
    real_git = R._run_git

    def broken_git(worktree: Path, *argv: str) -> subprocess.CompletedProcess:
        if argv[:2] == ("diff", "--binary"):
            return subprocess.CompletedProcess([], 1, b"", b"injected diff failure")
        return real_git(worktree, *argv)

    setattr(R, "_run_git", broken_git)
    try:
        code, _, err = run_bundle(git_case, git_output)
    finally:
        setattr(R, "_run_git", real_git)
    check(code == 1 and "Git read failed" in err, f"Git failure did not fail closed: {err!r}")
    check(not git_output.exists() and not Path(str(git_output) + ".manifest.json").exists(),
          "Git failure left partial output")

    atomic_case = bundle_setup(tmp / "atomic-failure")
    atomic_output = atomic_case["rundir"] / "bundle.txt"
    real_replace = R.os.replace
    calls = 0

    def fail_second(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected manifest promotion failure")
        real_replace(source, target)

    R.os.replace = fail_second
    try:
        code, _, err = run_bundle(atomic_case, atomic_output)
    finally:
        R.os.replace = real_replace
    check(code == 1 and "atomically" in err, f"atomic promotion failure was not reported: {err!r}")
    check(not atomic_output.exists() and not Path(str(atomic_output) + ".manifest.json").exists(),
          "second promotion failure left a partial bundle")
    check(not list(atomic_output.parent.glob(".*.tmp")), "bundle failure left staged temp files")

    moving_case = bundle_setup(tmp / "moving-head")
    moving_output = moving_case["rundir"] / "bundle.txt"
    real_git = R._run_git
    moved = False

    def moving_git(worktree: Path, *argv: str) -> subprocess.CompletedProcess:
        nonlocal moved
        if not moved and argv[:3] == ("diff", "--binary", "--no-ext-diff"):
            (worktree / "moved-during-bundle.txt").write_text("new head\n")
            git(worktree, "add", "moved-during-bundle.txt")
            git(worktree, "commit", "-q", "-m", "move during bundle")
            moved = True
        return real_git(worktree, *argv)

    setattr(R, "_run_git", moving_git)
    try:
        code, _, err = run_bundle(moving_case, moving_output)
    finally:
        setattr(R, "_run_git", real_git)
    check(code == 1 and "HEAD moved while building" in err,
          f"bundle mixed inputs from two heads instead of refusing: {err!r}")
    check(not moving_output.exists() and not Path(str(moving_output) + ".manifest.json").exists(),
          "moving head left a partial bundle")


def t_decide_is_bound_to_prepared_bundle(tmp: Path) -> None:
    """A decision record must copy the exact prepared bundle hash before it can spend the repair budget."""
    case = bundle_setup(tmp, origin="external")
    output = case["rundir"] / "repair-1-1.prompt.txt"
    code, out, err = run_bundle(case, output)
    check(code == 0, f"bundle failed: {err!r}")
    manifest = json.loads(out)
    record = case["rundir"] / "repair-1-1.md"
    record.write_text("BUNDLE-SHA256: " + "0" * 64 + "\n\nDECISION: demote\n\nThe finding is outside purpose.\n")
    argv = ["--file", str(case["ledger"]), "decide", "--pr", "1", "--decision", "demote",
            "--record", str(record), "--bundle-manifest", manifest["manifest_path"]]
    code, _, err = R.run(argv)
    check(code == 1 and "not bound to this bundle" in err, f"wrong bundle hash was accepted: {err!r}")
    check(field(case["ledger"], "repair_count") == "0", "wrong bundle hash spent the repair budget")
    record.write_text(f"{R.BUNDLE_MARKER}: {manifest['bundle_sha256']}\n\nDECISION: demote\n\n"
                      f"It is outside purpose.\n")

    manifest_path = Path(manifest["manifest_path"])
    manifest_doc = json.loads(manifest_path.read_text())
    manifest_doc["rounds"] = []
    manifest_path.write_text(R.canonical_json(manifest_doc))
    code, _, err = R.run(argv)
    check(code == 1 and "round summary" in err, f"manifest detached from its prompt was accepted: {err!r}")
    check(field(case["ledger"], "repair_count") == "0", "detached manifest spent the repair budget")
    manifest_doc["rounds"] = manifest["rounds"]
    manifest_path.write_text(R.canonical_json(manifest_doc))

    code, _, err = R.run(argv)
    check(code == 0, f"matching bundle hash was refused: {err!r}")
    check(field(case["ledger"], "repair_decision").startswith("demote@"),
          "bundle-bound decision was not recorded")
    code, _, err = R.run(argv)
    check(code == 1 and "exactly one decision" in err, f"one bundle was replayed for a second decision: {err!r}")
    check(field(case["ledger"], "repair_count") == "1", "replayed bundle spent the repair budget twice")

    moved = bundle_setup(tmp / "moved", origin="external")
    moved_output = moved["rundir"] / "repair-1-1.prompt.txt"
    code, moved_out, err = run_bundle(moved, moved_output)
    check(code == 0, f"second bundle failed: {err!r}")
    moved_manifest = json.loads(moved_out)
    (moved["repo"] / "after-bundle.txt").write_text("new head\n")
    git(moved["repo"], "add", "after-bundle.txt")
    git(moved["repo"], "commit", "-q", "-m", "move after bundle")
    moved_record = moved["rundir"] / "repair-1-1.md"
    moved_record.write_text(
        f"{R.BUNDLE_MARKER}: {moved_manifest['bundle_sha256']}\n\nDECISION: demote\n\nBased on the old head.\n")
    code, _, err = R.run([
        "--file", str(moved["ledger"]), "decide", "--pr", "1", "--decision", "demote",
        "--record", str(moved_record), "--bundle-manifest", moved_manifest["manifest_path"],
    ])
    check(code == 1 and "worktree HEAD moved" in err, f"decision for a moved head was accepted: {err!r}")
    check(field(moved["ledger"], "repair_count") == "0", "stale post-bundle decision spent repair budget")


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
    ("record-decision-binds", "the record's DECISION field must be well-formed and equal --decision", t_record_decision_must_match_the_argument),
    ("enum-is-closed", "the decision enum is closed, and each member is defined", t_decision_enum_is_closed),
    ("repair-dispatch-gate", "a repair needs a recorded decision; ordinary work stays frozen", t_the_repair_dispatch_gate),
    ("bundle-order-active", "bundle orders rounds numerically and selects only the active relaunch", t_bundle_orders_rounds_and_selects_active_attempt),
    ("bundle-deterministic", "bundle bytes/hash are deterministic and hostile payloads stay data", t_bundle_is_deterministic_and_payloads_are_data),
    ("bundle-refusals", "missing, stale, and duplicate active inputs fail before output", t_bundle_refuses_missing_stale_and_duplicate_inputs),
    ("bundle-old-intent", "old intent anchors survive; the cap round may have no audit yet", t_bundle_preserves_findings_from_an_older_intent),
    ("bundle-prior-cap", "a second cap builds; every earlier cap round's absent audit is exempt", t_bundle_exempts_every_prior_cap_round),
    ("bundle-audit-no-re-anchor", "a historical audit reads back after the intent is re-authored (no re-anchor)", t_bundle_audit_read_does_not_re_anchor),
    ("bundle-audit-header-only", "a header-only (incomplete) audit is refused, not embedded as present", t_bundle_refuses_a_header_only_audit),
    ("bundle-audit-complete-header-internal", "the completeness check is header-internal, never re-anchoring", t_bundle_audit_completeness_is_header_internal),
    ("bundle-cap-needs-finding", "a cap round with no gating finding is unusable history and refused", t_bundle_refuses_a_cap_round_with_no_gating_finding),
    ("bundle-report-needs-verdict", "a report with no terminal VERDICT line fails closed through parse_report", t_bundle_refuses_a_report_with_no_verdict),
    ("bundle-base-sha-pinned", "the base is pinned to one SHA before any read; a racing fetch cannot mix bases", t_bundle_pins_base_sha_against_a_racing_fetch),
    ("bundle-resume", "a bundle rebuilt after context loss reuses or regenerates, never wedges", t_bundle_resumes_after_context_loss),
    ("bundle-identity-scope", "a liveness write never wedges resume; a decision-field drift is still stale", t_bundle_identity_ignores_liveness_but_not_decision_fields),
    ("bundle-atomic", "Git and atomic-write failures leave no partial bundle", t_bundle_git_and_atomic_failures_leave_no_output),
    ("decision-bundle-bound", "decide accepts only a record bound to the matching prepared bundle", t_decide_is_bound_to_prepared_bundle),
    ("shared-module-loader", "path loading preserves registration and exception behavior", t_shared_module_loader_preserves_importlib_semantics),
]
