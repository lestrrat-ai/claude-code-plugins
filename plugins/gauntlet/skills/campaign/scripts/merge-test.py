#!/usr/bin/env python3
"""Mocked fixtures for `merge.py`; no fixture contacts GitHub or merges a real PR."""

from __future__ import annotations

import json
import os
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
                 live_head: str = SHA, reject_method: "str | None" = None,
                 view_head: "str | None" = None, view_branch: "str | None" = None,
                 view_base: "str | None" = None, base: str = "main",
                 labels: "list[dict] | None" = None, local_base: str = "absent",
                 repo_identity: str = "o/r", mergeable: str = "MERGEABLE",
                 merge_state_status: str = "CLEAN", header_base: "str | None" = None,
                 base_ff_blocked: bool = False, ancestor_ok: bool = True,
                 plumb_fail: "str | None" = None,
                 staged_paths: "list[str] | None" = None,
                 unstaged_paths: "list[str] | None" = None,
                 untracked_paths: "list[str] | None" = None,
                 incoming_paths: "list[str] | None" = None,
                 unmerged_paths: "list[str] | None" = None):
        self.root = root
        self.branch = "feat-pr"
        # `base` is the row's RECORDED base — an explicit row `base_branch`, unless `header_base` is given, in
        # which case the row inherits it as a legacy `-` row. Either way `effective_base` resolves to `base`,
        # which is what every base door (validate, ancestry, sync) must target.
        self.base = base
        self.header_base = header_base
        self.worktree = root / ".worktrees" / self.branch
        self.worktree_owned = worktree_owned
        self.branch_owned = branch_owned
        self.state = state
        self.ci = ci
        self.reviews = reviews
        self.status = status
        self.base_checked = base_checked
        # State of the LOCAL base ref on the no-checked-out-base path (_sync_base else branch):
        #   "absent" — no local base ref; the non-forced fetch creates/advances it (default).
        #   "synced" — the local ref exists and is equal-or-AHEAD of origin (origin is its ancestor); the
        #              already-synced short-circuit must SKIP the fetch, which — modelling a local-ahead
        #              non-fast-forward — is set to fail if ever attempted.
        self.local_base = local_base
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
        # The live view's head/base/branch, each defaulting to the pinned ledger value. Overriding one
        # models a push that advanced the head, or a base/branch rename, BEFORE the reported live state —
        # the input the strict merge pins refuse but the ledger-only CLOSED close-out must tolerate.
        self.view_head = view_head or SHA
        self.view_branch = view_branch or self.branch
        self.view_base = view_base or self.base
        # The live PR's labels. Default carries THIS run's owner label; passing [] models a HALF-ADOPTION
        # whose PR never got `gh pr edit`-attached its label, and a foreign entry models another run's PR.
        self.labels = labels if labels is not None else [{"name": "gauntlet-run-g1"}]
        # The nameWithOwner `gh repo view` derives from the checkout. Defaults to the `--repo` invoke passes
        # ("o/r"), so existing fixtures pass the cross-repo guard; overriding it models a `--repo` that does
        # NOT name the checkout's own repository (the fail-closed the guard refuses before any live view).
        self.repo_identity = repo_identity
        # The two GitHub merge enums the live view reports. Default MERGEABLE/CLEAN yields merge-check's MERGE
        # verdict; overriding merge_state_status to "BLOCKED" yields its PROBE sentinel, which the runner must
        # resolve through the SAME base-ancestry probe the MERGE path runs (behind -> rebase, current -> park).
        self.mergeable = mergeable
        self.merge_state_status = merge_state_status
        # Post-merge base-sync diagnostic knobs, all modeled on checked[0] (the checkout holding the base,
        # which the fixtures set to root). `base_ff_blocked` forces the checked-out-base `merge --ff-only`
        # to fail as git does against uncommitted work. `ancestor_ok` is the `merge-base --is-ancestor HEAD
        # origin/<base>` probe result (False models a genuine divergence, which must keep the raw error).
        # `plumb_fail` names the ONE plumbing probe that fails (unmerged/staged/unstaged/untracked/incoming),
        # which must also keep the raw error. The path lists are what the plumbing reports; a non-empty
        # `unmerged_paths` models a conflicted (stage > 0) index, which must keep the raw error too.
        self.base_ff_blocked = base_ff_blocked
        self.ancestor_ok = ancestor_ok
        self.plumb_fail = plumb_fail
        self.staged_paths = staged_paths or []
        self.unstaged_paths = unstaged_paths or []
        self.untracked_paths = untracked_paths or []
        self.incoming_paths = incoming_paths or []
        self.unmerged_paths = unmerged_paths or []

    def ledger(self, path: Path, *, worktree: "Path | None" = None, branch: "str | None" = None,
               run_id="g1", worktree_field: "str | None" = None) -> None:
        # Default: legacy shape — header holds the base, the row inherits it as `-`. With `header_base` set,
        # the row owns an EXPLICIT `base_branch` (self.base) and the header carries `header_base` (e.g. "-"):
        # the mixed-base shape. `effective_base` resolves to self.base either way.
        header_base = self.header_base if self.header_base is not None else self.base
        row_base = self.base if self.header_base is not None else "-"
        header = dict(L.HEADER_DEFAULTS, run_id=run_id, base_branch=header_base)
        row = dict(L.ROW_DEFAULTS)
        # `worktree_field` writes the row's worktree column VERBATIM — passing the ROW_DEFAULTS "-" models a
        # HALF-ADOPTION (pr-adopt.py registered the row before it resolved the worktree). Otherwise the
        # column is the absolute worktree path, resolved like an adopted row.
        wt = worktree_field if worktree_field is not None else str(worktree or self.worktree)
        row.update(pr="9", id="pr9", branch=branch or self.branch,
                   worktree=wt, worktree_owned=self.worktree_owned,
                   branch_owned=self.branch_owned, head_sha=SHA, reviews_ok=self.reviews,
                   ci=self.ci, tier="HIGH", status=self.status, base_branch=row_base)
        L.dump(path, header, [row])

    def view(self) -> dict:
        return {
            "state": self.state,
            "headRefOid": self.view_head,
            "headRefName": self.view_branch,
            "baseRefName": self.view_base,
            "labels": self.labels,
            "mergeable": self.mergeable,
            "mergeStateStatus": self.merge_state_status,
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

    def run(self, argv: list[str], *, cwd: "str | None" = None,
            text: bool = True) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), cwd))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        bad = lambda why: subprocess.CompletedProcess(argv, 1, "", why)
        # The path plumbing is captured in BYTE mode (text=False), so its success stdout must be bytes; the
        # runner splits on the NUL byte before decoding. `okb`/`nulb` mirror `ok`/`nul` in bytes. A failing
        # probe returns `bad` (returncode 1) whose stdout the runner never reads, so it may stay str.
        okb = lambda out=b"": subprocess.CompletedProcess(argv, 0, out, b"")
        nulb = lambda paths: b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)

        if argv[:5] == ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"]:
            return ok(f"{self.root}\n")
        if "check-ref-format" in argv:
            return ok()
        if argv[:3] == ["gh", "repo", "view"]:
            return ok(f"{self.repo_identity}\n")
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
        # Post-merge base-sync diagnostic plumbing on checked[0] (== root): all read-only. The is-ancestor
        # HEAD probe carries origin/<base> as its LAST arg, distinct from the no-checkout probe below whose
        # last arg is refs/heads/<base>, so the two never alias.
        checkout = str(self.root)
        if (argv[:3] == ["git", "-C", checkout]
                and argv[3:6] == ["merge-base", "--is-ancestor", "HEAD"]):
            return ok() if self.ancestor_ok else bad("HEAD is not an ancestor of origin base")
        # The unmerged-index probe (`ls-files --unmerged`) comes BEFORE the untracked `ls-files --others`
        # branch, since both share the `ls-files` subcommand slot; keyed on `--unmerged`.
        if argv[:4] == ["git", "-C", checkout, "ls-files"] and "--unmerged" in argv:
            return bad("ls-files -u failed") if self.plumb_fail == "unmerged" else okb(nulb(self.unmerged_paths))
        if argv[:4] == ["git", "-C", checkout, "diff-index"]:
            return bad("diff-index failed") if self.plumb_fail == "staged" else okb(nulb(self.staged_paths))
        if argv[:4] == ["git", "-C", checkout, "diff-files"]:
            return bad("diff-files failed") if self.plumb_fail == "unstaged" else okb(nulb(self.unstaged_paths))
        if argv[:4] == ["git", "-C", checkout, "ls-files"]:
            return bad("ls-files failed") if self.plumb_fail == "untracked" else okb(nulb(self.untracked_paths))
        if argv[:4] == ["git", "-C", checkout, "diff-tree"]:
            return bad("diff-tree failed") if self.plumb_fail == "incoming" else okb(nulb(self.incoming_paths))
        base_ref = f"refs/heads/{self.base}"
        # The already-synced short-circuit's two probes on the no-checked-out-base path: does the local base
        # ref exist, and is origin/<base> an ancestor of it (local equal-or-ahead). "absent" fails the
        # existence probe (fall through to fetch); "synced" passes both (skip fetch).
        if (len(argv) >= 5 and argv[:4] == ["git", "-C", str(self.root), "show-ref"]
                and argv[-1] == base_ref):
            return ok() if self.local_base != "absent" else bad("no such local base ref")
        if (argv[:5] == ["git", "-C", str(self.root), "merge-base", "--is-ancestor"]
                and argv[-1] == base_ref):
            return ok() if self.local_base == "synced" else bad("origin not an ancestor of local base")
        if len(argv) >= 6 and argv[:4] == ["git", "-C", str(self.root), "fetch"] and ":refs/remotes/" in argv[-1]:
            return bad("tracking fetch failed") if self._fail("sync-tracking") else ok()
        if argv[:6] == ["git", "-C", str(self.root), "worktree", "list", "--porcelain"]:
            return ok(self._worktrees())
        if len(argv) >= 6 and argv[:3] == ["git", "-C", str(self.root)] and argv[3:5] == ["fetch", "origin"]:
            # A local-AHEAD base ref makes this non-forced local-ref fetch a non-fast-forward that real git
            # rejects (exit 1) — the wedge the already-synced short-circuit exists to avoid. Model that, plus
            # the generic sync-base fail knob. With the fix in place this fetch is never reached when synced.
            if self.local_base == "synced" or self._fail("sync-base"):
                return bad("base ref fetch failed (non-fast-forward)")
            return ok()
        if len(argv) >= 6 and argv[:3] == ["git", "-C", str(self.root)] and argv[3:5] == ["merge", "--ff-only"]:
            if self.base_ff_blocked or self._fail("sync-base"):
                return bad("Your local changes to the following files would be overwritten by merge")
            return ok()
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


