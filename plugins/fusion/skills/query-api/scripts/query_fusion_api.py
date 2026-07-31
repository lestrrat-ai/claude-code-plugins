#!/usr/bin/env python3
"""Query the compiled Fusion 360 Python API database.

Database resolution order (first hit wins):
1. ``--db <path>``
2. ``~/.cache/fusion-api-db/fusion-api.db`` (written by compile_fusion_api.py)
3. ``../data/fusion-api.db`` relative to this script (bundled with the plugin)

Subcommands:
    info                     database provenance and symbol counts
    search <term>            case-insensitive substring match over symbol and
                             member names
    show <name>              full detail for a class, function, or member;
                             accepts qualnames (adsk.core.Application), bare
                             class names, and Class.member forms
    members <class>          list a class's members; --own omits inherited ones
    tree <class>             base chain and direct subclasses
    doc-search <term>        substring search over docstrings

Member lookups resolve inherited members by default: ``show Class.member``,
``show Class``, and ``members Class`` all walk the class's bases and report
which class declares each member. A member declared closer to the queried class
hides a same-named member on a farther base. Member names are compared exactly,
so ``foo`` and ``Foo`` are two members and neither hides the other; a lookup
that matches no member exactly is retried ignoring case, which lets a
mistyped-case name still resolve without ever shadowing an exact match.

Stdlib only. Read-only: the database is opened with ``mode=ro``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn

SEARCH_CAP = 100

# LIKE's own wildcards, plus the character that escapes them. Every LIKE in this script says
# ESCAPE '\', so the two must stay together: see like_pattern().
LIKE_ESCAPE = "\\"


# Everything outside printable ASCII, newline, and tab is a candidate for escaping; the category
# test in escape_char() decides. Printable ASCII is never Cc or Cf, so skipping it changes nothing
# and keeps ordinary text off the per-character path entirely: a field with nothing to escape is
# returned as the same string object rather than rebuilt one character at a time.
ESCAPE_CANDIDATE = re.compile(r"[^\n\t\x20-\x7e]")


def escape_char(match: re.Match[str]) -> str:
    char = match.group()
    if unicodedata.category(char) not in ("Cc", "Cf"):
        return char
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def sanitize(text: str) -> str:
    """Escape control and format characters in text that came out of the database.

    Docstrings and signatures are copied verbatim from the reference repository, so anything that
    repository contains reaches this terminal. Newlines and tabs are kept because docstrings are
    laid out with them; every other control or format character is shown as an escape rather than
    executed by the terminal.

    compile_fusion_api.py carries the same function for the same reason. The two scripts are
    standalone by design (each skill runs its own), so the copies must be changed together.
    """
    return ESCAPE_CANDIDATE.sub(escape_char, text)


def out(text: str = "") -> None:
    """The only stdout write in this script; err() is the only stderr write.

    Between them nothing reaches the terminal unsanitized. Being the only stdout write would say
    nothing about stderr, which is why the error path has its own writer rather than its own
    `print`.
    """
    print(sanitize(text))


def err(text: str) -> None:
    """The only stderr write in this script, so nothing reaches stderr unsanitized.

    A database is an operator-supplied file and its own schema errors quote names out of it, so an
    error message carries text this script never chose, exactly as stdout does.
    """
    print(sanitize(text), file=sys.stderr)


def like_pattern(term: str) -> str:
    """Build a substring LIKE pattern in which `term` matches literally.

    Callers must pair this with ``ESCAPE '\\'`` in the SQL; without the escaping a term
    containing `_` or `%` would match far more than the user asked for.
    """
    escaped = term
    for char in (LIKE_ESCAPE, "%", "_"):
        escaped = escaped.replace(char, LIKE_ESCAPE + char)
    return f"%{escaped}%"


def default_db_path() -> Path:
    cache = Path.home() / ".cache" / "fusion-api-db" / "fusion-api.db"
    if cache.exists():
        return cache
    return Path(__file__).resolve().parent.parent / "data" / "fusion-api.db"


def rebuild_command(path: Path) -> str:
    """The command that produces a usable database at `path`."""
    compiler = (
        Path(__file__).resolve().parent.parent.parent
        / "compile-api"
        / "scripts"
        / "compile_fusion_api.py"
    )
    script = compiler if compiler.exists() else Path("compile_fusion_api.py")
    return f"python3 {script} --output {path}"


def fail(message: str) -> NoReturn:
    err(f"error: {message}")
    raise SystemExit(1)


def open_db(arg: Path | None) -> sqlite3.Connection:
    path = arg if arg is not None else default_db_path()
    if not path.exists():
        fail(f"database not found at {path}; rebuild it with: {rebuild_command(path)}")
    # as_uri() percent-encodes '?' and '#'. Interpolating the path into the URI instead lets
    # either character end the path component, which opens a different file and drops mode=ro.
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        fail(f"cannot open {path} ({exc}); rebuild it with: {rebuild_command(path)}")
    conn.row_factory = sqlite3.Row
    try:
        for probe in (
            "SELECT value FROM meta WHERE key = 'schema_version'",
            "SELECT id FROM symbols LIMIT 1",
            "SELECT id FROM members LIMIT 1",
        ):
            conn.execute(probe).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        fail(
            f"{path} is not a Fusion API database ({exc});"
            f" rebuild it with: {rebuild_command(path)}"
        )
    return conn


def cmd_info(conn: sqlite3.Connection, _args: argparse.Namespace) -> int:
    for row in conn.execute("SELECT key, value FROM meta ORDER BY key"):
        out(f"{row['key']}: {row['value']}")
    symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    out(f"symbols: {symbols}")
    out(f"members: {members}")
    return 0


class HitSource(NamedTuple):
    """One query behind a capped listing.

    `rows_sql` ends in ``LIMIT ?`` and is called with the pattern plus the number of rows still
    printable; `count_sql` counts the same matches without retrieving them.
    """

    rows_sql: str
    count_sql: str
    render: Callable[[sqlite3.Row], str]


def capped_listing(
    conn: sqlite3.Connection, pattern: str, sources: list[HitSource]
) -> tuple[list[str], int]:
    """Return up to SEARCH_CAP rendered lines, and how many rows matched in total.

    Only the rows that can be printed are ever retrieved, so the cost of a term matching the whole
    database is the cost of the lines it prints. The total is counted separately, and only when the
    cap was actually reached: below the cap every matching row was already fetched, so the lines
    are the total and no counting query is needed.
    """
    lines: list[str] = []
    for source in sources:
        remaining = SEARCH_CAP - len(lines)
        if remaining <= 0:
            break
        lines.extend(
            source.render(row)
            for row in conn.execute(source.rows_sql, (pattern, remaining))
        )
    if len(lines) < SEARCH_CAP:
        return lines, len(lines)
    total = 0
    for source in sources:
        total += conn.execute(source.count_sql, (pattern,)).fetchone()[0]
    return lines, total


def print_capped(lines: list[str], total: int, empty: str) -> int:
    """Print a capped listing and the omitted-result count; 1 when nothing matched."""
    if not lines:
        out(empty)
        return 1
    for line in lines:
        out(line)
    if total > len(lines):
        out(f"... {total - len(lines)} more (narrow the term)")
    return 0


SEARCH_SOURCES = [
    HitSource(
        "SELECT qualname, kind FROM symbols WHERE name LIKE ? ESCAPE '\\'"
        " ORDER BY qualname LIMIT ?",
        "SELECT COUNT(*) FROM symbols WHERE name LIKE ? ESCAPE '\\'",
        lambda row: f"{row['qualname']}  [{row['kind']}]",
    ),
    HitSource(
        "SELECT s.qualname AS q, m.name AS n, m.kind AS k FROM members m"
        " JOIN symbols s ON s.id = m.symbol_id WHERE m.name LIKE ? ESCAPE '\\'"
        " ORDER BY s.qualname, m.name LIMIT ?",
        "SELECT COUNT(*) FROM members m JOIN symbols s ON s.id = m.symbol_id"
        " WHERE m.name LIKE ? ESCAPE '\\'",
        lambda row: f"{row['q']}.{row['n']}  [{row['k']}]",
    ),
]


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    lines, total = capped_listing(conn, like_pattern(args.term), SEARCH_SOURCES)
    return print_capped(lines, total, f"no matches for {args.term!r}")


def find_symbol(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM symbols WHERE qualname = ? COLLATE NOCASE", (name,)
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        "SELECT * FROM symbols WHERE name = ? COLLATE NOCASE ORDER BY qualname",
        (name,),
    ).fetchall()


def resolve_class(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The one class named `name`, or None after printing why there is not exactly one."""
    symbols = find_symbol(conn, name)
    if len(symbols) > 1:
        out(f"{name!r} is ambiguous:")
        for sym in symbols:
            out(f"  {sym['qualname']}")
        return None
    if len(symbols) != 1 or symbols[0]["kind"] != "class":
        out(f"no class named {name!r}; try the search subcommand")
        return None
    return symbols[0]


