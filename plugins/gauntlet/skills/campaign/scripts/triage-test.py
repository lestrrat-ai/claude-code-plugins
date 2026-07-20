#!/usr/bin/env python3
"""Fixtures for ``triage.py`` — deterministic campaign tier derivation.

The suite uses real temporary Git repositories for the file, mode, rename, delete, ordering and hostile
path cases.  It loads the owner by path and therefore fails loudly when the executable policy owner is
missing.  That missing-owner failure was the pre-change reproduction: campaign previously had only prose
classification, so no command could reject a stale head or consistently detect these classes.
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


def derive(repo: Path, base: str, *, systemic: str = "no") -> dict:
    head = os.fsdecode(git(repo, "rev-parse", "HEAD").stdout).strip()
    return M.derive(worktree=str(repo), base=base, head_sha=head, systemic=systemic)


def one_file(result: dict) -> dict:
    check(len(result["files"]) == 1, f"expected one changed file, got {result['files']!r}")
    return result["files"][0]


def t_human_docs_are_trivial() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        commit(repo, "docs")
        result = derive(repo, base)
    check(result["tier"] == M.TRIVIAL and result["required_reviews"] == 1,
          f"human prose must be TRIVIAL/1, got {result!r}")
    check(one_file(result)["class"] == M.HUMAN_DOC, "docs/guide.md must be HUMAN-DOC")


def t_top_level_human_doc_names() -> None:
    for name in ("README.md", "CHANGELOG.md", "LICENSE"):
        cls, reasons = M._path_class(name, b"plain prose\n")
        check(cls == M.HUMAN_DOC, f"{name} must be HUMAN-DOC, got {cls}: {reasons}")


def t_source_and_unknown_are_standard() -> None:
    with repository() as (repo, base):
        write(repo, "src/widget.py", "value = 1\n")
        write(repo, "assets/blob.weird", "x\n")
        commit(repo, "code")
        result = derive(repo, base)
    check(result["tier"] == M.STANDARD and result["required_reviews"] == 2,
          f"code/unknown content must be STANDARD/2, got {result!r}")
    check({row["class"] for row in result["files"]} == {M.CODE}, "both paths must classify CODE")


def t_agent_frontmatter_is_code() -> None:
    with repository() as (repo, base):
        write(repo, "docs/operator.md", "---\nname: operator\ndescription: agent behavior\n---\nBody\n")
        commit(repo, "agent doc")
        result = derive(repo, base)
    row = one_file(result)
    check(result["tier"] == M.STANDARD and row["class"] == M.CODE,
          f"agent-frontmatter Markdown must be CODE/STANDARD, got {result!r}")
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
    check(result["tier"] == M.HIGH, f"one sensitive file must raise the mixed diff to HIGH: {result!r}")
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
        check(result["tier"] == M.HIGH and row["class"] == M.SENSITIVE,
              f"mode {oct(before)}->{oct(after)} must be HIGH/SENSITIVE: {result!r}")
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
    check(row["class"] == M.CODE and result["tier"] == M.STANDARD,
          "a rename from human docs into source must classify both paths and become STANDARD")


def t_rename_from_sensitive_remains_high() -> None:
    with repository({"scripts/tool.py": ("print('x')\n", 0o644)}) as (repo, base):
        (repo / "docs").mkdir()
        (repo / "scripts/tool.py").rename(repo / "docs/tool.md")
        commit(repo, "rename sensitive")
        result = derive(repo, base)
    check(result["tier"] == M.HIGH and one_file(result)["class"] == M.SENSITIVE,
          f"touching the old sensitive path must remain HIGH: {result!r}")


def t_sensitive_deletion_is_high() -> None:
    with repository({"scripts/obsolete.py": ("print('x')\n", 0o644)}) as (repo, base):
        (repo / "scripts/obsolete.py").unlink()
        commit(repo, "delete")
        result = derive(repo, base)
    row = one_file(result)
    check(row["status"] == "D" and row["old_path"] == "scripts/obsolete.py",
          f"deletion must retain its old path: {row!r}")
    check(result["tier"] == M.HIGH, "deleting a sensitive file is still a sensitive change")


def t_deleted_agent_frontmatter_is_code() -> None:
    frontmatter = "---\nname: old-skill\ndescription: old agent rules\n---\nBody\n"
    with repository({"docs/old.md": (frontmatter, 0o644)}) as (repo, base):
        (repo / "docs/old.md").unlink()
        commit(repo, "delete agent doc")
        result = derive(repo, base)
    check(result["tier"] == M.STANDARD and one_file(result)["class"] == M.CODE,
          f"deleted Markdown must inspect base content for agent frontmatter: {result!r}")


def t_systemic_input_only_raises() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        commit(repo, "docs")
        yes = derive(repo, base, systemic="yes")
        unknown = derive(repo, base, systemic="unknown")
    check(yes["tier"] == M.HIGH and not yes["systemic_unresolved"],
          f"systemic=yes must raise human docs to HIGH: {yes!r}")
    check(unknown["tier"] == M.STANDARD and unknown["systemic_unresolved"],
          f"systemic=unknown must fail safe to STANDARD and stay unresolved: {unknown!r}")

    with repository() as (repo, base):
        write(repo, "scripts/tool.py", "print('x')\n")
        commit(repo, "sensitive")
        no = derive(repo, base, systemic="no")
    check(no["tier"] == M.HIGH, f"systemic=no must never lower content-driven HIGH: {no!r}")


def t_head_mismatch_is_refused_without_output() -> None:
    with repository() as (repo, base):
        write(repo, "docs/guide.md", "# Guide\n")
        head = commit(repo, "docs")
        wrong = ("0" if head[0] != "0" else "1") + head[1:]
        code, out, err = capture_cli(M.main, [
            "derive", "--worktree", str(repo), "--base", base, "--head-sha", wrong,
            "--systemic", "no",
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
            M.derive(worktree=directory, base="main", head_sha=sha_a, systemic="no", runner=runner)
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


def t_empty_diff_is_standard() -> None:
    with repository() as (repo, base):
        result = derive(repo, base)
    check(result["files"] == [] and result["tier"] == M.STANDARD and result["required_reviews"] == 2,
          f"an empty/uncertain diff must never become vacuously TRIVIAL: {result!r}")


def t_bad_inputs_and_git_failures_emit_no_partial_json() -> None:
    with repository() as (repo, base):
        head = os.fsdecode(git(repo, "rev-parse", "HEAD").stdout).strip()
        cases = (
            ["derive", "--worktree", str(repo), "--base", base, "--head-sha", "short", "--systemic", "no"],
            ["derive", "--worktree", str(repo), "--base=--not-a-ref", "--head-sha", head, "--systemic", "no"],
            ["derive", "--worktree", str(repo / "missing"), "--base", base, "--head-sha", head, "--systemic", "no"],
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


CASES = [
    ("human-doc", "human-facing prose is TRIVIAL with one required review", t_human_docs_are_trivial),
    ("human-names", "top-level README/CHANGELOG/LICENSE are HUMAN-DOC", t_top_level_human_doc_names),
    ("code-unknown", "source and unknown paths are CODE/STANDARD", t_source_and_unknown_are_standard),
    ("agent-frontmatter", "Markdown carrying skill/agent frontmatter is CODE", t_agent_frontmatter_is_code),
    ("agent-paths", "agent instructions, skill references, .claude and prompts are CODE", t_agent_paths_are_code),
    ("sensitive-classes", "CI/scripts/manifests/IaC/auth/build paths are SENSITIVE", t_sensitive_classes_are_high),
    ("mixed", "mixed files use the highest content class", t_mixed_content_uses_highest_class),
    ("executable-mode", "adding or removing executable mode is HIGH", t_executable_mode_add_and_remove_are_high),
    ("rename", "a rename classifies old and new paths", t_rename_classifies_both_paths),
    ("rename-sensitive", "renaming away from a sensitive path remains HIGH", t_rename_from_sensitive_remains_high),
    ("delete-sensitive", "deleting a sensitive file remains HIGH", t_sensitive_deletion_is_high),
    ("delete-frontmatter", "deleted Markdown uses base content for frontmatter", t_deleted_agent_frontmatter_is_code),
    ("systemic", "systemic judgment raises but never lowers content tier", t_systemic_input_only_raises),
    ("head-mismatch", "a stale expected head is refused without JSON", t_head_mismatch_is_refused_without_output),
    ("head-moving", "HEAD moving during evidence collection is refused", t_moving_head_is_refused),
    ("deterministic", "file ordering and JSON bytes are deterministic", t_deterministic_order_and_bytes),
    ("hostile-paths", "hostile and non-UTF8 Git paths remain inert data", t_hostile_paths_are_data),
    ("empty-diff", "an empty diff defaults STANDARD rather than vacuous TRIVIAL", t_empty_diff_is_standard),
    ("atomic-refusal", "input and Git failures emit no partial JSON", t_bad_inputs_and_git_failures_emit_no_partial_json),
    ("raw-partial", "malformed or partial raw Git records are refused", t_raw_parser_refuses_partial_records),
    ("head-output", "output is pinned to the full live HEAD", t_output_head_is_full_live_sha),
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
