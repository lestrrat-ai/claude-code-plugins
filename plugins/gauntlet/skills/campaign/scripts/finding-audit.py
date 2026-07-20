#!/usr/bin/env python3
"""Schema-owning accessor for campaign finding audits and review-fix scope.

The reviewer finding schema and gating rule belong to ``review-pass.py``. This accessor imports that
owner, binds one audit to the exact findings artifact it read, requires one evidenced audit result for
every gating finding, and derives the only review-fix input. It never decides whether a claim is true;
the dispatched audit worker and, for a standoff, the user still make those judgments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import NoReturn

from _gauntlet.atomic import replace_text
from _gauntlet.modules import load_module_from_path


DESCRIPTION = "Record and verify one complete audit of a review pass's gating findings."
HERE = Path(__file__).resolve().parent
REVIEW_PASS = HERE / "review-pass.py"
TEST_FILE = HERE / "finding-audit-test.py"

FORMAT_VERSION = "1"
HEADER = "finding_audit"
RESULT = "audit_result"
STANDOFF = "standoff_ruling"
CONSUMED = "fix_scope"
VERDICTS = ("CONFIRMED", "ADJUSTED", "REFUTED")
RULINGS = ("valid", "invalid")

HEADER_KEYS = {
    "type", "version", "findings_file", "source_digest", "gating_ids",
}
RESULT_KEYS = {
    "type", "finding_id", "verdict", "evidence", "adjusted_repro", "adjusted_fix",
}
STANDOFF_KEYS = {
    "type", "finding_id", "ruling", "counter", "evidence",
}
CONSUMED_KEYS = {
    "type", "consumed",
}


class AuditError(Exception):
    """The requested audit operation is invalid; no file is changed."""


def fail(message: str) -> NoReturn:
    print(f"finding-audit: {message}", file=sys.stderr)
    raise SystemExit(1)


def _review_pass():
    module = load_module_from_path("finding_audit_review_pass", REVIEW_PASS)
    if module is None:  # pragma: no cover - broken installed plugin, not an input case
        raise RuntimeError(f"cannot load finding schema owner at {REVIEW_PASS}")
    return module


R = _review_pass()
COUNT = R.COUNT
ATTEMPT = R.ATTEMPT
AUDIT_NAME_RE = re.compile(rf"^audit-(?P<pr>{COUNT})-(?P<pass>{COUNT})\.jsonl\Z")
SOURCE_NAME_RE = re.compile(
    rf"^review-(?P<pr>{COUNT})-(?P<pass>{COUNT})(?:\.a(?P<attempt>{ATTEMPT}))?\.findings\.jsonl\Z"
)


def canonical(record: object) -> bytes:
    """Canonical UTF-8 bytes used for source binding and stable finding IDs."""
    return json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def source_digest(findings: list[dict]) -> str:
    return "sha256:" + hashlib.sha256(canonical(findings)).hexdigest()


def finding_digest(finding: dict) -> str:
    return hashlib.sha256(canonical(finding)).hexdigest()


def enumerate_findings(findings: list[dict]) -> list[dict]:
    """Attach stable, content-derived IDs while keeping duplicate records distinct."""
    seen: Counter[str] = Counter()
    out = []
    for finding in findings:
        digest = finding_digest(finding)
        seen[digest] += 1
        out.append({
            "finding_id": f"f-{digest}-{seen[digest]}",
            "gating": R.gating(finding),
            "finding": finding,
        })
    return out


def audit_name(path: Path) -> re.Match[str]:
    match = AUDIT_NAME_RE.match(path.name)
    if match is None:
        raise AuditError(
            f"{path.name} is not an audit artifact name; use audit-<pr>-<pass>.jsonl"
        )
    return match


def source_name(path: Path) -> re.Match[str]:
    match = SOURCE_NAME_RE.match(path.name)
    if match is None:
        raise AuditError(
            f"{path.name} is not a findings artifact name; use the active "
            "review-<pr>-<pass>[.a<attempt>].findings.jsonl"
        )
    return match


def read_source(path: Path) -> list[dict]:
    """Read findings through review-pass.py's strict parser and gating owner."""
    try:
        return R.check_findings_file(R.read_text(path, "findings file"), path)
    except R.Defect as exc:
        raise AuditError(str(exc)) from exc