def t_blocked_probe_rebases_when_behind_parks_when_current():
    # merge-check.decide returns its PROBE sentinel (not MERGE) for a BLOCKED PR, because BLOCKED alone
    # cannot tell a genuine human/ruleset block from one that is merely BEHIND its base. The runner must
    # resolve PROBE through the SAME base-ancestry probe the MERGE path runs, mirroring merge-check.check():
    #   * BLOCKED + behind base  -> the rebase refusal (routes to a rebase, NOT a park).
    #   * BLOCKED + current base  -> the genuine park refusal (up-to-date human/ruleset block).
    # RED before the fix: _require_ready refused every non-MERGE verdict with the park reason immediately,
    # so a behind-BLOCKED PR was parked instead of rebased and the ancestry probe never ran.

    # BLOCKED + behind base: fail_once="stale-base" makes the worktree ancestry probe report behind (exit 1).
    td, root, f, led, real = scenario(merge_state_status="BLOCKED", fail_once="stale-base")
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0 and "base moved ahead" in err,
              f"BLOCKED-behind must route to a rebase, not a park: code={code} err={err!r}")
        check(f.merged_calls == 0, "a BLOCKED PROBE must never reach the merge command")
        # The probe must actually have run — the rebase verdict comes from the ancestry check, not decide.
        check(any(argv[:5] == ["git", "-C", str(f.worktree), "merge-base", "--is-ancestor"]
                  for argv, _ in f.calls),
              "the runner did not run the base-ancestry probe for a BLOCKED PROBE")
    finally:
        finish(td, real)

    # BLOCKED + current base: the ancestry probe passes, so it is a genuine block and parks.
    td, root, f, led, real = scenario(merge_state_status="BLOCKED")
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0 and "BLOCKED" in err,
              f"BLOCKED-current must park as a genuine block: code={code} err={err!r}")
        check("base moved ahead" not in err, f"an up-to-date BLOCKED PR must not be told to rebase: {err}")
        check(f.merged_calls == 0, "a parked BLOCKED PROBE must never reach the merge command")
        check(status(led) == "in_review", "a parked BLOCKED row must stay live, not terminate")
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
    # The absent-base path fetches with a FULLY-QUALIFIED refspec (`refs/heads/<base>:refs/heads/<base>`, no
    # leading `+`), never the bare `<base>:<base>` that git would option-parse from a dash-leading base name.
    for checked, expected in ((True, ["merge", "--ff-only"]),
                              (False, ["fetch", "origin", "refs/heads/main:refs/heads/main"])):
        td, root, f, led, real = scenario(base_checked=checked)
        try:
            code, _, err = invoke(f, led, root)
            check(code == 0, err)
            suffixes = [argv[3:] for argv, _ in f.calls if argv[:3] == ["git", "-C", str(root)]]
            check(any(parts[:len(expected)] == expected for parts in suffixes),
                  f"base checked={checked} did not use {expected}: {suffixes}")
        finally:
            finish(td, real)


