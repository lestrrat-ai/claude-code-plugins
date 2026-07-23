#!/usr/bin/env python3
"""Fixtures for `clean-rebase.py` — driven against REAL throwaway git repos, not a mocked git.

Every case builds an actual bare "remote", a base branch, a PR branch, and a PR-head worktree on disk,
then drives the tool's REAL code (`clean-rebase.py run`, in-process via `capture_cli`) and asserts the
resulting repo/ledger state with git plumbing — never an exit code alone. The ledger writes go through the
REAL `ledger.py` subprocess the tool invokes, so the whole path is exercised end to end.

`clean-rebase.py self-test` FAILS LOUDLY if it cannot load this file — it can never report health over a
suite it did not run.

The diff-changed cases are CONSTRUCTED, not synthesized: the base edits a line INSIDE the context window
of the PR's own hunk (line 5 vs the PR's line-3 change), including a real indentation-only edit. Each
rebase applies textually — no conflict — but the PR's three-dot diff carries the rewritten context line,
so `git patch-id --verbatim` differs before and after. These are genuine "clean textually, but the PR's
diff changed" rebases, and the tool must refuse them exactly as it refuses a conflict.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "clean-rebase.py"


def _load_owner():
    mod = load_module_from_path("clean_rebase_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the clean-rebase tool at {OWNER}")
    return mod


M = _load_owner()
L = M.L  # the ledger module the tool loaded


def check(cond, msg) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


# --- git / ledger helpers -----------------------------------------------------

def _run(argv, cwd=None):
    return subprocess.run(argv, capture_output=True, text=True, cwd=cwd)  # noqa: S603


def git(cwd, *args, allow_fail=False):
    r = _run(["git", "-C", str(cwd), *args])
    if not allow_fail and r.returncode != 0:
        raise M.SelfTestFailure(f"git {' '.join(args)} failed in {cwd}: {r.stderr.strip()}")
    return r


def _cfg(repo):
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Tester")


def head(cwd, ref="HEAD") -> str:
    return git(cwd, "rev-parse", ref).stdout.strip()


def _ledger(*args) -> int:
    """Run the real ledger main in-process (quietly) for test SETUP."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        try:
            return L.main(list(args))
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1


def _field(ledger: Path, pr, name):
    _, rows = L.load(ledger)
    row = L.find_row(rows, str(pr))
    return row[name] if row else None


# 12 numbered lines — a wide file so an adjacent-context edit and a far edit are unambiguous.
def _numbered(overrides: "dict[int, str]") -> str:
    lines = [overrides.get(i, str(i)) for i in range(1, 13)]
    return "\n".join(lines) + "\n"


PR_NUMBER = "12"
PR_BRANCH = "pr"


