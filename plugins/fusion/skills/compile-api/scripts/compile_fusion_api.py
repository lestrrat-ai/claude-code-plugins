#!/usr/bin/env python3
"""Compile the Fusion 360 Python API reference into a SQLite database.

Source: the auto-generated intellisense stubs under
``Fusion_API_Python_Reference/defs/adsk`` in
https://github.com/AutodeskFusion360/FusionAPIReference. Every class, module
function, method (``__init__`` included), property, and class attribute is
extracted with its signature, docstring, and inheritance links; ``async def``
declares a function or a method wherever ``def`` does and is indexed the same
way. A class attribute is indexed whatever it was declared with — a value (an
enum value), an annotation, both, or neither, the last being a name bound by an
unpacked assignment. A statement that binds no name and is not one of the
declared exemptions — ``is_exempt_statement`` is where they are named — is
counted into ``meta.unhandled_statements`` and reported on stderr, so a
construct it does not understand shows up as a number rather than a missing
symbol. That count is derived from what a statement bound, not from which node
types were recognised, so a shape this compiler never anticipated is counted
too. A target part that binds no name counts the same way, even where another
part of the same statement did bind one.

The stubs are documentation and disagree with the shipped runtime in a few known places. A member
this compiler knows the runtime does not define is DROPPED rather than indexed, so the database
answers "does this exist" the way Fusion does; ``STUB_ONLY_MEMBERS`` names every such member and
owns the reasoning, and ``meta.stub_only_members_dropped`` records what a given compile removed. An
entry that stops matching is reported on stderr rather than passed over, so the list cannot go
stale in silence.

Default output is the user-level cache (``~/.cache/fusion-api-db/fusion-api.db``),
which the query script prefers over the database bundled with the plugin. Pass
``--output`` to write elsewhere (e.g. the bundled path in the authoring repo).

The output database is replaced only after every stub has been parsed and the
result has been checked, so a failed compile leaves the previous database in
place rather than a truncated one. A stub larger than the per-stub parse limit
is refused before it is parsed and fails the compile, whether it was downloaded
or given by ``--source``.

Both halves of the provenance are recorded and reported: ``meta.source_ref``
holds the ref that was asked for and ``meta.source_commit`` the commit it
resolved to, and the ``compiling`` line on stderr names the same pair.

Stdlib only. Network access is needed unless ``--source`` points at a local
checkout of the stub directory. A local compile resolves no ref, so ``--source``
and ``--ref`` are refused together rather than one of them being ignored.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import http.client
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the stream type argparse hands _print_message; no runtime import
    from _typeshed import SupportsWrite

REPO = "AutodeskFusion360/FusionAPIReference"
STUB_DIR = "Fusion_API_Python_Reference/defs/adsk"
# Versions the TABLE schema below — the CREATE statements in SCHEMA — and nothing else. It is still
# "1" because those statements have not changed since the first compile. `meta` rows are deliberately
# NOT covered: a key is added without touching any table, every reader looks its keys up by name, and
# a reader that wants a key it does not find already learns that from the lookup. Bumping this for an
# added `meta` key would make an old database unreadable to nothing (no reader compares the value) or,
# if one were taught to, would reject a user's cached database over a purely additive change.
SCHEMA_VERSION = "1"
USER_AGENT = "fusion-plugin-compile-api"

# Download caps. Nothing here is negotiated with the server, so an unbounded compile would let a
# runaway or hostile response decide this process's memory use and request count. What the caps
# below actually guarantee:
#   * every download one compile makes — the commit lookup included — spends from a single
#     MAX_TOTAL_BYTES budget, so the bytes this process buffers over a whole compile are bounded;
#   * one response is read to at most one byte past the smaller of its own cap
#     (MAX_METADATA_BYTES or MAX_STUB_BYTES) and what the budget still allows, so a body far past
#     either limit is never buffered whole just to be rejected afterwards;
#   * MAX_STUB_ENTRIES bounds how many files one tree listing may ask this process to fetch, which
#     no byte cap can do: a tree naming thousands of empty blobs costs almost no bytes.
# The whole stub set measured about 4 MB across 8 files when these were chosen (2026-07); every cap
# in this block sits far above that, so a normal compile never trips one.
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_STUB_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_STUB_ENTRIES = 256

# Parse cap, which is not a download cap and does not follow from one. ast.parse() builds a tree
# costing roughly two orders of magnitude what the source text costs — a 4 MB stub measured about
# 570 MB peak RSS (2026-08) — so the byte caps above bound what a response may buffer and say
# nothing about what parsing that response costs. A `--source` stub is never downloaded at all, so
# no download cap reaches it either. Every stub is therefore measured and refused BEFORE it is
# parsed, whichever way it arrived, instead of being parsed in full and rejected afterwards.
# The largest stub measured about 2.8 MB when this was chosen (2026-08, adsk/fusion.py), so the cap
# below leaves roughly 3x headroom over real data while keeping one parse to about a gigabyte at
# the measured ratio, where the 32 MiB one download may carry would cost several.
MAX_PARSE_BYTES = 8 * 1024 * 1024

# Time caps. `urlopen(timeout=...)` bounds one socket operation, not a response: a server that
# sends a byte at a time resets that timer with every byte and can hold the compile open for as
# long as it likes. So the body is read in bounded chunks against a wall-clock deadline for the
# whole response, and the socket timeout keeps its separate job of bounding a single stalled read.
# One response therefore costs at most MAX_RESPONSE_SECONDS plus the one read already in flight.
SOCKET_TIMEOUT_SECONDS = 60
MAX_RESPONSE_SECONDS = 120
READ_CHUNK_BYTES = 64 * 1024

# Members the stubs declare that the SHIPPED RUNTIME does not define, dropped from the database
# rather than indexed.
#
# The stubs are auto-generated DOCUMENTATION; the module Fusion imports is the SWIG output under
# `API/Python/packages/adsk`. Where the two disagree the runtime is what user code meets, so a
# member only the stub declares is an answer of "yes, that exists" to a question whose real answer
# is AttributeError. A checker asking this database whether a call resolves is exactly the consumer
# that gets it wrong.
#
# Each entry is established by diffing a stub set against the SAME Fusion build's runtime modules,
# never against a different build: the reference repo tracks a newer API than an installed Fusion,
# and every difference that skew produces is a version gap rather than a defect. Version-matched,
# the diff yields the two names below and nothing else (2026-09-02, Fusion build 61bf25b2 against
# the stubs that build ships).
#
#   * `cast` is bound per class by an assignment at the end of each runtime module
#     (`Application.cast = lambda arg: ...`). `Base` gets no such line, so `adsk.core.Base.cast`
#     does not exist and nothing inherits `cast` from it either; every other runtime class that has
#     `cast` binds its own.
#   * `adsk.core.EventHandler` is documentation-only. The runtime defines the concrete
#     `<Event>EventHandler` classes and no `EventHandler` at all, so its `cast` cannot resolve.
#
# RESIDUAL, disclosed rather than fixed: this list drops MEMBERS, so `adsk.core.EventHandler` stays
# indexed as a class even though the runtime has no such name. Deleting the class would break the
# base chain of the stub classes that declare it as their base, which is a worse answer than a
# class whose members are accurate. A caller who needs "is this class importable" cannot get it
# from here.
STUB_ONLY_MEMBERS = frozenset(
    {
        ("adsk.core", "Base", "cast"),
        ("adsk.core", "EventHandler", "cast"),
    }
)

# A tree entry name must be a plain `.py` file name that lands directly in the staging directory:
# no separator, no leading dot, no `..`, nothing absolute.
STUB_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\.py")

# A git object id: 40 hex digits today, 64 under the SHA-256 object format. The commit lookup's
# answer is interpolated into fetch URLs and printed, so anything else is refused before either.
OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

# Everything outside printable ASCII, newline, and tab is a candidate for escaping; the category
# test in escape_char() decides. Printable ASCII is never Cc or Cf, so skipping it changes nothing
# and keeps ordinary text off the per-character path entirely.
ESCAPE_CANDIDATE = re.compile(r"[^\n\t\x20-\x7e]")


def escape_char(match: re.Match[str]) -> str:
    char = match.group()
    if unicodedata.category(char) not in ("Cc", "Cf"):
        return char
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def sanitize(text: str) -> str:
    """Escape control and format characters in text on its way to the terminal.

    A server chooses the error reasons, redirect targets, and stub contents this compiler reports,
    so anything a response carries can reach this terminal. Newlines and tabs are kept because
    messages and docstrings are laid out with them; every other control or format character is
    shown as an escape rather than executed by the terminal.

    query_fusion_api.py carries the same function for the same reason. The two scripts are
    standalone by design (each skill runs its own), so the copies must be changed together.
    """
    return ESCAPE_CANDIDATE.sub(escape_char, text)


def out(text: str) -> None:
    """Write sanitized text to stdout; err() is the stderr half of the same pair.

    Every line this script prints goes through one of the two: argparse's usage, help, and error
    text (SanitizedParser routes it here), and a warning Python raises while a stub is parsed
    (compile_module() captures it and reports it through err() instead of letting the interpreter
    quote the source line itself). What stays outside the pair is an unhandled exception's
    traceback, which Python writes on its own and is not sanitized.
    """
    print(sanitize(text))


def err(text: str) -> None:
    """Write sanitized text to stderr; see out() for what the pair does and does not cover."""
    print(sanitize(text), file=sys.stderr)


class SanitizedParser(argparse.ArgumentParser):
    """An argument parser whose own usage, help, and error text goes through out() and err().

    argparse writes to the two streams itself, and it echoes argv back: an unrecognized option is
    quoted verbatim into the message. That text is operator-supplied and needs the same escaping a
    server's text gets. Every argparse write funnels through ``_print_message``, so overriding that
    one method covers usage, ``--help``, and every error path at once.

    query_fusion_api.py carries the same class for the same reason. The two scripts are standalone
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


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE symbols(
    id INTEGER PRIMARY KEY,
    module TEXT NOT NULL,
    name TEXT NOT NULL,
    qualname TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    bases TEXT NOT NULL DEFAULT '[]',
    signature TEXT,
    doc TEXT
);
CREATE TABLE members(
    id INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT,
    returns TEXT,
    settable INTEGER NOT NULL DEFAULT 0,
    value TEXT,
    doc TEXT
);
CREATE INDEX idx_symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX idx_members_name ON members(name COLLATE NOCASE);
CREATE INDEX idx_members_symbol ON members(symbol_id);
"""


class ByteBudget:
    """Caps the total number of bytes one compile may download."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def spend(self, count: int, what: str) -> None:
        if self.used + count > self.limit:
            raise RuntimeError(
                f"download budget of {self.limit} bytes exhausted while fetching {what}"
            )
        self.used += count


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect instead of following it.

    urllib's default handler follows a 3xx to any http, https, or ftp target it is given, so a
    response could move a request to an origin this compiler never chose — and no caller inspects
    the URL a body actually came from. Every URL fetched here is built from a constant HTTPS host,
    so a redirect is a reason to stop, never a reason to open a second origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"refusing redirect to {newurl!r:.120}",
            headers,
            fp,
        )


