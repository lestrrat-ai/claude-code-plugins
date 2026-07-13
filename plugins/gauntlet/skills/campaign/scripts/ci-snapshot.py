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

And then THIS file shipped two of its own, both of the same family — evidence that is present but not
counted parses as "nothing wrong":

  * `parse()` SKIPPED blank lines and admitted ANY object with a `row` key, so a row of an unknown type
    was silently DISCARDED — including a FAILING one. A passing check run plus a FAILING row the tool
    did not recognise verified GREEN;
  * the VERIFY rule requires the header AND every evidence row's sha AND **the FILENAME** to equal the
    ledger's head_sha. Only the first two were checked, so green bytes MISFILED under a superseded
    commit's name verified green.

So this file is not a description of the rules. It is the rules, executed, with fixtures that FAIL
when the rules are wrong. `--self-test` is the handoff: give it to a reviewer or a fix subagent and
it answers "does the contract still hold?" without anyone re-reading a paragraph.

  verify    parse a snapshot, verify it against an expected head_sha, and print the verdict
  self-test run every fixture and assert its expected verdict

The verdict vocabulary intentionally includes UNCLASSIFIED — see KNOWN GAP below.
"""

from __future__ import annotations

import argparse
import json
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


# The FOUR row types stage-2-ci.md defines, and the fields each one MUST carry. There is no fifth type,
# and a row of a type we do not recognise is NOT nothing — it is something we FAILED TO UNDERSTAND, and
# failing to understand a row is never grounds to ignore it. An ignored row is invisible to `verify_sha`
# and to `decide`, so a FAILING one parses as "nothing wrong" — which is the exact false-green this whole
# file exists to kill, reproduced inside the tool meant to prevent it. So: reject, never skip.
ROW_FIELDS = {
    "header": ("sha",),
    "checkrun": ("sha", "name", "app_id", "status", "conclusion", "id"),
    "status": ("sha", "context", "state"),
    "witness": ("name", "id"),  # SHA-LESS by design — see verify_sha
}


def parse(path: Path) -> list[dict]:
    """JSONL: EVERY line is one JSON object, header included. No comment line, nothing to special-case.

    A line that does not parse is a corrupt snapshot — not something to skip past. Neither is a BLANK
    line: "every line is one JSON object, with NO exceptions" means a blank one is a defect in the
    producer, and a producer we cannot trust to write the file we specified is not a producer whose
    output we can green off.

    A row is admitted ONLY if its type is one of the four and it carries every field that type requires.
    A malformed row is not evidence either: a `checkrun` with no `status` cannot be judged, and a rule
    that cannot judge a row must never conclude it is fine.
    """
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            raise SnapshotError(f"line {n} is blank — the artifact is JSONL, every line is one object")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"line {n} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or "row" not in row:
            raise SnapshotError(f"line {n} is not a row object")
        kind = row["row"]
        if kind not in ROW_FIELDS:
            raise SnapshotError(
                f"line {n} is an UNRECOGNISED row type {kind!r} — the contract defines only "
                f"{', '.join(sorted(ROW_FIELDS))}. A row we cannot read is NOT a row we may ignore."
            )
        missing = [f for f in ROW_FIELDS[kind] if row.get(f) is None]
        if missing:
            raise SnapshotError(f"line {n}: {kind} row is missing required field(s) {', '.join(missing)}")
        rows.append(row)
    return rows


def verify_filename(path: Path, expected_sha: str) -> None:
    """The FILENAME must carry the expected head_sha too — the artifact is `ci-<pr>-<head_sha>.txt`.

    stage-2-ci.md's VERIFY rule names THREE things that must equal the ledger's head_sha: the header, every
    evidence row's sha, AND the filename. The filename is OURS, like the header, so it cannot catch a
    wrong-commit FETCH — what it catches is a MISFILED artifact: a stale `ci-<pr>-<old_sha>.txt` still
    sitting in the rundir, read as if it described the current head. Verifying the bytes of a file while
    never checking WHICH file you read is a hole big enough to green a superseded commit through.
    """
    if expected_sha not in path.stem.split("-"):
        raise SnapshotError(
            f"filename {path.name!r} does not carry the expected head_sha {expected_sha!r} — "
            f"the artifact is named ci-<pr>-<head_sha>.txt; this one describes another commit "
            f"(or is misfiled)"
        )


def verify_sha(rows: list[dict], expected_sha: str) -> None:
    """Every EVIDENCE row's sha must equal the expected head_sha.

    The power of this check comes entirely from the rows carrying what GITHUB said (`.head_sha` on a
    check run, top-level `.sha` on the status response). If they carried a literal we interpolated,
    this would compare a copy against its own source and could never fail — which is exactly the bug
    that shipped. `witness` rows are EXEMPT: the rollup carries no commit oid, so a sha on a witness
    row would be one we INVENTED. Fabricated evidence is worse than none.
    """
    header = [r for r in rows if r["row"] == "header"]
    if len(header) != 1:
        raise SnapshotError(f"expected exactly 1 header row, found {len(header)}")
    if header[0].get("sha") != expected_sha:
        raise SnapshotError("header sha does not match the expected head_sha")

    for r in rows:
        if r["row"] not in ("checkrun", "status"):
            continue  # witness rows carry no sha, by design
        if r.get("sha") != expected_sha:
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
        raise Unverifiable("a witness carries no identity — containment cannot be proven")
    dupes = [i for i, c in Counter(ids).items() if c > 1]
    if dupes:
        raise Unverifiable(f"witness identity is not unique ({dupes[0]}) — containment cannot be proven")

    rest = Counter(r.get("id") for r in rows if r["row"] == "checkrun")
    for ident, want in Counter(ids).items():
        if rest[ident] < want:
            raise SnapshotError(f"REST is missing a witnessed run ({ident}) — evidence incomplete")


class Unverifiable(Exception):
    """Containment cannot be decided. Fail closed; never green."""


def decide(rows: list[dict]) -> tuple[str, str]:
    """Decide from the verified file's contents. Returns (verdict, reason)."""
    runs = [r for r in rows if r["row"] == "checkrun"]
    statuses = [r for r in rows if r["row"] == "status"]
    evidence = runs + statuses

    if not evidence:
        return PENDING, "zero evidence rows — nothing has registered yet (NOT green)"

    for r in runs:
        c = r.get("conclusion")
        if r.get("status") == TERMINAL_STATUS and c in FAIL_CONCLUSIONS:
            return RED, f"{r['name']} concluded {c}"
    for s in statuses:
        if s.get("state") in STATUS_FAIL:
            return RED, f"commit status {s['context']} is {s['state']}"

    for r in runs:
        if r.get("status") != TERMINAL_STATUS:
            return PENDING, f"{r['name']} is {r.get('status')} — still running"
    for s in statuses:
        if s.get("state") in STATUS_RUNNING:
            return PENDING, f"commit status {s['context']} is {s['state']}"

    # Every remaining row is terminal. Anything not explicitly mapped falls HERE, not into green.
    for r in runs:
        c = r.get("conclusion")
        if c not in PASS_CONCLUSIONS:
            return UNCLASSIFIED, f"{r['name']} concluded {c} — no rule maps it (KNOWN GAP)"
    for s in statuses:
        if s.get("state") not in STATUS_PASS:
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

