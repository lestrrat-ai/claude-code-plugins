#!/usr/bin/env python3
"""Query the compiled Fusion 360 Python API database.

Database resolution order (first hit wins):
1. ``--db <path>``
2. ``~/.cache/fusion-api-db/fusion-api.db`` (written by compile_fusion_api.py)
3. ``../data/fusion-api.db`` relative to this script (bundled with the plugin)

``--db`` is an option of this script rather than of a subcommand, so it goes
*before* the subcommand::

    query_fusion_api.py --db <path> show adsk.core.Application

Placing it after the subcommand is a usage error and exits 2.

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
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, NamedTuple, NoReturn

if TYPE_CHECKING:  # the stream type argparse hands _print_message; no runtime import
    from _typeshed import SupportsWrite

# How many lines any one listing prints before it stops and states the remainder through
# omitted_line(). It governs `search`, `doc-search`, and `show`'s candidate list. A listing that
# prints every row it counted — `members` and `tree`'s subclasses — is not capped and does not use
# this: those two answer "what does this class have", where a partial answer is the wrong answer.
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
    """Write sanitized text to stdout; err() is the stderr half of the same pair.

    Every line this script prints goes through one of the two, argparse's usage, help, and error
    text included (SanitizedParser routes it here). What stays outside the pair is what the
    interpreter itself writes: an unhandled exception's traceback is Python's own output and is not
    sanitized. Being the only stdout writer would say nothing about stderr, which is why the error
    path has its own writer rather than its own `print`.
    """
    print(sanitize(text))


def err(text: str) -> None:
    """Write sanitized text to stderr; see out() for what the pair does and does not cover.

    A database is an operator-supplied file and its own schema errors quote names out of it, so an
    error message carries text this script never chose, exactly as stdout does. argv is
    operator-supplied in the same way, and argparse quotes it back on this stream.
    """
    print(sanitize(text), file=sys.stderr)


class SanitizedParser(argparse.ArgumentParser):
    """An argument parser whose own usage, help, and error text goes through out() and err().

    argparse writes to the two streams itself, and it echoes argv back: an unrecognized option or an
    invalid subcommand name is quoted verbatim into the message. That text is operator-supplied and
    needs the same escaping database text gets. Every argparse write funnels through
    ``_print_message``, so overriding that one method covers usage, ``--help``, and every error path
    at once.

    ``add_subparsers()`` builds each subcommand parser from ``type(self)``, so the subcommand
    parsers inherit this behavior without being named again.

    compile_fusion_api.py carries the same class for the same reason. The two scripts are standalone
    by design (each skill runs its own), so the copies must be changed together.
    """

    def _print_message(
        self, message: str, file: SupportsWrite[str] | None = None
    ) -> None:
        if not message:
            return
        # argparse's messages already end in a newline and print() adds one, so strip it: the
        # output stays what argparse would have written, only sanitized. argparse defaults a
        # message with no file to stderr, and so does this.
        writer = out if file is sys.stdout else err
        writer(message.rstrip("\n"))


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


def omitted_line(total: int, shown: int) -> str:
    """The one line that reports what a listing counted but did not print.

    Every listing in this script that prints fewer lines than the total it reports emits this line
    and nothing else, so a reader that sees a count larger than the lines it got is always told the
    difference the same way. Its callers are the only places where the two numbers can disagree.
    """
    return f"... {total - shown} more (narrow the term)"


def print_capped(lines: list[str], total: int, empty: str) -> int:
    """Print a capped listing and the omitted-result count; 1 when nothing matched."""
    if not lines:
        out(empty)
        return 1
    for line in lines:
        out(line)
    if total > len(lines):
        out(omitted_line(total, len(lines)))
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

    Breadth-first over a deque, so taking the next name off the frontier costs the same however
    long the frontier is. Each base name is looked up at most once and each qualname is appended at
    most once, so a class naming one base a hundred thousand times costs one lookup and one visit:
    a repeated base and a cycle in the recorded bases both terminate the walk instead of repeating
    or looping. Two spellings of one name (lookups fold case) still resolve to one visit, because
    the qualname decides what is already in the chain.
    """
    chain = [sym]
    seen = {sym["qualname"]}
    frontier: deque[str] = deque(json.loads(sym["bases"]))
    queried: set[str] = set()
    while frontier:
        base_name = frontier.popleft()
        if base_name in queried:
            continue
        queried.add(base_name)
        for base in find_symbol(conn, base_name):
            qualname = base["qualname"]
            if qualname in seen:
                continue
            seen.add(qualname)
            chain.append(base)
            frontier.extend(json.loads(base["bases"]))
    return chain


