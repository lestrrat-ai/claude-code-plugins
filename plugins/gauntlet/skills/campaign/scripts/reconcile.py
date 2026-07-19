#!/usr/bin/env python3
"""COMPARE the batched `prs.json` snapshot against the ledger and emit the per-PR FACTS — nothing else.

This tool does the MECHANICAL half of the per-heartbeat reconcile (`references/loop-control.md`, step 1):
line up each ledger row against the run's `prs.json` snapshot and report, per PR, what is OBSERVABLY
different. It NAMES NO ACTION. Whether a fact means "refresh the row", "reset the gate", "adopt a
candidate", "handle a terminal PR" — that routing is CAMPAIGN POLICY and stays in the skill prose. The
tool observes; loop-control.md step 1 routes. That split is the whole point: detection is deterministic
and testable, routing is judgement, and mixing them is how a routing bug hides inside a comparison.

    reconcile.py detect --ledger <state.jsonl> --prs <rundir>/prs.json --run-id <id>

`--run-id` is used ONLY to validate label scope: every snapshot entry MUST carry this run's
`gauntlet-run-<run-id>` label, because the canonical command that WRITES `prs.json` scopes the listing to
exactly that label (`references/files-and-ledger.md`, "The canonical `prs.json` command"). A snapshot that
escaped that scope is the run-isolation violation the label exists to prevent, so the whole file is
refused — never silently reconciled against another run's PRs.

FACTS ONLY, and NEUTRAL. The headline signal — a live row that is ABSENT from the snapshot — is the
MERGED/CLOSED-BY-ABSENCE fact: the canonical command lists `--state open`, so a merged or closed PR simply
DROPS OUT of the snapshot, and that absence IS the signal (`references/loop-control.md`, step 1). This tool
reports it as the plain boolean `absent_from_snapshot`; it is NEVER an error, the tool NEVER fetches
anything to "resolve" it, and routing it (terminal handling) is the skill's job. (A past change broke
adoption by "fixing" absence-based merged-detection with `--state all`, which resurrected merged PRs as
active work — see the repo's CLAUDE.md. Absence is a FACT to report, not a bug to fix.)

NO NETWORK, NO gh, NO WRITES. `detect` is a pure function of two files to one JSON object on stdout. It
exits 0 whenever the inputs were READABLE — a moved head or a vanished PR is a fact, not a tool failure —
and exits 2 (fail closed) only when an input is not evidence: a file it cannot read, a `prs.json` that is
not a JSON array, an entry missing a canonical field or carrying the wrong run label, or a ledger the
schema owner rejects.

The fixture suite is the SIBLING `reconcile-test.py`, this tool's executable contract; `self-test` loads
it by a `__file__`-relative path and FAILS LOUDLY if it is missing — a self-test that passes because it
found no tests is not a passing gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "reconcile-test.py"       # the fixture suite — this tool's executable contract


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


# The schema owner. `load` (its parser, corruption checks and field normalization) is REUSED, never
# restated — the ledger has exactly one reader, and reconcile is one of its consumers.
L = _load("reconcile_ledger", "ledger.py")

# The block in `references/files-and-ledger.md` that OWNS the `prs.json` `--json` field set and the run
# label. Every refusal about a snapshot points a reader there — the command has one definition, and a
# drifted snapshot is diagnosed against it.
CANONICAL_BLOCK = '"The canonical `prs.json` command" in references/files-and-ledger.md'

# The canonical `--json` field set, as data. This is the set reconcile READS off each snapshot entry, so
# it must match the field set the canonical command WRITES; `transport-contract-test.py` pins the copies
# of that command that live in the docs, and `reconcile-test.py` pins this tuple against the doc block, so
# a field added to the command and not here (or vice versa) fails a test rather than silently dropping a
# fact. Owner of the command: CANONICAL_BLOCK.
CANONICAL_FIELDS: tuple[str, ...] = (
    "number", "headRefName", "headRefOid", "title", "baseRefName",
    "state", "mergeable", "mergeStateStatus", "labels",
)

# The run-owner label prefix (`gauntlet-run-<run-id>`) and the two mutually-exclusive status labels whose
# presence reconcile REPORTS (never judges). These mirror `pr-adopt.py`/loop-control.md; the labels are a
# tiny, stable vocabulary, reported verbatim.
RUN_LABEL_PREFIX = "gauntlet-run-"
REVIEWING_LABEL = "gauntlet-reviewing"
ACCEPTED_LABEL = "gauntlet-accepted"

# The two TERMINAL row statuses. A terminal row is DONE and reconcile does not compare it against the
# snapshot at all (see `detect`). The status taxonomy is owned by `references/files-and-ledger.md`,
# "`status` held and parked taxonomy"; there is no importable constant for the terminal pair, so it is
# named here and pinned by a sibling fixture.
TERMINAL_STATUSES = ("merged", "aborted")

# The JSON shape reconcile REQUIRES of each canonical field, as a type. `labels` is a list; `number` is an
# integer; everything else is a string. `bool` is a subclass of `int`, so the accessor rejects a boolean
# where an int is declared (a `true` is not a PR number).
_FIELD_SHAPE: dict[str, type] = {
    "number": int,
    "labels": list,
    "headRefName": str,
    "headRefOid": str,
    "title": str,
    "baseRefName": str,
    "state": str,
    "mergeable": str,
    "mergeStateStatus": str,
}
_SHAPE_NAME = {str: "a string", int: "an integer", list: "a list", dict: "an object", bool: "a boolean"}


class Refusal(Exception):
    """The inputs are not evidence. `detect` fails CLOSED (exit 2) — it NEVER emits facts derived from a
    snapshot it could not trust, and it never guesses a value a drifted command failed to write."""


def sfield(entry: object, key: str, index: int) -> object:
    """**THE ONE DOOR every canonical field of a snapshot entry enters through.** No `.get` default: the
    caller does not name a fallback, it names the field, and anything not present at the DECLARED shape is
    a `Refusal`. Absent, null and wrong-type are distinguished so the message says which — an ABSENT field
    is a drifted command, a NULL one is a value we do not understand, and neither is silently coerced.

    `index` is the entry's 0-based position in the `prs.json` array, so a refusal points at the exact row.
    """
    shape = _FIELD_SHAPE[key]
    want = _SHAPE_NAME[shape]
    if not isinstance(entry, dict):
        raise Refusal(
            f"prs.json entry #{index} is {_SHAPE_NAME.get(type(entry), type(entry).__name__)}, not an "
            f"object — a snapshot entry must be a JSON object carrying the canonical field set "
            f"({CANONICAL_BLOCK}).")
    if key not in entry:
        raise Refusal(
            f"prs.json entry #{index} is MISSING the canonical field {key!r} — it is ABSENT, not {want}. A "
            f"snapshot from a DRIFTED command is not evidence: the `--json` field set is owned by "
            f"{CANONICAL_BLOCK}, and every field it names must be present. Refusing the whole file.")
    value = entry[key]
    if value is None:
        raise Refusal(
            f"prs.json entry #{index}: canonical field {key!r} is null, not {want} — a null we did not "
            f"expect is a value we cannot read, never a benign one. The field set is owned by "
            f"{CANONICAL_BLOCK}.")
    # bool is a subclass of int: a JSON boolean must never pass as a `number`.
    if isinstance(value, bool) or not isinstance(value, shape):
        got = _SHAPE_NAME.get(type(value), type(value).__name__)
        raise Refusal(
            f"prs.json entry #{index}: canonical field {key!r} is {got} ({value!r}), not {want} — a value "
            f"of a shape we did not declare is a response we cannot read. The field set is owned by "
            f"{CANONICAL_BLOCK}.")
    return value


def label_names(entry: object, index: int) -> list[str]:
    """The label NAMES on one entry. `labels` itself enters through `sfield` (a required list); each element
    is a GitHub label object `{"name": ...}` (a bare string is tolerated, mirroring `pr-adopt.py`). A
    malformed element is refused — the run-scope check below depends on reading every name, so a name we
    cannot read is a file we cannot trust."""
    labels = sfield(entry, "labels", index)
    names: list[str] = []
    for i, lbl in enumerate(labels):  # type: ignore[union-attr]
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if not isinstance(name, str):
                raise Refusal(
                    f"prs.json entry #{index}: label #{i} has no string `name` ({lbl!r}) — a label whose "
                    f"name we cannot read is a run-scope check we cannot perform. The field set is owned by "
                    f"{CANONICAL_BLOCK}.")
            names.append(name)
        elif isinstance(lbl, str):
            names.append(lbl)
        else:
            raise Refusal(
                f"prs.json entry #{index}: label #{i} is {_SHAPE_NAME.get(type(lbl), type(lbl).__name__)} "
                f"({lbl!r}), not a label object or string. The field set is owned by {CANONICAL_BLOCK}.")
    return names


def read_snapshot(prs_path: Path, run_id: str) -> "list[dict]":
    """Read `prs.json`, validate every entry against the canonical contract, and return the entries as
    typed fact dicts. Any failure raises `Refusal` — this is the fail-closed boundary; nothing downstream
    reads a raw snapshot value.

    Each returned dict carries ONLY the canonical fields, at their validated shapes, plus the extracted
    `label_names`. The run-label scope check runs here: an entry that does not carry `gauntlet-run-<run-id>`
    refuses the WHOLE file (a snapshot that escaped the run's scope cannot be partly trusted)."""
    run_label = RUN_LABEL_PREFIX + run_id
    try:
        raw = prs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot read the snapshot at {prs_path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{prs_path} is not valid JSON ({exc}) — the canonical command writes a JSON array "
                      f"({CANONICAL_BLOCK}).")
    if not isinstance(data, list):
        raise Refusal(
            f"{prs_path} is {_SHAPE_NAME.get(type(data), type(data).__name__)}, not a JSON array — the "
            f"canonical `gh pr list --json …` command writes an ARRAY of PR objects ({CANONICAL_BLOCK}).")

    entries: list[dict] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(data):
        names = label_names(entry, index)          # reads `labels` through sfield; refuses malformed
        if run_label not in names:
            raise Refusal(
                f"prs.json entry #{index} does not carry this run's label {run_label!r} (it has "
                f"{names!r}) — the snapshot escaped the run's label scope, which is the run-isolation "
                f"violation the canonical command's `--label` exists to prevent ({CANONICAL_BLOCK}). "
                f"Refusing the whole file; a snapshot scoped to the wrong PRs is not evidence.")
        fact = {k: sfield(entry, k, index) for k in CANONICAL_FIELDS if k != "labels"}
        fact["label_names"] = names
        number_key = str(fact["number"])
        if number_key in seen:
            raise Refusal(
                f"prs.json lists PR #{number_key} at both entry #{seen[number_key]} and #{index} — a "
                f"snapshot that names one PR twice cannot be reconciled deterministically. Refusing the "
                f"whole file.")
        seen[number_key] = index
        entries.append(fact)
    return entries


def _facts_for_present_row(row: dict, base_branch: str, entry: dict) -> dict:
    """The facts for a LIVE row that IS present in the snapshot. Neutral observations only — a detected
    change (head/base/branch) appears as a `{ledger, snapshot}` pair, and the verbatim GitHub fields are
    passed through as-is. No key here names or implies an action."""
    facts: dict = {"absent_from_snapshot": False}
    # A detected difference is emitted ONLY when it differs — the key's PRESENCE is the fact.
    if entry["headRefOid"] != row["head_sha"]:
        facts["head_moved"] = {"ledger": row["head_sha"], "snapshot": entry["headRefOid"]}
    if entry["baseRefName"] != base_branch:
        facts["base_changed"] = {"ledger": base_branch, "snapshot": entry["baseRefName"]}
    if entry["headRefName"] != row["branch"]:
        facts["branch_mismatch"] = {"ledger": row["branch"], "snapshot": entry["headRefName"]}
    # Verbatim GitHub observations — always reported when present, never judged.
    facts["state"] = entry["state"]
    facts["mergeable"] = entry["mergeable"]
    facts["mergeStateStatus"] = entry["mergeStateStatus"]
    facts["label_facts"] = {
        REVIEWING_LABEL: REVIEWING_LABEL in entry["label_names"],
        ACCEPTED_LABEL: ACCEPTED_LABEL in entry["label_names"],
    }
    return facts


def detect(ledger_path: Path, prs_path: Path, run_id: str) -> dict:
    """PURE (two files in, one dict out). Build the reconcile FACTS. Raises `Refusal` on any non-evidence
    input; otherwise always returns a result (a moved head or a vanished PR is a fact, not a failure)."""
    if not ledger_path.exists():
        raise Refusal(f"no ledger at {ledger_path} — reconcile compares an EXISTING run's ledger against "
                      f"its snapshot; a missing ledger is a wrong path, not an empty run.")
    try:
        header, rows = L.load(ledger_path)
    except SystemExit as exc:
        # `ledger.py` fails a corrupt store via `fail()` -> SystemExit(1), printing its own reason to
        # stderr. Turn that into reconcile's fail-closed refusal rather than letting the exit escape.
        raise Refusal(f"the ledger at {ledger_path} was rejected by the schema owner (see the `ledger:` "
                      f"message above) — a store the owner will not parse is not evidence.") from exc

    entries = read_snapshot(prs_path, run_id)
    by_pr = {str(e["number"]): e for e in entries}
    base_branch = header["base_branch"]

    ledger_prs = {row["pr"] for row in rows}
    result_rows: dict[str, dict] = {}
    terminal_n = live_n = present_n = absent_n = 0
    head_moved_n = base_changed_n = branch_mismatch_n = 0

    for row in rows:
        pr = row["pr"]
        status = row["status"]
        if status in TERMINAL_STATUSES:
            # A terminal row is DONE: it is NOT compared against the snapshot. Even if a matching entry is
            # present (a reopened-after-merge oddity), the tool stays silent beyond `terminal` — presence
            # is not reported, absence is not reported, no change is computed. Nothing is left to detect.
            result_rows[pr] = {"terminal": status}
            terminal_n += 1
            continue
        live_n += 1
        entry = by_pr.get(pr)
        if entry is None:
            # ABSENT — the merged/closed-BY-ABSENCE fact, stated neutrally. NOT an error, and the tool
            # fetches nothing to resolve it. Routing (terminal handling) is the skill's job.
            result_rows[pr] = {"absent_from_snapshot": True}
            absent_n += 1
            continue
        facts = _facts_for_present_row(row, base_branch, entry)
        result_rows[pr] = facts
        present_n += 1
        head_moved_n += "head_moved" in facts
        base_changed_n += "base_changed" in facts
        branch_mismatch_n += "branch_mismatch" in facts

    # UNADOPTED — snapshot entries with NO ledger row of any kind (terminal rows count as adopted, so a
    # terminal PR that reappears is never listed here). Candidate-discovery cares about these; facts only.
    unadopted = [
        {"number": e["number"], "title": e["title"], "headRefName": e["headRefName"]}
        for e in entries if str(e["number"]) not in ledger_prs
    ]

    return {
        "facts_only": True,
        "note": "FACTS ONLY - routing each fact to an action lives in references/loop-control.md, step 1.",
        "run_id": run_id,
        "generated_from": {"ledger": str(ledger_path), "prs": str(prs_path)},
        "rows": result_rows,
        "unadopted": unadopted,
        "counts": {
            "ledger_rows": len(rows),
            "terminal_rows": terminal_n,
            "live_rows": live_n,
            "snapshot_entries": len(entries),
            "present_in_snapshot": present_n,
            "absent_from_snapshot": absent_n,
            "head_moved": head_moved_n,
            "base_changed": base_changed_n,
            "branch_mismatch": branch_mismatch_n,
            "unadopted": len(unadopted),
        },
    }


def cmd_detect(ledger_path: Path, prs_path: Path, run_id: str) -> int:
    """Run `detect`, print its JSON to stdout, exit 0. A `Refusal` prints to stderr and exits 2 — nothing
    is printed to stdout on a refusal, so a caller never parses facts out of a fail-closed run."""
    try:
        result = detect(ledger_path, prs_path, run_id)
    except Refusal as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def _sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture suite is NOT AT {SIBLING} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass.")
    mod = load_module_from_path("reconcile_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    """Run the sibling suite over every fixture. Non-zero on any failure."""
    failures = 0
    try:
        cases = _sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':34} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the reconcile detector's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:34} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:34} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:34} -> {rule}")
    print()
    if failures:
        print(f"{failures} fixture(s) failed — the reconcile detector's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the detector's contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="emit the per-PR reconcile FACTS (ledger vs prs.json); routes nothing")
    d.add_argument("--ledger", required=True, type=Path, help="the run ledger (<rundir>/state.jsonl)")
    d.add_argument("--prs", required=True, type=Path, help="the batched snapshot (<rundir>/prs.json)")
    d.add_argument("--run-id", required=True, help="this run's id — validates every entry's label scope")

    sub.add_parser("self-test", help="run every fixture (reconcile-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    return cmd_detect(args.ledger, args.prs, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
