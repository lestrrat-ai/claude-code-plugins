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

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType

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
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = L.main(argv)
    except SystemExit as exc:  # fail() -> 1; argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


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
    path = write_lines(tmp / "v.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))

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
    path = write_lines(tmp / "nr.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "not-satisfied"])
    cli(L, ["--file", str(path), "verdict", "--pr", "1", "--head-sha", SHA_A, "--verdict", "not-satisfied"])

    for field in L.VERDICT_OWNED:
        for door in (["set", "--pr", "1"], ["add-row", "--pr", "77"]):
            code, out, err = cli(L, ["--file", str(path), *door, f"--{field.replace('_', '-')}", "0"])
            check(code == 2,
                  f"`{door[0]} --{field}` was ACCEPTED (exit {code}) — a door that can WRITE this counter "
                  f"is a door that can RESET it, and it is the loop's only memory across wakes:\n{out}{err}")
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
    path = write_lines(tmp / "t.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
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
                           json.dumps({"type": "row", "pr": "1", "head_sha": SHA_A, "review_rounds": value}))
        code, out, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                                 "--head-sha", SHA_A, "--verdict", "satisfied"])
        check(code == 1, f"review_rounds={value!r} was accepted as a number (exit {code}):\n{out}")
        check("not a count" in err, f"[{value!r}] refused for the wrong reason: {err!r}")


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
    path = write_lines(tmp / "atomic.jsonl", header_line(L), row_line(L, pr="1", head_sha=SHA_A))
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
    code, _, err = cli(L, ["--file", str(path), "verdict", "--pr", "1",
                           "--head-sha", SHA_A, "--verdict", "satisfied"])
    check(code == 0, f"the unsabotaged verdict exited {code}: {err!r}")
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
    on static content.
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
    return len(seq), 0


def capped_row(L: ModuleType, tmp: Path, name: str, **over: str) -> Path:
    fields = {"pr": "1", "head_sha": SHA_A, "status": "in_review", **over}
    return write_lines(tmp / name, header_line(L), row_line(L, **fields))


def t_round_cap_fires(L: ModuleType, tmp: Path) -> None:
    """At ROUND_CAP, a NOT SATISFIED holds the row `repairing` and EXITS NON-ZERO.

    Non-zero is the entire point. The skill's own "hard backstop" on the 2nd NOT SATISFIED sat unfired
    through 35 review rounds because nothing COMPUTED it — it was prose, evaluated by a fresh-context wake
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
                       row_line(L, pr=pr, head_sha=SHA_A, status="in_review"))
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
        if v == "N":  # a fix was dispatched and pushed: the head MOVED
            sha = f"{i:040x}"
            cli(L, ["--file", str(path), "set", "--pr", pr, "--head-sha", sha])
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
                       row_line(L, pr="1", head_sha=SHA_A, status="in_review",
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
    ("null-reads-as-default", "a present JSON null reads back as the field default, not the string \"None\"", t_null_reads_as_default),
    ("verdict-counts-rounds", "`verdict` bumps review_rounds on EVERY verdict and applies the tally atomically", t_verdict_counts_rounds),
    ("rounds-never-reset", "NOTHING resets review_rounds/ns_streak — there is no flag, at any door", t_review_rounds_never_reset),
    ("tally-floor-only", "`set` may VOID reviews_ok, NEVER raise it — only a verdict adds a verdict", t_set_cannot_raise_the_tally),
    ("verdict-head-pinned", "a verdict for a SUPERSEDED sha is refused — it describes content that is gone", t_verdict_refuses_a_moved_head),
    ("verdict-domain", "`verdict` needs a real row and one of exactly two verdicts", t_verdict_needs_a_row_and_a_known_verdict),
    ("counter-corrupt", "a counter field that is not a count is refused, never handed to int()", t_counter_refuses_a_corrupt_value),
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
    ("replay-the-record", "the REAL #42/#43 verdict sequences: it fires, never too early, and says what it costs", t_replay_the_real_record),
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
