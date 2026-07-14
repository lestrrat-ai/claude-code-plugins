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
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

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


TABLE_DEFAULT_FIELDS = ("pr", "slug", "tier", "reviews_ok", "ci", "attempts", "status", "head_sha")

# Display-only truncation: a 40-char SHA would dominate the table. The full
# value stays on disk and in `get`; the table is a projection, not a source.
TABLE_SHA_WIDTH = 8


def escape_cell(value: str) -> str:
    r"""Make a value safe to render inside the grid — no value may forge the layout.

    A field value is free text (`slug` is a PR title; `ci`-style reason fields hold
    prose), so it can contain the very characters the table's layout is built from.
    Rendered raw, a value carrying a `|` fabricates extra columns, a newline
    fabricates extra ROWS, and a leading `# ` mimics the run-config header lines
    printed above the grid. The table would then say something the ledger never did.

    ESCAPING, not quoting: quoting every cell would widen every column by two chars
    for the common (harmless) case and STILL need an escape for an embedded quote —
    so it buys nothing. Backslash escapes leave every ordinary value byte-identical
    and give the invariant outright: the returned string contains no BARE `|` (every
    one is escaped to `\|`, and a `|` preceded by a backslash can never spell the
    ` | ` column separator), no line break, no control character, and never starts
    with `#`. Each escape is
    unambiguous (a literal backslash is doubled), so the display stays reversible by
    eye — but it is still a DISPLAY form. Read values back with `get --field`, never
    by parsing this table.

    Callers MUST escape BEFORE computing column widths, so the widths measure what is
    actually printed. (Truncation of `head_sha` happens first, on the raw value, so a
    cut can never land inside an escape sequence.)
    """
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "|":
            out.append("\\|")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")  # any other control char
        else:
            out.append(ch)
    text = "".join(out)
    # Only a FIRST-column cell can open a line, but escape a leading '#' in every
    # cell regardless: one value then has one rendering, whatever column it lands in.
    if text.startswith("#"):
        text = "\\" + text
    return text


def cmd_table(path: Path, args) -> int:
    header, rows = load(path)
    if args.fields is not None:  # an empty --fields is malformed, not "omitted"
        fields = tuple(f.strip() for f in args.fields.split(","))
        for f in fields:
            check_field(f, ROW_FIELDS)
    else:
        fields = TABLE_DEFAULT_FIELDS
    # '#' + blank line keep the run-config lines from reading as table columns.
    # Header values are free text too, so they are escaped on the same terms: an
    # un-escaped newline here would inject lines that read as part of the grid.
    for f in HEADER_FIELDS:
        print(f"# {f}: {escape_cell(header[f])}")
    print()
    cells = []
    for row in rows:
        # truncate the RAW sha, then escape — so a cut never splits an escape
        cells.append(tuple(
            escape_cell(row[f][:TABLE_SHA_WIDTH] if f == "head_sha" else row[f])
            for f in fields
        ))
    # widths measure the ESCAPED cells — i.e. exactly the text that gets printed
    widths = [max(len(f), *(len(c[i]) for c in cells)) if cells else len(f)
              for i, f in enumerate(fields)]
    print(" | ".join(f.ljust(w) for f, w in zip(fields, widths)).rstrip())
    print("-+-".join("-" * w for w in widths))
    for c in cells:
        print(" | ".join(v.ljust(w) for v, w in zip(c, widths)).rstrip())
    if not cells:
        print("(no rows)")
    return 0


# --- self-test: the fixtures ARE the contract ---------------------------------
#
# `escape_cell` is the one piece of this file that is SECURITY-SHAPED: it exists so that no field value
# can make the table say something the ledger never did. A sanitizer verified by eye is a sanitizer
# verified by nothing — it is exactly the class of gap that turned `stage-2-ci.md`'s prose contract into
# `ci-snapshot.py`'s executable one, for exactly the same reason. So the rules are executed here, with
# fixtures that FAIL when the rule they pin is removed, and CI runs them.
#
# EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. A fixture that
# would still pass with its rule gone tests nothing and manufactures false confidence. The `->` column
# below names the rule each one stands on. (`ci-snapshot.py` has `mutate-ci-snapshot.py` to CHECK that
# claim mechanically; this suite is small enough that the claim is checked by hand — see the PR.)
#
# The GRID checks are MECHANICAL, never by eye: `grid()` parses the printed table BACK and asserts the
# declared column count and widths. Reading a table and going "looks fine" is how a forged column ships.

