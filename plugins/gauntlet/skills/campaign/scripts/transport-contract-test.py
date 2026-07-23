#!/usr/bin/env python3
"""Mechanical fixtures for campaign's typed runtime transport contract."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from _gauntlet.modules import load_module_from_path


ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"
COPILOT = ROOT.parent / "copilot-address-reviews"
DISPATCH_PATH = ROOT / "scripts" / "review-dispatch.py"


def _load_dispatch():
    mod = load_module_from_path("transport_contract_review_dispatch", DISPATCH_PATH)
    if mod is None:
        raise RuntimeError(f"cannot load review dispatch materializer at {DISPATCH_PATH}")
    return mod


DISPATCH = _load_dispatch()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read(name: str) -> str:
    return (REFS / name).read_text(encoding="utf-8")


TRIAGE_OWNER = '`stage-2-review-gate.md`, "2a-triage"'
TRIAGE_INPUT_BINDINGS = (
    ("--worktree", "<worktree>"),
    ("--base", "origin/<base>"),
    ("--head-sha", "<head_sha>"),
    ("--file", "<state.jsonl>"),
    ("--pr", "<pr>"),
)


def markdown_section(body: str, heading: str) -> str:
    require(heading.startswith("#") and heading.lstrip("#").startswith(" "),
            f"invalid markdown heading fixture: {heading!r}")
    starts = [match.start() for match in re.finditer(
        rf"(?m)^{re.escape(heading)}\s*$", body
    )]
    require(len(starts) == 1, f"expected exactly one {heading!r} section")
    start = starts[0]
    level = len(heading) - len(heading.lstrip("#"))
    next_heading = re.search(rf"(?m)^#{{1,{level}}} ", body[start + len(heading):])
    end = len(body) if next_heading is None else start + len(heading) + next_heading.start()
    return body[start:end]


def heartbeat_triage_region(body: str) -> str:
    regions = re.findall(
        r"(?ms)^   - any newly-adopted PR whose ledger row lacks a `tier`.*?"
        r"(?=^   - current tip has )",
        body,
    )
    require(len(regions) == 1,
            "loop-control.md must contain exactly one heartbeat triage region")
    return regions[0]


def command_argvs(block: str) -> list[list[str]]:
    logical_lines = re.sub(r"\\\r?\n", " ", block).splitlines()
    commands: list[list[str]] = []
    for line in logical_lines:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise AssertionError(f"cannot parse documented command line: {line!r}") from exc
        argv: list[str] = []
        for token in tokens:
            if token and not set(token).difference(";&|"):
                if argv:
                    commands.append(argv)
                    argv = []
            else:
                argv.append(token)
        if argv:
            commands.append(argv)
    return commands


def is_triage_derive(argv: list[str]) -> bool:
    return any(
        token.endswith("triage.py") and index + 1 < len(argv) and argv[index + 1] == "derive"
        for index, token in enumerate(argv)
    )


def has_binding(argv: list[str], binding: tuple[str, str]) -> bool:
    return any(tuple(argv[index:index + 2]) == binding for index in range(len(argv) - 1))


def has_exact_flag(body: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", body) is not None


def triage_prose_chunks(body: str) -> list[str]:
    return [
        chunk for chunk in re.split(r"\n[ \t>]*\n", body)
        if re.search(r"triage\.py\s+derive", " ".join(chunk.split()))
    ]


def check_consumer_triage_region(name: str, region: str) -> None:
    require(TRIAGE_OWNER in region, f"{name} lost its pointer to the campaign triage owner")
    chunks = [region, *triage_prose_chunks(region)]
    for chunk in chunks:
        normalized = " ".join(chunk.split())
        reconstructs_initial = (
            re.search(r"triage\.py\s+derive", normalized) is not None
            and any(has_exact_flag(normalized, flag) for flag, _ in TRIAGE_INPUT_BINDINGS)
        )
        require(not reconstructs_initial,
                f"{name} reconstructed the campaign triage invocation instead of using its owner")
        for replay in re.finditer(
            r"(?i)(?:\bagain\b|\bonce more\b|\brepeat(?:s|ed)?\b|"
            r"\bre-?run\b|\bsecond derive\b|\bveto\b)",
            normalized,
        ):
            require("--tier" not in normalized[replay.end():replay.end() + 240],
                    f"{name} reconstructed the campaign triage veto re-run instead of using its owner")


def check_campaign_triage_contract(stage: str, adoption: str, loop_control: str) -> None:
    # Stage 2 owns the one runnable campaign triage command. Parse the command itself so comments and
    # sibling commands in its fence cannot supply bindings that the triage process would never receive.
    stage_code_blocks = re.findall(r"```[^\n]*\n(.*?)```", stage, flags=re.DOTALL)
    stage_triage_commands = [
        argv for block in stage_code_blocks if "triage.py derive" in block
        for argv in command_argvs(block)
        if is_triage_derive(argv)
    ]
    require(len(stage_triage_commands) == 1,
            "stage-2-review-gate.md must own exactly one runnable campaign triage invocation")
    for binding in (("--file", "<state.jsonl>"), ("--pr", "<pr>")):
        require(has_binding(stage_triage_commands[0], binding),
                f"stage-2-review-gate.md campaign triage invocation lost {' '.join(binding)}")

    consumer_regions = (
        ("pr-adoption.md", adoption, markdown_section(adoption, "#### Adoption-time tier decision")),
        ("loop-control.md", loop_control, heartbeat_triage_region(loop_control)),
    )
    for name, body, region in consumer_regions:
        check_consumer_triage_region(name, region)
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", body, flags=re.DOTALL)
        require(not any(
            is_triage_derive(argv)
            for block in code_blocks if "triage.py derive" in block
            for argv in command_argvs(block)
        ), f"{name} restored a runnable campaign triage command outside its owner")
        # Catch a caller copy placed just outside the named region. A prose reconstruction starts when
        # the tool identity appears with any owner-owned process binding, including on separate lines.
        for chunk in triage_prose_chunks(body):
            normalized = " ".join(chunk.split())
            require(not any(has_exact_flag(normalized, flag)
                            for flag, _ in TRIAGE_INPUT_BINDINGS),
                    f"{name} reconstructed the campaign triage invocation instead of using its owner")


def require_rejected(callback, expected: str, message: str) -> None:
    try:
        callback()
    except AssertionError as exc:
        require(expected in str(exc),
                f"{message}: rejected for the wrong reason: {exc}")
        return
    raise AssertionError(message)


def insert_after_once(body: str, marker: str, insertion: str) -> str:
    require(body.count(marker) == 1, f"fixture insertion marker drifted: {marker!r}")
    return body.replace(marker, marker + insertion, 1)


def run_triage_contract_fixtures() -> None:
    stage = read("stage-2-review-gate.md")
    adoption = read("pr-adoption.md")
    loop_control = read("loop-control.md")

    # The live pointer-only prose is the positive fixture.
    check_campaign_triage_contract(stage, adoption, loop_control)

    insertion_marker = "classification policy; do not reconstruct them here."
    split_caller = insert_after_once(
        adoption,
        insertion_marker,
        """