def ancestry(conn: sqlite3.Connection, sym: sqlite3.Row) -> list[sqlite3.Row]:
    """`sym` followed by every ancestor reachable through `bases`, nearest first.

    Breadth-first, and each qualname is appended at most once, so a base listed twice and a cycle
    in the recorded bases both terminate the walk instead of repeating or looping.
    """
    chain = [sym]
    seen = {sym["qualname"]}
    frontier: list[str] = list(json.loads(sym["bases"]))
    while frontier:
        base_name = frontier.pop(0)
        for base in find_symbol(conn, base_name):
            qualname = base["qualname"]
            if qualname in seen:
                continue
            seen.add(qualname)
            chain.append(base)
            frontier.extend(json.loads(base["bases"]))
    return chain


def own_members(conn: sqlite3.Connection, symbol_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM members WHERE symbol_id = ? ORDER BY kind, name", (symbol_id,)
    ).fetchall()


def member_groups(
    conn: sqlite3.Connection, sym: sqlite3.Row
) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """(declaring class, its members) for `sym` and each ancestor, nearest first.

    A name declared closer to `sym` hides the same name on a farther ancestor, so an override is
    reported once, against the class that overrides it. Names are compared exactly: only the same
    name is an override, so a base's `foo` survives a subclass's `Foo` and both are listed.
    """
    groups: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
    claimed: set[str] = set()
    for owner in ancestry(conn, sym):
        rows = [
            row for row in own_members(conn, owner["id"]) if row["name"] not in claimed
        ]
        claimed.update(row["name"] for row in rows)
        groups.append((owner, rows))
    return groups


