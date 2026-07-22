#!/usr/bin/env python3
"""THE EXECUTABLE CONTRACT FOR `ledger.py` — every rule the accessor claims, pinned by a fixture.

Run it through the tool it tests (this is what CI runs):

    python3 ledger.py self-test

or directly, which does the same thing:

    python3 ledger-test.py

**THE SUITE IS A SIBLING, NOT A SECTION.** It used to live inside `ledger.py`, which is how a self-test
comes to be judged by the file it is judging. `ledger.py self-test` loads this file by a `__file__`-relative
path and **FAILS LOUDLY IF IT IS NOT THERE**: a self-test that passes because it found no tests is the
loudest possible false green, and it is a bug this repo has already shipped.

**THE ACCESSOR UNDER TEST IS HANDED IN, NEVER RE-IMPORTED** (`run(L, tmp)`). Every helper and every fixture
takes it as `L`, so the code these fixtures drive is the code the `self-test` command actually loaded —
not a second copy of the same file that happens to agree with it.

What the fixtures are aimed at, in two families:

  * **`escape_cell` and the grid** — the SECURITY-SHAPED half. A field value is free text (`slug` is a PR
    title), and `table` prints it into a layout built from `|`, newlines and a leading `#`. Rendered raw, a
    value forges a column, a row, a run-config line, or one of the out-of-band markers — and the table then
    says something the ledger never did. A sanitizer verified by eye is verified by nothing, so the grid is
    re-parsed MECHANICALLY (`grid()`) and the escaping is asserted INJECTIVE: two values that differ can
    never print as one cell.
  * **the TALLY and the COUNTERS** — the GATE-SHAPED half, and the newer one. `reviews_ok` decides whether a
    PR may merge; `review_rounds` is the only thing that can see a review loop that is not converging. The
    fixtures here pin the two rules that make those trustworthy: **only `verdict` may RAISE the tally**, and
    **nothing at all may reset `review_rounds`** — there is no door that writes it but the one that
    increments it, and a fixture stands on that.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from _gauntlet.testing import capture_cli

HERE = Path(__file__).resolve().parent
LEDGER_PY = HERE / "ledger.py"


def load_ledger() -> ModuleType:
    """Load `ledger.py` — used ONLY when this file is run directly.

    Driven through `ledger.py self-test`, the module is handed to `run()` instead, so the accessor under
    test is loaded exactly once and the fixtures drive the very module that command is running.
    """
    spec = importlib.util.spec_from_file_location("ledger", LEDGER_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        raise SystemExit(f"ledger-test: cannot load {LEDGER_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The adversarial values. Every one of them is a value the ledger can genuinely hold (`slug` is a PR
# title; a branch name is free text), and every one is aimed at the grid's own syntax: the column
# separator, the row separator, the run-config prefix — or at the ESCAPES THEMSELVES, which is the
# attack the naive sanitizer misses (escape `|` but not `\`, and `a\|b` and `a|b` collide).
HOSTILE_BASE = {
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

HOSTILE_BASE.update(WHITESPACE)


def hostile(L: ModuleType) -> "dict[str, str]":
    """The hostile values, plus the one that is the ACCESSOR'S OWN marker text.

    `TABLE_ALL_HIDDEN_MARKER` is READ from the accessor rather than retyped here: a fixture that forges a
    marker must forge the marker the tool actually prints, and a copy of it in this file would go stale the
    day the wording changes — leaving a forgery test that forges nothing and passes forever.
    """
    return {**HOSTILE_BASE, "all-hidden-marker": L.TABLE_ALL_HIDDEN_MARKER}


class SelfTestFailure(AssertionError):
    """A rule the accessor claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def cli(L: ModuleType, argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr).

    The subcommands are exercised through `main()` — never by calling their internals — so the argparse
    wiring (the `--fields` rejection, the dash/underscore aliases, the ABSENCE of a `--review-rounds` flag,
    `fail()`'s exit 1) is under test too.
    """
    return capture_cli(L.main, argv)


def write_lines(path: Path, *lines: str) -> Path:
    """Write a ledger RAW — bypassing dump(), so a fixture can hold what dump() would never emit."""
    path.write_text("".join(line + "\n" for line in lines))
    return path


def header_line(L: ModuleType, **over: str) -> str:
    return json.dumps({"type": "header", **L.HEADER_DEFAULTS, **over})


def row_line(L: ModuleType, **over: str) -> str:
    return json.dumps({"type": "row", **L.ROW_DEFAULTS, **over})


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


def grid(L: ModuleType, out: str, fields: "tuple[str, ...]",
         config_fields: "tuple[str, ...] | None" = None,
         markers: "tuple[str, ...] | None" = None) -> "tuple[list[str], list[int], list[list[str]]]":
    """Parse the printed table BACK and assert its INTEGRITY. Returns (config lines, widths, cells).

    `config_fields` and `markers` name the out-of-band text the store under test prints; they default to
    the LEDGER's own (`L.HEADER_FIELDS`, and its empty/all-hidden markers). The sibling store
    (`followups.py`) renders through the same `config_lines()`/`grid_lines()` and asserts its output with
    THIS same oracle, passing ITS two — so the layout is verified by ONE parser and a second store cannot
    be checked by a weaker copy of it.

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
    cfg_fields: "tuple[str, ...]" = L.HEADER_FIELDS if config_fields is None else config_fields
    mk: "tuple[str, ...]" = (L.TABLE_EMPTY_MARKER, L.TABLE_ALL_HIDDEN_MARKER) if markers is None else markers
    lines = out.split("\n")
    check(lines[-1] == "", "the table output must end in a newline")
    lines = lines[:-1]
    n = len(cfg_fields)
    check(len(lines) >= n + 3, f"expected {n} run-config lines, a blank line and a grid; got {lines!r}")
    config, lines = lines[:n], lines[n:]
    for f, line in zip(cfg_fields, config):
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
        check(line in mk
              or line.startswith("# ") and "hidden" in line,
              f"an unrecognised out-of-band line below the grid: {line!r}")
    # An empty grid must SAY which empty it is — never nothing, and never the wrong one.
    if not rows:
        check(bool(out_of_band) and out_of_band[0] in mk,
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

    def cut(line: str) -> "list[str]":
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
#
# Every fixture takes the ACCESSOR under test and its own scratch directory, handed to it by `run()`. A
# fixture that asserts on a PURE function needs no file, so it names that argument `_`: the signature is
# fixed by the CASES table (`run` calls `fn(L, work)` positionally, so the name is free), and a bare `_` is
# how a parameter says it is deliberately unused rather than forgotten. It must be a BARE `_`, not a
# descriptive `_tmp`: pyright's language server grays a named-underscore parameter as "not accessed", and
# only a bare `_` is silent.

def t_escape_injective(L: ModuleType, _: Path) -> None:
    """Two DIFFERENT values NEVER render as the same cell.

    A non-injective escaping is a NEW LIE inside the code written to stop lies: `a|b` and `a\\|b` are
    different values, and if both print as `a\\|b` the table has silently merged them. Doubling the
    BACKSLASH is the whole reason this holds — remove that one branch and the two collide immediately.
    """
    values = sorted(set(
        list(hostile(L).values())
        + [chr(c) for c in range(0x20)] + ["\x7f", "|", "#", "\\", "\\\\", "\\|", "\\n", "\\r", "\\t"]
        + ["\\x00", "\\x1b", "\\#", "-", "a", "pr1"]
    ))
    seen: dict[str, str] = {}
    for raw in values:
        cell = L.escape_cell(raw)
        if cell in seen:
            raise SelfTestFailure(
                f"escape_cell is NOT INJECTIVE: {seen[cell]!r} and {raw!r} both render as {cell!r} — "
                f"the table cannot be telling the truth about both"
            )
        seen[cell] = raw


def t_render_injective(L: ModuleType, tmp: Path) -> None:
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
    values = sorted(set(hostile(L).values()))
    path = tmp / "inj.jsonl"
    write_lines(path, header_line(L),
                *(row_line(L, pr=str(i + 1), slug=v, ci="green") for i, v in enumerate(values)))
    for fields in (("slug",), ("slug", "ci"), ("ci", "slug")):
        code, out, err = cli(L, ["--file", str(path), "table", "--fields", ",".join(fields)])
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
        _, _, cells = grid(L, out, fields)
        for raw, row in zip(values, cells):
            check(row[fields.index("slug")] == L.escape_cell(raw),
                  f"{fields}: {raw!r} printed as {row[fields.index('slug')]!r}, not {L.escape_cell(raw)!r}")


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


def t_escape_invariant(L: ModuleType, _: Path) -> None:
    """The escaped cell holds NO BARE grid metacharacter: no unescaped `|`, no line break, no control
    char — and it never opens with `#`. This is what makes the ` | ` separator and the `# ` config prefix
    mean ONE thing wherever they appear.

    …and NO EDGE WHITESPACE, which is the layout's metacharacter rather than the syntax's: `ljust()` and
    `rstrip()` eat it, so a cell that ends in whitespace is a cell whose printed bytes do not say what it
    holds. That guarantee is also what lets `grid()` recover a cell by removing its padding — every
    trailing space in a printed column IS padding, because content can no longer end in one.
    """
    for raw in sorted(set(list(hostile(L).values()) + [chr(c) for c in range(0x21)]
                          + ["\x7f", "\x85", "\xa0", " ", "　", " a ", "  ", "a\t"])):
        cell = L.escape_cell(raw)
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


def t_escape_mapping(L: ModuleType, _: Path) -> None:
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
        check(L.escape_cell(raw) == want, f"escape_cell({raw!r}) = {L.escape_cell(raw)!r}, not {want!r}")

    check(L.escape_cell("#x") == "\\#x", f"a LEADING '#' must be escaped: {L.escape_cell('#x')!r}")
    check(L.escape_cell("a#b") == "a#b", f"a '#' that is not leading needs no escape: {L.escape_cell('a#b')!r}")

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
        check(L.escape_cell(raw) == want, f"escape_cell({raw!r}) = {L.escape_cell(raw)!r}, not {want!r}")

    # …and the whole reason this is escaping and not quoting: an ordinary value is untouched.
    for plain in ("pr42", "fix/the-thing", "green", "-", "add a table view (#39)", "2026-07-13T10:00:00Z"):
        check(L.escape_cell(plain) == plain,
              f"an ordinary value was mangled: {plain!r} -> {L.escape_cell(plain)!r}")


def t_grid_integrity(L: ModuleType, tmp: Path) -> None:
    """EVERY hostile value, in EVERY column, and the grid still parses back to the declared shape.

    Checked MECHANICALLY (`grid()` re-parses the output). The `slug` column carries the hostile value
    while `pr` sits FIRST — where a leading `#` would open a line — and a second row holds a benign
    value, so a forged row/column has somewhere to be seen.
    """
    fields = ("slug", "pr", "ci")
    for name, value in hostile(L).items():
        path = tmp / f"grid-{name}.jsonl"
        write_lines(path, header_line(L), row_line(L, pr="1", slug=value, ci="green"), row_line(L, pr="2"))
        code, out, err = cli(L, ["--file", str(path), "table", "--fields", ",".join(fields)])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        _, _, cells = grid(L, out, fields)
        check(len(cells) == 2, f"[{name}] the value forged a ROW: {len(cells)} rows printed, not 2\n{out}")
        check(cells[0] == [L.escape_cell(v) for v in (value, "1", "green")],
              f"[{name}] the printed row is not the escaped row: {cells[0]!r}\n{out}")

    # …and again with the hostile value FIRST, which is the only position that can open a line — the
    # leading-'#' rule is pinned HERE (`grid()` rejects a body line starting with '#').
    for name, value in hostile(L).items():
        path = tmp / f"first-{name}.jsonl"
        write_lines(path, header_line(L), row_line(L, pr="1", slug=value))
        code, out, err = cli(L, ["--file", str(path), "table", "--fields", "slug,pr"])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        grid(L, out, ("slug", "pr"))

    # …and once more in a ONE-COLUMN table — the narrowest grid there is, where the cell has the whole
    # line to ITSELF and every other line of the table is something it could try to be. This is the shape
    # the empty-marker forgery lived in, so every hostile value is run through it: the grid must still
    # parse back to EXACTLY ONE row, never to an empty ledger and never to a fabricated second one.
    for name, value in hostile(L).items():
        path = tmp / f"one-{name}.jsonl"
        write_lines(path, header_line(L), row_line(L, pr="1", slug=value))
        code, out, err = cli(L, ["--file", str(path), "table", "--fields", "slug"])
        check(code == 0, f"[{name}] table exited {code}: {err!r}")
        _, _, cells = grid(L, out, ("slug",))
        check(cells == [[L.escape_cell(value)]],
              f"[{name}] a one-column grid did not print exactly the one escaped row: {cells!r}\n{out}")


def t_widths_from_escaped(L: ModuleType, tmp: Path) -> None:
    """Column widths are computed from the ESCAPED text — i.e. from what is actually PRINTED.

    Measure the RAW value and every escape makes its cell WIDER than the column reserved for it, so the
    ` | ` separators stop lining up and the grid is ragged from the first hostile value on.
    """
    raw = "a|b|c|d"                 # 7 raw, 11 escaped: `a\|b\|c\|d`… wider than the field name
    escaped = L.escape_cell(raw)
    check(len(escaped) > len(raw) > len("slug"), "fixture is not measuring anything")
    path = write_lines(tmp / "w.jsonl", header_line(L), row_line(L, pr="1", slug=raw))
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", "slug,pr"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, widths, _ = grid(L, out, ("slug", "pr"))
    check(widths[0] == len(escaped),
          f"the slug column is {widths[0]} wide but prints {len(escaped)} chars ({escaped!r}) — the width "
          f"was measured on the RAW value ({len(raw)}), not the printed one")


def t_config_cannot_be_forged(L: ModuleType, tmp: Path) -> None:
    """A hostile header value cannot inject a line that READS AS a run-config line.

    `base_branch` and `run_id` are free text too, and they are printed ABOVE the grid as `# <field>: …`.
    An unescaped newline in one of them writes any line it likes into that block.
    """
    path = write_lines(
        tmp / "cfg.jsonl",
        header_line(L, run_id="real", base_branch="main\n# run_id: forged\n\npr | slug"),
        row_line(L, pr="1"),
    )
    code, out, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    config, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check(len(cells) == 1, f"the header value forged a ROW: {out}")
    forged = [line for line in out.split("\n") if line.startswith("# run_id:")]
    check(forged == ["# run_id: real"],
          f"a hostile base_branch forged a run-config line — lines opening '# run_id:': {forged!r}\n{out}")
    check(config[0] == "# run_id: real", f"the real run_id line is {config[0]!r}")


def t_truncation_is_display_only(L: ModuleType, tmp: Path) -> None:
    """`table` shortens head_sha FOR DISPLAY. The ledger still holds all 40 chars — and `get` still says so.

    A projection that quietly became the source of truth would hand every caller an 8-char prefix, and a
    prefix is not a commit. Also pinned here: truncation happens BEFORE escaping, so a cut can never land
    INSIDE an escape sequence and leave a dangling backslash behind.
    """
    sha = "abcdefg|" + "9" * 32  # the 8-char cut lands EXACTLY on a character that must be escaped
    check(len(sha) == 40, "fixture sha is not 40 chars")
    path = write_lines(tmp / "sha.jsonl", header_line(L), row_line(L, pr="1", head_sha=sha))

    code, out, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    col = L.TABLE_DEFAULT_FIELDS.index("head_sha")
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
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "head_sha"])
    check((code, out) == (0, sha + "\n"), f"get --field head_sha returned {out!r}, not the full 40 chars")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1"])
    check(json.loads(out)["head_sha"] == sha, f"get returned a truncated head_sha: {out!r}")
    check(sha in path.read_text(), "the ON-DISK row does not carry the full head_sha")


def t_table_missing_file(L: ModuleType, tmp: Path) -> None:
    """A ledger that does not exist yet is a FRESH START, not an error: defaults, and no rows."""
    code, out, err = cli(L, ["--file", str(tmp / "nope.jsonl"), "table"])
    check(code == 0, f"table on a missing file exited {code}: {err!r}")
    config, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check(cells == [], f"a missing file produced rows: {cells!r}")
    check(out.rstrip().endswith(L.TABLE_EMPTY_MARKER),
          f"a missing file must still say {L.TABLE_EMPTY_MARKER!r}: {out!r}")
    check(config[0] == "# run_id: -", f"a missing file must fall back to the defaults; got {config[0]!r}")


def t_table_no_rows(L: ModuleType, tmp: Path) -> None:
    """A header-only ledger prints the grid and says so — never an empty, wordless table."""
    path = write_lines(tmp / "hdr.jsonl", header_line(L, run_id="r1"))
    code, out, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check(cells == [], f"a header-only ledger produced rows: {cells!r}")
    check(out.rstrip().endswith(L.TABLE_EMPTY_MARKER),
          f"a header-only ledger must say {L.TABLE_EMPTY_MARKER!r}: {out!r}")


def t_empty_marker_not_forgeable(L: ModuleType, tmp: Path) -> None:
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
    empty = write_lines(tmp / "empty.jsonl", header_line(L, run_id="r1"))
    for name, forgery in (
        ("old-marker", "(no rows)"),
        ("marker", L.TABLE_EMPTY_MARKER),
        ("marker-no-space", L.TABLE_EMPTY_MARKER.replace("# ", "#")),
        ("marker-padded", L.TABLE_EMPTY_MARKER + "   "),
    ):
        for field in ("reviews_ok", "slug", "pr"):
            path = write_lines(tmp / f"{name}-{field}.jsonl", header_line(L, run_id="r1"),
                               row_line(L, **{"pr": "1", field: forgery}))
            code, out, err = cli(L, ["--file", str(path), "table", "--fields", field])
            check(code == 0, f"[{name}/{field}] table exited {code}: {err!r}")

            code, blank, _ = cli(L, ["--file", str(empty), "table", "--fields", field])
            check(code == 0, "table on a header-only ledger must succeed")
            check(out != blank,
                  f"[{name}/{field}] a row holding {forgery!r} renders EXACTLY what an EMPTY ledger "
                  f"renders — the marker is forgeable:\n{out}")

            # …and mechanically: the grid must parse back to ONE row, not to an empty ledger.
            _, _, cells = grid(L, out, (field,))
            check(len(cells) == 1,
                  f"[{name}/{field}] a row holding {forgery!r} parsed back as an EMPTY ledger "
                  f"({len(cells)} rows) — the marker is forgeable:\n{out}")
            check(cells[0] == [L.escape_cell(forgery)],
                  f"[{name}/{field}] the printed row is not the escaped row: {cells[0]!r}\n{out}")
            # …and no LINE of a non-empty table IS the marker. (The escaped cell may well CONTAIN the
            # marker's text — `\# (no rows)` does — but it can never BE that line: the `\` is in front of
            # it, which is the whole point of the namespace.)
            body = out.split("\n\n", 1)[1].split("\n")
            check(L.TABLE_EMPTY_MARKER not in body,
                  f"[{name}/{field}] a line of a NON-EMPTY table is the empty marker:\n{out}")


