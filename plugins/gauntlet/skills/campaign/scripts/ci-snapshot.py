#!/usr/bin/env python3
"""Executable contract for the CI snapshot artifact (ci-derivation-spec.md).

The rules in `ci-derivation-spec.md` are prose, and prose cannot be run. Three defects shipped in that
prose anyway, and every one of them would have died instantly against a round-trip:

  * the artifact was space-delimited while check names CONTAIN spaces (`Lint scripts` parsed as
    name=`Lint`, app_id=`scripts`) — the format could not be read back at all;
  * the SHA verification compared a literal we had stamped ourselves against the value we stamped
    it from — it matched by construction and COULD NEVER FAIL;
  * containment compared check-run NAMES as a set, and names are not unique, so a snapshot missing
    a failing run still passed.

And then THIS file shipped several of its own, every one of the same family — evidence that is PRESENT
but NOT COUNTED parses as "nothing wrong":

  * `parse()` SKIPPED blank lines and admitted ANY object with a `row` key, so a row of an unknown type
    was silently DISCARDED — including a FAILING one. A passing check run plus a FAILING row the tool
    did not recognise verified GREEN;
  * the VERIFY rule requires the header AND every evidence row's sha AND **the FILENAME** to equal the
    ledger's head_sha. Only the first two were checked, so green bytes MISFILED under a superseded
    commit's name verified green;
  * then the filename check itself only asked whether the expected sha appeared SOMEWHERE among the
    name's hyphen-delimited parts, so `ci-35-<head_sha>-<old_sha>.txt` — a name that points at TWO
    commits — verified green. A second sha in the name is present, and was not counted;
  * the header was required to exist EXACTLY ONCE but never to be FIRST, so a header-second artifact
    verified green — and a SECOND header, naming another commit, was present and not counted;
  * a `witness` row carrying a `sha` was ACCEPTED and then IGNORED. The rollup carries no commit oid,
    so that sha is one we INVENTED — fabricated evidence, which the contract forbids outright — and the
    parser let it through by only ever asking whether the REQUIRED fields were there, never whether an
    UNEXPECTED one was. An unexpected field is as much "present but not counted" as an unknown row type.

  * the FILENAME rule accepted `ci-35-1499C72…C971.txt` — an UPPERCASE sha, which no producer of ours can
    emit — because the pattern said `[0-9a-fA-F]` and the comparison case-folded. "Close enough" is the
    substring bug one level down;
  * and a malformed row did not always get a VERDICT at all: `{"row":{"kind":"status"}}` reached a HASH
    lookup and raised `TypeError: unhashable type: 'dict'`. An unhandled exception is the tool FAILING TO
    HAVE AN OPINION — not `unusable`, not anything. A CRASH IS NOT A VERDICT, and "malformed input yields
    a verdict" is not a property you get one `isinstance` at a time: the type check belongs at the
    boundary, once, as a CLASS.

  * and then the rules were defeated ONE LEVEL BELOW themselves, by the DECODER. `json.loads` keeps the
    LAST value of a REPEATED member name and silently DISCARDS the earlier one, so the discarded value
    never reached a single rule above: `{"row":"header","sha":"<old>","sha":"<head>"}` verified GREEN with
    a stale commit sitting in the bytes, and `{"row":"status_context","row":"checkrun",…}` verified GREEN
    with the REJECTED row type vanished. Every rule in this file checks the EXACT shape of a dict it was
    handed — and none of them can check what the decoder threw away before they ran;
  * and a line nested thousands of levels deep raised `RecursionError` straight out of `json.loads` —
    a CRASH, again, where a verdict was owed.

  * and then the SAME defect turned up one level ABOVE every rule here, in the ARTIFACT'S OWN SHAPE. The
    contract opens with "BOTH families are MANDATORY. A source you never queried reports nothing, and
    'nothing' parses as 'nothing wrong'" — and the artifact could not express that rule. "The commit-status
    fetch RAN and this commit has zero statuses" and "the commit-status fetch was SKIPPED, or died before
    appending anything" produced the BYTE-IDENTICAL file: no `status` rows. So a check-runs-only snapshot
    of all-passing rows verified GREEN while a MANDATORY source had never been queried, with a failing
    Jenkins status — INVISIBLE to /check-runs by design — sitting on the commit, unread. The file's own
    founding principle, unenforced by its own artifact. `source` rows (below) are what make an ABSENCE
    say "we do not know" instead of "nothing wrong".

  * and then ONE LEVEL ABOVE THE ARTIFACT ENTIRELY — the last one, and the only one no rule here could
    ever have caught, because it is not a defect IN the file. THE REGISTRATION GAP: every rule above
    quantifies over the rows that ARE in the snapshot, and a REQUIRED check that has not registered yet is
    NOT A FAILING ROW — IT IS NO ROW. So a snapshot could be perfectly formed, every marker present, every
    row PASSING, containment holding — and still be silent about a required check that never showed up.
    `green` meant "everything that had registered by the time we looked had passed", which is a statement
    about WHAT SHOWED UP, while every caller read it as a statement about WHAT WAS REQUIRED. It was known,
    and it was DISCLOSED IN A COMMENT — which merges the PR just the same: a disclaimer printed beside a
    green is not a disclosure, it is a trapdoor with a sign on it. The fix is the only one available: STOP
    LOOKING ONLY AT THE FILE. Read what the base branch REQUIRES and pass it in (`--required-set`).

The lesson is one lesson, not nine: check the EXACT shape, and against the EXPECTED set. "The thing I need
is in there somewhere" is not a check — it is the absence of one — and neither is "everything in here looks
fine", when nothing ever asked what was supposed to BE here. Every defect above is that same absence
wearing a new hat.

So this file is not a description of the rules. It is the rules, executed, with fixtures that FAIL
when the rules are wrong. `self-test` is the handoff: give it to a reviewer or a fix subagent and
it answers "does the contract still hold?" without anyone re-reading a paragraph.

EVERY fixture must fail for ITS OWN reason. A fixture that would still go red after its rule was
deleted — because some OTHER rule happened to catch the same file — pins nothing, and manufactures the
false confidence this whole file is against. `malformed-checkrun.jsonl` was exactly that: a parser that
SKIPPED the malformed row left an unmatched witness behind, so CONTAINMENT failed and the fixture kept
passing while the rule it existed to pin was broken. Each fixture is now built so that DELETING its
rule makes it come back GREEN — the loudest possible failure.

And that claim is CHECKED, not asserted. `self-test` passing means every fixture returns its verdict; it
CANNOT see the failure mode that matters most — a RULE THAT NOTHING TESTS, whose deletion leaves the suite
green while the tool has quietly stopped checking. Two rules here were exactly that (the generic
unexpected-field rejection; the RUNNING/PENDING mapping) while a hand-written matrix swore all 17 were
covered. So every rule now MARKS itself — `# MUTATE:<id>:<weakening>` above the statement that enforces
it — and `mutate-ci-snapshot.py` removes each one in turn, re-runs the fixtures, names the fixture that
notices, and FAILS if none does. Both run in CI. "Which rules are unpinned?" is a question the SUITE
answers, not one a reviewer has to discover. ADD A RULE, MARK IT, AND GIVE IT A FIXTURE.

  verify    parse a snapshot, verify it against an expected head_sha AND the base branch's required set
            (--required-set, MANDATORY — the file alone cannot tell you what was missing from it), and
            print the verdict
  self-test run every fixture and assert its expected verdict (and its REASON — the reason is the only
            thing that says WHICH rule fired, and a fixture that fails for someone else's reason pins
            nothing)

CLASSIFICATION IS TOTAL over the real enums (see the constants below): every CheckStatusState,
CheckConclusionState and StatusState value lands in exactly one bucket. UNCLASSIFIED is the ESCALATION for a
value GitHub has ADDED SINCE — the one thing left that no rule can map. It is not a gap; it is the branch
that keeps a hole from becoming a wedge (a value that resolves to nothing, so the PR never moves) or a false
green (a value silently bucketed as "probably fine").
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import NamedTuple, NoReturn

# --- the contract (ci-derivation-spec.md) ---------------------------------------------------------------
#
# CLASSIFICATION IS TOTAL OVER THE REAL ENUMS. The sets below are ENUMERATED FROM GitHub's GraphQL schema,
# never from memory, and every value of every enum lands in exactly one of them. A rule that names only the
# values you happened to think of leaves HOLES, and a value that falls in a hole matches NO branch: not
# green, not red, not pending — it can never resolve, and the PR WEDGES FOREVER.
#
#   CheckStatusState      REQUESTED QUEUED IN_PROGRESS COMPLETED WAITING PENDING
#   CheckConclusionState  SUCCESS FAILURE TIMED_OUT CANCELLED ACTION_REQUIRED NEUTRAL SKIPPED
#                         STARTUP_FAILURE STALE
#   StatusState           SUCCESS PENDING EXPECTED FAILURE ERROR
#
# The catch-all (UNCLASSIFIED) is therefore reachable ONLY by a value GitHub has ADDED SINCE — which is
# exactly what it is for.

# A check run that is COMPLETED is done; it cannot move on its own.
TERMINAL_STATUS = "COMPLETED"

# The NON-terminal statuses, NAMED. **NEVER write this rule as `.status != COMPLETED`.** A NEGATED TEST IS A
# CATCH-ALL WEARING A DISGUISE: `!= COMPLETED` matches every value we have never heard of and maps it onto a
# verdict CHOSEN IN ADVANCE, so a CheckStatusState GitHub adds tomorrow would classify RUNNING, the driver
# would wait for it to finish, and it would NEVER REACH the UNCLASSIFIED escalation — the fail-closed rule,
# dead for `.status`, silently. Only an EXPLICIT MEMBERSHIP TEST leaves a hole for the catch-all to catch.
RUNNING_STATUSES = {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}

# SKIPPED and NEUTRAL are PASSES. GitHub itself rolls SKIPPED up that way (a rollup of 6 x SKIPPED + 1 x
# SUCCESS reports state SUCCESS), and skipped runs are ROUTINE, not exotic — path filters, conditional jobs
# and excluded matrix legs produce them in bulk. Calling SKIPPED anything else is not conservatism, it is a
# WEDGE: it is COMPLETED (so not pending), not SUCCESS (so not green), not a failure (so not red) — it would
# match no rule at all, and such a repo could never go green.
#
# NEUTRAL -> PASS is the one mapping here that is DOCS-BASED, NOT EXECUTED: it shares GitHub's non-failure
# bucket with SKIPPED, but no live NEUTRAL run was found to confirm it. Say so; do not launder it into a
# verified claim.
PASS_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}

# STARTUP_FAILURE and STALE are FAILURES. Omit them from the red list and the tool calls them
# not-a-failure and merges over them — a FALSE GREEN.
FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE",
}

# Commit-status states (`StatusContext` has .state and no .conclusion).
STATUS_PASS = {"SUCCESS"}
STATUS_FAIL = {"FAILURE", "ERROR"}  # ERROR IS a failure — never shrug it off as a glitch.
STATUS_RUNNING = {"PENDING", "EXPECTED"}  # EXPECTED = declared but not yet posted.

# Verdicts.
GREEN = "green"
RED = "red"
PENDING = "pending"
UNUSABLE = "unusable"  # the evidence is defective; refetch
UNVERIFIABLE = "unverifiable"  # containment cannot be decided; fail CLOSED
UNCLASSIFIED = "unclassified"  # a value NO rule maps — escalate to a human; NEVER guess a bucket for it


# --- WHAT WERE WE EXPECTING TO SEE? — the required-check set -------------------------------------
#
# EVERY RULE ABOVE QUANTIFIES OVER THE ROWS THAT ARE IN THE FILE. Not one of them can see a row that is
# NOT — and a REQUIRED check that has not registered yet is exactly that: not a failing row, NO ROW. So a
# snapshot can be nonempty, every row PASSING, containment holding, every marker present — and still not be
# the truth about the commit. "Every check that had registered by the time we looked had passed" is a
# statement about what SHOWED UP; `green` is supposed to be a statement about what was REQUIRED.
#
# That was the REGISTRATION GAP, and it is what this section closes: the expected set is READ from the base
# branch (branch protection AND rulesets — stage-2-ci.md owns the two reads), stored PER ROW as `required_set`
# (the base is per-row state, so its required set is too; the ledger header value is only the legacy
# fallback), and passed in here. `green` now requires every declared check to be PRESENT and PASSING.
#
# THREE STATES, NEVER TWO. "I could not see any" is NOT "there are none":
DECLARED = "declared"  # both reads succeeded, the union is non-empty — these checks must be present+passing
NONE_DECLARED = "none"  # both reads succeeded, the union is EMPTY — a read FACT: nothing is required
CANNOT_READ = "unknown"  # a read FAILED. We do not know what was required — see below

# `unknown` CANNOT GO GREEN, and that is the whole reason it is not folded into `none`. A green printed
# beside "...but a required check may exist, be missing, and campaign cannot tell" is NOT a disclosure, it
# is a TRAPDOOR WITH A SIGN ON IT: the merge still happens and nobody reads the sign. So `unknown` is a
# PENDING outcome that escalates (stage-2-ci.md, SETTLED) — and it is `ledger.py`'s DEFAULT, which makes a
# run that never performed the read merge NOTHING instead of merging everything with a footnote.

# The spec, as it is written in the ledger row's `required_set` (header is the legacy fallback): `declared:<json>` | `none` | `unknown`.
#
# The `declared:` payload is a JSON ARRAY, not a comma-separated list, and that is not fussiness: A REQUIRED
# CHECK'S NAME MAY CONTAIN A COMMA. A matrix job is named `job (a, b)` — 40 of the 100 check runs on
# `vercel/next.js`'s default-branch head carried one when this was written (a dated illustration; that
# commas are legal and common in check names is the permanent claim). `"build,test".split(",")` on that set
# invents checks that do not exist and loses the ones that do, and a required set you cannot parse back is a
# required set you DO NOT HAVE.
DECLARED_PREFIX = "declared:"

# The declaration binds no app — ANY producer satisfies it. Same "-" convention as NO_OID: an explicit
# "there is nothing here", never a value we failed to write down.
ANY_APP = "-"


class RequiredSet(NamedTuple):
    """What the base branch requires. `checks` is empty unless `state` is DECLARED."""

    state: str
    checks: tuple[tuple[str, str], ...] = ()  # (context, app_id | ANY_APP)


class SpecError(Exception):
    """The --required-set spec is not readable. An OPERATOR error, never a snapshot verdict."""


def parse_required_set(spec: str) -> RequiredSet:
    """Parse the ledger's `required_set` value. STRICT, and LOUD when it cannot.

    THE ONE THING THIS MUST NEVER DO IS DEGRADE. A spec we cannot read quietly becoming `none` would say
    "the base branch requires nothing" on the strength of a value we failed to parse — rebuilding the exact
    false green this whole section removes, one layer further down. So: raise, and the caller exits 2 with
    NO verdict. No verdict at all beats a verdict about the wrong question.
    """
    if spec == NONE_DECLARED:
        return RequiredSet(NONE_DECLARED)
    if spec == CANNOT_READ:
        return RequiredSet(CANNOT_READ)
    if not spec.startswith(DECLARED_PREFIX):
        raise SpecError(
            f"{spec!r} is not a required-set spec: expected {NONE_DECLARED!r}, {CANNOT_READ!r}, "
            f"or {DECLARED_PREFIX}<json>"
        )
    try:
        payload = json.loads(spec[len(DECLARED_PREFIX):])
    except json.JSONDecodeError as exc:
        raise SpecError(f"the {DECLARED_PREFIX} payload is not JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise SpecError(
            f"the {DECLARED_PREFIX} payload must be a NON-EMPTY JSON array — an empty required set is "
            f"{NONE_DECLARED!r}, which is a different fact and must be recorded as one"
        )
    checks: list[tuple[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"context", "app"}:
            raise SpecError(f"each declared check is {{'context': …, 'app': …}}, not {entry!r}")
        ctx, app = entry["context"], entry["app"]
        if not isinstance(ctx, str) or not isinstance(app, str) or not ctx:
            raise SpecError(f"`context` and `app` must be strings, and `context` non-empty: {entry!r}")
        # AN APP BINDING IS A GITHUB APP ID — DECIMAL DIGITS — OR `ANY_APP`. THERE IS NO THIRD SHAPE, and
        # the one that keeps trying to be one is the STRING "null": jq's `tostring` applied to a null
        # binding BEFORE the `// "-"` default, which is exactly what the producer read shipped (ci-derivation-spec.md
        # FETCH owns the rule; `cli/cli` returns `app_id: null` for every required check on `trunk`, so it is
        # the COMMON case). Bound to an app that DOES NOT EXIST, the check can never be matched by any row:
        # the PR reports `pending (required check absent)` FOREVER, for a reason nobody can see. That is a
        # WEDGE, and a wedge is not the safe side of a false green — it is the other failure.
        #
        # So it is REJECTED, never NORMALISED. Reading "null" as ANY_APP would be a GUESS about a value we
        # could not read — the same degradation this parser refuses everywhere else, and this time it would
        # guess in the direction that makes an unmatched required check pass.
        if app != ANY_APP and not (app.isascii() and app.isdigit()):
            raise SpecError(
                f"`app` is an app id in decimal digits, or {ANY_APP!r} when the declaration binds no app "
                f"(the string 'null' is a `//` default that fired too late, never a producer): {entry!r}"
            )
        checks.append((ctx, app))
    return RequiredSet(DECLARED, tuple(checks))


def satisfied_by(rows: list[dict], context: str, app: str) -> bool:
    """Is this declared required check PRESENT and PASSING in the evidence?

    MATCHED ON PRODUCER, NOT JUST ON NAME — A RIGHT-NAMED CHECK FROM THE WRONG APP IS NOT THE REQUIRED ONE.
    A declaration that binds an app (`app != ANY_APP`) is a statement about WHO must produce the check, and
    a rule that compares only the name would let any app in the world satisfy it by naming a job the same.

    A `status` row can satisfy only an UNBOUND declaration. The commit-status family carries NO producer
    field at all (the response has none to give — see the FETCH block in ci-derivation-spec.md), so an app-bound
    declaration CANNOT BE PROVEN by one. It stays unsatisfied, the PR goes `pending (required check
    missing)`, and SETTLED escalates it with the check named. That is FAIL-CLOSED ON PURPOSE: the
    alternative is to accept a status from an app we never identified as proof of a check that named one —
    the false green with an extra step.

    PASSING is tested here, not assumed from the caller's position. It is true that `decide` only reaches
    this after every row has already classified PASS — but a rule that is correct only because of where it
    sits is a rule that breaks silently the day the bullets are reordered.
    """
    for r in rows:
        if r["row"] == "checkrun" and r.get("name") == context:
            if app in (ANY_APP, r.get("app_id")) and r.get("status") == TERMINAL_STATUS \
                    and r.get("conclusion") in PASS_CONCLUSIONS:
                return True
        if r["row"] == "status" and r.get("context") == context and app == ANY_APP:
            if r.get("state") in STATUS_PASS:
                return True
    return False


class SnapshotError(Exception):
    """The artifact is not evidence."""


# The FIVE row types ci-derivation-spec.md defines, and the EXACT field set each one carries. There is no sixth
# type, and a row of a type we do not recognise is NOT nothing — it is something we FAILED TO UNDERSTAND,
# and failing to understand a row is never grounds to ignore it. An ignored row is invisible to
# `verify_sha` and to `decide`, so a FAILING one parses as "nothing wrong" — which is the exact false-green
# this whole file exists to kill, reproduced inside the tool meant to prevent it. So: reject, never skip.
#
# The set is EXACT, not a minimum. "Every required field is present" admits a row that ALSO carries a field
# we do not understand — and a field we do not understand is read by nothing, which makes it one more piece
# of evidence that is PRESENT AND NOT COUNTED. The witness `sha` is the case that proves it: the rollup
# carries no commit oid, so a sha there is one WE invented (fabricated evidence, forbidden outright), and
# the old "required fields present" test waved it straight through and then ignored it.
ROW_FIELDS = {
    "header": ("sha",),
    "source": ("source", "sha", "count"),  # ONE per mandatory source, written only by a fetch that RAN
    "checkrun": ("sha", "name", "app_id", "status", "conclusion", "id"),
    "status": ("sha", "context", "state"),
    "witness": ("name", "id"),  # SHA-LESS by design — see verify_sha
}

# The exact key set a row of each type may carry: `row` plus that type's fields. Nothing else.
ROW_KEYS = {kind: {"row", *fields} for kind, fields in ROW_FIELDS.items()}

# THE MANDATORY SOURCES, and the row type each one PRODUCES. This file's founding rule is that a source
# you never queried reports nothing, and "nothing" parses as "nothing wrong" — and until the `source` row
# existed the artifact COULD NOT EXPRESS THAT RULE. "The commit-status fetch RAN and this commit carries
# zero statuses" and "the commit-status fetch was SKIPPED, or died before appending anything" produced the
# BYTE-IDENTICAL file: no `status` rows. So a check-runs-only artifact of all-passing rows verified GREEN
# while a MANDATORY evidence source had never been queried — and a failing Jenkins commit status, which
# `/check-runs` CANNOT SEE by design, would have been sitting on that commit, unread. The false green this
# whole file exists to kill, one level ABOVE every rule written to catch it.
SOURCE_ROWS = {"check-runs": "checkrun", "status": "status", "rollup": "witness"}

# Does that source's RESPONSE carry a commit oid we can stamp on its marker — and WHEN?
#
#   "always"  the status response carries `.sha` at the TOP LEVEL, so it is there even when the commit
#             carries ZERO statuses. A `-` on that marker therefore did NOT come from the response.
#   "rows"    the check-runs response carries `.head_sha` on EACH ROW and nowhere else, so it has a commit
#             oid only when it RETURNED a row. Zero check runs => genuinely no oid => `-`, and inventing
#             one would be the fabrication this contract forbids.
#   "never"   the rollup carries NO commit oid at all (same reason `witness` rows are SHA-LESS). A sha on
#             that marker is a value WE made up.
SOURCE_OID = {"check-runs": "rows", "status": "always", "rollup": "never"}

# "this source's response carried no commit oid" — never "some sha we did not bother to write down".
NO_OID = "-"

# A count is a decimal integer, no sign, no leading zeros, no whitespace. A count we cannot COMPARE to the
# rows present cannot show the artifact is whole, and a comparison we cannot make is not one whose result
# we may assume. (`int()` would also accept `" 2 "`, `"+2"` and `"２"` — and then CRASH on `"two"`.)
COUNT_RE = re.compile(r"^(0|[1-9][0-9]*)$")

# The artifact's EXACT name, from ci-derivation-spec.md's PROMOTE step: `ci-<pr>-<head_sha>.txt`. Matching the
# SHAPE — one PR number, ONE sha, that extension — is the whole point. Asking only whether the expected sha
# appears SOMEWHERE in the name is not a check: `ci-35-<head_sha>-<old_sha>.txt` names TWO commits and
# would sail through it, the second sha present and uncounted.
#
# LOWERCASE, because a git object id IS lowercase — `[0-9a-fA-F]` admitted `ci-35-1499C72…C971.txt`, a name
# NO producer of ours can emit, so a file wearing it was written by something we do not know. And the
# captured sha is compared to the expected one EXACTLY (never case-folded): case-folding is the same
# "close enough" reasoning as the substring search, one level down.
FILENAME_RE = re.compile(r"^ci-(?P<pr>\d+)-(?P<sha>[0-9a-f]{40})\.txt$")

# A git object id, as GitHub returns it. The verdicts below only mean anything against a sha of this shape;
# an operator who passes anything else gets a LOUD error (exit 2), never a verdict computed from a
# comparison that could not have succeeded.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse(path: Path) -> list[dict]:
    """JSONL: EVERY line is one JSON object, header included. No comment line, nothing to special-case.

    A line that does not parse is a corrupt snapshot — not something to skip past. Neither is a BLANK
    line: "every line is one JSON object, with NO exceptions" means a blank one is a defect in the
    producer, and a producer we cannot trust to write the file we specified is not a producer whose
    output we can green off.

    A row is admitted ONLY if its type is one of the five and it carries EXACTLY that type's fields —
    every one it requires, and NOT ONE MORE. A malformed row is not evidence: a `checkrun` with no
    `status` cannot be judged, and a rule that cannot judge a row must never conclude it is fine. A row
    with an EXTRA field is not evidence either, for the mirror-image reason: nothing reads that field, so
    whatever it asserts is neither verified nor refuted — it is present and not counted.

    Structure is part of the shape: the header is "Exactly one, first line" (ci-derivation-spec.md). A file whose
    header is not the first row, or which carries a SECOND header, is rejected here — a later header is a
    row nothing reads, and if it named a different commit, the file would describe two.

    TYPE is part of the shape too, and it is the same rule wearing its third hat. Every field the table
    defines is a STRING; a value of any other type is a value we cannot read — and a value we cannot read
    is not one we may hand to a comparison and hope. Handing it on does not produce a lenient verdict, it
    produces NO verdict: `{"row":{"kind":"status"}}` used to reach `kind not in ROW_FIELDS` and raise
    `TypeError: unhashable type: 'dict'`, and an unhandled exception is the tool FAILING TO HAVE AN
    OPINION — the one outcome the contract has no word for. Same for a `conclusion` or an `id` holding an
    object: `in FAIL_CONCLUSIONS` and `Counter(...)` hash their input. So the type check lives HERE, once,
    at the boundary, as a CLASS — never as a patch at each site that would otherwise blow up.

    And every rule above checks the shape of the dict the DECODER handed it — so the decoder is part of the
    boundary too, and it was the weakest part of it. `json.loads` resolves a REPEATED member name by keeping
    the LAST and DISCARDING the earlier one, without a word; the discarded value is then present in the
    artifact's bytes and visible to NO rule here. That is this file's one bug, again, one level down. It is
    rejected below, at the decoder, where the duplicate is still visible — nowhere later can see it.
    """
    def strict_object(n: int):
        """`object_pairs_hook` that rejects a REPEATED member name — in ANY object on the line, nested ones
        included, because a nested object is bytes in the artifact just the same.

        A duplicate key makes the artifact UNUSABLE. Never "last one wins", never "first one wins": two
        values for one field means the file does not say ONE thing, and a file that does not say one thing
        is not evidence. Picking one is how the STALE sha in `{"sha":"<old>","sha":"<head>"}` and the
        REJECTED type in `{"row":"status_context","row":"checkrun"}` both went GREEN.
        """
        def hook(pairs: list[tuple[str, object]]) -> dict:
            dupes = sorted({k for k, c in Counter(k for k, _ in pairs).items() if c > 1})
            if dupes:
                # MUTATE:duplicate-key:pass
                raise SnapshotError(
                    f"line {n}: duplicate member name(s) {', '.join(dupes)} — the decoder keeps only ONE "
                    f"value for a repeated key and silently discards the other, so the discarded one is "
                    f"present in the bytes and reaches NO rule. A field given two values says nothing."
                )
            return dict(pairs)

        return hook

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # MUTATE:unreadable:text = path.read_bytes().decode("utf-8", errors="replace")
        raise SnapshotError(
            f"the snapshot cannot be read as UTF-8 text ({exc}) — bytes we cannot decode are not evidence, "
            f"and decoding them LENIENTLY would silently rewrite what the file says."
        ) from exc

    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            # MUTATE:blank-line:continue
            raise SnapshotError(f"line {n} is blank — the artifact is JSONL, every line is one object")
        try:
            row = json.loads(line, object_pairs_hook=strict_object(n))
        except json.JSONDecodeError as exc:
            # MUTATE:not-json:continue
            raise SnapshotError(f"line {n} is not JSON: {exc}") from exc
        except RecursionError as exc:
            # MUTATE:too-deep:continue
            raise SnapshotError(
                f"line {n} is nested too deeply to decode ({exc}) — the decoder ran out of stack and RAISED "
                f"where a verdict was owed, and a crash is not a verdict. A row of the shape this contract "
                f"defines is a flat object of strings; nothing legitimate goes anywhere near this depth."
            ) from exc
        if not isinstance(row, dict) or "row" not in row:
            # MUTATE:not-a-row:continue
            raise SnapshotError(f"line {n} is not a row object — every line is an object with a `row` key")
        kind = row["row"]
        if not isinstance(kind, str):
            # MUTATE:row-kind-type:continue
            raise SnapshotError(
                f"line {n}: the `row` field is a {type(kind).__name__}, not a string — a row type we cannot "
                f"even read is not a row we may ignore, and asking whether it is one of the five (a HASH "
                f"lookup) on a value like this used to CRASH instead of returning a verdict."
            )
        if kind not in ROW_FIELDS:
            # MUTATE:unknown-row:continue
            raise SnapshotError(
                f"line {n} is an UNRECOGNISED row type {kind!r} — the contract defines only "
                f"{', '.join(sorted(ROW_FIELDS))}. A row we cannot read is NOT a row we may ignore."
            )
        missing = [f for f in ROW_FIELDS[kind] if row.get(f) is None]
        if missing:
            # MUTATE:missing-field:continue
            raise SnapshotError(f"line {n}: {kind} row is missing required field(s) {', '.join(missing)}")
        untyped = [f for f in ROW_FIELDS[kind] if not isinstance(row[f], str)]
        if untyped:
            # MUTATE:field-type:continue
            raise SnapshotError(
                f"line {n}: {kind} row carries non-string value(s) for {', '.join(untyped)} — every field in "
                f"the contract's table is a STRING. A nested object or a number there is a value nothing can "
                f"compare, and it used to reach a set/Counter lookup and CRASH the tool."
            )
        extra = sorted(set(row) - ROW_KEYS[kind])
        if kind == "witness" and "sha" in extra:
            # MUTATE:witness-sha:pass
            raise SnapshotError(
                f"line {n}: a witness row carries a sha. The rollup returns NO commit oid, so that value "
                f"is one WE INVENTED — fabricated evidence, which is worse than none. witness rows are "
                f"SHA-LESS by contract; a sha on one is never 'harmless extra detail'."
            )
        if extra:
            # MUTATE:unexpected-field:pass
            raise SnapshotError(
                f"line {n}: {kind} row carries unexpected field(s) {', '.join(extra)} — the contract gives "
                f"each row type an EXACT field set. Nothing reads an unexpected field, so it is evidence "
                f"PRESENT AND NOT COUNTED, exactly like an unrecognised row type."
            )
        rows.append(row)

    # "Exactly one, first line" is TWO independent rules, and they are checked as two ON PURPOSE. Folded
    # into one ("no header after row 0") they would OVERLAP: a header-second file would trip the duplicate
    # rule, and the header-FIRST rule could then be deleted with no fixture noticing. Each rule must be
    # the ONLY thing standing between its fixture and a green.
    #
    # An EMPTY file lands here too, as "found 0" — and it lands here INSTEAD of under a rule of its own on
    # purpose. A separate `if not rows` raise would be a rule no fixture could ever pin: delete it and an
    # empty file is STILL unusable, caught by this one. `mutate-ci-snapshot.py` reports exactly that shape
    # of rule as unpinned, and the honest fix for an unpinnable rule is to not have it.
    headers = [n for n, r in enumerate(rows, 1) if r["row"] == "header"]
    where = f"line(s) {headers}" if rows else "the file has no rows at all"
    if len(headers) != 1:
        # MUTATE:one-header:pass
        raise SnapshotError(
            f"expected EXACTLY ONE header row, found {len(headers)} ({where}) — only the first "
            f"is ever read, so any other is present and NOT COUNTED: if it named a different commit, this "
            f"file would describe two."
        )
    if rows[0]["row"] != "header":
        # MUTATE:header-first:pass
        raise SnapshotError(
            f"the first row is a {rows[0]['row']!r}, not the header — the contract says the header is "
            f"'Exactly one, FIRST line'. A file that states which commit it is about only AFTER it has "
            f"already listed evidence has not stated it at all."
        )
    return rows


def verify_filename(path: Path, expected_sha: str) -> None:
    """The FILENAME must be EXACTLY `ci-<pr>-<head_sha>.txt`, for the expected head_sha.

    ci-derivation-spec.md's VERIFY rule names THREE things that must equal the ledger's head_sha: the header, every
    evidence row's sha, AND the filename. The filename is OURS, like the header, so it cannot catch a
    wrong-commit FETCH — what it catches is a MISFILED artifact: a stale `ci-<pr>-<old_sha>.txt` still
    sitting in the rundir, read as if it described the current head. Verifying the bytes of a file while
    never checking WHICH file you read is a hole big enough to green a superseded commit through.

    The SHAPE is what is checked, not "the sha turns up somewhere in the name". A name is a claim about
    which commit these bytes describe; a name carrying TWO shas makes two claims and is therefore no claim
    at all, and the version of this check that merely searched the hyphen-delimited parts for the expected
    sha said GREEN to it.
    """
    m = FILENAME_RE.match(path.name)
    if not m:
        # MUTATE:filename-shape:return
        raise SnapshotError(
            f"filename {path.name!r} is not the artifact's shape — it must be EXACTLY "
            f"ci-<pr>-<head_sha>.txt (one PR number, ONE LOWERCASE 40-hex sha). A name that carries no sha, "
            f"more than one, or a sha in a case no producer of ours emits, cannot say which commit these "
            f"bytes describe."
        )
    if m.group("sha") != expected_sha:
        # MUTATE:filename-sha:pass
        raise SnapshotError(
            f"filename {path.name!r} names head_sha {m.group('sha')!r}, not the expected "
            f"{expected_sha!r} — this artifact describes another commit (or is misfiled)"
        )


def verify_sha(rows: list[dict], expected_sha: str) -> None:
    """Every EVIDENCE row's sha must equal the expected head_sha.

    The power of this check comes entirely from the rows carrying what GITHUB said (`.head_sha` on a
    check run, top-level `.sha` on the status response). If they carried a literal we interpolated,
    this would compare a copy against its own source and could never fail — which is exactly the bug
    that shipped. `witness` rows are EXEMPT: the rollup carries no commit oid, so a sha on a witness
    row would be one we INVENTED. Fabricated evidence is worse than none.

    `parse()` has already established the STRUCTURE this relies on: `rows[0]` is the header, it is the
    ONLY header, and no witness row carries a sha at all. This function checks the VALUES.
    """
    header = rows[0]
    if header.get("sha") != expected_sha:
        # MUTATE:header-sha:pass
        raise SnapshotError(
            f"header sha {header.get('sha')!r} does not match the expected head_sha {expected_sha!r} — "
            f"this file was fetched for another commit"
        )

    for r in rows:
        if r["row"] not in ("checkrun", "status"):
            continue  # witness rows carry no sha; a `source` marker's sha is verified by verify_sources
        if r.get("sha") != expected_sha:
            # MUTATE:evidence-sha:pass
            raise SnapshotError(
                f"{r['row']} row {r.get('name') or r.get('context')!r} describes "
                f"{r.get('sha')!r}, not the expected {expected_sha!r} — superseded commit"
            )


def verify_sources(rows: list[dict], expected_sha: str) -> None:
    """EXACTLY ONE completion marker per MANDATORY source — proof each one was ACTUALLY QUERIED.

    A missing marker means a mandatory source's failures CANNOT be shown to be in this artifact, so the
    artifact is UNUSABLE. Never green. That is the whole point: an absence must stop reading as "nothing
    wrong" and start reading as "we do not know".

    A MARKER CANNOT BE A RUBBER STAMP, and that is not a promise — it is three properties, each of which
    only a fetch that RAN could satisfy:

      * **It cannot exist without its fetch.** The marker is emitted by the SAME `jq` filter, in the SAME
        command, as the rows it describes (ci-derivation-spec.md, PROMOTE), and only after `--paginate --slurp` has
        collected every page. A fetch that fails writes NEITHER its rows nor its marker, and the snapshot is
        never promoted. There is no way to write a marker for a fetch that did not happen.
      * **`count` must EQUAL the rows of that source actually present.** A marker claiming 5 where 3 are in
        the file means the artifact is TRUNCATED — rows the fetch emitted did not survive promotion, and a
        missing row could be the FAILING one. A marker that does not match the file it sits in describes
        some other file.
      * **`sha` must be GITHUB'S**, exactly like the evidence rows, and it is compared against the ledger's
        `head_sha`, which is OURS. Two independent sources, so they CAN disagree — which is the only reason
        the comparison can tell you anything. (The bug this file memorialises is a check built out of its
        own input; a marker stamped from our own literal would rebuild it.)

    And the sha may be ABSENT only where GitHub genuinely gave none — `SOURCE_OID` above says exactly where.
    That asymmetry is what makes a ZERO-row source PROVABLE rather than merely empty:

        {"row":"source","source":"status","sha":"<GITHUB'S OWN>","count":"0"}

    says, carrying GitHub's own commit oid: *we asked this commit for its statuses, and it has none.* That
    is a FACT, and it is exactly what an all-passing check-runs-only artifact could not state. An absent
    `status` section states nothing at all.
    """
    markers = [r for r in rows if r["row"] == "source"]
    seen = Counter(m["source"] for m in markers)
    if seen != Counter(list(SOURCE_ROWS)):  # `Counter(dict)` would read the VALUES as counts
        found = " ".join(f"{s}={seen.get(s, 0)}" for s in sorted(set(SOURCE_ROWS) | set(seen)))
        # MUTATE:source-set:pass
        raise SnapshotError(
            f"the artifact must carry EXACTLY ONE source marker for each MANDATORY source "
            f"({', '.join(SOURCE_ROWS)}), and no others — found: {found}. A source you never queried "
            f"reports NOTHING, and 'nothing' parses as 'nothing wrong': with no marker for it, a mandatory "
            f"source's failures cannot be shown to be in this artifact, so this artifact cannot be green."
        )

    by_source = {m["source"]: m for m in markers}
    for src, row_kind in SOURCE_ROWS.items():
        marker = by_source.get(src)
        if marker is None:
            # Unreachable while `source-set` stands. It is here so that MUTATING `source-set` away leaves
            # its fixtures with a clean FALSE GREEN — the loudest kill — instead of a KeyError crash, which
            # would pin the rule for the wrong reason.
            continue
        actual = sum(1 for r in rows if r["row"] == row_kind)

        if not COUNT_RE.match(marker["count"]):
            # MUTATE:source-count-shape:continue
            raise SnapshotError(
                f"the {src} marker's count {marker['count']!r} is not a count (a decimal integer) — a "
                f"marker whose count cannot be COMPARED to the rows present cannot show this artifact is "
                f"whole, and a comparison we cannot make is not one we may assume the result of."
            )
        if int(marker["count"]) != actual:
            # MUTATE:source-count:pass
            raise SnapshotError(
                f"the {src} marker says count={marker['count']} but the artifact carries {actual} "
                f"{row_kind} row(s) — TRUNCATED. Rows this fetch emitted are NOT IN THE FILE, and a row "
                f"that is not in the file could be the FAILING one. A marker that does not match the rows "
                f"present is a rubber stamp: it proves the fetch ran and nothing about what survived."
            )

        oid = SOURCE_OID[src]
        has_sha = marker["sha"] != NO_OID
        want_sha = oid == "always" or (oid == "rows" and actual > 0)
        if has_sha != want_sha:
            # MUTATE:source-oid:pass
            raise SnapshotError(
                (
                    f"the {src} marker carries a sha ({marker['sha']!r}), but that source's response holds "
                    f"NO commit oid AT ALL — so that value is one WE INVENTED. Fabricated evidence, the "
                    f"same defect as a sha on a witness row, and worse than none."
                )
                if has_sha
                else (
                    f"the {src} marker carries NO sha ('-'), but its response DOES carry one "
                    + (
                        "(the top-level `.sha`, present even when the commit has ZERO statuses)"
                        if oid == "always"
                        else f"(`.head_sha`, on each of the {actual} row(s) it returned)"
                    )
                    + " — a '-' there did not come from GitHub. A marker whose sha is not GITHUB'S cannot "
                    "disagree with the ledger, so it could never fail: a rubber stamp."
                )
            )
        if has_sha and marker["sha"] != expected_sha:
            # MUTATE:source-sha:pass
            raise SnapshotError(
                f"the {src} marker describes {marker['sha']!r}, not the expected head_sha "
                f"{expected_sha!r} — GitHub answered about ANOTHER commit, so every row this source "
                f"contributed is about that commit, whatever the rows themselves say."
            )


def check_containment(rows: list[dict]) -> None:
    """REST must have seen everything the rollup saw.

    Containment is a MULTISET over a USABLE identity, never a set over names — names are NOT unique
    (matrix jobs and reusable workflows emit many runs sharing one), so a set comparison cannot see a
    MISSING DUPLICATE. And the identity must be usable: a null or duplicated witness id cannot
    distinguish two runs, so it cannot prove either was seen. That is UNVERIFIABLE, and we fail
    CLOSED — a containment test that silently degrades is worse than one that says it cannot tell.

    A REST-only row is FINE: it can only ADD evidence, and it cannot hide a failure because the REST
    row carries identity AND verdict together. Requiring EQUALITY would never terminate — GitHub's
    rollup omits dynamic-event check suites by design.
    """
    witnesses = [r for r in rows if r["row"] == "witness"]
    ids = [w.get("id") for w in witnesses]

    if any(i in (None, "", "-") for i in ids):
        # MUTATE:witness-identity:pass
        raise Unverifiable("a witness carries no identity — containment cannot be proven")
    dupes = [i for i, c in Counter(ids).items() if c > 1]
    if dupes:
        # MUTATE:witness-unique:pass
        raise Unverifiable(f"witness identity is not unique ({dupes[0]}) — containment cannot be proven")

    rest = Counter(r.get("id") for r in rows if r["row"] == "checkrun")
    for ident, want in Counter(ids).items():
        if rest[ident] < want:
            # MUTATE:rest-containment:pass
            raise SnapshotError(f"REST is missing a witnessed run ({ident}) — evidence incomplete")


class Unverifiable(Exception):
    """Containment cannot be decided. Fail closed; never green."""


def decide(rows: list[dict], required: RequiredSet) -> tuple[str, str]:
    """Decide from the verified file's contents AND what the base branch REQUIRED — FIRST MATCH WINS.
    Returns (verdict, reason).

    `required` is the second half of the question, and the file alone cannot answer it: every rule that
    reads `rows` quantifies over the rows that ARE there, and a required check that never registered is
    NO ROW. See "WHAT WERE WE EXPECTING TO SEE?" above.

    THE ORDER IS PART OF THE RULE, and `red` OUTRANKS `UNCLASSIFIED` DELIBERATELY. A snapshot carrying BOTH
    a failing row and an unknown value is RED: the known failure is actionable NOW and blocks the merge
    regardless, and the unknown value is DEFERRED, NOT DISCARDED — the fix lands, the head moves, and the
    next derivation re-reads a fresh snapshot where, with no failure left to outrank it, the unknown value
    escalates and the PR parks exactly as it must. Parking first would be strictly worse: a PR with a real,
    fixable failure would sit on a human for a value that could not have merged it anyway.

    NO ORDER HERE CAN PRODUCE A FALSE GREEN, and that is the property to preserve if these are ever
    re-ordered: `green` is LAST and demands that EVERY evidence row pass, so while any unknown value is in
    the snapshot the green branch cannot be reached — whichever branch is tested first. Every earlier branch
    is a non-merging outcome, so the ordering can only decide WHICH non-green verdict is recorded and how
    fast the PR moves. It can never decide green.

    UNCLASSIFIED outranks `pending`, though: a still-running row does NOT postpone the escalation.
    """
    runs = [r for r in rows if r["row"] == "checkrun"]
    statuses = [r for r in rows if r["row"] == "status"]
    evidence = runs + statuses

    # --- red: any evidence row FAILS. Other rows still running does not change this.
    for r in runs:
        c = r.get("conclusion")
        if r.get("status") == TERMINAL_STATUS and c in FAIL_CONCLUSIONS:
            # MUTATE:checkrun-red:pass
            return RED, f"{r['name']} concluded {c}"
    for s in statuses:
        if s.get("state") in STATUS_FAIL:
            # MUTATE:status-red:pass
            return RED, f"commit status {s['context']} is {s['state']}"

    # --- UNCLASSIFIED: a value NO rule maps. ESCALATE — never guess a bucket for it.
    #
    # This is what makes the classification TOTAL, and it is NOT decoration. Two things reach it:
    # a value GitHub has ADDED SINCE (either field), and a COMPLETED run carrying NO conclusion at all
    # (`.conclusion` is "-" when the field is absent, and "-" is not a CheckConclusionState). On a row that
    # is still RUNNING the "-" is harmless — `.status` already classified it. But a COMPLETED row holding it
    # has NO VERDICT AT ALL, and it falls here exactly like tomorrow's enum value. That is the catch-all
    # doing its job: NEVER "read through" the "-" to a guess.
    for r in runs:
        st, c = r.get("status"), r.get("conclusion")
        if st != TERMINAL_STATUS and st not in RUNNING_STATUSES:
            # MUTATE:checkrun-unknown-status:pass
            return UNCLASSIFIED, f"{r['name']} has status {st} — no rule maps it; NOT assumed to be running"
        if st == TERMINAL_STATUS and c not in PASS_CONCLUSIONS:
            # A FAIL conclusion already returned RED above, so this is a conclusion in NEITHER set.
            # MUTATE:checkrun-unclassified:pass
            return UNCLASSIFIED, f"{r['name']} concluded {c} — no rule maps it"
    for s in statuses:
        if s.get("state") not in STATUS_PASS | STATUS_RUNNING:
            # STATUS_FAIL already returned RED above, so this is a state in NONE of the three sets.
            # MUTATE:status-unclassified:pass
            return UNCLASSIFIED, f"commit status {s['context']} is {s['state']} — no rule maps it"

    # --- pending: any evidence row is still RUNNING.
    for r in runs:
        if r.get("status") in RUNNING_STATUSES:
            # MUTATE:checkrun-pending:pass
            return PENDING, f"{r['name']} is {r.get('status')} — still running"
    for s in statuses:
        if s.get("state") in STATUS_RUNNING:
            # MUTATE:status-pending:pass
            return PENDING, f"commit status {s['context']} is {s['state']} — still running"

    # --- pending: nothing has registered yet. Disjoint from every branch above (they all quantify over
    # rows that do not exist here), so its position among them is immaterial — but it MUST precede green.
    if not evidence:
        # MUTATE:zero-evidence:pass
        return PENDING, "zero evidence rows — nothing has registered yet (NOT green)"

    # ---------------------------------------------------------------------------------------------
    # Every evidence row classified PASS. EVERY RULE ABOVE IS NOW SATISFIED — and that is exactly the
    # state in which the registration gap used to hand back a false green. The remaining question is the
    # one no row can answer: WERE THESE THE ROWS THAT WERE SUPPOSED TO BE HERE?
    # ---------------------------------------------------------------------------------------------

    # --- pending: we do not know what was required, so we cannot say this is green. INCOMPLETE EVIDENCE
    # IS NOT GREEN, exactly as ZERO evidence is not. This is the branch that makes `unknown` — ledger.py's
    # DEFAULT — fail CLOSED: a run that never read the base branch's required checks merges nothing.
    if required.state == CANNOT_READ:
        # MUTATE:required-set-unreadable:pass
        return PENDING, (
            "the base branch's required checks could NOT BE READ (required_set=unknown) — a required check "
            "may exist, be missing, and this snapshot cannot tell. NOT green"
        )

    # --- pending: a DECLARED required check has no passing row. It has not registered — which is NO ROW,
    # not a failing one, so every rule above passed straight over it.
    for context, app in required.checks:
        if not satisfied_by(rows, context, app):
            # MUTATE:required-check-missing:pass
            return PENDING, (
                f"required check absent: {context}"
                f"{'' if app == ANY_APP else f' (app {app})'} — declared by the base branch, and NOT "
                f"present and passing in this snapshot. NOT green"
            )

    # Every evidence row passed AND the required set is accounted for. Nothing else can reach this line,
    # and `green` here means the REQUIRED SET PASSED — not merely that whatever showed up did.
    return GREEN, (
        f"{len(evidence)} evidence rows, all passing, containment holds; required set "
        f"{'satisfied: ' + ', '.join(c for c, _ in required.checks) if required.checks else 'is EMPTY (read, not assumed)'}"
    )


def evaluate(
    path: Path, expected_sha: str, *, required: RequiredSet, expect_filename_sha: bool = True
) -> tuple[str, str]:
    """`required` is MANDATORY and keyword-only. There is deliberately NO default: a caller that forgot to
    say what the base branch requires must not be silently given the permissive answer — that is the whole
    class of bug this parameter exists to kill.

    `expect_filename_sha` is OFF only for the fixtures, which are named by the PROPERTY they pin
    (`green.jsonl`, `wrong-sha.jsonl`, …) rather than by a SHA — their names are documentation. It is ON
    for every real artifact, and `self-test` proves the check still fires (FILENAME_CASES below).
    """
    try:
        if expect_filename_sha:
            verify_filename(path, expected_sha)
        rows = parse(path)
        verify_sha(rows, expected_sha)
        verify_sources(rows, expected_sha)
        check_containment(rows)
    except Unverifiable as exc:
        return UNVERIFIABLE, str(exc)
    except SnapshotError as exc:
        return UNUSABLE, str(exc)
    return decide(rows, required)


# --- the liveness fingerprint --------------------------------------------------------------------

# The canonical line per EVIDENCE row type — the row kind, then exactly the fields CLASSIFY reads
# (`stage-2-ci.md`, SETTLED — its FINGERPRINT block owns the spec, and `doc-check` asserts the block's
# line formats match these tuples). `header`/`source`/`witness`
# rows carry no verdict and are EXCLUDED; so are the row's `sha` (already proven equal to the ledger's
# head_sha, which is hashed in once, at the front) and its `id` (a re-run under a new job id is not
# CI moving).
FINGERPRINT_FIELDS = {
    "checkrun": ("name", "app_id", "status", "conclusion"),
    "status": ("context", "state"),
}


def fingerprint(rows: list[dict], head_sha: str) -> str:
    """The liveness digest of a VERIFIED snapshot's evidence rows.

    Callers hand this VERIFIED rows only — an UNUSABLE snapshot has NO fingerprint at all (`derive`
    emits `fingerprint: null` for it): a strike is a claim that TRUSTED evidence did not move, and
    nothing rejected is ever hashed.

    Canonical form, pinned exactly by `ci-status-test.py`'s `[fp]` cases: each evidence row becomes its
    tab-joined line, DUPLICATE lines are KEPT (two matrix legs at the same verdict are two lines — a
    third arriving IS motion), lines are sorted by code point (UTF-8 preserves code-point order in its
    bytes, so this IS the bytewise sort the doc specifies), every line ends with `\\n`, and the payload
    is `head_sha` + `\\n` + the lines, hashed as UTF-8 through sha256.
    """
    lines = []
    for row in rows:
        fields = FINGERPRINT_FIELDS.get(row["row"])
        if fields is None:
            continue
        lines.append("\t".join([row["row"], *(row[f] for f in fields)]))
    payload = head_sha + "\n" + "".join(line + "\n" for line in sorted(lines))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- self-test: the fixtures ARE the evidence ---------------------------------------------------

FIXTURE_SHA = "1499c72bf1715e74abb0e28658b515eaa2c0c971"
SUPERSEDED_SHA = "e846cd76a783aa1087e221cc0684b84136419404"

# Each fixture pins ONE property of the contract, and pins it INDEPENDENTLY: delete the rule it exists for
# and the fixture comes back GREEN. That is the acceptance test for a fixture in this file. A fixture that
# would still fail after its rule was deleted — because some other rule catches the same file for a
# different reason — is worse than no fixture: it keeps passing while the rule it claims to pin is broken,
# manufacturing exactly the false confidence this file was written against.
#
# Every "UNUSABLE" fixture below is therefore built as OTHERWISE-GREEN evidence plus the ONE defect, with
# the defect kept out of every other rule's way — most importantly, a defective row is never the only
# source of a witness's containment partner (which is precisely how `malformed-checkrun.jsonl` used to
# pass for the wrong reason: skipping the bad row broke CONTAINMENT, not the missing-field rule).
#
# "Delete the rule and the fixture comes back green" is a CLAIM, and a claim nobody runs is a claim nobody
# checks — a hand-written matrix asserting it was WRONG about two rules (the generic unexpected-field
# rejection and the RUNNING/PENDING mapping were pinned by NOTHING, and a reviewer, not the suite, found
# that out). So the claim is now MACHINE-CHECKED: `mutate-ci-snapshot.py` weakens every rule in this file
# in turn and FAILS if no fixture notices. "Which rules are unpinned?" is a question the SUITE answers.
#
# Each entry is (verdict, needle, why): the fixture must produce that verdict AND a reason containing that
# needle. The needle is not decoration — it is what pins a rule whose ONLY contribution is its MESSAGE.
# `witness-sha` is exactly that rule: delete it and the generic unexpected-field rule still says UNUSABLE,
# so no VERDICT can pin it, and the specific message ("INVENTED") is the whole thing it adds.
#
# TWO CONSTRUCTION NOTES, both in service of "delete the rule and this fixture comes back GREEN":
#
#  * The `source` markers in a fixture describe the rows that SURVIVE its own rule's weakening, which is
#    not always the rows in the bytes. `malformed-checkrun.jsonl` and `field-not-string.jsonl` each carry a
#    SECOND, defective `checkrun` line and a check-runs marker that says `count:"1"` — because the mutant
#    for their rule SKIPS that line, and a marker counting it would then fire `source-count` instead, and
#    the fixture would pin the wrong rule. The BASELINE never reaches the count rule at all: `parse()`
#    raises on the defective row first, which is precisely what the fixture exists to prove.
#  * `wrong-sha.jsonl` and `source-sha-mismatch.jsonl` are deliberate MIRRORS. A real wrong-commit fetch
#    puts the superseded sha in BOTH the evidence rows and the source marker, and EITHER rule alone would
#    catch it — so each fixture puts it in exactly ONE place, and each therefore pins exactly one rule.
EXPECTED = {
    "green.jsonl": (GREEN, "all passing", "names contain SPACES and survive the round-trip"),
    "wrong-sha.jsonl": (UNUSABLE, "superseded commit", "evidence describes a superseded commit — the check that could not fail before"),
    "header-sha-mismatch.jsonl": (UNUSABLE, "fetched for another commit", "the header names a commit the evidence does not — a misfiled artifact"),
    "duplicate-witness-id.jsonl": (UNVERIFIABLE, "not unique", "colliding identity cannot prove containment — fail CLOSED"),
    "witness-no-identity.jsonl": (UNVERIFIABLE, "carries no identity", "a run with no details_url on BOTH sides — '-' cannot tell two runs apart, so it proves nothing"),
    "missing-witness.jsonl": (UNUSABLE, "missing a witnessed run", "REST is missing a run the rollup saw"),
    "zero-rows.jsonl": (PENDING, "zero evidence rows", "zero rows is NOT green"),
    "red-status-only.jsonl": (RED, "commit status", "a commit-status failure invisible to /check-runs — why BOTH families are read"),
    "red-checkrun.jsonl": (RED, "concluded FAILURE", "a FAILED check run is RED — the rule that decides the whole point of the artifact"),
    "running-checkrun.jsonl": (PENDING, "still running", "a check run whose `.status` classifies RUNNING can still move — it is NOT a green (and the test is MEMBERSHIP in RUNNING_STATUSES, never `!= COMPLETED`)"),
    "pending-status.jsonl": (PENDING, "still running", "a PENDING commit status can still move — it is NOT a green"),
    "expected-status.jsonl": (PENDING, "still running", "EXPECTED = a required status DECLARED BUT NOT POSTED YET — the one shape of not-yet-registered the evidence can express on its own; REQUIRED_CASES below covers the shape it cannot"),
    "status-passing.jsonl": (GREEN, "all passing", "a SUCCESS commit status and no check runs at all — the family /check-runs cannot see, green on its own; REQUIRED_CASES uses it to pin what a producer-less row can and cannot prove"),
    "skipped-is-pass.jsonl": (GREEN, "all passing", "SKIPPED is a PASS — GitHub rolls it up that way, and a rule set without it can NEVER go green on a repo with path filters"),
    "unbound-checkrun.jsonl": (GREEN, "all passing", "a check run with NO PRODUCER — `.app` was null, so the read wrote `-`. Green on its own; REQUIRED_CASES pins what that row may and may not PROVE"),
    # THE CATCH-ALL, and it is now reachable ONLY by a value GitHub has ADDED SINCE — which is exactly what
    # it is for. Both values below are INVENTED: no CheckStatusState is `AWAITING_APPROVAL`, no
    # CheckConclusionState is `FLAKED_OUT`, and no StatusState is `BLOCKED`. That is the point — the real
    # enums are now TOTALLY classified, so nothing real can pin these rules, and a fixture that used a real
    # value would silently stop pinning anything the moment the value got classified. (`known-gap-skipped`
    # was exactly that fixture: SKIPPED pinned the check-run catch-all until SKIPPED became a PASS.)
    "unknown-status.jsonl": (UNCLASSIFIED, "no rule maps it", "a `.status` value GitHub added since — the rule that CANNOT be written as `!= COMPLETED`: a negated test would call this RUNNING and wait for it FOREVER"),
    "unknown-conclusion.jsonl": (UNCLASSIFIED, "no rule maps it", "a COMPLETED run holding a `.conclusion` in NEITHER set — not green, not red, and never guessed into a bucket"),
    "unclassified-status.jsonl": (UNCLASSIFIED, "no rule maps it", "a commit-status state outside the mapped set — the catch-all is what keeps it OUT of green"),
    "unknown-row-type.jsonl": (UNUSABLE, "UNRECOGNISED row type", "a FAILING row of an unknown type — skipping it greened a red commit"),
    "not-a-row.jsonl": (UNUSABLE, "not a row object", "a line that is not an object with a `row` key is not a row we may skip past"),
    "row-kind-not-string.jsonl": (UNUSABLE, "not a string", "a `row` holding an OBJECT — it used to CRASH the tool (TypeError), and a crash is not a verdict"),
    "field-not-string.jsonl": (UNUSABLE, "non-string value(s)", "a conclusion holding an OBJECT — it used to CRASH on `in FAIL_CONCLUSIONS`; a nested FAILURE, uncounted"),
    "not-json.jsonl": (UNUSABLE, "is not JSON", "a corrupt line is a corrupt snapshot — treat it like a failed fetch, never skip it"),
    "not-utf8.jsonl": (UNUSABLE, "UTF-8", "bytes we cannot decode are not evidence — and decoding them LENIENTLY rewrites what the file says"),
    "empty.jsonl": (UNUSABLE, "no rows at all", "an empty file is no snapshot — caught by the header rule, which is why it gets no rule of its own"),
    "blank-line.jsonl": (UNUSABLE, "is blank", "JSONL has NO blank lines — a producer we cannot trust is not evidence"),
    "malformed-checkrun.jsonl": (UNUSABLE, "missing required field", "a REST-ONLY checkrun with no status cannot be judged — SKIPPING it would go GREEN"),
    "witness-sha.jsonl": (UNUSABLE, "INVENTED", "a sha on a witness row is one WE invented — accepted-and-ignored is fabricated evidence"),
    "unexpected-field.jsonl": (UNUSABLE, "unexpected field(s)", "a checkrun carrying a field nothing reads — present and NOT COUNTED, one level down from an unknown row"),
    "header-not-first.jsonl": (UNUSABLE, "FIRST line", "the header is 'Exactly one, FIRST line' — evidence before it is unstamped"),
    "duplicate-header.jsonl": (UNUSABLE, "EXACTLY ONE header", "a SECOND header naming another commit — read by nothing, so present and NOT COUNTED"),
    "duplicate-key.jsonl": (UNUSABLE, "duplicate member name(s)", "a header carrying a STALE sha AND the expected one — the decoder DISCARDED the stale one, so no rule above ever saw it, and the file verified GREEN"),
    "deeply-nested.jsonl": (UNUSABLE, "nested too deeply", "a line the decoder cannot decode without running out of stack — it RAISED where a verdict was owed, and a crash is not a verdict"),
    # THE PER-SOURCE COMPLETION MARKERS — "was this source ever QUERIED?", a question the artifact could
    # not previously be asked. A missing marker is never green: it means a MANDATORY source's failures
    # cannot be shown to be in this file.
    "missing-checkruns-marker.jsonl": (UNUSABLE, "check-runs=0", "the check-runs fetch left no marker — 'it ran and found nothing' is indistinguishable from 'it never ran'"),
    "missing-status-marker.jsonl": (UNUSABLE, "status=0", "THE ONE THAT MATTERS: all-passing check runs, and the commit-status family — which /check-runs CANNOT SEE — never queried. This used to be GREEN"),
    "missing-rollup-marker.jsonl": (UNUSABLE, "rollup=0", "no rollup marker: zero witnesses could mean 'the rollup had none' or 'the fetch never ran', and containment then passes TRIVIALLY"),
    "duplicate-source-marker.jsonl": (UNUSABLE, "check-runs=2", "TWO markers for one source — if they disagreed, the file would claim two things and nothing would notice"),
    "unknown-source-marker.jsonl": (UNUSABLE, "rollup-v2=1", "a marker for a source the contract does not define — nothing reads it, so it is present and NOT COUNTED"),
    "source-count-mismatch.jsonl": (UNUSABLE, "TRUNCATED", "the marker says 3, the file holds 2 — a row the fetch emitted is MISSING, and it could be the failing one"),
    "source-count-not-a-number.jsonl": (UNUSABLE, "is not a count", "a count that cannot be COMPARED proves nothing — and `int()` would have CRASHED on it"),
    "source-sha-mismatch.jsonl": (UNUSABLE, "marker describes", "GitHub answered about ANOTHER commit for this source — the mirror of wrong-sha.jsonl"),
    "rollup-marker-sha.jsonl": (UNUSABLE, "INVENTED", "a sha on the ROLLUP marker: the rollup has no commit oid at all, so that value is fabricated — exactly like a sha on a witness row"),
    "status-marker-no-sha.jsonl": (UNUSABLE, "carries NO sha", "the status response ALWAYS carries `.sha`, zero statuses or not — a '-' there did NOT come from GitHub, so the marker is a rubber stamp"),
    "checkruns-marker-no-sha.jsonl": (UNUSABLE, "carries NO sha", "check runs were returned, so GitHub DID give a `.head_sha` — a '-' means the marker was not built from the response"),
    # NEGATIVE CONTROL. Same wrong-commit data as wrong-sha.jsonl, but stamped the OLD way: the sha
    # interpolated from our own literal onto every row instead of taken from GitHub. It comes back
    # GREEN — which is the POINT. It is the proof that the old verification could not fail, and the
    # reason `verify_sha` is worth anything at all. If this fixture ever stops returning green, the
    # bug it memorialises has been reintroduced somewhere else.
    #
    # Its `source` markers are stamped the SAME self-referential way, and that PRESERVES what it
    # memorialises rather than diluting it: a sha we copied from our own literal agrees with our own
    # literal WHEREVER we write it — on an evidence row, on a marker, anywhere. Adding a place to write
    # it does not make a circular check fall over. Only taking the value from GITHUB does.
    "negative-control-circular-sha.jsonl": (GREEN, "all passing", "PROVES the old self-stamped check was VACUOUS — it passes data from the WRONG COMMIT, markers and all"),
}


# EVERY fixture in EXPECTED is evaluated against a base branch that requires NOTHING (`none`) — the state
# in which the rules above are the ONLY thing deciding, which is exactly what those fixtures are for. The
# required-set rule gets its own table, REQUIRED_CASES, because it is the one rule whose input is NOT in the
# file: the same bytes are green or pending depending on what the BASE BRANCH declared.
NO_REQUIRED = RequiredSet(NONE_DECLARED)


def self_test(fixtures: Path) -> int:
    """Every fixture is evaluated against the SAME expected head_sha the ledger would hold.

    `wrong-sha.jsonl` is the one that matters most: its header carries the sha we REQUESTED (ours),
    while its evidence rows carry the sha GitHub actually returned for a superseded commit. Under the
    old circular scheme every row was stamped from our own literal, so that file would have verified
    CLEAN. Here it must come back UNUSABLE.
    """
    failures = 0
    for name, (want, needle, why) in sorted(EXPECTED.items()):
        path = fixtures / name
        if not path.exists():
            print(f"MISSING  {name}")
            failures += 1
            continue
        # The fixtures are named by PROPERTY, not by SHA, so the filename rule is exercised separately.
        got, reason = evaluate(path, FIXTURE_SHA, required=NO_REQUIRED, expect_filename_sha=False)
        if got == want and needle in reason:
            print(f"ok       {name:28} -> {got:14} ({why})")
        elif got != want:
            print(f"FAIL     {name:28} -> {got:14} expected {want}\n         reason: {reason}")
            failures += 1
        else:
            # Right verdict, WRONG rule. The reason is the only thing that says WHICH rule fired, and a
            # fixture that goes UNUSABLE for someone else's reason pins nothing — that is the exact
            # wrong-reason pass this file was written against.
            print(f"FAIL     {name:28} -> {got:14} but the reason does not mention {needle!r}"
                  f"\n         reason: {reason}")
            failures += 1

    failures += filename_test(fixtures)
    failures += required_test(fixtures)
    failures += fetch_test()
    failures += spec_test()
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the CI-snapshot contract is broken.")
        return 1
    print(f"all {len(EXPECTED)} fixtures + {len(FILENAME_CASES)} filename cases + "
          f"{len(REQUIRED_CASES)} required-set cases + {len(FETCH_CASES)} fetch-read cases + "
          f"{len(SPEC_CASES)} spec cases hold — the contract is intact.")
    print("`mutate-ci-snapshot.py` is what proves each of them pins its OWN rule. Run it too.")
    return 0


# THE REGISTRATION GAP, PINNED. Every case below reuses fixture bytes that are ALREADY GREEN on their own —
# so the ONLY thing under test is what the BASE BRANCH declared. That is the point: the gap was never
# visible in the file, which is precisely why no rule that reads only the file could ever have closed it.
#
# Each case is (fixture, spec, verdict, needle, why).
REQUIRED_CASES = [
    # The two that would have been FALSE GREENS, and the reason this rule exists.
    ("green.jsonl", CANNOT_READ, PENDING, "could NOT BE READ",
     "THE ONE THAT MATTERS MOST: flawless all-passing evidence, and we never learned what was REQUIRED — "
     "so it is NOT green. `unknown` is ledger.py's DEFAULT, which makes a run that never looked merge nothing"),
    ("green.jsonl", f'{DECLARED_PREFIX}[{{"context": "integration-tests", "app": "-"}}]',
     PENDING, "required check absent: integration-tests",
     "THE REGISTRATION GAP ITSELF: a required check that NEVER REGISTERED is not a failing row, it is NO "
     "ROW — the snapshot is nonempty, every row passes, and it is still not the truth about this commit"),
    # PRODUCER. A right-named check from the WRONG app is not the required one.
    ("green.jsonl", f'{DECLARED_PREFIX}[{{"context": "Lint scripts", "app": "99999"}}]',
     PENDING, "required check absent: Lint scripts (app 99999)",
     "the name matches and the APP DOES NOT — matching on name alone would let any app on earth satisfy a "
     "declaration by naming a job the same"),
    ("status-passing.jsonl", f'{DECLARED_PREFIX}[{{"context": "ci/jenkins", "app": "15368"}}]',
     PENDING, "required check absent: ci/jenkins (app 15368)",
     "an APP-BOUND declaration against a COMMIT STATUS, which carries no producer field at all — it cannot "
     "be PROVEN, so it is not accepted. Fail-closed, and the doc says so"),
    # The greens. A rule that cannot say YES is not a gate, it is a wall.
    ("green.jsonl", NONE_DECLARED, GREEN, "required set is EMPTY (read, not assumed)",
     "the base branch requires NOTHING — and that is a READ FACT, not an absence of one, so nothing "
     "required can be missing and the green carries NO caveat"),
    ("green.jsonl",
     f'{DECLARED_PREFIX}[{{"context": "Lint scripts", "app": "15368"}}, '
     f'{{"context": "Validate plugins", "app": "-"}}]',
     GREEN, "required set satisfied: Lint scripts, Validate plugins",
     "every declared check PRESENT and PASSING — one bound to its app, one unbound. THIS is what green is "
     "now allowed to mean"),
    ("status-passing.jsonl", f'{DECLARED_PREFIX}[{{"context": "ci/jenkins", "app": "-"}}]',
     GREEN, "required set satisfied: ci/jenkins",
     "an UNBOUND declaration IS satisfiable by a commit status — the producer rule must not wall off the "
     "legacy family it was never about"),
    # `-` ON A ROW IS "NO PRODUCER", AND ON A DECLARATION IT IS "ANY PRODUCER". Both sides can now carry it
    # — the check-run read defaults a null `.app` to `-` — and the two meanings must not blur into a
    # wildcard that matches from either end.
    ("unbound-checkrun.jsonl", f'{DECLARED_PREFIX}[{{"context": "Deploy", "app": "-"}}]',
     GREEN, "required set satisfied: Deploy",
     "an UNBOUND declaration satisfied by a check run that HAS NO PRODUCER — the pair the wedge made "
     "unmatchable, and the configuration `cli/cli` actually runs: every required check on `trunk` is unbound"),
    ("unbound-checkrun.jsonl", f'{DECLARED_PREFIX}[{{"context": "Deploy", "app": "15368"}}]',
     PENDING, "required check absent: Deploy (app 15368)",
     "THE MIRROR: a producer-less row cannot prove an APP-BOUND declaration. `-` on a ROW means 'no app', "
     "NEVER 'any app' — read as a wildcard it would let an unidentified producer satisfy a check that named one"),
    # A declared check that is PRESENT but still RUNNING is NOT this rule's business: it is a RUNNING row,
    # so plain `pending` outranks and the PR gets WATCHED, as it must. Ordering, pinned.
    ("running-checkrun.jsonl", f'{DECLARED_PREFIX}[{{"context": "Validate plugins", "app": "-"}}]',
     PENDING, "still running",
     "a declared check that IS registered and still running is caught by the RUNNING bullet, NOT by "
     "'required check missing' — it can still move, so it is watched"),
]


def required_test(fixtures: Path) -> int:
    """The same bytes, different declarations. Nothing here is a property OF THE FILE."""
    failures = 0
    for name, spec, want, needle, why in REQUIRED_CASES:
        got, reason = evaluate(
            fixtures / name, FIXTURE_SHA, required=parse_required_set(spec), expect_filename_sha=False
        )
        label = f"{name} + {spec[:34]}"
        if got == want and needle in reason:
            print(f"ok       {label:70} -> {got:8} ({why})")
        elif got != want:
            print(f"FAIL     {label:70} -> {got:8} expected {want}\n         reason: {reason}")
            failures += 1
        else:
            print(f"FAIL     {label:70} -> {got:8} but the reason does not mention {needle!r}"
                  f"\n         reason: {reason}")
            failures += 1
    return failures


# --- THE PRODUCER, EXECUTED — the THREE evidence reads in ci-derivation-spec.md, RUN over recorded API payloads -----------
#
# Every rule above takes the required set AS GIVEN. So a required set the DOC READS WRONG is a defect that
# NOTHING above could see — and one shipped: both reads defaulted a null app binding AFTER converting it,
# `((.x | tostring) // "-")` where `((.x // "-") | tostring)` was meant (the shape is written with a
# placeholder on purpose: an illustration of a defect must not be a live string anyone can grep into).
# In jq, `null | tostring` is the STRING "null", which is TRUTHY, so the default NEVER FIRED, and a
# required check that binds NO app
# came out bound to a producer named "null". No row can ever match that: the PR reports `pending (required
# check absent)` FOREVER, for a reason nobody can see. That is a WEDGE — NOT the safe side of a false green,
# but the OTHER failure, the one that files no report. And it was not exotic: `repos/cli/cli/branches/trunk`
# returns `app_id: null` for EVERY required check it has.
#
# The three snapshot filters are NOT retyped here — they are EXTRACTED FROM ci-derivation-spec.md AND EXECUTED. A
# copy in this file is exactly what could NOT have caught a bug in the DOC, which is where an operator reads
# them. The required-set reads are now production Python in `ci-status.py required-set`; its sibling fixture
# suite drives that code over the same recorded API responses instead of testing copied shell.
DOC = Path(__file__).resolve().parents[1] / "references" / "ci-derivation-spec.md"
FETCH_PAYLOADS = Path(__file__).parent / "fixtures" / "fetch"   # payloads for the evidence reads

# Each read is identified by the COMMAND LINE that introduces it, VERBATIM — the pipeline included, so a
# `--paginate` silently dropped from the doc is a drift this harness NOTICES instead of quietly executing
# the truncating form. A jq filter contains no single quote, so the filter is everything up to the next one.
READS = {
    # The THREE evidence fetches (FETCH).
    "check-runs": 'gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/check-runs" | jq -c \'',
    "status": 'gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/status" | jq -c \'',
    # `,headRefOid` RIDES ALONG ON THIS READ, and it is part of the identity for a reason: the PR's current
    # head is what the MOVED-HEAD rule fires on (`ci-status.py`), and a doc that quietly drops the field
    # prescribes a fetch that rule can never fire on. Pinned verbatim here, so dropping it goes RED.
    # `--repo` IS PART OF THE IDENTITY FOR THE SAME REASON, and it is the one this read shipped WITHOUT: a
    # bare `gh pr view <pr>` resolves the PR in the CURRENT CHECKOUT, so the command asks whichever repo the
    # reader is standing in — the only one of the three reads that could not say what it was about.
    "rollup": "gh pr view <pr> --repo <owner>/<repo> --json statusCheckRollup,headRefOid | jq -c '",
}

# `gh api --paginate --slurp` hands jq ONE ARRAY OF PAGES. `jq -s` over N recorded page files builds exactly
# that — so an N-page fetch is reproducible offline, and a filter that forgets to flatten the pages (or a
# doc that drops the `--paginate`) goes RED here rather than silently producing a short snapshot.
SLURPED = {which for which, opener in READS.items() if "--slurp" in opener}


class DocDrift(Exception):
    """The read this test executes is no longer in the doc, or no longer runs. NEVER a silent skip: a check
    that cannot run must FAIL, or it reports health it did not measure."""


def doc_read(which: str) -> str:
    """The jq program ci-derivation-spec.md documents. Extracted, never retyped."""
    text = DOC.read_text(encoding="utf-8")
    opener = READS[which]
    start = text.find(opener)
    if start < 0:
        raise DocDrift(f"{DOC.name} no longer contains the {which} read — this test pins NOTHING until the "
                       f"line `{opener[:46]}…` is found again")
    start += len(opener)
    end = text.find("'", start)
    if end < 0:
        raise DocDrift(f"the {which} read in {DOC.name} is never closed by a `'`")
    return text[start:end]


def run_read(which: str, payloads: tuple[Path, ...]) -> list:
    """Run the DOC's read over recorded API PAGE(S) and return the JSON values it emits, one per line.

    The payloads are the pages the endpoint returned. For a `--slurp`ed read they are handed to `jq -s`,
    which is precisely the array-of-pages `gh api --paginate --slurp` produces — so PAGINATION ITSELF is
    under test, not assumed.
    """
    jq = shutil.which("jq")
    if jq is None:
        raise DocDrift("`jq` is not installed, so the reads cannot be executed — and a check that SKIPS is "
                       "a check that lies. Install jq (the campaign's own fetch needs it regardless)")
    argv = [jq, "-c"] + (["-s"] if which in SLURPED else []) + [doc_read(which)]
    argv += [str(p) for p in payloads]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603 - fixed argv
    if proc.returncode != 0:
        names = ", ".join(p.name for p in payloads)
        raise DocDrift(f"the {which} read FAILED on {names}: {proc.stderr.strip()}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


# (payload pages, read, the EXACT JSONL rows the read must emit, why)
# The three FETCH reads, executed. These emit the snapshot's own rows, so the assertion is the rows
# THEMSELVES — no verdict to hide behind: a wrong `app_id` here is the WEDGE, and it was pinned by nothing.
FETCH_CASES = [
    (("check-runs-p1.json", "check-runs-p2.json"), "check-runs", [
        {"row": "checkrun", "sha": FIXTURE_SHA, "name": "Lint scripts", "app_id": "15368",
         "status": "COMPLETED", "conclusion": "SUCCESS",
         "id": "https://github.com/lestrrat-ai/claude-code-plugins/actions/runs/29263565055/job/86862842667"},
        {"row": "checkrun", "sha": FIXTURE_SHA, "name": "Validate plugins", "app_id": ANY_APP,
         "status": "COMPLETED", "conclusion": "SUCCESS",
         "id": "https://github.com/lestrrat-ai/claude-code-plugins/actions/runs/29263565055/job/86862842710"},
        {"row": "checkrun", "sha": FIXTURE_SHA, "name": "Deploy", "app_id": ANY_APP,
         "status": "IN_PROGRESS", "conclusion": ANY_APP,
         "id": "https://github.com/lestrrat-ai/claude-code-plugins/actions/runs/29263565055/job/86862842999"},
        {"row": "source", "source": "check-runs", "sha": FIXTURE_SHA, "count": "3"},
     ],
     "THE `.app.id` DEFAULT, AT LAST EXECUTED. A check run need not come from an App, and `null|tostring` is "
     'the TRUTHY string "null", so `((.app.id|tostring) // "-")` never fires its default and binds the row '
     "to a producer named `null` that no declaration can match. Both unbound rows must read `-`, the bound "
     "one must keep 15368, the running row's null conclusion must read `-` — and `count` must be 3, which "
     "it can only be if BOTH PAGES were read"),
    (("status-one.json",), "status", [
        {"row": "status", "sha": FIXTURE_SHA, "context": "ci/jenkins", "state": "SUCCESS"},
        {"row": "source", "source": "status", "sha": FIXTURE_SHA, "count": "1"},
     ],
     "the LEGACY family. The oid is carried ONCE at the top level, never on the status — so both the row's "
     "sha and the marker's sha must come from THERE, and from GitHub, never from a literal we interpolate"),
    (("status-none.json",), "status", [
        {"row": "source", "source": "status", "sha": FIXTURE_SHA, "count": "0"},
     ],
     "a commit with ZERO statuses: NO rows, and a marker that still carries GitHub's sha and `count: 0`. "
     "That marker is the entire reason `we asked, and there are none` is distinguishable from `we never "
     "asked` — and the latter, read as the former, is an absence that parses as `nothing wrong`"),
    (("rollup.json",), "rollup", [
        {"row": "witness", "name": "Lint scripts",
         "id": "https://github.com/lestrrat-ai/claude-code-plugins/actions/runs/29263565055/job/86862842667"},
        {"row": "source", "source": "rollup", "sha": ANY_APP, "count": "1"},
     ],
     "WITNESSES ONLY. The StatusContext entry must NOT become a witness (the `select` is what keeps the "
     "containment test comparing like with like), and the rollup marker's sha must be `-`: the rollup "
     "carries no oid, so any sha there would be one WE INVENTED"),
]


def fetch_test() -> int:
    """The three FETCH reads in ci-derivation-spec.md, EXTRACTED AND RUN over recorded API pages.

    Nothing else in this file could catch a bug in them: every rule above takes the SNAPSHOT as given, and
    these reads are what PRODUCE it. The `.app.id` ordering fix lived here, fixed and pinned by nothing —
    revert it in the doc and, until this test existed, the whole suite stayed green.
    """
    failures = 0
    for pages, which, want_rows, why in FETCH_CASES:
        label = f"{pages[0]}{f' +{len(pages) - 1}p' if len(pages) > 1 else ''} -> {which} read"
        try:
            got_rows = run_read(which, tuple(FETCH_PAYLOADS / p for p in pages))
        except DocDrift as exc:
            print(f"FAIL     {label:44} -> {exc}")
            failures += 1
            continue
        if got_rows != want_rows:
            print(f"FAIL     {label:44} -> the read emitted\n           {got_rows}\n         expected\n"
                  f"           {want_rows}")
            failures += 1
            continue
        print(f"ok       {label:44} -> {len(got_rows)} rows ({why})")
    return failures


# A SPEC WE CANNOT READ MUST NEVER BECOME `none`. That is the one degradation that would rebuild the false
# green inside the parser — "the base branch requires nothing", asserted on the strength of a value we
# failed to parse. Every case below must RAISE, and the CLI turns each into exit 2 with NO verdict.
SPEC_CASES = [
    ("", "an empty spec is not a state"),
    ("declared:", "a `declared:` with no payload"),
    ("declared:[]", "an EMPTY declared list — an empty required set is `none`, a DIFFERENT fact"),
    ("declared:build,test", "THE OLD COMMA FORMAT: unparseable as JSON, and never silently read as `none`"),
    ('declared:{"context": "build", "app": "-"}', "an object where an ARRAY is required"),
    ('declared:[{"context": "build"}]', "a declared check missing its `app` — half a declaration"),
    ('declared:[{"context": "build", "app": 15368}]', "a non-string `app` — a value we cannot compare"),
    ('declared:[{"context": "", "app": "-"}]', "an EMPTY context — it would match nothing, forever"),
    ('declared:[{"context": "build", "app": "null"}]',
     'THE WEDGE, ONE LAYER DOWN: the STRING "null" — a `// "-"` default written AFTER `tostring`, so it '
     "never fired. It binds the check to an app that DOES NOT EXIST, no row can ever match it, and the PR "
     "is `pending (required check absent)` FOREVER. Rejected, and never normalised back to `-`"),
    ('declared:[{"context": "build", "app": "github-actions"}]',
     "an app NAME where the id belongs — a producer that no `app_id` on any row can equal"),
    ('declared:[{"context": "build", "app": ""}]',
     "an EMPTY app — 'binds no app' is `-`, written explicitly; an empty string is a value we failed to write"),
    ("none-declared", "a near-miss of a state name, which is not one of the three"),
    ("NONE", "the right word, the wrong case — states are exact, never close enough"),
]


def spec_test() -> int:
    failures = 0
    for spec, why in SPEC_CASES:
        try:
            got = parse_required_set(spec)
        except SpecError:
            print(f"ok       spec {spec[:30]!r:34} -> REJECTED ({why})")
            continue
        print(f"FAIL     spec {spec[:30]!r:34} -> PARSED as {got} — it must be REJECTED, never degraded")
        failures += 1
    return failures


# The filename rule, every way. A check that cannot FAIL is not a check — `wrong-sha.jsonl` exists because
# the SHA verification once could not fail, and the filename rule was, until now, not checked AT ALL.
# Every case holds the SAME green bytes: only the NAME differs, so the name is the only thing under test.
FILENAME_CASES = [
    (f"ci-35-{FIXTURE_SHA}.txt", GREEN, "all passing", "named for the head_sha it describes — the real artifact's shape"),
    (f"ci-35-{SUPERSEDED_SHA}.txt", UNUSABLE, "names head_sha", "green bytes MISFILED under a superseded sha — caught by the NAME alone"),
    ("green.jsonl", UNUSABLE, "not the artifact's shape", "a name carrying no sha at all proves nothing about which commit it is"),
    # The SHAPE case. The expected sha IS in this name — and the name still names TWO commits, so it says
    # nothing about which one these bytes describe. A check that asked "does the sha appear somewhere?"
    # passed it. The rule is the EXACT shape, never a substring search.
    (f"ci-35-{FIXTURE_SHA}-{SUPERSEDED_SHA}.txt", UNUSABLE, "not the artifact's shape", "an EXTRA sha in the name — the sha is present, and the name is still a lie"),
    # The CASE case. Same 40 hex digits, UPPERCASED. A git object id is lowercase and every producer of
    # ours emits it lowercase, so this name came from something we do not know — and `[0-9a-fA-F]` plus a
    # case-folded comparison waved it through as GREEN. "Close enough" is the substring bug in a new hat.
    (f"ci-35-{FIXTURE_SHA.upper()}.txt", UNUSABLE, "not the artifact's shape", "an UPPERCASE sha — no producer of ours writes one, so this name is not ours"),
]


def filename_test(fixtures: Path) -> int:
    """Copy `green.jsonl` under each name and evaluate WITH the filename rule on."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, want, needle, why in FILENAME_CASES:
            path = Path(tmp) / name
            shutil.copyfile(fixtures / "green.jsonl", path)
            got, reason = evaluate(path, FIXTURE_SHA, required=NO_REQUIRED, expect_filename_sha=True)
            if got == want and needle in reason:
                print(f"ok       {name:28} -> {got:14} ({why})")
            elif got != want:
                print(f"FAIL     {name:28} -> {got:14} expected {want}\n         reason: {reason}")
                failures += 1
            else:
                print(f"FAIL     {name:28} -> {got:14} but the reason does not mention {needle!r}"
                      f"\n         reason: {reason}")
                failures += 1
    return failures


