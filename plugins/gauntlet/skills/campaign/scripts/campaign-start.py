#!/usr/bin/env python3
"""Drive fresh campaign startup as a resumable command protocol.

The campaign used to ask the orchestrator to transcribe fresh-run setup from several reference files:
resolve the checkout, preflight every PR, create the run, initialize the ledger, mint a token, prepare host
continuity, acquire the lease, adopt each PR, request the two semantic judgments, initialize CI, and clear
the checkpoint.  This command owns that sequence.  It emits one JSON state after each invocation; the host
performs only the named host action or supplies the named judgment, then calls the returned next command.

The durable state is the existing ledger.  ``pending_adoption`` is the run-level startup checkpoint, and a
pending row's ``intent`` provenance says whether its judgment has landed.  No parallel startup state file
can drift from those owners.

Commands:

  campaign-start.py new --checkout <path> --host <host> --reviewer <choice> --pr <N> [--pr <N> ...]
  campaign-start.py take --checkout <path> --run <id> --token <tok> --heartbeat-id <proof>
                            [--allow-takeover]
  campaign-start.py advance --checkout <path> --run <id> --token <tok>
  campaign-start.py bind --checkout <path> --run <id> --token <tok> --pr <N> --tier <tier>
                         (--use-stated-intent | --intent-file <path>)
  campaign-start.py resume --checkout <path> --host <host> --run <id> [--allow-takeover]
  campaign-start.py self-test

``new`` performs the whole read-only PR preflight before creating anything.  It then atomically publishes
the complete header through ``ledger.py init`` and returns ``needs-host-arm``.  A scheduled host arms the
returned prompt as its last action; a scheduler-less host establishes its bounded-wait continuation.  The
resulting proof is passed to ``take``.  ``take`` acquires the lease and enters ``advance``.

``advance`` adopts one unbound PR at a time and returns ``needs-pr-judgment`` with exact title/body and
diff byte-files, the mechanically derived triage floor, and any usable stated intent. ``bind`` accepts the
orchestrator's tier decision and either that stated intent or a separate authored byte-file, validates both
before writing, records them, and advances again.  Once every requested PR is bound, the command initializes
required-check state and CI liveness, clears ``pending_adoption``, and returns ``ready`` plus typed CI-watch
host actions.  It never launches a background task itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn, cast

from _gauntlet import gh
from _gauntlet.atomic import replace_bytes, replace_text
from _gauntlet.git_refs import branch_problem
from _gauntlet.modules import load_sibling
from _gauntlet.repository import repo_problem
from _gauntlet.testing import run_sibling_suite

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "campaign-start-test.py"

LEDGER_PY = HERE / "ledger.py"
LEASE_PY = HERE / "lease.py"
RUN_ID_PY = HERE / "run-id.py"
PR_ADOPT_PY = HERE / "pr-adopt.py"
TRIAGE_PY = HERE / "triage.py"
HEARTBEAT_PY = HERE / "heartbeat.py"
REVIEW_PASS_PY = HERE / "review-pass.py"
CI_STATUS_PY = HERE / "ci-status.py"
LABEL_MIRROR_PY = HERE / "label-mirror.py"

L = load_sibling("campaign_start_ledger", HERE, LEDGER_PY.name)
A = load_sibling("campaign_start_adopt", HERE, PR_ADOPT_PY.name)
T = load_sibling("campaign_start_triage", HERE, TRIAGE_PY.name, register=True)
H = load_sibling("campaign_start_heartbeat", HERE, HEARTBEAT_PY.name)
RP = load_sibling("campaign_start_review_pass", HERE, REVIEW_PASS_PY.name)
LEASE = load_sibling("campaign_start_lease", HERE, LEASE_PY.name)

DESCRIPTION = "Resumable code-owned startup for a fresh Gauntlet campaign run."
HOST_INVOCATIONS = {"claude-code": "/gauntlet:campaign", "codex": "$gauntlet:campaign"}
RUN_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TERMINAL = frozenset(L.TERMINAL_STATUSES)
STANDARD = "STANDARD"


class Refusal(Exception):
    """Startup cannot safely advance; no later mutation in this invocation may run."""


class SelfTestFailure(AssertionError):
    """A rule this command claims to enforce does not hold."""


def refuse(message: str) -> NoReturn:
    raise Refusal(message)


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def run_argv(argv: list[str], *, cwd: "Path | None" = None, stdin: "str | None" = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def run_bytes(argv: list[str], *, cwd: "Path | None" = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            cwd=os.fsencode(cwd) if cwd is not None else None,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, b"", os.fsencode(str(exc)))


def tool_json(
    argv: list[str],
    *,
    cwd: "Path | None" = None,
    stdin: "str | None" = None,
    accepted: "tuple[int, ...]" = (0,),
) -> "tuple[dict, int]":
    proc = run_argv(argv, cwd=cwd, stdin=stdin)
    if proc.returncode not in accepted:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no diagnostic"
        refuse(f"{Path(argv[1]).name if len(argv) > 1 else argv[0]} exited {proc.returncode}: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        refuse(f"{Path(argv[1]).name if len(argv) > 1 else argv[0]} returned non-JSON output ({exc})")
    if not isinstance(payload, dict):
        refuse(f"{Path(argv[1]).name if len(argv) > 1 else argv[0]} returned {type(payload).__name__}, not an object")
    return payload, proc.returncode


def resolve_repository_context(checkout: Path) -> dict[str, Path]:
    """Resolve the supplied checkout exactly once, preserving the runtime adapter's byte boundary."""
    try:
        proc = subprocess.run(
            ["git", "-C", os.fspath(checkout), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        refuse(f"cannot resolve checkout {checkout}: {exc}")
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stderr).strip() or f"git exited {proc.returncode}"
        refuse(f"cannot resolve checkout {checkout}: {detail}")
    raw = proc.stdout
    if not raw.endswith(b"\n"):
        refuse("git rev-parse output has no terminating LF")
    text = os.fsdecode(raw[:-1])
    root = Path(text)
    if not text or not root.is_absolute():
        refuse(f"git rev-parse returned a non-absolute repository root {text!r}")
    return {
        "project_root": root,
        "scratch_root": root / ".gauntlet" / "tmp",
        "worktrees_root": root / ".worktrees",
    }


def require_gauntlet_ignored(project_root: Path) -> None:
    probe = ".gauntlet/campaign-start-ignore-probe"
    proc = run_argv(["git", "-C", os.fspath(project_root), "check-ignore", "-q", "--no-index", probe])
    if proc.returncode != 0:
        refuse(".gauntlet/ is not ignored by this repository; add `.gauntlet/` to `.gitignore` before startup")


def resolve_github_repo(project_root: Path) -> str:
    payload, _ = tool_json([
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner",
    ], cwd=project_root)
    repo = payload.get("nameWithOwner")
    if not isinstance(repo, str) or repo_problem(repo) is not None:
        refuse(f"gh repo view returned unusable nameWithOwner {repo!r}")
    return repo


def label_mirror_argv(ledger: Path, pr: str, github_repo: str) -> list[str]:
    return [
        sys.executable,
        os.fspath(LABEL_MIRROR_PY),
        "mirror",
        "--ledger",
        os.fspath(ledger),
        "--pr",
        pr,
        "--repo",
        github_repo,
    ]


def normalize_prs(values: list[str]) -> list[str]:
    if not values:
        refuse("new requires at least one --pr; an empty run is never created")
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw[1:] if raw.startswith("#") else raw
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            refuse(f"invalid PR number {raw!r}; use a positive integer")
        pr = str(int(value))
        if pr in seen:
            refuse(f"duplicate PR {pr} in one startup request")
        seen.add(pr)
        out.append(pr)
    return out


def load_ledger(path: Path) -> tuple[dict, list[dict]]:
    """Turn the ledger accessor's in-process refusal into this protocol's structured refusal."""
    try:
        return L.load(path)
    except SystemExit as exc:
        refuse(f"ledger accessor refused {path} (exit {exc.code})")


def active_skill_version(host: str) -> str:
    manifest_name = ".claude-plugin" if host == "claude-code" else ".codex-plugin"
    manifest = HERE.parents[2] / manifest_name / "plugin.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuse(f"cannot read active {host} plugin manifest {manifest}: {exc}")
    version = raw.get("version") if isinstance(raw, dict) else None
    if not isinstance(version, str) or not version.strip():
        refuse(f"active {host} plugin manifest {manifest} has no usable version")
    return version


def validate_reviewer(value: str) -> str:
    reviewer = value.strip()
    if not reviewer or reviewer == "-" or "\n" in value or "\r" in value:
        refuse("--reviewer must be the nonempty, single-line choice resolved from trusted host state")
    return reviewer


def _run_labels(view: dict) -> list[str]:
    out: list[str] = []
    for item in view.get("labels", []):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.startswith(A.RUN_LABEL_PREFIX):
            out.append(name)
    return out


def preflight_views(
    prs: list[str],
    repository: dict[str, Path],
    *,
    fetch: Callable[..., tuple[object, "str | None"]] = gh.pr_view_json,
) -> list[dict]:
    """Read and validate the whole PR set before any run directory or ledger exists."""
    views: list[dict] = []
    for pr in prs:
        view, error = fetch(pr, fields=A.VIEW_FIELDS, cwd=os.fspath(repository["project_root"]))
        if error is not None:
            refuse(f"PR {pr} preflight could not read GitHub metadata: {error}")
        problem = A.validate_view(view)
        if problem is not None:
            refuse(f"PR {pr} preflight received malformed GitHub metadata: {problem}")
        shaped = cast(dict, view)
        if str(shaped["number"]) != pr:
            refuse(f"PR {pr} preflight returned metadata for PR {shaped['number']}")
        owners = _run_labels(shaped)
        if owners:
            refuse(f"PR {pr} is already owned by {', '.join(owners)}")
        plan = A.build_plan(
            shaped,
            run_id="fresh-preflight",
            tier=STANDARD,
            worktrees_root=os.fspath(repository["worktrees_root"]),
        )
        if plan.get("verdict") != "adopt":
            refuse(f"PR {pr} preflight refused: {plan.get('reason', 'unknown refusal')}")
        branch_error = branch_problem(os.fspath(repository["project_root"]), str(plan["branch"]))
        if branch_error is not None:
            refuse(f"PR {pr} head branch is invalid: {branch_error}")
        views.append(shaped)
    return views


def create_run_directory(scratch_root: Path) -> tuple[str, Path]:
    payload, _ = tool_json(
        [sys.executable, os.fspath(RUN_ID_PY), "new", "--runs-dir", os.fspath(scratch_root)]
    )
    run_id, rundir = payload.get("run_id"), payload.get("rundir")
    if not isinstance(run_id, str) or not isinstance(rundir, str):
        refuse("run-id.py returned no usable run_id/rundir pair")
    path = Path(rundir)
    if path != scratch_root / run_id or not path.is_dir():
        refuse("run-id.py returned a run directory outside the repository scratch root")
    return run_id, path


def host_arm_state(
    *,
    host: str,
    repository: dict[str, Path],
    run_id: str,
    rundir: Path,
    token: str,
    reviewer: str,
    skill_version: str,
    allow_takeover: bool = False,
) -> dict:
    invocation = HOST_INVOCATIONS[host]
    primary = H.callback_command(invocation, run_id, token)
    watchdog = H.watchdog_command(invocation, run_id, token)
    proof = f"bounded-wait:{run_id}" if host == "codex" else None
    take_argv = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "take",
        "--checkout",
        os.fspath(repository["project_root"]),
        "--run",
        run_id,
        "--token",
        token,
        "--heartbeat-id",
        "<host-proof>",
    ]
    if allow_takeover:
        take_argv.append("--allow-takeover")
    return {
        "state": "needs-host-arm",
        "run_id": run_id,
        "rundir": os.fspath(rundir),
        "token": token,
        "reviewer": reviewer,
        "skill_version": skill_version,
        "host_actions": [
            {
                "kind": "ensure-session-watchdog",
                "optional": True,
                "key": f"gauntlet-watchdog-{run_id}",
                "prompt": watchdog,
            },
            {
                "kind": "arm-primary-heartbeat" if host == "claude-code" else "establish-bounded-wait",
                "must_run_last": host == "claude-code",
                "prompt": primary,
                "suggested_heartbeat_id": proof,
            },
        ],
        "next": {
            "command": "take",
            "argv": take_argv,
        },
    }


