#!/usr/bin/env python3
"""Materialize one complete, host-neutral fix-worker prompt as an atomic artifact bundle.

    worker-prompt.py fix --role review|ci-session|ci-economy \
        --project-root <absolute-path> --worktree <absolute-path> \
        --pr <N> --base <base> [--file <ledger>] --preflight-verdict proceed \
        --issues-file <path> [--logs-file <path>] --output-dir <new-directory>
    worker-prompt.py self-test

When `--file <ledger>` is given, `--base` is an ASSERTION checked against the `--pr` row's effective base
(its explicit `base_branch`, else the legacy header fallback), never an independent base source; a
disagreement refuses. Omit it and `--base` is used exactly as before.

The tool binds dynamic values once as bytes. It launches no worker, chooses no host model name, and
judges no fix. The published directory appears only after both `prompt.txt` and `metadata.json` are
complete; an existing output or any staging failure is refused without a partial bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Callable

from _gauntlet.modules import load_module_from_path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "worker-prompt-template.txt"
SIBLING = HERE / "worker-prompt-test.py"
FORMAT_PREFLIGHT = HERE / "format-preflight.py"
LEDGER = HERE / "ledger.py"


def _load_ledger():
    mod = load_module_from_path("worker_prompt_ledger", LEDGER)
    if mod is None:
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER}")
    return mod


L = _load_ledger()

EXIT_OK = 0
EXIT_REFUSED = 2
SCHEMA_VERSION = 1
PROMPT_NAME = "prompt.txt"
METADATA_NAME = "metadata.json"
ROLES = ("review", "ci-session", "ci-economy")
MODEL_CLASS = {"review": "session", "ci-session": "session", "ci-economy": "economy"}

SECTION_NAMES = ("COMMON", "REVIEW", "CI_SESSION", "CI_ECONOMY")
ROLE_SECTION = {"review": "REVIEW", "ci-session": "CI_SESSION", "ci-economy": "CI_ECONOMY"}
COMMON_SLOTS = {
    b"{{ROLE}}", b"{{MODEL_CLASS}}", b"{{PROJECT_ROOT}}", b"{{WORKTREE}}", b"{{PR}}",
    b"{{BASE}}", b"{{ISSUES_LENGTH}}", b"{{ISSUES_SHA256}}", b"{{ISSUES}}", b"{{ROLE_BLOCK}}",
}
CI_SLOTS = {b"{{LOGS_LENGTH}}", b"{{LOGS_SHA256}}", b"{{LOGS}}"}
# The economy role's mandatory format-preflight command is bound, not left for the isolated worker to
# resolve: `{{FORMAT_PREFLIGHT}}` receives `format-preflight.py`'s driver-resolved absolute path (from the
# same active-skill-dir source this tool uses for its own bundled scripts) and `{{WORKTREE_ARG}}` the
# worktree, so the published economy prompt runs the preflight from its own bytes. Both are bound already
# shell-quoted (`shlex.quote`) because the command is emitted as shell source inside backticks: a worktree
# or script path containing a space or shell metacharacter must stay one argument, or the preflight reads a
# truncated path and exits 2, silently skipping the mandatory check. `{{WORKTREE_ARG}}` is a distinct
# command-only slot so the shared prose slot `{{WORKTREE}}` stays an unquoted, readable path. A fresh worker
# gets only those bytes and cannot resolve `<skill-dir>` itself.
CI_ECONOMY_SLOTS = CI_SLOTS | {b"{{FORMAT_PREFLIGHT}}", b"{{WORKTREE_ARG}}"}
REQUIRED_SENTINELS = {
    "COMMON": (
        b"[GAUNTLET_FIX_PREFLIGHT_V1]",
        b"[GAUNTLET_FIX_SCOPE_V1]",
        b"[GAUNTLET_FIX_SWEEP_V1]",
        b"[GAUNTLET_FIX_REPORT_V1]",
    ),
    "REVIEW": (b"[GAUNTLET_FIX_REVIEW_V1]",),
    "CI_SESSION": (b"[GAUNTLET_FIX_CI_SESSION_V1]", b"[GAUNTLET_FIX_CI_NO_WEAKENING_V1]"),
    "CI_ECONOMY": (
        b"[GAUNTLET_FIX_CI_ECONOMY_V1]",
        b"[GAUNTLET_FIX_CI_NO_WEAKENING_V1]",
        b"[GAUNTLET_FIX_CI_ECONOMY_PROHIBITIONS_V1]",
        b"[GAUNTLET_FIX_CI_ECONOMY_RISK_V1]",
    ),
}


class Refusal(ValueError):
    """Controlled refusal for invalid inputs or output state."""


def _slots(data: bytes) -> set[bytes]:
    """Return every `{{UPPER_CASE}}` slot without interpreting inserted payload bytes."""
    found: set[bytes] = set()
    start = 0
    while True:
        left = data.find(b"{{", start)
        if left < 0:
            return found
        right = data.find(b"}}", left + 2)
        if right < 0:
            raise Refusal("template contains an unterminated slot")
        token = data[left:right + 2]
        name = token[2:-2]
        if not name or any(byte not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for byte in name):
            raise Refusal(f"template contains an invalid slot {token!r}")
        found.add(token)
        start = right + 2


def _extract_sections(template: bytes) -> dict[str, bytes]:
    sections: dict[str, bytes] = {}
    remaining = template
    for name in SECTION_NAMES:
        begin = f"@@BEGIN {name}@@\n".encode()
        end = f"@@END {name}@@\n".encode()
        if remaining.count(begin) != 1 or remaining.count(end) != 1:
            raise Refusal(f"template must contain exactly one {name} section")
        before, tail = remaining.split(begin, 1)
        if before.strip():
            raise Refusal(f"unexpected bytes before {name} section")
        body, remaining = tail.split(end, 1)
        sections[name] = body
    if remaining.strip():
        raise Refusal("unexpected bytes after the final template section")
    return sections


def load_template(path: Path = TEMPLATE) -> dict[str, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refusal(f"cannot read prompt template {path}: {exc}") from exc
    sections = _extract_sections(raw)
    expected_slots = {
        "COMMON": COMMON_SLOTS,
        "REVIEW": set(),
        "CI_SESSION": CI_SLOTS,
        "CI_ECONOMY": CI_ECONOMY_SLOTS,
    }
    for name, body in sections.items():
        actual = _slots(body)
        if actual != expected_slots[name]:
            missing = sorted(expected_slots[name] - actual)
            extra = sorted(actual - expected_slots[name])
            raise Refusal(f"{name} template slots disagree: missing={missing!r} extra={extra!r}")
        for slot in expected_slots[name]:
            if body.count(slot) != 1:
                raise Refusal(f"{name} template slot {slot!r} must occur exactly once")
        for sentinel in REQUIRED_SENTINELS[name]:
            if body.count(sentinel) != 1:
                raise Refusal(f"{name} template lost required block {sentinel.decode()}")
    return sections


def _bind_once(template: bytes, values: dict[bytes, bytes]) -> bytes:
    """Bind template-owned slots once. Inserted bytes are never searched or rebound."""
    parts: list[bytes] = []
    cursor = 0
    while True:
        left = template.find(b"{{", cursor)
        if left < 0:
            parts.append(template[cursor:])
            break
        right = template.find(b"}}", left + 2)
        if right < 0:
            raise Refusal("template contains an unterminated slot")
        token = template[left:right + 2]
        if token not in values:
            raise Refusal(f"template contains unresolved slot {token!r}")
        parts.extend((template[cursor:left], values[token]))
        cursor = right + 2
    return b"".join(parts)


def _payload_digest(data: bytes) -> bytes:
    return hashlib.sha256(data).hexdigest().encode("ascii")


def validate_payload(data: bytes, label: str) -> bytes:
    if not data:
        raise Refusal(f"{label} payload is empty")
    if b"\x00" in data:
        raise Refusal(f"{label} payload contains NUL bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal(f"{label} payload is not UTF-8: {exc}") from exc
    return data


def _read_payload(path_text: str, label: str) -> bytes:
    path = Path(path_text)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise Refusal(f"cannot inspect {label} file {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise Refusal(f"{label} file must be a regular, non-symlink file: {path}")
    try:
        return validate_payload(path.read_bytes(), label)
    except OSError as exc:
        raise Refusal(f"cannot read {label} file {path}: {exc}") from exc


def validate_context_path(path_text: str, label: str) -> str:
    if not path_text or not os.path.isabs(path_text):
        raise Refusal(f"{label} must be an absolute path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path_text):
        raise Refusal(f"{label} contains control characters")
    if not Path(path_text).is_dir():
        raise Refusal(f"{label} is not an existing directory: {path_text}")
    return path_text


def validate_text(value: str, label: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Refusal(f"{label} must be non-empty and contain no control characters")
    return value


def render_prompt(*, role: str, project_root: str, worktree: str, pr: int, base: str,
                  issues: bytes, logs: "bytes | None", format_preflight: str,
                  sections: dict[str, bytes]) -> bytes:
    if role not in ROLES:
        raise Refusal(f"invalid role {role!r}; expected one of {', '.join(ROLES)}")
    issues = validate_payload(issues, "issues")
    if role == "review" and logs is not None:
        raise Refusal("review role refuses --logs-file; put review evidence in --issues-file")
    if role != "review" and logs is None:
        raise Refusal(f"{role} role requires --logs-file")

    role_template = sections[ROLE_SECTION[role]]
    if logs is None:
        role_block = role_template
    else:
        logs = validate_payload(logs, "logs")
        # The economy section owns {{FORMAT_PREFLIGHT}} and {{WORKTREE_ARG}}, both bound shell-quoted so
        # the emitted backtick command survives a spaced/metacharacter path; the session section owns
        # neither, so those extra values are simply unused when binding the ci-session block.
        role_block = _bind_once(role_template, {
            b"{{LOGS_LENGTH}}": str(len(logs)).encode(),
            b"{{LOGS_SHA256}}": _payload_digest(logs),
            b"{{LOGS}}": logs,
            b"{{FORMAT_PREFLIGHT}}": shlex.quote(format_preflight).encode(),
            b"{{WORKTREE_ARG}}": shlex.quote(worktree).encode(),
        })

    values = {
        b"{{ROLE}}": role.encode(),
        b"{{MODEL_CLASS}}": MODEL_CLASS[role].encode(),
        b"{{PROJECT_ROOT}}": project_root.encode(),
        b"{{WORKTREE}}": worktree.encode(),
        b"{{PR}}": str(pr).encode(),
        b"{{BASE}}": base.encode(),
        b"{{ISSUES_LENGTH}}": str(len(issues)).encode(),
        b"{{ISSUES_SHA256}}": _payload_digest(issues),
        b"{{ISSUES}}": issues,
        b"{{ROLE_BLOCK}}": role_block,
    }
    return _bind_once(sections["COMMON"], values)


def metadata_bytes(role: str, prompt: bytes) -> bytes:
    payload = {
        "kind": "gauntlet-fix-worker-prompt",
        "model_class": MODEL_CLASS[role],
        "prompt_file": PROMPT_NAME,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "role": role,
        "schema_version": SCHEMA_VERSION,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_file(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def publish_bundle(output_dir: Path, prompt: bytes, metadata: bytes,
                   *, writer: Callable[[Path, bytes], None] = _write_file) -> None:
    output_text = str(output_dir)
    if not output_dir.is_absolute():
        raise Refusal("--output-dir must be an absolute path")
    if any(ord(char) < 32 or ord(char) == 127 for char in output_text):
        raise Refusal("--output-dir contains control characters")
    parent = output_dir.parent

    # Every filesystem probe below can raise OSError (ENAMETOOLONG on an overlong basename, EROFS/ENOSPC
    # under the parent) — including the pre-staging `is_dir`/`exists` probes and `mkdtemp` itself. They are
    # inside this try so OSError becomes the documented controlled Refusal (exit 2), never an escaping
    # traceback (exit 1). `staging` guards the cleanup because an early probe can fail before it is created.
    staging: "Path | None" = None
    published = False
    try:
        if not parent.is_dir() or parent.is_symlink():
            raise Refusal(f"output parent must be an existing, non-symlink directory: {parent}")
        if output_dir.exists() or output_dir.is_symlink():
            raise Refusal(f"output already exists; refusing conflicting artifacts: {output_dir}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=parent))
        writer(staging / PROMPT_NAME, prompt)
        writer(staging / METADATA_NAME, metadata)
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if output_dir.exists() or output_dir.is_symlink():
            raise Refusal(f"output appeared during publication: {output_dir}")
        os.rename(staging, output_dir)
        published = True
    except (OSError, Refusal) as exc:
        if isinstance(exc, Refusal):
            raise
        raise Refusal(f"cannot publish prompt bundle {output_dir}: {exc}") from exc
    finally:
        if staging is not None and not published:
            shutil.rmtree(staging, ignore_errors=True)


def run_fix(args: argparse.Namespace) -> int:
    if args.preflight_verdict != "proceed":
        raise Refusal("--preflight-verdict must be exactly 'proceed'")
    project_root = validate_context_path(args.project_root, "--project-root")
    worktree = validate_context_path(args.worktree, "--worktree")
    base = validate_text(args.base, "--base")
    # The base goes into the fix prompt as DATA. When a ledger is supplied, that data is an ASSERTION against
    # the row's source of truth: the row OWNS the base, so `--base` must equal the selected row's
    # `effective_base` (its explicit `base_branch`, else the legacy header fallback, through `ledger.py`'s
    # accessor — never a second copy of that rule). A leading `origin/` is stripped first. Absent `--file`,
    # the base is used as-is, as before.
    if args.file is not None:
        try:
            header, rows = L.load(Path(args.file))
        except SystemExit as exc:
            raise Refusal(f"could not read ledger {args.file}: {exc}") from exc
        row = L.find_row(rows, str(args.pr))
        if row is None:
            raise Refusal(f"no ledger row for pr {args.pr} — its base cannot be resolved")
        effective_base = L.effective_base(header, row)
        normalized = base[len("origin/"):] if base.startswith("origin/") else base
        if effective_base and effective_base != "-" and normalized != effective_base:
            raise Refusal(f"--base {base!r} disagrees with pr {args.pr}'s ledger effective base "
                          f"{effective_base!r} — --base is an assertion, not a base source")
    issues = _read_payload(args.issues_file, "issues")
    logs = _read_payload(args.logs_file, "logs") if args.logs_file is not None else None
    sections = load_template()
    prompt = render_prompt(role=args.role, project_root=project_root, worktree=worktree, pr=args.pr,
                           base=base, issues=issues, logs=logs,
                           format_preflight=str(FORMAT_PREFLIGHT), sections=sections)
    metadata = metadata_bytes(args.role, prompt)
    publish_bundle(Path(args.output_dir), prompt, metadata)
    print(metadata.decode("utf-8"), end="")
    return EXIT_OK


class SelfTestFailure(AssertionError):
    """A fixture failed."""


def self_test() -> int:
    module = load_module_from_path("worker_prompt_test", SIBLING)
    if module is None:
        print(f"worker-prompt: REFUSED — cannot load required sibling suite {SIBLING}", file=sys.stderr)
        return 1
    return int(module.run_all())


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = parser.add_subparsers(dest="command", required=True)

    fix = sub.add_parser("fix", help="materialize one complete fix-worker prompt bundle")
    fix.add_argument("--role", required=True, choices=ROLES)
    fix.add_argument("--project-root", required=True)
    fix.add_argument("--worktree", required=True)
    fix.add_argument("--pr", required=True, type=int)
    fix.add_argument("--base", required=True)
    fix.add_argument("--file", help="OPTIONAL ledger (state.jsonl); when given, --base is asserted against "
                                    "the --pr row's effective base")
    fix.add_argument("--preflight-verdict", required=True)
    fix.add_argument("--issues-file", required=True)
    fix.add_argument("--logs-file")
    fix.add_argument("--output-dir", required=True)
    sub.add_parser("self-test", help="run every fixture in worker-prompt-test.py")

    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    if args.pr <= 0:
        print("worker-prompt: REFUSED — --pr must be positive", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return run_fix(args)
    except Refusal as exc:
        print(f"worker-prompt: REFUSED — {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
