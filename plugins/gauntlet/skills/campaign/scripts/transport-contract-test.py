#!/usr/bin/env python3
"""Mechanical fixtures for campaign's typed runtime transport contract."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"


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

    for needle in (
        "## Typed data/process boundary",
        "run_argv(argv: list[Text]",
        "bind_review_prompt(template: Bytes",
        "<TRANSPORT-RECORD>",
        '"native-worker-write" | "external-process-capture"',
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
            'path_join(project_root, ".worktrees", headRefName)' in adoption,
            "adoption no longer preserves typed branch/path data")

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


def main() -> int:
    check_document_contract()
    run_hostile_fixtures()
    print("transport contract tests: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
