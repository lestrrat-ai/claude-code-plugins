#!/usr/bin/env python3
"""THE EXECUTABLE CONTRACT FOR `review-pass.py` — every rule pinned by a fixture, and every fixture proved
to pin one by DELETING the rule and watching it fail.

Run it through the tool it tests (this is what CI runs):

    python3 review-pass.py self-test

or directly, which does the same thing:

    python3 review-pass-test.py

**THE SUITE IS A SIBLING, NOT A SECTION.** It used to live inside `review-pass.py`. A fixture table that
ships inside the tool it tests is one that a single edit can make agree with itself — and a reviewer proved
exactly that on a sibling script in this repo: it spliced `CASES=[]` into the source in memory, and
`self_test()` still exited 0, reporting "all 0 fixtures hold". `review-pass.py self-test` now loads this
file by a `__file__`-relative path and FAILS LOUDLY if it is not there.

**THE TOOL UNDER TEST IS HANDED IN** (`run(R, tmp)`), so the fixtures drive the code that command actually
loaded. Every data table is built FROM that module (`Tables`), so a constant is never restated here — a
fixture that retyped `"not-satisfied"` would go on passing after the tool had stopped spelling it that way.

Six families, and each answers a question the others cannot:

  1. FIXTURES        — one rule each, asserted by VERDICT *and* by the needle that says WHICH rule fired.
  2. CLI CASES       — the same rules at the WRITE doors, plus the doors' own refusals.
  3. ROUND TRIP      — every write command x every pre-existing file state: **the command FAILS, or the file
                       it produced VERIFIES.** No per-rule fixture can state that; it is a property of the
                       doors TOGETHER, and both of the tool's worst bugs lived there.
  4. CROSS-DOOR      — an id the PLAN door takes is an id the EMIT door can NAME.
  5. BOUNDARIES      — every declared domain probed JUST INSIDE and JUST OUTSIDE. Two of this tool's bugs
                       were a boundary no fixture stood on (`a10`; `--amendments-ruled -1`).
  6. DOORS + DOCS    — every door RUN in the shape its own `--help` advertises; every JSON example in the
                       skill's docs fed THROUGH the tool.

…and then the MUTATION MATRIX, which is the only one that can answer "is any rule pinned by NOTHING?" It
deletes each rule in turn and fails if no fixture notices. THE COUNT IS A CLAIM; this derives it.

**THE FINDINGS FAMILY IS THE NEW ONE, AND IT IS THE POINT OF THE PR THAT ADDED IT.** Its regression fixtures
are the REAL findings from a review loop that ran one PR through 21 rounds and another through 14, and
converged on neither. They are reproduced here verbatim so the rule can be checked against the record rather
than against an argument: the false-green finding that MUST still gate (round-added, `writer=network`,
defends the PR's whole purpose), and the two self-test findings that must NOT (nobody but a developer with a
text editor can reach them).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from _gauntlet.mutation import (
    load_source_module,
    marked_statements,
    mutate_source,
    unmarked_enforcements,
)

HERE = Path(__file__).resolve().parent
OWNER = HERE / "review-pass.py"
WRAPPER = HERE / "emit-progress.py"
FINDING_WRAPPER = HERE / "emit-finding.py"
WRAPPER_DOOR = "emit-progress.py"
FINDING_WRAPPER_DOOR = "emit-finding.py"
WRAPPER_OWNER_COMMANDS = {
    WRAPPER_DOOR: "emit",
    FINDING_WRAPPER_DOOR: "finding-add",
}

# The `self-test` door is a door like any other, and EXECUTING it is what the door check does to every door
# — so probing it means self-test runs self-test. This is what stops that being infinite: the nested run
# sees the variable and skips ONLY the door checks (everything else runs in full), so the door is really
# executed, by the real parser, all the way through its real body.
DOOR_PROBE_ENV = "REVIEW_PASS_DOOR_PROBE"

SHA = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
OTHER_SHA = "b" * 40
TS = "2026-07-06T00:00:00Z"

PROGRESS_FILE = "review-41-1.progress.jsonl"
PLAN_FILE = "review-41-1.plan.jsonl"
FINDINGS_FILE = "review-41-1.findings.jsonl"
INTENT_FILE = "intent-41.md"

# --- THE INTENT the fixtures are measured against -------------------------------------------------
#
# This is the artifact `pr-adoption.md` writes into the run dir before a PR's first review pass — the thing
# the reviewer was NEVER given, and whose absence is the whole story. The dispatch prompt used to say
# "review the changes on this branch", full stop; the run did not so much as FETCH the PR's body. So the
# reviewer was asked "is anything wrong with this code?" — a question with no fixed point — instead of
# "does this PR do its job?".
#
# The `## Purpose` lines below are what a finding must QUOTE. They are deliberately the real ones from the
# PR that spent 14 rounds hunting false greens, because the regression fixtures at the bottom of this file
# are that PR's real findings.
INTENT = """\
# What this PR is for

## Purpose
- derive ci with a tool, not by eye
- never emit a false green

## Non-goals
- hardening the tool's own self-test against a developer who edits it

