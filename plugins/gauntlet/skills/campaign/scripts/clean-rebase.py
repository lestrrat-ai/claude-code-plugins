#!/usr/bin/env python3
"""EXECUTE the campaign's CLEAN base-only rebase — and REFUSE everything else, fail-closed.

`stage-2-review-gate.md`'s precondition-rebase site and `stage-3-merge.md`'s step-6 reconcile both
describe, in prose, the same mechanical act: when `<base>` has moved and the PR does not conflict, rebase
the PR onto the new base, and — **only if the PR's own diff is unchanged** — carry `reviews_ok` forward,
set `ci = pending`, and fire the head-move reset (`files-and-ledger.md`, the `head_sha` field,
"What a genuine head move resets", is the CANONICAL OWNER of that reset; this tool CITES it and never
re-decides it). That is git fetch/rebase/push plus a ledger write, transcribed
by a model every heartbeat. This turns the CLEAN case into a command.

**It does the CLEAN case and NOTHING else.** Conflict resolution is the driver's JUDGMENT and this tool
NEVER attempts it: the moment `git rebase` reports a conflict it `--abort`s, restores HEAD, and exits
`EXIT_NOT_CLEAN` — the driver takes over. And a rebase that applied textually but CHANGED the PR's own diff
(a base edit near the PR's hunk re-writes the PR's effective patch) is ALSO not the clean case: the PR's
patch identity is compared before and after, and a mismatch is `reset --hard`ed back and refused the same
way. The only path that mutates the remote or the ledger is a rebase that was textually clean AND left the
PR's patch identity byte-for-byte unchanged.

    clean-rebase.py run --ledger <state.jsonl> --pr <N> --worktree <path> --base <base> \
        [--remote origin] [--dry-run]
    clean-rebase.py self-test    run every fixture (clean-rebase-test.py)

Exit codes gate a caller's `$?`:
  0  the clean rebase landed (pushed + ledger written), or a --dry-run whose preconditions all pass
  2  a PRECONDITION refused it — nothing was mutated (no row, held/terminal, bad/dirty/stale worktree,
     branch mismatch, absent remote, or unavailable diff/patch identity). The caller fixes the stated
     condition and re-runs.
  3  NOT the clean case — a conflict, or the rebase changed the PR's diff. HEAD is restored; nothing
     pushed, ledger untouched. The driver falls back to the JUDGMENT path for BOTH subcases (resolve a
     conflict by hand where there is one, or accept the reshaped diff where there is none), then applies
     the gate-reset rules those docs own.
  1  a PARTIAL state the driver must resolve — most importantly a push that was REJECTED after a clean
     local rebase (the rebase is preserved locally; the ledger is NOT written, and orig_head is printed
     so the driver can decide). Never auto-reset here — that would silently destroy a completed rebase.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.argv import bind_separate_option_value
from _gauntlet.git_refs import select_base_fetch_refs
from _gauntlet.modules import load_module_from_path

DESCRIPTION = next(iter((__doc__ or "").splitlines()), "")

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "clean-rebase-test.py"     # the fixture suite — this tool's executable contract
LEDGER_PY = _HERE / "ledger.py"


def _load_ledger():
    mod = load_module_from_path("clean_rebase_ledger", LEDGER_PY)
    if mod is None:
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER_PY}")
    return mod


L = _load_ledger()

# TERMINAL statuses — a done PR is never rebased. `files-and-ledger.md` (`status`: "in_review -> merged,
# or aborted; plus the HELD (non-terminal) statuses") owns the enumeration; these are the two it names as
# terminal. HELD statuses are refused separately via `L.HELD_STATUSES` (the ledger module owns that set).
TERMINAL_STATUSES = ("merged", "aborted")

# The SHA-bound liveness counters this rebase resets. `stage-2-ci.md`, "THE LIVENESS COUNTERS", is the
# owner of the set and of the rule that EVERY `head_sha` change resets it; a clean base-only rebase moves
# `head_sha` (without resetting the gate), so it is one of those sites. The reset VALUES come from
# `ledger.py`'s ROW_DEFAULTS (the fresh-head defaults), never literals typed here — same discipline as
# `pr-adopt.py`'s re-adoption reset.
LIVENESS_COUNTERS = ("ci_fingerprint", "settled_strikes", "unusable_refetches", "ci_stalled_since")

# Exit codes (see the module docstring for what each means to the caller).
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_PRECONDITION = 2
EXIT_NOT_CLEAN = 3

# The patch identity of an EMPTY diff — its OWN value, distinct from every real patch id (which is 40
# lowercase hex, so this string can never collide). A PR whose diff was empty on BOTH sides compares equal
# and is clean; empty-vs-nonempty is a change like any other.
EMPTY_DIFF = "empty-diff"


# --- git / ledger plumbing ----------------------------------------------------

def _run(argv: list[str], *, cwd: "str | None" = None,
         stdin: "str | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, check=False, cwd=cwd, input=stdin)


def _git(worktree: str, *args: str) -> subprocess.CompletedProcess:
    """Every git command runs against the worktree via `git -C <worktree>`."""
    return _run(["git", "-C", worktree, *args])


def patch_identity(worktree: str, base_ref: str) -> "str | None":
    """The PR's MERGE-BASE-relative patch identity: `git diff <base-ref>...HEAD` piped through `git patch-id
    --verbatim`. Verbatim mode preserves whitespace, so an indentation-only context change cannot carry
    review credit. Returns the 40-hex id, `EMPTY_DIFF` for an empty diff, or None if the diff or patch
    identity could not be computed. Three-dot diff is measured from the merge base, so the SAME value is
    produced whether the PR sits on the old or the new base — which is exactly what lets a before/after
    comparison isolate what the REBASE did to the PR's content."""
    diff = _git(worktree, "diff", f"{base_ref}...HEAD")
    if diff.returncode != 0:
        return None
    pid = _run(["git", "patch-id", "--verbatim"], cwd=worktree, stdin=diff.stdout)
    if pid.returncode != 0:
        return None
    out = pid.stdout.strip()
    if not out:
        return EMPTY_DIFF
    return out.split()[0]


# --- output -------------------------------------------------------------------

def emit(obj: dict) -> None:
    print(json.dumps(obj))


def refuse(kind: str, message: str, code: int) -> int:
    """Print a structured refusal to stdout (machine-parseable) and a human line to stderr, and return the
    gating exit code. Used for every non-success outcome so a caller can gate on `$?` OR read the reason."""
    emit({"refused": kind, "detail": message})
    print(f"clean-rebase: REFUSED ({kind}) — {message}", file=sys.stderr)
    return code


# --- the run ------------------------------------------------------------------

def _is_git_worktree(worktree: str) -> bool:
    if not Path(worktree).is_dir():
        return False
    r = _git(worktree, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def run(args) -> int:
    ledger_path = Path(args.ledger)
    pr = str(args.pr)
    worktree = args.worktree
    base = args.base
    remote = args.remote

    # --- PRECONDITIONS: each fails CLOSED at exit 2, mutating NOTHING ----------

    # 1. The ledger row must exist — there is nothing to rebase for a PR the run does not track.
    header, rows = L.load(ledger_path)
    row = L.find_row(rows, pr)
    if row is None:
        return refuse("no-row", f"no ledger row for pr {pr} — adopt it first (`pr-adopt.py adopt`)",
                      EXIT_PRECONDITION)

    # 1a. `--base` is an ASSERTION, not a base source: the ROW owns the base. It must equal the row's
    #     `effective_base` (its explicit `base_branch`, else the legacy header fallback — resolved through
    #     `ledger.py`'s accessor, never a second copy of that rule). An UNRESOLVED base (blank or the `-`
    #     sentinel) is refused FIRST through `ledger.py`'s `require_effective_base` — the one owner of that
    #     fail-closed rule — so a `-` base is never treated as a real branch. Then refuse a disagreement BEFORE
    #     any fetch/rebase so a caller can never rebase this PR onto a branch the row does not track. Agreement
    #     is decided by `ledger.py`'s `base_agrees` — the one owner of that comparison. (A base that merely
    #     ADVANCED — same branch, new commits — is exactly what this rebase HANDLES; only a different branch
    #     NAME disagrees.)
    effective_base, base_problem = L.require_effective_base(header, row, pr)
    if base_problem is not None:
        return refuse("no-base", base_problem, EXIT_PRECONDITION)
    if not L.base_agrees(base, effective_base):
        return refuse("base-mismatch",
                      f"--base {base!r} disagrees with pr {pr}'s ledger effective base {effective_base!r} — "
                      f"--base is an assertion, not a base source", EXIT_PRECONDITION)
    # Operate on the ROW's resolved base, never the raw `--base` spelling: two spellings `base_agrees`
    # accepts (`main` vs `origin/main`) produce different fully qualified source/tracking refspecs, so every
    # operational fetch/rebase/patch-identity below follows the row, not the caller's argument.
    base = effective_base

    # 2. A HELD PR is FROZEN — no rebase (a mutation) is dispatched on it — and a TERMINAL PR is done.
    status = row.get("status", "-")
    if status in L.HELD_STATUSES:
        return refuse("held", f"pr {pr} is {status} — {L.held_reason(status)}; a held PR is never rebased",
                      EXIT_PRECONDITION)
    if status in TERMINAL_STATUSES:
        return refuse("terminal", f"pr {pr} is {status} (terminal) — a done PR is never rebased",
                      EXIT_PRECONDITION)

    # 3. The worktree must exist and be a real git worktree.
    if not _is_git_worktree(worktree):
        return refuse("worktree-missing",
                      f"{worktree} is missing or is not a git worktree — cannot rebase there",
                      EXIT_PRECONDITION)

    # 4. The remote must exist. `--remote` defaults to `origin`; an ABSENT remote is refused, never guessed
    #    at (fetch/push would fail mid-flight otherwise).
    if _git(worktree, "remote", "get-url", remote).returncode != 0:
        return refuse("no-remote", f"remote {remote!r} is not configured in {worktree}", EXIT_PRECONDITION)

    # 5. A DIRTY tree is NEVER rebased — uncommitted work would be destroyed or entangled. Nothing ignored.
    st = _git(worktree, "status", "--porcelain")
    if st.returncode != 0:
        return refuse("worktree-error", f"`git status` failed in {worktree}: {st.stderr.strip()}",
                      EXIT_PRECONDITION)
    if st.stdout.strip():
        return refuse("dirty", f"{worktree} has uncommitted changes — refusing to rebase a dirty tree",
                      EXIT_PRECONDITION)

    # 6. The worktree must have the ROW'S branch checked out (not detached, not a different branch).
    br = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if br.returncode != 0:
        return refuse("worktree-error", f"cannot read the checked-out branch in {worktree}: "
                      f"{br.stderr.strip()}", EXIT_PRECONDITION)
    checked_out = br.stdout.strip()
    row_branch = row.get("branch", "-")
    if checked_out != row_branch:
        return refuse("wrong-branch",
                      f"{worktree} has {checked_out!r} checked out but pr {pr}'s row branch is "
                      f"{row_branch!r} — rebase the branch the row names, not whatever is checked out",
                      EXIT_PRECONDITION)

    # 7. The worktree HEAD must equal the row's recorded head_sha. A mismatch means the checkout or the
    #    ledger is STALE, and rebasing either would corrupt the gate; name which value is which and send the
    #    caller to pr-adoption's refresh to reconcile them FIRST.
    hv = _git(worktree, "rev-parse", "HEAD")
    if hv.returncode != 0:
        return refuse("worktree-error", f"cannot read HEAD in {worktree}: {hv.stderr.strip()}",
                      EXIT_PRECONDITION)
    orig_head = hv.stdout.strip()
    row_head = row.get("head_sha", "-")
    if orig_head != row_head:
        return refuse("stale",
                      f"worktree HEAD is {orig_head} but pr {pr}'s ledger head_sha is {row_head} — the "
                      f"checkout or the ledger is stale; reconcile them via pr-adoption's refresh "
                      f"(`pr-adopt.py adopt`) before rebasing",
                      EXIT_PRECONDITION)

    # Fully qualify both sides of the refspec so a legal dash-leading base is ref data, never a Git option.
    # A symbolic normal destination gets a private ref; every later diff/rebase uses that exact full ref.
    selected, selection_problem = select_base_fetch_refs(worktree, remote, base)
    if selection_problem is not None or selected is None:
        return refuse("fetch-ref",
                      selection_problem or "could not select a base fetch destination",
                      EXIT_PRECONDITION)
    fetch_refspec = selected.refspec
    base_ref = selected.local_ref

    # --- --dry-run STOPS HERE — before the first mutation (fetch moves a tracking ref) ------------------
    if args.dry_run:
        emit({"dry_run": True, "pr": pr, "worktree": worktree, "base": base, "remote": remote,
              "orig_head": orig_head, "branch": row_branch,
              "would": f"fetch {remote} {fetch_refspec}; rebase onto {base_ref}; verify the PR diff is "
                       f"unchanged; push --force-with-lease; then set head_sha, ci=pending and reset the "
                       f"liveness counters in the ledger"})
        return EXIT_OK

    # --- EXECUTION ------------------------------------------------------------

    # Fetch the base BEFORE computing the comparison target, so both the target and the post-rebase
    # identity are measured against the SAME (updated) base ref.
    fetch = _git(worktree, "fetch", remote, fetch_refspec)
    if fetch.returncode != 0:
        return refuse("fetch-failed",
                      f"`git fetch {remote} {fetch_refspec}` failed: {fetch.stderr.strip()}",
                      EXIT_PRECONDITION)

    # The COMPARISON TARGET — the PR's patch identity as it stands now, measured against the fetched base.
    # (Three-dot is merge-base-relative, so this equals the PR's pre-rebase content; see patch_identity.)
    target_id = patch_identity(worktree, base_ref)
    if target_id is None:
        return refuse("diff-failed",
                      f"could not compute the patch identity for {base_ref}...HEAD in {worktree} "
                      f"(git diff or git patch-id failed) — nothing rebased", EXIT_PRECONDITION)

    # The rebase. ANY non-zero exit is a conflict this tool NEVER resolves: abort, restore HEAD, hand off.
    rebase = _git(worktree, "rebase", base_ref)
    if rebase.returncode != 0:
        _git(worktree, "rebase", "--abort")
        after = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        if after != orig_head:
            # `--abort` did not restore HEAD — a partial state no clean-only tool may leave silently.
            print(f"clean-rebase: rebase --abort left HEAD at {after}, not the original {orig_head} — "
                  f"the worktree is in a PARTIAL state; resolve it by hand", file=sys.stderr)
            emit({"error": "abort-did-not-restore", "pr": pr, "orig_head": orig_head, "head_now": after})
            return EXIT_PARTIAL
        return refuse("conflict",
                      f"rebase of pr {pr} onto {base_ref} conflicts — aborted, HEAD restored to "
                      f"{orig_head}. Conflict resolution is the driver's call; this tool does the clean "
                      f"case only", EXIT_NOT_CLEAN)

    # Textually clean — but did it change the PR's OWN diff? Compare patch identities.
    post_id = patch_identity(worktree, base_ref)
    if post_id != target_id:
        # NOT the clean case: the rebase re-wrote the PR's effective patch (e.g. a base edit next to the
        # PR's hunk), or its post-rebase identity could not be proved. Fail closed — reset to the original
        # head and hand off to the conflict-class path.
        _git(worktree, "reset", "--hard", orig_head)
        restored = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        if restored != orig_head:
            print(f"clean-rebase: reset --hard left HEAD at {restored}, not {orig_head} — PARTIAL state",
                  file=sys.stderr)
            emit({"error": "reset-did-not-restore", "pr": pr, "orig_head": orig_head, "head_now": restored})
            return EXIT_PARTIAL
        if post_id is None:
            return refuse("diff-failed",
                          f"could not compute pr {pr}'s patch identity after rebasing onto {base_ref} — "
                          f"reset to {orig_head}; nothing pushed and the ledger is "
                          f"untouched", EXIT_PRECONDITION)
        return refuse("diff-changed",
                      f"the rebase applied textually but CHANGED pr {pr}'s diff (patch identity "
                      f"{target_id} -> {post_id}) — reset to {orig_head}. This is not a clean base-only "
                      f"rebase; the driver's judgment path owns it", EXIT_NOT_CLEAN)

    new_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    # Clean AND diff-preserving — the ONLY path that mutates the remote. Never `--force`, only
    # `--force-with-lease`: a concurrent push to this branch makes the lease fail rather than clobber.
    push = _git(worktree, "push", "--force-with-lease", remote, row_branch)
    if push.returncode != 0:
        # The rebase is done and correct LOCALLY; only the push was rejected. Do NOT auto-reset — that
        # would silently destroy the completed rebase. Do NOT write the ledger — head_sha is not yet the
        # remote truth. Report honestly and print orig_head so the driver can reset if it chooses.
        print(f"clean-rebase: pr {pr} rebased cleanly to {new_head} but `push --force-with-lease` was "
              f"REJECTED: {push.stderr.strip()}. The rebase is preserved locally and the ledger was NOT "
              f"written. Driver decides (orig_head {orig_head}).", file=sys.stderr)
        emit({"pushed": False, "pr": pr, "orig_head": orig_head, "new_head": new_head, "base": base,
              "ledger_written": False, "reason": "push rejected — force-with-lease lease failed"})
        return EXIT_PARTIAL

    # ONE ledger write, through the ledger module: carry `reviews_ok` and the labels FORWARD (the clean
    # case KEEPS the gate — the "clean base-only rebase" Exception in stage-2-review-gate.md, "Status labels
    # mirror the review gate", is the owner). It IS a head_sha change, so: new head_sha, ci=pending, and
    # reset the four liveness counters to their fresh-head ROW_DEFAULTS. `reviews_ok`/labels are NOT touched.
    ledger_argv = ["python3", str(LEDGER_PY), "--file", str(ledger_path), "set", "--pr", pr,
                   "--head-sha", new_head, "--ci", "pending"]
    for field in LIVENESS_COUNTERS:
        ledger_argv += [f"--{field.replace('_', '-')}", str(L.ROW_DEFAULTS[field])]
    lw = _run(ledger_argv, cwd=str(ledger_path.parent))
    if lw.returncode != 0:
        # The push LANDED but the ledger did not update — a partial the driver must reconcile (the remote
        # is at new_head; the ledger still says orig_head). Never silently swallow it.
        print(f"clean-rebase: pr {pr} pushed to {new_head} but the ledger write FAILED: "
              f"{lw.stderr.strip()}. Reconcile the row to head_sha {new_head} by hand.", file=sys.stderr)
        emit({"pushed": True, "pr": pr, "orig_head": orig_head, "new_head": new_head, "base": base,
              "ledger_written": False, "reason": "ledger set failed after a successful push"})
        return EXIT_PARTIAL

    # Echo the ACTUAL post-write ledger values, re-read from the row `set` just wrote — NEVER the fresh-head
    # ROW_DEFAULTS. `apply_head_sha` voids `base_ok_sha` ONLY on a genuine head move; on a same-head no-op
    # (the base did not advance, so `new_head == orig_head`) it resets nothing and `base_ok_sha` keeps its
    # stamp — hardcoding the ROW_DEFAULTS reset here would make the echo LIE about that case. Reading the row
    # reports whichever actually happened: a reset `-` on a real move, the retained stamp on a no-op. This
    # keeps every reported field consistent with the stored row (the liveness counters, force-written to
    # their defaults by the explicit flags above, read the same value either way).
    _, written_rows = L.load(ledger_path)
    written = L.find_row(written_rows, pr) or {}
    emit({"pr": pr, "old_head": orig_head, "new_head": new_head, "base": base, "pushed": True,
          "ledger": {"head_sha": str(written.get("head_sha", new_head)),
                     "ci": str(written.get("ci", "pending")),
                     "base_ok_sha": str(written.get("base_ok_sha", L.ROW_DEFAULTS["base_ok_sha"])),
                     **{f: str(written.get(f, L.ROW_DEFAULTS[f])) for f in LIVENESS_COUNTERS}}})
    return EXIT_OK


# --- self-test: the executable contract lives in the SIBLING module -----------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run and CANNOT report "
            f"health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("clean_rebase_test", SIBLING, register=True)
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
        print("\n1 check(s) FAILED — the clean-rebase tool's contract is broken.")
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
        print(f"{failures} check(s) FAILED — the clean-rebase tool's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the clean-rebase tool's contract is intact.")
    return 0


# --- CLI ----------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=DESCRIPTION)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="execute the clean base-only rebase, or refuse if it is not clean")
    r.add_argument("--ledger", required=True, help="the run ledger (<rundir>/state.jsonl)")
    r.add_argument("--pr", required=True, help="PR number (row key)")
    r.add_argument("--worktree", required=True, help="the PR-head worktree to rebase in")
    r.add_argument("--base", required=True, help="the base branch to rebase onto")
    r.add_argument("--remote", default="origin", help="the git remote (default: origin; refused if absent)")
    r.add_argument("--dry-run", action="store_true", help="check preconditions and report; mutate nothing")

    sub.add_parser("self-test", help="run every fixture (clean-rebase-test.py)")

    args = p.parse_args(bind_separate_option_value(argv, "--base"))
    if args.cmd == "self-test":
        return self_test()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
