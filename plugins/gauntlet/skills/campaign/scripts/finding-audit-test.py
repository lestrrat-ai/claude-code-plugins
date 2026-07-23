#!/usr/bin/env python3
# ci: pyright
"""Fixtures for ``finding-audit.py``'s complete-audit and fix-scope contract."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli


HERE = Path(__file__).resolve().parent
OWNER = HERE / "finding-audit.py"


def _load_owner():
    module = load_module_from_path("finding_audit_owner_for_test", OWNER)
    if module is None:
        raise RuntimeError(f"cannot load {OWNER}")
    return module


A = _load_owner()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


PURPOSE = "Keep the campaign fix scope bound to audited gating findings."


def finding(number: int, *, gating: bool = True, **over) -> dict:
    row = {
        "type": "finding",
        "file": f"scripts/tool-{number}.py",
        "line": str(number),
        "writer": "repo-content" if gating else "driver-only",
        "purpose": PURPOSE if gating else "-",
        "repro": f"run fixture {number}",
        "fix": f"repair fixture {number}",
    }
    row.update(over)
    return row


def write_intent(directory: Path) -> None:
    (directory / "intent-41.md").write_text(
        f"## Purpose\n\n- {PURPOSE}\n\n## Non-goals\n\n- Unrelated cleanup.\n\n"
        "## Threat model\n\n- Repository content can exercise the changed parser.\n",
        encoding="utf-8",
    )


def rewrite_intent_dropping_purpose(directory: Path) -> None:
    """A sanctioned REPAIR-INTENT re-authors intent-<pr>.md, dropping the purpose the landed round's
    findings anchored to. The source findings file is left untouched, so only the intent has changed."""
    (directory / "intent-41.md").write_text(
        "## Purpose\n\n- A different purpose authored by a later repair-intent.\n\n"
        "## Non-goals\n\n- Unrelated cleanup.\n\n"
        "## Threat model\n\n- Repository content can exercise the changed parser.\n",
        encoding="utf-8",
    )


def write_source(directory: Path, rows: list[dict], name: str = "review-41-1.findings.jsonl") -> Path:
    write_intent(directory)
    path = directory / name
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return path


def progress_name(attempt: int = 1, *, pr: str = "41", npass: str = "1") -> str:
    stem = f"review-{pr}-{npass}"
    return f"{stem}.progress.jsonl" if attempt == 1 else f"{stem}.a{attempt}.progress.jsonl"


def write_progress(directory: Path, attempt: int = 1, *, pr: str = "41", npass: str = "1") -> Path:
    """A valid single-line `pass_identity` progress file for the active launch attempt.

    `init` now binds to this artifact and derives its findings path exactly as `review-pass.py` does, so
    every fixture that initializes an audit first writes the attempt's progress file.
    """
    identity = {
        "type": "pass_identity",
        "pr": pr,
        "pass": npass,
        "head_sha": "0" * 40,
        "launch_attempt": str(attempt),
        "dispatched_at": "2026-01-01T00:00:00Z",
        "default_non_goals": [],
    }
    path = directory / progress_name(attempt, pr=pr, npass=npass)
    path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
    return path


def invoke(argv: list[str]):
    return capture_cli(A.main, argv)


def initialize(directory: Path, rows: list[dict], *, source_name: str = "review-41-1.findings.jsonl",
               attempt: int = 1):
    source = write_source(directory, rows, source_name)
    progress = write_progress(directory, attempt)
    audit = directory / "audit-41-1.jsonl"
    code, out, err = invoke(["init", "--file", str(audit), "--progress", str(progress)])
    check(code == 0, f"init failed: {err}")
    return source, audit, json.loads(out)


def record(audit: Path, finding_id: str, verdict: str, evidence: str = "verified by focused fixture",
           extra: list[str] | None = None):
    argv = [
        "record", "--file", str(audit), "--finding-id", finding_id,
        "--verdict", verdict, "--evidence", evidence,
    ]
    if extra:
        argv += extra
    return invoke(argv)


def fix_list(audit: Path) -> "tuple[int, dict, str]":
    # `fix-list --json` always prints one JSON object on success (code 0); stdout is empty only when the
    # command failed, and every caller guards `code == 0` before reading the payload. Return `{}` rather
    # than `None` for that failure path so the payload is never Optional at a subscript.
    code, out, err = invoke(["fix-list", "--file", str(audit), "--json"])
    return code, json.loads(out) if out.strip() else {}, err


def t_complete_audit_derives_only_confirmed_and_adjusted_fixes() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        rows = [finding(11), finding(12), finding(13), finding(14, gating=False)]
        _source, audit, summary = initialize(directory, rows)
        ids = [item["finding_id"] for item in summary["gating"]]
        check(len(ids) == 3, f"init must expose exactly three gating subjects: {summary}")

        check(record(audit, ids[0], "CONFIRMED")[0] == 0, "CONFIRMED must record")
        adjusted = record(
            audit, ids[1], "ADJUSTED", "the failure is narrower than reported",
            ["--adjusted-repro", "run the narrowed trigger", "--adjusted-fix", "repair the narrow cause"],
        )
        check(adjusted[0] == 0, f"ADJUSTED with replacement details must record: {adjusted[2]}")
        check(record(audit, ids[2], "REFUTED", "the named call path cannot reach this branch")[0] == 0,
              "REFUTED must record")

        code, out, err = invoke(["verify", "--file", str(audit)])
        check(code == 0, f"complete audit must verify: {err}")
        verified = json.loads(out)
        check((verified["confirmed"], verified["adjusted"], verified["refuted"]) == (1, 1, 1),
              f"verify counts every result: {verified}")
        check([item["finding_id"] for item in verified["results"]] == ids,
              "verify returns the complete audit in source order for later readers")

        code, payload, err = fix_list(audit)
        check(code == 0, f"fix-list failed: {err}")
        fixes = payload["fixes"]
        check([item["finding_id"] for item in fixes] == ids[:2],
              f"fix-list must include CONFIRMED + ADJUSTED only: {fixes}")
        check(fixes[1]["repro"] == "run the narrowed trigger", "ADJUSTED replaces the trigger")
        check(fixes[1]["fix"] == "repair the narrow cause", "ADJUSTED replaces the fix")
        check(ids[2] not in json.dumps(fixes), "REFUTED finding must not leak into review-fix scope")
        check([item["finding_id"] for item in payload["refutations"]] == [ids[2]],
              "the separate refutation-comment scope names the REFUTED result")


def t_missing_result_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as name:
        _source, audit, summary = initialize(Path(name), [finding(11), finding(12)])
        first = summary["gating"][0]["finding_id"]
        check(record(audit, first, "CONFIRMED")[0] == 0, "first result records")
        for argv in (["verify", "--file", str(audit)],
                     ["fix-list", "--file", str(audit), "--json"]):
            code, out, err = invoke(list(argv))
            check(code == 1 and not out.strip() and "incomplete" in err,
                  f"missing audit coverage must fail closed: code={code}, err={err!r}")


def t_duplicate_and_unknown_results_are_refused_on_read() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "CONFIRMED")[0] == 0, "result records")
        original = audit.read_text(encoding="utf-8")

        duplicate = json.loads(original.splitlines()[1])
        audit.write_text(original + json.dumps(duplicate) + "\n", encoding="utf-8")
        code, _out, err = invoke(["verify", "--file", str(audit)])
        check(code == 1 and "duplicate audit result" in err, f"duplicate must fail: {err}")

        bad = dict(duplicate, verdict="MAYBE")
        audit.write_text(original.splitlines()[0] + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
        code, _out, err = invoke(["verify", "--file", str(audit)])
        check(code == 1 and "unknown" in err, f"unknown verdict must fail: {err}")


def t_non_gating_and_unknown_findings_cannot_be_audited() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        rows = [finding(11), finding(12, gating=False)]
        _source, audit, summary = initialize(directory, rows)
        indexed = A.enumerate_findings(rows)
        non_gating_id = indexed[1]["finding_id"]
        code, _out, err = record(audit, non_gating_id, "CONFIRMED")
        check(code == 1 and "non-gating" in err, f"non-gating audit must be refused: {err}")
        code, _out, err = record(audit, "f-unknown-1", "CONFIRMED")
        check(code == 1 and "no source finding" in err, f"unknown finding must be refused: {err}")
        check(summary["gating"] == [{
            "finding_id": indexed[0]["finding_id"],
            "finding": rows[0],
        }], "init output must not assign audit work to non-gating findings")


def t_source_change_makes_audit_stale() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "CONFIRMED")[0] == 0, "result records")
        source.write_text(json.dumps(finding(11, fix="different fix")) + "\n", encoding="utf-8")
        code, _out, err = invoke(["verify", "--file", str(audit)])
        check(code == 1 and "stale" in err, f"changed source finding must stale the audit: {err}")


def t_adjusted_details_and_evidence_are_required() -> None:
    with tempfile.TemporaryDirectory() as name:
        _source, audit, summary = initialize(Path(name), [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        code, _out, err = record(audit, fid, "ADJUSTED")
        check(code == 1 and "adjusted_repro" in err, f"ADJUSTED without replacements must fail: {err}")
        code, _out, err = record(audit, fid, "CONFIRMED", "   ")
        check(code == 1 and "evidence" in err, f"blank evidence must fail: {err}")
        code, _out, err = record(audit, fid, "CONFIRMED", extra=["--adjusted-fix", "not allowed"])
        check(code == 1 and "only with" in err, f"replacement on CONFIRMED must fail: {err}")
        check(audit.read_text(encoding="utf-8").count("\n") == 1,
              "every refused result must leave the header-only audit unchanged")


def t_standoff_ruling_is_durable_once_and_controls_fix_scope() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(10), finding(11)])
        prior_id, fid = [item["finding_id"] for item in summary["gating"]]
        check(record(audit, prior_id, "CONFIRMED", "fixed before the fresh review")[0] == 0,
              "the original round's confirmed item records")
        check(record(audit, fid, "REFUTED", "verified impossible call chain")[0] == 0, "refutation records")
        args = [
            "rule-standoff", "--file", str(audit), "--finding-id", fid,
            "--ruling", "valid", "--counter", "fresh reviewer reached the branch",
            "--evidence", "user accepted the fresh reproduction",
        ]
        code, _out, err = invoke(args)
        check(code == 0, f"valid standoff ruling records: {err}")
        code, payload, err = fix_list(audit)
        check(code == 0 and len(payload["fixes"]) == 1
              and payload["fixes"][0]["finding_id"] == fid
              and payload["fixes"][0]["disposition"] == "standoff-valid",
              f"valid standoff returns the finding to fix scope: {err}, {payload}")
        check(prior_id not in json.dumps(payload["fixes"]),
              "standoff scope must not replay CONFIRMED work fixed before the fresh review")
        check(payload["fixes"][0]["standoff_counter"] == "fresh reviewer reached the branch"
              and payload["fixes"][0]["standoff_evidence"] == "user accepted the fresh reproduction",
              "standoff fix scope carries the counter and user's ruling evidence")
        before = audit.read_text(encoding="utf-8")
        code, _out, err = invoke(args)
        check(code == 1 and "recorded once" in err, f"second ruling must be refused: {err}")
        check(audit.read_text(encoding="utf-8") == before, "second ruling must not rewrite the durable answer")

    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "REFUTED", "verified impossible call chain")[0] == 0, "refutation records")
        code, _out, err = invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", fid,
            "--ruling", "invalid", "--counter", "fresh reviewer repeated the claim",
            "--evidence", "user rejected the repeat after checking the evidence",
        ])
        check(code == 0, f"invalid ruling records: {err}")
        code, payload, err = fix_list(audit)
        check(code == 0 and payload["fixes"] == [], f"invalid ruling stays outside fix scope: {err}, {payload}")
        check(payload["refutations"] == [], "a ruled standoff must not replay the original refutation work")


def t_standoff_ruling_records_after_repair_intent_rewrites_intent() -> None:
    """A REPAIR-INTENT rewrites intent-<pr>.md BETWEEN recording REFUTED and the ruling.

    Regression for the finding that `rule-standoff` and standoff-phase `fix-list` re-anchored a landed
    round's audit to the CURRENT intent. A sanctioned REPAIR-INTENT drops the purpose the REFUTED finding
    anchored to; the re-anchoring read then rejected the historical finding (exit 1, NO standoff_ruling
    row), so the user's one durable ruling was UNRECORDABLE and the PR wedged in awaiting-user. The
    non-re-anchoring historical read fixes it while preserving the source-digest staleness guard. Without
    the fix, part (a) FAILS (the ruling is refused); with it, all three parts pass.
    """
    # (a) the ruling records DURABLY after the intent is rewritten, and (b) fix-list returns it once.
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "REFUTED", "verified impossible call chain")[0] == 0, "refutation records")
        rewrite_intent_dropping_purpose(directory)
        args = [
            "rule-standoff", "--file", str(audit), "--finding-id", fid,
            "--ruling", "valid", "--counter", "fresh reviewer reached the branch",
            "--evidence", "user accepted the fresh reproduction",
        ]
        code, _out, err = invoke(args)
        check(code == 0, f"standoff ruling must record after a repair-intent rewrite: {err}")
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        rulings = [r for r in rows if r.get("type") == "standoff_ruling"]
        check(len(rulings) == 1 and rulings[0]["finding_id"] == fid,
              f"a durable standoff_ruling row must be written: {rows}")

        code, payload, err = fix_list(audit)
        check(code == 0 and [f["finding_id"] for f in payload["fixes"]] == [fid]
              and payload["fixes"][0]["disposition"] == "standoff-valid",
              f"standoff fix-list must return the ruled fix after repair-intent: {err}, {payload}")
        code, payload, err = fix_list(audit)
        check(code == 0 and payload["fixes"] == [],
              f"the ruled standoff fix must be consumed exactly once: {err}, {payload}")

    # (c) the source-digest staleness guard SURVIVES on the historical path: a genuinely changed source
    # finding must still stale the ruling read even though the intent was rewritten.
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "REFUTED", "verified impossible call chain")[0] == 0, "refutation records")
        rewrite_intent_dropping_purpose(directory)
        source.write_text(json.dumps(finding(11, fix="a genuinely different fix")) + "\n", encoding="utf-8")
        code, _out, err = invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", fid,
            "--ruling", "valid", "--counter", "counter", "--evidence", "answer",
        ])
        check(code == 1 and "stale" in err,
              f"a changed source finding must still stale on the historical path: {err}")


def t_two_standoff_rulings_each_enter_fix_scope_once() -> None:
    """Two REFUTED findings ruled valid in SEPARATE rounds: the second scope must not replay the first.

    Regression for the finding that once any standoff ruling existed, `fix-list` re-emitted EVERY
    valid ruling on every call, so after f1's fix landed a later `fix-list` (a fresh memoryless
    heartbeat) re-dispatched it alongside the newly ruled f2.
    """
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(10), finding(11)])
        f1, f2 = [item["finding_id"] for item in summary["gating"]]
        check(record(audit, f1, "REFUTED", "verified impossible for f1")[0] == 0, "f1 refutation records")
        check(record(audit, f2, "REFUTED", "verified impossible for f2")[0] == 0, "f2 refutation records")

        # Round A: the user rules f1 valid; the fix scope is exactly f1.
        check(invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", f1,
            "--ruling", "valid", "--counter", "fresh reviewer reached f1",
            "--evidence", "user accepted the f1 reproduction",
        ])[0] == 0, "f1 valid ruling records")
        code, payload, err = fix_list(audit)
        check(code == 0 and [fix["finding_id"] for fix in payload["fixes"]] == [f1],
              f"round A fix scope must be exactly f1: {err}, {payload}")

        # f1's fix has landed. A memoryless heartbeat re-reads fix-list before the next ruling exists:
        # the already-consumed f1 must NOT come back as work.
        code, payload, err = fix_list(audit)
        check(code == 0 and payload["fixes"] == [],
              f"re-reading fix-list after consuming f1 must return no work: {err}, {payload}")

        # Round B: the user rules f2 valid; the fix scope must be ONLY f2, never replaying the landed f1.
        check(invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", f2,
            "--ruling", "valid", "--counter", "fresh reviewer reached f2",
            "--evidence", "user accepted the f2 reproduction",
        ])[0] == 0, "f2 valid ruling records")
        code, payload, err = fix_list(audit)
        check(code == 0 and [fix["finding_id"] for fix in payload["fixes"]] == [f2],
              f"round B fix scope must be exactly f2: {err}, {payload}")
        check(f1 not in json.dumps(payload["fixes"]),
              "the second standoff scope must not replay the first, already-consumed fix")


def t_standoff_requires_a_complete_refuted_audit() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11), finding(12)])
        first, second = [item["finding_id"] for item in summary["gating"]]
        check(record(audit, first, "REFUTED")[0] == 0, "refutation records")
        code, _out, err = invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", first,
            "--ruling", "valid", "--counter", "counter", "--evidence", "answer",
        ])
        check(code == 1 and second in err, f"standoff cannot bypass missing audit work: {err}")

    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "CONFIRMED")[0] == 0, "confirmation records")
        code, _out, err = invoke([
            "rule-standoff", "--file", str(audit), "--finding-id", fid,
            "--ruling", "valid", "--counter", "counter", "--evidence", "answer",
        ])
        check(code == 1 and "REFUTED" in err, f"only refuted findings can enter standoff: {err}")


def t_atomic_failure_preserves_existing_audit() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        before = audit.read_bytes()
        with mock.patch.object(A, "replace_text", side_effect=OSError("injected atomic failure")):
            code, _out, err = record(audit, fid, "CONFIRMED")
        check(code == 1 and "injected atomic failure" in err, f"write failure must be reported: {err}")
        check(audit.read_bytes() == before, "atomic write failure must preserve the previous audit bytes")
        check(not any(path.name.startswith(f".{audit.name}.") for path in directory.iterdir()),
              "atomic failure must leave no accessor temp artifact")


def t_hostile_payload_and_parent_path_round_trip_as_data() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        directory = root / "run with spaces `$()`\nand newline"
        directory.mkdir()
        _source, audit, summary = initialize(directory, [finding(11)])
        fid = summary["gating"][0]["finding_id"]
        evidence = "--leading `ticks` $(not-run) \"quotes\"\nsecond line Ω"
        code, _out, err = record(audit, fid, "CONFIRMED", evidence)
        check(code == 0, f"hostile payload must be JSON data: {err}")
        state = A.load_audit(audit, require_complete=True)
        check(state["results"][fid]["evidence"] == evidence, "hostile payload must round-trip byte content")


def t_init_refuses_wrong_names_existing_files_and_no_gating() -> None:
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        write_source(directory, [finding(11)])
        progress = write_progress(directory)
        wrong = directory / "audit-42-1.jsonl"
        code, _out, err = invoke(["init", "--file", str(wrong), "--progress", str(progress)])
        check(code == 1 and "same PR and pass" in err, f"mismatched name must fail: {err}")
        audit = directory / "audit-41-1.jsonl"
        audit.write_text("keep me\n", encoding="utf-8")
        code, _out, err = invoke(["init", "--file", str(audit), "--progress", str(progress)])
        check(code == 1 and audit.read_text(encoding="utf-8") == "keep me\n",
              "init must not overwrite an existing audit")

    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        write_source(directory, [finding(12, gating=False)])
        progress = write_progress(directory)
        audit = directory / "audit-41-1.jsonl"
        code, _out, err = invoke(["init", "--file", str(audit), "--progress", str(progress)])
        check(code == 1 and "no gating findings" in err and not audit.exists(),
              f"an all-non-gating pass has no audit to initialize: {err}")


def t_init_binds_to_active_attempt_refusing_a_superseded_one() -> None:
    """A relaunch is live (attempt 2); the DEAD attempt-1 findings must never enter the audit or fix scope.

    Regression for the finding that `init` bound an audit to a directly-passed findings path with no
    attempt binding, so it accepted attempt 1 while attempt 2 was the active pass and `fix-list` then
    returned the killed attempt's scope.
    """
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        # DEAD attempt 1 and its findings; LIVE attempt 2 with its own findings and a valid pass_identity.
        write_source(directory, [finding(11, file="scripts/dead-attempt1.py")],
                     name="review-41-1.findings.jsonl")
        dead_progress = write_progress(directory, 1)
        write_source(directory, [finding(22, file="scripts/live-attempt2.py")],
                     name="review-41-1.a2.findings.jsonl")
        live_progress = write_progress(directory, 2)
        audit = directory / "audit-41-1.jsonl"

        # Pointing init at the dead attempt is refused, and no audit is written.
        code, _out, err = invoke(["init", "--file", str(audit), "--progress", str(dead_progress)])
        check(code == 1 and "not the active launch attempt" in err and not audit.exists(),
              f"a superseded attempt must be refused fail-closed: code={code}, err={err!r}")

        # The active attempt initializes, and its findings — not the dead attempt's — drive the audit.
        code, out, err = invoke(["init", "--file", str(audit), "--progress", str(live_progress)])
        check(code == 0, f"the active attempt must initialize: {err}")
        summary = json.loads(out)
        check(summary["findings"] == "review-41-1.a2.findings.jsonl",
              f"init must derive the active attempt's findings artifact: {summary}")
        check("scripts/live-attempt2.py" in json.dumps(summary)
              and "scripts/dead-attempt1.py" not in json.dumps(summary),
              f"only the live attempt's finding may enter the audit: {summary}")
        fid = summary["gating"][0]["finding_id"]
        check(record(audit, fid, "CONFIRMED")[0] == 0, "the live finding records")
        code, payload, err = fix_list(audit)
        check(code == 0 and [fix["file"] for fix in payload["fixes"]] == ["scripts/live-attempt2.py"],
              f"fix scope must be the live attempt's, never the killed one: {err}, {payload}")


def t_duplicate_source_findings_receive_distinct_stable_ids() -> None:
    with tempfile.TemporaryDirectory() as name:
        row = finding(11)
        _source, _audit, summary = initialize(Path(name), [row, row])
        ids = [item["finding_id"] for item in summary["gating"]]
        check(len(set(ids)) == 2 and ids[0].endswith("-1") and ids[1].endswith("-2"),
              f"duplicate source rows still require two audit results: {ids}")


CASES = [
    t_complete_audit_derives_only_confirmed_and_adjusted_fixes,
    t_missing_result_fails_closed,
    t_duplicate_and_unknown_results_are_refused_on_read,
    t_non_gating_and_unknown_findings_cannot_be_audited,
    t_source_change_makes_audit_stale,
    t_adjusted_details_and_evidence_are_required,
    t_standoff_ruling_is_durable_once_and_controls_fix_scope,
    t_standoff_ruling_records_after_repair_intent_rewrites_intent,
    t_two_standoff_rulings_each_enter_fix_scope_once,
    t_standoff_requires_a_complete_refuted_audit,
    t_atomic_failure_preserves_existing_audit,
    t_hostile_payload_and_parent_path_round_trip_as_data,
    t_init_refuses_wrong_names_existing_files_and_no_gating,
    t_init_binds_to_active_attempt_refusing_a_superseded_one,
    t_duplicate_source_findings_receive_distinct_stable_ids,
]


def main() -> int:
    failures = []
    for case in CASES:
        try:
            case()
            print(f"PASS     {case.__name__}")
        except Exception as exc:  # noqa: BLE001 - show all independent fixture failures
            failures.append((case.__name__, exc))
            print(f"FAIL     {case.__name__}: {exc}")
    if failures:
        print(f"finding-audit tests: {len(failures)} failure(s)")
        return 1
    print(f"finding-audit tests: {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
