#!/usr/bin/env python3
"""The REASSESSMENT PASS's door — the only sanctioned way to record what to do about a PR that has stopped
converging.

The campaign review gate had no memory. Every heartbeat was a fresh agent instance, `reviews_ok` was zeroed on
every NOT SATISFIED, and nothing counted rounds — so the ledger after 21 review rounds was indistinguishable
from the ledger after 1, and every stopping rule in the skill was a rule with NO SENSOR. Two PRs ran 21 and
14 adversarial rounds, produced a true finding almost every time, and never converged. A human stopped it at
8.5 hours, and could only do so by holding all 21 rounds in mind at once.

`ledger.py` now carries the memory (`review_rounds`, `ns_streak`) and the caps. When one is reached the row
goes `repairing` and ordinary gate work is REFUSED for it. This file owns what happens next: a
context-isolated agent is handed THE WHOLE HISTORY AT ONCE — every round's verdict and finding, the
diff-growth curve, the PR's intent artifact, the current diff — and returns exactly ONE decision from a
CLOSED enum. `references/repair-pass.md` is the definition; this is its enforcement.

`bundle` assembles that history deterministically from validated active-attempt artifacts and Git reads;
`decide` accepts only a record carrying the exact prepared bundle hash.

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
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

DESCRIPTION = "Build and bind the reassessment pass for a PR that has stopped converging."

OWNER = Path(__file__).resolve().parent / "ledger.py"
REVIEW_OWNER = Path(__file__).resolve().parent / "review-pass.py"


def load_ledger():
    """Load `ledger.py` BY PATH — it owns the schema, the caps and the statuses, and it owns them ONCE.

    Not by import: the cwd is the driver's worktree while the skill's scripts live wherever the plugin is
    installed. Re-declaring the field names or the caps here would be a second copy of the schema, which is
    the exact defect `ledger.py` exists to prevent.
    """
    mod = load_module_from_path("ledger", OWNER)
    if mod is None:  # a broken install — never an input error
        print(f"repair-pass: cannot load its schema owner at {OWNER}", file=sys.stderr)
        raise SystemExit(1)
    return mod


L = load_ledger()


def load_review_pass():
    """Load the review artifact owner by installed path, never by ambient cwd or import path."""
    mod = load_module_from_path("repair_pass_review_owner", REVIEW_OWNER)
    if mod is None:  # a broken install — never an input error
        print(f"repair-pass: cannot load its review artifact owner at {REVIEW_OWNER}", file=sys.stderr)
        raise SystemExit(1)
    return mod


RP = load_review_pass()

BUNDLE_SCHEMA = "gauntlet-repair-bundle-v1"
MANIFEST_SCHEMA = "gauntlet-repair-bundle-manifest-v1"
BUNDLE_MARKER = "BUNDLE-SHA256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

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


def permitted_record(row: dict) -> dict:
    """The machine-readable decision set used by both `permitted` and `bundle`."""
    allowed = permitted_for(row)
    spent = L.counter(row, "repair_count") >= L.REPAIR_CAP
    return {
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
    }


def get_row(path: Path, pr: str) -> dict:
    _, rows = L.load(path)
    row = L.find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}")
    return row


def cmd_permitted(path: Path, args) -> int:
    """Print the decisions this PR may take, and why — the reassessment prompt is BUILT from this."""
    row = get_row(path, str(args.pr))
    print(json.dumps(permitted_record(row)))
    return 0


# --- deterministic reassessment bundle --------------------------------------

def canonical_json(value: object) -> str:
    """Stable JSON bytes for prompts, hashes, and manifests."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_utf8(path: Path, what: str, *, allow_empty: bool = False) -> str:
    if not path.exists():
        fail(f"missing required {what} at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"cannot read {what} {path} as UTF-8: {exc}")
    if not allow_empty and not text.strip():
        fail(f"required {what} at {path} is empty")
    return text


def artifact(path: Path, content: str, *, present: bool = True) -> dict:
    data = content.encode("utf-8")
    return {
        "path": str(path.resolve()),
        "present": present,
        "sha256": sha256_bytes(data),
        "content": content,
    }


def _run_git(worktree: Path, *argv: str) -> subprocess.CompletedProcess:
    """Run fixed Git argv in the supplied worktree. Dynamic values never enter shell source."""
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-c", "core.quotepath=true", "-C", str(worktree), *argv],
            capture_output=True, check=False)
    except OSError as exc:
        fail(f"Git could not run in {worktree}: {exc}")


