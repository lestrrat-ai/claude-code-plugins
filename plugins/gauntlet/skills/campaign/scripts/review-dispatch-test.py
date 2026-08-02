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

from _gauntlet.modules import load_sibling
from _gauntlet.testing import capture_cli, checker


OWNER = Path(__file__).resolve().parent / "review-dispatch.py"


D = load_sibling("review_dispatch_owner", OWNER.parent, OWNER.name)


check = checker(D.SelfTestFailure)


SHA = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
HISTORICAL_SHA = "b" * 40
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


def _write_landed_pass(rundir: Path, pr: str, review_pass: str, head_sha: str,
                        *, verdict: str = "satisfied", amendment: bool = False) -> None:
    """Write one completed active attempt through the artifact shapes `prepare` must validate later."""
    _write_inputs(rundir, pr, review_pass)
    paths = D.attempt_paths(rundir, pr, review_pass, "1")
    progress = [
        {"type": D.RP.IDENTITY, "pr": pr, "pass": review_pass, "head_sha": head_sha,
         "launch_attempt": "1", "dispatched_at": STAMP, "default_non_goals": []},
        {"type": D.RP.PROGRESS, "unit": "u01", "status": D.RP.STARTED},
        {"type": D.RP.PROGRESS, "unit": "u01", "status": D.RP.DONE,
         "evidence": "Reviewed src/review.py."},
    ]
    if amendment:
        progress.append({
            "type": D.RP.AMENDMENT,
            "ts": STAMP,
            "reason": "Cover generated documentation.",
            "proposed_unit": {
                "type": "unit",
                "id": "u02",
                "kind": "docs",
                "target": "docs/generated.md",
                "checks": ["matches the reviewed behavior"],
            },
        })
    paths["progress"].write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in progress) + "\n",
        encoding="utf-8",
    )
    paths["report"].write_text(
        json.dumps({
            "type": D.RP.REVIEW_REPORT,
            "verdict": verdict,
            "deferred_reason": "-" if verdict != D.RP.DEFERRED else "Review plan needs a decision.",
            "residual_risk": [],
            "summary": "The pass completed.",
        }, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _seed_contiguous_history(rundir: Path, pr: str, review_pass: str) -> None:
    for number in range(1, int(review_pass)):
        _write_landed_pass(rundir, pr, str(number), "bcdef0123456789a"[(number - 1) % 16] * 40)


def _fixture(
    root: Path,
    *,
    pr: str = "41",
    review_pass: str = "2",
    launch_attempt: str = "1",
    allocation_purpose: str = "initial",
    review_action: str = "launch-native",
    route: str = "native",
    producer: str = "reviewer-tool-write",
    prompt_profile: str = "standard",
    intent: bytes | None = None,
    base: str = "main",
    file: str | None = None,
    default_non_goals: str = "[]",
    seed_history: bool = True,
    head_sha: str = SHA,
) -> SimpleNamespace:
    rundir = root / "run artifacts"
    worktree = root / "candidate worktree"
    rundir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    if seed_history:
        _seed_contiguous_history(rundir, pr, review_pass)
    intent_path = _write_inputs(rundir, pr, review_pass, intent)
    return SimpleNamespace(
        cmd="prepare",
        run_dir=os.fspath(rundir),
        pr=pr,
        review_pass=review_pass,
        launch_attempt=launch_attempt,
        allocation_purpose=allocation_purpose,
        review_action=review_action,
        worktree=os.fspath(worktree),
        base=base,
        route=route,
        prompt_profile=prompt_profile,
        report_producer=producer,
        head_sha=head_sha,
        dispatched_at=STAMP,
        default_non_goals=default_non_goals,
        intent_file=os.fspath(intent_path),
        file=file,
    )


def _refused(args: SimpleNamespace, contains: str) -> None:
    try:
        D.prepare(args)
    except D.Refusal as exc:
        check(contains in str(exc), f"refusal must mention {contains!r}, got {exc!r}")
    else:
        check(False, f"preparation should have refused: {contains}")


def _default_launch_artifacts_absent(rundir: Path) -> bool:
    paths = D.attempt_paths(rundir, "41", "2", "1")
    return not paths["prompt"].exists() and not paths["progress"].exists()


def _record_result(args: SimpleNamespace, result: str) -> dict:
    return D.record_result(SimpleNamespace(
        run_dir=args.run_dir,
        pr=args.pr,
        review_pass=args.review_pass,
        launch_attempt=args.launch_attempt,
        result=result,
    ))


def _result_refused(args: SimpleNamespace, result: str, contains: str) -> None:
    journal = D.allocation_path(Path(args.run_dir), args.pr, args.review_pass)
    before = journal.read_bytes()
    try:
        _record_result(args, result)
    except D.Refusal as exc:
        check(contains in str(exc), f"refusal must mention {contains!r}, got {exc!r}")
    else:
        check(False, f"result {result!r} should have refused: {contains}")
    check(journal.read_bytes() == before, "a refused result must not append an allocation outcome")


def _write_report(args: SimpleNamespace, verdict: str) -> None:
    paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
    reason = "-" if verdict != D.RP.DEFERRED else "fixture deferred the review"
    code, _out, err = capture_cli(D.RP.main, [
        "report-write", "--file", os.fspath(paths["report"]), "--verdict", verdict,
        "--deferred-reason", reason, "--summary", "Fixture report.",
    ])
    check(code == 0 and err == "", f"fixture report-write failed: code={code}, stderr={err!r}")


def _complete_usable_binary_review(args: SimpleNamespace) -> None:
    paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
    for argv in (
        ["emit", "--file", os.fspath(paths["progress"]), "--unit", "u01", "--status", "started"],
        ["emit", "--file", os.fspath(paths["progress"]), "--unit", "u01", "--status", "done",
         "--evidence", "review-dispatch-test.py:1"],
    ):
        code, _out, err = capture_cli(D.RP.main, argv)
        check(code == 0 and err == "", f"fixture emit failed: code={code}, stderr={err!r}")
    _write_report(args, D.RP.SATISFIED)


def _prepare_predecessors(args: SimpleNamespace) -> None:
    """Materialize and settle every earlier allocation before testing a later attempt."""
    target_attempt = int(args.launch_attempt)
    target_route = args.route
    target_action = args.review_action
    target_profile = args.prompt_profile
    target_producer = args.report_producer
    for number in range(1, target_attempt):
        args.launch_attempt = str(number)
        args.allocation_purpose = (
            "initial" if number == 1 else "recovery" if number <= D.ORDINARY_ALLOCATION_LIMIT else "final"
        )
        args.route = "native"
        args.review_action = "launch-native"
        args.prompt_profile = "standard"
        args.report_producer = "reviewer-tool-write"
        D.prepare(args)
        _record_result(args, D.PROVIDER_FAILURE)
    args.launch_attempt = str(target_attempt)
    args.allocation_purpose = (
        "initial" if target_attempt == 1 else "recovery"
        if target_attempt <= D.ORDINARY_ALLOCATION_LIMIT else "final"
    )
    args.route = target_route
    args.review_action = target_action
    args.prompt_profile = target_profile
    args.report_producer = target_producer


def t_relaunch_paths_share_one_attempt_identity() -> None:
    """A relaunch cannot mix attempt-1 and attempt-2 output paths."""
    with tempfile.TemporaryDirectory() as raw:
        rundir = Path(raw)
        paths = D.attempt_paths(rundir, "41", "2", "2")
        expected = "review-41-2.a2"
        check(paths["prompt"].name == f"{expected}.prompt.txt", "prompt lost launch attempt 2")
        check(paths["progress"].name == f"{expected}.progress.jsonl", "progress lost launch attempt 2")
        check(paths["findings"].name == f"{expected}.findings.jsonl", "findings lost launch attempt 2")
        check(paths["report"].name == f"{expected}{D.RP.REPORT_SUFFIX}",
              "report lost launch attempt 2 or its artifact suffix")
        check(paths["plan"].name == "review-41-2.plan.jsonl", "the per-pass plan gained an attempt suffix")


def t_missing_historical_pass_refuses_later_dispatch() -> None:
    """The Decad gap had completed pass 1 and pass 3 artifacts, but no pass 2 evidence."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), review_pass="3", seed_history=False)
        rundir = Path(args.run_dir)
        _write_landed_pass(rundir, "41", "1", HISTORICAL_SHA)
        paths = D.attempt_paths(rundir, "41", "3", "1")
        _refused(args, "missing completed pass 2")
        check(not paths["prompt"].exists() and not paths["progress"].exists(),
              "a missing historical pass still prepared later launch artifacts")


def t_contiguous_history_accepts_prior_heads_after_repair() -> None:
    """Each earlier pass validates against its recorded head, not the head being reviewed now."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), review_pass="3")
        rundir = Path(args.run_dir)
        first = D.attempt_paths(rundir, "41", "1", "1")["progress"]
        events = D.RP.parse_lines(first.read_text(encoding="utf-8"), first.name)
        prior_identity = D.RP.check_identity(events, "41", "1", "1")
        check(prior_identity["head_sha"] == HISTORICAL_SHA and prior_identity["head_sha"] != args.head_sha,
              "the fixture must preserve a valid review completed before the repair head")
        payload = D.prepare(args)
        check(payload["transport"]["attempt"]["pass"] == 3,
              "contiguous historical evidence on prior heads must allow the later pass")


def t_contiguous_history_accepts_reauthored_intent() -> None:
    """A REPAIR-INTENT must not invalidate a landed finding anchored to its former purpose."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), seed_history=False)
        rundir = Path(args.run_dir)
        _write_landed_pass(rundir, "41", "1", HISTORICAL_SHA, verdict=D.RP.NOT_SATISFIED)
        findings = D.attempt_paths(rundir, "41", "1", "1")["findings"]
        findings.write_text(json.dumps({
            "type": D.RP.FINDING,
            "file": "src/review.py",
            "line": "1",
            "writer": "network",
            "purpose": "- Preserve review dispatch",
            "base": D.RP.INTRODUCED,
            "base_repro": D.RP.NO_BASE_REPRO,
            "repro": "A network response exposes the stale review state.",
            "fix": "Preserve the review state.",
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        Path(args.intent_file).write_text(
            "## Purpose\n- Re-author the review scope\n\n"
            "## Non-goals\n- Select a reviewer route\n\n"
            "## Threat model\n- repo-content can change the candidate diff\n",
            encoding="utf-8",
        )
        progress = D.attempt_paths(rundir, "41", "1", "1")["progress"]
        live_outcome, live_reason, _ = D.RP.evaluate_detail(progress, HISTORICAL_SHA)
        check(live_outcome == D.RP.UNUSABLE and "NOT a line" in live_reason,
              "the fixture must demonstrate why the live reader cannot validate re-authored history")
        payload = D.prepare(args)
        check(payload["transport"]["attempt"]["pass"] == 2,
              "a valid landed pass must remain usable after REPAIR-INTENT re-authors the current intent")


def t_historical_findings_keep_non_anchor_schema_checks() -> None:
    """History skips only the mutable-purpose anchor, not the finding schema."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), seed_history=False)
        rundir = Path(args.run_dir)
        _write_landed_pass(rundir, "41", "1", HISTORICAL_SHA, verdict=D.RP.NOT_SATISFIED)
        findings = D.attempt_paths(rundir, "41", "1", "1")["findings"]
        findings.write_text(json.dumps({
            "type": D.RP.FINDING,
            "file": "src/review.py",
            "line": "1",
            "writer": "attacker",
            "purpose": "- Preserve review dispatch",
            "base": D.RP.INTRODUCED,
            "base_repro": D.RP.NO_BASE_REPRO,
            "repro": "A network response exposes the stale review state.",
            "fix": "Preserve the review state.",
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        progress = D.attempt_paths(rundir, "41", "1", "1")["progress"]
        outcome, reason, report = D.RP.evaluate_historical_detail(progress, HISTORICAL_SHA)
        check(outcome == D.RP.UNUSABLE and report is None and "CLOSED enum" in reason,
              "historical validation must retain every non-anchor finding check")


def t_historical_unruled_amendment_refuses_later_dispatch() -> None:
    """A binary report cannot prove that the orchestrator ruled a historical amendment."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), seed_history=False)
        rundir = Path(args.run_dir)
        _write_landed_pass(rundir, "41", "1", HISTORICAL_SHA, amendment=True)
        paths = D.attempt_paths(rundir, "41", "2", "1")
        _refused(args, "not yet ruled on")
        check(not paths["prompt"].exists() and not paths["progress"].exists(),
              "unruled historical evidence still prepared later launch artifacts")


def t_nonbinary_historical_verdict_refuses_later_dispatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), seed_history=False)
        _write_landed_pass(Path(args.run_dir), "41", "1", HISTORICAL_SHA, verdict=D.RP.DEFERRED)
        _refused(args, "terminal binary verdict")


def t_prepare_attempt_one_materializes_one_record() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        payload = D.prepare(args)
        transport = payload["transport"]
        paths = D.attempt_paths(Path(args.run_dir), "41", "2", "1")
        check(payload["route"] == "native", "prepare must preserve the host-selected route")
        check(transport["attempt"] == {"pr": 41, "pass": 2, "launch_attempt": 1},
              "transport attempt must use JSON PositiveInt values")
        check(transport["prompt_profile"] == "standard",
              "attempt 1 must carry the standard prompt profile")
        check(transport["report"]["producer"] == "reviewer-tool-write",
              "every route must carry the one report producer: the reviewer's own report door")
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
        for field in ("emit_progress_path", "emit_finding_path", "emit_amendment_path",
                      "emit_report_path"):
            emitter = Path(transport[field])
            check(emitter.is_absolute() and emitter.is_file(), f"{field} must resolve from the installed script")


def t_later_attempt_keeps_the_reviewer_report_door() -> None:
    for route in ("external-codex", "external-claude"):
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(
                Path(raw), launch_attempt="7", review_action="launch-external", route=route,
                producer="reviewer-tool-write"
            )
            _prepare_predecessors(args)
            transport = D.prepare(args)["transport"]
            check(transport["attempt"]["launch_attempt"] == 7, f"{route} lost attempt 7")
            check(".a7." in transport["prompt_path"] and ".a7." in transport["progress_path"] and
                  ".a7." in transport["findings_path"], f"{route} mixed attempt-scoped artifact names")
            check(transport["report"]["path"].endswith("review-41-2.a7" + D.RP.REPORT_SUFFIX),
                  f"{route} report lost attempt 7 or its artifact suffix")
            check(transport["report"]["producer"] == "reviewer-tool-write",
                  f"{route} must leave the report to the reviewer's report door, not process capture")


def t_route_and_report_owner_must_agree() -> None:
    """**THE PRODUCER WAS SWAPPED, NOT ADDED — and that is what this pins.**

    It used to assert that each route required its OWN producer: a native worker wrote the file, an
    external process's final output was captured at the report path, and crossing them was refused. Both
    captures are gone; the reviewer's report door is the sole producer on every route. So the assertion
    turns over: NO route may name a capture producer any more, because a capture alongside the door would
    be a SECOND writer — and the codex capture writes AFTER the run, so it would replace the record rather
    than race it.
    """
    retired = ("native-worker-write", "external-process-capture")
    for route in D.ROUTE_PRODUCERS:
        check(D.ROUTE_PRODUCERS[route] == "reviewer-tool-write",
              f"{route} must map to the one report producer")
        for producer in retired:
            with tempfile.TemporaryDirectory() as raw:
                action = "launch-native" if route == "native" else "launch-external"
                args = _fixture(Path(raw), review_action=action, route=route, producer=producer)
                _refused(args, f"unknown report producer {producer!r}")
                paths = D.attempt_paths(Path(args.run_dir), "41", "2", "1")
                check(not paths["prompt"].exists() and not paths["progress"].exists(),
                      "a retired producer must create no launch artifacts")


def t_prompt_profiles_are_typed_and_action_scoped() -> None:
    """Only the external-Codex retry action receives recovery framing."""
    recovery = D.CODEX_RECOVERY_PREAMBLE
    allowed = (
        ("launch-external", "external-codex", "1", "initial", "standard", "reviewer-tool-write", False),
        ("retry-external", "external-codex", "2", "recovery", "codex-recovery", "reviewer-tool-write", True),
        ("retry-external", "external-claude", "2", "recovery", "standard", "reviewer-tool-write", False),
        ("fallback-native", "native", "3", "recovery", "standard", "reviewer-tool-write", False),
        ("launch-external", "external-codex", "2", "final", "standard", "reviewer-tool-write", False),
    )
    for action, route, launch_attempt, purpose, profile, producer, has_recovery in allowed:
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(
                Path(raw),
                launch_attempt=launch_attempt,
                allocation_purpose=purpose,
                review_action=action,
                route=route,
                producer=producer,
                prompt_profile=profile,
            )
            _prepare_predecessors(args)
            args.allocation_purpose = purpose
            transport = D.prepare(args)["transport"]
            prompt = Path(transport["prompt_path"]).read_bytes()
            check(transport["prompt_profile"] == profile, f"{route} attempt {launch_attempt} lost {profile}")
            check(prompt.startswith(recovery) is has_recovery,
                  f"{route} attempt {launch_attempt} recovery framing={not has_recovery}")
            body = prompt[len(recovery):] if has_recovery else prompt
            check(body.startswith(b"TRANSPORT is this JSON-decoded ReviewTransport record:\n"),
                  f"{route} attempt {launch_attempt} lost the shared review template")
            check(body.count(b"THE QUESTION YOU ARE ANSWERING IS:") == 1,
                  f"{route} attempt {launch_attempt} duplicated or removed the review question")
            check(Path(args.intent_file).read_bytes() in body,
                  f"{route} attempt {launch_attempt} lost the verbatim intent")
            for needle in (
                b"TRANSPORT.emit_progress_path",
                b"TRANSPORT.emit_finding_path",
                b"TRANSPORT.emit_amendment_path",
                b"TRANSPORT.emit_report_path",
                b'"--verdict", verdict',
                b'"--deferred-reason", deferred_reason',
            ):
                check(needle in prompt, f"{route} attempt {launch_attempt} lost contract needle {needle!r}")
            for needle in (
                b"local repository maintenance change",
                b"the PR achieves its stated Purpose",
                b"local diff, repository tests, and fixtures as proof",
                b"Do not contact or test third-party systems",
            ):
                check((needle in prompt) is has_recovery,
                      f"{route} attempt {launch_attempt} recovery framing drifted at {needle!r}")

    refused = (
        ("launch-external", "external-codex", "1", "initial", "codex-recovery", "standard"),
        ("launch-external", "external-codex", "2", "final", "codex-recovery", "standard"),
        ("retry-external", "external-codex", "2", "recovery", "standard", "codex-recovery"),
        ("retry-external", "external-claude", "2", "recovery", "codex-recovery", "standard"),
        ("launch-native", "native", "3", "recovery", "codex-recovery", "standard"),
        ("launch-native", "native", "1", "initial", "invented", "unknown prompt profile"),
        ("launch-native", "external-codex", "1", "initial", "standard", "requires route"),
    )
    for action, route, launch_attempt, purpose, profile, expected in refused:
        with tempfile.TemporaryDirectory() as raw:
            producer = "reviewer-tool-write"
            args = _fixture(
                Path(raw),
                launch_attempt=launch_attempt,
                allocation_purpose=purpose,
                review_action=action,
                route=route,
                producer=producer,
                prompt_profile=profile,
            )
            _prepare_predecessors(args)
            args.allocation_purpose = purpose
            _refused(args, expected)
            paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
            check(not paths["prompt"].exists() and not paths["progress"].exists(),
                  f"{route} attempt {launch_attempt} invalid profile created launch artifacts")


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
    transport = {"prompt_profile": "standard", "payload": "literal <INTENT>"}
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
            check(_default_launch_artifacts_absent(rundir),
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
        check(_default_launch_artifacts_absent(rundir),
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
        check(_default_launch_artifacts_absent(Path(args.run_dir)), "wrong-PR intent created identity")
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        (Path(args.run_dir) / "review-41-2.plan.jsonl").write_text("\n", encoding="utf-8")
        _refused(args, "blank")
        check(not list(Path(args.run_dir).glob("*.prompt.txt")), "malformed plan created a prompt")


def t_overlapping_run_dir_and_worktree_create_nothing() -> None:
    """An identical or either-way-nested run-dir/worktree pair refuses and materializes nothing."""

    def _no_artifacts(rundir: Path) -> None:
        check(
            _default_launch_artifacts_absent(rundir),
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
            allocation_purpose="initial",
            worktree=os.fspath(worktree),
            base="main",
            route="native",
            prompt_profile="standard",
            report_producer="reviewer-tool-write",
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

        # The suite swaps a private module attribute to inject the failure. `setattr` is how that is
        # spelled for a ModuleType the checker knows nothing about; the assignment form claims an
        # attribute the type does not declare.
        setattr(D, "_stage_bytes", fail_second_stage)
        try:
            D.install_pair(prompt, b"prompt", progress, b"identity")
        except OSError as exc:
            check("second-stage" in str(exc), "the injected staging failure must reach the caller")
        else:
            check(False, "second staging failure must refuse preparation")
        finally:
            setattr(D, "_stage_bytes", real_stage)
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
                "launch_attempt": "1", "dispatched_at": "2026-07-19T00:00:00Z", "default_non_goals": [],
            }, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        D.prepare(args)
        check(paths["prompt"].is_file() and paths["progress"].is_file(),
              "same-attempt prepare did not recover a stale identity-only strand")


def t_malformed_lone_identity_is_refused_not_reclaimed() -> None:
    """A lone ``pass_identity`` that FAILS the read-door schema is foreign residue, not the tool's own inert
    output: recovery leaves it in place and the conflict check refuses, rather than unlinking it and
    silently rebuilding.

    Only the tool writes identities, and it validates the whole record through ``check_progress_file``
    before it ever links one, so it never leaves a MALFORMED lone identity. A malformed one is a hand-edit,
    corruption, or a foreign writer of the driver-owned run file — deleting it destroys evidence. Every
    shape the write door rejects must therefore be refused here too: a ``head_sha`` that is not 40 hex, a
    record missing ``dispatched_at``, and a duplicate key that ``json.loads`` would silently collapse (the
    exact hole plain parsing left). The stale-but-well-formed reclaim is exercised above; this is its
    boundary.
    """
    malformed = {
        "bad head_sha": json.dumps({
            "type": "pass_identity", "pr": "41", "pass": "2", "head_sha": "bad",
            "launch_attempt": "1", "dispatched_at": STAMP, "default_non_goals": [],
        }, separators=(",", ":")),
        "missing dispatched_at": json.dumps({
            "type": "pass_identity", "pr": "41", "pass": "2", "head_sha": SHA,
            "launch_attempt": "1", "default_non_goals": [],
        }, separators=(",", ":")),
        # A duplicate key: json.dumps cannot emit one, so this line is built by hand. json.loads keeps the
        # LAST value and discards the truncated first; strict_object rejects the line outright.
        "duplicate head_sha": (
            '{"type":"pass_identity","pr":"41","pass":"2",'
            '"head_sha":"a3f29c1","head_sha":"' + SHA + '",'
            '"launch_attempt":"1","dispatched_at":"' + STAMP + '","default_non_goals":[]}'
        ),
    }

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        progress = D.attempt_paths(Path(args.run_dir), "41", "2", "1")["progress"]
        valid = json.dumps({
            "type": "pass_identity", "pr": "41", "pass": "2", "head_sha": SHA,
            "launch_attempt": "1", "dispatched_at": STAMP, "default_non_goals": [],
        }, separators=(",", ":"))
        progress.write_text(valid + "\n", encoding="utf-8")
        check(D._identity_only(progress),
              "a well-formed lone identity is no longer recognized as reclaimable inert residue")
        for label, line in malformed.items():
            progress.write_text(line + "\n", encoding="utf-8")
            check(not D._identity_only(progress),
                  f"{label}: a malformed lone identity was treated as reclaimable inert residue")

    for label, line in malformed.items():
        with tempfile.TemporaryDirectory() as raw:
            args = _fixture(Path(raw))
            paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
            paths["progress"].write_text(line + "\n", encoding="utf-8")
            _refused(args, "already present")
            check(paths["progress"].read_text(encoding="utf-8") == line + "\n",
                  f"{label}: recovery altered the malformed lone identity instead of leaving it in place")
            check(not paths["prompt"].exists(),
                  f"{label}: a prompt was materialized despite the malformed-identity conflict")


def t_allocation_reserves_final_review_after_nonterminal_outcomes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(
            Path(raw), launch_attempt="1", allocation_purpose="initial", route="external-codex",
            review_action="launch-external", producer="reviewer-tool-write", prompt_profile="standard",
        )
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        args.allocation_purpose = "recovery"
        _refused(args, "requires the initial review allocation")
        args.allocation_purpose = "initial"
        args.launch_attempt = "2"
        args.review_action = "retry-external"
        args.prompt_profile = "codex-recovery"
        _refused(args, "next allocation 1")
        args.launch_attempt = "1"
        args.review_action = "launch-external"
        args.prompt_profile = "standard"
        D.prepare(args)
        _record_result(args, D.PROVIDER_FAILURE)

        args.launch_attempt = "3"
        args.allocation_purpose = "recovery"
        args.route = "native"
        args.review_action = "fallback-native"
        args.prompt_profile = "standard"
        _refused(args, "next allocation 2")
        args.launch_attempt = "2"
        args.route = "external-codex"
        args.review_action = "retry-external"
        args.prompt_profile = "codex-recovery"
        D.prepare(args)
        _record_result(args, D.TRANSPORT_FAILURE)

        args.launch_attempt = "3"
        args.allocation_purpose = "recovery"
        args.route = "native"
        args.review_action = "fallback-native"
        args.prompt_profile = "standard"
        args.report_producer = "reviewer-tool-write"
        transport = D.prepare(args)["transport"]
        check(transport["attempt"]["launch_attempt"] == 3 and
              transport["report"]["path"].endswith("review-41-2.a3" + D.RP.REPORT_SUFFIX),
              "native fallback did not receive fresh attempt-3 artifacts")
        _record_result(args, D.AMENDED)

        args.launch_attempt = "4"
        args.allocation_purpose = "final"
        args.route = "external-codex"
        args.review_action = "launch-external"
        args.prompt_profile = "standard"
        transport = D.prepare(args)["transport"]
        check(transport["prompt_profile"] == "standard",
              "a final external-Codex launch must use the launch-external standard profile")
        _record_result(args, D.PROVIDER_FAILURE)

        args.launch_attempt = "5"
        args.review_action = "retry-external"
        args.prompt_profile = "codex-recovery"
        transport = D.prepare(args)["transport"]
        check(transport["prompt_profile"] == "codex-recovery",
              "a final external-Codex retry must use the retry-external recovery profile")
        check(Path(transport["prompt_path"]).read_bytes().startswith(D.CODEX_RECOVERY_PREAMBLE),
              "a final external-Codex retry lost its recovery framing")
        _record_result(args, D.INCOMPLETE_PLAN)

        args.launch_attempt = "6"
        args.route = "native"
        args.review_action = "fallback-native"
        args.prompt_profile = "standard"
        D.prepare(args)
        _complete_usable_binary_review(args)
        summary = _record_result(args, D.REVIEWED)
        check(summary["final_state"] == "consumed", f"usable final review did not consume the reserve: {summary}")
        check([(row["purpose"], row["result"]) for row in summary["attempts"]] == [
            ("initial", D.PROVIDER_FAILURE),
            ("recovery", D.TRANSPORT_FAILURE),
            ("recovery", D.AMENDED),
            ("final", D.PROVIDER_FAILURE),
            ("final", D.INCOMPLETE_PLAN),
            ("final", D.REVIEWED),
        ], f"allocation journal lost a purpose/result: {summary}")

        args.launch_attempt = "7"
        args.allocation_purpose = "recovery"
        _refused(args, "usable binary review result")

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        _complete_usable_binary_review(args)
        summary = _record_result(args, D.REVIEWED)
        check(summary["final_state"] == "consumed",
              f"an ordinary usable review did not consume the final allocation: {summary}")
        args.launch_attempt = "2"
        args.allocation_purpose = "recovery"
        _refused(args, "usable binary review result")

    refs = OWNER.parent.parent / "references"
    runtime = (refs / "runtime-adapter.md").read_text(encoding="utf-8")
    stage = (refs / "stage-2-review-gate.md").read_text(encoding="utf-8")
    loop = (refs / "loop-control.md").read_text(encoding="utf-8")
    dispatch = (refs / "review-dispatch.md").read_text(encoding="utf-8")
    check("three ordinary allocations plus one reserved final allocation" in runtime,
          "runtime owner does not reserve the final review allocation")
    for outcome in (
        "provider-failure", "transport-failure", "malformed-output", "incomplete-plan", "amended",
        "head-invalidated", "scope-invalidated",
    ):
        check(f"`{outcome}`" in runtime,
              f"runtime owner does not name {outcome!r} as preserving the final allocation")
    check("gap or out-of-order number" in runtime,
          "runtime owner does not require a contiguous allocation sequence")
    for name, text in (("Stage 2", stage), ("killed-session", loop)):
        check("Review allocation journal" in text,
              f"{name} recovery does not point to the allocation-policy owner")
    check("result --result reviewed" in dispatch and "missing, deferred" in dispatch,
          "review-dispatch.md does not state the reviewed-result evidence requirement")


def t_reviewed_result_requires_usable_binary_review() -> None:
    """`reviewed` cannot consume the reserve without a verify-countable active attempt."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        _result_refused(args, D.REVIEWED, "review-pass verify returned unusable")
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        check(not paths["report"].exists(), "missing-report fixture accidentally wrote review evidence")

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        _write_report(args, D.RP.DEFERRED)
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        check(D.RP.parse_report(paths["progress"])["verdict"] == D.RP.DEFERRED,
              "deferred fixture did not reach review-pass's report reader")
        _result_refused(args, D.REVIEWED, "review-pass verify returned incomplete")

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        paths = D.attempt_paths(Path(args.run_dir), args.pr, args.review_pass, args.launch_attempt)
        paths["report"].write_text('{"type":"review_report"}\n', encoding="utf-8")
        _result_refused(args, D.REVIEWED, "review-pass verify returned unusable")

    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        _write_report(args, D.RP.SATISFIED)
        _result_refused(args, D.REVIEWED, "review-pass verify returned incomplete")


def t_reviewed_result_refuses_scope_drift() -> None:
    """A scope-invalidated pass settles without consuming the final reserve."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), default_non_goals='["old"]')
        ledger = _build_ledger_scope(Path(args.run_dir), args.pr, "main", '["old"]')
        args.file = os.fspath(ledger)
        D.prepare(args)
        _complete_usable_binary_review(args)
        _result_refused(args, D.SCOPE_INVALIDATED, "still match")
        proc = subprocess.run(  # noqa: S603
            [sys.executable, os.fspath(D.LEDGER), "--file", os.fspath(ledger),
             "header", "set", "default_non_goals", '["old", "new"]'],
            capture_output=True, text=True, check=False)
        check(proc.returncode == 0, f"scope-drift fixture could not update the ledger: {proc.stderr.strip()}")
        _result_refused(args, D.REVIEWED, "review scope")
        _path, _text, _records, allocations, results = D.load_allocations(
            Path(args.run_dir), args.pr, args.review_pass)
        summary = D.allocation_summary(allocations, results)
        check(summary["final_state"] == "reserved",
              f"scope-drifted reviewed result consumed the final allocation: {summary}")
        check(summary["attempts"] == [{"launch_attempt": 1, "purpose": "initial", "result": "in-flight"}],
              f"scope-drifted reviewed result settled the allocation: {summary}")
        summary = _record_result(args, D.SCOPE_INVALIDATED)
        check(summary["final_state"] == "reserved", f"scope invalidation spent the final reserve: {summary}")
        check(summary["attempts"] == [{
            "launch_attempt": 1, "purpose": "initial", "result": D.SCOPE_INVALIDATED,
        }], f"scope invalidation did not settle the stale allocation: {summary}")
        args.launch_attempt = "2"
        args.allocation_purpose = "final"
        args.default_non_goals = '["old", "new"]'
        D.prepare(args)


def t_reviewed_result_refuses_live_head_drift() -> None:
    """A changed selected-ledger head settles as head-invalidated and permits final retry."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(
            Path(raw), head_sha="a" * 40, route="external-codex", review_action="launch-external",
        )
        ledger = _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]", head_sha=args.head_sha)
        args.file = os.fspath(ledger)
        D.prepare(args)
        _complete_usable_binary_review(args)
        _result_refused(args, D.HEAD_INVALIDATED, "is still the live ledger head")
        code, _out, err = capture_cli(D.L.main, [
            "--file", os.fspath(ledger), "set", "--pr", args.pr, "--head-sha", "b" * 40,
        ])
        check(code == 0 and err == "", f"head-drift fixture could not update the ledger: {err!r}")
        _result_refused(args, D.REVIEWED, "live ledger head")
        summary = _record_result(args, D.HEAD_INVALIDATED)
        check(summary["final_state"] == "reserved", f"head invalidation spent the final reserve: {summary}")
        check(summary["attempts"] == [{
            "launch_attempt": 1, "purpose": "initial", "result": D.HEAD_INVALIDATED,
        }], f"head invalidation did not settle the stale allocation: {summary}")
        args.launch_attempt = "2"
        args.allocation_purpose = "final"
        args.review_action = "launch-external"
        args.prompt_profile = "standard"
        transport = D.prepare(args)["transport"]
        check(transport["prompt_profile"] == "standard",
              "a final external-Codex launch must use the launch-external standard profile")


def t_binary_review_is_journaled_before_verdict_tally() -> None:
    """A verified binary pass is durably settled before the tally instruction."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        _build_ledger_scope(Path(args.run_dir), args.pr, "main", "[]")
        D.prepare(args)
        _complete_usable_binary_review(args)
        result_argv = [
            "result", "--run-dir", args.run_dir, "--pr", args.pr, "--pass", args.review_pass,
            "--launch-attempt", args.launch_attempt, "--result", D.REVIEWED,
        ]
        code, out, err = capture_cli(D.main, result_argv)
        check(code == 0 and err == "", f"binary result command failed: code={code}, stderr={err!r}")
        result = json.loads(out)
        check(result["attempts"] == [{"launch_attempt": 1, "purpose": "initial", "result": D.REVIEWED}],
              f"binary result did not journal the reviewed outcome: {result}")
        status_argv = ["allocation-status", "--run-dir", args.run_dir, "--pr", args.pr,
                       "--pass", args.review_pass]
        code, out, err = capture_cli(D.main, status_argv)
        check(code == 0 and err == "", f"allocation-status failed: code={code}, stderr={err!r}")
        check(json.loads(out)["attempts"] == result["attempts"],
              "allocation-status did not render the binary outcome that result journaled")

    loop = (OWNER.parent.parent / "references" / "loop-control.md").read_text(encoding="utf-8")
    completion = loop[loop.index("### Step 2 — Fold in completions"):loop.index("### Step 3 — Dispatch due work")]
    verify = completion.index("review-pass.py verify")
    result = completion.index("review-dispatch.py result")
    verdict = completion.index("scripts/ledger.py verdict")
    check(verify < result < verdict,
          "Step 2 must journal the verified binary result before recording its ledger verdict")


def t_killed_session_resume_settles_before_next_allocation() -> None:
    """A dead initial attempt is settled before recovery selects launch attempt two."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        D.prepare(args)
        _path, _text, _records, allocations, results = D.load_allocations(
            Path(args.run_dir), args.pr, args.review_pass)
        check(D.allocation_summary(allocations, results)["attempts"][0]["result"] == "in-flight",
              "the prepared dead-attempt fixture was not initially in flight")
        _record_result(args, D.PROVIDER_FAILURE)
        _path, _text, _records, allocations, results = D.load_allocations(
            Path(args.run_dir), args.pr, args.review_pass)
        check(D.allocation_summary(allocations, results)["attempts"][0]["result"] == D.PROVIDER_FAILURE,
              "the dead initial attempt was not settled before allocation selection")
        args.launch_attempt = "2"
        args.allocation_purpose = "recovery"
        D.prepare(args)
        _path, _text, _records, allocations, results = D.load_allocations(
            Path(args.run_dir), args.pr, args.review_pass)
        check(D.allocation_summary(allocations, results)["attempts"] == [
            {"launch_attempt": 1, "purpose": "initial", "result": D.PROVIDER_FAILURE},
            {"launch_attempt": 2, "purpose": "recovery", "result": "in-flight"},
        ], "recovery allocation did not follow the settled dead attempt")

    loop = (OWNER.parent.parent / "references" / "loop-control.md").read_text(encoding="utf-8")
    for start, end in (
        ("- **This run has live work → resume.", "**Reconcile against ground truth**"),
        ("#### Resume after a killed session", "**Every dead pass must land on exactly one branch"),
    ):
        resume = loop[loop.index(start):loop.index(end)]
        check(resume.index("review-dispatch.py result") < resume.index("allocation-status"),
              "resume must settle a dead attempt before allocation-status selects another allocation")


def t_zero_launch_evidence_settles_transport_failure_before_retry() -> None:
    """A killed zero-evidence attempt settles as transport-failure before recovery allocates."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw))
        D.prepare(args)
        _record_result(args, D.TRANSPORT_FAILURE)
        args.launch_attempt = "2"
        args.allocation_purpose = "recovery"
        D.prepare(args)
        _path, _text, _records, allocations, results = D.load_allocations(
            Path(args.run_dir), args.pr, args.review_pass)
        check(D.allocation_summary(allocations, results)["attempts"] == [
            {"launch_attempt": 1, "purpose": "initial", "result": D.TRANSPORT_FAILURE},
            {"launch_attempt": 2, "purpose": "recovery", "result": "in-flight"},
        ], "zero-launch recovery did not follow a settled transport failure")

    refs = OWNER.parent.parent / "references"
    stage = (refs / "stage-2-review-gate.md").read_text(encoding="utf-8")
    zero_launch = stage[stage.index("- **Zero launch evidence"):stage.index("- **This deadline test")]
    check(zero_launch.index("result --result transport-failure") <
          zero_launch.index("allocation-status") <
          zero_launch.index("Review preparation mapping"),
          "zero-launch recovery does not settle transport failure before allocation-status selects a retry")
    check("--allocation-purpose final" in zero_launch,
          "zero-launch recovery does not select the reserved final allocation when it is due")
    loop = (refs / "loop-control.md").read_text(encoding="utf-8")
    loop_launch = loop[loop.index("- a review pass is in flight"):loop.index("- CI red")]
    check(loop_launch.index("result --result\n     transport-failure") <
          loop_launch.index("allocation-status") <
          loop_launch.index("Review preparation mapping"),
          "loop-control selects a zero-launch retry before recording allocation status")
    check("--allocation-purpose final" in loop_launch,
          "loop-control omits the reserved final allocation from zero-launch recovery")
    critical = (refs / "critical-rules.md").read_text(encoding="utf-8")
    critical_launch = critical[critical.index("distinct bars, never collapsed"):critical.index("- Reviewers do not own the plan")]
    check("review-dispatch.py result --result transport-failure" in critical_launch and
          "review-dispatch.py allocation-status" in critical_launch,
          "critical rules omit allocation-status-driven zero-launch recovery")
    reviewer = (refs / "reviewer.md").read_text(encoding="utf-8")
    never_started = reviewer[reviewer.index("A reviewer that **never starts**"):]
    check("Settle it as `transport-failure`" in never_started and
          "`review-dispatch.py allocation-status`" in never_started,
          "reviewer guidance omits allocation-status-driven zero-launch recovery")

    dispatch = (refs / "review-dispatch.md").read_text(encoding="utf-8")
    owners = dispatch[dispatch.index("Inputs have these owners:"):dispatch.index("- `review_root`")]
    check("`review_action`, `route`, `prompt_profile`, and `report_producer` come from" in owners,
          "review preparation mapping owns fields beyond its action, route, producer, and profile")
    check("`launch_attempt` and `allocation_purpose` come only from" in owners and
          "`review-dispatch.py allocation-status`" in owners and
          "**Review preparation mapping** selects\n  neither" in owners,
          "review-dispatch.md does not make the allocation journal the sole allocation-purpose owner")


def t_killed_session_resume_uses_allocation_journal() -> None:
    refs = OWNER.parent.parent / "references"
    for name in ("loop-control.md", "stage-2-review-gate.md", "critical-rules.md"):
        text = (refs / name).read_text(encoding="utf-8")
        check("review-dispatch.py result" in text and "allocation-status" in text and
              "Review allocation journal" in text,
              f"{name} does not route a dead review through durable allocation state")
        check("--allocation-purpose final" in text,
              f"{name} does not prepare the reserved final allocation when it is due")
    loop = (refs / "loop-control.md").read_text(encoding="utf-8")
    stage = (refs / "stage-2-review-gate.md").read_text(encoding="utf-8")
    check("highest-numbered launch\nattempt's `pass_identity`" not in loop,
          "killed-session recovery still budgets from the highest attempt identity")
    check("`launch_attempt` **alone** through" not in stage,
          "Stage 2 still selects a killed-session recovery from attempt number alone")


def t_transition_actions_map_directly_to_prepare_inputs() -> None:
    runtime = (OWNER.parent.parent / "references" / "runtime-adapter.md").read_text(encoding="utf-8")
    for row in (
        "| `launch-external` | selected capability's external route | "
        "`reviewer-tool-write` | `standard` |",
        "| `retry-external` + `external-codex` | `external-codex` | "
        "`reviewer-tool-write` | `codex-recovery` |",
        "| `retry-external` + `external-claude` | `external-claude` | "
        "`reviewer-tool-write` | `standard` |",
        "| `launch-native` / `fallback-native` | `native` | `reviewer-tool-write` | `standard` |",
        "| `park-machine-blocker` | no preparation | no preparation | no preparation |",
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
        _seed_contiguous_history(rundir, "41", "2")
        intent_path = _write_inputs(rundir)
        argv = [
            "prepare", "--run-dir", os.fspath(rundir), "--pr", "41", "--pass", "2",
            "--launch-attempt", "1", "--allocation-purpose", "initial",
            "--worktree", os.fspath(worktree), "--base", "main",
            "--review-action", "launch-native",
            "--route", "native", "--prompt-profile", "standard",
            "--report-producer", "reviewer-tool-write",
            "--head-sha", SHA, "--dispatched-at", STAMP, "--default-non-goals", "[]",
            "--intent-file", os.fspath(intent_path),
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
        args = _fixture(
            Path(raw), review_action="launch-external", route="external-codex",
            producer="reviewer-tool-write",
        )
        argv = [
            "prepare", "--run-dir", args.run_dir, "--pr", args.pr, "--pass", args.review_pass,
            "--launch-attempt", args.launch_attempt, "--allocation-purpose", args.allocation_purpose,
            "--worktree", args.worktree, "--base", args.base,
            "--review-action", args.review_action,
            "--route", args.route, "--prompt-profile", args.prompt_profile,
            "--report-producer", args.report_producer,
            "--head-sha", args.head_sha, "--dispatched-at", args.dispatched_at,
            "--default-non-goals", args.default_non_goals, "--intent-file", args.intent_file,
        ]
        code, out, err = capture_cli(D.main, argv)
        check(code == 0 and err == "", f"prepare CLI failed: code={code}, stderr={err!r}")
        check(out.count("\n") == 1, "prepare CLI must print exactly one JSON record")
        payload = json.loads(out)
        check(payload["route"] == "external-codex", "CLI lost the host-selected route")
        check(set(payload) == {"route", "transport"}, "CLI added host-specific launch behavior")
        check("argv" not in payload and "model" not in payload,
              "materializer must not select or launch a host process")


def _build_ledger(directory: Path, pr: str, base_branch: str, head_sha: str = SHA) -> Path:
    """A real ledger (through ledger.py) with one row for `pr` carrying an EXPLICIT `base_branch`."""
    ledger = directory / "state.jsonl"
    for argv in (["header", "set", "run_id", "t"],
                 ["add-row", "--pr", pr, "--head-sha", head_sha, "--base-branch", base_branch]):
        proc = subprocess.run([sys.executable, os.fspath(D.LEDGER), "--file", os.fspath(ledger), *argv],  # noqa: S603
                              capture_output=True, text=True, check=False)
        check(proc.returncode == 0, f"ledger {' '.join(argv)} failed: {proc.stderr.strip()}")
    return ledger


def t_ledger_base_assertion_matches_prepares() -> None:
    """A `--file` whose row base equals `--base` passes the assertion and prepares one record as usual."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger(root, "41", "main")
        args = _fixture(root, base="main", file=os.fspath(ledger))
        payload = D.prepare(args)
        check(payload["transport"]["base"] == "main", f"the matching base must ride the transport: {payload!r}")


def t_default_non_goals_binds_into_identity() -> None:
    """The run's default Non-goals ride `--default-non-goals` and are BOUND into the pass_identity — the
    immutable, canonical dispatch-time scope the tally measures the verdict against (`check_scope`). A
    malformed value refuses before any identity is written; a non-canonical one is canonicalized through the
    ledger's ONE validator, so what lands is exactly what `verify --ledger` compares."""
    with tempfile.TemporaryDirectory() as raw:
        args = _fixture(Path(raw), default_non_goals='["  area X  ", "y"]')
        D.prepare(args)
        progress = D.attempt_paths(Path(args.run_dir), "41", "2", "1")["progress"]
        events = D.RP.parse_lines(progress.read_text(encoding="utf-8"), progress.name)
        ident = D.RP.check_identity(events, "41", "2", "1")
        check(ident["default_non_goals"] == ["area X", "y"],
              f"the identity must carry the CANONICAL run defaults, got {ident.get('default_non_goals')!r}")
    with tempfile.TemporaryDirectory() as raw:
        _refused(_fixture(Path(raw), default_non_goals="not-json"), "canonical JSON array")


def t_ledger_base_assertion_mismatch_refuses() -> None:
    """A `--file` whose row base disagrees with `--base` refuses — --base is an assertion, not a source."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger(root, "41", "main")
        args = _fixture(root, base="v3", file=os.fspath(ledger))
        _refused(args, "disagrees")


def t_ledger_origin_named_base_matches() -> None:
    """A row base LITERALLY named `origin/rel` (a legal branch name) matches an identical `--base` — the
    assertion routes through `ledger.py base_agrees`, where identical strings always agree. The bare form
    refuses: the STORED base is never stripped."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger(root, "41", "origin/rel")
        args = _fixture(root, base="origin/rel", file=os.fspath(ledger))
        payload = D.prepare(args)
        check(payload["transport"]["base"] == "origin/rel",
              f"identical origin/rel strings must pass the assertion and prepare: {payload!r}")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger(root, "41", "origin/rel")
        args = _fixture(root, base="rel", file=os.fspath(ledger))
        _refused(args, "disagrees")


def t_ledger_variant_spelling_uses_row_base() -> None:
    """A `--base` spelling `base_agrees` accepts but that names a DIFFERENT git ref than the row's base must
    NOT ride the transport. The reviewer diffs `origin/<TRANSPORT.base>...HEAD`, so an `origin/main` transport
    base against a row base `main` would diff `origin/origin/main` — a doubled, usually-nonexistent ref. The
    transport carries the ROW's resolved `effective_base`, so both `main` and the accepted `origin/main` form
    prepare the SAME `base=main`. FAILS if the raw `--base` rides the transport instead of the row's base."""
    for spelling in ("main", "origin/main"):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = _build_ledger(root, "41", "main")
            args = _fixture(root, base=spelling, file=os.fspath(ledger))
            transport = D.prepare(args)["transport"]
            check(transport["base"] == "main",
                  f"--base {spelling} must ride the transport as the row's effective base 'main', "
                  f"got {transport['base']!r}")


def t_ledger_unresolved_base_refuses() -> None:
    """A both-`-` ledger (header base unset AND row base unset) resolves through `effective_base` to the `-`
    sentinel — an UNRESOLVED base. `--base` is refused as "no usable effective base" BEFORE it can ride the
    transport, never accepted (`ledger.py require_effective_base`, the one owner). If that guard is deleted,
    the base assertion is SKIPPED and a caller `--base` prepares a transport unvalidated — so this FAILS."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = root / "state.jsonl"
        for argv in (["header", "set", "run_id", "t"],                      # base_branch left `-`
                     ["add-row", "--pr", "41", "--head-sha", "a" * 40]):    # row base-branch left `-`
            proc = subprocess.run([sys.executable, os.fspath(D.LEDGER), "--file", os.fspath(ledger), *argv],  # noqa: S603
                                  capture_output=True, text=True, check=False)
            check(proc.returncode == 0, f"ledger {' '.join(argv)} failed: {proc.stderr.strip()}")
        args = _fixture(root, base="v3", file=os.fspath(ledger))
        _refused(args, "no usable effective base")


def t_ledger_missing_row_refuses() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger(root, "99", "main")
        args = _fixture(root, pr="41", base="main", file=os.fspath(ledger))
        _refused(args, "no ledger row for pr 41")


def _build_ledger_scope(directory: Path, pr: str, base_branch: str, default_non_goals: str,
                        head_sha: str = SHA) -> Path:
    """A real ledger (through ledger.py) with one row for `pr` and a header `default_non_goals` set — the
    LIVE run scope `prepare`'s `--default-non-goals` assertion (F3) is checked against."""
    ledger = _build_ledger(directory, pr, base_branch, head_sha)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, os.fspath(D.LEDGER), "--file", os.fspath(ledger),
         "header", "set", "default_non_goals", default_non_goals],
        capture_output=True, text=True, check=False)
    check(proc.returncode == 0, f"ledger header set default_non_goals failed: {proc.stderr.strip()}")
    return ledger


def t_ledger_default_non_goals_assertion_matches_prepares() -> None:
    """F3: with `--file` present, `--default-non-goals` is an ASSERTION against the header's live scope. A
    value EQUAL to the header's `default_non_goals` passes and binds that scope into the pass_identity."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger_scope(root, "41", "main", '["area X"]')
        args = _fixture(root, base="main", file=os.fspath(ledger), default_non_goals='["area X"]')
        D.prepare(args)
        progress = D.attempt_paths(Path(args.run_dir), "41", "2", "1")["progress"]
        events = D.RP.parse_lines(progress.read_text(encoding="utf-8"), progress.name)
        ident = D.RP.check_identity(events, "41", "2", "1")
        check(ident["default_non_goals"] == ["area X"],
              f"the matching scope must bind into the identity, got {ident.get('default_non_goals')!r}")


def t_ledger_default_non_goals_assertion_mismatch_refuses() -> None:
    """F3: with `--file` present, a `--default-non-goals` that DISAGREES with the header's live scope refuses
    — the header owns the scope, `--default-non-goals` only asserts it. Mirrors the base-mismatch refusal one
    field over; delete the check and a stale scope binds a value the run has since left, unrefused."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ledger = _build_ledger_scope(root, "41", "main", '["area X"]')
        args = _fixture(root, base="main", file=os.fspath(ledger), default_non_goals="[]")
        _refused(args, "disagrees with pr 41's ledger header default_non_goals")


CASES = [
    (
        "relaunch-path-coherence",
        "all relaunch artifacts derive from one attempt identity",
        t_relaunch_paths_share_one_attempt_identity,
    ),
    ("history-gap", "a missing earlier pass refuses later review preparation",
     t_missing_historical_pass_refuses_later_dispatch),
    ("history-prior-head", "contiguous history validates each prior pass against its recorded head",
     t_contiguous_history_accepts_prior_heads_after_repair),
    ("history-unruled-amendment", "an unruled historical amendment refuses later review preparation",
     t_historical_unruled_amendment_refuses_later_dispatch),
    ("history-binary-verdict", "a prior deferred result refuses later review preparation",
     t_nonbinary_historical_verdict_refuses_later_dispatch),
    ("attempt-one", "prepare materializes one coherent attempt-1 record", t_prepare_attempt_one_materializes_one_record),
    ("later-external-attempt", "later attempts preserve suffix and the reviewer's report door", t_later_attempt_keeps_the_reviewer_report_door),
    ("producer-pairing", "route and sole report producer must agree", t_route_and_report_owner_must_agree),
    ("prompt-profile", "prompt profiles are typed and scoped to the review action",
     t_prompt_profiles_are_typed_and_action_scoped),
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
    ("malformed-identity-refused", "a malformed lone identity is refused, not reclaimed", t_malformed_lone_identity_is_refused_not_reclaimed),
    ("allocation-final-reserve", "nonterminal outcomes preserve one fresh final review allocation",
     t_allocation_reserves_final_review_after_nonterminal_outcomes),
    ("reviewed-needs-binary", "reviewed refuses a missing, deferred, malformed, or incomplete review",
     t_reviewed_result_requires_usable_binary_review),
    ("reviewed-refuses-scope-drift", "reviewed preserves the final reservation when the run scope moves",
     t_reviewed_result_refuses_scope_drift),
    ("reviewed-refuses-head-drift", "reviewed settles a changed ledger head without consuming the final reserve",
     t_reviewed_result_refuses_live_head_drift),
    ("binary-result-before-verdict", "verified binary outcomes are journaled before the tally instruction",
     t_binary_review_is_journaled_before_verdict_tally),
    ("killed-session-settlement", "dead attempts settle before resumed recovery allocation",
     t_killed_session_resume_settles_before_next_allocation),
    ("zero-launch-settlement", "zero launch evidence settles transport failure before recovery allocation",
     t_zero_launch_evidence_settles_transport_failure_before_retry),
    ("killed-session-allocation", "killed-session recovery selects the durable due allocation",
     t_killed_session_resume_uses_allocation_journal),
    ("transition-mapping", "review actions map directly to route, producer, and prompt profile",
     t_transition_actions_map_directly_to_prepare_inputs),
    ("unicode-delivery", "a Unicode path is delivered as UTF-8 bytes under ASCII stdout", t_unicode_worktree_delivers_under_ascii_stdout),
    ("host-neutral-json", "CLI emits canonical data and never launches", t_cli_emits_only_canonical_host_neutral_json),
    ("ledger-base-match", "--file with a matching row base passes the assertion and prepares",
     t_ledger_base_assertion_matches_prepares),
    ("scope-binds-into-identity", "the run defaults bind into pass_identity as the canonical dispatch-time scope",
     t_default_non_goals_binds_into_identity),
    ("ledger-base-mismatch", "--file with a disagreeing row base refuses (--base is an assertion)",
     t_ledger_base_assertion_mismatch_refuses),
    ("ledger-origin-named-base", "a base literally named origin/<x> matches itself; the bare form refuses",
     t_ledger_origin_named_base_matches),
    ("ledger-variant-spelling-row-base",
     "an accepted origin/<base> spelling rides the transport as the row's effective base, not the raw arg",
     t_ledger_variant_spelling_uses_row_base),
    ("ledger-unresolved-base", "--file resolving to a `-`/blank effective base refuses before the assertion",
     t_ledger_unresolved_base_refuses),
    ("ledger-missing-row", "--file naming an unknown PR row refuses", t_ledger_missing_row_refuses),
    ("ledger-default-non-goals-match",
     "--file with --default-non-goals equal to the header scope prepares and binds it",
     t_ledger_default_non_goals_assertion_matches_prepares),
    ("ledger-default-non-goals-mismatch",
     "--file with --default-non-goals disagreeing with the header scope refuses (an assertion, not a source)",
     t_ledger_default_non_goals_assertion_mismatch_refuses),
]
