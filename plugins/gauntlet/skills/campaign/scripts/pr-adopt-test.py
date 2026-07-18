#!/usr/bin/env python3
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
    check(p["base"] == "main", "base == baseRefName (a HEADER field, carried under `base`)")
    # teeth: base/worktree/ownership flags are NOT row fields — they are the caller's or step 5's
    check("base" not in row and "base_branch" not in row and "worktree" not in row,
          "base/base_branch/worktree must NOT be written as row fields")
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
# answers `git` from the scenario knobs. That pins the executor's gh scoping, its worktree SHA checks, and
# its ledger writes offline, with no live GitHub and no real git repo.

def _ledger(*args) -> int:
    """Run the real ledger main quietly (its row JSON would otherwise pollute the self-test output)."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return M.L.main(list(args))


def _init_ledger(ledger: Path, run_id: str = "g1") -> None:
    _ledger("--file", str(ledger), "header", "set", "run_id", run_id)


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
    _ledger("--file", str(ledger), "verdict", "--pr", str(pr), "--head-sha", head_sha, "--verdict", verdict)


def _field(ledger: Path, pr, name):
    _, rows = M.L.load(ledger)
    row = M.L.find_row(rows, str(pr))
    return row[name] if row else None


def _labelset(argv, flag):
    return {argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)}


class Recorder:
    def __init__(self, *, view, worktree_head=None, local_branch_exists=False):
        self.view = view
        self.calls: list = []
        self.worktree_head = worktree_head if worktree_head is not None else view["headRefOid"]
        self.local_branch_exists = local_branch_exists

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
            if sub == "show-ref":
                return CompletedProcess(argv, 0 if self.local_branch_exists else 1, "", "")
            if sub == "rev-parse":
                return CompletedProcess(argv, 0, self.worktree_head + "\n", "")
            return CompletedProcess(argv, 0, "", "")  # fetch / worktree add
        return CompletedProcess(argv, 0, "", "")

    def gh_calls(self):
        return [c for c in self.calls if c["argv"][0] == "gh"]

    def one(self, *prefix):
        for c in self.calls:
            if c["argv"][: len(prefix)] == list(prefix):
                return c["argv"]
        return None


def _adopt(d: Path, ledger: Path, v: dict, *, wroot: Path, worktree_head=None,
           local_branch_exists=False, tier="HIGH", run_id="g1", repo=None):
    rec = Recorder(view=v, worktree_head=worktree_head, local_branch_exists=local_branch_exists)
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
        # First adopt CREATED this worktree — owner=yes.
        _add_row(ledger, 12, head_sha=sha, worktree=str(wt), worktree_owned="yes", branch_owned="yes",
                 tier="HIGH", slug="fix-the-thing")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha)
        check(code == 0, f"unchanged-head re-adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "worktree_owned") == "yes",
              "re-adoption of the SAME worktree PRESERVES worktree_owned=yes (campaign created it)")
        check(_field(ledger, 12, "branch_owned") == "yes", "and preserves branch_owned=yes")
        # Teeth: a FIRST adoption of a genuinely pre-existing external checkout is no/no.
        ledger2 = d / "state2.jsonl"
        _init_ledger(ledger2)
        code2, _, err2, _ = _adopt(d, ledger2, view(headRefOid=sha), wroot=wroot, worktree_head=sha)
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
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha)
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
        _set_row(ledger, 12, ci="green")
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=new)
        check(code == 0, f"changed-head re-adopt succeeds (got {code}: {err})")
        check(_field(ledger, 12, "reviews_ok") == "0", "a MOVED head RESETS reviews_ok to 0")
        check(_field(ledger, 12, "ci") == "pending", "a moved head RESETS ci to pending")
        check(_field(ledger, 12, "head_sha") == new, "the row records the new head")
        check(_field(ledger, 12, "review_rounds") == "2",
              "review_rounds is MONOTONE — a re-adoption never resets the loop's memory")


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
        code, _, err, rec = _adopt(d, ledger, view(headRefOid=sha), wroot=wroot, worktree_head=sha)
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
        code, _, err, _ = _adopt(d, ledger, view(headRefOid=new), wroot=wroot, worktree_head=old)
        check(code != 0, "a reused worktree not at the planned head must be REFUSED — fail closed")
        check("stale" in err.lower(), f"the refusal must SAY the checkout is stale; got {err!r}")
        check(new in err and old in err, "the refusal names both the planned and the actual SHA")


CASES = [
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
    ("readopt_changed_head_resets", "a moved head resets reviews_ok/ci, review_rounds stays (fix 5)", t_readopt_changed_head_resets),
    ("readopt_accepted_unchanged_head_labels", "accepted+unchanged head keeps gauntlet-accepted, mutually exclusive (fix 6)", t_readopt_accepted_unchanged_head_labels),
    ("readopt_changed_head_labels", "a moved head returns to gauntlet-reviewing, removes accepted (fix 6)", t_readopt_changed_head_labels),
    ("stale_local_branch_refused", "a worktree off a stale local branch is refused (fix 7)", t_stale_local_branch_refused),
    ("reused_worktree_sha_mismatch_refused", "a reused worktree not at the planned head is refused (fix 7)", t_reused_worktree_sha_mismatch_refused),
]
