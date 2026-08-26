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
AMENDMENT_WRAPPER = HERE / "emit-amendment.py"
REPORT_WRAPPER = HERE / "emit-report.py"
WRAPPER_DOOR = "emit-progress.py"
FINDING_WRAPPER_DOOR = "emit-finding.py"
AMENDMENT_WRAPPER_DOOR = "emit-amendment.py"
REPORT_WRAPPER_DOOR = "emit-report.py"
WRAPPER_OWNER_COMMANDS = {
    WRAPPER_DOOR: "emit",
    FINDING_WRAPPER_DOOR: "finding-add",
    AMENDMENT_WRAPPER_DOOR: "amend",
    REPORT_WRAPPER_DOOR: "report-write",
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
REPORT_FILE = "review-41-1.report.jsonl"
INTENT_FILE = "intent-41.md"

# The four artifact suffixes, spelled ONCE here and RECONCILED against the owner's own constants in
# `run()`. The report's suffix has already changed once — it was `.txt` while the report was free text —
# and a second, unreconciled spelling of it in this file would go on building fixtures at a path the tool
# no longer writes, which is a suite that passes about the wrong file.
PROGRESS_SUFFIX, FINDINGS_SUFFIX, REPORT_SUFFIX = ".progress.jsonl", ".findings.jsonl", ".report.jsonl"


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


RESIDUAL_ONE = "parser contract — exact framing is hardest to verify"


def report_line(verdict: Value = "satisfied", *, reason: Value = "-",
                residual: "Sequence[str] | None" = None,
                summary: Value = "Report body.", **over: Value) -> str:
    """ONE report artifact's bytes: the single validated record, as a fixture writes it RAW.

    The record's field values are typed `Value` for the reason every other builder here is: half these
    fixtures write what the schema FORBIDS — a `residual_risk` that is a string, a `verdict` outside the
    enum, a key that is not there at all — and the READ side must catch each without being told how the
    bytes got there.
    """
    rec: "dict[str, Value]" = {"type": "review_report", "verdict": verdict,
                               "deferred_reason": reason,
                               "residual_risk": list(residual or []), "summary": summary}
    return _rec(rec, over) + "\n"


SAT_REPORT = report_line("satisfied", residual=[RESIDUAL_ONE])
NOT_SAT_REPORT = report_line("not-satisfied")
DEFERRED_REPORT = report_line("deferred", reason="fixture request must be handled first")


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
                         "launch_attempt": "1", "dispatched_at": TS, "default_non_goals": []}, over)

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
                         "base": R.INTRODUCED, "base_repro": R.NO_BASE_REPRO,
                         "repro": "a rollup whose headRefOid moved while the REST page still read green",
                         "fix": "refuse a snapshot whose head moved under the fetch"}, over)

        def waiver(dimension: str = "docs", **over: Value) -> str:
            return _rec({"type": R.WAIVER, "dimension": dimension,
                         "reason": "internal-only change; no user-facing doc covers this area"}, over)

        self.ident, self.unit, self.started, self.done = ident, unit, started, done
        self.amendment, self.finding, self.waiver = amendment, finding, waiver

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

            # THE WAIVERS — the plan's other row type: the orchestrator's recorded judgment that one
            # default dimension does not apply. A waiver demands no progress, and its rules hold at the
            # read door, which never assumes the write tool was used.
            "waived-plan": (PLAN + [waiver("docs")], WORKED, OK, "ARTIFACTS are sound",
                            "a waiver is a plan row, not a unit — it demands no progress and blocks nothing"),
            "waiver-unknown-dimension": (PLAN + [waiver("performance")], WORKED, UNUSABLE, "waives nothing",
                                         "a waiver naming a dimension outside the closed set waives nothing"),
            "waiver-extra-key": (PLAN + [waiver("docs", ts=TS)], WORKED, UNUSABLE, "a waiver carries EXACTLY",
                                 "a waiver carrying a key nothing reads — the exact-keys rule, one row type over"),
            "waiver-blank-reason": (PLAN + [waiver("docs", reason="  ")], WORKED, UNUSABLE, "a waiver IS its reason",
                                    "a blank reason records a judgment nobody can judge"),
            "waiver-duplicate": (PLAN + [waiver("docs"), waiver("docs", reason="said twice")], WORKED, UNUSABLE,
                                 "waived twice",
                                 "one waiver per dimension — a second records nothing the first did not"),
            "waiver-contradicts-unit": (PLAN + [unit("u03", kind="docs", target="README.md",
                                                     checks=["the README matches the change"]),
                                                waiver("docs")],
                                        WORKED + [started("u03"), done("u03", evidence="README.md:1")],
                                        UNUSABLE, "both planned",
                                        "a dimension both planned and waived — the plan contradicts itself, and with this rule deleted the pass verifies clean"),
            "waivers-only-plan": ([waiver("docs")], [ident()], UNUSABLE, "holds no units",
                                  "emptiness counts UNITS: a plan of nothing but waivers reviewed nothing"),

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
            "identity-noncanonical-scope": (PLAN, [ident(default_non_goals=["dup", "dup"]), done("u01"), done("u02")],
                                            UNUSABLE, "DISPATCH-TIME scope",
                                            "a pass_identity whose default_non_goals binding is not a canonical list "
                                            "(a duplicate entry) — the scope analogue of a truncated sha, fail-closed"),
            "identity-scope-not-list": (PLAN, [ident(default_non_goals="area X"), done("u01"), done("u02")],
                                        UNUSABLE, "DISPATCH-TIME scope",
                                        "the scope binding stored as a bare STRING, not the JSON array `verify --ledger` compares"),

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
                                         "…and at the THIRD intake: an amendment's `proposed_unit` is what the orchestrator FOLDS INTO THE PLAN, so an id the plan door would refuse must be refused here too — or the plan acquires, one heartbeat later, exactly the unmatchable unit this rule exists to keep out of it"),
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
                                   "a hand-written amendment (the read side never assumes the amend door was used) whose proposed unit is malformed"),
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
        # (plan, progress, findings lines, intent text or None, legacy verdict/report selector, want,
        # needle, why). The legacy value only chooses fixture report bytes; `evaluate` ignores it.
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

        # --- run-default MANAGED-BLOCK intents, for `scan_managed_block`'s structural rules ------------
        # A valid intent whose `## Non-goals` holds a well-formed managed block, and four that break its
        # fence one way each. Each malformed one is a COMPLETE, otherwise-valid intent, so `parse_intent`
        # reaches the managed-block scan and refuses THERE, not on an earlier section rule.
        MS, ME = R.MANAGED_START, R.MANAGED_END
        _mb_head = "## Purpose\n- never emit a false green\n\n## Non-goals\n- a pr specific exclusion\n"
        _mb_tail = "\n## Threat model\n- Who can write the inputs this code reads: the network\n"
        MB_VALID = _mb_head + MS + "\n- run default one\n" + ME + _mb_tail
        MB_DUPLICATE = (_mb_head + MS + "\n- run default one\n" + ME + "\n"
                        + MS + "\n- run default two\n" + ME + _mb_tail)
        MB_UNTERMINATED = _mb_head + MS + "\n- run default one\n" + _mb_tail
        MB_INVERTED = _mb_head + ME + "\n- run default one\n" + MS + _mb_tail
        MB_OUTSIDE = ("## Purpose\n- never emit a false green\n" + MS + "\n- run default one\n" + ME
                      + "\n\n## Non-goals\n- a pr specific exclusion\n" + _mb_tail)
        MB_NON_BULLET = _mb_head + MS + "\n- run default one\nnot a bullet line\n" + ME + _mb_tail

        # --- `verify --ledger`: the DISPATCH-TIME scope binding at tally (fu, finding B) ------------------
        # A complete, SATISFIED pass DISPATCHED under the run defaults ["area X"] — the scope is bound into the
        # immutable `pass_identity.default_non_goals`, NOT inferred from the mutable intent-<pr>.md. If the
        # operator BROADENS the header (removes X) while this pass is in flight, the stale SATISFIED (earned
        # under [X], X now removed) must NOT count, or area X merges unreviewed. The tuple sets the identity's
        # BOUND scope, the intent block's defaults (to prove the tally IGNORES it), and the header's live
        # defaults independently — so a case can reproduce the exact defeat the mutable-intent check missed:
        # re-adoption RE-SYNCS the intent to [] before the tally, yet the pass is still refused because the
        # BINDING says [X]. Each intent text is folded through the REAL module — fixed test data, not under test.
        # name -> (identity-bound scope, intent-block defaults, header defaults, ledger in a SIBLING dir?,
        #          CREATE the ledger file?, want exit, needle)
        self.LEDGER_CASES: "dict[str, tuple[list[str], list[str], list[str] | str, bool, bool, int, str]]" = {
            # THE DEFEAT, CLOSED: pass bound to [X]; the intent has been RE-SYNCED to [] to MATCH the header
            # (the exact move the mutable-intent check waved through) — yet the pass is refused, because the
            # tally reads the BINDING, not the intent.
            "resynced-intent-stale-scope-refused": (["area X"], [], [], False, True, 1, "DISPATCHED under"),
            # symmetric: a mid-flight ADD narrows scope; the binding [] no longer matches [X] -> voided (safe)
            "scope-added-refused": ([], ["area X"], ["area X"], False, True, 1, "DISPATCHED under"),
            # in sync: bound to [X], header still [X] -> the pass counts, exactly as a bare verify would
            "in-sync-counts": (["area X"], ["area X"], ["area X"], False, True, 0, "report-verdict=satisfied"),
            # a malformed header default_non_goals -> the run scope cannot be read -> fail closed, never count
            "malformed-header-fails-closed": ([], [], "not-json{", False, True, 1, "malformed"),
            # a --ledger from ANOTHER run measures the pass against the wrong scope -> operator error (exit 2)
            "cross-run-refused": (["area X"], ["area X"], ["area X"], True, True, 2, "same run directory"),
            # F1: a same-dir --ledger that DOES NOT EXIST would read as HEADER_DEFAULTS ([] — zero run
            # defaults) and count this pass against an EMPTY scope. It must fail CLOSED (exit 2), not exit 0.
            # The pass is bound to [] so, with the existence guard REMOVED, the missing ledger's back-filled
            # [] matches and the mutant returns a FALSE PASS — which is exactly what pins the guard.
            "missing-ledger-fails-closed": ([], [], [], False, False, 2, "does not exist"),
        }

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
                PLAN, WORKED, [R42_23], INTENT, None, UNUSABLE, "no active review report",
                "a complete pass with no report is unusable; no caller-retold verdict can replace it"),
            "in-flight-no-verdict": (
                PLAN, [ident(), started("u01")], None, INTENT, None, UNUSABLE,
                "no active review report",
                "strict verify requires a report; torn in-flight output belongs to lenient `status`"),

            # --- `deferred` is NOT a verdict: it routes to the progress-file state --------------------
            #
            # A reviewer that raised a separate request the orchestrator must handle first — a
            # `plan_amendment_request`, or a broken-dispatch stop — records `verdict: deferred`.
            # That result NEVER reaches the coherence rule;
            # the progress file is authoritative, and `decide` answers amended / incomplete / unusable.
            "deferred-with-amendment": (
                PLAN, [ident(), started("u01"), done("u01"), amendment(), started("u02"), done("u02")],
                [], INTENT, DEF, AMENDED, "not yet ruled on",
                "**THE CASE THE MARKER EXISTS FOR.** The reviewer raised a `plan_amendment_request` and "
                "recorded `deferred` instead of ruling. `deferred` is not weighed against anything — the "
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
                PLAN, [ident(), started("u01")], None, None, DEF, UNUSABLE, "THE RUN SKIPPED A STEP",
                "the pass is still WORKING (u01 started, nothing done) and its PR has no intent — and it is "
                "refused for the INTENT, not merely reported `incomplete`. A run that dispatched a reviewer "
                "with nothing to measure it against is broken from the first heartbeat, and the earliest verdict "
                "that can say so is the one that should"),

            # --- the finding's own shape -------------------------------------------------------------
            "finding-bad-writer": (
                PLAN, WORKED, [F(writer="attacker")], INTENT, NS, UNUSABLE, "CLOSED enum",
                "`writer` outside the enum. It is not a new kind of actor — it is a field nobody filled in, "
                "and the gating rule reads it"),
            "finding-bad-base": (
                PLAN, WORKED, [F(base="maybe")], INTENT, NS, UNUSABLE, "CLOSED enum of two",
                "`base` outside the enum, reached the ONLY way it can be: a HAND-WRITTEN findings line. "
                "Argparse's `choices` refuses it at the CLI door, so without this fixture the read-side "
                "rule is unpinned — and the read side is what `verify` re-derives the verdict from"),
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

            # --- the run-default MANAGED block's STRUCTURE (scan_managed_block) -----------------------
            #
            # The operator's run defaults sit inside `## Non-goals` between two markers. A reviewer must
            # never be handed an intent whose fence is ambiguous, so `parse_intent` refuses a malformed one
            # at the same door it refuses a malformed section. A well-formed block is invisible to the
            # verdict — the bullets are ordinary Non-goals — so the valid case must STILL pass.
            "managed-block-valid": (
                PLAN, WORKED, None, MB_VALID, SAT, OK, "0 gating finding(s)",
                "a well-formed run-default managed block is just Non-goals bullets with a fence; it parses "
                "and the pass is judged normally"),
            "managed-block-duplicate": (
                PLAN, WORKED, None, MB_DUPLICATE, SAT, UNUSABLE, "appears more than once",
                "TWO managed blocks — a nested/duplicated fence `intent-sync` can no longer own. One run, "
                "one block"),
            "managed-block-unterminated": (
                PLAN, WORKED, None, MB_UNTERMINATED, SAT, UNUSABLE, "unterminated",
                "a start marker with no end — one without the other fences nothing, so the operator's "
                "defaults blur into the PR-specific bullets below"),
            "managed-block-inverted": (
                PLAN, WORKED, None, MB_INVERTED, SAT, UNUSABLE, "inside out",
                "the end marker precedes the start — the fence encloses nothing and everything at once"),
            "managed-block-outside-nongoals": (
                PLAN, WORKED, None, MB_OUTSIDE, SAT, UNUSABLE, "NOT inside",
                "the block sits under `## Purpose` — run defaults are Non-goals and belong in that section "
                "alone, or `intent-sync` would rewrite the wrong part of the intent"),
            "managed-block-non-bullet": (
                PLAN, WORKED, None, MB_NON_BULLET, SAT, UNUSABLE, "non-bullet line",
                "a prose line between the markers — the managed block holds ONLY `- ` run-default bullets, "
                "so anything else is a hand-edit `intent-sync` did not write"),

            # --- the findings file's own line shape --------------------------------------------------
            "findings-not-json": (
                PLAN, WORKED, ["not a finding at all"], INTENT, NS, UNUSABLE, "is not JSON",
                "a corrupt line in the artifact the GATING RULE is computed from"),
            "findings-blank-line": (
                PLAN, WORKED, [R43_11, ""], INTENT, NS, UNUSABLE, "is blank",
                "JSONL has no blank lines — here as anywhere else"),
        }

        # --- the active report: ONE validated record ---------------------------------------------
        #
        # The report used to be free text with a `VERDICT:` line the parser had to LOCATE, and most of the
        # cases here used to be about that search: a truncated report, two verdict lines, a postscript that
        # made the result nonterminal, a residual-risk remark that had to be found among prose and lost its
        # tail to a control character. **None of those inputs can be built any more** — a JSON record has
        # no "last nonblank line" to be wrong about and no line boundary to be split at — so the cases
        # below pin what replaced them: the record's exact key set, its closed verdict enum, its
        # sentinel-or-reason rule, and the LIST that makes each residual record its own value.
        self.REPORT_CASES: "dict[str, dict]" = {
            "valid-satisfied": {
                "report": SAT_REPORT, "want": OK, "needle": "report verdict satisfied",
                "why": "the record yields SATISFIED and carries the records the reviewer wrote with it",
            },
            "valid-not-satisfied": {
                "report": NOT_SAT_REPORT, "findings": [finding()], "want": OK,
                "needle": "report verdict not-satisfied",
                "why": "NOT SATISFIED remains coherent when a gating finding stands",
            },
            "valid-deferred-amendment": {
                "progress": [ident(), amendment()], "report": DEFERRED_REPORT, "want": AMENDED,
                "needle": "not yet ruled on",
                "why": "DEFERRED routes an unruled plan request without turning it into a judgment",
            },
            "valid-deferred-incomplete": {
                "progress": [ident(), started("u01")], "report": DEFERRED_REPORT, "want": INCOMPLETE,
                "needle": "has not covered its plan",
                "why": "a broken-dispatch stop routes through incomplete progress",
            },
            "spurious-deferred": {
                "report": DEFERRED_REPORT, "want": UNUSABLE, "needle": "nothing to defer to",
                "why": "DEFERRED cannot replace a binary result on a finished pass with no request",
            },
            "missing": {
                "report": None, "want": UNUSABLE, "needle": "no active review report",
                "why": "a caller-retold verdict cannot stand in for an absent report",
            },
            "empty": {
                "report": "", "want": UNUSABLE, "needle": "holds 0 record(s)",
                "why": "an empty artifact contains no review result",
            },
            "two-records": {
                "report": NOT_SAT_REPORT + SAT_REPORT, "want": UNUSABLE,
                "needle": "holds 2 record(s)",
                "why": "two results leave no single review judgment — and this is what refuses a second "
                       "`report-write` too",
            },
            "not-json": {
                "report": "Report body only.\n", "want": UNUSABLE, "needle": "is not JSON",
                "why": "prose where a record belongs is a producer we cannot trust, not text to search",
            },
            "wrong-record-type": {
                "report": report_line("satisfied", type="progress"), "want": UNUSABLE,
                "needle": "not 'review_report'",
                "why": "a report artifact holds exactly one review_report record and nothing else",
            },
            "missing-key": {
                "report": report_line("satisfied", summary=DROP), "want": UNUSABLE,
                "needle": "carries EXACTLY",
                "why": "an absent required field is refused, never defaulted",
            },
            "extra-key": {
                "report": report_line("satisfied", note="carried but unread"), "want": UNUSABLE,
                "needle": "unexpected key(s)",
                "why": "a key nothing reads asserts something neither verified nor refuted",
            },
            "verdict-outside-enum": {
                "report": report_line("SATISFIED"), "want": UNUSABLE, "needle": "`verdict` is 'SATISFIED'",
                "why": "the verdict enum is closed and its spelling is the ledger's, not a variant",
            },
            "blank-summary": {
                "report": report_line("satisfied", summary="   "), "want": UNUSABLE,
                "needle": "`summary` is",
                "why": "a verdict with no account behind it is one nobody can read or reassess",
            },
            "deferred-without-reason": {
                "report": report_line("deferred"), "want": UNUSABLE,
                "needle": "carries the ONE-LINE REASON",
                "why": "a request without a reason cannot be routed",
            },
            "deferred-blank-reason": {
                "report": report_line("deferred", reason="  "), "want": UNUSABLE,
                "needle": "`deferred_reason` is",
                "why": "blank is not the sentinel and not a reason — absence must be TYPED",
            },
            "verdict-with-deferred-reason": {
                "report": report_line("satisfied", reason="but also here is a request"),
                "want": UNUSABLE, "needle": "defers nothing",
                "why": "a routing reason beside a rendered verdict is a request nobody will act on",
            },
            # THE CALIBRATION RECORD IS METADATA, AND THE ARTIFACT IS WHAT NOW MAKES THAT SAFE. It used to
            # be a line hunted inside prose, where every way of writing it raised the question of how
            # leniently to read it — a question that twice cost a complete, substantive review pass. There
            # is nothing left to write crookedly: the reviewer passes each remark as one value and the
            # encoder owns the boundaries. What the records CONTRIBUTE is `check_residual_records`'s to
            # pin; these pin only the shape rules.
            "satisfied-without-residual-risk": {
                "report": report_line("satisfied"), "want": OK, "needle": "report verdict satisfied",
                "why": "writing no calibration record is the correct output, not a deficient one",
            },
            "several-residual-records": {
                "report": report_line("satisfied", residual=["first — hard", "second — harder"]),
                "want": OK, "needle": "report verdict satisfied",
                "why": "two remarks are two records, not two verdicts to choose between",
            },
            # THE CONTROL CHARACTER THAT USED TO TRUNCATE THE RECORD. As a line in free text, everything
            # from U+001C onward was a separate line to the reader that found the remark — and the same
            # split decided which line the verdict was, so it could not be fixed there. As an ARRAY
            # ELEMENT the encoder escapes it, and the record arrives whole.
            "residual-record-with-control-character": {
                "report": report_line("satisfied", residual=["parser\u001ccontract — hard"]),
                "want": OK, "needle": "report verdict satisfied",
                "why": "a control character inside a record is escaped by the encoder, never a boundary",
            },
            "residual-not-a-list": {
                "report": report_line("satisfied", residual_risk="parser — hard"), "want": UNUSABLE,
                "needle": "`residual_risk` is",
                "why": "a bare string is one record spelled as text, and the LIST is what keeps boundaries",
            },
            "residual-on-not-satisfied": {
                "report": report_line("not-satisfied", residual=["parser — hard"]),
                "findings": [finding()], "want": UNUSABLE, "needle": "residual-risk record(s)",
                "why": "the signal names the least-certain area of an ACCEPTING pass, and only accepting "
                       "passes are read for it",
            },
            "wrong-attempt-only": {
                "progress_name": "review-41-1.a2.progress.jsonl",
                "progress": [ident(launch_attempt="2")], "report": None,
                "extra_reports": {REPORT_FILE: SAT_REPORT},
                "want": UNUSABLE, "needle": "review-41-1.a2.report.jsonl",
                "why": "attempt 1 cannot supply attempt 2's result",
            },
            "active-attempt-wins": {
                "progress_name": "review-41-1.a2.progress.jsonl",
                "progress": [ident(launch_attempt="2"), started("u01"), done("u01"),
                             started("u02"), done("u02", evidence="stage-2:161")],
                "report": SAT_REPORT, "extra_reports": {REPORT_FILE: NOT_SAT_REPORT},
                "want": OK, "needle": "report verdict satisfied",
                "why": "the active attempt's result wins while the dead attempt stays inert",
            },
            "hostile-bytes": {
                "report": b"\xff\n", "want": UNUSABLE,
                "needle": "cannot be read as UTF-8",
                "why": "undecodable report bytes are refused rather than rewritten",
            },
            "hostile-path": {
                "dirname": "report $() ' path", "report": SAT_REPORT, "want": OK,
                "needle": "report verdict satisfied",
                "why": "path punctuation remains data and does not affect report selection",
            },
        }

        # --- the WRITE doors ---------------------------------------------------------------------
        EMPTY: "list[str]" = []
        DISPATCHED = [ident()]                       # what the orchestrator leaves behind, before the launch
        BEGUN = [ident(), started("u01")]            # once the reviewer has ANNOUNCED u01
        FINISHED = [ident(), started("u01"), done("u01")]   # …and once it has already FINISHED u01
        self.EMPTY, self.DISPATCHED, self.BEGUN, self.FINISHED = EMPTY, DISPATCHED, BEGUN, FINISHED

        self.CLI_CASES = [
            (["emit", "--unit", "u01", "--status", "started"], DISPATCHED, 0, "", "the call every reviewer prompt makes"),
            (["emit", "--unit", "u01", "--status", "done", "--evidence", "f.py:1"], BEGUN, 0, "",
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
            (["identity", "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"], EMPTY, 0, "",
             "the line that was a `printf` — pr/pass/attempt now come from the FILENAME"),
            (["identity", "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", '["area X"]'], EMPTY, 0, "",
             "the dispatch-time scope binding is stored DIRECTLY as a JSON array — the immutable value the tally compares to the run's current defaults"),
            (["identity", "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "not-json"], EMPTY, 1, "canonical JSON array",
             "a malformed `--default-non-goals`: the run scope must decode through the ledger's ONE validator, or the binding names no scope"),
            (["identity", "--head-sha", SHA[:7], "--dispatched-at", TS, "--default-non-goals", "[]"], EMPTY, 1, "escaped into this repo's real state",
             "HEADLINE, WRITE DOOR: the truncated sha that got written into a real pass_identity"),
            (["identity", "--head-sha", SHA.upper(), "--dispatched-at", TS, "--default-non-goals", "[]"], EMPTY, 1, "LOWERCASE",
             "an UPPERCASE sha: no producer of ours emits one, so it did not come from `git rev-parse`"),
            (["identity", "--head-sha", SHA, "--dispatched-at", "just now", "--default-non-goals", "[]"], EMPTY, 1, "LAUNCH DEADLINE's clock",
             "a dispatch clock the launch deadline cannot be measured from"),
            (["identity", "--head-sha", SHA, "--dispatched-at", "2026-99-99T99:99:99Z", "--default-non-goals", "[]"], EMPTY, 1, "not a real UTC time",
             "…and the one the SHAPE rule cannot see: an impossible date in the right shape"),
            (["identity", "--head-sha", OTHER_SHA, "--dispatched-at", TS, "--default-non-goals", "[]"], [ident()], 1, "NOT EMPTY",
             "a SECOND identity into a live pass's file — how one pass ends up describing two commits"),
            (["identity", "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"], [""], 1, "NOT EMPTY",
             "HEADLINE, WRITE DOOR: a WHITESPACE-ONLY file. This door decided 'empty' with `.strip()`, wrote the identity below the blank line, exited 0 — and `verify` then refused the artifact FOR THAT BLANK LINE. EMPTY now means NO BYTES, at both doors"),
            (["verify", "--head-sha", SHA[:7], "--verdict", "satisfied"], EMPTY, 2, "No verdict beats a wrong one",
             "an OPERATOR error is not a snapshot verdict: exit 2"),
            (["verify", "--head-sha", SHA, "--amendments-ruled", "1", "--verdict", "satisfied"], EMPTY, 2, "raised only 0",
             "a ruling for an amendment that does not exist would silently clear the NEXT one raised"),
            (["verify", "--head-sha", SHA, "--amendments-ruled", "-1", "--verdict", "satisfied"], WORKED, 2,
             "smallest legal value is 0",
             "HEADLINE: A NEGATIVE RULING WEDGES A PASS THAT WAS EARNED. `decide` SUBTRACTS the ruling, so `0 - (-1) = 1` amendment 'not yet ruled on' that the reviewer never raised and no ruling can ever clear"),
            (["verify", "--head-sha", SHA], WORKED, 0, "report-verdict=satisfied",
             "the active report supplies the result; no caller verdict is required"),
            (["verify", "--head-sha", SHA, "--verdict", "not-satisfied"], WORKED, 0,
             "report-verdict=satisfied",
             "the compatibility flag is non-authoritative and cannot override the SATISFIED report"),
            (["verify", "--head-sha", SHA, "--verdict", "satisfied"], WORKED, 0, "0 gating finding(s)",
             "…and the same pass returning SATISFIED, which needs no finding at all"),
            (["verify", "--head-sha", SHA, "--verdict", "deferred"], WORKED, 0,
             "report-verdict=satisfied",
             "the compatibility flag cannot turn a SATISFIED report into a deferral"),

            # THE AMENDMENT WRITE DOOR — the one progress event a reviewer used to hand-write, now with a door.
            (["amend", "--reason", "no unit covers the harness", "--id", "u09", "--kind", "file",
              "--target", "harness.py", "--check", "it runs"], DISPATCHED, 0, "",
             "**THE FIX, AT THE WRITE DOOR.** The dispatch prompt never stated the amendment's schema, so "
             "reviewers invented `{type, gap}` and `verify` refused the malformed line — taking the WHOLE "
             "pass down. Now the amendment goes through a door like every other event: the `ts` is "
             "TOOL-STAMPED (no clock is an input), and the line it writes is one `verify` reads back"),
            (["amend", "--reason", "no unit covers the harness", "--id", " u01 ", "--kind", "file",
              "--target", "x.py", "--check", "a"], DISPATCHED, 1, "NOT AN ID",
             "the proposed unit's id goes through the SAME `check_unit` the plan door runs, so an id the "
             "plan would refuse is refused here — the plan cannot acquire, one heartbeat later, an "
             "unmatchable unit this amendment would have folded into it"),
            (["amend", "--reason", "   ", "--id", "u09", "--kind", "file", "--target", "x.py",
              "--check", "a"], DISPATCHED, 1, "an amendment is a CLAIM",
             "a blank `--reason`: the read side's own non-blank predicate, at the write door. The "
             "orchestrator RULES on the reason; a blank one forces the `amended` verdict while saying "
             "nothing to rule on"),
            (["amend", "--reason", "harness gap", "--id", "u09", "--kind", "file", "--target", "x.py"],
             DISPATCHED, 2, "the following arguments are required: --check",
             "the amendment's `--check` is REQUIRED and repeatable, exactly as `plan-add`'s is — a proposed "
             "unit with no checks is not a unit, and the help door may not bracket a flag the write path "
             "refuses"),
            (["amend", "--reason", "harness gap", "--id", "u09", "--kind", "file", "--target", "x.py",
              "--check", "a"], EMPTY, 1, "NO `pass_identity`",
             "the amendment appends into the SAME progress file the orchestrator seeds with `pass_identity` "
             "before dispatch — so an EMPTY file means the pass was never launched, and the write is refused "
             "and NOTHING is written, exactly as `emit`'s is"),
        ]

        # `plan-add` and `finding-add` get their own families: their flags do not fit the shape above (a
        # repeatable `--check`; a seven-flag finding), and the ARTIFACT'S NAME is under test too.
        self.PLAN_CLI_CASES = [
            (PLAN_FILE, ["--id", "u03", "--kind", "cross-cutting", "--target", "both doors", "--check", "a", "--check", "b"],
             0, "", "the plan stops being a shell heredoc"),
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

        # plan-waive: (plan name, argv, exit, needle, why) — the waiver's own write door.
        self.WAIVE_CLI_CASES = [
            (PLAN_FILE, ["--dimension", "docs", "--reason", "internal-only change"], 0, "",
             "the waiver door: a default dimension is dropped OUT LOUD, validated as it lands"),
            (PLAN_FILE, ["--dimension", "docs", "--reason", "   "], 1, "a waiver IS its reason",
             "the check argparse cannot make: a --reason that is present and BLANK"),
            (PLAN_FILE, ["--dimension", "performance", "--reason", "x"], 2, "invalid choice",
             "a dimension outside the closed set — refused by ARGPARSE, at the door, naming the flag"),
            ("plan.jsonl", ["--dimension", "docs", "--reason", "x"], 1, "not a plan artifact's name",
             "the same name rule as plan-add: a waiver written under a name nothing reads waives nothing"),
        ]

        # plan-check: name -> (plan lines, tier, exit, needle, why) — the pre-dispatch door for the rule
        # that used to be prose: every default dimension covered or waived.
        self.PLAN_CHECK_CASES: "dict[str, tuple]" = {
            "trivial-owes-nothing": (PLAN, "TRIVIAL", 0, "owes no default dimensions",
                                     "a TRIVIAL plan is minimal by rule; no defaults are due"),
            "standard-missing": (PLAN, "STANDARD", 1, "neither covered nor waived",
                                 "THE HEADLINE: the omitted tests/docs/public-api unit that used to cost a plan amendment plus a full re-review is refused BEFORE dispatch"),
            "standard-accounted": (PLAN + [unit("u03", kind="tests", target="scripts/review-pass-test.py",
                                                checks=["the change is covered"]),
                                           unit("u04", kind="public-api", target="exported surface",
                                                checks=["no exported symbol changed unreviewed"]),
                                           waiver("docs")],
                                   "STANDARD", 0, "waived —",
                                   "every default accounted for: two covered by units, one waived out loud"),
            "typo-tier-fails-closed": (PLAN, "trivial", 1, "neither covered nor waived",
                                       "a tier that is not exactly TRIVIAL owes the defaults — a typo can only ask for MORE accounting, never less"),
        }

        # The new `--base`/`--base-repro` pair is APPENDED, never spliced into the middle: the cases below
        # slice this list by INDEX (`FIND_OK[:6]`, `FIND_OK[8:]`), so a flag inserted anywhere but the end
        # would silently re-aim every one of those slices at a different flag.
        FIND_OK = ["--path", "scripts/ci-status.py", "--line", "769", "--writer", "network",
                   "--purpose", PURPOSE_GREEN, "--repro", "a paginated reply with no `statuses` member",
                   "--fix", "refuse a missing row array",
                   "--base", R.INTRODUCED, "--base-repro", R.NO_BASE_REPRO]
        # --- the REPORT write door ---------------------------------------------------------------
        #
        # `(filename, seed, argv, exit, needle, why)`. `seed` is what the target already holds, so the
        # SECOND-report case can be stated as the state it meets rather than as two calls.
        REPORT_OK = ["--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                     "--summary", "Report body."]
        self.REPORT_CLI_CASES = [
            (REPORT_FILE, None, REPORT_OK, 0, "",
             "the call every reviewer prompt makes, on the file the orchestrator derived for it"),
            (REPORT_FILE, None, [*REPORT_OK[:4], "--summary", "Report body.",
                                 "--residual-risk", RESIDUAL_ONE],
             0, "",
             "…and the same call carrying one calibration record, which is OPTIONAL and does not weaken it"),
            # **THE NAME.** A report written where `verify` will never DERIVE it is a report nothing reads,
            # and the pass is then refused for having none while its verdict sits on disk one filename
            # away. `.txt` is the name the report wore while it was free text, so it is exactly the wrong
            # name a caller is most likely to type.
            ("review-41-1.txt", None, REPORT_OK, 1, "not a launch attempt's report artifact",
             "the report's OLD free-text name is not a variant of its artifact name; it is not one"),
            (REPORT_FILE, SAT_REPORT, REPORT_OK, 1, "holds 2 record(s)",
             "**A SECOND REPORT IS NOT AN APPEND.** A pass yields ONE result, so the door refuses rather "
             "than recording a choice among two — and it is refused by the read side's own one-record "
             "rule, through the readback, not by a write-shaped copy of it"),
            (REPORT_FILE, None, ["--verdict", R.DEFERRED, "--deferred-reason", R.NO_DEFERRED_REASON,
                                 "--summary", "Report body."],
             1, "carries the ONE-LINE REASON",
             "a deferral is a REQUEST, and one the orchestrator cannot route is a pass that stopped for a "
             "reason only the reviewer knows"),
            (REPORT_FILE, None, ["--verdict", R.NOT_SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                                 "--summary", "Report body.", "--residual-risk", RESIDUAL_ONE],
             1, "residual-risk record(s)",
             "the calibration signal names the least-certain area of an ACCEPTING pass, and the one "
             "document that carries it onward reads only accepting passes"),
        ]

        self.FINDING_CLI_CASES = [
            (FINDINGS_FILE, FIND_OK, 0, "",
             "the call the reviewer prompt makes — a finding that DEFENDS a stated purpose and names a real actor"),
            (FINDINGS_FILE, FIND_OK, 0, "",
             "the same anchored finding is recorded without success chatter; its fields still require a "
             "NOT SATISFIED verdict while it stands"),
            (FINDINGS_FILE, ["--path", "scripts/followups.py", "--line", "1815", "--writer", "dev-time",
                             "--purpose", "-", "--repro", "I mutated EXCEPTIONS in memory and self_test() still exited 0",
                             "--fix", "bound the exception table",
                             "--base", R.INTRODUCED, "--base-repro", R.NO_BASE_REPRO],
             0, "",
             "**THE SPIRAL FINDING, RECORDED AND DISCHARGED.** It is WRITTEN and becomes a follow-up without success chatter"),
            (FINDINGS_FILE, [*FIND_OK[:6], "--purpose", "-", *FIND_OK[8:12],
                             "--base", R.PRE_EXISTING,
                             # An INVENTED base sha and an INVENTED output line. Neither exists anywhere in
                             # this tree, deliberately: a real sha rots into a meaningless string, and a
                             # real output line turns this fixture into a false positive for the next
                             # reader who greps for it and lands on the live site that prints it.
                             "--base-repro", "checked out the base at 0000000 and ran the same probe: "
                                             "it printed `all 1 probes agree` and exited 0 there too"],
             0, "",
             "**THE PR-207 FINDING, RECORDED AND DISCHARGED.** True, reproduced, `writer=repo-content` — "
             "and the BASE does exactly the same thing. Under the old two-question rule this GATED, and a "
             "refactor was made to pay for main's history: the fix added detection no version of that code "
             "had ever had, and four of eleven rounds went on its consequences. It anchors to no purpose "
             "line, so the base answer decides, and the finding becomes a follow-up"),
            (FINDINGS_FILE, [*FIND_OK[:6], "--purpose", "-", *FIND_OK[8:12],
                             "--base", R.PRE_EXISTING, "--base-repro", "-"],
             1, "may not go unmeasured",
             "**AND IT MAY NOT BE A BARE ASSERTION.** The same discharge, claimed with nothing behind it. "
             "The one claim that discharges a finding is the one claim that must carry its run — the "
             "failure this whole rule comes from was a claim nobody measured"),
            (FINDINGS_FILE, [*FIND_OK[:12], "--base", R.INTRODUCED,
                             "--base-repro", "I ran it on main and it failed there"],
             1, "contradicts itself",
             "the mirror: a base reproduction filed beside a claim that the base does NOT do this. The "
             "reader cannot tell which half to believe, so neither is accepted"),
            (FINDINGS_FILE, [*FIND_OK[:12], "--base", R.PRE_EXISTING,
                             "--base-repro", "the base prints the same thing at 0000000"],
             0, "",
             "**THE BOUND ON THE WHOLE RULE.** Pre-existing, measured, and it STILL gates — because this "
             "one anchors to a `## Purpose` line. A PR that promised to fix the thing cannot plead that "
             "the thing was already broken"),
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
            # A report artifact that ALREADY holds its one record — the state a second `report-write`
            # meets. Nothing special-cases it: `check_report_file` sees two records in `before + line`
            # and the write is refused by the same statement the read door refuses two with.
            "reported": SAT_REPORT.encode(),
            "corrupt": b"not json at all\n",
            "not-utf8": b"\xff\n",
        }

        self.WRITE_COMMANDS: "dict[str, tuple[str, list[str]]]" = {
            "emit": (PROGRESS_FILE, ["--unit", "u01", "--status", R.STARTED]),
            "identity": (PROGRESS_FILE, ["--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"]),
            "plan-add": (PLAN_FILE, ["--id", "u09", "--kind", "file", "--target", "x.py", "--check", "a"]),
            "plan-waive": (PLAN_FILE, ["--dimension", "docs", "--reason", "internal-only change"]),
            "amend": (PROGRESS_FILE, ["--reason", "harness gap", "--id", "u09", "--kind", "file",
                                      "--target", "x.py", "--check", "a"]),
            "finding-add": (FINDINGS_FILE, FIND_OK),
            "report-write": (REPORT_FILE, ["--verdict", R.SATISFIED, "--deferred-reason",
                                           R.NO_DEFERRED_REASON, "--summary", "Report body."]),
        }
        # `status` writes NOTHING — it is an ADVISORY read-only view — so the round trip does not drive it
        # (there is no produced artifact to read back), and it is declared read-only here so the
        # command-coverage check is satisfied the day the subcommand is added. `plan-check` likewise reads
        # the plan and writes nothing.
        self.READ_ONLY_COMMANDS = frozenset({"intent-check", "verify", "self-test", "status", "plan-check"})

        # --- the DOORS ---------------------------------------------------------------------------
        self.DOOR_SEEDS: "dict[str, tuple[str | None, Sequence[str] | None]]" = {
            "emit": (PROGRESS_FILE, DISPATCHED),        # the reviewer's door: the identity is already there
            WRAPPER_DOOR: (PROGRESS_FILE, DISPATCHED),  # …and the same door, through the wrapper it runs
            "identity": (PROGRESS_FILE, None),          # it writes into a file that must hold NO BYTES
            "plan-add": (PLAN_FILE, None),              # the first unit lands in a plan that does not exist
            "plan-waive": (PLAN_FILE, None),            # …and the first waiver may too (emptiness is read-side)
            "plan-check": (PLAN_FILE, PLAN),            # reads an existing plan; --tier TRIVIAL owes nothing
            "amend": (PROGRESS_FILE, DISPATCHED),       # the amendment appends after the identity, like emit
            AMENDMENT_WRAPPER_DOOR: (PROGRESS_FILE, DISPATCHED),  # …and the same door, through its wrapper
            "finding-add": (FINDINGS_FILE, None),       # …and the first finding in a findings file that does not
            FINDING_WRAPPER_DOOR: (FINDINGS_FILE, None),  # the reviewer's OTHER door, through its wrapper
            "report-write": (REPORT_FILE, None),        # the ONE report lands in a file that does not exist
            REPORT_WRAPPER_DOOR: (REPORT_FILE, None),   # …and the same door, through the wrapper it runs
            "intent-check": (INTENT_FILE, INTENT.splitlines()),
            # a COMPLETE, sound pass; seed_door adds its active SATISFIED report
            "verify": (PROGRESS_FILE, WORKED),
            # status takes `--run`, not `--file`, and writes nothing — its minimal advertised invocation is
            # `status --run .`, which globs the cwd, finds no passes, and exits 0. So it needs no seed file.
            "status": (None, None),
            "self-test": (None, None),                  # no --file, no flags at all
        }

        self.FLAG_VALUES: "dict[str, list[str]]" = {
            "--unit": ["u01"], "--status": [R.STARTED], "--evidence": ["f.py:1"],
            "--head-sha": [SHA], "--dispatched-at": [TS], "--default-non-goals": ["[]"],
            "--id": ["u09"], "--kind": ["file"], "--target": ["x.py"], "--check": ["a"],
            "--reason": ["no unit covers the harness"],
            "--dimension": ["docs"], "--tier": ["TRIVIAL"],
            "--amendments-ruled": ["0"], "--verdict": [R.SATISFIED],
            "--run-dir": ["."],
            "--path": ["scripts/ci-status.py"], "--line": ["769"], "--writer": ["network"],
            "--purpose": [PURPOSE_GREEN], "--repro": ["a reply with no rows"], "--fix": ["refuse it"],
            "--base": [R.INTRODUCED], "--base-repro": [R.NO_BASE_REPRO],
            "--deferred-reason": [R.NO_DEFERRED_REASON], "--summary": ["Report body."],
            "--residual-risk": [RESIDUAL_ONE],
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
                          REPORT_FILE: NOT_SAT_REPORT},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],   # `done` is TERMINAL, so the default hides it; --history shows it
                "expect": {"41-1": {"units": "2/2", "health": "done", "verdict": "NOT-SAT"}},
                "why": "the report record carries a binary verdict, so health is `done` (terminal, hidden "
                       "by default) and the verdict is scraped as NOT-SAT",
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
            "active-attempt-report": {
                "files": {"review-7-1.progress.jsonl": [ident7(), started("u01")],
                          "review-7-1.a2.progress.jsonl": [ident7(launch_attempt="2"), started("u01")],
                          "review-7-1.plan.jsonl": self.PLAN,
                          "review-7-1.report.jsonl": SAT_REPORT,
                          "review-7-1.a2.report.jsonl": NOT_SAT_REPORT},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],
                "expect": {"7-1": {"health": "done", "verdict": "SAT"},
                           "7-1.a2": {"health": "done", "verdict": "NOT-SAT"}},
                "why": "each attempt reads its own report; conflicting attempt-1 and attempt-2 verdicts "
                       "cannot overwrite the active relaunch's status",
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
                          REPORT_FILE: SAT_REPORT},
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
                          REPORT_FILE: SAT_REPORT},
                "now": "2026-07-06T00:03:00Z",
                "flags": ["--history"],   # `done` is terminal — --history reveals the row
                "expect": {"41-1": {"units": "?", "health": "done", "verdict": "SAT"}},
                "why": "the PROGRESS file is a real corruption, but its REPORT carries a verdict: the pass "
                       "FINISHED. The verdict is scraped and `done` wins BEFORE the unreadable give-up, so "
                       "the row reads `done`/`SAT`, never a live-looking `unreadable`",
            },
            "unreadable-done-hidden": {
                "files": {PROGRESS_FILE: UNREADABLE, PLAN_FILE: self.PLAN,
                          REPORT_FILE: SAT_REPORT},
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
    "hook", "read_text", "parse_lines", "read_lines", "check_id", "check_unit", "check_waiver",
    "plan_records", "load_plan",
    "check_event", "check_progress", "walk_progress", "check_identity_shape", "check_identity",
    "check_head", "check_scope", "check_progress_file", "check_plan_file", "parse_report", "decide", "parse_name", "check_ruled",
    "check_writer_path",
    "before_text", "write_line", "cmd_emit", "cmd_identity", "cmd_plan_add", "cmd_plan_waive",
    "cmd_plan_check", "cmd_verify",
    # …and the FINDINGS side: the intent, the anchor, the writer, and the artifact they live in.
    "parse_intent", "load_intent", "check_writer_repro", "check_finding", "findings_name",
    "check_findings_file", "load_findings", "cmd_finding_add",
    # …and the REPORT side: the artifact that used to be free text, and its one record.
    "report_name", "check_report", "check_report_file", "cmd_report_write",
)
ENFORCING_EXCEPTIONS = ("Defect", "OperatorError")
# The NAMES as they are spelled in the source, because that is what the AST holds — `return UNUSABLE, …`
# parses to an `ast.Name` whose `id` is "UNUSABLE", never to the string "unusable" it evaluates to.
# `return OK` is the ABSENCE of a rule, so it is not here.
ENFORCING_VERDICT_NAMES = ("INCOMPLETE", "AMENDED", "UNUSABLE")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def run_cli_streams(mod: types.ModuleType, argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process: (exit code, stdout, stderr), never its internals."""
    if argv and argv[0] in {"emit", "amend", "finding-add", "report-write"} and "--run-dir" not in argv:
        file_value_index = argv.index("--file") + 1
        target = Path(argv[file_value_index])
        argv = [*argv[:file_value_index + 1], "--run-dir", str(target.parent), *argv[file_value_index + 1:]]
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(argv)
    except SystemExit as exc:  # argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def run_cli(mod: types.ModuleType, argv: "list[str]") -> "tuple[int, str]":
    """Drive the REAL CLI in-process and return combined output for existing diagnostics."""
    code, stdout, stderr = run_cli_streams(mod, argv)
    return code, stdout + stderr


def write_intent(d: Path, text: "str | None" = INTENT) -> None:
    """The PR's intent, beside the pass's artifacts — exactly where adoption leaves it."""
    if text is not None:
        (d / INTENT_FILE).write_text(text, encoding="utf-8")


def report_for_result(R: types.ModuleType, verdict: "str | None") -> "str | None":
    return {R.SATISFIED: SAT_REPORT, R.NOT_SATISFIED: NOT_SAT_REPORT,
            R.DEFERRED: DEFERRED_REPORT, None: None}[verdict]


def build(tmp: Path, name: str, plan: "list[str] | None", progress: "list[str] | bytes",
          findings: "list[str] | None" = None, intent: "str | None" = INTENT, *,
          progress_name: str = PROGRESS_FILE,
          report: "str | bytes | None" = SAT_REPORT) -> Path:
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
    path = d / progress_name
    if isinstance(progress, bytes):
        path.write_bytes(progress)
    else:
        path.write_text("".join(line + "\n" for line in progress), encoding="utf-8")
    if plan is not None:
        (d / PLAN_FILE).write_text("".join(line + "\n" for line in plan), encoding="utf-8")
    if findings is not None:
        findings_path = d / (progress_name[: -len(PROGRESS_SUFFIX)] + FINDINGS_SUFFIX)
        findings_path.write_text("".join(line + "\n" for line in findings), encoding="utf-8")
    if report is not None:
        report_path = d / (progress_name[: -len(PROGRESS_SUFFIX)] + REPORT_SUFFIX)
        if isinstance(report, bytes):
            report_path.write_bytes(report)
        else:
            report_path.write_text(report, encoding="utf-8")
    write_intent(d, intent)
    return path


def reads_back(mod: types.ModuleType, artifact: str, path: Path) -> "tuple[bool, str]":
    """The READ side's answer about a file a write just produced: CAN IT BE READ BACK?

    It calls the (possibly mutated) module's OWN read side — never this one's — because the question is
    always "would THIS tool read back what THIS tool wrote?", and a mutant is a tool with a rule removed.

    An exception is the loudest failure of all: the read side owes a VERDICT on any bytes, and a crash is
    not a verdict.

    Every artifact is checked through the same whole-file reader its own write door uses — the report
    included, now that it has one.
    """
    try:
        if artifact == PLAN_FILE:
            # The write door's own whole-file check, NOT `load_plan`: emptiness ("holds no units") is a
            # rule about a plan a pass is JUDGED against, not about whether the bytes read back — and a
            # waivers-only plan is a legal intermediate state (the first `plan-waive` may land before the
            # first `plan-add`; `plan-check` still refuses to dispatch against it).
            mod.check_plan_file(mod.read_text(path, "plan"), path)
            return True, "the plan reads back"
        if artifact == FINDINGS_FILE:
            mod.check_findings_file(path.read_text(encoding="utf-8"), path)
            return True, "the findings read back"
        if artifact == REPORT_FILE:
            mod.check_report_file(mod.read_text(path, "active review report"), path)
            return True, "the report reads back"
        mod.check_progress_file(mod.read_text(path, "progress file"), path,
                                lambda: mod.load_plan(mod.plan_path(path)))
        return True, "the progress file reads back"
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
            run_cli(mod, ["identity", "--file", str(progress), "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"])
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


def report_key(i: int, name: str) -> str:
    return f"[report-cli {i}] {name}"


def run_cases(mod: types.ModuleType, T: Tables, tmp: Path) -> "dict[str, tuple[str, str]]":
    """Every fixture, every name case, every CLI case, the findings family, and the two properties —
    against this (possibly mutated) module.

    A mutant that CRASHES has not returned a verdict, and "no verdict" is itself a deviation — recorded,
    never swallowed."""
    got: dict[str, tuple[str, str]] = {}
    # Every ordinary case receives one valid SATISFIED report. Cases about report framing live in
    # REPORT_CASES; findings cases choose report bytes matching their expected coherence path.
    for name, (plan, progress, _, _, _) in T.CASES.items():  # drops want, needle, why
        path = build(tmp, f"case-{name}", plan, progress)
        try:
            got[name] = mod.evaluate(path, SHA, 0, mod.SATISFIED)
        except Exception as exc:  # noqa: BLE001 - a crash IS the result here
            got[name] = (f"crash:{type(exc).__name__}", str(exc))
    for name, (plan, progress, findings, intent, verdict, _, _, _) in T.FINDING_CASES.items():  # drops want, needle, why
        path = build(tmp, f"find-{name}", plan, progress, findings, intent,
                     report=report_for_result(mod, verdict))
        try:
            got[f"[finding] {name}"] = mod.evaluate(path, SHA, 0, verdict)
        except Exception as exc:  # noqa: BLE001
            got[f"[finding] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (name, _, _, _) in enumerate(T.NAME_CASES):  # drops want, needle, why
        d = build(tmp, f"name-{i}", T.PLAN, T.WORKED).parent
        path = d / name
        path.write_text("".join(line + "\n" for line in T.WORKED), encoding="utf-8")
        if mod.NAME_RE.match(name):
            mod.report_path(path).write_text(SAT_REPORT, encoding="utf-8")
        try:
            # The pass is COMPLETE (`WORKED`) in every one of these, so it states a verdict for the same
            # reason `CASES` does — the FILENAME is what is under test here, and nothing else may refuse it.
            got[f"[name] {name}"] = mod.evaluate(path, SHA, 0, mod.SATISFIED)
        except Exception as exc:  # noqa: BLE001
            got[f"[name] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for name, case in T.REPORT_CASES.items():
        progress_name = case.get("progress_name", PROGRESS_FILE)
        path = build(tmp, case.get("dirname", f"report-{name}"), T.PLAN,
                     case.get("progress", T.WORKED), case.get("findings"), INTENT,
                     progress_name=progress_name, report=case["report"])
        for filename, content in case.get("extra_reports", {}).items():
            (path.parent / filename).write_text(content, encoding="utf-8")
        try:
            got[f"[report] {name}"] = mod.evaluate(path, SHA)
        except Exception as exc:  # noqa: BLE001
            got[f"[report] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (argv, seed, _, _, _) in enumerate(T.CLI_CASES):  # drops want, needle, why
        path = build(tmp, f"cli-{i}", T.PLAN, seed)
        extra: "list[str]" = []
        # `verify --ledger` is now REQUIRED (F2): every verify case must thread a ledger or argparse refuses
        # it before the case's own rule is ever reached. Seed a same-dir ledger with EMPTY defaults — the
        # scope every `ident()` in these seeds is bound to — so the ledger is in sync and each case still
        # exercises the rule it was written for, not the scope check. F1's existence guard is satisfied too.
        if argv[0] == "verify" and "--ledger" not in argv[1:]:
            extra = ["--ledger", str(_write_ledger(path.parent / "state.jsonl", []))]
        try:
            code, stdout, stderr = run_cli_streams(mod, [argv[0], "--file", str(path), *argv[1:], *extra])
            text = stdout + stderr
            if argv[0] == "amend" and code == 0 and stdout:
                got[cli_key(i, argv)] = ("non-empty success stdout", text)
                continue
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
    for i, (pname, argv, _, _, _) in enumerate(T.WAIVE_CLI_CASES):  # drops want, needle, why
        plan = build(tmp, f"waive-cli-{i}", T.PLAN, []).parent / pname
        try:
            code, text = run_cli(mod, ["plan-waive", "--file", str(plan), *argv])
            got[f"[waive] {pname} {' '.join(argv)}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[waive] {pname} {' '.join(argv)}"] = (f"crash:{type(exc).__name__}", str(exc))
    for name, (plan_lines, tier, _, _, _) in T.PLAN_CHECK_CASES.items():  # drops want, needle, why
        plan = build(tmp, f"plan-check-{name}", plan_lines, []).parent / PLAN_FILE
        try:
            code, text = run_cli(mod, ["plan-check", "--file", str(plan), "--tier", tier])
            got[f"[plan-check] {name}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[f"[plan-check] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (fname, argv, _, _, _) in enumerate(T.FINDING_CLI_CASES):  # drops want, needle, why
        d = build(tmp, f"find-cli-{i}", T.PLAN, T.DISPATCHED, None, INTENT).parent
        try:
            code, stdout, stderr = run_cli_streams(mod, ["finding-add", "--file", str(d / fname), *argv])
            text = stdout + stderr
            if code == 0 and stdout:
                got[find_key(i, fname)] = ("non-empty success stdout", text)
                continue
            got[find_key(i, fname)] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[find_key(i, fname)] = (f"crash:{type(exc).__name__}", str(exc))
    for i, (fname, seed, argv, _, _, _) in enumerate(T.REPORT_CLI_CASES):  # drops want, needle, why
        d = build(tmp, f"report-cli-{i}", T.PLAN, T.WORKED, report=None).parent
        target = d / fname
        if seed is not None:
            target.write_text(seed, encoding="utf-8")
        try:
            code, text = run_cli(mod, ["report-write", "--file", str(target), *argv])
            got[report_key(i, fname)] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001
            got[report_key(i, fname)] = (f"crash:{type(exc).__name__}", str(exc))
    for name, (bound_scope, intent_defaults, defaults, sibling, create, _, _) in T.LEDGER_CASES.items():  # drops want, needle
        # The identity carries the DISPATCH-TIME scope binding; the intent block carries `intent_defaults`
        # (which the tally must IGNORE); the header carries `defaults`. A resynced-but-stale case sets the
        # intent to MATCH the header while the binding names the old scope — the exact defeat.
        intent_text = mod.merge_default_non_goals(INTENT, intent_defaults, Path(INTENT_FILE))
        worked = [T.ident(default_non_goals=list(bound_scope))] + T.WORKED[1:]
        path = build(tmp, f"ledger-{name}", T.PLAN, worked, intent=intent_text)
        run_dir = path.parent
        ledger_dir = run_dir.parent / f"ledger-{name}-otherrun" if sibling else run_dir
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_file = ledger_dir / "state.jsonl"
        # `create=False` (F1) leaves the same-dir ledger MISSING but still passes `--ledger <that path>`, so
        # the existence guard is what the case probes — not a path pointing nowhere the parser could reject.
        ledger = _write_ledger(ledger_file, defaults) if create else ledger_file
        try:
            code, text = run_cli(mod, ["verify", "--file", str(path), "--head-sha", SHA,
                                       "--ledger", str(ledger)])
            got[f"[ledger] {name}"] = (f"exit{code}", text)
        except Exception as exc:  # noqa: BLE001 - the CLI owes an exit code, and a crash is not one
            got[f"[ledger] {name}"] = (f"crash:{type(exc).__name__}", str(exc))
    got.update(round_trip(mod, T, tmp))
    got.update(cross_door(mod, tmp))
    got.update(writer_path_cases(mod, T, tmp))
    return got


def expectations(T: Tables) -> "dict[str, tuple[str, str, str]]":
    """case -> (expected outcome, needle its output must contain, why the case exists)."""
    out = {n: (w, needle, why) for n, (_, _, w, needle, why) in T.CASES.items()}  # drops plan, progress
    out.update({f"[finding] {n}": (w, needle, why)
                for n, (_, _, _, _, _, w, needle, why) in T.FINDING_CASES.items()})  # drops plan, progress, findings, intent, verdict
    out.update({f"[name] {n}": (w, needle, why) for n, w, needle, why in T.NAME_CASES})
    out.update({f"[report] {n}": (c["want"], c["needle"], c["why"])
                for n, c in T.REPORT_CASES.items()})
    out.update({cli_key(i, a): (f"exit{c}", needle, why)
                for i, (a, _, c, needle, why) in enumerate(T.CLI_CASES)})  # drops seed
    out.update({f"[plan] {p} {' '.join(a)}": (f"exit{c}", needle, why)
                for p, a, c, needle, why in T.PLAN_CLI_CASES})
    out.update({f"[waive] {p} {' '.join(a)}": (f"exit{c}", needle, why)
                for p, a, c, needle, why in T.WAIVE_CLI_CASES})
    out.update({f"[plan-check] {n}": (f"exit{c}", needle, why)
                for n, (_, _, c, needle, why) in T.PLAN_CHECK_CASES.items()})  # drops plan, tier
    out.update({find_key(i, p): (f"exit{c}", needle, why)
                for i, (p, _, c, needle, why) in enumerate(T.FINDING_CLI_CASES)})  # drops argv
    out.update({report_key(i, p): (f"exit{c}", needle, why)
                for i, (p, _, _, c, needle, why) in enumerate(T.REPORT_CLI_CASES)})  # drops seed, argv
    out.update({f"[ledger] {n}": (f"exit{c}", needle,
                                  "`verify --ledger` refuses a pass whose DISPATCH-TIME pass_identity scope "
                                  "binding drifted from the run's current default_non_goals — a stale-scope "
                                  "SATISFIED never counts, even when the mutable intent was resynced to match")
                for n, (_, _, _, _, _, c, needle) in T.LEDGER_CASES.items()})  # drops bound, intent, defaults, sibling, create
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
    out.update({f"[writer-path] {name}": ("exit1", needle,
                                           "reviewer artifact writers stay inside the active run directory")
                for name, needle in (
                    ("emit", "outside"), ("amend", "outside"), ("finding-add", "outside"),
                    ("report-write", "outside"), ("relative-run-root", "absolute"),
                    ("missing-run-root", "active run directory"),
                )})
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
    types_ = {R.UNIT, R.WAIVER, R.PROGRESS, R.AMENDMENT, R.IDENTITY, R.FINDING}
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
    want = {R.UNIT, R.WAIVER, R.PROGRESS, R.AMENDMENT, R.IDENTITY, R.FINDING}
    failures = 0
    for where, n, rec in examples:
        try:
            if rec["type"] == R.UNIT:
                R.check_unit(rec, f"{where}:{n}")
            elif rec["type"] == R.WAIVER:
                R.check_waiver(rec, f"{where}:{n}")
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


def check_quiet_finding_docs() -> int:
    """The reviewer-facing finding docs must match the silent write door and its recorded fields."""
    cases = (
        ("review-prompt.txt", HERE / "review-prompt.txt",
         "successful finding writes are silent"),
        ("emit-report.py", REPORT_WRAPPER,
         "recorded fields and validation rules determine each finding's kind; successful writes are silent"),
    )
    failures = 0
    for name, path, current in cases:
        text = " ".join(path.read_text(encoding="utf-8").split())
        if current in text:
            print(f"ok       [quiet-finding] {name:24} documents silent successful writes")
        else:
            print(f"FAIL     [quiet-finding] {name} still promises a success verdict message")
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

    The WRAPPERS are the doors that are separate SCRIPTS, so their parsers are rebuilt here from the
    owner's own `add_*_args` functions — the same single definitions the wrappers call. What is actually
    EXECUTED for them is the real script, as a subprocess, so a replica cannot hide a wrapper that has
    drifted from it.
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
    amendment_wrapper = argparse.ArgumentParser(prog=AMENDMENT_WRAPPER_DOOR)
    R.add_amendment_args(amendment_wrapper)
    doors[AMENDMENT_WRAPPER_DOOR] = amendment_wrapper
    report_wrapper = argparse.ArgumentParser(prog=REPORT_WRAPPER_DOOR)
    R.add_report_args(report_wrapper)
    doors[REPORT_WRAPPER_DOOR] = report_wrapper
    return doors


def declared(p: argparse.ArgumentParser) -> "tuple[set[str], set[str]]":
    """(the flags a parser REQUIRES, the flags it accepts and does not require) — from the parser itself."""
    required: set[str] = set()
    optional: set[str] = set()
    for action in p._actions:  # noqa: SLF001 - the flags are the actions; there is no public view of them
        if action.help == argparse.SUPPRESS:
            continue
        longs = {opt for opt in action.option_strings if opt.startswith("--")} - {"--help"}
        (required if action.required else optional).update(longs)
    return required, optional


def door_script(door: str) -> Path:
    if door == WRAPPER_DOOR:
        return WRAPPER
    if door == FINDING_WRAPPER_DOOR:
        return FINDING_WRAPPER
    if door == AMENDMENT_WRAPPER_DOOR:
        return AMENDMENT_WRAPPER
    if door == REPORT_WRAPPER_DOOR:
        return REPORT_WRAPPER
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
    if door == "verify":
        (d / REPORT_FILE).write_text(SAT_REPORT, encoding="utf-8")
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
            elif flag == "--run-dir":
                argv += ["--run-dir", str(tmp / f"door-{door}-{case}")]
            elif door in ("intent-check", "verify") and flag == "--ledger":
                # `--ledger` must name a REAL ledger in the SAME run dir as the seeded --file (a static
                # FLAG_VALUES path could satisfy neither the same-dir guard nor F1's existence guard), so
                # seed one there with empty defaults — for intent-check the seeded INTENT carries no managed
                # block, and for verify the seeded pass_identity is bound to [], so empty defaults is in sync.
                seed_dir = tmp / f"door-{door}-{case}"
                seed_dir.mkdir(parents=True, exist_ok=True)
                ledger = seed_dir / "state.jsonl"
                ledger.write_text(
                    '{"type": "header", "run_id": "door", "default_non_goals": "[]"}\n', encoding="utf-8")
                argv += ["--ledger", str(ledger)]
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


def _write_ledger(path: Path, defaults: "list[str] | str") -> Path:
    """A minimal one-line ledger header naming this run's `default_non_goals`. `defaults` is a list (encoded
    to the canonical JSON-array string) or a raw string (to plant a MALFORMED value the accessor must
    refuse). `ledger.load()` back-fills every other header field, so one line is a complete header."""
    dng = json.dumps(defaults) if isinstance(defaults, list) else defaults
    path.write_text(
        json.dumps({"type": "header", "run_id": "r1", "default_non_goals": dng}) + "\n", encoding="utf-8")
    return path


def check_residual_records(R: types.ModuleType, T: Tables, tmp: Path) -> int:
    """WHAT REACHES THE RECORD — the half `REPORT_CASES` cannot see, driven END TO END through the door.

    Those cases pin what each shape of a report costs the verdict; a verdict is all `evaluate` returns, so
    they cannot say what was KEPT. A door that silently dropped every residual-risk remark would satisfy
    every one of them while quietly emptying the final report's calibration section, which is the only
    consumer these records have.

    So each case here starts at the CLI the reviewer actually runs and ends at `parse_report`: the values
    go in as argv, the record comes back out, and the rule pinned is one sentence — **every remark is
    carried VERBATIM, as its OWN element, in the order written.** Not repaired, not split into fields, not
    chosen between, and not joined to the next one. The join was a real defect: it spliced a ` | ` no
    reviewer typed into text the final report then attributed to that reviewer.

    **THE CONTROL-CHARACTER CASE IS THE ONE THIS REDESIGN EXISTS FOR.** While the report was free text,
    the remark was a LINE, and the line reader that found it ended a line at U+001C — so everything after
    that character was silently lost, and the reader could not be changed because the SAME split decided
    which line the verdict was. As an array element there is no split to lose it to.

    The last case leaves the fixtures and drives the real `verify` CLI, because the records are consumed
    through what that command PRINTS: the final report reproduces each record as `verify` reports it, so
    records kept faithfully in the artifact and mangled on the way to stdout would be the same defect one
    layer down.
    """
    # argv-visible remarks -> what the record must hold. Each is passed as one `--residual-risk` value.
    cases: "dict[str, list[str]]" = {
        "canonical": ["parser — hard"],
        "wrong-separator": ["parser contract - hard"],
        "no-separator": ["the whole diff read once"],
        "two-remarks": ["first — hard", "second — harder"],
        "none-written": [],
        # A remark holding the record separator that used to TRUNCATE it, and the newline that used to end
        # the line it lived on. Both are the encoder's problem now, and it escapes them.
        "control-character": ["parser\u001ccontract — hard"],
        "embedded-newline": ["parser — hard\nand the rest of the same remark"],
        # Leading and trailing whitespace is the reviewer's to type: there is no prose to strip a prefix
        # from any more, so nothing is stripped and the value arrives as it was passed.
        "surrounding-space": ["  parser — hard  "],
    }
    failures = 0
    for name, remarks in cases.items():
        path = build(tmp, f"residual-{name}", T.PLAN, T.WORKED, report=None)
        argv = ["report-write", "--file", str(path.parent / REPORT_FILE),
                "--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                "--summary", "Report body."]
        for remark in remarks:
            argv += ["--residual-risk", remark]
        code, text = run_cli(R, argv)
        if code != 0:
            print(f"FAIL     [residual] {name}: the door refused the call ({code}): {text.strip()}")
            failures += 1
            continue
        got = R.parse_report(path)["residual_risk"]
        if got != remarks:
            print(f"FAIL     [residual] {name}: recorded {got!r}, expected {remarks!r}")
            failures += 1
        else:
            print(f"ok       [residual] {name:36} -> {got!r}")
    failures += check_residual_rendering(R, T, tmp)
    failures += check_residual_line_breaks(R)
    return failures


def recover_residual(R: types.ModuleType, line: str) -> "list[str]":
    """The recovery `bailout-and-final-report.md` hands a driver, EXECUTED rather than described.

    Find the field the tool names (`RESIDUAL_RISK_FIELD`, never a spelling retyped here), JSON-decode the
    array that begins there, and stop where the array
    ends — `raw_decode` returns at the closing `]`, so the human-readable reason printed after it is never
    read as record text. The doc's procedure and this function must stay the same procedure: a doc that
    says one thing while the suite proves another is how a driver ends up quoting a reviewer wrongly with
    a green suite behind it.
    """
    marker = f"{R.RESIDUAL_RISK_FIELD}="
    at = line.index(marker) + len(marker)
    records, _ = json.JSONDecoder().raw_decode(line, at)
    return records


def check_residual_rendering(R: types.ModuleType, T: Tables, tmp: Path) -> int:
    """What `verify` PRINTS — the only form the final report ever copies, and therefore the only form in
    which the record boundary can survive at all.

    A complete pass (`T.PLAN` + `T.WORKED`) is required: `verify` prints the detail line only for a report
    it could parse, so a bare fixture would exercise nothing here.

    **THE PROPERTY IS LOSSLESSNESS, NOT PRESENCE.** "Both records appear in the line" is satisfied by a
    rendering that has already destroyed the boundary between them, which is exactly how the joined form
    passed: `first-vs-one-record` below is TWO reports whose record lists differ (one element against
    two), and under any separator-joined rendering their detail lines are BYTE-IDENTICAL. So every case
    decodes the printed field back (`recover_residual`) and requires it to equal what `parse_report`
    returned for that same report, and the pair is additionally required to print DIFFERENTLY.

    `punctuated` carries `]`, `"`, `\\`, the old `; ` and a U+001C inside ONE record: a rendering that
    survives a tidy record and loses one holding its own delimiter — or one holding the character that
    used to TRUNCATE it while the report was free text — is a rendering that fails on precisely the
    reviewer whose words most need carrying.

    `unicode-line-breaks` is the same demand made of the three terminators the ENCODER does not
    escape (U+0085, U+2028, U+2029): they are not C0 controls, so a record holding one printed it
    literally and the detail line became TWO — the `len(lines) != 1` guard below is what caught it,
    and `recover_residual` could not decode the truncated first line at all.
    `check_residual_line_breaks` makes that whole CLASS mechanical rather than these three.

    Each case also pins that stdout stays ONE line, since the detail is a field of a single printed line.
    """
    reports = {
        # THE DEFECT'S OWN REPRODUCTION, both halves. One reviewer wrote a single remark that happens to
        # contain the separator; the other wrote two remarks. The ARTIFACT tells them apart by
        # construction now; before this rendering, `verify` printed the same bytes for both.
        "one-record": report_line("satisfied", residual=["first — hard; second — harder"]),
        "two-records": report_line("satisfied", residual=["first — hard", "second — harder"]),
        "punctuated": report_line(
            "satisfied", residual=['parser["x"] — holds ], a quote, a \\, a ; and a \u001c too']),
        # THE THREE LINE TERMINATORS THE ENCODER DOES NOT ESCAPE FOR US. They are not C0 controls,
        # so `ensure_ascii=False` printed them AS THEMSELVES: the detail line became TWO lines, and
        # `recover_residual` — which reads the field out of the FIRST one, exactly as the doc hands
        # the procedure to a driver — died on an unterminated string. Each record reaches this
        # rendering through the ordinary door: the artifact's own write escapes them, so a report
        # holding one is valid, one line, and accepted.
        "unicode-line-breaks": report_line("satisfied", residual=[
            "next-line \u0085 and the rest of the same remark",
            "line-separator \u2028 and the rest of the same remark",
            "paragraph-separator \u2029 and the rest of the same remark"]),
        # The field is printed even when the reviewer wrote nothing, so a reader never has to tell "no
        # records" apart from "the field is somewhere else".
        "none-written": report_line("satisfied"),
    }
    problems: "list[str]" = []
    printed: "dict[str, str]" = {}
    for name, text in reports.items():
        path = build(tmp, f"residual-render-{name}", T.PLAN, T.WORKED, report=text)
        ledger = _write_ledger(path.parent / "state.jsonl", [])
        code, out = run_cli(R, ["verify", "--file", str(path), "--head-sha", SHA,
                                "--ledger", str(ledger)])
        if code != 0:
            problems.append(f"{name}: verify exited {code}, so it never reached the detail line: "
                            f"{out.strip()!r}")
            continue
        lines = out.splitlines()
        if len(lines) != 1:
            problems.append(f"{name}: verify printed {len(lines)} lines, not one: {out!r}")
            continue
        printed[name] = lines[0]
        want = R.parse_report(path)["residual_risk"]
        try:
            got = recover_residual(R, lines[0])
        except (ValueError, json.JSONDecodeError) as exc:
            problems.append(f"{name}: the printed field does not decode ({exc}): {lines[0]!r}")
            continue
        if got != want:
            problems.append(f"{name}: the line decodes to {got!r}, but the pass recorded {want!r}: "
                            f"{lines[0]!r}")
        elif " | " in out:
            problems.append(f"{name}: the output splices a delimiter no reviewer wrote: {lines[0]!r}")
        else:
            print(f"ok       [residual] {'rendered ' + name:36} -> {lines[0].strip()!r}")
    if printed.get("one-record") is not None and printed["one-record"] == printed.get("two-records"):
        problems.append("one record and two records print the SAME line, so the boundary between the "
                        f"reviewer's remarks cannot be recovered: {printed['one-record']!r}")
    elif "one-record" in printed and "two-records" in printed:
        print(f"ok       [residual] {'one record != two records':36} -> the detail lines differ")
    # THE FIELD NAME IS ONE FACT IN TWO PLACES — the tool that prints it and the reference that tells a
    # driver to look for it — so the link is made MECHANICAL rather than left to a sweep. Rename
    # `RESIDUAL_RISK_FIELD` without touching that reference and a driver hunts a field that is no longer
    # printed; nothing above this line would notice, because every case here asks the tool what it calls
    # the field.
    doc = OWNER.parent.parent / "references" / "bailout-and-final-report.md"
    if f"{R.RESIDUAL_RISK_FIELD}=" in doc.read_text(encoding="utf-8"):
        print(f"ok       [residual] {'final report names the field':36} -> {doc.name}")
    else:
        problems.append(f"{doc.name} no longer names the `{R.RESIDUAL_RISK_FIELD}=` field `verify` "
                        f"prints, so its recovery procedure points at nothing")
    for problem in problems:
        print(f"FAIL     [residual] {problem}")
    return len(problems)


def check_residual_line_breaks(R: types.ModuleType) -> int:
    """EVERY character `str.splitlines()` ends a line at, WALKED rather than listed.

    The rendered field is read as ONE line: `bailout-and-final-report.md` tells a driver to find
    `residual-risk=` on `verify`'s detail line and decode the array that starts there, and
    `recover_residual` above executes that procedure. So the property is not "the three characters we
    remembered are escaped" but **no character a record may legally hold can end that line** — and the
    set of such characters belongs to `str.splitlines()`, not to a list anybody maintains here.

    So the set is DERIVED, by walking every code point and asking what splits. That is what makes the
    module's `UNESCAPED_LINE_BREAKS` a checked claim rather than a remembered one: this reconciles it
    against the terminators a bare `ensure_ascii=False` encoding leaves literal, so a Python that
    recognises one more terminator, or an encoder change that stops escaping one, FAILS here instead of
    silently splitting a reviewer's record in production.

    U+0085, U+2028 and U+2029 are additionally named: they are the three the defect was reported with,
    and requiring them to be IN the derived set is what stops this case going vacuous if the walk ever
    finds nothing.
    """
    breaks = [chr(c) for c in range(0x110000) if len(f"a{chr(c)}b".splitlines()) > 1]
    problems: "list[str]" = []
    for named in ("\u0085", "\u2028", "\u2029"):
        if named not in breaks:
            problems.append(f"U+{ord(named):04X} is not in the derived terminator set, so this case no "
                            f"longer covers the characters the defect was reported with")
    # What a bare `ensure_ascii=False` encoding leaves LITERAL — everything else is a C0 control the
    # encoder escapes on its own. This must be exactly the set `render_residual` escapes by hand.
    naive = json.dumps(breaks, ensure_ascii=False)
    unescaped = {c for c in breaks if c in naive}
    if unescaped != set(R.UNESCAPED_LINE_BREAKS):
        problems.append(f"the encoder leaves {sorted(hex(ord(c)) for c in unescaped)} literal, but "
                        f"UNESCAPED_LINE_BREAKS names "
                        f"{sorted(hex(ord(c)) for c in R.UNESCAPED_LINE_BREAKS)}")
    for ch in breaks:
        record = f"parser{ch}contract — hard"
        rendered = R.render_residual([record])
        if len(rendered.splitlines()) != 1:
            problems.append(f"a record holding U+{ord(ch):04X} renders as "
                            f"{len(rendered.splitlines())} lines, so the detail line splits and the "
                            f"pinned recovery reads a truncated field: {rendered!r}")
        elif json.loads(rendered) != [record]:
            problems.append(f"a record holding U+{ord(ch):04X} does not decode back to itself: "
                            f"{json.loads(rendered)!r}")
    for problem in problems:
        print(f"FAIL     [residual] {problem}")
    if not problems:
        print(f"ok       [residual] {'every splitlines terminator survives':36} -> "
              f"{len(breaks)} derived, {len(R.UNESCAPED_LINE_BREAKS)} escaped by hand")
    return len(problems)


def check_intent_door(R: types.ModuleType, tmp: Path) -> int:
    """Drive intent-check through its real CLI: structural refusals, AND the run-default sync enforcement.

    Each case gets its own run dir holding the intent and the run's `state.jsonl` (same dir, as the real
    flow lays them out). The sync cases are what pin `check_default_non_goals`: remove that enforcement and
    `missing-default`/`stale-default`/`malformed-managed` all go from exit 1 to exit 0, and these fail.
    """
    in_sync = R.merge_default_non_goals(INTENT, ["run default X"], Path("intent.md"))
    stale = R.merge_default_non_goals(INTENT, ["an old default"], Path("intent.md"))
    dup_block = in_sync.replace(
        R.MANAGED_END, R.MANAGED_END + "\n" + R.MANAGED_START + "\n- dup\n" + R.MANAGED_END, 1)
    # F4: an EMPTY-but-PRESENT managed block (markers, no bullets) under [] defaults. The bullet check reads
    # `have == [] == desired` and would PASS it, but the format requires NO block when defaults are empty
    # (intent-sync removes it) — so the presence test must refuse it.
    empty_present = INTENT.replace(
        "## Non-goals\n", "## Non-goals\n" + R.MANAGED_START + "\n" + R.MANAGED_END + "\n", 1)
    # name -> (intent text, ledger defaults, want exit, needle). A None ledger-defaults means "no ledger row
    # field", i.e. empty defaults.
    cases: "dict[str, tuple[str, list[str], int, str]]" = {
        "usable-empty-defaults": (INTENT, [], 0, "usable intent block"),
        "missing-threat-model": ("## Purpose\n- do the work\n\n## Non-goals\n", [], 1,
                                 "missing ['## Threat model']"),
        "in-sync": (in_sync, ["run default X"], 0, "in sync with 1 run default"),
        "missing-default": (INTENT, ["run default X"], 1, "OUT OF SYNC"),
        "stale-default": (stale, ["run default X"], 1, "OUT OF SYNC"),
        "malformed-managed": (dup_block, ["run default X"], 1, "appears more than once"),
        "empty-but-present-block": (empty_present, [], 1, "NO managed block"),
    }
    failures = 0
    for name, (content, defaults, want, needle) in cases.items():
        d = tmp / f"intent-door-{name}"
        d.mkdir(parents=True, exist_ok=True)
        path = d / INTENT_FILE
        path.write_text(content, encoding="utf-8")
        ledger = _write_ledger(d / "state.jsonl", defaults)
        code, output = run_cli(R, ["intent-check", "--file", str(path), "--ledger", str(ledger)])
        if code != want or needle not in output:
            print(f"FAIL     [intent-check] {name}: exit {code}, expected {want}; output: {output.strip()}")
            failures += 1
        else:
            print(f"ok       [intent-check] {name:24} exit {code}: {needle}")

    # A malformed ledger `default_non_goals` fails CLOSED — the defaults cannot be read, so the intent
    # cannot be judged against them. `not-json{` is a present STRING that never parses. A present JSON
    # `null` and a present native JSON array `[]` are the two values that once failed OPEN (both collide
    # with the canonical `"[]"` if coerced): `load()` now preserves each RAW, so the decode door refuses
    # them here too. Each is written as a RAW header value (not via `_write_ledger`, which JSON-encodes
    # lists to the valid string form and so cannot plant a native array). Only a genuinely MISSING key
    # back-fills to `[]` (covered by the missing-ledger / back-fill cases elsewhere).
    for label, raw in (("malformed-ledger", "not-json{"),
                       ("null-ledger", None), ("native-array-ledger", [])):
        d = tmp / f"intent-door-{label}"
        d.mkdir(parents=True, exist_ok=True)
        (d / INTENT_FILE).write_text(INTENT, encoding="utf-8")
        state = d / "state.jsonl"
        state.write_text(
            json.dumps({"type": "header", "run_id": "r1", "default_non_goals": raw}) + "\n", encoding="utf-8")
        code, output = run_cli(R, ["intent-check", "--file", str(d / INTENT_FILE), "--ledger", str(state)])
        if code != 1 or "malformed" not in output:
            print(f"FAIL     [intent-check] {label}: exit {code}, expected 1; output: {output.strip()}")
            failures += 1
        else:
            print(f"ok       [intent-check] {label:24} exit {code}: fails closed")

    # The pass-5 repro: an UNRELATED `ledger.py header set reviewer codex` write against a
    # `default_non_goals: null` store must NOT flip intent-check from fail-closed to green. `dump()` once
    # HEALED the malformed value to `"[]"` on that write, so a reload decoded clean and intent-check exited
    # 0 — a false green over a hand-edited fail-closed store. Run the write through the REAL ledger CLI (a
    # subprocess, as the campaign does), then assert intent-check STILL exits nonzero and the on-disk value
    # is still `null` (never healed to `"[]"`).
    d = tmp / "intent-door-heal-on-write"
    d.mkdir(parents=True, exist_ok=True)
    (d / INTENT_FILE).write_text(INTENT, encoding="utf-8")
    state = d / "state.jsonl"
    state.write_text(
        json.dumps({"type": "header", "run_id": "r1", "default_non_goals": None}) + "\n", encoding="utf-8")
    write = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(HERE / "ledger.py"), "--file", str(state), "header", "set", "reviewer", "codex"],
        capture_output=True, text=True, check=False)
    on_disk = json.loads(state.read_text().splitlines()[0]).get("default_non_goals", "<missing>")
    code, output = run_cli(R, ["intent-check", "--file", str(d / INTENT_FILE), "--ledger", str(state)])
    if write.returncode != 0:
        print(f"FAIL     [intent-check] heal-on-write: the unrelated `header set reviewer` failed: "
              f"{(write.stdout + write.stderr).strip()}")
        failures += 1
    elif on_disk is not None:
        print(f"FAIL     [intent-check] heal-on-write: the unrelated write HEALED default_non_goals on disk "
              f"to {on_disk!r} instead of leaving it null")
        failures += 1
    elif code == 0 or "malformed" not in output:
        print(f"FAIL     [intent-check] heal-on-write: exit {code} after an unrelated write "
              f"(expected still-nonzero fail-closed); output: {output.strip()}")
        failures += 1
    else:
        print(f"ok       [intent-check] {'heal-on-write':24} exit {code}: still fails closed after unrelated write")

    # The ledger and intent MUST share a run directory — a mismatch is the operator's error (exit 2).
    d = tmp / "intent-door-crossrun"
    (d / "run-a").mkdir(parents=True, exist_ok=True)
    (d / "run-b").mkdir(parents=True, exist_ok=True)
    (d / "run-a" / INTENT_FILE).write_text(INTENT, encoding="utf-8")
    ledger = _write_ledger(d / "run-b" / "state.jsonl", [])
    code, output = run_cli(R, ["intent-check", "--file", str(d / "run-a" / INTENT_FILE),
                               "--ledger", str(ledger)])
    if code != 2 or "same run directory" not in output:
        print(f"FAIL     [intent-check] cross-run: exit {code}, expected 2; output: {output.strip()}")
        failures += 1
    else:
        print(f"ok       [intent-check] {'cross-run-dir':24} exit {code}: refused")

    # F1: a same-dir --ledger that DOES NOT EXIST must fail CLOSED (exit 2). `ledger.load` back-fills a
    # missing file to the header defaults ([] — zero run defaults), so without the existence guard a typo'd
    # ledger path would read as "no scope" and wave a stale managed block through.
    d = tmp / "intent-door-missing-ledger"
    d.mkdir(parents=True, exist_ok=True)
    (d / INTENT_FILE).write_text(INTENT, encoding="utf-8")
    code, output = run_cli(R, ["intent-check", "--file", str(d / INTENT_FILE),
                               "--ledger", str(d / "state.jsonl")])  # never written
    if code != 2 or "does not exist" not in output:
        print(f"FAIL     [intent-check] missing-ledger: exit {code}, expected 2; output: {output.strip()}")
        failures += 1
    else:
        print(f"ok       [intent-check] {'missing-ledger':24} exit {code}: fails closed")
    return failures


def check_amendment_door(R: types.ModuleType, tmp: Path) -> int:
    """The amendment write door, END-TO-END: a line it writes is one `verify` reads back as `amended`.

    The refusal shapes are pinned by the CLI/round-trip families; what only an end-to-end can show is the
    WHOLE arc the fix exists for — the reviewer raises the amendment through the door, and the SAME `verify`
    that used to throw the pass away for a hand-written `{type, gap}` line now routes it `amended`. It drives
    BOTH the owner's `amend` subcommand and the reviewer-facing `emit-amendment.py` shim (a subprocess, as a
    caller runs it), so the shim's resolve-and-forward is exercised, not replicated.
    """
    failures = 0

    def seed(name: str) -> Path:
        d = tmp / f"amend-e2e-{name}"
        d.mkdir(parents=True, exist_ok=True)
        (d / PLAN_FILE).write_text("".join(line + "\n" for line in Tables(R).PLAN), encoding="utf-8")
        write_intent(d)
        return d

    # 1) The owner's door: raise the amendment, then the parsed DEFERRED report must route `amended`.
    d = seed("owner")
    progress = d / PROGRESS_FILE
    run_cli(R, ["identity", "--file", str(progress), "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"])
    code, stdout, stderr = run_cli_streams(R, ["amend", "--file", str(progress), "--reason", "no unit covers the harness",
                                               "--id", "u09", "--kind", "file", "--target", "harness.py", "--check", "it runs"])
    if code != 0 or stdout:
        print(f"FAIL     [amend] the owner's valid door was not quiet (exit {code}, stdout={stdout!r}): "
              f"{stderr.strip()}")
        failures += 1
    (d / REPORT_FILE).write_text(DEFERRED_REPORT, encoding="utf-8")
    # `verify --ledger` is REQUIRED (F2); the identity above is bound to [], so a same-dir []-defaults ledger
    # is in sync and the pass routes on its own merits, not the scope check.
    ledger = _write_ledger(d / "state.jsonl", [])
    code, out = run_cli(R, ["verify", "--file", str(progress), "--head-sha", SHA, "--ledger", str(ledger)])
    if code != 1 or R.AMENDED not in out:
        print(f"FAIL     [amend] `verify` did not route the DEFERRED report to {R.AMENDED!r} after a written "
              f"amendment (exit {code}): {out.strip()}")
        failures += 1
    else:
        print(f"ok       [amend] {'owner door -> verify':24} a written amendment routes `{R.AMENDED}`")

    # 2) The no-identity refusal writes NOTHING — the file the write door declined to grow is byte-for-byte
    #    what it was. (The CLI family pins the exit code and message; this pins that no bytes landed.)
    d = seed("no-identity")
    progress = d / PROGRESS_FILE
    progress.write_bytes(b"")
    code, out = run_cli(R, ["amend", "--file", str(progress), "--reason", "gap", "--id", "u09",
                            "--kind", "file", "--target", "x.py", "--check", "a"])
    if code == 0 or progress.read_bytes() != b"":
        print(f"FAIL     [amend] a refused amendment (no pass_identity) still changed the file "
              f"(exit {code}, {len(progress.read_bytes())} byte(s)): {out.strip()}")
        failures += 1
    else:
        print(f"ok       [amend] {'no-identity refusal':24} refused and NOTHING was written")

    # 3) The reviewer-facing shim, as a caller runs it (subprocess) — its line `verify` accepts.
    d = seed("shim")
    progress = d / PROGRESS_FILE
    run_cli(R, ["identity", "--file", str(progress), "--head-sha", SHA, "--dispatched-at", TS, "--default-non-goals", "[]"])
    run = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(AMENDMENT_WRAPPER), "--run-dir", str(d), "--file", str(progress), "--reason", "harness gap",
         "--id", "u09", "--kind", "docs", "--target", "y.md", "--check", "b"],
        capture_output=True, text=True, check=False)
    if run.returncode != 0 or run.stdout:
        print(f"FAIL     [amend] the `emit-amendment.py` shim was not quiet for a valid amendment "
              f"(exit {run.returncode}, stdout={run.stdout!r}): {run.stderr.strip()}")
        failures += 1
    (d / REPORT_FILE).write_text(DEFERRED_REPORT, encoding="utf-8")
    ledger = _write_ledger(d / "state.jsonl", [])  # in-sync []-scope ledger for the now-required --ledger (F2)
    code, out = run_cli(R, ["verify", "--file", str(progress), "--head-sha", SHA, "--ledger", str(ledger)])
    if code != 1 or R.AMENDED not in out:
        print(f"FAIL     [amend] `verify` did not accept the shim's line as {R.AMENDED!r} "
              f"(exit {code}): {out.strip()}")
        failures += 1
    else:
        print(f"ok       [amend] {'emit-amendment.py shim':24} its line verifies `{R.AMENDED}`")
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
            # Spelled out rather than taken from `_gauntlet.clock.TS_FORMAT` ON PURPOSE. The fixtures above
            # write their `mtimes` keys as literal strings, so reading them back through the constant the
            # tool itself uses would make a wrong edit to that constant agree with itself and pass. This
            # copy is the independent pin; it is the one place the literal is allowed to live outside its
            # owner.
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


# The docs that tell an agent what an UNWRITABLE run root means, and the fact each must carry. The write
# door's diagnostic (`_append_line`) is the OWNER; these are the copies an agent actually reads before it
# is ever launched, and they used to describe the recovery this root removed — a deferral the reviewer
# cannot deliver, because a deferral is a report and the report lands under the same shut door. A needle
# per file, so deleting or rewriting the correction FAILS here rather than sending the next reviewer to
# write a file it has just been told it cannot.
READONLY_DOC_NEEDLES = {
    "cross-agent-reviewers.md": "report cannot land",
    "stage-2-review-gate.md": "report cannot land",
}


def check_readonly_docs() -> int:
    """The read-only-run-root RULE, checked where agents READ it — not only where it runs.

    `check_unwritable_target` below pins what the TOOL says. That is half the rule: a reviewer follows the
    reference docs, and a doc still describing the retired deferral sends it to attempt something that
    cannot happen. Neither half implies the other, so both are checked.
    """
    refs = OWNER.parent.parent / "references"
    problems = 0
    for name, needle in sorted(READONLY_DOC_NEEDLES.items()):
        text = (refs / name).read_text(encoding="utf-8").lower()
        if needle.lower() in text:
            print(f"ok       [unwritable] {name:36} -> states that the report cannot land either")
        else:
            print(f"FAIL     [unwritable] {name} no longer says the report cannot land under a read-only "
                  f"run root, so its account of what a reviewer can still do is unpinned")
            problems += 1
    return problems


def check_unwritable_target(R: types.ModuleType, T: Tables, tmp: Path) -> int:
    """The EROFS/EACCES write-door diagnostic: an emit into an UNWRITABLE target must REFUSE with the -C
    diagnosis, not die with a bare `OSError` that names no cause.

    **THE REAL DEFECT.** A codex reviewer launched with `-C` at the candidate worktree made the run dir
    read-only under `workspace-write`; every `emit-progress.py` append failed with `OSError: [Errno 30]
    Read-only file system`, the reviewer deferred with a bare "progress file is read-only", the
    orchestrator re-dispatched the SAME wrong command, and a ~20-minute pass was lost each time. The write
    door now translates that OS failure into a DISPATCH-fault diagnostic naming the `-C` target and what
    the reviewer can still do about it.

    **THE RECOVERY IT NAMES CHANGED WITH THE REPORT, AND SAYING SO IS THE POINT.** It used to tell the
    reviewer to defer in its report's terminal line, which worked because an external process's final
    output WAS the report. The report is written through a door under that same unwritable root now, so
    the reviewer cannot deliver one at all: its final MESSAGE is the only channel left, the attempt is
    unusable for having no report, and the orchestrator relaunches or parks. A diagnostic that still
    promised the old recovery would send a reviewer to write a file it has just been told it cannot.

    We cannot mount a read-only filesystem in a unit test, so we reproduce the same errno family the door
    treats identically: appending to a `0o444` file raises `EACCES`, the sibling of the real `EROFS`.
    `chmod` does not restrict root, so under euid 0 (or a platform without `geteuid`) the condition cannot
    be created and the case skips cleanly — the diagnostic is behavioral, not a gate rule, so a skipped
    probe is honest rather than a false green.
    """
    failures = check_readonly_docs()
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        print("skip     [unwritable] chmod does not restrict root — the EROFS/EACCES write door "
              "cannot be probed here")
        return failures
    d = tmp / "unwritable"
    d.mkdir(parents=True, exist_ok=True)
    progress = d / PROGRESS_FILE
    # A validly dispatched-but-empty pass: pass_identity as the first line, a one-unit plan, and a sound
    # intent beside it — so emit clears every READ-side check and reaches the write door, which is the one
    # thing under test here.
    progress.write_text(T.ident() + "\n", encoding="utf-8")
    (d / PLAN_FILE).write_text(T.unit("u01") + "\n", encoding="utf-8")
    write_intent(d)
    progress.chmod(0o444)
    try:
        code, text = run_cli(R, ["emit", "--file", str(progress), "--unit", "u01", "--status", "started"])
    finally:
        progress.chmod(0o644)  # restore so the TemporaryDirectory cleanup can remove it
    if code == 0:
        print("FAIL     [unwritable] emit into a read-only progress file EXITED 0 — the write door "
              "swallowed the OSError instead of raising the DISPATCH-fault diagnostic")
        return failures + 1
    if "-C" not in text or "final message" not in text:
        print(f"FAIL     [unwritable] emit refused (exit {code}) but its message names neither the `-C` "
              f"target nor the still-available recovery:\n         {text.strip()}")
        return failures + 1
    if "DEFERRED" in text:
        print(f"FAIL     [unwritable] the diagnostic still promises a report-borne DEFERRED recovery, but "
              f"the report is written under the same unwritable root:\n         {text.strip()}")
        return failures + 1
    print(f"ok       [unwritable] emit into a read-only target -> exit {code}, DISPATCH-fault diagnostic "
          f"names -C and the final-message recovery")
    return failures


def writer_path_cases(R: types.ModuleType, T: Tables, tmp: Path) -> "dict[str, tuple[str, str]]":
    """Return mutation-matrix cases for reviewer destinations outside their bound run root."""
    root = tmp / "writer-case-root"
    outside = tmp / "writer-case-outside"
    root.mkdir()
    cases = (
        ("emit", ["--unit", "u01", "--status", "started"], PROGRESS_FILE),
        ("amend", ["--reason", "gap", "--id", "u09", "--kind", "file", "--target", "x.py",
                    "--check", "a"], PROGRESS_FILE),
        ("finding-add", T.FINDING_CLI_CASES[0][1], FINDINGS_FILE),
        ("report-write", ["--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                           "--summary", "Report body."], REPORT_FILE),
    )
    got: "dict[str, tuple[str, str]]" = {}
    for command, args, filename in cases:
        target = outside / filename
        code, text = run_cli(R, [command, "--file", str(target), "--run-dir", str(root), *args])
        got[f"[writer-path] {command}"] = (f"exit{code}", text)

    for name, run_dir in (("relative-run-root", Path("relative-run-root")),
                          ("missing-run-root", tmp / "missing-run-root")):
        target = (root if name == "relative-run-root" else run_dir) / REPORT_FILE
        code, text = run_cli(R, ["report-write", "--file", str(target), "--run-dir", str(run_dir),
                                 "--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                                 "--summary", "Report body."])
        got[f"[writer-path] {name}"] = (f"exit{code}", text)
    return got


def check_writer_path_boundaries(R: types.ModuleType, T: Tables, tmp: Path) -> int:
    """Every reviewer writer rejects a valid artifact basename outside the bound run root."""
    root = tmp / "writer-root"
    outside = tmp / "writer-outside"
    root.mkdir()
    cases = (
        ("emit", ["--unit", "u01", "--status", "started"], PROGRESS_FILE),
        ("amend", ["--reason", "gap", "--id", "u09", "--kind", "file", "--target", "x.py",
                    "--check", "a"], PROGRESS_FILE),
        ("finding-add", T.FINDING_CLI_CASES[0][1], FINDINGS_FILE),
        ("report-write", ["--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                           "--summary", "Report body."], REPORT_FILE),
    )
    failures = 0
    for command, args, filename in cases:
        target = outside / filename
        code, text = run_cli(R, [command, "--file", str(target), "--run-dir", str(root), *args])
        if code == 0 or target.exists() or outside.exists():
            print(f"FAIL     [writer-path] {command} accepted or created {target} outside {root}: {text.strip()}")
            failures += 1
        else:
            print(f"ok       [writer-path] {command} rejects a destination outside the active run directory")

    for run_dir, needle in ((Path("relative-run-root"), "absolute"),
                            (tmp / "missing-run-root", "active run directory")):
        target = (root if run_dir.name == "relative-run-root" else run_dir) / REPORT_FILE
        code, text = run_cli(R, ["report-write", "--file", str(target), "--run-dir", str(run_dir),
                                 "--verdict", R.SATISFIED, "--deferred-reason", R.NO_DEFERRED_REASON,
                                 "--summary", "Report body."])
        if code == 0 or needle not in text:
            print(f"FAIL     [writer-path] run root {run_dir} was not rejected: {text.strip()}")
            failures += 1
        else:
            print(f"ok       [writer-path] run root {run_dir} is rejected")
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

    # The MECHANICAL half of "this suite builds fixtures where the tool looks". Every artifact name in
    # this file is assembled from the three suffixes above, and the report's has already changed once —
    # so they are reconciled against the owner's own constants rather than trusted to have been swept.
    for name, mine in (("PROGRESS_SUFFIX", PROGRESS_SUFFIX), ("FINDINGS_SUFFIX", FINDINGS_SUFFIX),
                       ("REPORT_SUFFIX", REPORT_SUFFIX)):
        theirs = getattr(R, name)
        if mine != theirs:
            print(f"FAIL     [suffix] this suite spells {name} {mine!r}; review-pass.py says {theirs!r} — "
                  f"every fixture below is built at a path the tool does not read")
            failures += 1

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
    failures += check_quiet_finding_docs()
    print()
    failures += check_residual_records(R, T, tmp)
    print()
    failures += check_intent_door(R, tmp)
    print()
    failures += check_amendment_door(R, tmp)
    print()
    failures += run_status_cases(R, T, tmp)
    print()
    failures += check_unwritable_target(R, T, tmp)
    print()
    failures += check_writer_path_boundaries(R, T, tmp)
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the review-pass contract is broken.")
        return 1
    doors = ("the door checks were SKIPPED (this run is the `self-test` door's own probe)" if probe else
             f"every one of the {len(T.DOOR_SEEDS)} doors ({', '.join(sorted(T.DOOR_SEEDS))}) had the "
             f"MINIMAL invocation its OWN `--help` advertises EXECUTED, and it runs")
    print(f"all {len(T.CASES)} fixtures + {len(T.FINDING_CASES)} findings/intent fixtures + "
          f"{len(T.REPORT_CASES)} report fixtures + "
          f"{len(T.REPORT_CLI_CASES)} report-write CLI cases + "
          f"{len(T.NAME_CASES)} name cases + "
          f"{len(T.CLI_CASES) + len(T.PLAN_CLI_CASES) + len(T.WAIVE_CLI_CASES) + len(T.PLAN_CHECK_CASES) + len(T.FINDING_CLI_CASES)} CLI cases + "
          f"{len(T.LEDGER_CASES)} verify --ledger dispatch-scope-binding cases + "
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