def own_members(conn: sqlite3.Connection, symbol_id: int) -> Iterator[sqlite3.Row]:
    """Stream a class's own member rows in listing order.

    A cursor rather than a list: `members` reports every member a class has, by design, so the rows
    are handed on one at a time and what a large listing costs is what printing it costs, not what
    holding all of it at once would.
    """
    return conn.execute(
        "SELECT * FROM members WHERE symbol_id = ? ORDER BY kind, name", (symbol_id,)
    )


def own_member_count(conn: sqlite3.Connection, symbol_id: int) -> int:
    """How many members a class declares, counted without retrieving them."""
    return conn.execute(
        "SELECT COUNT(*) FROM members WHERE symbol_id = ?", (symbol_id,)
    ).fetchone()[0]


class MemberPlan(NamedTuple):
    """Where each member name reachable from a queried class is declared.

    `owners` is the queried class and its ancestors, nearest first. `declared_by` maps a member name
    to the qualname of the nearest owner that declares it, and `counts` gives each owner how many
    names it declares that nothing nearer hides. A listing is printed by streaming each owner's rows
    against this plan, so no set of rows is ever held whole; what is held is the names, which the
    hiding rule needs however the listing is produced.
    """

    owners: list[sqlite3.Row]
    declared_by: dict[str, str]
    counts: dict[str, int]

    def rows_of(
        self, conn: sqlite3.Connection, owner: sqlite3.Row
    ) -> Iterator[sqlite3.Row]:
        """Stream the members of `owner` that nothing nearer the queried class hides."""
        qualname = owner["qualname"]
        for row in own_members(conn, owner["id"]):
            if self.declared_by.get(row["name"]) == qualname:
                yield row


def member_plan(conn: sqlite3.Connection, sym: sqlite3.Row) -> MemberPlan:
    """Resolve the hiding rule for `sym` and its ancestors, reading names and no other column.

    A name declared closer to `sym` hides the same name on a farther ancestor, so an override is
    reported once, against the class that overrides it. Names are compared exactly: only the same
    name is an override, so a base's `foo` survives a subclass's `Foo` and both are listed. One
    class declaring one name twice keeps both rows, because a class cannot hide itself: an owner's
    claims are recorded only once the whole owner has been read.
    """
    owners = ancestry(conn, sym)
    declared_by: dict[str, str] = {}
    counts: dict[str, int] = {}
    for owner in owners:
        qualname = owner["qualname"]
        mine = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM members WHERE symbol_id = ?", (owner["id"],)
            )
            if row["name"] not in declared_by
        ]
        counts[qualname] = len(mine)
        declared_by.update((name, qualname) for name in mine)
    return MemberPlan(owners, declared_by, counts)


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
    plan = member_plan(conn, sym)
    own_count = plan.counts[sym["qualname"]]
    if own_count:
        out(f"members ({own_count}):")
        for row in plan.rows_of(conn, plan.owners[0]):
            out(member_line(row))
    for owner in plan.owners[1:]:
        count = plan.counts[owner["qualname"]]
        if not count:
            continue
        out(f"inherited from {owner['qualname']} ({count}):")
        for row in plan.rows_of(conn, owner):
            out(member_line(row))


class MemberHit(NamedTuple):
    """One member found for a lookup.

    `lookup` is the class the query went through; `declared_on` is the class that declares the
    member. They differ when the member is inherited.
    """

    lookup: str
    declared_on: str
    row: sqlite3.Row


