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
    members <class>          list a class's members; --inherited walks bases
    tree <class>             base chain and direct subclasses
    doc-search <term>        substring search over docstrings
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SEARCH_CAP = 100

SymbolRow = tuple[int, str, str, str, str, str | None, str | None]


def default_db_path() -> Path:
    cache = Path.home() / ".cache" / "fusion-api-db" / "fusion-api.db"
    if cache.exists():
        return cache
    return Path(__file__).resolve().parent.parent / "data" / "fusion-api.db"


def open_db(arg: Path | None) -> sqlite3.Connection:
    path = arg if arg is not None else default_db_path()
    if not path.exists():
        print(
            f"error: database not found at {path}; run compile_fusion_api.py first",
            file=sys.stderr,
        )
        raise SystemExit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_info(conn: sqlite3.Connection, _args: argparse.Namespace) -> int:
    for row in conn.execute("SELECT key, value FROM meta ORDER BY key"):
        print(f"{row['key']}: {row['value']}")
    symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    print(f"symbols: {symbols}")
    print(f"members: {members}")
    return 0


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    pattern = f"%{args.term}%"
    hits: list[str] = []
    for row in conn.execute(
        "SELECT qualname, kind FROM symbols WHERE name LIKE ? ORDER BY qualname",
        (pattern,),
    ):
        hits.append(f"{row['qualname']}  [{row['kind']}]")
    for row in conn.execute(
        "SELECT s.qualname AS q, m.name AS n, m.kind AS k FROM members m"
        " JOIN symbols s ON s.id = m.symbol_id WHERE m.name LIKE ?"
        " ORDER BY s.qualname, m.name",
        (pattern,),
    ):
        hits.append(f"{row['q']}.{row['n']}  [{row['k']}]")
    if not hits:
        print(f"no matches for {args.term!r}")
        return 1
    for line in hits[:SEARCH_CAP]:
        print(line)
    if len(hits) > SEARCH_CAP:
        print(f"... {len(hits) - SEARCH_CAP} more (narrow the term)")
    return 0


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


def member_line(row: sqlite3.Row) -> str:
    name = row["name"]
    kind = row["kind"]
    if kind == "property":
        access = "read/write" if row["settable"] else "read-only"
        return f"  {name}: {row['returns'] or '?'}  [property, {access}]"
    if kind == "attribute":
        return f"  {name} = {row['value']}  [attribute]"
    return f"  {name}{row['signature'] or '()'}  [{kind}]"


def print_symbol(
    conn: sqlite3.Connection, sym: sqlite3.Row, with_members: bool
) -> None:
    print(f"{sym['qualname']}  [{sym['kind']}]")
    bases = json.loads(sym["bases"])
    if bases:
        print(f"bases: {', '.join(bases)}")
    if sym["signature"]:
        print(f"signature: {sym['signature']}")
    if sym["doc"]:
        print(sym["doc"])
    if with_members:
        rows = conn.execute(
            "SELECT * FROM members WHERE symbol_id = ? ORDER BY kind, name",
            (sym["id"],),
        ).fetchall()
        if rows:
            print(f"members ({len(rows)}):")
            for row in rows:
                print(member_line(row))


def print_member(owner: str, row: sqlite3.Row) -> None:
    print(f"{owner}.{row['name']}  [{row['kind']}]")
    if row["kind"] == "property":
        access = "read/write" if row["settable"] else "read-only"
        print(f"type: {row['returns'] or '?'}  ({access})")
    elif row["kind"] == "attribute":
        print(f"value: {row['value']}")
    elif row["signature"]:
        print(f"signature: {row['signature']}")
    if row["doc"]:
        print(row["doc"])


