#!/usr/bin/env python3
"""Fixtures for `format-preflight.py` — the formatter-write-escape guard.

They live in a SIBLING file, and `format-preflight.py self-test` FAILS LOUDLY if it cannot load them.

Every fixture builds a REAL temp tree and drives the guard's real `os.lstat` walk — there is no mocking,
because the whole point of the tool is what the filesystem actually reports about links and components.
EACH FIXTURE PINS A RULE WITH TEETH: it asserts a refusal on one side of a boundary AND an `ok` on the
other (a regular file where a symlink is refused, a real component where a symlinked one is refused), so a
guard that returned a constant verdict would go red.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "format-preflight.py"


def _load_owner():
    mod = load_module_from_path("format_preflight_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the format-preflight guard at {OWNER}")
    return mod


R = _load_owner()


def check(cond, msg):
    if not cond:
        raise R.SelfTestFailure(msg)


def _result_for(worktree: str, file_arg: str) -> dict:
    """Drive the whole `run_check` path (worktree normalization included) and return the one result."""
    payload, _code = R.run_check(worktree, [file_arg])
    results = payload["results"]
    check(len(results) == 1, f"expected exactly one result, got {results!r}")
    return results[0]


def _refused_reason(worktree: str, file_arg: str) -> str:
    r = _result_for(worktree, file_arg)
    check(r["status"] == "refused", f"{file_arg!r} must be refused, got {r!r}")
    return r["reason"]


# --- a plain regular file is OK ----------------------------------------------

def t_regular_file_is_ok():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.txt").write_text("x\n", encoding="utf-8")
        r = _result_for(d, "f.txt")
        check(r["status"] == "ok" and r["reason"] is None,
              f"a plain regular file must read ok with no reason, got {r!r}")
        payload, code = R.run_check(d, ["f.txt"])
        check(code == R.EXIT_OK, f"an all-ok run must exit {R.EXIT_OK}, got {code}")
        check(payload["counts"] == {"ok": 1, "refused": 0, "total": 1},
              f"counts must tally one ok, got {payload['counts']!r}")


# --- the file IS a symlink ----------------------------------------------------

def t_file_that_is_a_symlink_is_refused():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "real.txt").write_text("x\n", encoding="utf-8")
        os.symlink("real.txt", str(Path(d) / "link.txt"))
        check(_refused_reason(d, "link.txt") == "symlink",
              "a file that IS a symlink must be refused 'symlink' (the formatter writes THROUGH it)")
        # teeth: the real target it points at reads ok — the tool is not refusing everything.
        check(_result_for(d, "real.txt")["status"] == "ok",
              "the real file the link points at must read ok")


# --- a symlinked PARENT directory --------------------------------------------

def t_file_under_symlinked_dir_is_refused_naming_component():
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(Path(d) / "realdir")
        (Path(d) / "realdir" / "f.txt").write_text("x\n", encoding="utf-8")
        os.symlink("realdir", str(Path(d) / "linkdir"))
        check(_refused_reason(d, "linkdir/f.txt") == "symlink-parent:linkdir",
              "a file under a symlinked dir must be refused 'symlink-parent:linkdir', naming the component")
        # teeth: the same file via the REAL directory reads ok.
        check(_result_for(d, "realdir/f.txt")["status"] == "ok",
              "the same file reached through the real directory must read ok")


def t_symlinked_component_deep_in_path():
    # a/blink/c where blink is a symlink deep in the path — the walk must catch it AT that component.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(Path(d) / "a" / "realb")
        (Path(d) / "a" / "realb" / "c").write_text("x\n", encoding="utf-8")
        os.symlink("realb", str(Path(d) / "a" / "blink"))  # a/blink -> a/realb
        check(_refused_reason(d, "a/blink/c") == "symlink-parent:blink",
              "a symlinked component DEEP in the path must be refused 'symlink-parent:blink'")
        # teeth: the real path a/realb/c reads ok — only the linked component is refused.
        check(_result_for(d, "a/realb/c")["status"] == "ok",
              "the deep file via its real components must read ok")


# --- missing file, and a non-regular file ------------------------------------

def t_missing_file_is_refused():
    with tempfile.TemporaryDirectory() as d:
        check(_refused_reason(d, "nope.txt") == "missing",
              "a file that is not there must be refused 'missing'")


def t_directory_passed_as_file_is_refused():
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(Path(d) / "somedir")
        check(_refused_reason(d, "somedir") == "not-a-regular-file",
              "a directory handed in as a file must be refused 'not-a-regular-file'")


# --- absolute paths: inside is OK, outside is refused ------------------------

def t_absolute_path_inside_worktree_is_ok():
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(Path(d) / "sub")
        (Path(d) / "sub" / "f.txt").write_text("x\n", encoding="utf-8")
        abs_inside = str(Path(d) / "sub" / "f.txt")
        r = _result_for(d, abs_inside)
        check(r["status"] == "ok", f"an absolute path INSIDE the worktree must read ok, got {r!r}")


def t_absolute_path_outside_worktree_is_refused():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
        outside = Path(other) / "elsewhere.txt"
        outside.write_text("x\n", encoding="utf-8")
        check(_refused_reason(d, str(outside)) == "outside-worktree",
              "an absolute path OUTSIDE the worktree must be refused 'outside-worktree'")
        # teeth: a relative path that climbs out with `..` is the same refusal, from the same lexical test.
        check(_refused_reason(d, "../climb-out.txt") == "outside-worktree",
              "a relative path climbing out with `..` must be refused 'outside-worktree'")


# --- operator errors: no files, and no worktree ------------------------------

def t_zero_files_is_operator_error():
    with tempfile.TemporaryDirectory() as d:
        payload, code = R.run_check(d, [])
        check(code == R.EXIT_OPERATOR, f"zero files must exit {R.EXIT_OPERATOR}, got {code}")
        check("error" in payload and "nothing" in payload["error"],
              f"a zero-file run must carry an explanatory error, got {payload!r}")


def t_missing_worktree_refuses_whole_run():
    with tempfile.TemporaryDirectory() as d:
        gone = str(Path(d) / "no-such-worktree")
        payload, code = R.run_check(gone, ["f.txt"])
        check(code == R.EXIT_OPERATOR,
              f"a worktree that is not a directory must exit {R.EXIT_OPERATOR}, got {code}")
        check("error" in payload, "a missing worktree must return an error payload, inspecting no file")


def t_file_passed_as_worktree_refuses_whole_run():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "not-a-dir"
        f.write_text("x\n", encoding="utf-8")
        payload, code = R.run_check(str(f), ["anything"])
        check(code == R.EXIT_OPERATOR,
              f"a regular file passed as the worktree must exit {R.EXIT_OPERATOR}, got {code}")
        check("error" in payload, "a non-directory worktree must return an error payload, inspecting no file")


# --- a worktree BEHIND a symlink is allowed (the docstring boundary) ----------

def t_symlinked_worktree_root_is_allowed():
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(Path(d) / "realwt")
        (Path(d) / "realwt" / "f.txt").write_text("x\n", encoding="utf-8")
        os.symlink("realwt", str(Path(d) / "wtlink"))
        payload, code = R.run_check(str(Path(d) / "wtlink"), ["f.txt"])
        check(code == R.EXIT_OK,
              "a worktree reached through a symlinked root is allowed — its files must still preflight ok")
        check(payload["results"][0]["status"] == "ok",
              "a file directly under a symlinked worktree root must read ok, not symlink-parent")


# --- a mixed list: per-file results, correct counts, exit 3 ------------------

def t_mixed_list_reports_per_file_and_exits_refused():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "good.txt").write_text("x\n", encoding="utf-8")
        (Path(d) / "real.txt").write_text("x\n", encoding="utf-8")
        os.symlink("real.txt", str(Path(d) / "badlink"))
        payload, code = R.run_check(d, ["good.txt", "badlink", "missing.txt"])
        check(code == R.EXIT_REFUSED, f"a list with any refusal must exit {R.EXIT_REFUSED}, got {code}")
        check(payload["counts"] == {"ok": 1, "refused": 2, "total": 3},
              f"counts must tally 1 ok / 2 refused / 3 total, got {payload['counts']!r}")
        by_file = {r["file"]: r for r in payload["results"]}
        check(by_file["good.txt"]["status"] == "ok", "good.txt must be ok")
        check(by_file["badlink"]["reason"] == "symlink", "badlink must be refused 'symlink'")
        check(by_file["missing.txt"]["reason"] == "missing", "missing.txt must be refused 'missing'")
        # order is preserved: results follow the argument order the worker passed.
        check([r["file"] for r in payload["results"]] == ["good.txt", "badlink", "missing.txt"],
              "results must preserve the input file order")


CASES = [
    ("regular-ok", "a plain regular file reads ok", t_regular_file_is_ok),
    ("file-is-symlink", "a file that is a symlink is refused 'symlink'", t_file_that_is_a_symlink_is_refused),
    ("symlinked-parent", "a symlinked parent dir refuses, naming the component",
     t_file_under_symlinked_dir_is_refused_naming_component),
    ("symlinked-deep", "a symlinked component deep in the path is caught", t_symlinked_component_deep_in_path),
    ("missing-file", "a missing file is refused 'missing'", t_missing_file_is_refused),
    ("dir-as-file", "a directory handed in as a file is refused", t_directory_passed_as_file_is_refused),
    ("abs-inside-ok", "an absolute path inside the worktree reads ok", t_absolute_path_inside_worktree_is_ok),
    ("abs-outside-refused", "an absolute (or `..`) path outside the worktree is refused",
     t_absolute_path_outside_worktree_is_refused),
    ("zero-files-operator", "zero files is operator error (exit 2)", t_zero_files_is_operator_error),
    ("missing-worktree", "a missing worktree refuses the whole run (exit 2)", t_missing_worktree_refuses_whole_run),
    ("file-as-worktree", "a file passed as the worktree refuses the whole run", t_file_passed_as_worktree_refuses_whole_run),
    ("symlinked-worktree-ok", "a worktree behind a symlink is allowed", t_symlinked_worktree_root_is_allowed),
    ("mixed-list", "a mixed list reports per-file and exits 3 with correct counts",
     t_mixed_list_reports_per_file_and_exits_refused),
]
