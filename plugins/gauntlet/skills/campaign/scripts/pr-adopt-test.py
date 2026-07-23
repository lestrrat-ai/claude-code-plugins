#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `pr-adopt.py` — the mechanical PR-adoption decision, pinned WITHOUT live GitHub.

Every case drives the PURE surface (`build_plan` / `slugify`) or the `plan` CLI (via `capture_cli` and a
`--view-json` file), so the whole refusal contract and every computed row field are checked offline. The
`adopt` executor is a thin wrapper of `gh`/`git`/ledger calls around the same `build_plan`, so the decision
it acts on is exactly what these fixtures pin.

`pr-adopt.py self-test` FAILS LOUDLY if it cannot load this file — it can never report health over a suite
it did not run.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "pr-adopt.py"


def _load_owner():
    mod = load_module_from_path("pr_adopt_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the pr-adopt tool at {OWNER}")
    return mod


M = _load_owner()


def _sibling(name: str, filename: str):
    """Load a sibling campaign tool as a module for the cross-script integration fixture below. Guards the
    `None` return exactly as `_load_owner` does, so every attribute access is on a real module."""
    mod = load_module_from_path(name, Path(__file__).resolve().parent / filename)
    if mod is None:
        raise RuntimeError(f"cannot load the sibling tool at {filename}")
    return mod


def check(cond, msg) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


def lbl(name: str) -> dict:
    return {"name": name}


def view(**kw) -> dict:
    """A clean, same-repo, OPEN `gh pr view` payload; override any field with a keyword. `body` is absent
    on purpose — adoption never requests it (a fork PR's body is attacker-controlled), so it is not here."""
    base = {
        "number": 12,
        "title": "Fix the thing",
        "headRefName": "feat-x",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "labels": [],
        "state": "OPEN",
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "someone"},
        "headRepository": {"name": "their-fork"},
    }
    base.update(kw)
    return base


def plan(**kw) -> dict:
    args = {"run_id": "g1", "tier": "HIGH", "worktrees_root": "/wt"}
    args.update({k: kw.pop(k) for k in list(kw) if k in args})
    return M.build_plan(view(**kw), **args)


# --- refusals (fail closed) ---------------------------------------------------

def t_refuse_fork():
    p = M.build_plan(view(isCrossRepository=True), run_id="g1", tier="HIGH", worktrees_root="/wt")
    check(p["verdict"] == "refuse", "a cross-repo (fork) PR must be REFUSED")
    check("fork" in p["reason"].lower(), "the fork refusal reason must name the fork")
    check("their-fork" in p["reason"], "the fork refusal names the head fork owner/repo")
    # teeth: the same view with isCrossRepository False is NOT refused as a fork
    check(plan(isCrossRepository=False)["verdict"] == "adopt",
          "a same-repo PR must not be refused as a fork")


def t_refuse_foreign_run():
    p = plan(labels=[lbl("gauntlet-run-OTHER")])
    check(p["verdict"] == "refuse", "a PR owned by another run must be REFUSED")
    check("gauntlet-run-OTHER" in p["reason"], "the refusal names the OTHER run's owner label")
    # teeth: an unrelated label does not trip the foreign-owner check
    check(plan(labels=[lbl("bug"), lbl("gauntlet-authored")])["verdict"] == "adopt",
          "a non-run label must not read as foreign ownership")


def t_refuse_closed():
    for st in ("MERGED", "CLOSED"):
        p = plan(state=st)
        check(p["verdict"] == "refuse", f"a {st} PR must be REFUSED — campaign gates OPEN PRs")
        check(st.lower() in p["reason"].lower() or st in p["reason"],
              f"the refusal reason must name the {st} state")
    check(plan(state="OPEN")["verdict"] == "adopt", "an OPEN PR must adopt")


# --- adopt --------------------------------------------------------------------

def t_adopt_same_repo():
    p = M.build_plan(view(headRefOid="b" * 40, headRefName="feat-x", baseRefName="main",
                          title="Fix the thing"),
                     run_id="g1", tier="HIGH", worktrees_root="/wt")
    check(p["verdict"] == "adopt", "a clean same-repo OPEN PR must adopt")
    row = p["row"]
    check(row["head_sha"] == "b" * 40, "head_sha = headRefOid")
    check(row["tier"] == "HIGH", "tier = the input, not triaged here")
    check(row["ci"] == "pending", "ci initializes to pending")
    check(row["status"] == "in_review", "status initializes to in_review")
    check(row["reviews_ok"] == "0", "reviews_ok initializes to 0 (no verdicts yet)")
    check(row["pr_origin"] == "external", "pr_origin = the input")
    check(p["labels_add"] == ["gauntlet-run-g1", "gauntlet-reviewing"],
          "labels_add == [our owner label, gauntlet-reviewing]")
    check(p["worktree"] == str(Path("/wt") / "feat-x"), "worktree == worktrees_root / headRefName")
    check(p["base"] == "main", "base == baseRefName, carried under `base` for the executor to record per-row")
    # teeth: base/worktree/ownership flags are NOT in the PLAN row — the executor writes the row base_branch
    # (add-row --base-branch), and step 5 decides worktree ownership; build_plan carries neither.
    check("base" not in row and "base_branch" not in row and "worktree" not in row,
          "base/base_branch/worktree must NOT be in the plan row (base rides `base`; the executor records it)")
    check("worktree_owned" not in row and "branch_owned" not in row,
          "worktree_owned/branch_owned are decided at step 5, never in the plan row")


def t_adopt_when_already_ours():
    p = plan(labels=[lbl("gauntlet-run-g1"), lbl("gauntlet-reviewing")])
    check(p["verdict"] == "adopt", "a PR already carrying OUR run label re-adopts, never refuses")
    check(p["row"]["status"] == "in_review", "re-adoption still yields the computed row")


# --- slugify ------------------------------------------------------------------

def t_slugify():
    check(M.slugify("Fix the Thing!") == "fix-the-thing", "lowercase, punctuation → single dash")
    check(M.slugify("  Hello, World  ") == "hello-world", "no leading/trailing dash, spaces collapse")
    check(M.slugify("A/B: c__d") == "a-b-c-d", "every non-alnum run collapses to one dash")
    s = M.slugify("!!! ??? !!!")
    check(s == "" and not s.startswith("-") and not s.endswith("-"),
          "an all-punctuation title slugs to the empty, dash-free string")


# --- the `plan` CLI (the testable surface) ------------------------------------

def _write_view(d: str, v: dict) -> str:
    f = Path(d) / "view.json"
    f.write_text(json.dumps(v), encoding="utf-8")
    return str(f)


def t_cli_plan_refuses_fork():
    with tempfile.TemporaryDirectory() as d:
        f = _write_view(d, view(isCrossRepository=True))
        code, out, _ = capture_cli(M.main, ["plan", "--view-json", f, "--run-id", "g1", "--tier", "HIGH"])
        check(code == 0, "`plan` always exits 0 — it prints a verdict, it does not gate")
        p = json.loads(out)
        check(p["verdict"] == "refuse", "`plan` prints the refuse verdict as JSON")
        check("fork" in p["reason"].lower(), "the printed refusal names the fork")


def t_cli_plan_adopts():
    with tempfile.TemporaryDirectory() as d:
        f = _write_view(d, view(title="Add pr-adopt", headRefName="feat-y", headRefOid="c" * 40))
        code, out, _ = capture_cli(M.main, ["plan", "--view-json", f, "--run-id", "g1", "--tier", "HIGH"])
        check(code == 0, "`plan` exits 0 on an adoptable PR")
        p = json.loads(out)
        check(p["verdict"] == "adopt", "`plan` prints the adopt verdict")
        check(p["row"]["tier"] == "HIGH", "the printed row carries the input tier")
        check(p["row"]["head_sha"] == "c" * 40, "the printed row's head_sha = headRefOid")
        check(p["labels_add"] == ["gauntlet-run-g1", "gauntlet-reviewing"],
              "the printed labels_add == [owner label, gauntlet-reviewing]")


