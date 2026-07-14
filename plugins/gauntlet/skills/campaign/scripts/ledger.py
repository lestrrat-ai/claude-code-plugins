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

HEADER_FIELDS = ("run_id", "base_branch", "api_changes", "reviewer", "required_set")
HEADER_DEFAULTS = {
    "run_id": "-",
    "base_branch": "-",
    "api_changes": "ask",
    "reviewer": "default",
    # What `base_branch` REQUIRES (stage-2-ci.md, "WHAT WERE WE EXPECTING TO SEE?", which owns the three
    # states and the format). A property of the BASE BRANCH, not of a PR, so it lives here, not on the rows.
    #
    #   declared:<json>  the required checks, READ. `ci-snapshot.py --required-set` is the one parser.
    #   none             both reads succeeded and the set is EMPTY — a read FACT: nothing is required.
    #   unknown          a read failed. We do not know what was required.
    #
    # The default is `unknown`, and it is LOAD-BEARING, not a placeholder: `unknown` CANNOT GO GREEN
    # (stage-2-ci.md), so a run that never performed the read merges NOTHING. "I have not looked" and "I
    # looked and there are none" are DIFFERENT facts, and the default is the one that claims nothing —
    # a `none` that really meant "I could not see" is how a green is recorded for a commit whose required
    # check never registered.
    "required_set": "unknown",
}