def git_bytes(worktree: Path, *argv: str) -> bytes:
    proc = _run_git(worktree, *argv)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        fail(f"Git read failed (`git {' '.join(argv)}`) in {worktree}: {detail or f'exit {proc.returncode}'}")
    return proc.stdout


def git_text(worktree: Path, *argv: str) -> str:
    return git_bytes(worktree, *argv).decode("utf-8", errors="surrogateescape")


def attempt_report_path(progress: Path) -> Path:
    """Derive the per-attempt report by replacing only the progress suffix."""
    return progress.parent / (progress.name[: -len(RP.PROGRESS_SUFFIX)] + ".txt")


def load_historical_findings(progress: Path) -> list[dict]:
    """Validate a landed round's finding schema without re-anchoring it to a later repaired intent.

    `review-pass.py verify` proved each finding against the intent that governed that round before its
    verdict landed. REPAIR-INTENT may later replace the PR's one current intent artifact; re-validating old
    purpose strings against that new file would make the complete history unreadable at the next cap. Keep
    the artifact owner's strict reader and every non-anchor finding rule, while treating its recorded
    purpose strings as historical evidence rather than claims about the current intent.
    """
    path = RP.findings_path(progress)
    if not path.exists():
        return []
    RP.findings_name(path)
    records = RP.parse_lines(RP.read_text(path, "findings file"), path.name)
    historical_purposes = [rec.get("purpose") for rec in records
                           if isinstance(rec.get("purpose"), str) and rec.get("purpose") != RP.NO_PURPOSE]
    for line_no, rec in enumerate(records, start=1):
        RP.check_finding(rec, f"{path.name} line {line_no}", historical_purposes)
    return records


def select_active_rounds(rundir: Path, pr: str, expected_rounds: int) -> "list[tuple[int, int, Path]]":
    """Select one highest numbered launch attempt per numeric review round for this PR."""
    attempts: dict[int, dict[int, Path]] = {}
    for progress in rundir.glob("review-*-*" + RP.PROGRESS_SUFFIX):
        try:
            named_pr, named_round, named_attempt = RP.parse_name(progress)
        except RP.Defect:
            continue
        if named_pr != pr:
            continue
        round_no, attempt_no = int(named_round), int(named_attempt)
        by_attempt = attempts.setdefault(round_no, {})
        if attempt_no in by_attempt:
            fail(f"duplicate launch attempt {attempt_no} for pr {pr} review round {round_no}")
        by_attempt[attempt_no] = progress

    expected = list(range(1, expected_rounds + 1))
    actual = sorted(attempts)
    if actual != expected:
        fail(f"review history for pr {pr} has numeric rounds {actual}, expected every round {expected}")
    return [(round_no, max(attempts[round_no]), attempts[round_no][max(attempts[round_no])])
            for round_no in expected]


def prior_cap_rounds(rundir: Path, pr: str) -> "set[int]":
    """Round numbers that ended at an EARLIER repair cap, recovered from the bundle manifests those caps
    wrote.

    A cap round legitimately carries no finding audit: the NOT-SATISFIED action sequence skips it when
    `ledger.py verdict` moves the row straight to `repairing`. The CURRENT cap round is always
    `review_rounds` (`expected_rounds`). But `REPAIR_CAP` allows a SECOND cap, and once it is reached the
    FIRST cap round is no longer the latest — yet its absent audit is still legitimate. Each earlier cap
    wrote a validated `repair-<pr>-<k>.prompt.txt.manifest.json` naming rounds 1..R; that R is the cap
    round, and reading it back is how a later bundle still recognises the earlier cap. Deriving from the
    manifests the caps already wrote — rather than adding ledger state — is the sanctioned recovery.
    """
    caps: "set[int]" = set()
    for manifest_file in rundir.glob("*.manifest.json"):
        try:
            doc = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a manifest we cannot read names no cap round we can trust; skip it
        if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA or doc.get("pr") != pr:
            continue
        rounds = doc.get("rounds")
        if not isinstance(rounds, list):
            continue
        nums: "list[int]" = []
        for item in rounds:
            if not isinstance(item, dict):
                continue
            round_no = item.get("round")
            if isinstance(round_no, int):
                nums.append(round_no)
        if nums:
            caps.add(max(nums))
    return caps