def prepare_new(
    *,
    checkout: Path,
    host: str,
    reviewer: str,
    prs: list[str],
    default_non_goals: str,
    fetch: Callable[..., tuple[object, "str | None"]] = gh.pr_view_json,
    run_creator: Callable[[Path], tuple[str, Path]] = create_run_directory,
    token_mint: Callable[[], str] = LEASE.mint_token,
    version: "str | None" = None,
) -> dict:
    repository = resolve_repository_context(checkout)
    require_gauntlet_ignored(repository["project_root"])
    normalized = normalize_prs(prs)
    selected = validate_reviewer(reviewer)
    running_version = version or active_skill_version(host)
    preflight_views(normalized, repository, fetch=fetch)

    run_id, rundir = run_creator(repository["scratch_root"])
    ledger = rundir / "state.jsonl"
    try:
        tool_json([
            sys.executable,
            os.fspath(LEDGER_PY),
            "--file",
            os.fspath(ledger),
            "init",
            "--run-id",
            run_id,
            "--pending-adoption",
            " ".join(normalized),
            "--reviewer",
            selected,
            "--skill-version",
            running_version,
            "--default-non-goals",
            default_non_goals,
        ])
    except Refusal:
        if not any(rundir.iterdir()):
            rundir.rmdir()
        raise
    token = token_mint()
    return host_arm_state(
        host=host,
        repository=repository,
        run_id=run_id,
        rundir=rundir,
        token=token,
        reviewer=selected,
        skill_version=running_version,
    )