OPENER = urllib.request.build_opener(NoRedirect)


def http_get(url: str, max_bytes: int, budget: ByteBudget | None = None) -> bytes:
    # Read one byte past the tighter of the two caps. Past the per-response cap tells the body is
    # oversized; past what the budget can still afford tells the same for the compile as a whole.
    # Either way the read stops there, so the check below never runs on a fully buffered body.
    ceiling = max_bytes if budget is None else min(max_bytes, budget.remaining)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline = time.monotonic() + MAX_RESPONSE_SECONDS
    chunks: list[bytes] = []
    read = 0
    with OPENER.open(req, timeout=SOCKET_TIMEOUT_SECONDS) as resp:
        # Chunked so the deadline is tested between reads: a body delivered a byte at a time never
        # trips the socket timeout, and without this loop nothing else would stop it.
        # `read1` and not `read`: `read` is served by a buffered reader that keeps issuing socket
        # reads until it has the whole amount asked for, so a drip would sit inside one `read` call
        # and the deadline would only be reached once the body had already arrived. `read1` returns
        # what one socket read produced, which is what makes the check below periodic.
        while read <= ceiling:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{url} did not finish within {MAX_RESPONSE_SECONDS} seconds"
                )
            chunk = resp.read1(min(READ_CHUNK_BYTES, ceiling + 1 - read))
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise RuntimeError(f"{url} returned more than the {max_bytes}-byte limit")
    if budget is not None:
        budget.spend(len(data), url)
    return data


