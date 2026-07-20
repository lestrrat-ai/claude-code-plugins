#!/usr/bin/env python3
"""Fixtures for ``triage.py`` — the mechanical file inventory and FLOOR tier for one pinned diff.

The suite uses real temporary Git repositories for the file, mode, rename, delete, modification,
type-change, symlink, ordering and hostile path cases.  It pins that classification inspects every side
that exists (base and head) and that a non-regular Git object (symlink, gitlink, unrecognized mode) is
never prose.  It loads the owner by path and therefore fails loudly when the executable policy owner is
missing.  It pins that the floor is only ever HIGH/STANDARD/None (never TRIVIAL — that is the
orchestrator's semantic call) and that the ``--tier`` lower-bound check vetoes a below-floor tier.  That
missing-owner failure was the pre-change reproduction: campaign previously had only prose classification,
so no command could reject a stale head or consistently detect these classes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "triage.py"


def _load_owner():
    mod = load_module_from_path("campaign_triage_owner", OWNER, register=True)
    if mod is None:
        raise RuntimeError(f"cannot load deterministic tier derivation at {OWNER}")
    return mod


M = _load_owner()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise M.SelfTestFailure(message)


def git(repo: Path, *args: str, check_result: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)  # noqa: S603
    if check_result and proc.returncode != 0:
        raise M.SelfTestFailure(
            f"git {' '.join(args)} failed ({proc.returncode}): {os.fsdecode(proc.stderr)}")
    return proc


def write(repo: Path, name: str, content: str = "content\n", mode: int = 0o644) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def write_bytes_name(repo: Path, name: bytes, content: bytes = b"content\n") -> None:
    target = os.path.join(os.fsencode(repo), name)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", message)
    return os.fsdecode(git(repo, "rev-parse", "HEAD").stdout).strip()


@contextmanager
def repository(base_files: dict[str, tuple[str, int]] | None = None):
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "Gauntlet Test")
        git(repo, "config", "user.email", "gauntlet@example.invalid")
        files = base_files or {"baseline.txt": ("base\n", 0o644)}
        for name, (content, mode) in files.items():
            write(repo, name, content, mode)
        base = commit(repo, "base")
        yield repo, base


def derive(repo: Path, base: str, *, tier: str | None = None) -> dict:
    head = os.fsdecode(git(repo, "rev-parse", "HEAD").stdout).strip()
    return M.derive(worktree=str(repo), base=base, head_sha=head, tier=tier)


def one_file(result: dict) -> dict:
    check(len(result["files"]) == 1, f"expected one changed file, got {result['files']!r}")
    return result["files"][0]


def t_human_docs_have_no_floor() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        commit(repo, "docs")
        result = derive(repo, base)
    check(result["floor"] is None,
          f"an all-prose diff must have NO floor — the tool never grants TRIVIAL: {result!r}")
    check(one_file(result)["class"] == M.HUMAN_DOC, "docs/guide.md must be HUMAN-DOC")


def t_top_level_human_doc_names() -> None:
    prose = ("README.md", "CHANGELOG", "CHANGELOG.md", "CHANGELOG.txt", "CHANGELOG.rst",
             "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE-MIT", "LICENSE-APACHE")
    for name in prose:
        cls, reasons = M._path_class(name, b"plain prose\n")
        check(cls == M.HUMAN_DOC, f"{name} must be HUMAN-DOC, got {cls}: {reasons}")


def t_prose_named_source_is_code() -> None:
    """A top-level name that begins with a prose word but carries a source-like or unknown suffix
    (CHANGELOG.py, license.go, LICENSE.exe) is NOT prose: an unbounded stem prefix let it clear the
    escalate-only floor. It must classify CODE and floor STANDARD, vetoing a decided --tier TRIVIAL."""
    for name in ("CHANGELOG.py", "license.go", "LICENSE.exe", "changelog.sh"):
        cls, reasons = M._path_class(name, b"import os\n")
        check(cls == M.CODE, f"{name} must be CODE, not prose-by-prefix: {cls}: {reasons}")
    with repository() as (repo, base):
        write(repo, "CHANGELOG.py", "import os\nos.system('noop')\n")
        write(repo, "license.go", "package main\n")
        head = commit(repo, "source named like prose")
        result = derive(repo, base)
        check(result["floor"] == M.STANDARD,
              f"source suffixes on prose stems must floor STANDARD: {result!r}")
        check({row["class"] for row in result["files"]} == {M.CODE},
              f"CHANGELOG.py and license.go must classify CODE: {result!r}")
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", head, "--tier", M.TRIVIAL])
    check(code == M.EXIT_REFUSED and out == "",
          f"--tier TRIVIAL must be refused for source named like prose: {code}/{out!r}")
    check("below the mechanical floor" in err, f"the refusal must name the floor: {err!r}")


def t_source_and_unknown_are_standard() -> None:
    with repository() as (repo, base):
        write(repo, "src/widget.py", "value = 1\n")
        write(repo, "assets/blob.weird", "x\n")
        commit(repo, "code")
        result = derive(repo, base)
    check(result["floor"] == M.STANDARD,
          f"code/unknown content must floor to STANDARD, got {result!r}")
    check({row["class"] for row in result["files"]} == {M.CODE}, "both paths must classify CODE")


def t_agent_frontmatter_is_code() -> None:
    with repository() as (repo, base):
        write(repo, "docs/operator.md", "---\nname: operator\ndescription: agent behavior\n---\nBody\n")
        commit(repo, "agent doc")
        result = derive(repo, base)
    row = one_file(result)
    check(result["floor"] == M.STANDARD and row["class"] == M.CODE,
          f"agent-frontmatter Markdown must be CODE and floor STANDARD, got {result!r}")
    check(any("frontmatter" in reason for reason in row["reasons"]), "reason must name frontmatter")


def t_agent_paths_are_code() -> None:
    cases = {
        "AGENTS.md": "agent-consumed instruction file",
        "plugins/x/skills/y/references/rules.md": "skill reference",
        ".claude/commands/do.md": "Claude agent configuration path",
        "docs/prompts/reviewer.txt": "prompt or agent-instruction path",
    }
    for path, expected in cases.items():
        cls, reasons = M._path_class(path, b"plain\n")
        check(cls == M.CODE and expected in reasons, f"{path} must be CODE because {expected}: {reasons}")


def t_sensitive_classes_are_high() -> None:
    cases = (
        ".github/workflows/ci.yml",
        "plugins/x/scripts/check.py",
        "package-lock.json",
        ".codex-plugin/plugin.json",
        "infra/main.tf",
        "deploy/helm/values.yaml",
        "src/auth/token.py",
        "config/secrets/app.txt",
        "Dockerfile",
        "Makefile",
    )
    for path in cases:
        cls, reasons = M._path_class(path, b"content\n")
        check(cls == M.SENSITIVE and reasons, f"{path} must be mechanically SENSITIVE, got {cls}: {reasons}")


def t_mixed_content_uses_highest_class() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        write(repo, "src/main.py", "print('x')\n")
        write(repo, ".github/workflows/ci.yml", "name: ci\n")
        commit(repo, "mixed")
        result = derive(repo, base)
    check(result["floor"] == M.HIGH, f"one sensitive file must raise the mixed diff's floor to HIGH: {result!r}")
    by_path = {row["path"]: row["class"] for row in result["files"]}
    check(by_path == {
        ".github/workflows/ci.yml": M.SENSITIVE,
        "docs/guide.md": M.HUMAN_DOC,
        "src/main.py": M.CODE,
    }, f"mixed classes drifted: {by_path!r}")


def t_executable_mode_add_and_remove_are_high() -> None:
    for before, after in ((0o644, 0o755), (0o755, 0o644)):
        with repository({"tool.txt": ("run\n", before)}) as (repo, base):
            (repo / "tool.txt").chmod(after)
            commit(repo, "mode")
            result = derive(repo, base)
        row = one_file(result)
        check(result["floor"] == M.HIGH and row["class"] == M.SENSITIVE,
              f"mode {oct(before)}->{oct(after)} must floor to HIGH/SENSITIVE: {result!r}")
        check("old or new Git mode is executable" in row["reasons"], "executable reason is missing")


def t_rename_classifies_both_paths() -> None:
    with repository({"docs/old.md": ("# Old\n", 0o644)}) as (repo, base):
        (repo / "src").mkdir()
        (repo / "docs/old.md").rename(repo / "src/new.md")
        commit(repo, "rename")
        result = derive(repo, base)
    row = one_file(result)
    check(row["status"].startswith("R") and row["old_path"] == "docs/old.md" and row["path"] == "src/new.md",
          f"rename identity is wrong: {row!r}")
    check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
          "a rename from human docs into source must classify both paths and floor to STANDARD")


def t_rename_from_sensitive_remains_high() -> None:
    with repository({"scripts/tool.py": ("print('x')\n", 0o644)}) as (repo, base):
        (repo / "docs").mkdir()
        (repo / "scripts/tool.py").rename(repo / "docs/tool.md")
        commit(repo, "rename sensitive")
        result = derive(repo, base)
    check(result["floor"] == M.HIGH and one_file(result)["class"] == M.SENSITIVE,
          f"touching the old sensitive path must keep the floor at HIGH: {result!r}")


def t_sensitive_deletion_is_high() -> None:
    with repository({"scripts/obsolete.py": ("print('x')\n", 0o644)}) as (repo, base):
        (repo / "scripts/obsolete.py").unlink()
        commit(repo, "delete")
        result = derive(repo, base)
    row = one_file(result)
    check(row["status"] == "D" and row["old_path"] == "scripts/obsolete.py",
          f"deletion must retain its old path: {row!r}")
    check(result["floor"] == M.HIGH, "deleting a sensitive file still floors to HIGH")


def t_deleted_agent_frontmatter_is_code() -> None:
    frontmatter = "---\nname: old-skill\ndescription: old agent rules\n---\nBody\n"
    with repository({"docs/old.md": (frontmatter, 0o644)}) as (repo, base):
        (repo / "docs/old.md").unlink()
        commit(repo, "delete agent doc")
        result = derive(repo, base)
    check(result["floor"] == M.STANDARD and one_file(result)["class"] == M.CODE,
          f"deleted Markdown must inspect base content for agent frontmatter and floor STANDARD: {result!r}")


def t_symlink_at_human_doc_path_is_code() -> None:
    """A symlink (Git mode 120000) added at a human-doc path is a non-regular Git object, never prose:
    it must classify at least CODE, floor STANDARD, and refuse a decided --tier TRIVIAL."""
    with repository({"docs/real.md": ("# Real\n", 0o644)}) as (repo, base):
        (repo / "docs/guide.md").symlink_to("real.md")
        head = commit(repo, "symlink at a doc path")
        result = derive(repo, base)
        row = one_file(result)
        check(row["path"] == "docs/guide.md" and row["new_mode"] == "120000",
              f"the changed file must be the symlink: {row!r}")
        check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
              f"a symlink at a docs path must be CODE and floor STANDARD, not a prose no-floor: {result!r}")
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", head, "--tier", M.TRIVIAL])
    check(code == M.EXIT_REFUSED and out == "",
          f"--tier TRIVIAL must be refused when a symlink clears the doc path: {code}/{out!r}")
    check("below the mechanical floor" in err, f"the refusal must name the floor: {err!r}")


def t_nonregular_modes_are_never_prose() -> None:
    """A symlink (120000), a gitlink (160000), or any unrecognized mode at a human-doc path is a
    non-regular Git object; the mode, not the content, forces at least CODE (fail-closed) so the
    escalate-only floor cannot be cleared by a non-blob object wearing a prose path."""
    def runner(argv: list[str], _worktree: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"plain prose\n", b"")

    for mode in ("120000", "160000", "100600"):
        change = M.Change(status="A", old_mode="000000", new_mode=mode, old_path=None, path=b"docs/guide.md")
        row = M._classify_change(change, runner, "/nonexistent", "a" * 40, "b" * 40)
        check(row["class"] == M.CODE,
              f"mode {mode} at docs/guide.md must classify CODE, not prose: {row!r}")
        check(any("non-regular" in reason for reason in row["reasons"]),
              f"the reason must name the non-regular mode: {row!r}")


def t_modification_classifies_base_and_head() -> None:
    """A single-path modification (status M) must classify BOTH its base and head content and keep the
    higher class. Stripping agent frontmatter leaves plain prose at HEAD but changed an agent-consumed
    document, so the base side must still classify CODE — as renames and deletions already do."""
    frontmatter = "---\nname: operator\ndescription: agent behavior\ntools: read\n---\nBody\n"
    with repository({"docs/operator.md": (frontmatter, 0o644)}) as (repo, base):
        write(repo, "docs/operator.md", "# Operator\n\nPlain prose now, no frontmatter.\n")
        head = commit(repo, "strip agent frontmatter")
        result = derive(repo, base)
        row = one_file(result)
        check(row["status"] == "M", f"the change must be a modification: {row!r}")
        check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
              f"a modification that strips frontmatter must classify the base side CODE and floor STANDARD: {result!r}")
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", head, "--tier", M.TRIVIAL])
    check(code == M.EXIT_REFUSED and out == "",
          f"--tier TRIVIAL must be refused for a frontmatter-strip modification: {code}/{out!r}")
    check("below the mechanical floor" in err, f"the refusal must name the floor, not a missing worktree: {err!r}")


def t_type_change_classifies_base_and_head() -> None:
    """A single-path type-change (status T) must classify BOTH sides. A regular prose doc that becomes a
    symlink keeps prose at the base but a non-regular object at HEAD, so it floors to STANDARD."""
    with repository({"docs/guide.md": ("# Guide\n", 0o644)}) as (repo, base):
        (repo / "docs/guide.md").unlink()
        (repo / "docs/guide.md").symlink_to("elsewhere.md")
        commit(repo, "doc becomes a symlink")
        result = derive(repo, base)
    row = one_file(result)
    check(row["status"] == "T", f"regular file -> symlink must be a type change: {row!r}")
    check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
          f"a type change to a symlink must classify at least CODE and floor STANDARD: {result!r}")


def t_tool_never_emits_trivial_floor() -> None:
    """The floor is only ever HIGH, STANDARD, or None (no floor) — the tool is STRUCTURALLY INCAPABLE of
    granting TRIVIAL. All-prose yields None (the orchestrator decides), NOT a TRIVIAL grant."""
    scenarios = (
        ({"docs/guide.md": "# Guide\n"}, None),
        ({"src/main.py": "x = 1\n"}, M.STANDARD),
        ({"scripts/tool.py": "print('x')\n"}, M.HIGH),
    )
    for files, expected in scenarios:
        with repository() as (repo, base):
            for name, content in files.items():
                write(repo, name, content)
            commit(repo, "scenario")
            result = derive(repo, base)
        check(result["floor"] == expected,
              f"floor for {sorted(files)} must be {expected!r}, got {result['floor']!r}")
        check(result["floor"] != M.TRIVIAL, "the tool must NEVER emit a TRIVIAL floor")
    # An empty diff also never floors below STANDARD — never a vacuous no-floor that could read as TRIVIAL.
    with repository() as (repo, base):
        empty = derive(repo, base)
    check(empty["floor"] == M.STANDARD, f"an empty diff floors to STANDARD, never TRIVIAL: {empty!r}")


def t_tier_below_floor_is_refused() -> None:
    """The optional --tier is a LOWER-BOUND check: a decided tier below the floor is refused (veto-downward),
    while a tier at or above the floor — including the orchestrator's TRIVIAL on an all-prose diff — passes."""
    # floor HIGH (sensitive): only HIGH clears it.
    with repository() as (repo, base):
        write(repo, "scripts/tool.py", "print('x')\n")
        head = commit(repo, "sensitive")
        for below in (M.TRIVIAL, M.STANDARD):
            code, out, err = capture_cli(M.main, [
                "derive", "--worktree", str(repo), "--base", base, "--head-sha", head, "--tier", below])
            check(code == M.EXIT_REFUSED and out == "",
                  f"--tier {below} below a HIGH floor must be refused with no JSON: {code}/{out!r}")
            check("below the mechanical floor" in err and "HIGH" in err,
                  f"the refusal must name the floor: {err!r}")
        check(derive(repo, base, tier=M.HIGH)["floor"] == M.HIGH,
              "--tier HIGH clears a HIGH floor and still emits the inventory")
    # floor STANDARD (code): TRIVIAL is refused, STANDARD/HIGH pass.
    with repository() as (repo, base):
        write(repo, "src/main.py", "x = 1\n")
        commit(repo, "code")
        try:
            derive(repo, base, tier=M.TRIVIAL)
        except M.TriageError as exc:
            check("below the mechanical floor" in str(exc), f"STANDARD floor must veto TRIVIAL: {exc}")
        else:
            raise M.SelfTestFailure("a TRIVIAL tier below a STANDARD floor was accepted")
        check(derive(repo, base, tier=M.STANDARD)["floor"] == M.STANDARD, "--tier STANDARD clears a STANDARD floor")
    # floor None (all prose): the orchestrator's TRIVIAL call is ALLOWED — nothing to veto.
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        commit(repo, "docs")
        ok = derive(repo, base, tier=M.TRIVIAL)
    check(ok["floor"] is None, "an all-prose diff has no floor, so a decided TRIVIAL is accepted")