ROW_FIELDS = (
    "id", "slug", "branch", "worktree", "worktree_owned", "branch_owned", "pr",
    "head_sha", "reviews_ok", "ci", "tier", "attempts", "started",
    "api_approval", "status",
    # Liveness (stage-2-ci.md, "SETTLED" and "UNUSABLE — the refetch is BOUNDED"). A non-green `ci` is
    # not enough to know whether CI is still MOVING or has STOPPED — these carry that, and they must
    # survive a context loss (a wake may be a fresh agent instance), so they live on disk and not in the
    # driver's head. A counter that dies with the context never reaches its cap.
    "ci_fingerprint",     # digest of the last VERIFIED CI snapshot; UNCHANGED + nothing running == SETTLED
    "settled_strikes",    # consecutive derivations seen SETTLED-but-not-green; at the cap -> escalate
    "unusable_refetches", # consecutive UNUSABLE snapshots (they have NO fingerprint); at the cap -> escalate
    # UNCHANGED + a row still RUNNING == RUNNING-STALL: something CLAIMS it can still move, and nothing in
    # the check set has. A TIMESTAMP, not a tally, and that is the point: SLOW and DEAD look identical on a
    # fingerprint, and derivations are driven by wakes whose cadence tracks the RUN'S LOAD, not this PR's
    # CI — so a derivation count would park a healthy 40-minute build on a busy run. Only elapsed TIME
    # tells them apart. On disk so `now - ci_stalled_since` needs nothing but the ledger.
    "ci_stalled_since",   # UTC ISO-8601 of the first derivation that saw this stall; at the cap -> escalate
    # The MACHINE-BLOCKER reason: what campaign cannot get past without a human, in a form they can act
    # on -- the question `blocker_ruling` answers. NOT merely "why `ci` is not green": that is one class
    # of it. The merge-precondition parks (stage-3-merge.md: a draft PR, BLOCKED, an unrecognized
    # mergeStateStatus) write it with `ci` GREEN. The `ci_` prefix understates it; files-and-ledger.md
    # owns the definition.
    "ci_reason",
    # Durable answer to a machine-blocker park: - | retry@<iso> | abort@<iso>. Durable AND spent exactly
    # once: set back to `-` on park ENTRY and on consuming a `retry`, so a ruling can only ever answer the
    # park it was written for (`abort` is terminal and is never cleared). stage-2-ci.md, "THE RULING IS
    # CONSUMED EXACTLY ONCE".
    "blocker_ruling",
)
ROW_DEFAULTS = {
    "id": "-", "slug": "-", "branch": "-", "worktree": "-", "worktree_owned": "-",
    "branch_owned": "-", "pr": "-", "head_sha": "-", "reviews_ok": "0", "ci": "pending",
    "tier": "-", "attempts": "0", "started": "-", "api_approval": "-", "status": "pending",
    "ci_fingerprint": "-", "settled_strikes": "0", "unusable_refetches": "0",
    "ci_stalled_since": "-", "ci_reason": "-", "blocker_ruling": "-",
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

# The out-of-band lines the table prints WHERE A ROW WOULD GO — so unlike every other piece of the layout
# they sit in the one region a value also occupies, and position alone cannot tell them apart. They are
# therefore made unforgeable BY CONSTRUCTION rather than by escaping them after the fact: they live in the
# `#` namespace, which `escape_cell()` already proves no cell can enter (a leading `#` is escaped, and a
# body line always opens with its first cell — or with that cell's padding). A bare `(no rows)` was
# forgeable: a row whose only shown field held that literal string rendered a body byte-identical to an
# empty ledger's. That is the SAME CLASS as the header forgery `escape_cell()` exists to kill — an
# out-of-band marker an in-band value can impersonate — so it gets the same answer: put every marker
# somewhere values provably cannot reach. Each new marker below inherits that guarantee for free, and only
# because it opens with `#`.
#
# The two EMPTY-GRID markers are deliberately DIFFERENT LINES, because they are different facts and a
# reader acts differently on each: an empty ledger has adopted nothing, while an all-hidden ledger has
# finished everything. Printing `# (no rows)` for the second would be the exact lie this file exists to
# prevent — a table saying something the ledger never did — and "every PR is merged" is a REAL end-of-run
# state, not a corner case.
TABLE_EMPTY_MARKER = "# (no rows)"
TABLE_ALL_HIDDEN_MARKER = "# (no rows shown — the ledger is NOT empty; every row it holds is hidden)"

# What the DEFAULT view hides. ONLY `merged` — and that is a deliberate call, not a synonym for
# "terminal". A merged PR is DONE: nothing in the run and nobody reading the table has anything left to do
# about it, and in a long run these rows are most of the ledger, crowding out the PRs still in play.
# `aborted` is terminal too, but it is the OPPOSITE kind of terminal — the run GAVE UP on that PR and left
# it open for its owner (`bailout-and-final-report.md`). It is precisely the row a HUMAN may still need to
# act on, so hiding it would bury the run's unfinished business, which is the one thing a status view must
# never do. The line is drawn at "finished successfully", NOT at "reached a terminal state". Everything
# else (in-flight and parked alike) is live work and always shows.
TABLE_HIDDEN_STATUSES = ("merged",)


def _hex_escape(ch: str) -> str:
    """`\\xNN` for a byte-sized code point, `\\uNNNN` above it — the same spelling Python's own repr uses."""
    return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"


def escape_cell(value: str) -> str:
    r"""Make a value safe to render inside the grid — no value may forge the layout, and no two
    values may render THE SAME.

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

    WHITESPACE IS ESCAPED AT THE EDGES, and that is not cosmetic — it is what makes the
    escaping INJECTIVE ONCE PRINTED. The layout pads each cell with `ljust()` and then
    `rstrip()`s the line, and BOTH of those EAT trailing whitespace: with it left raw,
    `""` and `"   "` printed the same blank cell and `"a"` and `"a "` printed the same
    `a`. The sanitizer was injective and the RENDERING was not — which is the same lie,
    one layer down. So a leading or trailing whitespace run is escaped (`\x20` for a
    space) and it survives display. INTERIOR spaces are left alone: escaping them would
    turn every PR title into `add\x20a\x20table`, and nothing eats them.

    Whitespace that is NOT a plain space is escaped WHEREVER it appears: it is invisible
    or line-break-ish, `str.rstrip()` eats every bit of it (NBSP and NEL included, not
    just ASCII), and nothing ordinary contains it. So the guarantee is flat: the escaped
    text NEVER starts or ends with whitespace, and holds no whitespace but the plain
    interior space — which is exactly what lets the printed cell be read back off the
    line by stripping its padding (see `grid()` in the self-test).

    Callers MUST escape BEFORE computing column widths, so the widths measure what is
    actually printed. (Truncation of `head_sha` happens first, on the raw value, so a
    cut can never land inside an escape sequence.)
    """
    # The RAW value's whitespace edges — `lstrip`/`rstrip` here use exactly the definition of
    # whitespace that `cmd_table`'s `rstrip()` will apply to the printed line, so what is escaped is
    # precisely what the layout would otherwise eat.
    lead = len(value) - len(value.lstrip())
    trail = len(value.rstrip())  # index where the trailing whitespace run starts
    out = []
    for i, ch in enumerate(value):
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
        elif ch.isspace() and ch != " ":
            out.append(_hex_escape(ch))  # NBSP, NEL, U+2028, ideographic space…: invisible, and rstrip() eats it
        elif ch == " " and (i < lead or i >= trail):
            out.append("\\x20")  # a LEADING or TRAILING space: the padding would swallow it
        else:
            out.append(ch)
    text = "".join(out)
    # Only a FIRST-column cell can open a line, but escape a leading '#' in every
    # cell regardless: one value then has one rendering, whatever column it lands in.
    # (A leading whitespace run is already escaped above, so a '#' here can only be the
    # value's own first character.)
    if text.startswith("#"):
        text = "\\" + text
    return text


def hidden_notice(n: int) -> str:
    """The line that makes the omission LOUD — printed whenever the default view drops a row.

    A FILTERED VIEW THAT DOES NOT SAY WHAT IT HID IS A LIE BY OMISSION, and it is the same lie as a
    truncated `gh pr list` reporting 30 of 200 PRs without a word: nothing is fabricated, the reader is
    simply never told that what they are looking at is a SUBSET. So the count is stated, and so is the flag
    that reveals the rest — a reader can always get from the filtered view to the whole ledger without
    knowing this file exists.

    The wording is DERIVED from `TABLE_HIDDEN_STATUSES`, never spelled beside it: change what the default
    hides and this line follows by construction, instead of becoming a stale restatement of the rule it
    is supposed to summarise.
    """
    what = "/".join(TABLE_HIDDEN_STATUSES)
    return f"# {n} {what} row{'' if n == 1 else 's'} hidden — pass --all to show every row"


def cmd_table(path: Path, args) -> int:
    header, rows = load(path)
    if args.fields is not None:  # an empty --fields is malformed, not "omitted"
        fields = tuple(f.strip() for f in args.fields.split(","))
        for f in fields:
            check_field(f, ROW_FIELDS)
    else:
        fields = TABLE_DEFAULT_FIELDS
    # The DEFAULT view drops finished work (see TABLE_HIDDEN_STATUSES); --all shows the whole ledger.
    # `--all` composes with `--fields`: one picks the ROWS, the other the COLUMNS, and neither reads the
    # other.
    shown = rows if args.show_all else [r for r in rows if r["status"] not in TABLE_HIDDEN_STATUSES]
    hidden = len(rows) - len(shown)
    # '#' + blank line keep the run-config lines from reading as table columns.
    # Header values are free text too, so they are escaped on the same terms: an
    # un-escaped newline here would inject lines that read as part of the grid.
    for f in HEADER_FIELDS:
        print(f"# {f}: {escape_cell(header[f])}")
    print()
    cells = []
    # ONLY the rows that are actually printed become cells. That is not merely an optimization: it is what
    # keeps a hidden row from reaching the VISIBLE output at all. Build cells from every row and the widths
    # below would still be measured over the hidden ones, so a merged PR with a 200-char slug would silently
    # blow out the columns of a table it does not even appear in — a value nobody printed, changing what
    # the reader sees.
    for row in shown:
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
        # An empty grid is now AMBIGUOUS — nothing adopted, or everything finished — so say WHICH.
        # Both markers live in the '#' namespace: no cell can render a line that impersonates either.
        print(TABLE_EMPTY_MARKER if not rows else TABLE_ALL_HIDDEN_MARKER)
    if hidden:
        print(hidden_notice(hidden))
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
    "empty-marker": "(no rows)",              # forge the EMPTY-LEDGER marker (the old, un-namespaced one)
    "empty-marker-hash": "# (no rows)",       # …and the marker as it is spelled TODAY
    "all-hidden-marker": TABLE_ALL_HIDDEN_MARKER,  # forge "the ledger is not empty, it is all hidden"
    "hidden-notice": "# 99 merged rows hidden — pass --all to show every row",  # forge the HIDDEN COUNT
    "rule-forge": "--------",                 # spelled like the rule line
}

