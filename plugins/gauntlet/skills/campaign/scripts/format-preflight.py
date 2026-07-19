#!/usr/bin/env python3
"""Refuse to format a file whose write could land OUTSIDE the worktree.

The cheap CI-fix subagent runs a formatter (`gofmt -w`, …) that writes bytes back through the path it is
given. A formatter writes THROUGH a symlink: point one at a file elsewhere on the machine and the bytes
land there, while `git diff` inside the worktree shows NOTHING — the one write the diff-reading model
cannot see. `stage-2-ci.md` ("The cheap CI-fix subagent", the PREFLIGHT hard rule) states the guard in
prose a worker executed by hand with `lstat`; this turns that prose into a COMMAND, so the check does not
depend on a model remembering to `lstat` every component.

A file is REFUSED when its write could escape the worktree, decided in order:
  (a) the worktree must exist and be a directory, or the whole run is refused (operator error);
  (b) EVERY directory component from the worktree root down to the file's parent is checked: a component
      that is a symlink refuses the file (the formatter would write THROUGH it); a component that does not
      exist refuses it (a file that is not there is not a file you format);
  (c) the file itself: a missing file, a symlink, or anything that is not a regular file (a directory,
      fifo, socket, device) is refused.

WHY `os.lstat`, NEVER `stat`, and NEVER `realpath` as the primary test: `stat` FOLLOWS symlinks, so it
reports the target and hides the very link the check exists to see; `os.path.realpath` collapses the whole
chain of links before we can look at any single component, so it ERASES the escape instead of catching it.
The check therefore walks the path one component at a time and `lstat`s each, so a symlink is seen AS a
symlink. The under-worktree test is likewise LEXICAL (compare normalized paths, no `realpath`) for the same
reason: resolving the path would follow the links we are trying to detect.

THE WORKTREE MAY ITSELF LIVE BEHIND A SYMLINK, and that is allowed — worktrees legitimately sit under a
symlinked home. So the worktree path is NOT resolved through `realpath`; every component is compared
RELATIVE to the worktree, and only the components BELOW the worktree are what the walk inspects. A
symlinked worktree root is fine; a symlinked component inside it is not.

FOOTGUN GUARD, NOT A SECURITY BOUNDARY. This stops a real accident — a vendored symlink walking the
formatter out of the tree to leave a confusing dirty file in another project. It is NOT a defence against
a malicious committer (campaign adopts same-repo PRs only; whoever commits a symlink already has write
access). `stage-2-ci.md`, "THE PRINCIPLE" / "STATE IT HONESTLY", owns that framing; do not generalise this
tool into anything more.

The tool only READS (`os.lstat`, `os.path`): it never writes, never formats, never follows a link.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "format-preflight-test.py"     # the fixture suite — this tool's executable contract

# Exit codes are the worker's signal, and each is distinct:
#   0  every named file is OK to format.
#   3  at least one file is refused — format ONLY the `ok` list; if none are ok, ESCALATE (stage-2-ci.md).
#   2  operator error (no worktree / not a directory / no files) — the caller passed something unusable.
EXIT_OK = 0
EXIT_OPERATOR = 2
EXIT_REFUSED = 3


def _ok(file_arg: str) -> dict:
    return {"file": file_arg, "status": "ok", "reason": None}


def _refused(file_arg: str, reason: str) -> dict:
    return {"file": file_arg, "status": "refused", "reason": reason}


def _normalize(worktree_abs: str, file_arg: str) -> "tuple[str, list[str] | None, str | None]":
    """Resolve `file_arg` against the worktree LEXICALLY and return `(file_abs, parts, error)`.

    PURE — no filesystem access. `error` is `"outside-worktree"` when the path (an absolute one, or a
    relative one that climbs out with `..`) does not sit under the worktree; then `parts` is `None`.
    Otherwise `parts` is the path components BELOW the worktree, in order, with `.`/empty segments dropped
    (`[]` when `file_arg` names the worktree itself). Normalization is lexical on purpose — `realpath`
    would collapse the symlinks the walk exists to catch (see the module docstring).
    """
    if os.path.isabs(file_arg):
        file_abs = os.path.normpath(file_arg)
    else:
        file_abs = os.path.normpath(os.path.join(worktree_abs, file_arg))
    rel = os.path.relpath(file_abs, worktree_abs)
    # `..` (exactly, or as the first segment) is the only way a lexical relpath climbs above the base; an
    # absolute path elsewhere yields one too. Either way the write would land outside the worktree.
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return (file_abs, None, "outside-worktree")
    parts = [p for p in rel.split(os.sep) if p not in ("", os.curdir)]
    return (file_abs, parts, None)


def check_file(worktree_abs: str, file_arg: str) -> dict:
    """Preflight ONE file under an already-normalized worktree. Returns a result dict; reads only.

    `worktree_abs` must be an absolute, normalized directory path (see `run_check`). The walk `lstat`s each
    component shallow-to-deep and RETURNS on the first refusal, so it never `lstat`s THROUGH a symlink it
    has already flagged.
    """
    _file_abs, parts, err = _normalize(worktree_abs, file_arg)
    if err is not None:
        return _refused(file_arg, err)
    assert parts is not None

    # (b) Every directory component from the worktree root down to the file's PARENT (parts[:-1]).
    for i in range(len(parts) - 1):
        comp = parts[i]
        comp_path = os.path.join(worktree_abs, *parts[: i + 1])
        try:
            st = os.lstat(comp_path)
        except (FileNotFoundError, NotADirectoryError):
            # Missing, or a non-directory earlier in the chain makes this component unreachable — either
            # way the file is not there to format.
            return _refused(file_arg, f"missing:{comp}")
        if stat.S_ISLNK(st.st_mode):
            return _refused(file_arg, f"symlink-parent:{comp}")

    # (c) The file itself (or the worktree root when `parts` is empty — a directory, caught below).
    target = os.path.join(worktree_abs, *parts) if parts else worktree_abs
    try:
        st = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return _refused(file_arg, "missing")
    if stat.S_ISLNK(st.st_mode):
        return _refused(file_arg, "symlink")
    if not stat.S_ISREG(st.st_mode):
        return _refused(file_arg, "not-a-regular-file")
    return _ok(file_arg)


def run_check(worktree: str, files: "list[str]") -> "tuple[dict, int]":
    """Preflight every file. Returns `(payload, exit_code)`; performs only reads.

    Operator errors (exit 2) — a worktree that is not a directory, or an empty file list — return an
    `{"error": ...}` payload and never inspect a file. Otherwise the payload carries `worktree`, the
    per-file `results`, and `counts`; exit 0 iff every file is ok, else exit 3.
    """
    worktree_abs = os.path.abspath(worktree)
    # `isdir` FOLLOWS symlinks by design: a worktree behind a symlinked home is legitimate and allowed.
    if not os.path.isdir(worktree_abs):
        return ({"error": f"worktree is not a directory: {worktree}", "worktree": worktree_abs},
                EXIT_OPERATOR)
    if not files:
        return ({"error": "a preflight of nothing checks nothing", "worktree": worktree_abs},
                EXIT_OPERATOR)

    results = [check_file(worktree_abs, f) for f in files]
    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_refused = len(results) - n_ok
    payload = {
        "worktree": worktree_abs,
        "results": results,
        "counts": {"ok": n_ok, "refused": n_refused, "total": len(results)},
    }
    return (payload, EXIT_OK if n_refused == 0 else EXIT_REFUSED)


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="refuse any file whose formatter-write could escape the worktree")
    c.add_argument("--worktree", required=True, help="the worktree the writes must stay inside")
    # nargs='*' (not '+') so that ZERO files is OUR operator error with a message, not argparse's bare exit.
    c.add_argument("files", nargs="*", help="files to preflight (relative to --worktree, or absolute)")

    sub.add_parser("self-test", help="run every fixture (format-preflight-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    payload, code = run_check(args.worktree, args.files)
    print(json.dumps(payload))
    return code


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run and CANNOT report "
            f"health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("format_preflight_test", SIBLING, register=True)
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
        print(f"FAIL     {'sibling-fixtures':32} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the format-preflight guard's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:32} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:32} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the format-preflight guard's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the format-preflight guard's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