# --- pr_origin is DERIVED from labels, never a caller flag (fix 3) -------------

def t_pr_origin_from_label():
    p = M.build_plan(view(labels=[lbl("gauntlet-authored")]), run_id="g1", tier="HIGH", worktrees_root="/wt")
    check(p["row"]["pr_origin"] == "gauntlet",
          "the gauntlet-authored label — and only it — makes pr_origin=gauntlet")
    p2 = M.build_plan(view(labels=[lbl("bug"), lbl("gauntlet-run-g1")]),
                      run_id="g1", tier="HIGH", worktrees_root="/wt")
    check(p2["row"]["pr_origin"] == "external",
          "any other label set defaults to external (the SAFE default)")
    p3 = M.build_plan(view(labels=[]), run_id="g1", tier="HIGH", worktrees_root="/wt")
    check(p3["row"]["pr_origin"] == "external", "no labels -> external")


def t_driver_cannot_assert_origin():
    """The `--pr-origin` flag is GONE from both subcommands, so a driver cannot claim `gauntlet` on a PR it
    did not open — argparse rejects the flag outright."""
    with tempfile.TemporaryDirectory() as d:
        f = _write_view(d, view(labels=[]))
        code, _, _ = capture_cli(M.main, ["plan", "--view-json", f, "--run-id", "g1", "--tier", "HIGH",
                                          "--pr-origin", "gauntlet"])
        check(code != 0, "the removed --pr-origin flag must be REJECTED on `plan`")
        code2, _, _ = capture_cli(M.main, ["adopt", "--pr", "1", "--run-id", "g1", "--file", "x",
                                           "--tier", "HIGH", "--worktrees-root", "w", "--project-root", "p",
                                           "--pr-origin", "gauntlet"])
        check(code2 != 0, "the removed --pr-origin flag must be REJECTED on `adopt` too")


# --- the `adopt` EXECUTOR — driven with a fake `gh`/`git`/ledger boundary ------
#
# `Recorder` replaces pr-adopt's `_run`: it RECORDS every argv+cwd, answers `gh pr view` from a canned
# view, runs the real ledger IN-PROCESS against the temp store (so re-adoption reads back true state), and
# answers `git` from the scenario knobs. That pins the executor's gh scoping, the DISCOVERY chokepoint
# (`git worktree list --porcelain -z`), its reuse/create branching, its worktree SHA checks, and its ledger
# writes offline, with no live GitHub and no real git repo.
#
# The worktree-discovery scenario is `checkouts`: a list of (path, branch_ref_or_None) tuples the fake
# renders as real `-z` porcelain, exactly as git would. A None branch is a DETACHED entry. Any path in
# `checkouts` is OCCUPIED, so a `git worktree add` targeting it fails (exit 128) — the same fail-closed git
# gives a detached/foreign checkout sitting at the default path.


def _worktree_list_z(checkouts, head) -> str:
    """Render `checkouts` as `git worktree list --porcelain -z` output: each field NUL-terminated, each
    entry ended by an extra empty (NUL) field. A None branch renders as a `detached` entry."""
    out = ""
    for path, branch in checkouts:
        out += f"worktree {path}\0HEAD {head}\0"
        out += "detached\0" if branch is None else f"branch {branch}\0"
        out += "\0"
    return out