# Each fixture pins one property of the contract. If a rule is ever weakened, the fixture that
# proves it goes green when it should not — and this exits non-zero.
EXPECTED = {
    "green.jsonl": (GREEN, "names contain SPACES and survive the round-trip"),
    "wrong-sha.jsonl": (UNUSABLE, "evidence describes a superseded commit — the check that could not fail before"),
    "duplicate-witness-id.jsonl": (UNVERIFIABLE, "colliding identity cannot prove containment — fail CLOSED"),
    "missing-witness.jsonl": (UNUSABLE, "REST is missing a run the rollup saw"),
    "zero-rows.jsonl": (PENDING, "zero rows is NOT green"),
    "red-status-only.jsonl": (RED, "a commit-status failure invisible to /check-runs — why BOTH families are read"),
    "known-gap-skipped.jsonl": (UNCLASSIFIED, "SKIPPED matches no rule — the disclosed gap, made executable"),
    "unknown-row-type.jsonl": (UNUSABLE, "a FAILING row of an unknown type — skipping it greened a red commit"),
    "blank-line.jsonl": (UNUSABLE, "JSONL has NO blank lines — a producer we cannot trust is not evidence"),
    "malformed-checkrun.jsonl": (UNUSABLE, "a checkrun with no status/conclusion cannot be judged — never 'fine'"),
    # NEGATIVE CONTROL. Same wrong-commit data as wrong-sha.jsonl, but stamped the OLD way: the sha
    # interpolated from our own literal onto every row instead of taken from GitHub. It comes back
    # GREEN — which is the POINT. It is the proof that the old verification could not fail, and the
    # reason `verify_sha` is worth anything at all. If this fixture ever stops returning green, the
    # bug it memorialises has been reintroduced somewhere else.
    "negative-control-circular-sha.jsonl": (GREEN, "PROVES the old self-stamped check was VACUOUS — it passes data from the WRONG COMMIT"),
}