## Threat model
- Who can write the inputs this code reads: GitHub's API over the network; the CI system; a user's CLI arguments
- Who cannot: nobody else — the run dir is git-ignored and driver-owned, and no one but the driver writes it
"""

PURPOSE_GREEN = "never emit a false green"
PURPOSE_TOOL = "derive ci with a tool, not by eye"


class SelfTestFailure(AssertionError):
    """A rule this tool claims to enforce does not hold."""


class _Drop:
    """The sentinel that REMOVES a key from a fixture record, so a fixture can OMIT a required field.

    It is not `None`: `null` is a legal JSON value, so a fixture must stay free to write
    `"evidence": null` and watch the tool refuse it. "Absent" and "present and null" are different bytes
    and different defects — collapse them onto one sentinel and one of the two becomes untestable.
    """


DROP = _Drop()

# A fixture record's values are typed `object`, and that IS the type: these builders exist to write what
# the schema FORBIDS — an `evidence` that is a list, a `proposed_unit` that is a string, a `line` that is a
# number, a key that is not there at all. Declaring them `str` would be a promise the fixtures are written
# to break, and a type checker believing it would reject the very cases the read side must catch.
Value = object


def _rec(fields: "dict[str, Value]", over: "dict[str, Value]") -> str:
    rec = {**fields, **over}
    return json.dumps({k: v for k, v in rec.items() if v is not DROP})


class Tables:
    """Every fixture table, built ONCE from the module under test.

    Nothing here restates a constant. `R.OK`, `R.UNUSABLE`, `R.FINDING`, `R.STARTED` — all of them are read
    off the tool, so a fixture cannot go on asserting a spelling the tool has abandoned. (The mutation
    harness only ever replaces statements INSIDE rule functions, so these constants are the same in a
    mutant as in the original — which is what makes it sound to build the tables once and reuse them.)
    """

    def __init__(self, R: types.ModuleType) -> None:
        self.R = R

        def ident(**over: Value) -> str:
            return _rec({"type": R.IDENTITY, "pr": "41", "pass": "1", "head_sha": SHA,
                         "launch_attempt": "1", "dispatched_at": TS}, over)

        def unit(uid: str = "u01", **over: Value) -> str:
            return _rec({"type": R.UNIT, "id": uid, "kind": "file", "target": "scripts/review-pass.py",
                         "checks": ["the read side refuses what the write side refuses"]}, over)

        def started(uid: str = "u01", **over: Value) -> str:
            return _rec({"type": R.PROGRESS, "unit": uid, "status": R.STARTED}, over)

        def done(uid: str = "u01", evidence: Value = "review-pass.py:42 `check_event`", **over: Value) -> str:
            return _rec({"type": R.PROGRESS, "unit": uid, "status": R.DONE, "evidence": evidence}, over)

        def amendment(**over: Value) -> str:
            return _rec({"type": R.AMENDMENT, "ts": TS, "reason": "no unit covers the mutation harness",
                         "proposed_unit": json.loads(unit("u99"))}, over)

        def finding(**over: Value) -> str:
            return _rec({"type": R.FINDING, "file": "scripts/ci-status.py", "line": "421",
                         "writer": "network", "purpose": PURPOSE_GREEN,
                         "repro": "a rollup whose headRefOid moved while the REST page still read green",
                         "fix": "refuse a snapshot whose head moved under the fetch"}, over)

        self.ident, self.unit, self.started, self.done = ident, unit, started, done
        self.amendment, self.finding = amendment, finding

        self.PLAN = [unit("u01"),
                     unit("u02", target="stage-2-review-gate.md", checks=["the docs match the tool"])]
        self.WORKED = [ident(), started("u01"), done("u01"), started("u02"),
                       done("u02", evidence="stage-2:161")]

        # A progress file written as BYTES, not lines — a sound pass with one byte in it that is not UTF-8.
        # Read leniently, `\xff` becomes U+FFFD and the file quietly says something it does not say.
        self.RAW_BYTES = b'{"type":"progress","unit":"u01","status":"done","evidence":"\xff"}\n'

        OK, UNUSABLE, INCOMPLETE, AMENDED = R.OK, R.UNUSABLE, R.INCOMPLETE, R.AMENDED
        PLAN, WORKED = self.PLAN, self.WORKED

        # name -> (plan lines, progress lines, expected verdict, needle its reason must contain, why).
        # EVERY fixture must FAIL WHEN ITS RULE IS DELETED — the mutation matrix checks that, one rule at a
        # time, and reports any rule no fixture notices the loss of.
        self.CASES: "dict[str, tuple]" = {
            "worked": (PLAN, WORKED, OK, "ARTIFACTS are sound",
                       "the shape of a pass that counts — and the tool STILL does not say SATISFIED"),

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
            "not-utf8": (PLAN, self.RAW_BYTES, UNUSABLE, "UTF-8",
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

        # The NAME cases. Same sound pass every time — only the FILENAME differs, so the name is the only
        # thing under test.
        self.NAME_CASES = [
            ("review-41-1.progress.jsonl", OK, "ARTIFACTS are sound", "attempt 1's name — the real artifact's shape"),
            ("review-41-1.a2.progress.jsonl", UNUSABLE, "silent self-defeat",
             "THE ONE THAT MATTERS: a RELAUNCH's file holding attempt 1's identity. The live pass would be writing into the dead attempt's file, and the launch check would read it as never launched"),
            ("review-42-1.progress.jsonl", UNUSABLE, "silent self-defeat", "another PR's pass, filed under this one"),
            ("review-41-2.progress.jsonl", UNUSABLE, "silent self-defeat", "pass 2's file holding pass 1's identity"),
            ("progress.jsonl", UNUSABLE, "not a progress artifact's name", "a name that binds these bytes to nothing at all"),
            ("review-41-1.progress.json", UNUSABLE, "not a progress artifact's name", "one character off is not the artifact"),
        ]

        # --- THE FINDINGS FAMILY --------------------------------------------------------------------
        #
        # (plan, progress, findings lines, intent text or None, --verdict or None, want, needle, why)
        #
        # This is the family the whole PR exists for. A finding used to be PROSE, so nothing could validate
        # its citation, bound its writer, or ask what it DEFENDED — and therefore nothing could ever decline
        # one. Every finding became a fix; every fix added surface; the next reviewer hunted the surface.
        F = self.finding
        NS, SAT, DEF = R.NOT_SATISFIED, R.SATISFIED, R.DEFERRED

        # THE THREE REGRESSION FIXTURES, FROM THE REAL RECORD. They are the acceptance test for the gating
        # rule, and they are quoted from the actual review artifacts of the two PRs that never converged.
        R43_11 = F(  # PR #43 round 11 — round-added code, and it STILL GATES. The case that kills the
                     # naive rule ("a finding against code an earlier fix round added is non-gating").
            file="scripts/ci-status.py", line="769", writer="network", purpose=PURPOSE_GREEN,
            repro="I removed the `statuses` member from the otherwise-green fixture while leaving "
                  "`total_count: 0`; `derive()` returned `verdict=green`, `ci=green`",
            fix="treat a MISSING row array as unusable — `page.get(rows_key) or []` reads absence as empty",
        )
        R42_23 = F(  # PR #42 round 23, the LAST round before a human stopped it. Verbatim.
            file="scripts/followups.py", line="1815", writer="dev-time", purpose="-",
            repro='I mutated `EXCEPTIONS |= {(ENTRY_TYPE, "found_run")}` in memory and the full '
                  '`self_test()` still exited 0 with "all 34 fixtures hold"',
            fix="require EXCEPTION_CHECKS.keys() == EXCEPTIONS",
        )
        R43_15 = F(  # PR #43 round 15 — the AST scanner that proves "no raw response escapes a scanned
                     # reader" fails to notice a response wrapped in a dict. The proof machinery had become
                     # the thing under review. It also attacks a DECLARED NON-GOAL.
            file="scripts/ci-status.py", line="1019", writer="dev-time", purpose="-",
            repro="a fetcher returning `{\"raw\": data}` or `identity(data)` is not detected by "
                  "`is_raw_response()`; both shapes were accepted as clean",
            fix="follow the value through dict/list literals and single-argument helpers",
        )
        self.R43_11, self.R42_23, self.R43_15 = R43_11, R42_23, R43_15

        self.FINDING_CASES: "dict[str, tuple]" = {
            # --- THE ACCEPTANCE TEST: the real record, classified -------------------------------------
            "real-43-r11-gates": (
                PLAN, WORKED, [R43_11], INTENT, NS, OK, "1 gating finding(s)",
                "**PR #43 ROUND 11, AND IF THE RULE LOSES THIS ONE THE RULE IS WRONG.** A paginated reader "
                "that an EARLIER FIX ROUND had added treated a missing row array as empty and produced a "
                "FALSE GREEN — from a real GitHub response. It is round-added, and it GATES: `writer=network` "
                "names an actor who can really send that reply, and it defends the PR's stated purpose "
                "verbatim. A false green is the exact thing that PR exists to prevent"),
            "real-42-r23-non-gating": (
                PLAN, WORKED, [R42_23], INTENT, SAT, OK, "0 gating finding(s)",
                "**PR #42 ROUND 23 — THE LAST ROUND, and a human had to stop the loop.** The self-test's own "
                "EXCEPTIONS table is not itself bounded. TRUE, reproduced, concrete — and it anchors to "
                "NOTHING: no line of the PR's purpose is served by fixing it, and the only actor who can "
                "reach it is a developer editing the source. NON-GATING: recorded as a follow-up, no fix "
                "dispatched, the review moves on"),
            "real-43-r15-non-gating": (
                PLAN, WORKED, [R43_15], INTENT, SAT, OK, "0 gating finding(s)",
                "**PR #43 ROUND 15** — the AST scanner that proves the OTHER guard misses a response wrapped "
                "in a dict. The proof machinery has become the thing under review. Nobody can write that "
                "input, it serves no stated purpose, and it attacks a DECLARED NON-GOAL. NON-GATING"),
            "real-42-r23-cannot-gate": (
                PLAN, WORKED, [R42_23], INTENT, NS, UNUSABLE, "NO GATING finding",
                "…AND THE SAME FINDING CANNOT BE TURNED INTO A BLOCK BY RETURNING NOT SATISFIED. This is "
                "where the loop is actually broken: the reviewer may still REPORT it, and the pass is "
                "REFUSED if it tries to gate on it. The tool can only ever SUBTRACT a pass — it never "
                "converts this into a SATISFIED, because a tool that could ACCEPT would merge a PR nobody "
                "reviewed"),

            # --- the verdict/findings coherence rule --------------------------------------------------
            "not-satisfied-no-findings": (
                PLAN, WORKED, [], INTENT, NS, UNUSABLE, "NO GATING finding",
                "NOT SATISFIED with no findings recorded AT ALL — a verdict that blocks a PR and names "
                "nothing that blocks it. Nobody downstream can act on it and nobody can check it"),
            "not-satisfied-with-gating": (
                PLAN, WORKED, [R43_11], INTENT, NS, OK, "1 gating finding(s)",
                "…and the shape that IS allowed to block: one gating finding, and the pass counts as a real "
                "NOT SATISFIED"),
            "satisfied-with-gating": (
                PLAN, WORKED, [R43_11], INTENT, SAT, UNUSABLE, "GATING finding(s) STAND",
                "**THE OTHER HALF OF THE IF AND ONLY IF, AND ONLY ONE HALF WAS EVER ENFORCED.** The contract "
                "is 'NOT SATISFIED exactly when at least one GATING finding stands'. A pass that RECORDS the "
                "round-11 false-green finding — round-added, `writer=network`, quoting the PR's purpose "
                "verbatim — and then returns SATISFIED verified `ok`, and the gate merged a PR over a defect "
                "its own reviewer had just written down. The reviewer decided this finding gates when it "
                "chose that `writer` and that `purpose`; the verdict may not then ignore it"),
            "satisfied-with-non-gating-is-fine": (
                PLAN, WORKED, [R42_23], INTENT, SAT, OK, "0 gating finding(s)",
                "…and the case that half must NOT catch: a SATISFIED pass carrying a NON-GATING finding is "
                "the shape the whole design is FOR. The finding is recorded, it becomes a follow-up, and it "
                "does not block. A rule that refused this would forbid the reviewer to report anything it "
                "was not willing to block on — which is the 21-round spiral, re-armed"),
            "complete-pass-no-verdict": (
                PLAN, WORKED, [R42_23], INTENT, None, UNUSABLE, "no verdict was given",
                "**THE COHERENCE RULE'S OWN INPUT COULD BE ABSENT, AND THEN THE RULE NEVER FIRED.** This "
                "exact pass — COMPLETE, sound, verified with NO verdict — used to come back `ok`, so a "
                "driver that simply FORGOT the flag switched off the one machine-checked rule about the "
                "reviewer's own verdict: a SATISFIED returned over a GATING finding sailed straight through "
                "it. It is the same defect as an intent that could be missing on precisely the passes that "
                "MERGE a PR, and it is closed the same way — the input is DEMANDED, not hoped for. A gate "
                "must not depend on an agent remembering to pass something"),
            "in-flight-no-verdict": (
                PLAN, [ident(), started("u01")], None, INTENT, None, INCOMPLETE, "has not covered its plan",
                "…and the case the rule above must NOT catch, which is why it sits BELOW the completeness "
                "check: a pass still WORKING has no verdict to state, and it is answered `incomplete` — not "
                "refused for lacking one. `verify` is a door you come to with the report in hand; a pass in "
                "flight is WATCHED, not verified"),

            # --- `deferred` is NOT a verdict: it routes to the progress-file state --------------------
            #
            # A reviewer that raised a separate request the orchestrator must handle first — a
            # `plan_amendment_request`, or a broken-dispatch stop — ends its report with `VERDICT: DEFERRED`
            # and the orchestrator passes `--verdict deferred`. That value NEVER reaches the coherence rule;
            # the progress file is authoritative, and `decide` answers amended / incomplete / unusable.
            "deferred-with-amendment": (
                PLAN, [ident(), started("u01"), done("u01"), amendment(), started("u02"), done("u02")],
                [], INTENT, DEF, AMENDED, "not yet ruled on",
                "**THE CASE THE MARKER EXISTS FOR.** The reviewer raised a `plan_amendment_request` and ended "
                "`VERDICT: DEFERRED` instead of ruling. `deferred` is not weighed against anything — the "
                "unruled amendment is found FIRST and returns `amended`, exactly as it would with no verdict "
                "at all. The orchestrator folds the amendment and re-runs the pass"),
            "deferred-nothing-outstanding": (
                PLAN, WORKED, [], INTENT, DEF, UNUSABLE, "nothing to defer to",
                "**THE SPURIOUS DEFERRAL.** Every planned unit is done and no `plan_amendment_request` is "
                "outstanding, so a `deferred` here points at NOTHING — there is no request for the "
                "orchestrator to handle first. A deferral must name what it defers to; this pass is FINISHED "
                "and owes a binary verdict, so it is refused"),
            "deferred-incomplete": (
                PLAN, [ident(), started("u01")], [], INTENT, DEF, INCOMPLETE, "has not covered its plan",
                "…and a `deferred` on a pass STILL WORKING is answered by the completeness check, not the "
                "deferral rule: a broken-dispatch stop before the plan is covered reads as `incomplete`, "
                "which relaunches. `deferred` changed nothing about which state the progress file is in"),

            # --- A PASS IS JUDGED AGAINST AN INTENT — WHATEVER IT FOUND, AND EVEN IF IT FOUND NOTHING ----
            #
            # THE HOLE THESE FOUR CLOSE: the intent used to be loaded only where a FINDING needed anchoring.
            # A pass with no findings never went there, so nothing ever asked whether the intent existed —
            # and a SATISFIED pass with no findings is the ORDINARY case, the one that MERGES A PR. The
            # guard's input could simply be ABSENT on precisely the passes that count.
            "satisfied-no-findings-file-no-intent": (
                PLAN, WORKED, None, None, SAT, UNUSABLE, "THE RUN SKIPPED A STEP",
                "**THE HOLE, IN ITS EXACT SHAPE: no findings file AT ALL, no intent file AT ALL, verdict "
                "SATISFIED — and it verified `ok`.** `load_findings` returns `[]` for an absent artifact and "
                "never reaches `load_intent`, so the one input the entire gate rests on was never even "
                "looked for. This pass MERGES the PR, and it was measured against nothing"),
            "satisfied-empty-findings-file-no-intent": (
                PLAN, WORKED, [], None, SAT, UNUSABLE, "THE RUN SKIPPED A STEP",
                "…and the same hole one byte over: the findings file EXISTS and is EMPTY. `check_findings_"
                "file` returns early on zero records — correctly, there is nothing to anchor — and the "
                "intent went unchecked through that door too. ABSENT and EMPTY are different bytes and the "
                "same defect"),
            "satisfied-no-findings-file-with-intent": (
                PLAN, WORKED, None, INTENT, SAT, OK, "0 gating finding(s)",
                "…and what must STILL pass, or the fix is a regression: a SATISFIED pass that found nothing, "
                "on a PR that HAS an intent. 'Finding nothing is a fine and common result' is the reviewer's "
                "own contract, and an absent findings file is zero findings, not a defect. What it is not is "
                "a licence to skip the intent"),
            "incomplete-no-intent": (
                PLAN, [ident(), started("u01")], None, None, None, UNUSABLE, "THE RUN SKIPPED A STEP",
                "the pass is still WORKING (u01 started, nothing done) and its PR has no intent — and it is "
                "refused for the INTENT, not merely reported `incomplete`. A run that dispatched a reviewer "
                "with nothing to measure it against is broken from the first wake, and the earliest verdict "
                "that can say so is the one that should"),

            # --- the finding's own shape -------------------------------------------------------------
            "finding-bad-writer": (
                PLAN, WORKED, [F(writer="attacker")], INTENT, NS, UNUSABLE, "CLOSED enum",
                "`writer` outside the enum. It is not a new kind of actor — it is a field nobody filled in, "
                "and the gating rule reads it"),
            "finding-invented-purpose": (
                PLAN, WORKED, [F(purpose="never emit a false green anywhere")], INTENT, NS, UNUSABLE,
                "NOT a line of this PR's",
                "**THE ANCHOR IS A FACT, NOT A CLAIM.** A purpose the reviewer PARAPHRASES is a purpose the "
                "reviewer WROTE. Only a VERBATIM line of the PR's `## Purpose` block validates, so a finding "
                "cannot manufacture the justification for its own block"),
            "finding-missing-field": (
                PLAN, WORKED, [F(fix=DROP)], INTENT, NS, UNUSABLE, "carries EXACTLY",
                "a finding with no `fix` — and `fix` is the field a fix subagent is DISPATCHED with"),
            "finding-blank-repro": (
                PLAN, WORKED, [F(repro="   ")], INTENT, NS, UNUSABLE, "nothing behind it",
                "a blank repro: a claim, not a demonstrated defect"),
            "finding-line-not-a-line": (
                PLAN, WORKED, [F(line="0")], INTENT, NS, UNUSABLE, "a decimal number from 1 up",
                "there is no line 0 — `line` is where a human and a fix subagent are both sent to look"),
            "finding-line-not-string": (
                PLAN, WORKED, [F(line=421)], INTENT, NS, UNUSABLE, "not a string",
                "a JSON number where every value in these artifacts is a string"),
            "finding-unknown-type": (
                PLAN, WORKED, [F(type="note")], INTENT, NS, UNUSABLE, "only 'finding' records",
                "a line of a type nothing reads, in the file the gating rule is computed from"),
            "finding-extra-key": (
                PLAN, WORKED, [F(severity="high")], INTENT, NS, UNUSABLE, "unexpected key",
                "**A `severity` FIELD IS EXACTLY WHAT THIS DESIGN REFUSES.** A severity adjective with no "
                "mechanical definition is the current failure mode in a new costume — the reviewer already "
                "HAD a bar, and every finding cleared it honestly. Nothing reads this key, so whatever it "
                "asserts is neither verified nor refuted"),

            # --- the writer/repro cross-check --------------------------------------------------------
            "writer-contradicts-repro": (
                PLAN, WORKED, [F(writer="network", purpose="-",
                                 repro='I mutated `EXCEPTIONS |= {(ENTRY_TYPE, "found_run")}` in memory '
                                       'and the full `self_test()` still exited 0')],
                INTENT, NS, UNUSABLE, "EDIT TO THE SOURCE UNDER REVIEW",
                "**THE SOFT JOINT, HARDENED WHERE IT MATTERS.** `writer` is the one thing the reviewer "
                "judges, so a reviewer could re-arm the whole spiral by typing `network` on the EXCEPTIONS "
                "finding. Its own REPRO gives it away: 'I mutated … in memory' describes a developer with a "
                "text editor. This is the REAL #42 r23 repro with a false writer, and it is REFUSED"),
            "dev-time-repro-is-fine": (
                PLAN, WORKED, [R42_23], INTENT, SAT, OK, "0 gating finding(s)",
                "…and the same repro with the HONEST writer is accepted without complaint. The check refuses "
                "a contradiction; it does not refuse the finding"),

            # --- the intent artifact itself ----------------------------------------------------------
            "intent-missing": (
                PLAN, WORKED, [R43_11], None, NS, UNUSABLE, "THE RUN SKIPPED A STEP",
                "**A MISSING INTENT IS NOT AN EMPTY INTENT.** A finding cannot be anchored to a document "
                "that is not there, and the alternative — treat every `purpose` as unverifiable and wave it "
                "through — hands the reviewer a field it can write anything into. Adoption writes this file "
                "before the first review pass is ever dispatched"),
            "intent-no-threat-model": (
                PLAN, WORKED, [R43_11],
                "## Purpose\n- never emit a false green\n\n## Non-goals\n- nothing\n", NS, UNUSABLE,
                "Threat model",
                "an intent with no `## Threat model` — the section that BOUNDS the adversarial sweep. Two of "
                "three sections is not a weaker intent; it is one the reviewer cannot be measured against"),
            "intent-empty-purpose": (
                PLAN, WORKED, [R43_11],
                "## Purpose\n\n## Non-goals\n- nothing\n\n## Threat model\n- Who can write: nobody\n",
                NS, UNUSABLE, "no bullets",
                "`## Purpose` with no lines: every finding would then anchor to `-` BY FORCE, and a guard "
                "whose input can be ABSENT never fires"),
            "intent-empty-threat-model": (
                PLAN, WORKED, [R43_11],
                "## Purpose\n- never emit a false green\n\n## Non-goals\n- nothing\n\n## Threat model\n",
                NS, UNUSABLE, "no bullets",
                "**THE GUARD, INSIDE OUT.** `## Threat model` with the heading present and NOT ONE ACTOR "
                "under it. A finding gates by naming an actor who can really write the bad input — so with "
                "no actor named, NOTHING can anchor to one, and REAL, REACHABLE defects get discharged as "
                "non-gating. It is the mirror image of the bug this whole block exists to fix, and a guard "
                "whose input can be EMPTY never fires"),
            "intent-empty-non-goals-is-fine": (
                PLAN, WORKED, [R43_11],
                "## Purpose\n- never emit a false green\n\n## Non-goals\n\n## Threat model\n"
                "- Who can write the inputs this code reads: GitHub's API over the network\n",
                NS, OK, "1 gating finding(s)",
                "…and `## Non-goals` with NO bullets is ACCEPTED, deliberately. 'We exclude nothing' is a "
                "complete answer and the one that makes the review HARDEST — nothing is off-limits — so an "
                "empty one can never weaken a pass. The two ANCHORS must say something; the exclusions may "
                "say nothing"),
            "intent-two-purposes": (
                PLAN, WORKED, [R43_11],
                INTENT + "\n## Purpose\n- something else entirely\n", NS, UNUSABLE, "appears TWICE",
                "two `## Purpose` blocks are two intents, and a finding quoting one of them is anchored to a "
                "document that says two things"),
            "intent-purpose-is-sentinel": (
                PLAN, WORKED, [F(writer="hand-edit", purpose="-")],
                "## Purpose\n- -\n\n## Non-goals\n- nothing\n\n## Threat model\n"
                "- Who can write the inputs this code reads: a human hand-editing a git-ignored file\n",
                SAT, UNUSABLE, "is the SENTINEL",
                "**A SENTINEL THAT IS ALSO DATA.** `NO_PURPOSE` is `-`, and a `## Purpose` bullet of `-` is "
                "that exact string typed in as a real purpose line. It passed the empty-purpose check "
                "(the block HAS a bullet), so the intent parsed — and then a finding quoting the `-` purpose "
                "VERBATIM carried `purpose == '-'`, which `gating()` reads as 'anchors to no purpose' and "
                "discharges. A REAL, anchored finding would be waved through as non-gating. The write door "
                "now REFUSES the `-` bullet, so real purpose lines and the absent-marker can never collide"),

            # --- the findings file's own line shape --------------------------------------------------
            "findings-not-json": (
                PLAN, WORKED, ["not a finding at all"], INTENT, NS, UNUSABLE, "is not JSON",
                "a corrupt line in the artifact the GATING RULE is computed from"),
            "findings-blank-line": (
                PLAN, WORKED, [R43_11, ""], INTENT, NS, UNUSABLE, "is blank",
                "JSONL has no blank lines — here as anywhere else"),
        }

        # --- the WRITE doors ---------------------------------------------------------------------
        EMPTY: "list[str]" = []
        DISPATCHED = [ident()]                       # what the orchestrator leaves behind, before the launch
        BEGUN = [ident(), started("u01")]            # once the reviewer has ANNOUNCED u01
        FINISHED = [ident(), started("u01"), done("u01")]   # …and once it has already FINISHED u01
        self.EMPTY, self.DISPATCHED, self.BEGUN, self.FINISHED = EMPTY, DISPATCHED, BEGUN, FINISHED

        self.CLI_CASES = [
            (["emit", "--unit", "u01", "--status", "started"], DISPATCHED, 0, '"status":"started"', "the call every reviewer prompt makes"),
            (["emit", "--unit", "u01", "--status", "done", "--evidence", "f.py:1"], BEGUN, 0, '"evidence":"f.py:1"',
             "…and its done form, on the file that HAS the matching `started` — the only file the done form was ever meant to be run against"),
            (["emit", "--unit", "u01", "--status", "started"], EMPTY, 1, "NO `pass_identity`",
             "HEADLINE, WRITE DOOR: THE FILE THIS TOOL WROTE AND WOULD NOT READ. `emit` on an EMPTY progress file exited 0 — it never looked for the identity — and `verify` then called that same file `unusable: NO pass_identity`. The reviewer was told its work landed, and the pass could not count"),
            (["emit", "--unit", "u01", "--status", "done", "--evidence", "somewhere else"], FINISHED, 1, "SECOND",
             "HEADLINE, WRITE DOOR: a SECOND `done` for a unit already finished. `verify` refused it on READ and this door WROTE it (exit 0) — the reviewer was handed a success and the pass was thrown away later for a defect the tool had just helped it commit"),
            (["emit", "--unit", "u02", "--status", "done", "--evidence", "f.py:1"], BEGUN, 1, "no earlier 'started'",
             "HEADLINE, WRITE DOOR: a `done` for a unit that was never begun. The write door refuses it at the moment the reviewer makes the mistake"),
            (["emit", "--unit", "u99", "--status", "done", "--evidence", "f.py:1"], DISPATCHED, 1, "NOT IN THE PLAN",
             "HEADLINE, WRITE DOOR: the tool accepted a self-granted unit. It no longer does — and it says UNPLANNED, not 'no started'"),
            (["emit", "--unit", "u99", "--status", "started"], DISPATCHED, 1, "NOT IN THE PLAN", "…and refuses to START one"),
            (["emit", "--unit", "u01", "--status", "done"], DISPATCHED, 1, "carries EXACTLY", "a done with no evidence — the SAME key rule a hand-written line meets"),
            (["emit", "--unit", "u01", "--status", "done", "--evidence", "  "], BEGUN, 1, "CONCRETE evidence", "…and blank evidence, on a file where the `started` is not the problem"),
            (["emit", "--unit", "u01", "--status", "started", "--evidence", "x"], DISPATCHED, 1, "carries EXACTLY", "a started carrying evidence: the mirror of a done without it"),
            (["emit", "--unit", "  ", "--status", "started"], DISPATCHED, 1, "The emit door does NOT strip it",
             "a blank unit id. It is refused for what it IS — not an id — and not for what it is not"),
            (["emit", "--unit", " u01 ", "--status", "started"], DISPATCHED, 1, "The emit door does NOT strip it",
             "HEADLINE, WRITE DOOR: THE FINDING. `plan-add --id ' u01 '` used to exit 0 while this door silently STRIPPED the padding — so the plan held a unit whose progress could never be recorded"),
            (["emit", "--unit", "u02", "--status", "started"], [ident(), '{"type":"progress","unit_id":"u01","status":"done","evidence":"x"}'], 1, "carries EXACTLY",
             "the file it is APPENDING TO is evidence too: a hand-written line already in it makes the pass unusable"),
            (["identity", "--head-sha", SHA, "--dispatched-at", TS], EMPTY, 0, '"launch_attempt":"1"',
             "the line that was a `printf` — pr/pass/attempt now come from the FILENAME"),
            (["identity", "--head-sha", SHA[:7], "--dispatched-at", TS], EMPTY, 1, "escaped into this repo's real state",
             "HEADLINE, WRITE DOOR: the truncated sha that got written into a real pass_identity"),
            (["identity", "--head-sha", SHA.upper(), "--dispatched-at", TS], EMPTY, 1, "LOWERCASE",
             "an UPPERCASE sha: no producer of ours emits one, so it did not come from `git rev-parse`"),
            (["identity", "--head-sha", SHA, "--dispatched-at", "just now"], EMPTY, 1, "LAUNCH DEADLINE's clock",
             "a dispatch clock the launch deadline cannot be measured from"),
            (["identity", "--head-sha", SHA, "--dispatched-at", "2026-99-99T99:99:99Z"], EMPTY, 1, "not a real UTC time",
             "…and the one the SHAPE rule cannot see: an impossible date in the right shape"),
            (["identity", "--head-sha", OTHER_SHA, "--dispatched-at", TS], [ident()], 1, "NOT EMPTY",
             "a SECOND identity into a live pass's file — how one pass ends up describing two commits"),
            (["identity", "--head-sha", SHA, "--dispatched-at", TS], [""], 1, "NOT EMPTY",
             "HEADLINE, WRITE DOOR: a WHITESPACE-ONLY file. This door decided 'empty' with `.strip()`, wrote the identity below the blank line, exited 0 — and `verify` then refused the artifact FOR THAT BLANK LINE. EMPTY now means NO BYTES, at both doors"),
            (["verify", "--head-sha", SHA[:7], "--verdict", "satisfied"], EMPTY, 2, "No verdict beats a wrong one",
             "an OPERATOR error is not a snapshot verdict: exit 2"),
            (["verify", "--head-sha", SHA, "--amendments-ruled", "1", "--verdict", "satisfied"], EMPTY, 2, "raised only 0",
             "a ruling for an amendment that does not exist would silently clear the NEXT one raised"),
            (["verify", "--head-sha", SHA, "--amendments-ruled", "-1", "--verdict", "satisfied"], WORKED, 2,
             "smallest legal value is 0",
             "HEADLINE: A NEGATIVE RULING WEDGES A PASS THAT WAS EARNED. `decide` SUBTRACTS the ruling, so `0 - (-1) = 1` amendment 'not yet ruled on' that the reviewer never raised and no ruling can ever clear"),
            (["verify", "--head-sha", SHA], WORKED, 2, "required: --verdict",
             "**THE VERIFY DOOR'S OWN MISSING INPUT, REFUSED BY ARGPARSE — AT THE DOOR, NAMING THE FLAG.** This exact call exited 0 on this exact pass: the flag was OPTIONAL, so the coherence rule was OFF for a driver that forgot it, and the gate then merged whatever the report claimed. It is `required=True` now — the same cure `--check` needed one door over, for the same disease: a guard whose input can be ABSENT never fires"),
            (["verify", "--head-sha", SHA, "--verdict", "not-satisfied"], WORKED, 1, "NO GATING finding",
             "**THE NEW ONE, AT THE VERIFY DOOR:** a complete, perfectly sound pass that returns NOT SATISFIED and records nothing that may block. The artifacts are flawless and the pass still cannot count — a verdict that blocks a PR must name what blocks it"),
            (["verify", "--head-sha", SHA, "--verdict", "satisfied"], WORKED, 0, "0 gating finding(s)",
             "…and the same pass returning SATISFIED, which needs no finding at all"),
            (["verify", "--head-sha", SHA, "--verdict", "deferred"], WORKED, 1, "nothing to defer to",
             "**THE THIRD `--verdict` VALUE, AT THE DOOR.** argparse ACCEPTS `deferred` (it is a "
             "`VERDICT_CHOICES` member, not an exit-2 rejection) and routes it to the progress file. On this "
             "complete pass with no amendment there is nothing to defer to, so it comes back UNUSABLE — "
             "proving the flag parses AND that `deferred` is never silently treated as a passing verdict"),
        ]

        # `plan-add` and `finding-add` get their own families: their flags do not fit the shape above (a
        # repeatable `--check`; a seven-flag finding), and the ARTIFACT'S NAME is under test too.
        self.PLAN_CLI_CASES = [
            (PLAN_FILE, ["--id", "u03", "--kind", "cross-cutting", "--target", "both doors", "--check", "a", "--check", "b"],
             0, '"checks":["a","b"]', "the plan stops being a shell heredoc"),
            (PLAN_FILE, ["--id", "u01", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "duplicate unit id",
             "a duplicate id — refused by the SAME statement `load_plan` refuses it with"),
            (PLAN_FILE, ["--id", "  ", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID", "a blank id"),
            (PLAN_FILE, ["--id", " u01 ", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID",
             "HEADLINE, PLAN DOOR: THE FINDING. This exited 0, and the id it wrote was one `emit` could never match"),
            (PLAN_FILE, ["--id", "U01", "--kind", "file", "--target", "x.py", "--check", "a"], 1, "NOT AN ID",
             "…and an id that is merely a different SPELLING of a legal one. There is no such thing"),
            (PLAN_FILE, ["--id", "u03", "--kind", "file", "--target", "x.py"], 2,
             "the following arguments are required: --check",
             "HEADLINE, THE HELP DOOR: **THIS IS THE COMMAND `plan-add --help` ADVERTISED.** `--check` was OPTIONAL to argparse — the usage line BRACKETED it — and the write path then refused that exact call. It is `required=True` now"),
            (PLAN_FILE, ["--id", "u03", "--kind", "file", "--target", "x.py", "--check", "  "], 1, "not a unit",
             "…and the check argparse CANNOT make: a `--check` that is present and BLANK"),
            ("plan.jsonl", ["--id", "u03", "--kind", "file", "--target", "x.py", "--check", "a"], 1,
             "not a plan artifact's name",
             "the plan's name was enforced at the READ door BY CONSTRUCTION and at the write door NOT AT ALL: this wrote a valid plan to a name nothing will ever open"),
        ]

        FIND_OK = ["--path", "scripts/ci-status.py", "--line", "769", "--writer", "network",
                   "--purpose", PURPOSE_GREEN, "--repro", "a paginated reply with no `statuses` member",
                   "--fix", "refuse a missing row array"]
        self.FINDING_CLI_CASES = [
            (FINDINGS_FILE, FIND_OK, 0, '"writer":"network"',
             "the call the reviewer prompt makes — a finding that DEFENDS a stated purpose and names a real actor"),
            (FINDINGS_FILE, FIND_OK, 0, "# GATING:",
             "**AND THE TOOL SAYS SO, AT THE WRITE DOOR.** The same call, and what the reviewer is TOLD: "
             "this finding ANCHORS, so it BLOCKS, so the verdict must be NOT SATISFIED while it stands. The "
             "NON-GATING notice below has always existed so a follow-up could not become a block by "
             "accident; this is its mirror, and the direction that actually merged a PR over a recorded "
             "defect — `verify` refuses that pass, and this is where the reviewer learns the rule instead "
             "of losing the pass to it fifteen minutes later"),
            (FINDINGS_FILE, ["--path", "scripts/followups.py", "--line", "1815", "--writer", "dev-time",
                             "--purpose", "-", "--repro", "I mutated EXCEPTIONS in memory and self_test() still exited 0",
                             "--fix", "bound the exception table"],
             0, "NON-GATING",
             "**THE SPIRAL FINDING, RECORDED AND DISCHARGED.** It is WRITTEN — this is not censorship, and the reviewer is not told to stop looking. The tool tells it, on stdout, that the finding anchors to nothing and MUST NOT produce NOT SATISFIED. It becomes a follow-up"),
            (FINDINGS_FILE, [*FIND_OK[:6], "--purpose", "stop false greens", *FIND_OK[8:]], 1,
             "NOT a line of this PR's",
             "a PARAPHRASED purpose. The anchor is checked against the intent VERBATIM, so a reviewer cannot invent the justification for its own block — and it is checked HERE, while the reviewer can still fix the call"),
            (FINDINGS_FILE, [*FIND_OK[:4], "--writer", "attacker", *FIND_OK[6:]], 2,
             "invalid choice",
             "a writer outside the CLOSED enum — refused by ARGPARSE, at the door, naming the flag"),
            (FINDINGS_FILE, [*FIND_OK[:4], "--writer", "network", "--purpose", "-",
                             "--repro", "I mutated the table in memory", *FIND_OK[10:]], 1,
             "EDIT TO THE SOURCE UNDER REVIEW",
             "the writer/repro contradiction at the WRITE door: a repro that says 'I mutated … in memory' while claiming a real-world writer. It fails SAFE — it can refuse a pass, never demote a finding"),
            (FINDINGS_FILE, ["--path", "x.py", "--line", "0", *FIND_OK[4:]], 1, "a decimal number from 1 up",
             "there is no line 0"),
            ("findings.jsonl", FIND_OK, 1, "not a findings artifact's name",
             "findings written under a name `verify` will never DERIVE are findings nothing reads — and the pass would then be refused for recording none while they sat on disk one filename away"),
        ]

        # --- the ROUND TRIP ----------------------------------------------------------------------
        self.FILE_STATES: "dict[str, bytes | None]" = {
            "absent": None,
            "empty": b"",
            "whitespace-only": b"   \n",
            "blank-line": b"\n",
            "identified": (ident() + "\n").encode(),
            "begun": (ident() + "\n" + started("u01") + "\n").encode(),
            "planned": (unit("u01") + "\n").encode(),
            "found": (R43_11 + "\n").encode(),
            # THE CONCATENATION. The last line has NO trailing newline, so the next append lands ON it and
            # fuses two records into one line that is not JSON. Every record-level check passes — the record
            # was never the problem — and only the bytes `before + line` can show it.
            "no-trailing-newline": ident().encode(),
            "plan-no-trailing-newline": unit("u01").encode(),
            "findings-no-trailing-newline": R43_11.encode(),
            "corrupt": b"not json at all\n",
            "not-utf8": b"\xff\n",
        }

        self.WRITE_COMMANDS: "dict[str, tuple[str, list[str]]]" = {
            "emit": (PROGRESS_FILE, ["--unit", "u01", "--status", R.STARTED]),
            "identity": (PROGRESS_FILE, ["--head-sha", SHA, "--dispatched-at", TS]),
            "plan-add": (PLAN_FILE, ["--id", "u09", "--kind", "file", "--target", "x.py", "--check", "a"]),
            "finding-add": (FINDINGS_FILE, FIND_OK),
        }
        # `status` writes NOTHING — it is an ADVISORY read-only view — so the round trip does not drive it
        # (there is no produced artifact to read back), and it is declared read-only here so the
        # command-coverage check is satisfied the day the subcommand is added.
        self.READ_ONLY_COMMANDS = frozenset({"intent-check", "verify", "self-test", "status"})

        # --- the DOORS ---------------------------------------------------------------------------
        self.DOOR_SEEDS: "dict[str, tuple[str | None, Sequence[str] | None]]" = {
            "emit": (PROGRESS_FILE, DISPATCHED),        # the reviewer's door: the identity is already there
            WRAPPER_DOOR: (PROGRESS_FILE, DISPATCHED),  # …and the same door, through the wrapper it runs
            "identity": (PROGRESS_FILE, None),          # it writes into a file that must hold NO BYTES
            "plan-add": (PLAN_FILE, None),              # the first unit lands in a plan that does not exist
            "finding-add": (FINDINGS_FILE, None),       # …and the first finding in a findings file that does not
            FINDING_WRAPPER_DOOR: (FINDINGS_FILE, None),  # the reviewer's OTHER door, through its wrapper
            "intent-check": (INTENT_FILE, INTENT.splitlines()),
            # a COMPLETE, sound pass — and the minimal invocation now CARRIES a `--verdict` (it is required),
            # so the door check drives the rule as well as the shape: `satisfied`, 0 gating findings, exit 0
            "verify": (PROGRESS_FILE, WORKED),
            # status takes `--run`, not `--file`, and writes nothing — its minimal advertised invocation is
            # `status --run .`, which globs the cwd, finds no passes, and exits 0. So it needs no seed file.
            "status": (None, None),
            "self-test": (None, None),                  # no --file, no flags at all
        }

        self.FLAG_VALUES: "dict[str, list[str]]" = {
            "--unit": ["u01"], "--status": [R.STARTED], "--evidence": ["f.py:1"],
            "--head-sha": [SHA], "--dispatched-at": [TS],
            "--id": ["u09"], "--kind": ["file"], "--target": ["x.py"], "--check": ["a"],
            "--amendments-ruled": ["0"], "--verdict": [R.SATISFIED],
            "--path": ["scripts/ci-status.py"], "--line": ["769"], "--writer": ["network"],
            "--purpose": [PURPOSE_GREEN], "--repro": ["a reply with no rows"], "--fix": ["refuse it"],
            # status's view flags. `--run .` drives the minimal invocation the door check executes; the
            # OPTIONAL flags are never in a minimal call, so their values are only here to satisfy the
            # "every advertised flag has a supplied value" reconciliation. `--verify`/`--history` are
            # store_true, so their value list is empty and never iterated.
            "--run": ["."], "--pr": ["41"], "--ledger": ["state.jsonl"], "--now": [TS],
            "--verify": [], "--history": [],
        }

        # --- the DOMAINS -------------------------------------------------------------------------
        def probe_id(name: str) -> "Callable[[object], None]":
            return lambda value: R.check_id(name, value, "[domain]")

        NAME_TEMPLATES = {"pr": "review-{v}-1.progress.jsonl",
                          "pass": "review-41-{v}.progress.jsonl",
                          "attempt": "review-41-1.a{v}.progress.jsonl"}

        def probe_name(field: str) -> "Callable[[object], None]":
            def probe(value: object) -> None:
                R.parse_name(Path(NAME_TEMPLATES[field].format(v=value)))
            return probe

        def probe_ruled(value: object) -> None:
            if not isinstance(value, int):
                raise R.OperatorError(f"[domain] --amendments-ruled {value!r} is not an integer — the "
                                      f"parser's `type=int` refuses it before the domain is ever reached")
            R.check_ruled(value)

        self.DOMAINS: "dict[str, tuple[Callable[[object], None], str]]" = {
            **{name: (probe_id(name), spec) for name, (_, spec, _) in R.ID_FORMATS.items()},  # drops regex, why
            "filename pr": (probe_name("pr"), "a decimal number from 1 up, as the progress file's NAME carries it"),
            "filename pass": (probe_name("pass"), "a decimal number from 1 up, as the NAME carries it"),
            "filename attempt": (probe_name("attempt"),
                                 "the `a<k>` suffix: a decimal integer from 2 UP, no leading zeros"),
            "--amendments-ruled": (probe_ruled,
                                   "a CARDINALITY: an integer from 0 up, and never more than the pass raised"),
        }

        self.BOUNDARY_CASES: "list[tuple[str, object, bool]]" = [
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

            # **THE FINDING'S CITATION.** `line` is an identifier by this tool's own definition — a value two
            # doors compare — so it goes through the ONE validator, and its domain is fenced on both sides
            # like every other. There is no line 0, and a citation nobody can open is not one.
            ("line", "1", True), ("line", "421", True), ("line", "1815", True),
            ("line", "0", False), ("line", "0421", False), ("line", "-1", False), ("line", "4 21", False),
            ("line", " 421", False), ("line", "421 ", False), ("line", "421\n", False),
            ("line", "", False), ("line", "many", False), ("line", 421, False), ("line", None, False),

            # The FILENAME's numbers — the same domains, at the door that reads the NAME rather than the bytes.
            ("filename pr", "1", True), ("filename pr", "41", True), ("filename pr", "10", True),
            ("filename pr", "0", False), ("filename pr", "041", False), ("filename pr", "", False),
            ("filename pr", "-1", False), ("filename pr", "1 ", False),
            ("filename pass", "1", True), ("filename pass", "2", True), ("filename pass", "10", True),
            ("filename pass", "0", False), ("filename pass", "01", False), ("filename pass", "", False),

            # **THE ATTEMPT SUFFIX — THE BOUNDARY NOBODY STOOD ON.** `a2`…`a9` and `a20` were accepted and
            # `a10`…`a19` were REFUSED, so both edges of the hole are pinned here (9/10 and 19/20).
            ("filename attempt", "2", True), ("filename attempt", "3", True), ("filename attempt", "9", True),
            ("filename attempt", "10", True), ("filename attempt", "11", True), ("filename attempt", "19", True),
            ("filename attempt", "20", True), ("filename attempt", "99", True), ("filename attempt", "100", True),
            ("filename attempt", "1", False), ("filename attempt", "0", False),
            ("filename attempt", "02", False), ("filename attempt", "010", False),
            ("filename attempt", "", False), ("filename attempt", "-2", False), ("filename attempt", "+2", False),
            ("filename attempt", "2 ", False), ("filename attempt", " 2", False), ("filename attempt", "2a", False),

            # **`--amendments-ruled` — A CARDINALITY, and 0 is INSIDE it.** `-1` is the wedge.
            ("--amendments-ruled", 0, True), ("--amendments-ruled", 1, True), ("--amendments-ruled", 7, True),
            ("--amendments-ruled", -1, False), ("--amendments-ruled", -2, False),
        ]

        # --- THE STATUS FAMILY: the ADVISORY render, pinned by its printed bytes ---------------------
        #
        # `status` is READ-ONLY and DECIDES NOTHING, so — unlike the gate rules above — it carries no
        # `# MUTATE` markers and is not in the mutation matrix. What it CAN get wrong is the RENDER, so
        # every case seeds a synthetic rundir (plan + progress [+ findings / report / ledger]), runs
        # `status --run <tmp>`, and asserts the PRINTED CELLS of the row(s) — the bytes, not internal
        # state — exactly as `ledger-test.py`'s `grid()` re-parses the printed table. A deterministic
        # `--now` seam fixes `elapsed`/`health` without the wall clock, and the STALLED case seeds the
        # progress file's mtime with `os.utime` (the liveness clock the design's gap 1 is about).
        #
        # Each case: {files, now, [flags], [mtimes], [expect], [absent], why}. `files` maps a filename to
        # its content — a list of JSONL lines, a raw str (a torn tail or a report), or bytes. `expect` maps
        # a rendered `pass` label to the cell values that row must show; `absent` lists labels that must
        # NOT appear. `mtimes` maps a filename to the UTC time `os.utime` stamps on it.
        TORN = ident() + "\n" + started("u01") + "\n" + done("u01") + "\n" + \
            '{"type":"progress","unit":"u02","status":"star'   # a half-written append, NO trailing newline
        UNREADABLE = "this is not valid json at all\n"   # a REAL corruption (has a newline; not a torn tail)
        PLAN3 = [unit("u01"), unit("u02", target="b.py", checks=["c"]),
                 unit("u03", target="c.py", checks=["c"]), unit("u04", target="d.py", checks=["c"])]
        NG1 = finding(writer="dev-time", purpose="-",
                      repro="I mutated it in memory", fix="bound it")
        NG2 = finding(writer="hand-edit", purpose="-",
                      repro="hand-edit a git-ignored file", fix="guard it")
        PLAN_HEADER = '{"type":"plan","pr":"41","pass":"1","units":2}'

        def ident7(**over: Value) -> str:
            return ident(pr="7", **over)

        self.STATUS_CASES: "dict[str, dict]" = {
            "launching": {
                "files": {PROGRESS_FILE: [ident()], PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"units": "0/2", "now": "-", "find": "0/0",
                                    "elapsed": "3m", "health": "launching", "verdict": "-"}},
                "why": "a fresh dispatch (identity only), 3 min in: no launch evidence yet and inside the "
                       "~5-min deadline, so it is `launching`, not an alarm",
            },
            "no-launch": {
                "files": {PROGRESS_FILE: [ident()], PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:10:00Z",
                "expect": {"41-1": {"units": "0/2", "health": "NO-LAUNCH!", "elapsed": "10m"}},
                "why": "identity only, PAST the ~5-min launch deadline — a likely failed launch, flagged "
                       "for attention",
            },
            "working-now-unit": {
                "files": {PROGRESS_FILE: [ident(), started("u01"), done("u01"), started("u02")],
                          PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"units": "1/2", "now": "u02", "health": "working", "verdict": "-"}},
                "why": "u02 started with no matching done — `now` reads the plan's own unit id, and the "
                       "pass is `working`",
            },
            "three-done": {
                "files": {PROGRESS_FILE: [ident(), started("u01"), done("u01"), started("u02"),
                                          done("u02", evidence="x:1"), started("u03"),
                                          done("u03", evidence="x:2")],
                          PLAN_FILE: PLAN3},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"units": "3/4", "now": "-", "health": "working"}},
                "why": "3 of 4 planned units done — the tolerant tally counts `done` events against the "
                       "plan's unit count",
            },
            "amend-outranks-liveness": {
                "files": {PROGRESS_FILE: [ident(), amendment()], PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:10:00Z",
                "expect": {"41-1": {"health": "AMEND(1)", "elapsed": "10m"}},
                "why": "an amendment IS launch evidence and the more actionable fact, so `AMEND(1)` "
                       "outranks the past-deadline NO-LAUNCH! this elapsed would otherwise show",
            },
            "done-verdict": {
                "files": {PROGRESS_FILE: self.WORKED, PLAN_FILE: self.PLAN,
                          "review-41-1.txt": "Report body.\nVERDICT: NOT SATISFIED\n"},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],   # `done` is TERMINAL, so the default hides it; --history shows it
                "expect": {"41-1": {"units": "2/2", "health": "done", "verdict": "NOT-SAT"}},
                "why": "the report `.txt` carries a VERDICT line, so health is `done` (terminal, hidden by "
                       "default) and the verdict is scraped as NOT-SAT (NOT SATISFIED is tested before "
                       "SATISFIED, which it contains)",
            },
            "find-gating-split": {
                "files": {PROGRESS_FILE: self.WORKED, PLAN_FILE: self.PLAN,
                          FINDINGS_FILE: [finding(), NG1, NG2]},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"find": "1/2", "health": "working"}},
                "why": "one gating (network / defends a purpose line) and two non-gating findings, "
                       "classified by the ONE `gating()` predicate — `find = 1/2`",
            },
            "torn-last-line": {
                "files": {PROGRESS_FILE: TORN, PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"units": "1/2", "now": "-", "health": "working"}},
                "why": "the file is mid-append (a torn trailing line with no newline). `status` truncates "
                       "at the last newline, so the torn u02 `started` is ignored and the prior u01 done "
                       "still counts — one bad tail must not blank the row",
            },
            "active-attempt-only": {
                "files": {"review-7-1.progress.jsonl": [ident7()],
                          "review-7-1.a2.progress.jsonl": [ident7(launch_attempt="2"), started("u01")],
                          "review-7-1.plan.jsonl": self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"7-1.a2": {"units": "0/2", "now": "u01", "health": "working"}},
                "absent": ["7-1"],
                "why": "both attempt 1 and attempt 2 exist on disk; the default view collapses to the "
                       "highest launch_attempt, so only `7-1.a2` renders and the dead `7-1` is suppressed",
            },
            "plan-header-tolerated": {
                "files": {PROGRESS_FILE: self.WORKED,
                          PLAN_FILE: [PLAN_HEADER, unit("u01"),
                                      unit("u02", target="b.py", checks=["c"])]},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"41-1": {"units": "2/2", "health": "working"}},
                "why": "a plan with a leading `{\"type\":\"plan\",...}` header (the design's gap 2): "
                       "`total` counts only the `unit` records, so the header is ignored and the render "
                       "does not crash — which is WHY `status` does not reuse the strict `load_plan`",
            },
            "stalled-by-mtime": {
                "files": {PROGRESS_FILE: [ident(), started("u01"), done("u01"), started("u02")],
                          PLAN_FILE: self.PLAN},
                "mtimes": {PROGRESS_FILE: "2026-07-06T00:00:00Z"},
                "now": "2026-07-06T00:20:00Z",
                "expect": {"41-1": {"units": "1/2", "now": "u02", "health": "STALLED", "elapsed": "20m"}},
                "why": "launch evidence is present but the progress file's mtime is 20 min old, past the "
                       "~15-min meaningful-progress deadline — STALLED. The mtime is the liveness clock "
                       "because progress events carry no timestamp (the design's gap 1)",
            },
            "verify-column": {
                "files": {PROGRESS_FILE: self.WORKED, PLAN_FILE: self.PLAN, INTENT_FILE: INTENT,
                          "review-41-1.txt": "VERDICT: SATISFIED\n"},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--verify", "--history"],   # `done` is terminal — --history reveals it
                "expect": {"41-1": {"units": "2/2", "verdict": "SAT", "counts(--verify)": "ok"}},
                "why": "the opt-in `--verify` column runs the AUTHORITATIVE `evaluate()` verdict verbatim "
                       "(complete, sound, SATISFIED, zero gating findings) — `ok`, distinct from the "
                       "advisory tally",
            },

            # --- TERMINAL vs LIVE: a pass whose reviewer is GONE must never render live-looking ---------
            "superseded-gone": {
                "files": {"review-41-1.progress.jsonl": [ident(), started("u01"), done("u01"),
                                                         started("u02")],
                          "review-41-2.progress.jsonl": [ident(**{"pass": "2"}), started("u01")],
                          PLAN_FILE: self.PLAN},
                "mtimes": {"review-41-1.progress.jsonl": "2026-07-06T00:00:00Z"},
                "now": "2026-07-06T00:20:00Z",
                "flags": ["--history"],
                "expect": {"41-1": {"units": "1/2", "health": "gone", "verdict": "-"},
                           "41-2": {"health": "working"}},
                "why": "pass 1 has launch evidence AND a 20-min-stale mtime — but pass 2 exists, so pass 1 "
                       "was SUPERSEDED and its reviewer is gone: `gone`, NEVER the `STALLED` this mtime "
                       "alone would show. Only the current pass 2 stays live (`working`)",
            },
            "superseded-hidden-by-default": {
                "files": {"review-41-1.progress.jsonl": [ident(), started("u01"), done("u01"),
                                                         started("u02")],
                          "review-41-2.progress.jsonl": [ident(**{"pass": "2"}), started("u01")],
                          PLAN_FILE: self.PLAN},
                "mtimes": {"review-41-1.progress.jsonl": "2026-07-06T00:00:00Z"},
                "now": "2026-07-06T00:20:00Z",
                "expect": {"41-2": {"health": "working"}},
                "absent": ["41-1"],
                "why": "the SAME run in the DEFAULT view: the superseded (gone) pass 1 is a terminal pass, "
                       "so it is HIDDEN and the table shows only the in-flight pass 2. Nothing is dropped "
                       "silently — a footer counts what was hidden and `--history` shows it",
            },
            "relaunched-attempt-gone": {
                "files": {"review-7-1.progress.jsonl": [ident7(), started("u01")],
                          "review-7-1.a2.progress.jsonl": [ident7(launch_attempt="2"), started("u01")],
                          "review-7-1.plan.jsonl": self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],
                "expect": {"7-1": {"health": "gone"},
                           "7-1.a2": {"health": "working"}},
                "why": "attempt 1 was RELAUNCHED — attempt 2 (`a2`) exists for the same (pr, pass) — so its "
                       "reviewer is gone: attempt 1 reads `gone`, never live, while the active attempt 2 "
                       "stays `working`",
            },

            # --- A TORN/CORRUPT progress file does not hide a pass's TERMINALITY (read from OTHER files) --
            "unreadable-done": {
                "files": {PROGRESS_FILE: UNREADABLE, PLAN_FILE: self.PLAN,
                          "review-41-1.txt": "Report body.\nVERDICT: SATISFIED\n"},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],   # `done` is terminal — --history reveals the row
                "expect": {"41-1": {"units": "?", "health": "done", "verdict": "SAT"}},
                "why": "the PROGRESS file is a real corruption, but its REPORT carries a VERDICT: the pass "
                       "FINISHED. The verdict is scraped and `done` wins BEFORE the unreadable give-up, so "
                       "the row reads `done`/`SAT`, never a live-looking `unreadable`",
            },
            "unreadable-done-hidden": {
                "files": {PROGRESS_FILE: UNREADABLE, PLAN_FILE: self.PLAN,
                          "review-41-1.txt": "Report body.\nVERDICT: SATISFIED\n"},
                "now": "2026-07-06T00:03:00Z",
                "absent": ["41-1"],
                "footer": "1 terminal pass(es) hidden",
                "why": "the SAME corrupt-but-finished pass in the DEFAULT view: `done` is terminal, so it is "
                       "HIDDEN and the footer counts it — a torn progress file no longer forces a dead pass "
                       "to show as in-flight",
            },
            "unreadable-superseded-gone": {
                "files": {"review-41-1.progress.jsonl": UNREADABLE,
                          "review-41-2.progress.jsonl": [ident(**{"pass": "2"}), started("u01")],
                          PLAN_FILE: self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],
                "expect": {"41-1": {"health": "gone", "verdict": "-"},
                           "41-2": {"health": "working"}},
                "why": "pass 1's progress file is corrupt AND pass 2 exists, so pass 1 was SUPERSEDED — its "
                       "reviewer is gone. `gone` is honoured before the unreadable give-up, so it reads "
                       "`gone` (no verdict), never `unreadable`; the current pass 2 stays `working`",
            },
            "relaunched-footer": {
                "files": {"review-7-1.progress.jsonl": [ident7(), started("u01")],
                          "review-7-1.a2.progress.jsonl": [ident7(launch_attempt="2"), started("u01")],
                          "review-7-1.plan.jsonl": self.PLAN},
                "now": "2026-07-06T00:03:00Z",
                "expect": {"7-1.a2": {"health": "working"}},
                "absent": ["7-1"],
                "footer": "1 terminal pass(es) hidden",
                "why": "the DEFAULT view of a RELAUNCHED pass: the superseded attempt 1 (`gone`) is hidden AND "
                       "COUNTED in the footer — it is a terminal launch attempt, not silently dropped, even "
                       "though only the active attempt 2 renders",
            },
        }


# --- the CROSS-DOOR property ----------------------------------------------------------------------
#
# The round trip asks "can the tool read back what it wrote?" — ONE artifact, one command. This asks the
# question one artifact over, and it is the one the tool got wrong: **the plan door and the emit door must
# agree about what a unit id IS.** They did not. `plan-add --id ' u01 '` exited 0; `emit --unit ' u01 '`
# then failed with `NOT IN THE PLAN` and printed `Planned: [' u01 ']`. The plan held a unit whose progress
# could never be recorded — a review that could never complete.
#
#   **THE PLAN DOOR REFUSES THE ID, OR THE EMIT DOOR CAN MATCH IT.** Never "planned, and unnameable".

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

HOLDS, VIOLATED = "holds", "VIOLATED"
FALSE_PASS, VERDICT_KILL, MESSAGE_KILL, CRASH_KILL = "FALSE-PASS", "VERDICT", "MESSAGE", "CRASH"

# The outcomes that mean "this passed": a mutant that turns a failing case into one of these has produced
# the loudest possible failure — the weakened tool says "ship it" about artifacts that are defective.
PASSING = ("ok", "exit0")

# The functions that ENFORCE the contract. Every enforcement point inside them must carry a marker.
#
# `evaluate` is NOT one, and it is the interesting exclusion: it RAISES nothing and REFUSES nothing — it
# composes the read side and maps whatever a rule raised onto a verdict. (Its `return UNUSABLE, str(exc)` is
# that MAPPING, not a rule; listing `evaluate` here would demand a marker on it and mutate the mapping
# itself, which pins nothing.) It does carry ONE marker — on the CALL that loads the intent for every pass —
# and that is exactly what a marker is for: the mutation harness reads markers from the WHOLE source, so a
# rule enforced by MAKING A CALL is mutated (the call is deleted) and must still be killed by a fixture. It
# is `unmarked` below, not this tuple, that is scoped to the functions which refuse.
RULE_FUNCTIONS = (
    "hook", "read_text", "parse_lines", "read_lines", "check_id", "check_unit", "plan_units", "load_plan",
    "check_event", "check_progress", "walk_progress", "check_identity_shape", "check_identity",
    "check_head", "check_progress_file", "check_plan_file", "decide", "parse_name", "check_ruled",
    "before_text", "write_line", "cmd_emit", "cmd_identity", "cmd_plan_add", "cmd_verify",
    # …and the FINDINGS side: the intent, the anchor, the writer, and the artifact they live in.
    "parse_intent", "load_intent", "check_writer_repro", "check_finding", "findings_name",
    "check_findings_file", "load_findings", "cmd_finding_add",
)
ENFORCING_EXCEPTIONS = ("Defect", "OperatorError")
# The NAMES as they are spelled in the source, because that is what the AST holds — `return UNUSABLE, …`
# parses to an `ast.Name` whose `id` is "UNUSABLE", never to the string "unusable" it evaluates to.
# `return OK` is the ABSENCE of a rule, so it is not here.
ENFORCING_VERDICT_NAMES = ("INCOMPLETE", "AMENDED", "UNUSABLE")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


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


def write_intent(d: Path, text: "str | None" = INTENT) -> None:
    """The PR's intent, beside the pass's artifacts — exactly where adoption leaves it."""
    if text is not None:
        (d / INTENT_FILE).write_text(text, encoding="utf-8")


def build(tmp: Path, name: str, plan: "list[str] | None", progress: "list[str] | bytes",
          findings: "list[str] | None" = None, intent: "str | None" = INTENT) -> Path:
    """Write a fixture pass to disk RAW — bypassing every write-side check, because half these fixtures
    hold exactly what the write side would have refused. That is the point: the READ side must catch them
    without being told how they got there. (`progress` as BYTES is how a fixture holds what is not text.)

    **THE INTENT SITS BESIDE EVERY FIXTURE UNLESS ONE SAYS OTHERWISE, AND THAT DEFAULT IS THE CONTRACT.**
    `evaluate` judges EVERY pass against an intent block — a pass that found nothing is measured against
    one exactly as a pass that found ten is — so a rundir without one is not a neutral fixture, it is a
    rundir with a defect. Every case that is not ABOUT the intent gets a sound one, so that what the read
    side says is about the thing the case is testing; a case that wants it absent, empty or malformed
    passes it explicitly (`intent=None`, or the broken text itself).
    """
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / PROGRESS_FILE
    if isinstance(progress, bytes):
        path.write_bytes(progress)
    else:
        path.write_text("".join(line + "\n" for line in progress), encoding="utf-8")
    if plan is not None:
        (d / PLAN_FILE).write_text("".join(line + "\n" for line in plan), encoding="utf-8")
    if findings is not None:
        (d / FINDINGS_FILE).write_text("".join(line + "\n" for line in findings), encoding="utf-8")
    write_intent(d, intent)
    return path


def reads_back(mod: types.ModuleType, artifact: str, path: Path) -> "tuple[bool, str]":
    """The READ side's answer about a file a write just produced: CAN IT BE READ BACK?

    It calls the (possibly mutated) module's OWN read side — never this one's — because the question is
    always "would THIS tool read back what THIS tool wrote?", and a mutant is a tool with a rule removed.

    An exception is the loudest failure of all: the read side owes a VERDICT on any bytes, and a crash is
    not a verdict.

    It asks for NO `--verdict`, and the question is why that is sound: a write door has just appended ONE
    line, so the pass it produced is never a COMPLETE one (`WRITE_COMMANDS` never finishes the plan), and
    the missing-verdict rule fires only on a complete pass. "Reads back" here means "not `unusable`" —
    `incomplete` is the honest answer about a pass that is one `emit` old, and it holds.
    """
    try:
        if artifact == PLAN_FILE:
            mod.load_plan(path)
            return True, "the plan reads back"
        if artifact == FINDINGS_FILE:
            mod.check_findings_file(path.read_text(encoding="utf-8"), path)
            return True, "the findings read back"
        verdict, reason = mod.evaluate(path, SHA)
        return verdict != mod.UNUSABLE, f"{verdict}: {reason}"
    except Exception as exc:  # noqa: BLE001 - a crash on READ is a violation, not an error to propagate
        return False, f"crash:{type(exc).__name__}: {exc}"


def round_trip(mod: types.ModuleType, T: Tables, tmp: Path) -> "dict[str, tuple[str, str]]":
    """EVERY write command x EVERY pre-existing file state: does the property hold on each?

    `holds` = the command REFUSED (any non-zero exit), or it wrote and the result READS BACK.
    `VIOLATED` = it exited 0 and produced an artifact its own read side will not read.
    """
    got: dict[str, tuple[str, str]] = {}
    for cmd, (artifact, argv) in T.WRITE_COMMANDS.items():
        for state, content in T.FILE_STATES.items():
            d = tmp / f"rt-{cmd}-{state}"
            d.mkdir(parents=True, exist_ok=True)
            # A sound plan and a sound intent sit beside every case, so that what the read side says about
            # the produced file is about THAT file and nothing else.
            (d / PLAN_FILE).write_text("".join(line + "\n" for line in T.PLAN), encoding="utf-8")
            write_intent(d)
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
            got[key] = ((HOLDS if ok else VIOLATED),
                        f"exit 0, and the file it produced reads back as -> {why}")
    return got


def cross_door(mod: types.ModuleType, tmp: Path) -> "dict[str, tuple[str, str]]":
    """`plan-add --id X`, then `emit --unit X` — the SAME string, through both doors, for each X."""
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
            got[key] = ((HOLDS if code == 0 else VIOLATED),
                        f"the plan door PLANNED it, and the emit door exited {code}: {text.strip()}")
        except Exception as exc:  # noqa: BLE001 - a crash at either door is a violation, not an error
            got[key] = (f"crash:{type(exc).__name__}", str(exc))
    return got


def cli_key(i: int, argv: "list[str]") -> str:
    """The case's key. The INDEX is in it because the SEED is part of the case and the argv is not."""
    return f"[cli {i}] {' '.join(argv)}"