def t_table_hides_merged(L: ModuleType, tmp: Path) -> None:
    """The DEFAULT view drops `merged` rows and shows everything else; `--all` shows the whole ledger.

    This is the projection's ROW rule, and it is the mirror of the FIELD rule: a missing row is not a
    missing PR. Both are pinned here — the default really does hide, and `--all` really does reveal.
    """
    path = write_lines(
        tmp / "mix.jsonl", header_line(L),
        row_line(L, pr="1", status="merged"),
        row_line(L, pr="2", status="in_review"),
        row_line(L, pr="3", status="merged"),
        row_line(L, pr="4", status="awaiting-user"),
    )
    code, out, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    col = L.TABLE_DEFAULT_FIELDS.index("pr")
    check([c[col] for c in cells] == ["2", "4"],
          f"the default view did not hide exactly the merged rows: {[c[col] for c in cells]!r}\n{out}")

    code, out, err = cli(L, ["--file", str(path), "table", "--all"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check([c[col] for c in cells] == ["1", "2", "3", "4"],
          f"--all did not show every row: {[c[col] for c in cells]!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, so it must claim nothing was hidden: {notices(out)!r}")


def t_table_hidden_count(L: ModuleType, tmp: Path) -> None:
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
                tmp / f"n{merged}-{live}.jsonl", header_line(L),
                *(row_line(L, pr=str(i), status="merged") for i in range(1, merged + 1)),
                *(row_line(L, pr=str(100 + i), status="in_review") for i in range(live)),
            )
            code, out, err = cli(L, ["--file", str(path), "table"])
            check(code == 0, f"table exited {code}: {err!r}")
            _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
            check(len(cells) == live, f"[{merged}/{live}] {len(cells)} rows shown, not {live}\n{out}")

            said = [n for n in notices(out)
                    if n not in (L.TABLE_EMPTY_MARKER, L.TABLE_ALL_HIDDEN_MARKER)]  # the DISCLOSURE line only
            if not merged:
                check(said == [], f"[{merged}/{live}] nothing was hidden, yet the table says {said!r}\n{out}")
                continue
            check(said == [L.hidden_notice(merged)],
                  f"[{merged}/{live}] the table hid {merged} row(s) and reported {said!r} — the omission "
                  f"must be stated, and stated CORRECTLY\n{out}")
            check(str(merged) in said[0] and "--all" in said[0],
                  f"[{merged}/{live}] the notice names neither the count nor the flag: {said[0]!r}")
            # …and the count is the number of rows `--all` reveals that the default did not. Derived from
            # the OUTPUT, not from the fixture's own arithmetic — otherwise it only checks itself.
            code, allout, _ = cli(L, ["--file", str(path), "table", "--all"])
            _, _, allcells = grid(L, allout, L.TABLE_DEFAULT_FIELDS)
            check(len(allcells) - len(cells) == merged,
                  f"[{merged}/{live}] the notice claims {merged} hidden, but --all reveals "
                  f"{len(allcells) - len(cells)} more rows")


def t_table_all_merged(L: ModuleType, tmp: Path) -> None:
    """EVERY row merged is a REAL end-of-run state — and it must NEVER read as an empty ledger.

    The default view shows no rows here, exactly as it does for a ledger that adopted nothing. Those are
    OPPOSITE facts — "nothing was ever adopted" vs "everything finished" — and printing `# (no rows)` for
    both would tell the reader at the end of a successful run that their campaign did nothing at all.
    So the two cases print DIFFERENT markers, and the all-hidden one also carries the count.
    """
    done = write_lines(tmp / "done.jsonl", header_line(L, run_id="r1"),
                       row_line(L, pr="1", status="merged"), row_line(L, pr="2", status="merged"))
    empty = write_lines(tmp / "none.jsonl", header_line(L, run_id="r1"))

    code, out, err = cli(L, ["--file", str(done), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check(cells == [], f"an all-merged ledger showed rows by default: {cells!r}\n{out}")
    check(notices(out) == [L.TABLE_ALL_HIDDEN_MARKER, L.hidden_notice(2)],
          f"an all-merged ledger must say the ledger is NOT empty, and how many rows it hid: "
          f"{notices(out)!r}\n{out}")
    check(L.TABLE_EMPTY_MARKER not in out.split("\n"),
          f"an all-merged ledger printed the EMPTY-LEDGER marker — it reads as 'no PRs at all':\n{out}")

    code, blank, err = cli(L, ["--file", str(empty), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    check(notices(blank) == [L.TABLE_EMPTY_MARKER],
          f"a genuinely empty ledger must say exactly {L.TABLE_EMPTY_MARKER!r}: {notices(blank)!r}\n{blank}")
    check(out != blank,
          f"an all-merged ledger renders EXACTLY what an EMPTY ledger renders — the two are "
          f"indistinguishable:\n{out}")

    # …and `--all` on the all-merged ledger brings every row back, with nothing left to disclose.
    code, out, err = cli(L, ["--file", str(done), "table", "--all"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check(len(cells) == 2, f"--all did not reveal the hidden rows: {cells!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, yet the table claims it did: {notices(out)!r}")


def t_table_aborted_is_visible(L: ModuleType, tmp: Path) -> None:
    """`aborted` STAYS VISIBLE by default — the design call, pinned.

    It is terminal like `merged`, so a rule that hid "terminal" rows would drop it. It must not: an
    aborted PR is the run's UNFINISHED BUSINESS — left open for its owner, with an `abort-<id>.md` a human
    is meant to read (`bailout-and-final-report.md`). Hiding the one row that still wants attention is the
    exact failure a status view exists to prevent. Every non-`merged` status shows; only `merged` hides.
    """
    statuses = ("in_review", "aborted", "awaiting-api", "awaiting-user", "pending", "merged")
    path = write_lines(tmp / "st.jsonl", header_line(L),
                       *(row_line(L, pr=str(i + 1), status=s) for i, s in enumerate(statuses)))
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", "pr,status"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, ("pr", "status"))
    shown = [c[1] for c in cells]
    check(shown == [s for s in statuses if s != "merged"],
          f"the default view hid something other than `merged` — it shows {shown!r}\n{out}")
    check("aborted" in shown, "an ABORTED row was hidden — the run's unfinished business is invisible")
    check(notices(out) == [L.hidden_notice(1)], f"exactly one merged row should be hidden: {notices(out)!r}")


def t_table_all_composes_with_fields(L: ModuleType, tmp: Path) -> None:
    """`--all` picks the ROWS, `--fields` picks the COLUMNS, and neither reads the other."""
    path = write_lines(tmp / "cmp.jsonl", header_line(L),
                       row_line(L, pr="1", slug="done", status="merged"),
                       row_line(L, pr="2", slug="live", status="in_review"))
    fields = ("slug", "status")
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", ",".join(fields)])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, fields)
    check(cells == [["live", "in_review"]], f"--fields did not hide the merged row: {cells!r}\n{out}")
    check(notices(out) == [L.hidden_notice(1)],
          f"--fields dropped the hidden-count notice: {notices(out)!r}\n{out}")

    code, out, err = cli(L, ["--file", str(path), "table", "--all", "--fields", ",".join(fields)])
    check(code == 0, f"table --all --fields exited {code}: {err!r}")
    _, _, cells = grid(L, out, fields)
    check(cells == [["done", "merged"], ["live", "in_review"]],
          f"--all --fields did not show every row in the chosen columns: {cells!r}\n{out}")
    check(notices(out) == [], f"--all hid nothing, yet the table claims it did: {notices(out)!r}")

    # …and a hidden row is still only HIDDEN, never gone: `get` reads it by field name, as always.
    code, got, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "slug"])
    check((code, got) == (0, "done\n"), f"a row the table hid is unreadable through `get`: {got!r}")


def t_hidden_row_cannot_reach_the_output(L: ModuleType, tmp: Path) -> None:
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
    live = (row_line(L, pr="1", slug="live-one", ci="green"), row_line(L, pr="2", slug="x"))
    clean = write_lines(tmp / "clean.jsonl", header_line(L), *live)
    poisoned = write_lines(
        tmp / "poisoned.jsonl", header_line(L), *live,
        *(row_line(L, pr=str(100 + i), slug=v, branch=v, ci=v, head_sha=v, status="merged")
          for i, v in enumerate(sorted(set(hostile(L).values())))),
    )
    n = len(set(hostile(L).values()))
    for fields in (None, "slug", "pr,slug,ci,head_sha"):
        argv = ["table"] + (["--fields", fields] if fields else [])
        code, want, err = cli(L, ["--file", str(clean), *argv])
        check(code == 0, f"[{fields}] table exited {code}: {err!r}")
        code, got, err = cli(L, ["--file", str(poisoned), *argv])
        check(code == 0, f"[{fields}] table exited {code}: {err!r}")
        check(notices(got) == [L.hidden_notice(n)],
              f"[{fields}] the hidden hostile rows were not disclosed: {notices(got)!r}")
        # strip ONLY the disclosure line; everything else must be byte-identical to the clean ledger
        stripped = "".join(l + "\n" for l in got.split("\n")[:-1] if l != L.hidden_notice(n))
        check(stripped == want,
              f"[{fields}] a HIDDEN row changed the VISIBLE output — it reached the widths or the grid.\n"
              f"--- with hidden rows ---\n{stripped}--- without them ---\n{want}")

    # …and --all still renders every one of them safely: the filter is not what makes them harmless.
    code, out, err = cli(L, ["--file", str(poisoned), "table", "--all", "--fields", "slug,pr"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = grid(L, out, ("slug", "pr"))
    check(len(cells) == n + 2, f"--all did not print every row exactly once: {len(cells)} of {n + 2}\n{out}")


def t_out_of_band_lines_not_forgeable(L: ModuleType, tmp: Path) -> None:
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
    all_hidden = write_lines(tmp / "ah.jsonl", header_line(L, run_id="r1"),
                             row_line(L, pr="9", status="merged"))
    for name, forgery in (
        ("all-hidden-marker", L.TABLE_ALL_HIDDEN_MARKER),
        ("notice", L.hidden_notice(1)),
        ("notice-zero", "# 0 merged rows hidden — pass --all to show every row"),
        ("notice-padded", L.hidden_notice(1) + "  "),
    ):
        for field in ("slug", "branch", "pr"):
            path = write_lines(tmp / f"{name}-{field}.jsonl", header_line(L, run_id="r1"),
                               row_line(L, **{"pr": "1", field: forgery}))
            code, out, err = cli(L, ["--file", str(path), "table", "--fields", field])
            check(code == 0, f"[{name}/{field}] table exited {code}: {err!r}")
            # the row is VISIBLE and nothing is hidden — so the table must disclose NOTHING…
            _, _, cells = grid(L, out, (field,))
            check(cells == [[L.escape_cell(forgery)]],
                  f"[{name}/{field}] the printed row is not the escaped row: {cells!r}\n{out}")
            check(notices(out) == [],
                  f"[{name}/{field}] a ROW forged an out-of-band line: {notices(out)!r}\n{out}")
            # …and no LINE of it IS one of the out-of-band lines (the escaped cell may CONTAIN the text —
            # `\# 1 merged row hidden…` does — but the `\` in front is exactly what the namespace buys).
            body = out.split("\n\n", 1)[1].split("\n")
            for line in (L.TABLE_ALL_HIDDEN_MARKER, L.hidden_notice(1), L.TABLE_EMPTY_MARKER):
                check(line not in body,
                      f"[{name}/{field}] a line of a table with a VISIBLE row IS {line!r}:\n{out}")

            code, blank, _ = cli(L, ["--file", str(all_hidden), "table", "--fields", field])
            check(code == 0, "table on an all-hidden ledger must succeed")
            check(out != blank,
                  f"[{name}/{field}] a VISIBLE row holding {forgery!r} renders EXACTLY what an ALL-HIDDEN "
                  f"ledger renders — the marker is forgeable:\n{out}")


def t_fields_rejected(L: ModuleType, tmp: Path) -> None:
    """`--fields` is CHECKED, and an EMPTY `--fields` is a malformed field list — never 'omitted'.

    `args.fields is not None` is the rule: falsiness would read `--fields ''` as "the user asked for the
    defaults" and print a table they did not ask for.
    """
    path = write_lines(tmp / "f.jsonl", header_line(L), row_line(L, pr="1"))
    for fields, needle in ((("--fields", ""), "unknown field ''"),
                           (("--fields", "nope"), "unknown field 'nope'")):
        code, out, err = cli(L, ["--file", str(path), "table", *fields])
        check(code == 1, f"table {fields!r} exited {code}, not 1 — it was ACCEPTED:\n{out}")
        check(needle in err, f"table {fields!r} failed with {err!r}, which does not mention {needle!r}")


def t_fields_duplicate(L: ModuleType, tmp: Path) -> None:
    """A field named TWICE prints TWICE — and the grid still declares the columns it actually has."""
    path = write_lines(tmp / "d.jsonl", header_line(L), row_line(L, pr="1", slug="s"))
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", "pr,pr"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, ("pr", "pr"))
    check(cells[0] == ["1", "1"], f"a duplicated field did not print twice: {cells!r}")


def t_jsonl_records_are_objects(L: ModuleType, tmp: Path) -> None:
    """Malformed JSON and non-object records are rejected with the caller prefix and source line."""
    header = header_line(L)
    for name, record, needle in (
        ("malformed", "{not json", "ledger: malformed JSON on line 3"),
        ("not-object", '["row"]', "ledger: line 3: record is not a JSON object"),
    ):
        path = write_lines(tmp / f"{name}.jsonl", header, "", record)
        code, _, err = cli(L, ["--file", str(path), "list"])
        check(code == 1, f"[{name}] an invalid JSONL record was ACCEPTED (exit {code})")
        check(needle in err, f"[{name}] failed for the wrong reason: {err!r}")


def t_json_duplicate_keys_use_the_default(L: ModuleType, tmp: Path) -> None:
    """Duplicate JSON keys keep ``json.loads``' default last-value-wins behavior."""
    row = '{"type":"row","pr":"first","pr":"last"}'
    path = write_lines(tmp / "duplicate-key.jsonl", header_line(L), row)
    code, out, err = cli(L, ["--file", str(path), "list"])
    check(code == 0, f"a duplicate JSON key changed from default decoding: exit {code}: {err!r}")
    check(out == "last\n", f"a duplicate JSON key did not keep its last value: {out!r}")


def t_unknown_record_type(L: ModuleType, tmp: Path) -> None:
    """A record type we do not recognise is REJECTED, never skipped.

    A skipped row is a row nothing reads — and if the campaign ever writes a type this accessor has not
    learned, silently dropping it loses a PR's whole state without a word.
    """
    path = write_lines(tmp / "u.jsonl", header_line(L), json.dumps({"type": "note", "pr": "1"}))
    code, _, err = cli(L, ["--file", str(path), "list"])
    check(code == 1, f"an unknown record type was ACCEPTED (exit {code})")
    check("unknown record type" in err, f"failed for the wrong reason: {err!r}")


def t_duplicate_row(L: ModuleType, tmp: Path) -> None:
    """Two rows for one PR is a CORRUPT ledger — `find_row` would silently read the first and `set` would
    update the first while the second stayed behind, disagreeing, forever."""
    path = write_lines(tmp / "dup.jsonl", header_line(L), row_line(L, pr="1"), row_line(L, pr="1"))
    code, _, err = cli(L, ["--file", str(path), "list"])
    check(code == 1, f"a duplicate row was ACCEPTED (exit {code})")
    check("duplicate row for pr 1" in err, f"failed for the wrong reason: {err!r}")
    # …and the duplicate is caught on the NORMALIZED key: a JSON number 1 and the string "1" are ONE pr.
    path = write_lines(tmp / "dup2.jsonl", header_line(L),
                       json.dumps({"type": "row", "pr": 1}), row_line(L, pr="1"))
    code, _, err = cli(L, ["--file", str(path), "list"])
    check(code == 1, f"a duplicate row keyed 1 vs \"1\" was ACCEPTED (exit {code})")
    check("duplicate row for pr 1" in err, f"failed for the wrong reason: {err!r}")


def t_missing_header(L: ModuleType, tmp: Path) -> None:
    """A ledger that EXISTS must carry a header. A missing FILE is a fresh start; a present-but-headerless
    one is CORRUPT — resetting it to defaults would drop the run config and every row without a word."""
    path = write_lines(tmp / "nohdr.jsonl", row_line(L, pr="1"))
    code, _, err = cli(L, ["--file", str(path), "list"])
    check(code == 1, f"a headerless ledger was ACCEPTED (exit {code})")
    check("first record must be the header" in err, f"failed for the wrong reason: {err!r}")

    empty = write_lines(tmp / "empty.jsonl", "", "   ")
    code, _, err = cli(L, ["--file", str(empty), "list"])
    check(code == 1, f"an all-blank ledger was ACCEPTED (exit {code})")
    check("missing header record" in err, f"failed for the wrong reason: {err!r}")

    two = write_lines(tmp / "two.jsonl", header_line(L, run_id="a"), row_line(L, pr="1"),
                      header_line(L, run_id="b"))
    code, _, err = cli(L, ["--file", str(two), "list"])
    check(code == 1, f"a SECOND header was ACCEPTED (exit {code})")
    check("second/out-of-order header" in err, f"failed for the wrong reason: {err!r}")


def t_id_is_derived(L: ModuleType, tmp: Path) -> None:
    """`id` is ALWAYS `pr<pr>` — recomputed on load, never trusted from the file and never caller-set."""
    path = write_lines(tmp / "id.jsonl", header_line(L),
                       json.dumps({"type": "row", "pr": "7", "id": "pwned"}))
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "7", "--field", "id"])
    check((code, out) == (0, "pr7\n"), f"a forged on-disk id survived load(): {out!r} {err!r}")

    fresh = tmp / "id2.jsonl"
    code, out, err = cli(L, ["--file", str(fresh), "add-row", "--pr", "9"])
    check(code == 0, f"add-row exited {code}: {err!r}")
    check(json.loads(out)["id"] == "pr9", f"add-row did not derive id from pr: {out!r}")
    code, out, _ = cli(L, ["--file", str(fresh), "get", "--pr", "9", "--field", "id"])
    check(out == "pr9\n", f"the id did not survive the round trip: {out!r}")


def t_defaults_backfill(L: ModuleType, tmp: Path) -> None:
    """A row written BEFORE a field existed still reads back complete — every absent field takes its default.

    This is what lets a field be ADDED to the schema without migrating the ledger: an old row is not a
    broken row. Drop the back-fill and every accessor starts raising KeyError on real, existing state.

    It is not a hypothetical: this very PR adds `review_rounds`, `ns_streak` and `intent` to the row and
    `skill_version` to the header, and every ledger written before it lacks all four.
    """
    path = write_lines(tmp / "old.jsonl", header_line(L),
                       json.dumps({"type": "row", "pr": "3", "slug": "old"}))
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "3"])
    check(code == 0, f"get on a pre-schema row exited {code}: {err!r}")
    row = json.loads(out)
    check(set(row) == set(L.ROW_FIELDS), f"get did not project onto ROW_FIELDS: {sorted(row)}")
    for f in ("tier", "status", "ci", "attempts", "review_rounds", "ns_streak", "intent"):
        check(row[f] == L.ROW_DEFAULTS[f],
              f"{f} back-filled as {row[f]!r}, not the default {L.ROW_DEFAULTS[f]!r}")
    check(row["slug"] == "old", "the field the row DID carry was overwritten by its default")
    # A header written before a field existed back-fills the same way.
    for f in ("reviewer", "skill_version"):
        code, out, _ = cli(L, ["--file", str(path), "header", "get", f])
        check(out == L.HEADER_DEFAULTS[f] + "\n", f"header default for {f} did not back-fill: {out!r}")


def t_default_non_goals(L: ModuleType, tmp: Path) -> None:
    """`default_non_goals` is a schema-owned run field: old headers back-fill `[]`, valid arrays round-trip
    CANONICALLY, and every malformed value is REFUSED without mutating the ledger. Consumers decode ONLY
    through the accessor, never the raw header value."""
    # A header written before the field existed reads back the canonical empty default, both ways.
    old = write_lines(tmp / "old.jsonl", json.dumps({"type": "header", "run_id": "r"}))
    code, out, err = cli(L, ["--file", str(old), "header", "get", "default_non_goals"])
    check((code, out) == (0, "[]\n"), f"an old header did not back-fill default_non_goals to []: {out!r} {err!r}")
    header, _ = L.load(old)
    check(L.default_non_goals(header) == [], f"the accessor did not decode the back-filled default: {header!r}")

    # A valid array sets, canonicalizes (whitespace trimmed, JSON re-emitted), and the accessor decodes it.
    path = write_lines(tmp / "dng.jsonl", header_line(L, run_id="r"))
    code, _, err = cli(L, ["--file", str(path), "header", "set", "default_non_goals", '["  a b  ", "c"]'])
    check(code == 0, f"a valid default_non_goals array was refused: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "header", "get", "default_non_goals"])
    check(out == '["a b", "c"]\n', f"default_non_goals did not canonicalize on set: {out!r}")
    header, _ = L.load(path)
    check(L.default_non_goals(header) == ["a b", "c"],
          f"the accessor did not decode the stored array: {header!r}")

    # Every malformed value is REFUSED (exit 1) and leaves the stored value EXACTLY as it was.
    before = path.read_text()
    for bad, why in (('{"a": 1}', "a non-array"), ('["a", "a"]', "a duplicate entry"),
                     ('["a", ""]', "a blank entry"), ('["a\\nb"]', "a multi-line entry"),
                     ('not json', "malformed JSON"), ('[1, 2]', "a non-string entry")):
        code, _, err = cli(L, ["--file", str(path), "header", "set", "default_non_goals", bad])
        check(code == 1, f"{why} ({bad!r}) was ACCEPTED (exit {code})")
        check("default_non_goals" in err and "refused" in err, f"{why} failed for the wrong reason: {err!r}")
        check(path.read_text() == before, f"a refused set for {why} MUTATED the ledger")

    # The accessor FAILS CLOSED on a hand-edited malformed stored value — it never guesses a list.
    corrupt = write_lines(tmp / "corrupt.jsonl",
                          json.dumps({"type": "header", "run_id": "r", "default_non_goals": "not-json"}))
    header, _ = L.load(corrupt)
    try:
        L.default_non_goals(header)
        check(False, "the accessor decoded a malformed stored default_non_goals instead of failing closed")
    except ValueError:
        pass


def t_values_are_strings(L: ModuleType, tmp: Path) -> None:
    """Every ingested value is coerced to `str`, so the on-disk JSON's type cannot change a comparison."""
    path = write_lines(tmp / "num.jsonl", header_line(L),
                       json.dumps({"type": "row", "pr": 11, "reviews_ok": 2, "worktree_owned": True}))
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "11"])
    check(code == 0, f"get exited {code}: {err!r}")
    row = json.loads(out)
    check(all(isinstance(v, str) for v in row.values()), f"a non-string value survived load(): {row!r}")
    check(row["reviews_ok"] == "2" and row["worktree_owned"] == "True", f"bad coercion: {row!r}")
    code, out, _ = cli(L, ["--file", str(path), "list", "--where", "reviews_ok=2"])
    check(out == "11\n", f"--where could not match a value that was a JSON number on disk: {out!r}")


def t_null_reads_as_default(L: ModuleType, tmp: Path) -> None:
    """A present JSON `null` is NOT the string "None" — it reads back as that field's DEFAULT, both ways.

    load() coerces every ingested value to `str` (t_values_are_strings) so a number and its spelling are one
    key. But `str(None)` is "None" — a plausible-looking, WRONG non-blank value on a gate field (`ci`,
    `status`, `reviews_ok`). A null means "absent", and absent is the default. dump() must round-trip it the
    same way, never writing "None" (or a bare null) back onto disk.
    """
    path = write_lines(tmp / "null.jsonl",
                       json.dumps({"type": "header", **L.HEADER_DEFAULTS, "reviewer": None}),
                       json.dumps({"type": "row", **L.ROW_DEFAULTS, "pr": "7", "ci": None, "status": None}))
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "7"])
    check(code == 0, f"get on a null-bearing row exited {code}: {err!r}")
    row = json.loads(out)
    check(row["ci"] == L.ROW_DEFAULTS["ci"], f"a null ci read back as {row['ci']!r}, not the default")
    check(row["status"] == L.ROW_DEFAULTS["status"], f"a null status read back as {row['status']!r}, not the default")
    check("None" not in row.values(), f'a JSON null coerced to the string "None": {row!r}')
    code, out, _ = cli(L, ["--file", str(path), "header", "get", "reviewer"])
    check(out == L.HEADER_DEFAULTS["reviewer"] + "\n", f"a null header field read back as {out!r}, not the default")
    # dump() round-trips it the same way: a `set` rewrites the store — the on-disk bytes must carry the
    # default, never "None" and never a bare null. This is the write side of the same guard.
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "7", "--slug", "x"])
    check(code == 0, f"set on a null-bearing row exited {code}: {err!r}")
    raw = path.read_text()
    check('"None"' not in raw, f'dump() wrote the string "None" for a null field: {raw!r}')
    rewritten = json.loads([ln for ln in raw.splitlines() if '"type": "row"' in ln][0])
    check(rewritten["ci"] == L.ROW_DEFAULTS["ci"] and rewritten["ci"] is not None,
          f"dump() did not normalise a null ci to its default: {rewritten['ci']!r}")


