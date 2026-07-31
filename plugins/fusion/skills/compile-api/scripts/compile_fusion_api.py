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
unpacked assignment. Any statement the compiler does not index is counted
into ``meta.unhandled_statements`` and reported on stderr, so a construct it
does not understand shows up as a number rather than a missing symbol.

Default output is the user-level cache (``~/.cache/fusion-api-db/fusion-api.db``),
which the query script prefers over the database bundled with the plugin. Pass
``--output`` to write elsewhere (e.g. the bundled path in the authoring repo).

The output database is replaced only after every stub has been parsed and the
result has been checked, so a failed compile leaves the previous database in
place rather than a truncated one.

Stdlib only. Network access is needed unless ``--source`` points at a local
checkout of the stub directory.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

REPO = "AutodeskFusion360/FusionAPIReference"
STUB_DIR = "Fusion_API_Python_Reference/defs/adsk"
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
# sits far above that, so a normal compile never trips one.
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_STUB_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_STUB_ENTRIES = 256

# Time caps. `urlopen(timeout=...)` bounds one socket operation, not a response: a server that
# sends a byte at a time resets that timer with every byte and can hold the compile open for as
# long as it likes. So the body is read in bounded chunks against a wall-clock deadline for the
# whole response, and the socket timeout keeps its separate job of bounding a single stalled read.
# One response therefore costs at most MAX_RESPONSE_SECONDS plus the one read already in flight.
SOCKET_TIMEOUT_SECONDS = 60
MAX_RESPONSE_SECONDS = 120
READ_CHUNK_BYTES = 64 * 1024

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
    """The only stdout write in this script, so nothing reaches stdout unsanitized."""
    print(sanitize(text))


def err(text: str) -> None:
    """The only stderr write in this script, so nothing reaches stderr unsanitized."""
    print(sanitize(text), file=sys.stderr)


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


def resolve_head_sha(ref: str, budget: ByteBudget | None = None) -> str:
    data = json.loads(
        http_get(
            f"https://api.github.com/repos/{REPO}/commits/{ref}",
            MAX_METADATA_BYTES,
            budget,
        )
    )
    sha = data["sha"]
    # This value reaches both fetch URLs and stderr, so it is checked before either. The rejected
    # value is quoted through repr, which escapes control characters, and clipped: a response may
    # carry up to MAX_METADATA_BYTES here.
    if not isinstance(sha, str) or not OBJECT_ID.fullmatch(sha):
        raise RuntimeError(
            f"commit lookup for {ref!r} returned {sha!r:.80}, not a git object id"
        )
    return sha


def list_stub_files(sha: str, budget: ByteBudget | None = None) -> list[str]:
    data = json.loads(
        http_get(
            f"https://api.github.com/repos/{REPO}/git/trees/{sha}:{STUB_DIR}",
            MAX_METADATA_BYTES,
            budget,
        )
    )
    if data.get("truncated"):
        raise RuntimeError("stub directory listing was truncated")
    names = [
        e["path"]
        for e in data["tree"]
        if e["type"] == "blob" and e["path"].endswith(".py")
    ]
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


def assignment_names(target: ast.expr) -> list[str]:
    """Every plain name an assignment target binds, in source order.

    A tuple or list target binds one name per element (`first, second = ...`) and may nest, so the
    elements are walked rather than the target alone. A starred element binds its own inner target.
    A subscript or attribute target assigns somewhere other than this class, so it binds nothing
    here and yields no names.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(assignment_names(element))
        return names
    if isinstance(target, ast.Starred):
        return assignment_names(target.value)
    return []


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


# Statements that carry no API of their own, so not indexing one is a decision rather than a gap:
# a docstring or an `...`/`pass` body is an `ast.Expr` or `ast.Pass` whose content is already stored
# with its owner, and an import declares a name that belongs to another module. Every statement
# outside this set that the enumerations below do not handle is counted as unhandled, so a stub
# construct this compiler does not understand is reported as a number instead of vanishing.
IGNORED_STATEMENTS = (ast.Expr, ast.Import, ast.ImportFrom, ast.Pass)


def compile_class(db: DocDb, module: str, node: ast.ClassDef) -> int:
    """Index one class; return how many of its body statements went unhandled."""
    bases = [normalize_base(b, module) for b in node.bases]
    symbol_id = db.add_symbol(
        module, node.name, "class", bases, None, ast.get_docstring(node)
    )
    properties: dict[str, dict[str, object]] = {}
    unhandled = 0
    for item in node.body:
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
        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
            # Both statements declare class attributes. An annotated one may carry no value at all
            # (`x: int`), so having a value is not what makes an attribute worth indexing, and a
            # chained `a = b = 3` declares every one of its targets. assignment_names() decides
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
                for name in assignment_names(target):
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
        elif not isinstance(item, IGNORED_STATEMENTS):
            unhandled += 1
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
    """Index one stub module; return how many statements went unhandled, its classes included."""
    module = module_name_for(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    unhandled = 0
    for node in tree.body:
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
        elif not isinstance(node, IGNORED_STATEMENTS):
            unhandled += 1
    return unhandled


def fill_database(
    db: DocDb, stub_paths: list[Path], source_desc: str, sha: str
) -> tuple[int, int, int]:
    """Compile every stub and its metadata into `db`.

    Returns (symbol count, member count, unhandled statement count). The last is recorded in `meta`
    as well as returned, so a database carries the number of constructs its compiler did not
    understand rather than only the symbols it did.
    """
    unhandled = 0
    for path in stub_paths:
        unhandled += compile_module(db, path)
    db.set_meta("unhandled_statements", str(unhandled))
    db.set_meta("schema_version", SCHEMA_VERSION)
    db.set_meta("source", source_desc)
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
    stub_paths: list[Path], output: Path, source_desc: str, sha: str
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
                db, stub_paths, source_desc, sha
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="local directory containing the adsk stub .py files (skips download)",
    )
    parser.add_argument(
        "--ref",
        default="main",
        help="git ref of the reference repo to compile (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".cache" / "fusion-api-db" / "fusion-api.db",
        help="database path to write (default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        if args.source is not None:
            stub_paths = sorted(args.source.glob("*.py"))
            if not stub_paths:
                err(f"error: no .py stubs in {args.source}")
                return 1
            # Every path emits this source record, so what a compile compiled is reportable
            # whichever way the stubs were obtained. A local compile has no commit to name, so
            # the commit field reads `local` — the same value `meta.source_commit` records.
            err(f"compiling {args.source}@local")
            symbols, members, unhandled = build_database(
                stub_paths, args.output, str(args.source), "local"
            )
        else:
            # Built before the first request, so the commit lookup spends from it too.
            budget = ByteBudget(MAX_TOTAL_BYTES)
            sha = resolve_head_sha(args.ref, budget)
            err(f"compiling {REPO}@{sha}")
            # The staging directory goes away on success and on failure alike.
            with tempfile.TemporaryDirectory(prefix="fusion-stubs-") as tmpdir:
                stub_paths = download_stubs(sha, Path(tmpdir), budget)
                symbols, members, unhandled = build_database(
                    stub_paths, args.output, f"https://github.com/{REPO}", sha
                )
    except (OSError, RuntimeError, SyntaxError, ValueError, KeyError, sqlite3.Error) as exc:
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