def _ledger(*args) -> int:
    """Run the real ledger main quietly (its row JSON would otherwise pollute the self-test output)."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return M.L.main(list(args))


def _init_ledger(ledger: Path, run_id: str = "g1", base_branch: str = "main") -> None:
    # A legacy-style header carries the run's real base (the `view()` default `baseRefName` is "main"), so a
    # re-adopted legacy row with NO explicit row base resolves through `effective_base` to the live base and
    # does not trip the re-adoption base gate. Pass a different `base_branch` to exercise a mismatch/park.
    _ledger("--file", str(ledger), "header", "set", "run_id", run_id)
    _ledger("--file", str(ledger), "header", "set", "base_branch", base_branch)


def _add_row(ledger: Path, pr, **fields) -> None:
    argv = ["--file", str(ledger), "add-row", "--pr", str(pr)]
    for k, v in fields.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    _ledger(*argv)


def _set_row(ledger: Path, pr, **fields) -> None:
    argv = ["--file", str(ledger), "set", "--pr", str(pr)]
    for k, v in fields.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    _ledger(*argv)


def _record_verdict(ledger: Path, pr, head_sha, verdict="satisfied") -> None:
    # `verdict` refuses unless a base-preflight `proceed` is on record for THIS head
    # (base_ok_sha == head_sha); stamp it first, exactly as the real flow does (base-preflight.py -> base-ok).
    _ledger("--file", str(ledger), "base-ok", "--pr", str(pr), "--head-sha", head_sha)
    _ledger("--file", str(ledger), "verdict", "--pr", str(pr), "--head-sha", head_sha, "--verdict", verdict)


def _field(ledger: Path, pr, name):
    _, rows = M.L.load(ledger)
    row = M.L.find_row(rows, str(pr))
    return row[name] if row else None


def _labelset(argv, flag):
    return {argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)}


class Recorder:
    def __init__(self, *, view, worktree_head=None, local_branch_exists=False,
                 checkouts=None, dirty=False, ff_fails=False):
        self.view = view
        self.calls: list = []
        self.worktree_head = worktree_head if worktree_head is not None else view["headRefOid"]
        self.local_branch_exists = local_branch_exists
        self.checkouts = list(checkouts or [])
        self.dirty = dirty
        self.ff_fails = ff_fails

    def _occupied(self):
        return {path for path, _ in self.checkouts}

    def __call__(self, argv, *, cwd=None):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        prog = argv[0]
        if prog == "gh":
            if argv[1:3] == ["pr", "view"]:
                return CompletedProcess(argv, 0, json.dumps(self.view), "")
            return CompletedProcess(argv, 0, "", "")  # label create / pr edit
        if prog == "python3":  # the ledger subprocess — run it for real against the temp store
            try:
                code = _ledger(*argv[2:])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            return CompletedProcess(argv, code or 0, "", "ledger reported failure")
        if prog == "git":
            sub = argv[3] if len(argv) > 3 and argv[1] == "-C" else ""
            if sub == "worktree":
                wsub = argv[4] if len(argv) > 4 else ""
                if wsub == "list":
                    return CompletedProcess(argv, 0, _worktree_list_z(self.checkouts, self.worktree_head), "")
                if wsub == "add":
                    # `add <worktree> <branch>` or `add -b <branch> <worktree> <ref>`; find the target path.
                    target = argv[7] if len(argv) > 5 and argv[5] == "-b" else argv[5]
                    if target in self._occupied():
                        return CompletedProcess(argv, 128, "", f"fatal: '{target}' already exists")
                    return CompletedProcess(argv, 0, "", "")
                return CompletedProcess(argv, 0, "", "")
            if sub == "show-ref":
                return CompletedProcess(argv, 0 if self.local_branch_exists else 1, "", "")
            if sub == "status":
                return CompletedProcess(argv, 0, "M file\n" if self.dirty else "", "")
            if sub == "merge":
                return CompletedProcess(argv, 1 if self.ff_fails else 0, "",
                                        "not a fast-forward" if self.ff_fails else "")
            if sub == "rev-parse":
                return CompletedProcess(argv, 0, self.worktree_head + "\n", "")
            return CompletedProcess(argv, 0, "", "")  # fetch
        return CompletedProcess(argv, 0, "", "")

    def gh_calls(self):
        return [c for c in self.calls if c["argv"][0] == "gh"]

    def one(self, *prefix):
        for c in self.calls:
            if c["argv"][: len(prefix)] == list(prefix):
                return c["argv"]
        return None

    def any_call(self, pred):
        return any(pred(c["argv"]) for c in self.calls)


def _adopt(d: Path, ledger: Path, v: dict, *, wroot: Path, worktree_head=None,
           local_branch_exists=False, checkouts=None, dirty=False, ff_fails=False,
           tier="HIGH", run_id="g1", repo=None):
    rec = Recorder(view=v, worktree_head=worktree_head, local_branch_exists=local_branch_exists,
                   checkouts=checkouts, dirty=dirty, ff_fails=ff_fails)
    argv = ["adopt", "--pr", str(v["number"]), "--run-id", run_id, "--file", str(ledger),
            "--tier", tier, "--worktrees-root", str(wroot), "--project-root", str(d)]
    if repo:
        argv += ["--repo", repo]
    old = M._run
    setattr(M, "_run", rec)
    try:
        code, out, err = capture_cli(M.main, argv)
    finally:
        setattr(M, "_run", old)
    return code, out, err, rec


# --- fix 1: `gh pr view` requests METADATA ONLY, never `body` ------------------

def t_view_omits_body():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        v = view()
        code, _, err, rec = _adopt(d, ledger, v, wroot=d / "wt")
        check(code == 0, f"a clean adopt succeeds (got {code}: {err})")
        va = rec.one("gh", "pr", "view")
        check(va is not None, "the executor issues a `gh pr view`")
        assert va is not None  # narrow for the type checker; `check` above is the readable guard
        fields = set(va[va.index("--json") + 1].split(","))
        check("body" not in fields, f"`gh pr view` must NOT request body; got {sorted(fields)}")
        expected = {"number", "title", "headRefName", "headRefOid", "baseRefName", "labels", "state",
                    "isCrossRepository", "headRepositoryOwner", "headRepository"}
        check(fields == expected, f"the view field set must be exactly the decision metadata; got {sorted(fields)}")


# --- fix 2: every gh command is scoped to project-root ------------------------

def t_gh_scoped_to_project_root():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        check(str(d) != os.getcwd(),
              "the test's project-root must differ from the invoking cwd for this check to have teeth")
        code, _, err, rec = _adopt(d, ledger, view(), wroot=d / "wt")
        check(code == 0, f"adopt succeeds (got {code}: {err})")
        ghs = rec.gh_calls()
        check(len(ghs) == 3, f"expected 3 gh calls (view, label create, pr edit); got {len(ghs)}")
        for c in ghs:
            check(c["cwd"] == str(d),
                  f"every gh call must run in project-root {d}, not the invoking cwd; "
                  f"{c['argv'][:3]} ran in {c['cwd']}")


# --- fix 4: re-adoption of the SAME worktree preserves created-ownership -------

def t_readopt_preserves_ownership():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        wt.mkdir(parents=True)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        # First adopt CREATED this worktree — owner=yes. Discovery finds the branch at that same path.
        _add_row(ledger, 12, head_sha=sha, worktree=str(wt), worktree_owned="yes", branch_owned="yes",
                 tier="HIGH", slug="fix-the-thing")
        checkouts = [(str(wt), "refs/heads/feat-x")]
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                 checkouts=checkouts)
        check(code == 0, f"unchanged-head re-adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "worktree_owned") == "yes",
              "re-adoption of the SAME worktree PRESERVES worktree_owned=yes (campaign created it)")
        check(_field(ledger, 12, "branch_owned") == "yes", "and preserves branch_owned=yes")
        # Teeth: a FIRST adoption of a genuinely pre-existing external checkout is no/no.
        ledger2 = d / "state2.jsonl"
        _init_ledger(ledger2)
        code2, _, err2, _ = _adopt(d, ledger2, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=checkouts)
        check(code2 == 0, f"first adopt of a pre-existing checkout succeeds (got {code2}: {err2})")
        check(_field(ledger2, 12, "worktree_owned") == "no",
              "a pre-existing external checkout is worktree_owned=no on FIRST adoption")
        check(_field(ledger2, 12, "branch_owned") == "no", "and branch_owned=no")


# --- fix 5: SHA-bound gate fields reset ONLY when the head moved ---------------

def t_readopt_unchanged_head_preserves_verdicts():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        wt.mkdir(parents=True)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=sha, worktree=str(wt), worktree_owned="yes", branch_owned="yes",
                 tier="HIGH")
        _record_verdict(ledger, 12, sha)
        _record_verdict(ledger, 12, sha)          # reviews_ok -> 2
        _set_row(ledger, 12, ci="green")
        check(_field(ledger, 12, "reviews_ok") == "2", "precondition: two SATISFIED verdicts recorded")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                 checkouts=[(str(wt), "refs/heads/feat-x")])
        check(code == 0, f"unchanged-head re-adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "reviews_ok") == "2",
              "an UNCHANGED head PRESERVES reviews_ok — the accumulated verdicts are not discarded")
        check(_field(ledger, 12, "ci") == "green", "an unchanged head preserves ci")


def t_readopt_changed_head_resets():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        old, new = "a" * 40, "b" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=old, tier="HIGH")          # no worktree dir -> create path
        _record_verdict(ledger, 12, old)
        _record_verdict(ledger, 12, old)
        # Stale liveness state describing the OLD head's CI, plus a green ci — all SHA-bound evidence.
        _set_row(ledger, 12, ci="green", ci_fingerprint="deadbeef", settled_strikes="2",
                 unusable_refetches="1", ci_stalled_since="2026-01-01T00:00:00Z")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=new)
        check(code == 0, f"changed-head re-adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "reviews_ok") == "0", "a MOVED head RESETS reviews_ok to 0")
        check(_field(ledger, 12, "ci") == "pending", "a moved head RESETS ci to pending")
        check(_field(ledger, 12, "head_sha") == new, "the row records the new head")
        check(_field(ledger, 12, "review_rounds") == "2",
              "review_rounds is MONOTONE — a re-adoption never resets the loop's memory")
        # The SHA-bound liveness counters describe the OLD head; a moved head resets every one of them to
        # its fresh-head default (ledger.py ROW_DEFAULTS). pr-adopt no longer hand-resets them — the ledger
        # `set --head-sha` DOOR does it (ledger.py's apply_head_sha), so this asserts the door's reset landed
        # through the ordinary refresh, IN THE SAME ledger update as head_sha.
        for field in M.LIVENESS_COUNTERS:
            check(_field(ledger, 12, field) == M.L.ROW_DEFAULTS[field],
                  f"a moved head RESETS the liveness counter {field} to its fresh-head default")
        # …and pr-adopt names the SET by re-exporting ledger's tuple — never a second copy of the list.
        check(M.LIVENESS_COUNTERS is M.L.LIVENESS_COUNTERS,
              "pr-adopt.LIVENESS_COUNTERS must BE ledger.LIVENESS_COUNTERS (one owner), not a duplicate")


def t_readopt_changed_head_preserves_held_status():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        old, new = "a" * 40, "b" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=old, tier="HIGH")
        _record_verdict(ledger, 12, old)
        _record_verdict(ledger, 12, old)          # reviews_ok=2 at the OLD head
        # The user then PARKED it: awaiting-user with a recorded ruling the park is waiting on.
        _set_row(ledger, 12, status="awaiting-user", blocker_ruling="retry@2026-01-01T00:00:00Z")
        check(_field(ledger, 12, "reviews_ok") == "2", "precondition: verdicts accrued before the park")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=new)
        check(code == 0, f"changed-head re-adopt of a held PR succeeds (got {code}: {err})")
        check(_field(ledger, 12, "status") == "awaiting-user",
              "a moved head PRESERVES status — it tracks a HUMAN decision, not the SHA, so re-adoption "
              "never silently un-holds a PR the user has not ruled on")
        check(_field(ledger, 12, "blocker_ruling") == "retry@2026-01-01T00:00:00Z",
              "and preserves the blocker_ruling the park is waiting on")
        # The SHA-bound EVIDENCE still resets even while status is held.
        check(_field(ledger, 12, "reviews_ok") == "0", "the moved head still resets reviews_ok")
        check(_field(ledger, 12, "ci") == "pending", "the moved head still resets ci")
        # And the PR is STILL HELD: dispatch-check must forbid ordinary gate work on it.
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            dc = M.L.main(["--file", str(ledger), "dispatch-check", "--pr", "12"])
        check(dc == M.L.EXIT_STOP,
              f"dispatch-check must still report the preserved awaiting-user row HELD "
              f"(exit {M.L.EXIT_STOP}); got {dc}")


# --- terminal rows are final — an OPEN PR cannot re-adopt them ----------------

def t_readopt_terminal_rows_refused():
    """Every ledger-owned terminal status refuses before any adoption mutation.

    Bailout may leave the PR OPEN after recording `aborted` and removing its labels. That missing label is
    not adoption work: the terminal row wins. Drive every member of ledger.py's one terminal-status set so
    adding a terminal status there automatically adds an executor fixture here.
    """
    for status in M.L.TERMINAL_STATUSES:
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            ledger = d / "state.jsonl"
            _init_ledger(ledger)
            _add_row(ledger, 12, head_sha="a" * 40, status=status, tier="HIGH")
            before = ledger.read_bytes()

            code, _, err, rec = _adopt(d, ledger, view(labels=[]), wroot=d / "wt")

            check(code != 0, f"an OPEN PR with terminal row status={status} must be REFUSED")
            check("terminal" in err.lower() and status in err,
                  f"the refusal must name terminal status={status}; got {err!r}")
            check(ledger.read_bytes() == before,
                  f"terminal status={status} must leave the ledger byte-identical")
            check([c["argv"][:3] for c in rec.gh_calls()] == [["gh", "pr", "view"]],
                  f"terminal status={status} must stop before label create/edit; got {rec.gh_calls()!r}")
            check(not rec.any_call(lambda a: a[0] == "git"),
                  f"terminal status={status} must stop before every worktree/git operation")
            check(not rec.any_call(lambda a: a[0] == "python3"),
                  f"terminal status={status} must stop before every ledger subprocess mutation")


# --- fix 6: the ONE status label mirrors the live gate, mutually exclusive -----

def t_readopt_accepted_unchanged_head_labels():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        wt.mkdir(parents=True)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=sha, worktree=str(wt), worktree_owned="yes", branch_owned="yes",
                 tier="HIGH")
        _record_verdict(ledger, 12, sha)
        _record_verdict(ledger, 12, sha)          # reviews_ok=2 == required(HIGH)
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(wt), "refs/heads/feat-x")])
        check(code == 0, f"accepted re-adopt succeeds (got {code}: {err})")
        e = rec.one("gh", "pr", "edit")
        check(e is not None, "a gh pr edit is issued")
        check(_labelset(e, "--add-label") == {"gauntlet-run-g1", "gauntlet-accepted"},
              f"an already-passed PR (gate met, unchanged head) ADDS run+accepted; got {e}")
        check(_labelset(e, "--remove-label") == {"gauntlet-reviewing"},
              f"and REMOVES the other status label in the same call; got {e}")


def t_readopt_changed_head_labels():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        old, new = "a" * 40, "b" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=old, tier="HIGH")
        _record_verdict(ledger, 12, old)
        _record_verdict(ledger, 12, old)          # was accepted at the OLD head
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=new)
        check(code == 0, f"changed-head re-adopt succeeds (got {code}: {err})")
        e = rec.one("gh", "pr", "edit")
        check(_labelset(e, "--add-label") == {"gauntlet-run-g1", "gauntlet-reviewing"},
              f"a moved head (reviews_ok reset) goes back under review; got {e}")
        check(_labelset(e, "--remove-label") == {"gauntlet-accepted"},
              f"and the stale gauntlet-accepted is removed in the same call; got {e}")


# --- fix 7: a reused/created worktree MUST match the planned head, else refuse -

def t_stale_local_branch_refused():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        old, new = "a" * 40, "b" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        # PR head advanced to NEW; a same-named LOCAL branch sits at OLD; no worktree dir yet (create path).
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot,
                                 worktree_head=old, local_branch_exists=True)
        check(code != 0, "a worktree created off a STALE local branch must be REFUSED — fail closed")
        check("stale" in err.lower(), f"the refusal must SAY the local branch was stale; got {err!r}")
        check(new in err and old in err, "the refusal names both the planned and the actual SHA")


def t_reused_worktree_sha_mismatch_refused():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        old, new = "a" * 40, "b" * 40
        wt = wroot / "feat-x"
        wt.mkdir(parents=True)                    # a reused checkout sitting at OLD
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _add_row(ledger, 12, head_sha=old, worktree=str(wt), worktree_owned="no", branch_owned="no",
                 tier="HIGH")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=old,
                                 checkouts=[(str(wt), "refs/heads/feat-x")])
        check(code != 0, "a reused worktree not at the planned head must be REFUSED — fail closed")
        check("stale" in err.lower(), f"the refusal must SAY the checkout is stale; got {err!r}")
        check(new in err and old in err, "the refusal names both the planned and the actual SHA")


# --- the worktree-discovery chokepoint: parse `git worktree list --porcelain -z` --------------------------

def t_worktree_for_branch_parser():
    z = _worktree_list_z([("/repo", "refs/heads/main"),
                          ("/repo/.worktrees/feat-x", "refs/heads/feat-x"),
                          ("/repo/.worktrees/detached", None),
                          ("/repo/.worktrees/other", "refs/heads/other")], "a" * 40)
    check(M.worktree_for_branch(z, "refs/heads/feat-x") == "/repo/.worktrees/feat-x",
          "an exact `branch refs/heads/<name>` entry resolves to its worktree path")
    check(M.worktree_for_branch(z, "refs/heads/main") == "/repo",
          "the branch checked out at the ROOT resolves to the root path")
    check(M.worktree_for_branch(z, "refs/heads/absent") is None,
          "a branch checked out nowhere resolves to None (create a worktree)")
    # A detached HEAD, or a differently-named branch, is NOT this branch's checkout.
    check(M.worktree_for_branch(z, "refs/heads/detached") is None,
          "a detached worktree never matches a branch ref — only `detached` was recorded, no branch line")
    check(M.worktree_for_branch("", "refs/heads/feat-x") is None, "empty listing -> None")


# --- fix: the WORKTREE is DISCOVERED, then reused-or-created across every cell -----------------------------
#
# Each cell drives the executor with a `checkouts` scenario and asserts the RECORDED worktree path is the
# DISCOVERED one (never blindly the default), and that a refusal touches nothing further.

BR = "refs/heads/feat-x"


def _worktree_added(rec) -> bool:
    return rec.any_call(lambda a: a[0] == "git" and "worktree" in a and "add" in a)


def t_reuse_at_root():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        # The PR branch is checked out at the PROJECT ROOT — reuse THAT path, never `git worktree add`.
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(d), BR)])
        check(code == 0, f"a branch checked out at the root adopts by REUSE (got {code}: {err})")
        check(_field(ledger, 12, "worktree") == str(d),
              "the RECORDED worktree is the discovered root path, not the default worktrees-root path")
        check(_field(ledger, 12, "worktree_owned") == "no", "a reused root checkout is worktree_owned=no")
        check(_field(ledger, 12, "branch_owned") == "no", "and branch_owned=no — campaign created neither")
        check(not _worktree_added(rec),
              "REUSE must NOT `git worktree add` (it exits 128 for a branch checked out elsewhere)")


def t_reuse_at_other_worktree():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        other = d / "somewhere-else" / "feat-x"
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(other), BR)])
        check(code == 0, f"a branch checked out at another worktree adopts by REUSE (got {code}: {err})")
        check(_field(ledger, 12, "worktree") == str(other),
              "the RECORDED worktree is the discovered other-worktree path")
        check(not _worktree_added(rec), "REUSE of another worktree must NOT `git worktree add`")


def t_reuse_default_clean():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"                      # the DEFAULT path — and it holds the branch, clean
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(wt), BR)])
        check(code == 0, f"a clean branch checkout at the default path is REUSED (got {code}: {err})")
        check(_field(ledger, 12, "worktree") == str(wt), "the recorded worktree is the default path")
        check(_field(ledger, 12, "worktree_owned") == "no",
              "a pre-existing checkout at the default path is still worktree_owned=no on first adoption")
        check(rec.any_call(lambda a: a[0] == "git" and "merge" in a and "--ff-only" in a),
              "a reused checkout is FAST-FORWARDED to the origin head before its SHA is verified")
        check(not _worktree_added(rec), "a reused default checkout must NOT `git worktree add`")


def t_detached_at_path_refused():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"                      # the default path is a DETACHED HEAD, not the branch
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        code, _, _, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                 checkouts=[(str(wt), None)])
        check(code != 0, "a DETACHED checkout at the default path must REFUSE — it is not the branch")
        check(rec.one("gh", "pr", "edit") is None, "a refusal touches nothing further — no gh pr edit")
        check(_field(ledger, 12, "worktree") == "-",
              "and the ledger worktree field is untouched (the row landed, the worktree never resolved)")


def t_different_branch_at_path_refused():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"                      # the default path holds a DIFFERENT branch
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        code, _, _, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                 checkouts=[(str(wt), "refs/heads/some-other-branch")])
        check(code != 0, "a DIFFERENT branch at the default path must REFUSE — the add hits an occupied path")
        check(rec.one("gh", "pr", "edit") is None, "a refusal touches nothing further — no gh pr edit")
        check(_field(ledger, 12, "worktree") == "-", "and the ledger worktree field is untouched")


def t_dirty_at_path_refused():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        # The branch IS checked out at the default path, but the tree is DIRTY.
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(wt), BR)], dirty=True)
        check(code != 0, "a DIRTY reused checkout must REFUSE — never adopt over uncommitted work")
        check("dirty" in err.lower(), f"the refusal must SAY the checkout is dirty; got {err!r}")
        check(rec.one("gh", "pr", "edit") is None, "a refusal touches nothing further — no gh pr edit")
        check(_field(ledger, 12, "worktree") == "-", "and the ledger worktree field is untouched")


def t_absent_creates_and_verifies():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        # No checkout of the branch ANYWHERE, and no local branch either -> create at the default path with -b.
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[], local_branch_exists=False)
        check(code == 0, f"an absent branch is CREATED at the default path (got {code}: {err})")
        check(_field(ledger, 12, "worktree") == str(wt), "the recorded worktree is the default path")
        check(_field(ledger, 12, "worktree_owned") == "yes", "a campaign-created worktree is worktree_owned=yes")
        check(_field(ledger, 12, "branch_owned") == "yes",
              "no pre-existing local branch -> campaign created it (branch_owned=yes, the -b path)")
        check(rec.any_call(lambda a: a[0] == "git" and "worktree" in a and "add" in a and "-b" in a),
              "the create path issues `git worktree add -b` from the fetched origin head")
        # Teeth: a pre-existing LOCAL branch (checked out nowhere) is reused -> branch_owned=no, no -b.
        ledger2 = d / "state2.jsonl"
        _init_ledger(ledger2)
        code2, _, err2, rec2 = _adopt(d, ledger2, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                      checkouts=[], local_branch_exists=True)
        check(code2 == 0, f"an absent-worktree but existing local branch is CREATED (got {code2}: {err2})")
        check(_field(ledger2, 12, "branch_owned") == "no",
              "a pre-existing local branch is reused (branch_owned=no), not recreated")
        check(rec2.any_call(lambda a: a[0] == "git" and "worktree" in a and "add" in a and "-b" not in a),
              "with a local branch present, the add is plain `worktree add <path> <branch>` (no -b)")


# --- mixed-base: the row RECORDS its live base at creation; a re-adoption mismatch PARKS ----------------

def t_adopt_records_row_base():
    """A fresh adoption RECORDS the PR's live `baseRefName` on the new row via `add-row --base-branch`.

    The base is per-row from creation (design: every new row writes an explicit base, even in a single-base
    run). It rides the plan under `base` and the executor writes it once; the plan row itself never carries it.
    """
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        # A NEW-run-style header (base_branch "-"); the row still records its own explicit base.
        _init_ledger(ledger, base_branch="-")
        code, _, err, rec = _adopt(d, ledger, view(baseRefName="v4"), wroot=d / "wt")
        check(code == 0, f"a clean adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "base_branch") == "v4",
              f"the new row must record its live baseRefName; got {_field(ledger, 12, 'base_branch')!r}")
        # The executor wrote it through add-row --base-branch (the CREATE_ONLY door), not a bare set.
        addrow = next((c["argv"] for c in rec.calls
                       if c["argv"][:1] == ["python3"] and "add-row" in c["argv"]), None)
        check(addrow is not None and "--base-branch" in addrow and addrow[addrow.index("--base-branch") + 1] == "v4",
              f"add-row must carry --base-branch v4; got {addrow!r}")


def t_readopt_base_mismatch_parks():
    """A re-adoption whose live base no longer matches the row's effective_base PARKS the row and stops.

    The recorded base is immutable; the campaign does not migrate it. pr-adopt parks through the existing
    machine-blocker transition with the EXACT reason, rewrites no base, and refreshes no SHA-bound evidence.
    """
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger, base_branch="main")        # header base main
        _add_row(ledger, 12, head_sha=sha, base_branch="v3", status="in_review", tier="HIGH")
        _record_verdict(ledger, 12, sha)                # reviews_ok -> 1 (evidence that must survive)
        # Live target is the view default `main`, which differs from the row's recorded `v3`.
        code, out, err, rec = _adopt(d, ledger, view(headRefOid=sha, baseRefName="main"),
                                     wroot=wroot, worktree_head=sha)
        check(code == 0, f"a base-mismatch re-adoption exits 0 after parking (got {code}: {err})")
        summary = json.loads(out)
        check(summary.get("parked") is True, f"the summary must report the park; got {summary!r}")
        check(_field(ledger, 12, "status") == "awaiting-user", "a base mismatch PARKS the row (awaiting-user)")
        check(_field(ledger, 12, "ci_reason") == "base changed from v3 to main; not supported mid-run",
              f"the durable park reason must be EXACT; got {_field(ledger, 12, 'ci_reason')!r}")
        check(_field(ledger, 12, "blocker_ruling") == "-", "the park clears blocker_ruling (park contract)")
        # The recorded base is NOT rewritten, and SHA-bound evidence is NOT refreshed.
        check(_field(ledger, 12, "base_branch") == "v3", "the recorded row base is immutable — never rewritten")
        check(_field(ledger, 12, "reviews_ok") == "1", "a parked mismatch refreshes no review evidence")
        # It stopped BEFORE touching any label — only the `gh pr view` (step 1) ran, no create/edit.
        ghs = [c["argv"][:3] for c in rec.gh_calls()]
        check(ghs == [["gh", "pr", "view"]],
              f"a parked mismatch applies no labels — only the pr view runs; got {ghs!r}")


def t_readopt_base_mismatch_already_held_keeps_question():
    """An ALREADY-held row keeps its open question — a base mismatch does not overwrite it.

    `ledger.py park` refuses a second park (EXIT_STOP) and leaves the existing `ci_reason` untouched; the
    executor reports the mismatch without disturbing the open blocker (design: held row keeps its question).
    """
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        ledger = d / "state.jsonl"
        _init_ledger(ledger, base_branch="main")
        _add_row(ledger, 12, head_sha=sha, base_branch="v3", tier="HIGH")
        # The row is already parked on a DIFFERENT question, with a ruling the park is waiting on.
        _set_row(ledger, 12, status="awaiting-user", ci_reason="CI has settled and is still not green",
                 blocker_ruling="retry@2026-01-01T00:00:00Z")
        code, out, err, _ = _adopt(d, ledger, view(headRefOid=sha, baseRefName="main"),
                                   wroot=wroot, worktree_head=sha)
        check(code == 0, f"an already-held mismatch exits 0 (got {code}: {err})")
        summary = json.loads(out)
        check(summary.get("already_held") is True and summary.get("parked") is False,
              f"the summary must report the row was already held; got {summary!r}")
        check(_field(ledger, 12, "ci_reason") == "CI has settled and is still not green",
              "the existing open question must be PRESERVED, not overwritten by the base mismatch")
        check(_field(ledger, 12, "blocker_ruling") == "retry@2026-01-01T00:00:00Z",
              "the pending ruling on the open park is untouched")


# --- CROSS-SCRIPT: one mixed-base run walks the real tools end to end -----------------------------------

def t_mixed_base_end_to_end():
    """A v3 row and a main row walk ONE ledger through the REAL tools, in sequence: adopt (pr-adopt) ->
    grouped required-set (ci-status) -> merge door (merge-check) -> park-on-retarget (reconcile + ledger
    park) -> distill (carryover). Each tool CONSUMES the ledger the previous one wrote.

    The isolated suites each build their OWN synthetic ledger, so only this fixture proves the row
    `base_branch` that `pr-adopt` writes at admission is the same value every downstream base door resolves
    through `effective_base` — the whole point of mixed-base admission. It exercises the composition, not any
    one tool's internal rules (those stay pinned by their own suites)."""
    CI = _sibling("mbi_ci_status", "ci-status.py")
    RC = _sibling("mbi_reconcile", "reconcile.py")
    MC = _sibling("mbi_merge_check", "merge-check.py")
    CO = _sibling("mbi_carryover", "carryover.py")
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        ledger = d / "state.jsonl"
        run_id = "g1"
        # A NEW-run header: the base is per-row now, so `base_branch` stays its `-` default and
        # `required_set` stays `unknown` — never a run-wide value (design: "Resolve row-owned state").
        _init_ledger(ledger, run_id=run_id, base_branch="-")

        # 1. ADMIT two PRs on DIFFERENT bases into ONE run — no common-base agreement required.
        sha_v3, sha_main = "a" * 40, "b" * 40
        v_v3 = view(number=41, headRefName="fix-v3", headRefOid=sha_v3, baseRefName="v3", title="v3 fix")
        v_main = view(number=52, headRefName="fix-main", headRefOid=sha_main, baseRefName="main",
                      title="main fix")
        code, _, err, _ = _adopt(d, ledger, v_v3, wroot=wroot, run_id=run_id)
        check(code == 0, f"adopt of the v3 PR succeeds (got {code}: {err})")
        code, _, err, _ = _adopt(d, ledger, v_main, wroot=wroot, run_id=run_id)
        check(code == 0, f"adopt of the main PR succeeds in the SAME run (got {code}: {err})")
        check(_field(ledger, 41, "base_branch") == "v3" and _field(ledger, 52, "base_branch") == "main",
              "each row records its OWN live base — mixed bases admitted into one run")
        header_after, _ = M.L.load(ledger)
        check(header_after["base_branch"] == "-",
              "the run header base stays `-` — the base is per-row, never a run-wide header value")

        # 2. GROUPED required-set (ci-status): one GitHub read per DISTINCT base, written to that base's rows.
        v3_set = CI.canonical_required_set([("v3-test", CI.SNAP.ANY_APP)])
        main_set = CI.canonical_required_set([("main-test", CI.SNAP.ANY_APP)])
        seen: list = []

        def fetch(source: str, argv: list) -> object:
            endpoint = str(argv[-1])
            base = "v3" if endpoint.endswith(CI.quote("v3", safe="")) else (
                "main" if endpoint.endswith(CI.quote("main", safe="")) else None)
            if base is None:
                raise CI.FetchError(f"unexpected endpoint {endpoint!r}")
            if source.endswith("classic"):
                seen.append(base)
                ctx = "v3-test" if base == "v3" else "main-test"
                return {"protection": {"enabled": True,
                        "required_status_checks": {"checks": [{"context": ctx, "app_id": None}]}}}
            return [[]]   # ruleset: one empty page

        out = CI.refresh_required_set(fetch, ledger, "o/r")
        check(out["settled"], f"both base groups settle: {out!r}")
        check(sorted(seen) == ["main", "v3"], f"each DISTINCT base is read exactly once: {seen!r}")
        check(_field(ledger, 41, "required_set") == v3_set, "the v3 row gets v3's required set")
        check(_field(ledger, 52, "required_set") == main_set, "the main row gets main's required set")
        header_now, _ = M.L.load(ledger)
        check(header_now["required_set"] == "unknown",
              "the new-run header required_set stays unknown — never materialized from a base group")

        # 3. MERGE DOOR (merge-check) resolves the SELECTED row's effective base. The v3 row seen live on
        #    `main` is an unsupported retarget -> the shared park reason; seen live on `v3` it clears the base
        #    step (and stops later only because its CI is still pending — proof the base door PASSED).
        hdr, rows = M.L.load(ledger)
        r41 = M.L.find_row(rows, "41")
        assert r41 is not None
        eff41 = M.L.effective_base(hdr, r41)
        check(eff41 == "v3", f"the merge door resolves #41's effective base to v3; got {eff41!r}")

        def mview(base_ref: str) -> dict:
            return {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "isDraft": False,
                    "state": "OPEN", "headRefOid": sha_v3, "baseRefName": base_ref}

        retarget = MC.decide(r41, mview("main"), required=MC.REQUIRED, effective_base=eff41)
        check(retarget["reason"] == M.BASE_CHANGE_PARK_REASON.format(recorded="v3", live="main"),
              f"a live retarget parks with the SHARED base-change reason; got {retarget!r}")
        onbase = MC.decide(r41, mview("v3"), required=MC.REQUIRED, effective_base=eff41)
        check("ci is pending" in onbase["reason"],
              f"on its own base the retarget check passes and decision proceeds; got {onbase!r}")

        # 4. PARK ON RETARGET (reconcile detects, ledger parks). Snapshot: #41 now targets `main`, #52 still
        #    `main`. reconcile flags #41 against its RECORDED v3; #52 on its own base is clean.
        prs = d / "prs.json"
        run_label = M.RUN_LABEL_PREFIX + run_id

        def entry(number: int, branch: str, base: str, head: str) -> dict:
            return {"number": number, "headRefName": branch, "headRefOid": head, "title": f"t{number}",
                    "baseRefName": base, "state": "OPEN", "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "labels": [{"name": run_label}, {"name": M.REVIEWING_LABEL}]}

        prs.write_text(json.dumps([entry(41, "fix-v3", "main", sha_v3),
                                   entry(52, "fix-main", "main", sha_main)]), encoding="utf-8")
        facts = RC.detect(ledger, prs, run_id)
        f41 = facts["rows"]["41"]
        check(f41.get("base_changed") == {"ledger": "v3", "snapshot": "main"},
              f"reconcile flags #41's retarget against its RECORDED v3, not the header: {f41!r}")
        check("base_changed" not in facts["rows"]["52"],
              f"the main PR on its own base is NOT flagged: {facts['rows']['52']!r}")
        park_reason = M.BASE_CHANGE_PARK_REASON.format(recorded=f41["base_changed"]["ledger"],
                                                       live=f41["base_changed"]["snapshot"])
        _ledger("--file", str(ledger), "park", "--pr", "41", "--reason", park_reason)
        check(_field(ledger, 41, "status") == "awaiting-user", "the retargeted row is PARKED")
        check(_field(ledger, 41, "ci_reason") == "base changed from v3 to main; not supported mid-run",
              f"the durable park reason is EXACT; got {_field(ledger, 41, 'ci_reason')!r}")
        check(_field(ledger, 41, "base_branch") == "v3",
              "the recorded base is immutable — a park never rewrites it")

        # 5. DISTILL (carryover v2): each terminal object carries its OWN base; metadata lists both, sorted.
        _set_row(ledger, 41, status="aborted")
        _set_row(ledger, 52, status="merged")
        out_dir = d / "history"
        summary = CO.distill(M.L, ledger, out_dir, "2026-07-22T00:00:00Z", force=False)
        check(summary["base_branches"] == ["main", "v3"],
              f"carryover v2 records the run's distinct bases, sorted: {summary!r}")
        text = (out_dir / f"{run_id}.md").read_text(encoding="utf-8")
        check("base_branches: [\"main\", \"v3\"]" in text,
              f"the distilled v2 metadata names both release lines: {text!r}")
# --- fu61: `intent-sync` folds the run's default Non-goals into a PR's intent managed block ------------
#
# The mechanical fold: pr-adopt reads defaults ONLY through `L.default_non_goals` and rewrites the managed
# block ONLY through `RP.merge_default_non_goals` (both exercised here for real), then writes atomically.
# The block's own format/idempotency rules are pinned in review-pass-test; these pin pr-adopt's boundary —
# derive the sibling path, refuse a missing row / unusable intent without writing, and report the outcome.

INTENT_BASE = (
    "# What this PR is for\n\n"
    "## Purpose\n- do the thing\n\n"
    "## Non-goals\n- a pr specific exclusion\n\n"
    "## Threat model\n- Who can write the inputs this code reads: the network\n"
)


def _set_defaults(ledger: Path, *bodies: str) -> None:
    _ledger("--file", str(ledger), "header", "set", "default_non_goals", json.dumps(list(bodies)))


def _write_intent(ledger: Path, pr, text: str = INTENT_BASE) -> Path:
    p = M.intent_path(str(ledger), str(pr))
    p.write_text(text, encoding="utf-8")
    return p


def _intent_sync(ledger: Path, pr):
    return capture_cli(M.main, ["intent-sync", "--file", str(ledger), "--pr", str(pr)])


def _managed(text: str) -> "list[str]":
    """The run-default bullets inside the managed block, via the review-pass scanner the tool itself uses."""
    return M.RP.scan_managed_block(text, Path("intent.md")).bullets


def t_intent_sync_inserts_and_preserves():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A", "run default B")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        intent = _write_intent(ledger, 12)
        code, out, err = _intent_sync(ledger, 12)
        check(code == 0, f"a fresh intent-sync succeeds (got {code}: {err})")
        check(json.loads(out)["intent_sync"] == "updated", f"the summary must report `updated`: {out}")
        text = intent.read_text()
        check(_managed(text) == ["run default A", "run default B"],
              f"both run defaults must land in the managed block; got {_managed(text)!r}")
        check("- a pr specific exclusion" in text, "the PR-specific Non-goal must be preserved untouched")


def t_intent_sync_dedup():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "a pr specific exclusion", "run default A")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        intent = _write_intent(ledger, 12)
        code, _, err = _intent_sync(ledger, 12)
        check(code == 0, f"intent-sync succeeds (got {code}: {err})")
        text = intent.read_text()
        check(_managed(text) == ["run default A"],
              f"a default already stated as a PR-specific Non-goal is NOT duplicated; got {_managed(text)!r}")
        check(text.count("a pr specific exclusion") == 1, "the shared bullet appears exactly once")