class Scenario:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        self.seed = tmp / "seed"
        self.wt = tmp / "wt"
        self.ledger = tmp / "state.jsonl"
        self.orig_head = ""

    def build(self, *, status="in_review", reviews_ok=2):
        _run(["git", "init", "--bare", "-b", "main", str(self.remote)])
        _run(["git", "clone", str(self.remote), str(self.seed)])
        _cfg(self.seed)
        # base commit on main
        (self.seed / "f").write_text(_numbered({}), encoding="utf-8")
        git(self.seed, "add", "f")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "push", "origin", "main")
        # PR branch: change line 3
        git(self.seed, "checkout", "-b", PR_BRANCH)
        (self.seed / "f").write_text(_numbered({3: "3-PR"}), encoding="utf-8")
        git(self.seed, "commit", "-am", "pr change")
        git(self.seed, "push", "origin", PR_BRANCH)
        self.orig_head = head(self.seed)
        # the PR-head worktree — a fresh clone with the PR branch checked out
        _run(["git", "clone", str(self.remote), str(self.wt)])
        _cfg(self.wt)
        git(self.wt, "checkout", PR_BRANCH)
        check(head(self.wt) == self.orig_head, "precondition: the worktree is at the PR head")
        # ledger: header + row, with reviews_ok earned by real verdicts at the PR head
        _ledger("--file", str(self.ledger), "header", "set", "base_branch", "main")
        _ledger("--file", str(self.ledger), "add-row", "--pr", PR_NUMBER, "--branch", PR_BRANCH,
                "--head-sha", self.orig_head, "--worktree", str(self.wt), "--tier", "STANDARD",
                "--status", status)
        # `verdict` refuses unless a base-preflight `proceed` is on record for this head
        # (base_ok_sha == head_sha); stamp it first, as the real flow does (base-preflight.py -> base-ok).
        if reviews_ok:
            _ledger("--file", str(self.ledger), "base-ok", "--pr", PR_NUMBER, "--head-sha", self.orig_head)
        for _ in range(reviews_ok):
            _ledger("--file", str(self.ledger), "verdict", "--pr", PR_NUMBER,
                    "--head-sha", self.orig_head, "--verdict", "satisfied")
        return self

    def advance_base(self, overrides: "dict[int, str]"):
        """Move remote main ahead by one commit that rewrites the given lines."""
        git(self.seed, "checkout", "main")
        (self.seed / "f").write_text(_numbered(overrides), encoding="utf-8")
        git(self.seed, "commit", "-am", "base advance")
        git(self.seed, "push", "origin", "main")
        git(self.seed, "checkout", PR_BRANCH)

    def move_remote_pr(self):
        """A concurrent push moves remote `pr` past the worktree's stale tracking ref (breaks the lease)."""
        git(self.seed, "checkout", PR_BRANCH)
        git(self.seed, "commit", "--allow-empty", "-m", "concurrent push")
        git(self.seed, "push", "origin", PR_BRANCH)
        git(self.seed, "checkout", "main")

    def remote_pr_head(self) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{PR_BRANCH}").stdout.strip()

    def invoke(self, *extra):
        argv = ["run", "--ledger", str(self.ledger), "--pr", PR_NUMBER, "--worktree", str(self.wt),
                "--base", "main", *extra]
        return capture_cli(M.main, argv)


def _scenario(tmp) -> Scenario:
    return Scenario(Path(tmp)).build()


# --- the CLEAN case, end to end ----------------------------------------------

def t_clean_rebase_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        # the fixture stamped base_ok_sha == orig_head (a base-preflight `proceed` on record) via base-ok
        check(_field(s.ledger, PR_NUMBER, "base_ok_sha") == s.orig_head,
              "fixture setup: a base-preflight proceed is on record for the pre-rebase head")
        s.advance_base({12: "12-BASE"})  # a FAR edit — outside the PR hunk's context, diff unchanged
        code, out, err = s.invoke()
        check(code == M.EXIT_OK, f"a clean base-only rebase must exit 0 (code={code}, err={err})")
        new_head = head(s.wt)
        check(new_head != s.orig_head, "the clean rebase moved the worktree HEAD to a new commit")
        # the ledger recorded the new head, ci pending, and the liveness counters reset — reviews_ok kept
        check(_field(s.ledger, PR_NUMBER, "head_sha") == new_head, "the ledger records the new head_sha")
        check(_field(s.ledger, PR_NUMBER, "ci") == "pending", "a head_sha change sets ci=pending")
        for f in M.LIVENESS_COUNTERS:
            check(_field(s.ledger, PR_NUMBER, f) == str(L.ROW_DEFAULTS[f]),
                  f"the clean rebase resets the liveness counter {f} to its fresh-head default")
        # the head move ALSO voids base_ok_sha at the `set --head-sha` door — a fresh base-preflight
        # `proceed` must be re-earned before the next verdict, even though reviews_ok carried forward.
        check(_field(s.ledger, PR_NUMBER, "base_ok_sha") == str(L.ROW_DEFAULTS["base_ok_sha"]),
              "the clean rebase VOIDS base_ok_sha — a moved head is unverified until a fresh proceed")
        check(_field(s.ledger, PR_NUMBER, "reviews_ok") == "2",
              "the clean case CARRIES reviews_ok FORWARD — the gate is not reset (the Exception rule)")
        check(_field(s.ledger, PR_NUMBER, "review_rounds") == "2",
              "review_rounds is monotone — a clean rebase never touches it")
        # the emitted result-JSON `ledger` object ECHOES the reset base_ok_sha, so a driver reading it
        # SEES the stamp was voided rather than discovering it only when `verdict` refuses.
        result = json.loads(out.strip().splitlines()[-1])
        check(result["ledger"]["base_ok_sha"] == str(L.ROW_DEFAULTS["base_ok_sha"]),
              f"the result JSON must echo the voided base_ok_sha; got {result.get('ledger')!r}")
        check(result["ledger"]["head_sha"] == new_head,
              "the result JSON echoes the new head_sha it wrote")
        # the echo is read back from the ACTUAL stored row, so EVERY reported field equals what was written
        # (no field is hardcoded from ROW_DEFAULTS) — on a genuine head move that is the reset value.
        for f in ("head_sha", "ci", "base_ok_sha", *M.LIVENESS_COUNTERS):
            check(result["ledger"][f] == str(_field(s.ledger, PR_NUMBER, f)),
                  f"the result JSON echoes the ACTUAL stored {f}; "
                  f"stored={_field(s.ledger, PR_NUMBER, f)!r} echoed={result['ledger'].get(f)!r}")
        # the remote branch was force-with-lease pushed to the new head
        check(s.remote_pr_head() == new_head, "the remote PR branch was updated to the rebased head")


