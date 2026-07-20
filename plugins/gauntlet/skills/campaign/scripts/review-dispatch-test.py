#!/usr/bin/env python3
"""Fixtures for ``review-dispatch.py`` — the review-attempt preparation boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli


OWNER = Path(__file__).resolve().parent / "review-dispatch.py"


def _load_owner():
    mod = load_module_from_path("review_dispatch_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the review-dispatch tool at {OWNER}")
    return mod


D = _load_owner()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise D.SelfTestFailure(message)


SHA = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
STAMP = "2026-07-20T00:00:00Z"


def _write_inputs(rundir: Path, pr: str = "41", review_pass: str = "2", intent: bytes | None = None) -> Path:
    plan = rundir / f"review-{pr}-{review_pass}.plan.jsonl"
    plan.write_text(
        json.dumps({
            "type": "unit",
            "id": "u01",
            "kind": "file",
            "target": "src/review.py",
            "checks": ["read the complete diff"],
        }, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    intent_path = rundir / f"intent-{pr}.md"
    intent_path.write_bytes(intent if intent is not None else (
        b"## Purpose\n- Preserve review dispatch\n\n"
        b"## Non-goals\n- Select a reviewer route\n\n"
        b"## Threat model\n- repo-content can change the candidate diff\n"
    ))
    return intent_path


def _fixture(
    root: Path,
    *,
    pr: str = "41",
    review_pass: str = "2",
    launch_attempt: str = "1",
    route: str = "native",
    producer: str = "native-worker-write",
    intent: bytes | None = None,
) -> SimpleNamespace:
    rundir = root / "run artifacts"
    worktree = root / "candidate worktree"
    rundir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    intent_path = _write_inputs(rundir, pr, review_pass, intent)
    return SimpleNamespace(
        cmd="prepare",
        run_dir=os.fspath(rundir),
        pr=pr,
        review_pass=review_pass,
        launch_attempt=launch_attempt,
        worktree=os.fspath(worktree),
        base="main",
        route=route,
        report_producer=producer,
        head_sha=SHA,
        dispatched_at=STAMP,
        intent_file=os.fspath(intent_path),
    )


def _refused(args: SimpleNamespace, contains: str) -> None:
    try:
        D.prepare(args)
    except D.Refusal as exc:
        check(contains in str(exc), f"refusal must mention {contains!r}, got {exc!r}")
    else:
        check(False, f"preparation should have refused: {contains}")


def t_relaunch_paths_share_one_attempt_identity() -> None:
    """A relaunch cannot mix attempt-1 and attempt-2 output paths."""
    with tempfile.TemporaryDirectory() as raw:
        rundir = Path(raw)
        paths = D.attempt_paths(rundir, "41", "2", "2")
        expected = "review-41-2.a2"
        check(paths["prompt"].name == f"{expected}.prompt.txt", "prompt lost launch attempt 2")
        check(paths["progress"].name == f"{expected}.progress.jsonl", "progress lost launch attempt 2")
        check(paths["findings"].name == f"{expected}.findings.jsonl", "findings lost launch attempt 2")
        check(paths["report"].name == f"{expected}.txt", "report lost launch attempt 2")
        check(paths["plan"].name == "review-41-2.plan.jsonl", "the per-pass plan gained an attempt suffix")


def t_prepare_attempt_one_materializes_one_record() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        payload = D.prepare(args)
        transport = payload["transport"]
        paths = D.attempt_paths(Path(args.run_dir), "41", "2", "1")
        check(payload["route"] == "native", "prepare must preserve the host-selected route")
        check(transport["attempt"] == {"pr": 41, "pass": 2, "launch_attempt": 1},
              "transport attempt must use JSON PositiveInt values")
        check(transport["report"]["producer"] == "native-worker-write",
              "native route must carry the native report owner")
        check(Path(transport["prompt_path"]) == paths["prompt"], "transport prompt path drifted")
        check(Path(transport["progress_path"]) == paths["progress"], "transport progress path drifted")
        check(Path(transport["findings_path"]) == paths["findings"], "transport findings path drifted")
        check(Path(transport["report"]["path"]) == paths["report"], "transport report path drifted")
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "prepare must materialize prompt and identity before returning")
        check(not paths["findings"].exists() and not paths["report"].exists(),
              "prepare must not claim reviewer-owned output files")
        events = D.RP.parse_lines(paths["progress"].read_text(encoding="utf-8"), paths["progress"].name)
        ident = D.RP.check_identity(events, "41", "2", "1")
        check(ident["head_sha"] == SHA and ident["dispatched_at"] == STAMP,
              "identity must carry the caller's full SHA and real dispatch clock")
        for field in ("emit_progress_path", "emit_finding_path", "emit_amendment_path"):
            emitter = Path(transport[field])
            check(emitter.is_absolute() and emitter.is_file(), f"{field} must resolve from the installed script")


def t_later_attempt_uses_external_report_owner() -> None:
    for route in ("external-codex", "external-claude"):
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(
                Path(raw), launch_attempt="7", route=route, producer="external-process-capture"
            )
            transport = D.prepare(args)["transport"]
            check(transport["attempt"]["launch_attempt"] == 7, f"{route} lost attempt 7")
            check(".a7." in transport["prompt_path"] and ".a7." in transport["progress_path"] and
                  ".a7." in transport["findings_path"], f"{route} mixed attempt-scoped artifact names")
            check(transport["report"]["path"].endswith("review-41-2.a7.txt"),
                  f"{route} report lost attempt 7")
            check(transport["report"]["producer"] == "external-process-capture",
                  f"{route} must leave report writing to process capture")


def t_route_and_report_owner_must_agree() -> None:
    pairs = (
        ("native", "external-process-capture", "native-worker-write"),
        ("external-codex", "native-worker-write", "external-process-capture"),
        ("external-claude", "native-worker-write", "external-process-capture"),
    )
    for route, producer, required in pairs:
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(Path(raw), route=route, producer=producer)
            _refused(args, f"requires report producer {required!r}")
            paths = D.attempt_paths(Path(args.run_dir), "41", "2", "1")
            check(not paths["prompt"].exists() and not paths["progress"].exists(),
                  "a producer mismatch must create no launch artifacts")


def t_hostile_paths_and_intent_remain_exact_data() -> None:
    with tempfile.TemporaryDirectory(prefix="dispatch ' \" ` $(literal)\n") as raw:
        root = Path(raw)
        marker = root / "MUST_NOT_EXIST"
        intent = (
            "## Purpose\n"
            f"- Preserve $(touch {marker}) `ticks` 'single' \"double\" <TRANSPORT-RECORD> 雪\n\n"
            "## Non-goals\n- <INTENT> is literal payload\n\n"
            "## Threat model\n- repo-content can start with --leading-option\n"
        ).encode("utf-8")
        args = _fixture(root, intent=intent)
        args.base = "--base$(literal)`tick`'quote\""
        payload = D.prepare(args)
        transport = payload["transport"]
        prompt = Path(transport["prompt_path"]).read_bytes()
        template = D.TEMPLATE.read_bytes()
        expected = D.bind_prompt(template, transport, intent)
        check(prompt == expected, "hostile intent/path bytes must be bound exactly once")
        check(intent in prompt, "the complete intent bytes must remain one verbatim prompt slice")
        check(transport["base"] == args.base, "hostile base text was normalized or shell-decoded")
        check("\n" in transport["review_root"], "hostile newline path fixture was lost")
        check(not marker.exists(), "prompt preparation executed payload syntax")


def t_template_slots_are_closed_before_payload_binding() -> None:
    transport = {"payload": "literal <INTENT>"}
    intent = b"literal <TRANSPORT-RECORD> stays payload"
    template = b"record=<TRANSPORT-RECORD>\nintent=<INTENT>\n"
    bound = D.bind_prompt(template, transport, intent)
    check(bound.endswith(intent + b"\n"), "binding rescanned a slot-like string inside intent")
    try:
        D.bind_prompt(template + b"bad=<UNRESOLVED-SLOT>\n", transport, intent)
    except D.Refusal as exc:
        check("exactly <TRANSPORT-RECORD> then <INTENT>" in str(exc),
              "unresolved-slot refusal must name the closed template contract")
    else:
        check(False, "an unresolved template slot must be refused")


def t_invalid_identifiers_create_nothing() -> None:
    cases = (
        ("pr", "041"),
        ("review_pass", "0"),
        ("launch_attempt", "02"),
        ("head_sha", "a3f29c1"),
        ("dispatched_at", "2026-99-99T00:00:00Z"),
    )
    for field, value in cases:
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(Path(raw))
            setattr(args, field, value)
            _refused(args, "review-dispatch" if field != "dispatched_at" else "real UTC")
            rundir = Path(args.run_dir)
            check(not list(rundir.glob("*.prompt.txt")) and not list(rundir.glob("*.progress.jsonl")),
                  f"invalid {field} must create no launch artifacts")


def t_invalid_utf8_filesystem_path_is_controlled_refusal() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as raw:
        bad_bytes = os.fsencode(raw) + b"/non-utf8-\xff"
        os.mkdir(bad_bytes)
        args = _fixture(Path(os.fsdecode(bad_bytes)))
        _refused(args, "UTF-8")
        rundir = Path(args.run_dir)
        check(not list(rundir.glob("*.prompt.txt")) and not list(rundir.glob("*.progress.jsonl")),
              "a non-UTF-8 transport path must create no launch artifacts")


def t_missing_or_wrong_intent_and_bad_plan_create_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        Path(args.intent_file).unlink()
        _refused(args, "intent")
        check(not list(Path(args.run_dir).glob("*.prompt.txt")), "missing intent created a prompt")
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        other = Path(args.run_dir) / "intent-42.md"
        other.write_bytes(Path(args.intent_file).read_bytes())
        args.intent_file = os.fspath(other)
        _refused(args, "derived artifact")
        check(not list(Path(args.run_dir).glob("*.progress.jsonl")), "wrong-PR intent created identity")
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        (Path(args.run_dir) / "review-41-2.plan.jsonl").write_text("\n", encoding="utf-8")
        _refused(args, "blank")
        check(not list(Path(args.run_dir).glob("*.prompt.txt")), "malformed plan created a prompt")


def t_overlapping_run_dir_and_worktree_create_nothing() -> None:
    """An identical or either-way-nested run-dir/worktree pair refuses and materializes nothing."""

    def _no_artifacts(rundir: Path) -> None:
        check(
            not list(rundir.glob("*.prompt.txt")) and not list(rundir.glob("*.progress.jsonl")),
            "an overlapping run-dir/worktree pair created a launch artifact",
        )

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        args.worktree = args.run_dir
        _refused(args, "different directories")
        _no_artifacts(Path(args.run_dir))

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        rundir = Path(args.run_dir)
        nested = rundir / "nested-worktree"
        nested.mkdir()
        args.worktree = os.fspath(nested)
        _refused(args, "nested inside --run-dir")
        _no_artifacts(rundir)

    with tempfile.TemporaryDirectory() as raw:
        worktree = Path(raw) / "candidate worktree"
        worktree.mkdir()
        rundir = worktree / "nested-run"
        rundir.mkdir()
        intent_path = _write_inputs(rundir)
        args = SimpleNamespace(
            cmd="prepare",
            run_dir=os.fspath(rundir),
            pr="41",
            review_pass="2",
            launch_attempt="1",
            worktree=os.fspath(worktree),
            base="main",
            route="native",
            report_producer="native-worker-write",
            head_sha=SHA,
            dispatched_at=STAMP,
            intent_file=os.fspath(intent_path),
        )
        _refused(args, "nested inside --worktree")
        _no_artifacts(rundir)


def t_every_existing_attempt_artifact_refuses_without_overwrite() -> None:
    for name in ("prompt", "progress", "findings", "report"):
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(Path(raw))
            path = D.attempt_paths(Path(args.run_dir), "41", "2", "1")[name]
            original = b"existing attempt evidence\n"
            path.write_bytes(original)
            _refused(args, "must all be fresh")
            check(path.read_bytes() == original, f"existing {name} was overwritten")
            others = D.attempt_paths(Path(args.run_dir), "41", "2", "1")
            for other_name in ("prompt", "progress"):
                if other_name != name:
                    check(not others[other_name].exists(),
                          f"conflict at {name} still created {other_name}")


def t_second_install_failure_rolls_back_first_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        prompt = root / "review-41-2.prompt.txt"
        progress = root / "review-41-2.progress.jsonl"
        calls = 0

        def fail_second(source, target) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second-link failure")
            os.link(source, target)

        raised = False
        try:
            D.install_pair(prompt, b"prompt", progress, b"identity", link=fail_second)
        except OSError as exc:
            raised = "injected" in str(exc)
        check(raised, "the injected second-link failure must reach the caller")
        check(not prompt.exists() and not progress.exists(),
              "a controlled second-file failure must roll back the first file")
        check(not list(root.glob(".review-dispatch-*.tmp")), "atomic rollback left staged temp files")

        real_stage = D._stage_bytes
        stage_calls = 0

        def fail_second_stage(path: Path, content: bytes) -> Path:
            nonlocal stage_calls
            stage_calls += 1
            if stage_calls == 2:
                raise OSError("injected second-stage failure")
            return real_stage(path, content)

        D._stage_bytes = fail_second_stage
        try:
            D.install_pair(prompt, b"prompt", progress, b"identity")
        except OSError as exc:
            check("second-stage" in str(exc), "the injected staging failure must reach the caller")
        else:
            check(False, "second staging failure must refuse preparation")
        finally:
            D._stage_bytes = real_stage
        check(not prompt.exists() and not progress.exists(), "staging failure created a target file")
        check(not list(root.glob(".review-dispatch-*.tmp")), "staging failure left a temp file")


def t_prompt_only_crash_state_is_recoverable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        child = r'''\
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

owner = Path(sys.argv[1])
sys.path.insert(0, os.fspath(owner.parent))
spec = importlib.util.spec_from_file_location("crashing_review_dispatch", owner)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_install = module.install_pair

def crash_install(prompt_path, prompt, progress_path, identity):
    def crash_after_first_link(source, target):
        os.link(source, target)
        os._exit(91)
    real_install(prompt_path, prompt, progress_path, identity, link=crash_after_first_link)

module.install_pair = crash_install
module.prepare(SimpleNamespace(**json.loads(sys.argv[2])))
'''
        crashed = subprocess.run(
            [sys.executable, "-c", child, os.fspath(OWNER), json.dumps(vars(args))],
            capture_output=True,
            text=True,
            check=False,
        )
        check(crashed.returncode == 91, f"crash fixture exited {crashed.returncode}, not 91")
        check(paths["prompt"].is_file() and not paths["progress"].exists() and
              not paths["findings"].exists() and not paths["report"].exists(),
              "crash fixture did not leave the exact inert prompt-only state")

        payload = D.prepare(args)
        check(Path(payload["transport"]["prompt_path"]) == paths["prompt"],
              "same-attempt recovery changed the prompt path")
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "same-attempt recovery did not recreate the complete pair")


def t_interrupt_after_identity_link_strands_no_residue() -> None:
    """A SIGINT delivered after the identity link's syscall returns must strand neither file.

    The identity is linked last, so the window is between that ``os.link`` returning and its bookkeeping.
    Because the destination is registered for rollback BEFORE the link, the interrupt rolls back both
    files instead of leaving an identity-only strand — and the attempt number is not wedged, so a
    same-attempt prepare rebuilds the pair.
    """
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        calls = 0

        def sigint_after_second(source, target) -> None:
            nonlocal calls
            calls += 1
            os.link(source, target)
            if calls == 2:
                raise KeyboardInterrupt("sigint right after the identity link syscall")

        raised = False
        try:
            D.install_pair(paths["prompt"], b"prompt", paths["progress"], b"identity", link=sigint_after_second)
        except KeyboardInterrupt:
            raised = True
        check(raised, "the post-identity-link interrupt must reach the caller")
        check(not paths["prompt"].exists() and not paths["progress"].exists(),
              "an interrupt after the identity link stranded a file instead of rolling both back")
        check(not list(Path(args.run_dir).glob(".review-dispatch-*.tmp")),
              "the interrupted install left staged temp files")

        payload = D.prepare(args)
        check(Path(payload["transport"]["progress_path"]) == paths["progress"],
              "same-attempt prepare changed the progress path after a rolled-back interrupt")
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "same-attempt prepare did not rebuild the pair after a rolled-back interrupt")


def t_hard_stop_residue_is_recoverable() -> None:
    """Every abrupt-stop residue that never launched a reviewer is reclaimed by the next prepare.

    A machine stop (no rollback runs) can leave both files present, or an identity line alone, with no
    reviewer output. Both are inert — the reviewer starts only after prepare returns — so a same-attempt
    prepare must reclaim them and rebuild the pair, not refuse the wedged attempt number.
    """
    both_present_child = r'''\
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

owner = Path(sys.argv[1])
sys.path.insert(0, os.fspath(owner.parent))
spec = importlib.util.spec_from_file_location("crashing_review_dispatch", owner)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_install = module.install_pair

def crash_install(prompt_path, prompt, progress_path, identity):
    state = {"n": 0}
    def crash_after_second_link(source, target):
        state["n"] += 1
        os.link(source, target)
        if state["n"] == 2:
            os._exit(92)
    real_install(prompt_path, prompt, progress_path, identity, link=crash_after_second_link)

module.install_pair = crash_install
module.prepare(SimpleNamespace(**json.loads(sys.argv[2])))
'''
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        crashed = subprocess.run(
            [sys.executable, "-c", both_present_child, os.fspath(OWNER), json.dumps(vars(args))],
            capture_output=True,
            text=True,
            check=False,
        )
        check(crashed.returncode == 92, f"both-present crash fixture exited {crashed.returncode}, not 92")
        check(paths["prompt"].is_file() and paths["progress"].is_file() and
              not paths["findings"].exists() and not paths["report"].exists(),
              "the hard stop did not leave both files present with no reviewer output")
        D.prepare(args)
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "same-attempt prepare did not recover the both-files hard-stop residue")
        events = D.RP.parse_lines(paths["progress"].read_text(encoding="utf-8"), paths["progress"].name)
        D.RP.check_identity(events, "41", "2", "1")

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        # An identity-only strand: the lone pass_identity line with no prompt and no reviewer output. Its
        # dispatched_at is deliberately stale (an earlier interrupted run wrote it), proving recovery keys
        # on the attempt identity, not on a byte match against the current invocation.
        paths["progress"].write_text(
            json.dumps({
                "type": "pass_identity", "pr": "41", "pass": "2", "head_sha": SHA,
                "launch_attempt": "1", "dispatched_at": "2026-07-19T00:00:00Z",
            }, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        D.prepare(args)
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "same-attempt prepare did not recover a stale identity-only strand")


def t_external_attempt_two_has_native_attempt_three_recovery() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(
            Path(raw), launch_attempt="2", route="external-codex",
            producer="external-process-capture",
        )
        D.prepare(args)
        args.launch_attempt = "3"
        args.route = "native"
        args.report_producer = "native-worker-write"
        transport = D.prepare(args)["transport"]
        check(transport["attempt"]["launch_attempt"] == 3 and
              transport["report"]["path"].endswith("review-41-2.a3.txt"),
              "native fallback did not receive fresh attempt-3 artifacts")

    refs = OWNER.parent.parent / "references"
    runtime = (refs / "runtime-adapter.md").read_text(encoding="utf-8")
    stage = (refs / "stage-2-review-gate.md").read_text(encoding="utf-8")
    loop = (refs / "loop-control.md").read_text(encoding="utf-8")
    check("attempt `2` fails → prepare fresh native fallback attempt `3`" in runtime,
          "runtime owner does not allocate attempt 3 after failed attempt 2")
    check("dead or unusable attempt `3` → `park-machine-blocker`" in runtime,
          "runtime owner does not terminate failed native fallback attempt 3")
    stale_attempt_two_terminal = "`2` → " + "fresh-worker fallback"
    for name, text in (("Stage 2", stage), ("killed-session", loop)):
        check("Review preparation mapping" in text,
              f"{name} recovery does not point to the attempt-3 owner")
        check(stale_attempt_two_terminal not in text,
              f"{name} recovery retains the stale attempt-2 terminal rule")


def t_transition_actions_map_directly_to_prepare_inputs() -> None:
    runtime = (OWNER.parent.parent / "references" / "runtime-adapter.md").read_text(encoding="utf-8")
    for row in (
        "| `launch-external` / `retry-external` | selected capability's external route | "
        "`external-process-capture` |",
        "| `launch-native` / `fallback-native` | `native` | `native-worker-write` |",
        "| `park-machine-blocker` | no preparation | no preparation |",
    ):
        check(row in runtime, f"review_transition mapping row is missing: {row}")


def t_unicode_worktree_delivers_under_ascii_stdout() -> None:
    """A Unicode worktree path is delivered as UTF-8 bytes even with an ASCII-configured stdout.

    The OUTPUT side must be symmetric with the already-guarded input side: text ``print`` would raise
    UnicodeEncodeError on ``PYTHONIOENCODING=ascii`` after both launch artifacts are installed. The byte
    delivery must instead exit 0 with a decodable UTF-8 JSON record carrying the raw Unicode path.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        rundir = root / "run artifacts"
        worktree = root / "雪-worktree"
        rundir.mkdir(parents=True)
        worktree.mkdir(parents=True)
        intent_path = _write_inputs(rundir)
        argv = [
            "prepare", "--run-dir", os.fspath(rundir), "--pr", "41", "--pass", "2",
            "--launch-attempt", "1", "--worktree", os.fspath(worktree), "--base", "main",
            "--route", "native", "--report-producer", "native-worker-write",
            "--head-sha", SHA, "--dispatched-at", STAMP, "--intent-file", os.fspath(intent_path),
        ]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "ascii"
        completed = subprocess.run(
            [sys.executable, os.fspath(OWNER), *argv],
            capture_output=True,
            env=env,
            check=False,
        )
        check(completed.returncode == 0,
              f"ascii-stdout Unicode-path prepare exited {completed.returncode}: {completed.stderr!r}")
        check(completed.stdout.endswith(b"\n"), "delivered record lost its newline terminator")
        payload = json.loads(completed.stdout.decode("utf-8"))
        check(payload["transport"]["worktree"] == os.fspath(worktree),
              "delivered transport lost the Unicode worktree path")


