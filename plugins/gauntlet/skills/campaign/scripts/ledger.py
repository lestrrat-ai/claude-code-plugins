#!/usr/bin/env python3
"""Schema-owning accessor for the campaign ledger (state.md).

The store stays a plaintext, cat/grep-able file: a small `key: value` run-config
header block, a blank line, one pipe-table header line naming the columns, then
one `val | val | ...` row per adopted PR. This script owns the schema ONCE (the
field lists below) so callers read/write by FIELD NAME and never by column
position — adding a column here can't silently shift every offset in the skill.
"""

from __future__ import annotations

import argparse
import json
import sys

DESCRIPTION = "Schema-owning accessor for the campaign ledger (state.md)."
from pathlib import Path
from typing import NoReturn

# --- schema (owned here, once) ------------------------------------------------

HEADER_FIELDS = ("run_id", "base_branch", "api_changes", "reviewer", "branch_ownership")
HEADER_DEFAULTS = {
    "run_id": "-",
    "base_branch": "-",
    "api_changes": "ask",
    "reviewer": "default",
    "branch_ownership": "declined",
}

ROW_FIELDS = (
    "id", "slug", "branch", "worktree", "worktree_owned", "branch_owned", "pr",
    "head_sha", "reviews_ok", "ci", "tier", "attempts", "started",
    "api_approval", "status",
)
ROW_DEFAULTS = {
    "id": "-", "slug": "-", "branch": "-", "worktree": "-", "worktree_owned": "-",
    "branch_owned": "-", "pr": "-", "head_sha": "-", "reviews_ok": "0", "ci": "pending",
    "tier": "-", "attempts": "0", "started": "-", "api_approval": "-", "status": "pending",
}


def fail(msg: str) -> NoReturn:
    print(f"ledger: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --- parse / serialize --------------------------------------------------------

def load(path: Path) -> "tuple[dict, list[dict]]":
    """Return (header, rows). A missing file yields defaults + no rows."""
    header = dict(HEADER_DEFAULTS)
    rows: list[dict] = []
    if not path.exists():
        return header, rows
    lines = path.read_text().splitlines()
    i = 0
    # header block: leading `key: value` lines (inline `# comments` tolerated)
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            break
        # a run-config line is `key: value`; the table-header line has no colon.
        # (an inline `# a | b` comment must NOT be mistaken for the table.)
        if line.lstrip().startswith("#") or ":" not in line:
            break
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if key in HEADER_FIELDS:
            header[key] = value
        i += 1
    # table: find the column-header line, then read rows under it
    while i < len(lines) and "|" not in lines[i]:
        i += 1
    if i < len(lines):
        i += 1  # skip the column-header line itself
    for line in lines[i:]:
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("|")]
        row = dict(ROW_DEFAULTS)
        for name, cell in zip(ROW_FIELDS, cells):
            row[name] = cell
        rows.append(row)
    return header, rows


def dump(path: Path, header: dict, rows: list[dict]) -> None:
    out = [f"{f}: {header.get(f, HEADER_DEFAULTS[f])}" for f in HEADER_FIELDS]
    out.append("")
    out.append(" | ".join(ROW_FIELDS))
    for row in rows:
        out.append(" | ".join(str(row.get(f, ROW_DEFAULTS[f])) for f in ROW_FIELDS))
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
        print(header.get(args.field, HEADER_DEFAULTS[args.field]))
        return 0
    if args.value is None:
        fail("header set requires a value")
    header[args.field] = args.value
    dump(path, header, rows)
    return 0


def _named_field_values(args) -> dict:
    """Collect the --<row-field> options that were actually supplied."""
    values = {}
    for name in ROW_FIELDS:
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
    row["pr"] = pr
    row["id"] = f"pr{pr}"
    row.update(_named_field_values(args))
    row["pr"] = pr  # --pr is the key, not overridable by a stray --pr option
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
    if args.field:
        check_field(args.field, ROW_FIELDS)
        print(row[args.field])
    else:
        print(json.dumps(row))
    return 0


def cmd_list(path: Path, args) -> int:
    _, rows = load(path)
    if args.where:
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
    parser.add_argument("--file", required=True, help="path to the ledger (state.md)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("header", help="get/set a run-config header field")
    h.add_argument("action", choices=("get", "set"))
    h.add_argument("field")
    h.add_argument("value", nargs="?")

    def add_row_field_opts(p) -> None:
        for name in ROW_FIELDS:
            if name == "pr":
                continue  # pr is the row key, passed via --pr
            p.add_argument(f"--{name}", help=f"row field '{name}'")

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
