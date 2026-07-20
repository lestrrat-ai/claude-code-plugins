#!/usr/bin/env python3
"""Execute one already-approved campaign merge and its owned local cleanup.

`merge-check.py` remains the owner of merge readiness. This command imports that gate, re-evaluates it
against the live PR and fetched base, then performs the established merge sequence. Each completed phase
is either durable outside this process (GitHub MERGED, updated refs, absent owned resources, terminal
ledger row) or safe to repeat. A rerun therefore resumes after interruption without repeating the merge.

It is also the FINALIZER for an absent-from-snapshot row loop-control.md Step 4 routes here after a merge
that landed but never finished: the single live view distinguishes MERGED (resume the owed base-sync /
cleanup / terminal-write phases) from CLOSED-without-merge (the terminal close-out — record `aborted`, no
merge, no cleanup, because unmerged branch content must never be destroyed).

    merge.py run --ledger <state.jsonl> --pr <N> --project-root <dir> --repo <owner/name> \
        [--merge-method squash|merge|rebase]
    merge.py self-test

The sibling `merge-test.py` is the executable contract. Tests replace the process boundary; they never
contact GitHub and never merge a real PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "merge-test.py"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


L = _load("merge_runner_ledger", "ledger.py")
MC = _load("merge_runner_check", "merge-check.py")

SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
COUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)\Z")
RUN_LABEL_PREFIX = "gauntlet-run-"
MERGE_METHODS = ("squash", "merge", "rebase")
VIEW_FIELDS = (
    "state,headRefOid,headRefName,baseRefName,labels,"
    "mergeable,mergeStateStatus,isDraft"
)


class Refusal(RuntimeError):
    """A fail-closed boundary or phase failure."""


def _run(argv: list[str], *, cwd: "str | None" = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _require(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no diagnostic").strip()
        raise Refusal(f"{what} failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def _one_lf(value: str) -> str:
    return value[:-1] if value.endswith("\n") else value


def resolve_project_root(checkout: Path) -> Path:
    """Resolve the typed repository root once, following runtime-adapter.md's boundary."""
    if not checkout.is_absolute():
        raise Refusal("--project-root must be absolute")
    out = _require(
        _run(["git", "-C", str(checkout), "rev-parse", "--show-toplevel"]),
        "repository resolution",
    )
    raw = _one_lf(out)
    if not raw or not os.path.isabs(raw):
        raise Refusal("repository resolution returned an empty or non-absolute path")
    resolved = Path(raw)
    if os.path.normpath(str(resolved)) != os.path.normpath(str(checkout)):
        raise Refusal(f"--project-root {checkout} resolves to a different repository root {resolved}")
    return resolved


def _labels(view: dict) -> list[str]:
    raw = view.get("labels")
    if not isinstance(raw, list):
        raise Refusal("live PR field 'labels' must be a list")
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            raise Refusal("live PR labels must contain string names")
        names.append(name)
    return names


def _validate_view(view: object) -> dict:
    if not isinstance(view, dict):
        raise Refusal("live PR view is not a JSON object")
    for field in ("state", "headRefOid", "headRefName", "baseRefName", "mergeable",
                  "mergeStateStatus"):
        if not isinstance(view.get(field), str):
            raise Refusal(f"live PR field {field!r} must be a string")
    if not isinstance(view.get("isDraft"), bool):
        raise Refusal("live PR field 'isDraft' must be a bool")
    _labels(view)
    return view


def _view(pr: str, repo: str, root: Path) -> dict:
    argv = ["gh", "pr", "view", pr, "--repo", repo, "--json", VIEW_FIELDS]
    out = _require(_run(argv, cwd=str(root)), f"live view for PR {pr}")
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise Refusal(f"live view for PR {pr} is not JSON: {exc}") from exc
    return _validate_view(parsed)


def _validate_ref(root: Path, value: str, role: str) -> None:
    if not value or value == "-":
        raise Refusal(f"{role} is unresolved")
    proc = _run(["git", "-C", str(root), "check-ref-format", f"refs/heads/{value}"])
    _require(proc, f"validation of {role} {value!r}")


