#!/usr/bin/env python3
"""Derive campaign's deterministic file-class risk tier for one PR head."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, NoReturn

from _gauntlet.modules import load_module_from_path


TRIVIAL, STANDARD, HIGH = "TRIVIAL", "STANDARD", "HIGH"
HUMAN_DOC, CODE, SENSITIVE = "HUMAN-DOC", "CODE", "SENSITIVE"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ROOT_DOC_REFERENCE_RE = re.compile(r"`(docs/[^`\r\n]+)`")

PROSE_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
REGULAR_MODES = {"100644", "100755"}
ROOT_INSTRUCTION_PATHS = ("AGENTS.md", "CLAUDE.md")
DEPENDENCY_NAMES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "deno.json", "deno.lock", "pyproject.toml", "poetry.lock", "pdm.lock",
    "pipfile", "pipfile.lock", "requirements.txt", "uv.lock", "go.mod", "go.sum", "cargo.toml",
    "cargo.lock", "gemfile", "gemfile.lock", "composer.json", "composer.lock", "pom.xml",
    "build.gradle", "build.gradle.kts", "gradle.lockfile", "pubspec.yaml", "pubspec.lock",
}
IAC_SUFFIXES = {".tf", ".tfvars", ".hcl"}
IAC_SEGMENTS = {"ansible", "cloudformation", "helm", "infra", "infrastructure", "k8s", "terraform"}
SECURITY_WORDS = {
    "auth", "authentication", "authorization", "authorize", "credential", "credentials", "crypto",
    "cryptography", "oauth", "oidc", "saml", "secret", "secrets",
}
AGENT_FRONTMATTER_KEYS = {
    "allowed-tools", "argument-hint", "disable-model-invocation", "model", "tools",
}


class TriageError(Exception):
    """The caller or Git checkout cannot support a trustworthy tier derivation."""


@dataclass(frozen=True)
class Change:
    old_mode: str
    new_mode: str
    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class Classified:
    path: str
    file_class: str
    reasons: tuple[str, ...]


def fail(message: str) -> NoReturn:
    print(f"triage: {message}", file=sys.stderr)
    raise SystemExit(2)


def git(worktree: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", os.fspath(worktree), *args], capture_output=True, check=False
    )
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stderr).strip()
        raise TriageError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {detail}")
    return proc.stdout


def one_lf(data: bytes, what: str) -> bytes:
    if not data.endswith(b"\n"):
        raise TriageError(f"{what} returned no terminating LF")
    value = data[:-1]
    if not value:
        raise TriageError(f"{what} returned an empty value")
    return value


def parse_raw(data: bytes) -> list[Change]:
    """Parse `git diff --raw -z`; paths stay NUL-delimited and are never shell-decoded."""
    if not data:
        return []
    fields = data.split(b"\0")
    if fields[-1] != b"":
        raise TriageError("git diff --raw -z returned an unterminated path")
    fields.pop()
    changes: list[Change] = []
    i = 0
    while i < len(fields):
        header = os.fsdecode(fields[i])
        i += 1
        parts = header.removeprefix(":").split()
        if not header.startswith(":") or len(parts) != 5:
            raise TriageError(f"cannot parse raw diff header {header!r}")
        old_mode, new_mode, _old_oid, _new_oid, status = parts
        if i >= len(fields):
            raise TriageError(f"raw diff header {header!r} has no path")
        first = os.fsdecode(fields[i])
        i += 1
        if status[:1] in {"R", "C"}:
            if i >= len(fields):
                raise TriageError(f"raw diff {status} entry has no destination path")
            second = os.fsdecode(fields[i])
            i += 1
            changes.append(Change(old_mode, new_mode, status, second, first))
        else:
            changes.append(Change(old_mode, new_mode, status, first))
    return changes


def root_agent_doc_paths(worktree: Path, revision: str) -> frozenset[str]:
    """Read exact `docs/**` path references from root instructions at one pinned revision."""
    data = git(worktree, "ls-tree", "-z", revision, "--", *ROOT_INSTRUCTION_PATHS)
    if not data:
        return frozenset()
    if not data.endswith(b"\0"):
        raise TriageError("git ls-tree returned an unterminated root instruction entry")
    references: set[str] = set()
    for record in data[:-1].split(b"\0"):
        try:
            metadata, raw_path = record.split(b"\t", 1)
        except ValueError as exc:
            raise TriageError(f"cannot parse root instruction entry {record!r}") from exc
        parts = metadata.split()
        if len(parts) != 3:
            raise TriageError(f"cannot parse root instruction metadata {metadata!r}")
        _mode, object_type, _oid = parts
        path = os.fsdecode(raw_path)
        if path not in ROOT_INSTRUCTION_PATHS or object_type != b"blob":
            raise TriageError(f"root instruction {path!r} is not a readable blob")
        content = git(worktree, "show", f"{revision}:{path}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TriageError(f"root instruction {path!r} is not UTF-8") from exc
        for match in ROOT_DOC_REFERENCE_RE.finditer(text):
            reference = match.group(1)
            parsed = PurePosixPath(reference)
            if (
                parsed.parts[:1] == ("docs",)
                and parsed.as_posix() == reference
                and not any(char in reference for char in "*?[]")
                and not ({".", ".."} & set(parsed.parts))
            ):
                references.add(reference)
    return frozenset(references)


def executable(mode: str) -> bool:
    try:
        return bool(int(mode, 8) & 0o111)
    except ValueError as exc:
        raise TriageError(f"git reported invalid mode {mode!r}") from exc


def words(path: PurePosixPath) -> set[str]:
    return {
        word for part in path.parts for word in re.split(r"[^a-z0-9]+", part.lower()) if word
    }


def agent_frontmatter(content: bytes | None) -> bool:
    if content is None:
        return False
    if content.startswith(b"---\n"):
        body_start = 4
    elif content.startswith(b"---\r\n"):
        body_start = 5
    else:
        return False
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return True  # uncertain human prose defaults to CODE
    body = text[body_start:]
    end = re.search(r"^---(?:\r?\n|\Z)", body, re.MULTILINE)
    if end is None:
        return False
    keys = {
        line.split(":", 1)[0].strip().lower()
        for line in body[:end.start()].splitlines()
        if ":" in line
    }
    return bool(keys & AGENT_FRONTMATTER_KEYS) or {"name", "description"} <= keys


def agent_doc(
    path: PurePosixPath, content: bytes | None, referenced_paths: frozenset[str] = frozenset()
) -> bool:
    lower_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    if path.as_posix() in referenced_paths:
        return True
    if name in {"skill.md", "agents.md", "claude.md"}:
        return True
    if ".claude" in lower_parts or "references" in lower_parts:
        return True
    if any(word in name for word in ("prompt", "agent-instruction", "instructions")):
        return True
    return path.suffix.lower() in {".md", ".mdx"} and agent_frontmatter(content)


def human_doc(
    path: PurePosixPath, content: bytes | None, referenced_paths: frozenset[str] = frozenset()
) -> bool:
    if agent_doc(path, content, referenced_paths):
        return False
    if len(path.parts) == 1 and (
        path.name == "README.md" or path.name.upper().startswith("CHANGELOG")
        or path.name.upper().startswith("LICENSE")
    ):
        return path.suffix.lower() in PROSE_SUFFIXES or path.suffix == ""
    return bool(path.parts and path.parts[0].lower() == "docs" and path.suffix.lower() in PROSE_SUFFIXES)


def sensitive(path: PurePosixPath, old_mode: str, new_mode: str) -> list[str]:
    lower_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    reasons: list[str] = []
    if lower_parts[:1] == (".github",):
        reasons.append("CI path")
    if "scripts" in lower_parts:
        reasons.append("scripts path")
    if executable(old_mode) or executable(new_mode):
        reasons.append("executable mode")
    if name in {"dockerfile", "makefile", "gnumakefile"} or name.startswith("dockerfile."):
        reasons.append("build entrypoint")
    if name in DEPENDENCY_NAMES or name.endswith((".lock", ".lockfile")):
        reasons.append("dependency manifest or lockfile")
    if path.suffix.lower() in IAC_SUFFIXES or bool(set(lower_parts) & IAC_SEGMENTS):
        reasons.append("infrastructure-as-code path")
    if words(path) & SECURITY_WORDS or path.suffix.lower() in {".key", ".pem"}:
        reasons.append("auth, crypto, credential, or secret path")
    return reasons


ContentReader = Callable[[str, bool], bytes | None]


def classify_change(
    change: Change,
    read_content: ContentReader,
    referenced_paths: frozenset[str] = frozenset(),
) -> Classified:
    candidates = [(change.path, False)]
    if change.old_path is not None:
        candidates.append((change.old_path, True))
    best_class = HUMAN_DOC
    reasons: list[str] = []
    for raw, old in candidates:
        path = PurePosixPath(raw)
        use_old = old or change.status.startswith("D")
        mode = change.old_mode if use_old else change.new_mode
        sensitive_reasons = sensitive(path, change.old_mode, change.new_mode)
        if sensitive_reasons:
            best_class = SENSITIVE
            reasons.extend(f"{raw}: {reason}" for reason in sensitive_reasons)
            continue
        if mode not in REGULAR_MODES:
            if best_class != SENSITIVE:
                best_class = CODE
            reasons.append(f"{raw}: non-regular mode {mode}")
            continue
        content = read_content(raw, use_old)
        if content is None:
            side = "base" if use_old else "head"
            raise TriageError(f"cannot read regular blob {raw!r} from {side}")
        if not human_doc(path, content, referenced_paths) and best_class != SENSITIVE:
            best_class = CODE
            reason = (
                "agent-consumed document"
                if agent_doc(path, content, referenced_paths)
                else "not human-facing prose"
            )
            reasons.append(f"{raw}: {reason}")
        elif best_class == HUMAN_DOC:
            reasons.append(f"{raw}: human-facing prose")
    rendered_path = f"{change.old_path} -> {change.path}" if change.old_path else change.path
    return Classified(rendered_path, best_class, tuple(dict.fromkeys(reasons)))


def tier_for(files: list[Classified], systemic: bool) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if systemic:
        reasons.append("caller marked the change systemic, cross-package, or root-cause")
    if any(item.file_class == SENSITIVE for item in files):
        reasons.append("at least one changed file is SENSITIVE")
    if systemic or any(item.file_class == SENSITIVE for item in files):
        return HIGH, reasons
    if not files:
        return STANDARD, ["no changed files were available; uncertainty defaults to STANDARD"]
    if all(item.file_class == HUMAN_DOC for item in files):
        return TRIVIAL, ["every changed file is HUMAN-DOC"]
    return STANDARD, ["at least one changed file is CODE and none is SENSITIVE"]


def derive(worktree: Path, base: str, head_sha: str, systemic: bool = False) -> dict:
    if not worktree.is_absolute() or not worktree.is_dir():
        raise TriageError(f"--worktree must be an existing absolute directory: {worktree}")
    if not SHA_RE.fullmatch(head_sha):
        raise TriageError(f"--head-sha must be 40 lowercase hex, got {head_sha!r}")
    live = os.fsdecode(one_lf(git(worktree, "rev-parse", "HEAD"), "git rev-parse HEAD"))
    if live != head_sha:
        raise TriageError(f"worktree HEAD is {live}, not ledger head_sha {head_sha}")
    merge_base = os.fsdecode(one_lf(git(worktree, "merge-base", base, head_sha), "git merge-base"))
    changes = parse_raw(git(worktree, "diff", "--raw", "-z", "--no-abbrev", merge_base, head_sha))
    referenced_paths = root_agent_doc_paths(worktree, head_sha)

    def read_content(path: str, old: bool) -> bytes | None:
        revision = merge_base if old else head_sha
        return git(worktree, "show", f"{revision}:{path}")

    files = sorted(
        (classify_change(change, read_content, referenced_paths) for change in changes),
        key=lambda item: item.path,
    )
    tier, reasons = tier_for(files, systemic)
    return {
        "base": base,
        "merge_base": merge_base,
        "head_sha": head_sha,
        "tier": tier,
        "required_reviews": 1 if tier == TRIVIAL else 2,
        "systemic": systemic,
        "reasons": reasons,
        "files": [
            {"path": item.path, "class": item.file_class, "reasons": list(item.reasons)} for item in files
        ],
    }


def self_test() -> int:
    path = Path(__file__).with_name("triage-test.py")
    module = load_module_from_path("triage_test", path)
    if module is None:
        fail(f"self-test fixture is missing at {path}")
    return module.run(sys.modules[__name__])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    command = sub.add_parser("derive", help="classify the base...head diff and print the tier as JSON")
    command.add_argument("--worktree", required=True, type=Path)
    command.add_argument("--base", required=True, help="the fetched base ref, usually origin/<base_branch>")
    command.add_argument("--head-sha", required=True, help="the ledger's full PR head SHA")
    command.add_argument(
        "--systemic", action="store_true",
        help="escalate to HIGH for a systemic, cross-package, or root-cause change",
    )
    sub.add_parser("self-test", help="run the sibling fixture suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    try:
        result = derive(args.worktree, args.base, args.head_sha, args.systemic)
    except TriageError as exc:
        fail(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