def collect_rounds(rundir: Path, pr: str, expected_rounds: int) -> list[dict]:
    """Read exactly the active attempt's validated artifacts for every round."""
    # The current cap round plus every EARLIER cap round (F2): each of these legitimately has no audit and
    # must carry a gating finding, and this set is what tells the two rules below which rounds are caps.
    cap_rounds = prior_cap_rounds(rundir, pr) | {expected_rounds}
    rounds = []
    for round_no, attempt_no, progress in select_active_rounds(rundir, pr, expected_rounds):
        try:
            progress_text = RP.read_text(progress, "progress file")
            events, units = RP.check_progress_file(
                progress_text, progress, lambda p=progress: RP.load_plan(RP.plan_path(p)))
            _, done = RP.walk_progress(events, units)
            if len(done) != len(units):
                fail(f"active attempt {attempt_no} of pr {pr} round {round_no} is incomplete "
                     f"({len(done)} of {len(units)} plan units done)")
            identity = RP.check_identity(events, pr, str(round_no), str(attempt_no))
            findings = load_historical_findings(progress)
        except RP.Defect as exc:
            fail(f"active attempt {attempt_no} of pr {pr} round {round_no} is unusable: {exc}")

        plan = RP.plan_path(progress)
        plan_text = read_utf8(plan, "review plan")
        report = attempt_report_path(progress)
        report_text = read_utf8(report, "review report")
        findings_file = RP.findings_path(progress)
        if findings_file.exists():
            findings_text = read_utf8(findings_file, "review findings", allow_empty=True)
            findings_artifact = artifact(findings_file, findings_text)
        else:
            findings_artifact = artifact(findings_file, "", present=False)

        audit_file = rundir / f"audit-{pr}-{round_no}.md"
        gating_findings = sum(1 for finding in findings if RP.gating(finding))
        # F1 — a cap round's verdict is DERIVABLE from validated state: a cap trips ONLY on a NOT SATISFIED,
        # and review-pass.py's coherence rule is an IF AND ONLY IF — NOT SATISFIED exactly when at least one
        # GATING finding stands. So a cap round with zero gating findings is history review-pass.py itself
        # would reject as `unusable`; refuse it here rather than hand the reassessment worker a NOT SATISFIED
        # with nothing behind it. BOUNDARY: we check only the coherence the artifacts + the `repairing`
        # status make derivable. A NON-cap round's verdict lives solely in the report PROSE, which
        # review-pass.py deliberately never parses and neither do we — that round is left unchecked on
        # purpose, not by oversight; a prose verdict reader would be a second, competing spelling of the rule.
        if round_no in cap_rounds and not gating_findings:
            fail(f"pr {pr} review round {round_no} reached a repair cap (status={L.REPAIR_STATUS}) but "
                 f"records NO gating finding. A cap trips only on NOT SATISFIED, which by the coherence rule "
                 f"must carry at least one gating finding; this is history review-pass.py rejects as unusable")
        if audit_file.exists():
            audit_artifact = artifact(audit_file, read_utf8(audit_file, "finding audit"))
        elif gating_findings and round_no not in cap_rounds:
            fail(f"missing required finding audit for pr {pr} review round {round_no}")
        else:
            # The NOT-SATISFIED action sequence intentionally skips its audit when `ledger.py verdict`
            # moves the row straight to `repairing`; EVERY such cap round therefore has a valid absent
            # audit, not only the latest one. `cap_rounds` names them all — the current `review_rounds`
            # plus every earlier cap recovered from its bundle manifest (F2).
            audit_artifact = artifact(audit_file, "", present=False)

        rounds.append({
            "round": round_no,
            "launch_attempt": attempt_no,
            "review_head_sha": identity["head_sha"],
            "dispatched_at": identity["dispatched_at"],
            "plan": artifact(plan, plan_text),
            "progress": artifact(progress, progress_text),
            "report": artifact(report, report_text),
            "findings": findings_artifact,
            "audit": audit_artifact,
            "gating_findings": gating_findings,
        })
    return rounds