# The WHITESPACE family — the attack on the LAYOUT rather than on the escapes. `ljust()` pads a cell with
# spaces and `cmd_table` rstrips the printed line, so any whitespace at a value's EDGE is eaten by the
# rendering and DISTINCT values print the same bytes. It is the quietest forgery of all: nothing is
# fabricated, two rows simply become indistinguishable, and `""` reads as `"   "` reads as a cell that was
# never there. `\xa0` and `\x85` are in here because `str.rstrip()` eats those too — "whitespace" is not
# just the space bar.
WHITESPACE = {
    "empty": "",
    "space": " ",
    "spaces": "   ",
    "plain": "a",
    "trailing-space": "a ",
    "trailing-spaces": "a  ",
    "leading-space": " a",
    "surrounded": " a ",
    "interior-space": "a b",          # …and THIS one must stay byte-identical: it is an ordinary title
    "nbsp": "a\xa0",                  # U+00A0 — invisible, and rstrip() eats it just the same
    "nel": "a\x85",                   # U+0085 — a line break to str.splitlines(), whitespace to rstrip()
    "ideographic-space": "a　",   # U+3000
    "escaped-space": "a\\x20",        # spelled like the escape for a trailing space
}

HOSTILE.update(WHITESPACE)


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


def body_lines(out: str) -> "list[str]":
    """The table's printed ROW lines, EXACTLY as they came out — the bytes a reader actually sees.

    A `#` line is NOT a row: the markers and the hidden-count notice live in the namespace `escape_cell()`
    proves no cell can enter, so dropping them here cannot drop a row (and a forged one — which prints as
    `\\#…` — is still counted, which is the point).
    """
    body = out.split("\n\n", 1)[1].split("\n")
    check(body[-1] == "", "the table output must end in a newline")
    return [line for line in body[2:-1] if not line.startswith("#")]


def notices(out: str) -> "list[str]":
    """The table's OUT-OF-BAND lines below the grid — the markers and the hidden-count notice."""
    body = out.split("\n\n", 1)[1].split("\n")[:-1]
    return [line for line in body[2:] if line.startswith("#")]