def decode_object(raw: bytes, what: str) -> dict[str, object]:
    """Decode a response body into a JSON object, or refuse it naming what arrived instead.

    Nothing at this end decides what a response carries: a proxy, an error page, or an API that
    changed can answer with a list, a string, a number, or null where an object was expected.
    Indexing or calling a method on one of those raises TypeError or AttributeError, and neither is
    in main()'s except tuple, so the operator would get a traceback where an ``error:`` line was
    promised. Every decoded response is therefore shape-checked before it is read, and a wrong shape
    fails the compile closed with a message that names it.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{what} did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{what} returned {type(data).__name__}, not a JSON object")
    return data


def resolve_head_sha(ref: str, budget: ByteBudget | None = None) -> str:
    """Resolve an operator-supplied git ref to the commit id GitHub reports for it.

    The ref becomes one path segment of the lookup URL, and git accepts ref names that mean
    something else inside a URL, so it is percent-encoded before it is inserted rather than after.
    Two characters make the difference between asking for one commit and compiling another:
    ``#`` starts a URL fragment, which urllib strips off before the request leaves this process
    (``main#nothing`` would be sent as ``main`` and answer with main's commit); and a ``%`` the
    operator typed is a percent-escape the server decodes itself (``ma%69n`` would arrive as
    ``main``). ``/`` is kept unescaped because a hierarchical ref such as ``release/2.0`` is one
    path, not a segment containing a separator.
    """
    data = decode_object(
        http_get(
            f"https://api.github.com/repos/{REPO}/commits/{urllib.parse.quote(ref, safe='/')}",
            MAX_METADATA_BYTES,
            budget,
        ),
        f"commit lookup for {ref!r}",
    )
    # `.get` and not `[...]`: a response object without the key is one more malformed shape, and the
    # check below already states what a value that is not an object id looks like.
    sha = data.get("sha")
    # This value reaches both fetch URLs and stderr, so it is checked before either. The rejected
    # value is quoted through repr, which escapes control characters, and clipped: a response may
    # carry up to MAX_METADATA_BYTES here.
    if not isinstance(sha, str) or not OBJECT_ID.fullmatch(sha):
        raise RuntimeError(
            f"commit lookup for {ref!r} returned {sha!r:.80}, not a git object id"
        )
    return sha


def list_stub_files(sha: str, budget: ByteBudget | None = None) -> list[str]:
    what = f"stub listing for {sha}"
    data = decode_object(
        http_get(
            f"https://api.github.com/repos/{REPO}/git/trees/{sha}:{STUB_DIR}",
            MAX_METADATA_BYTES,
            budget,
        ),
        what,
    )
    if data.get("truncated"):
        raise RuntimeError("stub directory listing was truncated")
    # Every level of this response is checked before it is read, for the reason decode_object()
    # gives: a tree that is not a list, an entry that is not an object, or a name that is not a
    # string would otherwise raise TypeError or AttributeError past main()'s except tuple.
    tree = data.get("tree")
    if not isinstance(tree, list):
        raise RuntimeError(f"{what} carried {type(tree).__name__} as its tree, not a list")
    names: list[str] = []
    for entry in tree:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"{what} carried {type(entry).__name__} as a tree entry, not an object"
            )
        kind = entry.get("type")
        path = entry.get("path")
        if not isinstance(kind, str) or not isinstance(path, str):
            raise RuntimeError(
                f"{what} carried a tree entry whose type is {type(kind).__name__} and whose"
                f" path is {type(path).__name__}, not two strings"
            )
        if kind == "blob" and path.endswith(".py"):
            names.append(path)
    if not names:
        raise RuntimeError(f"no .py stubs found under {STUB_DIR} at {sha}")
    # One request per name follows, and each may cost nothing against the byte budget, so the
    # listing's own length is what bounds the work. Refused here, before the first stub download.
    if len(names) > MAX_STUB_ENTRIES:
        raise RuntimeError(
            f"stub listing named {len(names)} .py files, above the"
            f" {MAX_STUB_ENTRIES}-entry limit"
        )
    return sorted(names)


def staging_target(dest: Path, name: str) -> Path:
    """Resolve a tree entry name to a file directly inside `dest`, or refuse it.

    The names come from the server's tree listing, so they are untrusted input: one carrying a
    separator, a `..`, or an absolute path would otherwise be written wherever it pointed.
    """
    if not STUB_NAME.fullmatch(name):
        raise RuntimeError(f"refusing stub entry {name!r}: expected a plain .py file name")
    root = dest.resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise RuntimeError(f"refusing stub entry {name!r}: it resolves outside {root}")
    return target


def download_stubs(sha: str, dest: Path, budget: ByteBudget | None = None) -> list[Path]:
    paths: list[Path] = []
    for name in list_stub_files(sha, budget):
        url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{STUB_DIR}/{name}"
        target = staging_target(dest, name)
        target.write_bytes(http_get(url, MAX_STUB_BYTES, budget))
        paths.append(target)
        err(f"fetched {name} ({target.stat().st_size} bytes)")
    return paths


def module_name_for(path: Path) -> str:
    return "adsk" if path.stem == "__init__" else f"adsk.{path.stem}"


def render_arg(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        sep = " = " if arg.annotation is not None else "="
        text += f"{sep}{ast.unparse(default)}"
    return text


def render_signature(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, str | None]:
    """Return ("(a, b: int) -> Ret", "Ret") for a function definition."""
    a = fn.args
    parts: list[str] = []
    positional = a.posonlyargs + a.args
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(a.defaults)
    ) + list(a.defaults)
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        parts.append(render_arg(arg, default))
        # `/` closes the positional-only group; without it the rendered signature would claim
        # those parameters can be passed by keyword.
        if index == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg is not None:
        parts.append("*" + render_arg(a.vararg, None))
    elif a.kwonlyargs:
        parts.append("*")
    for arg, kw_default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(render_arg(arg, kw_default))
    if a.kwarg is not None:
        parts.append("**" + render_arg(a.kwarg, None))
    returns = ast.unparse(fn.returns) if fn.returns is not None else None
    signature = "(" + ", ".join(parts) + ")"
    if returns is not None:
        signature += f" -> {returns}"
    return signature, returns


def decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [ast.unparse(d) for d in fn.decorator_list]


def assignment_targets(target: ast.expr) -> tuple[list[str], int]:
    """Every plain name an assignment target binds, in source order, and how many parts bind none.

    A tuple or list target binds one name per element (`first, second = ...`) and may nest, so the
    elements are walked rather than the target alone. A starred element binds its own inner target.
    A part that binds no name is RETURNED AS A COUNT rather than as silence, because the caller has
    already claimed the statement and nothing after it would notice the loss. A partial target list
    — one element binding a name and another binding none — reports both halves for the same reason.

    Which parts those are is DERIVED, never enumerated by node type. A subtree that yielded neither
    a name nor an already-counted part bound nothing, so it is itself one part that binds no name.
    That covers a subscript or attribute (which assigns somewhere other than this class) and an
    empty element list such as `()` or `[]` (which assigns nowhere at all) by the same test, and it
    covers whatever shape comes next without naming it.
    """
    if isinstance(target, ast.Name):
        return [target.id], 0
    if isinstance(target, ast.Starred):
        return assignment_targets(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        unbound = 0
        for element in target.elts:
            element_names, element_unbound = assignment_targets(element)
            names.extend(element_names)
            unbound += element_unbound
        if names or unbound:
            return names, unbound
        # Nothing came back from the elements, so this target binds nothing: fall through to the
        # line below and be counted as one part, exactly as a subscript target is.
    return [], 1


def normalize_base(base: ast.expr, module: str) -> str:
    """Map a base-class expression to a qualified name within the adsk package."""
    text = ast.unparse(base)
    if text.startswith("adsk."):
        return text
    if "." in text:
        return f"adsk.{text}"  # e.g. core.Base referenced from adsk.fusion
    return f"{module}.{text}"  # bare name → same module


class DocDb:
    def __init__(self, path: Path) -> None:
        # `path` is always a fresh staging file chosen by build_database(); nothing is deleted
        # here, so the previous database survives a compile that fails partway.
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def add_symbol(
        self,
        module: str,
        name: str,
        kind: str,
        bases: list[str],
        signature: str | None,
        doc: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO symbols(module, name, qualname, kind, bases, signature, doc)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (module, name, f"{module}.{name}", kind, json.dumps(bases), signature, doc),
        )
        rowid = cur.lastrowid
        assert rowid is not None
        return rowid

    def add_member(
        self,
        symbol_id: int,
        name: str,
        kind: str,
        signature: str | None,
        returns: str | None,
        settable: bool,
        value: str | None,
        doc: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO members(symbol_id, name, kind, signature, returns,"
            " settable, value, doc) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol_id, name, kind, signature, returns, int(settable), value, doc),
        )

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))


def is_exempt_statement(node: ast.stmt, index: int) -> bool:
    """True for a statement that carries no API of its own, so passing it over is not a gap.

    The test is STRUCTURAL, not by node type, and that is the whole point: `ast.Expr` covers a
    docstring, a bare `...`, AND a live expression such as a second string literal, `1 + 1`, or
    `print(...)`. Exempting the node type would drop the third kind without counting it, which is
    the one thing meta.unhandled_statements exists to prevent.

    `index` is the statement's position in the body it belongs to. A string constant is a docstring
    only in the leading position — the only position ast.get_docstring() reads — and that docstring
    is stored with its owner. `...` is a body placeholder, `pass` declares nothing, and an import
    declares a name belonging to another module. Everything else, this returns False for, and the
    callers count it.
    """
    if isinstance(node, (ast.Pass, ast.Import, ast.ImportFrom)):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        value = node.value.value
        return value is Ellipsis or (index == 0 and isinstance(value, str))
    return False


def compile_class(db: DocDb, module: str, node: ast.ClassDef) -> int:
    """Index one class; return how many of its body statements went unhandled."""
    bases = [normalize_base(b, module) for b in node.bases]
    symbol_id = db.add_symbol(
        module, node.name, "class", bases, None, ast.get_docstring(node)
    )
    properties: dict[str, dict[str, object]] = {}
    unhandled = 0
    for index, item in enumerate(node.body):
        # No branch below decides the count. Each one reports what the statement ACTUALLY BOUND into
        # this class (`declared`) and how many of its parts bound nothing (`unbound`), and the single
        # site after the branches derives the count from those two facts. That is what makes the rule
        # structural instead of per node type: a class-body statement is counted unless it is a
        # declared exemption or it bound at least one name. A shape no branch anticipated is
        # therefore counted for having produced no member, not for being unrecognised — which is how
        # an empty target (`() = ()`, `[] = []`) is counted despite reaching the assignment branch
        # and being claimed by it.
        declared: list[str] = []
        unbound = 0
        exempt = False
        # `async def` declares a member exactly as `def` does, so both node types are indexed.
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = decorator_names(item)
            signature, returns = render_signature(item)
            doc = ast.get_docstring(item)
            if "property" in decorators:
                properties[item.name] = {
                    "returns": returns,
                    "doc": doc,
                    "settable": properties.get(item.name, {}).get("settable", False),
                }
            elif f"{item.name}.setter" in decorators:
                entry = properties.setdefault(
                    item.name, {"returns": None, "doc": doc, "settable": False}
                )
                entry["settable"] = True
            else:
                kind = "method"
                if "staticmethod" in decorators:
                    kind = "staticmethod"
                elif "classmethod" in decorators:
                    kind = "classmethod"
                db.add_member(
                    symbol_id, item.name, kind, signature, returns, False, None, doc
                )
            # Every path above binds `item.name` into the class: a property entry, a setter on one,
            # or a member row. A definition always carries a name, so this is unconditional.
            declared.append(item.name)
        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
            # Both statements declare class attributes. An annotated one may carry no value at all
            # (`x: int`), so having a value is not what makes an attribute worth indexing, and a
            # chained `a = b = 3` declares every one of its targets. assignment_targets() decides
            # which names a target binds.
            targets: list[ast.expr] = (
                item.targets if isinstance(item, ast.Assign) else [item.target]
            )
            annotation = (
                ast.unparse(item.annotation)
                if isinstance(item, ast.AnnAssign)
                else None
            )
            value = ast.unparse(item.value) if item.value is not None else None
            for target in targets:
                # An unpacked target gives each of its names one piece of the value, and which
                # piece is not decidable from the source alone (`a, b = pair()`). So the value is
                # recorded only where a target is one name; every name is indexed either way.
                unpacked = not isinstance(target, ast.Name)
                names, target_unbound = assignment_targets(target)
                # A part that binds no name — `external.attr = 1`, `c[0] = 5`, the second half of
                # `a, external.b = pair()`, or an empty `()` — assigns somewhere this database does
                # not index. This branch has already claimed the statement, so the exemption test
                # below never sees it: reporting the parts is what keeps them from being dropped in
                # silence.
                unbound += target_unbound
                declared.extend(names)
                for name in names:
                    db.add_member(
                        symbol_id,
                        name,
                        "attribute",
                        None,
                        annotation,
                        False,
                        None if unpacked else value,
                        None,
                    )
        else:
            exempt = is_exempt_statement(item, index)
        if declared:
            # It bound something, so only the parts that bound nothing are missing.
            unhandled += unbound
        elif not exempt:
            # It bound nothing at all, so the statement itself is the gap. `unbound or 1` and not
            # `unbound + 1`: where parts were already reported they describe this same statement,
            # and where none were the statement still counts once.
            unhandled += unbound or 1
    for name, entry in properties.items():
        returns_val = entry["returns"]
        doc_val = entry["doc"]
        db.add_member(
            symbol_id,
            name,
            "property",
            None,
            returns_val if isinstance(returns_val, str) else None,
            bool(entry["settable"]),
            None,
            doc_val if isinstance(doc_val, str) else None,
        )
    return unhandled


def compile_module(db: DocDb, path: Path) -> int:
    """Index one stub module; return how many statements went unhandled, its classes included.

    A stub past MAX_PARSE_BYTES is refused here, before ast.parse() ever sees it, so the memory a
    compile spends on an oversized stub is the size of the stub rather than two orders of magnitude
    more. This is the one gate every stub passes, downloaded or handed over by `--source`, and it is
    an addition: the checks that run after a parse still decide whether the result may replace an
    existing database.

    A warning the parse raises is captured and reported through err() rather than left to Python's
    own warning writer. That writer quotes the offending SOURCE LINE verbatim, which is stub text a
    server chose, so it would put unescaped bytes on this terminal past both sanitizing writers —
    the one place stub content reached stderr without going through sanitize(). The report keeps the
    file, line, category, and message, and drops the source line the interpreter would have copied.
    """
    size = path.stat().st_size
    if size > MAX_PARSE_BYTES:
        raise RuntimeError(
            f"{path} is {size} bytes, past the {MAX_PARSE_BYTES}-byte per-stub parse limit;"
            " it was refused before parsing"
        )
    module = module_name_for(path)
    with warnings.catch_warnings(record=True) as raised:
        # Every category, not SyntaxWarning alone: the point is that no warning raised here reaches
        # the terminal by any route but err(). "always" keeps a repeat from being swallowed by the
        # once-per-location default, so what is reported is what the parse actually raised.
        warnings.simplefilter("always")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for entry in raised:
        err(
            f"warning: {path}:{entry.lineno}: {entry.category.__name__}: {entry.message}"
        )
    unhandled = 0
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.ClassDef):
            unhandled += compile_class(db, module, node)
        # `async def` declares a module function exactly as `def` does, so both are indexed.
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature, _ = render_signature(node)
            db.add_symbol(
                module,
                node.name,
                "function",
                [],
                signature,
                ast.get_docstring(node),
            )
        elif not is_exempt_statement(node, index):
            unhandled += 1
    return unhandled


def drop_stub_only_members(db: DocDb) -> tuple[list[str], list[str]]:
    """Delete every member `STUB_ONLY_MEMBERS` names; return (dropped, stale) qualnames.

    `stale` holds the entries that matched no row, and the caller warns about each one. That
    warning is the only thing standing between this list and silent rot: an upstream stub that
    stops declaring the member, a class renamed, or a typo in the tuple all produce an entry that
    removes nothing, and an entry suppressing nothing is indistinguishable from a working one
    without the check. It is a warning and not a failure because upstream fixing its own stub must
    not break a compile.

    `meta.unhandled_statements` is deliberately untouched. That count answers "what did the
    compiler fail to understand", and these statements were understood completely — they are
    dropped for disagreeing with the runtime, which is a different fact and is recorded separately
    in `meta.stub_only_members_dropped`.
    """
    dropped: list[str] = []
    stale: list[str] = []
    for module, class_name, member in sorted(STUB_ONLY_MEMBERS):
        # Scoped by module AND class name rather than by member name alone: `cast` is declared on
        # over a thousand classes, and every one of the others is real.
        cur = db.conn.execute(
            "DELETE FROM members WHERE name = ? AND symbol_id IN"
            " (SELECT id FROM symbols WHERE module = ? AND name = ? AND kind = 'class')",
            (member, module, class_name),
        )
        target = f"{module}.{class_name}.{member}"
        (dropped if cur.rowcount else stale).append(target)
    return dropped, stale


def fill_database(
    db: DocDb, stub_paths: list[Path], source_desc: str, sha: str, ref: str
) -> tuple[int, int, int]:
    """Compile every stub and its metadata into `db`.

    Returns (symbol count, member count, unhandled statement count). The last is recorded in `meta`
    as well as returned, so a database carries the number of constructs its compiler did not
    understand rather than only the symbols it did.

    The stub-only members are dropped here, between the last stub and the first count, so the
    totals returned describe the database that is written rather than the one that was parsed.

    `meta.source_ref` records what was ASKED FOR and `meta.source_commit` what that resolved to, so
    the two are checkable against each other. A commit id alone cannot be checked: it says which
    commit was compiled and nothing about whether it is the one the operator named.
    """
    unhandled = 0
    for path in stub_paths:
        unhandled += compile_module(db, path)
    # Before the counts below, so the symbol and member totals a compile reports describe the
    # database it actually wrote.
    dropped, stale = drop_stub_only_members(db)
    for target in stale:
        err(
            f"warning: {target} is listed in STUB_ONLY_MEMBERS but the stubs no longer declare it;"
            " drop the entry after confirming against the runtime module"
        )
    db.set_meta("stub_only_members_dropped", json.dumps(dropped))
    db.set_meta("unhandled_statements", str(unhandled))
    db.set_meta("schema_version", SCHEMA_VERSION)
    db.set_meta("source", source_desc)
    db.set_meta("source_ref", ref)
    db.set_meta("source_commit", sha)
    db.set_meta(
        "generated_at",
        datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    )
    db.set_meta("modules", json.dumps([module_name_for(p) for p in stub_paths]))
    db.conn.commit()
    symbols = db.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    members = db.conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    return symbols, members, unhandled


def build_database(
    stub_paths: list[Path], output: Path, source_desc: str, sha: str, ref: str
) -> tuple[int, int, int]:
    """Compile into a staging file beside `output` and replace `output` only once it is complete.

    A stub that fails to parse — or any other error — therefore leaves an existing database
    untouched, instead of replacing it with an empty one that later queries would trust.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, staging_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent
    )
    os.close(handle)
    staging = Path(staging_name)
    # sqlite3.connect creates the file itself; mkstemp only reserved the name.
    staging.unlink()
    try:
        db = DocDb(staging)
        try:
            symbols, members, unhandled = fill_database(
                db, stub_paths, source_desc, sha, ref
            )
        finally:
            db.conn.close()
        if symbols == 0 or members == 0:
            raise RuntimeError(
                f"compiled {symbols} symbols and {members} members from"
                f" {len(stub_paths)} stub(s); refusing to replace {output}"
            )
        # mkstemp files are private to the owner; the database is meant to be as readable as any
        # other file this user writes.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(staging, 0o666 & ~umask)
        os.replace(staging, output)
    except BaseException:
        for leftover in (staging, Path(f"{staging}-journal"), Path(f"{staging}-wal")):
            leftover.unlink(missing_ok=True)
        raise
    return symbols, members, unhandled