def member_line(row: sqlite3.Row) -> str:
    name = row["name"]
    kind = row["kind"]
    if kind == "property":
        access = "read/write" if row["settable"] else "read-only"
        return f"  {name}: {row['returns'] or '?'}  [property, {access}]"
    if kind == "attribute":
        # Either part may be absent, and both are absent for a name the compiler could not attach
        # a value to (one bound by an unpacked assignment), so each is rendered only if recorded.
        text = f"  {name}"
        if row["returns"] is not None:
            text += f": {row['returns']}"
        if row["value"] is not None:
            text += f" = {row['value']}"
        return f"{text}  [attribute]"
    return f"  {name}{row['signature'] or '()'}  [{kind}]"


def print_symbol(
    conn: sqlite3.Connection, sym: sqlite3.Row, with_members: bool
) -> None:
    out(f"{sym['qualname']}  [{sym['kind']}]")
    bases = json.loads(sym["bases"])
    if bases:
        out(f"bases: {', '.join(bases)}")
    if sym["signature"]:
        out(f"signature: {sym['signature']}")
    if sym["doc"]:
        out(sym["doc"])
    if not with_members or sym["kind"] != "class":
        return
    groups = member_groups(conn, sym)
    own = groups[0][1]
    if own:
        out(f"members ({len(own)}):")
        for row in own:
            out(member_line(row))
    for owner, rows in groups[1:]:
        if not rows:
            continue
        out(f"inherited from {owner['qualname']} ({len(rows)}):")
        for row in rows:
            out(member_line(row))


