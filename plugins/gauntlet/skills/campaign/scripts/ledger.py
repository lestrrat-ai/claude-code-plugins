#!/usr/bin/env python3
"""Schema-owning accessor for the campaign ledger (state.jsonl).

The store is a plaintext, cat/grep/jq-able JSONL file: one JSON object per line.
Line 1 is the run-config header record (`{"type": "header", ...}`); each following
line is one adopted PR's row record (`{"type": "row", ...}`). This script owns the
schema ONCE (the field lists below) so callers read/write by FIELD NAME and never
by column position — adding a field here can't silently shift every offset in the
skill.
"""

from __future__ import annotations

import argparse
import json
import sys

DESCRIPTION = "Schema-owning accessor for the campaign ledger (state.jsonl)."
from pathlib import Path
from typing import NoReturn

# --- schema (owned here, once) ------------------------------------------------

HEADER_FIELDS = ("run_id", "base_branch", "api_changes", "reviewer")
HEADER_DEFAULTS = {
    "run_id": "-",
    "base_branch": "-",
    "api_changes": "ask",
    "reviewer": "default",
}

ROW_FIELDS = (
    "id", "slug", "branch", "worktree", "worktree_owned", "branch_owned", "pr",
    "head_sha", "reviews_ok", "ci", "required_set", "tier", "attempts", "started",
    "api_approval", "status",
)
# `required_set` records the outcome of the required-check-set read (declared | none |
# unknown). It DEFAULTS TO `unknown`: a row that has never had a successful read must not
# read as "no required checks are declared" — the fail-safe is uncertainty, never a claim
# of completeness (`references/stage-2-ci.md`, "Three states, never two").
ROW_DEFAULTS = {
    "id": "-", "slug": "-", "branch": "-", "worktree": "-", "worktree_owned": "-",
    "branch_owned": "-", "pr": "-", "head_sha": "-", "reviews_ok": "0", "ci": "pending",
    "required_set": "unknown", "tier": "-", "attempts": "0", "started": "-",
    "api_approval": "-", "status": "pending",
}


