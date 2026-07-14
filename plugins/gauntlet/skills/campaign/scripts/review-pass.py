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
  self-test   the fixtures, the proof that every rule is pinned by one, and every JSON example in the
              docs fed THROUGH the tool (a documented example the tool refuses is a trap, not a typo)

WHAT `verify` DOES NOT DO — AND THE LINE IS DELIBERATE. It never opens `review-<pr>-<n>.txt`, never
parses the reviewer's prose, and CANNOT SAY `SATISFIED`. Its whole answer is about the pass's MECHANICS:
is there an identity, does it name the commit the pass actually ran on, is every `done` for a unit that
was really planned, did every `done` FOLLOW a `started` for that same unit, does every `done` carry
evidence, were amendments raised. The VERDICT is the reviewer's JUDGMENT and stays theirs.

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

  ok          the artifacts are sound; the pass's verdict may now be read from its report
  incomplete  sound, but a planned unit has no `done` event — the pass did not cover its plan
  amended     sound, but the reviewer raised a plan amendment nobody has ruled on yet
  unusable    the artifacts are defective — this pass CANNOT count, whatever its report says

`amended` is a VERDICT and not a footnote beside `ok` on purpose. A disclosure printed next to a pass is a
trapdoor, not a disclosure: "this pass counts, but note that the reviewer says the plan is missing a
dimension" gets read as "counts". The orchestrator rules on the amendment (fold it into the plan and
restart the pass, or record why not — stage-2-review-gate.md), and passes `--amendments-ruled <n>` to say
so. Absent that, the guard fires: the DEFAULT is that nothing has been ruled on.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tempfile
import types
from collections import Counter
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
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