# --- a NO-OP rebase: base did not advance, head unchanged, base_ok_sha RETAINED --

def t_noop_rebase_echoes_retained_base_ok_sha():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        # NO advance_base(): the base has not moved, so `git rebase` is a no-op and new_head == orig_head.
        # apply_head_sha treats a same-head write as "not a move" and resets NOTHING — so base_ok_sha keeps
        # the stamp the fixture recorded (== orig_head). The result JSON must echo that RETAINED value, NOT
        # the fresh-head "-" a genuine move produces: the echo is read from the written row, never hardcoded.
        check(_field(s.ledger, PR_NUMBER, "base_ok_sha") == s.orig_head,
              "fixture setup: base_ok_sha is stamped to the pre-rebase head")
        code, out, err = s.invoke()
        check(code == M.EXIT_OK, f"a no-op clean rebase still exits 0 (code={code}, err={err})")
        check(head(s.wt) == s.orig_head, "a no-op rebase leaves the worktree HEAD unchanged")
        stored = _field(s.ledger, PR_NUMBER, "base_ok_sha")
        check(stored == s.orig_head,
              "a same-head no-op does NOT void base_ok_sha — the accessor only voids it on a genuine move")
        result = json.loads(out.strip().splitlines()[-1])
        # the echo matches the STORED row for every field — base_ok_sha included — so a no-op reports the
        # retained stamp, not a reset that never happened.
        for f in ("head_sha", "ci", "base_ok_sha", *M.LIVENESS_COUNTERS):
            check(result["ledger"][f] == str(_field(s.ledger, PR_NUMBER, f)),
                  f"the no-op result JSON echoes the ACTUAL stored {f}; "
                  f"stored={_field(s.ledger, PR_NUMBER, f)!r} echoed={result['ledger'].get(f)!r}")
        check(result["ledger"]["base_ok_sha"] == s.orig_head,
              f"the no-op echoes the RETAINED base_ok_sha (the original stamp), not the fresh-head '-'; "
              f"got {result['ledger'].get('base_ok_sha')!r}")


# --- a CONFLICT: abort, restore, refuse (exit 3), touch nothing ---------------

def t_conflict_aborts_and_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({3: "3-BASE"})  # base rewrites the SAME line the PR changed -> conflict
        code, out, _ = s.invoke()
        check(code == M.EXIT_NOT_CLEAN, f"a conflicting rebase must exit {M.EXIT_NOT_CLEAN} (code={code})")
        check('"refused": "conflict"' in out, f"the refusal names the conflict; got {out!r}")
        check(head(s.wt) == s.orig_head,
              "a conflicting rebase is ABORTED and HEAD restored to the original — no partial state")
        # a clean tree survives (rebase --abort leaves no half-applied state)
        check(git(s.wt, "status", "--porcelain").stdout.strip() == "",
              "the worktree is clean after the abort")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head, "the ledger head_sha is untouched")
        check(_field(s.ledger, PR_NUMBER, "reviews_ok") == "2", "the ledger reviews_ok is untouched")
        check(s.remote_pr_head() == s.orig_head, "the remote PR branch is untouched")


