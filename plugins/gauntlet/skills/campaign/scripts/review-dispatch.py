#!/usr/bin/env python3
"""Prepare one review launch attempt's identity, prompt, and typed transport record.

This command is the executable boundary between campaign policy and host dispatch. The host first chooses
an available route through ``runtime-adapter.md``. ``prepare`` then validates that route's report owner,
the complete earlier review history, and the existing plan and intent through ``review-pass.py``. It derives
every attempt-scoped path from one identity, creates the progress identity and bound prompt, and prints the
canonical JSON record the host launches. It does not select a route, test route availability, or launch a
reviewer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn

from _gauntlet.atomic import replace_text
from _gauntlet.modules import load_module_from_path, load_sibling
from _gauntlet.testing import run_sibling_suite


_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "review-dispatch-test.py"
TEMPLATE = _HERE / "review-prompt.txt"
REVIEW_PASS = _HERE / "review-pass.py"
LEDGER = _HERE / "ledger.py"

TRANSPORT_SLOT = b"<TRANSPORT-RECORD>"
INTENT_SLOT = b"<INTENT>"
SLOT_RE = re.compile(rb"<[A-Z][A-Z0-9-]*>")

# **ONE PRODUCER, EVERY ROUTE — and it is a SWAP, not an addition.** The report used to have a
# route-dependent owner: a native worker wrote the file itself, and an external process's final output
# channel was captured at the report path by the orchestrator (`codex -o`, a `stdout_file`) with the
# reviewer forbidden to write it. Now the reviewer runs `emit-report.py` on every route, and the captured
# channel is authoritative for nothing. The mapping stays a table because the route is still validated
# against it — it simply has one value on the right-hand side now, which is the point.
ROUTE_PRODUCERS = {
    "native": "reviewer-tool-write",
    "external-codex": "reviewer-tool-write",
    "external-claude": "reviewer-tool-write",
}
REPORT_PRODUCERS = tuple(sorted(set(ROUTE_PRODUCERS.values())))
PROMPT_PROFILES = ("standard", "codex-recovery")
INITIAL_ALLOCATION = "initial"
RECOVERY_ALLOCATION = "recovery"
FINAL_ALLOCATION = "final"
ALLOCATION_PURPOSES = (INITIAL_ALLOCATION, RECOVERY_ALLOCATION, FINAL_ALLOCATION)

# A review pass keeps three bounded launch allocations for the ordinary route-recovery sequence.  The
# final allocation is separate: it is the independent review owed after a plan repair/amendment, and a
# provider/transport/artifact failure must never silently spend it.  Its retries receive new attempt
# artifacts but retain this one reservation until a usable binary review lands.
ORDINARY_ALLOCATION_LIMIT = 3
ALLOCATION = "review_allocation"
ALLOCATION_RESULT = "review_allocation_result"
PROVIDER_FAILURE = "provider-failure"
TRANSPORT_FAILURE = "transport-failure"
MALFORMED_OUTPUT = "malformed-output"
INCOMPLETE_PLAN = "incomplete-plan"
AMENDED = "amended"
REVIEWED = "reviewed"
ALLOCATION_RESULTS = (
    PROVIDER_FAILURE,
    TRANSPORT_FAILURE,
    MALFORMED_OUTPUT,
    INCOMPLETE_PLAN,
    AMENDED,
    REVIEWED,
)
FINAL_RETRYABLE_RESULTS = frozenset(ALLOCATION_RESULTS) - {REVIEWED}
CODEX_RECOVERY_PREAMBLE = (
    b"REPOSITORY MAINTENANCE REVIEW RETRY\n"
    b"Review this local repository maintenance change. The concrete local goal is to decide whether "
    b"the PR achieves its stated Purpose while preserving the complete review contract below. Use the "
    b"local diff, repository tests, and fixtures as proof. Do not "
    b"contact or test third-party systems.\n\n"
)


def _load_review_pass():
    mod = load_module_from_path("review_dispatch_review_pass", REVIEW_PASS)
    if mod is None:
        raise RuntimeError(f"cannot load the review-pass schema owner at {REVIEW_PASS}")
    return mod


RP = _load_review_pass()


L = load_sibling("review_dispatch_ledger", _HERE, "ledger.py")


class Refusal(Exception):
    """Preparation inputs are not one usable, fresh review launch attempt."""


class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def refuse(message: str) -> NoReturn:
    raise Refusal(message)


def _validate_id(name: str, value: str) -> None:
    try:
        RP.check_id(name, value, f"review-dispatch --{name.replace('_', '-')}")
    except RP.Defect as exc:
        refuse(str(exc))


def _absolute_directory(path: Path, field: str) -> None:
    if not path.is_absolute():
        refuse(f"--{field} must be an absolute path, got {path}")
    if not path.is_dir():
        refuse(f"--{field} is not an existing directory: {path}")


def _reject_overlapping_dirs(rundir: Path, worktree: Path) -> None:
    """Refuse a run-dir/worktree pair that is the same directory or nests either way.

    ``prepare`` derives every artifact from ``run-dir`` and writes into it, while ``worktree`` is
    the candidate checkout the review must not mutate. When the two overlap, the materializer would
    write launch artifacts inside the supplied worktree. The campaign driver never produces such a
    pair (``run-dir`` is a ``.gauntlet/...`` path, ``worktree`` a ``.worktrees/<ref>`` path, so they
    are structurally distinct), so this is cheap defense-in-depth for the tool's reject-malformed-
    paths posture: exactly these three refusals, no path-isolation machinery beyond them.
    """
    real_rundir = Path(os.path.realpath(rundir))
    real_worktree = Path(os.path.realpath(worktree))
    if real_rundir == real_worktree:
        refuse(
            "--run-dir and --worktree must be different directories; both resolve to "
            f"{real_rundir}"
        )
    if real_worktree in real_rundir.parents:
        refuse(
            f"--run-dir must not be nested inside --worktree; {real_rundir} is inside {real_worktree}"
        )
    if real_rundir in real_worktree.parents:
        refuse(
            f"--worktree must not be nested inside --run-dir; {real_worktree} is inside {real_rundir}"
        )


def _recorded_head(progress: Path) -> str:
    """Read one active attempt's validated, immutable review head."""
    pr, review_pass, launch_attempt = RP.parse_name(progress)
    events = RP.parse_lines(RP.read_text(progress, "historical progress file"), progress.name)
    RP.check_events(events, progress.name)
    identity = RP.check_identity(events, pr, review_pass, launch_attempt)
    return identity["head_sha"]