def candidate_line(hit: MemberHit) -> str:
    """One line of a candidate list, naming the lookup that reaches the member.

    The lookup is what a re-run has to be handed, and two same-named classes inheriting one member
    differ only there, so a line built from the declaring class alone would print twice over and
    leave the reader nothing to choose between. Where the two agree the line says so by staying
    silent, which is every lookup that names no owning class.
    """
    text = f"  {hit.lookup}.{hit.row['name']}  [{hit.row['kind']}]"
    if hit.declared_on != hit.lookup:
        text += f"  (inherited from {hit.declared_on})"
    return text


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
    """Every member of that name reachable from a class named `owner`, nearest declaration only.

    One hit per class named `owner`, never per member row. Two classes of the same name in
    different modules that inherit one member share the row that declares it, and collapsing those
    to a single hit would turn an ambiguous lookup into one arbitrary answer at exit 0. They are two
    answers, so both are returned and the caller lists them. Within one candidate class a row cannot
    repeat: the walk visits each ancestor once and stops at the first that declares the name.
    """
    sql = (
        "SELECT * FROM members WHERE symbol_id = ? AND name = ? COLLATE NOCASE"
        if fold_case
        else "SELECT * FROM members WHERE symbol_id = ? AND name = ?"
    )
    hits: list[MemberHit] = []
    for sym in find_symbol(conn, owner):
        for declaring in ancestry(conn, sym):
            rows = conn.execute(sql, (declaring["id"], member)).fetchall()
            if not rows:
                continue
            hits.extend(
                MemberHit(sym["qualname"], declaring["qualname"], row) for row in rows
            )
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
            out(candidate_line(hit))
        if len(hits) > SEARCH_CAP:
            out(omitted_line(len(hits), SEARCH_CAP))
        return 1
    print_member(hits[0])
    return 0


def cmd_members(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    sym = resolve_class(conn, args.name)
    if sym is None:
        return 1
    if args.own:
        out(f"{sym['qualname']} ({own_member_count(conn, sym['id'])} members):")
        for row in own_members(conn, sym["id"]):
            out(member_line(row))
        return 0
    plan = member_plan(conn, sym)
    for owner in plan.owners:
        out(f"{owner['qualname']} ({plan.counts[owner['qualname']]} members):")
        for row in plan.rows_of(conn, owner):
            out(member_line(row))
    return 0


def cmd_tree(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    sym = resolve_class(conn, args.name)
    if sym is None:
        return 1
    # Walks recorded base NAMES rather than ancestry()'s rows, so a base the database does not
    # hold (a class from outside the adsk package) still appears in the chain. Each name is
    # visited once and taken off a deque, so a repeated base or a cycle terminates the walk and a
    # long frontier costs no more per step than a short one.
    ancestors: list[str] = []
    frontier: deque[str] = deque(json.loads(sym["bases"]))
    seen: set[str] = {sym["qualname"]}
    while frontier:
        base_name = frontier.popleft()
        if base_name in seen:
            continue
        seen.add(base_name)
        ancestors.append(base_name)
        for base in find_symbol(conn, base_name):
            frontier.extend(json.loads(base["bases"]))
    out(f"{sym['qualname']}")
    if ancestors:
        out("ancestors: " + " <- ".join(ancestors))
    # Counted first and then streamed, the way a member listing is: every subclass is printed, and
    # the header needs the number before the first one, but neither needs the rows held at once.
    pattern = like_pattern(f'"{sym["qualname"]}"')
    subclasses = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE bases LIKE ? ESCAPE '\\'", (pattern,)
    ).fetchone()[0]
    if subclasses:
        out(f"direct subclasses ({subclasses}):")
        for row in conn.execute(
            "SELECT qualname FROM symbols WHERE bases LIKE ? ESCAPE '\\'"
            " ORDER BY qualname",
            (pattern,),
        ):
            out(f"  {row['qualname']}")
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
    parser = SanitizedParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, help="explicit database path (goes before the subcommand)"
    )
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