# --- diff-changed-under-rebase: clean textually, but the PR's diff moved ------

def t_diff_changed_resets_and_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        # base edits line 5 — INSIDE the context window of the PR's line-3 hunk. No conflict, but the PR's
        # three-dot diff now carries "5-BASE" as context, so its patch-id differs before/after the rebase.
        s.advance_base({5: "5-BASE"})
        code, out, err = s.invoke()
        check(code == M.EXIT_NOT_CLEAN,
              f"a rebase that changes the PR's diff must exit {M.EXIT_NOT_CLEAN} (code={code}, err={err})")
        check('"refused": "diff-changed"' in out, f"the refusal names diff-changed; got {out!r}")
        check(head(s.wt) == s.orig_head,
              "diff-changed is reset --hard back to the original head — no partial state")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head, "the ledger head_sha is untouched")
        check(_field(s.ledger, PR_NUMBER, "reviews_ok") == "2", "the ledger reviews_ok is untouched")
        check(s.remote_pr_head() == s.orig_head, "the remote PR branch is untouched")


def t_indentation_only_context_change_resets_and_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        ledger_before = s.ledger.read_bytes()
        remote_pr_before = s.remote_pr_head()
        # A REAL indentation-only base edit inside the PR hunk's context. The rebase applies without a
        # conflict, but the PR's three-dot diff changes from context line "5" to "    5".
        s.advance_base({5: "    5"})
        code, out, err = s.invoke()
        check(code == M.EXIT_NOT_CLEAN,
              f"an indentation-only context change must exit {M.EXIT_NOT_CLEAN} "
              f"(code={code}, err={err})")
        check('"refused": "diff-changed"' in out, f"the refusal names diff-changed; got {out!r}")
        check(head(s.wt) == s.orig_head,
              "indentation-only diff-changed restores the original local HEAD")
        check(s.remote_pr_head() == remote_pr_before,
              "indentation-only diff-changed leaves the remote PR branch untouched")
        check(s.ledger.read_bytes() == ledger_before,
              "indentation-only diff-changed leaves the ledger byte-for-byte untouched")
        check(_field(s.ledger, PR_NUMBER, "reviews_ok") == "2",
              "the review tally remains banked until the caller applies the required gate reset")
        check(_ledger("--file", str(s.ledger), "set", "--pr", PR_NUMBER, "--reviews-ok", "0") == 0,
              "the caller can apply the required gate reset after the refusal")
        check(_field(s.ledger, PR_NUMBER, "reviews_ok") == "0",
              "the review tally clears only when the caller resets it")


# --- patch-id failure: fail closed before push and ledger write --------------

def t_patch_id_failure_before_rebase_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({12: "12-BASE"})
        ledger_before = s.ledger.read_bytes()
        remote_pr_before = s.remote_pr_head()
        patch_inputs = []
        real_run = M._run

        def fail_patch_id(argv, *, cwd=None, stdin=None):
            if argv == ["git", "patch-id", "--verbatim"]:
                patch_inputs.append(stdin)
                return subprocess.CompletedProcess(argv, 129, "", "error: unknown option `verbatim'\n")
            return real_run(argv, cwd=cwd, stdin=stdin)

        with patch.object(M, "_run", side_effect=fail_patch_id):
            code, out, err = s.invoke()

        check(code == M.EXIT_PRECONDITION,
              f"a pre-rebase patch-id failure must exit {M.EXIT_PRECONDITION} "
              f"(code={code}, err={err})")
        check('"refused": "diff-failed"' in out,
              f"the pre-rebase refusal names the identity failure; got {out!r}")
        check(len(patch_inputs) == 1 and bool(patch_inputs[0]),
              "the fixture fails the first patch-id call after it receives the non-empty PR diff")
        check(head(s.wt) == s.orig_head,
              "a pre-rebase patch-id failure refuses before rebasing the local branch")
        check(s.remote_pr_head() == remote_pr_before,
              "a pre-rebase patch-id failure leaves the remote PR branch untouched")
        check(s.ledger.read_bytes() == ledger_before,
              "a pre-rebase patch-id failure leaves the ledger byte-for-byte untouched")


