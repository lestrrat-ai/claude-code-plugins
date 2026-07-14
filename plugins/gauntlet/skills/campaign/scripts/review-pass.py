#!/usr/bin/env python3
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
  self-test   the fixtures, and the proof that every rule is pinned by one

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

BOTH DOORS, ALWAYS. Every rule here holds where the COMMANDS enter (`emit`, `identity`, `plan-add`) AND
where the DATA enters (`verify`). An invariant enforced only at the write door is not enforced: the
progress file is a plaintext file in a directory the reviewer can write to, the emit-only rule is prose,
and a hand-written line lands in it just fine. So `verify` re-derives EVERYTHING from the bytes and
assumes nothing about how they got there — it never trusts that the write tool was used. The write side
and the read side run the SAME functions, so there is no second implementation to drift.

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
import sys
import tempfile
import types
from collections import Counter
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
UNIT_STRINGS = ("id", "kind", "target")
UNIT = "unit"

# A git object id, as git writes one: 40 LOWERCASE hex. **A SHORT SHA HAS ESCAPED INTO REAL STATE IN THIS
# REPO TWICE**, once through a hand-written `pass_identity`. A prefix is not a commit: it does not identify
# the content a pass reviewed, and every "did this verdict describe the live tip?" comparison made against
# one is a comparison that cannot mean what it says.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A decimal integer: no sign, no leading zeros, no whitespace, no `int()` (which would take `" +2 "` and
# then CRASH on `"two"`). `pr`, `pass` and `launch_attempt` are compared to the values parsed out of the
# FILENAME, and a value we cannot compare is not one whose comparison we may assume.
NUM_RE = re.compile(r"^(0|[1-9][0-9]*)$")

# `dispatched_at` is the launch check's CLOCK — the ~5-minute first-event deadline is measured from it. A
# value that cannot be parsed as a time silently DISABLES that deadline: the guard's input is absent, so
# the guard never fires, and a reviewer that never started is waited on forever. UTC ISO-8601, `Z`.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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
NAME_RE = re.compile(r"^review-(?P<pr>\d+)-(?P<pass>\d+)(?:\.a(?P<attempt>[2-9]\d*))?\.progress\.jsonl$")

# The plan is PER-PASS, not per-attempt: a relaunch reuses it unchanged (stage-2-review-gate.md). So it is
# DERIVED from the progress path and never passed separately — one fewer door, and no way to point a pass
# at somebody else's plan.
PLAN_NAME = "review-{pr}-{pass}.plan.jsonl"


class Defect(Exception):
    """The artifacts are not evidence. -> `unusable`, at either door."""


class OperatorError(Exception):
    """The CALLER is wrong, not the artifacts. A verdict about the wrong question is worse than none."""


# --- the strict JSONL reader (shared by both artifacts) -----------------------------------------

def strict_object(path: Path, n: int):
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
                f"{path.name} line {n}: duplicate member name(s) {', '.join(dupes)} — the decoder keeps "
                f"only ONE value for a repeated key and discards the other, so the discarded one is in the "
                f"bytes and reaches no rule"
            )
        return dict(pairs)

    return hook


