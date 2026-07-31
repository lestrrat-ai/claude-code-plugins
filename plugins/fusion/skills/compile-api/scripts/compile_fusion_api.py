#!/usr/bin/env python3
"""Compile the Fusion 360 Python API reference into a SQLite database.

Source: the auto-generated intellisense stubs under
``Fusion_API_Python_Reference/defs/adsk`` in
https://github.com/AutodeskFusion360/FusionAPIReference. Every class, module
function, method, property, and class attribute (enum value) is extracted with
its signature, docstring, and inheritance links.

Default output is the user-level cache (``~/.cache/fusion-api-db/fusion-api.db``),
which the query script prefers over the database bundled with the plugin. Pass
``--output`` to write elsewhere (e.g. the bundled path in the authoring repo).

Stdlib only. Network access is needed unless ``--source`` points at a local
checkout of the stub directory.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "AutodeskFusion360/FusionAPIReference"
STUB_DIR = "Fusion_API_Python_Reference/defs/adsk"
SCHEMA_VERSION = "1"
USER_AGENT = "fusion-plugin-compile-api"

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


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def resolve_head_sha(ref: str) -> str:
    data = json.loads(http_get(f"https://api.github.com/repos/{REPO}/commits/{ref}"))
    return data["sha"]


def list_stub_files(sha: str) -> list[str]:
    data = json.loads(
        http_get(f"https://api.github.com/repos/{REPO}/git/trees/{sha}:{STUB_DIR}")
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
    return sorted(names)


def download_stubs(sha: str, dest: Path) -> list[Path]:
    paths: list[Path] = []
    for name in list_stub_files(sha):
        url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{STUB_DIR}/{name}"
        target = dest / name
        target.write_bytes(http_get(url))
        paths.append(target)
        print(f"fetched {name} ({target.stat().st_size} bytes)", file=sys.stderr)
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


def render_signature(fn: ast.FunctionDef) -> tuple[str, str | None]:
    """Return ("(a, b: int) -> Ret", "Ret") for a function definition."""
    a = fn.args
    parts: list[str] = []
    positional = a.posonlyargs + a.args
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(a.defaults)
    ) + list(a.defaults)
    for arg, default in zip(positional, defaults):
        parts.append(render_arg(arg, default))
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


def decorator_names(fn: ast.FunctionDef) -> list[str]:
    return [ast.unparse(d) for d in fn.decorator_list]


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
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
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


def compile_class(db: DocDb, module: str, node: ast.ClassDef) -> None:
    bases = [normalize_base(b, module) for b in node.bases]
    symbol_id = db.add_symbol(
        module, node.name, "class", bases, None, ast.get_docstring(node)
    )
    properties: dict[str, dict[str, object]] = {}
    for item in node.body:
        if isinstance(item, ast.FunctionDef):
            if item.name == "__init__":
                continue  # stub constructors are always `def __init__(self): pass`
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
        elif isinstance(item, ast.Assign) and len(item.targets) == 1:
            target = item.targets[0]
            if isinstance(target, ast.Name):
                db.add_member(
                    symbol_id,
                    target.id,
                    "attribute",
                    None,
                    None,
                    False,
                    ast.unparse(item.value),
                    None,
                )
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


def compile_module(db: DocDb, path: Path) -> None:
    module = module_name_for(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            compile_class(db, module, node)
        elif isinstance(node, ast.FunctionDef):
            signature, _ = render_signature(node)
            db.add_symbol(
                module,
                node.name,
                "function",
                [],
                signature,
                ast.get_docstring(node),
            )


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

    source_desc: str
    if args.source is not None:
        stub_paths = sorted(args.source.glob("*.py"))
        if not stub_paths:
            print(f"error: no .py stubs in {args.source}", file=sys.stderr)
            return 1
        source_desc = str(args.source)
        sha = "local"
    else:
        sha = resolve_head_sha(args.ref)
        print(f"compiling {REPO}@{sha}", file=sys.stderr)
        tmpdir = Path(tempfile.mkdtemp(prefix="fusion-stubs-"))
        stub_paths = download_stubs(sha, tmpdir)
        source_desc = f"https://github.com/{REPO}"

    db = DocDb(args.output)
    for path in stub_paths:
        compile_module(db, path)

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
    db.conn.close()
    print(f"wrote {args.output}: {symbols} symbols, {members} members")
    return 0


if __name__ == "__main__":
    sys.exit(main())