def t_patch_id_failure_after_rebase_resets_and_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({12: "12-BASE"})
        ledger_before = s.ledger.read_bytes()
        remote_pr_before = s.remote_pr_head()
        patch_inputs = []
        real_run = M._run

        def fail_second_patch_id(argv, *, cwd=None, stdin=None):
            if argv == ["git", "patch-id", "--verbatim"]:
                patch_inputs.append(stdin)
                if len(patch_inputs) == 2:
                    return subprocess.CompletedProcess(
                        argv, 129, "", "error: unknown option `verbatim'\n")
            return real_run(argv, cwd=cwd, stdin=stdin)

        with patch.object(M, "_run", side_effect=fail_second_patch_id):
            code, out, err = s.invoke()

        check(code == M.EXIT_PRECONDITION,
              f"a post-rebase patch-id failure must exit {M.EXIT_PRECONDITION} "
              f"(code={code}, err={err})")
        check('"refused": "diff-failed"' in out,
              f"the post-rebase refusal names the identity failure; got {out!r}")
        check(len(patch_inputs) == 2 and all(patch_inputs),
              "the fixture fails only the second patch-id call after both receive non-empty PR diffs")
        check(head(s.wt) == s.orig_head,
              "a post-rebase patch-id failure resets the local branch to its original HEAD")
        check(s.remote_pr_head() == remote_pr_before,
              "a post-rebase patch-id failure leaves the remote PR branch untouched")
        check(s.ledger.read_bytes() == ledger_before,
              "a post-rebase patch-id failure leaves the ledger byte-for-byte untouched")


# --- precondition refusals (exit 2, nothing mutated) -------------------------

def t_dirty_worktree_refused():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        (s.wt / "f").write_text("dirtied\n", encoding="utf-8")  # uncommitted change
        code, out, _ = s.invoke()
        check(code == M.EXIT_PRECONDITION, f"a dirty worktree is refused at exit 2 (code={code})")
        check('"refused": "dirty"' in out, f"the refusal names the dirty tree; got {out!r}")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head, "nothing mutated — head_sha untouched")


def t_wrong_branch_refused():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        git(s.wt, "checkout", "main")  # the row names `pr`, but `main` is checked out
        code, out, _ = s.invoke()
        check(code == M.EXIT_PRECONDITION, f"a wrong-branch checkout is refused at exit 2 (code={code})")
        check('"refused": "wrong-branch"' in out, f"the refusal names the branch mismatch; got {out!r}")


def t_head_mismatch_refused():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        _ledger("--file", str(s.ledger), "set", "--pr", PR_NUMBER, "--head-sha", "b" * 40)
        code, out, _ = s.invoke()
        check(code == M.EXIT_PRECONDITION, f"a HEAD != ledger head_sha is refused at exit 2 (code={code})")
        check('"refused": "stale"' in out, f"the refusal names the stale mismatch; got {out!r}")
        check(s.orig_head in out and "b" * 40 in out,
              "the refusal names BOTH the worktree HEAD and the ledger head_sha")


def t_held_row_refused():
    with tempfile.TemporaryDirectory() as tmp:
        s = Scenario(Path(tmp)).build(status="awaiting-user")
        s.advance_base({12: "12-BASE"})
        code, out, _ = s.invoke()
        check(code == M.EXIT_PRECONDITION, f"a held row is refused at exit 2 (code={code})")
        check('"refused": "held"' in out, f"the refusal names the held status; got {out!r}")
        check(head(s.wt) == s.orig_head, "a held PR is never rebased — HEAD untouched")
        check(s.remote_pr_head() == s.orig_head, "and the remote PR branch is untouched")