def validate_run_id(run_id: str) -> str:
    if RUN_ID_RE.fullmatch(run_id) is None:
        refuse(f"invalid run id {run_id!r}")
    return run_id


def load_run(repository: dict[str, Path], run_id: str) -> tuple[Path, Path, dict, list[dict]]:
    validate_run_id(run_id)
    rundir = repository["scratch_root"] / run_id
    ledger = rundir / "state.jsonl"
    if not rundir.is_dir() or not ledger.is_file():
        refuse(f"run {run_id} has no startup ledger at {ledger}")
    header, rows = load_ledger(ledger)
    if header.get("run_id") != run_id:
        refuse(f"ledger {ledger} belongs to run {header.get('run_id')!r}, not {run_id}")
    return rundir, ledger, header, rows


def pending_prs(header: dict) -> list[str]:
    value = str(header.get("pending_adoption", "-"))
    if value == "-":
        return []
    return normalize_prs(value.split())


def extract_stated_intent(body: str) -> "str | None":
    """Return the three usable intent sections verbatim, or None when the body needs authorship."""
    if RP.MANAGED_START in body or RP.MANAGED_END in body:
        return None
    lines = body.splitlines(keepends=True)
    starts: dict[str, int] = {}
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line in RP.INTENT_SECTIONS:
            if line in starts:
                return None
            starts[line] = index
    if set(starts) != set(RP.INTENT_SECTIONS):
        return None
    positions = [starts[name] for name in RP.INTENT_SECTIONS]
    if positions != sorted(positions):
        return None
    chunks: list[str] = []
    for start in positions:
        stop = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].strip().startswith("#"):
                stop = index
                break
        chunks.append("".join(lines[start:stop]).rstrip())
    candidate = "\n".join(chunks).rstrip() + "\n"
    try:
        RP.parse_intent(candidate, Path("PR body"))
    except RP.Defect:
        return None
    return candidate


