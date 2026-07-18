#!/usr/bin/env python3
"""Adopt a PR into a run — the MECHANICAL half, as a command instead of a shell block in a doc.

`references/pr-adoption.md` is the authority; this tool performs its steps 1, 2, 4, 5 and the row of
step 3. It does NOT decide the review TIER (that is a triage judgment — passed in as `--tier`) and it does
NOT author the PR's INTENT (step 3a — the driver's working note about what the PR is for). Those two are
JUDGMENT; everything here is MECHANICS that a model transcribing a doc gets subtly wrong under load:

  * READ the PR (one `gh pr view` for the fields the ledger row needs, including the cross-repo field);
  * REFUSE fork/foreign/closed PRs — FAIL CLOSED, touching nothing when it refuses (step 2);
  * REGISTER the ledger row (refresh in place if it exists, never a duplicate `add-row`) (step 3, row);
  * CREATE-OR-REUSE the PR-head worktree (step 5);
  * LABEL the PR ours + under review (step 4).

The decision logic lives in a PURE `build_plan()` (and a pure `slugify()`), so every refusal and every
computed row field is pinned by an offline fixture with no live GitHub — the sibling `pr-adopt-test.py` is
this tool's executable contract. `adopt` is the thin executor that runs the real `gh`/`git`/ledger
commands around that plan.

  pr-adopt.py plan  --view-json <f> --run-id <id> --tier <T>   # PURE: parse a view, print the plan, exit 0
  pr-adopt.py adopt --pr <N> --run-id <id> --file <state.jsonl> --tier <T> \
                    --worktrees-root <p> --project-root <p>    # the real thing: gh + git + ledger

pr-adopt performs NO review and NO merge. It gets a PR INTO the run; Loop control drives it from there.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = next(iter((__doc__ or "").splitlines()), "")

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "pr-adopt-test.py"
LEDGER_PY = _HERE / "ledger.py"

# The run owner label's prefix and the shared "under review" status label. The owner label's colour
# matches the one pr-adoption.md's `gh label create` uses, so an idempotent `--force` create never churns
# it.
RUN_LABEL_PREFIX = "gauntlet-run-"
REVIEWING_LABEL = "gauntlet-reviewing"
RUN_LABEL_COLOR = "5319E7"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


L = _load("pr_adopt_ledger", "ledger.py")


# --- pure decision surface ----------------------------------------------------

def slugify(title: str) -> str:
    """A filesystem-safe slug: lowercase, every run of non-alphanumerics collapsed to one `-`, no leading
    or trailing dash. An all-punctuation title slugs to the empty string, which is a fine `-`-free value."""
    out: list[str] = []
    prev_dash = False
    for ch in title.lower():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _fork_ref(view: dict) -> str:
    """`<owner>/<repo>` for the fork a cross-repo PR's head lives in, read defensively from the two
    objects `gh pr view` returns (each an object with `login`/`name`, or already a bare string)."""
    owner = view.get("headRepositoryOwner")
    repo = view.get("headRepository")
    owner_s = owner.get("login") if isinstance(owner, dict) else owner
    repo_s = repo.get("name") if isinstance(repo, dict) else repo
    return f"{owner_s}/{repo_s}"


def build_plan(view: dict, *, run_id: str, tier: str, pr_origin: str, worktrees_root: str) -> dict:
    """Decide adoptability and compute the row — PURE, from a parsed `gh pr view` dict.

    Returns `{"verdict": "refuse", "reason": ...}` for a PR that must NOT be adopted (pr-adoption.md
    step 2), else `{"verdict": "adopt", "row": {...computed fields...}, "labels_add": [...], "branch": ...,
    "worktree": ..., "base": ...}`. FAIL CLOSED: every refusal is checked before any adopt field is built.
    """
    # A fork PR is untrusted, attacker-controllable content this autonomous pipeline would read and act on,
    # and it has no push target for fix commits — campaign gates SAME-REPO PRs only (step 2).
    if view.get("isCrossRepository") is True:
        return {"verdict": "refuse",
                "reason": f"fork PR (head in {_fork_ref(view)}) — campaign gates same-repo PRs only; "
                          f"push a same-repo branch"}

    # A `gauntlet-run-*` label that is not OURS means another run owns this PR — never steal its label.
    ours = f"{RUN_LABEL_PREFIX}{run_id}"
    for lbl in view.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str) and name.startswith(RUN_LABEL_PREFIX) and name != ours:
            return {"verdict": "refuse", "reason": f"owned by another run ({name})"}

    # Campaign gates OPEN PRs; a merged/closed PR is terminal, not adoptable.
    state = view.get("state")
    if state != "OPEN":
        return {"verdict": "refuse", "reason": f"PR is {state}, not open"}

    branch = view["headRefName"]
    # COMPUTED fields only. `base_branch` is a HEADER field, not a row field, so `baseRefName` rides the
    # plan under `base` for the caller and is never written as a row field. `worktree_owned`/`branch_owned`
    # are decided at worktree creation (step 5), never here.
    row = {
        "head_sha": view["headRefOid"],
        "tier": tier,
        "ci": "pending",
        "status": "in_review",
        "reviews_ok": "0",
        "pr_origin": pr_origin,
        "slug": slugify(str(view.get("title", ""))),
    }
    return {
        "verdict": "adopt",
        "row": row,
        "labels_add": [ours, REVIEWING_LABEL],
        "branch": branch,
        "worktree": str(Path(worktrees_root) / branch),
        "base": view["baseRefName"],
    }


# --- executor -----------------------------------------------------------------

def _run(argv: list[str], *, cwd: "str | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)  # noqa: S603


def _refuse(reason: str) -> int:
    print(f"pr-adopt: REFUSED — {reason}", file=sys.stderr)
    return 1


def cmd_plan(args) -> int:
    """The TESTABLE surface: read a parsed view from disk, print the plan, exit 0 (refuse or adopt alike).
    No `gh`, no `git`, no ledger — pure `build_plan` over a file."""
    view = json.loads(Path(args.view_json).read_text(encoding="utf-8"))
    plan = build_plan(view, run_id=args.run_id, tier=args.tier,
                      pr_origin=args.pr_origin, worktrees_root=args.worktrees_root)
    print(json.dumps(plan))
    return 0


def cmd_adopt(args) -> int:
    pr = str(args.pr)
    run_label = f"{RUN_LABEL_PREFIX}{args.run_id}"

    # 1. Read the PR.
    view_argv = ["gh", "pr", "view", pr]
    if args.repo:
        view_argv += ["--repo", args.repo]
    view_argv += ["--json", "number,title,body,headRefName,headRefOid,baseRefName,labels,state,"
                            "isCrossRepository,headRepositoryOwner,headRepository"]
    proc = _run(view_argv)
    if proc.returncode != 0:
        return _refuse(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        view = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _refuse(f"`gh pr view {pr}` output is not JSON ({exc})")

    # 2. Decide. On refuse, touch NOTHING — no label, no row, no worktree.
    plan = build_plan(view, run_id=args.run_id, tier=args.tier,
                      pr_origin=args.pr_origin, worktrees_root=args.worktrees_root)
    if plan["verdict"] == "refuse":
        return _refuse(str(plan["reason"]))

    branch = str(plan["branch"])
    worktree = str(plan["worktree"])
    row = plan["row"]

    # 3. Ensure the run owner label exists (idempotent — `--force` creates or updates).
    label_argv = ["gh", "label", "create", run_label, "--color", RUN_LABEL_COLOR,
                  "--description", f"gauntlet: run {args.run_id}", "--force"]
    if args.repo:
        label_argv += ["--repo", args.repo]
    proc = _run(label_argv)
    if proc.returncode != 0:
        return _refuse(f"could not create label {run_label}: {proc.stderr.strip()}")

    # 4. Register the row — refresh in place if it exists, else add. Never a duplicate row.
    _, rows = L.load(Path(args.file))
    verb = "set" if L.find_row(rows, pr) is not None else "add-row"
    ledger_argv = ["python3", str(LEDGER_PY), "--file", args.file, verb, "--pr", pr,
                   "--branch", branch,
                   "--head-sha", str(row["head_sha"]),
                   "--slug", str(row["slug"]),
                   "--tier", str(row["tier"]),
                   "--ci", str(row["ci"]),
                   "--status", str(row["status"]),
                   "--reviews-ok", str(row["reviews_ok"]),
                   "--pr-origin", str(row["pr_origin"])]
    proc = _run(ledger_argv, cwd=args.project_root)
    if proc.returncode != 0:
        return _refuse(f"ledger {verb} for PR {pr} failed: {proc.stderr.strip()}")

    # 5. Create-or-reuse the PR-head worktree, off the PR's OWN head branch. On any git failure, refuse
    # and say the row/label already landed — a half-adoption must be VISIBLE, never silently absorbed.
    half = (f"(the run label {run_label} and the ledger row for PR {pr} were ALREADY written; "
            f"the PR's labels were NOT applied)")
    if Path(worktree).is_dir():
        worktree_owned, branch_owned = "no", "no"
    else:
        fetch = _run(["git", "-C", args.project_root, "fetch", "origin",
                      f"refs/heads/{branch}:refs/remotes/origin/{branch}"])
        if fetch.returncode != 0:
            return _refuse(f"git fetch of head {branch} failed: {fetch.stderr.strip()} {half}")
        local = _run(["git", "-C", args.project_root, "show-ref", "--verify", "--quiet",
                      f"refs/heads/{branch}"])
        if local.returncode == 0:
            add = _run(["git", "-C", args.project_root, "worktree", "add", worktree, branch])
            branch_owned = "no"
        else:
            add = _run(["git", "-C", args.project_root, "worktree", "add", "-b", branch, worktree,
                        f"refs/remotes/origin/{branch}"])
            branch_owned = "yes"
        if add.returncode != 0:
            return _refuse(f"git worktree add for {branch} failed: {add.stderr.strip()} {half}")
        worktree_owned = "yes"

    set_argv = ["python3", str(LEDGER_PY), "--file", args.file, "set", "--pr", pr,
                "--worktree", worktree, "--worktree-owned", worktree_owned,
                "--branch-owned", branch_owned]
    proc = _run(set_argv, cwd=args.project_root)
    if proc.returncode != 0:
        return _refuse(f"ledger set of worktree fields for PR {pr} failed: {proc.stderr.strip()}")

    # 6. Label it ours and under review.
    edit_argv = ["gh", "pr", "edit", pr, "--add-label", run_label, "--add-label", REVIEWING_LABEL]
    if args.repo:
        edit_argv += ["--repo", args.repo]
    proc = _run(edit_argv)
    if proc.returncode != 0:
        return _refuse(f"`gh pr edit {pr}` (labels) failed: {proc.stderr.strip()} "
                       f"(the ledger row and worktree for PR {pr} were already written)")

    # 7. Adoption summary.
    print(json.dumps({
        "pr": pr,
        "run_id": args.run_id,
        "row_written": True,
        "worktree": worktree,
        "worktree_owned": worktree_owned,
        "labels": [run_label, REVIEWING_LABEL],
    }))
    return 0


# --- CLI ----------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="PURE: parse a `gh pr view` JSON file, print the adoption plan, exit 0")
    p.add_argument("--view-json", required=True, help="path to a parsed `gh pr view` JSON document")
    p.add_argument("--run-id", required=True, help="this run's id (the owner label is gauntlet-run-<id>)")
    p.add_argument("--tier", required=True, help="the review tier — an INPUT; this tool never triages")
    p.add_argument("--pr-origin", default="external", help="who wrote the PR (default: external)")
    p.add_argument("--worktrees-root", default=".worktrees", help="root under which the head worktree sits")

    a = sub.add_parser("adopt", help="the real thing: read the PR, refuse/register/worktree/label")
    a.add_argument("--pr", required=True, help="PR number to adopt")
    a.add_argument("--run-id", required=True, help="this run's id")
    a.add_argument("--file", required=True, help="the run ledger (<rundir>/state.jsonl)")
    a.add_argument("--tier", required=True, help="the review tier — an INPUT; this tool never triages")
    a.add_argument("--worktrees-root", required=True, help="root under which the head worktree sits")
    a.add_argument("--project-root", required=True, help="the repo checkout git/ledger commands run in")
    a.add_argument("--repo", help="owner/name (default: the current checkout's)")
    a.add_argument("--pr-origin", default="external", help="who wrote the PR (default: external)")

    sub.add_parser("self-test", help="run every fixture (pr-adopt-test.py's CASES)")

    args = parser.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "plan":
        return cmd_plan(args)
    return cmd_adopt(args)


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run "
                              f"and CANNOT report health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("pr_adopt_test", SIBLING, register=True)
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
        print("\n1 check(s) FAILED — pr-adopt's contract is broken.")
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
        print(f"{failures} check(s) FAILED — pr-adopt's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — pr-adopt's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