def t_dash_leading_base_is_never_option_parseable():
    # A network-supplied base name (gh baseRefName) that begins with a dash is LEGAL git but would be
    # option-parsed by `git fetch` if passed as a bare argv element. Both fetch sites must qualify it into
    # a `refs/heads/...` refspec so a hostile base can never inject an option or a command path.

    # F1 (_base_is_current, the merge-check ancestry refresh, on the worktree): tracking-ref form.
    base = "--upload-pack=/bin/false"
    td, root, f, led, real = scenario(base=base)
    try:
        code, _, err = invoke(f, led, root)
        check(code == 0, f"dash-leading base broke the ancestry refresh: {err}")
        worktree = str(f.worktree)
        tracking = f"refs/heads/{base}:refs/remotes/origin/{base}"
        wt_fetches = [argv for argv, _ in f.calls
                      if argv[:5] == ["git", "-C", worktree, "fetch", "origin"]]
        check(wt_fetches == [["git", "-C", worktree, "fetch", "origin", tracking]],
              f"ancestry refresh did not use the safe tracking refspec: {wt_fetches}")
        check(not any(argv[-1] == base for argv, _ in f.calls if "fetch" in argv),
              "a fetch passed the dash-leading base as a bare option-parseable positional")
    finally:
        finish(td, real)

    # F2 (_sync_base, the no-checked-out-base fast-forward): local-ref form, no leading `+`. The sync must
    # SUCCEED so cleanup and the terminal write run — the bare `--prune:--prune` form wedged them forever.
    base = "--prune"
    td, root, f, led, real = scenario(base=base, base_checked=False)
    try:
        code, _, err = invoke(f, led, root)
        check(code == 0, f"dash-leading base wedged the base-sync: {err}")
        check(status(led) == "merged", "base-sync failure blocked the terminal write")
        local = f"refs/heads/{base}:refs/heads/{base}"
        root_fetches = [argv for argv, _ in f.calls
                        if argv[:5] == ["git", "-C", str(root), "fetch", "origin"]]
        check(["git", "-C", str(root), "fetch", "origin", local] in root_fetches,
              f"absent-base sync did not use the safe local refspec: {root_fetches}")
        check(not any(token == f"{base}:{base}" for argv, _ in f.calls for token in argv),
              "the option-parseable bare `<base>:<base>` refspec is still assembled")
    finally:
        finish(td, real)


def t_live_base_retarget_refuses_with_shared_reason():
    # The live PR base no longer matches the row's recorded base: an unsupported mid-run retarget. The merge
    # runner must REFUSE (never merge onto the new base) with the SAME machine-blocker wording every base door
    # records — pr-adopt.py owns it, reached here via merge-check (M.MC.PA).
    td, root, f, led, real = scenario(view_base="v9")
    try:
        code, _result, err = invoke(f, led, root)
        expected = M.MC.PA.BASE_CHANGE_PARK_REASON.format(recorded="main", live="v9")
        check(code != 0, f"a live base retarget must refuse, not merge: {err}")
        check(expected in err, f"the refusal must use the shared base-change reason, got: {err!r}")
    finally:
        finish(td, real)


def t_explicit_row_base_drives_every_base_door():
    # A MIXED-BASE row: header base is `-`, the row owns an explicit base_branch ("v3"). Every base door
    # (validate, ancestry, post-merge sync) must target the ROW's base, not the header. A clean external MERGE
    # exercises validate + sync end to end; landing at merged proves the row base drove them.
    td, root, f, led, real = scenario(state="MERGED", base="v3", header_base="-")
    try:
        code, _result, err = invoke(f, led, root)
        check(code == 0, f"an explicit-row-base merge must finalize through the row base: {err}")
        check(status(led) == "merged", "the row must reach terminal merged")
        # the post-merge sync fetched origin/v3 (the ROW's base), never the header's `-`.
        synced = [argv for argv, _ in f.calls
                  if argv[:5] == ["git", "-C", str(root), "fetch", "origin"] and "v3" in argv[-1]]
        check(bool(synced), f"post-merge sync must target the row base v3: {[a for a, _ in f.calls]!r}")
    finally:
        finish(td, real)


def t_local_ahead_base_skips_fetch_and_finalizes():
    # A local base ref that is AHEAD of origin (unpushed commits; local already CONTAINS origin/<base>) is
    # already synchronized. The non-forced local-ref fetch would reject that as a non-fast-forward (exit 1)
    # and wedge post-merge finalization, so base-sync must SKIP the fetch and still reach cleanup + the
    # terminal write. Row is still in_review; the live PR is externally MERGED — the resume path that owes
    # the base-sync. RED before the fix: the fetch is attempted, fails non-ff, and blocks the terminal write.
    td, root, f, led, real = scenario(state="MERGED", base_checked=False, local_base="synced")
    try:
        code, _result, err = invoke(f, led, root)
        check(code == 0, err)
        check(status(led) == "merged", "already-synced base must not block the terminal write")
        check(not f.worktree_present and not f.branch_present,
              "finalization must remove both owned resources after the skipped fetch")
        local = f"refs/heads/{f.base}:refs/heads/{f.base}"
        check(not any(argv[:5] == ["git", "-C", str(root), "fetch", "origin"] and argv[-1] == local
                      for argv, _ in f.calls),
              "a local-ahead base must NOT trigger the non-fast-forward local-ref fetch")
        probe = ["git", "-C", str(root), "merge-base", "--is-ancestor", f"origin/{f.base}",
                 f"refs/heads/{f.base}"]
        check(any(argv == probe for argv, _ in f.calls),
              "the already-synced short-circuit must probe origin-is-ancestor-of-local")
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


def t_repeat_after_closed_terminal_is_noop():
    # Symmetric with t_repeat_after_terminal_is_noop, for the `aborted` terminal side. The close-out records
    # `aborted`; a SECOND run on that terminal row must be the SAME already-complete no-op the `merged` repeat
    # is — no merge, no cleanup, no ledger write, and NEVER destroying the unmerged work the abort preserved.
    td, root, f, led, real = scenario(state="CLOSED")
    try:
        first, _, err = invoke(f, led, root)
        check(first == 0 and status(led) == "aborted", f"close-out did not record aborted: {err}")
        before = len(f.calls)
        second, result, err = invoke(f, led, root)
        check(second == 0 and result is not None and result["status"] == "already-complete",
              f"aborted terminal rerun was not an already-complete no-op: {err}")
        check(status(led) == "aborted", "aborted terminal rerun changed the ledger")
        new = [argv for argv, _ in f.calls[before:]]
        check(not any(argv[:3] == ["gh", "pr", "merge"] for argv in new), "aborted terminal rerun issued a merge")
        check(not any("worktree" in argv and "remove" in argv for argv in new),
              "aborted terminal rerun cleaned resources")
        check(f.worktree_present and f.branch_present, "aborted terminal rerun destroyed unmerged work")
    finally:
        finish(td, real)

    # The repeat is ledger-only, so — exactly as the fresh close-out tolerates them
    # (t_closed_out_terminates_despite_moved_head_base_or_branch) — a head/base/branch that moved before the
    # close still yields the no-op, never a spurious live-ref refusal on a settled terminal row.
    for label, knob in (
        ("moved head", {"view_head": "b" * 40}),
        ("changed base", {"view_base": "release"}),
        ("changed branch", {"view_branch": "renamed"}),
    ):
        td, root, f, led, real = scenario(state="CLOSED", status="aborted", **knob)
        try:
            code, result, err = invoke(f, led, root)
            check(code == 0 and result is not None and result["status"] == "already-complete",
                  f"{label}: aborted+CLOSED repeat refused instead of no-op: {err}")
            check(status(led) == "aborted", f"{label}: the repeat changed the ledger")
            check(f.merged_calls == 0, f"{label}: the repeat issued a merge")
            check(f.worktree_present and f.branch_present, f"{label}: the repeat destroyed unmerged work")
        finally:
            finish(td, real)

    # Guardrail: an `aborted` row whose live PR is OPEN or MERGED is a CONTRADICTION, not a no-op — the PR is
    # no longer closed-without-merge. It must REFUSE naming the mismatch (not the generic "not in_review"),
    # mirroring the merged+non-MERGED guard.
    for live_state in ("OPEN", "MERGED"):
        td, root, f, led, real = scenario(state=live_state, status="aborted")
        try:
            code, _result, err = invoke(f, led, root)
            check(code != 0 and "aborted but GitHub state is" in err,
                  f"aborted+{live_state} must refuse as a contradiction, got code={code} err={err!r}")
            check(f.merged_calls == 0, f"aborted+{live_state} contradiction must not merge")
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