def find_members(
    conn: sqlite3.Connection, member: str, owner: str | None
) -> list[tuple[str, sqlite3.Row]]:
    if owner is not None:
        hits: list[tuple[str, sqlite3.Row]] = []
        for sym in find_symbol(conn, owner):
            for row in conn.execute(
                "SELECT * FROM members WHERE symbol_id = ? AND name = ? COLLATE NOCASE",
                (sym["id"], member),
            ):
                hits.append((sym["qualname"], row))
        return hits
    return [
        (row["q"], row)
        for row in conn.execute(
            "SELECT s.qualname AS q, m.* FROM members m"
            " JOIN symbols s ON s.id = m.symbol_id"
            " WHERE m.name = ? COLLATE NOCASE ORDER BY s.qualname",
            (member,),
        )
    ]


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    name: str = args.name
    symbols = find_symbol(conn, name)
    if len(symbols) == 1:
        print_symbol(conn, symbols[0], with_members=True)
        return 0
    if len(symbols) > 1:
        print(f"{name!r} is ambiguous:")
        for sym in symbols:
            print(f"  {sym['qualname']}")
        return 1
    owner: str | None = None
    member = name
    if "." in name:
        owner, member = name.rsplit(".", 1)
    hits = find_members(conn, member, owner)
    if not hits:
        print(f"no symbol or member named {name!r}; try the search subcommand")
        return 1
    if len(hits) > 1:
        print(f"{name!r} matches {len(hits)} members:")
        for qualname, row in hits[:SEARCH_CAP]:
            print(f"  {qualname}.{row['name']}  [{row['kind']}]")
        return 1
    print_member(hits[0][0], hits[0][1])
    return 0


def cmd_members(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    symbols = find_symbol(conn, args.name)
    if not symbols:
        print(f"no class named {args.name!r}; try the search subcommand")
        return 1
    if len(symbols) > 1:
        print(f"{args.name!r} is ambiguous:")
        for sym in symbols:
            print(f"  {sym['qualname']}")
        return 1
    sym = symbols[0]
    chain = [sym]
    if args.inherited:
        seen = {sym["qualname"]}
        frontier = json.loads(sym["bases"])
        while frontier:
            base_name = frontier.pop(0)
            if base_name in seen:
                continue
            seen.add(base_name)
            for base in find_symbol(conn, base_name):
                chain.append(base)
                frontier.extend(json.loads(base["bases"]))
    for owner in chain:
        rows = conn.execute(
            "SELECT * FROM members WHERE symbol_id = ? ORDER BY kind, name",
            (owner["id"],),
        ).fetchall()
        print(f"{owner['qualname']} ({len(rows)} members):")
        for row in rows:
            print(member_line(row))
    return 0


def cmd_tree(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    symbols = find_symbol(conn, args.name)
    if len(symbols) != 1:
        print(f"need exactly one class match for {args.name!r}; found {len(symbols)}")
        return 1
    sym = symbols[0]
    ancestors: list[str] = []
    frontier = json.loads(sym["bases"])
    seen: set[str] = set()
    while frontier:
        base_name = frontier.pop(0)
        if base_name in seen:
            continue
        seen.add(base_name)
        ancestors.append(base_name)
        for base in find_symbol(conn, base_name):
            frontier.extend(json.loads(base["bases"]))
    print(f"{sym['qualname']}")
    if ancestors:
        print("ancestors: " + " <- ".join(ancestors))
    subclasses = [
        row["qualname"]
        for row in conn.execute(
            "SELECT qualname FROM symbols WHERE bases LIKE ? ORDER BY qualname",
            (f'%"{sym["qualname"]}"%',),
        )
    ]
    if subclasses:
        print(f"direct subclasses ({len(subclasses)}):")
        for name in subclasses:
            print(f"  {name}")
    return 0


def cmd_doc_search(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    pattern = f"%{args.term}%"
    hits: list[str] = []
    for row in conn.execute(
        "SELECT qualname FROM symbols WHERE doc LIKE ? ORDER BY qualname", (pattern,)
    ):
        hits.append(row["qualname"])
    for row in conn.execute(
        "SELECT s.qualname AS q, m.name AS n FROM members m"
        " JOIN symbols s ON s.id = m.symbol_id WHERE m.doc LIKE ?"
        " ORDER BY s.qualname, m.name",
        (pattern,),
    ):
        hits.append(f"{row['q']}.{row['n']}")
    if not hits:
        print(f"no docstrings mention {args.term!r}")
        return 1
    for line in hits[:SEARCH_CAP]:
        print(line)
    if len(hits) > SEARCH_CAP:
        print(f"... {len(hits) - SEARCH_CAP} more (narrow the term)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="explicit database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="database provenance and counts")

    p_search = sub.add_parser("search", help="substring match over names")
    p_search.add_argument("term")

    p_show = sub.add_parser("show", help="full detail for a symbol or member")
    p_show.add_argument("name")

    p_members = sub.add_parser("members", help="list a class's members")
    p_members.add_argument("name")
    p_members.add_argument(
        "--inherited", action="store_true", help="include members from base classes"
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
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