# --- the TALLY and the COUNTERS: the review loop's only memory ----------------
#
# `reviews_ok` decides whether a PR may merge. `review_rounds` is the only thing in this schema that can
# see a review loop that is not converging — and it exists because one did: 21 rounds on one PR, with a
# ledger row that read `reviews_ok=0 attempts=0` at round 21 exactly as it had at round 1.

SHA_A = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
SHA_B = "b" * 40


def t_verdict_counts_rounds(L: ModuleType, tmp: Path) -> None:
    """`verdict` bumps `review_rounds` on EVERY landed verdict — satisfied or not — and applies the tally.

    This is the sensor the whole spiral was invisible for. Six rounds are driven here (the shape of a real
    gauntlet: fail, fail, pass, fail, pass, pass), and after them the row must say SIX. `reviews_ok` will
    have been voided and rebuilt twice on the way; `review_rounds` never moves but up.
    """
    path = write_lines(tmp / "v.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))

    def verdict(v: str) -> dict:
        code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                                 "--head-sha", SHA_A, "--verdict", v])
        check(code == 0, f"verdict {v} exited {code}: {err!r}")
        return json.loads(out)

    row = verdict("not-satisfied")
    check((row["review_rounds"], row["reviews_ok"], row["ns_streak"]) == ("1", "0", "1"),
          f"after 1 NOT SATISFIED: {row!r}")
    row = verdict("not-satisfied")
    check((row["review_rounds"], row["reviews_ok"], row["ns_streak"]) == ("2", "0", "2"),
          f"the NS streak must ACCUMULATE: {row!r}")
    row = verdict("satisfied")
    check((row["review_rounds"], row["reviews_ok"], row["ns_streak"]) == ("3", "1", "0"),
          f"a SATISFIED adds one to the tally and CLEARS the streak — and only a SATISFIED does: {row!r}")
    row = verdict("not-satisfied")
    check((row["review_rounds"], row["reviews_ok"], row["ns_streak"]) == ("4", "0", "1"),
          f"a NOT SATISFIED VOIDS the tally — one pass saying the content is wrong is enough: {row!r}")
    row = verdict("satisfied")
    row = verdict("satisfied")
    check((row["review_rounds"], row["reviews_ok"], row["ns_streak"]) == ("6", "2", "0"),
          f"two consecutive SATISFIED on one SHA is the gate met — at round SIX, and the row SAYS six: {row!r}")


def t_review_rounds_never_reset(L: ModuleType, tmp: Path) -> None:
    """**NOTHING RESETS `review_rounds`. There is no door.**

    Not a fix, not a rebase, not a content change, not a re-triage. The rule is enforced by the ABSENCE of
    a flag rather than by a promise: `set --review-rounds 0` and `add-row --review-rounds 0` are refused by
    ARGPARSE, which does not know what the field means and cannot be talked round. `ns_streak` is the same.

    A rule that says "never reset it" is an exhortation, and the loop it guards ran for eight hours under
    three of those. Removing the door is a mechanism.
    """
    path = write_lines(tmp / "nr.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "not-satisfied"])
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "not-satisfied"])

    for field in L.VERDICT_OWNED:
        for door in (["set", "--pr", "1"], ["add-row", "--pr", "77"]):
            code, out, err = cli(L, ["--file", str(path), *door, f"--{field.replace('_', '-')}", "0"])
            check(code == 2,
                  f"`{door[0]} --{field}` was ACCEPTED (exit {code}) — a door that can WRITE this counter "
                  f"is a door that can RESET it, and it is the loop's only memory across heartbeats:\n{out}{err}")
            check("unrecognized arguments" in err,
                  f"`{door[0]} --{field}` failed, but not because the flag does not EXIST: {err!r}")

    # …and the counter is still there, unharmed, after every attempt to write it.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
    check(out == "2\n", f"review_rounds is {out!r}, not '2' — something reset it")

    # A CONTENT CHANGE — the event that legitimately voids the tally — must not touch the rounds either.
    # This is the exact sequence a review fix runs: void the gate, move the head.
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--reviews-ok", "0",
                           "--head-sha", SHA_B])
    check(code == 0, f"the gate reset a fix performs was refused: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
    check(out == "2\n", f"a gate reset took `review_rounds` with it ({out!r}) — this is THE bug: the loop "
                        f"erases the evidence that it is looping, at the exact moment that evidence matters")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "ns_streak"])
    check(out == "2\n", f"a gate reset cleared `ns_streak` ({out!r}) — only a SATISFIED may")


def t_set_cannot_raise_the_tally(L: ModuleType, tmp: Path) -> None:
    """`set` may VOID `reviews_ok`. It may NEVER RAISE it — only a `verdict` adds a verdict.

    A hand-raised tally is indistinguishable from an earned one: nothing downstream can tell them apart,
    and `reviews_ok >= required(tier)` is the merge precondition. So the door is floor-only — and, just as
    importantly, it stays OPEN downward, because voiding the tally on a content change is a real and
    frequent event that is NOT a verdict (a fix commit, a conflict rebase, a formatter's push).
    """
    path = write_lines(tmp / "t.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])

    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--reviews-ok", "2"])
    check(code == 1, f"`set --reviews-ok 2` RAISED the tally (exit {code}) — it manufactured a SATISFIED "
                     f"verdict that no review pass ever returned:\n{out}")
    check("only thing that may" in err or "RAISE the tally" in err,
          f"refused for the wrong reason: {err!r}")

    # A NEW row is the same door: a tally at CREATION is the same forged verdict.
    code, out, err = cli(L, ["--file", str(path), "add-row", "--pr", "5", "--reviews-ok", "2"])
    check(code == 1, f"`add-row --reviews-ok 2` forged a tally on a brand-new row (exit {code}):\n{out}")

    # …and DOWN is still open — this is the gate reset every content change performs.
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--reviews-ok", "0"])
    check(code == 0, f"`set --reviews-ok 0` — the gate reset — was refused: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "0\n", f"the gate reset did not land: {out!r}")

    # …and setting it to the SAME value is not a raise (a re-adoption that rewrites what it read).
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--reviews-ok", "0"])
    check(code == 0, f"re-writing the same tally was refused: {err!r}")


def t_escalation_reset_is_one_atomic_write(L: ModuleType, tmp: Path) -> None:
    """The depth-raising escalation reset — the deeper `tier` AND the voided `reviews_ok` — lands in ONE
    `set` invocation, so the two move together (`loop-control.md`/`pr-adoption.md` re-triage steps).

    Two separate writes (`set --tier HIGH` then `set --reviews-ok 0`) open a window: a driver death between
    them leaves `tier=HIGH, reviews_ok=2`, and the next heartbeat sees no escalation (HIGH→HIGH) so never
    re-triggers the reset — a stricter tier standing on a stale accepted tally. `set` already applies every
    field flag in ONE atomic write, so the single-write form is available; this pins that it is.
    """
    path = write_lines(tmp / "e.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A, tier="STANDARD"))
    # A standing accepted tally at the shallower tier (earned, not hand-set — `set` cannot raise it).
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "2\n", f"setup: the shallow tally is {out!r}, not '2'")

    # THE escalation reset: raise the tier and void the tally in ONE call.
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--tier", "HIGH", "--reviews-ok", "0"])
    check(code == 0, f"the one-write escalation reset was refused (exit {code}): {err!r}")
    row = json.loads(out)
    check(row["tier"] == "HIGH", f"the deeper tier did not land in the same write: {row['tier']!r}")
    check(row["reviews_ok"] == "0", f"the tally was not voided in the same write: {row['reviews_ok']!r}")

    # And it is DURABLE — both fields are in the persisted row, not just the printed one.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "tier"])
    check(out == "HIGH\n", f"persisted tier is {out!r}, not 'HIGH'")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "0\n", f"persisted reviews_ok is {out!r}, not '0' — tier and tally did not move together")


def t_deescalation_is_a_tier_only_write(L: ModuleType, tmp: Path) -> None:
    """A de-escalation (required-lowering flip) writes the tier ALONE and PRESERVES the tally.

    The counterpart of the escalation reset: verdicts earned at a DEEPER depth are a superset that still
    satisfies a shallower tier, so `set --tier <shallower>` with NO `--reviews-ok` flag must leave
    `reviews_ok` standing (`loop-control.md`/`pr-adoption.md`/"Status labels mirror the review gate" — the
    de-escalation branch keeps the verdicts; only `required(tier)` moves). This pins the tool contract the
    prose deletion depends on: the single directional write is tier-only on a de-escalation, never a reset.
    """
    path = write_lines(tmp / "de.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A, tier="HIGH"))
    # A standing tally at the DEEPER tier (earned, not hand-set — `set` cannot raise it).
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "2\n", f"setup: the deep tally is {out!r}, not '2'")

    # THE de-escalation: lower the tier ONLY — no --reviews-ok flag.
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--tier", "STANDARD"])
    check(code == 0, f"the tier-only de-escalation write was refused (exit {code}): {err!r}")
    row = json.loads(out)
    check(row["tier"] == "STANDARD", f"the shallower tier did not land: {row['tier']!r}")
    check(row["reviews_ok"] == "2", f"the tally was voided by a tier-only write: {row['reviews_ok']!r}")

    # And it is DURABLE — the preserved tally is in the persisted row, not just the printed one.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "tier"])
    check(out == "STANDARD\n", f"persisted tier is {out!r}, not 'STANDARD'")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "2\n", f"persisted reviews_ok is {out!r}, not '2' — a de-escalation must preserve verdicts")


def t_verdict_refuses_a_moved_head(L: ModuleType, tmp: Path) -> None:
    """A verdict for a SHA that is not the row's head is REFUSED — it describes content that is gone.

    `review-pass.py verify` already refuses such a pass on the artifacts. This is the same rule at the
    ledger door, so a late verdict from a superseded launch attempt cannot reach the tally by way of a
    driver that skipped the first check. A tool that can only REFUSE a verdict can never merge anything.
    """
    path = write_lines(tmp / "h.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
    code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                             "--head-sha", SHA_B, "--verdict", "satisfied"])
    check(code == 1, f"a verdict for a SUPERSEDED sha was recorded (exit {code}):\n{out}")
    check("no longer there" in err, f"refused for the wrong reason: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "reviews_ok"])
    check(out == "0\n", f"the refused verdict still reached the tally: {out!r}")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
    check(out == "0\n", f"the refused verdict still bumped the rounds: {out!r}")

    # …and a --head-sha that is not a commit id at all is refused before any comparison is made.
    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                           "--head-sha", SHA_A[:7], "--verdict", "satisfied"])
    check(code == 1, "a TRUNCATED sha was accepted as a verdict's commit")
    check("40 LOWERCASE hex" in err, f"refused for the wrong reason: {err!r}")