def t_intent_sync_idempotent():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A", "run default B")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        intent = _write_intent(ledger, 12)
        _intent_sync(ledger, 12)
        first = intent.read_text()
        code, out, err = _intent_sync(ledger, 12)
        check(code == 0, f"a second intent-sync succeeds (got {code}: {err})")
        check(json.loads(out)["intent_sync"] == "unchanged",
              f"the second sync must report `unchanged`: {out}")
        check(intent.read_text() == first, "a second intent-sync is BYTE-IDENTICAL")


def t_intent_sync_change_replaces():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A", "run default B")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        intent = _write_intent(ledger, 12)
        _intent_sync(ledger, 12)
        # Change the header: A is dropped, C is added; the sync must REPLACE the managed block, not append.
        _set_defaults(ledger, "run default B", "run default C")
        code, out, err = _intent_sync(ledger, 12)
        check(code == 0, f"the re-sync succeeds (got {code}: {err})")
        check(json.loads(out)["intent_sync"] == "updated", "changing the header updates the block")
        check(_managed(intent.read_text()) == ["run default B", "run default C"],
              f"the block must reflect the NEW defaults exactly; got {_managed(intent.read_text())!r}")


def t_intent_sync_empty_removes():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        intent = _write_intent(ledger, 12)
        _intent_sync(ledger, 12)
        check(M.RP.MANAGED_START in intent.read_text(), "precondition: the managed block was inserted")
        _set_defaults(ledger)  # empty defaults
        code, _, err = _intent_sync(ledger, 12)
        check(code == 0, f"emptying the defaults syncs cleanly (got {code}: {err})")
        text = intent.read_text()
        check(M.RP.MANAGED_START not in text and M.RP.MANAGED_END not in text,
              "an empty default list removes the managed block entirely")
        check("- a pr specific exclusion" in text,
              "removing the managed block leaves the PR-specific Non-goals untouched")