def t_head_mismatch_is_refused_without_output() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        head = commit(repo, "docs")
        wrong = ("0" if head[0] != "0" else "1") + head[1:]
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", wrong,
        ])
    check(code == M.EXIT_REFUSED and out == "", f"stale expected head must emit no JSON, got {code}/{out!r}")
    check("HEAD mismatch" in err and wrong in err and head in err, f"mismatch refusal must name both SHAs: {err!r}")


def t_moving_head_is_refused() -> None:
    sha_a = "a" * 40
    sha_b = "b" * 40
    head_reads = 0

    def runner(argv: list[str], _worktree: str) -> subprocess.CompletedProcess[bytes]:
        nonlocal head_reads
        if argv == ["git", "rev-parse", "--verify", "HEAD^{commit}"]:
            head_reads += 1
            value = sha_a if head_reads == 1 else sha_b
            return subprocess.CompletedProcess(argv, 0, os.fsencode(value + "\n"), b"")
        if argv[:4] == ["git", "rev-parse", "--verify", "--end-of-options"]:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(sha_a + "\n"), b"")
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(sha_a + "\n"), b"")
        if argv[:3] == ["git", "diff", "--raw"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 99, b"", b"unexpected argv")

    with tempfile.TemporaryDirectory() as directory:
        try:
            M.derive(worktree=directory, base="main", head_sha=sha_a, runner=runner)
        except M.TriageError as exc:
            check("HEAD moved during triage" in str(exc), f"wrong moving-head refusal: {exc}")
        else:
            raise M.SelfTestFailure("a HEAD that moved during the read was accepted")