def judgment_path(rundir: Path, pr: str) -> Path:
    return rundir / f"startup-judgment-{pr}.json"


def diff_path(rundir: Path, pr: str) -> Path:
    return rundir / f"startup-diff-{pr}.patch"


def prepare_judgment(
    repository: dict[str, Path],
    rundir: Path,
    ledger: Path,
    pr: str,
    token: str,
) -> dict:
    header, rows = load_ledger(ledger)
    row = L.find_row(rows, pr)
    if row is None:
        refuse(f"cannot prepare judgment for PR {pr}: no adopted row")
    base, problem = L.require_effective_base(header, row, pr)
    if problem is not None:
        refuse(problem)
    view, error = gh.pr_view_json(
        pr,
        fields="number,title,body,headRefOid",
        cwd=os.fspath(repository["project_root"]),
    )
    if error is not None:
        refuse(f"cannot read PR {pr} intent inputs: {error}")
    if not isinstance(view, dict):
        refuse(f"PR {pr} intent input is {type(view).__name__}, not an object")
    if str(view.get("headRefOid")) != row.get("head_sha"):
        refuse(f"PR {pr} moved while startup prepared its judgment; reconcile the row and retry")
    body = view.get("body")
    title = view.get("title")
    if not isinstance(body, str) or not isinstance(title, str):
        refuse(f"PR {pr} title/body is malformed")

    triage, _ = tool_json([
        sys.executable,
        os.fspath(TRIAGE_PY),
        "derive",
        "--worktree",
        str(row["worktree"]),
        "--base",
        f"origin/{base}",
        "--head-sha",
        str(row["head_sha"]),
        "--file",
        os.fspath(ledger),
        "--pr",
        pr,
    ], cwd=repository["project_root"])
    floor = triage.get("floor")
    if floor not in (None, STANDARD, "HIGH"):
        refuse(f"triage returned unknown floor {floor!r} for PR {pr}")
    diff = run_bytes([
        "git",
        "-C",
        str(row["worktree"]),
        "diff",
        "--no-ext-diff",
        "--binary",
        f"origin/{base}...{row['head_sha']}",
        "--",
    ])
    if diff.returncode != 0:
        detail = os.fsdecode(diff.stderr).strip() or "git diff failed"
        refuse(f"cannot prepare PR {pr} intent diff: {detail}")
    diff_file = diff_path(rundir, pr)
    replace_bytes(diff_file, diff.stdout, temp_prefix=f".{diff_file.name}.")
    stated = extract_stated_intent(body)
    artifact = {
        "type": "campaign-start-judgment",
        "pr": pr,
        "head_sha": row["head_sha"],
        "title": title,
        "body": body,
        "diff_file": os.fspath(diff_file),
        "worktree": row["worktree"],
        "base": base,
        "triage": triage,
        "stated_intent": stated,
    }
    path = judgment_path(rundir, pr)
    replace_text(
        path,
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        temp_prefix=f".{path.name}.",
        encoding="utf-8",
    )
    allowed = [
        tier
        for tier in sorted(T.TIER_VALUES, key=T._TIER_RANK.__getitem__)
        if floor is None or T._TIER_RANK[tier] >= T._TIER_RANK[floor]
    ]
    return {
        "state": "needs-pr-judgment",
        "run_id": str(header["run_id"]),
        "pr": pr,
        "judgment_file": os.fspath(path),
        "head_sha": row["head_sha"],
        "tier_floor": floor,
        "suggested_tier": floor or STANDARD,
        "allowed_tiers": allowed,
        "intent_source": "stated" if stated is not None else "author-required",
        "next": {
            "command": "bind",
            "required_inputs": ["tier", "use-stated-intent" if stated is not None else "intent-file"],
            "argv": [
                sys.executable,
                os.fspath(Path(__file__).resolve()),
                "bind",
                "--checkout",
                os.fspath(repository["project_root"]),
                "--run",
                str(header["run_id"]),
                "--token",
                token,
                "--pr",
                pr,
                "--tier",
                "<tier>",
                "<intent-option>",
            ],
        },
    }