def fail(msg: str) -> NoReturn:
    print(f"ci-snapshot: {msg}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="verify a snapshot against an expected head_sha")
    v.add_argument("--file", required=True, type=Path)
    v.add_argument("--head-sha", required=True)
    v.add_argument(
        "--required-set",
        required=True,
        help=(
            "what the BASE BRANCH requires — the row's `effective_required_set` (the ledger header value is "
            "only its legacy fallback), VERBATIM: "
            "`declared:<json>` | `none` | `unknown` (stage-2-ci.md, 'WHAT WERE WE EXPECTING TO SEE?'). "
            "REQUIRED, with no default: a snapshot can be nonempty and all-passing while a REQUIRED check "
            "has not registered at all — that is no row, not a failing one, and no rule that reads only "
            "the file can see it. `unknown` NEVER goes green."
        ),
    )
    v.add_argument(
        "--expect-filename-sha",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "require the FILENAME to carry the expected head_sha (the artifact is ci-<pr>-<head_sha>.txt). "
            "ON by default: a real snapshot is always named for the commit it describes. Turn it off ONLY "
            "for a file deliberately named by property, e.g. the fixtures."
        ),
    )

    s = sub.add_parser("self-test", help="run every fixture and assert its expected verdict")
    s.add_argument("--fixtures", type=Path, default=Path(__file__).parent / "fixtures" / "ci-snapshot")

    args = p.parse_args()

    if args.cmd == "self-test":
        return self_test(args.fixtures)

    # An operator error is NOT a snapshot verdict. A `--head-sha` that is not a git object id makes every
    # comparison below unfalsifiable-by-construction (the filename rule compares EXACTLY, so an uppercase
    # or truncated sha would report the EVIDENCE as unusable — blaming the file for the caller's mistake).
    # Say so, loudly, and exit 2: no verdict at all beats a verdict about the wrong question.
    if not SHA_RE.match(args.head_sha):
        fail(f"--head-sha {args.head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to verify")
    if not args.file.exists():
        fail(f"no such snapshot: {args.file}")
    # A required-set spec we cannot READ is an operator error too, and it gets the same treatment: exit 2,
    # NO verdict. It must NEVER fall back to `none` — "the base branch requires nothing", asserted from a
    # value we failed to parse, is the false green this flag exists to prevent, rebuilt inside the parser.
    try:
        required = parse_required_set(args.required_set)
    except SpecError as exc:
        fail(f"--required-set: {exc}")
    verdict, reason = evaluate(
        args.file, args.head_sha, required=required, expect_filename_sha=args.expect_filename_sha
    )
    print(f"{verdict}: {reason}")
    # green is the ONLY exit-0 verdict. Everything else — including UNCLASSIFIED — is not a green.
    return 0 if verdict == GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