def t_half_adopted_closed_row_closes_out_without_cleanup_fields():
    # A HALF-ADOPTION: pr-adopt.py registers the ledger row (step 4) BEFORE it resolves the worktree
    # (step 5), and its documented git-failure path returns with worktree/worktree_owned/branch_owned left
    # at their ROW_DEFAULTS "-". If that PR is then CLOSED on GitHub, the close-out records `aborted` and
    # performs NO local cleanup, so it must NOT require those three fields — validating them (the old
    # over-strict path) refused on the unresolved worktree_owned, leaving status=in_review. Because a CLOSED
    # PR is absent from the open snapshot, the heartbeat re-routes it to this same finalizer forever (a wedge
    # loop). The close-out must terminate as `aborted`, clean NOTHING, and never touch the unresolved fields.
    td, root, f, led, real = scenario(state="CLOSED", worktree_owned="-", branch_owned="-")
    try:
        f.ledger(led, worktree_field="-")  # the half-adoption defaults: all three cleanup fields unresolved
        code, result, err = invoke(f, led, root)
        check(code == 0, f"half-adopted CLOSED row did not close out (over-strict validation?): {err}")
        check(status(led) == "aborted",
              f"half-adopted CLOSED row must terminate as aborted, got {status(led)!r}")
        check(result is not None and result["status"] == "closed-unmerged" and result["cleanup"] == {},
              f"the close-out must report closed-unmerged with no cleanup: {result}")
        check(f.merged_calls == 0, "the half-adopted close-out issued a merge command")
        check(not any(argv[:3] == ["gh", "pr", "merge"] for argv, _ in f.calls),
              "the half-adopted close-out issued a merge command")
        check(not any("worktree" in argv and "remove" in argv for argv, _ in f.calls),
              "the half-adopted close-out performed a local cleanup operation")
        check(f.worktree_present and f.branch_present,
              "the half-adopted close-out destroyed local resources")
    finally:
        finish(td, real)


def t_closed_out_terminates_despite_moved_head_base_or_branch():
    # The CLOSED close-out is LEDGER-ONLY (records `aborted`, merges nothing, cleans nothing), so the strict
    # live head/base/branch pins that gate a MERGE do not apply to it. Their trigger is ordinary same-repo
    # author behavior: a push advances the head, or a base/branch is renamed, and THEN the PR is closed.
    # A CLOSED PR never re-enters the open snapshot, so reconcile can never refresh the row's head_sha —
    # if the close-out refused on the pin the row would wedge at in_review forever. All three variants must
    # still terminate as `aborted` with no merge and no cleanup. (The pins stay in force on OPEN and MERGED:
    # t_stale_head_and_malformed_ownership_refused and t_root_and_foreign_targets_refused cover that.)
    for label, knob in (
        ("moved head", {"view_head": "b" * 40}),
        ("changed base", {"view_base": "release"}),
        ("changed branch", {"view_branch": "renamed"}),
    ):
        td, root, f, led, real = scenario(state="CLOSED", **knob)
        try:
            code, result, err = invoke(f, led, root)
            check(code == 0, f"{label}: close-out refused instead of terminating: {err}")
            check(status(led) == "aborted",
                  f"{label}: closed-without-merge row must terminate as aborted, got {status(led)!r}")
            check(result is not None and result["status"] == "closed-unmerged" and result["cleanup"] == {},
                  f"{label}: expected closed-unmerged with no cleanup, got {result}")
            check(f.merged_calls == 0, f"{label}: the close-out issued a merge command")
            check(f.worktree_present and f.branch_present,
                  f"{label}: the close-out deleted owned resources holding unmerged work")
        finally:
            finish(td, real)


def t_closed_out_terminates_every_held_status():
    # The close-out fires for ANY non-terminal status, not just `in_review`. A CLOSED PR moots every HELD
    # reason (`L.HELD_STATUSES` — awaiting-api/awaiting-user/repairing): nothing is left to merge, approve,
    # adjudicate, or repair, and a human closing a parked PR IS the resolution. So every held status must
    # terminate as `aborted` with NO merge and NO cleanup, exactly like the in_review close-out
    # (t_absent_snapshot_closed_row_terminates). This is the counterpart of the OPEN+held REFUSAL that
    # t_gate_refusals still pins: OPEN keeps waiting on the human, CLOSED closes out.
    for held in L.HELD_STATUSES:
        td, root, f, led, real = scenario(state="CLOSED", status=held)
        try:
            code, result, err = invoke(f, led, root)
            check(code == 0, f"{held}: CLOSED held row refused instead of terminating: {err}")
            check(status(led) == "aborted",
                  f"{held}: CLOSED held row must terminate as aborted, got {status(led)!r}")
            check(result is not None and result["status"] == "closed-unmerged" and result["cleanup"] == {},
                  f"{held}: expected closed-unmerged with no cleanup, got {result}")
            check(f.merged_calls == 0, f"{held}: the close-out issued a merge command")
            check(f.worktree_present and f.branch_present,
                  f"{held}: the close-out deleted owned resources holding unmerged work")
        finally:
            finish(td, real)

    # Guardrail: a `merged` row with a CLOSED live state stays a REFUSED contradiction (a merged PR reports
    # MERGED, not CLOSED). `merged` is excluded from the close-out, so it falls to the terminal status gate.
    td, root, f, led, real = scenario(state="CLOSED", status="merged")
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0 and "merged but GitHub state is" in err,
              f"merged+CLOSED must stay a refused contradiction, got code={code} err={err!r}")
        check(f.merged_calls == 0, "the merged/CLOSED contradiction must not merge")
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


def t_label_free_half_adopted_closed_out():
    # A HALF-ADOPTION can fail even EARLIER than the worktree step: pr-adopt.py persists the ledger row and
    # then, during its step-5 Git work, dies BEFORE `gh pr edit` attaches the run label. GitHub then reports
    # the PR as CLOSED with labels=[]. The close-out is ledger-only (records `aborted`, merges and cleans
    # NOTHING), so it must not require THIS run's own label to be present — over-strict validation refused
    # the missing label and left status=in_review, and because a CLOSED PR is absent from the open snapshot
    # the heartbeat re-routes it to this same finalizer forever (a wedge loop). The FOREIGN-label refusal is
    # NOT relaxed, though: a `gauntlet-run-*` label belonging to ANOTHER run still fails closed.
    td, root, f, led, real = scenario(state="CLOSED", worktree_owned="-", branch_owned="-", labels=[])
    try:
        f.ledger(led, worktree_field="-")  # half-adoption: cleanup fields unresolved AND no own label yet
        code, result, err = invoke(f, led, root)
        check(code == 0, f"label-free half-adopted CLOSED row did not close out (over-strict label?): {err}")
        check(status(led) == "aborted",
              f"label-free half-adopted CLOSED row must terminate as aborted, got {status(led)!r}")
        check(result is not None and result["status"] == "closed-unmerged" and result["cleanup"] == {},
              f"the close-out must report closed-unmerged with no cleanup: {result}")
        check(f.merged_calls == 0, "the label-free close-out issued a merge command")
        check(not any("worktree" in argv and "remove" in argv for argv, _ in f.calls),
              "the label-free close-out performed a local cleanup operation")
        check(f.worktree_present and f.branch_present, "the label-free close-out destroyed local resources")
    finally:
        finish(td, real)

    # The FOREIGN-label variant of the SAME half-adopted CLOSED row must still REFUSE — run isolation is not
    # relaxed just because own-label presence is.
    td, root, f, led, real = scenario(state="CLOSED", worktree_owned="-", branch_owned="-",
                                      labels=[{"name": "gauntlet-run-other"}])
    try:
        f.ledger(led, worktree_field="-")
        code, _result, err = invoke(f, led, root)
        check(code != 0 and "another run's owner label" in err,
              f"a foreign-labelled half-adopted CLOSED row was not refused: code={code} err={err!r}")
        check(f.merged_calls == 0, "the foreign-label refusal must precede any merge")
    finally:
        finish(td, real)