def refresh_lease(ledger: Path, token: str) -> None:
    payload, _ = tool_json([
        sys.executable,
        os.fspath(LEASE_PY),
        "--file",
        os.fspath(ledger.parent / "lease.json"),
        "refresh",
        "--token",
        token,
    ])
    if payload.get("verdict") != "owned":
        refuse(f"lease refresh did not confirm ownership: {payload}")


def adopt_one(repository: dict[str, Path], ledger: Path, run_id: str, pr: str) -> None:
    proc = run_argv([
        sys.executable,
        os.fspath(PR_ADOPT_PY),
        "adopt",
        "--pr",
        pr,
        "--run-id",
        run_id,
        "--file",
        os.fspath(ledger),
        "--tier",
        STANDARD,
        "--worktrees-root",
        os.fspath(repository["worktrees_root"]),
        "--project-root",
        os.fspath(repository["project_root"]),
    ], cwd=repository["project_root"])
    if proc.returncode != 0:
        refuse(f"PR {pr} adoption failed: {proc.stderr.strip() or proc.stdout.strip()}")


def initialize_ci(repository: dict[str, Path], rundir: Path, ledger: Path, rows: list[dict]) -> list[dict]:
    required = run_argv([
        sys.executable,
        os.fspath(CI_STATUS_PY),
        "required-set",
        "--ledger",
        os.fspath(ledger),
    ], cwd=repository["project_root"])
    if required.returncode not in (0, 1):
        refuse(f"required-set initialization failed: {required.stderr.strip() or required.stdout.strip()}")
    actions: list[dict] = []
    for row in rows:
        pr, head_sha = str(row["pr"]), str(row["head_sha"])
        derived = run_argv([
            sys.executable,
            os.fspath(CI_STATUS_PY),
            "derive",
            "--pr",
            pr,
            "--head-sha",
            head_sha,
            "--rundir",
            os.fspath(rundir),
            "--ledger",
            os.fspath(ledger),
        ], cwd=repository["project_root"])
        if derived.returncode not in (0, 1):
            refuse(f"CI derivation for PR {pr} failed: {derived.stderr.strip() or derived.stdout.strip()}")
        try:
            envelope = json.loads(derived.stdout)
        except ValueError as exc:
            refuse(f"CI derivation for PR {pr} returned non-JSON output ({exc})")
        machine_action = "due" if envelope.get("ci") == "red" else "none"
        live = run_argv([
            sys.executable,
            os.fspath(CI_STATUS_PY),
            "liveness",
            "--ledger",
            os.fspath(ledger),
            "--pr",
            pr,
            "--derive-json",
            "-",
            "--machine-action",
            machine_action,
        ], cwd=repository["project_root"], stdin=derived.stdout)
        if live.returncode not in (0, 3):
            refuse(f"CI liveness for PR {pr} failed: {live.stderr.strip() or live.stdout.strip()}")
        try:
            live_state = json.loads(live.stdout)
        except ValueError as exc:
            refuse(f"CI liveness for PR {pr} returned non-JSON output ({exc})")
        if live_state.get("watch_warranted") is True:
            actions.append({
                "kind": "ci-watch",
                "pr": pr,
                "cwd": os.fspath(repository["project_root"]),
                "argv": ["gh", "pr", "checks", pr, "--watch"],
            })
    return actions