def diff_growth(worktree: Path, base_ref: str, head_sha: str) -> list[dict]:
    """Measure every current PR path at every commit, in Git's numeric chronological order."""
    raw_paths = git_bytes(worktree, "diff", "--name-only", "-z", f"{base_ref}...{head_sha}")
    paths = sorted(part for part in raw_paths.split(b"\0") if part)
    commits = [line for line in git_text(worktree, "rev-list", "--reverse", "--topo-order",
                                          f"{base_ref}..{head_sha}").splitlines() if line]
    curve = []
    for commit in commits:
        meta = git_bytes(worktree, "show", "-s", "--format=%aI%x00%s", commit).removesuffix(b"\n")
        pieces = meta.split(b"\0", 1)
        if len(pieces) != 2:
            fail(f"Git returned malformed commit metadata for {commit}")
        files = []
        for raw_path in paths:
            path = raw_path.decode("utf-8", errors="surrogateescape")
            entry = git_bytes(worktree, "ls-tree", "--full-tree", "-z", commit, "--", f":(literal){path}")
            if not entry:
                files.append({"path": path, "object_type": "absent", "lines": None, "bytes": None})
                continue
            entries = [item for item in entry.split(b"\0") if item]
            if len(entries) != 1 or b"\t" not in entries[0]:
                fail(f"Git returned malformed tree data for {commit}:{path}")
            metadata, returned_path = entries[0].split(b"\t", 1)
            if returned_path != raw_path:
                fail(f"Git returned the wrong tree path while measuring {commit}:{path}")
            fields = metadata.split()
            if len(fields) != 3:
                fail(f"Git returned malformed tree metadata for {commit}:{path}")
            object_type = fields[1].decode("ascii", errors="replace")
            if object_type != "blob":
                files.append({"path": path, "object_type": object_type, "lines": None, "bytes": None})
                continue
            content = git_bytes(worktree, "cat-file", "blob", f"{commit}:{path}")
            files.append({"path": path, "object_type": "blob", "lines": len(content.splitlines()),
                          "bytes": len(content)})
        curve.append({
            "commit": commit,
            "authored_at": pieces[0].decode("utf-8", errors="surrogateescape"),
            "subject": pieces[1].decode("utf-8", errors="surrogateescape"),
            "files": files,
        })
    return curve


def bundle_manifest_path(output: Path) -> Path:
    return output.with_name(output.name + ".manifest.json")


def _stage_bytes(path: Path, data: bytes, prefix: str) -> Path:
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def path_present(path: Path) -> bool:
    """True for every directory entry, including a dangling symlink that `Path.exists()` misses."""
    return path.exists() or path.is_symlink()


def write_bundle(output: Path, prompt: bytes, manifest_path: Path, manifest: bytes) -> None:
    """Stage both files before promotion; any reported failure leaves neither output behind."""
    if path_present(output) or path_present(manifest_path):
        conflicts = [str(path) for path in (output, manifest_path) if path_present(path)]
        fail(f"bundle output already exists: {', '.join(conflicts)}")
    if not output.parent.is_dir():
        fail(f"bundle output parent does not exist: {output.parent}")

    prompt_tmp = _stage_bytes(output, prompt, f".{output.name}.")
    manifest_tmp: "Path | None" = None
    prompt_promoted = False
    manifest_promoted = False
    try:
        manifest_tmp = _stage_bytes(manifest_path, manifest, f".{manifest_path.name}.")
        if path_present(output) or path_present(manifest_path):
            fail("bundle output appeared while the bundle was being prepared; refusing to overwrite it")
        os.replace(prompt_tmp, output)
        prompt_promoted = True
        os.replace(manifest_tmp, manifest_path)
        manifest_promoted = True
    except BaseException:
        prompt_tmp.unlink(missing_ok=True)
        if manifest_tmp is not None:
            manifest_tmp.unlink(missing_ok=True)
        if prompt_promoted:
            output.unlink(missing_ok=True)
        if manifest_promoted:
            manifest_path.unlink(missing_ok=True)
        raise


