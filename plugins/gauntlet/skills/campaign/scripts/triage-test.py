#!/usr/bin/env python3
"""Fixtures for campaign's deterministic file-class triage."""

from __future__ import annotations

import io
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath


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
        ("frontmatter delimiter prefix", [classified(
            T, "docs/helper.md", content=b"---\nname: demo\ndescription: agent work\n---oops\n")],
         False, T.TRIVIAL),
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

    try:
        rename = T.Change("100644", "100644", "R100", "docs/run-notes.md", "scripts/run.sh")
        renamed = T.classify_change(rename, lambda _path, _old: b"")
        check(renamed.path == "scripts/run.sh -> docs/run-notes.md",
              f"rename path rendered as {renamed.path!r}")
        rename_tier, _ = T.tier_for([renamed], False)
        check(rename_tier == T.HIGH, f"rename tier is {rename_tier}")
        print("ok       rename evidence           -> old path -> new path, HIGH")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL     rename evidence           -> {type(exc).__name__}: {exc}")
        failures += 1

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

    try:
        unreadable = T.Change("100644", "100644", "M", "docs/unreadable.md")
        try:
            unreadable_result = T.classify_change(unreadable, lambda _path, _old: None)
        except T.TriageError:
            unreadable_result = None
        if unreadable_result is not None:
            check(unreadable_result.file_class != T.HUMAN_DOC,
                  "failed regular-blob read classified as HUMAN-DOC")
        print("ok       failed regular read       -> closed to CODE or error")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL     failed regular read       -> {type(exc).__name__}: {exc}")
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
        (repo / "docs").mkdir()
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text(
            "Read `docs/runtime-compatibility.md` before changing validation.\n",
            encoding="utf-8",
        )
        (repo / "docs" / "runtime-compatibility.md").write_text(
            "# Runtime compatibility\n\nKeep workflow rules shared.\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "README.md", "AGENTS.md", "docs/runtime-compatibility.md"],
            cwd=repo,
            check=True,
        )
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

        crlf_content = b"---\r\nname: demo\r\ndescription: agent work\r\n---\r\n"
        (repo / "docs" / "helper.md").write_bytes(crlf_content)
        subprocess.run(["git", "add", "docs/helper.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "agent docs"], cwd=repo, check=True)
        crlf_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        try:
            result = T.derive(repo, head, crlf_head)
            check(T.agent_doc(PurePosixPath("docs/helper.md"), crlf_content),
                  "CRLF frontmatter was not detected as agent content")
            check(result["files"][0]["class"] == T.CODE,
                  f"CRLF agent doc classified as {result['files'][0]['class']}")
            check(result["tier"] == T.STANDARD, f"CRLF agent doc tier is {result['tier']}")
            check(result["required_reviews"] == 2,
                  f"CRLF agent doc requires {result['required_reviews']} reviews")
            print("ok       CRLF agent frontmatter     -> CODE, STANDARD, 2 reviews")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     CRLF agent frontmatter     -> {type(exc).__name__}: {exc}")
            failures += 1

        (repo / "docs" / "runtime-compatibility.md").write_text(
            "# Runtime compatibility\n\nKeep workflow rules shared and pinned.\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "docs/runtime-compatibility.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "runtime instructions"], cwd=repo, check=True)
        instruction_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        try:
            result = T.derive(repo, crlf_head, instruction_head)
            check(result["files"][0]["path"] == "docs/runtime-compatibility.md",
                  f"instruction fixture classified {result['files'][0]['path']}")
            check(result["files"][0]["class"] == T.CODE,
                  f"referenced runtime instructions classified as {result['files'][0]['class']}")
            check(result["tier"] == T.STANDARD, f"referenced instruction tier is {result['tier']}")
            check(result["required_reviews"] == 2,
                  f"referenced instruction requires {result['required_reviews']} reviews")
            print("ok       root AGENTS reference      -> agent doc, STANDARD, 2 reviews")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     root AGENTS reference      -> {type(exc).__name__}: {exc}")
            failures += 1

        missing_oid = "0123456789abcdef0123456789abcdef01234567"
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo",
             f"160000,{missing_oid},docs/gitlink.md"],
            cwd=repo,
            check=True,
        )
        tree = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
        gitlink_head = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", instruction_head, "-m", "gitlink"],
            cwd=repo,
            text=True,
        ).strip()
        subprocess.run(["git", "reset", "--hard", "-q", gitlink_head], cwd=repo, check=True)
        try:
            result = T.derive(repo, instruction_head, gitlink_head)
            check(result["files"][0]["class"] == T.CODE,
                  f"missing-object gitlink classified as {result['files'][0]['class']}")
            check(any("160000" in reason for reason in result["files"][0]["reasons"]),
                  f"gitlink evidence omitted its mode: {result['files'][0]['reasons']}")
            check(result["tier"] == T.STANDARD, f"gitlink tier is {result['tier']}")
            print("ok       missing-object gitlink     -> CODE, STANDARD")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     missing-object gitlink     -> {type(exc).__name__}: {exc}")
            failures += 1

        target_oid = subprocess.check_output(
            ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"../scripts/run.sh"
        ).decode("ascii").strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo",
             f"120000,{target_oid},docs/link.md"],
            cwd=repo,
            check=True,
        )
        tree = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
        symlink_head = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", gitlink_head, "-m", "symlink"],
            cwd=repo,
            text=True,
        ).strip()
        subprocess.run(["git", "reset", "--hard", "-q", symlink_head], cwd=repo, check=True)
        try:
            result = T.derive(repo, gitlink_head, symlink_head)
            check(result["files"][0]["class"] == T.CODE,
                  f"docs symlink classified as {result['files'][0]['class']}")
            check(any("120000" in reason for reason in result["files"][0]["reasons"]),
                  f"symlink evidence omitted its mode: {result['files'][0]['reasons']}")
            check(result["tier"] == T.STANDARD, f"symlink tier is {result['tier']}")
            print("ok       docs symlink               -> CODE, STANDARD")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     docs symlink               -> {type(exc).__name__}: {exc}")
            failures += 1

        (repo / symlink_head).write_text("not a revision\n", encoding="utf-8")
        (repo / "docs" / "plain.md").write_text("# Plain\n\nProse.\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", symlink_head, "docs/plain.md"], cwd=repo, check=True
        )
        subprocess.run(["git", "commit", "-qm", "sha-named path"], cwd=repo, check=True)
        ambiguous_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        try:
            result = T.derive(repo, symlink_head, ambiguous_head)
            named = {item["path"]: item["class"] for item in result["files"]}
            check(named.get(symlink_head) == T.CODE,
                  f"SHA-named path classified as {named.get(symlink_head)!r}")
            check(named.get("docs/plain.md") == T.HUMAN_DOC,
                  f"sibling prose classified as {named.get('docs/plain.md')!r}")
            check(result["tier"] == T.STANDARD, f"SHA-named path tier is {result['tier']}")
            print("ok       SHA-named path             -> classified, not read as a revision")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     SHA-named path             -> {type(exc).__name__}: {exc}")
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
    print(f"all {len(cases) + 10} triage fixtures hold")
    return 0