class MemberHit(NamedTuple):
    """One member found for a lookup.

    `lookup` is the class the query went through; `declared_on` is the class that declares the
    member. They differ when the member is inherited.
    """

    lookup: str
    declared_on: str
    row: sqlite3.Row


def print_member(hit: MemberHit) -> None:
    row = hit.row
    out(f"{hit.lookup}.{row['name']}  [{row['kind']}]")
    if hit.declared_on != hit.lookup:
        out(f"inherited from: {hit.declared_on}")
    if row["kind"] == "property":
        access = "read/write" if row["settable"] else "read-only"
        out(f"type: {row['returns'] or '?'}  ({access})")
    elif row["kind"] == "attribute":
        if row["returns"] is not None:
            out(f"type: {row['returns']}")
        if row["value"] is not None:
            out(f"value: {row['value']}")
    elif row["signature"]:
        out(f"signature: {row['signature']}")
    if row["doc"]:
        out(row["doc"])


def anywhere_named(
    conn: sqlite3.Connection, member: str, fold_case: bool
) -> list[MemberHit]:
    """Every member of that name, on any class."""
    sql = (
        "SELECT s.qualname AS q, m.* FROM members m JOIN symbols s ON s.id = m.symbol_id"
        " WHERE m.name = ? COLLATE NOCASE ORDER BY s.qualname"
        if fold_case
        else "SELECT s.qualname AS q, m.* FROM members m JOIN symbols s ON s.id = m.symbol_id"
        " WHERE m.name = ? ORDER BY s.qualname"
    )
    return [MemberHit(row["q"], row["q"], row) for row in conn.execute(sql, (member,))]


def owned_named(
    conn: sqlite3.Connection, member: str, owner: str, fold_case: bool
) -> list[MemberHit]:
    """Every member of that name reachable from a class named `owner`, nearest declaration only."""
    sql = (
        "SELECT * FROM members WHERE symbol_id = ? AND name = ? COLLATE NOCASE"
        if fold_case
        else "SELECT * FROM members WHERE symbol_id = ? AND name = ?"
    )
    hits: list[MemberHit] = []
    seen_rows: set[int] = set()
    for sym in find_symbol(conn, owner):
        for declaring in ancestry(conn, sym):
            rows = conn.execute(sql, (declaring["id"], member)).fetchall()
            if not rows:
                continue
            for row in rows:
                if row["id"] in seen_rows:
                    continue
                seen_rows.add(row["id"])
                hits.append(MemberHit(sym["qualname"], declaring["qualname"], row))
            break  # nearest declaration wins; stop climbing this chain
    return hits


def find_members(
    conn: sqlite3.Connection, member: str, owner: str | None
) -> list[MemberHit]:
    """Every member named `member`, optionally restricted to the class named `owner`.

    With an owner, each candidate class is searched together with its ancestors and the nearest
    declaration wins, so an inherited member resolves exactly like an own one.

    The exact-name search runs over the whole chain first, and case is folded only when it found
    nothing at all. So a differently-cased member on a nearer class can never stop the exactly
    named one further up the chain from being found, while a case-folded query still resolves when
    no member carries the queried spelling.
    """
    if owner is None:
        return anywhere_named(conn, member, fold_case=False) or anywhere_named(
            conn, member, fold_case=True
        )
    return owned_named(conn, member, owner, fold_case=False) or owned_named(
        conn, member, owner, fold_case=True
    )


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    name: str = args.name
    symbols = find_symbol(conn, name)
    if len(symbols) == 1:
        print_symbol(conn, symbols[0], with_members=True)
        return 0
    if len(symbols) > 1:
        out(f"{name!r} is ambiguous:")
        for sym in symbols:
            out(f"  {sym['qualname']}")
        return 1
    owner: str | None = None
    member = name
    if "." in name:
        owner, member = name.rsplit(".", 1)
    hits = find_members(conn, member, owner)
    if not hits:
        out(f"no symbol or member named {name!r}; try the search subcommand")
        return 1
    if len(hits) > 1:
        out(f"{name!r} matches {len(hits)} members:")
        for hit in hits[:SEARCH_CAP]:
            out(f"  {hit.declared_on}.{hit.row['name']}  [{hit.row['kind']}]")
        return 1
    print_member(hits[0])
    return 0


