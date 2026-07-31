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
                             class names, and Class.member forms. A name that
                             reaches more than one of them — including a name
                             that is both a class and another class's member —
                             lists every candidate instead of picking one
    members <class>          list a class's members; --own omits inherited ones
    tree <class>             base chain and direct subclasses
    doc-search <term>        substring search over docstrings

Member lookups resolve inherited members by default: ``show Class.member``,
``show Class``, and ``members Class`` all walk the class's bases and report
which class declares each member. The bases are walked in the order Python
itself resolves an attribute (C3 linearization, what ``type.__mro__`` gives),
so the class named is the class the member really comes from; a class whose
recorded bases have no such order is refused rather than answered with a guess.
A member declared earlier in that order hides a same-named member declared
later. Member names are compared exactly,
so ``foo`` and ``Foo`` are two members and neither hides the other; a lookup
that resolved to nothing — no member of that exact name and no symbol either —
is retried ignoring case, which lets a mistyped-case name still resolve without
ever shadowing an exact match.

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


class InconsistentHierarchy(Exception):
    """A class whose recorded bases have no resolution order at all.

    Raised instead of answering, because every answer available at that point is a guess: with no
    order there is no fact about which base declares the member a lookup would reach, and a guess
    is indistinguishable from the real answer in the output.
    """


def base_rows(conn: sqlite3.Connection, sym: sqlite3.Row) -> list[sqlite3.Row]:
    """The classes `sym` names as bases, in the order it names them, each appearing once.

    Each distinct name is looked up once however many times it is listed, and a class already
    reached through an earlier name is not repeated, so a class naming one base a hundred thousand
    times costs one lookup and contributes one base. Two spellings of one name (lookups fold case)
    collapse the same way, because the qualname decides what is already there. A name the database
    does not hold contributes nothing: a base outside the adsk package has no members to inherit.
    """
    rows: list[sqlite3.Row] = []
    queried: set[str] = set()
    seen: set[str] = set()
    for name in json.loads(sym["bases"]):
        if name in queried:
            continue
        queried.add(name)
        for base in find_symbol(conn, name):
            qualname = base["qualname"]
            if qualname in seen:
                continue
            seen.add(qualname)
            rows.append(base)
    return rows


def c3_merge(sequences: list[list[sqlite3.Row]], owner: str) -> list[sqlite3.Row]:
    """Merge already-linearized base orders the way C3 does, or refuse.

    A class may be taken next only when it is the head of every sequence it appears in; taking one
    that appears in some sequence's tail would place it before a class the tail says comes first.
    When no head qualifies, the recorded bases contradict each other and there is no order to
    return — `owner` is named in the refusal because it is the class whose base list is the problem.
    """
    pending = [list(sequence) for sequence in sequences if sequence]
    order: list[sqlite3.Row] = []
    while pending:
        head: sqlite3.Row | None = None
        for sequence in pending:
            candidate = sequence[0]["qualname"]
            if not any(
                candidate in {row["qualname"] for row in other[1:]} for other in pending
            ):
                head = sequence[0]
                break
        if head is None:
            blocked = dict.fromkeys(remaining[0]["qualname"] for remaining in pending)
            raise InconsistentHierarchy(
                f"{owner} has no consistent resolution order: its recorded bases"
                f" disagree about which of {', '.join(blocked)} comes first, so which"
                " class declares an inherited member is not decidable"
            )
        order.append(head)
        for sequence in pending:
            if sequence[0]["qualname"] == head["qualname"]:
                del sequence[0]
        pending = [sequence for sequence in pending if sequence]
    return order