def t_no_row_refused():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(s.ledger), "--pr", "999",
                                            "--worktree", str(s.wt), "--base", "main"])
        check(code == M.EXIT_PRECONDITION, f"a missing row is refused at exit 2 (code={code})")
        check('"refused": "no-row"' in out, f"the refusal names the missing row; got {out!r}")


def t_base_mismatch_refused():
    # `--base` is an ASSERTION: a value disagreeing with the row's effective base is refused BEFORE any
    # fetch/rebase, and nothing mutates. The scenario's row inherits header base `main`; assert `v3`.
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(s.ledger), "--pr", PR_NUMBER,
                                            "--worktree", str(s.wt), "--base", "v3"])
        check(code == M.EXIT_PRECONDITION, f"a --base disagreeing with the row is refused at exit 2 (code={code})")
        check('"refused": "base-mismatch"' in out, f"the refusal names the base mismatch; got {out!r}")
        check("v3" in out and "main" in out, "the refusal names BOTH the passed --base and the effective base")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head, "nothing mutated — head_sha untouched")


def t_unresolved_base_refused():
    # A both-`-` ledger (header base_branch unset AND row base-branch unset) resolves through
    # `effective_base` to the `-` sentinel — an UNRESOLVED base. It is refused at step 1a as `no-base`
    # BEFORE any fetch/rebase, never treated as a real branch (`ledger.py require_effective_base`, the one
    # owner). If that guard is deleted, the base assertion is SKIPPED and execution falls through to a later
    # precondition (worktree-missing), so this fixture FAILS — that is exactly the bug the guard fixes.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ledger = tmp / "state.jsonl"
        _ledger("--file", str(ledger), "header", "set", "run_id", "t")            # base_branch left `-`
        _ledger("--file", str(ledger), "add-row", "--pr", PR_NUMBER, "--status", "in_review")  # base `-`
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(ledger), "--pr", PR_NUMBER,
                                            "--worktree", str(tmp / "missing"), "--base", "v3"])
        check(code == M.EXIT_PRECONDITION, f"an unresolved (`-`) base is refused at exit 2 (code={code})")
        check('"refused": "no-base"' in out, f"the refusal names the unresolved base as no-base; got {out!r}")
        check("no usable effective base" in out, f"the refusal explains the unresolved base; got {out!r}")


def t_origin_named_base_agrees():
    # A row base LITERALLY named `origin/rel` (a legal branch name) matches an identical `--base` — the
    # assertion routes through `ledger.py base_agrees`, where identical strings always agree. The refusal
    # that follows is the MISSING WORKTREE (step 3), proving the base assertion (step 1a) passed. The bare
    # form still refuses `base-mismatch`: the STORED base is never stripped.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ledger = tmp / "state.jsonl"
        _ledger("--file", str(ledger), "header", "set", "run_id", "t")
        _ledger("--file", str(ledger), "add-row", "--pr", "12", "--head-sha", "a" * 40,
                "--base-branch", "origin/rel", "--status", "in_review")
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(ledger), "--pr", "12",
                                            "--worktree", str(tmp / "missing"), "--base", "origin/rel"])
        check(code == M.EXIT_PRECONDITION, f"the missing worktree still refuses at exit 2 (code={code})")
        check('"refused": "worktree-missing"' in out and '"base-mismatch"' not in out,
              f"identical origin/rel strings must pass the base assertion and reach the worktree check; "
              f"got {out!r}")
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(ledger), "--pr", "12",
                                            "--worktree", str(tmp / "missing"), "--base", "rel"])
        check(code == M.EXIT_PRECONDITION and '"refused": "base-mismatch"' in out,
              f"a bare --base must disagree with a stored origin/-named base; got {out!r}")


