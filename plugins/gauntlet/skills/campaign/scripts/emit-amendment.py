#!/usr/bin/env python3
"""Raise ONE plan amendment — the reviewer's door into `review-pass.py`, and the ONLY way to raise one.

**THE AMENDMENT WAS THE ONE PROGRESS EVENT A REVIEWER HAND-WROTE** — exempt from the emit-only rule
that governs `started`/`done` — and that exemption is exactly what cost two full review passes in one real
run. The dispatch prompt never stated the event's schema, so external reviewers invented
`{"type":"plan_amendment_request","gap":"…"}`; `verify` requires EXACTLY `{type, ts, reason, proposed_unit}`
and refused the malformed line, and refusing ONE line takes the WHOLE pass down as `unusable`. A hand-written
event is a hand-written event: the read side never assumed the write tool was used, and there was no write
tool to use.

Now there is. This door builds the amendment through `review-pass.py`'s owner and validates it by the SAME
rules `verify` reads it back with: the `proposed_unit` goes through the unit check `plan-add` runs (id
format, non-empty checks, exact key set), `--reason` must be non-blank (the orchestrator RULES on it), and
the write is refused unless the file it would leave READS BACK — most often that means the progress file
must already carry the orchestrator's `pass_identity`.

**AND THE TOOL STAMPS `ts` — you supply no clock.** It is UTC to the second, the one form the verifier
accepts, so the amendment you raise is one `verify` can always order against the others. A `ts` you typed is
a `ts` you could get wrong; there is no flag for it.

Raising an amendment is how you say the plan MISSES a dimension — never rewrite the plan or self-grant a
unit. After it lands, this pass verifies `amended` until the orchestrator folds the proposed unit into the
plan and restarts the pass (or records why not), and you end your report `VERDICT: DEFERRED`.

A non-zero exit means the amendment was REJECTED, not that the tool is broken: read the message, fix the
call, re-run. The flags are `--file --reason --id --kind --target --check`, and they are defined in ONE
place — `review-pass.py`'s `add_amendment_args`, which this door and the owner's `amend` subcommand both
call, so `--help` here can never advertise a command the tool refuses.
"""

from __future__ import annotations

import sys

from _gauntlet.review_door import dispatch_amendment_door


def main(argv: "list[str] | None" = None) -> int:
    """This door's OWN parser — so what `--help` SAYS is what the tool TAKES, verbatim.

    The flags are NOT restated here: `add_amendment_args` is the owner's one definition of the amendment
    door, and both doors call it. The subcommand is supplied by `set_defaults`, where the caller cannot type
    it and no usage line can advertise it — the pattern that keeps a wrapper from advertising a command it
    then refuses.
    """
    return dispatch_amendment_door(__file__, __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