def require_contiguous_history(rundir: Path, pr: str, review_pass: str) -> None:
    """Refuse a later pass unless every prior pass has usable terminal evidence.

    A repair moves the PR head after an earlier review lands. Historical passes therefore validate against
    their OWN immutable ``pass_identity.head_sha``, not the current launch's head or current intent. Only
    each pass's active (highest-attempt) artifact is historical evidence; superseded retry attempts never
    landed a result.
    """
    target = int(review_pass)
    if target == 1:
        return

    historical: dict[int, Path] = {}
    for progress in RP.active_attempts(rundir):
        artifact_pr, artifact_pass, _ = RP.parse_name(progress)
        number = int(artifact_pass)
        if artifact_pr == pr and number < target:
            historical[number] = progress

    for number in range(1, target):
        progress = historical.get(number)
        if progress is None:
            refuse(
                f"review history for pr {pr} is missing completed pass {number} before requested pass "
                f"{review_pass} — recover or restart the missing pass before dispatching later review work"
            )
        try:
            recorded_head = _recorded_head(progress)
            # A terminal report records the reviewer verdict, not a durable orchestrator ruling count.
            outcome, reason, report = RP.evaluate_historical_detail(progress, recorded_head)
        except (OSError, RP.Defect) as exc:
            refuse(
                f"review history for pr {pr} pass {number} is invalid: {exc} — recover or restart that "
                "pass before dispatching later review work"
            )
        if report is None or report["verdict"] not in (RP.SATISFIED, RP.NOT_SATISFIED):
            verdict = None if report is None else report["verdict"]
            refuse(
                f"review history for pr {pr} pass {number} has no terminal binary verdict "
                f"(got {verdict!r}) — recover or restart that pass before dispatching later review work"
            )
        if outcome != RP.OK:
            refuse(
                f"review history for pr {pr} pass {number} is invalid: {reason} — recover or restart "
                "that pass before dispatching later review work"
            )


def attempt_paths(rundir: Path, pr: str, review_pass: str, launch_attempt: str) -> "dict[str, Path]":
    """Derive the complete artifact set from one validated attempt identity."""
    _validate_id("pr", pr)
    _validate_id("pass", review_pass)
    _validate_id("launch_attempt", launch_attempt)
    suffix = "" if launch_attempt == "1" else f".a{launch_attempt}"
    pass_stem = f"review-{pr}-{review_pass}"
    attempt_stem = pass_stem + suffix
    return {
        "prompt": rundir / f"{attempt_stem}.prompt.txt",
        "plan": rundir / f"{pass_stem}.plan.jsonl",
        "progress": rundir / f"{attempt_stem}.progress.jsonl",
        "findings": rundir / f"{attempt_stem}.findings.jsonl",
        "report": rundir / f"{attempt_stem}{RP.REPORT_SUFFIX}",
        "intent": rundir / f"intent-{pr}.md",
    }


def allocation_path(rundir: Path, pr: str, review_pass: str) -> Path:
    """The per-pass allocation journal, distinct from reviewer-owned gate artifacts."""
    _validate_id("pr", pr)
    _validate_id("pass", review_pass)
    return rundir / f"review-{pr}-{review_pass}.allocation.jsonl"