def ready_state(repository: dict[str, Path], rundir: Path, run_id: str, actions: list[dict]) -> dict:
    return {
        "state": "ready",
        "run_id": run_id,
        "rundir": os.fspath(rundir),
        "host_actions": actions,
        "carryover_review": {
            "history_dir": os.fspath(repository["project_root"] / ".gauntlet" / "history"),
            "blocking": False,
            "rule_owner": "references/carryover.md#pruning-the-ledger",
        },
    }


def advance_startup(
    repository: dict[str, Path],
    run_id: str,
    token: str,
    *,
    refresh: bool,
) -> dict:
    rundir, ledger, header, rows = load_run(repository, run_id)
    if refresh:
        refresh_lease(ledger, token)
        header, rows = load_ledger(ledger)
    pending = pending_prs(header)
    if not pending:
        return ready_state(repository, rundir, run_id, [])

    for pr in pending:
        row = L.find_row(rows, pr)
        if row is not None and row.get("status") in TERMINAL:
            refuse(f"pending startup PR {pr} already has terminal status {row.get('status')}")
        if row is None:
            adopt_one(repository, ledger, run_id, pr)
            header, rows = load_ledger(ledger)
            row = L.find_row(rows, pr)
        if row is None:
            refuse(f"PR {pr} adoption returned without a ledger row")
        if row.get("status") != L.LIVE_STATUS:
            refuse(f"pending startup PR {pr} is {row.get('status')}, not {L.LIVE_STATUS}; resolve its hold first")
        if row.get("intent", "-") == "-":
            return prepare_judgment(repository, rundir, ledger, pr, token)
        judgment_path(rundir, pr).unlink(missing_ok=True)
        diff_path(rundir, pr).unlink(missing_ok=True)

    actions = initialize_ci(repository, rundir, ledger, rows)
    proc = run_argv([
        sys.executable,
        os.fspath(LEDGER_PY),
        "--file",
        os.fspath(ledger),
        "header",
        "set",
        "pending_adoption",
        "-",
    ])
    if proc.returncode != 0:
        refuse(f"could not clear startup checkpoint: {proc.stderr.strip() or proc.stdout.strip()}")
    return ready_state(repository, rundir, run_id, actions)


def acquire_and_advance(
    repository: dict[str, Path],
    run_id: str,
    token: str,
    heartbeat_id: str,
    *,
    allow_takeover: bool,
) -> dict:
    _rundir, ledger, _header, _rows = load_run(repository, run_id)
    acquire = [
        sys.executable,
        os.fspath(LEASE_PY),
        "--file",
        os.fspath(ledger.parent / "lease.json"),
        "acquire",
        "--token",
        token,
        "--heartbeat-id",
        heartbeat_id,
    ]
    if allow_takeover:
        acquire.append("--allow-takeover")
    payload, _ = tool_json(acquire)
    if payload.get("verdict") not in ("owned", "adopted"):
        refuse(f"lease acquire did not grant ownership: {payload}")
    return advance_startup(repository, run_id, token, refresh=False)


def load_judgment(path: Path, pr: str, head_sha: str) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuse(f"cannot read startup judgment {path}: {exc}")
    if not isinstance(raw, dict) or raw.get("type") != "campaign-start-judgment":
        refuse(f"startup judgment {path} has no campaign-start-judgment record")
    if str(raw.get("pr")) != pr or raw.get("head_sha") != head_sha:
        refuse(f"startup judgment {path} is not bound to PR {pr} at {head_sha}")
    return raw


def validate_tier(repository: dict[str, Path], ledger: Path, row: dict, pr: str, tier: str) -> None:
    header, _ = load_ledger(ledger)
    base, problem = L.require_effective_base(header, row, pr)
    if problem is not None:
        refuse(problem)
    proc = run_argv([
        sys.executable,
        os.fspath(TRIAGE_PY),
        "derive",
        "--worktree",
        str(row["worktree"]),
        "--base",
        f"origin/{base}",
        "--head-sha",
        str(row["head_sha"]),
        "--file",
        os.fspath(ledger),
        "--pr",
        pr,
        "--tier",
        tier,
    ], cwd=repository["project_root"])
    if proc.returncode != 0:
        refuse(f"tier {tier} was refused for PR {pr}: {proc.stderr.strip() or proc.stdout.strip()}")