def fail(msg: str) -> NoReturn:
    print(f"ledger: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --- parse / serialize --------------------------------------------------------

def load(path: Path) -> "tuple[dict, list[dict]]":
    """Return (header, rows). A missing file yields defaults + no rows.

    The store is JSONL: one JSON object per line, routed by its `type` key. The
    header record MUST be the first non-blank line and appear exactly once; each
    following `row` record appends a row. Missing fields fill from the defaults.
    Every ingested field value is coerced to `str` (mirroring how `dump()` writes
    via `str()`), so on-disk JSON numbers/bools compare as the string keys the rest
    of the accessor uses; a row's `id` is always recomputed from its normalized
    `pr` (never trusted from the file). An unknown `type` is rejected, not dropped.
    """
    header = dict(HEADER_DEFAULTS)
    rows: list[dict] = []
    if not path.exists():
        return header, rows
    seen_pr: set[str] = set()
    saw_first = False
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            fail(f"malformed JSON on line {n}: {e}")
        if not isinstance(rec, dict):
            fail(f"line {n}: record is not a JSON object")
        kind = rec.get("type")
        if kind not in ("header", "row"):
            fail(f"line {n}: missing or unknown record type {kind!r}")
        # Header must be the first non-blank record and appear exactly once.
        if not saw_first:
            if kind != "header":
                fail(f"line {n}: first record must be the header")
            saw_first = True
        elif kind == "header":
            fail(f"line {n}: unexpected second/out-of-order header record")
        if kind == "header":
            # coerce every value to str, matching dump()'s write side
            for f in HEADER_FIELDS:
                header[f] = str(rec.get(f, HEADER_DEFAULTS[f]))
        else:  # kind == "row"
            row = dict(ROW_DEFAULTS)
            # coerce every value to str first, so 11 and "11" are one key
            for f in ROW_FIELDS:
                row[f] = str(rec.get(f, ROW_DEFAULTS[f]))
            pr = row["pr"]
            # id is derived, never trusted from the file: recompute from pr
            row["id"] = f"pr{pr}"
            # duplicate detection runs on the normalized (string) pr key
            if pr in seen_pr:
                fail(f"line {n}: duplicate row for pr {pr}")
            seen_pr.add(pr)
            rows.append(row)
    # A present file must carry a header record. A genuinely MISSING file is a
    # fresh start (returned defaults above); a present-but-headerless file (empty,
    # all-blank, or truncated) is corrupt — reject it rather than silently reset to
    # defaults, which would drop the run config and every row without complaint.
    if not saw_first:
        fail("line 1: missing header record")
    return header, rows


def dump(path: Path, header: dict, rows: list[dict]) -> None:
    out = [json.dumps({"type": "header", **{f: header.get(f, HEADER_DEFAULTS[f]) for f in HEADER_FIELDS}})]
    for row in rows:
        out.append(json.dumps({"type": "row", **{f: str(row.get(f, ROW_DEFAULTS[f])) for f in ROW_FIELDS}}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


def find_row(rows: list[dict], pr: str) -> "dict | None":
    for row in rows:
        if row.get("pr") == pr:
            return row
    return None


def check_field(name: str, valid: "tuple[str, ...]") -> None:
    if name not in valid:
        fail(f"unknown field '{name}'; valid: {', '.join(valid)}")


# --- subcommands --------------------------------------------------------------

def cmd_header(path: Path, args) -> int:
    header, rows = load(path)
    check_field(args.field, HEADER_FIELDS)
    if args.action == "get":
        if args.value is not None:  # `header get <field>` takes no value; reject a stray extra arg
            fail("header get takes no value")
        print(header.get(args.field, HEADER_DEFAULTS[args.field]))
        return 0
    if args.value is None:
        fail("header set requires a value")
    header[args.field] = args.value
    dump(path, header, rows)
    return 0


def _named_field_values(args) -> dict:
    """Collect the --<row-field> options that were actually supplied.

    `pr` is the row key (passed via --pr) and `id` is always derived from it,
    so neither is a settable update field; both are excluded here.
    """
    values = {}
    for name in ROW_FIELDS:
        if name in ("pr", "id"):
            continue
        val = getattr(args, name, None)
        if val is not None:
            values[name] = val
    return values


def cmd_add_row(path: Path, args) -> int:
    header, rows = load(path)
    pr = str(args.pr)
    if find_row(rows, pr) is not None:
        fail(f"a row for pr {pr} already exists; use `set` to update it")
    row = dict(ROW_DEFAULTS)
    row.update(_named_field_values(args))  # pr/id excluded, so both stay derived
    row["pr"] = pr  # --pr is the row key
    row["id"] = f"pr{pr}"  # id is always derived from pr, never caller-set
    rows.append(row)
    dump(path, header, rows)
    print(json.dumps(row))
    return 0


def cmd_set(path: Path, args) -> int:
    header, rows = load(path)
    pr = str(args.pr)
    row = find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}; use `add-row` to create it")
    updates = _named_field_values(args)
    if not updates:
        fail("set requires at least one --<field> <value>")
    row.update(updates)  # by NAME — never by column position
    dump(path, header, rows)
    print(json.dumps(row))
    return 0


def cmd_get(path: Path, args) -> int:
    _, rows = load(path)
    row = find_row(rows, str(args.pr))
    if row is None:
        fail(f"no row for pr {args.pr}")
    if args.field is not None:  # an empty --field is an invalid field, not "omitted"
        check_field(args.field, ROW_FIELDS)
        print(row[args.field])
    else:
        # project onto ROW_FIELDS so the injected `type` key never leaks out
        print(json.dumps({f: row[f] for f in ROW_FIELDS}))
    return 0


def cmd_list(path: Path, args) -> int:
    _, rows = load(path)
    if args.where is not None:  # an empty --where is malformed, not "omitted"
        if "=" not in args.where:
            fail("--where must be <field>=<value>")
        field, _, value = args.where.partition("=")
        check_field(field, ROW_FIELDS)
        rows = [r for r in rows if r.get(field) == value]
    for row in rows:
        print(row["pr"])
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", required=True, help="path to the ledger (state.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("header", help="get/set a run-config header field")
    h.add_argument("action", choices=("get", "set"))
    h.add_argument("field")
    h.add_argument("value", nargs="?")

    def add_row_field_opts(p) -> None:
        for name in ROW_FIELDS:
            if name in ("pr", "id"):
                continue  # pr is the row key (via --pr); id is always derived
            # Canonical flag is dash-form (--reviews-ok); accept the underscore
            # alias too. dest stays the underscore field name so getattr(args,
            # name) in cmd_set/cmd_add_row keeps working.
            opts = [f"--{name.replace('_', '-')}"]
            if "_" in name:
                opts.append(f"--{name}")
            p.add_argument(*opts, dest=name, help=f"row field '{name}'")

    a = sub.add_parser("add-row", help="append a new row for --pr")
    a.add_argument("--pr", required=True, help="PR number (row key)")
    add_row_field_opts(a)

    s = sub.add_parser("set", help="update named fields on the row for --pr")
    s.add_argument("--pr", required=True, help="PR number (row key)")
    add_row_field_opts(s)

    g = sub.add_parser("get", help="print the row for --pr as JSON, or one field")
    g.add_argument("--pr", required=True, help="PR number (row key)")
    g.add_argument("--field", help="print only this field")

    ls = sub.add_parser("list", help="print matching rows' pr numbers")
    ls.add_argument("--where", help="filter as <field>=<value>")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.file)
    handlers = {
        "header": cmd_header, "add-row": cmd_add_row, "set": cmd_set,
        "get": cmd_get, "list": cmd_list,
    }
    return handlers[args.cmd](path, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
