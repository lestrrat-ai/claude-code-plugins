#!/usr/bin/env python3
"""Write your review report — the reviewer's door into `review-pass.py`, and the ONLY way to deliver one.

**THE REPORT USED TO BE FREE TEXT, AND IT WAS THE LAST ARTIFACT THAT WAS.** Progress goes through
`emit-progress.py`, findings through `emit-finding.py`, amendments through `emit-amendment.py` — all
validated records. The report was prose with a terminal `VERDICT:` line somewhere at the end of it, and a
parser had to FIND that line inside text nobody had validated.

Everything that went wrong with it went wrong for one reason: **an artifact with no boundaries makes every
boundary a guess.** A line reader that ends a line at U+001C truncated a residual-risk remark; several
remarks ran together in the rendered output with nothing to say where one stopped; and "put the line last
before the verdict, with nothing between" turned into a standing argument about how leniently to read a
formatting request — an argument that twice cost a complete, substantive review pass.

So there is nothing left to read leniently. You pass fields; this tool validates them and writes the
record. A verdict is a FIELD, not a line to be located. A residual-risk remark is an ARRAY ELEMENT, so a
control character inside it is escaped rather than treated as the end of it. There is no placement to get
wrong, because you place nothing.

**RUN THIS EXACTLY ONCE, WHEN YOU ARE DONE.** A pass yields ONE report. The tool refuses a second call
rather than appending to the first, because a second result is not more evidence — it is a choice among
results, which is not a result. If what you concluded changes, the PASS re-runs; the record does not.

**AND IT IS NOW THE ONLY PRODUCER, ON EVERY ROUTE.** A native worker used to write the file itself and an
external process's final output was captured at the report path by the orchestrator. Neither happens any
more: whatever your host is, your report exists because you ran this. Returning it as your final message,
or writing that path by hand, produces NO report — and a pass with no report cannot count.

The flags:

  * `--verdict` — `satisfied` or `not-satisfied`. The rule is an IF AND ONLY IF: **`not-satisfied` exactly
    when at least one GATING finding stands.** `emit-finding.py` tells you which kind each finding was as
    it wrote it, so you are never guessing. `deferred` is NOT a verdict — it says you rendered none because
    you raised a request the orchestrator must handle first (a plan amendment, or a broken dispatch you are
    stopping on).
  * `--deferred-reason` — the one line the orchestrator routes a `deferred` result on, and the literal `-`
    when you rendered a verdict. "There is nothing to route" is a value you TYPE, never one you reach by
    leaving a field empty.
  * `--residual-risk` — OPTIONAL and repeatable: the part of the diff you verified with the LEAST certainty
    relative to the rest, and why. It is calibration metadata for the campaign's final report, never a
    finding, and it does NOT weaken a `satisfied`. Do not manufacture a concern to fill it — if identifying
    one surfaces a real GATING defect, record it with `emit-finding.py` and return `not-satisfied` instead.
    When you cannot honestly name a least-certain area, write none: that is the correct output, not a
    deficient one. Only a `satisfied` report may carry one.
  * `--summary` — your report: what you reviewed, what you found, and the concrete fix for each finding
    with its `file:line`. The findings artifact holds the authoritative record; this is what a human reads
    and what a later reassessment pass is handed.

A non-zero exit means the report was REJECTED, not that the tool is broken: read the message, fix the
call, re-run. The flags are defined in ONE place — `review-pass.py`'s `add_report_args`, which this door
and the owner's `report-write` subcommand both call, so `--help` here can never advertise a command the
tool refuses.
"""

from __future__ import annotations

import sys

from _gauntlet.review_door import dispatch_report_door


def main(argv: "list[str] | None" = None) -> int:
    """This door's OWN parser — so what `--help` SAYS is what the tool TAKES, verbatim.

    The flags are NOT restated here: `add_report_args` is the owner's one definition of the report door,
    and both doors call it. The subcommand is supplied by `set_defaults`, where the caller cannot type it
    and no usage line can advertise it — the pattern that keeps a wrapper from advertising a command it
    then refuses.
    """
    return dispatch_report_door(__file__, __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
