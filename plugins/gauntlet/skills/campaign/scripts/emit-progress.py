#!/usr/bin/env python3
"""Append one canonical reviewer progress event — the reviewer's door into `review-pass.py`.

**THIS FILE'S CLI IS A PUBLIC CONTRACT AND IT HAS NOT MOVED.** `--file --unit --status --evidence`: same
flags, same meanings, same "a non-zero exit means your inputs were rejected — fix them and re-run". It is
named in SKILL.md, in `stage-2-review-gate.md`, in `files-and-ledger.md`, and — the reason it can never be
renamed on a whim — inside every review prompt the orchestrator dispatches. Those prompts are already
running against INSTALLED copies of this skill: rename or remove this file and a live reviewer's emit call
dies mid-pass.

What has changed is what it ENFORCES. The whole review-pass artifact set now has ONE owner —
`review-pass.py`, which also writes the plan and the `pass_identity`, and reads a pass back to answer
"does this pass COUNT?". This file forwards to that owner instead of re-implementing half of it, so a
progress event has exactly one definition and there is no second copy to drift.

The rule you are most likely to meet: **a unit that is NOT IN THE PLAN is refused.** "Progress counts only
when it references a planned unit" was stated in prose and enforced by nobody — this tool used to accept a
`done` for a unit that was never planned, and the read side never looked. It holds at both doors now. If
the plan is genuinely missing a dimension, raise a `plan_amendment_request`; never self-grant a unit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

OWNER = Path(__file__).resolve().parent / "review-pass.py"


def load_owner():
    """Load `review-pass.py` BY PATH, from this script's own directory.

    Not by import: `review-pass` is not a legal module name, and the cwd is the reviewer's WORKTREE while
    the skill's scripts live wherever the plugin is installed. `__file__` is the only thing that knows
    where its sibling is.
    """
    spec = importlib.util.spec_from_file_location("review_pass", OWNER)
    if spec is None or spec.loader is None:  # a broken install — never an input error
        print(f"emit-progress: cannot load its owner at {OWNER}", file=sys.stderr)
        raise SystemExit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    raise SystemExit(load_owner().main(["emit", *sys.argv[1:]]))