def find_key(i: int, name: str) -> str:
    return f"[finding-cli {i}] {name}"


def run_cases(mod: types.ModuleType, T: Tables, tmp: Path) -> "dict[str, tuple[str, str]]":
    """Every fixture, every name case, every CLI case, the findings family, and the two properties —
    against this (possibly mutated) module.

    A mutant that CRASHES has not returned a verdict, and "no verdict" is itself a deviation — recorded,
    never swallowed."""
    got: dict[str, tuple[str, str]] = {}
    # **EVERY CASE STATES A VERDICT, and that default is the contract** — exactly as the intent sits beside
    # every fixture unless one says otherwise. A COMPLETE pass verified with NO verdict is `unusable` (the
    # coherence rule may not be switched off by omitting its input), so a case that is not ABOUT the verdict
    # must supply one or it would be asserting the wrong refusal. These cases carry no findings file, so
    # `satisfied` is the verdict that coheres; the cases that ARE about the verdict live in `FINDING_CASES`,
    # which passes its own — including `None`.
    for name, (plan, progress, _, _, _) in T.CASES.items():  # drops want, needle, why
        path = build(tmp, f"case-{name}", plan, progress)
        try:
            got[name] = mod.evaluate(path, SHA, 0, mod.SATISFIED)
        except Exception as exc:  # noqa: BLE001 - a crash IS the result here
            got[name] = (f"crash:{type(exc).__name__}", str(exc))
    for name, (plan, progress, findings, intent, verdict, _, _, _) in T.FINDING_CASES.items():  # drops want, needle, why
        path = build(tmp, f"find-{name}", plan, progress, findings, intent)
        try:
            got[f"[finding] {name}"] = mod.evaluate(path, SHA, 0, verdict)
        except Exception as exc:  # noqa: BLE001
            got[f"[finding] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (name, _, _, _) in enumerate(T.NAME_CASES):  # drops want, needle, why
        d = build(tmp, f"name-{i}", T.PLAN, T.WORKED).parent
        path = d / name
        path.write_text("".join(line + "\n" for line in T.WORKED), encoding="utf-8")
        try:
            # The pass is COMPLETE (`WORKED`) in every one of these, so it states a verdict for the same
            # reason `CASES` does — the FILENAME is what is under test here, and nothing else may refuse it.
            got[f"[name] {name}"] = mod.evaluate(path, SHA, 0, mod.SATISFIED)
        except Exception as exc:  # noqa: BLE001
            got[f"[name] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (argv, seed, _, _, _) in enumerate(T.CLI_CASES):  # drops want, needle, why
        path = build(tmp, f"cli-{i}", T.PLAN, seed)
        try:
            code, text = run_cli(mod, [argv[0], "--file", str(path), *argv[1:]])
            got[cli_key(i, argv)] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[cli_key(i, argv)] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (pname, argv, _, _, _) in enumerate(T.PLAN_CLI_CASES):  # drops want, needle, why
        plan = build(tmp, f"plan-cli-{i}", T.PLAN, []).parent / pname
        try:
            code, text = run_cli(mod, ["plan-add", "--file", str(plan), *argv])
            got[f"[plan] {pname} {' '.join(argv)}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[plan] {pname} {' '.join(argv)}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (fname, argv, _, _, _) in enumerate(T.FINDING_CLI_CASES):  # drops want, needle, why
        d = build(tmp, f"find-cli-{i}", T.PLAN, T.DISPATCHED, None, INTENT).parent
        try:
            code, text = run_cli(mod, ["finding-add", "--file", str(d / fname), *argv])
            got[find_key(i, fname)] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[find_key(i, fname)] = (f"crash:{type(exc).__name__}", str(exc))
    got.update(round_trip(mod, T, tmp))
    got.update(cross_door(mod, tmp))
    return got


def expectations(T: Tables) -> "dict[str, tuple[str, str, str]]":
    """case -> (expected outcome, needle its output must contain, why the case exists)."""
    out = {n: (w, needle, why) for n, (_, _, w, needle, why) in T.CASES.items()}  # drops plan, progress
    out.update({f"[finding] {n}": (w, needle, why)
                for n, (_, _, _, _, _, w, needle, why) in T.FINDING_CASES.items()})  # drops plan, progress, findings, intent, verdict
    out.update({f"[name] {n}": (w, needle, why) for n, w, needle, why in T.NAME_CASES})
    out.update({cli_key(i, a): (f"exit{c}", needle, why)
                for i, (a, _, c, needle, why) in enumerate(T.CLI_CASES)})  # drops seed
    out.update({f"[plan] {p} {' '.join(a)}": (f"exit{c}", needle, why)
                for p, a, c, needle, why in T.PLAN_CLI_CASES})
    out.update({find_key(i, p): (f"exit{c}", needle, why)
                for i, (p, _, c, needle, why) in enumerate(T.FINDING_CLI_CASES)})  # drops argv
    # The two PROPERTIES. Their expectation IS the property and not a particular rule — demanding a needle
    # would be demanding a specific defect where the case only demands a sound outcome.
    out.update({f"[round-trip] {cmd} on a {state} file": (
        HOLDS, "", f"`{cmd}` against a {state} target: it must FAIL, or the file it wrote must READ BACK")
        for cmd in T.WRITE_COMMANDS for state in T.FILE_STATES})
    out.update({f"[cross-door] the id {uid!r}": (
        HOLDS, "",
        f"`plan-add --id {uid!r}` then `emit --unit {uid!r}`: the PLAN door must refuse the id, or the "
        f"EMIT door must be able to name the unit it planned")
        for uid in CROSS_DOOR_IDS.values()})
    return out


# --- EVERY BOUNDED VALUE, probed JUST INSIDE and JUST OUTSIDE its declared domain -----------------

def check_boundaries(R: types.ModuleType, T: Tables) -> int:
    """Every bounded value, JUST INSIDE and JUST OUTSIDE its domain — and every domain probed on BOTH sides.

    Returns the failures. The second loop is the mechanical part: it is what a bug like `a10` has to get
    past, and it cannot — a domain nobody fenced on both sides is reported as unfenced, by name.
    """
    failures = 0
    sides: dict[str, set[bool]] = {}
    for name, value, accepted in T.BOUNDARY_CASES:
        sides.setdefault(name, set()).add(accepted)
        if name not in T.DOMAINS:
            continue
        probe, spec = T.DOMAINS[name]
        try:
            probe(value)
            got = True
        except (R.Defect, R.OperatorError):
            got = False
        if got == accepted:
            print(f"ok       [domain] `{name}` {'accepts' if accepted else 'REFUSES'} {value!r}")
        else:
            print(f"FAIL     [domain] `{name}` {'REFUSED' if accepted else 'ACCEPTED'} {value!r} — its "
                  f"domain is {spec}, and the BOUNDARY is where two doors come to disagree about what a "
                  f"value IS. `a10` was refused by a pattern whose own error message said `k >= 2`")
            failures += 1

    for name, (_, spec) in T.DOMAINS.items():  # drops the probe callable
        probed = sides.get(name, set())
        if probed == {True, False}:
            continue
        gap = ("NO CASES AT ALL" if not probed else
               "no case INSIDE it" if True not in probed else "no case OUTSIDE it")
        print(f"FAIL     [domain] `{name}` ({spec}) has {gap} — a domain is fenced only when the suite "
              f"stands on BOTH sides of its boundary. An unprobed side is what `a10` and `-1` cost")
        failures += 1

    stray = sorted(set(sides) - set(T.DOMAINS))
    if stray:
        print(f"FAIL     [domain] cases for a value with no declared domain: {stray} — a domain is "
              f"DECLARED in `ID_FORMATS`/`DOMAINS` or it is not a domain at all")
        failures += 1
    return failures


# --- the DOCS' examples, fed through the tool ----------------------------------------------------
#
# The doc is what a reviewer actually follows. **A documented example the tool REFUSES is not a typo — it
# is a trap that makes correct behavior impossible**: the `plan_amendment_request` example omitted
# `"type":"unit"` from its `proposed_unit`, the verifier REQUIRES that key, so a reviewer who copied the
# documented shape produced a pass the tool then called `unusable`, with nothing telling it why.

def doc_examples(R: types.ModuleType) -> "list[tuple[str, int, dict]]":
    """(file, line, record) for every JSON example in the docs that claims one of this tool's types."""
    # The campaign skill (SKILL.md + references/), from the OWNER's own location — not from `R.__file__`,
    # which a module built by the mutation harness sets to a synthetic name and which the type system
    # correctly says may be `None`.
    docs = OWNER.parent.parent
    types_ = {R.UNIT, R.PROGRESS, R.AMENDMENT, R.IDENTITY, R.FINDING}
    found: list[tuple[str, int, dict]] = []
    for md in sorted(docs.rglob("*.md")):
        for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.strip()
            if not text.startswith("{") or not text.endswith("}"):
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                continue  # a JSON-SHAPED line that is not JSON is some other doc's prose, not an example
            if isinstance(rec, dict) and rec.get("type") in types_:
                found.append((str(md.relative_to(docs)), n, rec))
    return found


def check_docs(R: types.ModuleType) -> int:
    """Every documented example, through the tool. Returns the number that the tool would REFUSE."""
    examples = doc_examples(R)
    want = {R.UNIT, R.PROGRESS, R.AMENDMENT, R.IDENTITY, R.FINDING}
    failures = 0
    for where, n, rec in examples:
        try:
            if rec["type"] == R.UNIT:
                R.check_unit(rec, f"{where}:{n}")
            elif rec["type"] == R.FINDING:
                # The doc's finding example must anchor to the doc's OWN purpose lines — and the intent
                # block the docs show is the one this suite feeds it. A documented finding the tool would
                # refuse is a trap: the reviewer copies the shape and the pass goes `unusable`.
                R.check_finding(rec, f"{where}:{n}", R.parse_intent(INTENT, Path(INTENT_FILE))[R.PURPOSE_H])
            else:
                R.check_event(rec, f"{where}:{n}")
                if rec["type"] == R.IDENTITY:
                    R.check_identity_shape(rec, f"{where}:{n}")
            print(f"ok       {where}:{n:<4} {rec['type']:22} the tool accepts its own documented example")
        except R.Defect as exc:
            print(f"FAIL     {where}:{n:<4} the tool REFUSES its own documented example: {exc}")
            failures += 1
    seen = {rec["type"] for _, _, rec in examples}  # drops want, needle
    if seen != want:
        print(f"FAIL     the docs no longer show an example of every record type — missing "
              f"{sorted(want - seen)}. A scan that matches nothing passes every time and checks nothing; "
              f"these examples ARE the contract, so their absence is the failure")
        failures += 1
    return failures


# --- EVERY DOOR'S HELP: what it SAYS must be what the tool TAKES ---------------------------------
#
# `emit-progress.py --help` printed `usage: emit-progress.py emit [-h] --file …`, and running that exact
# command failed with `unrecognized arguments: emit`. **AND THE CURE FOR THAT HAD THE DISEASE IT WAS
# CURING**: the check written for it ran the WRAPPER and nothing else, so the next help/parser lie shipped
# straight underneath it — `plan-add --help` printed `[--check CHECK]` (argparse for OPTIONAL) while the
# write path refused that exact advertised command. **A check that cannot fire on the case that matters is
# not a check; it is a claim.**
#
# So every door is driven, and the DOOR LIST IS DERIVED from `build_parser()` — never hand-written.

def advertised(help_text: str) -> "tuple[list[str], set[str], set[str]]":
    """(the COMMAND WORDS a `--help` advertises, its REQUIRED flags, its OPTIONAL flags) — from the usage.

    **The BRACKETS are the claim under test.** argparse writes a required option bare (`--file FILE`) and an
    optional one in brackets (`[--check CHECK]`), so the usage line does not merely list the flags — it says
    which ones you may LEAVE OUT. That promise is what `plan-add` broke.
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
    every = set(re.findall(r"--[a-z][a-z-]*", usage))
    optional = {flag for group in re.findall(r"\[[^\[\]]*\]", usage)
                for flag in re.findall(r"--[a-z][a-z-]*", group)}
    return words, every - optional, optional


def door_parsers(R: types.ModuleType) -> "dict[str, argparse.ArgumentParser]":
    """Every door the tool has, and the parser behind it — DERIVED from `build_parser`, never listed.

    The two WRAPPERS are the doors that are separate SCRIPTS, so their parsers are rebuilt here from the
    owner's own `add_emit_args`/`add_finding_args` — the same single definitions the wrappers call. What is
    actually EXECUTED for them is the real script, as a subprocess, so a replica cannot hide a wrapper that
    has drifted from it.
    """
    p, _ = R.build_parser()  # drops the commands map
    doors: dict[str, argparse.ArgumentParser] = {}
    for action in p._actions:  # noqa: SLF001 - the subparser map is where the doors are
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            doors.update({str(name): sub for name, sub in choices.items()})
    emit_wrapper = argparse.ArgumentParser(prog=WRAPPER_DOOR)
    R.add_emit_args(emit_wrapper)
    doors[WRAPPER_DOOR] = emit_wrapper
    finding_wrapper = argparse.ArgumentParser(prog=FINDING_WRAPPER_DOOR)
    R.add_finding_args(finding_wrapper)
    doors[FINDING_WRAPPER_DOOR] = finding_wrapper
    return doors


def declared(p: argparse.ArgumentParser) -> "tuple[set[str], set[str]]":
    """(the flags a parser REQUIRES, the flags it accepts and does not require) — from the parser itself."""
    required: set[str] = set()
    optional: set[str] = set()
    for action in p._actions:  # noqa: SLF001 - the flags are the actions; there is no public view of them
        longs = {opt for opt in action.option_strings if opt.startswith("--")} - {"--help"}
        (required if action.required else optional).update(longs)
    return required, optional


def door_script(door: str) -> Path:
    if door == WRAPPER_DOOR:
        return WRAPPER
    if door == FINDING_WRAPPER_DOOR:
        return FINDING_WRAPPER
    return OWNER


def seed_door(T: Tables, tmp: Path, door: str, case: str) -> "list[str]":
    """A FRESH realistic pre-existing state for one probe of one door, and the `--file` argv naming it.

    Fresh per probe, never shared: the minimal invocation of `emit` APPENDS to the file it is given, and a
    later probe run against that mutated file would be probing something nobody declared.
    """
    artifact, lines = T.DOOR_SEEDS[door]
    if artifact is None:
        return []
    d = tmp / f"door-{door}-{case}"
    d.mkdir(parents=True, exist_ok=True)
    if artifact != PLAN_FILE:  # a sound plan sits beside every other door, as in a real rundir
        (d / PLAN_FILE).write_text("".join(line + "\n" for line in T.PLAN), encoding="utf-8")
    write_intent(d)            # …and the intent, which the finding doors must anchor against
    target = d / artifact
    if lines is not None:
        target.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return ["--file", str(target)]


def run_door(door: str, words: "list[str]", argv: "list[str]") -> "tuple[int, str]":
    """Run a door AS A CALLER DOES — a subprocess, with the command words its OWN help advertised.

    `words[1:]` is the load-bearing part: it is the subcommand the usage line CLAIMS, not the one we know it
    to be. `emit-progress.py --help` used to advertise `emit-progress.py emit …`, and running THAT is the
    only thing that could have caught it.
    """
    run = subprocess.run([sys.executable, str(door_script(door)), *words[1:], *argv],  # noqa: S603
                         capture_output=True, text=True, check=False,
                         env={**os.environ, DOOR_PROBE_ENV: "1"})
    return run.returncode, (run.stdout + run.stderr).strip()


def check_door(T: Tables, door: str, parser: argparse.ArgumentParser, tmp: Path) -> int:
    """One door: what its `--help` says, against what it TAKES. Returns the failures."""
    script = door_script(door)
    ask = [] if script != OWNER else [door]
    help_run = subprocess.run([sys.executable, str(script), *ask, "--help"],  # noqa: S603 - our own scripts
                              capture_output=True, text=True, check=False)
    if help_run.returncode != 0:
        print(f"FAIL     [door] `{door} --help` exited {help_run.returncode}: {help_run.stderr.strip()}")
        return 1
    words, required, optional = advertised(help_run.stdout)
    failures = 0

    declared_required, declared_optional = declared(parser)
    if (required, optional) != (declared_required, declared_optional):
        print(f"FAIL     [door] `{door}` ADVERTISES required {sorted(required)} / optional "
              f"{sorted(optional)}, and its parser DECLARES required {sorted(declared_required)} / optional "
              f"{sorted(declared_optional)} — the help is the door a reviewer READS, and a flag that is one "
              f"thing there and another in the parser is a command someone will type and be refused for")
        failures += 1
    else:
        print(f"ok       [door] `{door}` advertises exactly what its parser takes: required "
              f"{sorted(required)}, optional {sorted(optional)}")

    unsupplied = sorted((required | optional) - set(T.FLAG_VALUES) - {"--file"})
    if unsupplied:
        print(f"FAIL     [door] `{door}` advertises {unsupplied}, and `FLAG_VALUES` declares no value for "
              f"it — a flag nothing can supply is a door nothing can drive, and an undriven door is exactly "
              f"how `plan-add` came to refuse the command its own help advertised")
        return failures + 1

    def invoke(flags: "set[str]", case: str) -> "tuple[int, str]":
        argv: list[str] = []
        for flag in sorted(flags):
            if flag == "--file":
                argv += seed_door(T, tmp, door, case)
            else:
                argv += [arg for value in T.FLAG_VALUES[flag] for arg in (flag, value)]
        return run_door(door, words, argv)

    # THE WHOLE POINT: the MINIMAL command the help advertises — every flag it brackets left OUT — EXECUTED.
    shown = " ".join([script.name, *words[1:], *(f"{flag} …" for flag in sorted(required))]) or door
    code, text = invoke(required, "minimal")
    if code != 0:
        print(f"FAIL     [door] the tool REFUSES the command its own `--help` advertises — `{shown}` exited "
              f"{code}: {text}\n         Every flag left out is one the help BRACKETS as optional. This is "
              f"the help door and the WRITE door disagreeing about what the command IS")
        failures += 1
    else:
        print(f"ok       [door] the advertised MINIMAL invocation RUNS: `{shown}` -> exit 0")

    # The wrappers fix their owner's command internally. Passing that command word explicitly must remain
    # an error: accepting it would restore the old CLI that advertised a subcommand the wrapper did not need.
    owner_command = WRAPPER_OWNER_COMMANDS.get(door)
    if owner_command is not None:
        hidden_argv = [owner_command]
        for flag in sorted(required):
            if flag == "--file":
                hidden_argv += seed_door(T, tmp, door, "hidden-command")
            else:
                hidden_argv += [arg for value in T.FLAG_VALUES[flag] for arg in (flag, value)]
        hidden = subprocess.run(
            [sys.executable, str(script), *hidden_argv],
            capture_output=True,
            text=True,
            check=False,
        )
        if hidden.returncode == 0:
            print(f"FAIL     [door] `{door}` ACCEPTED its owner's hidden `{owner_command}` command word — "
                  "the wrapper's public CLI has grown an extra command layer")
            failures += 1
        else:
            print(f"ok       [door] `{door}` keeps its owner's `{owner_command}` command word hidden")

    # …and the other direction: a flag the help calls REQUIRED must really be refused when it is absent.
    for flag in sorted(required):
        code, text = invoke(required - {flag}, f"no{flag}")
        if code == 0:
            print(f"FAIL     [door] `{door}` advertises {flag} as REQUIRED and then ACCEPTED the call "
                  f"WITHOUT it (exit 0) — the same lie as an optional flag it refuses, with its sign "
                  f"flipped")
            failures += 1
    if required:
        print(f"ok       [door] `{door}` REFUSES the call with any of {sorted(required)} absent")
    return failures


def check_doors(R: types.ModuleType, T: Tables, tmp: Path) -> int:
    """EVERY door — the subcommands `build_parser` has AND both wrappers. Returns the failures.

    The reconciliation is the mechanical part, and it is why this cannot rot back into the wrapper-only
    check it replaces: the door list comes from the PARSER, so a door with no seed is REPORTED, by name,
    the day it is added — and a seed for a door that no longer exists is reported too.
    """
    failures = 0
    parsers = door_parsers(R)
    for door in sorted(set(parsers) | set(T.DOOR_SEEDS)):
        if door not in parsers:
            print(f"FAIL     [door] `{door}` has a seed in `DOOR_SEEDS` but the tool has no such door — a "
                  f"door that was renamed or removed leaves a check that probes NOTHING and passes")
            failures += 1
        elif door not in T.DOOR_SEEDS:
            print(f"FAIL     [door] the parser has a door `{door}` that NOTHING drives — declare in "
                  f"`DOOR_SEEDS` what its `--file` names and the bytes that file realistically holds. A "
                  f"door nothing drives is one that is free to advertise a command it refuses")
            failures += 1
        else:
            failures += check_door(T, door, parsers[door], tmp)
    return failures


# --- the MUTATION MATRIX: is any rule pinned by NOTHING? ------------------------------------------


def check_commands_covered(R: types.ModuleType, T: Tables) -> "list[str]":
    """Is EVERY subcommand the parser has either driven by the round trip or declared to write nothing?

    This is what makes the round trip's coverage DERIVED rather than claimed. A new subcommand appears in
    the parser and in neither set below — so the suite goes red the day it is added, and stays red until
    someone says which it is.
    """
    _, commands = R.build_parser()  # drops the parser
    problems = []
    for cmd in commands:
        if cmd not in T.WRITE_COMMANDS and cmd not in T.READ_ONLY_COMMANDS:
            problems.append(
                f"the parser has a subcommand `{cmd}` that the round trip does not drive. If it WRITES, "
                f"add it to WRITE_COMMANDS (the property must hold for it); if it writes nothing, add it "
                f"to READ_ONLY_COMMANDS. An undriven write path is one nothing has ever asked to read back"
            )
    for cmd in sorted(set(T.WRITE_COMMANDS) | set(T.READ_ONLY_COMMANDS)):
        if cmd not in commands:
            problems.append(f"`{cmd}` is driven by the round trip but the parser no longer has it")
    return problems


def check_intent_door(R: types.ModuleType, tmp: Path) -> int:
    """Drive intent-check through its real CLI with one sound and one malformed artifact."""
    cases = {
        "usable": (INTENT, 0, "usable intent block"),
        "missing-threat-model": (
            "## Purpose\n- do the work\n\n## Non-goals\n",
            1,
            "missing ['## Threat model']",
        ),
    }
    failures = 0
    for name, (content, want, needle) in cases.items():
        path = tmp / f"intent-door-{name}.md"
        path.write_text(content, encoding="utf-8")
        code, output = run_cli(R, ["intent-check", "--file", str(path)])
        if code != want or needle not in output:
            print(f"FAIL     [intent-check] {name}: exit {code}, expected {want}; output: {output.strip()}")
            failures += 1
        else:
            print(f"ok       [intent-check] {name:24} exit {code}: {needle}")
    return failures


def status_parse(out: str) -> "tuple[list[str], dict[str, dict[str, str]]]":
    """Parse `status`'s printed table BACK: (column names, {pass label -> {column -> cell}}).

    The layout is a run header line, a blank line, the column-header row, a dash rule row, then one row per
    pass — two-space gutters, every data line rstripped. No status cell carries an interior space, so
    splitting on whitespace recovers the cells exactly. Re-parsing the PRINTED BYTES (never internal state)
    is the same discipline `ledger-test.py`'s `grid()` uses.
    """
    lines = out.split("\n")
    rule_i = next((i for i, line in enumerate(lines) if line and set(line) <= {"-", " "}), None)
    if rule_i is None or rule_i < 1:
        raise SelfTestFailure(f"status output has no dash rule line:\n{out}")
    columns = lines[rule_i - 1].split()
    rows: dict[str, dict[str, str]] = {}
    for line in lines[rule_i + 1:]:
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split()
        rows[cells[0]] = dict(zip(columns, cells))
    return columns, rows


def run_status_cases(mod: types.ModuleType, T: Tables, tmp: Path) -> int:
    """The ADVISORY render family: seed a synthetic rundir per case, run `status`, assert the PRINTED cells.

    **FAILS LOUDLY IF THE FAMILY IS MISSING** — a check that cannot find the thing it checks must fail,
    never pass; that is the founding rule of this whole suite (`load_test_module`)."""
    if not getattr(T, "STATUS_CASES", None):
        print("FAIL     [status] the STATUS_CASES fixture family is MISSING or EMPTY — `status` would "
              "render unpinned, and a check with no subject must FAIL, never report success")
        return 1
    failures = 0
    for name, case in T.STATUS_CASES.items():
        d = tmp / f"status-{name}"
        d.mkdir(parents=True, exist_ok=True)
        for fname, content in case["files"].items():
            path = d / fname
            if isinstance(content, bytes):
                path.write_bytes(content)
            elif isinstance(content, str):
                path.write_text(content, encoding="utf-8")   # a torn tail or a report body
            else:
                path.write_text("".join(line + "\n" for line in content), encoding="utf-8")
        for fname, ts in case.get("mtimes", {}).items():
            epoch = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            os.utime(d / fname, (epoch, epoch))
        argv = ["status", "--run", str(d), "--now", case["now"], *case.get("flags", [])]
        code, out = run_cli(mod, argv)
        if code != 0:
            print(f"FAIL     [status] {name}: `status` exited {code}\n{out}")
            failures += 1
            continue
        try:
            _, rows = status_parse(out)  # drops the header cols
        except SelfTestFailure as exc:
            # A view whose every pass is hidden prints a header + footer and NO table (no dash rule line).
            # That is legitimate for an `absent`/`footer`-only case; only a case that expects rendered rows
            # needs a table to parse.
            if case.get("expect"):
                print(f"FAIL     [status] {name}: {exc}")
                failures += 1
                continue
            rows = {}
        ok = True
        for label, want in case.get("expect", {}).items():
            got = rows.get(label)
            if got is None:
                print(f"FAIL     [status] {name}: no rendered row for {label!r}\n{out}")
                ok = False
                continue
            for col, val in want.items():
                if got.get(col) != val:
                    print(f"FAIL     [status] {name}: row {label} column {col!r} is {got.get(col)!r}, "
                          f"expected {val!r}\n{out}")
                    ok = False
        for label in case.get("absent", []):
            if label in rows:
                print(f"FAIL     [status] {name}: {label!r} must be suppressed but it rendered\n{out}")
                ok = False
        # `footer` pins the hidden-terminal count line (a `#` line status_parse skips as non-row): a
        # substring that MUST appear verbatim in the printed output, or `""` to assert NO footer at all.
        footer = case.get("footer")
        if footer is not None:
            has_footer = "terminal pass(es) hidden" in out
            if footer == "" and has_footer:
                print(f"FAIL     [status] {name}: expected NO hidden-count footer, but one printed\n{out}")
                ok = False
            elif footer and footer not in out:
                print(f"FAIL     [status] {name}: hidden-count footer {footer!r} not in output\n{out}")
                ok = False
        if ok:
            print(f"ok       [status] {name:24} {case['why'][:58]}")
        else:
            failures += 1
    return failures


def run(R: types.ModuleType, tmp: Path) -> int:
    """Every family, then the mutation matrix. Non-zero on any failure.

    `R` is the ALREADY-LOADED `review-pass.py` module — handed in by its `self-test`, so the tool under test
    is loaded exactly once and the code these fixtures drive is the code that command would run.
    """
    source = OWNER.read_text(encoding="utf-8")
    T = Tables(R)
    expect = expectations(T)
    failures = 0

    for problem in check_commands_covered(R, T):
        print(f"COMMANDS {problem}")
        failures += 1

    # The `self-test` door is EXECUTED like every other door — which means self-test runs self-test. The
    # nested run is the real door, doing its real work; it skips ONLY the door checks, which are the sole
    # thing that recurses. Everything else in it runs in full.
    probe = bool(os.environ.get(DOOR_PROBE_ENV))
    got = run_cases(R, T, tmp)
    if probe:
        door_failures = 0
        print("skip     [door] the door checks do not run inside the `self-test` door's own probe — "
              "this process IS that probe, and re-running them here would recurse forever")
    else:
        door_failures = check_doors(R, T, tmp)
    print()
    for case, (want, needle, why) in expect.items():
        outcome, text = got[case]
        if outcome == want and needle in text:
            print(f"ok       {case[:44]:44} -> {outcome:11} ({why[:60]})")
        elif outcome != want:
            print(f"FAIL     {case[:44]:44} -> {outcome:11} expected {want}\n         got: {text}")
            failures += 1
        else:
            # Right outcome, WRONG RULE. The message is the only thing that says which rule fired, and a
            # fixture that goes `unusable` for someone else's reason pins nothing.
            print(f"FAIL     {case[:44]:44} -> {outcome:11} but nothing mentions {needle!r}\n         got: {text}")
            failures += 1
    print()
    failures += door_failures
    failures += check_boundaries(R, T)
    print()
    failures += check_docs(R)
    print()
    failures += check_intent_door(R, tmp)
    print()
    failures += run_status_cases(R, T, tmp)
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the review-pass contract is broken.")
        return 1
    doors = ("the door checks were SKIPPED (this run is the `self-test` door's own probe)" if probe else
             f"every one of the {len(T.DOOR_SEEDS)} doors ({', '.join(sorted(T.DOOR_SEEDS))}) had the "
             f"MINIMAL invocation its OWN `--help` advertises EXECUTED, and it runs")
    print(f"all {len(T.CASES)} fixtures + {len(T.FINDING_CASES)} findings/intent fixtures + "
          f"{len(T.NAME_CASES)} name cases + "
          f"{len(T.CLI_CASES) + len(T.PLAN_CLI_CASES) + len(T.FINDING_CLI_CASES)} CLI cases + "
          f"{len(T.WRITE_COMMANDS) * len(T.FILE_STATES)} round-trip cases + "
          f"{len(CROSS_DOOR_IDS)} cross-door cases + {len(T.BOUNDARY_CASES)} boundary cases "
          f"({len(T.DOMAINS)} bounded values, each probed JUST INSIDE and JUST OUTSIDE its declared "
          f"domain) + {len(doc_examples(R))} DOC examples + {len(T.STATUS_CASES)} status render cases "
          f"hold — and {doors}.\n")

    # …and now the question the block above CANNOT answer: is any rule pinned by NO fixture?
    marked = marked_statements(
        source,
        error_factory=SelfTestFailure,
        no_markers_message="no MUTATE markers — the rules cannot mark themselves absent",
    )
    gaps = unmarked_enforcements(
        source,
        marked,
        rule_functions=RULE_FUNCTIONS,
        enforcing_exceptions=ENFORCING_EXCEPTIONS,
        enforcing_verdicts=ENFORCING_VERDICT_NAMES,
        source_name="review-pass.py",
    )
    for gap in gaps:
        print(f"UNMARKED {gap}")
    if gaps:
        print(f"\n{len(gaps)} enforcement point(s) carry NO marker.")
        return 1

    print(f"{'rule':32} {'weakened to':38} {'killed by':32} {'outcome':11} kill")
    print(f"{'-' * 32} {'-' * 38} {'-' * 32} {'-' * 11} ----")
    unpinned, broken, tally = [], [], Counter()
    for rule, (weakening, stmt) in marked.items():
        try:
            mod = load_source_module(
                mutate_source(source, rule, weakening, stmt),
                f"rp_mutant_{rule.replace('-', '_')}",
                OWNER,
            )
        except SyntaxError as exc:
            broken.append(f"{rule}: the weakening {weakening!r} does not compile ({exc})")
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            mutant = run_cases(mod, T, Path(tmpdir))
        # A mutation only ever REMOVES a rule, so it can never turn a PASSING case into a failing one.
        # If it does, the mutation is bogus — a harness bug, never a pinned rule.
        wrong = [f"{c} expected {w} but the mutant returned {mutant[c][0]}"
                 for c, (w, _, _) in expect.items() if w in PASSING and mutant[c][0] != w]  # drops needle, why
        if wrong:
            broken.append(f"{rule}: BOGUS MUTATION — {'; '.join(wrong)}")
            continue
        killers = []
        for case, (want, needle, _) in expect.items():  # drops why
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
            print(f"{rule:32} {weakening[:38]:38} {'NOTHING':32} {'—':11} UNPINNED")
            unpinned.append(rule)
            continue
        strength, case, outcome = killers[0]
        extra = f" (+{len(killers) - 1} more)" if len(killers) > 1 else ""
        tally[strength] += 1
        print(f"{rule:32} {weakening[:38]:38} {case[:32]:32} {outcome:11} {strength}{extra}")

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


def load_owner() -> types.ModuleType:
    """Load `review-pass.py` — used ONLY when this file is run directly."""
    spec = importlib.util.spec_from_file_location("review_pass", OWNER)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
        raise SystemExit(f"review-pass-test: cannot load {OWNER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        return run(load_owner(), Path(tmp))


if __name__ == "__main__":
    raise SystemExit(main())