def t_deterministic_order_and_bytes() -> None:
    with repository() as (repo, base):
        write(repo, "z-last.py", "z = 1\n")
        write(repo, "docs/a-first.md", "# A\n")
        write(repo, "middle.unknown", "m\n")
        commit(repo, "unordered")
        first = derive(repo, base)
        second = derive(repo, base)
    paths = [row["path"] for row in first["files"]]
    check(paths == sorted(paths), f"per-file records must sort by path: {paths!r}")
    encoded1 = json.dumps(first, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    encoded2 = json.dumps(second, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    check(encoded1 == encoded2, "the same pinned Git evidence must emit byte-identical JSON")


def t_hostile_paths_are_data() -> None:
    hostile = "--odd\n\"tick`$ name.py"
    with repository() as (repo, base):
        write(repo, hostile, "value = 1\n")
        write_bytes_name(repo, b"bad-\xff-name.md", b"plain\n")
        commit(repo, "hostile paths")
        result = derive(repo, base)
    paths = [row["path"] for row in result["files"]]
    check(hostile in paths, f"whitespace/quotes/backticks/dollar/leading dash path was not preserved: {paths!r}")
    check(any("\udcff" in path for path in paths), f"non-UTF8 filename bytes were not preserved by surrogateescape: {paths!r}")
    json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def t_empty_diff_floors_standard() -> None:
    with repository() as (repo, base):
        result = derive(repo, base)
    check(result["files"] == [] and result["floor"] == M.STANDARD,
          f"an empty/uncertain diff must floor to STANDARD, never a vacuous no-floor: {result!r}")


def t_bad_inputs_and_git_failures_emit_no_partial_json() -> None:
    with repository() as (repo, base):
        head = os.fsdecode(git(repo, "rev-parse", "HEAD").stdout).strip()
        cases = (
            ["derive", "--worktree", str(repo), "--base", base, "--head-sha", "short"],
            ["derive", "--worktree", str(repo), "--base=--not-a-ref", "--head-sha", head],
            ["derive", "--worktree", str(repo / "missing"), "--base", base, "--head-sha", head],
        )
        for argv in cases:
            code, out, err = capture_cli(M.main, list(argv))
            check(code == M.EXIT_REFUSED and out == "" and "REFUSED" in err,
                  f"bad input must fail atomically with no JSON: {argv!r} -> {code}/{out!r}/{err!r}")


def t_raw_parser_refuses_partial_records() -> None:
    malformed = (
        b"not-a-header\0path\0",
        b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0",
        b":10064x 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0path\0",
        b":100644 100644 short " + b"b" * 40 + b" M\0path\0",
    )
    for raw in malformed:
        try:
            M._parse_raw(raw)
        except M.TriageError:
            pass
        else:
            raise M.SelfTestFailure(f"malformed raw diff was accepted: {raw!r}")


def t_output_head_is_full_live_sha() -> None:
    with repository() as (repo, base):
        write(repo, "README.md", "# Read me\n")
        head = commit(repo, "readme")
        result = derive(repo, base)
    check(result["head_sha"] == head and M.SHA_RE.fullmatch(result["head_sha"]) is not None,
          f"output must carry the exact live 40-character HEAD: {result!r}")
    check(result["diff_base_sha"] == base, "linear fixture's diff base must be the base commit")


def t_blob_read_failure_on_existing_side_refuses() -> None:
    """`git show` failing for a REGULAR-file side the raw diff already named is unreadable evidence, never
    benign absence (a blob-filtered clone with the promisor down exits 128 'bad object' while `git diff`
    still parses at tree level). It must fail CLOSED — TriageError, exit 2, no JSON — like every other Git
    call, not silently read as empty prose that would drop stripped agent frontmatter under the floor."""
    base_sha = "a" * 40
    head_sha = "b" * 40
    # One modification of a regular-mode docs file: both sides are regular blobs, so both are read.
    raw = b":100644 100644 " + b"c" * 40 + b" " + b"d" * 40 + b" M\0docs/operator.md\0"

    def runner(argv: list[str], _worktree: str) -> subprocess.CompletedProcess[bytes]:
        if argv == ["git", "rev-parse", "--verify", "HEAD^{commit}"]:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(head_sha + "\n"), b"")
        if argv[:4] == ["git", "rev-parse", "--verify", "--end-of-options"]:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(base_sha + "\n"), b"")
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(base_sha + "\n"), b"")
        if argv[:3] == ["git", "diff", "--raw"]:
            return subprocess.CompletedProcess(argv, 0, raw, b"")
        if argv[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object")
        return subprocess.CompletedProcess(argv, 99, b"", b"unexpected argv")

    with tempfile.TemporaryDirectory() as directory:
        try:
            M.derive(worktree=directory, base="main", head_sha=head_sha, runner=runner)
        except M.TriageError as exc:
            check("exited 128" in str(exc), f"a failed git show must fail closed with git detail: {exc}")
        else:
            raise M.SelfTestFailure("a failed git show on an existing regular side was read as empty prose")


def t_nonregular_side_read_failure_is_tolerated() -> None:
    """The mirror of the rule above: a NON-regular side (symlink/gitlink) is forced to CODE by mode and its
    content is never read, so a `git show` failure on it must NOT abort — mode already carries the class."""
    def runner(argv: list[str], _worktree: str) -> subprocess.CompletedProcess[bytes]:
        if argv[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object")
        return subprocess.CompletedProcess(argv, 0, b"plain prose\n", b"")

    change = M.Change(status="A", old_mode="000000", new_mode="120000", old_path=None, path=b"docs/guide.md")
    row = M._classify_change(change, runner, "/nonexistent", "a" * 40, "b" * 40)
    check(row["class"] == M.CODE,
          f"a symlink side must classify CODE without reading (or failing on) its blob: {row!r}")


def t_quoted_frontmatter_keys_are_code() -> None:
    """Agent frontmatter written with QUOTED YAML keys ("tools":, 'name':) is still agent frontmatter — a
    bare-letter-only match dropped the key and read the block as prose. Floor STANDARD, --tier TRIVIAL vetoed."""
    doc = '---\n"name": operator\n"tools": [read, write]\n---\nBody\n'
    with repository() as (repo, base):
        write(repo, "docs/operator-guide.md", doc)
        head = commit(repo, "quoted frontmatter")
        result = derive(repo, base)
        row = one_file(result)
        check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
              f"quoted-key agent frontmatter must be CODE and floor STANDARD: {result!r}")
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", head, "--tier", M.TRIVIAL])
    check(code == M.EXIT_REFUSED and out == "",
          f"--tier TRIVIAL must be refused for quoted-key agent frontmatter: {code}/{out!r}")
    check("below the mechanical floor" in err, f"the refusal must name the floor: {err!r}")


def t_frontmatter_runs_for_all_prose_extensions() -> None:
    """The agent-frontmatter escape runs for every prose-like extension _is_human_doc accepts, not just
    `.md`: a docs/*.txt or *.rst carrying agent frontmatter is CODE, not prose that clears the floor."""
    doc = "---\nname: operator\ndescription: agent rules\ntools: read\n---\nBody\n"
    for name in ("docs/operator.txt", "docs/operator.rst"):
        with repository() as (repo, base):
            write(repo, name, doc)
            commit(repo, "prose doc with agent frontmatter")
            result = derive(repo, base)
        row = one_file(result)
        check(row["class"] == M.CODE and result["floor"] == M.STANDARD,
              f"{name} carrying agent frontmatter must be CODE and floor STANDARD: {result!r}")


def t_frontmatter_closing_past_line_100_is_code() -> None:
    """The closing-delimiter scan must not truncate at a fixed line: agent frontmatter whose closing `---`
    sits well past line 100 must still classify CODE, not read as prose because the scan gave up early."""
    filler = "".join(f"note-{i}: line\n" for i in range(150))
    doc = "---\nname: operator\ndescription: agent rules\ntools: read\n" + filler + "---\nBody\n"
    with repository() as (repo, base):
        write(repo, "docs/long.md", doc)
        commit(repo, "long frontmatter")
        result = derive(repo, base)
    check(one_file(result)["class"] == M.CODE and result["floor"] == M.STANDARD,
          f"agent frontmatter closing past line 100 must still be CODE: {result!r}")


def t_unterminated_frontmatter_fails_closed_to_code() -> None:
    """An opening `---` with NO closing delimiter anywhere is a malformed/unterminated frontmatter block:
    fail closed to CODE rather than silently reading the whole file as prose."""
    doc = "---\nname: operator\ntools: read\nno closing fence in this file\n"
    cls, reasons = M._path_class("docs/broken.md", doc.encode("utf-8"))
    check(cls == M.CODE and any("frontmatter" in r for r in reasons),
          f"unterminated frontmatter must fail closed to CODE: {cls}: {reasons}")


CASES = [
    ("human-doc", "an all-prose diff has no floor — the tool never grants TRIVIAL", t_human_docs_have_no_floor),
    ("human-names", "top-level README/CHANGELOG/LICENSE and prose suffixes are HUMAN-DOC", t_top_level_human_doc_names),
    ("prose-named-source", "prose-named source suffixes (CHANGELOG.py, license.go) are CODE", t_prose_named_source_is_code),
    ("code-unknown", "source and unknown paths are CODE and floor STANDARD", t_source_and_unknown_are_standard),
    ("agent-frontmatter", "Markdown carrying skill/agent frontmatter is CODE", t_agent_frontmatter_is_code),
    ("agent-paths", "agent instructions, skill references, .claude and prompts are CODE", t_agent_paths_are_code),
    ("sensitive-classes", "CI/scripts/manifests/IaC/auth/build paths are SENSITIVE", t_sensitive_classes_are_high),
    ("mixed", "mixed files use the highest content class", t_mixed_content_uses_highest_class),
    ("executable-mode", "adding or removing executable mode is HIGH", t_executable_mode_add_and_remove_are_high),
    ("rename", "a rename classifies old and new paths", t_rename_classifies_both_paths),
    ("rename-sensitive", "renaming away from a sensitive path remains HIGH", t_rename_from_sensitive_remains_high),
    ("delete-sensitive", "deleting a sensitive file remains HIGH", t_sensitive_deletion_is_high),
    ("delete-frontmatter", "deleted Markdown uses base content for frontmatter", t_deleted_agent_frontmatter_is_code),
    ("symlink-doc", "a symlink at a docs path is CODE, floors STANDARD, refuses TRIVIAL", t_symlink_at_human_doc_path_is_code),
    ("nonregular-modes", "symlink/gitlink/unrecognized modes are never prose", t_nonregular_modes_are_never_prose),
    ("modify-both-sides", "a modification classifies base and head; frontmatter strip stays CODE", t_modification_classifies_base_and_head),
    ("typechange-both-sides", "a type change classifies base and head", t_type_change_classifies_base_and_head),
    ("no-trivial-floor", "the tool never emits a TRIVIAL floor; all-prose is no-floor", t_tool_never_emits_trivial_floor),
    ("tier-veto", "a --tier below the floor is refused; at/above passes", t_tier_below_floor_is_refused),
    ("head-mismatch", "a stale expected head is refused without JSON", t_head_mismatch_is_refused_without_output),
    ("head-moving", "HEAD moving during evidence collection is refused", t_moving_head_is_refused),
    ("deterministic", "file ordering and JSON bytes are deterministic", t_deterministic_order_and_bytes),
    ("hostile-paths", "hostile and non-UTF8 Git paths remain inert data", t_hostile_paths_are_data),
    ("empty-diff", "an empty diff floors to STANDARD rather than a vacuous no-floor", t_empty_diff_floors_standard),
    ("atomic-refusal", "input and Git failures emit no partial JSON", t_bad_inputs_and_git_failures_emit_no_partial_json),
    ("raw-partial", "malformed or partial raw Git records are refused", t_raw_parser_refuses_partial_records),
    ("head-output", "output is pinned to the full live HEAD", t_output_head_is_full_live_sha),
    ("blob-read-fail", "a failed git show on an existing regular side fails closed (exit 2)", t_blob_read_failure_on_existing_side_refuses),
    ("nonregular-read-skip", "a non-regular side is CODE by mode; its blob is never read", t_nonregular_side_read_failure_is_tolerated),
    ("quoted-frontmatter", "quoted YAML frontmatter keys still classify CODE", t_quoted_frontmatter_keys_are_code),
    ("frontmatter-extensions", "the frontmatter check runs for .txt/.rst prose too", t_frontmatter_runs_for_all_prose_extensions),
    ("frontmatter-long", "frontmatter closing past line 100 still classifies CODE", t_frontmatter_closing_past_line_100_is_code),
    ("frontmatter-unterminated", "an unterminated frontmatter block fails closed to CODE", t_unterminated_frontmatter_fails_closed_to_code),
]


def run_cases() -> int:
    failures = 0
    for name, description, fn in CASES:
        try:
            fn()
            print(f"PASS {name}: {description}")
        except Exception as exc:  # noqa: BLE001 - fixture runner must report every case
            failures += 1
            print(f"FAIL {name}: {description}: {exc}")
    print(f"triage fixtures: {len(CASES) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_cases())