def t_absent_remote_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # a standalone repo with a commit but NO remote configured
        wt = tmp / "solo"
        wt.mkdir()
        _run(["git", "init", "-b", "pr", str(wt)])
        _cfg(wt)
        (wt / "f").write_text("x\n", encoding="utf-8")
        git(wt, "add", "f")
        git(wt, "commit", "-m", "only")
        h = head(wt)
        ledger = tmp / "state.jsonl"
        _ledger("--file", str(ledger), "header", "set", "base_branch", "main")
        _ledger("--file", str(ledger), "add-row", "--pr", PR_NUMBER, "--branch", PR_BRANCH,
                "--head-sha", h, "--worktree", str(wt), "--tier", "STANDARD", "--status", "in_review")
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(ledger), "--pr", PR_NUMBER,
                                            "--worktree", str(wt), "--base", "main"])
        check(code == M.EXIT_PRECONDITION, f"an absent default remote is refused at exit 2 (code={code})")
        check('"refused": "no-remote"' in out, f"the refusal names the absent remote; got {out!r}")


def t_worktree_missing_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ledger = tmp / "state.jsonl"
        _ledger("--file", str(ledger), "header", "set", "base_branch", "main")
        _ledger("--file", str(ledger), "add-row", "--pr", PR_NUMBER, "--branch", PR_BRANCH,
                "--head-sha", "a" * 40, "--worktree", str(tmp / "nope"), "--tier", "STANDARD",
                "--status", "in_review")
        code, out, _ = capture_cli(M.main, ["run", "--ledger", str(ledger), "--pr", PR_NUMBER,
                                            "--worktree", str(tmp / "nope"), "--base", "main"])
        check(code == M.EXIT_PRECONDITION, f"a missing worktree is refused at exit 2 (code={code})")
        check('"refused": "worktree-missing"' in out, f"the refusal names the missing worktree; got {out!r}")


# --- push failure: local rebase preserved, ledger NOT written (exit 1) --------

def t_push_rejected_preserves_local_rebase():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({12: "12-BASE"})     # a clean far edit, so the rebase itself succeeds
        s.move_remote_pr()                   # remote pr moves past the worktree's stale tracking ref
        remote_pr_before = s.remote_pr_head()
        code, out, _ = s.invoke()
        check(code == M.EXIT_PARTIAL, f"a rejected force-with-lease exits {M.EXIT_PARTIAL} (code={code})")
        check('"pushed": false' in out, f"the report says the push did not land; got {out!r}")
        new_head = head(s.wt)
        check(new_head != s.orig_head,
              "the local rebase is PRESERVED after a push rejection — never auto-reset")
        check(s.orig_head in out, "orig_head is printed so the driver can reset if it chooses")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head,
              "the ledger is NOT written after a push failure — head_sha still the original")
        check(s.remote_pr_head() == remote_pr_before,
              "the remote PR branch was not clobbered (force-with-lease refused it)")


def t_variant_spelling_rebases_against_row_base():
    # `--base origin/main` against a row whose effective base is `main` is ACCEPTED by `base_agrees` (a
    # leading `origin/` on the argument is stripped). The operational fetch/rebase must target the ROW's
    # resolved base, so this runs `git fetch origin main` / rebase onto `origin/main` — the SAME clean rebase
    # as `--base main`, exit 0, remote pushed. Trusting the raw `--base` (the reverted bug) would instead run
    # `git fetch origin origin/main`, which has no such remote ref → `fetch-failed`, refused at exit 2. This
    # FAILS if the operational ref is taken from the raw `--base` rather than the row's effective base.
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({12: "12-BASE"})  # a clean FAR edit — the rebase itself succeeds
        argv = ["run", "--ledger", str(s.ledger), "--pr", PR_NUMBER, "--worktree", str(s.wt),
                "--base", "origin/main"]
        code, out, err = capture_cli(M.main, argv)
        check(code == M.EXIT_OK,
              f"--base origin/main must rebase against the row's effective base 'main' and exit 0 "
              f"(code={code}, out={out!r}, err={err!r})")
        new_head = head(s.wt)
        check(new_head != s.orig_head, "the clean rebase moved the worktree HEAD")
        check(s.remote_pr_head() == new_head,
              "the remote PR branch was force-with-lease pushed to the rebased head")


