#!/usr/bin/env python3
"""DECIDE whether a PR may merge — from its ledger row plus its live GitHub view. GATE MACHINERY.

It prints ONE verdict: `merge` (every precondition met) or `not-yet` with a concrete `reason`. It NEVER
merges anything, and it wires into no merge step — deciding and doing are two jobs, and this is only the
first. `gh pr merge` lives in `stage-3-merge.md`, downstream of a `merge` verdict; nothing here runs it.

WHY THIS IS A COMMAND AND NOT A TABLE A DRIVER READS BY EYE. The merge decision crosses FIVE ledger
preconditions (held, open, draft, live head == reviewed head, ci, reviews) and then TWO GitHub enums
(`.mergeable` and `.mergeStateStatus`) that answer DIFFERENT questions — `.mergeable` says the branches
CAN be combined, `.mergeStateStatus` says the merge is PERMITTED RIGHT NOW. Reading one for the other is
the miscross that once turned a BLOCKED merge into an infinite CI watch (`stage-3-merge.md`): a PR that was
`.mergeable = MERGEABLE` with a fully green rollup, blocked only because it was a draft, was mapped to
`ci = pending` and watched forever, because nothing was ever going to move. This tool is the ONE place the
two enums are crossed, so nobody does it by hand and nobody does it wrong.

`.mergeable = MERGEABLE` is NECESSARY BUT NOT SUFFICIENT: it falls THROUGH to `.mergeStateStatus`, which is
the only field that yields `merge`. Both enums are mapped TOTALLY — every value GitHub's schema declares has
its own row, and a value with NO row is a WEDGE, so the catch-all PARKS it rather than guessing. The table
this implements is `references/stage-3-merge.md`'s merge-precondition table, and `doc-check` FAILS the build
if the doc and this code stop enumerating the same value sets — neither may add or drop a value alone.

    merge-check.py check --pr 31 --file <state.jsonl> [--repo owner/name] [--view-json <path>]
    merge-check.py doc-check   assert stage-3-merge.md's two enum sets equal the sets `decide` handles
    merge-check.py self-test   run every fixture (merge-check-test.py), then doc-check

The fixture suite is the SIBLING `merge-check-test.py`, this tool's EXECUTABLE CONTRACT; `self-test` loads
it by a `__file__`-relative path and FAILS LOUDLY if it is missing — a self-test that passes because it
found no tests is not a passing gate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "merge-check-test.py"     # the fixture suite — this tool's executable contract
DOC = _HERE.parent / "references" / "stage-3-merge.md"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


# The schema owner. `HELD_STATUSES`, `REPAIR_STATUS` and `load` are imported, never restated — a new held
# status inherits the merge freeze here with no edit, and the file format has exactly one parser.
L = _load("merge_check_ledger", "ledger.py")
HELD_STATUSES = L.HELD_STATUSES
REPAIR_STATUS = L.REPAIR_STATUS

# `required(tier)` — 1 if TRIVIAL else 2 — is REUSED, never retyped. The rule already lives in `nudge.py`
# (and `review-pass.py`); a third copy here would be the drift this repo keeps killing. So merge-check
# borrows the existing helper rather than spelling `1 if TRIVIAL else 2` a third time.
_N = _load("merge_check_nudge", "nudge.py")
REQUIRED = _N.required


# --- the two GitHub enums, as data so `decide` and `doc-check` read ONE source -------------------
#
# `.mergeable` — MERGEABLE is the ONLY value that does not decide on its own; it FALLS THROUGH to
# `.mergeStateStatus`. So its row is the FALL_THROUGH sentinel, not a verdict. The other two are terminal
# not-yets. A value in NEITHER row is one GitHub added since — the catch-all parks it.
FALL_THROUGH = "fall-through"
MERGEABLE = {
    "MERGEABLE": FALL_THROUGH,
    "CONFLICTING": "conflicts with base — rebase",
    "UNKNOWN": "mergeability not computed yet — re-poll",
}

# `.mergeStateStatus` — CLEAN and HAS_HOOKS are the ONLY two that clear the merge; every other value is a
# terminal not-yet. This is `stage-3-merge.md`'s merge-precondition table, value for value. A value with no
# row here parks via the catch-all in `decide`, never guesses — `doc-check` pins that this set equals the
# doc's.
MERGE = "merge"
NOT_YET = "not-yet"
MERGE_STATE_STATUS = {
    "CLEAN": (MERGE, ""),
    "HAS_HOOKS": (MERGE, ""),
    "BEHIND": (NOT_YET, "base moved ahead — rebase"),
    "DIRTY": (NOT_YET, "conflicts — rebase"),
    "UNSTABLE": (NOT_YET, "a check is non-passing (may still be running) — not campaign's ci signal"),
    "BLOCKED": (NOT_YET, "GitHub says BLOCKED — park awaiting-user"),
    "UNKNOWN": (NOT_YET, "merge state not computed yet — re-poll"),
}


def _merge() -> dict:
    return {"verdict": MERGE, "reason": ""}


def _not_yet(reason: str) -> dict:
    return {"verdict": NOT_YET, "reason": reason}


def _short(sha: str) -> str:
    """A SHA as git abbreviates it, for the REASON only. Equality is always compared on the FULL value."""
    return sha[:7] if len(sha) > 7 else sha


def decide(row: dict, view: dict, *, required) -> dict:
    """PURE. Return `{"verdict": "merge"|"not-yet", "reason": str}` for one PR. No I/O.

    The order is FIRST-FAILING-CHECK-WINS, and it is deliberate: a held PR is FROZEN regardless of counters,
    so it is asked before anything else; the two GitHub enums are asked LAST, only once every ledger
    precondition has already passed. `required` is the gate's `required(tier)` helper, passed in.
    """
    # 1. HELD — a parked or repairing PR is FROZEN, whatever its counters or the enums say.
    status = row["status"]
    if status in HELD_STATUSES or status == REPAIR_STATUS:
        return _not_yet(f"held ({status})")

    # 2. NOT OPEN — a merged/closed PR is not a merge candidate.
    state = view["state"]
    if state != "OPEN":
        return _not_yet(f"pr is {state}, not open")

    # 3. DRAFT — GitHub blocks the merge regardless of CI.
    if view["isDraft"]:
        return _not_yet("draft — park awaiting-user")

    # 4. STALE SHA — the gate was recorded against `row.head_sha`; if the live head has MOVED, every verdict
    #    describes a commit that is no longer the tip. Compared on the FULL sha; displayed short.
    head_now = view["headRefOid"]
    if head_now != row["head_sha"]:
        return _not_yet(
            f"PR head {_short(head_now)} moved off the reviewed SHA {_short(row['head_sha'])} — re-gate")

    # 5. CI — campaign's own SHA-pinned snapshot is the ONLY source of `ci`. `.mergeStateStatus` never feeds
    #    it (that miscross is this tool's founding bug).
    ci = row["ci"]
    if ci != "green":
        return _not_yet(f"ci is {ci}, not green")

    # 6. REVIEWS — the gate tally must meet `required(tier)`.
    ok = int(row["reviews_ok"])
    need = required(row["tier"])
    if ok < need:
        return _not_yet(f"{ok} of {need} approvals")

    # 7. THE TWO GITHUB ENUMS, crossed TOTALLY. `.mergeable` first, then `.mergeStateStatus`.
    mergeable = view["mergeable"]
    handling = MERGEABLE.get(mergeable)
    if handling is None:
        return _not_yet(f"unknown mergeable value {mergeable} — park")
    if handling != FALL_THROUGH:
        return _not_yet(handling)
    # `.mergeable = MERGEABLE`: NOT a licence to merge — decide on `.mergeStateStatus`.
    mss = view["mergeStateStatus"]
    row_mss = MERGE_STATE_STATUS.get(mss)
    if row_mss is None:
        return _not_yet(f"unknown merge state {mss} — park, never guess")
    verdict, reason = row_mss
    return _merge() if verdict == MERGE else _not_yet(reason)


# --- obtain the live PR view ---------------------------------------------------

VIEW_FIELDS = "mergeable,mergeStateStatus,isDraft,state,headRefOid"


class ViewError(Exception):
    """The live PR view could not be obtained. The decision fails CLOSED — never `merge`."""


# Every field `decide` reads off the view, with the JSON type it requires. `isDraft` is a bool; the other
# four are strings. `validate_view` pins this at the boundary so `decide` may assume a shaped view and never
# raises `KeyError`/`TypeError` on a value the caller handed in.
_VIEW_STR_FIELDS = ("mergeable", "mergeStateStatus", "state", "headRefOid")


def validate_view(view: object) -> "str | None":
    """`None` if `view` is a JSON object carrying every field `decide` consumes at the right JSON type;
    otherwise a short description of the FIRST thing wrong. PURE — no I/O. The CLI turns a non-`None` result
    into a fail-closed not-yet, so `decide` is never reached with a malformed view."""
    if not isinstance(view, dict):
        return f"view is not a JSON object (got {type(view).__name__})"
    for field in _VIEW_STR_FIELDS:
        if field not in view:
            return f"missing field {field!r}"
        # bool is a subclass of int, not str, so a JSON string is the only thing that passes here.
        if not isinstance(view[field], str):
            return f"field {field!r} must be a string, got {type(view[field]).__name__}"
    if "isDraft" not in view:
        return "missing field 'isDraft'"
    if not isinstance(view["isDraft"], bool):
        return f"field 'isDraft' must be a bool, got {type(view['isDraft']).__name__}"
    return None


def load_view(pr: str, repo: "str | None", view_json: "str | None") -> dict:
    """The PR's live view — from a recorded `gh pr view` JSON (`--view-json`, testable without gh) or from
    `gh pr view` itself. Any failure raises `ViewError`, which the caller turns into a fail-closed not-yet.
    """
    if view_json is not None:
        try:
            return json.loads(Path(view_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ViewError(str(exc)) from exc
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", repo]
    argv += ["--json", VIEW_FIELDS]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise ViewError(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ViewError(f"gh response is not JSON ({exc})") from exc


def check(pr: str, ledger_path: Path, repo: "str | None", view_json: "str | None") -> int:
    """Read the ledger row + the live view, decide, print the verdict as JSON. Exit 0 on a computed verdict;
    a view that could not be fetched is a fail-closed not-yet, and a non-zero exit is fine there."""
    _header, rows = L.load(ledger_path)
    row = next((r for r in rows if r["pr"] == str(pr)), None)
    if row is None:
        print(json.dumps(_not_yet("no ledger row")))
        return 0
    try:
        view = load_view(pr, repo, view_json)
    except ViewError as exc:
        print(json.dumps(_not_yet(f"could not fetch PR view: {exc}")))
        return 1
    # A syntactically valid but INCOMPLETE/WRONG-TYPED view must fail CLOSED here, never crash `decide` with
    # a KeyError/TypeError and never say `merge`. Mirrors the fetch-failure not-yet above.
    problem = validate_view(view)
    if problem is not None:
        print(json.dumps(_not_yet(f"malformed PR view: {problem}")))
        return 1
    print(json.dumps(decide(row, view, required=REQUIRED)))
    return 0


# --- doc-check: the doc and the code cannot silently disagree ------------------

# The merge-precondition table is DELIMITED in the doc by these two markers, each on its own line — the
# start immediately before the header row, the end immediately after the last row. They make the extraction
# UNAMBIGUOUS: `doc_enum` reads the ONE table between them and nothing else. Without a hard boundary, ANOTHER
# `|`-table elsewhere in the doc (or a value dropped from this table but still named in a second table) could
# supply the token the precondition table no longer has, and the drift-guard would PASS over a lost value.
PRECONDITION_TABLE_START = "<!-- merge-precondition-table:start -->"
PRECONDITION_TABLE_END = "<!-- merge-precondition-table:end -->"


def precondition_table_rows(text: str) -> "list[str] | None":
    """The lines strictly BETWEEN the two precondition-table markers, or `None` if the markers are absent,
    empty, or out of order. `None` is a HARD FAIL for `doc_check` — a table it cannot locate is never a pass.
    """
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == PRECONDITION_TABLE_START), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == PRECONDITION_TABLE_END), None)
    if start is None or end is None or end <= start + 1:
        return None
    return lines[start + 1:end]


def doc_enum(rows: "list[str]", field: str) -> set:
    """The set of `<field>` values the merge-precondition table enumerates, read off the backticked
    `.<field> = VALUE` tokens (e.g. `.mergeStateStatus = CLEAN`).

    `rows` are ONLY the lines between the precondition-table markers (see `precondition_table_rows`), and
    within each table row ONLY the FIRST `|`-delimited cell — the "Field / value" column — is read. Both
    scopings are load-bearing, not cosmetic, and each closes a distinct bypass:

    - Marker scoping: a value dropped from THIS table but still named in prose or in ANOTHER `|`-table
      elsewhere in the doc would otherwise supply the token this table no longer has — the drift-guard would
      PASS while the table it pins has silently lost a value.
    - First-cell scoping: a value dropped from its own row but still mentioned in a later cell (a "Meaning"
      or "Do" cell) of some row INSIDE the markers would likewise sneak the token back in. Only the value
      column declares what the table enumerates; the prose cells describe it and must not contribute.

    The TABLE's value column is the contract this tool implements, so ONLY it is read — nothing else can mask
    a dropped row.
    """
    enum = set()
    for line in rows:
        if not line.lstrip().startswith("|"):
            continue
        cells = line.split("|")
        # cells[0] is the empty string before the leading `|`; cells[1] is the "Field / value" column.
        first_cell = cells[1] if len(cells) > 1 else ""
        enum.update(re.findall(rf"\.{field}\s*=\s*([A-Z_]+)", first_cell))
    return enum


def doc_check(doc: Path) -> int:
    """Assert `stage-3-merge.md` and this code enumerate the SAME two enum value sets.

    Neither the doc nor `decide` may add or drop a `.mergeable` or `.mergeStateStatus` value without the
    other. A check that finds NOTHING must never pass, so absent markers (an unlocatable table) and an empty
    extraction (a renamed/reformatted table) both FAIL exactly as a mismatch does.
    """
    if not doc.exists():
        print(f"FAIL     the doc is not at {doc} — a check that cannot find its subject NEVER passes")
        return 1
    text = doc.read_text(encoding="utf-8")

    rows = precondition_table_rows(text)
    if rows is None:
        print(f"FAIL     the merge-precondition-table markers "
              f"({PRECONDITION_TABLE_START} … {PRECONDITION_TABLE_END}) are absent, empty, or out of order "
              f"in {doc.name} — a drift-guard that cannot LOCATE its table must never report success")
        return 1

    checks = [
        ("mergeable", doc_enum(rows, "mergeable"), set(MERGEABLE),
         "MERGEABLE is necessary-not-sufficient; dropping a value here wedges a PR or merges over it"),
        ("mergeStateStatus", doc_enum(rows, "mergeStateStatus"), set(MERGE_STATE_STATUS),
         "the merge-precondition table; a value in the doc but not the code (or vice versa) is a silent drift"),
    ]
    failures = 0
    for field, doc_set, code_set, why in checks:
        if not doc_set:
            failures += 1
            print(f"FAIL     .{field:20} the doc enumerates ZERO values — the table is GONE, renamed, or "
                  f"reformatted, and a check with no subject must never report success")
            continue
        if doc_set == code_set:
            print(f"ok       .{field:20} {' '.join(sorted(code_set))}")
            continue
        failures += 1
        print(f"FAIL     .{field:20} doc/code DISAGREE\n"
              f"         code says: {' '.join(sorted(code_set))}\n"
              f"         doc says:  {' '.join(sorted(doc_set))}\n"
              f"         missing from the doc: {' '.join(sorted(code_set - doc_set)) or '—'}   "
              f"only in the doc: {' '.join(sorted(doc_set - code_set)) or '—'}\n"
              f"         {why}")
    print()
    if failures:
        print(f"{failures} disagreement(s) between {doc.name} and the code that runs. ONE of them is wrong "
              f"and a reader will believe the other.")
        return 1
    print(f"{len(checks)} checks: {doc.name} and merge-check.py agree — both enum sets, mapped totally.")
    return 0


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def _sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture suite is NOT AT {SIBLING} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass.")
    mod = load_module_from_path("merge_check_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    """Run the sibling suite over every fixture, then `doc-check`. Non-zero on any failure."""
    failures = 0
    try:
        cases = _sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the merge-readiness decider's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:30} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:30} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:30} -> {rule}")
    print()
    # The DOC-CHECK is part of the contract: the code may not silently drift from stage-3-merge.md.
    doc_rc = doc_check(DOC)
    print()
    if failures or doc_rc != 0:
        print(f"{failures} fixture(s) failed and doc-check {'FAILED' if doc_rc else 'passed'} — the "
              f"merge-readiness decider's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold and stage-3-merge.md agrees — the decider's contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="decide merge-readiness for one PR (ledger row + live PR view)")
    c.add_argument("--pr", required=True)
    c.add_argument("--file", required=True, type=Path, help="the run ledger (<rundir>/state.jsonl)")
    c.add_argument("--repo", help="owner/name (default: the current checkout's)")
    c.add_argument("--view-json", help="a recorded `gh pr view` JSON — decide without calling gh")

    d = sub.add_parser("doc-check", help="assert stage-3-merge.md's two enum sets equal the code's")
    d.add_argument("--doc", type=Path, default=DOC)

    sub.add_parser("self-test", help="run every fixture (merge-check-test.py), then doc-check")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "doc-check":
        return doc_check(args.doc)
    return check(args.pr, args.file, args.repo, args.view_json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
