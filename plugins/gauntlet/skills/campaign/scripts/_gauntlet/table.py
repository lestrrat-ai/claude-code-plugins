"""Shared rendering for human-readable Gauntlet state tables."""

from __future__ import annotations


def _hex_escape(ch: str) -> str:
    """``\\xNN`` for a byte-sized code point, ``\\uNNNN`` above it."""
    return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"


def escape_cell(value: str) -> str:
    r"""Make a value safe to render inside the grid — no value may forge the layout, and no two
    values may render THE SAME.

    A field value is free text, so it can contain the very characters the table's layout is built from.
    Rendered raw, a value carrying a ``|`` fabricates extra columns, a newline fabricates extra ROWS, and
    a leading ``#`` mimics the out-of-band lines printed around the grid. The table would then say
    something its store never did.

    ESCAPING, not quoting: quoting every cell would widen every column by two chars for the common
    (harmless) case and STILL need an escape for an embedded quote. Backslash escapes leave every ordinary
    value byte-identical and give the invariant outright: the returned string contains no BARE ``|``, no
    line break, no control character, and never starts with ``#``. Each escape is unambiguous because a
    literal backslash is doubled. The display stays reversible by eye, but it is still a DISPLAY form;
    callers read stored values through their schema-owning accessor, never by parsing the table.

    WHITESPACE IS ESCAPED AT THE EDGES. The layout pads each cell with ``ljust()`` and then ``rstrip()``s
    the line, and BOTH eat trailing whitespace: left raw, ``""`` and ``"   "`` print the same blank cell,
    while ``"a"`` and ``"a "`` both print ``a``. A leading or trailing whitespace run is escaped and
    survives display. Interior spaces stay unchanged so ordinary prose remains readable.

    Whitespace that is NOT a plain space is escaped WHEREVER it appears: it is invisible or line-break-ish,
    and ``str.rstrip()`` eats it. The escaped text therefore never starts or ends with whitespace and holds
    no whitespace but a plain interior space. Callers MUST escape BEFORE computing column widths, so the
    widths measure what is actually printed. Display-only truncation must happen on the raw value first.
    """
    # Use the same whitespace definition as the layout's `rstrip()`, so every character the layout would
    # otherwise eat receives an escape spelling first.
    lead = len(value) - len(value.lstrip())
    trail = len(value.rstrip())
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
            out.append(_hex_escape(ch))  # NBSP, NEL, U+2028, ideographic space, and other invisible space
        elif ch == " " and (i < lead or i >= trail):
            out.append("\\x20")  # leading or trailing space: grid padding would swallow it
        else:
            out.append(ch)
    text = "".join(out)
    # One value has one rendering regardless of which column it occupies. Only a first-column cell can
    # open a line, but escape a leading `#` in every cell rather than making position part of the encoding.
    if text.startswith("#"):
        text = "\\" + text
    return text


def hidden_notice(n: int, hidden: tuple[str, ...]) -> str:
    """The line that makes a filtered table's omitted rows explicit.

    A filtered view that does not say what it hid is a lie by omission. The count and the flag that reveals
    every row are always present. The wording is derived from the caller's hidden-state set, so a store
    cannot change that set while leaving a stale description beside it.
    """
    what = "/".join(hidden)
    return f"# {n} {what} row{'' if n == 1 else 's'} hidden — pass --all to show every row"


def config_lines(pairs: list[tuple[str, str]]) -> list[str]:
    """The escaped ``# <name>: <value>`` block printed above a grid.

    The ``#`` prefix and the blank line callers print after the block keep these lines from reading as
    table columns. Values are free text, so they are escaped on the same terms as cells: an unescaped
    newline here would inject lines that look like part of the grid.
    """
    return [f"# {name}: {escape_cell(value)}" for name, value in pairs]


def grid_lines(fields: tuple[str, ...], rows: list[list[str]]) -> list[str]:
    """Render raw values as an aligned grid: column header, rule, then one line per row.

    The layout is owned here once for every table the campaign scripts print. A second copy of the
    separator, width arithmetic, and ``rstrip()`` would be a second definition of the syntax
    ``escape_cell()`` protects.

    Cells arrive RAW and are escaped here, so a caller cannot render an unescaped value by forgetting to.
    Widths are measured on the escaped text. Display-only truncation is the caller's job and must happen on
    the raw value before it is handed over, so a cut cannot land inside an escape sequence.
    """
    cells = [tuple(escape_cell(value) for value in row) for row in rows]
    widths = [
        max(len(field), *(len(cell[i]) for cell in cells)) if cells else len(field)
        for i, field in enumerate(fields)
    ]
    out = [
        " | ".join(field.ljust(width) for field, width in zip(fields, widths)).rstrip(),
        "-+-".join("-" * width for width in widths),
    ]
    out += [
        " | ".join(value.ljust(width) for value, width in zip(cell, widths)).rstrip()
        for cell in cells
    ]
    return out