INVENTED negative fixture: run `triage.py derive` with these caller inputs:
- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
- `--file <state.jsonl>`
- `--pr <pr>`
""",
    )
    require_rejected(
        lambda: check_campaign_triage_contract(stage, split_caller, loop_control),
        "reconstructed the campaign triage invocation",
        "split-line adoption triage caller was accepted",
    )

    three_flag_adoption = insert_after_once(
        adoption,
        insertion_marker,
        """

INVENTED negative fixture: run `triage.py derive` with these caller inputs:
- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
""",
    )
    require_rejected(
        lambda: check_campaign_triage_contract(stage, three_flag_adoption, loop_control),
        "reconstructed the campaign triage invocation",
        "three-flag adoption triage caller was accepted",
    )

    heartbeat_insertion_marker = "reconstruct them here."
    three_flag_heartbeat = insert_after_once(
        loop_control,
        heartbeat_insertion_marker,
        """

INVENTED negative fixture: run `triage.py derive` with these caller inputs:
- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
""",
    )
    require_rejected(
        lambda: check_campaign_triage_contract(stage, adoption, three_flag_heartbeat),
        "reconstructed the campaign triage invocation",
        "three-flag heartbeat triage caller was accepted",
    )

    paraphrased_veto = insert_after_once(
        adoption,
        insertion_marker,
        """