def t_intent_sync_pending_intent():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        # No intent artifact authored yet — a visible incomplete adoption, not a failure.
        code, out, err = _intent_sync(ledger, 12)
        check(code == 0, f"a missing intent is reported, not an error (got {code}: {err})")
        check(json.loads(out)["intent_sync"] == "pending-intent",
              f"the summary must report `pending-intent`: {out}")
        check(not M.intent_path(str(ledger), 12).exists(), "intent-sync must not CREATE the intent artifact")


def t_intent_sync_refuses_missing_row():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A")
        intent = _write_intent(ledger, 12)  # an intent exists, but no ledger row does
        before = intent.read_text()
        code, _, err = _intent_sync(ledger, 12)
        check(code == 1, f"a missing row must be REFUSED (got {code})")
        check("no ledger row" in err, f"the refusal must name the missing row: {err!r}")
        check(intent.read_text() == before, "a refused sync must not write the intent")


def t_intent_sync_refuses_unusable_intent():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A")
        _add_row(ledger, 12, head_sha="a" * 40, tier="HIGH")
        # missing ## Threat model, and a duplicated managed block — either makes the base intent unusable.
        bad = "## Purpose\n- do the thing\n\n## Non-goals\n- x\n"
        intent = _write_intent(ledger, 12, bad)
        code, _, err = _intent_sync(ledger, 12)
        check(code == 1, f"an unusable base intent must be REFUSED (got {code})")
        check("not a usable intent block" in err, f"the refusal must say the intent is unusable: {err!r}")
        check(intent.read_text() == bad, "a refused sync must not rewrite an unusable intent")

        dup = (INTENT_BASE.replace("- a pr specific exclusion\n",
                                   "- a pr specific exclusion\n" + M.RP.MANAGED_START + "\n- one\n"
                                   + M.RP.MANAGED_END + "\n" + M.RP.MANAGED_START + "\n- two\n"
                                   + M.RP.MANAGED_END + "\n"))
        intent.write_text(dup, encoding="utf-8")
        code, _, err = _intent_sync(ledger, 12)
        check(code == 1, f"a malformed managed block must be REFUSED (got {code})")
        check(intent.read_text() == dup, "a refused sync must not rewrite a malformed managed block")


