#!/usr/bin/env python3
# ci: pyright
"""Emit the mechanical FLOOR and file inventory for one immutable PR-head diff.

``references/stage-2-review-gate.md`` (``2a-triage``) owns the policy.  This command is a mechanical
INPUT to that policy with ESCALATE-ONLY authority, never the tier decision.  It resolves a base, reads one
raw Git diff pinned to the caller's expected 40-character HEAD, classifies every changed path and mode,
then proves HEAD did not move while the evidence was read.  From that it emits a per-file inventory and a
FLOOR tier — the minimum the mechanics compel:

  * any SENSITIVE file  -> floor ``HIGH``;
  * any non-HUMAN-DOC file (or an empty/unresolved diff) -> floor ``STANDARD``;
  * nothing but human prose -> ``null`` floor (no floor: the orchestrator decides).

It is STRUCTURALLY INCAPABLE of emitting a ``TRIVIAL`` floor: ``TRIVIAL`` is only ever the orchestrator's
semantic "is this all human prose?" call.  The optional ``--tier`` lets a caller present the tier it
DECIDED; the command then acts as a LOWER-BOUND check and REFUSES a tier below the floor (veto-downward).
It never grants a tier and never lowers one.

    triage.py derive --worktree <path> --base <ref> --head-sha <40-hex> [--tier TRIVIAL|STANDARD|HIGH]
        [--file <ledger> --pr <N>]
    triage.py self-test

When ``--file <ledger> --pr <N>`` is given, ``--base`` is an ASSERTION checked against the selected row's
effective base (its explicit ``base_branch``, else the legacy header fallback), never an independent base
source; a disagreement refuses.  Omit them and ``--base`` is used exactly as before.

Success prints one deterministic JSON object and exits 0.  Any Git failure, malformed evidence, stale
expected head, moving head, or a ``--tier`` below the floor prints no JSON, explains the refusal on
stderr, and exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "triage-test.py"
LEDGER_PY = _HERE / "ledger.py"


def _load_ledger():
    mod = load_module_from_path("triage_ledger", LEDGER_PY)
    if mod is None:
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER_PY}")
    return mod


L = _load_ledger()

EXIT_OK = 0
EXIT_REFUSED = 2

HUMAN_DOC = "HUMAN-DOC"
CODE = "CODE"
SENSITIVE = "SENSITIVE"

TRIVIAL = "TRIVIAL"
STANDARD = "STANDARD"
HIGH = "HIGH"

# Tier order, low to high. The floor is only ever STANDARD, HIGH, or None (no floor); TRIVIAL is never a
# floor. A None floor ranks below every tier, so any DECIDED tier — including the orchestrator's TRIVIAL —
# clears it. The --tier veto compares a driver's decided tier against the floor's rank.
_TIER_RANK = {TRIVIAL: 0, STANDARD: 1, HIGH: 2}
TIER_VALUES = frozenset(_TIER_RANK)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_HUMAN_SUFFIXES = frozenset({".md", ".mdown", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc"})
# Suffixes a top-level CHANGELOG/LICENSE stem may carry and still be prose (a bare stem has none). A stem
# with any other suffix is source-like or unknown and classifies CODE, never HUMAN-DOC.
_PROSE_DOC_SUFFIXES = frozenset({"", ".md", ".txt", ".rst"})
_AGENT_DOC_BASENAMES = frozenset({"skill.md", "agents.md", "claude.md"})
_AGENT_TOKENS = frozenset({"agent", "agents", "instruction", "instructions", "prompt", "prompts"})

_DEPENDENCY_NAMES = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "deno.lock", "pyproject.toml", "poetry.lock", "pipfile",
    "pipfile.lock", "setup.py", "setup.cfg", "uv.lock", "go.mod", "go.sum", "go.work",
    "go.work.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock", "composer.json",
    "composer.lock", "pom.xml", "gradle.lockfile", "mix.exs", "mix.lock", "pubspec.yaml",
    "pubspec.lock", "package.swift", "package.resolved", "nuget.config", "packages.lock.json",
    "environment.yml", "environment.yaml", "conda.yml", "conda.yaml",
})
_DEPENDENCY_SUFFIXES = (".csproj", ".fsproj", ".vbproj")
_IAC_SUFFIXES = frozenset({".tf", ".tfvars", ".hcl"})
_IAC_PARTS = frozenset({"terraform", "pulumi", "k8s", "kubernetes", "helm", "ansible"})
_PULUMI_MANIFEST_RE = re.compile(r"pulumi(?:\.[a-z0-9_.-]+)?\.ya?ml")
_SENSITIVE_TOKENS = frozenset({
    "auth", "authentication", "authorization", "oauth", "oidc", "jwt", "crypto", "cryptography",
    "secret", "secrets", "credential", "credentials", "key", "keys", "certificate", "certificates",
})
_FRONTMATTER_AGENT_KEYS = frozenset({"agent", "agents", "model", "tools", "skills"})

# One top-level key of a plain BLOCK-style YAML mapping line: bare (``tools:``), unescaped double-quoted, or
# single-quoted (``"tools":`` / ``'name':``), each allowed standard leading indentation. A quoted key is
# valid YAML that a bare-letter-only match would silently drop, reading agent frontmatter as prose and
# clearing the floor. A double-quoted key containing any escape is NOT decoded by this line parser, so it
# does not match and ``_frontmatter_top_level_keys`` fails the interior closed to CODE. This matches a
# block-mapping key line ONLY; a flow mapping (``{name: …}``) and every other non-block surface form also
# fails closed there.
_FRONTMATTER_KEY_RE = re.compile(
    r"""^\s*
        (?:"(?P<dq>[^"\\]+)"
          |'(?P<sq>[^']+)'
          |(?P<bare>[A-Za-z][A-Za-z0-9_-]*))
        \s*:
    """,
    re.VERBOSE,
)


class TriageError(Exception):
    """The requested tier cannot be derived from a stable, complete Git view."""


class SelfTestFailure(AssertionError):
    """A rule this executable claims to enforce did not hold."""


@dataclass(frozen=True)
class Change:
    status: str
    old_mode: str
    new_mode: str
    old_path: bytes | None
    path: bytes


Runner = Callable[[list[str], str], subprocess.CompletedProcess[bytes]]


def _real_run(argv: list[str], worktree: str) -> subprocess.CompletedProcess[bytes]:
    """Run a typed argv in the supplied worktree and preserve filename bytes with surrogateescape."""
    return subprocess.run(argv, cwd=worktree, capture_output=True, check=False)  # noqa: S603


def _run(runner: Runner, argv: list[str], worktree: str, operation: str) -> bytes:
    try:
        proc = runner(argv, worktree)
    except OSError as exc:
        raise TriageError(f"{operation} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stderr).strip()
        suffix = f": {detail}" if detail else ""
        raise TriageError(f"{operation} exited {proc.returncode}{suffix}")
    return proc.stdout


def _oid(raw: bytes, operation: str) -> str:
    value = os.fsdecode(raw).strip()
    if not SHA_RE.fullmatch(value):
        raise TriageError(f"{operation} returned {value!r}, not a 40-character lowercase commit SHA")
    return value


def _head(runner: Runner, worktree: str) -> str:
    return _oid(
        _run(runner, ["git", "rev-parse", "--verify", "HEAD^{commit}"], worktree, "reading HEAD"),
        "reading HEAD",
    )


def _resolve_base(runner: Runner, worktree: str, base: str) -> str:
    if not base or "\x00" in base:
        raise TriageError("--base must name a non-empty Git revision")
    return _oid(
        _run(
            runner,
            ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
            worktree,
            f"resolving base {base!r}",
        ),
        f"resolving base {base!r}",
    )


def _merge_base(runner: Runner, worktree: str, base_sha: str, head_sha: str) -> str:
    return _oid(
        _run(
            runner,
            ["git", "merge-base", base_sha, head_sha],
            worktree,
            "resolving the base/head merge-base",
        ),
        "resolving the base/head merge-base",
    )


def _parse_raw(raw: bytes) -> list[Change]:
    """Parse ``git diff --raw -z`` without ever round-tripping path bytes through shell text."""
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    changes: list[Change] = []
    index = 0
    while index < len(tokens):
        header = tokens[index]
        index += 1
        if not header.startswith(b":"):
            raise TriageError("raw diff contains a record without a ':' header")
        fields = header[1:].split(b" ")
        if len(fields) != 5:
            raise TriageError("raw diff header does not contain mode, object, and status fields")
        old_mode_b, new_mode_b, old_oid, new_oid, status_b = fields
        if not re.fullmatch(rb"[0-7]{6}", old_mode_b) or not re.fullmatch(rb"[0-7]{6}", new_mode_b):
            raise TriageError("raw diff contains a malformed file mode")
        if not re.fullmatch(rb"[0-9a-f]{40}", old_oid) or not re.fullmatch(rb"[0-9a-f]{40}", new_oid):
            raise TriageError("raw diff contains an abbreviated or malformed object id")
        if not re.fullmatch(rb"[A-Z][0-9]*", status_b):
            raise TriageError("raw diff contains a malformed change status")
        kind = chr(status_b[0])
        path_count = 2 if kind in {"R", "C"} else 1
        if index + path_count > len(tokens):
            raise TriageError("raw diff ends before its path fields")
        paths = tokens[index:index + path_count]
        index += path_count
        if any(path == b"" for path in paths):
            raise TriageError("raw diff contains an empty path")
        old_path = paths[0] if path_count == 2 else (paths[0] if kind == "D" else None)
        path = paths[-1]
        changes.append(Change(
            status=os.fsdecode(status_b),
            old_mode=os.fsdecode(old_mode_b),
            new_mode=os.fsdecode(new_mode_b),
            old_path=old_path,
            path=path,
        ))
    return changes


def _split_path(path: str) -> tuple[str, ...]:
    """Git paths always use '/', including on Windows-hosted repositories."""
    return tuple(part for part in PurePosixPath(path).parts if part not in {"", "."})


def _tokens(value: str) -> set[str]:
    return {piece for piece in re.split(r"[^a-z0-9]+", value.lower()) if piece}


def _dependency_reason(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    if name in _DEPENDENCY_NAMES:
        return "dependency manifest or lockfile"
    # pip requirements/constraints: the compiled ``requirements*.txt`` lockfile AND the human-authored
    # ``requirements*.in`` source manifest pip-tools compiles from it; ``constraints*.txt`` pins alike.
    if name.startswith("requirements") and name.endswith((".txt", ".in")):
        return "dependency manifest or lockfile"
    if name.startswith("constraints") and name.endswith(".txt"):
        return "dependency manifest or lockfile"
    if name.startswith(("build.gradle", "settings.gradle")):
        return "dependency manifest or lockfile"
    if name.endswith(_DEPENDENCY_SUFFIXES):
        return "dependency manifest or lockfile"
    if name in {"plugin.json", "marketplace.json", "manifest.json"}:
        return "plugin or package manifest"
    return None


def _iac_reason(path: str) -> str | None:
    parts = tuple(part.lower() for part in _split_path(path))
    name = parts[-1] if parts else ""
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in _IAC_SUFFIXES or any(part in _IAC_PARTS for part in parts):
        return "infrastructure-as-code path"
    if (_PULUMI_MANIFEST_RE.fullmatch(name) or name == "chart.yaml"
            or re.fullmatch(r"(?:docker-)?compose(?:\.[^.]+)?\.ya?ml", name)):
        return "infrastructure-as-code manifest"
    return None


def _sensitive_path_reasons(path: str) -> list[str]:
    parts = tuple(part.lower() for part in _split_path(path))
    name = parts[-1] if parts else ""
    reasons: list[str] = []
    if ".github" in parts:
        reasons.append("CI path (.github/**)")
    if "scripts" in parts:
        reasons.append("script path (scripts/**)")
    if name in {"dockerfile", "makefile", "gnumakefile"} or name.startswith("dockerfile."):
        reasons.append("build entrypoint")
    dependency = _dependency_reason(path)
    if dependency:
        reasons.append(dependency)
    iac = _iac_reason(path)
    if iac:
        reasons.append(iac)
    path_tokens: set[str] = set()
    for part in parts:
        path_tokens.update(_tokens(part))
    if path_tokens & _SENSITIVE_TOKENS:
        reasons.append("auth, crypto, credential, key, or secret path")
    return reasons


def _agent_path_reason(path: str) -> str | None:
    parts = tuple(part.lower() for part in _split_path(path))
    name = parts[-1] if parts else ""
    if name in _AGENT_DOC_BASENAMES:
        return "agent-consumed instruction file"
    if ".claude" in parts:
        return "Claude agent configuration path"
    if "skills" in parts and "references" in parts and parts.index("skills") < parts.index("references"):
        return "skill reference"
    if any(_tokens(part) & _AGENT_TOKENS for part in parts):
        return "prompt or agent-instruction path"
    return None


def _frontmatter_top_level_keys(interior: list[str]) -> tuple[set[str], bool]:
    """Extract a YAML frontmatter interior's top-level mapping keys, treating it as a plain BLOCK mapping.

    Returns ``(keys, accountable)``. ``accountable`` is ``True`` only when every content line is one this
    line-based extractor can COMPLETELY account for as a plain block mapping: a top-level ``key:`` entry
    (bare or quoted), a comment, a blank, or an indented continuation of the current entry's value (a block
    sequence item, a nested mapping, block-scalar text). It is ``False`` for any interior that is not a plain
    block mapping — a top-level FLOW mapping (``{name: operator, description: …}``) or flow sequence, a block
    sequence document (``- item``), a bare scalar, a ``?`` complex key, a value's flow collection continued
    onto its own line (a ``]``/``}`` at column 0), or a continuation with no owning key.

    The stdlib carries no YAML parser, so rather than parse those forms — and rather than repeat the bug of
    misreading them as prose (each surface form was patched in turn: bare keys, then quoted keys, now flow),
    the caller fails a non-accountable interior CLOSED to CODE. "Cannot parse completely" means CODE, never
    prose. The failure is escalate-only and therefore safe: it can only raise a would-be HUMAN-DOC to CODE."""
    keys: set[str] = set()
    opened_key = False
    for line in interior:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # a blank line or a comment carries no structural content
        if line[:1] in (" ", "\t"):
            # An indented line continues the current top-level entry's value. It is accountable only once an
            # entry has opened it; indentation with no owning key is not a plain block mapping.
            if not opened_key:
                return set(), False
            continue
        match = _FRONTMATTER_KEY_RE.match(line)
        if match is None:
            # A column-0 line that is not a block ``key:`` entry: a flow mapping/sequence, a ``-`` sequence
            # item, a bare scalar, a ``?`` complex key, or a ``]``/``}`` closing a value's flow collection
            # that was continued across lines. None of these is a plain block mapping this extractor finishes.
            return set(), False
        key = match.group("dq") or match.group("sq") or match.group("bare")
        keys.add(key.lower())
        opened_key = True
    return keys, True


def _has_agent_frontmatter(content: bytes | None) -> bool:
    if content is None:
        return False
    text = content.decode("utf-8", "replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    # Scan the WHOLE head for the closing delimiter — never a fixed-size window that would let a block
    # closing past an arbitrary line read as prose. An opening ``---`` with no closing delimiter anywhere is
    # a malformed/unterminated frontmatter block: fail closed (treat as agent frontmatter -> CODE).
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return True
    keys, accountable = _frontmatter_top_level_keys(lines[1:closing])
    # An interior the block extractor cannot COMPLETELY account for — a flow mapping/sequence and every other
    # non-block surface form — is never read as prose: fail closed to CODE. The interior is now validated as
    # a plain block mapping rather than scanned line by line and assumed prose on any line the key regex
    # misses, which is the class each earlier round patched one surface form at a time.
    if not accountable:
        return True
    return bool(keys & _FRONTMATTER_AGENT_KEYS or {"name", "description"} <= keys)


def _is_top_level_prose(name: str) -> bool:
    """A top-level file whose NAME AND SUFFIX are both prose. The prose stems (``README``, ``CHANGELOG``,
    ``LICENSE``/``LICENSE-<variant>``) are only human docs with a prose suffix or none — a source-like or
    unknown suffix (``CHANGELOG.py``, ``license.go``) is NOT prose and falls through to CODE. Matching a
    stem by unbounded prefix would let any ``CHANGELOG*``/``LICENSE*`` source file clear the floor."""
    lower = name.lower()
    if lower == "readme.md":
        return True
    suffix = PurePosixPath(name).suffix.lower()
    stem = lower[: len(lower) - len(suffix)] if suffix else lower
    if suffix not in _PROSE_DOC_SUFFIXES:
        return False
    return stem == "changelog" or stem == "license" or stem.startswith("license-")


def _is_human_doc(path: str) -> bool:
    parts = _split_path(path)
    if not parts:
        return False
    name = parts[-1]
    if len(parts) == 1 and _is_top_level_prose(name):
        return True
    return parts[0].lower() == "docs" and PurePosixPath(name).suffix.lower() in _HUMAN_SUFFIXES


def _mode_executable(mode: str) -> bool:
    return mode != "000000" and bool(int(mode, 8) & 0o111)


_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})


def _mode_regular_blob(mode: str) -> bool:
    """A regular file blob (``100644``) or its executable form (``100755``). A symlink (``120000``), a
    gitlink (``160000``), or any other mode is a non-regular Git object that is never human prose, so a
    side carrying such a mode floors to at least CODE (fail-closed)."""
    return mode in _REGULAR_BLOB_MODES


def _blob(runner: Runner, worktree: str, commit: str, path: bytes) -> bytes:
    """Read a regular-file side the raw diff already proved exists at ``commit``, for frontmatter
    classification. A failed ``git show`` here is never benign absence — the diff named this object — so it
    is unreadable evidence (e.g. a blob-filtered clone with the promisor down) and fails CLOSED with
    ``TriageError`` (exit 2), exactly like every other Git call. Non-regular modes never reach this: they
    are already forced to CODE by mode, so their content is not read."""
    spec = os.fsencode(commit) + b":" + path
    operation = f"reading {os.fsdecode(path)!r} at {commit}"
    try:
        proc = runner(["git", "show", os.fsdecode(spec)], worktree)
    except OSError as exc:
        raise TriageError(f"{operation} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stderr).strip()
        suffix = f": {detail}" if detail else ""
        raise TriageError(f"{operation} exited {proc.returncode}{suffix}")
    return proc.stdout


def _path_class(path: str, content: bytes | None) -> tuple[str, list[str]]:
    sensitive = _sensitive_path_reasons(path)
    if sensitive:
        return SENSITIVE, sensitive
    agent = _agent_path_reason(path)
    if agent:
        return CODE, [agent]
    human_doc = _is_human_doc(path)
    if human_doc and _has_agent_frontmatter(content):
        return CODE, ["prose file carrying skill or agent frontmatter"]
    if human_doc:
        return HUMAN_DOC, ["human-facing prose"]
    return CODE, ["unrecognized path defaults to CODE"]


_CLASS_RANK = {HUMAN_DOC: 0, CODE: 1, SENSITIVE: 2}


def _classify_change(change: Change, runner: Runner, worktree: str,
                     diff_base_sha: str, head_sha: str) -> dict:
    old_path_s = os.fsdecode(change.old_path) if change.old_path is not None else None
    path_s = os.fsdecode(change.path)
    status = change.status[0]

    # Classify EVERY side that exists — base and head alike — and keep the higher class. The base side
    # exists for a deletion, a modification, a type-change, and the old path of a rename/copy; the head
    # side exists for every status but a deletion. A single-path modification or type-change (M/T) must
    # therefore inspect its base content too — otherwise a diff that strips agent frontmatter reads as
    # plain prose at HEAD and clears the floor. Each side carries its own Git mode; a non-regular object
    # (symlink, gitlink, or any unrecognized mode) is never prose and floors to at least CODE.
    sides: list[tuple[str, bytes, str, str]] = []
    if change.old_path is not None:
        sides.append((old_path_s or "", change.old_path, diff_base_sha, change.old_mode))
    elif status in {"M", "T"}:
        sides.append((path_s, change.path, diff_base_sha, change.old_mode))
    if status != "D":
        sides.append((path_s, change.path, head_sha, change.new_mode))
    if not sides:  # Defensive: a deletion always has old_path, but malformed evidence must not crash.
        sides.append((path_s, change.path, diff_base_sha, change.old_mode))

    classes: list[str] = []
    reasons: list[str] = []
    for candidate_path, candidate_bytes, commit, mode in sides:
        # A non-regular Git object (symlink, gitlink, unrecognized mode) is never prose and is forced to
        # CODE by mode below, so its content is not read — and a ``git show`` on it is not required to
        # succeed. A regular blob's content IS read, and a failed read there fails closed (see ``_blob``).
        content = _blob(runner, worktree, commit, candidate_bytes) if _mode_regular_blob(mode) else None
        file_class, path_reasons = _path_class(candidate_path, content)
        if not _mode_regular_blob(mode) and _CLASS_RANK[file_class] < _CLASS_RANK[CODE]:
            file_class = CODE
            path_reasons = [*path_reasons, f"non-regular Git mode {mode} is never human prose"]
        classes.append(file_class)
        for reason in path_reasons:
            label = f"{candidate_path}: {reason}" if len(sides) > 1 else reason
            reasons.append(label)

    if _mode_executable(change.old_mode) or _mode_executable(change.new_mode):
        classes.append(SENSITIVE)
        reasons.append("old or new Git mode is executable")
    if status not in {"A", "C", "D", "M", "R", "T"}:
        classes.append(CODE)
        reasons.append(f"unknown Git change status {change.status!r} defaults to CODE")

    file_class = max(classes, key=_CLASS_RANK.__getitem__)
    result = {
        "class": file_class,
        "new_mode": change.new_mode,
        "old_mode": change.old_mode,
        "old_path": old_path_s,
        "path": path_s,
        "reasons": sorted(set(reasons)),
        "status": change.status,
    }
    return result


def _floor(files: list[dict]) -> tuple[str | None, str]:
    """The minimum tier the mechanics compel, and why. Never ``TRIVIAL``: an all-prose diff has NO floor
    (the orchestrator decides), and an empty/unresolved diff floors to ``STANDARD`` — never vacuously below
    it."""
    if any(row["class"] == SENSITIVE for row in files):
        return HIGH, "a SENSITIVE file is present"
    if not files:
        return STANDARD, "no files resolved from the diff — an empty or unreadable diff never floors below STANDARD"
    if all(row["class"] == HUMAN_DOC for row in files):
        return None, "every changed file is human-facing prose — no mechanical floor; the orchestrator decides the tier"
    return STANDARD, "a non-prose (CODE) file is present"


def derive(*, worktree: str, base: str, head_sha: str, tier: str | None = None,
           runner: Runner = _real_run) -> dict:
    """Emit one deterministic inventory-and-floor record, or raise ``TriageError`` without partial output.

    ``tier`` is the caller's DECIDED tier, if any. When supplied it is validated and checked against the
    floor as a LOWER BOUND: a tier below the floor is refused (veto-downward). The command never grants a
    tier and never returns ``TRIVIAL`` as a floor."""
    if tier is not None and tier not in TIER_VALUES:
        raise TriageError(f"--tier must be one of {sorted(TIER_VALUES)}, got {tier!r}")
    if not SHA_RE.fullmatch(head_sha):
        raise TriageError("--head-sha must be exactly 40 lowercase hexadecimal characters")
    if not Path(worktree).is_dir():
        raise TriageError(f"--worktree is not a directory: {worktree!r}")

    head_before = _head(runner, worktree)
    if head_before != head_sha:
        raise TriageError(f"HEAD mismatch: expected {head_sha}, found {head_before}")
    base_sha = _resolve_base(runner, worktree, base)
    diff_base_sha = _merge_base(runner, worktree, base_sha, head_sha)
    # ``--find-renames`` without ``-l0``: above Git's default ``diff.renameLimit`` the inexact-rename search
    # is skipped (Git prints ``warning: … rename detection was skipped`` to stderr, exit 0). That skip is
    # deliberately tolerated because it CANNOT lower the floor. The floor is a ``max`` over per-row classes,
    # and an undetected rename degrades to an A row plus a D row that ``_classify_change`` classifies on BOTH
    # sides' content — the D at the old path on the base, the A at the new path on the head — the exact two
    # ``(path, commit, mode)`` pairs the single R record would have classified. A + D can only ADD rows, so
    # the floor is >= the detected-rename case, never below it (verified end to end: a CODE fixture and an
    # executable-script fixture forced through ``renameLimit=1`` floor identically detected vs. skipped, and
    # the A+D split even re-checks the executable mode on both sides). Forcing ``-l0`` or refusing on the
    # warning would instead make any legitimately large diff un-triageable — a fail-closed regression bought
    # for a floor that was already correct.
    # ``--ignore-submodules=none`` overrides any committed ``submodule.<name>.ignore=all`` in ``.gitmodules``:
    # without it a commit that advances a gitlink whose submodule is configured ``ignore=all`` is OMITTED from
    # ``--raw`` output, so the changed gitlink would vanish from the inventory and the diff could read as
    # all-prose. Forcing ``none`` can only ADD a gitlink row (a ``160000`` mode that ``_classify_change`` floors
    # to at least CODE); it never removes a row and so never lowers the floor — escalate-only, like the tool.
    raw = _run(
        runner,
        ["git", "diff", "--raw", "-z", "--no-abbrev", "--find-renames", "--ignore-submodules=none",
         diff_base_sha, head_sha, "--"],
        worktree,
        "reading the pinned raw diff",
    )
    changes = _parse_raw(raw)
    files = [_classify_change(change, runner, worktree, diff_base_sha, head_sha) for change in changes]
    files.sort(key=lambda row: (row["path"], row["old_path"] or "", row["status"]))

    head_after = _head(runner, worktree)
    if head_after != head_before:
        raise TriageError(f"HEAD moved during triage: started at {head_before}, ended at {head_after}")

    floor, floor_reason = _floor(files)
    floor_rank = _TIER_RANK[floor] if floor is not None else -1
    if tier is not None and _TIER_RANK[tier] < floor_rank:
        raise TriageError(
            f"decided tier {tier} is below the mechanical floor {floor} ({floor_reason}); "
            f"the tool escalates only — decide at or above the floor")

    return {
        "base": base,
        "diff_base_sha": diff_base_sha,
        "files": files,
        "floor": floor,
        "floor_reason": floor_reason,
        "head_sha": head_sha,
    }


def _assert_ledger_base(file: str, pr: str, base: str) -> "tuple[str | None, str | None]":
    """When a ledger is supplied, `--base` is an ASSERTION, not a base source: the ROW owns the base. Resolve
    the row's `effective_base` (its explicit `base_branch`, else the legacy header fallback, through
    `ledger.py`'s accessor — never a second copy of that rule) and return `(effective_base, None)` when
    `--base` agrees, else `(None, <error string>)`. Triage passes `--base` as `origin/<base>`; agreement is
    decided by `ledger.py`'s `base_agrees` (the one owner of that comparison — identical strings always
    agree, and a leading `origin/` on the ARGUMENT is stripped, never on the stored base). Only a different
    branch NAME disagrees; this never inspects live GitHub.

    The RESOLVED `effective_base` is returned so the caller builds the operational git ref from the ROW's
    base, not the raw `--base` spelling: two spellings `base_agrees` accepts (`rel` vs `origin/rel`) resolve
    to DIFFERENT git refs, so trusting the raw string would diff against the wrong branch (a false permissive
    that under-triages a sensitive change). The row's base is authoritative; the operational ref follows it."""
    try:
        header, rows = L.load(Path(file))
    except SystemExit as exc:
        return None, f"could not read ledger {file}: {exc}"
    row = L.find_row(rows, str(pr))
    if row is None:
        return None, f"no ledger row for pr {pr} — its base cannot be resolved"
    effective_base, base_problem = L.require_effective_base(header, row, pr)
    if base_problem is not None:
        return None, base_problem
    if not L.base_agrees(base, effective_base):
        return None, (f"--base {base!r} disagrees with pr {pr}'s ledger effective base {effective_base!r} — "
                      f"--base is an assertion, not a base source")
    return effective_base, None