INVENTED negative fixture: after choosing the tier, execute the same derive once more.
Add `--tier <decided>` to that invocation.
""",
    )
    require_rejected(
        lambda: check_campaign_triage_contract(stage, paraphrased_veto, loop_control),
        "reconstructed the campaign triage veto re-run",
        "paraphrased adoption triage veto was accepted",
    )

    owner_command = (
        "    --file <state.jsonl> --pr <pr> [--tier <your decided tier>]\n"
    )
    decoy_command = (
        "    [--tier <your decided tier>] ; invented-other-tool --pr <pr>\n"
        "# INVENTED negative fixture decoy: --file <state.jsonl>\n"
    )
    require(stage.count(owner_command) == 1, "triage owner command fixture drifted")
    unbound_owner = stage.replace(owner_command, decoy_command, 1)
    require_rejected(
        lambda: check_campaign_triage_contract(unbound_owner, adoption, loop_control),
        "campaign triage invocation lost --file <state.jsonl>",
        "triage owner accepted bindings supplied only by fence decoys",
    )


def check_document_contract() -> None:
    runtime = read("runtime-adapter.md")
    stage = read("stage-2-review-gate.md")
    dispatch = read("review-dispatch.md")
    prompt = (ROOT / "scripts" / "review-prompt.txt").read_text(encoding="utf-8")
    reviewer = read("reviewer.md")
    cross = read("cross-agent-reviewers.md")
    adoption = read("pr-adoption.md")
    run_identity = read("run-identity-and-lease.md")
    merge = read("stage-3-merge.md")
    merge_runner = (ROOT / "scripts" / "merge.py").read_text(encoding="utf-8")
    root_cause = read("root-cause-pass.md")
    files_ledger = read("files-and-ledger.md")
    loop_control = read("loop-control.md")
    copilot = (COPILOT / "SKILL.md").read_text(encoding="utf-8")

    check_campaign_triage_contract(stage, adoption, loop_control)

    # The canonical prs.json producer is now one executable owner. Only files-and-ledger.md spells the
    # typed invocation; adoption and heartbeat prose point to it and never reconstruct the internal gh
    # argv. The output remains a typed Path argument and never enters shell source or stdout redirection.
    prs_fetch_argv = " ".join(" ".join((
        'argv: ["python3", path_join(skill_dir, "scripts", "reconcile.py"), "fetch",',
        '"--project-root", repository.project_root,',
        '"--run-id", run_id,',
        '"--output", path_join(<rundir>, "prs.json")],',
    )).split())
    require(prs_fetch_argv in " ".join(files_ledger.split()),
            "files-and-ledger.md lost the typed reconcile.py fetch invocation")
    require("stdout_file: null" in files_ledger,
            "files-and-ledger.md routed fetch output through a second writer")
    for name, body in (("pr-adoption.md", adoption), ("loop-control.md", loop_control)):
        require("The canonical `prs.json` command" in body and "reconcile.py fetch" in body,
                f"{name} lost its pointer to the executable snapshot owner")
        require('argv: ["gh", "pr", "list"' not in body,
                f"{name} reconstructed the internal gh query instead of using reconcile.py fetch")
    for name, body in (("files-and-ledger.md", files_ledger),
                       ("pr-adoption.md", adoption),
                       ("loop-control.md", loop_control)):
        require("> <rundir>/prs.json" not in body,
                f"{name} restored the prs.json shell redirection")

    # The per-PR `gh pr view` adoption snapshot is the same class: typed run_argv, its output path a
    # Path in stdout_file via path_join, never `> <rundir>/pr-<pr>.json`.
    # `body` is deliberately ABSENT — a fork PR's body is attacker-controlled and this pre-refusal read
    # never needs it (pr-adoption.md, step 1; scripts/pr-adopt.py). The intent read (step 3a) fetches it
    # separately, for a same-repo PR only.
    pr_view_argv = " ".join((
        'argv: ["gh", "pr", "view", pr, "--json", '
        '"number,title,headRefName,headRefOid,baseRefName,labels,state,'
        'isCrossRepository,headRepositoryOwner,headRepository"],'
    ).split())
    require(pr_view_argv in " ".join(adoption.split()),
            "pr-adoption.md lost the typed `gh pr view` adoption-snapshot argv")
    require('stdout_file: path_join(<rundir>, concat("pr-", pr, ".json"))' in adoption,
            "pr-adoption.md lost the typed pr-<pr>.json stdout_file Path")
    require("> <rundir>/pr-<pr>.json" not in adoption,
            "pr-adoption.md restored the pr-<pr>.json shell redirection")

    # CLASS INVARIANT: no live reference command block routes a dynamic path through a shell redirection.
    # Every driver-run command spec uses the typed run_argv stdout_file Path instead. (The stage-2-ci.md
    # snapshot block redirects to a `$tmp` shell var and `mv`s it — it documents ci-status.py's internal
    # promote algorithm, not a driver-run command, and carries no `> <rundir>/` / `> $PROJECT/` form.)
    for reference in sorted(REFS.glob("*.md")):
        body = reference.read_text(encoding="utf-8")
        for redirect in ("> <rundir>/", "> $" + "PROJECT/"):
            require(redirect not in body,
                    f"{reference.name} restored a dynamic-path shell redirection: {redirect!r}")

    for needle in (
        "## Typed repository context and data/process boundary",
        "resolve_repository_context(checkout: Path) -> RepositoryContext",
        "create_run_directory(repository: RepositoryContext) -> Path",
        "ProcessResult.stdout",  # create_run_directory captures run-id.py's stdout from the RESULT (stdout_file null), not a mis-slotted arg
        "default_worktree(repository: RepositoryContext, head_ref_name: Text) -> Path",
        "run_argv(argv: list[Text]",
        "review-dispatch.py prepare",
        "<TRANSPORT-RECORD>",
        '"native-worker-write" | "external-process-capture"',
        "ReviewIsolationCapability",
        "external_retry_spent: Bool",
        'event: "selected" | "external-system-failure" | "native-system-failure"',
        "current Claude Code and Codex adapters",
        "launch_mechanism_present",
        "Their absence NEVER blocks launch",
        "selected cross-engine route, paired CLI available | `launch-external`",
        "Missing native OS/startup controls alone never select",
        "### Review preparation mapping",
        "| `launch-external` / `retry-external` | selected capability's external route | "
        "`external-process-capture` |",
        "| `launch-native` / `fallback-native` | `native` | `native-worker-write` |",
        "attempt `2` fails → prepare fresh native fallback attempt `3`",
        "dead or unusable attempt `3` → `park-machine-blocker`",
    ):
        require(needle in runtime, f"runtime adapter lost typed owner: {needle}")

    for needle in (
        '["python3", review_dispatch_script, "prepare"',
        "prepared = JSON_DECODE(result.stdout)",
        "scripts/review-prompt.txt",
        "using the returned `transport` without reconstructing",
        "Every transport text value must encode as UTF-8",
        "Recover any inert residue of a preparation that never launched a reviewer",
    ):
        require(needle in dispatch, f"review-dispatch.md lost preparation handoff: {needle}")

    for needle in (
        "TRANSPORT is this JSON-decoded ReviewTransport record:",
        'TRANSPORT.report.producer is "native-worker-write"',
        '"external-process-capture", return the report only',
        'RUN_ARGV(["git", "-C", TRANSPORT.worktree, "diff"',
        'RUN_ARGV(["python3", TRANSPORT.emit_progress_path',
        'RUN_ARGV(["python3", TRANSPORT.emit_finding_path',
        'RUN_ARGV(["python3", TRANSPORT.emit_amendment_path',
    ):
        require(needle in prompt, f"review-prompt.txt lost reviewer operation: {needle}")

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
    require("create_run_directory(repository)" in run_identity,
            "fresh-run creation bypasses the repository context owner")
    require("root = resolve_project_root(project_root)" in merge_runner and
            '["git", "-C", str(root), "fetch"' in merge_runner and
            "shell=True" not in merge_runner,
            "merge runner bypasses the typed repository context/argv boundary")
    require("git -C $" not in merge and "cwd: project_root" not in stage
            and "cwd: project_root" not in dispatch,
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
    require("Launch only the route named by `prepared.route`" in dispatch,
            "Review dispatch can launch outside the prepared transition")
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
        "review-dispatch.md": (
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

        review_root = root / hostile[0]
        worktree = root / hostile[1]
        paths = DISPATCH.attempt_paths(review_root, "58", "5", "2")
        record = DISPATCH.build_transport(
            rundir=review_root,
            worktree=worktree,
            base="base$(printf${IFS}BAD)",
            pr="58",
            review_pass="5",
            launch_attempt="2",
            producer="native-worker-write",
            paths=paths,
        )
        encoded_record = json.dumps(record, ensure_ascii=False)
        require(json.loads(encoded_record) == record,
                "JSON transport record changed bytes/fields")

        template = b"before <TRANSPORT-RECORD> middle <INTENT> after"
        intent = b"literal <TRANSPORT-RECORD> and <INTENT> must not be rebound"
        bound = DISPATCH.bind_prompt(template, record, intent)
        require(bound.endswith(intent + b" after"), "prompt binding rescanned inserted intent bytes")
        require(paths["prompt"].name == "review-58-5.a2.prompt.txt" and
                paths["progress"].name == "review-58-5.a2.progress.jsonl" and
                paths["findings"].name == "review-58-5.a2.findings.jsonl" and
                paths["report"].name == "review-58-5.a2.txt",
                "the executable materializer mixed launch attempts")

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
    require(len(raw_root) > 0, "repository resolver accepted an empty root")
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

        # The canonical prs.json snapshot path is passed to reconcile.py fetch as a typed Path — never a
        # shell redirection. With the repository root carrying a space and a newline, path_join keeps the
        # snapshot ONE intact Path under <rundir>; it is never shell-split and never triggers a bash
        # "ambiguous redirect".
        prs_json_path = rundir / "prs.json"
        require(prs_json_path.parent == rundir and prs_json_path.name == "prs.json",
                "prs.json path_join did not stay under the run directory")
        require(" " in os.fspath(prs_json_path) and "\n" in os.fspath(prs_json_path),
                "prs.json fixture lost the hostile whitespace it exists to pin")
        require(prs_json_path.is_absolute() and
                (prs_json_path == repository_root or repository_root in prs_json_path.parents),
                f"prs.json snapshot path escaped the repository: {prs_json_path!s}")
        # As one argv element into a shell-only adapter, the space/newline-bearing path stays one token —
        # exactly one Path, never split by the shell.
        prs_json_probe = [sys.executable, "-c",
                          "import json,sys; print(json.dumps(sys.argv[1:]))", os.fspath(prs_json_path)]
        prs_json_done = subprocess.run(["sh", "-c", shlex.join(prs_json_probe)],
                                       text=True, capture_output=True, check=True)
        require(json.loads(prs_json_done.stdout) == [os.fspath(prs_json_path)],
                "prs.json fetch output path was shell-split by the mechanical encoder")

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

        # Both fetch sites qualify a hostile, dash-leading base into a `refs/heads/...` refspec so git can
        # never option-parse it (adoption: tracking ref; merge base-sync: local ref, no leading `+`).
        base = "--base with spaces\nand-newline"
        refresh_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
        adoption_fetch = ["git", "fetch", "origin", refresh_ref]
        merge_direct_ref = f"refs/heads/{base}:refs/heads/{base}"
        merge_direct_fetch = ["git", "fetch", "origin", merge_direct_ref]
        map_a_git = {
            "A05 copilot process cwd": (copilot_argv, repository["project_root"]),
            "A15 adoption/pre-review Git cwd": (adoption_fetch, repository["project_root"]),
            "A20 merge Git cwd": (merge_direct_fetch, repository["project_root"]),
        }
        for cell, (argv, cwd) in map_a_git.items():
            require(len(argv) >= 4 and cwd == repository_root and cwd.is_absolute(),
                    f"{cell} shifted argv or lost the resolved absolute cwd")
        require(adoption_fetch == ["git", "fetch", "origin", refresh_ref] and
                merge_direct_fetch == ["git", "fetch", "origin", merge_direct_ref],
                "repository Git argv shifted a hostile ref")


def review_action(capability: Mapping[str, object], external_retry_spent: bool = False,
                  external_failed: bool = False, native_exhausted: bool = False) -> str:
    # Every route launches on `fresh_conversation` + `launch_mechanism_present` alone. The three
    # `os_filesystem_isolation` properties are an optional stronger-boundary CLAIM and MUST NOT gate
    # launch — the function deliberately never reads them.
    route = str(capability["route"])
    launchable = bool(capability["fresh_conversation"] and capability["launch_mechanism_present"])
    if route.startswith("external-"):
        if not launchable:
            return "fallback-native"
        if external_failed:
            return "fallback-native" if external_retry_spent else "retry-external"
        return "launch-external"
    # Native is the last-resort route: if it cannot launch (unavailable — no fresh conversation or no
    # launch mechanism), there is nothing left to fall back to, which is exactly `park-machine-blocker`.
    if not launchable:
        return "park-machine-blocker"
    if native_exhausted:
        return "park-machine-blocker"
    return "launch-native"


def _os_isolation(*, proven: bool) -> dict[str, bool]:
    return {
        "instruction_neutral_startup": proven,
        "candidate_read_only": proven,
        "artifacts_only_writable": proven,
    }


def run_isolation_transition_fixtures() -> None:
    # Shipped state: the paired CLI is present and the three OS bools are false. The cross-engine route
    # LAUNCHES at native-limitation level — this is the default behavior of the PR.
    for route in ("external-codex", "external-claude"):
        shipped = {
            "route": route,
            "fresh_conversation": True,
            "launch_mechanism_present": True,
            "os_filesystem_isolation": _os_isolation(proven=False),
        }
        require(review_action(shipped) == "launch-external",
                f"shipped {route} did not launch cross-engine at native-limitation level")
        require(review_action(shipped, external_failed=True) == "retry-external",
                f"{route} first failure lost its retry")
        require(review_action(shipped, external_failed=True, external_retry_spent=True) == "fallback-native",
                f"{route} retry failure did not fall back to native")

        # Paired CLI absent -> unavailable -> immediate native fallback, no retry consumed.
        absent = dict(shipped, launch_mechanism_present=False)
        require(review_action(absent) == "fallback-native",
                f"{route} with the paired CLI absent did not take native fallback")

    # Proving the three OS bools NEVER changes launchability; it only adds a stronger-boundary claim.
    proven = {
        "route": "external-codex",
        "fresh_conversation": True,
        "launch_mechanism_present": True,
        "os_filesystem_isolation": _os_isolation(proven=True),
    }
    require(review_action(proven) == "launch-external",
            "an OS-proving adapter changed the launch decision")

    native = {
        "route": "native",
        "fresh_conversation": True,
        "launch_mechanism_present": True,
        "os_filesystem_isolation": _os_isolation(proven=False),
    }
    require(review_action(native) == "launch-native",
            "native limitations incorrectly parked an available pass")
    require(review_action(native, native_exhausted=True) == "park-machine-blocker",
            "exhausted invalid native route did not park")

    # A native route that is `unavailable` (no launch mechanism, or no fresh conversation) CANNOT launch.
    # Native is the last-resort route, so an unavailable one parks the machine blocker — it never launches.
    native_no_mechanism = dict(native, launch_mechanism_present=False)
    require(review_action(native_no_mechanism) == "park-machine-blocker",
            "native route with no launch mechanism was launched instead of parked")
    native_no_fresh = dict(native, fresh_conversation=False)
    require(review_action(native_no_fresh) == "park-machine-blocker",
            "native route without a fresh conversation was launched instead of parked")


def main() -> int:
    check_document_contract()
    run_triage_contract_fixtures()
    run_hostile_fixtures()
    run_repository_context_fixtures()
    run_isolation_transition_fixtures()
    print("transport contract tests: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