# The adversarial values. Every one of them is a value the ledger can genuinely hold (`slug` is a PR
# title; a branch name is free text), and every one is aimed at the grid's own syntax: the column
# separator, the row separator, the run-config prefix — or at the ESCAPES THEMSELVES, which is the
# attack the naive sanitizer misses (escape `|` but not `\`, and `a\|b` and `a|b` collide).
HOSTILE = {
    "pipe": "a|b",                          # forge a COLUMN
    "escaped-pipe": "a\\|b",                # collides with the above unless `\` is doubled
    "backslash": "\\",
    "double-backslash": "\\\\",
    "hex-lookalike": "\\x7c",               # spelled like the escape for U+007C ('|')
    "newline-lookalike": "a\\nb",           # spelled like the escape for a newline
    "newline": "a\nb",                      # forge a ROW
    "crlf": "a\r\nb",
    "tab": "a\tb",
    "nul": "a\x00b",
    "del": "a\x7fb",
    "esc": "a\x1bb",                        # an ANSI escape: control chars never reach the terminal raw
    "leading-hash": "#x",                   # forge a RUN-CONFIG line
    "escaped-hash": "\\#x",                 # collides with the above unless `\` is doubled
    "row-forge": "x\n1 | pwned | | | | | |",
    "config-forge": "main\n# run_id: forged",
    "column-forge": "a | b | c",
    "empty": "",
    "spaces": "   ",
}