def ancestry(conn: sqlite3.Connection, sym: sqlite3.Row) -> list[sqlite3.Row]:
    """`sym` followed by every ancestor, in the order Python itself resolves an attribute.

    C3 linearization, the same rule `type.__mro__` follows, and not breadth-first order: the two
    disagree the moment a class has more than one base. On a diamond where `D(B, C)`, `B(A)`, and
    both `A` and `C` declare one name, breadth-first reaches `C` first while Python resolves the
    name on `A`, so a breadth-first answer names a class that does not declare what the class
    actually inherits. This walk is the one place the order is decided; every member lookup reads
    it, so `show`, `members`, and the hiding rule all answer with Python's own resolution.

    Iterative rather than recursive, so a deep chain costs heap and not stack. A base already on
    the path being walked is a cycle: no class can inherit from something that inherits from it, so
    there is no order to report and the walk refuses instead of cutting the loop at an arbitrary
    point. A repeated base is not a cycle and does not refuse; base_rows() has already reduced it
    to one entry.
    """
    order: dict[str, list[sqlite3.Row]] = {}
    bases: dict[str, list[sqlite3.Row]] = {sym["qualname"]: base_rows(conn, sym)}
    stack: list[tuple[sqlite3.Row, int]] = [(sym, 0)]
    on_path: set[str] = {sym["qualname"]}
    while stack:
        node, index = stack[-1]
        qualname = node["qualname"]
        node_bases = bases[qualname]
        if index < len(node_bases):
            stack[-1] = (node, index + 1)
            base = node_bases[index]
            base_qualname = base["qualname"]
            if base_qualname in order:
                continue  # already linearized on another branch of the same hierarchy
            if base_qualname in on_path:
                raise InconsistentHierarchy(
                    f"{sym['qualname']} has no consistent resolution order:"
                    f" {qualname} names {base_qualname} as a base, and {base_qualname}"
                    f" reaches {qualname} again through its own bases"
                )
            on_path.add(base_qualname)
            bases[base_qualname] = base_rows(conn, base)
            stack.append((base, 0))
            continue
        stack.pop()
        on_path.discard(qualname)
        # Every base is linearized by now, so this class can be: C3 puts the class first, then
        # merges its bases' orders with the base list itself, which is what keeps the bases in the
        # order the class declared them.
        order[qualname] = [node] + c3_merge(
            [list(order[base["qualname"]]) for base in node_bases] + [list(node_bases)],
            qualname,
        )
    return order[sym["qualname"]]


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

    `owners` is the queried class and its ancestors in resolution order (see ancestry()).
    `declared_by` maps a member name to the qualname of the first owner in that order that declares
    it, and `counts` gives each owner how many names it declares that nothing earlier hides. A
    listing is printed by streaming each owner's rows against this plan, so no set of rows is ever
    held whole; what is held is the names, which the hiding rule needs however the listing is
    produced.
    """

    owners: list[sqlite3.Row]
    declared_by: dict[str, str]
    counts: dict[str, int]

    def rows_of(
        self, conn: sqlite3.Connection, owner: sqlite3.Row
    ) -> Iterator[sqlite3.Row]:
        """Stream the members of `owner` that no earlier class in the resolution order hides."""
        qualname = owner["qualname"]
        for row in own_members(conn, owner["id"]):
            if self.declared_by.get(row["name"]) == qualname:
                yield row


def member_plan(conn: sqlite3.Connection, sym: sqlite3.Row) -> MemberPlan:
    """Resolve the hiding rule for `sym` and its ancestors, reading names and no other column.

    A name declared earlier in the resolution order hides the same name on a class later in it, so
    an override is reported once, against the class that overrides it. Names are compared exactly:
    only the same name is an override, so a base's `foo` survives a subclass's `Foo` and both are
    listed. One class declaring one name twice keeps both rows, because a class cannot hide itself:
    an owner's claims are recorded only once the whole owner has been read.
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
    """Every member of that name reachable from a class named `owner`, first declaration only.

    "First" is first in the resolution order (see ancestry()), which is what Python itself would
    reach, so the class this reports as the declaring one is the class the attribute really comes
    from.

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
            break  # first declaration in resolution order wins; stop walking this chain
    return hits


def find_members(
    conn: sqlite3.Connection,
    member: str,
    owner: str | None,
    retry_folded: bool = True,
) -> list[MemberHit]:
    """Every member named `member`, optionally restricted to the class named `owner`.

    With an owner, each candidate class is searched together with its ancestors and the first
    declaration in the resolution order wins, so an inherited member resolves exactly like an own
    one.

    The exact-name search runs over the whole chain first, and case is folded only when it found
    nothing at all. So a differently-cased member on a class earlier in the order can never stop
    the exactly named one later in it from being found, while a case-folded query still resolves
    when no member carries the queried spelling.

    `retry_folded=False` drops that second pass. A caller that has already resolved the name some
    other way passes it: the retry exists for a name nothing matched, so letting it run anyway
    would answer a resolved lookup with members that merely share the spelling in another case.
    """
    if owner is None:
        return anywhere_named(conn, member, fold_case=False) or (
            anywhere_named(conn, member, fold_case=True) if retry_folded else []
        )
    return owned_named(conn, member, owner, fold_case=False) or (
        owned_named(conn, member, owner, fold_case=True) if retry_folded else []
    )


def ambiguity_header(name: str, symbols: int, members: int) -> str:
    """The first line of `show`'s candidate list, naming which kinds of candidate follow.

    There are three shapes because there are three ways one name reaches more than one answer:
    several symbols, several members, or a symbol and a member at once.
    """
    if not members:
        return f"{name!r} is ambiguous:"
    if not symbols:
        return f"{name!r} matches {members} members:"
    return f"{name!r} matches {symbols} symbol(s) and {members} member(s):"


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Print the one thing `name` reaches, or every candidate when it reaches more than one.

    A name can be a class AND a member of some other class at the same time — the bundled database
    has `Features` as a class in `adsk.fusion` and as a member of two `adsk.core` classes — so both
    lookups run and their results answer the same question. Returning the class alone at exit 0
    would be one answer to a question with three, and the reader would never learn the rest exist.

    The member lookup's case-folded retry is suppressed once a symbol has matched: that retry is
    for a name nothing matched at all, and letting it compete here would turn every class whose
    name some member spells differently — several hundred of them — into a candidate list.
    """
    name: str = args.name
    symbols = find_symbol(conn, name)
    owner: str | None = None
    member = name
    if "." in name:
        owner, member = name.rsplit(".", 1)
    hits = find_members(conn, member, owner, retry_folded=not symbols)
    total = len(symbols) + len(hits)
    if total == 0:
        out(f"no symbol or member named {name!r}; try the search subcommand")
        return 1
    if total == 1:
        if symbols:
            print_symbol(conn, symbols[0], with_members=True)
        else:
            print_member(hits[0])
        return 0
    out(ambiguity_header(name, len(symbols), len(hits)))
    # One cap over the whole candidate list, and one omitted-result line accounting for both parts
    # of it: two caps would let a list print more than SEARCH_CAP lines, and two counts would each
    # be true of half a list nobody asked for in halves.
    shown = 0
    for sym in symbols[:SEARCH_CAP]:
        out(f"  {sym['qualname']}")
        shown += 1
    for hit in hits[: SEARCH_CAP - shown]:
        out(candidate_line(hit))
        shown += 1
    if total > shown:
        out(omitted_line(total, shown))
    return 1


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
    # This is what a class can REACH, deliberately not the order a member lookup resolves in: a
    # name with no row cannot be linearized, and reporting it is the whole point of this listing.
    # ancestry() owns the resolution order, and the `ancestors:` line below never claims to be it.
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
    except InconsistentHierarchy as exc:
        # A real failure, not a lookup miss: the database records a hierarchy that cannot exist, so
        # there is no member answer to give. Reported on stderr, where the other real failures go.
        err(f"error: {exc}")
        return 1
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
