#!/usr/bin/env python3
"""Executable contract for the CI snapshot artifact (stage-2-ci.md).

The rules in `stage-2-ci.md` are prose, and prose cannot be run. Three defects shipped in that
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

The lesson is one lesson, not seven: check the EXACT shape. "The thing I need is in there somewhere" is
not a check — it is the absence of one, and every defect above is that same absence wearing a new hat.

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

  verify    parse a snapshot, verify it against an expected head_sha, and print the verdict
  self-test run every fixture and assert its expected verdict (and its REASON — the reason is the only
            thing that says WHICH rule fired, and a fixture that fails for someone else's reason pins
            nothing)

The verdict vocabulary intentionally includes UNCLASSIFIED — see KNOWN GAP below.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import NoReturn

# --- the contract (stage-2-ci.md) ---------------------------------------------------------------

# A check run that is not COMPLETED can still move on its own.
TERMINAL_STATUS = "COMPLETED"

# Conclusions the rules currently map. This set is KNOWN INCOMPLETE — see UNCLASSIFIED below.
PASS_CONCLUSIONS = {"SUCCESS"}
FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}

# Commit-status states (`StatusContext` has .state and no .conclusion).
STATUS_PASS = {"SUCCESS"}
STATUS_FAIL = {"FAILURE", "ERROR"}  # ERROR IS a failure — never shrug it off as a glitch.
STATUS_RUNNING = {"PENDING", "EXPECTED"}

# Verdicts.
GREEN = "green"
RED = "red"
PENDING = "pending"
UNUSABLE = "unusable"  # the evidence is defective; refetch
UNVERIFIABLE = "unverifiable"  # containment cannot be decided; fail CLOSED
UNCLASSIFIED = "unclassified"  # a conclusion no rule maps — the KNOWN GAP, made executable

# KNOWN GAP (disclosed in stage-2-ci.md, closed by the total-classification PR):
# SKIPPED / NEUTRAL / STARTUP_FAILURE / STALE are real CheckConclusionState values. A COMPLETED run
# holding one of them is not pending (it is COMPLETED), not green (not SUCCESS), and not red (absent
# from FAIL_CONCLUSIONS) — it matches NO rule. Rather than silently bucket it (which is how a hole
# becomes a false green), this returns UNCLASSIFIED, and `fixtures/known-gap-skipped.jsonl` asserts
# it. When the total classification lands, that fixture's expectation changes and this comment goes.


class SnapshotError(Exception):
    """The artifact is not evidence."""


# The FOUR row types stage-2-ci.md defines, and the EXACT field set each one carries. There is no fifth
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
    "checkrun": ("sha", "name", "app_id", "status", "conclusion", "id"),
    "status": ("sha", "context", "state"),
    "witness": ("name", "id"),  # SHA-LESS by design — see verify_sha
}

# The exact key set a row of each type may carry: `row` plus that type's fields. Nothing else.
ROW_KEYS = {kind: {"row", *fields} for kind, fields in ROW_FIELDS.items()}

# The artifact's EXACT name, from stage-2-ci.md's PROMOTE step: `ci-<pr>-<head_sha>.txt`. Matching the
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

    A row is admitted ONLY if its type is one of the four and it carries EXACTLY that type's fields —
    every one it requires, and NOT ONE MORE. A malformed row is not evidence: a `checkrun` with no
    `status` cannot be judged, and a rule that cannot judge a row must never conclude it is fine. A row
    with an EXTRA field is not evidence either, for the mirror-image reason: nothing reads that field, so
    whatever it asserts is neither verified nor refuted — it is present and not counted.

    Structure is part of the shape: the header is "Exactly one, first line" (stage-2-ci.md). A file whose
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
    """
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
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            # MUTATE:not-json:continue
            raise SnapshotError(f"line {n} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or "row" not in row:
            # MUTATE:not-a-row:continue
            raise SnapshotError(f"line {n} is not a row object — every line is an object with a `row` key")
        kind = row["row"]
        if not isinstance(kind, str):
            # MUTATE:row-kind-type:continue
            raise SnapshotError(
                f"line {n}: the `row` field is a {type(kind).__name__}, not a string — a row type we cannot "
                f"even read is not a row we may ignore, and asking whether it is one of the four (a HASH "
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

    stage-2-ci.md's VERIFY rule names THREE things that must equal the ledger's head_sha: the header, every
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
            continue  # witness rows carry no sha, by design
        if r.get("sha") != expected_sha:
            # MUTATE:evidence-sha:pass
            raise SnapshotError(
                f"{r['row']} row {r.get('name') or r.get('context')!r} describes "
                f"{r.get('sha')!r}, not the expected {expected_sha!r} — superseded commit"
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


def decide(rows: list[dict]) -> tuple[str, str]:
    """Decide from the verified file's contents. Returns (verdict, reason)."""
    runs = [r for r in rows if r["row"] == "checkrun"]
    statuses = [r for r in rows if r["row"] == "status"]
    evidence = runs + statuses

    if not evidence:
        # MUTATE:zero-evidence:pass
        return PENDING, "zero evidence rows — nothing has registered yet (NOT green)"

    for r in runs:
        c = r.get("conclusion")
        if r.get("status") == TERMINAL_STATUS and c in FAIL_CONCLUSIONS:
            # MUTATE:checkrun-red:pass
            return RED, f"{r['name']} concluded {c}"
    for s in statuses:
        if s.get("state") in STATUS_FAIL:
            # MUTATE:status-red:pass
            return RED, f"commit status {s['context']} is {s['state']}"

    for r in runs:
        if r.get("status") != TERMINAL_STATUS:
            # MUTATE:checkrun-pending:pass
            return PENDING, f"{r['name']} is {r.get('status')} — still running"
    for s in statuses:
        if s.get("state") in STATUS_RUNNING:
            # MUTATE:status-pending:pass
            return PENDING, f"commit status {s['context']} is {s['state']} — still running"

    # Every remaining row is terminal. Anything not explicitly mapped falls HERE, not into green.
    for r in runs:
        c = r.get("conclusion")
        if c not in PASS_CONCLUSIONS:
            # MUTATE:checkrun-unclassified:pass
            return UNCLASSIFIED, f"{r['name']} concluded {c} — no rule maps it (KNOWN GAP)"
    for s in statuses:
        if s.get("state") not in STATUS_PASS:
            # MUTATE:status-unclassified:pass
            return UNCLASSIFIED, f"commit status {s['context']} is {s['state']} — no rule maps it"

    return GREEN, f"{len(evidence)} evidence rows, all passing, containment holds"


def evaluate(path: Path, expected_sha: str, *, expect_filename_sha: bool = True) -> tuple[str, str]:
    """`expect_filename_sha` is OFF only for the fixtures, which are named by the PROPERTY they pin
    (`green.jsonl`, `wrong-sha.jsonl`, …) rather than by a SHA — their names are documentation. It is ON
    for every real artifact, and `self-test` proves the check still fires (FILENAME_CASES below).
    """
    try:
        if expect_filename_sha:
            verify_filename(path, expected_sha)
        rows = parse(path)
        verify_sha(rows, expected_sha)
        check_containment(rows)
    except Unverifiable as exc:
        return UNVERIFIABLE, str(exc)
    except SnapshotError as exc:
        return UNUSABLE, str(exc)
    return decide(rows)


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
    "running-checkrun.jsonl": (PENDING, "still running", "a check run that is not COMPLETED can still move — it is NOT a green"),
    "pending-status.jsonl": (PENDING, "still running", "a PENDING commit status can still move — it is NOT a green"),
    "expected-status.jsonl": (PENDING, "still running", "EXPECTED = a required status NOT POSTED YET — the registration gap, visible in the evidence for once"),
    "known-gap-skipped.jsonl": (UNCLASSIFIED, "no rule maps it (KNOWN GAP)", "SKIPPED matches no rule — the disclosed gap, made executable"),
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
    # NEGATIVE CONTROL. Same wrong-commit data as wrong-sha.jsonl, but stamped the OLD way: the sha
    # interpolated from our own literal onto every row instead of taken from GitHub. It comes back
    # GREEN — which is the POINT. It is the proof that the old verification could not fail, and the
    # reason `verify_sha` is worth anything at all. If this fixture ever stops returning green, the
    # bug it memorialises has been reintroduced somewhere else.
    "negative-control-circular-sha.jsonl": (GREEN, "all passing", "PROVES the old self-stamped check was VACUOUS — it passes data from the WRONG COMMIT"),
}


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
        got, reason = evaluate(path, FIXTURE_SHA, expect_filename_sha=False)
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
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the CI-snapshot contract is broken.")
        return 1
    print(f"all {len(EXPECTED)} fixtures + {len(FILENAME_CASES)} filename cases hold — the contract is intact.")
    print("`mutate-ci-snapshot.py` is what proves each of them pins its OWN rule. Run it too.")
    return 0


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
            got, reason = evaluate(path, FIXTURE_SHA, expect_filename_sha=True)
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
    verdict, reason = evaluate(args.file, args.head_sha, expect_filename_sha=args.expect_filename_sha)
    print(f"{verdict}: {reason}")
    # green is the ONLY exit-0 verdict. Everything else — including UNCLASSIFIED — is not a green.
    return 0 if verdict == GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
