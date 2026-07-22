#!/usr/bin/env python3
"""FETCH the canonical `prs.json` snapshot, then compare it against the ledger as FACTS.

This tool owns both MECHANICAL halves of the per-heartbeat reconcile (`references/loop-control.md`,
"Step 1 — reconcile from ground truth"):

* `fetch` constructs the ONE canonical `gh pr list` argv internally, captures stdout as bytes, validates
  the complete snapshot contract, and atomically promotes it to `prs.json`.
* `detect` lines up each ledger row against that file and reports what is OBSERVABLY different.

Neither command names an action. Whether a fact means "refresh the row", "reset the gate", "adopt a
candidate", or "handle a terminal PR" is CAMPAIGN POLICY and stays in the skill prose. The tool observes;
loop-control.md routes. That split keeps detection deterministic without asking each driver to reconstruct
the query whose bytes detection consumes.

    reconcile.py fetch --project-root <project-root> --run-id <id> --output <rundir>/prs.json
    reconcile.py detect --ledger <state.jsonl> --prs <rundir>/prs.json --run-id <id>

`--run-id` selects the label for `fetch` and validates label scope for both commands: every snapshot entry
MUST carry this run's `gauntlet-run-<run-id>` label. A snapshot that escaped that scope is the run-isolation
violation the label exists to prevent, so the whole file is refused — never silently reconciled against
another run's PRs.

FACTS ONLY, and NEUTRAL. The headline signal — a live row that is ABSENT from the snapshot — is the
MERGED/CLOSED-BY-ABSENCE fact: the canonical command lists `--state open`, so a merged or closed PR simply
DROPS OUT of the snapshot, and that absence IS the signal (`references/loop-control.md`, step 1). This tool
reports it as the plain boolean `absent_from_snapshot`; it is NEVER an error, the tool NEVER fetches
anything per-PR to "resolve" it, and routing it (terminal handling) is the skill's job. (A past change broke
adoption by "fixing" absence-based merged-detection with `--state all`, which resurrected merged PRs as
active work — see the repo's CLAUDE.md. Absence is a FACT to report, not a bug to fix.)

`detect` performs NO NETWORK or writes. It is a pure function of two files to one JSON object on stdout.
`fetch` performs one fixed `gh` read and one atomic local promotion. Both fail closed with exit 2 when an
input or response is not evidence. A failed command, malformed response, wrong-label row, possible
limit-boundary truncation, or failed promotion leaves any previous `prs.json` byte-for-byte intact.

The fixture suite is the SIBLING `reconcile-test.py`, this tool's executable contract; `self-test` loads
it by a `__file__`-relative path and FAILS LOUDLY if it is missing — a self-test that passes because it
found no tests is not a passing gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

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

# This module is the executable owner of the snapshot query and validation contract. Prose points to the
# `fetch` command instead of restating its argv.
CANONICAL_BLOCK = '`reconcile.py fetch` (the executable `prs.json` contract)'

# The canonical `--json` field set, as data. `fetch_argv` writes exactly this set and the validators read
# exactly this set, so one tuple binds producer and consumer without a prose copy.
CANONICAL_FIELDS: tuple[str, ...] = (
    "number", "headRefName", "headRefOid", "title", "baseRefName",
    "state", "mergeable", "mergeStateStatus", "labels",
)
SNAPSHOT_LIMIT = 1000
SNAPSHOT_STATE = "open"

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
    """The input or response is not evidence. Commands fail closed without promoting or emitting it."""


def fetch_argv(run_id: str) -> list[str]:
    """Return the ONE canonical PR snapshot argv. Dynamic values stay separate typed argv elements."""
    return [
        "gh", "pr", "list",
        "--label", RUN_LABEL_PREFIX + run_id,
        "--state", SNAPSHOT_STATE,
        "--limit", str(SNAPSHOT_LIMIT),
        "--json", ",".join(CANONICAL_FIELDS),
    ]


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


def _validated_entries(data: object, run_id: str, source: str) -> "list[dict]":
    """Validate decoded snapshot JSON and return typed fact dictionaries.

    Each returned dict carries ONLY the canonical fields, at their validated shapes, plus the extracted
    `label_names`. The run-label scope check runs here: an entry that does not carry `gauntlet-run-<run-id>`
    refuses the WHOLE response because a snapshot that escaped the run's scope cannot be partly trusted.
    """
    if not isinstance(data, list):
        raise Refusal(
            f"{source} is {_SHAPE_NAME.get(type(data), type(data).__name__)}, not a JSON array — "
            f"the canonical `gh pr list --json …` response is an ARRAY of PR objects ({CANONICAL_BLOCK}).")
    run_label = RUN_LABEL_PREFIX + run_id
    entries: list[dict] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(data):
        names = label_names(entry, index)          # reads `labels` through sfield; refuses malformed
        if run_label not in names:
            raise Refusal(
                f"prs.json entry #{index} does not carry this run's label {run_label!r} (it has "
                f"{names!r}) — the snapshot escaped the run's label scope, which is the run-isolation "
                f"violation the canonical fetch's `--label` exists to prevent ({CANONICAL_BLOCK}). "
                f"Refusing the whole response; a snapshot scoped to the wrong PRs is not evidence.")
        fact = {k: sfield(entry, k, index) for k in CANONICAL_FIELDS if k != "labels"}
        fact["label_names"] = names
        number_key = str(fact["number"])
        if number_key in seen:
            raise Refusal(
                f"prs.json lists PR #{number_key} at both entry #{seen[number_key]} and #{index} — a "
                f"snapshot that names one PR twice cannot be reconciled deterministically. Refusing the "
                f"whole response.")
        seen[number_key] = index
        entries.append(fact)
    return entries


def validate_snapshot_bytes(payload: bytes, run_id: str, source: str = "gh response") -> "list[dict]":
    """Decode and validate raw fetch bytes. No write happens until this returns successfully."""
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal(f"{source} is not UTF-8 ({exc}) — undecodable bytes are not snapshot evidence.") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{source} is not valid JSON ({exc}) — the canonical fetch must return a JSON array "
                      f"({CANONICAL_BLOCK}).") from exc
    return _validated_entries(data, run_id, source)


def read_snapshot(prs_path: Path, run_id: str) -> "list[dict]":
    """Read and validate `prs.json`; any failure raises `Refusal`."""
    try:
        payload = prs_path.read_bytes()
    except OSError as exc:
        raise Refusal(f"cannot read the snapshot at {prs_path}: {exc}")
    return validate_snapshot_bytes(payload, run_id, str(prs_path))


def _validate_fetch_paths(project_root: Path, output: Path) -> None:
    """Enforce the runtime adapter's absolute project-root and project-owned output contract."""
    if not project_root.is_absolute():
        raise Refusal(f"--project-root must be an absolute typed RepositoryContext path, got {project_root}")
    if not output.is_absolute():
        raise Refusal(f"--output must be an absolute typed Path, got {output}")
    try:
        root_real = project_root.resolve(strict=True)
        parent_real = output.parent.resolve(strict=True)
    except OSError as exc:
        raise Refusal(f"cannot resolve fetch paths: {exc}") from exc
    if not root_real.is_dir():
        raise Refusal(f"--project-root is not a directory: {project_root}")
    if parent_real != root_real and root_real not in parent_real.parents:
        raise Refusal(f"--output must stay under --project-root: {output}")