def _check_exact(record: dict, keys: set[str], where: str) -> None:
    if set(record) != keys:
        missing = sorted(keys - set(record))
        extra = sorted(set(record) - keys)
        detail = (f"; missing {missing}" if missing else "") + (f"; unexpected {extra}" if extra else "")
        raise AuditError(f"{where} carries exactly {sorted(keys)}{detail}")


def _nonblank(value: object, field: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditError(f"{where}: `{field}` must be a non-blank string")
    return value


def serialize(records: list[dict]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)


def parse_audit(text: str, path: Path) -> list[dict]:
    try:
        records = R.parse_lines(text, path.name)
    except R.Defect as exc:
        raise AuditError(str(exc)) from exc
    if not records:
        raise AuditError(f"{path.name} is empty; its first row must be a {HEADER!r} header")
    return records


def _source_for(path: Path, header: dict) -> tuple[Path, list[dict], list[dict]]:
    name = header.get("findings_file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise AuditError(
            f"{path.name} header: `findings_file` must be one artifact basename, not a path"
        )
    audit_match = audit_name(path)
    source_path = path.parent / name
    source_match = source_name(source_path)
    if (
        source_match.group("pr") != audit_match.group("pr")
        or source_match.group("pass") != audit_match.group("pass")
    ):
        raise AuditError(
            f"{path.name} names PR/pass {audit_match.group('pr')}/{audit_match.group('pass')}, but "
            f"{name} names {source_match.group('pr')}/{source_match.group('pass')}"
        )
    findings = read_source(source_path)
    indexed = enumerate_findings(findings)
    return source_path, findings, indexed


def validate_audit(text: str, path: Path, *, require_complete: bool = False) -> dict:
    """Validate proposed or stored audit bytes against the current source findings."""
    rows = parse_audit(text, path)
    header = rows[0]
    _check_exact(header, HEADER_KEYS, f"{path.name} line 1")
    if header["type"] != HEADER:
        raise AuditError(f"{path.name} line 1: type must be {HEADER!r}")
    if header["version"] != FORMAT_VERSION:
        raise AuditError(
            f"{path.name} line 1: unsupported version {header['version']!r}; expected {FORMAT_VERSION!r}"
        )

    source_path, findings, indexed = _source_for(path, header)
    expected_digest = source_digest(findings)
    if header["source_digest"] != expected_digest:
        raise AuditError(
            f"{path.name} is stale: {source_path.name} no longer matches its recorded source digest"
        )
    expected_ids = [item["finding_id"] for item in indexed if item["gating"]]
    if header["gating_ids"] != expected_ids:
        raise AuditError(
            f"{path.name} header does not name exactly the current gating findings, in source order"
        )
    if not expected_ids:
        raise AuditError(f"{source_path.name} has no gating findings to audit")

    by_id = {item["finding_id"]: item for item in indexed}
    gating_ids = set(expected_ids)
    results: dict[str, dict] = {}
    rulings: dict[str, dict] = {}
    consumed: set[str] = set()

    for number, row in enumerate(rows[1:], start=2):
        where = f"{path.name} line {number}"
        row_type = row.get("type")
        if row_type == RESULT:
            _check_exact(row, RESULT_KEYS, where)
            finding_id = _nonblank(row["finding_id"], "finding_id", where)
            if finding_id not in gating_ids:
                description = "a non-gating finding" if finding_id in by_id else "no source finding"
                raise AuditError(
                    f"{where}: {finding_id!r} identifies {description}; only gating findings are audited"
                )
            if finding_id in results:
                raise AuditError(f"{where}: duplicate audit result for {finding_id}")
            if row["verdict"] not in VERDICTS:
                raise AuditError(
                    f"{where}: verdict {row['verdict']!r} is unknown; choose one of {list(VERDICTS)}"
                )
            _nonblank(row["evidence"], "evidence", where)
            if row["verdict"] == "ADJUSTED":
                _nonblank(row["adjusted_repro"], "adjusted_repro", where)
                _nonblank(row["adjusted_fix"], "adjusted_fix", where)
            elif row["adjusted_repro"] != "" or row["adjusted_fix"] != "":
                raise AuditError(
                    f"{where}: adjusted_repro/adjusted_fix are allowed only for an ADJUSTED result"
                )
            results[finding_id] = row
            continue

        if row_type == STANDOFF:
            _check_exact(row, STANDOFF_KEYS, where)
            finding_id = _nonblank(row["finding_id"], "finding_id", where)
            if finding_id not in gating_ids:
                raise AuditError(f"{where}: {finding_id!r} is not a gating source finding")
            if finding_id in rulings:
                raise AuditError(
                    f"{where}: duplicate standoff ruling for {finding_id}; the user's ruling is recorded once"
                )
            if row["ruling"] not in RULINGS:
                raise AuditError(
                    f"{where}: ruling {row['ruling']!r} is unknown; choose one of {list(RULINGS)}"
                )
            _nonblank(row["counter"], "counter", where)
            _nonblank(row["evidence"], "evidence", where)
            result = results.get(finding_id)
            if result is None or result["verdict"] != "REFUTED":
                raise AuditError(
                    f"{where}: a standoff ruling is valid only after this audit recorded REFUTED"
                )
            rulings[finding_id] = row
            continue

        if row_type == CONSUMED:
            # A fix-scope marker records the standoff fixes an earlier `fix-list` already emitted, so a
            # later `fix-list` on a fresh memoryless heartbeat never re-dispatches a landed fix. Each id
            # it names must be a gating finding with a valid standoff ruling recorded BEFORE this row, and
            # no id may be consumed twice.
            _check_exact(row, CONSUMED_KEYS, where)
            ids = row["consumed"]
            if not isinstance(ids, list) or not ids:
                raise AuditError(f"{where}: `consumed` must be a non-empty list of finding ids")
            for finding_id in ids:
                _nonblank(finding_id, "consumed id", where)
                ruling = rulings.get(finding_id)
                if ruling is None or ruling["ruling"] != "valid":
                    raise AuditError(
                        f"{where}: {finding_id!r} has no valid standoff ruling recorded before it; only a "
                        "ruled-valid standoff fix is consumed"
                    )
                if finding_id in consumed:
                    raise AuditError(
                        f"{where}: {finding_id} is already consumed; a standoff fix enters the scope once"
                    )
                consumed.add(finding_id)
            continue

        raise AuditError(
            f"{where}: unknown record type {row_type!r}; expected {RESULT!r}, {STANDOFF!r}, or {CONSUMED!r}"
        )

    missing = [finding_id for finding_id in expected_ids if finding_id not in results]
    if require_complete and missing:
        raise AuditError(
            f"{path.name} is incomplete: {len(missing)} gating finding(s) have no audit result: "
            + ", ".join(missing)
        )

    return {
        "path": path,
        "text": text,
        "header": header,
        "source_path": source_path,
        "indexed": indexed,
        "gating_ids": expected_ids,
        "results": results,
        "rulings": rulings,
        "consumed": consumed,
        "missing": missing,
    }


def load_audit(path: Path, *, require_complete: bool = False) -> dict:
    """Read and validate the whole audit against the current source findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AuditError(f"no audit artifact at {path}; run `finding-audit.py init` first") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditError(f"cannot read {path} as UTF-8 text: {exc}") from exc
    return validate_audit(text, path, require_complete=require_complete)


def _replace(path: Path, text: str) -> None:
    try:
        replace_text(path, text, temp_prefix=f".{path.name}.", encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"cannot atomically write {path}: {exc}") from exc


def _append(state: dict, row: dict) -> None:
    path = state["path"]
    proposed = state["text"] + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    # Validate the proposed bytes before replacement. A write door cannot create an artifact its read
    # door rejects, and any refusal leaves the old file untouched.
    validate_audit(proposed, path)
    _replace(path, proposed)


def cmd_init(args) -> int:
    path = Path(args.file)
    progress_path = Path(args.progress)
    if path.exists():
        raise AuditError(f"{path} already exists; an audit is initialized once and never overwritten")
    if path.parent.resolve() != progress_path.parent.resolve():
        raise AuditError("the audit and progress artifacts must be in the same run directory")
    audit_match = audit_name(path)
    # Bind to the ACTIVE pass identity, not a raw findings path. The progress artifact's name says which
    # pass and which launch attempt these bytes are, and its `pass_identity` line must AGREE with that name
    # — both enforced by the schema owner. Without this, `init` accepted a DEAD attempt's
    # `review-<pr>-<n>.findings.jsonl` while a relaunch (attempt >= 2) was live, and `fix-list` then
    # returned the killed attempt's fix scope.
    try:
        pr, npass, attempt = R.parse_name(progress_path)
        events = R.read_lines(progress_path, "progress file")
        R.check_identity(events, pr, npass, attempt)
    except R.Defect as exc:
        raise AuditError(str(exc)) from exc
    # A superseded attempt's findings must never enter fix scope: accept only the highest launch attempt
    # for this (pr, pass) in the run directory — `active_attempts` is the owner's own selection.
    actives = {candidate.resolve() for candidate in R.active_attempts(progress_path.parent)}
    if progress_path.resolve() not in actives:
        raise AuditError(
            f"{progress_path.name} is not the active launch attempt for pass {npass} of PR {pr}; an audit "
            "binds to the active attempt's findings, never a superseded one"
        )
    if audit_match.group("pr") != pr or audit_match.group("pass") != npass:
        raise AuditError("the audit filename and progress filename must name the same PR and pass")
    # Derive the findings artifact the ONE way `review-pass.py` does — from the progress file's name, never
    # taken as its own argument, so no two doors can disagree about which attempt they belong to.
    findings_path = R.findings_path(progress_path)

    findings = read_source(findings_path)
    indexed = enumerate_findings(findings)
    gating = [item for item in indexed if item["gating"]]
    if not gating:
        raise AuditError(f"{findings_path.name} has no gating findings to audit")
    header = {
        "type": HEADER,
        "version": FORMAT_VERSION,
        "findings_file": findings_path.name,
        "source_digest": source_digest(findings),
        "gating_ids": [item["finding_id"] for item in gating],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = serialize([header])
    # Load the source again before promotion, closing the write/read asymmetry at init.
    validate_audit(text, path)
    _replace(path, text)
    try:
        state = load_audit(path)
    except AuditError:
        path.unlink(missing_ok=True)
        raise
    print(json.dumps({
        "audit": str(path),
        "findings": findings_path.name,
        "gating": [
            {
                "finding_id": item["finding_id"],
                "finding": item["finding"],
            }
            for item in state["indexed"] if item["gating"]
        ],
    }, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_record(args) -> int:
    state = load_audit(Path(args.file))
    if args.finding_id in state["results"]:
        raise AuditError(f"{args.finding_id} already has an audit result; each gating finding gets exactly one")
    if args.finding_id not in state["gating_ids"]:
        all_ids = {item["finding_id"]: item for item in state["indexed"]}
        description = "a non-gating finding" if args.finding_id in all_ids else "no source finding"
        raise AuditError(f"{args.finding_id!r} identifies {description}; only gating findings are audited")
    _nonblank(args.evidence, "evidence", "record")
    adjusted_repro = args.adjusted_repro or ""
    adjusted_fix = args.adjusted_fix or ""
    if args.verdict == "ADJUSTED":
        _nonblank(adjusted_repro, "adjusted_repro", "record")
        _nonblank(adjusted_fix, "adjusted_fix", "record")
    elif adjusted_repro or adjusted_fix:
        raise AuditError("--adjusted-repro/--adjusted-fix are allowed only with --verdict ADJUSTED")
    row = {
        "type": RESULT,
        "finding_id": args.finding_id,
        "verdict": args.verdict,
        "evidence": args.evidence,
        "adjusted_repro": adjusted_repro,
        "adjusted_fix": adjusted_fix,
    }
    _append(state, row)
    # Read back through the same owner before reporting success.
    load_audit(Path(args.file))
    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_verify(args) -> int:
    state = load_audit(Path(args.file), require_complete=True)
    counts = Counter(result["verdict"] for result in state["results"].values())
    print(json.dumps({
        "status": "ok",
        "audit": str(state["path"]),
        "findings": state["header"]["findings_file"],
        "gating": len(state["gating_ids"]),
        "confirmed": counts["CONFIRMED"],
        "adjusted": counts["ADJUSTED"],
        "refuted": counts["REFUTED"],
        "standoff_rulings": len(state["rulings"]),
        "results": [state["results"][finding_id] for finding_id in state["gating_ids"]],
        "rulings": [
            state["rulings"][finding_id]
            for finding_id in state["gating_ids"] if finding_id in state["rulings"]
        ],
    }, sort_keys=True))
    return 0


def cmd_fix_list(args) -> int:
    state = load_audit(Path(args.file), require_complete=True)
    fixes = []
    refutations = []
    standoff_phase = bool(state["rulings"])
    consumed = state["consumed"]
    for item in state["indexed"]:
        finding_id = item["finding_id"]
        if finding_id not in state["gating_ids"]:
            continue
        result = state["results"][finding_id]
        ruling = state["rulings"].get(finding_id)
        # In standoff phase a valid ruling is fix work exactly ONCE. An earlier `fix-list` marks the fix
        # consumed, so a later call (a fresh, memoryless heartbeat) never replays a fix that already
        # landed — even after a second, separate ruling puts new work in scope.
        eligible = (
            ruling is not None and ruling["ruling"] == "valid" and finding_id not in consumed
            if standoff_phase else result["verdict"] in ("CONFIRMED", "ADJUSTED")
        )
        if not eligible:
            if not standoff_phase and result["verdict"] == "REFUTED":
                finding = item["finding"]
                refutations.append({
                    "finding_id": finding_id,
                    "file": finding["file"],
                    "line": finding["line"],
                    "repro": finding["repro"],
                    "audit_evidence": result["evidence"],
                })
            continue
        finding = item["finding"]
        fix = {
            "finding_id": finding_id,
            "disposition": (
                "standoff-valid" if result["verdict"] == "REFUTED" else result["verdict"].lower()
            ),
            "file": finding["file"],
            "line": finding["line"],
            "repro": result["adjusted_repro"] if result["verdict"] == "ADJUSTED" else finding["repro"],
            "fix": result["adjusted_fix"] if result["verdict"] == "ADJUSTED" else finding["fix"],
            "audit_evidence": result["evidence"],
        }
        if ruling is not None:
            fix["standoff_counter"] = ruling["counter"]
            fix["standoff_evidence"] = ruling["evidence"]
        fixes.append(fix)
    # Emitting standoff fixes CONSUMES them durably, in the artifact, before the output is reported —
    # the record must outlive this agent instance. A crash between this write and the driver dispatching
    # the fix loses the emitted work rather than re-dispatching it; for this single-user advisory tool
    # that at-most-once boundary is the deliberate trade against the replay it replaces.
    #
    # This mark-before-emit ordering is INTENTIONAL, not an oversight, and the claim is falsifiable:
    # write-then-emit and emit-then-mark are duals. Mark-first is at-most-once — a failed stdout flush
    # (e.g. `fix-list --json` to /dev/full → exit 120) leaves the marker durable, so the one emitted fix
    # is lost on retry. Emit-first would be at-least-once — a crash after a successful flush but before
    # the marker write REPLAYS the fix, reopening the exact window the `fix_scope` marker was added to
    # close. Neither window shuts without a durable outbox/ack protocol, which the single-user policy
    # says not to build. The loss is a safe residual, not a broken guarantee: the failing call exits
    # NONZERO (signalled to the driver, never silent), the standoff ruling stays durable in the artifact,
    # and losing the emit reverts to the audit's own recorded REFUTED verdict — the gate's default — not
    # to merging a broken change. Accepted single-user residual per the repo's single-user policy.
    if standoff_phase and fixes:
        _append(state, {"type": CONSUMED, "consumed": [fix["finding_id"] for fix in fixes]})
        load_audit(Path(args.file), require_complete=True)
    print(json.dumps({
        "audit": str(state["path"]),
        "findings": state["header"]["findings_file"],
        "source_digest": state["header"]["source_digest"],
        "fixes": fixes,
        "refutations": refutations,
    }, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_rule_standoff(args) -> int:
    state = load_audit(Path(args.file), require_complete=True)
    result = state["results"].get(args.finding_id)
    if result is None or result["verdict"] != "REFUTED":
        raise AuditError("a standoff ruling can be recorded only for a finding this audit REFUTED")
    if args.finding_id in state["rulings"]:
        raise AuditError(
            f"{args.finding_id} already has a standoff ruling; the user's answer is durable and recorded once"
        )
    _nonblank(args.counter, "counter", "rule-standoff")
    _nonblank(args.evidence, "evidence", "rule-standoff")
    row = {
        "type": STANDOFF,
        "finding_id": args.finding_id,
        "ruling": args.ruling,
        "counter": args.counter,
        "evidence": args.evidence,
    }
    _append(state, row)
    load_audit(Path(args.file), require_complete=True)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="bind a new audit JSONL artifact to the active pass's findings")
    init.add_argument("--file", required=True, help="audit-<pr>-<pass>.jsonl to create atomically")
    init.add_argument("--progress", required=True,
                      help="the ACTIVE review-<pr>-<pass>[.a<attempt>].progress.jsonl in the same run "
                           "directory; its findings artifact is derived from this name")

    record = sub.add_parser("record", help="record exactly one evidenced verdict for one gating finding")
    record.add_argument("--file", required=True)
    record.add_argument("--finding-id", required=True,
                        help="stable ID printed by init for the source finding")
    record.add_argument("--verdict", required=True, choices=VERDICTS)
    record.add_argument("--evidence", required=True,
                        help="source/test evidence for CONFIRMED, ADJUSTED, or REFUTED")
    record.add_argument("--adjusted-repro",
                        help="replacement trigger/reproduction; required only for ADJUSTED")
    record.add_argument("--adjusted-fix", help="replacement fix; required only for ADJUSTED")

    verify = sub.add_parser("verify", help="require exactly one result for every gating finding")
    verify.add_argument("--file", required=True)

    fix_list = sub.add_parser(
        "fix-list",
        help="emit the mechanically filtered review-fix scope not yet consumed; emitting a standoff fix "
             "consumes it so a later call never replays it",
    )
    fix_list.add_argument("--file", required=True)
    fix_list.add_argument("--json", action="store_true", required=True,
                          help="required: emit the fix scope as one JSON object")

    standoff = sub.add_parser("rule-standoff",
                              help="record the user's one durable ruling on a re-raised REFUTED finding")
    standoff.add_argument("--file", required=True)
    standoff.add_argument("--finding-id", required=True)
    standoff.add_argument("--ruling", required=True, choices=RULINGS)
    standoff.add_argument("--counter", required=True, help="fresh reviewer's counter to the refutation")
    standoff.add_argument("--evidence", required=True, help="user's ruling and supporting evidence")

    sub.add_parser("self-test", help="run the sibling fixture suite")
    return parser


def self_test() -> int:
    module = load_module_from_path("finding_audit_test", TEST_FILE, register=True)
    if module is None:
        fail(f"cannot load required sibling fixture suite at {TEST_FILE}")
    cases = getattr(module, "CASES", None)
    if not cases:
        fail(f"{TEST_FILE} exports no CASES; the accessor's contract is untested")
    failures = []
    for case in cases:
        try:
            case()
            print(f"PASS     {case.__name__}")
        except Exception as exc:  # noqa: BLE001 - every fixture failure must be reported
            failures.append((case.__name__, exc))
            print(f"FAIL     {case.__name__}: {exc}", file=sys.stderr)
    if failures:
        print(f"finding-audit self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print(f"finding-audit self-test: {len(cases)} cases passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    try:
        return {
            "init": cmd_init,
            "record": cmd_record,
            "verify": cmd_verify,
            "fix-list": cmd_fix_list,
            "rule-standoff": cmd_rule_standoff,
        }[args.cmd](args)
    except AuditError as exc:
        print(f"finding-audit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
