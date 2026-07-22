#!/usr/bin/env python3
"""Fixtures for `carryover.py` — the terminal-ledger distiller.

They live in a SIBLING file, and `carryover.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH. The load-bearing ones are the two REFUSALS a distill must never
skip: a run with ANY non-terminal row is NOT distilled (a live run has no history to write yet), and an
existing `<run-id>.md` is NOT overwritten without `--force` (distilled exactly once). A distiller that
quietly wrote either would pass a naive "did it produce a file?" check and be exactly the corruption this
tool exists to prevent — a half-run recorded as finished, or a second exit clobbering the first.

Ledgers are built through the ledger module itself (`dump`), so a fixture cannot drift from the real
schema; the distiller reads them back through the same module's `load`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

_HERE = Path(__file__).resolve().parent
OWNER = _HERE / "carryover.py"
LEDGER = _HERE / "ledger.py"

NOW = "2026-07-04T18:00:00Z"
SHA_A = "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c"
SHA_B = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7089a1b"
SHA_C = "c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0"


def _load(name: str, path: Path):
    mod = load_module_from_path(name, path)
    if mod is None:
        raise RuntimeError(f"cannot load {path}")
    return mod


C = _load("carryover_owner", OWNER)
L = _load("carryover_ledger_for_test", LEDGER)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise C.SelfTestFailure(msg)


# --- helpers ------------------------------------------------------------------

def _header(run_id: str = "g260704-0915-a3f29c1b", base_branch: str = "main") -> dict:
    return dict(L.HEADER_DEFAULTS, run_id=run_id, base_branch=base_branch)


def _row(pr: str, **over) -> dict:
    return dict(L.ROW_DEFAULTS, pr=pr, id=f"pr{pr}", **over)


def _write_ledger(path: Path, header: dict, rows: list[dict]) -> None:
    L.dump(path, header, rows)


def _distill(tmp: Path, header: dict, rows: list[dict], *, extra: "list[str] | None" = None):
    """Write a ledger, run `distill` through the CLI, and return (code, stdout, stderr, out_dir)."""
    ledger_path = tmp / "state.jsonl"
    out_dir = tmp / "history"
    _write_ledger(ledger_path, header, rows)
    argv = ["distill", "--ledger", str(ledger_path), "--out-dir", str(out_dir), "--now", NOW]
    if extra:
        argv += extra
    code, out, err = capture_cli(C.main, argv)
    return code, out, err, out_dir


def _parse_sections(text: str) -> "dict[str, list[dict]]":
    """Parse the history file back into {section: [row-objects]}. Skips the comment/meta preamble — only
    lines under a `## <name>` heading that begin with `{` are rows.
    """
    out: dict[str, list[dict]] = {}
    cur: "str | None" = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None and line.startswith("{"):
            out[cur].append(json.loads(line))
    return out


def _temp_files(out_dir: Path) -> list[str]:
    """Any leftover atomic-write temp file (`.<name>.tmp`) — must be empty after a refusal or success."""
    if not out_dir.exists():
        return []
    return [p.name for p in out_dir.iterdir() if p.name.endswith(".tmp") or ".md." in p.name]


# --- the happy path: a terminal ledger is distilled ---------------------------

def t_terminal_ledger_is_distilled() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        rows = [
            _row("41", slug="fix-null-deref", status="merged", head_sha=SHA_A, tier="STANDARD",
                 review_rounds="3"),
            _row("52", slug="add-retry-flag", status="aborted", head_sha=SHA_B,
                 ci_reason="required check absent: integration-tests", blocker_ruling="abort@2026-07-04T17:00:00Z"),
            _row("60", slug="change-signature", status="aborted", head_sha=SHA_C,
                 api_approval="declined@2026-07-04T16:00:00Z",
                 ci_reason="api-changing fix declined", blocker_ruling="abort@2026-07-04T16:30:00Z"),
        ]
        code, out, err, out_dir = _distill(tmp, _header(), rows)
        check(code == 0, f"a fully-terminal ledger must distill (exit 0); stderr={err!r}")

        summary = json.loads(out)
        check(summary["run_id"] == "g260704-0915-a3f29c1b", "the summary names the run")
        check(summary["merged"] == 1 and summary["aborted"] == 2 and summary["api_declined"] == 1,
              f"the summary counts each section: got {summary}")

        out_path = out_dir / "g260704-0915-a3f29c1b.md"
        check(out_path.exists(), "distill writes <out-dir>/<run_id>.md")
        check(summary["path"] == str(out_path), "the summary path is the file it wrote")

        parsed = _parse_sections(out_path.read_text(encoding="utf-8"))
        check(len(parsed["merged"]) == 1, "one merged row in the file")
        check(len(parsed["aborted"]) == 2, "two aborted rows in the file")
        check(len(parsed["api-declined"]) == 1, "one api-declined row in the file")

        merged = parsed["merged"][0]
        # v2: every object also carries its effective base. These legacy-inheriting rows (base_branch `-`)
        # resolve to the header base `main`.
        check(merged == {"pr": "41", "slug": "fix-null-deref", "head_sha": SHA_A, "tier": "STANDARD",
                         "review_rounds": "3", "base_branch": "main"},
              f"the merged row projects exactly its documented fields plus effective base: {merged}")

        declined = parsed["api-declined"][0]
        check(declined == {"pr": "60", "slug": "change-signature",
                           "api_approval": "declined@2026-07-04T16:00:00Z", "base_branch": "main"},
              f"the api-declined row carries pr/slug/api_approval/base_branch: {declined}")
        # The declined PR is ALSO aborted — it appears in BOTH sections (a reminder projection).
        check(any(r["pr"] == "60" for r in parsed["aborted"]),
              "a declined-API PR is terminal-aborted and appears in the aborted section too")

        meta = out_path.read_text(encoding="utf-8")
        check(f"distilled_at: {NOW}" in meta, "the distilled-at stamp is the --now value, verbatim")
        check('base_branches: ["main"]' in meta,
              "v2 metadata is a sorted, deduplicated base_branches array (here just the header base)")
        check("format v2" in meta, "the file declares carryover format v2")
        check(summary["base_branches"] == ["main"], f"the summary carries the base set: {summary}")


def t_head_sha_is_never_shortened() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        rows = [_row("41", slug="s", status="merged", head_sha=SHA_A, tier="TRIVIAL", review_rounds="1")]
        code, _out, err, out_dir = _distill(tmp, _header(), rows)
        check(code == 0, f"distill; stderr={err!r}")
        parsed = _parse_sections((out_dir / "g260704-0915-a3f29c1b.md").read_text(encoding="utf-8"))
        check(parsed["merged"][0]["head_sha"] == SHA_A,
              "the carryover file stores the FULL 40-char SHA — never a display truncation")


# --- v2: mixed bases, per-object base, and the sorted/deduplicated array -------

def t_mixed_bases_per_object_and_sorted_array() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # One run, THREE rows on TWO bases — plus a legacy-inheriting row (base `-`) that resolves to the
        # header base. This exercises both channels of `effective_base` in one file.
        header = _header(base_branch="main")
        rows = [
            _row("41", slug="fix-v3", status="merged", head_sha=SHA_A, tier="STANDARD",
                 review_rounds="2", base_branch="v3"),
            _row("52", slug="fix-main", status="merged", head_sha=SHA_B, tier="TRIVIAL",
                 review_rounds="1"),  # base_branch `-` → inherits header `main`
            _row("60", slug="also-v3", status="aborted", head_sha=SHA_C, base_branch="v3",
                 ci_reason="required check absent", blocker_ruling="abort@2026-07-04T16:30:00Z"),
        ]
        code, out, err, out_dir = _distill(tmp, header, rows)
        check(code == 0, f"a mixed-base terminal ledger distills; stderr={err!r}")

        summary = json.loads(out)
        check(summary["base_branches"] == ["main", "v3"],
              f"base_branches is sorted and deduplicated across every row: {summary}")

        text = (out_dir / "g260704-0915-a3f29c1b.md").read_text(encoding="utf-8")
        check('base_branches: ["main", "v3"]' in text,
              "the metadata array is sorted and deduplicated, one entry per distinct base")
        parsed = _parse_sections(text)
        bases = {r["pr"]: r["base_branch"] for sec in parsed.values() for r in sec}
        check(bases == {"41": "v3", "52": "main", "60": "v3"},
              f"each object carries ITS OWN effective base (explicit, else inherited): {bases}")


def t_header_base_dash_is_accepted() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # A new run writes header base `-` (the legacy fallback is unset) and an explicit base per row.
        header = _header(base_branch="-")
        rows = [_row("41", slug="s", status="merged", head_sha=SHA_A, tier="TRIVIAL",
                     review_rounds="1", base_branch="main")]
        code, out, err, out_dir = _distill(tmp, header, rows)
        check(code == 0, f"a `-` header base is NO LONGER refused — base is per-row now; stderr={err!r}")
        summary = json.loads(out)
        check(summary["base_branches"] == ["main"], f"bases come from the rows, not the header: {summary}")
        parsed = _parse_sections((out_dir / "g260704-0915-a3f29c1b.md").read_text(encoding="utf-8"))
        check(parsed["merged"][0]["base_branch"] == "main", "the row's explicit base is projected")


# --- refusal: a live (non-terminal) run is never distilled --------------------

def t_unresolved_effective_base_is_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # BOTH the header base and the row base are `-` (the ROW_DEFAULTS default): `effective_base` resolves
        # to `-`, an UNRESOLVED base. distill must REFUSE rather than stamp `-` into the durable history,
        # where it would poison future per-base prunes (`ledger.py require_effective_base`, the one owner). If
        # that guard is deleted, distill writes `base_branches: ["-"]` and a per-object `base_branch: "-"` —
        # so this fixture FAILS.
        header = _header(base_branch="-")
        rows = [_row("41", slug="done", status="merged", head_sha=SHA_A, tier="STANDARD", review_rounds="2")]
        code, out, err, out_dir = _distill(tmp, header, rows)
        check(code == C.EXIT_STOP,
              f"an unresolved (`-`) base is refused with EXIT_STOP ({C.EXIT_STOP}); got {code}")
        check("41" in err and "no usable effective base" in err,
              f"the refusal NAMES the offending PR and the unresolved base: {err!r}")
        check(out.strip() == "", "a refused distill prints no summary on stdout")
        check(not (out_dir / "g260704-0915-a3f29c1b.md").exists(),
              "a refused distill writes NO history file")
        check(_temp_files(out_dir) == [], "a refused distill leaves NO temp file behind")


def t_non_terminal_row_is_refused_naming_the_pr() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        rows = [
            _row("41", slug="done", status="merged", head_sha=SHA_A, tier="STANDARD", review_rounds="2"),
            _row("52", slug="still-going", status="in_review", head_sha=SHA_B),
        ]
        code, out, err, out_dir = _distill(tmp, _header(), rows)
        check(code == C.EXIT_STOP, f"a live run is refused with EXIT_STOP ({C.EXIT_STOP}); got {code}")
        check("52" in err and "in_review" in err,
              f"the refusal NAMES the offending PR and its status: {err!r}")
        check(out.strip() == "", "a refused distill prints no summary on stdout")
        check(not (out_dir / "g260704-0915-a3f29c1b.md").exists(),
              "a refused distill writes NO history file")
        check(_temp_files(out_dir) == [], "a refused distill leaves NO temp file behind")


# --- refusal: distilled exactly once (existing file, no --force) --------------

def t_existing_file_refused_without_force_then_overwritten_with() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        rows = [_row("41", slug="v1", status="merged", head_sha=SHA_A, tier="STANDARD", review_rounds="2")]
        code, _out, err, out_dir = _distill(tmp, _header(), rows)
        check(code == 0, f"first distill succeeds; stderr={err!r}")
        out_path = out_dir / "g260704-0915-a3f29c1b.md"
        first = out_path.read_text(encoding="utf-8")

        # A DIFFERENT terminal ledger under the same run-id — a second exit must NOT clobber the first.
        rows2 = [_row("41", slug="v2-different", status="merged", head_sha=SHA_B, tier="HIGH",
                      review_rounds="9")]
        code, out, err, out_dir = _distill(tmp, _header(), rows2)
        check(code == C.EXIT_STOP, f"an existing file without --force is refused ({C.EXIT_STOP}); got {code}")
        check("already" in err.lower(), f"the refusal explains the once-only rule: {err!r}")
        check(out.strip() == "", "the refused re-distill prints no summary")
        check(out_path.read_text(encoding="utf-8") == first,
              "the refused re-distill leaves the ORIGINAL file byte-for-byte untouched")
        check(_temp_files(out_dir) == [], "the refused re-distill leaves NO temp file behind")

        # --force is the explicit crash-recovery escape hatch: now it overwrites.
        code, out, err, out_dir = _distill(tmp, _header(), rows2, extra=["--force"])
        check(code == 0, f"--force overwrites; stderr={err!r}")
        rewritten = _parse_sections(out_path.read_text(encoding="utf-8"))
        check(rewritten["merged"][0]["slug"] == "v2-different",
              "--force rewrites the file with the new distill")


# --- refusal: a malformed ledger (the loader's strictness is reused) ----------

def t_malformed_ledger_is_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ledger_path = tmp / "state.jsonl"
        out_dir = tmp / "history"
        ledger_path.write_text("this is not json at all\n", encoding="utf-8")
        code, out, err = capture_cli(
            C.main, ["distill", "--ledger", str(ledger_path), "--out-dir", str(out_dir), "--now", NOW])
        check(code == C.EXIT_OPERATOR,
              f"a malformed ledger is an operator error ({C.EXIT_OPERATOR}); got {code}")
        check(out.strip() == "", "a refused distill prints no summary")
        check(not out_dir.exists() or list(out_dir.iterdir()) == [],
              "a malformed ledger writes nothing under out-dir")


def t_header_without_run_id_is_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # A header whose run_id is unset (`-`) is not a run's ledger. (The base is no longer gated here.)
        header = dict(L.HEADER_DEFAULTS)  # run_id defaults to "-"
        rows = [_row("41", slug="s", status="merged", head_sha=SHA_A, tier="TRIVIAL", review_rounds="1")]
        code, out, err, out_dir = _distill(tmp, header, rows)
        check(code == C.EXIT_OPERATOR,
              f"a header with no run_id is an operator error ({C.EXIT_OPERATOR}); got {code}")
        check("run_id" in err, f"the refusal says the run identity is missing: {err!r}")
        check(out.strip() == "", "no summary on a refused distill")


# --- operator error: the clock is a required input ----------------------------

def t_missing_now_is_an_operator_error() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ledger_path = tmp / "state.jsonl"
        out_dir = tmp / "history"
        _write_ledger(ledger_path, _header(),
                      [_row("41", slug="s", status="merged", head_sha=SHA_A, tier="TRIVIAL",
                            review_rounds="1")])
        # No --now at all: argparse REQUIRES it and exits 2 (the operator-error code) before any work.
        code, out, err = capture_cli(
            C.main, ["distill", "--ledger", str(ledger_path), "--out-dir", str(out_dir)])
        check(code == C.EXIT_OPERATOR,
              f"a missing --now is an operator error ({C.EXIT_OPERATOR}); got {code}")
        check(not (out_dir / "g260704-0915-a3f29c1b.md").exists(),
              "a missing clock writes no history file")


def t_blank_now_is_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ledger_path = tmp / "state.jsonl"
        out_dir = tmp / "history"
        _write_ledger(ledger_path, _header(),
                      [_row("41", slug="s", status="merged", head_sha=SHA_A, tier="TRIVIAL",
                            review_rounds="1")])
        code, out, err = capture_cli(
            C.main, ["distill", "--ledger", str(ledger_path), "--out-dir", str(out_dir), "--now", "   "])
        check(code == C.EXIT_OPERATOR, f"a whitespace --now is refused ({C.EXIT_OPERATOR}); got {code}")
        check(not (out_dir / "g260704-0915-a3f29c1b.md").exists(), "a blank clock writes no file")


# --- an empty terminal run (nothing adopted, or everything gone) --------------

def t_empty_terminal_ledger_distills_with_zero_counts() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        code, out, err, out_dir = _distill(tmp, _header(), [])
        check(code == 0, f"a run with no rows is still a normal exit; stderr={err!r}")
        summary = json.loads(out)
        check(summary["merged"] == 0 and summary["aborted"] == 0 and summary["api_declined"] == 0,
              f"an empty run distills with zero counts: {summary}")
        parsed = _parse_sections((out_dir / "g260704-0915-a3f29c1b.md").read_text(encoding="utf-8"))
        check(parsed == {"merged": [], "aborted": [], "api-declined": []},
              "all three section headings are present and empty")


CASES = [
    ("terminal-distilled", "a fully-terminal ledger is projected into <run-id>.md",
     t_terminal_ledger_is_distilled),
    ("full-sha", "the carryover file stores the full 40-char SHA, never a truncation",
     t_head_sha_is_never_shortened),
    ("mixed-bases", "v2 stamps each object's effective base and a sorted, deduplicated base_branches array",
     t_mixed_bases_per_object_and_sorted_array),
    ("header-base-dash", "a `-` header base is accepted (base is per-row); bases come from the rows",
     t_header_base_dash_is_accepted),
    ("unresolved-base-refused", "a both-`-` ledger (row and header base unset) is refused, writing no `-` into history",
     t_unresolved_effective_base_is_refused),
    ("non-terminal-refused", "a live run is refused, naming the offending PR, writing nothing",
     t_non_terminal_row_is_refused_naming_the_pr),
    ("once-only", "an existing file is not overwritten without --force; --force overwrites",
     t_existing_file_refused_without_force_then_overwritten_with),
    ("malformed-refused", "a malformed ledger is refused (the loader's strictness)",
     t_malformed_ledger_is_refused),
    ("no-run-identity", "a header with no run_id is refused",
     t_header_without_run_id_is_refused),
    ("now-required", "a missing --now is an operator error",
     t_missing_now_is_an_operator_error),
    ("now-not-blank", "a whitespace --now is refused",
     t_blank_now_is_refused),
    ("empty-run", "an empty terminal ledger distills with zero counts",
     t_empty_terminal_ledger_distills_with_zero_counts),
]