def t_cli_emits_only_canonical_host_neutral_json() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), route="external-codex", producer="external-process-capture")
        argv = [
            "prepare", "--run-dir", args.run_dir, "--pr", args.pr, "--pass", args.review_pass,
            "--launch-attempt", args.launch_attempt, "--worktree", args.worktree, "--base", args.base,
            "--route", args.route, "--report-producer", args.report_producer,
            "--head-sha", args.head_sha, "--dispatched-at", args.dispatched_at,
            "--intent-file", args.intent_file,
        ]
        code, out, err = capture_cli(D.main, argv)
        check(code == 0 and err == "", f"prepare CLI failed: code={code}, stderr={err!r}")
        check(out.count("\n") == 1, "prepare CLI must print exactly one JSON record")
        payload = json.loads(out)
        check(payload["route"] == "external-codex", "CLI lost the host-selected route")
        check(set(payload) == {"route", "transport"}, "CLI added host-specific launch behavior")
        check("argv" not in payload and "model" not in payload,
              "materializer must not select or launch a host process")


CASES = [
    (
        "relaunch-path-coherence",
        "all relaunch artifacts derive from one attempt identity",
        t_relaunch_paths_share_one_attempt_identity,
    ),
    ("attempt-one", "prepare materializes one coherent attempt-1 record", t_prepare_attempt_one_materializes_one_record),
    ("later-external-attempt", "later attempts preserve suffix and external ownership", t_later_attempt_uses_external_report_owner),
    ("producer-pairing", "route and sole report producer must agree", t_route_and_report_owner_must_agree),
    ("hostile-data", "hostile paths and intent remain inert exact data", t_hostile_paths_and_intent_remain_exact_data),
    ("closed-template", "template slots close before payload binding", t_template_slots_are_closed_before_payload_binding),
    ("invalid-identifiers", "invalid identity fields create no artifacts", t_invalid_identifiers_create_nothing),
    ("invalid-utf8-path", "non-UTF-8 filesystem bytes produce a controlled refusal", t_invalid_utf8_filesystem_path_is_controlled_refusal),
    ("required-inputs", "missing/wrong intent and malformed plan create nothing", t_missing_or_wrong_intent_and_bad_plan_create_nothing),
    ("distinct-run-dir-worktree", "identical or nested run-dir/worktree refuses and writes nothing", t_overlapping_run_dir_and_worktree_create_nothing),
    ("fresh-attempt", "every existing attempt artifact refuses without overwrite", t_every_existing_attempt_artifact_refuses_without_overwrite),
    ("atomic-rollback", "second-file failure rolls back the first file", t_second_install_failure_rolls_back_first_file),
    ("crash-recovery", "the exact inert prompt-only crash state is recoverable", t_prompt_only_crash_state_is_recoverable),
    ("interrupt-rollback", "an interrupt after the identity link rolls both files back", t_interrupt_after_identity_link_strands_no_residue),
    ("hard-stop-recovery", "both-files and identity-only hard-stop residue is recoverable", t_hard_stop_residue_is_recoverable),
    ("fallback-attempt-three", "external retry failure has a terminal native attempt-3 path", t_external_attempt_two_has_native_attempt_three_recovery),
    ("transition-mapping", "review actions map directly to route and producer", t_transition_actions_map_directly_to_prepare_inputs),
    ("unicode-delivery", "a Unicode path is delivered as UTF-8 bytes under ASCII stdout", t_unicode_worktree_delivers_under_ascii_stdout),
    ("host-neutral-json", "CLI emits canonical data and never launches", t_cli_emits_only_canonical_host_neutral_json),
]