# --- --dry-run mutates nothing -----------------------------------------------

def t_dry_run_mutates_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        s = _scenario(tmp)
        s.advance_base({12: "12-BASE"})
        origin_main_before = head(s.wt, "refs/remotes/origin/main")
        code, out, _ = s.invoke("--dry-run")
        check(code == M.EXIT_OK, f"a dry-run whose preconditions pass exits 0 (code={code})")
        check('"dry_run": true' in out, f"the dry-run report says so; got {out!r}")
        check(head(s.wt) == s.orig_head, "dry-run does not rebase — HEAD unchanged")
        check(head(s.wt, "refs/remotes/origin/main") == origin_main_before,
              "dry-run stops BEFORE the fetch — the base tracking ref is unchanged")
        check(_field(s.ledger, PR_NUMBER, "head_sha") == s.orig_head, "dry-run does not write the ledger")
        check(s.remote_pr_head() == s.orig_head, "dry-run does not push")


CASES = [
    ("clean-rebase-e2e", "a clean base-only rebase pushes and updates the ledger, keeping reviews_ok",
     t_clean_rebase_end_to_end),
    ("noop-rebase-retains-base-ok-sha", "a no-op rebase (base unchanged) echoes the RETAINED base_ok_sha, "
     "not a reset — the result JSON reads the actual row", t_noop_rebase_echoes_retained_base_ok_sha),
    ("conflict-aborts", "a conflicting rebase aborts, restores HEAD, refuses (exit 3), mutates nothing",
     t_conflict_aborts_and_refuses),
    ("diff-changed-resets", "a textually-clean rebase that changes the PR diff resets and refuses (exit 3)",
     t_diff_changed_resets_and_refuses),
    ("indent-context-diff-changed",
     "a real indentation-only context change resets locally and refuses without touching remote or ledger",
     t_indentation_only_context_change_resets_and_refuses),
    ("patch-id-failure-before-rebase",
     "a failed pre-rebase patch-id refuses before rebase, push, or ledger write",
     t_patch_id_failure_before_rebase_refuses),
    ("patch-id-failure-after-rebase",
     "a failed post-rebase patch-id restores HEAD and refuses before push or ledger write",
     t_patch_id_failure_after_rebase_resets_and_refuses),
    ("dirty-refused", "a dirty worktree is refused at exit 2", t_dirty_worktree_refused),
    ("wrong-branch-refused", "a worktree on the wrong branch is refused at exit 2", t_wrong_branch_refused),
    ("head-mismatch-refused", "HEAD != ledger head_sha is refused at exit 2, naming both values",
     t_head_mismatch_refused),
    ("held-refused", "a held row is refused at exit 2 and never rebased", t_held_row_refused),
    ("no-row-refused", "a PR with no ledger row is refused at exit 2", t_no_row_refused),
    ("origin-named-base-agrees", "a base literally named origin/<x> matches itself; the bare form disagrees",
     t_origin_named_base_agrees),
    ("base-mismatch-refused", "a --base disagreeing with the row's effective base is refused at exit 2",
     t_base_mismatch_refused),
    ("variant-spelling-rebases-row-base",
     "an accepted origin/main spelling rebases against the row's effective base 'main', not the raw arg",
     t_variant_spelling_rebases_against_row_base),
    ("unresolved-base-refused", "a both-`-` ledger resolves to `-`; the unresolved base is refused as no-base at exit 2",
     t_unresolved_base_refused),
    ("no-remote-refused", "an absent default remote is refused at exit 2", t_absent_remote_refused),
    ("worktree-missing-refused", "a missing/non-git worktree is refused at exit 2", t_worktree_missing_refused),
    ("push-rejected-preserves-rebase", "a rejected push preserves the local rebase and does NOT write the "
     "ledger (exit 1)", t_push_rejected_preserves_local_rebase),
    ("dry-run-noop", "--dry-run mutates nothing (no fetch, rebase, push, or ledger write)",
     t_dry_run_mutates_nothing),
]