def _validate_state(header: dict, row: dict, pr: str, root: Path, view: dict,
                    *, check_live_refs: bool = True) -> None:
    # `check_live_refs=False` drops ONLY the live head/base/branch equality pins below — the checks a
    # MERGE needs to land on the exact reviewed tip. The ledger-only CLOSED close-out (execute()) passes
    # it: it records terminal `aborted` and touches nothing else, so a head push or base/branch rename
    # before the close is irrelevant to it. Every non-close-out caller keeps the pins (the default).
    if row.get("pr") != pr:
        raise Refusal(f"ledger row belongs to PR {row.get('pr')}, not PR {pr}")
    if not pr.isdecimal() or int(pr) < 1:
        raise Refusal(f"PR number {pr!r} is not a positive integer")
    run_id = header.get("run_id", "-")
    if not run_id or run_id == "-":
        raise Refusal("ledger run_id is unresolved")
    base = header.get("base_branch", "-")
    branch = row.get("branch", "-")
    _validate_ref(root, base, "base_branch")
    _validate_ref(root, branch, "row branch")
    if row.get("head_sha") is None or not SHA_RE.match(row["head_sha"]):
        raise Refusal("ledger head_sha must be a full lowercase 40-character object id")
    if row.get("tier") not in ("TRIVIAL", "STANDARD", "HIGH"):
        raise Refusal(f"ledger tier {row.get('tier')!r} is malformed")
    if not COUNT_RE.match(row.get("reviews_ok", "")):
        raise Refusal(f"ledger reviews_ok {row.get('reviews_ok')!r} is malformed")
    if row.get("ci") not in ("green", "red", "pending"):
        raise Refusal(f"ledger ci {row.get('ci')!r} is malformed")
    if row.get("worktree_owned") not in ("yes", "no"):
        raise Refusal(f"ledger worktree_owned {row.get('worktree_owned')!r} is unresolved")
    if row.get("branch_owned") not in ("yes", "no"):
        raise Refusal(f"ledger branch_owned {row.get('branch_owned')!r} is unresolved")
    if row["branch_owned"] == "yes" and row["worktree_owned"] != "yes":
        raise Refusal("branch_owned=yes with worktree_owned=no is not an adoption-produced ownership state")
    worktree = Path(row.get("worktree", ""))
    if not worktree.is_absolute():
        raise Refusal("ledger worktree must be an absolute path")
    if check_live_refs:
        if view["headRefOid"] != row["head_sha"]:
            raise Refusal(
                f"live head {view['headRefOid']} differs from ledger head {row['head_sha']} — re-gate")
        if view["headRefName"] != branch:
            raise Refusal(
                f"live head branch {view['headRefName']!r} differs from ledger branch {branch!r}")
        if view["baseRefName"] != base:
            raise Refusal(
                f"live base {view['baseRefName']!r} differs from ledger base {base!r}")
    labels = _labels(view)
    ours = f"{RUN_LABEL_PREFIX}{run_id}"
    run_labels = [name for name in labels if name.startswith(RUN_LABEL_PREFIX)]
    if ours not in run_labels:
        raise Refusal(f"PR {pr} does not carry this run's owner label {ours}")
    foreign = [name for name in run_labels if name != ours]
    if foreign:
        raise Refusal(f"PR {pr} also carries another run's owner label {foreign[0]}")
    if row["worktree_owned"] == "yes":
        expected = root / ".worktrees" / branch
        if os.path.normpath(str(worktree)) != os.path.normpath(str(expected)):
            raise Refusal(
                f"owned worktree {worktree} is not the repository-derived campaign path {expected}")
        if os.path.normpath(str(worktree)) == os.path.normpath(str(root)):
            raise Refusal("the repository root can never be an owned cleanup target")


def _base_is_current(row: dict, header: dict) -> None:
    worktree = row["worktree"]
    base = header["base_branch"]
    # Fully-qualified refspec so a dash-leading base name (network-supplied via baseRefName) can never be
    # parsed by git as an option; the same safety idiom as _sync_base and pr-adopt.py. This updates the very
    # origin/<base> remote-tracking ref the ancestry probe below reads.
    tracking_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
    _require(
        _run(["git", "-C", worktree, "fetch", "origin", tracking_ref]),
        f"refresh of origin/{base} for merge-check",
    )
    probe = _run(
        ["git", "-C", worktree, "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"])
    if probe.returncode == 1:
        raise Refusal("merge-check: base moved ahead — rebase")
    _require(probe, f"merge-check ancestry against origin/{base}")