def bind_judgment(
    repository: dict[str, Path],
    run_id: str,
    token: str,
    pr: str,
    tier: str,
    *,
    use_stated: bool,
    intent_file: "Path | None",
) -> dict:
    rundir, ledger, header, rows = load_run(repository, run_id)
    refresh_lease(ledger, token)
    pending = pending_prs(header)
    if pr not in pending:
        refuse(f"PR {pr} is not in run {run_id}'s pending startup checkpoint")
    row = L.find_row(rows, pr)
    if row is None or row.get("intent", "-") != "-":
        refuse(f"PR {pr} does not have an unbound startup row")
    if row.get("status") != L.LIVE_STATUS:
        refuse(f"PR {pr} is {row.get('status')}, not {L.LIVE_STATUS}; startup will not change its binding")
    if str(row.get("reviews_ok", "0")) != "0":
        refuse(f"PR {pr} has banked review credit; startup will not rewrite its tier/intent")
    if tier not in T.TIER_VALUES:
        refuse(f"tier {tier!r} is not one of {', '.join(sorted(T.TIER_VALUES))}")
    validate_tier(repository, ledger, row, pr, tier)

    artifact_path = judgment_path(rundir, pr)
    artifact = load_judgment(artifact_path, pr, str(row["head_sha"]))
    if use_stated:
        raw_intent = artifact.get("stated_intent")
        if not isinstance(raw_intent, str):
            refuse(f"PR {pr} has no usable stated intent; supply --intent-file")
        provenance = "stated"
    else:
        if intent_file is None:
            refuse("authored intent requires --intent-file")
        try:
            raw_intent = intent_file.read_text(encoding="utf-8")
        except OSError as exc:
            refuse(f"cannot read authored intent {intent_file}: {exc}")
        provenance = "authored"
    target = RP.intent_path(rundir, pr)
    try:
        RP.parse_intent(raw_intent, target)
        defaults = L.default_non_goals(header)
        final_intent = RP.merge_default_non_goals(raw_intent, defaults, target)
        RP.parse_intent(final_intent, target)
        RP.check_default_non_goals(final_intent, defaults, target)
    except (RP.Defect, ValueError) as exc:
        refuse(f"PR {pr} intent is unusable: {exc}")
    replace_text(
        target,
        final_intent,
        temp_prefix=f".{target.name}.",
        encoding="utf-8",
    )
    check = run_argv([
        sys.executable,
        os.fspath(REVIEW_PASS_PY),
        "intent-check",
        "--file",
        os.fspath(target),
        "--ledger",
        os.fspath(ledger),
    ])
    if check.returncode != 0:
        refuse(f"PR {pr} intent-check failed: {check.stderr.strip() or check.stdout.strip()}")
    tier_update = run_argv([
        sys.executable,
        os.fspath(LEDGER_PY),
        "--file",
        os.fspath(ledger),
        "set",
        "--pr",
        pr,
        "--tier",
        tier,
    ])
    if tier_update.returncode != 0:
        refuse(f"could not bind PR {pr} tier: {tier_update.stderr.strip() or tier_update.stdout.strip()}")
    dispatch = run_argv([
        sys.executable,
        os.fspath(LEDGER_PY),
        "--file",
        os.fspath(ledger),
        "dispatch-check",
        "--pr",
        pr,
    ])
    if dispatch.returncode != 0:
        refuse(f"PR {pr} label binding is not permitted: {dispatch.stderr.strip() or dispatch.stdout.strip()}")
    github_repo = resolve_github_repo(repository["project_root"])
    mirror = run_argv(
        label_mirror_argv(ledger, pr, github_repo),
        cwd=repository["project_root"],
    )
    if mirror.returncode != 0:
        refuse(f"could not mirror PR {pr} labels: {mirror.stderr.strip() or mirror.stdout.strip()}")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    complete = run_argv([
        sys.executable,
        os.fspath(LEDGER_PY),
        "--file",
        os.fspath(ledger),
        "set",
        "--pr",
        pr,
        "--intent",
        f"{provenance}@{stamp}",
    ])
    if complete.returncode != 0:
        refuse(f"could not complete PR {pr} intent binding: {complete.stderr.strip() or complete.stdout.strip()}")
    artifact_path.unlink(missing_ok=True)
    diff_path(rundir, pr).unlink(missing_ok=True)
    return advance_startup(repository, run_id, token, refresh=False)