def t_adopt_readoption_invokes_sync():
    """cmd_adopt runs intent-sync automatically: a re-adoption whose intent artifact is present gets its
    managed block folded, and the summary reports it."""
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        wroot = d / "wt"
        sha = "a" * 40
        wt = wroot / "feat-x"
        wt.mkdir(parents=True)
        ledger = d / "state.jsonl"
        _init_ledger(ledger)
        _set_defaults(ledger, "run default A")
        _add_row(ledger, 12, head_sha=sha, worktree=str(wt), worktree_owned="yes", branch_owned="yes",
                 tier="HIGH")
        intent = _write_intent(ledger, 12)
        code, out, err, _ = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                   checkouts=[(str(wt), "refs/heads/feat-x")])
        check(code == 0, f"re-adoption succeeds (got {code}: {err})")
        check(json.loads(out)["intent_sync"] == "updated",
              f"cmd_adopt must report the intent-sync outcome: {out}")
        check(_managed(intent.read_text()) == ["run default A"],
              "re-adoption folds the run default into the present intent artifact")
        # …and a FRESH adoption (no intent yet) reports pending-intent, authoring nothing. Its ledger sits
        # in its OWN run dir so the first adoption's intent-12.md is not a sibling of it.
        run2 = d / "run2"
        run2.mkdir()
        ledger2 = run2 / "state.jsonl"
        _init_ledger(ledger2)
        _set_defaults(ledger2, "run default A")
        code2, out2, err2, _ = _adopt(d, ledger2, view(headRefOid=sha), wroot=wroot, worktree_head=sha,
                                      checkouts=[(str(wt), "refs/heads/feat-x")])
        check(code2 == 0, f"fresh adoption succeeds (got {code2}: {err2})")
        check(json.loads(out2)["intent_sync"] == "pending-intent",
              f"a fresh adoption with no intent reports pending-intent: {out2}")