def _strict_records(path: Path) -> tuple[str, list[dict]]:
    """Read a journal as strict JSONL without making reviewer artifacts accept new records."""
    if not os.path.lexists(os.fspath(path)):
        return "", []
    if path.is_symlink() or not path.is_file():
        refuse(f"allocation journal must be a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        refuse(f"cannot read allocation journal {path}: {exc}")
    if not text:
        refuse(f"allocation journal {path} is empty — a durable journal cannot have no record")
    try:
        records = RP.parse_lines(text, path.name)
    except RP.Defect as exc:
        refuse(f"allocation journal {path} is malformed: {exc}")
    return text, records


def _check_allocation_time(value: object, where: str) -> None:
    if not isinstance(value, str) or not RP.TS_RE.match(value) or not RP.real_utc(value):
        refuse(f"{where} must be a real UTC ISO-8601 timestamp, got {value!r}")


def _check_allocation_record(record: dict, where: str, pr: str, review_pass: str) -> None:
    if record.get("type") != ALLOCATION:
        refuse(f"{where} must be a {ALLOCATION!r} record, got {record.get('type')!r}")
    keys = {"type", "pr", "pass", "launch_attempt", "head_sha", "dispatched_at", "purpose"}
    if set(record) != keys:
        refuse(f"{where} must carry exactly {sorted(keys)}, got {sorted(record)}")
    for field in ("pr", "pass", "launch_attempt", "head_sha", "dispatched_at", "purpose"):
        if not isinstance(record[field], str):
            refuse(f"{where} field {field!r} must be text")
    if record["pr"] != pr or record["pass"] != review_pass:
        refuse(f"{where} names pr/pass {record['pr']}/{record['pass']}, not {pr}/{review_pass}")
    _validate_id("pr", record["pr"])
    _validate_id("pass", record["pass"])
    _validate_id("launch_attempt", record["launch_attempt"])
    _validate_id("head_sha", record["head_sha"])
    _check_allocation_time(record["dispatched_at"], f"{where} dispatched_at")
    if record["purpose"] not in ALLOCATION_PURPOSES:
        refuse(f"{where} purpose must be one of {list(ALLOCATION_PURPOSES)}, got {record['purpose']!r}")


def _check_result_record(record: dict, where: str, pr: str, review_pass: str,
                         allocations: dict[str, dict], settled: set[str]) -> None:
    if record.get("type") != ALLOCATION_RESULT:
        refuse(f"{where} must be a {ALLOCATION_RESULT!r} record, got {record.get('type')!r}")
    keys = {"type", "pr", "pass", "launch_attempt", "result", "recorded_at"}
    if set(record) != keys:
        refuse(f"{where} must carry exactly {sorted(keys)}, got {sorted(record)}")
    for field in ("pr", "pass", "launch_attempt", "result", "recorded_at"):
        if not isinstance(record[field], str):
            refuse(f"{where} field {field!r} must be text")
    if record["pr"] != pr or record["pass"] != review_pass:
        refuse(f"{where} names pr/pass {record['pr']}/{record['pass']}, not {pr}/{review_pass}")
    _validate_id("launch_attempt", record["launch_attempt"])
    if record["launch_attempt"] not in allocations:
        refuse(f"{where} settles launch attempt {record['launch_attempt']}, which was never allocated")
    if record["launch_attempt"] in settled:
        refuse(f"{where} settles launch attempt {record['launch_attempt']} twice")
    if record["result"] not in ALLOCATION_RESULTS:
        refuse(f"{where} result must be one of {list(ALLOCATION_RESULTS)}, got {record['result']!r}")
    _check_allocation_time(record["recorded_at"], f"{where} recorded_at")


def load_allocations(rundir: Path, pr: str, review_pass: str) -> tuple[Path, str, list[dict], dict[str, dict], dict[str, dict]]:
    """Load and validate one pass's append-only allocation history.

    Allocation and result records deliberately live outside the progress/report files.  Those files are
    gate evidence written by the reviewer and `review-pass.py` must remain able to reject every unknown
    line.  This journal is the driver's durable recovery decision record.
    """
    path = allocation_path(rundir, pr, review_pass)
    text, records = _strict_records(path)
    allocations: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for number, record in enumerate(records, start=1):
        where = f"{path.name} line {number}"
        if record.get("type") == ALLOCATION:
            _check_allocation_record(record, where, pr, review_pass)
            attempt = record["launch_attempt"]
            if attempt in allocations:
                refuse(f"{where} allocates launch attempt {attempt} twice")
            expected = str(len(allocations) + 1)
            if attempt != expected:
                refuse(f"{where} allocates launch attempt {attempt}, but the next allocation must be {expected}")
            allocations[attempt] = record
            continue
        if record.get("type") == ALLOCATION_RESULT:
            _check_result_record(record, where, pr, review_pass, allocations, set(results))
            results[record["launch_attempt"]] = record
            continue
        refuse(f"{where} has unrecognised record type {record.get('type')!r}")
    return path, text, records, allocations, results


def _append_allocation_record(path: Path, text: str, record: dict) -> None:
    try:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        replace_text(path, text + line, temp_prefix=".review-allocation-", encoding="utf-8")
    except OSError as exc:
        refuse(f"cannot durably record allocation state at {path}: {exc}")


def _ensure_allocation_available(allocations: dict[str, dict], results: dict[str, dict],
                                 purpose: str) -> None:
    if purpose not in ALLOCATION_PURPOSES:
        refuse(f"--allocation-purpose must be one of {list(ALLOCATION_PURPOSES)}, got {purpose!r}")
    outstanding = sorted(attempt for attempt in allocations if attempt not in results)
    if outstanding:
        refuse(f"launch attempt(s) {outstanding} have no recorded result — settle the completed attempt before "
               "allocating another one")
    if any(result["result"] == REVIEWED for result in results.values()):
        refuse("this pass already has a usable binary review result; do not allocate another review")
    final_attempts = [record for record in allocations.values() if record["purpose"] == FINAL_ALLOCATION]
    if purpose == FINAL_ALLOCATION:
        if final_attempts:
            last = final_attempts[-1]
            result = results.get(last["launch_attempt"])
            if result is None:
                refuse(f"final allocation attempt {last['launch_attempt']} is still in flight")
            if result["result"] not in FINAL_RETRYABLE_RESULTS:
                refuse(f"final allocation was consumed by {result['result']!r}; no later final review is due")
        return
    if final_attempts:
        refuse("a final review allocation already began; continue or settle that reservation instead of "
               "allocating ordinary recovery work")
    ordinary = [record for record in allocations.values() if record["purpose"] != FINAL_ALLOCATION]
    if purpose == RECOVERY_ALLOCATION and not ordinary:
        refuse("a recovery allocation requires the initial review allocation first")
    if purpose == INITIAL_ALLOCATION and ordinary:
        refuse("an initial review allocation already exists; use recovery or the reserved final review")
    if purpose == RECOVERY_ALLOCATION and len(ordinary) >= ORDINARY_ALLOCATION_LIMIT:
        refuse(f"the {ORDINARY_ALLOCATION_LIMIT} ordinary allocations are spent; use the reserved final review")
    if purpose == INITIAL_ALLOCATION and len(ordinary) >= ORDINARY_ALLOCATION_LIMIT:
        refuse(f"the {ORDINARY_ALLOCATION_LIMIT} ordinary allocations are spent; use the reserved final review")


def _ensure_next_launch_attempt(allocations: dict[str, dict], launch_attempt: str) -> None:
    """Keep a pass's attempt identity contiguous and monotonically increasing."""
    expected = str(len(allocations) + 1)
    if launch_attempt != expected:
        refuse(f"--launch-attempt must be the next allocation {expected}, got {launch_attempt}")


def allocation_summary(allocations: dict[str, dict], results: dict[str, dict], *, journal_present: bool = True) -> dict:
    """Render journal state for a later heartbeat or final report without inferring from filenames."""
    attempts = []
    for attempt, allocation in allocations.items():
        result = results.get(attempt)
        attempts.append({
            "launch_attempt": int(attempt),
            "purpose": allocation["purpose"],
            "result": result["result"] if result is not None else "in-flight",
        })
    finals = [item for item in attempts if item["purpose"] == FINAL_ALLOCATION]
    if not journal_present:
        final_state = "unknown"
    elif any(result["result"] == REVIEWED for result in results.values()):
        final_state = "consumed"
    elif not finals:
        final_state = "reserved"
    elif finals[-1]["result"] == "in-flight":
        final_state = "in-flight"
    else:
        final_state = "reserved"
    return {"ordinary_limit": ORDINARY_ALLOCATION_LIMIT, "final_state": final_state, "attempts": attempts}


def _require_usable_binary_review(rundir: Path, pr: str, review_pass: str, launch_attempt: str,
                                  allocation: dict) -> None:
    """Refuse ``reviewed`` until the allocated attempt verifies as a binary pass result.

    The allocation journal is driver state, not review evidence.  Its ``reviewed`` outcome consumes the
    final reserve, so it must be backed by the same active attempt and run ledger that
    ``review-pass.py verify --ledger`` can count. Deriving the progress/report paths here prevents a caller
    from settling one allocation with another attempt's report.
    """
    paths = attempt_paths(rundir, pr, review_pass, launch_attempt)
    ledger = rundir / "state.jsonl"
    if not ledger.is_file():
        refuse(
            f"launch attempt {launch_attempt} cannot record reviewed without the run ledger {ledger} — "
            "a binary result must verify against this run's current review scope"
        )
    outcome, reason, report = RP.evaluate_detail(paths["progress"], allocation["head_sha"], ledger=ledger)
    if outcome != RP.OK:
        refuse(
            f"launch attempt {launch_attempt} cannot record reviewed: review-pass verify returned "
            f"{outcome}: {reason}"
        )
    if report is None or report["verdict"] not in (RP.SATISFIED, RP.NOT_SATISFIED):
        # `evaluate_detail` currently makes this unreachable, but keep the allocation boundary explicit:
        # a deferred result is not a verdict and must never consume the final reserve.
        refuse(f"launch attempt {launch_attempt} cannot record reviewed without a usable binary review")


def record_result(args) -> dict:
    rundir = Path(args.run_dir)
    _absolute_directory(rundir, "run-dir")
    _validate_id("pr", args.pr)
    _validate_id("pass", args.review_pass)
    _validate_id("launch_attempt", args.launch_attempt)
    path, text, _, allocations, results = load_allocations(rundir, args.pr, args.review_pass)
    if args.launch_attempt not in allocations:
        refuse(f"launch attempt {args.launch_attempt} has no allocation record in {path}")
    if args.launch_attempt in results:
        refuse(f"launch attempt {args.launch_attempt} already recorded result {results[args.launch_attempt]['result']!r}")
    if args.result not in ALLOCATION_RESULTS:
        refuse(f"--result must be one of {list(ALLOCATION_RESULTS)}, got {args.result!r}")
    if args.result == REVIEWED:
        _require_usable_binary_review(
            rundir,
            args.pr,
            args.review_pass,
            args.launch_attempt,
            allocations[args.launch_attempt],
        )
    stamped = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "type": ALLOCATION_RESULT,
        "pr": args.pr,
        "pass": args.review_pass,
        "launch_attempt": args.launch_attempt,
        "result": args.result,
        "recorded_at": stamped,
    }
    _append_allocation_record(path, text, record)
    results = {**results, args.launch_attempt: record}
    return allocation_summary(allocations, results)