def t_verdict_needs_a_row_and_a_known_verdict(L: ModuleType, tmp: Path) -> None:
    """The `verdict` door's own domain: a row that exists, and one of exactly two verdicts."""
    path = write_lines(tmp / "d.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "9",
                           "--head-sha", SHA_A, "--verdict", "satisfied"])
    check(code == 1, "a verdict was recorded for a PR with no row")
    check("no row for pr 9" in err, f"refused for the wrong reason: {err!r}")

    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                           "--head-sha", SHA_A, "--verdict", "SATISFIED"])
    check(code == 2, "an unknown verdict spelling was ACCEPTED — argparse must bound the choice")


def t_counter_refuses_a_corrupt_value(L: ModuleType, tmp: Path) -> None:
    """A counter field holding something that is not a count is a CORRUPT store — refused, never guessed at.

    These fields are ARITHMETIC: `verdict` adds to them. `int("-")` does not return a wrong answer, it
    CRASHES — in the middle of a write. And `-` is this schema's own spelling of "not set", so it is a
    value a hand-edited store can genuinely hand us. A crash is not a verdict, and a store accessor that
    raises mid-write is worse than one that refuses.
    """
    for value in ("-", "two", "-1", "01", " 2"):
        path = write_lines(tmp / f"c-{value.strip() or 'blank'}.jsonl", header_line(L),
                           json.dumps({"type": "row", "pr": "1", "head_sha": SHA_A, "base_ok_sha": SHA_A,
                                       "review_rounds": value}))
        code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                                 "--head-sha", SHA_A, "--verdict", "satisfied"])
        check(code == 1, f"review_rounds={value!r} was accepted as a number (exit {code}):\n{out}")
        check("not a count" in err, f"[{value!r}] refused for the wrong reason: {err!r}")


# --- the head_sha door resets THE LIVENESS COUNTERS ---------------------------
#
# A NEW `head_sha` is NEW evidence, so the old head's CI-liveness says nothing about it (stage-2-ci.md, "THE
# LIVENESS COUNTERS"). That property is enforced at the WRITE DOOR — `set --head-sha` — not by prose at each
# caller: on a genuine head move the four `LIVENESS_COUNTERS` reset to their `ROW_DEFAULTS` values in the same
# row write, a same-value write touches nothing, and a non-sha is refused before anything is written.

STALE_LIVENESS = {"ci_fingerprint": "deadbeef", "settled_strikes": "2",
                  "unusable_refetches": "1", "ci_stalled_since": "2026-01-01T00:00:00Z"}


def t_head_sha_change_resets_liveness(L: ModuleType, tmp: Path) -> None:
    """A NEW head_sha through `set` RESETS every LIVENESS_COUNTERS field to its default — in the SAME write —
    while a same-value write leaves them untouched, and a field written in the same call is preserved.

    THIS IS THE MUTATION PIN: delete the reset in `apply_head_sha` and the first block below goes red — a
    new-sha write that leaves stale counters must be impossible through `cmd_set`.
    """
    path = write_lines(tmp / "hs.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, ci="green", **STALE_LIVENESS))
    # NEW head + another field (ci) in ONE call: the counters reset, and the co-written field lands.
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--head-sha", SHA_B, "--ci", "red"])
    check(code == 0, f"set exited {code}: {err!r}")
    row = json.loads(out)
    check(row["head_sha"] == SHA_B, f"the new head did not land: {row['head_sha']!r}")
    check(row["ci"] == "red", f"a field written in the same call as the head move was lost: {row['ci']!r}")
    for field in L.LIVENESS_COUNTERS:
        check(row[field] == L.ROW_DEFAULTS[field],
              f"a NEW head_sha left {field}={row[field]!r} — the door did not reset it to "
              f"{L.ROW_DEFAULTS[field]!r}; a new-sha write leaving stale counters must be impossible")

    # SAME head again: not a move, so nothing resets. Write a fresh strike, then re-set the SAME head.
    cli(L, ["--file", str(path), "set", "--pr", "1", "--settled-strikes", "3"])
    code, out, _ = cli(L, ["--file", str(path), "set", "--pr", "1", "--head-sha", SHA_B])
    row = json.loads(out)
    check(row["settled_strikes"] == "3",
          f"a SAME-VALUE head_sha write reset a counter it must not touch: {row['settled_strikes']!r}")


def t_head_sha_explicit_counter_wins(L: ModuleType, tmp: Path) -> None:
    """An explicit liveness-counter flag in the SAME `set` call as a NEW head_sha WINS over the automatic
    reset — the flag is applied AFTER the reset — while every counter NOT named still resets."""
    path = write_lines(tmp / "hw.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, **STALE_LIVENESS))
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1",
                             "--head-sha", SHA_B, "--settled-strikes", "5"])
    check(code == 0, f"set exited {code}: {err!r}")
    row = json.loads(out)
    check(row["head_sha"] == SHA_B, f"the new head did not land: {row['head_sha']!r}")
    check(row["settled_strikes"] == "5",
          f"an explicit --settled-strikes did NOT win over the auto-reset: {row['settled_strikes']!r}")
    check(row["unusable_refetches"] == L.ROW_DEFAULTS["unusable_refetches"],
          f"a counter NOT named still had to reset on the head move: {row['unusable_refetches']!r}")


def t_set_head_sha_refuses_a_non_sha(L: ModuleType, tmp: Path) -> None:
    """`set --head-sha` validates the shape with SHA_RE — a value that is not 40 lowercase hex is REFUSED,
    and NOTHING is written: no head move, no counter reset. Refusing before any mutation is the point."""
    path = write_lines(tmp / "bad.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, **STALE_LIVENESS))
    for bad in (SHA_A[:7], "g" * 40, SHA_A.upper(), "-"):
        code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--head-sha", bad])
        check(code == 1, f"set --head-sha {bad!r} was ACCEPTED (exit {code}):\n{out}")
        check("40 LOWERCASE hex" in err, f"[{bad!r}] refused for the wrong reason: {err!r}")
    # …and the refused writes changed nothing at all — not the head, not one counter.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1"])
    row = json.loads(out)
    check(row["head_sha"] == SHA_A, f"a refused head_sha write still moved the head: {row['head_sha']!r}")
    for field, want in STALE_LIVENESS.items():
        check(row[field] == want, f"a refused head_sha write reset {field} to {row[field]!r}, not {want!r}")


# --- base_ok_sha: the MECHANICAL base-preflight precondition on `verdict` ------
#
# `verdict` refuses unless a base-preflight `proceed` is on record for THIS head (`base_ok_sha == head_sha`).
# `base-ok` is the ONLY writer; a genuine head move voids the stamp; there is no `set` door (PREFLIGHT_OWNED);
# and stamping is not activity. Together these make "run base-preflight before you record a verdict" a
# mechanism, not a prose rule a fresh-context heartbeat is trusted to remember.

def t_verdict_refused_without_base_ok(L: ModuleType, tmp: Path) -> None:
    """A verdict is REFUSED unless `base_ok_sha == head_sha` — for BOTH verdicts — and ALLOWED once it is.

    Skipping base-preflight leaves the stamp `-`, which no 40-hex head equals, so the door fails CLOSED: a
    review verdict cannot be recorded over a base no `proceed` cleared. A counted NOT SATISFIED spends the loop
    budget, so the guard covers not-satisfied exactly as it covers satisfied. A tool that can only REFUSE a
    verdict can never merge anything.
    """
    for verdict in ("satisfied", "not-satisfied"):
        path = write_lines(tmp / f"nobase-{verdict}.jsonl", header_line(L),
                           row_line(L, pr="1", head_sha=SHA_A))  # base_ok_sha defaults to `-`
        code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                                 "--head-sha", SHA_A, "--verdict", verdict])
        check(code == 1, f"[{verdict}] a verdict with no base-preflight proceed was recorded (exit {code}):\n{out}")
        check("no fresh base-preflight `proceed`" in err, f"[{verdict}] refused for the wrong reason: {err!r}")
        code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
        check(out == "0\n", f"[{verdict}] the refused verdict still bumped review_rounds: {out!r}")

    # A proceed on record for a DIFFERENT (earlier) head does not authorize a verdict on THIS content.
    path = write_lines(tmp / "stale-base.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_B))
    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    check(code == 1, "a verdict authorized by a proceed for a DIFFERENT head was recorded")
    check("no fresh base-preflight `proceed`" in err, f"refused for the wrong reason: {err!r}")

    # …and with the proceed on record for THIS head, the same verdict LANDS.
    path = write_lines(tmp / "withbase.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))
    code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    check(code == 0, f"a verdict WITH a fresh proceed on record was refused (exit {code}): {err!r}")
    check(json.loads(out)["review_rounds"] == "1", f"the allowed verdict did not land: {out!r}")


def t_head_move_voids_base_ok(L: ModuleType, tmp: Path) -> None:
    """A genuine head MOVE through `set --head-sha` voids `base_ok_sha` to `-`; a SAME-VALUE write leaves it.

    A new head is UNVERIFIED until a fresh proceed — so the verdict allowed on the old head is now refused, and
    only re-running base-preflight (here, `base-ok`) on the new head clears it again. THE MUTATION PIN: delete
    the `base_ok_sha` reset in `apply_head_sha` and the first block goes red.
    """
    path = write_lines(tmp / "void.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--head-sha", SHA_B])
    check(code == 0, f"set exited {code}: {err!r}")
    check(json.loads(out)["base_ok_sha"] == L.ROW_DEFAULTS["base_ok_sha"],
          f"a head MOVE left base_ok_sha standing: {json.loads(out)['base_ok_sha']!r} — the proceed for the "
          f"old head must not authorize the new content")
    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_B, "--verdict", "satisfied"])
    check(code == 1, "a verdict landed on a moved head whose proceed was voided")

    # …and a SAME-VALUE head write is not a move: a stamp for that head is left standing.
    cli(L, ["--file", str(path), "base-ok", "--pr", "1", "--head-sha", SHA_B])
    code, out, _ = cli(L, ["--file", str(path), "set", "--pr", "1", "--head-sha", SHA_B])
    check(json.loads(out)["base_ok_sha"] == SHA_B,
          f"a SAME-VALUE head write voided a stamp it must not touch: {json.loads(out)['base_ok_sha']!r}")


def t_base_ok_writes_and_refuses(L: ModuleType, tmp: Path) -> None:
    """`base-ok` records `base_ok_sha` for the row's CURRENT head, and REFUSES a non-40-hex value, a head
    mismatch, or a missing row — writing NOTHING on any refusal."""
    path = write_lines(tmp / "bok.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
    code, out, err = cli(L, ["--file", str(path), "base-ok", "--pr", "1", "--head-sha", SHA_A])
    check(code == 0, f"base-ok on the current head exited {code}: {err!r}")
    check(json.loads(out)["base_ok_sha"] == SHA_A, f"base-ok did not stamp base_ok_sha: {out!r}")

    for bad in (SHA_A[:7], "g" * 40, SHA_A.upper()):
        code, _, err = cli(L, ["--file", str(path), "base-ok", "--pr", "1", "--head-sha", bad])
        check(code == 1, f"base-ok --head-sha {bad!r} was accepted (exit {code})")
        check("40 LOWERCASE hex" in err, f"[{bad!r}] refused for the wrong reason: {err!r}")

    code, _, err = cli(L, ["--file", str(path), "base-ok", "--pr", "1", "--head-sha", SHA_B])
    check(code == 1, "base-ok stamped a head that is not the row's current one")
    check("does not match" in err, f"refused for the wrong reason: {err!r}")

    code, _, err = cli(L, ["--file", str(path), "base-ok", "--pr", "9", "--head-sha", SHA_A])
    check(code == 1, "base-ok wrote a stamp for a PR with no row")
    check("no row for pr 9" in err, f"refused for the wrong reason: {err!r}")

    # …and the stamp for the current head is still exactly SHA_A after every refusal.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "base_ok_sha"])
    check(out == SHA_A + "\n", f"a refused base-ok changed base_ok_sha: {out!r}")


def t_base_ok_sha_has_no_set_door(L: ModuleType, tmp: Path) -> None:
    """`base_ok_sha` cannot be written through `set`/`add-row` — the same mechanism as review_rounds and
    repair_count (PREFLIGHT_OWNED). A door that can hand-write the proceed stamp is a door that can FORGE a
    proceed no base-preflight ever decided, recording a verdict over a base that never passed. So there is no
    `--base-ok-sha` flag: argparse refuses it, and only `base-ok` writes the field."""
    check("base_ok_sha" in L.PREFLIGHT_OWNED, "base_ok_sha must be in PREFLIGHT_OWNED, or a set door can forge it")
    check(not L.settable("base_ok_sha"), "settable() must refuse base_ok_sha — PREFLIGHT_OWNED")
    path = write_lines(tmp / "bnodoor.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
    for door in (["set", "--pr", "1"], ["add-row", "--pr", "77"]):
        code, _, err = cli(L, ["--file", str(path), *door, "--base-ok-sha", SHA_A])
        check(code == 2, f"`{door[0]} --base-ok-sha` was accepted (exit {code}) — a hand-write door can forge a proceed")
        check("unrecognized arguments" in err,
              f"`{door[0]} --base-ok-sha` failed, but not because the flag is ABSENT: {err!r}")


def t_base_ok_is_not_activity(L: ModuleType, tmp: Path) -> None:
    """`base-ok` writes with `activity=False` — recording a precondition is not the run doing meaningful work,
    exactly as re-arming the watchdog is not. Also asserts base_ok_sha IS in ACTIVITY_EXEMPT (name the set)."""
    check("base_ok_sha" in L.ACTIVITY_EXEMPT,
          "base_ok_sha must be in ACTIVITY_EXEMPT — stamping a precondition must not reset the quiet sensor")
    path = write_lines(tmp / "boa.jsonl", header_line(L, run_id="r1"),
                       row_line(L, pr="1", head_sha=SHA_A, status="in_review"))
    with frozen_clock(L, FROZEN_A):  # a real change first, to give last_activity a known value
        code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--slug", "x"])
        check(code == 0, f"baseline set exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A, "the baseline change did not stamp last_activity")
    with frozen_clock(L, FROZEN_B):  # a base-ok write — must NOT re-stamp
        code, _, err = cli(L, ["--file", str(path), "base-ok", "--pr", "1", "--head-sha", SHA_A])
        check(code == 0, f"base-ok exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A,
          f"base-ok re-stamped last_activity to {last_activity(L, path)!r} — stamping a precondition is not "
          f"meaningful activity and must leave the quiet sensor alone")


def t_write_is_atomic(L: ModuleType, tmp: Path) -> None:
    """**A LEDGER WRITE IS ALL OR NOTHING.** No crash, no full disk, may leave a store torn in half.

    `dump()` used to end in `path.write_text(...)` — a TRUNCATE-then-write. Die between the two and the
    ledger on disk is empty or cut short, and `load()` (correctly) refuses a headerless file: the run's
    only memory is gone, and nothing can tell that from a run that never started. `verdict` is what made
    it acute — it moves `review_rounds`, `reviews_ok` and `ns_streak` in ONE write, and those three mean
    something only TOGETHER.

    THE CRASH IS SIMULATED WHERE IT IS FATAL: `os.replace` — the last step, with the new bytes already
    written — is made to raise. Everything the atomic write promises is then checked against what is
    actually on disk: the old ledger is BYTE-IDENTICAL, the temp file was beside it (a rename across
    filesystems is a copy, and a copy tears exactly like the truncate did), the bytes were `fsync`ed
    before anything pointed at them, and nothing was left behind.
    """
    path = write_lines(tmp / "atomic.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A))
    before = path.read_bytes()

    fsyncs = 0
    src: "Path | None" = None
    dst: "Path | None" = None
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(fd)

    def dying_replace(a: str, b: str) -> None:
        nonlocal src, dst
        src, dst = Path(a), Path(b)
        raise OSError("the machine died between the write and the rename")

    raised = False
    # `L.os` IS the os module, so this is a real interception of the call `dump()` makes. Restored in
    # `finally` — a fixture that leaves a broken `os.replace` behind would take every later fixture with it.
    L.os.fsync, L.os.replace = spy_fsync, dying_replace
    try:
        cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "satisfied"])
    except OSError:
        raised = True
    finally:
        L.os.fsync, L.os.replace = real_fsync, real_replace

    check(raised, "the write never reached `os.replace` — a truncate-then-write CANNOT be atomic, and the "
                  "one that is being pinned here is the rename")
    check(path.read_bytes() == before,
          "a write that DIED before the rename still CHANGED the ledger — the store tore, which is the whole "
          f"defect:\n  was: {before!r}\n  now: {path.read_bytes()!r}")
    check(src is not None and src.parent == path.parent,
          f"the new bytes were staged in {src.parent if src else None}, not in the ledger's own directory — "
          f"`os.replace` is atomic only WITHIN a filesystem, and across one it degrades to a copy")
    check(dst == path, f"the rename targeted {dst}, not the ledger at {path}")
    check(fsyncs >= 1,
          "the temp file was never `fsync`ed — the rename would then publish a name whose CONTENT is still "
          "only in the kernel's cache, and a power cut leaves an intact pointer to an empty file")
    left = sorted(p.name for p in path.parent.iterdir() if p != path)
    check(not left, f"the failed write left {left} behind — a half-written temp beside the store is debris "
                    f"the next reader can mistake for one")

    # …and with nothing sabotaged, the same write LANDS: the point is atomicity, not refusal.
    old_mask = os.umask(0o022)
    try:
        code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                               "--head-sha", SHA_A, "--verdict", "satisfied"])
    finally:
        os.umask(old_mask)
    check(code == 0, f"the unsabotaged verdict exited {code}: {err!r}")
    check(path.stat().st_mode & 0o777 == 0o644,
          f"the replacement changed the ledger's create mode to {path.stat().st_mode & 0o777:o}, not 644 "
          "under umask 022")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
    check((code, out) == (0, "1\n"), f"the verdict did not land: review_rounds is {out!r}")
    left = sorted(p.name for p in path.parent.iterdir() if p != path)
    check(not left, f"a SUCCESSFUL write left {left} behind — the temp file must become the ledger, not "
                    f"accumulate beside it")


