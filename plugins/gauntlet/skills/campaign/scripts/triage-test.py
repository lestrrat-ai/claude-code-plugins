#!/usr/bin/env python3
"""Fixtures for campaign's deterministic file-class triage."""

from __future__ import annotations

import io
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def classified(T, path: str, *, old_mode: str = "100644", new_mode: str = "100644",
               content: bytes = b""):
    change = T.Change(old_mode, new_mode, "M", path)
    return T.classify_change(change, lambda _path, _old: content)


def run(T) -> int:
    cases = [
        ("top README", [classified(T, "README.md")], False, T.TRIVIAL),
        ("human docs", [classified(T, "docs/guide.md")], False, T.TRIVIAL),
        ("agent frontmatter", [classified(
            T, "docs/skill.md", content=b"---\nname: demo\ndescription: agent work\n---\n")], False, T.STANDARD),
        ("skill instructions", [classified(T, "plugins/x/skills/y/SKILL.md")], False, T.STANDARD),
        ("source", [classified(T, "src/main.py")], False, T.STANDARD),
        ("script", [classified(T, "scripts/check.py")], False, T.HIGH),
        ("executable", [classified(T, "README.md", new_mode="100755")], False, T.HIGH),
        ("dependency", [classified(T, "package.json")], False, T.HIGH),
        ("security path", [classified(T, "src/auth/token.py")], False, T.HIGH),
        ("systemic prose", [classified(T, "README.md")], True, T.HIGH),
        ("mixed prose and code", [classified(T, "README.md"), classified(T, "src/main.py")], False, T.STANDARD),
        ("empty diff", [], False, T.STANDARD),
    ]
    failures = 0
    for name, files, systemic, want in cases:
        got, _ = T.tier_for(files, systemic)
        if got != want:
            print(f"FAIL     {name:24} -> {got}, expected {want}")
            failures += 1
        else:
            print(f"ok       {name:24} -> {got}")

    raw = (
        b":100644 100755 " + b"1" * 40 + b" " + b"2" * 40 + b" R100\0"
        b"docs/old name.md\0scripts/new name.py\0"
    )
    try:
        parsed = T.parse_raw(raw)
        check(len(parsed) == 1, f"rename parsed as {len(parsed)} records")
        check(parsed[0].old_path == "docs/old name.md", f"old path changed: {parsed[0]!r}")
        check(parsed[0].path == "scripts/new name.py", f"new path changed: {parsed[0]!r}")
        check(T.executable(parsed[0].new_mode), "new executable mode was not detected")
        print("ok       raw rename               -> paths and modes preserved")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL     raw rename               -> {type(exc).__name__}: {exc}")
        failures += 1

    with tempfile.TemporaryDirectory(prefix="gauntlet-triage-") as raw_tmp:
        repo = Path(raw_tmp)
        commands = [
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Gauntlet Test"],
            ["git", "config", "user.email", "gauntlet@example.invalid"],
        ]
        for argv in commands:
            subprocess.run(argv, cwd=repo, check=True)
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        (repo / "README.md").write_text("base\nmore prose\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "docs"], cwd=repo, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        try:
            result = T.derive(repo, base, head)
            check(result["tier"] == T.TRIVIAL, f"integration tier is {result['tier']}")
            check(result["head_sha"] == head, "integration result lost its pinned head")
            print("ok       git integration           -> pinned docs diff is TRIVIAL")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     git integration           -> {type(exc).__name__}: {exc}")
            failures += 1

        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = T.main(["derive", "--worktree", str(repo), "--base", base, "--head-sha", "f" * 40])
        except SystemExit as exc:
            code = exc.code
        if code != 2 or "worktree HEAD is" not in err.getvalue():
            print(f"FAIL     stale head                -> exit {code}: {err.getvalue().strip()}")
            failures += 1
        else:
            print("ok       stale head                -> refused before classification")

    if failures:
        print(f"{failures} triage fixture(s) failed")
        return 1
    print(f"all {len(cases) + 3} triage fixtures hold")
    return 0