def self_test(fixtures: Path) -> int:
    """Every fixture is evaluated against the SAME expected head_sha the ledger would hold.

    `wrong-sha.jsonl` is the one that matters most: its header carries the sha we REQUESTED (ours),
    while its evidence rows carry the sha GitHub actually returned for a superseded commit. Under the
    old circular scheme every row was stamped from our own literal, so that file would have verified
    CLEAN. Here it must come back UNUSABLE.
    """
    failures = 0
    for name, (want, why) in sorted(EXPECTED.items()):
        path = fixtures / name
        if not path.exists():
            print(f"MISSING  {name}")
            failures += 1
            continue
        # The fixtures are named by PROPERTY, not by SHA, so the filename rule is exercised separately.
        got, reason = evaluate(path, FIXTURE_SHA, expect_filename_sha=False)
        if got == want:
            print(f"ok       {name:28} -> {got:14} ({why})")
        else:
            print(f"FAIL     {name:28} -> {got:14} expected {want}\n         reason: {reason}")
            failures += 1

    failures += filename_test(fixtures)
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the CI-snapshot contract is broken.")
        return 1
    print(f"all {len(EXPECTED)} fixtures + {len(FILENAME_CASES)} filename cases hold — the contract is intact.")
    return 0


# The filename rule, both ways. A check that cannot FAIL is not a check — `wrong-sha.jsonl` exists because
# the SHA verification once could not fail, and the filename rule was, until now, not checked AT ALL.
# Both cases hold the SAME green bytes: only the NAME differs, so the name is the only thing under test.
FILENAME_CASES = [
    (f"ci-35-{FIXTURE_SHA}.txt", GREEN, "named for the head_sha it describes — the real artifact's shape"),
    (f"ci-35-{SUPERSEDED_SHA}.txt", UNUSABLE, "green bytes MISFILED under a superseded sha — caught by the NAME alone"),
    ("green.jsonl", UNUSABLE, "a name carrying no sha at all proves nothing about which commit it is"),
]


def filename_test(fixtures: Path) -> int:
    """Copy `green.jsonl` under each name and evaluate WITH the filename rule on."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, want, why in FILENAME_CASES:
            path = Path(tmp) / name
            shutil.copyfile(fixtures / "green.jsonl", path)
            got, reason = evaluate(path, FIXTURE_SHA, expect_filename_sha=True)
            if got == want:
                print(f"ok       {name:28} -> {got:14} ({why})")
            else:
                print(f"FAIL     {name:28} -> {got:14} expected {want}\n         reason: {reason}")
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

    if not args.file.exists():
        fail(f"no such snapshot: {args.file}")
    verdict, reason = evaluate(args.file, args.head_sha, expect_filename_sha=args.expect_filename_sha)
    print(f"{verdict}: {reason}")
    # green is the ONLY exit-0 verdict. Everything else — including UNCLASSIFIED — is not a green.
    return 0 if verdict == GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