def validate_prompt_profile(route: str, launch_attempt: str, prompt_profile: str) -> None:
    """Require the one profile assigned by the runtime action/route mapping."""
    if prompt_profile not in PROMPT_PROFILES:
        refuse(f"unknown prompt profile {prompt_profile!r}; expected one of {list(PROMPT_PROFILES)}")
    required = (
        "codex-recovery"
        if route == "external-codex" and launch_attempt == "2"
        else "standard"
    )
    if prompt_profile != required:
        refuse(
            f"route {route!r} launch attempt {launch_attempt} requires prompt profile "
            f"{required!r}, not {prompt_profile!r}"
        )


def build_transport(
    *,
    rundir: Path,
    worktree: Path,
    base: str,
    pr: str,
    review_pass: str,
    launch_attempt: str,
    prompt_profile: str,
    producer: str,
    paths: "dict[str, Path]",
) -> dict:
    """Build the canonical ``ReviewTransport`` object; every dynamic value remains data."""
    return {
        "attempt": {
            "pr": int(pr),
            "pass": int(review_pass),
            "launch_attempt": int(launch_attempt),
        },
        "review_root": os.fspath(rundir),
        "worktree": os.fspath(worktree),
        "base": base,
        "prompt_profile": prompt_profile,
        "prompt_path": os.fspath(paths["prompt"]),
        "plan_path": os.fspath(paths["plan"]),
        "progress_path": os.fspath(paths["progress"]),
        "findings_path": os.fspath(paths["findings"]),
        "emit_progress_path": os.fspath((_HERE / "emit-progress.py").resolve()),
        "emit_finding_path": os.fspath((_HERE / "emit-finding.py").resolve()),
        "emit_amendment_path": os.fspath((_HERE / "emit-amendment.py").resolve()),
        "emit_report_path": os.fspath((_HERE / "emit-report.py").resolve()),
        "report": {"producer": producer, "path": os.fspath(paths["report"])},
    }