def cmd_members(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    sym = resolve_class(conn, args.name)
    if sym is None:
        return 1
    if args.own:
        groups = [(sym, own_members(conn, sym["id"]))]
    else:
        groups = member_groups(conn, sym)
    for owner, rows in groups:
        out(f"{owner['qualname']} ({len(rows)} members):")
        for row in rows:
            out(member_line(row))
    return 0


def cmd_tree(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    sym = resolve_class(conn, args.name)
    if sym is None:
        return 1
    # Walks recorded base NAMES rather than ancestry()'s rows, so a base the database does not
    # hold (a class from outside the adsk package) still appears in the chain. Each name is
    # visited once, so a repeated base or a cycle terminates the walk.
    ancestors: list[str] = []
    frontier: list[str] = list(json.loads(sym["bases"]))
    seen: set[str] = {sym["qualname"]}
    while frontier:
        base_name = frontier.pop(0)
        if base_name in seen:
            continue
        seen.add(base_name)
        ancestors.append(base_name)
        for base in find_symbol(conn, base_name):
            frontier.extend(json.loads(base["bases"]))
    out(f"{sym['qualname']}")
    if ancestors:
        out("ancestors: " + " <- ".join(ancestors))
    subclasses = [
        row["qualname"]
        for row in conn.execute(
            "SELECT qualname FROM symbols WHERE bases LIKE ? ESCAPE '\\'"
            " ORDER BY qualname",
            (like_pattern(f'"{sym["qualname"]}"'),),
        )
    ]
    if subclasses:
        out(f"direct subclasses ({len(subclasses)}):")
        for name in subclasses:
            out(f"  {name}")
    return 0


DOC_SEARCH_SOURCES = [
    HitSource(
        "SELECT qualname FROM symbols WHERE doc LIKE ? ESCAPE '\\'"
        " ORDER BY qualname LIMIT ?",
        "SELECT COUNT(*) FROM symbols WHERE doc LIKE ? ESCAPE '\\'",
        lambda row: row["qualname"],
    ),
    HitSource(
        "SELECT s.qualname AS q, m.name AS n FROM members m"
        " JOIN symbols s ON s.id = m.symbol_id WHERE m.doc LIKE ? ESCAPE '\\'"
        " ORDER BY s.qualname, m.name LIMIT ?",
        "SELECT COUNT(*) FROM members m JOIN symbols s ON s.id = m.symbol_id"
        " WHERE m.doc LIKE ? ESCAPE '\\'",
        lambda row: f"{row['q']}.{row['n']}",
    ),
]


def cmd_doc_search(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    lines, total = capped_listing(conn, like_pattern(args.term), DOC_SEARCH_SOURCES)
    return print_capped(lines, total, f"no docstrings mention {args.term!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="explicit database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="database provenance and counts")

    p_search = sub.add_parser("search", help="substring match over names")
    p_search.add_argument("term")

    p_show = sub.add_parser("show", help="full detail for a symbol or member")
    p_show.add_argument("name")

    p_members = sub.add_parser(
        "members", help="list a class's members, inherited ones included"
    )
    p_members.add_argument("name")
    p_members.add_argument(
        "--own",
        action="store_true",
        help="list only members the class itself declares",
    )

    p_tree = sub.add_parser("tree", help="base chain and direct subclasses")
    p_tree.add_argument("name")

    p_doc = sub.add_parser("doc-search", help="substring search over docstrings")
    p_doc.add_argument("term")

    args = parser.parse_args()
    conn = open_db(args.db)
    handlers = {
        "info": cmd_info,
        "search": cmd_search,
        "show": cmd_show,
        "members": cmd_members,
        "tree": cmd_tree,
        "doc-search": cmd_doc_search,
    }
    try:
        return handlers[args.command](conn, args)
    except sqlite3.Error as exc:
        db_path = args.db if args.db is not None else default_db_path()
        err(
            f"error: {db_path} could not answer {args.command!r} ({exc});"
            f" rebuild it with: {rebuild_command(db_path)}"
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