def _replace_bytes(path: Path, payload: bytes) -> None:
    """Atomically promote bytes through a same-directory temp, preserving the prior target on failure."""
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def fetch_snapshot(
    project_root: Path,
    output: Path,
    run_id: str,
    *,
    runner: "Callable[..., subprocess.CompletedProcess[bytes]]" = subprocess.run,
) -> int:
    """Fetch, validate, and atomically promote one canonical snapshot. Return validated row count."""
    _validate_fetch_paths(project_root, output)
    argv = fetch_argv(run_id)
    try:
        proc = runner(argv, cwd=project_root, capture_output=True, check=False)
    except OSError as exc:
        raise Refusal(f"could not run canonical `gh pr list`: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise Refusal(f"canonical `gh pr list` exited {proc.returncode}: {stderr}")
    entries = validate_snapshot_bytes(proc.stdout, run_id)
    if len(entries) >= SNAPSHOT_LIMIT:
        raise Refusal(
            f"canonical `gh pr list` returned {len(entries)} rows at its --limit {SNAPSHOT_LIMIT} boundary; "
            "the response may be truncated, so absence is not evidence and no snapshot was promoted.")
    try:
        _replace_bytes(output, proc.stdout)
    except OSError as exc:
        raise Refusal(f"could not atomically promote the validated snapshot to {output}: {exc}") from exc
    return len(entries)


def _facts_for_present_row(row: dict, effective_base: str, entry: dict) -> dict:
    """The facts for a LIVE row that IS present in the snapshot. Neutral observations only — a detected
    change (head/base/branch) appears as a `{ledger, snapshot}` pair, and the verbatim GitHub fields are
    passed through as-is. No key here names or implies an action.

    `effective_base` is THIS row's effective base — its explicit `base_branch`, else the legacy header
    fallback, resolved by the caller through `ledger.py`'s `effective_base(header, row)`. A run may hold
    rows on different bases, so the comparison is per-row, never against the one header base. `base_changed`
    reports the row's RECORDED base as `ledger` and the live `baseRefName` as `snapshot`; loop-control.md
    routes that fact to the machine-blocker park (an unsupported mid-run base change is not migrated)."""
    facts: dict = {"absent_from_snapshot": False}
    # A detected difference is emitted ONLY when it differs — the key's PRESENCE is the fact.
    if entry["headRefOid"] != row["head_sha"]:
        facts["head_moved"] = {"ledger": row["head_sha"], "snapshot": entry["headRefOid"]}
    if entry["baseRefName"] != effective_base:
        facts["base_changed"] = {"ledger": effective_base, "snapshot": entry["baseRefName"]}
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
        facts = _facts_for_present_row(row, L.effective_base(header, row), entry)
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


def cmd_fetch(project_root: Path, output: Path, run_id: str) -> int:
    """Run the canonical fetch and print its result. A refusal emits no stdout and preserves `output`."""
    try:
        entries = fetch_snapshot(project_root, output, run_id)
    except Refusal as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "entries": entries,
        "output": str(output),
        "run_id": run_id,
    }, sort_keys=True))
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
        print("\n1 check(s) FAILED — the reconcile fetch/detect contract is broken.")
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
        print(f"{failures} fixture(s) failed — the reconcile fetch/detect contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the fetch/detect contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch, validate, and atomically promote the canonical prs.json")
    f.add_argument("--project-root", required=True, type=Path,
                   help="absolute repository.project_root from the typed RepositoryContext")
    f.add_argument("--run-id", required=True, help="this run's id — selects and validates its owner label")
    f.add_argument("--output", required=True, type=Path, help="absolute output path (<rundir>/prs.json)")

    d = sub.add_parser("detect", help="emit the per-PR reconcile FACTS (ledger vs prs.json); routes nothing")
    d.add_argument("--ledger", required=True, type=Path, help="the run ledger (<rundir>/state.jsonl)")
    d.add_argument("--prs", required=True, type=Path, help="the batched snapshot (<rundir>/prs.json)")
    d.add_argument("--run-id", required=True, help="this run's id — validates every entry's label scope")

    sub.add_parser("self-test", help="run every fixture (reconcile-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "fetch":
        return cmd_fetch(args.project_root, args.output, args.run_id)
    return cmd_detect(args.ledger, args.prs, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
