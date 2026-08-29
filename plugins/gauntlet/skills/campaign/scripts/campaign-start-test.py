#!/usr/bin/env python3
"""Offline fixtures for the code-owned fresh campaign startup protocol."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import checker

HERE = Path(__file__).resolve().parent
C = load_sibling("campaign_start_subject", HERE, "campaign-start.py", register=True)
check = checker(C.SelfTestFailure)


def init_repository(work: Path) -> Path:
    repository = work / "repository"
    repository.mkdir()
    proc = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repository)],
        capture_output=True,
        text=True,
        check=False,
    )
    check(proc.returncode == 0, f"git init failed: {proc.stderr}")
    (repository / ".gitignore").write_text(".gauntlet/\n", encoding="utf-8")
    return repository


def view(pr: int, *, fork: bool = False, labels: "list[dict] | None" = None) -> dict:
    return {
        "number": pr,
        "title": f"PR {pr}",
        "headRefName": f"feature-{pr}",
        "headRefOid": f"{pr:x}"[-1] * 40,
        "baseRefName": "main",
        "labels": labels or [],
        "state": "OPEN",
        "isCrossRepository": fork,
        "headRepositoryOwner": {"login": "owner"},
        "headRepository": {"name": "repository"},
    }


def fetcher(views: dict[int, dict]):
    def fetch(pr: str, *, fields: str, cwd: str):
        check(fields == C.A.VIEW_FIELDS, "preflight must request the adoption owner's complete view")
        check(Path(cwd).name == "repository", "preflight must run in the resolved repository root")
        return views[int(pr)], None

    return fetch


def run_creator(run_id: str = "g260829-1200-aabbccdd"):
    def create(scratch_root: Path):
        rundir = scratch_root / run_id
        rundir.mkdir(parents=True)
        return run_id, rundir

    return create


def initialized_run(repository: Path, run_id: str, pending: str) -> tuple[dict, Path, Path]:
    context = C.resolve_repository_context(repository)
    rundir = context["scratch_root"] / run_id
    rundir.mkdir(parents=True)
    ledger = rundir / "state.jsonl"
    C.L.init_run(
        ledger,
        run_id=run_id,
        pending_adoption=pending,
        reviewer="claude",
        skill_version="0.9.1",
    )
    return context, rundir, ledger


def add_bound_row(
    ledger: Path,
    repository: Path,
    pr: str,
    *,
    intent: str,
    status: str = "in_review",
) -> None:
    proc = subprocess.run([
        sys.executable,
        str(C.LEDGER_PY),
        "--file",
        str(ledger),
        "add-row",
        "--pr",
        pr,
        "--branch",
        f"feature-{pr}",
        "--worktree",
        str(repository),
        "--worktree-owned",
        "no",
        "--branch-owned",
        "no",
        "--head-sha",
        f"{int(pr):x}"[-1] * 40,
        "--base-branch",
        "main",
        "--tier",
        "STANDARD",
        "--ci",
        "pending",
        "--status",
        status,
        "--intent",
        intent,
    ], capture_output=True, text=True, check=False)
    check(proc.returncode == 0, f"could not prepare test row: {proc.stderr}")


def t_normalize_pr_set(_work: Path) -> None:
    check(C.normalize_prs(["#09", "10"]) == ["9", "10"], "PRs must be canonical positive integers")
    for values in ([], ["0"], ["x"], ["9", "#09"]):
        try:
            C.normalize_prs(values)
        except C.Refusal:
            continue
        raise C.SelfTestFailure(f"startup accepted malformed PR set {values!r}")


def t_repository_context_is_explicit(work: Path) -> None:
    repository = init_repository(work)
    nested = repository / "nested"
    nested.mkdir()
    context = C.resolve_repository_context(nested)
    check(context["project_root"] == repository.resolve(), "checkout resolution must return the git root")
    check(context["scratch_root"] == repository.resolve() / ".gauntlet" / "tmp",
          "scratch state must be rooted under the supplied checkout")
    C.require_gauntlet_ignored(repository)


def t_active_manifest_version(_work: Path) -> None:
    claude = C.active_skill_version("claude-code")
    codex = C.active_skill_version("codex")
    check(claude == codex, "both host manifests beside the active skill must report the same version")
    check(bool(claude), "the active skill version must not be empty")


def t_preflight_finishes_before_state(work: Path) -> None:
    repository = init_repository(work)
    created = False

    def must_not_create(_scratch_root: Path):
        nonlocal created
        created = True
        raise C.SelfTestFailure("run state was created before every PR passed preflight")

    try:
        C.prepare_new(
            checkout=repository,
            host="codex",
            reviewer="claude",
            prs=["1", "2"],
            default_non_goals="[]",
            fetch=fetcher({1: view(1), 2: view(2, fork=True)}),
            run_creator=must_not_create,
            token_mint=lambda: "token",
            version="0.9.1",
        )
    except C.Refusal as exc:
        check("fork PR" in str(exc), f"preflight returned the wrong refusal: {exc}")
    else:
        raise C.SelfTestFailure("startup accepted a fork PR")
    check(not created, "startup must not create a run before full-set preflight succeeds")
    check(not (repository / ".gauntlet").exists(), "failed preflight must leave no campaign state")


def t_new_publishes_complete_header(work: Path) -> None:
    repository = init_repository(work)
    result = C.prepare_new(
        checkout=repository,
        host="codex",
        reviewer="claude",
        prs=["#01", "2"],
        default_non_goals='["generated files"]',
        fetch=fetcher({1: view(1), 2: view(2)}),
        run_creator=run_creator(),
        token_mint=lambda: "token-aabbccdd",
        version="0.9.1",
    )
    ledger = Path(result["rundir"]) / "state.jsonl"
    header, rows = C.L.load(ledger)
    check(result["state"] == "needs-host-arm", "new must stop at the host-continuity boundary")
    check(header["run_id"] == result["run_id"], "the complete header must own the minted run id")
    check(header["pending_adoption"] == "1 2", "the complete header must publish the full PR checkpoint")
    check(header["reviewer"] == "claude", "the complete header must publish the trusted reviewer choice")
    check(header["skill_version"] == "0.9.1", "the complete header must publish the active skill version")
    check(C.L.default_non_goals(header) == ["generated files"], "run defaults must be canonicalized at init")
    check(rows == [], "new must not adopt a PR before the host establishes continuity")
    check(sorted(path.name for path in Path(result["rundir"]).iterdir()) == ["state.jsonl"],
          "the ledger must be the only durable startup state")


def t_host_actions_are_typed(work: Path) -> None:
    repository = init_repository(work)
    context = C.resolve_repository_context(repository)
    rundir = repository / ".gauntlet" / "tmp" / "g260829-1200-aabbccdd"
    claude = C.host_arm_state(
        host="claude-code",
        repository=context,
        run_id=rundir.name,
        rundir=rundir,
        token="token",
        reviewer="codex",
        skill_version="0.9.1",
    )
    codex = C.host_arm_state(
        host="codex",
        repository=context,
        run_id=rundir.name,
        rundir=rundir,
        token="token",
        reviewer="claude",
        skill_version="0.9.1",
    )
    check(claude["host_actions"][-1]["kind"] == "arm-primary-heartbeat",
          "Claude Code must receive the scheduled-heartbeat action")
    check(claude["host_actions"][-1]["must_run_last"] is True,
          "the scheduled heartbeat must remain the host's last action")
    check(codex["host_actions"][-1]["kind"] == "establish-bounded-wait",
          "Codex must receive its scheduler-less continuation action")
    check(codex["host_actions"][-1]["suggested_heartbeat_id"] == f"bounded-wait:{rundir.name}",
          "the bounded-wait host action must suggest a run-bound proof")
    takeover = C.host_arm_state(
        host="codex",
        repository=context,
        run_id=rundir.name,
        rundir=rundir,
        token="token",
        reviewer="claude",
        skill_version="0.9.1",
        allow_takeover=True,
    )
    check(takeover["next"]["argv"][-1] == "--allow-takeover",
          "an approved takeover must remain explicit at the lease acquisition door")


def t_stated_intent_is_exact(_work: Path) -> None:
    valid = (
        "Introduction.\n\n"
        "## Purpose\n- Start campaigns deterministically.\n"
        "## Non-goals\n"
        "## Threat model\n- The repository owner supplies PR metadata.\n"
        "## Notes\n- Outside the intent.\n"
    )
    expected = (
        "## Purpose\n- Start campaigns deterministically.\n"
        "## Non-goals\n"
        "## Threat model\n- The repository owner supplies PR metadata.\n"
    )
    check(C.extract_stated_intent(valid) == expected, "usable stated intent must be extracted verbatim")
    check(C.extract_stated_intent("## Purpose\n- x\n## Threat model\n- y\n") is None,
          "a body missing one intent section must require authorship")
    managed = expected.replace("## Non-goals\n", f"## Non-goals\n{C.RP.MANAGED_START}\n")
    check(C.extract_stated_intent(managed) is None, "managed run defaults must never be accepted as PR input")


def t_judgment_is_bound_to_head(work: Path) -> None:
    path = work / "startup-judgment-7.json"
    path.write_text(json.dumps({
        "type": "campaign-start-judgment",
        "pr": "7",
        "head_sha": "a" * 40,
    }), encoding="utf-8")
    check(C.load_judgment(path, "7", "a" * 40)["pr"] == "7",
          "a matching judgment artifact must load")
    try:
        C.load_judgment(path, "7", "b" * 40)
    except C.Refusal:
        return
    raise C.SelfTestFailure("a judgment prepared for an old head was accepted")


def t_diff_transport_preserves_bytes(work: Path) -> None:
    proc = C.run_bytes([
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(bytes([255, 0, 10]))",
    ])
    check(proc.returncode == 0, "the exact-byte subprocess fixture failed")
    path = work / "startup-diff-7.patch"
    C.replace_bytes(path, proc.stdout, temp_prefix=".startup-diff-7.patch.")
    check(path.read_bytes() == bytes([255, 0, 10]), "diff transport decoded or changed non-UTF-8 bytes")


def t_label_mirror_is_repo_scoped(work: Path) -> None:
    ledger = work / "state.jsonl"
    argv = C.label_mirror_argv(ledger, "7", "owner/repository")
    check(argv[-2:] == ["--repo", "owner/repository"], "label mirror must receive an explicit owner/name")
    check(argv[argv.index("--ledger") + 1] == str(ledger), "label mirror must receive this run's ledger")


def t_advance_adopts_then_requests_judgment(work: Path) -> None:
    repository = init_repository(work)
    run_id = "g260829-1200-aabbccdd"
    context, rundir, ledger = initialized_run(repository, run_id, "7")
    adopted: list[str] = []
    real_adopt = getattr(C, "adopt_one")
    real_prepare = getattr(C, "prepare_judgment")

    def adopt(_context: dict, target: Path, actual_run: str, pr: str) -> None:
        check(target == ledger and actual_run == run_id, "advance must adopt into the bound run ledger")
        adopted.append(pr)
        add_bound_row(target, repository, pr, intent="-")

    def prepare(_context: dict, actual_rundir: Path, target: Path, pr: str, token: str) -> dict:
        check(actual_rundir == rundir and target == ledger, "judgment must use the adopted run artifacts")
        check(token == "token-aabbccdd", "judgment handoff must preserve the owner token")
        return {"state": "needs-pr-judgment", "pr": pr}

    setattr(C, "adopt_one", adopt)
    setattr(C, "prepare_judgment", prepare)
    try:
        result = C.advance_startup(context, run_id, "token-aabbccdd", refresh=False)
    finally:
        setattr(C, "adopt_one", real_adopt)
        setattr(C, "prepare_judgment", real_prepare)
    check(adopted == ["7"], "advance must adopt the first missing checkpoint row exactly once")
    check(result == {"state": "needs-pr-judgment", "pr": "7"},
          "advance must stop at the semantic boundary after adoption")


def t_advance_initializes_then_commits_ready(work: Path) -> None:
    repository = init_repository(work)
    run_id = "g260829-1200-aabbccdd"
    context, rundir, ledger = initialized_run(repository, run_id, "7")
    add_bound_row(ledger, repository, "7", intent="stated@2026-08-29T12:00:00+00:00")
    real_initialize = getattr(C, "initialize_ci")

    def initialize(actual_context: dict, actual_rundir: Path, target: Path, rows: list[dict]) -> list[dict]:
        check(actual_context == context and actual_rundir == rundir and target == ledger,
              "CI initialization must use the bound repository and run")
        check([row["pr"] for row in rows] == ["7"], "CI initialization must receive every bound row")
        return [{"kind": "ci-watch", "pr": "7"}]

    setattr(C, "initialize_ci", initialize)
    try:
        result = C.advance_startup(context, run_id, "token-aabbccdd", refresh=False)
    finally:
        setattr(C, "initialize_ci", real_initialize)
    header, _rows = C.L.load(ledger)
    check(result["state"] == "ready", "fully bound startup must return ready")
    check(result["host_actions"] == [{"kind": "ci-watch", "pr": "7"}],
          "ready must return only CI watches warranted by initialization")
    check(header["pending_adoption"] == "-", "ready must atomically expose the cleared startup checkpoint")


def t_advance_refuses_a_held_row(work: Path) -> None:
    repository = init_repository(work)
    run_id = "g260829-1200-aabbccdd"
    context, _rundir, ledger = initialized_run(repository, run_id, "7")
    add_bound_row(ledger, repository, "7", intent="-", status="awaiting-user")
    try:
        C.advance_startup(context, run_id, "token-aabbccdd", refresh=False)
    except C.Refusal as exc:
        check("awaiting-user" in str(exc), f"held startup row was refused for the wrong reason: {exc}")
    else:
        raise C.SelfTestFailure("startup prepared a judgment for a held PR")
    header, _rows = C.L.load(ledger)
    check(header["pending_adoption"] == "7", "a held startup row must preserve the recovery checkpoint")


CASES = [
    ("pr-set", "startup canonicalizes the complete nonempty PR set", t_normalize_pr_set),
    ("repository-context", "all startup paths derive from the supplied checkout", t_repository_context_is_explicit),
    ("active-version", "startup reads the synchronized manifests beside the active skill",
     t_active_manifest_version),
    ("preflight-before-state", "every PR passes read-only preflight before state exists",
     t_preflight_finishes_before_state),
    ("atomic-complete-header", "new publishes one complete recoverable header", t_new_publishes_complete_header),
    ("typed-host-actions", "the protocol stops only at explicit host capability boundaries", t_host_actions_are_typed),
    ("stated-intent", "only a complete exact PR-body intent bypasses authorship", t_stated_intent_is_exact),
    ("head-bound-judgment", "semantic input cannot cross a head-SHA change", t_judgment_is_bound_to_head),
    ("exact-diff-bytes", "diff transport preserves bytes that are not UTF-8", t_diff_transport_preserves_bytes),
    ("repo-scoped-label", "status-label mutation names the repository explicitly", t_label_mirror_is_repo_scoped),
    ("adopt-to-judgment", "advance adopts one missing row and stops at its semantic boundary",
     t_advance_adopts_then_requests_judgment),
    ("initialized-to-ready", "CI initialization precedes the startup commit point",
     t_advance_initializes_then_commits_ready),
    ("held-row-frozen", "startup does not bind or initialize a held PR", t_advance_refuses_a_held_row),
]