def bind_prompt(template: bytes, transport: dict, intent: bytes) -> bytes:
    """Bind both data slots once without scanning or rewriting inserted bytes."""
    slots = SLOT_RE.findall(template)
    expected = [TRANSPORT_SLOT, INTENT_SLOT]
    if slots != expected:
        refuse(
            "review prompt template must contain exactly <TRANSPORT-RECORD> then <INTENT>, once each; "
            f"found {[os.fsdecode(slot) for slot in slots]}"
        )
    before_record, tail = template.split(TRANSPORT_SLOT, 1)
    between, after_intent = tail.split(INTENT_SLOT, 1)
    try:
        encoded = json.dumps(
            transport,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        refuse(
            "ReviewTransport text must be valid UTF-8; a filesystem path contains "
            f"non-UTF-8 bytes ({exc})"
        )
    prompt_profile = transport.get("prompt_profile")
    if prompt_profile == "standard":
        preamble = b""
    elif prompt_profile == "codex-recovery":
        preamble = CODEX_RECOVERY_PREAMBLE
    else:
        refuse(
            f"ReviewTransport prompt_profile must be one of {list(PROMPT_PROFILES)}, "
            f"got {prompt_profile!r}"
        )
    return preamble + before_record + encoded + between + intent + after_intent


def identity_bytes(
    progress: Path,
    *,
    pr: str,
    review_pass: str,
    launch_attempt: str,
    head_sha: str,
    dispatched_at: str,
    default_non_goals: "list[str]",
) -> bytes:
    """Build bytes accepted by the review-pass schema owner's read door.

    ``default_non_goals`` is the run's current default Non-goals — the DISPATCH-TIME scope this pass's
    verdict is measured against at tally. It is bound into the immutable ``pass_identity`` here, never
    inferred later from the mutable ``intent-<pr>.md`` (which per-heartbeat re-adoption re-syncs to the
    header before the tally). ``verify --ledger`` compares this binding to the run's live defaults.
    """
    record: "dict[str, object]" = {
        "type": RP.IDENTITY,
        "pr": pr,
        "pass": review_pass,
        "head_sha": head_sha,
        "launch_attempt": launch_attempt,
        "dispatched_at": dispatched_at,
        "default_non_goals": default_non_goals,
    }
    try:
        RP.check_event(record, "review-dispatch pass_identity")
        RP.check_identity_shape(record, "review-dispatch pass_identity")
        text = json.dumps(record, separators=(",", ":")) + "\n"
        RP.check_progress_file(text, progress, dict)
    except RP.Defect as exc:
        refuse(str(exc))
    return text.encode("utf-8")


def _stage_bytes(path: Path, content: bytes) -> Path:
    fd, raw = tempfile.mkstemp(dir=os.fspath(path.parent), prefix=".review-dispatch-", suffix=".tmp")
    staged = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def install_pair(
    prompt_path: Path,
    prompt: bytes,
    progress_path: Path,
    identity: bytes,
    *,
    link: Callable = os.link,
) -> None:
    """Install prompt then identity with no overwrite and rollback on a controlled failure.

    ``pass_identity`` is the launch evidence, so it is linked last. Residue left by an abrupt process or
    machine stop — the prompt alone, the identity alone, or both without a reported success — is inert, and
    the next matching prepare recovers it (``recover_inert_prompt``); a returned success always has both
    files. Any failure or interruption in this call rolls back every destination it may have created: each
    path is registered for cleanup **before** its ``link``, so an interruption landing in the window between
    a ``link`` syscall returning and its own bookkeeping cannot strand a linked file.
    """
    staged_prompt: "Path | None" = None
    staged_identity: "Path | None" = None
    installed: list[Path] = []
    try:
        staged_prompt = _stage_bytes(prompt_path, prompt)
        staged_identity = _stage_bytes(progress_path, identity)
        installed.append(prompt_path)
        link(staged_prompt, prompt_path)
        installed.append(progress_path)
        link(staged_identity, progress_path)
        staged_prompt.unlink()
        staged_prompt = None
        staged_identity.unlink()
        staged_identity = None
    except BaseException:
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        if staged_prompt is not None:
            staged_prompt.unlink(missing_ok=True)
        if staged_identity is not None:
            staged_identity.unlink(missing_ok=True)
        raise


def _identity_only(progress: Path) -> bool:
    """True iff ``progress`` is exactly this attempt's lone, WELL-FORMED ``pass_identity`` line.

    ``prepare`` launches no reviewer, so until it returns the progress file holds only the single identity
    line and the reviewer has written nothing. One well-formed ``pass_identity`` for this attempt and no
    further line is therefore inert residue, not reviewer output; any extra line means the reviewer ran.
    The identity's ``dispatched_at``/``head_sha`` may differ from the current invocation (the stranded line
    came from the interrupted run), so no ``head_sha`` is pinned — a stale-but-well-formed identity is still
    reclaimed.

    The lone line is validated through the read door's OWN whole-file schema, ``RP.check_progress_file``
    (no ``head_sha``) — the symmetric partner of the write door's ``identity_bytes`` (which validates the
    same way before it is ever allowed to link an identity). That single call covers exact keys, duplicate-
    key rejection, ``head_sha``/``dispatched_at`` shape and realness, and agreement between the record and
    the attempt its filename names, so the schema is never re-stated here. Only the tool writes identities
    and it validates fully first, so a MALFORMED lone identity is never the tool's own residue: any schema
    defect returns ``False`` and the normal conflict check refuses, rather than unlinking a foreign writer's
    evidence.
    """
    try:
        text = progress.read_text(encoding="utf-8")
    except OSError as exc:
        refuse(f"cannot recover interrupted preparation at {progress}: {exc}")
    if len(text.splitlines()) != 1:
        return False
    try:
        RP.check_progress_file(text, progress, dict)
    except RP.Defect:
        return False
    return True


def recover_inert_prompt(
    paths: "dict[str, Path]",
    expected_prompt: bytes,
) -> None:
    """Reclaim any residue of a preparation that never launched a reviewer.

    A reviewer starts only after ``prepare`` returns, so until then no findings or report exist and the
    progress file holds at most the orchestrator's single ``pass_identity`` line. Every abrupt-stop shape
    that leaves no reviewer output — the prompt alone, the identity alone, or both present but never
    reported as success — carries only this invocation's own inert bytes: a prompt whose bytes match, and a
    progress file that is exactly this attempt's lone, WELL-FORMED identity line (``_identity_only`` proves
    it against the read door's schema, so a malformed lone identity is NOT reclaimed). Reclaim (unlink)
    whichever is present so the same-attempt prepare rebuilds the complete pair. Every other
    existing-artifact state — a findings file, a report, a malformed identity, or any extra progress line —
    is not this invocation's inert residue and is left for the normal conflict check to refuse.
    """
    prompt = paths["prompt"]
    progress = paths["progress"]

    def present(path: Path) -> bool:
        return os.path.lexists(os.fspath(path))

    # Any reviewer-owned output proves a reviewer ran: never inert, never reclaimed.
    if present(paths["findings"]) or present(paths["report"]):
        return
    prompt_present = present(prompt)
    progress_present = present(progress)
    if not prompt_present and not progress_present:
        return

    # Every present artifact must be this invocation's inert residue; one foreign artifact refuses all.
    if prompt_present:
        if prompt.is_symlink() or not prompt.is_file():
            return
        try:
            if prompt.read_bytes() != expected_prompt:
                return
        except OSError as exc:
            refuse(f"cannot recover interrupted preparation at {prompt}: {exc}")
    if progress_present:
        if progress.is_symlink() or not progress.is_file():
            return
        if not _identity_only(progress):
            return

    try:
        if prompt_present:
            prompt.unlink()
        if progress_present:
            progress.unlink()
    except OSError as exc:
        refuse(f"cannot recover interrupted preparation: {exc}")


def prepare(args) -> dict:
    rundir = Path(args.run_dir)
    worktree = Path(args.worktree)
    intent_path = Path(args.intent_file)
    _absolute_directory(rundir, "run-dir")
    _absolute_directory(worktree, "worktree")
    _reject_overlapping_dirs(rundir, worktree)

    _validate_id("pr", args.pr)
    _validate_id("pass", args.review_pass)
    _validate_id("launch_attempt", args.launch_attempt)
    _validate_id("head_sha", args.head_sha)
    if args.allocation_purpose not in ALLOCATION_PURPOSES:
        refuse(f"--allocation-purpose must be one of {list(ALLOCATION_PURPOSES)}, "
               f"got {args.allocation_purpose!r}")
    if not args.base.strip():
        refuse("--base must be non-blank text")

    # The DISPATCH-TIME scope binding: the run's current default Non-goals, canonicalized through the
    # ledger's ONE validator. It is bound into the immutable pass_identity so the tally measures this
    # pass's verdict against the scope it was dispatched under, never the mutable intent block that
    # re-adoption re-syncs before the tally (stage-2-review-gate.md, "Does this pass COUNT?").
    try:
        default_non_goals = L.parse_default_non_goals(args.default_non_goals)
    except ValueError as exc:
        refuse(f"--default-non-goals {args.default_non_goals!r} is not a canonical JSON array of run-default "
               f"Non-goals ({exc}) — pass the run header's `default_non_goals` value verbatim, `[]` when none")

    # The base rides the typed transport as DATA (the reviewer diffs `origin/<base>...HEAD`). When a ledger
    # is supplied, that data is an ASSERTION against the row's source of truth: the row OWNS the base, so
    # `--base` must agree with the selected row's `effective_base` (its explicit `base_branch`, else the
    # legacy header fallback, through `ledger.py`'s accessor — never a second copy of that rule). Agreement
    # is decided by `ledger.py`'s `base_agrees` — the one owner of that comparison. Absent `--file`,
    # `--base` is carried as-is, as before.
    operational_base = args.base
    if args.file is not None:
        try:
            header, rows = L.load(Path(args.file))
        except SystemExit as exc:
            refuse(f"could not read ledger {args.file}: {exc}")
        row = L.find_row(rows, str(args.pr))
        if row is None:
            refuse(f"no ledger row for pr {args.pr} — its base cannot be resolved")
        effective_base, base_problem = L.require_effective_base(header, row, str(args.pr))
        if base_problem is not None:
            refuse(base_problem)
        if not L.base_agrees(args.base, effective_base):
            refuse(f"--base {args.base!r} disagrees with pr {args.pr}'s ledger effective base "
                   f"{effective_base!r} — --base is an assertion, not a base source")
        # The transport carries the ROW's resolved base, never the raw `--base` spelling: two spellings
        # `base_agrees` accepts (`main` vs `origin/main`) make the reviewer diff `origin/<base>...HEAD`
        # against different refs, so the transport must follow the row, not the caller's argument.
        operational_base = effective_base
        # …and the SAME assertion, one field over: `--default-non-goals` is bound into the pass_identity, and
        # when a ledger is supplied that binding must agree with the header's LIVE `default_non_goals` — the
        # header OWNS the scope, `--default-non-goals` only asserts it. A defense-in-depth mirror of the base
        # check: the pre-dispatch `intent-check` door already fences the intent block, this fences the bound
        # value against the same header so a stale `--default-non-goals` cannot bind a scope the run left.
        try:
            live_default_non_goals = L.default_non_goals(header)
        except ValueError as exc:
            refuse(f"ledger {args.file} header `default_non_goals` is malformed ({exc}) — the run's scope "
                   f"cannot be read, so the dispatched binding cannot be checked against it")
        if default_non_goals != live_default_non_goals:
            refuse(f"--default-non-goals {default_non_goals!r} disagrees with pr {args.pr}'s ledger header "
                   f"default_non_goals {live_default_non_goals!r} — --default-non-goals is an assertion of "
                   f"the run's current scope, not a scope source. Pass the header's value verbatim")

    if args.route not in ROUTE_PRODUCERS:
        refuse(f"unknown review route {args.route!r}; expected one of {list(ROUTE_PRODUCERS)}")
    validate_prompt_profile(args.route, args.launch_attempt, args.prompt_profile)
    if args.report_producer not in REPORT_PRODUCERS:
        refuse(f"unknown report producer {args.report_producer!r}; expected one of {list(REPORT_PRODUCERS)}")
    required_producer = ROUTE_PRODUCERS[args.route]
    if args.report_producer != required_producer:
        refuse(
            f"route {args.route!r} requires report producer {required_producer!r}, "
            f"not {args.report_producer!r}"
        )

    paths = attempt_paths(rundir, args.pr, args.review_pass, args.launch_attempt)
    allocation_journal, allocation_text, _, allocations, allocation_results = load_allocations(
        rundir, args.pr, args.review_pass
    )
    _ensure_allocation_available(allocations, allocation_results, args.allocation_purpose)
    _ensure_next_launch_attempt(allocations, args.launch_attempt)
    if not intent_path.is_absolute():
        refuse(f"--intent-file must be an absolute path, got {intent_path}")
    if intent_path != paths["intent"]:
        refuse(
            f"--intent-file must be this PR's derived artifact {paths['intent']}, got {intent_path}"
        )

    try:
        RP.load_plan(paths["plan"])
        RP.load_intent(intent_path)
        intent = intent_path.read_bytes()
    except (RP.Defect, OSError) as exc:
        refuse(str(exc))
    require_contiguous_history(rundir, args.pr, args.review_pass)

    try:
        template = TEMPLATE.read_bytes()
    except OSError as exc:
        refuse(f"cannot read bundled review prompt template at {TEMPLATE}: {exc}")

    transport = build_transport(
        rundir=rundir,
        worktree=worktree,
        base=operational_base,
        pr=args.pr,
        review_pass=args.review_pass,
        launch_attempt=args.launch_attempt,
        prompt_profile=args.prompt_profile,
        producer=args.report_producer,
        paths=paths,
    )
    for field in ("emit_progress_path", "emit_finding_path", "emit_amendment_path", "emit_report_path"):
        emitter = Path(transport[field])
        if not emitter.is_file():
            refuse(f"bundled emitter for {field} is missing at {emitter}")
    prompt = bind_prompt(template, transport, intent)
    identity = identity_bytes(
        paths["progress"],
        pr=args.pr,
        review_pass=args.review_pass,
        launch_attempt=args.launch_attempt,
        head_sha=args.head_sha,
        dispatched_at=args.dispatched_at,
        default_non_goals=default_non_goals,
    )
    recover_inert_prompt(paths, prompt)
    conflicts = [
        paths[name]
        for name in ("prompt", "progress", "findings", "report")
        if os.path.lexists(os.fspath(paths[name]))
    ]
    if conflicts:
        refuse(
            "launch attempt artifacts must all be fresh; already present: "
            + ", ".join(os.fspath(path) for path in conflicts)
        )
    try:
        install_pair(paths["prompt"], prompt, paths["progress"], identity)
    except OSError as exc:
        refuse(f"could not atomically prepare the launch attempt: {exc}")

    allocation = {
        "type": ALLOCATION,
        "pr": args.pr,
        "pass": args.review_pass,
        "launch_attempt": args.launch_attempt,
        "head_sha": args.head_sha,
        "dispatched_at": args.dispatched_at,
        "purpose": args.allocation_purpose,
    }
    try:
        _append_allocation_record(allocation_journal, allocation_text, allocation)
    except Refusal:
        # The caller never received a launch record, so these are still the inert pair `prepare` owns.
        # Remove them rather than strand a reviewer artifact with no durable allocation account.
        paths["prompt"].unlink(missing_ok=True)
        paths["progress"].unlink(missing_ok=True)
        raise

    return {"route": args.route, "transport": transport}


def self_test() -> int:
    """Run every fixture in the sibling suite on the shared runner (`_gauntlet/testing.py`)."""
    return run_sibling_suite(SIBLING, "review_dispatch_test", failure=SelfTestFailure,
                             subject="the review-dispatch contract", width=34)


def build_parser() -> argparse.ArgumentParser:
    # `__doc__` is this module's docstring, which is present; the guard states that for the checker and
    # would name the impossible case loudly rather than crashing on None.
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    command = sub.add_parser("prepare", help="materialize one fresh review launch attempt")
    command.add_argument("--run-dir", required=True, help="absolute active run-artifact directory")
    command.add_argument("--pr", required=True, help="positive decimal PR number")
    command.add_argument("--pass", dest="review_pass", required=True, help="positive decimal review pass")
    command.add_argument("--launch-attempt", required=True, help="positive decimal launch attempt")
    command.add_argument("--allocation-purpose", required=True, choices=ALLOCATION_PURPOSES,
                         help="initial/recovery/final allocation recorded in this pass's durable journal")
    command.add_argument("--worktree", required=True, help="absolute candidate worktree directory")
    command.add_argument("--base", required=True, help="base branch text carried as data; with --file it is "
                                                        "asserted against the row's effective base")
    command.add_argument("--file", help="OPTIONAL ledger (state.jsonl); when given, --base is asserted "
                                        "against the selected --pr row's effective base")
    command.add_argument("--route", required=True, choices=tuple(ROUTE_PRODUCERS),
                         help="route already selected by the host adapter")
    command.add_argument("--prompt-profile", required=True, choices=PROMPT_PROFILES,
                         help="typed prompt framing selected by the review preparation mapping")
    command.add_argument("--report-producer", required=True, choices=REPORT_PRODUCERS,
                         help="sole report producer; must match the selected route")
    command.add_argument("--head-sha", required=True, help="40-character lowercase review head SHA")
    command.add_argument("--dispatched-at", required=True, help="UTC timestamp YYYY-MM-DDThh:mm:ssZ")
    command.add_argument("--default-non-goals", required=True,
                         help="the run header's `default_non_goals` value (a canonical JSON array, `[]` when "
                              "the run declares none) — the immutable scope this pass's verdict is measured "
                              "against at tally")
    command.add_argument("--intent-file", required=True, help="absolute path to the derived intent-<pr>.md")

    result = sub.add_parser("result", help="durably record one completed launch allocation's outcome")
    result.add_argument("--run-dir", required=True, help="absolute active run-artifact directory")
    result.add_argument("--pr", required=True, help="positive decimal PR number")
    result.add_argument("--pass", dest="review_pass", required=True, help="positive decimal review pass")
    result.add_argument("--launch-attempt", required=True, help="the allocated launch attempt to settle")
    result.add_argument("--result", required=True, choices=ALLOCATION_RESULTS,
                        help="the transport or review result that determines whether final remains reserved")

    status = sub.add_parser("allocation-status", help="render one pass's durable allocation purpose/result history")
    status.add_argument("--run-dir", required=True, help="absolute active run-artifact directory")
    status.add_argument("--pr", required=True, help="positive decimal PR number")
    status.add_argument("--pass", dest="review_pass", required=True, help="positive decimal review pass")
    sub.add_parser("self-test", help="run every sibling fixture")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "allocation-status":
        try:
            rundir = Path(args.run_dir)
            _absolute_directory(rundir, "run-dir")
            _validate_id("pr", args.pr)
            _validate_id("pass", args.review_pass)
            journal, _, _, allocations, results = load_allocations(rundir, args.pr, args.review_pass)
            print(json.dumps(allocation_summary(allocations, results, journal_present=journal.is_file()),
                             separators=(",", ":")))
        except Refusal as exc:
            print(f"review-dispatch: REFUSED — {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "result":
        try:
            print(json.dumps(record_result(args), separators=(",", ":")))
        except Refusal as exc:
            print(f"review-dispatch: REFUSED — {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        payload = prepare(args)
    except Refusal as exc:
        print(f"review-dispatch: REFUSED — {exc}", file=sys.stderr)
        return 1
    # Deliver the canonical result as UTF-8 bytes so a valid Unicode worktree path cannot die in the
    # text layer of an ASCII-configured stdout. This makes the OUTPUT side symmetric with bind_prompt's
    # already-guarded INPUT side (a non-UTF-8 path is a controlled Refusal before any install). A
    # remaining delivery OSError (e.g. a closed read end) maps to the same controlled refusal path.
    # No rollback of the installed prompt/pass_identity pair is needed on a failed delivery: the driver
    # allocates launch_attempt monotonically and never reuses a failed attempt's artifacts
    # (runtime-adapter.md, "Review preparation mapping"), so the next attempt supersedes this one.
    record = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    buffer = getattr(sys.stdout, "buffer", None)
    try:
        if buffer is not None:
            buffer.write(record.encode("utf-8") + b"\n")
            buffer.flush()
        else:  # an in-process text capture (no byte buffer); encoding limits do not apply there
            sys.stdout.write(record + "\n")
            sys.stdout.flush()
    except OSError as exc:
        # The process had already installed its prompt/identity pair and allocation record, but the host
        # received no transport record and therefore cannot launch it.  Settle that exact reservation here
        # so the next fresh attempt cannot be blocked behind a delivery failure or consume a final review.
        try:
            record_result(type("DeliveryFailure", (), {
                "run_dir": args.run_dir,
                "pr": args.pr,
                "review_pass": args.review_pass,
                "launch_attempt": args.launch_attempt,
                "result": TRANSPORT_FAILURE,
            })())
        except Refusal:
            pass
        print(f"review-dispatch: REFUSED — could not deliver the prepared result: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
