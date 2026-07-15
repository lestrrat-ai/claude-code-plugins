#!/usr/bin/env python3
# THE EXEC BIT: mode 100644 is DELIBERATE, and TWO separate reviews have now proposed `chmod +x` here. The
# answer is the same both times: nothing invokes this as `./review-pass.py`. Every caller runs it as
# `python3 <path>` — CI does (`.github/workflows/ci.yml`), and so does the reviewer's emit call — so the
# shebang is a courtesy and the mode carries nothing. Leave it.
"""Executable contract for a REVIEW PASS's artifacts (stage-2-review-gate.md).

A review pass produces four things, and until now exactly ONE of them had a tool:

  * `review-<pr>-<n>.plan.jsonl`      the units the reviewer must check — written BY HAND, with a shell
                                      heredoc. No schema, no validation, no owner.
  * `review-<pr>-<n>.progress.jsonl`  what the reviewer did. Its `pass_identity` line was written BY HAND
                                      with `printf` — which is how a TRUNCATED SHA got into one. Its
                                      `progress` events came from `emit-progress.py`, the one tooled part.
  * `review-<pr>-<n>.txt`             the reviewer's report, and its VERDICT line.
  * the TALLY                         read BY HAND, with an ad-hoc parser written fresh each time.

**This is gate machinery**: what these files say decides whether a review pass COUNTS, and therefore
whether a PR may merge. The one component of the CI gate that was never mechanized is the one that
produced a false green in production — a driver read `gh pr checks` by eye and wrote `ci = green` on zero
evidence. Reading a progress file by eye is the same hole, one layer up.

So this file is the review pass's artifacts, executed:

  plan-add    append ONE validated unit to a pass's plan          (the plan stops being a heredoc)
  identity    write a pass's `pass_identity` line                 (the SHA stops being a `printf`)
  emit        append ONE progress event                           (what `emit-progress.py` calls)
  verify      READ a pass and answer: DOES THIS PASS COUNT?       (the tally stops being by eye)
  self-test   the fixtures, the proof that every rule is pinned by one, every JSON example in the docs fed
              THROUGH the tool (a documented example the tool refuses is a trap, not a typo), and EVERY
              DOOR run in the shape its own `--help` advertises (a command the help promises and the tool
              refuses is the same trap, one layer up — `plan-add` shipped one)

WHAT `verify` DOES NOT DO — AND THE LINE IS DELIBERATE. It never opens `review-<pr>-<n>.txt`, never
parses the reviewer's prose, and CANNOT SAY `SATISFIED`. Its whole answer is about the pass's MECHANICS:
is there an identity, does it name the commit the pass actually ran on, WAS THERE AN INTENT for this
reviewer to be measured against, is every `done` for a unit that was really planned, did every `done`
FOLLOW a `started` for that same unit, does every `done` carry evidence, were amendments raised, and does
the verdict the orchestrator READ cohere with the findings the reviewer RECORDED. The VERDICT itself is
the reviewer's JUDGMENT and stays theirs.

That line is what keeps this tool from BECOMING the gate. `verify` can only ever SUBTRACT a pass — refuse
one that is defective. It can never ADD a SATISFIED verdict, never raise `reviews_ok`, and never merge
anything. A bug in a tool that can only refuse costs a re-review; a bug in a tool that could accept would
merge a PR nobody reviewed. **`ok` IS NOT `SATISFIED`.** It means the pass is well-formed enough for its
verdict to be *read* — a NECESSARY condition for counting it, never a sufficient one.

BOTH DOORS, ALWAYS — AND ONE IMPLEMENTATION, NEVER TWO. Every rule here holds where the COMMANDS enter
(`emit`, `identity`, `plan-add`) AND where the DATA enters (`verify`), and it holds by the SAME statement
at both. Those are two halves of one discipline, and each half has already failed on its own:

  * a rule enforced only at the WRITE door is not enforced. The progress file is a plaintext file in a
    directory the reviewer can write to, the emit-only rule is prose, and a hand-written line lands in it
    just fine. So `verify` re-derives EVERYTHING from the bytes and assumes nothing about how they got
    there — it never trusts that the write tool was used.
  * a rule enforced only at the READ door is a trap. The SECOND `done` was refused by `verify` and WRITTEN
    by `emit`: exit 0, and the pass thrown away fifteen minutes later for a defect the tool had just
    helped the reviewer commit. The reviewer was told it had succeeded.
  * and a rule enforced at both doors by TWO implementations is a rule waiting to acquire two definitions.

So there are no per-door copies. `check_event`, `check_unit`, `check_identity_shape`, `check_progress` and
`plan_units` ARE the rules; `emit`, `identity` and `plan-add` call exactly the functions `verify` calls.

**AND THE RULES ARE NOT ENOUGH — ANYTHING THIS TOOL CAN WRITE, IT MUST BE ABLE TO READ BACK.** That is a
property of the doors TOGETHER, and every individual rule can be right at both doors while it fails. It
did, twice, in one release: `emit --status started` on an EMPTY progress file exited 0 (nothing checked
that the file had an identity yet), and `verify` then called that same file `unusable: NO pass_identity`.
`identity` decided "empty" with `.strip()`, so a file holding one blank line counted as fresh, the
identity went in below the blank line — and `verify` refused the artifact FOR THAT BLANK LINE. In both
cases the tool ACCEPTED the reviewer's work and then told it the work did not count. A pass is re-runnable
so nothing was lost but time; the same defect in a store with no second copy BRICKS it.

So the property is structural, not remembered: EVERY write goes through `write_line`, which hands the
bytes it is about to produce — `the file as it is` + `the line` — to the READ side's own whole-file
function (`check_progress_file`, `check_plan_file`) and REFUSES TO WRITE unless they accept it. Not a
write-shaped copy of the rules: the same functions `verify` runs. And "empty" means NO BYTES at every
door, because a file with a blank line in it is not an empty file — it is a file with a blank line, and
`verify` says so. The one read-door rule a write door cannot run is `check_head` (it compares the file to
the PR's LIVE head — the world, not the bytes); nothing a write does can cause that defect, and no write
door has a `--head-sha` to compare against. That gap is named there and nowhere else.

THE VERDICTS. Exactly one is printed, and there is no "counts, BUT…":

  ok          the artifacts are sound AND the verdict you gave coheres with them; it may now be tallied
  incomplete  sound, but a planned unit has no `done` event — the pass did not cover its plan
  amended     sound, but the reviewer raised a plan amendment nobody has ruled on yet
  unusable    the artifacts are defective — this pass CANNOT count, whatever its report says

`--verdict` IS REQUIRED, and that is the whole of what "a gate must not depend on a caller remembering"
means here. You come to `verify` WITH the report's `VERDICT:` line in hand; you do not come to it to find
out whether the reviewer is done. A pass still in flight is WATCHED, not verified — its progress file is
the liveness evidence (stage-2-review-gate.md, "Launch check"). While the flag could be left out, a
complete pass verified without it returned `ok`, so the one machine-checked rule about the reviewer's own
verdict was OFF for any driver that forgot a flag — and a driver that forgot it merged a PR whose reviewer
had returned SATISFIED over a GATING finding it recorded itself.

`amended` is a VERDICT and not a footnote beside `ok` on purpose. A disclosure printed next to a pass is a
trapdoor, not a disclosure: "this pass counts, but note that the reviewer says the plan is missing a
dimension" gets read as "counts". The orchestrator rules on the amendment (fold it into the plan and
restart the pass, or record why not — stage-2-review-gate.md), and passes `--amendments-ruled <n>` to say
so. Absent that, the guard fires: the DEFAULT is that nothing has been ruled on.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

# --- the contract (stage-2-review-gate.md) ------------------------------------------------------

# Verdicts. `ok` is the ONLY one that lets a pass be counted — and even then only after its REPORT is read.
OK = "ok"
INCOMPLETE = "incomplete"
AMENDED = "amended"
UNUSABLE = "unusable"

# The three event types a progress file may hold. There is no fourth, and a line of a type we do not
# recognise is NOT nothing — it is something we FAILED TO UNDERSTAND, and a row we failed to understand is
# never grounds to read on. An ignored line is invisible to every rule below, so a bogus `done` hiding
# under an unknown type would parse as "nothing wrong".
IDENTITY = "pass_identity"
PROGRESS = "progress"
AMENDMENT = "plan_amendment_request"

STARTED = "started"
DONE = "done"
STATUSES = (STARTED, DONE)

# The REVIEWER'S VERDICT, as the orchestrator READ it off the report and TELLS this tool. This file still
# never opens the report and still cannot say `SATISFIED` — it is handed the value so that ONE rule can be
# machine-checked, and that rule is an IF AND ONLY IF (`decide`): **NOT SATISFIED exactly when at least one
# GATING finding stands.** Both halves of it refuse a pass and neither can grant one. The spelling is the
# ledger's (`ledger.py verdict --verdict satisfied|not-satisfied`), because the same string is typed at both
# doors by the same driver in the same step, and two spellings of one verdict is a bug waiting for a wake.
SATISFIED, NOT_SATISFIED = "satisfied", "not-satisfied"
VERDICTS = (SATISFIED, NOT_SATISFIED)

# The EXACT key set each event carries — every key it requires, and NOT ONE MORE. "Every required key is
# present" admits an event that ALSO carries a key nothing reads, and a key nothing reads is evidence that
# is PRESENT AND NOT COUNTED: a `done` with a `ts` nobody parses, a `pass_identity` with a second sha. The
# progress events are keyed by (type, status) because a `started` carrying `evidence` is exactly as wrong
# as a `done` without it — the evidence rule and the no-evidence rule are ONE rule, stated once.
EVENT_KEYS: "dict[tuple[str, str | None], set[str]]" = {
    (IDENTITY, None): {"type", "pr", "pass", "head_sha", "launch_attempt", "dispatched_at"},
    (PROGRESS, STARTED): {"type", "unit", "status"},
    (PROGRESS, DONE): {"type", "unit", "status", "evidence"},
    (AMENDMENT, None): {"type", "ts", "reason", "proposed_unit"},
}

# A plan unit's EXACT key set — same rule, one file over. `checks` is the one field in either artifact that
# is not a string: it is a LIST of them, and it is what makes a unit auditable ("what did you actually
# look for?"). An empty list is a unit with no checks, which is not a unit.
UNIT_KEYS = {"type", "id", "kind", "target", "checks"}
# The two DESCRIPTIVE strings. `id` is NOT one of them: it is the one field in either artifact that is
# MATCHED — a progress event names a unit BY it — so it is an IDENTIFIER and is governed by `ID_FORMATS`
# below. Stating a weaker rule for it here as well is how a value comes to have two definitions.
UNIT_STRINGS = ("kind", "target")
UNIT = "unit"


# --- THE FINDING, and the INTENT it must ANCHOR to ------------------------------------------------
#
# **A FINDING USED TO BE PROSE.** It was a paragraph in `review-<pr>-<n>.txt`, and nothing could validate
# it, count it, or decline it — so every finding a reviewer reported became a fix, and every fix added
# code, and the next reviewer hunted the code the last fix added. One PR took 21 review rounds and never
# converged; a human had to stop it. **Not one of the late findings was WRONG.** They were true, reproduced,
# `file:line`-concrete statements about defects that really existed — in guards the loop had itself just
# built, against inputs that NOBODY CAN WRITE.
#
# The reviewer was not malfunctioning. It was doing exactly what it was asked, and **what it was asked has
# no fixed point**: "is anything wrong with this code?" There is always one more true thing to say.
#
# So the question changes, and this is the whole of the fix:
#
#   **DOES THIS PR ACHIEVE ITS STATED PURPOSE, WITHOUT BREAKING ANYTHING REACHABLE BY AN ACTOR NAMED IN
#   ITS THREAT MODEL?**
#
# A question with a fixed point. To ask it, the run must know what the PR is FOR — which it did not, at all:
# the dispatch prompt said "review the changes on this branch", and adoption did not even FETCH the PR's
# body. The intent block (`<rundir>/intent-<pr>.md`, `pr-adoption.md`) is that missing input, and these two
# fields are how a finding is held against it.
#
# EVERY FINDING ANCHORS, or it does not gate. It names EITHER:
#   * `purpose` — a VERBATIM line of the intent's `## Purpose` block, the thing this finding DEFENDS; or
#   * `writer`  — WHO CAN ACTUALLY PUT THE BAD INPUT THERE, from a closed enum.
# A finding that can anchor to neither is a true statement about code that nothing in the world can reach
# and nothing the PR promised depends on. It is recorded as a follow-up. It does not gate.

FINDING = "finding"

# The EXACT key set, by the same rule every other record here obeys: every key it requires, and NOT ONE
# more. `line` is a string like every other value in these artifacts.
FINDING_KEYS = {"type", "file", "line", "writer", "purpose", "repro", "fix"}
# The prose fields — checked for being a non-blank string, and nothing more. `file` and `line` are the
# CITATION (a finding with no `file:line` is not a finding, it is an opinion); `repro` is what makes it a
# demonstrated defect rather than a claim; `fix` is what the fix subagent is dispatched with.
FINDING_STRINGS = ("file", "repro", "fix")

# **WHO CAN ACTUALLY WRITE THE BAD INPUT — A CLOSED ENUM, DECLARED PER FINDING.**
#
# This is the reviewer's judgment, and it is the one place in this tool that is. It is bounded three ways:
# the enum is CLOSED (a value outside it is refused, never guessed at), the choice is cross-checked against
# the reviewer's own `repro` (`check_writer_repro`), and it only ever matters for a finding that ALSO
# anchors to no purpose line.
WRITERS = ("end-user", "network", "ci", "repo-content", "driver-only", "hand-edit", "dev-time")

# The three that name NO ADVERSARY — nobody outside the machine can produce the input:
#   driver-only  only the campaign driver itself writes this; no user, no network, no repo content
#   hand-edit    only someone hand-editing a LOCAL, GIT-IGNORED file the driver owns
#   dev-time     only someone EDITING THE SOURCE OF THE CODE UNDER REVIEW ("I mutated X in memory…")
#
# The other four name a real one: `end-user` (a CLI argument, a human), `network` (a real API response),
# `ci` (a CI system's output), `repo-content` (a file in the repo, a doc, a fixture, a file mode).
#
# **THE DISTINCTION IS THE WHOLE POINT, and the record proves both halves of it.** The findings that were
# worth 21 rounds were all in the first group. The findings that were GENUINELY serious were in the second
# — including one against code an EARLIER FIX ROUND had added, which is exactly why "was this line added by
# a fix?" is NOT the test: a fix round can absolutely introduce a real defect. A paginated reader added
# mid-gauntlet treated a missing row array as empty and produced a FALSE GREEN from a real GitHub response.
# `writer=network`. It gates, and it must.
NO_ADVERSARY = ("driver-only", "hand-edit", "dev-time")

# The `purpose` of a finding that defends no stated purpose. It is a literal `-`, never blank — "I looked
# and there is none" must be a value the reviewer TYPES, not one it can reach by leaving a field empty.
#
# **BECAUSE IT IS A SENTINEL, IT MUST NEVER ALSO BE DATA.** `-` means "anchors to NO purpose" AND is a
# string a human can type into a `## Purpose` bullet — so a purpose line that IS `-` would collide with the
# marker for its own absence, and a finding quoting that line verbatim would read as anchoring to nothing.
# `parse_intent` closes the gap at the WRITE door: a `## Purpose` bullet equal to `NO_PURPOSE` is REFUSED,
# so the set of real purpose lines and the "no purpose" marker stay disjoint and `check_finding`'s
# `purpose == NO_PURPOSE or purpose in purposes` can never be true for two different reasons at once.
NO_PURPOSE = "-"


def gating(rec: dict) -> bool:
    """**MAY THIS FINDING BLOCK THE PR?** The rule, in one statement, and the only definition of it.

    A finding gates unless it anchors to NOTHING: no line of the PR's stated purpose is served by fixing
    it, AND nobody outside the machine can write the input that triggers it.

    **NOT EVERY TRUE STATEMENT ABOUT THE CODE IS A REASON TO BLOCK IT.** A non-gating finding is not
    refuted, not dismissed and not necessarily wrong — it is simply not a reason to spend another round.
    It is recorded as a follow-up and the review moves on.

    Read the two conjuncts as the two ways a finding can EARN its block, because that is what they are:
      * it DEFENDS something the PR promised to do (`purpose` quotes that promise), or
      * it is reachable by SOMEONE (`writer` names them).
    Only a finding that can say neither is discharged.
    """
    return not (rec["purpose"] == NO_PURPOSE and rec["writer"] in NO_ADVERSARY)


# **THE REPRO THAT GIVES THE WRITER AWAY.** `writer` is declared by the reviewer, so it is the soft joint
# in this design — and this is the cross-check that hardens it where the record showed it mattered. A
# reproduction that begins "I mutated … in memory" or "I changed it in a temp copy" is describing an EDIT
# TO THE SOURCE UNDER REVIEW. There is no input, no actor and no adversary in it: the only person who can
# do that is a developer with a text editor. Such a repro MUST declare `writer=dev-time`.
#
# **IT IS A HEURISTIC AND I AM CALLING IT ONE.** It keys on PHRASES, listed below, and it therefore catches
# exactly the reproductions that use them — which is both of the real ones this rule was written from, and
# not a reproduction that describes the same source edit in other words. It cannot be complete, because a
# repro is prose. What it CAN do is fail SAFE: a repro that trips it while claiming a real-world writer is
# REFUSED (the pass is unusable and gets re-run), never quietly demoted. It can only ever cost a re-review.
#
# Each phrase names an act on the CODE, never on an INPUT — that is the line, and it is why "removed",
# "changed" and "crafted" are deliberately NOT here. A reviewer that "removed the `statuses` member from
# the otherwise-green fixture" is describing a RESPONSE SHAPE, and that finding was a real false green from
# a real GitHub reply. A rule that read the word "removed" as a source edit would have discharged it.
SOURCE_EDIT_RE = re.compile(
    r"\bmutat(?:e|ed|es|ing|ion|ions)\b"      # "I mutated EXCEPTIONS |= {…}"; "I executed two mutations"
    r"|\bin memory\b"                          # "…in memory and the full self_test() still exited 0"
    r"|\btemp(?:orary)? copy\b"                # "I changed this catch to RuntimeError in a temp copy"
    r"|\balternate index\b"                    # "I stripped ledger.py to 100644 in an alternate index"
    r"|\bedit(?:s|ed|ing)? the source\b",
    re.IGNORECASE,
)


# --- the IDENTIFIERS: ONE LEGAL STRING EACH, and NO CONVERSIONS ANYWHERE -------------------------
#
# **AN IDENTIFIER IS A VALUE TWO DOORS COMPARE.** A unit id is written by `plan-add` and matched by `emit`;
# `pr`/`pass`/`launch_attempt` are read from the FILENAME and compared to the `pass_identity`; `head_sha` is
# written by `identity` and compared to the PR's live head. Every one of them crosses the tool, and a value
# that crosses the tool is a value two doors must agree about.
#
# **SO EACH HAS EXACTLY ONE LEGAL FORM, AND ANYTHING ELSE IS AN ERROR — NEVER A VARIANT TO REPAIR.** That
# is the whole rule, and it is NOT the same as normalizing. This tool used to `.strip()` `--unit` at the
# emit door while the plan door accepted the padding, so `plan-add --id ' u01 '` succeeded and `emit --unit
# ' u01 '` then failed with NOT IN THE PLAN, printing `Planned: [' u01 ']` — a plan holding a unit the emit
# door could never match, and a review that could never complete. Stripping at BOTH doors would have fixed
# that one call and kept the disease: two spellings of one id, and every future door obliged to remember
# the conversion. A FORMAT leaves nothing to convert — `' u01 '` is not another way of writing `u01`, it is
# NOT AN ID — so there is nothing for two doors to disagree about.
#
# The `head_sha` row is the proof that this beats normalization outright: no amount of stripping catches a
# TRUNCATED sha (`a3f29c1`, which is what a hand-written `pass_identity` once carried into real state). It
# is perfectly "clean"; it is simply not a commit id. Only a FORMAT can say so.
#
# REJECT ON THE WAY IN, NEVER ON THE WAY OUT: every door that ACCEPTS one of these calls `check_id`, so a
# plan can never come to hold an id no door can match. `check_id` is the ONE validator; these are the ONE
# set of patterns; there is no second copy and no door-local rule.
#
# **EVERY PATTERN HERE ENDS `\Z`, NEVER `$`, AND THAT IS NOT A STYLE CHOICE.** In Python `$` also matches
# just BEFORE a trailing newline, so `^[0-9a-f]{40}$` — which is what this file used to say — ACCEPTS a
# 40-hex sha with a `\n` glued to the end. That is a second spelling of one commit id: exactly the disease
# the whole table exists to kill, hiding inside the fence meant to stop it. `\Z` is the end of the STRING,
# and an identifier is a whole string or it is not one. The format matrix (`ID_CASES`) is what found this,
# by asking each identifier to refuse its own value with a newline on it — which is the point of writing a
# format down: it has an exact boundary, so a test can stand on both sides of it.

# A decimal count of something that starts at ONE: no sign, no leading zeros, no whitespace, and no `int()`
# (which would take `" +2 "` and then CRASH on `"two"`). There is no PR 0, no pass 0 and no launch attempt
# 0, so a value that names one is not a value we may go on to compare.
COUNT = r"[1-9][0-9]*"

# The LAUNCH ATTEMPT, as a FILENAME wears it: the `a<k>` suffix, which exists only for k >= 2 (attempt 1
# has no suffix). Its domain is every decimal integer from 2 up, no leading zeros — a strict subset of
# `COUNT`, because the attempt in the NAME is compared to the `launch_attempt` in the `pass_identity` and
# the two must be the same kind of value.
#
# **IT WAS `[2-9][0-9]*`, WHICH READS LIKE "2 UPWARD" AND IS NOT.** That pattern accepts `a2`…`a9` and
# `a20`, and REFUSES `a10` THROUGH `a19`: a tenth launch attempt could not name its own file, while this
# tool's own error message and every doc say `k >= 2`. The regex and the definition disagreed about what an
# attempt number IS — the same disease as ` u01 ` being two spellings of one id, one door over. A domain
# with a hole in the middle of it is not a domain, and no fixture stood on that boundary.
#
# `[2-9]` is the one-digit case; `[1-9][0-9]+` is every longer one (so `10` is in and `02` is out).
ATTEMPT = r"(?:[2-9]|[1-9][0-9]+)"

# A unit id, and the one form of one: lowercase letters then digits — `u01`, `u02`. It is the id an
# ORCHESTRATOR writes into the plan and a REVIEWER names in every progress event, so it is typed twice by
# two different processes, and the whole point of pinning its shape is that those two typings cannot differ
# by a space. Nothing about it is normalized: `U01`, `u 01`, ` u01 ` and `u01 ` are all simply not ids.
UNIT_ID_RE = re.compile(r"^[a-z]+[0-9]+\Z")

# A git object id, as git writes one: 40 LOWERCASE hex. **A SHORT SHA HAS ESCAPED INTO REAL STATE IN THIS
# REPO TWICE**, once through a hand-written `pass_identity`. A prefix is not a commit: it does not identify
# the content a pass reviewed, and every "did this verdict describe the live tip?" comparison made against
# one is a comparison that cannot mean what it says.
SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")

# identifier -> (its ONE legal form, that form in words, and why a value outside it is not one to repair).
# The key is the FIELD NAME as it is spelled in the artifacts, so a message names the thing the caller
# typed. A field not in this table is not an identifier — it is prose (`kind`, `target`, `evidence`,
# `reason`), and prose is checked for being a non-blank string and nothing more.
ID_FORMATS: "dict[str, tuple[re.Pattern[str], str, str]]" = {
    "id": (UNIT_ID_RE, "lowercase letters then digits, e.g. `u01`",
           "a unit id is MATCHED, not read: the plan writes it and every progress event names the unit by "
           "it. A second spelling of one id is a unit the other door cannot find — ` u01 ` is not `u01` "
           "with a space, it is NOT AN ID. Nothing here is stripped or repaired, at any door"),
    "unit": (UNIT_ID_RE, "lowercase letters then digits, e.g. `u01`",
             "the unit this event is progress FOR, named exactly as the plan names it. The emit door does "
             "NOT strip it — if it did, the plan and the progress file would hold two spellings of one id "
             "and only one of them would ever match"),
    "pr": (re.compile(rf"^{COUNT}\Z"), "a decimal number from 1 up",
           "it is COMPARED to the number in the FILENAME, and a value we cannot compare proves nothing"),
    "pass": (re.compile(rf"^{COUNT}\Z"), "a decimal number from 1 up",
             "it is COMPARED to the number in the FILENAME, and a value we cannot compare proves nothing"),
    "launch_attempt": (re.compile(rf"^{COUNT}\Z"), "a decimal number from 1 up",
                       "it is COMPARED to the attempt in the FILENAME — it is how a later wake knows the "
                       "pass was already relaunched — and a value we cannot compare proves nothing"),
    "head_sha": (SHA_RE, "40 LOWERCASE hex — `git rev-parse HEAD`, NEVER an abbreviation",
                 "A short sha has escaped into this repo's real state TWICE, once through a hand-written "
                 "`pass_identity`. A prefix is not a commit: it names no content, so every 'did this "
                 "verdict describe the live tip?' comparison made against it is unfalsifiable. This is the "
                 "row no amount of trimming could ever have caught — a truncated sha is perfectly clean, "
                 "and simply not a commit id"),
    "line": (re.compile(rf"^{COUNT}\Z"), "a decimal number from 1 up",
             "a finding CITES a defect at a `file:line`, and a citation nobody can open is not one. There "
             "is no line 0, and `line` is where a human and a fix subagent are both sent to look"),
}


def check_id(name: str, value: object, where: str) -> None:
    """THE validator for every identifier this tool reads or writes. One function, one table, every door.

    It is called wherever an identifier ENTERS — `plan-add`'s `--id`, `emit`'s `--unit`, the `pass_identity`
    the orchestrator writes, and each of those same fields again as `verify` re-derives them from the bytes.
    No caller normalizes, compares or repairs an identifier itself; a caller that did would be the second
    definition, and two definitions of one value is the defect this table exists to make impossible.
    """
    pattern, spec, why = ID_FORMATS[name]
    if not isinstance(value, str) or not pattern.match(value):
        # MUTATE:id-format:return
        raise Defect(f"{where}: `{name}` is {value!r} — an identifier has ONE legal form ({spec}), and "
                     f"anything else is an ERROR, never a variant to be repaired. {why}")


# `dispatched_at` is the launch check's CLOCK — the ~5-minute first-event deadline is measured from it. A
# value that cannot be parsed as a time silently DISABLES that deadline: the guard's input is absent, so
# the guard never fires, and a reviewer that never started is waited on forever. UTC ISO-8601, `Z`.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def real_utc(value: str) -> bool:
    """Does this timestamp name a MOMENT — not merely look like one?

    `TS_RE` is a SHAPE check, and shape is not meaning: `2026-99-99T99:99:99Z` matches it exactly, and a
    month-99 date is not a date. A regex that accepts month 99 is a guard that CANNOT FIRE on the case
    that matters — the deadline measured from an impossible time is a deadline whose arithmetic cannot be
    done, which is the very failure the shape check was written to stop. So after the shape holds, the
    value is PARSED, and what does not parse is refused: `strptime` is the arbiter of what a date is, not
    ten digits in the right places. (`%Y-%m-%dT%H:%M:%SZ` — the `Z` is a literal, so this is UTC by
    construction; a value carrying any other offset never reaches here, `TS_RE` having refused it.)
    """
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True

# The artifact's EXACT name (`files-and-ledger.md`; the attempt table in stage-2-review-gate.md):
# attempt 1 is `review-<pr>-<n>.progress.jsonl`, a relaunch is `review-<pr>-<n>.a<k>.progress.jsonl`.
# The name is not decoration — it is the ONLY thing that says which PASS and which LAUNCH ATTEMPT these
# bytes belong to, and the docs already call substituting attempt-1 names into a relaunch a "silent
# self-defeat". Silent no longer: the name is parsed, and the identity inside must AGREE with it.
#
# The `pr` and `pass` in a NAME are the SAME identifiers `ID_FORMATS` governs, so they are built from the
# same `COUNT` — a name is an intake door too, and `review-041-1.progress.jsonl` is not another way of
# writing PR 41. The attempt suffix is `ATTEMPT`, and it is a NAMED constant for the reason every other
# value here is: written out inline it was `[2-9][0-9]*`, which silently refused `a10`-`a19`.
NAME_RE = re.compile(rf"^review-(?P<pr>{COUNT})-(?P<pass>{COUNT})(?:\.a(?P<attempt>{ATTEMPT}))?"
                     rf"\.progress\.jsonl\Z")

# The plan is PER-PASS, not per-attempt: a relaunch reuses it unchanged (stage-2-review-gate.md). So it is
# DERIVED from the progress path and never passed separately — one fewer door, and no way to point a pass
# at somebody else's plan.
#
# Deriving it means the READ side enforces the plan's name BY CONSTRUCTION: the only plan `verify` can ever
# open is the one at this name. The WRITE side took a `--file` and wrote wherever it was pointed — so
# `plan-add --file plan.jsonl` succeeded, and produced a plan NOTHING WILL EVER READ. `verify` would then
# refuse the pass for a MISSING plan while its units sat on disk a filename away. Enforced by construction
# at one door and not at all at the other is the same asymmetry as any other; `PLAN_NAME_RE` closes it.
PLAN_NAME = "review-{pr}-{pass}.plan.jsonl"
PLAN_NAME_RE = re.compile(rf"^review-{COUNT}-{COUNT}\.plan\.jsonl\Z")

# The FINDINGS artifact is PER LAUNCH ATTEMPT, not per pass — it is the reviewer's OUTPUT, like the report
# (`review-<pr>-<n>.txt`), and a relaunched pass produces its own. So its name is the progress file's name
# with one suffix swapped, and it is DERIVED from it exactly as the plan's is: no door takes a findings
# path and a progress path that could disagree about which pass they belong to.
PROGRESS_SUFFIX, FINDINGS_SUFFIX = ".progress.jsonl", ".findings.jsonl"
FINDINGS_NAME_RE = re.compile(rf"^review-(?P<pr>{COUNT})-{COUNT}(?:\.a{ATTEMPT})?\.findings\.jsonl\Z")

# The INTENT — what this PR is FOR. One per PR, written at adoption (`pr-adoption.md`), re-read every wake
# and never re-derived: a wake is a fresh agent instance, and an intent held only in context is one that
# gets invented a second time, differently.
#
# **EVERY PASS IS JUDGED AGAINST ONE — `evaluate` loads it whatever the pass found, and that is the whole
# rule.** It used to be loaded only where a finding needed ANCHORING, which meant a pass with NO findings
# never asked whether the intent existed at all: the guard's input could simply be ABSENT, and a guard whose
# input can be absent never fires. A SATISFIED pass that found nothing is the COMMON case and the one that
# merges a PR, so that was the hole in the exact shape of the door it was guarding.
#
# It is DERIVED from the artifact's own name too (the `pr` is in it), for the same reason the plan is: a
# `--intent` flag is a way to point a pass at somebody else's intent, and there is no reason to have one.
INTENT_NAME = "intent-{pr}.md"

# The three sections, verbatim, and ALL THREE ARE REQUIRED. A block missing one is not a weaker intent, it
# is an unusable one: `## Purpose` is what a finding quotes, `## Threat model` is what bounds the sweep, and
# `## Non-goals` is the only thing that can say "this was deliberate, stop reporting it".
PURPOSE_H, NON_GOALS_H, THREAT_H = "## Purpose", "## Non-goals", "## Threat model"
INTENT_SECTIONS = (PURPOSE_H, NON_GOALS_H, THREAT_H)


class Defect(Exception):
    """The artifacts are not evidence. -> `unusable`, at either door."""


class OperatorError(Exception):
    """The CALLER is wrong, not the artifacts. A verdict about the wrong question is worse than none."""


# --- the strict JSONL reader (shared by both artifacts) -----------------------------------------

def strict_object(name: str, n: int):
    """`object_pairs_hook` rejecting a REPEATED member name, in ANY object on the line.

    `json.loads` keeps the LAST value of a repeated key and silently DISCARDS the earlier one, so the
    discarded value is present in the bytes and reaches NO rule below. `{"head_sha":"<short>","head_sha":
    "<40 hex>"}` would pass every check in this file with a truncated sha sitting in the artifact. A field
    given two values does not say ONE thing, and a file that does not say one thing is not evidence.
    """
    def hook(pairs: "list[tuple[str, object]]") -> dict:
        dupes = sorted({k for k, c in Counter(k for k, _ in pairs).items() if c > 1})
        if dupes:
            # MUTATE:duplicate-key:pass
            raise Defect(
                f"{name} line {n}: duplicate member name(s) {', '.join(dupes)} — the decoder keeps "
                f"only ONE value for a repeated key and discards the other, so the discarded one is in the "
                f"bytes and reaches no rule"
            )
        return dict(pairs)

    return hook


def read_text(path: Path, what: str) -> str:
    """An artifact's BYTES, as text — the one place either door turns a file into something to read.

    Both doors call it, and that is the point: `verify` reads the file it JUDGES through this, and a write
    command reads the file it is about to append INTO through this, so a file the read side cannot decode
    is not one the write side will grow. It returns the raw text and not lines, because the write side
    needs the BYTES: what it is about to produce is `read_text(...) + the line`, and a file whose last line
    has no newline turns the next append into a CONCATENATION — two events fused into one unreadable line.
    Only the bytes can show that; a list of parsed lines has already forgotten it.
    """
    if not path.exists():
        # MUTATE:file-missing:pass
        raise Defect(
            f"no {what} at {path} — a review pass whose {what} is missing produced no evidence at all "
            f"(the orchestrator writes the plan before dispatch and `pass_identity` before the reviewer "
            f"starts, so this file exists from dispatch onward)"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # MUTATE:unreadable:return path.read_bytes().decode("utf-8", errors="replace")
        raise Defect(
            f"{path.name} cannot be read as UTF-8 text ({exc}) — bytes we cannot decode are not evidence, "
            f"and decoding them LENIENTLY rewrites what the file says"
        ) from exc


def parse_lines(text: str, name: str) -> "list[dict]":
    """Every line of a JSONL artifact's TEXT, as a dict. No line is skipped — not a blank one, not a bad one.

    A line this reader cannot understand is a producer we cannot trust, and a producer we cannot trust is
    not one whose output a PR may merge on.

    It takes TEXT, not a path, for one reason: the write side must be able to ask it about a file that does
    not exist yet — the file it is ABOUT TO PRODUCE. Same statements, same defects, one implementation.
    """
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            # MUTATE:blank-line:continue
            raise Defect(
                f"{name} line {n} is blank — JSONL has no blank lines, and a producer that writes one "
                f"is not one we can trust with the lines we DO read"
            )
        try:
            rec = json.loads(line, object_pairs_hook=strict_object(name, n))
        except json.JSONDecodeError as exc:
            # MUTATE:not-json:continue
            raise Defect(
                f"{name} line {n} is not JSON ({exc}) — a corrupt line is a corrupt artifact, never a "
                f"line to skip past"
            ) from exc
        except RecursionError as exc:
            # MUTATE:too-deep:continue
            raise Defect(
                f"{name} line {n} is nested too deeply for the decoder — it RAISED where a verdict "
                f"was owed, and a crash is not a verdict"
            ) from exc
        if not isinstance(rec, dict):
            # MUTATE:not-object:continue
            raise Defect(
                f"{name} line {n} is not a JSON object — every line of this artifact is one event"
            )
        out.append(rec)
    return out


def read_lines(path: Path, what: str) -> "list[dict]":
    """The file on disk, as events — the two halves above, in the only order they compose."""
    return parse_lines(read_text(path, what), path.name)


# --- the plan ------------------------------------------------------------------------------------

def check_unit(unit: object, where: str) -> None:
    """A plan unit, whether it sits in the plan or inside a `plan_amendment_request`. ONE definition.

    **`unit: object` — NOT `dict` — and that is load-bearing, not pedantry.** This is handed values
    straight out of `json.loads`, and a JSON value is whatever the file says it is: an amendment's
    `proposed_unit` can arrive as a STRING (`"proposed_unit": "u99"`), and a reviewer hand-writes that
    event — it is the one event type the emit-only rule exempts. Annotating the parameter `dict` would be
    a promise no caller can keep, and a type checker reading that promise concludes the `isinstance` guard
    below can never fire and the `raise` is UNREACHABLE. It is not: it fires, on that exact input, and the
    fixture `amendment-unit-not-object` drives it. Believe the annotation and delete the "dead" guard, and
    a string reaches `set(unit)` — which CRASHES, and a crash is not a verdict. Say what the caller can
    actually promise, and the guard the tool needs is the guard the type checker asks for.
    """
    if not isinstance(unit, dict):
        # MUTATE:unit-not-object:return
        raise Defect(f"{where}: the unit is not a JSON object")
    if set(unit) != UNIT_KEYS:
        missing = sorted(UNIT_KEYS - set(unit))
        extra = sorted(set(unit) - UNIT_KEYS)
        # MUTATE:unit-keys:pass
        raise Defect(
            f"{where}: a unit carries EXACTLY {sorted(UNIT_KEYS)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected key(s) {extra} — nothing reads them, so whatever they assert is neither "
               f"verified nor refuted" if extra else "")
        )
    if unit["type"] != UNIT:
        # MUTATE:plan-row-type:pass
        raise Defect(f"{where}: type is {unit['type']!r}, and a plan holds only {UNIT!r} records")
    # The id is an IDENTIFIER — the value the progress events MATCH — so it is checked by `check_id` and by
    # nothing else, here or at any other door. This is the intake: a unit whose id is not an id never
    # reaches the plan, so the plan can never come to hold a unit `emit` is unable to name.
    check_id("id", unit["id"], where)
    for field in UNIT_STRINGS:
        if not isinstance(unit[field], str) or not unit[field].strip():
            # MUTATE:unit-fields:continue
            raise Defect(
                f"{where}: `{field}` is {unit[field]!r} — a unit names a CONCRETE target and a concrete "
                f"kind, and an empty or non-string one names nothing"
            )
    checks = unit["checks"]
    if (not isinstance(checks, list) or not checks
            or not all(isinstance(c, str) and c.strip() for c in checks)):
        # MUTATE:unit-checks:pass
        raise Defect(
            f"{where}: `checks` is {checks!r} — a unit with no concrete checks is not a unit; it is a "
            f"heading, and a reviewer cannot be shown to have done anything against it"
        )


def plan_units(records: "list[dict]", name: str) -> "dict[str, dict]":
    """These plan records' units, by id — each validated, and a REPEATED id refused. ONE implementation,
    and therefore the same rule at both doors: `load_plan` runs it over the file the reviewer is judged
    against, and `cmd_plan_add` runs it over that file PLUS the unit it is about to append, so the write
    door refuses a duplicate id by the SAME statement the read door refuses it with.
    """
    units: dict[str, dict] = {}
    for n, rec in enumerate(records, start=1):
        check_unit(rec, f"{name} line {n}")
        if rec["id"] in units:
            # MUTATE:plan-duplicate-id:pass
            raise Defect(
                f"{name} line {n}: duplicate unit id {rec['id']!r} — a `done` naming it would be "
                f"ambiguous about WHICH unit was checked"
            )
        units[rec["id"]] = rec
    return units


def load_plan(path: Path) -> "dict[str, dict]":
    """The plan's units, by id. A plan is what makes `done` MEAN something — so it is validated, not read.

    An EMPTY plan is refused, and that rule carries the most weight of any here: "every planned unit is
    done" is VACUOUSLY TRUE of a plan with no units, so a pass that reviewed NOTHING would verify `ok`.
    A completeness check whose input can be empty is not a check.

    Emptiness is the ONE rule here that is deliberately read-only, and it is not one-sided: `plan-add` is
    how a plan STOPS being empty, so a write door that refused an empty plan could never write the first
    unit. The rule is about a plan a pass is JUDGED against, and that is the read door's question. Every
    other plan rule is `check_plan_file` — the same statement `plan-add` runs over the plan it produces.
    """
    units = check_plan_file(read_text(path, "plan"), path)
    if not units:
        # MUTATE:plan-empty:pass
        raise Defect(
            f"{path.name} holds no units — 'every planned unit is done' is VACUOUSLY TRUE of an empty "
            f"plan, so a pass that reviewed nothing at all would verify {OK}"
        )
    return units


# --- the intent, and the findings that must anchor to it -----------------------------------------

def parse_intent(text: str, path: Path) -> "dict[str, list[str]]":
    """The intent block's three sections, each as its list of bullet lines. ONE parser, at both doors.

    The format is the one `pr-adoption.md` writes and this file's docstring shows: three `##` headings,
    each followed by `- ` bullets. Nothing else about the file is read — a human may put whatever prose
    they like around it, and often should.

    **ALL THREE HEADINGS ARE REQUIRED, AND `## Purpose` AND `## Threat model` MUST EACH HAVE AT LEAST ONE
    BULLET.** They are the intent's two ANCHORS, and a finding gates by naming one of them: a `## Purpose`
    line quoted verbatim, or an actor from the `## Threat model` who can really write the bad input. Empty
    either one and the guard on that side has no input — and a guard whose input can be ABSENT never fires.
    An empty `## Purpose` forces every finding to anchor to `-`. An empty `## Threat model` names NO actor,
    so nothing a reviewer finds can be anchored to one, and REAL, REACHABLE defects are then discharged as
    non-gating: the exact failure this intent block exists to prevent, running backwards. A section that can
    be empty is a section that will be.

    **`## Non-goals` MAY be empty, and that is not an oversight — it is the one section where empty MEANS
    something.** "We exclude nothing" is a complete, honest answer, and it is the DEFAULT answer: it makes
    the reviewer's job strictly harder (nothing is off-limits), so nobody can weaken a review by leaving it
    blank. An empty threat model is not the analogous statement — "no actor can write any input this code
    reads" is not a scope decision, it is a section nobody filled in — and unlike the other two it is not a
    claim a reviewer would ever WRITE. So the rule is drawn where the risk is: the two anchors must say
    something; the exclusions may say nothing.

    **AND NO `## Purpose` BULLET MAY BE THE STRING `NO_PURPOSE` (`-`).** That value is the SENTINEL a
    finding types (`--purpose -`) to say it anchors to no purpose. A purpose line that IS that string is a
    sentinel masquerading as data: a finding quoting it verbatim carries `purpose == NO_PURPOSE`, which
    `gating()` reads as "anchors to nothing" and discharges — turning a real, anchored finding non-gating.
    The write door refuses it here, so the real purpose lines and the absent-marker can never overlap.
    """
    sections: dict[str, list[str]] = {}
    current: "str | None" = None
    for raw in text.splitlines():
        line = raw.strip()
        if line in INTENT_SECTIONS:
            if line in sections:
                # MUTATE:intent-duplicate-section:pass
                raise Defect(
                    f"{path.name}: `{line}` appears TWICE — two purposes are two intents, and a finding "
                    f"quoting a line from one of them would be anchored to a document that says two things"
                )
            sections[line] = []
            current = line
        elif line.startswith("#"):
            current = None  # some other heading: the intent's sections end here
        elif current is not None and line.startswith("- "):
            sections[current].append(line[2:].strip())
    missing = [h for h in INTENT_SECTIONS if h not in sections]
    if missing:
        # MUTATE:intent-missing-section:pass
        raise Defect(
            f"{path.name} is missing {missing} — an intent block is all three sections. `{PURPOSE_H}` is "
            f"what a finding QUOTES, `{THREAT_H}` is what BOUNDS the adversarial sweep, and "
            f"`{NON_GOALS_H}` is the only thing that can say a gap was DELIBERATE. Two of three is not a "
            f"weaker intent; it is one the reviewer cannot be measured against"
        )
    if not sections[PURPOSE_H]:
        # MUTATE:intent-empty-purpose:pass
        raise Defect(
            f"{path.name}: `{PURPOSE_H}` has no bullets — every finding would then anchor to {NO_PURPOSE!r} "
            f"by force, and a guard whose input can be ABSENT never fires. State at least one line the PR "
            f"must do"
        )
    if not sections[THREAT_H]:
        # MUTATE:intent-empty-threat-model:pass
        raise Defect(
            f"{path.name}: `{THREAT_H}` has no bullets — it NAMES THE ACTORS a finding may anchor to, so an "
            f"empty one names none, and a finding that cannot reach an actor is discharged as NON-GATING. "
            f"That turns the guard INSIDE OUT: real, reachable defects would be waved through, which is the "
            f"failure this block exists to prevent. State who can write the inputs this code reads — and who "
            f"cannot"
        )
    if NO_PURPOSE in sections[PURPOSE_H]:
        # MUTATE:intent-purpose-is-sentinel:pass
        raise Defect(
            f"{path.name}: a `{PURPOSE_H}` bullet is {NO_PURPOSE!r} — that is the SENTINEL a finding uses to "
            f"say it anchors to NO purpose (`--purpose {NO_PURPOSE}`), so a purpose line that IS that string "
            f"collides with the marker for its own absence: a finding quoting it VERBATIM carries "
            f"`purpose == {NO_PURPOSE!r}`, and `gating()` then reads a REAL, anchored finding as anchoring to "
            f"nothing and discharges it. A purpose is a thing the PR must DO; {NO_PURPOSE!r} names none, so it "
            f"is not one. State the line the PR must do, or drop the bullet"
        )
    return sections


def load_intent(path: Path) -> "dict[str, list[str]]":
    """The PR's intent, or a Defect saying it is not there.

    **A MISSING INTENT IS NOT AN EMPTY INTENT.** It is refused, loudly, and it is refused for EVERY pass —
    not merely for one that has a finding to anchor. A finding cannot be anchored to a document that does
    not exist, and the alternative (treat every `purpose` as unverifiable and wave it through) hands the
    reviewer a field it can write anything into. Adoption writes this file before the PR's first review
    pass is ever dispatched (`pr-adoption.md`), so its absence means the run skipped a step, never that the
    PR has no purpose.

    **BOTH DOORS CALL IT, AND SO DOES THE PASS ITSELF.** `cmd_finding_add` calls it to check an anchor at
    the moment the reviewer records one; `check_findings_file` calls it to check every anchor already on
    disk; and `evaluate` calls it for EVERY pass it judges — a pass with zero findings has nothing to
    anchor and is still measured against an intent, because "was this reviewer told what the PR is FOR?"
    is a question about the PASS, not about its findings. One function, one definition, three callers.

    **THE ABSENCE HAS ITS OWN MESSAGE, AND THAT IS NOT DECORATION.** Every other artifact this tool reads is
    written by the ORCHESTRATOR AT DISPATCH, so `read_text`'s message says so — and for the intent that
    sentence is a LIE with a recovery attached: it is written at ADOPTION, long before, and the fix is not
    to re-run the reviewer but to write the file and re-dispatch. A missing intent is the one `unusable`
    that is NOT a reviewer failure, and a driver that follows the generic message re-rolls a reviewer
    forever against a PR that still has no intent.
    """
    if not path.exists():
        # MUTATE:intent-missing-file:pass
        raise Defect(
            f"no intent block at {path} — THE RUN SKIPPED A STEP, and this is not a reviewer failure. "
            f"`{INTENT_NAME.format(pr='<pr>')}` is written at ADOPTION (`pr-adoption.md` step 3a), before "
            f"the PR's first review pass is ever dispatched, and EVERY pass is measured against it — a pass "
            f"that found nothing included. Re-rolling the reviewer cannot produce one. Write the intent "
            f"block, then re-dispatch the pass"
        )
    return parse_intent(read_text(path, "intent block"), path)


def check_writer_repro(rec: dict, where: str) -> None:
    """Does the reviewer's own REPRO contradict the WRITER it declared?

    The one automatic check on the one field this tool lets a reviewer judge. A reproduction that says "I
    mutated it in memory" describes a developer with a text editor — there is no input and no actor in it —
    so it MUST be `dev-time`. A finding that claims a real-world writer while reproducing itself by editing
    the code is either mis-declared or mis-reproduced, and either way its `writer` cannot be trusted to
    decide whether the PR merges.

    It FAILS SAFE and only ever costs a re-review: it can refuse a pass, never demote a finding.
    """
    hit = SOURCE_EDIT_RE.search(rec["repro"])
    if hit is not None and rec["writer"] != "dev-time":
        # MUTATE:writer-contradicts-repro:pass
        raise Defect(
            f"{where}: `writer` is {rec['writer']!r}, but the repro says {hit.group(0)!r} — that is an EDIT "
            f"TO THE SOURCE UNDER REVIEW, and the only actor who can perform it is a developer with a text "
            f"editor. Its writer is `dev-time`. Declare it, or reproduce the defect with an input the "
            f"writer you named can actually supply — if there is one, this is a DIFFERENT and much more "
            f"serious finding, and it should say so"
        )


def check_finding(rec: dict, where: str, purposes: "list[str]") -> None:
    """ONE finding, checked to the exact shape — and ANCHORED. Run at BOTH doors, so there is one
    definition of what a finding IS.

    `purposes` is the intent's `## Purpose` bullets. **The `purpose` field must be one of them, VERBATIM,
    or the literal `-`.** That is what makes the anchor a fact rather than a claim: the reviewer cannot
    invent a purpose line to justify a finding, because the only strings that validate are the ones the
    intent already says. It is the same discipline as every identifier in this file — one legal form, no
    repair — applied to a whole line of prose.

    `rec: dict` — and unlike `check_unit`, that IS a promise every caller can keep. A finding is never
    nested inside another record, so the only two things that reach here are a line `parse_lines` has
    already proved is a JSON object, and the dict `finding-add` builds from its own flags. There is no
    third door, so there is no "not an object" case to guard — and a guard for a case that cannot occur is
    a rule no fixture can ever kill.
    """
    if set(rec) != FINDING_KEYS:
        missing = sorted(FINDING_KEYS - set(rec))
        extra = sorted(set(rec) - FINDING_KEYS)
        # MUTATE:finding-keys:pass
        raise Defect(
            f"{where}: a finding carries EXACTLY {sorted(FINDING_KEYS)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected key(s) {extra} — nothing reads them, so whatever they assert is neither "
               f"verified nor refuted" if extra else "")
        )
    if rec["type"] != FINDING:
        # MUTATE:finding-row-type:pass
        raise Defect(f"{where}: type is {rec['type']!r}, and a findings file holds only {FINDING!r} records")
    for field in sorted(FINDING_KEYS - {"type"}):
        if not isinstance(rec[field], str):
            # MUTATE:finding-non-string:continue
            raise Defect(
                f"{where}: `{field}` is {rec[field]!r}, not a string — a value we cannot read is not one we "
                f"may hand to a comparison and hope"
            )
    # The CITATION. `line` is an identifier by this file's own definition — a value two doors compare —
    # so it goes through `check_id`, the one validator, exactly as a sha or a unit id does.
    check_id("line", rec["line"], where)
    for field in FINDING_STRINGS:
        if not rec[field].strip():
            # MUTATE:finding-blank:continue
            raise Defect(
                f"{where}: `{field}` is blank — a finding names the FILE it is in, the REPRO that makes it "
                f"fail, and the FIX. A blank one of those is a finding with nothing behind it, and this is "
                f"the field a fix subagent is dispatched with"
            )
    if rec["writer"] not in WRITERS:
        # MUTATE:finding-writer-enum:pass
        raise Defect(
            f"{where}: `writer` is {rec['writer']!r} — it names WHO CAN ACTUALLY PUT THE BAD INPUT THERE, "
            f"from a CLOSED enum: {list(WRITERS)}. A value outside it is not a new kind of actor, it is a "
            f"field nobody filled in. `hand-edit` = only by hand-editing a local git-ignored file the driver "
            f"owns; `dev-time` = only by editing the source of the code under review"
        )
    if rec["purpose"] != NO_PURPOSE and rec["purpose"] not in purposes:
        # MUTATE:finding-purpose-anchor:pass
        raise Defect(
            f"{where}: `purpose` is {rec['purpose']!r}, which is NOT a line of this PR's `{PURPOSE_H}` "
            f"block. It must be one of them VERBATIM, or the literal {NO_PURPOSE!r} — a purpose the reviewer "
            f"paraphrases is a purpose the reviewer WROTE, and the whole point of the anchor is that it is "
            f"the PR's claim and not the finding's. The stated purpose is:\n  "
            + "\n  ".join(f"- {p}" for p in purposes)
        )
    check_writer_repro(rec, where)


def findings_name(path: Path) -> "re.Match[str]":
    """THE findings artifact's name rule — one statement, and every door runs it.

    It RETURNS the match, because the name is not merely checked: the `pr` in it is what locates the intent
    this file's findings must anchor to. Checking the name and then re-deriving the pr somewhere else is how
    two doors come to disagree about which PR a file belongs to.
    """
    m = FINDINGS_NAME_RE.match(path.name)
    if m is None:
        # MUTATE:findings-name-shape:return FINDINGS_NAME_RE.match("review-1-1.findings.jsonl")
        raise Defect(
            f"{path.name} is not a findings artifact's name — it is `review-<pr>-<n>.findings.jsonl`, or "
            f"`review-<pr>-<n>.a<k>.findings.jsonl` for launch attempt k >= 2. `verify` never takes this "
            f"path: it DERIVES it from the progress file's name, so findings written under any other name "
            f"are findings nothing will ever read — and a NOT SATISFIED pass would then be refused for "
            f"recording no finding while its findings sat on disk one filename away"
        )
    return m


def check_findings_file(text: str, path: Path) -> "list[dict]":
    """Every rule a findings artifact's BYTES must satisfy — the same statement at both doors.

    The intent is loaded from the `pr` this file's own NAME carries, and only once there is a finding to
    anchor: with no findings there is nothing to anchor, so this function has nothing to say.

    **THAT IS A STATEMENT ABOUT THIS FILE, AND IT IS NOT — EVER — A STATEMENT ABOUT THE PASS.** "No
    findings, therefore no intent needed" was once true of both, and it was the hole: a pass with no
    findings never loaded the intent at all, so a SATISFIED pass on a PR whose intent was never written
    verified `ok`. The pass-level rule lives in `evaluate`, which loads the intent for EVERY pass it
    judges, whatever this file holds — including when it does not exist.
    """
    pr = findings_name(path).group("pr")
    records = parse_lines(text, path.name)
    if not records:
        return []
    purposes = load_intent(intent_path(path.parent, pr))[PURPOSE_H]
    for n, rec in enumerate(records, start=1):
        check_finding(rec, f"{path.name} line {n}", purposes)
    return records


def findings_path(progress: Path) -> Path:
    """The pass's findings artifact, DERIVED from its progress file's name — never passed in, at any door."""
    return progress.parent / (progress.name[: -len(PROGRESS_SUFFIX)] + FINDINGS_SUFFIX)


def intent_path(parent: Path, pr: str) -> Path:
    """Where this PR's intent lives — beside the pass's artifacts, in the run dir.

    It takes the `pr` rather than sniffing it out of a filename, because every caller has ALREADY parsed
    the name it came from (that parse is what refused a misfiled artifact one statement earlier). A second
    name check here would be a rule no input can reach and no fixture can kill.
    """
    return parent / INTENT_NAME.format(pr=pr)


def load_findings(progress: Path) -> "list[dict]":
    """This pass's findings — `[]` when the artifact does not exist.

    **AN ABSENT FINDINGS FILE IS ZERO FINDINGS, AND THAT IS NOT A DEFECT.** A pass that found nothing
    records nothing, and "finding nothing is a fine and common result" is the reviewer's own contract. What
    an absent file is NOT is a licence to return NOT SATISFIED: `decide` refuses that pass, because a
    verdict that blocks a PR with no gating finding behind it is a verdict nobody can act on and nobody can
    check.

    **AND IT IS NOT A LICENCE TO SKIP THE INTENT EITHER.** This function is the one place a whole artifact
    is allowed to be absent, so it is the one place that could quietly take the intent check down with it —
    and it DID: an absent findings file returned `[]` here, `load_intent` was never reached, and a pass on
    a PR with NO intent block at all counted. `evaluate` loads the intent for every pass, so absence here
    now means exactly what it says — zero findings — and nothing more.
    """
    path = findings_path(progress)
    if not path.exists():
        return []
    return check_findings_file(read_text(path, "findings file"), path)


# --- the progress events -------------------------------------------------------------------------

def check_event(rec: dict, where: str) -> None:
    """One progress-file event, checked to the EXACT shape. Run at BOTH doors, so there is one definition.

    Order matters: the TYPE decides which key set applies, the KEYS decide which fields exist, and only
    then may a field's VALUE be read. Reading a value before its shape is known is how a tool crashes
    instead of returning a verdict — and a crash is not a verdict.
    """
    kind = rec.get("type")
    status = rec.get("status") if kind == PROGRESS else None
    if kind not in (IDENTITY, PROGRESS, AMENDMENT):
        # MUTATE:unknown-event:return
        raise Defect(
            f"{where}: UNRECOGNISED event type {kind!r} — a line we cannot understand is not a line we may "
            f"read past; it is invisible to every rule below"
        )
    if kind == PROGRESS and status not in STATUSES:
        # MUTATE:bad-status:status = STARTED
        raise Defect(
            f"{where}: `status` is {status!r} — the only unit-progress statuses are {list(STATUSES)}"
        )
    keys = EVENT_KEYS[(kind, status)]
    if set(rec) != keys:
        missing = sorted(keys - set(rec))
        extra = sorted(set(rec) - keys)
        # MUTATE:event-keys:pass
        raise Defect(
            f"{where}: a {kind!r} event carries EXACTLY {sorted(keys)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected key(s) {extra} — nothing reads them, so they are present and NOT COUNTED"
               if extra else "")
        )
    for field in sorted(keys - {"proposed_unit"}):
        if not isinstance(rec[field], str):
            # MUTATE:non-string:continue
            raise Defect(
                f"{where}: `{field}` is {rec[field]!r}, not a string — a value we cannot read is not one "
                f"we may hand to a comparison and hope (it used to CRASH the tool)"
            )
    if kind == PROGRESS:
        # The unit this event names is the SAME identifier the plan's `id` is, checked by the SAME table —
        # so the two artifacts cannot come to hold two spellings of one unit. It is checked HERE, on the
        # event, and not only against the plan: "not in the plan" is the wrong thing to tell someone who
        # typed ` u01 `, and it is what this tool used to say — after quietly stripping the value first.
        check_id("unit", rec["unit"], where)
    if kind == AMENDMENT:
        # The amendment is the ONE event a reviewer really does hand-write (it is exempt from the
        # emit-only rule), so it is the one whose fields nothing upstream has already shaped. Its `ts` had
        # NO check at all beyond "is a string" — the identity's clock was guarded and this one, the same
        # kind of value, was not. It is what says WHEN the reviewer said the plan was wrong, and the
        # orchestrator rules on amendments in order; a `ts` that is not a time cannot be ordered.
        if not TS_RE.match(rec["ts"]) or not real_utc(rec["ts"]):
            # MUTATE:amendment-ts:pass
            raise Defect(
                f"{where}: `ts` is {rec['ts']!r}, not a real UTC ISO-8601 time (YYYY-MM-DDThh:mm:ssZ) — "
                f"the same clock rule the `pass_identity` obeys, and for the same reason: a value that is "
                f"not a moment cannot be compared to one"
            )
        if not rec["reason"].strip():
            # MUTATE:amendment-blank-reason:pass
            raise Defect(
                f"{where}: `reason` is blank — an amendment is a CLAIM that the plan misses a dimension, "
                f"and the orchestrator must RULE on it. A ruling needs something to rule on; blank `reason` "
                f"is the evidence-free `done` of the amendment world"
            )
        check_unit(rec["proposed_unit"], f"{where} proposed_unit")
    if kind == PROGRESS and status == DONE and not rec["evidence"].strip():
        # MUTATE:empty-evidence:pass
        raise Defect(
            f"{where}: a {DONE!r} event carries CONCRETE evidence (a file:line, a backticked span, a "
            f"filename) — blank evidence is a claim that a unit was checked, with nothing behind it"
        )


def check_progress(rec: dict, units: "dict[str, dict]", announced: "set[str]", done: "dict[str, str]",
                   where: str) -> None:
    """ONE unit-progress event, judged against the PLAN and against everything the file ALREADY says.

    **This is the single implementation of the three rules that govern a `started`/`done`, and BOTH doors
    call it**: `cmd_emit` before it appends, and `walk_progress` — which is what `verify` re-derives from
    the bytes — as it replays the file. It is one function and not three checks written twice, because the
    two failures are the same failure: a rule enforced at ONE door is not enforced, and a rule enforced at
    both doors by TWO implementations is a rule waiting to acquire two definitions.

    The SECOND `done` proved both halves at once. `verify` refused it and `emit` WROTE it — the reviewer
    got exit 0, the file grew two accounts of one unit, and the pass was thrown away fifteen minutes later
    for a defect the tool had just helped it commit.

    ORDER IS PART OF THE RULE. Unplanned first: a reviewer self-granting a unit must be told the unit is
    not in the plan — telling it "no earlier `started`" would be true, and the wrong lesson.
    """
    unit = rec["unit"]
    if unit not in units:
        # MUTATE:unplanned-unit:pass
        raise Defect(
            f"{where}: progress for unit {unit!r}, which is NOT IN THE PLAN — the reviewer never rewrites "
            f"the plan or self-grants units, and progress counts only when it references a PLANNED unit. "
            f"Planned: {sorted(units)}. If the plan is missing a dimension, raise a plan_amendment_request "
            f"instead"
        )
    if rec["status"] != DONE:
        return
    if unit not in announced:
        # MUTATE:done-without-started:pass
        raise Defect(
            f"{where}: {DONE!r} for unit {unit!r} with no earlier {STARTED!r} for it — a unit that was "
            f"never begun cannot have been finished. The reviewer emits {STARTED!r} when a unit BEGINS and "
            f"{DONE!r} when it ends, so a `{DONE}` standing alone (or standing ABOVE its `{STARTED}` in "
            f"this append-only file) is not the record of a review that happened; it is a file with the "
            f"right lines in it"
        )
    if unit in done:
        # MUTATE:duplicate-done:pass
        raise Defect(
            f"{where}: a SECOND {DONE!r} for unit {unit!r} — the file would offer two accounts of one "
            f"unit, and nothing says which was read. A unit is finished ONCE; if what you found changed, "
            f"the pass is what re-runs, not the line"
        )


def walk_progress(events: "list[dict]", units: "dict[str, dict]") -> "tuple[set[str], dict[str, str]]":
    """Replay a progress file's unit events IN ORDER under `check_progress`, and return what it says: the
    units ANNOUNCED, and the units DONE with their evidence.

    BY ORDER, never by presence: `announced` only ever holds units a line ALREADY READ announced, so a
    `started` that appears BELOW its `done` cannot satisfy it. The file is APPEND-ONLY, so its order IS
    the order the events happened in, and a forger who must fabricate the `started` FIRST has to fabricate
    the whole sequence — which is precisely the thing the file is evidence of.

    The WRITE door replays this same walk over the bytes already on disk, because it has nothing else to
    ask: a reviewer is many `emit` invocations, each a fresh process, and the only thing that survives
    between them is the file. The file is the memory, and this is how `emit` knows what it already says.
    """
    announced: set[str] = set()
    done: dict[str, str] = {}
    for n, rec in enumerate(events, start=1):
        if rec["type"] != PROGRESS:
            continue
        check_progress(rec, units, announced, done, f"line {n}")
        if rec["status"] == DONE:
            done[rec["unit"]] = rec["evidence"]
        else:
            announced.add(rec["unit"])
    return announced, done


def check_identity_shape(ident: dict, where: str) -> None:
    """Every VALUE in a `pass_identity`, checked once — and therefore at BOTH doors, because `identity`
    (write) and `check_identity` (read) both call this and there is no second implementation to drift.

    The identity is the pass's attempt id and its dispatch clock, and three rules downstream depend on it:
    a late verdict is ignored unless its attempt id still matches; the ~5-minute launch deadline is
    measured from `dispatched_at`; `launch_attempt` is how a *later* wake — possibly a fresh agent — knows
    the pass was already relaunched once. Every one of those is a COMPARISON, and a comparison against a
    malformed value is not one.
    """
    # FOUR IDENTIFIERS, ONE VALIDATOR. The sha rule and the number rules used to be written out here, and
    # writing a rule out is how it comes to exist in two places: the sha is ALSO what `verify`'s caller
    # passes, the numbers are ALSO what the filename carries. They are `ID_FORMATS` rows now, and every door
    # that takes one runs this same statement over it.
    for field in ("head_sha", "pr", "pass", "launch_attempt"):
        check_id(field, ident[field], where)
    if not TS_RE.match(ident["dispatched_at"]):
        # MUTATE:identity-dispatched-at:pass
        raise Defect(
            f"{where}: `dispatched_at` is {ident['dispatched_at']!r}, not a UTC ISO-8601 timestamp "
            f"(YYYY-MM-DDThh:mm:ssZ) — it is the LAUNCH DEADLINE's clock, and a deadline measured from a "
            f"time nobody can parse never fires"
        )
    if not real_utc(ident["dispatched_at"]):
        # MUTATE:identity-dispatched-at-real:pass
        raise Defect(
            f"{where}: `dispatched_at` is {ident['dispatched_at']!r} — the right SHAPE, and not a real UTC "
            f"time. A month 99 is not a month. The shape check alone could not fire on this, and the "
            f"deadline it exists to protect is measured by ARITHMETIC on this value: a moment that does "
            f"not exist is one no clock ever passes"
        )


def check_identity(events: "list[dict]", pr: str, npass: str, attempt: str) -> dict:
    """The `pass_identity` line: exactly one, FIRST, well-formed, and agreeing with the NAME it is filed
    under. **Everything here is a property of the BYTES ALONE** — which is exactly why both doors run it.

    The commit comparison is NOT here; it is `check_head`, and the split is the line between "is this file
    readable back?" (this) and "does what it says still describe the world?" (that). A write door can
    answer the first and cannot answer the second: `emit`'s CLI has no `--head-sha` and never will (its
    flags are a public contract). So `emit` runs THIS, and it is the whole of what a progress file must
    satisfy for `verify` to be able to read it at all.
    """
    ids = [e for e in events if e["type"] == IDENTITY]
    if not ids:
        # MUTATE:identity-missing:ids = [dict(events[0])]
        raise Defect(
            "the progress file holds NO `pass_identity` — nothing binds these events to a PR, a pass, an "
            "attempt or a COMMIT, so they could describe any review of anything"
        )
    if len(ids) > 1:
        # MUTATE:identity-duplicate:pass
        raise Defect(
            f"the progress file holds {len(ids)} `pass_identity` lines — if they disagreed the file would "
            f"describe two passes, and only one of them would ever be read"
        )
    if events[0]["type"] != IDENTITY:
        # MUTATE:identity-not-first:pass
        raise Defect(
            "`pass_identity` is not the FIRST line — the orchestrator writes it BEFORE the reviewer is "
            "launched, so an event ahead of it was written by something that had not been dispatched yet"
        )
    ident = ids[0]
    check_identity_shape(ident, "`pass_identity`")

    named = (ident["pr"], ident["pass"], ident["launch_attempt"])
    if named != (pr, npass, attempt):
        # MUTATE:identity-path-mismatch:pass
        raise Defect(
            f"`pass_identity` names pr/pass/attempt {named} but the file it is IN is attempt {attempt} of "
            f"pass {npass} for PR {pr} — substituting one attempt's paths into another is the silent "
            f"self-defeat the attempt-scoped artifacts exist to prevent: the live pass writes into the "
            f"DEAD attempt's file and reads as never launched"
        )
    return ident


def check_head(ident: dict, head_sha: str) -> None:
    """The pass's commit against the PR's LIVE head — the ONE rule that is not about the file.

    It compares the artifact to THE WORLD, and the world is not in the bytes: a file that reads back
    perfectly becomes stale the moment someone pushes. So it is the one read-door rule no write door can
    run, and it is the honest GAP in "a write is refused unless the result would verify" — a write can
    guarantee the file it produces is READABLE, never that the tip will not move afterwards. Nothing a
    write door does can cause this defect (`identity` is handed the sha it writes; `emit` appends events
    that name no commit at all), so the gap costs nothing: it is a rule about time, not about bytes.
    """
    if ident["head_sha"] != head_sha:
        # MUTATE:identity-head-mismatch:pass
        raise Defect(
            f"this pass ran on {ident['head_sha']} but the PR's head is {head_sha} — its verdict describes "
            f"content that is no longer there, and PR content changing is exactly what voids a tally"
        )


# --- the verdict ---------------------------------------------------------------------------------

def decide(events: "list[dict]", units: "dict[str, dict]", ruled: int,
           findings: "list[dict]", verdict: "str | None") -> "tuple[str, str]":
    """Given SOUND artifacts: does this pass COUNT? (Its report is still not read. That is the point.)

    The per-event rules — planned unit, `done` follows `started`, no SECOND `done` — are `check_progress`,
    replayed here by `walk_progress`. They are not restated: they are the SAME statements `emit` runs, so
    what this door refuses to read is exactly what that door refuses to write.

    **The `started` rule was PROSE and enforced by NOBODY, and it is the one the tool most needed.** A
    progress file with a valid identity and a `done` for EVERY planned unit — and NOT ONE `started` —
    verified `ok`: the tool that exists to prove a review HAPPENED accepted a review that demonstrably did
    not. Skip straight to "done" for every unit and the gate was satisfied on zero evidence of work. A
    `done` with no `started` is not progress, exactly as an empty plan is not a plan.

    **`verdict` IS TOLD TO THIS TOOL, NEVER READ BY IT — AND THE LINE IS THE SAME LINE AS ALWAYS.** This
    function still does not open `review-<pr>-<n>.txt` and still cannot SAY `SATISFIED`. The orchestrator
    reads the reviewer's VERDICT line, exactly as before, and passes what it read (`--verdict`) so that ONE
    coherence rule can be checked mechanically. **THE RULE IS AN IF AND ONLY IF, AND IT IS ENFORCED IN BOTH
    DIRECTIONS: NOT SATISFIED exactly when at least one GATING finding stands.**

    **AND ON A COMPLETE PASS THE VERDICT IS NOT OPTIONAL — an ABSENT one is `unusable`, never `ok`.** It
    was optional, and that made the coherence rule above a guard a caller could switch off by FORGETTING a
    flag: a complete pass verified with no `--verdict` returned `ok`, so a driver that dropped it merged a
    PR whose reviewer had returned SATISFIED over a GATING finding it had itself recorded. That is the same
    defect as the intent that could be missing on exactly the passes that count — **a guard whose input can
    be ABSENT never fires** — and it is closed the same way: the input is DEMANDED. A verdict a driver has
    not read yet is not a reason to skip the rule; it is a reason not to be at this door yet.

      * NOT SATISFIED and NO gating finding — a verdict that blocks a PR and names nothing that blocks it.
      * SATISFIED and a gating finding that STANDS — the reviewer recorded a defect that anchors to the PR's
        own purpose, or that a named actor can really reach, and then passed the PR anyway. Half the
        contract was enforced and this half was not, so exactly that pass verified `ok`: the finding is
        real, it gates by the rule the reviewer itself applied when it recorded it, and the gate waved it
        through. If the reviewer believes it does NOT gate, the fix is to say so where it is SAID — a
        finding that anchors to nothing is `purpose = -` and a no-adversary `writer`, and `finding-add`
        prints NON-GATING when it writes one. What may never happen is a finding that reads as gating in
        the artifact and as ignorable in the verdict.

    Note which direction EITHER half can move a pass — both can only ever REFUSE one. Nothing here can turn
    a NOT SATISFIED into a pass, raise `reviews_ok`, or merge anything; a tool that could accept would merge
    a PR nobody reviewed, and a bug in one that can only refuse costs a re-review.
    """
    _, done = walk_progress(events, units)  # drops the announced set

    amendments = [e for e in events if e["type"] == AMENDMENT]
    unruled = len(amendments) - ruled
    if unruled > 0:
        # MUTATE:amended:pass
        return AMENDED, (
            f"the reviewer raised {len(amendments)} plan amendment(s), {unruled} not yet ruled on: "
            + "; ".join(f"{a['proposed_unit']['id']}: {a['reason']}" for a in amendments[ruled:])
            + ". A plan the REVIEWER says is incomplete is not a plan this pass can be counted against. "
              "Fold it into the plan and restart the pass, or record why not, then pass "
              "--amendments-ruled to say so"
        )

    missing = sorted(set(units) - set(done))
    if missing:
        # MUTATE:incomplete:pass
        return INCOMPLETE, (
            f"{len(done)}/{len(units)} planned units are done; no `{DONE}` event for {missing} — the pass "
            f"has not covered its plan"
        )

    # The pass is COMPLETE from here down — every planned unit is done and no amendment is outstanding — so
    # there IS a report, and the ONE rule this tool can check mechanically has an input it may not be denied.
    # Ordered BELOW `incomplete` on purpose: a pass still in flight has no verdict to state, and asking it
    # for one would refuse a reviewer for being unfinished, which `incomplete` already says better.
    if verdict is None:
        # MUTATE:verdict-missing-on-complete:pass
        return UNUSABLE, (
            f"all {len(units)} planned units are done, so this pass is FINISHED — and no verdict was given "
            f"to check it against ({VERDICTS} — what the report's `VERDICT:` line says). The coherence rule "
            f"is the only thing standing between a reviewer that returns SATISFIED over a GATING finding it "
            f"recorded itself and a PR that merges anyway, and a rule whose input may be OMITTED is a rule a "
            f"caller switches off by forgetting a flag. Read the report's VERDICT line and state it"
        )

    blocking = [f for f in findings if gating(f)]
    if verdict == NOT_SATISFIED and not blocking:
        # MUTATE:not-satisfied-without-gating-finding:pass
        return UNUSABLE, (
            f"this pass returned NOT SATISFIED and recorded NO GATING finding ({len(findings)} finding(s), "
            f"all NON-GATING). A verdict that blocks a PR must name what blocks it: a finding that DEFENDS "
            f"a line of the PR's stated purpose, or one an actor in its threat model can actually reach. A "
            f"finding that anchors to NEITHER is a true statement about code nobody can reach, in service of "
            f"nothing the PR promised — record it as a follow-up and return SATISFIED. That is not a "
            f"loophole; it is the difference between the findings that were worth 21 rounds and the ones "
            f"that were not"
        )
    if verdict == SATISFIED and blocking:
        # MUTATE:satisfied-with-gating-finding:pass
        return UNUSABLE, (
            f"this pass returned SATISFIED while {len(blocking)} GATING finding(s) STAND: "
            + "; ".join(f"{f['file']}:{f['line']}" for f in blocking)
            + f". The contract is an IF AND ONLY IF — NOT SATISFIED exactly when at least one GATING finding "
              f"stands — and only its other half was ever enforced, so a pass could record a blocking defect "
              f"and pass the PR anyway. A gating finding is one that DEFENDS a line of the PR's stated "
              f"purpose or that an actor in its threat model can really reach, and the reviewer said so when "
              f"it recorded it. If it does not gate, say so where it is SAID: `purpose` is `-` and `writer` "
              f"is one of {list(NO_ADVERSARY)}. A finding cannot read as blocking in the artifact and as "
              f"ignorable in the verdict"
        )
    return OK, (
        f"all {len(units)} planned units are done with evidence, on {events[0]['head_sha']}, no unruled "
        f"amendments, {len(blocking)} gating finding(s) of {len(findings)}. This says the ARTIFACTS are "
        f"sound — NOT that the pass is SATISFIED. Read the VERDICT line in the report"
    )


def parse_name(path: Path) -> "tuple[str, str, str]":
    """(pr, pass, launch_attempt) — from the FILENAME, which is the only thing that says which pass and
    which launch attempt these bytes are."""
    m = NAME_RE.match(path.name)
    if not m:
        # MUTATE:name-shape:return ("0", "0", "1")
        raise Defect(
            f"{path.name} is not a progress artifact's name — it is `review-<pr>-<n>.progress.jsonl`, or "
            f"`review-<pr>-<n>.a<k>.progress.jsonl` for launch attempt k >= 2. The name is what binds "
            f"these bytes to a pass and an ATTEMPT; a file wearing another name was written by something "
            f"we do not know"
        )
    return m.group("pr"), m.group("pass"), m.group("attempt") or "1"


def plan_path(progress: Path) -> Path:
    """The pass's plan, DERIVED from its progress file's name — never passed in, at either door.

    One derivation, so `emit` cannot be judged against a plan `verify` will not open. (The plan is
    per-PASS, not per-attempt: a relaunch reuses it unchanged, so the attempt is not in its name.)
    """
    pr, npass, _ = parse_name(progress)  # drops the attempt
    return progress.parent / PLAN_NAME.format(pr=pr, **{"pass": npass})


def evaluate(progress: Path, head_sha: str, ruled: int = 0,
             verdict: "str | None" = None) -> "tuple[str, str]":
    """The whole read side. Every exception a rule can raise lands here as a VERDICT — never as a crash.

    **THE INTENT IS AN INPUT TO EVERY PASS, AND IT IS LOADED HERE FOR EXACTLY THAT REASON.** A pass is
    measured against what the PR is FOR: that is what this whole artifact set exists to make true, and a
    pass measured against nothing is the open-ended review that ran a PR through 21 rounds. So the question
    "is there an intent, and can it be read?" is asked of THE PASS, once, whatever the pass found — never
    delegated to a file that is allowed to be absent.

    It used to be asked only where a FINDING needed anchoring (`check_findings_file`), and a pass with no
    findings does not go there: an absent findings file returned `[]` and nothing ever looked for the
    intent. A SATISFIED pass with no findings is the ordinary case and the one that MERGES a PR, so the
    intent could be missing on precisely the passes that count. A guard whose input can be ABSENT never
    fires.

    The `pr` comes from the progress file's own NAME — the same parse `plan_path` derives the plan from —
    so a pass can no more be judged against another PR's intent than against another pass's plan, and there
    is no `--intent` flag for a caller to point somewhere else with.
    """
    try:
        plan = plan_path(progress)
        pr, _, _ = parse_name(progress)  # drops npass, attempt
        events, units = check_progress_file(text=read_text(progress, "progress file"), path=progress,
                                            plan=lambda: load_plan(plan), head_sha=head_sha)
        # MUTATE:intent-required:pass
        load_intent(intent_path(progress.parent, pr))
        return decide(events, units, ruled, load_findings(progress), verdict)
    except Defect as exc:
        return UNUSABLE, str(exc)


def check_events(events: "list[dict]", name: str) -> None:
    for n, rec in enumerate(events, start=1):
        check_event(rec, f"{name} line {n}")


# --- what "READABLE BACK" means, for each artifact — ONE statement of it, called at BOTH doors ----
#
# **ANYTHING THIS TOOL CAN WRITE, IT MUST BE ABLE TO READ BACK.** These two functions are that property,
# and they are the only definition of it. `verify` calls them to judge a pass; every write path calls them
# on the bytes it is ABOUT TO PRODUCE and refuses to write unless they hold (`write_line`). A write door
# that used its own notion of "well-formed" would be a second definition, and two definitions of one rule
# is how a tool comes to write a file it then refuses to read — which it DID: `emit --status started` on an
# EMPTY progress file exited 0, and `verify` then called that same file `unusable: NO pass_identity`. The
# tool accepted the reviewer's work and then told it the work did not count.
#
# Both take TEXT, never a path, because the file a write door must judge DOES NOT EXIST YET.


def check_progress_file(text: str, path: Path, plan: "Callable[[], dict[str, dict]]",
                        head_sha: "str | None" = None) -> "tuple[list[dict], dict[str, dict]]":
    """Every rule `verify` derives from a progress file's BYTES — the name it is filed under, its lines,
    its events, its identity, and the ORDER of its unit progress. Returns (events, plan units).

    ONE function, so the doors cannot compose the rules differently — and **the ORDER is part of the
    contract**, not an implementation detail: a file whose identity names another PR must be told THAT, not
    told its plan is missing (the plan's path is derived from the progress file's name, so a misfiled
    identity takes the plan's name down with it). Hence `plan` is a THUNK: the plan is not needed until the
    file's own events are replayed, so it is not loaded — and cannot fail — before the file has answered
    for itself.

    `head_sha` is optional for the one reason set out in `check_head`: it is the only rule here that
    compares the file to THE WORLD, and a write door has no `--head-sha` to compare against. Passing it is
    what makes this the WHOLE of `verify`'s read; omitting it makes this the whole of what a write door can
    guarantee about the file it produces. Nothing else differs between the doors.
    """
    pr, npass, attempt = parse_name(path)
    events = parse_lines(text, path.name)
    check_events(events, path.name)
    ident = check_identity(events, pr, npass, attempt)
    if head_sha is not None:
        check_head(ident, head_sha)
    units = plan()
    walk_progress(events, units)
    return events, units


def check_plan_file(text: str, path: Path) -> "dict[str, dict]":
    """…and the same, one artifact over: every rule `load_plan` derives from a plan file's BYTES.

    `load_plan`'s EMPTINESS rule is deliberately not here, and that is not an inconsistency — it is the
    reason this function exists. "A plan with no units is not a plan" is a rule about a plan a pass is
    JUDGED against; `plan-add` is how a plan stops being empty, so a write door that enforced it could
    never write the first unit. Every OTHER plan rule holds at both doors, by this statement.
    """
    if not PLAN_NAME_RE.match(path.name):
        # MUTATE:plan-name-shape:pass
        raise Defect(
            f"{path.name} is not a plan artifact's name — it is `review-<pr>-<n>.plan.jsonl`. `verify` "
            f"never takes the plan's path: it DERIVES it from the progress file's name, so a plan written "
            f"under any other name is a plan nothing will ever read. The pass would be refused for a "
            f"MISSING plan while its units sat on disk one filename away"
        )
    return plan_units(parse_lines(text, path.name), path.name)


def check_ruled(ruled: int) -> None:
    """`--amendments-ruled` is a CARDINALITY — how many amendments the orchestrator has ruled on — so its
    domain starts at ZERO, and this is the floor. The CEILING is the pass's own amendment count, and
    `cmd_verify` enforces it one statement later; together they are the whole domain, bounded on both sides.

    **A NEGATIVE VALUE WEDGES A PASS THAT WAS LEGITIMATELY EARNED.** `decide` computes `raised - ruled`, so
    `--amendments-ruled -1` on a sound, COMPLETE pass with no amendments at all gives `0 - (-1) = 1` unruled
    — and the pass comes back `amended`: "0 amendment(s), 1 not yet ruled on". There is no amendment to
    rule on, so there is no way to clear it: the verdict names a thing that does not exist. It fails SAFE
    (this tool can only ever SUBTRACT a pass, never grant one), and a pass withheld forever is still a pass
    withheld — the over-count rule below already refuses a ruling for an amendment that does not exist, and
    a NEGATIVE ruling is that same mistake with its sign flipped.
    """
    if ruled < 0:
        # MUTATE:caller-ruled-negative:pass
        raise OperatorError(
            f"--amendments-ruled {ruled} is negative — it is a CARDINALITY (how many amendments you have "
            f"already ruled on), so the smallest legal value is 0. A negative one is SUBTRACTED from the "
            f"amendments the pass raised, so a complete, sound pass with none at all would come back "
            f"{AMENDED!r} — '0 amendment(s), 1 not yet ruled on' — and no ruling could ever clear an "
            f"amendment that was never raised"
        )


def count_amendments(progress: Path) -> int:
    """How many amendments the file holds — read WITHOUT judging it, so `--amendments-ruled` can be
    checked against reality before any verdict is computed."""
    try:
        return sum(1 for e in read_lines(progress, "progress file") if e.get("type") == AMENDMENT)
    except Defect:
        return 0  # a file we cannot read has no countable amendments; `evaluate` will say so, loudly


# --- the write side (the same rules, at the other door) ------------------------------------------

def before_text(path: Path) -> str:
    """The bytes the file ALREADY holds — "" when it does not exist yet, and NOTHING ELSE means empty.

    **EMPTY MEANS NO BYTES.** It used to mean "no non-whitespace text" at one door and no bytes at the
    other, and a file with a blank line in it fell in the crack: `identity` called it fresh and wrote into
    it, `verify` then refused the artifact FOR THE BLANK LINE. A file with a blank line is not an empty
    file; it is a file with a blank line, and this returns it as such so the rules can say so.
    """
    return read_text(path, "file") if path.exists() else ""


def write_line(path: Path, before: str, rec: "dict[str, object]",
               readable_back: "Callable[[str], object]") -> str:
    """THE ONE WRITE. A record is appended ONLY IF the file it would PRODUCE reads back.

    Every write path in this tool goes through here, and `readable_back` is always one of the READ side's
    own whole-file functions — never a write-shaped restatement of them. So the property is structural
    rather than a rule someone remembered to repeat: **the tool cannot write a file it would refuse to
    read**, because the bytes are handed to the reader BEFORE they reach the disk.

    That is also what catches the defect neither door's per-record checks can see: a file whose last line
    has NO TRAILING NEWLINE. The append lands ON that line, fusing two events into one — and every
    record-level check passes, because the RECORD was never the problem. Only `before + line` shows it.
    """
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    # MUTATE:write-verifies-result:pass
    readable_back(before + line)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(line)
    return line


def cmd_emit(args) -> int:
    """Append one unit-progress event — the ONLY sanctioned way a reviewer records one.

    **It runs the READ side's functions, and no others**, over THREE things in this order: the EVENT it was
    asked for (`check_event`, `check_progress` — so the message names the mistake the reviewer just made),
    the file it is about to append INTO (`check_progress_file` — a file `verify` already refuses is not one
    to add a good line to), and the file it would PRODUCE (`write_line`). The last is what makes the
    property structural; the first two exist to say WHICH rule fired, and all three are the same
    statements, so there is no CLI-shaped second copy of any rule to drift from the one the verdict is
    computed with.

    **The file it appends into must ALREADY carry a valid `pass_identity`.** The orchestrator writes it
    before the reviewer is launched, so it is there — and this door used to not look: `emit --status
    started` on an EMPTY progress file exited 0, and `verify` then refused that very file for holding NO
    `pass_identity`. The tool wrote what it would not read, and told the reviewer it had succeeded.

    That is not symmetry for its own sake. Every rule this refuses at write, it refuses at the moment the
    reviewer makes the mistake — with a message it can act on — instead of the pass being thrown away
    fifteen minutes later by a `verify` the reviewer never sees.
    """
    path = Path(args.file)
    parse_name(path)  # validates the filename; return discarded
    # The RECORD IS THE FLAGS — VERBATIM. `--status done` with no `--evidence` is an event with no
    # `evidence` key, and `--status started --evidence x` is one carrying a key nothing reads, so the flags
    # are judged by the same `check_event` that judges a hand-written line and the evidence rule exists in
    # ONE place.
    #
    # **`--unit` IS NOT STRIPPED, AND THAT IS THE RULE, NOT AN OVERSIGHT.** It used to be, and that one
    # `.strip()` was the whole defect: the plan door accepted ` u01 ` verbatim while this door quietly
    # trimmed it, so `plan-add --id ' u01 '` exited 0 and `emit --unit ' u01 '` then said the unit was NOT
    # IN THE PLAN — while printing `Planned: [' u01 ']`. The plan held a unit this door could never match,
    # and the pass could never complete. An identifier now has ONE legal form and no door repairs it: the
    # id is refused where it ENTERS (`check_id`, from `check_event` below), by the same statement the plan
    # door refuses it with, and the value that reaches the file is the value the reviewer typed.
    rec: "dict[str, object]" = {"type": PROGRESS, "unit": args.unit, "status": args.status}
    if args.evidence is not None:
        rec["evidence"] = args.evidence
    check_event(rec, "the event you asked to emit (--unit/--status/--evidence)")

    plan = plan_path(path)
    text = read_text(path, "progress file")
    events, units = check_progress_file(text, path, lambda: load_plan(plan))
    announced, done = walk_progress(events, units)
    check_progress(rec, units, announced, done, "the event you asked to emit")
    # …and the file it would PRODUCE, through that same function. `units` is already loaded, so the thunk
    # just hands it back; nothing is re-derived, and nothing is re-stated.
    sys.stdout.write(write_line(path, text, rec, lambda after: check_progress_file(after, path, lambda: units)))
    return 0


def cmd_identity(args) -> int:
    """Write a pass's `pass_identity` — the line that used to be a `printf`, and once got a TRUNCATED SHA.

    It writes into a file that must hold NO BYTES. It used to demand only that the file hold no non-blank
    TEXT — so a whitespace-only file counted as fresh, the identity went in below the blank line, and
    `verify` then refused the artifact FOR THAT BLANK LINE. Two doors, two definitions of "empty", and the
    file in the crack was one this tool wrote and would not read.
    """
    path = Path(args.file)
    pr, npass, attempt = parse_name(path)
    text = before_text(path)
    if text:
        # MUTATE:identity-write-first:pass
        raise Defect(
            f"{path.name} is NOT EMPTY — it already holds {len(text)} byte(s), and `pass_identity` is the "
            f"FIRST line of a launch attempt's progress file, written before the reviewer starts. A "
            f"relaunch gets its OWN file (`review-<pr>-<n>.a<k>.progress.jsonl`), never this one. EMPTY "
            f"means NO BYTES: a file holding only a blank line is not empty, it is a file with a blank "
            f"line — `verify` refuses the pass for exactly that, so writing here would produce an artifact "
            f"this tool would then refuse to read"
        )
    rec: "dict[str, object]" = {
        "type": IDENTITY, "pr": pr, "pass": npass, "head_sha": args.head_sha,
        "launch_attempt": attempt, "dispatched_at": args.dispatched_at,
    }
    # The SAME two functions the read side runs — so a `pass_identity` this door writes is one `verify`
    # can never call malformed, and the sha/clock rules exist in exactly one place.
    check_event(rec, "the pass_identity you asked to write")
    check_identity_shape(rec, "the pass_identity you asked to write")
    # …and then the file it would PRODUCE, through the read side's own whole-file function. The EMPTY plan
    # is EXACT and not a shortcut: the guard above proved the file holds no bytes, so what this produces is
    # ONE line — the identity — and no unit-progress event a plan could have anything to say about. It is
    # also why `identity` does not require the plan to exist yet: the orchestrator writes both before
    # dispatch, and this door has no business imposing an order between them. (If the guard above is ever
    # weakened, this still refuses whatever was in the file: an event no plan names.)
    sys.stdout.write(write_line(path, text, rec, lambda after: check_progress_file(after, path, dict)))
    return 0


def cmd_plan_add(args) -> int:
    """Append one validated unit to a pass's plan — the artifact that used to be a shell heredoc."""
    path = Path(args.file)
    rec: "dict[str, object]" = {
        "type": UNIT, "id": args.id, "kind": args.kind, "target": args.target,
        "checks": list(args.check),
    }
    check_unit(rec, "the unit you asked to add")
    # The plan AS IT WOULD BE, run through the reader's own function: the NAME rule and the duplicate-id
    # rule fire from the one statement `load_plan` fires them from, never from a second copy that can drift
    # away from it. `check_plan_file` sees the produced BYTES, so a plan whose last line carries no newline
    # is refused rather than fused with the next unit into one line nothing can parse.
    #
    # The name is checked BEFORE the file is read, and by the same statement: a path that is not a plan's
    # name is not a file this tool reads at all, so `before_text` is not asked about it.
    text = before_text(path) if PLAN_NAME_RE.match(path.name) else ""
    sys.stdout.write(write_line(path, text, rec, lambda after: check_plan_file(after, path)))
    return 0


def cmd_finding_add(args) -> int:
    """Append ONE validated finding — the ONLY sanctioned way a reviewer records one.

    It is `emit`'s twin, one artifact over, and for the same reason: a finding used to be a PARAGRAPH in a
    report, so nothing could check its citation, bound its writer, or ask what it defended — and every
    finding therefore became a fix, and every fix became the next reviewer's hunting ground.

    The ANCHOR is checked HERE, at the moment the reviewer records it, and not fifteen minutes later by a
    `verify` the reviewer never sees: `--purpose` must quote a line of the PR's `## Purpose` block verbatim
    or be `-`, `--writer` must be in the enum, and the repro must not contradict the writer. A finding that
    cannot pass those is a finding the reviewer can still FIX while it is holding the evidence.
    """
    path = Path(args.file)
    rec: "dict[str, object]" = {
        "type": FINDING, "file": args.path, "line": args.line, "writer": args.writer,
        "purpose": args.purpose, "repro": args.repro, "fix": args.fix,
    }
    # The NAME first, and by the SAME statement the read door runs: a path that is not a findings
    # artifact's name is not a file this tool reads at all, and the `pr` in that name is what locates the
    # intent this finding must anchor to.
    pr = findings_name(path).group("pr")
    # …then the finding itself, checked HERE — while the reviewer is still holding the evidence, and not
    # fifteen minutes later by a `verify` it never sees.
    check_finding(rec, "the finding you asked to record",
                  load_intent(intent_path(path.parent, pr))[PURPOSE_H])
    sys.stdout.write(write_line(path, before_text(path), rec,
                                lambda after: check_findings_file(after, path)))
    # NEITHER of these is an error or a refusal — the finding is RECORDED either way. They are the tool
    # telling the reviewer WHAT IT JUST WROTE, because the verdict/findings rule is an IF AND ONLY IF and
    # a reviewer can get it wrong in BOTH directions: a NON-GATING finding turned into a NOT SATISFIED, or
    # a GATING one left out of the verdict. `verify` refuses the pass either way, fifteen minutes later,
    # by a tool the reviewer never sees; this is where it learns it, while it can still act.
    if not gating(rec):
        sys.stdout.write(
            f"# NON-GATING: this finding anchors to no `{PURPOSE_H}` line and its writer is "
            f"`{rec['writer']}` — nobody outside the machine can supply that input. It is RECORDED as a "
            f"follow-up and it MUST NOT produce NOT SATISFIED. If you believe it does gate, then either it "
            f"defends a stated purpose (quote that line in --purpose) or a real actor can write the input "
            f"(name them in --writer) — say which, do not simply re-file it.\n"
        )
    else:
        sys.stdout.write(
            f"# GATING: this finding ANCHORS — it defends a `{PURPOSE_H}` line, or `{rec['writer']}` can "
            f"really write that input, and you said so when you recorded it. So it BLOCKS: your verdict "
            f"MUST be NOT SATISFIED while it stands. A pass that records this and returns SATISFIED is "
            f"UNUSABLE and gets thrown away — the rule is NOT SATISFIED if and ONLY if at least one GATING "
            f"finding stands. If it does not really block, it is the ANCHOR that is wrong, not the verdict: "
            f"a finding that serves no stated purpose and that nobody outside the machine can trigger is "
            f"`--purpose -` with a `driver-only`/`hand-edit`/`dev-time` writer, and it is recorded as a "
            f"follow-up instead.\n"
        )
    return 0


def cmd_verify(args) -> int:
    path = Path(args.file)
    # The CALLER's sha, against the SAME pattern the artifact's own `head_sha` is held to (`ID_FORMATS`) —
    # they are compared to each other, so a form either one may take and the other may not is a comparison
    # waiting to be meaningless. Only the VERDICT differs: a malformed value here is the OPERATOR's mistake
    # (exit 2), not the artifact's.
    if not SHA_RE.match(args.head_sha):
        # MUTATE:caller-sha:pass
        raise OperatorError(
            f"--head-sha {args.head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to verify. "
            f"Every comparison below would be against a value that cannot be a commit, so the verdict "
            f"would be about the wrong question. No verdict beats a wrong one"
        )
    # The ruling's DOMAIN, bounded on both sides and BEFORE `decide` ever sees the value: `check_ruled` is
    # the floor (a cardinality starts at 0), the over-count rule below is the ceiling (you cannot rule on an
    # amendment that was never raised). Neither is a fact about the artifacts — both are the CALLER's
    # mistake, hence `OperatorError` and exit 2, not a verdict about the pass.
    check_ruled(args.amendments_ruled)
    raised = count_amendments(path)
    if args.amendments_ruled > raised:
        # MUTATE:caller-ruled:pass
        raise OperatorError(
            f"--amendments-ruled {args.amendments_ruled} but this pass raised only {raised} amendment(s) — "
            f"a ruling can only ever answer an amendment that EXISTS, and an over-count would silently "
            f"clear the next one the reviewer raises"
        )
    verdict, reason = evaluate(path, args.head_sha, args.amendments_ruled, args.verdict)
    print(f"{verdict}: {reason}")
    # `ok` is the ONLY exit-0 verdict — and it still is NOT `SATISFIED`.
    return 0 if verdict == OK else 1


# --- the status view: an ADVISORY, READ-ONLY glance across a run ---------------------------------
#
# **`status` DECIDES NOTHING** (`CLAUDE.md`, "Dogfood the branch's behavior — but NEVER let it gate
# itself"). It renders one aligned row per in-flight review pass and is invoked on demand or from the
# ScheduleWakeup heartbeat. It never calls `write_line`, never mutates a pass's artifacts, and never
# touches the ledger except `ledger.load()`. The authoritative "does this pass count?" answer stays
# `verify`/`evaluate`, which `status` can SURFACE verbatim (`--verify`) but never overrides.
#
# It reuses THIS FILE's own readers and predicates — `parse_name`, `parse_lines`, `plan_path`,
# `findings_path`, `gating`, `evaluate`, and the type/status constants — so there is no second parser for
# these artifacts (`review-pass.py`'s docstring: "one implementation, never two"). The one thing it does
# NOT reuse for the default tally is the strict verdict layer (`load_plan`/`walk_progress`/`decide`): those
# RAISE `Defect` on the first anomaly because they are the gate, and a live monitor must render a partial,
# mid-write, or imperfect pass rather than crash on it. So the default tally is TOLERANT (`read_lenient`),
# and `evaluate()`'s strict verdict is an opt-in column.

# The deadlines the skill already defines (references/stage-2-review-gate.md → "Launch check" and
# "Meaningful progress"). `status` reads them; it does not own them.
LAUNCH_DEADLINE_S = 5 * 60      # launch evidence must be present by ~5 min from dispatch
PROGRESS_DEADLINE_S = 15 * 60   # meaningful progress (a planned-unit done / accepted amendment) by ~15 min

# The health states (§4 of the design). The three ATTENTION states are upper-cased so they stand out in a
# column of lower-case `working`/`launching`.
#
# TWO of them are TERMINAL — the reviewer does not exist anymore — and the mtime-based liveness states
# (`launching`/`working`/`STALLED`/`NO-LAUNCH!`) apply to NEITHER: only a genuinely-CURRENT pass can be
# live. `done` is a finished pass (its report carries a `VERDICT:` line). `gone` is a pass whose reviewer
# left with NO verdict — it was SUPERSEDED (a later pass number exists for its PR) or RELAUNCHED (a later
# launch attempt exists). Without this split a completed or abandoned pass from hours ago rendered as
# `STALLED`/`AMEND(n)`, so the table listed "reviewers" that were long gone (`health_of`).
H_LAUNCHING, H_WORKING, H_DONE = "launching", "working", "done"
H_STALLED, H_NO_LAUNCH = "STALLED", "NO-LAUNCH!"
H_GONE = "gone"                 # TERMINAL, no verdict: superseded or relaunched — the reviewer is gone

# The report scrape's three outcomes (advisory: the authoritative verdict for gating is the ledger's).
V_SAT, V_NOT_SAT, V_NONE = "SAT", "NOT-SAT", "-"

NOW_ENV = "REVIEW_PASS_NOW"     # the deterministic "now" seam — a fixture sets it so elapsed/health are fixed


def read_lenient(path: Path) -> "list[dict] | None":
    """Records from a possibly-mid-write JSONL file. NEVER raises. status-only; NOT on the accept path.

    A JSONL file's complete records are exactly the text UP TO AND INCLUDING its last newline; anything
    after is a partial append in flight. So this truncates at the last `\\n` and feeds only that prefix to
    the reused `parse_lines` — the same reasoning `write_line` already uses about trailing newlines, not a
    second lenient JSON parser. `verify`/`evaluate` do NOT call this; they keep strict `read_lines`, so a
    torn or corrupt file is still `unusable` at the gate.

    Returns `[]` for an absent or newline-free file, the parsed records for a readable one, and `None` for
    a real (non-torn) corruption — which `status` renders as `unreadable` on that one row, never a crash.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cut = text.rfind("\n")
    prefix = text[: cut + 1] if cut >= 0 else ""   # drop a torn trailing append
    try:
        return parse_lines(prefix, path.name)      # reuse the schema owner's reader
    except Defect:
        return None                                # a real corruption — render as "unreadable"


def status_now(args) -> datetime:
    """The render clock, as a naive UTC datetime. `--now`/`REVIEW_PASS_NOW` is the determinism seam so a
    fixture can fix elapsed/health without the wall clock; absent both, it is the real now."""
    raw = getattr(args, "now", None) or os.environ.get(NOW_ENV)
    if raw:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(timezone.utc).replace(tzinfo=None)


def active_attempts(rundir: Path) -> "list[Path]":
    """Every pass's progress file, grouped by (pr, pass), keeping only the highest launch_attempt — the
    ACTIVE attempt. A file whose name `parse_name` refuses is skipped (it is not a progress artifact)."""
    best: dict[tuple[str, str], tuple[int, Path]] = {}
    for path in sorted(rundir.glob("review-*-*" + PROGRESS_SUFFIX)):
        try:
            pr, npass, attempt = parse_name(path)
        except Defect:
            continue
        key = (pr, npass)
        k = int(attempt)
        if key not in best or k > best[key][0]:
            best[key] = (k, path)
    return [path for _, path in best.values()]  # drops the attempt key


def all_attempts(rundir: Path) -> "list[tuple[Path, bool]]":
    """Every progress file, each flagged active (highest attempt for its (pr, pass)) or superseded."""
    active = set(active_attempts(rundir))
    out: list[tuple[Path, bool]] = []
    for path in sorted(rundir.glob("review-*-*" + PROGRESS_SUFFIX)):
        try:
            parse_name(path)
        except Defect:
            continue
        out.append((path, path in active))
    return out


def latest_pass_per_pr(rundir: Path) -> "dict[str, int]":
    """The highest pass NUMBER seen for each PR across the run. A pass whose number is below its PR's max
    was SUPERSEDED — the PR moved on to a later review round — so its reviewer is gone. Computed over EVERY
    progress file (not just the shown ones), the same glob+`parse_name` the attempt readers use, so there is
    no second parser: a name `parse_name` refuses is not a progress artifact and is skipped."""
    latest: dict[str, int] = {}
    for path in sorted(rundir.glob("review-*-*" + PROGRESS_SUFFIX)):
        try:
            pr, npass, _ = parse_name(path)  # drops the attempt
        except Defect:
            continue
        n = int(npass)
        if n > latest.get(pr, 0):
            latest[pr] = n
    return latest


def report_path(progress: Path) -> Path:
    """The reviewer's report (`review-<pr>-<n>.txt`) — per PASS, beside the progress file. `status` scrapes
    its `VERDICT:` tail for a convenience read; `verify` never opens it and neither does the gate."""
    pr, npass, _ = parse_name(progress)  # drops the attempt
    return progress.parent / f"review-{pr}-{npass}.txt"


def scrape_verdict(progress: Path) -> str:
    """The report's last `VERDICT:` line, mapped to SAT / NOT-SAT / - . Advisory only."""
    path = report_path(progress)
    if not path.exists():
        return V_NONE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return V_NONE
    verdict = V_NONE
    for line in text.splitlines():
        if "VERDICT:" in line.upper():
            rest = line.upper().split("VERDICT:", 1)[1]
            # NOT SATISFIED contains SATISFIED, so test the negative spelling first.
            if "NOT" in rest and "SATISF" in rest:
                verdict = V_NOT_SAT
            elif "SATISF" in rest:
                verdict = V_SAT
    return verdict


def plan_total(progress: Path) -> str:
    """`<n>` planned units, or `?` when the plan is absent or unreadable. Counts `unit` records only, so a
    hand-authored `{"type":"plan",...}` header line (if one is present) is ignored and the count is right
    either way — which is exactly why `status` does NOT reuse the strict `load_plan` for the tally."""
    plan = plan_path(progress)
    if not plan.exists():
        return "?"
    recs = read_lenient(plan)
    if recs is None:
        return "?"
    return str(sum(1 for r in recs if r.get("type") == UNIT))


def progress_tally(events: "list[dict]") -> "tuple[int, str]":
    """(done count, in-progress unit id or `-`) from a tolerant replay of the progress events.

    `done` = distinct units with a `done` event; `now` = the last unit that has a `started` and no `done`.
    Neither raises: a monitor renders what the file says so far."""
    done: list[str] = []
    started: list[str] = []
    for rec in events:
        if rec.get("type") != PROGRESS:
            continue
        unit, status = rec.get("unit"), rec.get("status")
        if not isinstance(unit, str):
            continue
        if status == DONE and unit not in done:
            done.append(unit)
        elif status == STARTED and unit not in started:
            started.append(unit)
    now = "-"
    for unit in reversed(started):
        if unit not in done:
            now = unit
            break
    return len(done), now


def finding_counts(progress: Path) -> str:
    """`<gating>/<non-gating>` via the ONE `gating()` predicate. A record missing the keys `gating()` reads
    is SKIPPED, not crashed on (advisory divergence from `verify`, which would reject it)."""
    recs = read_lenient(findings_path(progress))
    if not recs:
        return "0/0"
    g = ng = 0
    for rec in recs:
        if not isinstance(rec, dict) or "purpose" not in rec or "writer" not in rec:
            continue
        try:
            (g := g + 1) if gating(rec) else (ng := ng + 1)  # noqa: F841 - walrus updates the counters
        except Exception:  # noqa: BLE001 - a malformed finding is skipped by an advisory view, never fatal
            continue
    return f"{g}/{ng}"


def fmt_elapsed(seconds: float) -> str:
    """Age since dispatch: whole minutes under an hour (`6m`), else one decimal of hours (`1.2h`)."""
    if seconds < 0:
        seconds = 0
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes + 0.5)}m"
    return f"{minutes / 60:.1f}h"


