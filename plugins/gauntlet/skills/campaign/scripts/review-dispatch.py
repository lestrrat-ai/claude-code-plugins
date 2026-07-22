#!/usr/bin/env python3
"""Prepare one review launch attempt's identity, prompt, and typed transport record.

This command is the executable boundary between campaign policy and host dispatch. The host first chooses
an available route through ``runtime-adapter.md``. ``prepare`` then validates that route's report owner,
derives every attempt-scoped path from one identity, validates the existing plan and intent through
``review-pass.py``, creates the progress identity and bound prompt, and prints the canonical JSON record
the host launches. It does not select a route, test route availability, or launch a reviewer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, NoReturn

from _gauntlet.modules import load_module_from_path


_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "review-dispatch-test.py"
TEMPLATE = _HERE / "review-prompt.txt"
REVIEW_PASS = _HERE / "review-pass.py"
LEDGER = _HERE / "ledger.py"

TRANSPORT_SLOT = b"<TRANSPORT-RECORD>"
INTENT_SLOT = b"<INTENT>"
SLOT_RE = re.compile(rb"<[A-Z][A-Z0-9-]*>")

ROUTE_PRODUCERS = {
    "native": "native-worker-write",
    "external-codex": "external-process-capture",
    "external-claude": "external-process-capture",
}
REPORT_PRODUCERS = tuple(sorted(set(ROUTE_PRODUCERS.values())))


def _load_review_pass():
    mod = load_module_from_path("review_dispatch_review_pass", REVIEW_PASS)
    if mod is None:
        raise RuntimeError(f"cannot load the review-pass schema owner at {REVIEW_PASS}")
    return mod


RP = _load_review_pass()


def _load_ledger():
    mod = load_module_from_path("review_dispatch_ledger", LEDGER)
    if mod is None:
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER}")
    return mod


L = _load_ledger()


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
        "report": rundir / f"{attempt_stem}.txt",
        "intent": rundir / f"intent-{pr}.md",
    }


def build_transport(
    *,
    rundir: Path,
    worktree: Path,
    base: str,
    pr: str,
    review_pass: str,
    launch_attempt: str,
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
        "prompt_path": os.fspath(paths["prompt"]),
        "plan_path": os.fspath(paths["plan"]),
        "progress_path": os.fspath(paths["progress"]),
        "findings_path": os.fspath(paths["findings"]),
        "emit_progress_path": os.fspath((_HERE / "emit-progress.py").resolve()),
        "emit_finding_path": os.fspath((_HERE / "emit-finding.py").resolve()),
        "emit_amendment_path": os.fspath((_HERE / "emit-amendment.py").resolve()),
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
    return before_record + encoded + between + intent + after_intent


def identity_bytes(
    progress: Path,
    *,
    pr: str,
    review_pass: str,
    launch_attempt: str,
    head_sha: str,
    dispatched_at: str,
) -> bytes:
    """Build bytes accepted by the review-pass schema owner's read door."""
    record: "dict[str, object]" = {
        "type": RP.IDENTITY,
        "pr": pr,
        "pass": review_pass,
        "head_sha": head_sha,
        "launch_attempt": launch_attempt,
        "dispatched_at": dispatched_at,
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
    if not args.base.strip():
        refuse("--base must be non-blank text")

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

    if args.route not in ROUTE_PRODUCERS:
        refuse(f"unknown review route {args.route!r}; expected one of {list(ROUTE_PRODUCERS)}")
    if args.report_producer not in REPORT_PRODUCERS:
        refuse(f"unknown report producer {args.report_producer!r}; expected one of {list(REPORT_PRODUCERS)}")
    required_producer = ROUTE_PRODUCERS[args.route]
    if args.report_producer != required_producer:
        refuse(
            f"route {args.route!r} requires report producer {required_producer!r}, "
            f"not {args.report_producer!r}"
        )

    paths = attempt_paths(rundir, args.pr, args.review_pass, args.launch_attempt)
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
        producer=args.report_producer,
        paths=paths,
    )
    for field in ("emit_progress_path", "emit_finding_path", "emit_amendment_path"):
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

    return {"route": args.route, "transport": transport}


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — the review-dispatch tool has no runnable contract"
        )
    mod = load_module_from_path("review_dispatch_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — the suite cannot report health")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     sibling-fixtures -> {exc}")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:34} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:34} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:34} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the review-dispatch contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the review-dispatch contract is intact.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    command = sub.add_parser("prepare", help="materialize one fresh review launch attempt")
    command.add_argument("--run-dir", required=True, help="absolute active run-artifact directory")
    command.add_argument("--pr", required=True, help="positive decimal PR number")
    command.add_argument("--pass", dest="review_pass", required=True, help="positive decimal review pass")
    command.add_argument("--launch-attempt", required=True, help="positive decimal launch attempt")
    command.add_argument("--worktree", required=True, help="absolute candidate worktree directory")
    command.add_argument("--base", required=True, help="base branch text carried as data; with --file it is "
                                                        "asserted against the row's effective base")
    command.add_argument("--file", help="OPTIONAL ledger (state.jsonl); when given, --base is asserted "
                                        "against the selected --pr row's effective base")
    command.add_argument("--route", required=True, choices=tuple(ROUTE_PRODUCERS),
                         help="route already selected by the host adapter")
    command.add_argument("--report-producer", required=True, choices=REPORT_PRODUCERS,
                         help="sole report producer; must match the selected route")
    command.add_argument("--head-sha", required=True, help="40-character lowercase review head SHA")
    command.add_argument("--dispatched-at", required=True, help="UTC timestamp YYYY-MM-DDThh:mm:ssZ")
    command.add_argument("--intent-file", required=True, help="absolute path to the derived intent-<pr>.md")
    sub.add_parser("self-test", help="run every sibling fixture")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
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
        print(f"review-dispatch: REFUSED — could not deliver the prepared result: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