def reuse_existing_bundle(output: Path, manifest_path: Path, prompt: bytes, manifest_bytes: bytes) -> bool:
    """Decide what to do about a prompt/manifest pair that may already be on disk (F3 — resume).

    Returns True when the EXACT prepared pair is already present: adopt it. This is the resume the
    documented loop needs — a heartbeat that ran `bundle` and then ended before `decide` recorded a decision
    re-enters the same `repair_decision == "-"` branch and re-runs `bundle`; without this it wedged forever
    on "bundle output already exists". Returns False when nothing usable is present, having CLEARED a partial
    leftover so the caller may write fresh. FAILS on a foreign/stale pair or a symlink — never silently
    overwritten.

    bundle is DETERMINISTIC (`t_bundle_deterministic` pins it): identical inputs produce identical prompt and
    manifest BYTES. So an existing pair is THIS bundle if and ONLY if its bytes equal the freshly built bytes
    — that equality IS the validation against the live ledger, history, worktree, and head, needing no second
    reader. The worktree HEAD was already proven current above, and `repairing` freezes ordinary gate work
    (no fix, rebase, or merge), so the inputs cannot have shifted under a byte-for-byte matching pair.
    """
    out_link, man_link = output.is_symlink(), manifest_path.is_symlink()
    if out_link or man_link:
        # A bundle this tool writes is a regular file promoted by os.replace, NEVER a symlink. A symlink at
        # either path is foreign (or a hostile dangling link, which `resolve()` would follow off to its
        # missing target): never follow it, never reuse it, never overwrite it.
        conflicts = [str(p) for p, is_link in ((output, out_link), (manifest_path, man_link)) if is_link]
        fail(f"bundle output already exists as a symlink (refusing to follow it): {', '.join(conflicts)}")
    out_present, man_present = output.exists(), manifest_path.exists()
    if not out_present and not man_present:
        return False
    if out_present and man_present:
        try:
            same = output.read_bytes() == prompt and manifest_path.read_bytes() == manifest_bytes
        except OSError as exc:
            fail(f"cannot read the existing bundle pair to validate it: {exc}")
        if same:
            return True
        fail("a bundle already exists at this path and does NOT match the current ledger, history, and head "
             "— refusing to overwrite it. If it is stale, delete the prompt and its .manifest.json and rebuild")
    # Exactly one of the pair is present: a crash between the two atomic promotions left a PARTIAL bundle
    # that can neither be dispatched nor validated. Regenerate rather than wedge the PR forever.
    try:
        (output if out_present else manifest_path).unlink()
    except OSError as exc:
        fail(f"cannot clear the partial bundle leftover to regenerate it: {exc}")
    return False