def t_skill_version_is_recorded(L: ModuleType, tmp: Path) -> None:
    """`skill_version` is a header field, it defaults to `unknown`, and the table SHOWS it.

    A merged, version-bumped rule governs nothing until the installed plugin cache refreshes — and for
    days, one did not. No artifact of that run recorded which version was running, so no report could say
    so. The default is `unknown` and NOT a version number, for the same reason `required_set` defaults to
    `unknown`: "I did not look" is a different fact from any answer, and must never be spelled as one.
    """
    path = write_lines(tmp / "sv.jsonl", header_line(L), row_line(L, pr="1"))
    code, out, _ = cli(L, ["--file", str(path), "header", "get", "skill_version"])
    check((code, out) == (0, "unknown\n"), f"skill_version does not default to `unknown`: {out!r}")

    code, _, err = cli(L, ["--file", str(path), "header", "set", "skill_version", "0.1.4"])
    check(code == 0, f"header set skill_version exited {code}: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "header", "get", "skill_version"])
    check(out == "0.1.4\n", f"skill_version did not survive the round trip: {out!r}")

    # …and it is in the run-config block the table prints, so a report rendered from `table` carries it.
    code, out, _ = cli(L, ["--file", str(path), "table"])
    config, _, _ = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    check("# skill_version: 0.1.4" in config,
          f"the table's run-config block does not state which version governed the run: {config!r}")


# --- the caps: what READS the counters, and stops the loop ---------------------

def verdicts(L: ModuleType, path: Path, seq: str, pr: str = "1", move_head: bool = False) -> "tuple[int, int]":
    """Drive a verdict sequence ('N'/'S') through the real CLI. Returns (verdicts landed, exit of the last).

    `move_head` replays what really happens between rounds: a NOT SATISFIED sends a fix, the fix pushes,
    the head moves. That is what makes this a test of "`review_rounds` survives a fix" and not a rehearsal
    on static content. The head move voids `base_ok_sha`, so — exactly as the real flow re-runs
    base-preflight on the rebased tip — this re-stamps `base-ok` for the new head before the next verdict.

    The CALLER's row must start with `base_ok_sha == SHA_A` (a `proceed` on record for the first head), or
    the very first verdict is refused; the verdict-driving fixtures set it beside `head_sha`.
    """
    sha = SHA_A
    for i, v in enumerate(seq, start=1):
        code, _, _ = cli(L, ["--file", str(path), "verdict", "--pr", pr, "--head-sha", sha,
                             "--verdict", "satisfied" if v == "S" else "not-satisfied"])
        if code != 0:
            return i, code
        if move_head and v == "N":
            sha = f"{i:040x}"
            cli(L, ["--file", str(path), "set", "--pr", pr, "--head-sha", sha])
            cli(L, ["--file", str(path), "base-ok", "--pr", pr, "--head-sha", sha])  # re-preflight the new head
    return len(seq), 0


def capped_row(L: ModuleType, tmp: Path, name: str, **over: str) -> Path:
    # base_ok_sha == head_sha: a base-preflight `proceed` on record for the head, so `verdict` may count.
    fields = {"pr": "1", "head_sha": SHA_A, "base_ok_sha": SHA_A, "status": "in_review", **over}
    return write_lines(tmp / name, header_line(L), row_line(L, **fields))


def t_round_cap_fires(L: ModuleType, tmp: Path) -> None:
    """At ROUND_CAP, a NOT SATISFIED holds the row `repairing` and EXITS NON-ZERO.

    Non-zero is the entire point. The skill's own "hard backstop" on the 2nd NOT SATISFIED sat unfired
    through 35 review rounds because nothing COMPUTED it — it was prose, evaluated by a fresh-context heartbeat
    that could not see round 1 from round 21. A driver that dispatches a fix after this has a FAILED
    COMMAND in its transcript, not a judgment call.
    """
    path = capped_row(L, tmp, "cap.jsonl")
    # alternate so the NS streak never fires — this isolates the ROUND cap
    landed, code = verdicts(L, path, "NS" * (L.ROUND_CAP // 2) + "N"[: L.ROUND_CAP % 2])
    check(code == L.EXIT_STOP,
          f"{landed} verdicts landed and the last exited {code}, not {L.EXIT_STOP} — the round cap did not "
          f"fire, so the driver is free to dispatch fix number {L.ROUND_CAP + 1}")
    check(landed == L.ROUND_CAP, f"the cap fired at verdict {landed}, not {L.ROUND_CAP}")

    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1"])
    row = json.loads(out)
    check(row["status"] == L.REPAIR_STATUS, f"the row is {row['status']!r}, not {L.REPAIR_STATUS!r}")
    check(row["review_rounds"] == str(L.ROUND_CAP), "the verdict that tripped the cap must still be RECORDED")


def t_ns_streak_cap_fires(L: ModuleType, tmp: Path) -> None:
    """NS_STREAK_CAP is an INDEPENDENT sensor — it catches the PR that NEVER scores, well before ROUND_CAP.

    A PR that has not once come back SATISFIED is failing in a way a round count is slow to see. (The cap
    sits below ROUND_CAP on purpose; if the two were equal this fixture would pin nothing.)
    """
    check(L.NS_STREAK_CAP < L.ROUND_CAP, "the streak cap must bite before the round cap, or it is dead code")

    path = capped_row(L, tmp, "streak.jsonl")
    landed, code = verdicts(L, path, "N" * L.NS_STREAK_CAP)
    check(code == L.EXIT_STOP, f"{L.NS_STREAK_CAP} consecutive NOT SATISFIEDs exited {code}")
    check(landed == L.NS_STREAK_CAP, f"the streak cap fired at {landed}, not {L.NS_STREAK_CAP}")

    # …and ONE SATISFIED breaks it: the sensor measures CONSECUTIVE failure and nothing else.
    path = capped_row(L, tmp, "streak2.jsonl")
    _, code = verdicts(L, path, "N" * (L.NS_STREAK_CAP - 1) + "S" + "N")
    check(code == 0, "a SATISFIED did not clear the streak — the sensor is counting the wrong thing")


def t_a_satisfied_never_fires_a_cap(L: ModuleType, tmp: Path) -> None:
    """THE CAPS ARE NEVER EVALUATED ON A SATISFIED — a PR one pass from merging is never torn up.

    Not an optimization: a correctness rule taken from the record. PR #42's TENTH landed verdict was a
    SATISFIED, so a cap that fired on ANY verdict at 10 would have interrupted a PR whose very next pass
    could have merged it. A PR is interrupted only when a verdict says its content is STILL WRONG *and*
    the loop's history says it is no longer converging.
    """
    path = capped_row(L, tmp, "sat.jsonl")
    landed, code = verdicts(L, path, "S" * (L.ROUND_CAP * 2))
    check(code == 0, f"a SATISFIED exited {code} — a cap fired on a PASSING verdict")
    check(landed == L.ROUND_CAP * 2, "the fixture never got past the cap")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "status"])
    check(out.strip() == "in_review", f"a run of SATISFIED verdicts held the row: {out!r}")


def t_no_verdict_for_a_held_row(L: ModuleType, tmp: Path) -> None:
    """No verdict may land on a HELD row — no review pass should have been running for it at all."""
    for status in L.HELD_STATUSES:
        path = capped_row(L, tmp, f"held-{status}.jsonl", status=status)
        code, _, _ = cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A,
                             "--verdict", "not-satisfied"])
        check(code == 1, f"[{status}] a verdict on a held row was ACCEPTED (exit {code})")
        code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "review_rounds"])
        check(out == "0\n", f"[{status}] the REFUSED verdict bumped the counter anyway: {out!r}")


def t_dispatch_check_is_the_guard(L: ModuleType, tmp: Path) -> None:
    """`dispatch-check` refuses a mutating dispatch on EVERY held status, and allows one on a live row.

    The park was already a prose rule obeyed by attention; `repairing` is a new one. Both are now a command
    that FAILS. The message must say what CLEARS the hold — a guard that only says "refused" teaches the
    driver nothing and invites it to route around the guard.
    """
    for status in L.HELD_STATUSES:
        path = capped_row(L, tmp, f"dc-{status}.jsonl", status=status)
        code, _, err = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1"])
        check(code == L.EXIT_STOP, f"[{status}] dispatch-check exited {code}, not {L.EXIT_STOP}")
        check(status in err and "MUTATES" in err, f"[{status}] the refusal does not state the rule: {err!r}")
        check("watch" in err, f"[{status}] the refusal must keep the CI watch exempt — observing is not "
                              f"mutating, and a stopped watch is how a PR goes unwatched forever: {err!r}")

    check(L.REPAIR_STATUS in L.HELD_STATUSES, "the repairing status must be HELD, or NO guard covers it")

    for status in ("in_review", "pending"):
        path = capped_row(L, tmp, f"live-{status}.jsonl", status=status)
        code, _, err = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1"])
        check(code == 0, f"[{status}] a LIVE row was HELD (exit {code}) — the run would stall: {err!r}")

    # `--action repair`: refused with no decision recorded, and refused on a row that is not repairing.
    path = capped_row(L, tmp, "rep.jsonl", status=L.REPAIR_STATUS)
    code, _, err = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == L.EXIT_STOP, f"a repair was dispatchable with NO decision recorded (exit {code})")
    check("NO REASSESSMENT DECISION" in err, f"refused for the wrong reason: {err!r}")

    path = capped_row(L, tmp, "live-rep.jsonl", status="in_review")
    code, _, err = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == L.EXIT_STOP, f"repair work was dispatchable on a PR that never hit a cap (exit {code})")


def t_the_repair_bound_has_no_door(L: ModuleType, tmp: Path) -> None:
    """`repair_count` CANNOT BE WRITTEN through `set`/`add-row` — the same mechanism as `review_rounds`.

    A driver that could zero its own repair budget could repair forever. The bound on the repair is only a
    bound if nothing but the tool that SPENDS it may write it. Enforced by the ABSENCE of a flag, which
    argparse cannot be talked round.
    """
    path = capped_row(L, tmp, "ro.jsonl")
    for field in L.REPAIR_OWNED:
        for door in (["set", "--pr", "1"], ["add-row", "--pr", "77"]):
            code, _, err = cli(L, ["--file", str(path), *door, f"--{field.replace('_', '-')}", "0"])
            check(code == 2, f"`{door[0]} --{field}` was ACCEPTED (exit {code}) — a door that can write the "
                             f"repair budget is a door that can RESET it, and the repair would never end")
            check("unrecognized arguments" in err,
                  f"`{door[0]} --{field}` failed, but not because the flag does not EXIST: {err!r}")


def t_pr_origin_defaults_to_external(L: ModuleType, tmp: Path) -> None:
    """A row whose origin was never established is `external` — the FAIL-SAFE direction.

    `pr_origin` decides whether an autonomous repair may REWRITE this PR's branch. Guessing wrong in the
    other direction lets the mechanism reshape a stranger's work because a field was never set. "I do not
    know who wrote this" must never resolve to "I wrote this".
    """
    path = write_lines(tmp / "origin.jsonl", header_line(L),
                       json.dumps({"type": "row", "pr": "3", "slug": "pre-schema"}))
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "3"])
    check(code == 0, f"get on a pre-schema row exited {code}: {err!r}")
    row = json.loads(out)
    check(row["pr_origin"] == "external",
          f"pr_origin back-filled as {row['pr_origin']!r} — a row of UNKNOWN origin must never be treated "
          f"as campaign's own work to rewrite")
    check((row["repair_count"], row["repair_decision"]) == ("0", "-"), f"repair fields: {row!r}")


# --- the acceptance test: REPLAY THE REAL RECORD -------------------------------

# The verdict sequences PR #42 and PR #43 actually produced, in order, on the night of 2026-07-14 (run
# g260714-0746-bc5e8e20), read off each pass's `VERDICT:` line. The passes that landed NO verdict (#42 r4,
# r7; #43 r5, r16) are absent — a pass that lands no verdict is not a round. `S`/`N` = SATISFIED / NOT.
#
# `protect_through` and `preempts` are the two halves of the thresholds' DEFENCE, and they pull in OPPOSITE
# directions — which is exactly why BOTH are asserted. A cap must land after the work and before the
# spiral, and on this record those two windows very nearly touch.
REAL_RECORD = {
    "42": {
        "verdicts": "NNSNNNSNNSNSNSNNNSNNN",
        "passes": [1, 2, 3, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
        "fires_at": 13,
        # The last finding a real END USER could reach: `accept --at -` exits 0 and writes a store that
        # `list` then refuses to read. Squarely the PR's job — a ledger that corrupts itself.
        "protect_through": 10,
        # THE DISCLOSED COST, asserted so it can never be quietly forgotten. Pass r15 ("the load path
        # accepts blank required fields") is PR-ORIGINAL and gating, and the trigger DOES cut it off. It is
        # not lost: RESCOPE shrinks the PR back to its purpose and RE-GATES it from scratch, and this defect
        # is in the PR's own validation — a fresh reviewer meets it again on the shrunk diff if it survived.
        # What is bought for that risk is TEN ROUNDS: a human stopped this PR at r21; the cap stops it at r13.
        "preempts": [15],
    },
    "43": {
        "verdicts": "NNNNSNNNNNSNNN",
        "passes": [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        "fires_at": 13,
        # THE HARD BAR. #43's four genuine false-green bugs — the exact thing that PR exists to prevent —
        # landed at passes 1, 4, 7 and 11, and pass 12 is the SATISFIED that corroborated the last fix. The
        # trigger must be silent through ALL of it, and it fires at 13: the FIRST spiral round, where the
        # subject of review had become the AST scanner that the r11–r12 fixes themselves built.
        "protect_through": 12,
        "preempts": [],  # nothing genuine is cut off on #43
    },
}


def replay(L: ModuleType, tmp: Path, pr: str, stop_after: "int | None" = None) -> "int | None":
    """Drive one PR's REAL verdict sequence through the REAL CLI. Returns the pass the trigger fired at."""
    rec = REAL_RECORD[pr]
    seq, passes = rec["verdicts"], rec["passes"]
    assert isinstance(seq, str) and isinstance(passes, list)
    check(len(seq) == len(passes), f"[#{pr}] the fixture's own record is inconsistent")
    path = write_lines(tmp / f"replay-{pr}-{stop_after}.jsonl", header_line(L),
                       row_line(L, pr=pr, head_sha=SHA_A, base_ok_sha=SHA_A, status="in_review"))
    sha = SHA_A
    for i, (v, npass) in enumerate(zip(seq, passes), start=1):
        if stop_after is not None and npass > stop_after:
            return None
        code, _, _ = cli(L, ["--file", str(path), "verdict", "--pr", pr, "--head-sha", sha,
                             "--verdict", "satisfied" if v == "S" else "not-satisfied"])
        if code == L.EXIT_STOP:
            code, out, _ = cli(L, ["--file", str(path), "get", "--pr", pr, "--field", "status"])
            check(out.strip() == L.REPAIR_STATUS, f"[#{pr}] the trigger left the row live")
            return npass
        check(code == 0, f"[#{pr}] verdict {i} (pass r{npass}) exited {code}")
        if v == "N":  # a fix was dispatched and pushed: the head MOVED, and base-preflight re-ran on the new tip
            sha = f"{i:040x}"
            cli(L, ["--file", str(path), "set", "--pr", pr, "--head-sha", sha])
            cli(L, ["--file", str(path), "base-ok", "--pr", pr, "--head-sha", sha])
    return None


def t_replay_the_real_record(L: ModuleType, tmp: Path) -> None:
    """THE ACCEPTANCE TEST. Drive the two PRs that ACTUALLY spiralled through the real CLI.

    Three claims, and the last two are what make the thresholds *defended* rather than merely chosen:

    1. **The trigger fires.** #42 at review pass r13 (it ran 23), #43 at r13 (it ran 16). The loop stops.
    2. **It never fires before the genuine work is done.** #43's four false-green bugs landed at passes 1,
       4, 7 and 11 and were corroborated at 12; the trigger is silent through all of it. A cap that aborted
       #43 at pass 3 would have destroyed the entire point of that PR.
    3. **What it DOES cut off is STATED, not hidden.** On #42 the trigger pre-empts the PR-original finding
       at pass r15. That is a real cost, it is asserted here, and RESCOPE + a full re-gate is what recovers
       it. A design whose costs live only in its author's head is a design nobody can review.

    Re-tune a cap and this fixture tells you, in its own failure message, what the new number would have
    done to two PRs that really ran.
    """
    for pr, rec in REAL_RECORD.items():
        fires_at, protect, preempts = rec["fires_at"], rec["protect_through"], rec["preempts"]
        assert isinstance(fires_at, int) and isinstance(protect, int) and isinstance(preempts, list)

        fired_at = replay(L, tmp, pr)
        check(fired_at is not None,
              f"[#{pr}] the trigger NEVER FIRED across all {len(rec['verdicts'])} landed verdicts — this is "
              f"the 8.5-hour spiral, exactly as it happened, and the mechanism did nothing about it")
        check(fired_at == fires_at,
              f"[#{pr}] the trigger fired at review pass r{fired_at}, not r{fires_at}. With "
              f"ROUND_CAP={L.ROUND_CAP} / NS_STREAK_CAP={L.NS_STREAK_CAP} the loop stops at a different "
              f"round than the one the thresholds were defended against — re-derive them against the "
              f"record before changing this expectation")

        # (2) THE WORK IS PROTECTED. Replayed independently, so it cannot be satisfied by the run above
        # having already stopped: this asserts the trigger is SILENT through the last genuine finding.
        check(replay(L, tmp, pr, stop_after=protect) is None,
              f"[#{pr}] THE TRIGGER FIRED AT OR BEFORE PASS r{protect}, WHERE THE PR WAS STILL FINDING "
              f"GENUINE, PURPOSE-SERVING DEFECTS. A cap this tight destroys real value — #43's four "
              f"false-green bugs ARE the reason that PR exists. Raise ROUND_CAP (={L.ROUND_CAP}) / "
              f"NS_STREAK_CAP (={L.NS_STREAK_CAP}); do NOT weaken this fixture")

        # (3) THE COST IS DISCLOSED — a fact about the thresholds, so it is asserted like one.
        for p in preempts:
            check(fires_at < p,
                  f"[#{pr}] the record claims the trigger pre-empts the gating finding at pass r{p}, but it "
                  f"fires at r{fires_at} — the disclosure is now STALE, which is worse than no disclosure: "
                  f"it is the version people read")


def t_stale_repair_decision_cleared_at_cap(L: ModuleType, tmp: Path) -> None:
    """RE-ENTERING `repairing` at a cap CLEARS any stale reassessment decision — so the repair BUDGET binds.

    `repair_decision` is durable and is written by exactly two things: `repair-pass.py decide` (which also
    SPENDS `repair_count`) and this reset in `cmd_verdict`. A decision recorded for a PREVIOUS cap — left on
    the row after that repair completed and the row returned to `in_review` — would otherwise still be there
    when the loop hits a cap AGAIN, and `dispatch-check --action repair` accepts ANY non-`-` decision. A
    driver would then dispatch a SECOND repair with no fresh `decide`, spending no budget, and `REPAIR_CAP`
    would never bind: the mechanism that bounds non-convergence would itself fail to converge.

    So a NOT SATISFIED that trips a cap must land the row `repairing` AND wipe the stale decision to `-`.
    Delete that one write in `cmd_verdict` and this fixture goes red.
    """
    # The state a completed FIRST repair leaves behind: budget spent once, its decision still on the row,
    # and the loop back in review. Written RAW — `repair-pass.py decide` is what produces it, and
    # `repair_count`/`repair_decision` have NO `set` flag by design (t_the_repair_bound_has_no_door).
    stale = "rescope@2026-07-14T00:00:00Z"
    path = write_lines(tmp / "stale.jsonl", header_line(L),
                       row_line(L, pr="1", head_sha=SHA_A, base_ok_sha=SHA_A, status="in_review",
                                pr_origin="gauntlet", repair_count="1", repair_decision=stale))
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "repair_decision"])
    check(out.strip() == stale, f"fixture setup is wrong — the row does not carry the stale decision: {out!r}")

    # Hit the cap AGAIN via an independent NS streak — the last verdict trips it and exits EXIT_STOP.
    landed, code = verdicts(L, path, "N" * L.NS_STREAK_CAP)
    check(code == L.EXIT_STOP, f"the cap did not fire ({landed} verdicts landed, exit {code})")

    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1"])
    row = json.loads(out)
    check(row["status"] == L.REPAIR_STATUS, f"the row is {row['status']!r}, not {L.REPAIR_STATUS!r}")
    # THE PIN: the stale decision is GONE. Without the clear-on-entry write it would still read `stale`.
    check(row["repair_decision"] == "-",
          f"a STALE decision survived the re-entry: {row['repair_decision']!r}. `dispatch-check --action "
          f"repair` accepts any non-`-` decision, so a second repair would dispatch with NO fresh `decide` "
          f"and REPAIR_CAP would never bind")
    # The budget was NOT spent by hitting the cap — only a fresh `decide` spends it, which is exactly what
    # now holds the bound: one `decide` per cap, one `repair_count` per `decide`.
    check(row["repair_count"] == "1",
          f"re-entering the cap moved repair_count to {row['repair_count']!r} — the budget is spent by "
          f"`decide`, never by reaching the cap")

    # The consequence the pin buys: a repair is REFUSED until a fresh reassessment decides one.
    code, _, err = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == L.EXIT_STOP,
          f"a repair was dispatchable at the SECOND cap with no fresh decision (exit {code}) — the stale "
          f"decision was not cleared, so the repair budget can be bypassed")
    check("NO REASSESSMENT DECISION" in err, f"refused for the wrong reason: {err!r}")

    # …until a fresh decision is recorded (the state `repair-pass.py decide` writes: budget now at
    # REPAIR_CAP). The refusal LIFTS — the guard blocks until a decision exists, not forever.
    fresh = "rescope@2026-07-14T01:00:00Z"
    write_lines(path, header_line(L),
                row_line(L, pr="1", head_sha=SHA_A, status=L.REPAIR_STATUS,
                         pr_origin="gauntlet", repair_count="2", repair_decision=fresh))
    code, out, _ = cli(L, ["--file", str(path), "dispatch-check", "--pr", "1", "--action", "repair"])
    check(code == 0, f"a repair with a FRESH decision recorded was refused (exit {code})")
    check(fresh in out, f"dispatch-check must name the decision to dispatch: {out!r}")


