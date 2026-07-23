#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `run-id.py` — the run-id minter and atomic run-directory creator.

They live in a SIBLING file, and `run-id.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH: the load-bearing one is that a run directory is created ATOMICALLY,
so two fresh runs can never share it. That is verified by forcing a collision (the mint must move to a
FRESH id) and by exhausting the retries (the mint must RAISE, never reuse a taken dir) — a minter that
quietly reused an existing directory would pass a naive "did it return an id?" check and be exactly the
double-drive bug this tool exists to prevent.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "run-id.py"

FIXED = datetime(2026, 7, 4, 9, 15)  # → stamp 260704-0915, matching the doc's worked example
RUN_ID_RE = r"g\d{6}-\d{4}-[0-9a-f]{8}"


def _load_owner():
    mod = load_module_from_path("run_id_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the run-id tool at {OWNER}")
    return mod


R = _load_owner()


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise R.SelfTestFailure(msg)


# --- the id shape -------------------------------------------------------------

def t_run_id_shape_is_fs_and_label_safe() -> None:
    rid = R.make_run_id(FIXED, "a3f29c1b")
    check(rid == "g260704-0915-a3f29c1b", f"run-id must be g<YYMMDD>-<HHMM>-<rand>, got {rid!r}")
    check(re.fullmatch(RUN_ID_RE, rid) is not None,
          "a run-id is a directory name AND part of the gauntlet-run-<id> label — it must be "
          "[a-z0-9-] only")


# --- the atomic create --------------------------------------------------------

def t_new_run_creates_the_directory() -> None:
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "tmp"
        rid, rundir = R.new_run(runs, now_fn=lambda: FIXED, rand_fn=lambda: "a3f29c1b")
        check(rid == "g260704-0915-a3f29c1b", "new_run returns the minted id")
        check(rundir == runs / rid, "the rundir is <runs>/<run-id>")
        check(rundir.is_dir(), "new_run must actually CREATE the run directory, not just name it")


def t_parent_runs_dir_is_created_if_absent() -> None:
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "deep" / "not-yet" / "tmp"  # parent chain does not exist
        _rid, rundir = R.new_run(runs, now_fn=lambda: FIXED, rand_fn=lambda: "a3f29c1b")
        check(rundir.is_dir(), "new_run must create the parent runs-dir chain when it is absent")


def t_a_collision_mints_a_fresh_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "tmp"
        taken = runs / R.make_run_id(FIXED, "aaaaaaaa")
        taken.mkdir(parents=True)  # another run already holds the first candidate's directory
        rands = iter(["aaaaaaaa", "bbbbbbbb"])
        rid, rundir = R.new_run(runs, now_fn=lambda: FIXED, rand_fn=lambda: next(rands))
        check(rid == "g260704-0915-bbbbbbbb",
              "a collision must mint a FRESH id (new suffix) — never adopt the taken directory")
        check(rundir.is_dir() and rundir != taken, "the fresh rundir is created and is a different dir")
        check(taken.is_dir(), "the colliding dir is left untouched — never overwritten")


def t_exhausting_retries_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "tmp"
        taken = runs / R.make_run_id(FIXED, "cccccccc")
        taken.mkdir(parents=True)
        raised = False
        try:
            # rand_fn never yields a fresh suffix, so every attempt collides.
            R.new_run(runs, now_fn=lambda: FIXED, rand_fn=lambda: "cccccccc", max_attempts=3)
        except R.RunIdCollision:
            raised = True
        check(raised, "exhausting the retries must RAISE RunIdCollision — fail closed, never reuse a dir")
        check(list(runs.iterdir()) == [taken], "a failed mint must leave NO extra directory behind")


# --- the CLI ------------------------------------------------------------------

def t_cli_new_prints_json_and_creates_the_dir() -> None:
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "tmp"
        code, out, err = capture_cli(R.main, ["new", "--runs-dir", str(runs)])
        check(code == 0, f"`new` must exit 0 on success; stderr={err!r}")
        payload = json.loads(out)
        check("run_id" in payload and "rundir" in payload, "`new` prints {run_id, rundir} as JSON")
        rid = payload["run_id"]
        check(re.fullmatch(RUN_ID_RE, rid) is not None, f"the printed run_id must be well-formed, got {rid!r}")
        check(payload["rundir"] == str(runs / rid), "the reported rundir is <runs>/<run-id>")
        check(Path(payload["rundir"]).is_dir(), "`new` must actually create the rundir it reports")


def t_cli_new_fails_closed_when_runs_dir_is_a_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        clash = Path(d) / "not-a-dir"
        clash.write_text("x", encoding="utf-8")  # a FILE sits where the runs-dir should be
        code, out, err = capture_cli(R.main, ["new", "--runs-dir", str(clash)])
        check(code != 0, "`new` must FAIL CLOSED (non-zero) when the runs-dir path is a file, not a dir")
        check(out.strip() == "", "a refused `new` prints NO run-id on stdout")
        check("REFUSED" in err, "the refusal must explain itself on stderr")


CASES = [
    ("id-shape", "a run-id is g<YYMMDD>-<HHMM>-<rand>, fs- and label-safe", t_run_id_shape_is_fs_and_label_safe),
    ("creates-dir", "new_run actually creates <runs>/<run-id>", t_new_run_creates_the_directory),
    ("parent-created", "the parent runs-dir chain is created if absent", t_parent_runs_dir_is_created_if_absent),
    ("collision-fresh-id", "a collision mints a FRESH id, never reuses the taken dir", t_a_collision_mints_a_fresh_id),
    ("exhaustion-fails-closed", "exhausting retries RAISES rather than reusing a dir", t_exhausting_retries_fails_closed),
    ("cli-new", "`new` prints {run_id, rundir} JSON and creates the dir", t_cli_new_prints_json_and_creates_the_dir),
    ("cli-fail-closed", "`new` fails closed when the runs-dir is a file", t_cli_new_fails_closed_when_runs_dir_is_a_file),
]