class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def run(argv: list[str]) -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr).

    The subcommands are exercised through `main()` — never by calling their internals — so the argparse
    wiring (the `--fields` rejection, the dash/underscore aliases, `fail()`'s exit 1) is under test too.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:  # fail() -> 1; argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def write_lines(path: Path, *lines: str) -> Path:
    """Write a ledger RAW — bypassing dump(), so a fixture can hold what dump() would never emit."""
    path.write_text("".join(line + "\n" for line in lines))
    return path


def header_line(**over: str) -> str:
    return json.dumps({"type": "header", **HEADER_DEFAULTS, **over})


def row_line(**over: str) -> str:
    return json.dumps({"type": "row", **ROW_DEFAULTS, **over})


def grid(out: str, fields: "tuple[str, ...]") -> "tuple[list[str], list[int], list[list[str]]]":
    """Parse the printed table BACK and assert its INTEGRITY. Returns (config lines, widths, cells).

    Three properties, all checked here because all three are what a hostile value attacks:

      * the run config is EXACTLY len(HEADER_FIELDS) lines, each opening `# <field>: `, and NO grid line
        opens with `#` — so no value can forge a config line;
      * every grid line splits into EXACTLY the number of columns the rule line declares, each at the
        declared width — so no value can forge a column;
      * the grid has exactly the lines it should — so no value can forge a row.

    Lines are re-padded to the declared width before splitting, because `cmd_table` rstrips each line
    (trailing padding is noise). Re-padding can only put SPACES back; it cannot invent a column
    boundary. What it can NEVER repair is a raw `|` in a cell — that boundary is in the bytes, and the
    split below then counts one column too many, which is precisely the failure this exists to catch.
    """
    lines = out.split("\n")
    check(lines[-1] == "", "the table output must end in a newline")
    lines = lines[:-1]
    n = len(HEADER_FIELDS)
    check(len(lines) >= n + 3, f"expected {n} run-config lines, a blank line and a grid; got {lines!r}")
    config, lines = lines[:n], lines[n:]
    for f, line in zip(HEADER_FIELDS, config):
        check(line.startswith(f"# {f}: "), f"run-config line for {f!r} is {line!r}")
    check(lines[0] == "", f"a blank line must separate the run config from the grid; got {lines[0]!r}")
    body = lines[1:]
    for line in body:
        check(not line.startswith("#"), f"a GRID line opens with '#' — it reads as a run-config line: {line!r}")
    check(len(body) >= 3, f"the grid needs a column-header line, a rule line and a body: {body!r}")
    colhead, rule, rows = body[0], body[1], body[2:]

    runs = rule.split("-+-")
    check(len(runs) == len(fields), f"the rule line declares {len(runs)} columns, not {len(fields)}: {rule!r}")
    for r in runs:
        check(bool(r) and set(r) == {"-"}, f"malformed rule line: {rule!r}")
    widths = [len(r) for r in runs]
    full = sum(widths) + 3 * (len(widths) - 1)

    def split(line: str) -> list[str]:
        check(len(line) <= full, f"line is wider than the declared grid ({full}): {line!r}")
        parts = line.ljust(full).split(" | ")
        check(len(parts) == len(fields),
              f"line splits into {len(parts)} columns, not the declared {len(fields)}: {line!r}")
        for p, w in zip(parts, widths):
            check(len(p) == w, f"a column is {len(p)} wide, not the declared {w}: {line!r}")
        return parts

    check(split(colhead) == [f.ljust(w) for f, w in zip(fields, widths)],
          f"the column-header line does not name the fields: {colhead!r}")
    if rows == ["(no rows)"]:
        return config, widths, []
    return config, widths, [split(r) for r in rows]


# --- the fixtures -------------------------------------------------------------

def t_escape_injective(tmp: Path) -> None:
    """Two DIFFERENT values NEVER render as the same cell.

    A non-injective escaping is a NEW LIE inside the code written to stop lies: `a|b` and `a\\|b` are
    different values, and if both print as `a\\|b` the table has silently merged them. Doubling the
    BACKSLASH is the whole reason this holds — remove that one branch and the two collide immediately.
    """
    values = sorted(set(
        list(HOSTILE.values())
        + [chr(c) for c in range(0x20)] + ["\x7f", "|", "#", "\\", "\\\\", "\\|", "\\n", "\\r", "\\t"]
        + ["\\x00", "\\x1b", "\\#", "-", "a", "pr1"]
    ))
    seen: dict[str, str] = {}
    for raw in values:
        cell = escape_cell(raw)
        if cell in seen:
            raise SelfTestFailure(
                f"escape_cell is NOT INJECTIVE: {seen[cell]!r} and {raw!r} both render as {cell!r} — "
                f"the table cannot be telling the truth about both"
            )
        seen[cell] = raw


def bare(cell: str) -> "list[str]":
    """The characters in `cell` that are NOT part of an escape sequence and can forge the layout.

    Walk the escaped text the way a reader does: a `\\` opens a two-character escape, so skip both. Every
    OTHER character stands on its own — and a `|`, a line break or a control character standing on its
    own is a grid metacharacter the sanitizer failed to defuse.

    This is the check, and NOT `'|' not in cell`: the escaped form of a pipe IS `\\|`, which contains one.
    What must never appear is a BARE one — and it is the bare one that can spell the ` | ` separator.
    """
    out, i = [], 0
    while i < len(cell):
        if cell[i] == "\\":
            i += 2  # an escape sequence: its introducer plus the character it escapes
            continue
        if cell[i] == "|" or cell[i] == "\n" or cell[i] == "\r" or ord(cell[i]) < 0x20 or ord(cell[i]) == 0x7F:
            out.append(cell[i])
        i += 1
    return out


def t_escape_invariant(tmp: Path) -> None:
    """The escaped cell holds NO BARE grid metacharacter: no unescaped `|`, no line break, no control
    char — and it never opens with `#`. This is what makes the ` | ` separator and the `# ` config prefix
    mean ONE thing wherever they appear."""
    for raw in sorted(set(list(HOSTILE.values()) + [chr(c) for c in range(0x21)] + ["\x7f"])):
        cell = escape_cell(raw)
        check(not bare(cell),
              f"escape_cell({raw!r}) = {cell!r} still carries a BARE {bare(cell)!r} — it can forge the layout")
        check(" | " not in cell,
              f"escape_cell({raw!r}) = {cell!r} spells the column separator — it can forge a COLUMN")
        check("\n" not in cell and "\r" not in cell,
              f"escape_cell({raw!r}) = {cell!r} still carries a line break — it can forge a ROW")
        check(not any(ord(c) < 0x20 or ord(c) == 0x7F for c in cell),
              f"escape_cell({raw!r}) = {cell!r} still carries a control character")
        check(not cell.startswith("#"),
              f"escape_cell({raw!r}) = {cell!r} opens with '#' — it can forge a RUN-CONFIG line")


def t_escape_mapping(tmp: Path) -> None:
    r"""The escape TABLE itself, character by character — and ordinary text left BYTE-IDENTICAL.

    The invariant above is satisfied by more than one escaping (drop the `\n` branch and a newline still
    gets defused, as `\x0a`, by the control-char catch-all — safe, but no longer what this file says it
    prints). The DISPLAY FORM is a promise too: `escape_cell`'s docstring says the value stays readable,
    and every rule that is real but unpinned is a rule that can be deleted with the suite still green.
    That is exactly the gap `mutate-ci-snapshot.py` exists to find, and it found this one here.
    """
    for raw, want in {
        "\\": "\\\\",         # doubled — the escape that makes every OTHER escape unambiguous
        "|": "\\|",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\x00": "\\x00",      # anything else that is a control char: \xNN, lowercase hex
        "\x1b": "\\x1b",
        "\x7f": "\\x7f",
    }.items():
        check(escape_cell(raw) == want, f"escape_cell({raw!r}) = {escape_cell(raw)!r}, not {want!r}")

    check(escape_cell("#x") == "\\#x", f"a LEADING '#' must be escaped: {escape_cell('#x')!r}")
    check(escape_cell("a#b") == "a#b", f"a '#' that is not leading needs no escape: {escape_cell('a#b')!r}")

    # …and the whole reason this is escaping and not quoting: an ordinary value is untouched.
    for plain in ("pr42", "fix/the-thing", "green", "-", "add a table view (#39)", "2026-07-13T10:00:00Z"):
        check(escape_cell(plain) == plain, f"an ordinary value was mangled: {plain!r} -> {escape_cell(plain)!r}")


def t_grid_integrity(tmp: Path) -> None:
    """EVERY hostile value, in EVERY column, and the grid still parses back to the declared shape.

    Checked MECHANICALLY (`grid()` re-parses the output). The `slug` column carries the hostile value
    while `pr` sits FIRST — where a leading `#` would open a line — and a second row holds a benign
    value, so a forged row/column has somewhere to be seen.
    """
    fields = ("slug", "pr", "ci")
    for name, hostile in HOSTILE.items():
        path = tmp / f"grid-{name}.jsonl"
        write_lines(path, header_line(), row_line(pr="1", slug=hostile, ci="green"), row_line(pr="2"))
        code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        _, widths, cells = grid(out, fields)
        check(len(cells) == 2, f"[{name}] the value forged a ROW: {len(cells)} rows printed, not 2\n{out}")
        check(cells[0] == [escape_cell(v).ljust(w) for v, w in zip((hostile, "1", "green"), widths)],
              f"[{name}] the printed row is not the escaped row: {cells[0]!r}\n{out}")

    # …and again with the hostile value FIRST, which is the only position that can open a line — the
    # leading-'#' rule is pinned HERE (`grid()` rejects a body line starting with '#').
    for name, hostile in HOSTILE.items():
        path = tmp / f"first-{name}.jsonl"
        write_lines(path, header_line(), row_line(pr="1", slug=hostile))
        code, out, err = run(["--file", str(path), "table", "--fields", "slug,pr"])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        grid(out, ("slug", "pr"))


def t_widths_from_escaped(tmp: Path) -> None:
    """Column widths are computed from the ESCAPED text — i.e. from what is actually PRINTED.

    Measure the RAW value and every escape makes its cell WIDER than the column reserved for it, so the
    ` | ` separators stop lining up and the grid is ragged from the first hostile value on.
    """
    raw = "a|b|c|d"                 # 7 raw, 11 escaped: `a\|b\|c\|d`… wider than the field name
    escaped = escape_cell(raw)
    check(len(escaped) > len(raw) > len("slug"), "fixture is not measuring anything")
    path = write_lines(tmp / "w.jsonl", header_line(), row_line(pr="1", slug=raw))
    code, out, err = run(["--file", str(path), "table", "--fields", "slug,pr"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, widths, cells = grid(out, ("slug", "pr"))
    check(widths[0] == len(escaped),
          f"the slug column is {widths[0]} wide but prints {len(escaped)} chars ({escaped!r}) — the width "
          f"was measured on the RAW value ({len(raw)}), not the printed one")


def t_config_cannot_be_forged(tmp: Path) -> None:
    """A hostile header value cannot inject a line that READS AS a run-config line.

    `base_branch` and `run_id` are free text too, and they are printed ABOVE the grid as `# <field>: …`.
    An unescaped newline in one of them writes any line it likes into that block.
    """
    path = write_lines(
        tmp / "cfg.jsonl",
        header_line(run_id="real", base_branch="main\n# run_id: forged\n\npr | slug"),
        row_line(pr="1"),
    )
    code, out, err = run(["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    config, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(len(cells) == 1, f"the header value forged a ROW: {out}")
    forged = [line for line in out.split("\n") if line.startswith("# run_id:")]
    check(forged == ["# run_id: real"],
          f"a hostile base_branch forged a run-config line — lines opening '# run_id:': {forged!r}\n{out}")
    check(config[0] == "# run_id: real", f"the real run_id line is {config[0]!r}")


def t_truncation_is_display_only(tmp: Path) -> None:
    """`table` shortens head_sha FOR DISPLAY. The ledger still holds all 40 chars — and `get` still says so.

    A projection that quietly became the source of truth would hand every caller an 8-char prefix, and a
    prefix is not a commit. Also pinned here: truncation happens BEFORE escaping, so a cut can never land
    INSIDE an escape sequence and leave a dangling backslash behind.
    """
    sha = "abcdefg|" + "9" * 32  # the 8-char cut lands EXACTLY on a character that must be escaped
    check(len(sha) == 40, "fixture sha is not 40 chars")
    path = write_lines(tmp / "sha.jsonl", header_line(), row_line(pr="1", head_sha=sha))

    code, out, err = run(["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, widths, cells = grid(out, TABLE_DEFAULT_FIELDS)
    col = TABLE_DEFAULT_FIELDS.index("head_sha")
    cell = cells[0][col].rstrip()
    # truncate(8) THEN escape -> `abcdefg\|` (9 chars). Escape THEN truncate(8) would cut the `\|` in
    # half and print a DANGLING BACKSLASH — a cell that decodes to something the ledger never held.
    check(cell == "abcdefg\\|",
          f"head_sha renders as {cell!r}, not {'abcdefg\\|'!r} — escaping ran BEFORE truncation and the "
          f"cut landed inside an escape sequence")
    check((len(cell) - len(cell.rstrip("\\"))) % 2 == 0,
          f"the rendered head_sha {cell!r} ends in a DANGLING escape — the cut split an escape sequence")
    check(len(cell.rstrip()) < len(sha), "head_sha was not truncated for display at all")

    # …and the FULL value is still what the ledger holds and what `get` returns.
    code, out, _ = run(["--file", str(path), "get", "--pr", "1", "--field", "head_sha"])
    check((code, out) == (0, sha + "\n"), f"get --field head_sha returned {out!r}, not the full 40 chars")
    code, out, _ = run(["--file", str(path), "get", "--pr", "1"])
    check(json.loads(out)["head_sha"] == sha, f"get returned a truncated head_sha: {out!r}")
    check(sha in path.read_text(), "the ON-DISK row does not carry the full head_sha")


def t_table_missing_file(tmp: Path) -> None:
    """A ledger that does not exist yet is a FRESH START, not an error: defaults, and no rows."""
    code, out, err = run(["--file", str(tmp / "nope.jsonl"), "table"])
    check(code == 0, f"table on a missing file exited {code}: {err!r}")
    config, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(cells == [], f"a missing file produced rows: {cells!r}")
    check("(no rows)" in out, "a missing file must still say '(no rows)'")
    check(config[0] == "# run_id: -", f"a missing file must fall back to the defaults; got {config[0]!r}")


def t_table_no_rows(tmp: Path) -> None:
    """A header-only ledger prints the grid and says so — never an empty, wordless table."""
    path = write_lines(tmp / "hdr.jsonl", header_line(run_id="r1"))
    code, out, err = run(["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(cells == [], f"a header-only ledger produced rows: {cells!r}")
    check(out.rstrip().endswith("(no rows)"), f"a header-only ledger must say '(no rows)': {out!r}")


def t_fields_rejected(tmp: Path) -> None:
    """`--fields` is CHECKED, and an EMPTY `--fields` is a malformed field list — never 'omitted'.

    `args.fields is not None` is the rule: falsiness would read `--fields ''` as "the user asked for the
    defaults" and print a table they did not ask for.
    """
    path = write_lines(tmp / "f.jsonl", header_line(), row_line(pr="1"))
    for fields, needle in ((("--fields", ""), "unknown field ''"), (("--fields", "nope"), "unknown field 'nope'")):
        code, out, err = run(["--file", str(path), "table", *fields])
        check(code == 1, f"table {fields!r} exited {code}, not 1 — it was ACCEPTED:\n{out}")
        check(needle in err, f"table {fields!r} failed with {err!r}, which does not mention {needle!r}")


def t_fields_duplicate(tmp: Path) -> None:
    """A field named TWICE prints TWICE — and the grid still declares the columns it actually has."""
    path = write_lines(tmp / "d.jsonl", header_line(), row_line(pr="1", slug="s"))
    code, out, err = run(["--file", str(path), "table", "--fields", "pr,pr"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, ("pr", "pr"))
    check([c.rstrip() for c in cells[0]] == ["1", "1"], f"a duplicated field did not print twice: {cells!r}")


def t_unknown_record_type(tmp: Path) -> None:
    """A record type we do not recognise is REJECTED, never skipped.

    A skipped row is a row nothing reads — and if the campaign ever writes a type this accessor has not
    learned, silently dropping it loses a PR's whole state without a word.
    """
    path = write_lines(tmp / "u.jsonl", header_line(), json.dumps({"type": "note", "pr": "1"}))
    code, _, err = run(["--file", str(path), "list"])
    check(code == 1, f"an unknown record type was ACCEPTED (exit {code})")
    check("unknown record type" in err, f"failed for the wrong reason: {err!r}")


def t_duplicate_row(tmp: Path) -> None:
    """Two rows for one PR is a CORRUPT ledger — `find_row` would silently read the first and `set` would
    update the first while the second stayed behind, disagreeing, forever."""
    path = write_lines(tmp / "dup.jsonl", header_line(), row_line(pr="1"), row_line(pr="1"))
    code, _, err = run(["--file", str(path), "list"])
    check(code == 1, f"a duplicate row was ACCEPTED (exit {code})")
    check("duplicate row for pr 1" in err, f"failed for the wrong reason: {err!r}")
    # …and the duplicate is caught on the NORMALIZED key: a JSON number 1 and the string "1" are ONE pr.
    path = write_lines(tmp / "dup2.jsonl", header_line(),
                       json.dumps({"type": "row", "pr": 1}), row_line(pr="1"))
    code, _, err = run(["--file", str(path), "list"])
    check(code == 1, f"a duplicate row keyed 1 vs \"1\" was ACCEPTED (exit {code})")
    check("duplicate row for pr 1" in err, f"failed for the wrong reason: {err!r}")


def t_missing_header(tmp: Path) -> None:
    """A ledger that EXISTS must carry a header. A missing FILE is a fresh start; a present-but-headerless
    one is CORRUPT — resetting it to defaults would drop the run config and every row without a word."""
    path = write_lines(tmp / "nohdr.jsonl", row_line(pr="1"))
    code, _, err = run(["--file", str(path), "list"])
    check(code == 1, f"a headerless ledger was ACCEPTED (exit {code})")
    check("first record must be the header" in err, f"failed for the wrong reason: {err!r}")

    empty = write_lines(tmp / "empty.jsonl", "", "   ")
    code, _, err = run(["--file", str(empty), "list"])
    check(code == 1, f"an all-blank ledger was ACCEPTED (exit {code})")
    check("missing header record" in err, f"failed for the wrong reason: {err!r}")

    two = write_lines(tmp / "two.jsonl", header_line(run_id="a"), row_line(pr="1"), header_line(run_id="b"))
    code, _, err = run(["--file", str(two), "list"])
    check(code == 1, f"a SECOND header was ACCEPTED (exit {code})")
    check("second/out-of-order header" in err, f"failed for the wrong reason: {err!r}")


def t_id_is_derived(tmp: Path) -> None:
    """`id` is ALWAYS `pr<pr>` — recomputed on load, never trusted from the file and never caller-set."""
    path = write_lines(tmp / "id.jsonl", header_line(), json.dumps({"type": "row", "pr": "7", "id": "pwned"}))
    code, out, err = run(["--file", str(path), "get", "--pr", "7", "--field", "id"])
    check((code, out) == (0, "pr7\n"), f"a forged on-disk id survived load(): {out!r} {err!r}")

    fresh = tmp / "id2.jsonl"
    code, out, err = run(["--file", str(fresh), "add-row", "--pr", "9"])
    check(code == 0, f"add-row exited {code}: {err!r}")
    check(json.loads(out)["id"] == "pr9", f"add-row did not derive id from pr: {out!r}")
    code, out, _ = run(["--file", str(fresh), "get", "--pr", "9", "--field", "id"])
    check(out == "pr9\n", f"the id did not survive the round trip: {out!r}")


def t_defaults_backfill(tmp: Path) -> None:
    """A row written BEFORE a field existed still reads back complete — every absent field takes its default.

    This is what lets a field be ADDED to the schema without migrating the ledger: an old row is not a
    broken row. Drop the back-fill and every accessor starts raising KeyError on real, existing state.
    """
    path = write_lines(tmp / "old.jsonl", header_line(), json.dumps({"type": "row", "pr": "3", "slug": "old"}))
    code, out, err = run(["--file", str(path), "get", "--pr", "3"])
    check(code == 0, f"get on a pre-schema row exited {code}: {err!r}")
    row = json.loads(out)
    check(set(row) == set(ROW_FIELDS), f"get did not project onto ROW_FIELDS: {sorted(row)}")
    for f in ("tier", "status", "ci", "attempts"):
        check(row[f] == ROW_DEFAULTS[f], f"{f} back-filled as {row[f]!r}, not the default {ROW_DEFAULTS[f]!r}")
    check(row["slug"] == "old", "the field the row DID carry was overwritten by its default")
    # A header written before a field existed back-fills the same way.
    code, out, _ = run(["--file", str(path), "header", "get", "reviewer"])
    check(out == HEADER_DEFAULTS["reviewer"] + "\n", f"header default did not back-fill: {out!r}")


def t_values_are_strings(tmp: Path) -> None:
    """Every ingested value is coerced to `str`, so the on-disk JSON's type cannot change a comparison."""
    path = write_lines(tmp / "num.jsonl", header_line(),
                       json.dumps({"type": "row", "pr": 11, "reviews_ok": 2, "worktree_owned": True}))
    code, out, err = run(["--file", str(path), "get", "--pr", "11"])
    check(code == 0, f"get exited {code}: {err!r}")
    row = json.loads(out)
    check(all(isinstance(v, str) for v in row.values()), f"a non-string value survived load(): {row!r}")
    check(row["reviews_ok"] == "2" and row["worktree_owned"] == "True", f"bad coercion: {row!r}")
    code, out, _ = run(["--file", str(path), "list", "--where", "reviews_ok=2"])
    check(out == "11\n", f"--where could not match a value that was a JSON number on disk: {out!r}")


CASES = [
    ("escape-injective", "escape_cell is INJECTIVE — no two values collide", t_escape_injective),
    ("escape-invariant", "the escaped cell holds no bare |, newline, control char, or leading #", t_escape_invariant),
    ("escape-mapping", "the escape table, char by char — and ordinary values left byte-identical", t_escape_mapping),
    ("grid-integrity", "no hostile value forges a column, a row, or a config line", t_grid_integrity),
    ("widths-from-escaped", "column widths measure the ESCAPED text — what is printed", t_widths_from_escaped),
    ("config-not-forgeable", "a hostile header value cannot inject a `# <field>:` line", t_config_cannot_be_forged),
    ("truncation-display-only", "table truncates head_sha; disk and `get` keep all 40 — and the cut precedes the escape", t_truncation_is_display_only),
    ("table-missing-file", "a missing ledger is a fresh start: defaults, (no rows)", t_table_missing_file),
    ("table-no-rows", "a header-only ledger says (no rows)", t_table_no_rows),
    ("fields-rejected", "--fields is checked; an EMPTY --fields is malformed, not omitted", t_fields_rejected),
    ("fields-duplicate", "a field named twice prints twice, and the grid still parses", t_fields_duplicate),
    ("unknown-record-type", "an unrecognised record type is REJECTED, never skipped", t_unknown_record_type),
    ("duplicate-row", "two rows for one pr is a corrupt ledger — on the NORMALIZED key", t_duplicate_row),
    ("missing-header", "a present ledger must carry exactly one header, FIRST", t_missing_header),
    ("id-derived", "id is always pr<pr> — never trusted from the file, never caller-set", t_id_is_derived),
    ("defaults-backfill", "a row written before a field existed still reads back complete", t_defaults_backfill),
    ("values-are-strings", "every ingested value is coerced to str", t_values_are_strings),
]


def self_test() -> int:
    """Run every fixture. Exit 0 iff every rule this file claims to enforce actually holds."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, rule, fn in CASES:
            work = Path(tmpdir) / name
            work.mkdir()
            try:
                fn(work)
            except SelfTestFailure as exc:
                print(f"FAIL     {name:24} -> {rule}\n         {exc}")
                failures += 1
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                # A crash is not a verdict. If the accessor raises where a fixture expected an answer,
                # that fixture has FAILED — it must never be mistaken for a rule nothing tests.
                print(f"FAIL     {name:24} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"ok       {name:24} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the ledger's contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures hold — the ledger's contract is intact.")
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    # NOT `required=True`: `self-test` reads no ledger at all. Every OTHER subcommand does, and main()
    # enforces that through `parser.error` — the same message, usage line and exit 2 argparse itself
    # would have produced, so a forgotten --file fails exactly as loudly as it always did.
    parser.add_argument("--file", help="path to the ledger (state.jsonl)")
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

    t = sub.add_parser("table", help="print the run header and all rows as an aligned table")
    t.add_argument("--fields", help=f"comma-separated row fields to show (default: {','.join(TABLE_DEFAULT_FIELDS)})")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "self-test":  # stdlib only, no ledger, no repo checkout, no network
        return self_test()
    if args.file is None:
        parser.error("the following arguments are required: --file")
    path = Path(args.file)
    handlers = {
        "header": cmd_header, "add-row": cmd_add_row, "set": cmd_set,
        "get": cmd_get, "list": cmd_list, "table": cmd_table,
    }
    return handlers[args.cmd](path, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