def cmd_bundle(path: Path, args) -> int:
    """Build the complete deterministic input for one context-isolated reassessment."""
    pr = str(args.pr)
    header, rows = L.load(path)
    row = L.find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}")
    if row["status"] != L.REPAIR_STATUS:
        fail(f"pr {pr} is {row['status']}, not {L.REPAIR_STATUS} — no reassessment bundle is due")
    if row["repair_decision"] != "-":
        fail(f"pr {pr} already has reassessment decision {row['repair_decision']!r}; dispatch that decision "
             f"instead of preparing another bundle")

    rundir = Path(args.run_dir).resolve()
    worktree = Path(args.worktree).resolve()
    # Keep the final path component lexical so a dangling output symlink remains an existing artifact to
    # refuse. `resolve()` would follow it to its missing target and silently replace that target instead.
    output = Path(args.output).absolute()
    if not rundir.is_dir():
        fail(f"run directory does not exist: {rundir}")
    if not worktree.is_dir():
        fail(f"worktree does not exist: {worktree}")
    if path.resolve().parent != rundir:
        fail(f"--file {path.resolve()} is not inside --run-dir {rundir}; refusing to mix two runs' state")
    if output.parent != rundir:
        fail(f"--output {output} is not directly inside --run-dir {rundir}")
    try:
        recorded_worktree = Path(row["worktree"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        fail(f"ledger worktree {row['worktree']!r} cannot be resolved: {exc}")
    if worktree != recorded_worktree:
        fail(f"--worktree {worktree} does not match pr {pr}'s ledger worktree {recorded_worktree}")

    head_sha = git_text(worktree, "rev-parse", "HEAD").strip()
    if not RP.SHA_RE.match(head_sha):
        fail(f"Git returned a non-canonical HEAD {head_sha!r}")
    if row["head_sha"] != head_sha:
        fail(f"stale ledger head for pr {pr}: row has {row['head_sha']}, worktree HEAD is {head_sha}")

    base = header.get("base_branch", "-")
    if not isinstance(base, str) or not base.strip() or base == "-":
        fail("ledger header has no usable base_branch")
    base_ref = f"origin/{base}"
    git_bytes(worktree, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

    try:
        expected_rounds = int(row["review_rounds"])
    except (TypeError, ValueError):
        fail(f"pr {pr} has invalid review_rounds {row['review_rounds']!r}")
    if expected_rounds < 1:
        fail(f"pr {pr} is repairing with no landed review rounds")
    rounds = collect_rounds(rundir, pr, expected_rounds)
    if rounds[-1]["review_head_sha"] != head_sha:
        fail(f"latest active review ran on {rounds[-1]['review_head_sha']}, not current head {head_sha}")

    intent_file = rundir / f"intent-{pr}.md"
    try:
        RP.load_intent(intent_file)
    except RP.Defect as exc:
        fail(f"intent for pr {pr} is unusable: {exc}")
    intent_text = read_utf8(intent_file, "intent")
    current_diff = git_text(worktree, "diff", "--binary", "--no-ext-diff",
                            f"{base_ref}...{head_sha}")
    growth = diff_growth(worktree, base_ref, head_sha)
    final_head = git_text(worktree, "rev-parse", "HEAD").strip()
    if final_head != head_sha:
        fail(f"worktree HEAD moved while building the bundle: started at {head_sha}, ended at {final_head}")
    permitted = permitted_record(row)

    payload = {
        "schema": BUNDLE_SCHEMA,
        "pr": pr,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "ledger": {"path": str(path.resolve()), "row": {field: row[field] for field in L.ROW_FIELDS}},
        "intent": artifact(intent_file, intent_text),
        "rounds": rounds,
        "diff_growth": growth,
        "current_diff": current_diff,
        "permitted": permitted,
        "decision_definitions": {name: DECISIONS[name] for name in permitted["permitted"]},
    }
    payload_bytes = canonical_json(payload).encode("utf-8")
    bundle_hash = sha256_bytes(payload_bytes)
    marker = f"{BUNDLE_MARKER}: {bundle_hash}"
    prompt = (
        "REASSESSMENT PASS\n"
        "Read the complete JSON bundle below. Choose exactly one decision named in `permitted`; do not "
        "invent another decision. Explain how the complete history supports that decision. Write the "
        "decision record with the following marker as its first nonblank line so `repair-pass.py decide` "
        "can bind the decision to these exact bytes.\n"
        f"{marker}\n\n"
    ).encode("utf-8") + payload_bytes

    manifest_path = bundle_manifest_path(output)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "bundle_sha256": bundle_hash,
        "prompt_sha256": sha256_bytes(prompt),
        "prompt_path": str(output),
        "ledger_path": str(path.resolve()),
        "run_dir": str(rundir),
        "worktree": str(worktree),
        "pr": pr,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "rounds": [{"round": item["round"], "launch_attempt": item["launch_attempt"],
                    "review_head_sha": item["review_head_sha"]} for item in rounds],
    }
    manifest_bytes = canonical_json(manifest).encode("utf-8")
    if reuse_existing_bundle(output, manifest_path, prompt, manifest_bytes):
        # A prior heartbeat built this exact bundle and the process ended before `decide`; the bytes on disk
        # ARE what we just rebuilt, so adopt them instead of wedging. Fresh, partial, or non-matching pairs
        # have fallen through to write_bundle below (a partial one was cleared for regeneration).
        print(canonical_json({**manifest, "manifest_path": str(manifest_path)}), end="")
        return 0
    try:
        write_bundle(output, prompt, manifest_path, manifest_bytes)
    except OSError as exc:
        fail(f"could not write bundle atomically: {exc}")
    print(canonical_json({**manifest, "manifest_path": str(manifest_path)}), end="")
    return 0


MANIFEST_KEYS = {
    "schema", "bundle_sha256", "prompt_sha256", "prompt_path", "ledger_path", "run_dir", "worktree",
    "pr", "head_sha", "base_ref", "rounds",
}
BUNDLE_KEYS = {
    "schema", "pr", "head_sha", "base_ref", "ledger", "intent", "rounds", "diff_growth",
    "current_diff", "permitted", "decision_definitions",
}


def validate_decision_bundle(path: Path, header: dict, row: dict, pr: str, record: Path,
                             manifest_path: Path) -> None:
    """Prove the decision record names the exact prepared prompt bytes for this ledger row."""
    text = read_utf8(manifest_path, "bundle manifest")
    try:
        manifest = json.loads(text, object_pairs_hook=RP.strict_object(manifest_path.name, 1))
    except (json.JSONDecodeError, RP.Defect) as exc:
        fail(f"bundle manifest {manifest_path} is not strict JSON: {exc}")
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        fail(f"bundle manifest {manifest_path} has the wrong schema fields")
    if canonical_json(manifest) != text:
        fail(f"bundle manifest {manifest_path} is not canonical deterministic JSON")
    if manifest["schema"] != MANIFEST_SCHEMA:
        fail(f"bundle manifest {manifest_path} has unknown schema {manifest['schema']!r}")
    if manifest["pr"] != pr:
        fail(f"bundle manifest is for pr {manifest['pr']!r}, not pr {pr}")
    if manifest["ledger_path"] != str(path.resolve()):
        fail(f"bundle manifest is bound to ledger {manifest['ledger_path']!r}, not {str(path.resolve())!r}")
    if manifest["run_dir"] != str(path.resolve().parent):
        fail(f"bundle manifest is bound to run directory {manifest['run_dir']!r}, not {str(path.resolve().parent)!r}")
    if manifest["head_sha"] != row["head_sha"]:
        fail(f"bundle manifest is stale: it names {manifest['head_sha']!r}, row names {row['head_sha']!r}")
    expected_base_ref = f"origin/{header.get('base_branch', '-')}"
    if manifest["base_ref"] != expected_base_ref:
        fail(f"bundle manifest is bound to base {manifest['base_ref']!r}, not {expected_base_ref!r}")
    if not isinstance(manifest["worktree"], str):
        fail("bundle manifest worktree is not a string")
    try:
        recorded_worktree = Path(row["worktree"]).resolve(strict=True)
        manifest_worktree = Path(manifest["worktree"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        fail(f"bundle worktree cannot be resolved: {exc}")
    if manifest_worktree != recorded_worktree:
        fail(f"bundle manifest is bound to worktree {manifest_worktree}, not {recorded_worktree}")
    live_head = git_text(manifest_worktree, "rev-parse", "HEAD").strip()
    if live_head != row["head_sha"]:
        fail(f"bundle is stale: worktree HEAD moved to {live_head}, row and bundle name {row['head_sha']}")
    for field in ("bundle_sha256", "prompt_sha256"):
        if not isinstance(manifest[field], str) or not SHA256_RE.match(manifest[field]):
            fail(f"bundle manifest field {field} is not a sha256")
    if not isinstance(manifest["prompt_path"], str):
        fail("bundle manifest prompt_path is not a string")
    run_dir = path.resolve().parent
    try:
        prompt_path = Path(manifest["prompt_path"]).resolve(strict=True)
        actual_manifest_path = manifest_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        fail(f"bundle prompt or manifest cannot be resolved: {exc}")
    if prompt_path.parent != run_dir:
        fail(f"bundle prompt {prompt_path} is not directly inside run directory {run_dir}")
    if manifest["prompt_path"] != str(prompt_path):
        fail(f"bundle manifest prompt_path is not canonical: {manifest['prompt_path']!r}")
    expected_manifest_path = bundle_manifest_path(prompt_path)
    if actual_manifest_path != expected_manifest_path:
        fail(f"bundle manifest {actual_manifest_path} is not the prompt's sidecar {expected_manifest_path}")
    try:
        prompt = prompt_path.read_bytes()
    except OSError as exc:
        fail(f"cannot read bundle prompt {prompt_path}: {exc}")
    if sha256_bytes(prompt) != manifest["prompt_sha256"]:
        fail(f"bundle prompt {prompt_path} no longer matches its manifest hash")
    marker = f"{BUNDLE_MARKER}: {manifest['bundle_sha256']}"
    try:
        prompt_text = prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"bundle prompt {prompt_path} is not UTF-8: {exc}")
    if marker not in prompt_text.splitlines()[:8]:
        fail(f"bundle prompt {prompt_path} does not carry its bundle hash marker")
    prompt_parts = prompt.split(b"\n\n", 1)
    if len(prompt_parts) != 2 or sha256_bytes(prompt_parts[1]) != manifest["bundle_sha256"]:
        fail(f"bundle prompt {prompt_path} payload does not match its bundle hash")
    try:
        payload = json.loads(prompt_parts[1], object_pairs_hook=RP.strict_object(prompt_path.name, 1))
    except (json.JSONDecodeError, RP.Defect) as exc:
        fail(f"bundle prompt {prompt_path} payload is not strict JSON: {exc}")
    if (not isinstance(payload, dict) or set(payload) != BUNDLE_KEYS or payload.get("schema") != BUNDLE_SCHEMA
            or payload.get("pr") != pr or payload.get("head_sha") != row["head_sha"]):
        fail(f"bundle prompt {prompt_path} payload is not for this PR and head")
    if canonical_json(payload).encode("utf-8") != prompt_parts[1]:
        fail(f"bundle prompt {prompt_path} payload is not canonical deterministic JSON")
    if payload.get("base_ref") != manifest["base_ref"]:
        fail("bundle prompt and manifest name different base refs")
    ledger_snapshot = payload.get("ledger")
    if not isinstance(ledger_snapshot, dict) or set(ledger_snapshot) != {"path", "row"}:
        fail("bundle prompt has no valid ledger snapshot")
    if ledger_snapshot["path"] != str(path.resolve()) or not isinstance(ledger_snapshot["row"], dict):
        fail("bundle prompt is bound to a different ledger")
    for field in ("status", "pr_origin", "review_rounds", "ns_streak", "repair_count", "repair_decision"):
        if ledger_snapshot["row"].get(field) != row[field]:
            fail(f"bundle prompt is stale: ledger field {field} changed after it was prepared")
    if payload.get("permitted") != permitted_record(row):
        fail("bundle prompt's permitted decisions no longer match the ledger row")
    expected_definitions = {name: DECISIONS[name] for name in permitted_for(row)}
    if payload.get("decision_definitions") != expected_definitions:
        fail("bundle prompt's decision definitions do not match its permitted decisions")
    if not isinstance(payload.get("rounds"), list) or not isinstance(manifest["rounds"], list):
        fail("bundle prompt or manifest has an invalid rounds list")
    try:
        round_summary = [{"round": item["round"], "launch_attempt": item["launch_attempt"],
                          "review_head_sha": item["review_head_sha"]} for item in payload["rounds"]]
    except (KeyError, TypeError):
        fail("bundle prompt has a malformed round record")
    if manifest["rounds"] != round_summary:
        fail("bundle manifest's round summary does not match the prompt payload")
    record_text = read_utf8(record, "reassessment decision record")
    first = next((line.strip() for line in record_text.splitlines() if line.strip()), "")
    if first != marker:
        fail(f"decision record {record} is not bound to this bundle; first nonblank line must be `{marker}`")


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
    if row["repair_decision"] != "-":
        fail(f"pr {pr} already has reassessment decision {row['repair_decision']!r}; one cap accepts exactly "
             f"one decision")

    # THE DECISION RECORD MUST EXIST BEFORE IT IS RECORDED. A decision whose reasoning is only in a dead
    # agent's context is a decision nobody can audit — and every heartbeat is a fresh agent instance.
    record = Path(args.record)
    if not record.exists() or not read_utf8(record, "reassessment decision record", allow_empty=True).strip():
        fail(f"--record {record} does not exist or is empty. Write the reassessment's reasoning — the "
             f"round-by-round history it saw, the decision, and WHY — before recording the decision. A "
             f"decision with no record on disk cannot be audited by the next heartbeat, which remembers nothing.")

    validate_decision_bundle(path, header, row, pr, record, Path(args.bundle_manifest))

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
    return capture_cli(main, argv)


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
    mod = load_module_from_path("repair_pass_test", SIBLING, register=True)
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

    b = sub.add_parser("bundle", help="build the complete deterministic reassessment prompt and manifest")
    b.add_argument("--pr", required=True, help="PR number (row key)")
    b.add_argument("--run-dir", required=True, help="this campaign run's artifact directory")
    b.add_argument("--worktree", required=True, help="the PR-head worktree recorded in the ledger")
    b.add_argument("--output", required=True, help="new prompt path; a .manifest.json sidecar is also written")

    d = sub.add_parser("decide", help="record the reassessment pass's ONE decision")
    d.add_argument("--pr", required=True, help="PR number (row key)")
    d.add_argument("--decision", required=True, choices=tuple(DECISIONS),
                   help="; ".join(f"{k}: {v.split('.')[0]}" for k, v in DECISIONS.items()))
    d.add_argument("--record", required=True,
                   help="path to the decision record — the history the pass saw, the decision, and why. "
                        "Refused if it does not exist or is empty")
    d.add_argument("--bundle-manifest", required=True,
                   help="manifest emitted by `bundle`; the record's first nonblank line must bind its hash")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def dispatch(args) -> int:
    if args.cmd == "self-test":
        return self_test()
    if args.file is None:
        build_parser().error("the following arguments are required: --file")
    path = Path(args.file)
    return {"permitted": cmd_permitted, "bundle": cmd_bundle, "decide": cmd_decide}[args.cmd](path, args)


def main(argv: "list[str] | None" = None) -> int:
    return dispatch(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