class SelfTestFailure(RuntimeError):
    """One self-test assertion that did not hold."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestFailure(message)


# A stub set shaped like the real one in the ONE respect these fixtures test: `cast` is declared
# identically on a class the runtime gives it to and on a class the runtime does not, so nothing but
# STUB_ONLY_MEMBERS can tell the two apart. `EventHandler` is deliberately absent so the stale-entry
# warning has something to fire on.
SELF_TEST_STUB = '''
class Base():
    """The base class that all other classes are derived from."""
    @staticmethod
    def cast(arg) -> Base:
        return Base()
    @staticmethod
    def classType() -> str:
        return str()

class Point3D(Base):
    """A point."""
    @staticmethod
    def cast(arg) -> Point3D:
        return Point3D()
    @property
    def x(self) -> float:
        return float()
'''


def self_test() -> int:
    """Compile SELF_TEST_STUB and assert what the stub-only drop does and does not remove."""
    with tempfile.TemporaryDirectory(prefix="fusion-self-test-") as tmpdir:
        root = Path(tmpdir)
        stub = root / "core.py"
        stub.write_text(SELF_TEST_STUB, encoding="utf-8")
        output = root / "self-test.db"
        # err() writes through `sys.stderr`, so redirecting it captures the warnings this compile
        # raises instead of leaving them on the terminal of a run that is asserting them.
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            build_database([stub], output, str(root), "local", "local")
        stderr = captured.getvalue()
        conn = sqlite3.connect(output)
        try:
            present = {
                (module, symbol, member)
                for module, symbol, member in conn.execute(
                    "SELECT s.module, s.name, m.name FROM members m"
                    " JOIN symbols s ON s.id = m.symbol_id"
                )
            }
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()

    check(
        ("adsk.core", "Base", "cast") not in present,
        "adsk.core.Base.cast must be dropped: the runtime binds `cast` per class and gives Base"
        " no such attribute, so indexing it tells a caller a call resolves that raises",
    )
    check(
        ("adsk.core", "Base", "classType") in present,
        "the drop must be scoped to the listed member: Base's other members are real and stay",
    )
    check(
        ("adsk.core", "Point3D", "cast") in present,
        "the drop must be scoped to the listed CLASS: `cast` is declared on over a thousand"
        " classes and every one of the others is real",
    )
    check(
        json.loads(meta["stub_only_members_dropped"]) == ["adsk.core.Base.cast"],
        "meta.stub_only_members_dropped must record exactly what this compile removed;"
        f" got {meta.get('stub_only_members_dropped')!r}",
    )
    check(
        meta["unhandled_statements"] == "0",
        "a dropped member is understood, not unhandled — the two counts must stay separate",
    )
    # The stale-entry warning is the only thing that can catch STUB_ONLY_MEMBERS going out of date,
    # so it is asserted rather than assumed. This stub declares no `EventHandler`, which is exactly
    # the shape an upstream fix would produce.
    check(
        "adsk.core.EventHandler.cast" in stderr
        and "STUB_ONLY_MEMBERS" in stderr,
        "an entry that matched nothing must be reported on stderr; without that warning a stale"
        f" entry is indistinguishable from a working one. stderr was: {stderr!r}",
    )
    check(
        "adsk.core.Base.cast is listed" not in stderr,
        "an entry that DID match must not be reported as stale",
    )
    out("self-test: ok")
    return 0


def main() -> int:
    parser = SanitizedParser(description=__doc__)
    # Mutually exclusive, because a `--source` compile resolves no ref: it parses the directory it
    # was handed, whatever ref the operator also named. Accepting both would record `source_ref` as
    # `local` while the operator was told nothing, so the pair is refused as the usage error it is
    # (exit 2, through SanitizedParser like every other argparse message). `--ref` keeps its default
    # for the download path, which a group only constrains when both options are actually given.
    origin = parser.add_mutually_exclusive_group()
    origin.add_argument(
        "--source",
        type=Path,
        help="local directory containing the adsk stub .py files (skips download;"
        " not combinable with --ref)",
    )
    origin.add_argument(
        "--ref",
        default="main",
        help="git ref of the reference repo to compile (default: %(default)s;"
        " not combinable with --source)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".cache" / "fusion-api-db" / "fusion-api.db",
        help="database path to write (default: %(default)s)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="compile a built-in stub and assert the stub-only member drop, then exit",
    )
    args = parser.parse_args()

    if args.self_test:
        # Ahead of every other compile path, and it returns rather than falling through: the
        # self-test builds its own stub in its own temporary directory, so `--output` and `--ref`
        # have nothing to act on and are ignored (the SKILL.md option entry says so).
        try:
            return self_test()
        except (SelfTestFailure, OSError, sqlite3.Error) as exc:
            err(f"error: self-test: {exc}")
            return 1

    try:
        if args.source is not None:
            stub_paths = sorted(args.source.glob("*.py"))
            if not stub_paths:
                err(f"error: no .py stubs in {args.source}")
                return 1
            # Every path emits this source record, so what a compile compiled is reportable
            # whichever way the stubs were obtained. A local compile resolves no ref and has no
            # commit to name, so both fields read `local` — the same values `meta.source_ref` and
            # `meta.source_commit` record.
            err(f"compiling {args.source}@local")
            symbols, members, unhandled = build_database(
                stub_paths, args.output, str(args.source), "local", "local"
            )
        else:
            # Built before the first request, so the commit lookup spends from it too.
            budget = ByteBudget(MAX_TOTAL_BYTES)
            sha = resolve_head_sha(args.ref, budget)
            # The requested ref is named beside the commit it resolved to, because the resolution
            # is what an operator cannot otherwise check: a ref that reaches the server as a
            # different one still answers, and a line naming only the resolved commit reads exactly
            # the same either way. Quoted through repr so a ref that is empty, spaced, or padded is
            # visible as typed; err() escapes anything it carries.
            err(f"compiling {REPO}@{sha} (requested ref {args.ref!r})")
            # The staging directory goes away on success and on failure alike.
            with tempfile.TemporaryDirectory(prefix="fusion-stubs-") as tmpdir:
                stub_paths = download_stubs(sha, Path(tmpdir), budget)
                symbols, members, unhandled = build_database(
                    stub_paths, args.output, f"https://github.com/{REPO}", sha, args.ref
                )
    # http.client.HTTPException is here because it is NOT an OSError: a response that ends early
    # (a truncated chunked body) raises IncompleteRead, which would otherwise reach the interpreter
    # as a traceback rather than the `error:` line every other failure produces.
    except (
        OSError,
        RuntimeError,
        SyntaxError,
        ValueError,
        KeyError,
        sqlite3.Error,
        http.client.HTTPException,
    ) as exc:
        err(f"error: {exc}")
        return 1

    if unhandled:
        # Not a failure: the database is usable and every symbol the compiler understood is in it.
        # It is reported because a construct this compiler does not handle would otherwise be an
        # absent symbol nobody can distinguish from a symbol the stubs never declared.
        err(
            f"warning: {unhandled} statement(s) used a construct this compiler does not index;"
            " they are counted in meta.unhandled_statements"
        )
    out(f"wrote {args.output}: {symbols} symbols, {members} members")
    return 0


if __name__ == "__main__":
    sys.exit(main())
