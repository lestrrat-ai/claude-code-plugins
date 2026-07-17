#!/usr/bin/env python3
"""Record ONE review finding — the reviewer's door into `review-pass.py`, and the ONLY way to report one.

**A FINDING USED TO BE A PARAGRAPH.** It was prose in `review-<pr>-<n>.txt`: no schema, no citation rule,
no owner, and — the part that cost a night — **no way to decline one**. Every finding a reviewer reported
became a fix; every fix added code; the next reviewer hunted the code the last fix added. One PR ran 21
review rounds and never converged.

**And not one of the late findings was WRONG.** They were true, reproduced, `file:line`-concrete defects —
in guards the loop had itself just built, against inputs NOBODY CAN WRITE: a table you can only corrupt by
hand-editing a git-ignored scratch file the driver owns; a self-test you can only defeat by editing its
source in memory. The reviewer was not malfunctioning. It was answering the question it was asked — *"is
anything wrong with this code?"* — and **that question has no fixed point.** There is always one more true
thing to say.

So this tool exists to make a finding ANSWER A DIFFERENT QUESTION:

    **Does this PR achieve its stated Purpose, without breaking anything reachable by an actor named in
    its Threat model?**

Every finding must ANCHOR to that. It names EITHER:

  * `--purpose` — a line of the PR's `## Purpose` block (`<rundir>/intent-<pr>.md`), quoted **VERBATIM**,
    which this finding DEFENDS. The tool checks the quote against the intent: you cannot invent a purpose
    to justify a finding, because the only strings that validate are the ones the intent already says. If
    fixing it serves no stated purpose, pass `-` and mean it.
  * `--writer` — WHO CAN ACTUALLY PUT THE BAD INPUT THERE, from a closed enum: `end-user`, `network`, `ci`,
    `repo-content`, `driver-only`, `hand-edit`, `dev-time`. **A guard being incomplete is not, by itself, a
    defect: name the writer who gets through it.** Choose `hand-edit` when the input can only exist if
    someone hand-edits a local, git-ignored file the driver owns. Choose `dev-time` when the defect can only
    be triggered by editing the source of the code under review — **if your reproduction begins "I mutated …
    in memory", the writer is `dev-time`**, and the tool will tell you so.

**A finding that anchors to NEITHER is NON-GATING.** It is still RECORDED — as a follow-up, for a human —
and the tool says so on stdout when you write it. What it may not do is produce NOT SATISFIED, and no fix is
dispatched for it. That is not a loophole and it is not a licence to lower your bar: it is the difference
between the findings that were worth 21 rounds and the ones that were not, and it is the only reason this
gate can ever finish.

**AND A FINDING THAT DOES ANCHOR — GATES. The tool says that too, on the same stdout.** The rule is an IF
AND ONLY IF: **NOT SATISFIED exactly when at least one GATING finding stands.** So the anchor you type here
IS your verdict — record a gating finding and return SATISFIED anyway and the pass is UNUSABLE and gets
thrown away, because a finding cannot read as blocking in the artifact and as ignorable in the verdict. If
what you found does not really block the PR, the thing to change is the ANCHOR, not the verdict: `--purpose
-` with a `driver-only`/`hand-edit`/`dev-time` writer says so honestly, and the finding is still recorded.

**Keep hunting.** The adversarial sweep is not narrowed — it is BOUNDED, by the threat model rather than by
nothing. The findings that mattered were found by exactly this kind of hostile reading: a false CI green
reachable from a real GitHub response, found in code an earlier fix round had itself added. That one is
`writer=network`, it defends the PR's whole purpose, and it GATES. Look for its kind.

A non-zero exit means the finding was REJECTED, not that the tool is broken: read the message, fix the call,
re-run. The flags are `--file --path --line --writer --purpose --repro --fix`, and they are defined in ONE
place — `review-pass.py`'s `add_finding_args`, which this door and the owner's `finding-add` subcommand both
call, so `--help` here can never advertise a command the tool refuses.
"""

from __future__ import annotations

import sys

from _gauntlet.review_door import dispatch_finding_door


def main(argv: "list[str] | None" = None) -> int:
    """This door's OWN parser — so what `--help` SAYS is what the tool TAKES, verbatim.

    The flags are NOT restated here: `add_finding_args` is the owner's one definition of the finding door,
    and both doors call it. The subcommand is supplied by `set_defaults`, where the caller cannot type it
    and no usage line can advertise it — the wrapper this one is modelled on used to prepend it to `argv`
    instead, which is precisely how it came to advertise a command it then refused.
    """
    return dispatch_finding_door(__file__, __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
