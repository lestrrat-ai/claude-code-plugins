#!/usr/bin/env python3
"""Local fixture tests for the one-shot Fusion query CLI and JSON Lines session."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().with_name("query_fusion_api.py")
SPEC = importlib.util.spec_from_file_location("query_fusion_api", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
QUERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUERY)


def make_database(path: Path, *, inconsistent: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE symbols(
            id INTEGER PRIMARY KEY, module TEXT NOT NULL, name TEXT NOT NULL,
            qualname TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, bases TEXT NOT NULL DEFAULT '[]',
            signature TEXT, doc TEXT
        );
        CREATE TABLE members(
            id INTEGER PRIMARY KEY, symbol_id INTEGER NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, signature TEXT, returns TEXT, settable INTEGER NOT NULL DEFAULT 0,
            value TEXT, doc TEXT
        );
        INSERT INTO meta VALUES ('schema_version', '1');
        """
    )

    def symbol(module: str, name: str, bases: list[str] | None = None, doc: str = "") -> int:
        qualname = f"{module}.{name}"
        cursor = conn.execute(
            "INSERT INTO symbols(module,name,qualname,kind,bases,signature,doc) VALUES(?,?,?,?,?,?,?)",
            (module, name, qualname, "class", json.dumps(bases or []), None, doc),
        )
        return cursor.lastrowid

    def member(symbol_id: int, name: str, *, doc: str = "") -> None:
        conn.execute(
            "INSERT INTO members(symbol_id,name,kind,signature,returns,settable,value,doc)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (symbol_id, name, "method", "(self)", None, 0, None, doc),
        )

    base = symbol("pkg", "Base", doc="A base with Unicode café documentation.")
    left = symbol("pkg", "Left", ["pkg.Base"])
    right = symbol("pkg", "Right", ["pkg.Base"])
    diamond_bases = ["pkg.Left", "pkg.Right"]
    diamond = symbol("pkg", "Diamond", diamond_bases)
    one = symbol("pkg.one", "Widget")
    two = symbol("pkg.two", "Widget")
    cafe = symbol("pkg", "Café")
    member(base, "shared", doc="Inherited member.")
    member(left, "left_only")
    member(right, "right_only")
    member(diamond, "diamond_only")
    member(one, "widget_method")
    member(two, "widget_method")
    member(cafe, "décrire")
    if inconsistent:
        conn.execute("UPDATE symbols SET bases = ? WHERE qualname = ?", (json.dumps(["pkg.Right"]), "pkg.Left"))
        conn.execute("UPDATE symbols SET bases = ? WHERE qualname = ?", (json.dumps(["pkg.Left"]), "pkg.Right"))
    conn.commit()
    conn.close()


class QuerySessionTests(unittest.TestCase):
    def run_cli(self, db: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), *argv],
            check=False,
            capture_output=True,
            text=True,
        )

    def run_session(self, db: Path, requests: list[str]) -> tuple[int, list[dict], str]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--db", str(db), "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        ready = process.stdout.readline()
        self.assertTrue(ready)
        for line in requests:
            process.stdin.write(line + "\n")
        process.stdin.close()
        lines = [ready, *process.stdout.readlines()]
        stderr = process.stderr.read()
        returncode = process.wait()
        process.stdout.close()
        process.stderr.close()
        return returncode, [json.loads(line) for line in lines], stderr

    def test_session_results_match_cli_and_keep_association(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture with spaces.db"
            make_database(db)
            queries = [
                ["show", "pkg.Diamond"],
                ["members", "pkg.Diamond"],
                ["members", "pkg.Diamond", "--own"],
                ["search", "café"],
                ["show", "absent"],
                ["show", "Widget"],
            ]
            requests = [
                json.dumps({"id": index, "argv": argv}, ensure_ascii=False)
                for index, argv in enumerate(queries)
            ]
            returncode, responses, stderr = self.run_session(db, requests)
            self.assertEqual(returncode, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                responses[0],
                {"type": "ready", "protocol": 1, "commands": ["show", "members", "search"]},
            )
            for index, (response, argv) in enumerate(zip(responses[1:], queries)):
                cli = self.run_cli(db, *argv)
                self.assertEqual(response["id"], index)
                self.assertEqual(response["type"], "result")
                self.assertEqual(response["returncode"], cli.returncode)
                self.assertEqual(response["stdout"], cli.stdout)
                self.assertEqual(response["stderr"], cli.stderr)

    def test_request_errors_do_not_corrupt_next_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            make_database(db)
            requests = [
                "not JSON",
                json.dumps({"id": {"bad": True}, "argv": ["info"]}),
                json.dumps({"id": 3, "argv": ["show", 4]}),
                json.dumps({"id": 4, "argv": ["search", "café"]}, ensure_ascii=False),
            ]
            returncode, responses, _ = self.run_session(db, requests)
            self.assertEqual(returncode, 0)
            self.assertEqual(responses[1]["type"], "error")
            self.assertIsNone(responses[1]["id"])
            self.assertEqual(
                responses[2],
                {"id": None, "type": "error", "message": "unsupported session command: info"},
            )
            self.assertEqual(responses[3]["type"], "error")
            self.assertEqual(responses[4]["id"], 4)
            self.assertEqual(responses[4]["type"], "result")

    def test_bad_database_fails_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            process = subprocess.run(
                [sys.executable, str(SCRIPT), "--db", str(missing), "serve"],
                input="",
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stderr, "")
            response = json.loads(process.stdout)
            self.assertEqual(response["type"], "error")
            self.assertIsNone(response["id"])
            self.assertIn("database not found", response["message"])

    def test_inconsistent_hierarchy_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            make_database(db, inconsistent=True)
            returncode, responses, _ = self.run_session(
                db,
                [
                    json.dumps({"id": 1, "argv": ["show", "pkg.Diamond"]}),
                    json.dumps({"id": 2, "argv": ["search", "café"]}, ensure_ascii=False),
                ],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(responses[1]["type"], "error")
            self.assertEqual(responses[1]["id"], 1)
            self.assertIn("no consistent resolution order", responses[1]["message"])
            self.assertEqual(responses[2]["type"], "result")

    def test_session_opens_one_connection_for_many_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            make_database(db)
            request_lines = "\n".join(
                json.dumps({"id": index, "argv": ["search", "café"]}, ensure_ascii=False)
                for index in range(10)
            )
            stdin = io.StringIO(request_lines + "\n")
            stdout = io.StringIO()
            with (
                patch.object(QUERY.sqlite3, "connect", wraps=sqlite3.connect) as connect,
                patch("sys.stdin", stdin),
                patch("sys.stdout", stdout),
            ):
                returncode = QUERY.serve(db)
            self.assertEqual(returncode, 0)
            self.assertEqual(connect.call_count, 1)
            responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual([response["id"] for response in responses[1:]], list(range(10)))

    def test_single_command_exit_codes_stay_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            make_database(db)
            miss = self.run_cli(db, "show", "absent")
            self.assertEqual(miss.returncode, 1)
            self.assertIn("no symbol or member", miss.stdout)
            usage = self.run_cli(db, "members", "pkg.Base", "--unknown")
            self.assertEqual(usage.returncode, 2)


if __name__ == "__main__":
    unittest.main()