def decide(events: "list[dict]", units: "dict[str, dict]", ruled: int) -> "tuple[str, str]":
    """Given SOUND artifacts: does this pass COUNT? (Its report is still not read. That is the point.)

    The per-event rules — planned unit, `done` follows `started`, no SECOND `done` — are `check_progress`,
    replayed here by `walk_progress`. They are not restated: they are the SAME statements `emit` runs, so
    what this door refuses to read is exactly what that door refuses to write.

    **The `started` rule was PROSE and enforced by NOBODY, and it is the one the tool most needed.** A
    progress file with a valid identity and a `done` for EVERY planned unit — and NOT ONE `started` —
    verified `ok`: the tool that exists to prove a review HAPPENED accepted a review that demonstrably did
    not. Skip straight to "done" for every unit and the gate was satisfied on zero evidence of work. A
    `done` with no `started` is not progress, exactly as an empty plan is not a plan.
    """
    _announced, done = walk_progress(events, units)

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
    return OK, (
        f"all {len(units)} planned units are done with evidence, on {events[0]['head_sha']}, no unruled "
        f"amendments. This says the ARTIFACTS are sound — NOT that the pass is SATISFIED. Read the "
        f"VERDICT line in the report"
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
    pr, npass, _attempt = parse_name(progress)
    return progress.parent / PLAN_NAME.format(pr=pr, **{"pass": npass})


def evaluate(progress: Path, head_sha: str, ruled: int = 0) -> "tuple[str, str]":
    """The whole read side. Every exception a rule can raise lands here as a VERDICT — never as a crash."""
    try:
        plan = plan_path(progress)
        events, units = check_progress_file(text=read_text(progress, "progress file"), path=progress,
                                            plan=lambda: load_plan(plan), head_sha=head_sha)
        return decide(events, units, ruled)
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
    pr, npass, _attempt = parse_name(path)
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
    verdict, reason = evaluate(path, args.head_sha, args.amendments_ruled)
    print(f"{verdict}: {reason}")
    # `ok` is the ONLY exit-0 verdict — and it still is NOT `SATISFIED`.
    return 0 if verdict == OK else 1


# --- self-test: the fixtures ARE the contract ----------------------------------------------------
#
# Every rule above marks itself `# MUTATE:<id>:<weakening>` on the line ABOVE the statement that ENFORCES
# it. `self-test` then does four things, and the last two are the ones that matter:
#
#   1. runs every fixture and asserts its verdict AND the needle its reason must contain (the reason is
#      the only thing that says WHICH rule fired — a fixture that goes `unusable` for someone else's
#      reason pins NOTHING);
#   2. drives the ROUND TRIP — every write command the PARSER has, against every pre-existing file state —
#      and asserts the property no per-rule fixture can state: **either the command FAILS, or the file it
#      produced VERIFIES**;
#   3. asserts that EVERY enforcement point in a rule function sits under a marker. A rule ADDED without
#      one is never mutated, so nothing could ever report it unpinned. An unmarked rule is an untested one;
#   4. DELETES each rule in turn — splicing in its weakening — re-runs every fixture, every CLI case and
#      the whole round trip, and FAILS if no fixture notices.
#
# Step 4 exists because step 1 CANNOT see the failure that matters most: a rule NOTHING tests. Delete such
# a rule and the suite stays green while the tool has quietly stopped checking. A sibling PR in this repo
# proved the danger is not theoretical: a hand-written "N rules pinned" matrix was TRUE and INSUFFICIENT —
# 8 guards were not in the inventory at all, and 7 of those were pinned by nothing. THE COUNT IS A CLAIM.
# So the count is DERIVED, here, on every CI run, and "which rules are unpinned?" is a question the SUITE
# answers rather than one a reviewer has to discover.
#
# Step 2 exists for the failure step 1 cannot see EITHER, and it is a different one: a defect that is not
# in any rule but in the RELATION between the doors. Every rule can hold on both sides while the tool still
# writes a file it will not read — and a fixture that asserts one command's exit code cannot fail on that.
# The fixture at the head of the CLI list used to ASSERT the bad write succeeded (`emit` on an EMPTY file,
# expected exit 0); the test encoded the bug, and a green suite reported it as correct behavior.

SHA = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
OTHER_SHA = "b" * 40
TS = "2026-07-06T00:00:00Z"


class _Drop:
    """The sentinel that REMOVES a key from a fixture record, so a fixture can OMIT a required field.

    It is not `None`: `null` is a legal JSON value, so a fixture must stay free to write
    `"evidence": null` and watch the tool refuse it. "Absent" and "present and null" are different bytes
    and different defects — collapse them onto one sentinel and one of the two becomes untestable.
    """


DROP = _Drop()

# A fixture record's values are typed `object`, and that IS the type: these builders exist to write what
# the schema FORBIDS — an `evidence` that is a list, a `proposed_unit` that is a string, a key that is not
# there at all. Declaring them `str` would be a promise the fixtures are written to break, and a type
# checker believing it would reject the very cases the read side must catch.
Value = object


def _rec(fields: "dict[str, Value]", over: "dict[str, Value]") -> str:
    rec = {**fields, **over}
    return json.dumps({k: v for k, v in rec.items() if v is not DROP})


def ident(**over: Value) -> str:
    return _rec({"type": IDENTITY, "pr": "41", "pass": "1", "head_sha": SHA,
                 "launch_attempt": "1", "dispatched_at": TS}, over)


def unit(uid: str = "u01", **over: Value) -> str:
    return _rec({"type": UNIT, "id": uid, "kind": "file", "target": "scripts/review-pass.py",
                 "checks": ["the read side refuses what the write side refuses"]}, over)


def started(uid: str = "u01", **over: Value) -> str:
    return _rec({"type": PROGRESS, "unit": uid, "status": STARTED}, over)


def done(uid: str = "u01", evidence: Value = "review-pass.py:42 `check_event`", **over: Value) -> str:
    return _rec({"type": PROGRESS, "unit": uid, "status": DONE, "evidence": evidence}, over)


def amendment(**over: Value) -> str:
    return _rec({"type": AMENDMENT, "ts": TS, "reason": "no unit covers the mutation harness",
                 "proposed_unit": json.loads(unit("u99"))}, over)

PLAN = [unit("u01"), unit("u02", target="stage-2-review-gate.md", checks=["the docs match the tool"])]
WORKED = [ident(), started("u01"), done("u01"), started("u02"), done("u02", evidence="stage-2:161")]

# A progress file written as BYTES, not lines — a sound pass with one byte in it that is not UTF-8. Read
# leniently, `\xff` becomes U+FFFD and the file quietly says something it does not say.
RAW_BYTES = b'{"type":"progress","unit":"u01","status":"done","evidence":"\xff"}\n'

# name -> (plan lines, progress lines, expected verdict, needle its reason must contain, why it exists).
# EVERY fixture must FAIL WHEN ITS RULE IS DELETED — that is what step 3 above checks, one rule at a time.
CASES = {
    "worked": (PLAN, WORKED, OK, "ARTIFACTS are sound", "the shape of a pass that counts — and the tool STILL does not say SATISFIED"),

    # THE HEADLINES.
    "unplanned-done": (PLAN, [ident(), done("u99")], UNUSABLE, "NOT IN THE PLAN",
                       "a `done` for a unit nobody planned. The rule was PROSE and enforced by NOBODY: the write tool accepted it and the read side never looked"),
    "unplanned-started": (PLAN, [ident(), started("u99")], UNUSABLE, "NOT IN THE PLAN",
                          "…and a `started` for one, which is what a reviewer inventing a unit does FIRST"),
    "done-without-started": (PLAN, [ident(), done("u01"), done("u02", evidence="stage-2:161")], UNUSABLE,
                             "no earlier 'started'",
                             "THE FORGED PASS: a valid identity and a `done` for EVERY planned unit, with NOT ONE `started`. It verified `ok` — the tool that exists to prove a review HAPPENED accepted one that demonstrably did not, on zero evidence of any work"),
    "done-before-started": (PLAN, [ident(), done("u01"), started("u01"), started("u02"), done("u02")], UNUSABLE,
                            "no earlier 'started'",
                            "…and the ORDER of it: every `started` a real pass would have, but one lands BELOW its `done`. The file is append-only, so its order IS the sequence; 'u01 finished, then u01 began' is not a review"),
    "done-no-evidence": (PLAN, [ident(), done("u01", evidence=DROP)], UNUSABLE, "carries EXACTLY",
                         "a `done` with no evidence key at all — a claim with nothing behind it"),
    "done-blank-evidence": (PLAN, [ident(), done("u01", evidence="   ")], UNUSABLE, "CONCRETE evidence",
                            "…and a `done` whose evidence is whitespace, which the key check cannot see"),
    "short-sha": (PLAN, [ident(head_sha=SHA[:7]), done("u01"), done("u02")], UNUSABLE, "A prefix is not a commit",
                  "a truncated sha in a hand-written pass_identity — this HAPPENED, in production"),
    "handwritten-bogus": (PLAN, [ident(), '{"type":"progress","unit_id":"u01","status":"done","evidence":"x"}'],
                          UNUSABLE, "carries EXACTLY",
                          "the reviewer bypassed the emit tool and hand-wrote a line — with `unit_id`, the exact renaming stage-2 forbids. The READ side catches it: it never assumes the write tool was used"),

    # The progress file's line-level shape.
    "blank-line": (PLAN, [ident(), "", done("u01")], UNUSABLE, "is blank", "JSONL has no blank lines"),
    "not-json": (PLAN, [ident(), "u01 done"], UNUSABLE, "is not JSON", "a corrupt line is a corrupt artifact, never one to skip"),
    "not-object": (PLAN, [ident(), '"u01 done"'], UNUSABLE, "not a JSON object", "a bare string is not an event"),
    "duplicate-key": (PLAN, [ident(), '{"type":"progress","unit":"u99","unit":"u01","status":"started"}'],
                      UNUSABLE, "duplicate member name", "the decoder DISCARDS the first value, so the unplanned `u99` in the bytes reaches no rule at all"),
    "too-deep": (PLAN, [ident(), '{"type":"progress","unit":' + "[" * 20000 + "]" * 20000 + ',"status":"started"}'],
                 UNUSABLE, "nested too deeply", "the decoder RAISED where a verdict was owed, and a crash is not a verdict"),
    "unknown-event": (PLAN, [ident(), '{"type":"unit_done","unit":"u01"}'], UNUSABLE, "UNRECOGNISED event type",
                      "the exact renaming stage-2 forbids (`unit_done`) — skipping it makes the pass read as incomplete-but-clean"),
    "bad-status": (PLAN, [ident(), started("u01", status="finished")], UNUSABLE, "the only unit-progress statuses",
                   "a status the tool never emits — it can only have been hand-written"),
    "extra-key": (PLAN, [ident(), done("u01", ts=TS)], UNUSABLE, "present and NOT COUNTED",
                  "a `done` carrying a `ts` nothing reads (stage-2 forbids extra keys by name)"),
    "non-string": (PLAN, [ident(), done("u01", evidence=["file.py:1"])], UNUSABLE, "not a string",
                   "evidence as a LIST — it used to be handed straight to `.strip()`"),
    "started-with-evidence": (PLAN, [ident(), started("u01", evidence="x")], UNUSABLE, "carries EXACTLY",
                              "a `started` carrying evidence: the mirror of a `done` without it"),

    # pass_identity — the binding to a PR, a pass, an ATTEMPT and a COMMIT.
    "no-identity": (PLAN, [done("u01"), done("u02")], UNUSABLE, "NO `pass_identity`",
                    "two done units and nothing saying WHAT they reviewed"),
    "identity-not-first": (PLAN, [started("u01"), ident(), done("u01")], UNUSABLE, "not the FIRST line",
                           "an event written BEFORE the reviewer was dispatched"),
    "identity-twice": (PLAN, [ident(), ident(head_sha=OTHER_SHA), done("u01")], UNUSABLE, "2 `pass_identity`",
                       "a second identity naming another commit — read by nothing, present in the bytes"),
    "wrong-head": (PLAN, [ident(head_sha=OTHER_SHA), done("u01"), done("u02")], UNUSABLE, "no longer there",
                   "the pass ran on a commit that is not the tip: its verdict describes content that has moved"),
    "identity-bad-number": (PLAN, [ident(launch_attempt="one"), done("u01")], UNUSABLE,
                            "a decimal number from 1 up",
                            "an attempt number that cannot be COMPARED to the one in the filename"),
    "identity-bad-ts": (PLAN, [ident(dispatched_at="just now"), done("u01")], UNUSABLE, "LAUNCH DEADLINE's clock",
                        "a dispatched_at nobody can parse — the ~5-min deadline measured from it NEVER FIRES"),
    "identity-impossible-ts": (PLAN, [ident(dispatched_at="2026-99-99T99:99:99Z"), started("u01"), done("u01"),
                                      started("u02"), done("u02")], UNUSABLE, "not a real UTC time",
                               "A DATE THAT CANNOT EXIST, in the right SHAPE. The regex matched it and the whole pass verified `ok` — month 99, hour 99. The shape check could not fire on the one input that defeats the deadline it protects"),
    "identity-missing-key": (PLAN, [ident(dispatched_at=DROP), done("u01")], UNUSABLE, "carries EXACTLY",
                             "a pass_identity with no dispatch clock at all"),

    # The plan.
    "plan-empty": ([], WORKED, UNUSABLE, "VACUOUSLY TRUE",
                   "an EMPTY plan: 'every planned unit is done' is true of it, so a pass that reviewed NOTHING would verify ok"),
    "plan-duplicate-id": ([unit("u01"), unit("u01", target="other.py")], [ident(), done("u01")], UNUSABLE,
                          "duplicate unit id", "two units with one id — a `done` for it says nothing about WHICH was checked"),
    "plan-unknown-type": ([unit("u01"), unit("u02", type="note")], WORKED, UNUSABLE, "only 'unit'",
                          "a plan line of a type nothing reads — perfectly unit-SHAPED, and still not a unit"),
    "plan-unit-extra-key": ([unit("u01", owner="me")], [ident(), done("u01")], UNUSABLE, "unexpected key",
                            "a unit carrying a field nothing reads"),
    "plan-unit-no-checks": ([unit("u01", checks=[])], [ident(), done("u01")], UNUSABLE, "not a unit",
                            "a unit with an EMPTY checks list — a heading, not a unit: nothing can be shown to have been done against it"),
    "plan-unit-blank-target": ([unit("u01", target="  ")], [ident(), done("u01")], UNUSABLE, "names nothing",
                               "a unit with no target"),
    "plan-line-not-object": (['["u01"]'], [ident(), done("u01")], UNUSABLE, "not a JSON object",
                             "a plan LINE that is a list — the strict reader refuses it at the plan door exactly as at the progress door"),

    # THE IDENTIFIERS. One legal form each, at every door — and nothing is ever repaired into one.
    "plan-unit-padded-id": ([unit(" u01 ")], [ident()], UNUSABLE, "NOT AN ID",
                            "THE FINDING, AT THE PLAN DOOR: a unit id with surrounding whitespace. `plan-add --id ' u01 '` exited 0 and `emit --unit ' u01 '` then said NOT IN THE PLAN — while printing `Planned: [' u01 ']` — because the emit door STRIPPED the value and the plan door did not. The plan held a unit no door could match and the pass could never complete. It is not repaired now, at either door: ` u01 ` is not `u01` with a space, it is not an id"),
    "progress-padded-unit": (PLAN, [ident(), started(" u01 ")], UNUSABLE, "The emit door does NOT strip it",
                             "…and at the PROGRESS door, which is where the strip used to be. A hand-written event naming ` u01 ` is told its unit id is malformed — not told the unit is 'not in the plan', which is the wrong lesson and was the old message"),
    "amendment-padded-unit-id": (PLAN, [ident(), amendment(proposed_unit=json.loads(unit(" u99 ")))], UNUSABLE,
                                 "NOT AN ID",
                                 "…and at the THIRD intake: an amendment's `proposed_unit` is what the orchestrator FOLDS INTO THE PLAN, so an id the plan door would refuse must be refused here too — or the plan acquires, one wake later, exactly the unmatchable unit this rule exists to keep out of it"),
    "amendment-unit-not-object": (PLAN, [ident(), amendment(proposed_unit="u99")], UNUSABLE, "not a JSON object",
                                  "the amendment's proposed_unit is a STRING. This is the one place a non-dict unit can reach `check_unit` — the plan's own lines are objects by the time it runs — and it used to be handed straight to `set()`"),
    "plan-missing": (None, WORKED, UNUSABLE, "no plan at",
                     "NO PLAN FILE AT ALL. A guard whose input can be ABSENT never fires — so absence is refused, never skipped"),
    "not-utf8": (PLAN, RAW_BYTES, UNUSABLE, "UTF-8",
                 "bytes we cannot decode are not evidence — and decoding them LENIENTLY rewrites what the file says"),

    # Amendments, completeness, and the verdicts that are not refusals.
    "amendment-unruled": (PLAN, [ident(), started("u01"), done("u01"), amendment(), started("u02"), done("u02")],
                          AMENDED, "not yet ruled on",
                          "the reviewer says the plan is missing a dimension. It is a VERDICT, never a footnote printed beside `ok`"),
    "amendment-bad-unit": (PLAN, [ident(), amendment(proposed_unit={"id": "u99"})], UNUSABLE, "carries EXACTLY",
                           "a hand-written amendment (they are EXEMPT from the emit-only rule, so this is the one event a reviewer really does write) whose proposed unit is malformed"),
    "amendment-impossible-ts": (PLAN, [ident(), amendment(ts="2026-99-99T99:99:99Z")], UNUSABLE,
                                "not a real UTC ISO-8601 time",
                                "the amendment's `ts` had NO check at all beyond 'is a string' — the identity's clock was guarded and this one, the same kind of value, was not. The orchestrator rules on amendments; a `ts` that is not a moment cannot be ordered against one"),
    "amendment-blank-reason": (PLAN, [ident(), amendment(reason="   ")], UNUSABLE, "an amendment is a CLAIM",
                               "an amendment with a blank reason: it FORCES the `amended` verdict — a pass held back — while saying nothing the orchestrator can rule on. The evidence-free `done` of the amendment world"),
    "incomplete": (PLAN, [ident(), started("u01"), done("u01"), started("u02")], INCOMPLETE, "has not covered its plan",
                   "u02 was started and never finished — `started` is liveness, NEVER completion"),
    "duplicate-done": (PLAN, [ident(), started("u01"), done("u01"), done("u01", evidence="somewhere else"),
                              started("u02"), done("u02")],
                       UNUSABLE, "SECOND", "two accounts of one unit, and nothing says which was read"),
    "identity-only": (PLAN, [ident()], INCOMPLETE, "0/2",
                      "the file the orchestrator leaves at dispatch: the reviewer has produced NOTHING, and this is not an error — it is a pass that has not covered its plan yet"),
}

# The NAME cases. Same sound pass every time — only the FILENAME differs, so the name is the only thing
# under test. It is the one thing that says which PASS and which ATTEMPT these bytes are, and the docs
# already name substituting attempt-1 paths into a relaunch a "silent self-defeat".
NAME_CASES = [
    ("review-41-1.progress.jsonl", OK, "ARTIFACTS are sound", "attempt 1's name — the real artifact's shape"),
    ("review-41-1.a2.progress.jsonl", UNUSABLE, "silent self-defeat",
     "THE ONE THAT MATTERS: a RELAUNCH's file holding attempt 1's identity. The live pass would be writing into the dead attempt's file, and the launch check would read it as never launched"),
    ("review-42-1.progress.jsonl", UNUSABLE, "silent self-defeat", "another PR's pass, filed under this one"),
    ("review-41-2.progress.jsonl", UNUSABLE, "silent self-defeat", "pass 2's file holding pass 1's identity"),
    ("progress.jsonl", UNUSABLE, "not a progress artifact's name", "a name that binds these bytes to nothing at all"),
    ("review-41-1.progress.json", UNUSABLE, "not a progress artifact's name", "one character off is not the artifact"),
]

# The WRITE door. Same rules, other side. `(argv, the progress file it runs against, expected exit, needle
# in stdout+stderr, why)`. `emit-progress.py`'s CLI is unchanged, so the `emit` argv here are exactly what
# a live reviewer prompt already runs — the contract those prompts were written against is the contract
# still under test. The seed for `emit` is the file the orchestrator leaves AT DISPATCH — a `pass_identity`
# and nothing else — because that is the only file a reviewer's `emit` is ever run against. `identity`'s
# seed is the EMPTY file, which is what it is for.
EMPTY: "list[str]" = []
DISPATCHED = [ident()]  # what the orchestrator leaves behind: `pass_identity`, written before the launch
BEGUN = [ident(), started("u01")]  # the file a reviewer has in hand once it has ANNOUNCED u01
FINISHED = [ident(), started("u01"), done("u01")]  # …and once it has already FINISHED u01
CLI_CASES = [
    (["emit", "--unit", "u01", "--status", "started"], DISPATCHED, 0, '"status":"started"', "the call every reviewer prompt makes"),
    (["emit", "--unit", "u01", "--status", "done", "--evidence", "f.py:1"], BEGUN, 0, '"evidence":"f.py:1"',
     "…and its done form, on the file that HAS the matching `started` — the only file the done form was ever meant to be run against"),
    (["emit", "--unit", "u01", "--status", "started"], EMPTY, 1, "NO `pass_identity`",
     "HEADLINE, WRITE DOOR: THE FILE THIS TOOL WROTE AND WOULD NOT READ. `emit` on an EMPTY progress file exited 0 — it never looked for the identity — and `verify` then called that same file `unusable: NO pass_identity`. The reviewer was told its work landed, and the pass could not count. `emit` now runs the READ side's identity check on the file it appends into, so what it accepts is what `verify` can read"),
    (["emit", "--unit", "u01", "--status", "done", "--evidence", "somewhere else"], FINISHED, 1, "SECOND",
     "HEADLINE, WRITE DOOR: a SECOND `done` for a unit already finished. `verify` refused it on READ and this door WROTE it (exit 0) — the rule held at one door and not the other, so the reviewer was handed a success and the pass was thrown away later for a defect the tool had just helped it commit. Both doors now run the SAME predicate"),
    (["emit", "--unit", "u02", "--status", "done", "--evidence", "f.py:1"], BEGUN, 1, "no earlier 'started'",
     "HEADLINE, WRITE DOOR: a `done` for a unit that was never begun. The write door refuses it at the moment the reviewer makes the mistake, instead of the pass being thrown away by `verify` fifteen minutes later"),
    (["emit", "--unit", "u99", "--status", "done", "--evidence", "f.py:1"], DISPATCHED, 1, "NOT IN THE PLAN",
     "HEADLINE, WRITE DOOR: the tool accepted a self-granted unit. It no longer does — and it says UNPLANNED, not 'no started': an unplanned unit's real defect is that nobody planned it"),
    (["emit", "--unit", "u99", "--status", "started"], DISPATCHED, 1, "NOT IN THE PLAN", "…and refuses to START one"),
    (["emit", "--unit", "u01", "--status", "done"], DISPATCHED, 1, "carries EXACTLY", "a done with no evidence — the SAME key rule a hand-written line meets, not a second CLI-shaped copy of it"),
    (["emit", "--unit", "u01", "--status", "done", "--evidence", "  "], BEGUN, 1, "CONCRETE evidence", "…and blank evidence, on a file where the `started` is not the problem"),
    (["emit", "--unit", "u01", "--status", "started", "--evidence", "x"], DISPATCHED, 1, "carries EXACTLY", "a started carrying evidence: the mirror of a done without it, and the same rule"),
    (["emit", "--unit", "  ", "--status", "started"], DISPATCHED, 1, "The emit door does NOT strip it",
     "a blank unit id. It is refused for what it IS — not an id — and not for what it is not: this door used to STRIP `--unit` and then report the trimmed value 'not in the plan', which named the wrong defect even when it fired"),
    (["emit", "--unit", " u01 ", "--status", "started"], DISPATCHED, 1, "The emit door does NOT strip it",
     "HEADLINE, WRITE DOOR: THE FINDING. `plan-add --id ' u01 '` used to exit 0 while this door silently STRIPPED the padding — so the plan held a unit whose progress could never be recorded, and the review could never complete. `emit` said NOT IN THE PLAN and printed `Planned: [' u01 ']` in the same breath. Neither door repairs an identifier now, and the plan door refuses that id in the first place"),
    (["emit", "--unit", "u02", "--status", "started"], [ident(), '{"type":"progress","unit_id":"u01","status":"done","evidence":"x"}'], 1, "carries EXACTLY",
     "the file it is APPENDING TO is evidence too: a hand-written line already in it makes the pass unusable, so `emit` refuses to add a good line to a file `verify` will throw away"),
    (["identity", "--head-sha", SHA, "--dispatched-at", TS], EMPTY, 0, '"launch_attempt":"1"',
     "the line that was a `printf` — pr/pass/attempt now come from the FILENAME, so they cannot disagree with it"),
    (["identity", "--head-sha", SHA[:7], "--dispatched-at", TS], EMPTY, 1, "escaped into this repo's real state",
     "HEADLINE, WRITE DOOR: the truncated sha that got written into a real pass_identity"),
    (["identity", "--head-sha", SHA.upper(), "--dispatched-at", TS], EMPTY, 1, "LOWERCASE",
     "an UPPERCASE sha: no producer of ours emits one, so it did not come from `git rev-parse`"),
    (["identity", "--head-sha", SHA, "--dispatched-at", "just now"], EMPTY, 1, "LAUNCH DEADLINE's clock",
     "a dispatch clock the launch deadline cannot be measured from — the write door runs the READ side's shape rules, so it cannot write one `verify` would reject"),
    (["identity", "--head-sha", SHA, "--dispatched-at", "2026-99-99T99:99:99Z"], EMPTY, 1, "not a real UTC time",
     "…and the one the SHAPE rule cannot see: an impossible date in the right shape. Both doors parse it now, so neither can produce a `dispatched_at` no clock ever passes"),
    (["identity", "--head-sha", OTHER_SHA, "--dispatched-at", TS], [ident()], 1, "NOT EMPTY",
     "a SECOND identity into a live pass's file. `pass_identity` is the FIRST line, written before dispatch — a relaunch gets its OWN file, and appending here is how one pass ends up describing two commits"),
    (["identity", "--head-sha", SHA, "--dispatched-at", TS], [""], 1, "NOT EMPTY",
     "HEADLINE, WRITE DOOR: a WHITESPACE-ONLY file. This door decided 'empty' with `.strip()`, so a file holding one blank line counted as fresh — it wrote the identity in below the blank line, exited 0, and `verify` then refused the artifact FOR THAT BLANK LINE. Two doors, two definitions of empty, and the file in the crack was one this tool wrote and would not read. EMPTY now means NO BYTES, at both"),
    (["verify", "--head-sha", SHA[:7]], EMPTY, 2, "No verdict beats a wrong one",
     "an OPERATOR error is not a snapshot verdict: exit 2, never a verdict computed from a comparison that could not have succeeded"),
    (["verify", "--head-sha", SHA, "--amendments-ruled", "1"], EMPTY, 2, "raised only 0",
     "a ruling for an amendment that does not exist would silently clear the NEXT one raised"),
    (["verify", "--head-sha", SHA, "--amendments-ruled", "-1"], WORKED, 2, "smallest legal value is 0",
     "HEADLINE: A NEGATIVE RULING WEDGES A PASS THAT WAS EARNED. The seed is a COMPLETE, sound pass — it "
     "verifies `ok` with no flag at all — and `--amendments-ruled -1` used to turn it into `amended`: "
     "`decide` subtracts the ruling, so `0 - (-1) = 1` amendment 'not yet ruled on' that the reviewer "
     "never raised and no ruling can ever clear. It failed SAFE (this tool can only SUBTRACT a pass), and "
     "a pass withheld forever is still withheld. The over-count rule bounded the value ABOVE and NOTHING "
     "bounded it below: a cardinality's domain starts at 0, and now the floor is checked before `decide`"),
]

# `plan-add` gets its own family: its `--check` is repeatable, so its argv do not fit the shape above, and
# the plan file's NAME is under test too — `(the plan file's name, argv, expected exit, needle, why)`.
PLAN_FILE = "review-41-1.plan.jsonl"
PLAN_CLI_CASES = [
    (PLAN_FILE, ["--id", "u03", "--kind", "cross-cutting", "--target", "both doors", "--check", "a", "--check", "b"],
     0, '"checks":["a","b"]', "the plan stops being a shell heredoc"),
    (PLAN_FILE, ["--id", "u01", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "duplicate unit id",
     "a duplicate id — a `done` for it would say nothing about which unit was checked. Refused by the SAME statement `load_plan` refuses it with: the plan as it WOULD be goes through the reader's own function"),
    (PLAN_FILE, ["--id", "  ", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID", "a blank id"),
    (PLAN_FILE, ["--id", " u01 ", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID",
     "HEADLINE, PLAN DOOR: THE FINDING. This exited 0, and the id it wrote was one `emit` could never match — because `emit` stripped its `--unit` and this door did not. The plan is the INTAKE: refuse the id here and no later door can be handed a unit it cannot name"),
    (PLAN_FILE, ["--id", "U01", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID",
     "…and an id that is merely a different SPELLING of a legal one. There is no such thing: an identifier has one form, so `U01` is not `u01` in capitals — it is not an id, and it is refused rather than folded"),
    (PLAN_FILE, ["--id", "u03", "--kind", "file", "--target", "x.py"], 1, "not a unit", "a unit with NO checks"),
    ("plan.jsonl", ["--id", "u03", "--kind", "file", "--target", "x.py", "--check", "a"], 1,
     "not a plan artifact's name",
     "the plan's name was enforced at the READ door BY CONSTRUCTION (`verify` derives it and takes no plan path) and at the write door NOT AT ALL: this wrote a perfectly valid plan to a name nothing will ever open, and the pass would then be refused for a MISSING plan with its units on disk one filename away"),
]


class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


# --- the ROUND TRIP: anything the tool can WRITE, it must be able to READ BACK --------------------
#
# The cases above pin RULES, one by one, and that is exactly why they could not see this: **both findings
# it missed were about a file the tool WROTE and then REFUSED TO READ.** `emit --status started` on an
# empty progress file exited 0 and `verify` called that file `unusable: NO pass_identity`; `identity` on a
# whitespace-only file exited 0 and `verify` refused the artifact for the blank line. Every individual rule
# was correct at both doors. What was broken was the RELATION between them — and a per-rule fixture cannot
# fail on a relation nobody stated. (The fixture at the head of the CLI list even ASSERTED the bad write
# succeeded: `emit` on an EMPTY file, expected exit 0. The test encoded the bug.)
#
# So the relation is stated here, ONCE, as a property, and every write path is driven against a range of
# pre-existing file states:
#
#   **EITHER THE COMMAND FAILS, OR THE FILE IT PRODUCED VERIFIES.** Never "succeeds, then does not verify".
#
# "Verifies" means the READ side can READ it — not that the pass is `ok`. A pass whose plan is not yet
# covered reads back `incomplete`, and that is a correct, readable artifact: `ok`/`incomplete`/`amended`
# all say the bytes are sound. `unusable` — and a CRASH, which is worse — are what a write must never
# produce. (`verify` can still refuse the pass LATER for a reason that is not about the file: the head SHA
# moves when someone pushes. That is `check_head`, the one read-door rule no write door can run, and it is
# the honest gap in this property — see `check_head`.)
#
# THE COMMAND LIST IS DERIVED FROM THE PARSER (`build_parser`), never hand-listed: a subcommand that no
# entry below drives FAILS the self-test. A new write path is therefore covered on the day it is added —
# by failing until someone covers it, which is the only kind of coverage that cannot rot.

PROGRESS_FILE = "review-41-1.progress.jsonl"

# The pre-existing states of the file a write lands in. They are applied to WHICHEVER artifact the command
# writes, and deliberately not "realistic" per command: a plan file holding a progress event, or a progress
# file holding a plan unit, is exactly the kind of state nobody predicted — and the property must hold on
# every one of them, or it is not a property.
FILE_STATES: "dict[str, bytes | None]" = {
    "absent": None,
    "empty": b"",
    "whitespace-only": b"   \n",
    "blank-line": b"\n",
    "identified": (ident() + "\n").encode(),
    "begun": (ident() + "\n" + started("u01") + "\n").encode(),
    "planned": (unit("u01") + "\n").encode(),
    # THE CONCATENATION. The last line has NO trailing newline, so the next append lands ON it and fuses
    # two records into one line that is not JSON. Every record-level check passes — the record was never
    # the problem — and only the bytes `before + line` can show it. This is the SAME class as the two
    # findings, and nothing but the round trip would have caught it.
    "no-trailing-newline": (ident()).encode(),
    "plan-no-trailing-newline": (unit("u01")).encode(),
    "corrupt": b"not json at all\n",
    "not-utf8": b"\xff\n",
}

# How to drive each WRITE command, and which artifact it writes. The command LIST is derived; only the
# flags are here, because no parser can know what a valid `--check` looks like.
WRITE_COMMANDS: "dict[str, tuple[str, list[str]]]" = {
    "emit": (PROGRESS_FILE, ["--unit", "u01", "--status", "started"]),
    "identity": (PROGRESS_FILE, ["--head-sha", SHA, "--dispatched-at", TS]),
    "plan-add": (PLAN_FILE, ["--id", "u09", "--kind", "file", "--target", "x.py", "--check", "a"]),
}

# The subcommands that write NOTHING. They are listed so that `build_parser`'s subcommands can be
# ACCOUNTED FOR exhaustively — a new one falls into neither set and fails the suite.
READ_ONLY_COMMANDS = frozenset({"verify", "self-test"})

HOLDS, VIOLATED = "holds", "VIOLATED"


# --- the CROSS-DOOR property: an id the PLAN door takes is an id the EMIT door can NAME -----------
#
# The round trip above asks "can the tool read back what it wrote?" — ONE artifact, one command. This asks
# the question one artifact over, and it is the one the tool got wrong: **the plan door and the emit door
# must agree about what a unit id IS.** They did not. `plan-add --id ' u01 '` exited 0; `emit --unit
# ' u01 '` then failed with `NOT IN THE PLAN` and printed `Planned: [' u01 ']`, because the emit door
# STRIPPED its `--unit` and the plan door had accepted the padding verbatim. The plan held a unit whose
# progress could never be recorded — a review that could never complete, wedged by two doors disagreeing
# about one value.
#
# No per-rule fixture could fail on that: every rule was right on its own side. The defect was in the
# RELATION, exactly as with the two write-then-refuse-to-read findings — so, exactly as there, the relation
# is stated once, as a property, and driven:
#
#   **THE PLAN DOOR REFUSES THE ID, OR THE EMIT DOOR CAN MATCH IT.** Never "planned, and unnameable".
#
# It is driven with the reviewer's own input (` u01 `) and with every other way a shell, a YAML block or an
# agent hands over a value with something extra on it. Note what makes it hold now: not that both doors
# strip, but that NEITHER does — an id has one legal form (`ID_FORMATS`), so there is nothing to disagree
# about. A future door that "helpfully" normalizes its input fails this the day it is added.

CROSS_DOOR_IDS = {
    "plain": "u01",
    "padded": " u01 ",              # THE FINDING, verbatim: the reviewer's exact input
    "trailing-space": "u01 ",
    "leading-tab": "\tu01",
    "inner-space": "u 01",
    "blank": "   ",
    "uppercase": "U01",
    "newline": "u01\n",
}


def cross_door(mod: types.ModuleType, tmp: Path) -> "dict[str, tuple[str, str]]":
    """`plan-add --id X`, then `emit --unit X` — the SAME string, through both doors, for each X.

    `holds` = the plan door REFUSED the id (so no plan ever held it), or it accepted the id and the emit
    door could then name the unit. `VIOLATED` = the plan door took an id the emit door cannot match: a
    planned unit with no way to record progress against it, which is a pass that can never complete.
    """
    got: dict[str, tuple[str, str]] = {}
    for name, uid in CROSS_DOOR_IDS.items():
        d = tmp / f"xd-{name}"
        d.mkdir(parents=True, exist_ok=True)
        plan, progress = d / PLAN_FILE, d / PROGRESS_FILE
        key = f"[cross-door] the id {uid!r}"
        try:
            code, text = run_cli(mod, ["plan-add", "--file", str(plan), "--id", uid,
                                       "--kind", "file", "--target", "x.py", "--check", "a"])
            if code != 0:
                got[key] = (HOLDS, f"the PLAN door REFUSED it (exit {code}), so no plan can hold it: "
                                   f"{text.strip()}")
                continue
            run_cli(mod, ["identity", "--file", str(progress), "--head-sha", SHA, "--dispatched-at", TS])
            code, text = run_cli(mod, ["emit", "--file", str(progress), "--unit", uid, "--status", "started"])
            got[key] = (
                (HOLDS if code == 0 else VIOLATED),
                f"the plan door PLANNED it, and the emit door exited {code}: {text.strip()}",
            )
        except Exception as exc:  # noqa: BLE001 - a crash at either door is a violation, not an error
            got[key] = (f"crash:{type(exc).__name__}", str(exc))
    return got


# --- EVERY BOUNDED VALUE, probed JUST INSIDE and JUST OUTSIDE its declared domain -----------------
#
# A bounded value is one this tool ACCEPTS or REJECTS against a domain it has declared: every identifier
# (`ID_FORMATS`), every number the FILENAME carries, and every numeric flag. The declaration is the tool's
# CLAIM about what the value may be; this is the check on that claim, and it is the reason a strict format
# beats normalizing — a rule that says "trim it" cannot be tabulated (there is no set of strings it
# refuses), while a rule that says "it looks like THIS" has an exact boundary, and **an exact boundary is a
# thing a test can stand on BOTH SIDES OF.**
#
# **THE TOOL'S TWO WORST BUGS WERE BOTH A BOUNDARY NO FIXTURE STOOD ON**, and neither was a missing rule:
#
#   * the attempt suffix was `[2-9][0-9]*` — it took `a2`…`a9` and `a20` and REFUSED `a10` through `a19`,
#     while the tool's own error message said `k >= 2`. A domain with a HOLE in the middle of it.
#   * `--amendments-ruled` took `-1`, which `decide` then SUBTRACTED: a complete, sound pass with no
#     amendments came back `amended`, "0 amendment(s), 1 not yet ruled on", and nothing could clear it.
#
# Every rule was present; every rule was right in the middle of its range. So the fence is not another rule
# — it is COVERAGE, made mechanical. The values COVERED here are reconciled against `DOMAINS`, and a domain
# with no cases, **or with cases on only ONE side of its boundary**, FAILS the suite. "We tested carefully"
# cannot fail. "Every declared domain has a case just inside and just outside it, and here they are" can.

Probe = Callable[[object], None]


def probe_id(name: str) -> Probe:
    """An identifier, through `check_id` — the ONE validator every real door calls."""
    return lambda value: check_id(name, value, "[domain]")


# The three numbers a progress file's NAME carries. They are probed through `parse_name` — the REAL door,
# not a copy of its regex — by building the name that wears the value. `pr` and `pass` are the same `COUNT`
# the identifiers use (the name is compared to the `pass_identity`, so they must be the same kind of value);
# the attempt is `ATTEMPT`, from 2 up, and it is the one that was wrong.
NAME_TEMPLATES = {"pr": "review-{v}-1.progress.jsonl",
                  "pass": "review-41-{v}.progress.jsonl",
                  "attempt": "review-41-1.a{v}.progress.jsonl"}


def probe_name(field: str) -> Probe:
    # A `def`, not a lambda: `parse_name` RETURNS the three numbers it parsed, and a probe answers only
    # "was it refused?". The result is dropped here rather than leaked into a `Probe` that claims `None`.
    def probe(value: object) -> None:
        parse_name(Path(NAME_TEMPLATES[field].format(v=value)))

    return probe


def probe_ruled(value: object) -> None:
    """`--amendments-ruled`, through the same `check_ruled` that `verify` runs — never a second copy of it.

    The parser's `type=int` is what guarantees an int ever reaches that function, so this narrows the same
    way: a non-integer is refused by argparse at the CLI door, one layer before the domain is consulted.
    """
    if not isinstance(value, int):
        raise OperatorError(f"[domain] --amendments-ruled {value!r} is not an integer — the parser's "
                            f"`type=int` refuses it before the domain is ever reached")
    check_ruled(value)


# The tool's every bounded value: name -> (the REAL door it enters by, its domain in words).
DOMAINS: "dict[str, tuple[Probe, str]]" = {
    **{name: (probe_id(name), spec) for name, (_re, spec, _why) in ID_FORMATS.items()},
    "filename pr": (probe_name("pr"), "a decimal number from 1 up, as the progress file's NAME carries it"),
    "filename pass": (probe_name("pass"), "a decimal number from 1 up, as the NAME carries it"),
    "filename attempt": (probe_name("attempt"),
                         "the `a<k>` suffix: a decimal integer from 2 UP, no leading zeros (attempt 1 "
                         "wears no suffix at all)"),
    "--amendments-ruled": (probe_ruled,
                           "a CARDINALITY: an integer from 0 up, and never more than the pass raised"),
}

BOUNDARY_CASES: "list[tuple[str, object, bool]]" = [
    ("id", "u01", True), ("id", "u99", True), ("id", "unit01", True),
    ("id", " u01 ", False), ("id", "u01 ", False), ("id", "\tu01", False), ("id", "u 01", False),
    ("id", "u01\n", False), ("id", "U01", False), ("id", "u", False), ("id", "01", False),
    ("id", "u01a", False), ("id", "u-01", False), ("id", "", False), ("id", "   ", False),
    ("id", 1, False), ("id", None, False),
    ("unit", "u01", True), ("unit", " u01 ", False), ("unit", "U01", False), ("unit", "", False),
    ("pr", "41", True), ("pr", "1", True),
    ("pr", "0", False), ("pr", "041", False), ("pr", " 41", False), ("pr", "41 ", False),
    ("pr", "+41", False), ("pr", "-41", False), ("pr", "4_1", False), ("pr", "", False), ("pr", 41, False),
    ("pass", "1", True), ("pass", "0", False), ("pass", "one", False), ("pass", "01", False),
    ("launch_attempt", "1", True), ("launch_attempt", "2", True), ("launch_attempt", "10", True),
    ("launch_attempt", "0", False), ("launch_attempt", " 2", False), ("launch_attempt", "two", False),
    ("head_sha", SHA, True),
    ("head_sha", SHA[:7], False),          # THE TRUNCATED SHA — it reached real state, and it is CLEAN:
    ("head_sha", SHA.upper(), False),      # no trimming could ever have caught it. Only a FORMAT can.
    ("head_sha", SHA + "0", False), ("head_sha", SHA[:39], False),
    ("head_sha", " " + SHA, False), ("head_sha", SHA + "\n", False),
    ("head_sha", "", False), ("head_sha", None, False),

    # The FILENAME's numbers — the same domains, at the door that reads the NAME rather than the bytes.
    ("filename pr", "1", True), ("filename pr", "41", True), ("filename pr", "10", True),
    ("filename pr", "0", False), ("filename pr", "041", False), ("filename pr", "", False),
    ("filename pr", "-1", False), ("filename pr", "1 ", False),
    ("filename pass", "1", True), ("filename pass", "2", True), ("filename pass", "10", True),
    ("filename pass", "0", False), ("filename pass", "01", False), ("filename pass", "", False),

    # **THE ATTEMPT SUFFIX — THE BOUNDARY NOBODY STOOD ON.** `a2`…`a9` and `a20` were accepted and
    # `a10`…`a19` were REFUSED, so both edges of the hole are pinned here (9/10 and 19/20), along with the
    # bottom edge (1 is out, 2 is in — attempt 1 wears no suffix) and the leading zero.
    ("filename attempt", "2", True), ("filename attempt", "3", True), ("filename attempt", "9", True),
    ("filename attempt", "10", True), ("filename attempt", "11", True), ("filename attempt", "19", True),
    ("filename attempt", "20", True), ("filename attempt", "99", True), ("filename attempt", "100", True),
    ("filename attempt", "1", False), ("filename attempt", "0", False),
    ("filename attempt", "02", False), ("filename attempt", "010", False),
    ("filename attempt", "", False), ("filename attempt", "-2", False), ("filename attempt", "+2", False),
    ("filename attempt", "2 ", False), ("filename attempt", " 2", False), ("filename attempt", "2a", False),

    # **`--amendments-ruled` — A CARDINALITY, and 0 is INSIDE it.** `-1` is the wedge: subtracted from the
    # amendments the pass raised, it invents one that nobody can ever rule on.
    ("--amendments-ruled", 0, True), ("--amendments-ruled", 1, True), ("--amendments-ruled", 7, True),
    ("--amendments-ruled", -1, False), ("--amendments-ruled", -2, False),
]


def check_boundaries() -> int:
    """Every bounded value, JUST INSIDE and JUST OUTSIDE its domain — and every domain probed on BOTH sides.

    Returns the failures. The second loop is the mechanical part: it is what a bug like `a10` has to get
    past, and it cannot — a domain nobody fenced on both sides is reported as unfenced, by name.
    """
    failures = 0
    sides: dict[str, set[bool]] = {}
    for name, value, accepted in BOUNDARY_CASES:
        # Recorded BEFORE the lookup, so a case naming a value with no declared domain is REPORTED (below)
        # rather than skipped into silence — the `stray` check is how this table cannot quietly test a
        # domain the tool does not actually declare.
        sides.setdefault(name, set()).add(accepted)
        if name not in DOMAINS:
            continue
        probe, spec = DOMAINS[name]
        try:
            probe(value)
            got = True
        except (Defect, OperatorError):
            got = False
        if got == accepted:
            print(f"ok       [domain] `{name}` {'accepts' if accepted else 'REFUSES'} {value!r}")
        else:
            print(f"FAIL     [domain] `{name}` {'REFUSED' if accepted else 'ACCEPTED'} {value!r} — its "
                  f"domain is {spec}, and the BOUNDARY is where two doors come to disagree about what a "
                  f"value IS. `a10` was refused by a pattern whose own error message said `k >= 2`")
            failures += 1

    for name, (_probe, spec) in DOMAINS.items():
        probed = sides.get(name, set())
        if probed == {True, False}:
            continue
        gap = ("NO CASES AT ALL" if not probed else
               "no case INSIDE it" if True not in probed else "no case OUTSIDE it")
        print(f"FAIL     [domain] `{name}` ({spec}) has {gap} — a domain is fenced only when the suite "
              f"stands on BOTH sides of its boundary. An unprobed side is what `a10` and `-1` cost")
        failures += 1

    stray = sorted(set(sides) - set(DOMAINS))
    if stray:
        print(f"FAIL     [domain] cases for a value with no declared domain: {stray} — a domain is "
              f"DECLARED in `DOMAINS` or it is not a domain at all")
        failures += 1
    return failures


# --- the DOCS' examples, fed through the tool ----------------------------------------------------
#
# The doc is what a reviewer actually follows. **A documented example the tool REFUSES is not a typo — it
# is a trap that makes correct behavior impossible**: the `plan_amendment_request` example omitted
# `"type":"unit"` from its `proposed_unit`, the verifier REQUIRES that key, so a reviewer who copied the
# documented shape produced a pass the tool then called `unusable`, with nothing telling it why.
#
# Eyeballing them is what let that ship. So they are EXECUTED: every JSON example in the campaign skill's
# docs that claims one of THIS tool's types is parsed out and put through the very functions `verify` runs,
# and a doc example this tool would reject FAILS THE BUILD. (Routing is by `type`, so the ledger's own
# examples one file over are not this schema's business. The cost is that an example with a MISSPELLED
# type would be skipped rather than caught — which is why the type set found is asserted below: a scan
# that matched nothing, or lost a type, is a check that has quietly stopped checking.)

DOCS = Path(__file__).resolve().parent.parent  # the campaign skill: SKILL.md and references/
DOC_TYPES = {UNIT, PROGRESS, AMENDMENT, IDENTITY}


def doc_examples() -> "list[tuple[str, int, dict]]":
    """(file, line, record) for every JSON example in the docs that claims one of this tool's types."""
    found: list[tuple[str, int, dict]] = []
    for md in sorted(DOCS.rglob("*.md")):
        for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.strip()
            if not text.startswith("{") or not text.endswith("}"):
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                continue  # a JSON-SHAPED line that is not JSON is some other doc's prose, not an example
            if isinstance(rec, dict) and rec.get("type") in DOC_TYPES:
                found.append((str(md.relative_to(DOCS)), n, rec))
    return found


def check_docs() -> int:
    """Every documented example, through the tool. Returns the number that the tool would REFUSE."""
    examples = doc_examples()
    failures = 0
    for where, n, rec in examples:
        try:
            if rec["type"] == UNIT:
                check_unit(rec, f"{where}:{n}")
            else:
                check_event(rec, f"{where}:{n}")
                if rec["type"] == IDENTITY:
                    check_identity_shape(rec, f"{where}:{n}")
            print(f"ok       {where}:{n:<4} {rec['type']:22} the tool accepts its own documented example")
        except Defect as exc:
            print(f"FAIL     {where}:{n:<4} the tool REFUSES its own documented example: {exc}")
            failures += 1
    seen = {rec["type"] for _w, _n, rec in examples}
    if seen != DOC_TYPES:
        print(f"FAIL     the docs no longer show an example of every event type — missing "
              f"{sorted(DOC_TYPES - seen)}. A scan that matches nothing passes every time and checks "
              f"nothing; these examples ARE the contract, so their absence is the failure")
        failures += 1
    return failures


# --- the WRAPPER's HELP: what it SAYS must be what the tool TAKES --------------------------------
#
# `emit-progress.py --help` printed `usage: emit-progress.py emit [-h] --file …`, and running that exact
# command failed with `unrecognized arguments: emit` — the wrapper prepends `emit` itself, so its own help
# advertised a command shape it REFUSES. Two doors disagreeing about what the COMMAND is, which is the same
# defect as two doors disagreeing about what an ID is — and the help is the door a reviewer READS.
#
# An exhortation to "keep the help and the parser in sync" cannot fail. This can: the usage block is parsed
# for the invocation it advertises, and **that invocation is EXECUTED** — as a subprocess, against a real
# dispatched pass, exactly as a reviewer would run it. Advertise a command the tool refuses and the suite
# goes red. The FLAGS are checked the same way, against the emit door's own `add_emit_args`: the help may
# not name a flag the tool does not take, nor omit one it requires.

WRAPPER = Path(__file__).resolve().parent / "emit-progress.py"


def advertised(help_text: str) -> "tuple[list[str], set[str]]":
    """(the COMMAND WORDS `--help` advertises, the OPTIONS it advertises) — read out of its usage block.

    The usage block is the first `usage:` line plus argparse's indented continuation lines. The command
    words are everything before the first flag or bracket (`emit-progress.py`, and any subcommand it
    claims); the options are every `-x`/`--xyz` in it.
    """
    block: list[str] = []
    for line in help_text.splitlines():
        if line.startswith("usage:"):
            block.append(line)
        elif block and line.startswith(" ") and line.strip():
            block.append(line)
        elif block:
            break
    usage = " ".join(block).partition("usage:")[2]
    words: list[str] = []
    for word in usage.split():
        if word.startswith(("-", "[")):
            break
        words.append(word)
    return words, set(re.findall(r"--?[a-z][a-z-]*", usage))


def check_wrapper_help(tmp: Path) -> int:
    """Run `emit-progress.py --help`, take the command it advertises, and RUN THAT. Returns the failures."""
    failures = 0
    help_run = subprocess.run([sys.executable, str(WRAPPER), "--help"],  # noqa: S603 - our own sibling
                              capture_output=True, text=True, check=False)
    if help_run.returncode != 0:
        print(f"FAIL     [help] `{WRAPPER.name} --help` exited {help_run.returncode}: {help_run.stderr}")
        return 1
    words, options = advertised(help_run.stdout)

    probe = argparse.ArgumentParser()
    add_emit_args(probe)  # the door that ACCEPTS — its flags, not a list typed out here
    accepted = {opt for action in probe._actions for opt in action.option_strings  # noqa: SLF001
                if opt.startswith("--")} - {"--help"}
    long_flags = {opt for opt in options if opt.startswith("--")}
    if long_flags != accepted:
        print(f"FAIL     [help] it advertises {sorted(long_flags)} and the tool accepts {sorted(accepted)} "
              f"— a flag in one and not the other is a flag someone will type and be refused for")
        failures += 1
    else:
        print(f"ok       [help] the flags it advertises are the flags the emit door takes: {sorted(accepted)}")

    # …and now the whole point: EXECUTE what it advertises, on a pass that is really dispatched.
    progress = build(tmp, "wrapper-help", PLAN, [ident()])
    invocation = [sys.executable, str(WRAPPER), *words[1:],
                  "--file", str(progress), "--unit", "u01", "--status", STARTED]
    run = subprocess.run(invocation, capture_output=True, text=True, check=False)  # noqa: S603
    shown = " ".join([WRAPPER.name, *words[1:], "--file <progress> --unit u01 --status started"])
    if run.returncode != 0:
        print(f"FAIL     [help] the tool REFUSES the command its own --help advertises — `{shown}` exited "
              f"{run.returncode}: {(run.stdout + run.stderr).strip()}. The help door and the parser door "
              f"disagree about what the command IS, and the help is the one a reviewer reads")
        failures += 1
    else:
        print(f"ok       [help] the advertised invocation RUNS: `{shown}` -> exit 0")
    return failures


def build(tmp: Path, name: str, plan: "list[str] | None", progress: "list[str] | bytes") -> Path:
    """Write a fixture pass to disk RAW — bypassing every write-side check, because half these fixtures
    hold exactly what the write side would have refused. That is the point: the READ side must catch them
    without being told how they got there. (`progress` as BYTES is how a fixture holds what is not text.)"""
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "review-41-1.progress.jsonl"
    if isinstance(progress, bytes):
        path.write_bytes(progress)
    else:
        path.write_text("".join(line + "\n" for line in progress), encoding="utf-8")
    if plan is not None:
        (d / "review-41-1.plan.jsonl").write_text("".join(line + "\n" for line in plan), encoding="utf-8")
    return path


def run_cli(mod: types.ModuleType, argv: "list[str]") -> "tuple[int, str]":
    """Drive the REAL CLI in-process: (exit code, stdout+stderr). Never the internals — so argparse, the
    `Defect`/`OperatorError` -> exit-code mapping, and `main()`'s wiring are all under test too."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(argv)
    except SystemExit as exc:  # argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue() + err.getvalue()


def reads_back(mod: types.ModuleType, artifact: str, path: Path) -> "tuple[bool, str]":
    """The READ side's answer about a file a write just produced: CAN IT BE READ BACK?

    It calls the (possibly mutated) module's OWN read side — never this one's — because the question is
    always "would THIS tool read back what THIS tool wrote?", and a mutant is a tool with a rule removed.

    An exception is the loudest failure of all: the read side owes a VERDICT on any bytes, and a crash is
    not a verdict.
    """
    try:
        if artifact == PLAN_FILE:
            mod.load_plan(path)
            return True, "the plan reads back"
        verdict, reason = mod.evaluate(path, SHA)
        return verdict != UNUSABLE, f"{verdict}: {reason}"
    except Exception as exc:  # noqa: BLE001 - a crash on READ is a violation, not an error to propagate
        return False, f"crash:{type(exc).__name__}: {exc}"


def round_trip(mod: types.ModuleType, tmp: Path) -> "dict[str, tuple[str, str]]":
    """EVERY write command x EVERY pre-existing file state: does the property hold on each?

    `holds` = the command REFUSED (any non-zero exit), or it wrote and the result READS BACK.
    `VIOLATED` = it exited 0 and produced an artifact its own read side will not read. That is the bug
    class both findings belong to, and the one this asserts out of existence.
    """
    got: dict[str, tuple[str, str]] = {}
    for cmd, (artifact, argv) in WRITE_COMMANDS.items():
        for state, content in FILE_STATES.items():
            d = tmp / f"rt-{cmd}-{state}"
            d.mkdir(parents=True, exist_ok=True)
            # A sound plan sits beside every progress-file case, so that what `evaluate` says about the
            # produced file is about the PROGRESS file and nothing else.
            (d / PLAN_FILE).write_text("".join(line + "\n" for line in PLAN), encoding="utf-8")
            target = d / artifact
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(content)
            key = f"[round-trip] {cmd} on a {state} file"
            try:
                code, text = run_cli(mod, [cmd, "--file", str(target), *argv])
            except Exception as exc:  # noqa: BLE001 - the CLI owes an exit code, and a crash is not one
                got[key] = (f"crash:{type(exc).__name__}", str(exc))
                continue
            if code != 0:
                got[key] = (HOLDS, f"REFUSED (exit {code}) — nothing was written: {text.strip()}")
                continue
            ok, why = reads_back(mod, artifact, target)
            got[key] = (
                (HOLDS if ok else VIOLATED),
                f"exit 0, and the file it produced reads back as -> {why}",
            )
    return got


def run_cases(mod: types.ModuleType, tmp: Path) -> "dict[str, tuple[str, str]]":
    """Every fixture, every name case, every CLI case and the round-trip property, against this (possibly
    mutated) module.

    A mutant that CRASHES has not returned a verdict, and "no verdict" is itself a deviation — recorded,
    never swallowed."""
    got: dict[str, tuple[str, str]] = {}
    for name, (plan, progress, _want, _needle, _why) in CASES.items():
        path = build(tmp, f"case-{name}", plan, progress)
        try:
            got[name] = mod.evaluate(path, SHA)
        except Exception as exc:  # noqa: BLE001 - a crash IS the result here
            got[name] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (name, _want, _needle, _why) in enumerate(NAME_CASES):
        d = build(tmp, f"name-{i}", PLAN, WORKED).parent
        path = d / name
        path.write_text("".join(line + "\n" for line in WORKED), encoding="utf-8")
        try:
            got[f"[name] {name}"] = mod.evaluate(path, SHA)
        except Exception as exc:  # noqa: BLE001
            got[f"[name] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (argv, seed, _want, _needle, _why) in enumerate(CLI_CASES):
        path = build(tmp, f"cli-{i}", PLAN, seed)
        try:
            code, text = run_cli(mod, [argv[0], "--file", str(path), *argv[1:]])
            got[cli_key(i, argv)] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[cli_key(i, argv)] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (pname, argv, _want, _needle, _why) in enumerate(PLAN_CLI_CASES):
        plan = build(tmp, f"plan-cli-{i}", PLAN, []).parent / pname
        try:
            code, text = run_cli(mod, ["plan-add", "--file", str(plan), *argv])
            got[f"[plan] {pname} {' '.join(argv)}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[plan] {pname} {' '.join(argv)}"] = (f"crash:{type(exc).__name__}", str(exc))
    got.update(round_trip(mod, tmp))
    got.update(cross_door(mod, tmp))
    return got


def cli_key(i: int, argv: "list[str]") -> str:
    """The case's key. The INDEX is in it because the SEED is part of the case and the argv is not: `emit
    --unit u01 --status started` is a different case against an empty file than against a dispatched one —
    that is the whole of finding 1 — and two cases sharing a key would silently collapse into one."""
    return f"[cli {i}] {' '.join(argv)}"


def expectations() -> "dict[str, tuple[str, str, str]]":
    """case -> (expected outcome, needle its output must contain, why the case exists)."""
    out = {n: (w, needle, why) for n, (_p, _pr, w, needle, why) in CASES.items()}
    out.update({f"[name] {n}": (w, needle, why) for n, w, needle, why in NAME_CASES})
    out.update({cli_key(i, a): (f"exit{c}", needle, why)
                for i, (a, _seed, c, needle, why) in enumerate(CLI_CASES)})
    out.update({f"[plan] {p} {' '.join(a)}": (f"exit{c}", needle, why)
                for p, a, c, needle, why in PLAN_CLI_CASES})
    # The round trip's expectation is the PROPERTY, and it is the same for every case: the write is
    # refused, or what it wrote reads back. There is no needle — no particular rule has to fire, and
    # demanding one would be demanding a specific defect where the case only demands a sound outcome. The
    # OUTCOME is the whole assertion.
    out.update({f"[round-trip] {cmd} on a {state} file": (
        HOLDS, "",
        f"`{cmd}` against a {state} target: it must FAIL, or the file it wrote must READ BACK")
        for cmd in WRITE_COMMANDS for state in FILE_STATES})
    # …and the cross-door property, whose expectation is likewise the PROPERTY and not a particular rule:
    # the plan door refuses the id, or the emit door can match it. There is nothing else it may do.
    out.update({f"[cross-door] the id {uid!r}": (
        HOLDS, "",
        f"`plan-add --id {uid!r}` then `emit --unit {uid!r}`: the PLAN door must refuse the id, or the "
        f"EMIT door must be able to name the unit it planned")
        for uid in CROSS_DOOR_IDS.values()})
    return out


# The outcomes that mean "this passed": a mutant that turns a failing case into one of these has produced
# the loudest possible failure — the weakened tool says "ship it" about artifacts that are defective.
PASSING = (OK, "exit0")

MARKER_RE = re.compile(r"^(?P<indent>[ ]*)# MUTATE:(?P<rule>[a-z0-9-]+):(?P<weakening>.+?)\s*$")

# The functions that ENFORCE the contract. Every enforcement point inside them must carry a marker.
# `evaluate` is not one: it MAPS an exception to a verdict; it decides nothing.
RULE_FUNCTIONS = (
    "hook", "read_text", "parse_lines", "read_lines", "check_id", "check_unit", "plan_units", "load_plan",
    "check_event", "check_progress", "walk_progress", "check_identity_shape", "check_identity",
    "check_head", "check_progress_file", "check_plan_file", "decide", "parse_name", "check_ruled",
    "before_text", "write_line", "cmd_emit", "cmd_identity", "cmd_plan_add", "cmd_verify",
)
ENFORCING_EXCEPTIONS = ("Defect", "OperatorError")
# The NAMES as they are spelled in the source, because that is what the AST holds — `return UNUSABLE, …`
# parses to an `ast.Name` whose `id` is "UNUSABLE", never to the string "unusable" it evaluates to.
# `return OK` is the ABSENCE of a rule, so it is not here.
ENFORCING_VERDICT_NAMES = ("INCOMPLETE", "AMENDED", "UNUSABLE")

FALSE_PASS, VERDICT_KILL, MESSAGE_KILL, CRASH_KILL = "FALSE-PASS", "VERDICT", "MESSAGE", "CRASH"


def markers(source: str) -> "list[tuple[str, str, int]]":
    out = []
    for n, line in enumerate(source.splitlines(), 1):
        m = MARKER_RE.match(line)
        if m:
            out.append((m.group("rule"), m.group("weakening"), n))
    return out


def marked_statements(source: str) -> "dict[str, tuple[str, ast.stmt]]":
    """rule id -> (weakening, the statement the marker sits directly above).

    `ast.stmt`, never `ast.AST`: only a statement has a `lineno`/`end_lineno`, and those two are the whole
    reason this is collected — they are the span `mutate()` replaces.
    """
    tree = ast.parse(source)
    stmts = {node.lineno: node for node in ast.walk(tree) if isinstance(node, ast.stmt)}
    out: dict[str, tuple[str, ast.stmt]] = {}
    for rule, weakening, line in markers(source):
        stmt = stmts.get(line + 1)
        if stmt is None:
            raise SelfTestFailure(f"# MUTATE:{rule} on line {line} sits above no statement")
        if rule in out:
            raise SelfTestFailure(f"duplicate rule id {rule!r} — every rule is marked exactly once")
        out[rule] = (weakening, stmt)
    if not out:
        raise SelfTestFailure("no MUTATE markers — the rules cannot mark themselves absent")
    return out


def unmarked(source: str, marked: "dict[str, tuple[str, ast.stmt]]") -> "list[str]":
    """EVERY refusal and every non-OK return in a rule function must sit under a marker.

    This is the half of the question fixtures can NEVER answer. A rule added without a marker is never
    mutated, so nothing ever asks whether a fixture would notice its absence — it is reported "pinned" by
    nobody having looked. THE COUNT IS A CLAIM, and this is what makes the claim checkable: the inventory
    is DERIVED from the source, never typed into a report.

    Every node is NARROWED to the concrete statement type before its `lineno` is read. `ast.AST` does not
    declare one — reaching through the base class for it is how this walk would silently start reading the
    line number of something that has none.
    """
    lines = {stmt.lineno for _w, stmt in marked.values()}
    problems: list[str] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in RULE_FUNCTIONS:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise):
                exc = node.exc
                enforcing = (isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                             and exc.func.id in ENFORCING_EXCEPTIONS)
                what = "raise"
            elif isinstance(node, ast.Return):
                val = node.value
                enforcing = (isinstance(val, ast.Tuple) and bool(val.elts)
                             and isinstance(val.elts[0], ast.Name)
                             and val.elts[0].id in ENFORCING_VERDICT_NAMES)
                what = "return"
            else:
                continue  # `node` is now an ast.stmt, so `lineno` below is one it really has
            if enforcing and node.lineno not in lines:
                problems.append(
                    f"review-pass.py:{node.lineno}: {fn.name}() enforces a rule ({what}) with NO "
                    f"# MUTATE marker — an unmarked rule is never mutated, so nothing can report it unpinned"
                )
    return problems


def mutate(source: str, rule: str, weakening: str, stmt: ast.stmt) -> str:
    lines = source.splitlines()
    body = [f"{' ' * stmt.col_offset}{weakening}  # MUTANT:{rule}"]
    return "\n".join(lines[: stmt.lineno - 1] + body + lines[stmt.end_lineno:]) + "\n"


def load_module(source: str, name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = __file__
    exec(compile(source, f"<{name}>", "exec"), mod.__dict__)  # noqa: S102 - the whole job
    return mod


def check_commands_covered() -> "list[str]":
    """Is EVERY subcommand the parser has either driven by the round trip or declared to write nothing?

    This is what makes the round trip's coverage DERIVED rather than claimed. A new subcommand appears in
    the parser and in neither set below — so the suite goes red the day it is added, and stays red until
    someone says which it is. A hand-listed set of commands would have gone on passing, silently, about a
    write path nothing had ever driven.
    """
    _parser, commands = build_parser()
    problems = []
    for cmd in commands:
        if cmd not in WRITE_COMMANDS and cmd not in READ_ONLY_COMMANDS:
            problems.append(
                f"the parser has a subcommand `{cmd}` that the round trip does not drive. If it WRITES, "
                f"add it to WRITE_COMMANDS (the property must hold for it); if it writes nothing, add it "
                f"to READ_ONLY_COMMANDS. An undriven write path is one nothing has ever asked to read back"
            )
    for cmd in sorted(set(WRITE_COMMANDS) | set(READ_ONLY_COMMANDS)):
        if cmd not in commands:
            problems.append(f"`{cmd}` is driven by the round trip but the parser no longer has it")
    return problems


def self_test() -> int:
    source = Path(__file__).read_text(encoding="utf-8")
    expect = expectations()
    failures = 0

    for problem in check_commands_covered():
        print(f"COMMANDS {problem}")
        failures += 1

    with tempfile.TemporaryDirectory() as tmpdir:
        got = run_cases(sys.modules[__name__], Path(tmpdir))
        help_failures = check_wrapper_help(Path(tmpdir))
    print()
    for case, (want, needle, why) in expect.items():
        outcome, text = got[case]
        if outcome == want and needle in text:
            print(f"ok       {case[:44]:44} -> {outcome:11} ({why})")
        elif outcome != want:
            print(f"FAIL     {case[:44]:44} -> {outcome:11} expected {want}\n         got: {text}")
            failures += 1
        else:
            # Right outcome, WRONG RULE. The message is the only thing that says which rule fired, and a
            # fixture that goes `unusable` for someone else's reason pins nothing.
            print(f"FAIL     {case[:44]:44} -> {outcome:11} but nothing mentions {needle!r}\n         got: {text}")
            failures += 1
    print()
    failures += help_failures
    failures += check_boundaries()
    print()
    doc_failures = check_docs()
    failures += doc_failures
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the review-pass contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures + {len(NAME_CASES)} name cases + "
          f"{len(CLI_CASES) + len(PLAN_CLI_CASES)} CLI cases + "
          f"{len(WRITE_COMMANDS) * len(FILE_STATES)} round-trip cases ({len(WRITE_COMMANDS)} write "
          f"commands, derived from the parser, x {len(FILE_STATES)} pre-existing file states) + "
          f"{len(CROSS_DOOR_IDS)} cross-door cases (the plan door refuses the id, or the emit door can "
          f"match it) + {len(BOUNDARY_CASES)} boundary cases ({len(DOMAINS)} bounded values, each probed "
          f"JUST INSIDE and JUST OUTSIDE its declared domain) + {len(doc_examples())} DOC examples hold — "
          f"and the invocation "
          f"`{WRAPPER.name} --help` advertises was EXECUTED, and runs.\n")

    # …and now the question the block above CANNOT answer: is any rule pinned by NO fixture?
    marked = marked_statements(source)
    gaps = unmarked(source, marked)
    for gap in gaps:
        print(f"UNMARKED {gap}")
    if gaps:
        print(f"\n{len(gaps)} enforcement point(s) carry NO marker.")
        return 1

    print(f"{'rule':24} {'weakened to':42} {'killed by':32} {'outcome':11} kill")
    print(f"{'-' * 24} {'-' * 42} {'-' * 32} {'-' * 11} ----")
    unpinned, broken, tally = [], [], Counter()
    for rule, (weakening, stmt) in marked.items():
        try:
            mod = load_module(mutate(source, rule, weakening, stmt), f"rp_mutant_{rule.replace('-', '_')}")
        except SyntaxError as exc:
            broken.append(f"{rule}: the weakening {weakening!r} does not compile ({exc})")
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            mutant = run_cases(mod, Path(tmpdir))
        # A mutation only ever REMOVES a rule, so it can never turn a PASSING case into a failing one.
        # If it does, the mutation is bogus — a harness bug, never a pinned rule.
        wrong = [f"{c} expected {w} but the mutant returned {mutant[c][0]}"
                 for c, (w, _n, _y) in expect.items() if w in PASSING and mutant[c][0] != w]
        if wrong:
            broken.append(f"{rule}: BOGUS MUTATION — {'; '.join(wrong)}")
            continue
        killers = []
        for case, (want, needle, _why) in expect.items():
            if want in PASSING:
                continue  # a case that PASSES cannot kill a rule; it is a canary (checked above)
            outcome, text = mutant[case]
            if outcome in PASSING:
                strength = FALSE_PASS
            elif outcome.startswith("crash:"):
                strength = CRASH_KILL
            elif outcome != want:
                strength = VERDICT_KILL
            elif needle not in text:
                strength = MESSAGE_KILL
            else:
                continue
            killers.append((strength, case, outcome))
        order = {FALSE_PASS: 0, VERDICT_KILL: 1, CRASH_KILL: 2, MESSAGE_KILL: 3}
        killers.sort(key=lambda k: (order[k[0]], k[1]))
        if not killers:
            print(f"{rule:24} {weakening[:42]:42} {'NOTHING':32} {'—':11} UNPINNED")
            unpinned.append(rule)
            continue
        strength, case, outcome = killers[0]
        extra = f" (+{len(killers) - 1} more)" if len(killers) > 1 else ""
        tally[strength] += 1
        print(f"{rule:24} {weakening[:42]:42} {case[:32]:32} {outcome:11} {strength}{extra}")

    print()
    for b in broken:
        print(f"HARNESS BROKEN: {b}")
    if unpinned:
        print(f"{len(unpinned)} RULE(S) PINNED BY NO FIXTURE: {', '.join(unpinned)}\n"
              f"Delete any one of them and the fixtures still pass — the suite would report total health "
              f"while the tool had stopped checking. Write a fixture that FAILS when the rule is gone.")
    if unpinned or broken:
        return 1
    print(f"all {len(marked)} rules are pinned: {tally[FALSE_PASS]} by a FALSE PASS, "
          f"{tally[VERDICT_KILL]} by a verdict change, {tally[CRASH_KILL]} by a crash, "
          f"{tally[MESSAGE_KILL]} by its message. Remove any rule and a fixture fails.")
    return 0


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


def build_parser() -> "tuple[argparse.ArgumentParser, list[str]]":
    """The CLI, and the list of subcommands it actually has — DERIVED from the parser, never typed out.

    The round-trip fixture drives every command this returns and FAILS on one it does not know how to
    drive. So a subcommand added here is covered on the day it is added: there is no second list of
    commands to forget to update, which is the only way a new write path could ever ship unpinned.
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
    a.add_argument("--check", action="append", default=[], help="a concrete check; repeat (at least one)")

    v = sub.add_parser("verify", help="DOES THIS PASS COUNT? (it never reads the reviewer's report)")
    v.add_argument("--file", required=True, help="the ACTIVE launch attempt's progress.jsonl")
    v.add_argument("--head-sha", required=True, help="the PR's LIVE head — the pass must have run on it")
    v.add_argument("--amendments-ruled", type=int, default=0, metavar="N",
                   help="how many of this pass's plan amendments you have already ruled on — a count, so "
                        "N >= 0, and never more than the pass actually raised (default 0)")

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
        return {"emit": cmd_emit, "identity": cmd_identity,
                "plan-add": cmd_plan_add, "verify": cmd_verify}[args.cmd](args)
    except Defect as exc:
        fail(str(exc), 1)
    except OperatorError as exc:
        fail(str(exc), 2)


def main(argv: "list[str] | None" = None) -> int:
    p, _cmds = build_parser()
    return dispatch(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