def t_external_merge_while_held_resumes():
    # A fully-adopted, still-HELD row (status in `L.HELD_STATUSES`) whose exact reviewed head a maintainer
    # merges out-of-band: GitHub reports MERGED. The work LANDED, so the finalizer must RESUME the owed
    # base-sync / owned cleanup / terminal write — not refuse "held" before ever reaching the MERGED-resume
    # branch (the old order rejected the held status first, wedging every absent-row heartbeat). Iterate the
    # enum so a new held status is covered with no edit.
    for held in L.HELD_STATUSES:
        td, root, f, led, real = scenario(state="MERGED", status=held)
        try:
            code, result, err = invoke(f, led, root)
            check(code == 0, f"{held}: external MERGE of a held row did not resume: {err}")
            check(f.merged_calls == 0, f"{held}: an externally-merged PR must not be re-merged")
            check(status(led) == "merged", f"{held}: resume must finalize the terminal ledger row")
            check(not f.worktree_present and not f.branch_present,
                  f"{held}: resume must complete owned base-sync/cleanup per ownership")
            check(result is not None and result["status"] == "merged", f"{held}: unexpected result: {result}")
        finally:
            finish(td, real)

    # Guardrail: a live OPEN held row is STILL refused — the campaign must never INITIATE a merge on a held
    # PR. Only an already-landed external MERGE resumes; OPEN keeps waiting on the human.
    for held in L.HELD_STATUSES:
        td, root, f, led, real = scenario(state="OPEN", status=held)
        try:
            code, _result, err = invoke(f, led, root)
            check(code != 0 and f"held ({held})" in err,
                  f"OPEN+{held} must stay refused as held, got code={code} err={err!r}")
            check(f.merged_calls == 0, f"OPEN+{held} must never reach the merge command")
        finally:
            finish(td, real)


def t_absent_held_row_external_merge_routes_to_resume():
    # The ROUTING DECISION for the new resume: an absent-from-snapshot HELD row (held statuses are NON-terminal,
    # so reconcile reports the absent fact exactly as it does for in_review) that GitHub has externally MERGED.
    # The one `absent_from_snapshot` fact routes to `merge.py run`, which resumes the held row to `merged`.
    td, root, f, led, real = scenario(state="MERGED", status="awaiting-user")
    try:
        prs = root / "prs.json"
        prs.write_text("[]", encoding="utf-8")
        facts = RECON.detect(led, prs, "g1")
        row_fact = facts["rows"]["9"]
        check(row_fact.get("absent_from_snapshot") is True,
              f"reconcile did not report the held row absent: {row_fact}")
        check("terminal" not in row_fact, f"a held row must not be pre-classified terminal: {row_fact}")
        code, result, err = invoke(f, led, root)
        check(code == 0, f"absent held+MERGED row did not resume through run: {err}")
        check(status(led) == "merged", f"absent held row finalized to {status(led)!r}, expected merged")
        check(f.merged_calls == 0, "an already-merged held PR must not be re-merged on resume")
        check(result is not None and result["status"] == "merged", f"unexpected result: {result}")
    finally:
        finish(td, real)


def t_repo_identity_mismatch_refused_before_view():
    # execute() scopes every gh call by `--repo` while operating in cwd=root, but a `--repo` that does NOT
    # name the checkout's OWN repository (a collision across shared history or a fork on PR number, head_sha,
    # branch, base, and run-label) would merge the reviewed HEAD onto an UN-reviewed base. The cross-repo
    # guard derives the canonical repo from the checkout (`gh repo view --json nameWithOwner`) and refuses
    # the mismatch BEFORE the first `gh pr view` — no live view, no merge. RED before the guard: root-derived
    # "other/repo" != --repo "o/r" is unchecked, so the run proceeds through the view to the merge. GREEN
    # after: it refuses naming both values.
    td, root, f, led, real = scenario(repo_identity="other/repo")
    try:
        code, _result, err = invoke(f, led, root)  # invoke passes --repo "o/r"
        check(code != 0, "a --repo that mismatches the checkout was not refused")
        check("o/r" in err and "other/repo" in err,
              f"the refusal must name both the passed --repo and the checkout repo: {err}")
        check(not any(argv[:3] == ["gh", "pr", "view"] for argv, _ in f.calls),
              "the repo-identity guard must fire BEFORE any live PR view")
        check(f.merged_calls == 0, "a repo mismatch must never reach the merge command")
    finally:
        finish(td, real)

    # GitHub owner/name is case-insensitive, so a case-ONLY difference must NOT refuse — the run proceeds.
    td, root, f, led, real = scenario(repo_identity="O/R")
    try:
        code, _result, err = invoke(f, led, root)  # --repo "o/r" vs checkout identity "O/R"
        check(code == 0, f"a case-only repo difference must be treated as a match: {err}")
    finally:
        finish(td, real)


def _mutating_calls(fake: "Fake") -> list:
    # Any file-mutating git subcommand the diagnostic path must NEVER issue. `checkout` (git -C <path>)
    # and `restore` collide on the token "checkout"/"restore"; scan the SUBCOMMAND slot (argv[3]) plus a
    # bare `stash`/`clean`/`reset`, which is what these commands look like in argv.
    banned = {"stash", "reset", "restore", "checkout", "clean", "add"}
    return [argv for argv, _ in fake.calls if len(argv) > 3 and argv[3] in banned]