def cmd_derive(args: argparse.Namespace) -> int:
    base = args.base
    if args.file is not None:
        if args.pr is None:
            print("triage: REFUSED — --file requires --pr to select the ledger row", file=sys.stderr)
            return EXIT_REFUSED
        effective_base, problem = _assert_ledger_base(args.file, args.pr, args.base)
        if problem is not None:
            print(f"triage: REFUSED — {problem}", file=sys.stderr)
            return EXIT_REFUSED
        # Build the operational git ref from the ROW's resolved base, never the raw `--base` spelling:
        # `origin/<effective_base>` is the remote-tracking ref triage diffs against.
        base = f"origin/{effective_base}"
    try:
        result = derive(
            worktree=args.worktree,
            base=base,
            head_sha=args.head_sha,
            tier=args.tier,
        )
    except TriageError as exc:
        print(f"triage: REFUSED — {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return EXIT_OK


def sibling_cases() -> list:
    if not SIBLING.is_file():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} is missing — deterministic tier derivation is untested")
    module = load_module_from_path("campaign_triage_test", SIBLING, register=True)
    if module is None or not hasattr(module, "CASES") or not module.CASES:
        raise SelfTestFailure("triage sibling suite has no CASES — an empty gate checks nothing")
    return module.CASES


def cmd_self_test(_args: argparse.Namespace) -> int:
    failures = 0
    cases = sibling_cases()
    for name, description, fn in cases:
        try:
            fn()
            print(f"PASS {name}: {description}")
        except Exception as exc:  # noqa: BLE001 - report all independent fixtures
            failures += 1
            print(f"FAIL {name}: {description}: {exc}")
    print(f"triage fixtures: {len(cases) - failures} passed, {failures} failed")
    return 1 if failures else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = root.add_subparsers(dest="command", required=True)
    derive_parser = sub.add_parser(
        "derive", help="emit the per-file inventory and mechanical floor for one pinned Git diff")
    derive_parser.add_argument("--worktree", required=True)
    derive_parser.add_argument("--base", required=True, help="base revision, commonly origin/<base>")
    derive_parser.add_argument("--head-sha", required=True, help="expected live 40-character HEAD")
    derive_parser.add_argument(
        "--tier", choices=sorted(TIER_VALUES),
        help="the caller's DECIDED tier; refused if it is below the mechanical floor (veto-downward)")
    derive_parser.add_argument(
        "--file", help="OPTIONAL ledger (state.jsonl); when given, --base is asserted against the selected "
                       "row's effective base (requires --pr). Absent: --base is used as-is, as before")
    derive_parser.add_argument(
        "--pr", help="PR number (row key) selecting the ledger row for the --file base assertion")
    derive_parser.set_defaults(func=cmd_derive)
    test_parser = sub.add_parser("self-test", help="run the sibling fixture suite")
    test_parser.set_defaults(func=cmd_self_test)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
