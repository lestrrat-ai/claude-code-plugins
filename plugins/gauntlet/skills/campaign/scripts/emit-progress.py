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

**And `--unit` IS NOT TRIMMED — pass the id exactly as the plan spells it.** A unit id has ONE legal form
(lowercase letters then digits: `u01`), and a `--unit` outside that form is refused for what it IS — not
an id — rather than silently repaired. This CHANGES what the CLI accepts, and the flags have still not
moved: ` u01 ` used to be quietly stripped to `u01` here while `plan-add` took its `--id` verbatim, so a
plan could hold ` u01 ` and this tool would then tell you that unit was NOT IN THE PLAN — while printing
`Planned: [' u01 ']`. Two doors, two ideas of what an id is, and a planned unit whose progress could never
be recorded. Neither door repairs an identifier now, so there is only ever one string to pass.

The rule you are most likely to meet SECOND, and the one that CHANGES what this CLI accepts: **a `done`
for a unit with no earlier `started` is refused.** Emit `--status started` when a unit BEGINS and
`--status done` when it ends — do not batch both at the end, and never emit only the `done`. The flags are
unchanged; what is no longer accepted is a `done` that no `started` precedes. That is deliberate: a
progress file holding a `done` for every planned unit and NOT ONE `started` used to verify `ok`, which
made the tool that exists to prove a review HAPPENED accept one that demonstrably did not.

And the THIRD: **a unit is `done` exactly once — a SECOND `done` for it is refused.** `verify` already
refused to READ one; this door WROTE it, exited 0, and the pass was thrown away later for a defect this
tool had just helped commit. A rule enforced at one door is not enforced. If what you found changed, the
pass is what re-runs, not the line.

The FOURTH is not about your event at all — it is about the FILE: **a progress file that `verify` cannot
read is refused, and nothing is appended to it.** Most often that means the file does not yet carry the
orchestrator's `pass_identity` (it is written before you are launched, so an EMPTY or absent file means
the pass was never dispatched — do not try to start it yourself), or a line already in it was
hand-written, or its last line has no newline. This one is NEW, and it exists because the tool used to do
the opposite: `--status started` on an empty file exited 0, and `verify` then called that same file
`unusable: NO pass_identity`. Your event landed and your pass could not count. **Anything this tool
writes, it must be able to read back** — so if the file it would produce is one `verify` would throw away,
it declines to write and says why, while there is still something you can do about it.

A REFUSAL here is not a broken CLI. The flags are the contract and they have not moved; what a refusal
says is that the event you asked for — or the file it would land in — is one `verify` would refuse to
read, and writing it would only lose the pass. Read the message, fix the call, re-run. If the message is
about the FILE and not your event, it is not yours to fix: report it, because the pass's dispatch is
broken and no event you emit into that file will count.

**AND `--help` NOW DESCRIBES THE COMMAND THIS TOOL ACCEPTS.** It did not. This file had no parser of its
own — it prepended the owner's `emit` subcommand to `sys.argv` and let the OWNER's argparse render the
help under THIS script's name — so `--help` printed `usage: emit-progress.py emit [-h] --file …` while
running that exact command died with `unrecognized arguments: emit`: the wrapper supplies `emit` itself.
The help door and the parser door disagreed about what the command IS, which is the same defect as two
doors disagreeing about what an ID is, one layer up — and the help is the door a reviewer READS. It has
its own parser now, built from the owner's own `add_emit_args` (so the flags still have ONE definition and
cannot drift), and `review-pass.py self-test` EXECUTES the invocation this `--help` advertises.

**AND IT DOES THAT FOR EVERY DOOR, NOT JUST THIS ONE — WHICH IS THE PART THE FIRST CURE MISSED.** That
check looked at this wrapper and at nothing else, so the owner's own subcommands went on advertising
whatever they liked: `review-pass.py plan-add --help` bracketed `[--check CHECK]` — argparse for OPTIONAL —
while the write path refused that exact command for having no checks. The same defect, one door over,
underneath the check written to stop it. `self-test` now runs EVERY door — every subcommand and this
wrapper — in the shape its own `--help` advertises.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

OWNER = Path(__file__).resolve().parent / "review-pass.py"
PROG = Path(__file__).name


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


def main(argv: "list[str] | None" = None) -> int:
    """This door's OWN parser — so what `--help` SAYS is what the tool TAKES, verbatim.

    The flags are NOT restated here: `add_emit_args` is the owner's one definition of the emit door, and
    both doors call it. The subcommand is supplied by `set_defaults`, where the caller cannot type it and
    no usage line can advertise it — the old wrapper prepended it to `argv` instead, which is precisely how
    it came to advertise a command it then refused. And the parsed args go to the owner's `dispatch`, so
    the refusal-to-exit-code mapping is the owner's too: a non-zero exit means your inputs were rejected.
    """
    owner = load_owner()
    p = argparse.ArgumentParser(prog=PROG, description=(__doc__ or "").splitlines()[0])
    owner.add_emit_args(p)
    p.set_defaults(cmd="emit")
    return owner.dispatch(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
