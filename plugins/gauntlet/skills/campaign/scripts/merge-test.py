#!/usr/bin/env python3
"""Mocked fixtures for `merge.py`; no fixture contacts GitHub or merges a real PR."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "merge.py"


def _load_owner():
    mod = load_module_from_path("merge_runner_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load {OWNER}")
    return mod


def _load_reconcile():
    # Loaded by path (never a static import — reconcile.py is outside the pyright type-clean set, and
    # `merge.py` already loads its siblings this way). The helper raises rather than returning None, so the
    # module binding is non-optional at every use.
    path = OWNER.parent / "reconcile.py"
    mod = load_module_from_path("merge_test_reconcile", path)
    if mod is None:
        raise RuntimeError(f"cannot load {path}")
    return mod


M = _load_owner()
L = M.L
# The reconcile detector. The routing-decision fixture drives BOTH tools: the fact reconcile emits, and the
# `merge.py run` finalizer that fact routes to.
RECON = _load_reconcile()

SHA = "a" * 40


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


class Fake:
    def __init__(self, root: Path, *, worktree_owned="yes", branch_owned="yes",
                 state="OPEN", ci="green", reviews="2", status="in_review",
                 base_checked=True, fail_once: "str | None" = None,
                 live_head: str = SHA, reject_method: "str | None" = None):
        self.root = root
        self.branch = "feat-pr"
        self.base = "main"
        self.worktree = root / ".worktrees" / self.branch
        self.worktree_owned = worktree_owned
        self.branch_owned = branch_owned
        self.state = state
        self.ci = ci
        self.reviews = reviews
        self.status = status
        self.base_checked = base_checked
        self.worktree_present = True
        self.branch_present = True
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_once = fail_once
        self.failed: set[str] = set()
        self.view_error_once = fail_once == "confirm-view"
        self.merged_calls = 0
        # The live tip GitHub would merge, and a merge method the repo rejects — the two knobs that
        # model, respectively, a head-race (--match-head-commit must refuse) and a disabled method.
        self.live_head = live_head
        self.reject_method = reject_method

    def ledger(self, path: Path, *, worktree: "Path | None" = None, branch: "str | None" = None,
               run_id="g1") -> None:
        header = dict(L.HEADER_DEFAULTS, run_id=run_id, base_branch=self.base)
        row = dict(L.ROW_DEFAULTS)
        row.update(pr="9", id="pr9", branch=branch or self.branch,
                   worktree=str(worktree or self.worktree), worktree_owned=self.worktree_owned,
                   branch_owned=self.branch_owned, head_sha=SHA, reviews_ok=self.reviews,
                   ci=self.ci, tier="HIGH", status=self.status)
        L.dump(path, header, [row])

    def view(self) -> dict:
        return {
            "state": self.state,
            "headRefOid": SHA,
            "headRefName": self.branch,
            "baseRefName": self.base,
            "labels": [{"name": "gauntlet-run-g1"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
        }

    def _fail(self, phase: str) -> bool:
        if self.fail_once == phase and phase not in self.failed:
            self.failed.add(phase)
            return True
        return False

    def _worktrees(self) -> str:
        entries = []
        if self.base_checked:
            entries.append(f"worktree {self.root}\0HEAD {'b' * 40}\0branch refs/heads/{self.base}\0\0")
        if self.worktree_present:
            entries.append(
                f"worktree {self.worktree}\0HEAD {SHA}\0branch refs/heads/{self.branch}\0\0")
        return "".join(entries)

    def run(self, argv: list[str], *, cwd: "str | None" = None) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), cwd))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        bad = lambda why: subprocess.CompletedProcess(argv, 1, "", why)

        if argv[:5] == ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"]:
            return ok(f"{self.root}\n")
        if "check-ref-format" in argv:
            return ok()
        if argv[:3] == ["gh", "pr", "view"]:
            if self.view_error_once and self.merged_calls:
                self.view_error_once = False
                return bad("temporary view failure")
            return ok(json.dumps(self.view()))
        if argv[:3] == ["gh", "pr", "merge"]:
            self.merged_calls += 1
            if "--match-head-commit" in argv:
                pinned = argv[argv.index("--match-head-commit") + 1]
                if pinned != self.live_head:
                    return bad("Head branch was modified. Review and try the merge again.")
            if self.reject_method is not None and f"--{self.reject_method}" in argv:
                return bad(f"{self.reject_method.capitalize()} merges are not allowed on this repository")
            if self._fail("merge-before"):
                return bad("merge rejected")
            self.state = "MERGED"
            if self._fail("merge-after"):
                return bad("connection lost after acceptance")
            return ok()
        if argv[:4] == ["git", "-C", str(self.worktree), "fetch"]:
            return bad("ancestry fetch failed") if self._fail("entry-fetch") else ok()
        if argv[:5] == ["git", "-C", str(self.worktree), "merge-base", "--is-ancestor"]:
            return bad("stale") if self._fail("stale-base") else ok()
        if len(argv) >= 6 and argv[:4] == ["git", "-C", str(self.root), "fetch"] and ":refs/remotes/" in argv[-1]:
            return bad("tracking fetch failed") if self._fail("sync-tracking") else ok()
        if argv[:6] == ["git", "-C", str(self.root), "worktree", "list", "--porcelain"]:
            return ok(self._worktrees())
        if len(argv) >= 6 and argv[:3] == ["git", "-C", str(self.root)] and argv[3:5] == ["fetch", "origin"]:
            return bad("base ref fetch failed") if self._fail("sync-base") else ok()
        if len(argv) >= 6 and argv[:3] == ["git", "-C", str(self.root)] and argv[3:5] == ["merge", "--ff-only"]:
            return bad("base ff failed") if self._fail("sync-base") else ok()
        if argv[:4] == ["git", "-C", str(self.worktree), "status"]:
            return ok("dirty\n") if self._fail("dirty") else ok()
        if argv[:5] == ["git", "-C", str(self.root), "worktree", "remove"]:
            if self._fail("worktree-remove"):
                return bad("remove failed")
            self.worktree_present = False
            return ok()
        if len(argv) >= 7 and argv[:4] == ["git", "-C", str(self.root), "show-ref"]:
            return ok() if self.branch_present else bad("absent")
        if len(argv) >= 5 and argv[:4] == ["git", "-C", str(self.root), "rev-parse"]:
            return ok(f"{SHA}\n")
        if len(argv) >= 7 and argv[:4] == ["git", "-C", str(self.root), "branch"]:
            if self._fail("branch-remove"):
                return bad("branch delete failed")
            self.branch_present = False
            return ok()
        return bad(f"unexpected command: {argv!r}")


def scenario(**kwargs):
    td = tempfile.TemporaryDirectory()
    root = Path(td.name).resolve()
    fake = Fake(root, **kwargs)
    ledger = root / "state.jsonl"
    fake.ledger(ledger)
    real = getattr(M, "_run")
    setattr(M, "_run", fake.run)
    return td, root, fake, ledger, real


def invoke(fake: Fake, ledger: Path, root: Path,
           merge_method: str = "squash") -> tuple[int, "dict | None", str]:
    try:
        result = M.execute(ledger, "9", root, "o/r", merge_method=merge_method)
        return 0, result, ""
    except M.Refusal as exc:
        return 1, None, str(exc)


def finish(td, real) -> None:
    setattr(M, "_run", real)
    td.cleanup()


def status(ledger: Path) -> str:
    row = L.find_row(L.load(ledger)[1], "9")
    if row is None:
        raise M.SelfTestFailure("fixture ledger lost PR 9")
    return row["status"]


def t_happy_owned_cleanup_and_command():
    td, root, f, led, real = scenario()
    try:
        code, result, err = invoke(f, led, root)
        check(code == 0, err)
        check(status(led) == "merged", "terminal ledger state must land last")
        check(not f.worktree_present and not f.branch_present, "both owned resources must be removed")
        merge = [argv for argv, _ in f.calls if argv[:3] == ["gh", "pr", "merge"]]
        check(merge == [["gh", "pr", "merge", "9", "--repo", "o/r", "--squash",
                         "--match-head-commit", SHA]],
              f"wrong merge argv: {merge}")
        check(all("--delete-branch" not in argv for argv, _ in f.calls),
              "campaign must never request remote branch deletion")
        check(result is not None and
              result["cleanup"] == {"worktree": "removed", "branch": "removed"},
              f"cleanup result is incomplete: {result}")
    finally:
        finish(td, real)


def t_merge_landed_ledger_live_resumes():
    td, root, f, led, real = scenario(state="MERGED")
    try:
        code, _result, err = invoke(f, led, root)
        check(code == 0, err)
        check(f.merged_calls == 0, "a durable MERGED state must skip the merge command")
        check(status(led) == "merged", "resume must finish the live ledger row")
    finally:
        finish(td, real)


def t_reused_resources_are_left():
    for wt_owned, br_owned in (("no", "no"), ("yes", "no"), ("no", "yes"), ("yes", "yes")):
        td, root, f, led, real = scenario(worktree_owned=wt_owned, branch_owned=br_owned)
        try:
            code, _result, err = invoke(f, led, root)
            if (wt_owned, br_owned) == ("no", "yes"):
                check(code != 0 and "not an adoption-produced" in err,
                      f"{wt_owned}/{br_owned}: impossible ownership state was accepted")
                continue
            check(code == 0, f"{wt_owned}/{br_owned}: {err}")
            check(f.worktree_present == (wt_owned == "no"),
                  f"{wt_owned}/{br_owned}: wrong worktree cleanup")
            check(f.branch_present == (br_owned == "no"),
                  f"{wt_owned}/{br_owned}: wrong branch cleanup")
        finally:
            finish(td, real)


def t_root_and_foreign_targets_refused():
    td, root, f, led, real = scenario()
    try:
        f.ledger(led, worktree=root)
        code, _, err = invoke(f, led, root)
        check(code != 0 and "repository-derived campaign path" in err,
              f"owned root target was not refused: {err}")
        check(f.merged_calls == 0, "ownership refusal must happen before merge")
        f.ledger(led, branch="other")
        code, _, err = invoke(f, led, root)
        check(code != 0 and "live head branch" in err, f"foreign branch was not refused: {err}")
    finally:
        finish(td, real)


def t_gate_refusals():
    for kwargs, needle in (
        ({"ci": "red"}, "ci is red"),
        ({"ci": "pending"}, "ci is pending"),
        ({"reviews": "1"}, "1 of 2 approvals"),
        ({"status": "awaiting-user"}, "held"),
        ({"fail_once": "stale-base"}, "base moved ahead"),
    ):
        td, root, f, led, real = scenario(**kwargs)
        try:
            code, _, err = invoke(f, led, root)
            check(code != 0 and needle in err, f"{kwargs} did not fail closed: {err}")
            check(f.merged_calls == 0, f"{kwargs} reached merge")
        finally:
            finish(td, real)


def t_stale_head_and_malformed_ownership_refused():
    td, root, f, led, real = scenario()
    original_view = f.view
    try:
        def moved():
            v = original_view()
            v["headRefOid"] = "c" * 40
            return v
        f.view = moved
        code, _, err = invoke(f, led, root)
        check(code != 0 and "differs from ledger head" in err, f"stale SHA was accepted: {err}")
        check(f.merged_calls == 0, "stale SHA reached merge")

        f.view = original_view
        f.worktree_owned = "-"
        f.ledger(led)
        code, _, err = invoke(f, led, root)
        check(code != 0 and "worktree_owned" in err, f"malformed ownership was accepted: {err}")
    finally:
        finish(td, real)


def t_owner_label_and_uncertain_view_refused():
    td, root, f, led, real = scenario()
    original_view = f.view
    try:
        def foreign():
            v = original_view()
            v["labels"] = [{"name": "gauntlet-run-other"}]
            return v
        f.view = foreign
        code, _, err = invoke(f, led, root)
        check(code != 0 and "owner label" in err, f"another run was not refused: {err}")

        def malformed():
            v = original_view()
            del v["mergeStateStatus"]
            return v
        f.view = malformed
        code, _, err = invoke(f, led, root)
        check(code != 0 and "mergeStateStatus" in err, f"malformed GitHub state was not refused: {err}")
    finally:
        finish(td, real)


def t_base_checked_out_and_absent():
    for checked, expected in ((True, ["merge", "--ff-only"]), (False, ["fetch", "origin", "main:main"])):
        td, root, f, led, real = scenario(base_checked=checked)
        try:
            code, _, err = invoke(f, led, root)
            check(code == 0, err)
            suffixes = [argv[3:] for argv, _ in f.calls if argv[:3] == ["git", "-C", str(root)]]
            check(any(parts[:len(expected)] == expected for parts in suffixes),
                  f"base checked={checked} did not use {expected}: {suffixes}")
        finally:
            finish(td, real)


def t_merge_and_confirmation_failures_resume():
    for phase in ("merge-before", "confirm-view"):
        td, root, f, led, real = scenario(fail_once=phase)
        try:
            first, _, _ = invoke(f, led, root)
            check(first != 0 and status(led) == "in_review", f"{phase} must leave ledger live")
            if phase == "confirm-view":
                check(f.worktree_present and f.branch_present,
                      "confirmation failure cleaned resources before MERGED was observed")
            second, _, err = invoke(f, led, root)
            check(second == 0, f"{phase} did not resume: {err}")
            expected_merges = 2 if phase == "merge-before" else 1
            check(f.merged_calls == expected_merges,
                  f"{phase}: expected {expected_merges} merge command(s), got {f.merged_calls}")
        finally:
            finish(td, real)


def t_postmerge_phase_failures_resume():
    for phase in ("sync-tracking", "sync-base", "worktree-remove", "branch-remove"):
        td, root, f, led, real = scenario(fail_once=phase)
        try:
            first, _, _ = invoke(f, led, root)
            check(first != 0 and f.state == "MERGED" and status(led) == "in_review",
                  f"{phase}: partial state must be durable and ledger live")
            second, _, err = invoke(f, led, root)
            check(second == 0 and status(led) == "merged", f"{phase} did not resume: {err}")
            check(f.merged_calls == 1, f"{phase}: resume repeated merge")
        finally:
            finish(td, real)


def t_merge_transport_failure_after_acceptance_continues():
    td, root, f, led, real = scenario(fail_once="merge-after")
    try:
        code, _, err = invoke(f, led, root)
        check(code == 0, f"confirmed MERGED must outrank merge transport failure: {err}")
        check(status(led) == "merged", "confirmed merge did not complete")
    finally:
        finish(td, real)


def t_terminal_write_failure_resumes_after_cleanup():
    td, root, f, led, real = scenario()
    real_save = L.save
    fired = False

    def fail_once(path, header, rows, *, activity):
        nonlocal fired
        if not fired:
            fired = True
            raise OSError("simulated atomic replace failure")
        return real_save(path, header, rows, activity=activity)

    L.save = fail_once
    try:
        first, _, err = invoke(f, led, root)
        check(first != 0 and "terminal ledger write failed" in err, f"write failure was not controlled: {err}")
        check(f.state == "MERGED" and not f.worktree_present and not f.branch_present,
              "write failure did not leave safely re-derived completed phases")
        check(status(led) == "in_review", "failed atomic terminal write changed the ledger")
        second, _, err = invoke(f, led, root)
        check(second == 0 and status(led) == "merged", f"terminal write did not resume: {err}")
        check(f.merged_calls == 1, "terminal-write resume repeated merge")
    finally:
        L.save = real_save
        finish(td, real)


def t_repeat_after_terminal_is_noop():
    td, root, f, led, real = scenario()
    try:
        first, _, err = invoke(f, led, root)
        check(first == 0, err)
        before = len(f.calls)
        second, result, err = invoke(f, led, root)
        check(second == 0 and result is not None and result["status"] == "already-complete", err)
        new = [argv for argv, _ in f.calls[before:]]
        check(not any(argv[:3] == ["gh", "pr", "merge"] for argv in new),
              "terminal rerun repeated merge")
        check(not any("worktree" in argv and "remove" in argv for argv in new),
              "terminal rerun repeated cleanup")
    finally:
        finish(td, real)


def t_head_race_between_view_and_merge_refuses_before_landing():
    # A push advances the live tip to a DIFFERENT SHA in the window between the pre-merge view (which
    # still reports the reviewed head) and the merge call. --match-head-commit pins the reviewed SHA, so
    # GitHub refuses; the unreviewed head is never squashed and the row stays live for a clean re-gate.
    td, root, f, led, real = scenario(live_head="c" * 40)
    try:
        code, _result, _err = invoke(f, led, root)
        check(code != 0, "head-race merge was not refused")
        merge = [argv for argv, _ in f.calls if argv[:3] == ["gh", "pr", "merge"]]
        check(merge == [["gh", "pr", "merge", "9", "--repo", "o/r", "--squash",
                         "--match-head-commit", SHA]],
              f"merge argv did not pin the reviewed head: {merge}")
        check(f.state == "OPEN", "the unreviewed head was merged despite the pin")
        check(status(led) == "in_review", "a refused head-race must leave the row live to re-gate")
        check(f.worktree_present and f.branch_present,
              "a refused merge must not clean owned resources")
    finally:
        finish(td, real)


def t_merge_method_input_validated_and_applied():
    # The merge method is an explicit, validated runner input defaulting to squash. A squash-disabled
    # repo fails LOUDLY on the default; the documented recourse is to pass the repo's prevailing method.
    td, root, f, led, real = scenario(reject_method="squash")
    try:
        code, _result, err = invoke(f, led, root)  # default --squash on a squash-disabled repo
        check(code != 0 and "not allowed" in err, f"squash-disabled repo did not fail loudly: {err}")
        check(f.state == "OPEN", "a rejected squash must not merge")
        check(status(led) == "in_review", "a rejected merge must leave the row live")
    finally:
        finish(td, real)

    td, root, f, led, real = scenario(reject_method="squash")
    try:
        code, _result, err = invoke(f, led, root, merge_method="merge")  # prevailing method recourse
        check(code == 0, f"prevailing merge method was not honored: {err}")
        merge = [argv for argv, _ in f.calls if argv[:3] == ["gh", "pr", "merge"]]
        check(merge == [["gh", "pr", "merge", "9", "--repo", "o/r", "--merge",
                         "--match-head-commit", SHA]],
              f"merge argv did not carry the chosen method: {merge}")
        check(all("--delete-branch" not in argv for argv, _ in f.calls),
              "a non-squash method must still never request remote branch deletion")
        check(status(led) == "merged", "the prevailing-method merge did not complete")
    finally:
        finish(td, real)

    td, root, f, led, real = scenario()
    try:
        code, _result, err = invoke(f, led, root, merge_method="octopus")
        check(code != 0 and "merge method" in err, f"an invalid merge method was accepted: {err}")
        check(f.merged_calls == 0, "an invalid merge method reached the merge call")
    finally:
        finish(td, real)


def t_absent_snapshot_merged_row_resumes_via_run():
    # Heartbeat routing (loop-control.md Step 4): an absent-from-snapshot row whose ledger status is not
    # yet terminal is routed through `merge.py run`. GitHub already MERGED it, but the process died before
    # base-sync/cleanup/terminal write, so those later phases are still pending. run resumes them here —
    # no second merge, cleanup completes, terminal row lands — which is what the doc delegates to it.
    td, root, f, led, real = scenario(state="MERGED")
    try:
        check(f.worktree_present and f.branch_present, "precondition: later phases still pending")
        code, result, err = invoke(f, led, root)
        check(code == 0, err)
        check(f.merged_calls == 0, "a MERGED PR must not be re-merged when the heartbeat resumes it")
        check(status(led) == "merged", "resume must finalize the terminal ledger row")
        check(not f.worktree_present and not f.branch_present,
              "resume must complete the pending base-sync/cleanup phases")
        check(result is not None and result["status"] == "merged", f"unexpected result: {result}")
    finally:
        finish(td, real)


def t_absent_snapshot_closed_row_terminates():
    # Heartbeat routing (loop-control.md Step 4), the CLOSED side of the absent-row finalizer: an
    # absent-from-snapshot NON-TERMINAL row whose live PR is CLOSED WITHOUT merging (a human closed it, or the
    # driver died after `gh pr close`). `merge.py run` performs the terminal close-out — records `aborted`,
    # runs NO merge, and cleans NOTHING: the branch content never reached <base>, so removing an owned
    # worktree/branch would destroy unmerged work.
    td, root, f, led, real = scenario(state="CLOSED")
    try:
        code, result, err = invoke(f, led, root)
        check(code == 0, err)
        check(f.merged_calls == 0, "a CLOSED-without-merge PR must never be merged")
        check(status(led) == "aborted", f"a closed-without-merge row must terminate as aborted, got {status(led)!r}")
        check(result is not None and result["status"] == "closed-unmerged" and result["cleanup"] == {},
              f"the close-out must report closed-unmerged with no cleanup: {result}")
        check(f.worktree_present and f.branch_present,
              "the close-out must NOT delete owned resources holding unmerged work")
        check(not any(argv[:3] == ["gh", "pr", "merge"] for argv, _ in f.calls),
              "the close-out issued a merge command")
        check(not any("worktree" in argv and "remove" in argv for argv, _ in f.calls),
              "the close-out removed a worktree")
    finally:
        finish(td, real)


def t_absent_routing_decision():
    # The ROUTING DECISION itself (loop-control.md Step 1 -> Step 4), exercised end to end rather than by a
    # bare execute() call. reconcile.py observes the absent fact; `merge.py run` is the single finalizer BOTH
    # sides of the routing lead to. reconcile emits `absent_from_snapshot: True` for a NON-TERMINAL row missing
    # from the snapshot, and that one fact routes to `merge.py run`, which distinguishes MERGED (resume) from
    # CLOSED-without-merge (terminate). Both branches are driven from the same reconcile fact here.
    for live_state, want_status, want_result in (
        ("MERGED", "merged", "merged"),
        ("CLOSED", "aborted", "closed-unmerged"),
    ):
        td, root, f, led, real = scenario(state=live_state)
        try:
            # Routing INPUT: reconcile sees PR 9 (in_review) absent from an empty snapshot and reports the fact.
            prs = root / "prs.json"
            prs.write_text("[]", encoding="utf-8")
            facts = RECON.detect(led, prs, "g1")
            row_fact = facts["rows"]["9"]
            check(row_fact.get("absent_from_snapshot") is True,
                  f"{live_state}: reconcile did not report the absent fact: {row_fact}")
            check("terminal" not in row_fact,
                  f"{live_state}: a non-terminal absent row must not be pre-classified terminal: {row_fact}")
            # Routing OUTPUT: the fact routes to `merge.py run`, which finalizes the correct side.
            code, result, err = invoke(f, led, root)
            check(code == 0, f"{live_state}: {err}")
            check(status(led) == want_status,
                  f"{live_state}: absent row finalized to {status(led)!r}, expected {want_status!r}")
            check(result is not None and result["status"] == want_result,
                  f"{live_state}: run reported {result}, expected result status {want_result!r}")
            check(f.merged_calls == 0, f"{live_state}: an already-terminal live PR must not be re-merged")
        finally:
            finish(td, real)


CASES = [
    ("happy-owned", "exact merge argv, owned cleanup, terminal write", t_happy_owned_cleanup_and_command),
    ("merged-live-row", "MERGED with live ledger resumes without another merge", t_merge_landed_ledger_live_resumes),
    ("ownership-matrix", "all worktree/branch ownership combinations clean only owned resources", t_reused_resources_are_left),
    ("root-foreign", "root cleanup and foreign branch are refused before merge", t_root_and_foreign_targets_refused),
    ("gate-refusals", "held, stale, red, pending, and short-tally rows never merge", t_gate_refusals),
    ("stale-malformed", "stale live SHA and malformed ownership fail closed", t_stale_head_and_malformed_ownership_refused),
    ("owner-and-view", "another run and uncertain GitHub state fail closed", t_owner_label_and_uncertain_view_refused),
    ("base-location", "checked-out and absent local base use their documented update paths", t_base_checked_out_and_absent),
    ("merge-resume", "merge and confirmation failures resume safely", t_merge_and_confirmation_failures_resume),
    ("postmerge-resume", "every post-merge phase resumes without another merge", t_postmerge_phase_failures_resume),
    ("merge-accepted", "MERGED confirmation outranks a lost merge response", t_merge_transport_failure_after_acceptance_continues),
    ("terminal-write-resume", "a failed terminal write resumes after already-completed cleanup", t_terminal_write_failure_resumes_after_cleanup),
    ("terminal-repeat", "repeated invocation after terminal state is a no-op", t_repeat_after_terminal_is_noop),
    ("head-race", "--match-head-commit refuses a tip that advanced before the merge landed", t_head_race_between_view_and_merge_refuses_before_landing),
    ("merge-method", "merge method is a validated input; squash-disabled repo has a prevailing-method recourse", t_merge_method_input_validated_and_applied),
    ("absent-resume", "an absent-but-unfinalized MERGED row resumes its remaining phases through run", t_absent_snapshot_merged_row_resumes_via_run),
    ("absent-closed", "an absent-but-unfinalized CLOSED-without-merge row terminates as aborted with no cleanup", t_absent_snapshot_closed_row_terminates),
    ("absent-routing", "the absent fact routes reconcile -> merge.py run, which finalizes MERGED and CLOSED sides", t_absent_routing_decision),
]
