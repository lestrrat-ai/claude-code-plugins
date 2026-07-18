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
import tempfile
from pathlib import Path

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
    """A clean, same-repo, OPEN `gh pr view` payload; override any field with a keyword."""
    base = {
        "number": 12,
        "title": "Fix the thing",
        "body": "## Purpose\n- fix\n## Non-goals\n## Threat model\n- GitHub API",
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
    args = {"run_id": "g1", "tier": "HIGH", "pr_origin": "external", "worktrees_root": "/wt"}
    args.update({k: kw.pop(k) for k in list(kw) if k in args})
    return M.build_plan(view(**kw), **args)


# --- refusals (fail closed) ---------------------------------------------------

def t_refuse_fork():
    p = M.build_plan(view(isCrossRepository=True), run_id="g1", tier="HIGH",
                     pr_origin="external", worktrees_root="/wt")
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
                     run_id="g1", tier="HIGH", pr_origin="external", worktrees_root="/wt")
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


CASES = [
    ("refuse_fork", "a fork PR is refused, fail closed", t_refuse_fork),
    ("refuse_foreign_run", "a PR owned by another run is refused", t_refuse_foreign_run),
    ("refuse_closed", "a MERGED/CLOSED PR is refused", t_refuse_closed),
    ("adopt_same_repo", "a clean same-repo OPEN PR adopts with the computed row", t_adopt_same_repo),
    ("adopt_when_already_ours", "a PR already carrying our run label re-adopts", t_adopt_when_already_ours),
    ("slugify", "slugify yields a lowercase, dash-collapsed, untrimmed-dash-free slug", t_slugify),
    ("cli_plan_refuses_fork", "the plan CLI prints a refuse verdict and exits 0", t_cli_plan_refuses_fork),
    ("cli_plan_adopts", "the plan CLI prints an adopt verdict and exits 0", t_cli_plan_adopts),
]