def pass_identity_of(events: "list[dict]") -> "dict | None":
    for rec in events:
        if rec.get("type") == IDENTITY:
            return rec
    return None


def health_of(events: "list[dict]", verdict: str, elapsed_s: "float | None",
              mtime_age_s: float, terminal: bool = False) -> str:
    """The liveness read (§4). First match wins; the ATTENTION states sort above the calm ones.

    **THE TWO TERMINAL STATES COME FIRST, AND LIVENESS APPLIES TO NEITHER.** `done` is the report's VERDICT
    line — the pass FINISHED. `gone` is a pass whose reviewer left with NO verdict because it was SUPERSEDED
    or RELAUNCHED (`terminal`) — the reviewer is gone, and a table that showed it `STALLED`/`AMEND(n)` would
    claim a reviewer is stuck RIGHT NOW when none exists. So `gone` is decided BEFORE amendments and before
    the mtime split: a terminal pass is never live, whatever its progress file's age or amendments say.

    Only a genuinely-CURRENT pass (not terminal, no verdict) reaches the liveness reads: `AMEND(n)` for any
    raised `plan_amendment_request` (an amendment IS launch evidence and the more actionable fact, so it
    outranks liveness); then the launch-evidence split (`NO-LAUNCH!`/`launching`); then the progress-file
    mtime split (`STALLED`/`working`). `STALLED`/`NO-LAUNCH!` therefore flag ONLY a genuinely-current pass."""
    if verdict != V_NONE:
        return H_DONE
    if terminal:
        return H_GONE
    amendments = sum(1 for r in events if r.get("type") == AMENDMENT)
    if amendments:
        return f"AMEND({amendments})"
    # Launch evidence: ANY reviewer-written line after the identity — a progress event or an amendment
    # (amendments are already handled above, so here it reduces to a progress event).
    has_evidence = any(r.get("type") in (PROGRESS, AMENDMENT) for r in events)
    if not has_evidence:
        if elapsed_s is not None and elapsed_s > LAUNCH_DEADLINE_S:
            return H_NO_LAUNCH
        return H_LAUNCHING
    if mtime_age_s > PROGRESS_DEADLINE_S:
        return H_STALLED
    return H_WORKING