def grid(out: str, fields: "tuple[str, ...]") -> "tuple[list[str], list[int], list[list[str]]]":
    """Parse the printed table BACK and assert its INTEGRITY. Returns (config lines, widths, cells).

    Three properties, all checked here because all three are what a hostile value attacks:

      * the run config is EXACTLY len(HEADER_FIELDS) lines, each opening `# <field>: `, and NO grid line
        opens with `#` — except the out-of-band lines that are ALLOWED to (the empty/all-hidden markers and
        the hidden-count notice), which must form a CONTIGUOUS TRAILING BLOCK below the rows — so no value
        can forge a config line, a marker, or the notice, and none of them can hide BETWEEN rows;
      * every column boundary is EXACTLY where the rule line's widths declare it, and every BARE `|` in
        the line IS one of those boundaries — so no value can forge a column;
      * the grid has exactly the lines it should — so no value can forge a row.

    THE CELLS IT RETURNS ARE THE PRINTED BYTES. `cmd_table` writes `content + padding` in each column and
    then rstrips the line, so this oracle recovers the CONTENT by removing that padding — and that
    recovery is EXACT, but only because `escape_cell()` guarantees an escaped cell never ends in
    whitespace, so every trailing space in a printed column IS padding.

    IT MUST NEVER RE-PAD. The oracle this replaced did: it `ljust`ed the printed line back out to the full
    grid width and handed back PADDED columns, and every fixture then compared them against an equally
    `ljust`ed expectation. Both sides were re-padded, so a collision CAUSED BY the padding could not
    appear on either — and one did: `""` and `"   "` printed the same blank cell, `"a"` and `"a "` the
    same `a`, and this suite stayed green through it. An oracle that normalizes away the thing under test
    is not an oracle. What it hands back is what was PRINTED; nothing else.
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
    check(len(body) >= 3, f"the grid needs a column-header line, a rule line and a body: {body!r}")
    colhead, rule, rest = body[0], body[1], body[2:]
    # Split the row region from the out-of-band lines below it. A '#' line is out-of-band BY CONSTRUCTION
    # (escape_cell() escapes a leading '#', so no cell can open one), and the out-of-band lines are only
    # ever printed AFTER the rows — so they must be a contiguous TRAILING block. A '#' line found before a
    # row line, or on the column-header/rule line, means a cell reached the reserved namespace.
    tail = len(rest)
    while tail and rest[tail - 1].startswith("#"):
        tail -= 1
    rows, out_of_band = rest[:tail], rest[tail:]
    for line in [colhead, rule, *rows]:
        check(not line.startswith("#"), f"a GRID line opens with '#' — it reads as out-of-band text: {line!r}")
    for line in out_of_band:
        check(line in (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER) or line.startswith("# ") and "hidden" in line,
              f"an unrecognised out-of-band line below the grid: {line!r}")
    # An empty grid must SAY which empty it is — never nothing, and never the wrong one.
    if not rows:
        check(bool(out_of_band) and out_of_band[0] in (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER),
              f"an empty grid printed no empty-marker at all: {out_of_band!r}")

    runs = rule.split("-+-")
    check(len(runs) == len(fields), f"the rule line declares {len(runs)} columns, not {len(fields)}: {rule!r}")
    for r in runs:
        check(bool(r) and set(r) == {"-"}, f"malformed rule line: {rule!r}")
    widths = [len(r) for r in runs]
    # Where each column STARTS, and where each separator's '|' sits — fixed by the declared widths alone.
    starts = [sum(widths[:i]) + 3 * i for i in range(len(widths))]
    full = starts[-1] + widths[-1]
    pipes = {s - 2 for s in starts[1:]}

    def cut(line: str) -> list[str]:
        check(len(line) <= full, f"line is wider than the declared grid ({full}): {line!r}")
        check(line == line.rstrip(), f"a printed line ends in whitespace — it was not rstripped: {line!r}")
        # A BARE '|' anywhere but a declared separator is a forged column boundary. This is the check,
        # not a column COUNT: it pins the boundary's POSITION, so a raw `|` smuggled into a cell cannot
        # pass by landing where a separator would have been (a cell's bytes never reach that offset).
        for j, ch in enumerate(line):
            if ch == "|" and (j == 0 or line[j - 1] != "\\"):
                check(j in pipes, f"a BARE '|' at offset {j} is not a declared column boundary: {line!r}")
        cells = []
        for i, (s, w) in enumerate(zip(starts, widths)):
            if i:
                check(line[s - 3:s - 1] == " |",
                      f"the separator before column {i} is not where the widths declare it: {line!r}")
                # rstrip() can reach the separator's TRAILING space — and only that — when every cell
                # after it is empty. Nothing else of the separator is ever missing.
                check(line[s - 1:s] in (" ", ""), f"malformed column separator before column {i}: {line!r}")
            col = line[s:s + w]
            # A column is printed at its declared width — UNLESS the line stops inside it, which is the
            # one thing rstrip() can do: it eats the padding of the trailing columns (and, when they are
            # all empty, the last separator's own trailing space with it). Anything shorter than that is
            # a line that does not fit the grid it declares.
            check(len(col) == w or len(line) <= s + w,
                  f"column {i} is {len(col)} wide, not the declared {w}, and is not the stripped tail: {line!r}")
            cells.append(col.rstrip())  # remove the PADDING — and only the padding: content never ends in ws
        return cells

    check(cut(colhead) == list(fields), f"the column-header line does not name the fields: {colhead!r}")
    return config, widths, [cut(r) for r in rows]


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


def t_render_injective(tmp: Path) -> None:
    """Two DIFFERENT values never render as the same PRINTED ROW. Asserted on the BYTES, not on cells.

    `t_escape_injective` pins injectivity of the SANITIZER. That is not the property anyone relies on —
    what a reader sees is the LINE, and between `escape_cell()` and the line sit `ljust()` and `rstrip()`,
    both of which destroy trailing whitespace. So injectivity was pinned one layer above where it was
    broken: the sanitizer was injective, the RENDERING was not, and this suite was green. This fixture
    pins it where it is actually consumed — the printed row lines of a real table must be pairwise
    DISTINCT — and the WHITESPACE family is what it is aimed at.

    Three shapes, because the layout eats whitespace differently in each: a ONE-COLUMN table (the cell
    owns the whole line, and `rstrip()` hits it directly), the value in the FIRST of two columns (where
    `ljust()`'s padding swallows it instead), and the value in the LAST column (both at once). Only `slug`
    varies; the other column is constant, so a difference between two lines can come from NOTHING but the
    value.
    """
    values = sorted(set(HOSTILE.values()))
    path = tmp / "inj.jsonl"
    write_lines(path, header_line(),
                *(row_line(pr=str(i + 1), slug=v, ci="green") for i, v in enumerate(values)))
    for fields in (("slug",), ("slug", "ci"), ("ci", "slug")):
        code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
        check(code == 0, f"table exited {code}: {err!r}")
        lines = body_lines(out)
        check(len(lines) == len(values),
              f"{fields}: {len(lines)} row lines printed for {len(values)} rows:\n{out}")
        seen: dict[str, str] = {}
        for raw, line in zip(values, lines):
            if line in seen:
                raise SelfTestFailure(
                    f"{fields}: the RENDERING is NOT INJECTIVE: {seen[line]!r} and {raw!r} both print as "
                    f"the row line {line!r} — the table cannot be telling the truth about both"
                )
            seen[line] = raw
        # …and each line really is that value's escaped cell, not merely something distinct.
        _, _, cells = grid(out, fields)
        for raw, row in zip(values, cells):
            check(row[fields.index("slug")] == escape_cell(raw),
                  f"{fields}: {raw!r} printed as {row[fields.index('slug')]!r}, not {escape_cell(raw)!r}")


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
    mean ONE thing wherever they appear.

    …and NO EDGE WHITESPACE, which is the layout's metacharacter rather than the syntax's: `ljust()` and
    `rstrip()` eat it, so a cell that ends in whitespace is a cell whose printed bytes do not say what it
    holds. That guarantee is also what lets `grid()` recover a cell by removing its padding — every
    trailing space in a printed column IS padding, because content can no longer end in one.
    """
    for raw in sorted(set(list(HOSTILE.values()) + [chr(c) for c in range(0x21)]
                          + ["\x7f", "\x85", "\xa0", " ", "　", " a ", "  ", "a\t"])):
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
        check(cell == cell.strip(),
              f"escape_cell({raw!r}) = {cell!r} begins or ends with WHITESPACE — the padding eats it and "
              f"the printed cell no longer says what the value is")
        check(not any(c.isspace() and c != " " for c in cell),
              f"escape_cell({raw!r}) = {cell!r} carries whitespace that is not a plain space — it is "
              f"invisible, and rstrip() eats it")


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

    # Whitespace: escaped at the EDGES, where the layout eats it — and only there.
    for raw, want in {
        " ": "\\x20",
        "   ": "\\x20\\x20\\x20",          # an all-whitespace value is ALL edge
        "a ": "a\\x20",
        " a": "\\x20a",
        " a ": "\\x20a\\x20",
        "a  b": "a  b",                    # INTERIOR spaces are untouched — a title is not a hex dump
        "\xa0": "\\xa0",                   # not a plain space: escaped wherever it sits…
        "a\xa0b": "a\\xa0b",               # …including the interior, where rstrip() would not reach it
        "\x85": "\\x85",
        "　": "\\u3000",                   # above 0xff, so \uNNNN
        "#x ": "\\#x\\x20",                # the '#' escape and the whitespace escape compose
        " #x": "\\x20#x",                  # …and a '#' behind an escaped space no longer opens the cell
    }.items():
        check(escape_cell(raw) == want, f"escape_cell({raw!r}) = {escape_cell(raw)!r}, not {want!r}")

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
        _, _, cells = grid(out, fields)
        check(len(cells) == 2, f"[{name}] the value forged a ROW: {len(cells)} rows printed, not 2\n{out}")
        check(cells[0] == [escape_cell(v) for v in (hostile, "1", "green")],
              f"[{name}] the printed row is not the escaped row: {cells[0]!r}\n{out}")

    # …and again with the hostile value FIRST, which is the only position that can open a line — the
    # leading-'#' rule is pinned HERE (`grid()` rejects a body line starting with '#').
    for name, hostile in HOSTILE.items():
        path = tmp / f"first-{name}.jsonl"
        write_lines(path, header_line(), row_line(pr="1", slug=hostile))
        code, out, err = run(["--file", str(path), "table", "--fields", "slug,pr"])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        grid(out, ("slug", "pr"))

    # …and once more in a ONE-COLUMN table — the narrowest grid there is, where the cell has the whole
    # line to ITSELF and every other line of the table is something it could try to be. This is the shape
    # the empty-marker forgery lived in, so every hostile value is run through it: the grid must still
    # parse back to EXACTLY ONE row, never to an empty ledger and never to a fabricated second one.
    for name, hostile in HOSTILE.items():
        path = tmp / f"one-{name}.jsonl"
        write_lines(path, header_line(), row_line(pr="1", slug=hostile))
        code, out, err = run(["--file", str(path), "table", "--fields", "slug"])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        _, _, cells = grid(out, ("slug",))
        check(cells == [[escape_cell(hostile)]],
              f"[{name}] a one-column grid did not print exactly the one escaped row: {cells!r}\n{out}")


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
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    col = TABLE_DEFAULT_FIELDS.index("head_sha")
    cell = cells[0][col]
    # truncate(8) THEN escape -> `abcdefg\|` (9 chars). Escape THEN truncate(8) would cut the `\|` in
    # half and print a DANGLING BACKSLASH — a cell that decodes to something the ledger never held.
    check(cell == "abcdefg\\|",
          f"head_sha renders as {cell!r}, not {'abcdefg\\|'!r} — escaping ran BEFORE truncation and the "
          f"cut landed inside an escape sequence")
    check((len(cell) - len(cell.rstrip("\\"))) % 2 == 0,
          f"the rendered head_sha {cell!r} ends in a DANGLING escape — the cut split an escape sequence")
    check(len(cell) < len(sha), "head_sha was not truncated for display at all")

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
    check(out.rstrip().endswith(TABLE_EMPTY_MARKER),
          f"a missing file must still say {TABLE_EMPTY_MARKER!r}: {out!r}")
    check(config[0] == "# run_id: -", f"a missing file must fall back to the defaults; got {config[0]!r}")


def t_table_no_rows(tmp: Path) -> None:
    """A header-only ledger prints the grid and says so — never an empty, wordless table."""
    path = write_lines(tmp / "hdr.jsonl", header_line(run_id="r1"))
    code, out, err = run(["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(cells == [], f"a header-only ledger produced rows: {cells!r}")
    check(out.rstrip().endswith(TABLE_EMPTY_MARKER),
          f"a header-only ledger must say {TABLE_EMPTY_MARKER!r}: {out!r}")


def t_empty_marker_not_forgeable(tmp: Path) -> None:
    """A REAL row can NEVER render a body that reads as an EMPTY ledger.

    The marker is the one piece of out-of-band text `table` prints WHERE A ROW WOULD GO, so position
    cannot distinguish it from a value — only the `#` namespace can, and `escape_cell()` is what keeps
    values out of it. The attack is a one-column table (`--fields <f>`, the narrowest grid there is,
    where a cell has the whole line to itself) holding the marker's own text: with an un-namespaced
    marker the body came out BYTE-IDENTICAL to an empty ledger's, and a reader — human or parser — was
    told the run had no PRs at all while a PR sat right there in the store.

    Pins TWO rules at once, and goes red if EITHER is weakened: the marker must live in the `#`
    namespace (drop the `#` and the `(no rows)` case below forges it), and `escape_cell()` must escape a
    leading `#` (drop that branch and the `# (no rows)` case forges it instead).
    """
    empty = write_lines(tmp / "empty.jsonl", header_line(run_id="r1"))
    for name, forgery in (
        ("old-marker", "(no rows)"),
        ("marker", TABLE_EMPTY_MARKER),
        ("marker-no-space", TABLE_EMPTY_MARKER.replace("# ", "#")),
        ("marker-padded", TABLE_EMPTY_MARKER + "   "),
    ):
        for field in ("reviews_ok", "slug", "pr"):
            path = write_lines(tmp / f"{name}-{field}.jsonl", header_line(run_id="r1"),
                               row_line(**{"pr": "1", field: forgery}))
            code, out, err = run(["--file", str(path), "table", "--fields", field])
            check(code == 0, f"[{name}/{field}] table exited {code}: {err!r}")

            code, blank, _ = run(["--file", str(empty), "table", "--fields", field])
            check(code == 0, "table on a header-only ledger must succeed")
            check(out != blank,
                  f"[{name}/{field}] a row holding {forgery!r} renders EXACTLY what an EMPTY ledger "
                  f"renders — the marker is forgeable:\n{out}")

            # …and mechanically: the grid must parse back to ONE row, not to an empty ledger.
            _, _, cells = grid(out, (field,))
            check(len(cells) == 1,
                  f"[{name}/{field}] a row holding {forgery!r} parsed back as an EMPTY ledger "
                  f"({len(cells)} rows) — the marker is forgeable:\n{out}")
            check(cells[0] == [escape_cell(forgery)],
                  f"[{name}/{field}] the printed row is not the escaped row: {cells[0]!r}\n{out}")
            # …and no LINE of a non-empty table IS the marker. (The escaped cell may well CONTAIN the
            # marker's text — `\# (no rows)` does — but it can never BE that line: the `\` is in front of
            # it, which is the whole point of the namespace.)
            body = out.split("\n\n", 1)[1].split("\n")
            check(TABLE_EMPTY_MARKER not in body,
                  f"[{name}/{field}] a line of a NON-EMPTY table is the empty marker:\n{out}")


def t_table_hides_merged(tmp: Path) -> None:
    """The DEFAULT view drops `merged` rows and shows everything else; `--all` shows the whole ledger.

    This is the projection's ROW rule, and it is the mirror of the FIELD rule: a missing row is not a
    missing PR. Both are pinned here — the default really does hide, and `--all` really does reveal.
    """
    path = write_lines(
        tmp / "mix.jsonl", header_line(),
        row_line(pr="1", status="merged"),
        row_line(pr="2", status="in_review"),
        row_line(pr="3", status="merged"),
        row_line(pr="4", status="awaiting-user"),
    )
    code, out, err = run(["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    col = TABLE_DEFAULT_FIELDS.index("pr")
    check([c[col] for c in cells] == ["2", "4"],
          f"the default view did not hide exactly the merged rows: {[c[col] for c in cells]!r}\n{out}")

    code, out, err = run(["--file", str(path), "table", "--all"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check([c[col] for c in cells] == ["1", "2", "3", "4"],
          f"--all did not show every row: {[c[col] for c in cells]!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, so it must claim nothing was hidden: {notices(out)!r}")


def t_table_hidden_count(tmp: Path) -> None:
    """THE OMISSION IS NEVER SILENT — and the count it states is CORRECT.

    A filtered view that does not say what it hid is a lie by omission: nothing is fabricated, the reader
    is simply never told they are looking at a SUBSET. This repo has already shipped that bug twice (a
    summary that quietly dropped its caveats, a `gh pr list` that silently capped at 30). So the notice is
    pinned on all three counts: it APPEARS whenever a row is dropped, it is ABSENT when none is, and the
    number in it is the number actually dropped — a notice with a wrong count is a new lie, not a fix.
    """
    for merged in range(0, 4):
        for live in (0, 2):
            path = write_lines(
                tmp / f"n{merged}-{live}.jsonl", header_line(),
                *(row_line(pr=str(i), status="merged") for i in range(1, merged + 1)),
                *(row_line(pr=str(100 + i), status="in_review") for i in range(live)),
            )
            code, out, err = run(["--file", str(path), "table"])
            check(code == 0, f"table exited {code}: {err!r}")
            _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
            check(len(cells) == live, f"[{merged}/{live}] {len(cells)} rows shown, not {live}\n{out}")

            said = [n for n in notices(out)
                    if n not in (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER)]  # the DISCLOSURE line only
            if not merged:
                check(said == [], f"[{merged}/{live}] nothing was hidden, yet the table says {said!r}\n{out}")
                continue
            check(said == [hidden_notice(merged)],
                  f"[{merged}/{live}] the table hid {merged} row(s) and reported {said!r} — the omission "
                  f"must be stated, and stated CORRECTLY\n{out}")
            check(str(merged) in said[0] and "--all" in said[0],
                  f"[{merged}/{live}] the notice names neither the count nor the flag: {said[0]!r}")
            # …and the count is the number of rows `--all` reveals that the default did not. Derived from
            # the OUTPUT, not from the fixture's own arithmetic — otherwise it only checks itself.
            code, allout, _ = run(["--file", str(path), "table", "--all"])
            _, _, allcells = grid(allout, TABLE_DEFAULT_FIELDS)
            check(len(allcells) - len(cells) == merged,
                  f"[{merged}/{live}] the notice claims {merged} hidden, but --all reveals "
                  f"{len(allcells) - len(cells)} more rows")


def t_table_all_merged(tmp: Path) -> None:
    """EVERY row merged is a REAL end-of-run state — and it must NEVER read as an empty ledger.

    The default view shows no rows here, exactly as it does for a ledger that adopted nothing. Those are
    OPPOSITE facts — "nothing was ever adopted" vs "everything finished" — and printing `# (no rows)` for
    both would tell the reader at the end of a successful run that their campaign did nothing at all.
    So the two cases print DIFFERENT markers, and the all-hidden one also carries the count.
    """
    done = write_lines(tmp / "done.jsonl", header_line(run_id="r1"),
                       row_line(pr="1", status="merged"), row_line(pr="2", status="merged"))
    empty = write_lines(tmp / "none.jsonl", header_line(run_id="r1"))

    code, out, err = run(["--file", str(done), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(cells == [], f"an all-merged ledger showed rows by default: {cells!r}\n{out}")
    check(notices(out) == [TABLE_ALL_HIDDEN_MARKER, hidden_notice(2)],
          f"an all-merged ledger must say the ledger is NOT empty, and how many rows it hid: "
          f"{notices(out)!r}\n{out}")
    check(TABLE_EMPTY_MARKER not in out.split("\n"),
          f"an all-merged ledger printed the EMPTY-LEDGER marker — it reads as 'no PRs at all':\n{out}")

    code, blank, err = run(["--file", str(empty), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    check(notices(blank) == [TABLE_EMPTY_MARKER],
          f"a genuinely empty ledger must say exactly {TABLE_EMPTY_MARKER!r}: {notices(blank)!r}\n{blank}")
    check(out != blank,
          f"an all-merged ledger renders EXACTLY what an EMPTY ledger renders — the two are "
          f"indistinguishable:\n{out}")

    # …and `--all` on the all-merged ledger brings every row back, with nothing left to disclose.
    code, out, err = run(["--file", str(done), "table", "--all"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(out, TABLE_DEFAULT_FIELDS)
    check(len(cells) == 2, f"--all did not reveal the hidden rows: {cells!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, yet the table claims it did: {notices(out)!r}")


def t_table_aborted_is_visible(tmp: Path) -> None:
    """`aborted` STAYS VISIBLE by default — the design call, pinned.

    It is terminal like `merged`, so a rule that hid "terminal" rows would drop it. It must not: an
    aborted PR is the run's UNFINISHED BUSINESS — left open for its owner, with an `abort-<id>.md` a human
    is meant to read (`bailout-and-final-report.md`). Hiding the one row that still wants attention is the
    exact failure a status view exists to prevent. Every non-`merged` status shows; only `merged` hides.
    """
    statuses = ("in_review", "aborted", "awaiting-api", "awaiting-user", "pending", "merged")
    path = write_lines(tmp / "st.jsonl", header_line(),
                       *(row_line(pr=str(i + 1), status=s) for i, s in enumerate(statuses)))
    code, out, err = run(["--file", str(path), "table", "--fields", "pr,status"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, ("pr", "status"))
    shown = [c[1] for c in cells]
    check(shown == [s for s in statuses if s != "merged"],
          f"the default view hid something other than `merged` — it shows {shown!r}\n{out}")
    check("aborted" in shown, "an ABORTED row was hidden — the run's unfinished business is invisible")
    check(notices(out) == [hidden_notice(1)], f"exactly one merged row should be hidden: {notices(out)!r}")


def t_table_all_composes_with_fields(tmp: Path) -> None:
    """`--all` picks the ROWS, `--fields` picks the COLUMNS, and neither reads the other."""
    path = write_lines(tmp / "cmp.jsonl", header_line(),
                       row_line(pr="1", slug="done", status="merged"),
                       row_line(pr="2", slug="live", status="in_review"))
    fields = ("slug", "status")
    code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(out, fields)
    check(cells == [["live", "in_review"]], f"--fields did not hide the merged row: {cells!r}\n{out}")
    check(notices(out) == [hidden_notice(1)],
          f"--fields dropped the hidden-count notice: {notices(out)!r}\n{out}")

    code, out, err = run(["--file", str(path), "table", "--all", "--fields", ",".join(fields)])
    check(code == 0, f"table --all --fields exited {code}: {err!r}")
    _, _, cells = grid(out, fields)
    check(cells == [["done", "merged"], ["live", "in_review"]],
          f"--all --fields did not show every row in the chosen columns: {cells!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, yet the table claims it did: {notices(out)!r}")

    # …and a hidden row is still only HIDDEN, never gone: `get` reads it by field name, as always.
    code, got, _ = run(["--file", str(path), "get", "--pr", "1", "--field", "slug"])
    check((code, got) == (0, "done\n"), f"a row the table hid is unreadable through `get`: {got!r}")


def t_hidden_row_cannot_reach_the_output(tmp: Path) -> None:
    """A HOSTILE VALUE IN A HIDDEN ROW CANNOT TOUCH THE VISIBLE TABLE — not one byte of it.

    A row that is filtered out must be filtered out of the RENDERING, not merely out of the printed lines.
    The subtle leak is the COLUMN WIDTHS: measure them over every row and a merged PR with a 40-char slug
    silently widens a table it does not appear in — a value nobody printed, changing what the reader sees.
    The blunt one is worse: a merged row carrying a `|`, a newline or a leading `#` could forge a column,
    a row or a config line from behind the filter, where no one is even looking for it.

    The oracle is EQUALITY AGAINST THE SAME LEDGER WITHOUT THOSE ROWS: every byte of the table — widths,
    separators, config block, rows — must be identical, and the ONLY difference the hidden rows are allowed
    to make anywhere in the output is the hidden-count line that discloses them.
    """
    live = (row_line(pr="1", slug="live-one", ci="green"), row_line(pr="2", slug="x"))
    clean = write_lines(tmp / "clean.jsonl", header_line(), *live)
    poisoned = write_lines(
        tmp / "poisoned.jsonl", header_line(), *live,
        *(row_line(pr=str(100 + i), slug=v, branch=v, ci=v, head_sha=v, status="merged")
          for i, v in enumerate(sorted(set(HOSTILE.values())))),
    )
    n = len(set(HOSTILE.values()))
    for fields in (None, "slug", "pr,slug,ci,head_sha"):
        argv = ["table"] + (["--fields", fields] if fields else [])
        code, want, err = run(["--file", str(clean), *argv])
        check(code == 0, f"[{fields}] table exited {code}: {err!r}")
        code, got, err = run(["--file", str(poisoned), *argv])
        check(code == 0, f"[{fields}] table exited {code}: {err!r}")
        check(notices(got) == [hidden_notice(n)],
              f"[{fields}] the hidden hostile rows were not disclosed: {notices(got)!r}")
        # strip ONLY the disclosure line; everything else must be byte-identical to the clean ledger
        stripped = "".join(l + "\n" for l in got.split("\n")[:-1] if l != hidden_notice(n))
        check(stripped == want,
              f"[{fields}] a HIDDEN row changed the VISIBLE output — it reached the widths or the grid.\n"
              f"--- with hidden rows ---\n{stripped}--- without them ---\n{want}")

    # …and --all still renders every one of them safely: the filter is not what makes them harmless.
    code, out, err = run(["--file", str(poisoned), "table", "--all", "--fields", "slug,pr"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(out, ("slug", "pr"))
    check(len(cells) == n + 2, f"--all did not print every row exactly once: {len(cells)} of {n + 2}\n{out}")


def t_out_of_band_lines_not_forgeable(tmp: Path) -> None:
    """No ROW can forge the ALL-HIDDEN marker or the HIDDEN-COUNT notice.

    They are the empty-marker problem again, and they get the same answer: both are printed WHERE A ROW
    WOULD GO, so position cannot distinguish them from a value — only the `#` namespace can, and
    `escape_cell()` is what keeps values out of it. A forged `# 0 merged rows hidden` would be the worst
    of the three: it does not merely misreport the ledger, it tells the reader THE VIEW IS COMPLETE while
    rows sit hidden behind it — un-disclosing the very omission the notice exists to disclose.

    Pins the `#` namespace for BOTH new lines: drop the leading-`#` escape and every case below forges one.
    """
    # a ledger whose default view IS all-hidden, and one that hides a row and says so — the two outputs a
    # forgery would have to imitate.
    all_hidden = write_lines(tmp / "ah.jsonl", header_line(run_id="r1"), row_line(pr="9", status="merged"))
    for name, forgery in (
        ("all-hidden-marker", TABLE_ALL_HIDDEN_MARKER),
        ("notice", hidden_notice(1)),
        ("notice-zero", "# 0 merged rows hidden — pass --all to show every row"),
        ("notice-padded", hidden_notice(1) + "  "),
    ):
        for field in ("slug", "branch", "pr"):
            path = write_lines(tmp / f"{name}-{field}.jsonl", header_line(run_id="r1"),
                               row_line(**{"pr": "1", field: forgery}))
            code, out, err = run(["--file", str(path), "table", "--fields", field])
            check(code == 0, f"[{name}/{field}] table exited {code}: {err!r}")
            # the row is VISIBLE and nothing is hidden — so the table must disclose NOTHING…
            _, _, cells = grid(out, (field,))
            check(cells == [[escape_cell(forgery)]],
                  f"[{name}/{field}] the printed row is not the escaped row: {cells!r}\n{out}")
            check(notices(out) == [],
                  f"[{name}/{field}] a ROW forged an out-of-band line: {notices(out)!r}\n{out}")
            # …and no LINE of it IS one of the out-of-band lines (the escaped cell may CONTAIN the text —
            # `\# 1 merged row hidden…` does — but the `\` in front is exactly what the namespace buys).
            body = out.split("\n\n", 1)[1].split("\n")
            for line in (TABLE_ALL_HIDDEN_MARKER, hidden_notice(1), TABLE_EMPTY_MARKER):
                check(line not in body,
                      f"[{name}/{field}] a line of a table with a VISIBLE row IS {line!r}:\n{out}")

            code, blank, _ = run(["--file", str(all_hidden), "table", "--fields", field])
            check(code == 0, "table on an all-hidden ledger must succeed")
            check(out != blank,
                  f"[{name}/{field}] a VISIBLE row holding {forgery!r} renders EXACTLY what an ALL-HIDDEN "
                  f"ledger renders — the marker is forgeable:\n{out}")


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
    check(cells[0] == ["1", "1"], f"a duplicated field did not print twice: {cells!r}")


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
    ("render-injective", "the PRINTED ROWS are injective too — no two values print the same line", t_render_injective),
    ("escape-invariant", "the escaped cell holds no bare |, newline, control char, or leading #", t_escape_invariant),
    ("escape-mapping", "the escape table, char by char — and ordinary values left byte-identical", t_escape_mapping),
    ("grid-integrity", "no hostile value forges a column, a row, or a config line", t_grid_integrity),
    ("widths-from-escaped", "column widths measure the ESCAPED text — what is printed", t_widths_from_escaped),
    ("config-not-forgeable", "a hostile header value cannot inject a `# <field>:` line", t_config_cannot_be_forged),
    ("truncation-display-only", "table truncates head_sha; disk and `get` keep all 40 — and the cut precedes the escape", t_truncation_is_display_only),
    ("table-missing-file", "a missing ledger is a fresh start: defaults, `# (no rows)`", t_table_missing_file),
    ("table-no-rows", "a header-only ledger says `# (no rows)`", t_table_no_rows),
    ("empty-marker-safe", "no ROW can forge the empty-ledger marker — it lives where no cell can reach", t_empty_marker_not_forgeable),
    ("table-hides-merged", "the default view hides merged rows; --all shows every row", t_table_hides_merged),
    ("table-hidden-count", "the omission is NEVER silent — the hidden count is stated, and it is correct", t_table_hidden_count),
    ("table-all-merged", "an all-merged ledger never reads as an EMPTY one — different marker, plus the count", t_table_all_merged),
    ("table-aborted-visible", "aborted is terminal but STAYS VISIBLE — only `merged` hides", t_table_aborted_is_visible),
    ("table-all-and-fields", "--all picks the rows, --fields the columns — they compose", t_table_all_composes_with_fields),
    ("hidden-row-inert", "a hostile value in a HIDDEN row cannot change one byte of the visible table", t_hidden_row_cannot_reach_the_output),
    ("out-of-band-safe", "no ROW can forge the all-hidden marker or the hidden-count notice", t_out_of_band_lines_not_forgeable),
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

    t = sub.add_parser("table", help="print the run header and the live rows as an aligned table")
    t.add_argument("--fields", help=f"comma-separated row fields to show (default: {','.join(TABLE_DEFAULT_FIELDS)})")
    # The default hides rows; --all is how a reader gets the whole ledger back. The help text is derived
    # from TABLE_HIDDEN_STATUSES for the same reason hidden_notice() is: so it cannot drift from it.
    t.add_argument("--all", dest="show_all", action="store_true",
                   help=f"show every row (the default hides status={'/'.join(TABLE_HIDDEN_STATUSES)} "
                        f"and reports how many it hid)")

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