def _require_ready(row: dict, header: dict, view: dict) -> None:
    """Delegate policy to merge-check.py, then supply its fetched-base ancestry phase."""
    result = MC.decide(row, view, required=MC.REQUIRED)
    if result.get("verdict") != MC.MERGE:
        raise Refusal(f"merge-check: {result.get('reason', 'not ready')}")
    _base_is_current(row, header)


def _parse_worktrees(data: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in data.split("\0"):
        if token == "":
            if current:
                entries.append(current)
                current = {}
            continue
        key, sep, value = token.partition(" ")
        current[key] = value if sep else ""
    if current:
        entries.append(current)
    return entries


def _worktree_listing(root: Path) -> list[dict[str, str]]:
    out = _require(
        _run(["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"]),
        "worktree discovery",
    )
    return _parse_worktrees(out)


def _sync_base(root: Path, base: str) -> None:
    remote_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
    _require(
        _run(["git", "-C", str(root), "fetch", "origin", remote_ref]),
        f"refresh of origin/{base}",
    )
    branch_ref = f"refs/heads/{base}"
    checked: list[str] = []
    for entry in _worktree_listing(root):
        path = entry.get("worktree")
        if entry.get("branch") == branch_ref and path is not None:
            checked.append(path)
    if len(checked) > 1:
        raise Refusal(f"base branch {base!r} is checked out in more than one worktree")
    if checked:
        # ff-only intentionally accepts a local-ahead base (git reports "Already up to date", exit 0).
        # Downstream diffs/rebases read origin/<base> (freshly fetched above), not this local branch, so a
        # local-ahead checkout poisons nothing; local-ahead means origin is an ancestor of local, i.e. local
        # already contains the merged tip. The dangerous diverged case still fails ff-only and refuses below.
        _require(
            _run(["git", "-C", checked[0], "merge", "--ff-only", f"origin/{base}"]),
            f"fast-forward of checked-out base {base}",
        )
    else:
        # Fully-qualified refspec (no leading `+`, preserving fast-forward-only semantics) so a dash-leading
        # base name can never be parsed by git as an option — the same safety idiom used just above.
        local_ref = f"refs/heads/{base}:refs/heads/{base}"
        _require(
            _run(["git", "-C", str(root), "fetch", "origin", local_ref]),
            f"fast-forward of local base ref {base}",
        )


def _entry_at(entries: list[dict[str, str]], path: str) -> "dict[str, str] | None":
    target = os.path.normpath(path)
    for entry in entries:
        if os.path.normpath(entry.get("worktree", "")) == target:
            return entry
    return None


def _cleanup(root: Path, row: dict) -> dict:
    branch = row["branch"]
    branch_ref = f"refs/heads/{branch}"
    worktree = row["worktree"]
    removed_worktree = False
    removed_branch = False

    entries = _worktree_listing(root)
    entry = _entry_at(entries, worktree)
    if row["worktree_owned"] == "yes":
        if entry is not None:
            if entry.get("branch") != branch_ref:
                raise Refusal(
                    f"owned worktree path {worktree} now holds a foreign or detached checkout")
            status = _require(
                _run(["git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"]),
                f"cleanliness check for owned worktree {worktree}",
            )
            if status:
                raise Refusal(f"owned worktree {worktree} is dirty; refusing cleanup")
            _require(
                _run(["git", "-C", str(root), "worktree", "remove", worktree]),
                f"removal of owned worktree {worktree}",
            )
            removed_worktree = True
    elif entry is not None and entry.get("branch") not in (branch_ref, None):
        raise Refusal(f"reused worktree path {worktree} now holds a foreign branch")

    if row["branch_owned"] == "yes":
        entries = _worktree_listing(root)
        checked = [entry.get("worktree") for entry in entries if entry.get("branch") == branch_ref]
        if checked:
            raise Refusal(f"owned branch {branch!r} is still checked out at {checked[0]}")
        exists = _run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", branch_ref])
        if exists.returncode == 0:
            actual = _one_lf(_require(
                _run(["git", "-C", str(root), "rev-parse", branch_ref]),
                f"identity check for owned branch {branch}",
            ))
            if actual != row["head_sha"]:
                raise Refusal(
                    f"owned branch {branch!r} moved to {actual}; expected {row['head_sha']}")
            _require(
                _run(["git", "-C", str(root), "branch", "-D", "--", branch]),
                f"removal of owned branch {branch}",
            )
            removed_branch = True
        elif exists.returncode != 1:
            _require(exists, f"lookup of owned branch {branch}")

    return {
        "worktree": "removed" if removed_worktree else
                    ("already-absent" if row["worktree_owned"] == "yes" else "reused-left"),
        "branch": "removed" if removed_branch else
                  ("already-absent" if row["branch_owned"] == "yes" else "reused-left"),
    }


def _mark_terminal(ledger: Path, pr: str, status: str) -> None:
    """Write a TERMINAL ledger status (`merged` after a landed merge, `aborted` after a closed-without-merge
    close-out) as the last, resumable phase. A same-value write is a no-op, so a re-run finalizes idempotently."""
    header, rows = L.load(ledger)
    row = L.find_row(rows, pr)
    if row is None:
        raise Refusal(f"ledger row for PR {pr} disappeared before terminal write")
    if row["status"] != status:
        row["status"] = status
        try:
            L.save(ledger, header, rows, activity=True)
        except OSError as exc:
            raise Refusal(f"terminal ledger write failed: {exc}") from exc


def execute(ledger: Path, pr: str, project_root: Path, repo: str,
            merge_method: str = "squash") -> dict:
    if merge_method not in MERGE_METHODS:
        raise Refusal(
            f"merge method {merge_method!r} is not one of {', '.join(MERGE_METHODS)}")
    root = resolve_project_root(project_root)
    header, rows = L.load(ledger)
    row = L.find_row(rows, pr)
    if row is None:
        raise Refusal(f"no ledger row for PR {pr}")
    view = _view(pr, repo, root)

    # CLOSED WITHOUT MERGING — the terminal close-out, the CLOSED side of the absent-row finalizer
    # loop-control.md Step 4 routes here (a human closed the PR, or the driver died after `gh pr close`).
    # There is NOTHING to merge and NOTHING to clean up: the branch content never reached `<base>`, so an
    # owned worktree/branch holds UNMERGED work that removing it would destroy. This close-out is
    # ledger-only, so the live head/base/branch pins do NOT apply to it (`check_live_refs=False`): a push
    # that advanced the head before the close, or a base/branch rename, must still TERMINATE the row, not
    # wedge it non-terminal forever (a CLOSED PR never re-enters the open snapshot to be re-gated). ANY
    # non-terminal row — `in_review` OR any held status (`L.HELD_STATUSES`) — is a real close-out: a CLOSED
    # PR moots every held reason (nothing left to merge, approve, adjudicate, or repair), and a human
    # closing a parked PR IS the resolution. Only a `merged` row with a CLOSED live state is a contradiction
    # (a merged PR reports MERGED, not CLOSED), left to the fully-validated status gate below. Record the
    # terminal `aborted` status (files-and-ledger.md, `status` taxonomy: any non-terminal status -> `aborted`)
    # and stop.
    close_out = view["state"] == "CLOSED" and row["status"] not in ("merged", "aborted")
    # An already-`aborted` row whose PR is still CLOSED is the aborted TERMINAL-REPEAT, symmetric with the
    # `merged`-repeat no-op below: like the fresh close-out it is ledger-only (nothing to merge or clean up),
    # so it too drops the live head/base/branch pins — a push or base/branch rename that landed before the
    # close must not turn a settled no-op into a spurious refusal. The `aborted` block below is its
    # CLOSED-only guard; a live OPEN/MERGED keeps the pins (its refusal is a contradiction, not a no-op).
    aborted_repeat = row["status"] == "aborted" and view["state"] == "CLOSED"
    _validate_state(header, row, pr, root, view, check_live_refs=not (close_out or aborted_repeat))
    if close_out:
        _mark_terminal(ledger, pr, "aborted")
        return {"status": "closed-unmerged", "pr": pr, "cleanup": {}}

    if row["status"] == "aborted":
        # Terminal-repeat, symmetric with the `merged` no-op below (both terminal statuses are safe to
        # repeat). A CLOSED live state confirms the recorded abort still holds -> the same already-complete
        # no-op, no ledger write. A live OPEN or MERGED state CONTRADICTS the terminal `aborted` row (the PR
        # is no longer closed-without-merge); refuse naming the mismatch, never a silent no-op.
        if view["state"] != "CLOSED":
            raise Refusal(
                f"terminal ledger row says aborted but GitHub state is {view['state']!r}, not CLOSED")
        return {"status": "already-complete", "pr": pr, "cleanup": {}}

    if row["status"] == "merged":
        if view["state"] != "MERGED":
            raise Refusal(
                f"terminal ledger row says merged but GitHub state is {view['state']!r}")
        return {"status": "already-complete", "pr": pr, "cleanup": {}}
    if row["status"] != "in_review":
        if row["status"] in L.HELD_STATUSES:
            raise Refusal(f"PR {pr} is held ({row['status']})")
        raise Refusal(f"PR {pr} row status is {row['status']!r}, not in_review")

    if view["state"] == "OPEN":
        _require_ready(row, header, view)
        # --match-head-commit pins the merge to the exact reviewed SHA. If a push advanced the live tip
        # in the window between the pre-merge view and this call, GitHub refuses fail-closed rather than
        # squashing the unreviewed head; the post-merge re-validation would only detect that after landing.
        merge_argv = ["gh", "pr", "merge", pr, "--repo", repo, f"--{merge_method}",
                      "--match-head-commit", row["head_sha"]]
        merge_proc = _run(merge_argv, cwd=str(root))
        # A transport failure can happen after GitHub accepted the merge. Re-read state before deciding
        # whether this phase failed; MERGED is the durable checkpoint and prevents a second merge attempt.
        confirmed = _view(pr, repo, root)
        _validate_state(header, row, pr, root, confirmed)
        if confirmed["state"] != "MERGED":
            if merge_proc.returncode != 0:
                _require(merge_proc, f"merge of PR {pr}")
            raise Refusal(
                f"merge command returned success but PR {pr} state is {confirmed['state']!r}, not MERGED")
        view = confirmed
    elif view["state"] != "MERGED":
        # CLOSED was already finalized by the close-out above; only OPEN and MERGED remain live here.
        raise Refusal(f"PR {pr} state is {view['state']!r}; expected OPEN, MERGED, or CLOSED")

    # MERGED is confirmed before any local ref or worktree is changed. Each following phase is idempotent.
    _sync_base(root, header["base_branch"])
    cleanup = _cleanup(root, row)
    _mark_terminal(ledger, pr, "merged")
    return {"status": "merged", "pr": pr, "cleanup": cleanup}


class SelfTestFailure(AssertionError):
    """A claimed merge-runner rule did not hold."""


def _sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"fixture suite is missing at {SIBLING}")
    mod = load_module_from_path("merge_runner_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"cannot load fixture suite at {SIBLING}")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING.name} exports no CASES")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = _sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL sibling-fixtures: {exc}")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {rule}\n     {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}: {rule}")
    if failures:
        print(f"{failures} merge-runner fixture(s) failed")
        return 1
    print(f"all {len(cases)} merge-runner fixtures hold")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="resume or execute one proven-ready merge")
    run.add_argument("--ledger", required=True, type=Path)
    run.add_argument("--pr", required=True)
    run.add_argument("--project-root", required=True, type=Path)
    run.add_argument("--repo", required=True)
    run.add_argument("--merge-method", default="squash", choices=MERGE_METHODS,
                     help="merge method (default: squash; use the repo's prevailing method if squash is disabled)")
    sub.add_parser("self-test", help="run every fixture in merge-test.py")
    args = parser.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    try:
        result = execute(args.ledger, str(args.pr), args.project_root, args.repo,
                         merge_method=args.merge_method)
    except (Refusal, SystemExit) as exc:
        detail = exc if isinstance(exc, Refusal) else "ledger rejected malformed state"
        print(f"merge: REFUSED — {detail}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