def verify_column(progress: Path, events: "list[dict]", verdict: str) -> str:
    """`evaluate()`'s authoritative verdict for this attempt — the opt-in `--verify` read. It uses the
    pass's OWN recorded `head_sha` as the comparison target (a stateless render knows no other head), and
    feeds the scraped report verdict so a complete, sound pass reads `ok` rather than `unusable`."""
    ident = pass_identity_of(events)
    head = ident.get("head_sha") if isinstance(ident, dict) else None
    if not isinstance(head, str):
        head = "0" * 40   # no usable identity → evaluate() will say `unusable`, which is the honest answer
    told = {V_SAT: SATISFIED, V_NOT_SAT: NOT_SATISFIED}.get(verdict)
    try:
        return evaluate(progress, head, 0, told)[0]
    except Exception:  # noqa: BLE001 - the opt-in column never crashes the table
        return "unreadable"


def required_reviews(tier: str) -> int:
    """The tier's SATISFIED-verdict floor, as `references/stage-3-merge.md` defines it (1 for TRIVIAL, else
    2). **Advisory restatement for display only** — the merge precondition itself lives in the gate, and
    `status` decides nothing; it just annotates a pass with its PR's tally."""
    return 1 if tier == "TRIVIAL" else 2


def load_ledger_module() -> types.ModuleType:
    """The sibling ledger accessor, loaded by a `__file__`-relative path (never the cwd). `status` calls
    only its `load`/`find_row` READERS; it never writes the ledger."""
    path = Path(__file__).resolve().parent / "ledger.py"
    spec = importlib.util.spec_from_file_location("ledger", path)
    if spec is None or spec.loader is None:
        raise Defect(f"cannot load the ledger accessor at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def status_row(progress: Path, now: datetime, want_verify: bool,
               ledger_rows: "list[dict] | None", superseded: bool = False) -> "list[str]":
    """One pass's cells, rendered in isolation so ONE malformed pass cannot crash the whole table.

    `superseded` is TRUE when a later pass number or a later launch attempt exists for this (pr, pass) — the
    reviewer is gone. It reaches `health_of` as `terminal`, so a superseded pass with no verdict renders
    `gone`, never a live-looking `STALLED`/`AMEND(n)`. A superseded pass that DID finish still reads `done`
    (the verdict wins in `health_of`)."""
    pr, npass, attempt = parse_name(progress)
    label = f"{pr}-{npass}" + (f".a{attempt}" if attempt != "1" else "")
    events = read_lenient(progress)
    if events is None:
        # The progress file itself is unreadable (a real corruption, not a torn tail). But a corrupt
        # PROGRESS file does not make the pass any less TERMINAL: its report may still carry a verdict (the
        # pass FINISHED) and a later pass or launch attempt may still have superseded it (the reviewer is
        # GONE) — both facts live in OTHER files. So scrape the verdict and honour `superseded` BEFORE
        # giving up, reusing `health_of` so `done`-beats-`gone` has ONE owner. Only a pass that is neither
        # finished nor superseded stays `unreadable`; without this a finished/dead pass rendered a
        # live-looking `unreadable` that the default view then showed instead of hiding.
        verdict = scrape_verdict(progress)
        terminal = verdict != V_NONE or superseded
        health = health_of([], verdict, None, 0.0, superseded) if terminal else "unreadable"
        cells = [label, "?", "-", "-", "-", health, verdict]
        if want_verify:
            cells.append("unreadable")
        if ledger_rows is not None:
            cells.append(_ledger_cell(pr, ledger_rows))
        return cells

    total = plan_total(progress)
    done, now_unit = progress_tally(events)
    verdict = scrape_verdict(progress)
    ident = pass_identity_of(events)
    elapsed_s: "float | None" = None
    if isinstance(ident, dict) and isinstance(ident.get("dispatched_at"), str):
        try:
            elapsed_s = (now - datetime.strptime(ident["dispatched_at"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
        except ValueError:
            elapsed_s = None
    try:
        mtime = datetime.fromtimestamp(progress.stat().st_mtime, timezone.utc).replace(tzinfo=None)
        mtime_age_s = (now - mtime).total_seconds()
    except OSError:
        mtime_age_s = 0.0
    health = health_of(events, verdict, elapsed_s, mtime_age_s, superseded)
    elapsed = fmt_elapsed(elapsed_s) if elapsed_s is not None else "-"

    cells = [label, f"{done}/{total}", now_unit, finding_counts(progress), elapsed, health, verdict]
    if want_verify:
        cells.append(verify_column(progress, events, verdict))
    if ledger_rows is not None:
        cells.append(_ledger_cell(pr, ledger_rows))
    return cells


def _ledger_cell(pr: str, ledger_rows: "list[dict]") -> str:
    row = next((r for r in ledger_rows if r.get("pr") == pr), None)
    if row is None:
        return "-"
    return f"{row.get('reviews_ok', '0')}/{required_reviews(row.get('tier', '-'))}"


def render_status(header: str, columns: "list[str]", rows: "list[list[str]]") -> str:
    """The run header line, a blank line, then aligned columns — `ledger table`'s idiom, two-space gutters.

    Each data line is rstripped so a trailing `-` cell has no padding after it; the rule line never ends in
    whitespace by construction."""
    widths = [len(col) for col in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = [header, ""]
    out.append("  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip())
    out.append("  ".join("-" * w for w in widths))
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out) + "\n"


DIM, RESET = "\x1b[2m", "\x1b[0m"


def cmd_status(args) -> int:
    """Render live review-pass progress across a run. READ-ONLY: it opens files for reading only."""
    rundir = Path(args.run)
    now = status_now(args)

    ledger_rows: "list[dict] | None" = None
    reviewer: "str | None" = None
    if args.ledger:
        L = load_ledger_module()
        _header, ledger_rows = L.load(Path(args.ledger))
        reviewer = _header.get("reviewer")

    columns = ["pass", "units", "now", "find", "elapsed", "health", "verdict"]
    if args.verify:
        columns.append("counts(--verify)")
    if ledger_rows is not None:
        columns.append("tally(--ledger)")

    # SUPERSESSION is a fact about the WHOLE run: a pass is superseded when its PR has a later pass number.
    # It is computed over every progress file, once, before any view filtering (a `--pr` filter or the
    # default hide must not change whether a pass counts as superseded).
    latest_pass = latest_pass_per_pr(rundir)
    health_col = columns.index("health")

    # BOTH views enumerate EVERY attempt; the default view then hides terminal passes (and counts them in
    # the footer), while `--history` shows them. Building the default set from `active_attempts` alone
    # dropped every superseded launch attempt (a relaunched `.a1`) BEFORE the hidden counter ran, so the
    # table hid it but the footer under-counted. `all_attempts` flags each attempt active/superseded, and the
    # ONE terminal classification in the render loop below both hides and counts it — no separate count path.
    pairs = all_attempts(rundir)
    if args.pr is not None:
        pairs = [(p, a) for (p, a) in pairs if parse_name(p)[0] == str(args.pr)]
    pairs.sort(key=lambda pa: tuple(int(x) for x in parse_name(pa[0])[:2]) + (int(parse_name(pa[0])[2]),))

    # A pass is TERMINAL when its rendered health is `done` (finished) or `gone` (superseded/relaunched, no
    # verdict). The DEFAULT view hides terminal passes so the table shows only what is genuinely in flight;
    # `--history` shows everything (terminal passes and superseded attempts, dimmed on a TTY). Nothing is
    # ever silently dropped: the count of hidden passes is printed, and `--history` reveals them all.
    dim_terminal = args.history and sys.stdout.isatty()
    rows: list[list[str]] = []
    terminal_flags: list[bool] = []
    hidden = 0
    for progress, is_active in pairs:
        pr, npass, _ = parse_name(progress)  # drops the attempt
        superseded = (not is_active) or (int(npass) < latest_pass.get(pr, 0))
        try:
            row = status_row(progress, now, args.verify, ledger_rows, superseded)
        except Exception as exc:  # noqa: BLE001 - one bad pass must never crash the whole table
            row = [progress.name, "?", "-", "-", "-", f"error:{type(exc).__name__}", "-"]
            while len(row) < len(columns):
                row.append("-")
        is_terminal = row[health_col] in (H_DONE, H_GONE)
        if is_terminal and not args.history:
            hidden += 1
            continue
        rows.append(row)
        terminal_flags.append(is_terminal)

    scope = ("all passes (history)" if args.history else "in-flight passes only")
    segs = [f"run {rundir.name or str(rundir)}"]
    if reviewer:
        segs.append(f"reviewer={reviewer}")
    segs.append(scope)
    segs.append(f"as-of {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    header = "# " + "   ".join(segs)

    hidden_note = (f"# {hidden} terminal pass(es) hidden (done/gone) — --history to show them"
                   if hidden else None)

    if not rows:
        sys.stdout.write(header + "\n\n")
        sys.stdout.write(hidden_note + "\n" if hidden_note else f"# (no review passes in {rundir})\n")
        return 0

    text = render_status(header, columns, rows)
    if dim_terminal and any(terminal_flags):
        lines = text.split("\n")
        body_start = 4  # header, blank, column header, rule, then rows
        for i, terminal in enumerate(terminal_flags):
            if terminal:
                lines[body_start + i] = f"{DIM}{lines[body_start + i]}{RESET}"
        text = "\n".join(lines)
    if hidden_note:
        text += hidden_note + "\n"
    sys.stdout.write(text)
    return 0


# --- self-test: the fixtures ARE the contract, and they are a SIBLING ----------------------------
#
# **THE SUITE LIVES IN `review-pass-test.py`, NOT IN THIS FILE.** A fixture table that ships inside the tool
# it tests is a fixture table the tool it tests can quietly disarm — and this repo has watched a reviewer do
# exactly that: `CASES=[]`, spliced in memory, and `self_test()` still exited 0 reporting "all 0 fixtures
# hold". Moving the suite out does not make that impossible (nothing does, against someone editing source),
# but it stops the tool and its own contract from being one file that a single edit can make agree with
# itself.
#
# `self-test` loads the sibling by a `__file__`-relative path — never the cwd, which is the reviewer's
# worktree while these scripts live wherever the plugin is installed — and **FAILS LOUDLY IF IT IS NOT
# THERE.** A check that cannot find the thing it checks must FAIL, never pass. Reporting success because
# zero fixtures ran is a green derived from zero evidence, and it is the founding defect of everything on
# the other side of this call.
#
# It hands the sibling THIS MODULE (`sys.modules[__name__]`), so the fixtures drive the code the command
# actually loaded — and the mutation harness, which lives over there too, builds each mutant from THIS
# file's source text.

TEST_PY = Path(__file__).resolve().parent / "review-pass-test.py"


def load_test_module() -> types.ModuleType:
    if not TEST_PY.exists():
        fail(
            f"the fixture suite is NOT AT {TEST_PY} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass. Reporting success here would be a green "
            f"derived from zero evidence, which is precisely the bug those fixtures exist to prevent",
            1,
        )
    spec = importlib.util.spec_from_file_location("review_pass_test", TEST_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        fail(f"cannot load the fixture suite at {TEST_PY} — refusing to report a self-test that never ran", 1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def self_test() -> int:
    """Run the sibling suite against THIS module."""
    tests = load_test_module()
    with tempfile.TemporaryDirectory() as tmpdir:
        return tests.run(sys.modules[__name__], Path(tmpdir))


# --- CLI -----------------------------------------------------------------------------------------

def fail(msg: str, code: int) -> NoReturn:
    print(f"review-pass: {msg}", file=sys.stderr)
    raise SystemExit(code)


def add_emit_args(p: argparse.ArgumentParser) -> None:
    """The emit door's flags — `--file --unit --status --evidence` — defined in exactly ONE place.

    This is a PUBLIC CONTRACT: it is what every review prompt already dispatched against an INSTALLED copy
    of this skill runs. `build_parser`'s `emit` subcommand and `emit-progress.py`'s own top-level parser
    both call this, so the reviewer's door and the owner's door cannot come to accept — or ADVERTISE —
    different flags. `emit-progress.py` used to have no parser of its own at all, and rendered the OWNER's
    help instead: `--help` printed a command (`emit-progress.py emit …`) that the wrapper itself refuses.
    """
    p.add_argument("--file", required=True, help="the launch attempt's progress.jsonl")
    p.add_argument("--unit", required=True, help="a PLANNED unit's id — an unplanned one is refused")
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--evidence", help="concrete citation; REQUIRED for --status done")


def add_finding_args(p: argparse.ArgumentParser) -> None:
    """The finding door's flags — defined in exactly ONE place, exactly as `add_emit_args` is.

    `emit-finding.py` (the reviewer's door) and `review-pass.py finding-add` (the owner's) both call this,
    so the two cannot come to accept — or ADVERTISE — different flags. The lesson is `emit-progress.py`'s:
    it had no parser of its own, rendered the OWNER's help, and printed a command it then refused.

    `--path`, not `--file`: `--file` is the ARTIFACT this line is appended to, at every door in this tool,
    and a second meaning for it here is how a reviewer comes to write a finding into the source file it is
    about.
    """
    p.add_argument("--file", required=True, help="the launch attempt's findings.jsonl")
    p.add_argument("--path", required=True, help="the FILE the defect is in (the citation's first half)")
    p.add_argument("--line", required=True, help="the LINE it is on — a decimal from 1 up")
    p.add_argument("--writer", required=True, choices=WRITERS,
                   help="WHO CAN ACTUALLY PUT THE BAD INPUT THERE. `hand-edit` = only by hand-editing a "
                        "local git-ignored file the driver owns; `dev-time` = only by editing the source of "
                        "the code under review (if your repro starts 'I mutated … in memory', it is this). "
                        "A guard being incomplete is not, by itself, a defect: name the writer who gets "
                        "through it")
    p.add_argument("--purpose", required=True,
                   help="the line of the PR's `## Purpose` block this finding DEFENDS, quoted VERBATIM — or "
                        "`-` if fixing it serves no stated purpose. Not a formality: it is the question "
                        "'does this PR do its job?', and a finding that cannot answer it is not a reason to "
                        "block the PR")
    p.add_argument("--repro", required=True,
                   help="the command, input or edit that makes it fail — what you actually did")
    p.add_argument("--fix", required=True, help="the concrete fix")


def build_parser() -> "tuple[argparse.ArgumentParser, list[str]]":
    """The CLI, and the list of subcommands it actually has — DERIVED from the parser, never typed out.

    TWO checks stand on this, and both FAIL on a subcommand they do not know how to drive: the ROUND TRIP
    (does what this command writes read back?) and the DOOR check (does the command its own `--help`
    advertises actually RUN?). So a subcommand added here is covered on the day it is added — there is no
    second list of commands to forget to update, which is the only way a new door could ever ship
    advertising a shape it refuses.
    """
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    add_emit_args(sub.add_parser("emit", help="append one unit-progress event (what emit-progress.py calls)"))

    i = sub.add_parser("identity", help="write a pass's pass_identity line (pr/pass/attempt come from --file)")
    i.add_argument("--file", required=True, help="the launch attempt's progress.jsonl — it must not exist yet")
    i.add_argument("--head-sha", required=True, help="`git rev-parse HEAD` — 40 hex, NEVER an abbreviation")
    i.add_argument("--dispatched-at", required=True, help="UTC ISO-8601, e.g. 2026-07-06T00:00:00Z")

    a = sub.add_parser("plan-add", help="append one validated unit to a pass's plan")
    a.add_argument("--file", required=True, help="the pass's plan.jsonl")
    a.add_argument("--id", required=True)
    a.add_argument("--kind", required=True, help="file | cross-cutting | docs | …")
    a.add_argument("--target", required=True, help="the CONCRETE thing reviewed")
    # REQUIRED, and the `required=` is the whole of what this line had to say and did not. `--check` was
    # OPTIONAL to argparse — so `--help` bracketed it, `[--check CHECK]` — while `check_unit` refuses a unit
    # whose `checks` is empty. The command this tool's own help advertised (`plan-add --file … --id … --kind
    # … --target …`) therefore exited 1: `checks is [] — a unit with no concrete checks is not a unit`. The
    # help door and the WRITE door disagreed about what the command IS, which is the same defect as two
    # doors disagreeing about what an ID is. It is `required=True` now, so the shape the help advertises is
    # the shape the write path takes, and a missing `--check` is refused by ARGPARSE — at the door, naming
    # the flag — instead of by a rule about the unit that was built from it.
    a.add_argument("--check", action="append", default=[], required=True,
                   help="a concrete check; REQUIRED, and repeatable — a unit with no checks is not a unit")

    add_finding_args(sub.add_parser(
        "finding-add", help="record ONE finding, anchored to the PR's intent (what emit-finding.py calls)"))

    v = sub.add_parser("verify", help="DOES THIS PASS COUNT? (it never reads the reviewer's report)")
    v.add_argument("--file", required=True, help="the ACTIVE launch attempt's progress.jsonl")
    v.add_argument("--head-sha", required=True, help="the PR's LIVE head — the pass must have run on it")
    v.add_argument("--amendments-ruled", type=int, default=0, metavar="N",
                   help="how many of this pass's plan amendments you have already ruled on — a count, so "
                        "N >= 0, and never more than the pass actually raised (default 0)")
    # REQUIRED — and it was OPTIONAL, which is the same defect `--check` had one door over, in the shape
    # that costs the most. The coherence rule is the ONLY mechanical check on the reviewer's own verdict,
    # and while this flag could be left out, a complete pass verified WITHOUT it came back `ok`: the guard
    # was OFF for any driver that simply forgot the flag, and the gate merged whatever the report claimed.
    # A gate MUST NOT depend on an agent remembering to pass something. `verify` is a door you come to with
    # the report in hand; a pass still in flight is not verified, it is WATCHED (its progress file is the
    # liveness evidence — `stage-2-review-gate.md`, "Launch check"), and `decide` refuses an absent verdict
    # only once the pass is COMPLETE, so an in-process caller still gets `incomplete` rather than a scolding.
    v.add_argument("--verdict", choices=VERDICTS, required=True,
                   help="REQUIRED: the VERDICT line you read in the reviewer's report. It buys ONE "
                        "machine-checked rule, an IF AND ONLY IF: the pass is UNUSABLE if `not-satisfied` "
                        "recorded NO gating finding (a verdict that blocks a PR must name what blocks it), "
                        "and equally UNUSABLE if `satisfied` recorded ONE (a finding that gates cannot be "
                        "waved through in the verdict). A pass verified without it is UNUSABLE too — a rule "
                        "a caller can switch off by omitting a flag is not a gate")

    # status is ADVISORY and READ-ONLY: it renders live progress and DECIDES NOTHING. Its flags are all
    # about the VIEW, never about a verdict — there is no `--head-sha`, no `--verdict`, and it writes no file.
    st = sub.add_parser("status", help="ADVISORY read-only glance at every in-flight review pass in a run")
    st.add_argument("--run", required=True, help="the run directory to glob for review passes")
    st.add_argument("--pr", help="show only this PR's passes")
    st.add_argument("--verify", action="store_true",
                    help="add evaluate()'s authoritative verdict column (ok/incomplete/amended/unusable)")
    st.add_argument("--ledger", help="a state.jsonl — adds a reviews_ok/required(tier) tally column")
    st.add_argument("--history", dest="history", action="store_true",
                    help="show TERMINAL passes too — done (finished) and gone (superseded/relaunched) — plus "
                         "superseded launch attempts, dimmed on a TTY. The default hides them and prints a "
                         "count, so the table shows only what is genuinely in flight")
    st.add_argument("--now", help="UTC ISO-8601 render clock (or REVIEW_PASS_NOW) — a determinism seam")

    sub.add_parser("self-test", help="run every fixture, then DELETE each rule and prove a fixture notices")

    return p, sorted(str(name) for name in (sub.choices or {}))


def dispatch(args) -> int:
    """Run one PARSED command — and the ONE place a refusal becomes an exit code.

    `emit-progress.py` hands its own parser's `args` straight to this (with `cmd` fixed to `emit` by
    `set_defaults`, where no caller can type it and no help text can advertise it). So the wrapper reaches
    the same function through the same refusal-to-exit-code mapping: exit 1 = your inputs were rejected,
    exit 2 = the caller asked the wrong question. There is no second mapping to drift.
    """
    if args.cmd == "self-test":
        return self_test()
    try:
        return {"emit": cmd_emit, "identity": cmd_identity, "plan-add": cmd_plan_add,
                "finding-add": cmd_finding_add, "verify": cmd_verify, "status": cmd_status}[args.cmd](args)
    except Defect as exc:
        fail(str(exc), 1)
    except OperatorError as exc:
        fail(str(exc), 2)


def main(argv: "list[str] | None" = None) -> int:
    p, _ = build_parser()  # drops the commands map
    return dispatch(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