# --- park / unpark: the machine-blocker park is a MULTI-FIELD atomic write -----
#
# A park (status=awaiting-user, ci_reason=<blocker>, blocker_ruling=-) and a retry unpark (status=in_review,
# ruling spent, the four liveness counters reset) are each coherent only as ONE write. Hand-assembled from
# `set` a field gets dropped — a park with no ci_reason is unanswerable, a retry that leaves the counters
# re-escalates. These fixtures pin that each subcommand does the WHOLE write, refuses what it must, and
# leaves everything it does not own untouched.

PARK_ROW = dict(pr="1", head_sha=SHA_A, status="in_review", settled_strikes="2", unusable_refetches="1",
                ci_stalled_since="2026-07-14T00:00:00Z", ci_fingerprint="sha256:zz")


def count_dumps(L: ModuleType, fn) -> int:
    """Run `fn` and return how many times the accessor wrote the store — a park/unpark is ONE write."""
    calls = [0]
    real = L.dump

    def spy(*a, **k):  # noqa: ANN002,ANN003
        calls[0] += 1
        return real(*a, **k)

    # `setattr`, not `L.dump = spy`: assigning through the ModuleType attribute is a Pyright error on the
    # type-clean list, and this is a test seam, not a schema write.
    setattr(L, "dump", spy)
    try:
        fn()
    finally:
        setattr(L, "dump", real)
    return calls[0]


def t_park_writes_all_three(L: ModuleType, tmp: Path) -> None:
    """`park` sets status=awaiting-user, ci_reason=<blocker>, blocker_ruling=- in ONE write — and touches
    NOTHING else, the liveness counters included (a park does not reset them; only the unpark does)."""
    path = write_lines(tmp / "p.jsonl", header_line(L),
                       row_line(L, blocker_ruling="retry@2026-07-01T00:00:00Z", **PARK_ROW))
    captured: dict = {}

    def run() -> None:
        captured["r"] = cli(L, ["--file", str(path), "park", "--pr", "1", "--reason", "BLOCKED merge state — park"])

    dumps = count_dumps(L, run)
    code, out, err = captured["r"]
    check(code == 0, f"park exited {code}: {err!r}")
    check(dumps == 1, f"park wrote the store {dumps} times, not once — it is ONE atomic write")
    row = json.loads(out)
    check((row["status"], row["ci_reason"], row["blocker_ruling"])
          == ("awaiting-user", "BLOCKED merge state — park", "-"),
          f"park did not write the three fields atomically: {row!r}")
    # the liveness counters and every unrelated field are UNTOUCHED — park is not the counter-reset site.
    check((row["settled_strikes"], row["unusable_refetches"], row["ci_stalled_since"], row["ci_fingerprint"])
          == ("2", "1", "2026-07-14T00:00:00Z", "sha256:zz"),
          f"park reset a liveness counter — that is the UNPARK's job, not the park's: {row!r}")
    check(row["head_sha"] == SHA_A, f"park disturbed an unrelated field: {row!r}")
    # …and it is on DISK, not just in the printed JSON.
    code, got, _ = cli(L, ["--file", str(path), "get", "--pr", "1"])
    check(json.loads(got)["status"] == "awaiting-user", f"the park did not land on disk: {got!r}")


def t_park_refusals(L: ModuleType, tmp: Path) -> None:
    """Every park refusal writes NOTHING, and the double-park STOPS with the OPEN question surfaced."""
    # no row
    code, _, err = cli(L, ["--file", str(tmp / "none.jsonl"), "park", "--pr", "9", "--reason", "x"])
    check(code == 1 and "no row for pr 9" in err, f"park on a missing row: exit {code}, {err!r}")

    # terminal rows
    for term in ("merged", "aborted"):
        path = write_lines(tmp / f"{term}.jsonl", header_line(L), row_line(L, pr="1", status=term))
        code, _, err = cli(L, ["--file", str(path), "park", "--pr", "1", "--reason", "x"])
        check(code == 1 and term in err and "terminal" in err, f"[{term}] park was allowed: exit {code}, {err!r}")
        check("awaiting-user" not in path.read_text(), f"[{term}] a refused park still wrote the store")

    # empty / `-` reason — a park that cannot NAME its blocker
    for reason in ("", "   ", "-"):
        path = write_lines(tmp / f"r{reason.strip() or 'blank'}.jsonl", header_line(L),
                           row_line(L, pr="1", status="in_review"))
        code, _, err = cli(L, ["--file", str(path), "park", "--pr", "1", "--reason", reason])
        check(code == 1 and "name its blocker" in err, f"[{reason!r}] park with no blocker: exit {code}, {err!r}")
        check("awaiting-user" not in path.read_text(), f"[{reason!r}] a refused park still wrote the store")

    # conflicting-owner held states — each names the conflicting status, exit 1, nothing written
    for status in ("awaiting-api", L.REPAIR_STATUS):
        path = write_lines(tmp / f"o-{status}.jsonl", header_line(L), row_line(L, pr="1", status=status))
        code, _, err = cli(L, ["--file", str(path), "park", "--pr", "1", "--reason", "x"])
        check(code == 1 and status in err, f"[{status}] park did not name the conflicting owner: exit {code}, {err!r}")
        check("awaiting-user" not in path.read_text(), f"[{status}] a refused park still wrote the store")

    # DOUBLE PARK — a park is already open. STOP (EXIT_STOP), surface the OPEN ci_reason, overwrite NOTHING.
    open_q = "SETTLED at the STRIKE CAP — nobody is coming"
    path = write_lines(tmp / "dbl.jsonl", header_line(L),
                       row_line(L, pr="1", status="awaiting-user", ci_reason=open_q,
                                blocker_ruling="-"))
    code, _, err = cli(L, ["--file", str(path), "park", "--pr", "1", "--reason", "a DIFFERENT blocker"])
    check(code == L.EXIT_STOP, f"a second park did not STOP: exit {code}")
    check(open_q in err, f"the double-park refusal must surface the OPEN question: {err!r}")
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "ci_reason"])
    check(out.strip() == open_q,
          f"a second park OVERWROTE the open question — the very thing the user is being asked to rule on: {out!r}")


def t_unpark_spends_and_resets(L: ModuleType, tmp: Path) -> None:
    """`unpark` flips status=in_review, SPENDS the ruling to `-`, and resets ALL FOUR liveness counters —
    in ONE write — while every unrelated field survives."""
    path = write_lines(tmp / "u.jsonl", header_line(L),
                       row_line(L, blocker_ruling="retry@2026-07-14T01:00:00Z", status="awaiting-user",
                                ci_reason="SETTLED — nobody coming", slug="keep-me", reviews_ok="1",
                                review_rounds="5", ns_streak="0",
                                **{k: v for k, v in PARK_ROW.items() if k not in ("status",)}))
    captured: dict = {}

    def run() -> None:
        captured["r"] = cli(L, ["--file", str(path), "unpark", "--pr", "1"])

    dumps = count_dumps(L, run)
    code, out, err = captured["r"]
    check(code == 0, f"unpark exited {code}: {err!r}")
    check(dumps == 1, f"unpark wrote the store {dumps} times, not once — it is ONE atomic write")
    row = json.loads(out)
    check((row["status"], row["blocker_ruling"]) == ("in_review", "-"),
          f"unpark did not flip status and SPEND the ruling: {row!r}")
    # ALL FOUR liveness counters back to their ROW_DEFAULTS — asserted against the accessor's own defaults.
    for f in L.LIVENESS_COUNTERS:
        check(row[f] == L.ROW_DEFAULTS[f],
              f"unpark left liveness counter {f}={row[f]!r}, not its default {L.ROW_DEFAULTS[f]!r} — the "
              f"retry would re-escalate on its first derivation")
    # …and the fields the unpark does NOT own are all still there.
    check((row["head_sha"], row["slug"], row["reviews_ok"], row["review_rounds"], row["ci_reason"])
          == (SHA_A, "keep-me", "1", "5", "SETTLED — nobody coming"),
          f"unpark disturbed a field it does not own (ci_reason is overwritten by the NEXT derivation, "
          f"never by the unpark): {row!r}")
    code, got, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "status"])
    check(got.strip() == "in_review", f"the unpark did not land on disk: {got!r}")


def t_unpark_refusals(L: ModuleType, tmp: Path) -> None:
    """Every unpark refusal writes NOTHING; the unanswered park STOPS, and abort is routed to its own flow."""
    # no row
    code, _, err = cli(L, ["--file", str(tmp / "none.jsonl"), "unpark", "--pr", "9"])
    check(code == 1 and "no row for pr 9" in err, f"unpark on a missing row: exit {code}, {err!r}")

    # not parked — nothing to unpark
    for status in ("in_review", "pending", "merged", L.REPAIR_STATUS):
        path = write_lines(tmp / f"np-{status}.jsonl", header_line(L),
                           row_line(L, pr="1", status=status, blocker_ruling="retry@2026-07-14T00:00:00Z"))
        code, _, err = cli(L, ["--file", str(path), "unpark", "--pr", "1"])
        check(code == 1 and "not awaiting-user" in err, f"[{status}] unpark was allowed: exit {code}, {err!r}")

    # UNANSWERED park — ruling `-`. STOP (EXIT_STOP), surface the open blocker, write nothing.
    path = write_lines(tmp / "unans.jsonl", header_line(L),
                       row_line(L, pr="1", status="awaiting-user", ci_reason="the OPEN question",
                                blocker_ruling="-"))
    code, _, err = cli(L, ["--file", str(path), "unpark", "--pr", "1"])
    check(code == L.EXIT_STOP, f"unparking an UNANSWERED park did not STOP: exit {code}")
    check("the OPEN question" in err, f"the unanswered-unpark refusal must surface the open blocker: {err!r}")
    code, got, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "status"])
    check(got.strip() == "awaiting-user", f"a refused unpark still moved the row off the park: {got!r}")

    # ABORT ruling — NOT an unpark; routed to the abort flow, exit 1, nothing written
    path = write_lines(tmp / "abort.jsonl", header_line(L),
                       row_line(L, pr="1", status="awaiting-user", ci_reason="q",
                                blocker_ruling="abort@2026-07-14T00:00:00Z"))
    code, _, err = cli(L, ["--file", str(path), "unpark", "--pr", "1"])
    check(code == 1 and "abort" in err.lower(), f"an abort ruling was treated as an unpark: exit {code}, {err!r}")
    code, got, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "status"])
    check(got.strip() == "awaiting-user", f"a refused (abort) unpark still moved the row: {got!r}")

    # MALFORMED stamps — a bare `retry`, an empty stamp, a non-date stamp — all refused
    for bad in ("retry", "retry@", "retry@not-a-date", "retryX@2026-07-14T00:00:00Z"):
        path = write_lines(tmp / f"bad-{bad.strip('@') or 'x'}.jsonl", header_line(L),
                           row_line(L, pr="1", status="awaiting-user", ci_reason="q", blocker_ruling=bad))
        code, _, err = cli(L, ["--file", str(path), "unpark", "--pr", "1"])
        check(code == 1 and "well-formed" in err,
              f"[{bad!r}] a malformed ruling was accepted as a retry: exit {code}, {err!r}")
        check("in_review" not in path.read_text(), f"[{bad!r}] a refused unpark still wrote the store")


def t_set_status_transitions_stay_open(L: ModuleType, tmp: Path) -> None:
    """The `set --status awaiting-user` / `set --status in_review` transitions are DELIBERATELY still
    allowed — the design decision that keeps the REVIEW-STANDOFF park (finding-audit.md) working.

    The standoff park is answered through `finding-audit.py rule-standoff`, NOT `blocker_ruling`, and its unpark carries no
    `retry@<iso>` ruling for `unpark` to validate — so park/unpark CANNOT serve it and `set` must stay open.
    If a future change gates these transitions to force everything through park/unpark, this fixture goes
    red and says why: the standoff writer would break.
    """
    path = write_lines(tmp / "so.jsonl", header_line(L), row_line(L, pr="1", status="in_review"))
    # standoff PARK via set — no ci_reason, no ruling; answered through finding-audit.py
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "awaiting-user"])
    check(code == 0, f"`set --status awaiting-user` was refused — the review-standoff park is broken: {err!r}")
    check(json.loads(out)["status"] == "awaiting-user", f"set did not write the standoff park: {out!r}")
    # standoff UNPARK via set — a plain return to in_review, no retry ruling involved
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
    check(code == 0, f"`set --status in_review` was refused — the standoff unpark is broken: {err!r}")
    check(json.loads(out)["status"] == "in_review", f"set did not write the standoff unpark: {out!r}")
    # `blocker_ruling` also stays settable via set — it is where the user's ANSWER is recorded
    code, out, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--blocker-ruling", "retry@2026-07-14T00:00:00Z"])
    check(code == 0 and json.loads(out)["blocker_ruling"] == "retry@2026-07-14T00:00:00Z",
          f"`set --blocker-ruling` was refused — the user's answer cannot be recorded: exit {code}, {err!r}")


# --- last_activity: the run's durable "when did anything last move?" sensor -----
#
# Two frozen instants, so a stamp is DETERMINISTIC and a no-op write can be PROVEN not to re-stamp (the
# second value would differ from the first if it did). The clock is a module global that `save()` looks up
# by name, so replacing it is the same test-injectable-clock move `lease-test.py` makes on `now`.
FROZEN_A = "2026-01-02T03:04:05+00:00"
FROZEN_B = "2026-06-07T08:09:10+00:00"


@contextlib.contextmanager
def frozen_clock(L: ModuleType, value: str):
    """Freeze the accessor's activity clock to `value` for the duration of the block, then restore it."""
    prev = L.now_activity
    setattr(L, "now_activity", lambda: value)
    try:
        yield
    finally:
        setattr(L, "now_activity", prev)


def last_activity(L: ModuleType, path: Path) -> str:
    """Read the header's `last_activity` through the REAL CLI — the value a heartbeat would see."""
    code, out, err = cli(L, ["--file", str(path), "header", "get", "last_activity"])
    check(code == 0, f"header get last_activity exited {code}: {err!r}")
    return out.rstrip("\n")


