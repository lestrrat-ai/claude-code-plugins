#!/usr/bin/env python3
"""Mechanical fixtures for campaign's typed runtime transport contract."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"
COPILOT = ROOT.parent / "copilot-address-reviews"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read(name: str) -> str:
    return (REFS / name).read_text(encoding="utf-8")


def check_document_contract() -> None:
    runtime = read("runtime-adapter.md")
    stage = read("stage-2-review-gate.md")
    reviewer = read("reviewer.md")
    cross = read("cross-agent-reviewers.md")
    adoption = read("pr-adoption.md")
    run_identity = read("run-identity-and-lease.md")
    merge = read("stage-3-merge.md")
    root_cause = read("root-cause-pass.md")
    copilot = (COPILOT / "SKILL.md").read_text(encoding="utf-8")

    for needle in (
        "## Typed repository context and data/process boundary",
        "resolve_repository_context(checkout: Path) -> RepositoryContext",
        "create_run_directory(repository: RepositoryContext, run_id: Text) -> Path",
        "default_worktree(repository: RepositoryContext, head_ref_name: Text) -> Path",
        "run_argv(argv: list[Text]",
        "bind_review_prompt(template: Bytes",
        "<TRANSPORT-RECORD>",
        '"native-worker-write" | "external-process-capture"',
        "ReviewIsolationCapability",
        "external_retry_spent: Bool",
        'event: "selected" | "external-system-failure" | "native-system-failure"',
        "current Claude Code and Codex adapters",
        "both external routes are unavailable and take `fallback-native`",
        "Missing native OS/startup controls alone never select",
    ):
        require(needle in runtime, f"runtime adapter lost typed owner: {needle}")

    for needle in (
        "TRANSPORT is this JSON-decoded ReviewTransport record:",
        'TRANSPORT.report.producer is "native-worker-write"',
        '"external-process-capture", return the report only',
        'RUN_ARGV(["git", "-C", TRANSPORT.worktree, "diff"',
        'RUN_ARGV(["python3", TRANSPORT.emit_progress_path',
        'RUN_ARGV(["python3", TRANSPORT.emit_finding_path',
    ):
        require(needle in stage, f"review prompt lost typed operation: {needle}")

    require("producer rule applies to initial launch, relaunch, and native fallback" in reviewer,
            "native report producer no longer covers every attempt state")
    require('"-C", transport.review_root, "-o", transport.report.path, "-"' in cross,
            "external Codex argv contract drifted")
    require('"--add-dir", transport.worktree' in cross and
            "stdout_file: transport.report.path" in cross,
            "external Claude argv/capture contract drifted")
    require("parse_nul_porcelain_for_exact_branch" in adoption and
            "default_worktree(repository, headRefName)" in adoption and
            "repository.project_root" in adoption,
            "adoption no longer preserves typed branch/path data")
    require("path_join(project_root" not in adoption and "], project_root)" not in adoption,
            "adoption restored an unresolved project_root consumer")
    require("create_run_directory(repository, run_id)" in run_identity,
            "fresh-run creation bypasses the repository context owner")
    require('cwd: repository.project_root' in merge and "argv: [\"git\", \"fetch\"" in merge,
            "merge fetches bypass the typed repository context")
    require("git -C $" not in merge and "cwd: project_root" not in stage,
            "merge/pre-review restored an ambient or unresolved Git cwd")
    require('"bash", fetch_review_items_script, "--tmp-dir", repository.scratch_root, pr_url' in copilot and
            'path_join(repository.scratch_root, "copilot-review-items.json")' in copilot,
            "Copilot scratch create/read bypasses the repository context")

    for needle in (
        "Only `launch-external` or `retry-external` uses the commands below",
        "does not materialize or test the view",
        "owned transition instead of constructing this record",
    ):
        require(needle in cross, f"cross-agent capability/fallback contract drifted: {needle}")
    require("no other action constructs this external record" in stage,
            "Stage 2 launches external argv outside the owned transition")
    retired_same_enumeration = "same enumeration " + "independently"
    retired_parallel_role = "parallel adversarial " + "reviewer"
    retired_supplementary_role = "supplementary " + "enumeration"
    require("mandatory dedicated native session-class role" in root_cause and
            retired_same_enumeration not in root_cause and
            retired_parallel_role not in root_cause,
            "root-cause mapper regained an undefined external supplementary lifecycle")
    require(not any(term in cross.lower() for term in ("mapper", retired_supplementary_role)),
            "Stage 2 cross-agent transport was repurposed as a mapper lifecycle")

    live_docs = [ROOT / "SKILL.md", ROOT / "README.md", COPILOT / "SKILL.md", COPILOT / "README.md"]
    live_docs.extend(sorted(REFS.glob("*.md")))
    ambient_project = "$" + "PROJECT"
    project_hits = [str(path) for path in live_docs
                    if ambient_project in path.read_text(encoding="utf-8")]
    require(not project_hits,
            f"live repository operations restored ambient {ambient_project}: {project_hits}")

    # HISTORICAL regression witnesses from b1532eb. They remain only as negative assertions here; no
    # live procedure may contain them because the typed forms above are now the executable contract.
    forbidden = {
        "stage-2-review-gate.md": (
            'git -C "<worktree>"',
            'python3 "<SCRIPT>"',
            'python3 "<FINDING-SCRIPT>"',
            'git fetch origin "refs/heads/<base>',
            '-C "<review-root>"',
            '< "<review-root>/<prompt-file>"',
        ),
        "cross-agent-reviewers.md": (
            '-C "<review-root>"',
            '--add-dir "<worktree>"',
            '< "<review-root>/<prompt-file>"',
            '> "<review-root>/<review-output>"',
        ),
        "pr-adoption.md": (
            'refs/heads/<headRefName>:refs/remotes/origin/<headRefName>',
            'worktree add $PROJECT/.worktrees/<headRefName>',
            'awk -v b="refs/heads/<headRefName>"',
        ),
    }
    documents = {name: read(name) for name in forbidden}
    for name, needles in forbidden.items():
        for needle in needles:
            require(needle not in documents[name],
                    f"{name} restored dynamic shell-source template: {needle}")


def run_hostile_fixtures() -> None:
    with tempfile.TemporaryDirectory(prefix="gauntlet transport '") as raw:
        root = Path(raw)
        marker_dollar = root / "DOLLAR_EXECUTED"
        marker_tick = root / "TICK_EXECUTED"
        hostile = [
            f"path with spaces/$(touch {marker_dollar})",
            f"script`touch {marker_tick}`path",
            "single'quote",
            'double"quote',
            "line one\nline two",
            "--leading-option",
            "payload ${IFS} and unicode 雪",
        ]

        record = {
            "attempt": {"pr": 58, "pass": 5, "launch_attempt": 2},
            "review_root": hostile[0],
            "worktree": hostile[1],
            "base": "base$(printf${IFS}BAD)",
            "prompt_path": hostile[6],
            "plan_path": hostile[2],
            "progress_path": hostile[3],
            "findings_path": hostile[4],
            "emit_progress_path": hostile[5],
            "emit_finding_path": hostile[6],
            "report": {"producer": "native-worker-write", "path": hostile[0]},
        }
        encoded_record = json.dumps(record, ensure_ascii=False)
        require(json.loads(encoded_record) == record,
                "JSON transport record changed bytes/fields")

        template = b"before <TRANSPORT-RECORD> middle <INTENT> after"
        intent = b"literal <TRANSPORT-RECORD> and <INTENT> must not be rebound"
        before_record, tail = template.split(b"<TRANSPORT-RECORD>", 1)
        between, after_intent = tail.split(b"<INTENT>", 1)
        bound = before_record + encoded_record.encode() + between + intent + after_intent
        require(bound.endswith(intent + b" after"), "prompt binding rescanned inserted intent bytes")

        for launch_attempt in (1, 2, 7):
            for transport, producer in (
                ("native-codex", "native-worker-write"),
                ("native-claude", "native-worker-write"),
                ("native-codex-fallback", "native-worker-write"),
                ("native-claude-fallback", "native-worker-write"),
                ("external-codex", "external-process-capture"),
                ("external-claude", "external-process-capture"),
            ):
                owners = [producer == "native-worker-write", producer == "external-process-capture"]
                require(sum(owners) == 1,
                        f"{transport} attempt {launch_attempt} does not have exactly one report owner")

        # Exercise the documented shell-only adapter: mechanically encode the complete argv list.
        probe = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *hostile]
        completed = subprocess.run(
            ["sh", "-c", shlex.join(probe)],
            text=True,
            capture_output=True,
            check=True,
        )
        require(json.loads(completed.stdout) == hostile,
                "mechanical shell encoding failed one-argv/exact-text preservation")
        require(not marker_dollar.exists() and not marker_tick.exists(),
                "hostile argv executed command syntax")

        ref = "refs/heads/base$(printf${IFS}GAUNTLET_REF_EXEC)"
        ref_check = subprocess.run(["git", "check-ref-format", ref], capture_output=True)
        require(ref_check.returncode == 0, "hostile ref fixture is not a valid Git ref")
        head_name = f"topic$(touch {marker_dollar})/line two"
        default_worktree = root / ".worktrees" / head_name
        require(str(default_worktree).endswith(head_name),
                "typed project/head path join changed the branch text")
        require(not marker_dollar.exists(), "typed adoption path join executed branch syntax")

        input_path = root / "prompt $(must stay literal)\nbytes"
        output_path = root / "report `must stay literal` bytes"
        payload = b"intent $(not code)\nquote=' backtick=` nul-free\x00-adjacent"
        # NUL is legal file content even though it cannot be argv; stdin/stdout must remain byte-exact.
        input_path.write_bytes(payload)
        with input_path.open("rb") as source, output_path.open("wb") as sink:
            subprocess.run(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                stdin=source,
                stdout=sink,
                check=True,
            )
        require(output_path.read_bytes() == payload, "stdin/stdout file transport changed prompt bytes")
        require(not marker_dollar.exists() and not marker_tick.exists(),
                "file transport executed path syntax")


def resolve_repository_context_fixture(checkout: Path, env: dict[str, str]) -> tuple[dict[str, Path], list[str]]:
    argv = ["git", "-C", os.fspath(checkout), "rev-parse", "--show-toplevel"]
    completed = subprocess.run(argv, capture_output=True, check=True, env=env)
    require(completed.stdout.endswith(b"\n"), "repository resolver output lost its record terminator")
    raw_root = completed.stdout[:-1]
    require(raw_root, "repository resolver accepted an empty root")
    project_root = Path(os.fsdecode(raw_root))
    require(project_root.is_absolute(), "repository resolver returned a non-absolute root")
    return {
        "project_root": project_root,
        "scratch_root": project_root / ".gauntlet" / "tmp",
        "worktrees_root": project_root / ".worktrees",
    }, argv


def run_repository_context_fixtures() -> None:
    with tempfile.TemporaryDirectory(prefix="gauntlet repository context ") as raw:
        outer = Path(raw)
        repository_root = outer / "--root with spaces\nand-newline"
        checkout = repository_root / "--nested checkout\nand-newline"
        checkout.mkdir(parents=True)
        subprocess.run(["git", "-C", repository_root, "init", "-q"], check=True)

        env = dict(os.environ)
        env.pop("PROJECT", None)
        repository, resolver_argv = resolve_repository_context_fixture(checkout, env)
        require(resolver_argv == ["git", "-C", os.fspath(checkout), "rev-parse", "--show-toplevel"],
                "repository resolver shifted or split hostile checkout argv")
        require(repository["project_root"] == repository_root,
                "repository resolver changed whitespace/newline path bytes")

        run_id = "g260716-1200-a1b2c3d4"
        head_name = "--topic with spaces\nand-newline"
        scratch_root = repository["scratch_root"]
        rundir = scratch_root / run_id
        worktree = repository["worktrees_root"] / head_name
        map_a_paths = {
            "A01 copilot scratch create": scratch_root,
            "A02 copilot scratch read": scratch_root / "copilot-review-items.json",
            "A06 campaign scratch create": scratch_root,
            "A07 campaign scratch read/resume": rundir,
            "A08 campaign atomic run create": rundir,
            "A14 adoption created worktree": worktree,
        }
        for cell, derived in map_a_paths.items():
            require(derived.is_absolute() and
                    (derived == repository_root or repository_root in derived.parents),
                    f"{cell} escaped the repository: {derived!s}")
        require(scratch_root != Path("/.gauntlet/tmp"),
                "absent PROJECT regressed to the root-level scratch path")

        mkdir_parent = ["mkdir", "-p", "--", os.fspath(scratch_root)]
        mkdir_run = ["mkdir", "--", os.fspath(rundir)]
        subprocess.run(mkdir_parent, check=True, env=env)
        subprocess.run(mkdir_run, check=True, env=env)
        collision = subprocess.run(mkdir_run, capture_output=True, env=env)
        require(collision.returncode != 0, "atomic run directory create accepted a collision")

        fetch_script = COPILOT / "scripts" / "fetch-review-items.sh"
        pr_url = "https://github.com/example/repo/pull/58"
        copilot_argv = ["bash", os.fspath(fetch_script), "--tmp-dir", os.fspath(scratch_root), pr_url]
        require(copilot_argv[2:] == ["--tmp-dir", os.fspath(scratch_root), pr_url],
                "Copilot fetch argv shifted around the hostile repository root")
        worklist = scratch_root / "copilot-review-items.json"
        worklist.write_bytes(b"[]\n")
        require(worklist.read_bytes() == b"[]\n", "Copilot scratch read resolved a different path")
        for sibling in ("copilot-review-items.raw.json", "gh-pr-view.json",
                        "gh-pr-review-threads.json"):
            require((scratch_root / sibling).parent == scratch_root,
                    f"Copilot scratch sibling escaped its owner: {sibling}")

        base = "--base with spaces\nand-newline"
        refresh_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
        adoption_fetch = ["git", "fetch", "origin", refresh_ref]
        merge_direct_fetch = ["git", "fetch", "origin", f"{base}:{base}"]
        map_a_git = {
            "A05 copilot process cwd": (copilot_argv, repository["project_root"]),
            "A15 adoption/pre-review Git cwd": (adoption_fetch, repository["project_root"]),
            "A20 merge Git cwd": (merge_direct_fetch, repository["project_root"]),
        }
        for cell, (argv, cwd) in map_a_git.items():
            require(len(argv) >= 4 and cwd == repository_root and cwd.is_absolute(),
                    f"{cell} shifted argv or lost the resolved absolute cwd")
        require(adoption_fetch == ["git", "fetch", "origin", refresh_ref] and
                merge_direct_fetch == ["git", "fetch", "origin", f"{base}:{base}"],
                "repository Git argv shifted a hostile ref")


def review_action(capability: dict[str, object], external_retry_spent: bool = False,
                  external_failed: bool = False, native_exhausted: bool = False) -> str:
    route = capability["route"]
    external_available = all(capability[name] for name in (
        "fresh_conversation", "instruction_neutral_startup",
        "candidate_read_only", "artifacts_only_writable",
    ))
    if route.startswith("external-"):
        if not external_available:
            return "fallback-native"
        if external_failed:
            return "fallback-native" if external_retry_spent else "retry-external"
        return "launch-external"
    if native_exhausted:
        return "park-machine-blocker"
    return "launch-native"


def run_isolation_transition_fixtures() -> None:
    for route in ("external-codex", "external-claude"):
        current = {
            "route": route,
            "fresh_conversation": True,
            "instruction_neutral_startup": False,
            "candidate_read_only": False,
            "artifacts_only_writable": False,
        }
        action = review_action(current)
        require(action == "fallback-native",
                f"current {route} adapter launched or parked instead of native fallback: {action}")

    capable = {
        "route": "external-codex",
        "fresh_conversation": True,
        "instruction_neutral_startup": True,
        "candidate_read_only": True,
        "artifacts_only_writable": True,
    }
    require(review_action(capable) == "launch-external",
            "proved external capability did not become launchable")
    require(review_action(capable, external_failed=True) == "retry-external",
            "capable external first failure lost its retry")
    require(review_action(capable, external_retry_spent=True, external_failed=True) == "fallback-native",
            "capable external retry failure did not fall back")
    native = {"route": "native", "fresh_conversation": True,
              "instruction_neutral_startup": False, "candidate_read_only": False,
              "artifacts_only_writable": False}
    require(review_action(native) == "launch-native",
            "native limitations incorrectly parked an available pass")
    require(review_action(native, native_exhausted=True) == "park-machine-blocker",
            "exhausted invalid native route did not park")


def main() -> int:
    check_document_contract()
    run_hostile_fixtures()
    run_repository_context_fixtures()
    run_isolation_transition_fixtures()
    print("transport contract tests: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