CASES = [
    ("mixed_base_end_to_end", "one mixed-base run walks adopt -> grouped required-set -> merge door -> "
                              "reconcile park -> distill on ONE ledger", t_mixed_base_end_to_end),
    ("refuse_fork", "a fork PR is refused, fail closed", t_refuse_fork),
    ("refuse_foreign_run", "a PR owned by another run is refused", t_refuse_foreign_run),
    ("refuse_closed", "a MERGED/CLOSED PR is refused", t_refuse_closed),
    ("adopt_same_repo", "a clean same-repo OPEN PR adopts with the computed row", t_adopt_same_repo),
    ("adopt_when_already_ours", "a PR already carrying our run label re-adopts", t_adopt_when_already_ours),
    ("slugify", "slugify yields a lowercase, dash-collapsed, untrimmed-dash-free slug", t_slugify),
    ("cli_plan_refuses_fork", "the plan CLI prints a refuse verdict and exits 0", t_cli_plan_refuses_fork),
    ("cli_plan_adopts", "the plan CLI prints an adopt verdict and exits 0", t_cli_plan_adopts),
    ("pr_origin_from_label", "pr_origin is DERIVED from the gauntlet-authored label (fix 3)", t_pr_origin_from_label),
    ("driver_cannot_assert_origin", "the --pr-origin flag is gone; a driver cannot claim gauntlet (fix 3)", t_driver_cannot_assert_origin),
    ("view_omits_body", "`gh pr view` requests metadata only, never body (fix 1)", t_view_omits_body),
    ("gh_scoped_to_project_root", "every gh command runs in project-root (fix 2)", t_gh_scoped_to_project_root),
    ("readopt_preserves_ownership", "re-adopting the same worktree keeps created-ownership (fix 4)", t_readopt_preserves_ownership),
    ("readopt_unchanged_head_preserves_verdicts", "an unchanged head keeps reviews_ok/ci (fix 5)", t_readopt_unchanged_head_preserves_verdicts),
    ("readopt_changed_head_resets", "a moved head resets reviews_ok/ci and the liveness counters, review_rounds stays (fix 5)", t_readopt_changed_head_resets),
    ("readopt_changed_head_preserves_held_status", "a moved head preserves a held status and stays HELD, resetting only SHA-bound evidence", t_readopt_changed_head_preserves_held_status),
    ("readopt_terminal_rows_refused", "every terminal row refuses before label, ledger, or worktree mutation", t_readopt_terminal_rows_refused),
    ("readopt_accepted_unchanged_head_labels", "accepted+unchanged head keeps gauntlet-accepted, mutually exclusive (fix 6)", t_readopt_accepted_unchanged_head_labels),
    ("readopt_changed_head_labels", "a moved head returns to gauntlet-reviewing, removes accepted (fix 6)", t_readopt_changed_head_labels),
    ("stale_local_branch_refused", "a worktree off a stale local branch is refused (fix 7)", t_stale_local_branch_refused),
    ("reused_worktree_sha_mismatch_refused", "a reused worktree not at the planned head is refused (fix 7)", t_reused_worktree_sha_mismatch_refused),
    ("worktree_for_branch_parser", "the discovery chokepoint parses `git worktree list --porcelain -z` for the exact branch", t_worktree_for_branch_parser),
    ("reuse_at_root", "a branch checked out at the ROOT is reused, no worktree add", t_reuse_at_root),
    ("reuse_at_other_worktree", "a branch checked out at another worktree is reused at its discovered path", t_reuse_at_other_worktree),
    ("reuse_default_clean", "a clean checkout at the default path is reused, fast-forwarded, SHA-verified", t_reuse_default_clean),
    ("detached_at_path_refused", "a DETACHED HEAD at the default path fails closed, touches nothing further", t_detached_at_path_refused),
    ("different_branch_at_path_refused", "a DIFFERENT branch at the default path fails closed", t_different_branch_at_path_refused),
    ("dirty_at_path_refused", "a DIRTY reused checkout fails closed, touches nothing further", t_dirty_at_path_refused),
    ("absent_creates_and_verifies", "an absent branch is created (with/without a pre-existing local branch), SHA-verified", t_absent_creates_and_verifies),
    ("adopt_records_row_base", "a fresh adoption records the live base on the new row (add-row --base-branch)", t_adopt_records_row_base),
    ("readopt_base_mismatch_parks", "a re-adoption whose live base diverges from effective_base parks the row, exact reason, no rewrite", t_readopt_base_mismatch_parks),
    ("readopt_base_mismatch_already_held", "an already-held row keeps its open question on a base mismatch", t_readopt_base_mismatch_already_held_keeps_question),
    ("intent_sync_inserts_and_preserves", "intent-sync folds all run defaults in, preserving PR-specific Non-goals (fu61)", t_intent_sync_inserts_and_preserves),
    ("intent_sync_dedup", "a default already stated as a PR-specific Non-goal is not duplicated (fu61)", t_intent_sync_dedup),
    ("intent_sync_idempotent", "a second intent-sync is byte-identical and reports unchanged (fu61)", t_intent_sync_idempotent),
    ("intent_sync_change_replaces", "changing the header replaces the managed block, adding and removing defaults (fu61)", t_intent_sync_change_replaces),
    ("intent_sync_empty_removes", "empty defaults remove the managed block without touching PR-specific Non-goals (fu61)", t_intent_sync_empty_removes),
    ("intent_sync_pending_intent", "a missing intent is reported pending-intent, never authored (fu61)", t_intent_sync_pending_intent),
    ("intent_sync_refuses_missing_row", "a missing ledger row is refused without writing (fu61)", t_intent_sync_refuses_missing_row),
    ("intent_sync_refuses_unusable_intent", "an unusable or malformed-managed intent is refused without writing (fu61)", t_intent_sync_refuses_unusable_intent),
    ("adopt_readoption_invokes_sync", "cmd_adopt runs intent-sync: re-adoption folds, fresh adoption reports pending-intent (fu61)", t_adopt_readoption_invokes_sync),
]