def t_activity_stamped_on_a_real_change(L: ModuleType, tmp: Path) -> None:
    """A value-CHANGING `set` stamps `last_activity` in the same write; a NO-OP `set` leaves it untouched.

    The no-op half is what has teeth: the second write is frozen to a DIFFERENT instant, and the stamp must
    still read the FIRST — a sensor that re-stamped on a write that changed nothing would report a stalled
    run as alive, which is the exact reading it exists to prevent.
    """
    path = write_lines(tmp / "act.jsonl", header_line(L, run_id="r1"), row_line(L, pr="1", status="pending"))
    check(last_activity(L, path) == "-", "a fresh ledger must start with last_activity `-`")
    with frozen_clock(L, FROZEN_A):
        code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
        check(code == 0, f"set exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A,
          f"a value-changing set did not stamp last_activity: {last_activity(L, path)!r}")
    with frozen_clock(L, FROZEN_B):  # a NO-OP: status is ALREADY in_review
        code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
        check(code == 0, f"no-op set exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A,
          f"a NO-OP set re-stamped last_activity to {last_activity(L, path)!r} — a write that changes "
          f"nothing is not activity and must leave the sensor alone")


def t_verdict_stamps_activity(L: ModuleType, tmp: Path) -> None:
    """A landed `verdict` stamps `last_activity` — it always moves review_rounds, so it is always activity."""
    sha = "a" * 40
    path = write_lines(tmp / "v.jsonl", header_line(L, run_id="r1"),
                       row_line(L, pr="1", head_sha=sha, base_ok_sha=sha, status="in_review"))
    with frozen_clock(L, FROZEN_A):
        code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", sha,
                               "--verdict", "satisfied"])
        check(code == 0, f"verdict exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A,
          f"a landed verdict did not stamp last_activity: {last_activity(L, path)!r}")


def t_liveness_only_write_is_not_activity(L: ModuleType, tmp: Path) -> None:
    """A write that moves ONLY a liveness counter does NOT stamp — polling that saw no PR change is exactly
    the "nothing moved" last_activity exists to expose. Every member of LIVENESS_COUNTERS, one at a time.

    Reading `L.LIVENESS_COUNTERS` (never a retyped list) and asserting this fixture covers it is the
    mechanical half of "name the set": add a counter to that tuple without exempting it from activity and
    THIS fixture goes red, instead of a new counter silently stamping the sensor on every CI poll.
    """
    values = {"ci_fingerprint": "abc123", "settled_strikes": "2",
              "unusable_refetches": "3", "ci_stalled_since": "2026-05-05T05:05:05+00:00"}
    check(set(values) == set(L.LIVENESS_COUNTERS),
          f"this fixture does not cover every liveness counter — it tests {tuple(values)} but "
          f"LIVENESS_COUNTERS is {L.LIVENESS_COUNTERS}; a counter it misses could stamp with no fixture red")
    for field, value in values.items():
        path = write_lines(tmp / f"lv-{field}.jsonl", header_line(L, run_id="r1"),
                           row_line(L, pr="1", status="pending"))
        with frozen_clock(L, FROZEN_A):  # a real change first, to give last_activity a known value
            code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
            check(code == 0, f"[{field}] baseline set exited {code}: {err!r}")
        check(last_activity(L, path) == FROZEN_A, f"[{field}] the baseline change did not stamp")
        with frozen_clock(L, FROZEN_B):  # a liveness-counter-only write — must NOT re-stamp
            flag = "--" + field.replace("_", "-")
            code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", flag, value])
            check(code == 0, f"[{field}] liveness set exited {code}: {err!r}")
        check(last_activity(L, path) == FROZEN_A,
              f"[{field}] a liveness-counter-only set re-stamped last_activity to "
              f"{last_activity(L, path)!r} — a CI poll that saw no change is not meaningful activity")


def t_last_activity_is_a_sensor_no_door(L: ModuleType, tmp: Path) -> None:
    """`header set last_activity` is REFUSED — a sensor has no door, exactly as review_rounds has no flag —
    and it writes NOTHING. `header get last_activity` still READS it."""
    path = write_lines(tmp / "sensor.jsonl", header_line(L, run_id="r1"))
    code, out, err = cli(L, ["--file", str(path), "header", "set", "last_activity",
                             "2000-01-01T00:00:00+00:00"])
    check(code == 1, f"header set last_activity must be REFUSED (exit 1); got {code}: {out!r}")
    check("sensor" in err.lower(), f"the refusal must state WHY last_activity is not settable: {err!r}")
    check(last_activity(L, path) == "-",
          f"a REFUSED header set still wrote last_activity: {last_activity(L, path)!r} — the forged date "
          f"reached disk")


def t_last_activity_absent_is_tolerated(L: ModuleType, tmp: Path) -> None:
    """An OLD ledger written before last_activity existed reads back the default `-`, `table` still renders,
    and a mutating op on it stamps a fresh one — absence is a fresh start, never an error."""
    old_header = json.dumps({"type": "header", "run_id": "r1", "base_branch": "main"})  # NO last_activity key
    path = write_lines(tmp / "old.jsonl", old_header, row_line(L, pr="1", status="pending"))
    check(last_activity(L, path) == "-",
          f"an old ledger without last_activity must read back `-`: {last_activity(L, path)!r}")
    code, _, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table on a pre-field ledger exited {code}: {err!r}")  # readers tolerate the absence
    with frozen_clock(L, FROZEN_A):
        code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
        check(code == 0, f"set exited {code}: {err!r}")
    check(last_activity(L, path) == FROZEN_A,
          f"a mutating op on a pre-field ledger did not stamp last_activity: {last_activity(L, path)!r}")


# --- watchdog: the durable long-cadence health-pass deadline ------------------
#
# A single frozen instant so `arm` stamps a DETERMINISTIC deadline and `check` classifies it against a known
# `now`. `now_watchdog()` is the module clock `arm`/`check` both look up by name, so replacing it is the same
# test-injectable-clock move `frozen_clock` makes on `now_activity`.
WD_NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def frozen_watchdog(L: ModuleType, value: datetime):
    """Freeze the accessor's watchdog clock to `value` for the block, then restore it."""
    prev = L.now_watchdog
    setattr(L, "now_watchdog", lambda: value)
    try:
        yield
    finally:
        setattr(L, "now_watchdog", prev)


def header_field(L: ModuleType, path: Path, field: str) -> str:
    """Read one header field through the REAL CLI — the value a heartbeat would see."""
    code, out, err = cli(L, ["--file", str(path), "header", "get", field])
    check(code == 0, f"header get {field} exited {code}: {err!r}")
    return out.rstrip("\n")


def t_watchdog_arm_stamps_a_future_deadline(L: ModuleType, tmp: Path) -> None:
    """`watchdog arm` stamps `watchdog_due = now + WATCHDOG_INTERVAL` — a FUTURE aware ISO stamp — and does
    so through the real CLI. The teeth: the stamped value is EXACTLY the frozen now plus the module interval,
    so a drifted interval or a wrong sign is caught, not waved through."""
    path = write_lines(tmp / "wd.jsonl", header_line(L, run_id="r1"), row_line(L, pr="1", status="in_review"))
    check(header_field(L, path, "watchdog_due") == "-", "a fresh ledger must start with watchdog_due `-`")
    with frozen_watchdog(L, WD_NOW):
        code, _, err = cli(L, ["--file", str(path), "watchdog", "arm"])
        check(code == 0, f"watchdog arm exited {code}: {err!r}")
    expected = (WD_NOW + L.WATCHDOG_INTERVAL).isoformat()
    check(header_field(L, path, "watchdog_due") == expected,
          f"arm did not stamp now+WATCHDOG_INTERVAL: {header_field(L, path, 'watchdog_due')!r} != {expected!r}")


def t_watchdog_check_states(L: ModuleType, tmp: Path) -> None:
    """`watchdog check` prints EXACTLY one of unset/ok <rem>/due <age>/invalid — re-arm, always exit 0, and a
    malformed OR naive stored stamp is the `invalid` state, never a crash. Every branch, one fixture."""
    def check_line(due: "str | None") -> str:
        over = {} if due is None else {"watchdog_due": due}
        path = write_lines(tmp / "chk.jsonl", header_line(L, run_id="r1", **over))
        with frozen_watchdog(L, WD_NOW):
            code, out, err = cli(L, ["--file", str(path), "watchdog", "check"])
        check(code == 0, f"watchdog check must ALWAYS exit 0 (due={due!r}); got {code}: {err!r}")
        return out.rstrip("\n")

    check(check_line("-") == "unset", "a `-` deadline reads `unset`")
    check(check_line(None) == "unset", "an absent watchdog_due (old ledger) reads `unset`")
    future = (WD_NOW + timedelta(minutes=30)).isoformat()
    check(check_line(future) == "ok 30m", f"a future deadline reads `ok <remaining>`: {check_line(future)!r}")
    past = (WD_NOW - timedelta(minutes=7)).isoformat()
    check(check_line(past) == "due 7m", f"a past deadline reads `due <age>`: {check_line(past)!r}")
    check(check_line("2020-01-01T00:00:00") == "invalid — re-arm",
          "a NAIVE stamp (no tzinfo) is the invalid state — it cannot be compared to an aware now")
    check(check_line("not-a-date") == "invalid — re-arm",
          "a MALFORMED stamp is the invalid state — advisory repair-by-re-arm, never a crash")


def t_watchdog_arm_is_not_activity(L: ModuleType, tmp: Path) -> None:
    """`watchdog arm` must NOT stamp `last_activity` — `watchdog_due` is in ACTIVITY_EXEMPT, so re-arming the
    watchdog cannot masquerade as the run doing meaningful work and reset the quiet sensor it backs.

    The teeth: give last_activity a known value, then arm under a DIFFERENT frozen watchdog instant; the
    sensor must still read the first value. Also asserts watchdog_due IS in ACTIVITY_EXEMPT (name the set)."""
    check("watchdog_due" in L.ACTIVITY_EXEMPT,
          "watchdog_due must be in ACTIVITY_EXEMPT — a re-arm that stamped last_activity would defeat the "
          "quiet sensor the watchdog exists to back")
    path = write_lines(tmp / "arm-act.jsonl", header_line(L, run_id="r1"), row_line(L, pr="1", status="pending"))
    with frozen_clock(L, FROZEN_A):  # a real change first, to give last_activity a known value
        code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--status", "in_review"])
        check(code == 0, f"baseline set exited {code}: {err!r}")
    check(header_field(L, path, "last_activity") == FROZEN_A, "the baseline change did not stamp last_activity")
    with frozen_watchdog(L, WD_NOW):
        code, _, err = cli(L, ["--file", str(path), "watchdog", "arm"])
        check(code == 0, f"watchdog arm exited {code}: {err!r}")
    check(header_field(L, path, "last_activity") == FROZEN_A,
          f"watchdog arm stamped last_activity to {header_field(L, path, 'last_activity')!r} — a re-arm is "
          f"not meaningful activity and must leave the quiet sensor alone")


def t_watchdog_due_has_no_door(L: ModuleType, tmp: Path) -> None:
    """`header set watchdog_due` is REFUSED — a tool-stamped deadline has no hand-write door, exactly as
    `last_activity` has none — and it writes NOTHING. `header get watchdog_due` still READS it."""
    path = write_lines(tmp / "nodoor.jsonl", header_line(L, run_id="r1"))
    code, out, err = cli(L, ["--file", str(path), "header", "set", "watchdog_due", "2020-01-01T00:00:00+00:00"])
    check(code == 1, f"header set watchdog_due must be REFUSED (exit 1); got {code}: {out!r}")
    check("watchdog_due" in err, f"the refusal must name the field: {err!r}")
    check(header_field(L, path, "watchdog_due") == "-",
          f"a REFUSED header set still wrote watchdog_due: {header_field(L, path, 'watchdog_due')!r} — the "
          f"forged deadline reached disk")