def read_lines(path: Path, what: str) -> "list[dict]":
    """Every line of a JSONL artifact, as a dict. No line is skipped — not a blank one, not a bad one.

    A line this reader cannot understand is a producer we cannot trust, and a producer we cannot trust is
    not one whose output a PR may merge on.
    """
    if not path.exists():
        # MUTATE:file-missing:pass
        raise Defect(
            f"no {what} at {path} — a review pass whose {what} is missing produced no evidence at all "
            f"(the orchestrator writes the plan before dispatch and `pass_identity` before the reviewer "
            f"starts, so this file exists from dispatch onward)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # MUTATE:unreadable:text = path.read_bytes().decode("utf-8", errors="replace")
        raise Defect(
            f"{path.name} cannot be read as UTF-8 text ({exc}) — bytes we cannot decode are not evidence, "
            f"and decoding them LENIENTLY rewrites what the file says"
        ) from exc

    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            # MUTATE:blank-line:continue
            raise Defect(
                f"{path.name} line {n} is blank — JSONL has no blank lines, and a producer that writes one "
                f"is not one we can trust with the lines we DO read"
            )
        try:
            rec = json.loads(line, object_pairs_hook=strict_object(path, n))
        except json.JSONDecodeError as exc:
            # MUTATE:not-json:continue
            raise Defect(
                f"{path.name} line {n} is not JSON ({exc}) — a corrupt line is a corrupt artifact, never a "
                f"line to skip past"
            ) from exc
        except RecursionError as exc:
            # MUTATE:too-deep:continue
            raise Defect(
                f"{path.name} line {n} is nested too deeply for the decoder — it RAISED where a verdict "
                f"was owed, and a crash is not a verdict"
            ) from exc
        if not isinstance(rec, dict):
            # MUTATE:not-object:continue
            raise Defect(
                f"{path.name} line {n} is not a JSON object — every line of this artifact is one event"
            )
        out.append(rec)
    return out


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


def load_plan(path: Path) -> "dict[str, dict]":
    """The plan's units, by id. A plan is what makes `done` MEAN something — so it is validated, not read.

    An EMPTY plan is refused, and that rule carries the most weight of any here: "every planned unit is
    done" is VACUOUSLY TRUE of a plan with no units, so a pass that reviewed NOTHING would verify `ok`.
    A completeness check whose input can be empty is not a check.
    """
    units: dict[str, dict] = {}
    for n, rec in enumerate(read_lines(path, "plan"), start=1):
        check_unit(rec, f"{path.name} line {n}")
        if rec["id"] in units:
            # MUTATE:plan-duplicate-id:pass
            raise Defect(
                f"{path.name} line {n}: duplicate unit id {rec['id']!r} — a `done` naming it would be "
                f"ambiguous about WHICH unit was checked"
            )
        units[rec["id"]] = rec
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


def check_identity_shape(ident: dict, where: str) -> None:
    """Every VALUE in a `pass_identity`, checked once — and therefore at BOTH doors, because `identity`
    (write) and `check_identity` (read) both call this and there is no second implementation to drift.

    The identity is the pass's attempt id and its dispatch clock, and three rules downstream depend on it:
    a late verdict is ignored unless its attempt id still matches; the ~5-minute launch deadline is
    measured from `dispatched_at`; `launch_attempt` is how a *later* wake — possibly a fresh agent — knows
    the pass was already relaunched once. Every one of those is a COMPARISON, and a comparison against a
    malformed value is not one.
    """
    if not SHA_RE.match(ident["head_sha"]):
        # MUTATE:identity-sha:pass
        raise Defect(
            f"{where}: head_sha {ident['head_sha']!r} is not a git object id (40 LOWERCASE hex). A short "
            f"sha has escaped into this repo's real state TWICE — once through a hand-written "
            f"`pass_identity`. A prefix is not a commit: it names no content, so every 'did this verdict "
            f"describe the live tip?' comparison made against it is unfalsifiable. Use `git rev-parse "
            f"HEAD`, never an abbreviation"
        )
    for field in ("pr", "pass", "launch_attempt"):
        if not NUM_RE.match(ident[field]):
            # MUTATE:identity-numbers:continue
            raise Defect(
                f"{where}: `{field}` is {ident[field]!r}, not a decimal number — it is COMPARED to the "
                f"value in the FILENAME, and a value we cannot compare proves nothing"
            )
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


def check_identity(events: "list[dict]", pr: str, npass: str, attempt: str, head_sha: str) -> dict:
    """The `pass_identity` line: exactly one, FIRST, well-formed, and agreeing with the NAME it is filed
    under and with the commit the caller says the PR is on."""
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
    if ident["head_sha"] != head_sha:
        # MUTATE:identity-head-mismatch:pass
        raise Defect(
            f"this pass ran on {ident['head_sha']} but the PR's head is {head_sha} — its verdict describes "
            f"content that is no longer there, and PR content changing is exactly what voids a tally"
        )
    return ident


# --- the verdict ---------------------------------------------------------------------------------

def decide(events: "list[dict]", units: "dict[str, dict]", ruled: int) -> "tuple[str, str]":
    """Given SOUND artifacts: does this pass COUNT? (Its report is still not read. That is the point.)

    A `done` REQUIRES an earlier `started` for the same unit, and this walk enforces it BY ORDER, not by
    presence: `announced` only ever holds units a line ALREADY READ announced, so a `started` that appears
    BELOW its `done` cannot satisfy it. Presence alone would be the weaker rule and the wrong one — the
    file is APPEND-ONLY, so its order IS the order the events happened in, and a forger who has to also
    fabricate the `started` FIRST has to fabricate the whole sequence, which is precisely the thing the
    file is evidence of. "u01 finished, then u01 began" is not a review; it is a file with the right lines
    in it.

    **This is the rule that was PROSE and enforced by NOBODY, and it is the one the tool most needed.** A
    progress file with a valid identity and a `done` for EVERY planned unit — and NOT ONE `started` —
    verified `ok`: the tool that exists to prove a review HAPPENED accepted a review that demonstrably did
    not. Skip straight to "done" for every unit and the gate was satisfied on zero evidence of work. A
    `done` with no `started` is not progress, exactly as an empty plan is not a plan.
    """
    announced: set[str] = set()
    done: dict[str, str] = {}
    for n, rec in enumerate(events, start=1):
        if rec["type"] != PROGRESS:
            continue
        unit = rec["unit"]
        if unit not in units:
            # MUTATE:unplanned-unit:pass
            raise Defect(
                f"line {n}: progress for unit {unit!r}, which is NOT IN THE PLAN — the reviewer never "
                f"rewrites the plan or self-grants units, and progress counts only when it references a "
                f"PLANNED unit. Planned: {sorted(units)}"
            )
        if rec["status"] != DONE:
            announced.add(unit)
            continue
        if unit not in announced:
            # MUTATE:done-without-started:pass
            raise Defect(
                f"line {n}: {DONE!r} for unit {unit!r} with no earlier {STARTED!r} for it — a unit that "
                f"was never begun cannot have been finished. The reviewer emits {STARTED!r} when a unit "
                f"BEGINS and {DONE!r} when it ends, so a `{DONE}` standing alone (or standing ABOVE its "
                f"`{STARTED}` in this append-only file) is not the record of a review that happened; it is "
                f"a file with the right lines in it"
            )
        if unit in done:
            # MUTATE:duplicate-done:pass
            raise Defect(
                f"line {n}: a SECOND {DONE!r} for unit {unit!r} — the file now offers two accounts of one "
                f"unit, and nothing says which was read"
            )
        done[unit] = rec["evidence"]

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


def evaluate(progress: Path, head_sha: str, ruled: int = 0) -> "tuple[str, str]":
    """The whole read side. Every exception a rule can raise lands here as a VERDICT — never as a crash."""
    try:
        pr, npass, attempt = parse_name(progress)
        events = read_lines(progress, "progress file")
        check_events(events, progress)
        check_identity(events, pr, npass, attempt, head_sha)
        units = load_plan(progress.parent / PLAN_NAME.format(pr=pr, **{"pass": npass}))
        return decide(events, units, ruled)
    except Defect as exc:
        return UNUSABLE, str(exc)


def check_events(events: "list[dict]", path: Path) -> None:
    for n, rec in enumerate(events, start=1):
        check_event(rec, f"{path.name} line {n}")


def count_amendments(progress: Path) -> int:
    """How many amendments the file holds — read WITHOUT judging it, so `--amendments-ruled` can be
    checked against reality before any verdict is computed."""
    try:
        return sum(1 for e in read_lines(progress, "progress file") if e.get("type") == AMENDMENT)
    except Defect:
        return 0  # a file we cannot read has no countable amendments; `evaluate` will say so, loudly


# --- the write side (the same rules, at the other door) ------------------------------------------

def append(path: Path, rec: dict) -> str:
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(line)
    return line


def announced_units(path: Path) -> "set[str]":
    """The units this progress file has ALREADY announced a `started` for — read from the BYTES on disk.

    The write door cannot ask its own process what it emitted: a reviewer is many `emit` invocations, each
    a fresh process, and the only thing that survives between them is the file. So the file is the memory,
    and reading it is how `emit` knows whether the `done` it was just handed follows a `started`.
    """
    out: set[str] = set()
    for e in read_lines(path, "progress file"):
        if e.get("type") == PROGRESS and e.get("status") == STARTED and isinstance(e.get("unit"), str):
            out.add(e["unit"])
    return out


def cmd_emit(args) -> int:
    """Append one unit-progress event — the ONLY sanctioned way a reviewer records one.

    It refuses an unplanned unit HERE TOO, and not only in `verify`: the reviewer gets a non-zero exit and
    a message it can act on, at the moment it makes the mistake, instead of a pass silently thrown away
    fifteen minutes later. Same for a `done` that no `started` precedes.
    """
    path = Path(args.file)
    pr, npass, _attempt = parse_name(path)
    unit = args.unit.strip()
    if not unit:
        # MUTATE:emit-empty-unit:pass
        raise Defect("--unit must be non-empty")
    if args.status == DONE and (args.evidence is None or not args.evidence.strip()):
        # MUTATE:emit-done-evidence:pass
        raise Defect(
            "--evidence is required and must be non-empty when --status is done: a unit is not done "
            "because you say so, it is done because you can CITE what you checked"
        )
    if args.status == STARTED and args.evidence is not None:
        # MUTATE:emit-started-evidence:pass
        raise Defect("--evidence must NOT be provided when --status is started")

    rec = {"type": PROGRESS, "unit": unit, "status": args.status}
    if args.status == DONE:
        rec["evidence"] = args.evidence
    check_event(rec, "the event you asked to emit")
    units = load_plan(path.parent / PLAN_NAME.format(pr=pr, **{"pass": npass}))
    if unit not in units:
        # MUTATE:emit-unplanned:pass
        raise Defect(
            f"unit {unit!r} is NOT IN THE PLAN — you may not self-grant a unit. Planned: {sorted(units)}. "
            f"If the plan is missing a dimension, raise a plan_amendment_request instead"
        )
    # LAST, deliberately: an unplanned `done` must still be refused as UNPLANNED. Were this check first, a
    # reviewer self-granting a unit would be told "no earlier started" — true, and the wrong lesson.
    if args.status == DONE and unit not in announced_units(path):
        # MUTATE:emit-done-without-started:pass
        raise Defect(
            f"unit {unit!r} has no earlier `{STARTED}` event in {path.name} — emit `--status {STARTED}` "
            f"when the unit BEGINS, and `--status {DONE}` when it ends. A unit that was never begun cannot "
            f"be finished, and `verify` reads this back under the same rule: a `{DONE}` with no `{STARTED}` "
            f"makes the whole pass unusable, so writing one here would only lose the pass later"
        )
    sys.stdout.write(append(path, rec))
    return 0


def cmd_identity(args) -> int:
    """Write a pass's `pass_identity` — the line that used to be a `printf`, and once got a TRUNCATED SHA."""
    path = Path(args.file)
    pr, npass, attempt = parse_name(path)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace").strip():
        # MUTATE:identity-write-first:pass
        raise Defect(
            f"{path.name} already holds events — `pass_identity` is the FIRST line of a launch attempt's "
            f"progress file, written before the reviewer starts. A relaunch gets its OWN file "
            f"(`review-<pr>-<n>.a<k>.progress.jsonl`), never this one"
        )
    rec = {
        "type": IDENTITY, "pr": pr, "pass": npass, "head_sha": args.head_sha,
        "launch_attempt": attempt, "dispatched_at": args.dispatched_at,
    }
    # The SAME two functions the read side runs — so a `pass_identity` this door writes is one `verify`
    # can never call malformed, and the sha/clock rules exist in exactly one place.
    check_event(rec, "the pass_identity you asked to write")
    check_identity_shape(rec, "the pass_identity you asked to write")
    sys.stdout.write(append(path, rec))
    return 0


def cmd_plan_add(args) -> int:
    """Append one validated unit to a pass's plan — the artifact that used to be a shell heredoc."""
    path = Path(args.file)
    rec = {
        "type": UNIT, "id": args.id, "kind": args.kind, "target": args.target,
        "checks": list(args.check),
    }
    check_unit(rec, "the unit you asked to add")
    if path.exists():
        for existing in read_lines(path, "plan"):
            check_unit(existing, f"{path.name}")
            if existing["id"] == rec["id"]:
                # MUTATE:plan-add-duplicate:break
                raise Defect(f"the plan already holds a unit {rec['id']!r}")
    sys.stdout.write(append(path, rec))
    return 0


def cmd_verify(args) -> int:
    path = Path(args.file)
    if not SHA_RE.match(args.head_sha):
        # MUTATE:caller-sha:pass
        raise OperatorError(
            f"--head-sha {args.head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to verify. "
            f"Every comparison below would be against a value that cannot be a commit, so the verdict "
            f"would be about the wrong question. No verdict beats a wrong one"
        )
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
# it. `self-test` then does three things, in order, and the third is the one that matters:
#
#   1. runs every fixture and asserts its verdict AND the needle its reason must contain (the reason is
#      the only thing that says WHICH rule fired — a fixture that goes `unusable` for someone else's
#      reason pins NOTHING);
#   2. asserts that EVERY enforcement point in a rule function sits under a marker. A rule ADDED without
#      one is never mutated, so nothing could ever report it unpinned. An unmarked rule is an untested one;
#   3. DELETES each rule in turn — splicing in its weakening — re-runs every fixture and every CLI case,
#      and FAILS if no fixture notices.
#
# Step 3 exists because step 1 CANNOT see the failure that matters most: a rule NOTHING tests. Delete such
# a rule and the suite stays green while the tool has quietly stopped checking. A sibling PR in this repo
# proved the danger is not theoretical: a hand-written "N rules pinned" matrix was TRUE and INSUFFICIENT —
# 8 guards were not in the inventory at all, and 7 of those were pinned by nothing. THE COUNT IS A CLAIM.
# So the count is DERIVED, here, on every CI run, and "which rules are unpinned?" is a question the SUITE
# answers rather than one a reviewer has to discover.

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
    "identity-bad-number": (PLAN, [ident(launch_attempt="one"), done("u01")], UNUSABLE, "not a decimal number",
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
# still under test. The default seed is an EMPTY progress file: what the orchestrator has in hand when it
# writes `pass_identity`, and what `emit` appends to thereafter.
EMPTY: "list[str]" = []
BEGUN = [ident(), started("u01")]  # the file a reviewer has in hand once it has ANNOUNCED u01
CLI_CASES = [
    (["emit", "--unit", "u01", "--status", "started"], EMPTY, 0, '"status":"started"', "the call every reviewer prompt makes"),
    (["emit", "--unit", "u01", "--status", "done", "--evidence", "f.py:1"], BEGUN, 0, '"evidence":"f.py:1"',
     "…and its done form, on the file that HAS the matching `started` — the only file the done form was ever meant to be run against"),
    (["emit", "--unit", "u02", "--status", "done", "--evidence", "f.py:1"], BEGUN, 1, "no earlier `started`",
     "HEADLINE, WRITE DOOR: a `done` for a unit that was never begun. The write door refuses it at the moment the reviewer makes the mistake, instead of the pass being thrown away by `verify` fifteen minutes later"),
    (["emit", "--unit", "u99", "--status", "done", "--evidence", "f.py:1"], EMPTY, 1, "NOT IN THE PLAN",
     "HEADLINE, WRITE DOOR: the tool accepted a self-granted unit. It no longer does — and it says UNPLANNED, not 'no started': an unplanned unit's real defect is that nobody planned it"),
    (["emit", "--unit", "u99", "--status", "started"], EMPTY, 1, "NOT IN THE PLAN", "…and refuses to START one"),
    (["emit", "--unit", "u01", "--status", "done"], EMPTY, 1, "--evidence is required", "a done with no evidence"),
    (["emit", "--unit", "u01", "--status", "done", "--evidence", "  "], EMPTY, 1, "--evidence is required", "…or blank evidence"),
    (["emit", "--unit", "u01", "--status", "started", "--evidence", "x"], EMPTY, 1, "must NOT be provided", "a started carrying evidence"),
    (["emit", "--unit", "  ", "--status", "started"], EMPTY, 1, "--unit must be non-empty", "an empty unit id"),
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
    (["identity", "--head-sha", OTHER_SHA, "--dispatched-at", TS], [ident()], 1, "already holds events",
     "a SECOND identity into a live pass's file. `pass_identity` is the FIRST line, written before dispatch — a relaunch gets its OWN file, and appending here is how one pass ends up describing two commits"),
    (["verify", "--head-sha", SHA[:7]], EMPTY, 2, "No verdict beats a wrong one",
     "an OPERATOR error is not a snapshot verdict: exit 2, never a verdict computed from a comparison that could not have succeeded"),
    (["verify", "--head-sha", SHA, "--amendments-ruled", "1"], EMPTY, 2, "raised only 0",
     "a ruling for an amendment that does not exist would silently clear the NEXT one raised"),
]

# `plan-add` gets its own family: its `--check` is repeatable, so its argv do not fit the shape above.
PLAN_CLI_CASES = [
    (["--id", "u03", "--kind", "cross-cutting", "--target", "both doors", "--check", "a", "--check", "b"],
     0, '"checks":["a","b"]', "the plan stops being a shell heredoc"),
    (["--id", "u01", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "already holds a unit",
     "a duplicate id — a `done` for it would say nothing about which unit was checked"),
    (["--id", "  ", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "names nothing", "a blank id"),
    (["--id", "u03", "--kind", "file", "--target", "x.py"], 1, "not a unit", "a unit with NO checks"),
]


class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


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


def run_cases(mod: types.ModuleType, tmp: Path) -> "dict[str, tuple[str, str]]":
    """Every fixture, every name case and every CLI case, against this (possibly mutated) module.

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
            got[f"[cli] {' '.join(argv)}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[cli] {' '.join(argv)}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (argv, _want, _needle, _why) in enumerate(PLAN_CLI_CASES):
        plan = build(tmp, f"plan-cli-{i}", PLAN, []).parent / "review-41-1.plan.jsonl"
        try:
            code, text = run_cli(mod, ["plan-add", "--file", str(plan), *argv])
            got[f"[plan] {' '.join(argv)}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[plan] {' '.join(argv)}"] = (f"crash:{type(exc).__name__}", str(exc))
    return got


def expectations() -> "dict[str, tuple[str, str, str]]":
    """case -> (expected outcome, needle its output must contain, why the case exists)."""
    out = {n: (w, needle, why) for n, (_p, _pr, w, needle, why) in CASES.items()}
    out.update({f"[name] {n}": (w, needle, why) for n, w, needle, why in NAME_CASES})
    out.update({f"[cli] {' '.join(a)}": (f"exit{c}", needle, why) for a, _seed, c, needle, why in CLI_CASES})
    out.update({f"[plan] {' '.join(a)}": (f"exit{c}", needle, why) for a, c, needle, why in PLAN_CLI_CASES})
    return out


# The outcomes that mean "this passed": a mutant that turns a failing case into one of these has produced
# the loudest possible failure — the weakened tool says "ship it" about artifacts that are defective.
PASSING = (OK, "exit0")

MARKER_RE = re.compile(r"^(?P<indent>[ ]*)# MUTATE:(?P<rule>[a-z0-9-]+):(?P<weakening>.+?)\s*$")

# The functions that ENFORCE the contract. Every enforcement point inside them must carry a marker.
# `evaluate` is not one: it MAPS an exception to a verdict; it decides nothing.
RULE_FUNCTIONS = (
    "hook", "read_lines", "check_unit", "load_plan", "check_event", "check_identity_shape",
    "check_identity", "decide", "parse_name", "cmd_emit", "cmd_identity", "cmd_plan_add", "cmd_verify",
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


def self_test() -> int:
    source = Path(__file__).read_text(encoding="utf-8")
    expect = expectations()
    failures = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        got = run_cases(sys.modules[__name__], Path(tmpdir))
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
    if failures:
        print(f"{failures} check(s) FAILED — the review-pass contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures + {len(NAME_CASES)} name cases + "
          f"{len(CLI_CASES) + len(PLAN_CLI_CASES)} CLI cases hold.\n")

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


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="append one unit-progress event (what emit-progress.py calls)")
    e.add_argument("--file", required=True, help="the launch attempt's progress.jsonl")
    e.add_argument("--unit", required=True, help="a PLANNED unit's id — an unplanned one is refused")
    e.add_argument("--status", required=True, choices=STATUSES)
    e.add_argument("--evidence", help="concrete citation; REQUIRED for --status done")

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
                   help="how many of this pass's plan amendments you have already ruled on (default 0)")

    sub.add_parser("self-test", help="run every fixture, then DELETE each rule and prove a fixture notices")

    args = p.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    try:
        return {"emit": cmd_emit, "identity": cmd_identity,
                "plan-add": cmd_plan_add, "verify": cmd_verify}[args.cmd](args)
    except Defect as exc:
        fail(str(exc), 1)
    except OperatorError as exc:
        fail(str(exc), 2)


if __name__ == "__main__":
    raise SystemExit(main())