def t_base_ff_blocked_names_uncommitted_paths():
    # A checked-out-base fast-forward blocked by an unrelated actor's uncommitted edits must NAME the
    # offending paths and PROPOSE commit-or-stash + re-run, WITHOUT touching any path. A staged, unstaged,
    # or untracked path blocks ONLY when it overlaps a path the incoming fast-forward updates; an unrelated
    # change in ANY category (including a staged one) does not block a fast-forward and must not be named.
    td, root, f, led, real = scenario(
        base_ff_blocked=True,
        staged_paths=["src/main.py", "z-unrelated-staged.txt"],
        unstaged_paths=["docs/notes.md", "harmless.txt", "build"],
        untracked_paths=["scratch/new.txt", "unrelated-new.txt"],
        incoming_paths=["docs/notes.md", "scratch/new.txt", "build/output", "src/main.py"])
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0, "a blocked base fast-forward must refuse")
        for named in ('"build"', '"docs/notes.md"', '"scratch/new.txt"', '"src/main.py"'):
            check(named in err, f"blocking path {named} was not named: {err!r}")
        # An unrelated STAGED path must NOT be named (finding: staged is overlap-filtered, not unconditional).
        for spared in ("harmless.txt", "unrelated-new.txt", "z-unrelated-staged.txt"):
            check(spared not in err, f"a path the fast-forward does not touch was named: {spared} in {err!r}")
        # The tailored refusal proposes the safe recovery and names the checkout.
        for needle in ("Commit", "stash", "re-run", "resume the owed base-sync",
                       str(root), "Original Git diagnostic"):
            check(needle in err, f"refusal missing {needle!r}: {err!r}")
        # Fail-CLOSED and non-destructive: the PR is durably MERGED, the row stays live, owned resources are
        # untouched, and NOT ONE mutating git command was issued.
        check(f.state == "MERGED", "the merge must remain durable across the base-sync refusal")
        check(status(led) == "in_review", "a refused base-sync must leave the ledger row live")
        check(f.worktree_present and f.branch_present,
              "a base-sync refusal must not clean any owned resource")
        check(f.merged_calls == 1, "the merge command must have run exactly once")
        check(_mutating_calls(f) == [], f"the diagnostic issued a mutating command: {_mutating_calls(f)}")
    finally:
        finish(td, real)


def t_base_ff_odd_filenames_quoted_and_ordered():
    # Odd filenames (space, tab, newline) must be JSON-quoted so a newline cannot forge a "  - " line, and
    # the listing must be deterministically sorted by raw path. All three are staged AND incoming, so each
    # overlaps the fast-forward and blocks it.
    names = ["a normal.txt", "b\ttab.txt", "c\nnewline.txt"]
    td, root, f, led, real = scenario(base_ff_blocked=True, staged_paths=names, incoming_paths=names)
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0, "a blocked base fast-forward must refuse")
        quoted = [json.dumps(n) for n in sorted(names)]
        check('"c\\nnewline.txt"' in quoted[2], "the newline name must be escaped, not literal")
        positions = [err.find(q) for q in quoted]
        check(all(p >= 0 for p in positions), f"a quoted odd filename is missing: {err!r}")
        check(positions == sorted(positions), f"blocking paths are not deterministically ordered: {err!r}")
        # No raw newline may leak a forged bullet line: exactly one bullet per blocker.
        bullets = [ln for ln in err.splitlines() if ln.startswith("  - ")]
        check(len(bullets) == len(names), f"a forged or missing bullet line: {bullets!r}")
    finally:
        finish(td, real)


def t_base_ff_divergent_keeps_raw_diagnostic():
    # A fast-forward the GRAPH forbids (HEAD not an ancestor of origin/<base>) is a genuine divergence, not
    # an uncommitted-work block. The diagnostic must decline and the original raw _require() error stands.
    td, root, f, led, real = scenario(base_ff_blocked=True, ancestor_ok=False,
                                      staged_paths=["would-be-named.txt"])
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0, "a diverged base fast-forward must still refuse")
        check("fast-forward of checked-out base main failed" in err,
              f"the raw _require diagnostic must be preserved for a divergence: {err!r}")
        check("uncommitted paths block" not in err,
              f"a divergence must not be reported as an uncommitted-work block: {err!r}")
        check("would-be-named.txt" not in err, "no path may be named when the graph forbids the fast-forward")
    finally:
        finish(td, real)


def t_base_ff_unmerged_index_keeps_raw_diagnostic():
    # An UNMERGED (conflicted) index makes ff-only fail with git's unresolved-conflict error, and git refuses
    # both commit and stash while unmerged — so the commit-or-stash advice would be wrong. The helper must
    # detect the stage>0 index (ls-files --unmerged non-empty) and decline, keeping git's raw error. The
    # ancestor guard does NOT catch this (a conflicted merge leaves HEAD un-advanced, still an ancestor).
    td, root, f, led, real = scenario(
        base_ff_blocked=True, ancestor_ok=True,
        unmerged_paths=["conflicted.txt"],
        staged_paths=["conflicted.txt"], incoming_paths=["conflicted.txt"])
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0, "an unmerged-index base fast-forward must still refuse")
        check("fast-forward of checked-out base main failed" in err,
              f"an unmerged index must keep the raw git error: {err!r}")
        check("uncommitted paths block" not in err,
              f"an unmerged index must not be reported as a commit-or-stash-able block: {err!r}")
        check("conflicted.txt" not in err, "no path may be named when the index is unmerged")
    finally:
        finish(td, real)


def t_base_ff_plumbing_failure_keeps_raw_diagnostic():
    # If any diagnostic plumbing probe fails, the helper returns None and the ORIGINAL fast-forward error is
    # preserved — never replaced by a secondary diagnostic failure.
    for probe in ("unmerged", "staged", "unstaged", "untracked", "incoming"):
        td, root, f, led, real = scenario(base_ff_blocked=True, plumb_fail=probe,
                                          staged_paths=["x.txt"], incoming_paths=["x.txt"])
        try:
            code, _result, err = invoke(f, led, root)
            check(code != 0, f"{probe}: a blocked base fast-forward must refuse")
            check("fast-forward of checked-out base main failed" in err,
                  f"{probe}: a diagnostic-probe failure must keep the raw error: {err!r}")
            check("uncommitted paths block" not in err,
                  f"{probe}: a diagnostic-probe failure must not fabricate a blocker list: {err!r}")
        finally:
            finish(td, real)


def t_base_ff_block_clears_then_resumes():
    # After the blocking paths are committed/stashed, a second invocation completes the owed base-sync,
    # cleanup, and terminal write — WITHOUT another gh pr merge (the merge is durably MERGED).
    td, root, f, led, real = scenario(base_ff_blocked=True, staged_paths=["blocker.txt"])
    try:
        first, _result, err = invoke(f, led, root)
        check(first != 0 and status(led) == "in_review", f"first invocation must refuse and stay live: {err}")
        check(f.merged_calls == 1, "the first invocation must have merged exactly once")
        # The user commits/stashes the blocker; the base-sync is now unobstructed.
        f.base_ff_blocked = False
        f.staged_paths = []
        second, _result, err = invoke(f, led, root)
        check(second == 0 and status(led) == "merged", f"resume did not finalize: {err}")
        check(f.merged_calls == 1, "resume must not re-issue gh pr merge")
        check(not f.worktree_present and not f.branch_present,
              "resume must clean both owned resources once base-sync succeeds")
    finally:
        finish(td, real)


def t_dirty_owned_worktree_cleanup_refusal_unchanged():
    # The pre-existing dirty-owned-worktree cleanup refusal is a SEPARATE fail-closed boundary and is not
    # affected by the base-sync diagnostic: base-sync succeeds, then cleanup refuses the dirty owned worktree.
    td, root, f, led, real = scenario(fail_once="dirty")
    try:
        code, _result, err = invoke(f, led, root)
        check(code != 0 and "is dirty; refusing cleanup" in err,
              f"the dirty owned-worktree cleanup refusal must stand unchanged: {err!r}")
        check(f.state == "MERGED", "cleanup refusal must follow a confirmed merge")
        check(_mutating_calls(f) == [], f"cleanup refusal must issue no mutating command: {_mutating_calls(f)}")
    finally:
        finish(td, real)