def resume_state(repository: dict[str, Path], host: str, run_id: str, *, allow_takeover: bool) -> dict:
    rundir, _ledger, header, _rows = load_run(repository, run_id)
    lease, code = tool_json([
        sys.executable,
        os.fspath(LEASE_PY),
        "--file",
        os.fspath(rundir / "lease.json"),
        "read",
    ], accepted=(0, 1))
    verdict = lease.get("verdict")
    if verdict == "corrupt" or code not in (0,):
        return {"state": "refused", "run_id": run_id, "reason": lease.get("error", "corrupt lease")}
    if verdict == "held" and not allow_takeover:
        return {
            "state": "needs-user",
            "run_id": run_id,
            "reason": "another agent holds a fresh lease",
            "held_by": lease.get("agent"),
            "allowed_decisions": ["leave-running", "take-over"],
        }
    if verdict not in ("absent", "stale"):
        if verdict != "held":
            refuse(f"lease read returned unknown verdict {verdict!r}")
    token = LEASE.mint_token()
    return host_arm_state(
        host=host,
        repository=repository,
        run_id=run_id,
        rundir=rundir,
        token=token,
        reviewer=str(header["reviewer"]),
        skill_version=str(header["skill_version"]),
        allow_takeover=allow_takeover,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="preflight a complete PR set, then create one resumable fresh run")
    new.add_argument("--checkout", required=True)
    new.add_argument("--host", required=True, choices=tuple(HOST_INVOCATIONS))
    new.add_argument("--reviewer", required=True,
                     help="choice already resolved from explicit invocation/trusted host state")
    new.add_argument("--pr", action="append", default=[])
    new.add_argument("--default-non-goals", default="[]")

    take = sub.add_parser("take", help="acquire after host continuity is established, then advance startup")
    take.add_argument("--checkout", required=True)
    take.add_argument("--run", required=True)
    take.add_argument("--token", required=True)
    take.add_argument("--heartbeat-id", required=True)
    take.add_argument("--allow-takeover", action="store_true",
                      help="take a fresh foreign lease only after the user approved takeover")

    advance = sub.add_parser("advance", help="refresh ownership and resume startup from durable state")
    advance.add_argument("--checkout", required=True)
    advance.add_argument("--run", required=True)
    advance.add_argument("--token", required=True)

    bind = sub.add_parser("bind", help="bind one PR's semantic tier and intent, then advance startup")
    bind.add_argument("--checkout", required=True)
    bind.add_argument("--run", required=True)
    bind.add_argument("--token", required=True)
    bind.add_argument("--pr", required=True)
    bind.add_argument("--tier", required=True)
    intent = bind.add_mutually_exclusive_group(required=True)
    intent.add_argument("--use-stated-intent", action="store_true")
    intent.add_argument("--intent-file", type=Path)

    resume = sub.add_parser("resume", help="classify an existing startup and return its next host action")
    resume.add_argument("--checkout", required=True)
    resume.add_argument("--host", required=True, choices=tuple(HOST_INVOCATIONS))
    resume.add_argument("--run", required=True)
    resume.add_argument("--allow-takeover", action="store_true",
                        help="continue a previously reported held lease after user approval")

    sub.add_parser("self-test", help="run the sibling startup protocol fixtures")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return run_sibling_suite(
            SIBLING,
            "campaign_start_test",
            failure=SelfTestFailure,
            subject="the code-driven campaign startup protocol",
            per_case_dir=True,
        )
    try:
        if args.cmd == "new":
            result = prepare_new(
                checkout=Path(args.checkout),
                host=args.host,
                reviewer=args.reviewer,
                prs=args.pr,
                default_non_goals=args.default_non_goals,
            )
        else:
            repository = resolve_repository_context(Path(args.checkout))
            if args.cmd == "take":
                result = acquire_and_advance(
                    repository,
                    args.run,
                    args.token,
                    args.heartbeat_id,
                    allow_takeover=args.allow_takeover,
                )
            elif args.cmd == "advance":
                result = advance_startup(repository, args.run, args.token, refresh=True)
            elif args.cmd == "bind":
                result = bind_judgment(
                    repository,
                    args.run,
                    args.token,
                    normalize_prs([args.pr])[0],
                    args.tier,
                    use_stated=args.use_stated_intent,
                    intent_file=args.intent_file,
                )
            else:
                result = resume_state(repository, args.host, args.run, allow_takeover=args.allow_takeover)
    except Refusal as exc:
        emit({"state": "refused", "reason": str(exc)})
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