def t_watchdog_interval_prints_the_constant(L: ModuleType, tmp: Path) -> None:
    """`watchdog interval` prints WATCHDOG_INTERVAL in whole minutes, reads NO ledger (needs no --file), and
    the printed number is DERIVED from the constant — never a hard-coded literal that could drift from it."""
    code, out, err = cli(L, ["watchdog", "interval"])
    check(code == 0, f"watchdog interval must exit 0 with no --file; got {code}: {err!r}")
    expected = int(L.WATCHDOG_INTERVAL.total_seconds() // 60)
    check(out.rstrip("\n") == str(expected),
          f"watchdog interval printed {out.rstrip(chr(10))!r}, not the constant's {expected} minutes")


def t_pending_adoption_is_an_ordinary_field(L: ModuleType, tmp: Path) -> None:
    """`pending_adoption` is a NORMAL settable config field: `header set` writes it, it round-trips, clears
    back to `-`, and — unlike watchdog_due — setting it IS meaningful activity (it stamps last_activity)."""
    path = write_lines(tmp / "pend.jsonl", header_line(L, run_id="r1"), row_line(L, pr="1", status="pending"))
    check(header_field(L, path, "pending_adoption") == "-", "pending_adoption must default to `-`")
    with frozen_clock(L, FROZEN_A):
        code, _, err = cli(L, ["--file", str(path), "header", "set", "pending_adoption", "89 90"])
        check(code == 0, f"header set pending_adoption exited {code}: {err!r}")
    check(header_field(L, path, "pending_adoption") == "89 90",
          f"pending_adoption did not round-trip: {header_field(L, path, 'pending_adoption')!r}")
    check(header_field(L, path, "last_activity") == FROZEN_A,
          "setting pending_adoption IS meaningful activity — it must stamp last_activity (no exemption)")
    with frozen_clock(L, FROZEN_B):  # clearing it back to `-` is a real value change → still activity
        code, _, err = cli(L, ["--file", str(path), "header", "set", "pending_adoption", "-"])
        check(code == 0, f"clearing pending_adoption exited {code}: {err!r}")
    check(header_field(L, path, "pending_adoption") == "-", "pending_adoption must clear back to `-`")
    check(header_field(L, path, "last_activity") == FROZEN_B, "clearing pending_adoption is also activity")


# --- row-owned base / required set, with the header as the LEGACY FALLBACK -------------------------------
#
# `base_branch` and `required_set` are per-ROW state now; the header fields are only what a row with no
# explicit value inherits. These fixtures pin the schema half of mixed-base support (each consumer's own
# resolution through the accessors is exercised by that consumer's test suite): the two accessors, the
# immutable creation-only row base, and the `-` (inherit) vs `unknown` (explicit, fail-closed) distinction.

def t_old_ledger_resolves_through_the_header(L: ModuleType, tmp: Path) -> None:
    """An old ledger — written before the row base fields existed — loads and resolves through the header.

    Every row lacks `base_branch`/`required_set`, so each back-fills to `-` (the schema's "inherit the
    header" spelling), and `effective_base`/`effective_required_set` return the header's values: the exact
    behavior a single-base run had before these fields existed, with no migration and no file rewrite.
    """
    path = write_lines(
        tmp / "old.jsonl",
        header_line(L, run_id="r1", base_branch="main",
                    required_set='declared:[{"context": "test"}]'),
        json.dumps({"type": "row", "pr": "41", "slug": "old", "head_sha": SHA_A}),
    )
    # The row carries neither field on disk, and both read back as the `-` inherit default.
    code, out, err = cli(L, ["--file", str(path), "get", "--pr", "41"])
    check(code == 0, f"get on a pre-base-field row exited {code}: {err!r}")
    row = json.loads(out)
    check(row["base_branch"] == "-", f"an old row's base_branch must back-fill to `-`: {row['base_branch']!r}")
    check(row["required_set"] == "-", f"an old row's required_set must back-fill to `-`: {row['required_set']!r}")
    # …and the accessors resolve that `-` to the header — the legacy value, unchanged.
    header, rows = L.load(path)
    check(L.effective_base(header, rows[0]) == "main",
          f"effective_base of a `-` row must inherit the header: {L.effective_base(header, rows[0])!r}")
    check(L.effective_required_set(header, rows[0]) == 'declared:[{"context": "test"}]',
          f"effective_required_set of a `-` row must inherit the header: "
          f"{L.effective_required_set(header, rows[0])!r}")


def t_new_row_owns_its_base(L: ModuleType, tmp: Path) -> None:
    """A new row stores an explicit base, and the accessors return IT — never the header — for that row.

    This is what lets one run mix bases: the header can say one thing (or `-`) while a row on `v3` and a row
    on `main` each resolve to their own recorded base.
    """
    path = write_lines(tmp / "new.jsonl", header_line(L, run_id="r2", base_branch="-", required_set="unknown"))
    code, _, err = cli(L, ["--file", str(path), "add-row", "--pr", "52", "--base-branch", "v3",
                           "--required-set", 'declared:[{"context": "v3-test"}]'])
    check(code == 0, f"add-row with an explicit base exited {code}: {err!r}")
    code, _, err = cli(L, ["--file", str(path), "add-row", "--pr", "53", "--base-branch", "main"])
    check(code == 0, f"add-row with an explicit base exited {code}: {err!r}")
    header, rows = L.load(path)
    by_pr = {r["pr"]: r for r in rows}
    check(L.effective_base(header, by_pr["52"]) == "v3",
          f"an explicit row base must win over the header: {L.effective_base(header, by_pr['52'])!r}")
    check(L.effective_base(header, by_pr["53"]) == "main",
          f"an explicit row base must win over the header: {L.effective_base(header, by_pr['53'])!r}")
    check(L.effective_required_set(header, by_pr["52"]) == 'declared:[{"context": "v3-test"}]',
          f"an explicit row required_set must win over the header: "
          f"{L.effective_required_set(header, by_pr['52'])!r}")


def t_row_base_is_creation_only(L: ModuleType, tmp: Path) -> None:
    """The row base is written ONCE at `add-row` and is IMMUTABLE after — `set` has no `--base-branch` flag.

    The campaign never migrates a row to a new base (an unsupported live-base change parks the row for the
    user, later-stage work), so the recorded base cannot be rewritten. The mechanism is the absent-door one
    `base_ok_sha`/`review_rounds` use, asymmetric across the doors: present at `add-row`, absent at `set`.
    """
    check("base_branch" in L.CREATE_ONLY, "base_branch must be in CREATE_ONLY, or `set` could rewrite it")
    check(L.settable("base_branch"), "base_branch is still settable() — CREATE_ONLY narrows only WHICH door")
    path = write_lines(tmp / "cre.jsonl", header_line(L, run_id="r1"))
    code, _, err = cli(L, ["--file", str(path), "add-row", "--pr", "1", "--base-branch", "v3"])
    check(code == 0, f"add-row --base-branch exited {code}: {err!r}")
    # `set --base-branch` is an argparse refusal — the flag is ABSENT at that door, exactly like base_ok_sha.
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--base-branch", "main"])
    check(code == 2, f"`set --base-branch` was accepted (exit {code}) — the recorded base is not immutable")
    check("unrecognized arguments" in err,
          f"`set --base-branch` failed, but not because the flag is ABSENT: {err!r}")
    # …and the stored base is untouched by the refused write.
    code, out, _ = cli(L, ["--file", str(path), "get", "--pr", "1", "--field", "base_branch"])
    check(out == "v3\n", f"the recorded base changed despite the refusal: {out!r}")
    # `required_set` is NOT creation-only: the grouped required-set refresh rewrites it through `set`.
    check("required_set" not in L.CREATE_ONLY,
          "required_set must stay settable via `set` — the grouped required-set refresh writes it")
    code, _, err = cli(L, ["--file", str(path), "set", "--pr", "1", "--required-set", "none"])
    check(code == 0, f"`set --required-set` must stay open for the grouped required-set refresh: exit {code}, {err!r}")


def t_required_set_dash_vs_unknown(L: ModuleType, tmp: Path) -> None:
    """`-` and `unknown` are DIFFERENT row required-set states, and the accessor treats them differently.

    `-` means "this row owns no set — inherit the header". `unknown` is an EXPLICIT row value meaning a read
    for this base was attempted and failed, so it must fail closed and NOT be replaced by the header (a
    header `none`/`declared` silently overriding a row's `unknown` is exactly the false-green this guards).
    """
    path = write_lines(
        tmp / "req.jsonl",
        header_line(L, run_id="r1", required_set="none"),
        row_line(L, pr="1", required_set="-"),        # inherit
        row_line(L, pr="2", required_set="unknown"),  # explicit, fail-closed
    )
    header, rows = L.load(path)
    by_pr = {r["pr"]: r for r in rows}
    check(L.effective_required_set(header, by_pr["1"]) == "none",
          f"a `-` row must inherit the header required_set: {L.effective_required_set(header, by_pr['1'])!r}")
    check(L.effective_required_set(header, by_pr["2"]) == "unknown",
          f"an explicit `unknown` must be returned as-is, never replaced by the header: "
          f"{L.effective_required_set(header, by_pr['2'])!r}")
    # The base accessor draws the same line: `-` inherits, an explicit name wins.
    check(L.effective_base(header, {"base_branch": "-"}) == L.HEADER_DEFAULTS["base_branch"],
          "a `-` base must inherit the header")
    check(L.effective_base({"base_branch": "main"}, {"base_branch": "v3"}) == "v3",
          "an explicit base must win over the header")

    # A `-` row inherits the header required_set ONLY when its base IS the header base — the header describes
    # the header base alone. A `-` row on a DIFFERENT base (a mixed-base config) has no settled set here and
    # stays the fail-closed `unknown`, never reading as the header base's set (a false green). This is the
    # SAME base-agreement rule the grouped refresh applies (ci-status.refresh_required_set).
    mixed_header = {"base_branch": "main", "required_set": "none"}
    check(L.effective_required_set(mixed_header, {"base_branch": "main", "required_set": "-"}) == "none",
          "a `-` row on the header base must inherit the settled header")
    other = L.effective_required_set(mixed_header, {"base_branch": "v3", "required_set": "-"})
    check(other == L.HEADER_DEFAULTS["required_set"],
          f"a `-` row on a DIFFERENT base must NOT read as the header base's set — fail closed: {other!r}")


def t_base_agrees(L: ModuleType, _tmp: Path) -> None:
    """`base_agrees` — the ONE owner of the `--base`-assertion comparison every consumer routes through.

    Identical strings ALWAYS agree — a base literally named `origin/<x>` (a legal branch name) must match
    itself, so `--base origin/release` against a stored `origin/release` can never be read as a
    disagreement. A leading `origin/` is stripped from the ARGUMENT only (the review-diff form asserting a
    bare stored base); the STORED base is never stripped, so a bare argument cannot assert a base literally
    named `origin/<x>`.
    """
    check(L.base_agrees("main", "main"), "identical bare names must agree")
    check(L.base_agrees("origin/main", "main"), "an origin/-prefixed ARGUMENT asserts the bare stored base")
    check(L.base_agrees("origin/release", "origin/release"),
          "identical strings must ALWAYS agree — a base literally named origin/release matches itself")
    check(not L.base_agrees("release", "origin/release"),
          "the STORED base is never stripped — a bare argument cannot assert a base literally named "
          "origin/release")
    check(not L.base_agrees("v3", "main"), "different names must disagree")
    check(not L.base_agrees("origin/v3", "main"), "a stripped argument naming a different base must disagree")


def t_require_effective_base(L: ModuleType, _tmp: Path) -> None:
    """`require_effective_base` — the ONE owner of "a resolved-base consumer fails closed on an unresolved base".

    A usable base is returned as `(base, None)`; an unresolved one — blank, whitespace-only, or the `-` unset
    sentinel on BOTH the row and the header — is refused as `(None, reason)`, the reason naming the PR. This is
    what every base-USING consumer (clean-rebase, review-dispatch, worker-prompt, carryover, base-preflight,
    triage, repair-pass) routes through, so a `-` base is refused BEFORE it is compared, written, or diffed.
    """
    # A real resolved base passes through unchanged, with no problem.
    base, problem = L.require_effective_base({"base_branch": "main"}, {"base_branch": "-"}, "1")
    check(base == "main" and problem is None, f"a `-` row inheriting a real header base is usable: {base!r} {problem!r}")
    base, problem = L.require_effective_base({"base_branch": "main"}, {"base_branch": "v3"}, "1")
    check(base == "v3" and problem is None, f"an explicit row base is usable: {base!r} {problem!r}")
    # The both-`-` state (row AND header unset) — `effective_base` returns `-` — is REFUSED, not returned.
    base, problem = L.require_effective_base({"base_branch": "-"}, {"base_branch": "-"}, "999")
    check(base is None and problem == "pr 999 has no usable effective base in the ledger",
          f"a both-`-` ledger must fail closed, naming the PR: {base!r} {problem!r}")
    # A blank or whitespace-only resolved base is unresolved too — never a branch a caller may use.
    base, problem = L.require_effective_base({"base_branch": ""}, {"base_branch": "-"}, "5")
    check(base is None and problem is not None, f"a blank effective base must fail closed: {base!r} {problem!r}")
    base, problem = L.require_effective_base({"base_branch": "  "}, {"base_branch": "-"}, "5")
    check(base is None and problem is not None, f"a whitespace-only effective base must fail closed: {base!r} {problem!r}")


def t_table_shows_the_effective_base(L: ModuleType, tmp: Path) -> None:
    """`table` shows a computed, display-only `base` column: the row's EFFECTIVE base, not the raw field.

    A legacy `-` row shows the header base it inherits (never a bare `-`), while a raw `--fields base_branch`
    still shows the stored `-`; a new explicit-base row shows its own base. The column is a valid `--fields`
    selector, and — being a normal cell — it is escaped like any other, so a hostile base cannot forge layout.
    """
    check(L.TABLE_BASE_COLUMN in L.TABLE_DEFAULT_FIELDS, "the default table must carry the `base` column")
    check(L.TABLE_BASE_COLUMN not in L.ROW_FIELDS, "`base` is a COMPUTED column, never a stored field")
    path = write_lines(
        tmp / "tb.jsonl", header_line(L, run_id="r1", base_branch="main"),
        json.dumps({"type": "row", "pr": "41", "slug": "legacy"}),        # inherits main
        row_line(L, pr="52", base_branch="v3", status="in_review"),       # explicit v3
    )
    # Default view: the `base` column resolves each row.
    code, out, err = cli(L, ["--file", str(path), "table"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = grid(L, out, L.TABLE_DEFAULT_FIELDS)
    pcol, bcol = L.TABLE_DEFAULT_FIELDS.index("pr"), L.TABLE_DEFAULT_FIELDS.index("base")
    got = {c[pcol]: c[bcol] for c in cells}
    check(got == {"41": "main", "52": "v3"},
          f"the `base` column did not resolve each row's effective base: {got!r}\n{out}")
    # `base` is selectable; `base_branch` still shows the RAW stored value (the legacy row's `-`).
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", "pr,base,base_branch"])
    check(code == 0, f"table --fields base exited {code}: {err!r}")
    _, _, cells = grid(L, out, ("pr", "base", "base_branch"))
    check(cells == [["41", "main", "-"], ["52", "v3", "v3"]],
          f"`base` (effective) and `base_branch` (raw) must differ for a legacy row: {cells!r}\n{out}")
    # A hostile base value is escaped in the computed column — it cannot forge a row, column or config line.
    hostile_base = "v3\n# run_id: forged | x"
    path = write_lines(tmp / "tbx.jsonl", header_line(L, run_id="real"),
                       row_line(L, pr="1", base_branch=hostile_base))
    code, out, err = cli(L, ["--file", str(path), "table", "--fields", "pr,base"])
    check(code == 0, f"table with a hostile base exited {code}: {err!r}")
    _, _, cells = grid(L, out, ("pr", "base"))
    check(cells == [["1", L.escape_cell(hostile_base)]],
          f"a hostile base was not escaped in the computed column: {cells!r}\n{out}")


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
    ("jsonl-object-records", "malformed JSON and non-object records name their source line", t_jsonl_records_are_objects),
    ("json-default-duplicates", "duplicate JSON keys keep json.loads' last-value-wins behavior", t_json_duplicate_keys_use_the_default),
    ("unknown-record-type", "an unrecognised record type is REJECTED, never skipped", t_unknown_record_type),
    ("duplicate-row", "two rows for one pr is a corrupt ledger — on the NORMALIZED key", t_duplicate_row),
    ("missing-header", "a present ledger must carry exactly one header, FIRST", t_missing_header),
    ("id-derived", "id is always pr<pr> — never trusted from the file, never caller-set", t_id_is_derived),
    ("defaults-backfill", "a row written before a field existed still reads back complete", t_defaults_backfill),
    ("values-are-strings", "every ingested value is coerced to str", t_values_are_strings),
    ("default-non-goals", "default_non_goals: old headers back-fill [], valid arrays canonicalize, malformed values are refused unmutated, decode only through the accessor", t_default_non_goals),
    ("null-reads-as-default", "a present JSON null reads back as the field default, not the string \"None\"", t_null_reads_as_default),
    ("verdict-counts-rounds", "`verdict` bumps review_rounds on EVERY verdict and applies the tally atomically", t_verdict_counts_rounds),
    ("rounds-never-reset", "NOTHING resets review_rounds/ns_streak — there is no flag, at any door", t_review_rounds_never_reset),
    ("tally-floor-only", "`set` may VOID reviews_ok, NEVER raise it — only a verdict adds a verdict", t_set_cannot_raise_the_tally),
    ("escalation-reset-atomic", "the depth-raising reset writes deeper tier + voided tally in ONE atomic set", t_escalation_reset_is_one_atomic_write),
    ("deescalation-tier-only", "a de-escalation writes the tier ALONE and preserves the standing tally", t_deescalation_is_a_tier_only_write),
    ("verdict-head-pinned", "a verdict for a SUPERSEDED sha is refused — it describes content that is gone", t_verdict_refuses_a_moved_head),
    ("verdict-domain", "`verdict` needs a real row and one of exactly two verdicts", t_verdict_needs_a_row_and_a_known_verdict),
    ("counter-corrupt", "a counter field that is not a count is refused, never handed to int()", t_counter_refuses_a_corrupt_value),
    ("head-sha-resets-liveness", "a NEW head_sha through `set` resets THE LIVENESS COUNTERS; a same-value write does not", t_head_sha_change_resets_liveness),
    ("head-sha-explicit-counter-wins", "an explicit counter flag in the same call as a new head_sha wins over the auto-reset", t_head_sha_explicit_counter_wins),
    ("head-sha-refuses-non-sha", "`set --head-sha` refuses a non-40-hex value and writes nothing", t_set_head_sha_refuses_a_non_sha),
    ("verdict-needs-base-ok", "a verdict is refused unless base_ok_sha == head_sha — both verdicts; allowed once stamped", t_verdict_refused_without_base_ok),
    ("head-move-voids-base-ok", "a head MOVE voids base_ok_sha to `-`; a same-value write leaves it", t_head_move_voids_base_ok),
    ("base-ok-writes-refuses", "`base-ok` stamps the current head; refuses non-40-hex, head-mismatch, no-row", t_base_ok_writes_and_refuses),
    ("base-ok-sha-no-door", "base_ok_sha has NO set/add-row flag (PREFLIGHT_OWNED) — a hand-write door forges a proceed", t_base_ok_sha_has_no_set_door),
    ("base-ok-not-activity", "a base-ok write does NOT stamp last_activity — stamping a precondition is not activity", t_base_ok_is_not_activity),
    ("write-atomic", "a write that dies mid-way leaves the OLD ledger intact — temp + fsync + os.replace", t_write_is_atomic),
    ("skill-version", "the header records WHICH VERSION of the rules governed the run; default `unknown`", t_skill_version_is_recorded),
    ("round-cap", "at ROUND_CAP a NOT SATISFIED holds the row `repairing` and exits non-zero", t_round_cap_fires),
    ("ns-streak-cap", "NS_STREAK_CAP is an independent sensor; one SATISFIED clears the streak", t_ns_streak_cap_fires),
    ("satisfied-never-fires", "a cap is NEVER evaluated on a SATISFIED — a passing PR is never torn up", t_a_satisfied_never_fires_a_cap),
    ("held-row-no-verdict", "no verdict may land on a held row", t_no_verdict_for_a_held_row),
    ("dispatch-check", "every HELD status refuses a mutating dispatch; a repair needs its decision first", t_dispatch_check_is_the_guard),
    ("repair-bound-no-door", "repair_count has NO flag — a budget you can zero is not a bound", t_the_repair_bound_has_no_door),
    ("repair-decision-cleared", "re-entering a cap CLEARS the stale reassessment decision — the repair budget binds", t_stale_repair_decision_cleared_at_cap),
    ("pr-origin-default", "an unknown origin is `external` — the fail-safe direction", t_pr_origin_defaults_to_external),
    ("park-writes-three", "`park` writes status/ci_reason/blocker_ruling atomically, counters untouched", t_park_writes_all_three),
    ("park-refusals", "park refuses no-row/terminal/blank-reason/held-owner, and a double-park STOPs", t_park_refusals),
    ("unpark-spends-resets", "`unpark` flips status, spends the ruling, resets all four counters — one write", t_unpark_spends_and_resets),
    ("unpark-refusals", "unpark refuses not-parked/unanswered/abort/malformed — writing nothing", t_unpark_refusals),
    ("set-status-stays-open", "set may still write the standoff park/unpark transitions — park/unpark can't serve them", t_set_status_transitions_stay_open),
    ("replay-the-record", "the REAL #42/#43 verdict sequences: it fires, never too early, and says what it costs", t_replay_the_real_record),
    ("activity-stamped-on-change", "a value-changing set stamps last_activity; a no-op set does not", t_activity_stamped_on_a_real_change),
    ("verdict-stamps-activity", "a landed verdict stamps last_activity — it always moves review_rounds", t_verdict_stamps_activity),
    ("liveness-not-activity", "a liveness-counter-only write does NOT stamp — polling that saw no change is not activity", t_liveness_only_write_is_not_activity),
    ("last-activity-sensor", "last_activity has NO door — `header set last_activity` is refused and writes nothing", t_last_activity_is_a_sensor_no_door),
    ("last-activity-absent-ok", "an old ledger without last_activity reads `-` and mutating it stamps fresh", t_last_activity_absent_is_tolerated),
    ("watchdog-arm-future", "watchdog arm stamps watchdog_due = now + WATCHDOG_INTERVAL (a future stamp)", t_watchdog_arm_stamps_a_future_deadline),
    ("watchdog-check-states", "watchdog check prints unset/ok/due/invalid, always exit 0, naive+malformed → invalid", t_watchdog_check_states),
    ("watchdog-arm-not-activity", "watchdog arm does NOT stamp last_activity — watchdog_due is ACTIVITY_EXEMPT", t_watchdog_arm_is_not_activity),
    ("watchdog-due-no-door", "`header set watchdog_due` is refused and writes nothing", t_watchdog_due_has_no_door),
    ("watchdog-interval", "watchdog interval prints the constant in minutes, reads no ledger", t_watchdog_interval_prints_the_constant),
    ("pending-adoption-ordinary", "pending_adoption is an ordinary settable field; setting it IS activity", t_pending_adoption_is_an_ordinary_field),
    ("old-ledger-header-fallback", "an old row with no base fields loads and resolves through the header", t_old_ledger_resolves_through_the_header),
    ("new-row-owns-its-base", "a new row's explicit base wins over the header, per row", t_new_row_owns_its_base),
    ("row-base-creation-only", "the row base is written once at add-row and immutable — no `set --base-branch`", t_row_base_is_creation_only),
    ("required-set-dash-vs-unknown", "`-` inherits the header; explicit `unknown` fails closed, never inherits", t_required_set_dash_vs_unknown),
    ("base-agrees", "base_agrees: identical strings always agree; only the ARGUMENT's origin/ is stripped", t_base_agrees),
    ("require-effective-base", "require_effective_base: a usable base passes; a blank/whitespace/`-` base fails closed naming the PR", t_require_effective_base),
    ("table-effective-base", "table shows a computed `base` column — the effective base, escaped like any cell", t_table_shows_the_effective_base),
]


def run(L: ModuleType, tmp: Path) -> int:
    """Run every fixture against the accessor handed in. Exit 0 iff every rule it claims actually holds."""
    failures = 0
    # A SUITE WITH NOTHING IN IT PASSES VACUOUSLY — the loudest false green there is, and one this repo
    # has shipped. The count is asserted before anything runs.
    if not CASES:
        print("FAIL     the suite holds NO fixtures — a self-test with no subject passes every time")
        return 1
    for name, rule, fn in CASES:
        work = tmp / name
        work.mkdir(parents=True, exist_ok=True)
        try:
            fn(L, work)
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


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        return run(load_ledger(), Path(tmp))


if __name__ == "__main__":
    sys.exit(main())