# ------------------------------------------------------------------------------------------------------
# REAL-git fixtures. The Fake above cannot exercise the byte-mode path capture, the overlap-filtered staged
# semantics against a real `git merge --ff-only`, or a real unmerged index — its `nul` returns a Python
# object and its ff/plumbing results are hardcoded knobs. These build throwaway real repos in a tempdir and
# call `_blocking_uncommitted_paths` (and real `git merge --ff-only`) directly, with the real `_run`. They
# need a real `git` binary. Global/system config is neutralised for the SETUP commits (isolated env); the
# read-only probes the helper runs are config-independent (`-z` disables path quoting).
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def _rg_git(work: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(work), *args], env=_GIT_ENV,
                          capture_output=True, check=check)


def _rg_head(work: Path, rev: str = "HEAD") -> str:
    return _rg_git(work, "rev-parse", rev).stdout.decode().strip()


def _rg_write(work: Path, rel: bytes, content: bytes) -> None:
    # Write through BYTE paths so a filename carrying a non-UTF-8 byte (0xff) or a CR round-trips verbatim.
    full = os.path.join(os.fsencode(str(work)), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(content)


def _rg_setup_ahead(work: Path, incoming: "list[bytes]") -> None:
    # Commit C0 (each incoming path at v0), advance to C1 (each rewritten to v1), point origin/main at C1,
    # then reset HEAD --hard back to C0. HEAD is now an ancestor of origin/main and `git diff C0..C1` (the
    # incoming set) is exactly `incoming` — the shape a real post-merge base fast-forward would apply.
    subprocess.run(["git", "init", "-q", str(work)], env=_GIT_ENV, capture_output=True, check=True)
    for rel in incoming:
        _rg_write(work, rel, b"v0\n")
    _rg_git(work, "add", "-A")
    _rg_git(work, "commit", "-q", "-m", "C0")
    c0 = _rg_head(work)
    for rel in incoming:
        _rg_write(work, rel, b"v1\n")
    _rg_git(work, "add", "-A")
    _rg_git(work, "commit", "-q", "-m", "C1")
    _rg_git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    _rg_git(work, "reset", "-q", "--hard", c0)


def t_realgit_unrelated_staged_survives_ff():
    # Finding: `blockers = set(staged)` assumed EVERY staged path blocks a fast-forward. Real git disproves
    # it — `git merge --ff-only` SUCCEEDS with an unrelated staged file present, because the fast-forward
    # touches only the incoming paths. This is the ground truth the overlap filter is built on.
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "checkout"
        work.mkdir()
        _rg_setup_ahead(work, [b"docs/notes.md"])
        _rg_write(work, b"z-staged.txt", b"unrelated\n")  # a NEW file, not touched by the fast-forward
        _rg_git(work, "add", "z-staged.txt")
        ff = _rg_git(work, "merge", "--ff-only", "origin/main", check=False)
        check(ff.returncode == 0,
              f"git ff-only must SUCCEED with only an unrelated staged file (the 'any staged change blocks' "
              f"assumption is false): rc={ff.returncode} {ff.stderr!r}")
        check(_rg_head(work) == _rg_head(work, "origin/main"),
              "the successful fast-forward must have advanced HEAD to origin/main")


def t_realgit_blocked_ff_spares_unrelated_staged():
    # A real blocked fast-forward (a local edit to an incoming path) with an unrelated staged file also
    # present must name ONLY the overlapping incoming path — never the unrelated staged one.
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "checkout"
        work.mkdir()
        _rg_setup_ahead(work, [b"docs/notes.md"])
        _rg_write(work, b"docs/notes.md", b"local edit\n")  # unstaged edit to an INCOMING path -> blocks
        _rg_write(work, b"z-staged.txt", b"unrelated\n")
        _rg_git(work, "add", "z-staged.txt")               # unrelated staged file present
        ff = _rg_git(work, "merge", "--ff-only", "origin/main", check=False)
        check(ff.returncode != 0, "a local edit to an incoming path must block the fast-forward")
        blockers = M._blocking_uncommitted_paths(str(work), "main")
        check(blockers == [json.dumps("docs/notes.md")],
              f"only the overlapping incoming path may be named, not the unrelated staged file: {blockers}")


def t_realgit_odd_byte_and_cr_filenames_named():
    # A non-UTF-8 (0xff) filename and a CR filename must go through the byte-mode probe and be named
    # correctly. BEFORE the fix, text=True capture raised UnicodeDecodeError on the 0xff byte (a crash that
    # discarded the original ff error), and universal-newline translation rewrote the CR to LF (naming a
    # DIFFERENT path). Byte-mode split-before-decode with surrogateescape names both verbatim.
    cr = b"cr\rname.txt"
    odd = b"bad\xffname.txt"
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "checkout"
        work.mkdir()
        _rg_setup_ahead(work, [cr, odd])
        _rg_write(work, cr, b"local\n")   # unstaged edits to both incoming odd-named files -> block + named
        _rg_write(work, odd, b"local\n")
        ff = _rg_git(work, "merge", "--ff-only", "origin/main", check=False)
        check(ff.returncode != 0, "local edits to incoming odd-named files must block the fast-forward")
        blockers = M._blocking_uncommitted_paths(str(work), "main")
        check(blockers is not None, "the odd-filename probe must not crash or decline")
        expect = sorted([json.dumps(odd.decode("utf-8", "surrogateescape")),
                         json.dumps(cr.decode("utf-8", "surrogateescape"))])
        check(blockers == expect, f"odd-named blockers were misnamed: {blockers} != {expect}")
        check(any("\r" in json.loads(b) for b in blockers),
              f"the CR in a filename was translated away (text-mode capture leaked): {blockers}")


def t_realgit_unmerged_index_keeps_raw_error():
    # An UNMERGED index (a conflicting merge left in progress) makes ff-only fail with git's unresolved
    # -conflict error; git refuses both commit and stash while unmerged, so the commit-or-stash advice would
    # be wrong. HEAD stays un-advanced during the conflict, so it is STILL an ancestor of origin/main — the
    # ancestor guard does not catch this. The unmerged probe must, so the helper declines and the raw error
    # stands.
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "checkout"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], env=_GIT_ENV, capture_output=True, check=True)
        _rg_write(work, b"g.txt", b"base\n")
        _rg_write(work, b"f.txt", b"f\n")
        _rg_git(work, "add", "-A")
        _rg_git(work, "commit", "-q", "-m", "C0")
        c0 = _rg_head(work)
        _rg_git(work, "checkout", "-q", "-b", "side")
        _rg_write(work, b"g.txt", b"side\n")
        _rg_git(work, "add", "-A")
        _rg_git(work, "commit", "-q", "-m", "Cs")
        _rg_git(work, "checkout", "-q", "-b", "work", c0)
        _rg_write(work, b"g.txt", b"work\n")            # a conflicting change to g.txt from the same base
        _rg_git(work, "add", "-A")
        _rg_git(work, "commit", "-q", "-m", "Cc")
        cc = _rg_head(work)
        _rg_write(work, b"incoming.txt", b"up\n")       # advance origin/main beyond Cc, then reset to Cc
        _rg_git(work, "add", "-A")
        _rg_git(work, "commit", "-q", "-m", "Cn")
        _rg_git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
        _rg_git(work, "reset", "-q", "--hard", cc)
        conflict = _rg_git(work, "merge", "side", check=False)  # leaves an unmerged index + MERGE_HEAD
        check(conflict.returncode != 0, "the merge must conflict, leaving an unmerged index")
        check(_rg_git(work, "ls-files", "--unmerged").stdout.strip() != b"",
              "precondition: the index must carry stage>0 entries")
        check(_rg_git(work, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode == 0,
              "precondition: HEAD must still be an ancestor of origin/main (the ancestor guard passes)")
        ff = _rg_git(work, "merge", "--ff-only", "origin/main", check=False)
        check(ff.returncode != 0, "ff-only must fail while a merge is unresolved")
        blockers = M._blocking_uncommitted_paths(str(work), "main")
        check(blockers is None,
              f"an unmerged index must make the helper decline so git's raw error stands, got: {blockers}")


CASES = [
    ("happy-owned", "exact merge argv, owned cleanup, terminal write", t_happy_owned_cleanup_and_command),
    ("repo-identity", "a --repo that does not name the checkout's own repository is refused before any live view (case-insensitive match)", t_repo_identity_mismatch_refused_before_view),
    ("merged-live-row", "MERGED with live ledger resumes without another merge", t_merge_landed_ledger_live_resumes),
    ("ownership-matrix", "all worktree/branch ownership combinations clean only owned resources", t_reused_resources_are_left),
    ("root-foreign", "root cleanup and foreign branch are refused before merge", t_root_and_foreign_targets_refused),
    ("gate-refusals", "held, stale, red, pending, and short-tally rows never merge", t_gate_refusals),
    ("blocked-probe-ancestry", "a BLOCKED PROBE resolves via the base-ancestry probe: behind rebases, up-to-date parks; neither merges", t_blocked_probe_rebases_when_behind_parks_when_current),
    ("stale-malformed", "stale live SHA and malformed ownership fail closed", t_stale_head_and_malformed_ownership_refused),
    ("owner-and-view", "another run and uncertain GitHub state fail closed", t_owner_label_and_uncertain_view_refused),
    ("base-location", "checked-out and absent local base use their documented update paths", t_base_checked_out_and_absent),
    ("dash-base-safe", "a dash-leading base name is fully-qualified at both fetch sites, never option-parseable", t_dash_leading_base_is_never_option_parseable),
    ("live-base-retarget", "a live base retarget refuses with the shared machine-blocker reason", t_live_base_retarget_refuses_with_shared_reason),
    ("explicit-row-base", "an explicit row base_branch (header `-`) drives validate/ancestry/sync", t_explicit_row_base_drives_every_base_door),
    ("local-ahead-base-synced", "a local-ahead base is already synced: base-sync skips the non-ff fetch and finalization reaches cleanup + terminal write", t_local_ahead_base_skips_fetch_and_finalizes),
    ("merge-resume", "merge and confirmation failures resume safely", t_merge_and_confirmation_failures_resume),
    ("postmerge-resume", "every post-merge phase resumes without another merge", t_postmerge_phase_failures_resume),
    ("merge-accepted", "MERGED confirmation outranks a lost merge response", t_merge_transport_failure_after_acceptance_continues),
    ("terminal-write-resume", "a failed terminal write resumes after already-completed cleanup", t_terminal_write_failure_resumes_after_cleanup),
    ("terminal-repeat", "repeated invocation after terminal state is a no-op", t_repeat_after_terminal_is_noop),
    ("aborted-terminal-repeat", "repeating after a CLOSED close-out is an already-complete no-op (moved refs tolerated); aborted+OPEN/MERGED refuses as a contradiction", t_repeat_after_closed_terminal_is_noop),
    ("head-race", "--match-head-commit refuses a tip that advanced before the merge landed", t_head_race_between_view_and_merge_refuses_before_landing),
    ("merge-method", "merge method is a validated input; squash-disabled repo has a prevailing-method recourse", t_merge_method_input_validated_and_applied),
    ("absent-resume", "an absent-but-unfinalized MERGED row resumes its remaining phases through run", t_absent_snapshot_merged_row_resumes_via_run),
    ("absent-closed", "an absent-but-unfinalized CLOSED-without-merge row terminates as aborted with no cleanup", t_absent_snapshot_closed_row_terminates),
    ("half-adopted-closed", "a half-adopted CLOSED row closes out to aborted without requiring the cleanup-ownership fields", t_half_adopted_closed_row_closes_out_without_cleanup_fields),
    ("closed-out-moved-refs", "the CLOSED close-out terminates as aborted despite a moved head, base, or branch", t_closed_out_terminates_despite_moved_head_base_or_branch),
    ("closed-out-held-statuses", "a CLOSED PR closes out every held status to aborted; merged+CLOSED stays refused", t_closed_out_terminates_every_held_status),
    ("absent-routing", "the absent fact routes reconcile -> merge.py run, which finalizes MERGED and CLOSED sides", t_absent_routing_decision),
    ("label-free-half-adopted-closed", "a half-adopted CLOSED row with no own label closes out to aborted; a foreign label still refuses", t_label_free_half_adopted_closed_out),
    ("external-merge-held-resume", "an external MERGE of a held row resumes to merged for every held status; OPEN+held stays refused", t_external_merge_while_held_resumes),
    ("absent-held-merge-routing", "an absent held row externally MERGED routes reconcile -> merge.py run, which resumes it to merged", t_absent_held_row_external_merge_routes_to_resume),
    ("base-ff-blocked-named", "a checked-out base fast-forward blocked by uncommitted paths names them and proposes commit-or-stash + re-run, touching nothing", t_base_ff_blocked_names_uncommitted_paths),
    ("base-ff-odd-names", "odd blocking filenames are JSON-quoted (no forged line) and deterministically ordered", t_base_ff_odd_filenames_quoted_and_ordered),
    ("base-ff-divergent-raw", "a diverged base fast-forward keeps the raw git diagnostic, names no path", t_base_ff_divergent_keeps_raw_diagnostic),
    ("base-ff-unmerged-raw", "an unmerged/conflicted index keeps the raw git error and names no path (commit/stash advice would be wrong)", t_base_ff_unmerged_index_keeps_raw_diagnostic),
    ("base-ff-plumb-fail-raw", "a diagnostic-probe failure keeps the original fast-forward error", t_base_ff_plumbing_failure_keeps_raw_diagnostic),
    ("base-ff-clears-resumes", "committing/stashing the blockers lets a second run finish base-sync + cleanup with no re-merge", t_base_ff_block_clears_then_resumes),
    ("dirty-cleanup-refusal", "the pre-existing dirty owned-worktree cleanup refusal is unchanged and separate", t_dirty_owned_worktree_cleanup_refusal_unchanged),
    ("realgit-unrelated-staged-ff", "REAL git: an unrelated staged path survives a successful ff (the 'any staged change blocks' assumption is false)", t_realgit_unrelated_staged_survives_ff),
    ("realgit-blocked-ff-spares-staged", "REAL git: a blocked ff names only the overlapping incoming path, never an unrelated staged one", t_realgit_blocked_ff_spares_unrelated_staged),
    ("realgit-odd-filenames", "REAL git: a 0xff-byte and a CR filename go through the byte-mode probe and are named verbatim (no crash, no CR->LF)", t_realgit_odd_byte_and_cr_filenames_named),
    ("realgit-unmerged-index", "REAL git: an unmerged-index ff failure makes the helper decline, preserving git's raw unresolved-conflict error", t_realgit_unmerged_index_keeps_raw_error),
]
